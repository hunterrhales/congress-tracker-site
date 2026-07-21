"""Track which filings we've already emailed so each run only highlights new ones."""
from __future__ import annotations

import json
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
STATE_DIR.mkdir(exist_ok=True)
SEEN_FILE = STATE_DIR / "seen_filings.json"
LAST_SENT_FILE = STATE_DIR / "last_sent.txt"


def already_sent(day) -> bool:
    """True if a report was already emailed on `day` (a date)."""
    if not LAST_SENT_FILE.exists():
        return False
    return LAST_SENT_FILE.read_text().strip() == day.isoformat()


def mark_sent(day) -> None:
    LAST_SENT_FILE.write_text(day.isoformat())


LAST_RUN_FILE = STATE_DIR / "last_run.txt"


def minutes_since_last_run() -> float | None:
    """Minutes since the last successful publish, or None if never."""
    from datetime import datetime
    if not LAST_RUN_FILE.exists():
        return None
    try:
        ts = datetime.fromisoformat(LAST_RUN_FILE.read_text().strip())
    except ValueError:
        return None
    return (datetime.now() - ts).total_seconds() / 60


def mark_run() -> None:
    from datetime import datetime
    LAST_RUN_FILE.write_text(datetime.now().isoformat(timespec="seconds"))


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except json.JSONDecodeError:
        return set()


def save_seen(ids: set[str]) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(ids)))


def key(chamber: str, filing_id: str) -> str:
    return f"{chamber}:{filing_id}"
