"""Explicit governance question constraints, not a general intent classifier."""
from dataclasses import asdict, dataclass, replace
import re

FULL_SECTIONS = frozenset({"scores", "dispositions", "freshness", "time_splits", "limitations"})
DIMENSION_WORDS = {
    "Accurate": r"准确性|\baccurate\b",
    "Complete": r"完整性|\bcomplete(?:ness)?\b",
    "Unique": r"唯一性|\bunique(?:ness)?\b",
    "Consistent": r"一致性|\bconsisten(?:t|cy)\b",
    "Up-to-date": r"时效|新鲜|up.to.date|freshness",
}
TABLE_WORDS = {"ratings": r"评分|ratings?", "movies": r"电影|movies?", "users": r"用户|users?"}


def positive_clauses(question):
    # Handle explicit exclusions at clause boundaries. Ambiguous language stays
    # with the model; this intentionally does not infer arbitrary negation.
    clauses = re.split(r"[，。；!?！？;\n]|(?:但是|但)", question)
    negative = r"^\s*(?:请|也|并且|并|且|本次|这次)?\s*(?:不要|不需要|无需|不用|不必|别|而不是|do not|don't|without|no need)"
    return "，".join(c for c in clauses if not re.search(negative, c, re.I))


class ClarificationNeeded(ValueError):
    """The request has multiple plausible scopes; do not silently choose one."""


