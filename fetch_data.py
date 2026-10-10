"""Fetch latest prices (including pre-market where Yahoo provides it) and recent
news for every ticker in watchlist.json, and write data.json.

Prices: % change vs. the previous regular-session close.
News:   Yahoo Finance headlines plus a Google News search (which covers CNBC,
        BNN Bloomberg, Reuters and others), kept for the last 12 hours. A news
        failure never stops the prices from being saved.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).parent
NEWS_HOURS = 12      # the page's slider goes up to this
MAX_ARTICLES = 12    # per ticker
USER_AGENT = "Mozilla/5.0 (compatible; market-watch/1.0)"

# Used when a ticker has no "news" search term in watchlist.json.
DEFAULT_NEWS_QUERIES = {
    "CL=F": "crude oil prices",
    "NG=F": "natural gas prices",
    "^TNX": "10-year Treasury yield",
    "CADUSD=X": "Canadian dollar loonie",
    "VET": "Vermilion Energy",
    "VG": "Venture Global",
    "AMD": "Advanced Micro Devices AMD",
    "TM": "Toyota Motor",
}


# ---------------------------------------------------------------- prices
def fetch(symbol):
    t = yf.Ticker(symbol)
    daily = t.history(period="10d", interval="1d", prepost=False)
    intraday = t.history(period="2d", interval="5m", prepost=True)
    if daily.empty:
        raise ValueError("no data from Yahoo for this symbol")
    if intraday.empty:
        price, ts = float(daily["Close"].iloc[-1]), daily.index[-1]
    else:
        price, ts = float(intraday["Close"].iloc[-1]), intraday.index[-1]
    # previous close = last daily close dated before the latest quote's date
    prior = daily[daily.index.date < ts.date()]
    if prior.empty:
        prev = float(daily["Close"].iloc[-2]) if len(daily) > 1 else None
    else:
        prev = float(prior["Close"].iloc[-1])
    if not prev:
        raise ValueError("no previous close")
    return {
        "price": round(price, 4),
        "prev_close": round(prev, 4),
        "change_pct": round((price - prev) / prev * 100, 3),
        "quote_time": ts.isoformat(),
    }


def lookup_name(symbol):
    """Used only when a ticker was added without a display name."""
    try:
        info = yf.Ticker(symbol).get_info()
        return info.get("shortName") or info.get("longName")
    except Exception:
        return None


# ------------------------------------------------------------------ news
def _parse_iso(text):
    when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def parse_yahoo_item(item):
    """Handles both the newer ({'content': {...}}) and older yfinance layouts."""
    if not isinstance(item, dict):
        return None
    try:
        content = item.get("content")
        if isinstance(content, dict):
            title = content.get("title")
            url = (content.get("canonicalUrl") or {}).get("url") or (content.get("clickThroughUrl") or {}).get("url")
            source = (content.get("provider") or {}).get("displayName")
            when = _parse_iso(content.get("pubDate") or content.get("displayTime"))
        else:
            title = item.get("title")
            url = item.get("link")
            source = item.get("publisher")
            when = datetime.fromtimestamp(item["providerPublishTime"], timezone.utc)
    except Exception:
        return None
    if not (title and url) or not str(url).lower().startswith(("http://", "https://")):
        return None
    return {"title": title.strip(), "url": url, "source": source or "Yahoo Finance",
            "when": when, "via": "Yahoo Finance"}


def yahoo_news(symbol):
    t = yf.Ticker(symbol)
    try:
        items = t.get_news(count=20)
    except TypeError:  # older yfinance without the count argument
        items = t.news
    return [a for a in (parse_yahoo_item(i) for i in (items or [])) if a]


def google_news(query):
    q = urllib.parse.quote_plus(f"{query} when:{NEWS_HOURS}h")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-CA&gl=CA&ceid=CA:en"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        root = ET.fromstring(resp.read())
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        src_el = item.find("source")
        source = ((src_el.text or "").strip() if src_el is not None else "")
        try:
            when = parsedate_to_datetime(item.findtext("pubDate")).astimezone(timezone.utc)
        except Exception:
            continue
        if source and title.endswith(" - " + source):  # Google appends " - Publisher"
            title = title[: -len(source) - 3].rstrip()
        if title and link.lower().startswith(("http://", "https://")):
            out.append({"title": title, "url": link, "source": source or "Google News",
                        "when": when, "via": "Google News"})
    return out


def news_query(item, name):
    if item.get("news"):
        return str(item["news"])
    sym = item["symbol"]
    if sym in DEFAULT_NEWS_QUERIES:
        return DEFAULT_NEWS_QUERIES[sym]
    cleaned = re.sub(r"\([^)]*\)", "", name or "").strip()
    return cleaned or sym


RETRY_DELAYS = (3, 8)  # seconds to wait before the 2nd and 3rd try


def http_status(e):
    """HTTP status code behind an error, whether it came from urllib or requests."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code
    return getattr(getattr(e, "response", None), "status_code", None)


