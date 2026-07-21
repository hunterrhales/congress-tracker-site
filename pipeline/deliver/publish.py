"""Publish the site/ directory to GitHub Pages.

Setup expected in .env:
  GITHUB_TOKEN=<personal access token with repo scope>
  GITHUB_REPO=<owner/repo, e.g. hunterhales/congress-tracker-site>

First publish bootstraps everything: creates the repo if missing, initializes
git in site/, pushes, and enables GitHub Pages via the API. Subsequent runs
just commit data.json and push. If no token is configured we skip quietly —
the site still regenerates locally in site/.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import requests

SITE_DIR = Path(__file__).resolve().parent.parent / "site"
API = "https://api.github.com"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(SITE_DIR), *args],
                          capture_output=True, text=True, timeout=120)


def _gh_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json"}


def _ensure_repo(token: str, repo: str) -> None:
    r = requests.get(f"{API}/repos/{repo}", headers=_gh_headers(token), timeout=20)
    if r.status_code == 200:
        return
    name = repo.split("/", 1)[1]
    r = requests.post(f"{API}/user/repos", headers=_gh_headers(token), timeout=20,
                      json={"name": name, "private": False,
                            "description": "Congress trade tracker dashboard (auto-updated)",
                            "has_issues": False, "has_wiki": False})
    r.raise_for_status()


def _ensure_pages(token: str, repo: str) -> str:
    h = _gh_headers(token)
    r = requests.get(f"{API}/repos/{repo}/pages", headers=h, timeout=20)
    if r.status_code == 404:
        r = requests.post(f"{API}/repos/{repo}/pages", headers=h, timeout=20,
                          json={"source": {"branch": "main", "path": "/"}})
        if r.status_code not in (201, 409):
            r.raise_for_status()
        r = requests.get(f"{API}/repos/{repo}/pages", headers=h, timeout=20)
    return r.json().get("html_url", f"https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/")


def publish() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPO", "").strip()
    if not token or not repo:
        return "site updated locally (no GITHUB_TOKEN/GITHUB_REPO configured — not pushed)"

    _ensure_repo(token, repo)

    remote = f"https://x-access-token:{token}@github.com/{repo}.git"
    if not (SITE_DIR / ".git").exists():
        _git("init", "-b", "main")
        _git("config", "user.email", "tracker@localhost")
        _git("config", "user.name", "Congress Tracker Bot")
    # Always (re)set the remote so a rotated token takes effect.
    _git("remote", "remove", "origin")
    _git("remote", "add", "origin", remote)

    _git("add", "-A")
    commit = _git("commit", "-m", "update data")
    # "nothing to commit" is fine — still make sure remote is current.
    push = _git("push", "-u", "origin", "main", "--force-with-lease")
    if push.returncode != 0:
        # First push or diverged remote — plain push.
        push = _git("push", "-u", "origin", "main", "--force")
        if push.returncode != 0:
            raise RuntimeError(f"git push failed: {push.stderr.strip()[:300]}")

    url = _ensure_pages(token, repo)
    return f"published to {url}"


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    print(publish())
