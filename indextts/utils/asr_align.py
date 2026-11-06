"""ASR 强制对齐：成品 wav + 已知文本 -> 句级字幕的时间坐标。

只有这个模块碰 ``sherpa_onnx`` / ``numpy``；切句与对齐的纯计算在
``indextts.utils.subtitle``，HTTP 与落盘在 ``server.py``。

设计口径：

* 字幕是附属产物，**任何失败都降级成段级**，绝不抛到生成流程里。
  运行时没装、权重缺失、模型加载失败、音频坏了、解码超时、时间戳为空、
  对齐率过低 —— 统统返回 ``(None, reason)``。
* 按【段】解码而不是整条音频：段边界本来就是物理精确的（sidecar 的
  ``start/end`` 与成品 wav 同轴），按段裁能避开长音频的 CIF/内存问题，
  也让 difflib 的对齐范围变小、更准。
* 字幕正文永远用用户原文，ASR 只提供时间。ASR 输出的是归一文本，
  同音字、数字读法都会被抹平（见 skills/indextts2-tts/references/pitfalls.md）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import wave
from importlib.util import find_spec
from pathlib import Path

# indextts/utils/asr_align.py -> 项目根
_ROOT = Path(__file__).resolve().parents[2]
_MODEL_DIR = _ROOT / "checkpoints" / "paraformer-zh-en-ts"
_MODEL = _MODEL_DIR / "model.sherpa.int8.onnx"
_TOKENS = _MODEL_DIR / "tokens_sherpa.txt"
# 随包可以只带一份原始权重：下面两个派生文件由目录内的 prepare_sherpa.py
# 在首次需要时重建（见 NOTICE.md 第七节）。缺了它们不该让字幕静默降级。
_PREPARE = _MODEL_DIR / "prepare_sherpa.py"
_RAW_MODEL = _MODEL_DIR / "model.int8.onnx"
_RAW_TOKENS = _MODEL_DIR / "tokens.txt"

# 一次生成的对齐总预算。超时后剩余段直接降级，宁可少切几句也不拖慢出音频。
MAX_ALIGN_SECONDS = 20.0
# 重建派生文件要复制 233MB 权重，留足余量但不许把生成流程吊死。
PREPARE_TIMEOUT_SECONDS = 180

_recognizer = None
_unavailable_reason = None
_lock = threading.Lock()


def _ensure_derived():
    """派生权重/词表缺失时，用随包的原始权重重建一次。

    分发时若按 NOTICE.md 的建议只保留 ``model.int8.onnx`` + ``prepare_sherpa.py``，
    就不会有 233MB 的派生副本。这里把它补出来，原文件一字节不动。
    任何失败都返回 False，由调用方降级，绝不影响语音合成。
    """
    if _MODEL.is_file() and _TOKENS.is_file():
        return True
    if not (_RAW_MODEL.is_file() and _RAW_TOKENS.is_file() and _PREPARE.is_file()):
        return False
    try:
        proc = subprocess.run(
            [sys.executable, str(_PREPARE)],
            cwd=str(_MODEL_DIR),
            capture_output=True,
            timeout=PREPARE_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            return False
    except Exception:
        return False
    return _MODEL.is_file() and _TOKENS.is_file()


def align_status():
    """/status 用的轻量自检：只看文件与包，**绝不加载 233MB 模型**。

    ``/status`` 每 2 秒被前端轮询一次，所以这里不能碰模型加载、不能跑子进程。
    ``derived_ready=False`` 且 ``available=True`` 表示首次生成时会自动重建。
    """
    try:
        runtime = find_spec("sherpa_onnx") is not None
    except Exception:
        runtime = False
    raw_ready = _RAW_MODEL.is_file() and _RAW_TOKENS.is_file()
    derived_ready = _MODEL.is_file() and _TOKENS.is_file()
    can_rebuild = raw_ready and _PREPARE.is_file()
    if not runtime:
        reason = "runtime_missing"
    elif not (derived_ready or can_rebuild):
        reason = "model_missing"
    else:
        reason = "ok"
    return {
        "available": reason == "ok",
        "reason": reason,
        "runtime": runtime,
        "weights": raw_ready or derived_ready,
        "derived_ready": derived_ready,
    }


def _get_recognizer():
    """懒加载单例：只有第一次真正生成字幕时才付 233MB 模型加载的代价。"""
    global _recognizer, _unavailable_reason
    if _recognizer is not None:
        return _recognizer
    if _unavailable_reason:
        return None
    with _lock:
        if _recognizer is not None:
            return _recognizer
        if _unavailable_reason:
            return None
        if not _ensure_derived():
            _unavailable_reason = "model_missing"
            return None
        try:
            import sherpa_onnx  # noqa: F401  仅在函数内导入，模块顶层保持零依赖
        except ImportError:
            _unavailable_reason = "runtime_missing"
            return None
        try:
            _recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=str(_MODEL),
                tokens=str(_TOKENS),
                num_threads=2,
                provider="cpu",
                debug=False,
            )
        except Exception:
            # C++ 侧可能直接 _Exit 或抛异常；无论哪种都不该弄死生成流程
            _unavailable_reason = "load_failed"
            return None
        return _recognizer


def _read_wav(path):
    """int16 单声道 wav -> (采样率, float32 样本)。

    sherpa-onnx 的 Paraformer 设了 ``normalize_samples=false``，要传
    **原始 int16 幅度**（-32768~32767），不能除以 32768 归一化。
    """
    import numpy as np

    with wave.open(str(path), "rb") as f:
        sr = f.getframerate()
        ch = f.getnchannels()
        sw = f.getsampwidth()
        pcm = f.readframes(f.getnframes())
    if sw != 2:
        return None, None
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if ch == 2:
        a = a.reshape(-1, 2).mean(axis=1)
    return sr, a


def _decode_segment(rec, sr, samples, seg):
    """裁出一段波形解码，返回 (tokens, timestamps)；时间戳相对段首、单位秒。"""
    start = float(seg.get("start") or 0.0)
    end = float(seg.get("end") or 0.0)
    if end <= start:
        return [], []
    clip = samples[int(start * sr):int(end * sr)]
    if len(clip) < max(1, int(sr * 0.1)):
        return [], []
    stream = rec.create_stream()
    stream.accept_waveform(sr, clip)
    rec.decode_stream(stream)
    result = stream.result
    tokens = list(result.tokens or [])
    try:
        stamps = [float(x) for x in (result.timestamps or [])]
    except (TypeError, ValueError):
        stamps = []
    return tokens, stamps


def align_sidecar(wav_path, segments):
    """把段级快照对齐成句级条目。

    返回 ``(sentences, method)``：

    * ``sentences`` 是 ``[{start, end, text}, ...]``（绝对秒，正文是用户原文）；
      不可用时为 ``None``，调用方应退回段级。
    * ``method`` 是 ``"asr"``（全部成功）/ ``"mixed"``（部分段降级）/
      ``"model_missing"`` / ``"runtime_missing"`` / ``"load_failed"`` /
      ``"no_audio"`` / ``"bad_audio"`` / ``"unavailable"``。

    逐段降级：某段失败就用该段原来的整段 cue 顶上，其余段照常句级，
    所以字幕始终完整覆盖音频，不会出现缺口。
    """
    if not segments or not wav_path or not os.path.isfile(wav_path):
        return None, "no_audio"
    rec = _get_recognizer()
    if rec is None:
        return None, _unavailable_reason or "unavailable"
    try:
        sr, samples = _read_wav(wav_path)
    except Exception:
        return None, "bad_audio"
    if sr is None or samples is None or len(samples) == 0:
        return None, "bad_audio"

    from indextts.utils.subtitle import sentences_from_segment

    deadline = time.monotonic() + MAX_ALIGN_SECONDS
    sentences = []
    methods = []
    for seg in segments:
        tokens, stamps = [], []
        if time.monotonic() <= deadline:
            try:
                tokens, stamps = _decode_segment(rec, sr, samples, seg)
            except Exception:
                tokens, stamps = [], []  # 单段解码失败不影响其余段
        cues, method = sentences_from_segment(seg, tokens, stamps)
        sentences.extend(cues)
        methods.append(method)

    n_asr = sum(1 for m in methods if m == "asr")
    if n_asr == 0:
        return None, "unavailable"
    return sentences, "asr" if n_asr == len(methods) else "mixed"
