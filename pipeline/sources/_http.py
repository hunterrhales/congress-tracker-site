"""Resilient HTTP helpers shared by the scrapers.

Government disclosure sites (efdsearch.senate.gov especially) are slow and
intermittently time out. A single failed request must never crash the daily
run, so these wrappers retry with backoff and let callers decide whether to
skip-and-continue on final failure.
"""
from __future__ import annotations

import time

import requests

_RETRYABLE = (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError)


def get(session_or_requests, url, *, timeout=45, retries=3, backoff=2.0, **kwargs):
    last_exc = None
    for attempt in range(retries):
        try:
            return session_or_requests.get(url, timeout=timeout, **kwargs)
        except _RETRYABLE as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise last_exc


def post(session, url, *, timeout=45, retries=3, backoff=2.0, **kwargs):
    last_exc = None
    for attempt in range(retries):
        try:
            return session.post(url, timeout=timeout, **kwargs)
        except _RETRYABLE as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise last_exc
