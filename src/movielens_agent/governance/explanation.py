"""Deterministic reading aids for supported, published governance reports.

The model chooses relevant sections; it never supplies their numbers or prose.
Scores remain the Hadoop results. Only display deltas and ratios are derived.
"""
from .config import GovernanceConfig
from .report import utc

TABLES = ("users", "movies", "ratings")
DIMENSIONS = ("Accurate", "Complete", "Unique", "Consistent", "Up-to-date")
DISPOSITIONS = ("kept", "repaired", "deduplicated", "quarantined")


def number(value):
    return f"{value:,}"


def score(value):
    return "不可评价" if value is None else f"{value:.6f}"


def percentage(numerator, denominator):
    return "不可评价（分母为 0）" if not denominator else f"{100 * numerator / denominator:.6f}%"


def explanation_sections(report):
    """Only v1 semantics are supported; never apply current rules to old scores."""
    config = GovernanceConfig.model_validate(report["configuration"])
    if report.get("schema_version") != "1" or config.refs() != report["config_refs"]:
        raise ValueError("Unsupported report or configuration reference mismatch.")
    before, after = report["before"], report["after"]
    sections = {}
    lines = ["五维评分（0—100 分；变化为百分点）："]
    for dimension in DIMENSIONS:
        a, b = before["overall"][dimension]["score"], after["overall"][dimension]["score"]
        delta = b - a if a is not None and b is not None else None
        lines.append(f"{dimension}：{score(a)} → {score(b)}；变化 {score(delta)}。")
    lines.append("四项约束代理分数上升不证明原始事实真实；分母会随隔离和去重改变。没有另算综合分。")
    sections["scores"] = "\n".join(lines)

    lines = ["三表最终处置（每条输入只有一个最终去向）："]
    for table in TABLES:
        counts = after["dispositions"][table]
        kept, repaired, deduplicated, quarantined = (counts.get(k, 0) for k in DISPOSITIONS)
        initial, final = before["tables"][table]["rows"], after["tables"][table]["rows"]
        if initial != kept + repaired + deduplicated + quarantined or final != kept + repaired:
            raise ValueError("Report disposition conservation failed.")
        lines.append(f"{table}：输入 {number(initial)}，输出 {number(final)}；"
                     f"保留 {number(kept)}、修复后输出 {number(repaired)}、去重 {number(deduplicated)}、隔离 {number(quarantined)}。")
    lines.append("输入 = 保留 + 修复 + 去重 + 隔离；输出 = 保留 + 修复。隔离和去重不能算作修复。")
    sections["dispositions"] = "\n".join(lines)

    initial, final = before["tables"]["ratings"]["rows"], after["tables"]["ratings"]["rows"]
    d = after["dispositions"]["ratings"]
    sections["rating_loss"] = (
        f"评分输入 {number(initial)} 行，输出 {number(final)} 行，保留率为 {percentage(final, initial)}。"
        f"隔离 {number(d.get('quarantined', 0))} 行，占输入的 {percentage(d.get('quarantined', 0), initial)}；"
        f"去重 {number(d.get('deduplicated', 0))} 行，修复后输出 {number(d.get('repaired', 0))} 行。"
        "这些处置分别计数；输出变少不能统一描述为去重或修复。")

    parents = {key: count for key, count in after["reasons"]["ratings"].items() if key.startswith("R12_")}
    sections["parent_references"] = "\n".join([
        "评分父表引用问题（R12）：",
        *[f"{key}：{number(count)} 次。" for key, count in sorted(parents.items())],
        f"合计 {number(sum(parents.values()))} 次原因命中。标签可重叠，这不是去重后的受影响行数；不能与隔离行数相加。",
        "PARENT_MISSING 表示原始父表缺少该键；PARENT_REMOVED 表示该键在父表治理后未保留。",
    ])

    metric = config.metrics
    lines = [f"时效性使用固定历史参照 {utc(metric.reference_time)}，回看 {number(metric.window_seconds)} 秒。",
             f"新鲜窗口为 [{utc(metric.reference_time - metric.window_seconds)}, {utc(metric.reference_time)}]，两端均包含。"]
    for label, phase in (("清洗前", before), ("清洗后", after)):
        value = phase["tables"]["ratings"]["metrics"]["Up-to-date"]
        passed, population, points = value["passed"], value["population"], value["score"]
        if population and points is not None:
            lines.append(f"{label}：{number(passed)} / {number(population)} × 100 = {score(points)} 分"
                         f"（窗口内占比 {score(points)}%）。")
        else:
            lines.append(f"{label}：{number(passed)} / {number(population)}；不可评价，不以 0 分或满分代替。")
    lines.append("只有评分表参与时效性；另外两表不适用。治理不会让历史事件变新，也不能为提高分数改写时间或评价窗口。")
    sections["freshness"] = "\n".join(lines)

    split = config.split
    counts = after["splits"]
    if sum(counts.values()) != final:
        raise ValueError("Report split conservation failed.")
    storage = [line for line in report["limitations"] if "时间分区" in line]
    sections["time_splits"] = "\n".join([
        f"T1 = {split.train_end}（{utc(split.train_end)}）；T2 = {split.validation_end}（{utc(split.validation_end)}）。",
        f"训练 t ≤ T1：{number(counts['train'])} 行；验证 T1 < t ≤ T2：{number(counts['validation'])} 行；"
        f"测试 t > T2：{number(counts['test'])} 行。",
        *(storage if storage else ["报告未声明分区文件状态，不能确认已有独立分区文件。"]),
        "统计量和特征变换只能在训练分区拟合；验证用于选参，测试用于最终评价，不能先用全量评分拟合。",
    ])
    sections["metric_method"] = (
        "Accurate 是值约束通过数 / 应检查约束数；Complete 是完整必需槽位 / 期望槽位；"
        "Unique 是不同合法业务键 / 全部行；Consistent 是结构、值域、时间、同键及跨表约束全部通过的行 / 全部行。"
        "四项先分表计算再等权汇总，任一必需表为空则汇总不可评价。"
        "前后使用同一配置；隔离无效或无法判定的记录、去掉重复和规范化可修复值，会改变被评价的数据及分母。"
        "满分只表明保留数据通过已实现约束，不证明人口属性、标题等事实真实，也不证明没有选择偏差。")
    sections["reasons"] = "\n".join([
        "各表原因命中（可重叠，不是互斥处置量）：",
        *[f"{table} / {key}：{number(count)} 次。" for table in TABLES
          for key, count in sorted(after["reasons"][table].items())],
        "R14 是合法同键候选之间仍存在冲突，无法判定者隔离；R15 是规范化后完全相同记录的去重。",
    ])
    sections["warnings"] = "\n".join([
        "未自动判定的警告（不等同于隔离原因）：",
        *[f"{table} / {key}：{number(count)} 次。" for table in TABLES
          for key, count in sorted(after["warnings"][table].items())],
        "邮编格式、标题年份、疑似混合编码只记录警告；没有猜测补全原始事实。",
    ])
    if "changes" in after:
        sections["repairs"] = "\n".join([
            "规范化操作历史（可能与最终隔离或去重重叠）：",
            *[f"{table} / {key}：{number(count)} 次。" for table in TABLES
              for key, count in sorted(after["changes"][table].items())],
            "操作次数不等于修复后输出行数；最终 repaired 数量见三表处置。",
        ])
    sections["limitations"] = "\n".join(["已发布报告的局限：", *report["limitations"]])
    refs = {"原始数据": report["input_ref"], "清洗数据": report["cleaned_ref"], **report["config_refs"]}
    sections["versions"] = "\n".join([f"{key}：{value['artifact_id']}@{value['version']}" for key, value in refs.items()])
    return sections


