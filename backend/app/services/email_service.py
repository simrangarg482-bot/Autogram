"""Transactional email, sent via SMTP or the Resend HTTP API.

SMTP (SMTP_USER/SMTP_PASSWORD set, e.g. a Gmail App Password) is tried first,
then Resend (RESEND_API_KEY set) — using `httpx` directly rather than the
`resend` package, one fewer dependency for a single POST call. Neither
configured (local dev without either) is not an error: the email is logged
instead of sent, so the rest of the password-reset flow stays testable
offline.
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from app.core.config import (
    RESEND_API_KEY,
    EMAIL_FROM,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USER,
    SMTP_PASSWORD,
)

logger = logging.getLogger(__name__)

_RESEND_URL = "https://api.resend.com/emails"
_TIMEOUT_S = 10.0


def _send_via_smtp(to_email: str, subject: str, text_body: str, html_body: str) -> None:
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = EMAIL_FROM
    message["To"] = to_email
    message.attach(MIMEText(text_body, "plain"))
    message.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=_TIMEOUT_S) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(message)


def send_password_reset_email(to_email: str, reset_link: str, expire_minutes: int) -> None:
    """Best-effort send — never raises. A caller must not let email delivery
    failures leak into an API response (that would reveal account existence
    and turn a mail-provider outage into an auth-endpoint outage)."""
    subject = "Reset your Autogram password"
    text_body = (
        f"We received a request to reset your Autogram password.\n\n"
        f"Reset it here (expires in {expire_minutes} minutes):\n{reset_link}\n\n"
        f"If you didn't request this, you can safely ignore this email — "
        f"your password will not be changed."
    )
    html_body = f"""
      <div style="font-family: Arial, sans-serif; max-width: 480px; margin: 0 auto; color: #0f172a;">
        <h2 style="margin-bottom: 4px;">Reset your password</h2>
        <p style="color: #475569; line-height: 1.5;">
          We received a request to reset the password for your Autogram account.
          This link expires in {expire_minutes} minutes.
        </p>
        <p style="margin: 24px 0;">
          <a href="{reset_link}"
             style="background:#4f46e5;color:#fff;padding:12px 20px;border-radius:8px;
                    text-decoration:none;font-weight:600;display:inline-block;">
            Reset password
          </a>
        </p>
        <p style="color:#94a3b8;font-size:12px;line-height:1.5;">
          If you didn't request this, you can safely ignore this email —
          your password will not be changed. This link can only be used once.
        </p>
      </div>
    """

    if SMTP_USER and SMTP_PASSWORD:
        try:
            _send_via_smtp(to_email, subject, text_body, html_body)
        except smtplib.SMTPException:
            logger.exception("Failed to send password reset email via SMTP to %s", to_email)
        return

    if not RESEND_API_KEY:
        logger.warning(
            "No email backend configured (SMTP_USER/SMTP_PASSWORD or RESEND_API_KEY) — "
            "skipping actual send. Reset link for %s: %s",
            to_email, reset_link,
        )
        return

    try:
        response = httpx.post(
            _RESEND_URL,
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": EMAIL_FROM,
                "to": [to_email],
                "subject": subject,
                "html": html_body,
                "text": text_body,
            },
            timeout=_TIMEOUT_S,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to send password reset email via Resend to %s", to_email)
