# Requirement status (verified vs assumed)

All measurements: **synthetic development data**, single run, 1-core Xeon @2.8 GHz, Python 3.12.3, scikit-learn 1.8.0. Not official.

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | `evaluate.py` CLI, combined score, synthetic vs official clearly separated | Done (official mode untested on real files) | Banner differs by mode; seeds 23/29/31: score 96.5 / 95.4 / 97.0; train/test leak refused (tested) |
| 2 | Statistical emerging detector, safety bypass | Done | Poisson vs 4-week baseline per 1,000 records; min-count, warm-up, no-look-ahead tests; planted incidents 2/2 statistical + 1/1 safety detected |
| 3 | TF-IDF char n-gram + Logistic Regression, full taxonomy, safety overrides | Done on synthetic data only | 31 classes; issue macro-F1 0.98 (synthetic, in-distribution); real-language support **not** claimed |
| 4 | Escalation precision (TP/FP/FN/precision/recall) | Done; recall is proxy-based | precision 0.97-0.98 on synthetic; `/api/evaluation/escalation` explains when labels are missing |
| 5 | Regional one-page brief, route, print stylesheet | Done | 5 regions rendered; 1 page each (screen styles); safety capped at 3 slots + "also escalated" line |
| 6 | Cross-channel dedup, 24-48 h, links not deletes | Done, weak accuracy | precision ≈0.36 / recall ≈0.55 on synthetic; window validated 24-48 |
| 7 | 40,000-record performance | Done | 14.2 s end to end (ingest+classify 8.1 s, dedup+detect+incidents 6.1 s), peak RSS 345 MB; `reports/perf_report.json` |
| 8 | Regression `test_original_evidence_preserved_and_update_persists` | Fixed, assertions intact | Root cause: one ordinary report never creates an incident (unverified by design) → precondition now builds a real incident |
| 9 | Integration into app/DB/routes/UI | Done | Schema migration, bulk ingest, sync pipeline, 8 routes, UI hooks (region links, detection kind, channels, escalation rule) |

## Tests deliberately changed
* `test_repeated_issue_cluster` asserted the old "2 reports ⇒ incident" rule that requirement 2 replaces. It now asserts the new contract
  (2 reports do **not** flag; a burst against a baseline does, with Poisson statistics).
* `test_original_evidence_preserved_and_update_persists`: assertions unchanged; only the fixture changed (see #8).