def answer_sections(report, *, brief=False, dimensions=()):
    """Project verified facts for a question; never replace stored metrics."""
    sections = explanation_sections(report)
    if dimensions:
        selected = set(dimensions)
        lines = ["所问维度评分（0—100 分）："]
        for dimension in DIMENSIONS:
            if dimension in selected:
                a, b = (report[phase]["overall"][dimension]["score"] for phase in ("before", "after"))
                lines.append(f"{dimension}：{score(a)} → {score(b)}。")
        lines.append("得分衡量已实现约束，不能证明事实真实。")
        sections["scores"] = "\n".join(lines)
        formulas = {
            "Accurate": "值约束通过数 / 应检查约束数",
            "Complete": "完整必需槽位 / 期望槽位",
            "Unique": "不同合法业务键 / 全部行",
            "Consistent": "全部结构、值域、时间、同键及跨表约束通过的行 / 全部行",
        }
        methods = [f"{d} = {formulas[d]} × 100" for d in DIMENSIONS if d in selected and d in formulas]
        if methods:
            sections["metric_method"] = "；".join(methods) + "。先分表计算再等权汇总，空必需表不可评价；前后口径一致，隔离/去重改变分母。"
    if not brief:
        return sections
    if not dimensions:
        sections["scores"] = "评分（0—100 分）：" + "；".join(
            f"{d} {score(report['before']['overall'][d]['score'])} → {score(report['after']['overall'][d]['score'])}"
            for d in DIMENSIONS) + "。"
    if not dimensions:
        sections["metric_method"] = (
            "前后使用同一套约束公式。清洗通过隔离非法/冲突记录、去重及确定性修复改变评价对象和分母；"
            "四项满分只表示保留数据通过这些约束。")
    sections["limitations"] = "已发布报告的局限：" + "；".join(report["limitations"][:2])
    # Preserve the remaining report caveats in a compact projection, including
    # uncertainty and storage state rather than asserting unsupported guarantees.
    if len(report["limitations"]) > 2:
        sections["limitations"] += "；历史参照、不可核验属性及时间分区限制仍以报告为准。"
    config = GovernanceConfig.model_validate(report["configuration"])
    metric = config.metrics
    lines = [f"时效性历史窗口 [{utc(metric.reference_time - metric.window_seconds)}, {utc(metric.reference_time)}]（两端包含）。"]
    for label, phase in (("前", "before"), ("后", "after")):
        value = report[phase]["tables"]["ratings"]["metrics"]["Up-to-date"]
        if value["population"] and value["score"] is not None:
            lines.append(f"{label}：{number(value['passed'])} / {number(value['population'])} × 100 = {score(value['score'])} 分（{score(value['score'])}%）。")
        else:
            lines.append(f"{label}：{number(value['passed'])} / {number(value['population'])}；不可评价。")
    lines.append("仅评分表适用；清洗不改变历史事件时间。")
    sections["freshness"] = "\n".join(lines)
    return sections
