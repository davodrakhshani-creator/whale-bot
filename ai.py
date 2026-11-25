"""AI entry analyst for the whale paper-copy bot (stdlib only).

Before the bot copies a whale's new position it builds a compact evidence pack (multi-timeframe technicals from Hyperliquid candles,
funding / open interest, BTC regime, the whale's own track record, our portfolio) and asks an LLM for ENTER / SKIP.
Providers (first one with a key wins): ANTHROPIC_API_KEY (Claude), OPENROUTER_API_KEY.
No key / any failure -> returns None and the bot copies as usual (fail-open). Read-only, no exchange access.
"""
import json
import math
import os
import re
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
KNOWLEDGE = os.path.join(HERE, "ai_knowledge.md")
TFS = {"15m": (900000, 120), "1h": (3600000, 160), "4h": (14400000, 160), "1d": (86400000, 100)}
_cache = {"btc": (0, None), "ctx": {}}


# ---------------------------------------------------------------- indicators
def ema(v, n):
    k, out, p = 2 / (n + 1), [], None
    for x in v:
        p = x if p is None else x * k + p * (1 - k)
        out.append(p)
    return out


def rsi(c, n=14):
    if len(c) <= n:
        return None
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    g, l = g / n, l / n
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def atr(cs, n=14):
    if len(cs) <= n:
        return None
    trs = [max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - cs[i - 1]["c"]), abs(cs[i]["l"] - cs[i - 1]["c"])) for i in range(1, len(cs))]
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def macd_hist(c):
    if len(c) < 35:
        return None, None
    m = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]
    s = ema(m, 9)
    h = [a - b for a, b in zip(m, s)]
    return h[-1], h[-1] - h[-4]


def boll(c, n=20):
    if len(c) < n:
        return None, None
    w = c[-n:]
    mu = sum(w) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in w) / n)
    if sd == 0:
        return None, None
    return (c[-1] - (mu - 2 * sd)) / (4 * sd), 4 * sd / mu * 100  # %B, bandwidth %


def swings(cs, k=3):
    hi, lo = [], []
    for i in range(k, len(cs) - k):
        if all(cs[i]["h"] >= cs[j]["h"] for j in range(i - k, i + k + 1)):
            hi.append(cs[i]["h"])
        if all(cs[i]["l"] <= cs[j]["l"] for j in range(i - k, i + k + 1)):
            lo.append(cs[i]["l"])
    return hi, lo


def structure(hi, lo):
    if len(hi) >= 2 and len(lo) >= 2:
        if hi[-1] > hi[-2] and lo[-1] > lo[-2]:
            return "higher highs/higher lows"
        if hi[-1] < hi[-2] and lo[-1] < lo[-2]:
            return "lower highs/lower lows"
    return "mixed"


def r(x, d=4):
    if x is None:
        return None
    return float(f"{x:.{d + 2}g}") if abs(x) < 1 else round(x, d if abs(x) < 100 else 2)


def tf_pack(cs, price):
    if len(cs) < 30:
        return {"candles": len(cs), "note": "not enough candles"}
    cl = [c["c"] for c in cs]
    e20, e50 = ema(cl, 20), ema(cl, 50)
    e200 = ema(cl, 200)[-1] if len(cl) >= 200 else None
    a = atr(cs)
    mh, mslope = macd_hist(cl)
    pb, bw = boll(cl)
    hi, lo = swings(cs)
    above = [h for h in hi if h > price]
    below = [l for l in lo if l < price]
    vol5, vol20 = sum(c["v"] for c in cs[-5:]) / 5, sum(c["v"] for c in cs[-25:-5]) / 20
    out = {"close": r(cl[-1]), "ema20": r(e20[-1]), "ema50": r(e50[-1]), "ema200": r(e200) if e200 else None,
           "price_vs_ema20_atr": r((price - e20[-1]) / a, 2) if a else None,
           "ema_trend": "up" if e20[-1] > e50[-1] and price > e50[-1] else "down" if e20[-1] < e50[-1] and price < e50[-1] else "mixed",
           "rsi14": r(rsi(cl), 1), "macd_hist": r(mh), "macd_hist_change_3bars": r(mslope),
           "atr": r(a), "atr_pct": r(100 * a / price, 2) if a else None,
           "boll_pctB": r(pb, 2), "boll_bandwidth_pct": r(bw, 2),
           "structure": structure(hi, lo),
           "nearest_resistance": r(min(above)) if above else None, "nearest_support": r(max(below)) if below else None,
           "volume_ratio_5_vs_20": r(vol5 / vol20, 2) if vol20 else None}
    n = len(cl)
    for lbl, back in (("chg_5bars_pct", 5), ("chg_20bars_pct", 20)):
        if n > back:
            out[lbl] = r(100 * (cl[-1] / cl[-1 - back] - 1), 2)
    return out


