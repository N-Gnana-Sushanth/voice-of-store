"""Issue / severity / actionability classifier.

Primary model : TF-IDF character n-grams (char_wb, 2-4) + three Logistic Regression heads
                (issue type over the full taxonomy, severity, actionable).
Safety layer  : deterministic keyword/regex overrides that force a safety issue + High severity.
Fallback      : the original keyword rules, used only if no trained model can be loaded.

Language note : the model only knows what its training data contained. The bundled model is trained on SYNTHETIC
developer-written text (English, Hinglish, and rough transliterated Tamil/Marathi). Support for real-world
code-mixed text is UNVERIFIED until it is retrained and evaluated on the official labelled data.
"""
import json, os, re, threading
from pathlib import Path
from services.taxonomy import ISSUES, SAFETY_ISSUES, DEFAULT_SEVERITY

BASE = Path(__file__).resolve().parent.parent
MODEL_PATH = Path(os.environ.get("R5_MODEL_PATH", BASE / "models" / "voice_model.joblib"))
META_PATH = MODEL_PATH.with_suffix(".meta.json")

# ---- deterministic safety overrides: (issue, regex).  Typo-tolerant where cheap. -----------------------------
SAFETY_PATTERNS = [
    ("allergen_label", r"\ba+l+[ea]r+g\w*|\bnut[- ]?free\b|\bgluten\b"),
    ("food_safety", r"food\s*p[o0]is\w*|\bfood\s*safety\b|\bsalmonella\b|\bvomit\w*|\bstomach\s*(ache|illness|infection)|\bfell\s+sick\b|\brotten\b|\bspoiled\b|\bbadbu\b|\bulti\b|\bbimaar\b"),
    ("foreign_object", r"\bfound (a |some )?(piece|hair|glass|plastic|metal|insect|worm)|\bglass\b.*\b(food|pack|bread|rice)|\bkaanch\b|\bplastic (ka|cha|piece)\b|\bmetal piece\b|\bhair (in|and)\b"),
    ("pest_sighting", r"\bco?c?k?r?o+a?ch\w*|\bcokroach\w*|\brats?\b|\bchuh[ae]\w*|\bundir\b|\binsects?\b|\bpests?\b|\bkeed[ae]\b|\bpoochi\b|\bmice\b|\bmouse\b"),
    ("expired_product", r"\bexpir\w*|\bstale\b|\bbest before\b|\bpast (its )?date\b"),
    ("staff_misconduct", r"\bharass\w*|\bthreat\w*|\babus(e|ive)\b|\bmisbehav\w*|\bdhamki\b|\bgaali\b|\bmolest\w*|\bdiscriminat\w*|\bshivigal\b|\bmirati\b"),
    ("hygiene", r"\bunhygien\w*|\bfilth\w*|\bgandagi\b|\bunclean\b|\bdirty (floor|washroom|toilet)"),
]
_COMPILED = [(i, re.compile(p, re.I)) for i, p in SAFETY_PATTERNS]
_URL = re.compile(r"https?://\S+")
_REPEAT = re.compile(r"(.)\1{2,}")
_WS = re.compile(r"\s+")

def normalize_text(text):
    """Lowercase, drop URLs, squeeze letter elongation ('coooold'->'coold'), collapse whitespace. Emojis are kept."""
    t = str(text or "").lower()
    t = _URL.sub(" ", t)
    t = _REPEAT.sub(r"\1\1", t)
    return _WS.sub(" ", t).strip()

def safety_override(text):
    """Return the safety issue matched by a deterministic rule, or None."""
    t = str(text or "")
    for issue, rx in _COMPILED:
        if rx.search(t):
            return issue
    return None

# ---- legacy keyword baseline (fallback only) -----------------------------------------------------------------
RULES = [
    ("freezer_failure", ["freezer", "fridge not cold", "refrigerator", "cold storage"]),
    ("payment_failure", ["payment failed", "upi failed", "card declined", "payment error"]),
    ("billing", ["overcharged", "wrong bill", "billing error", "charged twice"]),
    ("stockout", ["out of stock", "stockout", "not available", "empty shelf"]),
    ("pricing", ["wrong price", "price mismatch", "discount missing", "price tag"]),
]
CRITICAL = SAFETY_ISSUES

def _keyword_classify(cleaned):
    for issue, terms in RULES:
        if any(t in cleaned for t in terms):
            return {"issue_type": issue, "severity": DEFAULT_SEVERITY.get(issue, "Medium"), "actionable": True, "confidence": 0.5, "method": "keyword_fallback"}
    return {"issue_type": "other", "severity": "Low", "actionable": False, "confidence": 0.3, "method": "keyword_fallback"}

