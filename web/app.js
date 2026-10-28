/* IndexTTS2 Studio frontend (v2) */
"use strict";

const $ = (id) => document.getElementById(id);

/* ---------------- API base ----------------
   所有 JSON 接口集中在 /api/v1 版本前缀下（旧根路径后端 307 兼容重定向）。
   音频/静态资源（/audio、/assets）不带前缀。 */
const API = "/api/v1";

/* 文本字数：软阈值 + 硬上限，与后端 server.py 同口径
   - TEXT_CHARS_SOFT 只做提示，超过照常生成
   - TEXT_CHARS_HARD 之后 textarea 会被 maxlength 挡住，服务端也会 400 */
const TEXT_CHARS_SOFT = 2000;
const TEXT_CHARS_HARD = 10000;

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
};
function _modalSetup() {
  if (_modal.mask) return;
  _modal.mask = $("modalMask");
  _modal.okBtn = $("modalOk");
  _modal.input = $("modalInput");
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
  _modal.prevFocus = document.activeElement;
  _modal.mask.hidden = false;
  (_modal.input.hidden ? _modal.okBtn : _modal.input).focus();
}
function _modalClose(ok) {
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

function draftClientId() {
  let id = sessionStorage.getItem("studioClientId");
  if (!id) {
    id = crypto.randomUUID();
    sessionStorage.setItem("studioClientId", id);
  }
  return id;
}
const CLIENT_ID = draftClientId();

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

function clearDraftBlobs() {
  return openDraftDb().then((db) => {
    const tx = db.transaction(DRAFT_STORE, "readwrite");
    const store = tx.objectStore(DRAFT_STORE);
    for (const key of Object.values(DRAFT_AUDIO_KEYS)) store.delete(key);
    return new Promise((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onabort = tx.onerror = () => reject(tx.error);
    });
  });
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

function applyDraft(draft) {
  if (!draft) return;
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
  $("charCount").textContent = `${$("text").value.trim().length} 字`;
  syncParams();
  emoVisible();
  updateGenSummary();
  scheduleSegments();
  enforceParamLock();
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
      jobReconnectTimer = setInterval(async () => {
        try {
          const r2 = await fetch(`${API}/jobs/${encodeURIComponent(curJobId)}`);
          if (!r2.ok) throw new Error("任务状态不可用");
          const j2 = await r2.json();
          const s = j2.job;
          if (!s) throw new Error("任务状态不可用");
          if (["done", "error", "cancelled"].includes(s.state)) {
            clearInterval(jobReconnectTimer);
            jobReconnectTimer = null;
            if (s.state === "done" && s.result) {
              histCurId = s.result.history_id || "";
              // 恢复路径与 SSE 路径对齐:先释放按钮/计时器/波形动画,再展示结果
              stopUi();
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
        }
      }, 500);
    } else if (job.state === "done" && job.result) {
      histCurId = job.result.history_id || "";
      // 与 SSE done 路径一致:先释放按钮/计时器,再展示已恢复的结果
      stopUi();
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
  localStorage.setItem("theme", next);
  applyTheme(next);
});

/* ---------------- header waveform (signature) ---------------- */
const hWave = $("waveHeader"), hCtx = hWave.getContext("2d");
let genAnim = { on: false, level: 0, progress: 0 };
let waveT = 0;

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
setInterval(() => { if (!document.hidden) drawHeaderWave(waveT + 16); }, 250); // gentle idle refresh
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
    $("devText").textContent = "CPU";
    devPill.title = "运行设备：CPU";
  }
  if (m.cpu_percent !== undefined) setMeter("mCpu", m.cpu_percent, `${Math.round(m.cpu_percent)}%`);
  if (m.mem_total_gb) setMeter("mMem", (m.mem_used_gb / m.mem_total_gb) * 100, `${m.mem_used_gb.toFixed(1)}/${m.mem_total_gb.toFixed(1)}G`);
  const hasGpu = m.gpu_percent !== undefined;
  $("mGpu").hidden = !hasGpu;
  $("mVram").hidden = !hasGpu;
  if (hasGpu) {
    setMeter("mGpu", m.gpu_percent, `${m.gpu_percent}%`);
    setMeter("mVram", (m.vram_used_mb / (m.vram_total_mb || 8192)) * 100, `${(m.vram_used_mb / 1024).toFixed(1)}/${((m.vram_total_mb || 0) / 1024).toFixed(1)}G`);
  }
  syncModelMetrics(m);
}

/* 模型页把 header 那四个小仪表放大成指标卡：8G 卡上「显存还剩多少」比任何文案都管用，
   而旧版这一页恰恰一个数字都没有。数据同源于 header 的 /status 轮询，不新增请求。 */
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
  if (m.vram_total_mb) {
    const total = m.vram_total_mb / 1024;
    const used = (m.vram_used_mb || 0) / 1024;
    mmMetric("mmVram", (used / total) * 100, `${used.toFixed(1)}/${total.toFixed(1)}G`, `余量 ${(total - used).toFixed(1)}G`);
  }
  if (m.gpu_percent !== undefined) mmMetric("mmGpu", m.gpu_percent, `${Math.round(m.gpu_percent)}%`, "利用率");
  if (m.mem_total_gb) {
    const pct = (m.mem_used_gb / m.mem_total_gb) * 100;
    mmMetric("mmMem", pct, `${m.mem_used_gb.toFixed(1)}/${m.mem_total_gb.toFixed(1)}G`, "系统内存");
  }
  if (m.cpu_percent !== undefined) mmMetric("mmCpu", m.cpu_percent, `${Math.round(m.cpu_percent)}%`, "系统");
}
async function pollStatus() {
  try {
    const s = await (await fetch(`${API}/status`)).json();
    renderModelPill(s);
    renderMetrics(s);
    applyModelBusy(s);            // 2 秒一次，模型页据此置灰「加载 / 重启」
  } catch (e) {
    $("modelPill").dataset.s = "unloaded";
    $("modelPillText").textContent = "服务连接中断…";
  }
}
setMetricsPolling(pollStatus);

