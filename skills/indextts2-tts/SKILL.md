---
name: indextts2-tts
version: 1.2.5
description: 调用 IndexTTS2 HTTP 服务做零样本语音合成（音色克隆）时使用。覆盖：项目目录定位（三条路：用户给路径 / 启动服务后反查端口 / 没有整合包则引导去 B 站获取）、端到端能力体检（doctor：探活+接口+冒烟合成+变速+显存与磁盘余量，确认"现在就能出声"）、能力清单（capabilities，供上游 Skill 发现音色与限制）、单条与批量合成（batch：几十上百句一次跑完、id 撞车提前拦截、单条失败不中断、按内容指纹断点续跑、只重跑改动句、manifest 逐条落盘、统一变速、回传每句时长）、预设系统（参考音频+参数一键复用，生成前强制向用户确认音色）、四种情感控制模式（同参考音频/情感音频/8维向量/情感文本）、内置示例音色（本地缺文件自动下载）、长文本分段、分段与数字读音预览（合成前核对模型实际读法，数字读错用术语表纠正）、SSE 事件流解析、保音高变速（tempo，不重新合成）、术语词汇表（多音字/数字/缩写读音纠错）、防并行合成锁、生成音频找回、只读自检、无头模式（INDEXTTS_HEADLESS，给视频配音等上游 Skill 调用）。含故障排查：模型懒加载语义（model_loaded=false 不是"正在加载"，不要轮询等待）、显存不足与 OOM（8G 卡两种表现）、音色不像/带杂音的输入检查、中断后重跑排队、磁盘满。支持本机/远程/Docker/便携整合包部署，配置全走环境变量。触发词：IndexTTS、indextts2、语音合成、TTS、音色克隆、参考音频、预设合成、情感向量、语速调整、变速、批量配音、视频配音、有声书、整合包、项目在哪、找不到项目、能不能用、体检、显存不足、OOM、变慢、声音不像、有杂音、读音错、多音字、数字读错、模型没加载、一直加载中。
---

# IndexTTS2 语音合成客户端

**一句话**：合成入口是 `POST {INDEXTTS_URL}/api/v1/tts`（multipart/form-data，必传参考音频 `spk_audio`），返回 SSE 事件流，`done` 事件里带 wav 下载 URL。

> **最重要的一条：模型是懒加载的。**
> 服务起来之后，`health` / `status` / `model` 里的 `model_loaded:false` 是**正常状态**，
> 不存在“正在加载、等一会儿就好”的阶段。不主动发合成请求，它永远不会自己加载。
> 要加载模型只有三种做法：**直接 `synth` / `batch`**、**跑 `doctor`**、或显式 **`model-load`**。
> **绝对不要轮询 `model_loaded` 等它变 true**——那会卡死在启动之后。

**版本**：`python "$CLIENT" --version`（写作时 1.2.5）。排障第一步先要版本 ——
skill 可能有用户级与项目内两份副本，没版本号既判断不了用户装的是哪一版，
也看不出两份是否漂移。

**优先用本 skill 自带的 `scripts/tts_client.py`**（纯标准库，零依赖，Python 3.10+）。它已封装：服务探活与启动（失败打日志尾部）、预设确认守卫、防并行文件锁、SSE 解析、中文路径下载、示例音色自动下载、保音高变速、音频找回、统一错误信息。不要手搓 HTTP 请求，除非你要调用的能力脚本没覆盖（清单见下文「这个脚本不做什么」）。

## 六条硬规则（第 2~4 条由脚本强制，第 0~1、5 条是交互契约）

0. **第一句话先讲这个 Skill 是干什么的**。用户刚调用本 skill 时，他只知道"点了个东西"，
   不知道这东西能帮他做什么。所以**本会话的第一次响应，必须先介绍能力与流程**（话术见
   「开场」一节），**然后**才说"我现在去干什么"。
   顺序不能反：先让人知道"这是干什么的"，再让人知道"你在干什么"。
   只在该会话第一次响应说一次，之后不要再重复。
1. **每一步动手前先说一句**。任何工具调用之前，先用一句话告诉用户**你正在做什么、大概要多久**。
   超过 10 秒的步骤（`ensure` 起服务、`doctor` 首次加载模型、长文本合成）**必须先报预期**。
   **不允许**连跑 Step 0 → 1 → 2 再一次性汇报——那段时间用户看到的只有"卡住了"，
   他不知道你在找路径、在起服务、还是在加载模型。
   脚本打到 stderr 的提示（`[ensure]`、`[doctor]`、`[progress]`）是给人看的，**要转述，别吞掉**。
   同理，stdout 的 JSON 是给你看的，**不要原样贴给用户**——把里面的信息翻译成一句人话。
   这条规则没有例外：哪怕只是跑一次 `locate`，也要先开口。
