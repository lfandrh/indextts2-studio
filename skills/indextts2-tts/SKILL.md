---
name: indextts2-tts
version: 1.2.4
description: 调用 IndexTTS2 HTTP 服务做零样本语音合成（音色克隆）时使用。覆盖：项目目录定位（三条路：用户给路径 / 启动服务后反查端口 / 没有整合包则引导去 B 站获取）、端到端能力体检（doctor：探活+接口+冒烟合成+变速+显存与磁盘余量，确认"现在就能出声"）、能力清单（capabilities，供上游 Skill 发现音色与限制）、单条与批量合成（batch：几十上百句一次跑完、id 撞车提前拦截、单条失败不中断、按内容指纹断点续跑、只重跑改动句、manifest 逐条落盘、统一变速、回传每句时长）、预设系统（参考音频+参数一键复用，生成前强制向用户确认音色）、四种情感控制模式（同参考音频/情感音频/8维向量/情感文本）、内置示例音色（本地缺文件自动下载）、长文本分段、SSE 事件流解析、保音高变速（tempo，不重新合成）、术语词汇表（多音字/数字/缩写读音纠错）、防并行合成锁、生成音频找回、只读自检、无头模式（INDEXTTS_HEADLESS，给视频配音等上游 Skill 调用）。含故障排查：显存不足与 OOM（8G 卡两种表现）、音色不像/带杂音的输入检查、中断后重跑排队、磁盘满。支持本机/远程/Docker/便携整合包部署，配置全走环境变量。**交互约定：本会话第一句话必须先讲清这个 skill 能干什么、流程有哪四步，再讲你接下来要做什么；之后每一步动手前先说一句正在做什么、大概多久；定位项目（locate）只跑一次，失败就把三条路摆给用户选，不要重试或扫盘。** 触发词：IndexTTS、indextts2、语音合成、TTS、音色克隆、参考音频、预设合成、情感向量、语速调整、变速、批量配音、视频配音、有声书、整合包、项目在哪、找不到项目、能不能用、体检、显存不足、OOM、变慢、声音不像、有杂音、读音错、多音字。
agent_created: true
---

# IndexTTS2 语音合成客户端

**一句话**：合成入口是 `POST {INDEXTTS_URL}/api/v1/tts`（multipart/form-data，必传参考音频 `spk_audio`），返回 SSE 事件流，`done` 事件里带 wav 下载 URL。

**版本**：`python "$CLIENT" --version`（写作时 1.2.4）。排障第一步先要版本 ——
skill 有两份副本（用户级 `~/.workbuddy/skills/` 与项目内 `skills/`），没版本号
既判断不了用户装的是哪一版，也看不出两份是否漂移。

**优先用本 skill 自带的 `scripts/tts_client.py`**（纯标准库，零依赖，Python 3.10+）。它已封装：服务探活与启动（失败打日志尾部）、预设确认守卫、防并行文件锁、SSE 解析、中文路径下载、示例音色自动下载、保音高变速、音频找回、统一错误信息。不要手搓 HTTP 请求，除非你要调用的能力脚本没覆盖（清单见下文「这个脚本不做什么」）。

## 五条硬规则（第 2~4 条由脚本强制，第 0~1 条是交互契约）

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

## 配置：全部走环境变量，无硬编码路径

脚本不 import 项目代码，只走 HTTP，所以对服务部署位置无假设——本机、远程服务器、Docker、反向代理后面都能用。

| 变量 | 默认 | 说明 |
|---|---|---|
| `INDEXTTS_URL` | `http://127.0.0.1:7860` | 服务地址。连远程/换端口只需改这一个 |
| `INDEXTTS_PROJECT_DIR` | 自动探测 | 项目根（含 `server.py` 与 `indextts/`）。**仅 `ensure` 需要**；定位顺序见 Step 0（环境变量 → 脚本位置 → cwd → 记忆文件 → 端口反查）。找不到时跑 `locate` 拿三条路 |
| `INDEXTTS_OUT_DIR` | `<项目根>/outputs/agent_tts` | agent 默认输出目录；探测不到项目根时退到 `~/.indextts2-tts/agent_tts` |
| `INDEXTTS_START_TIMEOUT` | `120` | `ensure` 拉起服务后的探活上限（秒） |
| `INDEXTTS_HTTP_TIMEOUT` | `900` | 单次请求 socket 超时（秒）。首次合成要等模型加载，别调小 |
| `INDEXTTS_PROXY` | 空（**不走代理**） | 默认绕过系统代理，见下方说明 |

