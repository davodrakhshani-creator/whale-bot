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
DATA_HEARTBEAT_S = 1800
HIST_F = os.path.join(ROOT, "history.json")
HIST_DAYS = 90
HIST_TTL = 6 * 3600 * 1000      # refresh a wallet's 90-day record every 6h
HIST_PER_RUN = 11               # wallets refreshed per run (keeps API weight under the limit)
HIST_MAX_PAGES = 6              # 2000 fills per page
GAP = 5 * 60 * 1000             # same coin+direction fills closer than this = one move
FEED_WINDOW = 72 * 3600 * 1000
FEED_MAX = 60
FULL_PER_RUN = 5              # heavy full refreshes per loop (weight ~140 each)
FULL_EVERY = 270 * 1000        # full feed refresh per wallet at most this often (the cheap new-fills check runs every loop)


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
    if d.startswith("close") or d.startswith("liquidated") or d.startswith("auto-deleveraging"):
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
    px = binance_paxg()
    if px:
        m["PAXG"] = px
    else:
        g = next((v for k, v in m.items() if is_gold(k)), None)
        if g:
            m["PAXG"] = g  # fallback: Hyperliquid gold price
    return m


def is_gold(coin):
    return "gold" in coin.lower()


def binance_paxg():
    """Public Binance spot price of PAXG (gold-backed token). Read-only, no key."""
    # perpetual futures first (supports long AND short), then spot, then Hyperliquid gold as last fallback
    for url in ("https://fapi.binance.com/fapi/v1/ticker/price?symbol=PAXGUSDT",
                "https://data-api.binance.vision/api/v3/ticker/price?symbol=PAXGUSDT",
                "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT"):
        host = url.split("/")[2]
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return float(json.loads(r.read().decode())["price"])
        except Exception as e:
            print("binance failed", host, e, file=sys.stderr)
    return None


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
    inst, margin, lev = coin, S["margin"], S["leverage"]
    if is_gold(coin):
        inst, margin, lev = "PAXG", S.get("gold_margin", 100), 1
        if sum(1 for p in st["open"].values() if p["coin"] == "PAXG") >= S.get("gold_max", 3):
            log(st, now, f"{w['tag']} خرید طلا (~${usd:,.0f}) ولی سقف {S.get('gold_max', 3)} پوزیشن هم‌زمان طلا پر بود (نهنگ‌های طلا هم‌زمان می‌خرند)")
            return
    if len(st["open"]) >= S["max_open"]:
        log(st, now, f"{w['tag']} باز کرد {coin} {side} (~${usd:,.0f}) ولی سقف {S['max_open']} پوزیشن باز پر بود")
        return
    if free_margin(st) < margin:
        log(st, now, f"{w['tag']} باز کرد {coin} {side} (~${usd:,.0f}) ولی موجودی آزاد کافی نبود")
        return
    base = marks.get(inst) or avg_px
    entry = base * (1 + SLIP) if side == "long" else base * (1 - SLIP)
    st["open"][key] = {"key": key, "tag": w["tag"], "group": w.get("group", ""), "coin": inst, "src": coin, "side": side, "entry": entry,
                       "whale_px": avg_px, "margin": margin, "lev": lev, "opened_at": now, "whale_usd": usd}
    log(st, now, f"کپی مجازی باز شد: {w['tag']} {inst} {'لانگ' if side == 'long' else 'شورت'} ورود {entry:.6g} (حجم نهنگ ~${usd:,.0f})")


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
        d = (f.get("dir") or "").strip().lower()
        if ">" in d:  # flip, e.g. "Long > Short": old side closed, remainder opens the other side
            a, b = [x.strip() for x in d.split(">", 1)]
            px, sz, t = float(f["px"]), float(f["sz"]), int(f["time"])
            okey = f"{addr}|{coin}|{a}"
            if okey in st["open"]:
                close_paper(st, okey, (marks.get("PAXG") or px) if is_gold(coin) else px, t, "whale")
            start = float(f.get("startPosition", 0) or 0)
            after_f = start + (sz if f.get("side") == "B" else -sz)
            rem = abs(after_f)
            if (after_f > 0) != (b == "long"):
                continue  # dir text says flip but the position did not actually cross zero: do nothing
            if rem * px > 0 and b in ("long", "short"):
                if rem >= 0.3 * abs(start):  # whale really reversed (not just closed and left a crumb)
                    st["pending"][f"{addr}|{coin}|{b}"] = {"usd": rem * px, "size": rem, "last": t}
                else:
                    log(st, t, f"{w['tag']} {coin} را بست و فقط بخش کوچکی ({rem * px:,.0f}$) در جهت مخالف ماند؛ شورت/لانگ جدید کپی نشد")
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
                close_paper(st, key, (marks.get("PAXG") or px) if is_gold(coin) else px, t, "whale")
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