2. **生成前必须确认音色**。`synth` 既没 `--preset` 也没 `--voice` 时，打印候选清单并以**退出码 2** 中止。agent 必须停下来向用户询问，不允许自行挑一个。
3. **禁止并行生成**。服务端是全局单锁 + 队列，并发请求只会排队；客户端再加一把文件锁（`--lock-wait` 秒，默认排队 3600s，`0` 表示抢不到立即失败）避免多条命令互相挂死。
4. **音频必须可找回**。不传 `--out` 时落到 `<输出目录>/<YYYYMMDD>/<服务端文件名>` 并写 `last.json`；`last` 子命令随时找回最近一次。
5. **不要等模型加载**。`ensure` 只保证 HTTP 服务起来了，`model_loaded:false` 不是错误、也不是进度条。
   任何“先等模型加载好再开始”的写法都是死循环：没有 `/tts` 请求触发，模型不会自己加载。
   下一步直接 `doctor` / `synth` / `batch`，或 `model-load`；这三条都会阻塞到加载完成再返回。
   脚本在 `health` / `status` / `model` / `ensure` 的输出里都带了这句提示，看到就照做，不要自己加轮询。

## 配置：全部走环境变量，无硬编码路径

脚本不 import 项目代码，只走 HTTP，所以对服务部署位置无假设——本机、远程服务器、Docker、反向代理后面都能用。

| 变量 | 默认 | 说明 |
|---|---|---|
| `INDEXTTS_URL` | `http://127.0.0.1:7860` | 服务地址。连远程/换端口只需改这一个 |
| `INDEXTTS_PROJECT_DIR` | 自动探测 | 项目根（含 `server.py` 与 `indextts/`）。**仅 `ensure` 需要**；定位顺序见 Step 0（环境变量 → 脚本位置 → cwd → 记忆文件 → 端口反查）。找不到时跑 `locate` 拿三条路 |
| `INDEXTTS_OUT_DIR` | `<项目根>/outputs/agent_tts` | agent 默认输出目录；探测不到项目根时退到 `~/.indextts2-tts/agent_tts` |
| `INDEXTTS_CONFIG_DIR` | `~/.indextts2-tts` | 定位记忆（`project.json`）与 `doctor` 缓存的存放目录；测试或多环境可隔离 |
| `INDEXTTS_START_TIMEOUT` | `120` | `ensure` 拉起服务后的探活上限（秒） |
| `INDEXTTS_HTTP_TIMEOUT` | `900` | 单次请求 socket 超时（秒）。首次合成要等模型加载，别调小 |
| `INDEXTTS_PROXY` | 空（**不走代理**） | 默认绕过系统代理，见下方说明 |
| `INDEXTTS_HEADLESS` | 空 | 置 `1` = 无头模式（等价全局 `--headless`）：该问人的地方 stdout 输出 JSON，供上游程序化调用 |

**关于代理**：脚本默认禁用 `HTTP_PROXY`/`HTTPS_PROXY`。企业网、CI、agent 沙箱常设全局代理，会把发往 `127.0.0.1` 的请求也交给代理，拿到 `502 upstream connect failed`，看起来像服务挂了。确实需要走代理时设 `INDEXTTS_PROXY=http://host:port`。

**关于启动方式**：`ensure` 不调用项目自带的 `start_server.bat` / `.sh`（那些脚本带本机路径假设）。它按 `<项目根>/runtime/` 解释器（便携分发包）→ `uv run --extra server --no-sync python server.py` → `<项目根>/.venv` 解释器 → 当前解释器的顺序探测，端口取自 `INDEXTTS_URL`。**`runtime/` 排第一是有意的**：分发包只有 `runtime/`，没有 `.venv` 也没装 uv，让 uv 抢在前面会往用户的包里新建空 `.venv` 再报缺依赖。

## 开场：第一句话先讲这个 Skill 是干什么的

用户调用时只知道"点了个东西"。**本会话第一次响应，先讲清四件事再动手**（硬规则 0）：
**是谁的包 → 能干哪两类活 → 要用户给什么 → 接下来做什么**；同会话只念一次，不超过 10 行。

用户没给具体任务时，用这个口吻：

> 这个 Skill 帮你调用 **B 站 UP 主「十一君Jun」制作的 IndexTTS2 超级优化版整合包**，
> 在本机完成**视频配音、文字转语音**——给一段 3~10 秒参考音频就能克隆音色，不用训练。
> 流程四步：① 找整合包（没装我告诉你去哪拿）→ ② 起服务并试合成一句 → ③ 你给参考音频和文稿
> → ④ 我出音并交回文件。现在从第 1 步开始，我先找一下包在哪，几秒。

用户已经说清任务（"把这段文稿配成音频"）时，只确认能做什么 + 先花一两分钟体检，不要背全文。

三条边界：**只讲能干的**，不主动列短板；**排障提问不念介绍**，直接进排查；
**同会话只念一次**。开场别塞 B 站链接——等真的找不到包时再给。

## Step 0 — 先定位项目（标准流程的前置，做任何事之前）

**这一步不能跳。** skill 只是调用客户端，不含模型和代码，说清"项目在哪"才谈得上合成。

**动手前先开口**（这是硬规则 1，最容易漏的一步）：

> 我先找一下你的 IndexTTS2 整合包在哪，大概几秒。

如果这是本会话第一次响应，**先说上一节的开场白，再说这句**——不能只有这句，否则用户
只知道你在"找什么东西"，不知道找的是干什么用的东西。

不要一上来就静默调用工具。用户看不见你的命令，只看见界面在转圈。

然后跑：

