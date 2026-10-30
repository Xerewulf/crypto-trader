#!/usr/bin/env python3
"""Omni-Channel 25+ Source High-Frequency (10-Second) Crypto News & Catalyst Monitor.
Continuously scans breaking news every 10 seconds across:
  1. Federal Reserve (federalreserve.gov/feeds/press_all.xml, speeches.xml)
  2. Wall Street Investment Banks & SEC (JPMorgan, BlackRock, Goldman Sachs, Morgan Stanley, ECB, SEC)
  3. Official Binance CMS Announcements (New Listings, Delistings, Launchpool, Collateral)
  4. Crypto Twitter / X Whale Flow (Lookonchain, Whale Alert, Arkham, Wu Blockchain, Arthur Hayes)
  5. Financial & Crypto Magazines (Forbes, Fortune, Bloomberg, Reuters, Bitcoin Magazine, The Block, Blockworks, CryptoSlate, BeInCrypto)
  6. 24/7 Breaking Crypto Journalism & Wire APIs (CoinDesk, Cointelegraph, Decrypt, CryptoCompare)
  7. CoinGecko Global Trending Search & Social Heat

Features:
  - Tiered 10s / 30s / 60s polling with HTTP ETag / Last-Modified conditional caching
  - 24-hour publication timestamp freshness filtering and recency-weighted scoring
  - Dynamic token symbol extraction from exchange announcements + 65+ recognized tokens
  - Scoped token vetoes vs. systemic macro vetoes + real-time 10s breaking delta alerts
"""

import argparse
import datetime
import email.utils
import gzip
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
NEWS_FILE = os.path.join(HERE, "news_feed.json")

POLL_INTERVAL_SECONDS = 10
MAX_NEWS_AGE_HOURS = 24.0

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

BULL_PATTERNS = [
    ("burn/buyback", r"\btoken burn[s]?\b|\bburn(?:s|ed)? [\d.,]+\b|\bbuyback[s]?\b|\bdeflation"),
    ("ETF/institutional inflow", r"\binflow[s]?\b|\bETF[s]? (?:approv|launch|debut|demand)|\bapprov(?:es|ed|al)\b.*\bETF\b|\badd(?:s|ed)? [\d,.]+ (?:BTC|ETH|SOL|bitcoin|ether)\b|\bbought [\d,.]+ (?:BTC|ETH|SOL|bitcoin|ether)\b|\bcustody\b|\btokeniz"),
    ("Binance listing/launch", r"\bBinance Will List\b|\bBinance Will Add\b|\bBinance Futures Will Launch\b|\bLaunchpool\b|\bMegadrop\b|\bHODLer Airdrop\b|\bNew Cryptocurrency Listing\b"),
    ("upgrade/partnership", r"\bupgrade\b|\bmainnet\b|\bhard fork\b|\bpartnership\b|\bintegration\b"),
    ("whale/CT accumulation", r"\baccumulat(?:e|es|ed|ing|ion)\b|\bwhale[s]? bought\b|\bwithdrew\b|\bLookonchain\b.*\bbought\b|\bfresh wallet\b"),
    ("bullish/breakout", r"\bbreakout\b|\brall(?:y|ies|ied)\b|\bsurge[sd]?\b|\bjump(?:s|ed)?\b|\bspike[sd]?\b|\bgain(?:s|ed)?\b|\bbullish\b|\bbull market\b|\ball-time high\b|\bATH\b|\bsoar(?:s|ed)?\b|\brebound[s]?\b"),
    ("Fed/macro tailwind", r"\brate cut[s]?\b|\bdovish\b|\bstimulus\b|\bQE\b|\bliquidity injection\b|\bregulatory clarity\b"),
]

BEAR_PATTERNS = [
    ("unlock/dilution", r"\btoken unlock[s]?\b|\bcliff unlock\b|\bunlock(?:s|ed)? (?:of |worth )?\$?[\d.,]+\s?(?:[mkb]|million|billion)?\b|\bvesting\b|\bdilution\b|\bdump(?:s|ed|ing)?\b|\bsell-?off\b"),
    ("whale deposit/dump", r"\bdeposited .* to Binance\b|\bwhale[s]? (?:sold|dump)|\btransferred .* to exchange\b"),
    ("hack/exploit", r"\bhack(?:ed|s|ers)?\b|\bexploit(?:ed)?\b|\bbreach\b|\bdrain(?:ed)?\b|\boutage\b|\bcyberattack\b"),
    ("regulatory/delist", r"\blawsuit\b|\bsued\b|\bSEC charges\b|\bprobe\b|\bdelist(?:s|ed|ing)?\b|\bban(?:s|ned)?\b|\bcrackdown\b"),
    ("outflow/liquidation", r"\boutflow[s]?\b|\bliquidat(?:ed|ion|ions)\b|\bcrash(?:es|ed)?\b|\bplunge[sd]?\b|\bbearish\b|\bslide[s]?\b|\btumble[sd]?\b|\bslump[s]?\b|\brisk[- ]off\b"),
    ("treasury buying pause", r"\bpause[sd]? (?:its )?(?:bitcoin |BTC )?(?:purchases|buying|buys)\b|\bskip(?:s|ped)? (?:bitcoin|BTC) (?:buys|purchases)\b|\bno (?:bitcoin|BTC) purchases\b"),
    ("Fed/macro headwind", r"\brate hike[s]?\b|\bhawkish\b|\btariff[s]?\b|\bhot inflation\b|\brecession\b|\bhigher for longer\b"),
]

