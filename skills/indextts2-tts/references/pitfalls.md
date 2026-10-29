# 陷阱与排查

按「现象 → 原因 → 处理」组织。带 ✅ 的是实际踩过并验证过的。

---

## 1. 连不上服务

**先跑 `tts_client.py health` 看 `reason`**，它对四种情况给了不同判断，对应下面不同小节：

| `reason` | 落到哪一节 |
|---|---|
| `unknown`（多半是代理的锅） | ① 502 upstream connect failed |
| `connection_refused` | ② connection refused (10061) |
| `http_error` | ③ 端口有监听但不返回 IndexTTS2 的 health |
| `dns_error` | 主机名解析不了：改用 IP |

`timeout`（有监听但 5 秒没应答）通常是**服务正在加载模型**，等 30~60 秒再探一次；
一直超时再看显存/内存有没有被占满（第 9 章）。

### ✅ 现象：连本地服务却返回 `502 upstream connect failed`

```
ERROR: GET /presets -> HTTP 502: upstream connect failed: 由于目标计算机积极拒绝，无法连接。
```

**原因**：机器设了 `HTTP_PROXY` / `HTTPS_PROXY`（企业网、CI、agent 沙箱很常见）。Python `urllib` 会把发往 `127.0.0.1` 的请求也交给代理，代理连不上上游就回 502。看起来像服务挂了，实际服务可能根本没启动，也可能正常在跑。

**处理**：本 skill 的脚本默认用 `ProxyHandler({})` 绕过所有代理，无需额外配置。如果你自己拼请求，要么显式禁用代理，要么设 `NO_PROXY=127.0.0.1,localhost`。

反过来：确实需要走代理才能到达服务（跨网段部署）时，设 `INDEXTTS_PROXY=http://host:port`。

### 现象：`connection refused`（10061）

**原因**：服务真的没起，或端口不对。

**处理**：`tts_client.py ensure`（需要本地有项目根），或自己启动：

```bash
uv run --extra server --no-sync python server.py --port 7860
```

### 现象：端口有监听，但 `/health` 不返回 `{"status":"ok"}`

**原因**：那个端口上蹲着**别的程序**。7860 是 Gradio 等工具的常用端口。

**处理**：先确认归属再决定，不要按端口盲杀进程：

```powershell
netstat -ano -p TCP | Select-String ":7860.*LISTENING"
Get-CimInstance Win32_Process -Filter 'ProcessId=<PID>' | Select CommandLine
```

命令行确实是本项目的 `server.py` 才动它；否则换端口（`--port 7861` + `INDEXTTS_URL`）。

### ✅ 现象：`INDEXTTS_URL` 端口写错，`ensure` 竟然"成功"了，结果多出一个服务实例

**原因**：`ensure` 的端口取自 `INDEXTTS_URL`，它只做"这个 URL 通不通"的判断。
端口写成 7861 而 7861 上什么都没有时，它会**在 7861 上再起一个实例**并探活成功 ——
你以为连的是原来的服务，其实连的是新进程。

**处理**：两个实例各自独立加载模型，8G 卡上第二个大概率 OOM 或变极慢。改端口/改地址后先：

```bash
python tts_client.py health     # 确认返回 {"status":"ok"}，再确认它是不是你以为的那个
python tts_client.py status     # 看 gpu/vram 与 model_loaded，和预期对得上才继续
```

真起了多余的实例，用 `--host`/端口对应的 PID 关掉，别按名字批量杀。

---

## 2. SSE 与超时

### 现象：拿不到进度，或流一次性吐完

**原因**：用了浏览器 `EventSource`，或 HTTP 客户端做了缓冲。

**处理**：

- `/tts` 是 **POST + multipart**，`EventSource` 发不了 body，只能自己 POST 后逐行读响应体
- curl 加 `-N` 关缓冲
- 逐行读（`for line in resp`），不要 `resp.read()` 整体读——那会一直阻塞到流结束

### ✅ 现象：客户端报超时 / 读到空响应

**原因**：首次 `/tts` 要懒加载模型，30~60s 起步。超时设 30s 必然失败。

**处理**：超时 ≥ 300s。本 skill 默认 `INDEXTTS_HTTP_TIMEOUT=900`。

### 现象：把 `stopped` 当成成功

**原因**：只处理了 `done` / `error` 两种事件。

