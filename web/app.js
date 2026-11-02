/* IndexTTS2 Studio frontend (v2) */
"use strict";

const $ = (id) => document.getElementById(id);

/* 让一个纯 <div> 条目能被键盘操作：加 tab 序 + button 语义 + Enter/Space 激活。
   历史条目、示例条目原本只绑了 click，键盘用户根本到不了，
   而同一个产品里的预设卡片（.pm-card）早就做了 tabindex + keydown，这里补齐一致性。 */
function makeActivatable(el, label) {
  if (!el) return;
  el.tabIndex = 0;
  el.setAttribute("role", "button");
  if (label) el.setAttribute("aria-label", label);
  el.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    if (ev.target !== el) return;   // 焦点在内部的删除按钮上时交给按钮自己处理
    ev.preventDefault();
    el.click();                     // 转发给已注册的 click，不改现有逻辑
  });
}

/* ---------------- API base ----------------
   所有 JSON 接口集中在 /api/v1 版本前缀下（旧根路径后端 307 兼容重定向）。
   音频/静态资源（/audio、/assets）不带前缀。 */
const API = "/api/v1";

/* 关闭/删除图标（矢量，取自 index.html 的图标精灵）。
   以前这里是 "✕" / "×" 文本字符：字号一改就歪，深浅主题下字重还不一样。
   edit.js 在 app.js 之后加载，共用这一个常量。 */
const ICON_CLOSE = '<svg class="ico" aria-hidden="true"><use href="#i-close"></use></svg>';

/* 文本字数：软阈值 + 硬上限，与后端 server.py 同口径
   - TEXT_CHARS_SOFT 只做提示，超过照常生成
   - TEXT_CHARS_HARD 之后输入会被截断，服务端也会 400。
     不用 textarea 的 maxlength：它按 UTF-16 单元数算，一个 emoji 算 2，
     和这里（也是服务端）的码点口径对不上，会提前拦掉合法文本。 */
const TEXT_CHARS_SOFT = 2000;
const TEXT_CHARS_HARD = 10000;

/* 字数口径与服务端一致：后端用 Python 的 len()，数的是码点；JS 的 .length 数的
   是 UTF-16 码元——一个 emoji 或部分生僻字在这里算 2、在后端算 1。以前按 .length
   计数，5001 个 emoji 会显示"10002 字（超过 10000 字上限，服务端会拒绝）"，
   而服务端实际照收，用户白放弃一次生成（BUG-012）。 */
function textUnits(str) {
  return [...str].length;
}

/* localStorage 读取助手：Firefox 禁 storage / 严格隐私模式下访问 localStorage
   属性本身就抛 SecurityError，裸调用会中断整个 init。index.html 内联主题脚本
   同样有 try/catch，这里补齐。 */
function lsGet(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

/* fetch 封装：自动拼 API 前缀 + 统一错误信息提取 */
async function apiFetch(path, opts) {
  const r = await fetch(API + path, opts);
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch (e) { /* non-JSON body */ }
    const err = new Error(detail || `请求失败（${r.status}）`);
    err.status = r.status;
    throw err;
  }
  return r;
}

/* ---------------- themed modal (replaces native confirm/prompt) ----------------
   uiConfirm({title, body, okText, danger}) -> Promise<boolean>
   uiPrompt ({title, body, placeholder, value})  -> Promise<string|null>
   阻塞语义与原生一致：点遮罩/取消都算否定，Esc 等同取消。 */
const _modal = {
  mask: null, okBtn: null, input: null, resolve: null, prevFocus: null,
  timer: null, countdownEl: null,
};
function _modalSetup() {
  if (_modal.mask) return;
  _modal.mask = $("modalMask");
  _modal.okBtn = $("modalOk");
  _modal.input = $("modalInput");
  _modal.countdownEl = $("modalCountdown");
  $("modalCancel").addEventListener("click", () => _modalClose(false));
  _modal.okBtn.addEventListener("click", () => {
    if (!_modal.input.hidden && !_modal.input.value.trim()) {
      const err = $("modalError");
      err.textContent = "请输入内容"; err.hidden = false;
      _modal.input.focus();
      return;
    }
    _modalClose(true);
  });
  _modal.mask.addEventListener("click", (e) => { if (e.target === _modal.mask) _modalClose(false); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !_modal.mask.hidden) _modalClose(false);
  });
}
function _modalOpen(opts) {
  _modalSetup();
  $("modalTitle").textContent = opts.title || "";
  $("modalBody").textContent = opts.body || "";
  $("modalBody").hidden = !opts.body;
  $("modalError").hidden = true;
  _modal.input.hidden = !opts.prompt;
  _modal.input.value = opts.value || "";
  _modal.input.placeholder = opts.placeholder || "";
  _modal.okBtn.textContent = opts.okText || "确定";
  $("modalCancel").textContent = opts.cancelText || "取消";
  _modal.okBtn.classList.toggle("primary", !opts.danger);
  // danger 确认键红底白字（实底非 ghost），与删除操作的语义一致
  _modal.okBtn.classList.toggle("danger", !!opts.danger);
  clearInterval(_modal.timer);
  _modal.timer = null;
  const cd = _modal.countdownEl;
  if (cd) {
    if (opts.countdown) {
      let left = Math.max(1, Math.ceil(opts.countdown));
      const label = opts.countdownText || ((n) => `${n} 秒后自动确认`);
      cd.textContent = label(left);
      cd.hidden = false;
      _modal.timer = setInterval(() => {
        left -= 1;
        if (left <= 0) {
          clearInterval(_modal.timer);
          _modal.timer = null;
          _modalClose(true);
          return;
        }
        cd.textContent = label(left);
      }, 1000);
    } else {
      cd.hidden = true;
      cd.textContent = "";
    }
  }
  _modal.prevFocus = document.activeElement;
  _modal.mask.hidden = false;
  (_modal.input.hidden ? _modal.okBtn : _modal.input).focus();
}
function _modalClose(ok) {
  clearInterval(_modal.timer);
  _modal.timer = null;
  if (_modal.mask.hidden) return;
  _modal.mask.hidden = true;
  const res = _modal.resolve;
  _modal.resolve = null;
  if (_modal.prevFocus && _modal.prevFocus.focus) _modal.prevFocus.focus();
  if (res) res(ok && !_modal.input.hidden ? _modal.input.value : ok);
}
function uiConfirm(opts) {
  return new Promise((resolve) => {
    if (_modal.resolve) _modal.resolve(false); // 顶掉上一个未决弹窗
    _modalOpen({ ...opts, prompt: false });
    _modal.resolve = resolve;
  });
}
function uiPrompt(opts) {
  return new Promise((resolve) => {
    if (_modal.resolve) _modal.resolve(false);
    _modalOpen({ ...opts, prompt: true });
    _modal.resolve = resolve;
  });
}
_modal.input = null; // init lazily in _modalSetup

/* ---------------- theme ---------------- */
const themeBtn = $("themeBtn"), themeIcon = $("themeIcon");
const SUN = '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4m11.4-11.4 1.4-1.4"/>';
const MOON = '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>';

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  themeIcon.innerHTML = t === "dark" ? SUN : MOON;
  drawHeaderWave();
  if (spkBuffer) drawSpkWave();
  if (trim.buffer) drawTrimWave();
}

/* ---------------- workspace draft (survives a page refresh) ---------------- */
const DRAFT_KEY = "studioDraft.v1";
const DB_NAME = "index-tts-studio";
const DB_VERSION = 1;
const DRAFT_STORE = "draft";
const DRAFT_AUDIO_KEYS = { spk: "spk_audio", emo: "emo_audio" };

// crypto.randomUUID 只在安全上下文存在（localhost 是，http://192.168.x.x 不是），
// sessionStorage 也可能被浏览器策略禁用。任何一条抛出都会中断整段脚本 → 页面全空白。
function safeClientId() {
  const make = () => {
    const c = window.crypto;
    if (c && typeof c.randomUUID === "function") return c.randomUUID();
    if (c && typeof c.getRandomValues === "function") {
      const b = new Uint8Array(16);
      c.getRandomValues(b);
      return [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
    }
    return "c" + Date.now().toString(36) + Math.random().toString(16).slice(2, 10);
  };
  try {
    const id = sessionStorage.getItem("studioClientId");
    if (id) return id;
    const fresh = make();
    sessionStorage.setItem("studioClientId", fresh);
    return fresh;
  } catch (e) {
    return make();
  }
}
const CLIENT_ID = safeClientId();

function openDraftDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(DRAFT_STORE)) db.createObjectStore(DRAFT_STORE);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function idbRequest(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function setDraftBlob(key, blob) {
  return openDraftDb().then((db) => {
    const tx = db.transaction(DRAFT_STORE, "readwrite");
    const store = tx.objectStore(DRAFT_STORE);
    if (blob == null) store.delete(key);
    else store.put(blob, key);
    return new Promise((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onabort = tx.onerror = () => reject(tx.error);
    });
  });
}

function getDraftBlob(key) {
  return openDraftDb().then((db) =>
    idbRequest(db.transaction(DRAFT_STORE).objectStore(DRAFT_STORE).get(key))
  );
}

function assignFile(inputEl, file) {
  const dt = new DataTransfer();
  dt.items.add(file);
  inputEl.files = dt.files;
}

function readDraft() {
  try {
    const value = JSON.parse(localStorage.getItem(DRAFT_KEY) || "null");
    return value && typeof value === "object" ? value : null;
  } catch {
    return null;
  }
}

function saveDraft() {
  const vec = Array.from(vecInputs(), (s) => Number(s.value));
  const draft = {
    version: 1,
    text: $("text").value,
    curPresetName,
    libTab: curLib,
    emoMode: $("emoMode").value,
    emoWeight: $("emoWeight").value,
    emoText: $("emoText").value,
    emoRandom: $("emoRandom").checked,
    doSample: $("doSample").checked,
    topP: $("topP").value,
    topK: $("topK").value,
    temperature: $("temperature").value,
    lenPen: $("lenPen").value,
    numBeams: $("numBeams").value,
    repPen: $("repPen").value,
    maxMel: $("maxMel").value,
    segTokens: $("segTokens").value,
    gapMs: $("gapMs").value,
    emoVector: vec,
    savedAt: Date.now(),
  };
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
  } catch (e) { /* storage may be full; the active session remains usable */ }
}

/* 界面同步：不管有没有本地草稿都要跑一遍。
   首屏文本域本身就预填了一段文字，如果跟着草稿一起跳过，计数器会停在 HTML 里的
   初始值「0 字」，而估算区已经按真实文本算出「共 1 段 · 40 字」——同一屏两个数。 */
function syncDraftUi() {
  $("charCount").textContent = `${textUnits($("text").value.trim())} 字`;
  syncParams();
  emoVisible();
  updateGenSummary();
  scheduleSegments();
  enforceParamLock();
}

function applyDraft(draft) {
  if (draft) {
    if (typeof draft.text === "string") $("text").value = draft.text;
    if (typeof draft.curPresetName === "string") curPresetName = draft.curPresetName;
    if (draft.emoMode != null) $("emoMode").value = String(draft.emoMode);
    if (draft.emoWeight != null) $("emoWeight").value = draft.emoWeight;
    if (typeof draft.emoText === "string") $("emoText").value = draft.emoText;
    if (draft.emoRandom != null) $("emoRandom").checked = !!draft.emoRandom;
    if (draft.doSample != null) $("doSample").checked = !!draft.doSample;
    if (draft.topP != null) $("topP").value = draft.topP;
    if (draft.topK != null) $("topK").value = draft.topK;
    if (draft.temperature != null) $("temperature").value = draft.temperature;
    if (draft.lenPen != null) $("lenPen").value = draft.lenPen;
    if (draft.numBeams != null) $("numBeams").value = draft.numBeams;
    if (draft.repPen != null) $("repPen").value = draft.repPen;
    if (draft.maxMel != null) $("maxMel").value = draft.maxMel;
    if (draft.segTokens != null) $("segTokens").value = draft.segTokens;
    if (draft.gapMs != null) $("gapMs").value = draft.gapMs;
    if (Array.isArray(draft.emoVector)) setVecInputs(draft.emoVector);
  }
  syncDraftUi();
}

function draftElapsed(job) {
  const startMs = (job.started_at || job.created_at || 0) * 1000;
  return Math.max(0, (Date.now() - startMs) / 1000);
}

function applyJobSnapshot(job) {
  const isNew = job.job_id !== curJobId;
  curJobId = job.job_id || "";
  curJobState = job.state === "stop_requested" ? "stopping"
    : job.state === "stopping" ? "stopping"
    : job.state === "queued" ? "queued"
    : "running";
  genBtn.disabled = true;
  if (isNew) resetSteps();
  genStart = performance.now() - draftElapsed(job) * 1000;
  errBox.classList.remove("on");
  showLoading(job.state === "stopping"
    ? "正在停止生成…"
    : job.state === "queued" ? "排队中…" : transDesc(job.stage_desc) || "正在生成语音…");
  $("pbarFill").style.width = `${Math.round((job.progress || 0) * 100)}%`;
  $("pStageMeta").textContent = job.state === "queued" ? "排队中" : `${Math.round((job.progress || 0) * 100)}%`;
  $("pElapsed").textContent = `${draftElapsed(job).toFixed(1)}s`;
  startElapsed();
  updateCancelUi();
}

let jobReconnectTimer = null;

async function restoreJob() {
  try {
    const r = await fetch(`${API}/jobs/current?client_id=${encodeURIComponent(CLIENT_ID)}`);
    const j = await r.json().catch(() => ({}));
    const job = j.job;
    if (!job) return;
    if (["queued", "running", "stop_requested", "stopping"].includes(job.state)) {
      applyJobSnapshot(job);
      clearInterval(jobReconnectTimer);
      // 防重入：单次 /jobs/{id} 卡在慢响应上时，500ms 的间隔会一口气叠出
      // 七八个并发请求，把服务端线程池占满、连带 /status 的仪表也变卡。
      // 标志放在定时器闭包里，重建轮询时自然重置。
      let reconnectBusy = false;
      jobReconnectTimer = setInterval(async () => {
        if (reconnectBusy) return;
        reconnectBusy = true;
        try {
          const r2 = await fetch(`${API}/jobs/${encodeURIComponent(curJobId)}`);
          if (!r2.ok) throw new Error("任务状态不可用");
          const j2 = await r2.json();
          const s = j2.job;
          if (!s) throw new Error("任务状态不可用");
          // 终态必须列全：少了 "stopped" 会让「停止生成后刷新页面」的轮询
          // 永远走 else 分支，界面卡在「正在停止生成…」直到 job 被服务端清掉。
          if (["done", "error", "cancelled", "stopped"].includes(s.state)) {
            clearInterval(jobReconnectTimer);
            jobReconnectTimer = null;
            if (s.state === "done" && s.result) {
              histCurId = s.result.history_id || "";
              // 恢复路径与 SSE 路径对齐:先释放按钮/计时器/波形动画,再展示结果
              stopUi();
              await restoreCandidates(s.result);
              await loadResult(s.result.wav, s.result.elapsed, s.result.file, { melCapped: s.result.mel_capped, normalizeFallback: s.result.normalize_fallback });
              await refreshHistory();
            } else if (s.state === "stopped") {
              // 已经跑起来、中途被用户停掉的：中性提示
              showStopped(s.message || "已停止生成");
            } else if (s.state === "cancelled") {
              // 排队中还没开始就被取消的：确实什么都没做
              showError("已取消（排队中的任务未开始生成）");
            } else {
              showError(s.error || "生成失败");
            }
            pollStatus();
          } else {
            applyJobSnapshot(s);
          }
        } catch (e) {
          clearInterval(jobReconnectTimer);
          jobReconnectTimer = null;
          showError("任务状态恢复失败，请稍后在生成历史中查看");
        } finally {
          reconnectBusy = false;
        }
      }, 500);
    } else if (job.state === "done" && job.result) {
      histCurId = job.result.history_id || "";
      // 与 SSE done 路径一致:先释放按钮/计时器,再展示已恢复的结果
      stopUi();
      await restoreCandidates(job.result);
      await loadResult(job.result.wav, job.result.elapsed, job.result.file, { melCapped: job.result.mel_capped, normalizeFallback: job.result.normalize_fallback });
      await refreshHistory();
    } else if (job.state === "error") {
      showError(job.error || "生成失败");
    } else if (job.state === "stopped") {
      // 已经跑起来、中途被用户停掉的：中性提示
      showStopped(job.message || "已停止生成");
    } else if (job.state === "cancelled") {
      showError("已取消（排队中的任务未开始生成）");
    }
  } catch (e) { /* server restart means there is nothing to reconnect */ }
}

async function restoreDraft() {
  applyDraft(readDraft());
  try {
    const [spkBlob, emoBlob] = await Promise.all([
      getDraftBlob(DRAFT_AUDIO_KEYS.spk).catch(() => null),
      getDraftBlob(DRAFT_AUDIO_KEYS.emo).catch(() => null),
    ]);
    if (spkBlob) {
      const file = new File([spkBlob], spkBlob.name || "reference.wav", { type: spkBlob.type || "audio/wav" });
      assignFile($("spkFile"), file);
      setSpkFile(file, false);
    }
    if (emoBlob) {
      const file = new File([emoBlob], emoBlob.name || "emotion.wav", { type: emoBlob.type || "audio/wav" });
      assignFile($("emoFile"), file);
      setEmoFile(file, false);
    }
  } catch (e) { /* audio restoration is best-effort */ }
}
themeBtn.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  // 先切主题再写偏好：setItem 在隐私模式/配额满时会抛异常，
  // 写在 applyTheme 之前会让按钮点了完全没反应。
  applyTheme(next);
  try { localStorage.setItem("theme", next); } catch (e) { /* 存不下就只在本次会话生效 */ }
});

function sizeCanvas(cv) {
  const r = cv.getBoundingClientRect();
  const d = window.devicePixelRatio || 1;
  const bw = Math.round(r.width * d), bh = Math.round(r.height * d);
  if (cv.width !== bw || cv.height !== bh) { cv.width = bw; cv.height = bh; }
  // scale the ctx so CSS-pixel drawing coords fill the HiDPI backing store;
  // canvas resize resets transforms, so reapply every time
  cv.getContext("2d").setTransform(bw / r.width || 1, 0, 0, bh / r.height || 1, 0, 0);
  return { w: r.width, h: r.height };
}

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

/* ---------------- header waveform (signature) ----------------
   顶栏这条是签名元素，保持 2026-10-28 之前的画法：
   4px 步进、2.4px 宽的竖条，居中对称。闲置时几乎是一条极淡的横线
   （0.06~0.11 振幅、50% 透明度），只有生成时才起包络。
   生成态：sin 半周做整体拱形 + 逐条抖动，已生成的那一段换成品牌红，
   相当于一条「声音正在被写出来」的进度带。 */
const hWave = $("waveHeader"), hCtx = hWave.getContext("2d");
let genAnim = { on: false, level: 0, progress: 0 };
let waveT = 0;

function drawHeaderWave(t) {
  t = t || waveT || 0;
  const { w, h } = sizeCanvas(hWave);
  hCtx.clearRect(0, 0, w, h);
  const accent = cssVar("--accent"), primary = cssVar("--primary");
  const n = Math.floor(w / 4);
  const mid = h / 2;
  for (let i = 0; i < n; i++) {
    let a;
    if (genAnim.on) {
      const env = Math.sin(i / n * Math.PI);
      a = env * (0.25 + 0.75 * Math.abs(Math.sin(i * 0.7 + t / 130))) * genAnim.level;
    } else {
      a = 0.06 + 0.05 * Math.sin(i * 0.5 + t / 900);
    }
    const bh = Math.max(2, a * h);
    hCtx.fillStyle = genAnim.on && i / n < genAnim.progress ? primary : accent;
    hCtx.globalAlpha = genAnim.on ? 0.95 : 0.5;
    hCtx.fillRect(i * 4, mid - bh / 2, 2.4, bh);
  }
  hCtx.globalAlpha = 1;
}
/* 呼吸：4 次/秒重画一次足够，肉眼读到的是「缓慢漂移」，不是动画。
   系统开了减少动态效果就完全停掉，只留一张静态横条。 */
if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
  // 按真实时间取相位：waveT 只在生成期的 RAF 里更新，闲置时用它会每 250ms
  // 重画同一帧（画面静止）。生成中让位给 RAF，避免两个时钟互相拉扯。
  setInterval(() => { if (!document.hidden && !genAnim.on) drawHeaderWave(performance.now()); }, 250);
}
let waveRAF = 0;
function waveLoop(t) {
  waveT = t;
  if (genAnim.on && !document.hidden) { drawHeaderWave(t); waveRAF = requestAnimationFrame(waveLoop); }
  else { waveRAF = 0; if (!document.hidden) drawHeaderWave(0); }
}
/* 后台标签页暂停：合并轮询会触发 nvidia-smi 子进程，闲置时不空转。
   生成进行中则不停 —— SSE 是推送流不受影响，但回前台要立即刷新一次。 */
let metricsTimer = null;
function setMetricsPolling(pollFn) {
  const on = !!pollFn;
  if (on && metricsTimer == null) {
    metricsTimer = setInterval(pollFn, 2000);
    pollFn();
  } else if (!on && metricsTimer != null) {
    clearInterval(metricsTimer);
    metricsTimer = null;
  }
}
document.addEventListener("visibilitychange", () => {
  setMetricsPolling(document.hidden ? null : pollStatus);
});

/* ---------------- system gauges (device / cpu / mem / gpu / vram) ---------------- */
function setMeter(id, pct, label) {
  const el = $(id);
  if (!el) return;
  const b = el.querySelector("b");
  if (label !== undefined && b) b.textContent = label;
  const bar = el.querySelector(".bar i");
  if (bar) bar.style.width = `${Math.min(pct ?? 0, 100)}%`;
  el.classList.toggle("hot", (pct ?? 0) > 85);
}
/* ---------------- model status + system gauges: merged polling ---------------- */
/* 轮询合并：原先 /model 与 /metrics 各自每 2s 一次（多标签页请求数翻倍），
   现在一个 /status 往返同时驱动模型徽标与四块仪表。document.hidden 时暂停。 */
let modelReady = false;
function renderModelPill(s) {
  modelReady = !!s.loaded;
  const phase = s.phase || (s.loaded ? "ready" : "unloaded");
  $("modelPill").dataset.s = phase === "ready" ? "ready" : phase === "unloaded" ? "unloaded" : "loading";
  $("modelPillText").textContent = {
    ready: "模型已加载",
    loading: "模型加载中…",
    reloading: "模型重载中…",
    unloading: "模型卸载中…",
    error: "模型状态异常",
    unloaded: "模型未加载",
  }[phase] || "模型未加载";
}
function renderMetrics(m) {
  /* 设备名只留 "RTX 3060 Ti"：四个仪表全显示后 header 预算很紧，完整型号
     "GeForce RTX 3060 Ti" 要多吃 50px，1180 宽时主题按钮会被挤到第三行。
     完整名字放进 title，鼠标悬停仍能看到。 */
  const devPill = $("devPill");
  if (m.gpu_name) {
    $("devText").textContent = m.gpu_name.replace(/^NVIDIA\s+/i, "").replace(/^GeForce\s+/i, "");
    devPill.title = "运行设备：" + m.gpu_name;
  } else {
    /* 读不到就写 "--"。以前这里固定写 "CPU"，会让「有 N 卡但监控没读到」的
       机器看起来像在跑 CPU 推理，属于谎报。 */
    $("devText").textContent = "--";
    devPill.title = "未读到 NVIDIA GPU 信息（驱动过旧 / nvidia-smi 不在 PATH / 本机无 N 卡）";
  }
  if (m.cpu_percent !== undefined) setMeter("mCpu", m.cpu_percent, `${Math.round(m.cpu_percent)}%`);
  if (m.mem_total_gb) setMeter("mMem", (m.mem_used_gb / m.mem_total_gb) * 100, `${m.mem_used_gb.toFixed(1)}/${m.mem_total_gb.toFixed(1)}G`);
  /* GPU 与显存分开判断：后端现在把两者解耦，可能只给得出其中一项 */
  const hasGpu = m.gpu_percent !== undefined;
  const hasVram = m.vram_used_mb !== undefined || m.vram_total_mb !== undefined;
  $("mGpu").hidden = !hasGpu;
  $("mVram").hidden = !hasVram;
  if (hasGpu) setMeter("mGpu", m.gpu_percent, `${m.gpu_percent}%`);
  if (hasVram) {
    $("mVram").title = "";
    const totalMb = m.vram_total_mb;
    const usedMb = m.vram_used_mb;
    if (usedMb !== undefined && totalMb) {
      setMeter("mVram", (usedMb / totalMb) * 100, `${(usedMb / 1024).toFixed(1)}/${(totalMb / 1024).toFixed(1)}G`);
    } else if (usedMb !== undefined) {
      /* 总量没读到就别拿 8192 当分母硬算比例，只老实报已用量 */
      setMeter("mVram", 0, `${(usedMb / 1024).toFixed(1)}G`);
      $("mVram").title = "总显存未读到，只显示已用量";
    } else {
      setMeter("mVram", 0, `--/${(totalMb / 1024).toFixed(1)}G`);
      $("mVram").title = "显存占用量未读到";
    }
  }
  syncModelMetrics(m);
}

/* 模型页只把「显存」放大成一张卡：8G 卡上「显存还剩多少」比任何文案都管用，
   且下面的精度开关会直接改变它；GPU/内存/CPU 顶栏已常驻，不再重复。
   数据同源于 header 的 /status 轮询，不新增请求。 */
function mmMetric(id, pct, label, sub) {
  const el = $(id);
  if (!el) return;
  const b = el.querySelector("b");
  if (label !== undefined && b) b.textContent = label;
  const bar = el.querySelector(".bar i");
  if (bar) bar.style.width = `${Math.min(pct ?? 0, 100)}%`;
  // 8G 卡上 80% 就只剩 1.5G 余量，比 header 的 85% 更早报警
  el.classList.toggle("hot", (pct ?? 0) > 80);
  const em = el.querySelector("em");
  if (em && sub !== undefined) em.textContent = sub;
}
function syncModelMetrics(m) {
  /* 只更新「显存」卡：CPU/GPU/内存顶栏已常驻，这页保留显存是因为精度开关会直接改变它。 */
  if (m.vram_total_mb && m.vram_used_mb !== undefined) {
    const total = m.vram_total_mb / 1024;
    const used = (m.vram_used_mb || 0) / 1024;
    mmMetric("mmVram", (used / total) * 100, `${used.toFixed(1)}/${total.toFixed(1)}G`, `余量 ${(total - used).toFixed(1)}G`);
  }
}
/* 防重入：/status 在 nvidia-smi 走兜底通道时单次可达 4s（server.py 里
   timeout=4），而这里是 2s 一次——不拦就会叠出 2~3 个并发请求，多标签页
   再翻几倍，全部堵在服务端线程池里排队，界面反而更慢。计时器到点只管
   跳过本轮，不再堆请求。 */
let statusInflight = false;
async function pollStatus() {
  if (statusInflight) return;
  statusInflight = true;
  try {
    const s = await (await apiFetch("/status")).json();
    applyAudioCapability(s);
    renderModelPill(s);
    renderMetrics(s);
    // 模型页状态卡与 Header 徽标同源同频：外部（另一标签页 / CLI / 直接调 API）
    // 改了模型状态，这一屏 2 秒内跟着变，不再一处新一处旧（BUG-015）。
    // 有未保存改动时 renderModelPage 只刷状态区，不覆盖控件。
    renderModelPage(s);
  } catch (e) {
    $("modelPill").dataset.s = "unloaded";
    // 服务端 5xx 会返回结构化错误：说「连接中断」是假的，按状态码区分。
    $("modelPillText").textContent = e && e.status ? `服务异常（${e.status}）` : "服务连接中断…";
  } finally {
    statusInflight = false;
  }
}
setMetricsPolling(pollStatus);

/* ---------------- speaker reference audio ---------------- */
const spkDrop = $("spkDrop"), spkFile = $("spkFile");
const spkWave = $("spkWave"), spkCtx = spkWave.getContext("2d");
const spkAudio = new Audio();
const spkPlay = $("spkPlay"), spkPlayIcon = $("spkPlayIcon");
let spkBuffer = null, spkPlaying = false, spkSeekFrac = 0;
let spkPlayPending = false;   // play() 的 await 悬挂期再入闸，防两条并行 spkTick RAF
let spkUrl = null;

/* 用户自己换音频（拖入 / 选择文件）时调用：当前参数组合已经不等于预设内容了，
   标签若继续显示「预设: 胡桃」，会让人以为生成还在用预设那份参考音频。
   注意只清标签，不重置采样参数——常用的玩法正是「沿用预设参数、只换一个音色」。 */
function dropPresetSelection() {
  if (!curPresetName) return;
  curPresetName = "";
  updateGenSummary();
  if (curLib === "presets") renderLib();   // 资源库里那张卡的选中态同步取消
  scheduleDraftSave();
}
/* 视频也能选进同一个入口：先提取音轨，再把 WAV 交给原来的选文件 / 裁剪流程。
   mediaPickSeq 是「当前选择意图」的版本号：新选文件、点更换音频、清空、加载
   预设都会 +1，晚到的视频提取结果对不上版本号就静默丢弃。 */
const mediaPickSeq = { spk: 0, emo: 0 };
// 最近一次真正生效的文件。新选的文件被拒绝（体积/格式/解码失败）时把
// <input> 退回它——否则界面显示的是上一份，提交却带上刚选的坏文件。
const lastGoodFile = { spk: null, emo: null };

function restoreInputFile(inputEl, file) {
  if (!file) { inputEl.value = ""; return; }
  if (inputEl.files[0] !== file) assignFile(inputEl, file);
}

async function userPickedSpk(f) {
  dropPresetSelection();
  if (!isVideoFile(f)) { ++mediaPickSeq.spk; setSpkFile(f); return; }
  const err = validateMediaFile(f, "音色参考音频");
  if (err) {
    ++mediaPickSeq.spk;
    restoreInputFile(spkFile, lastGoodFile.spk);
    showSpkWarn(err);
    return;
  }
  // 先清掉旧选择：提取期间点生成不该默默用上一份音频。
  // 但提取失败（没有音轨/文件损坏）时要把原选择还回去，不能因为试了一次视频
  // 就把用户原来的音色弄丢。
  const prev = lastGoodFile.spk;
  resetSpkSelection();
  $("spkSub").textContent = "正在从视频提取音轨…";
  const seq = ++mediaPickSeq.spk;
  const wav = await extractVideoAudio(f, seq, "spk");
  if (seq !== mediaPickSeq.spk) return;
  if (!wav) {
    const msg = $("spkSub").textContent;
    if (prev) setSpkFile(prev, true); else resetSpkSelection();
    showSpkWarn(msg);
    return;
  }
  setSpkFile(wav);
}
async function userPickedEmo(f) {
  dropPresetSelection();
  if (!isVideoFile(f)) { ++mediaPickSeq.emo; setEmoFile(f); return; }
  const err = validateMediaFile(f, "情感参考音频");
  if (err) {
    ++mediaPickSeq.emo;
    restoreInputFile(emoFile, lastGoodFile.emo);
    $("emoSub").textContent = err;
    return;
  }
  const prev = lastGoodFile.emo;
  clearEmoFile();
  $("emoSub").textContent = "正在从视频提取音轨…";
  const seq = ++mediaPickSeq.emo;
  const wav = await extractVideoAudio(f, seq, "emo");
  if (seq !== mediaPickSeq.emo) return;
  if (!wav) {
    const msg = $("emoSub").textContent;
    if (prev) setEmoFile(prev, true, true);   // 失败也保留原选择
    $("emoSub").textContent = msg;
    return;
  }
  setEmoFile(wav);
}