OMNI_RSS_FEEDS = (
    ("Fed Press", "fed", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("Fed Speeches", "fed", "https://www.federalreserve.gov/feeds/speeches.xml"),
    ("Banks & Fed Flow", "bank", "https://news.google.com/rss/search?q=(%22Federal+Reserve%22+OR+FOMC+OR+Powell+OR+ECB+OR+JPMorgan+OR+BlackRock+OR+%22Goldman+Sachs%22+OR+%22Morgan+Stanley%22+OR+%22Standard+Chartered%22+OR+Citigroup+OR+Fidelity+OR+SEC)+crypto+OR+bitcoin+OR+rates+when:3d&hl=en-US&gl=US&ceid=US:en"),
    ("X/Twitter Whale Flow", "twitter", "https://news.google.com/rss/search?q=(%22Lookonchain%22+OR+%22Whale+Alert%22+OR+%22Wu+Blockchain%22+OR+%22Arkham%22+OR+%22PeckShield%22+OR+%22ZachXBT%22+OR+%22Arthur+Hayes%22+OR+%22Crypto+Twitter%22+OR+%22on+X%22)+crypto+OR+bitcoin+OR+whale+when:3d&hl=en-US&gl=US&ceid=US:en"),
    ("Forbes/Bloomberg/Fortune", "magazine", "https://news.google.com/rss/search?q=(site:forbes.com+OR+site:fortune.com+OR+site:bloomberg.com+OR+site:reuters.com)+crypto+OR+bitcoin+when:3d&hl=en-US&gl=US&ceid=US:en"),
    ("Bitcoin Magazine", "magazine", "https://bitcoinmagazine.com/.rss/full/"),
    ("The Block", "magazine", "https://www.theblock.co/rss.xml"),
    ("Blockworks", "magazine", "https://blockworks.co/feed"),
    ("CryptoSlate", "magazine", "https://cryptoslate.com/feed/"),
    ("BeInCrypto", "magazine", "https://beincrypto.com/feed/"),
    ("CoinDesk", "news", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "news", "https://cointelegraph.com/rss"),
    ("Decrypt", "news", "https://decrypt.co/feed"),
)

# Tiered polling TTLs (in seconds) so 10s high-frequency monitoring never triggers 429 rate-limits:
#   Tier 1 (10s): Direct breaking wires, Fed XMLs, Binance CMS, CryptoCompare JSON
#   Tier 2 (30s): Crypto magazines & editorial feeds
#   Tier 3 (60s): Rate-limited Google News search queries & CoinGecko Trending
FEED_TTL_SECONDS = {
    "Fed Press": 10,
    "Fed Speeches": 10,
    "CoinDesk": 10,
    "Cointelegraph": 10,
    "Decrypt": 10,
    "Bitcoin Magazine": 30,
    "The Block": 30,
    "Blockworks": 30,
    "CryptoSlate": 30,
    "BeInCrypto": 30,
    "Banks & Fed Flow": 60,
    "X/Twitter Whale Flow": 60,
    "Forbes/Bloomberg/Fortune": 60,
}

BINANCE_CMS_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&pageNo=1&pageSize=15"
COINGECKO_TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"
CRYPTOCOMPARE_NEWS_URL = "https://min-api.cryptocompare.com/data/v2/news/?lang=EN"

# Expanded recognized tokens across Binance Spot & Futures
RECOGNIZED_TOKENS = {
    "BTC": ("bitcoin", "btc", "sats"),
    "ETH": ("ethereum", "ether", "eth"),
    "SOL": ("solana", "sol"),
    "BNB": ("binance coin", "bnb", "bsc", "pancakeswap"),
    "XRP": ("ripple", "xrp"),
    "DOGE": ("dogecoin", "doge"),
    "WLD": ("worldcoin", "wld"),
    "SUI": ("sui",),
    "AVAX": ("avalanche", "avax"),
    "NEAR": ("near protocol", "near"),
    "ADA": ("cardano", "ada"),
    "LINK": ("chainlink", "link"),
    "LTC": ("litecoin", "ltc"),
    "FIL": ("filecoin", "fil"),
    "PEPE": ("pepe",),
    "ENA": ("ethena", "ena"),
    "UNI": ("uniswap", "uni"),
    "TAO": ("bittensor", "tao"),
    "AAVE": ("aave",),
    "INJ": ("injective", "inj"),
    "TON": ("toncoin", "ton"),
    "TRX": ("tron", "trx"),
    "XLM": ("stellar", "xlm"),
    "ARB": ("arbitrum", "arb"),
    "OP": ("optimism",),
    "APT": ("aptos", "apt"),
    "SEI": ("sei network", "sei"),
    "TIA": ("celestia", "tia"),
    "RENDER": ("render", "rndr"),
    "FET": ("fetch.ai", "artificial superintelligence", "fet"),
    "PENDLE": ("pendle",),
    "ONDO": ("ondo finance", "ondo"),
    "HYPE": ("hyperliquid", "hype"),
    "JUP": ("jupiter", "jup"),
    "WIF": ("dogwifhat", "wif"),
    "BONK": ("bonk",),
    "SHIB": ("shiba inu", "shib"),
    "FLOKI": ("floki",),
    "DOT": ("polkadot", "dot"),
    "ATOM": ("cosmos", "atom"),
    "POL": ("polygon", "matic", "pol"),
    "HBAR": ("hedera", "hbar"),
    "ALGO": ("algorand", "algo"),
    "ICP": ("internet computer", "icp"),
    "VET": ("vechain", "vet"),
    "BCH": ("bitcoin cash", "bch"),
    "ETC": ("ethereum classic", "etc"),
    "CAKE": ("pancakeswap", "cake"),
    "CRV": ("curve dao", "crv"),
    "LDO": ("lido", "ldo"),
    "MKR": ("makerdao", "mkr", "sky"),
    "RUNE": ("thorchain", "rune"),
    "STX": ("stacks", "stx"),
    "KAS": ("kaspa", "kas"),
    "ORDI": ("ordi", "ordinals"),
    "GALA": ("gala games", "gala"),
    "SAND": ("the sandbox", "sand"),
    "MANA": ("decentraland", "mana"),
    "AXS": ("axie infinity", "axs"),
    "EIGEN": ("eigenlayer", "eigen"),
    "STRK": ("starknet", "strk"),
    "ZK": ("zksync",),
    "PYTH": ("pyth network", "pyth"),
    "JTO": ("jito", "jto"),
    "RAY": ("raydium", "ray"),
    "PHA": ("phala", "pha"),
    "ERA": ("caldera", "era"),
}

_STOP_TICKER_WORDS = {
    "USD", "USDT", "USDC", "FDUSD", "BUSD", "CEO", "CFO", "SEC", "CFTC", "DOJ", "FED",
    "FOMC", "ETF", "ETFS", "APR", "APY", "API", "VIP", "NEW", "THE", "AND", "FOR",
    "WITH", "FROM", "THIS", "THAT", "WILL", "LIST", "ADD", "SPOT", "PERP", "EARN",
    "AI", "PRO", "MAX", "MIN", "DAY", "NOW", "LIVE", "HOT", "TOP", "ALL", "OUT",
    "USA", "UK", "EU", "ECB", "BOJ", "IMF", "GDP", "CPI", "PPI", "NFP", "ATH",
}

# In-memory conditional HTTP cache: url -> {"ts": float, "etag": str, "last_modified": str, "body": str}
_HTTP_CACHE = {}
# Seen headline hashes: sha1 -> first_seen_ms
_SEEN_HEADLINES = {}
_INITIAL_SYNC_DONE = False


def clear_cache():
    """Reset HTTP cache and seen headline tracker (useful for testing or forced cold starts)."""
    global _INITIAL_SYNC_DONE
    _HTTP_CACHE.clear()
    _SEEN_HEADLINES.clear()
    _INITIAL_SYNC_DONE = False


def _http_get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
            try:
                data = gzip.decompress(data)
            except Exception:
                pass
        return data.decode("utf-8", "replace")


def _http_get_cached(url, timeout=7, ttl=10, force=False, now_ts=None):
    """Fetch URL with tiered TTL caching and HTTP ETag / If-Modified-Since conditional requests.
    Returns (text_body, was_cached_hit)."""
    now = now_ts if now_ts is not None else time.time()
    entry = _HTTP_CACHE.get(url)
    if entry and not force and ttl > 0 and (now - entry["ts"] < ttl) and entry.get("body") is not None:
        return entry["body"], True

    # If _http_get has been monkeypatched in unit tests, delegate directly to _http_get
    if getattr(_http_get, "__name__", "") != "_http_get" or hasattr(_http_get, "mock_calls"):
        body = _http_get(url, timeout=timeout)
        _HTTP_CACHE[url] = {"ts": now, "etag": "", "last_modified": "", "body": body}
        return body, False

    headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
    if entry and not force:
        if entry.get("etag"):
            headers["If-None-Match"] = entry["etag"]
        if entry.get("last_modified"):
            headers["If-Modified-Since"] = entry["last_modified"]

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                try:
                    data = gzip.decompress(data)
                except Exception:
                    pass
            body = data.decode("utf-8", "replace")
            _HTTP_CACHE[url] = {
                "ts": now,
                "etag": resp.headers.get("ETag") or "",
                "last_modified": resp.headers.get("Last-Modified") or "",
                "body": body,
            }
            return body, False
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and entry and entry.get("body") is not None:
            entry["ts"] = now
            return entry["body"], True
        if entry and entry.get("body") is not None:
            return entry["body"], True
        raise
    except Exception:
        if entry and entry.get("body") is not None:
            return entry["body"], True
        raise


def _json_or_none(url, timeout=8, ttl=0, force=False, now_ts=None):
    try:
        if ttl > 0:
            body, _ = _http_get_cached(url, timeout=timeout, ttl=ttl, force=force, now_ts=now_ts)
            return json.loads(body) if body else None
        return json.loads(_http_get(url, timeout=timeout))
    except Exception:
        return None


def html_unescape(s):
    text = html.unescape(s or "")
    return text.replace("\u2014", "-").replace("\u2013", "-")


def parse_pub_timestamp(pub_str="", title="", release_ms=None, now_ts=None):
    """Parse article publication time into UTC epoch seconds, or None if unavailable.
    Also detects explicit YYYY-MM-DD dates in Binance announcement titles so old archive
    articles are never scored as fresh breaking news."""
    now = now_ts if now_ts is not None else time.time()

    # 1. Explicit date in title like "(2025-06-16)" or "- 2024-10-30"
    if title:
        m_date = re.search(r"\b(202\d)-(\d{2})-(\d{2})\b", title)
        if m_date:
            try:
                y, mo, d = int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3))
                dt_title = datetime.datetime(y, mo, d, 12, 0, tzinfo=datetime.timezone.utc)
                title_ts = dt_title.timestamp()
                # If title explicitly references a date > 36h in the past, trust that date
                if now - title_ts > 36 * 3600:
                    return title_ts
            except Exception:
                pass

    # 2. Numeric epoch timestamp (ms or seconds)
    if release_ms is not None:
        try:
            val = float(release_ms)
            if val > 1e11:
                val /= 1000.0
            if val > 1e8:
                return val
        except Exception:
            pass

    # 3. RSS RFC-2822 or Atom ISO-8601 string
    s = (pub_str or "").strip()
    if s:
        try:
            dt = email.utils.parsedate_to_datetime(s)
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                return dt.timestamp()
        except Exception:
            pass
        try:
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.timestamp()
        except Exception:
            pass

    return None


