"""Emerging-issue detector (replaces the old '2 reports in 72h' rule).

For every (store, issue) series, at every as-of step (default every 6 hours):
  * current  window = last 7 days            -> count n, store record volume V_cur
  * baseline window = the 4 weeks before it  -> count c, store record volume V_base
  * normalised rates per 1,000 records: current = n/V_cur*1000, baseline = c/V_base*1000
  * baseline rate is shrunk toward the national rate for that issue (empirical-Bayes style, `prior_records`
    pseudo-records) so a store with little history is not judged against a noisy zero
  * expected = baseline_rate * V_cur;  one-sided Poisson test  P(X >= n | expected)
  * expected is floored at `min_expected` (default 1.0) so a near-zero baseline cannot make 3-4 reports look significant
  * FLAG if  n >= min_count  AND  p < alpha  AND  rate_ratio >= min_ratio
  * warm-up: nothing is flagged until `min_history_days` (35 = 4-week baseline + 7-day window) of history exist
Safety-critical issues bypass all of this: every report is an immediate safety flag.

No look-ahead: the statistic at step i only uses records with timestamp <= end of bin i.
Duplicates: pass only canonical records (dedup.py) so one incident reported on 4 channels counts once.
"""
from datetime import datetime, timezone, timedelta
import numpy as np
from scipy.stats import poisson
from services.taxonomy import SAFETY_ISSUES, POLICY

DEFAULTS = dict(bin_hours=6, window_days=7, baseline_weeks=4, min_count=POLICY["min_count"], alpha=POLICY["alpha"],
                min_ratio=POLICY["min_ratio"], prior_records=500,
                min_expected=1.0, min_history_days=35)

def to_epoch(value):
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception:
        return None

def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()

def safety_flags(records, is_safety=None):
    """Immediate flags: one entry per (store, issue) with the first report time. Bypasses every threshold."""
    groups = {}
    for r in records:
        issue = r.get("issue_type")
        if not (issue in SAFETY_ISSUES or (is_safety and is_safety(r))): continue
        ts = to_epoch(r.get("timestamp"))
        if ts is None: continue
        g = groups.setdefault((str(r.get("store_id")), issue), {"ids": [], "first": ts, "last": ts, "times": []})
        g["ids"].append(r.get("id")); g["times"].append(ts); g["first"] = min(g["first"], ts); g["last"] = max(g["last"], ts)
    return [{"kind": "safety", "store_id": s, "issue_type": i, "first_detected_at": iso(g["first"]), "latest_report_at": iso(g["last"]),
             "report_count": len(g["ids"]), "feedback_ids": g["ids"], "report_times": [iso(t) for t in sorted(g["times"])],
             "reason": "Safety-critical issue: flagged on first report, thresholds bypassed."} for (s, i), g in groups.items()]

