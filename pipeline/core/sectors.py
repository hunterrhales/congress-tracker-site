"""Ticker -> sector/industry lookup with a persistent disk cache.

Sectors change rarely, so we cache aggressively. yfinance's `.info` is slow
and occasionally flaky, so every lookup is wrapped — a failure just yields
"Unknown" rather than crashing the daily run.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import yfinance as yf

from core.prices import _yf_symbol

CACHE_FILE = Path(__file__).resolve().parent.parent / ".cache" / "sectors.json"
CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

_cache: dict[str, str] | None = None


def _load() -> dict[str, str]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            _cache = {}
    return _cache


def save() -> None:
    if _cache is not None:
        CACHE_FILE.write_text(json.dumps(_cache, indent=0, sort_keys=True))


def sector_for(ticker: str) -> str:
    """Return the GICS-ish sector for a ticker, 'Unknown' if unavailable."""
    cache = _load()
    key = ticker.upper()
    if key in cache:
        return cache[key]
    sector = "Unknown"
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            info = yf.Ticker(_yf_symbol(ticker)).get_info()
        sector = (info or {}).get("sector") or "Unknown"
    except Exception:
        sector = "Unknown"
    cache[key] = sector
    return sector


def warm(tickers: list[str], cap: int = 200) -> None:
    """Pre-fetch sectors for a batch of tickers (bounded), then persist."""
    seen = 0
    for t in tickers:
        if seen >= cap:
            break
        if t.upper() not in _load():
            sector_for(t)
            seen += 1
    save()
