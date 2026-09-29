# 华为云部署与持续交付

云端单节点环境的 SSH、模型工具调用、样本七作业流程与页面回读已验证，见[首次部署记录](reports/2026-09-27-华为云首次部署.md)。迭代一收尾后发布目标统一为 `main`，固定基线为 `iteration-01-v1.0`；完整数据包和验收边界见[交接说明](../iterations/01-governance/handoff/交接说明.md)。

## 目标与已知环境

把普通检查迁移到 GitHub Actions，云端按通过检查的提交更新，保持数据和模型配置跨版本不变。开发环境用于轻量验证，不在部署准备时重复运行 Hadoop。

目标服务器使用 Ubuntu 24.04 或兼容发行版，优先选择 x86_64；管理员通过 SSH 公钥维护，应用和 Hadoop 使用独立的普通账户 `movielens`。主机名、SSH 别名和应用根目录由部署者自行配置，不写入仓库。

以下命令中的两个变量由部署者按实际环境设置；示例值仅表示格式。`APP_ROOT` 应与应用账户的主目录一致，systemd 模板用 `%h` 引用该目录：

```bash
CLOUD_HOST=your-ssh-alias
APP_ROOT=/opt/movielens
```

当前集成与发布分支为 `main`。脚本默认值和安装的 systemd unit 均指定该分支；个人功能分支只运行 CI，经评审合入 `main` 后才进入云端发布。实际运行提交及其 CI 地址以服务器 `deployed.json` 为准，首次部署报告中的功能分支信息属于当时记录。

## 资源选择

当前配置需要为 YARN 容器、四个 Hadoop 守护进程、系统、JVM 非堆、客户端和 API 预留资源；低于部署预检要求的主机会被拒绝。

| 配置 | 建议用途 |
| --- | --- |
| 4 vCPU / 8 GB / 80 GB SSD | 迭代一的建议起点；单 worker、计算串行、普通 CI 在 GitHub 上执行 |
| 4 vCPU / 16 GB / 100 GB SSD | 更推荐，预留后续机器学习、图分析和多版本产物空间 |

规格优先选择 **x86_64**。鲲鹏 ARM 的 4 核 / 16 GiB 容量也足够，但现有 Hadoop 下载校验文件及 Conda 平台锁是 x86_64，ARM 尚未适配和验证；不能直接套用下面的安装流程。Python 版本检查也不等于 ARM 架构检查。

这些是按当前资源配置和课程规模给出的容量建议，不是云端压力测试结果。后续算法与并发增加时需要再量测。

## CI

[CI 工作流](../../.github/workflows/ci.yml)在 push、pull request 与手动触发时运行：

1. Ubuntu 24.04 x86_64 上顺序检查 Python 3.11（本地）与 3.12（云端），使用 Node.js 22，安装锁定 Python 依赖。
2. 检查前端脚本语法，运行临时小样本单元/API/工作流测试。
3. 构建 wheel，检查 Python 代码和前端静态文件是否随包交付，以及是否混入数据或凭据。
4. 保存测试日志与成功构建的 wheel，名称包含提交 SHA 与 Python 版本，保留 14 天。

CI 明确清空 `MOVIELENS_HADOOP_RUNTIME`，不读取 `.env`，不运行真实 Hadoop 或模型调用。默认单个测试进程，同一分支的新提交取消旧的普通检查。真实 Hadoop 验证须独立按需安排，不能让每次文档或界面提交都启动整套计算。

## CD：服务器拉取通过检查的版本

[交付器](../../scripts/deploy/pull_release.py)与 [systemd 模板](../../deploy/systemd/)使用独立普通用户 `movielens`，其主目录由 `$APP_ROOT` 表示：

```text
$APP_ROOT/
├── bin/                  # 单独安装的交付器与 Hadoop 生命周期脚本
├── releases/<SHA>/       # 对应精确提交的 code/ 和 venv/
├── current -> releases/<SHA>
├── shared/
│   ├── .env              # 服务器自己的模型启动配置，权限 0600
│   ├── catalog.sqlite3   # 持久数据与会话
│   ├── catalog.models.json
│   ├── data/             # 原始数据，登记时使用这里的稳定绝对路径
│   ├── hadoop/           # 本项目独立的 Hadoop 配置和存储
│   ├── runs/             # 工作流运行与正式产物
│   ├── handoff/          # 已校验的固定全量数据交接包，独立于会话目录
│   └── backups/          # 切换前的 SQLite 备份
├── deploy.lock
└── deployed.json         # 已部署 SHA、上一版本与 CI 地址
```

