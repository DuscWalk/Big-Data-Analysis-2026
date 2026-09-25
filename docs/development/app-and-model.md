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

模型回答未完成时，页面仍可查询任务与下载产物。点击“解释当前结果”可发送新问题；失败的报告解释另提供“重新解释此问题”和“查看该任务报告”。重新解释从服务端找回原问题、原任务及连续样例的位置，使用新请求 ID，保留原失败。重复传输相同重试 ID 只返回该次结果，模型故障不会自动连续重试。任务状态 `unknown` 表示需要核查，不能直接当作失败重跑。

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
| `POST /sessions/{sid}/messages/{mid}/retry` | 仅传新 `request_id`；重新解释失败回答的原问题、原任务与原样例位置 |
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
- `governance/explanation.py` 生成治理事实要点，`agent/explanation.py` 校验模型解释计划与本轮证据。
- `agent/service.py` 组织有界调用循环；工具名称从注册表生成，对外把点映射成下划线，如 `governance.run` → `governance_run`。
- `storage/conversations.py` 保存消息、工具、模型和请求去重记录。模型和 Hadoop 执行期间不持有数据库事务。
- `api/app.py` 管理 HTTP、同源检查、会话范围、产物下载与静态页面；`web/` 位于 Python 包内，随包安装。
- 新算法通过 `tools/` 注册参数与结果协议，通过 `workflows/`、`adapters/` 接入实际执行。模型自动获得工具 schema；专用结果视图按需要添加，公共会话与任务机制继续复用。

绑定任务的回答至少要求本轮读取该任务的证据。对已完成且有质量报告的治理追问，应用在第一次模型请求前，通过相同注册表读取本轮精确 summary；明确的异常规则、清洗文件、样例数和位置也会形成预读查询。每次预读重新校验文件哈希和会话范围，完整返回写入工具审计；`request_key` 以 `evidence:` 标明应用取证，模型请求工具沿用 `chat:`。新清洗任务的明确指令沿用原任务受理路径，不依赖旧报告可读。

当前解释策略为 `quality-facts-v3`。模型输入只保留解释需要的事实要点、样例描述和本轮调用 ID，完整结构化指标和原始样例留在审计与产物中；样例原始行不作为系统指令进入模型。中文使用正常 Unicode，避免让模型处理字面量 Unicode 转义。绑定任务仅带入最近两组同任务历史，过长历史答复节略至 600 字符，原记录不变。

模型最终返回 `quality_ref`、`sections`、`unsupported` 的 JSON 计划；提示中的示例随本题必要要点与无法确认标记生成，避免固定示例与当前要求冲突。应用除校验精确引用和本轮证据，还检查明确问题要求的主题、样例规则/文件/数量/位置及来源字段。已识别的明确主题只允许选择本题所需要点及必要依据，单一公式或样例问题不能扩展为报告概述；模型输入也按此范围裁剪。明确要求证明事实真实性或保证下游效果时，必须标记 `unsupported=true`。明确简短的回答默认至多 4 个要点、800 字符；复合主题或多条样例的必要预算会在 `requirements` 中明确增加，不能靠截断删除证据。完整解释仍覆盖评分、处置、时效、划分和局限；无匹配样例须明确无法满足，不能推断总体没有异常。

不符合要求时仅允许一次格式或覆盖修正，仍不满足则返回 `EXPLANATION_PLAN_INVALID`。应用取证失败返回 `EVIDENCE_UNAVAILABLE`，超过工具数量/位置限制返回 `EXPLANATION_REQUEST_UNSUPPORTED`。样例数量分配或指代不明确时，应用直接返回 `application_clarification`，不调用模型或查询工具。已绑定报告的解释及重试仅允许查询工具，执行层也拒绝 job 工具；明确的新清洗请求继续走任务受理流程。证据反馈与修正是 system 应用反馈，原始用户问题继续保留。

成功回答的 `response_origin=evidence_rendered`，页面显示“报告事实 · 要点选择模型”。`validation` 保存策略版本、精确报告、本轮摘要调用、要点来源、应用取证调用、问题要求、样例检查、实际字符数和被拒绝尝试。调用依据区分别标记 `application` 和 `model`，不把应用预读写成模型自主选择。来源样例由应用按工具原始返回生成，不能推算总体。

数量按明确来源分别绑定，支持“十一条”“第十一条”“两个评分样例和一个用户样例”、各来源共享数量及已有英文数词；每次每个来源支持 1—20 条、起始位置 1—10001。总量分配不明、范围或不定数量会要求澄清。“真实样例”本身不增加真实性证明主题。

“再给一个”“换一个例子”“再给两条”仅继承同一会话、任务和精确报告的最近成功样例，按实际返回条数推进。多来源时须指定其一；澄清消息不消耗游标。前页无样例、跨任务或缺少唯一来源时要求澄清。`validation.requirements.continuation_of` 记录来源消息，`sample_checks` 记录查询范围与实际数量。

显式重试冻结失败请求的要求与游标，后续消息和页面任务选择不改变重试目标；成功解释、非解释失败、任务未发布时拒绝此入口。兼容旧 v2 报告失败，按原问题重新解析。原报告版本改变或取证失效则明确失败。失败记录和自动解释缓存均保留。

当前解析仍是治理领域有边界的需求识别；复杂的隐含关系与未覆盖表达不保证识别。事实有来源不保证所有问题都切题，必须继续用重复实测检查。业务规则位于 `governance/questions.py` 和 `governance/explanation.py`，通用调用审计位于 `agent/service.py`；后续算法应提供自己的问题要求和证据解释策略。