def describe_error(e):
    code = http_status(e)
    return f"HTTP {code}" if code else type(e).__name__


def retryable(e):
    code = http_status(e)
    if code:
        return code == 429 or code >= 500  # rate-limited or the site had a hiccup
    return isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError)) or "RateLimit" in type(e).__name__


def with_retries(fn):
    for i in range(len(RETRY_DELAYS) + 1):
        try:
            return fn()
        except Exception as e:
            if i == len(RETRY_DELAYS) or not retryable(e):
                raise
            print(f"  {describe_error(e)}, retrying in {RETRY_DELAYS[i]}s", file=sys.stderr)
            time.sleep(RETRY_DELAYS[i])


def collect_news(symbol, query, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=NEWS_HOURS)
    found, errors = [], []
    for label, getter in (("Yahoo", lambda: yahoo_news(symbol)), ("Google News", lambda: google_news(query))):
        try:
            found += with_retries(getter)
        except Exception as e:
            errors.append(f"{label}: {describe_error(e)}")
            print(f"NEWS {label} failed for {symbol}: {e}", file=sys.stderr)
    seen, out = set(), []
    for a in sorted(found, key=lambda a: a["when"], reverse=True):
        if a["when"] < cutoff or a["when"] > now + timedelta(minutes=10):
            continue
        key = re.sub(r"\W+", " ", a["title"].lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": a["title"], "url": a["url"], "source": a["source"],
                    "published": a["when"].isoformat(), "via": a["via"]})
    return out[:MAX_ARTICLES], errors


# ------------------------------------------------------------------ main
def main():
    watchlist = json.loads((ROOT / "watchlist.json").read_text())
    out = {"updated": datetime.now(timezone.utc).isoformat(), "quotes": {}}
    total = failures = 0
    for group, items in watchlist.items():
        if not isinstance(items, list):
            continue
        for item in items:
            symbol = str(item.get("symbol", "")).strip()
            if not symbol or symbol in out["quotes"]:
                continue
            total += 1
            try:
                quote = fetch(symbol)
            except Exception as e:  # keep going so one bad symbol doesn't kill the run
                quote = {"error": str(e)}
                failures += 1
                print(f"FAILED {symbol}: {e}", file=sys.stderr)
            quote["name"] = item.get("name") or lookup_name(symbol) or symbol
            try:
                news, errors = collect_news(symbol, news_query(item, quote["name"]))
            except Exception as e:  # never let news break the prices
                news, errors = [], [f"news: {type(e).__name__}"]
            quote["news"] = news
            if errors:
                quote["news_errors"] = errors
            print(f"  {symbol}: {len(news)} news articles" + (f" ({'; '.join(errors)})" if errors else ""))
            out["quotes"][symbol] = quote
            time.sleep(0.5)  # be polite to the news sites

    (ROOT / "data.json").write_text(json.dumps(out, indent=2))
    print(f"Wrote data.json ({total - failures}/{total} symbols OK)")
    if total and failures == total:
        sys.exit(1)


if __name__ == "__main__":
    main()
