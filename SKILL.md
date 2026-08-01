---
name: clickhouse-dba
description: >
  Read-only ClickHouse diagnostics and root-cause analysis over MCP.
  Runs SELECT / SHOW / EXPLAIN only — never DDL, DML or SYSTEM commands.
  Produces a prioritised health report with evidence, confidence and an
  action class for every recommendation.
license: Apache-2.0
tools:
  - execute_select
---

# ClickHouse DBA Skill

A diagnostic playbook for an agent with read-only access to a ClickHouse cluster.
It encodes the order in which system tables are worth reading, how to interpret
what comes back, and how to report it without guessing.

## Access boundary

Allowed: `SELECT`, `SHOW`, `EXPLAIN`.

Forbidden: DDL, DML and `SYSTEM` commands (`FLUSH`, `RELOAD CONFIG`,
`RESTART REPLICA` and similar).

If a `SYSTEM` command is the right fix, describe it in text with its exact
syntax and expected effect — do not execute it. Some `SYSTEM` commands block
for a long time on a loaded cluster; the human running it needs to know that
before they do.

This boundary is the point of the skill. An agent that can only read is safe
to point at production; an agent that can write is not.

## Version notes

Written against ClickHouse 24.x and later. Verify against your own version
before relying on any of it — system tables and setting names change between
releases.

- **Distributed send settings were renamed.** `distributed_directory_monitor_*`
  became `distributed_background_insert_*`. The old names still work as
  aliases, so a cluster may show either form. Query both when you are not sure
  which the deployment uses.
- `system.distribution_queue` includes `last_exception` and `last_exception_time`.
- `system.merge_tree_settings` holds the *actual* part limits for the running
  server. Read them; do not hardcode defaults, they differ between versions.
- `system.asynchronous_insert_log` is only populated when async inserts are on.
- For Keeper state, use `system.zookeeper`. Note that `/keeper_map_tables/` is
  a path prefix inside Keeper used by the KeeperMap engine — it is not a
  queryable system table.

## Tables that may not be there

Several tables in the investigation order are conditional. They are not
guaranteed to exist, and an agent that assumes they do will report a broken
cluster when the cluster is fine.

| Table | Missing when |
|---|---|
| `system.query_log` | Query logging disabled in config |
| `system.text_log` | Text logging disabled — off by default in many builds |
| `system.asynchronous_insert_log` | Async inserts not enabled |
| `system.zookeeper` | No Keeper or ZooKeeper configured — normal on a single node |

If one of these raises `Code: 60 — Unknown table expression identifier`, treat
it as **absent, not broken**. Say in the report which check could not be
performed and why, and continue with the rest. A gap in coverage is a fact
worth reporting; it is not an incident.

## Thresholds

Everything below is a **starting default, not a universal truth**. Tune per
deployment: a cluster ingesting OpenTelemetry spans and a cluster serving BI
queries do not fail the same way.

| Signal | Default | Notes |
|---|---|---|
| Long-running query | `elapsed > 60s` | Depends entirely on workload |
| Query memory | `memory_usage > 10 GB` | Compare against `max_server_memory_usage` |
| Slow query in history | `query_duration_ms > 10000` | |
| Parts, warning | `parts >= parts_to_delay_insert * 0.8` | Read the limit from `system.merge_tree_settings` |
| Parts, critical | `parts >= parts_to_throw_insert * 0.9` | Same |
| Disk, warning | `used_pct >= 78` | |
| Disk, critical | `used_pct >= 90` | |
| Replica lag | `absolute_delay > 600s` | |
| Stuck replication task | `num_tries > 10` | |
| Small async inserts | `avg_rows_per_insert < 10000` | Signals part accumulation risk |

## Investigation order

From cheap and immediate to deep and expensive:

```
 1. system.processes            — what is happening right now
 2. system.distribution_queue   — async Distributed queue (often the root cause)
 3. system.errors               — recent server errors
 4. system.query_log            — query history: slow, failed
 5. system.metrics              — current counters
 6. system.events               — cumulative events
 7. system.merges               — active background merges
 8. system.parts                — active parts per partition
 9. system.mutations            — stuck mutations
10. system.replicas             — replica state
11. system.replication_queue    — replication backlog
12. system.disks                — disk usage
13. system.merge_tree_settings  — real limits, not defaults
14. system.settings             — session / profile settings
15. system.text_log             — detailed log, last resort
```

---

## Health check

### 1. Current processes

```sql
SELECT query_id, user, elapsed, memory_usage, read_rows, query
FROM system.processes
ORDER BY elapsed DESC;
```

