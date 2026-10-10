"""Fetch latest prices (including pre-market where Yahoo provides it) for every
ticker in watchlist.json and write data.json with % change vs. the previous
regular-session close."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).parent
watchlist = json.loads((ROOT / "watchlist.json").read_text())


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
        out["quotes"][symbol] = quote

(ROOT / "data.json").write_text(json.dumps(out, indent=2))
print(f"Wrote data.json ({total - failures}/{total} symbols OK)")
if total and failures == total:
    sys.exit(1)