spkDrop.addEventListener("dragover", (e) => { e.preventDefault(); spkDrop.classList.add("over"); });
spkDrop.addEventListener("dragleave", () => spkDrop.classList.remove("over"));
spkDrop.addEventListener("drop", (e) => { e.preventDefault(); spkDrop.classList.remove("over"); if (e.dataTransfer.files[0]) userPickedSpk(e.dataTransfer.files[0]); });
spkFile.addEventListener("change", () => spkFile.files[0] && userPickedSpk(spkFile.files[0]));
let draftSaveTimer = null;
function scheduleDraftSave() {
  clearTimeout(draftSaveTimer);
  draftSaveTimer = setTimeout(saveDraft, 400);
}
const spkPlayerBox = $("spkPlayerBox");
spkPlayerBox.addEventListener("dragover", (e) => { e.preventDefault(); spkPlayerBox.classList.add("over"); });
spkPlayerBox.addEventListener("dragleave", () => spkPlayerBox.classList.remove("over"));
spkPlayerBox.addEventListener("drop", (e) => {
  e.preventDefault();
  spkPlayerBox.classList.remove("over");
  if (e.dataTransfer.files[0]) userPickedSpk(e.dataTransfer.files[0]);
});
$("spkChange").addEventListener("click", () => {
  resetSpkSelection();
  spkFile.click();
});
$("spkClip").addEventListener("click", openSpkClip);

/* emo reference audio shares the same pattern */
const emoDrop = $("emoDrop"), emoFile = $("emoFile");
// 参考音频“当前选择”的版本号：预设音频是异步 fetch 的，下载期间用户
// 可能已经自己拖了新文件进来，晚到的预设音频不能再覆盖用户的选择。
const refSelSeq = { spk: 0, emo: 0 };
emoDrop.addEventListener("dragover", (e) => { e.preventDefault(); emoDrop.classList.add("over"); });
emoDrop.addEventListener("dragleave", () => emoDrop.classList.remove("over"));
emoDrop.addEventListener("drop", (e) => { e.preventDefault(); emoDrop.classList.remove("over"); if (e.dataTransfer.files[0]) userPickedEmo(e.dataTransfer.files[0]); });
emoFile.addEventListener("change", () => emoFile.files[0] && userPickedEmo(emoFile.files[0]));
function setEmoFile(f, persist = true, quiet = false) {
  const err = validateAudioFile(f, "情感参考音频");
  if (err) {
    restoreInputFile(emoFile, lastGoodFile.emo);
    $("emoSub").textContent = err;
    return;
  }
  clearPrecheckError("情感参考音频模式下请先选择情感音频");
  if (!quiet) $("emoSub").textContent = audioHintText("emo");
  refSelSeq.emo++;
  // 拖拽进来 / 预设 fetched 的 Blob 不会自动写进 <input type=file>，
  // 而提交时读的是 input.files[0]。这里必须同步，否则界面换了音频、
  // 生成却仍然用 input 里的旧文件（预设自带的那份）。
  if (emoFile.files[0] !== f) assignFile(emoFile, f);
  lastGoodFile.emo = f;
  $("emoName").textContent = f.name;
  if (persist) setDraftBlob(DRAFT_AUDIO_KEYS.emo, f).catch(() => {});
}
function clearEmoFile() {
  // 作废进行中的视频提取 / 预设音频下载，晚到的结果不能再装回来
  ++mediaPickSeq.emo;
  ++refSelSeq.emo;
  lastGoodFile.emo = null;
  emoFile.value = "";
  $("emoName").textContent = "点击选择情感参考音频";
  setDraftBlob(DRAFT_AUDIO_KEYS.emo, null).catch(() => {});
}

/* 参考音频声纹：2px 柱 + 2px 空隙的对称柱状条。
   为什么不做 1px 连续列：这块画布只有 64px 高，1px 列在 chrome 尺度上读成
   「一条毛边」，看不出是柱阵；2px 柱正好在「柱子还在」和「细密纹理」之间。
   定位精度不受影响 —— seek 用的是画布内的连续比例，跟柱宽无关。 */
const SPK_BAR = { pitch: 4, w: 2, r: 1, amp: .78, floor: 2 };

function drawSpkWave() {
  const { w, h } = sizeCanvas(spkWave);
  const data = spkBuffer.getChannelData(0);
  const B = SPK_BAR;
  const mid = h / 2;
  const ink = cssVar("--accent"), played = cssVar("--primary"), border = cssVar("--border");
  spkCtx.clearRect(0, 0, w, h);
  // 零轴：柱状条中间那根淡线，帮眼睛锚住基线
  spkCtx.fillStyle = border;
  spkCtx.fillRect(0, mid - 0.5, w, 1);

  // 柱数由宽度决定，整组居中，避免右边留一截空白
  const n = Math.max(1, Math.floor(w / B.pitch));
  const x0 = Math.round((w - n * B.pitch + B.w) / 2);
  // 每根柱负责一段样本，取该段的峰谷绝对值做高度（上下对称）
  const step = Math.max(1, Math.floor(data.length / n));
  const pos = spkPlaying || spkAudio.currentTime > 0 ? spkSeekFrac * w : 0;
  // 未播部分退到背景纹理层，红色成为唯一的进度信号。
  // 暗色下 --accent 是浅蓝，同样的透明度会偏亮、跟红色抢，所以再压一档。
  const quiet = document.documentElement.dataset.theme === "dark" ? 0.38 : 0.42;

  for (let i = 0; i < n; i++) {
    const base = i * step;
    let peak = 0;
    for (let k = 0; k < step; k++) {
      const v = data[base + k] || 0;
      const a = v < 0 ? -v : v;
      if (a > peak) peak = a;
    }
    // 静音段留 2px 地板，否则整条断成一串点
    const bh = Math.max(B.floor, peak * mid * B.amp);
    const x = x0 + i * B.pitch;
    const isPlayed = x <= pos;
    spkCtx.fillStyle = isPlayed ? played : ink;
    spkCtx.globalAlpha = isPlayed ? 0.95 : quiet;
    if (spkCtx.roundRect) {
      spkCtx.beginPath();
      spkCtx.roundRect(x, mid - bh, B.w, bh * 2, Math.min(B.r, bh));
      spkCtx.fill();
    } else {
      spkCtx.fillRect(x, mid - bh, B.w, bh * 2);
    }
  }
  spkCtx.globalAlpha = 1;
}
function spkFmt(t) { return `${t.toFixed(2)}s`; }
function spkTick() {
  if (!spkBuffer) return;
  const d = spkBuffer.duration;
  // 零长度音频（d=0）会让 currentTime/0 变成 NaN 或 Infinity，
  // 时间标签显示 NaN，波形的 x <= pos 判定也全为 false。
  if (spkPlaying && d > 0) spkSeekFrac = Math.min(spkAudio.currentTime / d, 1);
  $("spkTime").textContent = `${spkFmt(spkPlaying ? spkAudio.currentTime : spkSeekFrac * d)} / ${spkFmt(d)}`;
  drawSpkWave();
  if (spkPlaying) requestAnimationFrame(spkTick);
}
spkAudio.addEventListener("ended", spkStop);
function spkStop() {
  spkAudio.pause();
  spkPlaying = false;
  spkAudio.currentTime = 0;
  spkSeekFrac = 0;
  spkPlayIcon.innerHTML = PLAY_SVG;
  spkTick();
}
const PLAY_SVG = '<path d="M8 5v14l11-7z"/>';
const PAUSE_SVG = '<path d="M6 5h4v14H6zM14 5h4v14h-4z"/>';
spkPlay.addEventListener("click", async () => {
  if (!spkBuffer) return;
  if (spkPlaying) {
    spkAudio.pause();
    spkPlaying = false;
    spkPlayIcon.innerHTML = PLAY_SVG;
    spkTick();
  } else {
    if (spkPlayPending) return;
    spkPlayPending = true;
    spkAudio.currentTime = spkSeekFrac * spkBuffer.duration;
    // 播放成功才切「播放中」状态；失败（无输出设备、快速点暂停的 AbortError）
    // 不能让图标停在暂停态而实际没声音。
    try {
      await spkAudio.play();
      spkPlaying = true;
      spkPlayIcon.innerHTML = PAUSE_SVG;
      requestAnimationFrame(spkTick);
    } catch (e) {
      spkPlaying = false;
      spkPlayIcon.innerHTML = PLAY_SVG;
    } finally {
      spkPlayPending = false;
    }
  }
});
spkWave.addEventListener("pointerdown", (e) => {
  if (!spkBuffer) return;
  const r = spkWave.getBoundingClientRect();
  spkSeekFrac = Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1);
  spkAudio.currentTime = spkSeekFrac * spkBuffer.duration;
  spkTick();
});

const AC = window.AudioContext || window.webkitAudioContext;
let sharedAC = null; // browsers cap AudioContext instances — reuse one
function getAC() {
  if (!sharedAC || sharedAC.state === "closed") sharedAC = new AC();
  return sharedAC;
}

/* ---------------- 解码 ----------------
   decodeAudioData 会把音频重采样到 AudioContext 的采样率（设备通常 48000）。
   我们的产物是 22050Hz，22050→48000 的插值会过冲 0.3~0.5dB：一份峰值正好压在
   0dBFS（没削波）的文件，解出来变成 +0.3dBFS，于是峰值提示每次都误报削波、
   还建议用户降 0.5dB；导出的剪辑结果也被无谓升采样成 48000（体积 ×2.2，音质不变）。
   所以先读 wav 头拿文件自己的采样率，用对应采样率的 OfflineAudioContext 解码，
   整条链路上不产生重采样。非 wav（如用户上传的 mp3）读不出表头，退回设备采样率。 */

/** 读 RIFF/WAVE 头里的采样率；读不出（非 wav）返回 0 */
function wavSampleRate(ab) {
  try {
    if (ab.byteLength < 44) return 0;
    const dv = new DataView(ab);
    const tag = (o) => String.fromCharCode(dv.getUint8(o), dv.getUint8(o + 1), dv.getUint8(o + 2), dv.getUint8(o + 3));
    if (tag(0) !== "RIFF" || tag(8) !== "WAVE") return 0;
    // 不能假设 fmt 块固定在 12：有的文件前面还有 LIST/JUNK 块
    for (let o = 12; o + 8 <= dv.byteLength;) {
      const id = tag(o), size = dv.getUint32(o + 4, true);
      if (id === "fmt ") {
        const rate = dv.getUint32(o + 12, true);
        return rate >= 8000 && rate <= 384000 ? rate : 0;
      }
      o += 8 + size + (size % 2);   // 块按偶数字节对齐
    }
  } catch (e) { /* 表头损坏，交给设备采样率兜底 */ }
  return 0;
}

const offlineByRate = new Map();   // 每个采样率复用同一个 OfflineAudioContext
const OAC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
/** 按文件自身采样率解码，避免重采样过冲 */
function decodeNative(ab) {
  const rate = OAC ? wavSampleRate(ab) : 0;
  if (!rate) return getAC().decodeAudioData(ab);
  let ctx = offlineByRate.get(rate);
  if (!ctx) {
    ctx = new OAC(1, 1, rate);     // 只用它解码，长度无所谓
    offlineByRate.set(rate, ctx);
  }
  return ctx.decodeAudioData(ab);
}

/* 超长提示放在 #spkWarn（波形下方常驻），不是 #spkSub：
   后者是 #spkDrop 的子元素，上传后父级 hidden，提示根本看不见。 */
function showSpkWarn(text) {
  const box = $("spkWarn");
  if (!box) return;
  // 提示节点在 #spkPlayerBox 里：没有可用波形时（从未选成功/已清空）父级隐藏，
  // 兜底写回拖拽区，否则「上传被拒」又变成静默失败。
  if ($("spkPlayerBox").hidden) {
    $("spkWarnText").textContent = "";
    box.hidden = true;
    $("spkSub").textContent = text || audioHintText("spk");
    return;
  }
  $("spkWarnText").textContent = text || "";
  box.hidden = !text;
}

function resetSpkSelection() {
  // 作废进行中的视频提取 / 预设音频下载：否则「更换音频」清空之后，
  // 晚到的结果会把旧文件又装回来（实测 pauses 一次提取就能复现）。
  ++mediaPickSeq.spk;
  ++refSelSeq.spk;
  lastGoodFile.spk = null;
  spkAudio.pause();
  // 释放 blob URL 并清 src：否则旧音频的 blob 一直滞留到下次选文件或关页面
  if (spkUrl) { URL.revokeObjectURL(spkUrl); spkUrl = null; }
  spkAudio.removeAttribute("src");
  spkAudio.load();
  spkPlaying = false;
  spkSeekFrac = 0;
  spkBuffer = null;
  // input 自身也要清空：只清草稿和界面的话，用户在“更换音频”里点取消，
  // 音色参考音频区域显示为空，生成却仍会默默用上一次的文件。
  spkFile.value = "";
  // 草稿里的音频 blob 一并清掉:否则"更换音频"后取消文件选择,
  // 刷新页面会把旧音频从 IndexedDB 恢复回来
  setDraftBlob(DRAFT_AUDIO_KEYS.spk, null).catch(() => {});
  $("spkPlayerBox").hidden = true;
  $("spkDrop").hidden = false;
  $("spkName").textContent = defaultSpkName();
  $("spkSub").textContent = audioHintText("spk");
  $("spkFileName").textContent = "";
  $("spkFileName").title = "";
  showSpkWarn("");
}

function setSpkFile(f, persist = true) {
  // 拖拽/预设/草稿恢复都会走到这里：格式不支持的在入口就拦下，
  // 不让它进 input.files 等到点生成才报错。
  const err = validateAudioFile(f, "音色参考音频");
  if (err) {
    restoreInputFile(spkFile, lastGoodFile.spk);
    showSpkWarn(err);
    return;
  }
  clearPrecheckError("请先选择音色参考音频");
  const prevGood = lastGoodFile.spk;
  refSelSeq.spk++;
  // 同 setEmoFile：拖拽 / 预设音频只是 JS 里的一个 File，不会进 input.files，
  // 而 /tts 与「保存预设」都只读 input.files[0]，不同步就会出现
  // “界面和试听都换了，生成还是旧音色”。
  if (spkFile.files[0] !== f) assignFile(spkFile, f);
  $("spkFileName").textContent = f.name;
  $("spkFileName").title = f.name;
  $("spkDrop").hidden = true;
  $("spkPlayerBox").hidden = false;
  $("spkTime").textContent = "读取中…";
  if (persist) setDraftBlob(DRAFT_AUDIO_KEYS.spk, f).catch(() => {});
  try {
    if (spkUrl) URL.revokeObjectURL(spkUrl);
    spkUrl = URL.createObjectURL(f);
    spkAudio.src = spkUrl;
    f.arrayBuffer().then((ab) => decodeNative(ab)).then((buf) => {
      spkBuffer = buf;
      lastGoodFile.spk = f;
      spkPlaying = false; spkSeekFrac = 0;
      $("spkTime").textContent = `0.00s / ${spkBuffer.duration.toFixed(2)}s`;
      // 参考音频超过 15 秒时，服务端只取前 15 秒做音色克隆（infer_v2 的
      // _load_and_cut_audio 硬截断），超出部分白传。明确提示，免得用户
      // 以为整段都用了，或把音色偏归咎于 bug。
      // 15.05 是浮点容差：15.000001 秒不该报「只有前 15 秒」。
      const dur = spkBuffer.duration;
      showSpkWarn(dur > 15.05 ? `${dur.toFixed(1)} 秒 · 只有前 15 秒参与音色克隆` : "");
      spkTick();
    }).catch(async () => {
      // 浏览器解不了的容器（实测 Chrome 不支持 wma）交给服务端 ffmpeg 转 WAV，
      // 转出来的就是普通 wav，再走一遍本函数。
      // prevGood === f 时不能再拿它「恢复」，否则同一份解不开的文件会无限重试。
      const restorePrev = () => {
        if (prevGood && prevGood !== f) setSpkFile(prevGood, true);
        else resetSpkSelection();
      };
      const seq = mediaPickSeq.spk;
      let wav = null;
      try {
        wav = await transcodeForBrowser(f);
      } catch (e) {
        if (seq !== mediaPickSeq.spk) return;   // 期间用户已换文件，丢弃
        restorePrev();
        showSpkWarn(`无法使用这个音频：${e.message || e}`);
        return;
      }
      if (seq !== mediaPickSeq.spk) return;
      if (wav) { setSpkFile(wav); return; }
      restorePrev();
      showSpkWarn("浏览器无法解码这个音频，文件可能已损坏；请换一个文件");
    });
  } catch (e) { /* ignore */ }
}

/* ---------------- 参考音频裁剪 ----------------
   整段录音里往往只有一小段是干净的目标音色，而服务端硬截断前 15 秒。
   「先截出来再用」比「传整段让它自己猜」既准又省一次上传。
   选区 UI 全在 web/clip.js（只管选区），这里负责编码、写回表单、问存预设。 */

/* 裁剪后的文件名：总长封顶 60 个 UTF-16 单元（NTFS 单段上限 255，这里给路径与
   中文留足余量），并且**先算后缀再算原名**。原来先把原名切到 60 再拼后缀，反复
   裁剪时后缀越叠越长，原名被从中间截断成「…_clip_0.00-10.00_c_clip_…」断串；
   现在每次都先剥掉上一次的 _clip_ 标记，名字里永远只留最近一次选区。 */
const SPK_CLIP_NAME_MAX = 60;
const SPK_CLIP_EPS = 0.01;      // 选区两端各差 10ms 以内视为「整段」
const clipBareName = (srcName) =>
  srcName.replace(/\.[^.]+$/, "").replace(/_clip_[0-9.]+-[0-9.]+$/, "") || "参考音频";

function clipFileName(srcName, s0, s1) {
  const suffix = `_clip_${s0.toFixed(2)}-${s1.toFixed(2)}.wav`;
  const room = Math.max(1, SPK_CLIP_NAME_MAX - suffix.length);
  return `${clipBareName(srcName).slice(0, room)}${suffix}`;
}

function openSpkClip() {
  if (!spkBuffer) { showActionError("音频还在读取，请稍候再试"); return; }
  if (!window.ClipDialog) { showActionError("裁剪组件未加载，请刷新页面"); return; }
  const src = spkFile.files[0] || null;
  const srcName = (src && src.name) || "参考音频";
  window.ClipDialog.open({
    buffer: spkBuffer,
    name: srcName,
    okText: "设为参考音频",
    onDownload: (r) => downloadSpkClip(srcName, r),
  }).then((res) => {
    // 取消 / 关闭返回 null；0 长度选区在弹窗里已被禁用，这里兜一次
    if (!res || !(res.s1 > res.s0)) return;
    applySpkClip(srcName, res);
  }).catch((e) => showActionError("裁剪失败: " + (e.message || e)));
}

/* 裁剪弹窗内的下载：完整 = 原始文件字节（不重编码，mp3 还是 mp3，与「未做裁剪」同语义）；
   选区 = 16bit WAV，命名与「设为参考音频」完全一致，下载的就是会生效的那一份。 */
function downloadSpkClip(srcName, r) {
  const d = spkBuffer.duration;
  if (r.full) {
    const f = spkFile.files[0];
    if (f) {
      triggerDownload(URL.createObjectURL(f), f.name || `${clipBareName(srcName)}.wav`);
      return "已下载完整音频";
    }
    triggerDownload(URL.createObjectURL(encodeWav(spkBuffer)), `${clipBareName(srcName)}.wav`);
    return "已下载完整音频（WAV）";
  }
  if (!(r.s1 > r.s0)) throw new Error("选区为空");
  // encodeWav 收的是「归一化比例」而不是秒（内部乘 buffer.length）
  triggerDownload(URL.createObjectURL(encodeWav(spkBuffer, r.s0 / d, r.s1 / d)),
    clipFileName(srcName, r.s0, r.s1));
  return `已下载选区（${(r.s1 - r.s0).toFixed(2)} 秒）`;
}

function applySpkClip(srcName, res) {
  const d = spkBuffer.duration;
  // 弹窗默认全选，什么都不动直接点「应用」也会走到这里。选区≈整段就是没裁：
  // 不重编码、不改名、不弹「保存为预设」，只说清楚发生了什么（BUG-013）。
  if (res.s0 <= SPK_CLIP_EPS && res.s1 >= d - SPK_CLIP_EPS) {
    showActionHint("未做裁剪：选区就是整段音频，参考音频保持不变");
    return;
  }
  // encodeWav 收的是「归一化比例」而不是秒（内部乘 buffer.length）
  const blob = encodeWav(spkBuffer, res.s0 / d, res.s1 / d);
  const file = new File([blob], clipFileName(srcName, res.s0, res.s1), { type: "audio/wav" });
  // 走「用户主动换音频」那条路径：清掉预设选中态、同步 input.files、写草稿。
  // 不同步 input.files 的话，界面显示的是裁剪结果，生成/存预设却仍拿原文件。
  userPickedSpk(file);
  showActionHint(`已截取 ${(res.s1 - res.s0).toFixed(2)} 秒`);
  askSaveClipPreset(clipBareName(srcName)).catch((e) => showActionError("保存剪辑预设失败: " + (e.message || e)));
}

/* 裁完再问一次要不要存预设：不存的话，下次想复用还得重新上传原文件、再裁一遍。 */
async function askSaveClipPreset(stem) {
  const yes = await uiConfirm({
    title: "保存为预设",
    body: "已把截取的这一段设为参考音频。要不要顺手存成一个预设？下次直接选用，不用再裁一次。",
    okText: "保存为预设",
    cancelText: "不用了",
  });
  if (!yes) return;
  const name = await uiPrompt({
    title: "预设名称",
    placeholder: "请输入预设名称",
    value: `${stem.slice(0, 40)}_clip`,
  });
  if (name === null || name === false) return;
  const trimmed = String(name).trim();
  if (!trimmed) { showActionError("请输入预设名称"); return; }
  try {
    // savePresetByName 读的是 #spkFile.files[0]，上面 userPickedSpk 已把它换成裁剪结果
    if (!await savePresetByName(trimmed)) return;
    curPresetName = trimmed;
    showActionHint("预设已保存: " + trimmed);
    await refreshPresets();
    renderLib();
    updateGenSummary();
    refreshPmList();
  } catch (e) { showActionError("保存预设失败: " + (e.message || e)); }
}

/* ---------------- emotion mode visibility ---------------- */
const EMO_NAMES = ["喜", "怒", "哀", "惧", "厌恶", "低落", "惊喜", "平静"];
const vecRow = $("vecs");
EMO_NAMES.forEach((n, i) => {
  const row = document.createElement("div");
  row.className = "vec-row";
  const name = document.createElement("span");
  name.className = "vec-name"; name.textContent = n;
  const s = document.createElement("input");
  s.type = "range"; s.min = 0; s.max = 1; s.step = 0.05; s.value = 0;
  s.dataset.i = i; s.title = n;
  // 只给 title 不够：多数读屏不把 title 当可靠的可访问名，
  // 于是 8 个滑块全被读成「滑块 0.5」，分不清是「喜」还是「怒」。
  s.setAttribute("aria-label", "情感向量 " + n);
  const val = document.createElement("span");
  val.className = "vec-val"; val.textContent = "0.00";
  s.addEventListener("input", () => {
    val.textContent = Number(s.value).toFixed(2);
    s.style.setProperty("--fill", (s.value * 100) + "%");
  });
  row.append(name, s, val);
  vecRow.appendChild(row);
});
const vecInputs = () => document.querySelectorAll("#vecs input");
function setVecInputs(arr) {
  vecInputs().forEach((s) => {
    const v = arr[parseInt(s.dataset.i)] ?? 0;
    s.value = v;
    s.style.setProperty("--fill", (v * 100) + "%");
    const val = s.parentElement.querySelector(".vec-val");
    if (val) val.textContent = Number(v).toFixed(2);
  });
}
/* 快捷情感：一键填一组典型向量（单轴饱和值，与示例库 voice_09 的用法一致） */
$("vecChips").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  setVecInputs(chip.dataset.v.split(",").map(Number));
});
function emoVisible() {
  const m = $("emoMode").value;
  $("emoAudioBox").hidden = m !== "1";
  $("emoVecBox").hidden = m !== "2";
  $("emoTextBox").hidden = m !== "3";
}
$("emoMode").addEventListener("change", emoVisible);
// 清空旧情感音频选择，避免从模式1切走再切回时把过期文件发上去
$("emoMode").addEventListener("change", () => { if ($("emoMode").value !== "1") clearEmoFile(); });
$("emoMode").addEventListener("change", scheduleDraftSave);

/* ---------------- 参数：滑块 + 数字框同源、单项恢复默认、锁定 ---------------- */
// 关键约定：滑块（range）始终持有"真值"，数字框只是它的同步显示与输入入口。
// 因此所有既有读取点（生成请求 / 保存预设 / 草稿）继续读 $(id).value 即可，
// 不需要判断"以哪个为准"。为保证任意两位小数都能被滑块表示，range 的 step
// 统一调到 0.01（temperature）/ 1（maxMel、gapMs），不再用 0.05 / 10。
const PARAMS = [
  { id: "emoWeight",   dec: 2, lock: false },
  { id: "temperature", dec: 2, lock: true  },
  { id: "topP",        dec: 2, lock: true  },
  { id: "topK",        dec: 0, lock: true  },
  { id: "numBeams",    dec: 0, lock: true  },
  { id: "maxMel",      dec: 0, lock: true  },
  { id: "segTokens",   dec: 0, lock: false },
  { id: "repPen",      dec: 1, lock: true  },
  { id: "lenPen",      dec: 1, lock: true  },
  { id: "gapMs",       dec: 0, lock: true  },
];
const PARAM_BY_ID = Object.fromEntries(PARAMS.map((p) => [p.id, p]));
// 锁定范围 = 「高级参数」折叠里的 9 项（do_sample 开关 + 8 个数值）；
// segTokens 属于"怎么切句"，不锁。
const LOCKED_IDS = ["doSample", ...PARAMS.filter((p) => p.lock).map((p) => p.id)];
const DEFAULTS = { emoWeight: 0.65, temperature: 0.8, topP: 0.8, topK: 30, numBeams: 3, maxMel: 1500, segTokens: 120, repPen: 10, lenPen: 0, gapMs: 200, doSample: true };

// 前 5 项参数是 range + 配套的 <id>Num 数字框；repPen / lenPen / gapMs 天生就是
// number 输入，本身即数字框，此时 numEl 直接返回它自己。
const numEl = (id) => {
  const paired = $(id + "Num");
  if (paired) return paired;
  const self = $(id);
  return self && self.type === "number" ? self : null;
};
const paramDec = (id) => PARAM_BY_ID[id]?.dec ?? 2;
const fmtParam = (id, v) => Number(v).toFixed(paramDec(id));

function sliderFill(el) {
  const min = +el.min, max = +el.max, v = +el.value;
  el.style.setProperty("--fill", `${((v - min) / (max - min)) * 100}%`);
}

/* 任意输入 → 该参数的合法值：夹到 min/max，按 step 吸附到网格 */
function normalizeParam(id, raw) {
  const el = $(id);
  const min = +el.min, max = +el.max, step = +el.step || 1;
  let v = Number(raw);
  if (!Number.isFinite(v)) return null;
  if (Number.isFinite(min)) v = Math.max(min, v);
  if (Number.isFinite(max)) v = Math.min(max, v);
  if (Number.isFinite(step) && step > 0) {
    const base = Number.isFinite(min) ? min : 0;
    v = base + Math.round((v - base) / step) * step;
    v = Number(v.toFixed(6)); // 收掉 0.1 步长产生的 0.30000000000000004
  }
  return v;
}

function paramLabel(id) {
  const lab = document.querySelector(`label[for="${id}"]`);
  // label 形如「重复惩罚 <span>repetition_penalty</span>」，取中文那半截
  return lab ? (lab.firstChild.textContent || "").trim() : id;
}

/* 发请求前把所有参数收敛到合法区间，并把界面显示一起回夹。
   以前数字框里留着 99 就直接发出去，服务端静默夹到 20：界面显示 99、实际用 20，
   而同一个面板里的滑块型参数会立刻回夹——两种行为并存，用户没法凭界面判断真实参数
   （BUG-006）。这里以界面为准收敛后再发送，并把改动的项一次性说出来。 */
function clampParamsForSend() {
  const changed = [];
  for (const p of PARAMS) {
    const el = $(p.id), num = numEl(p.id);
    if (!el || !num) continue;
    const v = normalizeParam(p.id, num.value);
    if (v === null) continue;              // 空/非数字：保持原值，交给既有兜底
    if (Number(num.value) !== v) changed.push(`${paramLabel(p.id)} ${num.value} → ${fmtParam(p.id, v)}`);
    el.value = v;                          // 滑块跟着走（el === num 时就是回写数字框）
    if (el.type === "range") sliderFill(el);
    num.value = fmtParam(p.id, v);
    markResetBtn(p.id);
  }
  return changed;
}

/* 写值并同步两侧显示。src="num" 表示来源是数字框，此时不回写它，避免打断打字 */
function setParam(id, raw, src = "") {
  const v = normalizeParam(id, raw);
  if (v === null) return false;
  const el = $(id), num = numEl(id);
  el.value = v;
  if (el.type === "range") sliderFill(el);
  if (num && src !== "num") num.value = fmtParam(id, v);
  markResetBtn(id);
  return true;
}

/* 同步全部参数的显示（替代原来的 bindSliders 调用点） */
function syncParams() {
  for (const p of PARAMS) {
    const el = $(p.id), num = numEl(p.id);
    if (!el) continue;
    if (el.type === "range") sliderFill(el);
    if (num) num.value = fmtParam(p.id, el.value);
    markResetBtn(p.id);
  }
}

/* ↺ 的可用感：偏离默认值才变实色，扫一眼就知道哪几项被改过 */
function markResetBtn(id) {
  const btn = document.querySelector(`.rdef[data-pid="${id}"]`);
  const el = $(id);
  if (!btn || !el) return;
  const step = Number(el.step) || 1;
  btn.classList.toggle("on", Math.abs(Number(el.value) - Number(DEFAULTS[id])) > step / 2);
}

/* 事件只绑一次。原来叫 bindSliders，每次调用都 addEventListener 会叠加监听器 */
function bindParamEvents() {
  // 英文参数名在窄栏里会被省略号截断，把全名挂到 title 上
  document.querySelectorAll(".pitem label span").forEach((s) => { s.title = s.textContent; });
  for (const p of PARAMS) {
    const el = $(p.id), num = numEl(p.id);
    if (el && el.type === "range") {
      el.addEventListener("input", () => {
        if (num) num.value = fmtParam(p.id, el.value);
        sliderFill(el);
        markResetBtn(p.id);
      });
    }
    if (!num) continue;
    num.addEventListener("input", () => {
      const raw = num.value;
      if (raw === "" || raw === "-" || raw === ".") return; // 打字中间态，等 blur 兜底
      const v = normalizeParam(p.id, raw);
      if (v === null) return;
      // el === num 时（repPen/lenPen/gapMs）不能回写，否则会把正在输入的内容
      // 覆盖掉——例如输入 "0.5"，打到 "0" 就被夹成 0.1。
      if (el !== num) {
        el.value = v;            // 滑块跟着走；数字框保留用户原文
        sliderFill(el);
      }
      markResetBtn(p.id);
      scheduleDraftSave();
    });
    num.addEventListener("blur", () => {
      const v = normalizeParam(p.id, num.value);
      if (v === null || num.value === "") { num.value = fmtParam(p.id, el.value); return; }
      setParam(p.id, v);
      if (p.id === "segTokens") scheduleSegments();
      updateGenSummary();
      scheduleDraftSave();
    });
    num.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); num.blur(); }
    });
  }
  // 单项恢复默认值
  document.querySelectorAll(".rdef").forEach((btn) => {    btn.addEventListener("click", () => {
      const id = btn.dataset.pid;
      if (paramLock.locked && LOCKED_IDS.includes(id)) return;
      setParam(id, DEFAULTS[id]);
      if (id === "segTokens") scheduleSegments();
      updateGenSummary();
      scheduleDraftSave();
    });
  });
}