**关于代理**：脚本默认禁用 `HTTP_PROXY`/`HTTPS_PROXY`。企业网、CI、agent 沙箱常设全局代理，会把发往 `127.0.0.1` 的请求也交给代理，拿到 `502 upstream connect failed`，看起来像服务挂了。确实需要走代理时设 `INDEXTTS_PROXY=http://host:port`。

**关于启动方式**：`ensure` 不调用项目自带的 `start_server.bat` / `.sh`（那些脚本带本机路径假设）。它按 `<项目根>/runtime/` 解释器（便携分发包）→ `uv run --extra server --no-sync python server.py` → `<项目根>/.venv` 解释器 → 当前解释器的顺序探测，端口取自 `INDEXTTS_URL`。**`runtime/` 排第一是有意的**：分发包只有 `runtime/`，没有 `.venv` 也没装 uv，让 uv 抢在前面会往用户的包里新建空 `.venv` 再报缺依赖。

## 开场：第一句话先讲这个 Skill 是干什么的

用户调用这个 skill 时，他只知道"我点了个东西"，不知道它能干什么、要给什么、要等多久。
**本会话第一次响应，先把这些讲清楚**（硬规则 0），再进入 Step 0。

### A. 用户没说具体任务（只说了"用 IndexTTS2"之类）

> 这个 Skill 帮你调用 **B 站 UP 主「十一君Jun」制作的 IndexTTS2 超级优化版整合包**，
> 在你自己的电脑上完成**视频配音、文字转语音**——用一段参考音频克隆音色，不需要训练模型。
>
> 整个流程四步：
>
> 1. **确认整合包**——我先找到它的位置（没装的话，我会告诉你去哪拿）
> 2. **跑起来**——启动项目，测一遍接口是否正常，确认现在就能出声
> 3. **给素材**——你选一段 3~10 秒的参考音频（好听的声音，或你自己的），再给我要念的文稿
> 4. **出音**——生成完成后，我直接把克隆了那个音色的音频放给你听
>
> 现在从第 1 步开始，我先找一下你的整合包在哪，几秒。

### B. 用户已经说清任务（"把这段文稿配成音频"）

> 这个 Skill 做的是文字转语音 + 音色克隆，你这个需求能直接做。开工前先花一到两分钟做次体检
> （起服务 + 真合成一句验证能出声），过了就按你的内容出音。现在先找整合包。

### 三条边界

- **只讲能干的，不主动列短板。** 用户是来用工具的，不是来听免责声明的。能力边界写在
  「这个脚本**不**做什么」那一节，**等他问到或真的撞上了再说**，别在开场泼冷水。
- **排障类提问不念介绍。** 用户问"声音不像怎么办""8G 卡 OOM""报错 xxx"——他已经知道自己要什么，
  直接回一句"这个能帮上，我先看一下"然后进排查。念开场白只会显得没听人说话。
- **同会话只念一次，长度不超过 10 行。** 第二次之后的响应直接干活，不要再自我介绍。

> 上面两版是**锚点不是台词**，可以按上下文调措辞，但四个要素不能少：
> **是谁的包 → 能干哪两类活 → 要用户给什么 → 接下来我做什么**。
> 也别把 B 站链接塞进开场——等到第 1 步真的找不到整合包时再给，那时候才有用。

## Step 0 — 先定位项目（做任何事之前）

**这一步不能跳。** skill 只是调用客户端，不含模型和代码，说清"项目在哪"才谈得上合成。

**动手前先开口**（这是硬规则 1，最容易漏的一步）：

