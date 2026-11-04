"""字幕生成：段边界 -> 字幕条目 -> SRT/VTT，以及字幕缓存读写。

这里只做纯计算，标准库而已（difflib / json / os），不碰 torch、不碰 onnxruntime，
这样格式化规则可以被单测钉住，也不用为了测一行时间戳去加载几 GB 的模型。
HTTP 与落盘编排在 server.py，算法都在这里。

两个口径必须记住：
1. 段与段的分界是【物理精确】的。成品音频 = 各段波形 + 段间静音拼起来，
   所以每段的起止秒就是样本数累加，不是估算（见 cumulative_slices）。
2. 字幕正文用【用户原文】，不用模型词元拼回来的串。词元流是规范化之后的
   英文大写 + 半角标点（"English" -> "ENGLISH"、"，" -> ","），直接拿来当字幕
   会难看得没法用；本模块用模糊对齐把规范化串的切点映射回原文字符位置。
"""
import json
import os
import re
from difflib import SequenceMatcher

WORD_SEP = "▁"  # sentencepiece 的词首标记
SIDECAR_VERSION = 1

# 原文锚定的下限：规范化改动太大（大量数字转汉字、术语替换）时，
# 对齐出来的切点不可信，退回词元重建的文本，至少字幕有内容。
MIN_MATCH_RATIO = 0.5
# difflib 在两个长串上的耗时与长度乘积相关。硬上限之外不做对齐，
# 直接用词元重建文本 —— 生成一次要跑几分钟，不能为字幕卡几十秒。
MAX_ALIGN_CHARS = 12000


def _is_ascii_word(ch: str) -> bool:
    """只在 ASCII 字母数字之间补空格：中文词与词之间本来就没有空格。"""
    return ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9")


def tokens_to_readable(tokens):
    """把 BPE 词元拼回可读文本。

    返回 ``(文本, 结束下标列表)``。``ends`` 与 ``tokens`` 一一对应，
    ``ends[i]`` 是消费到第 i 个词元（含）时在文本里的字符长度，用于把
    「段 = 一段连续词元」换成字符区间。不产字符的词元（独立的 ``▁``）也要
    占一个位置，否则段边界会整体错位。

    刻意不用 ``tokenizer.decode()``：它会走 common.de_tokenized_by_CJK_char，
    那函数还原英文的条件是 ``if len(all_matches) > 1``，单个英文词永远还原不回来
    （server.py 的分段预览换掉 decode 时实测过：``这里有English这个单词。``
    会变成 ``这里有<sent_0>这个单词.``）。

    也刻意不照旧的预览实现那样把 ``▁`` 一律删掉：英文词元是 ``▁I``、``▁LOVE``
    这种带词首标记的形式，删掉标记就把单词粘成了 ``ILOVEYOU``。这里的规则是
    「词首标记只在两侧都是 ASCII 字母数字时落地成一个空格」，中文照旧无缝拼接。
    """
    parts = []
    ends = []
    length = 0  # 运行长度；用 sum(len(p) for p in parts) 会是 O(n^2)
    last = ""  # 已产出文本的最后一个字符
    pending = False  # 见到词首标记，还没决定要不要变成空格
    for tok in tokens:
        if tok.startswith(WORD_SEP):
            pending = True
            body = tok[len(WORD_SEP):]
        else:
            body = tok
        if body:
            if pending and last and _is_ascii_word(last) and _is_ascii_word(body[0]):
                parts.append(" ")
                length += 1
            parts.append(body)
            length += len(body)
            last = body[-1]
            pending = False
        # 独立的 ▁ 是中文分词的产物，不产字符，但边界要留给下一个词元
        ends.append(length)
    return "".join(parts), ends


def _fold_case(s: str) -> str:
    """只对 ASCII 字母做大写折叠，保证长度不变（``ß.upper()`` 会变长，不能用）。"""
    return "".join(ch.upper() if ch.isascii() else ch for ch in s)


