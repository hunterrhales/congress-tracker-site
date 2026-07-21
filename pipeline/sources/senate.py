"""Senate eFD scraper.

Handshake flow: GET /search/home/ to grab CSRF + accept the prohibition
agreement, then POST it back. After that the session can hit the PTR
search JSON endpoint and view individual PTR HTML pages.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterator

import requests
from bs4 import BeautifulSoup

from sources import _http

BASE = "https://efdsearch.senate.gov"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/121"


@dataclass
class SenateFiling:
    first_name: str
    last_name: str
    title: str
    ptr_url: str
    filing_date: date
    ptr_id: str
    is_paper: bool


@dataclass
class SenateTransaction:
    filing: SenateFiling
    txn_date: date
    owner: str
    ticker: str
    asset_name: str
    asset_type: str
    action: str          # "Purchase" | "Sale (Full)" | "Sale (Partial)" | "Exchange"
    amount_range: str    # e.g. "$15,001 - $50,000"
    comment: str


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    home = s.get(f"{BASE}/search/home/", timeout=20)
    home.raise_for_status()
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', home.text)
    if not token:
        raise RuntimeError("Could not find Senate eFD CSRF token on home page")
    csrf = token.group(1)
    accept = s.post(
        f"{BASE}/search/home/",
        data={"prohibition_agreement": "1", "csrfmiddlewaretoken": csrf},
        headers={"Referer": f"{BASE}/search/home/"},
        allow_redirects=False,
        timeout=20,
    )
    if accept.status_code not in (200, 302):
        raise RuntimeError(f"Senate eFD agreement POST returned {accept.status_code}")
    s.headers["X-CSRFToken"] = s.cookies.get("csrftoken", csrf)
    return s


def search_ptrs(since: date) -> list[SenateFiling]:
    s = _new_session()
    payload = {
        "start": "0",
        "length": "100",
        "report_types": "[11]",          # 11 = Periodic Transaction Report
        "filer_types": "[]",
        "submitted_start_date": since.strftime("%m/%d/%Y") + " 00:00:00",
        "submitted_end_date": "",
        "candidate_state": "",
        "senator_state": "",
        "office_id": "",
        "first_name": "",
        "last_name": "",
        "csrfmiddlewaretoken": s.cookies.get("csrftoken"),
    }
    out: list[SenateFiling] = []
    while True:
        r = _http.post(
            s,
            f"{BASE}/search/report/data/",
            data=payload,
            headers={"Referer": f"{BASE}/search/", "X-Requested-With": "XMLHttpRequest"},
        )
        r.raise_for_status()
        data = r.json()
        for row in data.get("data", []):
            first, last, title, link_html, filed = row
            m = re.search(r'href="([^"]+)"', link_html)
            if not m:
                continue
            href = m.group(1)
            is_paper = "/view/paper/" in href
            ptr_id = href.rstrip("/").rsplit("/", 1)[-1]
            try:
                fdate = datetime.strptime(filed, "%m/%d/%Y").date()
            except ValueError:
                continue
            out.append(
                SenateFiling(
                    first_name=first.strip(),
                    last_name=last.strip(),
                    title=title.strip(),
                    ptr_url=BASE + href,
                    filing_date=fdate,
                    ptr_id=ptr_id,
                    is_paper=is_paper,
                )
            )
        total = data.get("recordsFiltered", 0)
        start = int(payload["start"]) + int(payload["length"])
        if start >= total:
            break
        payload["start"] = str(start)
    # Re-attach the session so we can fetch each PTR HTML with the same cookies
    for f in out:
        f._session = s  # type: ignore[attr-defined]
    return out


_AMOUNT_RE = re.compile(r"\$[\d,]+\s*-\s*\$[\d,]+|\$[\d,]+\+|Over \$[\d,]+", re.I)


def fetch_transactions(filing: SenateFiling) -> list[SenateTransaction]:
    """Parse one digital PTR HTML page into transactions.

    Paper-filed PTRs are scanned PDFs and are not handled here — caller
    should check `filing.is_paper` and surface them as 'manual review'.
    """
    if filing.is_paper:
        return []
    s: requests.Session = getattr(filing, "_session", None) or _new_session()
    try:
        r = _http.get(s, filing.ptr_url)
        r.raise_for_status()
    except requests.RequestException as e:
        # A single flaky PTR fetch must not crash the whole run. Skip it; the
        # filing is unseen-state so it'll be retried on the next run.
        print(f"    [warn] skipping Senate PTR {filing.ptr_id} ({filing.last_name}): {e}")
        return []
    soup = BeautifulSoup(r.text, "lxml")
    table = soup.find("table")
    if not table:
        return []
    rows = table.find_all("tr")
    out: list[SenateTransaction] = []
    for tr in rows[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if len(cells) < 8:
            continue
        # cols: #, Transaction Date, Owner, Ticker, Asset Name, Asset Type, Type, Amount, Comment
        try:
            txn_date = datetime.strptime(cells[1], "%m/%d/%Y").date()
        except ValueError:
            continue
        out.append(
            SenateTransaction(
                filing=filing,
                txn_date=txn_date,
                owner=cells[2],
                ticker=cells[3].strip("- ").upper(),
                asset_name=cells[4],
                asset_type=cells[5],
                action=cells[6],
                amount_range=cells[7],
                comment=cells[8] if len(cells) > 8 else "",
            )
        )
    return out


def iter_recent_transactions(since: date) -> Iterator[SenateTransaction]:
    """Yield every transaction across every PTR filed since `since`."""
    for filing in search_ptrs(since):
        if filing.is_paper:
            continue
        for txn in fetch_transactions(filing):
            yield txn


if __name__ == "__main__":
    from datetime import timedelta
    since = date.today() - timedelta(days=30)
    filings = search_ptrs(since)
    print(f"{len(filings)} Senate PTRs filed since {since}")
    for f in filings[:5]:
        print(f"  {f.filing_date} {f.last_name}, {f.first_name} "
              f"({'paper' if f.is_paper else 'digital'})")
    if filings:
        first_digital = next((f for f in filings if not f.is_paper), None)
        if first_digital:
            txns = fetch_transactions(first_digital)
            print(f"\nFirst digital PTR ({first_digital.last_name}): {len(txns)} transactions")
            for t in txns[:5]:
                print(f"  {t.txn_date} {t.action:18s} {t.ticker:6s} {t.amount_range}")
