"""Regional weekly brief: top-5 issues by NORMALISED rate (per 1,000 records in the region), affected stores,
original evidence quotes, trend arrow vs the previous week, and the applicable escalation rule."""
from collections import Counter, defaultdict
from datetime import timedelta
from services.detector import to_epoch, iso
from services.taxonomy import is_safety, owner_of, escalation_rule, LABELS

UNMAPPED = "Unmapped"
MAX_SAFETY_SLOTS = 3
MIN_COUNT_FOR_RANK = 2     # single reports of ordinary issues are unverified; ranked only if the top-5 is not otherwise filled
ARROW = {"up": "↑", "stable": "→", "down": "↓"}

def regions_of(stores):
    return sorted({s.get("region") or UNMAPPED for s in stores.values()}) if stores else []

def trend(cur_n, cur_vol, prev_n, prev_vol, rel=0.25):
    """up/stable/down on the rate per 1,000 records. Needs a >=25% relative change AND >=1 report of absolute change."""
    if prev_vol <= 0 and cur_n > 0: return "up"
    exp_prev = prev_n * (cur_vol / prev_vol) if prev_vol else 0.0
    diff = cur_n - exp_prev
    if prev_n == 0 and cur_n >= MIN_COUNT_FOR_RANK: return "up"
    if exp_prev > 0 and diff / exp_prev >= rel and diff >= 1: return "up"
    if exp_prev > 0 and diff / exp_prev <= -rel and diff <= -1: return "down"
    return "stable"

def build_regional_brief(records, stores, region, as_of, days=7, top_n=5, flags=None):
    """records: dicts with id, store_id, issue_type, original_text, channel, timestamp, canonical_id(optional)."""
    end = to_epoch(as_of); start = end - days * 86400; pstart = start - days * 86400
    def region_of(r):
        s = stores.get(str(r["store_id"])); return (s.get("region") if s else None) or UNMAPPED
    cur, prev = [], []
    for r in records:
        if r.get("canonical_id") not in (None, "", r["id"]): continue          # count each de-duplicated incident once
        if region_of(r) != region: continue
        t = to_epoch(r["timestamp"])
        if t is None: continue
        if start < t <= end: cur.append(r)
        elif pstart < t <= start: prev.append(r)
    cv, pv = len(cur), len(prev)
    by_issue = defaultdict(list)
    for r in cur: by_issue[r["issue_type"]].append(r)
    pcount = Counter(r["issue_type"] for r in prev)
    flagged = {}
    for f in (flags or []):
        flagged[(f["store_id"], f["issue_type"])] = f
    items = []
    for issue, rs in by_issue.items():
        if issue == "other": continue
        n = len(rs); safety = is_safety(issue)
        rate = n / cv * 1000 if cv else 0.0; prate = pcount[issue] / pv * 1000 if pv else 0.0
        stores_c = Counter(str(r["store_id"]) for r in rs)
        quotes, seen = [], set()
        for r in sorted(rs, key=lambda x: x["timestamp"], reverse=True):       # prefer one quote per channel, newest first
            if r["channel"] in seen and len(quotes) < 2: continue
            seen.add(r["channel"]); quotes.append(r)
            if len(quotes) == 3: break
        esc = [f for (s, i), f in flagged.items() if i == issue and s in stores_c]
        items.append({
            "issue_type": issue, "label": LABELS.get(issue, issue), "owner": owner_of(issue), "severity": "High" if safety else "Medium" if n >= MIN_COUNT_FOR_RANK else "Low",
            "safety": safety, "count": n, "rate_per_1000": round(rate, 2), "prev_rate_per_1000": round(prate, 2),
            "trend": trend(n, cv, pcount[issue], pv), "verified": safety or n >= MIN_COUNT_FOR_RANK,
            "affected_store_count": len(stores_c),
            "affected_stores": [{"store_id": s, "name": (stores.get(s) or {}).get("store_name", s), "count": c} for s, c in stores_c.most_common(6)],
            "evidence": [{"id": r["id"], "store_id": r["store_id"], "channel": r["channel"], "timestamp": r["timestamp"], "quote": str(r["original_text"])[:220]} for r in quotes],
            "escalation_rule": escalation_rule(issue),
            "escalated_now": bool(esc) or safety, "escalation_source": ("safety policy" if safety else "emerging-issue detector" if esc else "not escalated")})
    for it in items: it["trend_arrow"] = ARROW[it["trend"]]
    key = lambda x: (not x["verified"], -x["rate_per_1000"], -x["count"])
    saf = sorted((x for x in items if x["safety"]), key=key); oth = sorted((x for x in items if not x["safety"]), key=key)
    # Safety issues are High by policy and listed first, but capped at MAX_SAFETY_SLOTS so they cannot crowd out every ordinary issue.
    # Safety issues beyond the cap are not hidden: they appear in `also_escalated_safety` and still raise incidents.
    k = min(len(saf), MAX_SAFETY_SLOTS, top_n)
    if len(oth) < top_n - k: k = min(len(saf), top_n - len(oth))          # not enough ordinary issues: give the slots back
    shown = saf[:k] + oth[:top_n - k]
    extra_safety = [{"issue_type": x["issue_type"], "label": x["label"], "count": x["count"], "affected_store_count": x["affected_store_count"], "owner": x["owner"]} for x in saf[k:]]
    return {"region": region, "period": {"start": iso(start), "end": iso(end), "days": days}, "records_in_week": cv, "records_prev_week": pv,
            "stores_reporting": len({str(r["store_id"]) for r in cur}), "stores_in_region": sum(1 for s in stores.values() if (s.get("region") or UNMAPPED) == region),
            "top_issues": shown, "also_escalated_safety": extra_safety, "omitted_issue_types": max(0, len(items) - len(shown) - len(extra_safety)),
            "basis": "Rate = reports per 1,000 de-duplicated feedback records in the region for the week; safety issues first (max 3 slots), then ordinary issues by normalised rate. "
                     "Single reports of ordinary issues are shown only if fewer than five verified issues exist and are marked unverified."}
