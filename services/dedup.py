"""Cross-channel de-duplication.

Idea: the same incident often reaches head office through several channels (review + app + staff note + ticket).
Two reports are *likely duplicates* when they are
  - from the same store,
  - on DIFFERENT channels,
  - within `window_hours` (configurable 24-48h) of each other,
  - classified as the same issue type, and
  - textually similar: cosine similarity of TF-IDF character n-grams >= `threshold`.
A cluster holds at most ONE report per channel, so many different customers complaining on the same channel are
never merged (that volume is real signal). The earliest report is the canonical one. Nothing is deleted: callers keep
every source row and just store `canonical_id`.

Known limit: character n-gram similarity cannot match a report in one language/script with a paraphrase in another
(e.g. English staff note vs Hinglish ticket) unless they share words. Those pairs are missed (recall < 1).
"""
from collections import defaultdict
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from services.detector import to_epoch

MIN_WINDOW, MAX_WINDOW = 24, 48
DEFAULT_WINDOW, DEFAULT_THRESHOLD = 36, 0.30

def check_window(window_hours):
    if not (MIN_WINDOW <= float(window_hours) <= MAX_WINDOW):
        raise ValueError(f"window_hours must be between {MIN_WINDOW} and {MAX_WINDOW}, got {window_hours}")
    return float(window_hours)

def cluster_duplicates(records, window_hours=DEFAULT_WINDOW, threshold=DEFAULT_THRESHOLD, vectorizer=None):
    """records: dicts with id, store_id, issue_type, channel, timestamp, cleaned_text (or original_text/text).
    Returns {"canonical": {id: canonical_id}, "pairs": [(a, b, sim)], "clusters": {canonical_id: [ids]}}"""
    window = check_window(window_hours) * 3600
    texts = [str(r.get("cleaned_text") or r.get("original_text") or r.get("text") or "") for r in records]
    if vectorizer is None:
        vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 4), min_df=2, max_features=60000, sublinear_tf=True)
    try:
        X = vectorizer.fit_transform(texts)
    except ValueError:                      # empty vocabulary (e.g. tiny input)
        vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 4), sublinear_tf=True)
        X = vectorizer.fit_transform(texts)
    ts = np.array([to_epoch(r.get("timestamp")) or 0.0 for r in records])
    groups = defaultdict(list)
    for k, r in enumerate(records):
        groups[(str(r.get("store_id")), r.get("issue_type"))].append(k)
    cand = []
    for idxs in groups.values():
        if len(idxs) < 2: continue
        idxs = sorted(idxs, key=lambda k: ts[k])
        for chunk_start in range(0, len(idxs), 1500):             # bound the O(n^2) block size
            blk = idxs[max(0, chunk_start - 200): chunk_start + 1500]
            S = (X[blk] @ X[blk].T).tocoo()
            for a, b, v in zip(S.row, S.col, S.data):
                if a >= b or v < threshold: continue
                ka, kb = blk[a], blk[b]
                if records[ka].get("channel") == records[kb].get("channel"): continue
                if abs(ts[ka] - ts[kb]) > window: continue
                cand.append((float(v), ka, kb))
    cand.sort(reverse=True)
    parent = list(range(len(records))); chans = [{records[k].get("channel")} for k in range(len(records))]
    def find(x):
        while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
        return x
    pairs = []
    for v, ka, kb in cand:
        ra, rb = find(ka), find(kb)
        if ra == rb or (chans[ra] & chans[rb]): continue            # at most one report per channel per cluster
        parent[rb] = ra; chans[ra] |= chans[rb]; pairs.append((records[ka]["id"], records[kb]["id"], round(v, 3)))
    members = defaultdict(list)
    for k in range(len(records)): members[find(k)].append(k)
    canonical, clusters = {}, {}
    for ks in members.values():
        ks.sort(key=lambda k: (ts[k], str(records[k]["id"])))
        cid = records[ks[0]]["id"]
        clusters[cid] = [records[k]["id"] for k in ks]
        for k in ks: canonical[records[k]["id"]] = cid
    return {"canonical": canonical, "pairs": pairs, "clusters": clusters}

def pair_metrics(records, canonical, truth_key="event_id"):
    """Pairwise precision/recall of 'same cluster' against ground-truth event ids (synthetic data only)."""
    from collections import Counter
    def n_pairs(c): return sum(v * (v - 1) // 2 for v in c.values())
    pred = Counter(canonical[r["id"]] for r in records)
    truth = Counter(r[truth_key] for r in records)
    both = Counter((canonical[r["id"]], r[truth_key]) for r in records)
    tp, pp, tt = n_pairs(both), n_pairs(pred), n_pairs(truth)
    return {"pairs_predicted": pp, "pairs_true": tt, "true_positive_pairs": tp,
            "precision": round(tp / pp, 4) if pp else None, "recall": round(tp / tt, 4) if tt else None}
