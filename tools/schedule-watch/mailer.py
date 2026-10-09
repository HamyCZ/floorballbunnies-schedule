#!/usr/bin/env python3
"""
Send digest / alert mail via existing SMTP mailbox (preferred) or Resend.

Secrets must come from the environment (GitHub Actions Secrets) — never from
committed config. This module never logs passwords or API keys.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Any


def _emails(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_emails(item))
        return out
    return []


def smtp_configured() -> bool:
    return bool((os.getenv("SMTP_HOST") or "").strip() and (os.getenv("SMTP_PASSWORD") or "").strip())


def resend_configured() -> bool:
    return bool((os.getenv("RESEND_API_KEY") or "").strip())


def mail_ready() -> bool:
    return smtp_configured() or resend_configured()


def alert_from() -> str:
    return (os.getenv("ALERT_FROM") or "").strip()


def send_smtp(
    *,
    frm: str,
    to: list[str],
    subject: str,
    text: str,
    html: str | None = None,
) -> str:
    host = (os.getenv("SMTP_HOST") or "").strip()
    if not host:
        raise RuntimeError("SMTP_HOST is not set")
    port = int(os.getenv("SMTP_PORT") or "587")
    user = (os.getenv("SMTP_USER") or "").strip() or frm
    password = os.getenv("SMTP_PASSWORD") or ""
    if not password:
        raise RuntimeError("SMTP_PASSWORD is not set")
    # Default: STARTTLS on 587. Set SMTP_SSL=1 for implicit TLS (465).
    use_ssl = (os.getenv("SMTP_SSL") or "").strip().lower() in {"1", "true", "yes"}

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = ", ".join(to)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    if use_ssl:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, timeout=60, context=context) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(msg)
    return f"smtp:{host}:{port} → {len(to)} recipient(s)"


def send_resend(
    *,
    api_key: str,
    frm: str,
    to: list[str],
    subject: str,
    text: str,
    html: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "from": frm,
        "to": to,
        "subject": subject,
        "text": text,
    }
    if html:
        payload["html"] = html
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode()[:500]
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        raise RuntimeError(f"Resend HTTP {e.code}: {body}") from e


def deliver(
    *,
    to: list[str],
    subject: str,
    text: str,
    html: str | None = None,
    frm: str | None = None,
) -> str:
    """
    Deliver mail. Prefer SMTP (existing mailbox); Resend only if SMTP is unset.
    Never prints credentials.
    """
    recipients = [a for a in to if a]
    if not recipients:
        raise RuntimeError("No recipients")
    from_addr = (frm or alert_from()).strip()
    if not from_addr:
        raise RuntimeError("ALERT_FROM is not set")

    if smtp_configured():
        return send_smtp(frm=from_addr, to=recipients, subject=subject, text=text, html=html)

    api_key = (os.getenv("RESEND_API_KEY") or "").strip()
    if api_key:
        return send_resend(
            api_key=api_key,
            frm=from_addr,
            to=recipients,
            subject=subject,
            text=text,
            html=html,
        )

    raise RuntimeError("No mail transport configured (set SMTP_* or RESEND_API_KEY)")
