# 大数据分析课程实验（2026）

本仓库用于管理大数据分析课程的小组实验，围绕 **MovieLens 1M 数据分析 Agent 系统**开展三轮迭代：从 Hadoop 数据清洗与质量评估出发，逐步接入机器学习、知识图谱和图分析能力。

项目目标是让用户通过简易前端输入自然语言请求，由 Agent 调用实际的数据处理工具，返回任务状态、分析结果及其依据，并支持围绕结果继续提问。

**当前进度：Agent、页面、持久会话和真实 Hadoop 治理已接通，数量解析、连续样例追问与失败重新解释已完成本轮验证。** [最新实测](docs/iterations/01-governance/reports/2026-09-25-追问解析与重试实测.md)记录修正后原七题 7/7 通过、14 项变体与多轮检查首测 13/14 通过（含两项应用澄清）；唯一的网关 504 单独补测一次通过。真实页面的新追问和历史失败重试均通过。服务仍有时延与可用性波动，已记录原始失败、内部修正和解析边界。此前的[备用模型全链路实测](docs/iterations/01-governance/reports/2026-09-25-备用模型全链路实测.md)保留七作业、下载、分区读取与重启证据，固定数据读取见[说明](docs/iterations/01-governance/handoff/清洗数据读取.md)。

## 实验文档

完整入口见 [文档导航](docs/README.md)，协作约定见 [Git 工作流](docs/development/git-workflow.md)。现场操作见[迭代一演示说明](docs/iterations/01-governance/demo/演示说明.md)。

- [项目总体要求与汇报安排](docs/course/引言_项目总体要求与汇报安排.md)：系统目标、三轮迭代要求、提交内容与汇报安排。
- [迭代一：Hadoop 数据清洗与 Agent 基础](docs/course/迭代一_Hadoop数据清洗与Agent基础.md)：数据检查方向、五维质量评分、Agent 工具与前端要求。
- [迭代一：需求拆解与验收清单](docs/iterations/01-governance/需求拆解与验收清单.md)：交付范围、需求追踪、验收场景、开发顺序与待确定事项。
- [Agent 整体架构与迭代边界](docs/architecture/Agent整体架构与迭代边界.md)：共用模块、工具与任务协议、版本化产物、后续接入方式及课件参考。
- [迭代一：技术方案与接口约定](docs/iterations/01-governance/技术方案与接口约定.md)：首版选型建议、模块边界、工具与 HTTP 接口、任务状态及结果发布。
- [迭代一：数据与评分约定](docs/iterations/01-governance/数据与评分约定.md)：三表输出格式、来源追踪、已实现的清洗规则、五维评分方法与时间划分。
- [清洗数据交接与读取](docs/iterations/01-governance/handoff/清洗数据读取.md)：固定产物引用、哈希校验、T1/T2 分区与下一轮 Python 示例。
- [迭代一：开发与交接计划](docs/iterations/01-governance/开发与交接计划.md)：W00—W09 工作项、依赖关系、运行验证和交接清单。

README 提供项目入口与准备说明。课程要求以总体与各轮实验文档为准；需求清单和架构草案另记录小组确定的交付目标与实现建议。

## 迭代计划

| 迭代 | 目标与主要任务 | 主要产物 | 状态 |
| --- | --- | --- | --- |
| 一：Agent 基础与数据治理 | 建设可复用的 Agent 框架，并通过 Hadoop 完成清洗前评分、清洗、清洗后评分与对比 | 工具/任务/产物协议与共用框架、清洗数据、版本与任务记录、`T1/T2`、报告及接入说明 | Agent、页面和 Hadoop 已实现；验证与交接见本轮报告 |
| 二：机器学习分析 | 预测评分是否不低于 4 分；按历史观影偏好聚类用户；降维并比较聚类效果、信息保留与资源开销 | 分类模型、用户画像与群体、降维模型与坐标、参数及评价结果 | 待实现 |
| 三：知识图谱与图分析 | 构建并校验电影领域知识图谱，在同一图谱版本或其图投影上完成推荐、电影社区挖掘与链接分析 | 带来源与版本的知识图谱、推荐结果、社区结构与节点排序结果 | 待实现 |

迭代二的分类、聚类、降维，以及迭代三的推荐、图挖掘、链接分析，**每类任务均需从相应课程 PPT 中选择两种算法实现**，并将每种算法封装为可独立调用的 Agent 工具。

迭代一需要比较以下五个维度的清洗前后得分：

| 维度 | 评价关注点 |
| --- | --- |
| Accurate（准确性） | 数据值是否真实、准确；区分可检查的格式或范围与无法核验的真实性 |
| Complete（完整性） | 业务所需的记录与字段是否完整 |
| Unique（唯一性） | 是否存在重复业务记录或不必要的冗余 |
| Up-to-date（时效性） | 数据相对于所选时间参照与分析场景是否足够新 |
| Consistent（一致性） | 类型、格式、含义及跨表关联是否一致 |

