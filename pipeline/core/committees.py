"""Committee assignment lookup.

Source: the unitedstates/congress-legislators community dataset, which
mirrors the official House/Senate committee membership rosters. Two files:

  - committees-current.yaml         thomas_id -> committee name
  - committee-membership-current.yaml  thomas_id -> [{name, bioguide, ...}]

We normalize member names to "lastfirst" (lowercase, ascii) so the lookup
tolerates differences like "Sheldon Whitehouse" vs "Whitehouse, Sheldon"
and "Debbie Wasserman Schultz" vs "Wasserman Schultz, Debbie".

Cache is 24h. Cold fetch is ~750KB total over GitHub raw — well under the
time budget for a once-a-day run.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "committees"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_CMTES_URL = "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/committees-current.yaml"
_MEMBERSHIP_URL = "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/committee-membership-current.yaml"

_TTL = timedelta(hours=24)


def _fetch(url: str, filename: str) -> bytes:
    p = CACHE_DIR / filename
    if p.exists() and datetime.now() - datetime.fromtimestamp(p.stat().st_mtime) < _TTL:
        return p.read_bytes()
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    p.write_bytes(r.content)
    return r.content


_MIDDLE_INITIAL_RE = re.compile(r"^[A-Z]\.?$")
_SUFFIX_RE = re.compile(r"^(jr|sr|ii|iii|iv|v)\.?$", re.IGNORECASE)


def _drop_middle_initials(tokens: list[str]) -> list[str]:
    return [t for t in tokens if not _MIDDLE_INITIAL_RE.match(t)]


def _strip_suffixes(s: str) -> str:
    """Remove generational suffixes (Jr./Sr./II/III) wherever they appear so
    that 'King, Jr., Angus S' and 'Angus S. King, Jr.' normalize to the same
    key as 'Angus King'."""
    tokens = re.split(r"(\s+|,)", s)
    cleaned = [t for t in tokens if not _SUFFIX_RE.match(t.strip(",.").strip())]
    out = "".join(cleaned)
    # Collapse leftover comma-space noise like ", ," -> ","
    out = re.sub(r",\s*,", ",", out)
    out = re.sub(r",\s*$", "", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _normalize(s: str) -> str:
    """Last+first lowercase ASCII letters only, no punctuation, no spaces.

    Accepts both "Sheldon Whitehouse" and "Whitehouse, Sheldon" forms and
    produces the same key for both. Middle initials ("T.", "M") are dropped
    so e.g. "McCaul, Michael T." matches "Michael T. McCaul".
    """
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"\s+", " ", s).strip()
    s = _strip_suffixes(s)
    if "," in s:
        last_part, _, first_part = s.partition(",")
        last_tokens = _drop_middle_initials(last_part.strip().split())
        first_tokens = _drop_middle_initials(first_part.strip().split())
        if not first_tokens or not last_tokens:
            return re.sub(r"[^a-z]", "", s.lower())
        last, first = " ".join(last_tokens), first_tokens[0]
    else:
        tokens = _drop_middle_initials(s.split())
        if len(tokens) < 2:
            return re.sub(r"[^a-z]", "", s.lower())
        first, last = tokens[0], " ".join(tokens[1:])
    key = (last + first).lower()
    return re.sub(r"[^a-z]", "", key)


# Friendly short names for the most-relevant committees. The yaml has long
# titles ("United States Senate Committee on Banking, Housing, and Urban
# Affairs") that bloat the email — these compress them.
_SHORT = {
    "Senate Committee on Banking, Housing, and Urban Affairs": "Banking",
    "Senate Committee on Finance": "Finance",
    "Senate Committee on Health, Education, Labor, and Pensions": "HELP",
    "Senate Committee on Armed Services": "Armed Services",
    "Senate Committee on Energy and Natural Resources": "Energy",
    "Senate Committee on Commerce, Science, and Transportation": "Commerce",
    "Senate Committee on Foreign Relations": "Foreign Relations",
    "Senate Committee on the Judiciary": "Judiciary",
    "Senate Committee on Intelligence": "Intelligence",
    "Senate Committee on Appropriations": "Appropriations",
    "Senate Committee on Agriculture, Nutrition, and Forestry": "Agriculture",
    "Senate Committee on Environment and Public Works": "EPW",
    "Senate Committee on Homeland Security and Governmental Affairs": "HSGAC",
    "Senate Committee on Veterans' Affairs": "Veterans",
    "Senate Committee on Small Business and Entrepreneurship": "Small Business",
    "Senate Committee on the Budget": "Budget",
    "Senate Committee on Rules and Administration": "Rules",
    "Senate Committee on Indian Affairs": "Indian Affairs",
    "Senate Special Committee on Aging": "Aging",
    "Senate Select Committee on Ethics": "Ethics",
    "House Committee on Financial Services": "Financial Services",
    "House Committee on Ways and Means": "Ways & Means",
    "House Committee on Energy and Commerce": "Energy & Commerce",
    "House Committee on Armed Services": "Armed Services",
    "House Committee on Agriculture": "Agriculture",
    "House Committee on Appropriations": "Appropriations",
    "House Committee on Education and the Workforce": "Education",
    "House Committee on Foreign Affairs": "Foreign Affairs",
    "House Committee on Homeland Security": "Homeland Security",
    "House Committee on the Judiciary": "Judiciary",
    "House Committee on Natural Resources": "Natural Resources",
    "House Committee on Oversight and Government Reform": "Oversight",
    "House Committee on Science, Space, and Technology": "Science",
    "House Committee on Small Business": "Small Business",
    "House Committee on Transportation and Infrastructure": "Transportation",
    "House Committee on Veterans' Affairs": "Veterans",
    "House Committee on the Budget": "Budget",
    "House Committee on Rules": "Rules",
    "House Committee on Ethics": "Ethics",
    "House Permanent Select Committee on Intelligence": "Intelligence",
}


def _short_name(full: str) -> str:
    if full in _SHORT:
        return _SHORT[full]
    # Strip "United States" / "Committee on" / "House"/"Senate" prefixes for unknowns.
    s = full
    s = re.sub(r"^United States\s+", "", s)
    s = re.sub(r"^(House|Senate)\s+Committee on\s+", "", s)
    s = re.sub(r"^(House|Senate)\s+Select Committee on\s+", "", s)
    s = re.sub(r"^(House|Senate)\s+Special Committee on\s+", "", s)
    return s


def load_assignments() -> dict[str, list[str]]:
    """Return {normalized_name: [committee_short_names...]} for current Congress."""
    cmtes = yaml.safe_load(_fetch(_CMTES_URL, "committees-current.yaml"))
    membership = yaml.safe_load(_fetch(_MEMBERSHIP_URL, "committee-membership-current.yaml"))
    # thomas_id -> short committee name
    id_to_name: dict[str, str] = {}
    for c in cmtes:
        tid = c.get("thomas_id")
        if tid:
            id_to_name[tid] = _short_name(c.get("name", tid))
    # Build the member lookup. We capture top-level committees only; sub-committee
    # thomas_ids have a digit suffix appended like "SSAF12" and we skip them so
    # the email isn't cluttered with sub-committee names.
    out: dict[str, set[str]] = {}
    for thomas_id, members in membership.items():
        if thomas_id not in id_to_name:
            continue  # sub-committee — skip
        cname = id_to_name[thomas_id]
        for m in members or []:
            key = _normalize(m.get("name", ""))
            if not key:
                continue
            out.setdefault(key, set()).add(cname)
    return {k: sorted(v) for k, v in out.items()}


def committees_for(member: str, lookup: dict[str, list[str]]) -> list[str]:
    return lookup.get(_normalize(member), [])


if __name__ == "__main__":
    a = load_assignments()
    print(f"{len(a)} members with committee assignments")
    for name in ["Whitehouse, Sheldon", "Wasserman Schultz, Debbie",
                 "Gottheimer, Josh", "Fetterman, John", "McCaul, Michael T."]:
        print(f"  {name}: {committees_for(name, a)}")
