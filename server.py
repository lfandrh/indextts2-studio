"""IndexTTS2 lightweight FastAPI server (no gradio).

Startup only loads light components (config, text normalizer, tokenizer);
the main TTS model is loaded lazily on first /tts request, so the server
boots fast and idle VRAM stays at zero.
"""
import argparse
import asyncio
import gc
import hashlib
import io
import json
import os
import queue
import re
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
    cap_prompt_files as _cap_prompt_files,
    ensure_wetext_ascii_path,
    AUDIO_MAX_BYTES,
)
# 中文路径分发包兼容：kaldifst 走 ANSI C API 打开 .fst，wetext 装在非 ASCII
# 路径下 import 即炸。必须在任何 wetext 触达之前装好重定向（ASCII 路径下为空操作）。
ensure_wetext_ascii_path()
# 中文 Windows 控制台默认 GBK 代码页：推理链路里的 print（如 token 列表、模型路径）
# 含 GBK 无法编码的字符时直接 UnicodeEncodeError 炸掉整次生成。统一改 UTF-8 + 替换。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

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
        self.glossary_path = os.path.join(model_dir, "glossary.yaml")
        # 级联替换开关单独存。塞进 glossary.yaml 会被当成「术语 -> 读音」，
        # _clean_glossary 只查 key 不查 value，一个 bool 会活下来变成术语。
        self.glossary_settings_path = os.path.join(model_dir, "glossary_settings.yaml")
        self.normalizer = None
        self.tokenizer = None
        self.model_version = None
        self._phase = "unloaded"
        self._phase_lock = threading.Lock()

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
            if os.path.exists(self.glossary_path):
                try:
                    normalizer.load_glossary_from_yaml(self.glossary_path)
                except Exception as e:
                    logger.warning("Failed to load glossary: %s", e)
            try:
                normalizer.load_glossary_settings(self.glossary_settings_path)
            except Exception as e:
                logger.warning("Failed to load glossary settings: %s", e)
            # 全部就绪后才发布，中途不暴露半成品
            self.cfg = cfg
            self.normalizer = normalizer
            self.tokenizer = tokenizer
            self.model_version = getattr(cfg, "version", None)
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
                        )
                        t.normalizer = self.normalizer
                        t.tokenizer = self.tokenizer
                        t.diffusion_steps = self.init_kwargs.get("diffusion_steps", 25)
                        t.inference_cfg_rate = self.init_kwargs.get("inference_cfg_rate", 0.7)
                        self.cfg = t.cfg
                        self._tts = t
                        self._set_phase("ready")
                        logger.info("IndexTTS2 main model loaded.")
                    except Exception:
                        self._set_phase("error")
                        raise
        return self._tts

    def infer(self, *args, **kwargs):
        return self.ensure_loaded().infer(*args, **kwargs)

    def normalize_emo_vec(self, *args, **kwargs):
        return self.ensure_loaded().normalize_emo_vec(*args, **kwargs)

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
        }

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
                self.init_kwargs[real] = val
            # cuDNN benchmark is a process-global torch flag: apply immediately,
            # no model reload needed.
            self._apply_backend_flags()
            if self._tts is not None:
                self._tts.diffusion_steps = int(self.init_kwargs.get("diffusion_steps", 25))
                self._tts.inference_cfg_rate = float(self.init_kwargs.get("inference_cfg_rate", 0.7))

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
)

# 全部数据目录锚定到 server.py 所在的项目根，与 presets（_project_root）一致；
# 不用 CWD 相对路径，避免从别的目录启动 `python server.py` 时文件落错位置
OUTPUTS_DIR = os.path.join(current_dir, "outputs")
PROMPTS_DIR = os.path.join(current_dir, "prompts")
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