/* ---------------- 参数锁定：快照 + 服务端持久化 ---------------- */
// 语义：锁定瞬间记下 9 项的值，之后任何来源（应用预设、草稿恢复、重置）的写入
// 都被回滚到这份快照。存在服务端 data/ui_state.json，清浏览器缓存不会丢。
const LOCK_CACHE_KEY = "studioParamLock.v1";
let paramLock = { locked: false, values: {} };

function snapshotLockedParams() {
  const out = {};
  for (const id of LOCKED_IDS) {
    const el = $(id);
    out[id] = el.type === "checkbox" ? !!el.checked : String(el.value);
  }
  return out;
}

/* 把快照写回控件 —— 所有覆盖路径的最后一道关 */
function applyLockSnapshot() {
  if (!paramLock.locked) return;
  for (const id of LOCKED_IDS) {
    const v = paramLock.values[id];
    if (v === undefined || v === null || v === "") continue;
    const el = $(id);
    if (el.type === "checkbox") el.checked = !!v;
    else setParam(id, v);
  }
}
function enforceParamLock() { applyLockSnapshot(); }

function paintLockUI() {
  const locked = !!paramLock.locked;
  const fold = $("advFold");
  if (fold) fold.classList.toggle("params-locked", locked);
  $("lockBadge").hidden = !locked;
  $("lockHint").hidden = !locked;
  $("paramLock").checked = locked;
  for (const id of LOCKED_IDS) {
    const el = $(id), num = numEl(id);
    if (el) el.disabled = locked;
    if (num) num.disabled = locked;
  }
  document.querySelectorAll(".rdef").forEach((b) => {
    if (LOCKED_IDS.includes(b.dataset.pid)) b.disabled = locked;
  });
}

async function saveParamLock() {
  try { localStorage.setItem(LOCK_CACHE_KEY, JSON.stringify(paramLock)); } catch (e) { /* 存满也不影响使用 */ }
  try {
    await apiFetch("/prefs/param-lock", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(paramLock),
    });
  } catch (e) {
    // 本地缓存仍然生效，但只在这一台浏览器里——不说的话，下次换设备就以为锁丢了
    showActionError("参数锁定未能同步到服务端（" + (e.message || e) + "），本次仅在本机生效");
  }
}

function toggleParamLock(locked) {
  paramLock = locked
    ? { locked: true, values: snapshotLockedParams() }
    : { locked: false, values: {} };
  paintLockUI();
  saveParamLock();
  // 把「眼下看到的数值」记成草稿：锁定期间草稿可能停在锁定前的旧值（刷新时
  // 被快照覆盖过），解锁后若不落盘，下一次刷新会看到参数自己跳回旧值。
  scheduleDraftSave();
  showActionHint(locked ? "参数已锁定：应用预设不再改变这些数值" : "参数已解锁");
}
$("paramLock").addEventListener("change", () => toggleParamLock($("paramLock").checked));

/* 先用本地缓存顶上，避免页面先闪一下默认值；随后以服务端为准校正 */
function loadParamLockFromCache() {
  try {
    const raw = JSON.parse(localStorage.getItem(LOCK_CACHE_KEY) || "null");
    if (!raw || typeof raw !== "object") return;
    paramLock = {
      locked: !!raw.locked,
      values: raw.values && typeof raw.values === "object" ? raw.values : {},
    };
    paintLockUI();
    applyLockSnapshot();
  } catch (e) { /* ignore */ }
}

async function loadParamLockFromServer() {
  try {
    const r = await fetch(`${API}/prefs/param-lock`);
    if (!r.ok) return;
    const j = await r.json();
    // 进入本函数时若已是锁定态，说明上一步的本地缓存已经（或即将）把旧快照
    // 压到控件上；服务端一旦说「没锁」，这份覆盖就必须被撤掉。
    const cacheOverlaid = !!paramLock.locked;
    paramLock = {
      locked: !!j.locked,
      values: j.locked && j.values && typeof j.values === "object" ? j.values : {},
    };
    paintLockUI();
    if (paramLock.locked) applyLockSnapshot();
    else if (cacheOverlaid) restoreLockedParamsFromDraft();
    try { localStorage.setItem(LOCK_CACHE_KEY, JSON.stringify(paramLock)); } catch (e) { /* ignore */ }
  } catch (e) { /* 服务端不可用：沿用本地缓存 */ }
}

/* 本地缓存的快照只是「避免闪一下默认值」的乐观显示，服务端才是权威。
   当服务端判定未锁定、而缓存已经把快照压到控件上时，要把这 9 项还原成草稿里
   的真值——否则解锁后参数会一直停在某个浏览器残留的旧快照上，看起来像「锁
   开关关了但数值没回来」。草稿里缺该项（老版本草稿）就回默认值。 */
function restoreLockedParamsFromDraft() {
  const draft = readDraft();
  for (const id of LOCKED_IDS) {
    const el = $(id);
    if (!el) continue;
    const v = draft ? draft[id] : undefined;
    if (el.type === "checkbox") el.checked = v == null ? !!DEFAULTS[id] : !!v;
    else setParam(id, v == null ? DEFAULTS[id] : v);
  }
  syncParams();
  updateGenSummary();
  scheduleDraftSave();
}

$("resetParams").addEventListener("click", () => {
  if (paramLock.locked) {
    showActionHint("参数已锁定，先解锁再恢复默认值");
    return;
  }
  for (const [k, v] of Object.entries(DEFAULTS)) {
    const el = $(k);
    if (el.type === "checkbox") el.checked = v;
    else setParam(k, v);
  }
  syncParams();
  scheduleSegments();
  updateGenSummary();
  scheduleDraftSave();
});
$("segTokens").addEventListener("input", scheduleSegments);

/* ---------------- segments preview (collapsible, capped scroll) ---------------- */
const segList = $("segList");
let segTimer = null;
let segSeq = 0;          // 请求序号：防止慢的旧请求后到、覆盖新内容
let segEst = null;       // 最近一次分句预览的耗时估算，长文候选提示复用
async function updateSegments() {
  const mine = ++segSeq;
  const text = $("text").value;
  if (!text.trim()) {
    segEst = null;
    segList.replaceChildren();
    $("segInfo").textContent = "分句预览待输入";
    $("estDurEcho").textContent = "输入文本后这里会显示分句预览";
    return;
  }
  const fd = new FormData();
  fd.append("text", text);
  fd.append("max_text_tokens_per_segment", $("segTokens").value);
  try {
    const r = await fetch(`${API}/segments`, { method: "POST", body: fd });
    // 服务端报错（500 / 分词器加载失败 / 代理返回 HTML）时不判 ok，
    // 会走到 segs=[] 把列表清空，而上方的「分句 N 段 · M tok」还留着旧值，
    // 看起来像数据丢了且没有任何错误反馈。
    if (!r.ok) {
      if (mine === segSeq) { segList.replaceChildren(); $("segInfo").textContent = "分句失败，请稍后重试"; }
      return;
    }
    const j = await r.json().catch(() => ({}));
    // 期间用户又改过文本、发出了更新的请求：丢掉这次结果，否则慢的旧请求
    // 后到会把新内容盖回旧内容。实测：服务刚启动时第一个 /segments 要走
    // _ensure_light 加载分词器（几秒），第二个请求几十毫秒就回来了——
    // 老的慢请求后到，预览就变回第一次输入的内容。
    if (mine !== segSeq) return;
    const segs = j.segments || [];
    if (!segs.length) { segList.replaceChildren(); return; }
    const rows = segs.map((s) => {
      const row = document.createElement("div");
      row.className = "seg-item";
      const idx = document.createElement("span");
      idx.className = "seg-idx";
      idx.textContent = s.index;
      const text = document.createElement("span");
      text.className = "seg-text";
      // 后端已把 BPE 词元反解码成可读文本（且是真正会被念出来的规范化文本）
      text.textContent = s.text;
      const tok = document.createElement("span");
      tok.className = "seg-tok";
      tok.textContent = `${s.tokens} tok`;
      row.append(idx, text, tok);
      return row;
    });
    segList.replaceChildren(...rows);
    const totalTok = segs.reduce((a, s) => a + s.tokens, 0);
    $("segInfo").textContent = `分句 ${segs.length} 段 · ${totalTok} tok`;
    // 耗时预估：每段固定开销 + 每 token 线性项（按本机 RTX 3060 Ti 默认配置标定，
    // 粗略值只为给长文本用户一个心理预期，不是精确基准）。
    // 注意不含【首次模型加载】（实测冷启动约 30~60 秒），也不含排队等待。
    const estSec = Math.round(segs.length * 4 + totalTok * 0.06);
    segEst = { text, segTokens: $("segTokens").value, estSec };
    // 只说一次「约」：原来是「预估 约 8 秒」，两个词表达同一件事
    const est = estSec >= 60 ? `${Math.round(estSec / 60)} 分钟` : `${estSec} 秒`;
    $("estDurEcho").textContent =
      `共 ${segs.length} 段 · ${textUnits($("text").value.trim())} 字 · 约 ${est}（不含首次加载模型）`;
    if (segs.length > 10) {
      $("estDurEcho").textContent += "（长文本，建议分段生成）";
    }
  } catch (err) { /* keep the last good preview */ }
}
function scheduleSegments() {
  clearTimeout(segTimer);
  segTimer = setTimeout(updateSegments, 300);
}
$("text").addEventListener("input", () => {
  const el = $("text");
  let value = el.value;
  let truncated = false;
  if (textUnits(value) > TEXT_CHARS_HARD) {
    const cut = Array.from(value).slice(0, TEXT_CHARS_HARD).join("");
    const caret = Math.min(el.selectionStart ?? cut.length, cut.length);
    el.value = cut;
    el.setSelectionRange(caret, caret);
    value = cut;
    truncated = true;
  }
  const len = textUnits(value.trim());
  const cc = $("charCount");
  cc.textContent = `${len} 字`;
  // 软上限提示：超过 TEXT_CHARS_SOFT 只是提醒，不再拦着不让生成
  // （后端 /tts 只在 TEXT_CHARS_HARD=10000 处才拒）
  cc.classList.toggle("warn", len > TEXT_CHARS_SOFT);
  if (truncated) {
    cc.textContent += `（超过 ${TEXT_CHARS_HARD} 字上限，多余部分已截断）`;
  } else if (len > TEXT_CHARS_SOFT) {
    cc.textContent += "（文本较长，生成会很慢，期间无法生成其他内容）";
  }
  if (len) clearPrecheckError("请先输入要合成的文本");
  scheduleSegments();
  scheduleDraftSave();
});

// 文本框高度由 CSS 固定（#text 固定高 + 内部滚动），不随内容自动撑高

/* ---------------- text file import (F4) ----------------
   纯前端：读文件 → 判编码 → 清洗 → 截断 → 用 execCommand 整体替换。
   走 execCommand 而不是直接改 value，是为了保留浏览器原生撤销栈（Ctrl+Z 能回退）。 */
const TEXT_FILE_MAX = 2 * 1024 * 1024;
const textWrap = $("textWrap");
const textDropOverlay = $("textDropOverlay");
const textImportNote = $("textImportNote");

function showTextImportNote(msg, isError) {
  textImportNote.textContent = msg;
  textImportNote.classList.toggle("error", !!isError);
  textImportNote.hidden = false;
  // 本次失败时不能继续挂着上一次「已导入 …」的成功提示
  if (isError) showActionHint("");
}
function clearTextImportNote() {
  textImportNote.textContent = "";
  textImportNote.classList.remove("error");
  textImportNote.hidden = true;
}

/* Files 只在拖入文件时出现；拖选中的文字不算，不能拦（否则正常拖字也失效） */
function dndHasFiles(e) {
  return !!(e.dataTransfer && [...e.dataTransfer.types].includes("Files"));
}

/* 二进制判定：NUL 直接判死；C0 控制字符（不含 \t \n \v \f \r）占比 >0.5% 判死。
   不能用「替换符占比」：gb18030 几乎能解任何字节，docx/pdf 解出来替换符接近 0。 */
function looksBinary(u8) {
  const n = Math.min(u8.length, 64 * 1024);
  let ctrl = 0;
  for (let i = 0; i < n; i++) {
    const b = u8[i];
    if (b === 0) return true;
    if (b < 0x09 || (b > 0x0d && b < 0x20) || b === 0x7f) ctrl++;
  }
  return ctrl / Math.max(n, 1) > 0.005;
}

/* 返回 { text, encoding, warn } 或 { binary: true }。
   BOM 必须先于 looksBinary：UTF-16 文本本身就含大量 NUL，先查 NUL 会把正常文本判成二进制。 */
function decodeTextFile(buf) {
  const u8 = new Uint8Array(buf);
  if (u8.length >= 3 && u8[0] === 0xef && u8[1] === 0xbb && u8[2] === 0xbf) {
    return { text: new TextDecoder("utf-8").decode(u8.subarray(3)), encoding: "UTF-8", warn: "" };
  }
  if (u8.length >= 2 && u8[0] === 0xff && u8[1] === 0xfe) {
    return { text: new TextDecoder("utf-16le").decode(u8.subarray(2)), encoding: "UTF-16LE", warn: "" };
  }
  if (u8.length >= 2 && u8[0] === 0xfe && u8[1] === 0xff) {
    return { text: new TextDecoder("utf-16be").decode(u8.subarray(2)), encoding: "UTF-16BE", warn: "" };
  }
  if (looksBinary(u8)) return { binary: true };
  try {
    // fatal:true 严格试解：能解就是 UTF-8，解不了就是别的编码，不会静默产出替换符
    return { text: new TextDecoder("utf-8", { fatal: true }).decode(u8), encoding: "UTF-8", warn: "" };
  } catch (e) { /* 不是合法 UTF-8，继续试中文 Windows 的默认编码 */ }
  try {
    return { text: new TextDecoder("gb18030").decode(u8), encoding: "GB18030", warn: "" };
  } catch (e) { /* 浏览器不支持 gb18030（极少见），退到带替换符的 UTF-8 */ }
  return { text: new TextDecoder("utf-8").decode(u8), encoding: "UTF-8", warn: "编码未识别，个别字符可能乱码" };
}

/* 按码点截断：slice 数的是 UTF-16 单元，会把代理对切一半，留下孤立的高位代理 */
function cutImportText(s) {
  const cp = [...s];
  return cp.length <= TEXT_CHARS_HARD ? s : cp.slice(0, TEXT_CHARS_HARD).join("");
}

function importText(text) {
  const ta = $("text");
  ta.focus();
  ta.select();                       // 整体替换：不会把新文本接在旧文本后面
  let ok = false;
  try { ok = document.execCommand("insertText", false, text); } catch (e) { ok = false; }
  if (!ok) {
    ta.value = text;                 // 老浏览器兜底：失去撤销，但仍能导入
    // 只有兜底路径需要手动派发；execCommand 成功时浏览器自己会发 input，
    // 字数/分句/草稿监听（上方 input 处理）会正常跑，不能再派发一次。
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  }
}

async function importTextFile(file) {
  if (!file) return;
  clearTextImportNote();
  const size = Number(file.size) || 0;
  if (size > TEXT_FILE_MAX) {
    showTextImportNote(`「${file.name}」${(size / 1048576).toFixed(1)}MB，超过 2MB 上限，请拆分后再导入`, true);
    return;
  }
  // 音频拖错位置：直接指路，不去读二进制再报「不是纯文本」
  const looksAudio = /^(audio|video)\//.test(file.type || "")
    || /\.(mp3|wav|flac|ogg|m4a|aac|opus)$/i.test(file.name || "");
  if (looksAudio) {
    // m4a/aac/wma 只有在服务端装了 ffmpeg 时才能收，没装就别把人指到一个必然被拒的入口
    const exotic = /\.(m4a|aac|wma)$/i.test(file.name || "")
      || /^audio\/(mp4|x-m4a|aac|x-aac|x-ms-wma)$/.test(file.type || "");
    const unsupported = exotic && !serverFfmpeg;
    showTextImportNote(unsupported
      ? "m4a/aac/wma 格式不支持，请先转成 wav 或 mp3，再拖到上方「音色参考音频」"
      : "这是音频文件，请拖到上方「音色参考音频」", true);
    return;
  }
  let buf;
  try {
    buf = await file.arrayBuffer();
  } catch (e) {
    showTextImportNote(`读取「${file.name}」失败：${e.message || e}`, true);
    return;
  }
  let res;
  try {
    res = decodeTextFile(buf);
  } catch (e) {
    showTextImportNote(`解析「${file.name}」失败：${e.message || e}`, true);
    return;
  }
  if (res.binary) {
    showTextImportNote(`「${file.name}」不是纯文本文件（可能是 Word / PDF / 图片 / 压缩包），请先另存为 .txt 再导入`, true);
    return;
  }
  // 只做三件不改变内容的事：去 BOM、统一换行、去 NUL。空格与空行是用户的文本，不动。
  const cleaned = (res.text || "").replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n").replace(/\u0000/g, "");
  if (!cleaned.trim()) {
    showTextImportNote(`「${file.name}」是空文件，没有可导入的文本`, true);
    return;
  }
  const chars = textUnits(cleaned);
  const truncated = chars > TEXT_CHARS_HARD;
  const text = cutImportText(cleaned);
  const cur = $("text").value;
  if (cur.trim()) {
    const ok = await uiConfirm({
      title: "替换现有文本",
      body: `导入会用「${file.name}」的内容替换文本框里现有的 ${textUnits(cur.trim())} 字；导入后可按 Ctrl+Z 撤销。`,
      okText: "替换",
    });
    if (!ok) return;
  }
  importText(text);
  const parts = [`已导入 ${file.name}`, `${chars} 字`, res.encoding];
  if (truncated) parts.push(`已截断到 ${TEXT_CHARS_HARD} 字`);
  const msg = parts.join(" · ");
  if (res.warn) showActionError(`${msg} · ${res.warn}`);
  else showActionHint(msg);
}

$("textImportBtn").addEventListener("click", () => $("textFile").click());
$("textFile").addEventListener("change", () => {
  const f = $("textFile").files && $("textFile").files[0];
  // 先清空再导入：同一个文件第二次选择也要触发 change（否则"再导入一次"毫无反应）
  $("textFile").value = "";
  if (f) importTextFile(f).catch((e) => showTextImportNote("导入失败：" + (e.message || e), true));
});

/* 拖拽用计数判断"真正离开"：文本框是子节点，进出它会连续触发 dragleave，
   单次判断会让高亮在拖动过程中不停闪烁。 */
