from collections import Counter
from datetime import datetime, timedelta, timezone
import csv, io

def parse_dt(value):
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def build_weekly_report(records, days=7, owners=None, now=None):
    owners = owners or {}
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    current = [r for r in records if parse_dt(r.get("timestamp")) and start <= parse_dt(r.get("timestamp")) <= now]
    counts = Counter(r.get("issue_type", "other") for r in current)
    top = []
    for issue, count in counts.most_common(5):
        examples = [r for r in current if r.get("issue_type", "other") == issue][:3]
        top.append({"issue_type": issue, "count": count, "department": owners.get(issue, "Unassigned"),
                    "stores": sorted(set(r.get("store_id", "unknown") for r in current if r.get("issue_type", "other") == issue)),
                    "evidence": [{"feedback_id": r.get("id"), "store_id": r.get("store_id"), "quote": (r.get("original_text") or "")[:220]} for r in examples],
                    "recommended_action": "Review the original evidence, verify the cause with the store, and assign a corrective action owner."})
    high = [r for r in current if r.get("severity") == "High"]
    return {"period": {"start": start.isoformat(), "end": now.isoformat(), "days": days},
            "basis": "Observed feedback counts only; no transaction or footfall denominator is assumed.",
            "feedback_count": len(current), "top_issues": top,
            "high_severity_evidence": [{"id": r.get("id"), "store_id": r.get("store_id"), "issue_type": r.get("issue_type"), "quote": (r.get("original_text") or "")[:220]} for r in high[:15]],
            "limitations": ["Demo records are fictional. Align labels and owner mappings with the official challenge taxonomy.",
                            "Official planted-incident lead-time scoring requires the challenge dataset and evaluator."]}

def report_csv(report):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["period_start", "period_end", "basis", "issue_type", "count", "department", "stores", "evidence_quotes", "recommended_action"])
    for item in report["top_issues"]:
        writer.writerow([report["period"]["start"], report["period"]["end"], report["basis"], item["issue_type"], item["count"], item["department"], "; ".join(item["stores"]), " | ".join(e["quote"] for e in item["evidence"]), item["recommended_action"]])
    return out.getvalue()
