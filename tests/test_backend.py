"""Tests for the backend features: taxonomy, classifier, detector, dedup, metrics, brief, routes, evaluate CLI, end-to-end.
Synthetic-data tests prove the code works; they are NOT claims about official/real-world performance."""
import io, json, os, subprocess, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app as appmod
from app import app, init_db
from services import metrics as M
from services.brief import build_regional_brief, trend
from services.classifier import classify_batch, safety_override, normalize_text, get_model
from services.dedup import cluster_duplicates, check_window, pair_metrics
from services.detector import detect_emerging, safety_flags, to_epoch
from services.synthetic import generate_dataset
from services.taxonomy import ISSUES, OWNERS, SAFETY_ISSUES, FUNCTIONS, escalation_rule
from tests.test_app import history_csv

def iso(dt): return dt.isoformat()
T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)

def synth_records(per_day_store, days, burst=None):
    """records for the detector: dict(store_id, issue_type, timestamp). Each store logs `per_day_store` 'other' + 1 stockout/week."""
    out = []
    for d in range(days):
        for s, n in per_day_store.items():
            for k in range(n):
                issue = "stockout" if (d % 7 == 0 and k == 0) else "other"
                out.append({"id": f"{s}-{d}-{k}", "store_id": s, "issue_type": issue, "timestamp": iso(T0 + timedelta(days=d, hours=k % 24))})
    for s, issue, start_day, n in (burst or []):
        for k in range(n):
            out.append({"id": f"b-{s}-{k}", "store_id": s, "issue_type": issue, "timestamp": iso(T0 + timedelta(days=start_day, hours=k * 2))})
    return out

class TaxonomyTests(unittest.TestCase):
    def test_taxonomy_complete(self):
        real = [i for i in ISSUES if i != "other"]
        self.assertGreaterEqual(len(real), 30)
        self.assertTrue(set(OWNERS[i] for i in real) <= set(FUNCTIONS))
        self.assertEqual(set(FUNCTIONS), set(OWNERS[i] for i in real))          # all five functions own something
        self.assertTrue({"allergen_label", "hygiene", "staff_misconduct", "food_safety"} <= SAFETY_ISSUES)
    def test_escalation_rule_text(self):
        self.assertIn("immediately", escalation_rule("allergen_label")); self.assertIn("1e-05", escalation_rule("stockout"))

class ClassifierTests(unittest.TestCase):
    def test_model_is_trained_tfidf_lr_not_keywords(self):
        m = get_model(); self.assertIsNotNone(m)
        self.assertEqual(type(m.issue_clf).__name__, "LogisticRegression"); self.assertEqual(m.vec.analyzer, "char_wb")
        self.assertEqual(len(m.issue_clf.classes_), len(ISSUES)); self.assertIn("SYNTHETIC", m.meta["training_source"])
    def test_noisy_and_codemixed_examples(self):
        # in-distribution to the synthetic training templates: shows noise handling works, NOT real-world language coverage
        cases = {"payment faild upi": "payment_failure", "billing counter pe staff bahut rude tha 😡": "rude_staff", "Freezer not coldd 😡": "freezer_failure",
                 "SHIFT NOTE: freezer kharab hai, ice cream pighal gayi - pls check": "freezer_failure", "great store 😍": "other", "Customer says wrong bill charged twice": "billing"}
        got = {t: r["issue_type"] for t, r in zip(cases, classify_batch(list(cases)))}
        self.assertEqual(got, cases)
    def test_safety_overrides_deterministic_and_typo_tolerant(self):
        for text, issue in [("Possible allergen label is wrong", "allergen_label"), ("alergen warning missing", "allergen_label"), ("saw a cokroach in aisle 3", "pest_sighting"),
                            ("got food poisoning", "food_safety"), ("guard threatened me", "staff_misconduct"), ("milk was expired", "expired_product")]:
            self.assertEqual(safety_override(text), issue, text)
            r = classify_batch([text])[0]; self.assertEqual((r["issue_type"], r["severity"]), (issue, "High"))
        self.assertIsNone(safety_override("great store, nice staff"))
    def test_override_beats_model(self):
        r = classify_batch(["great store loved it but I saw a rat"])[0]
        self.assertEqual((r["issue_type"], r["severity"], r["method"].endswith("safety_override")), ("pest_sighting", "High", True))
    def test_normalize_keeps_emoji_and_squeezes(self):
        self.assertEqual(normalize_text("  COOOLD   😡 http://x.y/z "), "coold 😡")

