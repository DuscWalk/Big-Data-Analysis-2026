# 华为云部署与持续交付

## 目标与已知环境

把普通检查迁移到 GitHub Actions，华为云按通过检查的提交更新，保持数据和模型配置跨版本不变。WSL 继续用于开发与轻量验证，不在部署准备时重复运行 Hadoop。

2026-09-27 已更换为 Ubuntu 24.04、x86_64、4 vCPU、16 GiB、100 GiB SSD 的新服务器，主机名 `ecs-7f2c-20a6`。本机 SSH 别名仍为 `HuaweiCloud`，使用 `duscwalk` 的公钥登录；旧主机指纹已备份并替换，新机密码登录已关闭。服务器管理使用 root，应用和 Hadoop 使用独立的普通账户 `movielens`。

最初核查的旧实例为 2 核 / 2 GB，已有其他实验服务且无本项目 YARN 环境；新服务器容量已满足下面的建议，不需要清理旧实例的服务来腾出资源。

当前开发分支为 `feat/iteration-01-foundation`，`main` 尚未合入本轮实现。部署目标必须显式指定分支与提交；本轮不自动合并主分支或创建发布标签。

## 资源选择

即使删除旧服务，现有 2 核 / 2 GB 也不足以按当前配置运行完整系统：YARN 容器预算为 3072 MiB，四个 Hadoop 守护进程默认各 256 MiB Java 堆，尚需系统、JVM 非堆、客户端和 API 的余量。

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

[交付器](../../scripts/deploy/pull_release.py)与 [systemd 模板](../../deploy/systemd/)使用独立普通用户 `movielens`，其主目录为 `/srv/movielens`：

```text
/srv/movielens/
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
│   └── backups/          # 切换前的 SQLite 备份
├── deploy.lock
└── deployed.json         # 已部署 SHA、上一版本与 CI 地址
```

定时器每三分钟检查一次。仅接受目标仓库、指定分支、当前 SHA 的 `push` 事件，且 `.github/workflows/ci.yml` 已完成并成功；PR、其他工作流、旧提交或失败/取消的检查不能部署。公开 GitHub API 失败或限流时保持当前版本，下轮再查。当前仓库公开，本机没有 GitHub API 管理令牌，因此采用服务器拉取，无需把现有 SSH 私钥交给 GitHub。

交付器从 GitHub 官方 codeload 获取对应 SHA 的源码归档，校验顶层目录中的完整提交标识、解包路径、文件类型和大小，再准备独立虚拟环境并安装锁定依赖。任务排队、运行或模型请求处理中时延后更新。停止 API 时允许已接受的 HTTP 请求正常结束，并在停止后再次查询任务状态；发现新工作则恢复旧 API。没有待执行工作时才停止 worker、备份 SQLite、切换链接和启动新服务。Hadoop 服务不随每次应用发布重启。

新 API 健康检查或服务状态检查失败时回到旧代码。数据库、产物与模型配置始终位于 `shared/`，回退不自动恢复旧数据库以免丢弃新写入；以后包含不兼容数据库迁移的版本需单独安排迁移与回退，不能只依赖代码切换。部署期间不要通过管理员 CLI 绕过 API 提交新任务。

API、worker、Hadoop 和交付器分别设置内存与 CPU 上限。完整部署要求主机至少约 7 GiB 可见内存、交付时至少 512 MiB 可用，并已准备独立 Hadoop runtime、启动相应服务；现有 2 GB 机器会被部署预检拒绝。

## 首次安装顺序

以下步骤适用于符合资源要求的新服务器。Hadoop 使用本项目独立配置，系统、SSH 与华为云基础代理保留。

1. 管理员安装 Git、Python 3.12 的 venv 支持、OpenJDK 17、curl 和 tar；建立普通用户 `movielens`，主目录 `/srv/movielens`，启用其 systemd 用户管理器与 linger。
2. 将经过 CI 的代码放到该用户的临时 bootstrap 目录；创建 `bin`、`shared` 和 `~/.config/systemd/user`。复制交付器、`scripts/hadoop/local_cluster.py` 和五份 unit 模板到相应位置，所有开发及运行文件归该用户。交付器固定在 `bin`，应用版本切换不会自动替换它。
3. 以 `movielens` 执行 bootstrap 中的 `scripts/hadoop/install.py`，安装校验锁定的 Hadoop 3.5.0；用以下命令初始化**本项目新目录**并启动集群。Streaming 程序只依赖 Python 标准库，可使用服务器 `/usr/bin/python3`，无需在云端复制本机 Conda 绝对路径。

```bash
python3 bin/local_cluster.py init --runtime shared/hadoop --python /usr/bin/python3
systemctl --user daemon-reload
systemctl --user enable --now movielens-hadoop.service
python3 bin/local_cluster.py status --runtime shared/hadoop --python /usr/bin/python3
```

4. 准备 `shared/.env`（0600）。先运行交付器的只读检查，确认 SHA 与 CI 地址；随后首次启用应用部署，再启用三分钟定时检查。

```bash
python3 bin/pull_release.py --base /srv/movielens
python3 bin/pull_release.py --base /srv/movielens --apply
systemctl --user enable movielens-api.service movielens-worker.service
systemctl --user enable --now movielens-deploy.timer
```

5. 原始课程数据传到 `shared/data/ml-1m` 后，使用当前版本的 Python 执行 `profile` 并把清单写入 `shared/catalog.sqlite3`。不要直接复制本机带有 `/home/duscwalk/...` 源路径的登记记录。模型设置可通过 SSH 隧道打开页面后配置；不会把凭据写进仓库或 CI 日志。
6. 核对四个服务、部署 SHA、模型连通性，再顺序运行小样本 Hadoop 检查。首次云端验收通过后再考虑全量；本机 WSL 不再陪跑同一计算。

首次安装需要设置正确的 `XDG_RUNTIME_DIR` 和 `DBUS_SESSION_BUS_ADDRESS` 才能从管理员切换身份操作用户服务。这些值由实际 UID 决定；交付器运行时会自动设置。

功能分支合并后，可通过 `movielens-deploy.service` 的 `ExecStart` 显式改为 `--branch main`，重新加载用户 unit。不要把服务器上的源码目录作为其他成员的开发工作区；成员提交分支、CI 通过，再按团队选定的发布分支交付。

当前应用只接受本机地址，尚无用户身份认证。部署初期可通过 SSH 隧道访问回环端口；若要提供公网协作入口，需要对接经过身份认证的 HTTPS 入口，不能直接开放模型配置与任务 API。

返回[文档导航](../README.md)。