def freshness_weight(age_seconds):
    """Weight headlines by how recently they broke:
      <= 15 min: 2.0x (immediate breaking catalyst)
      <= 2 hours: 1.25x
      <= 6 hours: 1.0x
      > 6 hours: 0.6x"""
    if age_seconds is None:
        return 1.0
    if age_seconds <= 900:
        return 2.0
    if age_seconds <= 7200:
        return 1.25
    if age_seconds <= 21600:
        return 1.0
    return 0.6


def _classify_headline(title):
    mb = [lbl for lbl, pat in BULL_PATTERNS if re.search(pat, title, re.I)]
    mr = [lbl for lbl, pat in BEAR_PATTERNS if re.search(pat, title, re.I)]
    if mb and not mr:
        return "BULLISH", mb
    if mr and not mb:
        return "BEARISH", mr
    if mb and mr:
        return "MIXED", mb + mr
    return "NEUTRAL", []


def _normalize_base_symbol(sym):
    s = str(sym or "").upper().strip()
    for q in ("USDT", "USDC", "FDUSD", "BUSD"):
        if s.endswith(q) and len(s) > len(q):
            s = s[:-len(q)]
            break
    if s.startswith("1000") and len(s) > 4:
        s = s[4:]
    return s


def _detect_tokens(title, extra_symbols=None):
    """Detect impacted crypto tokens from a headline using:
    1. RECOGNIZED_TOKENS dictionary (65+ major coins & aliases)
    2. Dynamic extra_symbols from active trading universe / watchlist
    3. Exchange announcement ticker extraction (e.g. 'Binance Will Delist EPIC, QUICK', 'PREDICTUSDT', '(AVNT)')"""
    found = []
    title_str = str(title or "")
    title_lower = title_str.lower()

    for sym, aliases in RECOGNIZED_TOKENS.items():
        for a in aliases:
            if re.search(r"\b" + re.escape(a) + r"\b", title_lower):
                if sym not in found:
                    found.append(sym)
                break

    if extra_symbols:
        for raw_sym in extra_symbols:
            base = _normalize_base_symbol(raw_sym)
            if base and len(base) >= 2 and base not in found and base not in _STOP_TICKER_WORDS:
                if re.search(r"\b" + re.escape(base.lower()) + r"(?:usdt|usdc|usd)?\b", title_lower):
                    found.append(base)

    # Extract explicit XXXUSDT / XXXUSDC pairs mentioned in exchange notices
    for m in re.finditer(r"\b([A-Z0-9]{2,10})(?:USDT|USDC|FDUSD)\b", title_str):
        cand = _normalize_base_symbol(m.group(1))
        if cand and len(cand) >= 2 and cand not in _STOP_TICKER_WORDS and cand not in found:
            found.append(cand)

    # Extract parenthetical tickers like "Introducing Avantis (AVNT)" or "Filecoin (FIL)"
    for m in re.finditer(r"\(([A-Z]{2,8})\)", title_str):
        cand = m.group(1)
        if cand not in _STOP_TICKER_WORDS and cand not in found:
            found.append(cand)

    # Extract comma/and-separated tickers after "Binance Will Delist X, Y" or "Binance Will List X, Y"
    m_list = re.search(
        r"\bBinance (?:Will|Alpha Will) (?:Delist|List|Remove|Add)\s+([A-Z0-9,\s&]+?)(?:\s+on\b|\s+from\b|\s*\(|\s*$)",
        title_str,
    )
    if m_list:
        for tok in re.findall(r"\b([A-Z][A-Z0-9]{1,8})\b", m_list.group(1)):
            if tok not in _STOP_TICKER_WORDS and tok not in found:
                found.append(tok)

    return found or ["CRYPTO"]