# ---------------------------------------------------------------- data
def get_candles(post, coin, tf, now):
    ms, n = TFS[tf]
    a = post({"type": "candleSnapshot", "req": {"coin": coin, "interval": tf, "startTime": now - ms * n, "endTime": now}})
    return [{"t": int(x["t"]), "o": float(x["o"]), "h": float(x["h"]), "l": float(x["l"]), "c": float(x["c"]), "v": float(x["v"])} for x in a]


def asset_ctx(post, coin, now):
    dex = coin.split(":")[0] if ":" in coin else None
    hit = _cache["ctx"].get(dex)
    if not hit or now - hit[0] > 60000:
        q = {"type": "metaAndAssetCtxs"}
        if dex:
            q["dex"] = dex
        meta, ctxs = post(q)
        _cache["ctx"][dex] = (now, {u["name"]: c for u, c in zip(meta["universe"], ctxs)})
        hit = _cache["ctx"][dex]
    c = hit[1].get(coin)
    if not c:
        return None
    mark = float(c.get("markPx") or 0)
    prev = float(c.get("prevDayPx") or 0)
    f = float(c.get("funding") or 0)
    return {"funding_hourly_pct": r(f * 100, 5), "funding_annualized_pct": r(f * 24 * 365 * 100, 1),
            "open_interest_usd": r(float(c.get("openInterest") or 0) * mark, 0), "volume_24h_usd": r(float(c.get("dayNtlVlm") or 0), 0),
            "premium_pct": r(float(c.get("premium") or 0) * 100, 4), "change_24h_pct": r(100 * (mark / prev - 1), 2) if prev else None}


def btc_regime(post, now):
    t, v = _cache["btc"]
    if v and now - t < 10 * 60000:
        return v
    out = {}
    for tf in ("1h", "4h", "1d"):
        cs = get_candles(post, "BTC", tf, now)
        out[tf] = tf_pack(cs, cs[-1]["c"])
    d = get_candles(post, "BTC", "1d", now)
    if len(d) > 7:
        out["change_7d_pct"] = r(100 * (d[-1]["c"] / d[-8]["c"] - 1), 2)
    _cache["btc"] = (now, out)
    return out


def whale_pack(hist_w, coin, side):
    if not hist_w or not hist_w.get("prof"):
        return {"note": "no history available"}
    p = hist_w["prof"]
    wr = 1 - p["lossRate"]
    exp = wr * p["avgWin"] - p["lossRate"] * p["avgLoss"]
    out = {"closed_positions": p["n"], "win_rate_pct": r(100 * wr, 1), "avg_win_usd": r(p["avgWin"], 0), "avg_loss_usd": r(p["avgLoss"], 0),
           "expectancy_per_position_usd": r(exp, 0), "sample_reliable": p["n"] >= 30,
           "same_side_overall": {"n": p["sides"][side]["n"], "wins": p["sides"][side]["w"]}}
    cs = (hist_w.get("coins") or {}).get(f"{coin}|{side}")
    if cs:
        out["same_coin_and_side"] = {"n": cs["n"], "wins": cs["w"], "pnl_usd": r(cs["pnl"], 0)}
    s = hist_w.get("sum") or {}
    for k in ("avgHoldMs", "avgDurationMs"):
        if s.get(k):
            out["avg_hold_hours"] = r(s[k] / 3600000, 1)
    return out


def portfolio_pack(st, marks, unreal_fn, addr, coin, side):
    eq = st["balance"]
    longs = shorts = 0
    n_long = n_short = 0
    same_coin = []
    mine = 0
    for p in st["open"].values():
        mk = marks.get(p["coin"], p["entry"])
        eq += unreal_fn(p, mk)
        notional = p["margin"] * p["lev"]
        if p["side"] == "long":
            longs += notional
            n_long += 1
        else:
            shorts += notional
            n_short += 1
        if (p.get("src") or p["coin"]) == coin:
            same_coin.append({"whale": p["tag"], "side": p["side"]})
        if p["key"].startswith(addr + "|"):
            mine += 1
    return {"equity_usd": r(eq, 0), "open_positions": len(st["open"]), "open_longs": n_long, "open_shorts": n_short,
            "net_notional_usd": r(longs - shorts, 0), "gross_notional_usd": r(longs + shorts, 0),
            "copies_open_from_this_whale": mine, "other_open_copies_same_coin": same_coin[:6]}


