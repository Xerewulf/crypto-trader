#!/usr/bin/env python3
"""Chart Scanner for trader-scanner: scan thousands of charts for setups. Standard library only.

    python3 scan.py universe                          # Nasdaq Trader lists -> trader/universe.txt
    python3 scan.py daily                             # before the open: daily setups -> trader/watchlist.json
    python3 scan.py daily --symbols TSLA,NVDA,AMD     # or --watchlist FILE, or --limit 200
    python3 scan.py orb                               # after 9:50 AM ET: opening-range breakouts so far
    python3 scan.py orb --at 10:15 --or-minutes 15    # cut the clock at 10:15 AM ET
    python3 scan.py orb --offline wall5m.json.gz --now "2026-10-19 10:15" --min-price 0

universe  Downloads Nasdaq Trader's symbol directories (nasdaqlisted.txt and
          otherlisted.txt) and keeps US-listed common stocks, ordinary shares
          and ADRs. The rule, in order:
            1. not a test issue, not an ETF, not NextShares
            2. a plain ticker: 1 to 5 letters, or 1 to 4 letters plus one
               class letter after a dot (BRK.B); a .U / .W / .R suffix
               (units, warrants, rights) is out, and so is a $ preferred
            3. out, whatever else the name says: warrants; SPAC units and
               rights ("Units, each consisting of", "Rights to receive");
               notes, ETNs, debentures; a % coupon; "Depositary Shares" that
               are not American or Global (those carry a preferred)
            4. in: the name calls it common stock / shares / units, ordinary
               shares, shares of beneficial interest, ADS / ADR / American or
               Global depositary shares, or partnership / trust units
            5. out: the name still says unit, right, bond, preferred or preference
            6. in: everything else (plain names like "AMETEK, Inc.")

daily     Pre-market scan of daily CLOSES (Yahoo spark: 20 symbols a request,
          3 months of 1-day bars, closes only). Liquidity from the spark meta:
          regularMarketPrice >= --min-price (5) and regularMarketVolume >=
          --min-volume (1,000,000). Setups, from closes only:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily bar is dropped until the session's close.

orb       Opening-range breakouts, 5-minute bars. The range is the first
          --or-minutes (15) of the session. Stage 1: spark closes for every
          symbol (1 day of 5-minute bars, 20 a request); a chart goes on only
          if a completed bar after the range closed at or above the highest
          close of the range (a necessary condition). Stage 2: each candidate's
          v8 chart (real highs and lows): range high = the highest high of the
          range bars, a hit = the first completed bar to CLOSE above it, at that
          bar's close time. Stage 2 fetches at most --max-confirm (200) charts,
          the most traded candidates first (price x volume so far), and says
          so when it stops short. A chart is usable only when every 5-minute
          bar from 9:30 to the cutoff is there (a missing bar is a halt or no
          trades). Floors: --min-price 5 (last close), --min-volume 0.

Bars: null closes are dropped, a bar off the 5-minute grid (the live tick,
like 10:49:45) is dropped, and a bar counts only once it is complete
(start + 5 minutes <= the clock). Times are New York (ET). --now pins the
clock ("2026-10-19 10:15", New York time; ...Z for UTC).

Yahoo's endpoints are free, unofficial and can lag. query2 first, query1 on
a 429 or 5xx, one retry after a pause on a 429. A polite pause between
requests (--pause). Partial results are reported as partial.

--offline PATH reads files instead of the network: universe wants a folder
with nasdaqlisted.txt and otherlisted.txt; daily and orb take a folder of
<T>.json v8 chart files or spark responses, or one file that is a spark
response, a v8 chart, or a bundle {"TSLA": <v8 json>, ...}. .json.gz is fine.

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
"""

import argparse
import concurrent.futures
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
BINANCE_HOSTS = ("https://api.binance.com", "https://data-api.binance.vision")
NASDAQ_FILES = ("nasdaqlisted.txt", "otherlisted.txt")
NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/"
STABLECOINS = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "GBP", "EURI", "AEUR",
               "USD1", "USDE", "RLUSD", "XUSD", "USDT", "PYUSD", "BFUSD"}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
SPARK_MAX = 20      # Yahoo's spark answers 400 above 20 symbols
PAUSE = 0.05        # seconds between requests: be polite to free endpoints
BACKOFF = 5.0       # seconds to wait before the one retry after a 429
SLEEP = time.sleep  # tests swap this out
BAR_MIN = 5
MAX_CONFIRM = 200   # stage-2 v8 charts per orb scan, most traded first
MIN_DAILY_BARS = 30
DEFAULT_UNIVERSE = "trader/universe.txt"
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."

NYSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King, Jr. Day",
    "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth National Independence Day",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King, Jr. Day",
    "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth National Independence Day (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
}
NYSE_EARLY_CLOSE = {  # 1:00 p.m. ET
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
}
CALENDAR_YEARS = (2026, 2027)
OPEN = datetime.time(9, 30)
CLOSE = datetime.time(16, 0)
EARLY = datetime.time(13, 0)

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise.
    DST runs from 2:00 a.m. on the second Sunday of March to 2:00 a.m. on the
    first Sunday of November (US rule since 2007)."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))   # 2:00 EST
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))    # 2:00 EDT
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def ts_to_eastern(ts):
    """Unix seconds -> naive New York time. Never uses the machine's time zone."""
    return to_eastern(datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts)))


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as New York time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise ScanError('--now wants a time like "2026-10-19 10:15" (New York) or ...Z (UTC)')
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, flag="--at"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScanError("%s wants a New York time like 10:15" % flag)
    return datetime.time(int(m.group(1)), int(m.group(2)))