**处理**：`/tts/{id}/stop` 触发后，服务端先发 `stopping`（收尾中）再发 `stopped`（已停止）。`stopped` **不是错误**，是用户主动中断 —— 但它也不是 `done`，此时**没有音频产物**。客户端必须显式区分这三种结局，否则会把"没生成"报成"生成成功"。

---

## 3. 中文路径

### 现象：`UnicodeEncodeError: 'ascii' codec can't encode characters`

**原因**：输出文件名默认取文本前 15 字，常含中文。`urllib` 不会自动 percent-encode 请求行。

**处理**：手工下载时先 encode：

```python
parts = urllib.parse.urlsplit(url)
if not parts.path.isascii():
    parts = parts._replace(path=urllib.parse.quote(parts.path))
url = urllib.parse.urlunsplit(parts)
```

**另一类同样的报错，但原因不同**：`stdout` 被管道/文件捕获时，Windows 用的是 ANSI 代码页
（中文系统 cp936、英文 cp1252）。脚本打印中文路径或 `⚠` 这类符号就会
`UnicodeEncodeError`，而且往往发生在**合成成功、音频已落盘之后**，看起来像"没生成"。
v1.1.0 起脚本启动时会把非 tty 的 stdout/stderr 固定成 UTF-8（`errors="replace"`），
已不再触发；如果你在别的脚本里复刻这段逻辑，记得同样处理，别只盯着 URL 编码。

### 现象：下载预设音频 404

**原因**：**双重编码**。`GET /presets/{name}` 返回的 `prompt_audio_url` 已经是 `%E8%83%A1...` 形态，再 `quote` 一次会把 `%` 编成 `%25`。

**处理**：先判断 `path.isascii()`，已是编码态就不要再编。本 skill 的 `download()` 已按这个规则分支处理。

### 现象：multipart 上传中文文件名失败

**原因**：`Content-Disposition` 里的 `filename` 按 RFC 要用 ASCII 或 `filename*=` 扩展。

**处理**：上传时文件名一律用 ASCII（`spk.wav` / `emo.wav`），真实内容靠字段名区分。

### ✅ 现象：`--text-file` 读进来的文本，开头多出一个看不见的字符

**原因**：Windows 记事本存 UTF-8 会写 BOM（`U+FEFF`）。用 `encoding="utf-8"` 读不会报错，
但 BOM 会作为一个正常字符送进 TTS，合成出来的第一句可能带怪停顿或杂音，而且极难排查。

**处理**：客户端统一用 `utf-8-sig` 解码，自动吃掉 BOM。自己写代码读文本时也用 `utf-8-sig`。

### ✅ 现象：`examples` 里的名字当本地路径用，报"reference audio not found"

**原因**：`GET /examples` 返回的 `prompt_audio` 是**服务端 URL**（形如 `/examples/voice_01.wav`），
不是本地文件。本机部署时恰好能在 `examples/` 目录里找到同名文件，所以看着能用；
纯远程部署（Docker、别的机器）本地根本没这个文件。

**处理**：直接 `--voice voice_01.wav`（或 `/examples/voice_01.wav` 原样传也行）。
客户端会先查本地，查不到就 `GET /examples/{name}` 下载到 `<输出目录>/.voices/` 再上传，
第二次走缓存不再下载。服务端那张表在 `examples/cases.jsonl`。

---

## 4. 情感控制

### 现象：传了 `emo_weight` 但没效果

**原因**：`emo_mode=0` 时服务端把 `emo_alpha` 强制为 `1.0`，`emo_weight` 被忽略。

**处理**：要调强度就用 `emo_mode=1`（情感音频）或 `2`（向量）。

### 现象：`惊讶` / `平静` 设成 1.0 但感觉不明显

**原因**：`normalize_emo_vec` 会做 bias 缩放，`惊讶` × 0.6875、`平静` × 0.5625。设 1.0 实际只有 0.69 / 0.56。

**处理**：这两维要配合其它维度一起给，别指望单维拉满。详见 `parameters.md` 的 bias 表。

### 现象：`emo_mode=3` 第一次调用特别慢

**原因**：情感文本要送 Qwen 情感模型，而该模型是**懒加载**的，首次用到才载入。

**处理**：这是预期行为，不是卡死。首调用给足超时。

### 现象：`emo_mode=1` 返回 400

**原因**：没传 `emo_audio`，或传了但预设里本来就没有 `emo_ref.wav`。

**处理**：先 `GET /presets/{name}` 看 `emo_audio_url` 是否为空。空就说明这个预设只能用 mode 0/2/3。

---

## 5. 静默失败（不报错但结果不对）

