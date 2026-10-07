"""The SMTP sender: Mailpit only, and which failures are worth a retry."""

import socket
import socketserver
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from pydantic import ValidationError

from sales_ops.sending import PermanentSendError, SMTPMailer, TransientSendError
from sales_ops.worker import WorkerSettings
from tests.helpers import Mailpit, unique_email


def send(mailer: SMTPMailer, recipient: str, subject: str = "Subject", body: str = "Body") -> None:
    mailer.send(
        message_id="<test@sales-ops.example>",
        sender="sales@sales-ops.example",
        recipient=recipient,
        subject=subject,
        body=body,
    )


# Mailpit only (D-11) --------------------------------------------------------------------------


def test_the_mailer_refuses_any_host_but_mailpit() -> None:
    with pytest.raises(ValueError, match="only to Mailpit"):
        SMTPMailer("smtp.example.com", 25)


def test_the_worker_refuses_to_start_with_another_smtp_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    with pytest.raises(ValidationError, match="only to Mailpit"):
        WorkerSettings(llm_provider="fake")


def test_a_mail_arrives_as_written(mailpit: Mailpit, smtp: SMTPMailer) -> None:
    recipient = unique_email()
    body = (
        "こんにちは。\n\nお問い合わせありがとうございます。" + "とても長い行です。" * 20 + "\n\n"
        "The Sales Team"
    )

    smtp.send(
        message_id="<arrives-as-written@sales-ops.example>",
        sender="sales@sales-ops.example",
        recipient=recipient,
        subject="お問い合わせありがとうございます",
        body=body,
    )

    [mail] = mailpit.messages_to(recipient)
    assert mail["MessageID"] == "arrives-as-written@sales-ops.example"
    assert mail["From"]["Address"] == "sales@sales-ops.example"
    assert mail["Subject"] == "お問い合わせありがとうございます"
    assert mailpit.text(mail) == body


# Which failures are retried -------------------------------------------------------------------


class _Handler(socketserver.StreamRequestHandler):
    """Just enough SMTP to fail at a chosen step with a chosen reply."""

    replies: dict[str, str] = {}

    def reply(self, step: str, default: str) -> None:
        self.wfile.write((self.replies.get(step, default) + "\r\n").encode())

    def handle(self) -> None:
        self.reply("GREETING", "220 fake ready")
        while line := self.rfile.readline():
            command = line[:4].upper().decode()
            if command in ("EHLO", "HELO"):
                self.reply(command, "250 fake")
            elif command == "DATA":
                self.wfile.write(b"354 go ahead\r\n")
                while self.rfile.readline() not in (b".\r\n", b""):
                    pass
                self.reply("DATA", "250 queued")
            elif command == "QUIT":
                self.reply("QUIT", "221 bye")
                return
            else:  # MAIL, RCPT, RSET, NOOP
                self.reply(command, "250 ok")


@contextmanager
def fake_smtp(**replies: str) -> Iterator[int]:
    handler = type("Handler", (_Handler,), {"replies": replies})
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler) as server:
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()


def mailer_on(port: int) -> SMTPMailer:
    return SMTPMailer("127.0.0.1", port, timeout_s=5)


def test_the_fake_server_accepts_a_mail_when_told_nothing() -> None:
    with fake_smtp() as port:
        send(mailer_on(port), "someone@example.com")  # no exception: the harness itself works


@pytest.mark.parametrize(
    ("replies", "kind"),
    [
        pytest.param({"GREETING": "421 4.3.2 shutting down"}, "smtp_421", id="greeting-421"),
        pytest.param({"MAIL": "451 4.3.0 try again"}, "smtp_451", id="mail-from-451"),
        pytest.param({"RCPT": "450 4.2.1 mailbox busy"}, "smtp_450", id="rcpt-450"),
        pytest.param({"RCPT": "451 4.7.1 greylisted"}, "smtp_451", id="rcpt-451-greylisting"),
        pytest.param({"DATA": "452 4.3.1 out of storage"}, "smtp_452", id="data-452"),
    ],
)
def test_4xx_replies_are_transient(replies: dict[str, str], kind: str) -> None:
    with fake_smtp(**replies) as port, pytest.raises(TransientSendError) as caught:
        send(mailer_on(port), "someone@example.com")
    assert caught.value.kind == kind


@pytest.mark.parametrize(
    ("replies", "kind"),
    [
        pytest.param({"MAIL": "550 5.7.1 sender rejected"}, "smtp_550", id="mail-from-550"),
        pytest.param({"RCPT": "550 5.1.1 no such user"}, "smtp_550", id="rcpt-550"),
        pytest.param({"DATA": "554 5.6.0 message rejected"}, "smtp_554", id="data-554"),
    ],
)
def test_5xx_replies_are_permanent(replies: dict[str, str], kind: str) -> None:
    with fake_smtp(**replies) as port, pytest.raises(PermanentSendError) as caught:
        send(mailer_on(port), "someone@example.com")
    assert caught.value.kind == kind


def test_nothing_listening_is_transient() -> None:
    with socket.socket() as probe:  # a port that was free a moment ago
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(TransientSendError) as caught:
        send(mailer_on(port), "someone@example.com")
    assert caught.value.kind == "ConnectionRefusedError"
