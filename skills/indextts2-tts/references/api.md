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
| `POST /model/load` | `model-load`（显式加载；正常由首个 `/tts` 自动触发） |
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

其余接口（预设改名复制删除、历史清理、`/jobs/*`、`/metrics`、`/prefs/*`、
`/save-dir/*`、`/glossary/cascade`）没有封装，需要时按下面的字段直接打 HTTP。
`/lf/*`（长篇台本）与 `/lf/tl/*`（时间轴）是部分整合包才装的 WebUI 扩展，本 skill 不覆盖、
本文档也不展开；你的包有没有，跑 `tts_client.py api` 看对账结果。

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
  "gpu_name": "NVIDIA GeForce RTX 3060 Ti",
  "gpt_batch": {
    "preview": { "auto_rows": 3, "budget_gib": 1.42, "free_gib": 5.31 },
    "last": { "mode": "auto", "rows": 3, "rows_requested": 3, "segments": 7,
              "groups": 3, "budget_gib": 1.42, "reason": "" }
  }
}
```

无 GPU 时 `gpu_*` 字段为空或缺失。

`gpt_batch` 是段级批处理（`gpt_batch_size`）的**本机实际状态**，模型页靠它展示"自动模式在这台机器上
能拼几行"，所以 `/status` 和 `/model` 都带这个字段，前端不必为它单发一次请求：

| 字段 | 说明 |
|---|---|
| `preview` | 按**当前空闲显存**估算的自动行数。模型未加载时为 `null`（此时显存数字没有参考价值，前端显示"加载后显示本机行数"而不是编一个数）；生成期间也沿用上一次空闲时的结果 |
| `preview.auto_rows` | 自动模式（`gpt_batch_size=-1`）在本机实际会拼几行 |
| `preview.budget_gib` / `free_gib` | 参与判定的一行预算显存 / 当前空闲显存（GiB） |
| `last` | 上一次**真实生成**的运行结果；从未生成过时为 `null` |
| `last.mode` | `auto`（设置值为负）/ `manual`（>1）/ `off`（0 或 1） |
| `last.segments` | 上次切出多少段 |
| `last.groups` | 实际拼成的批数。**只有真的拼了批才 >0**；逐段生成路径恒为 `0` |
| `last.rows` | 批内实际行数；逐段路径在请求值 ≤1 时记 `0` |
| `last.rows_requested` | 拼批时为**夹取前**的行数（与 `rows` 不等就说明被显存压过）；逐段时为原始设置值（`-1` 自动 / `0` 关闭） |
| `last.reason` | 空串 = 正常拼批；其余见下表 |

| `reason` | 含义 | 出现时 `groups` |
|---|---|---|
| `""` | 已按批生成 | 批数（≥1） |
| `clamped` | 显存不足，行数被压小后拼批 | 批数（≥1） |
| `streaming` | 走了流式路径，批处理未启用 | `0` |
| `single_segment` | 只有一段，无需拼批 | `0` |
| `low_vram` | 自动模式算出来只够 1 行，退回逐段 | `0` |
| `disabled` | 设置值 ≤0，功能关闭 | `0` |

### `GET /model`

同 `status` 的模型部分（含 `gpt_batch`），**不**带 `cpu_*` / `mem_*` / `gpu_*` 硬件仪表，额外带
`generating`。用于在点按钮前判断"现在动模型会不会打断生成"。

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

Body 是 JSON，只接受这 8 个 key，未知 key 一律 **400**（防拼写错误被静默吞掉）：

| key | 类型 | 生效时机 |
|---|---|---|
| `diffusion_steps` | int 1–50 | 立即 |
| `inference_cfg_rate` | float 0–1 | 立即 |
| `gpt_batch_size` | int −1–12 | 立即（下次生成生效） |
| `cudnn_benchmark` | bool | 立即 |
| `fp16` | bool | **需 restart** |
| `s2mel_fp16` | bool | **需 restart** |
| `w2v_fp16` | bool | **需 restart** |
| `qwen_fp16` | bool | **需 restart** |

**全或无**：任一个 key 类型不对、是 `null`/NaN/无穷、或布尔键传了 `1`/`"true"` 之外的非布尔值，
整个请求 **400** 并在 `detail` 里点名是哪个参数——不再默默取默认值，也不会出现「改了一半」。
数值键按上表区间夹取（夹取本身是允许的，与界面「保存配置」的口径一致）。

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
| 音频非白名单格式 / > 32 MB | 400 |
| `emo_mode=1` 缺 `emo_audio` | 400 |
| `emo_mode=3` 缺 `emo_text`（或全空白） | 400 |
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
| `file` | 音频文件（同样受 32 MB / 格式白名单约束） |
| `rate` | 倍率，`0.5`–`2.0`，超出返回 400 |

底层是 audiotsm WSOLA（MIT），不是 librosa 相位声码器——所以没有"机器人音"。

### `POST /audio/extract`

从视频（或受支持的音频容器）抽出音轨，**不落盘**，直接返回 22.05kHz 单声道 WAV（`audio/wav`）。前端参考音频入口选到视频时调用，拿到的 WAV 再走普通的选文件 / 裁剪 / `/tts` 流程。

| 字段 | 说明 |
|---|---|
| `file` | 视频文件（mp4/mov/m4v/mkv/webm/avi/flv，≤ 200 MB）或受支持的音频容器 |

服务端没有 ffmpeg 时 **400**（提示先转码）；超过 200 MB **413**；只取前 **10 分钟**（`VIDEO_MAX_SECONDS=600`）；其余无法识别的文件 **400**。

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
| `POST /presets` | 创建。同名 **409**；音频不是可识别格式 **400**；音频 > 32 MB **413**。`advanced_params` 落盘前按 `/tts` 的参数区间夹取，所以存进去的已经是夹过的值 |
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
| `GET /examples/{name}` | **返回该示例的音频文件本体**（`FileResponse`），不是 JSON 详情——名字对不上任何 `cases.jsonl` 条目也能取到 `examples/` 目录下的同名音频。**只放行音频扩展名**（`.wav` `.mp3` `.flac` `.ogg`），`cases.jsonl`、`batch/` 子目录、无扩展名一律 **404** |
| `POST /segments` | `text` + `max_text_tokens_per_segment`（+ 可选 `scenario_id`）→ 预览自动分段结果，**不合成**。**只吃表单**；响应另带 `normalized_text`、`numbers`、`scenario_id/scenario_name` |
| `GET /history` | 本次服务运行的生成历史（**内存态，重启即清**），最多 20 条 |
| `POST /history/config` | `auto_clean` 开关：超出 20 条时是否连带删除音频文件 |
| `DELETE /history` | 清空历史 |
| `DELETE /history/{item_id}` | 删单条 |
| `GET /metrics` | 轻量指标（供监控轮询） |
| `GET /glossary` | 规则文档：`rules` / `scenarios` / `active_scenario` / `rev` / `cascade`，另带旧字段 `glossary`（默认场景扁平表） |
| `POST /glossary` | `term` + `reading_zh` / `reading_en`，写入 `checkpoints/glossary_rules.yaml`（`glossary.yaml` 同步为兼容快照）；新规则加入服务端当前场景 |
| `POST /glossary/cascade` | `cascade=true\|false`，只改级联开关不动术语表 |
| `DELETE /glossary/{term}` | 删单条（并从所有场景移除引用）；术语可含空格/中文/符号，需 percent-encode |
| `DELETE /glossary` | 清空规则；场景结构保留（变空场景） |

**`prompt_audio` 是 URL 不是路径**：`GET /examples` 每条里的 `prompt_audio` 形如 `/examples/voice_01.wav`，
是供前端直接播放的相对 URL。纯远程部署时客户端本地没有这个文件，必须走 `GET /examples/{name}` 下载
（`tts_client.py` 的 `--voice` 已内置这个回落）。

**术语级联开关**：`cascade=true` 时替换结果会再被其它词条继续匹配（长词优先）；`false` 时用私有区占位符做单遍替换，避免"A→B，B→C"这类连锁误伤。默认 `false`。

**`POST /segments` 只接受表单**（`application/x-www-form-urlencoded` 或 `multipart/form-data`）。
以前 JSON body 也回 200：`text` 取到默认值 `""`，于是返回 `{"segments": []}`——看起来像"这段文本被切出了
0 段"，而且顺手绕过了超长护栏。现在非表单的 `Content-Type` 直接 **415**，`detail` 里写明当前类型。
另外两条：`len(text) > 10000` 字 **413**（这个上限只防误粘贴卡死服务，正常长文请分几次生成）；请求既没有
`Content-Type` 也没有表单时给 **422**「缺少 text 表单字段」，而带了表单但 `text` 为空则正常返回
`{"segments": []}`——空数组只表示"这段文本确实切不出段"，不再兼作"你没传参数"。
`max_text_tokens_per_segment` 按 `/tts` 同一区间夹取，不做硬拒。

**`POST /segments` 的响应里还带规范化和数字读法**（v1.2.5 起），用来在合成前发现"读错字"：

```json
{
  "segments": [{"index": 0, "text": "五零九零显卡,值得买", "tokens": 18}],
  "normalized_text": "五零九零显卡,值得买",
  "numbers": [{"text": "5090", "read": "五千零九十"}],
  "numbers_truncated": false,
  "normalize_fallback": null
}
```

- `segments[].text` 是**模型实际会读的内容**（规范化后、术语表替换后），不是原始输入；
- `numbers` 是逐个数字片段单独规范化的结果（最多 50 条，超出置 `numbers_truncated=true`），
  **只作提示**——真正的读音以上下文里的 `segments[].text` 为准；
- `normalize_fallback` 非 null 表示规范化整条失败退回了原文，读音可能不标准。

`synth` 默认先调一次 `/segments` 并把同样的内容放进 stdout 的 `preview` 字段（`--no-preview` 跳过）；
`/segments` 不可用时跳过预览，不影响合成。

**`POST /glossary` 同一术语再次提交时按列合并**：只填 `reading_zh` 就只改中文读法，`reading_en` 沿用原值
（反之亦然）；两列都填则都替换。旧版是整体覆盖，未提交的那一列被静默清空——条目还在、读法少了一半、界面
上看不出发生过任何事。至少填一种读法，否则 **400**；术语为空 **400**。老格式/手写 YAML 里一个术语对应
一个字符串时，视为中英文读法相同，第一次按列提交会把它展开成 `{zh, en}` 再改其中一列。

落盘是整包事务：内存里复制一份改完再整体校验，然后按「扁平快照 → settings → rules」顺序**原子写**，
`glossary_rules.yaml` 最后写、作为提交点；任何一步失败都会回滚已写文件且不发布内存状态，接口返回 **500**。
保存成功后服务端把已加载 normalizer 的兜底词典同步为默认场景；显式带 `scenario_id` 的请求用请求开始时的
快照，不受影响。`POST /glossary/cascade` 只改 settings，不递增 rev、不重写规则文件。

---

## 5. 界面偏好（prefs）与保存目录

偏好存在服务端 `data/ui_state.json`，**不是浏览器的 localStorage**：换浏览器、清缓存、换端口都不丢。

| 端点 | 说明 |
|---|---|
| `GET /prefs/param-lock` | `{"locked": bool, "values": {...}, "updated_at": ts}`；未设置过返回 `{"locked": false, "values": {}}` |
| `PUT /prefs/param-lock` | 整体写入锁定快照。**`locked` 必须显式给出**（缺失 **400**，不再默默取 `false` 把已存的锁定清空）；`values` 的 key 必须都在参数白名单里，未知 key **400** 并点名；值必须是有效数字（NaN/无穷 **400**），`doSample` 例外（按布尔取） |
| `GET /prefs/save-dir` | `{"dir": str, "exists": bool, "writable": bool}`，目录不可用时额外带 `reason`（见下）；未设置时 `dir` 为空串 |
| `PUT /prefs/save-dir` | **仅本机**，`{"dir": "路径"}`；空串 = 清除设置 |
| `POST /prefs/save-dir/pick` | **仅本机**，弹系统文件夹选择框 |
| `POST /prefs/save-dir/open` | **仅本机**，用资源管理器打开当前保存目录 |
| `POST /save-dir/outputs/{name}` | 把 `outputs/` 里的生成音频**复制**进保存目录 → `{"ok": true, "path": 落盘全路径, "dir": 目录}` |
| `POST /save-dir/upload` | 接收前端算好的剪辑 WAV 并落盘（`multipart`，`file` + 可选 `name`），返回同上 |

**「仅本机」的判定**：`request.client.host` 在回退/本地地址集合里。从局域网另一台设备访问时这四个
写偏好端点返回 **403**，`detail` 会说"只有运行服务的这台电脑可以……"。
`/save-dir/outputs` 与 `/save-dir/upload` **不受此限制**（任何浏览器都能保存），代价是文件落在
**运行服务的那台电脑**上，而不是当前设备的磁盘。

### 目录探针：限时、缓存、失败要说清原因

`os.path.isdir` / `os.path.exists` 本身也会阻塞——指向断掉的网卡路径（`\\10.255.255.1\share`）时
实测卡 **26.7 秒**。这些端点是同步 `def`，跑在 anyio 默认 40 线程池里，挂住的线程不释放；实测 45 次
请求后线程池耗尽，连 `/health`、`/status` 都排队超时。所以护栏是三层，存在性检查和写探针**都在**
私有池里：

| 层 | 值 |
|---|---|
| 私有线程池 | `max_workers=2`（卡死的探针线程被困在这里，泄漏有上界，不吃那 40 个线程） |
| 单次限时 | `future.result(timeout=2.0)`，超时按「不可用」返回 |
| 按目录缓存 | 15 秒；并发请求全局串行 + 双检，后来的直接命中缓存 |

`reason` 的取值（`exists=false` 或 `writable=false` 时才有意义）：

| `reason` | 触发 |
|---|---|
| `目录不存在` | `isdir` 和 `exists` 都失败 |
| `这是一个文件，不是目录` | 路径存在但是文件 |
| `没有写入权限` | 写探针 `os.open(O_CREAT\|O_EXCL\|O_WRONLY)` 抛 `PermissionError` |
| `目录已不存在` | 探针期间目录被删掉（`FileNotFoundError`） |
| `无法创建文件（…）` | 其它 `OSError`，括号里是 `strerror` |
| `目录检查超过 2s 未返回，按不可用处理` | 探针超时 |
| `目录检查异常（异常类名）` | 探针自身意外异常，不让它把端点变成 500 |

用 `os.access(W_OK)` 不够：Windows 上只读目录也会返回 `True`，所以探针真的建一个临时文件再删掉。
**`PUT /prefs/save-dir` 与真正的写盘路径不吃 15 秒缓存**（`use_cache=False`）——缓存期内目录被删/改名时，
不能再拿旧判定说它「可用」，否则错误会推迟到写盘那一刻以另一副面孔出现。

### `PUT /prefs/save-dir` 的结果

| 情况 | 结果 |
|---|---|
| 非本机 | **403** |
| `dir` 缺失或不是字符串 | **400**「dir 必须是字符串（传空字符串表示清除）」 |
| 空串 / 只有空白 / 只有引号 | 清除设置，返回 `{"ok": true, "dir": "", "exists": false, "writable": false}` |
| 相对路径 | 按**服务进程的工作目录**解析（`os.path.abspath`，不是浏览器那台机器），响应带 `relative: true` 和解析后的绝对 `dir`，前端据此显示给用户 |
| 目录不存在 / 是文件 | **400**，`detail` = `reason` + 路径 |
| 目录不可写 | **400**「目录不可写（原因）：路径」 |
| `ui_state.json` 写不进去（磁盘满等） | **503**「界面偏好写入失败（原因），请稍后重试」 |

`POST /prefs/save-dir/pick`：同一时刻只允许一个选择框，第二个请求 **409**；没有 `tkinter` 或弹框失败
**501** 并提示手动输入路径；用户取消返回 `{"cancelled": true}`。

### 保存落盘（`/save-dir/outputs/{name}` 与 `/save-dir/upload`）

- 文件名先收敛：去路径（只取 `basename`）、非法字符 `<>:"/\|?*` 与控制字符替换为 `_`、首尾的点/空格去掉、
  不是 `.wav` 就补上、Windows 保留名（`CON`/`LPT1`/`COM1`…）前面加 `_`。