def _anchor_map(original: str, readable: str):
    """建立「规范化串字符位置 -> 原文字符位置」的分段线性映射。

    两条串的差异来源：英文被词表大写、全角标点变半角、数字按读法展开
    （``2012`` -> ``二零一二年``，长度也会变）。所以只能模糊对齐，
    再用匹配块把位置插值回去。对不上就返回 None，由调用方换切法。
    """
    if not original or not readable:
        return None
    if max(len(original), len(readable)) > MAX_ALIGN_CHARS:
        return None
    # seq1 = 原文，seq2 = 规范化串。difflib 的 Match.a 索引 seq1、.b 索引 seq2，
    # 取反就把映射方向搞倒了（实测后果：整段英文被截成 "I love you, but this is a tes"）。
    matcher = SequenceMatcher(None, _fold_case(original), _fold_case(readable), autojunk=False)
    if matcher.ratio() < MIN_MATCH_RATIO:
        return None
    pts = []  # (规范化串位置, 原文位置)
    for block in matcher.get_matching_blocks():
        pts.append((block.b, block.a))
        pts.append((block.b + block.size, block.a + block.size))
    pts.sort(key=lambda p: (p[0], p[1]))
    # 相邻块会给出重复的规范化位置，去重取最靠后的原文位置；
    # 再剔掉破坏原文位置单调性的点，否则插值会倒挂。
    clean = []
    for rd, orig in pts:
        if clean and clean[-1][0] == rd:
            clean[-1] = (rd, max(orig, clean[-1][1]))
        elif clean and orig < clean[-1][1]:
            continue
        else:
            clean.append((rd, orig))
    if len(clean) < 2:
        return None
    pts = clean
    tail_rd, tail_orig = pts[-1]

    def map_pos(x: int) -> int:
        if x <= pts[0][0]:
            return max(0, min(len(original), tail_clamp(pts[0][1] - (pts[0][0] - x))))
        for i in range(1, len(pts)):
            rd1, orig1 = pts[i]
            if x <= rd1:
                rd0, orig0 = pts[i - 1]
                span = rd1 - rd0
                if span <= 0:
                    return orig1
                return int(round(orig0 + (x - rd0) / span * (orig1 - orig0)))
        # 越过最后一个锚点（尾部整段对不上，如数字被展开成汉字）：按剩余长度平移
        return tail_clamp(tail_orig + (x - tail_rd))

    def tail_clamp(v: int) -> int:
        return max(0, min(len(original), int(round(v))))

    return map_pos


# 段与段本来就是在标点处断开的，所以占比切点吸附到「最近的标点之后」通常就回到正确位置
SNAP_PUNCT = "。！？!?；;，、,.:：\n"


def _snap_to_punct(original: str, pos: int, window: int):
    """在 ±window 内找最近的「标点之后」落点；找不到返回 None。"""
    best = None
    best_dist = None
    for i in range(max(1, pos - window), min(len(original) - 1, pos + window) + 1):
        if original[i - 1] in SNAP_PUNCT:
            dist = abs(i - pos)
            if best_dist is None or dist < best_dist:
                best, best_dist = i, dist
    return best


def segment_texts(original: str, seg_token_lists):
    """引擎切出来的每一段，取回它在【用户原文】里的片段。

    返回 ``(每段文本, 切法)``。切法随字幕一起存进缓存，界面才说得出文本从哪来：

    - ``anchor``：整条词元流拼成可读串，与原文做模糊对齐，段边界映射回原文。
      精确路径，字幕就是用户敲进去的字（大小写、全角标点都保住）。
    - ``proportional``：数字被展开成汉字之类改动太大、对不齐时，按可读串上的长度
      占比切原文，再把落点吸附到最近的标点之后。段边界本来就是标点，吸附后误差
      通常在一句以内，但这是【估算】，必须标注。
    - ``readable``：原文是空的，无从锚定，只能交回词元拼出来的文本。

    三种切法都按【平铺】处理：所有段文本首尾相接覆盖整段原文，不丢字也不重字
    （所以单段为空是允许的，那说明原文在这里只有空白）。
    """
    original = original or ""
    tokens = []
    for seg in seg_token_lists:
        tokens.extend(seg)
    readable, ends = tokens_to_readable(tokens)
    cuts = [0]
    for seg in seg_token_lists:
        cuts.append(cuts[-1] + len(seg))
    n = len(seg_token_lists)

    def span(i):
        """第 i 段在可读串上的 [a, b) 字符区间。

        ``ends[k]`` 是「消费到第 k 个词元后的长度」，所以消费 j 个词元之后的位置是
        ``ends[j-1]``，一个都没消费是 0 —— 少减一就整体偏一个词元。
        """
        j0, j1 = cuts[i], cuts[i + 1]
        a = ends[j0 - 1] if j0 > 0 else 0
        b = ends[j1 - 1] if j1 > 0 else 0
        return a, max(b, a)

    def tile(points):
        """原文上的 N+1 个切点 -> 夹紧成单调不减且首尾覆盖整段，再切片。"""
        pts = [max(0, min(len(original), int(round(p)))) for p in points]
        pts[0] = 0
        pts[-1] = len(original)
        for i in range(1, len(pts)):
            pts[i] = max(pts[i], pts[i - 1])
        return [original[pts[i]:pts[i + 1]].strip() for i in range(n)]

    if n == 0:
        return [], "anchor"

    prefixes = [span(i)[0] for i in range(n)] + [len(readable)]
    try:
        map_pos = _anchor_map(original, readable)
    except Exception:
        map_pos = None
    if map_pos is not None:
        return tile([map_pos(p) for p in prefixes]), "anchor"

    if not original:
        # 没有原文可锚（上游给了空文本）：只能拿词元拼回来的文本，
        # 它虽然是大写 + 半角标点，但每段内容是对的，比交一堆空条目强。
        return [readable[a:b].strip() for a, b in (span(i) for i in range(n))], "readable"
    window = max(8, int(len(original) * 0.03))
    pts = [0]
    for p in prefixes[1:-1]:
        raw = len(original) * p / max(1, len(readable))
        pts.append(_snap_to_punct(original, int(round(raw)), window) or raw)
    pts.append(len(original))
    return tile(pts), "proportional"