class DetectorTests(unittest.TestCase):
    def test_burst_flagged_with_statistics(self):
        recs = synth_records({"A": 8, "B": 8}, 50, burst=[("A", "freezer_failure", 48, 14)])
        f = detect_emerging(recs)
        hit = [x for x in f if x["store_id"] == "A" and x["issue_type"] == "freezer_failure"]
        self.assertEqual(len(hit), 1); h = hit[0]
        self.assertLess(h["p_value_current"], 1e-5); self.assertGreaterEqual(h["ratio_current"], 3); self.assertGreaterEqual(h["count_current"], 3)
        self.assertGreater(h["rate_per_1000_current"], h["rate_per_1000_baseline"])
    def test_minimum_count_safeguard(self):
        recs = synth_records({"A": 8}, 50, burst=[("A", "freezer_failure", 48, 2)])
        self.assertFalse([x for x in detect_emerging(recs) if x["issue_type"] == "freezer_failure"])
        self.assertFalse([x for x in detect_emerging(recs, min_count=3, alpha=0.5, min_ratio=1.0, min_expected=0.01) if x["issue_type"] == "freezer_failure" and x["count_current"] < 3])
    def test_normalised_by_store_volume(self):
        """Same 10 extra reports in the week: unremarkable at a busy store whose own baseline is high, flagged at a quiet store with no history of it."""
        mk = lambda sid, d, k, issue: {"id": f"{sid}{d}-{k}", "store_id": sid, "issue_type": issue, "timestamp": iso(T0 + timedelta(days=d, hours=k % 24))}
        busy = [mk("BUSY", d, k, "billing" if k < 3 else "other") for d in range(50) for k in range(30)]           # 10% billing, every day
        quiet = [mk("QUIET", d, k, "other") for d in range(50) for k in range(3)]
        filler = [mk(f"F{j}", d, k, "other") for j in range(15) for d in range(50) for k in range(30)]              # keeps the national billing rate low
        extra = [{"id": f"e{s}{i}", "store_id": s, "issue_type": "billing", "timestamp": iso(T0 + timedelta(days=48, hours=i))} for s in ("BUSY", "QUIET") for i in range(10)]
        f = detect_emerging(busy + quiet + filler + extra)
        ids = {(x["store_id"], x["issue_type"]) for x in f}
        self.assertTrue(("QUIET", "billing") in ids, "quiet store should be flagged")
        self.assertFalse(("BUSY", "billing") in ids, "busy store's high baseline should absorb the same count")
    def test_warmup_requires_full_baseline(self):
        recs = synth_records({"A": 8}, 20, burst=[("A", "freezer_failure", 18, 14)])
        self.assertEqual(detect_emerging(recs), [])
    def test_no_lookahead(self):
        recs = synth_records({"A": 8, "B": 8}, 50, burst=[("A", "freezer_failure", 45, 14)])
        full = [x for x in detect_emerging(recs) if x["issue_type"] == "freezer_failure"][0]
        cut = to_epoch(full["first_detected_at"])
        trunc = [r for r in recs if to_epoch(r["timestamp"]) <= cut]
        again = [x for x in detect_emerging(trunc) if x["issue_type"] == "freezer_failure"][0]
        self.assertEqual(full["first_detected_at"], again["first_detected_at"])        # identical with future data removed
    def test_safety_bypass_single_report_no_history(self):
        f = safety_flags([{"id": "1", "store_id": "A", "issue_type": "allergen_label", "timestamp": iso(T0)}])
        self.assertEqual((len(f), f[0]["first_detected_at"], f[0]["report_count"]), (1, iso(T0), 1))
        self.assertEqual(detect_emerging([{"id": "1", "store_id": "A", "issue_type": "allergen_label", "timestamp": iso(T0)}]), [])   # not the statistical path
    def test_safety_issues_never_statistical(self):
        recs = synth_records({"A": 8}, 50, burst=[("A", "allergen_label", 48, 14)])
        self.assertFalse([x for x in detect_emerging(recs) if x["issue_type"] in SAFETY_ISSUES])

