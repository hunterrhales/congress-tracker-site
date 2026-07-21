"""Generate a plain-English 'what's trending' read for the top of each email.

This is rule-based template synthesis — the daily email is sent by an
unattended cron job with no language model in the loop. Every sentence is
derived directly from the computed data, and phrasing stays descriptive
(what the data shows), never prescriptive (what the reader should do).
"""
from __future__ import annotations

from collections import Counter

from core import sectors


def _money(x: float) -> str:
    sign = "-" if x < 0 else "+"
    return f"{sign}${abs(x):,.0f}"


def build(*, new_trades, trends, ranking, today) -> list[str]:
    """Return an ordered list of short takeaway sentences."""
    L: list[str] = []
    window = trends.window_days if trends is not None else 90

    # --- 1. Today's activity ------------------------------------------------
    daily = list(new_trades)
    filed_today = [t for t in daily if t.notification_date == today]
    if daily:
        buys = sum(1 for t in daily if t.action == "buy")
        sells = sum(1 for t in daily if t.action == "sell")
        sec_counts = Counter(sectors.sector_for(t.ticker) for t in daily)
        sec_counts.pop("Unknown", None)
        top_sec = sec_counts.most_common(1)[0][0] if sec_counts else None
        msg = (f"{len(daily)} newly disclosed trade{'s' if len(daily) != 1 else ''} since the last refresh"
               + (f" ({len(filed_today)} filed today)" if filed_today else "")
               + f": {buys} buy{'s' if buys != 1 else ''} vs {sells} sell{'s' if sells != 1 else ''}")
        if top_sec:
            msg += f", most active in {top_sec}"
        L.append(msg + ".")
        # A ticker repeated across new filings is a cluster worth noting.
        tick_counts = Counter(t.ticker for t in daily)
        tk, n = tick_counts.most_common(1)[0]
        if n >= 2:
            L.append(f"{tk} showed up in {n} of the newly disclosed trades.")
    else:
        L.append(f"No new filings since the last refresh — the read below reflects "
                 f"the last {window} days of disclosed trading.")

    # --- 2. Cross-Congress trend takeaways ---------------------------------
    if trends is not None:
        if trends.consensus_buys:
            tt = trends.consensus_buys[0]
            extra = f", though {len(tt.sellers)} sold" if tt.sellers else ""
            L.append(f"Broadest buying is in {tt.ticker} ({tt.sector}) — "
                     f"{len(tt.buyers)} different members bought it over the last {window} days{extra}.")
        if trends.consensus_sells:
            tt = trends.consensus_sells[0]
            extra = f" against {len(tt.buyers)} buying" if tt.buyers else ""
            L.append(f"Broadest selling is in {tt.ticker} ({tt.sector}) — "
                     f"{len(tt.sellers)} members sold{extra}.")
        if trends.sector_flows:
            net = sum(s.net_usd for s in trends.sector_flows)
            sells_lean = sum(1 for s in trends.sector_flows if len(s.sellers) > len(s.buyers))
            buys_lean = sum(1 for s in trends.sector_flows if len(s.buyers) > len(s.sellers))
            if net < 0:
                heaviest = min(trends.sector_flows, key=lambda s: s.net_usd)
                L.append(f"Across sectors, members leaned net-seller "
                         f"({sells_lean} of {len(trends.sector_flows)} sectors), "
                         f"heaviest in {heaviest.sector} ({_money(heaviest.net_usd)}).")
            elif net > 0:
                heaviest = max(trends.sector_flows, key=lambda s: s.net_usd)
                L.append(f"Across sectors, members leaned net-buyer "
                         f"({buys_lean} of {len(trends.sector_flows)} sectors), "
                         f"heaviest in {heaviest.sector} ({_money(heaviest.net_usd)}).")

    # --- 3. Leaderboard pointer --------------------------------------------
    if ranking:
        ms = ranking[0]
        L.append(f"Top trailing-12-month performer remains {ms.member} "
                 f"({ms.return_pct:+.1f}% on {ms.trade_count} trades).")

    return L
