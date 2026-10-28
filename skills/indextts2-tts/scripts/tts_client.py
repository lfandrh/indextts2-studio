#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IndexTTS2 服务调用客户端（stdlib only，无需第三方依赖）。

只跟 HTTP 接口打交道，不 import 项目代码，因此对服务部署位置无假设：
本地起服务、远程服务器、Docker、反向代理后面都能用，只要改 INDEXTTS_URL。

用法示例：
    python tts_client.py locate                                  # 定位项目根（找不到则给三条路，退出码 3）
    python tts_client.py locate --set "D:\\IndexTTS2.0_v3"        # 用户给的路径：校验后记住
    python tts_client.py ensure                                  # 确保服务在跑（没跑就后台拉起）
    python tts_client.py health                                  # 探活
    python tts_client.py status                                  # 模型状态 + GPU 仪表
    python tts_client.py presets                                 # 列出预设
    python tts_client.py preset 胡桃                              # 查看预设详情
    python tts_client.py examples                                # 列出内置示例音色
    python tts_client.py segments --text "..."                   # 预览长文本分段
    python tts_client.py choose                                  # 交互选择预设（tty）；非 tty 打印候选+退出码 2
    python tts_client.py last [--open]                           # 最近一次生成的音频路径
    python tts_client.py synth --preset 胡桃 --text "..." --out out.wav
    python tts_client.py synth --voice examples/voice_01.wav --text "..." --out out.wav
    python tts_client.py synth --voice voice_01.wav --text "..."  # 内置示例音色：本地找不到会自动从服务端取
    python tts_client.py synth --voice v.wav --emo-mode 2 --emo-vector 0,0,0.8,0,0,0,0,0 --text "..."
    python tts_client.py tempo --file in.wav --rate 1.2 --out fast.wav   # 保音高变速，不重新合成
    python tts_client.py stop <job_id>                           # 停止任务（--keep-model 保留已加载模型）
    python tts_client.py history                                 # 生成历史
    python tts_client.py selftest                                # 只读自检（不合成、不烧 GPU）

synth 阻塞到生成结束；stdout 输出一行 JSON，进度信息走 stderr。

配置（全部走环境变量，无硬编码路径）：
    INDEXTTS_URL           服务地址，默认 http://127.0.0.1:7860
    INDEXTTS_PROJECT_DIR   项目根（含 server.py）；仅 ensure 需要，不设则自动探测
    INDEXTTS_OUT_DIR       agent 默认输出目录，默认 <项目根>/outputs/agent_tts
    INDEXTTS_START_TIMEOUT ensure 探活上限秒数，默认 120
    INDEXTTS_HTTP_TIMEOUT  单次请求超时秒数，默认 900

三条硬规则（脚本强制）：
1. **生成前必须确认音色**：synth 未传 --preset 也未传 --voice 时，打印预设/示例
   候选清单并以**退出码 2** 中止——agent 必须停下来向用户询问用哪个预设，
   不允许自行默默挑一个。
2. **禁止并行生成**：synth 通过全局文件锁串行化（默认排队等待，--lock-wait 秒；
   传 0 则抢不到锁立即失败）。服务端本身也是单锁+队列，这里是双保险。
3. **音频可找回**：不传 --out 时默认落到 <输出目录>/<日期>/，并记录到 last.json；
   `last` 子命令随时找回最近一次生成的音频。

退出码：0=成功；1=失败；2=需要先向用户确认预设/音色；3=需要用户提供项目位置（locate）。
"""
import argparse
import hashlib
import io
import json
import mimetypes
import os
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# skill 自身版本。用户报问题时先问这个——skill 有两份副本（用户级/项目内），
# 没有版本号就只能靠"文件时间"猜，也没法判断两份是否漂移。
SKILL_VERSION = "1.2.4"


def _relax_stdio():
    """把**被重定向**的 stdout/stderr 固定成 UTF-8。

    为什么必须做：Windows 上 stdout 一旦被管道/文件捕获，用的就不是控制台而是
    ANSI 代码页（中文系统 cp936、英文系统 cp1252）。脚本要打印中文路径、中文
    提示和 JSON，只要有一个字符该代码页编不了，就抛 UnicodeEncodeError——
    典型场景是合成**已经成功、音频也落盘了**，最后打印那一步把进程弄崩，
    用户看不到结果还以为没生成。

    交互式控制台不碰：Python 3.6+ 走 Unicode 控制台 API，本来就不会崩，
    强行改成 UTF-8 反而会让中文控制台显示乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass        # 老 Python / 被替换过的流：保持原样，不能因此启动失败


_relax_stdio()

# ---- 配置（环境变量优先，无任何硬编码路径）------------------------------------
# 所有配置都可通过环境变量覆盖，本脚本不假设部署位置：
#   INDEXTTS_URL          服务地址，默认 http://127.0.0.1:7860
#                         —— 连远程/别的端口/反向代理时只需改这一个
#   INDEXTTS_PROJECT_DIR  项目根目录（含 server.py）。仅 ensure 子命令需要；
#                         不设则自动从脚本位置与当前工作目录向上探测。
#                         纯远程部署不必设，ensure 会明确报错而不是乱猜。
#   INDEXTTS_OUT_DIR      agent 默认输出目录，默认 <项目根>/outputs/agent_tts，
#                         探测不到项目根时退到 ~/.indextts2-tts/agent_tts
#   INDEXTTS_START_TIMEOUT ensure 拉起服务后的探活上限（秒），默认 120
#   INDEXTTS_HTTP_TIMEOUT  单次请求 socket 超时（秒），默认 900
def _env(name, default=None):
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    return v.strip()


def _truthy(v):
    return bool(v) and str(v).strip().lower() not in ("0", "false", "no", "off")


# 机器模式：被别的 Skill / 脚本调用时置 INDEXTTS_HEADLESS=1。
# 差别只在"需要问人"的地方——此时 stdout 会给出结构化 JSON（need/options），
# 上游靠退出码 + JSON 决定下一步，不必去解析人类提示文案。
HEADLESS = _truthy(_env("INDEXTTS_HEADLESS"))


# ---- 项目根定位 ---------------------------------------------------------------
# 越靠前越可信：
#   1. INDEXTTS_PROJECT_DIR 环境变量 —— 用户/agent 显式指定
#   2. 从脚本位置向上          —— skill 放在项目内的分发方式（脚本第 3 层就是包根）
#   3. 从 cwd 向上             —— 用户把包目录当工作区打开
#   4. ~/.indextts2-tts/project.json —— 用户之前用 `locate --set` 确认过的路径
#   5. 从本地监听端口反查进程  —— 服务已在跑时最准（用户双击了 start_server.bat）
# 全部失败返回 None：只有 ensure 不可用，其余子命令照常走 HTTP。
# 配置目录可用 INDEXTTS_CONFIG_DIR 改走别处：测试要隔离，多套环境（公司/家里）
# 也不想互相覆盖记住的项目路径。
CONFIG_DIR = Path(_env("INDEXTTS_CONFIG_DIR") or (Path.home() / ".indextts2-tts")).expanduser()
PROJECT_POINTER = CONFIG_DIR / "project.json"


def _is_project_dir(d):
    """项目根判据：同时有 server.py 与 indextts/ 子目录。"""
    try:
        d = Path(d)
        return (d / "server.py").is_file() and (d / "indextts").is_dir()
    except OSError:
        return False


def _read_project_pointer():
    """读上次记住的项目根；目录已被移动/删除时视为失效，返回 None。"""
    try:
        data = json.loads(PROJECT_POINTER.read_text(encoding="utf-8"))
    except Exception:
        return None
    d = data.get("project_dir") if isinstance(data, dict) else None
    if not d:
        return None
    p = Path(str(d)).expanduser()
    return p if _is_project_dir(p) else None


