# -*- coding: utf-8 -*-
import logging
import os
import re
from typing import List, Union, overload
import warnings
from indextts.utils.common import tokenize_by_CJK_char, de_tokenized_by_CJK_char
from indextts.utils.glossary_store import (
    apply_glossary as _apply_glossary,
    get_term_pattern,
    reading_for as _reading_for,
    valid_pinyin_tone,
)
from sentencepiece import SentencePieceProcessor

# 接进 server.py 已经配好的日志系统（logs/server.log，按天轮转）。
# 之前这里一律用 print()，异常只落在 stdout 的黑窗口里，
# 排查「为什么这次合成的读音不对」时看不到任何记录。
logger = logging.getLogger("indextts.front")


class TextNormalizer:
    def __init__(self, enable_glossary=False):
        self.zh_normalizer = None
        self.en_normalizer = None
        # 最近一次 normalize() 是否退化成了「按原文处理」。
        # None = 正常；"zh"/"en" = 该语种的规范化失败过。
        # 上层（infer_v2 -> server -> 前端）据此提示用户，
        # 避免「读音不对但系统一声不吭」。
        self.normalize_fallback = None
        self.char_rep_map = {
            "：": ",",
            "；": ",",
            ";": ",",
            "，": ",",
            "。": ".",
            "！": "!",
            "？": "?",
            "\n": " ",
            "·": "-",
            "、": ",",
            "...": "…",
            ",,,": "…",
            "，，，": "…",
            "……": "…",
            "“": "'",
            "”": "'",
            '"': "'",
            "‘": "'",
            "’": "'",
            "（": "'",
            "）": "'",
            "(": "'",
            ")": "'",
            "《": "'",
            "》": "'",
            "【": "'",
            "】": "'",
            "[": "'",
            "]": "'",
            "—": "-",
            "～": "-",
            "~": "-",
            "「": "'",
            "」": "'",
            ":": ",",
        }
        self.zh_char_rep_map = {
            "$": ".",
            **self.char_rep_map,
        }
        self.enable_glossary = enable_glossary
        # 术语词汇表：用户可自定义专业术语的读法
        # 格式: {"原始术语": {"en": "英文读法", "zh": "中文读法"}}
        # "M.2": {"en": "M dot two", "zh": "M 二"},
        # "PCIe 5.0": {"en": "PCIE five", "zh": "PCIE 五点零"},
        # "PCIe 4.0": {"en": "PCIE four", "zh": "PCIE 四点零"},
        # "AHCI": "A H C I",
        # "TTS": "T T S",
        # "Inc.": {"en": "Ink"},
        # ".json": {"en": " dot Jay-Son", "zh": "点 Jay-Son"},
        # "C++": {"en": "C plus plus", "zh": "C 加加"},
        # "C#": "C sharp"
        # self.term_glossary = {
        #     "C++": {"en": "C plus plus", "zh": "C 加加"},
        #     "C#": "C sharp",
        #     "CMake": "C Make",
        # }
        self.term_glossary = dict()
        # 级联替换：关（默认）时每条术语只作用于原文，替换结果不再被后续术语改写；
        # 开时替换结果留在文本里，后面的术语会继续改写它（见 apply_glossary_terms）。
        # 单独存 glossary_settings.yaml，不塞进 term_glossary —— 那个 dict 会被
        # 当成「术语 -> 读音」遍历，混进一个 bool 会被当成一条叫 x 的术语。
        self.cascade_glossary = False

    def load_glossary_settings(self, settings_path):
        """读级联开关。文件不存在或坏掉都退回默认（关），不让它拖垮规范化。"""
        if not settings_path or not os.path.exists(settings_path):
            return
        try:
            import yaml
            with open(settings_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict) and "cascade" in data:
                self.cascade_glossary = bool(data.get("cascade"))
        except Exception as e:
            logger.warning("Failed to load glossary settings: %s", e)
            self.cascade_glossary = False

    def save_glossary_settings(self, settings_path):
        """写级联开关。原子替换，避免半截文件被下次读取当成坏数据。"""
        if not settings_path:
            return
        import yaml
        tmp = settings_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump({"cascade": bool(self.cascade_glossary)}, f,
                      allow_unicode=True, default_flow_style=False)
        os.replace(tmp, settings_path)

    def match_email(self, email):
        # 正则表达式匹配邮箱格式：数字英文@数字英文.英文
        pattern = r"^[a-zA-Z0-9]+@[a-zA-Z0-9]+\.[a-zA-Z]+$"
        return re.match(pattern, email) is not None

    PINYIN_TONE_PATTERN = r"(?<![a-z])((?:[bpmfdtnlgkhjqxzcsryw]|[zcs]h)?(?:[aeiouüv]|[ae]i|u[aio]|ao|ou|i[aue]|[uüv]e|[uvü]ang?|uai|[aeiuv]n|[aeio]ng|ia[no]|i[ao]ng)|ng|er)([1-5])"
    """
    匹配拼音声调格式：pinyin+数字，声调1-5，5表示轻声
    例如：xuan4, jve2, ying1, zhong4, shang5
    不匹配：beta1, voice2
    """
    NAME_PATTERN = r"[\u4e00-\u9fff]+(?:[-·—][\u4e00-\u9fff]+){1,2}"
    """
    匹配人名，格式：中文·中文，中文·中文-中文
    例如：克里斯托弗·诺兰，约瑟夫·高登-莱维特
    """

    TECH_TERM_PATTERN = r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+"
    """
    匹配技术术语，格式：字母开头+(字母或数字)*+(-字母或数字)+
    例如：GPT-5-nano, F5-TTS, Fish-Speech, GPT-5, CosyVoice-2
    必须以字母开头，避免匹配纯数字（如电话号码 135-4567-8900）
    用于保护连字符结构，防止中文normalizer将连字符解析为减号（如"负五减"）
    """

    # 匹配常见英语缩写 's，仅用于替换为 is，不匹配所有 's
    ENGLISH_CONTRACTION_PATTERN = r"(what|where|who|which|how|t?here|it|s?he|that|this)'s"


    def use_chinese(self, s):
        has_chinese = bool(re.search(r"[\u4e00-\u9fff]", s))
        has_alpha = bool(re.search(r"[a-zA-Z]", s))
        is_email = self.match_email(s)
        if has_chinese or not has_alpha or is_email:
            return True

        # 只有「真拼音」才算中文文本。早先用裸正则判断，A4 paper / win10 build
        # 这类纯英文句子会因为 "A4"、"win1" 命中而被送进中文规范化，
        # 结果 "paper" 被念成 "帕斯卡per"。
        return bool(self.find_pinyin_tones(s))

    @staticmethod
    def find_pinyin_tones(text):
        """返回文本里确实是拼音声调的片段（已过滤型号、版本号、系统名）。"""
        pattern = re.compile(TextNormalizer.PINYIN_TONE_PATTERN, re.IGNORECASE)
        found = []
        for match in pattern.finditer(text):
            if not TextNormalizer._valid_pinyin_tone(text, match):
                continue
            piece = "".join(g for g in match.groups() if g)
            if piece not in found:
                found.append(piece)
        return found

    def load(self):
        # print(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        # sys.path.append(model_dir)
        import platform
        if self.zh_normalizer is not None and self.en_normalizer is not None:
            return
        if platform.system() != "Linux":  # Mac and Windows
            from wetext import Normalizer

            self.zh_normalizer = Normalizer(remove_erhua=False, lang="zh", operator="tn")
            self.en_normalizer = Normalizer(lang="en", operator="tn")
        else:
            from tn.chinese.normalizer import Normalizer as NormalizerZh
            from tn.english.normalizer import Normalizer as NormalizerEn
            # use new cache dir for build tagger rules with disable remove_interjections and remove_erhua
            cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tagger_cache")
            if not os.path.exists(cache_dir):
                os.makedirs(cache_dir)
                with open(os.path.join(cache_dir, ".gitignore"), "w") as f:
                    f.write("*\n")
            self.zh_normalizer = NormalizerZh(
                cache_dir=cache_dir, remove_interjections=False, remove_erhua=False, overwrite_cache=False
            )
            self.en_normalizer = NormalizerEn(overwrite_cache=False)

    def normalize(self, text: str, glossary=None, cascade=None) -> str:
        if not self.zh_normalizer or not self.en_normalizer:
            logger.error("TextNormalizer is not initialized; returning raw text")
            self.normalize_fallback = "uninitialized"
            return text
        self.normalize_fallback = None      # 每次调用复位
        if self.use_chinese(text):
            text = re.sub(TextNormalizer.ENGLISH_CONTRACTION_PATTERN, r"\1 is", text, flags=re.IGNORECASE)
            # 应用术语词汇表（优先级最高，在所有保护之前）
            if self.enable_glossary or glossary is not None:
                text = self.apply_glossary_terms(text, lang="zh", glossary=glossary, cascade=cascade)
            # 保护技术术语（如 GPT-5-nano）避免被中文normalizer错误处理
            replaced_text, tech_list = self.save_tech_terms(text.rstrip())
            replaced_text, pinyin_list = self.save_pinyin_tones(replaced_text)

            replaced_text, original_name_list = self.save_names(replaced_text)
            try:
                result = self.zh_normalizer.normalize(replaced_text)
            except Exception:
                # 绝不能返回 ""。空串会让下游 split_segments 得到空列表、
                # 最终 torch.cat([]) 抛出 "expected a non-empty list of Tensors"
                # —— 一句和真实原因毫无关系的误导性报错。
                # 退回未规范化的原文：读音可能不标准，但至少能出声。
                logger.exception("zh normalization failed; falling back to pre-normalized text")
                self.normalize_fallback = "zh"
                result = replaced_text
            # 恢复人名
            result = self.restore_names(result, original_name_list)
            # 恢复拼音声调
            result = self.restore_pinyin_tones(result, pinyin_list)
            # 恢复技术术语
            result = self.restore_tech_terms(result, tech_list)
            pattern = re.compile("|".join(re.escape(p) for p in self.zh_char_rep_map.keys()))
            result = pattern.sub(lambda x: self.zh_char_rep_map[x.group()], result)
        else:
            try:
                text = re.sub(TextNormalizer.ENGLISH_CONTRACTION_PATTERN, r"\1 is", text, flags=re.IGNORECASE)
                # 应用术语词汇表（优先级最高，在所有保护之前）
                if self.enable_glossary or glossary is not None:
                    text = self.apply_glossary_terms(text, lang="en", glossary=glossary, cascade=cascade)
                # 保护技术术语（如 GPT-5-Nano）避免被英文normalizer错误处理
                replaced_text, tech_list = self.save_tech_terms(text)
                result = self.en_normalizer.normalize(replaced_text)
                # 恢复技术术语
                result = self.restore_tech_terms(result, tech_list)
            except Exception:
                # 中文分支原来退回原文，英文分支却退回空串，
                # 导致「英文偶尔还能出个东西、中文直接报错」的不对称。
                # 两边统一退回原文。
                logger.exception("en normalization failed; falling back to raw text")
                self.normalize_fallback = self.normalize_fallback or "en"
                result = text
            pattern = re.compile("|".join(re.escape(p) for p in self.char_rep_map.keys()))
            result = pattern.sub(lambda x: self.char_rep_map[x.group()], result)
        return result

    def correct_pinyin(self, pinyin: str):
        """
        将 jqx 的韵母为 u/ü 的拼音转换为 v
        如：ju -> jv , que -> qve, xün -> xvn
        """
        if pinyin[0] not in "jqxJQX":
            return pinyin
        # 匹配 jqx 的韵母为 u/ü 的拼音
        pattern = r"([jqx])[uü](n|e|an)*(\d)"
        repl = r"\g<1>v\g<2>\g<3>"
        pinyin = re.sub(pattern, repl, pinyin, flags=re.IGNORECASE)
        return pinyin.upper()

    def save_names(self, original_text):
        """
        替换人名为占位符 <n_a>、 <n_b>, ...
        例如：克里斯托弗·诺兰 -> <n_a>
        """
        # 人名
        name_pattern = re.compile(TextNormalizer.NAME_PATTERN, re.IGNORECASE)
        original_name_list = re.findall(name_pattern, original_text)
        if len(original_name_list) == 0:
            return (original_text, None)
        original_name_list = list(set("".join(n) for n in original_name_list))
        transformed_text = original_text
        # 替换占位符 <n_a>、 <n_b>, ...
        for i, name in enumerate(original_name_list):
            number = chr(ord("a") + i)
            transformed_text = transformed_text.replace(name, f"<n_{number}>")

        return transformed_text, original_name_list

    def restore_names(self, normalized_text, original_name_list):
        """
        恢复人名为原来的文字
        例如：<n_a> -> original_name_list[0]
        """
        if not original_name_list or len(original_name_list) == 0:
            return normalized_text

        transformed_text = normalized_text
        # 替换为占位符 <n_a>、 <n_b>, ...
        for i, name in enumerate(original_name_list):
            number = chr(ord("a") + i)
            transformed_text = transformed_text.replace(f"<n_{number}>", name)
        return transformed_text

    def save_tech_terms(self, original_text):
        """
        保护技术术语中的连字符，防止被中文normalizer解析为减号
        策略：将术语中的连字符替换为特殊占位符<H>，数字仍可被正常处理
        例如：GPT-5-nano -> GPT<H>5<H>nano，然后 5 被转换为 五
        最终恢复为：GPT-五-nano
        """
        tech_pattern = re.compile(TextNormalizer.TECH_TERM_PATTERN)
        original_tech_list = tech_pattern.findall(original_text)
        if len(original_tech_list) == 0:
            return (original_text, None)

        # 去重并按长度降序排列（避免短匹配先替换导致问题）
        original_tech_list = sorted(set(original_tech_list), key=len, reverse=True)
        transformed_text = original_text

        # 将术语中的连字符替换为占位符 <H>
        for term in original_tech_list:
            # 将 GPT-5-nano 替换为 GPT<H>5<H>nano
            protected_term = term.replace("-", "<H>")
            transformed_text = transformed_text.replace(term, protected_term)

        return transformed_text, original_tech_list

    def restore_tech_terms(self, normalized_text, original_tech_list):
        """
        恢复技术术语中的连字符
        将占位符 <H> 恢复为连字符 -
        同时清理 normalizer 可能在占位符周围添加的多余空格
        """
        if not original_tech_list or len(original_tech_list) == 0:
            return normalized_text

        # 清理 <H> 周围可能的空格，然后恢复为连字符
        # 处理模式: " <H> " -> "-", " <H>" -> "-", "<H> " -> "-", "<H>" -> "-"
        transformed_text = re.sub(r'\s*<H>\s*', '-', normalized_text)
        return transformed_text

    def apply_glossary_terms(self, text, lang="zh", glossary=None, cascade=None):
        """
        应用术语词汇表，将专业术语替换为对应语言的读法。

        Args:
            text: 待处理文本
            lang: 语言类型 "zh" 或 "en"
            glossary: 可选快照；None 表示用实例上的 term_glossary
            cascade: 可选级联开关；None 表示用实例上的 cascade_glossary

        匹配实现放在 glossary_store（纯逻辑、无 torch），
        保证「试读结果 = 预览结果 = 实际分词输入」。

        Example:
            "M.2 NVMe SSD" -> (zh) "M 二 NVMe SSD"
            "M.2 NVMe SSD" -> (en) "M dot two NVMe SSD"
        """
        source = self.term_glossary if glossary is None else glossary
        if not source:
            return text
        use_cascade = self.cascade_glossary if cascade is None else cascade
        result, _matched = _apply_glossary(text, source, lang, bool(use_cascade))
        return result

    def _glossary_replacement(self, glossary, term, lang):
        """兼容旧调用；空读法/缺列一律返回 None（不再替换成空串）。"""
        return _reading_for(glossary.get(term), lang)

    def load_glossary(self, glossary_dict):
        """
        加载外部术语词汇表

        Args:
            glossary_dict: 术语词典，格式为 {"术语": {"en": "英文读法", "zh": "中文读法"}}

        Example:
            normalizer.load_glossary({
                "M.2": {"en": "M dot two", "zh": "M 二"},
                "PCIe": {"en": "PCIE", "zh": "PCIE"}
            })
        """
        if glossary_dict and isinstance(glossary_dict, dict):
            self.term_glossary.update(self._clean_glossary(glossary_dict))

    def load_glossary_from_yaml(self, glossary_path):
        """
        从 YAML 文件加载术语词汇表

        Args:
            glossary_path: YAML 文件路径

        Example:
            normalizer.load_glossary_from_yaml("checkpoints/glossary.yaml")

        YAML 文件格式:
            M.2:
              en: M dot two
              zh: M 二
            NVMe: N-V-M-E  # 中英文相同读法
        """
        if glossary_path and os.path.exists(glossary_path):
            import yaml
            with open(glossary_path, 'r', encoding='utf-8') as f:
                # BaseLoader：未加引号的 5090 / yes 全部按字符串读，
                # 旧实现会把它解析成 int/bool，key 被 _clean_glossary 丢掉。
                external_glossary = yaml.load(f, Loader=yaml.BaseLoader)
                if external_glossary and isinstance(external_glossary, dict):
                    self.term_glossary = self._clean_glossary(external_glossary)
                    return True
        return False

    @staticmethod
    def _clean_glossary(data):
        """丢弃会让规范化链崩掉的词条，并返回新字典。

        术语表是可以手写的 YAML，一条脏数据的后果是【每一次】合成都崩：
          - 空 / 非字符串 key -> get_term_pattern 里 term[0] 抛 IndexError
          - 读音不是字符串   -> re.sub 抛 TypeError（在 apply_glossary_terms 兜底）
        这里静默跳过 + 记 warning，坏数据不应该拖垮整个服务。
        """
        if not isinstance(data, dict):
            return {}
        cleaned = {}
        dropped = 0
        for k, v in data.items():
            if not isinstance(k, str) or not k.strip():
                dropped += 1
                logger.warning("Dropping glossary entry with invalid key: %r", k)
                continue
            cleaned[k] = v
        if dropped:
            logger.warning("Dropped %d invalid glossary entr%s",
                           dropped, "y" if dropped == 1 else "ies")
        return cleaned

    def save_glossary_to_yaml(self, glossary_path):
        """
        保存术语词汇表到 YAML 文件（原子替换，避免半截文件）。

        Args:
            glossary_path: YAML 文件路径
        """
        import yaml
        tmp = glossary_path + ".tmp"
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            yaml.dump(self.term_glossary, f, allow_unicode=True, default_flow_style=False)
        os.replace(tmp, glossary_path)

    def save_pinyin_tones(self, original_text):
        """
        替换拼音声调为占位符 <pinyin_a>, <pinyin_b>, ...
        例如：xuan4 -> <pinyin_a>

        正则只做粗筛（它同时被用来校验词表里那批拼音 token，不能收紧），
        真正决定「这串到底是不是拼音」的是 _valid_pinyin_tone：版本号、
        型号、系统版本这类字母紧贴数字的写法一律放过，交给 wetext 正常
        转成中文数字——否则数字会被藏进占位符、绕开规范化，最后变成
        <unk> 直接念不出来（v2.1.3 念成 "v 一点三"，2 被吞掉）。
        """
        # 声母韵母+声调数字
        origin_pinyin_pattern = re.compile(TextNormalizer.PINYIN_TONE_PATTERN, re.IGNORECASE)
        collected = []

        def _replace(match):
            if not TextNormalizer._valid_pinyin_tone(original_text, match):
                return match.group(0)
            pinyin = "".join(g for g in match.groups() if g)
            if pinyin not in collected:
                collected.append(pinyin)
            return f"<pinyin_{chr(ord('a') + collected.index(pinyin))}>"

        transformed_text = origin_pinyin_pattern.sub(_replace, original_text)
        if not collected:
            return (original_text, None)

        # print("original_text: ", original_text)
        # print("transformed_text: ", transformed_text)
        return transformed_text, collected

    @staticmethod
    def _valid_pinyin_tone(text, match):
        """正则命中的这一段该不该按「拼音+声调」保护。

        判定本体在 glossary_store.valid_pinyin_tone：术语弹窗的试读
        （/glossary/test 的 detect_lang）要按同一口径选读法列，两处各写
        一份迟早漂移，所以这里只做转发。
        """
        return valid_pinyin_tone(text, match)

    def restore_pinyin_tones(self, normalized_text, original_pinyin_list):
        """
        恢复拼音中的音调数字（1-5）为原来的拼音
        例如：<pinyin_a> -> original_pinyin_list[0]
        """
        if not original_pinyin_list or len(original_pinyin_list) == 0:
            return normalized_text

        transformed_text = normalized_text
        # 替换占位符 <pinyin_a>, <pinyin_b>, ...
        for i, pinyin in enumerate(original_pinyin_list):
            number = chr(ord("a") + i)
            pinyin = self.correct_pinyin(pinyin)
            transformed_text = transformed_text.replace(f"<pinyin_{number}>", pinyin)
        # print("normalized_text: ", normalized_text)
        # print("transformed_text: ", transformed_text)
        return transformed_text