/* ---------------- speaker reference audio ---------------- */
const spkDrop = $("spkDrop"), spkFile = $("spkFile");
const spkWave = $("spkWave"), spkCtx = spkWave.getContext("2d");
const spkAudio = new Audio();
const spkPlay = $("spkPlay"), spkPlayIcon = $("spkPlayIcon");
let spkBuffer = null, spkPlaying = false, spkSeekFrac = 0;
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
function userPickedSpk(f) { dropPresetSelection(); setSpkFile(f); }
function userPickedEmo(f) { dropPresetSelection(); setEmoFile(f); }

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

/* emo reference audio shares the same pattern */
const emoDrop = $("emoDrop"), emoFile = $("emoFile");
// 参考音频“当前选择”的版本号：预设音频是异步 fetch 的，下载期间用户
// 可能已经自己拖了新文件进来，晚到的预设音频不能再覆盖用户的选择。
const refSelSeq = { spk: 0, emo: 0 };
emoDrop.addEventListener("dragover", (e) => { e.preventDefault(); emoDrop.classList.add("over"); });
emoDrop.addEventListener("dragleave", () => emoDrop.classList.remove("over"));
emoDrop.addEventListener("drop", (e) => { e.preventDefault(); emoDrop.classList.remove("over"); if (e.dataTransfer.files[0]) userPickedEmo(e.dataTransfer.files[0]); });
emoFile.addEventListener("change", () => emoFile.files[0] && userPickedEmo(emoFile.files[0]));
function setEmoFile(f, persist = true) {
  refSelSeq.emo++;
  // 拖拽进来 / 预设 fetched 的 Blob 不会自动写进 <input type=file>，
  // 而提交时读的是 input.files[0]。这里必须同步，否则界面换了音频、
  // 生成却仍然用 input 里的旧文件（预设自带的那份）。
  if (emoFile.files[0] !== f) assignFile(emoFile, f);
  $("emoName").textContent = f.name;
  if (persist) setDraftBlob(DRAFT_AUDIO_KEYS.emo, f).catch(() => {});
}
function clearEmoFile() {
  emoFile.value = "";
  $("emoName").textContent = "点击选择情感参考音频";
  setDraftBlob(DRAFT_AUDIO_KEYS.emo, null).catch(() => {});
}

function drawSpkWave() {
  const { w, h } = sizeCanvas(spkWave);
  const data = spkBuffer.getChannelData(0);
  const step = Math.max(1, Math.floor(data.length / w));
  const mid = h / 2;
  const ink = cssVar("--accent"), played = cssVar("--primary"), border = cssVar("--border");
  spkCtx.clearRect(0, 0, w, h);
  spkCtx.fillStyle = border;
  spkCtx.fillRect(0, mid - 0.5, w, 1);
  const pos = spkPlaying || spkAudio.currentTime > 0 ? spkSeekFrac * w : 0;
  for (let x = 0; x < w; x++) {
    let min = 1, max = -1;
    for (let i = 0; i < step; i++) {
      const v = data[x * step + i] || 0;
      if (v < min) min = v; if (v > max) max = v;
    }
    const y0 = mid - max * mid * 0.9, y1 = mid - min * mid * 0.9;
    spkCtx.fillStyle = x <= pos ? played : ink;
    spkCtx.globalAlpha = x <= pos ? 0.95 : 0.55;
    spkCtx.fillRect(x, y0, 1, Math.max(1, y1 - y0));
  }
  spkCtx.globalAlpha = 1;
}
function spkFmt(t) { return `${t.toFixed(2)}s`; }
function spkTick() {
  if (!spkBuffer) return;
  const d = spkBuffer.duration;
  if (spkPlaying) spkSeekFrac = Math.min(spkAudio.currentTime / d, 1);
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
    spkAudio.currentTime = spkSeekFrac * spkBuffer.duration;
    await spkAudio.play().catch(() => {});
    spkPlaying = true;
    spkPlayIcon.innerHTML = PAUSE_SVG;
    requestAnimationFrame(spkTick);
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

function resetSpkSelection() {
  spkAudio.pause();
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
  $("spkName").textContent = "点击选择或拖入音频文件";
  $("spkSub").textContent = "约 3~10 秒参考音频效果最佳";
  $("spkFileName").textContent = "";
  $("spkFileName").title = "";
}

function setSpkFile(f, persist = true) {
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
    f.arrayBuffer().then((ab) => getAC().decodeAudioData(ab)).then((buf) => {
      spkBuffer = buf;
      spkPlaying = false; spkSeekFrac = 0;
      $("spkTime").textContent = `0.00s / ${spkBuffer.duration.toFixed(2)}s`;
      // 参考音频超过 15 秒时，服务端只取前 15 秒做音色克隆（infer_v2 的
      // _load_and_cut_audio 硬截断），超出部分白传。这里明确提示，免得用户
      // 以为整段都用了，或把音色偏归咎于 bug。正常长度则恢复默认提示。
      $("spkSub").textContent = spkBuffer.duration > 15
        ? `音频 ${spkBuffer.duration.toFixed(0)} 秒，只会用前 15 秒做音色克隆；建议先截出目标音色段再上传`
        : "约 3~10 秒参考音频效果最好";
      spkTick();
    }).catch(() => {
      resetSpkSelection();
      $("spkSub").textContent = "不支持该音频格式，请换一个文件（wav/mp3/flac/ogg）";
    });
  } catch (e) { /* ignore */ }
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
  $("emoAudioBox").hidden = m != "1";
  $("emoVecBox").hidden = m != "2";
  $("emoTextBox").hidden = m != "3";
}
$("emoMode").addEventListener("change", emoVisible);
// 清空旧情感音频选择，避免从模式1切走再切回时把过期文件发上去
$("emoMode").addEventListener("change", () => { if ($("emoMode").value != "1") clearEmoFile(); });
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
    await fetch(`${API}/prefs/param-lock`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(paramLock),
    });
  } catch (e) { /* 服务端没起：本地缓存仍生效，下次连上再写 */ }
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
async function updateSegments() {
  const mine = ++segSeq;
  const text = $("text").value;
  if (!text.trim()) {
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
    const j = await r.json();
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
    const est = estSec >= 60 ? `约 ${Math.round(estSec / 60)} 分钟` : `约 ${estSec} 秒`;
    $("estDurEcho").textContent =
      `共 ${segs.length} 段 · ${$("text").value.trim().length} 字 · 预估 ${est}（不含首次加载模型）`;
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
  const len = $("text").value.trim().length;
  const cc = $("charCount");
  cc.textContent = `${len} 字`;
  // 软上限提示：超过 TEXT_CHARS_SOFT 只是提醒，不再拦着不让生成
  // （后端 /tts 只在 TEXT_CHARS_HARD=10000 处才拒）
  cc.classList.toggle("warn", len > TEXT_CHARS_SOFT);
  if (len > TEXT_CHARS_HARD) {
    cc.textContent += `（超过 ${TEXT_CHARS_HARD} 字上限，服务端会拒绝）`;
  } else if (len > TEXT_CHARS_SOFT) {
    cc.textContent += "（文本较长，生成会很慢，期间无法生成其他内容）";
  }
  scheduleSegments();
  scheduleDraftSave();
});