EN_NUMBERS = dict(zip("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split(), range(21)))
CN_DIGITS = dict(zip("零一二三四五六七八九", range(10))) | {"两": 2, "〇": 0}
NUMBER = r"(?:-?\d+(?:\.\d+)?|负?[零〇一二两三四五六七八九十百千万]+|" + "|".join(EN_NUMBERS) + r")"
SAMPLE_WORDS = r"样例|示例|例子|\bsamples?\b|\bexamples?\b"
FOLLOWUP = r"再(?:给|来|看|读|展示|显示)|换(?:一|个|另)|下一(?:个|条|批)|另外|另一个|\b(?:another|next|more)\b"


def numeral(value):
    value = value.lower()
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if value in EN_NUMBERS:
        return EN_NUMBERS[value]
    total, digit, last_unit = 0, None, 100000
    for char in value:
        if char in CN_DIGITS:
            if digit not in (None, 0):
                raise ClarificationNeeded("数量表达不明确，请使用明确整数，例如 11 条或第 11 条。")
            digit = CN_DIGITS[char]
        elif char in "十百千万":
            unit = {"十": 10, "百": 100, "千": 1000, "万": 10000}[char]
            if unit >= last_unit or digit is None and unit != 10:
                raise ClarificationNeeded("数量表达不明确，请使用阿拉伯数字。")
            total += (1 if digit is None else digit) * unit
            digit, last_unit = None, unit
        else:
            raise ClarificationNeeded("样例数量和位置须为明确整数。")
    return total + (digit or 0)


def quantity(text):
    if re.search(NUMBER + r"\s*(?:到|至|~|—|-)\s*" + NUMBER + r"\s*(?:条|个|例)", text, re.I):
        raise ClarificationNeeded("请指定一个样例数量或序号，不要使用数量范围。")
    if re.search(r"(?:几|若干|两三|十几|几十)\s*(?:条|个|例)", text):
        raise ClarificationNeeded("请指定需要几条样例，或一个明确的序号。")
    ordinal = re.search(r"第(" + NUMBER + r")\s*(?:个|条|例)", text, re.I)
    amount = re.search(r"(?<![第\d.零〇一二两三四五六七八九十百千万负-])(" + NUMBER + r")\s*(?:个|条|例)", text, re.I)
    english = re.search(r"\b(" + NUMBER + r")\s+(?:(?:real|source|ratings?|users?|movies?)\s+)?(?:samples?|examples?)\b", text, re.I)
    count = 1 if ordinal or not (amount or english) else numeral((amount or english)[1])
    offset = numeral(ordinal[1]) - 1 if ordinal else 0
    if not 1 <= count <= 20 or not 0 <= offset <= 10000:
        raise ValueError("样例请求须为 1—20 条，起始位置不能超过 10001。")
    return count, offset, bool(ordinal or amount or english), bool(ordinal)


@dataclass(frozen=True)
class SampleRequest:
    mode: str
    count: int = 1
    offset: int = 0
    reason: str | None = None
    table: str | None = None
    file_name: str | None = None

    def arguments(self, ref):
        return {"artifact_ref": ref, "mode": self.mode, "limit": self.count, "offset": self.offset,
                **({"reason": self.reason} if self.reason else {}),
                **({"table": self.table} if self.table and self.mode == "examples" else {}),
                **({"file_name": self.file_name} if self.file_name else {})}


@dataclass(frozen=True)
class AnswerRequirements:
    full: bool
    brief: bool
    required_sections: tuple[str, ...]
    dimensions: tuple[str, ...]
    samples: tuple[SampleRequest, ...]
    max_sections: int
    max_characters: int | None
    require_unsupported: bool = False
    continuation_of: str | None = None

    @classmethod
    def from_dict(cls, value):
        return cls(**{**value, "required_sections": tuple(value["required_sections"]),
                      "dimensions": tuple(value["dimensions"]),
                      "samples": tuple(SampleRequest(**item) for item in value["samples"])})

    def as_dict(self):
        return {**asdict(self), "required_sections": list(self.required_sections),
                "dimensions": list(self.dimensions), "samples": [asdict(s) for s in self.samples]}


def sample_targets(text, cleaned=False):
    rules = re.findall(r"\b(users|movies|ratings)/(R\d{2}_[A-Z0-9_]+|[A-Z][A-Z0-9_]+)\b", text, re.I)
    if rules:
        return [SampleRequest("examples", reason=t.lower() + "/" + r.upper(), table=t.lower()) for t, r in dict.fromkeys(rules)]
    files = re.findall(r"\b(users|movies|ratings)\.jsonl\b", text, re.I)
    tables = list(dict.fromkeys(t.lower() for t in files)) or [t for t, pattern in TABLE_WORDS.items() if re.search(pattern, text, re.I)]
    if cleaned or files or re.search(r"清洗后|清洗数据|cleaned", text, re.I):
        return [SampleRequest("sample", file_name=t + ".jsonl", table=t) for t in tables]
    rule = "R15_DUPLICATE" if re.search(r"去重|重复|duplicate", text, re.I) else "R14_VALID_KEY_CONFLICT" if re.search(r"冲突|conflict", text, re.I) else None
    return [SampleRequest("examples", reason=t + "/" + rule if rule else None, table=t) for t in tables]


def sample_requests(text, previous=None):
    following = bool(re.search(FOLLOWUP, text, re.I))
    requested = bool(re.search(SAMPLE_WORDS, text, re.I))
    if not requested and not (following and re.search(r"(?:个|条|another|next|more)", text, re.I)):
        return (), None
    # Split explicit targets before binding their own quantities. Shared counts
    # require 'each/各'; a single count cannot silently be copied to every target.
    parts = re.split(r"[，。；;、\n]|(?:以及|并且|还有|和|与|\band\b)", text, flags=re.I)
    cleaned = bool(re.search(r"清洗后|清洗数据|cleaned", text, re.I))
    scoped = [(part, sample_targets(part, cleaned)) for part in parts]
    scoped = [(part, targets) for part, targets in scoped if targets and not (
        re.search(r"解释|说明|统计|多少|为什么|为何|比例|损失|变化|\bwhy\b|\bpercent\b", part, re.I)
        and not re.search(SAMPLE_WORDS + r"|/R\d|\.jsonl", part, re.I))]
    if not scoped:
        scoped = [(text, [SampleRequest("sample", file_name="ratings.jsonl", table="ratings") if cleaned else SampleRequest("examples")])]
    distributed = bool(re.search(r"各|每(?:种|类|张表)|\beach\b", text, re.I))
    shared = quantity(text) if distributed else None
    unbound = [quantity(part) for part in parts if re.search(SAMPLE_WORDS, part, re.I)
               and not sample_targets(part, cleaned) and quantity(part)[2]] if scoped[0][0] != text else []
    if len(unbound) > 1:
        raise ClarificationNeeded("多个数量没有明确对应的样例来源，请分别说明。")
    parsed = []
    for part, targets in scoped:
        count, offset, explicit, ordinal = quantity(part)
        if distributed and not explicit:
            count, offset = shared[:2]
        if len(targets) > 1 and explicit and not distributed:
            raise ClarificationNeeded("有多个样例来源，请分别指定数量，或说明每种来源各需要几条。")
        for target in targets:
            parsed.append((replace(target, count=count, offset=offset), explicit, ordinal))
    if unbound:
        if any(item[1] for item in parsed) or len(parsed) > 1 and not distributed:
            raise ClarificationNeeded("总量与各来源的数量分配不明确，请分别说明。")
        parsed = [(replace(sample, count=unbound[0][0], offset=unbound[0][1]), True, unbound[0][3])
                  for sample, _, _ in parsed]
    if len(parsed) > 1 and not distributed and any(p[1] for p in parsed) and not all(p[1] for p in parsed):
        raise ClarificationNeeded("多个样例来源的数量分配不明确，请分别指定，或使用“各几条”。")
    samples, continuation = [], None
    prior = (previous or {}).get("requirements", {}).get("samples", [])
    checks = (previous or {}).get("sample_checks", [])
    for sample, explicit, ordinal in parsed:
        specified = bool(sample.reason or sample.table or sample.file_name)
        if following and not ordinal:
            candidates = [(old, check) for old, check in zip(prior, checks)
                          if (not sample.reason or old.get("reason") == sample.reason)
                          and (not sample.table or old.get("table") == sample.table)
                          and (not sample.file_name or old.get("file_name") == sample.file_name)]
            if len(candidates) == 1:
                old, check = candidates[0]
                if not check.get("count"):
                    raise ClarificationNeeded("上一页已经没有样例，请指定其他规则、文件或位置。")
                sample = replace(SampleRequest(**old), count=sample.count,
                                 offset=old.get("offset", 0) + check["count"])
                if sample.offset > 10000:
                    raise ValueError("后续样例位置超过工具支持的范围。")
                continuation = previous["message_id"]
            elif not specified or len(candidates) > 1 or re.search(r"下一(?:个|条|批)|换|\bnext\b", text, re.I):
                raise ClarificationNeeded("请指定要继续查看的规则或文件；当前没有唯一的前序样例来源。")
        samples.append(sample)
    if len(set(samples)) != len(samples):
        raise ClarificationNeeded("重复的样例目标不明确，请合并数量或分别指定位置。")
    return tuple(samples), continuation


def question_requirements(question, full=False, previous=None):
    text = positive_clauses(question)
    has = lambda pattern: bool(re.search(pattern, text, re.I))
    brief = has(r"简短|简洁|简要|精简|一句话|\bbrief(?:ly)?\b|\bconcise(?:ly)?\b|short answer")
    full = full or has(r"完整(?:解释|报告|概述|结果)|全面|全部(?:结果|维度)|五维|五个维度|all (?:results|dimensions)|full (?:report|explanation)")
    dimensions = tuple(k for k, pattern in DIMENSION_WORDS.items() if has(pattern))
    samples, continuation = sample_requests(text, previous)
    sample_requested = bool(samples)
    topics = set()
    if "Up-to-date" in dimensions:
        topics.add("freshness")
    general_scores = has(r"分数|得分") and not dimensions
    if any(d != "Up-to-date" for d in dimensions) or general_scores or has(r"五维|四项|五个维度|满分|质量(?:得分|评分)|quality scores?"):
        topics.add("scores")
    if has(r"\b(?:T1|T2|train|validation|test)\b|训练|验证期|测试期|分区|时间边界|时间划分"):
        topics.add("time_splits")
    if has(r"真实性|事实.*真实|数据.*真实|(?:证明|保证|核验).*真实|准确率|保证|局限|不能核验|无法核验|未能核验|未.*解决|偏差|limitations?|truth|guarantee|prove") or (has(r"真实") and not sample_requested):
        topics.add("limitations")
    if ("scores" in topics and has(r"为什么|为何|原因|why|怎么|how|计算|公式|真实")) or has(r"评分方法|评价方法|约束代理|评分口径"):
        topics.add("metric_method")
    statistics = not sample_requested or has(r"比例|多少|总和|数量|统计|变化|损失|减少|保留率|为什么|为何|percent|count|why")
    if statistics and has(r"父表|R12_|parent|外键|关联引用"):
        topics.add("parent_references")
    if statistics and has(r"(?:评分|ratings).*(?:减少|损失|隔离|保留率|去重)|隔离比例|数据损失|rating.loss"):
        topics.add("rating_loss")
    if has(r"三表|数据处置|各表.*(?:去向|数量|处置)"):
        topics.add("dispositions")
    if not samples and has(r"版本|version"):
        topics.add("versions")
    if not topics and not samples and has(r"清洗(?:依据|规则)|异常原因|规则依据"):
        topics.add("reasons")
    if has(r"五维|四项|五个维度|全部维度|all dimensions"):
        dimensions = ()
    if full:
        topics.update(FULL_SECTIONS)
        dimensions = ()
    required = len(topics) + len(samples)
    max_sections = max(4, required) if brief else 16 if full or not question else max(6, required)
    max_characters = max(800, len(topics) * 160 + sum(s.count for s in samples) * 400) if brief else None
    require_unsupported = has(r"(?:证明|保证|承诺|核验).*(?:真实|准确率|分类效果|预测效果|下游效果)|(?:prove|guarantee).*(?:true|truth|accuracy|performance)")
    return AnswerRequirements(full, brief, tuple(sorted(topics)), dimensions, tuple(samples), max_sections, max_characters, require_unsupported, continuation)


def explicit_governance_run(question):
    """Recognize explicit new-run commands; exclusions and questions stay queries."""
    text = positive_clauses(question).strip()
    if re.match(r"^(?:请)?(?:解释|说明|查看|查询|为什么|为何)", text):
        return False
    return bool(re.search(
        r"(?:重新|再次)(?:执行)?清洗|重跑(?:一次)?(?:任务|治理|清洗)|再跑(?:一次)?(?:治理|清洗)|"
        r"(?:开始|执行|启动|发起)(?:一次|数据)?(?:清洗|治理)|"
        r"^(?:请(?:你)?(?:帮我)?)?(?:(?:使用|用)(?:已登记|默认)?(?:规则|配置))?清洗(?!后|结果|规则|依据)", text))
