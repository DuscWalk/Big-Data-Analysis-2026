# 任务、工具与产物开发

当前已实现共用工具注册、原始数据登记、任务受理、独立 worker、阶段记录和原子发布；持久会话、模型与前端已完成实际联调，见[应用指南](app-and-model.md)。本文说明底层 Python/CLI 行为和下一轮接入方式。

## 运行一个治理任务

先按[环境指南](environment.md)安装并登记原始数据，按[Hadoop 指南](hadoop-local.md)启动集群。在仓库根目录运行：

```bash
python -m movielens_agent submit --version sha256-46bfa0020d750da32d409f93c6cb305a1347019f8a703f7f6b943458ee0fa578 --request-id governance-001
python -m movielens_agent worker --once
python -m movielens_agent task --task-id TASK_ID
```

示例数据版本仅适用于当前课程副本，换数据时使用 profile 返回的实际版本。submit 返回 accepted 和 task_id，不返回未计算的分数。CLI `--config` 明确指定配置文件，省略则使用 `configs/governance/default.json`；这与网页中“默认”跟随持久化共享方案的行为不同。`--once` 处理队列中的一个任务；不加该参数则持续处理队列。正常应用将单独运行 worker，提交进程和页面无需等待 Hadoop。

task 返回真实状态、当前阶段、错误、所有阶段尝试、外部作业标识和发布产物。CLI 默认会话为 local-cli；需要区分上下文可在提交、查询时使用同一个 `--session-id`。这是应用内部范围隔离，尚不是 HTTP 身份认证。

## 执行阶段与可计量进度

治理工作流 `governance.v1` 包含输入准备、七个 Hadoop 作业、核对导出，共九个阶段。API 的任务详情返回 `workflow`；每次阶段尝试包含 `sequence`、`status`、外部作业 ID 与可空的 `progress`。`tasks.get` 工具也返回进度，助手可以据此回答执行情况。HTTP 和工具视图不返回本地执行日志路径。

`progress` 保存消息、更新时间和最多四项指标，每项包括 `key`、`label`、`current`、`total`、`unit`。单位为 `rows`、`files`、`bytes` 或 `percent`；计数不能为负或超过总量，百分比的总量固定为 100。例如：

```json
{
  "message": "Hadoop 作业处理中",
  "updated_at": "2026-09-27T08:22:24+00:00",
  "metrics": [
    {"key": "map", "label": "Map", "current": 100, "total": 100, "unit": "percent"},
    {"key": "reduce", "label": "Reduce", "current": 25, "total": 100, "unit": "percent"}
  ]
}
```

上例用于说明结构，并非某次实测值。真实计量来源如下：

| 阶段 | 计量来源 | 显示含义 |
| --- | --- | --- |
| 核对与封装输入 | 已读取封装的原始行数 / 登记清单总行数；成功上传输入文件数 / 3 | 三表合计数据量，以及 HDFS 输入准备情况 |
| 七个 Hadoop 作业 | Streaming 客户端日志中的 `map N% reduce N%` | 分别展示 Map、Reduce 的进度；不是原始记录数，也不合成为预计总耗时 |
| 核对并导出产物 | 实际读取的两个清洗中间文件字节数 / 总字节数；成功上传清洗文件数 / 3 | 读取与上传各自的进展；读取完成后仍须通过守恒、外键与文件摘要校验 |

行数和字节回调在遍历中计量，以每万条检查一次且间隔至少 0.5 秒的方式限流，并在每个文件结束时记录；不为进度额外扫描数据。Hadoop 日志解析兼容分块读取、换行、回车与末尾未换行，忽略重复百分比；重试造成的实际回退照实记录。Map/Reduce 已到 100% 时仍可能在下载结果，阶段状态须等下载完成后才转为成功。

最新快照写入 SQLite 的 `attempt_progress` 表，按 `(task_id, sequence)` 关联阶段。仅当任务及尝试都处于 `running` 时接受更新，避免迟到回调覆盖已停止记录。旧数据库自动补建此表，旧阶段保留 `progress=null`；不补造历史计数。worker 中断后的 `unknown` 阶段保留最后记录，不能据此认定外部作业已经停止。

页面每 2 秒读取任务进度，读取失败后间隔 4 秒重试。待执行阶段显示空进度条；运行但尚未上报数值时显示等待动画；失败或待核查时停止动画并保留最后数值；完成的历史阶段显示完成状态并说明缺少处理中的计数。刷新不会收起执行阶段，切换任务或会话会清除旧任务进度。阶段完成数只表示完成了几步，不代表耗时比例。

轻量回归可运行 `python -m unittest discover -s tests -v`，先确保没有设置 `MOVIELENS_HADOOP_RUNTIME`。`test_progress_workflow.py` 使用总计 3 条输入和进程内适配器，检查计数、导出与上传失败，不启动 JVM 或连接集群。真实 Hadoop 测试需要显式设置环境变量；即使只有 22 条输入，也会启动七个作业，不适合资源紧张的开发环境反复执行。此次验证范围见[阶段进度实测](../iterations/01-governance/reports/2026-09-27-执行阶段进度实测.md)。

## 幂等、失败与恢复

