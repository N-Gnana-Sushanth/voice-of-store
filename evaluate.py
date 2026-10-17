#!/usr/bin/env python
"""Voice-of-Store evaluation CLI.

SYNTHETIC development run (default; generates its own data, never touches official labels):
    python evaluate.py --synthetic [--n 40000] [--seed 23] [--retrain] [--json out.json]

OFFICIAL hidden-label run (needs the organiser's files; column names are ASSUMPTIONS, override with flags):
    python evaluate.py --official --records feedback.csv --labels labels.csv --incidents planted.json \
                       [--train train_labelled.csv] [--stores stores.csv]
  records.csv : id, store_id, timestamp, channel, text
  labels.csv  : id, issue_type, severity, actionable          (hidden-label sample)
  incidents   : JSON list of {name, store_id, issue_type, incident_start, [first_report_at]}

Lead time = incident_start - first_detection (hours). POSITIVE => detected early (before the incident start).
Combined score = weighted sum, weights printed below. Synthetic numbers are NOT official / real-world performance.
"""
import argparse, csv, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from services import classifier as clf
from services.classifier import classify_batch, normalize_text
from services.dedup import cluster_duplicates, pair_metrics, DEFAULT_WINDOW, DEFAULT_THRESHOLD
from services.detector import detect_emerging, safety_flags
from services import metrics as M

def run_pipeline(records, window_hours=DEFAULT_WINDOW, threshold=DEFAULT_THRESHOLD, as_of=None):
    """records need id, store_id, timestamp, channel, text. Uses ONLY these fields (no labels)."""
    t = time.time(); pred = classify_batch([r["text"] for r in records]); t_cls = time.time() - t
    work = [{"id": r["id"], "store_id": r["store_id"], "timestamp": r["timestamp"], "channel": r["channel"], "original_text": r["text"],
             "cleaned_text": normalize_text(r["text"]), "issue_type": p["issue_type"], "severity": p["severity"], "actionable": p["actionable"]}
            for r, p in zip(records, pred)]
    t = time.time(); dd = cluster_duplicates(work, window_hours, threshold); t_dd = time.time() - t
    for w in work: w["canonical_id"] = dd["canonical"][w["id"]]
    canon = [w for w in work if w["canonical_id"] == w["id"]]
    t = time.time(); safety = safety_flags(canon); emerging = detect_emerging(canon, as_of=as_of); t_det = time.time() - t
    return {"pred": pred, "work": work, "canon": canon, "dedup": dd, "safety": safety, "emerging": emerging,
            "timing_s": {"classify": round(t_cls, 2), "dedup": round(t_dd, 2), "detect": round(t_det, 2)}}

def evaluate(records, truth_by_id, incidents, **kw):
    out = run_pipeline(records, **kw)
    ids = [r["id"] for r in records if r["id"] in truth_by_id and truth_by_id[r["id"]].get("label_issue_type") not in (None, "")]
    idx = {r["id"]: k for k, r in enumerate(records)}
    yt = lambda f: [truth_by_id[i][f] for i in ids]
    yp = lambda f: [out["pred"][idx[i]][f] for i in ids]
    res = {"n_records": len(records), "n_labelled": len(ids)}
    res["issue"] = M.classification_metrics(yt("label_issue_type"), yp("issue_type"))
    res["severity"] = M.classification_metrics(yt("label_severity"), yp("severity"))
    res["actionable"] = M.classification_metrics([int(x) for x in yt("label_actionable")], [int(x) for x in yp("actionable")], positive=1)
    esc = M.build_escalated(out["canon"], out["safety"], out["emerging"])
    truth_recs = [{**r, **truth_by_id[r["id"]]} for r in records if r["id"] in truth_by_id]
    res["escalation"] = M.escalation_metrics(esc, truth_by_id, M.proxy_truth_keys(truth_recs))
    res["detection"] = M.planted_detection(incidents, out["safety"], out["emerging"])
    planted_keys = {(i["store_id"], i["issue_type"]) for i in incidents}
    res["flags"] = {"safety_flags": len(out["safety"]), "emerging_flags": len(out["emerging"]),
                    "emerging_not_planted": sum(1 for f in out["emerging"] if (f["store_id"], f["issue_type"]) not in planted_keys)}
    res["dedup"] = {"records": len(records), "canonical": len(out["canon"]), "merged_away": len(records) - len(out["canon"])}
    if all("event_id" in truth_by_id[i] for i in ids[:50]) and ids:
        res["dedup"]["pairwise_vs_event_id"] = pair_metrics([{**{"id": r["id"]}, "event_id": truth_by_id[r["id"]]["event_id"]} for r in records if r["id"] in truth_by_id], out["dedup"]["canonical"])
    det_score = sum(d["score"] for d in incidents_rows(res)) / max(1, len(res["detection"]))
    parts = {"issue_macro_f1": res["issue"]["macro_f1"], "severity_macro_f1": res["severity"]["macro_f1"], "actionable_f1": res["actionable"]["f1"],
             "escalation_precision": res["escalation"]["precision"], "detection_lead_score": det_score}
    res["combined_score"], res["score_breakdown"] = M.combined_score(parts)
    res["timing_s"] = out["timing_s"]
    return res, out

