"""IndexTTS2 lightweight FastAPI server (no gradio).

Startup only loads light components (config, text normalizer, tokenizer);
the main TTS model is loaded lazily on first /tts request, so the server
boots fast and idle VRAM stays at zero.
"""
import argparse
import asyncio
import contextlib
import errno
import gc
import hashlib
import io
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import warnings
from pathlib import Path
# Keep every HF / transformers / torch hub cache inside the project so running
# the server never writes to the user's system drive (C:) on Windows.
current_dir = os.path.dirname(os.path.abspath(__file__))
_hf = os.path.join(current_dir, "checkpoints", "hf_cache")
_torch = os.path.join(current_dir, "checkpoints", "torch_cache")
os.environ.setdefault("HF_HOME", _hf)
os.environ.setdefault("HF_HUB_CACHE", _hf)
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HUGGINGFACE_HUB_OFFLINE", "1")
os.environ.setdefault("HUGGINGFACE_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TORCH_HOME", _torch)

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

sys.path.append(current_dir)
sys.path.append(os.path.join(current_dir, "indextts"))

from fastapi import APIRouter, Body, FastAPI, File, Form, Request, Response, UploadFile, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from omegaconf import OmegaConf
from urllib.parse import quote
from indextts.utils.presets import (
    list_presets,
    save_preset,
    load_preset,
    delete_preset,
    get_presets_dir,
    safe_preset_name,
    preset_exists,
    rename_preset,
    duplicate_preset,
)
from indextts.utils.glossary_store import (
    DEFAULT_SCENARIO,
    DEFAULT_SCENARIO_NAME,
    GlossaryConflict,
    GlossaryReadOnly,
    GlossaryStore,
    GlossaryValidationError,
    UnknownScenario,
)
# Pure helpers (mojibake repair / filename rules) live in their own module so
# they can be unit-tested without importing the app or torch.
from indextts.utils.server_helpers import (
    fix_mojibake as _fix_mojibake,
    safe_title as _safe_title,
    unique_path as _unique_path,
    output_name as _output_name,
    prompt_file as _prompt_file_impl,
    clean_legacy_prompts as _clean_legacy_prompts_impl,
    looks_like_audio as _looks_like_audio,
    audio_kind as _audio_kind,
    video_kind as _video_kind,
    audio_ext_from_magic as _audio_ext_from_magic,
    cap_prompt_files as _cap_prompt_files,
    normalize_emo_vec as _normalize_emo_vec,
    ensure_wetext_ascii_path,
    estimate_long_text as _estimate_long_text,
    estimated_minutes as _estimated_minutes,
    format_long_text_warn as _format_long_text_warn,
    AUDIO_MAX_BYTES,
    SUPPORTED_AUDIO_EXTS,
    SUPPORTED_AUDIO_TEXT,
    FFMPEG_AUDIO_EXTS,
    FFMPEG_AUDIO_TEXT,
    VIDEO_EXTS,
    VIDEO_TEXT,
    VIDEO_MAX_BYTES,
    VIDEO_MAX_SECONDS,
)
# 字幕纯计算（段边界 -> 条目 -> SRT/VTT）：同样是标准库实现，可在不导入
# torch 的情况下被单测钉住；HTTP 与落盘编排留在本文件。
from indextts.utils.subtitle import (
    SIDECAR_VERSION,
    cues_from_sidecar,
    format_srt,
    format_vtt,
    load_sidecar,
    save_sidecar,
    sidecar_path,
    tokens_to_readable,
)
# 中文路径分发包兼容：kaldifst 走 ANSI C API 打开 .fst，wetext 装在非 ASCII
# 路径下 import 即炸。必须在任何 wetext 触达之前装好重定向（ASCII 路径下为空操作）。
ensure_wetext_ascii_path()
# 中文 Windows 控制台默认 GBK 代码页：推理链路里的 print（如 token 列表、模型路径）
# 含 GBK 无法编码的字符时直接 UnicodeEncodeError 炸掉整次生成。统一改 UTF-8 + 替换。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception as e:
    # logger 还没建（97 行才配置），只能直接打 stderr。这里静默吞掉的后果是：
    # GBK 控制台上 emoji 崩溃将无从排查，而这段代码存在的唯一目的就是防止它。
    print(f"[warn] failed to reconfigure std streams to utf-8: {e}", file=sys.stderr)

# ---- logging --------------------------------------------------------------------
# 持久日志写 logs/server.log（按天轮转、保留 7 天），控制台同步输出。
# start_server.bat 重定向的 server_bg.log 只是控制台镜像（每次启动清空），
# 排查历史问题的唯一依据是这里带时间戳的 server.log。
#
# handler 挂在 "indextts" 这个父 logger 上，而不是 "indextts.server"：
# 兄弟 logger（如 indextts.utils.front 的文本规范化异常）不会自动继承挂在
# 兄弟身上的 handler，会一路传播到没配 handler 的 root，只由 logging 的
# lastResort 打到 stderr —— 表现是「控制台看得见，server.log 里没有」。
# 挂父节点则 indextts.* 下所有子 logger 统一继承。
import logging
from logging.handlers import TimedRotatingFileHandler

