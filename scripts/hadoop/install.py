#!/usr/bin/env python3
"""Install the checksum-pinned Hadoop release into the current user's directory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache/bigdata")
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local/opt")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Use the normal development user, not root.")
    lock = json.loads((Path(__file__).resolve().parents[2] /
                       "environments/hadoop-linux-x86_64.json").read_text())
    args.cache.mkdir(parents=True, exist_ok=True)
    args.prefix.mkdir(parents=True, exist_ok=True)
    archive = args.cache / f"hadoop-{lock['version']}.tar.gz"
    installation = args.prefix / f"hadoop-{lock['version']}"
    if not archive.exists():
        partial = archive.with_suffix(".gz.partial")
        subprocess.run(["curl", "-fL", "--retry", "2", "--connect-timeout", "30",
                        "--max-time", "1800", "-C", "-", lock["url"], "-o", str(partial)], check=True)
        partial.rename(archive)
    with archive.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha512").hexdigest()
    if checksum != lock["sha512"]:
        parser.error("Archive SHA-512 mismatch; no extraction performed.")
    if installation.exists():
        if not (installation / "bin/hadoop").is_file():
            parser.error("Existing installation is incomplete; inspect it before selecting a new prefix.")
        print(f"Archive verified; existing installation retained: {installation}")
        return
    with tempfile.TemporaryDirectory(prefix=".hadoop-", dir=args.prefix) as temporary:
        subprocess.run(["tar", "-xzf", str(archive), "-C", temporary], check=True)
        (Path(temporary) / installation.name).rename(installation)
    print(f"Installed verified Hadoop {lock['version']}: {installation}")


if __name__ == "__main__":
    main()