def trading_day(d):
    """(is_trading_day, reason). Weekends and the NYSE holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NYSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NYSE holiday: " + name
    return True, ""


def session(d):
    """(open_time, close_time) in New York time, or None if closed all day."""
    ok, _ = trading_day(d)
    if not ok:
        return None
    return OPEN, (EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE)


def clock_label(t):
    """datetime or time -> '10:15 AM'."""
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


# ---------------------------------------------------------------- network

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_get(path):
    """GET a Yahoo Finance path. query2 first, query1 on a 429 or 5xx, and one
    more round after BACKOFF seconds if a 429 was seen. 4xx other than 429 is final."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                try:
                    body = e.read().decode("utf-8", "replace") if 400 <= e.code < 500 and e.code != 429 else ""
                except Exception:
                    body = ""
                finally:
                    e.close()
                if e.code == 429:
                    saw_429 = True
                    continue
                if e.code >= 500:
                    continue
                if body.strip().startswith("{"):
                    return body  # Yahoo's JSON error ("No data found"), parsed by the caller
                raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))


def to_yahoo(sym):
    return sym.upper().replace(".", "-")


def to_binance(sym):
    s = re.sub(r"[\s/\-_]+", "", str(sym or "").upper())
    if s.endswith(".USD"):
        s = s[:-4] + "USDT"
    elif s.endswith("USD") and not s.endswith(("USDT", "FDUSD", "BUSD", "TUSD", "PYUSD", "RLUSD", "XUSD", "BFUSD")):
        s = s[:-3] + "USDT"
    elif not s.endswith(("USDT", "USDC", "FDUSD", "BUSD")) and not (len(s) > 4 and s.endswith(("BTC", "ETH", "BNB"))):
        s = s + "USDT"
    return s


def binance_get(path, timeout=20):
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in BINANCE_HOSTS:
            try:
                return http_get(host + path, timeout=timeout)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                try:
                    body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = ""
                finally:
                    e.close()
                if e.code in (429, 418):
                    saw_429 = True
                    continue
                if e.code in (451, 403) or e.code >= 500:
                    continue
                raise ScanError("Binance answered %s for %s: %s" % (last, path.split("?")[0], body[:120]))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ScanError("Binance answered %s for %s" % (last, path.split("?")[0]))


def build_binance_universe(quote="USDT"):
    info = json.loads(binance_get("/api/v3/exchangeInfo?permissions=SPOT", timeout=40))
    tickers = json.loads(binance_get("/api/v3/ticker/24hr", timeout=40))
    vols = {}
    for t in tickers if isinstance(tickers, list) else []:
        sym = t.get("symbol")
        if sym:
            try:
                vols[sym] = float(t.get("quoteVolume") or 0.0)
            except (TypeError, ValueError):
                vols[sym] = 0.0
    symbols = info.get("symbols") or []
    counts = {"rows": len(symbols), "keep": 0}
    kept = []
    q = quote.upper()
    for row in symbols:
        sym = (row.get("symbol") or "").strip().upper()
        base = (row.get("baseAsset") or "").strip().upper()
        qa = (row.get("quoteAsset") or "").strip().upper()
        status = (row.get("status") or "").strip().upper()
        if status != "TRADING" or not row.get("isSpotTradingAllowed", True):
            counts["not trading"] = counts.get("not trading", 0) + 1
            continue
        if qa != q:
            counts["other quote asset"] = counts.get("other quote asset", 0) + 1
            continue
        if base in STABLECOINS:
            counts["stablecoin / fiat"] = counts.get("stablecoin / fiat", 0) + 1
            continue
        if any(base.endswith(suf) and len(base) > len(suf) for suf in LEVERAGED_SUFFIXES):
            counts["leveraged token"] = counts.get("leveraged token", 0) + 1
            continue
        counts["keep"] += 1
        qv = vols.get(sym, 0.0)
        kept.append((sym, "%s / %s (24h vol $%s)" % (base, qa, "{:,.0f}".format(qv)), qv))
    kept.sort(key=lambda x: (-x[2], x[0]))
    return [(s, desc) for s, desc, _ in kept], counts


