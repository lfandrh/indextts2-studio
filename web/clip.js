/* ==========================================================================
   参考音频裁剪弹窗
   --------------------------------------------------------------------------
   为什么单独一个文件：app.js 已经 3300+ 行，把「波形选区 + 拖拽 + 试听」
   塞进去只会让下一次改动更难。这里只做一件事——把一段 AudioBuffer 的
   某个时间区间选出来，resolve 成 { s0, s1 }。

   职责边界（重要）：
     本文件不认识 encodeWav / setSpkFile / 预设，也不知道后端。
     编码、写回表单、问「要不要存预设」全由调用方（app.js）处理。
     这样它既能给参考音频用，将来预设页要复用也只是传另一个 buffer 进来。

   为什么在前端切而不是后端：
     - 便携包不带 ffmpeg，加进去约 80MB，还多一个二进制依赖；
     - 服务端 soundfile 只写 wav/flac/ogg，mp3 输入还得先解码；
     - 画波形本来就要在前端解码，切片是纯内存操作，零网络往返。

   交互约定：
     - 拖把手 = 改边界；在选区内拖 = 整体平移；在选区外拖 = 重新框一段
     - 把手吸附 10ms，不会出现 12.397s 这种数
     - 默认选区：超过 15 秒就选前 15 秒（与服务端截断行为一致，
       用户什么都不动直接点确定 == 确认「就用前 15 秒」），否则全选
   ========================================================================== */