原问题与执行顺序见[追问可靠性优化计划](../iterations/01-governance/plans/追问可靠性优化.md)，实际结果见[追问优化实测](../iterations/01-governance/reports/2026-09-25-追问可靠性优化实测.md)：三轮 21 次请求中 17 正确返回、4 次网关故障，受影响三类题各补测一次均通过；成功答案的样例与简短约束通过检查。历史的[备用服务实测](../iterations/01-governance/reports/2026-09-25-备用模型全链路实测.md)保留 v1 的样例遗漏与冗长问题，不能用新策略覆盖旧运行记录。

历史错误回答和缓存请求保持原样。新逻辑不会重写旧消息，也不会让同一 `explain:{tid}` 请求重新执行；阅读旧任务时，可通过新消息重新提问；符合条件的旧失败也可点击“重新解释此问题”，生成新的“报告事实”回答。汇报仍需核对采用的代码、报告与回答版本。

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


仅回归已有任务的真实模型解释时，先停止 API 和其他独立 Agent 脚本，再执行：

```bash
python scripts/checks/explanation_regression.py \
  --session-id 0db6090d62ba443595980aad6b2d0eab \
  --task-id 33ce0c793b6e4d92acb6400cd96b3faa
```

可追加 `--repeat 3 --expected-provider backup`，对七类问题逐轮创建新请求，记录模型轮数、输入 token、字符数、修正次数和时延；`--output` 必须选择没有既有结果的新目录。脚本持有与 API 相同的进程锁，防止服务启动把正在处理的独立回归消息标成中断；锁被占用时立即退出，不开始模型请求。它直接调用实际 AgentService 和模型，逐项检查完整解释、错误合计、公式单位、分区状态、无法确认的推断及来源样例，不需要启动 API/Hadoop/worker。每题创建新消息请求，保存原始调用与校验结果，并检查任务数没有增加；只适用于已经存在该会话/任务的本地 catalog，其他环境请替换为自己的引用。失败后可用 `--case complete` 等参数只重试对应题目，并为 `--output` 指定新目录保留旧记录。

独立回归结束后再启动 API，可在浏览器中验证一次真实追问、回答来源标签、调用依据和刷新持久性：

```bash
NODE_PATH="$PWD/var/browser-check/node_modules" \
  MOVIELENS_SESSION_ID=0db6090d62ba443595980aad6b2d0eab \
  MOVIELENS_TASK_ID=33ce0c793b6e4d92acb6400cd96b3faa \
  node scripts/checks/evidence_reply_browser.cjs
```

该检查会调用一次真实追问的模型循环，截图和结果默认写入 `var/verification/evidence-reply-browser/`；可设置 `MOVIELENS_BROWSER_OUTPUT` 改目录。它不提交治理任务。服务暂不可用时，另设 `MOVIELENS_MESSAGE_ID` 为已有真实回答 ID，可仅验证该持久化回答的显示、调用依据与刷新；结果标记为 `persisted-reply`，不能将其当作一次新的模型请求成功。

本轮数量与多轮追问回归使用 `scripts/checks/followup_variants.py`，同样需停止 API 并选择新输出目录：

```bash
python scripts/checks/followup_variants.py \
  --session-id 94c8e0627f004038b75fd28c94af2e37 \
  --task-id 064523f8a8bf41a38f7c2193caf59930 \
  --output var/verification/followup-variants
```

脚本要求配置为 `MODEL_PROVIDER=backup`，不新增治理任务；检查局部数量、来源、连续位置、澄清和三个简短变体。可用 `--case` 选择补测场景，连续追问必须同时包含前序场景；保留首测及补测目录。

`scripts/checks/retry_browser.cjs` 用 `MOVIELENS_SESSION_ID`、`MOVIELENS_TASK_ID`、`MOVIELENS_MESSAGE_ID` 选择真实失败回答，并通过页面按钮调用一次真实重新解释；还需设置新的 `MOVIELENS_BROWSER_OUTPUT` 目录。它核对原失败不变、原任务绑定、重复传输缓存、报告可读、刷新与手机宽度，不提交治理任务。可控故障交互另用 `retry_fixture.py --root <新目录> --port 8766` 启动隔离测试服务，再向浏览器检查传入 `MOVIELENS_RETRY_FIXTURE=<目录>/fixture.json`；其中模型与数据均为测试替身，不证明真实服务可用。

需要完整的自然语言验收时，在 API、Hadoop 和 worker 都已启动后执行：

```bash
NODE_PATH="$PWD/var/browser-check/node_modules" node scripts/checks/natural_language_e2e.cjs
```

该命令会使用真实模型、创建一个新会话、提交一次课程全量 Hadoop 任务，并等待自动解释与追问，可能运行十余分钟；结果保存在 `var/verification/natural-language/`。这是显式的集成验收入口，普通单元测试不会调用模型或启动 Hadoop。

仅使用备用服务验收时，在本地 `.env` 设置 `MODEL_PROVIDER=backup` 后重启 API，并在上述命令前增加 `MOVIELENS_EXPECT_PROVIDER=backup`。脚本会核对 API 当前配置，以及受理、自动解释和追问的每次模型尝试确实都来自备用服务；同时要求真实任务回执、任务成功、完整解释覆盖必要要点、追问引用本任务的质量报告且没有重提任务。各阶段的调用记录即时写入结果目录，失败记录保留，不仅检查 HTTP 200。

返回[文档导航](../README.md)。
