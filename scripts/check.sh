#!/bin/sh

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd) || exit 1
cd "$repo" || exit 1
status=0

stage() {
    name=$1
    shift
    if "$@"; then
        printf 'STAGE %s OK\n' "$name"
    else
        printf 'STAGE %s FAIL\n' "$name"
        status=1
    fi
}

stage tests uv run scripts/test_leader.py

stage roster uv run python - <<'PY'
import tomllib
from pathlib import Path

with Path("roster.example.toml").open("rb") as source:
    roster = tomllib.load(source)
workers = roster["workers"]
pools = roster["pools"]
for task, names in roster["defaults"].items():
    if not isinstance(names, list):
        raise ValueError(f"defaults.{task} must be a list")
    for name in names:
        if name not in workers:
            raise ValueError(f"defaults.{task}: unknown worker {name}")
for name, worker in workers.items():
    for field in ("pool", "launch", "direct"):
        if field not in worker:
            raise ValueError(f"{name}: missing {field}")
    if worker["pool"] not in pools:
        raise ValueError(f"{name}: unknown pool {worker['pool']}")
PY

stage private uv run python - <<'PY'
import os
from pathlib import Path
import re
import subprocess

files = subprocess.check_output(["git", "ls-files", "-co", "--exclude-standard", "-z"])
home = re.compile(rb"/(?:home|Users)/([^/\s]+)/")
failed = False
for raw in sorted(set(files.split(b"\0")) - {b""}):
    path = Path(os.fsdecode(raw))
    if path.name == "roster.toml":
        print(f"Private roster: {str(path)!r}")
        failed = True
    if not path.exists():
        continue
    try:
        content = path.read_bytes()
    except OSError as error:
        print(f"Cannot scan {str(path)!r}: {error}")
        failed = True
        continue
    if any(match.group(1) != b"you" for match in home.finditer(content)):
        print(f"Private home path in {str(path)!r}")
        failed = True
raise SystemExit(1 if failed else 0)
PY

exit "$status"