> 我先找一下你的 IndexTTS2 整合包在哪，大概几秒。

如果这是本会话第一次响应，**先说上一节的开场白，再说这句**——不能只有这句，否则用户
只知道你在"找什么东西"，不知道找的是干什么用的东西。

不要一上来就静默调用工具。用户看不见你的命令，只看见界面在转圈。

然后跑：

```bash
SKILL_DIR="$HOME/.workbuddy/skills/indextts2-tts"     # 用户级安装；随仓库分发的用 <项目根>/skills/indextts2-tts
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

### 自动定位的顺序

脚本按这个顺序找，**越靠前越可信**，命中即停：

1. `INDEXTTS_PROJECT_DIR` 环境变量
2. 从脚本位置向上（skill 随包放进项目内时，向上第 3 层就是包根）
3. 从 cwd 向上（用户把包目录当工作区打开）
4. `~/.indextts2-tts/project.json`（`locate --set` 记住的路径；目录被移动/删除会自动失效）
5. **从本地监听端口反查**：`netstat -ano` 取监听 PID → ctypes 读进程 exe → 沿目录向上找 `server.py`
   （便携包是 `runtime\python.exe`，venv 是 `.venv\Scripts\python.exe`，逐级校验都撞得到）
6. 全失败 → 退出码 3，回到上面三条路

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

## 便携整合包用户（IndexTTS2.0_v3 这类分发包）

结构上和源码仓库**不一样**：**没有 `.venv`，依赖全在 `runtime\` 里，机器上也没装 `uv`**。
脚本已把 `runtime\python.exe` 排在启动顺序第一位，定位走上面 Step 0 的三条路。

**最省事**：让用户双击包根目录的 `start_server.bat`，等 `http://127.0.0.1:7860` 能打开之后，
`locate` 会自动从端口反推出包根——**连 `INDEXTTS_PROJECT_DIR` 都不用设**。

也可以直接把路径钉死（最适合无人值守）：

```bash
# Windows cmd
set INDEXTTS_PROJECT_DIR=D:\IndexTTS2.0_v3
python "%USERPROFILE%\.workbuddy\skills\indextts2-tts\scripts\tts_client.py" ensure
```

```bash
# bash / Git Bash
export INDEXTTS_PROJECT_DIR=/d/IndexTTS2.0_v3
python "$HOME/.workbuddy/skills/indextts2-tts/scripts/tts_client.py" ensure
```

判断是否找对了看 `selftest`：`project_dir` 那项 `ok: true` 才算对（没设时它是 false，
但其余检查照常通过，合成也不受影响）。

两条纪律：

1. **别用 `uv run` / `uv sync` 启动或"修"分发包**。依赖是冻结好的，uv 会在包根新建一个空 `.venv` 把路径搞乱。
2. **别用 pip 往 `runtime\` 里装或升级任何包**。依赖错乱基本是解压不完整或被安全软件删过，
   正确做法是重新解压一份干净包，不是继续 pip 修。

## 标准流程

脚本只用标准库，Python 3.10+ 即可，不需要装任何依赖。**用绝对路径调用，别依赖 cwd。**

**开头就给用户一个整体预期**，别让他不知道自己在用什么、要等多久。
本会话第一次响应按「开场」一节的 A/B 版说完，然后按 Step 0 → 6 走。
**每一步动手前先说一句，每步结束说一句结果**（硬规则 1）。

```bash
SKILL_DIR="$HOME/.workbuddy/skills/indextts2-tts"     # 用户级安装；随仓库分发的用 <项目根>/skills/indextts2-tts
CLIENT="$SKILL_DIR/scripts/tts_client.py"
python "$CLIENT" presets
```

Windows cmd：

```bat
set SKILL_DIR=%USERPROFILE%\.workbuddy\skills\indextts2-tts
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

现象有两种，别当成两个问题：**报错**是 `CUDA out of memory`；**不报错**是生成突然慢十倍
（Windows 静默借了共享显存）。后者更容易被误判成卡死。