这类最危险 —— 音频出来了，但内容不对，没有任何异常。

### ✅ `mel_capped=true`

音频被 `max_mel_tokens` 截断。**不报错，只是变短**。

**处理**：读 `done` 事件的 `mel_capped`，为 `true` 就告诉用户。优先切短文本，其次调大 `max_mel_tokens` —— 但它有硬上界 **1815**（与网页 `#maxMel` 滑块一致），传更大只会被静默夹回 1815，音频还是不够长就只能回去切文本。

### `normalize_fallback` 非 null

文本规范化失败，退回了原文。**音频仍然生成**，但多音字/数字/符号的读音可能不标准。

**处理**：读 `done` 事件的 `normalize_fallback`，非 null 时提示用户核对读音。

### ✅ `interval_silence` 越界被静默夹取

服务端 `max(10, min(500, x))`。传 1000 不报错，实际按 500 跑。

**处理**：客户端已提前告警 `[warn] --interval-silence 1000 out of range, server clamps to 500 ms`。

### 术语表连锁误伤

`{"C++": "C 加加", "C": "西"}` 输入「他主要写 C++」→ 得到「他主要写西加加」。

**原因**：`cascade=true` 时替换结果会再参与后续匹配。

**处理**：保持 `cascade=false`（默认）。它用私有区占位符做单遍替换，不会连锁。

---

## 6. 任务与锁

### 现象：`/tts/{id}/stop` 返回 404

**原因**：任务 id 不存在，或已经跑完了（任务结束后会从表里移除）。

**处理**：这不是 bug。但要**用退出码区分**——`tts_client.py stop` 对 4xx 返回 1，不要把"没停到"报成"停成功"。

### ✅ 现象：停止后下一次生成又要等 30~60s

**原因**：`/stop` 默认会卸载并重建模型（清掉半途停止留下的脏缓存）。

**处理**：确实要停就带 `keep_model=1`，模型保留，下次直接用：

```bash
tts_client.py stop <job_id> --keep-model
```

### 现象：模型正在生成时点卸载，返回 409

**原因**：`POST /model/unload` 默认拒绝打断生成——服务端不替用户决定"这次生成要不要放弃"。

**处理**：确认要打断就带 `force=1`，响应里的 `interrupted` 告诉你是否真的打断了。
命令行版就是 `tts_client.py unload --force`；不带 `--force` 时脚本会以退出码 1 停下并
在 stderr 说明原因，不会自作主张打断。

### 现象：生成中 `model-restart` 直接 409，而且没有 force 可用

**原因**：重建等于把正在用的模型换掉，服务端一律拒绝；`force` 通道只给了 `unload`
（卸载是"丢掉"，重建是"替换"，后果不一样）。

**处理**：等这次生成结束再跑。真要立刻打断就先 `unload --force`（顺带清掉模型），
之后 `model-restart` 或下次合成都会自动重新加载。

⚠️ 409 是在**写配置之前**返回的，所以那次调用带的 `--diffusion-steps 30` 根本没生效——
别看到 stderr 打了"先应用配置"就以为已经改了，`model` 复核一下。

### 现象：两个合成任务互相等死

**原因**：服务端全局单锁 + 队列，第二个请求会一直挂着等。

**处理**：客户端文件锁会先挡住第二个 `synth`（stderr 打 `[lock] ... waiting`）。`--lock-wait 0` 让它立即失败而不是无限等。等待异常久时先 `status` 看是不是真在推理。

### 现象：并发请求返回 409

**原因**：`/model/load` 和 `/model/restart` 在生成期间一律 409，不会静默排队（重建模型等于把正在用的模型换掉）。

**处理**：等生成结束再操作。`/model/unload` 例外，它有 `force=1` 的正当中断路径。

### 现象：中断（关对话 / 请求超时）后立刻重跑，卡在 `[queue] N job(s) ahead`

**原因**：客户端断开**不会**取消服务端的任务。那个 job 还在跑完整段推理、占着 GPU，
新请求只能排在它后面。用户看到的现象是"点了没反应"，其实是排队。

**处理**：

```bash
python tts_client.py status        # 看 "正在生成中" 与服务端阶段
python tts_client.py history       # 看还在跑/刚结束的 job_id
python tts_client.py stop <job_id> --keep-model   # 确实要掐掉
```

`--keep-model` 很重要：不带它会卸载并重建模型，下次合成再等 30~60 秒。

---

## 7. 上传与输出

### 现象：音频上传返回 400

