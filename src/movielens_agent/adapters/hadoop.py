"""HDFS/YARN Streaming adapter using argument lists, fixed programs and job tags."""
import os
from pathlib import Path
import json
import re
import selectors
import shlex
import subprocess
import time

from pydantic import Field

from ..contracts import Contract
from ..jobs.worker import ExternalStateUnknown


class HadoopRuntime(Contract):
    hadoop_home: Path
    java_home: Path
    conf_dir: Path
    python: Path
    hdfs_root: str = Field(pattern=r"^/[a-zA-Z0-9/_-]+$")
    job_timeout_seconds: int = Field(ge=10, le=86400)

    @classmethod
    def read(cls, path):
        return cls.model_validate_json(Path(path).read_text())


class Hadoop:
    def __init__(self, runtime: HadoopRuntime):
        self.runtime = runtime
        self.env = os.environ | {
            "JAVA_HOME": str(runtime.java_home), "HADOOP_HOME": str(runtime.hadoop_home),
            "HADOOP_CONF_DIR": str(runtime.conf_dir),
            "HADOOP_MAPRED_HOME": str(runtime.hadoop_home), "HADOOP_HEAPSIZE_MAX": "256",
            "LANG": "C.UTF-8", "TZ": "UTC",
        }
        jars = list(runtime.hadoop_home.glob("share/hadoop/tools/lib/hadoop-streaming-*.jar"))
        if len(jars) != 1:
            raise ValueError("Expected exactly one Hadoop Streaming jar.")
        self.streaming_jar = jars[0]

    def command(self, binary, arguments):
        return [str(self.runtime.hadoop_home / "bin" / binary), *map(str, arguments)]

    def fs(self, *args):
        result = subprocess.run(self.command("hdfs", ["dfs", *args]), env=self.env,
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(f"HDFS command failed: {result.stderr[-2000:]}")
        return result.stdout

    def put(self, local, remote):
        self.fs("-put", str(local), remote)

    def fetch(self, remote_directory, destination):
        self.fs("-test", "-e", remote_directory + "/_SUCCESS")
        with Path(destination).open("xb") as stream:
            result = subprocess.run(self.command("hdfs", ["dfs", "-cat",
                                                        remote_directory + "/part-*"]),
                                    env=self.env, stdout=stream, stderr=subprocess.PIPE,
                                    timeout=180)
        if result.returncode:
            raise RuntimeError(f"Cannot fetch Hadoop output: {result.stderr.decode()[-2000:]}")

    def application_status(self, application_id):
        if not re.fullmatch(r"application_[0-9]+_[0-9]+", application_id):
            raise ValueError("Invalid YARN application identifier.")
        try:
            result = subprocess.run(self.command("yarn", ["application", "-status", application_id]),
                                    env=self.env, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"application_id": application_id, "state": "UNKNOWN", "error": str(error)}
        text = result.stdout + result.stderr
        state = re.search(r"^\s*State\s*:\s*(\S+)", text, re.M)
        final = re.search(r"^\s*Final-State\s*:\s*(\S+)", text, re.M)
        return {"application_id": application_id,
                "state": state.group(1) if state else "UNKNOWN",
                "final_state": final.group(1) if final else None,
                "error": text[-1000:] if result.returncode else None}

    def run_job(self, *, task_id, stage, inputs, output, files, mapper, reducer,
                log_path, on_identifier, reducers=1):
        command = self.command("hadoop", [
            "jar", self.streaming_jar,
            "-D", f"mapreduce.job.name=movielens-{task_id}-{stage}",
            "-D", f"mapreduce.job.tags=movielens,{task_id}",
            "-D", f"mapreduce.job.reduces={reducers}",
            "-D", "mapreduce.output.textoutputformat.separator=",
            "-files", ",".join(Path(path).resolve().as_uri() + "#" + alias
                              for alias, path in files.items()),
        ])
        for path in inputs:
            command += ["-input", path]
        command += ["-output", output, "-mapper", shlex.join(mapper),
                    "-reducer", shlex.join(reducer)]
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        applications, identifiers = set(), set()
        tail = ""
        with Path(log_path).open("xb") as log:
            log.write((json.dumps({"argv": command}, ensure_ascii=True) + "\n").encode())
            log.flush()
            process = subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT)
            started = time.monotonic()
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while selector.get_map():
                        if time.monotonic() - started > self.runtime.job_timeout_seconds:
                            raise ExternalStateUnknown(
                                f"Timeout at {stage}; external jobs may still run. Inspect {log_path}.")
                        for key, _ in selector.select(timeout=1):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            log.write(chunk)
                            log.flush()
                            tail = (tail + chunk.decode("utf-8", errors="replace"))[-8192:]
                            for identifier in re.findall(r"(?:application|job)_[0-9]+_[0-9]+", tail):
                                if identifier not in identifiers:
                                    on_identifier(identifier)
                                    identifiers.add(identifier)
                                if identifier.startswith("application_"):
                                    applications.add(identifier)
                returncode = process.wait(timeout=10)
            except BaseException as error:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                if isinstance(error, (KeyboardInterrupt, SystemExit, ExternalStateUnknown)):
                    raise
                raise ExternalStateUnknown(
                    f"Submission monitoring interrupted at {stage}: {error}; inspect {log_path}.") from error
            finally:
                process.stdout.close()
        if returncode != 0:
            statuses = [self.application_status(identifier) for identifier in applications]
            terminal_failure = statuses and all(status["state"] in {"FINISHED", "FAILED", "KILLED"}
                and status.get("final_state") in {"FAILED", "KILLED"} for status in statuses)
            if terminal_failure:
                raise RuntimeError(f"Hadoop stage {stage} failed; see {log_path}.")
            raise ExternalStateUnknown(
                f"Hadoop client exited {returncode} at {stage}; verify external state in {log_path}.")
        if not applications:
            raise RuntimeError("No YARN application identifier: refusing non-YARN execution.")
        self.fs("-test", "-e", output + "/_SUCCESS")
        return sorted(identifiers)
