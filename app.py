from flask import Flask, request, jsonify, Response
from datetime import datetime, time
from zoneinfo import ZoneInfo
import os
import json
import uuid

try:
    import requests
except Exception:
    requests = None

app = Flask(__name__)

# =========================
# Config (Render Env Vars)
# =========================
SECRET = os.getenv("SECRET", "").strip()
TG_TOKEN = os.getenv("TG_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()

# Trading window (Puerto Rico)
TZ = ZoneInfo("America/Puerto_Rico")
ENFORCE_HOURS = os.getenv("ENFORCE_HOURS", "1").strip()  # "1" = on, "0" = off
START_HHMM = os.getenv("TRADE_START", "05:00").strip()   # 05:00
END_HHMM = os.getenv("TRADE_END", "16:00").strip()       # 16:00

def parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))

TRADE_START = parse_hhmm(START_HHMM)
TRADE_END = parse_hhmm(END_HHMM)

# =========================
# In-memory "queue"
# =========================
STATE = {
    "last": None,        # last received (even if already pulled)
    "pending": None,     # next to be pulled (one-shot)
}

def now_pr() -> datetime:
    return datetime.now(TZ)

def in_trade_window(dt: datetime) -> bool:
    if ENFORCE_HOURS != "1":
        return True
    t = dt.time()
    # inclusive start, inclusive end
    return (t >= TRADE_START) and (t <= TRADE_END)

def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT_ID or requests is None:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=8)
    except Exception:
        pass

def check_secret(payload_secret: str | None, header_secret: str | None, query_secret: str | None) -> bool:
    if not SECRET:
        # If you forgot to set SECRET in Render, allow nothing for safety.
        return False
    candidate = (payload_secret or header_secret or query_secret or "").strip()
    return candidate == SECRET

def normalize_action(a: str) -> str:
    a = (a or "").strip().upper()
    if a in ("BUY", "LONG"):
        return "BUY"
    if a in ("SELL", "SHORT"):
        return "SELL"
    return ""

def safe_float(x):
    try:
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if s == "" or s.lower() == "na":
            return None
        return float(s)
    except Exception:
        return None

def build_signal(data: dict) -> dict:
    action = normalize_action(data.get("action") or data.get("side") or data.get("signal"))
    symbol = (data.get("symbol") or data.get("ticker") or "XAUUSD").strip()
    lots = safe_float(data.get("lots") or data.get("lot") or data.get("qty") or 0.01) or 0.01
    sl = safe_float(data.get("sl"))
    tp = safe_float(data.get("tp"))
    comment = (data.get("comment") or data.get("cmt") or "TV").strip()
    magic = int(safe_float(data.get("magic") or 2026) or 2026)

    sig_id = (data.get("id") or data.get("signal_id") or str(uuid.uuid4())[:8]).strip()
    ts = now_pr().isoformat()

    return {
        "id": sig_id,
        "ts": ts,
        "action": action,
        "symbol": symbol,
        "lots": lots,
        "sl": sl,
        "tp": tp,
        "comment": comment,
        "magic": magic,
        "raw": data,
    }

def parse_body_to_dict():
    # Accept JSON (preferred) OR plain text "KEY=VALUE" lines OR pipe "BUY|XAUUSD|0.01|SL|TP"
    if request.is_json:
        try:
            return request.get_json(force=True) or {}
        except Exception:
            return {}

    body = (request.get_data(as_text=True) or "").strip()
    if not body:
        return {}

    # Try JSON string
    if body.startswith("{") and body.endswith("}"):
        try:
            return json.loads(body)
        except Exception:
            pass

    # Pipe format: BUY|XAUUSD|0.01|SL|TP
    if "|" in body and ("BUY" in body.upper() or "SELL" in body.upper()):
        parts = [p.strip() for p in body.split("|")]
        d = {}
        if len(parts) >= 1: d["action"] = parts[0]
        if len(parts) >= 2: d["symbol"] = parts[1]
        if len(parts) >= 3: d["lots"] = parts[2]
        if len(parts) >= 4: d["sl"] = parts[3]
        if len(parts) >= 5: d["tp"] = parts[4]
        return d

    # KEY=VALUE lines
    d = {}
    for line in body.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d

@app.get("/")
def root():
    return jsonify({
        "service": "tv-mt4-bridge",
        "ok": True,
        "endpoints": ["/health", "/webhook (POST)", "/pull", "/latest"],
        "trade_window_pr": {"start": START_HHMM, "end": END_HHMM, "enforced": ENFORCE_HOURS == "1"}
    })

@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "time_pr": now_pr().isoformat(),
        "has_secret": bool(SECRET),
        "pending": STATE["pending"] is not None,
        "latest": STATE["last"]["id"] if STATE["last"] else None
    })

@app.post("/webhook")
def webhook():
    data = parse_body_to_dict()

    payload_secret = (data.get("secret") or data.get("key") or data.get("token"))
    header_secret = request.headers.get("X-Secret")
    query_secret = request.args.get("secret")

    if not check_secret(payload_secret, header_secret, query_secret):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    dt = now_pr()
    if not in_trade_window(dt):
        return jsonify({
            "ok": False,
            "error": "outside_trade_window",
            "time_pr": dt.isoformat(),
            "window": {"start": START_HHMM, "end": END_HHMM}
        }), 403

    sig = build_signal(data)
    if not sig["action"]:
        return jsonify({"ok": False, "error": "missing_action_BUY_or_SELL"}), 400

    # Store last + pending (one-shot)
    STATE["last"] = sig
    STATE["pending"] = sig

    tg_send(f"✅ TV Signal received: {sig['action']} {sig['symbol']} lots={sig['lots']} sl={sig['sl']} tp={sig['tp']} id={sig['id']}")

    return jsonify({"ok": True, "stored": True, "id": sig["id"], "time_pr": sig["ts"]})

@app.get("/latest")
def latest():
    query_secret = request.args.get("secret")
    header_secret = request.headers.get("X-Secret")
    if not check_secret(None, header_secret, query_secret):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    return jsonify({"ok": True, "last": STATE["last"]})

@app.get("/pull")
def pull():
    """
    MT4 polls this endpoint. It returns the pending signal ONCE, then clears it.
    Use ?format=txt for easy MT4 parsing.
    """
    query_secret = request.args.get("secret")
    header_secret = request.headers.get("X-Secret")
    if not check_secret(None, header_secret, query_secret):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    fmt = (request.args.get("format") or "json").lower()

    sig = STATE["pending"]
    if sig is None:
        if fmt == "txt":
            return Response("NONE", mimetype="text/plain")
        return jsonify({"ok": True, "pending": None})

    # consume one-shot
    STATE["pending"] = None

    if fmt == "txt":
        # OK|id|action|symbol|lots|sl|tp|ts|magic|comment
        sl = "" if sig["sl"] is None else str(sig["sl"])
        tp = "" if sig["tp"] is None else str(sig["tp"])
        comment = (sig.get("comment") or "").replace("|", " ")
        out = f"OK|{sig['id']}|{sig['action']}|{sig['symbol']}|{sig['lots']}|{sl}|{tp}|{sig['ts']}|{sig['magic']}|{comment}"
        return Response(out, mimetype="text/plain")

    return jsonify({"ok": True, "pending": sig})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))