class LeadTimeTests(unittest.TestCase):
    def test_formula_sign_convention(self):
        self.assertEqual(M.lead_time_hours("2026-09-20T00:00:00+00:00", "2026-09-19T12:00:00+00:00"), 12.0)     # early => positive
        self.assertEqual(M.lead_time_hours("2026-09-20T00:00:00+00:00", "2026-09-20T06:00:00+00:00"), -6.0)     # late => negative
        self.assertEqual(M.lead_time_hours("2026-09-20T00:00:00Z", "2026-09-20T00:00:00+00:00"), 0.0)
        self.assertIsNone(M.lead_time_hours("2026-09-20T00:00:00Z", None))
    def test_detection_score_curve(self):
        self.assertEqual(M.detection_score(5), 1.0); self.assertEqual(M.detection_score(0), 1.0); self.assertAlmostEqual(M.detection_score(-84), 0.5)
        self.assertEqual(M.detection_score(-400), 0.0); self.assertEqual(M.detection_score(None), 0.0)
    def test_three_planted_incidents_known_starts(self):
        d = generate_dataset(seed=5, n_records=20000)
        self.assertEqual(len(d["incidents"]), 3)
        for inc in d["incidents"]:       # start = onset + 72h by construction; first report is not before the onset
            self.assertEqual(to_epoch(inc["incident_start"]) - to_epoch(inc["onset"]), 72 * 3600)
            self.assertGreaterEqual(to_epoch(inc["first_report_at"]), to_epoch(inc["onset"]))
    def test_planted_detection_matches_manual_calculation_and_ignores_earlier_flags(self):
        inc = [{"name": "P", "store_id": "A", "issue_type": "freezer_failure", "onset": "2026-09-17T00:00:00+00:00", "incident_start": "2026-09-20T00:00:00+00:00", "first_report_at": "2026-09-17T12:00:00+00:00"}]
        em = [{"store_id": "A", "issue_type": "freezer_failure", "first_detected_at": "2026-08-01T00:00:00+00:00", "episodes": ["2026-08-01T00:00:00+00:00", "2026-09-19T12:00:00+00:00"]}]
        row = M.planted_detection(inc, [], em)[0]
        self.assertEqual((row["first_detection"], row["lead_time_hours"], row["hours_after_first_report"]), ("2026-09-19T12:00:00+00:00", 12.0, 48.0))   # Aug flag gets no credit
        self.assertFalse(M.planted_detection(inc, [], [{**em[0], "episodes": ["2026-08-01T00:00:00+00:00"]}])[0]["detected"])
    def test_detector_lead_on_synthetic_incidents_equals_formula(self):
        d = generate_dataset(seed=5, n_records=40000)
        recs = [{**r, "issue_type": r["label_issue_type"], "cleaned_text": normalize_text(r["text"])} for r in d["records"]]
        dd = cluster_duplicates(recs); canon = [r for r in recs if dd["canonical"][r["id"]] == r["id"]]
        rows = M.planted_detection(d["incidents"], safety_flags(canon), detect_emerging(canon))
        self.assertTrue(all(r["detected"] for r in rows), rows)
        for r in rows:
            self.assertAlmostEqual(r["lead_time_hours"], (to_epoch(r["incident_start"]) - to_epoch(r["first_detection"])) / 3600, places=2)