let textDragDepth = 0;
textWrap.addEventListener("dragenter", (e) => {
  if (!dndHasFiles(e)) return;
  e.preventDefault();
  textDragDepth++;
  textDropOverlay.hidden = false;
});
textWrap.addEventListener("dragover", (e) => {
  if (!dndHasFiles(e)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "copy";
});
textWrap.addEventListener("dragleave", (e) => {
  if (!dndHasFiles(e)) return;
  textDragDepth = Math.max(0, textDragDepth - 1);
  if (!textDragDepth) textDropOverlay.hidden = true;
});
textWrap.addEventListener("drop", (e) => {
  if (!dndHasFiles(e)) return;
  e.preventDefault();
  textDragDepth = 0;
  textDropOverlay.hidden = true;
  const files = [...(e.dataTransfer.files || [])];
  if (!files.length) return;
  const first = files[0];
  // 多文件时只取第一个：一次导入一份稿子，其余忽略并说明
  importTextFile(first)
    .then(() => { if (files.length > 1) showTextImportNote(`已导入第 1 个文件「${first.name}」，其余 ${files.length - 1} 个已忽略`, false); })
    .catch((err) => showTextImportNote("导入失败：" + (err.message || err), true));
});

/* 全局守卫：拖入文件到页面任何位置都拦住浏览器默认的"打开这个文件"，
   否则误拖一次就离开工作台、草稿与正在播放的音频全部丢失。 */
["dragover", "drop"].forEach((t) => {
  document.addEventListener(t, (e) => {
    if (dndHasFiles(e)) e.preventDefault();
  });
});

/* gen summary line: emotion mode + preset name (visible param confirmation above CTA) */
function updateGenSummary() {
  const modeNames = ["情感同参考音频", "情感参考音频", "情感向量", "情感描述文本"];
  const m = modeNames[$("emoMode").value] ?? $("emoMode").value;
  const parts = [
    `情感: ${m} (权重 ${Number($("emoWeight").value).toFixed(2)})`,
    curPresetName ? `预设: ${curPresetName}` : "预设: 无",
    `采样: temp ${Number($("temperature").value).toFixed(2)} / top_p ${Number($("topP").value).toFixed(2)} / top_k ${$("topK").value}`,
  ];
  $("genSummary").replaceChildren(...parts.map((t) => {
    // 一段一个 span（样式里 nowrap）：换行只发生在段与段之间，
    // 不会把「top_k 30」拆成两行、让末行只剩一个孤零零的数字
    const s = document.createElement("span");
    s.textContent = t;
    return s;
  }));
}
["emoMode", "emoWeight", "temperature", "topP", "topK"].forEach((id) => {
  $(id).addEventListener("input", updateGenSummary);
  $(id).addEventListener("change", updateGenSummary);
});
["emoText", "emoRandom", "doSample", "topP", "topK", "temperature", "lenPen", "numBeams", "repPen", "maxMel", "segTokens", "emoWeight", "gapMs"].forEach((id) => {
  $(id).addEventListener("input", scheduleDraftSave);
  $(id).addEventListener("change", scheduleDraftSave);
});

/* ---------------- generation ---------------- */
const genBtn = $("genBtn"), errBox = $("errBox"), cancelBtn = $("cancelBtn");
const emptyHint = $("emptyHint"), loadingBox = $("loadingBox"), resultBody = $("resultBody");
let elapsedTimer = null, genStart = 0;
let hasResult = false;
let curJobId = "";            // active inference job id (for cancel)
let curJobState = "";         // queued | running | stopping

function setResultVisible(visible) {
  // 结束时清空 inline display，让 CSS 接管（宽屏下 #resultBody 是 flex 纵向容器，
  // 写死 "block" 会把 CSS 的 display:flex 覆盖掉，编辑面板就压不到底部）
  resultBody.style.display = visible ? "" : "none";
  emptyHint.style.display = visible ? "none" : "";
}

function showLoading(stage, cancellable = true) {
  setResultVisible(false);
  // 空态占位要一起收起来：它和进度面板都是 flex:1，留着会让「还没有生成的音频」
  // 与进度条上下堆在一张卡里，候选条也被顶到卡片最底部。
  emptyHint.style.display = "none";
  loadingBox.hidden = false;
  $("pStage").textContent = stage;
  errBox.classList.remove("on");
  cancelBtn.hidden = !cancellable;
}

const PROG_DESC = {
  "starting inference...": "启动推理",
  "reference audio processing...": "处理参考音频…",
  "emotion analysis...": "情感分析（Qwen）…",
  "text processing...": "文本分词与分段",
  "saving audio...": "解码合成音频",
};
function transDesc(d) {
  if (!d) return "";
  // 扩散步数标记：desc 形如 "speech synthesis 1/2...|s2mel|12/25"
  const mDiff = /\|s2mel\|(\d+)\/(\d+)/.exec(d);
  if (mDiff) {
    const mSeg = /speech synthesis (\d+)\/(\d+)/.exec(d);
    const segText = mSeg ? `语音合成 ${mSeg[1]}/${mSeg[2]} 段` : "";
    return `${segText ? segText + " · " : ""}扩散 ${mDiff[1]}/${mDiff[2]}`;
  }
  if (d.includes("|")) d = d.split("|")[0];
  if (PROG_DESC[d]) return PROG_DESC[d];
  const m = /speech synthesis (\d+)\/(\d+)\.\.\./.exec(d);
  if (m) return `语音合成 ${m[1]}/${m[2]} 段`;
  return d;
}

const stepEls = Array.from(document.querySelectorAll(".pstep"));
const stepState = [0, 0, 0, 0, 0];
function markStep(i, s) {
  const rank = { waiting: 0, running: 1, done: 2 };
  if (s === "running" && stepState[i] === 2) stepState[i] = 1;
  else if (rank[s] <= stepState[i]) return;
  else stepState[i] = rank[s];
  const el = stepEls[i];
  if (el) { el.classList.remove("waiting", "running", "done"); el.classList.add(s); }
}
function resetSteps() {
  for (let i = 0; i < 5; i++) { stepState[i] = 0; stepEls[i].classList.remove("running", "done"); }
}

function startElapsed() {
  genAnim.on = true; genAnim.level = 0.5; genAnim.progress = 0;
  cancelAnimationFrame(waveRAF);
  waveRAF = requestAnimationFrame(waveLoop);
  clearInterval(elapsedTimer);
  elapsedTimer = setInterval(() => {
    $("pElapsed").textContent = `${((performance.now() - genStart) / 1000).toFixed(1)}s`;
  }, 100);
}
/* 整轮生成是否进行中（候选数 ≥2 时一轮里有多条）。
   stopUi() 在 showError / showStopped 内部也会被调到：如果它无条件解锁 genBtn，
   第 1 条失败时用户就能在循环跑到第 2 条之前再点一次生成，直接起第二个循环。
   所以循环期间 stopUi 只收尾计时器/动画，genBtn 的解锁留给整轮结束。 */
let candRunning = false;
function stopUi() {
  genBtn.disabled = candRunning;
  clearInterval(elapsedTimer);
  cancelAnimationFrame(waveRAF);
  waveRAF = 0;
  genAnim.on = false;
  cancelBtn.hidden = true;
  curJobId = "";
  curJobState = "";
  drawHeaderWave(0);
}

function updateCancelUi() {
  if (!curJobId) return;
  if (curJobState === "queued") {
    cancelBtn.textContent = "取消排队";
    cancelBtn.disabled = false;
  } else if (curJobState === "cancelling") {
    cancelBtn.textContent = "正在取消…";
    cancelBtn.disabled = true;
  } else if (curJobState === "stopping") {
    cancelBtn.textContent = "正在停止…";
    cancelBtn.disabled = true;
  } else {
    cancelBtn.textContent = "停止生成";
    cancelBtn.disabled = false;
  }
}
function showError(msg) {
  errBox.textContent = msg;
  errBox.classList.add("on");
  loadingBox.hidden = true;
  setResultVisible(hasResult);
  stopUi();
}

/* 只清与本次操作对应的前置校验错误：生成失败、OOM 等其它红色提示不能误清。 */
function clearPrecheckError(msg) {
  if (errBox.classList.contains("on") && errBox.textContent === msg) {
    errBox.textContent = "";
    errBox.classList.remove("on");
  }
}

function showActionError(msg) {
  const hint = $("loadHint");
  hint.textContent = msg;
  hint.classList.add("error");
}

function showActionHint(msg) {
  const hint = $("loadHint");
  hint.textContent = msg;
  hint.classList.remove("error");
}

/* 用户主动停止生成：不是故障。
   红色错误框只该留给真的失败（OOM、文件读不出来、模型加载失败）。停止是用户
   自己点的，用中性提示，否则每次正常停止都像出了故障。 */
function showStopped(msg) {
  showActionHint(msg || "已停止生成");
  errBox.classList.remove("on");      // 清掉可能残留的红色错误框
  errBox.textContent = "";
  loadingBox.hidden = true;
  setResultVisible(hasResult);
  stopUi();
}

function clearActionError() {
  const hint = $("loadHint");
  hint.textContent = "";
  hint.classList.remove("error");
}

/* 取消生成：排队中的任务直接取消（不浪费算力）；运行中的任务无法安全中断，
   由用户确认后仅放弃等待，推理在后台继续跑完并计入历史。 */
cancelBtn.addEventListener("click", async () => {
  if (!curJobId) return;
  const jobId = curJobId;
  cancelBtn.disabled = true;
  try {
    if (curJobState === "running") {
      const stop = await uiConfirm({
        title: "停止生成",
        body: (candRunning && candTotal > 1)
          ? `将中断当前这条推理（第 ${candActive + 1}/${candTotal} 条），这条不会保存；已完成的 ${candItems.length} 条会保留。模型保持加载，下次生成可直接继续。`
          : "将中断当前推理，本次已生成的部分不会保存。模型保持加载，下次生成可直接继续。",
        okText: "停止生成",
        cancelText: "继续等待",
        danger: true,
      });
      if (!stop) {
        updateCancelUi();
        return;
      }
    }
    // keep_model=1：停止后不重建模型。重建要十几秒，而停止只发生在阶段边界——
    // 参考缓存在 0.05 阶段一次写完（连续 5 条赋值、中间不回调），per-run 标志
    // 在每次推理开头重置，输出 wav 要全部段落跑完才落盘——停下来时没有半成品
    // 状态。实测 4 轮「停止 -> 再生成」显存收敛在 6760MB 上下（不升反降），
    // 输出音频时长/峰值/均值与基准同一量级。
    const r = await fetch(`${API}/tts/${jobId}/stop?keep_model=1`, { method: "POST" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || "停止失败");
    if (j.state === "cancelled") {
      // 排队任务已转 cancelled,worker 的终止事件马上就到(排队取消现在即时生效)
      curJobState = "cancelling";
    } else if (j.state === "stop_requested" || j.state === "running") {
      curJobState = "stopping";
      showLoading("正在停止生成…");
    }
    updateCancelUi();
  } catch (e) {
    showActionError("停止失败: " + (e.message || e));
    updateCancelUi();
  } finally {
    if (curJobState !== "stopping") updateCancelUi();
  }
});

/* 前端文件校验：与后端 413/400 同口径，能在请求发出前就拦住（后端仍是权威校验）。
   基础白名单是 libsndfile 能直接读的格式（Ogg 容器里的 Vorbis/Opus 都算）；
   服务端装了 ffmpeg 时还能收 m4a/aac/wma（上传时转成 WAV），并且能把视频
   （mp4/mov/m4v/mkv/webm/avi/flv）里的音轨抽出来当参考音频。
   实际清单由 /status 的 audio_exts / video_exts 下发，后端是唯一事实来源。 */
const AUDIO_MAX_MB = 32;      // 兜底值；服务端 /status 的 audio_max_mb 优先
let audioMaxMB = AUDIO_MAX_MB;
const AUDIO_EXTS = [".wav", ".mp3", ".flac", ".ogg", ".opus"];
const AUDIO_MIMES = [
  "audio/wav", "audio/x-wav", "audio/wave", "audio/mpeg", "audio/mp3",
  "audio/flac", "audio/x-flac", "audio/ogg", "application/ogg", "audio/x-opus+ogg",
];
const AUDIO_EXTRA_EXTS = [".m4a", ".aac", ".wma"];
const AUDIO_EXTRA_MIMES = [
  "audio/mp4", "audio/x-m4a", "audio/aac", "audio/x-aac", "audio/x-ms-wma",
];
const VIDEO_EXTS = [".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".flv"];
const VIDEO_MIMES = [
  "video/mp4", "video/quicktime", "video/x-m4v", "video/x-matroska",
  "video/webm", "video/x-msvideo", "video/x-flv",
];
let serverFfmpeg = false;                 // 服务端有没有 ffmpeg
let supportedAudioExts = AUDIO_EXTS.slice();
let supportedAudioMimes = AUDIO_MIMES.slice();
let supportedVideoExts = [];
let videoMaxMB = 200;
let capabilityKey = "";

function audioFormatList() {
  return supportedAudioExts.map((e) => e.slice(1)).join("/");
}

function videoFormatList() {
  return supportedVideoExts.map((e) => e.slice(1)).join("/");
}

function isVideoFile(f) {
  if (!f) return false;
  const n = (f.name || "").toLowerCase();
  return VIDEO_EXTS.some((t) => n.endsWith(t))
    || VIDEO_MIMES.includes(f.type || "")
    || (f.type || "").startsWith("video/");
}

function audioHintText(kind) {
  const list = audioFormatList();
  const vid = supportedVideoExts.length ? "；视频自动提取音轨" : "";
  return kind === "emo"
    ? `支持 ${list}${vid} · 表达目标情绪的片段`
    : `支持 ${list}${vid} · 约 3~10 秒最佳`;
}

function defaultSpkName() {
  return supportedVideoExts.length ? "点击选择或拖入音频 / 视频文件" : "点击选择或拖入音频文件";
}

function updateAudioHint() {
  if ($("spkSub")) $("spkSub").textContent = audioHintText("spk");
  if ($("emoSub")) $("emoSub").textContent = audioHintText("emo");
  // 空选区时才改占位文案：已经选好文件的名字不能被能力刷新覆盖
  if ($("spkDrop") && !$("spkDrop").hidden) $("spkName").textContent = defaultSpkName();
}

/* 能力变化时才改 DOM（accept + 提示文案）；已经选进来的文件不动，
   避免用户刚选完就被清空。 */
function applyAudioCapability(status) {
  serverFfmpeg = !!status.ffmpeg;
  const raw = Array.isArray(status.audio_exts) ? status.audio_exts : null;
  const exts = raw && raw.length
    ? raw.filter((e) => typeof e === "string" && e.startsWith("."))
    : AUDIO_EXTS.concat(serverFfmpeg ? AUDIO_EXTRA_EXTS : []);
  const rawVid = Array.isArray(status.video_exts) ? status.video_exts : null;
  const vids = rawVid
    ? rawVid.filter((e) => typeof e === "string" && e.startsWith("."))
    : (serverFfmpeg ? VIDEO_EXTS.slice() : []);
  const maxMB = Number(status.video_max_mb) > 0 ? Number(status.video_max_mb) : 200;
  const amax = Number(status.audio_max_mb) > 0 ? Number(status.audio_max_mb) : AUDIO_MAX_MB;
  const key = `${exts.join(",")}|${vids.join(",")}|${maxMB}|${amax}`;
  if (key === capabilityKey) return;
  capabilityKey = key;
  supportedAudioExts = exts;
  supportedAudioMimes = AUDIO_MIMES.concat(serverFfmpeg ? AUDIO_EXTRA_MIMES : []);
  supportedVideoExts = vids;
  videoMaxMB = maxMB;
  audioMaxMB = amax;
  const accept = supportedAudioExts.concat(supportedAudioMimes)
    .concat(supportedVideoExts, supportedVideoExts.length ? VIDEO_MIMES : []).join(",");
  for (const id of ["spkFile", "emoFile"]) {
    const el = $(id);
    if (el) el.accept = accept;
  }
  updateAudioHint();
}

function validateMediaFile(file, label) {
  if (!file) return "";
  const video = isVideoFile(file);
  const cap = video ? videoMaxMB : audioMaxMB;
  if (file.size > cap * 1048576) {
    return `${label}过大（${(file.size / 1048576).toFixed(1)} MB，上限 ${cap} MB）`;
  }
  const n = (file.name || "").toLowerCase();
  if (video) {
    if (!supportedVideoExts.length) {
      return `${label}是视频，但服务端未启用音轨提取（需要 ffmpeg）`;
    }
    if (!supportedVideoExts.some((t) => n.endsWith(t)) && !VIDEO_MIMES.includes(file.type || "")) {
      return `${label}不是可识别的视频文件（支持 ${videoFormatList()}）`;
    }
    return "";
  }
  if (!supportedAudioExts.some((t) => n.endsWith(t)) && !supportedAudioMimes.includes(file.type || "")) {
    return `${label}不是可识别的音频文件（支持 ${audioFormatList()}）`;
  }
  return "";
}

/* 视频 → WAV：服务端抽成 22050Hz 单声道，正好是推理侧 librosa 的读取格式。
   seq 不匹配（用户又选了一个文件）时丢弃结果，连错误提示都不写。 */
async function extractVideoAudio(f, seq, kind) {
  const subEl = $(kind === "spk" ? "spkSub" : "emoSub");
  const current = () => seq === mediaPickSeq[kind];
  try {
    const fd = new FormData();
    fd.append("file", f);
    const res = await fetch(`${API}/audio/extract`, { method: "POST", body: fd });
    if (!current()) return null;
    if (!res.ok) {
      let msg = `提取视频音轨失败（HTTP ${res.status}）`;
      try {
        const j = await res.json();
        if (j && typeof j.detail === "string" && j.detail) msg = j.detail;
      } catch (e) { /* 非 JSON 错误体 */ }
      subEl.textContent = msg;
      return null;
    }
    const blob = await res.blob();
    if (!current()) return null;
    const base = (f.name || "视频").replace(/\.[^.]+$/, "") || "视频音轨";
    return new File([blob], `${base}.wav`, { type: "audio/wav" });
  } catch (e) {
    if (current()) subEl.textContent = `提取视频音轨失败：${e.message || e}`;
    return null;
  }
}

/* 浏览器解码失败时的服务端兜底。只对「服务端多收的那三种」（m4a/aac/wma）
   尝试：基础格式本地都解不了，通常意味着文件损坏，转码也救不回来。
   返回 null 表示不该走兜底；抛错表示兜底也失败（调用方保留上一份选择）。 */
async function transcodeForBrowser(f) {
  if (!serverFfmpeg) return null;
  const n = (f.name || "").toLowerCase();
  if (!AUDIO_EXTRA_EXTS.some((t) => n.endsWith(t))) return null;
  $("spkSub").textContent = "浏览器不支持该格式，正在用服务端转码…";
  const fd = new FormData();
  fd.append("file", f);
  const res = await fetch(`${API}/audio/extract`, { method: "POST", body: fd });
  if (!res.ok) {
    let msg = `转码失败（HTTP ${res.status}）`;
    try {
      const j = await res.json();
      if (j && typeof j.detail === "string" && j.detail) msg = j.detail;
    } catch (e) { /* 非 JSON 错误体 */ }
    throw new Error(msg);
  }
  const blob = await res.blob();
  const base = (f.name || "参考音频").replace(/\.[^.]+$/, "") || "参考音频";
  return new File([blob], `${base}.wav`, { type: "audio/wav" });
}

function validateAudioFile(file, label) {
  if (!file) return "";
  if (file.size > audioMaxMB * 1048576) {
    return `${label}过大（${(file.size / 1048576).toFixed(1)} MB，上限 ${audioMaxMB} MB）`;
  }
  const n = (file.name || "").toLowerCase();
  if (!supportedAudioExts.some((t) => n.endsWith(t)) && !supportedAudioMimes.includes(file.type || "")) {
    return `${label}不是可识别的音频文件（支持 ${audioFormatList()}）`;
  }
  return "";
}

/* ---------------- 多候选生成（F3） ----------------
   同一段文本串行跑 N 次（N=1/2/3），每条都是一次正常 /tts：各自落一个文件、
   各进一条历史。严格串行——8GB 卡上同一时刻只跑一个推理，显存峰值与单条一致。
   候选条是 #resultBody 的兄弟节点，独立显隐（见 index.html 注释）。 */
const CAND_LABELS = ["①", "②", "③"];
let candN = 1;                 // 1 | 2 | 3；刻意不持久化，刷新回到 1（决策 8B）
let candGroup = "";            // 本轮候选组 id（N=1 时为空）
let candTotal = 1;             // 当前展示的候选条总数
let candItems = [];            // 成功的候选 {index,url,file,elapsed,historyId,melCapped,normalizeFallback,duration,missing}
let candErrors = [];           // 失败的候选 {index,message}
let candAdopted = -1;          // 已采用的下标
let candActive = -1;           // 正在生成的下标（占位卡显示「生成中…」）
let candPlayIndex = -1;        // 候选试听当前下标
let candChainOn = false;
let candRound = 0;             // 换轮/切组计数：让过期的 metadata 回调失效
let lastFd = null;             // 重试这条用的请求体（FormData 可重复发）
const candAudio = $("candAudio");

function uid8() {
  // crypto.randomUUID 在 http://192.168.x.x 这类非安全上下文里是 undefined
  const b = new Uint8Array(4);
  if (window.crypto && window.crypto.getRandomValues) window.crypto.getRandomValues(b);
  else for (let i = 0; i < 4; i++) b[i] = Math.floor(Math.random() * 256);
  return [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
}

function syncCandSeg() {
  const seg = $("candSeg");
  if (!seg) return;
  seg.querySelectorAll("button[data-cand]").forEach((b) => {
    const on = Number(b.dataset.cand) === candN;
    b.setAttribute("aria-pressed", String(on));
    b.tabIndex = on ? 0 : -1;
  });
}
function setCandN(n) {
  candN = n === 2 || n === 3 ? n : 1;
  syncCandSeg();
}

/* 单条预计达到 10 分钟就算长文：3 条候选合计 ≈ 30 分钟起，逐条试听也费时。
   估算优先复用分句预览的真实 token 数；预览还没回来时用同一套经验公式兜底。 */
const LONG_TEXT_CAND_SEC = 600;
function estimateGenSeconds(text, segTok) {
  if (segEst && segEst.text === text && segEst.segTokens === String(segTok)) return segEst.estSec;
  const chars = textUnits(text);
  const toks = Math.ceil(chars * 1.9);
  const segs = Math.max(1, Math.ceil(toks / Math.max(1, Number(segTok) || 120)));
  return Math.round(segs * 4 + toks * 0.06);
}
$("candSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-cand]");
  if (!b) return;
  setCandN(Number(b.dataset.cand));
});
$("candSeg").addEventListener("keydown", (e) => {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  e.preventDefault();
  setCandN(candN === 1 ? 2 : candN === 2 ? 3 : 1);
  const cur = $("candSeg").querySelector(`button[data-cand="${candN}"]`);
  if (cur) cur.focus();
});
syncCandSeg();

function candItem(i) { return candItems.find((x) => x.index === i) || null; }
function candFmtDur(d) { return Number.isFinite(d) && d > 0 ? `${d.toFixed(2)}s` : "—"; }

function setChain(on) {
  candChainOn = !!on;
  const box = $("candChain");
  if (box) box.checked = candChainOn;
}

function syncCandPlayIcons() {
  document.querySelectorAll("#candGrid .cand-play").forEach((b) => {
    const i = Number(b.dataset.i);
    const on = candPlayIndex >= 0 && !candAudio.paused && i === candPlayIndex;
    b.innerHTML = `<svg class="ico" aria-hidden="true"><use href="#${on ? "i-pause" : "i-play"}"></use></svg>`;
    b.setAttribute("aria-label", `${on ? "暂停" : "试听"}候选 ${i + 1}`);
  });
}

function renderCandidates() {
  const show = candTotal > 1;
  $("candStrip").hidden = !show;
  if (!show) { $("candGrid").replaceChildren(); return; }
  const ok = candItems.length;
  const bad = candErrors.length;
  $("candTitle").textContent = `${candTotal} 条候选 · 点 ▶ 试听，点卡片采用`;
  $("candFoot").textContent = candRunning
    ? `正在串行生成，已完成 ${ok}/${candTotal} 条${bad ? ` · 失败 ${bad} 条` : ""}`
    : `${ok} 条已存进右侧历史，随时可回看${bad ? ` · ${bad} 条失败，可单独重试` : ""}`;
  const grid = $("candGrid");
  // 重建会换掉 DOM 节点。键盘焦点原本落在哪张卡上，重建后要回到同一张：
  // 否则按 Enter 采用后焦点掉回 body，再想切下一条就得从页面开头 Tab 一遍。
  const focusIdx = grid.contains(document.activeElement)
    ? document.activeElement.closest(".cand-card")?.dataset.i
    : undefined;
  grid.replaceChildren();
  for (let i = 0; i < candTotal; i++) grid.appendChild(candCard(i));
  syncCandPlayIcons();
  if (focusIdx !== undefined) {
    const again = grid.querySelector(`.cand-card[data-i="${focusIdx}"]`);
    if (again && again.tabIndex >= 0) again.focus();
  }
}

function candCard(i) {
  const card = document.createElement("div");
  const item = candItem(i);
  const err = candErrors.find((e) => e.index === i);
  card.className = "cand-card";
  card.dataset.i = String(i);
  if (candAdopted === i) card.classList.add("on");
  if (item && item.missing) card.classList.add("missing");
  if (err) card.classList.add("bad");

  const dur = item ? candFmtDur(item.duration) : (err ? "失败" : "—");
  const meta = item
    ? `生成 ${Number(item.elapsed || 0).toFixed(1)}s · ${item.missing ? "文件已清理" : "已存进历史"}`
    : err
      ? err.message
      : candActive === i
        ? "生成中…"
        : candRunning
          ? `等待生成 ${i + 1}/${candTotal}`
          : "本次未生成";
  let state;
  if (item && candAdopted === i) {
    state = `<span class="cand-check"><svg class="ico" aria-hidden="true"><use href="#i-check"></use></svg>已采用</span>`;
  } else if (item && item.missing) {
    state = `<span class="cand-dead">文件已清理，无法试听/采用</span>`;
  } else if (item) {
    state = `<span class="cand-cta">采用</span>`;
  } else if (err) {
    state = `<button class="linkbtn cand-retry" type="button">重试这条</button>`;
  } else {
    state = `<span class="cand-wait">${candActive === i ? "进行中" : "等待"}</span>`;
  }

  card.innerHTML = `
    <div class="cand-top">
      <span class="cand-no">${CAND_LABELS[i] || i + 1}</span>
      <span class="cand-dur mono">${dur}</span>
      <button class="cand-play" type="button" data-i="${i}" aria-label="试听候选 ${i + 1}" ${item && !item.missing ? "" : "disabled"}>
        <svg class="ico" aria-hidden="true"><use href="#i-play"></use></svg>
      </button>
    </div>
    <div class="cand-meta"></div>
    <div class="cand-state">${state}</div>`;
  // 服务端错误原文可能带引号/尖括号：一律用 textContent 写，别拼进 HTML
  const metaEl = card.querySelector(".cand-meta");
  metaEl.textContent = meta;
  metaEl.title = meta;

  if (item && !item.missing) {
    card.setAttribute("role", "button");
    card.tabIndex = 0;
    card.setAttribute("aria-pressed", String(candAdopted === i));
    card.setAttribute("aria-label", `采用候选 ${i + 1}`);
    card.addEventListener("click", (e) => {
      if (e.target.closest(".cand-play")) return;
      adoptCandidate(i);
    });
    card.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") return;
      e.preventDefault();
      adoptCandidate(i);
    });
  } else {
    card.tabIndex = -1;
  }
  const playBtn = card.querySelector(".cand-play");
  if (playBtn) {
    playBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleCandPlay(i); });
  }
  const retry = card.querySelector(".cand-retry");
  if (retry) retry.addEventListener("click", (e) => { e.stopPropagation(); retryCandidate(i); });
  return card;
}

/* 时长只能从音频 metadata 读：done 事件里没有 duration（PLAN §4.2）。
   3 秒读不出来就显示「—」，不阻塞试听/采用。 */
function readCandDuration(index) {
  const item = candItem(index);
  if (!item || !item.url) return;
  const round = candRound;
  const a = new Audio();
  let settled = false;
  const done = (d) => {
    if (settled) return;
    settled = true;
    if (round !== candRound) return;          // 已经换轮：别把旧时长写到新卡片上
    const cur = candItem(index);
    if (cur) cur.duration = d;
    // 只改这一张卡的时长文字：整条重渲染会让键盘焦点从按钮上掉下来
    const cards = $("candGrid").querySelectorAll(".cand-card");
    const card = cards[index];
    const durEl = card && card.querySelector(".cand-dur");
    if (durEl) durEl.textContent = candFmtDur(d);
  };
  const timer = setTimeout(() => done(null), 3000);
  a.addEventListener("loadedmetadata", () => { clearTimeout(timer); done(Number.isFinite(a.duration) ? a.duration : null); }, { once: true });
  a.addEventListener("error", () => { clearTimeout(timer); done(null); }, { once: true });
  a.preload = "metadata";
  a.src = item.url;
}

function clearCandidates() {
  candRound++;
  candGroup = "";
  candTotal = 1;
  candItems = [];
  candErrors = [];
  candAdopted = -1;
  candActive = -1;
  stopCandPlay();
  setChain(false);
  renderCandidates();
}

async function adoptCandidate(index, opts = {}) {
  const item = candItem(index);
  if (!item || item.missing || candAdopted === index) return false;
  // 决策 4：切候选会重置剪辑状态，有编辑时先确认。自动采用（整轮结束）不弹——
  // 那与改动前「生成完直接换结果」一致，多一次确认反而挡路。
  if (!opts.auto && ED.hasOps()) {
    const ok = await uiConfirm({
      title: "切换候选",
      body: "当前有未保存的剪辑（裁剪/音量/变速），切换到另一条候选会丢弃这些编辑。继续吗？",
      okText: "切换",
      cancelText: "留在当前",
      danger: true,
    });
    if (!ok) return false;
  }
  stopCandPlay();
  setChain(false);
  histCurId = item.historyId || "";
  const loaded = await loadResult(item.url, item.elapsed, item.file, {
    melCapped: item.melCapped,
    normalizeFallback: item.normalizeFallback,
  });
  if (!loaded) {
    // 文件被 20 条上限/14 天清理掉：只标这一张卡，其它候选照常可用
    item.missing = true;
    renderCandidates();
    return false;
  }
  candAdopted = index;
  if (trim.buffer) item.duration = trim.buffer.duration;
  renderCandidates();
  renderHistory();
  return true;
}

function stopCandPlay() {
  candPlayIndex = -1;
  candAudio.pause();
  candAudio.removeAttribute("src");
  candAudio.dataset.index = "";
  try { candAudio.load(); } catch (e) { /* 某些浏览器在未设 src 时 load 会抛 */ }
  syncCandPlayIcons();
}

function playCand(index) {
  const item = candItem(index);
  if (!item || item.missing) return;
  candPlayIndex = index;
  candAudio.dataset.index = String(index);
  candAudio.src = item.url;
  candAudio.play().catch((e) => {
    // 连点两条候选时，上一次 play() 会被新的 src 打断并以 AbortError 收尾。
    // 那是「被后来者顶掉」，不是文件坏了：当成失败会把一张好好的候选标成已清理。
    if (e && e.name === "AbortError") return;
    if (candPlayIndex !== index) return;     // 已经切到别的候选，这张卡不该被改
    if (e && e.name === "NotAllowedError") {
      // 只有非手势路径才会走到这里；文件没问题，别标死
      showActionError("浏览器拦截了自动播放，请再点一次 ▶ 试听");
      return;
    }
    item.missing = true;
    renderCandidates();
    showActionError(`候选 ${index + 1} 试听失败：${e.message || "无法播放"}`);
  });
}

function nextCandIndex(from) {
  for (let j = Math.max(0, from); j < candTotal; j++) {
    const it = candItem(j);
    if (it && !it.missing) return j;
  }
  return -1;
}

function toggleCandPlay(index) {
  const item = candItem(index);
  if (!item || item.missing) return;
  const same = candPlayIndex === index && !candAudio.paused;
  setChain(false);                 // 手动点 ▶ 视为接管播放，退出连播
  if (same) { candAudio.pause(); return; }
  playCand(index);
}

$("candChain").addEventListener("change", (e) => {
  candChainOn = e.target.checked;
  if (!candChainOn) return;        // 关掉只停止「继续播下一条」，不打断当前这条
  if (candTotal <= 1) { setChain(false); return; }
  const start = candPlayIndex >= 0 ? candPlayIndex : 0;
  if (candAudio.paused) {
    const j = nextCandIndex(start);
    if (j >= 0) playCand(j);
    else setChain(false);
  }
});

candAudio.addEventListener("play", () => {
  // 与主播放器 / 剪辑结果互斥：同一时刻只有一路出声。
  // 这里用 $("player") 而不是 player：player 常量在文件更下方才初始化，
  // 模块顶层注册监听时直接引用会撞上 TDZ。
  const p = $("player");
  if (p && !p.paused) p.pause();
  const oa = $("outAudio");
  if (oa && !oa.paused) oa.pause();
  candPlayIndex = Number(candAudio.dataset.index);
  syncCandPlayIcons();
});
candAudio.addEventListener("pause", syncCandPlayIcons);
candAudio.addEventListener("ended", () => {
  const from = candPlayIndex + 1;
  candPlayIndex = -1;
  syncCandPlayIcons();
  if (!candChainOn) return;
  const j = nextCandIndex(from);
  if (j >= 0) playCand(j);
  else setChain(false);            // 播完自动停
});
candAudio.addEventListener("error", () => {
  if (!candAudio.getAttribute("src")) return;
  const i = candPlayIndex;
  const item = i >= 0 ? candItem(i) : null;
  if (item) { item.missing = true; renderCandidates(); }
  showActionError("候选试听失败：文件可能已被清理");
});
$("player").addEventListener("play", () => { if (!candAudio.paused) candAudio.pause(); });
const outAudioEl = $("outAudio");
if (outAudioEl) outAudioEl.addEventListener("play", () => { if (!candAudio.paused) candAudio.pause(); });

/* 从历史重建候选条：刷新后 / 点历史里带 cand_group 的条目时走这里。
   只恢复已经落盘的那几条，不补跑未生成的候选（PLAN §4.3-5）。 */
/* 恢复入口：刷新后从 /jobs/current 拿回的 done 结果里带 cand_group 就重建候选条 */
async function restoreCandidates(result) {
  if (result && result.cand_group) await rebuildCandFromHistory(result.cand_group, result.history_id);
  else clearCandidates();
}

async function rebuildCandFromHistory(group, currentId) {
  if (!group) { clearCandidates(); return; }
  try {
    const r = await (await apiFetch("/history")).json();
    const rows = (r.items || []).filter((it) => it.cand_group === group);
    if (rows.length < 2) { clearCandidates(); return; }
    rows.sort((a, b) => (Number(a.cand_index) || 0) - (Number(b.cand_index) || 0));
    candRound++;
    candGroup = group;
    candTotal = Math.min(3, Math.max(rows.length, ...rows.map((it) => Number(it.cand_total) || rows.length)));
    candItems = rows.map((it) => ({
      index: Number(it.cand_index) || 0,
      url: it.url,
      file: it.file,
      elapsed: it.elapsed,
      historyId: it.id,
      melCapped: false,
      normalizeFallback: null,
      duration: null,
      missing: !!it.missing,
    }));
    candErrors = [];
    candAdopted = candItems.findIndex((x) => x.historyId === currentId);
    renderCandidates();
    candItems.forEach((it) => readCandDuration(it.index));
  } catch (e) {
    clearCandidates();
  }
}

async function retryCandidate(index) {
  const err = candErrors.find((e) => e.index === index);
  if (!err || !lastFd || candRunning || genBtn.disabled) return;
  candRunning = true;
  candActive = index;
  genBtn.disabled = true;
  clearActionError();
  resetSteps();
  renderCandidates();
  try {
    const res = await runOneGeneration(lastFd, index, candTotal);
    candErrors = candErrors.filter((e) => e.index !== index);
    candItems.push({ ...res, index });
    renderCandidates();
    readCandDuration(index);
    if (candAdopted < 0) await adoptCandidate(index, { auto: true });
    await refreshHistory();
  } catch (e) {
    err.message = e.message || "生成失败";
    renderCandidates();
    showActionError(`候选 ${index + 1} 重试失败：${err.message}`);
  } finally {
    candActive = -1;
    candRunning = false;
    stopUi();
    if (hasResult) { loadingBox.hidden = true; setResultVisible(true); }
  }
}

/* 单条候选：建请求 → 读 SSE → 返回终态数据。循环状态（genBtn/计时器/
   loadResult）全部留在外面，所以这里只管「跑一次」。 */
async function runOneGeneration(fd, index, total) {
  if (total > 1) {
    // 同一条 FormData 可重复发：append 候选字段不会污染已写好的参数
    fd.set("cand_group", candGroup);
    fd.set("cand_index", String(index));
    fd.set("cand_total", String(total));
  }
  const prefix = total > 1 ? `候选 ${index + 1}/${total} · ` : "";
  const fold = (v) => Math.max(0, Math.min(1, (index + (v || 0)) / total)) * 100;
  // 不能用 genBtn.disabled 判定连接中断：stopUi() 延后到整轮结束，第 1 条成功时
  // 按钮仍是禁用的，会把每次成功都误报成「连接中断」。
  let sawTerminal = false;
  showLoading(prefix + (modelReady ? "准备生成…" : "首次生成，正在加载模型…（约 30~60 秒）"));
  try {
    // 整个生成期间连接都开着，故意不挂 AbortController：取消必须由服务端 /stop
    // 收尾（要停推理、重建或卸载模型），本地 abort 只会卡在「正在停止生成…」。
    const r = await fetch(`${API}/tts`, { method: "POST", body: fd });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || `请求失败（${r.status}）`);
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let loadingModel = false;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const dl = block.split("\n").find((l) => l.startsWith("data:"));
        if (!dl) continue;
        let ev;
        try { ev = JSON.parse(dl.slice(5).trim()); } catch (e) { continue; }
        if (ev.type === "started") {
          curJobId = ev.job_id || "";
          curJobState = "running";
          updateCancelUi();
        } else if (ev.type === "queue") {
          curJobState = "queued";
          updateCancelUi();
          showLoading(`${prefix}排队中… 前面还有 ${ev.ahead} 个任务`);
          $("pStageMeta").textContent = `排队 #${ev.ahead + 1}`;
        } else if (ev.type === "load") {
          curJobState = "running";
          updateCancelUi();
          loadingModel = true;
          markStep(0, "running");
          showLoading(`${prefix}正在加载模型…首次调用需 30~60 秒`);
          $("pbarFill").style.width = `${fold(0.08)}%`;
        } else if (ev.type === "progress") {
          if (curJobState === "queued") {
            curJobState = "running";
            updateCancelUi();
          }
          if (loadingModel) { loadingModel = false; $("pbarFill").style.width = "0%"; }
          markStep(0, "done");
          const pStage = String(ev.desc || "").split("|")[1] || "";
          // qwen：情感文本模式的 Qwen 情感分析阶段（可能含首次加载模型的额外耗时）
          if (pStage === "qwen") markStep(1, "running");
          if (pStage === "ref" || (ev.value || 0) < 0.1) markStep(1, "running");
          if (pStage !== "ref" && (ev.value || 0) >= 0.1) markStep(1, "done");
          if (pStage === "gpt") markStep(2, "running");
          if (pStage === "s2mel") { markStep(2, "done"); markStep(3, "running"); }
          if (pStage === "bigvgan") { markStep(3, "done"); markStep(4, "running"); }
          // 进度按「第几条 + 本条进度」折算成整轮进度；单条时公式退化为今天的行为
          showLoading(prefix + (transDesc(ev.desc) || "正在生成语音…"));
          genAnim.level = Math.min(1, 0.4 + (ev.value || 0));
          genAnim.progress = ev.value || 0;
          $("pbarFill").style.width = `${Math.round(fold(ev.value || 0))}%`;
          // s2mel 阶段优先显示扩散步数，其余阶段显示全局百分比
          const mDiff = /\|s2mel\|(\d+)\/(\d+)/.exec(String(ev.desc || ""));
          $("pStageMeta").textContent = mDiff
            ? `扩散 ${mDiff[1]}/${mDiff[2]}`
            : `${Math.round((ev.value || 0) * 100)}%`;
        } else if (ev.type === "done") {
          sawTerminal = true;
          for (let i = 0; i < 5; i++) markStep(i, "done");
          $("pbarFill").style.width = `${Math.round(fold(1))}%`;
          loadingBox.hidden = true;
          return {
            // 候选条统一按 item.url 取音频（历史重建路径也是 url），
            // 这里必须把 done 事件的 wav 映射成 url，否则三张卡都会去请求 /undefined
            url: ev.wav,
            file: ev.file,
            elapsed: ev.elapsed,
            historyId: ev.history_id || "",
            melCapped: !!ev.mel_capped,
            normalizeFallback: ev.normalize_fallback || null,
          };
        } else if (ev.type === "error") {
          sawTerminal = true;
          throw new Error(ev.detail || "生成失败");
        } else if (ev.type === "warn") {
          // 软提示：文本过长。不中断生成，只把「等多久/期间不能干别的/
          // 刷新不中断」讲清楚，免得用户按下按钮才知道要等半小时。
          showActionHint(ev.detail || "文本较长，生成会很慢");
        } else if (ev.type === "stopping") {
          curJobState = "stopping";
          updateCancelUi();
          showLoading(`${prefix}正在停止生成…`);
        } else if (ev.type === "stopped") {
          // 终态：用户主动停止。这里只抛带标记的错误，不弹提示——showStopped()
          // 内部会调 stopUi()，循环中会把 genBtn 提前解锁（见 stopUi 注释）。
          // 由外层在收尾后统一提示。
          sawTerminal = true;
          curJobState = "stopped";
          updateCancelUi();
          throw Object.assign(new Error(ev.detail || "已停止生成"), { stopped: true });
        }
      }
    }
    if (!sawTerminal) throw new Error("生成连接中断，请重试");
  } finally {
    if (total > 1) {
      fd.delete("cand_group"); fd.delete("cand_index"); fd.delete("cand_total");
    }
  }
}

