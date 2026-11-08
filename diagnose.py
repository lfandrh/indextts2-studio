#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IndexTTS2 startup diagnostics (human-readable layer over the raw log).

Design contract - please keep these when extending this file:

1. ADD, NEVER REPLACE. The Chinese explanation is an extra layer on top of
   the original traceback/log. The raw text is printed in full, unedited and
   untruncated. That raw text is what an AI needs in order to debug.
2. NARROW EVIDENCE ONLY. A diagnosis is emitted only when there is hard
   evidence. Anything unrecognised falls through to an honest "could not
   identify" message. A confidently wrong guess (e.g. telling the user to
   install dependencies when the project's own source tree was deleted) is
   worse than an untranslated traceback. Prefer missing a rule to guessing.
3. NO AUTOMATIC REPAIR. We only print commands. No pip install, no uv sync,
   no git restore, no network access. The user (or an AI) decides.
4. SELF-CONTAINED REPORT. Every failure also writes logs/last_error.txt with
   structured context + Chinese explanation + the complete raw log, so it
   can be handed to an AI instead of being selected/copied from a console.
5. STDLIB ONLY. This tool must work on a machine whose project dependencies
   are broken - that is the entire point of it.
6. NEVER GUESS AN ENCODING, AND NEVER LIE ABOUT IT. The log is decoded as
   strict UTF-8; undecodable bytes become visible, reversible \\xNN escapes.
   A whole-file gb18030 fallback is forbidden: gb18030 accepts almost any
   byte pair, so a single stray GBK byte used to turn an otherwise valid
   UTF-8 log into silent mojibake while the report still claimed the log was
   "unedited". When the presented text is not byte-identical to the log, the
   report says so and appends a base64 copy of the untouched raw bytes.
   Alternative codecs are only used when a human explicitly asks for them.

The two .bat files stay pure ASCII on purpose (cmd.exe decodes .bat files
with the OEM code page, so UTF-8 Chinese inside them breaks parsing). All
Chinese text lives here, in a UTF-8 Python source file.

Usage (normally called by start_server.bat / stop_server.bat):

    python diagnose.py --code bad_port --port 7860
    python diagnose.py --analyze --rc 1 --log logs/server_bg.log \
        --port 7860 --runner .venv/Scripts/python.exe --run-kind python
    python diagnose.py --analyze --rc 1 --json     # machine-readable
    python diagnose.py --analyze --rc 1 --log-encoding gb18030  # GBK log

Exit codes: 0 = ok/preflight clean, 2 = preflight found a problem,
1 = diagnostics tool itself could not run (caller falls back to ASCII text).
"""

from __future__ import annotations

import argparse
import base64
import codecs
import json
import os
import platform
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent
DEFAULT_LOG = ROOT / "logs" / "server_bg.log"
DEFAULT_REPORT = ROOT / "logs" / "last_error.txt"

RULE_VERSION = "indextts-start-error/1"
SEP = "=" * 66
THIN = "-" * 66

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def configure_stdout() -> None:
    """Never let an unencodable character kill the diagnostics output.

    Keep the console's native encoding (setting UTF-8 here would produce
    mojibake on a GBK console); only change the error handler.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


UTF8_BOM = b"\xef\xbb\xbf"
AUTO_ENCODINGS = frozenset(
    ("", "auto", "utf-8", "utf8", "utf_8", "utf-8-sig", "utf_8_sig")
)


