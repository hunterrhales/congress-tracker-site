"""Detect cross-Congress trends: stocks and sectors many members are moving on.

Unlike the net-$ aggregate (which one big trade can dominate), this measures
*breadth* — how many DISTINCT members bought or sold the same thing in a
recent window. Broad consensus is a stronger signal than one large bet.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from core import sectors
from core.normalize import Trade


@dataclass
class TickerTrend:
    ticker: str
    sector: str
    buyers: set[str] = field(default_factory=set)
    sellers: set[str] = field(default_factory=set)
    buy_usd: float = 0.0
    sell_usd: float = 0.0

    @property
    def net_members(self) -> int:
        return len(self.buyers) - len(self.sellers)

    @property
    def total_members(self) -> int:
        return len(self.buyers | self.sellers)


@dataclass
class SectorTrend:
    sector: str
    buyers: set[str] = field(default_factory=set)
    sellers: set[str] = field(default_factory=set)
    buy_usd: float = 0.0
    sell_usd: float = 0.0
    tickers: set[str] = field(default_factory=set)

    @property
    def net_usd(self) -> float:
        return self.buy_usd - self.sell_usd

    @property
    def trade_members(self) -> int:
        return len(self.buyers | self.sellers)


@dataclass
class Trends:
    window_days: int
    consensus_buys: list[TickerTrend]
    consensus_sells: list[TickerTrend]
    sector_flows: list[SectorTrend]
    min_members: int


def compute(trades: list[Trade], window_days: int = 90, top_n: int = 6) -> Trends:
    cutoff = date.today() - timedelta(days=window_days)
    window = [t for t in trades if t.txn_date >= cutoff and t.action in ("buy", "sell")]

    # Warm the sector cache for the tickers we'll roll up (bounded, cached).
    sectors.warm(sorted({t.ticker for t in window}))

    by_ticker: dict[str, TickerTrend] = {}
    by_sector: dict[str, SectorTrend] = {}
    for t in window:
        sec = sectors.sector_for(t.ticker)
        tt = by_ticker.get(t.ticker)
        if tt is None:
            tt = by_ticker[t.ticker] = TickerTrend(ticker=t.ticker, sector=sec)
        st = by_sector.get(sec)
        if st is None:
            st = by_sector[sec] = SectorTrend(sector=sec)
        st.tickers.add(t.ticker)
        if t.action == "buy":
            tt.buyers.add(t.member)
            tt.buy_usd += t.amount_mid
            st.buyers.add(t.member)
            st.buy_usd += t.amount_mid
        else:
            tt.sellers.add(t.member)
            tt.sell_usd += t.amount_mid
            st.sellers.add(t.member)
            st.sell_usd += t.amount_mid

    # Consensus threshold: prefer >=3 distinct members; relax to >=2 if sparse.
    def pick(direction: str) -> tuple[list[TickerTrend], int]:
        for threshold in (3, 2):
            if direction == "buy":
                hits = [tt for tt in by_ticker.values() if len(tt.buyers) >= threshold]
                hits.sort(key=lambda x: (len(x.buyers), x.buy_usd), reverse=True)
            else:
                hits = [tt for tt in by_ticker.values() if len(tt.sellers) >= threshold]
                hits.sort(key=lambda x: (len(x.sellers), x.sell_usd), reverse=True)
            if hits:
                return hits[:top_n], threshold
        return [], 2

    consensus_buys, thr_b = pick("buy")
    consensus_sells, thr_s = pick("sell")

    sector_flows = [s for s in by_sector.values() if s.sector != "Unknown"]
    sector_flows.sort(key=lambda s: s.trade_members, reverse=True)

    return Trends(
        window_days=window_days,
        consensus_buys=consensus_buys,
        consensus_sells=consensus_sells,
        sector_flows=sector_flows[:top_n],
        min_members=min(thr_b, thr_s),
    )