class TextTokenizer:
    def __init__(self, vocab_file: str, normalizer: TextNormalizer = None):
        self.vocab_file = vocab_file
        self.normalizer = normalizer

        if self.vocab_file is None:
            raise ValueError("vocab_file is None")
        if not os.path.exists(self.vocab_file):
            raise ValueError(f"vocab_file {self.vocab_file} does not exist")
        if self.normalizer:
            self.normalizer.load()
        # 加载词表
        self.sp_model = SentencePieceProcessor(model_file=self.vocab_file)

        self.pre_tokenizers = [
            # 预处理器
            tokenize_by_CJK_char,
        ]

    @property
    def vocab_size(self):
        return self.sp_model.GetPieceSize()

    @property
    def unk_token(self):
        return "<unk>"

    @property
    def pad_token(self):
        return None

    @property
    def bos_token(self):
        return "<s>"

    @property
    def eos_token(self):
        return "</s>"

    @property
    def pad_token_id(self):
        return -1

    @property
    def bos_token_id(self):
        return 0

    @property
    def eos_token_id(self):
        return 1

    @property
    def unk_token_id(self):
        return self.sp_model.unk_id()

    @property
    def special_tokens_map(self):
        return {
            "unk_token": self.unk_token,
            "pad_token": self.pad_token,
            "bos_token": self.bos_token,
            "eos_token": self.eos_token,
        }

    def get_vocab(self):
        vocab = {self.convert_ids_to_tokens(i): i for i in range(self.vocab_size)}
        return vocab

    @overload
    def convert_ids_to_tokens(self, ids: int) -> str: ...

    @overload
    def convert_ids_to_tokens(self, ids: List[int]) -> List[str]: ...

    def convert_ids_to_tokens(self, ids: Union[List[int], int]):
        return self.sp_model.IdToPiece(ids)

    def convert_tokens_to_ids(self, tokens: Union[List[str], str]) -> List[int]:
        if isinstance(tokens, str):
            tokens = [tokens]
        return [self.sp_model.PieceToId(token) for token in tokens]

    def tokenize(self, text: str, glossary=None, cascade=None) -> List[str]:
        return self.encode(text, out_type=str, glossary=glossary, cascade=cascade)

    def encode(self, text: str, glossary=None, cascade=None, **kwargs):
        if len(text) == 0:
            return []
        if len(text.strip()) == 1:
            # 原实现直接返回，术语表在这条路径上完全不生效。
            # 只在调用方显式传了快照时补一次术语替换，保持库默认行为不变。
            if self.normalizer and glossary is not None:
                lang = "zh" if self.normalizer.use_chinese(text) else "en"
                text = self.normalizer.apply_glossary_terms(
                    text, lang=lang, glossary=glossary, cascade=cascade
                )
            return self.sp_model.Encode(text, out_type=kwargs.pop("out_type", int), **kwargs)
        # 预处理
        if self.normalizer:
            text = self.normalizer.normalize(text, glossary=glossary, cascade=cascade)
        if len(self.pre_tokenizers) > 0:
            for pre_tokenizer in self.pre_tokenizers:
                text = pre_tokenizer(text)
        return self.sp_model.Encode(text, out_type=kwargs.pop("out_type", int), **kwargs)

    def batch_encode(self, texts: List[str], glossary=None, cascade=None, **kwargs):
        # 预处理
        if self.normalizer:
            texts = [
                self.normalizer.normalize(text, glossary=glossary, cascade=cascade)
                for text in texts
            ]
        if len(self.pre_tokenizers) > 0:
            for pre_tokenizer in self.pre_tokenizers:
                texts = [pre_tokenizer(text) for text in texts]
        return self.sp_model.Encode(texts, out_type=kwargs.pop("out_type", int), **kwargs)

    def decode(self, ids: Union[List[int], int], do_lower_case=False, **kwargs):
        if isinstance(ids, int):
            ids = [ids]
        decoded = self.sp_model.Decode(ids, out_type=kwargs.pop("out_type", str), **kwargs)
        return de_tokenized_by_CJK_char(decoded, do_lower_case=do_lower_case)

    @staticmethod
    def split_segments_by_token(
        tokenized_str: List[str],
        split_tokens: List[str],
        max_text_tokens_per_segment: int,
        quick_streaming_tokens: int = 0,
        sentence_boundary_split: bool = True,
    ) -> List[List[str]]:
        """
        将tokenize后的结果按特定token进一步分割

        max_text_tokens_per_segment 必须是正整数：0 会让下面的
        range(0, len, 0) 抛 "range() arg 3 must not be zero"，负数则
        range 步长为负导致片段列表为空。上游（server.py 的 _clamp_seg_tokens）
        已经挡住 HTTP 入口，这里是纯库调用方的兜底。

        sentence_boundary_split=True（默认）时，以句末标点结尾的段不再与邻段
        合并，「一句话一段」。设 False 恢复旧的逐字合并行为——那时只要相邻两段
        合计不超上限就会粘在一起，多句话被连成一段，段内停顿由模型自由发挥。
        """
        if max_text_tokens_per_segment < 1:
            raise ValueError(
                "max_text_tokens_per_segment 必须 >= 1，收到 %r" % (max_text_tokens_per_segment,)
            )
        # 处理特殊情况
        if len(tokenized_str) == 0:
            return []
        segments: List[List[str]] = []
        current_segment = []
        current_segment_tokens_len = 0
        # 下面要把紧跟切分符的 "'" 一并吞掉；for 循环里改 i 不生效（下一轮会重取），
        # 所以用跳过集合显式表达「这个下标已经处理过了」。
        skipped: set = set()
        for i in range(len(tokenized_str)):
            if i in skipped:
                continue
            token = tokenized_str[i]
            current_segment.append(token)
            current_segment_tokens_len += 1
            if not  ("," in split_tokens or "▁," in split_tokens ) and ("," in current_segment or "▁," in current_segment): 
                # 如果当前tokens中有,，则按,分割
                sub_segments = TextTokenizer.split_segments_by_token(
                    current_segment, [",", "▁,"], max_text_tokens_per_segment=max_text_tokens_per_segment, quick_streaming_tokens = quick_streaming_tokens
                )
            elif "-" not in split_tokens and "-" in current_segment:
                # 没有,，则按-分割
                sub_segments = TextTokenizer.split_segments_by_token(
                    current_segment, ["-"], max_text_tokens_per_segment=max_text_tokens_per_segment, quick_streaming_tokens = quick_streaming_tokens
                )
            elif current_segment_tokens_len <= max_text_tokens_per_segment:
                if token in split_tokens and current_segment_tokens_len > 2:
                    # 版本号里的点：`v2.1.3`、`1.0.0` 的点前后都是数字，
                    # 它是小数点不是句号。规范化会把纯数字转成中文读法
                    # （3.14 -> 三点一四），但带字母前后缀的版本号会保留原点号，
                    # 不挡住就会被切成「v2.」+「1.3」，听感上中间多一次停顿。
                    if TextTokenizer._is_decimal_point(tokenized_str, i):
                        continue
                    # 标点连用（？！ / ！？）：只在最后一个后面切。
                    # 否则「你确定？！」会切成「你确定？」+「！这可不是……」，
                    # 感叹号孤立跑到下一段开头。
                    if TextTokenizer._followed_by_punct(tokenized_str, i, split_tokens):
                        continue
                    if i < len(tokenized_str) - 1:
                        if tokenized_str[i + 1] in ["'", "▁'"]:
                            # 后续token是'，则不切分
                            current_segment.append(tokenized_str[i + 1])
                            skipped.add(i + 1)
                    segments.append(current_segment)
                    current_segment = []
                    current_segment_tokens_len = 0
                continue
            # 如果当前tokens的长度超过最大限制
            else:
                # 按照长度分割
                sub_segments = []
                for j in range(0, len(current_segment), max_text_tokens_per_segment):
                    if j + max_text_tokens_per_segment < len(current_segment):
                        sub_segments.append(current_segment[j : j + max_text_tokens_per_segment])
                    else:
                        sub_segments.append(current_segment[j:])
                warnings.warn(
                    f"The tokens length of segment exceeds limit: {max_text_tokens_per_segment}, "
                    f"Tokens in segment: {current_segment}."
                    "Maybe unexpected behavior",
                    RuntimeWarning,
                )
            segments.extend(sub_segments)
            current_segment = []
            current_segment_tokens_len = 0
        if current_segment_tokens_len > 0:
            assert current_segment_tokens_len <= max_text_tokens_per_segment
            segments.append(current_segment)
        # 如果相邻的句子加起来长度小于最大限制，且此前token总数超过quick_streaming_tokens，则合并
        #
        # 但句末标点切出来的段不参与合并：合并的本意只是避免产生过短的碎片段，
        # 而句号/问号/感叹号恰恰是听感上该换气的地方。一旦把多句话合并成一段，
        # 段内停顿就由模型自由发挥、无法控制（实测段间 0.64~0.91s 且波动很大），
        # 用户设的 interval_silence 也就失效了。sentence_boundary_split=False
        # 可恢复逐字合并的旧行为，用于对照与回滚。
        sentence_end_tokens = (
            set(TextTokenizer.punctuation_marks_tokens) if sentence_boundary_split else set()
        )
        merged_segments = []
        tail_done = []      # 与 merged_segments 一一对应：该段是否已经「说完」
        quote_open = False  # 按出现顺序配对引号：True 表示现在在引号内部
        total_token = 0
        for segment in segments:
            total_token += len(segment)
            if len(segment) == 0:
                continue
            # 先过一遍本段的引号，判断段尾那个引号是开还是闭。
            # 词表里 “”「」" 和全角括号全被归一成同一个 "'"，只能靠配对区分：
            # 第 1、3、5… 个是开引号，第 2、4、6… 个是闭引号。
            last_quote_closes = None
            for t in segment:
                if t in TextTokenizer._TRAILING_QUOTE_TOKENS:
                    quote_open = not quote_open
                    last_quote_closes = not quote_open
            done = TextTokenizer._ends_sentence(
                segment, sentence_end_tokens, last_quote_closes
            )
            # 上段已经说完就换段；否则（上段是半句）允许继续合并。
            # 反过来不限：本段收尾是句号、上段是逗号时仍要合并，否则
            # 「老张推开门的时候，」会孤零零成为一段。
            if not merged_segments or tail_done[-1]:
                merged_segments.append(segment)
                tail_done.append(done)
                continue
            if len(merged_segments[-1]) + len(segment) <= max_text_tokens_per_segment and total_token > quick_streaming_tokens:
                merged_segments[-1] = merged_segments[-1] + segment
                tail_done[-1] = done
            # 或小于最大长度限制的一半，则合并
            elif len(merged_segments[-1]) + len(segment) <= max_text_tokens_per_segment / 2:
                merged_segments[-1] = merged_segments[-1] + segment
                tail_done[-1] = done
            else:
                merged_segments.append(segment)
                tail_done.append(done)
        return merged_segments

    # 引号、括号在 tokenize 后一律变成裸 "'"（中文 “”「」、英文 "、全角括号
    # 都被前端替换表归一），并且紧跟在句号后面，会挡住「这句说完了」的判断。
    # 例：`▁.` `'` 是「。”」，得跳过引号再看前一个 token。
    _TRAILING_QUOTE_TOKENS = ("'", "▁'")

    @staticmethod
    def _ends_sentence(segment: List[str], sentence_end_tokens,
                       tail_quote_closes=None) -> bool:
        """这一段是否以句末标点收尾。

        tail_quote_closes 指明段尾那个引号是闭引号（True）/ 开引号（False），
        由调用方按引号配对算出。段尾是开引号时句子还没说完——引号里还有话，
        此时不能当句末，否则「他说：“……」」会被切成两段，中间硬插一个停顿。
        """
        if not sentence_end_tokens or not segment:
            return False
        i = len(segment) - 1
        if segment[i] in TextTokenizer._TRAILING_QUOTE_TOKENS:
            if tail_quote_closes is False:
                return False
            while i >= 0 and segment[i] in TextTokenizer._TRAILING_QUOTE_TOKENS:
                i -= 1
        return i >= 0 and segment[i] in sentence_end_tokens

    # 数字 token 的字符集：阿拉伯数字 + 中文数字。规范化会把 3.14 转成
    # 「三点一四」，剩下带点的基本只有版本号（v2.1.3 / 1.0.0），那里必须
    # 认出「点左右都是数字」才能不切。
    _DIGIT_CHARS = set("0123456789零一二三四五六七八九十百千万亿〇")

    @staticmethod
    def _is_digit_token(tok: str) -> bool:
        t = (tok or "").lstrip("▁")
        return bool(t) and all(c in TextTokenizer._DIGIT_CHARS for c in t)

    @staticmethod
    def _is_decimal_point(tokens: List[str], i: int) -> bool:
        """这个点是不是小数点/版本号里的点（前后都是数字）。"""
        if tokens[i] not in (".", "▁."):
            return False
        k = i - 1
        while k >= 0 and tokens[k] == "▁":
            k -= 1
        before = tokens[k] if k >= 0 else ""
        j = i + 1
        while j < len(tokens) and tokens[j] == "▁":
            j += 1
        after = tokens[j] if j < len(tokens) else ""
        return TextTokenizer._is_digit_token(before) and TextTokenizer._is_digit_token(after)

    @staticmethod
    def _followed_by_punct(tokens: List[str], i: int, split_tokens) -> bool:
        """切点后面紧跟另一个切分标点（如「？!」），说明标点连用。

        这时先不切，让循环继续累积到最后一个标点再断，否则后一个标点会
        孤立地跑到下一段开头。
        """
        marks = set(split_tokens or ())
        j = i + 1
        while j < len(tokens) and tokens[j] == "▁":
            j += 1
        return j < len(tokens) and tokens[j] in marks

    punctuation_marks_tokens = [
        ".",
        "!",
        "?",
        "▁.",
        # "▁!", # unk
        "▁?",
        "▁...", # ellipsis
    ]
    def split_segments(self, tokenized: List[str], max_text_tokens_per_segment=120,
                       quick_streaming_tokens=0, sentence_boundary_split=True) -> List[List[str]]:
        return TextTokenizer.split_segments_by_token(
            tokenized, self.punctuation_marks_tokens,
            max_text_tokens_per_segment=max_text_tokens_per_segment,
            quick_streaming_tokens=quick_streaming_tokens,
            sentence_boundary_split=sentence_boundary_split,
        )