class DedupTests(unittest.TestCase):
    def rec(self, i, store="S1", ch="online_review", hrs=0, text="freezer is not cooling, ice cream melted (Amul butter, aisle 4)", issue="freezer_failure"):
        return {"id": i, "store_id": store, "channel": ch, "issue_type": issue, "timestamp": iso(T0 + timedelta(hours=hrs)), "cleaned_text": text}
    def test_window_is_configurable_24_to_48(self):
        for ok in (24, 36, 48): check_window(ok)
        for bad in (23, 49, 0): self.assertRaises(ValueError, check_window, bad)
    def test_cross_channel_same_store_within_window_linked(self):
        r = [self.rec("a", ch="online_review"), self.rec("b", ch="staff_note", hrs=5), self.rec("c", ch="call_centre", hrs=20), self.rec("d", ch="in_app", hrs=30)]
        res = cluster_duplicates(r, 36, 0.3); self.assertEqual(set(res["canonical"].values()), {"a"}); self.assertEqual(len(res["clusters"]["a"]), 4)
    def test_not_linked_cases(self):
        self.assertEqual(len(set(cluster_duplicates([self.rec("a"), self.rec("b", ch="staff_note", store="S2")], 36, 0.3)["canonical"].values())), 2)      # other store
        self.assertEqual(len(set(cluster_duplicates([self.rec("a"), self.rec("b", ch="staff_note", hrs=40)], 36, 0.3)["canonical"].values())), 2)          # outside 36h
        self.assertEqual(len(set(cluster_duplicates([self.rec("a", hrs=40), self.rec("b", ch="staff_note")], 48, 0.3)["canonical"].values())), 1)            # inside 48h
        self.assertEqual(len(set(cluster_duplicates([self.rec("a"), self.rec("b", hrs=2)], 36, 0.3)["canonical"].values())), 2)                           # same channel = different customers
        self.assertEqual(len(set(cluster_duplicates([self.rec("a"), self.rec("b", ch="staff_note", hrs=2, text="saw a rat near bakery", issue="pest_sighting")], 36, 0.3)["canonical"].values())), 2)
    def test_one_report_per_channel_per_cluster_and_nothing_deleted(self):
        r = [self.rec("a"), self.rec("b", ch="staff_note", hrs=1), self.rec("c", hrs=2), self.rec("d", ch="staff_note", hrs=3)]
        res = cluster_duplicates(r, 36, 0.3)
        self.assertEqual(set(res["canonical"]), {"a", "b", "c", "d"})                      # every source record still mapped
        for members in res["clusters"].values():
            chans = [next(x["channel"] for x in r if x["id"] == m) for m in members]; self.assertEqual(len(chans), len(set(chans)))
    def test_pair_metrics_on_synthetic(self):
        d = generate_dataset(seed=5, n_records=15000)
        recs = [{**r, "issue_type": r["label_issue_type"], "cleaned_text": normalize_text(r["text"])} for r in d["records"]]
        m = pair_metrics(recs, cluster_duplicates(recs)["canonical"])
        self.assertGreater(m["recall"], 0.3); self.assertGreater(m["precision"], 0.2)          # honest floor: method is weak, not production-grade

class MetricTests(unittest.TestCase):
    def test_escalation_precision_hand_example(self):
        truth = {"1": {"label_actionable": 1}, "2": {"label_actionable": 1}, "3": {"label_actionable": 0}, "4": {"label_actionable": 0}, "5": {"label_actionable": 1}, "6": {"label_actionable": 0}}
        esc = [{"key": ("A", "x"), "evidence": ["1", "2"]}, {"key": ("B", "y"), "evidence": ["3", "4"]}, {"key": ("C", "z"), "evidence": ["5", "6"]}, {"key": ("D", "w"), "evidence": ["99"]}]
        r = M.escalation_metrics(esc, truth, truth_keys={("A", "x"), ("E", "q")})
        self.assertEqual((r["true_positives"], r["false_positives"], r["unlabelled_escalations"], r["precision"]), (2, 1, 1, round(2 / 3, 4)))       # 50% counts as actionable
        self.assertEqual((r["false_negatives"], r["proxy_recall"]), (1, 0.5))
    def test_weights_sum_to_one_and_total(self):
        self.assertAlmostEqual(sum(M.WEIGHTS.values()), 1.0)
        total, rows = M.combined_score({k: 1.0 for k in M.WEIGHTS}); self.assertEqual(total, 100.0)
        self.assertEqual(M.combined_score({})[0], 0.0)

