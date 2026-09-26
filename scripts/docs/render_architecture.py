"""Generate editable SVG architecture and governance workflow drawings.

Python uses only the standard library.
PNG/PDF export is a separate, optional Playwright command documented beside the figures.
No application, model or Hadoop requests are made.
"""
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "architecture" / "diagrams"
INK, MUTED, PAPER = "#262722", "#656a62", "#faf9f6"
RED, GREEN, GREY, MODEL = "#ad352c", "#52756b", "#788078", "#71809a"


class Drawing:
    def __init__(self, width, height, title, description):
        self.parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="figure-title figure-description">
<title id="figure-title">{escape(title)}</title><desc id="figure-description">{escape(description)}</desc>
<defs>''']
        for name, color in (("red", RED), ("green", GREEN), ("grey", GREY), ("model", MODEL)):
            self.parts.append(f'<marker id="arrow-{name}" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto-start-reverse"><path d="M0 0 L9 4.5 L0 9 Z" fill="{color}"/></marker>')
        self.parts.append(f'</defs><rect width="{width}" height="{height}" fill="{PAPER}"/>')
        self.parts.append('<g font-family="Noto Sans CJK SC, Microsoft YaHei, sans-serif">')

    def text(self, x, y, lines, size=18, color=INK, weight=400, step=27, anchor="start"):
        if isinstance(lines, str):
            lines = [lines]
        self.parts.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{color}" text-anchor="{anchor}">')
        for index, line in enumerate(lines):
            self.parts.append(f'<tspan x="{x}" dy="{0 if index == 0 else step}">{escape(line)}</tspan>')
        self.parts.append('</text>')

    def group(self, key, x, y, width, height, title, fill="#f1f0eb", stroke="#d4d6ce", dashed=False):
        dash = ' stroke-dasharray="10 7"' if dashed else ''
        self.parts.append(f'<g id="{key}"><rect x="{x}" y="{y}" width="{width}" height="{height}" rx="14" fill="{fill}" stroke="{stroke}" stroke-width="1.5"{dash}/></g>')
        self.text(x + 24, y + 37, title, size=24, weight=600)

    def box(self, key, x, y, width, height, title, lines=(), fill="#fffefa", stroke="#d4d6ce", title_size=21, size=18, step=26):
        self.parts.append(f'<g id="{key}" class="module"><rect x="{x}" y="{y}" width="{width}" height="{height}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
        self.text(x + 18, y + 32, title, size=title_size, weight=600)
        self.text(x + 18, y + 63, lines, size=size, color=MUTED, step=step)
        self.parts.append('</g>')

    def arrow(self, points, kind="grey", both=False, dashed=False):
        colors = {"red": RED, "green": GREEN, "grey": GREY, "model": MODEL}
        path = " ".join(("M" if index == 0 else "L") + f"{x},{y}" for index, (x, y) in enumerate(points))
        extra = f' marker-start="url(#arrow-{kind})"' if both else ''
        if dashed:
            extra += ' stroke-dasharray="8 6"'
        self.parts.append(f'<path d="{path}" fill="none" stroke="{colors[kind]}" stroke-width="2" stroke-linejoin="round" marker-end="url(#arrow-{kind})"{extra}/>')

    def save(self, name):
        self.parts.append('</g></svg>')
        (OUT / name).write_text("\n".join(self.parts) + "\n", encoding="utf-8")


def overview():
    d = Drawing(2000, 1980, "MovieLens 数据治理 Agent 详细架构", "当前实现的浏览器、单进程 API 与 Agent、外部模型、共享持久化存储、独立 worker、Hadoop，以及待实现的后续迭代。红色为任务控制，绿色为数据证据，灰色为模块调用。")
    d.text(60, 66, "MovieLens 数据治理 Agent", size=38, weight=600)
    d.text(60, 106, "系统架构  /  迭代一已实现  /  2026-09-27  /  实现基线 9732d72", size=19, color=MUTED)
    for x, kind, label in [(60,"red","任务提交与发布"),(350,"green","数据与证据"),(620,"grey","模块调用与状态"),(940,"model","外部模型协议")]:
        d.arrow([(x,143),(x+46,143)],kind)
        d.text(x+62,150,label,size=17,color=MUTED)
    d.text(1940, 145, "实线框：当前实现   ·   虚线框：待实现", size=17, color=MUTED, anchor="end")

    d.group("browser",60,180,1420,160,"浏览器  /  手记版工作台")
    d.box("browser-chat",90,240,620,75,"实验助手",["自然语言请求 / 会话与任务选择 / 连续追问 / 显式重试"],title_size=20,size=17)
    d.box("browser-results",760,240,690,75,"结果与证据",["状态 / 五维质量 / 数据处置 / 时间划分 / 来源样例 / 下载"],title_size=20,size=17)
    d.box("external-model",1560,180,380,210,"外部模型服务",["支持 primary / backup / auto 路由","返回工具调用或解释计划","仅接收所需上下文与有界证据","不直接访问原始文件或 Hadoop"],fill="#edf0f4",stroke="#a5afbe",size=17,step=29)

    d.group("api-process",60,410,1420,635,"应用进程 A  /  FastAPI + Agent  /  127.0.0.1:8765",fill="#fcf5f1",stroke="#cb9a8e")
    d.box("http-api",90,473,1360,80,"HTTP 接口与会话范围检查",["POST messages / retry / explanation     ·     GET messages / calls / tasks / quality / artifacts / download"],fill="#f8e8e2",stroke="#c48b7b",size=18)
    d.box("agent-service",90,621,520,125,"AgentService · 会话协调",["请求去重 / 当前任务绑定 / 有界历史","工具循环 / 真实任务回执 / 调用审计"],stroke=RED)
    d.box("report-answer",660,621,510,125,"ReportAnswer · 已发布任务追问",["问题范围与样例位置 → 本轮取证","模型解释计划校验 → 按报告事实生成回答"],size=17)
    d.box("model-router",1220,621,230,125,"RoutedModel",["主备路由 / 超时","协议适配 / 上下文"],title_size=20,size=17)
    d.group("tool-registry",90,810,1360,200,"ToolRegistry  /  参数与结果校验；报告解释路径只允许查询",fill="#f9eee8",stroke="#cda397")
    d.box("job-tool",115,875,415,100,"计算工具 · governance.run",["固定输入、配置与实现哈希","写入 queued，返回 task_id"],title_size=20,size=17,step=24)
    d.box("query-tools",570,875,855,100,"查询工具",["datasets.describe / tasks.get / artifacts.list / artifacts.get","元数据 / 报告摘要 / 清洗样例 / 异常来源"],title_size=20,size=17,step=24)

    d.group("persistence",60,1120,1020,340,"持久化存储  /  同一个数据库 + 本机文件",fill="#f0f4f0",stroke="#94aba0")
    d.box("sqlite",90,1190,500,230,"SQLite · 元数据与调用证据",["var/catalog.sqlite3","DatasetCatalog：原始数据版本与清单","TaskStore：任务 / 阶段 / 产物","ConversationStore：会话 / 消息 / 调用","请求去重 / 配置快照 / 精确版本引用"],stroke="#94aba0",size=17,step=29)
    d.box("raw-data",620,1190,430,90,"只读原始文件",["ml-1m/ · users / movies / ratings"],title_size=20,size=17)
    d.box("run-files",620,1310,430,110,"任务专属文件",["var/runs/{task_id}/","input / results / logs / published"],title_size=20,size=17,step=25)

    d.group("worker-process",1140,1120,800,440,"应用进程 B  /  独立 worker  /  同一 catalog 单实例",fill="#f0f3f3",stroke="#9daeb0")
    d.box("worker",1170,1190,740,80,"Worker · 从数据库领取任务",["事务领取 queued → running；更新阶段与状态"],stroke="#718c92",size=18)
    d.box("workflow",1170,1310,740,90,"GovernanceWorkflow · governance.v1",["版本核对 → 来源封装 → 七个 Hadoop 作业 → 核对与导出"],size=17)
    d.box("verify",1170,1450,350,80,"发布前核对",["守恒 / 外键 / 分区 / SHA-256"],title_size=20,size=16,stroke="#718c92")
    d.box("hadoop-adapter",1570,1450,340,80,"Hadoop 适配器",["提交作业 / HDFS 读写 / 日志"],title_size=20,size=16)

    d.group("future",60,1540,1020,220,"后续迭代  /  待实现",fill="#f5f4ef",stroke="#a9aaa0",dashed=True)
    d.text(86,1620,"迭代二：分类 / 聚类 / 降维",size=21,weight=500)
    d.text(86,1661,"迭代三：知识图谱 / 推荐 / 社区挖掘 / 链接分析",size=21,weight=500)
    d.text(86,1705,["接入新的算法工具、工作流、产物类型与专用视图。","复用上方的会话、注册、任务、证据及版本机制。"],size=18,color=MUTED,step=28)

    d.group("hadoop-runtime",1140,1610,800,275,"Hadoop 运行环境  /  本机单节点 · 真实批处理",fill="#e8efee",stroke="#94aaa6")
    d.box("yarn",1170,1680,215,165,"YARN",["ResourceManager","NodeManager","资源与容器调度"],size=16,step=28)
    d.box("streaming",1415,1680,240,165,"Hadoop Streaming",["Python Mapper / Reducer","分组 / 规则 / 五维评分","七个 MapReduce 作业"],title_size=19,size=15,step=28,stroke="#718c92")
    d.box("hdfs",1685,1680,225,165,"HDFS",["NameNode / DataNode","/movielens/tasks/{id}/","输入 / 输出 / 清洗副本"],size=15,step=28)

    d.arrow([(350,340),(350,410)],"red",both=True)
    d.text(365,386,"提出请求 / 回答与状态",size=17,color=RED)
    d.arrow([(1040,340),(1040,410)],"green",both=True)
    d.text(1055,386,"轮询 / 查看 / 下载",size=17,color=GREEN)
    d.arrow([(350,553),(350,621)])
    d.text(365,595,"会话请求",size=16,color=MUTED)
    d.arrow([(540,621),(540,587),(1335,587),(1335,621)],"model",both=True)
    d.text(800,579,"模型请求与回复",size=16,color=MODEL)
    d.arrow([(610,685),(660,685)])
    d.arrow([(1450,685),(1520,685),(1520,295),(1560,295)],"model",both=True)
    d.arrow([(350,746),(350,810)])
    d.text(365,786,"结构化工具调用",size=16,color=MUTED)
    d.arrow([(850,746),(850,810)],"green")
    d.text(865,786,"先读本轮精确证据",size=16,color=GREEN)
    d.arrow([(320,975),(320,1120)],"red")
    d.text(337,1089,"提交任务与固定配置",size=17,color=RED)
    d.arrow([(850,975),(850,1120)],"green",both=True)
    d.text(868,1089,"读取状态、报告与样例",size=17,color=GREEN)
    d.arrow([(1450,510),(1493,510),(1493,1070),(1040,1070),(1040,1120)],"green")
    d.text(1180,1061,"GET 状态 / 文件下载",size=16,color=GREEN)
    d.arrow([(1080,1230),(1140,1230)],"red",both=True)
    d.arrow([(1080,1355),(1140,1355)],"green",both=True)
    d.arrow([(1530,1270),(1530,1310)])
    d.arrow([(1345,1400),(1345,1450)])
    d.text(1165,1434,"全部作业结束后",size=15,color=MUTED)
    d.arrow([(1740,1400),(1740,1450)])
    d.arrow([(1170,1490),(1100,1490),(1100,1380),(1080,1380)],"red")
    d.text(1068,1495,"产物登记与 succeeded 原子发布",size=17,color=RED,anchor="end")
    d.arrow([(1740,1530),(1740,1610)],"green",both=True)
    d.text(1757,1590,"实际计算 / 文件读写",size=16,color=GREEN)
    d.arrow([(1385,1765),(1415,1765)])
    d.arrow([(1655,1765),(1685,1765)],"green",both=True)

    d.text(60,1815,["阅读提示：大框表示运行分组；内部矩形表示逻辑模块。","API 和 worker 通过 SQLite 任务记录衔接；报告、样例与下载可直接读取。","解释失败保留已发布产物；外部作业状态不明时，任务标记 unknown 待核查。"],size=18,color=MUTED,step=31)
    d.text(60,1942,"已实现：会话 → 工具 → 持久任务 → Hadoop → 版本化产物 → 证据解释",size=18,weight=500)
    d.text(1940,1942,"详见：系统架构详解.md",size=17,color=MUTED,anchor="end")
    d.save("system-architecture.svg")



def governance_flow():
    d = Drawing(1800, 2170, "Hadoop 治理的七个 MapReduce 作业", "执行顺序：准备输入，清洗前父表检查、评分表检查、指标聚合，清洗父表和评分表，清洗后复查和指标聚合，核对导出，最后由 worker 登记并发布四类产物。")
    d.text(60, 66, "Hadoop 治理工作流", size=38, weight=600)
    d.text(60, 106, "执行细节  /  迭代一已实现  /  2026-09-27  /  实现基线 9732d72", size=19, color=MUTED)
    d.text(60, 150, "九个执行阶段 = 输入准备 + 七个 MapReduce 作业 + 核对导出；随后由 worker 发布产物。", size=21)

    d.box("source-files",60,200,440,145,"输入：已登记的原始三表",["users.dat / movies.dat / ratings.dat","保留原文件，不覆盖、不原地清洗","每行记录原始版本、行号和字节偏移"],size=18)
    d.box("prepare-inputs",570,200,710,145,"准备 · prepare-inputs",["核对原始文件哈希、大小与行数","逐行封装 Base64 原始字节及 source_ref，不做业务过滤","上传到任务专属 HDFS 输入目录"],fill="#edf2ef",stroke=GREEN,size=18)
    d.box("configuration",1350,200,390,145,"固定本次运行依据",["输入版本 / 规则 / 评分 / 时间配置","实现哈希与 streaming.py 快照","每个作业使用同一份配置"],size=17)

    d.group("before-phase",60,420,440,400,"A · 清洗前评分",fill="#f2f1ec")
    d.text(86,502,["先检查 users、movies 两张父表，","再检查 ratings 引用的用户与电影。","父表索引描述 ID 是否存在及冲突。"],size=18,color=MUTED,step=32)
    d.text(86,640,["五个维度分别计算：","准确性 / 完整性 / 唯一性","时效性 / 一致性","评分来自真实 Hadoop 输出。"],size=18,color=MUTED,step=32)
    d.box("before-parents",570,420,710,110,"01 · before-parents",["读取原始 users、movies；检查字段与同键记录","输出分组统计及原始父表索引所需记录"],size=18)
    d.box("before-ratings",570,565,710,110,"02 · before-ratings",["读取原始 ratings，使用原始父表索引","检查评分事件、引用及质量；使用 2 个 reducer"],size=18)
    d.box("before-metrics",570,710,710,110,"03 · before-metrics",["汇总作业 01、02 的输出","形成清洗前各表指标、五维得分及其分子分母"],size=18)
    d.box("raw-parent-index",1350,420,390,160,"原始父表索引",["来自作业 01 的 Hadoop 输出","由工作流整理为 JSON 文件","随作业分发给 02、05 使用"],size=17,step=28)
    d.box("before-quality",1350,710,390,110,"清洗前质量结果",["before-metrics.jsonl","作为前后对比的固定基准"],size=17)

    d.group("clean-phase",60,900,440,255,"B · 清洗与处置",fill="#fcf1ea",stroke="#d4afa0")
    d.text(86,984,["先清父表，再处理评分的引用。","记录保留、修复、去重、隔离原因。","不按官方记录数补齐或删减数据。","每条输出和处置保留来源。"],size=18,color=MUTED,step=32)
    d.box("clean-parents",570,900,710,110,"04 · clean-parents",["重新读取原始 users、movies，执行清洗规则","输出保留记录、处置记录及清洗后父表索引"],fill="#fcf1ea",stroke="#d4afa0",size=18)
    d.box("clean-ratings",570,1045,710,110,"05 · clean-ratings",["读取原始 ratings，使用原始 + 清洗后父表索引","处理评分冲突、无效引用等；使用 2 个 reducer"],fill="#fcf1ea",stroke="#d4afa0",size=18)
    d.box("clean-parent-index",1350,900,390,110,"清洗后父表索引",["来自作业 04 的输出","分发给作业 05、06 使用"],size=17)
    d.box("clean-records",1350,1045,390,140,"候选清洗数据与处置记录",["作业 04：用户和电影","作业 05：评分","此时尚未登记为完整产物"],size=17)

    d.group("after-phase",60,1250,440,255,"C · 清洗后复查",fill="#edf2ef",stroke="#9aafa5")
    d.text(86,1334,["按同一指标口径重新评分。","统计记录处置与 T1/T2 时间区间。","应用层核对结果并生成报告，","不在本地重算一套替代得分。"],size=18,color=MUTED,step=32)
    d.box("after-groups",570,1250,710,110,"06 · after-groups",["读取作业 04、05 的保留三表，使用清洗后父表索引","复查约束并产生评分分组；使用 2 个 reducer"],fill="#edf2ef",stroke="#9aafa5",size=18)
    d.box("after-metrics",570,1395,710,110,"07 · after-metrics",["汇总作业 06 的评分以及作业 04、05 的处置输出","形成清洗后五维指标、处置数量、训练 / 验证 / 测试行数"],fill="#edf2ef",stroke="#9aafa5",size=18)
    d.box("after-quality",1350,1395,390,140,"清洗后质量结果",["after-metrics.jsonl","保留行指标 / 全部处置统计","T1/T2 过滤范围及行数"],size=17)

    d.box("conservation",60,1620,440,145,"守恒与有效引用",["输入 = 输出 + 去重 + 隔离","输出 = 保留 + 修复后保留","每条保留评分引用有效用户和电影"],size=18,stroke=GREEN)
    d.box("verify-export",570,1620,710,145,"核对与导出 · verify-and-export",["核对行数守恒、外键、约束分数与时间分区合计","写入清洗三表、处置记录、质量报告及清单","将清洗三表上传到 HDFS dataset/；本地文件设为只读"],fill="#edf2ef",stroke=GREEN,size=18)
    d.box("publication",570,1850,710,120,"Worker 发布 · TaskStore.publish",["再次核对每个产物文件的大小与 SHA-256","在同一 SQLite 事务中登记四类产物并将任务置为 succeeded"],fill="#f8e8e2",stroke=RED,size=18)
    d.box("published-artifacts",1350,1620,390,370,"发布结果：四类产物 / 七个文件",["cleaned_dataset","  users.jsonl / movies.jsonl","  ratings.jsonl","quality_report","  quality.json","disposition_log","  dispositions.jsonl","report","  report.md / dataset-manifest.json"],size=16,step=31,title_size=20,stroke=GREEN)
    d.box("pipeline-limits",60,1850,440,150,"结果的含义",["约束满分不等于真实世界无误","分区是过滤条件，未另存三份数据","来源定位用于追溯，不是正确性证明"],size=18)

    d.arrow([(500,270),(570,270)],"green")
    d.arrow([(1350,270),(1280,270)])
    for start, end in [(345,420),(530,565),(675,710),(820,900),(1010,1045),(1155,1250),(1360,1395),(1505,1620),(1765,1850)]:
        d.arrow([(925,start),(925,end)],"red" if start == 1765 else "grey")
    for y in [470,760,950,1095,1445]:
        d.arrow([(1280,y),(1350,y)],"green")
    d.arrow([(1280,1910),(1350,1910)],"red")
    d.text(945,1808,"文件就绪后核对并登记",size=17,color=RED)
    d.text(60,2080,["中央箭头表示工作流执行顺序，右侧展示关键输出；具体输入在每个作业框中标明。","每个 MapReduce 作业都记录真实 application/job ID。计算中间文件不作为成功产物对外发布。"],size=19,color=MUTED,step=32)
    d.save("governance-pipeline.svg")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    overview()
    governance_flow()
    for file in sorted(OUT.glob("*.svg")):
        print(file.relative_to(ROOT))


if __name__ == "__main__":
    main()
