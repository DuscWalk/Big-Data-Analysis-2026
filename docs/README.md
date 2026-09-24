# 文档导航

文档按课程来源、整体架构、迭代实施和开发指南分层。课程原文保持原貌；设计建议和实际进展在对应的实施文档更新。

| 目录 | 维护内容 | 入口 |
| --- | --- | --- |
| `course/` | 课程提供的任务与汇报要求 | [总体要求](course/引言_项目总体要求与汇报安排.md)、[迭代一原始要求](course/迭代一_Hadoop数据清洗与Agent基础.md) |
| `architecture/` | 跨三轮复用的模块职责和扩展边界 | [Agent 整体架构](architecture/Agent整体架构与迭代边界.md) |
| `iterations/01-governance/` | 第一轮需求、接口、数据规则、开发计划和实际报告 | [迭代一导航](iterations/01-governance/README.md) |
| `development/` | 环境复现与协作方法 | [开发环境](development/environment.md)、[Hadoop](development/hadoop-local.md)、[任务与工具](development/tasks-and-tools.md)、[Git 工作流](development/git-workflow.md) |

建议依次阅读课程要求、整体架构、当前迭代的需求和技术方案。核查或运行报告放在该迭代的 `reports/` 中，代码、模型和大规模运行产物通过报告中的版本与位置引用。

后续迭代按 `iterations/02-analysis/`、`iterations/03-knowledge-graph/` 扩展，实际开始时再创建。共用接口变化同步更新整体架构和受影响的迭代文档。