def whale_positions(addr, xyz):
    """Whale's real current perp positions (public clearinghouseState, weight 2): {coin: signed size}."""
    pos = {}
    for dex in ([None, "xyz"] if xyz else [None]):
        q = {"type": "clearinghouseState", "user": addr}
        if dex:
            q["dex"] = dex
        for ap in post(q).get("assetPositions", []):
            p = ap.get("position", {})
            pos[p.get("coin", "")] = float(p.get("szi", 0) or 0)
    return pos


def reconcile(st, cfg, w, marks, now):
    """Safety net: close a paper copy if the whale no longer holds that position (missed fill, liquidation, odd 'dir' text)."""
    mine = [p for p in st["open"].values() if p["key"].startswith(w["addr"] + "|")]
    if not mine:
        return
    pos = whale_positions(w["addr"], any(p["src"].startswith("xyz:") for p in mine))
    for p in mine:
        if now - p["opened_at"] < 90000:
            continue
        szi = pos.get(p["src"], 0.0)
        px = marks.get(p["coin"]) or p["entry"]
        held = (szi > 0 and p["side"] == "long") or (szi < 0 and p["side"] == "short")
        if not held or abs(szi) * (marks.get(p["src"]) or px) < 20:
            close_paper(st, p["key"], px, now, "whale")
            log(st, now, f"همگام‌سازی: {w['tag']} دیگر {p['src']} {p['side']} ندارد؛ کپی مجازی بسته شد")


def is_spot(coin):
    return coin.startswith("@") or "/" in coin


def fetch_history(addr, now):
    """Page through ~90 days of fills (public data). Throttled to respect the IP weight limit."""
    start, seen, out = now - HIST_DAYS * 86400000, set(), []
    for _ in range(HIST_MAX_PAGES):
        page = post({"type": "userFillsByTime", "user": addr, "startTime": start})
        if not page:
            break
        for f in page:
            k = f.get("tid") or (f.get("hash"), f.get("oid"), f.get("time"), f.get("sz"))
            if k not in seen:
                seen.add(k)
                out.append(f)
        time.sleep(min(8.0, (20 + len(page) / 20.0) / 20.0))
        if len(page) < 2000:
            break
        start = max(int(f["time"]) for f in page)
    return out


def lifecycles(fills):
    """Rebuild closed positions (flat -> flat) per coin from fills. Positions that began before the
    window (first fill already has a non-zero startPosition) are skipped as incomplete."""
    by = {}
    for f in sorted(fills, key=lambda f: (int(f["time"]), int(f.get("tid", 0) or 0))):
        c = f.get("coin", "")
        if not is_spot(c):
            by.setdefault(c, []).append(f)
    res, n_open = [], 0
    for coin, fs in by.items():
        cur = None
        for f in fs:
            sz, px, t = float(f["sz"]), float(f["px"]), int(f["time"])
            signed = sz if f.get("side") == "B" else -sz
            start = float(f.get("startPosition", 0) or 0)
            after = start + signed
            if cur is None:
                cur = {"coin": coin, "side": "long" if signed > 0 else "short", "t0": t, "t1": t, "pnl": 0.0, "liq": False,
                       "skip": abs(start) * px >= 1}
            cur["pnl"] += float(f.get("closedPnl", 0) or 0) - float(f.get("fee", 0) or 0)
            cur["t1"] = t
            if f.get("liquidation"):
                cur["liq"] = True
            crossed = (start > 0 > after) or (start < 0 < after)
            if abs(after) * px < 1 or crossed:
                if not cur["skip"]:
                    res.append(cur)
                cur = None
                if crossed and abs(after) * px >= 1:
                    cur = {"coin": coin, "side": "long" if after > 0 else "short", "t0": t, "t1": t, "pnl": 0.0, "liq": False, "skip": False}
        if cur is not None and not cur["skip"]:
            n_open += 1
    return res, n_open


