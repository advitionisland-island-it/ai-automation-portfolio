"""The n8n workflows in ./n8n against the API they call (M4). No n8n needed: these read the JSON.

The e2e run (`make e2e`) exercises the workflows for real; these tests catch drift early: a
workflow calling a route that does not exist, a key and a body built from different values,
an error workflow that is not published.
"""

import json
import re
from typing import Any

import pytest
from fastapi.routing import APIRoute

from sales_ops.main import app
from tests.helpers import PROJECT_ROOT

N8N = PROJECT_ROOT / "n8n"
WORKFLOWS = {path.name: json.loads(path.read_text()) for path in (N8N / "workflows").glob("*.json")}
[CREDENTIAL] = json.loads((N8N / "credentials" / "mailpit-smtp.json").read_text())
START = (N8N / "start.sh").read_text()
W3 = "P01W3AlertMail00"


def nodes(of_type: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (name, node)
        for name, workflow in WORKFLOWS.items()
        for node in workflow["nodes"]
        if node["type"] == f"n8n-nodes-base.{of_type}"
    ]


def api_routes() -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }


def test_there_are_three_workflows_and_all_are_published() -> None:
    ids = {workflow["id"] for workflow in WORKFLOWS.values()}
    assert ids == {"P01W1InboundLead", "P01W2ReviewMail0", W3}
    published = set(re.findall(r"for id in ([^;]+);", START)[0].split())
    assert published == ids  # an unpublished error workflow never runs (evidence M4/n8n-spike)


def test_failures_go_to_the_alert_workflow() -> None:
    for workflow in WORKFLOWS.values():
        expected = None if workflow["id"] == W3 else W3
        assert workflow["settings"].get("errorWorkflow") == expected, workflow["name"]


def test_successful_runs_keep_no_data() -> None:
    # Polling every 10 s would otherwise keep thousands of runs a day. n8n still soft-deletes
    # each run and removes it after an hour (evidence M4/n8n-execution-storage.txt).
    for workflow in WORKFLOWS.values():
        assert workflow["settings"]["saveDataSuccessExecution"] == "none", workflow["name"]


def test_every_api_call_targets_a_route_that_exists() -> None:
    routes = api_routes()
    calls = nodes("httpRequest")
    assert calls
    for name, node in calls:
        url = node["parameters"]["url"]
        # "={{ 'http://api:8000/v1/alerts/' + $('Claim alerts').item.json.id + '/delivered' }}"
        path = re.sub(r"'\s*\+\s*\$\(.+?\)\.item\.json\.id\s*\+\s*'", "{id}", url)
        path = re.sub(r"^=\{\{\s*'|'\s*\}\}$", "", path)
        path = path.removeprefix("http://api:8000").split("?")[0]
        method = node["parameters"]["method"]
        pattern = re.sub(r"\{[^}]+\}", "{}", path)
        known = {(m, re.sub(r"\{[^}]+\}", "{}", p)) for m, p in routes}
        assert (method, pattern) in known, f"{name}: {method} {path}"


def test_the_idempotency_key_is_the_hash_of_exactly_the_body_that_is_sent() -> None:
    [(_, create)] = [(n, node) for n, node in nodes("httpRequest") if node["name"] == "Create lead"]
    parameters = create["parameters"]
    [header] = parameters["headerParameters"]["parameters"]
    body = re.fullmatch(r"=\{\{ JSON\.stringify\((.+)\) \}\}", parameters["jsonBody"])
    key = re.fullmatch(
        r"=\{\{ 'form-' \+ JSON\.stringify\((.+)\)\.hash\('sha256'\) \}\}", header["value"]
    )
    assert header["name"] == "Idempotency-Key"
    assert body is not None and key is not None
    # Built from different values, a resubmitted form would get 409 instead of its lead back.
    assert key.group(1) == body.group(1)


def test_polling_skips_an_unreachable_api_instead_of_raising_an_alert_every_tick() -> None:
    for workflow in WORKFLOWS.values():
        for node in workflow["nodes"]:
            if node["name"].startswith("Claim "):
                assert node["onError"] == "continueErrorOutput", node["name"]
                success, error = workflow["connections"][node["name"]]["main"]
                assert success and error == [], node["name"]


@pytest.mark.parametrize(("workflow", "node"), nodes("emailSend"))
def test_mail_goes_to_mailpit_without_a_login(workflow: str, node: dict[str, Any]) -> None:
    assert node["credentials"]["smtp"]["id"] == CREDENTIAL["id"]
    assert node["parameters"]["toEmail"].endswith("@sales-ops.example")
    assert node["parameters"]["options"]["appendAttribution"] is False


def test_the_smtp_credential_holds_no_secret() -> None:
    data = CREDENTIAL["data"]
    assert (data["host"], data["port"], data["user"], data["password"]) == ("mailpit", 1025, "", "")


def test_the_form_webhook_path_matches_the_e2e_script() -> None:
    [(_, webhook)] = nodes("webhook")
    path = webhook["parameters"]["path"]
    assert f"/webhook/{path}" in (PROJECT_ROOT / "scripts" / "e2e.py").read_text()
    assert webhook["parameters"]["responseMode"] == "responseNode"


def test_workflow_files_are_named_after_their_workflows() -> None:
    for filename, workflow in WORKFLOWS.items():
        assert filename.startswith(workflow["name"].split()[0].lower()), filename


def test_the_start_script_imports_before_n8n_starts() -> None:
    lines = [
        line.strip() for line in START.splitlines() if line.strip().startswith(("n8n", "exec"))
    ]
    assert lines[-1] == "exec n8n start"
    assert all("import:" in line or "publish:" in line for line in lines[:-1])


def test_the_status_check_expects_exactly_these_workflows() -> None:
    # `make n8n-status` (AC-4.1, U7) must look for the workflows this repository ships.
    status = (PROJECT_ROOT / "scripts" / "n8n_status.py").read_text()
    [expected] = re.findall(r"EXPECTED = (\{[^}]+\})", status)
    assert set(re.findall(r'"([^"]+)"', expected)) == {w["id"] for w in WORKFLOWS.values()}
