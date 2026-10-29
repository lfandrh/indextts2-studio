"""Pure helpers for the FastAPI server (server.py).

Kept import-light (stdlib only) so tests can exercise the text-repair and
file-naming rules without importing torch or FastAPI. server.py imports
these re-exports; nothing here may depend on the app instance.
"""
import hashlib
import os
import re
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path

# Mojibake repair ------------------------------------------------------------
# Some clients hand us text that was already mangled BEFORE it reached HTTP:
# a console piping GBK bytes, a paste out of a GBK document, a tool that
# re-encoded with the system ANSI codepage. Two shapes show up in outputs/:
#   "楂樼鐨勯熸潗"        UTF-8 bytes read back as GBK
#   "ä»Šå¤©å¤©æ°”"      UTF-8 bytes read back as cp1252
# Both are reversible: re-encode with the suspect charset, decode as UTF-8.
MOJIBAKE_CHARSETS = ("cp1252", "gbk", "big5", "shift_jis", "latin-1")


def fix_mojibake(s: str) -> str:
    """Recover text whose UTF-8 bytes were decoded with the wrong charset.

    Only fires when the input carries an explicit decode-failure marker
    (U+FFFD). An unconditional round-trip is NOT safe here: the GBK bytes of
    ordinary Chinese can themselves form valid UTF-8. "为什么" encodes to GBK
    as CE AA CA B2 C3 B4, which is a perfectly valid UTF-8 sequence decoding to
    "Ϊʲô" -- so trying every charset on every input rewrites correct text into
    junk. Checked against the 481 real filenames in outputs/ -- all clean, zero
    false positives.

    Mangling is usually lossy (whatever failed to decode became U+FFFD), so
    the recovery is partial rather than exact.
    """
    if not s or "\ufffd" not in s:
        return s
    stripped = s.replace("\ufffd", "")
    if not stripped:
        return s
    for enc in MOJIBAKE_CHARSETS:
        try:
            raw = stripped.encode(enc)
        except (UnicodeEncodeError, LookupError):
            continue
        for errors in ("strict", "replace"):
            try:
                cand = raw.decode("utf-8", errors=errors)
            except UnicodeDecodeError:
                continue
            if cand and cand != s and cand.count("\ufffd") <= s.count("\ufffd"):
                return cand
    return s


def safe_title(text, maxlen=15):
    """Normalize arbitrary text into a filename-safe title."""
    t = strip_invisible(fix_mojibake(str(text or "")))
    t = re.sub(r"[\r\n\t]+", " ", t)
    t = re.sub(r'[\\/:*?"<>|]+', " ", t)
    t = t.replace("\ufffd", "")                 # drop decode-failure markers
    t = re.sub(r"[\x00-\x1f\x7f]+", "", t)      # drop control chars
    t = re.sub(r"\s+", " ", t).strip()
    t = t.strip(" .")
    if not t:
        return ""
    t = t[:maxlen].strip(" .")                  # 截断后可能又露出尾点/尾空格
    return escape_win_reserved(t) or ""


# Windows 保留设备名 ----------------------------------------------------------
# CON / PRN / AUX / NUL / COM1-9 / LPT1-9 即使带扩展名也仍被当成设备：
#   outputs/NUL.wav  -> open() 写到空设备，文件根本不落盘
#   mkdtemp()/NUL    -> FileNotFoundError
# 实测：文本为 "LPT1" 时 output_name() 产出裸的 LPT1.wav，
# 真实推理报 "Error opening 'outputs\\LPT1.wav': System error"，
# 烧完整轮 GPU 算力只换来一句与模型毫无关系的报错。
# WebUI 默认用 title_time 命名（带时间戳后缀）侥幸躲过，
# 但 file_naming=title 是 API 的合法取值，必然踩中。
# 预设名走的是 safe_preset_name -> 目录名，七个保留名全部 mkdir 失败。
_WIN_RESERVED = (
    {"con", "prn", "aux", "nul"}
    | {"com%d" % i for i in range(1, 10)}
    | {"lpt%d" % i for i in range(1, 10)}
)


def is_win_reserved(name: str) -> bool:
    """True if *name* collides with a Windows device, ignoring extension/case."""
    stem = str(name or "").strip().rstrip(" .").split(".")[0].strip().lower()
    return stem in _WIN_RESERVED


def escape_win_reserved(name: str) -> str:
    """Prefix reserved device names with "_" so they become ordinary files."""
    return "_" + name if is_win_reserved(name) else name


