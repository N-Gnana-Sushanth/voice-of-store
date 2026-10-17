"""SYNTHETIC DEVELOPMENT DATA GENERATOR.

Everything produced here is invented by the developer. Hinglish / transliterated Tamil / Marathi phrases are
rough developer-written approximations that have NOT been reviewed by native speakers. Metrics computed on this
data say whether the code works end to end; they are NOT official or real-world performance numbers.
"""
import random
from datetime import datetime, timedelta, timezone
from services.taxonomy import ISSUES, DEFAULT_SEVERITY, SAFETY_ISSUES

# issue -> {lang: [phrases]}
P = {
"freezer_failure": {"en": ["freezer is not cooling, ice cream melted", "the freezer section is warm and frozen items are soft", "fridge not cold in the dairy aisle", "freezer temperature alarm sounding"],
  "hi": ["freezer kharab hai, ice cream pighal gayi", "fridge thanda nahi kar raha"], "ta": ["freezer work aagala, ice cream urugiduchu"], "mr": ["freezer band aahe, ice cream vitagli"]},
"hygiene": {"en": ["store floor is dirty and unhygienic", "washroom is filthy and smells bad", "dirty trolleys and sticky counters everywhere"],
  "hi": ["store bahut ganda hai, safai nahi hai", "washroom me bahut gandagi hai"], "ta": ["kadai romba azhukku, suthama illa"], "mr": ["dukan khup ghaan aahe, swachata nahi"]},
"pest_sighting": {"en": ["saw a cockroach near the snacks aisle", "rat running near the grain shelves", "insects crawling on the bakery items"],
  "hi": ["cockroach dikha bakery ke paas", "chuha dekha anaaj wali shelf pe"], "ta": ["poochi iruku bakery pakkathula"], "mr": ["undir disla dhanya shelf javal"]},
"allergen_label": {"en": ["allergen label missing on the peanut cookies", "packet says nut free but ingredients list nuts", "wrong allergy warning on the chocolate pack"],
  "hi": ["allergen label nahi laga hai cookies pe", "packet pe nut free likha hai par nuts hai"], "ta": ["allergen label illa cookies la"], "mr": ["allergen label nahi cookies var"]},
"food_safety": {"en": ["got food poisoning after eating the packed sandwich", "chicken smelled rotten and looked spoiled", "kids fell sick after the cooked meal"],
  "hi": ["sandwich khane ke baad food poisoning ho gaya", "chicken se badbu aa rahi thi, kharab tha"], "ta": ["sandwich saapta apram vayiru valikuthu food poisoning"], "mr": ["sandwich khalyavar food poisoning jhala"]},
"foreign_object": {"en": ["found a piece of plastic in the bread", "there was a hair and some glass in the packaged food", "metal piece in my rice packet"],
  "hi": ["bread me plastic ka tukda mila", "chawal ke packet me kaanch mila"], "ta": ["bread la plastic thundu irundhuchu"], "mr": ["bread madhe plastic cha tukda sapadla"]},
"queue_long": {"en": ["queue at billing counter was very long, waited 40 minutes", "only one counter open and huge line"],
  "hi": ["billing counter pe bahut lambi line thi", "sirf ek counter khula tha, bahut wait karna pada"], "ta": ["billing la romba neram queue"], "mr": ["billing counter var khup lamb rang hoti"]},
"ac_failure": {"en": ["store is very hot, air conditioning not working", "AC broken and it feels stuffy inside"],
  "hi": ["AC kharab hai, store me bahut garmi hai"], "ta": ["AC work aagala romba veppam"], "mr": ["AC band aahe khup garam hote"]},
"parking_issue": {"en": ["no parking space and the parking lot is blocked", "parking attendant charging extra fees"],
  "hi": ["parking nahi mili, parking me jam tha"], "ta": ["parking idam illa"], "mr": ["parking la jaga nahi"]},
"stockout": {"en": ["item out of stock again, empty shelf", "the milk shelf is empty, nothing available", "out of stock"],
  "hi": ["saaman out of stock hai, shelf khali hai", "doodh available nahi hai"], "ta": ["stock illa, shelf kaali"], "mr": ["stock sampla aahe, shelf rikami"]},
"late_delivery": {"en": ["home delivery arrived 3 hours late", "my order was delayed and delivery never came on time"],
  "hi": ["delivery 3 ghante late aayi", "order der se pahuncha"], "ta": ["delivery romba late"], "mr": ["delivery khup late aali"]},
"damaged_goods": {"en": ["received damaged packaging, box was crushed", "the bottles were broken and leaking in the bag"],
  "hi": ["saaman toota hua mila, packing kharab thi"], "ta": ["packing udanjiruchu"], "mr": ["saaman futlele hote"]},
"expired_product": {"en": ["bought milk that was already expired", "expiry date has passed on the yogurt cups", "stale expired biscuits on the shelf"],
  "hi": ["expired doodh bech diya", "dahi ki expiry date nikal chuki hai"], "ta": ["expiry date mudinjiruchu paal la"], "mr": ["doodh expire jhala hota"]},
"short_shipment": {"en": ["delivery had missing items, one item short", "two items from my order were not in the package"],
  "hi": ["delivery me saaman kam tha, do items missing"], "ta": ["order la item missing"], "mr": ["order madhe saaman kami hota"]},
"wrong_item_delivered": {"en": ["got the wrong item delivered, ordered rice got wheat", "wrong product sent in my order"],
  "hi": ["galat item bhej diya, chawal ki jagah gehu"], "ta": ["thappana item anuppitanga"], "mr": ["chukicha item pathavla"]},
"pricing": {"en": ["shelf price and billing price do not match", "wrong price tag on the detergent"],
  "hi": ["shelf ka price aur bill ka price alag hai", "price tag galat laga hai"], "ta": ["price tag thappa irukku"], "mr": ["price tag chukicha aahe"]},
"promo_not_applied": {"en": ["discount offer not applied at checkout", "promo coupon did not work, offer missing from bill"],
  "hi": ["discount bill me lagaya nahi", "offer coupon kaam nahi kiya"], "ta": ["discount apply aagala"], "mr": ["discount lagla nahi bill var"]},
"product_quality": {"en": ["vegetables were rotten and low quality", "product quality has become very poor lately"],
  "hi": ["sabzi sadi hui thi, quality kharab hai"], "ta": ["quality romba mosam"], "mr": ["quality kharab aahe bhaji sadli hoti"]},
"assortment_gap": {"en": ["you never stock my favourite brand anymore", "limited variety, many products missing from range"],
  "hi": ["meri pasand ka brand ab nahi rakhte", "variety bahut kam hai"], "ta": ["en brand illa"], "mr": ["avadta brand nahi thevat"]},
"planogram_misplaced": {"en": ["products are in the wrong aisle, hard to find items", "shelf labels mismatch with products placed"],
  "hi": ["saaman galat aisle me rakha hai, dhoondhna mushkil"], "ta": ["porutkal thappana idathula"], "mr": ["saaman chukichya aisle madhe"]},
"payment_failure": {"en": ["payment failed but money got deducted", "UPI transaction failed at the counter", "card declined twice at checkout"],
  "hi": ["payment fail ho gaya par paise kat gaye", "UPI fail ho raha hai counter pe"], "ta": ["payment fail aachu panam pochu"], "mr": ["payment fail jhala paise kaple"]},
"billing": {"en": ["customer says wrong bill charged twice", "I was overcharged and the bill has an extra item", "billing error, charged twice for same product"],
  "hi": ["galat bill bana diya, do baar charge kiya", "bill me extra item laga diya"], "ta": ["bill la thappa charge pannitanga"], "mr": ["bill chukicha banavla donda charge kela"]},
"app_crash": {"en": ["app keeps crashing when I open the cart", "the app freezes and closes on checkout"],
  "hi": ["app baar baar crash ho raha hai"], "ta": ["app crash aaguthu"], "mr": ["app crash hotoy"]},
"otp_issue": {"en": ["OTP not received, can't login", "never get the verification code on my phone"],
  "hi": ["OTP nahi aa raha, login nahi ho raha"], "ta": ["OTP varala"], "mr": ["OTP yet nahi"]},
"order_tracking": {"en": ["order tracking shows wrong status", "cannot track my order in the app, stuck on packed"],
  "hi": ["order tracking galat status dikha raha hai"], "ta": ["order tracking sariyaa illa"], "mr": ["order tracking chukicha dakhavte"]},
"app_login": {"en": ["unable to login to the app, password reset fails", "login screen shows error every time"],
  "hi": ["app me login nahi ho raha, password reset fail"], "ta": ["login aagala"], "mr": ["login hot nahi"]},
"staff_misconduct": {"en": ["staff member harassed a female customer", "security guard threatened me and was abusive", "cashier misbehaved and used abusive language"],
  "hi": ["staff ne mahila customer ko pareshan kiya", "guard ne dhamki di aur gaali di"], "ta": ["staff thappa pesunanga, mirati"], "mr": ["staff ne shivigal keli ani dhamki dili"]},
"rude_staff": {"en": ["billing staff was rude and ignored me", "the counter person behaved rudely with attitude"],
  "hi": ["billing wale ka behaviour rude tha", "staff ne dhang se baat nahi ki"], "ta": ["staff romba rude ah pesinanga"], "mr": ["staff ne uddhat vagnuk keli"]},
"staff_shortage": {"en": ["no staff available to help in the aisles", "understaffed, nobody at the service desk"],
  "hi": ["help karne ke liye staff nahi tha"], "ta": ["staff yaarum illa"], "mr": ["madat karayla staff nahi"]},
"staff_untrained": {"en": ["staff did not know product details or return policy", "employee could not operate the billing machine properly"],
  "hi": ["staff ko product ki jaankari nahi thi"], "ta": ["staff ku theriyala"], "mr": ["staff la mahiti navhti"]},
}
# non-actionable chatter, labelled 'other'
CHATTER = {"en": ["great store, loved it", "good experience overall", "nice place, will come again", "worst experience ever", "never coming back", "waste of time", "ok ok", "average"],
           "hi": ["bahut accha store hai", "bekaar experience", "theek hai"], "ta": ["nalla kadai"], "mr": ["chan dukan aahe"]}