// 文本框高度由 CSS 固定（#text 固定高 + 内部滚动），不随内容自动撑高

/* gen summary line: emotion mode + preset name (visible param confirmation above CTA) */
function updateGenSummary() {
  const modeNames = ["情感同参考音频", "情感参考音频", "情感向量", "情感描述文本"];
  const m = modeNames[$("emoMode").value] ?? $("emoMode").value;
  const parts = [
    `情感: ${m} (权重 ${Number($("emoWeight").value).toFixed(2)})`,
    curPresetName ? `预设: ${curPresetName}` : "预设: 无",
    `采样: temp ${Number($("temperature").value).toFixed(2)} / top_p ${Number($("topP").value).toFixed(2)} / top_k ${$("topK").value}`,
  ];
  $("genSummary").textContent = parts.join("  ·  ");
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
let curAbort = null;          // AbortController for the SSE fetch
let curJobState = "";         // queued | running | stopping
function jobActive() { return genBtn.disabled; }

function setResultVisible(visible) {
  resultBody.style.display = visible ? "block" : "none";
  emptyHint.style.display = visible ? "none" : "";
}

function showLoading(stage, cancellable = true) {
  setResultVisible(false);
  loadingBox.hidden = false;
  $("pStage").textContent = stage;
  errBox.classList.remove("on");
  cancelBtn.hidden = !cancellable;
}

const PROG_DESC = {
  "starting inference...": "启动推理",
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
function stopUi() {
  genBtn.disabled = false;
  clearInterval(elapsedTimer);
  cancelAnimationFrame(waveRAF);
  waveRAF = 0;
  genAnim.on = false;
  cancelBtn.hidden = true;
  curJobId = "";
  curJobState = "";
  curAbort = null;
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
}function showError(msg) {
  errBox.textContent = msg;
  errBox.classList.add("on");
  loadingBox.hidden = true;
  setResultVisible(hasResult);
  stopUi();
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
        body: "将中断当前推理，本次已生成的部分不会保存。模型保持加载，下次生成可直接继续。",
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
    showActionError("停止失败: " + e.message);
    updateCancelUi();
  } finally {
    if (curJobState !== "stopping") updateCancelUi();
  }
});

/* 前端文件校验：与后端 413/400 同口径，能在请求发出前就拦住（后端仍是权威校验） */
const AUDIO_MAX_MB = 20;
const AUDIO_TYPES = ["audio/", ".wav", ".mp3", ".flac", ".ogg", ".m4a"];
function validateAudioFile(file, label) {
  if (!file) return "";
  if (file.size > AUDIO_MAX_MB * 1048576) {
    return `${label}过大（${(file.size / 1048576).toFixed(0)} MB，上限 ${AUDIO_MAX_MB} MB）`;
  }
  const n = file.name.toLowerCase();
  if (!AUDIO_TYPES.some((t) => file.type.startsWith(t) || n.endsWith(t))) {
    return `${label}不是可识别的音频文件（支持 wav/mp3/flac/ogg/m4a）`;
  }
  return "";
}