```bash
SKILL_DIR="$HOME/.workbuddy/skills/indextts2-tts"     # 改成宿主实际安装位置：Claude Code 用 ~/.claude/skills/indextts2-tts；随仓库分发的用 <项目根>/skills/indextts2-tts
python "$SKILL_DIR/scripts/tts_client.py" locate
```

（下面所有命令都复用这个 `SKILL_DIR`。）

- **成功**：一行 JSON，带 `project_dir` 和 `source`（命中的是哪条规则）。**用一句话汇报后再往下走**：
  > 找到了：`F:\...\IndexTTS2.0_v3`（服务已经在跑，从端口反推出来的）。
  不要贴 JSON，`source` 的值要翻译成人话（`环境变量` / `脚本位置向上` / `当前工作目录向上` /
  `记忆文件` / `端口反查` → "服务正在跑，从端口反推的"）。
- **失败（退出码 3）**：stdout 是 `{"ok": false, "reason": "project_not_found"}`，stderr 会打出三条路。
  **agent 必须停下来把三条路摆给用户选**，不要自己猜路径，也不要全盘扫描。

> **`locate` 只跑一次。** 退出码 3 就是结论，不是"再想想办法"的信号。
> 不要换 `cwd` 重试、不要自己拼 `--set` 去猜路径、不要扫盘（`find` / `dir /s`）、
> 不要连续重跑同一条命令。这些动作加起来能让用户盯着转圈好几分钟，而他只想要你问一句。
> 把三条路摆出来、等用户回话——**这时候停下来才是对的**。

| 用户情况 | 怎么办 | 脚本动作 |
|---|---|---|
| ① 手上有整合包 / 仓库 | 让他把根目录路径发过来 | `locate --set "<路径>"` —— 校验通过后记住，以后不再问 |
| ② 不想翻目录 | 让他双击包里的 `start_server.bat`，等 `http://127.0.0.1:7860` 能打开 | 再跑 `locate`，脚本从占用该端口的进程反推包根 |
| ③ 根本没有整合包 | 只有 skill 是跑不出声音的 | 转告他：B 站 UP 主「十一君Jun」，主页 <https://space.bilibili.com/470166715> ，在投稿里找「Index TTS 2.0」相关的视频，按视频说明获取整合包 |

第 ③ 条要**原话给到用户**，别自己编替代方案——换别的 TTS、让他去 GitHub 下源码都不对，他要的是能跑起来的那个包。

### 自动定位顺序与包根识别

脚本依次查：环境变量 → 脚本位置向上 → cwd 向上 → 记忆文件 → 本地监听端口反查。
目录里**同时**有 `server.py` 和 `indextts\` 才算包根，别选进 `runtime\`。完整顺序与目录示例见
`references/cli.md`「定位细节」。

### 便携整合包用户（IndexTTS2.0_v3 这类分发包）

没有 `.venv`，依赖全在 `runtime\`，可能也没装 `uv`。脚本已把 `runtime\python.exe` 排在启动第一位：
让用户双击 `start_server.bat` 后跑 `locate` 即可，不用设路径；无人值守时再
`set INDEXTTS_PROJECT_DIR` 钉死（示例见 `references/cli.md`）。判断是否找对看 `selftest` 的 `project_dir`。

两条纪律：**别用 `uv run` / `uv sync` 启动或"修"分发包**（会新建空 `.venv` 搞乱路径）；
**别用 pip 往 `runtime\` 里装或升级包**（依赖错乱就重新解压干净包，不要继续 pip 修）。

## 标准流程

脚本只用标准库，Python 3.10+ 即可，不需要装任何依赖。**用绝对路径调用，别依赖 cwd。**

**开头就给用户一个整体预期**，别让他不知道自己在用什么、要等多久。
本会话第一次响应按「开场」一节的 A/B 版说完，然后从 Step 0 开始：定位与服务启动是所有任务共用的开头，
之后按下面「先选路径」进入对应分支。
**每一步动手前先说一句，每步结束说一句结果**（硬规则 1）。

### 先选路径

| 要做的事 | 路径 |
|---|---|
| 单句 / 短段落出音 | `doctor` 体检 → 确认音色 → `synth` |
| 长文 / 整篇 / 有声书 | `segments --out tasks.json` 预检 → `batch -f`（几十句一次跑完，**不要循环调 `synth`**） |
| 视频配音等上游 Skill 调用 | `capabilities` 看能力边界 → `batch` 拿 manifest 逐句时长 → 转交音频（见「被其他 Skill 调用」） |
| 建 / 换音色 | `presets` / `preset-save`；生成前必须让用户确认用哪个 |
| 某个词读错 | `segments` 看 `numbers` → `glossary --add --zh` → 再 `segments` 复核 |
| 语速不合适 | `tempo` 对已有音频变速，不重新合成 |
| 改了一句要返工 | `batch --only <id>`；中断后续跑用 `--resume` |
| 连不上 / 显存 / 变慢 | `health` → `status` → `doctor`，再查「陷阱」与 `references/pitfalls.md` |

四条全程不变：**定位只做一次**、**不等模型加载**（直接 `synth` / `batch` 触发）、**绝不并行生成**、
**合成前确认音色和读音**。命令参数看下文各 Step 与「子命令速查」，这里只说顺序。

```bash
SKILL_DIR="$HOME/.workbuddy/skills/indextts2-tts"     # 改成宿主实际安装位置：Claude Code 用 ~/.claude/skills/indextts2-tts；随仓库分发的用 <项目根>/skills/indextts2-tts
CLIENT="$SKILL_DIR/scripts/tts_client.py"
python "$CLIENT" presets
```

Windows cmd：

```bat
set SKILL_DIR=%USERPROFILE%\.workbuddy\skills\indextts2-tts
:: 上一行改成宿主实际安装位置后再跑
python "%SKILL_DIR%\scripts\tts_client.py" presets
```

> 别再写 `cd skills/indextts2-tts`：那只在「cwd 恰好是项目根、且项目里有同名目录」时成立，
> 换个工作目录就找不到脚本，而报错会是 `can't open file` 这种跟音色无关的噪音。

