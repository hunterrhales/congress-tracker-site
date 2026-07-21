"""Read-only Alpaca paper-account client.

We deliberately use plain HTTP and only GET endpoints — no POST/PATCH/DELETE
on order or position routes. This module is *incapable* of placing trades.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import requests

BASE = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")


@dataclass
class AlpacaAccount:
    cash: float
    portfolio_value: float
    buying_power: float
    equity: float


@dataclass
class AlpacaPosition:
    ticker: str
    qty: float
    avg_entry_price: float
    market_value: float
    unrealized_pl: float
    unrealized_plpc: float
    current_price: float


def _headers() -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY_ID"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_API_SECRET"],
    }


def get_account() -> AlpacaAccount:
    r = requests.get(f"{BASE}/v2/account", headers=_headers(), timeout=15)
    r.raise_for_status()
    d = r.json()
    return AlpacaAccount(
        cash=float(d["cash"]),
        portfolio_value=float(d["portfolio_value"]),
        buying_power=float(d["buying_power"]),
        equity=float(d["equity"]),
    )


def get_positions() -> list[AlpacaPosition]:
    r = requests.get(f"{BASE}/v2/positions", headers=_headers(), timeout=15)
    r.raise_for_status()
    return [
        AlpacaPosition(
            ticker=p["symbol"],
            qty=float(p["qty"]),
            avg_entry_price=float(p["avg_entry_price"]),
            market_value=float(p["market_value"]),
            unrealized_pl=float(p["unrealized_pl"]),
            unrealized_plpc=float(p["unrealized_plpc"]),
            current_price=float(p["current_price"]),
        )
        for p in r.json()
    ]


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    acct = get_account()
    print(f"Equity: ${acct.equity:,.2f}  Cash: ${acct.cash:,.2f}  "
          f"Portfolio: ${acct.portfolio_value:,.2f}")
    positions = get_positions()
    print(f"\n{len(positions)} open positions")
    for p in positions[:10]:
        print(f"  {p.ticker:6s} qty={p.qty:>8.2f} avg=${p.avg_entry_price:>8.2f} "
              f"mv=${p.market_value:>10,.2f} pl={p.unrealized_plpc*100:>+6.2f}%")