def fetch_binance_series(sym, interval, limit=60, ticker_meta=None, funding_pct=None):
    bsym = to_binance(sym)
    raw = json.loads(binance_get("/api/v3/klines?symbol=%s&interval=%s&limit=%d" % (bsym, interval, limit)))
    if not isinstance(raw, list) or not raw:
        raise ScanError("no Binance klines for %s" % bsym)
    now_ms = int(time.time() * 1000)
    t, o, h, l, c, v = [], [], [], [], [], []
    tb_ratios = []
    for k in raw:
        open_ms, close_ms = int(k[0]), int(k[6])
        if interval == "1d" and close_ms > now_ms:
            continue  # drop today's incomplete UTC daily bar
        t.append(open_ms // 1000)
        o.append(float(k[1]))
        h.append(float(k[2]))
        l.append(float(k[3]))
        c.append(float(k[4]))
        qv = float(k[7]) if len(k) > 7 else float(k[5])
        tb_qv = float(k[10]) if len(k) > 10 else (0.5 * qv)
        v.append(qv)
        if qv > 0:
            tb_ratios.append(100.0 * tb_qv / qv)
    tm = ticker_meta or {}
    last_p = float(tm.get("lastPrice") or (c[-1] if c else 0.0))
    quote_vol = float(tm.get("quoteVolume") or (v[-1] if v else 0.0))
    taker_buy_pct = round(tb_ratios[-1], 1) if tb_ratios else None
    meta = {
        "symbol": bsym,
        "source": "binance",
        "regularMarketPrice": last_p,
        "regularMarketVolume": quote_vol,
        "taker_buy_pct": taker_buy_pct,
    }
    if funding_pct is not None:
        meta["funding_rate_pct"] = funding_pct
    return {
        "t": t, "o": o, "h": h, "l": l, "c": c, "v": v,
        "meta": meta,
    }


def fetch_binance_batch(symbols, interval, limit=60, min_price=0.0, min_volume=0, a=None):
    """Fetch 24h stats + futures funding in 1 call each, pre-filter liquid symbols, then fetch klines concurrently."""
    data, failed = {}, {}
    tickers_raw = json.loads(binance_get("/api/v3/ticker/24hr", timeout=40))
    tmap = {t["symbol"]: t for t in (tickers_raw if isinstance(tickers_raw, list) else []) if "symbol" in t}
    fmap = {}
    try:
        prem_raw = json.loads(http_get("https://fapi.binance.com/fapi/v1/premiumIndex", timeout=10))
        if isinstance(prem_raw, list):
            for row in prem_raw:
                fsym = row.get("symbol")
                fr = row.get("lastFundingRate")
                if fsym and fr is not None:
                    fmap[fsym] = round(float(fr) * 100.0, 4)
                    if fsym.startswith("1000") and len(fsym) > 4:
                        fmap[fsym[4:]] = round(float(fr) * 100.0, 4)
    except Exception:
        pass
    to_fetch = []
    for sym in symbols:
        bsym = to_binance(sym)
        tm = tmap.get(bsym)
        if not tm:
            failed[sym] = "not on Binance Spot (%s)" % bsym
            continue
        p = float(tm.get("lastPrice") or 0.0)
        qv = float(tm.get("quoteVolume") or 0.0)
        if p < min_price or (min_volume > 0 and qv < min_volume):
            # Still store minimal series so scan_daily / scan_orb counts it as illiquid rather than failed
            data[sym] = {"t": [], "o": [], "h": [], "l": [], "c": [], "v": [],
                         "meta": {"symbol": bsym, "regularMarketPrice": p, "regularMarketVolume": qv}}
            continue
        to_fetch.append((sym, bsym, tm, fmap.get(bsym)))
    if a is not None and to_fetch:
        progress(a, "Fetching Binance %s klines for %d liquid pairs..." % (interval, len(to_fetch)))

    def _one(item):
        sym, bsym, tm, fpct = item
        return sym, fetch_binance_series(bsym, interval, limit=limit, ticker_meta=tm, funding_pct=fpct)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_one, item): item[0] for item in to_fetch}
        for fut in concurrent.futures.as_completed(futs):
            sym = futs[fut]
            try:
                _, ser = fut.result()
                data[sym] = ser
            except Exception as e:
                failed[sym] = str(e)
    return data, failed


def spark_path(symbols, rng, interval):
    return "/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (
        ",".join(to_yahoo(s) for s in symbols), rng, interval)


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (to_yahoo(sym), rng, interval)


# ---------------------------------------------------------------- parsing

def series_from_result(res):
    """A Yahoo chart result (v8 or one spark response) -> plain lists."""
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    n = len(t)

    def col(k):
        v = q.get(k)
        return list(v) + [None] * (n - len(v)) if isinstance(v, list) else None
    return {"t": list(t), "c": col("close") or [None] * n, "o": col("open"), "h": col("high"),
            "l": col("low"), "v": col("volume"), "meta": res.get("meta") or {}}


def parse_spark(text_or_doc, wanted=None):
    """Spark JSON -> {SYM: series}. Symbols Yahoo does not know are simply missing
    from the answer. `wanted` maps Yahoo symbols back to ours (BRK-B -> BRK.B)."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    sp = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(sp, dict):
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    err = sp.get("error")
    out = {}
    for item in sp.get("result") or []:
        resp = item.get("response") or []
        if not resp:
            continue
        ysym = item.get("symbol") or (resp[0].get("meta") or {}).get("symbol") or ""
        sym = (wanted or {}).get(ysym.upper(), ysym.upper().replace("-", "."))
        out[sym] = series_from_result(resp[0])
    if err and not out:
        raise ScanError("Yahoo spark: %s" % (err.get("description") or err.get("code") or err))
    return out


def parse_chart(text_or_doc, sym=""):
    """v8 chart JSON -> series. Raises ScanError for Yahoo's 'no data' answers."""
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise ScanError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise ScanError("no data for %s: %s" % (sym, err.get("description", "unknown ticker")))
    return series_from_result(res[0])


def _read_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _absorb(doc, name_hint, data, errors):
    """One parsed offline JSON doc -> data / errors."""
    if isinstance(doc, dict) and "spark" in doc:
        data.update(parse_spark(doc))
    elif isinstance(doc, dict) and "chart" in doc:
        sym = name_hint
        if not sym:
            try:
                sym = (doc["chart"].get("result") or [{}])[0]["meta"]["symbol"]
            except (AttributeError, KeyError, TypeError, IndexError):
                raise ScanError("an offline v8 chart without a symbol: name the file <TICKER>.json")
        sym = sym.upper().replace("-", ".")
        try:
            data[sym] = parse_chart(doc, sym)
        except ScanError as e:
            errors[sym] = str(e)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors)
    else:
        raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % name_hint)


