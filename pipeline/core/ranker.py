"""Rank members by simulated trailing-12-month portfolio return.

Methodology (called out explicitly in the email):
  * Disclosed amounts are ranges. We take the midpoint as notional dollars.
  * For each ticker, sum signed notionals per member: buys add long exposure,
    sells subtract. The net long exposure (if > 0) is marked-to-market at the
    current price using the average buy price.
  * Closed positions (where sells offset buys) contribute realized P&L using
    the price on each transaction date.
  * This is a simulation, not an actual portfolio. It can't account for
    pre-existing positions disclosed in annual reports, options, or sizing
    inside the disclosed ranges. Treat the rank as directional.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from core import committees, prices
from core.normalize import Trade


@dataclass
class MemberStats:
    member: str
    chamber: str
    trade_count: int = 0
    total_notional: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    open_positions: dict[str, float] = field(default_factory=dict)  # ticker -> shares
    avg_cost: dict[str, float] = field(default_factory=dict)        # ticker -> avg cost/share
    recent_trades: list[Trade] = field(default_factory=list)
    committees: list[str] = field(default_factory=list)

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def return_pct(self) -> float:
        if self.total_notional <= 0:
            return 0.0
        return 100.0 * self.total_pnl / self.total_notional


def simulate(trades: list[Trade], window_days: int = 365) -> dict[str, MemberStats]:
    cutoff = date.today() - timedelta(days=window_days)
    relevant = [t for t in trades if t.txn_date >= cutoff]
    # Process per member in chronological order.
    by_member: dict[str, list[Trade]] = defaultdict(list)
    for t in relevant:
        by_member[t.member].append(t)

    committee_lookup = committees.load_assignments()
    stats: dict[str, MemberStats] = {}
    for member, txns in by_member.items():
        txns.sort(key=lambda x: x.txn_date)
        ms = MemberStats(member=member, chamber=txns[0].chamber)
        ms.recent_trades = txns
        ms.trade_count = len(txns)
        ms.committees = committees.committees_for(member, committee_lookup)

        # Per-ticker running position with avg-cost.
        for t in txns:
            notional = t.amount_mid
            ms.total_notional += notional
            bar_res = prices.bar_on(t.ticker, t.txn_date)
            if bar_res is None:
                continue
            px_date, bar = bar_res
            px = bar.close
            if px is None or px <= 0:
                continue
            # Record the ACTUAL market prices for the transaction date so the
            # email can show the real close and the day's high–low range.
            t.px_date = px_date
            t.px_close = bar.close
            t.px_low = bar.low
            t.px_high = bar.high
            shares = notional / px
            cur = ms.open_positions.get(t.ticker, 0.0)
            avg = ms.avg_cost.get(t.ticker, 0.0)
            if t.action == "buy":
                # Weighted average cost.
                new_total = cur + shares
                if new_total > 0:
                    ms.avg_cost[t.ticker] = (cur * avg + shares * px) / new_total
                ms.open_positions[t.ticker] = new_total
            elif t.action == "sell":
                sell_shares = min(shares, cur) if cur > 0 else shares
                if cur > 0:
                    ms.realized_pnl += sell_shares * (px - avg)
                    ms.open_positions[t.ticker] = cur - sell_shares
                # Any excess sold beyond what was tracked here is unobserved
                # short or sale of a pre-existing position; we ignore.

        # Mark open positions to current price.
        for ticker, shares in ms.open_positions.items():
            if shares <= 0:
                continue
            cp = prices.current_price(ticker)
            if cp is None:
                continue
            ms.unrealized_pnl += shares * (cp - ms.avg_cost.get(ticker, 0.0))

        stats[member] = ms
    return stats


def rank(stats: dict[str, MemberStats], min_trades: int = 3) -> list[MemberStats]:
    eligible = [s for s in stats.values() if s.trade_count >= min_trades and s.total_notional > 0]
    eligible.sort(key=lambda s: s.return_pct, reverse=True)
    return eligible
