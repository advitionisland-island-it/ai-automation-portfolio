"""The evaluation harness (AC-5.3). No model is called: scripted responses stand in."""

import csv
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from sales_ops import evaluation
from sales_ops.evaluation import Item, Outcome
from sales_ops.llm import LLMResponse, PermanentLLMError, TransientLLMError
from sales_ops.qualification import build_request
from tests.fakes import ScriptedLLM, output, raw
from tests.helpers import PROJECT_ROOT

DATASET = PROJECT_ROOT / "eval" / "dataset.csv"
DESIGN = PROJECT_ROOT / "eval" / "design.csv"


def item(item_id: str = "X1", tier: str | None = "hot") -> Item:
    return Item(item_id, "Acme KK", "acme.example", None, "We want to buy.", tier)  # type: ignore[arg-type]


def outcome(item_id: str, expected: str | None, predicted: str | None, **extra: object) -> Outcome:
    fields: dict[str, object] = {
        "item_id": item_id,
        "expected": expected,
        "predicted": predicted,
        "score": None,
        "confidence": None,
        "needs_review": False,
        "rejection": None,
        "error": None,
        "attempts": 1,
        "latency_ms": 100,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        **extra,
    }
    return Outcome(**fields)  # type: ignore[arg-type]


# The dataset ----------------------------------------------------------------------------------


def test_the_dataset_is_thirty_synthetic_inquiries_six_of_each_kind() -> None:
    items = evaluation.load_dataset(DATASET)
    kinds = evaluation.load_categories(DESIGN)

    assert len(items) == 30
    assert sorted(kinds) == sorted(i.id for i in items)
    assert sorted(list(kinds.values()).count(k) for k in set(kinds.values())) == [6] * 5
    # Synthetic: reserved example domains only, never a real company's.
    assert all(
        i.email_domain == "example.com" or i.email_domain.endswith(".example") for i in items
    )


def test_the_labeling_file_does_not_show_the_intended_kind() -> None:
    header = DATASET.read_text(encoding="utf-8").splitlines()[0]
    assert "category" not in header  # the kinds live in design.csv, so they cannot anchor a label


def test_an_unknown_tier_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "dataset.csv"
    bad.write_text(
        "id,company,email_domain,company_profile,message,tier\nX1,A,a.example,,hi,maybe\n"
    )
    with pytest.raises(ValueError, match="hot, warm or cold"):
        evaluation.load_dataset(bad)


# Freezing the labels ---------------------------------------------------------------------------


def labeled_copy(tmp_path: Path, tier: str = "warm") -> Path:
    rows = list(csv.DictReader(DATASET.open(encoding="utf-8")))
    path = tmp_path / "dataset.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "tier": tier})
    return path


def test_a_real_run_needs_labels_frozen_as_they_are(tmp_path: Path) -> None:
    labeled = labeled_copy(tmp_path)
    freeze = tmp_path / "FREEZE.json"

    assert "missing" in (evaluation.freeze_problem(labeled, freeze) or "")
    freeze.write_text(json.dumps({"sha256": evaluation.file_sha256(labeled)}))
    assert evaluation.freeze_problem(labeled, freeze) is None
    labeled.write_text(labeled.read_text().replace(",warm\n", ",hot\n", 1))  # a label changed
    assert "changed after it was frozen" in (evaluation.freeze_problem(labeled, freeze) or "")


def test_a_frozen_file_with_missing_tiers_is_still_refused(tmp_path: Path) -> None:
    unlabeled = labeled_copy(tmp_path, tier="")
    freeze = tmp_path / "FREEZE.json"
    freeze.write_text(json.dumps({"sha256": evaluation.file_sha256(unlabeled)}))
    assert "without a tier" in (evaluation.freeze_problem(unlabeled, freeze) or "")


def test_labeling_may_fill_in_tiers_but_not_edit_inquiries(tmp_path: Path) -> None:
    labeled = labeled_copy(tmp_path)
    assert evaluation.changed_inputs(DATASET, labeled) == []
    edited = tmp_path / "edited.csv"
    edited.write_text(labeled.read_text(encoding="utf-8").replace("price?", "price??"), "utf-8")
    [changed] = evaluation.changed_inputs(DATASET, edited)
    assert changed.startswith("E")


PRICED = (Decimal(2), Decimal(10))


def git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - fixed program, arguments from this test
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com",  # noqa: S607
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True,
    )  # fmt: skip


