from __future__ import annotations
import csv, io, json, os, sqlite3, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from flask import Flask, jsonify, render_template, request, send_file, abort
import hashlib
from collections import Counter, defaultdict
from services.classifier import classify_batch, normalize_text
from services.reports import build_weekly_report, report_csv
from services.taxonomy import OWNERS as ISSUE_OWNERS, SAFETY_ISSUES, ISSUES, escalation_rule, owner_of, is_safety
from services.detector import safety_flags, detect_emerging, to_epoch, iso
from services.dedup import cluster_duplicates, check_window, DEFAULT_WINDOW, DEFAULT_THRESHOLD
from services.brief import build_regional_brief, regions_of, UNMAPPED
from services import metrics as M

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("R5_DB_PATH", BASE / "data" / "r5.sqlite3"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

HIGH_RISK = SAFETY_ISSUES
DEDUP_WINDOW_HOURS = check_window(os.environ.get("R5_DEDUP_WINDOW_HOURS", DEFAULT_WINDOW))   # configurable, 24-48h
DEDUP_THRESHOLD = float(os.environ.get("R5_DEDUP_THRESHOLD", DEFAULT_THRESHOLD))
LOOKBACK_DAYS = int(os.environ.get("R5_LOOKBACK_DAYS", 28))   # incidents surface for flags raised within this many days of the newest record
_SYNC_CACHE = {}        # db path -> signature of the last synced state
_LAST_FLAGS = {}        # db path -> {"safety": [...], "emerging": [...], "as_of": iso}

def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS feedback (
          id TEXT PRIMARY KEY, source_id TEXT, store_id TEXT NOT NULL,
          timestamp TEXT NOT NULL, channel TEXT NOT NULL, original_text TEXT NOT NULL,
          cleaned_text TEXT NOT NULL, issue_type TEXT NOT NULL, severity TEXT NOT NULL,
          actionable INTEGER NOT NULL, confidence REAL NOT NULL, status TEXT NOT NULL DEFAULT 'New',
          metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_feedback_store_time ON feedback(store_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_feedback_issue ON feedback(issue_type);
        CREATE TABLE IF NOT EXISTS incidents (
          id TEXT PRIMARY KEY, store_id TEXT NOT NULL, issue_type TEXT NOT NULL,
          summary TEXT NOT NULL, why_raised TEXT NOT NULL, report_count INTEGER NOT NULL,
          first_seen TEXT NOT NULL, latest_seen TEXT NOT NULL, severity TEXT NOT NULL,
          department TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Detected — awaiting review',
          assigned_user TEXT, priority TEXT NOT NULL, due_at TEXT, next_action TEXT NOT NULL,
          resolution TEXT, root_cause TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS incident_evidence (
          incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
          feedback_id TEXT NOT NULL REFERENCES feedback(id),
          PRIMARY KEY (incident_id, feedback_id)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL,
          actor TEXT NOT NULL, action TEXT NOT NULL, old_value TEXT, new_value TEXT,
          note TEXT, timestamp TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stores (
          store_id TEXT PRIMARY KEY, store_name TEXT, region TEXT NOT NULL, city TEXT, size_tier TEXT
        );
        CREATE TABLE IF NOT EXISTS resolution_memory (
          id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL,
          issue_type TEXT NOT NULL, store_id TEXT NOT NULL, root_cause TEXT,
          actions TEXT NOT NULL, outcome TEXT NOT NULL, recorded_at TEXT NOT NULL
        );
        """)
        c.commit()
        _ensure_columns(c, "feedback", {"pred_issue": "TEXT", "pred_severity": "TEXT", "pred_actionable": "INTEGER", "label_issue": "TEXT",
                                        "label_severity": "TEXT", "label_actionable": "INTEGER", "canonical_id": "TEXT",
                                        "classifier_method": "TEXT", "safety_override": "TEXT"})
        _ensure_columns(c, "incidents", {"kind": "TEXT", "stats_json": "TEXT", "escalation_rule": "TEXT"})
        c.executescript("""CREATE INDEX IF NOT EXISTS idx_feedback_ts ON feedback(timestamp);
                           CREATE INDEX IF NOT EXISTS idx_feedback_canon ON feedback(canonical_id);
                           CREATE INDEX IF NOT EXISTS idx_feedback_store_issue_ts ON feedback(store_id, issue_type, timestamp);
                           CREATE INDEX IF NOT EXISTS idx_stores_region ON stores(region);""")
        c.commit()

def _ensure_columns(c, table, cols):
    have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    for name, typ in cols.items():
        if name not in have:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")

def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def rowdict(row):
    return dict(row) if row else None

def parse_timestamp(value):
    if not value:
        return now_iso()
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None

def _opt_int(v):
    if v in (None, ""): return None
    try: return int(float(v))
    except (TypeError, ValueError): return 1 if str(v).strip().lower() in {"true", "yes", "y"} else 0 if str(v).strip().lower() in {"false", "no", "n"} else None

def ingest_records(records, batch=5000):
    """Validate, batch-classify, and bulk-insert feedback in one transaction per batch. Returns counts + ids.
    Original text is stored untouched; nothing is ever deleted or overwritten."""
    prepared, errors = [], []
    for n, rec in enumerate(records, start=1):
        original = str(rec.get("text") or rec.get("feedback") or rec.get("comment") or "").strip()
        store_id = str(rec.get("store_id") or "").strip()
        ts = parse_timestamp(rec.get("timestamp") or rec.get("created_at"))
        if not original or not store_id or not ts:
            errors.append({"row": n, "error": "Missing text/store_id or invalid timestamp"}); continue
        prepared.append((rec, original, store_id, ts))
    inserted = dups = 0; ids = []
    skip = {"text", "feedback", "comment"}
    with connect() as c:
        for k in range(0, len(prepared), batch):
            chunk = prepared[k:k + batch]
            preds = classify_batch([p[1] for p in chunk])
            rows = []
            for (rec, original, store_id, ts), pr in zip(chunk, preds):
                supplied = rec.get("issue_type")
                issue = str(supplied or pr["issue_type"])
                severity = str(rec.get("severity") or pr["severity"])
                actionable = rec.get("actionable"); actionable = bool(_opt_int(actionable)) if actionable not in (None, "") else bool(pr["actionable"])
                fid = str(rec.get("id") or rec.get("feedback_id") or uuid.uuid4().hex[:12]); ids.append(fid)
                meta = {a: b for a, b in rec.items() if a not in skip}
                rows.append((fid, str(rec.get("source_id") or ""), store_id, ts, str(rec.get("channel") or rec.get("source") or "unknown"), original,
                             normalize_text(original), issue, severity, int(actionable), float(pr["confidence"]), "New", json.dumps(meta, ensure_ascii=False), now_iso(),
                             pr["issue_type"], pr["severity"], int(pr["actionable"]),
                             rec.get("label_issue_type") or None, rec.get("label_severity") or None, _opt_int(rec.get("label_actionable")),
                             pr["method"], pr.get("override")))
            before = c.total_changes
            c.executemany("""INSERT OR IGNORE INTO feedback
              (id,source_id,store_id,timestamp,channel,original_text,cleaned_text,issue_type,severity,actionable,confidence,status,metadata_json,created_at,
               pred_issue,pred_severity,pred_actionable,label_issue,label_severity,label_actionable,classifier_method,safety_override)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
            n_ins = c.total_changes - before
            inserted += n_ins; dups += len(rows) - n_ins
        c.commit()
    return {"inserted": inserted, "duplicates": dups, "errors": errors, "ids": ids}

def create_feedback(record):
    res = ingest_records([record])
    if res["errors"]: return None, res["errors"][0]["error"]
    fid = res["ids"][0]
    return (fid, "Duplicate ID ignored") if res["duplicates"] else (fid, None)

def get_feedback(filters=None):
    filters = filters or {}
    sql = "SELECT * FROM feedback WHERE 1=1"
    args = []
    for key, col in [("store_id","store_id"),("issue_type","issue_type"),("severity","severity"),("channel","channel"),("status","status")]:
        if filters.get(key):
            sql += f" AND {col}=?"; args.append(filters[key])
    if filters.get("q"):
        sql += " AND (original_text LIKE ? OR id LIKE ?)"
        args += [f"%{filters['q']}%", f"%{filters['q']}%"]
    if filters.get("start"):
        sql += " AND timestamp>=?"; args.append(filters["start"])
    if filters.get("end"):
        sql += " AND timestamp<?"; args.append(filters["end"])
    sql += " ORDER BY timestamp DESC LIMIT ?"
    args.append(min(int(filters.get("limit", 500)), 5000))
    with connect() as c:
        return [rowdict(r) for r in c.execute(sql, args).fetchall()]

def _dedup_pass(c):
    """Link likely cross-channel duplicates to a canonical record. Only records without a canonical_id (new) plus the
    preceding window of already-linked records are re-clustered, so single POSTs stay cheap. No row is deleted."""
    new = c.execute("SELECT MIN(timestamp) FROM feedback WHERE canonical_id IS NULL").fetchone()[0]
    if new is None: return 0
    lo = iso(to_epoch(new) - (DEDUP_WINDOW_HOURS + 1) * 3600)
    rows = [rowdict(r) for r in c.execute("SELECT id,store_id,issue_type,channel,timestamp,cleaned_text,canonical_id FROM feedback WHERE timestamp>=? OR canonical_id IS NULL", (lo,))]
    if not rows: return 0
    res = cluster_duplicates(rows, DEDUP_WINDOW_HOURS, DEDUP_THRESHOLD)
    old = {r["id"]: r["canonical_id"] for r in rows}
    upd = [(res["canonical"][i], i) for i in old if old[i] != res["canonical"][i]]
    c.executemany("UPDATE feedback SET canonical_id=? WHERE id=?", upd)
    return len(upd)

def _sig(c):
    return (str(DB_PATH), c.execute("SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM feedback").fetchone()[:])

def _upsert_incident(c, sig_id, store, issue, kind, summary, why, rows, first, latest, severity, stats):
    ids = [r["id"] for r in rows]
    canon = sum(1 for r in rows if r["canonical_id"] in (None, r["id"]))
    due_h = 4 if severity == "High" else 24
    stats_json = json.dumps(stats, default=str)
    if c.execute("SELECT 1 FROM incidents WHERE id=?", (sig_id,)).fetchone():
        c.execute("""UPDATE incidents SET report_count=?,first_seen=?,latest_seen=?,updated_at=?,kind=?,stats_json=?,escalation_rule=?,why_raised=?
                     WHERE id=? AND status='Detected — awaiting review'""", (canon, first, latest, now_iso(), kind, stats_json, escalation_rule(issue), why, sig_id))
        c.execute("UPDATE incidents SET kind=?,stats_json=?,escalation_rule=? WHERE id=? AND kind IS NULL", (kind, stats_json, escalation_rule(issue), sig_id))
    else:
        c.execute("""INSERT INTO incidents(id,store_id,issue_type,summary,why_raised,report_count,first_seen,latest_seen,severity,department,status,assigned_user,
                     priority,due_at,next_action,created_at,updated_at,kind,stats_json,escalation_rule) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (sig_id, store, issue, summary, why, canon, first, latest, severity, owner_of(issue), "Detected — awaiting review", None, severity,
                   iso(to_epoch(latest) + due_h * 3600), "Review original evidence, contact the store, and record a verification decision.", now_iso(), now_iso(),
                   kind, stats_json, escalation_rule(issue)))
    for fid in ids: c.execute("INSERT OR IGNORE INTO incident_evidence VALUES(?,?)", (sig_id, fid))

def sync_first_signal(force=False):
    """Pipeline: dedup (link duplicates) -> immediate safety flags + statistical emerging-issue detection -> incidents."""
    with connect() as c:
        sig = _sig(c)
        if not force and _SYNC_CACHE.get(str(DB_PATH)) == sig: return _LAST_FLAGS.get(str(DB_PATH))
        _dedup_pass(c); c.commit()
        recs = [rowdict(r) for r in c.execute("SELECT id,store_id,issue_type,timestamp,canonical_id,safety_override,channel,original_text FROM feedback")]
        if not recs:
            _SYNC_CACHE[str(DB_PATH)] = sig; _LAST_FLAGS[str(DB_PATH)] = {"safety": [], "emerging": [], "as_of": None}; return _LAST_FLAGS[str(DB_PATH)]
        as_of_ts = max(to_epoch(r["timestamp"]) or 0 for r in recs); as_of = iso(as_of_ts)
        canon = [r for r in recs if r["canonical_id"] in (None, r["id"])]
        by_key = defaultdict(list)
        for r in recs: by_key[(str(r["store_id"]), r["issue_type"])].append(r)
        # 1) safety-critical: immediate, thresholds bypassed. Only reports from the last LOOKBACK_DAYS surface as incidents.
        recent = [r for r in canon if (to_epoch(r["timestamp"]) or 0) >= as_of_ts - LOOKBACK_DAYS * 86400]
        sflags = safety_flags(recent, is_safety=lambda r: bool(r.get("safety_override")))
        for f in sflags:
            issue = f["issue_type"]; rows = [r for r in by_key[(f["store_id"], issue)] if (to_epoch(r["timestamp"]) or 0) >= as_of_ts - LOOKBACK_DAYS * 86400]
            sid = "FS-" + hashlib.sha1(f"{f['store_id']}|{issue}".encode()).hexdigest()[:10].upper()
            _upsert_incident(c, sid, f["store_id"], issue, "safety", f"{issue.replace('_', ' ').title()} reports at {f['store_id']}",
                             "Mandatory safety/staff-risk issue: escalated on first report regardless of volume (thresholds bypassed).",
                             rows, f["first_detected_at"], f["latest_report_at"], "High", {"rule": "safety_bypass", "report_count": f["report_count"]})
        # 2) statistical emerging issues (needs 35 days of history for a full 4-week baseline + 7-day window)
        eflags = detect_emerging(canon, as_of=as_of, scan_from=iso(as_of_ts - LOOKBACK_DAYS * 86400))
        for f in eflags:
            issue = f["issue_type"]
            end_ts = as_of_ts if f["active_now"] else to_epoch(f["latest_flag_at"])
            rows = [r for r in by_key[(f["store_id"], issue)] if end_ts - 7 * 86400 < (to_epoch(r["timestamp"]) or 0) <= end_ts]
            if not rows: continue
            times = sorted(r["timestamp"] for r in rows)
            sid = "FS-" + hashlib.sha1(f"{f['store_id']}|{issue}".encode()).hexdigest()[:10].upper()
            st = f["at_first_detection"]
            now_txt = (f"Still elevated now: {f['count_current']} reports in 7 days vs {f['expected_current']} expected ({f['rate_per_1000_current']} vs {f['rate_per_1000_baseline']} per 1,000 records, {f['ratio_current']}x, Poisson p={f['p_value_current']:.2g}, z={f['z_current']}). "
                       if f["active_now"] else "No longer elevated at the latest data. ")
            why = (f"Emerging: first flagged {f['first_detected_at'][:16]} with {st['count']} reports in 7 days vs {st['expected']} expected from this store's 4-week baseline "
                   f"({st['ratio']}x, Poisson p={st['p_value']:.2g}). " + now_txt)
            _upsert_incident(c, sid, f["store_id"], issue, "emerging", f"{issue.replace('_', ' ').title()} rising at {f['store_id']}", why, rows,
                             times[0], times[-1], "Medium", {k: v for k, v in f.items() if k not in ("episodes",)})
        c.commit()
        _SYNC_CACHE[str(DB_PATH)] = sig
        _LAST_FLAGS[str(DB_PATH)] = {"safety": sflags, "emerging": eflags, "as_of": as_of}
        return _LAST_FLAGS[str(DB_PATH)]

@app.get("/")
def index():
    return render_template("index.html")

@app.get("/api/health")
def health():
    with connect() as c:
        count = c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        incidents = c.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
    return jsonify({"status":"ok","feedback_count":count,"incident_count":incidents})

@app.get("/api/overview")
def overview():
    sync_first_signal()
    with connect() as c:
        total = c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        unresolved = c.execute("SELECT COUNT(*) FROM incidents WHERE status NOT IN ('Resolved','Not substantiated')").fetchone()[0]
        high = c.execute("SELECT COUNT(*) FROM incidents WHERE severity='High' AND status NOT IN ('Resolved','Not substantiated')").fetchone()[0]
        recent = [rowdict(r) for r in c.execute("SELECT * FROM incidents WHERE status NOT IN ('Resolved','Not substantiated') ORDER BY CASE severity WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, latest_seen DESC LIMIT 6")]
    return jsonify({"feedback_count":total,"unresolved_count":unresolved,"high_priority_count":high,"incidents":recent})

@app.get("/api/feedback")
def feedback_api():
    return jsonify(get_feedback(request.args))

@app.post("/api/feedback")
def add_feedback_api():
    data = request.get_json(silent=True) or {}
    fid, error = create_feedback(data)
    if error and error != "Duplicate ID ignored":
        return jsonify({"error":error}), 400
    sync_first_signal()
    return jsonify({"id":fid,"message":error or "Feedback ingested"}), 201 if not error else 200

@app.post("/api/feedback/import")
def import_feedback():
    if "file" not in request.files:
        return jsonify({"error":"Choose a CSV or JSON file"}), 400
    file = request.files["file"]
    name = file.filename.lower()
    raw = file.read().decode("utf-8-sig", errors="replace")
    try:
        if name.endswith(".json"):
            data = json.loads(raw)
            records = data if isinstance(data, list) else data.get("records", [])
        elif name.endswith(".csv"):
            records = list(csv.DictReader(io.StringIO(raw)))
        else:
            return jsonify({"error":"Only CSV and JSON imports are supported"}), 400
    except Exception as exc:
        return jsonify({"error":f"Could not parse file: {exc}"}), 400
    res = ingest_records(records)
    sync_first_signal()
    return jsonify({"inserted": res["inserted"], "duplicates": res["duplicates"], "invalid": len(res["errors"]), "errors": res["errors"][:30]})

@app.get("/api/incidents")
def incidents_api():
    sync_first_signal()
    with connect() as c:
        rows = c.execute("SELECT * FROM incidents ORDER BY CASE severity WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, updated_at DESC").fetchall()
        result = []
        for row in rows:
            item = rowdict(row)
            ev = c.execute("""SELECT f.id,f.timestamp,f.channel,f.original_text,f.issue_type,f.severity,f.canonical_id
                              FROM feedback f JOIN incident_evidence e ON e.feedback_id=f.id
                              WHERE e.incident_id=? ORDER BY f.timestamp""",(item["id"],)).fetchall()
            item["evidence"] = [rowdict(x) for x in ev][:60]
            item["evidence_ids"] = [x["id"] for x in ev]
            item["channel_counts"] = dict(Counter(x["channel"] for x in ev))
            canon_ids = {x["canonical_id"] or x["id"] for x in c.execute("SELECT f.id,f.canonical_id FROM feedback f JOIN incident_evidence e ON e.feedback_id=f.id WHERE e.incident_id=?", (item["id"],))}
            item["unique_reports"] = len(canon_ids)
            item["duplicate_reports_linked"] = max(0, len(ev) - len(canon_ids))
            item["escalation_rule"] = item.get("escalation_rule") or escalation_rule(item["issue_type"])
            item["kind"] = item.get("kind") or ("safety" if item["issue_type"] in SAFETY_ISSUES else "cluster")
            try: item["stats"] = json.loads(item.get("stats_json") or "{}")
            except ValueError: item["stats"] = {}
            item["audit"] = [rowdict(x) for x in c.execute("SELECT * FROM audit_log WHERE incident_id=? ORDER BY timestamp DESC",(item["id"],))]
            result.append(item)
    status = request.args.get("status")
    if status: result = [r for r in result if r["status"] == status]
    return jsonify(result)

@app.post("/api/incidents/<incident_id>/view")
def track_incident_view(incident_id):
    data = request.get_json(silent=True) or {}
    with connect() as c:
        if not c.execute("SELECT 1 FROM incidents WHERE id=?", (incident_id,)).fetchone():
            return jsonify({"error": "Incident not found"}), 404
        c.execute("INSERT INTO audit_log(incident_id,actor,action,old_value,new_value,note,timestamp) VALUES(?,?,?,?,?,?,?)",
                  (incident_id, str(data.get("actor") or "Reviewer"), "problem_statement_viewed", None, "opened",
                   "User opened the full problem statement from the View option", now_iso()))
        c.commit()
    return jsonify({"message": "Problem statement view tracked"})

@app.patch("/api/incidents/<incident_id>")
def update_incident(incident_id):
    data = request.get_json(silent=True) or {}
    allowed = {"status","assigned_user","department","severity","priority","due_at","next_action","resolution","root_cause"}
    changes = {k:v for k,v in data.items() if k in allowed}
    if not changes:
        return jsonify({"error":"No supported fields supplied"}),400
    with connect() as c:
        old = c.execute("SELECT * FROM incidents WHERE id=?",(incident_id,)).fetchone()
        if not old: return jsonify({"error":"Incident not found"}),404
        old = rowdict(old)
        for key, value in changes.items():
            c.execute(f"UPDATE incidents SET {key}=?,updated_at=? WHERE id=?",(value,now_iso(),incident_id))
            c.execute("INSERT INTO audit_log(incident_id,actor,action,old_value,new_value,note,timestamp) VALUES(?,?,?,?,?,?,?)",
                      (incident_id,str(data.get("actor") or "Reviewer"),key,str(old.get(key)),str(value),str(data.get("note") or ""),now_iso()))
        current = rowdict(c.execute("SELECT * FROM incidents WHERE id=?",(incident_id,)).fetchone())
        if current["status"] == "Resolved" and (current.get("resolution") or current.get("root_cause")):
            c.execute("""INSERT INTO resolution_memory(incident_id,issue_type,store_id,root_cause,actions,outcome,recorded_at)
                         VALUES(?,?,?,?,?,?,?)""",(incident_id,current["issue_type"],current["store_id"],current.get("root_cause"),
                         current.get("resolution") or "No action details recorded",current["status"],now_iso()))
        c.commit()
    return jsonify({"message":"Incident updated","incident":current})

@app.post("/api/incidents/<incident_id>/evidence")
def attach_evidence(incident_id):
    data = request.get_json(silent=True) or {}
    feedback_id = str(data.get("feedback_id") or "")
    with connect() as c:
        if not c.execute("SELECT 1 FROM incidents WHERE id=?",(incident_id,)).fetchone(): return jsonify({"error":"Incident not found"}),404
        if not c.execute("SELECT 1 FROM feedback WHERE id=?",(feedback_id,)).fetchone(): return jsonify({"error":"Feedback not found"}),404
        c.execute("INSERT OR IGNORE INTO incident_evidence VALUES(?,?)",(incident_id,feedback_id))
        c.execute("INSERT INTO audit_log(incident_id,actor,action,new_value,note,timestamp) VALUES(?,?,?,?,?,?)",
                  (incident_id,str(data.get("actor") or "Reviewer"),"evidence_linked",feedback_id,str(data.get("note") or ""),now_iso()))
    return jsonify({"message":"Evidence linked; original record preserved"})

@app.get("/api/reports/weekly")
def weekly_report():
    days = int(request.args.get("days",7))
    days = min(max(days,1),31)
    report = build_weekly_report(get_feedback({"limit":5000}), days=days, owners=ISSUE_OWNERS)
    return jsonify(report)

@app.get("/api/reports/weekly.csv")
def weekly_csv():
    report = build_weekly_report(get_feedback({"limit":5000}), days=7, owners=ISSUE_OWNERS)
    out = io.BytesIO(report_csv(report).encode("utf-8-sig"))
    return send_file(out, mimetype="text/csv", as_attachment=True, download_name="r5_weekly_brief.csv")

# ---------------------------------------------------------------- stores / regions / regional brief
def load_stores():
    with connect() as c:
        return {r["store_id"]: rowdict(r) for r in c.execute("SELECT * FROM stores")}

def import_stores(records):
    ok = 0
    with connect() as c:
        for r in records:
            sid = str(r.get("store_id") or "").strip(); region = str(r.get("region") or "").strip()
            if not sid or not region: continue
            c.execute("INSERT OR REPLACE INTO stores(store_id,store_name,region,city,size_tier) VALUES(?,?,?,?,?)",
                      (sid, r.get("store_name") or sid, region, r.get("city") or "", r.get("size_tier") or ""))
            ok += 1
        c.commit()
    return ok

@app.post("/api/stores/import")
def stores_import():
    if "file" in request.files:
        f = request.files["file"]; raw = f.read().decode("utf-8-sig", errors="replace")
        try:
            recs = json.loads(raw) if f.filename.lower().endswith(".json") else list(csv.DictReader(io.StringIO(raw)))
        except Exception as exc:
            return jsonify({"error": f"Could not parse file: {exc}"}), 400
    else:
        data = request.get_json(silent=True); recs = data if isinstance(data, list) else (data or {}).get("stores", [])
    n = import_stores(recs)
    if not n: return jsonify({"error": "No valid stores. Each needs store_id and region."}), 400
    return jsonify({"stores_loaded": n})

@app.get("/api/stores")
def stores_api():
    return jsonify(list(load_stores().values()))

def _brief_context():
    sync_first_signal()
    with connect() as c:
        recs = [rowdict(r) for r in c.execute("SELECT id,store_id,issue_type,channel,timestamp,original_text,canonical_id FROM feedback")]
    stores = load_stores()
    as_of = request.args.get("as_of") or (_LAST_FLAGS.get(str(DB_PATH)) or {}).get("as_of") or now_iso()
    flags = ((_LAST_FLAGS.get(str(DB_PATH)) or {}).get("emerging") or [])
    flags = [f for f in flags if f.get("active_now")]
    days = min(max(int(request.args.get("days", 7)), 1), 31)
    return recs, stores, as_of, days, flags

def _all_regions(recs, stores):
    regs = set(regions_of(stores))
    if any(str(r["store_id"]) not in stores for r in recs): regs.add(UNMAPPED)
    return sorted(regs)

@app.get("/api/regions")
def regions_api():
    recs, stores, as_of, days, _ = _brief_context()
    return jsonify({"as_of": as_of, "regions": [{"region": r, "stores": sum(1 for s in stores.values() if s["region"] == r)} for r in _all_regions(recs, stores)]})

@app.get("/api/reports/regional")
def regional_api():
    recs, stores, as_of, days, flags = _brief_context()
    region = request.args.get("region")
    regs = [region] if region else _all_regions(recs, stores)
    if region and region not in _all_regions(recs, stores): return jsonify({"error": f"Unknown region '{region}'"}), 404
    return jsonify({"as_of": as_of, "briefs": [build_regional_brief(recs, stores, r, as_of, days, flags=flags) for r in regs]})

@app.get("/brief")
def brief_index():
    recs, stores, as_of, days, _ = _brief_context()
    return render_template("brief_index.html", regions=_all_regions(recs, stores), as_of=as_of)

@app.get("/brief/<region>")
def brief_page(region):
    recs, stores, as_of, days, flags = _brief_context()
    if region not in _all_regions(recs, stores): return "Unknown region", 404
    b = build_regional_brief(recs, stores, region, as_of, days, flags=flags)
    return render_template("brief.html", b=b, generated=now_iso(), regions=_all_regions(recs, stores))

# ---------------------------------------------------------------- escalation precision & dedup summary
@app.get("/api/evaluation/escalation")
def escalation_precision_api():
    """Precision of escalated incidents against ground-truth label_actionable stored with the feedback. Reports why if unavailable."""
    sync_first_signal()
    with connect() as c:
        n_total = c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        n_lab = c.execute("SELECT COUNT(*) FROM feedback WHERE label_actionable IS NOT NULL").fetchone()[0]
        incs = [rowdict(r) for r in c.execute("SELECT id,store_id,issue_type,kind FROM incidents WHERE kind IN ('safety','emerging')")]
        esc = []
        for i in incs:
            ev = [r[0] for r in c.execute("SELECT feedback_id FROM incident_evidence WHERE incident_id=?", (i["id"],))]
            esc.append({"kind": i["kind"], "key": (i["store_id"], i["issue_type"]), "evidence": ev, "incident_id": i["id"]})
        truth = {r["id"]: {"label_actionable": r["label_actionable"]} for r in c.execute("SELECT id,label_actionable FROM feedback WHERE label_actionable IS NOT NULL")}
        full = None
        if n_total and n_lab / n_total >= 0.9:
            full = M.proxy_truth_keys([{"store_id": r["store_id"], "label_issue_type": r["label_issue"], "label_actionable": r["label_actionable"], "timestamp": r["timestamp"]}
                                       for r in c.execute("SELECT store_id,label_issue,label_actionable,timestamp FROM feedback WHERE label_actionable IS NOT NULL AND label_issue IS NOT NULL")])
    limitations = ["Precision uses stored ground-truth label_actionable on each escalated incident's evidence (an incident is true when >=50% of its evidence is actionable); predicted values are never used.",
                   "False negatives / recall use a PROXY definition of an actionable incident and are only computed when >=90% of records are labelled."]
    if not esc:
        return jsonify({"available": False, "reason": "No escalated incidents yet.", "limitations": limitations})
    res = M.escalation_metrics(esc, truth, full)
    if res["true_positives"] + res["false_positives"] == 0:
        return jsonify({"available": False, "reason": f"{len(esc)} incident(s) escalated but none of their evidence carries a ground-truth actionable label (label_actionable). "
                        "Labels are hidden/absent in live data, so precision cannot be computed here; run evaluate.py with a labelled sample.", "escalated_total": len(esc), "labelled_feedback": n_lab, "limitations": limitations})
    res.update({"available": True, "labelled_feedback": n_lab, "total_feedback": n_total, "limitations": limitations})
    return jsonify(res)

@app.get("/api/dedup/summary")
def dedup_summary():
    sync_first_signal()
    with connect() as c:
        total = c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        canon = c.execute("SELECT COUNT(*) FROM feedback WHERE canonical_id IS NULL OR canonical_id=id").fetchone()[0]
        big = c.execute("""SELECT canonical_id, COUNT(*) n, GROUP_CONCAT(DISTINCT channel) channels, MIN(store_id) store_id, MIN(issue_type) issue_type
                           FROM feedback WHERE canonical_id IS NOT NULL GROUP BY canonical_id HAVING n>1 ORDER BY n DESC, canonical_id LIMIT 10""").fetchall()
        by_ch = c.execute("SELECT channel, COUNT(*) FROM feedback GROUP BY channel").fetchall()
    return jsonify({"total_reports": total, "canonical_incidents": canon, "linked_duplicates": total - canon,
                    "window_hours": DEDUP_WINDOW_HOURS, "similarity_threshold": DEDUP_THRESHOLD,
                    "channel_counts": {r[0]: r[1] for r in by_ch}, "largest_clusters": [rowdict(r) for r in big],
                    "note": "Duplicates are linked via canonical_id; no source report is deleted or altered."})

@app.get("/api/taxonomy")
def taxonomy_api():
    from services.taxonomy import LABELS, DEFAULT_SEVERITY
    return jsonify([{"issue": i, "label": LABELS[i], "owner": owner_of(i), "default_severity": DEFAULT_SEVERITY[i], "safety": is_safety(i), "escalation_rule": escalation_rule(i)} for i in ISSUES])

@app.get("/api/resolution-memory")
def resolution_memory():
    issue = request.args.get("issue_type")
    with connect() as c:
        sql = "SELECT * FROM resolution_memory"
        args = []
        if issue: sql += " WHERE issue_type=?"; args.append(issue)
        sql += " ORDER BY recorded_at DESC LIMIT 50"
        return jsonify([rowdict(r) for r in c.execute(sql,args).fetchall()])

@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api/"): return jsonify({"error":"API endpoint not found"}),404
    return "Page not found",404

def seed_demo_data_if_empty():
    """Load clearly labelled fictional demo records + a synthetic store master on first local launch only."""
    with connect() as c:
        existing = c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    if existing:
        return 0
    stores_file = BASE / "demo_stores.csv"
    if stores_file.exists():
        with stores_file.open("r", encoding="utf-8-sig", newline="") as f:
            import_stores(list(csv.DictReader(f)))
    demo_file = BASE / "demo_data.csv"
    if not demo_file.exists():
        return 0
    with demo_file.open("r", encoding="utf-8-sig", newline="") as f:
        records = list(csv.DictReader(f))
    res = ingest_records(records)
    if res["inserted"]:
        sync_first_signal()
    return res["inserted"]

init_db()

if os.environ.get("R5_LOAD_DEMO", "1").strip().lower() not in {"0", "false", "no"}:
    seeded = seed_demo_data_if_empty()
    if seeded:
        print(f"Loaded {seeded} fictional demo feedback records.")

if __name__ == "__main__":
    print("R5 Voice-of-Store Engine running")
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False
    )