定时器每三分钟检查一次。仅接受目标仓库、指定分支、当前 SHA 的 `push` 事件，且 `.github/workflows/ci.yml` 已完成并成功；PR、其他工作流、旧提交或失败/取消的检查不能部署。公开 GitHub API 失败或限流时保持当前版本，下轮再查。当前仓库公开，服务器可通过公开 API 拉取已通过检查的版本，无需配置 GitHub 管理令牌或向 GitHub 提供服务器 SSH 私钥。

交付器从 GitHub 官方 codeload 获取对应 SHA 的源码归档，校验顶层目录中的完整提交标识、解包路径、文件类型和大小，再准备独立虚拟环境并安装锁定依赖。任务排队、运行或模型请求处理中时延后更新。停止 API 时允许已接受的 HTTP 请求正常结束，并在停止后再次查询任务状态；发现新工作则恢复旧 API。没有待执行工作时才停止 worker、备份 SQLite、切换链接和启动新服务。Hadoop 服务不随每次应用发布重启。

新 API 健康检查或服务状态检查失败时回到旧代码。数据库、产物与模型配置始终位于 `shared/`，回退不自动恢复旧数据库以免丢弃新写入；以后包含不兼容数据库迁移的版本需单独安排迁移与回退，不能只依赖代码切换。部署期间不要通过管理员 CLI 绕过 API 提交新任务。

API、worker、Hadoop 和交付器分别设置内存与 CPU 上限。完整部署要求主机至少约 7 GiB 可见内存、交付时至少 512 MiB 可用，并已准备独立 Hadoop runtime、启动相应服务；不满足这些条件的主机会被部署预检拒绝。

## 首次安装顺序

以下步骤适用于符合资源要求的新服务器。Hadoop 使用本项目独立配置，系统、SSH 与华为云基础代理保留。

1. 管理员安装 Git、Python 3.12 的 venv 支持、OpenJDK 17、curl 和 tar；建立普通用户 `movielens`，主目录 `$APP_ROOT`，启用其 systemd 用户管理器与 linger。
2. 将经过 CI 的代码放到该用户的临时 bootstrap 目录；创建 `bin`、`shared` 和 `~/.config/systemd/user`。复制交付器、`scripts/hadoop/local_cluster.py` 和五份 unit 模板到相应位置，所有开发及运行文件归该用户。交付器固定在 `bin`，应用版本切换不会自动替换它。
3. 以 `movielens` 执行 bootstrap 中的 `scripts/hadoop/install.py`，安装校验锁定的 Hadoop 3.5.0；在应用账户的主目录执行以下命令，初始化**本项目新目录**并启动集群。Streaming 程序只依赖 Python 标准库，可使用服务器 `/usr/bin/python3`；解释器路径按目标环境配置。

```bash
python3 bin/local_cluster.py init --runtime shared/hadoop --python /usr/bin/python3
systemctl --user daemon-reload
systemctl --user enable --now movielens-hadoop.service
python3 bin/local_cluster.py status --runtime shared/hadoop --python /usr/bin/python3
```

4. 准备 `shared/.env`（0600）。先运行交付器的只读检查，确认 SHA 与 CI 地址；随后首次启用应用部署，再启用三分钟定时检查。

```bash
python3 bin/pull_release.py --base "$APP_ROOT" --branch main
python3 bin/pull_release.py --base "$APP_ROOT" --branch main --apply
systemctl --user enable movielens-api.service movielens-worker.service
systemctl --user enable --now movielens-deploy.timer
```

5. 原始课程数据传到 `shared/data/ml-1m` 后，使用当前版本的 Python 执行 `profile` 并把清单写入 `shared/catalog.sqlite3`。不要直接复制带有其他机器绝对源路径的登记记录；应在目标服务器重新登记。模型设置可通过 SSH 隧道打开页面后配置；不会把凭据写进仓库或 CI 日志。
6. 核对四个服务、部署 SHA、模型连通性，再顺序运行小样本 Hadoop 检查。首次云端验收通过后再考虑全量；日常开发继续使用轻量检查。

