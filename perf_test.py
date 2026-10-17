#!/usr/bin/env python
"""Reproducible performance test: push N synthetic records (default 40,000) through the REAL application pipeline
(bulk ingest + batch classification -> dedup -> detection -> incidents -> regional briefs -> API reads) on a temp SQLite DB.

    python perf_test.py [--n 40000] [--seed 41] [--json reports/perf_report.json]

Reports wall-clock per stage, peak memory, DB indexes and the machine it ran on. Synthetic data only."""
import argparse, json, os, platform, resource, sqlite3, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

def env_info():
    cpu = "unknown"
    try: cpu = next(l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name"))
    except Exception: pass
    mem = None
    try: mem = round(int(next(l for l in open("/proc/meminfo") if l.startswith("MemTotal")).split()[1]) / 1048576, 1)
    except Exception: pass
    import sklearn, numpy, scipy, flask
    return {"platform": platform.platform(), "cpu": cpu, "logical_cores": os.cpu_count(), "ram_gb": mem, "python": platform.python_version(),
            "sklearn": sklearn.__version__, "numpy": numpy.__version__, "scipy": scipy.__version__, "flask": flask.__version__, "sqlite": sqlite3.sqlite_version}

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--n", type=int, default=40000); ap.add_argument("--seed", type=int, default=41); ap.add_argument("--json")
    a = ap.parse_args()
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False); tmp.close()
    os.environ["R5_DB_PATH"] = tmp.name
    import app as appmod
    from services.synthetic import generate_dataset
    from services.classifier import get_model
    get_model()                                             # load the model outside the timed region (one-off, reported separately)
    t = time.time(); d = generate_dataset(seed=a.seed, n_records=a.n); t_gen = time.time() - t
    recs = [{k: r[k] for k in ("id", "store_id", "timestamp", "channel", "text")} for r in d["records"]]
    appmod.import_stores(d["stores"])
    st = {}
    t = time.time(); res = appmod.ingest_records(recs); st["ingest_classify_insert_s"] = round(time.time() - t, 2)
    t = time.time(); appmod.sync_first_signal(force=True); st["dedup_detect_incidents_s"] = round(time.time() - t, 2)
    c = appmod.app.test_client()
    t = time.time(); inc = c.get("/api/incidents").get_json(); st["api_incidents_s"] = round(time.time() - t, 2)
    t = time.time(); regs = c.get("/api/reports/regional").get_json()["briefs"]; st["regional_briefs_all_regions_s"] = round(time.time() - t, 2)
    t = time.time(); page = c.get("/brief/North"); st["brief_html_page_s"] = round(time.time() - t, 2)
    t = time.time(); c.get("/api/dedup/summary"); st["api_dedup_summary_s"] = round(time.time() - t, 2)
    t = time.time(); c.get("/api/overview"); st["api_overview_cached_s"] = round(time.time() - t, 2)
    st["total_pipeline_s"] = round(st["ingest_classify_insert_s"] + st["dedup_detect_incidents_s"], 2)
    con = sqlite3.connect(tmp.name)
    idx = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='feedback' AND name LIKE 'idx_%'")]
    plan = [r[3] for r in con.execute("EXPLAIN QUERY PLAN SELECT * FROM feedback WHERE store_id=? AND timestamp>=?", ("ST-001", "2026-09-01"))]
    n_rows = con.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]; n_canon = con.execute("SELECT COUNT(*) FROM feedback WHERE canonical_id=id").fetchone()[0]
    out = {"records_requested": a.n, "records_inserted": res["inserted"], "rows_in_db": n_rows, "canonical": n_canon, "incidents": len(inc),
           "brief_status": page.status_code, "regions_briefed": len(regs), "stage_seconds": st, "records_per_second_end_to_end": round(res["inserted"] / st["total_pipeline_s"]),
           "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1), "synthetic_generation_s": round(t_gen, 2),
           "feedback_indexes": idx, "query_plan_store_time": plan, "environment": env_info(),
           "note": "SYNTHETIC data, single run on the machine above. Timings vary with hardware; rerun to reproduce."}
    print(json.dumps(out, indent=2))
    if a.json: Path(a.json).write_text(json.dumps(out, indent=2), encoding="utf-8")
    os.unlink(tmp.name)
    ok = res["inserted"] == a.n and n_rows == a.n and page.status_code == 200
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
