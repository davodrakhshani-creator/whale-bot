#!/usr/bin/env python3
"""Whale paper-copy bot. Reads PUBLIC Hyperliquid data only. No keys, no real orders.
Run every few minutes (GitHub Actions). State lives in state.json, view data in docs/data.json."""
import json, os, sys, time, urllib.request

API = "https://api.hyperliquid.xyz/info"
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_F = os.path.join(ROOT, "state.json")
DATA_F = os.path.join(ROOT, "docs", "data.json")
CFG_F = os.path.join(ROOT, "config.json")
SLIP, FEE = 0.0003, 0.00045
PENDING_TTL = 30 * 60 * 1000
HEARTBEAT_S = 3600


def post(payload, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(API, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # network / rate limit
            last = e
            time.sleep(2 * (i + 1))
    raise last


def parse_dir(d):
    d = (d or "").strip().lower()
    if d.startswith("open"):
        return "open", ("long" if "long" in d else "short")
    if d.startswith("close"):
        return "close", ("long" if "long" in d else "short")
    return None, None


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def marks_now():
    m = {}
    for p in ({"type": "allMids"}, {"type": "allMids", "dex": "xyz"}):
        try:
            for k, v in post(p).items():
                try:
                    m[k] = float(v)
                except Exception:
                    pass
        except Exception as e:
            print("marks failed", p, e, file=sys.stderr)
    return m


def unreal(pos, mark):
    n = pos["margin"] * pos["lev"]
    d = 1 if pos["side"] == "long" else -1
    return n * d * (mark - pos["entry"]) / pos["entry"] - n * FEE * 2


def log(st, now, text):
    st["events"].append({"t": now, "text": text})
    st["events"] = st["events"][-200:]
    st["dirty"] = True
    print(text)


def free_margin(st):
    return st["balance"] - sum(p["margin"] for p in st["open"].values())


def open_paper(st, cfg, w, coin, side, avg_px, usd, now, marks):
    key = f"{w['addr']}|{coin}|{side}"
    if key in st["open"]:
        return
    S = cfg["settings"]
    if len(st["open"]) >= S["max_open"] or free_margin(st) < S["margin"]:
        log(st, now, f"{w['tag']} باز کرد {coin} {side} (~${usd:,.0f}) ولی جا یا موجودی نبود")
        return
    base = marks.get(coin) or avg_px
    entry = base * (1 + SLIP) if side == "long" else base * (1 - SLIP)
    st["open"][key] = {"key": key, "tag": w["tag"], "group": w.get("group", ""), "coin": coin, "side": side, "entry": entry,
                       "whale_px": avg_px, "margin": S["margin"], "lev": S["leverage"], "opened_at": now, "whale_usd": usd}
    log(st, now, f"کپی مجازی باز شد: {w['tag']} {coin} {'لانگ' if side == 'long' else 'شورت'} ورود {entry:.6g} (حجم نهنگ ~${usd:,.0f})")


def close_paper(st, key, px, now, reason):
    pos = st["open"].pop(key, None)
    if not pos:
        return
    if reason == "liq":
        pnl = -pos["margin"]
        ex = px
    else:
        ex = px * (1 - SLIP) if pos["side"] == "long" else px * (1 + SLIP)
        n = pos["margin"] * pos["lev"]
        d = 1 if pos["side"] == "long" else -1
        pnl = max(-pos["margin"], n * d * (ex - pos["entry"]) / pos["entry"] - n * FEE * 2)
    st["balance"] += pnl
    st["closed"].append({**pos, "exit": ex, "pnl": pnl, "closed_at": now, "reason": reason})
    st["closed"] = st["closed"][-300:]
    why = {"whale": "نهنگ بست", "liq": "تصفیه"}.get(reason, reason)
    log(st, now, f"کپی مجازی بسته شد ({why}): {pos['tag']} {pos['coin']} {'لانگ' if pos['side'] == 'long' else 'شورت'} سود/زیان {pnl:+.2f}$")


def process_wallet(st, cfg, w, fills, marks, now):
    addr = w["addr"]
    last = st["last_seen"].get(addr)
    if last is None:
        st["last_seen"][addr] = now  # first run: ignore history
        st["dirty"] = True
        return
    new = sorted([f for f in fills if int(f.get("time", 0)) > last], key=lambda f: int(f["time"]))
    if not new:
        return
    st["last_seen"][addr] = int(new[-1]["time"])
    st["dirty"] = True
    for f in new:
        coin = f.get("coin", "")
        if coin.startswith("@") or "/" in coin:
            continue
        kind, side = parse_dir(f.get("dir"))
        if not kind:
            continue
        px, sz, t = float(f["px"]), float(f["sz"]), int(f["time"])
        if kind == "close":
            start = float(f.get("startPosition", 0) or 0)
            after = start + (sz if f.get("side") == "B" else -sz)
            key = f"{addr}|{coin}|{side}"
            if key in st["open"] and abs(after) * px < 20:
                close_paper(st, key, px, t, "whale")
            continue
        pk = f"{addr}|{coin}|{side}"
        p = st["pending"].get(pk)
        if not p or t - p["last"] > PENDING_TTL:
            p = {"usd": 0.0, "size": 0.0, "last": t}
        p["usd"] += px * sz
        p["size"] += sz
        p["last"] = t
        st["pending"][pk] = p
    for pk in [k for k, p in st["pending"].items() if k.startswith(addr + "|")]:
        p = st["pending"][pk]
        _, coin, side = pk.split("|")
        if p["usd"] >= cfg["settings"]["min_usd"]:
            open_paper(st, cfg, w, coin, side, p["usd"] / p["size"], p["usd"], p["last"], marks)
            del st["pending"][pk]
        elif now - p["last"] > PENDING_TTL:
            del st["pending"][pk]


def build_data(st, cfg, marks, now):
    opens, upl = [], 0.0
    for p in st["open"].values():
        m = marks.get(p["coin"])
        u = unreal(p, m) if m else None
        if u is not None:
            upl += u
        opens.append({**p, "mark": m, "upnl": u})
    return {"updated": now, "start": cfg["settings"]["start_balance"], "balance": st["balance"], "equity": st["balance"] + upl,
            "open": opens, "closed": st["closed"][-60:][::-1], "events": st["events"][-60:][::-1],
            "wallets": [{"tag": w["tag"], "addr": w["addr"], "group": w.get("group", "")} for w in cfg["wallets"]]}


def main():
    cfg = load(CFG_F, None)
    st = load(STATE_F, None) or {"balance": cfg["settings"]["start_balance"], "open": {}, "closed": [], "pending": {}, "last_seen": {}, "events": [], "data_ts": 0}
    st["dirty"] = False
    now = int(time.time() * 1000)
    marks = marks_now()
    for w in cfg["wallets"]:
        try:
            fills = post({"type": "userFills", "user": w["addr"]})
            process_wallet(st, cfg, w, fills, marks, now)
        except Exception as e:
            print("wallet failed", w["tag"], e, file=sys.stderr)
    for key, p in list(st["open"].items()):
        m = marks.get(p["coin"])
        if m and unreal(p, m) <= -0.9 * p["margin"]:
            close_paper(st, key, m, now, "liq")
    if st["dirty"] or now - st.get("data_ts", 0) > HEARTBEAT_S * 1000:
        st["data_ts"] = now
        os.makedirs(os.path.dirname(DATA_F), exist_ok=True)
        with open(DATA_F, "w", encoding="utf-8") as f:
            json.dump(build_data(st, cfg, marks, now), f, ensure_ascii=False)
        st.pop("dirty", None)
        with open(STATE_F, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    print("done", "equity-ish balance", round(st["balance"], 2), "open", len(st["open"]))


if __name__ == "__main__":
    main()