def _write_project_pointer(d):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PROJECT_POINTER.write_text(
        json.dumps({"project_dir": str(d), "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")


def _env_port(default=7860):
    """从 INDEXTTS_URL 取端口。这个函数比 ORIGIN 更早执行，所以单独解析一次。"""
    try:
        return urllib.parse.urlsplit(_env("INDEXTTS_URL", "http://127.0.0.1:7860")).port or default
    except ValueError:
        return default


def _listen_pids(port):
    """找出正在**监听** port 的进程 PID。

    只认监听行：netstat 里 ESTABLISHED 行的本地端口也是 7860（客户端那侧），
    一起收进来会拿到浏览器/客户端的 exe，反推出来是错的目录。
    """
    pids = []
    if os.name == "nt":
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                                 text=True, timeout=8, errors="replace").stdout
        except Exception:
            return pids
        for line in out.splitlines():
            f = line.split()
            if len(f) < 5 or not f[0].upper().startswith("TCP"):
                continue
            local, foreign, pid = f[1], f[2], f[4]
            if not local.endswith(":%d" % port):
                continue
            # 监听行的对端端口是 0（0.0.0.0:0 / [::]:0）；ESTABLISHED 的对端是真实端口
            if not foreign.endswith(":0"):
                continue
            if pid.isdigit() and pid not in pids:
                pids.append(pid)
        return pids

    # POSIX：Linux 走 /proc（无需外部命令），macOS 走 lsof
    inodes = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(name, encoding="utf-8", errors="replace") as f:
                for line in f.read().splitlines()[1:]:
                    f2 = line.split()
                    if len(f2) >= 10 and f2[3] == "0A" and int(f2[1].split(":")[1], 16) == port:
                        inodes.add(f2[9])
        except (OSError, ValueError):
            continue
    if inodes:
        import glob
        for fd in glob.glob("/proc/[0-9]*/fd/*"):
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                pid = fd.split("/")[2]
                if pid not in pids:
                    pids.append(pid)
        return pids
    import shutil
    lsof = shutil.which("lsof")
    if lsof:
        try:
            out = subprocess.run([lsof, "-ti", "tcp:%d" % port, "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=8).stdout
            pids = [l.strip() for l in out.splitlines() if l.strip().isdigit()]
        except Exception:
            pass
    return pids


def _exe_of_pid(pid):
    """进程可执行文件的绝对路径。

    Windows 用 ctypes 直接调 QueryFullProcessImageNameW —— 纯标准库，不依赖
    wmic（新版 Windows 已弃用，且常被安全软件拦）或 PowerShell（启动要 0.5s+）。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return None                              # 权限不够（别的用户的进程）
            try:
                buf = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(buf))
                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    return buf.value or None
            finally:
                k32.CloseHandle(h)
        except Exception:
            return None
        return None
    try:
        return os.readlink("/proc/%d/exe" % pid)
    except OSError:
        pass
    import shutil
    lsof = shutil.which("lsof")
    if lsof:
        try:
            out = subprocess.run([lsof, "-p", str(pid), "-Fn", "-a", "-d", "txt"],
                                 capture_output=True, text=True, timeout=8).stdout
            for line in out.splitlines():
                if line.startswith("n/"):
                    return line[1:]
        except Exception:
            pass
    return None


def _detect_project_from_port(port=None):
    """服务已在跑时，从占用端口的进程反推包根。

    便携包里进程是 <包根>/runtime/python.exe，venv 里是 <根>/.venv/Scripts/python.exe，
    都只要沿 exe 所在目录向上几层就能撞到含 server.py 的那级，所以统一逐级校验。
    """
    for pid in _listen_pids(port or _env_port()):
        exe = _exe_of_pid(pid)
        if not exe:
            continue
        d = Path(exe).parent
        for _ in range(4):                 # runtime/ 或 .venv/Scripts/ 需上 1~2 层，留余量
            if _is_project_dir(d):
                return d
            if d.parent == d:
                break
            d = d.parent
    return None


def _detect_project_dir_ex():
    """定位项目根，返回 (路径, 来源说明)；找不到返回 (None, None)。"""
    cands = []
    env_dir = _env("INDEXTTS_PROJECT_DIR")
    if env_dir:
        cands.append((Path(env_dir).expanduser(), "环境变量 INDEXTTS_PROJECT_DIR"))
    for base, tag in ((Path(__file__).resolve().parent, "脚本位置向上"),
                      (Path.cwd(), "当前工作目录向上")):
        cands.extend((d, tag) for d in [base, *base.parents])
    seen = set()
    for d, tag in cands:
        key = str(d).lower() if os.name == "nt" else str(d)
        if key in seen:
            continue
        seen.add(key)
        if _is_project_dir(d):
            return d, tag

    p = _read_project_pointer()
    if p is not None:
        return p, "记忆文件 %s" % PROJECT_POINTER
    p = _detect_project_from_port()
    if p is not None:
        return p, "端口反查（服务已在运行）"
    return None, None


PROJECT_DIR, PROJECT_SOURCE = _detect_project_dir_ex()
ORIGIN = (_env("INDEXTTS_URL", "http://127.0.0.1:7860")).rstrip("/")
API = ORIGIN + "/api/v1"

# 本 skill 已适配的服务端接口（方法 + 去掉 /api/v1 前缀的路径模板）。
# 为什么要有这份清单：下面的调用全是硬编码路径，整合包一升级（比如多挂了
# longform / timeline 扩展）skill 不会知道也不会报错，只会一直用旧接口。
# `api` 子命令拿服务端 openapi.json 与这份清单对账，把"新增了但没适配"的接口
# 显式列出来——这是唯一的版本漂移信号。
ADAPTED_ENDPOINTS = {
    "GET /health",
    "GET /status",
    "GET /model",
    "POST /model/unload",
    "POST /model/config",
    "POST /model/restart",
    "GET /presets",
    "POST /presets",
    "GET /presets/{name}",
    "GET /examples",
    "POST /segments",
    "POST /tts",
    "POST /tts/{job_id}/stop",
    "POST /audio/tempo",
    "GET /history",
    "GET /glossary",
    "POST /glossary",
    "DELETE /glossary",
    "DELETE /glossary/{term}",
}

# 扩展接口前缀 -> 归属说明。这些是 WebUI 的编辑工程层（长篇台本 / 多轨时间轴），
# 不是"文字出语音"这一层，skill 不该适配，报账时单独归类，避免把噪音当缺口。
EXT_PREFIXES = (
    ("/lf/tl", "timeline 时间轴扩展"),
    ("/lf", "longform 长篇台本扩展"),
)

# /model/config 与 /model/restart 接受的键（服务端 _MODEL_CONFIG_KEYS，多传一个就 400）。
# 前 5 个是布尔开关，后 2 个是数值。生效时机完全不同，见 cmd_model_config 的提示。
MODEL_BOOL_KEYS = ("fp16", "s2mel_fp16", "w2v_fp16", "qwen_fp16", "cudnn_benchmark")
MODEL_RUNTIME_KEYS = ("diffusion_steps", "inference_cfg_rate")
# 这 4 个只在重建模型时被读取；写到已加载的实例上不会换掉权重精度。
MODEL_PRECISION_KEYS = ("fp16", "s2mel_fp16", "w2v_fp16", "qwen_fp16")

HTTP_TIMEOUT = int(_env("INDEXTTS_HTTP_TIMEOUT", 900))    # 模型懒加载 30~60s + 推理，给足
START_TIMEOUT = int(_env("INDEXTTS_START_TIMEOUT", 120))  # ensure 拉起服务后的探活上限（秒）
# 文本长度：与服务端一致。SOFT 只是经验值（超过给 warn，仍照常生成），
# HARD 是防跑飞的兜底（超了服务端直接 400）。客户端不擅自加严。
TEXT_CHARS_SOFT = 2000
TEXT_CHARS_HARD = 10000
MAX_EMO_TEXT_CHARS = 500
AUDIO_MAX_BYTES = 20 * 1024 * 1024

# 显存余量判据（仅用于 doctor 的告警，不参与任何拦截）。
# 实测基线（RTX 3060 Ti 8G）：模型加载后整卡占用约 6.7G、余 1.3G 仍能正常合成；
# 再低就会被 Windows 静默借共享显存 —— 表现为"不报错但慢十倍"，
# 用户以为卡死，实际是 OOM 的软着陆形态。所以告警线放在 1.2G，不放在 1.5G。
VRAM_WARN_LOADED_MB = 1200
# 模型加载带来的**增量**（不是加载后的整卡占用）：6.7G 总占用里约 1.4G 是原本就有的
# 桌面/浏览器占用。未加载时按增量估"加载后还剩多少"。
VRAM_MODEL_GROWTH_MB = 5500
# 未加载时是估算值，本身有误差，阈值放宽，宁可少报也不要把能用的机器报成危险。
VRAM_WARN_ESTIMATE_MB = 800
# 与服务端 _validate_audio_upload 白名单一致（服务端还会做魔数嗅探，扩展名只是初筛）
AUDIO_EXTS = (".wav", ".mp3", ".flac", ".ogg", ".m4a")

# agent 默认输出目录：<项目根>/outputs/agent_tts/<日期>/<服务端文件名>
# - 固定可预测，配合 last 子命令随时找回
# - 位于 outputs/ 子目录，服务端 _clean_stale_outputs 只扫 outputs 根目录的 wav，不会误删
# - 探测不到项目根（纯远程）时退到用户目录，不依赖项目是否存在
if _env("INDEXTTS_OUT_DIR"):
    AGENT_OUT_DIR = Path(_env("INDEXTTS_OUT_DIR")).expanduser()
elif PROJECT_DIR is not None:
    AGENT_OUT_DIR = PROJECT_DIR / "outputs" / "agent_tts"
else:
    AGENT_OUT_DIR = Path.home() / ".indextts2-tts" / "agent_tts"
SYNTH_LOCK_PATH = AGENT_OUT_DIR / ".synth.lock"   # 全局合成锁：同一时刻只允许一个 synth
LAST_OUTPUT_PATH = AGENT_OUT_DIR / "last.json"    # 最近一次生成记录（last 子命令读取）

# 8 维情感向量顺序（与 infer_v2.normalize_emo_vec 注释一致）
EMO_VEC_NAMES = ["高兴", "愤怒", "悲伤", "害怕", "厌恶", "忧郁", "惊讶", "平静"]

# 默认绕过系统代理。企业网/CI/agent 沙箱常设 HTTP_PROXY，urllib 会把发往
# 127.0.0.1 的请求也交给代理，结果拿到 502 "upstream connect failed"，
# 看起来像服务挂了。要真走代理时设 INDEXTTS_PROXY=http://host:port。
_proxy = _env("INDEXTTS_PROXY")
if _proxy:
    _OPENER = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": _proxy, "https": _proxy}))
else:
    _OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---- HTTP 基础 ---------------------------------------------------------------
def _urlopen(req, timeout):
    """urlopen 薄封装：把连接类错误转成可读的 SystemExit。

    agent 调用最常踩的坑就是"服务没起"，原始 traceback 既吵又没有行动指引。
    HTTPError 照旧抛给上层（它带状态码和 body，调用方要分别处理）。
    """
    try:
        return _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError:
        raise
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        raise SystemExit(
            "ERROR: cannot reach IndexTTS2 service at %s (%s)\n"
            "  - service not started?  run: python %s ensure\n"
            "  - different host/port?  set INDEXTTS_URL=http://host:port"
            % (ORIGIN, reason, Path(__file__).name)
        )
    except (TimeoutError, OSError) as e:
        raise SystemExit("ERROR: request to %s failed: %s" % (ORIGIN, e))


def api_get_json(path, timeout=30):
    """GET JSON；中文路径段调用方负责 quote。

    HTTP 错误统一转成 SystemExit 并带上服务端 detail，避免把 traceback 丢给 agent。
    """
    try:
        with _urlopen(urllib.request.Request(API + path, method="GET"), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit("ERROR: GET %s -> HTTP %s: %s" % (path, e.code, http_error_detail(e)))


def api_post_form(path, fields, timeout=60):
    """POST application/x-www-form-urlencoded，返回 (status, parsed_json_or_text)。"""
    data = urllib.parse.urlencode({k: v for k, v in fields.items() if v is not None}).encode("utf-8")
    req = urllib.request.Request(API + path, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with _urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body


def api_post_json(path, obj, timeout=60):
    """POST application/json，返回 (status, parsed_json_or_text)。

    `/model/config` 和 `/model/restart` 的 body 是 `req: dict = Body(...)`，
    不是表单——用 urlencoded 打过去会 422。
    """
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with _urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body


def post_multipart(path, fields, files, timeout=HTTP_TIMEOUT):
    """POST multipart/form-data，返回原始 HTTPResponse（调用方负责关闭）。

    fields: dict[str, str|number|bool]（None 跳过）
    files:  dict[str, (filename, bytes, content_type)]
    """
    boundary = uuid.uuid4().hex
    crlf = b"\r\n"
    buf = io.BytesIO()

    def _w(s):
        buf.write(s.encode("utf-8"))

    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        _w("--%s" % boundary); buf.write(crlf)
        _w('Content-Disposition: form-data; name="%s"' % name); buf.write(crlf)
        buf.write(crlf)
        _w(str(value)); buf.write(crlf)
    for name, (filename, data, ctype) in files.items():
        _w("--%s" % boundary); buf.write(crlf)
        # 文件名一律用 ASCII，避免中文文件名在 Content-Disposition 里的编码坑
        _w('Content-Disposition: form-data; name="%s"; filename="%s"' % (name, filename)); buf.write(crlf)
        _w("Content-Type: %s" % ctype); buf.write(crlf)
        buf.write(crlf)
        buf.write(data); buf.write(crlf)
    _w("--%s--" % boundary); buf.write(crlf)

    req = urllib.request.Request(API + path, data=buf.getvalue(), method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=%s" % boundary)
    return _urlopen(req, timeout=timeout)


def iter_sse(resp):
    """逐行解析 SSE：yield 每个 data: 负载的 dict，done/error 由调用方终止。"""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload:
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def http_error_detail(e):
    try:
        body = e.read().decode("utf-8", "replace")
        try:
            return json.loads(body).get("detail") or body
        except json.JSONDecodeError:
            return body
    except Exception:
        return str(e)


def _oom_hint(detail):
    """识别显存不足，返回可直接照做的排查步骤；不是 OOM 就返回空串。

    为什么要单独翻译：服务端只把原始的 `CUDA out of memory. Tried to allocate
    2.5 GiB...` 丢过来，用户看到这句话不知道下一步做什么。而在 8G 卡上这是
    最高频的失败，且它的"软"形态根本不报错（借共享显存 → 慢十倍）。
    """
    low = (detail or "").lower()
    if not any(k in low for k in ("out of memory", "cuda oom", "cublas_status_alloc_failed",
                                  "cuda error", "not enough memory")):
        return ""
    return (
        "\n  这是显存不足，不是模型坏了。按顺序试："
        "\n    1) python tts_client.py status      # 看 vram_used_mb / vram_total_mb 与余量"
        "\n    2) 关掉占显存的程序：浏览器硬件加速、游戏、另一个 TTS 实例"
        "\n    3) 模型已加载时释放：POST /model/unload（前端「模型管理」页也有按钮）"
        "\n    4) 文本切短：python tts_client.py segments --text-file <稿子.txt>，再分批跑"
        "\n    5) 仍不行就降 diffusion_steps（25 -> 20/15），或在服务端开 w2v_fp16（省约 1G）"
    )


def download(url, out_path):
    if url.startswith("/"):
        url = ORIGIN + url
    # 输出文件名默认取文本前 15 字，常含中文；urllib 不会自动编码请求行，
    # 必须手动 percent-encode path，否则 UnicodeEncodeError。
    # 注意只编码"原始"中文路径——预设音频 URL 已是 %E8%83%A1... 形式，
    # 再 quote 会把 % 编码成 %25 造成双重编码 404。
    parts = urllib.parse.urlsplit(url)
    path = parts.path
    if not path.isascii():
        path = urllib.parse.quote(path)
    url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = None
    with _urlopen(urllib.request.Request(url, method="GET"), timeout=120) as r:
        data = r.read()
    try:
        out_path.write_bytes(data)
    except OSError as e:
        # 磁盘满是这里最隐蔽的失败：合成已经成功了（GPU 白跑），却卡在落盘上，
        # 原始异常是 "No space left on device" / errno 28，不说人话。
        if getattr(e, "errno", None) == 28 or "no space" in str(e).lower():
            raise SystemExit("ERROR: 磁盘已满，写不进 %s（合成成功但落盘失败）。\n"
                             "  清掉旧产物或换输出目录：--outdir <别的盘>\n"
                             "  agent 默认目录会一直累积，见 `last` 查看历史产物"
                             % out_path)
        raise SystemExit("ERROR: 写文件失败 %s: %s" % (out_path, e))
    return out_path


# ---- 合成核心（synth / batch / doctor 共用）------------------------------------
def post_tts(params, files, verbose=True):
    """POST /tts 并读完 SSE，返回 done 事件（附带 `_job_id` / `_wall`）。

    抽出来的理由：`synth`、`batch`、`doctor` 三处都要跑同一套「POST + 逐事件读流」，
    各写一遍早晚会在事件处理上漂移（比如只在一处处理 `stopped`）。
    进度/告警一律走 stderr，stdout 留给调用方打结构化结果。
    """
    t0 = time.time()
    try:
        resp = post_multipart("/tts", params, files, timeout=HTTP_TIMEOUT)
    except urllib.error.HTTPError as e:
        detail = http_error_detail(e)
        raise SystemExit("ERROR: /tts rejected: %s%s" % (detail, _oom_hint(detail)))

    job_id = None
    result = None
    with resp:
        for ev in iter_sse(resp):
            t = ev.get("type")
            if t == "started":
                job_id = ev.get("job_id")
            elif t == "progress":
                if verbose:
                    print("[progress] %3.0f%% %s" % (float(ev.get("value") or 0) * 100, ev.get("desc") or ""),
                          file=sys.stderr)
            elif t == "queue":
                if verbose:
                    print("[queue] %s job(s) ahead of this one; server generates serially."
                          % ev.get("ahead", 0), file=sys.stderr)
            elif t == "warn":
                print("[warn] %s" % (ev.get("detail") or ""), file=sys.stderr)
            elif t == "stopping":
                print("[stopping] %s" % (ev.get("detail") or ""), file=sys.stderr)
            elif t == "stopped":
                raise SystemExit("ERROR: generation stopped: %s"
                                 % (ev.get("detail") or "stopped by user"))
            elif t == "done":
                result = ev
                break
            elif t == "error":
                detail = ev.get("detail") or "unknown"
                raise SystemExit("ERROR: generation failed: %s%s" % (detail, _oom_hint(detail)))
    if result is None:
        raise SystemExit("ERROR: SSE stream ended without a done/error event")
    result["_job_id"] = job_id
    result["_wall"] = round(time.time() - t0, 2)
    return result


def prepare_tts_request(ns, text, tmpdir):
    """把命令行参数命名空间组装成 /tts 请求，返回 (params, files, voice_ref, emo_mode)。

    `ns` 需要有与 `synth` 子命令同名同默认值的属性；`batch` 复用同一个 parser 造出来的
    默认命名空间再覆盖，所以两边字段永远不会漂移。
    """
    params = {}
    preset_spk = preset_emo = None
    if getattr(ns, "preset", None):
        d = preset_detail(ns.preset)
        params = preset_to_tts_params(d)
        preset_spk, preset_emo = download_preset_audio(d, tmpdir)
        print("[synth] preset '%s' loaded (emo_mode=%s)." % (ns.preset, params["emo_mode"]), file=sys.stderr)

    voice_path = resolve_voice(ns.voice) if getattr(ns, "voice", None) else (Path(preset_spk) if preset_spk else None)
    if voice_path is None:
        raise SystemExit("ERROR: no reference audio. Pass --voice <wav> or --preset <name> "
                         "(preset must contain prompt.wav).")
    emo_audio_path = resolve_voice(ns.emo_audio) if getattr(ns, "emo_audio", None) else (Path(preset_emo) if preset_emo else None)

    emo_mode = ns.emo_mode if ns.emo_mode is not None else params.get("emo_mode", 0)
    params["emo_mode"] = emo_mode
    if ns.emo_weight is not None:
        params["emo_weight"] = ns.emo_weight
    if ns.emo_text is not None:
        params["emo_text"] = ns.emo_text
    if ns.use_random:
        params["use_random"] = True

    # 情感参数与 emo_mode 不匹配时明确告警：服务端会静默忽略（模式互斥），
    # 不提示的话用户会以为"设了向量怎么没效果"。
    if ns.emo_vector and emo_mode != 2:
        print("[warn] --emo-vector ignored: emo_mode=%s (vector is only used when emo_mode=2; "
              "add --emo-mode 2)." % emo_mode, file=sys.stderr)
    if ns.emo_text and emo_mode != 3:
        print("[warn] --emo-text ignored: emo_mode=%s (emotion text is only used when emo_mode=3; "
              "add --emo-mode 3)." % emo_mode, file=sys.stderr)
    if ns.emo_audio and emo_mode != 1:
        print("[warn] --emo-audio ignored: emo_mode=%s (emotion audio is only used when emo_mode=1; "
              "add --emo-mode 1)." % emo_mode, file=sys.stderr)

    if emo_mode == 2:
        vec = parse_emo_vector(ns.emo_vector) if ns.emo_vector else \
            [params.get("vec%d" % i, 0.0) for i in range(1, 9)]
        for i, v in enumerate(vec, start=1):
            params["vec%d" % i] = v
    if emo_mode == 3 and not (params.get("emo_text") or "").strip():
        raise SystemExit("ERROR: emo_mode=3 requires --emo-text (or a preset with emo_text)")
    if emo_mode == 1 and emo_audio_path is None:
        raise SystemExit("ERROR: emo_mode=1 requires an emotion reference audio "
                         "(--emo-audio, or a preset containing emo_ref.wav)")
    emo_text = params.get("emo_text") or ""
    if emo_text and len(emo_text) > MAX_EMO_TEXT_CHARS:
        raise SystemExit("ERROR: emo_text too long (%d > %d)" % (len(emo_text), MAX_EMO_TEXT_CHARS))

    cli_overrides = {
        "max_text_tokens_per_segment": ns.seg_tokens,
        "do_sample": (None if ns.no_sample else (True if ns.sample else None)),
        "top_p": ns.top_p, "top_k": ns.top_k, "temperature": ns.temperature,
        "length_penalty": ns.length_penalty, "num_beams": ns.num_beams,
        "repetition_penalty": ns.repetition_penalty, "max_mel_tokens": ns.max_mel_tokens,
        "interval_silence": ns.interval_silence,
    }
    for k, v in cli_overrides.items():
        if v is not None:
            params[k] = v

    # 服务端会把 interval_silence 静默夹到 [10,500]；客户端提前告知，
    # 免得用户以为设了 1000ms 却没效果。
    if ns.interval_silence is not None:
        clamped = max(10, min(500, ns.interval_silence))
        if clamped != ns.interval_silence:
            print("[warn] --interval-silence %d out of range, server clamps to %d ms"
                  % (ns.interval_silence, clamped), file=sys.stderr)
            params["interval_silence"] = clamped

    params["text"] = text
    params["client_id"] = ns.client_id
    params["file_naming"] = ns.naming

    files = {"spk_audio": ("spk.wav", read_audio(voice_path, "spk_audio"), "audio/wav")}
    if emo_mode == 1 and emo_audio_path is not None:
        files["emo_audio"] = ("emo.wav", read_audio(emo_audio_path, "emo_audio"), "audio/wav")

    voice_ref = ("preset:%s" % ns.preset) if getattr(ns, "preset", None) else str(voice_path)
    return params, files, voice_ref, emo_mode


# ---- 全局合成锁（防并行生成）---------------------------------------------------
def _lock_fd_nb(fd):
    """对 fd 的第 0 字节加非阻塞排他锁；被占用时抛 OSError。"""
    if os.name == "nt":
        import msvcrt
        fd.seek(0, os.SEEK_END)
        if fd.tell() == 0:                      # msvcrt 锁区必须存在，空文件先写 1 字节占位
            fd.write(b"\x00")
            fd.flush()
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class SynthLock:
    """跨进程合成锁：保证同一时刻只有一个 synth 请求发往服务端。

    服务端本身有全局单锁+队列（并发请求会排队等），客户端再加一把锁是为了：
    1. 明确告知调用方"已有另一个生成任务在进行"，避免多条命令同时挂起苦等；
    2. 串行化参考音频下载与 wav 落盘，减少磁盘/连接抖动。
    默认排队等待 wait_seconds 秒；传 0 则抢不到锁立即失败。
    锁通过文件字节锁实现，进程退出（含崩溃）自动释放，不会死锁。
    """

    def __init__(self, path, wait_seconds=3600):
        self.path = Path(path)
        self.wait_seconds = max(0, int(wait_seconds))
        self._fd = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(self.path, "a+b")
        deadline = time.time() + self.wait_seconds
        announced = False
        waited = 0.0
        next_notice = 15.0
        while True:
            try:
                _lock_fd_nb(fd)
                fd.seek(0)
                fd.truncate()
                fd.write(("pid=%d since=%s\n" % (os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S"))).encode("utf-8"))
                fd.flush()
                self._fd = fd
                return True
            except OSError:
                pass
            if self.wait_seconds <= 0:
                fd.close()
                return False
            if not announced:
                print("[lock] another synth is in progress; waiting up to %ds..." % self.wait_seconds,
                      file=sys.stderr)
                announced = True
            if time.time() >= deadline:
                fd.close()
                return False
            if waited >= next_notice:
                print("[lock] still waiting (%.0fs elapsed)..." % waited, file=sys.stderr)
                next_notice += 30.0
            time.sleep(2)
            waited += 2

    def release(self):
        if self._fd is not None:
            try:
                self._fd.close()     # 关闭 fd 即释放字节锁
            finally:
                self._fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


# ---- 预设摘要（供"生成前向用户确认用哪个预设"展示）-----------------------------
def _disp_width(s):
    """终端显示宽度：中文/全角字符按 2 列计。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s, width):
    return s + " " * max(1, width - _disp_width(s))


def _preset_details(names):
    """并发拉取预设详情。

    /presets 只给名字，摘要需要逐个 GET /presets/{name}（N+1 请求）。串行在
    本机看不出差别，但连远程/反向代理时要按 N × 往返时间累加，列表直接卡住。
    这里并发 8 路，失败项记 None，由展示层降级成 (detail unavailable)。
    """
    if not names:
        return {}
    from concurrent.futures import ThreadPoolExecutor

    def one(n):
        try:
            return n, preset_detail(n)
        except Exception:
            return n, None

    out = {}
    with ThreadPoolExecutor(max_workers=min(8, len(names))) as ex:
        for n, d in ex.map(one, names):
            out[n] = d
    return out


def _preset_summary_lines(names, numbered=False):
    if not names:
        return ["(no presets yet; create one in the WebUI or POST /api/v1/presets)"]
    lines = ["可用预设（%d 个）：" % len(names)]
    details = _preset_details(names)
    for i, n in enumerate(names, start=1):
        prefix = "%2d) " % i if numbered else "  - "
        d = details.get(n)
        if d is None:
            lines.append("%s%s (detail unavailable)" % (prefix, _pad(n, 18)))
            continue
        method = int(d.get("emo_control_method", 0) or 0)
        has_prompt = "yes" if d.get("prompt_audio_url") else "NO"
        desc = ""
        if method == 2:
            vec = d.get("emo_vector") or []
            if len(vec) == 8:
                desc = "vec=" + ",".join("%.1f" % float(v) for v in vec)
        elif method == 3:
            desc = (d.get("emo_text") or "").strip()[:30]
        lines.append("%s%s emo_mode=%s prompt=%s %s"
                     % (prefix, _pad(n, 18), method, has_prompt, desc))
    return lines


def _read_last_output():
    try:
        return json.loads(LAST_OUTPUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_last_output(rec):
    try:
        AGENT_OUT_DIR.mkdir(parents=True, exist_ok=True)
        LAST_OUTPUT_PATH.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except OSError as e:
        print("[warn] failed to record last output: %s" % e, file=sys.stderr)
        return False


def _open_in_file_manager(path):
    """在系统文件管理器中打开目录（Windows 资源管理器/macOS Finder/Linux）。"""
    try:
        if os.name == "nt":
            os.startfile(str(path))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
        return True
    except Exception as e:
        print("[warn] failed to open %s: %s" % (path, e), file=sys.stderr)
        return False


# ---- 服务生命周期 -------------------------------------------------------------
def health_probe():
    """探活并带回失败原因：返回 (dict_or_None, reason, detail)。

    以前只返回 None，`health` 命令就只能吐 `{"ok": false, "health": null}`，
    分不清"服务没起 / 端口被别的程序占 / 服务还在加载模型 / 被代理拦了"这四种
    最常见的首诊场景——而这四种的处理办法完全不同。这里把异常分类带出来。

    reason 取值：ok / connection_refused / timeout / http_error / dns_error / unknown
    """
    try:
        with _OPENER.open(urllib.request.Request(ORIGIN + "/api/v1/health", method="GET"),
                          timeout=5) as r:
            return json.loads(r.read().decode("utf-8")), "ok", ""
    except urllib.error.HTTPError as e:
        # 端口有人应答但不是 /api/v1/health —— 多半是被别的程序占了。
        # detail 压成单行并截断：占了端口的常常是别的 Web 服务，抛回来一整段
        # HTML，agent 把它读进上下文既浪费又干扰判断。
        d = " ".join(str(http_error_detail(e)).split())
        if len(d) > 160:
            d = d[:160] + "…"
        return None, "http_error", "HTTP %s: %s" % (e.code, d)
    except urllib.error.URLError as e:
        rc = e.reason
        if isinstance(rc, ConnectionRefusedError) or "refused" in str(rc).lower():
            return None, "connection_refused", str(rc)
        if isinstance(rc, (TimeoutError, socket.timeout)) or "timed out" in str(rc).lower():
            return None, "timeout", str(rc)
        if isinstance(rc, socket.gaierror):
            return None, "dns_error", str(rc)
        # 代理把 127.0.0.1 也劫走了会落到这里
        return None, "unknown", str(rc)
    except (TimeoutError, socket.timeout) as e:
        return None, "timeout", str(e)
    except Exception as e:                      # noqa: BLE001 —— 探活不该抛
        return None, "unknown", "%s: %s" % (type(e).__name__, e)


def health():
    """探活：返回 dict 或 None（None 表示连不上）。故意不走 _urlopen——
    这里"连不上"是正常分支，不能被转成 SystemExit。"""
    h, _reason, _detail = health_probe()
    return h


def _health_hints(reason):
    """按失败原因给可执行的下一步，而不是一句"连不上"。"""
    if reason == "connection_refused":
        return [
            "服务没启动：双击整合包根目录的 start_server.bat，或跑 `ensure`（需要项目根）",
            "端口不对：确认 INDEXTTS_URL，默认 http://127.0.0.1:7860",
            "起在别的机器：INDEXTTS_URL 指过去，别用 127.0.0.1",
        ]
    if reason == "timeout":
        return [
            "服务正在加载模型（首次 30~60s）：等一下再探，或调大 INDEXTTS_HTTP_TIMEOUT",
            "机器卡住/显存换页：看任务管理器 GPU、内存占用",
        ]
    if reason == "http_error":
        return [
            "端口被别的程序占用：换个端口，或先停掉占用 7860 的进程",
            "确认 URL 指向的是 IndexTTS2，而不是同端口的其它 Web 服务",
        ]
    if reason == "dns_error":
        return ["主机名解析失败：改用 IP，或检查网络/代理设置"]
    return [
        "检查是否有 HTTP_PROXY/HTTPS_PROXY 拦了 127.0.0.1（脚本默认绕过，设了 INDEXTTS_PROXY 才会走）",
        "跑 `doctor` 看完整体检，或确认 INDEXTTS_URL 拼写",
    ]


def cmd_health(args):
    h, reason, detail = health_probe()
    out = {"ok": h is not None, "origin": ORIGIN, "health": h}
    if h is None:
        out["reason"] = reason
        out["detail"] = detail
        out["hints"] = _health_hints(reason)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if h is not None else 1


def _origin_port():
    """从 ORIGIN 提取端口；取不到就用 7860。"""
    try:
        return urllib.parse.urlsplit(ORIGIN).port or 7860
    except ValueError:
        return 7860


def _server_cmd(port):
    """构造启动命令：便携包 runtime → uv → 项目 .venv → 当前解释器。

    不依赖项目自带的 start_server.bat / .sh —— 那些脚本带有本机路径假设，
    换个部署就不成立。这里按依赖最可能装在哪的顺序探测：
      1. <项目根>/runtime/ 里的解释器 —— **便携分发包（整合包）自带的那份**
         必须排第一：这类包的依赖全装在 runtime/ 里，既没有 .venv 也没有 uv
         项目环境。若让 uv 抢在前面，它会往用户包里新建一个空 .venv 再报
         ModuleNotFoundError，等于把包搞脏还启动不了。
      2. uv（开发机上用 uv 管理依赖）
      3. <项目根>/.venv 里的解释器（很多人用 venv 装依赖）
      4. 当前解释器（最后兜底；若缺 torch 会在 server 日志里看到 ImportError）

    注意 uv 这一路带 `--no-sync`：故意不在启动时联网装依赖（会卡住几十秒甚至
    因镜像失败而超时）。代价是它**不会**自动补齐缺失的 server extra ——
    真缺依赖时日志里是 ModuleNotFoundError，ensure 会把日志尾部打出来并提示
    `uv sync --extra server`。
    """
    import shutil
    for rel in ("runtime/python.exe", "runtime/bin/python"):
        cand = PROJECT_DIR / rel
        if cand.is_file():
            return [str(cand), "server.py", "--port", str(port)]
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "--extra", "server", "--no-sync", "python", "server.py", "--port", str(port)]
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        cand = PROJECT_DIR / rel
        if cand.is_file():
            return [str(cand), "server.py", "--port", str(port)]
    return [sys.executable, "server.py", "--port", str(port)]


def _tail_lines(path, n=20):
    """取日志文件末尾 n 行（只读最后 8KB，避免整文件读入）。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            text = f.read().decode("utf-8", "replace")
        lines = [l for l in text.splitlines() if l.strip()]
        return lines[-n:]
    except OSError:
        return []


LOCATE_HELP = """\
定位 IndexTTS2 项目目录：没找到（需要同时含 server.py 与 indextts/ 的文件夹）。
skill 本身只是调用客户端，不含模型与代码，所以必须先确认项目在哪。

请让用户在下面三条里选一条，不要自己猜路径：

  ① 用户手上有整合包 —— 直接把路径发过来
       把整合包根目录贴出来（例如 D:\\IndexTTS2.0_v3），然后执行：
         python tts_client.py locate --set "D:\\IndexTTS2.0_v3"
       校验通过后路径会被记住（~/.indextts2-tts/project.json），以后不用再给。

  ② 用户不想翻目录 —— 先把服务启动，我自动定位
       让用户双击整合包根目录的 start_server.bat，
       等浏览器能打开 http://127.0.0.1:7860 之后，执行：
         python tts_client.py locate
       我会从占用该端口的进程反推出包根。

  ③ 用户根本没有整合包 —— 只装 skill 是跑不出声音的
       去 B 站找 UP 主「十一君Jun」，主页 https://space.bilibili.com/470166715 ，
       在投稿里找「Index TTS 2.0」相关的视频，按视频说明获取整合包。
       拿到包、解压、双击 start_server.bat 跑通之后，再回到 ① 或 ②。

  补充：health / presets / synth 只走 HTTP，不依赖项目根；
        只有 ensure（自动拉起服务）和「服务端文件路径」相关能力需要它。
"""


def ensure_server(allow_start=True):
    """返回 True 表示服务可用；不在跑且 allow_start 时后台拉起并探活。"""
    h = health()
    if h is not None:
        return True
    if not allow_start:
        return False
    if PROJECT_DIR is None:
        print("[ensure] 服务没在跑（%s），而且没找到项目目录，无法自动拉起。\n" % ORIGIN
              + LOCATE_HELP, file=sys.stderr)
        return False

    port = _origin_port()
    log_path = PROJECT_DIR / "logs" / "agent_tts_client.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logf = open(log_path, "ab")
    except OSError as e:
        print("[ensure] ERROR: cannot open log file %s: %s" % (log_path, e), file=sys.stderr)
        return False

    cmd = _server_cmd(port)
    print("[ensure] server not running, starting: %s" % " ".join(cmd), file=sys.stderr)
    kwargs = dict(cwd=str(PROJECT_DIR), stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, close_fds=True)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True          # 与终端解耦，ssh 断开也不被带走
    try:
        subprocess.Popen(cmd, **kwargs)
    except OSError as e:
        print("[ensure] ERROR: failed to spawn server: %s" % e, file=sys.stderr)
        return False
    finally:
        logf.close()
    print("[ensure] spawned, waiting for listen (log: %s)..." % log_path, file=sys.stderr)

    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        time.sleep(2)
        if health() is not None:
            print("[ensure] server is up.", file=sys.stderr)
            return True
    tail = _tail_lines(log_path)
    print("[ensure] ERROR: server did not come up within %ds (log: %s)" % (START_TIMEOUT, log_path), file=sys.stderr)
    if tail:
        print("[ensure] last log lines:", file=sys.stderr)
        for line in tail:
            print("  | %s" % line, file=sys.stderr)
    if any(("ModuleNotFoundError" in l) or ("ImportError" in l) for l in tail):
        print("[ensure] looks like missing server deps. `uv run --no-sync` never installs;\n"
              "  fix: uv sync --extra server   (or pip install fastapi uvicorn psutil python-multipart)",
              file=sys.stderr)
        if (PROJECT_DIR / "runtime").is_dir():
            print("[ensure] 便携整合包：runtime\\ 里的依赖不完整（解压中断、被安全软件删过，或包被改动）。\n"
                  "  正确做法是重新解压一份干净的包；不要用 pip 往 runtime\\ 里装或升级任何东西。",
                  file=sys.stderr)
    return False


# ---- 预设 ---------------------------------------------------------------------
def preset_detail(name):
    """返回预设详情 dict；音频字段附带可下载的绝对 URL。"""
    q = urllib.parse.quote(name, safe="")
    d = api_get_json("/presets/%s" % q)
    for key in ("prompt_audio_url", "emo_audio_url"):
        if d.get(key) and d[key].startswith("/"):
            d[key] = ORIGIN + d[key]
    return d


def preset_to_tts_params(d):
    """preset.json -> /tts form 字段（与前端 applyPresetToForm 同一套映射）。"""
    params = {
        "emo_mode": int(d.get("emo_control_method", 0) or 0),
        "emo_weight": d.get("emo_weight", 0.65),
        "emo_text": d.get("emo_text") or "",
        "use_random": bool(d.get("emo_random", False)),
    }
    vec = d.get("emo_vector") or []
    if len(vec) == 8:
        for i, v in enumerate(vec, start=1):
            params["vec%d" % i] = v
    adv = d.get("advanced_params") or {}
    for k in ("do_sample", "top_p", "top_k", "temperature", "length_penalty",
              "num_beams", "repetition_penalty", "max_mel_tokens", "max_text_tokens_per_segment"):
        if adv.get(k) is not None:
            params[k] = adv[k]
    return params


def download_preset_audio(d, tmpdir):
    """把预设自带的参考音频下载到临时目录，返回 (spk_path, emo_path)。"""
    spk = emo = None
    if d.get("prompt_audio_url"):
        spk = str(download(d["prompt_audio_url"], Path(tmpdir) / "preset_prompt.wav"))
    if d.get("emo_audio_url"):
        emo = str(download(d["emo_audio_url"], Path(tmpdir) / "preset_emo_ref.wav"))
    return spk, emo


# ---- 音色参考音频解析 -----------------------------------------------------------
_EXAMPLE_NAMES_CACHE = None


def _example_voice_names():
    """内置示例音色的文件名集合（如 {'voice_01.wav', ...}）。

    注意 /examples 返回的 prompt_audio 是**服务端 URL**（/examples/voice_01.wav），
    不是本地路径——纯远程部署时本地根本没有这个文件，所以这里缓存一份名单，
    供 resolve_voice 在本地查不到时回落到"从服务端下载"。
    """
    global _EXAMPLE_NAMES_CACHE
    if _EXAMPLE_NAMES_CACHE is not None:
        return _EXAMPLE_NAMES_CACHE
    names = set()
    try:
        for e in api_get_json("/examples", timeout=15).get("examples", []):
            p = e.get("prompt_audio") or ""
            if p:
                names.add(Path(p.replace("\\", "/")).name)
    except SystemExit:
        names = set()          # 服务不可达/接口异常：当作"没有示例"，走原有报错
    _EXAMPLE_NAMES_CACHE = names
    return names


def fetch_example_voice(name):
    """下载内置示例音色到本地缓存目录，返回本地 Path。

    内容不变（示例音频随仓库发布），所以缓存命中直接复用，不重复下载。
    """
    base = Path(name.replace("\\", "/")).name
    dest = AGENT_OUT_DIR / ".voices" / base
    try:
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
    except OSError:
        pass
    dest = Path(download("/examples/%s" % urllib.parse.quote(base), dest))
    print("[voice] fetched built-in example '%s' -> %s" % (base, dest), file=sys.stderr)
    return dest


def resolve_voice(spec):
    """--voice 取值：本地路径（绝对/相对 cwd）、相对项目根/examples/prompts 的名称，
    或内置示例音色名（本地没有时从服务端下载）。

    最后这条是通用性关键：examples 列表里打印的是 /examples/voice_01.wav 这种
    服务端 URL，agent 会原样传进来；纯远程部署时本地不存在，
    不做回落就只能"看得见、用不了"。
    """
    cands = [Path(spec)]
    if not Path(spec).is_absolute():
        cands.append(Path.cwd() / spec)
        if PROJECT_DIR is not None:
            cands += [PROJECT_DIR / spec,
                      PROJECT_DIR / "examples" / Path(spec).name,
                      PROJECT_DIR / "prompts" / Path(spec).name]
    for p in cands:
        try:
            if p.is_file():
                return p.resolve()
        except OSError:
            continue

    base = Path(spec.replace("\\", "/")).name
    if base.lower().endswith(AUDIO_EXTS) and base in _example_voice_names():
        try:
            return fetch_example_voice(base).resolve()
        except SystemExit:
            pass               # 下载失败则落回下面的统一报错，信息更完整
        except Exception as e:
            print("[voice] failed to fetch example '%s': %s" % (base, e), file=sys.stderr)

    raise SystemExit(
        "ERROR: reference audio not found: %s\n"
        "  tried local: %s\n"
        "  built-in examples: %s\n"
        "  (pass an absolute path, or run `examples` to see built-in names)"
        % (spec, ", ".join(str(c) for c in cands), ", ".join(sorted(_example_voice_names())) or "unavailable"))


def read_text_file(path):
    """读 UTF-8 文本文件；utf-8-sig 顺带吃掉 Windows 记事本可能写入的 BOM。"""
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise SystemExit("ERROR: %s is not valid UTF-8 (%s); re-save it as UTF-8" % (path, e))
    except OSError as e:
        raise SystemExit("ERROR: cannot read text file %s: %s" % (path, e))


def read_audio(path, label):
    data = Path(path).read_bytes()
    if not data:
        raise SystemExit("ERROR: %s is empty: %s" % (label, path))
    if len(data) > AUDIO_MAX_BYTES:
        raise SystemExit("ERROR: %s too large (%.1f MB > %d MB)" % (label, len(data) / 1048576, AUDIO_MAX_BYTES // 1048576))
    return data


def wav_info(path):
    """读 wav 头拿时长/采样率/声道。

    视频配音场景必须知道每句多长才能对齐时间轴，而服务端 done 事件只给 file/elapsed，
    不含 duration，所以客户端自己量。非标准 wav（WAVE_FORMAT_EXTENSIBLE 等）读不出时
    返回 None 而不是抛异常——时长只是附加信息，不该让整条流程失败。
    """
    try:
        import wave
        with wave.open(str(path), "rb") as w:
            sr = w.getframerate()
            frames = w.getnframes()
            return {
                "duration": round(frames / float(sr), 3) if sr else None,
                "sample_rate": sr,
                "channels": w.getnchannels(),
            }
    except Exception:
        return None


def parse_emo_vector(s):
    s = s.strip().strip("[]")
    parts = [p.strip() for p in s.split(",") if p.strip()]
    try:
        vec = [float(p) for p in parts]
    except ValueError:
        raise SystemExit("ERROR: --emo-vector must be 8 numbers, got: %s" % s)
    if len(vec) != 8:
        raise SystemExit("ERROR: --emo-vector must contain exactly 8 values (%s), got %d"
                         % (", ".join(EMO_VEC_NAMES), len(vec)))
    for v in vec:
        if not 0.0 <= v <= 1.0:
            raise SystemExit("ERROR: --emo-vector values must be in 0..1, got %s" % vec)
    if sum(vec) > 0.8:
        print("[warn] vector sum %.2f > 0.8; server will auto-scale it down (normalize_emo_vec)." % sum(vec),
              file=sys.stderr)
    return vec


# ---- 子命令 ---------------------------------------------------------------------
def cmd_locate(args):
    """定位项目根：自动探测 / 手动指定并记住 / 三条路引导（退出码 3）。"""
    if getattr(args, "forget", False):
        try:
            PROJECT_POINTER.unlink()
            print(json.dumps({"ok": True, "forgotten": str(PROJECT_POINTER)}, ensure_ascii=False))
        except OSError:
            print(json.dumps({"ok": True, "forgotten": None}, ensure_ascii=False))
        return 0

    if getattr(args, "set_path", None):
        p = Path(args.set_path).expanduser()
        if not _is_project_dir(p):
            print("[locate] 这不是 IndexTTS2 项目根：%s" % p, file=sys.stderr)
            print("  判断条件：该目录里必须同时有 server.py 和 indextts\\ 子目录。", file=sys.stderr)
            print("  整合包解压后的根目录长这样（认准 server.py 那一层，别选进 runtime\\）：",
                  file=sys.stderr)
            print("    IndexTTS2.0_v3\\", file=sys.stderr)
            print("      ├ server.py", file=sys.stderr)
            print("      ├ indextts\\", file=sys.stderr)
            print("      ├ runtime\\", file=sys.stderr)
            print("      └ start_server.bat", file=sys.stderr)
            print(json.dumps({"ok": False, "reason": "not_a_project_dir", "path": str(p)},
                             ensure_ascii=False))
            return 1
        _write_project_pointer(p)
        print(json.dumps({"ok": True, "project_dir": str(p), "source": "--set（已记住）"},
                         ensure_ascii=False))
        return 0

    if PROJECT_DIR is not None:
        # 成功也要在 stderr 说一句：stdout 那行 JSON 是给 agent 解析的，
        # 人看不懂。用户界面上"一直在转圈却没有任何输出"是最劝退的体验，
        # 这句是兜底——即使 agent 忘了转述，终端里也有迹可循。
        print("[locate] 找到项目：%s（%s）" % (PROJECT_DIR, PROJECT_SOURCE), file=sys.stderr)
        print(json.dumps({"ok": True, "project_dir": str(PROJECT_DIR), "source": PROJECT_SOURCE},
                         ensure_ascii=False))
        return 0

    print(LOCATE_HELP, file=sys.stderr)
    print(json.dumps({"ok": False, "reason": "project_not_found",
                      "need": "project_dir",
                      "options": [
                          {"id": "path", "how": "让用户把整合包根目录（含 server.py 的那一层）发过来，然后 locate --set \"<路径>\""},
                          {"id": "start_service", "how": "让用户双击包根目录的 start_server.bat，等服务起来后再跑 locate（会自动从端口反查）"},
                          {"id": "no_package", "how": "用户没有整合包：B 站 UP 主「十一君Jun」https://space.bilibili.com/470166715 ，在投稿里找 Index TTS 2.0 相关视频，按说明获取整合包"},
                      ],
                      "next": "让用户在 ①直接给目录 / ②先启动服务 / ③没有整合包去B站 三条里选一条"},
                     ensure_ascii=False))
    return 3


def cmd_ensure(args):
    ok = ensure_server(allow_start=not args.no_start)
    h = health()
    print(json.dumps({"ok": ok, "health": h}, ensure_ascii=False))
    return 0 if ok else 1


def cmd_status(args):
    """模型状态 + 仪表。默认输出人话（vram_used_mb 这种字段名用户看不懂），
    `--json` 给原始响应，给上游程序解析用。
    """
    st = api_get_json("/status")
    if getattr(args, "json", False):
        print(json.dumps(st, ensure_ascii=False))
        return 0

    loaded = bool(st.get("loaded"))
    line = "模型: %s" % ("已加载" if loaded else "未加载（首次合成时才加载，约 30~60s）")
    if st.get("generating"):
        line += "，正在生成中"
    print(line)
    if st.get("phase"):
        print("  阶段: %s" % st["phase"])

    gpu = st.get("gpu_name")
    used, total = st.get("vram_used_mb"), st.get("vram_total_mb")
    if gpu:
        if isinstance(used, int) and isinstance(total, int) and total:
            free = (total - used) / 1024.0
            print("GPU: %s | 显存 %.1f/%.1f G（当前余 %.1f G）"
                  % (gpu, used / 1024.0, total / 1024.0, free))
            if loaded and free * 1024 < VRAM_WARN_LOADED_MB:
                print("  [!] 余量偏低（< %.1f G）：长文本会明显变慢，甚至借共享显存后慢十倍"
                      % (VRAM_WARN_LOADED_MB / 1024.0))
        else:
            print("GPU: %s（未上报显存用量）" % gpu)
    else:
        print("GPU: 未检测到（CPU 推理会非常慢）")

    mem_used, mem_total = st.get("mem_used_gb"), st.get("mem_total_gb")
    if mem_used is not None and mem_total is not None:
        print("内存: %.1f/%.1f G" % (mem_used, mem_total))
    if st.get("cpu_percent") is not None:
        print("CPU: %s%%" % st["cpu_percent"])
    if loaded:
        bits = []
        if st.get("fp16"):
            bits.append("fp16")
        if st.get("w2v_fp16"):
            bits.append("w2v_fp16")
        if st.get("diffusion_steps"):
            bits.append("steps=%s" % st["diffusion_steps"])
        if bits:
            print("精度/采样: %s" % " ".join(bits))
    return 0


def cmd_model(args):
    """GET /model：这一层模型自己的配置 + 是否正在生成。

    和 status 的分工：status 是整机仪表（CPU / 内存 / 显存），model 是模型实例
    本身——精度开关、扩散步数，以及 generating。`generating` 是"现在卸载会不会
    被拒绝/打断"的唯一可靠信号，排查 OOM 时先看它。
    """
    d = api_get_json("/model")
    if getattr(args, "json", False):
        print(json.dumps(d, ensure_ascii=False))
        return 0

    loaded = bool(d.get("loaded"))
    head = "模型: %s" % ("已加载" if loaded else "未加载")
    if d.get("generating"):
        head += "，正在生成中"
    print(head)
    if d.get("phase"):
        print("  阶段: %s" % d["phase"])

    prec = []
    for key in ("fp16", "s2mel_fp16", "w2v_fp16", "qwen_fp16"):
        if key in d:
            prec.append("%s=%s" % (key, "on" if d[key] else "off"))
    if prec:
        print("精度: %s" % " ".join(prec))
    if d.get("diffusion_steps") is not None:
        print("采样: steps=%s cfg=%s" % (d.get("diffusion_steps"), d.get("inference_cfg_rate")))
    if d.get("cudnn_benchmark"):
        print("cuDNN benchmark: on")
    if not loaded:
        print("  (未加载时以上是**下次加载**会用的配置；改配置在 WebUI「模型管理」页)")
    return 0


def cmd_unload(args):
    """POST /model/unload：释放显存。

    为什么做成命令：8G 卡上 OOM 是最高频失败，而此前 skill 只在错误提示里让
    用户"自己去 POST /model/unload"——等于把最需要对症的那一步推回给人。
    正在生成时服务端返回 409（它不替用户决定要不要打断），要打断必须显式
    --force，这是刻意保留的一道确认。
    """
    q = "?force=1" if getattr(args, "force", False) else ""
    status, out = api_post_form("/model/unload" + q, {}, timeout=180)
    if status == 409:
        detail = out.get("detail") if isinstance(out, dict) else out
        print(json.dumps({"ok": False, "reason": "generating", "detail": detail},
                         ensure_ascii=False))
        print("[unload] 正在生成，没有卸载。要打断当前生成并卸载：unload --force",
              file=sys.stderr)
        return 1
    if status >= 400:
        print(json.dumps({"ok": False, "detail": out}, ensure_ascii=False))
        print("[unload] failed: HTTP %s" % status, file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False))
    print("[unload] 模型已卸载、显存已释放。下次合成会重新加载（首次约 30~60s）。",
          file=sys.stderr)
    return 0


def _onoff(spec):
    """on/off（也认 1/0/true/false）→ bool。别的值直接报错，不做模糊猜测。

    没用 argparse 的 store_true，是因为"把某个精度开关关掉"同样常见；
    两个互斥的 --x / --no-x 会让参数表翻倍，也更容易被 agent 传错。
    """
    v = str(spec).strip().lower()
    if v in ("on", "1", "true", "yes", "y"):
        return True
    if v in ("off", "0", "false", "no", "n"):
        return False
    raise SystemExit("ERROR: expected on/off for this switch, got %r" % (spec,))


def _collect_model_config(args):
    """从命令行收集要写的键；没给的键不出现（服务端只改传过去的那些）。"""
    cfg = {}
    for key in MODEL_BOOL_KEYS:
        raw = getattr(args, key, None)
        if raw is not None:
            cfg[key] = _onoff(raw)
    for key in MODEL_RUNTIME_KEYS:
        raw = getattr(args, key, None)
        if raw is not None:
            cfg[key] = raw
    return cfg


def cmd_model_config(args):
    """POST /model/config：改加速配置，不重建模型。

    这是 8G 卡在"卸载"之外唯一真正有用的显存旋钮：diffusion_steps 决定显存与耗时，
    w2v_fp16 单独就省约 1G。但它有个陷阱——精度开关只是**写进配置**，已加载的模型
    不会被换掉，用户改完发现"没变化"就是踩在这里。所以两类键的生效时机必须分开说。
    """
    cfg = _collect_model_config(args)
    if not cfg:
        raise SystemExit(
            "ERROR: nothing to change. Pass at least one of --fp16 / --s2mel-fp16 / "
            "--w2v-fp16 / --qwen-fp16 / --cudnn-benchmark / --diffusion-steps / --cfg-rate "
            "(current values: python %s model)" % Path(__file__).name)

    status, out = api_post_json("/model/config", cfg)
    if status >= 400:
        print(json.dumps({"ok": False, "detail": out}, ensure_ascii=False))
        print("[model-config] failed: HTTP %s" % status, file=sys.stderr)
        return 1

    print(json.dumps(out, ensure_ascii=False))
    if any(k in cfg for k in MODEL_PRECISION_KEYS):
        print("[model-config] 精度开关已写进配置，但**已加载的模型不会变**："
              "要生效得 model-restart（30~60s），或等服务下次自然加载。", file=sys.stderr)
    if any(k in cfg for k in MODEL_RUNTIME_KEYS):
        print("[model-config] 采样参数（steps / cfg_rate）从下一次生成起生效，不用重启。",
              file=sys.stderr)
    return 0


def cmd_model_restart(args):
    """POST /model/restart：重建模型，可顺带写配置。

    为什么值得单独给个命令：精度开关只有重建才会被读取，所以"改 w2v_fp16 省显存"
    天生是两步；合成一步能少一次"漏做第二步"的机会。
    生成中服务端一律 409（重建等于把正在用的模型换掉），没有 force 通道——
    真要打断就先 unload --force，语义比"restart 顺手杀掉别人"清楚。
    """
    cfg = _collect_model_config(args)
    if cfg:
        # 用"携带"而不是"已应用"：409 时服务端在写配置之前就返回了，说"已应用"会骗人
        print("[model-restart] 携带配置: %s" % json.dumps(cfg, ensure_ascii=False),
              file=sys.stderr)
    status, out = api_post_json("/model/restart", cfg, timeout=600)
    if status == 409:
        detail = out.get("detail") if isinstance(out, dict) else out
        print(json.dumps({"ok": False, "reason": "generating", "detail": detail,
                          "config_applied": False}, ensure_ascii=False))
        print("[model-restart] 正在生成，服务端不重建（那会把正在用的模型换掉）。"
              "本次配置**没有写入**，等这次生成结束再来，或先 unload --force 打断。",
              file=sys.stderr)
        return 1
    if status >= 400 or (isinstance(out, dict) and out.get("ok") is False):
        print(json.dumps({"ok": False, "detail": out}, ensure_ascii=False))
        print("[model-restart] failed: HTTP %s" % status, file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False))
    print("[model-restart] 模型已按当前配置重建。", file=sys.stderr)
    return 0


def cmd_presets(args):
    names = api_get_json("/presets").get("presets", [])
    if args.json:
        details_map = _preset_details(names)
        details = []
        for n in names:
            d = details_map.get(n)
            details.append(d if d is not None else {"name": n, "error": "detail unavailable"})
        print(json.dumps(details, ensure_ascii=False))
    else:
        for line in _preset_summary_lines(names):
            print(line)
    return 0


def cmd_preset(args):
    print(json.dumps(preset_detail(args.name), ensure_ascii=False, indent=2))
    return 0


def cmd_preset_save(args):
    """POST /presets：把一段参考音频存成命名音色。

    为什么需要：没有它，AI 每轮都得把同一段 wav 重新上传一遍（--voice <path>），
    用户说"以后都用这个声音"就落不了地。存成预设后 --preset <name> 一行搞定，
    而且预设会把情感参数、采样参数一起记住，比每次重建一遍参数稳。
    """
    name = (args.name or "").strip()
    if not name:
        raise SystemExit("ERROR: --name is required")
    if not args.audio:
        raise SystemExit("ERROR: --audio <wav> is required (reference voice to save)")

    emo_mode = args.emo_mode if args.emo_mode is not None else 0
    if emo_mode == 1 and not args.emo_audio:
        raise SystemExit("ERROR: emo_mode=1 requires --emo-audio")
    if emo_mode == 3 and not (args.emo_text or "").strip():
        raise SystemExit("ERROR: emo_mode=3 requires --emo-text")
    if emo_mode == 2 and not args.emo_vector:
        print("[warn] emo_mode=2 without --emo-vector: saving an all-zero vector", file=sys.stderr)

    audio_path = resolve_voice(args.audio)
    fields = {
        "name": name,
        "overwrite": bool(getattr(args, "overwrite", False)),
        "emo_control_method": emo_mode,
    }
    if args.emo_weight is not None:
        fields["emo_weight"] = args.emo_weight
    if args.emo_text:
        fields["emo_text"] = args.emo_text
    if getattr(args, "use_random", False):
        fields["use_random"] = True
    if emo_mode == 2:
        vec = parse_emo_vector(args.emo_vector) if args.emo_vector else [0.0] * 8
        for i, v in enumerate(vec, start=1):
            fields["vec%d" % i] = v
    for key in ("top_p", "top_k", "temperature", "length_penalty", "num_beams",
                "repetition_penalty", "max_mel_tokens", "seg_tokens"):
        val = getattr(args, key, None)
        if val is None:
            continue
        fields["max_text_tokens_per_segment" if key == "seg_tokens" else key] = val

    files = {"prompt_audio": ("prompt.wav", read_audio(audio_path, "prompt_audio"), "audio/wav")}
    if emo_mode == 1 and args.emo_audio:
        emo_path = resolve_voice(args.emo_audio)
        files["emo_audio"] = ("emo.wav", read_audio(emo_path, "emo_audio"), "audio/wav")

    try:
        with post_multipart("/presets", fields, files, timeout=120) as r:
            out = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = http_error_detail(e)
        if e.code == 409:
            # 同名保护是服务端刻意的行为，客户端不要自作主张覆盖
            print(json.dumps({"ok": False, "reason": "exists", "detail": detail},
                             ensure_ascii=False))
            print("[preset-save] 「%s」已存在。要覆盖就加 --overwrite" % name, file=sys.stderr)
            return 1
        print(json.dumps({"ok": False, "detail": detail}, ensure_ascii=False))
        print("[preset-save] failed: HTTP %s" % e.code, file=sys.stderr)
        return 1

    print(json.dumps({"ok": True, "name": name, "presets": out.get("presets", [])},
                     ensure_ascii=False))
    print("[preset-save] 已保存音色「%s」。以后合成直接 --preset %s。" % (name, name),
          file=sys.stderr)
    return 0


def cmd_examples(args):
    ex = api_get_json("/examples").get("examples", [])
    if args.json:
        print(json.dumps(ex, ensure_ascii=False))
    else:
        for e in ex:
            print("%-16s emo_mode=%s  %s" % (e.get("prompt_audio", "?"), e.get("emo_mode", 0),
                                             (e.get("text") or "")[:40]))
        if not ex:
            print("(no examples)")
        else:
            print("\n用法：synth --voice <名称> --text ...   例：--voice %s"
                  % Path((ex[0].get("prompt_audio") or "voice_01.wav").replace("\\", "/")).name)
            print("（名称本地没有时，脚本会自动从服务端 examples 取一份，纯远程部署也能用）")
    return 0


def cmd_segments(args):
    text = args.text or (read_text_file(args.text_file) if args.text_file else "")
    if not text.strip():
        raise SystemExit("ERROR: empty text")
    _, out = api_post_form("/segments", {"text": text, "max_text_tokens_per_segment": args.seg_tokens})
    segs = out.get("segments") or []

    if not getattr(args, "out", None):
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # --out：直接落成 batch 能吃的任务清单。长文→批量合成此前要人工中转一次，
    # 而"超限了去看 segments"这种提示用户根本不知道怎么接。
    defaults = {}
    for k in ("preset", "voice", "rate"):
        v = getattr(args, k, None)
        if v:
            defaults[k] = v
    items = [{"id": "seg_%03d" % (i + 1), "text": s.get("text") or ""}
             for i, s in enumerate(segs) if (s.get("text") or "").strip()]
    if not items:
        raise SystemExit("ERROR: server returned no usable segments")
    p = Path(args.out).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"defaults": defaults, "items": items},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "out": str(p.resolve()), "count": len(items),
                      "defaults": defaults}, ensure_ascii=False))
    if not defaults.get("preset") and not defaults.get("voice"):
        print("[segments] 提示：清单里没写音色，batch 会以退出码 2 停下来问。"
              "补 --preset/--voice，或事后改 defaults。", file=sys.stderr)
    return 0


def cmd_history(args):
    print(json.dumps(api_get_json("/history"), ensure_ascii=False, indent=2))
    return 0


def cmd_tempo(args):
    """保音高变速：POST /audio/tempo（audiotsm WSOLA），不落盘、不重新合成。

    为什么值得单独一个子命令：语速不对是高频诉求，而重新合成既慢又可能改变
    音色细节；变速只动时长、保音高，几秒钟出结果。它也完全不碰模型和 GPU。
    """
    src = Path(args.file)
    if not src.is_file():
        raise SystemExit("ERROR: input audio not found: %s" % src)
    rate = float(args.rate)
    if not 0.5 <= rate <= 2.0:
        raise SystemExit("ERROR: --rate must be within 0.5~2.0 (got %s); "
                         "the server rejects anything outside this range" % rate)

    data = read_audio(src, "audio")
    ctype = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
    if not ensure_server(allow_start=not args.no_start):
        return 1
    try:
        resp = post_multipart("/audio/tempo", {"rate": rate},
                              {"file": ("input" + src.suffix.lower(), data, ctype)},
                              timeout=300)
    except urllib.error.HTTPError as e:
        raise SystemExit("ERROR: /audio/tempo rejected: %s" % http_error_detail(e))
    with resp:
        wav = resp.read()
    if not wav:
        raise SystemExit("ERROR: server returned an empty body for /audio/tempo")

    if args.out:
        out_path = Path(args.out)
    else:
        base_dir = Path(args.outdir) if args.outdir else AGENT_OUT_DIR
        out_path = base_dir / time.strftime("%Y%m%d") / ("%s_tempo%s.wav" % (src.stem, ("%.2f" % rate).rstrip("0").rstrip(".")))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(wav)
    print(json.dumps({
        "ok": True,
        "out": str(out_path.resolve()),
        "rate": rate,
        "bytes": len(wav),
        "source": str(src.resolve()),
    }, ensure_ascii=False))
    print("[tempo] %s -> %s (rate %.2f, no GPU used)" % (src.name, out_path.resolve(), rate), file=sys.stderr)
    return 0


def cmd_selftest(args):
    """只读自检：服务可达性 / 模型状态 / 预设 / 示例音色 / 输出目录可写。

    刻意不做合成——合成要占 GPU 几十秒，不适合当"装好了没"的体检。
    想验证完整链路请跑 synth（音色仍需按硬规则与用户确认）。
    退出码 0=全过，1=有失败项。
    """
    checks = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    h = health()
    add("service_reachable", h is not None, ORIGIN)
    if h is not None:
        add("health_ok", (h or {}).get("status") == "ok", str(h))
        try:
            st = api_get_json("/status")
            add("status", True, "model_loaded=%s generating=%s gpu=%s"
                % (st.get("loaded"), st.get("generating"), st.get("gpu_name") or "n/a"))
        except SystemExit as e:
            add("status", False, str(e))
        try:
            names = api_get_json("/presets").get("presets", [])
            add("presets", True, "%d preset(s): %s" % (len(names), ", ".join(names[:8]) or "-"))
            missing_prompt = []
            for n, d in _preset_details(names).items():
                if d is not None and not d.get("prompt_audio_url"):
                    missing_prompt.append(n)
            add("presets_have_reference_audio", not missing_prompt,
                "no prompt.wav: %s" % ", ".join(missing_prompt) if missing_prompt else "all presets have prompt.wav")
        except SystemExit as e:
            add("presets", False, str(e))
        try:
            ex = api_get_json("/examples").get("examples", [])
            add("examples", bool(ex), "%d built-in example voice(s)" % len(ex))
        except SystemExit as e:
            add("examples", False, str(e))

    try:
        AGENT_OUT_DIR.mkdir(parents=True, exist_ok=True)
        probe = AGENT_OUT_DIR / (".selftest_%d.tmp" % os.getpid())
        probe.write_bytes(b"ok")
        probe.unlink()
        add("out_dir_writable", True, str(AGENT_OUT_DIR))
    except OSError as e:
        add("out_dir_writable", False, "%s (%s)" % (AGENT_OUT_DIR, e))

    add("project_dir", PROJECT_DIR is not None,
        "%s（来源：%s）" % (PROJECT_DIR, PROJECT_SOURCE) if PROJECT_DIR is not None
        else "未找到；仅 ensure 受影响，其余子命令照常。跑 `locate` 拿到三条路")
    add("proxy_bypass", _proxy is None,
        "bypassing system proxy" if _proxy is None else "INDEXTTS_PROXY=%s" % _proxy)

    failed = [c for c in checks if not c["ok"]]
    print(json.dumps({"ok": not failed, "checks": checks}, ensure_ascii=False, indent=2))
    if failed:
        print("[selftest] %d check(s) failed" % len(failed), file=sys.stderr)
        return 1
    print("[selftest] all %d checks passed" % len(checks), file=sys.stderr)
    return 0


# ---- 能力体检（doctor）--------------------------------------------------------
# 与 selftest 的分工：
#   selftest  只读，秒级，答"服务通不通、预设全不全"；
#   doctor    真跑一次合成 + 一次变速，答"现在能不能直接出声"，首次要等模型加载。
# 二者不合并：装完后反复排查用 selftest，正式开工前跑一次 doctor。
DOCTOR_CACHE = CONFIG_DIR / "doctor.json"
DOCTOR_CACHE_TTL = 1800          # 冒烟结果缓存 30 分钟，避免每轮对话都等模型加载
DOCTOR_TEXT = "语音合成能力检查。"


def _doctor_cache_load():
    """取缓存；过期、换了服务地址、文件损坏都返回 None（宁可不复用，也不能谎报）。"""
    try:
        d = json.loads(DOCTOR_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("origin") != ORIGIN:
        return None
    try:
        age = time.time() - float(d.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    if age < 0 or age > DOCTOR_CACHE_TTL:
        return None
    d["age"] = int(age)
    return d


def _doctor_cache_save(rec):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = dict(rec, origin=ORIGIN, ts=time.time())
        DOCTOR_CACHE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass                  # 缓存写不进去不影响体检结论


def _synth_ns(**over):
    """造一个带 synth 全部默认值的参数命名空间。

    doctor / batch 靠它复用 `prepare_tts_request`，默认值直接取自真实 parser，
    所以 synth 以后加参数也不会让这两处悄悄漏字段。
    """
    ns = build_parser().parse_args(["synth", "--text", "x"])
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _pick_probe_voice(preset_arg, voice_arg=None):
    """挑一个能用来做冒烟合成的音色：指定预设 → 指定音频 → 首个带参考音频的预设 → 首个示例音色。"""
    if preset_arg:
        return preset_arg, None, "指定预设"
    if voice_arg:
        return None, voice_arg, "指定参考音频"
    try:
        names = api_get_json("/presets", timeout=15).get("presets", [])
    except SystemExit:
        names = []
    for n, d in _preset_details(names).items():
        if d is not None and d.get("prompt_audio_url"):
            return n, None, "首个含参考音频的预设"
    try:
        ex = api_get_json("/examples", timeout=15).get("examples", [])
    except SystemExit:
        ex = []
    for e in ex:
        name = Path((e.get("prompt_audio") or "").replace("\\", "/")).name
        if name:
            return None, name, "首个内置示例音色"
    return None, None, ""


def cmd_doctor(args):
    """端到端能力体检：探活 → 接口 → 真合成一次 → 变速一次。

    退出码 0=全过（可以开始生成），1=有失败项。stdout 一行 JSON 供 agent/上游解析。
    冒烟音频**保留**成试听样本：用户听到声音，比看到十个 ok:true 更能确认"能用了"。
    """
    checks = []
    t_start = time.time()

    def add(name, ok, detail="", **extra):
        rec = {"check": name, "ok": bool(ok), "detail": detail}
        rec.update(extra)
        checks.append(rec)
        return rec

    # 1) 服务可达 —— 后面所有项都依赖它，挂了就没必要继续
    h = health()
    add("service_reachable", h is not None,
        ORIGIN if h is not None else "无法连接；先跑 ensure（或双击整合包的 start_server.bat）")
    if h is None:
        failed = [c for c in checks if not c["ok"]]
        print(json.dumps({"ok": False, "ready": False, "origin": ORIGIN, "checks": checks},
                         ensure_ascii=False, indent=2))
        print("[doctor] 服务没连上：%s\n  下一步：python tts_client.py ensure（整合包用户也可双击 start_server.bat）"
              % ORIGIN, file=sys.stderr)
        return 1

    add("health_ok", (h or {}).get("status") == "ok", str(h))

    # 2) 模型状态（只报信息，未加载不算失败——懒加载是设计如此）
    st = {}
    try:
        st = api_get_json("/status")
        add("status_api", True, "model_loaded=%s gpu=%s" % (st.get("loaded"), st.get("gpu_name") or "n/a"),
            model_loaded=bool(st.get("loaded")))
    except SystemExit as e:
        add("status_api", False, str(e))

    # 2b) 显存余量 —— 只告警不判失败（optional）。
    # 8G 卡上这是最隐蔽的失败源：冒烟用的短句能过，长文本就借共享显存，
    # 表现为"不报错但慢十倍"，用户以为卡死。提前把余量说清楚比事后排查省事。
    used_mb, total_mb = st.get("vram_used_mb"), st.get("vram_total_mb")
    if isinstance(used_mb, int) and isinstance(total_mb, int) and total_mb > 0:
        loaded = bool(st.get("loaded"))
        if loaded:
            # 模型已驻留：used 已经含模型，余量就是真实可用的
            free_after = total_mb - used_mb
            limit = VRAM_WARN_LOADED_MB
        else:
            # 模型未加载：used 是其它程序的占用，加载后还要再吃掉一份增量
            free_after = total_mb - used_mb - VRAM_MODEL_GROWTH_MB
            limit = VRAM_WARN_ESTIMATE_MB
        ok = free_after >= limit
        detail = "显存 %d/%d MB（%s），%s余 %.1f G" % (
            used_mb, total_mb, "模型已加载" if loaded else "模型未加载",
            "" if loaded else "加载后预计", free_after / 1024.0)
        if not ok:
            detail += ("；余量偏低（< %.1f G）。长文本会明显变慢甚至 OOM——"
                       "先关掉占显存的程序；模型已加载时可 POST /model/unload 释放"
                       % (limit / 1024.0))
        add("vram_headroom", ok, detail, optional=True, warn=not ok,
            free_after_mb=free_after, used_mb=used_mb, total_mb=total_mb, model_loaded=loaded)
    else:
        add("vram_headroom", True, "服务端未上报显存（GPU 未启用或驱动不可用），跳过",
            optional=True, skipped=True)

    # 3) 预设接口 + 预设完整性
    preset_names = []
    try:
        preset_names = api_get_json("/presets").get("presets", [])
        add("presets_api", True, "%d preset(s)" % len(preset_names), count=len(preset_names))
        missing = [n for n, d in _preset_details(preset_names).items()
                   if d is not None and not d.get("prompt_audio_url")]
        add("presets_have_reference_audio", not missing,
            ("缺少 prompt.wav: %s" % ", ".join(missing)) if missing
            else ("%d 个预设均含参考音频" % len(preset_names)) if preset_names
            else "服务端没有预设（可用内置示例音色或 --voice）")
    except SystemExit as e:
        add("presets_api", False, str(e))

    # 4) 示例音色接口
    try:
        ex = api_get_json("/examples").get("examples", [])
        add("examples_api", True, "%d 个内置示例音色" % len(ex), count=len(ex))
    except SystemExit as e:
        add("examples_api", False, str(e))

    # 5) 输出目录可写 + 磁盘余量
    try:
        AGENT_OUT_DIR.mkdir(parents=True, exist_ok=True)
        probe = AGENT_OUT_DIR / (".doctor_%d.tmp" % os.getpid())
        probe.write_bytes(b"ok")
        probe.unlink()
        add("out_dir_writable", True, str(AGENT_OUT_DIR))
        try:
            import shutil as _sh
            free_gb = _sh.disk_usage(str(AGENT_OUT_DIR)).free / 1024 ** 3
            add("disk_headroom", free_gb >= 1.0,
                "剩余 %.1f G%s" % (free_gb, "" if free_gb >= 1.0
                                   else "；不足 1 G，批量合成到一半可能写盘失败"),
                optional=True, warn=(free_gb < 1.0), free_gb=round(free_gb, 2))
        except OSError:
            pass
    except OSError as e:
        msg = str(e)
        if getattr(e, "errno", None) == 28 or "no space" in msg.lower():
            msg = "磁盘已满；清掉旧产物或换 --outdir"
        add("out_dir_writable", False, "%s (%s)" % (AGENT_OUT_DIR, msg))

    # 6) 冒烟合成 —— 唯一能证明"接口通 + 模型能跑 + 音频能落盘"的一步
    sample = None
    if args.no_synth:
        add("synth_smoke", True, "skipped (--no-synth)", skipped=True)
    else:
        cached = None if args.force else _doctor_cache_load()
        preset, voice, why = _pick_probe_voice(args.preset, args.voice)
        if preset is None and voice is None:
            add("synth_smoke", False,
                "没有可用音色：预设里没有带参考音频的，服务端也没有示例音色。"
                "先去前端「预设」页建一个，或用 --voice 指定一份参考音频")
        elif cached and cached.get("preset") == preset and cached.get("voice") == voice:
            sample = cached.get("sample") or {}
            add("synth_smoke", bool(sample.get("out")),
                "复用 %d 分钟前的结果：%s（%.2fs）" % (cached.get("age", 0) // 60,
                                                     sample.get("out"), sample.get("duration") or 0),
                cached=True)
        else:
            tmpdir = tempfile.mkdtemp(prefix="indextts_doctor_")
            try:
                ns = _synth_ns(preset=preset, voice=voice,
                               client_id="doctor_" + uuid.uuid4().hex[:8],
                               text=args.text or DOCTOR_TEXT)
                params, files, voice_ref, emo_mode = prepare_tts_request(ns, args.text or DOCTOR_TEXT, tmpdir)
                print("[doctor] 冒烟合成中（首次要加载模型，约 30~60s）...", file=sys.stderr)
                result = post_tts(params, files, verbose=False)
                server_file = result.get("file")
                out_path = (Path(args.outdir) if args.outdir else AGENT_OUT_DIR) \
                    / time.strftime("%Y%m%d") / ("doctor_%s.wav" % time.strftime("%H%M%S"))
                download(result.get("wav"), out_path)
                info = wav_info(out_path) or {}
                sample = {
                    "out": str(out_path.resolve()),
                    "duration": info.get("duration"),
                    "sample_rate": info.get("sample_rate"),
                    "channels": info.get("channels"),
                    "bytes": out_path.stat().st_size,
                    "elapsed": result.get("elapsed"),
                    "wall": result.get("_wall"),
                    "voice": voice_ref,
                    "preset": preset,
                    "mel_capped": bool(result.get("mel_capped", False)),
                }
                ok = bool(sample["duration"] and sample["duration"] > 0.1)
                add("synth_smoke", ok,
                    "%s（%.2fs，%s Hz，%s）" % (why, sample["duration"] or 0,
                                              sample["sample_rate"], out_path.resolve()),
                    sample=sample)
                if not ok:
                    print("[doctor] 合成返回了文件但读不出有效时长，见 %s" % out_path, file=sys.stderr)
                else:
                    _doctor_cache_save({"preset": preset, "voice": voice, "sample": sample})
            except SystemExit as e:
                add("synth_smoke", False, str(e))
            finally:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)

    # 6b) 冒烟之后再测一次显存。上面 2b) 是在模型加载前查的，只能给"预计"；
    # 模型驻留后剩多少才是长文本能不能跑的关键数字，而且这个分支（loaded=true）
    # 在首次体检里永远走不到——不补测就等于没测真实场景。
    if sample and not args.no_synth:
        try:
            st2 = api_get_json("/status", timeout=10)
            u2, t2 = st2.get("vram_used_mb"), st2.get("vram_total_mb")
            if isinstance(u2, int) and isinstance(t2, int) and t2 > 0:
                free_after = t2 - u2
                ok = free_after >= VRAM_WARN_LOADED_MB
                detail = "显存 %d/%d MB（模型已加载，实测）余 %.1f G" % (
                    u2, t2, free_after / 1024.0)
                if not ok:
                    detail += ("；余量偏低（< %.1f G）。长文本会明显变慢甚至 OOM——"
                               "先关掉占显存的程序，或 POST /model/unload 释放"
                               % (VRAM_WARN_LOADED_MB / 1024.0))
                for i, c in enumerate(checks):
                    if c["check"] == "vram_headroom":
                        checks[i] = {"check": "vram_headroom", "ok": ok, "detail": detail,
                                     "optional": True, "warn": not ok,
                                     "free_after_mb": free_after, "used_mb": u2,
                                     "total_mb": t2, "model_loaded": True}
                        break
        except SystemExit:
            pass

    # 7) 变速接口 —— /audio/tempo 不占 GPU，但对齐时间轴时要用
    if args.no_synth or not sample or not sample.get("out"):
        add("tempo_api", True, "skipped（没有冒烟音频可变速）", skipped=True)
    else:
        try:
            data = Path(sample["out"]).read_bytes()
            resp = post_multipart("/audio/tempo", {"rate": 1.05},
                                  {"file": ("in.wav", data, "audio/wav")}, timeout=300)
            with resp:
                out_bytes = resp.read()
            ok = bool(out_bytes)
            add("tempo_api", ok, "1.05x -> %d bytes" % len(out_bytes) if ok else "返回空 body")
        except urllib.error.HTTPError as e:
            add("tempo_api", False, "/audio/tempo -> HTTP %s: %s" % (e.code, http_error_detail(e)))
        except SystemExit as e:
            add("tempo_api", False, str(e))

    # 8) 项目根：只作信息项，失败不算体检失败（远程部署本来就没有）
    add("project_dir", PROJECT_DIR is not None,
        "%s（来源：%s）" % (PROJECT_DIR, PROJECT_SOURCE) if PROJECT_DIR is not None
        else "未定位；仅 ensure 受影响，合成照常（跑 locate 可指定）",
        optional=True)

    failed = [c for c in checks if not c["ok"] and not c.get("optional")]
    ready = not failed
    total = round(time.time() - t_start, 1)
    vram_check = next((c for c in checks if c["check"] == "vram_headroom"), None)
    vram_txt = ""
    if vram_check and not vram_check.get("skipped"):
        vram_txt = "显存余 %.1f G" % ((vram_check.get("free_after_mb") or 0) / 1024.0)
    # 冒烟这一项容易被误读：duration 是**音频多长**，elapsed 才是**花了多久**。
    # 只写 "冒烟合成 2.28s" 会让人以为出音只要 2 秒（首次其实含 20s+ 的模型加载），
    # 拿这个数估批量时间会差一个数量级，所以两个数都摆出来、并标清单位。
    if sample and sample.get("duration"):
        if sample.get("elapsed"):
            smoke_txt = "冒烟 %.1fs音频/耗时%.1fs" % (sample["duration"], sample["elapsed"])
        else:
            smoke_txt = "冒烟 %.1fs音频" % sample["duration"]
    else:
        smoke_txt = "未做冒烟"
    summary = " · ".join(x for x in [
        "服务在线",
        "预设 %d 个" % len(preset_names),
        smoke_txt,
        "变速可用" if any(c["check"] == "tempo_api" and c["ok"] and not c.get("skipped") for c in checks) else "变速未测",
        vram_txt,
    ] if x)
    print(json.dumps({
        "ok": ready,
        "ready": ready,
        "skill_version": SKILL_VERSION,
        "skill_script": str(Path(__file__).resolve()),
        "origin": ORIGIN,
        "elapsed": total,
        "summary": summary,
        "checks": checks,
        "sample": sample,
    }, ensure_ascii=False, indent=2))
    if not ready:
        print("[doctor] %d 项未通过：%s" % (len(failed), "; ".join(c["check"] for c in failed)), file=sys.stderr)
        return 1
    print("[doctor] 能力体检通过：%s（整套体检 %.1fs）" % (summary, total), file=sys.stderr)
    # 冒烟用的是短句，触发不到长文本的 KV cache 峰值。这一点必须说清，
    # 否则"体检通过"会被理解成"多长都能跑"。
    print("[doctor] 注意：冒烟只证明「短句能跑通」，长文本（>500 字）首次跑请盯一下进度与耗时。",
          file=sys.stderr)
    if vram_check and vram_check.get("warn"):
        print("[doctor] [!] 显存余量偏低：%s" % vram_check["detail"], file=sys.stderr)
    if sample and sample.get("out"):
        print("[doctor] 可以开始生成语音了。试听样本：%s" % sample["out"], file=sys.stderr)
    else:
        print("[doctor] 可以开始生成语音了。", file=sys.stderr)
    return 0


def cmd_capabilities(args):
    """输出机器可读的能力清单，供别的 Skill 一次性发现"你能干什么、怎么调、有什么限制"。

    上游 Skill 不该靠读文档猜参数——它读一次这个 JSON 就够了。
    """
    h = health()
    caps = {
        "ok": h is not None,
        "skill": {
            "name": "indextts2-tts",
            "version": SKILL_VERSION,
            "script": str(Path(__file__).resolve()),
        },
        "service": {
            "url": ORIGIN,
            "reachable": h is not None,
            "api_base": API,
            "tts_endpoint": "POST /api/v1/tts (multipart/form-data, SSE response)",
        },
        "voices": {"presets": [], "examples": []},
        "emotion": {
            "modes": {"0": "跟随参考音频", "1": "情感参考音频 emo_audio",
                      "2": "8 维情感向量 vec1..vec8", "3": "情感文本 emo_text"},
            "vector_order": EMO_VEC_NAMES,
        },
        "limits": {
            "text_soft_chars": TEXT_CHARS_SOFT,
            "text_hard_chars": TEXT_CHARS_HARD,
            "emo_text_chars": MAX_EMO_TEXT_CHARS,
            "audio_mb": AUDIO_MAX_BYTES // 1048576,
            "tempo_rate": [0.5, 2.0],
            "interval_silence_ms": [10, 500],
        },
        "concurrency": {
            "parallel": False,
            "note": "服务端全局单锁串行推理；并发请求只会排队，客户端另有文件锁",
            "serial_tool": "batch（一次进程内串行，不重复抢锁）",
        },
        "timing": {
            "first_synth_seconds": "30~60（首次要加载模型）",
            "loaded_synth_seconds": "≈ 音频时长 × 1~3",
        },
        "commands": {
            "locate": "定位项目根；exit 3 = 需要用户给路径或先起服务",
            "health": "探活；连不上时带 reason(connection_refused/timeout/http_error/dns_error) 与 hints",
            "doctor": "端到端能力体检（含冒烟合成）；exit 0 = 可以开始生成",
            "presets": "预设清单", "examples": "内置示例音色",
            "preset-save": "把一段参考音频存成命名音色（之后 --preset 引用）",
            "synth": "单条合成，stdout 一行 JSON", "batch": "批量合成，stdout 一份 manifest",
            "tempo": "保音高变速，不占 GPU", "last": "找回最近一次产物",
            "model": "模型配置 + 是否正在生成（不占 GPU）",
            "unload": "卸载模型释放显存（--force 可打断正在进行的生成）",
            "model-config": "改精度开关/扩散步数/CFG（精度类要 model-restart 才生效）",
            "model-restart": "按当前配置重建模型（30~60s，生成中 409）",
            "glossary": "术语表：列表 / --add / --delete / --clear --yes",
            "api": "接口对账：服务端 openapi vs skill 已适配清单（--strict 有缺口则 exit 1）",
        },
        "exit_codes": {
            "0": "成功", "1": "失败", "2": "需要先确认音色（headless 下 stdout 给 JSON）",
            "3": "需要用户提供项目位置（headless 下 stdout 给 JSON）",
        },
    }
    if h is not None:
        try:
            names = api_get_json("/presets", timeout=15).get("presets", [])
            details = _preset_details(names)
            caps["voices"]["presets"] = [
                {"name": n, "emo_mode": (details.get(n) or {}).get("emo_control_method", 0),
                 "has_reference": bool((details.get(n) or {}).get("prompt_audio_url"))}
                for n in names
            ]
        except SystemExit:
            pass
        try:
            caps["voices"]["examples"] = [
                {"name": Path((e.get("prompt_audio") or "").replace("\\", "/")).name,
                 "emo_mode": e.get("emo_mode", 0)}
                for e in api_get_json("/examples", timeout=15).get("examples", [])
            ]
        except SystemExit:
            pass
        try:
            st = api_get_json("/status", timeout=15)
            caps["service"]["model_loaded"] = bool(st.get("loaded"))
            caps["service"]["gpu"] = st.get("gpu_name")
        except SystemExit:
            pass
    print(json.dumps(caps, ensure_ascii=False, indent=2))
    return 0 if h is not None else 1


# ---- 接口对账（openapi）---------------------------------------------------------
# 非 API 的作用域路径：页面、静态资源与文档本身也注册在 app 上（且部分没写
# include_in_schema=False，会进 openapi），不排除就会把"未适配"清单灌成噪音。
NON_API_PATHS = {"/", "/assets/{name}", "/openapi.json", "/docs",
                 "/docs/oauth2-redirect", "/redoc",
                 "/timeline", "/timeline-assets/{name}",
                 "/longform", "/longform-assets/{name}",
                 "/guide", "/guide-img/{name}"}


def _openapi_routes():
    """拉服务端 openapi.json，返回 (doc, 命中的地址)；都被拒则 (None, 尝试过的地址)。

    FastAPI 默认把文档挂在根路径 /openapi.json（路由 prefix 不影响它）；显式传过
    openapi_url 的版本可能挂在别处，所以两个地址都试。这里只做探测，不走 API 常量。
    """
    tried = []
    for path in ("/openapi.json", "/api/v1/openapi.json"):
        url = ORIGIN + path
        tried.append(url)
        try:
            with _urlopen(urllib.request.Request(url, method="GET"), timeout=20) as r:
                return json.loads(r.read().decode("utf-8")), url
        except urllib.error.HTTPError as e:
            if e.code in (404, 405):
                continue
            raise SystemExit("ERROR: GET %s -> HTTP %s: %s" % (path, e.code, http_error_detail(e)))
    return None, tried


def _classify(path):
    """把接口归类：core（本 skill 该管的）或某个扩展层。"""
    for prefix, label in EXT_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return label
    return "core"


def cmd_api(args):
    """接口对账：服务端 openapi 与 ADAPTED_ENDPOINTS 比一比。

    这是 skill 唯一的版本漂移探针。硬编码路径不会自己报错——整合包加了接口、
    或者路径改名了，skill 只会安静地继续打旧地址，直到某天 404。用 openapi
    对账就能把"新增但没适配"和"skill 写了但服务端已经没了"两类问题都摆出来。
    """
    doc, src = _openapi_routes()
    if doc is None:
        print(json.dumps({"ok": False, "reason": "openapi_disabled", "tried": src},
                         ensure_ascii=False))
        print("[api] 服务端没有暴露 openapi（FastAPI 的 openapi_url 被关掉了？）。"
              "无法对账，skill 的接口清单只能靠人肉维护。", file=sys.stderr)
        return 1

    server = {}
    for path, ops in (doc.get("paths") or {}).items():
        if path in NON_API_PATHS:
            continue
        norm = path[len("/api/v1"):] if path.startswith("/api/v1") else path
        for method in ops:
            m = method.upper()
            if m in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                server["%s %s" % (m, norm)] = _classify(norm)

    core_server = {e for e, group in server.items() if group == "core"}
    ext_count = {}
    for group in server.values():
        if group != "core":
            ext_count[group] = ext_count.get(group, 0) + 1

    adapted_core = {e for e in ADAPTED_ENDPOINTS if _classify(e.split(" ", 1)[1]) == "core"}
    missing = sorted(core_server - adapted_core)     # 服务端有、skill 没适配
    gone = sorted(adapted_core - core_server)        # skill 在调、服务端已经没有

    if args.json:
        print(json.dumps({
            "ok": True, "source": src,
            "server_total": len(server),
            "server_core": len(core_server),
            "adapted": len(adapted_core & core_server),
            "adapted_total": len(adapted_core),
            "extensions": ext_count,
            "unadapted": missing,
            "missing_on_server": gone,
        }, ensure_ascii=False, indent=2))
    else:
        print("服务端 %s 共 %d 个接口" % (src, len(server)))
        ext_desc = " / ".join("%s %d 个" % (k, n) for k, n in sorted(ext_count.items())) or "无扩展"
        print("  核心 %d 个 / %s" % (len(core_server), ext_desc))
        print("skill 已适配核心接口: %d / %d" % (len(adapted_core & core_server), len(core_server)))
        if missing:
            print("\n未适配的核心接口（服务端有，skill 还调不了）:")
            for e in missing:
                print("  " + e)
        else:
            print("  核心接口无缺口")
        if gone:
            print("\n[!] skill 写了但服务端没有（整合包版本偏旧时会出现，调用会 404）:")
            for e in gone:
                print("  " + e)
        if ext_count:
            print("\n扩展接口（WebUI 的编辑工程层，不在本 skill 职责内）:")
            for k, n in sorted(ext_count.items()):
                print("  %s: %d 个" % (k, n))

    # --strict 只认"核心接口对不上"：扩展层本来就是另一个 skill 的事，不该让它报错
    if getattr(args, "strict", False) and (missing or gone):
        return 1
    return 0


def cmd_stop(args):
    """停止任务。--keep-model 保留已加载模型（否则按服务端默认策略处理）。

    退出码反映真实结果：404（任务不存在/已结束）与其它 4xx/5xx 都返回 1，
    否则 agent 无法区分"停成功"和"根本没这个任务"。
    """
    q = "?keep_model=1" if getattr(args, "keep_model", False) else ""
    status, out = api_post_form("/tts/%s/stop%s" % (urllib.parse.quote(args.job_id, safe=""), q), {})
    print(json.dumps(out, ensure_ascii=False))
    if status >= 400:
        print("[stop] failed: HTTP %s" % status, file=sys.stderr)
        return 1
    return 0


def cmd_glossary(args):
    """术语表管理：无参数=列表，--add=新增/覆盖，--delete=删除单条，--clear=清空。

    --clear 要求再带 --yes：它会一次删掉所有术语且不可恢复，而这脚本会被 AI
    调用，一个手滑的参数不该造成不可逆的结果。
    """
    status = 200
    if args.add:
        status, out = api_post_form("/glossary", {"term": args.add,
                                                  "reading_zh": args.zh or "",
                                                  "reading_en": args.en or ""})
    elif args.delete:
        q = urllib.parse.quote(args.delete, safe="")
        req = urllib.request.Request(API + "/glossary/" + q, method="DELETE")
        try:
            with _urlopen(req, timeout=30) as r:
                out = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            status, out = e.code, {"detail": http_error_detail(e)}
    elif getattr(args, "clear", False):
        if not getattr(args, "yes", False):
            print(json.dumps({"ok": False, "reason": "need_confirm",
                              "detail": "clearing removes every glossary term and cannot be undone"},
                             ensure_ascii=False))
            print("[glossary] 清空会删掉全部术语且不可恢复。确认要清就再加 --yes", file=sys.stderr)
            return 1
        req = urllib.request.Request(API + "/glossary", method="DELETE")
        try:
            with _urlopen(req, timeout=30) as r:
                out = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            status, out = e.code, {"detail": http_error_detail(e)}
    else:
        out = api_get_json("/glossary")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if status >= 400:
        print("[glossary] failed: HTTP %s" % status, file=sys.stderr)
        return 1
    return 0


def cmd_last(args):
    """找回最近一次生成的音频（默认输出目录 + last.json）。"""
    rec = _read_last_output()
    if rec is None:
        print(json.dumps({"ok": False, "detail": "no generation recorded yet (%s not found)" % LAST_OUTPUT_PATH},
                         ensure_ascii=False))
        return 1
    out = rec.get("out") or ""
    rec["exists"] = bool(out) and Path(out).exists()
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    if args.open:
        if out and Path(out).exists():
            _open_in_file_manager(Path(out).parent)
        elif out:
            _open_in_file_manager(AGENT_OUT_DIR)
    if not rec["exists"]:
        print("[warn] recorded file no longer exists: %s" % out, file=sys.stderr)
        return 1
    return 0


def cmd_choose(args):
    """选择预设：tty 下交互菜单；非 tty 打印候选清单并返回退出码 2（交给 agent 问用户）。"""
    names = api_get_json("/presets").get("presets", [])
    if not names:
        print("(no presets yet; create one in the WebUI or POST /api/v1/presets)", file=sys.stderr)
        return 1
    if not sys.stdin.isatty():
        print("\n".join(_preset_summary_lines(names)), file=sys.stderr)
        print("", file=sys.stderr)
        print("stdin is not a tty: ask the user which preset to use, then re-run "
              "with --preset <name>.", file=sys.stderr)
        return 2
    print("\n".join(_preset_summary_lines(names, numbered=True)))
    try:
        sel = input("\n选择预设编号（回车取消）：").strip()
    except EOFError:
        return 2
    if not sel:
        return 2
    if not sel.isdigit() or not (1 <= int(sel) <= len(names)):
        print("invalid choice: %s" % sel, file=sys.stderr)
        return 2
    print(names[int(sel) - 1])
    return 0


def cmd_synth(args):
    # ---- 文本：先做纯本地校验 ----
    # 顺序有意为之：文本为空/超长这类错误跟服务在不在没关系，不该先花时间去
    # 拉起服务再报错。ensure 挪到文本校验之后。
    if args.text_file:
        text = read_text_file(args.text_file).strip()
    elif args.text:
        text = args.text
    elif not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    else:
        raise SystemExit("ERROR: one of --text / --text-file / stdin is required")
    if not text.strip():
        # 只为空白字符时服务端会 400；本地先拦，省掉一次服务启动/请求往返。
        # 注意只判断、不改写 text：真正送出去的内容保持调用方原样。
        raise SystemExit("ERROR: empty text (whitespace only)")
    if len(text) > TEXT_CHARS_HARD:
        # 这句话是用户最常撞到的硬墙，光说 "split it first" 他不知道怎么拆。
        # 直接把两条可执行的路给出来。
        raise SystemExit(
            "ERROR: 文本太长（%d 字 > 硬上限 %d 字），服务端会直接 413，必须先拆。\n"
            "  先看自动分段：  python tts_client.py segments --text-file <稿子.txt>\n"
            "  批量合成：      按段拆成 batch 清单，一次跑完，失败可 --resume 重跑\n"
            "  单条建议 ≤ %d 字（超了只告警不拦，但更容易被截断、也更吃显存）"
            % (len(text), TEXT_CHARS_HARD, TEXT_CHARS_SOFT))
    if len(text) > TEXT_CHARS_SOFT:
        print("[warn] text is %d chars (soft limit %d); generation may take a long time. "
              "Consider splitting with `segments`." % (len(text), TEXT_CHARS_SOFT), file=sys.stderr)

    if not ensure_server(allow_start=not args.no_start):
        return 1

    # ---- 生成前必须与用户确认音色来源 -----------------------------------------
    # 服务就绪后，synth 不允许"默默挑一个预设"：既没 --preset 也没 --voice 时，
    # 打印候选清单并以退出码 2 中止，agent 必须停下来向用户询问。
    preset_ok = True
    try:
        preset_names = api_get_json("/presets", timeout=15).get("presets", [])
    except Exception:
        preset_names, preset_ok = [], False
    # 注意 preset_names 为空也要校验：服务端一个预设都没有时（刚解压的整合包就是这样），
    # 放行会让后面的 preset_detail() 直接 404，用户看到的是 HTTP 报错而不是"没有预设"。
    # 只有列表本身没取到时才放行，让后续错误暴露真实原因。
    if args.preset and preset_ok and args.preset not in preset_names:
        if preset_names:
            print("\n".join(_preset_summary_lines(preset_names)), file=sys.stderr)
            print("ERROR: preset '%s' not found (exit 2). "
                  "Ask the user to pick one from the list above." % args.preset, file=sys.stderr)
        else:
            print("ERROR: 服务端没有任何预设（%s）。\n"
                  "  改用 --voice <参考音频> 直接合成，或先在前端「预设」页建一个（exit 2）。"
                  % ORIGIN, file=sys.stderr)
        if HEADLESS:
            print(json.dumps({"ok": False, "need": "voice", "reason": "preset_not_found",
                              "preset": args.preset, "presets": preset_names}, ensure_ascii=False))
        raise SystemExit(2)
    if not args.preset and not args.voice:
        msg = ["", "=== 生成前需要先确认音色：请向用户询问使用哪个预设 ===", ""]
        msg += _preset_summary_lines(preset_names)
        last = _read_last_output()
        if last and last.get("preset"):
            msg += ["", "(hint: 上次生成使用过预设 '%s'，可询问用户是否继续使用)" % last["preset"]]
        ex = []
        try:
            ex = api_get_json("/examples", timeout=15).get("examples", [])
            if ex:
                msg += ["", "或直接指定参考音频 --voice（内置示例）："]
                for e in ex:
                    msg.append("  %-28s emo_mode=%s  %s"
                               % (e.get("prompt_audio", "?"), e.get("emo_mode", 0), (e.get("text") or "")[:30]))
        except Exception:
            pass
        msg += ["", "确认后重试：synth --preset \"<名称>\" --text ... --out ..."]
        print("\n".join(msg), file=sys.stderr)
        if HEADLESS:
            # 机器模式：上游拿不到"人来选"，把候选直接给它，由它自己决定或向上问
            print(json.dumps({
                "ok": False, "need": "voice", "reason": "voice_not_specified",
                "presets": preset_names,
                "examples": [{"name": Path((e.get("prompt_audio") or "").replace("\\", "/")).name,
                              "emo_mode": e.get("emo_mode", 0)} for e in ex],
                "hint": "pass --preset <name> or --voice <file>; headless 模式下不会替你挑",
            }, ensure_ascii=False))
        raise SystemExit(2)

    # ---- 全局合成锁：禁止并行生成 ---------------------------------------------
    lock = SynthLock(SYNTH_LOCK_PATH, wait_seconds=args.lock_wait)
    if not lock.acquire():
        raise SystemExit("ERROR: another synth is still running after %ds (lock: %s). "
                         "Wait for it to finish, or pass --lock-wait 0 to fail fast."
                         % (args.lock_wait, SYNTH_LOCK_PATH))

    tmpdir = tempfile.mkdtemp(prefix="indextts_synth_")
    try:
        # ---- 预设打底 + 参考音频 + 情感模式 ----
        # 这段逻辑与 batch / doctor 完全共用同一实现，避免两处各写一份后行为漂移。
        params, files, voice_ref, emo_mode = prepare_tts_request(args, text, tmpdir)
        print("[synth] text=%d chars, emo_mode=%s, voice=%s" % (len(text), emo_mode, voice_ref), file=sys.stderr)

        t0 = time.time()
        result = post_tts(params, files)
        job_id = result.get("_job_id")

        wav_url = result.get("wav")
        server_file = result.get("file")
        if args.out:
            out_path = Path(args.out)
        else:
            # 默认落 <project>/outputs/agent_tts/<日期>/<服务端文件名>：
            # 固定可预测（last 子命令可找回），且在 outputs 子目录里，
            # 服务端 14 天孤儿清理只扫 outputs 根目录，不会波及。
            out_path = (Path(args.outdir) if args.outdir else AGENT_OUT_DIR) \
                / time.strftime("%Y%m%d") / server_file
        download(wav_url, out_path)

        # server_path 只在本地同机部署（探测到项目根）时有意义；纯远程调用给 None，
        # 避免编造一个不存在的路径误导下游。
        server_path = str((PROJECT_DIR / "outputs" / server_file).resolve()) if PROJECT_DIR is not None else None
        summary = {
            "ok": True,
            "job_id": job_id,
            "file": server_file,
            "elapsed": result.get("elapsed"),
            "mel_capped": bool(result.get("mel_capped", False)),
            "preset": args.preset,
            "voice": voice_ref,
            "out": str(out_path.resolve()),
            "server_path": server_path,
        }
        _write_last_output({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "preset": args.preset,
            "voice": voice_ref,
            "emo_mode": emo_mode,
            "text_chars": len(text),
            "text_preview": text[:100],
            "out": summary["out"],
            "server_path": summary["server_path"],
            "job_id": job_id,
            "elapsed": summary["elapsed"],
            "mel_capped": summary["mel_capped"],
        })
        if summary["mel_capped"]:
            print("[warn] mel_capped=true: audio was silently truncated at max_mel_tokens. "
                  "Split the text or raise max_mel_tokens.", file=sys.stderr)
        print("[synth] done in %ss (wall %.1fs)" % (result.get("elapsed"), time.time() - t0), file=sys.stderr)
        print("[synth] audio saved: %s" % out_path.resolve(), file=sys.stderr)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    finally:
        lock.release()
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


# ---- 批量合成（batch）----------------------------------------------------------
def _safe_name(s, fallback):
    """把 id 变成安全的文件名：保留中英文数字与 -_.，其它字符换下划线。"""
    s = str(s or "").strip()
    if not s:
        return fallback
    name = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in s).strip("._")
    return (name or fallback)[:80]


def _load_batch_spec(path):
    """读任务清单：裸数组或 {"defaults": {...}, "items": [...]}；`-` 或省略表示 stdin。

    清单里的 `text_file` 相对路径按**清单文件所在目录**解析，不是 cwd。
    上游 Skill 常用 `-f /abs/tasks.json` 从自己的工作目录调用，按 cwd 解析会
    报"读不到文件"，而错误信息里只有文件名，看不出是基准目录不对。
    """
    if path in (None, "-"):
        raw = sys.stdin.read()
        src = "stdin"
        base = Path.cwd()
    else:
        raw = read_text_file(path)      # 复用：顺带处理 UTF-8 BOM
        src = path
        base = Path(path).resolve().parent
    if not raw.strip():
        raise SystemExit("ERROR: batch spec is empty (%s)" % src)
    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise SystemExit("ERROR: batch spec is not valid JSON (%s): %s" % (e, src))
    if isinstance(obj, list):
        items, defaults = obj, {}
    elif isinstance(obj, dict):
        items, defaults = obj.get("items") or [], obj.get("defaults") or {}
    else:
        raise SystemExit('ERROR: batch spec must be a JSON array or {"defaults":{...},"items":[...]}')
    if not isinstance(items, list) or not items:
        raise SystemExit("ERROR: batch spec has no items")
    if not isinstance(defaults, dict):
        raise SystemExit("ERROR: batch spec 'defaults' must be an object")

    # text_file 统一转绝对（基准 = 清单文件所在目录），下游就不必关心 cwd 了
    def _abs_text_file(d):
        tf = d.get("text_file")
        if not tf:
            return
        p = Path(str(tf)).expanduser()
        if not p.is_absolute():
            d["text_file"] = str((base / p).resolve())

    _abs_text_file(defaults)
    for it in items:
        if isinstance(it, dict):
            _abs_text_file(it)
    return items, defaults, src


def _fmt_secs(x):
    x = int(max(0, x))
    return "%ds" % x if x < 60 else "%dmin%02ds" % (x // 60, x % 60)


# ---- batch 内容指纹（--resume 的判据）------------------------------------------
SIG_STORE_NAME = ".indextts_sig.json"
_SIG_CACHE = {}


def _item_signature(eff, text, rate):
    """这条任务的"内容指纹"：文本 + 所有影响声音的参数 + 变速倍率。

    为什么不能只判"文件在不在"：配音返工的常态就是"第 12 句改词、第 30 句换音色"。
    只判文件存在会把它们当"已完成"跳过，交付上一版音频，manifest 还写 ok:true ——
    用户要到成片里才发现，而且没有任何错误信息可查。
    """
    payload = {
        "text": text,
        "preset": eff.get("preset") or "",
        "voice": eff.get("voice") or "",
        "emo_mode": eff.get("emo_mode"),
        "emo_text": eff.get("emo_text") or "",
        "emo_vector": eff.get("emo_vector") or "",
        "emo_audio": eff.get("emo_audio") or "",
        "emo_weight": eff.get("emo_weight"),
        "seg_tokens": eff.get("seg_tokens"),
        "interval_silence": eff.get("interval_silence"),
        "use_random": bool(eff.get("use_random")),
        "rate": round(float(rate or 1.0), 4),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sig_store(dirpath):
    """按目录维护 {绝对路径: {sig, ts}}，和产物放同一目录（跟着文件走，不依赖全局状态）。"""
    key = os.path.normcase(str(Path(dirpath).resolve()))
    store = _SIG_CACHE.get(key)
    if store is not None:
        return store
    p = Path(dirpath) / SIG_STORE_NAME
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        store = d.get("files") if isinstance(d, dict) else None
        if not isinstance(store, dict) or store is None:
            store = {}
    except (OSError, ValueError):
        store = {}
    _SIG_CACHE[key] = store
    return store


def _sig_get(out_path):
    rec = _sig_store(Path(out_path).parent).get(os.path.normcase(str(Path(out_path).resolve())))
    return rec if isinstance(rec, dict) else None


def _sig_put(out_path, sig):
    out_path = Path(out_path)
    store = _sig_store(out_path.parent)
    store[os.path.normcase(str(out_path.resolve()))] = {
        "sig": sig, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    p = out_path.parent / SIG_STORE_NAME
    try:
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "files": store}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:
        print("[batch] 警告：指纹文件写不进去（%s）；下次 --resume 会重跑这条" % e,
              file=sys.stderr)


def _apply_tempo(path, rate):
    """就地变速（保音高）。失败抛 SystemExit，由 batch 记到该条目的 error 上。"""
    data = Path(path).read_bytes()
    resp = post_multipart("/audio/tempo", {"rate": rate},
                          {"file": ("in.wav", data, "audio/wav")}, timeout=300)
    with resp:
        out_bytes = resp.read()
    if not out_bytes:
        raise SystemExit("ERROR: /audio/tempo returned empty body")
    Path(path).write_bytes(out_bytes)


def cmd_batch(args):
    """批量合成：一份任务清单 → 一份 manifest。

    为什么需要它：视频配音/有声书的真实形态是几十上百句。逐条起进程意味着每条都要
    重新 ensure 探活、重新抢一次文件锁，几十秒的固定开销叠起来比推理本身还贵。
    这里一个进程内串行跑完、锁只抢一次，并且**单条失败不中断**，
    最后统一汇报，调用方只需重跑失败的那几条（配合 --resume）。
    """
    items, defaults, src = _load_batch_spec(args.file)
    outdir = Path(args.outdir) if args.outdir else AGENT_OUT_DIR
    day = time.strftime("%Y%m%d")

    # 先把计划排好：解析出每条的最终路径，才能判断 --resume 要跳过哪些
    plan = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise SystemExit("ERROR: batch item #%d is not an object" % (i + 1))
        eff = dict(defaults)
        eff.update({k: v for k, v in it.items() if v is not None})
        rid = str(eff.get("id") or ("%04d" % (i + 1)))
        text = eff.get("text")
        if text is None and eff.get("text_file"):
            text = read_text_file(eff["text_file"])
        if text is None or not str(text).strip():
            plan.append({"id": rid, "pre": {"ok": False, "error": "no text (need 'text' or 'text_file')",
                                            "id": rid, "out": None}})
            continue
        out_path = Path(eff["out"]) if eff.get("out") else outdir / day / (_safe_name(rid, "%04d" % (i + 1)) + ".wav")
        plan.append({"id": rid, "text": str(text), "eff": eff, "out": out_path, "pre": None})

    # id / 输出路径撞车必须在这里拦住，不能交给后面的循环。
    # 后果是**静默**的：两条写同一个 .wav，后一条覆盖前一条，manifest 里两条都
    # ok 且 out 相同（用户拿到两段一模一样的音频）；配 --resume 时两条都会命中
    # 同一个存在的文件被跳过，其中一条从此永远生成不出来。
    # _safe_name 还会把 "第1句/开场" 和 "第1句:开场" 都归一成 "第1句_开场"，
    # 所以不能只比 id，必须比解析后的输出路径。
    seen_ids, seen_paths, clashes = {}, {}, []
    for pos, p in enumerate(plan, start=1):
        rid = p["id"]
        prev = seen_ids.get(rid)
        if prev is not None:
            clashes.append("id '%s' 在第 %d 项和第 %d 项重复" % (rid, prev, pos))
        else:
            seen_ids[rid] = pos
        if p.get("out") is not None:
            try:
                key = os.path.normcase(str(Path(p["out"]).resolve()))
            except OSError:
                key = os.path.normcase(str(p["out"]))
            prev = seen_paths.get(key)
            if prev is not None:
                clashes.append("第 %d 项和第 %d 项输出到同一个文件：%s" % (prev, pos, p["out"]))
            else:
                seen_paths[key] = pos
    if clashes:
        msg = ["", "=== batch 任务清单有冲突，已中止（一句都没合成）===", ""]
        msg += ["  - " + c for c in clashes]
        msg += ["",
                "同一个 id 会写同一个 .wav：后一条静默覆盖前一条，两条都报 ok 但内容相同；",
                "配 --resume 时两条都会被当成「已存在」跳过，其中一条永远生成不出来。",
                "修法：给每条一个唯一 id（line_001 / line_002 …），或显式用 out 区分。"]
        print("\n".join(msg), file=sys.stderr)
        if HEADLESS:
            print(json.dumps({"ok": False, "reason": "duplicate_ids", "clashes": clashes},
                             ensure_ascii=False))
        return 1

    # ---- --only：只重跑指定 id（返工单句时不必整批重来）----
    filtered_out = 0
    only = None
    if getattr(args, "only", None):
        only = {x for part in args.only.split(",") for x in part.split() if x}
        unknown = sorted(only - {p["id"] for p in plan})
        if unknown:
            raise SystemExit("ERROR: --only refers to unknown id(s): %s (清单里没有)"
                             % ", ".join(unknown))
        before = len(plan)
        plan = [p for p in plan if p["id"] in only]
        filtered_out = before - len(plan)

    # 指纹在这里就定好：--resume 靠它判断"内容变没变"
    for p in plan:
        if p.get("pre") is None:
            p["rate"] = float(p["eff"].get("rate") or args.rate or 1.0)
            p["sig"] = _item_signature(p["eff"], p["text"], p["rate"])

    def _skip_reason(p):
        """能跳过就返回原因，必须重跑就返回 None。

        判据顺序（任意一条不满足都重跑；宁可多跑一句，也不交付上一版音频）：
        文件在且非空 → wav 能读出时长（防上次写一半）→ 有指纹且指纹一致。
        """
        out = p["out"]
        if not (out.is_file() and out.stat().st_size > 0):
            return None
        info = wav_info(out)
        if not info or not info.get("duration"):
            return None                      # 空/截断的 wav，不能当完成
        if args.resume_any:
            return "exists (--resume-any)"
        rec = _sig_get(out)
        if rec is None:
            return None                      # 旧版本留下的文件没有指纹：重跑一次
        if rec.get("sig") != p["sig"]:
            return None                      # 文本/音色/参数/变速改过
        return "unchanged"

    skip_plan, skip_reasons, todo = {}, {}, []
    if args.resume or args.resume_any:
        for p in plan:
            if p.get("pre") is not None:
                continue
            why = _skip_reason(p)
            if why:
                skip_plan[p["id"]] = wav_info(p["out"]) or {}
                skip_reasons[p["id"]] = why
            else:
                todo.append(p)
    else:
        todo = [p for p in plan if p.get("pre") is None]

    n = len(plan)
    results = []
    done = failed = skipped = 0
    total_audio = 0.0
    interrupted = False
    t_start = time.time()
    synth_times = []
    last_ok = None

    # manifest 默认逐条落盘：100 句跑到第 80 句被 Ctrl-C / 超时杀掉时，
    # 前 79 条的时长与路径都还在，不用整批重跑再猜。
    manifest_path = None
    if getattr(args, "manifest_out", None):
        manifest_path = Path(args.manifest_out).expanduser()
    elif not getattr(args, "no_manifest", False):
        manifest_path = outdir / day / "manifest.json"

    def _manifest():
        return {
            "ok": (not interrupted) and failed == 0 and len(results) == n,
            "skill_version": SKILL_VERSION,
            "source": src,
            "manifest": str(manifest_path) if manifest_path else None,
            "count": n, "done": done, "skipped": skipped, "failed": failed,
            "not_run": n - len(results), "interrupted": interrupted,
            "filtered_out": filtered_out,
            "only": sorted(only) if only else None,
            "total_audio_seconds": round(total_audio, 3),
            "wall_seconds": round(time.time() - t_start, 1),
            "failed_ids": [r["id"] for r in results if not r["ok"]],
            "items": results,
        }

    def _write_manifest():
        if manifest_path is None:
            return
        try:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = manifest_path.with_name(manifest_path.name + ".tmp")
            tmp.write_text(json.dumps(_manifest(), ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, manifest_path)
        except OSError as e:
            print("[batch] 警告：manifest 写不进去（%s）；只保留 stdout 输出" % e, file=sys.stderr)

    # 全部命中 --resume 时不必拉起服务：重跑通常就是这种情况，秒回很重要
    if todo:
        if not ensure_server(allow_start=not args.no_start):
            return 1
        lock = SynthLock(SYNTH_LOCK_PATH, wait_seconds=args.lock_wait)
        if not lock.acquire():
            raise SystemExit("ERROR: another synth is still running after %ds (lock: %s). "
                             "Wait for it to finish, or pass --lock-wait 0 to fail fast."
                             % (args.lock_wait, SYNTH_LOCK_PATH))
    else:
        lock = None

    client_id = "batch_" + uuid.uuid4().hex[:8]
    try:
        for idx, p in enumerate(plan, start=1):
            rid = p["id"]
            if p["pre"] is not None:
                results.append(p["pre"])
                failed += 1
                print("[batch] %d/%d %s FAILED: %s" % (idx, n, rid, p["pre"]["error"]), file=sys.stderr)
                _write_manifest()
                if args.fail_fast:
                    break
                continue
            if rid in skip_plan:
                info = skip_plan[rid]
                dur = info.get("duration")
                total_audio += dur or 0
                skipped += 1
                results.append({"id": rid, "ok": True, "out": str(p["out"].resolve()),
                                "duration": dur, "sample_rate": info.get("sample_rate"),
                                "skipped": True, "reason": skip_reasons[rid]})
                print("[batch] %d/%d %s skipped (%s)" % (idx, n, rid, skip_reasons[rid]),
                      file=sys.stderr)
                _write_manifest()
                continue

            eff = p["eff"]
            rate = p["rate"]
            tmpdir = tempfile.mkdtemp(prefix="indextts_batch_")
            t0 = time.time()
            try:
                ns = _synth_ns(
                    preset=eff.get("preset"), voice=eff.get("voice"), client_id=client_id,
                    emo_mode=eff.get("emo_mode"), emo_text=eff.get("emo_text"),
                    emo_vector=eff.get("emo_vector"), emo_audio=eff.get("emo_audio"),
                    emo_weight=eff.get("emo_weight"),
                    seg_tokens=eff.get("seg_tokens"), interval_silence=eff.get("interval_silence"),
                    use_random=bool(eff.get("use_random")),
                )
                params, files, voice_ref, emo_mode = prepare_tts_request(ns, p["text"], tmpdir)
                result = post_tts(params, files, verbose=False)
                p["out"].parent.mkdir(parents=True, exist_ok=True)
                download(result.get("wav"), p["out"])
                if abs(rate - 1.0) > 1e-6:
                    if not 0.5 <= rate <= 2.0:
                        raise SystemExit("ERROR: rate must be within 0.5~2.0 (got %s)" % rate)
                    _apply_tempo(p["out"], rate)
                info = wav_info(p["out"]) or {}
                dur = info.get("duration")
                if not dur:
                    # 落盘了但读不出时长：宁可报错，也不能把坏文件记成 ok 再被 --resume 复用
                    raise SystemExit("ERROR: 落盘后 wav 读不出时长（文件可能不完整）")
                total_audio += dur
                done += 1
                _sig_put(p["out"], p["sig"])
                rec = {"id": rid, "ok": True, "out": str(p["out"].resolve()),
                       "duration": dur, "sample_rate": info.get("sample_rate"),
                       "rate": rate, "elapsed": result.get("elapsed"),
                       "voice": voice_ref, "emo_mode": emo_mode,
                       "mel_capped": bool(result.get("mel_capped", False)),
                       "bytes": p["out"].stat().st_size}
                results.append(rec)
                last_ok = dict(rec, text=p["text"])
                took = time.time() - t0
                synth_times.append(took)
                eta = ""
                if idx < n and synth_times:
                    remain = (n - idx) * (sum(synth_times) / len(synth_times))
                    eta = "  ETA≈%s" % _fmt_secs(remain)
                print("[batch] %d/%d %s ok %.2fs (%.1fs)%s" % (idx, n, rid, dur, took, eta),
                      file=sys.stderr)
                _write_manifest()
            except (SystemExit, OSError) as e:
                failed += 1
                msg = str(e) if isinstance(e, SystemExit) else "%s: %s" % (type(e).__name__, e)
                results.append({"id": rid, "ok": False, "error": msg, "out": None})
                print("[batch] %d/%d %s FAILED: %s" % (idx, n, rid, msg), file=sys.stderr)
                _write_manifest()
                if args.fail_fast:
                    break
            finally:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)
    except KeyboardInterrupt:
        interrupted = True
        print("\n[batch] 已中断（Ctrl-C）：已完成的条目和 manifest 都保留，"
              "重跑用 --resume 只补剩下的", file=sys.stderr)
    finally:
        if lock is not None:
            lock.release()

    manifest = _manifest()
    _write_manifest()
    print(json.dumps(manifest, ensure_ascii=False))
    print("[batch] %d 条：%d 成功 / %d 跳过 / %d 失败，音频共 %.1fs，用时 %.0fs"
          % (n, done, skipped, failed, total_audio, time.time() - t_start), file=sys.stderr)
    if manifest_path is not None:
        print("[batch] manifest: %s" % manifest_path, file=sys.stderr)

    # 让 last 也能找回 batch 的最后一条（此前只有 synth 会写 last.json）
    if last_ok is not None:
        _write_last_output({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "command": "batch",
            "id": last_ok["id"],
            "preset": None,
            "voice": last_ok.get("voice"),
            "emo_mode": last_ok.get("emo_mode"),
            "text_chars": len(last_ok.get("text") or ""),
            "text_preview": (last_ok.get("text") or "")[:100],
            "out": last_ok["out"],
            "server_path": None,
            "job_id": None,
            "elapsed": last_ok.get("elapsed"),
            "mel_capped": last_ok.get("mel_capped"),
        })
    return 0 if manifest["ok"] else 1


# ---- CLI ------------------------------------------------------------------------
def _add_model_config_args(sp):
    """model-config / model-restart 共用的参数表（服务端只认这 7 个键）。"""
    sp.add_argument("--fp16", default=None, metavar="on|off",
                    help="主模型 fp16（重建模型才生效）")
    sp.add_argument("--s2mel-fp16", dest="s2mel_fp16", default=None, metavar="on|off",
                    help="s2mel 阶段 fp16（重建才生效）")
    sp.add_argument("--w2v-fp16", dest="w2v_fp16", default=None, metavar="on|off",
                    help="w2v-bert fp16，8G 卡省约 1G（重建才生效）")
    sp.add_argument("--qwen-fp16", dest="qwen_fp16", default=None, metavar="on|off",
                    help="情感模型 fp16（重建才生效）")
    sp.add_argument("--cudnn-benchmark", dest="cudnn_benchmark", default=None, metavar="on|off",
                    help="进程级 torch 开关，立刻生效（不占显存）")
    sp.add_argument("--diffusion-steps", dest="diffusion_steps", type=int, default=None,
                    metavar="N", help="1~50，默认 25；下次生成生效（降它省显存也降质量）")
    sp.add_argument("--cfg-rate", dest="inference_cfg_rate", type=float, default=None,
                    metavar="R", help="0~1，默认 0.7；下次生成生效")


def build_parser():
    p = argparse.ArgumentParser(description="IndexTTS2 local server client (stdlib only).")
    p.add_argument("--headless", action="store_true",
                   help="machine mode: where a human would be asked, print JSON on stdout instead "
                        "(same as INDEXTTS_HEADLESS=1)")
    p.add_argument("--version", action="version", version="indextts2-tts %s" % SKILL_VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("locate", help="find (or set) the project dir; exit 3 = ask the user")
    sp.add_argument("--set", dest="set_path", default=None, metavar="PATH",
                    help="validate and remember a project dir (must contain server.py + indextts/)")
    sp.add_argument("--forget", action="store_true", help="forget the remembered project dir")
    sp.set_defaults(func=cmd_locate)

    sp = sub.add_parser("ensure", help="start server if not running, then health-check")
    sp.add_argument("--no-start", action="store_true", help="only check, never spawn")
    sp.set_defaults(func=cmd_ensure)

    sp = sub.add_parser("health", help="health probe")
    sp.set_defaults(func=cmd_health)

    sp = sub.add_parser("status", help="model state + system gauges（默认人话，--json 给原始响应）")
    sp.add_argument("--json", action="store_true", help="print the raw /status JSON")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("model", help="model config + whether it is generating（默认人话）")
    sp.add_argument("--json", action="store_true", help="print the raw /model JSON")
    sp.set_defaults(func=cmd_model)

    sp = sub.add_parser("unload", help="unload the model to free VRAM")
    sp.add_argument("--force", action="store_true",
                    help="interrupt a running generation and unload (otherwise the server refuses with 409)")
    sp.set_defaults(func=cmd_unload)

    sp = sub.add_parser("model-config",
                        help="change model config (precision switches only take effect after model-restart)")
    _add_model_config_args(sp)
    sp.set_defaults(func=cmd_model_config)

    sp = sub.add_parser("model-restart",
                        help="rebuild the model, applying any config flags passed (30~60s, blocked while generating)")
    _add_model_config_args(sp)
    sp.set_defaults(func=cmd_model_restart)

    sp = sub.add_parser("presets", help="list presets")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_presets)

    sp = sub.add_parser("preset", help="show one preset detail")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_preset)

    sp = sub.add_parser("preset-save", help="save a reference audio as a named preset voice")
    sp.add_argument("--name", required=True, help="preset name (also used as --preset later)")
    sp.add_argument("--audio", required=True,
                    help="reference audio path, or a built-in example name (e.g. voice_01.wav)")
    sp.add_argument("--overwrite", action="store_true", help="replace an existing preset with the same name")
    sp.add_argument("--emo-mode", type=int, default=None, choices=[0, 1, 2, 3],
                    help="0=like reference, 1=emotion audio, 2=vector, 3=emotion text")
    sp.add_argument("--emo-audio", default=None, help="emotion reference audio (mode 1)")
    sp.add_argument("--emo-text", default=None, help="emotion description (mode 3)")
    sp.add_argument("--emo-vector", default=None,
                    help="8 values: %s (comma or bracket list)" % ",".join(EMO_VEC_NAMES))
    sp.add_argument("--emo-weight", type=float, default=None)
    sp.add_argument("--use-random", action="store_true")
    sp.add_argument("--top-p", type=float, default=None)
    sp.add_argument("--top-k", type=int, default=None)
    sp.add_argument("--temperature", type=float, default=None)
    sp.add_argument("--length-penalty", type=float, default=None)
    sp.add_argument("--num-beams", type=int, default=None)
    sp.add_argument("--repetition-penalty", type=float, default=None)
    sp.add_argument("--max-mel-tokens", type=int, default=None)
    sp.add_argument("--seg-tokens", type=int, default=None)
    sp.set_defaults(func=cmd_preset_save)

    sp = sub.add_parser("examples", help="list built-in example voices")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_examples)

    sp = sub.add_parser("segments", help="preview text segmentation (or emit a batch task list)")
    sp.add_argument("--text", default=None)
    sp.add_argument("--text-file", default=None, help="UTF-8 text file (BOM tolerated)")
    sp.add_argument("--seg-tokens", type=int, default=120)
    sp.add_argument("--out", default=None, metavar="TASKS.json",
                    help="write a batch task list ({\"defaults\":...,\"items\":[...]}) to this path")
    sp.add_argument("--preset", default=None, help="preset to put into the generated defaults")
    sp.add_argument("--voice", default=None, help="voice to put into the generated defaults")
    sp.add_argument("--rate", type=float, default=None, help="rate to put into the generated defaults")
    sp.set_defaults(func=cmd_segments)

    sp = sub.add_parser("tempo", help="change speed keeping pitch (no re-synthesis, no GPU)")
    sp.add_argument("--file", required=True, metavar="PATH", help="input audio (wav/mp3/flac/ogg/m4a)")
    sp.add_argument("--rate", required=True, type=float, metavar="RATE", help="speed factor 0.5~2.0 (1.2 = 20%% faster)")
    sp.add_argument("--out", default=None, help="output wav path (default: <out dir>/<date>/<name>_tempo<rate>.wav)")
    sp.add_argument("--outdir", default=None, help="default output dir when --out is not given")
    sp.add_argument("--no-start", action="store_true", help="fail instead of spawning server")
    sp.set_defaults(func=cmd_tempo)

    sp = sub.add_parser("selftest", help="read-only health check (never synthesizes, never touches GPU)")
    sp.set_defaults(func=cmd_selftest)

    sp = sub.add_parser("doctor", help="end-to-end capability check (does one real synthesis)")
    sp.add_argument("--no-synth", action="store_true",
                    help="skip the smoke synthesis (interface checks only)")
    sp.add_argument("--force", action="store_true", help="ignore the 30-min smoke result cache")
    sp.add_argument("--preset", default=None, help="use this preset for the smoke synthesis")
    sp.add_argument("--voice", default=None, help="use this reference audio for the smoke synthesis")
    sp.add_argument("--text", default=None, help="smoke text (default: %s)" % DOCTOR_TEXT)
    sp.add_argument("--outdir", default=None, help="where to keep the sample wav")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("capabilities", help="machine-readable capability manifest for upstream skills")
    sp.set_defaults(func=cmd_capabilities)

    sp = sub.add_parser("api", help="interface audit: server openapi vs the endpoints this skill adapts")
    sp.add_argument("--json", action="store_true", help="machine-readable report")
    sp.add_argument("--strict", action="store_true",
                    help="exit 1 when a core endpoint is unadapted or no longer exists on the server")
    sp.set_defaults(func=cmd_api)

    sp = sub.add_parser("batch", help="synthesize many lines from a JSON task list")
    sp.add_argument("--file", "-f", default=None, metavar="PATH",
                    help="JSON task list, '-' or omitted = stdin")
    sp.add_argument("--outdir", default=None, help="default output dir (default: %s)" % AGENT_OUT_DIR)
    sp.add_argument("--rate", type=float, default=1.0,
                    help="speed factor applied to every item after synthesis (0.5~2.0)")
    sp.add_argument("--resume", action="store_true",
                    help="skip only items whose text+voice+params are unchanged (content fingerprint)")
    sp.add_argument("--resume-any", action="store_true",
                    help="legacy resume: skip whenever the output file exists (ignores content)")
    sp.add_argument("--only", default=None, metavar="ID[,ID]",
                    help="run only these ids (re-work a few lines without touching the rest)")
    sp.add_argument("--manifest-out", default=None, metavar="PATH",
                    help="where to write the manifest (default: <outdir>/<date>/manifest.json)")
    sp.add_argument("--no-manifest", action="store_true", help="do not write the manifest to disk")
    sp.add_argument("--fail-fast", action="store_true", help="stop at the first failure")
    sp.add_argument("--lock-wait", type=int, default=3600, metavar="SEC",
                    help="max seconds to wait for the global synth lock (default 3600; 0=fail fast)")
    sp.add_argument("--no-start", action="store_true", help="fail instead of spawning server")
    sp.set_defaults(func=cmd_batch)

    sp = sub.add_parser("history", help="generation history of this server run")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("stop", help="stop a queued/running job")
    sp.add_argument("job_id")
    sp.add_argument("--keep-model", action="store_true",
                    help="keep the model loaded (skip the ~10-20s rebuild; next synth starts immediately)")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("glossary", help="list / add / delete / clear glossary terms")
    sp.add_argument("--add", default=None, metavar="TERM")
    sp.add_argument("--zh", default=None, metavar="READING_ZH")
    sp.add_argument("--en", default=None, metavar="READING_EN")
    sp.add_argument("--delete", default=None, metavar="TERM")
    sp.add_argument("--clear", action="store_true", help="delete every term (requires --yes)")
    sp.add_argument("--yes", action="store_true", help="confirm --clear (irreversible)")
    sp.set_defaults(func=cmd_glossary)

    sp = sub.add_parser("choose", help="pick a preset (interactive on tty; exit 2 otherwise)")
    sp.set_defaults(func=cmd_choose)

    sp = sub.add_parser("last", help="show the most recent generated audio (default out dir + last.json)")
    sp.add_argument("--open", action="store_true", help="open the containing folder in the file manager")
    sp.set_defaults(func=cmd_last)

    sp = sub.add_parser("synth", help="generate speech (blocks until done)")
    sp.add_argument("--preset", default=None, help="preset name (reference audio + params)")
    sp.add_argument("--voice", default=None,
                    help="reference audio path, or a built-in example name (e.g. voice_01.wav; "
                         "fetched from the server when not present locally)")
    sp.add_argument("--text", default=None)
    sp.add_argument("--text-file", default=None, help="UTF-8 text file (BOM tolerated; alternative to --text)")
    sp.add_argument("--out", default=None,
                    help="output wav path (default: <INDEXTTS_OUT_DIR>/<date>/<server filename>)")
    sp.add_argument("--outdir", default=None,
                    help="default output dir when --out is not given (default: %s)" % AGENT_OUT_DIR)
    sp.add_argument("--emo-mode", type=int, default=None, choices=[0, 1, 2, 3],
                    help="0=like reference, 1=emotion audio, 2=vector, 3=emotion text")
    sp.add_argument("--emo-audio", default=None, help="emotion reference audio (mode 1)")
    sp.add_argument("--emo-text", default=None, help="emotion description (mode 3)")
    sp.add_argument("--emo-vector", default=None,
                    help="8 values: %s (comma or bracket list)" % ",".join(EMO_VEC_NAMES))
    sp.add_argument("--emo-weight", type=float, default=None)
    sp.add_argument("--use-random", action="store_true")
    sp.add_argument("--seg-tokens", type=int, default=None)
    sp.add_argument("--sample", action="store_true", default=None, help="force do_sample=true")
    sp.add_argument("--no-sample", action="store_true", help="force do_sample=false")
    sp.add_argument("--top-p", type=float, default=None)
    sp.add_argument("--top-k", type=int, default=None)
    sp.add_argument("--temperature", type=float, default=None)
    sp.add_argument("--length-penalty", type=float, default=None)
    sp.add_argument("--num-beams", type=int, default=None)
    sp.add_argument("--repetition-penalty", type=float, default=None)
    sp.add_argument("--max-mel-tokens", type=int, default=None)
    sp.add_argument("--interval-silence", type=int, default=None, metavar="MS",
                    help="silence inserted between segments, 10-500 ms (default 200); only matters for multi-segment text")
    sp.add_argument("--naming", default=None, choices=["title_time", "title", "default"])
    sp.add_argument("--client-id", default="agent_" + uuid.uuid4().hex[:8])
    sp.add_argument("--lock-wait", type=int, default=3600, metavar="SEC",
                    help="max seconds to wait for the global synth lock (default 3600; 0=fail fast if busy)")
    sp.add_argument("--no-start", action="store_true", help="fail instead of spawning server")
    sp.set_defaults(func=cmd_synth)
    return p


def main(argv=None):
    global HEADLESS
    args = build_parser().parse_args(argv)
    if getattr(args, "headless", False):
        HEADLESS = True
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