def cumulative_slices(seg_samples, sampling_rate, interval_ms):
    """每段样本数 + 段间静音 -> 每段在成品音频里的起止秒。

    与 infer_v2 的拼接严格同式：``sil_dur = int(sr * ms / 1000)``，
    段与段之间插入静音，【最后一段之后不补】。200ms@22050 = 4410 采样。
    """
    if sampling_rate <= 0 or not seg_samples:
        return []
    gap = max(0, int(sampling_rate * (interval_ms or 0) / 1000))
    out = []
    cur = 0
    total = len(seg_samples)
    for i, n in enumerate(seg_samples):
        n = max(0, int(n))
        out.append((cur / sampling_rate, (cur + n) / sampling_rate))
        cur += n + (gap if i < total - 1 else 0)
    return out


def cues_from_segments(segments):
    """段级字幕条目：一段一条。

    粒度就是「一次合成一段」，时间是物理边界，不做任何插值估算。
    装得上字级对齐组件时才会进一步在段内按标点分句（M2/M3）。
    """
    cues = []
    for seg in segments or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = float(seg.get("start") or 0.0)
        end = float(seg.get("end") or 0.0)
        if end <= start:
            continue
        cues.append({"start": start, "end": end, "text": text})
    return cues


def _timestamp(seconds: float, decimal=",") -> str:
    """SRT 用逗号、VTT 用点。进位按总毫秒算，避免 59.9999 写成 00:00:59,1000。"""
    total_ms = max(0, int(round(float(seconds) * 1000)))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    h = total_s // 3600
    m = (total_s % 3600) // 60
    s = total_s % 60
    return f"{h:02d}:{m:02d}:{s:02d}{decimal}{ms:03d}"


def _one_line(text) -> str:
    """cue 正文压成一行：剪映 / Premiere 会把换行当成字幕里的真实换行。"""
    return re.sub(r"\s*\n\s*", " ", text).strip()


# WebVTT 的 cue 正文按「cue text」解析，不是纯文本：裸 `<` 会被当成标签吃掉，
# 已有的 `&amp;` 会被解码成 `&`。字幕正文来自用户原文，必须把这三个字符转成
# 实体才能原样显示。translate 是逐字符替换，`&` 不会二次转义自己产出的 `&amp;`。
VTT_ESCAPE = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})


def format_srt(cues) -> str:
    """SRT 文本：行尾统一 CRLF、序号从 1 连续、cue 正文不硬折行。

    要不要 BOM 由调用方决定：中文 Windows 上 PotPlayer / Premiere 读不到 BOM
    就按 ANSI 解码，整份字幕变乱码，所以本项目的导出接口写 UTF-8 带 BOM。

    正文不硬折行 —— 剪映 / Premiere 会把换行当成字幕里的真实换行。
    """
    blocks = []
    for i, cue in enumerate(cues, start=1):
        body = _one_line(cue["text"])
        blocks.append(
            f"{i}\r\n{_timestamp(cue['start'])} --> {_timestamp(cue['end'])}\r\n{body}\r\n"
        )
    if not blocks:
        return ""
    # 块之间空一行；最后一个块后面也补一行，按「空行切块」的老解析器才不会漏最后一条
    return "\r\n".join(blocks) + "\r\n"


def format_vtt(cues) -> str:
    """WebVTT：首行 WEBVTT，时间戳用点，不带序号。行尾同样统一 CRLF（含空文件）。"""
    lines = ["WEBVTT", ""]
    for cue in cues:
        body = _one_line(cue["text"]).translate(VTT_ESCAPE)
        lines.append(f"{_timestamp(cue['start'], '.')} --> {_timestamp(cue['end'], '.')}")
        lines.append(body)
        lines.append("")
    if len(lines) <= 2:
        return "WEBVTT\r\n\r\n"
    return "\r\n".join(lines)


def sidecar_path(subtitle_dir: str, wav_name: str) -> str:
    """字幕缓存路径：与音频同名，换成 .json，放在 subtitle_dir 下。

    只取 basename，调用方传进来的是文件名而不是路径 —— 带路径的输入
    （``..\\..\\x.wav``）不能穿越出缓存目录。
    """
    stem = os.path.splitext(os.path.basename(wav_name))[0]
    return os.path.join(subtitle_dir, f"{stem}.json")


def save_sidecar(path: str, data: dict) -> bool:
    """写字幕缓存。tmp + os.replace，永不半写；失败只返回 False，不抛。

    缓存是附属产物：慢盘、只读目录、磁盘满都不该让「生成成功」变成「报错」。
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def load_sidecar(path: str):
    """读字幕缓存；缺文件、坏 JSON、结构不对都当没有。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != SIDECAR_VERSION:
        return None
    if not isinstance(data.get("segments"), list):
        return None
    return data
