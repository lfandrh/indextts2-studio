#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tts_client.py 的离线回归测试（不需要 GPU、不需要 IndexTTS2 服务）。

为什么要有这套东西：脚本 2400+ 行，之前每次改动都靠"起真服务跑一遍"验证，
既慢又只能覆盖主路径——错误码、断点续跑、id 冲突、编码这些分支根本碰不到。
这里用标准库起一个假服务（/health、/tts 的 SSE、/audio/tempo 全都有），
把关键行为钉成断言，改脚本后 10 秒就能知道有没有改坏。

跑法：
    python tests/test_client.py            # 全部
    python tests/test_client.py -k resume  # 只跑名字带 resume 的

约定：所有用例都通过子进程调用 tts_client.py，和真实调用方式一致
（环境变量、退出码、stdout JSON 都是"对外契约"，不走进程内 import）。
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "scripts" / "tts_client.py"
PY = sys.executable


def make_wav_bytes(seconds=0.5, rate=22050):
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(b"\x00\x00" * int(rate * seconds))
    w.close()
    return buf.getvalue()


WAV_BYTES = make_wav_bytes()


def parse_multipart(body, ctype):
    """够用的 multipart 解析：只取字段名、字段值、文件名，不实现 RFC 全部细节。"""
    m = re.search(r"boundary=([^\s;]+)", ctype or "")
    if not m:
        return {}, {}
    boundary = ("--" + m.group(1)).encode()
    fields, files = {}, {}
    for part in body.split(boundary):
        if not part or part.strip() in (b"", b"--"):
            continue
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        data = data[:-2] if data.endswith(b"\r\n") else data
        hm = re.search(rb'name="([^"]*)"', head)
        if not hm:
            continue
        name = hm.group(1).decode("utf-8", "replace")
        if b"filename=" in head:
            files[name] = data
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


