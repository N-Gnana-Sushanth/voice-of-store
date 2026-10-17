"""Issue taxonomy: ~30 operational issue types + 'other', owning function, default severity, safety flag.

DEVELOPMENT TAXONOMY. The official challenge taxonomy was not available when this was written.
To use the official one, point R5_TAXONOMY_FILE at a JSON list of
{"issue": str, "owner": str, "severity": "Low|Medium|High", "safety": bool, "label": str}.
"""
import json, os

_DEFAULT = [
    # Store Ops
    ("freezer_failure", "Store Ops", "High", False, "Freezer / cold-chain failure"),
    ("hygiene", "Store Ops", "High", True, "Store or washroom hygiene"),
    ("pest_sighting", "Store Ops", "High", True, "Pest sighting"),
    ("allergen_label", "Store Ops", "High", True, "Allergen label missing / wrong"),
    ("food_safety", "Store Ops", "High", True, "Food safety / illness"),
    ("foreign_object", "Store Ops", "High", True, "Foreign object in product"),
    ("queue_long", "Store Ops", "Medium", False, "Long billing queue"),
    ("ac_failure", "Store Ops", "Low", False, "Air conditioning failure"),
    ("parking_issue", "Store Ops", "Low", False, "Parking problem"),
    # Supply Chain
    ("stockout", "Supply Chain", "Medium", False, "Stock-out / empty shelf"),
    ("late_delivery", "Supply Chain", "Medium", False, "Late delivery"),
    ("damaged_goods", "Supply Chain", "Medium", False, "Damaged goods"),
    ("expired_product", "Supply Chain", "High", True, "Expired product on shelf"),
    ("short_shipment", "Supply Chain", "Medium", False, "Missing items in delivery"),
    ("wrong_item_delivered", "Supply Chain", "Medium", False, "Wrong item delivered"),
    # Merchandising
    ("pricing", "Merchandising", "Medium", False, "Price mismatch"),
    ("promo_not_applied", "Merchandising", "Medium", False, "Promotion not applied"),
    ("product_quality", "Merchandising", "Medium", False, "Product quality"),
    ("assortment_gap", "Merchandising", "Low", False, "Assortment gap"),
    ("planogram_misplaced", "Merchandising", "Low", False, "Planogram / misplaced products"),
    # Digital
    ("payment_failure", "Digital", "Medium", False, "Payment failure"),
    ("billing", "Digital", "Medium", False, "Billing error"),
    ("app_crash", "Digital", "Medium", False, "App crash"),
    ("otp_issue", "Digital", "Medium", False, "OTP not received"),
    ("order_tracking", "Digital", "Low", False, "Order tracking wrong"),
    ("app_login", "Digital", "Medium", False, "App login problem"),
    # HR
    ("staff_misconduct", "HR", "High", True, "Staff misconduct / harassment"),
    ("rude_staff", "HR", "Medium", False, "Rude staff"),
    ("staff_shortage", "HR", "Low", False, "Staff shortage"),
    ("staff_untrained", "HR", "Low", False, "Staff untrained"),
    # catch-all
    ("other", "Unassigned", "Low", False, "Other / no operational issue"),
]

def load_taxonomy():
    path = os.environ.get("R5_TAXONOMY_FILE")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        return [(r["issue"], r["owner"], r["severity"], bool(r["safety"]), r.get("label", r["issue"])) for r in rows]
    return list(_DEFAULT)

_ROWS = load_taxonomy()
ISSUES = [r[0] for r in _ROWS]
OWNERS = {r[0]: r[1] for r in _ROWS}
DEFAULT_SEVERITY = {r[0]: r[2] for r in _ROWS}
SAFETY_ISSUES = {r[0] for r in _ROWS if r[3]}
LABELS = {r[0]: r[4] for r in _ROWS}
FUNCTIONS = ["Store Ops", "Supply Chain", "Merchandising", "Digital", "HR"]

def owner_of(issue):
    return OWNERS.get(issue, "Unassigned")

def is_safety(issue):
    return issue in SAFETY_ISSUES

# Escalation policy parameters (kept here so the rule text and the code cannot drift apart)
POLICY = {"safety_sla_hours": 4, "emerging_sla_hours": 24, "min_count": 3, "alpha": 1e-5, "min_ratio": 3.0}

def escalation_rule(issue):
    owner = owner_of(issue)
    p = POLICY
    if is_safety(issue):
        return (f"HIGH by policy: any single report escalates immediately to {owner} "
                f"(respond within {p['safety_sla_hours']}h), regardless of volume or statistical thresholds.")
    return (f"Escalate to {owner} (respond within {p['emerging_sla_hours']}h) when a store's 7-day rate per 1,000 records is "
            f">={p['min_ratio']:g}x its trailing 4-week baseline, with >={p['min_count']} reports and Poisson p<{p['alpha']:g}. "
            f"Otherwise monitor only; a single report is unverified feedback.")
