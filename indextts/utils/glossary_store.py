# -*- coding: utf-8 -*-
"""术语规则库：规则 / 场景 / 快照 / 原子落盘。

这个模块刻意不依赖 FastAPI、torch 或项目的 normalizer：
服务启动时只读两个小 YAML 就能把弹窗和侧栏渲染出来，
不需要等模型/分词器加载。
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable

import yaml

logger = logging.getLogger("indextts.glossary")

FORMAT_V2 = "index-tts-glossary-v2"
RULES_FILENAME = "glossary_rules.yaml"
FLAT_FILENAME = "glossary.yaml"
SETTINGS_FILENAME = "glossary_settings.yaml"
BACKUP_SUFFIX = ".bak"

DEFAULT_SCENARIO = ""            # 默认场景没有 id，接口里用空串
DEFAULT_SCENARIO_NAME = "默认（全部启用）"
RESERVED_SCENARIO_NAMES = {"默认", "默认（全部启用）"}

MAX_TERM_LEN = 200
MAX_READING_LEN = 500
MAX_SCENARIO_NAME_LEN = 40
MAX_RULES_SOFT = 1000
MAX_SCENARIOS_SOFT = 50

_PUA_RE = re.compile("[\ue000-\uf8ff]")
_INVISIBLE_RE = re.compile("[\u200b-\u200d\ufeff\u202a-\u202e]")
_CONTROL_RE = re.compile("[\x00-\x1f\x7f]")
# 与 front.TextNormalizer.PINYIN_TONE_PATTERN 同口径：试读只判断走中/英分支，
# 不真正转换拼音，所以这里保留一份副本，避免纯逻辑模块反向依赖 torch。
_PINYIN_TONE_PATTERN = (
    r"(?<![a-z])((?:[bpmfdtnlgkhjqxzcsryw]|[zcs]h)?"
    r"(?:[aeiouüv]|[ae]i|u[aio]|ao|ou|i[aue]|[uüv]e|[uvü]ang?|uai|"
    r"[aeiuv]n|[aeio]ng|ia[no]|i[ao]ng)|ng|er)([1-5])"
)
_PINYIN_TONE_RE = re.compile(_PINYIN_TONE_PATTERN, re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9]+@[a-zA-Z0-9]+\.[a-zA-Z]+$")


def valid_pinyin_tone(text: str, match) -> bool:
    """正则命中的这一段该不该按「拼音+声调」保护。

    PINYIN_TONE_PATTERN 是为词表校验写的（要能匹配 A5、E1、XUAN4 这些
    拼音 token），直接拿来扫正文过于宽松，实测误伤：

        v2.1.3    ->  v2 当成「韵母 v + 第二声」，2 被吞
        A4 纸     ->  A4 当成「a + 第四声」，4 被吞
        win10     ->  win1 当成「wen 之类」，1 被吞
        京A12345  ->  A1 被保护，1 被吞

    误伤的后果不是「读得不准」，而是数字直接消失，所以按真实拼音的
    书写规则再筛一遍。判定放在这里而不是 front.py：术语弹窗的试读
    （/glossary/test -> detect_lang）必须和主链路同口径，两处各写一份
    迟早会漂移。
    """
    letters = match.group(1) or ""
    if not letters:
        return False
    low = letters.lower()

    # 拼音音节没有以 v / ü 开头的（女=nv、绿=lv、鱼=yu），v2 必是版本号
    if low[0] in "vü":
        return False

    # 单个元音 + 数字：真拼音只有 a/o/e，且用户输入是小写（e2、o2）。
    # 写成大写单字母的（A4、E3、O2）在中文正文里一律是型号编号。
    if len(low) == 1 and letters.isupper():
        return False

    # 声母 w 只接 a/o/u/e（wa wo wu we wen wang），没有 wi- 音节，
    # 所以 win7 / win10 / wifi5 都是系统名，不是拼音
    if low[0] == "w" and (len(low) < 2 or low[1] not in "aoue"):
        return False

    # 声调数字后面还跟着数字、或跟着「.数字」→ 版本号/编号（v2.1.3、10.5.2）
    tail = text[match.end():match.end() + 2]
    if tail and tail[0].isdigit():
        return False
    if len(tail) > 1 and tail[0] in ".．" and tail[1].isdigit():
        return False

    return True


def has_pinyin_tone(text: str) -> bool:
    """文本里有没有真拼音声调（型号、版本号、系统名不算）。"""
    return any(valid_pinyin_tone(text, m) for m in _PINYIN_TONE_RE.finditer(text))


class GlossaryValidationError(ValueError):
    """保存前校验失败。errors 是 [{field, index, message}]，直接回给前端。"""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("; ".join(e["message"] for e in errors[:5]) or "术语规则校验失败")


class GlossaryConflict(ValueError):
    """rev 不匹配：别处已经改过，调用方决定覆盖还是刷新。"""

    def __init__(self, document: dict):
        self.document = document
        super().__init__("术语规则已在别处修改")


class GlossaryReadOnly(RuntimeError):
    """未知格式版本降级打开：只读，禁止保存，防止把新文件覆盖坏。"""


class UnknownScenario(KeyError):
    def __init__(self, scenario_id: str):
        self.scenario_id = scenario_id
        super().__init__(scenario_id)


@dataclass(frozen=True)
class GlossarySnapshot:
    scenario_id: str
    scenario_name: str
    glossary: dict
    cascade: bool


# ---------------------------------------------------------------------------
# 纯匹配逻辑：front.TextNormalizer 与 /glossary/test 共用同一份实现
# ---------------------------------------------------------------------------

@lru_cache(maxsize=4096)
def get_term_pattern(term: str):
    """编译术语匹配正则。

    边界只对纯 ASCII 单词型术语生效，且边界字符集必须是 ASCII：
    ``\\w`` 会把汉字算成单词字符，导致「5090显卡」里的 5090 匹配不到。
    """
    if not term:
        return None
    pattern = re.escape(term)
    if term.isascii():
        if re.match(r"[A-Za-z0-9_]", term[0]):
            pattern = r"(?<![A-Za-z0-9_+])" + pattern
        if re.match(r"[A-Za-z0-9_]", term[-1]):
            pattern = pattern + r"(?![A-Za-z0-9_+])"
    return re.compile(pattern, re.IGNORECASE)


def reading_for(value: Any, lang: str):
    """取一条规则在指定语言下的读法；None 表示这条/这一列不参与替换。

    空串和纯空格一律当“没填”。旧实现会把它当成有效读法，替换成空串，
    表现为术语被静默删词。
    """
    if isinstance(value, dict):
        reading = value.get(lang)
    else:
        reading = value
    if reading is None:
        return None
    if not isinstance(reading, str):
        logger.warning("Coercing non-string glossary reading %r to str", reading)
        reading = str(reading)
    if not reading.strip():
        return None
    return reading


def detect_lang(text: str) -> str:
    """判断术语表该走中文还是英文列（与 TextNormalizer.use_chinese 同口径）。"""
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh"
    if not re.search(r"[a-zA-Z]", text):
        return "zh"
    if _EMAIL_RE.match(text):
        return "zh"
    # 早先这里用裸正则判断，A4 paper / win10 build 这类纯英文会命中
    # "A4"/"win1" 被判成中文，术语试读就取错了读法列（主链路的
    # use_chinese 早已按真拼音判断，两处口径必须一致）。
    if has_pinyin_tone(text):
        return "zh"
    return "en"


def apply_glossary(text: str, glossary: dict, lang: str, cascade: bool):
    """按规则替换术语，返回 (处理后文本, 命中的术语列表)。

    级联关闭（默认）：替换结果放进私有区占位符，后续规则不会再改写它。
    级联开启：逐条按长度顺序改写，允许链式结果；每条规则只执行一次。
    所有替换都按字面量传 lambda，读音里的 \\1 / $& 不会引发 re.error。
    """
    matched: list[str] = []
    if not glossary or not text:
        return text, matched
    sorted_terms = sorted((t for t in glossary if t), key=len, reverse=True)
    if not cascade:
        transformed = text
        slots: list[str] = []
        for term in sorted_terms:
            replacement = reading_for(glossary[term], lang)
            pattern = get_term_pattern(term)
            if replacement is None or pattern is None:
                continue
            if not pattern.search(transformed):
                continue
            slots.append(replacement)
            matched.append(term)
            ph = "\ue000%d\ue001" % (len(slots) - 1)
            transformed = pattern.sub(lambda _m, p=ph: p, transformed)
        for i, replacement in enumerate(slots):
            transformed = transformed.replace("\ue000%d\ue001" % i, replacement)
        return transformed, matched

    transformed = text
    for term in sorted_terms:
        replacement = reading_for(glossary[term], lang)
        pattern = get_term_pattern(term)
        if replacement is None or pattern is None:
            continue
        if not pattern.search(transformed):
            continue
        matched.append(term)
        transformed = pattern.sub(lambda _m, r=replacement: r, transformed)
    return transformed, matched


def _glossary_from_rules(rules: Iterable[dict]) -> dict:
    out: dict = {}
    for rule in rules:
        value = {}
        for lang in ("zh", "en"):
            reading = (rule.get(lang) or "").strip()
            if reading:
                value[lang] = reading
        if value:
            out[rule["term"]] = value
    return out


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _text_error(value: str, name: str, max_len: int):
    if not value.strip():
        return f"{name}为空"
    if len(value) > max_len:
        return f"{name}过长（{len(value)} 字，上限 {max_len}）"
    if _CONTROL_RE.search(value):
        return f"{name}含换行或控制字符"
    if _PUA_RE.search(value):
        return f"{name}不能包含不可见私有区字符"
    if _INVISIBLE_RE.search(value):
        return f"{name}含不可见字符"
    return None


def _as_bool(value, default=False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value, default=0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _new_rule_id() -> str:
    return "r_" + uuid.uuid4().hex[:10]


def _new_scenario_id() -> str:
    return "s_" + uuid.uuid4().hex[:8]


def _rule_id_for_term(term: str) -> str:
    return "r_" + hashlib.sha1(term.encode("utf-8")).hexdigest()[:10]


class GlossaryStore:
    """规则库唯一事实源。所有修改方法都自行加锁、原子落盘。"""

    def __init__(self, model_dir: str):
        self.model_dir = model_dir
        self.rules_path = os.path.join(model_dir, RULES_FILENAME)
        self.flat_path = os.path.join(model_dir, FLAT_FILENAME)
        self.settings_path = os.path.join(model_dir, SETTINGS_FILENAME)
        self.rev = 0
        self.rules: list[dict] = []
        self.scenarios: list[dict] = []
        self.cascade = False
        self.active_scenario = DEFAULT_SCENARIO
        self.warnings: list[str] = []
        self.read_only = False
        self._rules_file_existed = os.path.exists(self.rules_path)
        self._lock = threading.RLock()
        self.load()

    # ---- 读取 ------------------------------------------------------------

    @staticmethod
    def _read_yaml(path: str) -> Any:
        with open(path, "r", encoding="utf-8") as f:
            # BaseLoader：全部按字符串读，避免 5090 -> int、yes -> bool
            return yaml.load(f, Loader=yaml.BaseLoader)

    def _warn(self, message: str):
        logger.warning("%s", message)
        self.warnings.append(message)

    def load(self):
        with self._lock:
            self.warnings = []
            self.read_only = False
            if os.path.exists(self.rules_path):
                try:
                    data = self._read_yaml(self.rules_path)
                except Exception as e:
                    self._warn(f"规则文件读取失败，已退回兼容术语表：{e}")
                    data = None
                if isinstance(data, dict) and data.get("format") == FORMAT_V2:
                    try:
                        self._load_v2(data)
                        self._load_settings()
                        self._rebuild_flat_on_startup()
                        return
                    except Exception as e:
                        self._warn(f"规则文件解析失败，已退回兼容术语表：{e}")
                elif isinstance(data, dict) and data:
                    # 未知/未来版本：能认多少认多少，但禁止保存，防止降级覆盖
                    self._warn("规则文件格式版本未知，已按只读模式加载")
                    self.read_only = True
                    try:
                        self._load_v2(data, lenient=True)
                        self._load_settings()
                        return
                    except Exception as e:
                        self._warn(f"未知格式规则文件解析失败，已退回兼容术语表：{e}")
            self._migrate_flat()
            self._load_settings()
            logger.info(
                "Glossary store loaded from legacy flat file: %d rules", len(self.rules)
            )

    def _load_settings(self):
        self.cascade = False
        self.active_scenario = DEFAULT_SCENARIO
        if not os.path.exists(self.settings_path):
            return
        try:
            data = self._read_yaml(self.settings_path)
        except Exception as e:
            self._warn(f"术语设置读取失败，已使用默认值：{e}")
            return
        if not isinstance(data, dict):
            self._warn("术语设置格式异常，已使用默认值")
            return
        self.cascade = _as_bool(data.get("cascade"), False)
        sid = str(data.get("active_scenario") or "")
        if sid and self._find_scenario(sid) is None:
            self._warn(f"当前场景 {sid} 不存在，已回退默认场景")
            sid = DEFAULT_SCENARIO
        self.active_scenario = sid

    def _load_v2(self, data: dict, lenient: bool = False):
        self.rev = max(0, _as_int(data.get("rev"), 0))
        raw_rules = data.get("rules") or []
        if not isinstance(raw_rules, list):
            raise ValueError("rules 不是列表")
        rules: list[dict] = []
        seen_terms: dict[str, str] = {}
        seen_ids: set[str] = set()
        for idx, item in enumerate(raw_rules):
            if not isinstance(item, dict):
                self._warn(f"第 {idx + 1} 条规则格式异常，已跳过")
                continue
            term = item.get("term")
            if not isinstance(term, str) or not term.strip():
                self._warn(f"第 {idx + 1} 条规则术语为空，已跳过")
                continue
            term = term.strip()
            if term.casefold() in seen_terms:
                self._warn(
                    f"术语「{term}」与「{seen_terms[term.casefold()]}」重复，保留先出现的一条"
                )
                continue
            rid = item.get("id")
            if not isinstance(rid, str) or not rid.strip() or rid in seen_ids:
                rid = _rule_id_for_term(term)
                suffix = 1
                while rid in seen_ids:
                    rid = _rule_id_for_term(term) + str(suffix)
                    suffix += 1
                if lenient:
                    self._warn(f"术语「{term}」的 id 无效，已重新生成")
            seen_terms[term.casefold()] = term
            seen_ids.add(rid)
            rules.append({
                "id": rid,
                "term": term,
                "zh": str(item.get("zh") or "").strip(),
                "en": str(item.get("en") or "").strip(),
                "enabled": _as_bool(item.get("enabled"), True),
            })
        raw_scenarios = data.get("scenarios") or []
        if not isinstance(raw_scenarios, list):
            raise ValueError("scenarios 不是列表")
        scenarios: list[dict] = []
        names: set[str] = set()
        ids: set[str] = set()
        rule_ids = {r["id"] for r in rules}
        for idx, item in enumerate(raw_scenarios):
            if not isinstance(item, dict):
                self._warn(f"第 {idx + 1} 个场景格式异常，已跳过")
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                self._warn(f"第 {idx + 1} 个场景名称为空，已跳过")
                continue
            name = self._dedupe_name(name, names)
            sid = str(item.get("id") or "").strip()
            if not sid or sid in ids:
                sid = _new_scenario_id()
                while sid in ids:
                    sid = _new_scenario_id()
            ids.add(sid)
            names.add(name.casefold())
            members: list[str] = []
            for rid in item.get("rule_ids") or []:
                rid = str(rid)
                if rid in rule_ids and rid not in members:
                    members.append(rid)
                else:
                    self._warn(f"场景「{name}」引用了不存在的规则 {rid}，已忽略")
            scenarios.append({"id": sid, "name": name, "rule_ids": members})
        self.rules = rules
        self.scenarios = scenarios

    def _dedupe_name(self, name: str, used: set[str]) -> str:
        base = name[:MAX_SCENARIO_NAME_LEN]
        candidate = base
        n = 2
        while candidate.casefold() in used or candidate in RESERVED_SCENARIO_NAMES:
            suffix = f" ({n})"
            candidate = base[: MAX_SCENARIO_NAME_LEN - len(suffix)] + suffix
            n += 1
        if candidate != name:
            self._warn(f"场景名「{name}」不可用，已改为「{candidate}」")
        return candidate

    def _migrate_flat(self):
        self.rev = 0
        self.rules = []
        self.scenarios = []
        if not os.path.exists(self.flat_path):
            return
        try:
            data = self._read_yaml(self.flat_path)
        except Exception as e:
            self._warn(f"术语表读取失败：{e}")
            return
        if not isinstance(data, dict):
            self._warn("术语表格式异常，已按空库启动")
            return
        seen: dict[str, str] = {}
        for term, value in data.items():
            if not isinstance(term, str) or not term.strip():
                self._warn(f"术语表里有无效术语键 {term!r}，已跳过")
                continue
            term = term.strip()
            if term.casefold() in seen:
                self._warn(
                    f"术语「{term}」与「{seen[term.casefold()]}」仅大小写不同，保留先出现的一条"
                )
                continue
            if isinstance(value, dict):
                zh = str(value.get("zh") or "").strip()
                en = str(value.get("en") or "").strip()
            elif isinstance(value, (list, tuple, set)):
                self._warn(
                    f"术语「{term}」的读法不是文本（{type(value).__name__}），已跳过"
                )
                continue
            elif value is None:
                zh = en = ""
            else:
                zh = en = str(value).strip()
            if not zh and not en:
                self._warn(f"术语「{term}」没有任何读法，已跳过")
                continue
            seen[term.casefold()] = term
            self.rules.append({
                "id": _rule_id_for_term(term),
                "term": term,
                "zh": zh,
                "en": en,
                "enabled": True,
            })
        logger.info("Migrated %d legacy glossary terms", len(self.rules))

    def _rebuild_flat_on_startup(self):
        try:
            text = self._render_flat(self._default_rules())
            current = None
            if os.path.exists(self.flat_path):
                with open(self.flat_path, "r", encoding="utf-8") as f:
                    current = f.read()
            if current != text:
                self._atomic_write(self.flat_path, text)
        except Exception as e:
            # 目录只读等场景：不影响启动和生成，保存时才报错
            self._warn(f"兼容术语表重建失败（不影响生成）：{e}")

    # ---- 生效与查询 ------------------------------------------------------

    def _find_scenario(self, scenario_id: str) -> dict | None:
        for scenario in self.scenarios:
            if scenario["id"] == scenario_id:
                return scenario
        return None

    def _scenario_name(self, scenario_id: str) -> str:
        if not scenario_id:
            return DEFAULT_SCENARIO_NAME
        scenario = self._find_scenario(scenario_id)
        return scenario["name"] if scenario else DEFAULT_SCENARIO_NAME

    def _effective_rules(self, scenario_id: str) -> list[dict]:
        if not scenario_id:
            return [r for r in self.rules if r["enabled"]]
        scenario = self._find_scenario(scenario_id)
        if scenario is None:
            return []
        members = set(scenario["rule_ids"])
        return [r for r in self.rules if r["enabled"] and r["id"] in members]

    def _default_rules(self) -> list[dict]:
        return [r for r in self.rules if r["enabled"]]

    def snapshot(self, scenario_id: str | None = None) -> GlossarySnapshot:
        sid = DEFAULT_SCENARIO if scenario_id is None else scenario_id
        if sid and self._find_scenario(sid) is None:
            raise UnknownScenario(sid)
        with self._lock:
            glossary = _glossary_from_rules(self._effective_rules(sid))
            return GlossarySnapshot(
                scenario_id=sid,
                scenario_name=self._scenario_name(sid),
                glossary=glossary,
                cascade=bool(self.cascade),
            )

    def document(self, scenario_id: str | None = None) -> dict:
        with self._lock:
            sid = self.active_scenario if scenario_id is None else scenario_id
            effective = self._effective_rules(sid)
            doc = {
                "rev": self.rev,
                "cascade": bool(self.cascade),
                "active_scenario": self.active_scenario,
                "rules": copy.deepcopy(self.rules),
                "scenarios": copy.deepcopy(self.scenarios),
                "effective_count": len(effective),
                "warnings": list(self.warnings),
                "glossary": _glossary_from_rules(self._default_rules()),
                "read_only": bool(self.read_only),
            }
            if len(self.rules) > MAX_RULES_SOFT:
                doc["warnings"].append(f"规则较多（{len(self.rules)} 条），保存后预览可能变慢")
            if len(self.scenarios) > MAX_SCENARIOS_SOFT:
                doc["warnings"].append(
                    f"场景较多（{len(self.scenarios)} 个），弹窗列表可能变长"
                )
            return doc

    # ---- 校验与保存 ------------------------------------------------------

    def _validate(self, rules: Any, scenarios: Any):
        errors: list[dict] = []
        if not isinstance(rules, list):
            raise GlossaryValidationError([{"field": "rules", "index": -1, "message": "规则必须是列表"}])
        if not isinstance(scenarios, list):
            raise GlossaryValidationError([{"field": "scenarios", "index": -1, "message": "场景必须是列表"}])
        normalized: list[dict] = []
        seen_ids: set[str] = set()
        seen_terms: dict[str, str] = {}
        for idx, item in enumerate(rules):
            if not isinstance(item, dict):
                errors.append({"field": "rules", "index": idx, "message": f"第 {idx + 1} 行格式异常"})
                continue
            term = item.get("term")
            term = term.strip() if isinstance(term, str) else ""
            err = _text_error(term, "术语", MAX_TERM_LEN)
            if err:
                errors.append({"field": "term", "index": idx, "message": f"第 {idx + 1} 行：{err}"})
                continue
            key = term.casefold()
            if key in seen_terms:
                errors.append({
                    "field": "term", "index": idx,
                    "message": f"第 {idx + 1} 行：「{term}」与「{seen_terms[key]}」重复（忽略大小写）",
                })
                continue
            seen_terms[key] = term
            readings = {}
            for lang, label in (("zh", "中文读法"), ("en", "英文读法")):
                value = item.get(lang)
                value = value.strip() if isinstance(value, str) else ""
                err = _text_error(value, label, MAX_READING_LEN) if value else None
                if err:
                    errors.append({"field": lang, "index": idx, "message": f"第 {idx + 1} 行：{err}"})
                readings[lang] = value
            if not readings["zh"] and not readings["en"]:
                errors.append({
                    "field": "reading", "index": idx,
                    "message": f"第 {idx + 1} 行：至少填一种读法",
                })
                continue
            rid = item.get("id")
            rid = rid.strip() if isinstance(rid, str) else ""
            if rid:
                if len(rid) > 64:
                    errors.append({"field": "id", "index": idx, "message": f"第 {idx + 1} 行：id 过长"})
                    continue
                if rid in seen_ids:
                    errors.append({"field": "id", "index": idx, "message": f"第 {idx + 1} 行：id 重复"})
                    continue
            else:
                rid = _new_rule_id()
                while rid in seen_ids:
                    rid = _new_rule_id()
            seen_ids.add(rid)
            normalized.append({
                "id": rid,
                "term": term,
                "zh": readings["zh"],
                "en": readings["en"],
                "enabled": _as_bool(item.get("enabled"), True),
            })
        normalized_scenarios: list[dict] = []
        seen_sids: set[str] = set()
        seen_names: dict[str, str] = {}
        for idx, item in enumerate(scenarios):
            if not isinstance(item, dict):
                errors.append({"field": "scenarios", "index": idx, "message": f"第 {idx + 1} 个场景格式异常"})
                continue
            name = item.get("name")
            name = name.strip() if isinstance(name, str) else ""
            if not name:
                errors.append({"field": "scenario_name", "index": idx, "message": f"第 {idx + 1} 个场景：名称不能为空"})
                continue
            if len(name) > MAX_SCENARIO_NAME_LEN:
                errors.append({
                    "field": "scenario_name", "index": idx,
                    "message": f"第 {idx + 1} 个场景：名称过长（上限 {MAX_SCENARIO_NAME_LEN} 字）",
                })
                continue
            if name in RESERVED_SCENARIO_NAMES:
                errors.append({
                    "field": "scenario_name", "index": idx,
                    "message": f"第 {idx + 1} 个场景：「{name}」是保留名",
                })
                continue
            if name.casefold() in seen_names:
                errors.append({
                    "field": "scenario_name", "index": idx,
                    "message": f"第 {idx + 1} 个场景：「{name}」与「{seen_names[name.casefold()]}」重名",
                })
                continue
            seen_names[name.casefold()] = name
            sid = item.get("id")
            sid = sid.strip() if isinstance(sid, str) else ""
            if sid:
                if sid == DEFAULT_SCENARIO or sid in seen_sids:
                    errors.append({"field": "scenario_id", "index": idx, "message": f"第 {idx + 1} 个场景：id 重复或非法"})
                    continue
            else:
                sid = _new_scenario_id()
                while sid in seen_sids:
                    sid = _new_scenario_id()
            seen_sids.add(sid)
            members: list[str] = []
            raw_ids = item.get("rule_ids")
            if raw_ids is None:
                raw_ids = []
            if not isinstance(raw_ids, list):
                errors.append({
                    "field": "scenario_rules", "index": idx,
                    "message": f"第 {idx + 1} 个场景：rule_ids 必须是列表",
                })
                continue
            bad = False
            for rid in raw_ids:
                rid = str(rid)
                if rid not in seen_ids:
                    errors.append({
                        "field": "scenario_rules", "index": idx,
                        "message": f"第 {idx + 1} 个场景引用了不存在的规则 {rid}",
                    })
                    bad = True
                    continue
                if rid not in members:
                    members.append(rid)
            if bad:
                continue
            normalized_scenarios.append({"id": sid, "name": name, "rule_ids": members})
        if errors:
            raise GlossaryValidationError(errors)
        return normalized, normalized_scenarios

    @staticmethod
    def _render_v2(rules, scenarios, rev) -> str:
        return yaml.dump(
            {"format": FORMAT_V2, "rev": rev, "rules": rules, "scenarios": scenarios},
            allow_unicode=True, default_flow_style=False, sort_keys=False,
        )

    @staticmethod
    def _render_flat(rules) -> str:
        return yaml.dump(
            _glossary_from_rules(rules),
            allow_unicode=True, default_flow_style=False, sort_keys=False,
        )

    def _render_settings(self, cascade, active_scenario) -> str:
        return yaml.dump(
            {"cascade": bool(cascade), "active_scenario": active_scenario or ""},
            allow_unicode=True, default_flow_style=False, sort_keys=False,
        )

    @staticmethod
    def _atomic_write(path: str, text: str):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)

    @staticmethod
    def _read_text(path: str) -> str | None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def _backup_once(self):
        if self._rules_file_existed or os.path.exists(self.rules_path):
            return
        backup = self.flat_path + BACKUP_SUFFIX
        if os.path.exists(backup) or not os.path.exists(self.flat_path):
            return
        try:
            with open(self.flat_path, "rb") as src, open(backup, "wb") as dst:
                dst.write(src.read())
            logger.info("Glossary backup written: %s", backup)
        except OSError as e:
            raise RuntimeError(f"备份旧术语表失败：{e}") from e

    def _commit(self, rules, scenarios, rev, cascade, active_scenario):
        """按 flat -> settings -> rules 顺序写盘；rules 是提交点。

        任何一步失败都不发布内存状态，并尽力把已写的前置文件回滚。
        """
        self._backup_once()
        flat_text = self._render_flat([r for r in rules if r["enabled"]])
        settings_text = self._render_settings(cascade, active_scenario)
        rules_text = self._render_v2(rules, scenarios, rev)
        old = {
            self.flat_path: self._read_text(self.flat_path),
            self.settings_path: self._read_text(self.settings_path),
            self.rules_path: self._read_text(self.rules_path),
        }
        written: list[str] = []
        try:
            for path, text in (
                (self.flat_path, flat_text),
                (self.settings_path, settings_text),
                (self.rules_path, rules_text),
            ):
                self._atomic_write(path, text)
                written.append(path)
        except Exception:
            for path in reversed(written):
                try:
                    if old[path] is None:
                        os.remove(path)
                    else:
                        self._atomic_write(path, old[path])
                except Exception as rollback_err:
                    logger.error("Glossary rollback failed for %s: %s", path, rollback_err)
            raise

    def save_config(self, rules, scenarios, rev=None, force=False) -> dict:
        with self._lock:
            if self.read_only:
                raise GlossaryReadOnly("规则文件版本未知，已禁止保存")
            normalized_rules, normalized_scenarios = self._validate(rules, scenarios)
            if rev is not None and not force:
                try:
                    expected = int(rev)
                except (TypeError, ValueError):
                    raise GlossaryValidationError([
                        {"field": "rev", "index": -1, "message": "rev 必须是整数"}
                    ])
                if expected != self.rev:
                    raise GlossaryConflict(self.document())
            new_rev = self.rev + 1
            active = self.active_scenario
            if active and not any(s["id"] == active for s in normalized_scenarios):
                active = DEFAULT_SCENARIO
            self._commit(normalized_rules, normalized_scenarios, new_rev, self.cascade, active)
            self.rules = normalized_rules
            self.scenarios = normalized_scenarios
            self.rev = new_rev
            self.active_scenario = active
            self.warnings = []
            self._rules_file_existed = True
            logger.info(
                "Glossary saved: rev=%d rules=%d scenarios=%d",
                new_rev, len(normalized_rules), len(normalized_scenarios),
            )
            return self.document()

    def set_active(self, scenario_id: str) -> dict:
        sid = scenario_id or DEFAULT_SCENARIO
        with self._lock:
            if sid and self._find_scenario(sid) is None:
                raise UnknownScenario(sid)
            old = self._read_text(self.settings_path)
            self._atomic_write(self.settings_path, self._render_settings(self.cascade, sid))
            self.active_scenario = sid
            logger.info("Glossary active scenario -> %s", sid or "default")
            return self.document()

    def set_cascade(self, cascade: bool) -> dict:
        with self._lock:
            self._atomic_write(
                self.settings_path,
                self._render_settings(bool(cascade), self.active_scenario),
            )
            self.cascade = bool(cascade)
            logger.info("Glossary cascade -> %s", self.cascade)
            return self.document()

    # ---- 旧接口映射 ------------------------------------------------------

    def legacy_add(self, term: str, zh: str, en: str) -> dict:
        with self._lock:
            term = (term or "").strip()
            if not term:
                raise GlossaryValidationError([{"field": "term", "index": -1, "message": "请输入术语"}])
            key = term.casefold()
            rules = copy.deepcopy(self.rules)
            scenarios = copy.deepcopy(self.scenarios)
            target = next((r for r in rules if r["term"].casefold() == key), None)
            if target is None:
                target = {"id": _new_rule_id(), "term": term, "zh": "", "en": "", "enabled": True}
                rules.append(target)
            # 空列沿用原值（BUG-004），显式提交的列才覆盖
            if (zh or "").strip():
                target["zh"] = zh.strip()
            if (en or "").strip():
                target["en"] = en.strip()
            if not target["zh"] and not target["en"]:
                raise GlossaryValidationError([
                    {"field": "reading", "index": -1, "message": "请至少输入一种读法"}
                ])
            target["enabled"] = True
            active = self.active_scenario
            if active:
                scenario = next((s for s in scenarios if s["id"] == active), None)
                if scenario is not None and target["id"] not in scenario["rule_ids"]:
                    scenario["rule_ids"].append(target["id"])
            return self.save_config(rules, scenarios, rev=None, force=True)

    def legacy_delete(self, term: str) -> dict:
        with self._lock:
            term = (term or "").strip()
            key = term.casefold()
            rules = copy.deepcopy(self.rules)
            target = next((r for r in rules if r["term"] == term), None)
            if target is None:
                target = next((r for r in rules if r["term"].casefold() == key), None)
            if target is None:
                raise KeyError(term)
            rules = [r for r in rules if r["id"] != target["id"]]
            scenarios = copy.deepcopy(self.scenarios)
            for scenario in scenarios:
                scenario["rule_ids"] = [rid for rid in scenario["rule_ids"] if rid != target["id"]]
            return self.save_config(rules, scenarios, rev=None, force=True)

    def legacy_clear(self) -> dict:
        with self._lock:
            scenarios = copy.deepcopy(self.scenarios)
            for scenario in scenarios:
                scenario["rule_ids"] = []
            return self.save_config([], scenarios, rev=None, force=True)

    # ---- 导入导出 --------------------------------------------------------

    def export_text(self, fmt: str = "yaml") -> str:
        with self._lock:
            data = {
                "format": FORMAT_V2,
                "rev": self.rev,
                "rules": copy.deepcopy(self.rules),
                "scenarios": copy.deepcopy(self.scenarios),
            }
        if fmt == "json":
            return json.dumps(data, ensure_ascii=False, indent=2)
        return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)

    def import_data(self, data: Any) -> dict:
        """合并导入。规则按术语、场景按名称合并；不删除现有内容。

        导入内容先并入草稿，再走一次完整 save_config：任一条非法则整体拒绝，
        不会留下半套数据。同一文件重复导入结果一致（规则按术语去重）。
        """
        with self._lock:
            if self.read_only:
                raise GlossaryReadOnly("规则文件版本未知，已禁止导入")
            if not isinstance(data, dict):
                raise GlossaryValidationError([
                    {"field": "file", "index": -1, "message": "导入文件必须是映射/对象"}
                ])
            imported_rules: list[dict] = []
            raw_scenarios: list = []
            if isinstance(data.get("rules"), list):
                for idx, item in enumerate(data["rules"]):
                    if not isinstance(item, dict):
                        self._warn(f"导入文件第 {idx + 1} 条规则格式异常，已跳过")
                        continue
                    term = str(item.get("term") or "").strip()
                    if not term:
                        self._warn(f"导入文件第 {idx + 1} 条规则术语为空，已跳过")
                        continue
                    imported_rules.append({
                        "id": str(item.get("id") or "").strip(),
                        "term": term,
                        "zh": str(item.get("zh") or "").strip(),
                        "en": str(item.get("en") or "").strip(),
                        "enabled": item.get("enabled", True),
                    })
                if isinstance(data.get("scenarios"), list):
                    raw_scenarios = data["scenarios"]
            else:
                for term, value in data.items():
                    if term in ("format", "rev", "rules", "scenarios"):
                        continue
                    term = str(term).strip()
                    if not term:
                        continue
                    if isinstance(value, dict):
                        zh = str(value.get("zh") or "").strip()
                        en = str(value.get("en") or "").strip()
                    elif value is None:
                        zh = en = ""
                    else:
                        zh = en = str(value).strip()
                    imported_rules.append({
                        "id": "", "term": term, "zh": zh, "en": en, "enabled": True,
                    })
            rules = copy.deepcopy(self.rules)
            by_term = {r["term"].casefold(): r for r in rules}
            id_map: dict[str, str] = {}
            added = updated = unchanged = 0
            for item in imported_rules:
                term = item["term"]
                existing = by_term.get(term.casefold())
                if existing is None:
                    new_rule = {
                        "id": _new_rule_id(),
                        "term": term,
                        "zh": item["zh"],
                        "en": item["en"],
                        "enabled": _as_bool(item["enabled"], True),
                    }
                    rules.append(new_rule)
                    by_term[term.casefold()] = new_rule
                    added += 1
                    if item["id"]:
                        id_map[item["id"]] = new_rule["id"]
                    continue
                changed = False
                if item["zh"] and item["zh"] != existing["zh"]:
                    existing["zh"] = item["zh"]
                    changed = True
                if item["en"] and item["en"] != existing["en"]:
                    existing["en"] = item["en"]
                    changed = True
                if changed:
                    updated += 1
                else:
                    unchanged += 1
                if item["id"]:
                    id_map[item["id"]] = existing["id"]
            scenarios = copy.deepcopy(self.scenarios)
            by_name = {s["name"].casefold(): s for s in scenarios}
            known_ids = {r["id"] for r in rules}
            scenarios_added = 0
            for item in raw_scenarios:
                if not isinstance(item, dict):
                    self._warn("导入文件里有格式异常的场景，已跳过")
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    self._warn("导入文件里有空名场景，已跳过")
                    continue
                scenario = by_name.get(name.casefold())
                if scenario is None:
                    scenario = {"id": _new_scenario_id(), "name": name, "rule_ids": []}
                    scenarios.append(scenario)
                    by_name[name.casefold()] = scenario
                    scenarios_added += 1
                for rid in item.get("rule_ids") or []:
                    mapped = id_map.get(str(rid), str(rid))
                    if mapped not in known_ids:
                        self._warn(f"导入场景「{name}」引用了不存在的规则 {rid}，已忽略")
                        continue
                    if mapped not in scenario["rule_ids"]:
                        scenario["rule_ids"].append(mapped)
            doc = self.save_config(rules, scenarios, rev=None, force=True)
            logger.info(
                "Glossary imported: added=%d updated=%d unchanged=%d scenarios_added=%d",
                added, updated, unchanged, scenarios_added,
            )
            return {
                "added": added,
                "updated": updated,
                "unchanged": unchanged,
                "scenarios_added": scenarios_added,
                "warnings": doc["warnings"],
            }

        normalized_rules, normalized_scenarios = self._validate(rules, scenarios)
    # ---- 试读 ------------------------------------------------------------

    def test(self, text: str, rules, scenarios, scenario_id: str | None, cascade: bool):
        normalized_rules, normalized_scenarios = self._validate(rules, scenarios)
        sid = DEFAULT_SCENARIO if scenario_id is None else scenario_id
        if sid and not any(s["id"] == sid for s in normalized_scenarios):
            raise UnknownScenario(sid)
        if sid:
            members = set(next(s["rule_ids"] for s in normalized_scenarios if s["id"] == sid))
            effective = [r for r in normalized_rules if r["enabled"] and r["id"] in members]
        else:
            effective = [r for r in normalized_rules if r["enabled"]]
        glossary = _glossary_from_rules(effective)
        lang = detect_lang(text)
        result, matched = apply_glossary(text, glossary, lang, bool(cascade))
        by_term = {r["term"]: r for r in effective}
        matched_info = [
            {
                "id": by_term[t]["id"],
                "term": t,
                "reading": by_term[t].get(lang, ""),
            }
            for t in matched
        ]
        return {
            "normalized_text": result,
            "lang": lang,
            "matched": matched_info,
        }