### 2. Distributed queue

The first thing to check on any write-path problem.

```sql
SELECT database, table, data_files, data_compressed_bytes,
       broken_data_files, error_count, last_exception, last_exception_time
FROM system.distribution_queue
ORDER BY data_files DESC;
```

| Observation | Reading |
|---|---|
| `broken_data_files > 0` | P1 — corrupted data in the queue |
| `data_files` rising, `last_exception` non-empty | P1 — a shard is unreachable |
| `data_files` rising, no exception | P1 — the sender cannot keep up, typically many small inserts |
| `data_files` high but stable | P2 — batching needed |
| `data_files` low, `error_count = 0` | P3 — normal for async mode |

**A single sample tells you nothing.** Take two readings 1–2 minutes apart and
compare. One INSERT into a Distributed table produces one `.bin` file, so a
growing `data_files` with no exception usually means insert frequency exceeds
send throughput.

Check the sender configuration — query both naming schemes, since the old
names survive as aliases:

```sql
SELECT name, value
FROM system.settings
WHERE name LIKE 'distributed_background_insert%'
   OR name LIKE 'distributed_directory_monitor%';
```

Relevant knobs: `distributed_background_insert_batch` groups files into one
delivery, `distributed_background_insert_sleep_time_ms` sets the pause between
send iterations. Read the current values rather than assuming defaults.

Recommend, do not execute:
- Enable batching by setting `distributed_background_insert_batch = 1`.
- Force a queue flush with `SYSTEM FLUSH DISTRIBUTED db.table`. Warn that this
  can block for a long time; suggest running it via `clickhouse-client` with a
  raised `--receive_timeout`.

### 3. Recent server errors

```sql
SELECT name, code, value, last_error_time, last_error_message
FROM system.errors
WHERE last_error_time > now() - INTERVAL 1 HOUR
ORDER BY last_error_time DESC
LIMIT 30;
```

### 4. Real part limits

```sql
SELECT name, value
FROM system.merge_tree_settings
WHERE name IN ('parts_to_delay_insert', 'parts_to_throw_insert', 'max_parts_in_total');
```

Use these as the baseline for the next query. Do not use remembered defaults —
they change between versions, and by a lot. On ClickHouse 26.5 these come back
as `parts_to_delay_insert = 1000` and `parts_to_throw_insert = 3000`. Guidance
written a few years ago commonly quotes 300 and 600. An agent working from
memory would raise a critical alert at roughly a fifth of the real limit.

### 5. Active parts per partition

```sql
SELECT database, table, partition,
       count()            AS parts_in_partition,
       sum(rows)          AS total_rows,
       sum(bytes_on_disk) AS bytes_on_disk
FROM system.parts
WHERE active
GROUP BY database, table, partition
ORDER BY parts_in_partition DESC
LIMIT 20;
```

Compare against the limits from step 4.

### 6. Replication

```sql
SELECT database, table, is_readonly, is_session_expired, absolute_delay,
       queue_size, inserts_in_queue, merges_in_queue, lost_part_count
FROM system.replicas
WHERE absolute_delay > 0 OR queue_size > 0 OR is_readonly
   OR is_session_expired OR lost_part_count > 0
ORDER BY absolute_delay DESC;
```

```sql
SELECT database, table, type, num_tries, num_postponed, last_exception
FROM system.replication_queue
WHERE num_tries > 10 OR last_exception != ''
ORDER BY num_tries DESC
LIMIT 20;
```

`is_readonly = 1` means writes are being lost — P1. `lost_part_count > 0` means
unrecoverable data loss — P1, and say so plainly.

### 7. Disks

```sql
SELECT name, path, free_space, total_space,
       round(100.0 * (total_space - free_space) / total_space, 1) AS used_pct
FROM system.disks
ORDER BY used_pct DESC;
```

If a disk is filling up, find out why:

```sql
SELECT database, table,
       sum(bytes_on_disk) AS total_bytes,
       formatReadableSize(sum(bytes_on_disk)) AS size
FROM system.parts
WHERE active
GROUP BY database, table
ORDER BY total_bytes DESC
LIMIT 20;
```

```sql
SELECT database, table, count(), sum(bytes_on_disk) AS sz
FROM system.detached_parts
GROUP BY database, table
ORDER BY sz DESC;
```

Detached parts belong to no table but still occupy disk — a common blind spot.

### 8. Slow and failed queries