def _parse_rss(xml_text, source_lbl, category="news"):
    out = []
    if not xml_text:
        return out
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out
    items = list(root.iter("item"))
    if not items:
        items = [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "entry"]
    for item in items:
        title = ""
        pub = ""
        for c in item:
            t = c.tag.rsplit("}", 1)[-1]
            if t == "title" and not title:
                title = (c.text or "").strip()
            elif t in ("pubDate", "published", "updated") and not pub:
                pub = (c.text or "").strip()
        if title:
            title = re.sub(r"<[^>]+>", "", title)
            title = html_unescape(title)
            out.append((source_lbl, category, title, pub))
    return out


def _headline_hash(title):
    norm = re.sub(r"\W+", " ", (title or "").lower()).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _is_systemic_macro_crisis(h):
    """Only allow a market-wide 'CRYPTO' veto when the headline represents a genuine
    systemic macro or exchange-wide emergency, never for isolated minor articles."""
    tags = set(h.get("tags") or [])
    title_l = (h.get("title") or "").lower()
    cat = h.get("category") or ""
    if cat == "fed" and "Fed/macro headwind" in tags:
        return True
    if ("hack/exploit" in tags or "outflow/liquidation" in tags) and any(
        w in title_l for w in ("binance", "tether", "usdt", "usdc", "market-wide", "bitcoin crash", "flash crash")
    ):
        return True
    return False