对 `(session_id, request_id)` 建唯一约束。规范化参数相同则返回原任务；同一键参数不同返回 REQUEST_CONFLICT。规则、评分、时间配置与源清单在受理时固定；worker 拒绝执行代码版本已改变的排队请求。再次明确运行应使用新的 request ID。

正常状态：queued → running → succeeded/failed。队列和结果保存于 SQLite，使用 WAL 与短事务；整个 Hadoop 作业不会占用数据库事务。

一个 OS 文件锁保证同一数据库只有一个 worker。worker 启动时把遗留 running 标为 unknown，不会自动重排；已有 YARN 作业可能仍在运行。日志在提交前登记，捕获到 job/application ID 后立即写入。客户端异常或超时、且无法确认外部失败时标 unknown；已核实的 YARN 失败标 failed。未知状态的人工核查步骤见 Hadoop 指南。

成功发布前须检查：

1. 所有正式作业输出存在 _SUCCESS。
2. 三表导出数量、四类处置与 Hadoop 统计守恒，评分外键无悬空。
3. 前后配置相同，时间分区总量与评分输出一致。
4. 必需文件可读，字节数与 SHA-256 一致。

文件先准备，全部产物登记和任务成功在同一个 SQLite 事务中完成。中断可留下未登记文件，但这些文件不会成为查询工具可见的正式产物。已发布文件设为只读；内容读取会再核对校验值。

## 已实现工具

| 工具 | 类型 | 主要参数与结果 |
| --- | --- | --- |
| datasets.describe | query | 精确 raw dataset_ref，返回原始清单 |
| governance.configs | query | offset/limit；已登记方案、配置快照和默认 revision |
| governance.configure | action | name、可选 base_ref、rules/metrics/split 部分修改；可带 set_default/default_revision，保存方案，不提交计算 |
| governance.set_default | action | config_ref、default_revision；切换共享默认 |
| governance.run | job | dataset_ref、可选 config_ref 和匹配该方案的 rule_ref/metric_ref；返回 task_ref |
| tasks.get | query | task_id；返回当前会话可见的状态与产物引用 |
| artifacts.list | query | task_id、offset/limit；每页最多 100 项 |
| artifacts.get | query | artifact_ref、mode、file_name、offset/limit；元数据、评分摘要、报告或有来源的 JSONL 样例 |

通用入口位于 [registry.py](../../src/movielens_agent/tools/registry.py)；保留 QueryTool 这个首版类型名，mode 区分 query 查询、action 即时修改和 job 任务受理。报告解释模式只允许 query。输入输出都经 Pydantic 校验，额外参数拒绝。模型只能填写业务参数；会话、request_id 和本轮选择约束由应用传入 ToolContext。

首次启动以 [default.json](../../configs/governance/default.json)初始化治理方案，之后使用 catalog 中的持久默认。完整内容哈希是方案版本，分项另有 rules/metrics/split 引用。模型未传引用时补入本轮选定方案，显式选择冲突或未知引用拒绝；受理后将名称、完整配置、方案及分项引用保存在 task payload。默认变更不影响历史任务。配置失败后须修正成功才能在同一轮提交清洗，不执行任意路径、脚本或 Shell 命令。详见[配置计划](../iterations/01-governance/plans/治理方案配置.md)。

## 读取证据

从 task 的 artifacts 中复制精确引用：

```bash
python -m movielens_agent evidence --artifact-id ARTIFACT_ID --version ARTIFACT_VERSION --mode summary
python -m movielens_agent evidence --artifact-id CLEANED_ARTIFACT_ID --version ARTIFACT_VERSION --mode sample --file-name ratings.jsonl --limit 3
```

包含多个文件的产物须指定 file-name。JSONL 样例每次最多 20 行，返回 offset、has_more 和来源；完整处置与清洗数据不放进模型上下文。评分摘要取已发布 quality.json 中的真实事实。工具查询按当前会话范围校验任务和产物；原始数据清单是项目公共数据。应用没有用户身份认证，历史入口可切换会话，因此这种查询范围校验不构成成员间的权限隔离。

## 下一轮的扩展位置

1. 用 Contract 定义业务输入和返回模型，在 registry 注册工具；即时查询直接返回事实，长任务调用 TaskStore.submit。
2. 新建独立工作流 handler，在 Worker 的工作流字典中注册，不给分发器新增算法专用分支。
3. 工作流经适配器执行外部程序，沿用阶段日志和外部 ID 持久化；无法确认状态时抛 ExternalStateUnknown。
4. 准备并校验结果文件，返回统一产物清单；worker 负责最终原子发布。新增产物类型应记录输入数据、模型/图谱配置、时间范围和实际文件。
5. API、模型和页面复用这些工具与引用。训练和图工具额外检查数据可用性与 T1/T2；task succeeded 不自动证明满足训练或图谱前置条件。

当前清洗数据通过 artifacts.get 查询，datasets.describe 尚只覆盖原始数据。下一轮直接读取清洗文件时，可复用 [CleanedDataset](../../src/movielens_agent/storage/cleaned.py) 的精确引用、哈希校验和显式分区过滤；可运行命令、固定数据版本及防止未来数据进入训练的方法见[清洗数据交接](../iterations/01-governance/handoff/清洗数据读取.md)。实际新增查询工具的注册示例见 [catalog_versions.py](../../scripts/examples/catalog_versions.py)。