genBtn.addEventListener("click", async () => {
  const text = $("text").value.trim();
  if (!text) { showError("请先输入要合成的文本"); return; }
  const spkSelected = $("spkFile").files[0];
  if (!spkSelected) { showError("请先选择音色参考音频"); return; }
  const mode = $("emoMode").value;
  if (mode === "1" && !$("emoFile").files[0]) { showError("情感参考音频模式下请先选择情感音频"); return; }
  if (mode === "3" && !$("emoText").value.trim()) { showError("请输入情感描述文本"); return; }
  const spkErr = validateAudioFile(spkSelected, "音色参考音频");
  if (spkErr) { showError(spkErr); return; }
  if (mode === "1") {
    const emoErr = validateAudioFile($("emoFile").files[0], "情感参考音频");
    if (emoErr) { showError(emoErr); return; }
  }
  // 超限值在发送前回夹：服务端也会夹，但界面上留 99、实际用 20 是两码事（BUG-006）
  const clampedParams = clampParamsForSend();

  let N = candN;
  let candFoldedHint = "";
  if (N > 1) {
    const textNow = $("text").value;
    const estSec = estimateGenSeconds(textNow, $("segTokens").value);
    if (estSec >= LONG_TEXT_CAND_SEC) {
      const chars = textUnits(textNow.trim());
      const oneMin = Math.max(1, Math.ceil(estSec / 60));
      const allMin = Math.max(oneMin, Math.ceil((estSec * N) / 60));
      const keepOne = await uiConfirm({
        title: "长文建议先生成一条",
        body: `当前文本约 ${chars} 字，单条预计约 ${oneMin} 分钟；${N} 条候选合计约 ${allMin} 分钟，长文逐条试听也更费时间。`,
        okText: "调整为 1 条",
        cancelText: `保持 ${N} 条`,
        countdown: 5,
        countdownText: (left) => `${left} 秒后自动调整为 1 条候选`,
      });
      if (keepOne) {
        setCandN(1);
        N = 1;
        candFoldedHint = "已按长文建议调整为 1 条候选";
      }
    }
  }
  genStart = performance.now();
  curJobId = "";
  player.pause();
  bigPlayIcon.innerHTML = PLAY2;
  clearActionError();
  const genHints = [];
  if (candFoldedHint) genHints.push(candFoldedHint);
  if (clampedParams.length) genHints.push(`参数超出可设范围，已按边界执行：${clampedParams.join("、")}`);
  if (genHints.length) showActionHint(genHints.join("；"));
  setResultVisible(false);
  errBox.classList.remove("on");
  // 新一轮：候选条归零重来（单条时直接隐藏）。组 id 只有 N>1 才生成。
  candRound++;
  candGroup = N > 1 ? uid8() : "";
  candTotal = N;
  candItems = [];
  candErrors = [];
  candAdopted = -1;
  candActive = -1;
  stopCandPlay();
  setChain(false);
  renderCandidates();
  showLoading(modelReady ? "准备生成…" : "首次生成，正在加载模型…（约 30~60 秒）");
  resetSteps();
  $("pbarFill").style.width = "0%";
  $("pStageMeta").textContent = "";
  startElapsed();
  saveDraft();

    const fd = new FormData();
  fd.append("client_id", CLIENT_ID);
  fd.append("spk_audio", spkSelected);
  fd.append("text", $("text").value);
  fd.append("emo_mode", mode);
  fd.append("emo_weight", $("emoWeight").value);
  // 只有模式 3 才读描述文本；其它模式下它是隐藏的旧值，不能拿它拦生成
  if (mode === "3") fd.append("emo_text", $("emoText").value);
  fd.append("use_random", $("emoRandom").checked);
  const ea = $("emoFile").files[0];
  if (mode === "1" && ea) fd.append("emo_audio", ea);
  if (mode === "2") vecInputs().forEach((s) => fd.append("vec" + (parseInt(s.dataset.i) + 1), s.value));
  fd.append("max_text_tokens_per_segment", $("segTokens").value);
  fd.append("do_sample", $("doSample").checked);
  fd.append("top_p", $("topP").value);
  fd.append("top_k", $("topK").value);
  fd.append("temperature", $("temperature").value);
  fd.append("length_penalty", $("lenPen").value);
  fd.append("num_beams", $("numBeams").value);
  fd.append("repetition_penalty", $("repPen").value);
  fd.append("max_mel_tokens", $("maxMel").value);
  // 段间停顿（毫秒）。只随本次生成走，不进预设——它是「这次怎么念」的偏好，
  // 存进预设会让同一份音色参数因预设不同而发出不同的声音，没道理。
  fd.append("interval_silence", $("gapMs").value);
  fd.append("file_naming", "title_time");
  lastFd = fd;

  // 串行循环：每条都是一次普通 /tts。单条时请求体/SSE 解析与改动前逐行等价，
  // 只是「采用」从 done 事件里挪到循环结束后统一做（PLAN §4.3-1）。
  let firstErr = null;
  let stopped = false;
  try {
    // 放进 try：请求体构造若抛异常，finally 里的 stopUi() 才会把按钮解开
    genBtn.disabled = true;
    candRunning = true;
    for (let i = 0; i < N; i++) {
      candActive = i;
      renderCandidates();
      resetSteps();                       // 否则第 2 条的步骤条还停在「全部完成」
      try {
        const res = await runOneGeneration(fd, i, N);
        candItems.push({ ...res, index: i, duration: null, missing: false });
        candErrors = candErrors.filter((e) => e.index !== i);
        renderCandidates();
        readCandDuration(i);
        await refreshHistory();           // 侧栏实时出现新条目
        pollStatus();
      } catch (e) {
        if (e.stopped) { stopped = true; break; }
        firstErr = firstErr || e;
        candErrors = candErrors.concat({ index: i, message: e.message || "生成失败" });
        renderCandidates();               // 该卡显示失败 + 「重试这条」，其余照常
      }
    }
  } finally {
    candActive = -1;
    candRunning = false;
    stopUi();                             // 整轮结束才解锁 genBtn / 停计时器
  }
  if (!candItems.length) {
    renderCandidates();
    if (stopped) showStopped("已停止生成");
    else showError(firstErr ? firstErr.message : "生成失败");
    pollStatus();
    return;
  }
  // 第 1 条失败时采用第一条成功的；自动采用不弹「切换会丢剪辑」的确认
  const firstOk = candItems.reduce((a, b) => (a.index <= b.index ? a : b));
  await adoptCandidate(firstOk.index, { auto: true });
  if (stopped) showActionHint(`已停止，保留已生成的 ${candItems.length} 条（当前这条不保存）`);
  pollStatus();
});

/* ---------------- result, trim & download ---------------- */
// trim.buffer 仅作"当前是否有结果音频"的镜像（剪辑状态本身在 web/edit.js 的 ED 里）
const trim = { buffer: null, drag: null };
const tWave = $("waveTrim"), tCtx = tWave.getContext("2d");
const player = $("player");
const bigPlay = $("bigPlay"), bigPlayIcon = $("bigPlayIcon");
const PLAY2 = '<path d="M8 5v14l11-7z"/>';
const PAUSE2 = '<path d="M6 5h4v14H6zM14 5h4v14h-4z"/>';
let curUrl = null;
let curServerFile = "";       // 服务端实际保存的文件名（下载时与它保持一致）

/* ---- 文本规范化退化提示：只打扰一次，之后降级成小铃铛 ----
   后端在 wetext 内部失败时会退回原文继续跑（音频能出来，但读音可能不准）。
   那是「无声的降级」，所以这里补一条提示。修完底层那些已知触发点之后，
   这条提示正常使用时几乎不该出现；真出现了说明确实有不对劲，值得打断一下。
   但也不该每次都弹——第一次完整展开，之后只留小铃铛，点击随时能展开。
   完整记录始终在 logs/server.log 里，和界面提示次数无关。 */
const NORM_SEEN_KEY = "tts.normalizeFallbackSeen";
function showNormFallbackNote(lang) {
  const el = $("normCapNote");
  if (!el) return;
  el.hidden = false;
  el.title = `本次退化的语种：${lang}`;
  let seen = false;
  try { seen = localStorage.getItem(NORM_SEEN_KEY) === "1"; } catch (e) { /* 隐私模式 */ }
  el.classList.toggle("quiet", seen);          // 已提示过 -> 小铃铛
  if (!seen) {
    try { localStorage.setItem(NORM_SEEN_KEY, "1"); } catch (e) { /* 存不下就每次都展开 */ }
  }
}
function hideNormFallbackNote() { const el = $("normCapNote"); if (el) el.hidden = true; }
(function () {
  const el = $("normCapNote");
  if (!el) return;
  const mini = el.querySelector(".capnote-mini");
  if (mini) mini.addEventListener("click", () => el.classList.remove("quiet"));
})();

async function loadResult(url, elapsed, serverFile, opts) {
  const { melCapped = false, normalizeFallback = null } = opts || {};
  $("melCapNote").hidden = !melCapped;
  if (normalizeFallback) showNormFallbackNote(normalizeFallback);
  else hideNormFallbackNote();
  curUrl = url;
  // 下载名与服务端 outputs/ 里的实际文件一致；文本改动不再影响已生成的结果名
  curServerFile = serverFile || (url || "").split("/").pop() || "";
  loadingBox.hidden = true;
  setResultVisible(true);
  clearActionError();
  // 换音频前必须摘掉上一次「播放选中」挂的 timeupdate：
  // 否则新音频播到上次选区的结束时间就被自动暂停，看起来像播放器坏了。
  if (selStopTick) { player.removeEventListener("timeupdate", selStopTick); selStopTick = null; }
  player.src = url;
  try {
    const buf = await (await fetch(url)).arrayBuffer();
    // 按文件采样率解码：升采样过冲会让峰值虚高 ~0.3dB，进而误报削波，见 decodeNative
    trim.buffer = await decodeNative(buf);
    ED.setSource(trim.buffer, url);          // 剪辑状态回到"无操作"，见 web/edit.js
  } catch (e) {
    // 失败原因不能只丢给用户看的那句兜底文案：控制台要留下 URL 与原始异常，
    // 否则「加载失败」在线上/log 里没有任何可追的线索（这次 /undefined 就差点被漏掉）。
    console.error("载入结果音频失败", url, e);
    trim.buffer = null;
    ED.clear();
    hasResult = false;
    player.pause();
    player.removeAttribute("src");
    player.load();
    setResultVisible(false);
    showError("生成结果音频加载失败，请重试");
    return false;
  }
  hasResult = true;
  $("trimRange").textContent = `全长 ${trim.buffer.duration.toFixed(2)}s`;
  // 「耗时」后面补上成品本身的时长与采样率：只写生成耗时，用户想知道这条音频
  // 几秒、什么规格就无从可查（采样率全站没有任何一处显示过）。
  $("genMeta").textContent =
    `耗时 ${Number(elapsed || 0).toFixed(1)}s · 成品 ${trim.buffer.duration.toFixed(2)}s`
    + ` / ${(trim.buffer.sampleRate / 1000).toFixed(1)}kHz`;
  syncAxis();
  updateDlName();
  drawTrimWave();
  return true;
}

// 时间轴两端：随音频总长更新，没有音频时显示占位符
function syncAxis() {
  const d = ED.src ? ED.src.duration : 0;
  $("axisStart").textContent = d ? "0.00s" : "—";
  $("axisEnd").textContent = d ? d.toFixed(2) + "s" : "—";
}

bigPlay.addEventListener("click", () => {
  if (player.paused) { player.play().catch(() => { bigPlayIcon.innerHTML = PLAY2; }); bigPlayIcon.innerHTML = PAUSE2; }
  else { player.pause(); bigPlayIcon.innerHTML = PLAY2; }
});
player.addEventListener("ended", () => { bigPlayIcon.innerHTML = PLAY2; });

/* 结果区源波形：跟左边参考音频同一种柱状条（3px 柱 + 2px 空隙）。
   原来是 1px 连续列，在 104px 这么宽的画布上读成一片密纹；柱状条既能看出
   「这是一段声音」，又保留了选区可读的边界。拖动定位用的是连续比例，
   跟柱宽无关，精度一点没丢。 */
const TRIM_BAR = { pitch: 5, w: 3, r: 1.5, amp: .84, floor: 2 };

function drawTrimWave() {
  if (!ED.src) return;
  const { w, h } = sizeCanvas(tWave);
  const data = ED.src.getChannelData(0);
  const B = TRIM_BAR;
  const mid = h / 2;
  const ink = cssVar("--accent"), mut = cssVar("--border"), pri = cssVar("--primary");
  // 波形恒画源音频（编辑台），选区与播放进度都是源时间轴。
  // 编辑结果有自己的播放器（#outAudio）和自己的波形，两者互不干扰。
  const dur = ED.src.duration;
  const pos = dur ? (player.currentTime / dur) * w : 0;
  const s = ED.getState().trim;
  tCtx.clearRect(0, 0, w, h);

  // 柱数由宽度决定，整组居中，避免右侧留一截空白
  const n = Math.max(1, Math.floor(w / B.pitch));
  const x0 = Math.round((w - n * B.pitch + B.w) / 2);
  const step = Math.max(1, Math.floor(data.length / n));

  for (let i = 0; i < n; i++) {
    const base = i * step;
    let peak = 0;
    for (let k = 0; k < step; k++) {
      const v = data[base + k] || 0;
      const a = v < 0 ? -v : v;
      if (a > peak) peak = a;
    }
    // 静音段留 2px 地板，否则整条断成一串点
    const bh = Math.max(B.floor, peak * mid * B.amp);
    const x = x0 + i * B.pitch;
    const p = n > 1 ? i / (n - 1) : 0;
    const inSel = !s || (p >= s[0] && p <= s[1]);
    const isPlayed = x <= pos;
    tCtx.fillStyle = isPlayed ? pri : (inSel ? ink : mut);
    tCtx.globalAlpha = isPlayed ? 0.95 : (inSel ? 0.9 : 0.55);
    if (tCtx.roundRect) {
      tCtx.beginPath();
      tCtx.roundRect(x, mid - bh, B.w, bh * 2, Math.min(B.r, bh));
      tCtx.fill();
    } else {
      tCtx.fillRect(x, mid - bh, B.w, bh * 2);
    }
  }
  tCtx.globalAlpha = 1;
  if (s) {
    tCtx.fillStyle = pri;
    tCtx.fillRect(s[0] * w - 1, 0, 2, h);
    tCtx.fillRect(s[1] * w - 1, 0, 2, h);
    tCtx.globalAlpha = 0.08;
    tCtx.fillRect(s[0] * w, 0, (s[1] - s[0]) * w, h);
    tCtx.globalAlpha = 1;
  }
}
player.addEventListener("timeupdate", () => { if (trim.buffer) drawTrimWave(); });

function xToFrac(e) {
  const r = tWave.getBoundingClientRect();
  return Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1);
}
tWave.addEventListener("pointerdown", (e) => {
  if (!ED.src) return;
  trim.drag = [xToFrac(e), xToFrac(e)];
  tWave.setPointerCapture(e.pointerId);
});
tWave.addEventListener("pointermove", (e) => {
  if (!trim.drag) return;
  trim.drag[1] = xToFrac(e);
  const sel = [Math.min(...trim.drag), Math.max(...trim.drag)];
  ED.setTrimLive(sel);          // 拖动中只更新状态与芯片，不重渲染音频
  drawTrimWave();
  const d = ED.src.duration;
  $("trimRange").textContent =
    `选中 ${(sel[0] * d).toFixed(2)}s – ${(sel[1] * d).toFixed(2)}s · ${((sel[1] - sel[0]) * d).toFixed(2)}s`;
});
tWave.addEventListener("pointerup", () => {
  if (!trim.drag) return;
  trim.drag = null;
  ED.setTrim(ED.getState().trim);   // 定稿才压快照：拖一次不该产生几十条撤销记录
  updateDlName();                   // 选区定稿后再刷新下载名（带 _trim 后缀）
});

let selStopTick = null;
// 从头播放 / 播放选中都作用在源播放器上（#player 永远挂源音频），与编辑结果无关
$("playFromStart").addEventListener("click", () => {
  if (!ED.src) return;
  if (selStopTick) { player.removeEventListener("timeupdate", selStopTick); selStopTick = null; }
  player.currentTime = 0;
  player.play().catch(() => { bigPlayIcon.innerHTML = PLAY2; });
  bigPlayIcon.innerHTML = PAUSE2;
});
$("playSel").addEventListener("click", () => {
  if (!ED.src) return;
  if (!player.paused) player.pause();
  if (selStopTick) { player.removeEventListener("timeupdate", selStopTick); selStopTick = null; }
  // #player 永远挂源音频，所以这里定位的是源时间轴上的选区，与编辑结果无关
  const sel = ED.getState().trim;
  const d = ED.src.duration;
  player.currentTime = (sel ? sel[0] : 0) * d;
  player.play().catch(() => { bigPlayIcon.innerHTML = PLAY2; });
  bigPlayIcon.innerHTML = PAUSE2;
  if (sel) {
    const stopAt = sel[1] * d;
    selStopTick = () => { if (player.currentTime >= stopAt) { player.pause(); player.removeEventListener("timeupdate", selStopTick); selStopTick = null; bigPlayIcon.innerHTML = PLAY2; } };
    player.addEventListener("timeupdate", selStopTick);
  }
});
$("resetSel").addEventListener("click", () => {
  // 清空选区（只动 trim，保留音量/变速）。文案与波形高亮由 syncTrimView 统一刷新
  ED.setTrim(null);
});

player.addEventListener("play", () => { bigPlayIcon.innerHTML = PAUSE2; });
player.addEventListener("pause", () => { bigPlayIcon.innerHTML = PLAY2; });

/* ---- 导出格式：WAV / MP3 ----
   格式开关是结果卡级别的设置，同时作用于「下载源音频」「下载编辑结果」「保存到目录」
   三个出口。默认 WAV（零行为变化），选择记在 localStorage。
   MP3 由 web/lame-1.2.1.min.js（lamejs 1.2.1，LGPL-3.0）在浏览器里编码，
   不依赖 ffmpeg、不占服务端 CPU；组件懒加载，首屏零影响。 */
const DL_FORMAT_KEY = "dlFormat";
let dlFormat = lsGet(DL_FORMAT_KEY) === "mp3" ? "mp3" : "wav";
let mp3State = "pending";      // pending | ready | unavailable

function fmtExt() { return dlFormat === "mp3" ? ".mp3" : ".wav"; }
function swapExt(name, ext) { return String(name || "").replace(/\.(wav|mp3)$/i, "") + ext; }

function syncFmtSeg() {
  const seg = $("fmtSeg");
  if (!seg) return;
  seg.querySelectorAll("button[data-fmt]").forEach((b) => {
    const on = b.dataset.fmt === dlFormat;
    b.setAttribute("aria-pressed", String(on));
    // 两个按钮共用一个 Tab 停靠点，组内用左右键切换（ARIA radio 组的惯例）
    b.tabIndex = on ? 0 : -1;
    if (b.dataset.fmt === "mp3") {
      b.disabled = mp3State === "unavailable";
      b.title = mp3State === "unavailable"
        ? "MP3 组件未加载成功（web/lame-1.2.1.min.js），请使用 WAV"
        : "MP3 160kbps，首次使用会加载编码组件";
    }
  });
}

function setDlFormat(fmt, persist = true) {
  dlFormat = fmt === "mp3" ? "mp3" : "wav";
  if (persist) {
    // 隐私模式下 localStorage 可能直接抛异常：存不下就只在本次会话生效
    try { localStorage.setItem(DL_FORMAT_KEY, dlFormat); } catch (e) { /* ignore */ }
  }
  syncFmtSeg();
  updateDlName();
}

function markMp3Ready() { mp3State = "ready"; syncFmtSeg(); }
function markMp3Unavailable() {
  mp3State = "unavailable";
  // 已选的 MP3 自动退回 WAV，避免按钮灰着却仍按 MP3 逻辑走进死路
  if (dlFormat === "mp3") setDlFormat("wav");
  syncFmtSeg();
}

let lamePromise = null;
function loadLameJs() {
  if (window.lamejs && window.lamejs.Mp3Encoder) return Promise.resolve(window.lamejs);
  if (!lamePromise) {
    lamePromise = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      // 经典脚本（function lamejs(){…} lamejs();），只能用 <script> 引入，
      // 不能 type="module"、也不能当 ESM 打包
      s.src = "/assets/lame-1.2.1.min.js";
      s.async = true;
      s.onload = () => (window.lamejs && window.lamejs.Mp3Encoder)
        ? resolve(window.lamejs)
        : reject(new Error("MP3 组件未定义"));
      s.onerror = () => reject(new Error("MP3 组件加载失败"));
      document.head.appendChild(s);
    }).catch((e) => {
      lamePromise = null;              // 允许下次点击重试（网络恢复后无需刷新页面）
      throw e;
    });
  }
  return lamePromise;
}

// 空闲时预热：只下文件，失败就把 MP3 按钮置灰。
// 必须写 window.requestIdleCallback：裸标识符做可选调用（requestIdleCallback?.()）
// 在没有该 API 的浏览器上会抛 ReferenceError，整个脚本当场中断。
const _preheat = window.requestIdleCallback || ((fn) => setTimeout(fn, 1500));
_preheat(() => loadLameJs().then(markMp3Ready, markMp3Unavailable));

const MP3_SAMPLE_RATES = [8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000];

/* 编码一段 AudioBuffer（可带选区比例 s0/s1）。分块进行，每约 2 秒音频让一次主线程，
   长音频不会把页面卡死；onProgress 收到 0..1。 */
async function encodeMp3(buffer, s0, s1, onProgress) {
  const L = await loadLameJs();
  const sr = buffer.sampleRate;
  if (!MP3_SAMPLE_RATES.includes(sr)) {
    throw new Error(`采样率 ${sr}Hz 不支持 MP3 编码`);
  }
  const ch = Math.min(buffer.numberOfChannels, 2);
  const enc = new L.Mp3Encoder(ch, sr, 160);   // 22.05k 属 MPEG-2，LAME 上限就是 160kbps
  const from = Math.floor((s0 || 0) * buffer.length);
  const to = Math.min(buffer.length, Math.floor((s1 ?? 1) * buffer.length));
  if (to <= from) throw new Error("没有可编码的音频片段");
  const lc = buffer.getChannelData(0);
  const rc = ch === 2 ? buffer.getChannelData(1) : null;
  const parts = [];
  const FRAME = 1152;
  const total = to - from;
  for (let i = from; i < to; i += FRAME) {
    const n = Math.min(FRAME, to - i);
    // lamejs 的立体声签名是 encodeBuffer(left, right)，不吃交错数组：
    // 传单个 Int16Array 给 2 声道会直接抛 "Cannot read properties of undefined"。
    const l = new Int16Array(n);
    const r = rc ? new Int16Array(n) : null;
    for (let k = 0; k < n; k++) {
      const a = Math.max(-1, Math.min(1, lc[i + k] || 0));
      l[k] = a < 0 ? a * 0x8000 : a * 0x7fff;
      if (r) {
        const b = Math.max(-1, Math.min(1, rc[i + k] || 0));
        r[k] = b < 0 ? b * 0x8000 : b * 0x7fff;
      }
    }
    const chunk = r ? enc.encodeBuffer(l, r) : enc.encodeBuffer(l);
    if (chunk.length) parts.push(chunk);
    if ((i - from) % (FRAME * 40) === 0) {      // ≈每 2 秒音频让一次主线程
      if (onProgress) onProgress((i - from) / total);
      await new Promise((tick) => setTimeout(tick, 0));
    }
  }
  parts.push(enc.flush());
  return new Blob(parts, { type: "audio/mpeg" });
}

/* 按钮忙态 + 进度。labelEl 是按钮里的文字 span（保留图标），没有就退回改整个按钮文字。 */
async function withBtnProgress(btn, fn) {
  const labelEl = btn.querySelector("span") || btn;
  const orig = labelEl.textContent;
  btn.disabled = true;
  try {
    return await fn((p) => { labelEl.textContent = `编码中 ${Math.round(Math.max(0, Math.min(1, p)) * 100)}%`; });
  } finally {
    btn.disabled = false;
    labelEl.textContent = orig;
  }
}

/* 源音频的缓冲：优先用已解码的那份；页面刷新后恢复的 job 可能只有 URL，
   再 fetch 一次解码。文件已被清理时给可读错误，不静默回退成 WAV。 */
async function sourceBufferForExport() {
  if (ED.src) return ED.src;
  if (ED.srcUrl) {
    const r = await fetch(ED.srcUrl);
    if (!r.ok) {
      throw new Error(r.status === 404 ? "源音频已被清理，请重新生成" : `读取源音频失败（${r.status}）`);
    }
    return decodeNative(await r.arrayBuffer());
  }
  throw new Error("当前没有可用的源音频");
}

/* ---- WAV export & download ---- */
function encodeWav(buffer, s0, s1) {
  const sr = buffer.sampleRate;
  const from = Math.floor((s0 || 0) * buffer.length);
  const to = Math.floor((s1 ?? 1) * buffer.length);
  const n = Math.max(to - from, 1);
  const ch = buffer.numberOfChannels;
  const out = new DataView(new ArrayBuffer(44 + n * ch * 2));
  const ws = (o, s) => { for (let i = 0; i < s.length; i++) out.setUint8(o + i, s.charCodeAt(i)); };
  ws(0, "RIFF"); out.setUint32(4, 36 + n * ch * 2, true); ws(8, "WAVE"); ws(12, "fmt ");
  out.setUint32(16, 16, true); out.setUint16(20, 1, true); out.setUint16(22, ch, true);
  out.setUint32(24, sr, true); out.setUint32(28, sr * ch * 2, true);
  out.setUint16(32, ch * 2, true); out.setUint16(34, 16, true);
  ws(36, "data"); out.setUint32(40, n * ch * 2, true);
  let o = 44;
  for (let i = 0; i < n; i++) {
    for (let c = 0; c < ch; c++) {
      const v = Math.max(-1, Math.min(1, buffer.getChannelData(c)[from + i] || 0));
      out.setInt16(o, v < 0 ? v * 0x8000 : v * 0x7fff, true); o += 2;
    }
  }
  return new Blob([out], { type: "audio/wav" });
}

function triggerDownload(href, name) {
  const a = document.createElement("a");
  a.href = href;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // 下面两处下载用的是 createObjectURL 造的 blob URL，不 revoke 的话
  // 整段 wav（数 MB ~ 数十 MB）会一直驻留到页面关闭。
  if (href.startsWith("blob:")) setTimeout(() => URL.revokeObjectURL(href), 10000);
}

// 源音频的"原名"：优先用服务端 outputs/ 里的实际文件名，其次从 URL 推导
function srcFileName() {
  if (curServerFile) return curServerFile;
  const fromUrl = (curUrl || "").split("/").pop();
  if (fromUrl && fromUrl !== "audio") return fromUrl;
  return `spk_${Math.floor(Date.now() / 1000)}.wav`;
}

function updateDlName() {
  // 命名规则沿用原有：无操作=原名，仅裁剪=_trim，含增益/变速=_edit
  // _edit 的判定与「编辑结果」条的显示条件同源——看到什么就拿什么
  // 后缀统一在这里生成：界面上显示 .mp3 就一定走 MP3 字节，不会再出现"名字改了、内容没改"
  const base = srcFileName();
  const stem = base.replace(/\.(wav|mp3)$/i, "");
  const ext = fmtExt();
  const suffix = ED.hasResult() ? "_edit" : (ED.getState().trim ? "_trim" : "");
  const srcName = `${stem}${ext}`;
  const outName = `${stem}${suffix}${ext}`;
  $("dlSrcName").textContent = srcName;
  $("dlName").textContent = outName;
  // 「下载编辑结果」不在这里管显示与否——它整个包在 #resultPreview 里，
  // 由 edit.js 的 paintResult() 随结果区一起显隐（默认不显示，有编辑结果才出现）
  $("dlBtn").title = `下载剪辑后的音频（${outName}）`;
  $("dlSrcBtn").title = `下载未经剪辑的生成音频（${srcName}）`;
}

// edit.js 在状态变化后回调：把源波形的选区高亮与选区文案拉回同步
// （「重置全部」「撤销」都会清掉选区，只改 ED 是不够的）
window.syncTrimView = function () {
  if (!ED.src) return;
  const s = ED.getState().trim;
  const d = ED.src.duration;
  $("trimRange").textContent = s
    ? `选中 ${(s[0] * d).toFixed(2)}s – ${(s[1] * d).toFixed(2)}s · ${((s[1] - s[0]) * d).toFixed(2)}s`
    : `全长 ${d.toFixed(2)}s`;
  syncAxis();
  drawTrimWave();
};

/* WAV 出口：源音频直接拿服务端那份文件（不经过任何剪辑链路，字节就是原始字节）；
   MP3 出口必须自己编码，不能拿服务端 WAV 冒充。编码失败就退回 WAV 并说明。 */
async function doDownloadSrc() {
  const name = $("dlSrcName").textContent || srcFileName();
  const wav = () => {
    if (ED.srcUrl) triggerDownload(ED.srcUrl, swapExt(name, ".wav"));
    else if (ED.src) triggerDownload(URL.createObjectURL(encodeWav(ED.src)), swapExt(name, ".wav"));
    else showActionError("当前没有可下载的音频");
  };
  if (dlFormat !== "mp3") { wav(); return; }
  try {
    const blob = await withBtnProgress($("dlSrcBtn"), async (onProgress) => {
      const buf = await sourceBufferForExport();
      return encodeMp3(buf, 0, 1, onProgress);
    });
    triggerDownload(URL.createObjectURL(blob), name);
  } catch (e) {
    console.error("MP3 编码失败", e);
    showActionError(`MP3 编码失败（${e.message}），已改为 WAV 下载`);
    wav();
  }
}

async function doDownloadEdit() {
  const buf = ED.getRendered();
  if (!buf || !ED.hasOps()) return;
  // 下载与试听同源（都是 ED.rendered），不存在两套实现算出不同结果的可能
  const name = $("dlName").textContent || "audio_edit.wav";
  if (dlFormat !== "mp3") { triggerDownload(URL.createObjectURL(encodeWav(buf)), swapExt(name, ".wav")); return; }
  try {
    const blob = await withBtnProgress($("dlBtn"), (onProgress) => encodeMp3(buf, 0, 1, onProgress));
    triggerDownload(URL.createObjectURL(blob), name);
  } catch (e) {
    console.error("MP3 编码失败", e);
    showActionError(`MP3 编码失败（${e.message}），已改为 WAV 下载`);
    triggerDownload(URL.createObjectURL(encodeWav(buf)), swapExt(name, ".wav"));
  }
}

$("dlSrcBtn").addEventListener("click", doDownloadSrc);
$("dlBtn").addEventListener("click", doDownloadEdit);

// 格式开关：鼠标点选 + 左右方向键切换（两个按钮共用一个 tab 停靠点，按 ARIA radio 语义）
$("fmtSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-fmt]");
  if (!b || b.disabled) return;
  setDlFormat(b.dataset.fmt);
});
$("fmtSeg").addEventListener("keydown", (e) => {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  e.preventDefault();
  const next = dlFormat === "wav" ? "mp3" : "wav";
  if (next === "mp3" && mp3State === "unavailable") return;
  setDlFormat(next);
  const cur = $("fmtSeg").querySelector(`button[data-fmt="${dlFormat}"]:not(:disabled)`);
  if (cur) cur.focus();
});
syncFmtSeg();

/* ---------------- 保存到指定目录（服务端落盘，任何浏览器都能用） ----------------
   与「下载」是两条独立的路：下载永远走浏览器；保存只在点保存按钮时把文件复制/
   上传到用户在服务端设置的目录。复制失败时退回一次普通下载，保证音频不丢。 */
const saveDirState = { dir: "", exists: false, writable: false, reason: "", relative: false, busy: false, masked: false };
let saveMenuAnchor = null;
let saveToastTimer = 0;

function saveDirUsable() {
  return !!(saveDirState.dir && saveDirState.exists && saveDirState.writable);
}

function saveToastShow(msg, isErr) {
  const el = $("saveToast");
  el.textContent = msg;
  el.classList.toggle("err", !!isErr);
  el.hidden = false;
  clearTimeout(saveToastTimer);
  saveToastTimer = setTimeout(() => { el.hidden = true; }, isErr ? 8000 : 4000);
}

async function refreshSaveDir() {
  try {
    const r = await apiFetch("/prefs/save-dir");
    Object.assign(saveDirState, await r.json());
  } catch (e) {
    // 读不到就按"未设置"处理：点保存时会再走一次选择流程，不会静默写错地方
    Object.assign(saveDirState, { dir: "", exists: false, writable: false, reason: "" });
  }
  const el = $("saveMenuDir");
  // 具体原因由服务端给出（区分"目录不存在"/"这是个文件不是目录"/"没有写入权限"），
  // 以前这里三种情况共用一句"目录不存在"，用户照着提示找不到北（BUG-005）。
  const why = saveDirState.reason || (saveDirState.exists ? "没有写入权限" : "目录不存在");
  let label;
  if (!saveDirState.dir) label = "尚未设置保存目录";
  else if (!saveDirState.exists) label = `${saveDirState.dir}（${why}）`;
  else if (!saveDirState.writable) label = `${saveDirState.dir}（不可写：${why}）`;
  else label = saveDirState.dir;
  el.textContent = label;
  el.title = saveDirState.dir ? `${label}\n（路径在运行服务的这台电脑上）` : label;
  el.classList.toggle("is-empty", !saveDirState.dir);
  // masked：从别的设备访问时服务端不回显绝对路径，也打不开「打开目录」
  $("saveMenuOpen").disabled = !saveDirUsable() || !!saveDirState.masked;
  $("saveMenuForget").disabled = !saveDirState.dir;
  return saveDirState;
}

function closeSaveMenu() {
  const m = $("saveMenu");
  if (!m.hidden) m.hidden = true;
  if (saveMenuAnchor) saveMenuAnchor.setAttribute("aria-expanded", "false");
  saveMenuAnchor = null;
}