```bash
python "$CLIENT" model              # 先看：模型有没有加载、现在是否正在生成
python "$CLIENT" unload             # 释放显存。生成中会被拒（409），这是有意的
python "$CLIENT" unload --force     # 确认要打断当前生成再卸
```

顺序建议：先 `model` 看 `generating`，别在别人正在跑的时候手一抖 `--force`。
卸载后下一次合成要重新加载模型（首次 30~60s），这是省显存换来的代价，不是故障。

真正的长期解法是调配置，不用反复卸载：

```bash
python "$CLIENT" model-config --diffusion-steps 20        # 下次生成生效，质量略降
python "$CLIENT" model-config --w2v-fp16 off              # 只写配置 → 还得下面这一步
python "$CLIENT" model-restart --w2v-fp16 on --diffusion-steps 20   # 一步改完并重建
```

**最容易踩的坑**：`model-config` 改精度开关（`--fp16/--w2v-fp16/...`）不会立刻生效，
它只是写进配置——已加载的模型还是旧精度。要生效必须 `model-restart`（30~60s）。
`--diffusion-steps` / `--cfg-rate` 是例外，下一次生成直接生效。脚本会在 stderr 把这两种情况分开说明。

## 子命令速查

| 命令 | 用途 |
|---|---|
| `locate [--set PATH \| --forget]` | 定位项目根；退出码 3 = 需要用户从三条路里选一条。`--set` 校验后记住路径 |
| `ensure` | 探活；没在跑则启动并等待就绪（失败会打日志尾部） |
| `health` | `GET /health`。连得上输出 `{ok, origin, health}`；**连不上额外带 `reason` + `detail` + `hints`**（见下方「health 说不上话时怎么看」） |
| `status [--json]` | 模型状态 + 显存/内存/CPU 仪表（默认人话，`--json` 给原始响应） |
| `model [--json]` | 模型自己的配置（精度开关 / 扩散步数 / CFG）+ **是否正在生成**。不占 GPU |
| `model-config --diffusion-steps 20 --cfg-rate 0.6` | 改运行配置，不重建模型。**采样参数下次生成生效；精度开关只写配置，要重启才生效** |
| `model-restart [--diffusion-steps N] [--w2v-fp16 on\|off]` | 按当前配置重建模型（可顺带改配置），30~60s；生成中服务端 409，无 force 通道 |
| `unload [--force]` | 卸载模型释放显存。生成中服务端会拒绝（409）；`--force` 才打断当前生成 |
| `presets` / `preset <名>` | 预设列表 / 单个预设详情 |
| `preset-save --name X --audio Y [--overwrite]` | 把一段参考音频存成命名音色（可带 `--emo-mode/--emo-text/--emo-vector` 与采样参数）；同名默认拒绝，`--overwrite` 才覆盖 |
| `examples` | 内置示例音色清单（名字可直接喂给 `--voice`） |
| `choose` | tty 下交互选预设；非 tty 打印候选并以退出码 2 退出 |
| `segments --text "..." [--out tasks.json]` | 长文本分段预览（不合成）；`--out` 可直接产出 batch 能吃的清单（配 `--preset/--voice/--rate` 写进 defaults） |
| `synth ...` | 合成（见上） |
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
| `--version`（全局） | 打印 skill 版本，如 `indextts2-tts 1.2.4` |

### 这个脚本**不**做什么

- 预设的删除 / 改名 / 复制（`DELETE /presets/{name}`、`/rename`、`/duplicate`）：AI 判断不了
  哪个音色该留、该叫什么名字，这类整理归人。
- 历史清理（`DELETE /history*`、`POST /history/config`）：不可回溯，不交给 agent。
- `POST /model/load`：没封装，因为不需要——`ensure` 拉服务、`synth`/`batch` 首次请求都会自动加载。
- 长篇台本工作台（`/lf/*`，81 个接口）和多轨时间轴（`/lf/tl/*`，31 个接口）：那是 WebUI 的
  编辑工程层（素材处理、分轨混音、工程存档），和"文字出语音"不是一层。