def _auto_decode(
    data: bytes, body: bytes, had_bom: bool, prefix: str = ""
) -> DecodedLog:
    """Strict UTF-8, with undecodable bytes shown as visible \\xNN escapes.

    ``backslashreplace`` acts per byte, so valid UTF-8 text surrounding a bad
    byte survives unchanged. That is the whole point: no whole-file encoding
    guess, therefore no silent mojibake and no claim we cannot back up.
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("utf-8", errors="backslashreplace")
        note = (
            "日志不是完整 UTF-8：合法 UTF-8 内容原样保留，无法解码的字节以 \\xNN "
            "转义显示（可见、可逆，不会静默变成乱码）。"
        )
        return DecodedLog(
            text, data, "utf-8+backslashreplace", False, had_bom,
            (prefix + " " + note).strip(),
        )
    return DecodedLog(text, data, "utf-8", True, had_bom, prefix or "日志为 UTF-8，原文无损。")


def decode_log_bytes(data: bytes, encoding: str = "auto") -> DecodedLog:
    """Decode a log without ever silently corrupting it.

    Old behaviour tried the WHOLE file as utf-8-sig and then as gb18030.
    gb18030 accepts almost any byte pair, so one stray GBK byte made an
    otherwise valid UTF-8 log decode as mojibake -- silently, with no U+FFFD
    warning, while the report still claimed the log was "unedited".

    * ``auto`` (default): strict UTF-8; bad bytes become visible \\xNN escapes.
    * an explicit codec name: decode the whole file with it. Never guessed.
    * either way the untouched bytes are kept for a byte-exact base64 copy.
    """
    had_bom = data.startswith(UTF8_BOM)
    body = data[3:] if had_bom else data
    requested = (encoding or "auto").strip()
    if requested.lower() in AUTO_ENCODINGS:
        return _auto_decode(data, body, had_bom)

    # An explicit codec is a decision made by a human, not a guess made here.
    # Diagnostics must keep running even if the codec name is odd or broken.
    try:
        codecs.lookup(requested)
        text = body.decode(requested)
        if not isinstance(text, str):
            raise ValueError("codec did not return text")
    except Exception:
        return _auto_decode(
            data, body, had_bom,
            "日志编码 %r 不可用或无法严格解码，已回退为 UTF-8 处理。" % requested,
        )
    lossless = text.encode("utf-8") == body
    note = "整份日志按 %s 解码。" % requested
    if not lossless:
        note += "文字已转成 UTF-8 显示，字节与原文不同。"
    return DecodedLog(text, data, requested, lossless, had_bom, note)


def decode_log(data: bytes) -> str:
    """Backwards-compatible helper: readable text only, default decoding."""
    return decode_log_bytes(data).text


def read_log(path: Path, encoding: str = "auto") -> DecodedLog:
    try:
        return decode_log_bytes(path.read_bytes(), encoding)
    except (OSError, ValueError):
        return DecodedLog("", b"", "unavailable", True, False, "日志文件无法读取或不存在。")


def scan_project(root: Path) -> list[str]:
    """Pure filesystem checks. No imports, no network, sub-millisecond."""
    issues: list[str] = []
    if not (root / "server.py").is_file():
        issues.append("server.py 不存在")
    pkg = root / "indextts"
    if not pkg.is_dir():
        issues.append("indextts\\ 目录不存在")
    elif not list(pkg.glob("*.py")):
        issues.append("indextts\\ 目录下没有任何 .py 文件")
    utils = pkg / "utils"
    if pkg.is_dir() and not utils.is_dir():
        issues.append("indextts\\utils\\ 目录不存在")
    elif utils.is_dir() and not list(utils.glob("*.py")):
        issues.append("indextts\\utils\\ 下没有任何 .py 文件")
    web = root / "web"
    if not web.is_dir():
        issues.append("web\\ 目录不存在")
    elif not any(web.iterdir()):
        issues.append("web\\ 目录是空的")
    return issues


def sync_command(ctx: Context) -> str:
    """Give a command that matches the runner that was actually selected."""
    runner = ctx.runner.strip('"')
    if ctx.run_kind == "uv" or runner.lower().endswith("uv.exe") or runner == "uv":
        return "uv sync --extra server"
    if runner and runner.lower().endswith("python.exe"):
        return '"%s" -m pip install -e ".[server]"' % runner
    return "uv sync --extra server   (或者用 .venv 里的 python -m pip install -e \".[server]\")"


def alt_port(port: str) -> str:
    return str(int(port) + 1) if port.isdigit() and int(port) < 65535 else "7861"


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecodedLog:
    """Readable text plus the untouched bytes it was produced from.

    ``lossless`` is True only when ``text.encode("utf-8")`` equals the log
    bytes (after removing a leading UTF-8 BOM). When False, the report must
    say so and carry ``raw`` as base64 for byte-exact AI forensics.
    """

    text: str
    raw: bytes
    encoding: str = "utf-8"
    lossless: bool = True
    had_bom: bool = False
    note: str = ""

    @property
    def b64(self) -> str:
        return base64.b64encode(self.raw).decode("ascii")


@dataclass
class Context:
    root: Path = ROOT
    code: str = ""
    rc: int | None = None
    log_path: Path = DEFAULT_LOG
    port: str = ""
    host: str = "127.0.0.1"
    runner: str = ""
    run_kind: str = ""
    report_path: Path = DEFAULT_REPORT
    log_text: str = ""
    log_info: DecodedLog | None = None

    @property
    def has_traceback(self) -> bool:
        return "Traceback (most recent call last)" in self.log_text


@dataclass(frozen=True)
class Diagnosis:
    rule: str
    title: str
    detail: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    note: str = ""
    severity: str = "error"  # error | info

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "title": self.title,
            "detail": list(self.detail),
            "actions": list(self.actions),
            "note": self.note,
            "severity": self.severity,
        }


# ---------------------------------------------------------------------------
# branch messages: .bat calls these with --code when it already knows why
# ---------------------------------------------------------------------------


def code_diagnosis(ctx: Context) -> Diagnosis | None:
    code = ctx.code
    port = ctx.port or "7860"
    log = str(ctx.log_path)
    sync = sync_command(ctx)

    if code == "bad_port":
        return Diagnosis(
            "bad_port",
            "端口号填写不正确",
            (
                "收到的是 %r，端口必须是 1 到 65535 之间的纯数字。" % port,
                "为避免参数被当成命令执行，非数字输入一律拒绝。",
            ),
            ("示例： start_server.bat 7860", "示例： start_server.bat 7861"),
        )

    if code == "no_runner":
        return Diagnosis(
            "no_runner",
            "找不到 Python 运行环境",
            (
                "启动脚本没有找到 runtime\\python.exe、.venv\\Scripts\\python.exe，也没有找到 uv。",
                "没有 Python 就跑不了 IndexTTS2；这一步还没有开始启动服务。",
            ),
            (
                "安装 uv（https://docs.astral.sh/uv/）后，在项目目录执行： %s" % sync,
                "或者把 Python 3.10 / 3.11 放到项目下的 .venv\\ 或 runtime\\ 目录",
                "装好之后重新运行 start_server.bat",
            ),
        )

    if code == "no_windows":
        return Diagnosis(
            "no_windows",
            "找不到 Windows 系统目录（System32）",
            (
                "连 System32 都定位不到，通常是 SystemRoot 环境变量异常，或者 Windows 安装本身有问题。",
                "cmd、PowerShell、netstat 都依赖它，脚本无法继续。",
            ),
            (
                "检查 SystemRoot 是否指向 C:\\Windows",
                "换一个正常的 Windows 会话重新运行",
            ),
        )

    if code == "port_busy":
        return Diagnosis(
            "port_busy",
            "端口被其他程序占用，脚本没有强杀它",
            (
                "端口 %s 上有进程在监听，但它的命令行不是本项目的 server.py。" % port,
                "为了避免误关你正在用的其他软件，启动已中止，什么都没有被结束。",
            ),
            (
                "查看是谁占用了端口： netstat -ano | findstr :%s" % port,
                "确认后手动结束它，或换个端口启动： start_server.bat %s" % alt_port(port),
                "如果那是旧脚本残留的服务，运行 stop_server.bat 再试",
            ),
        )

    if code == "log_busy":
        return Diagnosis(
            "log_busy",
            "日志文件被占用，无法写入",
            (
                "无法清空或写入日志文件： %s" % log,
                "最常见的原因是旧版启动脚本留下的后台服务还开着，独占了这个文件。",
                "这种情况下直接启动会静默失败（Python 甚至会以退出码 0 结束），所以这里主动停下。",
            ),
            (
                "运行 stop_server.bat 结束旧实例，然后重新启动",
                '或者临时换个日志： set "INDEXTTS_LOG=%TEMP%\\indextts.log"',
                "注意：这个环境变量只在当前命令行窗口有效",
            ),
        )

    if code == "watcher_timeout":
        return Diagnosis(
            "watcher_timeout",
            "服务进程还在，但 60 秒内没有监听端口",
            (
                "端口 %s 一直没起来，服务进程本身也还没有退出。" % port,
                "常见于首次下载模型、加载很慢，或者卡在依赖 / 网络请求上。",
                "这不是崩溃，脚本不会自动结束服务；请根据日志判断进度。",
            ),
            (
                "查看日志： %s" % log,
                "若日志长时间不再增长，再关闭窗口结束服务",
                '不想自动开浏览器可以： set "INDEXTTS_NO_BROWSER=1"',
            ),
            severity="info",
        )

    if code in ("preflight_source", "preflight_server"):
        issues = scan_project(ctx.root)
        listed = tuple("  - " + item for item in issues) or ("  - 检查未返回具体项",)
        return Diagnosis(
            code,
            "项目文件不完整，服务没有启动",
            (
                "启动前的快速检查发现问题：",
                *listed,
                "这种情况安装依赖解决不了，服务即使启动也会在 import 阶段失败。",
                "因为先检查再启动，你原来的服务没有被结束，日志文件也没有被改动。",
            ),
            (
                "在项目目录执行： git restore -- indextts web examples",
                "再用 git status 看还有哪些是未跟踪的自定义文件（git restore 恢复不了它们）",
                "恢复完成后重新运行 start_server.bat",
            ),
        )

    if code == "stop_refused":
        return Diagnosis(
            "stop_refused",
            "没有停止任何进程：端口上的不是本项目服务",
            (
                "端口 %s 上的进程，命令行里没有 server.py。" % port,
                "停止脚本拒绝结束它，以免误关其他软件。",
            ),
            (
                "查看占用者： netstat -ano | findstr :%s" % port,
                "确认是别的服务后，请自行处理，不要盲目结束进程",
            ),
        )

    if code == "stop_still_busy":
        return Diagnosis(
            "stop_still_busy",
            "端口仍然被占用",
            (
                "发出停止请求后，端口 %s 依然处于监听状态。" % port,
                "可能是子进程还没退出，或者由另一个进程重新占用了端口。",
            ),
            (
                "等几秒再运行一次 stop_server.bat",
                "仍不行就手动查： netstat -ano | findstr :%s" % port,
                "重启服务前请先确认端口确实释放",
            ),
        )

    if code == "stop_bad_port":
        return Diagnosis(
            "stop_bad_port",
            "端口号填写不正确",
            ("收到的是 %r，端口必须是 1 到 65535 之间的纯数字。" % port,),
            ("示例： stop_server.bat 7860", "示例： stop_server.bat 7861"),
        )

    if code == "stop_no_windows":
        return Diagnosis(
            "stop_no_windows",
            "找不到 Windows 系统目录（System32）",
            ("停止脚本依赖 netstat / PowerShell，缺少 System32 无法安全校验进程归属。",),
            ("检查 SystemRoot 环境变量后重试",),
        )

    if code == "stop_ok":
        return Diagnosis(
            "stop_ok",
            "服务已停止",
            (
                "端口 %s 上的本项目服务已经结束，端口也已释放。" % port,
                "确认过进程命令行包含 server.py，没有碰其他程序。",
            ),
            ("现在可以重新运行 start_server.bat。",),
            severity="info",
        )

    if code == "stop_nothing":
        return Diagnosis(
            "stop_nothing",
            "端口上没有服务在运行",
            ("端口 %s 没有任何进程监听，服务本来就是停止状态。" % port,),
            ("无需进一步操作。",),
            severity="info",
        )

    return None


# ---------------------------------------------------------------------------
# log rules: narrow, evidence-based, ordered most specific first
# ---------------------------------------------------------------------------

RE_MISSING = re.compile(
    r"(?:ModuleNotFoundError|ImportError):\s*No module named ['\"]([^'\"]+)['\"]"
)
RE_STARTED = re.compile(
    r"Uvicorn running|Application startup complete|IndexTTS2 server starting"
    r"|Light components ready|Server is ready",
    re.I,
)


def missing_modules(ctx: Context) -> list[str]:
    return RE_MISSING.findall(ctx.log_text)


def _is_own_module(name: str) -> bool:
    return name == "indextts" or name.startswith("indextts.")


def rule_project_deleted(ctx: Context) -> Diagnosis | None:
    """The real incident: indextts/ emptied -> 'No module named indextts.utils'.

    Installing dependencies here would be actively misleading, so this rule
    must run before the dependency rule.
    """
    mods = missing_modules(ctx)
    if not any(_is_own_module(m) for m in mods):
        return None
    issues = scan_project(ctx.root)
    if not issues:
        return None  # source tree looks intact: let the per-module rule decide
    return Diagnosis(
        "project_deleted",
        "项目源码不完整，不是缺少依赖",
        (
            "日志里的报错是： %s" % ", ".join("No module named '%s'" % m for m in mods),
            "同时检查到项目文件本身有问题：",
            *("  - " + item for item in issues),
            "这是项目文件被删除或没拷全，安装依赖包解决不了。",
        ),
        (
            "在项目目录执行： git restore -- indextts web examples",
            "再运行 git status，确认有没有未提交 / 未跟踪的自定义模块需要单独找回",
            "恢复后重新运行 start_server.bat",
        ),
        note="已核对文件系统，结论基于“报错 + 目录确实缺失”两个证据。",
    )


def rule_module_file_missing(ctx: Context) -> Diagnosis | None:
    """e.g. glossary_store.py was written but never committed, then lost."""
    mods = [m for m in missing_modules(ctx) if _is_own_module(m)]
    if not mods:
        return None
    absent: list[str] = []
    for name in mods:
        rel = Path(*name.split("."))
        mod_file = ctx.root / (str(rel) + ".py")
        pkg_init = ctx.root / rel / "__init__.py"
        if not mod_file.is_file() and not pkg_init.is_file():
            absent.append(str(rel).replace(os.sep, ".") + "  (" + str(rel) + ".py)")
    if not absent:
        return None
    return Diagnosis(
        "module_file_missing",
        "项目里缺少某个模块文件",
        (
            "日志显示找不到： %s" % ", ".join(mods),
            "对应的文件在项目中确实不存在：",
            *("  - " + item for item in absent),
            "这类文件常见于是开发中新建、还没提交进 Git 的模块；git restore 也恢复不了。",
            "同样不要去装依赖，pip 里没有这个模块。",
        ),
        (
            "运行 git status 和 git log --all 确认它是否曾经入库",
            "从备份 / 回收站 / 其他副本找回，或按调用方重新实现该模块",
            "也可以临时把 server.py 里对应的 import 退回上一版",
        ),
        note="结论基于“import 报错 + 对应 .py 文件确实不存在”两个证据。",
    )


def rule_dependency_missing(ctx: Context) -> Diagnosis | None:
    mods = [m for m in missing_modules(ctx) if not _is_own_module(m)]
    if not mods:
        return None
    if scan_project(ctx.root):
        return None  # source tree broken: do not blame third-party packages
    names = ", ".join(sorted(set(mods)))
    return Diagnosis(
        "dependency_missing",
        "缺少 Python 依赖包",
        (
            "日志显示 import 失败： %s" % names,
            "项目源码目录检查正常，所以问题出在运行环境缺包。",
        ),
        (
            "按当前运行环境安装完整依赖： %s" % sync_command(ctx),
            "完整依赖清单见 pyproject.toml 的 [project].dependencies 和 server extra",
            "注意：import 名和 pip 包名经常不同（例如 cv2 / opencv-python），不要直接 pip install 报错里的名字",
            "装完重新运行 start_server.bat",
        ),
    )


def rule_port_in_use(ctx: Context) -> Diagnosis | None:
    if not re.search(
        r"WinError 10048|Address already in use"
        r"|Only one usage of each socket address|\[Errno 98\]|error while attempting to bind",
        ctx.log_text,
        re.I,
    ):
        return None
    port = ctx.port or "7860"
    return Diagnosis(
        "port_in_use",
        "服务端口被占用，绑定失败",
        (
            "服务进程启动到监听端口时失败，报错是典型的端口冲突。",
            "启动脚本会先清理旧的 server.py 实例，所以这里通常是别的程序占了 %s。" % port,
        ),
        (
            "查占用者： netstat -ano | findstr :%s" % port,
            "换个端口启动： start_server.bat %s" % alt_port(port),
        ),
    )


def rule_config_missing(ctx: Context) -> Diagnosis | None:
    m = re.search(r"FileNotFoundError:[^\r\n]*", ctx.log_text)
    if not m:
        return None
    line = m.group(0)
    if not re.search(
        r"checkpoints|model_config|config|\.json|\.ya?ml|\.pth|\.safetensors|\.pt\b|\.bin\b",
        line,
        re.I,
    ):
        return None
    return Diagnosis(
        "config_missing",
        "配置或模型文件找不到",
        (
            "服务在读取配置 / 权重文件时失败：",
            "  " + line.strip(),
            "通常是模型权重没下载全，或者配置文件路径被改动。",
        ),
        (
            "核对 checkpoints\\ 目录和 model_config.json 是否存在",
            "按 README 的模型下载步骤补齐权重文件",
            "不要手动改路径，先看日志里报的是哪一个具体文件",
        ),
    )


def rule_cuda_oom(ctx: Context) -> Diagnosis | None:
    if not re.search(
        r"CUDA out of memory|OutOfMemoryError|CUDA error: out of memory"
        r"|not enough memory",
        ctx.log_text,
        re.I,
    ):
        return None
    return Diagnosis(
        "cuda_oom",
        "显存不足",
        (
            "日志里有显存分配失败（CUDA out of memory）。",
            "这是显卡容量问题，不是依赖缺失。",
        ),
        (
            "编辑 start_server.bat，把 FP16 / S2MEL_FP16 / W2V_FP16 都设为 1",
            "把 DIFFUSION_STEPS 调小（例如 16），关闭其他占用显存的程序",
            "保持 CUDNN_BENCHMARK=0，不要改",
        ),
    )


def rule_cuda_missing(ctx: Context) -> Diagnosis | None:
    if not re.search(
        r"Torch not compiled with CUDA|no CUDA-capable device|CUDA driver"
        r"|Found no NVIDIA driver|CUDA initialization|No CUDA runtime",
        ctx.log_text,
        re.I,
    ):
        return None
    return Diagnosis(
        "cuda_missing",
        "CUDA / 显卡环境不可用",
        (
            "日志显示 PyTorch 找不到可用的 CUDA 设备或驱动。",
        ),
        (
            "运行 nvidia-smi 确认驱动和显卡是否正常",
            "确认安装的是 CUDA 版 PyTorch，而不是 CPU 版",
            "重装依赖可用： %s" % sync_command(ctx),
        ),
    )


def rule_permission(ctx: Context) -> Diagnosis | None:
    if not re.search(
        r"PermissionError|Access is denied|WinError 5\b|not permitted",
        ctx.log_text,
        re.I,
    ):
        return None
    return Diagnosis(
        "permission",
        "文件访问权限不足",
        ("日志里有权限被拒绝的报错，通常是目录只读，或被安全软件拦截。",),
        (
            "确认项目目录不在受保护的位置，且当前用户可写",
            "检查杀毒软件 / 受控文件夹访问是否拦截了 python.exe",
            "必要时用管理员身份打开命令行再试",
        ),
    )


def rule_silent_exit(ctx: Context) -> Diagnosis | None:
    if ctx.rc not in (0, None):
        return None
    if len(ctx.log_text.strip()) > 200 or RE_STARTED.search(ctx.log_text):
        return None
    return Diagnosis(
        "silent_exit",
        "服务没有留下任何输出就退出了",
        (
            "进程退出码是 %s，日志几乎是空的。" % ctx.rc,
            "常见原因：日志重定向失败、Python 被安全软件拦截、或进程被外部提前结束。",
            "启动脚本已经做过日志可写性检查，若仍看到这个提示，说明是运行环境层面的问题。",
        ),
        (
            "确认 %s 可写" % ctx.log_path,
            "在项目目录手动运行 server.py，看终端是否直接报错",
            "检查安全软件是否拦截了 python.exe",
        ),
    )


def rule_server_exception(ctx: Context) -> Diagnosis | None:
    if not ctx.has_traceback:
        return None
    return Diagnosis(
        "server_exception",
        "服务启动时抛出了未识别的异常",
        (
            "日志里有完整的 Python 异常栈，但没有匹配到任何已知问题。",
            "没有把握给出准确结论，所以这里不做猜测。",
            "下面是完整原始日志；若含非 UTF-8 字节，取证包会另附字节级原文，请整个发给 AI 排查。",
        ),
        (
            "把下面保存的取证包文件整个发给 AI",
            "或按日志最后一行的异常类型搜索 / 提问",
        ),
    )


def rule_started_then_exit(ctx: Context) -> Diagnosis | None:
    if not RE_STARTED.search(ctx.log_text):
        return None
    return Diagnosis(
        "started_then_exit",
        "服务曾成功启动，随后退出",
        (
            "日志里能看到启动成功的信息，所以这不是启动失败。",
            "通常是用户关闭窗口、Ctrl+C，或运行期间被外部结束。",
        ),
        (
            "运行期的详细日志见 logs\\server.log",
            "如果是不希望出现的退出，把完整日志发给 AI 排查",
        ),
        severity="info",
    )


def rule_unknown(ctx: Context) -> Diagnosis | None:
    return Diagnosis(
        "unknown",
        "服务启动失败，原因未识别",
        (
            "脚本没有从日志里找到可判定的特征，不敢给出可能错误的结论。",
            "原始信息已完整保留在下面，请交给 AI 或按日志内容排查。",
        ),
        (
            "把取证包文件整个发给 AI",
            "附上你是怎么启动的、改动过哪些文件",
        ),
    )


RULES: tuple[Callable[[Context], Diagnosis | None], ...] = (
    rule_project_deleted,
    rule_module_file_missing,
    rule_dependency_missing,
    rule_port_in_use,
    rule_config_missing,
    rule_cuda_oom,
    rule_cuda_missing,
    rule_permission,
    rule_silent_exit,
    rule_server_exception,
    rule_started_then_exit,
    rule_unknown,
)


def analyze(ctx: Context) -> Diagnosis:
    for rule in RULES:
        result = rule(ctx)
        if result is not None:
            return result
    return rule_unknown(ctx)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def structured_fields(ctx: Context, diag: Diagnosis) -> dict:
    return {
        "schema": RULE_VERSION,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rule": diag.rule,
        "severity": diag.severity,
        "code": ctx.code,
        "rc": ctx.rc,
        "port": ctx.port,
        "host": ctx.host,
        "runner": ctx.runner,
        "run_kind": ctx.run_kind,
        "log": str(ctx.log_path),
        "log_bytes": len(ctx.log_text.encode("utf-8", errors="replace")),
        "log_raw_bytes": len(ctx.log_info.raw) if ctx.log_info else 0,
        "log_encoding": ctx.log_info.encoding if ctx.log_info else "unavailable",
        "log_lossless": (
            str(bool(ctx.log_info.lossless)).lower() if ctx.log_info else "unknown"
        ),
        "log_bom": str(bool(ctx.log_info.had_bom)).lower() if ctx.log_info else "unknown",
        "root": str(ctx.root),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def render_human(ctx: Context, diag: Diagnosis, report_written: bool) -> str:
    lines = [SEP, "  " + diag.title, THIN]
    lines.extend("  " + item for item in diag.detail)
    if diag.actions:
        lines += ["", "  建议下一步："]
        lines += ["    %d. %s" % (i, a) for i, a in enumerate(diag.actions, 1)]
    if diag.note:
        lines += ["", "  说明： " + diag.note]
    fields = structured_fields(ctx, diag)
    lines += ["", "  " + THIN, "  结构化信息（给 AI 排查用）："]
    lines += ["    %s=%s" % (k, v) for k, v in fields.items()]
    if report_written:
        lines += [
            "",
            "  完整错误报告已保存： %s" % ctx.report_path,
            "  把这个文件发给 AI 即可，不需要在控制台里手动复制文字。",
        ]
    lines.append(SEP)
    return "\n".join(lines)


def raw_log_heading(
    info: DecodedLog | None, raw_section: str = "[4]"
) -> tuple[str, str]:
    """Describe the rendered log truthfully - never claim "unedited" if it isn't."""
    if info is None or info.encoding == "unavailable":
        return "原始日志 / 无法读取", (info.note if info else "日志文件不存在或不可读。")
    if info.lossless:
        return "原始日志 / 未修改、未截断、未翻译", info.note
    if raw_section:
        heading = "原始日志 / 已按 %s 呈现，字节级原文见 %s" % (
            info.encoding, raw_section,
        )
    else:
        heading = "原始日志 / 已按 %s 呈现（本次未写出字节级副本）" % info.encoding
    return heading, info.note


