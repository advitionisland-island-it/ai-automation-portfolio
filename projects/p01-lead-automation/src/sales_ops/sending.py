"""Sending approved follow-ups (D-11). P01 sends to Mailpit only.

The mailer refuses any SMTP host other than Mailpit, so no configuration mistake can mail a
real address. Only an explicit 5xx reply is permanent. 4xx replies (at any step, including a
refused recipient) and connection problems are transient: the worker retries them, up to its
attempt limit.
"""

import smtplib
from email.message import EmailMessage
from typing import Protocol

MAILPIT_HOSTS = frozenset({"127.0.0.1", "localhost", "mailpit"})


class TransientSendError(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class PermanentSendError(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _failure(code: int) -> TransientSendError | PermanentSendError:
    kind = f"smtp_{code}"
    return PermanentSendError(kind) if 500 <= code < 600 else TransientSendError(kind)


class Mailer(Protocol):
    def send(
        self, *, message_id: str, sender: str, recipient: str, subject: str, body: str
    ) -> None: ...


class SMTPMailer:
    def __init__(self, host: str, port: int, timeout_s: float = 10.0) -> None:
        if host not in MAILPIT_HOSTS:
            raise ValueError(f"P01 sends only to Mailpit; refusing SMTP host {host!r}")
        self.host = host
        self.port = port
        self.timeout_s = timeout_s

    def send(
        self, *, message_id: str, sender: str, recipient: str, subject: str, body: str
    ) -> None:
        message = EmailMessage()
        message["Message-ID"] = message_id  # the same on a resend, so receivers can dedupe
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout_s) as smtp:
                smtp.send_message(message)
        except smtplib.SMTPResponseException as exc:
            raise _failure(exc.smtp_code) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            # smtplib raises this for any refused RCPT, 4xx included, and it is not an
            # SMTPResponseException: classify it by its reply code like every other step
            # (evidence/p01/M3/diagnosis-rcpt-4xx.txt).
            raise _failure(max(code for code, _ in exc.recipients.values())) from exc
        except (smtplib.SMTPException, OSError) as exc:  # disconnects, refusals, timeouts
            raise TransientSendError(type(exc).__name__) from exc