### Step 1 — 确保服务在跑

**先说要多久**——服务没起时这一步最长等 120 秒，最容易让人以为卡死：

> 现在把 TTS 服务起起来。如果本来就在跑就秒过；没跑的话要等它加载，最多两分钟。

```bash
python "$CLIENT" ensure      # 已在跑直接返回；没在跑则后台拉起并探活
```

启动失败时（比如依赖没装）它会**把服务日志最后 20 行直接打出来**，不用再去翻日志文件。

服务起来了要**说一声再进下一步**（"服务起来了"），别闷头继续。

**服务起来 ≠ 模型已加载。** `ensure` / `health` 返回 `model_loaded:false` 是懒加载的正常状态，
不是“正在加载”。下一步 `doctor`（或直接 `synth` / `batch`）才会真正加载模型；
**不要在这里停下来等模型**，也不要去查 `model_loaded` 轮询——它不会自己变。

### Step 2 — 能力体检：确认"现在就能出声"

**这一步必须提前报预期**——首次要加载模型，用户会盯着界面等 30~60 秒：

> 接下来做一次真的合成测试，确认模型能跑、能出声。首次要加载模型，大概半分钟到一分钟，跑完我把它生成的那句话放给你听。

```bash
python "$CLIENT" doctor      # 探活 → 接口 → 真合成一句 → 对这句变速一次
```

这一步不能省。`selftest` 只证明"接口能连"，`doctor` 证明的是"模型能跑、能落盘、能变速"。
输出 JSON 里 `ready: true` 且退出码 `0` 才算过。

- 脚本会往 stderr 打 `[doctor] 冒烟合成中（首次要加载模型，约 30~60s）...`，
  **这行要转述给用户**，不要只留一个转圈的界面。
- **首次 30~60 秒**（要加载模型），结果缓存 30 分钟；`--force` 强制重测，`--no-synth` 只过接口不出声（秒级）。
- 冒烟音频**保留**成试听样本，路径在 `sample.out`。把它给用户听 —— 听到声音比说十遍"已就绪"有用。
- 失败（退出码 1）：JSON 的 `checks` 逐项标明哪步没过，按 `detail` 修。**没通过就别硬上 `synth`**，
  那只会把真正的原因（依赖缺失/显存不足/模型损坏）埋在一堆无关报错里。
- **`vram_headroom` 是告警项，不阻塞**：模型未加载时按增量估"加载后还剩多少"；余量偏低会 warn，
  但 `ready` 仍可能是 true。这种情况下**必须先告诉用户**，并把长文本拆短再跑。
- **冒烟只证明「短句能跑通」**。长文本（>500 字）会吃 KV cache，首次跑请盯一下进度与耗时；
  冒烟 3 秒、长文本 90 秒在同一台机器上是正常的，不是回归。
- **`summary` 里的 `冒烟 2.2s音频/耗时25.2s` 是两个不同的数**：前者是**音频多长**，后者是**真的花了多久**
  （含首次加载模型）。给用户估批量工期只能用后者——拿 `2.2s` 去估 100 句会差一个数量级。

**体检通过后的第一句话**：告诉用户"可以开始生成语音了"，紧接着问要生成什么内容。
这两句要连着说，别只汇报体检结果就停在那等。

### Step 3 — 问用户：生成什么内容 + 用哪个音色

一次问清两件事，别拆成两轮。

```bash
python "$CLIENT" presets     # 服务端有预设时：表格（名称 / emo_mode / 有无参考音频）
python "$CLIENT" examples    # 没有预设时：内置示例音色（可直接喂给 --voice）
```

- 用户手上已有文稿 → 直接进 Step 4。
- 文稿很长（>2000 字）→ 先看分段，**再直接落成批量清单**（这一步别让用户手工拆）：
  ```bash
  python "$CLIENT" segments --text-file 长文.txt --out tasks.json --preset "胡桃"
  python "$CLIENT" batch -f tasks.json          # 分段→合成闭环，不用中转
  ```
- 用户没说用哪个音色 → 把清单原样摆出来让他选，**不要自己挑**（脚本也会以退出码 2 拦住）。
  预设 `/presets` 返回空不等于服务坏了：刚解压的整合包就是这样，用 `--voice` 或在前端建预设。
