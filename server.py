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
CACHE_TTL_SEC = int(os.environ.get("CHAIN_CACHE_TTL", "600"))

_chain_cache: dict[str, tuple[float, dict]] = {}
_yahoo_session = None
_yahoo_crumb = None
_yahoo_crumb_at = 0.0

app = Flask(__name__, static_folder=str(ROOT), static_url_path="")


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_call_delta(S: float, K: float, T: float, sigma: float, r: float = 0.04) -> float:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 1.0 if S > K else 0.0
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    return max(0.01, min(0.99, _norm_cdf(d1)))


def _yahoo_client():
    """用 curl_cffi 模擬瀏覽器，降低 Yahoo 對雲端 IP 的封鎖。"""
    global _yahoo_session, _yahoo_crumb, _yahoo_crumb_at
    from curl_cffi import requests as creq

    now = time.time()
    if _yahoo_session is None or not _yahoo_crumb or now - _yahoo_crumb_at > 1800:
        s = creq.Session(impersonate="chrome")
        s.get("https://fc.yahoo.com", timeout=20)
        crumb_resp = s.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=20)
        crumb_resp.raise_for_status()
        crumb = (crumb_resp.text or "").strip()
        if not crumb or "Too Many Requests" in crumb:
            raise RuntimeError("Yahoo crumb 取得失敗（可能限流）")
        _yahoo_session = s
        _yahoo_crumb = crumb
        _yahoo_crumb_at = now
    return _yahoo_session, _yahoo_crumb


def _yahoo_get_json(url: str, retries: int = 3) -> dict:
    last_err: Exception | None = None
    for i in range(retries):
        try:
            s, crumb = _yahoo_client()
            sep = "&" if "?" in url else "?"
            full = f"{url}{sep}crumb={crumb}"
            r = s.get(full, timeout=40)
            if r.status_code in (401, 403):
                # crumb 失效，強制重抓
                global _yahoo_session, _yahoo_crumb, _yahoo_crumb_at
                _yahoo_session = None
                _yahoo_crumb = None
                _yahoo_crumb_at = 0.0
                last_err = RuntimeError(f"Yahoo HTTP {r.status_code}")
                time.sleep(1.0 + i)
                continue
            if r.status_code == 429:
                last_err = RuntimeError("Yahoo 限流 429")
                time.sleep(2.0 + i * 2)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(1.0 + i)
    raise RuntimeError(f"Yahoo 請求失敗: {last_err}")


def _parse_calls_from_yahoo(symbol: str, data: dict) -> dict:
    results = (data.get("optionChain") or {}).get("result") or []
    if not results:
        err = (data.get("optionChain") or {}).get("error")
        raise RuntimeError(f"Yahoo 無選擇權資料: {err or 'empty'}")
    res = results[0]
    quote = res.get("quote") or {}
    price = float(
        quote.get("regularMarketPrice")
        or quote.get("postMarketPrice")
        or quote.get("preMarketPrice")
        or 0
    )
    if not price:
        raise RuntimeError("無法取得現價")
    prev = float(quote.get("regularMarketPreviousClose") or 0) or None
    chg = ((price - prev) / prev * 100.0) if prev else float(quote.get("regularMarketChangePercent") or 0)

    exp_ts_list = list(res.get("expirationDates") or [])
    today = date.today()
    parsed: list[tuple[str, int, int]] = []  # (YYYY-MM-DD, dte, ts)
    for ts in exp_ts_list:
        try:
            d = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
        except Exception:
            continue
        dte = (d - today).days
        if 7 <= dte <= 90:
            parsed.append((d.isoformat(), dte, int(ts)))
    if not parsed:
        for ts in exp_ts_list[:8]:
            try:
                d = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
                parsed.append((d.isoformat(), max(1, (d - today).days), int(ts)))
            except Exception:
                continue
    parsed = parsed[:6]
    if not parsed:
        raise RuntimeError("沒有可用的到期日")

    calls = []
    ivs = []
    # 第一包已含最近到期；其餘再依 date 抓
    by_ts = {}
    first_opts = res.get("options") or []
    if first_opts:
        by_ts[int(first_opts[0].get("expirationDate") or 0)] = first_opts[0]

    for exp, dte, ts in parsed:
        opt = by_ts.get(ts)
        if opt is None:
            try:
                more = _yahoo_get_json(
                    f"https://query1.finance.yahoo.com/v7/finance/options/{symbol}?date={ts}"
                )
                more_res = ((more.get("optionChain") or {}).get("result") or [None])[0]
                if more_res and more_res.get("options"):
                    opt = more_res["options"][0]
                    time.sleep(0.35)  # 禮貌間隔，降低限流
            except Exception:
                continue
        if not opt:
            continue
        T = max(dte, 1) / 365.0
        for row in opt.get("calls") or []:
            try:
                K = float(row.get("strike"))
            except Exception:
                continue
            bid = float(row.get("bid") or 0)
            ask = float(row.get("ask") or 0)
            last = float(row.get("lastPrice") or 0)
            if bid > 0 and ask > 0:
                premium = (bid + ask) / 2.0
            elif last > 0:
                premium = last
            elif ask > 0:
                premium = ask
            else:
                premium = max(last, 0.01)

            iv = float(row.get("impliedVolatility") or 0)
            if iv and iv > 0:
                ivs.append(iv)
                delta = _bs_call_delta(price, K, T, iv)
            else:
                m = (K - price) / price
                delta = max(0.02, min(0.95, 0.5 - m * 2.2))

            oi = int(row.get("openInterest") or 0)
            vol = int(row.get("volume") or 0)
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
        "expiries": [{"date": e, "dte": d} for e, d, _ in parsed],
        "strikes": strikes,
        "calls": calls,
        "levels": levels,
        "note": "延遲報價，僅供個人觀察；Delta 由隱含波動粗估。雲端有快取，約數分鐘更新一次。",
    }


def fetch_chain(symbol: str) -> dict:
    symbol = symbol.strip().upper()
    data = _yahoo_get_json(f"https://query1.finance.yahoo.com/v7/finance/options/{symbol}")
    return _parse_calls_from_yahoo(symbol, data)


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
    if path.startswith("api/"):
        return jsonify({"error": "not found"}), 404
    target = ROOT / path
    if target.is_file():
        return send_from_directory(ROOT, path)
    return send_from_directory(ROOT, "index.html")


def main():
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    print("Covered Call 觀察站已啟動", flush=True)
    print("請用瀏覽器打開: http://127.0.0.1:%d/" % PORT, flush=True)
    print("API: http://127.0.0.1:%d/api/chain?symbol=TSLA" % PORT, flush=True)
    print("請保持此視窗開著。關閉即停止。", flush=True)
    app.run(host=host, port=PORT, threaded=True)


if __name__ == "__main__":
    main()
