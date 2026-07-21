"""House Clerk scraper.

Pipeline:
  1. Download the per-year financial-disclosure ZIP (refreshed daily by the
     House Clerk). It contains an XML index of every filing.
  2. Filter the index to FilingType='P' (Periodic Transaction Report)
     filings — these are the trade disclosures we want.
  3. For each PTR, fetch the per-filing PDF and parse transactions out of
     `pdftotext -layout` output with a transaction-line regex.

Scanned/paper filings older than a few years sometimes don't extract
cleanly; we surface zero-transaction parses to the caller so the email
can flag them for manual review.
"""
from __future__ import annotations

import io
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import requests

from sources import _http

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "house"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/121"


def _resolve_pdftotext() -> str:
    """Locate the `pdftotext` binary regardless of the caller's PATH.

    launchd jobs run with a minimal PATH that excludes Homebrew, so relying on
    PATH alone meant House PDFs silently failed to parse under the scheduler.
    """
    found = shutil.which("pdftotext")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/pdftotext", "/usr/local/bin/pdftotext",
                 "/opt/local/bin/pdftotext", "/usr/bin/pdftotext"):
        if Path(cand).exists():
            return cand
    raise RuntimeError(
        "pdftotext not found — install poppler (`brew install poppler`). "
        "House filings cannot be parsed without it."
    )


_PDFTOTEXT = None  # resolved lazily on first use


@dataclass
class HouseFiling:
    first_name: str
    last_name: str
    state_dst: str
    filing_date: date
    doc_id: str
    year: int

    @property
    def pdf_url(self) -> str:
        return f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{self.year}/{self.doc_id}.pdf"


@dataclass
class HouseTransaction:
    filing: HouseFiling
    txn_date: date
    notification_date: date
    owner: str
    ticker: str
    asset_name: str
    asset_type: str
    action: str          # "Purchase" | "Sale" | "Sale (Partial)" | "Exchange"
    amount_range: str


def _download_year_index(year: int) -> bytes:
    url = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.ZIP"
    r = _http.get(requests, url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    return r.content


def list_ptr_filings(year: int, since: date) -> list[HouseFiling]:
    raw = _download_year_index(year)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        xml_name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
        with zf.open(xml_name) as fh:
            tree = ET.parse(fh)
    out: list[HouseFiling] = []
    for m in tree.getroot().findall("Member"):
        if m.findtext("FilingType") != "P":
            continue
        try:
            fdate = datetime.strptime(m.findtext("FilingDate") or "", "%m/%d/%Y").date()
        except ValueError:
            continue
        if fdate < since:
            continue
        out.append(
            HouseFiling(
                first_name=(m.findtext("First") or "").strip(),
                last_name=(m.findtext("Last") or "").strip(),
                state_dst=(m.findtext("StateDst") or "").strip(),
                filing_date=fdate,
                doc_id=(m.findtext("DocID") or "").strip(),
                year=year,
            )
        )
    return out


_ACTION_MAP = {
    "P": "Purchase",
    "S": "Sale (Full)",
    "S (partial)": "Sale (Partial)",
    "E": "Exchange",
}

# Owners that anchor a transaction row in House PTR PDFs.
_OWNER_RE = re.compile(r"^\s*(SP|DC|JT|Self)\b", re.IGNORECASE)

# Pieces extracted independently because PDF wrapping shuffles their order:
# in single-line rows the ticker sits between asset name and action, but
# when the row wraps the ticker drops to the next line and ends up appearing
# AFTER the dates/amount once stitched.
_TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,9})\)")
_TYPE_RE = re.compile(r"\[([A-Z]+)\]")
_DATES_RE = re.compile(r"(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})")
# Whitespace lookarounds keep us from matching the P/S inside "Partial"/"State".
_ACTION_RE = re.compile(
    r"(?:^|\s)(S\s*\(partial\)|P|S|E)(?=\s|$)", re.IGNORECASE
)
# Amount range tolerates arbitrary text between the two dollar figures —
# the PDF row often splits ticker/asset metadata into the middle of the range.
_AMOUNT_RE = re.compile(
    r"\$[\d,]+\s*-\s*(?:[^$\n]*?)\$[\d,]+|\$[\d,]+\+|Over\s+\$[\d,]+", re.IGNORECASE
)


def _normalize_amount(amount: str) -> str:
    m = re.match(r"(\$[\d,]+)\s*-\s*.*?(\$[\d,]+)", amount)
    return f"{m.group(1)} - {m.group(2)}" if m else amount


