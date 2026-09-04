#!/usr/bin/env python3
"""Daily market data refresh for the S&P 500 directory site.

Reads data.json, refreshes it from Massive/Polygon.io + CoinGecko + Wikipedia,
and writes data.json back in place. Run by .github/workflows/update-data.yml
on a daily schedule; the workflow commits whatever this script writes.
"""
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

API_KEY = os.environ["POLYGON_API_KEY"]
BASE = "https://api.polygon.io"
DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data.json")

STABLECOIN_SYMBOLS = {
    "USDT", "USDC", "DAI", "USDS", "USD1", "USDE", "PYUSD", "USDF", "USDG",
    "USDGO", "USTB", "USYC", "RLUSD", "GHO", "BUIDL", "JAAA", "EURSAFO",
    "EUTBL", "FDUSD", "BFUSD", "TUSD", "USDD", "USD0",
}


def log(msg):
    print(msg, flush=True)


def http_get_json(url, retries=6, base_sleep=1.5):
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": "sp500-directory-bot/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                time.sleep(base_sleep * (attempt + 1))
                continue
            if e.code >= 500:
                time.sleep(base_sleep)
                continue
            raise
        except Exception as e:
            last_err = e
            time.sleep(base_sleep)
    raise last_err


def wilder_rsi(closes, period=14):
    n = len(closes)
    rsis = [None] * n
    if n < period + 1:
        return rsis
    gains, losses = [], []
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    rsis[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, n):
        ch = closes[i] - closes[i - 1]
        gain = max(ch, 0.0)
        loss = max(-ch, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rsis[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return rsis


def find_swings(values, side=3):
    highs, lows = [], []
    n = len(values)
    for i in range(side, n - side):
        window = values[i - side:i + side + 1]
        if values[i] == max(window) and window.count(values[i]) == 1:
            highs.append(i)
        if values[i] == min(window) and window.count(values[i]) == 1:
            lows.append(i)
    return highs, lows


def compute_divergence(closes, rsis, lookback=40):
    n = len(closes)
    start = max(0, n - lookback)
    sub_close = closes[start:]
    sub_rsi = rsis[start:]
    highs, lows = find_swings(sub_close)
    out = {"regularBearish": False, "regularBullish": False,
           "hiddenBearish": False, "hiddenBullish": False, "label": None}

    def valid(i1, i2):
        return sub_rsi[i1] is not None and sub_rsi[i2] is not None

    if len(highs) >= 2:
        i1, i2 = highs[-2], highs[-1]
        if valid(i1, i2):
            p1, p2 = sub_close[i1], sub_close[i2]
            r1, r2 = sub_rsi[i1], sub_rsi[i2]
            if p2 > p1 and r2 < r1 and r1 >= 65 and r2 >= 65:
                out["regularBearish"] = True
                out["label"] = "일반 약세"
            elif p2 < p1 and r2 > r1 and r1 >= 65 and r2 >= 65:
                out["hiddenBearish"] = True
                out["label"] = "히든 약세"
    if out["label"] is None and len(lows) >= 2:
        i1, i2 = lows[-2], lows[-1]
        if valid(i1, i2):
            p1, p2 = sub_close[i1], sub_close[i2]
            r1, r2 = sub_rsi[i1], sub_rsi[i2]
            if p2 < p1 and r2 > r1 and r1 <= 35 and r2 <= 35:
                out["regularBullish"] = True
                out["label"] = "일반 강세"
            elif p2 > p1 and r2 < r1 and r1 <= 35 and r2 <= 35:
                out["hiddenBullish"] = True
                out["label"] = "히든 강세"
    return out


def rsi_zone(rsi):
    if rsi is None:
        return None
    if rsi >= 70:
        return "overbought"
    if rsi <= 30:
        return "oversold"
    return "neutral"


def polygon_bars(ticker, days_back=220):
    end = date.today()
    start = end - timedelta(days=days_back)
    url = (f"{BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start.isoformat()}/{end.isoformat()}"
           f"?adjusted=true&sort=asc&limit=300&apiKey={API_KEY}")
    d = http_get_json(url)
    return d.get("results", [])


def fetch_stock_technicals(symbol):
    bars = polygon_bars(symbol)
    if len(bars) < 20:
        return None
    closes = [b["c"] for b in bars]
    rsis = wilder_rsi(closes)
    last_rsi = rsis[-1]
    price = closes[-1]
    prev_close = closes[-2] if len(closes) > 1 else price
    change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else None
    div = compute_divergence(closes, rsis)
    zone = rsi_zone(last_rsi)
    has_signal = bool(div["label"]) or zone in ("overbought", "oversold")
    return {
        "price": round(price, 2),
        "changePct": change_pct,
        "rsi": round(last_rsi, 1) if last_rsi is not None else None,
        "rsiZone": zone,
        "regularBearish": div["regularBearish"],
        "regularBullish": div["regularBullish"],
        "hiddenBearish": div["hiddenBearish"],
        "hiddenBullish": div["hiddenBullish"],
        "divergenceLabel": div["label"],
        "hasSignal": has_signal,
    }


def fetch_simple_change(ticker):
    end = date.today()
    start = end - timedelta(days=15)
    url = (f"{BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start.isoformat()}/{end.isoformat()}"
           f"?adjusted=true&sort=asc&limit=20&apiKey={API_KEY}")
    d = http_get_json(url)
    results = d.get("results", [])
    if len(results) < 2:
        return None
    last, prev = results[-1]["c"], results[-2]["c"]
    return {"price": round(last, 2), "changePct": round((last - prev) / prev * 100, 2)}


def fetch_many(symbols, fetch_fn, sleep_s=0.5, max_retries=3):
    results, failed = {}, []
    for sym in symbols:
        ok = False
        for attempt in range(max_retries):
            try:
                r = fetch_fn(sym)
                if r is not None:
                    results[sym] = r
                ok = True
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(sleep_s * (attempt + 2))
                    continue
                log(f"  FAILED {sym}: HTTP {e.code}")
                break
            except Exception as e:
                log(f"  FAILED {sym}: {e}")
                break
        if not ok:
            failed.append(sym)
        time.sleep(sleep_s)
    return results, failed


def fetch_many_with_retry(symbols, fetch_fn, label):
    log(f"Fetching {label} for {len(symbols)} symbols...")
    results, failed = fetch_many(symbols, fetch_fn, sleep_s=0.5)
    round_n = 1
    sleep_s = 1.5
    while failed and round_n <= 4:
        log(f"  retry round {round_n}: {len(failed)} symbols, sleep={sleep_s}s")
        more, failed = fetch_many(failed, fetch_fn, sleep_s=sleep_s)
        results.update(more)
        sleep_s *= 1.8
        round_n += 1
    if failed:
        log(f"  still failed after retries: {failed}")
    return results, failed


def wikipedia_constituents():
    req = urllib.request.Request(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8")
    m = re.search(r'<table[^>]*id="constituents"[^>]*>(.*?)</table>', html, re.S)
    if not m:
        raise RuntimeError("constituents table not found on Wikipedia page")
    table = m.group(1)
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)

    def clean(cell):
        cell = re.sub(r"<[^>]+>", "", cell)
        return re.sub(r"\s+", " ", cell).strip()

    out = []
    for row in rows[1:]:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
        if len(cells) < 8:
            continue
        out.append({
            "symbol": clean(cells[0]),
            "name": clean(cells[1]),
            "sector": clean(cells[2]),
            "subIndustry": clean(cells[3]),
            "hq": clean(cells[4]),
            "added": clean(cells[5]),
            "founded": clean(cells[7]),
        })
    return out


def parse_founded_year(founded_str):
    m = re.search(r"\d{4}", founded_str or "")
    return int(m.group(0)) if m else None


def sync_constituents(stocks_by_symbol, meta_source):
    log("Checking S&P 500 constituents against Wikipedia...")
    try:
        wiki = wikipedia_constituents()
    except Exception as e:
        log(f"  Wikipedia fetch failed, skipping constituent sync: {e}")
        return stocks_by_symbol, meta_source

    wiki_by_symbol = {w["symbol"]: w for w in wiki}
    db_symbols = set(stocks_by_symbol.keys())
    wiki_symbols = set(wiki_by_symbol.keys())

    new_symbols = wiki_symbols - db_symbols
    missing_symbols = db_symbols - wiki_symbols

    pending_removal = set()
    m = re.search(r"PENDING_REMOVAL_NEXT_RUN:\s*([^\n]*)", meta_source or "")
    if m:
        pending_removal = {s.strip() for s in m.group(1).split(",") if s.strip() and s.strip() != "none"}

    added, removed = [], []
    for sym in new_symbols:
        w = wiki_by_symbol[sym]
        stocks_by_symbol[sym] = {
            "symbol": sym, "name": w["name"], "sector": w["sector"],
            "subIndustry": w["subIndustry"], "hq": w["hq"], "added": w["added"],
            "founded": w["founded"], "foundedYear": parse_founded_year(w["founded"]),
            "marketCap": None, "price": None, "changePct": None, "rsi": None,
            "rsiZone": None, "regularBearish": False, "regularBullish": False,
            "hiddenBearish": False, "hiddenBullish": False, "divergenceLabel": None,
            "hasSignal": False,
        }
        added.append(sym)

    still_missing = missing_symbols & pending_removal
    for sym in still_missing:
        del stocks_by_symbol[sym]
        removed.append(sym)

    new_pending = missing_symbols - still_missing
    pending_str = ", ".join(sorted(new_pending)) if new_pending else "none"

    note = (f"Constituents: +{len(added)} ({', '.join(sorted(added)) or 'none'}), "
            f"-{len(removed)} ({', '.join(sorted(removed)) or 'none'}). "
            f"PENDING_REMOVAL_NEXT_RUN: {pending_str}")
    log(f"  {note}")
    return stocks_by_symbol, note


def fetch_kr_ranking(pages=1):
    """Top Korean (KOSPI) stocks by market cap, scraped from Naver Finance."""
    out = []
    for page in range(1, pages + 1):
        url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page={page}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("euc-kr", errors="replace")
        rows = re.findall(r"<tr[^>]*onMouseOver[^>]*>(.*?)</tr>", html, re.S)
        for row in rows:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if len(cells) < 7:
                continue
            code_m = re.search(r"code=(\d{6})", cells[1])
            if not code_m:
                continue
            code = code_m.group(1)

            def clean(cell):
                cell = re.sub(r"<[^>]+>", "", cell)
                return re.sub(r"\s+", " ", cell).strip()

            name = clean(cells[1])
            if re.search(r"(우|우B|우C)$", name):
                continue  # skip preferred-share listings, keep one row per company
            market_cap_text = clean(cells[6]).replace(",", "")
            try:
                market_cap = float(market_cap_text) * 100_000_000  # 억원 -> 원
            except ValueError:
                market_cap = None
            out.append({"code": code, "name": name, "marketCap": market_cap})
    return out


def fetch_kr_technicals(code):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.KS?range=1y&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode("utf-8"))
    result = (d.get("chart") or {}).get("result")
    if not result:
        return None
    closes_raw = result[0]["indicators"]["quote"][0]["close"]
    closes = [c for c in closes_raw if c is not None]
    if len(closes) < 20:
        return None
    rsis = wilder_rsi(closes)
    last_rsi = rsis[-1]
    price = closes[-1]
    prev_close = closes[-2] if len(closes) > 1 else price
    change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else None
    div = compute_divergence(closes, rsis)
    zone = rsi_zone(last_rsi)
    has_signal = bool(div["label"]) or zone in ("overbought", "oversold")
    return {
        "price": round(price, 2),
        "changePct": change_pct,
        "rsi": round(last_rsi, 1) if last_rsi is not None else None,
        "rsiZone": zone,
        "regularBearish": div["regularBearish"],
        "regularBullish": div["regularBullish"],
        "hiddenBearish": div["hiddenBearish"],
        "hiddenBullish": div["hiddenBullish"],
        "divergenceLabel": div["label"],
        "hasSignal": has_signal,
    }


def update_kr_stocks(existing_kr_stocks, target_count=50):
    log(f"Fetching KOSPI market-cap ranking from Naver Finance (top {target_count})...")
    kr_by_code = {s["code"]: s for s in existing_kr_stocks}
    try:
        ranking = fetch_kr_ranking(pages=2)[:target_count]
    except Exception as e:
        log(f"  Naver ranking fetch failed: {e}")
        return existing_kr_stocks, f"KR ranking fetch failed: {e}"

    for i, r in enumerate(ranking):
        code = r["code"]
        prev = kr_by_code.get(code, {})
        kr_by_code[code] = {
            **prev,
            "rank": i + 1,
            "symbol": code,
            "name": r["name"],
            "marketCap": r["marketCap"],
        }

    codes = [r["code"] for r in ranking]
    results, failed = fetch_many_with_retry(codes, fetch_kr_technicals, "KR stocks")
    for code, tech in results.items():
        kr_by_code[code].update(tech)

    note = f"KR stocks: {len(results)}/{len(codes)} updated (KOSPI top {target_count} by market cap via Naver Finance)"
    if failed:
        note += f" (failed: {', '.join(failed)})"
    log(f"  {note}")

    ranked_codes = {r["code"] for r in ranking}
    kr_list = [v for k, v in kr_by_code.items() if k in ranked_codes]
    kr_list.sort(key=lambda s: s.get("rank", 999))
    return kr_list, note


def main():
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    stocks_by_symbol = {s["symbol"]: s for s in data["stocks"]}
    etfs_by_symbol = {e["symbol"]: e for e in data["etfs"]}
    indices_by_code = {i["code"]: i for i in data["indices"]}

    prev_source = (data.get("meta") or {}).get("source", "")
    stocks_by_symbol, constituent_note = sync_constituents(stocks_by_symbol, prev_source)

    BACKFILL_STEP = 50  # how many more never-fetched stocks to add each night
    ranked_symbols = sorted(
        [s for s in stocks_by_symbol if stocks_by_symbol[s].get("marketCap")],
        key=lambda s: stocks_by_symbol[s]["marketCap"],
        reverse=True,
    )
    currently_filled = sum(1 for s in ranked_symbols if stocks_by_symbol[s].get("price") is not None)
    target_count = min(len(ranked_symbols), currently_filled + BACKFILL_STEP)
    top100_symbols = ranked_symbols[:target_count]
    log(f"Coverage: {currently_filled} stocks already had data; targeting {target_count} tonight "
        f"(+{target_count - currently_filled} newly backfilled, out of {len(ranked_symbols)} total).")

    stock_results, stock_failed = fetch_many_with_retry(top100_symbols, fetch_stock_technicals, f"top-{target_count} stocks")
    for sym, r in stock_results.items():
        stocks_by_symbol[sym].update(r)

    etf_symbols = list(etfs_by_symbol.keys())
    etf_results, etf_failed = fetch_many_with_retry(etf_symbols, fetch_simple_change, "ETFs")
    for sym, r in etf_results.items():
        etfs_by_symbol[sym].update(r)

    index_note = "IXIC updated"
    try:
        comp = fetch_simple_change("I:COMP")
        if comp and "IXIC" in indices_by_code:
            end = date.today()
            start = end - timedelta(days=15)
            url = (f"{BASE}/v2/aggs/ticker/I:COMP/range/1/day/{start.isoformat()}/{end.isoformat()}"
                   f"?adjusted=true&sort=asc&limit=20&apiKey={API_KEY}")
            d = http_get_json(url)
            results = d.get("results", [])
            if len(results) >= 2:
                last, prev = results[-1]["c"], results[-2]["c"]
                indices_by_code["IXIC"]["level"] = round(last, 2)
                indices_by_code["IXIC"]["change"] = round(last - prev, 2)
                indices_by_code["IXIC"]["changePct"] = round((last - prev) / prev * 100, 2)
    except Exception as e:
        index_note = f"IXIC update failed: {e}"
    log(index_note)
    log("DJIA/SPX/VIX left unchanged (not authorized on current Massive/Polygon.io plan).")

    log("Fetching crypto market data from CoinGecko...")
    crypto_by_symbol = {c["symbol"]: c for c in data["crypto"]}
    crypto_note = ""
    try:
        markets = http_get_json(
            "https://api.coingecko.com/api/v3/coins/markets"
            "?vs_currency=usd&order=market_cap_desc&per_page=100&page=1"
        )
        matched = 0
        by_upper_symbol = {}
        for coin in markets:
            sym = (coin.get("symbol") or "").upper()
            by_upper_symbol[sym] = coin
            if sym in crypto_by_symbol:
                crypto_by_symbol[sym]["price"] = coin.get("current_price")
                crypto_by_symbol[sym]["changePct"] = round(coin.get("price_change_percentage_24h") or 0, 2)
                crypto_by_symbol[sym]["marketCap"] = coin.get("market_cap")
                crypto_by_symbol[sym]["rank"] = coin.get("market_cap_rank")
                matched += 1
        crypto_note = f"{matched}/{len(crypto_by_symbol)} tracked coins matched CoinGecko top 100 by symbol"

        top20_ids = []
        for coin in sorted(markets, key=lambda c: c.get("market_cap_rank") or 999)[:40]:
            sym = (coin.get("symbol") or "").upper()
            if sym in STABLECOIN_SYMBOLS or sym not in crypto_by_symbol:
                continue
            top20_ids.append((sym, coin["id"]))
            if len(top20_ids) >= 20:
                break

        for sym, cg_id in top20_ids:
            try:
                chart = http_get_json(
                    f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart"
                    "?vs_currency=usd&days=100&interval=daily"
                )
                prices = [p[1] for p in chart.get("prices", [])]
                if len(prices) < 20:
                    continue
                rsis = wilder_rsi(prices)
                div = compute_divergence(prices, rsis)
                last_rsi = rsis[-1]
                crypto_by_symbol[sym]["rsi"] = round(last_rsi, 1) if last_rsi is not None else None
                crypto_by_symbol[sym]["rsiZone"] = rsi_zone(last_rsi)
                crypto_by_symbol[sym]["regularBearish"] = div["regularBearish"]
                crypto_by_symbol[sym]["regularBullish"] = div["regularBullish"]
                crypto_by_symbol[sym]["hiddenBearish"] = div["hiddenBearish"]
                crypto_by_symbol[sym]["hiddenBullish"] = div["hiddenBullish"]
                time.sleep(1.5)
            except Exception as e:
                log(f"  crypto RSI fetch failed for {sym}: {e}")
                time.sleep(2)
    except Exception as e:
        crypto_note = f"CoinGecko fetch failed: {e}"
    log(f"  {crypto_note}")

    existing_kr_stocks = data.get("krStocks", [])
    kr_stocks, kr_note = update_kr_stocks(existing_kr_stocks, target_count=50)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    source = (
        f"Live refresh via Massive/Polygon.io + CoinGecko + Naver/Yahoo Finance at {now}. "
        f"Stocks: {len(stock_results)}/{len(top100_symbols)} of top-{target_count} updated "
        f"({currently_filled} carried over, {target_count - currently_filled} newly backfilled)"
        + (f" (failed: {', '.join(stock_failed)})" if stock_failed else "") + ". "
        f"ETFs: {len(etf_results)}/{len(etf_symbols)} updated"
        + (f" (failed: {', '.join(etf_failed)})" if etf_failed else "") + ". "
        f"{index_note}. {crypto_note}. {kr_note}. {constituent_note}"
    )

    data["stocks"] = list(stocks_by_symbol.values())
    data["etfs"] = list(etfs_by_symbol.values())
    data["krStocks"] = kr_stocks
    data["indices"] = list(indices_by_code.values())
    data["crypto"] = list(crypto_by_symbol.values())
    data["meta"] = {"updatedAt": now, "source": source}

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    log("data.json written.")
    log(source)


if __name__ == "__main__":
    main()