# ---- trainable model -----------------------------------------------------------------------------------------
class VoiceModel:
    def __init__(self, vec, issue_clf, sev_clf, act_clf, meta):
        self.vec, self.issue_clf, self.sev_clf, self.act_clf, self.meta = vec, issue_clf, sev_clf, act_clf, meta

    def predict(self, cleaned_texts):
        X = self.vec.transform(cleaned_texts)
        pi = self.issue_clf.predict_proba(X)
        issues = self.issue_clf.classes_[pi.argmax(1)]
        conf = pi.max(1)
        sev = self.sev_clf.predict(X)
        pa = self.act_clf.predict_proba(X)
        act_idx = list(self.act_clf.classes_).index(1) if 1 in self.act_clf.classes_ else 0
        return issues, conf, sev, pa[:, act_idx]

def train_model(texts, issues, severities, actionable, source="unspecified", max_features=40000, C=10.0, seed=0):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    import sklearn, datetime
    cleaned = [normalize_text(t) for t in texts]
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=2, max_features=max_features, sublinear_tf=True, lowercase=False)
    X = vec.fit_transform(cleaned)
    issue_clf = LogisticRegression(C=C, max_iter=300, random_state=seed).fit(X, list(issues))
    sev_clf = LogisticRegression(C=C, max_iter=300, random_state=seed).fit(X, list(severities))
    act_clf = LogisticRegression(C=C, max_iter=300, random_state=seed).fit(X, [int(a) for a in actionable])
    meta = {"model": "tfidf_char_wb_2-4 + LogisticRegression x3 (issue, severity, actionable)", "training_source": source,
            "n_train": len(cleaned), "n_issue_classes": len(set(issues)), "sklearn": sklearn.__version__,
            "trained_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "language_claim": "None validated. Trained on the source named above; see module docstring."}
    return VoiceModel(vec, issue_clf, sev_clf, act_clf, meta)

def save_model(model, path=None):
    import joblib
    path = Path(path or MODEL_PATH); path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, path, compress=3)
    path.with_suffix(".meta.json").write_text(json.dumps(model.meta, indent=2), encoding="utf-8")

_MODEL, _LOCK, _TRIED = None, threading.Lock(), False

def get_model(autotrain=True):
    """Load the bundled model; if missing/incompatible, retrain once on synthetic dev data (clearly labelled)."""
    global _MODEL, _TRIED
    if os.environ.get("R5_DISABLE_ML") == "1": return None
    if _MODEL is not None or _TRIED: return _MODEL
    with _LOCK:
        if _MODEL is not None or _TRIED: return _MODEL
        _TRIED = True
        try:
            import joblib
            if MODEL_PATH.exists():
                _MODEL = joblib.load(MODEL_PATH); return _MODEL
        except Exception as exc:
            print(f"[classifier] could not load {MODEL_PATH.name}: {exc}")
        if autotrain:
            try:
                from services.synthetic import generate_dataset
                d = generate_dataset(seed=11, n_records=12000, plant=False)["records"]
                _MODEL = train_model([r["text"] for r in d], [r["label_issue_type"] for r in d], [r["label_severity"] for r in d],
                                     [r["label_actionable"] for r in d], source="SYNTHETIC dev data seed=11 (auto-trained)")
                save_model(_MODEL)
            except Exception as exc:
                print(f"[classifier] auto-train failed ({exc}); using keyword fallback")
        return _MODEL

def set_model(model):
    global _MODEL, _TRIED
    _MODEL, _TRIED = model, True

def classify_batch(texts):
    """Classify a list of texts. Returns list of dicts: issue_type, severity, actionable, confidence, method, override."""
    texts = [str(t or "") for t in texts]
    cleaned = [normalize_text(t) for t in texts]
    model = get_model()
    out = []
    if model is None:
        for c in cleaned: out.append(_keyword_classify(c))
    else:
        issues, conf, sev, pact = model.predict(cleaned)
        for k in range(len(texts)):
            out.append({"issue_type": str(issues[k]), "severity": str(sev[k]), "actionable": bool(pact[k] >= 0.5),
                        "confidence": round(float(conf[k]), 4), "method": "tfidf_char_lr"})
    for k, t in enumerate(texts):
        ov = safety_override(t)
        r = out[k]
        r["override"] = ov
        if ov:                                    # deterministic safety policy wins over the model
            r.update(issue_type=ov, severity="High", actionable=True, method=r["method"] + "+safety_override")
        elif r["issue_type"] in SAFETY_ISSUES:    # model-predicted safety issue is High by policy
            r["severity"] = "High"
    return out

def classify_feedback(text):
    return classify_batch([text])[0]

def is_mandatory_escalation(issue_type, text):
    """Safety trigger: an issue in the safety set, or a deterministic safety keyword match."""
    return issue_type in SAFETY_ISSUES or safety_override(text) is not None