```sql
SELECT type, event_time, query_duration_ms, memory_usage, read_rows,
       read_bytes, result_rows, exception,
       substring(query, 1, 300) AS query_head
FROM system.query_log
WHERE event_time > now() - INTERVAL 1 HOUR
  AND ((type = 'QueryFinish' AND query_duration_ms > 10000)
       OR type = 'ExceptionWhileProcessing')
ORDER BY query_duration_ms DESC
LIMIT 20;
```

### 9. Stuck mutations

```sql
SELECT database, table, mutation_id, command, create_time,
       parts_to_do, is_done, latest_fail_reason
FROM system.mutations
WHERE is_done = 0
ORDER BY create_time ASC;
```

An unfinished mutation blocks merges on that table, so parts accumulate. This
is frequently the hidden cause behind a "too many parts" symptom.

### 10. Active merges

```sql
SELECT database, table, elapsed, progress,
       total_size_bytes_compressed, num_parts, result_part_name
FROM system.merges
ORDER BY elapsed DESC;
```

Many long merges alongside growing parts means the background pool is saturated.

### 11. Server settings

```sql
SELECT name, value, description
FROM system.server_settings
WHERE name IN (
    'max_concurrent_queries',
    'background_pool_size',
    'background_merges_mutations_concurrency_ratio',
    'max_server_memory_usage',
    'max_server_memory_usage_to_ram_ratio'
)
ORDER BY name;
```

### 12. Async inserts

Only meaningful when async insert is enabled — common for OpenTelemetry and
other high-frequency ingestion.

```sql
SELECT database, table,
       count()         AS inserts,
       sum(rows)       AS total_rows,
       avg(rows)       AS avg_rows_per_insert,
       min(event_time) AS first,
       max(event_time) AS last
FROM system.asynchronous_insert_log
WHERE event_time > now() - INTERVAL 1 HOUR
GROUP BY database, table
ORDER BY inserts DESC
LIMIT 20;
```

---

## RCA procedure

1. Identify the symptom from what the user described.
2. Run the starting set — processes, distribution queue, errors.
3. On an anomaly, follow the chain down the investigation order.
4. Build a causal chain, not a list of findings.
5. Give each recommendation an action class:
   - `safe_auto` — reversible, no data loss
   - `review` — needs a human decision (schema change, DDL, collector config)
   - `observe` — watch, do not act yet

### Symptom triggers

| Symptom | Start here | Likely cause |
|---|---|---|
| Too many distributed files | `system.distribution_queue` | Small inserts, or an unreachable shard |
| Too many parts | `system.parts` + `system.merge_tree_settings` | Frequent inserts, high-cardinality partition key, stuck mutation |
| Replica read-only | `system.replicas` | Lost connection to Keeper |
| Slow SELECTs | `system.query_log` + `system.merges` | Part count, heavy merges, missing projections |
| Disk full | `system.disks` + `system.detached_parts` | No TTL, detached parts, distributed queue backlog |
| OOM | `system.query_log` (memory_usage) | Unbounded GROUP BY or JOIN |
| Slow INSERTs | `system.distribution_queue` + `system.merges` | Merge pressure, saturated background pool |

---

## Report format

```
# Health Score: XX/100

## Critical
[P1 — act now]

## High
[P2 — within hours]

## Medium
[P3 — scheduled]

## Root cause
The most likely first cause and the chain of events behind it.

## Recommendations
For each: cause · what to do (with action_class) · expected effect · priority
```

### Health score

Start at 100, floor at 0. Weights are a starting point — adjust for what
matters in your deployment.

| Finding | Penalty |
|---|---|
| `broken_data_files > 0` | −30 |
| Replica read-only or session expired | −25 |
| `lost_part_count > 0` | −25 |
| Distribution queue growing without errors | −20 |
| Disk >= 90% | −20 |
| Parts >= `parts_to_throw_insert` × 0.9 | −20 |
| Unfinished mutation with an error | −15 |
| Replica `absolute_delay > 600s` | −15 |
| Disk >= 78% | −10 |
| Distribution queue high but stable | −10 |
| Parts >= `parts_to_delay_insert` × 0.8 | −10 |
| More than 5 slow queries in the last hour | −5 |
| `avg_rows_per_insert < 10000` | −5 |

## Output rules

- Never dump a raw result table without interpreting it.
- For every problem state: symptom, likely cause, evidence (concrete values
  from the queries), and confidence — high, medium or low.
- When the cause is not obvious, say so. List hypotheses and name the `SELECT`
  that would confirm or rule out each one. A wrong confident answer costs more
  than an honest "I do not know yet".
- Tag every recommendation with its action class.
- Never suggest a `SYSTEM` command without its exact syntax and a warning about
  how long it may block.
