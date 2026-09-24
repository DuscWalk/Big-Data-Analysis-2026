#!/usr/bin/env python3
"""User-owned single-node HDFS/YARN lifecycle; no SSH or system service changes."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import time
import xml.etree.ElementTree as ET


def xml_file(path, values):
    root = ET.Element("configuration")
    for key, value in values.items():
        prop = ET.SubElement(root, "property")
        ET.SubElement(prop, "name").text = key
        ET.SubElement(prop, "value").text = str(value)
    ET.indent(root)
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


def configure(runtime, installation, java, python):
    runtime.mkdir(parents=True, exist_ok=True)
    conf = runtime / "conf"
    conf.mkdir(exist_ok=True)
    shutil.copyfile(installation / "etc/hadoop/log4j.properties", conf / "log4j.properties")
    for name in ("logs", "pids", "data/name", "data/data", "tmp", "nm-local", "nm-logs"):
        (runtime / name).mkdir(parents=True, exist_ok=True)
    xml_file(conf / "core-site.xml", {
        "fs.defaultFS": "hdfs://127.0.0.1:9000",
        "hadoop.tmp.dir": runtime / "tmp",
    })
    xml_file(conf / "hdfs-site.xml", {
        "dfs.replication": 1,
        "dfs.namenode.name.dir": (runtime / "data/name").as_uri(),
        "dfs.datanode.data.dir": (runtime / "data/data").as_uri(),
        "dfs.namenode.rpc-address": "127.0.0.1:9000",
        "dfs.namenode.http-address": "127.0.0.1:9870",
        "dfs.datanode.address": "127.0.0.1:9866",
        "dfs.datanode.http.address": "127.0.0.1:9864",
        "dfs.datanode.ipc.address": "127.0.0.1:9867",
        "dfs.datanode.hostname": "127.0.0.1",
        "dfs.client.use.datanode.hostname": "true",
    })
    xml_file(conf / "yarn-site.xml", {
        "yarn.resourcemanager.hostname": "127.0.0.1",
        "yarn.resourcemanager.bind-host": "127.0.0.1",
        "yarn.resourcemanager.webapp.address": "127.0.0.1:8088",
        "yarn.nodemanager.hostname": "127.0.0.1",
        "yarn.nodemanager.bind-host": "127.0.0.1",
        "yarn.nodemanager.webapp.address": "127.0.0.1:8042",
        "yarn.nodemanager.local-dirs": runtime / "nm-local",
        "yarn.nodemanager.log-dirs": runtime / "nm-logs",
        "yarn.nodemanager.aux-services": "mapreduce_shuffle",
        "yarn.nodemanager.resource.memory-mb": 3072,
        "yarn.nodemanager.resource.cpu-vcores": 4,
        "yarn.scheduler.minimum-allocation-mb": 256,
        "yarn.scheduler.maximum-allocation-mb": 3072,
        "yarn.nodemanager.vmem-check-enabled": "false",
        "yarn.nodemanager.env-whitelist": "JAVA_HOME,HADOOP_COMMON_HOME,HADOOP_HDFS_HOME,"
            "HADOOP_CONF_DIR,HADOOP_YARN_HOME,HADOOP_MAPRED_HOME,PATH,LANG,TZ",
        "yarn.nodemanager.delete.debug-delay-sec": 3600,
        "yarn.log-aggregation-enable": "true",
        "yarn.nodemanager.remote-app-log-dir": "/movielens/logs",
    })

    xml_file(conf / "capacity-scheduler.xml", {
        "yarn.scheduler.capacity.root.queues": "default",
        "yarn.scheduler.capacity.root.default.capacity": 100,
        "yarn.scheduler.capacity.root.default.maximum-capacity": 100,
        "yarn.scheduler.capacity.root.default.state": "RUNNING",
        "yarn.scheduler.capacity.root.default.acl_submit_applications": "*",
        "yarn.scheduler.capacity.maximum-am-resource-percent": 0.5,
    })
    xml_file(conf / "mapred-site.xml", {
        "mapreduce.framework.name": "yarn",
        "mapreduce.application.classpath": f"{installation}/share/hadoop/mapreduce/*,"
            f"{installation}/share/hadoop/mapreduce/lib/*",
        "yarn.app.mapreduce.am.env": f"HADOOP_MAPRED_HOME={installation}",
        "mapreduce.map.env": f"HADOOP_MAPRED_HOME={installation},TZ=UTC",
        "mapreduce.reduce.env": f"HADOOP_MAPRED_HOME={installation},TZ=UTC",
        "mapreduce.map.memory.mb": 768,
        "mapreduce.reduce.memory.mb": 768,
        "yarn.app.mapreduce.am.resource.mb": 768,
        "mapreduce.map.java.opts": "-Xmx256m",
        "mapreduce.reduce.java.opts": "-Xmx256m",
        "yarn.app.mapreduce.am.command-opts": "-Xmx384m",
        "mapreduce.task.io.sort.mb": 64,
        "mapreduce.map.speculative": "false",
        "mapreduce.reduce.speculative": "false",
        "mapreduce.map.maxattempts": 1,
        "mapreduce.reduce.maxattempts": 1,
        "mapreduce.input.fileinputformat.split.minsize": 134217728,
        "mapreduce.input.fileinputformat.split.maxsize": 268435456,
    })
    settings = {
        "JAVA_HOME": str(java), "HADOOP_HOME": str(installation),
        "HADOOP_CONF_DIR": str(conf), "HADOOP_LOG_DIR": str(runtime / "logs"),
        "HADOOP_PID_DIR": str(runtime / "pids"), "HADOOP_HEAPSIZE_MAX": "256",
        "HADOOP_MAPRED_HOME": str(installation), "YARN_HEAPSIZE": "256",
    }
    content = "\n".join(f"export {key}={shlex.quote(value)}" for key, value in settings.items()) + "\n"
    (conf / "hadoop-env.sh").write_text(content)
    (conf / "yarn-env.sh").write_text(content)
    (runtime / "env.sh").write_text(content)
    (runtime / "runtime.json").write_text(json.dumps({
        "hadoop_home": str(installation), "java_home": str(java), "conf_dir": str(conf),
        "python": str(python), "hdfs_root": "/movielens",
        "job_timeout_seconds": 1800,
    }, indent=2) + "\n")
    return os.environ | settings


def wait_for_hdfs(installation, env, timeout=90):
    """Daemon start returns before RPC and block reports are ready on a cold start."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                [installation / "bin/hdfs", "dfsadmin", "-safemode", "get"],
                env=env, capture_output=True, text=True,
                timeout=max(0.1, min(10, deadline - time.monotonic())))
            if result.returncode == 0 and "Safe mode is OFF" in result.stdout:
                print("HDFS RPC ready; safe mode is OFF.", flush=True)
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(max(0, min(2, deadline - time.monotonic())))
    raise RuntimeError("HDFS did not become ready; inspect the local NameNode and DataNode logs.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "start", "serve", "stop", "status"))
    parser.add_argument("--runtime", type=Path, default=Path("var/hadoop"))
    parser.add_argument("--hadoop-home", type=Path,
                        default=Path.home() / ".local/opt/hadoop-3.5.0")
    parser.add_argument("--java-home", type=Path, default=Path("/usr/lib/jvm/java-17-openjdk-amd64"))
    parser.add_argument("--python", type=Path,
                        default=Path.home() / "miniconda3/envs/AgentDev/bin/python")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Run as the normal development user, not root.")
    runtime, installation = args.runtime.resolve(), args.hadoop_home.resolve()
    if not (installation / "bin/hadoop").is_file() or not args.python.is_file():
        parser.error("Install Hadoop and the AgentDev interpreter first.")
    if args.command == "init":
        env = configure(runtime, installation, args.java_home.resolve(), args.python.resolve())
        name_dir = runtime / "data/name"
        if (name_dir / "current/VERSION").exists():
            print("Existing NameNode storage retained.")
            return
        if any(name_dir.iterdir()):
            parser.error("Nonempty unrecognized NameNode storage; refusing to format.")
        subprocess.run([installation / "bin/hdfs", "namenode", "-format", "-nonInteractive"],
                       env=env, check=True)
        return
    config = json.loads((runtime / "runtime.json").read_text())
    installation = Path(config["hadoop_home"])
    env = os.environ | {"JAVA_HOME": config["java_home"], "HADOOP_HOME": str(installation),
                        "HADOOP_CONF_DIR": config["conf_dir"],
                        "HADOOP_LOG_DIR": str(runtime / "logs"),
                        "HADOOP_PID_DIR": str(runtime / "pids")}
    daemons = [("hdfs", "namenode"), ("hdfs", "datanode"),
               ("yarn", "resourcemanager"), ("yarn", "nodemanager")]
    if args.command in ("start", "serve", "stop"):
        action = "stop" if args.command == "stop" else "start"
        if action == "start" and not (runtime / "data/name/current/VERSION").exists():
            parser.error("Initialize fresh NameNode storage with init first.")
        for binary, daemon in (daemons if action == "start" else reversed(daemons)):
            subprocess.run([installation / "bin" / binary, "--daemon", action, daemon],
                           env=env, check=True)
        if action == "start":
            wait_for_hdfs(installation, env)
            subprocess.run([installation / "bin/hdfs", "dfs", "-mkdir", "-p",
                            "/movielens", "/user/" + os.environ["USER"]],
                           env=env, check=True)

        if args.command == "serve":
            def interrupted(signum, frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, interrupted)
            signal.signal(signal.SIGINT, interrupted)
            print("Local Hadoop cluster running; Ctrl-C stops its four services.", flush=True)
            try:
                while True:
                    signal.pause()
            except KeyboardInterrupt:
                for binary, daemon in reversed(daemons):
                    subprocess.run([installation / "bin" / binary, "--daemon", "stop", daemon],
                                   env=env, check=False)
    else:
        subprocess.run([installation / "bin/hdfs", "dfsadmin", "-report"], env=env, check=True)
        subprocess.run([installation / "bin/yarn", "node", "-list"], env=env, check=True)


if __name__ == "__main__":
    main()