具体指标、分值范围、权重和清洗规则由小组设计并说明依据。清洗前后使用同一套评价口径，不预设所有得分必须提高；应分别说明修复、去重和隔离的影响。

## 目录结构

以下结构包含本地准备的数据与课程资料；克隆仓库后需另行准备 `ml-1m/` 和所需的 `slides/` 资料。

```text
.
├── README.md
├── .gitignore
├── pyproject.toml                 # 包定义与依赖声明
├── environment.yml                # AgentDev 环境声明
├── requirements.lock              # 已验证的 Python 库版本
├── environments/                  # 平台环境锁文件
├── src/movielens_agent/            # Agent、API、页面、工具与 Hadoop 工作流
├── configs/governance/            # 规则、评分与时间配置
├── scripts/                       # Hadoop、实际验收与交接读取示例
├── tests/                         # 小型数据与框架行为验证
├── docs/
│   ├── README.md                  # 文档导航
│   ├── course/                    # 课程原始要求
│   ├── architecture/              # 三轮共用架构
│   ├── iterations/01-governance/   # 迭代一需求、方案、计划与核查报告
│   └── development/               # 开发环境与 Git 协作指南
├── ml-1m/                         # 本地原始数据，Git 忽略
└── slides/                        # 本地课件与预习资料，Git 忽略
```

## 获取仓库与准备数据

以下命令适用于 Linux / WSL，需要 Git 和 `unzip`。使用 SSH 克隆前需配置 GitHub SSH 认证；已有本地仓库可跳过克隆步骤。

```bash
git clone git@github.com:DuscWalk/Big-Data-Analysis-2026.git
cd Big-Data-Analysis-2026
git switch feat/iteration-01-foundation
```

当前实现位于上述功能分支，正式交付合并后再以主分支和交付标签为准。

将课程提供的 `ml-1m.zip` 放到仓库根目录后解压。若 `ml-1m/` 已就位，无需重复解压。

```bash
unzip ml-1m.zip
```

确认根目录下存在 `ml-1m/README`、`ml-1m/movies.dat`、`ml-1m/ratings.dat` 和 `ml-1m/users.dat`，可用以下命令核对文件行数：

```bash
wc -l ml-1m/movies.dat ml-1m/ratings.dat ml-1m/users.dat
```

`ml-1m/` 与 `slides/` 不随 Git 同步。开展实验前，小组成员应确认使用的数据来源与版本一致，并保留原始数据，另行存放清洗产物。

以上步骤完成仓库与数据准备；实际执行见下文及开发指南。

## 本地开发与已实现命令

使用 `duscwalk` 用户的 Conda 环境 **`AgentDev`**，当前已验证 Python 3.11.16。依赖与完整复现步骤见 [开发环境与本地运行](docs/development/environment.md)。

```bash
source /home/duscwalk/miniconda3/etc/profile.d/conda.sh
conda activate AgentDev
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m movielens_agent profile --data-dir ml-1m
python -m unittest discover -s tests -v
```

核查命令自动建立新的本地输出目录并返回数据版本，可通过 `describe` 子命令查询登记清单。首次全量结果见 [2026-09-24 原始数据核查](docs/iterations/01-governance/reports/2026-09-24-原始数据核查.md)。核查仅提供数据证据，不替代 Hadoop 的正式清洗和五维评分。

继续运行治理任务：先按 [Hadoop 指南](docs/development/hadoop-local.md)安装，并在独立终端运行 `python scripts/hadoop/local_cluster.py serve`，再提交任务并启动 worker：

```bash
python -m movielens_agent submit --version sha256-46bfa0020d750da32d409f93c6cb305a1347019f8a703f7f6b943458ee0fa578 --request-id governance-001
python -m movielens_agent worker --once
python -m movielens_agent task --task-id TASK_ID
```

版本使用 profile 的实际输出，TASK_ID 使用 submit 返回值。去重、失败核查、报告及样例查询见 [任务与工具指南](docs/development/tasks-and-tools.md)。全量运行保留 935,354 条评分，前后分数、处置损失与作业记录见 [2026-09-24 Hadoop 治理实测](docs/iterations/01-governance/reports/2026-09-24-Hadoop治理实测.md)。

应用入口与模型配置见 [Agent、模型与页面指南](docs/development/app-and-model.md)。准备好 `.env` 后，在 worker 和 Hadoop 运行期间启动：

```bash
python -m movielens_agent model-probe
python -m movielens_agent serve --port 8765
```