- 用户丢来一段录音说"以后都用这个声音" → **存成音色**，别每次重新上传：
  ```bash
  python "$CLIENT" preset-save --name "旁白" --audio 参考.wav
  python "$CLIENT" synth --preset "旁白" --text "..."
  ```
  同名默认拒绝（`reason: exists`）；确认要换掉旧的就加 `--overwrite`，不要默认覆盖。

### Step 3.5 — 文本预检：先看模型会怎么读（合成前必做）

拿到文稿后、合成前，先跑一次分段预览。它返回**规范化之后的读法**（数字、符号已经展开成模型实际会念的字），
还有一张数字读法表。合成前核对，比生成完再返工便宜得多。

```bash
python "$CLIENT" segments --text-file 稿子.txt          # 或 --text "..."
python "$CLIENT" segments --text-file 长文.txt --out tasks.json --preset "胡桃"   # 长文顺便落批量清单
```

- 把 `numbers` 里的 `原文→读法` **念给用户核对**，例如 `5090→五千零九十`、`2026年→二零二六年`。
- **数字是最高频的读错点**，尤其显卡型号、价格、编号这类不该按基数词读的内容：
  `5090显卡` 会被读成“五千零九十显卡”。读法不对就用术语表改写成同音汉字：
  ```bash
  python "$CLIENT" glossary --add "5090" --zh "五零九零"
  python "$CLIENT" segments --text-file 稿子.txt        # 复核
  ```
  术语紧贴中文（`5090显卡`、`TTS技术`）现在也能命中，不需要把整句写进词表。
- 词表是**服务端全局**的：加之前确认不会误伤别的项目；只想改这一次就直接改写稿子本身。
- 文本 ≤ 20 字且不含数字/英文缩写时可以跳过这一步；`synth` 仍会在输出 JSON 里带回 `preview`。
- `batch` **不逐条**做预览。长文/多句请先用本步的 `segments` 看全文读法，再生成清单。
- **读音验收就到这里为止，不要用 ASR 回测成片音频**：语音识别输出的是归一文本，`五千零七十` 和
  `五零七零` 都会识别成 `5070`，验不出实际读法。只有下游还要剪停顿（`silenceremove`）、担心首字被削掉时，
  抽听一句即可。原理见 `references/pitfalls.md` §10。

### Step 4 — 合成

**合成前报一句预期**（长文本能跑一分钟以上，不打招呼会像卡死）：

> 开始合成了，这句大概 X 秒。生成过程中会显示进度。

短句（<50 字）一般 3~10 秒，长文本按「字数 ÷ 8」秒粗估。

```bash
# 用预设（自动下载预设里的参考音频 + 套用全部参数）
python "$CLIENT" synth --preset "胡桃" --text "你好，这是预设合成测试。" --out out.wav

# 直接指定参考音频
python "$CLIENT" synth --voice /path/to/ref.wav --text "直接指定参考音频。" --out out.wav

# 内置示例音色（本地没有也没关系，会自动从服务端下载一份）
python "$CLIENT" synth --voice voice_01.wav --text "内置示例音色。" --out out.wav

# 8 维情感向量（顺序：高兴/愤怒/悲伤/害怕/厌恶/忧郁/惊讶/平静）
python "$CLIENT" synth --voice v.wav --emo-mode 2 \
    --emo-vector 0,0,0.8,0,0,0,0,0 --text "..."
```

- 阻塞到生成结束；stdout 输出**一行 JSON**，进度走 stderr。
- **生成前会自动取一次分段预览**，放在 `preview` 字段里（`count` / `segments` / `numbers` /
  `normalized_text`）；stderr 也会打 `[synth] 数字读音：5090→五千零九十`。
  把数字读法转述给用户；读法不对就按 Step 3.5 加词表后重跑。不要预览时加 `--no-preview`。
- `mel_capped=true` 表示音频触到 `max_mel_tokens` 被静默截断 —— 必须告诉用户并建议分段或调大上限。
- CLI 显式传入的参数**优先于**预设里的值。
- 用了与 `emo_mode` 不匹配的情感参数（如没给 `--emo-mode 2` 却传 `--emo-vector`）时，
  脚本会告警"该参数被忽略"——别把这条警告当噪音，服务端确实会丢掉它。
- 采样与情感参数（`--emo-weight` / `--temperature` / `--top-p` / `--top-k` / `--num-beams` /
  `--repetition-penalty` / `--max-mel-tokens` / `--length-penalty`）越界时打
  `[warn] ... server clamps to X` 并就地夹取。这代表用户设的值**没被采纳**，回话时报最终生效值，
  别照抄他的输入；`top_k=0` 是"关闭过滤"，不是"取 0 个"。同一越界值每次运行只提示一次，
  `batch` 里看到一次不代表只有第一条被夹——每条都夹过，只是不重复刷屏。

常用开关：

