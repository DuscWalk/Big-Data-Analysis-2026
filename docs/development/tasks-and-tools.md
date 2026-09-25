# 任务、工具与产物开发

当前已实现共用工具注册、原始数据登记、任务受理、独立 worker、阶段记录和原子发布；持久会话、模型与前端已完成实际联调，见[应用指南](app-and-model.md)。本文说明底层 Python/CLI 行为和下一轮接入方式。

## 运行一个治理任务

先按[环境指南](environment.md)安装并登记原始数据，按[Hadoop 指南](hadoop-local.md)启动集群。在仓库根目录运行：

```bash
python -m movielens_agent submit --version sha256-46bfa0020d750da32d409f93c6cb305a1347019f8a703f7f6b943458ee0fa578 --request-id governance-001
python -m movielens_agent worker --once
python -m movielens_agent task --task-id TASK_ID
```

示例数据版本仅适用于当前课程副本，换数据时使用 profile 返回的实际版本。submit 返回 accepted 和 task_id，不返回未计算的分数。`--once` 处理队列中的一个任务；不加该参数则持续处理队列。正常应用将单独运行 worker，提交进程和页面无需等待 Hadoop。

task 返回真实状态、当前阶段、错误、所有阶段尝试、外部作业标识和发布产物。CLI 默认会话为 local-cli；需要区分上下文可在提交、查询时使用同一个 `--session-id`。这是应用内部范围隔离，尚不是 HTTP 身份认证。

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
| governance.run | job | dataset_ref，可选已登记 rule_ref/metric_ref；返回 task_ref |
| tasks.get | query | task_id；返回当前会话可见的状态与产物引用 |
| artifacts.list | query | task_id、offset/limit；每页最多 100 项 |
| artifacts.get | query | artifact_ref、mode、file_name、offset/limit；元数据、评分摘要、报告或有来源的 JSONL 样例 |

通用入口位于 [registry.py](../../src/movielens_agent/tools/registry.py)；保留 QueryTool 这个首版类型名，通过 mode 区分查询与任务受理。输入输出都经 Pydantic 校验，额外参数拒绝。模型只能填写业务参数；会话与 request_id 由应用传入 ToolContext。

配置默认从 [default.json](../../configs/governance/default.json)读取，配置内容哈希是实际版本。传入未登记的规则/评分引用会拒绝，不执行任意路径、脚本或 Shell 命令。

## 读取证据

从 task 的 artifacts 中复制精确引用：

```bash
python -m movielens_agent evidence --artifact-id ARTIFACT_ID --version ARTIFACT_VERSION --mode summary
python -m movielens_agent evidence --artifact-id CLEANED_ARTIFACT_ID --version ARTIFACT_VERSION --mode sample --file-name ratings.jsonl --limit 3
```

包含多个文件的产物须指定 file-name。JSONL 样例每次最多 20 行，返回 offset、has_more 和来源；完整处置与清洗数据不放进模型上下文。评分摘要取已发布 quality.json 中的真实事实。其他会话不能读取任务或产物；原始数据清单目前是项目公共数据。

## 下一轮的扩展位置

1. 用 Contract 定义业务输入和返回模型，在 registry 注册工具；即时查询直接返回事实，长任务调用 TaskStore.submit。
2. 新建独立工作流 handler，在 Worker 的工作流字典中注册，不给分发器新增算法专用分支。
3. 工作流经适配器执行外部程序，沿用阶段日志和外部 ID 持久化；无法确认状态时抛 ExternalStateUnknown。
4. 准备并校验结果文件，返回统一产物清单；worker 负责最终原子发布。新增产物类型应记录输入数据、模型/图谱配置、时间范围和实际文件。
5. API、模型和页面复用这些工具与引用。训练和图工具额外检查数据可用性与 T1/T2；task succeeded 不自动证明满足训练或图谱前置条件。

当前清洗数据通过 artifacts.get 查询，datasets.describe 尚只覆盖原始数据。下一轮直接读取清洗文件时，可复用 [CleanedDataset](../../src/movielens_agent/storage/cleaned.py) 的精确引用、哈希校验和显式分区过滤；可运行命令、固定数据版本及防止未来数据进入训练的方法见[清洗数据交接](../iterations/01-governance/handoff/清洗数据读取.md)。实际新增查询工具的注册示例见 [catalog_versions.py](../../scripts/examples/catalog_versions.py)。
