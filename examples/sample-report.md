# Example output

Illustrative only. The numbers below are made up to show the report shape —
they are not from any real cluster.

**Prompt:** *"Inserts into the distributed table have been slow since this morning."*

---

# Health Score: 55/100

## Critical

**Distribution queue growing with no exceptions** — `analytics.events_dist`

- `data_files`: 41,300 → 48,720 over a 2-minute window
- `error_count`: 0, `last_exception`: empty
- `broken_data_files`: 0

The queue is growing and nothing is failing, so this is throughput, not an
outage. Confidence: high.

## High

**Part count approaching the delay threshold** — `analytics.events_local`

- Parts in the busiest partition: 810
- `parts_to_delay_insert`: 1000 (read from `system.merge_tree_settings`, not assumed)
- 810 / 1000 = 81%, past the 80% warning line

Confidence: high.

**Small async inserts**

- `avg_rows_per_insert`: 890 over the last hour
- 4,100 inserts in the same period

Confidence: medium — the async insert log only covers tables where async
insert is enabled, so this may understate the picture.

## Medium

Two merges running longer than 40 minutes on `analytics.events_local`.
Consistent with the part count above rather than a separate problem.

## Root cause

Ingestion is producing many small inserts. Each INSERT into the Distributed
table becomes one `.bin` file, and the background sender is not keeping up —
hence the growing queue with no errors. The same small-insert pattern creates
many small parts on the local table, which is why the part count is climbing
and merges are running long. One cause, three symptoms.

## Recommendations

**1. Enable batching on the distributed sender** · `review`

Cause: one file per INSERT, sender throughput is the bottleneck.
Action: set `distributed_background_insert_batch = 1`. Current value read from
`system.settings` was `0`.
Expected effect: queue drains, `data_files` stops climbing.
Priority: P1.

**2. Batch at the producer** · `review`

Cause: 890 rows per insert is far below the point where ClickHouse is efficient.
Action: raise the batch size in the ingestion pipeline, or enable async insert
with a larger `async_insert_max_data_size`.
Expected effect: fewer parts, less merge pressure. This is the durable fix —
recommendation 1 only moves the bottleneck.
Priority: P1.

**3. Flush the accumulated queue** · `review`

Action: `SYSTEM FLUSH DISTRIBUTED analytics.events_dist`
Warning: this blocks until the queue drains. With ~48k files that may take a
long time. Run it from `clickhouse-client` with a raised `--receive_timeout`,
and preferably after recommendation 1 is in place, or the queue will refill.
Priority: P2.

**4. Watch the part count** · `observe`

No action yet. If parts cross `parts_to_throw_insert` × 0.9 the cluster starts
rejecting inserts. Recheck after the fixes above land.
Priority: P3.

## What I could not determine

Whether the ingestion change that caused this was deployed today. Nothing in
the read-only surface shows deployment history. Worth correlating with your
change log before treating batching as the whole answer.
