"""Explicit governance question constraints, not a general intent classifier."""
from dataclasses import asdict, dataclass
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


def numeral(value):
    names = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
             "七": 7, "八": 8, "九": 9, "十": 10, "二十": 20,
             "one": 1, "two": 2, "three": 3}
    return int(value) if value.isdigit() else names.get(value.lower(), 1)


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

    def as_dict(self):
        return {**asdict(self), "required_sections": list(self.required_sections),
                "dimensions": list(self.dimensions), "samples": [asdict(s) for s in self.samples]}


def question_requirements(question, full=False):
    text = positive_clauses(question)
    has = lambda pattern: bool(re.search(pattern, text, re.I))
    brief = has(r"简短|简洁|简要|精简|一句话|\bbrief(?:ly)?\b|\bconcise(?:ly)?\b|short answer")
    full = full or has(r"完整(?:解释|报告|概述|结果)|全面|全部(?:结果|维度)|五维|五个维度|all (?:results|dimensions)|full (?:report|explanation)")
    dimensions = tuple(k for k, pattern in DIMENSION_WORDS.items() if has(pattern))
    sample_requested = has(r"样例|示例|例子|\bsamples?\b|\bexamples?\b")
    samples = []
    if sample_requested:
        count, offset = 1, 0
        ordinal = re.search(r"第(\d+|二十|[一二两三四五六七八九十])(?:个|条|例)", text)
        quantity = re.search(r"(?<![第\d])(\d+|二十|[一二两三四五六七八九十])\s*(?:个|条|例)", text)
        english = re.search(r"\b(\d+|one|two|three)\s+(?:(?:real|source)\s+)?(?:samples?|examples?)\b", text, re.I)
        if ordinal:
            offset = numeral(ordinal[1]) - 1
        elif quantity or english:
            count = numeral((quantity or english)[1])
        if not 1 <= count <= 20 or not 0 <= offset <= 10000:
            raise ValueError("样例请求须为 1—20 条，起始位置不能超过 10001。")
        rules = re.findall(r"\b(users|movies|ratings)/(R\d{2}_[A-Z0-9_]+|[A-Z][A-Z0-9_]+)\b", text, re.I)
        tables = [table for table, pattern in TABLE_WORDS.items() if has(pattern)]
        table = tables[0] if len(tables) == 1 else None
        if rules:
            samples = [SampleRequest("examples", count, offset, t.lower() + "/" + r.upper(), t.lower())
                       for t, r in dict.fromkeys(rules)]
        elif has(r"清洗后|清洗数据|cleaned|\.jsonl"):
            files = re.findall(r"\b(users|movies|ratings)\.jsonl\b", text, re.I)
            files = list(dict.fromkeys(f.lower() for f in files)) or tables or ["ratings"]
            samples = [SampleRequest("sample", count, offset, file_name=t + ".jsonl", table=t) for t in files]
        else:
            rule = "R15_DUPLICATE" if has(r"去重|重复|duplicate") else "R14_VALID_KEY_CONFLICT" if has(r"冲突|conflict") else None
            samples = [SampleRequest("examples", count, offset, table + "/" + rule if table and rule else None, table)]
    topics = set()
    if "Up-to-date" in dimensions:
        topics.add("freshness")
    if any(d != "Up-to-date" for d in dimensions) or has(r"五维|四项|五个维度|满分|质量(?:得分|评分)|quality scores?"):
        topics.add("scores")
    if has(r"\b(?:T1|T2|train|validation|test)\b|训练|验证期|测试期|分区|时间边界|时间划分"):
        topics.add("time_splits")
    if has(r"真实|准确率|保证|局限|不能核验|无法核验|未能核验|未.*解决|偏差|limitations?|truth|guarantee|prove"):
        topics.add("limitations")
    if ("scores" in topics and has(r"为什么|为何|原因|why|怎么|how|计算|公式|真实")) or has(r"评分方法|评价方法|约束代理|评分口径"):
        topics.add("metric_method")
    statistics = not sample_requested or has(r"比例|多少|总和|数量|统计|变化|保留率|为什么|为何|percent|count|why")
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
    return AnswerRequirements(full, brief, tuple(sorted(topics)), dimensions, tuple(samples), max_sections, max_characters)


def explicit_governance_run(question):
    """Recognize explicit new-run commands; exclusions and questions stay queries."""
    text = positive_clauses(question).strip()
    if re.match(r"^(?:请)?(?:解释|说明|查看|查询|为什么|为何)", text):
        return False
    return bool(re.search(
        r"(?:重新|再次)(?:执行)?清洗|重跑(?:一次)?(?:任务|治理|清洗)|再跑(?:一次)?(?:治理|清洗)|"
        r"(?:开始|执行|启动|发起)(?:一次|数据)?(?:清洗|治理)|"
        r"^(?:请(?:你)?(?:帮我)?)?(?:(?:使用|用)(?:已登记|默认)?(?:规则|配置))?清洗(?!后|结果|规则|依据)", text))
