# HTTP API 参考

所有接口前缀 `{INDEXTTS_URL}/api/v1`（默认 `http://127.0.0.1:7860/api/v1`）。根路径 `/` 返回 Web UI，`/api/v1` 之外没有 JSON 接口。

服务无鉴权、可删文件，`--host` 应保持 `127.0.0.1`。

## 哪些接口有命令行封装

**不要再手搓 curl**——下面这些已经封装好了，agent 直接用命令，输出是稳定 JSON：

| 接口 | 命令 |
|---|---|
| `GET /health` | `health` |
| `GET /status` | `status [--json]` |
| `GET /model` | `model [--json]` |
| `POST /model/unload` | `unload [--force]` |
| `POST /model/config` | `model-config --diffusion-steps N --cfg-rate R --w2v-fp16 on\|off …` |
| `POST /model/restart` | `model-restart [同上的配置参数]`（重建模型，30~60s） |
| `GET /presets` / `GET /presets/{name}` | `presets` / `preset <名>` |
| `POST /presets` | `preset-save --name X --audio Y [--overwrite]` |
| `GET /examples` | `examples` |
| `POST /tts` | `synth` / `batch` |
| `POST /tts/{id}/stop` | `stop <job_id> [--keep-model]` |
| `POST /audio/tempo` | `tempo --file X --rate R` |
| `POST /segments` | `segments [--out tasks.json]` |
| `GET /history` | `history` |
| `GET /glossary` / `POST /glossary` / `DELETE /glossary/{term}` / `DELETE /glossary` | `glossary [--add/--delete/--clear --yes]` |

其余接口（`POST /model/load`、预设改名复制删除、历史清理、`/jobs/*`、`/metrics`、
`/lf/*` 长篇台本、`/lf/tl/*` 时间轴）没有封装，需要时按下面的字段直接打 HTTP。
想知道你的整合包比这份清单多了什么，跑 `tts_client.py api`。

---

## 1. 服务与模型

### `GET /health`

```json
{"status": "ok", "model_loaded": false}
```

`model_loaded` 是**当前**是否驻留显存，不是"能否加载"。首次 `/tts` 才会真正加载。

`health` 命令在这一层之上做了分类：连不上时客户端自己补 `reason`（`connection_refused` /
`timeout` / `http_error` / `dns_error`）、`detail`（截断到 160 字）和 `hints`，
服务端本身不返回这些字段。四种原因的处置见 `SKILL.md` 的「health 说不上话时怎么看」。

### `GET /status`

前端 2 秒轮询用的合并端点（模型状态 + 硬件仪表）：

```json
{
  "ok": true, "generating": false, "loaded": false, "phase": "unloaded",
  "fp16": true, "s2mel_fp16": true, "w2v_fp16": true, "qwen_fp16": true,
  "cudnn_benchmark": false, "diffusion_steps": 25, "inference_cfg_rate": 0.7,
  "cpu_percent": 0.0, "mem_used_gb": 13.56, "mem_total_gb": 31.15,
  "gpu_percent": 34, "vram_used_mb": 1004, "vram_total_mb": 8192,
  "gpu_name": "NVIDIA GeForce RTX 3060 Ti"
}
```

无 GPU 时 `gpu_*` 字段为空或缺失。

### `GET /model`

同 `status`，额外带 `generating`。用于在点按钮前判断"现在动模型会不会打断生成"。

### `POST /model/load`

加载模型（同步阻塞，30~60s）。**生成期间返回 409**，不会静默排队。

### `POST /model/unload?force=0|1`

| 参数 | 行为 |
|---|---|
| 无 `force` | 正在生成时返回 **409**，并告知已进行多少秒 |
| `force=1` | 打断当前生成（置停止标志）后再卸载。响应里 `interrupted` 标明是否真的打断了 |

返回 `{"ok": true, "interrupted": bool, ...state}`。卸载后会 `gc.collect()` + `torch.cuda.empty_cache()`。

### `POST /model/restart`

带 body 时先应用配置再重建模型。**生成期间返回 409**。

### `POST /model/config`

Body 是 JSON，只接受这 7 个 key，未知 key 一律 **400**（防拼写错误被静默吞掉）：