LANG_W = [("en", .55), ("hi", .28), ("ta", .09), ("mr", .08)]
CHANNELS = ["online_review", "in_app", "staff_note", "call_centre"]
WRAP = {
 "online_review": ["Visited on Sunday. {x}. Would not recommend.", "{x} Disappointed.", "2/5 stars - {x}", "{x}"],
 "in_app": ["★★ {x}", "{x}", "Rating 1: {x}", "{x} pls fix"],
 "staff_note": ["SHIFT NOTE: {x} - pls check", "{x}. informed manager.", "Customer complaint noted: {x}", "{x}"],
 "call_centre": ["Caller reports {x}. Ticket raised.", "Agent notes: customer said {x}", "CALLER: {x} AGENT: noted, escalate", "{x}"],
}
DETAIL = ["", "", "", " near the entrance", " since morning", " again today", " - very bad", " yesterday evening", " at the weekend"]
PRODUCTS = ["Amul butter", "Maggi noodles", "Parle-G", "Tata salt", "Aashirvaad atta", "Britannia bread", "Surf Excel", "Dairy Milk", "Kurkure", "Fortune oil",
            "Haldiram bhujia", "Red Label tea", "Dettol soap", "Colgate paste", "Mother Dairy curd", "Lays chips", "Sunfeast biscuits", "Nescafe", "Patanjali ghee", "Kissan jam",
            "Real juice", "Bru coffee", "Harpic", "Vim bar", "Saffola oil", "MDH masala", "Everest chilli", "Pepsi", "Coca-Cola", "Frooti"]
