# 架构图件

图件对应 2026-09-27 核对的应用实现，基线为 `9732d72`。阅读说明、时序图、任务状态和源码索引见[系统架构详解](../系统架构详解.md)。

| 图件 | 内容 | 可用格式 |
| --- | --- | --- |
| 系统总览 | 进程、模块、主要调用、存储、Hadoop 及待实现的后续迭代 | [SVG](system-architecture.svg) / [PNG](system-architecture.png) / [PDF](system-architecture.pdf) |
| Hadoop 治理流程 | 输入准备、七个作业的输入输出、核对导出和发布 | [SVG](governance-pipeline.svg) / [PNG](governance-pipeline.png) / [PDF](governance-pipeline.pdf) |

SVG 适合放大阅读和矢量编辑；PNG 可直接放入幻灯片；PDF 保留矢量图形和可选择的文字，每图一页，使用与画布匹配的自定义纸张。总览画布为 2000 × 1980，流程图为 1800 × 2170。较多细节适合放大讲解。

## 图源与生成

图件布局和文字集中维护于 [render_architecture.py](../../../scripts/docs/render_architecture.py)，只依赖 Python 标准库。SVG 是可查看的导出文件，修改应回到脚本后统一生成，避免下次生成覆盖手工改动。

在仓库根目录运行：

```bash
python scripts/docs/render_architecture.py
```

图源采用原生 SVG 矩形、路径和文字，不引用外部图片、模型服务或应用接口。首选字体为 `Noto Sans CJK SC`；本机导出使用该字体，其他环境应准备同款中文字体以保持排版一致。

## PNG / PDF 导出

[export_diagrams.cjs](../../../scripts/docs/export_diagrams.cjs) 使用 Playwright Chromium 加载本地图件，等待字体就绪，检查模块框内文字是否越界，再生成对应 PNG 和 PDF。它不启动 API、模型或 Hadoop。

导出脚本使用 Node.js 运行，已验证的 Playwright 版本为 **1.61.1**。如需重新导出且没有现成 Playwright，可将文档工具安装到 Git 忽略的 `var/` 下：

```bash
npm install --prefix var/docs-export --no-save --package-lock=false playwright@1.61.1
var/docs-export/node_modules/.bin/playwright install chromium
NODE_PATH="$PWD/var/docs-export/node_modules" node scripts/docs/export_diagrams.cjs
```

如果已有可用的 Playwright，令 `NODE_PATH` 指向其 `node_modules` 即可。Python 应用的依赖无需因此改变。

## 修改后的检查

1. 按当前源码核对进程边界、作业输入输出、产物类型及已实现/待实现状态。
2. 重新生成两张 SVG，再导出 PNG/PDF；确认导出日志中 `clipped_text` 和 `browser_errors` 均为空。
3. 打开图件检查连线、中文、文字密度和阅读顺序。程序的文字边界检查不能代替目视检查。
4. 检查 PDF 每图一页、文字可提取，并同步更新架构文档及导航链接。

本次已完成上述检查；文档中的两张时序图和一张状态图另以 Mermaid 源码维护。