首次安装需要设置正确的 `XDG_RUNTIME_DIR` 和 `DBUS_SESSION_BUS_ADDRESS` 才能从管理员切换身份操作用户服务。这些值由实际 UID 决定；交付器运行时会自动设置。

本轮收尾已将控制脚本默认分支和 `movielens-deploy.service` 的 `ExecStart` 切换为 `main`。日后修改这些控制文件时，先通过 CI，暂停定时器并确认没有正在执行的交付，备份后显式安装新脚本和 unit、重新加载用户配置，完成只读检查和一次实际交付后再恢复定时器。应用代码更新不自动完成这些步骤。

成员从最新 `main` 建自己的分支，经 PR 评审合并；共享环境不直接编辑源码，也没有自动的功能分支预览环境。协作步骤见[Git 工作流](../development/git-workflow.md)。

## 访问与日常管理

部署者先设置自己的 SSH 别名和应用根目录，再确认 `$CLOUD_HOST` 能通过公钥连接服务器。在一个终端保持隧道：

```bash
ssh -N -L 127.0.0.1:18765:127.0.0.1:8765 "$CLOUD_HOST"
```

浏览器打开 <http://127.0.0.1:18765/>。示例使用本地 18765 转发到服务器 8765；本地端口被占用时可改成其他空闲端口。已有同端口隧道时直接使用，不要重复启动。关闭隧道不影响云端计算任务。

部署方案使用 UFW 默认拒绝入站、允许出站，只放行 TCP 22。API、HDFS 与 YARN 管理界面使用回环地址；Hadoop 的 Shuffle/AM 部分内部端口会监听所有网卡，由主机防火墙限制外部访问。不要为演示开放 Hadoop 端口。之后启用经过认证的 HTTPS 入口时再按需放行 443。

查看服务器运行版本：

```bash
ssh "$CLOUD_HOST" cat "$APP_ROOT/deployed.json"
```

连接服务器后，以应用账户查看服务。运行时目录由目标机器的实际 UID 计算：

```bash
APP_UID="$(id -u movielens)"
runuser -u movielens -- env XDG_RUNTIME_DIR="/run/user/$APP_UID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$APP_UID/bus" systemctl --user status movielens-api movielens-worker movielens-hadoop movielens-deploy.timer
```

交付日志由 systemd 保存，管理员可用 `journalctl _SYSTEMD_USER_UNIT=movielens-deploy.service --since today` 查询。`deployed.json` 保存当前提交、上一版本和通过的 CI 地址。定时器失败不影响正在运行的应用；检查网络、CI 状态和部署日志后再触发。

当前部署让应用监听回环地址，尚无用户身份认证。其他成员使用各自 SSH 公钥和独立账号建立隧道；成员公钥接入和公网域名尚未配置。模型设置、数据目录和历史会话共享，修改模型设置前先对齐；会话 ID 不构成用户安全边界。只需页面访问时可配置仅限端口转发的账号；取得数据包需要另行配置只读文件权限或由负责人提供，不能把隧道权限当作文件权限。若需要公网协作，再配置有身份认证的 HTTPS 入口。

## 自动检查记录

首次 CI 因 checkout 只保留一个提交，格式检查把整个仓库当作新文件，误报课程原文的历史换行。改为保留父提交后通过，课程原文未改写；Actions 使用当前 Node 24 运行时版本。随后针对云端 Git HTTPS 拉取超时，交付器改为官方提交归档下载。

[归档修正后的 CI](https://github.com/DuscWalk/Big-Data-Analysis-2026/actions/runs/36311402290)在 Python 3.11、3.12 上均通过。交付策略的 8 项小型测试覆盖精确 SHA/分支/工作流、繁忙延后、停止 API 时新任务到达、服务停止异常、成功切换、健康失败回退、首次失败及归档路径与版本校验。本地包构建检查也通过；真实云端运行见[首次部署记录](reports/2026-09-27-华为云首次部署.md)。

返回[文档导航](../README.md)。
