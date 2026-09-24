"""Render already-computed Hadoop facts; never calculate substitute scores."""
from datetime import datetime, timezone


LIMITATIONS = [
    "Accurate 是值约束代理，不能核验用户人口属性或电影事实的真实性。",
    "前后使用相同公式；分母随隔离和去重而变化，得分改善不等于信息已恢复。",
    "历史参照固定于 2003-02-28 23:59:59 UTC，时效性不代表相对于今天的新鲜程度。",
    "保留合法的不同时间评分；冲突组中无可靠依据判断的记录全部隔离。",
    "异常邮编、未附年份的标题和疑似混合编码只记录警告，未猜测补全或改写。",
    "时间分区仅登记过滤条件，尚未物化；训练工具必须按 T1 过滤，不能使用全量评分拟合。",
]


def utc(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def render_report(result):
    before, after = result["before"], result["after"]
    lines = ["# MovieLens 数据治理报告", "",
             f"- 任务：{result['task_id']}",
             f"- 原始版本：{result['input_ref']['version']}",
             f"- 执行代码：{result['implementation_sha256']}",
             f"- 清洗数据引用：{result['cleaned_ref']['artifact_id']}@{result['cleaned_ref']['version']}",
             "", "## 五维结果", "",
             "| 维度 | 清洗前 | 清洗后 | 变化（百分点） |",
             "| --- | ---: | ---: | ---: |"]
    for dimension in before["overall"]:
        initial = before["overall"][dimension]["score"]
        final = after["overall"][dimension]["score"]
        fmt = lambda value: "不可评价" if value is None else f"{value:.6f}"
        delta = final - initial if initial is not None and final is not None else None
        lines.append(f"| {dimension} | {fmt(initial)} | {fmt(final)} | {fmt(delta)} |")
    lines += ["", "分表分子、分母、权重及不可评价原因见同任务 quality.json。没有额外综合分。",
              "", "## 数据处置", "",
              "| 表 | 输入 | 输出 | 保留 | 修复 | 去重 | 隔离 |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for table in ("users", "movies", "ratings"):
        counts = after["dispositions"][table]
        values = [before["tables"][table]["rows"], after["tables"][table]["rows"],
                  *[counts.get(key, 0) for key in ("kept", "repaired", "deduplicated", "quarantined")]]
        lines.append("| " + table + " | " + " | ".join(str(value) for value in values) + " |")
    lines += ["", "每条输入只有一个最终去向；先排除确定非法候选，再处理剩余同键冲突。",
              "修复历史与最终处置分别保存；隔离、去重不会被计作修复。", "",
              "## 隔离与去重原因", "", "| 表 | 规则/原因 | 次数（可重叠） |",
              "| --- | --- | ---: |"]
    for table, reasons in after["reasons"].items():
        for reason, count in sorted(reasons.items()):
            lines.append(f"| {table} | {reason} | {count} |")
    config = result["configuration"]
    lines += ["", "## 方法与时间边界", "",
              "Accurate：ratings 每行 1 项值约束、users 每行 3 项、movies 每行 1 项；",
              "Complete：完整必需槽位 / 期望槽位；Unique：不同合法业务键 / 全部行；",
              "Consistent：结构、值域、时间、同键与跨表约束均通过的行 / 全部行。",
              "四项先分表计算再等权汇总；任一必需表为空时汇总不可评价。",
              "Up-to-date：固定窗口内的评分行 / 全部评分行；其他两表不适用。", "",
              f"- 时效参照：{utc(config['metrics']['reference_time'])}；"
              f"窗口 {config['metrics']['window_seconds']} 秒。",
              f"- T1：{utc(config['split']['train_end'])}（训练包含边界）。",
              f"- T2：{utc(config['split']['validation_end'])}（验证包含边界）。",
              f"- 实际评分数：训练 {after['splits']['train']}，验证 {after['splits']['validation']}，"
              f"测试 {after['splits']['test']}。", ""]
    for name, ref in result["config_refs"].items():
        lines.append(f"- {name}：{ref['version']}")
    lines += ["", "## Hadoop 执行依据", "",
              "| 阶段 | 外部作业标识 |", "| --- | --- |"]
    for job in result["jobs"]:
        lines.append(f"| {job['stage']} | {', '.join(job['external_ids'])} |")
    lines += ["", "## 局限与未解决事项", ""]
    lines += [f"- {text}" for text in LIMITATIONS]
    lines += ["", "完整处置证据存于同任务 dispositions.jsonl；quality.json 含每类最多 3 条"
              "按来源排序的代表样例，样例不代表全部记录。", ""]
    return "\n".join(lines)
