"""Price lookup with disk cache. yfinance for both historical and current.

Cache lives in .cache/prices/{TICKER}.csv with one row per trading day:
    date,open,high,low,close
We fetch the trailing ~15 months on miss and cache. `current_price` is the
last close in the cache (refreshed at most once per run).

The OHLC is stored (not just close) so the email can show the *actual* price
range a stock traded at on a member's transaction date. Note: congressional
filings record a transaction DATE only — never a time — so there is no
intraday timestamp to match. The honest "actual price" for a trade is the
real market data for that calendar day: its open/high/low/close.
"""
from __future__ import annotations

import csv
import logging
from collections import namedtuple
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance as yf

# Silence yfinance's chatty "possibly delisted" stderr noise for tickers we
# can't find. We still skip those tickers in the ranking — just quietly.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

Bar = namedtuple("Bar", ["open", "high", "low", "close"])


def _yf_symbol(ticker: str) -> str:
    """Translate disclosure-style tickers to yfinance form.

    Congressional filings write Berkshire as 'BRK.B'; yfinance wants 'BRK-B'.
    Same for any dot-class-share ticker.
    """
    return ticker.replace(".", "-")


CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "prices"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_FETCH_AGE = timedelta(hours=12)


def _csv_path(ticker: str) -> Path:
    return CACHE_DIR / f"{ticker.upper().replace('/', '_')}.csv"


def _read_cache(ticker: str) -> dict[date, Bar]:
    p = _csv_path(ticker)
    if not p.exists():
        return {}
    out: dict[date, Bar] = {}
    with p.open() as fh:
        for row in csv.reader(fh):
            try:
                d = datetime.strptime(row[0], "%Y-%m-%d").date()
                out[d] = Bar(float(row[1]), float(row[2]), float(row[3]), float(row[4]))
            except (ValueError, IndexError):
                continue
    return out


def _write_cache(ticker: str, bars: dict[date, Bar]) -> None:
    p = _csv_path(ticker)
    with p.open("w") as fh:
        w = csv.writer(fh)
        for d in sorted(bars):
            b = bars[d]
            w.writerow([d.isoformat(), b.open, b.high, b.low, b.close])


def _cache_age(ticker: str) -> timedelta:
    p = _csv_path(ticker)
    if not p.exists():
        return timedelta(days=999)
    return datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)


def _refresh(ticker: str, lookback_days: int = 460) -> dict[date, Bar]:
    start = date.today() - timedelta(days=lookback_days)
    yf_sym = _yf_symbol(ticker)
    try:
        import contextlib, io as _io
        with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
            df = yf.download(
                yf_sym,
                start=start.isoformat(),
                progress=False,
                auto_adjust=True,
                threads=False,
            )
    except Exception:
        return {}
    if df is None or df.empty:
        return {}
    # yfinance returns a MultiIndex DataFrame even for a single ticker — flatten.
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        try:
            df = df.xs(yf_sym, axis=1, level=-1)
        except Exception:
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    needed = {"Open", "High", "Low", "Close"}
    if not needed.issubset(set(df.columns)):
        return {}
    bars: dict[date, Bar] = {}
    for ts, row in df.iterrows():
        try:
            d = ts.date() if hasattr(ts, "date") else ts
            o, h, lo, c = (float(row["Open"]), float(row["High"]),
                           float(row["Low"]), float(row["Close"]))
        except (TypeError, ValueError, KeyError):
            continue
        # NaN check (NaN != NaN)
        if c == c and o == o and h == h and lo == lo:
            bars[d] = Bar(o, h, lo, c)
    _write_cache(ticker, bars)
    return bars


def history(ticker: str) -> dict[date, Bar]:
    if _cache_age(ticker) > _FETCH_AGE:
        return _refresh(ticker)
    cached = _read_cache(ticker)
    return cached or _refresh(ticker)


def bar_on(ticker: str, on: date) -> tuple[date, Bar] | None:
    """Actual OHLC bar for `on`, or the most recent prior trading day.

    Returns (matched_trading_day, Bar). The matched day may differ from `on`
    when `on` is a weekend/holiday — markets were closed, so the nearest
    prior session is the real price context.
    """
    h = history(ticker)
    if not h:
        return None
    candidates = [d for d in h if d <= on]
    if not candidates:
        return None
    d = max(candidates)
    return d, h[d]


def price_on(ticker: str, on: date) -> float | None:
    """Closing price on `on`, or the most recent prior trading day."""
    res = bar_on(ticker, on)
    return res[1].close if res else None


def current_price(ticker: str) -> float | None:
    h = history(ticker)
    if not h:
        return None
    return h[max(h)].close
