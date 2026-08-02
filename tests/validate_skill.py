#!/usr/bin/env python3
"""Validate every SQL statement in SKILL.md against a real ClickHouse engine.

Checks that each query parses, that every referenced system table exists and
that every referenced column exists. Uses chDB (embedded ClickHouse).
"""
import os
import re
import sys

import chdb

SKILL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "SKILL.md")


def q(sql):
    return chdb.query(sql, "CSV").bytes().decode()


def extract_sql_blocks(path):
    text = open(path, encoding="utf-8").read()
    blocks = re.findall(r"```sql\n(.*?)```", text, re.S)
    out = []
    for b in blocks:
        for stmt in b.split(";"):
            stmt = "\n".join(
                l for l in stmt.splitlines() if not l.strip().startswith("--")
            ).strip()
            if stmt:
                out.append(stmt)
    return out


print("ClickHouse engine:", q("SELECT version()").strip())
print()

# ---- 1. every system table named in the skill exists ----
text = open(SKILL, encoding="utf-8").read()
tables = sorted(set(re.findall(r"system\.([a-z_]+)", text)))
print(f"=== system tables referenced: {len(tables)} ===")
missing_tables = []
for t in tables:
    try:
        q(f"SELECT 1 FROM system.{t} LIMIT 0")
        print(f"  OK      system.{t}")
    except Exception as e:
        first = str(e).split("\n")[0][:90]
        print(f"  MISSING system.{t}  -> {first}")
        missing_tables.append(t)

# ---- 2. every SQL statement parses and runs ----
stmts = extract_sql_blocks(SKILL)
print(f"\n=== SQL statements in SKILL.md: {len(stmts)} ===")
failed = []
for i, s in enumerate(stmts, 1):
    label = " ".join(s.split())[:72]
    try:
        q(s)
        print(f"  [{i:02d}] PASS  {label}")
    except Exception as e:
        first = str(e).split("\n")[0][:110]
        print(f"  [{i:02d}] FAIL  {label}\n         -> {first}")
        failed.append((i, label, first))

# ---- 3. settings named in the skill actually exist ----
print("\n=== settings referenced ===")
settings = [
    "distributed_background_insert_batch",
    "distributed_background_insert_sleep_time_ms",
    "distributed_directory_monitor_batch_inserts",
    "distributed_directory_monitor_sleep_time_ms",
]
for s in settings:
    try:
        r = q(f"SELECT value FROM system.settings WHERE name = '{s}'").strip()
        print(f"  {'OK     ' if r else 'ABSENT '} {s}  {('= ' + r) if r else ''}")
    except Exception as e:
        print(f"  ERROR   {s} -> {str(e).splitlines()[0][:70]}")

print("\n=== merge_tree limits the skill tells the agent to read ===")
for s in ["parts_to_delay_insert", "parts_to_throw_insert", "max_parts_in_total"]:
    try:
        r = q(f"SELECT value FROM system.merge_tree_settings WHERE name = '{s}'").strip()
        print(f"  {'OK     ' if r else 'ABSENT '} {s}  {('= ' + r) if r else ''}")
    except Exception as e:
        print(f"  ERROR   {s} -> {str(e).splitlines()[0][:70]}")

print("\n=== claim check: system.keeper_map_tables (removed from skill) ===")
try:
    q("SELECT 1 FROM system.keeper_map_tables LIMIT 0")
    print("  EXISTS — removing it from the skill was wrong")
except Exception as e:
    print("  CONFIRMED ABSENT —", str(e).splitlines()[0][:80])

print("\n" + "=" * 60)
print(f"tables missing: {len(missing_tables)}  |  statements failed: {len(failed)}")
if missing_tables:
    print("missing tables:", ", ".join(missing_tables))
sys.exit(1 if failed or missing_tables else 0)
