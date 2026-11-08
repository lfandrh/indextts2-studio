# 命令行速查与定位细节

这是 SKILL.md 的展开参考：全部子命令、脚本的能力边界、探活失败分类、项目定位顺序。
单个命令的即时参数以 `python "$CLIENT" <命令> --help` 为准。

## 全部子命令（26 个）

| 命令 | 用途 |
|---|---|
| `locate [--set PATH \| --forget]` | 定位项目根；退出码 3 = 需要用户从三条路里选一条。`--set` 校验后记住路径 |
| `ensure` | 探活；没在跑则启动并等待就绪（失败会打日志尾部） |
| `health` | `GET /health`。连得上输出 `{ok, origin, health}`；**连不上额外带 `reason` + `detail` + `hints`**（见下方「health 说不上话时怎么看」） |
| `status [--json]` | 模型状态 + 显存/内存/CPU 仪表（默认人话，`--json` 给原始响应） |
| `model [--json]` | 模型自己的配置（精度开关 / 扩散步数 / CFG）+ **是否正在生成**。不占 GPU |
| `model-load` | **显式触发加载**（30~60s）。正常不需要：`synth`/`batch`/`doctor` 首次请求会自动加载；只在“必须先加载好再干别的”的编排里用。生成中 409 |
| `model-config --diffusion-steps 20 --cfg-rate 0.6` | 改运行配置，不重建模型。**采样参数下次生成生效；精度开关只写配置，要重启才生效** |
| `model-restart [--diffusion-steps N] [--w2v-fp16 on\|off]` | 按当前配置重建模型（可顺带改配置），30~60s；生成中服务端 409，无 force 通道 |
| `unload [--force]` | 卸载模型释放显存。生成中服务端会拒绝（409）；`--force` 才打断当前生成 |
| `presets` / `preset <名>` | 预设列表 / 单个预设详情 |
| `preset-save --name X --audio Y [--overwrite]` | 把一段参考音频存成命名音色（可带 `--emo-mode/--emo-text/--emo-vector` 与采样参数）；同名默认拒绝，`--overwrite` 才覆盖 |
| `examples` | 内置示例音色清单（名字可直接喂给 `--voice`） |
| `choose` | tty 下交互选预设；非 tty 打印候选并以退出码 2 退出 |
| `segments --text "..." [--out tasks.json]` | 分段预览（不合成）：每段 `text` 是**模型实际读法**，另带 `normalized_text` 与 `numbers`（数字 `原文→读法`）；`--out` 可直接产出 batch 能吃的清单（配 `--preset/--voice/--rate` 写进 defaults） |
| `synth ...` | 合成；默认带回 `preview` 分段/数字读法，`--no-preview` 关闭 |
| `tempo --file X --rate 1.2` | 保音高变速，不重新合成、不占 GPU |
| `stop <job_id> [--keep-model]` | 停止任务；`--keep-model` 保留已加载模型 |
| `last [--open]` | 找回最近一次生成 |
| `history` | 本次服务运行的生成历史（内存态，重启即清） |
| `glossary [--add X --zh Y] [--delete X] [--clear --yes]` | 术语词汇表增删查；`--clear` 会清空全部术语，必须再带 `--yes` |
| `doctor [--no-synth] [--force] [--preset X] [--voice Y]` | 端到端能力体检（真合成一句 + 变速 + 显存/磁盘余量），退出码 0 = 可以开工；结果缓存 30 分钟 |
| `capabilities` | 机器可读能力清单：音色、情感模式、限制、命令表、退出码语义（给上游 Skill 用） |
| `api [--json] [--strict]` | **接口对账**：拉服务端 `openapi.json`，和脚本已适配的接口清单比对。列出"服务端有但没适配"与"脚本在调但服务端没有"，并按 longform/timeline 分层归类 |
| `batch [-f tasks.json] [--resume\|--resume-any] [--only id] [--rate 1.1] [--fail-fast]` | 批量合成；`--resume` 按**内容指纹**跳过（文本/音色/参数/变速没变才跳），`--only` 只重跑指定句，manifest 逐条落盘，单条失败不中断 |
| `selftest` | 只读自检（不合成） |
| `--version`（全局） | 打印 skill 版本，如 `indextts2-tts 1.2.5` |

## 这个脚本**不**做什么