async function openSaveMenu(caret) {
  const reopen = saveMenuAnchor === caret && !$("saveMenu").hidden;
  closeSaveMenu();
  if (reopen) return;
  saveMenuAnchor = caret;
  const m = $("saveMenu");
  m.hidden = false;
  caret.setAttribute("aria-expanded", "true");
  const place = () => {
    const r = caret.getBoundingClientRect();
    const mw = m.offsetWidth, mh = m.offsetHeight;
    const left = Math.min(Math.max(8, r.right - mw), Math.max(8, window.innerWidth - mw - 8));
    let top = r.bottom + 6;
    if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 6);
    m.style.left = `${left}px`;
    m.style.top = `${top}px`;
  };
  // 先按当前宽度摆一次，避免刷新目录期间菜单闪在左下角；
  // 目录路径长短会改变菜单宽度，刷新后再摆一次贴边。
  place();
  await refreshSaveDir();
  place();
  const first = m.querySelector(".save-menu-item:not(:disabled)");
  if (first) first.focus({ preventScroll: true });
}

document.addEventListener("click", (e) => {
  if ($("saveMenu").hidden) return;
  if (!(e.target instanceof Element)) return;
  if (e.target.closest("#saveMenu") || e.target.closest(".save-caret")) return;
  closeSaveMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("saveMenu").hidden) closeSaveMenu();
});
window.addEventListener("resize", closeSaveMenu);
// 菜单是 fixed 定位，页面一滚就跟按钮脱开了；capture 顺带接住内部容器的滚动
window.addEventListener("scroll", closeSaveMenu, true);

async function putSaveDir(dir) {
  const r = await apiFetch("/prefs/save-dir", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dir }),
  });
  Object.assign(saveDirState, await r.json());
  await refreshSaveDir();
  if (!dir) {
    saveToastShow("已忘记保存目录");
  } else {
    // 手输相对路径时按"运行服务的那台电脑的工作目录"解析，结果往往不是用户想的位置，
    // 所以把解析后的绝对路径显式回显（BUG-005）。
    const note = saveDirState.relative ? `（相对路径，已按服务所在目录解析）` : "";
    saveToastShow(`保存目录已设为 ${saveDirState.dir}${note}`);
  }
  return !!dir;
}

async function promptSaveDir() {
  const v = await uiPrompt({
    title: "保存目录",
    body: "输入音频保存目录的完整路径（目录需已存在）",
    placeholder: "D:\\音频\\生成",
    value: saveDirState.dir || "",
  });
  if (!v) return false;
  return putSaveDir(String(v).trim());
}

async function pickSaveDir() {
  // 系统选择框弹在"运行服务的这台电脑"的桌面上：远程访问会 403，
  // 无 tkinter 的环境会 501，两种情况都退回手动输入，不让流程卡死。
  let j;
  try {
    const r = await apiFetch("/prefs/save-dir/pick", { method: "POST" });
    j = await r.json();
  } catch (e) {
    if (e.status === 403 || e.status === 501) return promptSaveDir();
    if (e.status === 409) throw new Error("文件夹选择框已经在电脑上打开，请先完成或取消它");
    throw e;
  }
  if (j.cancelled) return false;
  return putSaveDir(j.dir);
}

async function ensureSaveDir() {
  await refreshSaveDir();
  if (saveDirUsable()) return;
  // 没设置或目录已失效：就地弹一次选择框，选完继续保存
  const ok = await pickSaveDir();
  if (!ok) {
    const e = new Error("尚未设置保存目录");
    e.cancelled = true;
    throw e;
  }
}

async function uploadBlob(blob, name) {
  const fd = new FormData();
  fd.append("name", name);
  fd.append("file", blob, name);
  const r = await apiFetch("/save-dir/upload", { method: "POST", body: fd });
  return r.json();
}

/* 按当前格式编码并上传。MP3 失败时改成 WAV 再传一次（音频不能丢），
   文件名后缀随之换成 .wav——上传入口会校验后缀与内容一致。 */
async function encodeForExport(which, mp3, onProgress) {
  const buf = which === "src" ? await sourceBufferForExport() : ED.getRendered();
  if (!buf) throw new Error(which === "src" ? "当前没有可保存的音频" : "当前没有可保存的编辑结果");
  return mp3 ? encodeMp3(buf, 0, 1, onProgress) : encodeWav(buf);
}

async function saveEncoded(which, btn, name) {
  const wantMp3 = dlFormat === "mp3";
  const onProgress = (p) => { btn.textContent = `编码中 ${Math.round(Math.max(0, Math.min(1, p)) * 100)}%`; };
  try {
    const blob = await encodeForExport(which, wantMp3, wantMp3 ? onProgress : null);
    return await uploadBlob(blob, swapExt(name, wantMp3 ? ".mp3" : ".wav"));
  } catch (e) {
    if (!wantMp3) throw e;
    console.error("MP3 编码失败", e);
    saveToastShow(`MP3 编码失败（${e.message}），已改为保存 WAV`, true);
    const blob = await encodeForExport(which, false);
    return await uploadBlob(blob, swapExt(name, ".wav"));
  }
}

async function saveToDir(which, btn) {
  if (saveDirState.busy) return;
  saveDirState.busy = true;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "保存中…";
  try {
    await ensureSaveDir();
    let res;
    if (which === "src") {
      const name = $("dlSrcName").textContent || srcFileName();
      // 「服务端直接复制」快路径只对 WAV 成立：那份文件是 WAV，
      // 选了 MP3 却复制它就会出现"名字是 .mp3、内容是 WAV"，必须自己编码上传。
      if (dlFormat === "wav" && curServerFile && ED.srcUrl && !ED.srcUrl.startsWith("blob:")) {
        // 源文件本来就在服务端 outputs/：直接复制，字节与「下载」完全一致
        const r = await apiFetch(`/save-dir/outputs/${encodeURIComponent(curServerFile)}`, { method: "POST" });
        res = await r.json();
      } else {
        res = await saveEncoded(which, btn, name);
      }
    } else {
      const buf = ED.getRendered();
      if (!buf || !ED.hasOps()) throw new Error("当前没有可保存的编辑结果");
      res = await saveEncoded(which, btn, $("dlName").textContent || "audio_edit.wav");
    }
    const fname = String(res.path || "").split(/[\\/]/).pop();
    saveToastShow(`已保存 ${fname} → ${res.dir}`);
  } catch (e) {
    if (e.cancelled) {
      saveToastShow("已取消保存：尚未设置保存目录");
    } else {
      // 落盘失败不能把文件弄丢：退回一次普通下载
      saveToastShow(`保存失败：${e.message}（已改为浏览器下载）`, true);
      if (which === "src") doDownloadSrc(); else doDownloadEdit();
    }
  } finally {
    saveDirState.busy = false;
    btn.disabled = false;
    btn.textContent = label;
  }
}

$("saveSrcBtn").addEventListener("click", () => saveToDir("src", $("saveSrcBtn")));
$("saveEditBtn").addEventListener("click", () => saveToDir("edit", $("saveEditBtn")));
$("saveMenuSrc").addEventListener("click", () => openSaveMenu($("saveMenuSrc")));
$("saveMenuEdit").addEventListener("click", () => openSaveMenu($("saveMenuEdit")));

$("saveMenuPick").addEventListener("click", async () => {
  closeSaveMenu();
  try { await pickSaveDir(); } catch (e) { saveToastShow(`设置失败：${e.message}`, true); }
});
$("saveMenuInput").addEventListener("click", async () => {
  closeSaveMenu();
  try { await promptSaveDir(); } catch (e) { saveToastShow(`设置失败：${e.message}`, true); }
});
$("saveMenuOpen").addEventListener("click", async () => {
  closeSaveMenu();
  try {
    await apiFetch("/prefs/save-dir/open", { method: "POST" });
  } catch (e) {
    saveToastShow(`打开目录失败：${e.message}`, true);
  }
});
$("saveMenuForget").addEventListener("click", async () => {
  closeSaveMenu();
  try { await putSaveDir(""); } catch (e) { saveToastShow(`操作失败：${e.message}`, true); }
});

refreshSaveDir();

/* ---------------- preset library (right column, tabs with examples) ---------------- */
let presetNamesCache = [];
let curPresetName = "";       // applied preset name (for summary line + card highlight)
let exCache = [];             // example items cache

/* 预设自带的参考音频是预设的一部分：返回 "ok" / "superseded"（用户已手动换掉）/ "error"。
   以前这里是「发了就不管」，音频 404 也照样提示「已加载预设」，用户听着却还是
   上一份音色。 */
async function fetchPresetAudio(url, inputEl, applyFn, seqKey, fileName) {
  const seq = seqKey ? refSelSeq[seqKey] : 0;
  try {
    const r = await fetch(url);
    if (!r.ok) return "error";
    const blob = await r.blob();
    // 下载期间用户已经自己换了音频：以用户的选择为准
    if (seqKey && refSelSeq[seqKey] !== seq) return "superseded";
    fileName = fileName || (url.split("/").pop() || "audio") + ".wav";
    const file = new File([blob], fileName, { type: "audio/wav" });
    const dt = new DataTransfer();
    dt.items.add(file);
    inputEl.files = dt.files;
    applyFn(file);
    return "ok";
  } catch (e) {
    return "error";
  }
}
function applyPresetToForm(j) {
  $("emoMode").value = String(j.emo_control_method ?? 0);
  $("emoWeight").value = j.emo_weight ?? 0.65;
  const adv = j.advanced_params || {};
  if (adv.do_sample != null) $("doSample").checked = !!adv.do_sample;
  if (adv.top_p != null) $("topP").value = adv.top_p;
  if (adv.top_k != null) $("topK").value = adv.top_k;
  if (adv.temperature != null) $("temperature").value = adv.temperature;
  if (adv.length_penalty != null) $("lenPen").value = adv.length_penalty;
  if (adv.num_beams != null) $("numBeams").value = adv.num_beams;
  if (adv.repetition_penalty != null) $("repPen").value = adv.repetition_penalty;
  if (adv.max_mel_tokens != null) $("maxMel").value = adv.max_mel_tokens;
  if (adv.max_text_tokens_per_segment != null) $("segTokens").value = adv.max_text_tokens_per_segment;
  // 旧预设没有 interval_silence 字段，保持当前值不动（不强行回默认值）
  if (adv.interval_silence != null) $("gapMs").value = adv.interval_silence;
  if (j.emo_text != null) $("emoText").value = j.emo_text || "";
  // emo_random 一直有存，此前漏了回填
  if (j.emo_random != null) $("emoRandom").checked = !!j.emo_random;
  setVecInputs(j.emo_vector || [0, 0, 0, 0, 0, 0, 0, 0]);
  syncParams();
  emoVisible();
  scheduleSegments();
  enforceParamLock();
  scheduleDraftSave();
}
function collectPresetState(fd) {
  const mode = $("emoMode").value;
  fd.append("emo_control_method", mode);
  fd.append("emo_weight", $("emoWeight").value);
  // 与生成一致：只保存当前模式真正会用到的描述文本
  if (mode === "3") fd.append("emo_text", $("emoText").value);
  fd.append("use_random", $("emoRandom").checked);
  fd.append("do_sample", $("doSample").checked);
  fd.append("top_p", $("topP").value);
  fd.append("top_k", $("topK").value);
  fd.append("temperature", $("temperature").value);
  fd.append("length_penalty", $("lenPen").value);
  fd.append("num_beams", $("numBeams").value);
  fd.append("repetition_penalty", $("repPen").value);
  fd.append("max_mel_tokens", $("maxMel").value);
  fd.append("max_text_tokens_per_segment", $("segTokens").value);
  fd.append("interval_silence", $("gapMs").value);
  vecInputs().forEach((s) => fd.append("vec" + (parseInt(s.dataset.i) + 1), s.value));
}

/* library tab switching (presets / examples) */
const libTabs = document.querySelectorAll("#libTabs .libtab");
let curLib = "presets";

/* 资源库折叠：偏好存 localStorage，收起时只留标题行 */
const LIB_FOLD_KEY = "libFolded";
function libSetFolded(folded, persist = true) {
  $("libBox").classList.toggle("lib-collapsed", folded);
  $("libFold").title = folded ? "展开资源库" : "收起资源库";
  $("libFold").setAttribute("aria-label", $("libFold").title);
  // 同 themeBtn：写偏好失败不该让折叠动作本身失效
  if (persist) { try { localStorage.setItem(LIB_FOLD_KEY, folded ? "1" : "0"); } catch (e) { /* 存不下就只在本次会话生效 */ } }
}
$("libFold").addEventListener("click", () => {
  libSetFolded(!$("libBox").classList.contains("lib-collapsed"));
});
libSetFolded(lsGet(LIB_FOLD_KEY) === "1", false);

/* shared: apply a preset by name; returns success */
async function applyPresetByName(name) {
  try {
    const j = await presetDetail(name, true); // 应用时强制拉最新
    curPresetName = name;
    applyPresetToForm(j);
    // 音频要么换成预设自带的，要么明确清掉——留着上一个音色却显示
    // 「已加载预设」，是把用户往错误的成品上带。
    if (!j.prompt_audio_url) resetSpkSelection();
    if (!j.emo_audio_url) clearEmoFile();
    const jobs = [];
    if (j.prompt_audio_url) {
      jobs.push(fetchPresetAudio(j.prompt_audio_url, $("spkFile"), setSpkFile, "spk",
        j.prompt_audio_name || `${name}.wav`));
    }
    if (j.emo_audio_url) {
      jobs.push(fetchPresetAudio(j.emo_audio_url, $("emoFile"), setEmoFile, "emo",
        j.emo_audio_name || `${name} 情感.wav`));
    }
    const states = jobs.length ? await Promise.all(jobs) : [];
    updateGenSummary();
    if (states.includes("superseded")) {
      showActionHint("已加载预设: " + name + "（参考音频已按你的手动选择保留）");
    } else if (states.includes("error")) {
      showActionError("已加载预设 " + name + "，但参考音频下载失败，请手动选择音频");
    } else if (!jobs.length) {
      showActionHint("已加载预设: " + name + "（该预设不含参考音频）");
    } else {
      showActionHint("已加载预设: " + name);
    }
    return true;
  } catch (e) { showActionError("加载预设失败: " + (e.message || e)); return false; }
}

function switchLib(which) {
  curLib = which;
  libTabs.forEach((b) => b.classList.toggle("cur", b.dataset.lib === which));
  $("libSearch").value = "";
  renderLib();
  $("libTools").hidden = which !== "presets"; // 搜索/刷新/保存仅预设 tab 需要
  scheduleDraftSave();
}
libTabs.forEach((b) => b.addEventListener("click", () => switchLib(b.dataset.lib)));

/* 草稿里记着预设名，但预设可能已在别的标签页/外部被删掉。
   列表请求成功后才校验；请求失败时保留旧值，不能把网络抖动当成“预设没了”。 */
function pruneMissingPreset() {
  if (!curPresetName || presetNamesCache.includes(curPresetName)) return;
  curPresetName = "";
  updateGenSummary();
  scheduleDraftSave();
}
async function refreshPresets() {
  try {
    const r = await (await apiFetch("/presets")).json();
    presetNamesCache = r.presets || [];
    pruneMissingPreset();
  } catch (err) { /* keep old list */ }
}

/* 预设详情统一缓存（跨 libList / preset 管理页复用，避免 N+1 重复请求） */
const presetDetailCache = new Map();
function presetDetail(name, force = false) {
  if (!force && presetDetailCache.has(name)) return Promise.resolve(presetDetailCache.get(name));
  const p = apiFetch("/presets/" + encodeURIComponent(name))
    .then((r) => r.json())
    .then((d) => {
      presetDetailCache.set(name, d);
      return d;
    })
    .catch((e) => {
      // 失败结果不留在缓存里：一次网络抖动（或服务刚启动）会让这张卡
      // 永久显示「详情不可用」，只有整页刷新才恢复。
      presetDetailCache.delete(name);
      throw e;
    });
  presetDetailCache.set(name, p);
  return p;
}

function renderLib() {
  const box = $("libList");
  const q = ($("libSearch").value || "").trim().toLowerCase();
  box.replaceChildren();
  if (curLib === "presets") {
    const names = presetNamesCache.filter((n) => !q || n.toLowerCase().includes(q));
    if (!names.length) {
      const empty = document.createElement("div");
      empty.className = "hist-item";
      empty.style.cursor = "default";
      empty.style.color = "var(--faint)";
      empty.textContent = q ? "没有匹配的预设" : "暂无预设，点右上角「保存当前为预设」创建";
      box.appendChild(empty);
      return;
    }
    for (const name of names) {
      const row = document.createElement("div");
      row.className = "hist-item" + (name === curPresetName ? " cur" : "");
      row.innerHTML = `<span class="htext"></span><span class="hmeta">加载中…</span>
        <span class="pm-bar">
          <span class="pm-acts">
            <button class="pm-act" data-act="rename" type="button" title="改名">改</button>
            <button class="pm-act" data-act="duplicate" type="button" title="复制">复</button>
          </span>
          <button class="pm-del" type="button" title="删除此预设">${ICON_CLOSE}</button>
        </span>`;
      row.querySelector(".htext").textContent = name;
      row.title = "点击应用此预设";
      row.querySelector(".pm-del").setAttribute("aria-label", "删除预设 " + name);
      const meta = row.querySelector(".hmeta");
      presetDetail(name).then((d) => {
        const adv = d.advanced_params || {};
        // 卡片只有约 156px 宽，原来这一行是「同参考 · 含音频 · temp 0.80」，
        // 三列布局下必然省略成「…含音频 · t…」——收尾一个孤零零的字母最像 bug。
        // 卡片上只留两个能一眼分类的事实，采样参数去检视面板看，全文挂 title。
        const emoLabel = ["同参考", "情感音频", "向量", "文本"][d.emo_control_method] ?? "-";
        meta.textContent = `${emoLabel} · ${d.prompt_audio_url ? "含音频" : "无音频"}`;
        meta.title = `${meta.textContent} · temp ${adv.temperature ?? "-"}`;
      }).catch(() => { meta.textContent = "-"; });
      row.addEventListener("click", async (e) => {
        if (e.target.closest(".pm-del") || e.target.closest(".pm-act")) return;
        if (await applyPresetByName(name)) renderLib();
      });
      makeActivatable(row, "应用预设 " + name);
      row.querySelectorAll(".pm-act").forEach((btn) => {
        btn.addEventListener("click", async (e) => {
          e.stopPropagation();
          await pmPresetAction(name, btn.dataset.act);
        });
      });
      row.querySelector(".pm-del").addEventListener("click", async (e) => {
        e.stopPropagation();
        await pmPresetAction(name, "delete");
      });
      box.appendChild(row);
    }
  } else {
    // examples tab
    const items = exCache.filter((ex) => !q || (ex.text || "").toLowerCase().includes(q) || (ex.prompt_audio || "").toLowerCase().includes(q));
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "hist-item";
      empty.style.cursor = "default";
      empty.style.color = "var(--faint)";
      empty.textContent = "暂无示例";
      box.appendChild(empty);
      return;
    }
    for (const ex of items) {
      const row = document.createElement("div");
      row.className = "hist-item";
      row.innerHTML = `<span class="htext"></span><span class="hmeta"></span>`;
      row.querySelector(".htext").textContent = ex.text || "";
      row.querySelector(".hmeta").textContent = (ex.prompt_audio || "").split("/").pop();
      row.querySelector(".hmeta").title = row.querySelector(".hmeta").textContent;
      row.title = "点击加载示例音频与参数";
      row.addEventListener("click", async () => {
        try {
          const audio = await fetch(ex.prompt_audio);
          if (!audio.ok) throw new Error("示例音频不存在");
          const blob = await audio.blob();
          const fileName = (ex.prompt_audio || "sample.wav").split("/").pop();
          const file = new File([blob], fileName, { type: "audio/wav" });
          const dt = new DataTransfer();
          dt.items.add(file);
          $("spkFile").files = dt.files;
          // persist=true：示例音频也要进草稿。以前传 false（理由是"服务端已有这个文件"），
          // 结果是刷新后文本/参数都还在、音色却回到了空占位——和预设路径行为不一致（BUG-008）。
          setSpkFile(file);
          $("text").value = ex.text || "";
          $("charCount").textContent = `${textUnits((ex.text || "").trim())} 字`;
          $("emoMode").value = String(ex.emo_mode ?? 0);
          if (ex.emo_weight != null) { $("emoWeight").value = ex.emo_weight; }
          $("emoText").value = ex.emo_text || "";
          setVecInputs([ex.emo_vec_1, ex.emo_vec_2, ex.emo_vec_3, ex.emo_vec_4,
            ex.emo_vec_5, ex.emo_vec_6, ex.emo_vec_7, ex.emo_vec_8]);
          syncParams();
          emoVisible();
          curPresetName = "";
          scheduleSegments();
          scheduleDraftSave();
          showActionHint("示例已加载: " + ((ex.text || "").slice(0, 30) || fileName));
          updateGenSummary();
        } catch (err) { showActionError("加载示例失败: " + (err.message || err)); }
      });
      makeActivatable(row, "加载示例 " + ((ex.text || "").slice(0, 24) || ""));
      box.appendChild(row);
    }
  }
}
$("libSearch").addEventListener("input", renderLib);
$("btnLibRefresh").addEventListener("click", async () => {
  if (curLib !== "presets") return;
  clearActionError();
  await refreshPresets();
  renderLib();
  if (!presetNamesCache.length) showActionHint("暂无预设");
});
/* 保存预设统一入口：收集当前表单 → POST /presets；同名返回 409 时弹确认后
   带 overwrite 重发。返回成功与否，调用方各自给出提示位置。 */
async function savePresetByName(name, onDone) {
  const fd = new FormData();
  fd.append("name", name);
  collectPresetState(fd);
  const spkF = $("spkFile").files[0];
  if (spkF) fd.append("prompt_audio", spkF);
  const emoF = $("emoFile").files[0];
  if (emoF) fd.append("emo_audio", emoF);
  let overwrite = false;
  for (let attempt = 0; attempt < 2; attempt++) {
    if (overwrite) fd.set("overwrite", "true");
    const r = await fetch(`${API}/presets`, { method: "POST", body: fd });
    if (r.ok) {
      // 两处缓存都得清：只清 presetDetailCache 的话，切到预设页时
      // pmEnsureDetails 用 `!pmDetailMap.has(n)` 过滤会把旧详情当成已加载跳过，
      // 右栏和卡片上的 temp/top_p/beam 仍是覆盖前的旧值。
      presetDetailCache.delete(name);
      pmDetailMap.delete(name);
      await (onDone || (() => {}));
      return true;
    }
    const j = await r.json().catch(() => ({}));
    if (r.status === 409 && !overwrite) {
      const yes = await uiConfirm({
        title: "覆盖预设",
        body: `预设「${name}」已存在，是否用当前参数覆盖？`,
        okText: "覆盖保存",
        danger: true,
      });
      if (!yes) return false;
      overwrite = true;
      continue;
    }
    throw new Error(j.detail || "保存失败");
  }
  return false;
}

$("btnLibSave").addEventListener("click", async () => {
  const name = await uiPrompt({ title: "保存当前为预设", placeholder: "请输入预设名称", value: curPresetName || "" });
  if (name === null || name === false) return;
  const trimmed = name.trim();
  if (!trimmed) { showActionError("请输入预设名称"); return; }
  try {
    if (!await savePresetByName(trimmed)) return;
    curPresetName = trimmed;
    showActionHint("预设已保存: " + trimmed);
    await refreshPresets();
    switchLib("presets");
    updateGenSummary();
    refreshPmList();
  } catch (e) { showActionError("保存预设失败: " + (e.message || e)); }
});

async function loadExamples() {
  try {
    const j = await (await apiFetch("/examples")).json();
    exCache = j.examples || [];
  } catch (err) { /* keep old list */ }
}

/* ---------------- generation history (right sidebar) ----------------
   列表由后端落盘到 data/history.json，重启后仍在；但产物本身受 14 天保留期约束
   （启动时按 mtime 清理 outputs/），所以文案统一写「最长 14 天」，不能承诺永久。 */
const histBar = $("histBar");
const genPage = document.querySelector('.page[data-page="gen"]');
const HIST_KEY = "histCollapsed";
const EMO_LABEL = ["同参考", "情感音频", "向量", "文本"];
let histItems = [];
let histCurId = "";

function histSetCollapsed(collapsed, persist = true) {
  histBar.classList.toggle("collapsed", collapsed);
  genPage.classList.toggle("hist-collapsed", collapsed);
  // 同上：写偏好失败不能中断后面的计数刷新
  if (persist) { try { localStorage.setItem(HIST_KEY, collapsed ? "1" : "0"); } catch (e) { /* 存不下就只在本次会话生效 */ } }
  $("histRailCount").textContent = histItems.length;
}

function histFmtTime(ts) {
  const d = new Date((ts || 0) * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function histFmtSize(bytes) {
  const b = Number(bytes) || 0;
  return b >= 1048576 ? (b / 1048576).toFixed(1) + "M" : Math.max(1, Math.round(b / 1024)) + "K";
}

/* 只把明确的 404 当作"文件没了"。网络抖动/服务重启返回 true 会把一条好记录标灰，
   所以异常一律按"还在"处理，真取不到再交给 loadResult 报错。 */
async function historyFileMissing(url) {
  try {
    const r = await fetch(url, { method: "HEAD", cache: "no-store" });
    return r.status === 404;
  } catch (e) {
    return false;
  }
}

function renderHistory() {
  const box = $("histList");
  $("histCount").textContent = histItems.length;
  $("histRailCount").textContent = histItems.length;
  box.replaceChildren();
  if (!histItems.length) {
    const empty = document.createElement("div");
    empty.className = "hb-empty";
    empty.textContent = "暂无记录 · 生成后会保留（最长 14 天）";
    box.appendChild(empty);
    return;
  }
  for (const it of histItems) {
    const row = document.createElement("div");
    row.className = "hb-item" + (it.id === histCurId ? " cur" : "") + (it.missing ? " missing" : "");
    row.innerHTML = `<span class="hb-text"></span><span class="hb-meta"></span><button class="pm-del" type="button" title="删除这条记录">${ICON_CLOSE}</button>`;
    row.querySelector(".hb-text").textContent = it.text || "(无文本)";
    row.querySelector(".hb-meta").textContent = it.missing
      ? "文件已不存在 · 点 ✕ 删除这条"
      : `${histFmtTime(it.created_at)} · ${Number(it.elapsed).toFixed(1)}s · ${histFmtSize(it.size)} · ${EMO_LABEL[it.emo_mode] ?? "-"}`;
    row.querySelector(".hb-meta").title = it.missing
      ? "音频文件已被清理或手动删除，记录只能删除"
      : row.querySelector(".hb-meta").textContent;
    row.title = it.missing ? "音频文件已不存在" : "点击载入到右侧播放器";
    const del = row.querySelector(".pm-del");
    del.setAttribute("aria-label", "删除这条历史记录");
    row.addEventListener("click", async (e) => {
      if (e.target.closest(".pm-del")) return;
      // 生成中禁止载入历史：loadResult 会隐藏 loading，与仍在推送的 progress 事件
      // 互相打架，histCurId/ED.setSource/裁剪缓冲被两条路径交替覆盖。
      if (genBtn.disabled) { showActionHint("正在生成，结束后再查看历史"); return; }
      if (it.missing) { showActionHint("这个音频文件已不存在，可点右侧 ✕ 删除这条记录"); return; }
      histCurId = it.id;
      renderHistory();
      // 先探一次文件在不在：借 404 把「文件被清理」变成行内灰化提示，
      // 而不是让 loadResult 弹红色「音频加载失败」——那条错误会让人以为播放器坏了。
      if (await historyFileMissing(it.url)) {
        it.missing = true;
        renderHistory();
        showActionHint("音频文件已不存在（超过 14 天或已手动删除），可点 ✕ 删除这条记录");
        return;
      }
      // 带候选组的条目：按组重建候选条；旧记录/单条则清掉上一轮的候选条
      if (it.cand_group) await rebuildCandFromHistory(it.cand_group, it.id);
      else clearCandidates();
      await loadResult(it.url, it.elapsed, it.file);
    });
    // 双击保护：第一击删完会重渲染，旧节点脱离 DOM，但第二击的事件可能已经在队列里，
    // 于是对同一条再发一次 DELETE 拿到 404，弹出红色"删除历史失败"（BUG-014）。
    // 404 的真实含义就是"这条已经不在了"，静默刷新即可。
    let delInFlight = false;
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (delInFlight || !row.isConnected) return;
      delInFlight = true;
      del.disabled = true;
      try {
        const r = await fetch(`${API}/history/` + encodeURIComponent(it.id), { method: "DELETE" });
        if (r.status === 404) {
          await refreshHistory();            // 已被删除（连点/别的标签页）：按已删除处理，不报错
        } else if (!r.ok) {
          throw new Error("删除失败");
        } else {
          if (histCurId === it.id) histCurId = "";
          await refreshHistory();
        }
      } catch (err) { showActionError("删除历史失败: " + (err.message || err)); }
      finally { delInFlight = false; del.disabled = false; }
    });
    makeActivatable(row, "载入历史记录 " + histFmtTime(it.created_at));
    box.appendChild(row);
  }
}

async function refreshHistory() {
  try {
    const r = await (await apiFetch("/history")).json();
    histItems = r.items || [];
    $("histAuto").checked = !!r.auto_clean;
    // 写清上限，避免用户以为重启后永远都在：产物本身仍受 14 天保留期约束
    const lim = r.limit || 20;
    $("histNote").textContent = r.auto_clean
      ? `跨重启保留 · 最长 14 天 · 自动清理保留最近 ${lim} 条`
      : `跨重启保留 · 列表保留最近 ${lim} 条，文件不删（仍受 14 天保留期约束）`;
    $("histNoteQ").title = "记录在 data/history.json；outputs/ 里超过 14 天的文件会在启动时清理";
    // 折叠时也渲染列表：隐藏态下渲染无副作用，否则展开后看到空列表，
    // 要等下一次刷新才补上（计数有值但内容为空的观感就是"数据丢了"）
    renderHistory();
  } catch (e) { /* history is a side feature: never block the main flow */ }
}

$("histToggle").addEventListener("click", () => histSetCollapsed(false));
$("histCollapse").addEventListener("click", () => histSetCollapsed(true));

$("histAuto").addEventListener("change", async (e) => {
  const fd = new FormData();
  fd.append("auto_clean", e.target.checked);
  try {
    const r = await fetch(`${API}/history/config`, { method: "POST", body: fd });
    // 不判 ok 的话，失败时响应体是 {"detail":...}，r.auto_clean 为 undefined，
    // 复选框被静默拨回关闭，用户以为关掉了其实没关。
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json();
    $("histAuto").checked = !!j.auto_clean;
    // 服务端已按新值生效（内存里），只是没能落盘。复选框保持服务端的真实值，
    // 另行提示"重启会恢复"——如果把勾选回滚，界面显示的状态就和服务端行为不一致了。
    if (j.persisted === false) {
      showActionError("已切换，但数据目录不可写：这项设置仅在本次运行有效，重启后恢复原值");
    }
  } catch (err) {
    $("histAuto").checked = !e.target.checked;   // 请求失败：回滚，与服务端保持一致
    showActionError("切换自动清理失败");
  }
});

