"""Main entry: pull new Senate + House disclosures, rank, compose email, send.

Usage:
  python run.py                # normal run, sends email (or writes to outbox)
  python run.py --dry-run      # forces outbox-only delivery
  python run.py --ltm-days N   # ranking window (default 365)
  python run.py --backfill N   # look back N days for new filings (default 7)
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings
from datetime import date, timedelta

# Hide the harmless urllib3/LibreSSL warning that fires on macOS system Python.
warnings.filterwarnings("ignore", message=".*OpenSSL.*", category=Warning)

from dotenv import load_dotenv

import json
from pathlib import Path

from core import alpaca, ranker, trends as trends_mod
from core.normalize import all_trades
from deliver import email_body, publish, send, sitedata, state
from sources import house, senate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Force outbox-only delivery, ignore SMTP env vars.")
    parser.add_argument("--ltm-days", type=int, default=365,
                        help="Ranking window in days (default 365).")
    parser.add_argument("--backfill", type=int, default=7,
                        help="Look back N days for new filings (default 7).")
    parser.add_argument("--feed-days", type=int, default=60,
                        help="Show every disclosure filed in the last N days on the site (default 60).")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the weekday + once-per-day guard (manual sends).")
    args = parser.parse_args()

    load_dotenv()
    if args.dry_run:
        os.environ.pop("SMTP_HOST", None)

    today = date.today()

    # --- refresh guard ------------------------------------------------------
    # The launchd agent fires at several weekday times AND at every login/boot
    # (RunAtLoad). Since the site benefits from intraday refreshes (unlike the
    # old once-a-day email), we only rate-limit: skip if the last successful
    # publish was under 2 hours ago, and skip weekends (no filings post and
    # markets are closed). --force / --dry-run bypass.
    if not args.force and not args.dry_run:
        if today.weekday() >= 5:  # 5=Sat, 6=Sun
            print(f"[{today}] weekend — skipping refresh.")
            return 0
        mins = state.minutes_since_last_run()
        if mins is not None and mins < 120:
            print(f"[{today}] last refresh {mins:.0f}m ago — skipping (rate limit).")
            return 0

    new_cutoff = today - timedelta(days=args.backfill)
    ltm_cutoff = today - timedelta(days=args.ltm_days)
    # We pull LTM-wide data for ranking; "new" trades are the subset filed
    # after `new_cutoff` AND not yet recorded in state.
    pull_since = min(new_cutoff, ltm_cutoff)

    print(f"[{today}] pulling filings since {pull_since} "
          f"(new window: {args.backfill}d, ranking window: {args.ltm_days}d)")

    # === Senate ===
    # Wrapped so a total Senate-side failure (eFD down/slow) still lets the
    # email go out with House data rather than crashing the whole run.
    source_errors: list[str] = []
    print("  fetching Senate eFD...")
    senate_txns = []
    paper_skipped = 0
    try:
        senate_filings = senate.search_ptrs(pull_since)
        paper_skipped = sum(1 for f in senate_filings if f.is_paper)
        for f in senate_filings:
            if f.is_paper:
                continue
            senate_txns.extend(senate.fetch_transactions(f))
        print(f"    {len(senate_filings)} filings ({paper_skipped} paper), "
              f"{len(senate_txns)} digital transactions")
    except Exception as e:
        source_errors.append(f"Senate eFD fetch failed: {e}")
        print(f"    [error] Senate fetch failed, continuing without it: {e}")

    # === House ===
    print("  fetching House Clerk...")
    house_txns = []
    try:
        house_filings = house.list_ptr_filings(today.year, pull_since)
        # If our window crosses a year boundary, grab last year's index too.
        if pull_since.year < today.year:
            house_filings += house.list_ptr_filings(today.year - 1, pull_since)
        for f in house_filings:
            house_txns.extend(house.fetch_transactions(f))
        print(f"    {len(house_filings)} filings, {len(house_txns)} transactions")
    except Exception as e:
        source_errors.append(f"House Clerk fetch failed: {e}")
        print(f"    [error] House fetch failed, continuing without it: {e}")

    if source_errors and not senate_txns and not house_txns:
        # Both sources dead — don't send a hollow email or mark today done.
        print("  [abort] both data sources failed; will retry on next run.")
        return 1

    # === Normalize ===
    trades = all_trades(senate_txns, house_txns)
    ltm_trades = [t for t in trades if t.txn_date >= ltm_cutoff]
    # The SITE feed shows every disclosure filed in the last `feed_days` (not the
    # email-era "new since last run" diff), so it's never mysteriously empty.
    feed_cutoff = today - timedelta(days=args.feed_days)
    feed_trades = [t for t in trades if t.notification_date >= feed_cutoff]
    print(f"  {len(trades)} priceable trades parsed "
          f"({len(ltm_trades)} in LTM window, {len(feed_trades)} filed in last {args.feed_days}d)")

    # === Rank ===
    print("  pricing + ranking (this may take a minute on first run)...")
    stats = ranker.simulate(ltm_trades, window_days=args.ltm_days)
    ranking = ranker.rank(stats)
    if ranking:
        print(f"    top performer: {ranking[0].member} "
              f"({ranking[0].return_pct:+.1f}%, {ranking[0].trade_count} trades)")

    # === Cross-Congress trends ===
    # Wrapped so a slow sector lookup can never sink the email.
    print("  computing cross-Congress trends...")
    try:
        trends = trends_mod.compute(ltm_trades, window_days=90)
        print(f"    {len(trends.consensus_buys)} consensus buys, "
              f"{len(trends.consensus_sells)} consensus sells, "
              f"{len(trends.sector_flows)} sectors")
    except Exception as e:
        trends = None
        print(f"    [warn] trends computation failed, omitting section: {e}")

    # === Alpaca (optional) ===
    # Skipped in the cloud build (SKIP_ALPACA=1) so no brokerage keys are
    # needed off-machine. When skipped, the site hides the paper-account panel.
    acct, positions = None, []
    if os.environ.get("SKIP_ALPACA") == "1":
        print("  skipping Alpaca (SKIP_ALPACA=1)")
    else:
        try:
            print("  fetching Alpaca paper account...")
            acct = alpaca.get_account()
            positions = alpaca.get_positions()
            print(f"    equity ${acct.equity:,.2f}, {len(positions)} positions")
        except Exception as e:
            print(f"    [warn] Alpaca fetch failed, hiding that panel: {e}")
            acct, positions = None, []

    # === New-detection: diff this run's feed against the last published one ===
    prev_keys = sitedata.load_prev_keys()
    new_objs = [t for t in feed_trades if sitedata.trade_key(t) not in prev_keys]
    new_keys = {sitedata.trade_key(t) for t in new_objs}
    # On the very first publish there is no previous data, so everything would
    # look "new" — suppress that so the first load doesn't flag 600 rows.
    if not prev_keys:
        new_keys = set()
    print(f"  {len(new_keys)} disclosures new since last publish")

    # === Build review + site payload ===
    try:
        from core import review as review_mod
        review = review_mod.build(
            new_trades=new_objs, trends=trends, ranking=ranking, today=today)
    except Exception:
        review = []

    payload = sitedata.build_payload(
        feed_trades=feed_trades,
        new_keys=new_keys,
        ranking=ranking,
        trends=trends,
        review=review,
        alpaca_account=acct,
        alpaca_positions=positions,
        paper_filings_skipped=paper_skipped,
        data_warnings=source_errors,
        feed_days=args.feed_days,
    )
    out_path = sitedata.DATA_FILE
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload))
    print(f"  {out_path} written ({len(payload['feed'])} feed entries)")

    # === Publish to GitHub Pages (local Mac path only; the cloud workflow
    #     commits data.json itself, so it leaves GITHUB_TOKEN unset here) ===
    if args.dry_run:
        print("  dry-run: not pushing to GitHub Pages")
    else:
        try:
            print(f"  {publish.publish()}")
        except Exception as e:
            print(f"  [error] publish failed: {e}")
            return 1

    # === Optional legacy email (disabled unless EMAIL_ENABLED=1 in .env) ===
    if os.environ.get("EMAIL_ENABLED") == "1":
        subject, html, text = email_body.compose(
            new_trades=new_objs, all_trades_ltm=ltm_trades, ranking=ranking,
            alpaca_account=acct, alpaca_positions=positions,
            paper_filings_skipped=paper_skipped, trends=trends,
            data_warnings=source_errors,
        )
        print(f"  {send.send(subject, html, text)}")

    # Record the successful publish time for the local rate-limit guard.
    if not args.dry_run:
        state.mark_run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