| 开关 | 说明 |
|---|---|
| `--out PATH` | 指定输出路径；不传则落默认目录 |
| `--outdir DIR` | 换默认输出根目录（仍按日期分子目录） |
| `--text-file PATH` | 从 UTF-8 文件读文本（容忍 BOM），长文本避免 shell 转义问题 |
| `--voice` | 本地音频路径，或内置示例名（`voice_01.wav`）；都找不到时本地不存在会自动下载 |
| `--emo-mode 0\|1\|2\|3` | 情感模式，见 `references/parameters.md` |
| `--emo-text` / `--emo-vector` / `--emo-audio` | 对应 mode 3 / 2 / 1 |
| `--interval-silence MS` | 段间停顿，10~500，越界会告警并夹取 |
| `--seg-tokens N` | 每段 token 上限（默认 120） |
| `--lock-wait SEC` | 等锁上限，`0` = 抢不到立即失败 |
| `--no-start` | 服务没在跑就报错，不自动拉起 |
| `--no-preview` | 跳过生成前的分段/数字读音预览（默认会取；预览失败不影响生成） |

### Step 4b — 语速不对？先别重新合成

```bash
python "$CLIENT" tempo --file out.wav --rate 1.2 --out out_fast.wav
```

`/audio/tempo` 走 audiotsm WSOLA，**保音高**、不重新推理、完全不占 GPU，几秒出结果。
倍率 0.5~2.0。想更快/更慢时优先用它，而不是改 `length_penalty`（效果不线性、不可控）。

### Step 5 — 找回音频

```bash
python "$CLIENT" last            # 最近一次：完整路径 + 元信息 + exists 校验
python "$CLIENT" last --open     # 顺带打开所在目录
```

### Step 6（可选）— 只读自检

```bash
python "$CLIENT" selftest        # 服务/状态/预设/示例音色/输出目录 一次过一遍
```

不合成、不碰 GPU，失败项带 detail。**装完或怀疑环境有问题时先跑它**，
比直接 synth 快，也不会因为音色问题被退出码 2 挡住。

### 显存不够时（8G 卡必读）

两种表现：报错 `CUDA out of memory`，或不报错但慢十倍（Windows 静默借共享显存）。
先 `model` 看是否在生成，再用 `unload`（生成中 409）或调配置。**`model-config` 改精度开关只写配置，
必须 `model-restart` 才生效**；`--diffusion-steps` / `--cfg-rate` 下次生成就生效。
完整命令与踩坑见 `references/pitfalls.md` §9。

## 子命令速查

26 个子命令的完整清单、脚本**不**做什么、`health` 连不上时四种 `reason` 的处理、定位细节，
都在 `references/cli.md`；任何命令的即时参数用 `python "$CLIENT" <命令> --help`。
预设整理、历史清理、长篇台本/时间轴、`/jobs/*` 不在脚本范围，需要时直接打 HTTP，别 fiddle 脚本。

退出码：`0`=成功，`1`=失败，`2`=需要先确认预设/音色（必须停下来问），`3`=需要用户提供项目位置
（把三条路摆给用户）。headless 下 2/3 会在 stdout 给结构化 JSON（`need` + 候选）。

## 改脚本之后怎么自测

`python tests/test_client.py`：47 项离线回归（假服务、无 GPU、约 10 秒），覆盖退出码、
stdout JSON、`--resume` 指纹、manifest、中文编码管道。改完先跑它，再用真实整合包跑
`doctor --force` 验收。细节见 `references/testing.md`。

## 关键事实

| 事实 | 值 |
|---|---|
| 模型加载 | **懒加载**：首个 `/tts`（或显式 `POST /model/load`）才载入 GPU，约 30~60s。`model_loaded:false` 是启动后的正常状态，**不要轮询等待**——没有请求触发就不会加载 |
| 模型驻留 | 加载后**一直驻留**，不会空闲自动卸载。要释放显存需显式 `POST /model/unload`（前端「模型管理」页） |
| 显存占用 | 8G 卡加载后约 6.7 G，余 1.3 G 属正常；余量低于 1.2 G 时长文本明显变慢，再低会借共享显存（慢十倍但不报错） |
| 并发 | 服务端全局单锁 + 队列串行推理；客户端另加文件锁 |
| 文本长度 | 软上限 2000 字（只警告）；硬上限 10000 字（服务端 413） |
| 情感文本 | `emo_text` ≤ 500 字 |
| 音频上传 | ≤ 32 MB，wav/mp3/flac/ogg/opus；服务端有 ffmpeg（`/status` 的 `ffmpeg` 为 true）时另收 m4a/aac/wma（自动转 WAV）。**魔数嗅探**（改扩展名的假音频会被 400） |
| 视频提取音轨 | 前端参考音频入口可直接选 mp4/mov/m4v/mkv/webm/avi/flv（≤ 200 MB），走 `POST /audio/extract` 抽成 22.05kHz 单声道 WAV（前 10 分钟）再当参考音频；需要 ffmpeg。命令行没有对应子命令，需要时先自己 `ffmpeg -i in.mp4 -vn -ac 1 -ar 22050 out.wav` |
| 段间停顿 | `interval_silence` 10~500 ms，默认 200；服务端静默夹取，客户端会提前告警 |
| 变速 | `POST /audio/tempo`，倍率 0.5~2.0，audiotsm WSOLA 保音高，**不落盘**；客户端对应 `tempo` 子命令 |
| 输出清理 | `outputs/` 根目录 wav 超 14 天自动删；`agent_tts/` 子目录**不在清理范围**，会累积 |
| 参考音频缓存 | `prompts/spk_<md5>.wav` 内容寻址，LRU 保留 30 个 |
| 安全 | 无鉴权、可删文件，`--host` 保持 `127.0.0.1` |