$("histClear").addEventListener("click", async () => {
  if (!histItems.length) return;
  if (!await uiConfirm({
    title: "清空生成历史",
    body: `自动清理开启时，对应的 ${histItems.length} 个音频文件会一并删除，此操作不可恢复。`,
    okText: "清空",
    danger: true,
  })) return;
  try {
    // 不判 ok 的话，删除失败也会走到底下的「清空成功」提示，
    // 随后 refreshHistory 又把列表拉回来 —— 数据没变却提示已清空。
    const r = await fetch(`${API}/history`, { method: "DELETE" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    histCurId = "";
    await refreshHistory();
  } catch (err) { showActionError("清空历史失败"); }
});

/* ---------------- glossary ---------------- */
const glError = $("glError");
// 表格里当前显示的词，用于把"这条已存在，改的是哪一列"说清楚（BUG-004）
let glossaryCache = {};
function showGlossaryError(msg) {
  glError.textContent = msg;
  glError.hidden = false;
}
function clearGlossaryError() {
  glError.textContent = "";
  glError.hidden = true;
}

/* 折叠面板展开后，sticky CTA 会遮挡新展开内容的末尾（毛玻璃吸底），
   自动把面板滚进 ops 可视区，让完整内容（如术语添加行）可见。 */
document.querySelectorAll("details.fold").forEach((f) => {
  f.addEventListener("toggle", () => {
    if (!f.open) return;
    const ops = f.closest(".ops");
    if (!ops) return;
    // 等两帧：details 展开先塌陷再撑高，一帧内量到的还是旧布局
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const fb = f.getBoundingClientRect();
      // 阈值取 CTA 实际顶边（含负 margin 等偏移），比按高度推算稳
      const ctaRect = ops.querySelector(".cta")?.getBoundingClientRect();
      const limit = ctaRect ? ctaRect.top : ops.getBoundingClientRect().bottom;
      if (fb.bottom > limit) ops.scrollTop += fb.bottom - limit;
    }));
  });
});

async function renderGlossary() {
  try {
    const j = await (await apiFetch("/glossary")).json();
    const g = j.glossary || {};
    glossaryCache = g;
    // 级联开关跟术语表一起返回，刷新页面/别的标签页改了也能同步回来
    $("glCascade").checked = !!j.cascade;
    const keys = Object.keys(g);
    const box = $("glTable");
    box.replaceChildren();
    if (!keys.length) {
      const empty = document.createElement("div");
      empty.className = "gl-row";
      const term = document.createElement("span");
      term.className = "gl-term";
      term.textContent = "暂无自定义术语";
      empty.append(term, document.createElement("span"), document.createElement("span"));
      box.appendChild(empty);
      $("btnClearTerms").hidden = true;
      return;
    }
    for (const key of keys) {
      const value = g[key];
      const row = document.createElement("div");
      row.className = "gl-row";
      const term = document.createElement("span");
      term.className = "gl-term";
      const zh = document.createElement("span");
      const en = document.createElement("span");
      const del = document.createElement("button");
      del.className = "gl-del";
      del.type = "button";
      del.innerHTML = ICON_CLOSE;
      del.title = `删除术语「${key}」`;
      del.setAttribute("aria-label", `删除术语 ${key}`);
      del.addEventListener("click", () => removeTerm(key));
      row.append(term, zh, en, del);
      term.textContent = key;
      zh.textContent = typeof value === "object" ? (value.zh || "") : "";
      en.textContent = typeof value === "object" ? (value.en || "") : (value || "");
      box.appendChild(row);
    }
    $("btnClearTerms").hidden = false;
  } catch (err) { showGlossaryError("术语表加载失败，显示的是上次内容"); }
}

/* 单条删除是轻量可逆操作（重新添加即可），不弹确认框；清空全部才拦截。 */
async function removeTerm(key) {
  try {
    await apiFetch("/glossary/" + encodeURIComponent(key), { method: "DELETE" });
    clearGlossaryError();
    await renderGlossary();
    showActionHint(`已删除术语: ${key}`);
  } catch (err) { showGlossaryError("删除术语失败: " + (err.message || err)); }
}

$("btnClearTerms").addEventListener("click", async () => {
  const ok = await uiConfirm({
    title: "清空术语",
    body: "确定清空全部自定义术语？此操作不可恢复。",
    okText: "清空", danger: true,
  });
  if (!ok) return;
  try {
    await apiFetch("/glossary", { method: "DELETE" });
    clearGlossaryError();
    await renderGlossary();
    showActionHint("术语已清空");
  } catch (err) { showGlossaryError("清空术语失败: " + (err.message || err)); }
});
$("btnAddTerm").addEventListener("click", async () => {
  const termKey = $("glTerm").value.trim();
  const wantZh = $("glZh").value.trim();
  const wantEn = $("glEn").value.trim();
  const fd = new FormData();
  fd.append("term", $("glTerm").value);
  fd.append("reading_zh", $("glZh").value);
  fd.append("reading_en", $("glEn").value);
  try {
    const j = await (await apiFetch("/glossary", { method: "POST", body: fd })).json();
    $("glTerm").value = ""; $("glZh").value = ""; $("glEn").value = "";
    clearGlossaryError();
    await renderGlossary();
    // 空着的那一列由服务端沿用原读法（BUG-004），这里必须说出来——
    // 否则用户以为自己填的一列把整条覆盖了，或以为那一列是自己刚写的。
    const saved = (j.glossary || {})[termKey] || {};
    const kept = [];
    if (!wantZh && saved.zh) kept.push(`中文读法沿用「${saved.zh}」`);
    if (!wantEn && saved.en) kept.push(`英文读法沿用「${saved.en}」`);
    showActionHint(kept.length ? `术语已更新，${kept.join("、")}` : "术语已添加");
  } catch (err) { showGlossaryError("添加术语失败: " + (err.message || err)); }
});
["glTerm", "glZh", "glEn"].forEach((id) => {
  $(id).addEventListener("input", clearGlossaryError);
});
$("glCascade").addEventListener("change", async (e) => {
  const want = e.target.checked;
  clearGlossaryError();
  const fd = new FormData();
  fd.append("cascade", want ? "true" : "false");
  try {
    const j = await (await apiFetch("/glossary/cascade", { method: "POST", body: fd })).json();
    // 用服务端返回的值回填，别信本地 checkbox——失败时会和服务端不一致
    e.target.checked = !!j.cascade;
    await renderGlossary();
    scheduleSegments();          // 替换策略变了，预览要跟着重算
    showActionHint(want
      ? "已开启级联替换：读音里如果含有其它术语，会被继续改写"
      : "已关闭级联替换：每条术语只作用于你输入的原文");
  } catch (err) {
    showGlossaryError("切换失败: " + (err.message || err));
    await renderGlossary();
  }
});

/* ---------------- page tabs (gen / presets / model) ---------------- */
const tabBtns = document.querySelectorAll("#tabs .tab");
/* 页脚提示按当前页签换文案：三个页面共用一个 <footer>，
   原来在预设页和模型页也写着「Ctrl+Enter 生成」，与当前能做的事无关。
   结构改成 [{ keys: [...], text }] 而不是一整行字符串：键位要单独排成键帽，
   说明文字才不会被「Ctrl+Enter 生成」这种混排吃掉层次。分隔符也交给 CSS，
   文案里不再手写「·」。 */
const FOOT_HINT = {
  gen: [
    { keys: ["Ctrl", "Enter"], text: "生成" },
    { keys: ["Space"], text: "播放暂停" },
    { keys: ["Ctrl", "S"], text: "存预设" },
    { keys: [], text: "主题跟随系统偏好，可点右上角切换" },
  ],
  presets: [
    { keys: [], text: "预设存于服务端，跨会话可用" },
    { keys: [], text: "删除不可恢复" },
    { keys: [], text: "点卡片在右侧查看音色与采样参数" },
  ],
  model: [
    { keys: [], text: "精度与加速项改动后需点「重启并应用配置」才生效" },
    { keys: [], text: "模型懒加载，首次生成约需 30~60 秒" },
  ],
  guide: [
    { keys: [], text: "点左侧目录跳到对应章节" },
    { keys: [], text: "点配图可放大，Esc 关闭" },
    { keys: [], text: "地址栏 #guide/章节 可直接分享定位" },
  ],
};

/* 用 DOM 拼而不是拼 innerHTML：文案里含「」，走 textContent 不用担心转义 */
function renderFootHint(name) {
  const frag = document.createDocumentFragment();
  for (const { keys, text } of FOOT_HINT[name] || FOOT_HINT.gen) {
    const item = document.createElement("span");
    item.className = "fh-item";
    for (const k of keys) {
      const kbd = document.createElement("kbd");
      kbd.textContent = k;
      item.appendChild(kbd);
    }
    const label = document.createElement("span");
    label.textContent = text;
    item.appendChild(label);
    frag.appendChild(item);
  }
  $("footHint").replaceChildren(frag);
}

/* 离开模型页前拦一道（BUG-017）：精度/步数/CFG 这些改动只有点「保存配置」才生效，
   切页后回来会被服务端值覆盖。原来一声不吭就丢了，现在确认过再走，取消就留在模型页。 */
let curPage = "gen";
let pageSwitching = false;
/* 页签与 hash：页面名后面可以再挂一段锚点（#guide/3-2-音色参考音频），
   刷新页面或贴链接都能直接落到某一节。其它页面忽略锚点。 */
const PAGES = ["gen", "presets", "model", "guide"];
function parseHash() {
  const raw = location.hash.slice(1);
  const cut = raw.indexOf("/");
  const page = cut < 0 ? raw : raw.slice(0, cut);
  if (!PAGES.includes(page)) return { page: "gen", anchor: "" };
  let anchor = cut < 0 ? "" : raw.slice(cut + 1);
  try { anchor = decodeURIComponent(anchor); } catch (e) { /* 畸形转义按原样用 */ }
  return { page, anchor };
}
/* 中文锚点在 location.hash 里是百分号编码，直接和原文比较会永远不相等 */
function hashIs(want) {
  try { return decodeURIComponent(location.hash) === want; }
  catch (e) { return location.hash === want; }
}
async function switchPage(name, updateHash = true, anchor = "") {
  // 重入闸：确认框是 await 悬挂点，期间 hashchange（前进/后退、手改地址栏）
  // 能再进来一次，两条路径会交替改 curPage/页签高亮。确认框期间来的切换
  // 一律按「取消」处理：弹回当前页 hash，与上面取消分支语义一致。
  if (pageSwitching) { history.replaceState(null, "", "#" + curPage); return; }
  pageSwitching = true;
  const from = curPage;
  try {
    if (name !== "model" && curPage === "model" && mmCfgDirty()) {
      const ok = await uiConfirm({
        title: "配置还没保存",
        body: "模型页上改过的精度、扩散步数、CFG 强度、分段批处理还没点「保存配置」。"
          + "离开后这些改动不会生效，回到模型页会显示服务端当前正在用的值。",
        okText: "仍要离开",
      });
      if (!ok) {
        // 地址栏已经跟着点了改：回滚成 #model，免得刷新后跑去了别的页
        if (location.hash.slice(1) !== "model") history.replaceState(null, "", "#model");
        return;
      }
      mmServerCfg = null;      // 用户确认丢弃：基准清空，回来按服务端值重建
    }
    curPage = name;
    tabBtns.forEach((b) => b.classList.toggle("cur", b.dataset.page === name));
    document.querySelectorAll(".page").forEach((p) => {
      const on = p.dataset.page === name;
      p.classList.toggle("cur", on);
      p.hidden = !on;
    });
    renderFootHint(name);
    if (name === "model") refreshModelPage();
    if (name === "presets") refreshPmList();
    if (name === "guide") {
      await ensureGuide();
      if (anchor) goGuideAnchor(anchor, false);
      else guideScrollTo(0, false);
      syncGuideActive();
    } else if (from === "guide") {
      // 指南是长页面，读一半切走会带着几千像素的滚动位置，其它页会“凭空”从半腰出现
      guideScrollTo(0, false);
    }
    // hash 路由：刷新/分享链接时停留在当前页签
    if (updateHash) {
      const want = anchor ? `#${name}/${anchor}` : `#${name}`;
      if (!hashIs(want)) history.replaceState(null, "", want);
    }
  } finally {
    pageSwitching = false;
  }
}
tabBtns.forEach((b) => b.addEventListener("click", () => switchPage(b.dataset.page)));
window.addEventListener("hashchange", () => {
  const { page, anchor } = parseHash();
  switchPage(page, false, anchor);
});

/* ---------------- 使用指南页 ----------------
   正文由服务端从 web/guide/guide.md 渲染（/api/v1/guide），前端只负责：
   目录树、锚点滚动、滚动高亮、配图灯箱。第一次进页面才拉取，失败可再进重试。 */
let guidePromise = null;
let guideHeadings = [];
let guideSpyBound = false;
let guideScrollTick = false;

function guideTocList(tokens) {
  const ul = document.createElement("ul");
  ul.className = "guide-toc-list";
  for (const t of tokens) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.className = "guide-toc-link" + (t.level > 2 ? " lv3" : "");
    a.href = `#guide/${t.id}`;
    a.dataset.anchor = t.id;
    a.textContent = t.name;
    a.addEventListener("click", (ev) => {
      ev.preventDefault();
      goGuideAnchor(t.id, true);
    });
    li.appendChild(a);
    if (t.children && t.children.length) li.appendChild(guideTocList(t.children));
    ul.appendChild(li);
  }
  return ul;
}

function guideHeaderOffset() {
  const h = document.querySelector("header");
  return (h ? h.getBoundingClientRect().height : 0) + 16;
}

/* 宽屏是应用式布局（页面不滚，正文卡自己滚），窄屏回到整页滚动。
   锚点定位和高亮要在同一个坐标系里算，所以统一走下面三个小助手。 */
const GUIDE_WIDE = window.matchMedia("(min-width: 1101px)");

function guideScrollBox() {
  return GUIDE_WIDE.matches ? $("guideBody") : null;
}

function guideScrollTo(top, smooth) {
  const box = guideScrollBox();
  const opts = { top: Math.max(0, top), behavior: smooth ? "smooth" : "auto" };
  if (box) box.scrollTo(opts);
  else window.scrollTo(opts);
}

function goGuideAnchor(id, updateHash) {
  const el = id ? document.getElementById(id) : null;
  if (!el) return;
  const box = guideScrollBox();
  let top;
  if (box) {
    top = box.scrollTop + el.getBoundingClientRect().top - box.getBoundingClientRect().top - 16;
  } else {
    top = window.scrollY + el.getBoundingClientRect().top - guideHeaderOffset();
  }
  guideScrollTo(top, true);
  const want = `#guide/${id}`;
  if (updateHash && !hashIs(want)) history.replaceState(null, "", want);
  setGuideActive(id);
}

function setGuideActive(id) {
  document.querySelectorAll("#guideToc .guide-toc-link").forEach((a) => {
    const on = a.dataset.anchor === id;
    a.classList.toggle("cur", on);
    if (on) a.setAttribute("aria-current", "true");
    else a.removeAttribute("aria-current");
  });
}

/* 高亮当前章节：找最后一个已经越过内容区顶沿的标题 */
function syncGuideActive() {
  if (!guideHeadings.length) return;
  const box = guideScrollBox();
  const line = box ? box.getBoundingClientRect().top + 20 : guideHeaderOffset() + 12;
  let cur = guideHeadings[0].id;
  for (const h of guideHeadings) {
    if (h.getBoundingClientRect().top <= line) cur = h.id;
    else break;
  }
  setGuideActive(cur);
}

let guideLbPrev = null;
function openGuideLightbox(img) {
  if (!img || !img.src) return;
  const mask = $("guideLightbox"), big = $("guideLightboxImg"), cap = $("guideLightboxCap");
  guideLbPrev = document.activeElement;
  big.src = img.currentSrc || img.src;
  big.alt = img.alt || "";
  cap.textContent = img.alt || "";
  cap.hidden = !img.alt;
  mask.hidden = false;
  $("guideLightboxClose").focus();
}

function closeGuideLightbox() {
  const mask = $("guideLightbox");
  if (!mask || mask.hidden) return;
  mask.hidden = true;
  $("guideLightboxImg").removeAttribute("src");
  if (guideLbPrev && guideLbPrev.focus) guideLbPrev.focus();
  guideLbPrev = null;
}

function bindGuideContent(root) {
  root.querySelectorAll("img").forEach((img) => {
    // 27 张图一共 2.4 MB，本地服务瞬间读盘；不用懒加载，
    // 免得深链落地后图片陆续撑开、把目标章节挤走。
    img.decoding = "async";
    // 独占一段的配图升级成 figure + 图注（图注取 alt，读图和看图的人都有上下文）
    const p = img.parentElement;
    if (p && p.tagName === "P" && p.childNodes.length === 1) {
      const fig = document.createElement("figure");
      fig.className = "guide-fig";
      p.replaceWith(fig);
      fig.appendChild(img);
      if (img.alt) {
        const cap = document.createElement("figcaption");
        cap.textContent = img.alt;
        fig.appendChild(cap);
      }
    }
    img.classList.add("guide-zoom");
    img.title = "点击查看大图";
    makeActivatable(img, "查看大图：" + (img.alt || "配图"));
    img.addEventListener("click", () => openGuideLightbox(img));
  });
  // 宽表格在窄屏横向滚动，不撑破卡片
  root.querySelectorAll("table").forEach((table) => {
    const wrap = document.createElement("div");
    wrap.className = "guide-table-wrap";
    table.replaceWith(wrap);
    wrap.appendChild(table);
  });
}

async function ensureGuide() {
  if (guidePromise) return guidePromise;
  guidePromise = (async () => {
    const body = $("guideBody"), nav = $("guideToc");
    try {
      const data = await (await apiFetch("/guide")).json();
      // html 由服务端从仓库内的 guide.md 渲染（只读本地文件，不含用户输入），
      // 这里用 innerHTML 是有意为之；任何「用户可控文本」都不许走这条路。
      body.innerHTML = data.html || "";
      nav.replaceChildren(guideTocList(data.toc || []));
      guideHeadings = Array.from(body.querySelectorAll("h2[id], h3[id]"));
      bindGuideContent(body);
      if (!guideSpyBound) {
        guideSpyBound = true;
        const queueSync = () => {
          if (curPage !== "guide" || guideScrollTick) return;
          guideScrollTick = true;
          requestAnimationFrame(() => {
            guideScrollTick = false;
            syncGuideActive();
          });
        };
        $("guideBody").addEventListener("scroll", queueSync, { passive: true });
        window.addEventListener("scroll", queueSync, { passive: true });
      }
    } catch (err) {
      guidePromise = null;      // 允许下次进页面重试
      const p = document.createElement("p");
      p.className = "guide-state";
      p.textContent = "使用指南加载失败：" + (err.message || err);
      body.replaceChildren(p);
      nav.replaceChildren();
    }
  })();
  return guidePromise;
}

$("guideLightbox").addEventListener("click", (ev) => {
  if (ev.target === ev.currentTarget || ev.target.closest("#guideLightboxClose")) {
    closeGuideLightbox();
  }
});
/* 灯箱打开时吃掉 Esc / 空格 / 回车：空格原本是“播放暂停”，不能穿透到底层页面 */
document.addEventListener("keydown", (ev) => {
  const mask = $("guideLightbox");
  if (!mask || mask.hidden) return;
  if (ev.key === "Escape" || ev.key === " " || ev.key === "Enter") {
    ev.preventDefault();
    ev.stopPropagation();
    closeGuideLightbox();
  }
}, true);

/* ---------------- preset manage page：三栏工作台 ----------------
   左栏筛选目录 / 中栏卡片墙 / 右栏检视面板（未选中时退回创建表单）。

   与旧版三处关键差异：
   1) 点卡片不再「立刻应用并跳回合成页」，而是选中并在右栏展开详情——管理页的首要动作
      是看清这条预设是什么，不是一进来就被弹走。要一步到位用卡片上的「应用」。
   2) 详情一次性预取齐（并发 6），卡片一渲染就带情绪色/音频/参数，筛选计数也才准；
      旧的逐个 .then 会让卡片先空后跳。
   3) 右栏永远有内容：选中→详情，未选中→创建表单。旧版这栏只放了两句免责声明。 */
const pmCards = $("pmCards"), pmHint = $("pmHint"), pmSearch = $("pmSearch");
let pmListCache = [];       // 全部预设名
let pmSelName = "";         // 右栏正在检视的预设（≠ 已应用的 curPresetName）
let pmFilter = "all";       // all | audio | noaudio
let pmEmoFilter = -1;       // -1 = 不限；0..3 = emo_control_method
const pmDetailMap = new Map();

function pmFlash(msg, isErr) {
  pmHint.textContent = msg;
  pmHint.style.color = isErr ? cssVar("--danger") : "";
}
const EMO_MODE_NAMES = ["与音色参考相同", "情感参考音频", "情感向量", "情感描述文本"];
const EMO_MODE_COLORS = ["#8a8580", "#3f6fb5", "#7c5cc4", "#d07a2a"];
const EMO_NAMES8 = ["喜悦", "愤怒", "悲伤", "恐惧", "厌恶", "低落", "惊喜", "平静"];

function pmNum(v, dec = 2) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(dec) : "—";
}
function pmEmoOf(d) {
  const m = d?.emo_control_method;
  return {
    m,
    label: EMO_MODE_NAMES[m] ?? "未知模式",
    color: EMO_MODE_COLORS[m] ?? "var(--faint)",
  };
}

async function pmLoadDetail(name) {
  return presetDetail(name);
}

/* 并发上限 6，别让 50 条预设把浏览器连接池占满 */
async function pmEnsureDetails(names) {
  const todo = names.filter((n) => !pmDetailMap.has(n));
  if (!todo.length) return;
  let i = 0;
  const worker = async () => {
    while (i < todo.length) {
      const n = todo[i++];
      try {
        const d = await pmLoadDetail(n);
        // 失败就不写进 map：todo 是用 `!pmDetailMap.has(n)` 过滤的，
        // 写个 null 进去等于把这条永久标记为「已加载」，下次刷新永远跳过它。
        if (d != null) pmDetailMap.set(n, d);
      } catch { /* 留空，下次刷新会重试 */ }
    }
  };
  await Promise.all(Array.from({ length: Math.min(6, todo.length) }, worker));
}

async function refreshPmList() {
  try {
    const r = await (await apiFetch("/presets")).json();
    pmListCache = r.presets || [];
    await pmEnsureDetails(pmListCache);
    if (pmSelName && !pmListCache.includes(pmSelName)) pmSelName = "";
    pmClampSelection();   // 列表可能刚变过，选中项要跟着收敛到可见集合
    renderPmNav();
    renderPmCards();
    renderInspector(pmSelName);
  } catch (err) {
    pmFlash("刷新预设失败: " + (err.message || err), true);
  }
}

function pmVisibleNames() {
  const q = (pmSearch.value || "").trim().toLowerCase();
  return pmListCache.filter((n) => {
    if (q && !n.toLowerCase().includes(q)) return false;
    const d = pmDetailMap.get(n);
    if (pmFilter === "audio") return !!d?.prompt_audio_url;
    if (pmFilter === "noaudio") return !!d && !d.prompt_audio_url;
    if (pmEmoFilter >= 0) return d?.emo_control_method === pmEmoFilter;
    return true;
  });
}

/* 选中项必须落在可见集合里：不合法就退到第一条可见（进页面时 pmSelName 为空，
   也由这里落到第一条，右栏才有内容可看）。可见集合为空（搜索无匹配）时保留
   原选中，避免打字打到一半右栏跳空。 */
function pmClampSelection() {
  const names = pmVisibleNames();
  if (names.length && !names.includes(pmSelName)) pmSelName = names[0];
}

function renderPmNav() {
  const hasAudio = (n) => !!pmDetailMap.get(n)?.prompt_audio_url;
  const base = [
    ["all", "全部预设", pmListCache.length, null],
    ["audio", "含参考音频", pmListCache.filter(hasAudio).length, null],
    ["noaudio", "仅参数", pmListCache.filter((n) => pmDetailMap.has(n) && !hasAudio(n)).length, null],
  ];
  $("pmNav").replaceChildren(...base.map(([k, label, n]) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "pm-nav-row" + (pmFilter === k ? " cur" : "");
    const s = document.createElement("span");
    s.textContent = label;
    const cnt = document.createElement("b");
    cnt.textContent = n;
    b.append(s, cnt);
    b.addEventListener("click", () => {
      pmFilter = k; pmEmoFilter = -1;
      renderPmNav(); pmRepaint();
    });
    return b;
  }));

  $("pmNavEmo").replaceChildren(...EMO_MODE_NAMES.map((label, i) => {
    const n = pmListCache.filter((nm) => pmDetailMap.get(nm)?.emo_control_method === i).length;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "pm-nav-row" + (pmEmoFilter === i ? " cur" : "");
    const dot = document.createElement("span");
    dot.className = "nav-dot";
    dot.style.background = EMO_MODE_COLORS[i];
    const s = document.createElement("span");
    s.textContent = label;
    const cnt = document.createElement("b");
    cnt.textContent = n;
    b.append(dot, s, cnt);
    b.addEventListener("click", () => {
      pmEmoFilter = pmEmoFilter === i ? -1 : i;
      pmFilter = "all";
      renderPmNav(); pmRepaint();
    });
    return b;
  }));
}

function renderPmCards() {
  const names = pmVisibleNames();
  // 全量时只报总数，筛选/搜索后报「命中 / 总数」；这是页面上唯一一处数量
  $("pmShown").textContent = names.length === pmListCache.length
    ? `共 ${names.length} 个预设`
    : `${names.length} / ${pmListCache.length} 个预设`;
  pmCards.replaceChildren();

  if (!names.length) {
    const empty = document.createElement("div");
    empty.className = "pm-empty";
    const q = (pmSearch.value || "").trim();
    empty.textContent = q
      ? `没有匹配「${q}」的预设`
      : (pmListCache.length ? "当前筛选下没有预设" : "暂无预设，可在右侧创建第一条");
    pmCards.appendChild(empty);
    return;
  }

  for (const name of names) {
    const d = pmDetailMap.get(name);
    const card = document.createElement("div");
    card.className = "pm-card" + (name === pmSelName ? " cur" : "");
    card.tabIndex = 0;
    card.innerHTML = `
      <div class="pm-card-head"><span class="pm-dot"></span><span class="pm-name"></span></div>
      <div class="pm-row"><span class="pm-tag pm-flag"></span></div>
      <span class="pm-params"></span>
      <div class="pm-foot"><span class="pm-emo"></span><button class="pm-apply" type="button">应用</button></div>
      <span class="pm-bar">
        <span class="pm-acts">
          <button class="pm-act" data-act="rename" type="button" title="改名">改</button>
          <button class="pm-act" data-act="duplicate" type="button" title="复制">复</button>
        </span>
        <button class="pm-del" type="button" title="删除此预设">${ICON_CLOSE}</button>
      </span>`;
    card.querySelector(".pm-name").textContent = name;
    card.querySelector(".pm-name").title = name;   // 名称让位给操作按钮后会省略，hover 给全名
    card.querySelector(".pm-del").setAttribute("aria-label", "删除预设 " + name);

    const emo = pmEmoOf(d);
    card.querySelector(".pm-dot").style.background = emo.color;
    const emoEl = card.querySelector(".pm-emo");
    emoEl.textContent = emo.label + (name === curPresetName ? " · 已应用" : "");
    emoEl.title = emo.label;

    const flag = card.querySelector(".pm-flag");
    const params = card.querySelector(".pm-params");
    if (!d) {
      flag.textContent = "详情不可用";
      flag.classList.add("mute");
      params.textContent = "—";
    } else if (d.prompt_audio_url) {
      flag.textContent = "含音频";
      flag.classList.add("on");
    } else {
      flag.textContent = "无音频";
      flag.classList.add("mute");
    }
    if (d) {
      const adv = d.advanced_params || {};
      const bits = [];
      if (adv.temperature != null) bits.push(`temp ${pmNum(adv.temperature)}`);
      if (adv.top_p != null) bits.push(`top_p ${pmNum(adv.top_p)}`);
      if (adv.num_beams != null) bits.push(`beam ${adv.num_beams}`);
      params.textContent = bits.join(" · ") || "默认采样参数";
      params.title = params.textContent;
    }

    card.addEventListener("click", (e) => {
      if (e.target.closest(".pm-del") || e.target.closest(".pm-act")) return;
      if (e.target.closest(".pm-apply")) { pmApplyPreset(name); return; }
      pmSelect(name);
    });
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pmSelect(name); }
    });
    card.querySelectorAll(".pm-act").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        await pmPresetAction(name, btn.dataset.act);
      });
    });
    card.querySelector(".pm-del").addEventListener("click", async (e) => {
      e.stopPropagation();
      await pmPresetAction(name, "delete");
    });
    pmCards.appendChild(card);
  }
}

function pmSelect(name) {
  pmSelName = name;
  renderPmCards();
  renderInspector(name);
}

/* 筛选/搜索改变了「可见集合」后重绘卡片墙 + 右栏。
   与 pmSelect 的区别：这里不信任 pmSelName，被筛掉的选中项由 pmClampSelection
   自动收敛，否则右栏会一直挂着一个在卡片墙上根本不存在的预设。
   右栏只在选中项真的换了才重建——搜索是逐字触发的，每次都重建会把正在试听的
   参考音频掐断。 */
function pmRepaint() {
  const before = pmSelName;
  pmClampSelection();
  renderPmCards();
  if (pmSelName !== before) renderInspector(pmSelName);
}

async function pmApplyPreset(name) {
  if (!await applyPresetByName(name)) return;
  pmSelName = name;
  renderPmCards();
  renderInspector(name);
  switchPage("gen");
}

/* 右栏：选中→详情；未选中→创建表单，保证这一栏永远有内容 */
/* ---------------- 预设检视面板的小播放器 ----------------
   <audio controls> 在 Chrome / Firefox / Safari 是三种长相，压到 34px 高还会把
   播放键、音量、⋮ 挤成一团，和合成页的播放器完全不是一套控件。
   这里用同一套控件语言自绘：圆形播放键 + 蓝色进度 + 等宽时间。
   同一时刻只放一条，避免两条音频叠着响。 */
let miniAudioNow = null;   // 当前可能在响的那条

