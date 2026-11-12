# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Leader helper: health, roster, launch, send, wait, log. Stdlib only.

State lives under $LEADER_HOME (default ~/.local/share/leader):
  ledger/<host>.jsonl   append-only events, schema in references/ledger-v1.md
  runs/<run-id>/        brief.md, result.md, meta.json, stdout.log, followup-<n>.md
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("LEADER_HOME", "~/.local/share/leader")).expanduser()
CLASSES = ["impl", "review", "research", "bulk", "read", "write"]
OUTCOMES = ["done", "failed", "abandoned"]
SENTINEL = re.compile(r"^===AGENTDECK_DONE===\s+status=(ok|fail)\s+summary=(.*)$", re.M)
NEED = re.compile(r"^NEED:.*$", re.M)
LIMIT = re.compile(r"^.*(usage[ -]?limit|rate limit|hit your (?:[\w-]+ )?limit|quota exceeded|insufficient (balance|credit)).*$",
                   re.M | re.I)


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def die(msg, **extra):
    out({"error": msg, **extra})
    sys.exit(1)


def roster_path():
    for p in (os.environ.get("LEADER_ROSTER"), "~/.config/leader/roster.toml"):
        if p and Path(p).expanduser().is_file():
            return Path(p).expanduser()
    own = SKILL_DIR / "roster.toml"
    return own if own.is_file() else SKILL_DIR / "roster.example.toml"


def load_roster():
    with open(roster_path(), "rb") as f:
        return tomllib.load(f)


def host(roster=None):
    if os.environ.get("LEADER_HOST"):
        return os.environ["LEADER_HOST"]
    raw = socket.gethostname().split(".")[0]
    return (roster or load_roster()).get("hosts", {}).get(raw, raw)


def ledger_file(h):
    return HOME / "ledger" / f"{h}.jsonl"


def run_dir(run_id):
    return HOME / "runs" / run_id


def ad(*args, timeout=60):
    """Run agent-deck. Returns (ok, stdout)."""
    if not shutil.which("agent-deck"):
        return False, "agent-deck not on PATH"
    try:
        p = subprocess.run(["agent-deck", *args], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)
    return p.returncode == 0, p.stdout if p.returncode == 0 else (p.stderr or p.stdout)


def ad_json(*args, timeout=60):
    ok, text = ad(*args, "--json", timeout=timeout)
    if not ok:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def leader_identity():
    sid = os.environ.get("AGENTDECK_INSTANCE_ID")
    ident = {"session": sid, "harness": None, "model": None}
    sessions = ad_json("list", timeout=20) if sid else None
    for s in sessions or []:
        if s.get("id") == sid:
            ident["harness"] = s.get("tool")
            ident["model"] = s.get("model_id") or s.get("model")
    return ident


def read_ledger(h, limit=None):
    f = ledger_file(h)
    if not f.exists():
        return []
    rows = []
    for line in f.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:] if limit else rows