def analyze(fills):
    lcs, n_open = lifecycles(fills)
    if not lcs:
        return None
    H = 3600000
    wins = [l for l in lcs if l["pnl"] > 0]
    loss = [l for l in lcs if l["pnl"] <= 0]
    dur = lambda l: l["t1"] - l["t0"]
    avg = lambda a: sum(a) / len(a) if a else 0.0
    aw, al = avg([l["pnl"] for l in wins]), avg([-l["pnl"] for l in loss])
    sides = {s: {"n": 0, "w": 0} for s in ("long", "short")}
    for l in lcs:
        sides[l["side"]]["n"] += 1
        sides[l["side"]]["w"] += 1 if l["pnl"] > 0 else 0
    buckets = []
    for label, lo, hi in (("کمتر از ۶ ساعت", 0, 6 * H), ("۶ ساعت تا ۲ روز", 6 * H, 48 * H), ("بیش از ۲ روز", 48 * H, 1e18)):
        sel = [l for l in lcs if lo <= dur(l) < hi]
        buckets.append({"label": label, "n": len(sel), "w": sum(1 for l in sel if l["pnl"] > 0)})
    losses = sorted([-l["pnl"] for l in loss], reverse=True)
    tot_l = sum(losses)
    coins = {}
    for l in lcs:
        for k, side in ((l["coin"] + "|" + l["side"], l["side"]), (l["coin"] + "|*", "*")):
            c = coins.setdefault(k, {"n": 0, "w": 0, "pnl": 0.0, "coin": l["coin"], "side": side})
            c["n"] += 1
            c["w"] += 1 if l["pnl"] > 0 else 0
            c["pnl"] += l["pnl"]
    last = max(int(f["time"]) for f in fills if not is_spot(f.get("coin", "")))  # any fill, not only closes
    summary = {"closed": len(lcs), "wins": len(wins), "winRate": 100.0 * len(wins) / len(lcs), "totalPnl": sum(l["pnl"] for l in lcs),
               "biggestWin": max([l["pnl"] for l in wins], default=0.0), "biggestLoss": min([l["pnl"] for l in loss], default=0.0),
               "liquidations": sum(1 for l in lcs if l["liq"]), "avgDurationMs": avg([dur(l) for l in lcs]), "open": n_open,
               "lastActivityAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last / 1000))}
    prof = {"n": len(lcs), "wins": len(wins), "losses": len(loss), "lossRate": len(loss) / len(lcs), "avgWin": aw, "avgLoss": al,
            "payoff": aw / al if al else 0.0, "winDur": avg([dur(l) for l in wins]), "lossDur": avg([dur(l) for l in loss]),
            "sides": sides, "buckets": buckets, "top3Share": sum(losses[:3]) / tot_l if tot_l else 0.0}
    return {"sum": summary, "prof": prof, "coins": coins}


def refresh_history(hist, cfg, now):
    ws = hist.setdefault("wallets", {})
    due = [w for w in cfg["wallets"] if w.get("hist", True) and now - ws.get(w["addr"], {}).get("at", 0) > HIST_TTL]
    due.sort(key=lambda w: ws.get(w["addr"], {}).get("at", 0))
    changed = False
    t0 = time.time()
    for w in due[:HIST_PER_RUN]:
        if time.time() - t0 > 150:  # time budget; the rest continues next run
            break
        try:
            res = analyze(fetch_history(w["addr"], now)) or {"empty": True}
            ws[w["addr"]] = {"at": now, **res}
            changed = True
            print("history", w["tag"], "closed", (res.get("sum") or {}).get("closed", 0))
        except Exception as e:
            print("history failed", w["tag"], e, file=sys.stderr)
    return changed


def fetch_orders(addr):
    """Whale's open stop / take-profit style orders (public). Only reduce-only or trigger orders on perps."""
    out = []
    for o in post({"type": "frontendOpenOrders", "user": addr}):
        c = o.get("coin", "")
        if is_spot(c) or not (o.get("isTrigger") or o.get("reduceOnly") or o.get("isPositionTpsl")):
            continue
        try:
            px = float(o.get("triggerPx") if o.get("isTrigger") and float(o.get("triggerPx") or 0) > 0 else o.get("limitPx"))
        except Exception:
            continue
        out.append({"coin": c, "side": o.get("side"), "px": px, "type": o.get("orderType", ""), "sz": float(o.get("sz", 0) or 0),
                    "ts": int(o.get("timestamp", 0) or 0)})
    return out[:40]


def make_groups(tag, addr, fills, now):
    """Same-coin, same-direction fills less than GAP apart become one 'move' (feed card)."""
    open_g, groups = {}, []
    for f in sorted(fills, key=lambda f: int(f["time"])):
        coin = f.get("coin", "")
        if is_spot(coin) or not f.get("dir"):
            continue
        t, px, sz = int(f["time"]), float(f["px"]), float(f["sz"])
        k = coin + "|" + f["dir"]
        g = open_g.get(k)
        if not g or t - g["last"] > GAP:
            g = {"tag": tag, "addr": addr, "coin": coin, "dir": f["dir"], "side": "BUY" if f.get("side") == "B" else "SELL",
                 "first": t, "last": t, "usd": 0.0, "size": 0.0, "n": 0, "pnl": 0.0}
            open_g[k] = g
            groups.append(g)
        g["usd"] += px * sz
        g["size"] += sz
        g["n"] += 1
        g["pnl"] += float(f.get("closedPnl", 0) or 0)
        if t >= g["last"]:
            g["last"] = t
            g["lf"] = {"start": float(f.get("startPosition", 0) or 0), "side": f.get("side"), "sz": sz, "px": px}
        g["id"] = f"{tag}|{coin}|{g['dir']}|{g['first']}"
    groups = [g for g in groups if g["last"] >= now - FEED_WINDOW]
    return groups[-FEED_MAX:]


def build_data(st, cfg, marks, now, hist, feeds, prev, ords, fat):
    opens, upl = [], 0.0
    for p in st["open"].values():
        m = marks.get(p["coin"])
        u = unreal(p, m) if m else None
        if u is not None:
            upl += u
        opens.append({**p, "mark": m, "upnl": u})
    need = {p["coin"] for p in st["open"].values()} | {"PAXG"}
    wallets = []
    old = {w["addr"]: w for w in (prev or {}).get("wallets", [])}
    for w in cfg["wallets"]:
        addr, h = w["addr"], hist.get("wallets", {}).get(w["addr"], {})
        feed = feeds.get(addr)
        entry = {"tag": w["tag"], "addr": addr, "group": w.get("group", ""), "status": "ok" if feed is not None else "error", "at": now,
                 "feed_at": fat.get(addr), "orders": ords.get(addr) if ords.get(addr) is not None else old.get(addr, {}).get("orders", []),
                 "groups": feed if feed is not None else old.get(addr, {}).get("groups", []),
                 "hist_at": h.get("at"), "sum": h.get("sum"), "prof": h.get("prof"), "coins": h.get("coins", {})}
        if feed is None:
            entry["at"] = old.get(addr, {}).get("at", now)
        for g in entry["groups"]:
            need.add(g["coin"])
        wallets.append(entry)
    return {"updated": now, "started": st.get("started", now), "settings": cfg["settings"], "start": cfg["settings"]["start_balance"],
            "balance": st["balance"], "equity": st["balance"] + upl, "open": opens, "closed": st["closed"][-60:][::-1],
            "events": st["events"][-60:][::-1], "marks": {c: marks[c] for c in need if c in marks}, "wallets": wallets}


def main():
    cfg = load(CFG_F, None)
    st = load(STATE_F, None) or {"balance": cfg["settings"]["start_balance"], "open": {}, "closed": [], "pending": {}, "last_seen": {}, "events": [], "data_ts": 0}
    st["dirty"] = False
    now = int(time.time() * 1000)
    if "started" not in st:
        st["started"] = now
        st["dirty"] = True
    hist = load(HIST_F, {"wallets": {}})
    prev = load(DATA_F, None)
    marks = marks_now()
    feeds, ords, fat, nfull = {}, {}, {}, 0
    oldw = {w["addr"]: w for w in (prev or {}).get("wallets", [])}
    for w in cfg["wallets"]:
        addr, ow = w["addr"], oldw.get(w["addr"], {})
        try:
            last = st["last_seen"].get(addr)
            fresh = None
            if w.get("copy", True) and last is not None:  # cheap check (weight ~20): only fills newer than the last one seen
                fresh = post({"type": "userFillsByTime", "user": addr, "startTime": int(last) + 1})
                if fresh:
                    process_wallet(st, cfg, w, fresh, marks, now)
            due = (last is None and w.get("copy", True)) or ow.get("feed_at") is None or now - ow.get("feed_at", 0) > FULL_EVERY
            if due and not fresh and last is not None and nfull >= FULL_PER_RUN:
                due = False  # spread the heavy requests over several loops to stay under the API weight limit
            if w.get("copy", True):
                try:
                    reconcile(st, cfg, w, marks, now)
                except Exception as e:
                    print("reconcile failed", w["tag"], e, file=sys.stderr)
            need_full = bool(fresh) or due
            nfull += 1 if need_full else 0
            if need_full:  # full fills + open orders: heavy, so only every few minutes or when something new happened
                fills = post({"type": "userFills", "user": addr})
                if w.get("copy", True) and last is None:
                    process_wallet(st, cfg, w, fills, marks, now)  # first run: baseline only
                feeds[addr] = make_groups(w["tag"], addr, fills, now)
                fat[addr] = now
                try:
                    ords[addr] = fetch_orders(addr)
                except Exception as e:
                    print("orders failed", w["tag"], e, file=sys.stderr)
            else:
                feeds[addr] = ow.get("groups", [])
                fat[addr] = ow.get("feed_at", now)
        except Exception as e:
            print("wallet failed", w["tag"], e, file=sys.stderr)
    for key, p in list(st["open"].items()):
        m = marks.get(p["coin"])
        if m and unreal(p, m) <= -0.9 * p["margin"]:
            close_paper(st, key, m, now, "liq")
    if refresh_history(hist, cfg, now):
        with open(HIST_F, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False)
    data = build_data(st, cfg, marks, now, hist, feeds, prev, ords, fat)
    def core(d):
        d = {k: v for k, v in (d or {}).items() if k not in ("updated", "marks")}
        d["wallets"] = [{k: v for k, v in w.items() if k not in ("at", "feed_at")} for w in d.get("wallets", [])]
        return json.dumps(d, sort_keys=True, ensure_ascii=False)
    if core(data) != core(prev) or now - (prev or {}).get("updated", 0) > DATA_HEARTBEAT_S * 1000:
        os.makedirs(os.path.dirname(DATA_F), exist_ok=True)
        with open(DATA_F, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    if st.pop("dirty", False) or now - st.get("data_ts", 0) > HEARTBEAT_S * 1000:
        st["data_ts"] = now
        with open(STATE_F, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    print("done", "balance", round(st["balance"], 2), "open", len(st["open"]))


if __name__ == "__main__":
    main()
