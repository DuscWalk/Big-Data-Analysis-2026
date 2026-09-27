# Git 协作与提交约定

本机 WSL 的日常开发、文件写入、Git 和 SSH 使用系统用户 `duscwalk`，提交身份为 `duscwalk <241276007@smail.nju.edu.cn>`。提交前通过 `git var GIT_AUTHOR_IDENT` 和 `git var GIT_COMMITTER_IDENT` 核对。其他成员在自己的电脑使用各自真实的 Git 身份和 SSH 密钥。

## 分支与同步

`main` 是集成与云端发布分支。迭代一从 `feat/iteration-01-foundation` 合入，基线标签为 `iteration-01-v1.0`；原功能分支保留历史，不再承担持续发布。后续从最新 `main` 创建有含义的 `feat/...`、`fix/...` 或 `docs/...` 分支。

以迭代二为例，在工作区干净时执行：

```bash
git fetch origin
git switch main
git pull --ff-only origin main
git switch -c feat/iteration-02-tools
```

独立功能完成后推送自己的分支，创建目标为 `main` 的 PR。描述问题、最终行为、验证结果，以及是否改变公共接口或数据版本。由另一位成员评审，CI 通过后由当前迭代负责人合并；共享分支不强制推送，不重写已发布历史。

截至本轮收尾，公开 GitHub 接口返回 `main.protected=false`；上述评审流程是团队约定，尚未通过分支保护强制执行。成员协作者邀请仍需其 GitHub 用户名；不要把尚未设置的权限或保护写成已启用。

## 自动检查与云端版本

push 和 pull request 会触发 GitHub CI，顺序检查 Python 3.11/3.12、小样本测试、前端语法及可安装包；真实 Hadoop 与模型调用不进入默认流水线。测试日志和安装包按提交 SHA 保存，保留 14 天。

个人分支 push → CI → PR 评审 → 合入 `main` → 合并提交的 push CI 成功 → 云端空闲时部署。PR 的 CI 成功不能代替合并提交的 CI。交付器每三分钟检查 `main` 的精确 SHA；有排队、运行任务或正在生成回答时延后发布。

云端是共享集成环境，没有为各功能分支自动创建预览环境。成员在本地开发并跑小样本检查，重计算验收统一协调时间。运行目录 `/srv/movielens/current` 由发布器管理，不是成员的工作区。控制脚本和 systemd 定义变更需要管理员另行安装，不能仅靠应用自动更新。具体操作见[华为云与 CI/CD](../deployment/README.md)。

## 提交范围

每个提交完成一件可审阅的事，例如文档目录整理、工具注册或查询接口。提交信息采用 `docs:`、`feat:`、`fix:`、`test:`、`ci:` 或 `chore:` 开头，说明具体变化。

提交前检查 `git status --short`、`git diff` 和 `git diff --check`；按明确路径暂存，再通过 `git diff --cached` 核对范围。代码变更附带相应验证与使用文档。

原始数据、课件、运行目录、缓存、环境文件和凭据由 `.gitignore` 排除。可复现的源码、依赖声明和脱离数据明细也可阅读的报告进入仓库；大型产物按固定引用与校验值交接。

## 里程碑与交接

迭代一标签 `iteration-01-v1.0` 固定本轮合并后的代码与文档；后续修复创建新的提交或标签，不移动该标签。查看基线：

```bash
git fetch origin --tags
git show --no-patch iteration-01-v1.0
git rev-parse 'iteration-01-v1.0^{commit}'
```

需要运行旧版本时可 `git switch --detach iteration-01-v1.0`；继续开发应切回自己的功能分支。云端实际版本以 `/srv/movielens/deployed.json` 为准，包含提交、上一版本和通过的 CI 地址。

每次交接给出代码基线、精确数据版本、运行证据、已知限制和后续接口；本轮入口见[交接说明](../iterations/01-governance/handoff/交接说明.md)。

返回 [文档导航](../README.md)。
