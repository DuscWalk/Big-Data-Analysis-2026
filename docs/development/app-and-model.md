# 本地 Agent、模型与页面

应用使用 AgentDev 中的 FastAPI、httpx 和普通 HTML/JavaScript。模型通过结构化工具调用访问已有工具注册表，Hadoop 工作流仍由独立 worker 执行。依赖版本见 [requirements.lock](../../requirements.lock)，安装步骤见[环境指南](environment.md)。

## 配置与连通性

在仓库根目录准备 `.env`，字段见 [.env.example](../../.env.example)。已有文件直接补充，不要用示例覆盖。配置由 python-dotenv 按数据读取，支持等号两侧空格；不要用 Shell `source .env`。系统环境变量优先于文件。

| 配置 | 用途 |
| --- | --- |
| `MODEL_URL`、`MODEL_NAME`、`MODEL_API_KEY` | 主服务 API 基址、实际模型 ID 与凭据；基址含 `/v1` 时保留该部分 |
| `MODEL_URL_BACKUP`、`MODEL_NAME_BACKUP`、`MODEL_API_KEY_BACKUP` | 可选备用服务，凭据独立 |
| `MODEL_PROVIDER` | 默认 `auto`；可固定为 `primary` 或 `backup` |
| `MODEL_TIMEOUT_SECONDS` | 每个服务单次请求的网络超时，默认 45 秒；当前本机为 90 秒 |
| `MODEL_MAX_TOKENS` | 输出上限，默认 4096；过小可能截断解释，不能把截断响应当成完成 |
| `AGENT_MAX_ROUNDS`、`AGENT_MAX_CALLS` | 默认每条消息最多 6 轮模型调用、12 次工具调用 |
| `AGENT_MAX_CONTEXT_CHARS` | 默认 100000 字符，包含提示、结构化工具定义与证据；超限明确失败 |
| `DEFAULT_DATASET_ID`、`DEFAULT_DATASET_VERSION` | 默认数据 ID 为 `ml-1m.raw`；存在多个版本时必须显式选择精确版本 |
| `TOOL_CALL_PARSER` | 服务端解析器提示，仅作记录，不发送为聊天请求参数 |

```bash
python -m movielens_agent model-probe --provider backup --list-models
python -m movielens_agent model-probe
```

第一条只列出模型 ID 和 HTTP 状态，第二条真实验证原生函数调用协议。2026-09-25 主服务返回 HTTP 502；备用服务列出的 `deepseek-v4-flash` 已通过工具调用测试，原填写的展示名称未出现在模型列表中。实际联调证据放在迭代报告中，测试替身不能证明服务可用。

`auto` 每轮按主、备顺序各尝试至多一次；只在模型调用失败时切换，成功后不再请求另一服务。记录服务角色、实际模型名、失败代码和耗时。切换发生在工具分发前，不重新执行已受理的任务。若主服务长期故障，可在本地设 `MODEL_PROVIDER=backup`；配置修改后重启 API 生效。

密钥只放在对应服务的 Authorization 请求头，不进入模型消息、调用记录或前端。客户端禁止 HTTP 重定向；异常只保存结构化代码，不记录原始响应体或请求头。`.env` 保持仅当前用户可读写，不能提交到 Git。对话、样例和查询结果仍会传给用户配置的模型服务，这是模型解释的正常数据范围。

## 启动与使用

先准备数据登记和[Hadoop](hadoop-local.md)。在三个终端分别执行：

```bash
# 终端一：已有存储只启动，不重复 init 或格式化
python scripts/hadoop/local_cluster.py serve

# 终端二：持续认领已受理任务
python -m movielens_agent worker

# 终端三：本地 API 和页面
python -m movielens_agent serve --port 8765
```

浏览器打开 <http://127.0.0.1:8765>。输入“请用默认规则清洗 MovieLens 1M 并评估前后质量”。模型返回的 job 工具成功受理后，应用直接用真实任务引用生成回执（`response_origin=application_receipt`），不再请求模型轮询。回复中的“受理”表示任务已入队，页面随后查询真实阶段；任务完成后展示五维指标、三表处置量、分母、时间划分、精确版本、报告下载和来源样例，并自动请求一次结果解释。API 请求不等待 Hadoop 完成。

模型回答未完成时，页面仍可查询任务与下载产物。点击“解释当前结果”并发送新消息可重新提问；重复旧 `request_id` 只返回旧结果，包括旧失败，不重新调用模型。任务状态 `unknown` 表示需要核查，不能直接当作失败重跑。

已有 CLI 任务归属 `local-cli`，可显式创建同名本地会话来查看，原任务的归属不会改动：

```bash
python -m movielens_agent session --session-id local-cli --title '课程治理实测'
```

再打开 <http://127.0.0.1:8765/?session=local-cli>。创建新会话、刷新页面、切换任务不会隐式迁移任务。停止 API 不停止 worker/Hadoop；所有进程在各自终端 Ctrl-C 停止。worker 执行中停止后的处理见[任务指南](tasks-and-tools.md)。

当前只有回环地址、同源检查和会话范围校验，**没有多用户登录或身份认证**；会话 ID 不是访问令牌。适用于可信本地开发，不能直接绑定公网。每个 catalog 只允许一个 API 进程和一个 worker，分别持有文件锁。

## 已实现的 HTTP 接口

统一前缀 `/api/v1`；根路径 `/` 是页面，`/static/` 提供静态资源。