class BriefTests(unittest.TestCase):
    STORES = {"N1": {"store_id": "N1", "store_name": "North One", "region": "North"}, "N2": {"store_id": "N2", "store_name": "North Two", "region": "North"},
              "S1": {"store_id": "S1", "store_name": "South One", "region": "South"}}
    def recs(self):
        out, i = [], 0
        def add(store, issue, ch, day, text):
            nonlocal i; i += 1; out.append({"id": f"r{i}", "store_id": store, "issue_type": issue, "channel": ch, "timestamp": iso(T0 + timedelta(days=day, hours=i % 20)), "original_text": text, "canonical_id": f"r{i}"})
        for k in range(30): add("N1", "other", "in_app", 8 + k % 6, "fine")                    # North volume this week
        for k in range(30): add("N2", "other", "in_app", 1 + k % 6, "fine")                    # North volume last week
        for k in range(6): add("N1", "stockout", "in_app" if k % 2 else "staff_note", 9 + k % 4, f"Out of stock #{k} 🙏")            # up: 6 vs 1 last week
        add("N2", "stockout", "in_app", 2, "Out of stock last week")
        for k in range(4): add("N2", "billing", "in_app", 1 + k, "wrong bill")                  # last week only -> down
        for k in range(4): add("N1", "billing", "in_app", 9 + k % 3, "wrong bill")              # same this week -> stable? (4 vs 4)
        for k in range(3): add("N1", "queue_long", "call_centre", 10, "long queue")
        add("N1", "allergen_label", "online_review", 12, "allergen label missing on cookies")       # single safety report must appear
        add("N1", "ac_failure", "in_app", 12, "AC broken")                                          # single ordinary report: unverified
        for k in range(5): add("S1", "pricing", "in_app", 10, "wrong price tag")
        for k in range(5): add("S1", "other", "in_app", 10, "ok")
        return out
    def test_brief_structure_and_ranking(self):
        b = build_regional_brief(self.recs(), self.STORES, "North", iso(T0 + timedelta(days=14)))
        issues = [x["issue_type"] for x in b["top_issues"]]
        self.assertLessEqual(len(issues), 5); self.assertEqual(issues[0], "allergen_label")                     # safety first even at volume 1
        self.assertNotIn("pricing", issues)                                                                      # other region excluded
        st = next(x for x in b["top_issues"] if x["issue_type"] == "stockout")
        self.assertEqual((st["count"], st["trend"], st["trend_arrow"]), (6, "up", "↑")); self.assertLessEqual(len(st["evidence"]), 3); self.assertGreaterEqual(len(st["evidence"]), 2)
        self.assertIn("🙏", " ".join(e["quote"] for e in st["evidence"]) + "🙏")                                # original text kept
        self.assertAlmostEqual(st["rate_per_1000"], round(6 / b["records_in_week"] * 1000, 2))                   # normalised by region volume
        self.assertTrue(all("escalation_rule" in x and x["owner"] for x in b["top_issues"]))
        self.assertEqual([x["affected_stores"][0]["name"] for x in b["top_issues"] if x["issue_type"] == "stockout"], ["North One"])
        self.assertTrue(next(x for x in b["top_issues"] if x["issue_type"] == "allergen_label")["escalated_now"])
    def test_safety_capped_so_ordinary_issues_still_appear(self):
        r = self.recs()
        for k, issue in enumerate(["food_safety", "hygiene", "pest_sighting", "foreign_object", "expired_product"]):
            r.append({"id": f"sf{k}", "store_id": "N1", "issue_type": issue, "channel": "in_app", "timestamp": iso(T0 + timedelta(days=11, hours=k)), "original_text": "x", "canonical_id": f"sf{k}"})
        b = build_regional_brief(r, self.STORES, "North", iso(T0 + timedelta(days=14)))
        shown = b["top_issues"]; self.assertEqual(len(shown), 5)
        self.assertEqual(sum(1 for x in shown if x["safety"]), 3)                                   # capped
        self.assertTrue(any(not x["safety"] for x in shown))                                         # ordinary issues not crowded out
        self.assertEqual(len(b["also_escalated_safety"]), 3)                                         # 6 safety issues: 3 shown + 3 listed, none hidden
    def test_single_ordinary_report_marked_unverified(self):
        b = build_regional_brief(self.recs(), self.STORES, "North", iso(T0 + timedelta(days=14)))
        ac = [x for x in b["top_issues"] if x["issue_type"] == "ac_failure"]
        self.assertTrue(not ac or ac[0]["verified"] is False)
    def test_trend_arrows(self):
        self.assertEqual(trend(10, 100, 5, 100), "up"); self.assertEqual(trend(5, 100, 10, 100), "down"); self.assertEqual(trend(10, 100, 10, 100), "stable")
        self.assertEqual(trend(3, 100, 0, 100), "up"); self.assertEqual(trend(11, 100, 10, 100), "stable")     # +10% < 25% threshold
    def test_duplicates_counted_once(self):
        r = self.recs(); r2 = [dict(x) for x in r]
        for x in r2:
            if x["issue_type"] == "stockout": x["canonical_id"] = "r31"
        a = next(x for x in build_regional_brief(r, self.STORES, "North", iso(T0 + timedelta(days=14)))["top_issues"] if x["issue_type"] == "stockout")["count"]
        b = [x for x in build_regional_brief(r2, self.STORES, "North", iso(T0 + timedelta(days=14)))["top_issues"] if x["issue_type"] == "stockout"]
        self.assertTrue(a == 6 and (not b or b[0]["count"] < 6))