**原因**：三种可能 —— 超过 20 MB；格式不在 wav/mp3/flac/ogg/m4a 白名单；**改了扩展名的假音频**（服务端做魔数嗅探，不看扩展名）。

**处理**：确认文件真实格式。`.m4a` 改名为 `.wav` 一定会被拒。

### 现象：生成的音频过几天找不到了

**原因**：`outputs/` 根目录的 wav 超过 14 天会被自动清理；`/history` 是内存态，服务重启即清。

**处理**：要留就尽早转存。注意 `outputs/agent_tts/` 是**子目录**，不在清理范围内，会一直累积 —— 长期使用需要自己清。

### 现象：参考音频重复上传占空间

**原因**：`prompts/spk_<md5>.wav` 是内容寻址，同一音频不会重复占空间。

**处理**：但目录是 **LRU 30 个**，长期依赖某个参考音频请用预设固化，别指望缓存一直在。

### 现象：合成"成功"了但文件不存在，报 `No space left on device`

**原因**：模型跑完了（GPU 已经白跑一趟），卡在写盘上。原始异常是 errno 28，不说人话。

**处理**：客户端已把 ENOSPC 翻译成"磁盘已满"。`doctor` 的 `disk_headroom` 低于 1 G 会提前告警。
换 `--outdir` 到别的盘最快；`agent_tts/` 不在自动清理范围，长期用要自己清。

### ✅ 现象：`batch` 跑完，两条不同台词却拿到一模一样的音频

**原因**：**id 撞车**。输出文件名由 id 经 `_safe_name` 归一化得到，同一个名字后一条覆盖前一条。
两条在 manifest 里都 `ok: true`、`out` 相同，所以完全看不出来。

`_safe_name` 会把非字母数字、非 `-_` 的字符换成 `_`，所以这些**不同 id** 会变成**同一个文件**：

| id | 归一化后 |
|---|---|
| `第1句/开场` | `第1句_开场` |
| `第1句:开场` | `第1句_开场` |
| `line 001` | `line_001` |
| `line?001` | `line_001` |

配 `--resume` 更糟：两条都会命中同一个已存在的文件被跳过，其中一条**永远生成不出来**，且无任何报错。

**处理**：客户端现在在**合成前**就拦（退出码 1，headless 下 `reason: duplicate_ids`，列出冲突项）。
给每条的 id 取唯一值，或用显式 `out` 区分。别靠改名规避——上游解析 manifest 时会对不上。

### ✅ 现象：改了一句台词重跑 `--resume`，成片里还是老台词

**原因**：早期的 `--resume` 只判"输出文件存不存在"。改稿后文件还在，于是被判成"已完成"跳过，
manifest 写 `ok: true`、`duration` 从旧文件读——**一切看起来都正常，错在内容**。
返工（改词、换音色、调语速）是这个流程的常态，所以这个坑一定会踩到。

**处理**：v1.1.0 起 `--resume` 判的是**内容指纹**（文本 + 预设/音色 + emo 参数 + rate），
记在同目录的 `.indextts_sig.json`。下面三类都会自动重跑：

- 文本/音色/emo/rate 有改动
- wav 读不出时长（上次写盘被中断，只留了半截）
- 旧版本留下的文件没有指纹记录（宁可重跑一次）

要继续用"文件存在即跳过"的老语义，显式加 `--resume-any`。只改一句时用 `--only line_012` 更快。

顺带：manifest 现在每完成一条就落盘到 `<outdir>/<日期>/manifest.json`，
跑到一半被杀也有 `interrupted: true` 和前 79 条记录，不必整批重来。

---

## 8. 进程生命周期（沙箱 / 非交互环境）

### ✅ 现象：`ensure` 报"服务已就绪"，下一条命令又连不上

**原因**：`ensure` 用 detached 方式 spawn 服务，但在 agent 沙箱里，命令结束时整个进程树会被回收（Windows Job Object 的 kill-on-close）。服务确实起来过、也响应了 `health`，然后被连带杀掉。

**处理**：这不是代码 bug，是环境约束。跨命令复用服务时，用**受管后台任务**启动，而不是靠 `ensure`：

```bash
# 用后台任务方式启动（run_in_background），服务跨命令存活
cd <项目根> && uv run --extra server --no-sync python server.py --port 7860
```

单次命令内 `ensure` + `synth` 连着跑没问题。

### 现象：`ensure` 报找不到项目根

**原因**：脚本不在项目目录内，且 `INDEXTTS_PROJECT_DIR` 没设。此时是**纯远程模式**，`ensure` 主动放弃而不是乱猜路径。