genBtn.addEventListener("click", async () => {
  const text = $("text").value.trim();
  if (!text) { showError("请先输入要合成的文本"); return; }
  const spkSelected = $("spkFile").files[0];
  if (!spkSelected) { showError("请先选择音色参考音频"); return; }
  const mode = $("emoMode").value;
  if (mode == "1" && !$("emoFile").files[0]) { showError("情感参考音频模式下请先选择情感音频"); return; }
  if (mode == "3" && !$("emoText").value.trim()) { showError("请输入情感描述文本"); return; }
  const spkErr = validateAudioFile(spkSelected, "音色参考音频");
  if (spkErr) { showError(spkErr); return; }
  if (mode == "1") {
    const emoErr = validateAudioFile($("emoFile").files[0], "情感参考音频");
    if (emoErr) { showError(emoErr); return; }
  }

  genBtn.disabled = true;
  genStart = performance.now();
  curJobId = "";
  curAbort = new AbortController();
  player.pause();
  bigPlayIcon.innerHTML = PLAY2;
  clearActionError();
  setResultVisible(false);
  errBox.classList.remove("on");
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
  fd.append("emo_text", $("emoText").value);
  fd.append("use_random", $("emoRandom").checked);
  const ea = $("emoFile").files[0];
  if (mode == "1" && ea) fd.append("emo_audio", ea);
  if (mode == "2") vecInputs().forEach((s) => fd.append("vec" + (parseInt(s.dataset.i) + 1), s.value));
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

  try {
    const r = await fetch(`${API}/tts`, { method: "POST", body: fd, signal: curAbort.signal });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || `请求失败（${r.status}）`);
    }
    // consume the SSE stream manually — EventSource can't POST form data
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
          showLoading(`排队中… 前面还有 ${ev.ahead} 个任务`);
          $("pStageMeta").textContent = `排队 #${ev.ahead + 1}`;
        } else if (ev.type === "load") {
          curJobState = "running";
          updateCancelUi();
          loadingModel = true;
          markStep(0, "running");
          showLoading("正在加载模型…首次调用需 30~60 秒");
          $("pbarFill").style.width = "8%";
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
          showLoading(transDesc(ev.desc) || "正在生成语音…");
          genAnim.level = Math.min(1, 0.4 + (ev.value || 0));
          genAnim.progress = ev.value || 0;
          $("pbarFill").style.width = `${Math.round((ev.value || 0) * 100)}%`;
          // s2mel 阶段优先显示扩散步数，其余阶段显示全局百分比
          const mDiff = /\|s2mel\|(\d+)\/(\d+)/.exec(String(ev.desc || ""));
          $("pStageMeta").textContent = mDiff
            ? `扩散 ${mDiff[1]}/${mDiff[2]}`
            : `${Math.round((ev.value || 0) * 100)}%`;
        } else if (ev.type === "done") {
          for (let i = 0; i < 5; i++) markStep(i, "done");
          $("pbarFill").style.width = "100%";
          loadingBox.hidden = true;
          stopUi();
          histCurId = ev.history_id || "";
          await loadResult(ev.wav, ev.elapsed, ev.file, { melCapped: ev.mel_capped, normalizeFallback: ev.normalize_fallback });
          await refreshHistory();
          pollStatus();
        } else if (ev.type === "error") {
          throw new Error(ev.detail || "生成失败");
        } else if (ev.type === "warn") {
          // 软提示：文本过长。不中断生成，只把「等多久/期间不能干别的/
          // 刷新不中断」讲清楚，免得用户按下按钮才知道要等半小时。
          showActionHint(ev.detail || "文本较长，生成会很慢");
        } else if (ev.type === "stopping") {
          curJobState = "stopping";
          updateCancelUi();
          showLoading("正在停止生成…");
        } else if (ev.type === "stopped") {
          // 终态：用户主动停止。走中性提示而不是红色错误框——
          // 停止不是故障，每��正常停止都报红会让人以为出了事。
          curJobState = "stopped";
          updateCancelUi();
          showStopped(ev.detail);
        }
      }
    }
    if (genBtn.disabled) showError("生成连接中断，请重试");
  } catch (e) {
    if (e.name === "AbortError") return; // 用户主动放弃等待，提示已在 cancel 里给过
    showError(e.message);
    pollStatus();
  }
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
  player.src = url;
  try {
    const buf = await (await fetch(url)).arrayBuffer();
    trim.buffer = await getAC().decodeAudioData(buf);
    ED.setSource(trim.buffer, url);          // 剪辑状态回到"无操作"，见 web/edit.js
  } catch (e) {
    trim.buffer = null;
    ED.clear();
    hasResult = false;
    player.pause();
    player.removeAttribute("src");
    player.load();
    setResultVisible(false);
    showError("生成结果音频加载失败，请重试");
    return;
  }
  hasResult = true;
  $("trimRange").textContent = `全长 ${trim.buffer.duration.toFixed(2)}s`;
  $("genMeta").textContent = `耗时 ${Number(elapsed || 0).toFixed(1)}s`;
  updateDlName();
  drawTrimWave();
}

bigPlay.addEventListener("click", () => {
  if (player.paused) { player.play(); bigPlayIcon.innerHTML = PAUSE2; }
  else { player.pause(); bigPlayIcon.innerHTML = PLAY2; }
});
player.addEventListener("ended", () => { bigPlayIcon.innerHTML = PLAY2; });