def strip_invisible(text: str) -> str:
    """Drop Unicode format characters (category Cf).

    它们在界面上完全看不见，却会进文件名造成两种麻烦：
      - U+202E 右到左覆盖让资源管理器把
        "报告<U+202E>gpj.exe_20261015.wav" 显示成 "报告gpj.exe_20261015.wav"，
        扩展名被伪装，看图和实际文件对不上；
      - U+200B / U+FEFF / U+00AD 让 "含零宽" 和 "含零宽" 生成两个肉眼
        完全相同的文件，肉眼根本删不掉其中之一。
    """
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def unique_path(path: str) -> str:
    """Return a non-existing path: append _2, _3... before the suffix."""
    p = Path(path)
    if not p.exists():
        return str(p)
    n = 2
    while True:
        cand = p.with_name(f"{p.stem}_{n}{p.suffix}")
        if not cand.exists():
            return str(cand)
        n += 1


def output_name(text: str, naming: str, outputs_dir: str = "outputs") -> str:
    """Pick the wav path for one generation.

    naming: "title_time" / "title" / "default" (timestamp+uuid).
    """
    title = safe_title(text)
    if naming == "title" and title:
        return unique_path(os.path.join(outputs_dir, f"{title}.wav"))
    if naming == "title_time" and title:
        ts = time.strftime("%Y%m%d_%H%M%S")
        return unique_path(os.path.join(outputs_dir, f"{title}_{ts}.wav"))
    return os.path.join(outputs_dir, f"spk_{int(time.time())}_{uuid.uuid4().hex[:6]}.wav")


def prompt_file(prompts_dir: str, kind: str, data: bytes) -> str:
    """Content-hash prompt filename: spk_<md5>.wav / emo_<md5>.wav.

    Same audio content maps to the same path, so the reference-audio feature
    cache in infer_v2 (keyed by path) hits across requests and the file is
    overwritten in place instead of piling up per request.
    """
    return os.path.join(prompts_dir, f"{kind}_{hashlib.md5(data).hexdigest()}.wav")


def clean_legacy_prompts(prompts_dir: str) -> None:
    """Remove old random-uuid prompt files; keep content-hash files for reuse.

    preset_prompt_*/preset_emo_* are per-request upload staging files removed
    in a finally block; only a mid-request crash can leave them behind, and
    this runs at startup when nothing is in flight, so they are always safe
    to delete.
    """
    for name in os.listdir(prompts_dir):
        if not name.endswith(".wav"):
            continue
        path = os.path.join(prompts_dir, name)
        if name.startswith(("preset_prompt_", "preset_emo_")):
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        kind, _, rest = name.partition("_")
        stem = rest[:-4] if rest.endswith(".wav") else rest
        try:
            with open(path, "rb") as f:
                digest = hashlib.md5(f.read()).hexdigest()
        except OSError:
            continue
        if kind in ("spk", "emo") and stem != digest:
            try:
                os.remove(path)
            except OSError:
                pass


PROMPTS_KEEP = 30  # cap on cached reference wavs; oldest evicted first


def cap_prompt_files(prompts_dir: str, keep: int = PROMPTS_KEEP) -> int:
    """Evict prompt wavs beyond `keep`, oldest mtime first (true LRU: callers
    touch the file on cache hit, so frequently used voices survive).

    Content-hash naming means every distinct reference audio ever uploaded
    leaves a file here forever without this cap. Eviction is always safe:
    the model's feature cache is keyed by path, and re-uploading the same
    audio re-creates the same path, costing only one re-extraction.
    Returns the number of files removed.
    """
    try:
        entries = [os.path.join(prompts_dir, n) for n in os.listdir(prompts_dir) if n.endswith(".wav")]
    except OSError:
        return 0
    excess = len(entries) - keep
    if excess <= 0:
        return 0
    entries.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
    removed = 0
    for path in entries[:excess]:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed


# Upload validation ----------------------------------------------------------
# 上传只做大小上限 + 常见音频容器的魔数嗅探：拦截改名的文本/图片这类明显
# 不是音频的文件，在落盘前给出可读的 4xx。完整解码校验仍由推理侧 librosa
# 承担（失败经 SSE 报错），这里不做重解码。

AUDIO_MAX_BYTES = 20 * 1024 * 1024


# ---------------------------------------------------------------------------
# 情感向量归一化（纯数学）
# ---------------------------------------------------------------------------
# 与 indextts/infer_v2.py:IndexTTS2.normalize_emo_vec 同源。此前 server 直接
# 调 tts.normalize_emo_vec()，而 LazyTTS 的每个方法开头都是 ensure_loaded()——
# 于是「对 8 个数做一次乘加」的代价变成了把 6.7GB 模型整个加载起来：
#   1. 开机后第一次用"情感向量控制"生成，点下按钮后界面长时间停在"准备生成…"，
#      没有进度也没有取消按钮；
#   2. 显存不够时用户看到的是 Request failed (500)，与真实原因无关；
#   3. 这行跑在 _register_job() 之前、不持推理锁，加载途中点"卸载模型"会把
#      _tts 置空，最后状态变成"说了卸载其实又被拉起来了"。
# 搬到这里后是纯函数，启动即可用，也不再有上面三条。
#
# 维度顺序（与前端 8 个滑块的标签一致）：
#   [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
EMO_VEC_BIAS = (0.9375, 0.875, 1.0, 1.0, 0.9375, 0.9375, 0.6875, 0.5625)
# 总和上限 0.8：超过会让情感把音色和语调一起压掉，合成结果听起来失真。
EMO_VEC_SUM_MAX = 0.8