def _parse_row(row: str) -> tuple | None:
    """Pull (owner, asset_name, ticker, type, action, txn_date, notif_date, amount)
    out of one stitched row, tolerating column shuffles from PDF wrapping.
    Returns None if any required field is missing.
    """
    owner_m = _OWNER_RE.match(row)
    if not owner_m:
        return None
    ticker_m = _TICKER_RE.search(row)
    type_m = _TYPE_RE.search(row)
    dates_m = _DATES_RE.search(row)
    amount_m = _AMOUNT_RE.search(row)
    if not (ticker_m and dates_m and amount_m):
        return None
    asset_type = type_m.group(1) if type_m else ""
    # Action must sit between the asset name and the dates.
    before_dates = row[: dates_m.start()]
    # Strip trailing tickers/types from `before_dates` because in wrapped rows
    # the ticker is after the action.
    action_m = None
    for m in _ACTION_RE.finditer(before_dates):
        action_m = m  # take the last one before dates
    if not action_m:
        return None
    asset_name_end = min(ticker_m.start(), action_m.start())
    asset_name = row[owner_m.end():asset_name_end].strip(" -")
    return (
        owner_m.group(1),
        asset_name,
        ticker_m.group(1),
        asset_type,
        re.sub(r"\s+", " ", action_m.group(1).strip()),
        dates_m.group(1),
        dates_m.group(2),
        _normalize_amount(re.sub(r"\s+", " ", amount_m.group(0))),
    )


def _stitch_rows(text: str) -> list[str]:
    """Collapse PDF row-continuations into one line per transaction.

    A row starts on a line beginning with an owner code (SP/DC/JT/Self).
    Following non-blank, non-section-break lines are continuations until
    the next owner code or a blank line. Section-break tokens like
    "F   S   :", "S       O :", "D       :" indicate metadata lines below
    a transaction — we stop stitching there.
    """
    out: list[str] = []
    buf: list[str] = []
    META_RE = re.compile(r"^\s*(F\s+S\s*:|S\s+O\s*:|D\s+:|I\s+P\s+O)", re.I)
    for raw in text.splitlines():
        if not raw.strip():
            if buf:
                out.append(" ".join(buf))
                buf = []
            continue
        if META_RE.search(raw):
            if buf:
                out.append(" ".join(buf))
                buf = []
            continue
        if _OWNER_RE.match(raw):
            if buf:
                out.append(" ".join(buf))
            buf = [raw.strip()]
        elif buf:
            buf.append(raw.strip())
    if buf:
        out.append(" ".join(buf))
    return out


def _pdf_text(pdf_path: Path) -> str:
    global _PDFTOTEXT
    if _PDFTOTEXT is None:
        _PDFTOTEXT = _resolve_pdftotext()  # raises clearly if poppler missing
    res = subprocess.run(
        [_PDFTOTEXT, "-layout", str(pdf_path), "-"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return res.stdout


def fetch_transactions(filing: HouseFiling) -> list[HouseTransaction]:
    pdf_path = CACHE_DIR / f"{filing.year}_{filing.doc_id}.pdf"
    if not pdf_path.exists():
        try:
            r = _http.get(requests, filing.pdf_url, headers={"User-Agent": UA}, timeout=30)
        except requests.RequestException as e:
            print(f"    [warn] skipping House PTR {filing.doc_id} ({filing.last_name}): {e}")
            return []
        if r.status_code != 200:
            return []
        pdf_path.write_bytes(r.content)
    text = _pdf_text(pdf_path)
    if not text:
        return []
    out: list[HouseTransaction] = []
    for row in _stitch_rows(text):
        parsed = _parse_row(row)
        if not parsed:
            continue
        owner, asset_name, ticker, asset_type, action_raw, txn_s, notif_s, amount = parsed
        try:
            txn_date = datetime.strptime(txn_s, "%m/%d/%Y").date()
            notif_date = datetime.strptime(notif_s, "%m/%d/%Y").date()
        except ValueError:
            continue
        out.append(
            HouseTransaction(
                filing=filing,
                txn_date=txn_date,
                notification_date=notif_date,
                owner=owner.upper(),
                ticker=ticker.upper(),
                asset_name=asset_name.strip(),
                asset_type=asset_type.upper(),
                action=_ACTION_MAP.get(action_raw, action_raw),
                amount_range=re.sub(r"\s+", " ", amount).strip(),
            )
        )
    return out


def iter_recent_transactions(since: date, year: int | None = None) -> Iterator[HouseTransaction]:
    year = year or date.today().year
    for filing in list_ptr_filings(year, since):
        for txn in fetch_transactions(filing):
            yield txn


if __name__ == "__main__":
    from datetime import timedelta
    since = date.today() - timedelta(days=30)
    filings = list_ptr_filings(date.today().year, since)
    print(f"{len(filings)} House PTRs filed since {since}")
    for f in filings[:5]:
        print(f"  {f.filing_date} {f.last_name}, {f.first_name} ({f.state_dst}) DocID={f.doc_id}")
    if filings:
        sample = filings[0]
        txns = fetch_transactions(sample)
        print(f"\nFirst PTR ({sample.last_name}): {len(txns)} transactions")
        for t in txns[:8]:
            print(f"  {t.txn_date} {t.action:18s} {t.ticker:6s} {t.amount_range}")