function drawTrimWave() {
  if (!ED.src) return;
  const { w, h } = sizeCanvas(tWave);
  const data = ED.src.getChannelData(0);
  const step = Math.floor(data.length / w) || 1;
  const mid = h / 2;
  const ink = cssVar("--accent"), mut = cssVar("--border"), pri = cssVar("--primary");
  // 波形恒画源音频（编辑台），选区与播放进度都是源时间轴。
  // 编辑结果有自己的播放器（#outAudio）和自己的波形，两者互不干扰。
  const dur = ED.src.duration;
  const pos = dur ? (player.currentTime / dur) * w : 0;
  const s = ED.getState().trim;
  tCtx.clearRect(0, 0, w, h);
  for (let x = 0; x < w; x++) {
    let min = 1, max = -1;
    for (let i = 0; i < step; i++) {
      const v = data[x * step + i] || 0;
      if (v < min) min = v; if (v > max) max = v;
    }
    const y0 = mid - max * mid * 0.92, y1 = mid - min * mid * 0.92;
    const inSel = !s || (x / w >= s[0] && x / w <= s[1]);
    tCtx.fillStyle = x <= pos ? pri : (inSel ? ink : mut);
    tCtx.globalAlpha = x <= pos ? 0.95 : (inSel ? 0.9 : 0.55);
    tCtx.fillRect(x, y0, 1, Math.max(1, y1 - y0));
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
  player.play();
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
  player.play();
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
  const base = srcFileName();
  const stem = base.replace(/\.wav$/i, "");
  const suffix = ED.hasResult() ? "_edit" : (ED.getState().trim ? "_trim" : "");
  const outName = suffix ? `${stem}${suffix}.wav` : base;
  $("dlSrcName").textContent = base;
  $("dlName").textContent = outName;
  // 「下载编辑结果」不在这里管显示与否——它整个包在 #resultPreview 里，
  // 由 edit.js 的 paintResult() 随结果区一起显隐（默认不显示，有编辑结果才出现）
  $("dlBtn").title = `下载剪辑后的音频（${outName}）`;
  $("dlSrcBtn").title = `下载未经剪辑的生成音频（${base}）`;
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
  drawTrimWave();
};

$("dlSrcBtn").addEventListener("click", () => {
  // 原音频：直接拿服务端那份文件，不经过任何剪辑链路，字节就是原始字节
  const name = $("dlSrcName").textContent || srcFileName();
  if (ED.srcUrl) triggerDownload(ED.srcUrl, name);
  else if (ED.src) triggerDownload(URL.createObjectURL(encodeWav(ED.src)), name);
});

$("dlBtn").addEventListener("click", () => {
  const buf = ED.getRendered();
  if (!buf || !ED.hasOps()) return;
  // 下载与试听同源（都是 ED.rendered），不存在两套实现算出不同结果的可能
  triggerDownload(URL.createObjectURL(encodeWav(buf)), $("dlName").textContent);
});

/* ---------------- preset library (right column, tabs with examples) ---------------- */
let presetNamesCache = [];
let curPresetName = "";       // applied preset name (for summary line + card highlight)
let exCache = [];             // example items cache

async function fetchPresetAudio(url, inputEl, applyFn, seqKey) {
  const seq = seqKey ? refSelSeq[seqKey] : 0;
  try {
    const r = await fetch(url);
    if (!r.ok) return;
    const blob = await r.blob();
    // 下载期间用户已经自己换了音频：以用户的选择为准
    if (seqKey && refSelSeq[seqKey] !== seq) return;
    const fileName = (url.split("/").pop() || "audio") + ".wav";
    const file = new File([blob], fileName, { type: "audio/wav" });
    const dt = new DataTransfer();
    dt.items.add(file);
    inputEl.files = dt.files;
    applyFn(file);
  } catch (e) { /* audio optional */ }
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
  if (j.prompt_audio_url) fetchPresetAudio(j.prompt_audio_url, $("spkFile"), setSpkFile, "spk");
  if (j.emo_audio_url) fetchPresetAudio(j.emo_audio_url, $("emoFile"), setEmoFile, "emo");
  scheduleSegments();
  enforceParamLock();
  scheduleDraftSave();
}
function collectPresetState(fd) {
  fd.append("emo_control_method", $("emoMode").value);
  fd.append("emo_weight", $("emoWeight").value);
  fd.append("emo_text", $("emoText").value);
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
  if (persist) localStorage.setItem(LIB_FOLD_KEY, folded ? "1" : "0");
}
$("libFold").addEventListener("click", () => {
  libSetFolded(!$("libBox").classList.contains("lib-collapsed"));
});
libSetFolded(localStorage.getItem(LIB_FOLD_KEY) === "1", false);

/* shared: apply a preset by name; returns success */
async function applyPresetByName(name) {
  try {
    const j = await presetDetail(name, true); // 应用时强制拉最新
    if (!j) throw new Error("加载失败");
    curPresetName = name;
    applyPresetToForm(j);
    showActionHint("已加载预设: " + name);
    updateGenSummary();
    return true;
  } catch (e) { showActionError("加载预设失败: " + e.message); return false; }
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

async function refreshPresets() {
  try {
    const r = await (await fetch(`${API}/presets`)).json();
    presetNamesCache = r.presets || [];
  } catch (err) { /* keep old list */ }
}

/* 预设详情统一缓存（跨 libList / preset 管理页复用，避免 N+1 重复请求） */
const presetDetailCache = new Map();
function presetDetail(name, force = false) {
  if (!force && presetDetailCache.has(name)) return Promise.resolve(presetDetailCache.get(name));
  const p = fetch(`${API}/presets/` + encodeURIComponent(name))
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => d)
    .catch(() => null);
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
        <span class="pm-acts">
          <button class="pm-act" data-act="rename" type="button" title="改名">改</button>
          <button class="pm-act" data-act="duplicate" type="button" title="复制">复</button>
        </span>
        <button class="pm-del" type="button" title="删除此预设">✕</button>`;
      row.querySelector(".htext").textContent = name;
      row.title = "点击应用此预设";
      row.querySelector(".pm-del").setAttribute("aria-label", "删除预设 " + name);
      const meta = row.querySelector(".hmeta");
      presetDetail(name).then((d) => {
        if (!d) { meta.textContent = "-"; return; }
        const adv = d.advanced_params || {};
        meta.textContent = `${["同参考", "情感音频", "向量", "文本"][d.emo_control_method] ?? "-"} · ${d.prompt_audio_url ? "含音频" : "无音频"} · temp ${adv.temperature ?? "-"}`;
      });
      row.addEventListener("click", async (e) => {
        if (e.target.closest(".pm-del") || e.target.closest(".pm-act")) return;
        if (await applyPresetByName(name)) renderLib();
      });
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
          setSpkFile(file, false); // example audio already exists server-side
          $("text").value = ex.text || "";
          $("charCount").textContent = `${(ex.text || "").trim().length} 字`;
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
        } catch (err) { showActionError("加载示例失败: " + err.message); }
      });
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
      presetDetailCache.delete(name);
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
  } catch (e) { showActionError("保存预设失败: " + e.message); }
});

async function loadExamples() {
  try {
    const j = await (await fetch(`${API}/examples`)).json();
    exCache = j.examples || [];
  } catch (err) { exCache = []; }
}

/* ---------------- generation history (right sidebar) ----------------
   Scope is this server run: the list lives in the backend's memory, so a page
   refresh keeps it but a server restart clears it. No persistence by design. */
const histBar = $("histBar");
const genPage = document.querySelector('.page[data-page="gen"]');
const HIST_KEY = "histCollapsed";
const EMO_LABEL = ["同参考", "情感音频", "向量", "文本"];
let histItems = [];
let histCurId = "";

function histSetCollapsed(collapsed, persist = true) {
  histBar.classList.toggle("collapsed", collapsed);
  genPage.classList.toggle("hist-collapsed", collapsed);
  if (persist) localStorage.setItem(HIST_KEY, collapsed ? "1" : "0");
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

function renderHistory() {
  const box = $("histList");
  $("histCount").textContent = histItems.length;
  $("histRailCount").textContent = histItems.length;
  box.replaceChildren();
  if (!histItems.length) {
    const empty = document.createElement("div");
    empty.className = "hb-empty";
    empty.textContent = "暂无记录。生成一次语音后，这里会留下本次运行的历史。";
    box.appendChild(empty);
    return;
  }
  for (const it of histItems) {
    const row = document.createElement("div");
    row.className = "hb-item" + (it.id === histCurId ? " cur" : "");
    row.innerHTML = `<span class="hb-text"></span><span class="hb-meta"></span><button class="pm-del" type="button" title="删除这条记录">✕</button>`;
    row.querySelector(".hb-text").textContent = it.text || "(无文本)";
    row.querySelector(".hb-meta").textContent =
      `${histFmtTime(it.created_at)} · ${Number(it.elapsed).toFixed(1)}s · ${histFmtSize(it.size)} · ${EMO_LABEL[it.emo_mode] ?? "-"}`;
    row.title = "点击载入到右侧播放器";
    const del = row.querySelector(".pm-del");
    del.setAttribute("aria-label", "删除这条历史记录");
    row.addEventListener("click", async (e) => {
      if (e.target.closest(".pm-del")) return;
      histCurId = it.id;
      renderHistory();
      await loadResult(it.url, it.elapsed, it.file);
    });
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        const r = await fetch(`${API}/history/` + encodeURIComponent(it.id), { method: "DELETE" });
        if (!r.ok) throw new Error("删除失败");
        if (histCurId === it.id) histCurId = "";
        await refreshHistory();
      } catch (err) { showActionError("删除历史失败: " + err.message); }
    });
    box.appendChild(row);
  }
}

async function refreshHistory() {
  try {
    const r = await (await fetch(`${API}/history`)).json();
    histItems = r.items || [];
    $("histAuto").checked = !!r.auto_clean;
    $("histNote").textContent = `本次运行生成 · 自动清理保留最近 ${r.limit} 条`;
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
    const r = await (await fetch(`${API}/history/config`, { method: "POST", body: fd })).json();
    $("histAuto").checked = !!r.auto_clean;
  } catch (err) { showActionError("切换自动清理失败"); }
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
    await fetch(`${API}/history`, { method: "DELETE" });
    histCurId = "";
    await refreshHistory();
  } catch (err) { showActionError("清空历史失败"); }
});

/* ---------------- glossary ---------------- */
const glError = $("glError");
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
    const j = await (await fetch(`${API}/glossary`)).json();
    const g = j.glossary || {};
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
      del.textContent = "×";
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
  } catch (err) { /* keep old list */ }
}

/* 单条删除是轻量可逆操作（重新添加即可），不弹确认框；清空全部才拦截。 */
async function removeTerm(key) {
  try {
    const r = await fetch(`${API}/glossary/` + encodeURIComponent(key), { method: "DELETE" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { showGlossaryError("删除术语失败: " + (j.detail || r.status)); return; }
    clearGlossaryError();
    await renderGlossary();
    showActionHint(`已删除术语: ${key}`);
  } catch (err) { showGlossaryError("删除术语失败: " + err); }
}

$("btnClearTerms").addEventListener("click", async () => {
  const ok = await uiConfirm({
    title: "清空术语",
    body: "确定清空全部自定义术语？此操作不可恢复。",
    okText: "清空", danger: true,
  });
  if (!ok) return;
  try {
    const r = await fetch(`${API}/glossary`, { method: "DELETE" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { showGlossaryError("清空术语失败: " + (j.detail || r.status)); return; }
    clearGlossaryError();
    await renderGlossary();
    showActionHint("术语已清空");
  } catch (err) { showGlossaryError("清空术语失败: " + err); }
});
$("btnAddTerm").addEventListener("click", async () => {
  const fd = new FormData();
  fd.append("term", $("glTerm").value);
  fd.append("reading_zh", $("glZh").value);
  fd.append("reading_en", $("glEn").value);
  try {
    const r = await fetch(`${API}/glossary`, { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok) { showGlossaryError("添加术语失败: " + (j.detail || "")); return; }
    $("glTerm").value = ""; $("glZh").value = ""; $("glEn").value = "";
    clearGlossaryError();
    await renderGlossary();
    showActionHint("术语已添加");
  } catch (err) { showGlossaryError("添加术语失败: " + err); }
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
    const r = await fetch(`${API}/glossary/cascade`, { method: "POST", body: fd });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { showGlossaryError("切换失败: " + (j.detail || r.status)); await renderGlossary(); return; }
    // 用服务端返回的值回填，别信本地 checkbox——失败时会和服务端不一致
    e.target.checked = !!j.cascade;
    await renderGlossary();
    scheduleSegments();          // 替换策略变了，预览要跟着重算
    showActionHint(want
      ? "已开启级联替换：读音里如果含有其它术语，会被继续改写"
      : "已关闭级联替换：每条术语只作用于你输入的原文");
  } catch (err) {
    showGlossaryError("切换失败: " + err);
    await renderGlossary();
  }
});

/* ---------------- page tabs (gen / presets / model) ---------------- */
const tabBtns = document.querySelectorAll("#tabs .tab");
function switchPage(name, updateHash = true) {
  tabBtns.forEach((b) => b.classList.toggle("cur", b.dataset.page === name));
  document.querySelectorAll(".page").forEach((p) => {
    const on = p.dataset.page === name;
    p.classList.toggle("cur", on);
    p.hidden = !on;
  });
  if (name === "model") refreshModelPage();
  if (name === "presets") refreshPmList();
  // hash 路由：刷新/分享链接时停留在当前页签
  if (updateHash && location.hash.slice(1) !== name) {
    history.replaceState(null, "", "#" + name);
  }
}
tabBtns.forEach((b) => b.addEventListener("click", () => switchPage(b.dataset.page)));
window.addEventListener("hashchange", () => {
  const target = location.hash.slice(1);
  if (["gen", "presets", "model"].includes(target)) switchPage(target, false);
});

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
      try { pmDetailMap.set(n, await pmLoadDetail(n)); }
      catch { pmDetailMap.set(n, null); }
    }
  };
  await Promise.all(Array.from({ length: Math.min(6, todo.length) }, worker));
}

async function refreshPmList() {
  try {
    const r = await (await fetch(`${API}/presets`)).json();
    pmListCache = r.presets || [];
    await pmEnsureDetails(pmListCache);
    if (pmSelName && !pmListCache.includes(pmSelName)) pmSelName = "";
    pmClampSelection();   // 列表可能刚变过，选中项要跟着收敛到可见集合
    renderPmNav();
    renderPmCards();
    renderInspector(pmSelName);
  } catch (err) {
    pmFlash("刷新预设失败: " + err, true);
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

  $("pmCount").textContent = `${pmListCache.length} 个预设`;
}

function renderPmCards() {
  const names = pmVisibleNames();
  $("pmShown").textContent = names.length === pmListCache.length
    ? `共 ${names.length} 个`
    : `${names.length} / ${pmListCache.length}`;
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
      <span class="pm-acts">
        <button class="pm-act" data-act="rename" type="button" title="改名">改</button>
        <button class="pm-act" data-act="duplicate" type="button" title="复制">复</button>
      </span>
      <button class="pm-del" type="button" title="删除此预设">✕</button>`;
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
function renderInspector(name) {
  const insp = $("pmInsp"), create = $("pmCreateBox");
  const d = name ? pmDetailMap.get(name) : null;
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
    const a = document.createElement("audio");
    a.controls = true;
    a.preload = "none";
    a.src = d.prompt_audio_url;
    a.style.width = "100%";
    const note = document.createElement("p");
    note.className = "mg-hint";
    note.style.margin = "0";
    note.textContent = "预设自带的参考音频，应用后自动回填到合成页";
    audioBox.append(a, note);
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
    box.style.marginBottom = "8px";
    EMO_NAMES8.forEach((label, i) => {
      const v = Math.max(0, Math.min(1, vec[i] ?? 0));
      const row = document.createElement("div");
      row.innerHTML = `<span>${label}</span><i><b style="width:${(v * 100).toFixed(1)}%"></b></i><em>${v.toFixed(2)}</em>`;
      box.appendChild(row);
    });
    const note = document.createElement("p");
    note.className = "mg-hint";
    note.style.margin = "0";
    note.textContent = "8 维情感向量，数值越大该情绪越强";
    emoBox.append(box, note);
  } else if (m === 3) {
    const t = document.createElement("div");
    t.className = "insp-emotext";
    t.textContent = d.emo_text || "（未填写描述文本）";
    emoBox.appendChild(t);
  } else if (m === 1) {
    if (d.emo_audio_url) {
      const a = document.createElement("audio");
      a.controls = true;
      a.preload = "none";
      a.src = d.emo_audio_url;
      a.style.width = "100%";
      emoBox.appendChild(a);
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
  } catch (err) { pmFlash("操作失败: " + err.message, true); }
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
  } catch (e) { pmFlash("创建失败: " + e.message, true); }
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
function mmElapsedTimer(label) {
  const t0 = Date.now();
  mmState.textContent = `${label}… 0s`;
  mmState.className = "mono";
  const box = $("mmStateBox");
  if (box) box.dataset.s = "loading";
  return setInterval(() => {
    mmState.textContent = `${label}… ${Math.round((Date.now() - t0) / 1000)}s`;
  }, 500);
}
async function refreshModelPage() {
  try {
    const s = await (await fetch(`${API}/model`)).json();
    applyModelBusy(s);
    const phase = s.phase || (s.loaded ? "ready" : "unloaded");
    const stateNames = {
      ready: "已加载", loading: "加载中", reloading: "重载中",
      unloading: "卸载中", error: "加载失败", unloaded: "未加载",
    };
    mmState.textContent = stateNames[phase] || stateNames.unloaded;
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
    for (const k of CFG_KEYS) { const el = $(CFG_BOX[k]); if (el) el.checked = !!s[k]; }
    $("cfgSteps").value = s.diffusion_steps ?? 25;
    $("cfgRate").value = s.inference_cfg_rate ?? 0.7;
    for (const s2 of mmCfgSliders) {
      const el = $(s2.id), out = $(s2.out);
      out.textContent = Number(el.value).toFixed(s2.dec);
      sliderFill(el);
    }
    mmRuntime.textContent = [
      `FP16=${s.fp16}  S2MEL_FP16=${s.s2mel_fp16}`,
      `W2V_FP16=${s.w2v_fp16}  QWEN_FP16=${s.qwen_fp16}`,
      `CUDNN_BENCHMARK=${s.cudnn_benchmark}`,
      `DIFFUSION_STEPS=${s.diffusion_steps}  CFG_RATE=${s.inference_cfg_rate}`,
    ].join("\n");
    mmFlashErr("");
  } catch (e) {
    mmFlashErr("无法读取模型状态: " + e);
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
    mmBusyBtns.delete(btn);
    btn.disabled = false;
    btn.textContent = old;
  }
  await refreshModelPage();                 // 无论成败都刷新状态位，清掉计时文案
  if (!failed && onDone) onDone();
}

$("btnModelLoad").addEventListener("click", () => withBusyBtn(
  $("btnModelLoad"), "加载中…", async () => {   // 首次加载 30~120s
    const r = await (await fetch(`${API}/model/load`, { method: "POST" })).json();
    if (!r.ok) throw new Error(r.error || r.detail || "加载失败");
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
      const r = await (await fetch(`${API}/model/unload?force=1`, { method: "POST" })).json();
      if (!r.ok) throw new Error(r.error || r.detail || "卸载失败");
      pollStatus();
    });
    return;
  }
  if (!await uiConfirm({ title: "卸载模型", body: "卸载后首次生成需重新加载（约 30~60 秒）。", okText: "卸载" })) return;
  await withBusyBtn($("btnModelUnload"), "卸载中…", async () => {
    const r = await (await fetch(`${API}/model/unload`, { method: "POST" })).json();
    if (!r.ok) throw new Error(r.error || r.detail || "卸载失败");
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
    const r = await (await fetch(`${API}/model/restart`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })).json();
    if (!r.ok) throw new Error(r.error || r.detail || "重启失败");
    pollStatus();
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
  mmFlashErr("");
});

$("btnCfgSave").addEventListener("click", () => {
  const body = {};
  for (const k of CFG_KEYS) body[k] = $(CFG_BOX[k]).checked;
  body.diffusion_steps = parseInt($("cfgSteps").value, 10);
  body.inference_cfg_rate = parseFloat($("cfgRate").value);
  return withBusyBtn($("btnCfgSave"), "保存中…", async () => {
    try {
      const r = await fetch(`${API}/model/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const j = await r.json();
      if (!j.ok) throw new Error(j.detail || "服务未返回失败原因");
    } catch (e) {
      throw new Error("保存配置失败: " + e.message);
    }
    pollStatus();
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
  // 模态框打开时不响应快捷键（Esc 关窗已由 modal 自己处理）
  if (!$("modalMask").hidden) return;
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
  if (e.key === " " && !typing && trim.buffer) {
    e.preventDefault();
    if (player.paused) { player.play(); bigPlayIcon.innerHTML = PAUSE2; }
    else { player.pause(); bigPlayIcon.innerHTML = PLAY2; }
  }
});

/* ---------------- init (single entry point) ---------------- */
applyTheme(localStorage.getItem("theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
/* 侧边栏初始状态：没有保存过偏好就按窗口宽度决定（宽屏展开，窄屏收起） */
const _savedHist = localStorage.getItem(HIST_KEY);
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
  // 草稿恢复完再套锁定：锁定优先级最高，且先用本地缓存避免闪一下默认值
  loadParamLockFromCache();
  renderLib();
  restoreJob();
  loadParamLockFromServer(); // 服务端为准，校正本地缓存
})();
refreshPmList();
renderGlossary();
/* 起始页签跟 URL hash（#gen/#presets/#model），刷新后不丢当前页 */
switchPage(["gen", "presets", "model"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "gen", false);

const _ro = new ResizeObserver(() => {
  drawHeaderWave();
  if (spkBuffer) drawSpkWave();
  if (trim.buffer) drawTrimWave();
});
_ro.observe(hWave);
_ro.observe(spkWave);
_ro.observe(tWave);