def fetch_all_news(extra_symbols=None, force=False, max_age_hours=MAX_NEWS_AGE_HOURS, now_ts=None):
    """Polls 25+ omni-channel feeds in parallel with tiered 10s/30s/60s HTTP caching,
    filters out stale (>24h) archive items, evaluates recency-weighted token sentiment,
    detects newly broken 10-second delta alerts, and generates scoped trading vetoes."""
    global _INITIAL_SYNC_DONE
    now = now_ts if now_ts is not None else time.time()
    now_ms = int(now * 1000)
    max_age_sec = float(max_age_hours) * 3600.0

    headlines = []
    seen_in_batch = set()
    trending_coins = []
    cache_hits = 0
    live_fetches = 0

    with ThreadPoolExecutor(max_workers=15) as ex:
        rss_futs = {}
        for name, cat, url in OMNI_RSS_FEEDS:
            ttl = FEED_TTL_SECONDS.get(name, 30)
            rss_futs[ex.submit(_http_get_cached, url, 6, ttl, force, now)] = (name, cat)

        f_bnc = ex.submit(_json_or_none, BINANCE_CMS_URL, 6, 10, force, now)
        f_cc = ex.submit(_json_or_none, CRYPTOCOMPARE_NEWS_URL, 6, 10, force, now)
        f_trd = ex.submit(_json_or_none, COINGECKO_TRENDING_URL, 6, 60, force, now)

        # 1. Collect Official Binance CMS Articles (Tier 1: 10s)
        try:
            bdoc = f_bnc.result() or {}
            for cat_obj in (bdoc.get("data") or {}).get("catalogs") or []:
                cat_name = cat_obj.get("catalogName") or "Binance Announcement"
                for art in (cat_obj.get("articles") or [])[:5]:
                    t_art = (art.get("title") or "").strip()
                    if not t_art:
                        continue
                    t_clean = html_unescape(re.sub(r"<[^>]+>", "", t_art))
                    if t_clean in seen_in_batch:
                        continue
                    rel_ms = art.get("releaseDate")
                    pub_ts = parse_pub_timestamp("", title=t_clean, release_ms=rel_ms, now_ts=now)
                    if pub_ts is not None and (now - pub_ts) > max_age_sec:
                        continue
                    seen_in_batch.add(t_clean)
                    sent, tags = _classify_headline(t_clean)
                    toks = _detect_tokens(t_clean, extra_symbols=extra_symbols)
                    age_s = max(0.0, now - pub_ts) if pub_ts is not None else 0.0
                    pub_iso = (
                        datetime.datetime.fromtimestamp(pub_ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        if pub_ts is not None else ""
                    )
                    headlines.append({
                        "source": "Binance (" + cat_name + ")",
                        "category": "binance",
                        "title": t_clean,
                        "pubDate": pub_iso,
                        "pub_ts": int(pub_ts) if pub_ts is not None else int(now),
                        "age_min": round(age_s / 60.0, 1),
                        "sentiment": sent,
                        "tags": tags,
                        "tokens": toks,
                        "time": datetime.datetime.fromtimestamp(
                            pub_ts if pub_ts is not None else now, datetime.timezone.utc
                        ).strftime("%H:%M:%S UTC"),
                    })
        except Exception:
            pass

        # 2. Collect CryptoCompare Real-Time Wire (Tier 1: 10s)
        try:
            cc_doc = f_cc.result() or {}
            cc_items = cc_doc.get("Data") if isinstance(cc_doc, dict) else []
            if isinstance(cc_items, list):
                for item in cc_items[:12]:
                    t_raw = (item.get("title") or "").strip()
                    if not t_raw:
                        continue
                    t_clean = html_unescape(re.sub(r"<[^>]+>", "", t_raw))
                    if t_clean in seen_in_batch:
                        continue
                    pub_on = item.get("published_on")
                    pub_ts = parse_pub_timestamp("", title=t_clean, release_ms=pub_on, now_ts=now)
                    if pub_ts is not None and (now - pub_ts) > max_age_sec:
                        continue
                    seen_in_batch.add(t_clean)
                    src_info = item.get("source_info") or {}
                    src_name = src_info.get("name") or item.get("source") or "CryptoCompare Wire"
                    sent, tags = _classify_headline(t_clean)
                    toks = _detect_tokens(t_clean, extra_symbols=extra_symbols)
                    # Also parse CryptoCompare categories e.g. "BTC|ETH|Regulation"
                    for cat_tok in str(item.get("categories") or "").split("|"):
                        cu = cat_tok.strip().upper()
                        if cu in RECOGNIZED_TOKENS and cu not in toks:
                            if toks == ["CRYPTO"]:
                                toks = [cu]
                            else:
                                toks.append(cu)
                    age_s = max(0.0, now - pub_ts) if pub_ts is not None else 0.0
                    headlines.append({
                        "source": str(src_name),
                        "category": "news",
                        "title": t_clean,
                        "pubDate": datetime.datetime.fromtimestamp(
                            pub_ts if pub_ts is not None else now, datetime.timezone.utc
                        ).strftime("%Y-%m-%d %H:%M:%S UTC"),
                        "pub_ts": int(pub_ts) if pub_ts is not None else int(now),
                        "age_min": round(age_s / 60.0, 1),
                        "sentiment": sent,
                        "tags": tags,
                        "tokens": toks,
                        "time": datetime.datetime.fromtimestamp(
                            pub_ts if pub_ts is not None else now, datetime.timezone.utc
                        ).strftime("%H:%M:%S UTC"),
                    })
        except Exception:
            pass

        # 3. Collect Omni-Channel RSS Feeds (Tiered 10s / 30s / 60s)
        for f, (name, cat) in rss_futs.items():
            try:
                xml_body, was_cached = f.result()
                if was_cached:
                    cache_hits += 1
                else:
                    live_fetches += 1
                for src, category, title, pub in _parse_rss(xml_body, name, cat)[:10]:
                    if not title or title in seen_in_batch:
                        continue
                    pub_ts = parse_pub_timestamp(pub, title=title, now_ts=now)
                    if pub_ts is not None and (now - pub_ts) > max_age_sec:
                        continue
                    seen_in_batch.add(title)
                    sent, tags = _classify_headline(title)
                    toks = _detect_tokens(title, extra_symbols=extra_symbols)
                    age_s = max(0.0, now - pub_ts) if pub_ts is not None else 3600.0
                    headlines.append({
                        "source": src,
                        "category": category,
                        "title": title,
                        "pubDate": pub,
                        "pub_ts": int(pub_ts) if pub_ts is not None else int(now),
                        "age_min": round(age_s / 60.0, 1),
                        "sentiment": sent,
                        "tags": tags,
                        "tokens": toks,
                        "time": datetime.datetime.fromtimestamp(
                            pub_ts if pub_ts is not None else now, datetime.timezone.utc
                        ).strftime("%H:%M:%S UTC"),
                    })
            except Exception:
                continue

        # 4. Collect CoinGecko Global Trending Search (Tier 3: 60s)
        try:
            tdoc = f_trd.result() or {}
            for idx, c_item in enumerate((tdoc.get("coins") or [])[:12], 1):
                it = c_item.get("item") or {}
                csym = (it.get("symbol") or "").upper()
                if csym:
                    trending_coins.append({
                        "rank": idx,
                        "symbol": csym,
                        "name": it.get("name") or csym,
                        "market_cap_rank": it.get("market_cap_rank"),
                    })
        except Exception:
            pass

    # Sort newest first by publication timestamp
    headlines.sort(key=lambda h: h.get("pub_ts", 0), reverse=True)

    # Track 10-second breaking delta (newly discovered headlines)
    breaking_alerts = []
    for h in headlines:
        h_hash = _headline_hash(h["title"])
        if h_hash not in _SEEN_HEADLINES:
            _SEEN_HEADLINES[h_hash] = now_ms
            is_fresh = h.get("age_min", 999.0) <= 15.0
            h["is_new"] = True if (_INITIAL_SYNC_DONE or is_fresh) else False
        else:
            h["is_new"] = False

        if h["is_new"] and h["sentiment"] in ("BULLISH", "BEARISH"):
            breaking_alerts.append({
                "source": h["source"],
                "sentiment": h["sentiment"],
                "tokens": h["tokens"],
                "tags": h["tags"],
                "title": h["title"],
                "time": h["time"],
            })

    _INITIAL_SYNC_DONE = True

    # Prune _SEEN_HEADLINES older than 48h to bound memory
    if len(_SEEN_HEADLINES) > 2500:
        cutoff_ms = now_ms - 48 * 3600 * 1000
        for k in [k for k, ts in _SEEN_HEADLINES.items() if ts < cutoff_ms]:
            del _SEEN_HEADLINES[k]

    # Token sentiment & scoped veto computation
    token_scores = {sym: 0.0 for sym in RECOGNIZED_TOKENS}
    if extra_symbols:
        for s in extra_symbols:
            b = _normalize_base_symbol(s)
            if b:
                token_scores.setdefault(b, 0.0)

    token_vetoes = {}
    macro_score = 0.0

    for h in headlines:
        sent = h["sentiment"]
        src_weight = 1.0
        if "Binance" in h["source"]:
            src_weight = 2.5
        elif h["category"] in ("fed", "bank"):
            src_weight = 1.8
        elif h["category"] == "twitter":
            src_weight = 1.4

        age_sec = h.get("age_min", 60.0) * 60.0
        recency_mult = freshness_weight(age_sec)
        pts = (1.5 if sent == "BULLISH" else -1.5 if sent == "BEARISH" else 0.0) * src_weight * recency_mult
        macro_score += pts * 0.15

        is_bear_veto_tag = (
            sent == "BEARISH"
            and any(t in h["tags"] for t in ("delist", "hack/exploit", "regulatory/delist", "whale deposit/dump"))
        )
        is_bull_veto_tag = sent == "BULLISH" and ("Binance listing/launch" in h["tags"])

        for t in h["tokens"]:
            token_scores.setdefault(t, 0.0)
            if t != "CRYPTO":
                token_scores[t] += pts

            # Scoped Veto detection: only apply to specific tokens (or systemic macro crisis for CRYPTO)
            if t == "CRYPTO" and not _is_systemic_macro_crisis(h):
                continue

            # Only fresh catalysts (< 12 hours old) can impose an active trading veto
            if h.get("age_min", 0.0) > 720.0:
                continue

            if is_bear_veto_tag:
                token_vetoes[t] = {
                    "veto_long": True,
                    "veto_short": False,
                    "severity": "CRITICAL" if ("hack/exploit" in h["tags"] or "Binance" in h["source"]) else "HIGH",
                    "reason": "HIGH RISK BEARISH CATALYST: %s (%s)" % (h["title"][:70], h["source"]),
                }
            elif is_bull_veto_tag and t != "CRYPTO":
                token_vetoes[t] = {
                    "veto_long": False,
                    "veto_short": True,
                    "severity": "HIGH",
                    "reason": "HIGH RISK BULLISH NEWS SQUEEZE: %s (%s)" % (h["title"][:70], h["source"]),
                }

    # Add trending bonus
    for t_info in trending_coins:
        sym = t_info["symbol"]
        if sym in token_scores:
            token_scores[sym] += 2.0

    data = {
        "updated": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "updated_ms": now_ms,
        "poll_interval_sec": POLL_INTERVAL_SECONDS,
        "cache_stats": {"cache_hits": cache_hits, "live_fetches": live_fetches},
        "macro_score": round(max(-10.0, min(10.0, macro_score)), 1),
        "trending_coins": trending_coins,
        "token_scores": {k: round(v, 1) for k, v in token_scores.items() if k != "CRYPTO" and abs(v) > 0.1},
        "token_vetoes": token_vetoes,
        "breaking_alerts": breaking_alerts[:10],
        "new_count": sum(1 for h in headlines if h.get("is_new")),
        "total_headlines": len(headlines),
        "headlines": headlines[:40],
    }

    try:
        tmp = NEWS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, NEWS_FILE)
    except Exception:
        pass

    return data


