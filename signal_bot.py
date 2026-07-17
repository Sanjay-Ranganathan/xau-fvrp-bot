"""XAUUSD FVRP signal bot — Asia, London, & New York sessions"""
import csv, json, os, sys, time
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# === CONFIG ===
TELEGRAM_TOKEN = "8926761913:AAFnBUk9Z3gLalt1v1Rf1WN8YgjTlvLAKXA"
TELEGRAM_CHAT_ID = "814890629"
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BOT_DIR, "state.json")
BIN_SIZE = 0.5   # $0.50 bins for XAUUSD
TICK_V = 0.10
MAX_R = 80.0

# Hardcoded zone start times (UTC).
# User provides IST times: Asia 5:30, London 13:00, NY 19:00.
# IST = UTC+5:30, so UTC times are:
SESSIONS = {
    "asia":    {"zone": (0, 0),     "active_end": (7, 29)},   # 5:30 IST=00:00 UTC, end before London
    "london":  {"zone": (7, 30),    "active_end": (13, 29)},  # 13:00 IST=07:30 UTC, end before NY
    "newyork": {"zone": (13, 30),   "active_end": (23, 55)},  # 19:00 IST=13:30 UTC
}

LABELS = {
    "asia": "Asia (Tokyo open)",
    "london": "London open",
    "newyork": "New York open",
}

MODELS = [
    {"name": "2R",     "sl_atr": 0.2,  "target": 2.0},
    {"name": "2R-q",   "sl_atr": 0.3,  "target": 2.0},
    {"name": "2R-h",   "sl_atr": 0.5,  "target": 2.0},
    {"name": "1R",     "sl_atr": 1.0,  "target": 1.0},
    {"name": "1R-70",  "sl_atr": 1.5,  "target": 1.0},
]

session = requests.Session()

# ── Helpers ──────────────────────────────────────────────────────

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

def detect_session(now=None):
    now = now or datetime.now(timezone.utc)
    hm = now.hour * 60 + now.minute
    for sk in ("asia", "london", "newyork"):
        zh, zm = SESSIONS[sk]["zone"]
        eh, em = SESSIONS[sk]["active_end"]
        start = zh * 60 + zm
        end = eh * 60 + em
        if start <= hm <= end:
            return sk
    return None

def poll_ticks(duration=900, interval=5):
    """Poll Swissquote. Returns [(ts, mid), ...]."""
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
    bs = sorted(bins.items())
    tv = sum(bins.values()); tgt = tv * 0.6
    br = float("inf"); bv = bvh = None
    for i in range(len(bs)):
        cum = 0
        for j in range(i, len(bs)):
            cum += bs[j][1]
            if cum >= tgt and bs[j][0] - bs[i][0] < br:
                br = bs[j][0] - bs[i][0]; bv = bs[i][0]; bvh = bs[j][0]; break
    return {"VAL": bv, "VAH": bvh, "n_ticks": len(mids)} if bv else None

