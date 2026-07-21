"""Normalize Senate/House transactions into one schema for downstream code."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Literal

from sources.house import HouseTransaction
from sources.senate import SenateTransaction

Action = Literal["buy", "sell", "exchange", "other"]
Chamber = Literal["Senate", "House"]


@dataclass
class Trade:
    chamber: Chamber
    member: str            # "Last, First"
    state: str
    txn_date: date
    filing_date: date
    notification_date: date  # equal to filing_date for Senate
    owner: str             # Self / Spouse / Joint / Dependent
    ticker: str
    asset_name: str
    asset_type: str
    action: Action
    amount_low: int
    amount_high: int
    filing_id: str
    raw_action: str
    raw_amount: str
    source_url: str
    # Populated by the ranker during simulation from real market history.
    # The filing records a transaction DATE only (no time), so these are the
    # ACTUAL market prices for that calendar day. px_date is the matched
    # trading session (may be the nearest prior day if the txn date fell on a
    # weekend/holiday). Note: share COUNT is never disclosed and cannot be
    # recovered — only the dollar range is filed — so we do not report shares.
    px_date: "date | None" = None
    px_close: float | None = None
    px_low: float | None = None
    px_high: float | None = None

    @property
    def amount_mid(self) -> float:
        return (self.amount_low + self.amount_high) / 2

    @property
    def disclosure_lag_days(self) -> int:
        return (self.notification_date - self.txn_date).days


_AMOUNT_PAIR = re.compile(r"\$?([\d,]+)\s*-\s*\$?([\d,]+)")
_AMOUNT_OPEN = re.compile(r"\$?([\d,]+)\s*\+|Over\s+\$?([\d,]+)", re.I)


def _parse_amount(raw: str) -> tuple[int, int]:
    m = _AMOUNT_PAIR.search(raw)
    if m:
        return int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
    m = _AMOUNT_OPEN.search(raw)
    if m:
        v = int((m.group(1) or m.group(2)).replace(",", ""))
        # "$50,000,000+" type — treat as point estimate
        return v, v
    return 0, 0


def _classify(action: str) -> Action:
    a = action.lower()
    if "purchase" in a or a.strip() == "p":
        return "buy"
    if "sale" in a or a.strip() in ("s", "s (partial)"):
        return "sell"
    if "exchange" in a or a.strip() == "e":
        return "exchange"
    return "other"


_HOUSE_OWNER = {"JT": "Joint", "SP": "Spouse", "DC": "Dependent", "SELF": "Self"}


def from_senate(t: SenateTransaction) -> Trade:
    lo, hi = _parse_amount(t.amount_range)
    return Trade(
        chamber="Senate",
        member=f"{t.filing.last_name.strip().title()}, {t.filing.first_name.strip().title()}",
        state="",
        txn_date=t.txn_date,
        filing_date=t.filing.filing_date,
        notification_date=t.filing.filing_date,
        owner=t.owner,
        ticker=t.ticker,
        asset_name=t.asset_name,
        asset_type=t.asset_type,
        action=_classify(t.action),
        amount_low=lo,
        amount_high=hi,
        filing_id=t.filing.ptr_id,
        raw_action=t.action,
        raw_amount=t.amount_range,
        source_url=t.filing.ptr_url,
    )


def from_house(t: HouseTransaction) -> Trade:
    lo, hi = _parse_amount(t.amount_range)
    return Trade(
        chamber="House",
        member=f"{t.filing.last_name}, {t.filing.first_name}",
        state=t.filing.state_dst,
        txn_date=t.txn_date,
        filing_date=t.filing.filing_date,
        notification_date=t.notification_date,
        owner=_HOUSE_OWNER.get(t.owner.upper(), t.owner),
        ticker=t.ticker,
        asset_name=t.asset_name,
        asset_type=t.asset_type,
        action=_classify(t.action),
        amount_low=lo,
        amount_high=hi,
        filing_id=t.filing.doc_id,
        raw_action=t.action,
        raw_amount=t.amount_range,
        source_url=t.filing.pdf_url,
    )


def all_trades(
    senate_txns: Iterable[SenateTransaction],
    house_txns: Iterable[HouseTransaction],
) -> list[Trade]:
    out = [from_senate(t) for t in senate_txns]
    out += [from_house(t) for t in house_txns]
    # Drop non-equity rows we can't price (options, bonds, MFs lacking tickers).
    return [
        t for t in out
        if t.ticker
        and t.asset_type.upper() in {"ST", "STOCK", "EQ", ""}
        and t.action in ("buy", "sell")
    ]