def build_pack(post, st, cfg, hist_w, marks, unreal_fn, w, coin, side, avg_px, usd, now):
    mk = marks.get("PAXG") if "gold" in coin.lower() else marks.get(coin)
    mark = mk or avg_px
    pack = {"time_utc": time.strftime("%Y-%m-%d %H:%M (%a)", time.gmtime(now / 1000)),
            "signal": {"whale": w["tag"], "coin": coin, "side": side, "whale_entry_usd": r(usd, 0), "whale_avg_entry_price": r(avg_px),
                       "current_mark": r(mark),
                       "adverse_move_since_whale_entry_pct": r(100 * ((mark - avg_px) / avg_px) * (1 if side == "long" else -1), 3)},
            "our_trade": {"margin_usd": cfg["settings"]["margin"], "leverage": cfg["settings"]["leverage"],
                          "hard_stop_pct_of_margin": cfg["settings"].get("stop_pct", 0.30), "round_trip_cost_pct_of_margin": 0.75}}
    pack["whale_track_record"] = whale_pack(hist_w, coin, side)
    pack["portfolio"] = portfolio_pack(st, marks, unreal_fn, w["addr"], coin, side)
    errs = []
    try:
        pack["derivatives"] = asset_ctx(post, coin, now)
    except Exception as e:
        errs.append(f"derivatives:{e}")
    pack["technicals"] = {}
    for tf in TFS:
        try:
            pack["technicals"][tf] = tf_pack(get_candles(post, coin, tf, now), mark)
        except Exception as e:
            errs.append(f"{tf}:{e}")
    try:
        pack["btc_regime"] = btc_regime(post, now)
    except Exception as e:
        errs.append(f"btc:{e}")
    if errs:
        pack["data_errors"] = errs[:5]
    return pack


# ---------------------------------------------------------------- LLM
def knowledge():
    with open(KNOWLEDGE, encoding="utf-8") as f:
        return f.read()


def http_json(url, headers, body, timeout=75):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def provider(cfg):
    a = cfg["settings"].get("ai", {})
    want = a.get("provider", "auto")
    if os.environ.get("ANTHROPIC_API_KEY") and want in ("auto", "anthropic"):
        return "anthropic"
    if os.environ.get("OPENROUTER_API_KEY") and want in ("auto", "openrouter"):
        return "openrouter"
    return None


def call_llm(cfg, system, user):
    a = cfg["settings"].get("ai", {})
    prov = provider(cfg)
    for attempt in (1, 2):
        try:
            if prov == "anthropic":
                model = a.get("model", "claude-sonnet-5-5")
                d = http_json("https://api.anthropic.com/v1/messages", {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
                              {"model": model, "max_tokens": 1000, "temperature": 0.2, "system": system, "messages": [{"role": "user", "content": user}]})
                return model, "".join(b.get("text", "") for b in d.get("content", []))
            model = a.get("openrouter_model", "meta-llama/llama-3.3-70b-instruct:free")
            d = http_json("https://openrouter.ai/api/v1/chat/completions", {"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"]},
                          {"model": model, "max_tokens": 1000, "temperature": 0.2, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
            return model, d["choices"][0]["message"]["content"]
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(3)


def parse(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in reply")
    d = json.loads(m.group(0))
    dec = str(d.get("decision", "")).lower().strip()
    if dec not in ("enter", "skip"):
        raise ValueError("bad decision")
    def num(x, lo, hi, default=None):
        try:
            return max(lo, min(hi, float(x)))
        except Exception:
            return default
    sp = num(d.get("stop_pct"), 0.15, 0.30)
    return {"decision": dec, "win_prob": num(d.get("win_prob"), 0, 100), "confidence": num(d.get("confidence"), 0, 100), "stop_pct": sp,
            "summary": str(d.get("summary_fa", ""))[:400], "reasons": [str(x)[:220] for x in (d.get("reasons_fa") or [])][:6],
            "red_flags": [str(x)[:220] for x in (d.get("red_flags_fa") or [])][:5], "regime": str(d.get("regime", ""))[:12]}


def analyze(post, st, cfg, hist_w, marks, unreal_fn, w, coin, side, avg_px, usd, now):
    """Returns a decision dict, or None when AI is off/unavailable (caller then copies as usual)."""
    a = cfg["settings"].get("ai", {})
    if not a.get("enabled", True) or not provider(cfg):
        return None
    day = time.strftime("%Y-%m-%d", time.gmtime(now / 1000))
    b = st.setdefault("ai_budget", {"day": day, "n": 0})
    if b["day"] != day:
        b["day"], b["n"] = day, 0
    if b["n"] >= a.get("daily_cap", 120):
        return {"unavailable": "سقف روزانه‌ی تحلیل AI پر شد"}
    t0 = time.time()
    try:
        pack = build_pack(post, st, cfg, hist_w, marks, unreal_fn, w, coin, side, avg_px, usd, now)
        b["n"] += 1
        model, text = call_llm(cfg, knowledge(), "Evidence pack (JSON). Decide ENTER or SKIP per the instructions.\n" + json.dumps(pack, ensure_ascii=False, separators=(",", ":")))
        d = parse(text)
        d["model"] = model
        d["ms"] = int((time.time() - t0) * 1000)
        return d
    except Exception as e:
        return {"unavailable": f"{type(e).__name__}: {str(e)[:160]}"}
