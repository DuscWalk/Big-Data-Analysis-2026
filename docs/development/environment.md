# 开发环境与本地运行

**已验证范围：** Python 包、原始核查与版本登记、工具注册、持久任务与独立 worker、HDFS/YARN 七作业治理、产物发布与证据查询。小数据通过手算断言；全量结果由迭代报告记录。模型主备适配、SQLite 会话与调用记录、HTTP API 和页面已接入，实际证据见[Agent 联调报告](../iterations/01-governance/reports/2026-09-25-Agent联调实测.md)。

## 环境与依赖

本机以 `duscwalk` 执行，Conda 环境为 `AgentDev`。当前 Python 3.11.16、Pydantic 2.13.5、FastAPI 0.141.1、httpx 0.28.1，实际依赖集合记录在 [requirements.lock](../../requirements.lock)；Conda 环境声明见 [environment.yml](../../environment.yml)。

```bash
source /home/duscwalk/miniconda3/etc/profile.d/conda.sh
conda activate AgentDev
python -c 'import sys; print(sys.executable); print(sys.version)'
```

本机解释器应为 `/home/duscwalk/miniconda3/envs/AgentDev/bin/python`。在新的开发环境中，可先执行 `conda env create -f environment.yml`；已存在的环境按需使用 `conda env update -n AgentDev -f environment.yml`，不要直接覆盖或删除他人的环境。

需要复现本次 Linux x86_64 的确切 Conda 包构建时，使用 [平台锁文件](../../environments/conda-linux-64.lock) 在尚不存在的环境中创建：

```bash
conda create -n AgentDev --file environments/conda-linux-64.lock
```

选择好环境后，在仓库根目录安装锁定的 Python 依赖与本地代码：

```bash
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pip check
```

新增业务依赖时同步更新 `pyproject.toml` 和锁文件；不同平台先按环境声明安装并记录实际兼容版本。锁文件不含模型凭据或运行数据。

## 数据核查与登记

将课程数据放到 `ml-1m/` 后执行：

```bash
python -m movielens_agent profile --data-dir ml-1m --output-dir var/profiles/run-001 --catalog var/catalog.sqlite3
```

该命令读取三个 `.dat` 文件，输出 `profile.json`、`manifest.json`，并向 SQLite 登记原始数据清单。输出目录必须是新目录且位于原始数据目录之外；再次核查使用新的运行目录，避免覆盖既有证据。省略 `--output-dir` 时自动生成新的运行目录。

标准输出包含 `dataset_ref`。同一内容产生相同版本，内容变化生成新版本；同一版本不可被不同清单覆盖。清单包含当前数据的本地路径，数据搬迁需要显式处理位置登记，首版不会悄悄替换现有记录。

## 通过工具注册表查询

复制核查命令输出的实际版本。当前副本可使用：

```bash
python -m movielens_agent describe --catalog var/catalog.sqlite3 --artifact-id ml-1m.raw --version sha256-46bfa0020d750da32d409f93c6cb305a1347019f8a703f7f6b943458ee0fa578
```

查询返回 `completed` 与清单、证据引用，或结构化的拒绝/失败原因；缺失版本不会回退到最新版本。它读取登记信息，不复查源文件是否在查询后改变；真正处理任务在消费原始文件前必须重新核对校验值。

Hadoop 安装与启动见 [Hadoop 指南](hadoop-local.md)。任务提交、后台执行、证据读取和扩展步骤见 [任务与工具指南](tasks-and-tools.md)。自然语言入口、主备模型配置、页面及会话 API 见 [Agent 与页面指南](app-and-model.md)。CLI、测试替身与真实 Agent 联调的证据分别记录。

## 验证

```bash
python -m unittest discover -s tests -v
```

测试使用临时的小型已知数据验证行为；正式原始数据核查结果见 [本轮报告](../iterations/01-governance/reports/2026-09-24-原始数据核查.md)。`var/` 包含生成报告、SQLite 数据库及临时产物，已由 Git 忽略。

返回 [文档导航](../README.md)。
