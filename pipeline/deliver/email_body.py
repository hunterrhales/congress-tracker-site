"""Compose the daily email summary as HTML + plaintext.

Design goals for the HTML:
  * Scannable on phone and desktop — single column, generous spacing.
  * Color-coded BUY/SELL badges so direction reads at a glance.
  * The most decision-relevant blocks (actionable trades + the mirror
    candidate) sit at the top; reference tables sink to the bottom.
  * Inline styles only — Gmail/Outlook strip <style> blocks and don't
    support flexbox, so layout uses tables and inline CSS.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from html import escape
from typing import Iterable

from core import committees, review as review_mod
from core.normalize import Trade
from core.ranker import MemberStats
from core.alpaca import AlpacaAccount, AlpacaPosition


ACTIONABLE_LAG_DAYS = 14

# --- palette ---
INK = "#1a1a2e"
MUTED = "#6b7280"
LINE = "#e5e7eb"
BG = "#f4f5f7"
CARD = "#ffffff"
GREEN = "#0f9d58"
GREEN_BG = "#e6f4ea"
RED = "#d93025"
RED_BG = "#fce8e6"
ACCENT = "#3b5bdb"
ACCENT_BG = "#edf0fd"
AMBER_BG = "#fef7e0"
AMBER_LINE = "#f0c33c"


def _action_label(t: Trade) -> str:
    if t.action == "buy":
        return "BUY"
    if t.action == "sell":
        return "SELL"
    return t.action.upper()


def _fmt_money(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.0f}"


def _amount_short(t: Trade) -> str:
    """'$1K–$15K' style compaction of the disclosed range."""
    def k(v: int) -> str:
        if v >= 1_000_000:
            return f"${v/1_000_000:.0f}M"
        if v >= 1_000:
            return f"${v//1000}K"
        return f"${v}"
    return f"{k(t.amount_low)}–{k(t.amount_high)}"


def _px_close(t: Trade) -> str:
    """Actual closing price on the transaction date, or em-dash if unpriced."""
    if t.px_close is None:
        return "—"
    return f"${t.px_close:,.2f}"


def _px_range(t: Trade) -> str:
    """Actual low–high the stock traded at on the transaction date."""
    if t.px_low is None or t.px_high is None:
        return "—"
    return f"${t.px_low:,.2f}–${t.px_high:,.2f}"


def _badge(label: str, *, fg: str, bg: str) -> str:
    return (
        f"<span style='display:inline-block;padding:2px 8px;border-radius:10px;"
        f"font-size:11px;font-weight:700;color:{fg};background:{bg};"
        f"letter-spacing:.3px'>{escape(label)}</span>"
    )


def _action_badge(t: Trade) -> str:
    if t.action == "buy":
        return _badge("BUY", fg=GREEN, bg=GREEN_BG)
    if t.action == "sell":
        return _badge("SELL", fg=RED, bg=RED_BG)
    return _badge(t.action.upper(), fg=MUTED, bg=BG)


def _chip(text: str) -> str:
    return (
        f"<span style='display:inline-block;padding:1px 7px;margin:1px 2px;"
        f"border-radius:8px;font-size:11px;color:{ACCENT};background:{ACCENT_BG}'>"
        f"{escape(text)}</span>"
    )


def compose(
    new_trades: list[Trade],
    all_trades_ltm: list[Trade],
    ranking: list[MemberStats],
    alpaca_account: AlpacaAccount,
    alpaca_positions: list[AlpacaPosition],
    paper_filings_skipped: int,
    trends=None,
    data_warnings=None,
) -> tuple[str, str]:
    today = date.today()
    subject = f"Congress trade tracker — {today.isoformat()} ({len(new_trades)} new)"

    cmte_lookup = committees.load_assignments()

    def cmte_list(name: str, max_shown: int = 4) -> list[str]:
        cs = committees.committees_for(name, cmte_lookup)
        cs = [
            c for c in cs
            if "Commission" not in c
            and "Caucus" not in c
            and "Subcommittee" not in c
            and len(c) <= 35
        ]
        return cs[:max_shown]

    def cmtes(name: str, max_shown: int = 3) -> str:
        cs = committees.committees_for(name, cmte_lookup)
        cs = [
            c for c in cs
            if "Commission" not in c
            and "Caucus" not in c
            and "Subcommittee" not in c
            and len(c) <= 35
        ]
        if not cs:
            return ""
        if len(cs) > max_shown:
            return ", ".join(cs[:max_shown]) + f" +{len(cs) - max_shown}"
        return ", ".join(cs)

    actionable = [t for t in new_trades if t.disclosure_lag_days <= ACTIONABLE_LAG_DAYS]

    by_ticker = Counter()
    for t in all_trades_ltm:
        sign = 1 if t.action == "buy" else -1 if t.action == "sell" else 0
        by_ticker[t.ticker] += sign * t.amount_mid
    top_buys = sorted(by_ticker.items(), key=lambda x: -x[1])[:6]
    top_sells = sorted(by_ticker.items(), key=lambda x: x[1])[:6]

    top_n = ranking[:5]
    top_member = top_n[0] if top_n else None
    top_positions: list[tuple[str, float]] = []
    if top_member:
        top_positions = sorted(
            ((tk, sh) for tk, sh in top_member.open_positions.items() if sh > 0),
            key=lambda x: -x[1],
        )[:10]

    # Plain-English synthesis for the top of the email.
    try:
        review = review_mod.build(
            new_trades=new_trades, trends=trends, ranking=ranking, today=today)
    except Exception:
        review = []

    text = _plaintext(
        today, new_trades, actionable, top_buys, top_sells, top_n,
        top_member, top_positions, alpaca_account, alpaca_positions,
        paper_filings_skipped, cmtes, trends, review,
    )
    html = _html(
        today, new_trades, actionable, top_buys, top_sells, top_n,
        top_member, top_positions, alpaca_account, alpaca_positions,
        paper_filings_skipped, cmtes, cmte_list, trends, review, data_warnings,
    )
    return subject, html, text


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
def _section(title: str, subtitle: str = "") -> str:
    sub = (f"<div style='font-size:12px;color:{MUTED};margin-top:2px'>{escape(subtitle)}</div>"
           if subtitle else "")
    return (
        f"<tr><td style='padding:26px 24px 8px'>"
        f"<div style='font-size:13px;font-weight:700;letter-spacing:.6px;"
        f"text-transform:uppercase;color:{INK}'>{escape(title)}</div>{sub}</td></tr>"
    )


def _card_open() -> str:
    return (
        f"<tr><td style='padding:0 24px'>"
        f"<table width='100%' cellpadding='0' cellspacing='0' style='border-collapse:separate'>"
    )


def _card_close() -> str:
    return "</table></td></tr>"


def _stat_chip(value: str, label: str, color: str) -> str:
    return (
        f"<td style='padding:10px 14px;background:{CARD};border:1px solid {LINE};"
        f"border-radius:10px;text-align:center'>"
        f"<div style='font-size:22px;font-weight:800;color:{color};line-height:1'>{escape(value)}</div>"
        f"<div style='font-size:11px;color:{MUTED};margin-top:4px;text-transform:uppercase;"
        f"letter-spacing:.4px'>{escape(label)}</div></td>"
    )


def _table(headers: Iterable[str], rows: Iterable[Iterable[str]],
           aligns: list[str] | None = None) -> str:
    headers = list(headers)
    aligns = aligns or ["left"] * len(headers)
    head = "".join(
        f"<th style='text-align:{aligns[i]};padding:8px 12px;font-size:11px;"
        f"font-weight:700;color:{MUTED};text-transform:uppercase;letter-spacing:.4px;"
        f"border-bottom:2px solid {LINE}'>{h}</th>"
        for i, h in enumerate(headers)
    )
    body_rows = []
    for ri, r in enumerate(rows):
        bg = CARD if ri % 2 == 0 else BG
        cells = "".join(
            f"<td style='padding:9px 12px;font-size:13px;color:{INK};"
            f"text-align:{aligns[i]};border-bottom:1px solid {LINE}'>{c}</td>"
            for i, c in enumerate(r)
        )
        body_rows.append(f"<tr style='background:{bg}'>{cells}</tr>")
    return (
        f"<table width='100%' cellpadding='0' cellspacing='0' "
        f"style='border-collapse:collapse;border:1px solid {LINE};border-radius:10px;"
        f"overflow:hidden'>"
        f"<thead><tr style='background:{CARD}'>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table>"
    )


def _member_cell(name: str, chamber: str) -> str:
    return (
        f"<div style='font-weight:600'>{escape(name)}</div>"
        f"<div style='font-size:11px;color:{MUTED}'>{escape(chamber)}</div>"
    )


def _html(today, new_trades, actionable, top_buys, top_sells, top_n,
          top_member, top_positions, acct, positions, paper_skipped,
          cmtes, cmte_list, trends=None, review=None, data_warnings=None) -> str:
    P: list[str] = []

    # ---- header band ----
    P.append(
        f"<tr><td style='padding:28px 24px 8px;background:{INK}'>"
        f"<div style='font-size:20px;font-weight:800;color:#fff'>Congress Trade Tracker</div>"
        f"<div style='font-size:13px;color:#aeb4c2;margin-top:2px'>{today.strftime('%A, %B %-d, %Y')}</div>"
        f"</td></tr>"
    )
    # ---- stat chips ----
    P.append(
        f"<tr><td style='padding:16px 24px 4px;background:{INK}'>"
        f"<table width='100%' cellpadding='0' cellspacing='6'><tr>"
        + _stat_chip(str(len(new_trades)), "new disclosures", INK)
        + _stat_chip(str(len(actionable)), f"actionable ≤{ACTIONABLE_LAG_DAYS}d", GREEN if actionable else MUTED)
        + _stat_chip(
            f"{top_n[0].return_pct:+.0f}%" if top_n else "—",
            "top LTM return", ACCENT)
        + (_stat_chip(str(paper_skipped), "paper PTRs", MUTED) if paper_skipped else "")
        + "</tr></table></td></tr>"
    )
    P.append(f"<tr><td style='height:8px;background:{INK}'></td></tr>")

    # ---- DATA WARNING BANNER ----
    # A source that failed to load is surfaced loudly so a silent data gap
    # (e.g. House feed down) is never mistaken for "Congress didn't trade."
    if data_warnings:
        items = "".join(f"<li>{escape(w)}</li>" for w in data_warnings)
        P.append(
            f"<tr><td style='padding:12px 24px 0'>"
            f"<div style='padding:12px 16px;background:{RED_BG};border:1px solid {RED};"
            f"border-radius:10px;font-size:13px;color:{RED}'>"
            f"<b>⚠ Data warning — this report is incomplete.</b> One or more sources "
            f"failed to load, so trades from them are missing below:"
            f"<ul style='margin:6px 0 0;padding-left:18px'>{items}</ul></div></td></tr>"
        )

    # ---- TODAY'S READ (synthesis) ----
    if review:
        bullets = "".join(
            f"<li style='margin:5px 0;line-height:1.45'>{escape(s)}</li>" for s in review
        )
        P.append(_section("📝 Today's read", "Auto-generated summary of the updates + trends"))
        P.append(_card_open())
        P.append(
            f"<tr><td style='padding:14px 18px;background:{ACCENT_BG};"
            f"border:1px solid {LINE};border-radius:12px'>"
            f"<ul style='margin:0;padding-left:18px;font-size:14px;color:{INK}'>{bullets}</ul>"
            f"</td></tr>"
        )
        P.append(_card_close())

    # ---- DAILY UPDATE (top) ----
    # Trades newly disclosed since the last report. Because the STOCK Act lags
    # 30–45 days, "new today" means newly FILED, not newly executed — the trade
    # date and lag are shown per row so that's transparent.
    daily = sorted(new_trades, key=lambda x: (x.notification_date, x.txn_date), reverse=True)
    filed_today = [t for t in daily if t.notification_date == today]
    if daily:
        dates = [t.notification_date for t in daily]
        span = (f"filed {min(dates).strftime('%b %-d')}–{max(dates).strftime('%b %-d')}"
                if min(dates) != max(dates) else f"filed {max(dates).strftime('%b %-d')}")
        sub = (f"{len(filed_today)} filed today · {len(daily)} new since your last report ({span})"
               if filed_today else f"{len(daily)} new since your last report ({span})")
        P.append(_section("📅 Daily update", sub))
        P.append(_card_open())
        rows = []
        for t in daily[:40]:
            cm = cmtes(t.member, max_shown=2)
            today_flag = (" <span style='color:" + GREEN + ";font-weight:700'>•today</span>"
                          if t.notification_date == today else "")
            rows.append([
                t.notification_date.strftime("%b %-d") + today_flag,
                t.txn_date.strftime("%b %-d"),
                _action_badge(t),
                f"<b>{escape(t.ticker)}</b>",
                f"<b>{_px_close(t)}</b>",
                _member_cell(t.member, t.chamber),
                (f"<span style='font-size:11px;color:{ACCENT}'>{escape(cm)}</span>" if cm else
                 f"<span style='font-size:11px;color:{MUTED}'>—</span>"),
            ])
        P.append("<tr><td style='padding:6px 0'>")
        P.append(_table(
            ["Filed", "Traded", "Action", "Ticker", "Price", "Member", "Committees"],
            rows,
            aligns=["left", "left", "left", "left", "right", "left", "left"]))
        if len(daily) > 40:
            P.append(f"<div style='font-size:11px;color:{MUTED};margin-top:6px'>"
                     f"+ {len(daily) - 40} more new filings not shown</div>")
        P.append("</td></tr>")
        P.append(_card_close())
    else:
        P.append(_section("📅 Daily update", "No new disclosures filed since your last report"))
        P.append(_card_open())
        P.append(
            f"<tr><td style='padding:14px 16px;background:{CARD};border:1px solid {LINE};"
            f"border-radius:10px;font-size:13px;color:{MUTED}'>"
            f"Nothing new in the official feeds since the last run. The leaderboard and "
            f"signals below still reflect the trailing 12 months.</td></tr>"
        )
        P.append(_card_close())

    # ---- TRENDS ACROSS CONGRESS ----
    if trends is not None and (trends.consensus_buys or trends.consensus_sells
                               or trends.sector_flows):
        P.append(_section(
            "📈 Trends across Congress",
            f"Where multiple members are moving the same way · last {trends.window_days} days "
            f"of disclosed trades. Counts are DISTINCT members."))
        P.append(_card_open())
        P.append("<tr><td style='padding:6px 0'>")

        def _consensus_rows(items, direction):
            rows = []
            for tt in items:
                pro = len(tt.buyers) if direction == "buy" else len(tt.sellers)
                con = len(tt.sellers) if direction == "buy" else len(tt.buyers)
                col = GREEN if direction == "buy" else RED
                rows.append([
                    f"<b>{escape(tt.ticker)}</b>",
                    f"<span style='color:{col};font-weight:700'>{pro}</span>",
                    str(con) if con else "—",
                    f"<span style='font-size:11px;color:{MUTED}'>{escape(tt.sector)}</span>",
                ])
            return rows

        # two side-by-side consensus tables
        buys_tbl = (
            f"<div style='font-size:12px;font-weight:700;color:{GREEN};margin-bottom:6px'>"
            f"▲ Most members BUYING</div>"
            + (_table(["Ticker", "Buyers", "Sellers", "Sector"],
                      _consensus_rows(trends.consensus_buys, "buy"),
                      aligns=["left", "right", "right", "left"])
               if trends.consensus_buys
               else f"<div style='font-size:12px;color:{MUTED}'>No multi-member buying cluster.</div>")
        )
        sells_tbl = (
            f"<div style='font-size:12px;font-weight:700;color:{RED};margin-bottom:6px'>"
            f"▼ Most members SELLING</div>"
            + (_table(["Ticker", "Sellers", "Buyers", "Sector"],
                      _consensus_rows(trends.consensus_sells, "sell"),
                      aligns=["left", "right", "right", "left"])
               if trends.consensus_sells
               else f"<div style='font-size:12px;color:{MUTED}'>No multi-member selling cluster.</div>")
        )
        P.append(
            f"<table width='100%' cellpadding='0' cellspacing='0'><tr>"
            f"<td width='50%' style='vertical-align:top;padding-right:6px'>{buys_tbl}</td>"
            f"<td width='50%' style='vertical-align:top;padding-left:6px'>{sells_tbl}</td>"
            f"</tr></table>"
        )

        # sector flows
        if trends.sector_flows:
            sec_rows = []
            for s in trends.sector_flows:
                lean = len(s.buyers) - len(s.sellers)
                lean_str = (f"<span style='color:{GREEN};font-weight:700'>net buy</span>"
                            if lean > 0 else
                            f"<span style='color:{RED};font-weight:700'>net sell</span>"
                            if lean < 0 else
                            f"<span style='color:{MUTED}'>mixed</span>")
                sec_rows.append([
                    f"<b>{escape(s.sector)}</b>",
                    str(len(s.buyers)),
                    str(len(s.sellers)),
                    lean_str,
                    f"<span style='font-size:11px;color:{MUTED}'>{_fmt_money(s.net_usd)}</span>",
                ])
            P.append("<div style='height:14px'></div>")
            P.append(f"<div style='font-size:12px;font-weight:700;color:{INK};margin-bottom:6px'>"
                     f"Sector flows (by distinct members)</div>")
            P.append(_table(["Sector", "Buying", "Selling", "Lean", "Net $"], sec_rows,
                            aligns=["left", "right", "right", "left", "right"]))
        P.append("</td></tr>")
        P.append(_card_close())

    # ---- MIRROR CANDIDATE hero ----
    if top_member:
        chips = "".join(_chip(c) for c in cmte_list(top_member.member, 6))
        pos_rows = [
            [f"<b>{escape(tk)}</b>", f"~{sh:.0f} sh", f"${top_member.avg_cost.get(tk, 0):.2f}"]
            for tk, sh in top_positions
        ]
        P.append(_section("Mirror candidate", "Highest trailing-12-month simulated return"))
        P.append(_card_open())
        P.append(
            f"<tr><td style='padding:16px 18px;background:{ACCENT_BG};"
            f"border:1px solid {LINE};border-radius:12px'>"
            f"<table width='100%' cellpadding='0' cellspacing='0'><tr>"
            f"<td style='vertical-align:top'>"
            f"<div style='font-size:18px;font-weight:800;color:{INK}'>{escape(top_member.member)}</div>"
            f"<div style='font-size:12px;color:{MUTED};margin:2px 0 8px'>{escape(top_member.chamber)}"
            f" · {top_member.trade_count} trades · {_fmt_money(top_member.total_notional)} notional</div>"
            f"<div>{chips}</div>"
            f"</td>"
            f"<td style='vertical-align:top;text-align:right;white-space:nowrap'>"
            f"<div style='font-size:30px;font-weight:800;color:{GREEN if top_member.return_pct>=0 else RED}'>"
            f"{top_member.return_pct:+.1f}%</div>"
            f"<div style='font-size:11px;color:{MUTED};text-transform:uppercase;letter-spacing:.4px'>LTM return</div>"
            f"</td></tr></table>"
        )
        if pos_rows:
            P.append(
                f"<div style='font-size:12px;color:{MUTED};margin:14px 0 6px'>"
                f"Current net-long positions — <i>recommendations only, no orders placed</i></div>"
            )
            P.append(_table(["Ticker", "Approx. size", "Avg cost"], pos_rows,
                            aligns=["left", "right", "right"]))
        P.append("</td></tr>")
        P.append(_card_close())

    # ---- ACTIONABLE ----
    if actionable:
        P.append(_section(f"Actionable now · {len(actionable)}",
                          f"Trade happened within {ACTIONABLE_LAG_DAYS} days of disclosure — freshest signal"))
        P.append(_card_open())
        rows = []
        for t in sorted(actionable, key=lambda x: x.txn_date, reverse=True):
            lag_color = GREEN if t.disclosure_lag_days <= 7 else INK
            rows.append([
                _action_badge(t),
                f"<b>{escape(t.ticker)}</b>",
                _amount_short(t),
                f"<span style='color:{lag_color};font-weight:600'>{t.disclosure_lag_days}d</span>",
                _member_cell(t.member, t.chamber)
                + (f"<div style='font-size:11px;color:{MUTED};margin-top:2px'>{escape(cmtes(t.member))}</div>"
                   if cmtes(t.member) else ""),
            ])
        P.append(f"<tr><td style='padding:6px 0'>")
        P.append(_table(["Action", "Ticker", "Amount", "Lag", "Member"], rows,
                        aligns=["left", "left", "right", "right", "left"]))
        P.append("</td></tr>")
        P.append(_card_close())

    # ---- LEADERBOARD (quick-scan summary) ----
    if top_n:
        P.append(_section("Top 5 performers · trailing 12 months",
                          "Midpoint of disclosed range, marked-to-market. Excludes pre-existing positions."))
        P.append(_card_open())
        rows = []
        for i, ms in enumerate(top_n, 1):
            ret_color = GREEN if ms.return_pct >= 0 else RED
            rows.append([
                f"<b>{i}</b>",
                _member_cell(ms.member, ms.chamber),
                f"<span style='color:{ret_color};font-weight:700'>{ms.return_pct:+.1f}%</span>",
                str(ms.trade_count),
                _fmt_money(ms.total_notional),
                "".join(_chip(c) for c in cmte_list(ms.member, 3)) or "—",
            ])
        P.append(f"<tr><td style='padding:6px 0'>")
        P.append(_table(["#", "Member", "Return", "Trades", "Notional", "Committees"], rows,
                        aligns=["center", "left", "right", "right", "right", "left"]))
        P.append("</td></tr>")
        P.append(_card_close())

    # ---- PER-PERFORMER TRADE DETAIL ----
    if top_n:
        P.append(_section("What the top performers traded",
                          "Every disclosed trade in the trailing 12 months. Price = ACTUAL market "
                          "close on the transaction date; Day range = that day's real low–high. "
                          "(Filings give a date only — no time — and never a share count.)"))
        for i, ms in enumerate(top_n, 1):
            ret_color = GREEN if ms.return_pct >= 0 else RED
            chips = "".join(_chip(c) for c in cmte_list(ms.member, 8)) or \
                f"<span style='font-size:11px;color:{MUTED}'>no standing-committee seats found</span>"
            ms_trades = sorted(ms.recent_trades, key=lambda x: x.txn_date, reverse=True)
            shown = ms_trades[:9]
            trade_rows = []
            for t in shown:
                ticker_cell = (
                    f"<b>{escape(t.ticker)}</b>"
                    + (f"<div style='font-size:10px;color:{MUTED}'>{escape(t.asset_name[:24])}</div>"
                       if t.asset_name else "")
                )
                trade_rows.append([
                    t.txn_date.strftime("%b %-d, %y"),
                    _action_badge(t),
                    ticker_cell,
                    _amount_short(t),
                    f"<b>{_px_close(t)}</b>",
                    f"<span style='font-size:11px;color:{MUTED}'>{_px_range(t)}</span>",
                    f"<span style='font-size:11px;color:{MUTED}'>{t.disclosure_lag_days}d</span>",
                ])
            P.append(_card_open())
            P.append(
                f"<tr><td style='padding:14px 16px;background:{CARD};border:1px solid {LINE};"
                f"border-radius:12px'>"
                # card header
                f"<table width='100%' cellpadding='0' cellspacing='0'><tr>"
                f"<td style='vertical-align:top'>"
                f"<span style='display:inline-block;width:22px;height:22px;border-radius:11px;"
                f"background:{ACCENT};color:#fff;font-size:12px;font-weight:700;text-align:center;"
                f"line-height:22px;margin-right:8px'>{i}</span>"
                f"<span style='font-size:16px;font-weight:800;color:{INK}'>{escape(ms.member)}</span>"
                f"<div style='font-size:12px;color:{MUTED};margin:3px 0 8px'>{escape(ms.chamber)}"
                f" · {ms.trade_count} trades · {_fmt_money(ms.total_notional)} notional</div>"
                f"<div>{chips}</div>"
                f"</td>"
                f"<td style='vertical-align:top;text-align:right;white-space:nowrap'>"
                f"<div style='font-size:24px;font-weight:800;color:{ret_color}'>{ms.return_pct:+.1f}%</div>"
                f"<div style='font-size:10px;color:{MUTED};text-transform:uppercase;"
                f"letter-spacing:.4px'>LTM return</div>"
                f"</td></tr></table>"
                f"<div style='height:10px'></div>"
                + _table(["Date", "Action", "Ticker", "Amount", "Price", "Day range", "Lag"],
                         trade_rows,
                         aligns=["left", "left", "left", "right", "right", "right", "right"])
                + (f"<div style='font-size:11px;color:{MUTED};margin-top:6px'>"
                   f"+ {len(ms_trades) - len(shown)} more trades not shown</div>"
                   if len(ms_trades) > len(shown) else "")
                + "</td></tr>"
            )
            P.append(_card_close())

    # ---- AGGREGATE SIGNAL ----
    P.append(_section("Aggregate signal · last 12 months",
                      "Net dollar flow across all members (disclosed-range midpoints)"))
    P.append(_card_open())
    P.append("<tr><td style='padding:6px 0'>")
    P.append(
        f"<table width='100%' cellpadding='0' cellspacing='0'><tr>"
        f"<td width='50%' style='vertical-align:top;padding-right:6px'>"
        f"<div style='font-size:12px;font-weight:700;color:{GREEN};margin-bottom:6px'>▲ Most bought</div>"
        + _table(["Ticker", "Net"],
                 [[f"<b>{escape(tk)}</b>", f"<span style='color:{GREEN}'>{_fmt_money(v)}</span>"]
                  for tk, v in top_buys],
                 aligns=["left", "right"])
        + "</td>"
        f"<td width='50%' style='vertical-align:top;padding-left:6px'>"
        f"<div style='font-size:12px;font-weight:700;color:{RED};margin-bottom:6px'>▼ Most sold</div>"
        + _table(["Ticker", "Net"],
                 [[f"<b>{escape(tk)}</b>", f"<span style='color:{RED}'>{_fmt_money(v)}</span>"]
                  for tk, v in top_sells],
                 aligns=["left", "right"])
        + "</td></tr></table>"
    )
    P.append("</td></tr>")
    P.append(_card_close())

    # (The day's new disclosures now lead the email as "Daily update" — see top.)

    # ---- ALPACA ----
    P.append(_section("Your Alpaca paper account"))
    P.append(_card_open())
    P.append(
        f"<tr><td style='padding:14px 16px;background:{CARD};border:1px solid {LINE};border-radius:10px'>"
        f"<table width='100%' cellpadding='0' cellspacing='0'><tr>"
        + _stat_chip(f"${acct.equity:,.0f}", "equity", INK)
        + f"<td style='width:8px'></td>"
        + _stat_chip(f"${acct.cash:,.0f}", "cash", INK)
        + f"<td style='width:8px'></td>"
        + _stat_chip(str(len(positions)), "positions", INK)
        + "</tr></table>"
    )
    if positions:
        rows = [
            [f"<b>{escape(p.ticker)}</b>", f"{p.qty:.0f}", f"${p.avg_entry_price:.2f}",
             f"${p.market_value:,.0f}",
             f"<span style='color:{GREEN if p.unrealized_plpc>=0 else RED};font-weight:600'>"
             f"{p.unrealized_plpc*100:+.1f}%</span>"]
            for p in positions
        ]
        P.append("<div style='height:12px'></div>")
        P.append(_table(["Ticker", "Qty", "Avg", "Value", "P&L"], rows,
                        aligns=["left", "right", "right", "right", "right"]))
    else:
        P.append(
            f"<div style='font-size:13px;color:{MUTED};margin-top:12px'>"
            f"No open positions. Recommendations above are not auto-executed.</div>"
        )
    P.append("</td></tr>")
    P.append(_card_close())

    # ---- footer ----
    P.append(
        f"<tr><td style='padding:24px'>"
        f"<div style='padding:14px 16px;background:{AMBER_BG};border:1px solid {AMBER_LINE};"
        f"border-radius:10px;font-size:12px;color:#7a5c00;line-height:1.6'>"
        f"<b>How to read this.</b> Disclosures lag trades 30–45 days under the STOCK Act — "
        f"this is the freshest public data. The <b>Lag</b> column shows trade-to-disclosure days; "
        f"smaller is fresher. <b>Price</b> and <b>Day range</b> are ACTUAL market data — the real "
        f"close and low–high for the stock on the transaction date (pulled from price history). "
        f"Filings record a date only, with no intraday time, so the day's range shows the full span "
        f"the member could have transacted at. <b>Dollar amounts are disclosed as ranges</b>, and the "
        f"filing never includes a share count, so we don't report shares. The leaderboard covers a "
        f"single year and a few outliers can skew it — weight by trade count. "
        f"<b>No trades have been placed; this is a recommendation only.</b>"
        f"</div></td></tr>"
    )

    inner = "".join(P)
    return (
        f"<!doctype html><html><body style='margin:0;padding:0;background:{BG}'>"
        f"<table width='100%' cellpadding='0' cellspacing='0' style='background:{BG}'>"
        f"<tr><td align='center' style='padding:16px'>"
        f"<table width='620' cellpadding='0' cellspacing='0' "
        f"style='max-width:620px;width:100%;background:{BG};border-radius:14px;overflow:hidden;"
        f"font-family:-apple-system,BlinkMacSystemFont,\"Segoe UI\",Roboto,Helvetica,Arial,sans-serif'>"
        f"{inner}"
        f"</table></td></tr></table></body></html>"
    )


# --------------------------------------------------------------------------- #
# Plaintext fallback
# --------------------------------------------------------------------------- #
def _plaintext(today, new_trades, actionable, top_buys, top_sells, top_n,
               top_member, top_positions, acct, positions, paper_skipped,
               cmtes, trends=None, review=None) -> str:
    L: list[str] = []
    L.append(f"CONGRESS TRADE TRACKER — {today.isoformat()}")
    L.append("=" * 56)
    L.append(f"{len(new_trades)} new disclosures · {len(actionable)} actionable "
             f"(<={ACTIONABLE_LAG_DAYS}d lag)"
             + (f" · {paper_skipped} paper PTRs skipped" if paper_skipped else ""))
    L.append("")
    # ---- TODAY'S READ ----
    if review:
        L.append("TODAY'S READ")
        for s in review:
            L.append(f"  • {s}")
        L.append("")
    # ---- DAILY UPDATE (top) ----
    daily = sorted(new_trades, key=lambda x: (x.notification_date, x.txn_date), reverse=True)
    filed_today = [t for t in daily if t.notification_date == today]
    L.append("DAILY UPDATE — new since your last report"
             + (f"  ({len(filed_today)} filed today, {len(daily)} total)" if daily else ""))
    if daily:
        for t in daily[:60]:
            cm = cmtes(t.member, 2)
            px = f" @ {_px_close(t)}" if t.px_close is not None else ""
            flag = " *TODAY*" if t.notification_date == today else ""
            L.append(f"  filed {t.notification_date}{flag}  txn {t.txn_date}  "
                     f"{_action_label(t):4s} {t.ticker:6s}{px}  {t.member} ({t.chamber})"
                     + (f"  [{cm}]" if cm else ""))
        if len(daily) > 60:
            L.append(f"  ... and {len(daily)-60} more")
    else:
        L.append("  No new disclosures filed since your last report.")
    L.append("")
    # ---- TRENDS ----
    if trends is not None and (trends.consensus_buys or trends.consensus_sells
                               or trends.sector_flows):
        L.append(f"TRENDS ACROSS CONGRESS (last {trends.window_days}d, distinct members)")
        if trends.consensus_buys:
            L.append("  Most members BUYING:")
            for tt in trends.consensus_buys:
                L.append(f"    {tt.ticker:6s} {len(tt.buyers)} buyers / {len(tt.sellers)} sellers"
                         f"  [{tt.sector}]")
        if trends.consensus_sells:
            L.append("  Most members SELLING:")
            for tt in trends.consensus_sells:
                L.append(f"    {tt.ticker:6s} {len(tt.sellers)} sellers / {len(tt.buyers)} buyers"
                         f"  [{tt.sector}]")
        if trends.sector_flows:
            L.append("  Sector flows:")
            for s in trends.sector_flows:
                lean = ("net buy" if len(s.buyers) > len(s.sellers)
                        else "net sell" if len(s.sellers) > len(s.buyers) else "mixed")
                L.append(f"    {s.sector:22s} {len(s.buyers)}B/{len(s.sellers)}S "
                         f"{lean} ({_fmt_money(s.net_usd)})")
        L.append("")
    if top_member:
        L.append(f"MIRROR CANDIDATE: {top_member.member} ({top_member.chamber})")
        L.append(f"  LTM return {top_member.return_pct:+.1f}% · {top_member.trade_count} trades")
        cm = cmtes(top_member.member, 6)
        if cm:
            L.append(f"  Committees: {cm}")
        for tk, sh in top_positions:
            L.append(f"    {tk:6s} ~{sh:.0f} sh @ ${top_member.avg_cost.get(tk,0):.2f}")
        L.append("  (Recommendations only — no orders placed.)")
        L.append("")
    if actionable:
        L.append(f"ACTIONABLE ({len(actionable)})")
        for t in sorted(actionable, key=lambda x: x.txn_date, reverse=True):
            cm = cmtes(t.member)
            L.append(f"  {_action_label(t):4s} {t.ticker:6s} {t.raw_amount}  "
                     f"lag {t.disclosure_lag_days}d  {t.member} ({t.chamber})"
                     + (f"  [{cm}]" if cm else ""))
        L.append("")
    L.append("TOP 5 PERFORMERS, LTM — WHAT THEY TRADED")
    for i, ms in enumerate(top_n, 1):
        cm = cmtes(ms.member, 6)
        L.append(f"  {i}. {ms.member} ({ms.chamber})  {ms.return_pct:+.1f}%  "
                 f"trades={ms.trade_count}  notional={_fmt_money(ms.total_notional)}")
        if cm:
            L.append(f"     Committees: {cm}")
        for t in sorted(ms.recent_trades, key=lambda x: x.txn_date, reverse=True)[:25]:
            px = f" @ {_px_close(t)} (day {_px_range(t)})" if t.px_close is not None else ""
            L.append(f"       {t.txn_date}  {_action_label(t):4s} {t.ticker:6s} "
                     f"{t.raw_amount}{px}  (lag {t.disclosure_lag_days}d)")
        L.append("")
    L.append("")
    L.append("AGGREGATE LTM SIGNAL (net $)")
    L.append("  Most bought: " + ", ".join(f"{tk} {_fmt_money(v)}" for tk, v in top_buys))
    L.append("  Most sold:   " + ", ".join(f"{tk} {_fmt_money(v)}" for tk, v in top_sells))
    L.append("")
    L.append("YOUR ALPACA PAPER ACCOUNT")
    L.append(f"  equity ${acct.equity:,.2f} · cash ${acct.cash:,.2f} · {len(positions)} positions")
    for p in positions:
        L.append(f"    {p.ticker:6s} qty={p.qty:.0f} avg=${p.avg_entry_price:.2f} "
                 f"mv=${p.market_value:,.0f} pl={p.unrealized_plpc*100:+.1f}%")
    L.append("")
    L.append("Disclosures lag trades 30–45d (STOCK Act). Amounts are ranges; returns "
             "assume midpoints. Leaderboard is one year and outlier-sensitive. "
             "No trades placed — recommendation only.")
    return "\n".join(L)