**处理**：服务自己起，只设 `INDEXTTS_URL`；或设 `INDEXTTS_PROJECT_DIR` 让 `ensure` 能干活。

---

## 9. 显存不足（8G 卡最常见）

8G 卡（3060 Ti / 4060 这类）是这套模型的下限配置，加载后整卡约 6.7 G，余 1.3 G 属正常。
在这之上**再加一点东西就会出事**，而且两种表现完全不同：

### ✅ 现象：不报错，但生成从 3 秒变成 90 秒

**原因**：显存不够时 Windows 不会报错，而是把溢出的部分放到共享内存（走 PCIe）。
算力掉一个数量级，但流程完全正常 —— 音频照样出来，只是极慢。

**这是最危险的一种**：用户会以为"机器老了"、"模型就这样"，而不是"显存不够"。

**处理**：`tts_client.py status` 看 `显存 x/y G（当前余 z G）`：

```bash
python tts_client.py status
# 模型: 已加载
# GPU: NVIDIA GeForce RTX 3060 Ti | 显存 6.7/8.0 G（当前余 1.3 G）
```

余量低于 1.2 G 就要处理，低于 0.5 G 基本必然变慢。`doctor` 的 `vram_headroom` 会给同样的判断
（模型加载后实测，不是估算）。

### 现象：`generation failed: CUDA out of memory`

**原因**：真 OOM，通常是长文本 + 余量本就不够。客户端会自动附上排查步骤。

**处理**，按代价从低到高：

1. 关掉占显存的程序（浏览器硬件加速、游戏、另一个 TTS 实例）
2. 释放模型：`python tts_client.py unload`（等价 `POST /model/unload`，前端「模型管理」页也有按钮），
   下次合成重新加载 30~60s。生成中会被拒，确认要打断再加 `--force`
3. 文本切短：长文本的 KV cache 峰值跟长度是平方级关系，砍一半长度省的不止一半
4. 降 `diffusion_steps`：`python tts_client.py model-config --diffusion-steps 20`（下次生成生效）
5. 开 `w2v_fp16`：`python tts_client.py model-restart --w2v-fp16 on`（省约 1G，需自测音质）

先 `tts_client.py model` 看一眼再动手：它给的是模型**自身**的精度与步数配置以及 `generating`，
比 `status` 的整机仪表更适合确认"现在到底能不能卸"。

### ✅ 现象：改了 `w2v_fp16` / `fp16`，速度、显存一点没变

**原因**：精度开关只有**重建模型**时才会被读。`POST /model/config` 只是把新值写进
`init_kwargs`，已加载的实例仍按旧精度跑。这是最容易白折腾一轮的坑——改完看没效果，
会误以为"这个开关是假的"。

**处理**：

```bash
# 错：只写配置，正在跑的模型不动
python tts_client.py model-config --w2v-fp16 on

# 对：写配置 + 重建，一步到位
python tts_client.py model-restart --w2v-fp16 on
```

对比之下 `--diffusion-steps` / `--cfg-rate` 走的是另一条路：`set_runtime` 会直接写进已加载
实例的字段，**下一次生成就生效，不用重启**。脚本在 stderr 会把这两类分开提示。

改完用 `python tts_client.py model` 复核，别只看命令返回的 `ok: true`。

**别做的事**：不要为了"提高稳定性"同时起两个服务实例。两个实例各自加载一套模型，
8G 卡上必然有一个崩或者两个都变极慢。

---

## 10. 音色不像 / 读音错

### 现象：声音不像参考音频，或者带杂音、回声

**原因**：这是零样本克隆，**参考音频里有什么，输出就带什么**。九成问题在输入，不在模型。

| 参考音频 | 输出 |
|---|---|
| 带背景音乐 | 每句都掺那点音乐残响 |
| 房间回声大 | 发闷，像在浴室说话 |
| 短于 3 秒 | 音色飘，前后不一致 |
| 超过 60 秒 / 情绪起伏大 | 音色被后半段带偏 |
| 变声器或 AI 处理过 | 电子味，越学越假 |

**处理**：换一段**单人、无 BGM、无混响、5~15 秒、情绪平稳**的音频。
预处理（分离人声、降噪）比调参数有效得多 —— `emo_weight`、`temperature` 这些
动的是"情感"和"随机性"，救不了脏输入，只会让结果更不可预测。

### 现象：「重庆」读成 chóng qìng、「2026」读成「二零二六」