def test_the_unlabeled_original_is_found_from_a_nested_project(tmp_path: Path) -> None:
    # The layout that broke make eval-freeze: the project sits below the repository root, and
    # git reads a pathspec relative to where it runs (evidence M5/diagnosis-freeze-pathspec.txt).
    dataset = tmp_path / "projects" / "p01" / "eval" / "dataset.csv"
    dataset.parent.mkdir(parents=True)
    dataset.write_text("id,tier\nE01,\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("repository with history\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-q", "-m", "history")
    assert evaluation.first_committed_version(dataset) is None  # the dataset is not committed yet
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "unlabeled")
    dataset.write_text("id,tier\nE01,hot\n", encoding="utf-8")  # labeled, not committed

    found = evaluation.first_committed_version(dataset)

    assert found is not None
    commit, content = found
    assert len(commit) == 40
    assert content == "id,tier\nE01,\n"


@pytest.mark.parametrize(
    ("frozen_problem", "prices", "worst", "budget", "refused"),
    [
        pytest.param("not frozen", PRICED, Decimal("0.5"), Decimal(5), True, id="unfrozen"),
        pytest.param(None, (None, Decimal(10)), None, Decimal(5), True, id="no-price"),
        pytest.param(None, PRICED, Decimal("5.01"), Decimal(5), True, id="over-budget"),
        pytest.param(None, PRICED, Decimal("0.5"), Decimal(0), True, id="default-budget"),
        pytest.param(None, PRICED, Decimal("0.5"), Decimal(5), False, id="allowed"),
    ],
)
def test_a_real_run_is_refused_unless_frozen_priced_and_within_budget(
    frozen_problem: str | None,
    prices: tuple[Decimal | None, Decimal | None],
    worst: Decimal | None,
    budget: Decimal,
    refused: bool,
) -> None:
    reason = evaluation.real_run_refusal(frozen_problem, *prices, worst, budget)
    assert (reason is not None) == refused


