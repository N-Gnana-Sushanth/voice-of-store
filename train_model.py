"""Train the classifier and save it to models/voice_model.joblib.

  python train_model.py                      # SYNTHETIC dev data (seed 11) -- development only
  python train_model.py --labelled train.csv # your labelled data: columns text,label_issue_type,label_severity,label_actionable
"""
import argparse, csv, time
from services.classifier import train_model, save_model

ap = argparse.ArgumentParser()
ap.add_argument("--labelled", help="CSV with text,label_issue_type,label_severity,label_actionable")
ap.add_argument("--n", type=int, default=12000, help="synthetic training size")
ap.add_argument("--seed", type=int, default=11)
a = ap.parse_args()
t = time.time()
if a.labelled:
    rows = list(csv.DictReader(open(a.labelled, encoding="utf-8-sig")))
    src = f"labelled file {a.labelled}"
    texts, iss, sev, act = [r["text"] for r in rows], [r["label_issue_type"] for r in rows], [r["label_severity"] for r in rows], [int(float(r["label_actionable"])) for r in rows]
else:
    from services.synthetic import generate_dataset
    rows = generate_dataset(seed=a.seed, n_records=a.n, plant=False)["records"]
    src = f"SYNTHETIC dev data seed={a.seed} n={a.n} (NOT official)"
    texts, iss, sev, act = [r["text"] for r in rows], [r["label_issue_type"] for r in rows], [r["label_severity"] for r in rows], [r["label_actionable"] for r in rows]
m = train_model(texts, iss, sev, act, source=src)
save_model(m)
print(f"trained on {len(texts)} rows from {src} in {time.time()-t:.1f}s; classes={m.meta['n_issue_classes']}")
