# Ledger format, v1

The ledger is the leader's half of the observation data. It holds what only the leader knows: why a worker was picked and how good the result was. Tokens, cost and duration are not here. A telemetry pipeline derives them from the harness transcripts and joins on `worker.session` (agent-deck session id) or, for `via: direct`, on time window and working directory.

## Files

- `$LEADER_HOME/ledger/<host>.jsonl`, default `LEADER_HOME=~/.local/share/leader`
- One file per host, so two machines never write the same file. Merge by concatenating.
- Append-only. Corrections are new events, never edits.
- One JSON object per line, UTF-8.

## Event: `dispatch`

Written once per finished piece of work, delegated or done by the leader itself.

| Field | Type | Meaning |
|---|---|---|
| `v` | int | Schema version, `1` |
| `type` | string | `"dispatch"` |
| `id` | string | Run id, `YYYYMMDD-HHMMSS-xxxx`. Same as the run folder name |
| `ts` | ISO 8601 | When the row was written (end of the work) |
| `started` | ISO 8601 or null | When the worker was launched |
| `host` | string | Short host name from roster `[hosts]` |
| `leader` | object | `{session, harness, model}` of the leader. Fields may be null when agent-deck is down |
| `route` | string | `delegate` or `self` |
| `via` | string | `agent-deck`, `direct` (headless CLI fallback) or `self` |
| `worker` | object or null | `{id, session, pool}`. `id` is the roster key, e.g. `pi/glm-5.2`. Null for `self` |
| `class` | string | `impl`, `review`, `research`, `bulk`, `read`, `write` |
| `task` | string | One line describing the piece of work |
| `project` | string | Working directory |
| `why` | string | One line on why this worker (or self) |
| `explore` | bool | True when this was an exploration slot, not the default |
| `bakeoff` | string or null | Shared id when the same task went to several workers |
| `verify` | string or null | What the leader checked |
| `outcome` | string | `done`, `failed`, `abandoned` |
| `grade` | int or null | 3 = used as is, 2 = used after follow-ups or small fixes, 1 = unusable or redone |
| `rework` | string or null | What had to be fixed, in words |
| `followups` | int or null | Fix rounds sent back to the same worker with `leader send`. Null for `self` |

A retry on another worker is a new `dispatch` row. The failed attempt keeps its own row with `outcome: failed`.

## Event: `regrade`

A later opinion on an existing dispatch, usually from the user.

| Field | Type | Meaning |
|---|---|---|
| `v`, `type`, `ts`, `host` | | As above, `type` is `"regrade"` |
| `id` | string | The dispatch `id` being regraded |
| `by` | string | `user` or a session id |
| `grade` | int | 1 to 3 |
| `note` | string or null | Why |

The latest `regrade` for an `id` wins over the dispatch's own `grade`.

## Event: `pool_limit`

Written by `leader wait` when a worker stops on a usage limit. `leader roster` reads it and keeps the pool closed for the hold time (`[budget] limit_hold_minutes`, default 300), so other leaders on the host do not hit the same wall.

| Field | Type | Meaning |
|---|---|---|
| `v`, `type`, `ts`, `host` | | As above, `type` is `"pool_limit"` |
| `pool` | string | Roster pool of the worker |
| `worker` | string | Roster key of the worker |
| `run` | string | Run id of the dispatch that hit the limit |
| `summary` | string | The limit message as the worker printed it |

## Run folder

`$LEADER_HOME/runs/<id>/` holds `brief.md` (what the worker was told), `result.md` (what it wrote), `meta.json` (launch details), `followup-<n>.md` (each follow-up message) and, for `via: direct`, `stdout.log`. The pipeline may archive these next to the ledger.

## Versioning

Add fields freely within v1. Readers ignore unknown fields. Renaming or removing a field, or changing a meaning, bumps `v`.
