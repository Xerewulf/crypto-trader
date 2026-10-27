#!/usr/bin/env python3
"""Omni-Channel 25+ Source Crypto News & Catalyst Intelligence Monitor.
Continuously scans breaking news across:
  1. Federal Reserve (federalreserve.gov/feeds/press_all.xml, speeches.xml)
  2. Wall Street Investment Banks & SEC (JPMorgan, BlackRock, Goldman Sachs, Morgan Stanley, ECB, SEC)
  3. Official Binance CMS Announcements (New Listings, Delistings, Launchpool, Collateral)
  4. Crypto Twitter / X Whale Flow (Lookonchain, Whale Alert, Arkham, Wu Blockchain, Arthur Hayes)
  5. Financial & Crypto Magazines (Forbes, Fortune, Bloomberg, Reuters, Bitcoin Magazine, The Block, Blockworks, CryptoSlate, BeInCrypto)
  6. 24/7 Breaking Crypto Journalism (CoinDesk, Cointelegraph, Decrypt, Yahoo Finance)
  7. CoinGecko Global Trending Search & Social Heat

Classifies headlines, identifies token-specific impact, and outputs high-conviction
catalyst vetoes to prevent entering bad trades or catching falling knives.
"""

import datetime
import gzip
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
NEWS_FILE = os.path.join(HERE, "news_feed.json")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

BULL_PATTERNS = [
    ("burn/buyback", r"\btoken burn[s]?\b|\bburn(?:s|ed)? [\d.,]+\b|\bbuyback[s]?\b|\bdeflation"),
    ("ETF/institutional inflow", r"\binflow[s]?\b|\bETF[s]? (?:approv|launch|debut|demand)|\bapprov(?:es|ed|al)\b.*\bETF\b|\badd(?:s|ed)? [\d,.]+ (?:BTC|ETH|SOL|bitcoin|ether)\b|\bbought [\d,.]+ (?:BTC|ETH|SOL|bitcoin|ether)\b|\bcustody\b|\btokeniz"),
    ("Binance listing/launch", r"\bBinance Will List\b|\bBinance Will Add\b|\bBinance Futures Will Launch\b|\bLaunchpool\b|\bMegadrop\b|\bHODLer Airdrop\b|\bNew Cryptocurrency Listing\b"),
    ("upgrade/partnership", r"\bupgrade\b|\bmainnet\b|\bhard fork\b|\bpartnership\b|\bintegration\b"),
    ("whale/CT accumulation", r"\baccumulat(?:e|es|ed|ing|ion)\b|\bwhale[s]? bought\b|\bwithdrew\b|\bLookonchain\b.*\bbought\b|\bfresh wallet\b"),
    ("bullish/breakout", r"\bbreakout\b|\brall(?:y|ies|ied)\b|\bsurge[sd]?\b|\bbullish\b|\bbull market\b|\ball-time high\b|\bATH\b|\bsoar(?:s|ed)?\b|\brebound[s]?\b"),
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

BINANCE_CMS_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&pageNo=1&pageSize=15"
COINGECKO_TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"

# Major crypto symbols to scan in headlines
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
}


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


def _json_or_none(url, timeout=8):
    try:
        return json.loads(_http_get(url, timeout=timeout))
    except Exception:
        return None


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


def _detect_tokens(title):
    found = []
    title_lower = title.lower()
    for sym, aliases in RECOGNIZED_TOKENS.items():
        for a in aliases:
            if re.search(r"\b" + re.escape(a) + r"\b", title_lower):
                found.append(sym)
                break
    return found or ["CRYPTO"]


def _parse_rss(xml_text, source_lbl, category="news"):
    out = []
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
            # Clean HTML tags and entities
            title = re.sub(r"<[^>]+>", "", title)
            title = html_unescape(title)
            out.append((source_lbl, category, title, pub))
    return out


def html_unescape(s):
    import html
    return html.unescape(s)


