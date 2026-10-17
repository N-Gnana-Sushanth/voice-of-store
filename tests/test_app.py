import io, json, os, tempfile, unittest
from app import app, init_db

from datetime import datetime, timedelta, timezone

def history_csv(burst_text=None, burst_store="S-1", burst_n=12, days=45, end="2026-10-16T12:00:00+00:00", burst_channel="staff_note"):
    """CSV text: `days` of quiet baseline traffic (3 stores x 6 records/day, no safety words) + an optional burst at one store."""
    end_dt = datetime.fromisoformat(end); rows = ["id,store_id,timestamp,channel,text"]
    chatter = ["great store, loved it", "good experience overall", "Out of stock", "nice place, will come again", "ok ok", "queue at billing counter was very long, waited 40 minutes"]
    n = 0
    for d in range(days, 0, -1):
        for s in ("S-1", "S-2", "S-3"):
            for k in range(6):
                ts = (end_dt - timedelta(days=d) + timedelta(hours=k * 3, minutes=7)).isoformat()
                n += 1; rows.append(f"H{n},{s},{ts},in_app,{chatter[(d + k) % len(chatter)]}")
    if burst_text:
        for k in range(burst_n):
            ts = (end_dt - timedelta(hours=burst_n - k)).isoformat(); n += 1
            rows.append(f"B{n},{burst_store},{ts},{burst_channel},{burst_text}")
    return "\n".join(rows)

class R5Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        import app as appmod
        appmod.DB_PATH = __import__("pathlib").Path(self.tmp.name)
        init_db()
        self.client = app.test_client()

    def tearDown(self):
        try: os.unlink(self.tmp.name)
        except OSError: pass

    def add(self, text, store="S-1", issue=None, timestamp="2026-10-16T10:00:00Z"):
        data={"text":text,"store_id":store,"timestamp":timestamp,"channel":"staff_note"}
        if issue: data["issue_type"]=issue
        return self.client.post("/api/feedback",json=data)

    def test_health_and_ui(self):
        self.assertEqual(self.client.get("/api/health").status_code,200)
        self.assertEqual(self.client.get("/").status_code,200)

    def test_mandatory_safety_signal_single_report(self):
        r=self.add("Possible allergen label is wrong on this product")
        self.assertEqual(r.status_code,201)
        incidents=self.client.get("/api/incidents").get_json()
        self.assertTrue(any(x["severity"]=="High" and x["issue_type"]=="allergen_label" for x in incidents))

    def test_repeated_issue_cluster(self):
        # Old contract (2 reports within 72h => incident) was REPLACED by the statistical detector (requirement 2).
        # New contract: two reports are below the minimum-count safeguard, so no incident ...
        self.add("Freezer not cold",timestamp="2026-10-16T08:00:00Z")
        self.add("Freezer is warm again",timestamp="2026-10-16T09:00:00Z",issue="freezer_failure")
        self.assertFalse(any(x["issue_type"]=="freezer_failure" for x in self.client.get("/api/incidents").get_json()))
        # ... while a burst against a 4-week baseline is flagged with its statistics.
        self.client.post("/api/feedback/import", data={"file": (io.BytesIO(history_csv("Freezer not cold, ice cream melted", burst_n=14).encode()), "h.csv")}, content_type="multipart/form-data")
        inc=[x for x in self.client.get("/api/incidents").get_json() if x["issue_type"]=="freezer_failure"]
        self.assertTrue(inc and inc[0]["report_count"]>=10 and inc[0]["kind"]=="emerging")
        self.assertIn("Poisson", inc[0]["why_raised"])

    def test_single_ordinary_report_is_unverified_and_creates_no_incident(self):
        """Root cause of the old IndexError: one ordinary report is unverified feedback, so no incident exists to read."""
        self.add("Customer says wrong bill charged twice")
        self.assertEqual(self.client.get("/api/incidents").get_json(), [])

    def test_original_evidence_preserved_and_update_persists(self):
        # Precondition: a real incident. A single ordinary report never creates one (see test above), so build one the
        # legitimate way: 45 days of baseline traffic, then a burst of billing complaints at one store.
        r = self.client.post("/api/feedback/import", data={"file": (io.BytesIO(history_csv("Customer says wrong bill charged twice").encode()), "h.csv")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        incident=self.client.get("/api/incidents").get_json()[0]
        updated=self.client.patch("/api/incidents/"+incident["id"],json={"status":"Under investigation","assigned_user":"Reviewer A","note":"Checking receipt"})
        self.assertEqual(updated.status_code,200)
        feedback=self.client.get("/api/feedback").get_json()
        self.assertTrue(any("wrong bill" in x["original_text"] for x in feedback))
        self.assertTrue(self.client.get("/api/incidents").get_json()[0]["audit"])

    def test_bad_feedback_rejected(self):
        self.assertEqual(self.client.post("/api/feedback",json={"text":"hello"}).status_code,400)

    def test_weekly_report_schema(self):
        self.add("Out of stock")
        report=self.client.get("/api/reports/weekly").get_json()
        self.assertIn("top_issues",report)
        self.assertIn("basis",report)

if __name__ == "__main__": unittest.main()
