# 上游 Skill 对接：batch 细节

SKILL.md「被其他 Skill 调用」的展开参考：批量清单格式、manifest 字段、断点续跑、并发与示例。

## batch 的输入输出

输入（`-f tasks.json`；省略或 `-` 读 stdin）：

```json
{
  "defaults": { "preset": "胡桃", "rate": 1.0 },
  "items": [
    { "id": "line_001", "text": "第一句台词。" },
    { "id": "line_002", "text": "第二句要快一点。", "rate": 1.15 },
    { "id": "line_003", "text_file": "line3.txt", "emo_mode": 3, "emo_text": "愤怒地质问" }
  ]
}
```

- `id` 决定输出文件名，也是 manifest 里回指的唯一键。**必须唯一**：同一个 id（或归一化后
  同名，如 `第1句/开场` 与 `第1句:开场`）会写同一个 `.wav`，后一条静默覆盖前一条。
  客户端在合成前就拦下并退出码 1（`reason: duplicate_ids`），不会让你拿到两份一样的音频。
- `defaults` 会被每项覆盖；`text` 与 `text_file` 二选一（后者容忍 BOM）。
  **`text_file` 的相对路径按清单文件所在目录解析**，不是 cwd——上游从别的工作目录用
  `-f /abs/tasks.json` 调用时不会踩空。
- `rate` 是合成后的**保音高变速**，不重新推理。统一调语速用它，别去改 `length_penalty`（不线性）。

输出（stdout 一行 JSON，同时**逐条落盘**到 `<outdir>/<日期>/manifest.json`）：

```json
{"ok":false,"skill_version":"1.2.5","source":"tasks.json","manifest":".../manifest.json",
 "count":3,"done":2,"skipped":0,"failed":1,"not_run":0,"interrupted":false,
 "filtered_out":0,"only":null,"total_audio_seconds":7.2,"failed_ids":["line_003"],
 "items":[{"id":"line_001","ok":true,"out":"...","duration":2.41,"sample_rate":22050,"rate":1.0},
          {"id":"line_003","ok":false,"error":"..."}]}
```

- **`duration` 是给对齐时间轴用的**：服务端 `done` 事件不含时长，客户端读 wav 头量。
- 单条失败不中断，跑完再汇总；`--fail-fast` 改成遇错即停。
- **manifest 每完成一条就刷新**：进程被杀/Ctrl-C 时前面条目的时长与路径都还在，
  只是 `"interrupted": true`、`ok: false`。
- 退出码：`0`=全部成功，`1`=有失败或被中断（看 `failed_ids` / `interrupted`）。

## 返工与断点续跑

```bash
python "$CLIENT" batch -f tasks.json --resume          # 只重跑"内容变了"的
python "$CLIENT" batch -f tasks.json --only line_012   # 只重跑指定句
```

`--resume` 判的是**内容指纹**（文本 + 预设/音色 + emo 参数 + rate），不是"文件在不在"：

| 情况 | 行为 |
|---|---|
| 指纹一致 | 跳过，`skipped: true`、`reason: "unchanged"`，`duration` 从文件读 |
| 文本/音色/emo/rate 改过 | **重新生成**（这是"改稿后配音"的正确行为） |
| wav 读不出时长（上次写一半） | 重新生成，不复用坏文件 |
| 旧版本留下的文件，没指纹记录 | 重新生成一次（宁可多跑一句，也不交付旧音频） |
| 加 `--resume-any` | 老语义：文件存在就跳过，不看内容 |

全部命中时**连服务都不探活**，秒回。返工只改一句就用 `--only <id>`，比整批传一遍快。

## 并发

服务端全局单锁串行推理，**不要并行跑多个 `batch` / `synth`**。上游有多轨需求就并成一个列表交给
`batch`——它在一个进程里串行跑完，锁只抢一次。

## 上游对接示例

```python
env = {**os.environ, "INDEXTTS_HEADLESS": "1"}

caps = json.loads(run([py, client, "capabilities"], env=env).stdout)
if not caps["ok"]:
    return "TTS 服务不可用（先跑 doctor / locate）"

tasks = {"defaults": {"preset": "胡桃"},
         "items": [{"id": s.id, "text": s.text} for s in script]}
m = json.loads(run([py, client, "batch", "-f", "-"],
                   input=json.dumps(tasks), env=env).stdout)

for it in m["items"]:
    if it["ok"]:
        timeline.place(it["id"], it["out"], it["duration"])
```
