"""Structural rules: business logic depends on the LLM interface, never on a provider (U6);
only the state machine changes a lead's status (AC-3.5)."""

import ast
import subprocess
import sys
from pathlib import Path

from sales_ops.anthropic_llm import AnthropicLLM
from sales_ops.fake_llm import HeuristicFakeLLM
from sales_ops.llm import LLMClient
from tests.fakes import ScriptedLLM, output

SRC = Path(__file__).resolve().parents[1] / "src" / "sales_ops"
ADAPTER = "anthropic_llm.py"
FACTORY = "providers.py"


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_only_the_adapter_imports_the_provider_sdk() -> None:
    importers = {
        path.name
        for path in SRC.glob("*.py")
        if any(name == "anthropic" or name.startswith("anthropic.") for name in _imports(path))
    }
    assert importers == {ADAPTER}


def test_only_the_provider_factory_imports_the_adapter() -> None:
    importers = {
        path.name for path in SRC.glob("*.py") if "sales_ops.anthropic_llm" in _imports(path)
    }
    assert importers == {FACTORY}


def test_business_logic_never_mentions_the_provider() -> None:
    business = [path for path in SRC.glob("*.py") if path.name not in {ADAPTER, FACTORY}]
    mentions = [path.name for path in business if "anthropic" in path.read_text().lower()]
    assert business
    assert mentions == []


def test_fake_and_real_providers_implement_the_same_interface() -> None:
    clients = [
        HeuristicFakeLLM(),
        ScriptedLLM(output()),
        AnthropicLLM("m", timeout_s=1, api_key="test-key"),  # constructed only, never called
    ]
    assert all(isinstance(client, LLMClient) for client in clients)


def test_fake_runtime_does_not_load_the_provider_sdk() -> None:
    code = (
        "import sys, sales_ops.worker, sales_ops.providers as p; "
        "p.build_llm_client(p.ProviderSettings(llm_provider='fake')); "
        "print('anthropic' in sys.modules)"
    )
    # Fixed interpreter and constant code: no untrusted input reaches the subprocess.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


def _changes_leads(call: ast.Call) -> bool:
    """update(leads), leads.update(), or an insert into leads that updates on conflict."""
    func = call.func

    def is_leads(node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and node.id == "leads"

    if isinstance(func, ast.Name) and func.id == "update":
        return bool(call.args) and is_leads(call.args[0])
    if isinstance(func, ast.Attribute) and func.attr == "update":
        return is_leads(func.value)
    if isinstance(func, ast.Attribute) and func.attr == "on_conflict_do_update":
        receiver: ast.expr = func.value
        while isinstance(receiver, ast.Call | ast.Attribute):
            if isinstance(receiver, ast.Call):
                if (
                    isinstance(receiver.func, ast.Name)
                    and receiver.func.id in ("insert", "pg_insert")
                    and receiver.args
                    and is_leads(receiver.args[0])
                ):
                    return True
                receiver = receiver.func
            else:
                receiver = receiver.value
    return False


def test_only_the_state_machine_changes_lead_rows() -> None:
    # AC-3.5 holds for the whole app only if every status change goes through transition().
    writers = {
        path.name
        for path in SRC.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call) and _changes_leads(node)
    }
    raw_sql = [path.name for path in SRC.glob("*.py") if "update leads" in path.read_text().lower()]
    assert writers == {"states.py"}
    assert raw_sql == []


def test_the_lead_writer_detector_finds_each_form() -> None:
    forms = [
        "update(leads).values(status='sent')",
        "leads.update().values(status='sent')",
        "pg_insert(leads).values(x=1).on_conflict_do_update(index_elements=['id'], set_={})",
    ]
    for form in forms:
        [statement] = ast.parse(form).body
        assert isinstance(statement, ast.Expr)
        calls = [node for node in ast.walk(statement) if isinstance(node, ast.Call)]
        assert any(_changes_leads(call) for call in calls), form
    [other] = ast.parse("update(jobs).values(status='done')").body
    assert isinstance(other, ast.Expr)
    assert not any(_changes_leads(n) for n in ast.walk(other) if isinstance(n, ast.Call))