- 预设的删除 / 改名 / 复制（`DELETE /presets/{name}`、`/rename`、`/duplicate`）：AI 判断不了
  哪个音色该留、该叫什么名字，这类整理归人。
- 历史清理（`DELETE /history*`、`POST /history/config`）：不可回溯，不交给 agent。
- 长篇台本工作台（`/lf/*`，81 个接口）和多轨时间轴（`/lf/tl/*`，31 个接口）：那是 WebUI 的
  编辑工程层（素材处理、分轨混音、工程存档），和"文字出语音"不是一层。
- `GET /jobs/*`：都强制要 `client_id`，那是浏览器标签页生成的，脚本拿不到，调了也是 400。

需要这些能力时直接打 HTTP（字段见 `references/api.md`），不要 fiddle 脚本。
不确定整合包升级后有没有新接口、或路径有没有变，跑 `api` 对账——这是唯一的漂移探针。

## health 说不上话时怎么看

连不上时 `health` 会带 `reason`，**四种原因的处理办法完全不同，别一句"服务挂了"糊过去**：

| `reason` | 含义 | 该做什么 |
|---|---|---|
| `connection_refused` | 端口没人监听 | 服务没启动 → 让用户双击 `start_server.bat`，或跑 `ensure`；也可能是 `INDEXTTS_URL` 端口写错 |
| `timeout` | 有人监听但 5 秒没应答 | 进程在但转不过来（刚启动没绑好路由，或正被一次生成占满），等 30~60s 再探；**不是模型加载**（懒加载发生在 `/tts` 内部，不会让探活超时） |
| `http_error` | 端口有 HTTP 服务，但它不是 IndexTTS2 | 端口被别的程序占了（`detail` 里能看到对方返回什么），换端口或停掉占用进程 |
| `dns_error` | 主机名解析不了 | 改用 IP，或检查网络/代理 |

`hints` 里已经写好了对应的下一步，**原样转给用户即可**，不要自己另编排查步骤。
`detail` 超过 160 字会被截断并带 `…`——那是别的 Web 服务返回的 HTML，不是本项目的信息。

退出码：`0`=成功，`1`=失败，`2`=**需要先确认预设/音色**（见到 2 必须停下来问），`3`=**需要用户提供项目位置**（见到 3 必须把三条路摆给用户，见 Step 0）。
headless 模式下 2 和 3 会**额外在 stdout 给结构化 JSON**（`need` + 候选），供上游程序化决策——见「被其他 Skill 调用」。

## 定位细节（Step 0 的展开）

### 自动定位的顺序

脚本按这个顺序找，**越靠前越可信**，命中即停：

1. `INDEXTTS_PROJECT_DIR` 环境变量
2. 从脚本位置向上（skill 随包放进项目内时，向上第 3 层就是包根）
3. 从 cwd 向上（用户把包目录当工作区打开）
4. `~/.indextts2-tts/project.json`（`locate --set` 记住的路径；目录被移动/删除会自动失效）
5. **从本地监听端口反查**：`netstat -ano` 取监听 PID → ctypes 读进程 exe → 沿目录向上找 `server.py`
   （便携包是 `runtime\python.exe`，venv 是 `.venv\Scripts\python.exe`，逐级校验都撞得到）
6. 全失败 → 退出码 3，回到三条路

第 5 条只认**监听行**（对端端口为 0），不会把浏览器/客户端那条 ESTABLISHED 连接误当成服务。

### 怎么认包根

目录里**同时**有 `server.py` 和 `indextts\` 才算。整合包解压后长这样：

```
IndexTTS2.0_v3\
  ├ server.py          ← 认准这一层
  ├ indextts\
  ├ runtime\
  ├ checkpoints\
  └ start_server.bat
```

别选进 `runtime\`——用户常犯的错就是看到 `python.exe` 以为那是根。

### 便携包把路径钉死

```bat
:: Windows cmd
set SKILL_DIR=%USERPROFILE%\.workbuddy\skills\indextts2-tts
set INDEXTTS_PROJECT_DIR=<盘符>:\IndexTTS2.0_v3
python "%SKILL_DIR%\scripts\tts_client.py" ensure
```

```bash
# bash / Git Bash
export SKILL_DIR="$HOME/.workbuddy/skills/indextts2-tts"   # 改成宿主实际安装位置
export INDEXTTS_PROJECT_DIR=/<盘符>/IndexTTS2.0_v3
python "$SKILL_DIR/scripts/tts_client.py" ensure
```