TOD = ["around 6pm", "this morning", "at lunch time", "around 8pm", "early morning", "after 4pm", "on Saturday", "on Sunday"]
EMOJI = ["😡", "😤", "👎", "🙏", "😷", "🤢", "❗", "😞"]
REGIONS = ["North", "South", "East", "West", "Central"]
ISSUE_LIST = [i for i in ISSUES if i != "other"]

def _typo(word, rng):
    if len(word) < 4: return word
    k = rng.randrange(1, len(word) - 1); m = rng.random()
    if m < .34: return word[:k] + word[k+1:]
    if m < .67: return word[:k] + word[k] + word[k:]
    return word[:k] + word[k+1] + word[k] + word[k+2:]

def _noise(text, rng, typo=.25, emoji=.15, trunc=.04, case=.10):
    w = text.split()
    if rng.random() < typo and w:
        for _ in range(rng.choice([1, 1, 2])):
            j = rng.randrange(len(w)); w[j] = _typo(w[j], rng)
    t = " ".join(w)
    r = rng.random()
    if r < case / 2: t = t.upper()
    elif r < case: t = t.lower()
    if rng.random() < emoji: t += " " + rng.choice(EMOJI)
    if rng.random() < trunc and len(t) > 25: t = t[: rng.randrange(18, len(t) - 3)]
    return t