| key | 类型 | 生效时机 |
|---|---|---|
| `diffusion_steps` | int 1–50 | 立即 |
| `inference_cfg_rate` | float 0–1 | 立即 |
| `cudnn_benchmark` | bool | 立即 |
| `fp16` | bool | **需 restart** |
| `s2mel_fp16` | bool | **需 restart** |
| `w2v_fp16` | bool | **需 restart** |
| `qwen_fp16` | bool | **需 restart** |

---

## 2. 合成

### `POST /tts`

`multipart/form-data`，返回 `text/event-stream`（SSE）。字段全表见 `parameters.md`。

**前置校验（在进入推理前就返回 HTTP 错误）**：

| 情况 | 状态码 |
|---|---|
| `text` 全空白 | 400 |
| `text` > 10000 字 | 413 |
| `emo_text` > 500 字 | 413 |
| 音频非白名单格式 / > 20 MB | 400 |
| `emo_mode=1` 缺 `emo_audio` | 400 |
| 整型字段传了非数字 | 422（FastAPI 自动） |

**SSE 事件类型**（每行 `data: {json}`）：

| type | 字段 | 含义 |
|---|---|---|
| `started` | `job_id` | 任务已注册，记下这个 id 用于 `stop` |
| `warn` | `detail` | 非致命提醒（如文本超软上限、预计耗时） |
| `load` | `value`, `desc` | 模型正在加载 |
| `queue` | `ahead` | 前面还有几个任务 |
| `progress` | `value`(0~1), `desc` | 生成进度，`desc` 是中文阶段描述 |
| `done` | `wav`, `file`, `elapsed`, `history_id`, `mel_capped`, `normalize_fallback` | 成功 |
| `stopping` | `detail` | 停止流程已启动（收尾中） |
| `stopped` | `detail` | 已停止（**不是失败**，是用户主动中断） |
| `error` | `detail` | 失败，`detail` 可读 |

`done` 字段说明：

- `wav` — 下载路径，形如 `/api/v1/audio/xxx.wav`
- `file` — 服务端文件名（默认是"文本前 15 字 + 时间戳"）
- `mel_capped` — **`true` 表示音频被 `max_mel_tokens` 静默截断**，不报错只是变短
- `normalize_fallback` — `null` 表示文本规范化正常；非 null 表示规范化失败、退回原文（读音可能不标准，但音频仍生成）
- `history_id` — 对应 `/history` 里的条目 id

**客户端读流注意**：这是 POST + multipart，浏览器 `EventSource` 发不了 body。必须自己发 POST 再逐行读响应体。

### `GET /audio/{name}`

下载生成的 wav（16-bit PCM）。`name` 可能含中文，**必须 percent-encode**。

### `POST /audio/tempo`

保音高变速，**不落盘**，直接返回 wav 二进制（`audio/wav`）。客户端子命令：`tts_client.py tempo --file in.wav --rate 1.2 --out out.wav`。

| 字段 | 说明 |
|---|---|
| `file` | 音频文件（同样受 20 MB / 格式白名单约束） |
| `rate` | 倍率，`0.5`–`2.0`，超出返回 400 |

底层是 audiotsm WSOLA（MIT），不是 librosa 相位声码器——所以没有"机器人音"。

### `GET /jobs/current` / `GET /jobs/{job_id}`

任务状态：`queued` / `running` / `stop_requested` / `stopped` / `cancelled` / `done` / `error`。

### `POST /tts/{job_id}/cancel`

只对 `queued` 有效：GPU 未触碰前直接丢弃。

### `POST /tts/{job_id}/stop?keep_model=0|1`

| 参数 | 行为 |
|---|---|
| 默认 | 在下个进度回调处停下，然后**卸载并重建模型**（清掉半途停止留下的脏缓存），下次生成重新付加载时间 |
| `keep_model=1` | 停下但**保留模型**，下次生成直接接着用 |

`job_id` 不存在或已结束返回 **404** `{"detail": "Job not found or already finished"}`。

---

## 3. 预设

预设 = 参考音频 + 情感配置 + 全部采样参数，存 `outputs/presets/<名称>/`：

```
outputs/presets/胡桃/
├── preset.json      # 参数
├── prompt.wav       # 音色参考音频（可能不存在）
└── emo_ref.wav      # 情感参考音频（可能不存在）
```

`preset.json`：