def load_offline(path):
    """A folder of <T>.json(.gz) files or one file -> ({SYM: series}, {SYM: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            try:
                doc = json.loads(_read_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s is not valid JSON: %s" % (f, e))
            if isinstance(doc, dict) and ("chart" in doc or "spark" in doc):
                _absorb(doc, "" if "spark" in doc else _stem(f), data, errors)
            else:
                _absorb(doc, "", data, errors)
    elif os.path.isfile(path):
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        _absorb(doc, "", data, errors)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


# ---------------------------------------------------------------- universe

TICKER = re.compile(r"^[A-Z]{1,5}$|^[A-Z]{1,4}\.[A-Z]$")
ALWAYS_OUT = [
    ("warrant", re.compile(r"\bwarrants?\b")),
    ("unit or right", re.compile(r"\b(units?|rights?),? (each|that|entitling)\b|(?<!the )\brights? to\b")),
    ("note", re.compile(r"\b(notes?|etns?|debentures?)\b")),
    ("coupon", re.compile(r"%")),
    ("depositary", re.compile(r"(?<!american )(?<!global )\bdepositary shares?\b")),
]
COMMON = re.compile(
    r"\bcommon (stock|shares?|units?)\b|\bord(inary)? shares?\b|\bshares? of beneficial interest\b"
    r"|\b(american|global) deposit[ao]ry\b|\bads[s]?\b|\badrs?\b"
    r"|\b(limited partnership|partnership|trust) units?\b|\bl\.?p\.? units?\b"
    r"|\bunits? (of|representing)\b.*\b(limited partner|partnership|beneficial interest|limited liability)")
SOFT_OUT = [
    ("unit", re.compile(r"\bunits?\b")),
    ("right", re.compile(r"\brights?\b")),
    ("bond", re.compile(r"\bbonds?\b")),
    ("preferred", re.compile(r"\bpreferred\b|\bpreference\b")),
]


def classify(row):
    """One symbol-directory row -> 'keep' or the rule that drops it."""
    sym = (row.get("Symbol") or row.get("ACT Symbol") or "").strip()
    if (row.get("Test Issue") or "").strip() != "N":
        return "test issue"
    if (row.get("ETF") or "").strip() != "N":
        return "ETF"
    if (row.get("NextShares") or "N").strip() != "N":
        return "NextShares"
    if not TICKER.match(sym):
        return "not a plain ticker"
    if re.search(r"\.[UWR]$", sym):
        return "unit / warrant / right suffix"
    name = " ".join((row.get("Security Name") or "").lower().split())
    for tag, rx in ALWAYS_OUT:
        if rx.search(name):
            return tag
    if COMMON.search(name):
        return "keep"
    for tag, rx in SOFT_OUT:
        if rx.search(name):
            return tag
    return "keep"


def parse_symdir(text):
    """Nasdaq Trader pipe file -> (rows, file_created). The last line is 'File Creation Time: ...'."""
    lines = [ln for ln in text.replace("\r", "").split("\n") if ln.strip()]
    if not lines or "|" not in lines[0]:
        raise ScanError("not a Nasdaq Trader symbol directory")
    hdr = [h.strip() for h in lines[0].split("|")]
    if "Security Name" not in hdr or not ({"Symbol", "ACT Symbol"} & set(hdr)):
        raise ScanError("not a Nasdaq Trader symbol directory (header %s)" % lines[0][:60])
    rows, created = [], None
    for ln in lines[1:]:
        if ln.startswith("File Creation Time"):
            m = re.search(r"(\d{2})(\d{2})(\d{4})(\d{2}):(\d{2})", ln)
            if m:
                created = "%s-%s-%s %s:%s" % (m.group(3), m.group(1), m.group(2), m.group(4), m.group(5))
            continue
        rows.append(dict(zip(hdr, ln.split("|"))))
    return rows, created


def build_universe(texts):
    """{filename: text} -> (kept [(SYM, name)], counts, created)."""
    kept, counts, created = {}, {}, None
    for fname in NASDAQ_FILES:
        if fname not in texts:
            raise ScanError("missing %s" % fname)
        rows, c = parse_symdir(texts[fname])
        created = created or c
        counts["rows"] = counts.get("rows", 0) + len(rows)
        for r in rows:
            why = classify(r)
            counts[why] = counts.get(why, 0) + 1
            if why == "keep":
                sym = (r.get("Symbol") or r.get("ACT Symbol")).strip()
                kept[sym] = " ".join((r.get("Security Name") or "").split())
    return sorted(kept.items()), counts, created


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes (the same math as the reel's refs/scan.py)."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_closes(series, now_utc):
    """[(date, close)] of COMPLETE daily bars: null closes dropped, today's bar dropped
    until the session closes, anything dated after the clock dropped."""
    if (series.get("meta") or {}).get("source") == "binance":
        out = {}
        for ts, c in zip(series["t"], series["c"]):
            if c is None:
                continue
            d = (datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts))).date()
            out[d] = float(c)
        return sorted(out.items())
    now = to_eastern(now_utc)
    today = now.date()
    s = session(today)
    today_done = s is None or now.time() >= s[1]
    out = {}
    for ts, c in zip(series["t"], series["c"]):
        if c is None:
            continue
        d = ts_to_eastern(ts).date()
        if d > today or (d == today and not today_done):
            continue
        out[d] = float(c)  # one bar a day: a late duplicate (the live tick) wins
    return sorted(out.items())


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def _round_p(x):
    x = float(x)
    if x >= 1.0:
        return round(x, 2)
    if x >= 0.01:
        return round(x, 4)
    return round(x, 8)


def scan_daily(data, symbols, now_utc, min_price, min_volume):
    """Pure. data = {SYM: series}. Returns (hits, counts, last_date)."""
    hits, dates = [], {}
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    is_binance = any(((data.get(sym) or {}).get("meta") or {}).get("source") == "binance" for sym in symbols)
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_closes(s, now_utc)
        meta = s.get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None and bars:
            price = bars[-1][1]
        vol = meta.get("regularMarketVolume")
        if price is None or price < min_price or (min_volume > 0 and (vol is None or vol < min_volume)):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [c for _, c in bars]
        tags, nums = find_setups(closes)
        if tags:
            p_val = _round_p(closes[-1]) if is_binance else round(closes[-1], 2)
            hit_row = {"symbol": sym, "setups": tags, "price": p_val,
                       "close_date": d_last.isoformat(), "rsi": round(nums["rsi"], 1),
                       "volume": int(vol) if vol is not None else None}
            if meta.get("funding_rate_pct") is not None:
                hit_row["funding_rate_pct"] = meta["funding_rate_pct"]
            if meta.get("taker_buy_pct") is not None:
                hit_row["taker_buy_pct"] = meta["taker_buy_pct"]
            hits.append(hit_row)
    if is_binance:
        hits.sort(key=lambda h: (-(h["volume"] or 0), h["symbol"]))  # volume is already 24h USDT quoteVolume
    else:
        hits.sort(key=lambda h: (-(h["price"] * (h["volume"] or 0)), h["symbol"]))
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- opening-range breakout

def slot(day, k, open_t=None):
    return datetime.datetime.combine(day, open_t or OPEN) + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(series, day, cutoff, open_t=None, close_t=None):
    """5-minute bars of `day` that are complete at `cutoff` (naive New York time),
    on the grid, with a close. Sorted, one per slot."""
    is_binance = (series.get("meta") or {}).get("source") == "binance"
    if is_binance:
        o_t = open_t or datetime.time(0, 0)
        c_t = close_t or datetime.time(23, 59, 59)
    else:
        s = session(day)
        o_t = open_t or OPEN
        c_t = close_t or (s[1] if s else CLOSE)
    out = {}
    hs, ls = series.get("h"), series.get("l")
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        et = ts_to_eastern(ts)
        if et.date() != day or et.second or et.minute % BAR_MIN:
            continue  # another day, or the live tick off the grid
        if et.time() < o_t or (not is_binance and et.time() >= c_t):
            continue  # pre-market, after hours, the 4:00 PM closing print
        if et + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still in progress at the cutoff
        v = series.get("v")
        out[et] = {"start": et, "c": float(c),
                   "h": None if not hs or hs[i] is None else float(hs[i]),
                   "l": None if not ls or ls[i] is None else float(ls[i]),
                   "v": (v[i] or 0) if v else 0}
    return [out[k] for k in sorted(out)]


def usable(bars, day, cutoff, n_range, open_t=None, is_binance=False):
    """Every slot from open_t to the cutoff is there, and at least the range."""
    o_t = open_t or (datetime.time(0, 0) if is_binance else OPEN)
    want = int((cutoff - slot(day, 0, o_t)).total_seconds() // (BAR_MIN * 60))
    if not is_binance:
        s = session(day)
        if s:
            want = min(want, int((datetime.datetime.combine(day, s[1]) - slot(day, 0, o_t)).total_seconds()
                                 // (BAR_MIN * 60)))
    if want < n_range or len(bars) != want:
        return False
    return all(b["start"] == slot(day, k, o_t) for k, b in enumerate(bars))


def orb_candidate(bars, n_range):
    """Stage 1, closes only: some completed bar after the range closed at or above
    the highest close of the range. Necessary for a breakout (range high >= any range close)."""
    top = max(b["c"] for b in bars[:n_range])
    return any(b["c"] >= top for b in bars[n_range:])


def orb_hit(bars, n_range, is_binance=False):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = bars[:n_range]
    if any(b["h"] is None or b["l"] is None for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            rp = _round_p if is_binance else (lambda v: round(v, 2))
            return {"price": rp(b["c"]), "at": clock_label(closes_at),
                    "bar_start": b["start"].strftime("%H:%M"), "bar_index": k,
                    "or_high": rp(oh), "or_low": rp(ol),
                    "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at}
    return None


def orb_window(now_utc, at, or_minutes, until, explicit_day=None, is_binance=False, open_t=None):
    """-> (day, cutoff naive ET) or raise ScanError with the plain message."""
    now = to_eastern(now_utc)
    day = explicit_day or now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    if is_binance:
        o_t = open_t or datetime.time(0, 0)
        range_end = slot(day, or_minutes // BAR_MIN, o_t)
        first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
        if at is not None:
            cutoff = datetime.datetime.combine(day, at)
            if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
                raise ScanError("--at %s is later than now (%s ET)." % (at.strftime("%H:%M"), clock_label(now)))
        else:
            cutoff = now.replace(second=0, microsecond=0)
        if cutoff < first_close:
            raise ScanError("It is %s ET: the %d-minute opening range runs %s to %s and the first bar after it "
                            "closes at %s. Run orb again after %s."
                            % (clock_label(cutoff), or_minutes, clock_label(o_t), clock_label(range_end),
                               clock_label(first_close), clock_label(first_close)))
        return day, cutoff
    ok, why = trading_day(day)
    if not ok:
        raise ScanError("No session on %s (%s): the market is closed. Run orb on a trading day "
                        "after the opening range, or replay a past day with --offline." % (day_label(day), why))
    s = session(day)
    range_end = slot(day, or_minutes // BAR_MIN)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = datetime.datetime.combine(day, s[1])
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s ET)." % (at.strftime("%H:%M"), clock_label(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (at.strftime("%H:%M"), clock_label(close_dt),
                                                                       day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The market closed at %s ET on %s. Replay the session with --at 10:15."
                            % (clock_label(close_dt), day_label(day)))
    if cutoff < first_close:
        raise ScanError("It is %s ET: the %d-minute opening range runs 9:30 to %s and the first bar after it "
                        "closes at %s. Run orb again after %s."
                        % (clock_label(cutoff), or_minutes, clock_label(range_end), clock_label(first_close),
                           clock_label(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s ET, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s, or replay "
                        "today with --at 10:15." % (clock_label(cutoff), until.strftime("%H:%M"),
                                                      clock_label(first_close)))
    return day, cutoff


def scan_orb(stage1, symbols, day, cutoff, or_minutes, min_price=0.0, min_volume=0, confirm=None,
             max_confirm=None, open_t=None):
    """stage1 = {SYM: series} with closes (spark or v8). confirm(sym) -> series with highs/lows
    (None = use the stage-1 series). With confirm and max_confirm, only the max_confirm most
    traded candidates (price x volume so far) are confirmed; the rest are counted as unconfirmed.
    Returns (hits, counts, failed {sym: why})."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    is_binance = any(((stage1.get(sym) or {}).get("meta") or {}).get("source") == "binance" for sym in symbols)
    for sym in symbols:
        s = stage1.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = session_bars(s, day, cutoff, open_t=open_t)
        if not usable(bars, day, cutoff, n_range, open_t=open_t, is_binance=is_binance):
            counts["unusable"] += 1
            continue
        vol = sum(b["v"] for b in bars) if s.get("v") else (s.get("meta") or {}).get("regularMarketVolume") or 0
        if bars[-1]["c"] < min_price or vol < min_volume:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if orb_candidate(bars, n_range):
            rank_val = vol if is_binance else (bars[-1]["c"] * vol)
            cands.append((sym, bars, rank_val))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: -x[2])
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, bars, _ in cands:
        full = bars
        if confirm is not None:
            try:
                s2 = confirm(sym)
            except ScanError as e:
                failed[sym] = str(e)
                continue
            full = session_bars(s2, day, cutoff, open_t=open_t)
            if not usable(full, day, cutoff, n_range, open_t=open_t, is_binance=is_binance):
                failed[sym] = "the v8 chart has a missing bar"
                continue
        counts["confirmed"] += 1
        h = orb_hit(full, n_range, is_binance=is_binance)
        if h:
            meta = (stage1.get(sym) or {}).get("meta") or {}
            row = dict({"symbol": sym, "setups": ["orb"]}, **h)
            if meta.get("funding_rate_pct") is not None:
                row["funding_rate_pct"] = meta["funding_rate_pct"]
            if meta.get("taker_buy_pct") is not None:
                row["taker_buy_pct"] = meta["taker_buy_pct"]
            hits.append(row)
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_spark(symbols, rng, interval, pause, a=None):
    """Batches of 20. -> ({SYM: series}, {SYM: error})."""
    data, failed = {}, {}
    batches = [symbols[i:i + SPARK_MAX] for i in range(0, len(symbols), SPARK_MAX)]
    for k, batch in enumerate(batches):
        if k and pause:
            SLEEP(pause)
        wanted = {to_yahoo(s): s for s in batch}
        try:
            got = parse_spark(yahoo_get(spark_path(batch, rng, interval)), wanted)
        except (ScanError, ValueError) as e:
            for s in batch:
                failed[s] = str(e)
            continue
        for s in batch:
            if s in got:
                data[s] = got[s]
            else:
                failed[s] = "no data from Yahoo"
        if a is not None and len(batches) > 25 and (k + 1) % 50 == 0:
            progress(a, "  spark %d of %d requests" % (k + 1, len(batches)))
    return data, failed