if __name__ == "__main__":
    # 测试程序

    text_normalizer = TextNormalizer(enable_glossary=True)

    cases = [
        "IndexTTS 正式发布1.0版本了，效果666",
        "晕XUAN4是一种GAN3觉",
        "我爱你！",
        "I love you!",
        "“我爱你”的英语是“I love you”",
        "2.5平方电线",
        "共465篇，约315万字",
        "2002年的第一场雪，下在了2003年",
        "速度是10km/h",
        "现在是北京时间2025年01月11日 20:00",
        "他这条裤子是2012年买的，花了200块钱",
        "电话：135-4567-8900",
        "1键3连",
        "他这条视频点赞3000+，评论1000+，收藏500+",
        "这是1024元的手机，你要吗？",
        "受不liao3你了",
        "“衣裳”不读衣chang2，而是读衣shang5",
        "最zhong4要的是：不要chong2蹈覆辙",
        "不zuo1死就不会死",
        "See you at 8:00 AM",
        "8:00 AM 开会",
        "Couting down 3, 2, 1, go!",
        "数到3就开始：1、2、3",
        "This sales for 2.5% off, only $12.5.",
        "5G网络是4G网络的升级版，2G网络是3G网络的前身",
        "苹果于2030/1/2发布新 iPhone 2X 系列手机，最低售价仅 ¥12999",
        "这酒...里...有毒...",
        # 异常case
        "只有,,,才是最好的",
        "babala2是什么？",  # babala二是什么?
        "用beta1测试",  # 用beta一测试
        "have you ever been to beta2?",  # have you ever been to beta two?
        "where's the money?",  # where is the money?
        "who's there?",  # who is there?
        "which's the best?",  # which is the best?
        "how's it going?",  # how is it going?
        "今天是个好日子 it's a good day",  # 今天是个好日子 it is a good day
        # 术语
        "such as XTTS, CosyVoice2, Fish-Speech, and F5-TTS",  # such as xtts,cosyvoice two,fish-speech,and f five-tts
        "GPT-5-Nano is the smallest and fastest variant in the GPT-5 model family.",  # GPT-five-Nano is the smallest and fastest variant in the GPT-five model family
        "GPT-5-Nano 是 GPT-5 模型家族中最小且速度最快的变体",  # GPT-五-Nano 是 GPT-五 系统中最小且速度最快的变体
        "2025/09/08 IndexTTS-2 全球发布",  # 二零二五年九月八日 IndexTTS-二全球发布
        "Here are some highly-rated M.2 NVMe SSDs: Samsung 9100 PRO PCIe 5.0 SSD M.2, $139.99",  # Here are some highly-rated M dot two NVMe SSD's, Samsung nine thousand one hundred PRO PCIE five SSD M dot two . one hundred and thirty nine dollars and ninety nine cents
        "we dive deep into the showdown between DisplayPort 1.4 and HDMI 2.1 to determine which is the best choice for gaming enthusiasts",
        # 人名
        "约瑟夫·高登-莱维特（Joseph Gordon-Levitt is an American actor）",
        "蒂莫西·唐纳德·库克（英文名：Timothy Donald Cook），通称蒂姆·库克（Tim Cook），美国商业经理、工业工程师和工业开发商，现任苹果公司首席执行官。",
        # 长句子
        "《盗梦空间》是由美国华纳兄弟影片公司出品的电影，由克里斯托弗·诺兰执导并编剧，莱昂纳多·迪卡普里奥、玛丽昂·歌迪亚、约瑟夫·高登-莱维特、艾利奥特·佩吉、汤姆·哈迪等联袂主演，2010年7月16日在美国上映，2010年9月1日在中国内地上映，2020年8月28日在中国内地重映。影片剧情游走于梦境与现实之间，被定义为“发生在意识结构内的当代动作科幻片”，讲述了由莱昂纳多·迪卡普里奥扮演的造梦师，带领特工团队进入他人梦境，从他人的潜意识中盗取机密，并重塑他人梦境的故事。",
        "清晨拉开窗帘，阳光洒在窗台的Bloomixy花艺礼盒上——薰衣草香薰蜡烛唤醒嗅觉，永生花束折射出晨露般光泽。设计师将“自然绽放美学”融入每个细节：手工陶瓷花瓶可作首饰收纳，香薰精油含依兰依兰舒缓配方。限量款附赠《365天插花灵感手册》，让每个平凡日子都有花开仪式感。\n宴会厅灯光暗下的刹那，Glimmeria星月系列耳坠开始发光——瑞士冷珐琅工艺让蓝宝石如银河流动，钛合金骨架仅3.2g无负重感。设计师秘密：内置微型重力感应器，随步伐产生0.01mm振幅，打造“行走的星光”。七夕限定礼盒含星座定制铭牌，让爱意如星辰永恒闪耀。",
        "电影1：“黑暗骑士”（演员：克里斯蒂安·贝尔、希斯·莱杰；导演：克里斯托弗·诺兰）；电影2：“盗梦空间”（演员：莱昂纳多·迪卡普里奥；导演：克里斯托弗·诺兰）；电影3：“钢琴家”（演员：艾德里安·布洛迪；导演：罗曼·波兰斯基）；电影4：“泰坦尼克号”（演员：莱昂纳多·迪卡普里奥；导演：詹姆斯·卡梅隆）；电影5：“阿凡达”（演员：萨姆·沃辛顿；导演：詹姆斯·卡梅隆）；电影6：“南方公园：大电影”（演员：马特·斯通、托马斯·艾恩格瑞；导演：特雷·帕克）",
    ]
    # 测试分词器
    tokenizer = TextTokenizer(
        vocab_file="checkpoints/bpe.model",
        normalizer=text_normalizer,
    )

    codes = tokenizer.batch_encode(
        cases,
        out_type=int,
    )

    print(f"vocab_size: {tokenizer.vocab_size}")
    # print(f"pad_token: {tokenizer.pad_token}, pad_token_id: {tokenizer.pad_token_id}")
    print(f"bos_token: {tokenizer.bos_token}, bos_token_id: {tokenizer.bos_token_id}")
    print(f"eos_token: {tokenizer.eos_token}, eos_token_id: {tokenizer.eos_token_id}")
    print(f"unk_token: {tokenizer.unk_token}, unk_token_id: {tokenizer.unk_token_id}")
    # 测试拼音 (8474-10201)
    for id in range(8474, 10201):
        pinyin = tokenizer.convert_ids_to_tokens(id)
        if re.match(TextNormalizer.PINYIN_TONE_PATTERN, pinyin, re.IGNORECASE) is None:
            print(f"{pinyin} should be matched")
    for badcase in [
        "beta1", "better1", "voice2", "bala2", "babala2", "hunger2"
    ]:
        if re.match(TextNormalizer.PINYIN_TONE_PATTERN, badcase, re.IGNORECASE) is not None:
            print(f"{badcase} should not be matched!")
    # 不应该有 unk_token_id
    for t in set([*TextTokenizer.punctuation_marks_tokens, ",", "▁,", "-", "▁..."]):
        tokens = tokenizer.convert_tokens_to_ids(t)
        if tokenizer.unk_token_id in tokens:
            print(f"Warning: {t} is unknown token")
        print(f"`{t}`", "->", tokens, "->", tokenizer.convert_ids_to_tokens(tokens))
    for ch in set(tokenizer.normalizer.zh_char_rep_map.values()):
        # 测试 normalize后的字符能被分词器识别
        print(f"`{ch}`", "->", tokenizer.sp_model.Encode(ch, out_type=str))
        print(f"` {ch}`", "->", tokenizer.sp_model.Encode(f" {ch}", out_type=str))
    max_text_tokens_per_segment=120
    for i in range(len(cases)):
        print(f"原始文本: {cases[i]}")
        print(f"Normalized: {text_normalizer.normalize(cases[i])}")
        tokens = tokenizer.tokenize(cases[i])
        print("Tokenzied: ", ", ".join([f"`{t}`" for t in tokens]))
        segments = tokenizer.split_segments(tokens, max_text_tokens_per_segment=max_text_tokens_per_segment)
        print("Segments count:", len(segments))
        if len(segments) > 1:
            for j in range(len(segments)):
                print(f"  {j}, count:", len(segments[j]), ", tokens:", "".join(segments[j]))
                if len(segments[j]) > max_text_tokens_per_segment:
                    print(f"Warning: segment {j} is too long, length: {len(segments[j])}")
        #print(f"Token IDs (first 10): {codes[i][:10]}")
        if tokenizer.unk_token in codes[i]:
            print(f"Warning: `{cases[i]}` contains UNKNOWN token")
        print(f"Decoded: {tokenizer.decode(codes[i], do_lower_case=True)}")
        print("-" * 50)