def build_report(ctx: Context, diag: Diagnosis) -> str:
    """Self-contained: context + Chinese text + log + byte-exact raw copy."""
    parts = [
        "IndexTTS2 启动诊断报告",
        SEP,
        "",
        "[1] 结构化信息 / 给 AI",
    ]
    parts += ["%s=%s" % (k, v) for k, v in structured_fields(ctx, diag).items()]
    parts += ["", "[2] 中文说明 / 给人", "", diag.title, THIN]
    parts += list(diag.detail)
    if diag.actions:
        parts += ["", "建议下一步："]
        parts += ["  %d. %s" % (i, a) for i, a in enumerate(diag.actions, 1)]
    if diag.note:
        parts += ["", "说明： " + diag.note]

    heading, note = raw_log_heading(ctx.log_info)
    parts += ["", "[3] " + heading, THIN]
    needs_note = note and (
        ctx.log_info is None
        or not ctx.log_info.lossless
        or ctx.log_info.encoding == "unavailable"
    )
    if needs_note:
        parts += [note, THIN]
    # Deliberately NOT truncated: a tail can drop the very line an AI needs.
    parts += [
        ctx.log_text if ctx.log_text else "(本次没有服务日志，属于启动前的环境/参数错误)"
    ]

    info = ctx.log_info
    if info is not None and not info.lossless and info.raw:
        parts += [
            "",
            "[4] 原始字节 / base64（字节级无损，未做任何转码或过滤）",
            THIN,
            "说明： [3] 是给人看的呈现结果；本段是日志文件的原始字节，AI 可据此精确还原。",
            "还原： 拼接下面的 base64 后解码，写回 .log 文件即可；也可直接把整份报告交给 AI。",
            "",
        ]
        encoded = info.b64
        parts += [encoded[i:i + 76] for i in range(0, len(encoded), 76)]
    return "\n".join(parts) + "\n"