| 接口 | 作用 |
| --- | --- |
| `GET /status` | 配置状态、登记数据版本与规则；不返回模型 URL 或密钥 |
| `POST /sessions`、`GET /sessions/{sid}` | 新建和查询本地会话 |
| `GET /sessions/{sid}/messages` | `offset` / `limit` 分页读取消息和调用摘要 |
| `POST /sessions/{sid}/messages` | `request_id`、`content`、可选 `task_id`；返回持久化回答或失败 |
| `GET /sessions/{sid}/messages/{mid}/calls` | 本轮工具参数、结果，以及已脱敏的模型请求、回复和切换记录 |
| `GET /sessions/{sid}/tasks`、`GET /sessions/{sid}/tasks/{tid}` | 本会话任务列表与真实执行状态 |
| `GET /sessions/{sid}/tasks/{tid}/quality` | 已成功发布的质量摘要，重新校验文件摘要 |
| `POST /sessions/{sid}/tasks/{tid}/explanation` | 对已完成任务生成一次有质量摘要依据的解释，稳定键为 `explain:{tid}` |
| `GET /sessions/{sid}/artifacts/{id}/versions/{version}` | `mode=metadata/summary/sample/examples`；`examples` 可用 `reason=ratings/R14_VALID_KEY_CONFLICT` 筛选 |
| 同上加 `/download?file_name=…` | 下载登记的具体文件，校验 SHA-256，不接收任意文件路径 |

`404` 表示不可见或不存在，`409` 表示会话忙、请求冲突或产物失效，`422` 表示参数错误，`503` 表示本轮回答失败。`GET task` 返回 HTTP 200 并不意味着任务成功，必须查看其 `status`。

同一会话只允许一条消息处理中。请求去重绑定文本、显式任务和解释要求；已完成请求返回原记录，修改请求内容却复用 ID 会冲突。参数经 Pydantic 规范化后构建稳定的工具请求键，模型更换 call ID 或显式补全默认值不会重复受理同一任务。

API 重启将未完成回答标为中断；若工具受理后尚未来得及记录返回值，通过稳定键找回已受理任务。API 不更改 worker 状态，不自动重发任务。后台任务的中断恢复由 worker 独立处理。

## 代码与扩展位置

- `agent/settings.py` 读取和脱敏配置；`agent/model.py` 适配原生 `tool_calls` 和备用切换。文本中的伪工具指令不会被执行。
- `agent/service.py` 组织有界调用循环；工具名称从注册表生成，对外把点映射成下划线，如 `governance.run` → `governance_run`。
- `storage/conversations.py` 保存消息、工具、模型和请求去重记录。模型和 Hadoop 执行期间不持有数据库事务。
- `api/app.py` 管理 HTTP、同源检查、会话范围、产物下载与静态页面；`web/` 位于 Python 包内，随包安装。
- 新算法通过 `tools/` 注册参数与结果协议，通过 `workflows/`、`adapters/` 接入实际执行。模型自动获得工具 schema；专用结果视图按需要添加，公共会话与任务机制继续复用。

绑定任务的回答至少要求本轮读取该任务的证据；自动结果解释还要求读取该任务精确质量版本的 summary。summary 附带 `interpretation_facts`：直接计算父表引用原因的命中总和、明确计数可重叠、给出评分去重数，并引用报告的分区存储说明。这些辅助事实不改变已发布的分数或报告。

该检查不等于验证模型每一句话。本次联调确实出现过原因合计错误与分区描述矛盾，已保留原回答和重新取证后的更正；文字中的公式单位仍需审校。正式分数、原始分子/分母和文件状态以产物为准，人工汇报应核对模型总结。

注册扩展示例位于 [catalog_versions.py](../../scripts/examples/catalog_versions.py)。它新增读取真实 catalog 的 `datasets.versions`，更新工具 schema 快照，复用原分发器、会话记录和页面调用依据，无需修改核心分发逻辑：

```bash
python scripts/examples/catalog_versions.py
```

该命令真实调用配置的模型，返回新会话 ID；打开页面并使用该 ID 可查看结果和调用依据。示例只在自身进程注册新工具，普通 API 启动不会自动加载它；团队正式新增算法时，应在应用初始化阶段注册对应工具，并在注册完毕后生成 schema。

## 验证

```bash
python -m unittest discover -s tests -v
```

模型协议、请求并发、会话隔离、凭据隔离、下载校验和中断恢复使用明确的测试替身或小数据。真实模型、Hadoop 和浏览器验收另行记录。

浏览器检查对已发布的真实任务运行，读取该任务的 API 指标作比较，不内置演示分数。只需额外的开发依赖：

```bash
npm install --prefix var/browser-check playwright@1.61.1
var/browser-check/node_modules/.bin/playwright install chromium
NODE_PATH="$PWD/var/browser-check/node_modules" MOVIELENS_SESSION_ID=local-cli node scripts/checks/browser_smoke.cjs
```

可通过 `MOVIELENS_TASK_ID` 选择具体任务，`MOVIELENS_BASE_URL` 更换本地端口。脚本验证页面、手机宽度、样例、下载校验和新会话清空；截图与结果保存在被忽略的 `var/verification/browser/`。页面打开已完成任务时可能调用真实模型解释，因此检查前配置好服务。


需要完整的自然语言验收时，在 API、Hadoop 和 worker 都已启动后执行：

```bash
NODE_PATH="$PWD/var/browser-check/node_modules" node scripts/checks/natural_language_e2e.cjs
```

该命令会使用真实模型、创建一个新会话、提交一次课程全量 Hadoop 任务，并等待自动解释与追问，可能运行十余分钟；结果保存在 `var/verification/natural-language/`。这是显式的集成验收入口，普通单元测试不会调用模型或启动 Hadoop。

返回[文档导航](../README.md)。