def _pick_lang(rng):
    r = rng.random(); acc = 0
    for l, w in LANG_W:
        acc += w
        if r < acc: return l
    return "en"

def make_text(issue, channel, rng, lang=None, det=None):
    lang = lang or _pick_lang(rng)
    if issue == "other":
        base = rng.choice(CHATTER[lang])
    else:
        bank = P[issue].get(lang) or P[issue]["en"]
        base = rng.choice(bank) + rng.choice(DETAIL)
        if det: base += f" ({det[0]}, aisle {det[1]}, {det[2]})"
    return _noise(rng.choice(WRAP[channel]).format(x=base), rng), lang

def make_stores(n_stores, seed):
    rng = random.Random(seed * 7 + 1)
    out = []
    for i in range(1, n_stores + 1):
        tier = rng.choices(["small", "medium", "large"], [.4, .4, .2])[0]
        out.append({"store_id": f"ST-{i:03d}", "store_name": f"Demo Store {i:03d}", "region": REGIONS[(i - 1) % 5],
                    "city": f"City-{(i - 1) % 17 + 1}", "size_tier": tier,
                    "volume_mult": {"small": .5, "medium": 1.0, "large": 2.2}[tier]})
    return out

def _sev(issue, rng):
    base = DEFAULT_SEVERITY[issue]
    if issue in SAFETY_ISSUES: return "High"
    order = ["Low", "Medium", "High"]; i = order.index(base)
    r = rng.random()
    if r < .12: i = min(2, i + 1)
    elif r < .22: i = max(0, i - 1)
    return order[i]

def _label(issue, rng, flip=.03):
    if issue == "other":
        return "Low", False
    act = rng.random() > .08            # ~8% genuine-issue records labelled non-actionable (vague / unverifiable)
    if rng.random() < flip: act = not act   # label noise
    return _sev(issue, rng), act

PLANTED_TEMPLATE = [  # (name, issue, store_idx, onset_day_offset_from_end, kind)
    ("PLANT-1 freezer failure", "freezer_failure", 7, 24),
    ("PLANT-2 payment failures", "payment_failure", 31, 17),
    ("PLANT-3 allergen mislabel", "allergen_label", 45, 10),
]