def append_event(h, event):
    f = ledger_file(h)
    f.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    with open(f, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(line + "\n")
        fcntl.flock(fh, fcntl.LOCK_UN)
    return f


# ---------- health ----------

def cmd_health(args):
    roster = load_roster()
    report = {"host": host(roster), "roster": str(roster_path()), "ledger": str(ledger_file(host(roster)))}
    ok, text = ad("list", "--json", timeout=20)
    report["agent_deck"] = "ok" if ok else f"down: {text.strip()[:200]}"
    ws = ad_json("inbox", "writer-status", timeout=20)
    report["notify_daemon"] = bool(ws and ws.get("running"))
    usage = ad_json("usage", timeout=30)
    report["usage"] = usage
    clis = {w: shutil.which(spec["direct"][0]) is not None for w, spec in roster["workers"].items()}
    report["direct_cli_available"] = clis
    if ok:
        report["mode"] = "normal"
    elif any(clis.values()):
        report["mode"] = "degraded"
    else:
        report["mode"] = "self-only"
    out(report)


# ---------- roster ----------

def cmd_roster(args):
    roster = load_roster()
    h = host(roster)
    events = read_ledger(h)
    regrades = {r["id"]: r["grade"] for r in events if r.get("type") == "regrade"}
    dispatches = [{**r, "grade": regrades.get(r["id"], r.get("grade"))}
                  for r in events if r.get("type") == "dispatch" and r.get("route") == "delegate"]
    recent = dispatches[-10:]
    explored = sum(1 for r in recent if r.get("explore"))
    share = roster.get("exploration_share", 0.2)
    workers = roster["workers"]
    classes = [args.cls] if args.cls else CLASSES
    stats = {}
    for r in dispatches:
        w = r.get("worker", {}).get("id")
        if not w or r.get("class") not in classes:
            continue
        s = stats.setdefault(w, {}).setdefault(r["class"], {"n": 0, "grades": [], "failed": 0, "abandoned": 0})
        s["n"] += 1
        if r.get("grade") is not None:
            s["grades"].append(r["grade"])
        s["failed"] += r.get("outcome") == "failed"
        s["abandoned"] += r.get("outcome") == "abandoned"
    result = {
        "host": h,
        "explore_now": len(recent) >= 3 and explored < round(share * len(recent)),
        "recent_explored": f"{explored}/{len(recent)}",
        "defaults": {c: roster.get("defaults", {}).get(c, []) for c in classes},
        "workers": {
            w: {
                "pool": spec.get("pool"),
                "available": shutil.which(spec["direct"][0]) is not None,
                "cap_usd": spec.get("cap_usd"),
                "free": spec.get("free", False),
                "note": spec.get("note"),
                "ledger": {
                    c: {
                        "n": s["n"],
                        "mean_grade": round(sum(s["grades"]) / len(s["grades"]), 2) if s["grades"] else None,
                        "failed": s["failed"],
                        "abandoned": s["abandoned"],
                    }
                    for c, s in stats[w].items()
                } if w in stats else None,
            }
            for w, spec in workers.items()
        },
    }
    out(result)


# ---------- launch ----------

CONTRACT = """

## Output contract (from your leader)
- Write your full result to: {result}
- Your final message: at most five lines of summary, then the line `RESULT: {result}`.
- If you are blocked and need a decision, start a line with `NEED:` and stop.
"""

DIRECT_SENTINEL = """- Print exactly this as the last line of your final message:
  ===AGENTDECK_DONE=== status=ok summary=<what you accomplished, one line>
  Use status=fail if you could not complete it; put the blocker in the summary.
"""


def cmd_launch(args):
    roster = load_roster()
    spec = roster["workers"].get(args.worker)
    if not spec:
        die(f"unknown worker {args.worker}", known=sorted(roster["workers"]))
    path = Path(args.path).expanduser().resolve()
    if not path.is_dir():
        die(f"path does not exist: {path}")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    rd = run_dir(run_id)
    rd.mkdir(parents=True)
    result = rd / "result.md"
    brief = Path(args.brief).expanduser().read_text() + CONTRACT.format(result=result) + DIRECT_SENTINEL
    (rd / "brief.md").write_text(brief)
    title = f"{args.title} [{args.worker}]"
    meta = {"run": run_id, "worker": args.worker, "path": str(path), "title": title,
            "started": now(), "result": str(result), "via": None, "session": None, "pid": None}

    via = None
    if not args.direct:
        before = ad_json("list", timeout=30)
        if before is None:
            meta["agent_deck_error"] = "agent-deck unavailable before launch; falling back to direct"
        else:
            cmd = ["launch", "-t", title, *spec["launch"], "-assert-done",
                   "-message-file", str(rd / "brief.md"), "-inherit-group"]
            if os.environ.get("AGENTDECK_INSTANCE_ID"):
                cmd += ["-parent", os.environ["AGENTDECK_INSTANCE_ID"]]
            if args.worktree:
                cmd += ["-w", args.worktree, "-b"]
            if args.cls:
                cmd += ["-tag", f"leader:{args.cls}"]
            meta["via"] = "agent-deck"
            (rd / "meta.json").write_text(json.dumps(meta, indent=2))
            ok, response = ad(*cmd, "--json", str(path), timeout=180)
            try:
                data = json.loads(response)
            except json.JSONDecodeError:
                data = None
            if ok and isinstance(data, dict) and data.get("success") and data.get("session_id"):
                via = "agent-deck"
                meta["session"] = data["session_id"]
            else:
                old_ids = {s.get("id") for s in before}
                candidates = [s for s in (ad_json("list", timeout=30) or [])
                              if s.get("id") not in old_ids and s.get("title") == title]
                if len(candidates) == 1:
                    meta["session"] = candidates[0].get("id")
                meta["launch_error"] = response or "agent-deck launch returned no result"
                (rd / "meta.json").write_text(json.dumps(meta, indent=2))
                die("agent-deck launch failed; inspect the recorded session before retrying", **meta)

    if via is None:
        if args.worktree:
            die("direct fallback cannot create the requested worktree; do the task yourself (route self)",
                run=run_id)
        exe = shutil.which(spec["direct"][0])
        if not exe:
            die("agent-deck launch failed and direct CLI is missing; do the task yourself (route self)",
                run=run_id)
        with open(rd / "stdout.log", "w") as log:
            p = subprocess.Popen([*spec["direct"], brief], cwd=path, stdout=log, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        via = "direct"
        meta["pid"] = p.pid

    meta["via"] = via
    (rd / "meta.json").write_text(json.dumps(meta, indent=2))
    out(meta)


# ---------- send ----------

FOLLOWUP = """

When you are done, update {result} and end your final message the same way as before:
  ===AGENTDECK_DONE=== status=ok summary=<what you changed, one line>
"""


def digest(text):
    return hashlib.sha256((text or "").encode()).hexdigest()


def cmd_send(args):
    rd = run_dir(args.run)
    meta = json.loads((rd / "meta.json").read_text())
    if meta["via"] != "agent-deck" or not meta.get("session") or meta.get("launch_error"):
        die("send needs a live agent-deck worker; launch a new one instead", run=args.run)
    message = Path(args.message_file).expanduser().read_text() if args.message_file else args.message
    if not message:
        die("give --message or --message-file")
    ok, last = ad("session", "output", meta["session"], timeout=30)
    if not ok:
        die("could not read the worker's last output", detail=last[:300])
    n = meta.get("followups", 0) + 1
    f = rd / f"followup-{n}.md"
    f.write_text(message + FOLLOWUP.format(result=meta["result"]))
    ok, err = ad("session", "send", "--message-file", str(f), meta["session"], timeout=660)
    if not ok:
        die("agent-deck send failed", detail=err[:300])
    meta["followups"] = n
    meta["seen_output"] = digest(last)
    (rd / "meta.json").write_text(json.dumps(meta, indent=2))
    out({"run": args.run, "session": meta["session"], "followups": n, "message": str(f)})


# ---------- wait ----------

def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def classify(text):
    m = None
    for m in SENTINEL.finditer(text or ""):
        pass
    if m:
        return ("done" if m.group(1) == "ok" else "fail"), m.group(2).strip()
    limits = [m.group(0).strip() for m in LIMIT.finditer(text or "")]
    if limits:
        return "limit", limits[-1][:300]
    needs = NEED.findall(text or "")
    if needs:
        return "needs_input", needs[-1]
    return None, None


def cmd_wait(args):
    meta = json.loads((run_dir(args.run) / "meta.json").read_text())
    result = Path(meta["result"])
    if meta.get("launch_error"):
        out({"run": args.run, "state": "error", "summary": meta["launch_error"],
             "via": meta["via"], "session": meta["session"], "result": str(result),
             "result_exists": result.exists()})
        return
    deadline = time.time() + args.timeout
    failures = 0
    state, summary, last_status = None, None, None
    while time.time() < deadline:
        if meta["via"] == "agent-deck":
            sessions = ad_json("list", timeout=30)
            ok, text = ad("session", "output", meta["session"], timeout=30)
            if sessions is None or not ok:
                failures += 1
                if failures >= 3:
                    state = "lost"
                    summary = "agent-deck stopped answering; check the result file, the child may still finish"
                    break
            else:
                failures = 0
                last_status = next((s.get("status") for s in sessions if s.get("id") == meta["session"]), "missing")
                if last_status in ("error", "missing"):
                    state, summary = "error", f"session status {last_status}"
                    break
                if last_status in ("waiting", "idle", "stopped"):
                    fresh = meta.get("seen_output") != digest(text)
                    state, summary = classify(text) if fresh else (None, None)
                    if state:
                        break
                    if last_status == "stopped":
                        state, summary = "fail", "session stopped without a completion line"
                        break
        else:
            log = (run_dir(args.run) / "stdout.log").read_text(errors="replace")
            state, summary = classify(log)
            if state:
                break
            if not pid_alive(meta["pid"]):
                state = "fail"
                summary = log.strip().splitlines()[-1][:300] if log.strip() else "process exited without a completion line"
                break
        time.sleep(args.interval)
    else:
        state, summary = "timeout", f"no completion after {args.timeout}s (last status {last_status})"
    out({"run": args.run, "state": state, "summary": summary, "via": meta["via"],
         "session": meta["session"], "result": str(result), "result_exists": result.exists()})


# ---------- log ----------

def cmd_log(args):
    roster = load_roster()
    h = host(roster)
    project = args.project or os.getcwd()
    if args.route == "self":
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        worker, started, via, followups = None, args.started, "self", None
    else:
        if not args.run:
            die("--run is required for a delegated dispatch")
        meta = json.loads((run_dir(args.run) / "meta.json").read_text())
        run_id, started, via = meta["run"], meta["started"], meta["via"]
        project = args.project or meta["path"]
        spec = roster["workers"].get(meta["worker"], {})
        worker = {"id": meta["worker"], "session": meta["session"], "pool": spec.get("pool")}
        followups = meta.get("followups", 0)
    if args.grade is not None and args.grade not in (1, 2, 3):
        die("grade must be 1, 2 or 3")
    event = {
        "v": 1, "type": "dispatch", "id": run_id, "ts": now(), "started": started, "host": h,
        "leader": leader_identity(), "route": args.route, "via": via, "worker": worker,
        "class": args.cls, "task": args.task, "project": project,
        "why": args.why, "explore": args.explore, "bakeoff": args.bakeoff,
        "verify": args.verify, "outcome": args.outcome, "grade": args.grade, "rework": args.rework,
        "followups": followups,
    }
    f = append_event(h, event)
    out({"logged": run_id, "file": str(f)})


def cmd_regrade(args):
    h = host()
    event = {"v": 1, "type": "regrade", "id": args.id, "ts": now(), "host": h,
             "by": args.by, "grade": args.grade, "note": args.note}
    f = append_event(h, event)
    out({"logged": args.id, "file": str(f)})


def main():
    ap = argparse.ArgumentParser(prog="leader")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health").set_defaults(fn=cmd_health)

    p = sub.add_parser("roster")
    p.add_argument("--class", dest="cls", choices=CLASSES)
    p.set_defaults(fn=cmd_roster)

    p = sub.add_parser("launch")
    p.add_argument("--worker", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--brief", required=True, help="file with the task brief")
    p.add_argument("--title", required=True)
    p.add_argument("--class", dest="cls", choices=CLASSES)
    p.add_argument("--worktree", help="new git worktree branch for the child")
    p.add_argument("--direct", action="store_true", help="skip agent-deck, run the CLI headless")
    p.set_defaults(fn=cmd_launch)

    p = sub.add_parser("send")
    p.add_argument("--run", required=True)
    p.add_argument("--message")
    p.add_argument("--message-file")
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("wait")
    p.add_argument("--run", required=True)
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--interval", type=int, default=20)
    p.set_defaults(fn=cmd_wait)

    p = sub.add_parser("log")
    p.add_argument("--route", choices=["delegate", "self"], default="delegate")
    p.add_argument("--run")
    p.add_argument("--class", dest="cls", choices=CLASSES, required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--why", required=True)
    p.add_argument("--outcome", choices=OUTCOMES, required=True)
    p.add_argument("--grade", type=int)
    p.add_argument("--verify")
    p.add_argument("--rework")
    p.add_argument("--project")
    p.add_argument("--explore", action="store_true")
    p.add_argument("--bakeoff")
    p.add_argument("--started", help="ISO time the work began (route self)")
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("regrade")
    p.add_argument("--id", required=True)
    p.add_argument("--grade", type=int, choices=[1, 2, 3], required=True)
    p.add_argument("--by", default="user")
    p.add_argument("--note")
    p.set_defaults(fn=cmd_regrade)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
