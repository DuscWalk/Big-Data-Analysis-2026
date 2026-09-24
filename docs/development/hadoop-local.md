# 本机 Hadoop：安装、运行与排错

当前采用 Hadoop **3.5.0 + OpenJDK 17 + AgentDev Python 3.11.16**，真实 HDFS/YARN Streaming 验证见[开发计划](../iterations/01-governance/开发与交接计划.md)。Apache 的 [Java 兼容说明](https://cwiki.apache.org/confluence/display/HADOOP/Hadoop+Java+Versions)规定 3.5 服务端使用 JDK 17；[发行页面](https://hadoop.apache.org/releases.html)列明该版本于 2026-04-02 发布。

## 安装与配置

以日常用户执行以下命令；当前机器为 `duscwalk`。不要使用 root 运行守护进程或格式化存储。

```bash
conda activate AgentDev
python scripts/hadoop/install.py
python scripts/hadoop/local_cluster.py init
```

安装器读取[版本与 SHA-512 锁定信息](../../environments/hadoop-linux-x86_64.json)，下载约 555 MiB 的 Apache 二进制包，校验后解压至 `~/.local/opt/hadoop-3.5.0`。已有安装不会被覆盖。依赖系统已有的 JDK 17、curl 和 tar；Python 使用 AgentDev。

默认本机路径：

| 内容 | 位置 |
| --- | --- |
| Hadoop 二进制 | ~/.local/opt/hadoop-3.5.0 |
| Java | /usr/lib/jvm/java-17-openjdk-amd64 |
| Streaming Python | ~/miniconda3/envs/AgentDev/bin/python |
| Hadoop 配置、存储与日志 | var/hadoop/ |
| 应用运行配置 | var/hadoop/runtime.json |
| HDFS 项目空间 | /movielens |

其他机器在 `init` 时通过 `--hadoop-home`、`--java-home`、`--python`、`--runtime` 指定实际路径。生成的配置不进入 Git。存在 `data/name/current/VERSION` 时保留 NameNode 存储；非空但不能识别的存储会拒绝格式化。只有明确的 `init` 操作会初始化新存储。

重新生成配置后须重启集群才能生效。配置包含独立的 capacity-scheduler 队列；不能仅复制 core-site 和 yarn-site 就认为 YARN 可调度。

## 启动与停止

推荐在一个独立终端保持前台服务：

```bash
python scripts/hadoop/local_cluster.py serve
```

在另一终端检查，随后启动应用 worker：

```bash
python scripts/hadoop/local_cluster.py status
python -m movielens_agent worker
```

前台终端 Ctrl-C 会停止该项目的四个 Hadoop 服务。也可在普通持久终端使用 `start` / `stop` 子命令；某些工具执行器会清理命令结束后的子进程，这类环境使用 `serve`。关闭终端或 WSL 后，要重新启动集群。

健康检查应显示一个 Live DataNode 和一个 RUNNING YARN 节点；“Safe mode is OFF”本身不能证明 YARN 可用。

- NameNode 页面：http://127.0.0.1:9870
- ResourceManager 页面：http://127.0.0.1:8088
- NodeManager 页面：http://127.0.0.1:8042

HDFS RPC、DataNode 和管理界面绑定本机回环地址。本方案是本机单节点开发环境，不提供跨机器身份认证或高可用。

## 资源配置

本机约 7.6 GiB 内存，YARN 宣告 3072 MiB、4 vcores；Map、Reduce 与 AM 各申请 768 MiB 容器，Java 堆分别 256/256/384 MiB，额外空间供 Python 和 JVM 非堆内存。四个服务默认各用 256 MiB 堆。此为实验资源预算，不是进程峰值统计。

启用物理内存检查，关闭虚拟地址空间检查以适配 JVM/WSL；禁用推测执行并将单任务尝试设为 1，便于首版排错。评分表分组使用 2 个 reducer，汇总作业使用 1 个。作业输出保留在任务专属 HDFS 路径。

## 验证与日志

普通单元测试不要求 Hadoop。以下测试会实际执行七个小数据作业及一次故意使用不存在解释器的失败作业：

```bash
MOVIELENS_HADOOP_RUNTIME=var/hadoop/runtime.json python -m unittest discover -s tests -p test_hadoop_integration.py -v
```

小数据覆盖字段异常、确定非法候选、合法冲突、去重、外键连带移除和 T1/T2 边界，断言手算处置量、分数与实际发布结果。测试材料是人工构造的边界样例，正式报告另用课程全量数据。

查看任务和外部状态：

```bash
python -m movielens_agent task --task-id TASK_ID --external
```

`--external` 只查询记录中的 YARN application，不重提作业或改写任务状态。任务日志在 `var/runs/TASK_ID/logs/`；集群日志在 `var/hadoop/logs/`。Hadoop 作业标签包含任务 ID，便于在客户端中断、尚未保存 application ID 时定位。

需要人工检查 Hadoop 时：

```bash
source var/hadoop/env.sh
"$HADOOP_HOME/bin/yarn" application -list -appStates ALL
"$HADOOP_HOME/bin/yarn" application -status APPLICATION_ID
"$HADOOP_HOME/bin/yarn" logs -applicationId APPLICATION_ID
"$HADOOP_HOME/bin/hdfs" dfs -ls /movielens/tasks/TASK_ID
```

不要删除任务路径或重新格式化 NameNode 来“修复”任务状态。任务为 unknown 时，先结合外部状态、日志及输出核实；当前版本没有自动接续或自动发布中断任务的能力。确认旧作业终止后，重新运行使用新的 request ID，保留原任务审计记录。

返回[环境指南](environment.md)。
