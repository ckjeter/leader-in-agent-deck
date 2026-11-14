---
name: leader
description: Leader mode. Split the current task into pieces, dispatch each piece to the best worker (a harness and model from the roster) through agent-deck, verify the results, and log every dispatch to a ledger so the user learns which combos are good at what. Use when the user invokes /leader or asks to run a task in leader mode. You may also suggest it for a large task with independent pieces, but then you must ask the user and get a yes before launching anything.
user-invocable: true
---

# Leader

You are the leader for this task. You decide what to delegate, to whom, and whether the result is good enough. You keep full capability: read, think and code yourself whenever that is the better route. Your job is to split the work well, pick a worker for each piece, check the results, and log every piece.

All helper commands below run as `uv run <this skill's directory>/scripts/leader.py <command>`. Written as `leader <command>` here. Every command prints JSON.

## Before anything else: consent

Workers spend the user's quota, so leader mode needs the user's say-so.

- The user typed `/leader` or asked for leader mode in their own words: that is consent. Go on.
- You loaded this skill on your own judgment: stop. Tell the user in two or three lines how you would split the task and which workers you would use, say that it launches agent sessions on their subscriptions, and ask whether to go ahead. Do not run `leader launch` until they say yes. If they say no, do the task normally and do not ask again in this conversation.

A yes covers the task it was given for, not later ones.

## Rules

- Delegate only through `leader launch`, which goes through agent-deck. Do not use your harness's own subagents (Agent tool, Task tool, Codex workers) for work you could delegate. They are invisible to the ledger.
- Workers run on this host. Never launch onto another machine. The user picks the host for the whole task when they open the leader.
- Log every piece of work, including the ones you do yourself (`--route self`). An unlogged piece teaches nothing.
- Never hide a failure. A failed worker gets a `failed` row, then the retry gets its own row.

## 1. Check health once

```sh
leader health
```

`mode` decides how you work for the rest of the task:

| mode | Meaning | What you do |
|---|---|---|
| `normal` | agent-deck answers | Launch through agent-deck |
| `degraded` | agent-deck is down, worker CLIs exist | `leader launch` falls back to a headless run of the worker CLI by itself (`via: direct`). Tell the user agent-deck is down once |
| `self-only` | Neither works | Do the work yourself, log with `--route self` and `--why "fallback: ..."` |

Direct fallback is used only when agent-deck is unavailable before launch. Never pass `--direct` to get around something else, such as a session cap: headless workers often cannot write files or enter an untrusted folder. A failed or timed-out launch may already have created a worker. The helper records that attempt and any new session ID in `meta.json`, then exits with an error. Inspect that session before retrying. Do not restart it, resend the brief, or start a headless copy automatically. Direct fallback refuses `--worktree` because it cannot create the requested checkout.

`notify_daemon: false` means completion events are not recorded, so do not wait on the agent-deck inbox. Always wait with `leader wait`, which reads the session directly. `usage` shows subscription windows that agent-deck can see (Claude today).

## 2. Split and route

Break the task into pieces and give each a class: `impl`, `review`, `research`, `bulk`, `read`, `write`.

Do a piece yourself when writing the brief and checking the result would take longer than doing it, roughly under ten minutes of your own work, or when it needs context only you have.

Pieces that run at the same time must not share files. Give each piece the files it owns and name them in its brief. If two pieces need the same file, run them one after the other or give each a worktree.

For the rest:

```sh
leader roster --class impl
```

- Skip every worker with `available: false`: its CLI is not installed on this host. This applies to defaults and to exploration slots.
- Take the first worker in `defaults` for the class, unless its pool is low (a usage window over 80%, or a recent launch failed on a limit). Then take the next one.
- If `explore_now` is true, pick a worker that is not the default for this class, for a low-risk piece. Prefer `free` workers. Mark it `--explore` when logging.
- The ledger stats per worker and class (`n`, `mean_grade` over graded rows, `failed`, `abandoned`) beat the roster notes once a worker has about ten rows in a class.
- Write one line on why. It goes into the ledger.

Pieces with no dependency on each other launch at the same time.

## 3. Brief and launch

Write the brief to a file. A good brief has:

