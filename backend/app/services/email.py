"""Transactional email — password reset.

Uses the stdlib ``smtplib`` so there is NO new dependency. If SMTP isn't
configured (``SMTP_HOST`` blank), we log the reset link instead of sending —
so the flow is fully testable on dev/staging before a relay is provisioned.

Send errors are swallowed (logged, not raised): the caller schedules this as a
background task after the HTTP response, and a relay hiccup must not surface to
the user (it would also leak whether an email is registered).
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings

log = logging.getLogger(__name__)


def send_password_reset_email(to_email: str, reset_link: str) -> None:
    s = get_settings()
    body = (
        "We received a request to reset your password.\n\n"
        "Click the link below to choose a new one (valid for 30 minutes):\n\n"
        f"{reset_link}\n\n"
        "If you didn't request this, you can safely ignore this email."
    )

    if not s.smtp_configured:
        # No relay configured — log the link so the flow is testable in dev.
        log.warning(
            "SMTP not configured (SMTP_HOST blank); password-reset link for %s: %s",
            to_email, reset_link,
        )
        return

    msg = EmailMessage()
    msg["Subject"] = "Reset your password"
    msg["From"] = s.email_from
    msg["To"] = to_email
    msg.set_content(body)

    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as server:
            if s.smtp_use_tls:
                server.starttls()
            if s.smtp_user:
                server.login(s.smtp_user, s.smtp_password)
            server.send_message(msg)
        log.info("sent password-reset email to %s", to_email)
    except Exception:  # noqa: BLE001
        log.exception("failed to send password-reset email to %s", to_email)