```json
{
  "emo_control_method": 0,
  "emo_weight": 0.65,
  "emo_vector": [0, 0, 0, 0, 0, 0, 0, 0],
  "emo_text": "",
  "emo_random": false,
  "advanced_params": {
    "do_sample": true, "top_p": 0.8, "top_k": 30, "temperature": 0.8,
    "length_penalty": 0.0, "num_beams": 3, "repetition_penalty": 10.0,
    "max_mel_tokens": 1500, "max_text_tokens_per_segment": 120
  },
  "prompt_audio": "prompt.wav",
  "emo_audio": ""
}
```

| 端点 | 说明 |
|---|---|
| `GET /presets` | `{"presets": ["胡桃", ...]}` |
| `GET /presets/{name}` | 详情；音频字段换成 `prompt_audio_url` / `emo_audio_url`（**已是 percent-encode 态**） |
| `GET /presets/{name}/audio/{kind}` | `kind` = `prompt` \| `emo_ref` |
| `POST /presets` | 创建。同名 **409**；音频不是可识别格式 **400**；音频 > 20 MB **413**。`advanced_params` 落盘前按 `/tts` 的参数区间夹取，所以存进去的已经是夹过的值 |
| `DELETE /presets/{name}` | 删除 |
| `POST /presets/{name}/rename` | 重命名 |
| `POST /presets/{name}/duplicate` | 复制 |

映射到 `/tts` 字段（与前端 `applyPresetToForm` 一致）：

- `emo_mode` ← `emo_control_method`
- `vec1..vec8` ← `emo_vector`（长度必须为 8）
- `use_random` ← `emo_random`
- `advanced_params` 原样透传
- `emo_mode=1` 时必须把 `emo_ref.wav` 作为 `emo_audio` 上传

---

## 4. 其他

| 端点 | 说明 |
|---|---|
| `GET /examples?include_experimental=0\|1` | 内置示例音色（来自 `examples/cases.jsonl`），自带推荐情感参数。默认**过滤掉 `emo_mode=3`** 的实验性条目，带 `include_experimental=1` 才全给 |
| `GET /examples/{name}` | **返回该示例的音频文件本体**（`FileResponse`），不是 JSON 详情——名字对不上任何 `cases.jsonl` 条目也能取到 `examples/` 目录下的同名音频。**只放行音频扩展名**（`.wav` `.mp3` `.flac` `.ogg` `.m4a`），`cases.jsonl`、`batch/` 子目录、无扩展名一律 **404** |
| `POST /segments` | `text` + `max_text_tokens_per_segment` → 预览自动分段结果，**不合成** |
| `GET /history` | 本次服务运行的生成历史（**内存态，重启即清**），最多 20 条 |
| `POST /history/config` | `auto_clean` 开关：超出 20 条时是否连带删除音频文件 |
| `DELETE /history` | 清空历史 |
| `DELETE /history/{item_id}` | 删单条 |
| `GET /metrics` | 轻量指标（供监控轮询） |
| `GET /glossary` | `{"glossary": {...}, "cascade": bool}` |
| `POST /glossary` | `term` + `reading_zh` / `reading_en`，持久化到 `checkpoints/glossary.yaml` |
| `POST /glossary/cascade` | `cascade=true\|false`，只改级联开关不动术语表 |
| `DELETE /glossary/{term}` | 删单条；术语可含空格/中文/符号，需 percent-encode |
| `DELETE /glossary` | 清空全部术语 |

**`prompt_audio` 是 URL 不是路径**：`GET /examples` 每条里的 `prompt_audio` 形如 `/examples/voice_01.wav`，
是供前端直接播放的相对 URL。纯远程部署时客户端本地没有这个文件，必须走 `GET /examples/{name}` 下载
（`tts_client.py` 的 `--voice` 已内置这个回落）。

**术语级联开关**：`cascade=true` 时替换结果会再被其它词条继续匹配（长词优先）；`false` 时用私有区占位符做单遍替换，避免"A→B，B→C"这类连锁误伤。默认 `false`。

---

## 5. 状态码约定

| 码 | 场景 |
|---|---|
| 200 | 正常 |
| 400 | 参数非法（格式、范围、必填缺失） |
| 404 | 任务/预设/术语/音频不存在 |
| 409 | 资源冲突：模型正在生成时做 load/restart；创建同名预设 |
| 413 | 文本或音频过大 |
| 422 | FastAPI 类型校验失败（如整型字段传了字符串） |
| 500 | 服务端异常（`detail` 带原因） |