function clockSec(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/**
 * 紧凑播放器。返回一个 .audiorow 元素（内含隐藏的 <audio>）。
 * @param {string} url   音频地址
 * @param {string} label 播放按钮的可读名称，也是暂停后复原的 aria-label
 */
function miniPlayer(url, label) {
  const row = document.createElement("div");
  row.className = "audiorow";

  // 用原生 <audio> 只做解码与播放，控件全部自绘；preload=metadata 才能立刻显示总时长
  const audio = document.createElement("audio");
  audio.preload = "metadata";
  audio.src = url;
  audio.hidden = true;

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "iconbtn";
  btn.setAttribute("aria-label", label);
  btn.innerHTML = '<svg class="ico" aria-hidden="true"><use href="#i-play"></use></svg>';

  const track = document.createElement("div");
  track.className = "audiorow-track";
  track.title = "点击或拖动定位";
  const fill = document.createElement("i");
  fill.className = "audiorow-fill";
  track.appendChild(fill);

  const time = document.createElement("span");
  time.className = "audiorow-time";
  time.textContent = "0:00";

  row.append(btn, track, time, audio);

  let raf = 0;
  const paint = () => {
    const d = audio.duration;
    if (!Number.isFinite(d) || d <= 0) { time.textContent = "0:00"; return; }
    fill.style.width = `${Math.min(100, (audio.currentTime / d) * 100)}%`;
    time.textContent = `${clockSec(audio.currentTime)} / ${clockSec(d)}`;
  };
  const tick = () => { paint(); if (!audio.paused) raf = requestAnimationFrame(tick); };

  btn.addEventListener("click", () => {
    if (!audio.paused) { audio.pause(); return; }
    if (miniAudioNow && miniAudioNow !== audio) miniAudioNow.pause();
    miniAudioNow = audio;
    audio.play().catch(() => {});
  });
  audio.addEventListener("play", () => {
    btn.innerHTML = '<svg class="ico" aria-hidden="true"><use href="#i-pause"></use></svg>';
    btn.setAttribute("aria-label", "暂停");
    row.classList.add("playing");
    cancelAnimationFrame(raf);
    tick();
  });
  audio.addEventListener("pause", () => {
    btn.innerHTML = '<svg class="ico" aria-hidden="true"><use href="#i-play"></use></svg>';
    btn.setAttribute("aria-label", label);
    row.classList.remove("playing");
    cancelAnimationFrame(raf);
    paint();
  });
  audio.addEventListener("ended", () => { audio.currentTime = 0; paint(); });
  audio.addEventListener("loadedmetadata", paint);

  // 点击定位 + 按住拖动。用 pointer capture 才能在指针移出轨道后继续跟随，
  // 否则拖到边缘就断，手感像「点一下跳一下」。
  let dragging = false;
  const seekTo = (clientX) => {
    const d = audio.duration;
    if (!Number.isFinite(d) || d <= 0) return;
    const r = track.getBoundingClientRect();
    audio.currentTime = Math.min(Math.max((clientX - r.left) / r.width, 0), 1) * d;
    paint();
  };
  track.addEventListener("pointerdown", (e) => {
    dragging = true;
    try { track.setPointerCapture(e.pointerId); } catch (err) { /* 合成事件没有 pointerId */ }
    seekTo(e.clientX);
  });
  track.addEventListener("pointermove", (e) => { if (dragging) seekTo(e.clientX); });
  const endDrag = (e) => {
    dragging = false;
    try { track.releasePointerCapture(e.pointerId); } catch (err) { /* 未捕获时忽略 */ }
  };
  track.addEventListener("pointerup", endDrag);
  track.addEventListener("pointercancel", endDrag);

  return row;
}

function inspNote(text) {
  const p = document.createElement("p");
  p.className = "mg-hint";
  p.textContent = text;
  return p;
}

function renderInspector(name) {
  const insp = $("pmInsp"), create = $("pmCreateBox");
  const d = name ? pmDetailMap.get(name) : null;
  // 换选中项 / 列表刷新都可能重建面板，先把还在响的那条停掉，否则元素被摘掉后声音还在
  if (miniAudioNow) miniAudioNow.pause();
  if (!name || !d) {
    insp.hidden = true;
    create.hidden = false;
    return;
  }
  create.hidden = true;
  insp.hidden = false;

  const emo = pmEmoOf(d);
  $("inspDot").style.background = emo.color;
  $("inspName").textContent = name;
  $("inspName").title = name;

  const badges = [];
  const emoTag = document.createElement("span");
  emoTag.className = "pm-tag";
  emoTag.textContent = emo.label;
  emoTag.style.color = emo.color;
  emoTag.style.borderColor = `color-mix(in srgb, ${emo.color} 40%, transparent)`;
  badges.push(emoTag);
  const audioTag = document.createElement("span");
  audioTag.className = "pm-tag " + (d.prompt_audio_url ? "on" : "mute");
  audioTag.textContent = d.prompt_audio_url ? "含音频" : "无音频";
  badges.push(audioTag);
  if (name === curPresetName) {
    const applied = document.createElement("span");
    applied.className = "pm-tag on";
    applied.textContent = "已应用";
    badges.push(applied);
  }
  $("inspBadges").replaceChildren(...badges);

  // 音色：有音频就给能直接听的播放器，没有就明说会沿用当前音色
  const audioBox = $("inspAudio");
  audioBox.replaceChildren();
  if (d.prompt_audio_url) {
    audioBox.append(
      miniPlayer(d.prompt_audio_url, "播放参考音频"),
      inspNote("预设自带的参考音频，应用后自动回填到合成页"),
    );
  } else {
    const none = document.createElement("div");
    none.className = "insp-audio-none";
    none.textContent = "无参考音频。应用后沿用合成页当前的音色，只替换参数。";
    audioBox.appendChild(none);
  }

  // 情感
  const emoBox = $("inspEmo");
  emoBox.replaceChildren();
  const m = d.emo_control_method;
  if (m === 2) {
    const vec = Array.isArray(d.emo_vector) ? d.emo_vector.map(Number) : [];
    const box = document.createElement("div");
    box.className = "insp-vec";
    EMO_NAMES8.forEach((label, i) => {
      // ?? 挡不住 NaN：Math.min(1, NaN) 仍是 NaN，会渲染成 width:NaN%（无效 CSS，进度条塌成 0）
      const v = Math.max(0, Math.min(1, Number.isFinite(vec[i]) ? vec[i] : 0));
      const row = document.createElement("div");
      row.innerHTML = `<span>${label}</span><i><b style="width:${(v * 100).toFixed(1)}%"></b></i><em>${v.toFixed(2)}</em>`;
      box.appendChild(row);
    });
    emoBox.append(box, inspNote("8 维情感向量，数值越大该情绪越强"));
  } else if (m === 3) {
    const t = document.createElement("div");
    t.className = "insp-emotext val";
    t.textContent = d.emo_text || "（未填写描述文本）";
    emoBox.appendChild(t);
  } else if (m === 1) {
    if (d.emo_audio_url) {
      emoBox.appendChild(miniPlayer(d.emo_audio_url, "播放情感参考音频"));
    } else {
      const none = document.createElement("div");
      none.className = "insp-audio-none";
      none.textContent = "未保存情感参考音频";
      emoBox.appendChild(none);
    }
  } else {
    const t = document.createElement("div");
    t.className = "insp-emotext";
    t.textContent = "与音色参考音频相同，不额外指定情感";
    emoBox.appendChild(t);
  }

  // 采样参数
  const adv = d.advanced_params || {};
  const rows = [
    ["temperature", pmNum(adv.temperature)],
    ["top_p", pmNum(adv.top_p)],
    ["top_k", adv.top_k ?? "—"],
    ["num_beams", adv.num_beams ?? "—"],
    ["length_penalty", pmNum(adv.length_penalty)],
    ["repetition_penalty", pmNum(adv.repetition_penalty)],
    ["max_mel_tokens", adv.max_mel_tokens ?? "—"],
    ["分句 Token 上限", adv.max_text_tokens_per_segment ?? "—"],
    ["段间停顿 ms", adv.interval_silence ?? "—"],
  ];
  $("inspParams").replaceChildren(...rows.map(([k, v]) => {
    const row = document.createElement("div");
    const s = document.createElement("span");
    s.textContent = k;
    const b = document.createElement("b");
    b.textContent = v;
    row.append(s, b);
    return row;
  }));
}


/* 预设操作统一处理：rename / duplicate / delete。
   刷新两处列表（管理页 + 生成页库区）并同步各处缓存与选中态。 */
async function pmPresetAction(name, act) {
  try {
    if (act === "rename") {
      const input = await uiPrompt({ title: "预设改名", placeholder: "新名称", value: name });
      if (input === null || input === false) return;
      const trimmed = String(input).trim();
      if (!trimmed) { pmFlash("新名称不能为空", true); return; }
      if (trimmed === name) return;
      const r = await fetch(`${API}/presets/` + encodeURIComponent(name) + "/rename", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: trimmed }),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "改名失败");
      const j = await r.json();
      presetDetailCache.delete(name);
      pmDetailMap.delete(name);
      const renamed = j.name || trimmed;
      if (pmSelName === name) pmSelName = renamed;
      if (curPresetName === name) { curPresetName = renamed; updateGenSummary(); }
      pmFlash(`已改名: ${name} → ${j.name || trimmed}`);
    } else if (act === "duplicate") {
      const r = await fetch(`${API}/presets/` + encodeURIComponent(name) + "/duplicate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "复制失败");
      const j = await r.json();
      pmFlash(`已复制为: ${j.name}`);
    } else if (act === "delete") {
      if (!await uiConfirm({ title: "删除预设", body: `确定删除预设「${name}」？此操作不可恢复。`, okText: "删除", danger: true })) return;
      const r = await fetch(`${API}/presets/` + encodeURIComponent(name), { method: "DELETE" });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "删除失败");
      presetDetailCache.delete(name);
      pmDetailMap.delete(name);
      if (pmSelName === name) pmSelName = "";
      if (curPresetName === name) { curPresetName = ""; updateGenSummary(); }
      pmFlash("已删除预设: " + name);
    }
    await refreshPmList();
    await refreshPresets();
    renderLib();
  } catch (err) { pmFlash("操作失败: " + (err.message || err), true); }
}
/* 搜索只改变可见集合，选中项的合法性由 pmRepaint 统一收口 */
pmSearch.addEventListener("input", pmRepaint);
$("btnPmRefresh").addEventListener("click", () => {
  presetDetailCache.clear();
  pmDetailMap.clear();
  refreshPmList();
  pmFlash("已刷新");
});
/* 顶栏新建 = 把右栏切回创建表单并聚焦输入框（右栏本来就是它的归宿） */
$("btnPmNew").addEventListener("click", () => {
  pmSelName = "";
  renderPmCards();
  renderInspector("");
  $("pmName").focus();
});
$("inspApply").addEventListener("click", () => { if (pmSelName) pmApplyPreset(pmSelName); });
$("inspRename").addEventListener("click", () => { if (pmSelName) pmPresetAction(pmSelName, "rename"); });
$("inspDup").addEventListener("click", () => { if (pmSelName) pmPresetAction(pmSelName, "duplicate"); });
$("inspDel").addEventListener("click", () => { if (pmSelName) pmPresetAction(pmSelName, "delete"); });
$("btnPmCreate").addEventListener("click", async () => {
  const name = $("pmName").value.trim();
  if (!name) { pmFlash("请输入预设名称", true); return; }
  try {
    if (!await savePresetByName(name)) return;
    $("pmName").value = "";
    // 同名覆盖时必须先失效缓存再刷新，否则 pmEnsureDetails 会命中旧详情
    presetDetailCache.delete(name);
    pmDetailMap.delete(name);
    pmSelName = name;
    await refreshPmList();
    await refreshPresets();
    pmFlash("预设已创建: " + name);
  } catch (e) { pmFlash("创建失败: " + (e.message || e), true); }
});

/* ---------------- model manage page ---------------- */
const mmState = $("mmState"), mmRuntime = $("mmRuntime"), mmErr = $("mmErr");
const CFG_KEYS = ["fp16", "s2mel_fp16", "w2v_fp16", "qwen_fp16", "cudnn_benchmark"];
const CFG_BOX = {
  fp16: "cfgFp16", s2mel_fp16: "cfgS2melFp16", w2v_fp16: "cfgW2vFp16", qwen_fp16: "cfgQwenFp16", cudnn_benchmark: "cfgCudnnBench",
};
const mmCfgSliders = [
  { id: "cfgSteps", out: "cfgStepsOut", dec: 0 },
  { id: "cfgRate", out: "cfgRateOut", dec: 2 },
];
for (const s of mmCfgSliders) {
  const el = $(s.id), out = $(s.out);
  const upd = () => { out.textContent = Number(el.value).toFixed(s.dec); sliderFill(el); };
  el.addEventListener("input", upd);
  upd();
}

/* 分段批处理三档：逐段生成(0) / 自动提速(-1) / 自己定段数(2~12)。
   提交值只在「保存配置」或「重启并应用配置」时发出，与同组 cuDNN 一样保存即生效；
   行数是每次生成开头读取的标量，不重启就能 A/B。 */
const CFG_BATCH_SEG = $("cfgBatchSeg");
const CFG_BATCH_ROWS = $("cfgBatchRows");
let cfgBatchMode = "off";
let cfgBatchRows = 3;
let mmBatchState = null;            // /model 返回的 {preview, last}，用来解释「本机实际」

function cfgBatchValue() {
  if (cfgBatchMode === "auto") return -1;
  if (cfgBatchMode === "manual") return cfgBatchRows;
  return 0;
}
function setBatchMode(mode) {
  cfgBatchMode = mode;
  for (const b of CFG_BATCH_SEG.querySelectorAll("button[data-mode]")) {
    b.setAttribute("aria-checked", String(b.dataset.mode === mode));
  }
  $("cfgBatchRowsRow").hidden = mode !== "manual";
}
function setBatchRows(n) {
  cfgBatchRows = Math.max(2, Math.min(12, n | 0));
  CFG_BATCH_ROWS.value = cfgBatchRows;
  $("cfgBatchRowsOut").textContent = cfgBatchRows;
  sliderFill(CFG_BATCH_ROWS);
}
/* 「本机实际档位」：自动模式按当前空闲显存能拼几行 + 上一次生成实际怎么批的。
   没有这行，用户选了「自动」也不知道自己吃到 1 行还是 8 行，退档原因也看不见。 */
function batchLiveText() {
  const p = (mmBatchState || {}).preview;
  const last = (mmBatchState || {}).last;
  const bits = [];
  if (cfgBatchMode === "auto") {
    // 「预算 0 GiB」单独出现时用户只会以为档位坏了：真正决定能不能拼的是
    // CUDA 可分配量（nvidia-smi 的空闲通常更大，差额是进程保留的块）。
    bits.push(p
      ? `本机自动 ≈ ${p.auto_rows} 行${p.auto_rows <= 1
          ? Number.isFinite(p.free_gib)
            ? `（CUDA 可分配 ${p.free_gib} GiB，低于 1 GiB 安全余量）`
            : "（显存不足，等同逐段）"
          : `（预算 ${p.budget_gib} GiB）`}`
      : "加载模型后显示本机行数");
  } else if (cfgBatchMode === "manual") {
    bits.push(p
      ? `每批 ${cfgBatchRows} 段${cfgBatchRows > p.auto_rows ? `，本机最多 ${p.auto_rows} 行（会被压到 ${p.auto_rows}）` : `，本机可拼 ${p.auto_rows} 行`}`
      : `每批 ${cfgBatchRows} 段`);
  } else {
    bits.push("当前：逐段生成");
  }
  if (last && last.segments) {
    // 服务端只在「真的拼了批」时填 groups（批数），逐段路径 groups=0。
    // 先按 groups 分流，再在同一路径里解释 reason，退档原因不会被
    // 「几段几批」的文案挤掉（BUG-010）。
    const rows = Number(last.rows) || 0;
    const requested = Number(last.rows_requested) || 0;
    const groupCount = Number(last.groups) || 0;
    if (groupCount > 0) {
      const groupBit = groupCount > 1 ? ` · ${last.segments} 段 → ${groupCount} 批` : "";
      bits.push(last.reason === "clamped"
        ? `上次：按显存压到每批 ${rows} 行${requested && requested !== rows ? `（请求 ${requested} 行）` : ""}${groupBit}`
        : `上次：每批 ${rows} 行${groupBit}`);
    } else if (last.reason === "streaming") {
      bits.push("上次：流式生成，未启用");
    } else if (last.reason === "single_segment") {
      bits.push("上次：单段，未启用");
    } else if (last.reason === "low_vram") {
      bits.push(`上次：显存不足，${last.segments} 段退回逐段`);
    } else {
      bits.push(`上次：${last.segments} 段逐段生成`);
    }
  }
  return bits.join(" · ");
}
/** 控件（而非服务端）决定的批处理档位：逐段=0 / 自动=-1 / 手选=2~12 */
function applyBatchControls(s) {
  const size = Number.isInteger(s.gpt_batch_size) ? s.gpt_batch_size : 0;
  if (size >= 2) { setBatchRows(size); setBatchMode("manual"); }
  else if (size < 0) setBatchMode("auto");
  else setBatchMode("off");
}
CFG_BATCH_SEG.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-mode]");
  if (!b) return;
  setBatchMode(b.dataset.mode);
  $("cfgBatchLive").textContent = batchLiveText();
});
CFG_BATCH_ROWS.addEventListener("input", () => {
  setBatchRows(parseInt(CFG_BATCH_ROWS.value, 10));
  $("cfgBatchLive").textContent = batchLiveText();
});
function mmFlashErr(msg) { mmErr.textContent = msg; mmErr.classList.toggle("on", !!msg); }
/* 生成中时，模型页要能提前说出「现在动模型会打断生成」。
   加载模型 / 重启模型会重建模型、必然打断生成，生成中一律置灰；
   卸载模型不同——后端支持 force=1 打断后直接卸载（不重建），所以保持可点，
   由确认框告知后果。用户已经决定要卸载了，不该被拒绝后再点一次。 */
let mmGenerating = false;
const mmBusyBtns = new Set();      // withBusyBtn 正在用的按钮，2 秒轮询不许改它们的 disabled
function applyModelBusy(s) {
  mmGenerating = !!s.generating;
  const hint = mmGenerating
    ? "正在生成语音，此刻加载/重启模型会打断本次生成"
    : "";
  for (const id of ["btnModelLoad", "btnModelRestart"]) {
    const b = $(id);
    if (!mmBusyBtns.has(b)) b.disabled = mmGenerating;
    b.title = hint;
  }
  $("btnModelUnload").title = mmGenerating ? "将中断当前生成后卸载模型（会先确认）" : "";
}
/* 模型加载/重启期间的实时耗时提示，写在状态位上；返回 interval id，调用方负责 clearInterval */
let mmElapsedActive = false;   // 秒表运行中：轮询渲染不许覆盖状态文案
function mmElapsedTimer(label) {
  const t0 = Date.now();
  mmElapsedActive = true;
  mmState.textContent = `${label}… 0s`;
  mmState.className = "mono";
  const box = $("mmStateBox");
  if (box) box.dataset.s = "loading";
  return setInterval(() => {
    mmState.textContent = `${label}… ${Math.round((Date.now() - t0) / 1000)}s`;
  }, 500);
}
/* ---------------- 模型页：未保存判定 + 状态同步（BUG-015 / BUG-017） ---------
   状态卡跟着 Header 的 2 秒轮询走：另一标签页 / CLI / 直接调 API 改了模型状态，
   这一屏不再自相矛盾（顶部 pill 说未加载、卡片还说常驻显存）。
   控件则只在「没有未保存改动」时才被服务端值写回——否则用户正在填的步数会被
   轮询悄悄吞掉，比原来只在切页时覆盖更糟。 */
let mmServerCfg = null;      // 最近一次写进控件的服务端配置，作为差异基准

function mmFormCfg() {
  const cfg = {};
  for (const k of CFG_KEYS) { const el = $(CFG_BOX[k]); cfg[k] = !!(el && el.checked); }
  cfg.diffusion_steps = parseInt($("cfgSteps").value, 10);
  cfg.inference_cfg_rate = parseFloat($("cfgRate").value);
  cfg.gpt_batch_size = cfgBatchValue();
  return cfg;
}
function mmCfgDirty() {
  if (!mmServerCfg) return false;
  const cur = mmFormCfg();
  return Object.keys(cur).some((k) => {
    const a = cur[k], b = mmServerCfg[k];
    // 空输入框读出来是 NaN，两边都 NaN 时算「没改」，否则永远脏
    if (typeof a === "number" && typeof b === "number")
      return !(a === b || (Number.isNaN(a) && Number.isNaN(b)));
    return a !== b;
  });
}
/** 把服务端配置写进控件，并以此重置差异基准。 */
function writeCfgControls(s) {
  for (const k of CFG_KEYS) { const el = $(CFG_BOX[k]); if (el) el.checked = !!s[k]; }
  $("cfgSteps").value = s.diffusion_steps ?? 25;
  $("cfgRate").value = s.inference_cfg_rate ?? 0.7;
  for (const s2 of mmCfgSliders) {
    const el = $(s2.id), out = $(s2.out);
    out.textContent = Number(el.value).toFixed(s2.dec);
    sliderFill(el);
  }
  applyBatchControls(s);
  mmServerCfg = mmFormCfg();
}
/** 用一份 /model 或 /status 的返回值渲染模型页（不再单独发请求）。 */
function renderModelPage(s) {
  applyModelBusy(s);
  const phase = s.phase || (s.loaded ? "ready" : "unloaded");
  const stateNames = {
    ready: "已加载", loading: "加载中", reloading: "重载中",
    unloading: "卸载中", error: "加载失败", unloaded: "未加载",
  };
  // 操作进行中时状态位由秒表接管：2 秒轮询的返回值比秒表旧，
  // 覆盖一次就成了「加载中… 12s」闪回「加载中」，看着像计时丢了。
  if (!mmElapsedActive) mmState.textContent = stateNames[phase] || stateNames.unloaded;
  // 颜色/状态灯交给父级 data-s，不再用 className 覆盖（否则会清掉 mono）
  const stateBox = $("mmStateBox");
  if (stateBox) {
    stateBox.dataset.s = phase === "ready" ? "ready"
      : phase === "error" ? "error"
      : phase === "unloaded" ? "off" : "loading";
  }
  const stateSub = $("mmStateSub");
  if (stateSub) {
    stateSub.textContent = mmGenerating
      ? "正在生成 · 模型操作已锁定"
      : ({
        ready: "常驻显存，可直接合成",
        loading: "正在加载权重…",
        reloading: "正在重载模型…",
        unloading: "正在释放显存…",
        error: "请查看下方错误信息",
        unloaded: "首次生成时自动加载",
      }[phase] || "");
  }
  // 有未保存改动时只刷状态区，控件保持用户填的内容；「本机实际档位」用的是
  // 服务端刚返回的档位，与控件无关，照常更新。
  if (!mmCfgDirty()) writeCfgControls(s);
  mmBatchState = s.gpt_batch || null;
  $("cfgBatchLive").textContent = batchLiveText();
  mmRuntime.textContent = [
    `FP16=${s.fp16}  S2MEL_FP16=${s.s2mel_fp16}`,
    `W2V_FP16=${s.w2v_fp16}  QWEN_FP16=${s.qwen_fp16}`,
    `CUDNN_BENCHMARK=${s.cudnn_benchmark}`,
    `DIFFUSION_STEPS=${s.diffusion_steps}  CFG_RATE=${s.inference_cfg_rate}`,
    `GPT_BATCH=${s.gpt_batch_size}`,
  ].join("\n");
}
async function refreshModelPage(keepError = false) {
  try {
    renderModelPage(await (await apiFetch("/model")).json());
    if (!keepError) mmFlashErr("");
  } catch (e) {
    mmFlashErr("无法读取模型状态: " + (e.message || e));
  }
}
/* 模型管理这 4 个按钮统一走这里发请求。

   为什么要抽出来：这些操作可能要排队等推理锁（生成语音进行中时，卸载/重启
   都要等），实测一次生成 85.8 秒、期间点保存配置阻塞 60.76 秒。而原来
   「卸载模型」和「保存配置」点了完全没反应——既不置灰也不改文案，用户以为
   没点上就反复点；保存配置的「已保存 ✓」还要等响应回来才出现，那 60 秒里
   页面看起来跟没点过一样。

   所以统一成：请求发出前立刻置灰 + 换文案，状态区同步走秒表，结束后还原。
   onDone 在按钮还原之后调用，用来放「已保存 ✓」这类收尾文案。 */
async function withBusyBtn(btn, busyLabel, run, onDone) {
  if (btn.disabled) return;                 // 同一按钮不重复提交
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = busyLabel;
  mmBusyBtns.add(btn);                      // 挡住 2 秒轮询的 applyModelBusy
  mmFlashErr("");
  const iv = mmElapsedTimer(busyLabel.replace(/…$/, ""));
  let failed = false;
  try {
    await run();
  } catch (e) {
    failed = true;
    mmFlashErr(e.message || String(e));
  } finally {
    clearInterval(iv);
    mmElapsedActive = false;
    mmBusyBtns.delete(btn);
    btn.disabled = false;
    btn.textContent = old;
  }
  await refreshModelPage(failed);           // 成功时清掉计时/旧错误；失败时保留原因
  if (!failed && onDone) onDone();
}

$("btnModelLoad").addEventListener("click", () => withBusyBtn(
  $("btnModelLoad"), "加载中…", async () => {   // 首次加载 30~120s
    const j = await (await apiFetch("/model/load", { method: "POST" })).json().catch(() => ({}));
    if (!j.ok) throw new Error(j.error || j.detail || "加载失败");
    pollStatus();
  }));
$("btnModelUnload").addEventListener("click", async () => {
  // 生成中：后端支持 force=1 打断后直接卸载（跳过重建），所以不拒绝，
  // 换一句把后果说清楚的确认框。用户已经决定要卸载，不该被拒后再点一次。
  if (mmGenerating) {
    const ok = await uiConfirm({
      title: "卸载会中断当前生成",
      body: "正在生成语音。卸载会中断本次生成，已生成的部分结果将丢弃；"
          + "下次生成需要重新加载模型（约 30~60 秒）。确定继续吗？",
      okText: "中断并卸载", danger: true,
    });
    if (!ok) return;
    await withBusyBtn($("btnModelUnload"), "停止并卸载中…", async () => {
      const j = await (await apiFetch("/model/unload?force=1", { method: "POST" })).json().catch(() => ({}));
      if (!j.ok) throw new Error(j.error || j.detail || "卸载失败");
      pollStatus();
    });
    return;
  }
  if (!await uiConfirm({ title: "卸载模型", body: "卸载后首次生成需重新加载（约 30~60 秒）。", okText: "卸载" })) return;
  await withBusyBtn($("btnModelUnload"), "卸载中…", async () => {
    const j = await (await apiFetch("/model/unload", { method: "POST" })).json().catch(() => ({}));
    if (!j.ok) throw new Error(j.error || j.detail || "卸载失败");
    pollStatus();
  });
});
$("btnModelRestart").addEventListener("click", async () => {
  if (!await uiConfirm({ title: "重启模型", body: "将应用当前页面上的加速配置，期间无法生成。", okText: "重启" })) return;
  await withBusyBtn($("btnModelRestart"), "重启中…", async () => {
    const body = {};
    for (const k of CFG_KEYS) body[k] = $(CFG_BOX[k]).checked;
    body.diffusion_steps = parseInt($("cfgSteps").value, 10);
    body.inference_cfg_rate = parseFloat($("cfgRate").value);
    body.gpt_batch_size = cfgBatchValue();
    const j = await (await apiFetch("/model/restart", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })).json().catch(() => ({}));
    if (!j.ok) throw new Error(j.error || j.detail || "重启失败");
    pollStatus();
    // 重启把页面上这些值一并应用了，与「保存配置」同样认账，别让轮询以为还有未保存改动
    mmServerCfg = mmFormCfg();
  });
});
/* 推荐配置：FP16 + S2MEL_FP16 开（几乎无损、显著提速），W2V_FP16 开（8G 卡省 ~1GB 显存），
   QWEN_FP16 开，cuDNN 自动调优关（实测负优化：BigVGAN 3.35s → 97s）；步数 25、CFG 0.7 */
const RECOMMEND_CFG = {
  s2mel_fp16: true, fp16: true, w2v_fp16: true, qwen_fp16: true, cudnn_benchmark: false,
  diffusion_steps: 25, inference_cfg_rate: 0.7,
};
$("btnPresetCfg").addEventListener("click", () => {
  for (const k of CFG_KEYS) {
    const el = $(CFG_BOX[k]);
    if (el) el.checked = !!RECOMMEND_CFG[k];
  }
  $("cfgSteps").value = RECOMMEND_CFG.diffusion_steps;
  $("cfgSteps").dispatchEvent(new Event("input", { bubbles: true }));
  $("cfgRate").value = RECOMMEND_CFG.inference_cfg_rate;
  $("cfgRate").dispatchEvent(new Event("input", { bubbles: true }));
  // 批处理不进推荐配置：它会改变输出字节（本来就是随机采样），要不要提速由用户单独决定
  setBatchMode("off");
  $("cfgBatchLive").textContent = batchLiveText();
  mmFlashErr("");
});

$("btnCfgSave").addEventListener("click", () => {
  const body = {};
  for (const k of CFG_KEYS) body[k] = $(CFG_BOX[k]).checked;
  body.diffusion_steps = parseInt($("cfgSteps").value, 10);
  body.inference_cfg_rate = parseFloat($("cfgRate").value);
  body.gpt_batch_size = cfgBatchValue();
  return withBusyBtn($("btnCfgSave"), "保存中…", async () => {
    try {
      const j = await (await apiFetch("/model/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })).json().catch(() => ({}));
      if (!j.ok) throw new Error(j.detail || j.error || "服务未返回失败原因");
    } catch (e) {
      throw new Error("保存配置失败: " + (e.message || e));
    }
    pollStatus();
    // 保存成功即认账：服务端现在就是控件里这些值，基准跟着走，
    // 否则轮询会一直以为「还有未保存改动」，把控件永久锁住。
    mmServerCfg = mmFormCfg();
  }, () => {
    const save = $("btnCfgSave");
    save.textContent = "已保存 ✓";
    setTimeout(() => { save.textContent = "保存配置"; }, 1600);
  });
});

/* ---------------- keyboard shortcuts ----------------
   Ctrl+Enter        生成语音（生成页可见时）
   Space             播放/暂停当前结果（焦点不在输入框时）
   Ctrl+S            保存当前为预设
   Esc               关闭模态框（已由 _modalSetup 注册） */
document.addEventListener("keydown", (e) => {
  // 模态框打开时不响应快捷键（Esc 关窗已由各自弹窗处理）。
  // 这里必须查「任意 .modal-mask」而不是只认 modalMask：裁剪弹窗是第二个模态层，
  // 只盯 id 的话在裁剪窗口里按 Ctrl+Enter 会把背后的表单提交去生成。
  if (document.querySelector(".modal-mask:not([hidden])")) return;
  // 输入控件里只放行 Ctrl+Enter / Ctrl+S（修饰键组合不与打字冲突）
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    if (!genBtn.disabled) genBtn.click();
    return;
  }
  if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) {
    e.preventDefault();
    $("btnLibSave").click();
    return;
  }
  // 焦点在按钮或 <summary> 上时，空格是它们的标准激活键（WCAG）。
  // 这里若抢走，Tab 到「生成语音」「保存预设」或任意折叠标题按空格就会
  // 去播放音频而不是触发该控件 —— 键盘操作直接错乱。
  const a = document.activeElement;
  // 历史条目/示例条目是加了 role="button" 的 <div>，空格同样是它们的激活键
  const spaceOwned = a && (a.tagName === "BUTTON" || a.tagName === "SUMMARY"
    || a.tagName === "A" || a.getAttribute("role") === "button");
  if (e.key === " " && !typing && !spaceOwned && trim.buffer) {
    e.preventDefault();
    if (player.paused) { player.play().catch(() => {}); bigPlayIcon.innerHTML = PAUSE2; }
    else { player.pause(); bigPlayIcon.innerHTML = PLAY2; }
  }
});

/* 未捕获异常只进控制台：界面上已经有各自的错误文案，再加一个大横幅
   会和真实原因混在一起；但完全不留痕迹会让排查变成猜谜。 */
window.addEventListener("error", (e) => {
  console.error("[index-tts] 未捕获异常:", e.error || e.message,
    e.filename ? `${e.filename}:${e.lineno}:${e.colno}` : "");
});
window.addEventListener("unhandledrejection", (e) => {
  console.error("[index-tts] 未处理的 Promise 拒绝:", e.reason);
});

/* ---------------- init (single entry point) ---------------- */
applyTheme(lsGet("theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
/* 侧边栏初始状态：没有保存过偏好就按窗口宽度决定（宽屏展开，窄屏收起） */
const _savedHist = lsGet(HIST_KEY);
histSetCollapsed(_savedHist === null ? window.innerWidth < 1360 : _savedHist === "1", false);
refreshHistory();
bindParamEvents();
syncParams();
emoVisible();
updateSegments();
updateGenSummary();
switchLib(readDraft()?.libTab === "examples" ? "examples" : "presets");
(async () => {
  await Promise.all([restoreDraft(), refreshPresets(), loadExamples()]);
  // restoreDraft 与 refreshPresets 并发，草稿里的预设名可能晚于列表写入，
  // 所以两边都完成后再校验一次
  pruneMissingPreset();
  // 草稿恢复完再套锁定：锁定优先级最高，且先用本地缓存避免闪一下默认值
  loadParamLockFromCache();
  renderLib();
  restoreJob();
  loadParamLockFromServer(); // 服务端为准，校正本地缓存
})();
refreshPmList();
renderGlossary();
/* 起始页签跟 URL hash（#gen/#presets/#model/#guide，指南可带章节锚点），刷新后不丢当前页 */
const _start = parseHash();
switchPage(_start.page, false, _start.anchor);

const _ro = new ResizeObserver(() => {
  drawHeaderWave();
  if (spkBuffer) drawSpkWave();
  if (trim.buffer) drawTrimWave();
});
_ro.observe(hWave);
_ro.observe(spkWave);
_ro.observe(tWave);
