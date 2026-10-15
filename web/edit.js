/* IndexTTS2 Studio — 音频剪辑：裁剪 / 音量增益 / 变速 + 撤销重做
 *
 * 设计要点（详见 docs/AUDIO_EDITING_PLAN_zh.md）：
 *  1. 三槽位状态 {trim, tempo, gain}，每个功能至多一条；撤销/重做用快照栈，
 *     不是"操作数组"——语义清晰，不会出现"撤销后速率变成别的值"。
 *  2. 固定处理顺序 trim → tempo → gain。trim 必须在源时间轴上（否则一改变速
 *     选区就失效）；tempo 在 gain 之前，这样上传服务端的音频峰值必然 ≤ 源峰值，
 *     16bit 不可能削波。
 *  3. 波形永远画源音频（编辑台），结果另用一条只读细波形展示；结果条只在
 *     gain / tempo 有值时才出现——纯裁剪时源波形的选区高亮已经把信息表达完了。
 *  4. 拖动中不压快照，松手才定稿，否则拖一次滑块撤销要按几十次。
 *
 * 依赖 app.js 的全局：$ / API / getAC / sizeCanvas / cssVar / encodeWav /
 * showActionError / player / PAUSE2 / PLAY2（本文件必须在其后加载）
 */
(function () {
  "use strict";

  const HIST_MAX = 50;          // 快照栈上限
  const TEMPO_CACHE_MAX = 8;    // 变速结果缓存条数
  const GAIN_DEBOUNCE = 80;     // ms，拖动增益时的重算节流

  const ED = {
    src: null,                  // 源 AudioBuffer，只读，全程不改动
    srcUrl: "",                 // 源音频 URL（无编辑时直接播它，省一次编码）
    state: { trim: null, tempo: null, gain: null },
    hist: [],
    cursor: -1,
    rendered: null,             // 当前渲染结果
    basePeak: 0,                // trim+tempo 后、gain 之前的峰值（算削波用）
    tempoCache: new Map(),
    rev: 0,                     // 渲染序号，用于丢弃过期的异步结果
    busy: false,
  };
  window.ED = ED;

  let curOutUrl = "";           // 当前挂在结果播放器上的 blob URL
  let curOutKey = "";           // 结果音频对应的状态指纹（相同就不重建，别打断正在播的音频）
  let gainLive = false;         // 增益拖动中：只更新视觉，不重建音频

  /* ---------------- 状态工具 ---------------- */
  const cloneState = (s) => ({
    trim: s.trim ? [s.trim[0], s.trim[1]] : null,
    tempo: s.tempo,
    gain: s.gain,
  });
  const sameState = (a, b) =>
    a.tempo === b.tempo && a.gain === b.gain && String(a.trim) === String(b.trim);

  const srcDur = () => (ED.src ? ED.src.duration : 0);
  const trimKey = () => (ED.state.trim ? ED.state.trim.map((v) => v.toFixed(4)).join("-") : "full");
  const hasOps = () => !!(ED.state.trim || ED.state.tempo || ED.state.gain);
  // 结果条只在有增益/变速时出现（纯裁剪靠源波形上的选区高亮表达）
  const hasResult = () => !!(ED.state.tempo || ED.state.gain);

  /* ---------------- 纯 DSP（全同步，34.9s 音频 < 5ms） ---------------- */
  function sliceBuffer(buf, [s0, s1]) {
    const from = Math.floor(s0 * buf.length);
    const to = Math.floor(s1 * buf.length);
    const n = Math.max(to - from, 1);
    const out = getAC().createBuffer(buf.numberOfChannels, n, buf.sampleRate);
    for (let c = 0; c < buf.numberOfChannels; c++) {
      out.copyToChannel(buf.getChannelData(c).subarray(from, from + n), c);
    }
    return out;
  }

  function gainBuffer(buf, db) {
    const f = Math.pow(10, db / 20);
    const out = getAC().createBuffer(buf.numberOfChannels, buf.length, buf.sampleRate);
    for (let c = 0; c < buf.numberOfChannels; c++) {
      const s = buf.getChannelData(c), d = out.getChannelData(c);
      for (let i = 0; i < s.length; i++) {
        const v = s[i] * f;
        d[i] = v > 1 ? 1 : v < -1 ? -1 : v;
      }
    }
    return out;
  }

  function peakOf(buf) {
    let p = 0;
    for (let c = 0; c < buf.numberOfChannels; c++) {
      const d = buf.getChannelData(c);
      for (let i = 0; i < d.length; i++) {
        const v = d[i] < 0 ? -d[i] : d[i];
        if (v > p) p = v;
      }
    }
    return p;
  }

  /* ---------------- 变速（服务端 librosa，保音高） ---------------- */
  function cacheSet(key, val) {
    ED.tempoCache.set(key, val);
    while (ED.tempoCache.size > TEMPO_CACHE_MAX) {
      ED.tempoCache.delete(ED.tempoCache.keys().next().value);
    }
  }

  async function tempoBuffer(buf, rate) {
    const key = trimKey() + "|" + rate.toFixed(3);
    if (ED.tempoCache.has(key)) return ED.tempoCache.get(key);
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", encodeWav(buf), "in.wav");
      fd.append("rate", String(rate));
      const r = await fetch(API + "/audio/tempo", { method: "POST", body: fd });
      if (!r.ok) {
        let d = "HTTP " + r.status;
        try { d = (await r.json()).detail || d; } catch (e) { /* 非 JSON 错误体 */ }
        throw new Error(d);
      }
      const out = await getAC().decodeAudioData(await r.arrayBuffer());
      cacheSet(key, out);
      return out;
    } finally {
      setBusy(false);
    }
  }

  /* ---------------- 渲染：按固定顺序重放状态 ---------------- */
  /** @returns {Promise<boolean>} 是否渲染成功（供调用方决定是否回滚状态） */
  async function render() {
    if (!ED.src) return false;
    const rev = ++ED.rev;
    let buf = ED.src;
    try {
      if (ED.state.trim) buf = sliceBuffer(buf, ED.state.trim);
      if (ED.state.tempo && ED.state.tempo !== 1) {
        buf = await tempoBuffer(buf, ED.state.tempo);
        if (rev !== ED.rev) return true;          // 已被更新的渲染取代，不算失败
      }
      ED.basePeak = peakOf(buf);                  // 变速会微调峰值，实测 0.41→0.35
      if (ED.state.gain) buf = gainBuffer(buf, ED.state.gain);
    } catch (e) {
      if (rev !== ED.rev) return true;
      showActionError("音频处理失败: " + e.message);
      return false;
    }
    if (rev !== ED.rev) return true;
    ED.rendered = buf;
    paintResult();
    return true;
  }

  /* ---------------- 结果呈现 ---------------- */
  // 下载名归 app.js 的 updateDlName() 管（它知道 outputs/ 里的原始文件名），
  // 但它只在拖选结束/载入结果时被调用，所以每次状态变化后这里要主动通知一次，
  // 否则加了增益下载名还停在 _trim。
  function notifyDlName() {
    if (typeof updateDlName === "function") updateDlName();
  }

  // 撤销 / 重置会把选区一并清掉，源波形上的高亮和「选中 x–y s」文案归 app.js 管，
  // 这里回调它同步一次（见 web/app.js 的 window.syncTrimView）。
  function notifyTrimView() {
    if (typeof syncTrimView === "function") syncTrimView();
  }

  function paintResult() {
    if (!ED.src || !ED.rendered) return;
    const show = hasResult();
    el.out.hidden = !show;
    // 下载入口跟着"有没有编辑"走，而不是跟着结果波形区走：
    // 纯裁剪时结果区不出现（决策四），但裁出来的那一段仍然要能下载
    el.outFoot.hidden = !hasOps();
    el.dlLabel.textContent = show ? "下载编辑结果" : "下载裁剪结果";
    el.outDur.textContent = ED.rendered.duration.toFixed(2) + "s";
    if (show) drawOutWave();
    paintPeak();
    paintEstimate();
    syncChips();
    notifyDlName();
    notifyTrimView();
    // 拖动增益时先不重建音频（会打断播放），松手后才换
    if (!gainLive) paintOutPlayer();
  }

  /** 结果播放器（#outAudio）。
   *
   *  与源播放器（#player）**完全独立**：源波形区的播放按钮、进度条、空格键
   *  永远作用于源音频，编辑结果有自己的播放按钮。早期版本把结果塞进 #player，
   *  导致"一编辑，源音频区播放的就是结果了"——那是错的，两者是两份东西。
   */
  function paintOutPlayer() {
    const key = hasOps() ? JSON.stringify([ED.state.trim, ED.state.tempo, ED.state.gain]) : "";
    if (!key) {                                   // 无操作：结果=源，不需要独立播放器
      if (curOutUrl) {
        outAudio.pause();
        outAudio.removeAttribute("src");
        outAudio.load();
        curOutUrl = "";
      }
      curOutKey = "";
      if (ED.blobUrl) { URL.revokeObjectURL(ED.blobUrl); ED.blobUrl = ""; }
      return;
    }
    if (key === curOutKey) return;                // 结果内容没变，别重建（会打断播放）
    curOutKey = key;
    const wasPlaying = !!outAudio.src && !outAudio.paused;
    const pos = outAudio.currentTime || 0;
    if (ED.blobUrl) URL.revokeObjectURL(ED.blobUrl);
    ED.blobUrl = URL.createObjectURL(encodeWav(ED.rendered));
    curOutUrl = ED.blobUrl;
    outAudio.src = ED.blobUrl;
    if (wasPlaying) {
      // 换 src 会把播放位置归零，续播前 seek 回去
      const resume = () => {
        outAudio.removeEventListener("loadedmetadata", resume);
        if (outAudio.duration) outAudio.currentTime = Math.min(pos, outAudio.duration - 0.05);
        outAudio.play().catch(() => {});
      };
      outAudio.addEventListener("loadedmetadata", resume);
    }
  }

  function paintPeak() {
    const factor = ED.state.gain ? Math.pow(10, ED.state.gain / 20) : 1;
    const outPeak = ED.basePeak * factor;
    const peak = Math.min(outPeak, 1);
    const maxDb = ED.basePeak > 0 ? 20 * Math.log10(1 / ED.basePeak) : 24;
    el.peakNote.textContent = peak > 0 ? "峰值 " + (20 * Math.log10(peak)).toFixed(1) + " dBFS" : "峰值 —";
    const clipped = outPeak > 1.0001;
    el.peakNote.classList.toggle("warn-peak", clipped);
    el.gainMax.hidden = !clipped;
    if (clipped) {
      const safe = Math.max(-24, Math.min(24, Math.floor(maxDb * 2) / 2));
      el.gainMax.textContent = "拉到无损上限 " + (safe >= 0 ? "+" : "") + safe.toFixed(1) + " dB";
      el.gainMax.dataset.db = String(safe);
    }
  }

  function drawOutWave() {
    const cv = el.outWave;
    if (!cv || !ED.rendered || !el.outCtx) return;
    const { w, h } = sizeCanvas(cv);
    const data = ED.rendered.getChannelData(0);
    const step = Math.floor(data.length / w) || 1;
    const mid = h / 2;
    const ink = cssVar("--accent"), pri = cssVar("--primary");
    const pos = outAudio.duration ? (outAudio.currentTime / outAudio.duration) * w : 0;
    el.outCtx.clearRect(0, 0, w, h);
    for (let x = 0; x < w; x++) {
      let min = 1, max = -1;
      for (let i = 0; i < step; i++) {
        const v = data[x * step + i] || 0;
        if (v < min) min = v;
        if (v > max) max = v;
      }
      const y0 = mid - max * mid * 0.92, y1 = mid - min * mid * 0.92;
      el.outCtx.fillStyle = x <= pos ? pri : ink;
      el.outCtx.globalAlpha = x <= pos ? 0.95 : 0.7;
      el.outCtx.fillRect(x, y0, 1, Math.max(1, y1 - y0));
    }
    el.outCtx.globalAlpha = 1;
  }

  /* ---------------- 快照栈 ---------------- */
  function pushHistory() {
    const snap = cloneState(ED.state);
    if (ED.cursor >= 0 && sameState(snap, ED.hist[ED.cursor])) return;
    ED.hist = ED.hist.slice(0, ED.cursor + 1);   // 新分支丢弃 redo 段
    ED.hist.push(snap);
    if (ED.hist.length > HIST_MAX) ED.hist.shift();
    ED.cursor = ED.hist.length - 1;
    syncChips();
  }

  function restore(snap) {
    ED.state = cloneState(snap);
    syncControls();     // 状态 → 滑块/数值框：撤销后控件必须跟着回退
    render();
    syncChips();
  }

  function undo() {
    if (ED.cursor <= 0) return;
    ED.cursor--;
    restore(ED.hist[ED.cursor]);
  }

  function redo() {
    if (ED.cursor >= ED.hist.length - 1) return;
    ED.cursor++;
    restore(ED.hist[ED.cursor]);
  }

  function removeOp(kind) {
    if (!ED.state[kind]) return;
    ED.state[kind] = null;
    pushHistory();
    syncControls();
    render();
  }

  /* ---------------- UI 同步 ---------------- */
  function setBusy(on) {
    ED.busy = on;
    el.tempoRange.disabled = on;
    el.tempoNote.classList.toggle("busy", on);
    if (on) el.tempoNote.textContent = "变速计算中…";
  }

  function syncControls() {
    const g = ED.state.gain;
    el.gainRange.value = String(g || 0);
    el.gainVal.textContent = (g > 0 ? "+" : "") + (g || 0).toFixed(1) + " dB";
    const t = ED.state.tempo;
    el.tempoRange.value = String(t || 1);
    el.tempoVal.textContent = (t || 1).toFixed(2) + "×";
    fill(el.gainRange);
    fill(el.tempoRange);
    paintEstimate();
  }

  // 变速拖动时即时反馈：结果时长纯前端可算，不必等服务端
  function paintEstimate() {
    if (ED.busy) return;
    const rate = Number(el.tempoRange.value) || 1;
    const t = ED.state.trim;
    const base = t ? (t[1] - t[0]) * srcDur() : srcDur();
    el.tempoNote.textContent = base ? "保音高 · 结果 " + (base / rate).toFixed(2) + "s" : "保音高";
  }

  function syncChips() {
    const d = srcDur();
    const items = [];
    if (ED.state.trim) {
      items.push(["trim", "裁剪 " + (ED.state.trim[0] * d).toFixed(2) + "–" + (ED.state.trim[1] * d).toFixed(2) + "s"]);
    }
    if (ED.state.tempo) items.push(["tempo", "变速 " + ED.state.tempo.toFixed(2) + "×"]);
    if (ED.state.gain) {
      items.push(["gain", "增益 " + (ED.state.gain > 0 ? "+" : "") + ED.state.gain.toFixed(1) + "dB"]);
    }
    el.chips.textContent = "";
    if (!items.length) {
      const span = document.createElement("span");
      span.className = "ops-empty";
      span.textContent = "暂无操作";
      el.chips.appendChild(span);
    } else {
      for (const [kind, text] of items) {
        const chip = document.createElement("span");
        chip.className = "op-chip";
        chip.appendChild(document.createTextNode(text));
        const del = document.createElement("i");
        del.textContent = "✕";
        del.title = "移除该操作";
        del.setAttribute("aria-label", "移除操作 " + text);
        del.addEventListener("click", () => removeOp(kind));
        chip.appendChild(del);
        el.chips.appendChild(chip);
      }
    }
    el.undo.disabled = ED.cursor <= 0;
    el.redo.disabled = ED.cursor >= ED.hist.length - 1;
    if (el.resetAll) el.resetAll.disabled = !hasOps();   // 本来就没编辑，没什么可重置
  }

  /** 一键归零：三项操作全清（与「撤销」不同——撤销只退一步）。
   *  本身也压一条快照，所以「重置全部」可以被 Ctrl+Z 撤回来。 */
  function resetAll() {
    if (!hasOps()) return;
    ED.state = { trim: null, tempo: null, gain: null };
    pushHistory();
    syncControls();
    render();
  }

  /* ---------------- 对外 API ---------------- */
  ED.setSource = function (buffer, url) {
    ED.src = buffer;
    ED.srcUrl = url || "";
    ED.state = { trim: null, tempo: null, gain: null };
    ED.hist = [cloneState(ED.state)];
    ED.cursor = 0;
    ED.rendered = buffer;          // 无操作时结果即源
    ED.basePeak = buffer ? peakOf(buffer) : 0;
    ED.rev++;
    ED.tempoCache.clear();
    if (ED.blobUrl) { URL.revokeObjectURL(ED.blobUrl); ED.blobUrl = ""; }
    curOutUrl = "";
    curOutKey = "";
    outAudio.pause();
    outAudio.removeAttribute("src");
    setBusy(false);
    el.out.hidden = true;
    el.outFoot.hidden = true;
    syncControls();
    syncChips();
    paintPeak();
    notifyDlName();
  };

  ED.clear = function () {
    ED.src = null;
    ED.srcUrl = "";
    ED.rendered = null;
    ED.basePeak = 0;
    ED.state = { trim: null, tempo: null, gain: null };
    ED.hist = [];
    ED.cursor = -1;
    ED.rev++;
    ED.tempoCache.clear();
    if (ED.blobUrl) { URL.revokeObjectURL(ED.blobUrl); ED.blobUrl = ""; }
    curOutUrl = "";
    curOutKey = "";
    outAudio.pause();
    outAudio.removeAttribute("src");
    el.out.hidden = true;
    el.outFoot.hidden = true;
    el.chips.textContent = "";
    el.undo.disabled = true;
    el.redo.disabled = true;
    if (el.resetAll) el.resetAll.disabled = true;
  };

  /** 拖动中的实时选区：只更新状态与芯片，不重渲染音频
   *（拖一次不该产生几十个变速请求，也不该压几十条快照）。 */
  ED.setTrimLive = function (sel) {
    ED.state.trim = sel ? [sel[0], sel[1]] : null;
    syncChips();
    paintEstimate();
  };

  /** 选区定稿（比例区间，源时间轴）：压快照 + 重渲染。app.js 在 pointerup 里调用。 */
  ED.setTrim = function (sel) {
    ED.state.trim = sel ? [sel[0], sel[1]] : null;
    pushHistory();
    syncControls();
    render();
  };

  ED.getState = () => ED.state;
  ED.getRendered = () => ED.rendered;
  ED.hasOps = hasOps;
  ED.hasResult = hasResult;
  ED.undo = undo;
  ED.redo = redo;

  /* ---------------- DOM ---------------- */
  // 复用 app.js 的滑块填充逻辑（--fill 变量驱动 .srow 的渐变背景），保证观感一致
  const fill = (elm) => {
    if (typeof sliderFill === "function") sliderFill(elm);
  };

  const el = {
    gainRange: $("gainRange"), gainVal: $("gainVal"), peakNote: $("peakNote"), gainMax: $("gainMax"),
    tempoRange: $("tempoRange"), tempoVal: $("tempoVal"), tempoNote: $("tempoNote"),
    chips: $("opsChips"), undo: $("undoBtn"), redo: $("redoBtn"), resetAll: $("resetAllBtn"),
    out: $("resultPreview"), outWave: $("waveOut"), outDur: $("outDur"),
    outPlay: $("outPlay"), outPlayIcon: $("outPlayIcon"),
    outFoot: $("outFoot"), dlLabel: $("dlBtnLabel"),
  };
  el.outCtx = el.outWave ? el.outWave.getContext("2d") : null;
  const outAudio = $("outAudio");       // 结果专用播放器，与 app.js 的 player 互不影响

  /* ---------------- 事件绑定 ---------------- */
  let gainTimer = 0;
  el.gainRange.addEventListener("input", () => {
    const db = Number(el.gainRange.value);
    ED.state.gain = db === 0 ? null : db;       // 拖回 0 等于取消该操作
    el.gainVal.textContent = (db > 0 ? "+" : "") + db.toFixed(1) + " dB";
    el.gainMax.hidden = true;
    fill(el.gainRange);
    gainLive = true;
    paintPeak();                 // 峰值实时跟着滑块走，不等 debounce 后的 render
    clearTimeout(gainTimer);
    gainTimer = setTimeout(render, GAIN_DEBOUNCE);
  });
  el.gainRange.addEventListener("change", () => {
    clearTimeout(gainTimer);
    gainLive = false;
    pushHistory();
    render();
  });

  el.gainMax.addEventListener("click", () => {
    el.gainRange.value = el.gainMax.dataset.db || "0";
    el.gainRange.dispatchEvent(new Event("input"));
    el.gainRange.dispatchEvent(new Event("change"));
  });

  el.tempoRange.addEventListener("input", () => {           // 拖动即时更新数字，松手才请求服务端
    const rate = Number(el.tempoRange.value) || 1;
    fill(el.tempoRange);
    el.tempoVal.textContent = rate.toFixed(2) + "×";
    paintEstimate();
  });
  el.tempoRange.addEventListener("change", async () => {     // 松手才请求服务端
    const prev = ED.state.tempo;
    const rate = Number(el.tempoRange.value);
    ED.state.tempo = Math.abs(rate - 1) < 0.001 ? null : rate;
    el.tempoVal.textContent = (ED.state.tempo || 1).toFixed(2) + "×";
    pushHistory();
    if (!(await render())) {                                // 失败回滚，别留下一个假的 chip
      ED.state.tempo = prev;
      el.tempoRange.value = String(prev || 1);
      el.tempoVal.textContent = (prev || 1).toFixed(2) + "×";
      syncChips();
      paintEstimate();
    }
  });

  el.undo.addEventListener("click", undo);
  el.redo.addEventListener("click", redo);
  if (el.resetAll) el.resetAll.addEventListener("click", resetAll);

  el.outPlay.addEventListener("click", () => {
    if (!outAudio.src) return;
    if (outAudio.paused) outAudio.play(); else outAudio.pause();
  });
  outAudio.addEventListener("play", () => { if (el.outPlayIcon) el.outPlayIcon.innerHTML = PAUSE2; });
  outAudio.addEventListener("pause", () => { if (el.outPlayIcon) el.outPlayIcon.innerHTML = PLAY2; });
  outAudio.addEventListener("ended", () => { if (el.outPlayIcon) el.outPlayIcon.innerHTML = PLAY2; });
  outAudio.addEventListener("timeupdate", () => { if (hasResult()) drawOutWave(); });

  // 两个播放器互斥：源音频和编辑结果不该叠着响
  player.addEventListener("play", () => { if (!outAudio.paused) outAudio.pause(); });
  outAudio.addEventListener("play", () => { if (!player.paused) player.pause(); });

  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
    if (typing) return;
    if (!(e.ctrlKey || e.metaKey)) return;
    const k = (e.key || "").toLowerCase();
    if (k === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
    else if ((k === "z" && e.shiftKey) || k === "y") { e.preventDefault(); redo(); }
  });

  const ro = new ResizeObserver(() => { if (hasResult()) drawOutWave(); });
  if (el.outWave) ro.observe(el.outWave);
})();
