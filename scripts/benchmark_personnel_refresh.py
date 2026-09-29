"""Isolated PostgreSQL load simulation; never constructs a real ESI client."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import threading
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.esi.personnel_archive import PersonnelArchive
from app.esi.personnel_runtime import PersonnelRuntime


def validate_local_dsn(dsn: str) -> None:
    """Require a dedicated test account and nonstandard loopback port."""
    fields = conninfo_to_dict(dsn)
    if (fields.get("host") not in {"127.0.0.1", "::1"}
            or fields.get("hostaddr", fields["host"]) != fields["host"]
            or fields.get("user") != "codex_test"
            or not fields.get("port", "").isdigit()
            or not 1024 <= int(fields["port"]) <= 65535
            or fields["port"] == "5432"
            or fields.get("service")):
        raise ValueError("Only explicit loopback, non-5432 port, codex_test account allowed")


def distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}
    return {"count": len(ordered), "median": statistics.median(ordered),
            "p95": ordered[math.ceil(len(ordered) * 0.95) - 1], "max": ordered[-1]}


class TimedArchive:
    """Measure public repository calls without changing production symbols."""

    def __init__(self, archive):
        self.archive = archive
        self.samples = defaultdict(list)
        self.lock = threading.Lock()

    def __getattr__(self, name):
        method = getattr(self.archive, name)

        def measured(*args, **kwargs):
            started = time.perf_counter()
            try:
                return method(*args, **kwargs)
            finally:
                with self.lock:
                    self.samples[name].append((time.perf_counter() - started) * 1000)

        return measured

    def snapshot(self):
        with self.lock:
            return {key: {**distribution(values), "total_ms": sum(values)}
                    for key, values in self.samples.items()}


class FakeEsi:
    """Deterministic affiliation payload plus configurable per-request latency."""

    def __init__(self, latency: float):
        self.latency = latency
        self.local = threading.local()
        self.lock = threading.Lock()
        self.batch_sizes = []

    def get_character_affiliations(self, ids):
        time.sleep(self.latency)
        now = time.time()
        self.local.freshness = {str(cid): {"fetched_at": now, "expires_at": now + 3600} for cid in ids}
        with self.lock:
            self.batch_sizes.append(len(ids))
        return [{"character_id": cid, "corporation_id": 98000000 + cid % 1000,
                 "alliance_id": 99000000 + cid % 100} for cid in ids]

    def response_freshness(self):
        return self.local.freshness


def seed(conn, count: int, now: float):
    """Bulk SQL generation keeps Python memory independent of archive size."""
    conn.execute(
        "INSERT INTO personnel_profiles "
        "(character_id,name,name_checked_at,affiliation_json,affiliation_fetched_at,first_seen_at,last_seen_at) "
        "SELECT 900000000+g, 'Synthetic Pilot '||g, %s, "
        "json_build_object('corporation_id',98000000+g%%1000,'alliance_id',99000000+g%%100)::text, "
        "%s-7200-g%%1000, %s-86400*100, "
        "%s-CASE g%%4 WHEN 0 THEN 3600 WHEN 1 THEN 86400*3 WHEN 2 THEN 86400*15 ELSE 86400*60 END "
        "FROM generate_series(1,%s) AS g", (now, now, now, now, count))
    conn.execute("INSERT INTO personnel_names (name_key,character_id,verified_at) "
                 "SELECT lower(name),character_id,name_checked_at FROM personnel_profiles")
    conn.execute("INSERT INTO personnel_organizations (kind,entity_id,name,fetched_at) "
                 "SELECT 'corporation',98000000+g,'Synthetic Corp '||g,%s FROM generate_series(0,999) AS g "
                 "UNION ALL SELECT 'alliance',99000000+g,'Synthetic Alliance '||g,%s "
                 "FROM generate_series(0,99) AS g", (now, now))
    conn.execute("INSERT INTO personnel_refresh_jobs (kind,entity_key,priority,next_due_at) "
                 "SELECT 'affiliation',character_id::text,2+(character_id%4)::int,affiliation_fetched_at+3600 "
                 "FROM personnel_profiles UNION ALL "
                 "SELECT 'identity',character_id::text,2+(character_id%4)::int,name_checked_at+90*86400 "
                 "FROM personnel_profiles")
    for table in ("personnel_profiles", "personnel_names", "personnel_refresh_jobs", "personnel_organizations"):
        conn.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table)))


def memory():
    try:
        import psutil
        return {"python_rss_mb": psutil.Process().memory_info().rss / 1e6,
                "system_available_mb": psutil.virtual_memory().available / 1e6}
    except ImportError:
        return {}


def run_case(dsn: str, count: int, duration: float, latency: float) -> dict:
    schema = "eve_sentry_bench_" + uuid4().hex
    pool = runtime = None
    result = {"profiles": count, "jobs_seeded": 2 * count, "mock_request_latency_s": latency}
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            isolated = make_conninfo(dsn, options=f"-csearch_path={schema} -cstatement_timeout=5000 -clock_timeout=2000")
            pool = ConnectionPool(isolated, min_size=1, max_size=8, timeout=2,
                                  kwargs={"row_factory": dict_row})
            pool.wait()
            archive = PersonnelArchive(pool.connection)
            archive.migrate()
            started = time.perf_counter()
            with psycopg.connect(make_conninfo(isolated, options=f"-csearch_path={schema} -cstatement_timeout=120000")) as conn:
                seed(conn, count, time.time())
            result["seed_seconds"] = time.perf_counter() - started
            with pool.connection() as conn:
                result["tables_and_indexes_mb"] = float(conn.execute(
                    "SELECT sum(pg_total_relation_size(relid)) / 1000000.0 AS size "
                    "FROM pg_stat_user_tables WHERE schemaname=%s", (schema,)).fetchone()["size"])
                result["database_settings"] = {key: conn.execute(sql.SQL("SHOW {}").format(sql.Identifier(key))).fetchone()[key]
                    for key in ("server_version", "shared_buffers", "work_mem", "synchronous_commit", "fsync")}
            timings = []
            for _ in range(7):
                started = time.perf_counter()
                stats = archive.statistics(time.time())
                timings.append((time.perf_counter() - started) * 1000)
            assert stats["profiles"] == count
            assert sum(item["count"] for item in stats["due_by_priority"]) == count
            result["idle_statistics_ms"] = distribution(timings)
            measured = TimedArchive(archive)
            client = FakeEsi(latency)
            runtime = PersonnelRuntime(measured, client)
            pending, delivered = {}, {}
            probe_lock = threading.Lock()

            def callback(changed, all_current):
                with probe_lock:
                    for cid, (sent, sent_wall) in pending.items():
                        if cid in delivered:
                            continue
                        row = runtime.lookup(f"Synthetic Pilot {cid - 900000000}")
                        if row and row.get("affiliation_fetched_at", 0) >= sent_wall:
                            delivered[cid] = time.perf_counter() - sent
                return True

            begin = time.perf_counter()
            result["memory_before"] = memory()
            runtime.start(callback)
            next_progress, wave = 5, 0
            while time.perf_counter() - begin < duration:
                elapsed = time.perf_counter() - begin
                if wave < 3 and elapsed >= 3 + wave * (duration - 6) / 3:
                    identities = []
                    with probe_lock:
                        for j in range(10):
                            # Choose newer-due P5 jobs far from normal/cold claim heads.
                            index = count - 997 + (wave * 10 + j) * 4
                            cid = 900000000 + index
                            sent_wall = time.time()
                            pending[cid] = (time.perf_counter(), sent_wall)
                            identities.append((cid, f"Synthetic Pilot {index}", sent_wall))
                    runtime.set_active(identities)
                    wave += 1
                if elapsed >= next_progress:
                    snap = runtime.snapshot()
                    print(json.dumps({"progress_rows": count, "elapsed_s": round(elapsed, 1),
                                      "success": snap.get("refresh_success", 0),
                                      "probes_delivered": len(delivered), "memory": memory()}), flush=True)
                    next_progress += 5
                time.sleep(0.05)
            result["observed_seconds"] = time.perf_counter() - begin
            result["runtime_at_window_end"] = runtime.snapshot()
            result["repository_calls_ms"] = measured.snapshot()
            with probe_lock:
                result["realtime_callback_seconds"] = distribution(list(delivered.values()))
                result["realtime_probes_sent"] = len(pending)
                result["realtime_probes_pending_at_window_end"] = len(pending) - len(delivered)
            result["mock_requests_at_window_end"] = len(client.batch_sizes)
            result["mock_batch_sizes"] = distribution(client.batch_sizes[:])
            result["memory_at_window_end"] = memory()
            result["refresh_per_second"] = result["runtime_at_window_end"].get("refresh_success", 0) / result["observed_seconds"]
            rate = result["refresh_per_second"]
            result["extrapolated_full_sweep_minutes_not_measured"] = count / rate / 60 if rate else None
            runtime.close(timeout=None)
            # Post-stop verification counts committed rows, not just API returns.
            with pool.connection() as conn:
                result["post_stop_database"] = dict(conn.execute(
                    "SELECT count(*) FILTER (WHERE affiliation_fetched_at > %s) AS fresh_profiles, "
                    "count(*) AS all_profiles FROM personnel_profiles", (time.time() - duration - 30,)).fetchone())
                result["post_stop_failed_jobs"] = conn.execute(
                    "SELECT count(*) AS n FROM personnel_refresh_jobs WHERE failures>0").fetchone()["n"]
            hot_times = []
            cid = next(iter(pending))
            for _ in range(1000):
                started = time.perf_counter()
                runtime.profile(cid)
                hot_times.append((time.perf_counter() - started) * 1e6)
            result["hot_profile_read_microseconds"] = distribution(hot_times)
            return result
        finally:
            if runtime:
                runtime.close(timeout=None)
            if pool:
                pool.close()
            # Only our randomly named, newly created schema is removed.
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", nargs="+", type=int, default=[100000, 300000, 500000])
    parser.add_argument("--seconds", type=float, default=45)
    parser.add_argument("--latency", type=float, default=0.2)
    parser.add_argument("--confirm-local-test", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dsn = os.environ.get("EVE_SENTRY_TEST_POSTGRES_DSN", "")
    validate_local_dsn(dsn)
    if not all(1000 <= count <= 500000 for count in args.rows) or not 10 <= args.seconds <= 120 or not 0 <= args.latency <= 2:
        parser.error("Require 1000..500000 rows, 10..120 seconds and 0..2 seconds mock latency")
    if args.output.exists():
        parser.error("Output must be a new file")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "platform": platform.platform(), "python": platform.python_version(),
              "logical_cpus": os.cpu_count(), "initial_memory": memory(),
              "scope": "Real current archive/runtime; synthetic SQL data and ESI; no HTTP, SSE, bot or production load",
              "cases": []}
    try:
        for count in args.rows:
            print(f"Seeding isolated {count} profile / {2 * count} job case", flush=True)
            result["cases"].append(run_case(dsn, count, args.seconds, args.latency))
            print(json.dumps(result["cases"][-1], ensure_ascii=False), flush=True)
    finally:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Report: {args.output.resolve()}", flush=True)


if __name__ == "__main__":
    main()
