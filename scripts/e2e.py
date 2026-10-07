"""End-to-end check of the running compose stack (AC-4.2, AC-4.3). Fake LLM, Mailpit only.

1. A form submission to n8n W1 becomes a lead; the same submission again returns that lead.
2. n8n W2 mails the reviewers a link to the review page.
3. A person approves on that page: the script opens the link from the mail and posts the
   page's own form, the way a browser does.
4. The customer's mail arrives in Mailpit once, with exactly the approved text.
5. With the api stopped, a form submission fails and n8n W3 mails an alert.

Synthetic data only: every address is on example.com, and all mail goes to Mailpit.
Run it with `make e2e`, which starts the stack first.
"""

import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx2

N8N = "http://127.0.0.1:5678"
API = "http://127.0.0.1:8000"
MAILPIT = "http://127.0.0.1:8025"
REVIEWERS = "reviewers@sales-ops.example"
ALERTS = "alerts@sales-ops.example"
# How to reach the stack's compose project. The default is the local one; deploy/vm/e2e.sh
# points it at the deployed project in the M6 VM.
COMPOSE = shlex.split(os.environ.get("E2E_COMPOSE", "docker compose"))


class Failed(Exception):
    pass


def check(condition: bool, what: str) -> None:
    if not condition:
        raise Failed(what)
    print(f"   ok   {what}")


def must_find(pattern: str, text: str, what: str) -> re.Match[str]:
    match = re.search(pattern, text)
    if match is None:
        raise Failed(what)
    print(f"   ok   {what}")
    return match


def wait_for[T](what: str, probe: Callable[[], T | None], timeout_s: float = 60) -> T:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = probe()
        if value is not None:
            return value
        time.sleep(1)
    raise Failed(f"nothing after {timeout_s:.0f}s: {what}")


def mails_to(mailpit: httpx2.Client, address: str) -> list[dict[str, Any]]:
    found = mailpit.get("/api/v1/search", params={"query": f"to:{address}", "limit": 200})
    found.raise_for_status()
    return [m for m in found.json()["messages"] if [t["Address"] for t in m["To"]] == [address]]


def text_of(mailpit: httpx2.Client, message: dict[str, Any]) -> str:
    text: str = mailpit.get(f"/api/v1/message/{message['ID']}").json()["Text"]
    return text.replace("\r\n", "\n").rstrip("\n")


def submit(n8n: httpx2.Client, form: dict[str, str]) -> httpx2.Response:
    # n8n registers webhooks a few seconds after it reports healthy; 404 means "not yet".
    # Retrying is safe: the same form content always carries the same Idempotency-Key.
    def attempt() -> httpx2.Response | None:
        response = n8n.post("/webhook/lead-form", json=form)
        return None if response.status_code == 404 else response

    return wait_for("the form webhook (n8n W1)", attempt, timeout_s=30)


def compose(*args: str) -> None:
    subprocess.run([*COMPOSE, *args], check=True, capture_output=True)  # noqa: S603


def run() -> None:
    tag = uuid.uuid4().hex[:8]
    customer = f"e2e-{tag}@example.com"
    form = {
        "email": customer,
        "name": "E2E Test",
        "company": f"E2E Test KK {tag}",
        "message": "SYNTHETIC E2E TEST. We get 300 inquiries a month and want to answer faster.",
    }
    with (
        httpx2.Client(base_url=N8N, timeout=30) as n8n,
        httpx2.Client(base_url=API, timeout=30) as api,
        httpx2.Client(base_url=MAILPIT, timeout=30) as mailpit,
    ):
        print(f"== 1. form -> n8n W1 -> lead API  (customer {customer})")
        created = submit(n8n, form)
        check(created.status_code == 201, f"new lead: HTTP {created.status_code}")
        lead_id = created.json()["lead_id"]
        again = submit(n8n, form)
        check(again.json()["lead_id"] == lead_id, "the same submission again returns the same lead")
        bad = submit(n8n, {**form, "email": "not-an-email"})
        check(bad.status_code == 422, f"a bad email is the sender's error: HTTP {bad.status_code}")

        print("== 2. worker drafts; n8n W2 mails the reviewers")

        def review_mail() -> str | None:
            for message in mails_to(mailpit, REVIEWERS):
                text = text_of(mailpit, message)
                if f"/review/{lead_id}" in text:
                    return text
            return None

        mail = wait_for("the review request mail", review_mail)
        link = must_find(
            r"http://\S+/review/[0-9a-f-]{36}", mail, "the mail links to the review page"
        )
        check("Nothing is sent to the lead until a person approves" in mail, "the mail says so")

        print("== 3. a person approves on the review page")
        page = httpx2.get(link.group(0), timeout=30)
        check(page.status_code == 200, "the linked page opens")
        sha = must_find(
            r'name="draft_sha256" value="([0-9a-f]{64})"', page.text, "the page shows the draft"
        )
        approved = api.post(
            f"/review/{lead_id}/approve",
            data={"draft_sha256": sha.group(1), "reviewer": "e2e-reviewer"},
            follow_redirects=False,
        )
        check(approved.status_code == 303, f"approve form accepted: HTTP {approved.status_code}")

        print("== 4. the customer's mail arrives, once, as approved")

        def sent() -> dict[str, Any] | None:
            view: dict[str, Any] = api.get(f"/v1/leads/{lead_id}").json()
            return view if view["status"] == "sent" else None

        view = wait_for("the lead to be sent", sent, timeout_s=30)
        time.sleep(3)  # a few worker polls: a second copy would show up by now
        mails = mails_to(mailpit, customer)
        check(len(mails) == 1, f"one mail to the customer (found {len(mails)})")
        check(mails[0]["Subject"] == view["draft"]["subject"], "subject as approved")
        check(text_of(mailpit, mails[0]) == view["draft"]["body"], "body as approved")
        check(f"<{mails[0]['MessageID']}>" == view["sent"]["message_id"], "Message-ID recorded")
        requests = [m for m in mails_to(mailpit, REVIEWERS) if lead_id in text_of(mailpit, m)]
        check(len(requests) == 1, f"one review request for the lead (found {len(requests)})")

        print("== 5. the api is down: n8n W3 mails an alert")
        before = {m["ID"] for m in mails_to(mailpit, ALERTS)}
        compose("stop", "api")
        try:
            down = n8n.post(
                "/webhook/lead-form", json={**form, "email": f"e2e-down-{tag}@example.com"}
            )
            check(down.status_code == 500, f"the form gets an error: HTTP {down.status_code}")

            def alert() -> str | None:
                for message in mails_to(mailpit, ALERTS):
                    if message["ID"] not in before and "W1" in message["Subject"]:
                        return f"{message['Subject']}\n{text_of(mailpit, message)}"
                return None

            text = wait_for("the alert mail", alert, timeout_s=30)
            check("Failed node: Create lead" in text, "the alert names the failed step")
            print("   " + text.replace("\n", "\n   "))
        finally:
            compose("up", "-d", "--wait", "api")
        check(api.get("/healthz").status_code == 200, "the api is back")


def main() -> int:
    try:
        run()
    except (Failed, httpx2.HTTPError, subprocess.CalledProcessError) as exc:
        print(f"FAIL {exc}")
        return 1
    print("e2e: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
