"""Ensure a release wheel contains the Python package and browser assets only."""
from pathlib import Path
import sys
from zipfile import ZipFile


def check(path):
    with ZipFile(path) as archive:
        names = set(archive.namelist())
    required = {"movielens_agent/__init__.py", "movielens_agent/cli.py",
                "movielens_agent/web/index.html", "movielens_agent/web/app.js",
                "movielens_agent/web/manage.js", "movielens_agent/web/app.css"}
    missing = required - names
    forbidden = [name for name in names if Path(name).name.startswith(".env")
                 or name.endswith((".sqlite3", ".models.json"))
                 or name.startswith(("var/", "ml-1m/", "tests/"))]
    if missing or forbidden:
        raise SystemExit(f"Invalid wheel: missing={sorted(missing)}, forbidden={forbidden}")
    print(f"Verified {Path(path).name}: package and browser assets present; no runtime data.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/checks/check_wheel.py PACKAGE.whl")
    check(sys.argv[1])