def detect_emerging(records, as_of=None, scan_from=None, **kw):
    """Statistical emerging-issue scan. Returns list of flag dicts (one per store+issue that was ever flagged in the scan range)."""
    p = {**DEFAULTS, **kw}
    B = p["bin_hours"] * 3600; W = p["window_days"] * 24 // p["bin_hours"]; BW = p["baseline_weeks"] * 7 * 24 // p["bin_hours"]
    rows = []
    for r in records:
        ts = to_epoch(r.get("timestamp")); iss = r.get("issue_type")
        if ts is None or not r.get("store_id"): continue
        rows.append((str(r["store_id"]), iss, ts))
    if not rows: return []
    ts_all = np.array([x[2] for x in rows])
    t0 = np.floor(ts_all.min() / 86400) * 86400
    as_of_ts = to_epoch(as_of) if as_of else ts_all.max()
    nb = int((min(ts_all.max(), as_of_ts) - t0) // B) + 1
    keep = ts_all <= as_of_ts
    stores = sorted({x[0] for x in rows}); sidx = {s: k for k, s in enumerate(stores)}
    issues = sorted({x[1] for x in rows}); iidx = {s: k for k, s in enumerate(issues)}
    S, I = len(stores), len(issues)
    bi = ((ts_all - t0) // B).astype(int)
    si = np.array([sidx[x[0]] for x in rows]); ii = np.array([iidx[x[1]] for x in rows])
    m = keep & (bi < nb)
    bi, si, ii = bi[m], si[m], ii[m]
    tot = np.zeros((S, nb), dtype=np.int64); np.add.at(tot, (si, bi), 1)
    ser = np.zeros((S * I, nb), dtype=np.int32); np.add.at(ser, (si * I + ii, bi), 1)
    nat = np.zeros((I, nb), dtype=np.int64); np.add.at(nat, (ii, bi), 1)
    nat_tot = tot.sum(0)
    z = lambda a: np.concatenate([np.zeros(a.shape[:-1] + (1,), dtype=a.dtype), np.cumsum(a, axis=-1)], axis=-1)
    Ct, Cs, Cn, Cnt = z(tot), z(ser), z(nat), z(nat_tot)
    first_step = max(0, int((to_epoch(scan_from) - t0) // B)) if scan_from else 0
    # warm-up: no statistical flags until a full baseline + current window of history exists
    first_step = max(first_step, int(p["min_history_days"] * 86400 // B) - 1)
    if first_step >= nb: return []
    steps = np.arange(first_step, nb)
    hi, mid, lo = steps + 1, steps + 1 - W, np.maximum(0, steps + 1 - W - BW)
    mid = np.maximum(mid, 0)
    cur_vol_store = Ct[:, hi] - Ct[:, mid]; base_vol_store = Ct[:, mid] - Ct[:, lo]       # (S, T)
    cur_n = Cs[:, hi] - Cs[:, mid]; base_n = Cs[:, mid] - Cs[:, lo]                        # (S*I, T)
    nat_base_n = Cn[:, mid] - Cn[:, lo]; nat_base_vol = Cnt[mid] - Cnt[lo]                 # (I,T), (T,)
    nat_all_n = Cn[:, hi] - Cn[:, lo]; nat_all_vol = Cnt[hi] - Cnt[lo]
    use_base = nat_base_vol >= 200
    r_nat = np.where(use_base[None, :], nat_base_n / np.maximum(nat_base_vol, 1), nat_all_n / np.maximum(nat_all_vol, 1))   # (I,T)
    flags = {}
    cand = np.where((cur_n >= p["min_count"]).any(axis=1))[0]
    for sid in cand:
        s, i = divmod(sid, I)
        issue = issues[i]
        if issue in SAFETY_ISSUES or issue == "other": continue
        n = cur_n[sid]; vc = cur_vol_store[s].astype(float); vb = base_vol_store[s].astype(float)
        rate_b = (base_n[sid] + p["prior_records"] * r_nat[i]) / (vb + p["prior_records"])
        expected = np.maximum(rate_b * vc, p["min_expected"])   # floor: a near-zero baseline must not make 4 reports look "significant"
        ok = n >= p["min_count"]
        pv = np.ones_like(expected); pv[ok] = poisson.sf(n[ok] - 1, expected[ok])
        rate_c = np.where(vc > 0, n / np.maximum(vc, 1), 0)
        ratio = n / expected            # observed / expected-at-baseline (same as rate ratio when expected is not floored)
        hit = ok & (pv < p["alpha"]) & (ratio >= p["min_ratio"])
        if not hit.any(): continue
        t_first = int(np.argmax(hit)); t_last = int(len(hit) - 1 - np.argmax(hit[::-1]))
        starts = np.where(hit & ~np.concatenate([[False], hit[:-1]]))[0]      # beginnings of contiguous flagged runs
        k = t_first
        flags[(stores[s], issue)] = {
            "kind": "emerging", "store_id": stores[s], "issue_type": issue,
            "first_detected_at": iso(t0 + (steps[t_first] + 1) * B), "latest_flag_at": iso(t0 + (steps[t_last] + 1) * B),
            "active_now": bool(hit[-1]), "episodes": [iso(t0 + (steps[k0] + 1) * B) for k0 in starts],
            "count_current": int(n[-1]), "expected_current": round(float(expected[-1]), 3),
            "rate_per_1000_current": round(float(rate_c[-1] * 1000), 2), "rate_per_1000_baseline": round(float(rate_b[-1] * 1000), 2),
            "ratio_current": round(float(ratio[-1]), 2), "p_value_current": float(pv[-1]), "z_current": round(float((n[-1] - expected[-1]) / np.sqrt(expected[-1])), 2),
            "at_first_detection": {"count": int(n[k]), "expected": round(float(expected[k]), 3), "ratio": round(float(ratio[k]), 2),
                                   "p_value": float(pv[k]), "window_volume": int(vc[k])},
            "params": {k2: p[k2] for k2 in ("min_count", "alpha", "min_ratio", "window_days", "baseline_weeks", "bin_hours")}}
    return sorted(flags.values(), key=lambda f: f["first_detected_at"])

def detect_all(records, as_of=None, scan_from=None, **kw):
    """Combined: immediate safety flags + statistical emerging flags (records should already be de-duplicated)."""
    return {"safety": safety_flags(records), "emerging": detect_emerging(records, as_of=as_of, scan_from=scan_from, **kw)}
