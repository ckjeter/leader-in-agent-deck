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
SENTINEL = re.compile(r"^[ \t]*===AGENTDECK_DONE===\s+status=(ok|fail)\s+summary=(.*)$", re.M)
NEED = re.compile(r"^[ \t]*(NEED:.*)$", re.M)
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


# ---------- pool usage ----------

WINDOW_CAPS = {"5h": 80, "7d": 90}


def window_label(minutes):
    return {300: "5h", 10080: "7d"}.get(minutes, f"{minutes}m")


def find_key(obj, key):
    if isinstance(obj, dict):
        if isinstance(obj.get(key), dict):
            return obj[key]
        obj = list(obj.values())
    if isinstance(obj, list):
        for item in obj:
            found = find_key(item, key)
            if found:
                return found
    return None


def codex_usage():
    """Codex writes the account's rate limits into every session file. Read the newest one."""
    root = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
    try:
        files = sorted(root.glob("*/*/*/rollout-*.jsonl"), key=lambda f: f.stat().st_mtime)[-5:]
    except OSError:
        return None
    for f in reversed(files):
        try:
            lines = [line for line in f.read_bytes().splitlines() if b'"rate_limits"' in line]
        except OSError:
            continue
        for line in reversed(lines):
            try:
                limits = find_key(json.loads(line), "rate_limits")
            except json.JSONDecodeError:
                continue
            windows = [{"label": window_label(w.get("window_minutes")), "used_percentage": w.get("used_percent"),
                        "resets_at": w.get("resets_at")}
                       for w in ((limits or {}).get("primary"), (limits or {}).get("secondary")) if w]
            if windows:
                return {"windows": windows, "as_of": int(f.stat().st_mtime), "source": "codex session files"}
    return None


def pool_usage(roster, events):
    """Per pool: what the provider says is used, when a worker last hit a limit, and whether to dispatch."""
    budget = roster.get("budget", {})
    hold = budget.get("limit_hold_minutes", 300) * 60
    bake_cap = budget.get("bakeoff_max", 60)
    current = time.time()
    deck = None
    report = {}
    for name, spec in roster.get("pools", {}).items():
        gauge = str(spec.get("gauge", ""))
        entry = {"windows": None, "as_of": None, "source": None}
        if gauge.startswith("agent-deck"):
            deck = deck if deck is not None else (ad_json("usage", timeout=30) or {})
            for p in deck.get("providers", []):
                if p.get("id") == spec.get("provider", name):
                    entry = {"windows": [{k: w.get(k) for k in ("label", "used_percentage", "resets_at")}
                                         for w in p.get("windows", [])],
                             "as_of": p.get("updated_at"), "source": "agent-deck usage"}
        elif gauge == "codex-sessions":
            entry = codex_usage() or entry
        why, room = [], True
        for w in entry["windows"] or []:
            used = w.get("used_percentage")
            if used is None:
                continue
            if w.get("resets_at") and w["resets_at"] <= current:
                w["reset_since_reading"] = True
                continue
            if used >= budget.get(f"max_{w['label']}", WINDOW_CAPS.get(w["label"], 90)):
                why.append(f"{w['label']} window at {used}%")
            room = room and used < bake_cap
        hits = [e for e in events if e.get("type") == "pool_limit" and e.get("pool") == name]
        entry["last_limit"] = hits[-1]["ts"] if hits else None
        if hits and current - datetime.fromisoformat(hits[-1]["ts"]).timestamp() < hold:
            why.append(f"a worker hit its limit at {hits[-1]['ts']}")
        entry["age_minutes"] = round((current - entry["as_of"]) / 60) if entry["as_of"] else None
        entry["ok"] = not why
        entry["why"] = why
        entry["room_for_bakeoff"] = not why and room and entry["windows"] is not None
        report[name] = entry
    return report


# ---------- health ----------

def cmd_health(args):
    roster = load_roster()
    report = {"host": host(roster), "roster": str(roster_path()), "ledger": str(ledger_file(host(roster)))}
    ok, text = ad("list", "--json", timeout=20)
    report["agent_deck"] = "ok" if ok else f"down: {text.strip()[:200]}"
    ws = ad_json("inbox", "writer-status", timeout=20)
    report["notify_daemon"] = bool(ws and ws.get("running"))
    report["pools"] = pool_usage(roster, read_ledger(host(roster)))
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
    dispatches = [r for r in events if r.get("type") == "dispatch" and r.get("route") == "delegate"]
    workers = roster["workers"]
    classes = [args.cls] if args.cls else CLASSES
    stats = {}
    for r in dispatches:
        w = (r.get("worker") or {}).get("id")
        if not w or r.get("class") not in classes:
            continue
        s = stats.setdefault(w, {}).setdefault(r["class"], {"n": 0, "failed": 0, "abandoned": 0})
        s["n"] += 1
        s["failed"] += r.get("outcome") == "failed"
        s["abandoned"] += r.get("outcome") == "abandoned"
    pools = pool_usage(roster, events)
    defaults = {c: roster.get("defaults", {}).get(c, []) for c in classes}
    available = {w: shutil.which(spec["direct"][0]) is not None for w, spec in workers.items()}

    def usable(w):
        return available.get(w, False) and pools.get(workers[w].get("pool"), {}).get("ok", True)

    bake = roster.get("bakeoff", {})
    bake_pools = set(bake.get("pools") or {workers[w].get("pool") for ws in roster.get("defaults", {}).values()
                                           for w in ws if w in workers})
    route = {}
    for c in classes:
        pick = next((w for w in defaults[c] if usable(w)), None)
        challengers = [w for w in workers if w != pick and usable(w) and workers[w].get("pool") in bake_pools
                       and pools.get(workers[w].get("pool"), {}).get("room_for_bakeoff")]
        # Another pool first: a different vendor tells more, and the extra run does not draw on the same window.
        challenger = min(challengers, default=None,
                         key=lambda w: (bool(pick) and workers[w].get("pool") == workers[pick].get("pool"),
                                        stats.get(w, {}).get(c, {}).get("n", 0)))
        if not (bake.get("enabled", True) and pick
                and pools.get(workers[pick].get("pool"), {}).get("room_for_bakeoff")):
            challenger = None
        route[c] = {"pick": pick, "bakeoff_challenger": challenger}
    out({
        "host": h,
        "pools": pools,
        "route": route,
        "defaults": defaults,
        "workers": {
            w: {
                "pool": spec.get("pool"),
                "available": available[w],
                "cap_usd": spec.get("cap_usd"),
                "free": spec.get("free", False),
                "note": spec.get("note"),
                "ledger": stats.get(w),
            }
            for w, spec in workers.items()
        },
    })


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
    for found in SENTINEL.finditer(text or ""):
        # The brief shows this line indented with a <placeholder> summary. An echo of it is not a result.
        if not found.group(2).lstrip().startswith("<"):
            m = found
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
    if state == "limit" and not meta.get("limit_logged"):
        roster = load_roster()
        pool = roster["workers"].get(meta["worker"], {}).get("pool")
        append_event(host(roster), {"v": 1, "type": "pool_limit", "ts": now(), "host": host(roster), "pool": pool,
                                    "worker": meta["worker"], "run": args.run, "summary": summary})
        meta["limit_logged"] = True
        (run_dir(args.run) / "meta.json").write_text(json.dumps(meta, indent=2))
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