class FakeHandler(BaseHTTPRequestHandler):
    """最小可用的 IndexTTS2 假服务：只实现客户端真正会打的那几个端点。"""

    protocol_version = "HTTP/1.1"
    tts_calls = []          # 每次 /tts 的表单体长度，用来断言"有没有真的重新生成"
    tempo_calls = 0
    presets = {}            # name -> 是否有参考音频
    generating = False
    unload_calls = 0
    config_calls = 0
    restart_calls = 0
    glossary = {}
    model_cfg = {"loaded": True, "fp16": True, "s2mel_fp16": True, "w2v_fp16": False,
                 "qwen_fp16": True, "cudnn_benchmark": False,
                 "diffusion_steps": 25, "inference_cfg_rate": 0.7}
    CONFIG_KEYS = {"fp16", "s2mel_fp16", "w2v_fp16", "qwen_fp16", "cudnn_benchmark",
                   "diffusion_steps", "inference_cfg_rate"}

    def log_message(self, *args):
        pass

    # ---- 底层 ----
    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def _not_found(self):
        self._json({"detail": "not found"}, 404)

    # ---- GET ----
    def do_GET(self):
        p = urlsplit(self.path).path
        if p == "/api/v1/health":
            self._json({"status": "ok", "model_loaded": True})
        elif p == "/api/v1/status":
            self._json({"loaded": True, "gpu_name": "FakeGPU",
                        "vram_used_mb": 4096, "vram_total_mb": 8192,
                        "cpu_percent": 10, "mem_used_mb": 2048, "mem_total_mb": 16384})
        elif p == "/api/v1/model":
            self._json({"ok": True, "generating": type(self).generating,
                        "phase": "generating" if type(self).generating else "idle",
                        **type(self).model_cfg})
        elif p == "/openapi.json":
            # 只给对账用到的形状：故意留一个未适配的核心接口 + 两层扩展
            self._json({"openapi": "3.1.0", "paths": {
                "/": {"get": {}}, "/assets/{name}": {"get": {}},
                "/api/v1/health": {"get": {}}, "/api/v1/status": {"get": {}},
                "/api/v1/model": {"get": {}}, "/api/v1/model/unload": {"post": {}},
                "/api/v1/model/config": {"post": {}}, "/api/v1/model/restart": {"post": {}},
                "/api/v1/model/load": {"post": {}},            # ← 未适配
                "/api/v1/presets": {"get": {}, "post": {}},
                "/api/v1/presets/{name}": {"get": {}},
                "/api/v1/examples": {"get": {}}, "/api/v1/segments": {"post": {}},
                "/api/v1/tts": {"post": {}}, "/api/v1/tts/{job_id}/stop": {"post": {}},
                "/api/v1/audio/tempo": {"post": {}}, "/api/v1/history": {"get": {}},
                "/api/v1/glossary": {"get": {}, "post": {}, "delete": {}},
                "/api/v1/glossary/{term}": {"delete": {}},
                "/api/v1/lf/scripts": {"get": {}},
                "/api/v1/lf/tl/rooms": {"get": {}},
            }})
        elif p == "/api/v1/presets":
            self._json({"presets": sorted(type(self).presets)})
        elif p == "/api/v1/examples":
            self._json({"examples": [{"prompt_audio": "/examples/voice_01.wav",
                                      "emo_mode": 0, "text": "内置示例"}]})
        elif p.startswith("/examples/"):
            self._send(WAV_BYTES, "audio/wav")
        elif p.startswith("/api/v1/audio/"):
            self._send(WAV_BYTES, "audio/wav")
        elif p == "/api/v1/history":
            self._json({"history": []})
        elif p == "/api/v1/glossary":
            self._json({"glossary": {}, "cascade": False})
        else:
            self._not_found()

    # ---- POST ----
    def do_POST(self):
        p = urlsplit(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if p == "/api/v1/tts":
            type(self).tts_calls.append(len(body))
            sse = (
                'data: {"type": "started", "job_id": "job-1"}\n\n'
                'data: {"type": "progress", "value": 0.5, "desc": "sampling"}\n\n'
                'data: {"type": "done", "wav": "/api/v1/audio/out.wav", "file": "out.wav", '
                '"elapsed": 0.42, "mel_capped": false}\n\n'
            ).encode("utf-8")
            self._send(sse, "text/event-stream")
        elif p == "/api/v1/audio/tempo":
            type(self).tempo_calls += 1
            self._send(WAV_BYTES, "audio/wav")
        elif p == "/api/v1/segments":
            form = parse_qs(body.decode("utf-8", "replace"))
            text = (form.get("text") or [""])[0]
            cut = max(1, len(text) // 2)
            self._json({"segments": [{"index": 0, "text": text[:cut], "tokens": cut},
                                     {"index": 1, "text": text[cut:], "tokens": len(text) - cut}]})
        elif p == "/api/v1/model/config":
            req = json.loads(body.decode("utf-8") or "{}")
            unknown = sorted(k for k in req if k not in type(self).CONFIG_KEYS)
            if unknown:
                self._json({"detail": "Unknown config keys: " + ", ".join(unknown)}, 400)
            else:
                type(self).config_calls += 1
                type(self).model_cfg.update(req)
                self._json({"ok": True, **type(self).model_cfg})
        elif p == "/api/v1/model/restart":
            if type(self).generating:
                self._json({"detail": "正在生成语音，重建模型会换掉正在用的模型。"}, 409)
            else:
                req = json.loads(body.decode("utf-8") or "{}")
                unknown = sorted(k for k in req if k not in type(self).CONFIG_KEYS)
                if unknown:
                    self._json({"detail": "Unknown config keys: " + ", ".join(unknown)}, 400)
                else:
                    type(self).restart_calls += 1
                    type(self).model_cfg.update(req)
                    self._json({"ok": True, **type(self).model_cfg})
        elif p == "/api/v1/model/unload":
            if type(self).generating and "force=1" not in self.path:
                self._json({"detail": "正在生成语音（已进行 3 秒）。"
                                      "如需中断本次生成并卸载，请带 force=1。"}, 409)
            else:
                type(self).generating = False
                type(self).unload_calls += 1
                self._json({"ok": True, "interrupted": False,
                            "loaded": False, "phase": "idle"})
        elif p == "/api/v1/presets":
            fields, files = parse_multipart(body, self.headers.get("Content-Type", ""))
            name = fields.get("name", "").strip()
            if not name or "prompt_audio" not in files:
                self._json({"detail": "name and prompt_audio are required"}, 400)
            elif name in type(self).presets and fields.get("overwrite") not in ("true", "1"):
                self._json({"detail": "预设「%s」已存在" % name}, 409)
            else:
                type(self).presets[name] = True
                self._json({"presets": sorted(type(self).presets)})
        elif p == "/api/v1/glossary":
            ctype = self.headers.get("Content-Type", "")
            if "multipart" in ctype:
                fields, _ = parse_multipart(body, ctype)
            else:                                   # 客户端走 urlencoded
                fields = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
            term = fields.get("term", "").strip()
            if term:
                type(self).glossary[term] = {"zh": fields.get("reading_zh", "")}
            self._json({"glossary": type(self).glossary, "cascade": False})
        else:
            self._not_found()

    def do_DELETE(self):
        p = urlsplit(self.path).path
        if p == "/api/v1/glossary":
            type(self).glossary = {}
            self._json({"glossary": {}, "cascade": False})
        elif p.startswith("/api/v1/glossary/"):
            term = unquote(p.rsplit("/", 1)[-1])
            type(self).glossary.pop(term, None)
            self._json({"glossary": type(self).glossary, "cascade": False})
        else:
            self._not_found()


class ClientTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="indextts_test_"))
        FakeHandler.tts_calls = []
        FakeHandler.tempo_calls = 0
        FakeHandler.presets = {}
        FakeHandler.generating = False
        FakeHandler.unload_calls = 0
        FakeHandler.config_calls = 0
        FakeHandler.restart_calls = 0
        FakeHandler.glossary = {}
        FakeHandler.model_cfg = {"loaded": True, "fp16": True, "s2mel_fp16": True,
                                 "w2v_fp16": False, "qwen_fp16": True,
                                 "cudnn_benchmark": False, "diffusion_steps": 25,
                                 "inference_cfg_rate": 0.7}
        self.voice = self.tmp / "ref.wav"
        self.voice.write_bytes(WAV_BYTES)
        self.out = self.tmp / "out"

    # ---- 工具 ----
    def run_client(self, *args, cwd=None, env_extra=None, stdin=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("INDEXTTS_")}
        env.update({
            "INDEXTTS_URL": "http://127.0.0.1:%d" % self.port,
            "INDEXTTS_OUT_DIR": str(self.out),
            "INDEXTTS_CONFIG_DIR": str(self.tmp / "config"),   # 别碰真实的 ~/.indextts2-tts
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        })
        if env_extra:
            env.update(env_extra)
        return subprocess.run([PY, str(CLIENT)] + [str(a) for a in args],
                              cwd=str(cwd or self.tmp), env=env, input=stdin,
                              capture_output=True, text=True, timeout=180)

    @staticmethod
    def json_out(res):
        s = (res.stdout or "").strip()
        try:
            return json.loads(s)            # doctor/capabilities/last 是缩进过的整块 JSON
        except ValueError:
            pass
        for line in reversed(s.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
        raise AssertionError("stdout 没有 JSON：%r / stderr=%r" % (res.stdout, res.stderr[-800:]))

    def write_tasks(self, items, name="tasks.json", defaults=None):
        p = self.tmp / name
        p.write_text(json.dumps({"defaults": defaults if defaults is not None else
                                 {"voice": str(self.voice)}, "items": items},
                                ensure_ascii=False), encoding="utf-8")
        return p

    def wavs(self):
        return sorted(p for p in self.out.rglob("*.wav"))

    # ---- 基础契约 ----
    def test_version(self):
        r = self.run_client("--version")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("indextts2-tts 1.2.4", r.stdout)

    # ---- 模型管理（/model、/model/unload）----
    def test_model_plain_and_json(self):
        r = self.run_client("model")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("已加载", r.stdout)
        self.assertIn("steps=25", r.stdout)
        r = self.run_client("model", "--json")
        self.assertTrue(self.json_out(r)["ok"])

    def test_unload_refuses_while_generating(self):
        FakeHandler.generating = True
        r = self.run_client("unload")
        self.assertEqual(1, r.returncode, "生成中未加 --force 却卸载了")
        d = self.json_out(r)
        self.assertEqual("generating", d["reason"])
        self.assertIn("--force", r.stderr)
        self.assertEqual(0, FakeHandler.unload_calls, "409 之后仍然调到了卸载")

    def test_unload_force_interrupts(self):
        FakeHandler.generating = True
        r = self.run_client("unload", "--force")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual(1, FakeHandler.unload_calls)
        self.assertFalse(self.json_out(r)["loaded"])
        self.assertIn("显存已释放", r.stderr)

    # ---- 模型配置（/model/config、/model/restart）----
    def test_model_config_requires_a_change(self):
        r = self.run_client("model-config")
        self.assertEqual(1, r.returncode)
        self.assertIn("nothing to change", r.stderr)
        self.assertEqual(0, FakeHandler.config_calls, "没给参数却发了请求")

    def test_model_config_runtime_keys_take_effect_without_restart(self):
        r = self.run_client("model-config", "--diffusion-steps", "18", "--cfg-rate", "0.6")
        self.assertEqual(0, r.returncode, r.stderr)
        d = self.json_out(r)
        self.assertEqual(18, d["diffusion_steps"])
        self.assertAlmostEqual(0.6, d["inference_cfg_rate"])
        self.assertIn("不用重启", r.stderr)
        self.assertEqual(0, FakeHandler.restart_calls, "改采样参数不该重建模型")

    def test_model_config_precision_switch_warns_about_restart(self):
        r = self.run_client("model-config", "--w2v-fp16", "off")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertFalse(FakeHandler.model_cfg["w2v_fp16"])
        self.assertIn("model-restart", r.stderr)
        self.assertIn("不会变", r.stderr)

    def test_model_config_rejects_bad_switch_value(self):
        r = self.run_client("model-config", "--fp16", "maybe")
        self.assertEqual(1, r.returncode)
        self.assertIn("expected on/off", r.stderr)
        self.assertEqual(0, FakeHandler.config_calls, "参数非法却发了请求")

    def test_model_restart_applies_config_in_one_step(self):
        r = self.run_client("model-restart", "--diffusion-steps", "15", "--w2v-fp16", "on")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual(1, FakeHandler.restart_calls)
        d = self.json_out(r)
        self.assertEqual(15, d["diffusion_steps"])
        self.assertTrue(d["w2v_fp16"])
        self.assertIn("重建", r.stderr)

    def test_model_restart_refuses_while_generating(self):
        FakeHandler.generating = True
        r = self.run_client("model-restart", "--diffusion-steps", "12")
        self.assertEqual(1, r.returncode, "生成中却重建了模型")
        self.assertEqual("generating", self.json_out(r)["reason"])
        self.assertEqual(0, FakeHandler.restart_calls)

    # ---- 存音色（POST /presets）----
    def test_preset_save_creates_voice(self):
        r = self.run_client("preset-save", "--name", "旁白", "--audio", str(self.voice))
        self.assertEqual(0, r.returncode, r.stderr)
        d = self.json_out(r)
        self.assertTrue(d["ok"])
        self.assertIn("旁白", d["presets"])
        self.assertIn("--preset 旁白", r.stderr)

    def test_preset_save_guards_existing_name(self):
        self.run_client("preset-save", "--name", "旁白", "--audio", str(self.voice))
        r = self.run_client("preset-save", "--name", "旁白", "--audio", str(self.voice))
        self.assertEqual(1, r.returncode, "同名预设被静默覆盖了")
        self.assertEqual("exists", self.json_out(r)["reason"])
        self.assertIn("--overwrite", r.stderr)
        # 显式 --overwrite 才放行
        r = self.run_client("preset-save", "--name", "旁白", "--audio", str(self.voice), "--overwrite")
        self.assertEqual(0, r.returncode, r.stderr)

    def test_preset_save_requires_audio(self):
        r = self.run_client("preset-save", "--name", "空音色", "--audio", str(self.tmp / "nope.wav"))
        self.assertEqual(1, r.returncode)
        self.assertIn("not found", r.stderr)

    # ---- 术语表清空 ----
    def test_glossary_clear_requires_yes(self):
        self.run_client("glossary", "--add", "重庆", "--zh", "虫庆")
        self.assertEqual({"重庆"}, set(FakeHandler.glossary))
        r = self.run_client("glossary", "--clear")
        self.assertEqual(1, r.returncode, "没带 --yes 就清空了")
        self.assertEqual("need_confirm", self.json_out(r)["reason"])
        self.assertEqual({"重庆"}, set(FakeHandler.glossary))
        r = self.run_client("glossary", "--clear", "--yes")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual(set(), set(FakeHandler.glossary))

    def test_glossary_delete_single_term(self):
        self.run_client("glossary", "--add", "重庆", "--zh", "虫庆")
        r = self.run_client("glossary", "--delete", "重庆")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual(set(), set(FakeHandler.glossary))

    # ---- 接口对账（openapi）----
    def test_api_audit_lists_unadapted_core_endpoint(self):
        r = self.run_client("api")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("POST /model/load", r.stdout)
        self.assertIn("longform", r.stdout)

    def test_api_audit_strict_fails_on_gap(self):
        r = self.run_client("api", "--strict")
        self.assertEqual(1, r.returncode, "有核心缺口但 --strict 没报错")
        d = self.json_out(self.run_client("api", "--json"))
        self.assertIn("POST /model/load", d["unadapted"])
        self.assertEqual([], d["missing_on_server"], "假服务里 skill 写的接口应当都存在")

    # ---- health：连不上时要能说清原因 ----
    def test_health_ok_has_no_reason_field(self):
        r = self.run_client("health")
        self.assertEqual(0, r.returncode, r.stderr)
        d = self.json_out(r)
        self.assertTrue(d["ok"])
        self.assertIsNotNone(d["health"])
        self.assertNotIn("reason", d, "连得上时不该有失败原因字段")

    def test_health_unreachable_reports_reason_and_hints(self):
        # 指向一个没人监听的端口：必须给出可区分的 reason，而不是只有一个 null
        r = self.run_client("health", env_extra={"INDEXTTS_URL": "http://127.0.0.1:9"})
        self.assertEqual(1, r.returncode, "连不上必须 exit 1")
        d = self.json_out(r)
        self.assertFalse(d["ok"])
        self.assertIsNone(d["health"])
        self.assertEqual("connection_refused", d["reason"])
        self.assertTrue(d["hints"], "给了 reason 却没给下一步提示")
        self.assertIn("start_server.bat", " ".join(d["hints"]))
        self.assertEqual("http://127.0.0.1:9", d["origin"])

    def test_health_http_error_detail_is_truncated(self):
        """端口被别的 Web 服务占用时，detail 不能把整段 HTML 灌给 agent。

        真起一个对任何路径都返回 404 + 大段 HTML 的服务来触发 HTTPError，
        而不是拿假服务的正常响应去反证——那种断言什么都证明不了。
        """
        class _Html404(BaseHTTPRequestHandler):
            def do_GET(self):                      # noqa: N802
                body = b"<html><body>" + b"x" * 800 + b"</body></html>"
                self.send_response(404)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):             # 别往测试输出里刷日志
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Html404)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        try:
            r = self.run_client("health", env_extra={"INDEXTTS_URL": "http://127.0.0.1:%d" % port})
            self.assertEqual(1, r.returncode, r.stderr)
            d = self.json_out(r)
            self.assertEqual("http_error", d["reason"])
            self.assertLessEqual(len(d["detail"]), 200, "detail 未截断，会把 HTML 灌进上下文")
            self.assertIn("HTTP 404", d["detail"])
            self.assertIn("…", d["detail"], "截断后应留省略号，让 agent 知道还有内容")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_doctor_no_synth(self):
        r = self.run_client("doctor", "--no-synth")
        self.assertEqual(0, r.returncode, r.stderr)
        d = self.json_out(r)
        self.assertTrue(d["ready"])
        self.assertIn("skill_version", d)

    def test_doctor_summary_separates_audio_length_from_cost(self):
        """summary 必须把「音频多长」和「花了多久」分开写。

        只写一个数会被当成耗时去估算批量时间——首次合成含模型加载，
        两个数差一个数量级，混起来会让人把 100 句的工期估成 1/10。
        """
        r = self.run_client("doctor")
        self.assertEqual(0, r.returncode, r.stderr)
        d = self.json_out(r)
        summary = d["summary"]
        self.assertIn("音频", summary, "summary 没标出这是音频时长：%r" % summary)
        self.assertIn("耗时", summary, "summary 没标出真实耗时：%r" % summary)
        # 两个数都得来自 sample 的真实字段
        self.assertEqual("%.1fs" % d["sample"]["duration"], summary.split("冒烟 ")[1].split("音频")[0])
        self.assertIn("%.1f" % d["sample"]["elapsed"], summary)

    def test_capabilities(self):
        r = self.run_client("capabilities")
        d = self.json_out(r)
        self.assertTrue(d["ok"])
        self.assertEqual("indextts2-tts", d["skill"]["name"])
        self.assertIn("version", d["skill"])

    def test_synth_missing_voice_exits_2_headless_json(self):
        r = self.run_client("--headless", "synth", "--text", "没有音色")
        self.assertEqual(2, r.returncode, r.stderr)
        d = self.json_out(r)
        self.assertEqual("voice", d["need"])

    # ---- batch：指纹续跑 ----
    def test_batch_resume_regenerates_only_changed_items(self):
        tasks = self.write_tasks([{"id": "a", "text": "第一句台词"},
                                  {"id": "b", "text": "第二句台词"}])
        r = self.run_client("batch", "-f", tasks)
        m = self.json_out(r)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual(2, m["done"])
        self.assertEqual(2, len(self.wavs()))
        first_calls = len(FakeHandler.tts_calls)
        self.assertEqual(2, first_calls)

        # 再跑一次：内容没变 → 全部跳过，且一次 /tts 都不该发
        r = self.run_client("batch", "-f", tasks, "--resume")
        m = self.json_out(r)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual(0, m["done"])
        self.assertEqual(2, m["skipped"])
        self.assertEqual(first_calls, len(FakeHandler.tts_calls), "内容没变却重新合成了")

        # 改掉第二句 → 只应重跑 b
        tasks = self.write_tasks([{"id": "a", "text": "第一句台词"},
                                  {"id": "b", "text": "第二句彻底改写了"}])
        r = self.run_client("batch", "-f", tasks, "--resume")
        m = self.json_out(r)
        self.assertEqual(1, m["done"])
        self.assertEqual(1, m["skipped"])
        self.assertEqual(first_calls + 1, len(FakeHandler.tts_calls))
        self.assertEqual(["a"], [i["id"] for i in m["items"] if i.get("skipped")])

    def test_resume_any_ignores_content_change(self):
        tasks = self.write_tasks([{"id": "a", "text": "原台词"}])
        self.run_client("batch", "-f", tasks)
        tasks = self.write_tasks([{"id": "a", "text": "改过的台词"}])
        r = self.run_client("batch", "-f", tasks, "--resume-any")
        m = self.json_out(r)
        self.assertEqual(1, m["skipped"])
        self.assertEqual("exists (--resume-any)", m["items"][0]["reason"])

    def test_resume_regenerates_truncated_wav(self):
        tasks = self.write_tasks([{"id": "a", "text": "台词"}])
        self.run_client("batch", "-f", tasks)
        wav = self.wavs()[0]
        wav.write_bytes(b"RIFF\x00\x00")           # 模拟上次写盘被中断
        before = len(FakeHandler.tts_calls)
        r = self.run_client("batch", "-f", tasks, "--resume")
        m = self.json_out(r)
        self.assertEqual(1, m["done"], "截断的 wav 被当成已完成复用了")
        self.assertEqual(before + 1, len(FakeHandler.tts_calls))

    def test_duplicate_ids_blocked_before_synth(self):
        tasks = self.write_tasks([{"id": "x", "text": "甲"}, {"id": "x", "text": "乙"}])
        r = self.run_client("--headless", "batch", "-f", tasks)
        self.assertEqual(1, r.returncode)
        d = self.json_out(r)
        self.assertEqual("duplicate_ids", d["reason"])
        self.assertEqual(0, len(FakeHandler.tts_calls), "冲突时不该开始合成")

    def test_only_filters_items(self):
        tasks = self.write_tasks([{"id": "a", "text": "甲"}, {"id": "b", "text": "乙"}])
        r = self.run_client("batch", "-f", tasks, "--only", "b")
        m = self.json_out(r)
        self.assertEqual(1, m["count"])
        self.assertEqual(1, m["filtered_out"])
        self.assertEqual(["b"], [i["id"] for i in m["items"]])

    def test_only_unknown_id_fails(self):
        tasks = self.write_tasks([{"id": "a", "text": "甲"}])
        r = self.run_client("batch", "-f", tasks, "--only", "zzz")
        self.assertEqual(1, r.returncode)
        self.assertIn("zzz", r.stderr)

    # ---- manifest ----
    def test_manifest_written_to_disk(self):
        tasks = self.write_tasks([{"id": "a", "text": "甲"}, {"id": "b", "text": "乙"}])
        r = self.run_client("batch", "-f", tasks)
        m = self.json_out(r)
        mp = Path(m["manifest"])
        self.assertTrue(mp.is_file(), "manifest 没落盘：%s" % mp)
        on_disk = json.loads(mp.read_text(encoding="utf-8"))
        self.assertEqual(2, on_disk["count"])
        self.assertEqual(2, on_disk["done"])
        self.assertEqual(2, len(on_disk["items"]))
        self.assertIn("skill_version", on_disk)
        self.assertFalse(on_disk["interrupted"])

    def test_no_manifest_flag(self):
        tasks = self.write_tasks([{"id": "a", "text": "甲"}])
        r = self.run_client("batch", "-f", tasks, "--no-manifest")
        m = self.json_out(r)
        self.assertIsNone(m["manifest"])
        self.assertEqual([], list(self.out.rglob("manifest.json")))

    def test_last_updated_by_batch(self):
        tasks = self.write_tasks([{"id": "a", "text": "甲"}])
        self.run_client("batch", "-f", tasks)
        r = self.run_client("last")
        rec = self.json_out(r)
        self.assertEqual("batch", rec.get("command"))
        self.assertEqual("a", rec.get("id"))

    # ---- 输入处理 ----
    def test_text_file_relative_to_task_file(self):
        sub = self.tmp / "specs"
        sub.mkdir()
        (sub / "line.txt").write_text("相对路径的台词", encoding="utf-8")
        tasks = sub / "tasks.json"
        tasks.write_text(json.dumps(
            {"defaults": {"voice": str(self.voice)},
             "items": [{"id": "a", "text_file": "line.txt"}]}, ensure_ascii=False),
            encoding="utf-8")
        # cwd 故意放在别处：相对路径必须按清单文件所在目录解析
        r = self.run_client("batch", "-f", tasks, cwd=self.tmp)
        m = self.json_out(r)
        self.assertEqual(1, m["done"], r.stderr)

    def test_segments_out_emits_batch_tasklist(self):
        txt = self.tmp / "long.txt"
        txt.write_text("第一段文本。第二段文本。", encoding="utf-8")
        out_json = self.tmp / "tasks_from_segments.json"
        r = self.run_client("segments", "--text-file", txt, "--out", out_json,
                            "--voice", str(self.voice))
        self.assertEqual(0, r.returncode, r.stderr)
        spec = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(2, len(spec["items"]))
        self.assertEqual(spec["items"][0]["id"], "seg_001")
        self.assertEqual(str(self.voice), spec["defaults"]["voice"])
        # 闭环：直接喂给 batch 能跑通
        r = self.run_client("batch", "-f", out_json)
        m = self.json_out(r)
        self.assertEqual(2, m["done"], r.stderr)

    def test_tempo_applied_for_rate(self):
        tasks = self.write_tasks([{"id": "a", "text": "快一点", "rate": 1.2}])
        r = self.run_client("batch", "-f", tasks)
        m = self.json_out(r)
        self.assertEqual(1, m["done"])
        self.assertEqual(1, FakeHandler.tempo_calls)

    # ---- 编码 ----
    def test_no_unicode_crash_on_legacy_codepage(self):
        # 管道 + cp1252（英文 Windows 的默认 ANSI）：含中文的输出不能抛 UnicodeEncodeError
        r = self.run_client("status", env_extra={"PYTHONIOENCODING": "cp1252"})
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertNotIn("UnicodeEncodeError", r.stderr)

    def test_no_unicode_crash_cp936(self):
        r = self.run_client("doctor", "--no-synth", env_extra={"PYTHONIOENCODING": "gbk"})
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertNotIn("UnicodeEncodeError", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