## 需要细节时读这些

| 文件 | 内容 |
|---|---|
| `references/api.md` | 全部 HTTP 端点、SSE 事件字段、状态码语义、预设目录结构 |
| `references/parameters.md` | `/tts` 全部表单字段、四种情感模式、采样参数与调参建议 |
| `references/pitfalls.md` | 实测踩坑清单与排查路径（含沙箱/代理/中文路径） |
| `references/cli.md` | 26 个命令全表、脚本边界、health 失败分类、定位顺序 |
| `references/integration.md` | 上游对接：batch 清单/manifest、断点续跑、并发、示例 |
| `references/testing.md` | 改脚本后的离线回归与真机验收 |

## 结果不对怎么查（音色 / 读音 / 时长）

合成**成功了**但"不像 / 念错 / 时长不对"，三类都不是 bug，**先查输入**：

| 现象 | 先做什么 |
|---|---|
| 音色不像 / 带杂音回声 | 换参考音频：单人、无 BGM、无混响、5~15 秒、情绪平稳 |
| 多音字 / 数字 / 缩写读错 | `segments` 看 `numbers` → `glossary --add --zh 同音汉字` → 复核 |
| 偏短 / 结尾被切 | 看 `mel_capped`；切短文本或调大 `--max-mel-tokens` |
| 重复 / 多出句子 | `--seg-tokens` 调到 80~100 |
| 语速不对但音色正常 | `tempo --rate` 变速，**不要重新合成** |

词表是**服务端全局**的，改了会影响前端、别的 Skill 和别的项目——加词前想一下会不会误伤，
改完告诉用户这是全局改动。时长参考：中文约 3~6 字/秒（含标点停顿）。
逐条诊断、参考音频问题表、术语表边界规则见 `references/pitfalls.md` §10。

## 陷阱（高频，先看这几条）

1. **SSE 不能用 EventSource**：`/tts` 是 POST + multipart，EventSource 发不了 body。必须「POST 后逐行读流」。
2. **首次请求超时要给足**：模型加载 30~60s + 推理，客户端超时别低于 300s。
3. **不要起第二个实例**：先 `health` 探活。`ensure` 只认 `INDEXTTS_URL`，**端口写错它会在那个端口再起一个实例**（两个实例各自加载模型，8G 卡必炸）；占着端口却返回 502 的往往是别的程序或代理，不是本项目服务。对 `/api/v1/tts` 这类只收 POST 的接口发 GET 会回 **405**，也只代表端口有人应答，不代表服务没起来。
4. **URL 中文编码**：输出文件名默认取文本前 15 字（中文），手工下载必须 percent-encode；但预设详情接口返回的 URL **已是编码态**，不能再编一次（`%`→`%25` 会 404）。脚本已分别处理。
5. **`/stop` 的代价**：默认会卸载并重建模型，下次生成重新付 30~60s。能等就跑完；确实要停就带 `keep_model=1`。
6. **`mel_capped=true` 是静默截断**：不会报错，只是音频变短。
7. **默认输出目录不被自动清理**：`agent_tts/` 会持续累积，长期使用需自行清理。
8. **进程生命周期（沙箱环境）**：`ensure` 在同一条命令内自启自用没问题；但命令结束后沙箱会回收命令内 spawn 的 detached 进程。跨命令复用服务时，请用后台任务方式启动，而不是靠 `ensure`。
9. **`examples` 给的是服务端 URL，不是本地文件**：`/examples` 返回的 `prompt_audio` 形如 `/examples/voice_01.wav`，纯远程部署时本地根本不存在。现在 `--voice voice_01.wav` 会自动从服务端下载到 `<输出目录>/.voices/` 再上传，所以照抄名字也能用——但别把它当成本地路径去做 `ls`。
10. **情感参数会和 `emo_mode` 打架**：`--emo-vector` 只在 `emo_mode=2` 生效、`--emo-text` 只在 3、`--emo-audio` 只在 1。不匹配时服务端**静默丢弃**，客户端会打 `[warn] ... ignored`，看到就补上对应 `--emo-mode`。
11. **`emo_mode=0` 下 `--emo-weight` 无效**：服务端会把 `emo_alpha` 强制为 1.0。
12. **`doctor` 的冒烟结果缓存 30 分钟**：换了模型、改过预设、或怀疑显存不足时用 `--force` 重测，否则会复用旧结论。
13. **`batch` 退出码 1 不等于整批没跑**：它是不中断的，先看 `failed_ids`，只重跑那几条（配 `--resume`）。
    中断（Ctrl-C / 超时被杀）也是退出码 1，区别看 manifest 的 `"interrupted": true`。