KNOWN_CRYPTO = {
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "SUI",
    "DOT", "NEAR", "APT", "TRX", "LTC", "BCH", "UNI", "MATIC", "POL", "PEPE",
    "SHIB", "FET", "RENDER", "TAO", "WIF", "ICP", "ETC", "XLM", "FIL", "AAVE",
    "AR", "TIA", "ATOM", "RUNE", "INJ", "KAS", "IMX", "GRT", "HBAR", "FTM",
    "SEI", "STX", "THETA", "OP", "ARB", "ALGO", "LDO", "BLUR", "PENDLE", "JUP"
}


def read_symbols(a, offline_syms=None):
    if a.symbols:
        syms = [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
        if not a.offline:
            if any(s.endswith(("USDT", "FDUSD", "USDC", "BUSD")) or s in KNOWN_CRYPTO for s in syms):
                syms = [to_binance(s) for s in syms]
    elif a.watchlist:
        syms = symbols_from_file(a.watchlist)
    elif offline_syms is not None and not a.universe:
        syms = sorted(offline_syms)  # offline: every chart in the file, unless a list was given
    else:
        path = a.universe or DEFAULT_UNIVERSE
        if not os.path.exists(path):
            raise ScanError("no %s yet: run `scan.py universe` first, or pass --symbols" % path)
        syms = symbols_from_file(path)
    out = []
    for s in syms:
        if s not in out:
            out.append(s)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        return [str(x.get("symbol") if isinstance(x, dict) else x).upper() for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln.split("|")[0].strip().upper())
    return out


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    return cfg if isinstance(cfg, dict) else {}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def money(x):
    x = float(x)
    if 0 < abs(x) < 0.0001:
        return "${:,.8f}".format(x).rstrip("0").rstrip(".")
    if 0 < abs(x) < 1.0:
        return "${:,.6f}".format(x).rstrip("0").rstrip(".")
    return "${:,.2f}".format(x)


# ---------------------------------------------------------------- commands

def cmd_universe(a):
    t0 = time.time()
    if a.offline:
        texts = {}
        for k, fname in enumerate(NASDAQ_FILES):
            p = os.path.join(a.offline, fname)
            if not os.path.exists(p):
                raise ScanError("no %s in %s" % (fname, a.offline))
            with open(p, encoding="utf-8", errors="replace") as fh:
                texts[fname] = fh.read()
        kept, counts, created = build_universe(texts)
    else:
        kept, counts = build_binance_universe(getattr(a, "quote", "USDT"))
        created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for sym, name in kept:
                fh.write("%s|%s\n" % (sym, name))
    dropped = {k: v for k, v in counts.items() if k not in ("keep", "rows")}
    if a.json:
        print(json.dumps({"count": len(kept), "rows": counts.get("rows", 0), "files_created": created,
                          "dropped": dropped, "out": None if a.no_write else a.out,
                          "seconds": round(time.time() - t0, 1)}, indent=2))
    else:
        if a.offline:
            print("Nasdaq Trader symbol directories%s: %s rows." % (
                (", files of %s ET" % created) if created else "", "{:,}".format(counts.get("rows", 0))))
            print("Kept %s US-listed common stocks, ordinary shares and ADRs." % "{:,}".format(len(kept)))
        else:
            print("Binance Spot exchangeInfo (%s): %s total trading symbols." % (
                created, "{:,}".format(counts.get("rows", 0))))
            print("Kept %s active Binance USDT spot pairs (sorted by 24h volume)." % "{:,}".format(len(kept)))
        if dropped:
            print("Dropped: " + ", ".join("%s %s" % ("{:,}".format(v), k)
                                          for k, v in sorted(dropped.items(), key=lambda kv: -kv[1])) + ".")
        if not a.no_write:
            print("Wrote %s (one SYMBOL|Name a line)." % a.out)
    return 0 if kept else 1


def cmd_daily(a):
    t0 = time.time()
    now = parse_now(a.now)
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
    symbols = read_symbols(a, offline_syms)
    sample_syms = (symbols[:10] if symbols else [])
    is_binance = any(str(s).endswith(("USDT", "BUSD", "FDUSD", "USDC")) for s in sample_syms)
    if not a.offline:
        if is_binance:
            data, failed = fetch_binance_batch(symbols, "1d", limit=35, min_price=a.min_price,
                                               min_volume=a.min_volume, a=a)
        else:
            progress(a, "Fetching daily closes for %s symbols (%d spark requests)..."
                     % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
            data, failed = fetch_spark(symbols, "3mo", "1d", a.pause, a)
    hits, counts, last_date = scan_daily(data, symbols, now, a.min_price, a.min_volume)
    nfailed = len(symbols) - counts["fetched"]
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    src = "binance" if is_binance else "yahoo"
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "daily",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "source": src, "filters": {"min_price": a.min_price, "min_volume": a.min_volume},
           "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    local = to_eastern(now)
    print("Daily scan, %s ET, closes through %s (%s, daily closes)." % (
        local.strftime("%a %Y-%m-%d %H:%M"), last_date.isoformat() if last_date else "nothing",
        "Yahoo spark" if a.offline else "Binance Spot 1d klines"))
    print("Fetched %s of %s charts%s. %s passed the liquidity filter ($%s+ and %s+ %s)%s; "
          "%s scanned." % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
                           (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
                           "{:,}".format(counts["fetched"] - counts["illiquid"]), _n(a.min_price),
                           "{:,}".format(a.min_volume), "shares" if a.offline else "24h USDT vol",
                           (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
                           "{:,}".format(counts["scanned"])))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
        print("Most traded first (24h volume):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        extra = ""
        if h.get("taker_buy_pct") is not None:
            extra += "  TakerBuy %4.1f%%" % h["taker_buy_pct"]
        if h.get("funding_rate_pct") is not None:
            extra += "  Fund %+.4f%%" % h["funding_rate_pct"]
        print("  %-10s %12s  RSI %5.1f%s  %s" % (h["symbol"], money(h["price"]), h["rsi"], extra,
                                                 ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news TICKER.")
    return 0 if hits else 1


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def cmd_orb(a):
    t0 = time.time()
    now = parse_now(a.now)
    at = parse_hhmm(a.at) if a.at else None
    cfg = read_config(a.config)
    data = failed = None
    explicit_day = None
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
        if not a.now:
            days = [ts_to_eastern(s["t"][-1]).date() for s in data.values() if s["t"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    symbols = read_symbols(a, offline_syms)
    sample_syms = (symbols[:10] if symbols else [])
    is_binance = any(str(s).endswith(("USDT", "BUSD", "FDUSD", "USDC")) for s in sample_syms)
    until = parse_hhmm(a.until or cfg.get("no_entries_after") or ("23:59" if is_binance else "15:30"), "--until")
    open_t = parse_hhmm(a.session_open, "--session-open") if getattr(a, "session_open", None) else (
        datetime.time(0, 0) if is_binance else OPEN)
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day, is_binance=is_binance, open_t=open_t)
    confirm = None
    if not a.offline:
        if is_binance:
            # Limit default Binance orb scan to top 150 by volume unless --limit or --symbols was given
            if not a.symbols and not a.watchlist and not a.limit and len(symbols) > 150:
                symbols = symbols[:150]
            data, failed = fetch_binance_batch(symbols, "%dm" % BAR_MIN, limit=300,
                                               min_price=a.min_price, min_volume=a.min_volume, a=a)
        else:
            today = to_eastern(parse_now(None)).date()
            if (today - day).days > 6:
                raise ScanError("Yahoo's free 5-minute history is short: replay %s with --offline" % day.isoformat())
            rng = "1d" if day == today else "5d"
            progress(a, "Stage 1: 5-minute closes for %s symbols (%d spark requests)..."
                     % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
            data, failed = fetch_spark(symbols, rng, "%dm" % BAR_MIN, a.pause, a)
            done = []

            def confirm(sym):
                if a.pause:
                    SLEEP(a.pause)
                done.append(sym)
                if len(done) % 50 == 0:
                    progress(a, "  stage 2: %d v8 charts so far" % len(done))
                return parse_chart(yahoo_get(chart_path(sym, rng, "%dm" % BAR_MIN)), sym)
    hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, a.min_volume, confirm,
                                     a.max_confirm, open_t=open_t)
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([s for s in symbols if s in failed])
    as_of = "%s %s ET" % (day.isoformat(), cutoff.strftime("%H:%M"))
    src = "binance" if is_binance else "yahoo"
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "orb",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "unconfirmed": counts["unconfirmed"], "source": src, "or_minutes": a.or_minutes,
           "interval": "%dm" % BAR_MIN,
           "filters": {"min_price": a.min_price, "min_volume": a.min_volume}, "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Opening-range breakout scan, %s %s ET: %d-minute range (%s to %s), 5-minute bars, %s."
          % (day_label(day), clock_label(cutoff), a.or_minutes, clock_label(open_t),
             clock_label(slot(day, a.or_minutes // BAR_MIN, open_t)), "Binance" if is_binance else "Yahoo"))
    print("Fetched %s of %s charts%s; %s usable%s; %s candidates, %s confirmed with real highs and lows."
          % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
             (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
             "{:,}".format(counts["scanned"]),
             (" (%s under the price / volume floor)" % "{:,}".format(counts["illiquid"])) if counts["illiquid"] else "",
             "{:,}".format(counts["candidates"]), "{:,}".format(counts["confirmed"])))
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the %s most traded are; --max-confirm 0 "
              "checks all)." % ("{:,}".format(counts["unconfirmed"]), "{:,}".format(a.max_confirm)))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s:"
              % (len(hits), ("the %s checked" % "{:,}".format(counts["confirmed"])) if counts["unconfirmed"]
                 else "{:,}".format(counts["scanned"]), clock_label(cutoff)))
        has_crypto_hits = any(str(h.get("symbol", "")).endswith(("USDT", "BUSD", "FDUSD", "USDC")) or h.get("taker_buy_pct") is not None for h in hits)
        for h in hits[:a.show]:
            extra = ""
            if h.get("taker_buy_pct") is not None:
                extra += "  TakerBuy %4.1f%%" % h["taker_buy_pct"]
            if h.get("funding_rate_pct") is not None:
                extra += "  Fund %+.4f%%" % h["funding_rate_pct"]
            if has_crypto_hits:
                print("  %8s  %-10s %12s  range %s to %s  %+.2f%% over%s" % (
                    h["at"], h["symbol"], money(h["price"]), _p(h["or_low"]), _p(h["or_high"]), h["pct_over"], extra))
            else:
                print("  %8s  %-6s %10s  range %s to %s  %+.2f%% over" % (
                    h["at"], h["symbol"], money(h["price"]), _p(h["or_low"]), _p(h["or_high"]), h["pct_over"]))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed above its opening-range high by %s." % clock_label(cutoff))
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news TICKER and /trader-entry TICKER.")
    return 0 if hits else 1


def _p(x):
    x = float(x)
    if 0 < abs(x) < 0.0001:
        return "{:,.8f}".format(x).rstrip("0").rstrip(".")
    if 0 < abs(x) < 1.0:
        return "{:,.6f}".format(x).rstrip("0").rstrip(".")
    return "{:,.2f}".format(x)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chart Scanner (trader-scanner): Binance crypto daily setups and opening-range breakouts.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="download Binance Spot USDT pairs -> trader/universe.txt")
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    p.add_argument("--quote", default="USDT", help="Binance quote asset (default USDT)")
    p.add_argument("--offline", metavar="DIR", help="a folder with nasdaqlisted.txt and otherlisted.txt")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--json", action="store_true")

    def common(p, min_price, min_volume):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated tickers instead of the universe (e.g. BTCUSDT,ETHUSDT,SOLUSDT)")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of tickers")
        p.add_argument("--universe", help="symbol list, one SYMBOL|Name a line (default %s)" % DEFAULT_UNIVERSE)
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, default=min_price)
        p.add_argument("--min-volume", type=int, default=min_volume)
        p.add_argument("--offline", metavar="PATH", help="fixture folder or file instead of the network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-19 10:15" New York, or ...Z)')
        p.add_argument("--out", default="trader/watchlist.json")
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (%.2f)" % PAUSE)
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="daily setups from Binance 1d closes")
    common(p, 0.0, 1000000)
    p = sub.add_parser("orb", help="opening-range breakouts so far today on 5m bars")
    common(p, 0.0, 0)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--session-open", help="session open time HH:MM ET (default 00:00 for 24/7 crypto)")
    p.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                   help="v8 charts to fetch in stage 2, most traded first (%d; 0 = all)" % MAX_CONFIRM)
    p.add_argument("--at", help="cut the clock at this New York time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM (default: config no_entries_after)")
    p.add_argument("--config", default="trader/config.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except ScanError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