def generate_dataset(seed=23, n_records=40000, n_stores=60, days=84, plant=True, start=None, detail_share=0.7):
    """Return dict(records, stores, incidents). records carry label_* (ground truth) + event_id (dedup truth)."""
    rng = random.Random(seed)
    start = start or datetime(2026, 7, 19, tzinfo=timezone.utc)
    stores = make_stores(n_stores, 5)       # store master is the same across seeds
    mult = [s["volume_mult"] for s in stores]
    # issue propensity: Zipf-ish, safety issues rare; each store has 2 chronic issues (3x)
    wrng = random.Random(99)
    base_w = {i: (0.25 if i in SAFETY_ISSUES else 1.0) / (1 + wrng.random() * 4) for i in ISSUE_LIST}
    planted_issues = {p[1] for p in PLANTED_TEMPLATE}
    store_w = []
    for k in range(n_stores):
        w = dict(base_w)
        for i in wrng.sample([x for x in ISSUE_LIST if x not in planted_issues and x not in SAFETY_ISSUES], 2): w[i] *= 3
        store_w.append(w)
    records, incidents = [], []
    uid = [0]
    ev_det = {}
    def add(store, ts, issue, channel, event_id, incident_id=None, lang=None):
        # concrete details (product/aisle/time) belong to the EVENT, so duplicate reports of one event can share them.
        # detail_share = probability that a given report mentions them (0 => duplicates share no specifics: worst case).
        if event_id not in ev_det:
            ev_det[event_id] = (rng.choice(PRODUCTS), rng.randrange(1, 13), rng.choice(TOD))
        det = ev_det[event_id] if (issue != "other" and rng.random() < detail_share) else None
        text, lang = make_text(issue, channel, rng, lang, det)
        sev, act = _label(issue, rng)
        if incident_id: act = True
        uid[0] += 1
        records.append({"id": f"S{seed}-{uid[0]:06d}", "store_id": store["store_id"], "timestamp": ts.isoformat(), "channel": channel,
                        "text": text, "label_issue_type": issue, "label_severity": sev, "label_actionable": int(act),
                        "event_id": event_id, "incident_id": incident_id or "", "lang_style": lang})
    planted_n = 0
    if plant:
        end = start + timedelta(days=days)
        for name, issue, sidx, off in PLANTED_TEMPLATE:
            store = stores[sidx]
            onset = end - timedelta(days=off)
            istart = onset + timedelta(hours=72)
            first = None; cnt = 0
            for h in range(0, 24 * 7):                      # hourly Poisson-ish draws
                t = onset + timedelta(hours=h)
                if h < 72: rate = 0.04 + (0.20 - 0.04) * (h / 72)     # low, rising phase (small absolute volume)
                elif h < 72 + 96: rate = 0.35                          # surge phase
                else: rate = 0
                k = 0; p = rate
                while rng.random() < p and k < 5: k += 1; p = rate
                for _ in range(k):
                    ts = t + timedelta(minutes=rng.randrange(60)); ev = f"{name[:7]}-{cnt}"
                    ch = rng.choice(CHANNELS)
                    add(store, ts, issue, ch, ev, name); cnt += 1
                    if first is None or ts < first: first = ts
                    if rng.random() < .15:                    # same incident reported on another channel shortly after
                        ch2 = rng.choice([c for c in CHANNELS if c != ch]); add(store, ts + timedelta(hours=rng.randrange(1, 30)), issue, ch2, ev, name); cnt += 1
            incidents.append({"name": name, "store_id": store["store_id"], "issue_type": issue, "onset": onset.isoformat(),
                              "incident_start": istart.isoformat(), "first_report_at": first.isoformat() if first else None,
                              "n_records": cnt, "safety": issue in SAFETY_ISSUES})
            planted_n += cnt
    # background
    n_bg = max(0, n_records - planted_n)
    tot_mult = sum(mult)
    k = 0
    while k < n_bg:
        sidx = rng.choices(range(n_stores), mult)[0]; store = stores[sidx]
        day = rng.random() * days
        ts = start + timedelta(days=day)
        if rng.random() < .35: issue = "other"
        else:
            w = store_w[sidx]; issue = rng.choices(ISSUE_LIST, [w[i] for i in ISSUE_LIST])[0]
        ch = rng.choice(CHANNELS); ev = f"B{seed}-{k}"
        add(store, ts, issue, ch, ev); k += 1
        if issue != "other" and rng.random() < .10 and k < n_bg:       # duplicate of the same event on other channels
            for ch2 in rng.sample([c for c in CHANNELS if c != ch], rng.choice([1, 1, 2, 3])):
                if k >= n_bg: break
                add(store, ts + timedelta(hours=rng.randrange(0, 30)), issue, ch2, ev); k += 1
    records.sort(key=lambda r: r["timestamp"])
    return {"records": records, "stores": stores, "incidents": incidents, "seed": seed, "start": start.isoformat(), "days": days}