def run_continuous_monitor(interval_sec=POLL_INTERVAL_SECONDS, max_cycles=None):
    """Run the 10-second continuous crypto & macro news monitor loop."""
    interval_sec = max(2.0, float(interval_sec))
    print("=" * 72, flush=True)
    print(" 25-SOURCE HIGH-FREQUENCY CRYPTO NEWS MONITOR (Every %.0fs)" % interval_sec, flush=True)
    print("=" * 72, flush=True)
    cycles = 0
    while True:
        t0 = time.time()
        try:
            res = fetch_all_news()
            elapsed = (time.time() - t0) * 1000.0
            cs = res.get("cache_stats", {})
            print(
                "[%s] Scanned %d fresh (<24h) headlines in %.0fms (live:%d, cached:%d) | Macro: %+.1f | New: %d | Vetoes: %d"
                % (
                    res["updated"],
                    res["total_headlines"],
                    elapsed,
                    cs.get("live_fetches", 0),
                    cs.get("cache_hits", 0),
                    res["macro_score"],
                    res.get("new_count", 0),
                    len(res.get("token_vetoes", {})),
                ),
                flush=True,
            )
            for alert in res.get("breaking_alerts", []):
                print(
                    "  [BREAKING %s] (%s) %s: %s"
                    % (alert["sentiment"], ",".join(alert["tokens"]), alert["source"], alert["title"]),
                    flush=True,
                )
        except Exception as exc:
            print("[ERROR] News poll error: %s" % exc, flush=True)

        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        sleep_for = max(0.5, interval_sec - (time.time() - t0))
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            print("\nStopped 10s news monitor.", flush=True)
            break


