"""XAUUSD FVRP signal bot — GitHub Actions edition"""
import csv, json, os, sys, time
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Config ───────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BOT_DIR = Path(__file__).parent
STATE_DIR = BOT_DIR / "state"
STATE_DIR.mkdir(exist_ok=True)

BIN_SIZE = 0.5
TICK_V = 0.10
MAX_R = 80.0

MODELS = [
    {"name": "2R",     "sl_atr": 0.2,  "target": 2.0},
    {"name": "2R-q",   "sl_atr": 0.3,  "target": 2.0},
    {"name": "2R-h",   "sl_atr": 0.5,  "target": 2.0},
    {"name": "1R",     "sl_atr": 1.0,  "target": 1.0},
    {"name": "1R-70",  "sl_atr": 1.5,  "target": 1.0},
]

session = requests.Session()

# ── DST-aware session times ──────────────────────────────────────

def session_utc_starts():
    from zoneinfo import ZoneInfo
    london = datetime.now(ZoneInfo("Europe/London"))
    ny = datetime.now(ZoneInfo("America/New_York"))
    # London open 08:00 local → UTC = 8 - offset
    # NY open 09:30 local → UTC = 9.5 - offset
    lon_h = 8 - london.utcoffset().total_seconds() / 3600
    ny_h = 9.5 - ny.utcoffset().total_seconds() / 3600
    return lon_h, ny_h

def get_session_times():
    lon_h, ny_h = session_utc_starts()
    lh, lm = int(lon_h), int(round((lon_h % 1) * 60))
    nh, nm = int(ny_h), int(round((ny_h % 1) * 60))
    ae_h, ae_m = (lh, lm - 1) if lm > 0 else (lh - 1, 59)
    le_h, le_m = (nh, nm - 1) if nm > 0 else (nh - 1, 59)
    return {
        "asia":    {"zh": 0, "zm": 0,  "active": (0, 0, ae_h, ae_m)},
        "london":  {"zh": lh, "zm": lm, "active": (lh, lm, le_h, le_m)},
        "newyork": {"zh": nh, "zm": nm, "active": (nh, nm, 23, 55)},
    }

def detect_session(now=None):
    now = now or datetime.now(timezone.utc)
    hm = now.hour * 60 + now.minute
    st = get_session_times()
    for sk in ("asia", "london", "newyork"):
        a0, b0, a1, b1 = st[sk]["active"]
        if a0 * 60 + b0 <= hm <= a1 * 60 + b1:
            return sk
    return None

def is_zone_time(sk, now=None):
    now = now or datetime.now(timezone.utc)
    st = get_session_times()
    target = st[sk]["zh"] * 60 + st[sk]["zm"]
    actual = now.hour * 60 + now.minute
    return abs(actual - target) <= 5

LABELS = {"asia": "Asia (Tokyo)", "london": "London", "newyork": "New York"}

# ── Swissquote API ───────────────────────────────────────────────

