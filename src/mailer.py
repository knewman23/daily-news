"""Email the day's digest when a run finishes.

SMTP rather than AppleScript-driving Mail.app. Automating Mail.app needs macOS
Automation permission granted to the *calling* process, and a launchd agent has
no session to show that prompt in — the run would silently fail to send with
nothing in the log to explain why. SMTP has no such dependency.

The app password is read from the macOS Keychain at send time, never stored in
config.toml. config.toml is committed; a password in it would be published.

Sending never raises. A run that produced a digest and could not send the email
has still succeeded at the part that matters.
"""

from __future__ import annotations

import logging
import smtplib
import subprocess
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from typing import Callable, Sequence

log = logging.getLogger(__name__)

MAX_HEADLINES = 40


class MailError(Exception):
    """The message could not be sent."""


@dataclass
class MailResult:
    ok: bool
    sent: bool
    message: str


def keychain_password(
    service: str,
    account: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Read the SMTP app password from the login Keychain."""
    completed = runner(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True, text=True,
    )
    if completed.returncode != 0:
        raise MailError(
            f"no Keychain item for service {service!r} account {account!r}. Add one with:\n"
            f"  security add-generic-password -s {service} -a {account} -w"
        )

    password = (completed.stdout or "").strip()
    if not password:
        raise MailError(f"the Keychain item {service!r}/{account!r} is empty")
    return password


def build_message(
    day: date,
    headlines: Sequence[str],
    stats,
    site_url: str = "",
    failures: Sequence[str] = (),
) -> tuple[str, str]:
    """Subject and plain-text body.

    The headlines go in the body rather than only a link, so the email is worth
    reading on a phone lock screen without opening anything.
    """
    pretty = f"{day.strftime('%B')} {day.day}"
    count = len(headlines)

    if not count:
        subject = f"Daily News — no news for {pretty}"
    else:
        subject = f"Daily News — {count} topic{'s' if count != 1 else ''} for {pretty}"
    if failures:
        subject += " (incomplete)"

    lines = [
        f"{count} topic{'s' if count != 1 else ''} from "
        f"{getattr(stats, 'post_count', 0)} post"
        f"{'s' if getattr(stats, 'post_count', 0) != 1 else ''}"
        f" ({getattr(stats, 'transcribed_count', 0)} with usable text).",
        "",
    ]

    for headline in headlines[:MAX_HEADLINES]:
        lines.append(f"  • {headline}")
    if count > MAX_HEADLINES:
        lines.append(f"  … and {count - MAX_HEADLINES} more")

    if failures:
        lines += ["", "Problems during this run:"]
        lines += [f"  ! {note}" for note in failures]

    if site_url:
        lines += ["", f"Read it all: {site_url}"]

    return subject, "\n".join(lines) + "\n"


def send(
    cfg,
    subject: str,
    body: str,
    transport: Callable[[EmailMessage, str, str, int], None] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> MailResult:
    """Send one message. Never raises."""
    email_cfg = cfg.email
    if not email_cfg.enabled:
        return MailResult(True, False, "email is disabled in config.toml")
    if not email_cfg.to:
        return MailResult(False, False, "no [email] to address configured")

    try:
        password = keychain_password(
            email_cfg.keychain_service,
            email_cfg.keychain_account or email_cfg.sender or email_cfg.to,
            runner=runner,
        )
    except MailError as exc:
        log.error("cannot send email: %s", exc)
        return MailResult(False, False, str(exc))

    message = EmailMessage()
    message["From"] = email_cfg.sender or email_cfg.to
    message["To"] = email_cfg.to
    message["Subject"] = subject
    message.set_content(body)

    deliver = transport or _smtp_transport(
        email_cfg.sender or email_cfg.to, password,
    )

    try:
        deliver(message, email_cfg.smtp_host, email_cfg.smtp_port, 0)
    except Exception as exc:
        log.error("sending failed: %s", exc)
        return MailResult(False, False, f"send failed: {exc}")

    log.info("emailed %s: %s", email_cfg.to, subject)
    return MailResult(True, True, f"emailed {email_cfg.to}")


# --- internals -------------------------------------------------------------


def _smtp_transport(username: str, password: str):
    def deliver(message: EmailMessage, host: str, port: int, _unused: int) -> None:
        # STARTTLS on 587 rather than implicit TLS on 465: it is what Gmail
        # documents, and smtplib's SMTP_SSL gives worse error messages when the
        # password is wrong.
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)

    return deliver