def estimate_atr_from_history(state):
    ph = state.get("price_history", [])
    now = time.time()
    ph = [p for p in ph if now - p["t"] < 86400 * 3]
    if len(ph) < 30: return None
    buckets = defaultdict(list)
    for p in ph:
        bk = (datetime.fromtimestamp(p["t"], tz=timezone.utc).hour // 4)
        buckets[bk].append(p["p"])
    if len(buckets) < 15: return None
    ranges = []
    prev = None
    for bk in sorted(buckets):
        v = buckets[bk]; h, l = max(v), min(v)
        if prev: ranges.append(max(h - l, abs(h - prev), abs(l - prev)))
        else: ranges.append(h - l)
        prev = v[-1]
    return sum(ranges[-14:]) / 14 if len(ranges) >= 14 else None

def estimate_trend_from_history(state):
    ph = state.get("price_history", [])
    now = time.time()
    ph = [p for p in ph if now - p["t"] < 43200]
    if len(ph) < 6: return 0
    return 1 if ph[-1]["p"] > ph[0]["p"] + 0.5 else (-1 if ph[-1]["p"] < ph[0]["p"] - 0.5 else 0)

# ── State ────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            try: return json.load(f)
            except: pass
    return {"zones": {}, "triggered": {}, "price_history": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Build zone ───────────────────────────────────────────────────

def is_zone_time(now, sk):
    zh, zm = SESSIONS[sk]["zone"]
    target = zh * 60 + zm
    actual = now.hour * 60 + now.minute
    return abs(actual - target) <= 5

def build_zone():
    now = datetime.now(timezone.utc)
    sk = detect_session(now)
    if not sk or not is_zone_time(now, sk):
        return None

    label = LABELS.get(sk, sk)

    state = load_state()
    zones = state.get("zones", {})
    today = now.date().isoformat()
    existing = zones.get(sk, {})
    if existing.get("date") == today:
        return None

    print(f"[{now.isoformat()}] Building {sk} zone ({label})...")

    ticks = poll_ticks(duration=900, interval=5)
    if len(ticks) < 5:
        return f"⚠️ {sk}: not enough ticks ({len(ticks)})"

    zone = compute_zone(ticks)
    if not zone:
        return f"⚠️ {sk}: zone computation failed"

    zone["date"] = today
    zone["session"] = sk

    if "zones" not in state: state["zones"] = {}
    if "triggered" not in state: state["triggered"] = {}
    if "rebreak" not in state: state["rebreak"] = {}

    state["zones"][sk] = zone
    state["triggered"][sk] = {m["name"]: False for m in MODELS}
    # Reset REBREAK_OPP state for this session
    state["rebreak"][sk] = {"first_dir": None, "first_seen": False, "opp_touched": False, "took": False}
    state["price_history"] = state.get("price_history", [])
    state["last_session"] = sk
    save_state(state)

    atr = estimate_atr_from_history(state)
    trend = estimate_trend_from_history(state)
    trend_str = {1: "UP", -1: "DOWN", 0: "NEUTRAL"}.get(trend, "?")
    atr_str = f"${atr:.2f}" if atr else "N/A"
    bias = "LONG on VAH break" if trend > 0 else ("SHORT on VAL break" if trend < 0 else "EITHER")

    msg = (
        f"{sk.upper()} zone — {today} ({label})\n"
        f"VAH: ${zone['VAH']:.2f}  VAL: ${zone['VAL']:.2f}\n"
        f"Ticks: {zone['n_ticks']}  ATR: {atr_str}  Trend: {trend_str}\n"
        f"Bias: {bias}\n"
        f"Monitoring breakouts..."
    )
    print(msg)
    return msg

# ── Check breakouts (REBREAK_OPP: skip first, wait for opp test, take re-break) ─

def check_breakouts():
    now = datetime.now(timezone.utc)
    sk = detect_session(now)
    if not sk:
        return None

    state = load_state()
    zones = state.get("zones", {})
    zone = zones.get(sk)
    if not zone:
        return None

    today = now.date().isoformat()
    if zone.get("date") != today:
        return None

    ba = swissquote_bidask()
    if not ba:
        return "⚠️ Cannot fetch price"
    mid = (ba[0] + ba[1]) / 2

    vah, val = zone["VAH"], zone["VAL"]
    lo = mid > vah
    sh_brk = mid < val
    in_zone = not lo and not sh_brk

    # Init rebreak state
    if "rebreak" not in state: state["rebreak"] = {}
    if sk not in state["rebreak"]:
        state["rebreak"][sk] = {"first_dir": None, "first_seen": False, "opp_touched": False, "took": False}
    rb = state["rebreak"][sk]

    # Track opposite side touch (price visits the other zone level)
    if rb["first_seen"] and not rb["opp_touched"]:
        if rb["first_dir"] == "long":
            # For long, opp is VAL
            if mid <= val:
                rb["opp_touched"] = True
                print(f"[{now.isoformat()}] {sk}: opp side (VAL) touched, ready for re-break")
                save_state(state)
        elif rb["first_dir"] == "short":
            # For short, opp is VAH
            if mid >= vah:
                rb["opp_touched"] = True
                print(f"[{now.isoformat()}] {sk}: opp side (VAH) touched, ready for re-break")
                save_state(state)

    if not in_zone:
        breakout_dir = "long" if lo else "short"

        # Case 1: First breakout of the session — skip it (likely fakeout)
        if not rb["first_seen"]:
            rb["first_seen"] = True
            rb["first_dir"] = breakout_dir
            save_state(state)
            print(f"[{now.isoformat()}] {sk}: first {breakout_dir} breakout — skip, waiting for opp test + re-break")
            return None  # no signal sent

        # Case 2: Already took the re-break trade — nothing more to do
        if rb["took"]:
            return None

        # Case 3: Breakout in opposite direction of first breakout — reset
        if breakout_dir != rb["first_dir"]:
            rb["first_dir"] = breakout_dir
            rb["first_seen"] = True
            rb["opp_touched"] = False
            save_state(state)
            print(f"[{now.isoformat()}] {sk}: direction flip to {breakout_dir} — reset, skip as first")
            return None

        # Case 4: Same direction breakout — only trade if opp side was touched
        if rb["opp_touched"]:
            rb["took"] = True
            save_state(state)

            triggered = state.get("triggered", {}).get(sk, {})
            signals = []
            for m in MODELS:
                name = m["name"]
                if triggered.get(name, False): continue

                atr_v = estimate_atr_from_history(state) or 10.0
                trend = estimate_trend_from_history(state)

                if (breakout_dir == "long" and trend <= 0) or (breakout_dir == "short" and trend >= 0):
                    continue

                sl = (mid - m["sl_atr"] * atr_v - 0.05) if breakout_dir == "long" else (mid + m["sl_atr"] * atr_v + 0.05)
                risk_actual = abs(mid - sl)
                pos = max(1, int(MAX_R / (int(risk_actual / TICK_V) * TICK_V)))
                tp = mid + m["target"] * risk_actual if breakout_dir == "long" else mid - m["target"] * risk_actual
                r_mult = abs(tp - mid) / abs(mid - sl) if abs(mid - sl) > 0 else 0

                signals.append(
                    f"🚨 {sk.upper()} {name}\n"
                    f"{'LONG' if breakout_dir == 'long' else 'SHORT'}  Entry: ${mid:.2f}\n"
                    f"SL: ${sl:.2f}  TP: ${tp:.2f}  R: 1:{r_mult:.1f}\n"
                    f"Size: {pos} u-lots  Risk: ${risk_actual*pos*TICK_V:.2f}"
                )

                if "triggered" not in state: state["triggered"] = {}
                if sk not in state["triggered"]: state["triggered"][sk] = {}
                state["triggered"][sk][name] = True

            if not signals:
                return None  # trend rejects, nothing to send

            save_state(state)
            state["price_history"].append({"t": time.time(), "p": mid})
            state["price_history"] = state["price_history"][-500:]
            save_state(state)

            return "\n\n".join(signals)

        # Case 5: Same direction but opp NOT touched yet — wait
        print(f"[{now.isoformat()}] {sk}: re-break {breakout_dir} but opp not touched — wait")
        return None

    # Price is in zone — no breakout
    return None

# ── Telegram ─────────────────────────────────────────────────────

def send_telegram(msg):
    if TELEGRAM_TOKEN == "YOUR_TOKEN":
        print(f"\n--- MESSAGE ---\n{msg}\n---------------")
        return
    try:
        r = session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=15
        )
        if r.status_code == 200:
            print("✅ Telegram sent")
        else:
            print(f"❌ Telegram error: {r.status_code}")
    except Exception as e:
        print(f"❌ Telegram error: {e}")

# ── CLI ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"

    if cmd == "build":
        msg = build_zone()
    elif cmd == "check":
        msg = check_breakouts()
        if msg is None: sys.exit(0)
    else:
        msg = f"Unknown: {cmd}. Use build|check"

    if msg:
        send_telegram(msg)
