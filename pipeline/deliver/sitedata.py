"""Build the data.json payload for the dashboard site.

The site is a static shell (site/index.html) that renders this JSON client-side
and polls it for updates. The cron regenerates data.json each run and pushes it
to GitHub Pages.

Unlike the old email (which showed a "new since last email" diff), the site
feed shows EVERY disclosure filed in a recent window every time, so it is never
mysteriously empty. "New in last update" is computed by diffing this run's feed
against the previously published data.json.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

from core import committees, sectors
from core.normalize import Trade

SITE_DIR = Path(__file__).resolve().parent.parent / "site"
# DATA_OUT lets the cloud build write data.json to the repo root instead of
# the local site/ dir. Both the writer (run.py) and prev-key loader use it.
DATA_FILE = Path(os.environ["DATA_OUT"]) if os.environ.get("DATA_OUT") else SITE_DIR / "data.json"
FEED_CAP = 900


def trade_key(t: Trade) -> str:
    return (f"{t.chamber}:{t.filing_id}:{t.ticker}:{t.txn_date.isoformat()}"
            f":{t.action}:{t.raw_amount}")


def _cmtes(name: str, lookup, max_shown: int = 6) -> list[str]:
    cs = committees.committees_for(name, lookup)
    cs = [c for c in cs
          if "Commission" not in c and "Caucus" not in c
          and "Subcommittee" not in c and len(c) <= 35]
    return cs[:max_shown]


def _trade_row(t: Trade, lookup) -> dict:
    return {
        "filed": t.notification_date.isoformat(),
        "txn": t.txn_date.isoformat(),
        "action": t.action,
        "ticker": t.ticker,
        "asset": t.asset_name[:70],
        "amount": t.raw_amount,
        "amount_mid": t.amount_mid,
        "px_close": t.px_close,
        "px_low": t.px_low,
        "px_high": t.px_high,
        "member": t.member,
        "chamber": t.chamber,
        "state": getattr(t, "state", ""),
        "committees": _cmtes(t.member, lookup),
        "sector": sectors.sector_for(t.ticker),
        "lag_days": t.disclosure_lag_days,
        "owner": t.owner,
        "source_url": t.source_url,
        "key": trade_key(t),
    }


def load_prev_keys() -> set[str]:
    """Feed keys from the previously published data.json (for new-detection)."""
    try:
        prev = json.loads(DATA_FILE.read_text())
        return {r["key"] for r in prev.get("feed", [])}
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return set()


def build_payload(*, feed_trades, new_keys, ranking, trends, review,
                  alpaca_account, alpaca_positions, paper_filings_skipped,
                  data_warnings, feed_days) -> dict:
    lookup = committees.load_assignments()
    today = date.today()

    feed = [_trade_row(t, lookup) for t in feed_trades]
    feed.sort(key=lambda r: (r["filed"], r["txn"]), reverse=True)
    feed = feed[:FEED_CAP]

    # committee universe for the dropdown
    all_cmtes = sorted({c for r in feed for c in r["committees"]})

    performers = []
    for i, ms in enumerate(ranking[:5], 1):
        member_trades = sorted(ms.recent_trades, key=lambda x: x.txn_date, reverse=True)
        rows = [_trade_row(t, lookup) for t in member_trades]
        last_buy = next((r for r in rows if r["action"] == "buy"), None)
        last_sell = next((r for r in rows if r["action"] == "sell"), None)
        performers.append({
            "rank": i,
            "member": ms.member,
            "chamber": ms.chamber,
            "return_pct": round(ms.return_pct, 1),
            "realized": round(ms.realized_pnl, 0),
            "unrealized": round(ms.unrealized_pnl, 0),
            "trade_count": ms.trade_count,
            "notional": ms.total_notional,
            "committees": _cmtes(ms.member, lookup, 8),
            "last_buy": last_buy,
            "last_sell": last_sell,
            "open_positions": sorted(
                ({"ticker": tk, "shares": round(sh, 1),
                  "avg": round(ms.avg_cost.get(tk, 0), 2)}
                 for tk, sh in ms.open_positions.items() if sh > 0),
                key=lambda x: -x["shares"])[:12],
            "trades": rows,
        })

    trends_out = None
    if trends is not None:
        trends_out = {
            "window_days": trends.window_days,
            "consensus_buys": [
                {"ticker": tt.ticker, "sector": tt.sector,
                 "buyers": len(tt.buyers), "sellers": len(tt.sellers),
                 "buy_usd": tt.buy_usd, "sell_usd": tt.sell_usd}
                for tt in trends.consensus_buys],
            "consensus_sells": [
                {"ticker": tt.ticker, "sector": tt.sector,
                 "buyers": len(tt.buyers), "sellers": len(tt.sellers),
                 "buy_usd": tt.buy_usd, "sell_usd": tt.sell_usd}
                for tt in trends.consensus_sells],
            "sector_flows": [
                {"sector": s.sector, "buyers": len(s.buyers),
                 "sellers": len(s.sellers), "net_usd": s.net_usd,
                 "buy_usd": s.buy_usd, "sell_usd": s.sell_usd}
                for s in trends.sector_flows],
        }

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generated_date": today.isoformat(),
        "feed_days": feed_days,
        "review": review or [],
        "warnings": data_warnings or [],
        "paper_filings_skipped": paper_filings_skipped,
        "new_count": len(new_keys),
        "new_keys": sorted(new_keys),
        "committee_options": all_cmtes,
        "feed": feed,
        "trends": trends_out,
        "performers": performers,
        "alpaca": None if alpaca_account is None else {
            "equity": alpaca_account.equity,
            "cash": alpaca_account.cash,
            "positions": [
                {"ticker": p.ticker, "qty": p.qty, "avg": p.avg_entry_price,
                 "mv": p.market_value, "plpc": p.unrealized_plpc}
                for p in alpaca_positions],
        },
    }