window.ClipDialog = (function () {
  "use strict";

  /* 选区最短 50ms：再短提取不出音色，而且 0 长度会让「时长→像素」换算除零 */
  const MIN_LEN = 0.05;
  /* 把手吸附到 10ms */
  const SNAP = 0.01;
  /* 离边界 10px 以内算「抓把手」。把手视觉 17px 宽（半宽 8.5px），
     命中只比视觉多 1.5px —— 定 16px 时把手外侧还有 9.5px 空白也算抓，
     按下就会把边界拽到指针位置，手感发飘。 */
  const NEAR_PX = 10;
  /* 选区外按下后要横向拖出这么多像素才开始重框。
     不足这个距离视为「点了一下」，选区一动不动 —— 而原来的实现是拖 3px
     就把整个选区清掉，手滑一次全没了。 */
  const NEW_PX = 8;
  /* 重框把原选区换掉时，新选区「小到什么程度」要提醒一句并给回退出口：
     短于 0.5 秒（本来就不能应用），或者不到原选区的 15%。
     注意只提醒 + 给按钮，**不自动还原** —— 自动还原会让真想框一小段的人
     怎么框都被弹回去，那才是真的「选不了了」。 */
  const SHRINK_MIN = 0.5;
  const SHRINK_RATIO = 0.15;
  /* 一次性提示停留时长（毫秒） */
  const HINT_MS = 4000;
  /* 与 #spkWave 同一套柱子规格（2px 柱 / 4px 步进），两处波形读起来是一族的 */
  const BAR = { pitch: 4, w: 2, r: 1, amp: 0.78, floor: 2 };
  /* 服务端 infer_v2 的 _load_and_cut_audio 硬截断在 15 秒 */
  const SERVER_CAP = 15;

  const dom = {};
  let buffer = null;
  let sel = { s0: 0, s1: 0 };
  let drag = null;        // { mode: 'left'|'right'|'move'|'new', anchor, grab, len }
  let preview = null;     // { src, startAt, raf }
  let playPos = null;     // 试听进度（秒），null = 不在播
  let resolveOpen = null;
  let prevFocus = null;
  let busy = false;
  let ro = null;
  let hint = null;        // { tone, text } 临时顶替状态条的提示（拖动引导 / 一次性的说明）
  let hintTimer = 0;      // 一次性提示的到期清场，close 时必须清掉
  let undo = null;        // { s0, s1 } 重框前的选区：状态条上「恢复上一个选区」用它

  const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
  const dur = () => (buffer && buffer.duration) || 0;
  const fmt = (t) => `${t.toFixed(2)}s`;

  /* 临时提示：顶替 statusOf 的那一行，把「为什么没反应 / 为什么还原了」写出来。
     ms 省略 = 一直留着（拖动过程中由代码逐帧更新）；给了 ms = 到点自动回到常规提示。 */
  function setHint({ tone, text, ms }) {
    clearTimeout(hintTimer);
    hintTimer = 0;
    hint = { tone, text };
    updateUI();
    if (ms) hintTimer = setTimeout(() => { hintTimer = 0; hint = null; updateUI(); }, ms);
  }

  function clearHint() {
    if (!hint && !hintTimer) return;
    clearTimeout(hintTimer);
    hintTimer = 0;
    hint = null;
  }

  /* ---------------- DOM / 事件绑定（首次 open 时做一次） ---------------- */

  function ensure() {
    if (dom.clipMask) return true;
    const ids = ["clipMask", "clipBox", "clipMeta", "clipStage", "clipWave",
      "clipHandleL", "clipHandleR", "clipAxisL", "clipAxisR", "clipRange",
      "clipStatus", "clipStatusText", "clipStatusFix", "clipPreview",
      "clipPreviewLabel", "clipApply", "clipCancel", "clipClose",
      "clipAll", "clipFirst15", "clipNone"];
    for (const id of ids) {
      dom[id] = document.getElementById(id);
      if (!dom[id]) return false;   // HTML 没同步更新时别让整页跟着炸
    }
    bind();
    return true;
  }

  function bind() {
    dom.clipCancel.addEventListener("click", () => close(null));
    dom.clipClose.addEventListener("click", () => close(null));
    dom.clipMask.addEventListener("click", (e) => { if (e.target === dom.clipMask) close(null); });
    dom.clipApply.addEventListener("click", commit);
    dom.clipPreview.addEventListener("click", togglePreview);
    dom.clipAll.addEventListener("click", () => setSel(0, dur()));
    dom.clipFirst15.addEventListener("click", () => setSel(0, Math.min(SERVER_CAP, dur())));
    dom.clipNone.addEventListener("click", () => setSel(0, 0));
    // 状态条右侧那个按钮一个位置两种用途：正常情况下是「截到 15 秒」，
    // 误框变短时变成「恢复上一个选区」。按 dataset.act 分流，避免两个按钮抢位置。
    dom.clipStatusFix.addEventListener("click", () => {
      if (dom.clipStatusFix.dataset.act === "undo" && undo) {
        const back = undo;
        undo = null;
        setSel(back.s0, back.s1);
        setHint({ tone: "hint", text: `已恢复为 ${fmt(back.s0)} – ${fmt(back.s1)}`, ms: HINT_MS });
        return;
      }
      // 「截到 15 秒」：保住选区起点，把尾巴收进服务端真正会用的长度
      const s0 = sel.s0;
      setSel(s0, Math.min(s0 + SERVER_CAP, dur()));
    });

    dom.clipStage.addEventListener("pointerdown", onDown);
    dom.clipStage.addEventListener("pointermove", onMove);
    dom.clipStage.addEventListener("pointerup", onUp);
    dom.clipStage.addEventListener("pointercancel", onUp);
    dom.clipHandleL.addEventListener("keydown", (e) => onHandleKey("l", e));
    dom.clipHandleR.addEventListener("keydown", (e) => onHandleKey("r", e));

    // 弹窗开着时接管 Esc / Tab。用 capture 是为了抢在页面级快捷键之前处理。
    document.addEventListener("keydown", (e) => {
      if (!dom.clipMask || dom.clipMask.hidden) return;
      if (e.key === "Escape") { e.preventDefault(); close(null); return; }
      if (e.key === "Tab") trapFocus(e);
    }, true);
  }

  function trapFocus(e) {
    const list = [...dom.clipBox.querySelectorAll(
      'button:not(:disabled), [tabindex]:not([tabindex="-1"])')]
      .filter((el) => el.getClientRects().length > 0);
    if (!list.length) return;
    const first = list[0], last = list[list.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  /* ---------------- 坐标换算 ---------------- */

  /* rect 可选：拖动是每帧都要算的高频路径，调用方已经量过就把结果带进来，
     省掉一次 getBoundingClientRect（写样式后读它等于强制同步布局）。 */
  function timeAt(clientX, rect) {
    const r = rect || dom.clipWave.getBoundingClientRect();
    if (!r.width) return 0;
    return clamp(((clientX - r.left) / r.width) * dur(), 0, dur());
  }
  function snap(t) { return Math.round(t / SNAP) * SNAP; }

  /* ---------------- 选区 ---------------- */

  function setSel(a, b) {
    const d = dur();
    let s0 = clamp(a, 0, d), s1 = clamp(b, 0, d);
    if (s0 > s1) { const t = s0; s0 = s1; s1 = t; }
    sel.s0 = s0; sel.s1 = s1;
    clearHint();
    undo = null;
    stopPreview();
    updateUI();
  }

  function statusOf(len) {
    if (len < 0.5) return { tone: "danger", text: `选区 ${len.toFixed(2)} 秒，太短了，至少 0.5 秒` };
    if (len < 3) return { tone: "warn", text: `${len.toFixed(2)} 秒，偏短，克隆效果可能不稳` };
    if (len <= SERVER_CAP + 1e-6) return { tone: "ok", text: `${len.toFixed(2)} 秒，落在推荐区间 3~15 秒` };
    return { tone: "warn", text: `${len.toFixed(2)} 秒，超出 15 秒的部分不会参与克隆`, fix: true };
  }

  function updateUI() {
    const d = dur();
    const p0 = d > 0 ? (sel.s0 / d) * 100 : 0;
    const p1 = d > 0 ? (sel.s1 / d) * 100 : 100;
    dom.clipHandleL.style.left = p0 + "%";
    dom.clipHandleR.style.left = p1 + "%";
    dom.clipHandleL.setAttribute("aria-valuemin", "0");
    dom.clipHandleL.setAttribute("aria-valuemax", d.toFixed(2));
    dom.clipHandleL.setAttribute("aria-valuenow", sel.s0.toFixed(2));
    dom.clipHandleL.setAttribute("aria-valuetext", `${fmt(sel.s0)}`);
    dom.clipHandleR.setAttribute("aria-valuemin", "0");
    dom.clipHandleR.setAttribute("aria-valuemax", d.toFixed(2));
    dom.clipHandleR.setAttribute("aria-valuenow", sel.s1.toFixed(2));
    dom.clipHandleR.setAttribute("aria-valuetext", `${fmt(sel.s1)}`);

    dom.clipRange.textContent = `${fmt(sel.s0)} – ${fmt(sel.s1)}`;

    const real = statusOf(sel.s1 - sel.s0);
    // 提示优先显示，但「能不能应用」永远按真实选区判：提示只是文案，不能放过短于 0.5 秒的选区
    const st = hint || real;
    dom.clipStatus.dataset.tone = st.tone;
    dom.clipStatusText.textContent = st.text;

    // 一个按钮位，三种状态：有可回退的旧选区 → 恢复；超 15 秒 → 截断；都不是 → 收起。
    // 「恢复」比「截断」优先：误框之后用户最需要的是把刚才那段找回来。
    const fixBtn = dom.clipStatusFix;
    if (undo) {
      fixBtn.dataset.act = "undo";
      fixBtn.textContent = "恢复上一个选区";
      fixBtn.hidden = false;
    } else if (real.fix) {
      fixBtn.dataset.act = "cap";
      fixBtn.textContent = "截到 15 秒";
      fixBtn.hidden = false;
    } else {
      fixBtn.hidden = true;
    }
    // 0.5 秒以下不产出：切出来的是个提取不出音色的空壳，不如直接拦住
    dom.clipApply.disabled = busy || real.tone === "danger";
    paint();
  }

  /* ---------------- 绘制 ---------------- */

  function paint() {
    if (!buffer || !dom.clipWave) return;
    const { w, h } = sizeCanvas(dom.clipWave);
    const ctx = dom.clipWave.getContext("2d");
    ctx.clearRect(0, 0, w, h);
    if (!w || !h) return;

    const d = dur();
    const mid = h / 2;
    const accent = cssVar("--accent");
    const dark = document.documentElement.dataset.theme === "dark";

    // 零轴：柱子中间那根淡线，帮眼睛锚住基线（与参考音频区同一做法）
    ctx.globalAlpha = 1;
    ctx.fillStyle = cssVar("--border");
    ctx.fillRect(0, mid - 0.5, w, 1);

    const x0 = d > 0 ? (sel.s0 / d) * w : 0;
    const x1 = d > 0 ? (sel.s1 / d) * w : 0;
    const hasSel = x1 - x0 >= 1;

    // 选区底：极淡的一层，让「框了哪一段」在密集柱阵里也能一眼看出
    if (hasSel) {
      ctx.fillStyle = accent;
      ctx.globalAlpha = 0.09;
      ctx.fillRect(x0, 0, x1 - x0, h);
    }

    const data = buffer.getChannelData(0);
    const n = Math.max(1, Math.floor(w / BAR.pitch));
    const xStart = Math.round((w - n * BAR.pitch + BAR.w) / 2);
    const step = Math.max(1, Math.floor(data.length / n));
    // 选区外压暗但不能压没：用户要看清"哪里有声"，才知道该往哪拖。
    // 0.30 太淡（浅色下只剩零散柱尖），0.42 还留着 2.2 倍的明暗差。
    const quiet = dark ? 0.36 : 0.42;
    ctx.fillStyle = accent;

    for (let i = 0; i < n; i++) {
      const base = i * step;
      let peak = 0;
      for (let k = 0; k < step; k++) {
        const v = data[base + k] || 0;
        const a = v < 0 ? -v : v;
        if (a > peak) peak = a;
      }
      const bh = Math.max(BAR.floor, peak * mid * BAR.amp);
      const x = xStart + i * BAR.pitch;
      // 用柱心判断内外：贴边那根柱子不会因为取整来回跳成两种颜色
      const cx = x + BAR.w / 2;
      ctx.globalAlpha = hasSel && cx >= x0 && cx <= x1 ? 0.92 : quiet;
      if (ctx.roundRect) {
        ctx.beginPath();
        ctx.roundRect(x, mid - bh, BAR.w, bh * 2, Math.min(BAR.r, bh));
        ctx.fill();
      } else {
        ctx.fillRect(x, mid - bh, BAR.w, bh * 2);
      }
    }

    // 选区两条边界：2px 实线，与把手描边同色，读作「这一段的两头」
    if (hasSel) {
      ctx.globalAlpha = 1;
      ctx.fillRect(clamp(x0, 0, w - 2), 0, 2, h);
      ctx.fillRect(clamp(x1 - 2, 0, w - 2), 0, 2, h);
    }

    // 试听进度：品牌红细线（与参考音频区「已播部分」同色）
    if (playPos !== null && d > 0) {
      const px = clamp((playPos / d) * w, 1, w - 1);
      ctx.globalAlpha = 1;
      ctx.fillStyle = cssVar("--primary");
      ctx.fillRect(px - 1, 0, 2, h);
    }
    ctx.globalAlpha = 1;
  }

  /* ---------------- 拖拽 ---------------- */

  function onDown(e) {
    if (busy || (e.pointerType === "mouse" && e.button !== 0)) return;
    const d = dur();
    if (!d) return;
    const r = dom.clipWave.getBoundingClientRect();
    if (!r.width) return;

    // 一次 reflow 同时拿到像素位置与时间：拖拽是每 pointermove 都要算的高频路径，
    // 不再额外调 timeAt（那会再量一次 getBoundingClientRect）。
    const x = clamp(e.clientX - r.left, 0, r.width);
    const raw = (x / r.width) * d;
    const x0 = (sel.s0 / d) * r.width;
    const x1 = (sel.s1 / d) * r.width;
    const dL = Math.abs(x - x0), dR = Math.abs(x - x1);
    const len = sel.s1 - sel.s0;

    let mode;
    if (len < SNAP) mode = "new";                 // 清空态：拖一段新的
    else if (dL <= NEAR_PX && dL <= dR) mode = "left";
    else if (dR <= NEAR_PX) mode = "right";
    else if (raw > sel.s0 && raw < sel.s1) mode = "move";
    else mode = "pending";                        // 点在选区外：拖够 NEW_PX 才算重框

    clearHint();
    // 一旦开始新的拖动，上一次的重框就不再是「刚发生的误操作」，回退入口随之失效
    undo = null;
    // lastX 是增量模型的原点：之后每帧只取「这一帧指针挪了多少」，
    // 不再把边界对齐到指针绝对位置 —— 后者就是「按下瞬间边界蹦过去」的根源。
    drag = {
      mode,
      anchor: raw,
      startX: e.clientX,
      lastX: e.clientX,
      len,
      prev: { s0: sel.s0, s1: sel.s1 },
    };
    if (mode === "new") sel.s0 = sel.s1 = snap(raw);

    // 抓住哪一头就点亮哪一头：按下时手上要有「抓住了」的反馈，
    // 否则只有光标从 crosshair 变成 grabbing，几乎看不出来。
    if (mode === "left") {
      dom.clipHandleL.classList.add("is-drag");
      dom.clipHandleL.focus({ preventScroll: true });
    } else if (mode === "right") {
      dom.clipHandleR.classList.add("is-drag");
      dom.clipHandleR.focus({ preventScroll: true });
    }

    stopPreview();
    dom.clipStage.classList.add("dragging");
    dom.clipStage.dataset.drag = mode;
    // 拖动时状态条每帧都换字，aria-live 会把它念成机关枪；松手后再恢复播报
    dom.clipStatusText.setAttribute("aria-live", "off");
    try { dom.clipStage.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }

    if (mode === "pending") {
      // 按下不动选区。先把「松开也不会改」说清楚，别让人以为点坏了。
      setHint({ tone: "hint", text: `松开不会改动选区；按住横向拖出 ${NEW_PX}px 可重新框选` });
    }
    e.preventDefault();
  }

  function onMove(e) {
    if (!drag) return;
    const d = dur();
    const r = dom.clipWave.getBoundingClientRect();
    const w = r.width || 1;

    /* 增量模型：只取这一帧指针挪动的像素量换算成时间，累加到当前边界上。
       好处有两个：① 按下瞬间不位移（原来是把边界绝对对齐到指针，按在把手外
       12px 处边界会直接跳 1.1 秒）；② 任何时候都能用 Shift 降档，不用重设基准。 */
    const dx = e.clientX - drag.lastX;
    drag.lastX = e.clientX;
    const dt = (dx / w) * d * (e.shiftKey ? 0.2 : 1);

    if (drag.mode === "pending") {
      // 3px 太容易误触，现在要拖够 NEW_PX；没拖够就只更新那句引导，选区一动不动
      const far = Math.abs(e.clientX - drag.startX);
      if (far < NEW_PX) {
        setHint({ tone: "hint", text: `再横向拖 ${Math.ceil(NEW_PX - far)}px 开始重新框选（现在松开不改动选区）` });
        return;
      }
      drag.mode = "new";
      drag.anchor = snap(drag.anchor);
      sel.s0 = sel.s1 = drag.anchor;
      dom.clipStage.dataset.drag = "new";
      clearHint();
    }

    if (drag.mode === "new") {
      // 重框走绝对位置：从按下的那个点拉到现在，这才是「框」
      const t = snap(clamp(timeAt(e.clientX, r), 0, d));
      sel.s0 = Math.min(drag.anchor, t);
      sel.s1 = Math.max(drag.anchor, t);
    } else if (drag.mode === "left") {
      sel.s0 = clamp(snap(sel.s0 + dt), 0, Math.max(sel.s1 - MIN_LEN, 0));
    } else if (drag.mode === "right") {
      sel.s1 = clamp(snap(sel.s1 + dt), Math.min(sel.s0 + MIN_LEN, d), d);
    } else if (drag.mode === "move") {
      // 整体平移：长度锁死，只在两端留出边界
      const len = drag.len;
      const s0 = clamp(snap(sel.s0 + dt), 0, Math.max(d - len, 0));
      sel.s0 = s0;
      sel.s1 = s0 + len;
    }
    updateUI();
  }

  function onUp(e) {
    if (!drag) return;
    const d = dur();
    const wasPending = drag.mode === "pending";
    const wasNew = drag.mode === "new";
    const prev = drag.prev;
    const len = sel.s1 - sel.s0;

    drag = null;
    dom.clipStage.classList.remove("dragging");
    delete dom.clipStage.dataset.drag;
    dom.clipHandleL.classList.remove("is-drag");
    dom.clipHandleR.classList.remove("is-drag");
    dom.clipStatusText.setAttribute("aria-live", "polite");   // 恢复状态条播报
    try { dom.clipStage.releasePointerCapture(e.pointerId); } catch (err) { /* 已释放 */ }

    if (wasPending) {
      // 在选区外点了一下就松开：明说「没坏、选区还在」，否则很容易被当成「点不动了」
      setHint({ tone: "hint", text: "选区未改动 · 要重新框选，请按住鼠标横向拖出一段", ms: HINT_MS });
    } else if (wasNew) {
      const prevLen = prev.s1 - prev.s0;
      // 新框比原来小一大截：只提示 + 给回退按钮，选区本身按用户框的来。
      // 这里刻意不自动还原 —— 否则真想框一小段的人每次松手都被弹回去，等于框不了。
      if (prevLen >= SHRINK_MIN && (len < SHRINK_MIN || len < prevLen * SHRINK_RATIO)) {
        undo = { s0: prev.s0, s1: prev.s1 };
        setHint({
          tone: "warn",
          text: `框出的选区只有 ${len.toFixed(2)} 秒，比原来的 ${prevLen.toFixed(2)} 秒短很多`,
        });
      }
    } else if (len > 0 && len < MIN_LEN) {
      // 拖出了一点点就松手（< 50ms）时补到最小长度，否则用户看到 0.03 秒的选区
      // 会以为界面坏了；真正「没拖动」（长度 0）保持清空态，让他重新框。
      if (sel.s1 + (MIN_LEN - len) <= d) sel.s1 = sel.s0 + MIN_LEN;
      else sel.s0 = Math.max(sel.s1 - MIN_LEN, 0);
    }
    updateUI();
  }

  function onHandleKey(which, e) {
    const d = dur();
    if (!d) return;
    const step = e.shiftKey ? 0.2 : 0.05;
    let t = which === "l" ? sel.s0 : sel.s1;

    if (e.key === "ArrowLeft") t -= step;
    else if (e.key === "ArrowRight") t += step;
    else if (e.key === "Home") t = 0;
    else if (e.key === "End") t = d;
    else return;

    e.preventDefault();
    t = clamp(snap(t), 0, d);
    if (which === "l") sel.s0 = clamp(Math.min(t, sel.s1 - MIN_LEN), 0, d);
    else sel.s1 = clamp(Math.max(t, sel.s0 + MIN_LEN), 0, d);
    clearHint();
    undo = null;
    stopPreview();
    updateUI();
  }

  /* ---------------- 试听 ---------------- */

  function stopPreview() {
    if (!preview) { if (playPos !== null) { playPos = null; paint(); } return; }
    const { src, raf } = preview;
    preview = null;
    if (raf) cancelAnimationFrame(raf);
    src.onended = null;
    try { src.stop(); } catch (err) { /* 已经自然结束 */ }
    try { src.disconnect(); } catch (err) { /* 忽略 */ }
    playPos = null;
    dom.clipPreview.classList.remove("playing");
    dom.clipPreviewLabel.textContent = "试听选区";
    paint();
  }

  async function togglePreview() {
    if (preview) { stopPreview(); return; }
    const len = sel.s1 - sel.s0;
    if (!buffer || len < MIN_LEN) return;

    const ac = getAC();
    try { if (ac.state === "suspended") await ac.resume(); } catch (err) { /* 无输出设备 */ }

    const src = ac.createBufferSource();
    src.buffer = buffer;
    src.connect(ac.destination);
    const startAt = ac.currentTime + 0.02;
    preview = { src, startAt, raf: 0 };
    playPos = sel.s0;
    dom.clipPreview.classList.add("playing");
    dom.clipPreviewLabel.textContent = "停止试听";

    src.onended = () => { if (preview && preview.src === src) stopPreview(); };
    try {
      src.start(startAt, sel.s0, len);
    } catch (err) {
      stopPreview();
      return;
    }

    const tick = () => {
      if (!preview || preview.src !== src) return;
      playPos = Math.min(sel.s0 + Math.max(0, ac.currentTime - startAt), sel.s1);
      paint();
      preview.raf = requestAnimationFrame(tick);
    };
    preview.raf = requestAnimationFrame(tick);
  }

  /* ---------------- 打开 / 关闭 / 提交 ---------------- */

  function commit() {
    if (busy || dom.clipApply.disabled) return;
    busy = true;
    dom.clipApply.disabled = true;
    dom.clipApply.textContent = "处理中…";
    const out = { s0: sel.s0, s1: sel.s1 };
    // 切片是同步的（几十秒音频约 100~200ms）。先让浏览器把「处理中」画出来，
    // 否则点下去的瞬间会像卡住。
    requestAnimationFrame(() => requestAnimationFrame(() => close(out)));
  }

  function close(result) {
    if (!dom.clipMask || dom.clipMask.hidden) return;
    stopPreview();
    // 提示的定时器必须跟着弹窗一起收，否则下次打开会被上一次的 setTimeout 清掉框
    clearTimeout(hintTimer);
    hintTimer = 0;
    hint = null;
    undo = null;
    dom.clipMask.hidden = true;
    dom.clipStage.classList.remove("dragging");
    delete dom.clipStage.dataset.drag;
    dom.clipHandleL.classList.remove("is-drag");
    dom.clipHandleR.classList.remove("is-drag");
    drag = null;
    if (ro) { ro.disconnect(); ro = null; }
    const fn = resolveOpen;
    resolveOpen = null;
    const pf = prevFocus;
    prevFocus = null;
    if (pf && typeof pf.focus === "function") {
      try { pf.focus({ preventScroll: true }); } catch (err) { /* 元素已移除 */ }
    }
    if (fn) fn(result || null);
  }

  function open(opts) {
    if (!ensure()) {
      console.warn("[ClipDialog] 弹窗 DOM 缺失，裁剪功能不可用");
      return Promise.resolve(null);
    }
    const buf = opts && opts.buffer;
    if (!buf || !buf.duration) return Promise.resolve(null);
    // 上一个还挂着（理论上不会，弹窗是模态的）：先按取消收尾，
    // 否则旧 Promise 永远不 resolve，调用方的 await 会一直悬着。
    if (resolveOpen) close(null);

    buffer = buf;
    busy = false;
    playPos = null;
    // 上一次留下的提示（含未到期的定时器）不能跨次生效
    clearTimeout(hintTimer);
    hintTimer = 0;
    hint = null;
    undo = null;
    dom.clipStage.classList.remove("dragging");
    delete dom.clipStage.dataset.drag;
    dom.clipHandleL.classList.remove("is-drag");
    dom.clipHandleR.classList.remove("is-drag");
    dom.clipStatusText.setAttribute("aria-live", "polite");
    const d = buf.duration;
    // 超过服务端上限就默认前 15 秒（不动的用户得到的结果 == 现在服务端的实际行为）
    sel = { s0: 0, s1: Math.min(SERVER_CAP, d) };

    dom.clipMeta.textContent = [
      opts.name,
      `${d.toFixed(2)} 秒`,
      `${buf.sampleRate} Hz`,
      buf.numberOfChannels > 1 ? "立体声" : "单声道",
    ].filter(Boolean).join(" · ");
    // 总时长在一次打开里不变，端点只在这里写一次（updateUI 只管选区）
    dom.clipAxisL.textContent = "0.00s";
    dom.clipAxisR.textContent = fmt(d);
    dom.clipApply.textContent = opts.okText || "设为参考音频";
    dom.clipApply.disabled = false;
    dom.clipPreviewLabel.textContent = "试听选区";
    dom.clipPreview.classList.remove("playing");

    prevFocus = document.activeElement;
    dom.clipMask.hidden = false;
    updateUI();

    // 弹窗还在 hidden 时 canvas 的 CSS 尺寸是 0，必须等布局完成再量一次
    requestAnimationFrame(() => {
      paint();
      if (ro) return;
      if ("ResizeObserver" in window) {
        ro = new ResizeObserver(() => paint());
        ro.observe(dom.clipStage);
      }
    });
    // 焦点给对话框本身：读屏会念标题，Tab 的第一站是「关闭」
    requestAnimationFrame(() => dom.clipBox.focus({ preventScroll: true }));

    return new Promise((resolve) => { resolveOpen = resolve; });
  }

  return {
    open,
    isOpen: () => !!(dom.clipMask && !dom.clipMask.hidden),
  };
})();