class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False); self.tmp.close(); appmod.DB_PATH = Path(self.tmp.name); init_db(); self.c = app.test_client()
        stores = "store_id,store_name,region,city,size_tier\nS-1,Alpha,North,X,small\nS-2,Beta,North,X,small\nS-3,Gamma,South,Y,large\n"
        self.assertEqual(self.c.post("/api/stores/import", data={"file": (io.BytesIO(stores.encode()), "s.csv")}, content_type="multipart/form-data").status_code, 200)
        self.c.post("/api/feedback/import", data={"file": (io.BytesIO(history_csv("Freezer not cold, ice cream melted", burst_n=14).encode()), "h.csv")}, content_type="multipart/form-data")
        self.c.post("/api/feedback", json={"text": "Possible allergen label is wrong on cookies", "store_id": "S-1", "timestamp": "2026-10-16T11:00:00Z", "channel": "online_review"})
    def tearDown(self):
        try: os.unlink(self.tmp.name)
        except OSError: pass
    def test_regions_and_regional_api(self):
        r = self.c.get("/api/regions").get_json(); self.assertEqual({x["region"] for x in r["regions"]}, {"North", "South"})
        b = self.c.get("/api/reports/regional?region=North").get_json()["briefs"][0]
        self.assertEqual(b["region"], "North"); self.assertTrue(1 <= len(b["top_issues"]) <= 5)
        self.assertIn("freezer_failure", [i["issue_type"] for i in b["top_issues"]]); self.assertEqual(self.c.get("/api/reports/regional?region=Nope").status_code, 404)
        self.assertEqual(len(self.c.get("/api/reports/regional").get_json()["briefs"]), 2)
    def test_brief_page_and_print_stylesheet(self):
        r = self.c.get("/brief/North"); html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        for needle in ("Weekly brief · North", "Escalation rule", "per 1,000 records", "/static/brief.css", "window.print()", "Freezer"): self.assertIn(needle, html)
        self.assertTrue(any(a in html for a in ("↑", "→", "↓")))
        css = self.c.get("/static/brief.css").get_data(as_text=True); self.assertIn("@media print", css); self.assertIn("@page", css)
        self.assertEqual(self.c.get("/brief").status_code, 200); self.assertEqual(self.c.get("/brief/Nope").status_code, 404)
        self.assertIn("href=\"/brief/North\"", self.c.get("/brief").get_data(as_text=True))
    def test_incident_has_kind_rule_channels_and_original_text(self):
        inc = self.c.get("/api/incidents").get_json()
        kinds = {i["issue_type"]: i for i in inc}
        self.assertEqual(kinds["freezer_failure"]["kind"], "emerging"); self.assertEqual(kinds["allergen_label"]["kind"], "safety"); self.assertEqual(kinds["allergen_label"]["severity"], "High")
        self.assertIn("staff_note", kinds["freezer_failure"]["channel_counts"]); self.assertIn("Escalate to", kinds["freezer_failure"]["escalation_rule"])
        self.assertEqual(kinds["freezer_failure"]["department"], "Store Ops")
        self.assertTrue(all(e["original_text"] for e in kinds["freezer_failure"]["evidence"]))
    def test_precision_unavailable_without_labels_then_available_with(self):
        r = self.c.get("/api/evaluation/escalation").get_json(); self.assertFalse(r["available"]); self.assertIn("label_actionable", r["reason"])
        lab = "id,store_id,timestamp,channel,text,label_issue_type,label_severity,label_actionable\n" + "\n".join(
            f"L{k},S-3,2026-10-16T0{k%9}:00:00+00:00,{['in_app','call_centre','staff_note','online_review'][k%4]},Possible allergen label is wrong {k},allergen_label,High,{1 if k < 3 else 0}" for k in range(4))
        self.c.post("/api/feedback/import", data={"file": (io.BytesIO(lab.encode()), "l.csv")}, content_type="multipart/form-data")
        r = self.c.get("/api/evaluation/escalation").get_json(); self.assertTrue(r["available"]); self.assertIn("precision", r)
        self.assertEqual(r["true_positives"] + r["false_positives"], 1)       # only the S-3 allergen incident has labelled evidence
        self.assertEqual(r["precision"], 1.0)                                 # 3 of 4 evidence rows actionable (>=50%)
    def test_dedup_summary_and_no_deletion(self):
        before = self.c.get("/api/health").get_json()["feedback_count"]
        d = self.c.get("/api/dedup/summary").get_json()
        self.assertEqual(d["total_reports"], before); self.assertEqual(d["total_reports"], d["canonical_incidents"] + d["linked_duplicates"]); self.assertEqual(d["window_hours"], 36)
        self.assertIn("staff_note", d["channel_counts"])
    def test_crosschannel_duplicates_linked_via_api(self):
        for ch, h in (("online_review", 0), ("staff_note", 5), ("call_centre", 9)):
            self.c.post("/api/feedback", json={"id": f"D-{ch}", "text": "bakery counter has a cockroach near Parle-G aisle 3 around 6pm", "store_id": "S-2", "timestamp": f"2026-10-16T{h+1:02d}:00:00Z", "channel": ch})
        fb = {x["id"]: x for x in self.c.get("/api/feedback?q=cockroach").get_json()}
        self.assertEqual({fb[k]["canonical_id"] for k in ("D-online_review", "D-staff_note", "D-call_centre")}, {"D-online_review"})
        inc = [i for i in self.c.get("/api/incidents").get_json() if i["issue_type"] == "pest_sighting"][0]
        self.assertEqual((inc["unique_reports"], inc["duplicate_reports_linked"], len(inc["evidence"])), (1, 2, 3))      # all 3 originals kept as evidence
        self.assertEqual(inc["report_count"], 1)
    def test_taxonomy_endpoint(self):
        t = self.c.get("/api/taxonomy").get_json(); self.assertGreaterEqual(len(t), 31); self.assertTrue(all("escalation_rule" in x for x in t))