def test_the_command_refuses_a_real_model_before_touching_any_credential() -> None:
    # Whatever the labels' state, a budget of 0 refuses: this test can never reach a real API.
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("LLM_", "EVAL_"))},
        "LLM_PROVIDER": "anthropic",
        "LLM_MODEL": "claude-sonnet-5",
        "LLM_PRICE_INPUT_USD_PER_MTOK": "2",
        "LLM_PRICE_OUTPUT_USD_PER_MTOK": "10",
        "EVAL_BUDGET_USD": "0",
        "ANTHROPIC_API_KEY": "",
        # And if the refusal ever broke, the SDK would talk to a closed local port, not Anthropic.
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
    }
    run = subprocess.run(  # noqa: S603 - fixed interpreter and script
        [sys.executable, str(PROJECT_ROOT / "scripts" / "eval.py")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 2
    assert run.stdout.startswith("refused:")


def test_the_worst_case_counts_every_answer_at_its_output_limit() -> None:
    items = evaluation.load_dataset(DATASET)
    worst = evaluation.worst_case_cost_usd(items, 4096, Decimal(2), Decimal(10))
    prompts = sum(evaluation.prompt_token_bound(build_request(i.context())) for i in items)
    # 30 answers of 4096 output tokens at $10 per million is $1.2288; the prompts add the rest.
    assert worst == Decimal("1.2288") + Decimal(prompts) * 2 / 1_000_000


def test_the_prompt_bound_covers_every_token_count_measured_on_the_real_api() -> None:
    # The budget check is only as good as this bound. The real runs (U11) report the input
    # tokens of every call, and each must fit: the first bound, a token per 2 characters of the
    # prompt text, fell short on all 60 (evidence M5/diagnosis-worst-case-bound.txt).
    items = {i.id: i for i in evaluation.load_dataset(DATASET)}
    digest = evaluation.file_sha256(DATASET)
    measured: list[tuple[str, int]] = []
    for path in sorted((PROJECT_ROOT / "eval" / "reports").glob("*-anthropic-*.json")):
        report = json.loads(path.read_text())
        if report["meta"]["dataset_sha256"] == digest:
            measured += [
                (o["item_id"], o["input_tokens"])
                for o in report["outcomes"]
                if o["input_tokens"] is not None
            ]
    assert len(measured) >= 60  # Sonnet 5 and Haiku 4.5, 30 calls each
    over = [
        (item_id, tokens)
        for item_id, tokens in measured
        if tokens > evaluation.prompt_token_bound(build_request(items[item_id].context()))
    ]
    assert over == []


def test_the_items_over_the_prompt_bound_are_listed() -> None:
    bound = evaluation.prompt_token_bound(build_request(item("A").context()))
    outcomes = [
        outcome("A", "hot", "hot", input_tokens=bound + 1),
        outcome("B", "hot", "hot", input_tokens=bound),
        outcome("C", "hot", None),  # the call reported no usage
    ]
    assert evaluation.prompt_tokens_over_bound([item("A"), item("B"), item("C")], outcomes) == ["A"]


def test_a_fake_run_records_no_bound_check() -> None:
    # The fake provider's token counts are made up: they say nothing about the real bound.
    outcomes = [outcome("A", "hot", "hot", input_tokens=10**9)]
    assert evaluation.recorded_bound_check("fake", [item("A")], outcomes) is None
    assert evaluation.recorded_bound_check("anthropic", [item("A")], outcomes) == ["A"]


@pytest.mark.parametrize(
    ("over", "shown"), [(None, "not measured"), ([], "none"), (["E01", "E07"], "E01, E07")]
)
def test_the_report_shows_the_items_over_the_prompt_bound(
    over: list[str] | None, shown: str
) -> None:
    meta = {
        "provider": "anthropic",
        "model": "claude-test",
        "started_at": "2026-09-27T00:00:00+00:00",
        "dataset": "eval/dataset.csv",
        "dataset_sha256": "0" * 64,
        "frozen": True,
        "prompt_version": "qualify-v1",
        "prompt_tokens_over_bound": over,
    }
    text = evaluation.report(meta, evaluation.summarize([], {}), [])
    assert f"| Prompt tokens over the budget check's bound | {shown} |" in text


# Running and scoring ---------------------------------------------------------------------------


def test_a_transient_error_is_retried_and_a_permanent_one_is_not() -> None:
    naps: list[float] = []
    llm = ScriptedLLM(TransientLLMError("rate_limit"), output(score=80), PermanentLLMError("auth"))

    first, second = evaluation.run(
        [item("A"), item("B")],
        llm,
        max_output_tokens=512,
        low_confidence_threshold=0.5,
        price_in=Decimal(2),
        price_out=Decimal(10),
        sleep=naps.append,
    )

    assert (first.predicted, first.attempts, first.error) == ("hot", 2, None)
    assert first.cost_usd == Decimal("0.00169")  # (420 * 2 + 85 * 10) / 1,000,000
    assert naps == [2.0]
    assert (second.predicted, second.attempts, second.error) == (None, 1, "permanent:auth")
    assert second.cost_usd is None  # no response, no usage: unknown, not free


def test_an_answer_without_usage_has_an_unknown_cost_not_zero() -> None:
    no_usage = LLMResponse(
        text=output().text, stop_reason="end", input_tokens=None, output_tokens=None
    )
    [result] = evaluation.run(
        [item()],
        ScriptedLLM(no_usage),
        max_output_tokens=512,
        low_confidence_threshold=0.5,
        price_in=Decimal(2),
        price_out=Decimal(10),
    )
    assert (result.predicted, result.cost_usd) == ("hot", None)  # a score of 82, cost unknown


def test_an_unusable_answer_counts_as_no_answer() -> None:
    [result] = evaluation.run(
        [item()],
        ScriptedLLM(raw("not json")),
        max_output_tokens=512,
        low_confidence_threshold=0.5,
        price_in=None,
        price_out=None,
    )
    assert (result.predicted, result.rejection, result.cost_usd) == (None, "invalid_json", None)


def test_agreement_counts_only_labeled_items_that_got_an_answer() -> None:
    outcomes = [
        outcome("A", "hot", "hot", cost_usd=Decimal("0.002")),
        outcome("B", "warm", "cold", cost_usd=Decimal("0.004"), latency_ms=300),
        outcome("C", "cold", None, rejection="refusal"),
        outcome("D", None, "hot", latency_ms=900),
    ]

    summary = evaluation.summarize(outcomes, {"A": "CLEAR GOOD", "B": "BORDERLINE"})

    assert (summary["judged"], summary["agreement"]) == (2, 0.5)
    assert summary["confusion"] == {"hot->hot": 1, "warm->cold": 1}
    assert summary["by_category"] == {
        "BORDERLINE": {"agree": 0, "judged": 1},
        "CLEAR GOOD": {"agree": 1, "judged": 1},
    }
    assert (summary["cost_known"], summary["cost_per_lead_usd"]) == (2, "0.003")
    assert (summary["latency_ms_p50"], summary["latency_ms_p95"]) == (100, 900)
    assert summary["rejections"] == {"refusal": 1}


@pytest.mark.parametrize(
    ("values", "q", "expected"),
    [([], 50, None), ([7], 95, 7), (list(range(1, 11)), 50, 5), (list(range(1, 11)), 95, 10)],
)
def test_percentile_is_nearest_rank(values: list[float], q: float, expected: float | None) -> None:
    assert evaluation.percentile(values, q) == expected


def test_a_fake_report_says_it_measures_no_model() -> None:
    meta = {
        "provider": "fake",
        "model": "fake-heuristic-v1",
        "started_at": "2026-09-27T00:00:00+00:00",
        "dataset": "eval/dataset.csv",
        "dataset_sha256": "0" * 64,
        "frozen": False,
        "prompt_version": "qualify-v1",
    }
    outcomes = [outcome("A", None, "hot")]
    text = evaluation.report(meta, evaluation.summarize(outcomes, {}), outcomes)
    assert "It measures no real model." in text
    assert "not computed" in text