LOGS_DIR = os.path.join(current_dir, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
logger = logging.getLogger("indextts")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    _log_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    _file_handler = TimedRotatingFileHandler(
        os.path.join(LOGS_DIR, "server.log"), when="midnight", backupCount=7, encoding="utf-8",
    )
    _file_handler.setFormatter(_log_fmt)
    _console_handler = logging.StreamHandler(sys.stdout)
    _console_handler.setFormatter(_log_fmt)
    logger.addHandler(_file_handler)
    logger.addHandler(_console_handler)
parser = argparse.ArgumentParser(
    description="IndexTTS2 FastAPI server",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
parser.add_argument("--verbose", action="store_true", default=False)
parser.add_argument("--port", type=int, default=7860)
# Default to loopback: the server has no auth and can delete files/presets,
# so it must not be reachable from the LAN unless the user opts in.
parser.add_argument("--host", type=str, default="127.0.0.1")
parser.add_argument("--model_dir", type=str, default="./checkpoints")
# 无参数启动时的默认配置与 start_server.bat / WebUI「推荐配置」保持一致：
# FP16+S2MEL_FP16+W2V_FP16+QWEN_FP16 全开（w2v 省 ~1GB 显存；8G 卡经实测可跑），
# cuDNN 自动调优关（实测负优化），steps 25 / cfg 0.7
parser.add_argument("--fp16", action="store_true", default=True)
parser.add_argument("--no-fp16", dest="fp16", action="store_false",
                    help="Disable GPT main model FP16 (default on).")
parser.add_argument("--s2mel_fp16", action="store_true", default=True)
parser.add_argument("--no-s2mel_fp16", dest="s2mel_fp16", action="store_false",
                    help="Disable s2mel diffusion FP16 (default on).")
parser.add_argument("--w2v_fp16", action=argparse.BooleanOptionalAction, default=None,
                    help="Run the w2v-bert-2.0 semantic encoder in FP16 (saves ~1GB VRAM). Default on.")
parser.add_argument("--qwen_fp16", action=argparse.BooleanOptionalAction, default=None,
                    help="Run the Qwen emotion model in FP16 (default on; --no-qwen_fp16 falls back to FP32).")
parser.add_argument("--diffusion_steps", type=int, default=25)
parser.add_argument("--inference_cfg_rate", type=float, default=0.7)
parser.add_argument("--cudnn_benchmark", action="store_true", default=False,
                    help="Enable cuDNN autotuning. MEASURED SLOWDOWN on this model: cuDNN "
                         "re-tunes for every new input length, so BigVGAN goes 3.35s -> 97s "
                         "and a whole run 38s -> 141s. Leave off unless the input shape is fixed.")
parser.add_argument("--gpt_batch", type=int, default=0,
                    help="Segment batching for non-streaming multi-segment text: 0/1 = off, "
                         "-1 = auto by free VRAM, 2-12 = fixed rows. Only applies when the text "
                         "is split into >=2 segments. Measured ~1.5x end-to-end at 2 rows "
                         "(+0.5GiB); see docs/PERFORMANCE_zh.md section 6.3/6.4.")
cmd_args = parser.parse_args()

app = FastAPI(
    title="IndexTTS2",
    version="1.0",
    description="IndexTTS2 本地服务。JSON 接口统一挂在 /api/v1 前缀下，"
                "交互式文档见 /docs；静态页面与资源留在根路径（/、/assets）。",
)
# API 版本化：全部 JSON 路由定义在 api 路由器上（文件末尾统一挂到 app），
# 前缀 /api/v1；静态资源不走版本前缀。
API_V1 = "/api/v1"
api = APIRouter(prefix=API_V1)


# 请求日志：方法/路径/状态码/耗时。高频轮询端点与静态资源降为 DEBUG，
# 其余 INFO —— 事后能还原"谁在什么时候调了什么、结果如何"。
@app.middleware("http")
async def _log_requests(request: Request, call_next):
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        raise
    ms = (time.perf_counter() - t0) * 1000
    path = request.url.path
    line = "%s %s -> %s (%.0f ms)", request.method, path, response.status_code, ms
    if path.startswith(("/assets", "/favicon")) or path in ("/api/v1/status", "/api/v1/metrics", "/api/v1/health"):
        logger.debug(*line)
    else:
        logger.info(*line)
    return response


class LazyTTS:
    """Loads nothing heavy at startup; torch and the TTS model load on first use.

    Even the light components (normalizer/tokenizer) pull in torch via
    indextts.utils.common, so they are built lazily too -- startup stays
    torch-free so the page opens instantly even on slow machines.
    """

    def __init__(self, model_dir, cfg_path, **init_kwargs):
        self.model_dir = model_dir
        self.cfg_path = cfg_path
        self.init_kwargs = init_kwargs
        self._tts = None
        self._load_lock = threading.Lock()
        self._light_lock = threading.Lock()
        # init_kwargs 的写入锁。不与 _INFER_LOCK 混用：set_runtime 只是改
        # 几个标量，推理在开始时读一次就没再动了（infer_v2.py 在 infer_generator
        # 开头把它们 pop 进局部变量），没必要为它去抢 GPU 锁。
        self._cfg_lock = threading.Lock()
        self.cfg = None
        # 规则库在启动时只读两个小 YAML，不加载 torch；管理接口因此能秒开。
        # store 抛异常（磁盘损坏等）不能让服务起不来：失败时按空库继续，
        # 具体原因会写进 warning 列表并在界面横幅提示。
        try:
            self.glossary_store = GlossaryStore(model_dir)
        except Exception as e:
            logger.error("Glossary store init failed: %s", e, exc_info=True)
            self.glossary_store = None
        self.glossary_path = os.path.join(model_dir, "glossary.yaml")
        # 级联替换开关单独存。塞进 glossary.yaml 会被当成「术语 -> 读音」，
        # _clean_glossary 只查 key 不查 value，一个 bool 会活下来变成术语。
        self.glossary_settings_path = os.path.join(model_dir, "glossary_settings.yaml")
        self.normalizer = None
        self.tokenizer = None
        self.model_version = None
        self._phase = "unloaded"
        self._phase_lock = threading.Lock()
        # (测量时刻, 预览结果)：empty_cache 是全局同步操作，不值得每次 /model 轮询都做一遍
        self._preview_cache = (0.0, None)

    @property
    def phase(self):
        with self._phase_lock:
            return self._phase

    def _set_phase(self, phase):
        with self._phase_lock:
            self._phase = phase

    def _ensure_light(self):
        """Build config/normalizer/tokenizer on first need (imports torch).

        先全部造在局部变量里、造完再一次性发布给 self。原来的写法在
        第一个赋值（self.cfg）就对外「挂牌」，但 tokenizer 要等到最后
        才赋值，中间隔着 normalizer.load() 的数秒空窗——这期间任何并发
        请求都会命中外层的无锁快路径 `if self.cfg is None`，误判为已就绪，
        然后拿到还是 None 的 tokenizer（AttributeError 500）。更糟的是
        期间到达的 /tts 请求会在 ensure_loaded 里用 None 覆盖掉
        IndexTTS2.__init__ 刚建好的 tokenizer，整次模型加载作废。
        """
        if self.cfg is not None:
            return self.cfg
        with self._light_lock:
            if self.cfg is not None:               # 双重检查：等锁期间可能已被别人建好
                return self.cfg
            t0 = time.perf_counter()
            from indextts.utils.front import TextNormalizer, TextTokenizer
            cfg = OmegaConf.load(self.cfg_path)
            normalizer = TextNormalizer(enable_glossary=True)
            normalizer.load()
            bpe_path = os.path.join(self.model_dir, cfg.dataset["bpe_model"])
            tokenizer = TextTokenizer(bpe_path, normalizer)
            # 规则库是唯一事实源；normalizer 只保留一份默认场景快照，
            # 给未显式传快照的旧库调用方兜底。
            snapshot = None
            if self.glossary_store is not None:
                try:
                    snapshot = self.glossary_store.snapshot(None)
                except Exception as e:
                    logger.warning("Failed to take default glossary snapshot: %s", e, exc_info=True)
            if snapshot is not None:
                normalizer.term_glossary = snapshot.glossary
                normalizer.cascade_glossary = snapshot.cascade
            elif os.path.exists(self.glossary_path):
                try:
                    normalizer.load_glossary_from_yaml(self.glossary_path)
                except Exception as e:
                    logger.warning("Failed to load glossary: %s", e, exc_info=True)
            if self.glossary_store is None:
                try:
                    normalizer.load_glossary_settings(self.glossary_settings_path)
                except Exception as e:
                    logger.warning("Failed to load glossary settings: %s", e, exc_info=True)
            # 全部就绪后才发布，中途不暴露半成品。cfg 是 235 行无锁快路径的
            # 发布标志位，必须最后赋——先赋它等于在 tokenizer 还空着时就挂了
            # 「已就绪」的牌子。
            self.normalizer = normalizer
            self.tokenizer = tokenizer
            self.model_version = getattr(cfg, "version", None)
            self.cfg = cfg
            logger.info("Light components ready in %.2fs", time.perf_counter() - t0)
        return self.cfg

    @property
    def loaded(self):
        return self._tts is not None

    def ensure_loaded(self):
        if self._tts is None:
            with self._load_lock:
                if self._tts is None:
                    self._ensure_light()
                    self._apply_backend_flags()
                    logger.info("Loading IndexTTS2 main model on first use...")
                    self._set_phase("loading")
                    try:
                        from indextts.infer_v2 import IndexTTS2
                        t = IndexTTS2(
                            model_dir=self.model_dir,
                            cfg_path=self.cfg_path,
                            use_fp16=self.init_kwargs.get("use_fp16", False),
                            use_s2mel_fp16=self.init_kwargs.get("use_s2mel_fp16", False),
                            use_w2v_fp16=self.init_kwargs.get("use_w2v_fp16", True),
                            use_qwen_fp16=self.init_kwargs.get("use_qwen_fp16", True),
                            gpt_batch_size=self.init_kwargs.get("gpt_batch_size", 0),
                            # 段头/段尾各保留多少空白（毫秒）。0 = 关闭裁剪，退回旧行为。
                            # 默认 100 对应「段间停顿下限 200」，两者必须一起改。
                            edge_trim_ms=self.init_kwargs.get("edge_trim_ms", 100),
                        )
                        t.normalizer = self.normalizer
                        t.tokenizer = self.tokenizer
                        t.diffusion_steps = self.init_kwargs.get("diffusion_steps", 25)
                        t.inference_cfg_rate = self.init_kwargs.get("inference_cfg_rate", 0.7)
                        self.cfg = t.cfg
                        self._tts = t
                        self._preview_cache = (0.0, None)
                        self._set_phase("ready")
                        logger.info("IndexTTS2 main model loaded.")
                    except Exception:
                        self._set_phase("error")
                        raise
        return self._tts

    def infer(self, *args, **kwargs):
        return self.ensure_loaded().infer(*args, **kwargs)

    def normalize_emo_vec(self, *args, **kwargs):
        # 故意不走 ensure_loaded()：这只是 8 个数的乘加，为此加载 6.7GB 模型
        # 是最贵的错误（详见 server_helpers.normalize_emo_vec 的注释）。
        return _normalize_emo_vec(*args, **kwargs)

    def _apply_backend_flags(self):
        """Apply the live cuDNN benchmark flag.

        Process-global and effective immediately, even on an already loaded
        model (unlike fp16 / s2mel_fp16, which need a rebuild).
        """
        bench = bool(self.init_kwargs.get("cudnn_benchmark", False))
        try:
            import torch
            torch.backends.cudnn.benchmark = bench
        except Exception:
            pass

    _FLAG_MAP = {
        "fp16": "use_fp16",
        "s2mel_fp16": "use_s2mel_fp16",
        "w2v_fp16": "use_w2v_fp16",
        "qwen_fp16": "use_qwen_fp16",
    }

    def state(self):
        k = self.init_kwargs
        return {
            "loaded": self.loaded,
            "phase": self.phase,
            "fp16": bool(k.get("use_fp16", False)),
            "s2mel_fp16": bool(k.get("use_s2mel_fp16", False)),
            "w2v_fp16": bool(k.get("use_w2v_fp16", True)),
            "qwen_fp16": bool(k.get("use_qwen_fp16", True)),
            "cudnn_benchmark": bool(k.get("cudnn_benchmark", False)),
            "diffusion_steps": int(k.get("diffusion_steps", 25)),
            "inference_cfg_rate": float(k.get("inference_cfg_rate", 0.7)),
            "gpt_batch_size": int(k.get("gpt_batch_size", 0)),
        }

    def gpt_batch_state(self):
        """批处理开关的 UI 状态：本机自动行数（不生成也能看到）+ 上次实际运行信息。

        预览要模型已加载才有意义（显存占用此时才是真实值），未加载返回 None，
        前端据此显示"加载后显示本机行数"而不是编一个数。
        """
        t = self._tts
        if t is None:
            self._preview_cache = (0.0, None)
            return {"preview": None, "last": None}
        cached_at, cached = self._preview_cache
        if _INFER_LOCK.locked():
            # 生成中读到的空闲显存没有参考价值（KV cache 正在吃满），沿用上次空闲时的结果
            preview = cached
        elif cached is None or time.time() - cached_at > 4.0:
            preview = cached
            # 非阻塞拿锁 = 此刻必无推理。多标签页 2s 轮询会同时进来，
            # 拿不到锁的那几个直接沿用旧值，不再并发跑 empty_cache +
            # 重复测显存（会反复 cudaMalloc，造成分配器抖动）。
            if _INFER_LOCK.acquire(blocking=False):
                try:
                    preview = t.gpt_batch_preview(refresh=True)
                except Exception:
                    preview = None
                finally:
                    _INFER_LOCK.release()
                self._preview_cache = (time.time(), preview)
        else:
            preview = cached
        return {"preview": preview, "last": getattr(t, "gpt_batch_last", None)}

    def set_runtime(self, **kw):
        """Apply a config dict. fp16 / s2mel_fp16 take effect on the next
        (re)build; diffusion steps, CFG rate and the cuDNN benchmark flag apply
        live.

        不占用 _INFER_LOCK：这里只写几个标量，不碰模型实例也不碰显存。正在
        进行的那次生成早就在 infer_generator 开头把 diffusion_steps /
        inference_cfg_rate 读进了局部变量（infer_v2.py:557-558），本次生成不受
        影响；下一次生成才用新值。原来包着推理锁，导致生成期间点「保存配置」
        实测阻塞 60.76 秒（生成总耗时 85.8 秒）——纯粹的等待，零收益。

        init_kwargs 的写入仍用 _cfg_lock 串起来，免得与 /model/restart 里那次
        set_runtime 交错，出现「一半新一半旧」的组合。
        """
        with self._cfg_lock:
            for key, val in kw.items():
                real = self._FLAG_MAP.get(key, key)
                if key in ("fp16", "s2mel_fp16", "w2v_fp16", "qwen_fp16", "cudnn_benchmark"):
                    val = bool(val)
                elif key == "diffusion_steps":
                    val = int(max(1, min(50, val)))
                elif key == "inference_cfg_rate":
                    val = float(max(0.0, min(1.0, val)))
                elif key == "gpt_batch_size":
                    # 0/1 = 关闭；-1 = 按空闲显存自动；2~12 = 固定行数（超出范围钳制）
                    val = int(val)
                    val = -1 if val < 0 else (0 if val <= 1 else min(12, val))
                self.init_kwargs[real] = val
            # cuDNN benchmark is a process-global torch flag: apply immediately,
            # no model reload needed.
            self._apply_backend_flags()
            if self._tts is not None:
                self._tts.diffusion_steps = int(self.init_kwargs.get("diffusion_steps", 25))
                self._tts.inference_cfg_rate = float(self.init_kwargs.get("inference_cfg_rate", 0.7))
                # 批处理行数是每次生成开头读的标量，改完下一次生成就用新值，无需重启
                self._tts.gpt_batch_size = int(self.init_kwargs.get("gpt_batch_size", 0))

    def unload(self):
        """Drop the loaded TTS model. Only call while no inference is running."""
        old = self._tts
        self._set_phase("unloading")
        self._tts = None
        if old is not None:
            old.cache_spk_cond = None
            old.cache_s2mel_style = None
            old.cache_s2mel_prompt = None
            old.cache_spk_audio_prompt = None
            old.cache_mel = None
            old.cache_emo_cond = None
            old.cache_emo_audio_prompt = None
        self._release_memory()
        self._set_phase("unloaded")

    def rebuild(self):
        # A rebuild must release the old instance before constructing the new
        # one, otherwise both models briefly coexist and can OOM 8GB GPUs.
        old = self._tts
        self._set_phase("reloading")
        self._tts = None
        if old is not None:
            old.cache_spk_cond = None
            old.cache_s2mel_style = None
            old.cache_s2mel_prompt = None
            old.cache_spk_audio_prompt = None
            old.cache_mel = None
            old.cache_emo_cond = None
            old.cache_emo_audio_prompt = None
        self._release_memory()
        del old
        return self.ensure_loaded()

    @staticmethod
    def _release_memory():
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:
            pass


tts = LazyTTS(
    model_dir=cmd_args.model_dir,
    cfg_path=os.path.join(cmd_args.model_dir, "config.yaml"),
    use_fp16=cmd_args.fp16,
    use_s2mel_fp16=cmd_args.s2mel_fp16,
    use_w2v_fp16=cmd_args.w2v_fp16 if cmd_args.w2v_fp16 is not None else True,
    use_qwen_fp16=cmd_args.qwen_fp16 if cmd_args.qwen_fp16 is not None else True,
    diffusion_steps=cmd_args.diffusion_steps,
    inference_cfg_rate=cmd_args.inference_cfg_rate,
    cudnn_benchmark=cmd_args.cudnn_benchmark,
    gpt_batch_size=cmd_args.gpt_batch,
)


def _glossary_store() -> GlossaryStore:
    """管理接口用；store 初始化失败时给明确错误，而不是 None 上继续点属性。"""
    store = tts.glossary_store
    if store is None:
        raise HTTPException(503, "术语规则库初始化失败，请查看 logs/server.log")
    return store


def _glossary_snapshot(scenario_id: str | None):
    """请求级快照。不存在/已删除的场景 → 400，不静默换场景。"""
    store = tts.glossary_store
    if store is None:
        return None
    try:
        return store.snapshot(scenario_id)
    except UnknownScenario:
        raise HTTPException(400, f"场景不存在或已删除：{scenario_id}")

# 全部数据目录锚定到 server.py 所在的项目根，与 presets（_project_root）一致；
# 不用 CWD 相对路径，避免从别的目录启动 `python server.py` 时文件落错位置
OUTPUTS_DIR = os.path.join(current_dir, "outputs")
PROMPTS_DIR = os.path.join(current_dir, "prompts")
# 用户偏好与生成历史的落盘目录。刻意不放 outputs/：那是产物目录，启动时会被
# 保留期清理（OUTPUTS_RETENTION_DAYS），把配置/历史放进去会跟着一起没。
# 定义在模块前部，是因为历史模块（~2071 行）也要用，避免同一常量两处定义。
DATA_DIR = os.path.join(current_dir, "data")
# 字幕缓存目录：与音频同名换成 .json。刻意不放 outputs/——那是产物目录，
# 启动时按保留期清理，字幕放进去会跟着一起没（昨天还能导出、今天 404）。
SUBTITLE_DIR = os.path.join(DATA_DIR, "subtitles")
os.makedirs(OUTPUTS_DIR, exist_ok=True)
os.makedirs(PROMPTS_DIR, exist_ok=True)


def _prompt_file(kind: str, data: bytes) -> str:
    return _prompt_file_impl(PROMPTS_DIR, kind, data)


def _clean_legacy_prompts():
    _clean_legacy_prompts_impl(PROMPTS_DIR)


_clean_legacy_prompts()


# 文本长度：软提示 + 硬上限
#
# TEXT_CHARS_SOFT = 2000 是应用层的经验值，没有任何模型依据
# （模型侧的真实约束是单段 600 token，由 SEG_TOKENS_MAX 管，见下）。
# 它原本是硬拒绝，但实测发现拦不住真正想用的人，只会把长文本需求逼到别处去，
# 所以改成软提示：超过只回一个 warn 字段让前端显示警告照常生成。
#
# TEXT_CHARS_HARD = 10000 是防跑飞的兜底。手滑粘贴几十万字会切出上千段、
# 攒几百 MB 内存、并把 GPU 锁死几十分钟，这一档必须拒。
#
# 提示里必须说清的三件事（否则用户按下生成才发现）：
#   1. 会切成多少段——决定推理耗时（串行，每段都要完整跑 GPT+s2mel+声码器）
#   2. 期间 GPU 被占，其他生成请求全部排队
#   3. 页面关闭/刷新不会中断任务（job 在服务端继续跑）
TEXT_CHARS_SOFT = 2000
TEXT_CHARS_HARD = 10000
MAX_EMO_TEXT_CHARS = 500  # 情感描述文本（送 Qwen 做情感分析）

# 每段 token 上限的合法区间。与 web/index.html 的 #segTokens 滑块
# （min=20 max=600）同口径，前端只是 UI 约束，API 必须自己把关。
#
# 这个值此前完全没校验，后果实测过：
#   0  -> front.py 的 range(0, len, 0) 抛 "range() arg 3 must not be zero"
#         -> HTTP 500，用户看到的是与输入毫无关系的 Internal Server Error
#   -5 -> 片段列表为空 -> split_segments 返回 [] -> 推理端 torch.cat([])
#         -> "expected a non-empty list of Tensors"，同样是指南南的报错
#   1  -> 能跑，但每个 1-token 片段都要走完整的 GPT + s2mel + BigVGAN，
#         一段 30 字的话能切出 38 段，是个没有底的性能悬崖
SEG_TOKENS_MIN, SEG_TOKENS_MAX = 20, 600


def _clamp_seg_tokens(value: int) -> int:
    """把每段 token 上限夹到合法区间，超界给出可读的 400。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "每段 Token 上限必须是整数")
    if n < SEG_TOKENS_MIN or n > SEG_TOKENS_MAX:
        raise HTTPException(
            400,
            f"每段 Token 上限 {n} 超出范围（允许 {SEG_TOKENS_MIN}~{SEG_TOKENS_MAX}）；"
            "调太小会把一句话切成大量碎片，每段都要完整跑一遍 GPT + s2mel + 声码器，"
            "耗时反而暴涨",
        )
    return n


# 生成参数（前端"高级参数"折叠里的 8 个数值）的合法区间，与 web/index.html
# 对应滑块的 min/max 同口径：
#   #emoWeight 0~1 / #temperature 0.1~2 / #topP 0~1 / #topK 0~100（0=关闭过滤，比滑块多一个 0）
#   #numBeams 1~10 / #maxMel 50~1815 / #repPen 0.1~20 / #lenPen -2~2
# 前端只是 UI 约束，API 必须自己把关——之前这 8 项原样透给模型，于是
# temperature=0、num_beams=0 这类值会直接进采样/束搜索，报错信息与输入毫无
# 关系，严重时卡死。
#
# 为什么这 8 项「夹取」而不是像每段 token 那样直接 400：
#   - 越界的后果只是音质/耗时变差，不像段长那样会崩溃或让段数爆炸；
#   - 调用方通常把自己一整套配置原样透传，1e-7 量级的浮点误差不该让整个
#     生成失败。夹到边界 + 记一条 warning，任务仍能正常出结果。
# 每段 token 仍是 400，因为它越界会直接抛 range() 错误（见 _clamp_seg_tokens）。
# 段间停顿（interval_silence）的合法区间与默认值。
# 下限 200 不是拍脑袋：infer_v2 会把每段头/尾多余空白各裁到 edge_trim_ms=100ms
# 再拼接，头尾已经占了 200ms，比这更短的接缝物理上做不到（只能倒扣）。
# 与其静默返回一个用户没设过的值，不如在这里抬到下限，并让前端滑块也以 200 起。
INTERVAL_SILENCE_MIN = 200
INTERVAL_SILENCE_MAX = 500
INTERVAL_SILENCE_DEFAULT = 300
GEN_PARAMS = (
    # (表单字段, 下界, 上界, 是否整数, 默认值)
    ("emo_weight", 0.0, 1.0, False, 0.65),
    ("temperature", 0.1, 2.0, False, 0.8),
    ("top_p", 0.0, 1.0, False, 0.8),
    # 下界取 0 而不是滑块的 1：0 在生成接口里是「关闭 top_k 过滤」的行家写法
    # （下游 1498 行把 0 转成 None）。夹到 1 会把「不过滤」变成「只留最高概率
    # 的那一个 token」，语义正好拧反，音质会明显受损。
    ("top_k", 0, 100, True, 30),
    ("num_beams", 1, 10, True, 3),
    ("max_mel_tokens", 50, 1815, True, 1500),
    # 下界 1.0 而不是 0.1：该参数直通 HF 的 RepetitionPenaltyLogitsProcessor，
    # 语义是 penalty<1 "鼓励重复"（score<0 ? score*penalty : score/penalty），
    # 不是"更弱的惩罚"。实测 0.5 会 100% 生成到撞 max_mel_tokens 上限（音频
    # 29.95s vs 正常 3.4s），0.1 更会让 CUDA 报 device-side assert 并把上下文
    # 打死——之后连默认参数都失败，且 unload/load 救不回来，只能重启进程。
    ("repetition_penalty", 1.0, 20.0, False, 10.0),
    ("length_penalty", -2.0, 2.0, False, 0.0),
)


def _clamp_gen_params(values: dict) -> dict:
    """把 8 个生成参数夹到合法区间，非有限值回退默认值。

    NaN / inf 单独处理是有原因的：float("nan") 能被正常解析，于是
    temperature=nan 会一路带进采样和束搜索，结果是整段静音或直接崩在 CUDA
    kernel 里，而报错信息跟"参数填错"毫无关系。
    """
    out = {}
    for field, lo, hi, integer, dflt in GEN_PARAMS:
        raw = values.get(field, dflt)
        try:
            v = float(raw)
        except (TypeError, ValueError):
            logger.warning("参数 %s=%r 不是数字，回退默认值 %s", field, raw, dflt)
            v = float(dflt)
        else:
            if not math.isfinite(v):
                logger.warning("参数 %s=%r 不是有限数，回退默认值 %s", field, raw, dflt)
                v = float(dflt)
            elif v < lo or v > hi:
                logger.warning("参数 %s=%s 超出范围 [%s, %s]，已夹到边界", field, v, lo, hi)
                v = min(max(v, lo), hi)
        out[field] = int(round(v)) if integer else v
    return out


# ---- 参考音频解码能力 -------------------------------------------------------
# libsndfile 只认 wav/mp3/flac/ogg（含 Ogg 里的 Opus），AAC/M4A/WMA 这类容器
# 需要一个 ffmpeg 才能读。ffmpeg 是可选件：装了就多支持几种格式，没装就只收
# 那四种，行为自动降级（前端 accept 也跟着变，见 /status 的 ffmpeg 字段）。
_FFMPEG_CANDIDATES_REL = (
    os.path.join("bin", "ffmpeg.exe"),
    os.path.join("bin", "ffmpeg"),
    os.path.join("tools", "ffmpeg", "bin", "ffmpeg.exe"),
    os.path.join("tools", "ffmpeg", "bin", "ffmpeg"),
)
_ffmpeg_state = {"checked": False, "path": None}


def _ffmpeg_bin():
    """定位 ffmpeg：包内 bin/ → tools/ffmpeg/bin → 系统 PATH。

    只做存在性判断（/status 每 2 秒轮询，探测必须便宜）；exe 能不能真跑由第一次
    转码的返回码决定。结果进程内缓存，换了 ffmpeg 重启服务即可生效。
    """
    if _ffmpeg_state["checked"]:
        return _ffmpeg_state["path"]
    cands = []
    cands.extend(os.path.join(current_dir, rel) for rel in _FFMPEG_CANDIDATES_REL)
    which = shutil.which("ffmpeg")
    if which:
        cands.append(which)
    found = next((p for p in cands if p and os.path.isfile(p)), None)
    _ffmpeg_state.update(checked=True, path=found)
    return found


def _supported_audio_exts() -> list[str]:
    """本机实际能收的扩展名：基础白名单 +（有 ffmpeg 时的）转码格式。"""
    return list(SUPPORTED_AUDIO_EXTS) + (list(FFMPEG_AUDIO_EXTS) if _ffmpeg_bin() else [])


def _audio_reject_detail(label: str) -> str:
    if _ffmpeg_bin():
        return f"{label}不是可识别的音频文件（支持 {SUPPORTED_AUDIO_TEXT}/{FFMPEG_AUDIO_TEXT}）"
    return (
        f"{label}不是可识别的音频文件（支持 {SUPPORTED_AUDIO_TEXT}；"
        f"{FFMPEG_AUDIO_TEXT} 请先转成 wav 或 mp3 再上传）"
    )


def _transcode_to_wav(
    data: bytes,
    label: str,
    *,
    max_seconds: int | None = None,
    mono: bool = False,
    sample_rate: int | None = None,
) -> bytes:
    """用 ffmpeg 把任意容器解成 16bit PCM WAV。

    默认保持原采样率/声道（音频参考用）；视频提取会传 mono + 22050，
    因为推理侧 librosa 最终也是按 22.05kHz 单声道读的。

    输入先落一个临时文件再交给 ffmpeg：mp4/m4a 的 moov 可能在整个文件末尾，
    纯管道不可回退，大文件会直接「moov atom not found」。临时文件用完即删。
    """
    ff = _ffmpeg_bin()
    if not ff:
        raise HTTPException(400, _audio_reject_detail(label))
    fd, src = tempfile.mkstemp(prefix="indextts_media_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        cmd = [ff, "-v", "error", "-nostdin", "-i", src, "-vn"]
        if max_seconds:
            cmd += ["-t", str(int(max_seconds))]
        if mono:
            cmd += ["-ac", "1"]
        if sample_rate:
            cmd += ["-ar", str(int(sample_rate))]
        cmd += ["-c:a", "pcm_s16le", "-f", "wav", "pipe:1"]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=600)
        except (OSError, subprocess.SubprocessError) as e:
            raise HTTPException(500, f"调用 ffmpeg 解码{label}失败: {e}")
    finally:
        try:
            os.remove(src)
        except OSError:
            pass
    if proc.returncode != 0 or not proc.stdout:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        blob = "\n".join(tail)
        # ffmpeg 的原话是 "Output file does not contain any stream" / "matches no
        # streams"：录屏、动画这类纯画面视频很常见。直接把最后一行 "Invalid
        # argument" 甩给用户等于没说，这里翻成人话。
        if "does not contain any stream" in blob or "matches no streams" in blob:
            raise HTTPException(
                400, f"{label}里没有音轨（可能是静音录屏），请换一个带声音的文件"
            )
        if "moov atom not found" in blob or "Invalid data found when processing input" in blob:
            raise HTTPException(400, f"{label}已损坏或不是完整的音视频文件，无法解码")
        raise HTTPException(
            400,
            f"{label}无法解码成音频" + (f"（{tail[-1][:120]}）" if tail else ""),
        )
    return proc.stdout


def _video_reject_detail() -> str:
    if not _ffmpeg_bin():
        return (
            f"服务端没有 ffmpeg，无法从视频提取音轨（支持 {VIDEO_TEXT}；"
            "没有 ffmpeg 时请先把视频转成 wav 或 mp3）"
        )
    return f"不是可识别的音视频文件（视频支持 {VIDEO_TEXT}）"


def _extract_video_wav(data: bytes, label: str = "视频") -> bytes:
    """视频 → 22.05kHz 单声道 WAV（前 10 分钟），中间临时文件由转码函数清理。"""
    if not data:
        raise HTTPException(400, f"{label}是空文件")
    if len(data) > VIDEO_MAX_BYTES:
        raise HTTPException(
            413,
            f"{label}过大（{len(data) / 1048576:.1f} MB，上限 {VIDEO_MAX_BYTES // 1048576} MB）",
        )
    if not (_audio_kind(data) or _video_kind(data)):
        raise HTTPException(400, _video_reject_detail())
    if not _ffmpeg_bin():
        raise HTTPException(400, _video_reject_detail())
    return _transcode_to_wav(
        data, label,
        max_seconds=VIDEO_MAX_SECONDS, mono=True, sample_rate=22050,
    )


def _check_audio_upload_size(data: bytes, label: str) -> None:
    if not data:
        raise HTTPException(400, f"{label}是空文件")
    if len(data) > AUDIO_MAX_BYTES:
        raise HTTPException(
            413,
            f"{label}过大（{len(data) / 1048576:.1f} MB，上限 {AUDIO_MAX_BYTES // 1048576} MB）",
        )


def _validate_audio_upload(data: bytes, label: str, *, allow_ffmpeg: bool = True) -> None:
    """上传校验：大小上限 + 常见音频容器的魔数嗅探，在落盘前给出可读的 4xx。

    完整解码校验仍由推理侧 librosa 承担（失败经 SSE 报错）；这里只拦截
    明显不是音频的文件（改名的文本/图片等），不做重解码。
    allow_ffmpeg=False 用于只吃「浏览器已经解码成功」的入口（如 /audio/tempo）：
    这些请求的源文件本来就只可能是基础白名单，文案里不该再提 m4a/aac/wma。
    """
    _check_audio_upload_size(data, label)
    if not _looks_like_audio(data):
        if allow_ffmpeg:
            raise HTTPException(400, _audio_reject_detail(label))
        raise HTTPException(
            400, f"{label}不是可识别的音频文件（支持 {SUPPORTED_AUDIO_TEXT}）"
        )


def _prepare_ref_audio(data: bytes, label: str) -> bytes:
    """参考音频落盘前的统一处理：能直接读的保持原字节，读不了的容器交给 ffmpeg 转 WAV。

    只放行白名单里的容器（见 server_helpers.audio_kind）：webm/mp4 视频/amr 这类
    即使 ffmpeg 能解也不收，保证「界面上能选的 = 后端能收的」。
    返回的总是一个 libsndfile 能读的文件，所以 prompts/ 里不会再有「上传成功、
    推理时才炸」的缓存文件；同一段 m4a 重复上传转出的 WAV 字节一致，内容哈希
    仍然命中同一个缓存路径。
    """
    _check_audio_upload_size(data, label)
    kind = _audio_kind(data)
    if kind in ("wav", "mp3", "flac", "ogg"):
        return data
    if kind in ("m4a", "aac", "wma") and _ffmpeg_bin():
        return _transcode_to_wav(data, label)
    raise HTTPException(400, _audio_reject_detail(label))


def _read_upload_capped(f: UploadFile, label: str) -> bytes:
    """分段限长读取上传文件，超限立刻 413，不把整个文件先吃进内存。

    UploadFile.size 是 Starlette 在接收阶段记下的（有 Content-Length 时最准），
    先拿它做零成本预检；read(N+1) 兜住 chunked 传输——最坏也只读上限+1 字节。
    以前四处上传都是 file.read() 全量进内存之后才查上限，--host 0.0.0.0 时
    一个超大上传就能把内存打满。
    """
    limit = AUDIO_MAX_BYTES
    if getattr(f, "size", None) is not None and f.size > limit:
        raise HTTPException(413, f"{label}过大（上限 {limit // 1048576} MB）")
    try:
        data = f.file.read(limit + 1)
    except Exception as e:
        raise HTTPException(400, f"读取{label}失败: {e}")
    if len(data) > limit:
        raise HTTPException(413, f"{label}过大（上限 {limit // 1048576} MB）")
    return data


# outputs/ 保留策略：历史自动清理只覆盖"本次运行"生成过的文件，服务器重启后
# 旧文件全成孤儿，目录会无限膨胀（实测曾积到 489 个 / 207MB）。启动时按
# mtime 清掉超过保留天数的孤儿 wav；预设目录(outputs/presets)一并保护。
OUTPUTS_RETENTION_DAYS = 14


def _clean_stale_outputs(retention_days: int = OUTPUTS_RETENTION_DAYS) -> int:
    """Delete orphan wavs older than retention_days; returns removed count."""
    if retention_days <= 0:
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    try:
        names = os.listdir(OUTPUTS_DIR)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(OUTPUTS_DIR, name)
        if not name.endswith(".wav") or not os.path.isfile(path):
            continue
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed


def _clean_stale_subtitles(retention_days: int = OUTPUTS_RETENTION_DAYS) -> int:
    """清掉「对应音频已不在 outputs/、且自己也过了保留期」的字幕缓存。

    字幕与音频同寿命：音频被保留期清掉（或被用户删除时由 _delete_audio_async
    连带删掉）后，这份缓存永远不会再被读到，留着只会让 data/subtitles/ 无限
    膨胀。天数沿用 OUTPUTS_RETENTION_DAYS，不另设参数。
    """
    if retention_days <= 0:
        return 0
    cutoff = time.time() - retention_days * 86400
    try:
        names = os.listdir(SUBTITLE_DIR)
    except OSError:
        return 0
    removed = 0
    for name in names:
        path = os.path.join(SUBTITLE_DIR, name)
        if not os.path.isfile(path):
            continue
        # save_sidecar 的中间产物（x.json.tmp）在进程被杀时可能留下，一起按名清理
        stem = name[:-4] if name.endswith(".tmp") else name
        if not stem.endswith(".json"):
            continue
        wav = os.path.join(OUTPUTS_DIR, os.path.splitext(stem)[0] + ".wav")
        if os.path.isfile(wav):
            continue  # 音频还在，字幕就还能导出
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed


# 与 _clean_legacy_prompts() 同理放在 import 期：以前只在 __main__ 分支跑，
# `uvicorn server:app` 启动时孤儿 wav 清理永不执行，outputs/ 会无限膨胀。
_removed_outputs = _clean_stale_outputs()
if _removed_outputs:
    logger.info("Cleaned %d output file(s) older than %d days.",
                _removed_outputs, OUTPUTS_RETENTION_DAYS)
_removed_subtitles = _clean_stale_subtitles()
if _removed_subtitles:
    logger.info("Cleaned %d subtitle cache(s) with no matching output.",
                _removed_subtitles)


@api.get("/health")
def health():
    return {"status": "ok", "model_loaded": tts.loaded, "ffmpeg": bool(_ffmpeg_bin())}


# ---- system metrics (v2 UI header gauges) ----
import ctypes


# NVIDIA 在 Windows 上有两个安装位置：
#   新驱动（R440 起）→ %SystemRoot%\System32，天然在 DLL/PATH 搜索范围里；
#   老驱动、部分 OEM 驱动 → %ProgramFiles%\NVIDIA Corporation\NVSMI\，**不在 PATH**。
# 只按裸名找，会在老驱动机器上让 NVML 和 nvidia-smi 两条通道一起失效 ——
# 表现就是别人机器上 GPU/显存两块仪表整块消失、设备名显示成 CPU。
def _nvsmi_dir():
    """NVIDIA 老式安装目录（多数机器上并不存在）。"""
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    return os.path.join(pf, "NVIDIA Corporation", "NVSMI")


_SMI_EXE = {"path": None, "t": 0.0}
_SMI_EXE_RETRY_SEC = 60.0


def _smi_exe():
    """定位 nvidia-smi.exe：PATH 优先，其次 NVSMI 目录。找不到则 60s 后再试。"""
    if _SMI_EXE["path"]:
        return _SMI_EXE["path"]
    now = time.time()
    if _SMI_EXE["t"] and now - _SMI_EXE["t"] < _SMI_EXE_RETRY_SEC:
        return None
    _SMI_EXE["t"] = now
    exe = shutil.which("nvidia-smi")
    if not exe and sys.platform == "win32":
        cand = os.path.join(_nvsmi_dir(), "nvidia-smi.exe")
        if os.path.isfile(cand):
            exe = cand
    _SMI_EXE["path"] = exe
    return exe


def _nvidia_smi(query: str):
    """兜底通道：fork nvidia-smi 解析 stdout。"""
    exe = _smi_exe()
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()[0].strip()
    except Exception:
        pass
    return None


class _NvmlUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlMem(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


# GPU 数据源优先级：进程内 NVML > nvidia-smi 子进程。
# 踩过的坑：某台机器上 nvidia-smi.exe 一启动就报 "Failed to initialize NVML:
# Unknown Error"（驱动升级后命令行外壳与驱动不自洽），但它依赖的 nvml.dll
# 完全正常。旧实现只走 nvidia-smi，一次失败就让显存 / 利用率永久显示 "--"。
# 顺带收益：不用每次刷新都 fork 一个 ~200ms 的子进程。
def _nvml_libs():
    """候选 DLL，按可靠性排序：裸名（System32 在搜索路径里）→ NVSMI 全路径。"""
    if sys.platform == "win32":
        cands = ["nvml.dll"]
        legacy = os.path.join(_nvsmi_dir(), "nvml.dll")
        if os.path.isfile(legacy):
            cands.append(legacy)
        return tuple(cands)
    return ("libnvidia-ml.so.1", "libnvidia-ml.so")


_NVML_LIBS = _nvml_libs()
_NVML = {"lib": None, "handle": None, "dead": False, "dead_t": 0.0}
# 加载失败后的冷却时间。旧实现一旦失败就把 dead 钉死到进程结束 ——
# 开机自启时驱动往往比服务晚就绪，那一次失败会让 GPU 指标整场缺席。
_NVML_RETRY_SEC = 15.0


def _nvml_fail():
    """标记 NVML 不可用并开始冷却；冷却到期后 _nvml_open 会重试一次。"""
    _NVML["dead"] = True
    _NVML["dead_t"] = time.time()
    return False


def _nvml_open() -> bool:
    """惰性加载 nvml 并缓存 device 0 句柄；加载失败则冷却后再试，期间交给 smi 兜底。"""
    if _NVML["handle"] is not None:
        return True
    if _NVML["dead"]:
        if time.time() - _NVML["dead_t"] < _NVML_RETRY_SEC:
            return False
        _NVML["dead"] = False
        _NVML["lib"] = None
    lib = None
    for name in _NVML_LIBS:
        try:
            lib = ctypes.WinDLL(name) if sys.platform == "win32" else ctypes.CDLL(name)
            break
        except OSError:
            continue
    if lib is None:
        return _nvml_fail()
    try:
        init = getattr(lib, "nvmlInit_v2", None) or lib.nvmlInit
        init.restype = ctypes.c_int
        if init() != 0:
            return _nvml_fail()
        get_handle = (
            getattr(lib, "nvmlDeviceGetHandleByIndex_v2", None)
            or lib.nvmlDeviceGetHandleByIndex
        )
        get_handle.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
        get_handle.restype = ctypes.c_int
        handle = ctypes.c_void_p()
        if get_handle(0, ctypes.byref(handle)) != 0 or not handle.value:
            return _nvml_fail()
        lib.nvmlDeviceGetUtilizationRates.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_NvmlUtil)
        ]
        lib.nvmlDeviceGetUtilizationRates.restype = ctypes.c_int
        lib.nvmlDeviceGetMemoryInfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_NvmlMem)
        ]
        lib.nvmlDeviceGetMemoryInfo.restype = ctypes.c_int
        get_name = getattr(lib, "nvmlDeviceGetName", None)
        if get_name is not None:
            get_name.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint]
            get_name.restype = ctypes.c_int
    except Exception:
        return _nvml_fail()
    _NVML["lib"], _NVML["handle"] = lib, handle
    return True


def _nvml_reads():
    """一次读齐 (util%, used_mb, total_mb, name)。

    四项相互独立：某一项读不到只该是 None，不能连其它项一起丢。旧实现在
    util 失败时直接 return None，把本来读得到的显存也一起扔了 —— 部分老卡和
    虚拟化环境的 utilization 会返回 NOT_SUPPORTED，于是整块指标消失。
    全部读不到才返回 None，交给 nvidia-smi 兜底。
    """
    if not _nvml_open():
        return None
    lib, handle = _NVML["lib"], _NVML["handle"]
    util_pct = used_mb = total_mb = name = None
    try:
        util = _NvmlUtil()
        if lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(util)) == 0:
            util_pct = int(util.gpu)
    except Exception:
        pass
    try:
        mem = _NvmlMem()
        if lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)) == 0:
            used_mb = int(mem.used) // 1024**2
            total_mb = int(mem.total) // 1024**2
    except Exception:
        pass
    try:
        get_name = getattr(lib, "nvmlDeviceGetName", None)
        if get_name is not None:
            buf = ctypes.create_string_buffer(96)
            if get_name(handle, buf, len(buf)) == 0:
                name = buf.value.decode("utf-8", "replace").strip() or None
    except Exception:
        pass
    if util_pct is None and used_mb is None and name is None:
        return None
    return (util_pct, used_mb, total_mb, name)


# 静态信息（型号 / 显存总量）缓存，但失败后每 15s 允许重试一次：
# 旧实现只在 import 时读一次，驱动短暂抖动就会让 GPU 指标整场消失。
_GPU_INFO_RETRY_SEC = 15.0
_gpu_info_cache = {"t": 0.0, "info": None}
# 多标签页每 2s 轮询 /status，几个刷新分支会在无锁 dict 上并发读写：
# GIL 下不会崩，但会重复 fork nvidia-smi（每个子进程 ~200ms）。
_GPU_LOCK = threading.Lock()


def _gpu_static_info():
    """返回 {"name", "vram_total_mb"}；无 GPU 或暂时读不到返回 None。"""
    if _gpu_info_cache["info"] is not None:
        return _gpu_info_cache["info"]
    with _GPU_LOCK:
        # 双检：等锁期间别的轮询线程可能已经刷新完
        if _gpu_info_cache["info"] is not None:
            return _gpu_info_cache["info"]
        now = time.time()
        if _gpu_info_cache["t"] and now - _gpu_info_cache["t"] < _GPU_INFO_RETRY_SEC:
            return None
        _gpu_info_cache["t"] = now

        r = _nvml_reads()
        if r is not None and (r[3] or r[2]):
            # 个别驱动版本读不到型号名；退化成 "GPU" 也强过整块指标消失
            info = {"name": r[3] or "GPU", "vram_total_mb": r[2] or None}
        else:
            name = _nvidia_smi("name")
            if not name:
                return None
            total = _nvidia_smi("memory.total")
            try:
                total_mb = int(total) if total else None
            except ValueError:
                total_mb = None  # nvidia-smi 偶发返回 "N/A" 之类，不能让服务起不来
            info = {"name": name, "vram_total_mb": total_mb}
        _gpu_info_cache["info"] = info
        return info


# 多标签页每 2s 轮询一次 /status；1s 缓存避免重复读（NVML 很快，smi 要 fork）。
_smi_cache = {"t": 0.0, "util": None, "used": None}


def _gpu_readings():
    now = time.time()
    if now - _smi_cache["t"] <= 1.0:
        return _smi_cache["util"], _smi_cache["used"]
    with _GPU_LOCK:
        now = time.time()
        if now - _smi_cache["t"] > 1.0:      # 双检：等锁期间可能已被别人刷新
            _smi_cache["t"] = now
            r = _nvml_reads()
            if r is not None:
                _smi_cache["util"], _smi_cache["used"] = r[0], r[1]
            else:
                _smi_cache["util"] = _nvidia_smi("utilization.gpu")
                _smi_cache["used"] = _nvidia_smi("memory.used")
    return _smi_cache["util"], _smi_cache["used"]


@api.get("/metrics")
def metrics():
    try:
        import psutil
    except ImportError:
        psutil = None
    data = {}
    if psutil is not None:
        vm = psutil.virtual_memory()
        data.update({
            "cpu_percent": psutil.cpu_percent(interval=None),
            "mem_used_gb": round(vm.used / 1024**3, 2),
            "mem_total_gb": round(vm.total / 1024**3, 2),
        })
    gpu = _gpu_static_info()
    if gpu:
        util, used = _gpu_readings()
        # 两项各自独立：某个驱动把 utilization 报成 "N/A" 时，不该连显存一起丢
        if util is not None:
            try:
                data["gpu_percent"] = int(util)
            except (TypeError, ValueError):
                pass
        if used is not None:
            try:
                data["vram_used_mb"] = int(used)
            except (TypeError, ValueError):
                pass
        if gpu.get("vram_total_mb"):
            data["vram_total_mb"] = int(gpu["vram_total_mb"])
    data["gpu_name"] = (gpu or {}).get("name")
    return data


# ---- web UI: static files live in web/ and are served from the site root ----
_WEB_DIR = os.path.join(current_dir, "web")

# 静态资源内容指纹：读 web/ 下各文件 md5 前 8 位作版本号，注入 index.html
# 的 ?v= 占位符。文件改动 → 指纹变 → URL 变，配合长缓存头让浏览器自动拉新，
# 不再手工维护 ?v=20260912 这类日期版本号。
# （结果只存在下面的 _ASSET_FP_CACHE 里；原先另有一个只写不读的指纹字典，
#   属于死状态，已删。）

# 指纹缓存：fname -> ((mtime_ns, size), md5_8)
# 原来的实现只在进程启动时算一遍并常驻内存，文件改了字典也不会变 —— 于是改个
# CSS 也必须重启进程才能让浏览器拿到新 URL，而重启会把显存里的模型一并释放，
# 下次还得重新加载。改成请求时按 mtime 判活：没改过就直接用缓存，改过才重算，
# 代价是几次 os.stat（微秒级），换来「改前端只需刷新浏览器」。
_ASSET_FP_CACHE = {}


def _fingerprint(fname: str) -> str:
    path = os.path.join(_WEB_DIR, fname)
    try:
        st = os.stat(path)
    except OSError:
        return "0"
    key = (st.st_mtime_ns, st.st_size)
    hit = _ASSET_FP_CACHE.get(fname)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path, "rb") as f:
            fp = hashlib.md5(f.read()).hexdigest()[:8]
    except OSError:
        return "0"
    _ASSET_FP_CACHE[fname] = (key, fp)
    return fp


def _build_asset_fingerprints():
    for fname in os.listdir(_WEB_DIR):
        fpath = os.path.join(_WEB_DIR, fname)
        if not os.path.isfile(fpath) or fname == "index.html":
            continue
        if _fingerprint(fname) == "0":
            continue


_build_asset_fingerprints()


def _asset_response(name: str):
    """Serve one file from web/ by basename (no path traversal).

    静态资源带一年 immutable 缓存：URL 中的 ?v= 内容指纹变了浏览器才会拉新，
    所以可以放心让浏览器与磁盘缓存长期复用（app.js 52KB + style.css 32KB
    每次刷新都重新下载纯属浪费）。

    例外：web/fonts/ 下的字体文件（100+ 片 woff2）走相对路径引用，
    按子目录整体放行，其余仍只认 basename，防目录穿越。
    """
    fname = os.path.basename(name)
    if "/" in name or "\\" in name:
        # 只放行 fonts/ 与 guide/ 开头的子目录（字体分片、使用指南插图），
        # 且必须仍在各自目录内 —— 其余一律拒绝，防目录穿越。
        parts = [p for p in name.replace("\\", "/").split("/") if p]
        if len(parts) < 2 or parts[0] not in ("fonts", "guide"):
            raise HTTPException(404, "Not found")
        if any(p in (".", "..") for p in parts):
            raise HTTPException(404, "Not found")
        rel = os.path.join(*parts)
        path = os.path.normpath(os.path.join(_WEB_DIR, rel))
        root = os.path.join(_WEB_DIR, parts[0])
        if not path.startswith(root + os.sep):
            raise HTTPException(404, "Not found")
        fname = os.path.basename(path)
    else:
        path = os.path.join(_WEB_DIR, fname)
    if not os.path.isfile(path):
        raise HTTPException(404, "Not found")
    media = {
        ".css": "text/css",
        ".js": "application/javascript",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
    }.get(Path(fname).suffix.lower(), "application/octet-stream")
    return FileResponse(
        path, media_type=media,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/", response_class=FileResponse)
def index():
    path = os.path.join(_WEB_DIR, "index.html")
    if not os.path.exists(path):
        raise HTTPException(404, "web/index.html not found")
    try:
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
    except OSError as e:
        raise HTTPException(500, f"Failed to read index.html: {e}")
    # 把 ?v=xxx 占位符替换为内容指纹；favicon 之类没指纹的保持原样。
    # 每次请求现算（带 mtime 缓存），改完前端刷新即可生效，不必重启服务。
    html = re.sub(r'\?v=\{([a-z0-9_.]+)\}', lambda m: f"?v={_fingerprint(m.group(1))}", html)
    return Response(content=html, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/assets/{name:path}")
def asset(name: str):
    return _asset_response(name)


@api.get("/status")
def status():
    """合并端点：模型状态 + 系统仪表 + 批处理档位，一次往返。

    前端 Header 每 2s 轮询一次这里即可（此前 /model 与 /metrics 各发一次，
    多标签页叠加时请求数翻倍）。
    """
    data = {"ok": True, "generating": _INFER_LOCK.locked(), **tts.state()}
    data.update(metrics())
    # 模型页的「本机实际档位」也搭这趟车：前端 2 秒轮询要顺带刷新模型页状态，
    # 单独再发一次 /model 等于把请求数翻倍，正是这个合并端点要避免的。
    data["gpt_batch"] = tts.gpt_batch_state()
    # 参考音频能收哪些容器取决于有没有 ffmpeg，前端据此切换 accept 与预检
    data["ffmpeg"] = bool(_ffmpeg_bin())
    data["audio_exts"] = _supported_audio_exts()
    data["audio_max_mb"] = AUDIO_MAX_BYTES // 1048576
    data["video_exts"] = list(VIDEO_EXTS) if _ffmpeg_bin() else []
    data["video_max_mb"] = VIDEO_MAX_BYTES // 1048576
    # 句级字幕能力自检：只看运行时包与权重文件，不加载 233MB 模型。
    # 前端据此区分「这台机器缺 ASR」和「这份旧音频生成时还没启用」，
    # 二者都表现为段级字幕，但给用户的下一步完全不同。
    try:
        from indextts.utils.asr_align import align_status
        data["subtitle_align"] = align_status()
    except Exception:
        data["subtitle_align"] = {
            "available": False, "reason": "error", "runtime": False,
            "weights": False, "derived_ready": False,
        }
    return data


@api.get("/model")
def model_state():
    # generating 让模型页在用户点之前就知道「现在动模型会被打断」，
    # 从而把不可用的按钮置灰、并给「卸载」换上会说明后果的确认框。
    data = {"ok": True, "generating": _INFER_LOCK.locked(), **tts.state()}
    # 批处理档位的「本机实际值」：自动模式现在能拼几行、上次生成实际拼了几段。
    data["gpt_batch"] = tts.gpt_batch_state()
    return data


def _generation_elapsed():
    """返回 (是否在生成, 已经跑了多少秒)。

    推理锁在整个生成期间被持有（run_infer 里 acquire 之后包着整段推理），
    所以 locked() 就是可靠信号，不用另建一份状态。秒数从任务注册表的
    started_at 算——它在拿到锁的那一刻才打点。

    只短暂持 _JOBS_LOCK 且不嵌套拿 _INFER_LOCK：推理线程的顺序恒为
    _INFER_LOCK -> _JOBS_LOCK，这里反过来嵌套会死锁。
    """
    if not _INFER_LOCK.locked():
        return False, 0.0
    now = time.time()
    with _JOBS_LOCK:
        starts = [j["started_at"] for j in _JOBS.values()
                  if j.get("started_at") and j["state"] in ("running", "stop_requested")]
    if not starts:
        return True, 0.0
    return True, max(0.0, now - min(starts))


def _reject_if_generating():
    """加载 / 重启模型在生成期间一律拒绝，而不是让调用方静默排队。

    这两个都会重建模型，跑在生成中途等于把正在用的模型换掉。前端已经把按钮
    置灰，这里是给 API 调用方和「2 秒轮询还没刷新到」的竞态兜底。
    卸载不走这条——它有 force=1 的正当中断路径（用户已经确认过后果了）。
    """
    busy, elapsed = _generation_elapsed()
    if busy:
        raise HTTPException(
            409, f"正在生成语音（已进行 {int(elapsed)} 秒），请等本次生成结束后再操作模型。")


# Keys that POST /model/config accepts. `loaded` is state, not a setting;
# unknown keys are typos and must 400 instead of being silently dropped.
_MODEL_CONFIG_KEYS = frozenset({
    "fp16", "s2mel_fp16", "w2v_fp16", "qwen_fp16", "cudnn_benchmark",
    "diffusion_steps", "inference_cfg_rate", "gpt_batch_size",
})
# 每个字段的类型与生效区间（与模型页滑轨、set_runtime 的兜底取同一档位）。
_MODEL_CONFIG_BOOL = ("fp16", "s2mel_fp16", "w2v_fp16", "qwen_fp16", "cudnn_benchmark")
_MODEL_CONFIG_INT = {"diffusion_steps": (1, 50), "gpt_batch_size": (-1, 12)}
_MODEL_CONFIG_FLOAT = {"inference_cfg_rate": (0.0, 1.0)}

# ---- 模型加速配置的落盘持久化 -------------------------------------------
# 「保存配置」之前只写进内存（set_runtime 仅改标量），重启服务全部丢回命令行
# 默认值——用户反馈过这个问题。现在 /model/config 与 /model/restart 改动后
# 会写 model_config.json；启动时在这里读回来套用，保证设置跨重启生效。
_MODEL_CFG_PATH = os.path.join(current_dir, "model_config.json")


def _model_cfg_save():
    """把当前生效的模型配置写进 model_config.json（写失败只记日志，不影响生成）。"""
    try:
        state = {k: v for k, v in tts.state().items() if k in _MODEL_CONFIG_KEYS}
        with open(_MODEL_CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        logger.info("Model config saved to %s", _MODEL_CFG_PATH)
    except Exception as e:
        logger.warning("Failed to save model config: %s", e, exc_info=True)


def _model_cfg_load():
    """启动时把上次保存的模型配置套用回 init_kwargs（坏文件/缺文件直接跳过）。"""
    try:
        with open(_MODEL_CFG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        logger.warning("Ignored unreadable model config %s: %s", _MODEL_CFG_PATH, e)
        return
    if not isinstance(data, dict):
        return
    # JSON 合法但值类型错的配置（如 "diffusion_steps": "abc"、"fp16": null）
    # 以前会直接进 set_runtime，在 import 期抛 TypeError —— 服务根本起不来，
    # 与「坏文件直接跳过」的承诺相反。逐键校验，坏的那一个跳过并留痕。
    kw = {}
    for key, val in data.items():
        if key not in _MODEL_CONFIG_KEYS:
            continue
        try:
            kw[key] = _model_config_value(key, val)
        except HTTPException as e:
            logger.warning("Ignored invalid model config %s=%r: %s", key, val, e.detail)
    if not kw:
        return
    tts.set_runtime(**kw)
    logger.info("Model config restored from %s: %s", _MODEL_CFG_PATH, kw)


# _model_cfg_load() 的调用放在 _model_config_value 定义之后（见下方），
# 否则启动期校验会撞上「函数还没定义」。


def _model_config_number(key: str, val):
    """数值字段的类型校验：None / 布尔 / 非数字字符串一律 400 并点名参数。

    以前这些值直接进 set_runtime，None 会抛 TypeError → 接口 500，前端的 toast
    只剩一句「保存失败」，看不出是哪个字段坏了（BUG-003）。
    """
    if val is None:
        raise HTTPException(400, f"参数 {key} 不能为空（需要数字）")
    if isinstance(val, bool):
        raise HTTPException(400, f"参数 {key} 需要数字，不能是 true/false")
    if isinstance(val, (int, float)):
        if isinstance(val, float) and (val != val or val in (float("inf"), float("-inf"))):
            raise HTTPException(400, f"参数 {key} 不是有效数字")
        return val
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            raise HTTPException(400, f"参数 {key} 需要数字，当前是字符串 {val[:32]!r}")
    raise HTTPException(400, f"参数 {key} 需要数字，当前是 {type(val).__name__}")


def _model_config_value(key: str, val):
    """把 /model/config 的一个字段收敛成 set_runtime 需要的类型，并按界面区间夹取。"""
    if key in _MODEL_CONFIG_BOOL:
        if not isinstance(val, bool):
            raise HTTPException(400, f"参数 {key} 需要 true/false，当前是 {type(val).__name__}")
        return val
    if key in _MODEL_CONFIG_INT:
        lo, hi = _MODEL_CONFIG_INT[key]
        return int(max(lo, min(hi, _model_config_number(key, val))))
    if key in _MODEL_CONFIG_FLOAT:
        lo, hi = _MODEL_CONFIG_FLOAT[key]
        return float(max(lo, min(hi, _model_config_number(key, val))))
    raise HTTPException(400, f"参数 {key} 不受支持")


# 启动恢复：放在 _model_config_value 之后，见 _model_cfg_load 处的说明。
_model_cfg_load()


@api.post("/model/config")
def model_config(req: dict = Body(...)):
    """存加速配置。不排队、不等推理锁——set_runtime 只写标量，见其 docstring。"""
    unknown = [k for k in req if k not in _MODEL_CONFIG_KEYS]
    if unknown:
        raise HTTPException(400, f"Unknown config keys: {', '.join(sorted(unknown))}")
    clean = {key: _model_config_value(key, val) for key, val in req.items()}
    # 一次调用：set_runtime 内部整段持有 _cfg_lock，逐键调用会让两个并发保存
    # 在键与键之间交错，恰好制造它声明要避免的「一半新一半旧」。
    tts.set_runtime(**clean)
    _model_cfg_save()
    cfg = tts.state()
    logger.info("Runtime config updated: steps=%s cfg_rate=%s s2mel_fp16=%s fp16=%s",
                cfg["diffusion_steps"], cfg["inference_cfg_rate"], cfg["s2mel_fp16"], cfg["fp16"])
    return {"ok": True, **tts.state()}


@api.post("/model/load")
def model_load():
    _reject_if_generating()
    with _INFER_LOCK:
        try:
            tts.ensure_loaded()
            result = {"ok": True}
        except Exception as e:
            logger.error("Model load failed: %s", e, exc_info=True)
            result = {"ok": False, "error": str(e)}
    return {**result, **tts.state()}


@api.post("/model/unload")
def model_unload(force: bool = False):
    """Unload the model. With force=1, interrupt a running generation first.

    用户在模型页明确要卸载时，不该被拒绝后再点一次——那是在替他做决定。
    force=1 的语义是「打断当前生成也要卸」，前端会先弹确认框告知后果。

    推理线程在整个生成期间持有 _INFER_LOCK，所以这里不与它抢锁：只置停止
    标志（stop_unload 让推理线程停下后直接卸载、不重建），真正的等待发生在
    下面的 with _INFER_LOCK 上，通常 1~3 秒。
    """
    stopped = False
    if _INFER_LOCK.locked():
        if not force:
            _, elapsed = _generation_elapsed()
            raise HTTPException(
                409,
                f"正在生成语音（已进行 {int(elapsed)} 秒）。"
                "如需中断本次生成并卸载，请带 force=1。",
            )
        with _JOBS_LOCK:
            for job in _JOBS.values():
                if job["state"] == "running":
                    job["stop_unload"] = True
                    job["state"] = "stop_requested"
                    job["stop_event"].set()
                    stopped = True
                    break
        if stopped:
            logger.info("Unload requested with force: interrupting running generation")
    with _INFER_LOCK:
        tts.unload()
    try:
        import gc, torch
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    return {"ok": True, "interrupted": stopped, **tts.state()}


@api.post("/model/restart")
def model_restart(req: dict | None = Body(None)):
    """Apply the page config (when supplied), then rebuild the model."""
    _reject_if_generating()
    if req:
        unknown = [k for k in req if k not in _MODEL_CONFIG_KEYS]
        if unknown:
            raise HTTPException(400, f"Unknown config keys: {', '.join(sorted(unknown))}")
        # 和 /model/config 用同一个校验器：以前这里只看键名不看值，
        # {"diffusion_steps": null} 会 500，{"fp16": null} 会 bool(None)=False
        # 静默关掉 fp16，并且被 _model_cfg_save() 持久化到 model_config.json，
        # 跨重启生效。
        clean = {key: _model_config_value(key, val) for key, val in req.items()}
        # 和 /model/config 一致：写配置不占推理锁，只有 rebuild 才需要
        tts.set_runtime(**clean)
        _model_cfg_save()
    cfg = json.dumps(tts.state(), ensure_ascii=False)
    with _INFER_LOCK:
        try:
            tts.rebuild()
            result = {"ok": True}
        except Exception as e:
            logger.error("Model rebuild failed: %s", e, exc_info=True)
            result = {"ok": False, "error": str(e)}
    if result["ok"]:
        logger.info("Model restarted with config: %s", cfg)
    return {**result, **tts.state()}


# 全局推理锁:同一时刻只允许一个请求占用 GPU。
# 没有它,客户端断开(超时/取消)后 run_infer 线程仍在 GPU 上跑,孤儿请求会越积越多,
# 把后续所有请求拖到分钟级甚至卡死在 ref 阶段。
_INFER_LOCK = threading.Lock()


# 推理任务注册表:让"排队中的任务"可被取消。已在 GPU 上运行的任务不强行中断
# —— 在进度回调处请求停止,随后卸载并重建模型,避免半途模型留下脏状态。
_JOBS_LOCK = threading.Lock()
_JOBS = {}  # job_id -> {"state": ..., "stop_event": threading.Event}
JOB_RETENTION_SECONDS = 300
# 排队上限：每个 /tts 无条件起一个线程 + 一条 SSE 连接，没有上界时多标签页
# 连续提交会把线程和内存堆到服务不可用。超限直接 429，让用户等现有任务。
TTS_QUEUE_MAX = 16


class _InferenceStop(Exception):
    """Raised inside the inference progress callback after a stop request."""


def _job_public(job, now=None):
    """Return a JSON-safe snapshot; call with the jobs lock held."""
    data = {k: v for k, v in job.items() if k not in ("stop_event", "events")}
    if data.get("state") in ("done", "error", "cancelled", "stopped") and data.get("finished_at"):
        now = now or time.time()
        if now - data["finished_at"] >= JOB_RETENTION_SECONDS:
            return None
    return data


def _prune_finished_jobs():
    """Drop terminal snapshots older than the retention window."""
    now = time.time()
    stale = [
        job_id for job_id, job in _JOBS.items()
        if job.get("state") in ("done", "error", "cancelled", "stopped")
        # finished_at 兜底：排队中就被取消的任务不经过 worker 收尾，历史上
        # 这里是 None，`now - None` 会抛 TypeError，把 /jobs/* 和下一次提交
        # 全打成 500。`or now` 把 None 当"刚结束"，照样保留完整的 300 秒。
        and now - (job.get("finished_at") or now) >= JOB_RETENTION_SECONDS
    ]
    for job_id in stale:
        _JOBS.pop(job_id, None)


def _register_job(client_id):
    """Register a queued inference job; returns (job_id, jobs_ahead)."""
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _prune_finished_jobs()
        # 队首正在跑的任务也算「前面还有」——只数 queued 会让「已经有一个在
        # 跑、再提交一个」的第二个任务 ahead=0，界面就不显示排队提示了。
        ahead = sum(1 for j in _JOBS.values() if j["state"] in ("queued", "running"))
        _JOBS[job_id] = {
            "job_id": job_id,
            "client_id": client_id,
            "state": "queued",
            "stop_event": threading.Event(),
            # 停止后不要重建模型，直接卸载（见 /model/unload?force=1）。
            # 停止本身要 rebuild 是因为半途停止会留下脏缓存；卸载是把模型整个
            # 丢掉，脏状态自然消失，没必要先花十几秒建一个马上又要扔的。
            "stop_unload": False,
            "stop_keep_model": False,
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "progress": 0.0,
            "stage_desc": "queued",
        }
    return job_id, ahead


@api.get("/jobs/current")
def jobs_current(client_id: str = ""):
    """Return the newest job belonging to one browser tab."""
    if not client_id:
        raise HTTPException(400, "client_id is required")
    with _JOBS_LOCK:
        _prune_finished_jobs()
        candidates = [
            job for job_id, job in _JOBS.items()
            if job.get("client_id") == client_id
        ]
        job = max(candidates, key=lambda j: j.get("created_at", 0), default=None)
        data = _job_public(job) if job is not None else None
    return {"ok": True, "job": data}


@api.get("/jobs/{job_id}")
def jobs_detail(job_id: str):
    with _JOBS_LOCK:
        _prune_finished_jobs()
        job = _JOBS.get(job_id)
        data = _job_public(job) if job is not None else None
    if data is None:
        raise HTTPException(404, "Job not found or expired")
    return {"ok": True, "job": data}


@api.post("/tts/{job_id}/cancel")
def tts_cancel(job_id: str):
    """Cancel an inference job.

    queued  -> skipped before it ever touches the GPU (no wasted compute);
    running -> cannot be safely interrupted; the response says so and the
               client decides whether to keep waiting.
    """
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found or already finished")
        state = job["state"]
        if state == "queued":
            job["state"] = "cancelled"
            # 排队中的任务还没进 worker，没人会给它写 finished_at。这里必须
            # 自己写：否则 finished_at 停在 None，下次 _prune_finished_jobs
            # 做 now - None 会抛 TypeError，把 /jobs/current、/jobs/{id} 和
            # 下一次提交（_register_job）全部变成 500。
            job["finished_at"] = time.time()
            state = "cancelled"
    return {"ok": True, "state": state}


@api.post("/tts/{job_id}/stop")
def tts_stop(job_id: str, keep_model: bool = False):
    """Stop a queued or running job.

    queued  -> skipped before the GPU is touched;
    running -> stops at the next progress callback, then rebuilds the model
               (or leaves it alone when keep_model=1).
    """
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found or already finished")
        state = job["state"]
        if state == "queued":
            job["state"] = "cancelled"
            # 与 tts_cancel 同理：没进 worker 就没有收尾代码写 finished_at，
            # 留成 None 会让 _prune_finished_jobs 抛 TypeError。
            job["finished_at"] = time.time()
            state = "cancelled"
        elif state == "running":
            job["state"] = "stop_requested"
            job["stop_event"].set()
            if keep_model:
                # 停但不重载：省掉十幾秒的模型重建，下一次生成直接接着用。
                # 安全性依据见 _InferenceStop 分支里 keep_model 那段注释。
                job["stop_keep_model"] = True
            state = "stop_requested"
    return {"ok": True, "state": state}

def _err_detail(e: Exception, cap: int = 300) -> str:
    """把异常收成一条可读的客户端错误文本。

    有些异常 str() 为空（如裸 RuntimeError()），直接插值会让前端只看到
    「Inference failed: 」后面一片空白；超长异常文本则可能把服务端绝对路径
    整段透给客户端。完整信息进服务端日志，这里只截 cap 个字符。
    """
    msg = str(e).strip()
    if not msg or msg == "None":
        msg = f"{type(e).__name__}（无错误信息，详见服务端日志）"
    return msg[:cap]


@api.post("/tts")
def do_tts(
    text: str = Form(...),
    client_id: str = Form(""),
    spk_audio: UploadFile = File(...),
    emo_mode: str = Form("0"),          # 0 same-as-speaker, 1 ref-audio, 2 vector, 3 emo-text
    emo_audio: UploadFile | None = File(None),
    emo_weight: float = Form(0.65),
    vec1: float = Form(0.0),
    vec2: float = Form(0.0),
    vec3: float = Form(0.0),
    vec4: float = Form(0.0),
    vec5: float = Form(0.0),
    vec6: float = Form(0.0),
    vec7: float = Form(0.0),
    vec8: float = Form(0.0),
    emo_text: str = Form(""),
    use_random: bool = Form(False),
    seed: int | None = Form(None),       # 可选复现种子：同时喂 use_random 的 random 与 torch 采样
    max_text_tokens_per_segment: int = Form(120),
    do_sample: bool = Form(True),
    top_p: float = Form(0.8),
    top_k: int = Form(30),
    temperature: float = Form(0.8),
    length_penalty: float = Form(0.0),
    num_beams: int = Form(3),
    repetition_penalty: float = Form(10.0),
    max_mel_tokens: int = Form(1500),
    interval_silence: int = Form(INTERVAL_SILENCE_DEFAULT),  # 段间停顿（毫秒），下限见常量区
    file_naming: str = Form("title_time"),   # title_time / title / default
    # 多候选（F3）：同一轮生成的几条各自是一次普通 /tts，靠这三个字段互相关联。
    # 旧前端不传 → 空组、单条，行为与改动前逐字节一致。
    cand_group: str = Form(""),
    cand_index: int = Form(-1),
    cand_total: int = Form(1),
    # 本次生成使用的术语场景。不传=默认场景（全部启用规则，兼容旧客户端）；
    # 传了不存在的 id 直接 400，避免「看着是 A、实际用默认」的静默错配。
    scenario_id: str | None = Form(None),
):
    if file_naming not in ("title_time", "title", "default"):
        file_naming = "title_time"
    # 组 id 只用来「同组互相找回」，不参与文件名/落盘，截断即可；
    # index/total 越界按边界夹取而不是 400——它们只影响展示，不影响合成结果。
    cand_group = (cand_group or "").strip()[:32]
    if cand_index < -1 or cand_index > 2:
        logger.warning("cand_index 越界（%s），已夹到 0..2", cand_index)
    cand_index = max(-1, min(2, cand_index))
    cand_total = max(1, min(3, cand_total))
    if cand_group and cand_index < 0:
        cand_index = 0
    if not cand_group:
        cand_index = -1
        cand_total = 1
    # 文本可能在到达 HTTP 之前就已乱码（见 _fix_mojibake），先还原再用于合成与命名
    text = _fix_mojibake(text)
    emo_text = _fix_mojibake(emo_text)
    # 纯空白文本：分词后得到空片段列表，推理端 torch.cat([]) 会抛
    # "expected a non-empty list of Tensors" —— 与真实原因无关的误导性报错。
    # 前端已经拦了一道，这里是给 API 调用方的第二道。
    if not text.strip():
        raise HTTPException(400, "文本为空或只含空白字符，请输入要合成的文本")
    if len(text) > TEXT_CHARS_HARD:
        raise HTTPException(
            413,
            f"文本过长（{len(text)} 字，上限 {TEXT_CHARS_HARD} 字）。"
            "这个上限只是防止误粘贴卡死服务；正常长文请分几次生成。",
        )
    # 段间停顿。infer_v2 原本把它写死成 200ms，这里放开给调用方。
    # 下限 200：infer_v2 每段头/尾各保留 100ms 自然收尾，头尾已经占了 200ms，
    #   用户设 100 物理上做不到（只能倒扣）。老客户端仍可能传 10~199，一律抬到 200。
    # 上限 500：再大就不是「停顿」而是「朗读课文」了，超出请分段生成。
    # 注意：签名已是 int = Form(...)，非数字输入在进函数体前就被 FastAPI 以
    # 422 拒绝，和其它 int 参数（max_text_tokens_per_segment 等）行为一致，
    # 这里只需要夹一下范围，不需要再转一次。
    interval_silence = max(INTERVAL_SILENCE_MIN, min(INTERVAL_SILENCE_MAX, interval_silence))
    # 8 个生成参数统一夹取（区间与前端滑块同口径，见 GEN_PARAMS 上方注释）。
    # 这里必须在建 job 之前完成：越界值一旦进了推理线程，报错就与输入无关了。
    _gen = _clamp_gen_params({
        "emo_weight": emo_weight,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "num_beams": num_beams,
        "max_mel_tokens": max_mel_tokens,
        "repetition_penalty": repetition_penalty,
        "length_penalty": length_penalty,
    })
    emo_weight = _gen["emo_weight"]
    temperature = _gen["temperature"]
    top_p = _gen["top_p"]
    top_k = _gen["top_k"]
    num_beams = _gen["num_beams"]
    max_mel_tokens = _gen["max_mel_tokens"]
    repetition_penalty = _gen["repetition_penalty"]
    length_penalty = _gen["length_penalty"]
    # 夹取要放在 warn 之前：提示里的段数必须和 /segments 预览用同一个上限，
    # 否则用户会看到「预览 36 段、提示 21 段」这种自相矛盾的数字。
    max_text_tokens_per_segment = _clamp_seg_tokens(max_text_tokens_per_segment)
    # 在请求线程取一次快照：生成途中切场景/改规则不影响本次任务。
    glossary_snapshot = _glossary_snapshot(scenario_id)
    text_warn = None
    if len(text) > TEXT_CHARS_SOFT:
        # 只警告不拦。段数用和 /segments 同一套分词算真实值；分词器起不来
        # （首次加载失败等）才退回经验公式。tokenizer 一旦建好就常驻，
        # 这里只在超长文本上多一次几十毫秒的分词。
        est_segs = None
        try:
            tts._ensure_light()
            _tokens = tts.tokenizer.tokenize(
                text,
                glossary=glossary_snapshot.glossary if glossary_snapshot else None,
                cascade=glossary_snapshot.cascade if glossary_snapshot else None,
            )
            est_segs = len(tts.tokenizer.split_segments(
                _tokens, max_text_tokens_per_segment=max_text_tokens_per_segment))
        except Exception as e:
            logger.warning("long-text warn: tokenizer unavailable (%s)", e)
        if est_segs:
            est_min = _estimated_minutes(est_segs)
        else:
            est_segs, est_min = _estimate_long_text(len(text), max_text_tokens_per_segment)
        text_warn = _format_long_text_warn(len(text), est_segs, est_min)
    spk_data = _prepare_ref_audio(_read_upload_capped(spk_audio, "音色参考音频"), "音色参考音频")
    spk_path = _prompt_file("spk", spk_data)
    try:
        if os.path.exists(spk_path):
            # LRU touch: cap_prompt_files 按旧 mtime 驱逐，常用音色常驻
            os.utime(spk_path, None)
        else:
            with open(spk_path, "wb") as f:
                f.write(spk_data)
            _cap_prompt_files(PROMPTS_DIR)
    except OSError as e:
        # 服务端写盘失败（磁盘满/权限/目录没了）不是客户端的错，不能回 400
        raise _save_oserror(e, spk_path) from e
    except Exception as e:
        logger.error("Failed to save speaker audio: %s", e, exc_info=True)
        raise HTTPException(500, "服务端保存参考音频失败，请查看服务日志")

    # emo_mode 来自表单是字符串；非数字用 400 明确拒绝，而不是在 int() 上
    # 抛未捕获异常变成 500。越界值（只允许 0~3）同样拒绝——此前 9 之类会
    # 静默落进 else 当成 0 处理。
    # 解析放在情感音频落盘之前：mode≠1 时推理端根本不用这份音频，先写盘
    # 再校验等于「副作用先于校验」。
    try:
        mode = int(emo_mode)
    except (TypeError, ValueError):
        raise HTTPException(400, "情感模式参数无效（应为 0~3 的整数）")
    if mode not in (0, 1, 2, 3):
        raise HTTPException(400, "情感模式参数无效（应为 0~3 的整数）")

    # 描述文本只在模式 3 生效；其它模式下前端可能仍留着隐藏的旧文本，
    # 不能让它把一次与它无关的生成拦下来。
    if mode != 3:
        emo_text = ""
    elif len(emo_text) > MAX_EMO_TEXT_CHARS:
        raise HTTPException(
            413,
            f"情感描述文本过长（{len(emo_text)} 字，上限 {MAX_EMO_TEXT_CHARS} 字）",
        )

    emo_ref_path = None
    if emo_audio is not None and mode == 1:
        emo_data = _prepare_ref_audio(_read_upload_capped(emo_audio, "情感参考音频"), "情感参考音频")
        emo_ref_path = _prompt_file("emo", emo_data)
        try:
            if os.path.exists(emo_ref_path):
                os.utime(emo_ref_path, None)
            else:
                with open(emo_ref_path, "wb") as f:
                    f.write(emo_data)
                _cap_prompt_files(PROMPTS_DIR)
        except OSError as e:
            raise _save_oserror(e, emo_ref_path) from e
        except Exception as e:
            logger.error("Failed to save emo audio: %s", e, exc_info=True)
            raise HTTPException(500, "服务端保存情感参考音频失败，请查看服务日志")

    if mode == 0:
        vec = None
    elif mode == 1:
        # 忘传音频时 infer_v2 会静默回退用 spk 音频做情感参考并强制
        # emo_alpha=1.0：用户以为在用情感参考，实际拿到 mode 0 的效果，
        # emo_weight 也被无视，全程无提示。入口直接拒绝更干净。
        if emo_ref_path is None:
            raise HTTPException(400, "情感模式 1（参考音频）需要上传情感参考音频")
        vec = None
    elif mode == 2:
        # 只钳位置不动 server_helpers：NormalizeEmoVec 要对齐 infer_v2 的逐位结果。
        # NaN 能通过 float("nan") 解析，而 normalize_emo_vec 的 total > 上限
        # 对 NaN 恒 False，NaN 向量会原样进模型（GEN_PARAMS 注释里说过 NaN 会整段
        # 静音或崩在 CUDA kernel 里）。
        raw_vec = [vec1, vec2, vec3, vec4, vec5, vec6, vec7, vec8]
        if not all(math.isfinite(v) for v in raw_vec):
            raise HTTPException(400, "情感向量含 NaN/inf，请检查输入")
        # 纯数学，不加载模型（见 _normalize_emo_vec 的注释）
        vec = _normalize_emo_vec(raw_vec, apply_bias=True)
    else:  # mode == 3
        # 前端与 CLI 都把 emo_text 当必填。infer_v2 在它为 None 时会静默拿
        # 正文当情感描述（emo_text = text），用户以为设了情绪，实际拿到的是
        # 正文自带的情绪，全程无提示——入口直接拒绝更干净。
        if not emo_text.strip():
            raise HTTPException(400, "情感模式 3（描述文本）需要填写情感描述文本")
        vec = None

    if emo_text == "":
        emo_text = None

    t0 = time.time()
    out_path = None  # 在拿到推理锁后由 run_infer 计算（见下）
    progress_q = queue.Queue()
    with _JOBS_LOCK:
        inflight = sum(
            1 for j in _JOBS.values()
            if j["state"] in ("queued", "running", "stop_requested", "stopping")
        )
    if inflight >= TTS_QUEUE_MAX:
        raise HTTPException(429, f"排队任务已达上限（{TTS_QUEUE_MAX}），请等待现有任务完成")
    job_id, jobs_ahead = _register_job(client_id)
    with _JOBS_LOCK:
        stop_event = _JOBS[job_id]["stop_event"]
    logger.info("TTS job %s queued: chars=%d emo_mode=%s ahead=%d naming=%s",
                job_id, len(text), emo_mode, jobs_ahead, file_naming)

    def record_event(ev):
        progress_q.put(ev)
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return
            if ev["type"] == "queue":
                job["stage_desc"] = "排队中"
            elif ev["type"] == "load":
                job["progress"] = 0.08
                job["stage_desc"] = "正在加载模型…"
            elif ev["type"] == "progress":
                job["progress"] = float(ev.get("value") or 0.0)
                job["stage_desc"] = ev.get("desc") or "正在生成语音…"
            elif ev["type"] == "stopping":
                job["state"] = "stopping"
                job["stage_desc"] = "正在停止生成并重新加载模型…"
            elif ev["type"] == "done":
                job["state"] = "done"
                job["finished_at"] = time.time()
                job["stage_desc"] = "生成完成"
                job["result"] = {
                    "wav": ev.get("wav"),
                    "file": ev.get("file"),
                    "elapsed": ev.get("elapsed"),
                    "history_id": ev.get("history_id"),
                    "mel_capped": ev.get("mel_capped", False),
                    "normalize_fallback": ev.get("normalize_fallback"),
                    "subtitle": ev.get("subtitle", False),
                    # /jobs/current 的恢复路径读的就是这个字典：漏写会让刷新后
                    # 候选条退化成单条（见 PLAN §4.5）
                    "cand_group": ev.get("cand_group", ""),
                    "cand_index": ev.get("cand_index", -1),
                    "cand_total": ev.get("cand_total", 1),
                }
                logger.info("TTS job %s done: file=%s elapsed=%ss mel_capped=%s",
                            job_id, ev.get("file"), ev.get("elapsed"), ev.get("mel_capped", False))
            elif ev["type"] == "stopped":
                # 用户主动停止：这是终态，不是失败。cancelled 留给「排队中还没
                # 开始就被取消」，stopped 表示「已经跑起来了、中途停的」——
                # 前端重连时要显示不同的说明，不能混成一个。
                job["state"] = "stopped"
                job["finished_at"] = time.time()
                job["stage_desc"] = "已停止"
                job["message"] = ev.get("detail") or "已停止生成"
                logger.info("TTS job %s stopped: %s", job_id, job["message"])
            elif ev["type"] == "error":
                job["error"] = ev.get("detail") or "生成失败"
                job["finished_at"] = time.time()
                # 兜底：万一还有别的路径用 error 事件收尾一个主动停止（旧调用方、
                # 或事件类型拼错），别把它记成失败。元组里必须有 "stopping"——
                # 上面的 stopping 分支已经把状态从 stop_requested 改写成
                # "stopping" 了，只认前两个的话会落进 else。
                if job["state"] in ("cancelled", "stop_requested", "stopping"):
                    job["state"] = "cancelled"
                    job["stage_desc"] = "已取消"
                    logger.info("TTS job %s cancelled: %s", job_id, job["error"])
                else:
                    job["state"] = "error"
                    job["stage_desc"] = "生成失败"
                    logger.error("TTS job %s failed: %s", job_id, job["error"])

    def run_infer():
        try:
            record_event({"type": "started", "job_id": job_id})
            if text_warn:
                # 软提示：文本过长。不拦，照常生成，但把「会等多久、
                # 期间不能干别的、刷新页面不会中断」讲清楚。
                record_event({"type": "warn", "detail": text_warn})
            if not tts.loaded:
                record_event({"type": "load", "value": 0.0, "desc": "model loading..."})
            if jobs_ahead > 0:
                record_event({"type": "queue", "ahead": jobs_ahead})
            # 排队等锁期间周期性检查取消标志:被取消的排队任务立即短路,
            # 不必等队首推理跑完才被确认(长文本任务可能耗时数分钟)
            while not _INFER_LOCK.acquire(timeout=0.5):
                with _JOBS_LOCK:
                    job = _JOBS.get(job_id)
                    skip = job is not None and job["state"] in ("cancelled", "stop_requested")
                # record_event 内部也要拿 _JOBS_LOCK(不可重入),必须在锁外调用
                if skip:
                    record_event({"type": "error", "detail": "已取消（排队中的任务未开始生成）"})
                    return
            try:
                # queued 阶段被取消:任务从没碰过 GPU,直接短路,不浪费算力。
                # 锁内只置标志、事件上报放到锁外,否则 record_event 会在锁内
                # 再拿同一把不可重入的 _JOBS_LOCK,直接死锁并卡住后续所有请求
                short_circuit = None
                with _JOBS_LOCK:
                    job = _JOBS.get(job_id)
                    if job is not None:
                        if job["state"] == "cancelled":
                            short_circuit = "已取消（排队中的任务未开始生成）"
                        elif job["state"] == "stop_requested":
                            short_circuit = "已停止生成（任务未开始推理）"
                        else:
                            job["state"] = "running"
                            job["started_at"] = time.time()
                            logger.info("TTS job %s started (waited %.1fs)", job_id, job["started_at"] - t0)
                if short_circuit is not None:
                    record_event({"type": "error", "detail": short_circuit})
                    return
                # 输出路径必须在拿到推理锁之后再定：unique_path 只查当时是否
                # 存在，若在排队前计算，同标题的并发任务会拿到同一路径互相覆盖
                nonlocal out_path
                out_path = _output_name(text, file_naming, OUTPUTS_DIR)
                _run_infer_locked()
            finally:
                _INFER_LOCK.release()
        except Exception as e:
            logger.error("TTS job %s worker crashed: %s", job_id, e, exc_info=True)
            # 兜底:任何未捕获异常都必须以终止事件收尾,否则 SSE 会永久挂起。
            try:
                record_event({"type": "error", "detail": f"Inference failed: {_err_detail(e)}"})
            except Exception:
                progress_q.put({"type": "error", "detail": "Inference failed"})

    def _run_infer_locked():
        model = None
        prev_progress = None

        def report_progress(v, desc=""):
            if stop_event.is_set():
                raise _InferenceStop()
            record_event({"type": "progress", "value": round(float(v), 4), "desc": desc})

        try:
            # 模型加载失败必须落到下面的 except:若 ensure_loaded() 在 try 外抛出,
            # worker 会直接退出而不发终止事件,SSE 永久挂起,任务卡在 running
            model = tts.ensure_loaded()
            prev_progress = model.gr_progress
            model.gr_progress = report_progress
            if seed is not None:
                # 种子在推理锁内、infer 之前设置：use_random 的 random.randint 与
                # do_sample 的 torch 采样都在 infer 内部取数，锁保证不被其它任务穿插。
                # 复现仅在同 torch 版本/同硬件下成立。
                import random as _random
                import torch as _torch
                _torch.manual_seed(seed)
                _random.seed(seed)
            result = model.infer(
                spk_audio_prompt=spk_path,
                text=text,
                output_path=out_path,
                emo_audio_prompt=emo_ref_path,
                emo_alpha=emo_weight,
                emo_vector=vec,
                use_emo_text=(mode == 3),
                emo_text=emo_text,
                use_random=use_random,
                verbose=cmd_args.verbose,
                max_text_tokens_per_segment=max_text_tokens_per_segment,
                do_sample=do_sample,
                top_p=top_p,
                top_k=top_k if top_k > 0 else None,
                temperature=temperature,
                length_penalty=length_penalty,
                num_beams=num_beams,
                repetition_penalty=repetition_penalty,
                max_mel_tokens=max_mel_tokens,
                interval_silence=interval_silence,
                glossary=glossary_snapshot.glossary if glossary_snapshot else None,
                cascade=glossary_snapshot.cascade if glossary_snapshot else None,
            )
            if result is None or not os.path.exists(out_path):
                record_event({"type": "error", "detail": "Inference produced no output"})
            else:
                elapsed = round(time.time() - t0, 2)
                wav_name = Path(out_path).name
                entry = _history_add(
                    wav_name, text, elapsed, mode,
                    cand_group=cand_group, cand_index=cand_index, cand_total=cand_total,
                )
                # 触到 max_mel_tokens 上限的生成是静默截断，前端必须提示
                mel_capped = bool(getattr(model, "mel_tokens_capped", False))
                # 文本规范化失败并退回原文同样是无声的（读音不标准但音频出来了），
                # 一起透给前端。None 表示一切正常。
                normalize_fallback = getattr(model, "text_normalize_fallback", None)
                # 字幕 sidecar 只在这一刻拿得到段边界（引擎实例上的本次快照），
                # 存完就与任务无关了；写盘失败不影响音频，只让字幕导出不可用。
                has_subtitle = _subtitle_save(
                    wav_name,
                    getattr(model, "segment_text_method", None),
                    getattr(model, "segment_slices", None),
                    interval_silence,
                    len(str(text or "")),
                    wav_path=out_path,
                )
                record_event({
                    "type": "done",
                    "wav": f"{API_V1}/audio/{wav_name}",
                    "file": wav_name,
                    "elapsed": elapsed,
                    "history_id": entry["id"],
                    "mel_capped": mel_capped,
                    "normalize_fallback": normalize_fallback,
                    "subtitle": has_subtitle,
                    "cand_group": cand_group,
                    "cand_index": cand_index,
                    "cand_total": cand_total,
                })
        except _InferenceStop:
            # 用户在模型页点「卸载模型」时会带上 stop_unload：这时不要重建，
            # 直接卸载即可。重建是「停止」的必要收尾（清掉半途停止留下的脏
            # 缓存），而卸载把模型整个丢掉，脏状态随之消失——先建再扔是白等
            # 十几秒。/model/unload 拿到推理锁后还会再调一次 tts.unload()，
            # 那次是空操作（_tts 已是 None），无害。
            unload_only = False
            keep_model = False
            with _JOBS_LOCK:
                j = _JOBS.get(job_id)
                unload_only = bool(j and j.get("stop_unload"))
                keep_model = bool(j and j.get("stop_keep_model"))
            if unload_only:
                record_event({"type": "stopping", "detail": "正在停止生成并卸载模型…"})
                model = None
                try:
                    tts.unload()
                    detail = "已停止生成，模型已卸载"
                except Exception as e:
                    logger.error("TTS job %s: unload after stop failed: %s", job_id, e, exc_info=True)
                    detail = f"已停止生成，卸载失败: {e}"
            elif keep_model:
                # 停止但不重载：只把回调摘掉、放开局部引用让显存回收，模型本身
                # 原样留着给下一次生成用。停止只能发生在阶段边界的进度回调上
                # （infer_v2.py 的 0.03/0.05/0.1 与每段的 gpt/s2mel/bigvgan），
                # 而参考音频缓存是在 0.05 阶段一次性写完的（连续 5 条赋值，
                # 中间不回调），per-run 标志在下一次推理开头重置，输出 wav
                # 要全部段落跑完才落盘——所以停下来时没有半成品状态。
                record_event({"type": "stopping", "detail": "正在停止生成…"})
                model.gr_progress = prev_progress
                model = None
                gc.collect()
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
                detail = "已停止生成"
            else:
                record_event({"type": "stopping", "detail": "正在停止生成并重新加载模型…"})
                # Unwind the local model reference first so CUDA memory can be released.
                model.gr_progress = prev_progress
                model = None
                try:
                    tts.rebuild()
                    gc.collect()
                    try:
                        import torch
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                    except Exception:
                        pass
                    detail = "已停止生成，模型已重新加载"
                except Exception as e:
                    logger.error("TTS job %s: model reload after stop failed: %s", job_id, e, exc_info=True)
                    detail = f"已停止生成，模型重新加载失败，下次生成将自动加载: {e}"
            record_event({"type": "stopped", "detail": detail})
        except Exception as e:
            logger.error("TTS job %s inference error: %s", job_id, e, exc_info=True)
            record_event({"type": "error", "detail": f"Inference failed: {_err_detail(e)}"})
        finally:
            if model is not None:
                model.gr_progress = prev_progress

    threading.Thread(target=run_infer, daemon=True).start()

    def gen():
        while True:
            try:
                ev = progress_q.get(timeout=30)
            except queue.Empty:
                # 排队/加载可能静默数分钟，没有心跳时代理或浏览器会掐断连接。
                # SSE 注释行浏览器自动忽略，app.js 也按 data: 前缀过滤。
                yield ": keepalive\n\n"
                with _JOBS_LOCK:
                    job = _JOBS.get(job_id)
                # worker 极端情况下（未发终止事件就退出）不再永久挂起占线程
                if job is None or job["state"] in ("done", "error", "cancelled", "stopped"):
                    yield f"data: {json.dumps({'type': 'error', 'detail': '任务已结束但未收到终止事件'})}\n\n"
                    return
                continue
            yield f"data: {json.dumps(ev)}\n\n"
            if ev["type"] in ("done", "error", "stopped"):
                return

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@api.get("/audio/{name}")
def audio(name: str):
    path = os.path.join(OUTPUTS_DIR, os.path.basename(name))
    if not os.path.exists(path):
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="audio/wav")


# ---- tempo (pitch-preserving time stretch) ----------------------------------
# Pure CPU work: deliberately NOT guarded by _INFER_LOCK. A queued long-text
# generation must never be blocked by someone dragging the speed slider.
# Engine: audiotsm's WSOLA (time-domain overlap-add, MIT licensed). The old
# engine was a bare phase vocoder (librosa.effects.time_stretch) which rewrites
# phase without touching magnitude -- overlapping frames then cancel each other
# out, so it sounds metallic ("电音") and the peak came out at ~50% of source.
# WSOLA searches for the best alignment before overlap-add, so energy and
# transients survive (measurements in docs/TEMPO_ENGINE_LICENSE_REVIEW_zh.md).
# audiotsm is imported lazily -- it is only ever needed here, and pulling it in
# at startup would slow down every boot for a feature most runs never touch.
_TEMPO_MIN, _TEMPO_MAX = 0.5, 2.0
_WSOLA_FRAME = 1024          # audiotsm 的默认帧长；显式写出来，补帧时要用
_wsola = None


def _get_wsola():
    global _wsola
    if _wsola is None:
        from audiotsm import wsola  # noqa: PLC0415  (lazy on purpose, see above)
        _wsola = wsola
    return _wsola


def _stretch_wav(in_path: str, rate: float) -> bytes:
    """Read wav -> WSOLA time-stretch -> write 16bit PCM wav into memory."""
    import numpy as np  # noqa: PLC0415
    import soundfile as sf  # noqa: PLC0415
    from audiotsm.io.array import ArrayReader, ArrayWriter  # noqa: PLC0415

    y, sr = sf.read(in_path, dtype="float32")
    if y.ndim > 1:                      # stereo -> mono, the model output is mono anyway
        y = y.mean(axis=1)
    if not y.size:
        raise ValueError("音频没有可处理的采样点")
    n = y.shape[0]
    # WSOLA 按整帧输出，收尾那帧不满就被丢掉：直接跑出来的时长会比理想值短一帧左右
    # （55s 的音频差 60ms，3s 的短音频差 2.5%，肉眼能看出和前端预估对不上）。
    # 所以尾部补一帧静音再按理想长度裁回来 —— 尾巴不被吃掉，时长与「结果 x.xx s」一致。
    src = np.ascontiguousarray(y.reshape(1, -1), dtype="float32")
    src = np.concatenate([src, np.zeros((1, _WSOLA_FRAME), dtype="float32")], axis=1)
    writer = ArrayWriter(1)
    _get_wsola()(channels=1, speed=rate, frame_length=_WSOLA_FRAME).run(ArrayReader(src), writer)
    out = writer.data[0]
    want = int(round(n / rate))
    if out.shape[0] < want:             # 慢速时偶尔短几十个采样，补零对齐
        out = np.concatenate([out, np.zeros(want - out.shape[0], dtype=out.dtype)])
    else:                              # 多出来的是刚补的静音尾巴，裁掉
        out = out[:want]
    buf = io.BytesIO()
    sf.write(buf, out, sr, subtype="PCM_16", format="WAV")
    return buf.getvalue()


def _stretch_from_bytes(data: bytes, rate: float) -> bytes:
    """写临时文件 + WSOLA 整段放进 executor，事件循环不碰磁盘 I/O。"""
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, "in.wav")
        with open(in_path, "wb") as f:
            f.write(data)
        return _stretch_wav(in_path, rate)


def _warmup_tempo():
    """Pay the audiotsm import cost off the request path.

    WSOLA has no JIT to warm up (that was the old phase vocoder's numba kernel,
    ~8s on a fresh install), so this only pulls numpy + audiotsm in and proves
    the dependency is importable -- a missing audiotsm then shows up as a boot
    warning instead of a user staring at "变速计算中…" forever.
    """
    try:
        import numpy as np  # noqa: PLC0415
        from audiotsm.io.array import ArrayReader, ArrayWriter  # noqa: PLC0415

        _get_wsola()(channels=1, speed=1.1).run(
            ArrayReader(np.zeros((1, 4410), dtype="float32")), ArrayWriter(1)   # 0.2s @22.05k
        )
        logger.info("audiotsm wsola warmed up")
    except Exception as e:                       # warmup is best-effort only
        logger.warning("audiotsm warmup failed: %s", e)


@api.post("/audio/tempo")
async def audio_tempo(file: UploadFile = File(...), rate: float = Form(...)):
    """Pitch-preserving speed change. Returns a new wav (not persisted)."""
    # 先按元数据挡，再限量读取：以前是全量 read() 进内存之后才查上限
    if getattr(file, "size", None) is not None and file.size > AUDIO_MAX_BYTES:
        raise HTTPException(413, f"音频过大（上限 {AUDIO_MAX_BYTES // 1048576} MB）")
    data = await file.read(AUDIO_MAX_BYTES + 1)
    if len(data) > AUDIO_MAX_BYTES:
        raise HTTPException(413, f"音频过大（上限 {AUDIO_MAX_BYTES // 1048576} MB）")
    # 这个接口的调用方只有裁剪弹窗的「变速」，源文件必然已经过浏览器解码；
    # 文案里只列基础格式，不提需要 ffmpeg 的 m4a/aac/wma。
    _validate_audio_upload(data, "音频", allow_ffmpeg=False)
    if not (_TEMPO_MIN <= rate <= _TEMPO_MAX):
        raise HTTPException(
            400, f"变速倍率超出范围（{rate}，允许 {_TEMPO_MIN}–{_TEMPO_MAX}）"
        )
    # get_running_loop：本函数是 async def，一定在事件循环里跑。
    # get_event_loop() 在 3.10+ 无运行循环时已弃用（会 DeprecationWarning），
    # 这里取到的本来就是同一个循环，换成不会告警的写法。
    loop = asyncio.get_running_loop()
    try:
        # 写临时文件也挪进 executor：同步写最大 32MB 会卡住整个事件循环，
        # 写盘期间所有并发请求（含 2s 轮询的 /status）一起停摆。
        wav = await loop.run_in_executor(None, _stretch_from_bytes, data, rate)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("tempo failed")
        raise HTTPException(500, f"变速失败：{e}")
    return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "no-cache"})


@api.post("/audio/extract")
async def audio_extract(file: UploadFile = File(...)):
    """从视频（或任意受支持的音频容器）抽出音轨，返回 22.05kHz 单声道 WAV。

    前端选到视频时立刻调用，拿到的 WAV 就是普通参考音频，后续播放、裁剪、
    /tts 上传全走原路径，不额外引入一套「视频参考」状态。
    """
    if not _ffmpeg_bin():
        raise HTTPException(400, _video_reject_detail())
    if getattr(file, "size", None) is not None and file.size > VIDEO_MAX_BYTES:
        raise HTTPException(413, f"视频过大（上限 {VIDEO_MAX_BYTES // 1048576} MB）")
    data = await file.read(VIDEO_MAX_BYTES + 1)
    loop = asyncio.get_running_loop()
    # 同一个端点也承担「浏览器解不了的音频容器」的转码，文案里的名字按实际内容取
    label = "视频" if _video_kind(data) else "音频"
    try:
        # 200MB 视频的 ffmpeg 解码是同步阻塞的，放进线程池，别卡住 /status 轮询
        wav = await loop.run_in_executor(None, _extract_video_wav, data, label)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("video extract failed")
        raise HTTPException(500, f"提取音轨失败：{e}")
    return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "no-cache"})


# ---- generation history -------------------------------------------------------
# 列表落盘到 data/history.json，重启后仍在（上限仍是 HISTORY_LIMIT）。
# 注意「最长 14 天」：outputs/ 里的产物会在启动时被保留期清理，被清掉的文件对应
# 的历史条目在载入时直接丢弃，所以这个文件保证的是「列表不丢」，不是「文件永久保留」。
# 列表不再只由推理线程写：HTTP 线程会清空/单条删除，因此有自己的锁。
# 音频文件的删除一律丢到后台线程——删除在网络盘、杀软扫描、文件被占用时会长时间
# 阻塞，以前它发生在推理线程里、且在 done 事件之前，一阻塞用户就永久卡在
# 「saving audio...」，整个服务排队。
HISTORY_LIMIT = 20          # auto-clean keeps the newest N; older entries drop off

_HISTORY_PATH = os.path.join(DATA_DIR, "history.json")

_history = []               # newest first
_history_auto_clean = True
_HISTORY_LOCK = threading.Lock()


def _delete_audio_async(file_names) -> None:
    """Delete output wavs on a daemon thread so a slow disk can't stall anything.

    字幕 sidecar 跟着音频一起删：只删 wav 会在 data/subtitles/ 里留下永不复活的
    孤儿（历史不再引用它，也不会再有同名产物）。

    最坏情况只是文件暂时残留（甚至删不掉），服务本身照常可用——这是刻意的取舍：
    宁可漏清理，不可让生成或删除请求卡住。
    """
    names = [n for n in file_names if n]
    if not names:
        return

    def run():
        for n in names:
            base = os.path.basename(n)
            for p in (os.path.join(OUTPUTS_DIR, base),
                      sidecar_path(SUBTITLE_DIR, base)):
                try:
                    os.remove(p)
                except OSError:
                    pass

    threading.Thread(target=run, name="history-delete", daemon=True).start()


def _load_history(path: str | None = None) -> None:
    """启动时载入历史；损坏则备份后空启动；产物已不存在的条目直接丢弃。

    path 只为测试注入（与 _persist_history_locked 同口径）：不传时用 _HISTORY_PATH。
    这样测试不必 reload 整个 server 模块——import 期还带着保留期清理、资源指纹等副作用。
    """
    path = path or _HISTORY_PATH
    global _history, _history_auto_clean
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        logger.warning("history.json 无法解析，已备份并从空历史启动", exc_info=True)
        try:
            os.replace(path, f"{path}.bad-{int(time.time())}")
        except OSError:
            pass
        return
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return
    clean = []
    for e in items[:HISTORY_LIMIT]:
        if not isinstance(e, dict):
            continue
        file_name = os.path.basename(str(e.get("file") or ""))
        if not e.get("id") or not file_name:
            continue
        if not os.path.isfile(os.path.join(OUTPUTS_DIR, file_name)):
            continue          # 已手工删除 / 被 14 天保留期清掉：条目没有意义
        e["file"] = file_name
        # url 由文件名单一来源重建：手改过的 JSON 不会把播放链接指到别处
        e["url"] = f"{API_V1}/audio/{quote(file_name)}"
        clean.append(e)
    _history = clean
    _history_auto_clean = bool(data.get("auto_clean", True))
    logger.info("载入生成历史 %d 条（上限 %d）", len(clean), HISTORY_LIMIT)


def _persist_history_locked(path: str | None = None) -> bool:
    """调用方必须已持有 _HISTORY_LOCK。tmp + os.replace，永不半写。

    只 flush 不 fsync：这个写发生在推理线程、done 事件之前，而项目里历史文件的
    删除之所以丢到后台线程，就是因为慢盘（U 盘/网络盘，便携包常见）会卡住推理线程。
    断电最多丢最后一次改动，附属功能可以接受。

    返回值表示是否真的落盘：只读盘 / 磁盘满时前端要能提示「仅本次运行有效」。
    """
    path = path or _HISTORY_PATH
    payload = {"version": 1, "auto_clean": _history_auto_clean, "items": _history}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
        os.replace(tmp, path)
        return True
    except OSError:
        # 历史是附属功能：写盘失败只记日志，绝不让生成/删除请求 500
        logger.warning("history.json 写入失败，本次改动仅内存生效", exc_info=True)
        return False


def _history_add(
    file_name: str,
    text: str,
    elapsed: float,
    emo_mode: int,
    cand_group: str = "",
    cand_index: int = -1,
    cand_total: int = 1,
) -> dict:
    """Record one successful generation and return the new entry."""
    try:
        size = os.path.getsize(os.path.join(OUTPUTS_DIR, file_name))
    except OSError:
        size = 0
    entry = {
        "id": uuid.uuid4().hex[:8],
        "file": file_name,
        "url": f"{API_V1}/audio/{quote(file_name)}",
        "text": _safe_title(text, 60),
        "chars": len(str(text or "")),
        "elapsed": round(float(elapsed), 2),
        "emo_mode": int(emo_mode),
        "size": size,
        "created_at": time.time(),
    }
    # 只在真有候选组时写这三个键：旧条目没有它们 → 前端按单条处理，
    # 也不会让 history.json 因为一个空字段整体多出一份噪音。
    if cand_group:
        entry["cand_group"] = cand_group
        entry["cand_index"] = int(cand_index)
        entry["cand_total"] = int(cand_total)
    with _HISTORY_LOCK:
        _history.insert(0, entry)
        victims = []
        while len(_history) > HISTORY_LIMIT:
            victims.append(_history.pop())
        _persist_history_locked()
    # 条目该丢还是要丢（不论 auto_clean 开关），只有删文件受开关控制；
    # 文件删除丢后台，所以这里不会阻塞推理线程
    if _history_auto_clean:
        _delete_audio_async([e["file"] for e in victims])
    return entry


# 必须在 _clean_stale_outputs()（import 期，~668 行）之后：被 14 天保留期清掉的
# 产物，载入时直接丢弃，不留死条目。
_load_history()


@api.get("/history")
def history_list():
    with _HISTORY_LOCK:
        items = list(_history)          # 快照：避免边删边读拿到不一致的列表
    return JSONResponse({
        "items": items,
        "limit": HISTORY_LIMIT,
        "auto_clean": _history_auto_clean,
        "count": len(items),
    })


@api.post("/history/config")
def history_config(auto_clean: bool = Form(...)):
    global _history_auto_clean
    _history_auto_clean = bool(auto_clean)
    with _HISTORY_LOCK:
        count = len(_history)
        persisted = _persist_history_locked()
    return JSONResponse({
        "auto_clean": _history_auto_clean,
        "count": count,
        "persisted": persisted,     # 只读盘下前端据此回滚开关并提示
    })


@api.delete("/history")
def history_clear():
    """Clear the list; with auto-clean on, the audio files go too (in background)."""
    with _HISTORY_LOCK:
        removed = len(_history)
        victims = [e["file"] for e in _history]
        _history.clear()
        _persist_history_locked()
    # 列表立刻清空（用户视角已生效），文件删除丢后台，删除慢了也不拖住这个请求
    if _history_auto_clean:
        _delete_audio_async(victims)
    return JSONResponse({"ok": True, "removed": removed, "count": 0})


@api.delete("/history/{item_id}")
def history_remove(item_id: str):
    with _HISTORY_LOCK:
        hit = None
        for e in _history:
            if e["id"] == item_id:
                hit = e
                break
        # 原地删除：_history 是全局单例，推理线程会向它 insert；此处一旦重绑定
        # 新列表，并发插入会落到被丢弃的旧列表上（条目和自动清理都丢）。
        if hit is not None:
            _history.remove(hit)
            _persist_history_locked()
        count = len(_history)
    if hit is None:
        raise HTTPException(404, "History entry not found")
    if _history_auto_clean:
        _delete_audio_async([hit["file"]])
    return JSONResponse({"ok": True, "count": count})


@api.get("/presets")
def presets_list():
    return JSONResponse({"presets": list_presets()})


@api.post("/presets")
def presets_create(
    name: str = Form(...),
    emo_control_method: int = Form(0),
    emo_weight: float = Form(0.65),
    vec1: float = Form(0.0),
    vec2: float = Form(0.0),
    vec3: float = Form(0.0),
    vec4: float = Form(0.0),
    vec5: float = Form(0.0),
    vec6: float = Form(0.0),
    vec7: float = Form(0.0),
    vec8: float = Form(0.0),
    emo_text: str = Form(""),
    use_random: bool = Form(False),
    do_sample: bool = Form(True),
    top_p: float = Form(0.8),
    top_k: int = Form(30),
    temperature: float = Form(0.8),
    length_penalty: float = Form(0.0),
    num_beams: int = Form(3),
    repetition_penalty: float = Form(10.0),
    max_mel_tokens: int = Form(1500),
    max_text_tokens_per_segment: int = Form(120),
    interval_silence: int = Form(INTERVAL_SILENCE_DEFAULT),
    prompt_audio: UploadFile | None = File(None),
    emo_audio: UploadFile | None = File(None),
    overwrite: bool = Form(False),
):
    name = _fix_mojibake(name)
    emo_text = _fix_mojibake(emo_text)
    # safe_preset_name 会把 ". / _ / 空格" 这类净化后为空的名字兜底成 "untitled"。
    # 以前这种输入会静默建成一个不叫用户所填名字的预设并返回 200，误操作几次
    # 列表里就堆一串 untitled。这里显式拒绝，而真正想叫 untitled 的不受影响。
    if safe_preset_name(name) == "untitled" and name.strip() != "untitled":
        raise HTTPException(400, "预设名不能为空或只含符号（. / _ / 空格等），请换一个名字")
    # 落盘的参数也要夹：预设是把"上次用的配置"存起来复用，存进 JSON 的越界值
    # 会原样显示在预设卡片上（例如温度 9），应用时又要在 do_tts 里被夹一次。
    # 同一个 helper 处理，保证存进去的就是真正会生效的值。
    _gen = _clamp_gen_params({
        "emo_weight": emo_weight,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "num_beams": num_beams,
        "max_mel_tokens": max_mel_tokens,
        "repetition_penalty": repetition_penalty,
        "length_penalty": length_penalty,
    })
    # 同名保护：未显式声明 overwrite 时返回 409，由前端弹确认，不再静默覆盖
    if preset_exists(name):
        if not overwrite:
            raise HTTPException(409, f"预设「{name}」已存在")
        delete_preset(name)
    raw_vec = [vec1, vec2, vec3, vec4, vec5, vec6, vec7, vec8]
    # NaN/inf 不能进预设 JSON（JSON 本身也不合法），且应用时会原样进模型
    if not all(math.isfinite(float(v)) for v in raw_vec):
        raise HTTPException(400, "情感向量含 NaN/inf，请检查输入")
    data = {
        "emo_control_method": int(emo_control_method),
        "emo_weight": _gen["emo_weight"],
        "emo_vector": [float(v) for v in raw_vec],
        "emo_text": (emo_text or "") if emo_control_method == 3 else "",
        "emo_random": bool(use_random),
        "advanced_params": {
            "do_sample": bool(do_sample),
            "top_p": _gen["top_p"],
            "top_k": _gen["top_k"],
            "temperature": _gen["temperature"],
            "length_penalty": _gen["length_penalty"],
            "num_beams": _gen["num_beams"],
            "repetition_penalty": _gen["repetition_penalty"],
            "max_mel_tokens": _gen["max_mel_tokens"],
            # 与 do_tts 同款校验：以前只 int() 不查范围，越界值照样入库
            "max_text_tokens_per_segment": _clamp_seg_tokens(max_text_tokens_per_segment),
            "interval_silence": max(INTERVAL_SILENCE_MIN, min(INTERVAL_SILENCE_MAX, int(interval_silence))),
        },
    }

    prompt_tmp = None
    emo_tmp = None
    try:
        if prompt_audio is not None:
            prompt_data = _prepare_ref_audio(_read_upload_capped(prompt_audio, "预设音色音频"), "预设音色音频")
            prompt_tmp = os.path.join(PROMPTS_DIR, f"preset_prompt_{uuid.uuid4().hex}.wav")
            with open(prompt_tmp, "wb") as f:
                f.write(prompt_data)
            data["prompt_audio_name"] = os.path.basename(prompt_audio.filename or "")[:200]
        if emo_audio is not None:
            emo_data = _prepare_ref_audio(_read_upload_capped(emo_audio, "预设情感音频"), "预设情感音频")
            emo_tmp = os.path.join(PROMPTS_DIR, f"preset_emo_{uuid.uuid4().hex}.wav")
            with open(emo_tmp, "wb") as f:
                f.write(emo_data)
            data["emo_audio_name"] = os.path.basename(emo_audio.filename or "")[:200]
        save_preset(name, data, prompt_audio=prompt_tmp, emo_audio=emo_tmp)
    except HTTPException:
        # _prepare_ref_audio 抛的 400/413 已经带着可读原因，必须原样上抛。
        # 落进下面的 except Exception 会被重包成 500，用户只看到
        # "Failed to save preset: 400: 预设音色音频不是可识别的音频文件"——
        # 一条既说 500 又夹着 400 的报错（/audio/tempo 早就是这么写的，这里漏了）。
        raise
    except Exception as e:
        logger.exception("保存预设「%s」失败", name)
        raise HTTPException(500, f"Failed to save preset: {e}")
    finally:
        for p in (prompt_tmp, emo_tmp):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass
    return JSONResponse({"presets": list_presets()})


@api.get("/presets/{name}")
def presets_detail(name: str, legacy: bool = False):
    try:
        data = load_preset(name)
    except ValueError as e:
        # 单个预设文件损坏不该让整个预设页 500，也不该把 traceback 甩给客户端
        raise HTTPException(422, str(e))
    if data is None:
        raise HTTPException(404, "Preset not found")
    # 兼容老客户端：带 ?legacy=1 时退回不含 interval_silence 的旧结构。
    # 新增字段本身是向后兼容的加法，但严格 schema 校验（extra=forbid /
    # additionalProperties:false）的第三方插件会因此报错，留一个逃生门。
    if legacy:
        adv = data.get("advanced_params")
        if isinstance(adv, dict):
            adv.pop("interval_silence", None)
    # load_preset 把音频字段转成了服务器绝对路径，只对内使用；
    # 响应里换成语 URL，避免把服务器目录结构暴露给客户端
    if data.get("prompt_audio"):
        data["prompt_audio_url"] = f"{API_V1}/presets/{quote(name)}/audio/prompt"
    if data.get("emo_audio"):
        data["emo_audio_url"] = f"{API_V1}/presets/{quote(name)}/audio/emo_ref"
    data.pop("prompt_audio", None)
    data.pop("emo_audio", None)
    return JSONResponse(data)


@api.get("/presets/{name}/audio/{kind}")
def presets_audio(name: str, kind: str):
    rel = {"prompt": "prompt.wav", "emo_ref": "emo_ref.wav"}.get(kind)
    if rel is None:
        raise HTTPException(404, "Not found")
    path = os.path.join(get_presets_dir(), safe_preset_name(name), rel)
    if not os.path.exists(path):
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="audio/wav")


@api.delete("/presets/{name}")
def presets_remove(name: str):
    ok = delete_preset(name)
    if not ok:
        raise HTTPException(404, "Preset not found")
    return JSONResponse({"presets": list_presets()})


@api.post("/presets/{name}/rename")
def presets_rename(name: str, req: dict = Body(...)):
    """改名：保留目录内音频与参数，只换目录名。"""
    new_name = req.get("name")
    if not new_name or not str(new_name).strip():
        raise HTTPException(400, "新名称不能为空")
    new_name = _fix_mojibake(str(new_name))
    # 与创建路径同款拦截：改名成 ". / _ / 空格" 只会被净化成 untitled，
    # 以前会 200 成功并把预设悄悄改名为一个用户没输入过的名字。
    if safe_preset_name(new_name) == "untitled" and new_name.strip() != "untitled":
        raise HTTPException(400, "新名称不能为空或只含符号（. / _ / 空格等），请换一个名字")
    try:
        final = rename_preset(name, new_name)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return JSONResponse({"ok": True, "name": final, "presets": list_presets()})


@api.post("/presets/{name}/duplicate")
def presets_duplicate(name: str, req: dict | None = Body(None)):
    """复制预设；未指定新名时默认「原名_copy」。"""
    new_name = ((req or {}).get("name") or "").strip() or f"{name}_copy"
    new_name = _fix_mojibake(str(new_name))
    # 与创建/改名路径同款拦截：传 ". / _ / 空格" 只会被净化成 untitled，
    # 以前会 200 成功并建出一个用户没输入过名字的预设。
    if safe_preset_name(new_name) == "untitled" and new_name.strip() != "untitled":
        raise HTTPException(400, "新名称不能为空或只含符号（. / _ / 空格等），请换一个名字")
    try:
        final = duplicate_preset(name, new_name)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return JSONResponse({"ok": True, "name": final, "presets": list_presets()})


# ---- UI 偏好：参数锁定 ---------------------------------------------------------
# 存磁盘而不是浏览器 localStorage：用户清缓存后锁定的参数不该丢。
# 文件放在项目根的 data/ 下（DATA_DIR，见 OUTPUTS_DIR 附近的说明）。
_UI_STATE_DIR = DATA_DIR
_UI_STATE_PATH = os.path.join(_UI_STATE_DIR, "ui_state.json")
_UI_STATE_LOCK = threading.Lock()
# 锁定的参数白名单：与前端「高级参数」折叠里的 9 项一致。
# segTokens 不在此列 —— 它属于「怎么切句」，不是音色质量参数。
_LOCK_PARAM_KEYS = frozenset({
    "doSample", "temperature", "topP", "topK", "numBeams",
    "maxMel", "repPen", "lenPen", "gapMs",
})


def _read_ui_state() -> dict:
    """Read data/ui_state.json; a missing or corrupt file is an empty state."""
    try:
        with open(_UI_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("ui_state.json 无法解析，按空状态处理", exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _write_ui_state(state: dict) -> None:
    """Atomically replace ui_state.json (tmp + os.replace, same volume)."""
    os.makedirs(_UI_STATE_DIR, exist_ok=True)
    tmp = _UI_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _UI_STATE_PATH)


def _ui_state_write(key: str, value) -> None:
    """在锁里写一个偏好键（value 为 None 表示删除该键）。

    直接让 OSError 冒出去会变成 500 + 堆栈，用户看不出是磁盘写不了。
    """
    with _UI_STATE_LOCK:
        state = _read_ui_state()
        if value is None:
            state.pop(key, None)
        else:
            state[key] = value
        try:
            _write_ui_state(state)
        except OSError as e:
            logger.exception("写入 ui_state.json 失败，本次修改未保存")
            raise HTTPException(503, f"界面偏好写入失败（{e.strerror or e.__class__.__name__}），请稍后重试") from e


@api.get("/prefs/param-lock")
def prefs_param_lock_get():
    with _UI_STATE_LOCK:
        payload = _read_ui_state().get("paramLock")
    if not isinstance(payload, dict):
        payload = {"locked": False, "values": {}}
    return JSONResponse(payload)


@api.put("/prefs/param-lock")
def prefs_param_lock_put(req: dict = Body(...)):
    # locked 必须显式给出：以前用 req.get("locked", False)，字段名拼错（如 "lock"）
    # 会静默取默认值 False，把用户已保存的参数锁定整份清空，且返回 200 毫无提示。
    if "locked" not in req:
        raise HTTPException(400, "缺少 locked 字段（是否锁定的开关）；要清除锁定请显式传 locked=false")
    locked = req["locked"]
    if not isinstance(locked, bool):
        raise HTTPException(400, "locked 必须是布尔值")
    raw = req.get("values") or {}
    if not isinstance(raw, dict):
        raise HTTPException(400, "values 必须是对象")
    unknown = [k for k in raw if k not in _LOCK_PARAM_KEYS]
    if unknown:
        raise HTTPException(400, f"未知参数: {', '.join(sorted(unknown))}")
    values = {}
    for key, val in raw.items():
        if key == "doSample":
            values[key] = bool(val)
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            raise HTTPException(400, f"参数 {key} 不是数字")
        if num != num or num in (float("inf"), float("-inf")):
            raise HTTPException(400, f"参数 {key} 不是有效数字")
        values[key] = str(val).strip()
    payload = {"locked": locked, "values": values, "updated_at": int(time.time())}
    _ui_state_write("paramLock", payload)
    return JSONResponse(payload)


# ---- UI 偏好：音频保存目录 ----------------------------------------------------
# 目录存在服务端 data/ui_state.json 而不是浏览器：换浏览器、清缓存、换端口都不丢。
# 落盘由服务端完成（复制/接收上传），所以任何浏览器都能用；代价是从别的设备访问时，
# 文件会写到运行服务的这台电脑上，而不是当前设备的磁盘。
_SAVE_DIR_KEY = "saveDir"
_PICK_LOCK = threading.Lock()
_LOCAL_CLIENTS = ("127.0.0.1", "::1", "localhost")


def _is_local_client(request: Request) -> bool:
    return bool(request.client) and request.client.host in _LOCAL_CLIENTS


def _save_dir_get() -> str:
    with _UI_STATE_LOCK:
        d = _read_ui_state().get(_SAVE_DIR_KEY)
    return d if isinstance(d, str) else ""


# ---- 目录写探针（BUG-001 的护栏）------------------------------------------------
# 探针必须真写一个文件：Windows 上 os.access(W_OK) 对只读目录也会返回 True。
# 但受保护的目录（如 C:\Windows\System32）上创建文件可能长时间不返回——权限重定向
# 或安全软件拦在那里。这些端点是同步 def，跑在 anyio 的默认线程池（40 个线程）里，
# 挂住的线程不会释放；实测 45 次请求后线程池耗尽，连 /health、/status 都排队超时。
# 所以三层护栏：探针跑在自己的一次性 daemon 线程里、单次限时 2s、结果按目录缓存 15s。
#   - 超时：判"不可写"并返回，调用方最多等 2s，永远不会把请求吊死；
#   - 卡死的探针线程是 daemon、不占共享池，池不存在"耗尽"这回事
#     （旧的 2-worker 私有池会：cancel() 对已执行的 future 无效，两次挂死后
#     后续每次 probe 都要在串行锁下等满 2s）；
#   - 缓存让同一目录的并发/连续请求只探一次，重复点击不再叠加开销。
_PROBE_TIMEOUT_S = 2.0
_PROBE_CACHE_TTL_S = 15.0
_PROBE_SERIAL_LOCK = threading.Lock()
_PROBE_CACHE_LOCK = threading.Lock()
# path -> (是目录, 可写, 原因, 采样时刻)
_PROBE_CACHE: dict[str, tuple[bool, bool, str, float]] = {}


def _write_probe(path: str) -> tuple[bool, str]:
    """真的建一个临时文件再删掉。返回 (是否可写, 不可写的原因)。"""
    probe = os.path.join(path, f".indextts-probe-{os.getpid()}-{threading.get_ident()}.tmp")
    fd = -1
    created = False
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created = True
        os.write(fd, b"")
        return True, ""
    except FileNotFoundError:
        return False, "目录已不存在"
    except PermissionError:
        return False, "没有写入权限"
    except OSError as e:
        return False, f"无法创建文件（{e.strerror or e.__class__.__name__}）"
    finally:
        # 探针文件用完就删，绝不在用户目录里留垃圾；删不掉也不影响判定结果。
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if created:
            try:
                os.remove(probe)
            except OSError as e:
                logger.warning("探针临时文件未能删除：%s -> %s", probe, e)


def _dir_probe(path: str) -> tuple[bool, bool, str]:
    """在线程里一次问齐：是不是目录 / 可写 / 原因。

    `os.path.isdir` 和 `os.path.exists` 也不是免费的——指向断掉的网卡路径
    （\\\\10.255.255.1\\share）时实测阻塞 26.7 秒才返回。所以存在性检查和写
    探针必须待在同一个限时线程里，只把写探针挪进池子等于没修完。
    """
    if not os.path.isdir(path):
        if os.path.exists(path):
            return False, False, "这是一个文件，不是目录"
        return False, False, "目录不存在"
    writable, reason = _write_probe(path)
    return True, writable, reason


def _probe_cached(path: str) -> tuple[bool, bool, str] | None:
    with _PROBE_CACHE_LOCK:
        hit = _PROBE_CACHE.get(path)
        if hit and time.monotonic() - hit[3] < _PROBE_CACHE_TTL_S:
            return hit[0], hit[1], hit[2]
    return None


def _probe_dir(path: str, use_cache: bool = True) -> tuple[bool, bool, str]:
    """带超时与缓存的目录探针，返回 (是目录, 可写, 原因)。

    并发时全局串行，后来的请求先命中缓存；超时按「不可用」返回。
    探针放在一次性 daemon 线程里：卡死的线程不会拖住共享的 anyio 线程池，
    也不存在固定小池被永久占满后「后续每次保存都慢 2s」的问题。

    use_cache=False 给真正要写文件的路径用：缓存最长 15 秒，目录在这期间被删掉
    时不能再拿旧判定说它「可用」，否则错误会推迟到写盘那一刻才以另一种面目出现。
    不走缓存也不等于回到裸调用——探测照样丢进限时线程，卡死的目录不会把请求拖住。
    """
    if use_cache:
        cached = _probe_cached(path)
        if cached is not None:
            return cached
    with _PROBE_SERIAL_LOCK:
        if use_cache:
            cached = _probe_cached(path)          # 双检：等锁期间别人可能已经探完
            if cached is not None:
                return cached
        res_q: queue.Queue = queue.Queue(maxsize=1)
        threading.Thread(
            target=lambda: res_q.put(_dir_probe(path)),
            name="savedir-probe",
            daemon=True,
        ).start()
        try:
            is_dir, writable, reason = res_q.get(timeout=_PROBE_TIMEOUT_S)
        except queue.Empty:
            is_dir, writable = False, False
            reason = f"目录检查超过 {_PROBE_TIMEOUT_S:g}s 未返回，按不可用处理"
            logger.warning("保存目录探针超时：%s", path)
        except Exception as e:                # 探针自身的意外不能让端点 500
            is_dir, writable = False, False
            reason = f"目录检查异常（{e.__class__.__name__}）"
            logger.warning("保存目录探针异常：%s -> %s", path, e)
        if use_cache:
            with _PROBE_CACHE_LOCK:
                _PROBE_CACHE[path] = (is_dir, writable, reason, time.monotonic())
        return is_dir, writable, reason


def _save_dir_info(path: str, use_cache: bool = True) -> dict:
    """目录状态。不可用时给出具体原因（BUG-005），路径只在日志里出现。"""
    if not path:
        return {"dir": "", "exists": False, "writable": False}
    exists, writable, reason = _probe_dir(path, use_cache=use_cache)
    info = {"dir": path, "exists": exists, "writable": writable}
    if reason:
        info["reason"] = reason
    return info


_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WIN_STEMS = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
}

# 文件名长度预算。Windows 单个名字上限 255，但真正会撞的是整条路径的 MAX_PATH(260)；
# 未限长时 300 字的名字直接让 open 抛 [Errno 22]，接口回 500 并把服务端绝对路径
# 原样吐给前端（BUG-002）。这里提前把名字截短，让超长变成一个可读的 400 之外的事——
# 根本不报错，只是自动截。
_NAME_UNITS_MAX = 100      # 单个文件名（含 .wav）允许的 UTF-16 单元数
_SUFFIX_RESERVE = 12       # 给 ".wav" 和可能追加的 " (9999)" 留出来的余量
_PATH_UNITS_MAX = 250      # 整条路径（目录 + 文件名）预算，MAX_PATH 260 留 10 单元


def _win_units(text: str) -> int:
    """Windows 认为这个字符串有多长：UTF-16 单元数（补充平面字符算 2 个）。"""
    return len(text.encode("utf-16-le")) // 2


def _stem_budget(folder: str) -> int:
    """stem 还能用多少单元：既看名字上限，也看目录本身占了多长的路。"""
    budget = _NAME_UNITS_MAX - _SUFFIX_RESERVE
    if folder:
        left = _PATH_UNITS_MAX - _win_units(os.path.abspath(folder)) - 1 - _SUFFIX_RESERVE
        budget = min(budget, left)
    return max(8, budget)


def _truncate_to_units(text: str, units: int) -> str:
    out = []
    used = 0
    for ch in text:
        w = _win_units(ch)
        if used + w > units:
            break
        out.append(ch)
        used += w
    return "".join(out)


# 保存目录只接受这两种扩展名，且必须与内容嗅探结果一致（见 save_dir_upload）
_SAVE_AUDIO_EXTS = (".wav", ".mp3")


def _safe_audio_name(
    name: str, fallback: str = "audio.wav", folder: str = "", allowed_exts: tuple[str, ...] = (".wav",)
) -> str:
    """收敛成"目录名 + 一个文件名"：去路径、去非法字符、补扩展名、按预算截断。

    allowed_exts 默认只有 .wav，旧的复制/保存路径行为逐字节不变；
    只有 save_dir_upload（浏览器算出的 WAV/MP3）会传 (".wav", ".mp3")。
    """
    base = os.path.basename((name or "").strip().replace("\\", "/"))
    base = _INVALID_NAME_CHARS.sub("_", base).strip(" .")
    if not base:
        base = fallback
    stem, ext = os.path.splitext(base)
    if ext.lower() not in allowed_exts:
        # 不认识的后缀当成名字的一部分：a.foo → a.foo.wav，不吞掉用户的名字
        stem = base
        ext = allowed_exts[0]
    stem = stem.strip(" .")
    if not stem:
        stem = "audio"
    if stem.upper() in _RESERVED_WIN_STEMS:
        stem = "_" + stem
    stem = _truncate_to_units(stem, _stem_budget(folder)).rstrip(" .")
    if not stem:
        stem = "audio"
    return stem + ext.lower()


def _claim_unique_dest(folder: str, name: str) -> tuple[int, str]:
    """原子占名：用 O_CREAT|O_EXCL 抢一个还没被占用的文件名，返回 (已打开的 fd, 路径)。

    旧写法是 os.path.exists 判空后再 open(dest, "wb")——两个请求同时判"不 exists"，
    就会同时打开同一个路径，后写入的那份把前一份静默覆盖掉，而两次请求都回 200、
    界面弹两次"已保存"，用户以为存了两份（BUG-007，实测 20 并发只剩 18 个文件）。
    占名与创建合并成一次系统调用后，抢不到就往后换名，同名文件不可能互相覆盖。
    调用方负责写完 close（异常路径里会顺手删掉半成品）。
    """
    stem, ext = os.path.splitext(name)
    last_err: OSError | None = None
    for n in range(0, 10000):
        cand_name = f"{stem}{ext}" if n == 0 else f"{stem} ({n}){ext}"
        cand = os.path.join(folder, cand_name)
        try:
            return os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666), cand
        except FileExistsError:
            continue                      # 名字被占，试下一个
        except OSError as e:
            # 目录整个没了/没权限：换名字也不会成功，立刻停下报可读错误
            last_err = e
            break
    if last_err is not None:
        raise _save_oserror(last_err, os.path.join(folder, name)) from last_err
    raise HTTPException(409, "目标目录里同名文件过多（已超过 9999 个），无法生成新文件名")


def _save_oserror(e: OSError, dest: str = "") -> HTTPException:
    """把落盘的 OSError 翻译成可读、且不泄露服务端绝对路径的 HTTP 错误。

    路径只写进日志（运维要看），不给前端（用户看了没用，还多露一层内部结构）。
    """
    logger.warning("保存到目录失败：%s -> %r", dest or "(未确定)", e)
    err = getattr(e, "errno", None)
    if isinstance(e, FileNotFoundError) or err == errno.ENOENT:
        return HTTPException(409, "已设置的保存目录不存在，请重新选择目录")
    if isinstance(e, NotADirectoryError) or err == errno.ENOTDIR:
        return HTTPException(409, "已设置的保存路径不是目录，请重新选择")
    if isinstance(e, PermissionError) or err in (errno.EACCES, errno.EPERM):
        return HTTPException(403, "保存目录不可写，请换一个目录")
    if err == errno.ENAMETOOLONG:
        return HTTPException(400, "文件名过长，请缩短后再保存")
    if err == errno.ENOSPC:
        return HTTPException(507, "磁盘空间不足，无法写入保存目录")
    return HTTPException(500, f"保存失败：{e.strerror or e.__class__.__name__}")


def _discard_partial(dest: str) -> None:
    """写入失败时清掉占名产生的半成品，别在用户目录里留 0 字节的坏文件。"""
    try:
        os.remove(dest)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("清理未写完的保存文件失败：%s -> %s", dest, e)


@contextlib.contextmanager
def _save_writer(folder: str, name: str, allowed_exts: tuple[str, ...] = (".wav",)):
    """占一个不覆盖别人的文件名，并打开写入流。yield (dest, fh)。

    失败路径一律：关流 → 删半成品 → 翻译成可读 HTTP 错误。成功路径只 flush，
    不做 fsync——保存端点是同步 handler，任何无上限的阻塞都会吃线程（BUG-001）。
    """
    fd, dest = _claim_unique_dest(
        folder, _safe_audio_name(name, folder=folder, allowed_exts=allowed_exts)
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            yield dest, fh
    except HTTPException:
        _discard_partial(dest)
        raise
    except OSError as e:
        _discard_partial(dest)
        raise _save_oserror(e, dest) from e
    except Exception as e:
        _discard_partial(dest)
        logger.exception("写入保存目录失败：%s", dest)
        raise HTTPException(500, f"保存失败：{e.__class__.__name__}") from e


@api.get("/prefs/save-dir")
def prefs_save_dir_get(request: Request = None):
    info = _save_dir_info(_save_dir_get())
    # 直接调用（测试）时没有 request：按本机处理，与真实本机请求同口径
    if info.get("dir") and request is not None and not _is_local_client(request):
        # 局域网客户端只需要知道「设过、能不能写」，不必看到这台电脑的绝对路径
        info.pop("reason", None)   # reason 里可能夹着完整路径
        info["dir"] = "（运行服务的这台电脑上的目录）"
        info["masked"] = True
    return JSONResponse(info)


@api.put("/prefs/save-dir")
def prefs_save_dir_put(request: Request, req: dict = Body(...)):
    if not _is_local_client(request):
        raise HTTPException(403, "只有运行服务的这台电脑可以修改保存目录")
    if "dir" not in req or not isinstance(req["dir"], str):
        raise HTTPException(400, "dir 必须是字符串（传空字符串表示清除）")
    raw = req["dir"].strip().strip('"')
    if not raw:
        _ui_state_write(_SAVE_DIR_KEY, None)
        logger.info("保存目录已清除")
        return JSONResponse({"ok": True, **_save_dir_info("")})
    # 相对路径按服务进程的工作目录解析——用户手输时很可能不是这个意思，
    # 所以把解析后的绝对路径与 relative 标记一起回给前端显示（BUG-005）。
    expanded = os.path.expanduser(raw)
    relative = not os.path.isabs(expanded)
    path = os.path.abspath(expanded)
    info = _save_dir_info(path)
    if not info["exists"]:
        # reason 已经区分"这是一个文件，不是目录"和"目录不存在"
        logger.warning("设置保存目录被拒（%s）：%s", info.get("reason", "目录不可用"), path)
        raise HTTPException(400, f"{info.get('reason') or '目录不存在'}：{path}")
    if not info["writable"]:
        logger.warning("设置保存目录被拒（不可写：%s）：%s", info.get("reason", ""), path)
        raise HTTPException(400, f"目录不可写（{info.get('reason') or '没有写入权限'}）：{path}")
    _ui_state_write(_SAVE_DIR_KEY, path)
    logger.info("保存目录已设置：%s%s", path, "（由相对路径解析）" if relative else "")
    return JSONResponse({"ok": True, "relative": relative, **info})


@api.post("/prefs/save-dir/pick")
def prefs_save_dir_pick(request: Request):
    """弹系统文件夹选择框。只能在本机弹——框出现在服务端这台电脑的桌面上。"""
    if not _is_local_client(request):
        raise HTTPException(403, "只有运行服务的这台电脑可以弹出文件夹选择框")
    if not _PICK_LOCK.acquire(blocking=False):
        raise HTTPException(409, "文件夹选择框已经打开，请先完成或取消它")
    try:
        try:
            import tkinter as tk
            from tkinter import filedialog
        except Exception as e:
            raise HTTPException(501, f"当前环境无法弹出文件夹选择框（{e}），请手动输入路径")
        root = None
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(title="选择音频保存目录", mustexist=True)
        except Exception as e:
            raise HTTPException(501, f"文件夹选择框打开失败（{e}），请手动输入路径")
        finally:
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass
    finally:
        _PICK_LOCK.release()
    if not path:
        return JSONResponse({"cancelled": True})
    return JSONResponse({"cancelled": False, "dir": os.path.abspath(path)})


@api.post("/prefs/save-dir/open")
def prefs_save_dir_open(request: Request):
    if not _is_local_client(request):
        raise HTTPException(403, "只有运行服务的这台电脑可以打开文件夹")
    folder = _save_dir_get()
    if not folder:
        raise HTTPException(409, "还没有设置保存目录")
    info = _save_dir_info(folder)
    if not info["exists"]:
        raise HTTPException(409, f"已设置的保存目录不可用（{info.get('reason') or '目录不存在'}），请重新选择")
    if not hasattr(os, "startfile"):
        raise HTTPException(501, "当前系统不支持打开文件夹")
    try:
        os.startfile(folder)
    except OSError as e:
        logger.warning("打开保存目录失败：%s -> %s", folder, e)
        raise HTTPException(500, f"打开文件夹失败：{e.strerror or e.__class__.__name__}") from e
    return {"ok": True, "dir": folder}


def _save_target_dir() -> str:
    """取当前可用的保存目录。失效原因分三种报，不再共用一句"尚未设置"（BUG-005）。"""
    folder = _save_dir_get()
    if not folder:
        raise HTTPException(409, "还没有设置保存目录，请先在偏好里选择目录")
    # 真要落盘的场合不吃 15 秒缓存：目录刚被删掉/改名的瞬间不能还说它「可用」
    info = _save_dir_info(folder, use_cache=False)
    if not info["exists"]:
        raise HTTPException(409, f"已设置的保存目录不可用（{info.get('reason') or '目录不存在'}），请重新选择")
    if not info["writable"]:
        raise HTTPException(409, f"保存目录不可写（{info.get('reason') or '没有写入权限'}），请换一个目录")
    return folder


@api.post("/save-dir/outputs/{name}")
def save_dir_copy_output(name: str):
    """把 outputs/ 里的生成音频复制到保存目录（字节与「下载」完全一致）。"""
    folder = _save_target_dir()
    src = os.path.join(OUTPUTS_DIR, os.path.basename(name))
    if not os.path.isfile(src):
        raise HTTPException(404, "音频文件不存在或已被清理")
    with _save_writer(folder, name) as (dest, out):
        try:
            with open(src, "rb") as inp:
                shutil.copyfileobj(inp, out)
        except FileNotFoundError as e:
            logger.warning("源音频在复制期间消失：%s -> %s", src, e)
            raise HTTPException(404, "音频文件不存在或已被清理") from e
        except OSError as e:
            raise _save_oserror(e, dest) from e
        # 保留源文件的修改时间与属性，行为与旧的 shutil.copy2 一致
        try:
            shutil.copystat(src, dest)
        except OSError as e:
            logger.warning("复制音频属性失败（音频本身已存好）：%s -> %s", dest, e)
        written = out.tell()
    logger.info("已复制到保存目录：%s（源 %s，%d 字节）", dest, src, written)
    return {"ok": True, "path": dest, "dir": folder}


@api.post("/save-dir/upload")
def save_dir_upload(name: str = Form(""), file: UploadFile = File(...)):
    """接收浏览器算出的剪辑结果并落盘（剪辑 WAV 只存在于前端）。

    这是唯一由客户端直接投喂内容到保存目录的入口，必须和 /tts 的上传口
    同样严格：魔数嗅探 + 32MB 上限 + 边读边写（内存恒定 ≤1MB）。文件名有
    _safe_audio_name 保护，内容以前是裸奔的——任意内容任意体积都能写进去。
    """
    folder = _save_target_dir()
    try:
        head = file.file.read(64)
        # 只放行 WAV / MP3（前端能算出来的两种），并区分具体格式：
        # 只看 bool 会把"wav 改名成 .mp3"当合法上传存下去，后缀与内容对不上。
        content_ext = _audio_ext_from_magic(head)
        if content_ext not in _SAVE_AUDIO_EXTS:
            raise HTTPException(400, "上传内容不是 WAV / MP3 音频（这个入口只接收浏览器生成的音频）")
        raw_name = (name or file.filename or "audio").strip()
        base = os.path.basename(raw_name.replace("\\", "/"))
        given_ext = os.path.splitext(base)[1].lower()
        if given_ext in _SAVE_AUDIO_EXTS and given_ext != content_ext:
            raise HTTPException(
                400, f"文件扩展名与内容不符（实际内容是 {content_ext}，文件名却是 {given_ext}）"
            )
        if given_ext not in _SAVE_AUDIO_EXTS:
            base = base + content_ext        # 没有扩展名：按嗅探结果补全
        # 不是死代码：文件全长 ≤64 字节时全落在 head 里，下面的分块循环拿到空
        # chunk 直接 break，超限只能在这里拦（上限被调小的测试路径依赖此检查）。
        if len(head) > AUDIO_MAX_BYTES:
            raise HTTPException(413, f"音频过大（上限 {AUDIO_MAX_BYTES // 1048576} MB）")
        with _save_writer(folder, base, allowed_exts=_SAVE_AUDIO_EXTS) as (dest, out):
            try:
                out.write(head)
                written = len(head)
                while True:
                    chunk = file.file.read(1 << 20)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > AUDIO_MAX_BYTES:
                        raise HTTPException(
                            413, f"音频过大（上限 {AUDIO_MAX_BYTES // 1048576} MB）"
                        )
                    out.write(chunk)
            except OSError as e:
                raise _save_oserror(e, dest) from e
    finally:
        try:
            file.file.close()
        except Exception:
            pass
    logger.info("已保存上传音频：%s（%d 字节）", dest, written)
    return {"ok": True, "path": dest, "dir": folder}



def _load_examples(include_experimental=False):
    """Load examples/cases.jsonl into a list of dicts (paths exposed as URLs)."""
    cases_path = os.path.join(current_dir, "examples", "cases.jsonl")
    if not os.path.exists(cases_path):
        return []
    examples = []
    with open(cases_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not include_experimental and item.get("emo_mode") == 3:
                continue
            item["prompt_audio"] = f"/examples/{os.path.basename(item.get('prompt_audio', ''))}"
            if item.get("emo_audio"):
                item["emo_audio"] = f"/examples/{os.path.basename(item['emo_audio'])}"
            examples.append(item)
    return examples


@api.get("/examples")
def examples_list(include_experimental: bool = False):
    return JSONResponse({"examples": _load_examples(include_experimental=include_experimental)})


_EXAMPLE_AUDIO_EXT = SUPPORTED_AUDIO_EXTS


@api.get("/examples/{name}")
def examples_file(name: str):
    # 只放行音频。examples/ 里除了示例音频还有 cases.jsonl（示例清单）和
    # batch/ 子目录；原先只取 basename 就返回，于是 /examples/cases.jsonl
    # 也能下载，而且 media_type 被写死成 audio/wav——一个 jsonl 当 wav 下发。
    # 用 404 而不是 403，不额外透露"这个文件存在但你不能拿"。
    base = os.path.basename(name)
    if not base.lower().endswith(_EXAMPLE_AUDIO_EXT):
        raise HTTPException(404, "Not found")
    path = os.path.join(current_dir, "examples", base)
    if not os.path.isfile(path):
        raise HTTPException(404, "Not found")
    return FileResponse(path)


@api.post("/segments")
def segments_preview(
    request: Request,
    text: str = Form(""),
    max_text_tokens_per_segment: int = Form(120),
    scenario_id: str | None = Form(None),
):
    # 这个接口只吃表单。以前 JSON body 也回 200：text 取到默认值 ""，于是返回
    # {"segments": []}——看起来像"这段文本被切出了 0 段"，而且顺手绕过了下面的
    # 413 超长护栏（BUG-011）。非表单的 Content-Type 现在直接 415 说清楚。
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    _FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")
    if ctype and ctype not in _FORM_TYPES:
        raise HTTPException(
            415,
            "/segments 只接受表单参数（Content-Type: "
            + " 或 ".join(_FORM_TYPES)
            + f"），当前是 {ctype}",
        )
    text = (text or "").strip()
    if not text:
        if ctype:
            return JSONResponse({"segments": []})
        raise HTTPException(422, "缺少 text 表单字段：分段预览需要待切分的文本")
    if len(text) > TEXT_CHARS_HARD:
        raise HTTPException(
            413,
            f"文本过长（{len(text)} 字，上限 {TEXT_CHARS_HARD} 字）。"
            "这个上限只是防止误粘贴卡死服务；正常长文请分几次生成。",
        )
    max_text_tokens_per_segment = _clamp_seg_tokens(max_text_tokens_per_segment)
    # 整个请求只取一次快照：分段、规范化文本、数字读音共用同一份词典。
    snapshot = _glossary_snapshot(scenario_id)
    tts._ensure_light()
    tokens = tts.tokenizer.tokenize(
        text,
        glossary=snapshot.glossary if snapshot else None,
        cascade=snapshot.cascade if snapshot else None,
    )
    segs = tts.tokenizer.split_segments(tokens, max_text_tokens_per_segment=max_text_tokens_per_segment)

    # 词元拼回可读文本的实现在 indextts/utils/subtitle.py，与字幕共用一份。
    # 这里以前是 `"".join(seg).replace("▁", "")`：英文词元带词首标记（▁I ▁LOVE），
    # 直接删掉▁会把单词粘成 ILOVE，预览看不出到底念了什么。
    #
    # 额外返回 normalized_text 与 numbers：模型读的是规范化之后的文本，
    # 而数字是最高频的出错点（"5090显卡" 会被读成"五千零九十显卡"）。
    # 调用方据此在合成前就能核对读法，读错了用术语表纠正后再生成。
    normalizer = tts.normalizer
    normalized_text = normalizer.normalize(
        text,
        glossary=snapshot.glossary if snapshot else None,
        cascade=snapshot.cascade if snapshot else None,
    )
    # 必须先取：_number_readings 里还会调 normalize（每次调用都会复位
    # normalize_fallback），放在后面读就变成"最后一个数字片段"的状态了。
    normalize_fallback = normalizer.normalize_fallback
    numbers, truncated = _number_readings(
        normalizer, text,
        glossary=snapshot.glossary if snapshot else None,
        cascade=snapshot.cascade if snapshot else None,
    )
    return JSONResponse({
        "segments": [
            {"index": i, "text": tokens_to_readable(s)[0], "tokens": len(s)}
            for i, s in enumerate(segs)
        ],
        "normalized_text": normalized_text,
        "numbers": numbers,
        "numbers_truncated": truncated,
        "normalize_fallback": normalize_fallback,
        "scenario_id": snapshot.scenario_id if snapshot else DEFAULT_SCENARIO,
        "scenario_name": snapshot.scenario_name if snapshot else DEFAULT_SCENARIO_NAME,
    })


# 数字读音提示用的匹配范围：连续数字（含小数/千分位/时间冒号）、百分号，
# 再加一个可能改变读法的中文单位（"2026年" 读"二零二六年"，而裸 "2026"
# 会读成"两千零二十六"）。只做提示，不参与实际合成。
_NUMBER_HINT_RE = re.compile(r"\d+(?:[.,:]\d+)*(?:[%％])?[年月日号点岁元块万亿千百十]?")
_NUMBER_HINT_MAX = 50


def _number_readings(normalizer, text: str, glossary=None, cascade=None):
    """返回 (数字读法列表, 是否被截断)。

    每项是 {"text": 原文片段, "read": 规范化后的读法}。读法是单独规范化
    这个片段得到的，语境可能略有差别（比如 "8:00 AM" 单独读"八点"、
    整句读"上午八点"），完整语境以 segments 里的文本为准——这里的作用
    是让调用方一眼看到"模型到底会怎么念这个数字"，方便判断要不要加术语表。
    传入的 glossary/cascade 必须与本请求分词用的快照一致。
    """
    items, seen = [], set()
    truncated = False
    for m in _NUMBER_HINT_RE.finditer(text):
        raw = m.group(0)
        if raw in seen:
            continue
        seen.add(raw)
        try:
            read = (normalizer.normalize(raw, glossary=glossary, cascade=cascade) or "").strip()
        except Exception:
            read = ""
        if read and read != raw:
            if len(items) >= _NUMBER_HINT_MAX:
                truncated = True
                break
            items.append({"text": raw, "read": read})
    return items, truncated


# ---- 字幕（段级 / 句级） -----------------------------------------------------
# 时间是【物理】的：成品音频 = 各段波形 + 段间静音拼接，引擎在拼接前把每段的
# 样本数记进 model.segment_slices（见 infer_v2._record_segment_slices），这里
# 只负责存盘与出文件。算法与格式化都在 indextts/utils/subtitle.py（纯标准库）。
#
# 段内本来没有时间点。装上 ASR 运行时后，会拿成品 wav 再跑一遍 Paraformer，
# 从波形里读出 token 时间戳，把段切成句级（utils/asr_align.py）。ASR 不可用、
# 超时、对齐失败时逐段降级回段级：字幕是附属产物，绝不能让生成报错。
#
# 必须在生成结束的那一刻存：过了这刻只剩成品 wav 的总时长，每段边界再也算不出来。
# 所以每次生成都顺手落一份 sidecar（几 KB），用户点不点导出都一样。

def _subtitle_align(wav_path, segments):
    """尽量把段级快照切成句级；任何异常都返回 (None, 原因)，不影响生成。"""
    try:
        import importlib
        aligner = importlib.import_module("indextts.utils.asr_align")
        return aligner.align_sidecar(wav_path, segments)
    except Exception as exc:
        logger.warning("字幕句级对齐跳过，改用段级：%s", exc)
        return None, "error"


def _subtitle_save(wav_name: str, text_source: str, segments, interval_ms, chars: int,
                   wav_path=None) -> bool:
    """把本次生成的字幕快照写进 data/subtitles/<同名>.json；失败只返回 False。"""
    if not segments:
        # 引擎没记上（段数对不上 / 内部异常）：没有字幕可用，前端据此说明原因。
        return False
    sentences, align_method = _subtitle_align(wav_path, segments)
    data = {
        "version": SIDECAR_VERSION,
        "file": os.path.basename(wav_name),
        # 只有真的拿到句级结果才标 sentence；ASR 没装 / 全部降级时与旧版
        # 逐字节一致，旧缓存、旧前端、旧单测都不受影响。
        "granularity": "sentence" if sentences else "segment",
        "align_method": align_method,
        "text_source": text_source or "",
        "interval_ms": int(interval_ms or 0),
        "chars": int(chars or 0),
        "created_at": time.time(),
        "segments": segments,
    }
    if sentences:
        # version 仍是 1：segments 原样保留，旧代码读到也只当段级用。
        data["sentences"] = sentences
    if not save_sidecar(sidecar_path(SUBTITLE_DIR, wav_name), data):
        logger.warning("字幕缓存写入失败：%s（音频已生成成功，不影响播放）", wav_name)
        return False
    return True


def _subtitle_load_or_404(name: str) -> dict:
    """读 sidecar；缺文件或结构不对都回 404，并把「为什么没有」说清楚。"""
    base = os.path.basename(name or "")
    if not base:
        raise HTTPException(400, "缺少音频文件名")
    data = load_sidecar(sidecar_path(SUBTITLE_DIR, base))
    if data is None:
        raise HTTPException(
            404,
            f"{base} 没有字幕记录：这份音频生成时还没记录段边界"
            "（字幕是后加的功能，旧音频没有）。重新生成一次即可导出字幕。",
        )
    return data


@api.get("/subtitle", include_in_schema=False)
def subtitle_get(file: str = ""):
    """字幕条目（前端字幕面板用）。句级优先、旧缓存自动回退段级；不占推理锁。"""
    data = _subtitle_load_or_404(file)
    return JSONResponse({
        "file": data["file"],
        "granularity": data.get("granularity") or "segment",
        "align_method": data.get("align_method") or "",
        "text_source": data.get("text_source") or "",
        "created_at": data.get("created_at"),
        "segments": data["segments"],
        "cues": cues_from_sidecar(data),
    })


@api.get("/subtitle/export", include_in_schema=False)
def subtitle_export(file: str = "", fmt: str = "srt"):
    """导出 SRT / WebVTT 文本。浏览器直接下载，所以文件名走 filename*。

    SRT 用 UTF-8 带 BOM：中文 Windows 上 PotPlayer / Premiere 之类别的文件不写
    BOM 就当 ANSI 读，字幕直接变乱码。VTT 按规范纯 UTF-8。
    """
    fmt = (fmt or "srt").lower()
    if fmt not in ("srt", "vtt"):
        raise HTTPException(400, f"字幕格式只支持 srt / vtt，收到 {fmt}")
    data = _subtitle_load_or_404(file)
    cues = cues_from_sidecar(data)
    if not cues:
        raise HTTPException(404, "这份音频的字幕条目是空的（文本被全部过滤）")
    if fmt == "srt":
        body = format_srt(cues).encode("utf-8-sig")
        media = "application/x-subrip"
    else:
        body = format_vtt(cues).encode("utf-8")
        media = "text/vtt"
    stem = os.path.splitext(os.path.basename(data["file"]))[0] or "subtitle"
    filename = quote(f"{stem}.{fmt}")
    return Response(
        content=body,
        media_type=f"{media}; charset=utf-8",
        headers={
            "Content-Disposition":
                f"attachment; filename=\"subtitle.{fmt}\"; filename*=UTF-8''{filename}",
            "Cache-Control": "no-store",
        },
    )


# ---- 术语规则库 -------------------------------------------------------------
# 规则/场景的唯一事实源在 indextts.utils.glossary_store；这里只做 HTTP 映射。
# 管理接口是应用内前端专用，include_in_schema=False，skill 的接口对账不受影响。

_GLOSSARY_SAVE_LOCK = threading.Lock()


def _glossary_payload():
    return _glossary_store().document()


def _glossary_json(document, status_code: int = 200):
    return JSONResponse(
        document, status_code=status_code, headers={"Cache-Control": "no-store"}
    )


def _sync_normalizer():
    """保存后同步已加载 normalizer 的兜底词典（默认场景 + 当前 cascade）。

    显式传快照的请求不受影响；还没改造的旧库调用方读实例字段，
    这里保证它们看到的内容与文件一致。
    """
    if tts.normalizer is None:
        return
    try:
        snapshot = _glossary_snapshot(None)
    except Exception as e:
        logger.warning("Failed to sync normalizer glossary: %s", e)
        return
    if snapshot is not None:
        tts.normalizer.term_glossary = snapshot.glossary
        tts.normalizer.cascade_glossary = snapshot.cascade


def _glossary_http_error(exc: Exception, action: str, term: str = ""):
    """把 store 的异常翻译成 HTTP；日志里保留完整堆栈。"""
    if isinstance(exc, GlossaryValidationError):
        detail = exc.errors[0]["message"] if exc.errors else str(exc)
        return HTTPException(400, detail)
    if isinstance(exc, GlossaryReadOnly):
        return HTTPException(409, str(exc))
    logger.exception("Failed to %s glossary%s", action, f" ({term})" if term else "")
    return HTTPException(500, f"{action}失败：{exc}")


@api.get("/glossary")
def glossary_list():
    return _glossary_json(_glossary_payload())


@api.post("/glossary")
def glossary_add(
    term: str = Form(...),
    reading_zh: str = Form(""),
    reading_en: str = Form(""),
    cascade: bool | None = Form(None),
):
    """旧版单条 upsert：空列沿用原值（BUG-004），新规则加入服务端当前场景。"""
    term = _fix_mojibake(term).strip()
    reading_zh = _fix_mojibake(reading_zh).strip()
    reading_en = _fix_mojibake(reading_en).strip()
    store = _glossary_store()
    try:
        with _GLOSSARY_SAVE_LOCK:
            document = store.legacy_add(term, reading_zh, reading_en)
            if cascade is not None:
                document = store.set_cascade(cascade)
    except Exception as e:
        raise _glossary_http_error(e, "保存术语", term) from e
    _sync_normalizer()
    logger.info("术语已更新：%s（中文=%r 英文=%r）", term, reading_zh, reading_en)
    return _glossary_json(document)


@api.post("/glossary/cascade")
def glossary_set_cascade(cascade: bool = Form(...)):
    """只改级联开关，不动规则文件。"""
    store = _glossary_store()
    try:
        with _GLOSSARY_SAVE_LOCK:
            document = store.set_cascade(cascade)
    except Exception as e:
        raise _glossary_http_error(e, "保存级联设置") from e
    _sync_normalizer()
    logger.info("Glossary cascade replacement: %s", bool(cascade))
    return _glossary_json(document)


@api.delete("/glossary")
def glossary_clear():
    """清空规则，保留场景结构（场景变成空场景）。"""
    store = _glossary_store()
    try:
        with _GLOSSARY_SAVE_LOCK:
            document = store.legacy_clear()
    except Exception as e:
        raise _glossary_http_error(e, "清空术语") from e
    _sync_normalizer()
    logger.info("Glossary cleared (scenarios kept)")
    return _glossary_json(document)


@api.delete("/glossary/{term}")
def glossary_remove(term: str):
    """删除单条术语，并从所有场景移除引用。

    路径参数里的术语可能带空格/中文/符号（"PCIe 5.0"、"C++"），前端必须
    encodeURIComponent 后再拼 URL；含 "/" 的术语（TCP/IP）受路由限制，
    改在弹窗里删除。
    """
    term = _fix_mojibake(term).strip()
    if not term:
        raise HTTPException(400, "术语不能为空")
    store = _glossary_store()
    try:
        with _GLOSSARY_SAVE_LOCK:
            document = store.legacy_delete(term)
    except KeyError as e:
        raise HTTPException(404, "术语不存在") from e
    except Exception as e:
        raise _glossary_http_error(e, "删除术语", term) from e
    _sync_normalizer()
    logger.info("术语已删除：%s", term)
    return _glossary_json(document)


@api.put("/glossary/config", include_in_schema=False)
def glossary_config(payload: dict = Body(...)):
    """一次保存规则 + 场景（带 rev）。校验全部通过才落盘。"""
    rules = payload.get("rules")
    scenarios = payload.get("scenarios")
    if not isinstance(rules, list) or not isinstance(scenarios, list):
        raise HTTPException(422, "请求体必须包含 rules 和 scenarios 两个列表")
    force = bool(payload.get("force"))
    store = _glossary_store()
    try:
        with _GLOSSARY_SAVE_LOCK:
            document = store.save_config(
                rules, scenarios, rev=payload.get("rev"), force=force
            )
    except GlossaryConflict as e:
        logger.info("Glossary rev conflict; client must choose overwrite or refresh")
        return _glossary_json(e.document, status_code=409)
    except GlossaryValidationError as e:
        raise HTTPException(422, detail={"message": str(e), "errors": e.errors}) from e
    except GlossaryReadOnly as e:
        raise HTTPException(409, str(e)) from e
    except Exception as e:
        logger.exception("Failed to save glossary config")
        raise HTTPException(500, f"保存术语规则失败：{e}") from e
    _sync_normalizer()
    return _glossary_json(document)


@api.post("/glossary/active", include_in_schema=False)
def glossary_set_active(scenario_id: str = Form("")):
    """切换服务端记住的当前场景。空串=默认场景。"""
    store = _glossary_store()
    try:
        with _GLOSSARY_SAVE_LOCK:
            document = store.set_active(scenario_id)
    except UnknownScenario as e:
        raise HTTPException(400, f"场景不存在或已删除：{scenario_id}") from e
    except Exception as e:
        raise _glossary_http_error(e, "切换场景") from e
    return _glossary_json(document)


@api.post("/glossary/test", include_in_schema=False)
def glossary_test(payload: dict = Body(...)):
    """用弹窗里的草稿试读，不改任何状态。"""
    text = payload.get("text")
    text = text if isinstance(text, str) else ""
    if len(text) > 2000:
        raise HTTPException(413, "试读文本上限 2000 字")
    rules = payload.get("rules")
    scenarios = payload.get("scenarios")
    if not isinstance(rules, list) or not isinstance(scenarios, list):
        raise HTTPException(422, "试读需要完整的 rules 和 scenarios")
    store = _glossary_store()
    try:
        result = store.test(
            text, rules, scenarios,
            payload.get("scenario_id"), bool(payload.get("cascade")),
        )
    except UnknownScenario as e:
        raise HTTPException(400, f"场景不存在或已删除：{e.scenario_id}") from e
    except GlossaryValidationError as e:
        raise HTTPException(422, detail={"message": str(e), "errors": e.errors}) from e
    except Exception as e:
        logger.exception("Glossary test failed")
        raise HTTPException(500, f"试读失败：{e}") from e
    return JSONResponse(result)


@api.post("/glossary/import", include_in_schema=False)
async def glossary_import(file: UploadFile = File(...)):
    """导入 YAML/JSON 并与现有规则合并。全量校验，失败不改任何状态。"""
    import yaml
    raw = await file.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(413, "导入文件上限 2 MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise HTTPException(400, "导入文件不是 UTF-8 编码") from e
    name = (file.filename or "").lower()
    try:
        if name.endswith(".json") or text.lstrip().startswith("{"):
            data = json.loads(text)
        else:
            data = yaml.load(text, Loader=yaml.BaseLoader)
    except Exception as e:
        raise HTTPException(400, f"导入文件解析失败：{e}") from e
    store = _glossary_store()
    try:
        with _GLOSSARY_SAVE_LOCK:
            summary = store.import_data(data)
    except GlossaryValidationError as e:
        raise HTTPException(422, detail={"message": str(e), "errors": e.errors}) from e
    except GlossaryReadOnly as e:
        raise HTTPException(409, str(e)) from e
    except Exception as e:
        logger.exception("Glossary import failed")
        raise HTTPException(500, f"导入失败：{e}") from e
    _sync_normalizer()
    return JSONResponse(summary)


@api.get("/glossary/export", include_in_schema=False)
def glossary_export(format: str = "yaml"):
    """导出完整 v2 文档（含停用规则与场景）；文件名固定，不拼用户输入。"""
    fmt = (format or "yaml").lower()
    if fmt not in ("yaml", "json"):
        raise HTTPException(400, "format 只支持 yaml 或 json")
    store = _glossary_store()
    try:
        body = store.export_text(fmt)
    except Exception as e:
        logger.exception("Glossary export failed")
        raise HTTPException(500, f"导出失败：{e}") from e
    media = "application/json" if fmt == "json" else "application/x-yaml"
    return Response(
        content=body,
        media_type=f"{media}; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="glossary.{fmt}"',
            "Cache-Control": "no-store",
        },
    )


# ---- 使用指南：web/guide/guide.md 在服务端渲染成 HTML -------------------------
# 解析放服务端：前端零依赖 / 零构建；图片走 /assets/guide/ 静态路由。
_GUIDE_MD = os.path.join(_WEB_DIR, "guide", "guide.md")
_GUIDE_CACHE: dict = {}

try:
    import markdown as _markdown
except Exception:  # 只在漏装 server extra 的环境里触发，不能因此让服务起不来
    _markdown = None


def _guide_slugify(value, separator):
    # 默认 slugify 会把中文整段抹掉（只剩 "0"、"1"），目录跳转就失去意义。
    value = re.sub(r"[^\w]+", separator, value.strip().lower(), flags=re.UNICODE)
    return value.strip(separator) or "section"


# 只给应用内指南页用，不属于对外 TTS 接口：不进 openapi，
# 免得 agent skill 的接口对账把它算成「服务端有、skill 没适配」。
@api.get("/guide", include_in_schema=False)
def guide_content():
    if _markdown is None:
        raise HTTPException(503, "服务端缺少 markdown 组件，请执行 uv sync --extra server")
    try:
        st = os.stat(_GUIDE_MD)
    except OSError:
        raise HTTPException(404, "使用指南未安装（web/guide/guide.md 不存在）")
    key = (st.st_mtime_ns, st.st_size)
    cached = _GUIDE_CACHE.get("payload")
    if cached and cached[0] == key:
        return cached[1]
    with open(_GUIDE_MD, "r", encoding="utf-8") as f:
        text = f.read()
    md = _markdown.Markdown(
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
        extension_configs={"toc": {"toc_depth": "2-3", "slugify": _guide_slugify}},
    )
    html = md.convert(text)
    # 图片相对路径 → 静态路由；带内容指纹，指南更新后不会命中旧图缓存
    ver = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    html = html.replace('src="images/', 'src="/assets/guide/images/')
    html = re.sub(r'src="(/assets/guide/images/[^"]+)"',
                  lambda m: f'src="{m.group(1)}?v={ver}"', html)
    html = re.sub(r'<a href="(https?://[^"]+)"',
                  r'<a href="\1" target="_blank" rel="noopener noreferrer"', html)
    payload = {"html": html, "toc": md.toc_tokens, "version": ver}
    _GUIDE_CACHE["payload"] = (key, payload)
    return payload


# ---- API 挂载 -----------------------------------------------------------------
# 版本化：JSON 接口正式路径为 /api/v1/*，前端 fetch 前缀在 app.js 顶部集中常量化。
# 旧根路径地址以 307 重定向保活，兼容已存在的书签 / 第三方脚本。
app.include_router(api)

_legacy_json_routes = (
    ("GET", "health"), ("GET", "metrics"), ("GET", "model"), ("GET", "status"),
    ("POST", "model/config"), ("POST", "model/load"), ("POST", "model/unload"),
    ("POST", "model/restart"), ("POST", "tts"), ("POST", "tts/{job_id}/cancel"),
    ("POST", "tts/{job_id}/stop"), ("GET", "jobs/current"), ("GET", "jobs/{job_id}"),
    ("GET", "audio/{name}"), ("GET", "history"), ("POST", "history/config"),
    ("DELETE", "history"), ("DELETE", "history/{item_id}"),
    ("GET", "presets"), ("POST", "presets"), ("GET", "presets/{name}"),
    ("GET", "presets/{name}/audio/{kind}"), ("DELETE", "presets/{name}"),
    ("POST", "presets/{name}/rename"), ("POST", "presets/{name}/duplicate"),
    ("GET", "examples"), ("GET", "examples/{name}"), ("POST", "segments"),
    ("GET", "glossary"), ("POST", "glossary"), ("DELETE", "glossary"),
    ("DELETE", "glossary/{term}"), ("POST", "glossary/cascade"),
)


def _register_legacy_redirects():
    """307-redirect legacy root-level JSON paths to their /api/v1 equivalent.

    Route templates like `presets/{name}` must be filled in from the actual
    request path params, or the redirect target would carry literal `{name}`.
    """
    for method, path in _legacy_json_routes:
        def endpoint(request: Request, p=path):
            # substitute {param} with the values FastAPI matched on this request
            target = re.sub(r"\{(\w+)\}", lambda m: quote(str(request.path_params.get(m.group(1), m.group(0)))), p)
            return RedirectResponse(url=f"{API_V1}/{target}", status_code=307)
        app.add_api_route(
            f"/{path}", endpoint, methods=[method],
            include_in_schema=False, name=f"legacy_{method.lower()}_{path}",
        )


_register_legacy_redirects()


if __name__ == "__main__":
    import uvicorn
    # 孤儿清理已在 import 期执行（见 _clean_stale_outputs 之后的调用）
    logger.info("IndexTTS2 server starting... port %d", cmd_args.port)
    # 后台加载 numpy + audiotsm：变速引擎的 import 成本提前付掉，顺便让「包内
    # 缺少 audiotsm」在启动日志里就报出来，而不是等用户拖滑块时卡在「变速计算中…」。
    # 失败不影响服务。
    threading.Thread(target=_warmup_tempo, daemon=True, name="tempo-warmup").start()
    uvicorn.run(app, host=cmd_args.host, port=cmd_args.port, log_level="warning")
