"""参考音频条件向量的持久化缓存（IndexTTS2 主线程）。

设计目标：把「只在换音色时才用一下」的 w2v-bert / semantic_codec.quantize /
CAMPPlus / mel_fn / length_regulator 的计算结果落盘，命中时完全不加载
w2v-bert，从而把 8G 卡上的常驻显存降下来，并让常用音色免去首包 1~2 秒的
参考音频处理。

关键契约（改动本模块前务必读完）：

* ``spk`` 条目存 4 个张量、``emo`` 条目只存 ``emo_cond_emb``。两类条目的
  key 各自带角色前缀，因为 ``infer_generator`` 里 spk 与 emo 是两段独立的
  提取代码（16k 音频来源不同），把 emo 向量塞进 spk 条目会导致「同一音色
  换情绪参考音频后后写覆盖、情绪串味且不报错」。
* 磁盘上永远是 **CPU 张量**。``load(device=...)`` 负责逐张量搬回计算设备；
  调用方必须传 ``self.device``。下游 ``merge_emovec`` / ``get_conditioning``
  / ``torch.cat`` / CFM 全在计算设备上，漏搬会在第二次合成时 device mismatch。
* key 由「音频内容 hash + 配置/权重指纹」组成。指纹必须涵盖所有会改变这
  5 个张量数值的权重与精度开关（尤其 w2v-bert-2.0 目录和 CAMPPlus），否则
  升级权重后会静默命中旧向量。文件名里的指纹之外，payload metadata 里还会
  再校验一次，防止手改文件或旧版本残留。
* 本模块被 ``infer_v2.py`` 直接调用，而后者还会被 CLI / webui / profile 脚本
  直调，**不能依赖 server.py 的 ``_INFER_LOCK``**。写路径自带线程锁与跨进程
  文件锁；读路径依赖 ``os.replace`` 的原子性，不需要锁。
* 「缓存损坏」与「环境错误」必须分开处理：前者删文件，后者（Windows 文件
  被占用、磁盘满、只读目录）保留文件、只告警并回退 miss，绝不把好缓存删掉，
  也绝不因为缓存写失败让合成请求 500。

缓存目录默认 ``<项目根>/data/cond_cache``，可用环境变量
``INDEXTTS_COND_CACHE_DIR`` 覆盖（测试用它隔离，避免污染仓库）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

import torch
from safetensors import safe_open
from safetensors.torch import save_file

logger = logging.getLogger("indextts.cond_cache")

# schema 结构变更时 +1；写入 payload metadata，加载时强校验
CACHE_VERSION = "v1"
ROLES = ("spk", "emo")
EXPECTED_KEYS: Dict[str, Tuple[str, ...]] = {
    "spk": ("spk_cond_emb", "prompt_condition", "style", "ref_mel"),
    "emo": ("emo_cond_emb",),
}

DEFAULT_MAX_ITEMS = 200
DEFAULT_MAX_MB = 200

_FULL_HASH_LIMIT = 64 * 1024 * 1024   # 小于此值读全文件，否则只取首尾片段
_HEAD_TAIL = 1024 * 1024
_LOCK_TIMEOUT_SEC = 2.0
_LOCK_STALE_SEC = 30.0

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_DIR = _PROJECT_ROOT / "data" / "cond_cache"
_CACHE_DIR_ENV = "INDEXTTS_COND_CACHE_DIR"

_MD5_PROMPT_NAME = re.compile(r"^(?:spk|emo)_([0-9a-fA-F]{32})\.wav$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")

_STATS_LOCK = threading.Lock()
STATS: Dict[str, int] = {
    "hits": 0,
    "misses": 0,
    "writes": 0,
    "corrupted": 0,
    "io_errors": 0,
}
_WRITE_LOCK = threading.Lock()


class _Corrupted(Exception):
    """缓存文件内容/元数据不合法，应当删除并回退 miss。"""


# --------------------------------------------------------------------------
# 目录、计数、校验
# --------------------------------------------------------------------------

def _cache_dir(cache_dir=None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir)
    override = os.environ.get(_CACHE_DIR_ENV)
    return Path(override) if override else DEFAULT_CACHE_DIR


def stats() -> Dict[str, int]:
    """返回计数快照。模块级计数跨 ``LazyTTS.unload()/rebuild()`` 保留。"""
    with _STATS_LOCK:
        return dict(STATS)


def reset_stats() -> None:
    """仅供测试使用。"""
    with _STATS_LOCK:
        for key in STATS:
            STATS[key] = 0


def _bump(key: str, n: int = 1) -> None:
    with _STATS_LOCK:
        STATS[key] = STATS.get(key, 0) + n


def _validate_role(role: str) -> None:
    if role not in EXPECTED_KEYS:
        raise ValueError(f"unknown cond cache role: {role!r}")


def _validate_key_parts(audio_hash: str, cfg_fp: str) -> None:
    # 只允许十六进制，杜绝路径穿越 / 中文 / 空格 / 超长文件名
    if not _HEX64.match(audio_hash):
        raise ValueError("audio_hash must be a 64-char lowercase hex sha256")
    if not _HEX16.match(cfg_fp):
        raise ValueError("cfg_fp must be a 16-char lowercase hex string")


def cache_key(role: str, audio_hash: str, cfg_fp: str) -> str:
    _validate_role(role)
    _validate_key_parts(audio_hash, cfg_fp)
    return f"{role}__{audio_hash}__{cfg_fp}"


# --------------------------------------------------------------------------
# 音频内容 hash
# --------------------------------------------------------------------------

def _update_head_tail(f, h, size: int) -> None:
    """把文件首尾各 1MiB 喂进 hash；超大文件避免整读。"""
    h.update(f.read(_HEAD_TAIL))
    if size > 2 * _HEAD_TAIL:
        f.seek(-_HEAD_TAIL, os.SEEK_END)
        h.update(f.read(_HEAD_TAIL))


def _file_md5(p: Path) -> str:
    digest = hashlib.md5()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audio_hash(path) -> str:
    """音频的内容指纹（64 位小写 hex sha256）。

    ``/tts`` 上传路径的 ``spk_<md5>.wav`` / ``emo_<md5>.wav`` 文件名本身就是
    **转码后落盘 WAV 字节的 MD5**（``server_helpers.prompt_file``），名字与
    内容一致时直接对 md5 串做 sha256，免去再算一遍内容 hash。但捷径**必须
    以内容校验为前提**：本模块不只服务端在用（CLI / webui / profile 脚本会传
    任意本地路径），用户的文件也可能恰好叫这个名字——两份内容不同却同名的
    音频若还信文件名，就会命中同一条缓存、静默串味。所以先读内容核对 md5，
    不符一律退回内容 hash。代价是热路径多一次 md5 读（几 MB 参考音频约几毫秒，
    相对秒级合成可忽略）；服务端文件名字节＝写入字节（server.py 写盘处），
    核对永远通过，key 与历史条目完全兼容。
    """
    p = Path(path)
    m = _MD5_PROMPT_NAME.match(p.name)
    if m and _file_md5(p) == m.group(1).lower():
        return hashlib.sha256(b"md5:" + m.group(1).lower().encode("ascii")).hexdigest()

    h = hashlib.sha256()
    size = p.stat().st_size
    h.update(f"bytes:{size}:".encode("ascii"))
    with p.open("rb") as f:
        if size <= _FULL_HASH_LIMIT:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        else:
            _update_head_tail(f, h, size)
    return h.hexdigest()


# --------------------------------------------------------------------------
# 权重 / 配置指纹
# --------------------------------------------------------------------------

def file_signature(path) -> str:
    """轻量签名：文件名 + size + mtime_ns。文件不存在时返回可比较的占位值。"""
    p = Path(path)
    try:
        st = p.stat()
        return f"{p.name}:{st.st_size}:{int(st.st_mtime_ns)}"
    except OSError:
        return f"{p.name}:missing"


def dir_signature(path) -> str:
    """目录签名：内部所有普通文件的 file_signature 排序拼接。"""
    p = Path(path)
    if not p.is_dir():
        return f"{p.name}:missing-dir"
    try:
        files = sorted((f for f in p.iterdir() if f.is_file()), key=lambda x: x.name)
    except OSError:
        return f"{p.name}:unreadable-dir"
    return "|".join(file_signature(f) for f in files) or f"{p.name}:empty-dir"


def _content_digest(path) -> str:
    """内容片段 hash：文件读首尾 1MiB；目录逐文件读首尾 64KiB。"""
    p = Path(path)
    h = hashlib.sha256()
    if p.is_dir():
        try:
            files = sorted((f for f in p.iterdir() if f.is_file()), key=lambda x: x.name)[:32]
        except OSError:
            return "unreadable-dir"
        for f in files:
            h.update(f.name.encode("utf-8"))
            try:
                size = f.stat().st_size
                h.update(str(size).encode("ascii"))
                with f.open("rb") as fh:
                    h.update(fh.read(64 * 1024))
                    if size > 128 * 1024:
                        fh.seek(-64 * 1024, os.SEEK_END)
                        h.update(fh.read(64 * 1024))
            except OSError:
                h.update(b"unreadable")
    elif p.is_file():
        try:
            size = p.stat().st_size
            h.update(str(size).encode("ascii"))
            with p.open("rb") as fh:
                _update_head_tail(fh, h, size)
        except OSError:
            return "unreadable-file"
    else:
        return "missing"
    return h.hexdigest()


def _atomic_write_json(path: Path, data: Mapping) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2),
                       encoding="utf-8")
        os.replace(str(tmp), str(path))
    finally:
        _safe_unlink(tmp)


def build_weight_fingerprint(weights: Mapping[str, str], cache_dir=None) -> Dict[str, str]:
    """对权重路径算指纹，结果缓存在 ``_fingerprint.json`` manifest 里。

    ``size + mtime_ns`` 签名未变时直接复用上次的内容片段 hash，**不会每次
    启动遍历 GB 级权重**；签名变化时才重新读取该路径首尾片段（每个最多
    2MiB）。返回 ``{label: digest}``，交由 ``compute_cfg_fp`` 汇总。
    """
    root = _cache_dir(cache_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("cond cache: cannot create %s (%s); fingerprint falls back to signatures",
                       root, e)
        return {label: file_signature(p) if not os.path.isdir(p) else dir_signature(p)
                for label, p in weights.items()}

    manifest_path = root / "_fingerprint.json"
    try:
        raw = manifest_path.read_text(encoding="utf-8") if manifest_path.is_file() else "{}"
        manifest = json.loads(raw)
        if not isinstance(manifest, dict):
            manifest = {}
    except Exception as e:
        logger.warning("cond cache: unreadable fingerprint manifest (%s), rebuilding", e)
        manifest = {}

    out: Dict[str, str] = {}
    changed = False
    for label in sorted(weights):
        p = weights[label]
        sig = dir_signature(p) if os.path.isdir(p) else file_signature(p)
        entry = manifest.get(label)
        if isinstance(entry, dict) and entry.get("sig") == sig and entry.get("digest"):
            out[label] = str(entry["digest"])
            continue
        digest = f"{sig}:{_content_digest(p)[:16]}"
        out[label] = digest
        manifest[label] = {"sig": sig, "digest": digest}
        changed = True

    if changed:
        try:
            _atomic_write_json(manifest_path, manifest)
        except OSError as e:
            # manifest 只是加速缓存，写失败不影响正确性（下次启动重算）
            logger.warning("cond cache: cannot save fingerprint manifest (%s)", e)
    return out


def compute_cfg_fp(parts: Mapping[str, str]) -> str:
    """把影响缓存张量数值的所有维度汇总成 16 位 hex 指纹。

    ``parts`` 应包含：权重摘要（w2v / stat / codec / camp / s2mel）、
    ``w2v_fp16``、``s2mel_fp16``、``dev``、``prep``。不要放 ``fp16``
    （GPT 精度）、``qwen_fp16``、gpt.pth、bigvgan —— 它们不参与这 5 个
    张量的计算，放进去只会让无关的配置改动把全部音色缓存无谓失效。
    """
    all_parts = {"cache_version": CACHE_VERSION, **parts}
    payload = "|".join(f"{k}={all_parts[k]}" for k in sorted(all_parts))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------

def _safe_unlink(path) -> bool:
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _acquire_lock(lock_path: Path):
    """跨进程写锁：O_EXCL 原子创建，超时或陈旧锁都不阻塞合成。"""
    deadline = time.monotonic() + _LOCK_TIMEOUT_SEC
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("ascii"))
            except OSError:
                pass
            return fd
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > _LOCK_STALE_SEC:
                    _safe_unlink(lock_path)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)
        except OSError:
            # 目录不可写等：不阻塞主流程，直接放弃锁
            return None


def _release_lock(fd, lock_path: Path) -> None:
    try:
        os.close(fd)
    except OSError:
        pass
    _safe_unlink(lock_path)


def load(role: str, audio_hash: str, cfg_fp: str, *, device=None, cache_dir=None):
    """读取条件向量；命中时返回 ``{name: Tensor}``（已搬到 ``device``），否则 None。

    任何失败都回退 miss，不抛给调用方。文件损坏才删除；``OSError``
    （Windows 占用 / 磁盘满 / 权限）一律保留原文件，仅告警。
    """
    _validate_role(role)
    _validate_key_parts(audio_hash, cfg_fp)
    path = _cache_dir(cache_dir) / f"{cache_key(role, audio_hash, cfg_fp)}.safetensors"
    if not path.is_file():
        _bump("misses")
        return None

    try:
        with safe_open(str(path), framework="pt", device="cpu") as f:
            meta = f.metadata() or {}
            if meta.get("cache_version") != CACHE_VERSION:
                raise _Corrupted(f"cache_version={meta.get('cache_version')!r}")
            if meta.get("cfg_fp") != cfg_fp:
                raise _Corrupted("cfg_fp mismatch")
            expected = set(EXPECTED_KEYS[role])
            if set(f.keys()) != expected:
                raise _Corrupted(f"keys={sorted(f.keys())}")
            out: Dict[str, torch.Tensor] = {}
            for name in EXPECTED_KEYS[role]:
                t = f.get_tensor(name)
                shape_meta, dtype_meta = meta.get(f"shape:{name}"), meta.get(f"dtype:{name}")
                if not shape_meta or not dtype_meta:
                    raise _Corrupted(f"missing metadata for {name}")
                if json.loads(shape_meta) != list(t.shape):
                    raise _Corrupted(f"shape mismatch for {name}")
                if str(t.dtype) != dtype_meta:
                    raise _Corrupted(f"dtype mismatch for {name}")
                t = t.contiguous()
                if device is not None:
                    t = t.to(device)
                out[name] = t
    except OSError as e:
        _bump("io_errors")
        _bump("misses")
        logger.warning("cond cache: cannot read %s (%s: %s); falling back to extraction, "
                       "file kept", path.name, type(e).__name__, e)
        return None
    except Exception as e:
        _bump("corrupted")
        _bump("misses")
        logger.warning("cond cache: corrupted entry %s (%s); removed", path.name, e)
        _safe_unlink(path)   # 只删损坏；OSError 已在上面单独处理
        return None

    _bump("hits")
    return out


def save(role: str, audio_hash: str, cfg_fp: str, tensors: Mapping[str, torch.Tensor],
         *, cache_dir=None) -> bool:
    """原子写入一条缓存；失败只返回 False 并告警，绝不影响合成。"""
    _validate_role(role)
    _validate_key_parts(audio_hash, cfg_fp)
    expected = EXPECTED_KEYS[role]
    if set(tensors) != set(expected):
        raise ValueError(f"{role} entry expects exactly {expected}, got {sorted(tensors)}")

    root = _cache_dir(cache_dir)
    key = cache_key(role, audio_hash, cfg_fp)
    final = root / f"{key}.safetensors"
    tmp = root / f"{key}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    lock_path = root / f"{key}.lock"
    fd = None

    try:
        root.mkdir(parents=True, exist_ok=True)
        payload: Dict[str, torch.Tensor] = {}
        meta: Dict[str, str] = {
            "cache_version": CACHE_VERSION,
            "cfg_fp": cfg_fp,
            "role": role,
            "audio_hash": audio_hash,
            "created_at": f"{time.time():.3f}",
        }
        for name in expected:
            t = tensors[name]
            if not isinstance(t, torch.Tensor):
                raise TypeError(f"{name} must be a torch.Tensor, got {type(t).__name__}")
            t = t.detach().to("cpu").contiguous()
            payload[name] = t
            meta[f"shape:{name}"] = json.dumps(list(t.shape))
            meta[f"dtype:{name}"] = str(t.dtype)

        with _WRITE_LOCK:
            fd = _acquire_lock(lock_path)
            save_file(payload, str(tmp), metadata=meta)
            os.replace(str(tmp), str(final))
        _bump("writes")
        prune_global_cache(cache_dir=root)
        return True
    except OSError as e:
        _bump("io_errors")
        logger.warning("cond cache: write skipped for %s (%s: %s); synthesis continues",
                       key[:24], type(e).__name__, e)
        return False
    except Exception as e:
        # 不吞掉堆栈：这是编程/格式错误，日志里必须能查到
        _bump("io_errors")
        logger.exception("cond cache: unexpected write failure for %s (%s)", key[:24], e)
        return False
    finally:
        _safe_unlink(tmp)
        if fd is not None:
            _release_lock(fd, lock_path)


# --------------------------------------------------------------------------
# LRU
# --------------------------------------------------------------------------

def prune_global_cache(max_items: int = DEFAULT_MAX_ITEMS,
                       max_mb: int = DEFAULT_MAX_MB,
                       cache_dir=None) -> int:
    """按 mtime 从旧到新驱逐，直到条目数与总字节数同时满足上限。

    生产里在每次成功写入后与服务启动时各调一次（2.5 项目的同名函数是
    死代码，缓存目录会无限增长，这里不照抄）。
    """
    root = _cache_dir(cache_dir)
    if not root.is_dir():
        return 0
    try:
        entries = []
        for p in root.glob("*.safetensors"):
            try:
                st = p.stat()
                entries.append((st.st_mtime, st.st_size, p))
            except OSError:
                continue
    except OSError:
        return 0

    entries.sort(key=lambda e: e[0])
    total = sum(e[1] for e in entries)
    limit = max(0, max_mb) * 1024 * 1024
    removed = 0
    while entries and (len(entries) > max(0, max_items) or total > limit):
        _, size, path = entries.pop(0)
        if not _safe_unlink(path):
            break   # 删不动（Windows 占用）就停，避免死循环
        total -= size
        removed += 1
    return removed
