# Voice of Store — FIRST SIGNAL AI (R5)

Turns multi-channel store feedback into evidence-linked, owner-routed signals and a weekly one-page brief per region.

> **Data honesty.** The official 40k dataset, taxonomy and hidden labels were **not** available while building this.
> Everything measured here uses **synthetic data written by the developer** (`services/synthetic.py`). Those numbers prove the
> code works end to end; they are **not** official or real-world performance. Hinglish / transliterated Tamil & Marathi
> phrases are rough developer approximations, never reviewed by native speakers.

## Run
```
pip install -r requirements.txt
python app.py                      # http://127.0.0.1:5000  (demo data + synthetic store master load on first launch)
```
Windows: double-click `RUN_ON_WINDOWS.bat`. Set `R5_LOAD_DEMO=0` to start empty.

## Pipeline (what the app actually does)
1. **Ingest** (`app.ingest_records`): validate → batch classify → bulk insert (originals kept, never altered or deleted).
2. **Classify** (`services/classifier.py`): TF-IDF char n-grams (2-4) + 3 Logistic Regression heads (issue over 31 classes, severity, actionable)
   + **deterministic safety overrides** (typo-tolerant regex) that force a safety issue and High severity.
3. **De-duplicate** (`services/dedup.py`): same store, *different channel*, within a configurable 24-48 h window, same issue, char-n-gram cosine ≥ 0.30.
   One report per channel per cluster; earliest = canonical; `feedback.canonical_id` links the rest. Nothing is deleted.
4. **Detect** (`services/detector.py`): per store+issue, 7-day count vs a trailing 4-week baseline, normalised per 1,000 store records, shrunk toward the
   national rate, one-sided **Poisson** test with guards (≥3 reports, p<1e-5, ≥3x ratio, expected floored at 1.0, 35-day warm-up). **Safety issues bypass all thresholds.**
5. **Incidents** (`app.sync_first_signal`): `kind` = `safety` | `emerging`, owner from taxonomy, escalation rule text, statistics, channel counts, linked evidence.
6. **Regional brief**: `/brief/<region>` (printable A4) and `/api/reports/regional`.

## Routes added
`GET /brief`, `/brief/<region>` · `/api/regions` · `/api/reports/regional[?region=&as_of=&days=]` · `POST /api/stores/import` · `GET /api/stores`
· `/api/evaluation/escalation` · `/api/dedup/summary` · `/api/taxonomy`. Existing routes unchanged; `/api/incidents` now also returns
`kind`, `stats`, `escalation_rule`, `channel_counts`, `unique_reports`, `duplicate_reports_linked`.
Import a store master (`store_id,store_name,region,city,size_tier`) to get real regions; unmapped stores appear under `Unmapped`.

## Commands
```
python -m unittest discover -s tests -t .        # 51 tests
python evaluate.py --synthetic                   # dev evaluation (held-out seed 23; refuses seeds the model trained on)
python evaluate.py --official --records R.csv --labels L.csv --incidents I.json [--train T.csv]
python train_model.py [--labelled train.csv]     # retrain (synthetic by default; use your labelled data when you have it)
python perf_test.py --n 40000                    # reproducible performance test
```

## evaluate.py
Lead time = `incident_start − first_detection` (hours); **positive = early**. Detections only count if first raised in `[onset, onset+14d]` (earlier
flags on the same store+issue get no credit). Combined score weights: issue macro-F1 .25 · severity macro-F1 .10 · actionable F1 .10 ·
escalation precision .10 · detection lead score .45 (1.0 if at/before start, linear to 0 at 7 days late, 0 if missed).
`--official` expects `records.csv (id,store_id,timestamp,channel,text)`, `labels.csv (id,issue_type,severity,actionable)`,
`incidents.json` — **column names are assumptions**; it has only been exercised on synthetic data exported in that format.

## Known limitations (read before trusting any number)
* **Classifier language coverage is unverified.** It is trained and tested on synthetic templates; real code-mixed text will be harder. Retrain on the official labelled sample.
* **Statistical detection is silent until ~5 weeks of history exist** (4-week baseline + 7-day window). Safety flags work from day one.
* **Dedup is weak.** On synthetic data: pairwise precision ≈ 0.36, recall ≈ 0.55 (≈ 0.21 / 0.25 when duplicates share no specifics). It cannot match a report in one language to a paraphrase in another. Treat the stretch goal as partially met.
* **Dedup trades sensitivity for tidiness:** a genuine multi-channel burst is counted once per cluster, which can delay statistical flags.
* **Escalation precision** uses ground-truth `label_actionable` on each escalated incident's evidence (≥50% actionable = true). Recall / FN use a *proxy* definition of an actionable incident and are indicative only. In live data labels are absent, so the endpoint says so.
* Lead-time results depend on how the synthetic generator defines `incident_start` (onset + 72 h, after a slow-rise phase). The official incidents may behave differently.
* Printing: `@media print` + `@page` rules are present; page count was verified with a renderer that used screen styles, not the print stylesheet.