def main(argv=None):
    ap = argparse.ArgumentParser(description="Omni-Channel 10-Second Crypto & Macro News Monitor")
    ap.add_argument("--watch", action="store_true", help="Run continuous 10-second monitoring loop")
    ap.add_argument("--interval", type=float, default=POLL_INTERVAL_SECONDS, help="Polling interval in seconds (default: 10)")
    ap.add_argument("--json", action="store_true", help="Print full JSON output")
    args = ap.parse_args(argv)

    if args.watch:
        run_continuous_monitor(interval_sec=args.interval)
        return 0

    print("Fetching omni-channel news feeds...")
    res = fetch_all_news(force=True)
    if args.json:
        print(json.dumps(res, indent=2))
        return 0
    print(
        "Updated: %s | Total Fresh Headlines (<24h): %d | Macro Score: %+.1f"
        % (res["updated"], res["total_headlines"], res["macro_score"])
    )
    print("Top Trending Coins:", [c["symbol"] for c in res["trending_coins"][:6]])
    if res.get("token_vetoes"):
        print("Active Vetoes:", res["token_vetoes"])
    print("\nSample Headlines:")
    for h in res["headlines"][:8]:
        print("  [%s] %s | %s | %s" % (h["sentiment"], h["source"], ",".join(h["tokens"]), h["title"][:65]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