打开 <http://127.0.0.1:8765>，用自然语言发起清洗，再查看自动更新的任务、五维对比、异常样例与报告。模型解释失败时仍可读取实际产物；“受理”与“计算完成”是不同状态。应用仅面向可信本机使用。

## 数据集说明

MovieLens 1M 由明尼苏达大学 GroupLens Research 发布。官方原始数据包含 6,040 位匿名用户、3,883 条电影记录和 1,000,209 条评分；每位入选用户至少评价过 20 部电影。

| 文件 | 字段格式 | 官方原始记录数 | 初始化时本地文件行数 |
| --- | --- | ---: | ---: |
| `movies.dat` | `MovieID::Title::Genres` | 3,883 | 4,465 |
| `ratings.dat` | `UserID::MovieID::Rating::Timestamp` | 1,000,209 | 1,150,241 |
| `users.dat` | `UserID::Gender::Age::Occupation::Zip-code` | 6,040 | 6,946 |

本地行数于 **2026-09-24** 核对，与官方原始规模不同，仅用于说明初始化时的数据副本。行数不等于有效记录数；实际核查已发现格式、值域、重复与冲突等问题，具体统计和局限见本轮核查报告。更换数据后应重新核验并登记版本。

解析时需注意：

- 文件无表头，字段使用 `::` 分隔；按课程文档使用 **ISO-8859-1** 编码读取。
- `Rating` 为 1–5 的整数，`Timestamp` 为 Unix 时间戳，单位是秒。
- `Genres` 中的多个类型使用 `|` 分隔；电影编号并不连续，最大编号不代表电影数量。
- `Age` 和 `Occupation` 是类别编码；`Zip-code` 应保留为字符串，避免丢失前导零。
- 用户人口属性由用户自愿填写且未经核验，格式正确不能证明内容真实；历史数据的时效性也需结合时间参照解释。

完整字段枚举与使用条件见解压后的 `ml-1m/README`。官方介绍与下载入口见 [GroupLens MovieLens 1M](https://grouplens.org/datasets/movielens/1m/)。

## 实验实现约定

- **架构复用：** 第一轮实现共用的工具注册、任务执行、会话证据、产物登记与前端入口；第二、三轮通过独立算法工具、工作流和结果视图扩展。
- **复用与版本管理：** 后续迭代复用前一轮已登记的成果；数据、清洗规则、模型、知识图谱和评价结果均记录版本，避免混用不同版本的产物。
- **统一时间边界：** 迭代一确定 `T1/T2`。`T1` 为训练期截止时间，`T2` 为验证期截止时间，测试数据位于 `T2` 之后；不得使用验证期或测试期信息构造训练输入。
- **实际执行：** Hadoop 承担迭代一的实际清洗与评分计算；Agent 组织任务、调用工具并解释结果。前端展示实际状态、证据与异常，失败或未完成时如实说明。
- **可追溯结果：** 保留任务标识、输入与输出版本、算法参数、评价方法和运行结果，不使用占位数据、示例结果或主观推测代替实际结果。
- **图谱前置条件：** 迭代三的知识图谱须包含用户、电影、类型及其关系，记录来源、训练截止时间与版本；图谱未完成、校验失败或版本不一致时，不运行其上的三个分析任务。

## 汇报与提交

| 汇报 | 日期 | 对应迭代 | 安排 |
| --- | --- | --- | --- |
| 第一次 | 9 月 30 日 | 迭代一 | 所有小组参加 |
| 第二次 | 10 月 21 日 | 迭代二 | 抽取一半小组参加 |
| 第三次 | 11 月 11 日 | 迭代二 | 抽取一半小组参加；第二次存在问题的小组可重新汇报 |
| 第四次 | 12 月 2 日 | 迭代三 | 抽取一半小组参加 |
| 第五次 | 12 月 23 日 | 迭代三 | 抽取一半小组参加；第四次存在问题的小组可重新汇报 |

每次汇报约 **10 分钟**，优先展示核心功能、实际运行结果、评价结论与主要限制。

每轮必须提交对应的**源码与相关文档**，包括 Agent 工具、前端、配置与运行脚本，以及运行方法、工具接口、数据和模型版本、实验配置、评价结果与已知限制。提交内容应与汇报展示的系统版本一致。

PPT、演示视频及其他汇报材料为可选项；可使用预录视频替代现场系统操作展示。具体安排见[项目总体要求与汇报安排](docs/course/引言_项目总体要求与汇报安排.md)。

## 数据引用

数据集的使用与引用条件以随附的 `ml-1m/README` 为准。相关论文：

> F. Maxwell Harper and Joseph A. Konstan. 2015. The MovieLens Datasets: History and Context. ACM Transactions on Interactive Intelligent Systems, 5(4), Article 19. [DOI: 10.1145/2827872](https://doi.org/10.1145/2827872).