14. **服务端一个预设都没有是正常状态**：刚解压的整合包就是这样。此时传 `--preset` 会以退出码 2 提示"没有预设"，改用 `--voice` 或先在前端建预设。
15. **显存不足有两种表现，只有一种报错**：真 OOM 会给 `generation failed: CUDA out of memory`（脚本已附带排查步骤）；更常见的是**不报错、单纯变慢十倍**（Windows 静默借共享显存）。后者只能靠 `status` 看余量发现，别把"变慢"当成机器老化。
16. **`batch` 的 id 必须唯一**：同一个 id 会写同一个 `.wav`，后一条静默覆盖前一条，两条都报 `ok` 但内容相同。脚本现在会**在合成前拦下**（退出码 1，`reason: duplicate_ids`）。注意 `第1句/开场` 和 `第1句:开场` 也会归一成同一个文件名。
17. **中断后立刻重跑会排队**：关掉对话/请求超时后，服务端那个 job 仍在推理，GPU 没释放。下次合成会卡在 `[queue] N job(s) ahead`。先 `status` 看 `generating`，必要时 `stop <job_id>`。
18. **磁盘满时合成已经跑完了**：GPU 白跑一趟，卡在写盘上。`doctor` 的 `disk_headroom` 低于 1 G 会告警；`download` 遇到 ENOSPC 会直说"磁盘已满"。换 `--outdir` 到别的盘最快。
19. **`--resume` 判的是内容，不是"文件在不在"**：这三类会自动重跑——文本/音色/emo/rate 有改动、
    wav 读不出时长（上次写盘被中断）、旧版本留下的文件没有指纹记录。要老行为（文件存在就跳过）用
    `--resume-any`。返工"只改第 12 句"用 `--only line_012`，别整批重跑。
20. **manifest 默认落盘**（`<outdir>/<日期>/manifest.json`，每完成一条刷新一次）：
    批量跑到 80/100 被 Ctrl-C，前 79 条的时长和路径都在，配 `--resume` 接着跑即可。
    `--no-manifest` 关掉，`--manifest-out` 换路径。
21. **`model_loaded` 不会自己变 true**：模型懒加载，`ensure`/`health` 之后一直是 `false`，
    直到有 `/tts` 或 `/model/load` 请求。看到 `false` 的正确反应是“下一步直接生成”，
    不是“再等 30 秒”；循环查状态会永远等下去。
22. **数字读错不会让合成失败**：`5090显卡` 读成“五千零九十显卡”时音频照样生成、退出码 0。
    生成前用 `segments` 看 `numbers`，或看 `synth` 输出里的 `preview.numbers`；
    读法不对用 `glossary --add "5090" --zh "五零九零"` 纠正（术语紧贴中文也能命中）。

## 被其他 Skill 调用（视频配音 / 有声书 / 批处理）

这一节是给"上游 Skill 调本 Skill"用的契约。两种调用方要的东西不一样：

| | 人跟 agent 对话 | 上游 Skill 程序化调用 |
|---|---|---|
| 音色没定 | 停下来问人 | 返回候选，由上游决定 |
| 出错 | 讲清楚 + 给三条路 | 只看退出码 + JSON |
| 一批台词 | 一句一句来 | 一次 `batch` 跑完 |
| 关心什么 | 能不能用 | 时长、路径、失败清单 |

### 三条约定

1. **置 `INDEXTTS_HEADLESS=1`**（或命令行加 `--headless`）。凡是"需要问人"的地方，stdout 变成 JSON：
   - 缺音色 → `{"ok":false,"need":"voice","presets":[...],"examples":[...]}`，退出码 `2`
   - 找不到项目 → `{"ok":false,"need":"project_dir","options":[{"id","how"}...]}`，退出码 `3`
   - `batch` 的 id 撞车 → `{"ok":false,"reason":"duplicate_ids","clashes":[...]}`，退出码 `1`

   人类提示仍然走 stderr，不影响 `json.loads(stdout)`。**上游不要靠解析文案判断状态。**
   拿到 `duplicate_ids` 要回头改自己生成的 id，**不要**自动改名重试——manifest 的 `id`
   是上游回指的唯一键，改了就对不上。

2. **先 `capabilities` 再动手**。一次拿到音色清单、情感模式、文本/音频上限、并发语义、建议超时，
   不用读文档猜参数。服务不可达时它返回 `ok:false` 但结构完整，可以据此直接报"服务没起"。

3. **批量用 `batch`，不要循环调 `synth`**。每调一次 `synth` 都要重新探活 + 抢一次文件锁，
   几十条叠起来固定开销比推理本身还贵。

另外：`synth` 默认会先调一次 `/segments`，把 `preview`（分段文本 + 数字读音）放进 stdout JSON，
供上游在合成前做文本校对；不需要时加 `--no-preview` 省掉这次请求。`batch` 不做逐条预览。

关于模型加载：headless 编排里如果必须先确认“模型已加载”，用 `model-load`，**不要**轮询
`health.model_loaded`。`health` 返回 `model_loaded:false` 只代表“还没人请求过”，
不代表加载失败或正在进行。

### batch 的输入输出、返工与并发

清单格式（`defaults` + `items`）、manifest 字段（含对齐时间轴要用的 `duration`）、
`--resume` / `--only` 的指纹规则、并发纪律和一段最小对接示例，见 `references/integration.md`。

三条底线：

- 一次 `batch` 跑完，**不循环 `synth`、不并行多个 `batch`**；
- `id` 必须唯一，撞车时改自己的 id，**不要自动改名重试**；
- 退出码 `0`=全成，`1`=有失败或被中断，看 `failed_ids` / `interrupted`。