- 长度按 **UTF-16 单元**（补充平面字符算 2）而不是字符数计：单个名字 100 单元、整条路径 250 单元
  （`MAX_PATH` 260 留 10）。超了**自动截短**，不报错。
- 同名不覆盖：`O_CREAT\|O_EXCL` 逐个试 `名字 (1).wav`、`(2)`……撞满 9999 个才 **409**。
- 写入失败（含目录在探针后消失）：关流 → **删掉半成品** → 翻译成可读错误，绝不在用户目录留 0 字节坏文件。
  成功路径只 `flush`，不做 `fsync`（同步 handler 里任何无上限阻塞都会吃线程）。
- 错误码：不存在/非目录 **409**、不可写 **403**、文件名过长 **400**、磁盘满 **507**、其余 **500**。
- **服务端绝对路径只进日志**，`detail` 只有可读原因（`PUT` 那三个 400 例外：那里回显的是用户自己刚提交的路径）。

---

## 6. 状态码约定

| 码 | 场景 |
|---|---|
| 200 | 正常 |
| 400 | 参数非法（格式、范围、必填缺失、未知 key、保存目录不可用/不可写） |
| 403 | 非本机调用只做本机的事（改保存目录、弹选择框、打开目录）；保存目录没有写权限 |
| 404 | 任务/预设/术语/音频/历史条目不存在 |
| 409 | 资源冲突：模型正在生成时做 load/restart/unload、创建同名预设、保存目录失效、同名文件撞满 9999 个 |
| 413 | 文本或音频过大 |
| 415 | 该端点只吃表单，`Content-Type` 却是 JSON 等其它类型（如 `POST /segments`） |
| 422 | FastAPI 类型校验失败（如整型字段传了字符串）、缺整个 body |
| 500 | 服务端异常（`detail` 带原因，不含服务端路径） |
| 501 | 当前环境做不到（没有 `tkinter` 弹不了选择框、系统不支持打开文件夹） |
| 503 | 界面偏好写盘失败（`data/ui_state.json` 写不进去） |
| 507 | 磁盘空间不足，写不进保存目录 |