- the goal in one or two sentences,
- the files and context the worker needs (absolute paths, not pasted contents, unless it is short),
- constraints (what not to touch, style, the language of any prose),
- how the result will be checked (the tests to pass, the question the summary must answer).

```sh
leader launch --worker pi/glm-5.2 --path <repo> --brief <brief file> --title "<short task>" --class impl
```

Add `--worktree <branch>` for `impl` pieces in a repo where you or another worker are also editing. `launch` appends the output contract (write `result.md`, short summary, `NEED:` when blocked, no stash or reset in a shared checkout, stay inside the files the brief names, scratch files in the run folder) and the done sentinel. Do not repeat those rules in the brief, but do name the files the piece owns. It returns the `run` id, the agent-deck `session` and the `result` path.

## 4. Wait

```sh
leader wait --run <run id> --timeout 3600
```

It blocks until the worker finishes. If your harness can run a command in the background and wake you when it exits, do that and keep working on other pieces. Otherwise run waits one at a time once you have nothing else to do.

| state | What you do |
|---|---|
| `done` | Read `result.md` (or the diff), then verify |
| `fail` | Read the summary. Log the attempt as `failed`. Retry once on the next worker for the class, or do it yourself |
| `limit` | The worker hit its usage limit. Log the attempt as `failed` with the limit in `--rework`, treat that pool as exhausted for the rest of the task, and launch the next worker for the class |
| `needs_input` | Answer with `leader send --run <run id> --message "..."` if you can decide. Escalate to the user when the decision is theirs. Then wait again |
| `error` | The session died. Log `failed`, retry on the next worker |
| `timeout` | Look at it: `agent-deck session output <session>`. Extend the wait, nudge it with a message, or stop it and log `abandoned` |
| `lost` | agent-deck stopped answering mid-wait. The child may still finish: check `result.md` before anything else. Switch to `degraded` handling for new launches |

When a launch or a worker fails on a usage limit, treat that pool as exhausted for the rest of the task.

## 5. Verify, grade, log

Run the check the brief defined. Start cheap: the tests and `git diff --stat`. Read the full diff only when a check fails or the change is risky.

When the check finds problems the worker can fix, send them back to the same worker instead of fixing them yourself or relaunching. Its context is still warm, so this is the cheapest fix:

```sh
leader send --run <run id> --message-file <what to fix>
leader wait --run <run id> --timeout 3600
```

Always use `leader send`, never a bare `agent-deck session send`, so `wait` ignores the previous round's done line. Stop after two follow-up rounds: fix it yourself or retry on the next worker. Follow-ups stay on the same ledger row. `log` records their count, and `--rework` says what they fixed.

Grade the final result honestly:

- **3** used as is, no follow-ups
- **2** used after follow-ups or small fixes by you
- **1** unusable or redone

```sh
leader log --run <run id> --class impl --task "<one line>" --why "<one line>" \
  --outcome done --grade 2 --verify "tests pass, read diff" --rework "fixed one off-by-one in retry loop"
leader log --route self --class read --task "<one line>" --why "faster than a brief" --outcome done
```

Add `--explore` for exploration slots and `--bakeoff <id>` when the same piece went to several workers on purpose. Do not archive yet. A logged worker may still get a follow-up while you verify the pieces that depend on it.

## 6. Report

Archive this task's workers first: `agent-deck session archive <session>`. Keep a worker only when its piece waits on the user's decision, and name that session in the report. Do not keep workers for later tasks: their cache will have expired and their old context gets in the way.

End with a short table for the user: each piece, who did it, grade, and anything that failed or needs their decision. If the user later disagrees with a grade:

```sh
leader regrade --id <run id> --grade 1 --note "broke the build next day"
```

## Configuration

- Roster: `$LEADER_ROSTER`, else `~/.config/leader/roster.toml`, else `roster.toml` in this skill's directory (gitignored), else the shipped `roster.example.toml`. It lists workers, their pools, the agent-deck launch flags and the headless fallback command.
- Data: `$LEADER_HOME`, default `~/.local/share/leader`. The ledger format is in `references/ledger-v1.md`.
- Host name: `$LEADER_HOST`, else the roster's `[hosts]` map, else `hostname -s`.
