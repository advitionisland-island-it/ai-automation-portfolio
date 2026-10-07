"""AC-4.1 (U7): after `make up`, n8n itself shows that W1-W3 were imported and are active.

Three independent views, none of which needs a login or an API key:
1. n8n's CLI in the container: every workflow, whether it is active, and whether the
   published version is the current one.
2. n8n's /metrics: the number of active workflows.
3. W1's webhook: a GET is refused as "registered for POST", which only a published workflow
   produces. (A GET, so no lead is created.)
"""

import json
import os
import subprocess
import sys
import time

import httpx2

N8N = f"http://127.0.0.1:{os.environ.get('N8N_PORT', '5678')}"
EXPECTED = {"P01W1InboundLead", "P01W2ReviewMail0", "P01W3AlertMail00"}


EXPORT = ["docker", "compose", "exec", "-T", "n8n", "n8n", "export:workflow", "--all"]


def workflows() -> list[dict[str, object]]:
    # A fixed command: nothing from outside reaches it.
    run = subprocess.run(EXPORT, check=True, capture_output=True, text=True)  # noqa: S603
    listed: list[dict[str, object]] = json.loads(run.stdout[run.stdout.index("[") :])
    return listed


def main() -> int:
    failures = []
    print("1. n8n CLI: n8n export:workflow --all")
    seen = set()
    for workflow in workflows():
        published = workflow["activeVersionId"] == workflow["versionId"]
        seen.add(workflow["id"])
        print(
            f"   {workflow['id']} | {workflow['name']} | active: {workflow['active']}"
            f" | published = current: {published}"
        )
        if workflow["id"] in EXPECTED and not (workflow["active"] and published):
            failures.append(f"{workflow['id']} is not active with its current version")
    if missing := EXPECTED - seen:
        failures.append(f"not imported: {sorted(missing)}")

    print("2. n8n HTTP: GET /metrics")
    metrics = httpx2.get(f"{N8N}/metrics", timeout=10).text
    [active] = [
        line for line in metrics.splitlines() if line.startswith("n8n_active_workflow_count")
    ]
    print(f"   {active}")
    if int(active.split()[-1]) < len(EXPECTED):
        failures.append(f"fewer than {len(EXPECTED)} active workflows")

    print("3. n8n HTTP: GET /webhook/lead-form (W1 listens for POST)")
    # n8n registers webhooks a few seconds after it reports healthy: wait up to 15 s for that.
    for _ in range(15):
        reply = httpx2.get(f"{N8N}/webhook/lead-form", timeout=10)
        if "Did you mean to make a POST request?" in reply.text:
            break
        time.sleep(1)
    print(f"   HTTP {reply.status_code} {reply.text}")
    if "Did you mean to make a POST request?" not in reply.text:
        failures.append("W1's webhook is not registered")

    for failure in failures:
        print(f"FAIL {failure}")
    print("n8n-status: all checks passed" if not failures else "n8n-status: failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