def incidents_rows(res): return res["detection"]

def report(res, mode, model_meta=None, truth_note=""):
    bar = "=" * 78
    print(bar)
    if mode == "synthetic":
        print("SYNTHETIC DEVELOPMENT DATA -- generated by services/synthetic.py. NOT the official hidden-label evaluation.")
        print("These numbers show the code works end to end. They are NOT official or real-world performance.")
    else:
        print("OFFICIAL HIDDEN-LABEL EVALUATION (labels supplied by --labels). " + truth_note)
    print(bar)
    if model_meta: print(f"Model: {model_meta.get('model')}\n  trained on: {model_meta.get('training_source')}  (n={model_meta.get('n_train')})")
    print(f"Records: {res['n_records']:,}   labelled: {res['n_labelled']:,}")
    print("\nCLASSIFICATION (on labelled records)")
    print(f"  issue type : accuracy {res['issue']['accuracy']:.4f}   macro-F1 {res['issue']['macro_f1']:.4f}")
    print(f"  severity   : accuracy {res['severity']['accuracy']:.4f}   macro-F1 {res['severity']['macro_f1']:.4f}")
    print(f"  actionable : accuracy {res['actionable']['accuracy']:.4f}   F1(positive) {res['actionable']['f1']:.4f}")
    e = res["escalation"]
    print("\nESCALATION PRECISION  (escalated incident is TRUE when >=50% of its evidence has label_actionable=1)")
    print(f"  escalated {e['escalated_total']}   TP {e['true_positives']}   FP {e['false_positives']}   unlabelled {e['unlabelled_escalations']}   precision {e['precision']}")
    if "false_negatives" in e:
        print(f"  FN {e['false_negatives']} of {e['proxy_truth_incidents']} proxy-truth incidents   proxy recall {e['proxy_recall']}")
        print(f"  note: {e['fn_recall_basis']}")
    print("\nDETECTION OF PLANTED INCIDENTS   lead = incident_start - first_detection  (positive = early)")
    for d in res["detection"]:
        kind = "safety bypass" if d["safety"] else "statistical"
        if d["detected"]:
            print(f"  {d['name']:<28} [{kind}] start {d['incident_start'][:16]}  first report {str(d['first_report_at'])[:16]}  detected {d['first_detection'][:16]}"
                  f"\n      lead {d['lead_time_hours']:+.1f} h ({d['lead_time_hours']/24:+.2f} d)   detected {d['hours_after_first_report']:.1f} h after the first report   score {d['score']:.3f}")
        else:
            print(f"  {d['name']:<28} [{kind}] NOT DETECTED   score 0.000")
    f = res["flags"]
    print(f"  flags: {f['safety_flags']} safety, {f['emerging_flags']} statistical ({f['emerging_not_planted']} not planted)")
    dd = res["dedup"]
    print(f"\nDEDUP  {dd['records']:,} records -> {dd['canonical']:,} canonical ({dd['merged_away']:,} linked to a canonical incident; none deleted)")
    if "pairwise_vs_event_id" in dd:
        p = dd["pairwise_vs_event_id"]; print(f"  pairwise vs true event ids: precision {p['precision']}  recall {p['recall']}  (synthetic truth)")
    print("\nCOMBINED SCORE (weights are explicit; detection score: 1.0 if detected at/before start, linear to 0 at 7 days late, 0 if missed)")
    for r in res["score_breakdown"]:
        print(f"  {r['component']:<22} weight {r['weight']:.2f}  value {r['value']:.4f}  -> {r['points']:6.2f}")
    print(f"  {'TOTAL':<22} {res['combined_score']:.2f} / 100")
    print(f"\nstage timings (s): {res['timing_s']}")
    print(bar)

