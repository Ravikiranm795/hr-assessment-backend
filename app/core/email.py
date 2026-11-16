"""Outbound email (per-org SMTP, optional; spec 3.2, M4). Only invite links and the SMTP test
use email; notifications are in-app only (spec 4.10).

Use ``get_email_sender().send(smtp, message)``. Tests swap in ``MemoryEmailSender`` via
``set_email_sender`` so nothing leaves the process.
"""

from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr
from typing import Literal, Protocol

from app.core.errors import AppError


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    security: Literal["none", "starttls", "ssl"]
    username: str | None
    password: str | None  # decrypted just before sending; never log
    sender_email: str
    sender_name: str | None = None

    def __repr__(self) -> str:
        return f"SmtpConfig(host={self.host!r}, port={self.port}, security={self.security!r})"


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    html: str
    text: str | None = None


class EmailSender(Protocol):
    async def send(self, smtp: SmtpConfig | None, message: OutgoingEmail) -> None: ...


def build_message(smtp: SmtpConfig, message: OutgoingEmail) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((smtp.sender_name or "", smtp.sender_email))
    msg["To"] = message.to
    msg["Subject"] = message.subject
    msg.set_content(message.text or "")
    msg.add_alternative(message.html, subtype="html")
    return msg


class SmtpEmailSender:
    async def send(self, smtp: SmtpConfig | None, message: OutgoingEmail) -> None:
        if smtp is None:
            raise AppError("SMTP_NOT_CONFIGURED")
        import aiosmtplib

        await aiosmtplib.send(
            build_message(smtp, message),
            hostname=smtp.host,
            port=smtp.port,
            username=smtp.username or None,
            password=smtp.password or None,
            use_tls=smtp.security == "ssl",
            start_tls=smtp.security == "starttls",
            timeout=20,
        )


@dataclass
class MemoryEmailSender:
    outbox: list[tuple[SmtpConfig, OutgoingEmail]] = field(default_factory=list)

    async def send(self, smtp: SmtpConfig | None, message: OutgoingEmail) -> None:
        if smtp is None:
            raise AppError("SMTP_NOT_CONFIGURED")
        self.outbox.append((smtp, message))


_sender: EmailSender | None = None


def get_email_sender() -> EmailSender:
    global _sender
    if _sender is None:
        _sender = SmtpEmailSender()
    return _sender


def set_email_sender(sender: EmailSender | None) -> None:
    global _sender
    _sender = sender