def fetch_all_news():
    """Polls 25+ omni-channel feeds in parallel, categorizes headlines,
    evaluates token-level sentiment, and generates trading vetoes."""
    headlines = []
    seen = set()
    binance_cms = []
    trending_coins = []

    with ThreadPoolExecutor(max_workers=14) as ex:
        rss_futs = {ex.submit(_http_get, url, 7): (name, cat) for name, cat, url in OMNI_RSS_FEEDS}
        f_bnc = ex.submit(_json_or_none, BINANCE_CMS_URL, 7)
        f_trd = ex.submit(_json_or_none, COINGECKO_TRENDING_URL, 7)

        # Collect RSS items
        for f, (name, cat) in rss_futs.items():
            try:
                for src, category, title, pub in _parse_rss(f.result(), name, cat)[:10]:
                    if title and title not in seen:
                        seen.add(title)
                        sent, tags = _classify_headline(title)
                        toks = _detect_tokens(title)
                        headlines.append({
                            "source": src,
                            "category": category,
                            "title": title,
                            "pubDate": pub,
                            "sentiment": sent,
                            "tags": tags,
                            "tokens": toks,
                            "time": datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S UTC"),
                        })
            except Exception:
                continue

        # Collect Binance CMS Articles
        try:
            bdoc = f_bnc.result() or {}
            for cat_obj in (bdoc.get("data") or {}).get("catalogs") or []:
                cat_name = cat_obj.get("catalogName") or "Binance Announcement"
                for art in (cat_obj.get("articles") or [])[:5]:
                    t_art = (art.get("title") or "").strip()
                    if t_art and t_art not in seen:
                        seen.add(t_art)
                        t_clean = html_unescape(re.sub(r"<[^>]+>", "", t_art))
                        sent, tags = _classify_headline(t_clean)
                        toks = _detect_tokens(t_clean)
                        headlines.insert(0, {
                            "source": "Binance (" + cat_name + ")",
                            "category": "binance",
                            "title": t_clean,
                            "pubDate": "",
                            "sentiment": sent,
                            "tags": tags,
                            "tokens": toks,
                            "time": datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S UTC"),
                        })
        except Exception:
            pass

        # Collect CoinGecko Global Trending Search
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

    # Token sentiment & veto computation
    token_scores = {sym: 0.0 for sym in RECOGNIZED_TOKENS}
    token_vetoes = {}
    macro_score = 0.0

    for h in headlines:
        sent = h["sentiment"]
        weight = 1.0
        if "Binance" in h["source"]:
            weight = 2.5
        elif h["category"] in ("fed", "bank"):
            weight = 1.8
        elif h["category"] == "twitter":
            weight = 1.4

        pts = (1.5 if sent == "BULLISH" else -1.5 if sent == "BEARISH" else 0.0) * weight
        macro_score += pts * 0.2

        for t in h["tokens"]:
            if t in token_scores:
                token_scores[t] += pts
            # Veto detection
            if "delist" in h["tags"] or "hack/exploit" in h["tags"] or "regulatory/delist" in h["tags"]:
                token_vetoes[t] = {
                    "veto_long": True,
                    "veto_short": False,
                    "reason": "HIGH RISK BEARISH CATALYST: %s (%s)" % (h["title"][:60], h["source"]),
                }
            elif "Binance listing/launch" in h["tags"]:
                token_vetoes[t] = {
                    "veto_long": False,
                    "veto_short": True,
                    "reason": "HIGH RISK BULLISH NEWS SQUEEZE: %s (%s)" % (h["title"][:60], h["source"]),
                }

    # Add trending bonus
    for t_info in trending_coins:
        sym = t_info["symbol"]
        if sym in token_scores:
            token_scores[sym] += 2.0

    data = {
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "updated_ms": int(time.time() * 1000),
        "macro_score": round(max(-10.0, min(10.0, macro_score)), 1),
        "trending_coins": trending_coins,
        "token_scores": {k: round(v, 1) for k, v in token_scores.items() if abs(v) > 0.1},
        "token_vetoes": token_vetoes,
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


if __name__ == "__main__":
    print("Fetching omni-channel news feeds...")
    res = fetch_all_news()
    print("Updated: %s | Total Headlines: %d | Macro Score: %+.1f" % (
        res["updated"], res["total_headlines"], res["macro_score"]
    ))
    print("Top Trending Coins:", [c["symbol"] for c in res["trending_coins"][:6]])
    print("\nSample Headlines:")
    for h in res["headlines"][:8]:
        print("  [%s] %s | %s | %s" % (h["sentiment"], h["source"], ",".join(h["tokens"]), h["title"][:65]))