def load_csv(path): return list(csv.DictReader(open(path, encoding="utf-8-sig", newline="")))

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(); g.add_argument("--synthetic", action="store_true"); g.add_argument("--official", action="store_true")
    ap.add_argument("--n", type=int, default=40000); ap.add_argument("--seed", type=int, default=23)
    ap.add_argument("--detail-share", type=float, default=0.7, help="synthetic only: chance a report mentions its event's specifics (0 = duplicates share no specifics, worst case for dedup)")
    ap.add_argument("--retrain", action="store_true", help="retrain the classifier on synthetic seed 11 before evaluating")
    ap.add_argument("--records"); ap.add_argument("--labels"); ap.add_argument("--incidents"); ap.add_argument("--train")
    ap.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW); ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--json", help="write full results to this file")
    a = ap.parse_args()
    if not a.official: a.synthetic = True
    if a.synthetic:
        from services.synthetic import generate_dataset
        if a.retrain or clf.get_model() is None:
            tr = generate_dataset(seed=11, n_records=12000, plant=False)["records"]
            clf.set_model(clf.train_model([r["text"] for r in tr], [r["label_issue_type"] for r in tr], [r["label_severity"] for r in tr], [r["label_actionable"] for r in tr], source="SYNTHETIC dev data seed=11"))
        m = clf.get_model().meta
        if f"seed={a.seed}" in str(m.get("training_source")): sys.exit(f"Refusing to evaluate: model was trained on seed {a.seed} (train/test leak). Use a different --seed.")
        d = generate_dataset(seed=a.seed, n_records=a.n, detail_share=a.detail_share)
        records = [{k: r[k] for k in ("id", "store_id", "timestamp", "channel", "text")} for r in d["records"]]   # labels withheld from the pipeline
        truth = {r["id"]: r for r in d["records"]}
        res, _ = evaluate(records, truth, d["incidents"], window_hours=a.window_hours, threshold=a.threshold)
        report(res, "synthetic", m)
    else:
        if not (a.records and a.labels and a.incidents): sys.exit("--official needs --records, --labels and --incidents")
        if a.train:
            tr = load_csv(a.train)
            clf.set_model(clf.train_model([r["text"] for r in tr], [r["label_issue_type"] for r in tr], [r["label_severity"] for r in tr], [int(float(r["label_actionable"])) for r in tr], source=f"labelled file {a.train}"))
        records = load_csv(a.records); lab = {r["id"]: r for r in load_csv(a.labels)}
        truth = {i: {"label_issue_type": r["issue_type"], "label_severity": r["severity"], "label_actionable": r["actionable"]} for i, r in lab.items()}
        incidents = json.load(open(a.incidents, encoding="utf-8"))
        res, _ = evaluate(records, truth, incidents, window_hours=a.window_hours, threshold=a.threshold)
        report(res, "official", getattr(clf.get_model(), "meta", None), "Column names are assumptions; verify against the official schema.")
    if a.json: Path(a.json).write_text(json.dumps(res, indent=2, default=str), encoding="utf-8"); print("wrote", a.json)

if __name__ == "__main__":
    main()
