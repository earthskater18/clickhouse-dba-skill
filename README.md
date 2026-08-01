# ClickHouse DBA Skill

A read-only diagnostic skill for ClickHouse. Give an agent MCP access to your
cluster with `SELECT` only, and it can walk the system tables in a sensible
order, interpret what it finds, and hand you a prioritised report instead of a
wall of query output.

Written by an observability engineer who administers a ClickHouse cluster used
as a telemetry store, from the queries actually reached for during incidents.

## Why read-only

The skill declares exactly one tool, `execute_select`, and forbids DDL, DML and
`SYSTEM` commands outright. When a `SYSTEM` command is the correct fix, the
agent writes it out with its syntax and a warning about how long it may block —
and stops there.

This is the design decision the whole thing rests on. An agent that can only
read is safe to point at a production cluster during an incident. An agent that
can write is a second incident waiting to happen. Diagnosis and remediation are
different trust levels, and the tool boundary is where that difference should
live — not in a prompt asking the model to be careful.

## What it covers

- **Investigation order** — 15 system tables ranked from cheap and immediate to
  deep and expensive, so the agent does not start with `system.text_log`.
- **Interpretation tables** — what a growing `distribution_queue` means with and
  without exceptions, why one sample is useless, when part counts are actually a
  problem.
- **Symptom triggers** — a lookup from "too many parts" or "replica read-only"
  to the query worth running first and the causes worth suspecting.
- **Action classes** — every recommendation tagged `safe_auto`, `review` or
  `observe`, so a human knows what needs their judgement.
- **Health score** — a weighted rollup, with the weights exposed rather than
  hidden in a prompt.

## Usage

Pair it with an MCP server that exposes ClickHouse to your agent. The official
one is [ClickHouse/mcp-clickhouse](https://github.com/ClickHouse/mcp-clickhouse).

1. Configure the MCP server against your cluster.
2. Grant a user with `SELECT` privileges only — the skill assumes this and does
   not work around a wider grant.
3. Drop `SKILL.md` into your agent's skills directory.
4. Ask it something concrete: *"inserts into the distributed table are slow,
   what is going on"*.

The skill is deliberately a Markdown workflow encoding rather than code. The
knowledge here is "which table to read next and what the number means", and that
belongs in something a human can read, argue with and edit — not compiled into a
function.

## Verified

Every SQL statement in `SKILL.md` is executed against a real ClickHouse engine
by `tests/validate_skill.py`. The test checks three things: that each statement
parses and runs, that every referenced system table exists, and that every
referenced setting resolves.

```bash
pip install chdb
python3 tests/validate_skill.py
```

Last run against **ClickHouse 26.5.1.1**: 14 of 16 statements pass. The two that
do not are `system.query_log` and `system.asynchronous_insert_log`, both absent
in the embedded test engine by design — see *Tables that may not be there* in
`SKILL.md`, which the skill now handles explicitly.

Two things the run settled that documentation alone did not:

- **The old distributed setting names are still live.** Both
  `distributed_background_insert_batch` and
  `distributed_directory_monitor_batch_inserts` resolve on 26.5. A skill that
  queries only the new names will miss the configuration on some deployments,
  so the skill queries both.
- **`parts_to_delay_insert` is 1000 and `parts_to_throw_insert` is 3000** on
  26.5, not the 300 and 600 quoted in a lot of older material. This is the
  clearest argument for the skill's own rule: read the limit from
  `system.merge_tree_settings`, never from memory. An agent using remembered
  defaults would page someone at a fifth of the real threshold.

## Requirements

- ClickHouse 24.x or later. Verify the version notes in `SKILL.md` against your
  own build: system tables and setting names change between releases, and the
  skill says so in several places rather than pretending otherwise.
- An MCP-capable agent.
- A ClickHouse user restricted to `SELECT`.

## Thresholds are defaults, not truth

Every number in the skill — part limits, disk percentages, replica lag, health
score weights — is a starting point. A cluster ingesting OpenTelemetry spans and
a cluster serving BI dashboards do not fail the same way, and they should not
score the same way. The thresholds live in one table near the top of `SKILL.md`
so they are easy to change.

Where a real limit is available from the server, the skill reads it —
`parts_to_delay_insert` and `parts_to_throw_insert` come from
`system.merge_tree_settings`, never from a remembered default.

## Contributing

Corrections welcome, particularly:

- version-specific behaviour that has changed
- system tables or columns worth adding to the investigation order
- interpretation rules that turned out wrong on a real cluster

Open an issue or a pull request.

## License

Apache-2.0. See [LICENSE](LICENSE).
