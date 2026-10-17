"""Evaluation helpers: classification quality, escalation precision/recall, detection lead time, combined score."""
from collections import defaultdict
from datetime import datetime, timezone
from services.detector import to_epoch
from services.taxonomy import SAFETY_ISSUES

WEIGHTS = {"issue_macro_f1": 0.25, "severity_macro_f1": 0.10, "actionable_f1": 0.10, "escalation_precision": 0.10, "detection_lead_score": 0.45}
GRACE_DAYS = 7          # a detection later than this after the incident start scores 0
PROXY_MIN_ACTIONABLE = 8  # proxy rule for 'ground-truth actionable incident' used only for recall (see escalation_metrics)

def classification_metrics(y_true, y_pred, positive=None):
    from sklearn.metrics import accuracy_score, f1_score
    out = {"n": len(y_true), "accuracy": round(accuracy_score(y_true, y_pred), 4)}
    if positive is None:
        out["macro_f1"] = round(f1_score(y_true, y_pred, average="macro", zero_division=0), 4)
    else:
        out["f1"] = round(f1_score(y_true, y_pred, pos_label=positive, zero_division=0), 4)
    return out

def lead_time_hours(incident_start, first_detection):
    """lead = incident_start - first_detection.  POSITIVE => detected BEFORE the incident start (early)."""
    a, b = to_epoch(incident_start), to_epoch(first_detection)
    if a is None or b is None: return None
    return (a - b) / 3600.0

def detection_score(lead_hours, grace_days=GRACE_DAYS):
    """1.0 if detected at/before incident start; linearly down to 0 at `grace_days` late; 0 if never detected."""
    if lead_hours is None: return 0.0
    if lead_hours >= 0: return 1.0
    return max(0.0, 1.0 + lead_hours / (grace_days * 24.0))

MATCH_DAYS = 14   # a flag counts for a planted incident only if first raised in [onset, onset + 14 days]

def planted_detection(incidents, safety, emerging):
    """Match flags to planted incidents on (store, issue) AND time: only detections at/after the incident's onset (and within
    14 days) count. Earlier flags for the same store+issue are unrelated background and get no credit."""
    cand = defaultdict(list)
    for f in safety:
        cand[(f["store_id"], f["issue_type"])] += list(f.get("report_times") or [f["first_detected_at"]])
    for f in emerging:
        cand[(f["store_id"], f["issue_type"])] += list(f.get("episodes") or [f["first_detected_at"]])
    rows = []
    for inc in incidents:
        lo = to_epoch(inc["onset"]); hi = lo + MATCH_DAYS * 86400
        times = sorted(t for t in cand.get((inc["store_id"], inc["issue_type"]), []) if lo <= to_epoch(t) <= hi)
        det = times[0] if times else None
        lead = lead_time_hours(inc["incident_start"], det) if det else None
        delay_onset = (to_epoch(det) - to_epoch(inc.get("first_report_at") or inc["onset"])) / 3600 if det else None
        rows.append({"name": inc["name"], "store_id": inc["store_id"], "issue_type": inc["issue_type"], "safety": bool(inc.get("safety")),
                     "onset": inc["onset"], "incident_start": inc["incident_start"], "first_report_at": inc.get("first_report_at"), "first_detection": det,
                     "lead_time_hours": None if lead is None else round(lead, 2), "hours_after_first_report": None if delay_onset is None else round(delay_onset, 2),
                     "detected": det is not None, "score": round(detection_score(lead), 4)})
    return rows

def build_escalated(records, safety, emerging, window_days=7):
    """Escalated incidents with their evidence record ids (canonical records only)."""
    by_key = defaultdict(list)
    for r in records: by_key[(str(r["store_id"]), r.get("issue_type"))].append(r)
    out = []
    for f in safety:
        out.append({"kind": "safety", "key": (f["store_id"], f["issue_type"]), "evidence": f["feedback_ids"]})
    for f in emerging:
        t1 = to_epoch(f["first_detected_at"]); t0 = t1 - window_days * 86400
        ev = [r["id"] for r in by_key[(f["store_id"], f["issue_type"])] if t0 <= (to_epoch(r["timestamp"]) or 0) <= t1]
        out.append({"kind": "emerging", "key": (f["store_id"], f["issue_type"]), "evidence": ev})
    return out

def proxy_truth_keys(truth_records, min_actionable=PROXY_MIN_ACTIONABLE, window_days=7):
    """PROXY ground-truth actionable incidents (assumption, not an official definition):
       (store, TRUE issue) with >=1 actionable record of a safety issue, or >= min_actionable actionable records inside any 7-day window."""
    g = defaultdict(list)
    for r in truth_records:
        if int(r.get("label_actionable") or 0) != 1: continue
        t = to_epoch(r["timestamp"])
        if t is not None: g[(str(r["store_id"]), r["label_issue_type"])].append(t)
    keys = set()
    for (s, i), ts in g.items():
        if i in SAFETY_ISSUES: keys.add((s, i)); continue
        ts.sort(); j = 0
        for k in range(len(ts)):
            while ts[k] - ts[j] > window_days * 86400: j += 1
            if k - j + 1 >= min_actionable: keys.add((s, i)); break
    return keys

def escalation_metrics(escalated, truth_by_id, truth_keys=None, majority=0.5):
    """Precision = TP/(TP+FP) over escalated incidents; an escalated incident is TRULY ACTIONABLE when >= `majority`
    of its evidence records carry label_actionable == 1 (ground-truth labels, not predictions).
    FN / recall are over PROXY truth keys (see proxy_truth_keys) and are only indicative."""
    tp = fp = unlabeled = 0
    for e in escalated:
        lab = [int(truth_by_id[i]["label_actionable"]) for i in e["evidence"] if i in truth_by_id and truth_by_id[i].get("label_actionable") not in (None, "")]
        if not lab: unlabeled += 1; e["truly_actionable"] = None; continue
        ok = sum(lab) / len(lab) >= majority
        e["truly_actionable"] = ok
        if ok: tp += 1
        else: fp += 1
    res = {"escalated_total": len(escalated), "true_positives": tp, "false_positives": fp, "unlabelled_escalations": unlabeled,
           "precision": round(tp / (tp + fp), 4) if (tp + fp) else None}
    if truth_keys is not None:
        esc_keys = {e["key"] for e in escalated}
        covered = len(truth_keys & esc_keys); fn = len(truth_keys - esc_keys)
        res.update({"proxy_truth_incidents": len(truth_keys), "false_negatives": fn, "proxy_recall": round(covered / len(truth_keys), 4) if truth_keys else None,
                    "fn_recall_basis": f"PROXY truth: safety-actionable (store,issue) or >={PROXY_MIN_ACTIONABLE} actionable reports in 7 days. Not an official definition."})
    return res

def combined_score(parts):
    """parts: dict component -> value in [0,1] (None counts as 0). Returns (score 0-100, rows)."""
    rows = [{"component": k, "weight": w, "value": round(parts.get(k) or 0.0, 4), "points": round(100 * w * (parts.get(k) or 0.0), 2)} for k, w in WEIGHTS.items()]
    return round(sum(r["points"] for r in rows), 2), rows