- `GET /jobs/*`：都强制要 `client_id`，那是浏览器标签页生成的，脚本拿不到，调了也是 400。

需要这些能力时直接打 HTTP（字段见 `references/api.md`），不要 fiddle 脚本。
不确定整合包升级后有没有新接口、或路径有没有变，跑 `api` 对账——这是唯一的漂移探针。

### health 说不上话时怎么看

连不上时 `health` 会带 `reason`，**四种原因的处理办法完全不同，别一句"服务挂了"糊过去**：

| `reason` | 含义 | 该做什么 |
|---|---|---|
| `connection_refused` | 端口没人监听 | 服务没启动 → 让用户双击 `start_server.bat`，或跑 `ensure`；也可能是 `INDEXTTS_URL` 端口写错 |
| `timeout` | 有人监听但 5 秒没应答 | 服务正在加载模型（首次 30~60s），等一下；一直这样就看 GPU/内存是否被占满 |
| `http_error` | 端口有 HTTP 服务，但它不是 IndexTTS2 | 端口被别的程序占了（`detail` 里能看到对方返回什么），换端口或停掉占用进程 |
| `dns_error` | 主机名解析不了 | 改用 IP，或检查网络/代理 |

`hints` 里已经写好了对应的下一步，**原样转给用户即可**，不要自己另编排查步骤。
`detail` 超过 160 字会被截断并带 `…`——那是别的 Web 服务返回的 HTML，不是本项目的信息。

退出码：`0`=成功，`1`=失败，`2`=**需要先确认预设/音色**（见到 2 必须停下来问），`3`=**需要用户提供项目位置**（见到 3 必须把三条路摆给用户，见 Step 0）。
headless 模式下 2 和 3 会**额外在 stdout 给结构化 JSON**（`need` + 候选），供上游程序化决策——见「被其他 Skill 调用」。

## 改脚本之后怎么自测

`tests/test_client.py` 用标准库起一个假服务（`/health`、`/tts` 的 SSE、`/audio/tempo` 全有），
**不需要 GPU、不需要真服务，10 秒跑完**：

```bash
python tests/test_client.py          # 38 项：续跑指纹、id 冲突、manifest、--only、旧代码页编码、
                                     #      存音色同名保护、unload 409、模型配置键校验、openapi 对账、
                                     #      health 失败原因分类、doctor summary 的两个时间…
```

覆盖的都是"改一行就可能悄悄坏掉"的契约：退出码、stdout JSON 字段、`--resume` 语义、
manifest 落盘、中文在 cp936/cp1252 管道下的输出。改完脚本先跑它，再用真实整合包跑
一次 `doctor --force` 做验收——前者保证逻辑没崩，后者保证真机能出声。

## 关键事实

| 事实 | 值 |
|---|---|
| 模型加载 | **懒加载**：首个 `/tts` 请求才载入 GPU，约 30~60s |
| 模型驻留 | 加载后**一直驻留**，不会空闲自动卸载。要释放显存需显式 `POST /model/unload`（前端「模型管理」页） |
| 显存占用 | 8G 卡加载后约 6.7 G，余 1.3 G 属正常；余量低于 1.2 G 时长文本明显变慢，再低会借共享显存（慢十倍但不报错） |
| 并发 | 服务端全局单锁 + 队列串行推理；客户端另加文件锁 |
| 文本长度 | 软上限 2000 字（只警告）；硬上限 10000 字（服务端 413） |
| 情感文本 | `emo_text` ≤ 500 字 |
| 音频上传 | ≤ 20 MB，wav/mp3/flac/ogg/m4a，**魔数嗅探**（改扩展名的假音频会被 400） |
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
| `tests/test_client.py` | 离线回归测试（假服务、无 GPU），改脚本后先跑这个 |

## 结果不对怎么查（音色 / 读音 / 时长）

合成**成功了**，但用户说"不像"、"念错了"、"不对"。这三类占绝大多数，而且都不是 bug。

### 一、音色不像 / 有杂音 / 带回声

