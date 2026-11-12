# Onboarding, written for an agent

You are an AI coding agent and your user asked you to set up `leader`. Do the steps in order. Ask the user when a step says to. Do not launch any worker during setup: workers spend the user's quota.

## 1. Check what is there

```sh
agent-deck version
uv --version
```

If either is missing, stop and tell the user. Link [agent-deck](https://github.com/asheshgoplani/agent-deck) or [uv](https://docs.astral.sh/uv/). Do not install them unless the user asks.

## 2. Install the skill

Pick the skills directory of the harness you are running in:

| Harness | Directory |
|---|---|
| Claude Code | `~/.claude/skills/leader` |
| Codex, pi | `~/.agents/skills/leader` |

If the directory already exists, stop and ask the user before changing it. Otherwise:

```sh
git clone https://github.com/ckjeter/leader-in-agent-deck <directory>
```

Below, `leader` means `uv run <directory>/scripts/leader.py`.

## 3. Write the roster

If `~/.config/leader/roster.toml` exists, keep it and go to step 4.

```sh
mkdir -p ~/.config/leader
cp <directory>/roster.example.toml ~/.config/leader/roster.toml
leader roster
```

The example lists more workers than most people have. Trim it to this user:

1. Remove every worker that `leader roster` reports as `available: false`. Its CLI is not installed.
2. Ask the user which subscriptions or API accounts they have. Remove workers whose model they cannot use. An installed CLI does not mean the model is reachable.
3. Remove pools that no worker uses.
4. Rewrite `[defaults]` so each of the six classes lists only workers that remain, best first. Ask the user if the order is not obvious.
5. Replace `[hosts]` with one line: the output of `hostname -s` mapped to a short name the user picks, for example `"<hostname -s output>" = "laptop"`. On macOS the short hostname can change between networks. If the user moves around, also suggest setting `LEADER_HOST`.
6. Rewrite each `note` as the user's own reason for keeping that worker, or delete it. The notes in the example are someone else's.

## 4. Verify

```sh
leader health
leader roster --class impl
```

`health` must print `"mode": "normal"` and the `roster` path must be `~/.config/leader/roster.toml`. Every worker left in the roster should be `available: true`.

## 5. Tell the user

Report in a few lines:

- where the skill and the roster are,
- which workers are on the roster and the default order per class,
- how to start: `/leader <task>` in Claude Code, or "run this in leader mode" in Codex and pi. The leader session should be one that agent-deck started, so workers attach to it,
- one thing to know: the first time a worker opens a folder its CLI has never seen, the CLI asks whether to trust the folder. agent-deck's `trusted_folders` setting avoids that. See Known limitations in the README.
