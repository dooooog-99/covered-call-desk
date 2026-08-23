# -*- coding: utf-8 -*-
"""靜態檔 + 延遲選擇權鏈 API（本機／免費雲端共用）"""
from __future__ import annotations

import math
import os
import time
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "5174"))
CACHE_TTL_SEC = int(os.environ.get("CHAIN_CACHE_TTL", "300"))

_chain_cache: dict[str, tuple[float, dict]] = {}

app = Flask(__name__, static_folder=str(ROOT), static_url_path="")


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_call_delta(S: float, K: float, T: float, sigma: float, r: float = 0.04) -> float:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 1.0 if S > K else 0.0
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    return max(0.01, min(0.99, _norm_cdf(d1)))


def fetch_chain(symbol: str) -> dict:
    import yfinance as yf

    symbol = symbol.strip().upper()
    t = yf.Ticker(symbol)

    price = None
    try:
        price = float(t.fast_info["last_price"])
    except Exception:
        pass
    if not price:
        info = t.info or {}
        price = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
    if not price:
        raise RuntimeError("無法取得現價")

    prev = None
    try:
        prev = float(t.fast_info.get("previous_close") or 0) or None
    except Exception:
        prev = None
    chg = ((price - prev) / prev * 100.0) if prev else 0.0

    expiries_raw = list(t.options or [])
    if not expiries_raw:
        raise RuntimeError("此代號沒有選擇權鏈（或來源暫不可用）")

    today = date.today()
    parsed = []
    for e in expiries_raw:
        try:
            d = datetime.strptime(e, "%Y-%m-%d").date()
        except ValueError:
            continue
        dte = (d - today).days
        if 7 <= dte <= 90:
            parsed.append((e, dte))
    if not parsed:
        for e in expiries_raw[:8]:
            try:
                d = datetime.strptime(e, "%Y-%m-%d").date()
                parsed.append((e, max(1, (d - today).days)))
            except ValueError:
                continue
    parsed = parsed[:6]

    calls = []
    ivs = []
    for exp, dte in parsed:
        try:
            chain = t.option_chain(exp)
        except Exception:
            continue
        df = chain.calls
        if df is None or df.empty:
            continue
        T = max(dte, 1) / 365.0
        for _, row in df.iterrows():
            try:
                K = float(row.get("strike"))
            except Exception:
                continue
            bid = float(row["bid"]) if row.get("bid") == row.get("bid") else 0.0
            ask = float(row["ask"]) if row.get("ask") == row.get("ask") else 0.0
            last = float(row["lastPrice"]) if row.get("lastPrice") == row.get("lastPrice") else 0.0
            if bid > 0 and ask > 0:
                premium = (bid + ask) / 2.0
            elif last > 0:
                premium = last
            elif ask > 0:
                premium = ask
            else:
                premium = max(last, 0.01)

            iv = float(row["impliedVolatility"]) if row.get("impliedVolatility") == row.get("impliedVolatility") else 0.0
            if iv and iv > 0:
                ivs.append(iv)
                delta = _bs_call_delta(price, K, T, iv)
            else:
                m = (K - price) / price
                delta = max(0.02, min(0.95, 0.5 - m * 2.2))

            oi = int(row["openInterest"]) if row.get("openInterest") == row.get("openInterest") else 0
            vol = int(row["volume"]) if row.get("volume") == row.get("volume") else 0
            otm = (K - price) / price * 100.0
            prem_pct = premium / price * 100.0
            calls.append(
                {
                    "expiry": exp,
                    "dte": dte,
                    "strike": round(K, 2),
                    "premium": round(premium, 2),
                    "delta": round(float(delta), 3),
                    "oi": oi,
                    "volume": vol,
                    "otmPct": round(otm, 2),
                    "premPct": round(prem_pct, 2),
                    "iv": round(iv * 100.0, 1) if iv else None,
                }
            )

    if not calls:
        raise RuntimeError("選擇權資料為空")

    strikes = sorted({c["strike"] for c in calls})
    lo, hi = price * 0.85, price * 1.25
    strikes = [k for k in strikes if lo <= k <= hi]
    strike_set = set(strikes)
    calls = [c for c in calls if c["strike"] in strike_set]

    atm_iv = None
    if ivs:
        atm_iv = round(sorted(ivs)[len(ivs) // 2] * 100.0, 1)

    levels = [
        {"role": "偏樂觀參考（+20%）", "price": round(price * 1.20, 2)},
        {"role": "壓力參考（+12%）", "price": round(price * 1.12, 2)},
        {"role": "現價", "price": round(price, 2)},
        {"role": "支撐參考（-10%）", "price": round(price * 0.90, 2)},
    ]

    return {
        "symbol": symbol,
        "source": "yahoo-delayed",
        "asOf": datetime.now(timezone.utc).isoformat(),
        "quote": {
            "price": round(price, 2),
            "changePct": round(chg, 2),
            "iv": atm_iv if atm_iv is not None else 0.0,
            "ivChg": 0.0,
        },
        "expiries": [{"date": e, "dte": d} for e, d in parsed],
        "strikes": strikes,
        "calls": calls,
        "levels": levels,
        "note": "延遲報價，僅供個人觀察；Delta 由隱含波動粗估。免費雲端有快取，約數分鐘更新一次。",
    }


def get_chain_cached(symbol: str) -> dict:
    key = symbol.strip().upper()
    now = time.time()
    hit = _chain_cache.get(key)
    if hit and now - hit[0] < CACHE_TTL_SEC:
        payload = dict(hit[1])
        payload["cached"] = True
        return payload
    payload = fetch_chain(key)
    _chain_cache[key] = (now, payload)
    out = dict(payload)
    out["cached"] = False
    return out


@app.get("/api/health")
def api_health():
    return jsonify({"ok": True})


@app.get("/api/chain")
def api_chain():
    symbol = (request.args.get("symbol") or "TSLA").strip() or "TSLA"
    try:
        return jsonify(get_chain_cached(symbol))
    except Exception as e:
        return (
            jsonify({"error": str(e), "detail": traceback.format_exc(limit=3)}),
            502,
        )


@app.get("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.get("/<path:path>")
def static_files(path: str):
    # 避免把 /api 誤當靜態
    if path.startswith("api/"):
        return jsonify({"error": "not found"}), 404
    target = ROOT / path
    if target.is_file():
        return send_from_directory(ROOT, path)
    return send_from_directory(ROOT, "index.html")


def main():
    try:
        import yfinance  # noqa: F401
    except ImportError:
        print("缺少 yfinance，請先執行: pip install -r requirements.txt", flush=True)
        raise SystemExit(1)

    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    print("Covered Call 觀察站已啟動", flush=True)
    print("請用瀏覽器打開: http://127.0.0.1:%d/" % PORT, flush=True)
    print("API: http://127.0.0.1:%d/api/chain?symbol=TSLA" % PORT, flush=True)
    print("請保持此視窗開著。關閉即停止。", flush=True)
    app.run(host=host, port=PORT, threaded=True)


if __name__ == "__main__":
    main()
