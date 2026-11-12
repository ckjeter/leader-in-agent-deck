# Contributing

## The three rules

1. One problem per pull request. Say what you wanted and show real output.
2. Add a test that fails without the change and passes with it. Run the local check before opening.
3. A human merges. Any AI review is advice, and it goes stale on the next push.

The ideas behind these rules come from [agent-deck's contribution guide](https://github.com/asheshgoplani/agent-deck/blob/main/CONTRIBUTING.md).

## Before you open a PR

For changes over about 200 lines, open an issue first.
Run this command from the repo before opening a PR:

```sh
sh scripts/check.sh
```

You can also run the script by its path from any directory.
Redact private paths, hostnames and task text from evidence.

## What CI runs

On pull requests and pushes to `develop`, CI runs the same script with Python 3.11 and 3.13.
It runs `scripts/test_leader.py`, checks the example roster and scans tracked and untracked non-ignored files for private home paths and `roster.toml`.
The only home-path placeholders allowed are `/home/you/` and `/Users/you/`.
Each stage prints `STAGE <name> OK` or `STAGE <name> FAIL`. Any failure fails the check.

## Adding a roster worker

Change `roster.example.toml`, never your private `roster.toml`.
Include the JSON printed by one real `leader launch` with that worker, with paths redacted, and output from one run of its `direct` command.
Give the worker a valid pool, `launch` flags and a `direct` command.
A `note` is a dated hypothesis, not a verdict. Include its date.
Do not put prices in the roster. Link the provider's pricing page instead.
Do not change `[defaults]`. That order is the maintainer's own lineup.

## Sharing ledger observations

Never post raw ledger rows, briefs or results. The `project`, `task` and `why` fields hold private paths and task text.
Share per-worker stats from `leader roster --class <class>`: `n`, `mean_grade`, `failed` and `abandoned`.
Wait for at least 10 rows per worker in that class. Include the date, the harness version and one line on the kind of work.
Post these observations in [Discussions](https://github.com/ckjeter/leader-in-agent-deck/discussions), not in the repo. Questions also belong there.

## AI-assisted PRs

Welcome, on the same rules as any other PR. Say which model helped in the notes for the reviewer.
You must understand the change and have run the check yourself.

## Review and merge

There is one maintainer, so reviews may be slow.
AI review is advisory. Any new push makes the previous AI review stale.
A human reviews the current change and merges it.

## License

Contributions are provided under the MIT license.