def normalize_emo_vec(emo_vector, apply_bias: bool = True):
    """按偏置表缩放 8 维情感向量，并把总和压到 0.8 以内。

    纯数学，不依赖模型权重，所以服务刚起、模型还没加载时也能调用。
    与 infer_v2 的实现逐位一致（tests/test_server_helpers.py 有对齐断言，
    改这里必须同步改那边，否则两边行为会悄悄分叉）。
    """
    vec = [float(v) for v in emo_vector]
    if apply_bias:
        vec = [v * bias for v, bias in zip(vec, EMO_VEC_BIAS)]
    total = sum(vec)
    if total > EMO_VEC_SUM_MAX:
        scale = EMO_VEC_SUM_MAX / total
        vec = [v * scale for v in vec]
    return vec


def looks_like_audio(data: bytes) -> bool:
    """Cheap magic-byte sniff: wav / mp3 (ID3 or frame sync) / flac / ogg / m4a."""
    if not data or len(data) < 12:
        return False
    if data[:4] == b"RIFF":
        return data[8:12] == b"WAVE"
    if data[:4] == b"OggS" or data[:4] == b"fLaC":
        return True
    if data.startswith(b"ID3"):
        return True  # ID3v2-tagged MP3
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return True  # raw MP3 frame sync (first 11 bits set)
    if data[4:8] == b"ftyp":
        return True  # mp4/m4a
    return False


# wetext / kaldifst non-ASCII path workaround -----------------------------------
# kaldifst (C++ extension) opens .fst files via the ANSI C API: a wetext
# package installed under a non-ASCII path (e.g. a Chinese bundle folder)
# fails at import with "Error opening input stream". Fix: if the wetext
# package path is not pure-ASCII, copy its fsts directory once to an
# ASCII-safe cache and redirect importlib.resources.files for "wetext.fsts"
# to that copy. Must run before the first `import wetext`.

def _is_ascii(s):
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _writable_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_probe")
        with open(probe, "w") as f:
            f.write("1")
        os.remove(probe)
        return True
    except OSError:
        return False


def ensure_wetext_ascii_path():
    """No-op on ASCII install paths; redirect wetext FSTs to an ASCII copy otherwise.

    Returns True when a redirect was installed (non-ASCII install detected).
    """
    import importlib.util
    import importlib.resources
    import sys

    pkg_dir = None
    try:
        spec = importlib.util.find_spec("wetext")
        if spec is not None and spec.submodule_search_locations:
            pkg_dir = str(list(spec.submodule_search_locations)[0])
    except Exception:
        return False
    if not pkg_dir or _is_ascii(pkg_dir):
        return False  # normal ASCII install: nothing to do

    fsts_src = os.path.join(pkg_dir, "fsts")
    if not os.path.isdir(fsts_src):
        return False

    # %TEMP% often sits under a non-ASCII username on Chinese Windows;
    # ProgramData is the machine-wide ASCII-safe writable root.
    candidates = [
        os.path.join(os.environ.get("ProgramData", "C:\\ProgramData"), "IndexTTS2"),
        tempfile.gettempdir(),
    ]
    cache_root = next((c for c in candidates if _is_ascii(c) and _writable_dir(c)), None)
    if cache_root is None:
        return False
    fsts_dst = os.path.join(cache_root, "wetext_fsts")

    import shutil
    marker = os.path.join(cache_root, "wetext_fsts.done")
    try:
        if not (os.path.isdir(fsts_dst) and os.path.exists(marker)):
            shutil.rmtree(fsts_dst, ignore_errors=True)
            shutil.copytree(fsts_src, fsts_dst)
            with open(marker, "w") as f:
                f.write(fsts_src)
    except Exception:
        return False

    # Too late to patch if wetext already imported: its constants.py loads
    # the FSTs at module import time and would have crashed before this.
    if "wetext" in sys.modules:
        return False

    orig_files = importlib.resources.files

    class _AsciiFstsDir:
        """Minimal importlib.resources-style dir handle wetext can joinpath()."""
        def __init__(self, path):
            self._path = path
        def joinpath(self, name):
            return _AsciiFstsDir(os.path.join(self._path, str(name)))
        def __str__(self):
            return self._path

    def _patched_files(anchor):
        if str(anchor) == "wetext.fsts":
            return _AsciiFstsDir(fsts_dst)
        return orig_files(anchor)

    importlib.resources.files = _patched_files
    return True