**先看参考音频，九成问题在这。** 这个模型是零样本克隆：参考音频里有什么，输出就带什么。

| 参考音频的问题 | 输出会怎样 | 怎么办 |
|---|---|---|
| 背景有音乐、噪声、他人说话 | 每句话都掺进那点噪声 | 换一段**干净的**，或用前端分离人声 |
| 有房间回声、混响 | 输出发闷、像在浴室里说话 | 换近场录音（手机贴脸录也行） |
| 太短（< 3 秒） | 音色飘、前后不一致 | 5~15 秒、连续说完整句子 |
| 太长（> 60 秒）或情绪起伏大 | 音色被后半段带偏 | 截取最典型的一段 |
| 是变声器/AI 处理过的 | 电子味，越学越假 | 用原始人声 |
| 采样率低、音量过小 | 底噪被放大 | 换 16 kHz 以上、音量正常的 |

正确做法：**单人、无 BGM、无混响、5~15 秒、情绪平稳**。达不到就先把音频处理好再合成，
换参数（`emo_weight`、`temperature`）解决不了输入的问题——只会让效果更不稳定。

### 二、读音不对（多音字 / 数字 / 英文缩写）

"重庆"读成 chóng qìng、"2026"读成"二零二六"、iPhone 读成 i-p-h-o-n-e ——
这是文本规范化的局限，**不是模型坏**。用术语表把它改写成"读出来就是对的"写法：

```bash
python "$CLIENT" glossary --add "重庆" --zh "虫庆"
python "$CLIENT" glossary --add "2026" --zh "二零二六年"
python "$CLIENT" glossary                  # 看当前词表
python "$CLIENT" glossary --delete "重庆"  # 删掉
```

- `--zh` 填**同音汉字**，不是拼音 —— 词表是在送进模型前做文本替换。
- **词表是服务端的、全局的**：`POST /api/v1/glossary` 写进
  `<模型目录>/glossary.yaml`，由服务端 normalizer 在每次规范化时统一替换。
  也就是说改了以后**前端页面、别的 Skill、别的项目一起变**，不是"只影响我自己"。
- 因此：加词前想一下会不会影响别人；改完让用户知道这是全局改动。换机器/换包的词表各是各的。
- 别写太短的词（`"C"`、`"了"`），会产生连锁误伤，详见 `references/pitfalls.md`。

### 三、时长不对 / 内容缺或多

| 现象 | 先查什么 |
|---|---|
| 明显比预期短，结尾被切断 | `mel_capped=true`（静默截断）。切短文本，或调大 `--max-mel-tokens` |
| 读出多余的句子、重复 | 文本本身有重复；或分段 token 太大（`--seg-tokens` 调小到 80~100） |
| 整段变成静音 | 参考音频几乎无声或损坏；换音色再试一次 |
| 语速太快/太慢但音色正常 | **别重新合成**，用 `tempo --rate` 保音高变速（Step 4b） |

判断"时长是否合理"：中文约 **3~6 字/秒**（含标点停顿）。100 字的稿子出来 8 秒或 60 秒都不正常。

## 陷阱（高频，先看这几条）

1. **SSE 不能用 EventSource**：`/tts` 是 POST + multipart，EventSource 发不了 body。必须「POST 后逐行读流」。
2. **首次请求超时要给足**：模型加载 30~60s + 推理，客户端超时别低于 300s。
3. **不要起第二个实例**：先 `health` 探活。`ensure` 只认 `INDEXTTS_URL`，**端口写错它会在那个端口再起一个实例**（两个实例各自加载模型，8G 卡必炸）；占着端口却返回 502 的往往是别的程序或代理，不是本项目服务。
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

### batch 的输入输出

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
{"ok":false,"skill_version":"1.2.4","source":"tasks.json","manifest":".../manifest.json",
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

### 返工与断点续跑

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

### 并发

服务端全局单锁串行推理，**不要并行跑多个 `batch` / `synth`**。上游有多轨需求就并成一个列表交给
`batch`——它在一个进程里串行跑完，锁只抢一次。

### 上游对接示例

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