def _validate_audio_upload(data: bytes, label: str) -> None:
    """上传校验：大小上限 + 常见音频容器的魔数嗅探，在落盘前给出可读的 4xx。

    完整解码校验仍由推理侧 librosa 承担（失败经 SSE 报错）；这里只拦截
    明显不是音频的文件（改名的文本/图片等），不做重解码。
    """
    if not data:
        raise HTTPException(400, f"{label}是空文件")
    if len(data) > AUDIO_MAX_BYTES:
        raise HTTPException(
            413,
            f"{label}过大（{len(data) / 1048576:.0f} MB，上限 {AUDIO_MAX_BYTES // 1048576} MB）",
        )
    if not _looks_like_audio(data):
        raise HTTPException(400, f"{label}不是可识别的音频文件（支持 wav/mp3/flac/ogg/m4a）")


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


@api.get("/health")
def health():
    return {"status": "ok", "model_loaded": tts.loaded}


# ---- system metrics (v2 UI header gauges) ----
import ctypes
import shutil
import subprocess as _sp


def _nvidia_smi(query: str):
    """兜底通道：fork nvidia-smi 解析 stdout。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = _sp.run(
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
_NVML_LIBS = (
    ("nvml.dll",) if sys.platform == "win32"
    else ("libnvidia-ml.so.1", "libnvidia-ml.so")
)
_NVML = {"lib": None, "handle": None, "dead": False}


def _nvml_open() -> bool:
    """惰性加载 nvml 并缓存 device 0 句柄；加载失败则永久跳过，交给 smi 兜底。"""
    if _NVML["handle"] is not None:
        return True
    if _NVML["dead"]:
        return False
    lib = None
    for name in _NVML_LIBS:
        try:
            lib = ctypes.WinDLL(name) if sys.platform == "win32" else ctypes.CDLL(name)
            break
        except OSError:
            continue
    if lib is None:
        _NVML["dead"] = True
        return False
    try:
        init = getattr(lib, "nvmlInit_v2", None) or lib.nvmlInit
        init.restype = ctypes.c_int
        if init() != 0:
            _NVML["dead"] = True
            return False
        get_handle = (
            getattr(lib, "nvmlDeviceGetHandleByIndex_v2", None)
            or lib.nvmlDeviceGetHandleByIndex
        )
        get_handle.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
        get_handle.restype = ctypes.c_int
        handle = ctypes.c_void_p()
        if get_handle(0, ctypes.byref(handle)) != 0 or not handle.value:
            _NVML["dead"] = True
            return False
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
        _NVML["dead"] = True
        return False
    _NVML["lib"], _NVML["handle"] = lib, handle
    return True


def _nvml_reads():
    """一次读齐 (util%, used_mb, total_mb, name)；任一环节失败返回 None。"""
    if not _nvml_open():
        return None
    lib, handle = _NVML["lib"], _NVML["handle"]
    try:
        util = _NvmlUtil()
        if lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(util)) != 0:
            return None
        mem = _NvmlMem()
        if lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)) != 0:
            return None
        name = None
        get_name = getattr(lib, "nvmlDeviceGetName", None)
        if get_name is not None:
            buf = ctypes.create_string_buffer(96)
            if get_name(handle, buf, len(buf)) == 0:
                name = buf.value.decode("utf-8", "replace").strip() or None
        return (
            int(util.gpu),
            int(mem.used) // 1024**2,
            int(mem.total) // 1024**2,
            name,
        )
    except Exception:
        return None


# 静态信息（型号 / 显存总量）缓存，但失败后每 15s 允许重试一次：
# 旧实现只在 import 时读一次，驱动短暂抖动就会让 GPU 指标整场消失。
_GPU_INFO_RETRY_SEC = 15.0
_gpu_info_cache = {"t": 0.0, "info": None}


def _gpu_static_info():
    """返回 {"name", "vram_total_mb"}；无 GPU 或暂时读不到返回 None。"""
    if _gpu_info_cache["info"] is not None:
        return _gpu_info_cache["info"]
    now = time.time()
    if _gpu_info_cache["t"] and now - _gpu_info_cache["t"] < _GPU_INFO_RETRY_SEC:
        return None
    _gpu_info_cache["t"] = now

    r = _nvml_reads()
    if r is not None:
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
    if now - _smi_cache["t"] > 1.0:
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
        if util is not None and used is not None:
            try:
                data["gpu_percent"] = int(util)
                data["vram_used_mb"] = int(used)
                data["vram_total_mb"] = gpu.get("vram_total_mb") or 0
            except (TypeError, ValueError):
                pass  # 个别驱动返回 "N/A"，宁可少两项也不让整次轮询 500
    data["gpu_name"] = (gpu or {}).get("name")
    return data


# ---- web UI: static files live in web/ and are served from the site root ----
_WEB_DIR = os.path.join(current_dir, "web")

# 静态资源内容指纹：启动时读 web/ 下各文件 md5 前 8 位作版本号，注入 index.html
# 的 ?v= 占位符。文件改动 → 指纹变 → URL 变，配合长缓存头让浏览器自动拉新，
# 不再手工维护 ?v=20260912 这类日期版本号。
_ASSET_FINGERPRINT = {}


def _build_asset_fingerprints():
    for fname in os.listdir(_WEB_DIR):
        fpath = os.path.join(_WEB_DIR, fname)
        if not os.path.isfile(fpath) or fname == "index.html":
            continue
        try:
            with open(fpath, "rb") as f:
                _ASSET_FINGERPRINT[fname] = hashlib.md5(f.read()).hexdigest()[:8]
        except OSError:
            continue


_build_asset_fingerprints()


def _asset_response(name: str):
    """Serve one file from web/ by basename (no path traversal).

    静态资源带一年 immutable 缓存：URL 中的 ?v= 内容指纹变了浏览器才会拉新，
    所以可以放心让浏览器与磁盘缓存长期复用（app.js 52KB + style.css 32KB
    每次刷新都重新下载纯属浪费）。
    """
    fname = os.path.basename(name)
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
    # 把 ?v=xxx 占位符替换为内容指纹；favicon 之类没指纹的保持原样
    html = re.sub(r'\?v=\{([a-z0-9_.]+)\}', lambda m: f"?v={_ASSET_FINGERPRINT.get(m.group(1), '0')}", html)
    return Response(content=html, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/assets/{name}")
def asset(name: str):
    return _asset_response(name)


@api.get("/status")
def status():
    """合并端点：模型状态 + 系统仪表一次往返。

    前端 Header 每 2s 轮询一次这里即可（此前 /model 与 /metrics 各发一次，
    多标签页叠加时请求数翻倍）。
    """
    data = {"ok": True, "generating": _INFER_LOCK.locked(), **tts.state()}
    data.update(metrics())
    return data


@api.get("/model")
def model_state():
    # generating 让模型页在用户点之前就知道「现在动模型会被打断」，
    # 从而把不可用的按钮置灰、并给「卸载」换上会说明后果的确认框。
    return {"ok": True, "generating": _INFER_LOCK.locked(), **tts.state()}


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
    "diffusion_steps", "inference_cfg_rate",
})


@api.post("/model/config")
def model_config(req: dict = Body(...)):
    """存加速配置。不排队、不等推理锁——set_runtime 只写标量，见其 docstring。"""
    unknown = [k for k in req if k not in _MODEL_CONFIG_KEYS]
    if unknown:
        raise HTTPException(400, f"Unknown config keys: {', '.join(sorted(unknown))}")
    for key, val in req.items():
        tts.set_runtime(**{key: val})
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
        # 和 /model/config 一致：写配置不占推理锁，只有 rebuild 才需要
        for key, val in req.items():
            tts.set_runtime(**{key: val})
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
        and now - job.get("finished_at", 0) >= JOB_RETENTION_SECONDS
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
    max_text_tokens_per_segment: int = Form(120),
    do_sample: bool = Form(True),
    top_p: float = Form(0.8),
    top_k: int = Form(30),
    temperature: float = Form(0.8),
    length_penalty: float = Form(0.0),
    num_beams: int = Form(3),
    repetition_penalty: float = Form(10.0),
    max_mel_tokens: int = Form(1500),
    interval_silence: int = Form(200),      # 段间停顿（毫秒）
    file_naming: str = Form("title_time"),   # title_time / title / default
):
    if file_naming not in ("title_time", "title", "default"):
        file_naming = "title_time"
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
    # 下限 10 而不是 0：完全不留停顿时多段文本会连成一串，听起来像漏气。
    # 上限 500：再大就不是「停顿」而是「朗读课文」了，超出请分段生成。
    # 注意：签名已是 int = Form(200)，非数字输入在进函数体前就被 FastAPI 以
    # 422 拒绝，和其它 int 参数（max_text_tokens_per_segment 等）行为一致，
    # 这里只需要夹一下范围，不需要再转一次。
    interval_silence = max(10, min(500, interval_silence))
    text_warn = None
    if len(text) > TEXT_CHARS_SOFT:
        # 只警告不拦。段数估算按中文约 1.31 token/字、默认每段 120 token
        # 折算（实测 2000 字 -> 2628 token -> 28 段）。这里只给量级，
        # 前端有更准的分段预览可看。
        est_tokens = int(len(text) * 1.31)
        est_segs = max(1, est_tokens // max(1, max_text_tokens_per_segment))
        est_min = round(est_segs * 8 / 60)          # 每段约 8s 的保守估计
        text_warn = (
            f"文本较长（{len(text)} 字，约切 {est_segs} 段，预计 {est_min} 分钟以上）。"
            "生成期间会独占 GPU，其他任务需排队；"
            "关闭或刷新页面不会中断，服务端会继续跑完。"
        )
    if emo_text and len(emo_text) > MAX_EMO_TEXT_CHARS:
        raise HTTPException(
            413,
            f"情感描述文本过长（{len(emo_text)} 字，上限 {MAX_EMO_TEXT_CHARS} 字）",
        )
    max_text_tokens_per_segment = _clamp_seg_tokens(max_text_tokens_per_segment)
    try:
        spk_data = spk_audio.file.read()
    except Exception as e:
        raise HTTPException(400, f"Failed to read speaker audio: {e}")
    _validate_audio_upload(spk_data, "音色参考音频")
    spk_path = _prompt_file("spk", spk_data)
    try:
        if os.path.exists(spk_path):
            # LRU touch: cap_prompt_files 按旧 mtime 驱逐，常用音色常驻
            os.utime(spk_path, None)
        else:
            with open(spk_path, "wb") as f:
                f.write(spk_data)
            _cap_prompt_files(PROMPTS_DIR)
    except Exception as e:
        raise HTTPException(400, f"Failed to save speaker audio: {e}")

    emo_ref_path = None
    if emo_audio is not None:
        try:
            emo_data = emo_audio.file.read()
        except Exception as e:
            raise HTTPException(400, f"Failed to read emo audio: {e}")
        _validate_audio_upload(emo_data, "情感参考音频")
        emo_ref_path = _prompt_file("emo", emo_data)
        try:
            if os.path.exists(emo_ref_path):
                os.utime(emo_ref_path, None)
            else:
                with open(emo_ref_path, "wb") as f:
                    f.write(emo_data)
                _cap_prompt_files(PROMPTS_DIR)
        except Exception as e:
            raise HTTPException(400, f"Failed to save emo audio: {e}")

    # emo_mode 来自表单是字符串；非数字用 400 明确拒绝，而不是在 int() 上
    # 抛未捕获异常变成 500。越界值（只允许 0~3）同样拒绝——此前 9 之类会
    # 静默落进 else 当成 0 处理。
    try:
        mode = int(emo_mode)
    except (TypeError, ValueError):
        raise HTTPException(400, "情感模式参数无效（应为 0~3 的整数）")
    if mode not in (0, 1, 2, 3):
        raise HTTPException(400, "情感模式参数无效（应为 0~3 的整数）")
    if mode == 0:
        emo_ref_path = None
        vec = None
    elif mode == 1:
        vec = None
    elif mode == 2:
        vec = tts.normalize_emo_vec(
            [vec1, vec2, vec3, vec4, vec5, vec6, vec7, vec8], apply_bias=True
        )
    else:
        vec = None

    if emo_text == "":
        emo_text = None

    t0 = time.time()
    out_path = None  # 在拿到推理锁后由 run_infer 计算（见下）
    progress_q = queue.Queue()
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
            logger.error("TTS worker crashed: %s", e, exc_info=True)
            # 兜底:任何未捕获异常都必须以终止事件收尾,否则 SSE 会永久挂起
            try:
                record_event({"type": "error", "detail": f"Inference failed: {e}"})
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
            )
            if result is None or not os.path.exists(out_path):
                record_event({"type": "error", "detail": "Inference produced no output"})
            else:
                elapsed = round(time.time() - t0, 2)
                wav_name = Path(out_path).name
                entry = _history_add(wav_name, text, elapsed, mode)
                # 触到 max_mel_tokens 上限的生成是静默截断，前端必须提示
                mel_capped = bool(getattr(model, "mel_tokens_capped", False))
                # 文本规范化失败并退回原文同样是无声的（读音不标准但音频出来了），
                # 一起透给前端。None 表示一切正常。
                normalize_fallback = getattr(model, "text_normalize_fallback", None)
                record_event({
                    "type": "done",
                    "wav": f"{API_V1}/audio/{wav_name}",
                    "file": wav_name,
                    "elapsed": elapsed,
                    "history_id": entry["id"],
                    "mel_capped": mel_capped,
                    "normalize_fallback": normalize_fallback,
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
            record_event({"type": "error", "detail": f"Inference failed: {e}"})
        finally:
            if model is not None:
                model.gr_progress = prev_progress

    threading.Thread(target=run_infer, daemon=True).start()

    def gen():
        while True:
            ev = progress_q.get()
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
    data = await file.read()
    _validate_audio_upload(data, "音频")
    if not (_TEMPO_MIN <= rate <= _TEMPO_MAX):
        raise HTTPException(
            400, f"变速倍率超出范围（{rate}，允许 {_TEMPO_MIN}–{_TEMPO_MAX}）"
        )
    loop = asyncio.get_event_loop()
    try:
        with tempfile.TemporaryDirectory() as td:
            in_path = os.path.join(td, "in.wav")
            with open(in_path, "wb") as f:
                f.write(data)
            wav = await loop.run_in_executor(None, _stretch_wav, in_path, rate)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("tempo failed")
        raise HTTPException(500, f"变速失败：{e}")
    return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "no-cache"})


# ---- generation history -------------------------------------------------------
# Scope: this server run only (in-memory, nothing persisted). Writes happen from
# the inference thread, which _INFER_LOCK already serializes, so the list needs no
# lock of its own -- reads just take a snapshot of an append-only list.
HISTORY_LIMIT = 20          # auto-clean keeps the newest N; older entries drop off

_history = []               # newest first
_history_auto_clean = True


def _history_trim():
    """Drop entries past HISTORY_LIMIT, deleting their audio files too."""
    while len(_history) > HISTORY_LIMIT:
        old = _history.pop()
        if not _history_auto_clean:
            continue
        try:
            os.remove(os.path.join(OUTPUTS_DIR, os.path.basename(old["file"])))
        except OSError:
            pass


def _history_add(file_name: str, text: str, elapsed: float, emo_mode: int) -> dict:
    """Record one successful generation and return the new entry."""
    try:
        size = os.path.getsize(os.path.join(OUTPUTS_DIR, file_name))
    except OSError:
        size = 0
    entry = {
        "id": uuid.uuid4().hex[:8],
        "file": file_name,
        "url": f"{API_V1}/audio/{file_name}",
        "text": _safe_title(text, 60),
        "chars": len(str(text or "")),
        "elapsed": round(float(elapsed), 2),
        "emo_mode": int(emo_mode),
        "size": size,
        "created_at": time.time(),
    }
    _history.insert(0, entry)
    _history_trim()
    return entry


@api.get("/history")
def history_list():
    return JSONResponse({
        "items": _history,
        "limit": HISTORY_LIMIT,
        "auto_clean": _history_auto_clean,
        "count": len(_history),
    })


@api.post("/history/config")
def history_config(auto_clean: bool = Form(...)):
    global _history_auto_clean
    _history_auto_clean = bool(auto_clean)
    return JSONResponse({"auto_clean": _history_auto_clean, "count": len(_history)})


@api.delete("/history")
def history_clear():
    """Clear the list; with auto-clean on, the audio files go too."""
    removed = len(_history)
    if _history_auto_clean:
        for e in list(_history):
            try:
                os.remove(os.path.join(OUTPUTS_DIR, os.path.basename(e["file"])))
            except OSError:
                pass
    _history.clear()
    return JSONResponse({"ok": True, "removed": removed, "count": 0})


@api.delete("/history/{item_id}")
def history_remove(item_id: str):
    hit = None
    for e in _history:
        if e["id"] == item_id:
            hit = e
            break
    if hit is None:
        raise HTTPException(404, "History entry not found")
    # 原地删除：_history 是全局单例，推理线程会向它 insert；此处一旦重绑定
    # 新列表，并发插入会落到被丢弃的旧列表上（条目和自动清理都丢）。
    _history.remove(hit)
    if _history_auto_clean:
        try:
            os.remove(os.path.join(OUTPUTS_DIR, os.path.basename(hit["file"])))
        except OSError:
            pass
    return JSONResponse({"ok": True, "count": len(_history)})


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
    interval_silence: int = Form(200),
    prompt_audio: UploadFile | None = File(None),
    emo_audio: UploadFile | None = File(None),
    overwrite: bool = Form(False),
):
    name = _fix_mojibake(name)
    emo_text = _fix_mojibake(emo_text)
    # 同名保护：未显式声明 overwrite 时返回 409，由前端弹确认，不再静默覆盖
    if preset_exists(name):
        if not overwrite:
            raise HTTPException(409, f"预设「{name}」已存在")
        delete_preset(name)
    data = {
        "emo_control_method": int(emo_control_method),
        "emo_weight": float(emo_weight),
        "emo_vector": [float(v) for v in (vec1, vec2, vec3, vec4, vec5, vec6, vec7, vec8)],
        "emo_text": emo_text or "",
        "emo_random": bool(use_random),
        "advanced_params": {
            "do_sample": bool(do_sample),
            "top_p": float(top_p),
            "top_k": int(top_k),
            "temperature": float(temperature),
            "length_penalty": float(length_penalty),
            "num_beams": int(num_beams),
            "repetition_penalty": float(repetition_penalty),
            "max_mel_tokens": int(max_mel_tokens),
            "max_text_tokens_per_segment": int(max_text_tokens_per_segment),
            "interval_silence": int(interval_silence),
        },
    }

    prompt_tmp = None
    emo_tmp = None
    try:
        if prompt_audio is not None:
            prompt_data = prompt_audio.file.read()
            _validate_audio_upload(prompt_data, "预设音色音频")
            prompt_tmp = os.path.join(PROMPTS_DIR, f"preset_prompt_{uuid.uuid4().hex}.wav")
            with open(prompt_tmp, "wb") as f:
                f.write(prompt_data)
        if emo_audio is not None:
            emo_data = emo_audio.file.read()
            _validate_audio_upload(emo_data, "预设情感音频")
            emo_tmp = os.path.join(PROMPTS_DIR, f"preset_emo_{uuid.uuid4().hex}.wav")
            with open(emo_tmp, "wb") as f:
                f.write(emo_data)
        save_preset(name, data, prompt_audio=prompt_tmp, emo_audio=emo_tmp)
    except Exception as e:
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
    data = load_preset(name)
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
    try:
        final = duplicate_preset(name, new_name)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return JSONResponse({"ok": True, "name": final, "presets": list_presets()})


# ---- UI 偏好：参数锁定 ---------------------------------------------------------
# 存磁盘而不是浏览器 localStorage：用户清缓存后锁定的参数不该丢。
# 文件放在项目根的 data/ 下，不放 outputs/ —— 那是生成产物目录，会被保留期清理。
_UI_STATE_DIR = os.path.join(current_dir, "data")
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


@api.get("/prefs/param-lock")
def prefs_param_lock_get():
    with _UI_STATE_LOCK:
        payload = _read_ui_state().get("paramLock")
    if not isinstance(payload, dict):
        payload = {"locked": False, "values": {}}
    return JSONResponse(payload)


@api.put("/prefs/param-lock")
def prefs_param_lock_put(req: dict = Body(...)):
    locked = req.get("locked", False)
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
    with _UI_STATE_LOCK:
        state = _read_ui_state()
        state["paramLock"] = payload
        _write_ui_state(state)
    return JSONResponse(payload)


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


@api.get("/examples/{name}")
def examples_file(name: str):
    path = os.path.join(current_dir, "examples", os.path.basename(name))
    if not os.path.exists(path):
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="audio/wav")


@api.post("/segments")
def segments_preview(text: str = Form(""), max_text_tokens_per_segment: int = Form(120)):
    text = text.strip()
    if not text:
        return JSONResponse({"segments": []})
    if len(text) > TEXT_CHARS_HARD:
        raise HTTPException(
            413,
            f"文本过长（{len(text)} 字，上限 {TEXT_CHARS_HARD} 字）。"
            "这个上限只是防止误粘贴卡死服务；正常长文请分几次生成。",
        )
    max_text_tokens_per_segment = _clamp_seg_tokens(max_text_tokens_per_segment)
    tts._ensure_light()
    tokens = tts.tokenizer.tokenize(text)
    segs = tts.tokenizer.split_segments(tokens, max_text_tokens_per_segment=max_text_tokens_per_segment)

    def _readable(seg):
        """把 BPE 词元拼回可读文本。

        词元序列本身就是【规范化之后】的文本（也就是真正会被念出来的内容），
        只是被 BPE 切碎、每个词元用 ▁ 标记词首。所以直接拼起来、去掉 ▁ 就行。

        这里刻意【不】用 tokenizer.decode()：它会走 indextts/utils/common.py 的
        de_tokenized_by_CJK_char，那函数先��英文词替换成 <sent_N> 占位符再还原，
        还原的条件是 `if len(all_matches) > 1`——单个英文词只产生 1 个占位符，
        于是永远还原不回来。实测后果（改用 decode 时的数据）：

            这里有 English 这个单词。 -> 这里有<sent_0>这个单词.
            Hello world, this is a test. -> <sent_0>,<sent_1>.   信息全丢
            test@example.com -> test<sent_2>?example.com        内容还被改坏

        纯中文 / 纯数字场景下 decode 的输出与直接拼词元完全一致，所以换成直接拼
        没有任何损失，英文场景则是从「看不见」变成「看得见」。

        不用 sp_model.Decode 也不行：它会给汉字之间也插空格（"这 是 一 段"），
        等于退回更早的碎片显示。

        decode() 本身在 indextts/infer_v2.py:886 解析 Qwen 情绪 JSON 时被真实
        用到，那个上游函数的还原判断不该为了预览去动。
        """
        return "".join(seg).replace("▁", "")

    return JSONResponse({
        "segments": [
            {"index": i, "text": _readable(s), "tokens": len(s)}
            for i, s in enumerate(segs)
        ]
    })


def _glossary_dict():
    tts._ensure_light()
    return {k: v for k, v in (tts.normalizer.term_glossary or {}).items()}


def _glossary_payload():
    tts._ensure_light()
    return {
        "glossary": _glossary_dict(),
        "cascade": bool(getattr(tts.normalizer, "cascade_glossary", False)),
    }


@api.get("/glossary")
def glossary_list():
    return JSONResponse(_glossary_payload())


@api.post("/glossary")
def glossary_add(
    term: str = Form(...),
    reading_zh: str = Form(""),
    reading_en: str = Form(""),
    cascade: bool | None = Form(None),
):
    tts._ensure_light()  # 懒加载：首次请求可能是本端点，normalizer 此时还是 None
    term = _fix_mojibake(term).strip()
    reading_zh = _fix_mojibake(reading_zh).strip()
    reading_en = _fix_mojibake(reading_en).strip()
    if not term:
        raise HTTPException(400, "请输入术语")
    if not reading_zh and not reading_en:
        raise HTTPException(400, "请至少输入一种读法")

    if reading_zh and reading_en:
        reading = {"zh": reading_zh, "en": reading_en}
    elif reading_zh:
        reading = {"zh": reading_zh}
    else:
        reading = {"en": reading_en}

    # 复制写入：不要原地往 term_glossary 上加 key（见 _glossary_replace）
    new_glossary = dict(tts.normalizer.term_glossary)
    new_glossary[term] = reading
    _glossary_replace(new_glossary)
    saved = _save_glossary()
    _set_cascade(cascade, saved)
    return JSONResponse(_glossary_payload())


def _glossary_replace(new_dict):
    """整本换掉术语表，不做原地修改。

    推理线程可能正在遍历 term_glossary（front.py 的 apply_glossary_terms）。
    原地增删会抛 RuntimeError: dictionary changed size during iteration，
    该异常又会被 normalize() 的兜底接住，表现为「读音不对但一声不吭」。
    重新赋值是原子操作，遍历中的线程继续用旧对象，不受影响。
    """
    tts.normalizer.term_glossary = new_dict


def _save_glossary():
    """落盘术语表。出错直接 500，不吞——静默写失败会让人以为改了其实没改。"""
    try:
        tts.normalizer.save_glossary_to_yaml(tts.glossary_path)
    except Exception as e:
        raise HTTPException(500, f"保存词汇表出错: {e}")


def _set_cascade(cascade, saved: bool = True):
    """设置级联替换开关。None 表示本次请求没带这个参数，保持原值不动。"""
    if cascade is None:
        return
    tts.normalizer.cascade_glossary = bool(cascade)
    try:
        tts.normalizer.save_glossary_settings(tts.glossary_settings_path)
    except Exception as e:
        # 内存里已经改了但没落盘：提示用户「重启后会失效」，别默默丢掉
        logger.warning("Failed to save glossary settings: %s", e)
        raise HTTPException(500, f"级联开关已生效但保存失败，重启后会恢复默认: {e}")
    logger.info("Glossary cascade replacement: %s", tts.normalizer.cascade_glossary)


@api.post("/glossary/cascade")
def glossary_set_cascade(cascade: bool = Form(...)):
    """只改级联开关，不动术语表。"""
    tts._ensure_light()
    _set_cascade(cascade)
    return JSONResponse(_glossary_payload())


@api.delete("/glossary")
def glossary_clear():
    tts._ensure_light()
    _glossary_replace({})
    _save_glossary()
    return JSONResponse(_glossary_payload())


@api.delete("/glossary/{term}")
def glossary_remove(term: str):
    """删除单条术语。

    路径参数里的术语可能带空格/中文/符号（"PCIe 5.0"、"C++"），前端必须
    encodeURIComponent 后再拼 URL，FastAPI 会自动做百分号解码。
    """
    tts._ensure_light()
    term = _fix_mojibake(term).strip()
    if not term:
        raise HTTPException(400, "术语不能为空")
    glossary = tts.normalizer.term_glossary
    if not glossary or term not in glossary:
        raise HTTPException(404, "术语不存在")
    new_glossary = dict(glossary)
    del new_glossary[term]
    _glossary_replace(new_glossary)
    _save_glossary()
    return JSONResponse(_glossary_payload())


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
    removed = _clean_stale_outputs()
    if removed:
        logger.info("Cleaned %d output file(s) older than %d days.", removed, OUTPUTS_RETENTION_DAYS)
    logger.info("IndexTTS2 server starting... port %d", cmd_args.port)
    # 后台加载 numpy + audiotsm：变速引擎的 import 成本提前付掉，顺便让「包内
    # 缺少 audiotsm」在启动日志里就报出来，而不是等用户拖滑块时卡在「变速计算中…」。
    # 失败不影响服务。
    threading.Thread(target=_warmup_tempo, daemon=True, name="tempo-warmup").start()
    uvicorn.run(app, host=cmd_args.host, port=cmd_args.port, log_level="warning")