def swissquote_bidask():
    try:
        r = session.get(
            "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD",
            timeout=10, verify=False
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                spp = data[0].get("spreadProfilePrices", [{}])[0]
                bid, ask = spp.get("bid"), spp.get("ask")
                if bid and ask: return float(bid), float(ask)
    except: pass
    return None

# ── State I/O (JSON files) ───────────────────────────────────────

def read_state(name):
    p = STATE_DIR / f"{name}.json"
    if p.exists():
        with open(p) as f:
            try: return json.load(f)
            except: pass
    return {} if name in ("zones", "triggered") else []

def write_state(name, data):
    p = STATE_DIR / f"{name}.json"
    with open(p, "w") as f:
        json.dump(data, f, indent=2, default=str)

# ── Zone building ────────────────────────────────────────────────

def poll_ticks(duration=900, interval=5):
    ticks = []
    deadline = time.time() + duration
    while time.time() < deadline:
        ba = swissquote_bidask()
        if ba:
            ticks.append((datetime.now(timezone.utc), (ba[0] + ba[1]) / 2))
        time.sleep(interval)
    return ticks

def compute_zone(ticks):
    if len(ticks) < 5: return None
    mids = [m for _, m in ticks]
    bins = defaultdict(int)
    for p in mids: bins[round(p / BIN_SIZE) * BIN_SIZE] += 1
    bs = sorted(bins.items()); tv = sum(bins.values()); tgt = tv * 0.6
    br = float("inf"); bv = bvh = None
    for i in range(len(bs)):
        cum = 0
        for j in range(i, len(bs)):
            cum += bs[j][1]
            if cum >= tgt and bs[j][0] - bs[i][0] < br:
                br = bs[j][0] - bs[i][0]; bv = bs[i][0]; bvh = bs[j][0]; break
    return {"VAL": bv, "VAH": bvh, "n_ticks": len(mids)} if bv else None

def estimate_atr(ph):
    ph = [p for p in ph if time.time() - p["t"] < 86400 * 3]
    if len(ph) < 30: return None
    buckets = defaultdict(list)
    for p in ph:
        bk = (datetime.fromtimestamp(p["t"], tz=timezone.utc).hour // 4)
        buckets[bk].append(p["p"])
    if len(buckets) < 15: return None
    ranges = []; prev = None
    for bk in sorted(buckets):
        v = buckets[bk]; h, l = max(v), min(v)
        if prev: ranges.append(max(h - l, abs(h - prev), abs(l - prev)))
        else: ranges.append(h - l)
        prev = v[-1]
    return sum(ranges[-14:]) / 14 if len(ranges) >= 14 else None

def estimate_trend(ph):
    ph = [p for p in ph if time.time() - p["t"] < 43200]
    if len(ph) < 6: return 0
    return 1 if ph[-1]["p"] > ph[0]["p"] + 0.5 else (-1 if ph[-1]["p"] < ph[0]["p"] - 0.5 else 0)

def build_zone():
    now = datetime.now(timezone.utc)
    sk = detect_session(now)
    if not sk or not is_zone_time(sk, now):
        return None
    
    print(f"Building {sk} zone...")
    ticks = poll_ticks(900, 5)
    if len(ticks) < 5:
        print(f"Not enough ticks: {len(ticks)}")
        return None
    
    zone = compute_zone(ticks)
    if not zone:
        print("Zone computation failed")
        return None
    
    today = now.date().isoformat()
    zone["date"] = today
    zone["session"] = sk
    
    zones = read_state("zones")
    triggered = read_state("triggered")
    ph = read_state("price_history")
    
    zones[sk] = zone
    if sk not in triggered: triggered[sk] = {}
    for m in MODELS: triggered[sk][m["name"]] = False
    
    write_state("zones", zones)
    write_state("triggered", triggered)
    
    atr = estimate_atr(ph)
    trend = estimate_trend(ph)
    
    msg = (
        f"{sk.upper()} zone — {today} ({LABELS.get(sk, sk)})\n"
        f"VAH: ${zone['VAH']:.2f}  VAL: ${zone['VAL']:.2f}\n"
        f"Ticks: {zone['n_ticks']}"
    )
    if atr: msg += f"  ATR: ${atr:.2f}"
    if trend: msg += f"  Trend: {'UP' if trend > 0 else 'DOWN'}"
    print(msg)
    return msg

# ── Breakout check ───────────────────────────────────────────────

def check_breakouts():
    now = datetime.now(timezone.utc)
    sk = detect_session(now)
    if not sk: return None
    
    zones = read_state("zones")
    zone = zones.get(sk)
    if not zone or zone.get("date") != now.date().isoformat():
        return None
    
    ba = swissquote_bidask()
    if not ba: return None
    mid = (ba[0] + ba[1]) / 2
    
    vah, val = zone["VAH"], zone["VAL"]
    lo, sh = mid > vah, mid < val
    if not lo and not sh:
        return None  # in zone, no alert
    
    triggered = read_state("triggered")
    sk_trig = triggered.get(sk, {})
    signals = []
    
    ph = read_state("price_history")
    atr = estimate_atr(ph) or 10.0
    trend = estimate_trend(ph)
    
    for m in MODELS:
        name = m["name"]
        if sk_trig.get(name): continue
        
        sd = "long" if lo else "short"
        if (sd == "long" and trend <= 0) or (sd == "short" and trend >= 0):
            continue
        
        sl = (mid - m["sl_atr"] * atr - 0.05) if sd == "long" else (mid + m["sl_atr"] * atr + 0.05)
        risk_actual = abs(mid - sl)
        pos = max(1, int(MAX_R / (int(risk_actual / TICK_V) * TICK_V)))
        tp = mid + m["target"] * risk_actual if sd == "long" else mid - m["target"] * risk_actual
        r_mult = abs(tp - mid) / abs(mid - sl) if abs(mid - sl) > 0 else 0
        
        signals.append(
            f"{sk.upper()} {name}\n"
            f"{'LONG' if sd == 'long' else 'SHORT'} @ ${mid:.2f}\n"
            f"SL: ${sl:.2f}  TP: ${tp:.2f}  R:1:{r_mult:.1f}\n"
            f"Size: {pos} ul  Risk: ${risk_actual*pos*TICK_V:.2f}"
        )
        sk_trig[name] = True
    
    triggered[sk] = sk_trig
    write_state("triggered", triggered)
    
    # Update price history
    ph.append({"t": time.time(), "p": mid})
    write_state("price_history", ph[-500:])
    
    return "\n\n".join(signals) if signals else None

# ── Telegram ─────────────────────────────────────────────────────

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"TELEGRAM not configured. Would send:\n{msg}")
        return
    try:
        r = session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=15
        )
        if r.status_code == 200: print("Telegram sent")
        else: print(f"Telegram error: {r.status_code}")
    except Exception as e:
        print(f"Telegram error: {e}")

# ── Main ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    sk = detect_session(now)
    
    if sk and is_zone_time(sk, now):
        msg = build_zone()
    elif sk:
        msg = check_breakouts()
    else:
        msg = None
    
    if msg:
        send_telegram(msg)
        # Signal to workflow that state changed
        print("STATE_CHANGED")