def write_report(ctx: Context, diag: Diagnosis) -> bool:
    try:
        ctx.report_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.report_path.write_text(build_report(ctx, diag), encoding="utf-8")
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="IndexTTS2 startup diagnostics")
    p.add_argument("--code", default="", help="known failure branch, e.g. bad_port")
    p.add_argument("--analyze", action="store_true", help="analyse the server log")
    p.add_argument("--rc", type=int, default=None)
    p.add_argument("--log", default=str(DEFAULT_LOG))
    p.add_argument("--port", default="")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--runner", default="")
    p.add_argument("--run-kind", default="")
    p.add_argument(
        "--out",
        default=os.environ.get("INDEXTTS_REPORT") or str(DEFAULT_REPORT),
    )
    p.add_argument("--root", default=str(ROOT))
    p.add_argument(
        "--log-encoding",
        default=os.environ.get("INDEXTTS_LOG_ENCODING", "auto"),
        help="log codec: auto (default = strict UTF-8 + visible \\xNN escapes) "
             "or an explicit codec such as gb18030; also INDEXTTS_LOG_ENCODING",
    )
    p.add_argument("--json", action="store_true", help="machine-readable one-line output")
    p.add_argument("--no-report", action="store_true", help="do not write the report file")
    return p


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    args = build_parser().parse_args(argv)

    ctx = Context(
        root=Path(args.root).resolve(),
        code=args.code.strip(),
        rc=args.rc,
        log_path=Path(args.log),
        port=args.port.strip().strip('"'),
        host=args.host.strip().strip('"'),
        runner=args.runner.strip().strip('"'),
        run_kind=args.run_kind.strip().strip('"'),
        report_path=Path(args.out),
    )
    info = read_log(ctx.log_path, args.log_encoding)
    ctx.log_info = info
    ctx.log_text = info.text

    diag: Diagnosis | None = None
    if args.code:
        diag = code_diagnosis(ctx)
        if diag is None:
            diag = Diagnosis(
                "unknown_code",
                "未知的错误类型",
                ("脚本传入了未识别的错误代码 %r。" % args.code,),
                ("把完整日志发给 AI 排查",),
            )
    if diag is None:
        diag = analyze(ctx)

    # Informational messages must NOT overwrite a real failure report: the
    # user may still need to hand that older report to an AI.
    should_report = (not args.no_report) and diag.severity != "info"
    report_ok = write_report(ctx, diag) if should_report else False

    if args.json:
        payload = diag.as_dict()
        payload.update(
            {
                "report": str(ctx.report_path) if report_ok else "",
                "report_written": report_ok,
                "log_chars": len(ctx.log_text),
                "raw_included": bool(ctx.log_text),
                "log_path": str(ctx.log_path),
                "log_encoding": info.encoding,
                "log_lossless": info.lossless,
                "log_raw_bytes": len(info.raw),
                "log_bom": info.had_bom,
                "log_raw_copy": bool(not info.lossless and info.raw),
            }
        )
        print(json.dumps(payload, ensure_ascii=False))
    else:
        sys.stdout.write(render_human(ctx, diag, report_ok) + "\n")
        heading, note = raw_log_heading(info, "报告 [4]" if report_ok else "")
        sys.stdout.write("\n[" + heading + "]\n" + THIN + "\n")
        if note and (not info.lossless or info.encoding == "unavailable"):
            sys.stdout.write(note + "\n" + THIN + "\n")
        # Deliberately NOT truncated: a tail can drop the very line an AI needs.
        sys.stdout.write(ctx.log_text if ctx.log_text else "(本次没有服务日志)\n")
        if not info.lossless and report_ok:
            sys.stdout.write(
                "\n字节级原文（base64）已写入报告 [4] 段： %s\n" % ctx.report_path
            )
        sys.stdout.write("\n" + SEP + "\n")
        sys.stdout.flush()

    return 2 if diag.rule.startswith("preflight_") else 0


if __name__ == "__main__":
    sys.exit(main())