**原因**：文本规范化的局限，不是模型坏。多音字、阿拉伯数字、英文缩写在送进模型前
会被规则处理，规则没覆盖到就按默认读法。

**处理**：用术语表把词改写成"读出来就是对的"写法：

```bash
python tts_client.py glossary --add "重庆" --zh "虫庆"
python tts_client.py glossary --add "2026" --zh "二零二六年"
python tts_client.py glossary --delete "重庆"     # 删
python tts_client.py glossary                     # 看当前词表
```

- `--zh` 填**同音汉字**，不是拼音。它是在送模型前做文本替换。
- 词表是**服务端全局**的：`POST /api/v1/glossary` 落到 `<模型目录>/glossary.yaml`，
  由服务端 normalizer 统一替换。**前端页面、别的 Skill、别的项目会一起变**——
  不是"只影响我这一个调用方"。加词前想一下会不会误伤别人的稿子。
- 换机器 / 换整合包，词表各是各的（跟着模型目录走）。
- 别加太短的词（`"C"`、`"了"`），会和别的词连锁误伤（见第 5 章 `cascade`）。

---

## 11. 整合包升级后接口漂移

### 现象：脚本对着新整合包调接口，莫名其妙 404 / 400

**原因**：脚本里的接口路径是硬编码的。整合包升级（比如多挂了 longform 长篇台本、
timeline 时间轴两个扩展）不会让旧调用报错，只会一直用旧地址，直到某天真的改了路径。
这类问题**不会自己暴露**，只会表现成"以前能用，现在不行了"。

**处理**：跑对账。

```bash
python tts_client.py api             # 人话报告：核心/扩展分层 + 未适配清单
python tts_client.py api --json      # 机器可读（unadapted / missing_on_server）
python tts_client.py api --strict    # 核心接口对不上就 exit 1，适合发版门禁
```

输出分三类，别混着看：

| 分类 | 含义 | 要不要处理 |
|---|---|---|
| 未适配的核心接口 | 服务端有，脚本还不能调 | 看是否属于合成流程，是就补 |
| `[!] skill 写了但服务端没有` | 脚本在调、服务端已经没了 | **危险**，很可能是路径改名或版本偏旧 |
| 扩展接口（longform / timeline） | WebUI 的编辑工程层 | 不属于本 skill，忽略 |

参考基线（IndexTTS2.0_v3 实测）：服务端共 146 个接口 = 核心 34 + longform 81 + timeline 31，
本 skill 适配核心 17 个。

`--strict` 只认核心接口的缺口，扩展层再多也不会让它报错——那本来就是另一个 skill 的事。

### 现象：`api` 报 `openapi_disabled`

**原因**：服务端 `FastAPI(openapi_url=None)` 把文档关了，或者不是 FastAPI 实现。

**处理**：只能人肉比对（`/docs` 或读 `server.py` 的路由装饰器）。正常情况下 FastAPI 默认
在根路径 `/openapi.json` 暴露，脚本先试这个、再试 `/api/v1/openapi.json`。

---

## 12. 自检清单

环境层面先跑一把（只读、不占 GPU）：

```bash
python tts_client.py --version        # 先确认装的是哪一版（两份副本容易漂移）
python tts_client.py selftest
python tts_client.py api              # 整合包版本和脚本适配的接口对不对得上
```

改了脚本本身？先跑离线测试，它不需要 GPU：

```bash
python tests/test_client.py           # 38 项，约 25 秒
```

合成成功后按这个顺序验，能挡住绝大多数"看起来成功其实废了"：

- [ ] 生成前已向用户确认音色（或用户明确给了 `--voice`）
- [ ] SSE 收到了 `done`（不是 `stopped`，也不是流断在半路）
- [ ] `mel_capped` 为 `false`
- [ ] `normalize_fallback` 为 `null`
- [ ] stdout JSON 里的 `out` 文件存在且 > 10 KB（静音/失败样本通常极小）
- [ ] 音频能解析出时长：

  ```python
  import wave
  with wave.open(path) as w:
      dur = w.getnframes() / w.getframerate()
      print(w.getnchannels(), w.getframerate(), round(dur, 2))
  ```

  正常输出形如 `1 22050 4.92`（单声道、22.05 kHz）。时长应与文本长度大致匹配（中文约 3~6 字/秒，取决于标点和语速）；明显偏短就要怀疑截断，明显偏长要怀疑重复生成
- [ ] `last` 子命令能读到这条记录