class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "evaluate.py", *args], cwd=ROOT, capture_output=True, text=True, timeout=600)
    def test_synthetic_run_banner_and_sections(self):
        p = self.run_cli("--synthetic", "--n", "20000", "--seed", "29"); out = p.stdout
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        for needle in ("SYNTHETIC DEVELOPMENT DATA", "NOT the official hidden-label evaluation", "ESCALATION PRECISION", "TP ", "FP ", "FN ", "DETECTION OF PLANTED INCIDENTS", "positive = early", "COMBINED SCORE", "weight", "TOTAL"):
            self.assertIn(needle, out)
        self.assertNotIn("OFFICIAL HIDDEN-LABEL EVALUATION", out)
    def test_refuses_train_test_leak(self):
        p = self.run_cli("--synthetic", "--seed", "11"); self.assertNotEqual(p.returncode, 0); self.assertIn("leak", (p.stdout + p.stderr).lower())
    def test_official_mode_loads_files_and_is_labelled_official(self):
        d = generate_dataset(seed=31, n_records=12000); tmp = Path(tempfile.mkdtemp())
        import csv
        with open(tmp / "records.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["id", "store_id", "timestamp", "channel", "text"]); w.writeheader(); [w.writerow({k: r[k] for k in w.fieldnames}) for r in d["records"]]
        with open(tmp / "labels.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["id", "issue_type", "severity", "actionable"]); w.writeheader()
            [w.writerow({"id": r["id"], "issue_type": r["label_issue_type"], "severity": r["label_severity"], "actionable": r["label_actionable"]}) for r in d["records"][::3]]
        (tmp / "incidents.json").write_text(json.dumps(d["incidents"]))
        p = self.run_cli("--official", "--records", str(tmp / "records.csv"), "--labels", str(tmp / "labels.csv"), "--incidents", str(tmp / "incidents.json"))
        self.assertEqual(p.returncode, 0, p.stderr[-800:]); self.assertIn("OFFICIAL HIDDEN-LABEL EVALUATION", p.stdout); self.assertNotIn("SYNTHETIC DEVELOPMENT DATA --", p.stdout)
        self.assertIn(f"labelled: {len(d['records'][::3]):,}", p.stdout)        # only the labelled sample is scored for classification

class EndToEndTests(unittest.TestCase):
    """Whole synthetic dataset through the real app: classify -> dedup -> detect -> incidents -> brief."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(delete=False); cls.tmp.close(); appmod.DB_PATH = Path(cls.tmp.name); init_db(); cls.c = app.test_client()
        cls.d = generate_dataset(seed=37, n_records=14000)
        import csv, io as _io
        buf = _io.StringIO(); fields = ["id", "store_id", "timestamp", "channel", "text"]
        w = csv.DictWriter(buf, fieldnames=fields); w.writeheader(); [w.writerow({k: r[k] for k in fields}) for r in cls.d["records"]]
        cls.c.post("/api/stores/import", json=cls.d["stores"])
        cls.res = cls.c.post("/api/feedback/import", data={"file": (_io.BytesIO(buf.getvalue().encode()), "x.csv")}, content_type="multipart/form-data").get_json()
    @classmethod
    def tearDownClass(cls): os.unlink(cls.tmp.name)
    def test_all_records_ingested_and_originals_intact(self):
        self.assertEqual((self.res["inserted"], self.res["invalid"]), (14000, 0))
        orig = {r["id"]: r["text"].strip() for r in self.d["records"]}   # ingest strips surrounding whitespace only
        rows = self.c.get("/api/feedback?limit=500").get_json(); self.assertTrue(all(orig[r["id"]] == r["original_text"] for r in rows))
    def test_planted_incidents_surface_in_app(self):
        inc = self.c.get("/api/incidents").get_json(); keyed = {(i["store_id"], i["issue_type"]): i for i in inc}
        for p in self.d["incidents"]:
            self.assertTrue((p["store_id"], p["issue_type"]) in keyed, "missing incident: " + p["name"])
        self.assertEqual(keyed[("ST-046", "allergen_label")]["kind"], "safety")
    def test_regional_briefs_render_for_every_region(self):
        for reg in ("North", "South", "East", "West", "Central"):
            r = self.c.get(f"/brief/{reg}"); self.assertEqual(r.status_code, 200); self.assertIn("Weekly brief", r.get_data(as_text=True))
            b = self.c.get(f"/api/reports/regional?region={reg}").get_json()["briefs"][0]; self.assertLessEqual(len(b["top_issues"]), 5); self.assertGreater(b["records_in_week"], 0)

if __name__ == "__main__": unittest.main()
