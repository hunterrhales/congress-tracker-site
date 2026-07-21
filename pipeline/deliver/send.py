"""Email sender. Uses SMTP if SMTP_* env vars are set, otherwise writes the
email to state/outbox/ as a .html file so the user can see it (useful for
dry-run before wiring SMTP)."""
from __future__ import annotations

import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

OUTBOX = Path(__file__).resolve().parent.parent / "state" / "outbox"
OUTBOX.mkdir(parents=True, exist_ok=True)


def send(subject: str, html: str, text: str | None = None) -> str:
    # EMAIL_TO may be a comma-separated list of recipients.
    recipients = [a.strip() for a in os.environ.get("EMAIL_TO", "").split(",") if a.strip()]
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    if smtp_host and smtp_user and smtp_pass and recipients:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = os.environ.get("EMAIL_FROM") or smtp_user
        msg["To"] = ", ".join(recipients)
        # Order matters: clients render the LAST part they can display, so
        # text first, HTML second.
        if text:
            msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))
        port = int(os.environ.get("SMTP_PORT", "587"))
        with smtplib.SMTP(smtp_host, port) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg, to_addrs=recipients)
        return f"sent via SMTP to {', '.join(recipients)}"
    # Dry run: write to outbox
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = OUTBOX / f"{ts}.html"
    path.write_text(html)
    return f"dry-run: wrote {path}"
