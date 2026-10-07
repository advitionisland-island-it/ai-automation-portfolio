"""Evaluation of lead qualification against tiers a person assigned (AC-5.3, UD-5).

`eval/dataset.csv` holds synthetic inquiries and, in the `tier` column, the tier a person gave
each one. The labels are frozen before the first run against a real model: `eval/FREEZE.json`
records the file's sha256, and a real provider is refused while the file does not match it.
Each response goes through the worker's own pipeline (validation, normalization, the tier
rule), so the evaluation measures what the product would store.
"""

import csv
import hashlib
import json
import math
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from sales_ops.llm import LLMClient, LLMRequest, PermanentLLMError, TransientLLMError
from sales_ops.qualification import Accepted, LeadContext, build_request, evaluate

Tier = Literal["hot", "warm", "cold"]
TIERS: tuple[Tier, ...] = ("hot", "warm", "cold")
INPUT_COLUMNS = ("id", "company", "email_domain", "company_profile", "message")


@dataclass(frozen=True)
class Item:
    id: str
    company: str | None
    email_domain: str
    company_profile: dict[str, str] | None
    message: str
    tier: Tier | None  # the person's label; None until labeled

    def context(self) -> LeadContext:
        return LeadContext(
            company=self.company,
            email_domain=self.email_domain,
            company_profile=self.company_profile,
            messages=[("web_form", self.message)],
        )


@dataclass(frozen=True)
class Outcome:
    item_id: str
    expected: Tier | None
    predicted: Tier | None  # None: no usable answer (see `rejection` or `error`)
    score: int | None
    confidence: float | None
    needs_review: bool
    rejection: str | None
    error: str | None
    attempts: int
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None


def load_dataset(path: Path) -> list[Item]:
    items = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if tuple(reader.fieldnames or ()) != (*INPUT_COLUMNS, "tier"):
            raise ValueError(f"{path}: columns must be {', '.join((*INPUT_COLUMNS, 'tier'))}")
        for row in reader:
            tier = row["tier"].strip().lower()
            if tier and tier not in TIERS:
                raise ValueError(f"{path}: {row['id']}: tier must be hot, warm or cold")
            items.append(
                Item(
                    id=row["id"],
                    company=row["company"] or None,
                    email_domain=row["email_domain"],
                    company_profile=json.loads(row["company_profile"])
                    if row["company_profile"]
                    else None,
                    message=row["message"],
                    tier=cast(Tier, tier) if tier else None,
                )
            )
    if len({item.id for item in items}) != len(items):
        raise ValueError(f"{path}: ids are not unique")
    return items


def load_categories(path: Path) -> dict[str, str]:
    with path.open(newline="", encoding="utf-8") as f:
        return {row["id"]: row["category"] for row in csv.DictReader(f)}


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze_problem(dataset: Path, freeze: Path) -> str | None:
    """Why a real model may not be evaluated on `dataset` yet, or None if it may."""
    if not freeze.exists():
        return f"{freeze.name} is missing: the labels have not been frozen (make eval-freeze)"
    record = json.loads(freeze.read_text())
    if record.get("sha256") != file_sha256(dataset):
        return f"{dataset.name} changed after it was frozen (sha256 differs from {freeze.name})"
    if any(item.tier is None for item in load_dataset(dataset)):
        return f"{dataset.name} has rows without a tier"
    return None


def call_cost_usd(
    input_tokens: int | None,
    output_tokens: int | None,
    price_in: Decimal | None,
    price_out: Decimal | None,
) -> Decimal | None:
    """Cost of one call, or None when it cannot be known (no usage reported, or no price)."""
    if input_tokens is None or output_tokens is None or price_in is None or price_out is None:
        return None
    return (Decimal(input_tokens) * price_in + Decimal(output_tokens) * price_out) / 1_000_000


# What the API adds around the text of a request (message framing, structured-output
# instructions). On the real runs of 2026-09-27 a request used about 340 (Haiku 4.5) to 400
# (Sonnet 5) tokens more than its text, schema included (evidence M5/diagnosis-worst-case-bound).
REQUEST_OVERHEAD_TOKENS = 1000


def prompt_token_bound(request: LLMRequest) -> int:
    """Most input tokens one request can use: one per UTF-8 byte of everything it carries (the
    system prompt, the user message and the answer's JSON schema), plus REQUEST_OVERHEAD_TOKENS.
    A byte-level tokenizer never needs more than one token per byte. Counting a token per 2
    characters of the prompt text fell short on all 60 real calls."""
    schema = json.dumps(request.output_schema, ensure_ascii=False, sort_keys=True)
    carried = request.system + request.user + schema
    return len(carried.encode("utf-8")) + REQUEST_OVERHEAD_TOKENS


def worst_case_cost_usd(
    items: list[Item], max_output_tokens: int, price_in: Decimal, price_out: Decimal
) -> Decimal:
    """An upper bound before anything is sent: every answer uses all its output tokens, and
    every prompt its prompt_token_bound."""
    total = Decimal(0)
    for item in items:
        request = build_request(item.context(), max_output_tokens=max_output_tokens)
        prompt_tokens = prompt_token_bound(request)
        total += Decimal(prompt_tokens) * price_in + Decimal(max_output_tokens) * price_out
    return total / 1_000_000


def prompt_tokens_over_bound(items: list[Item], outcomes: list[Outcome]) -> list[str]:
    """Items whose reported input tokens went over prompt_token_bound. A real report records
    them: if any are listed, the budget check's premise failed on that run."""
    bound = {i.id: prompt_token_bound(build_request(i.context())) for i in items}
    return [
        o.item_id
        for o in outcomes
        if o.input_tokens is not None and o.input_tokens > bound[o.item_id]
    ]


def recorded_bound_check(
    provider: str, items: list[Item], outcomes: list[Outcome]
) -> list[str] | None:
    """What a report records about the bound: nothing for the fake provider, whose token counts
    are made up, else prompt_tokens_over_bound."""
    return None if provider == "fake" else prompt_tokens_over_bound(items, outcomes)


def run(
    items: list[Item],
    llm: LLMClient,
    *,
    max_output_tokens: int,
    low_confidence_threshold: float,
    price_in: Decimal | None,
    price_out: Decimal | None,
    max_attempts: int = 3,
    backoff_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Outcome]:
    """One qualification call per item, with the worker's retry rule for transient errors."""
    return [
        _run_one(
            item,
            llm,
            max_output_tokens,
            low_confidence_threshold,
            price_in,
            price_out,
            max_attempts,
            backoff_s,
            sleep,
        )  # fmt: skip
        for item in items
    ]


def _run_one(
    item: Item,
    llm: LLMClient,
    max_output_tokens: int,
    low_confidence_threshold: float,
    price_in: Decimal | None,
    price_out: Decimal | None,
    max_attempts: int,
    backoff_s: float,
    sleep: Callable[[float], None],
) -> Outcome:
    request = build_request(item.context(), max_output_tokens=max_output_tokens)
    started = time.perf_counter()
    attempts, response, error = 0, None, None
    while attempts < max_attempts:
        attempts += 1
        try:
            response, error = llm.complete(request), None
            break
        except TransientLLMError as exc:
            error = f"transient:{exc.kind}"
            if attempts < max_attempts:
                sleep(backoff_s * 2 ** (attempts - 1))
        except PermanentLLMError as exc:
            error = f"permanent:{exc.kind}"
            break
    blank = Outcome(
        item_id=item.id,
        expected=item.tier,
        predicted=None,
        score=None,
        confidence=None,
        needs_review=False,
        rejection=None,
        error=error,
        attempts=attempts,
        latency_ms=round((time.perf_counter() - started) * 1000),
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
    )
    if response is None:
        return blank
    measured = replace(
        blank,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cost_usd=call_cost_usd(response.input_tokens, response.output_tokens, price_in, price_out),
    )
    verdict = evaluate(response, low_confidence_threshold)
    if not isinstance(verdict, Accepted):
        return replace(measured, rejection=verdict.kind)
    a = verdict.assessment
    return replace(
        measured,
        predicted=a.tier,
        score=a.score,
        confidence=a.confidence,
        needs_review=verdict.needs_review,
    )


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile: the smallest value with at least q% of the values at or below."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize(outcomes: list[Outcome], categories: dict[str, str]) -> dict[str, Any]:
    answered = [o for o in outcomes if o.predicted is not None]
    judged = [o for o in answered if o.expected is not None]
    costs = [o.cost_usd for o in outcomes if o.cost_usd is not None]
    by_category: dict[str, dict[str, int]] = {}
    for o in judged:
        entry = by_category.setdefault(categories.get(o.item_id, "?"), {"agree": 0, "judged": 0})
        entry["judged"] += 1
        entry["agree"] += o.predicted == o.expected
    confusion = Counter((o.expected, o.predicted) for o in judged)
    return {
        "items": len(outcomes),
        "answered": len(answered),
        "labeled": sum(o.expected is not None for o in outcomes),
        "judged": len(judged),
        "agreement": (sum(o.predicted == o.expected for o in judged) / len(judged))
        if judged
        else None,
        "confusion": {f"{e}->{p}": n for (e, p), n in sorted(confusion.items())},
        "by_category": dict(sorted(by_category.items())),
        "needs_review": sum(o.needs_review for o in outcomes),
        "rejections": dict(Counter(o.rejection for o in outcomes if o.rejection)),
        "errors": dict(Counter(o.error for o in outcomes if o.error)),
        "latency_ms_p50": percentile([o.latency_ms for o in outcomes], 50),
        "latency_ms_p95": percentile([o.latency_ms for o in outcomes], 95),
        "cost_known": len(costs),
        "cost_total_usd": str(sum(costs, Decimal(0))) if costs else None,
        "cost_per_lead_usd": str(sum(costs, Decimal(0)) / len(costs)) if costs else None,
    }


def report(meta: dict[str, Any], summary: dict[str, Any], outcomes: list[Outcome]) -> str:
    agreement = summary["agreement"]
    lines = [
        f"# Qualification evaluation — {meta['provider']} / {meta['model']}",
        "",
    ]
    if meta["provider"] == "fake":
        lines += [
            "**Fake provider: this run checks the evaluation harness. It measures no real model.**",
            "",
        ]
    lines += [
        f"- When: {meta['started_at']} (UTC)",
        f"- Dataset: `{meta['dataset']}`, sha256 `{meta['dataset_sha256']}`",
        f"- Labels frozen: {meta['frozen']}",
        f"- Prompt version: {meta['prompt_version']}",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Items | {summary['items']} (labeled {summary['labeled']}, "
        f"answered {summary['answered']}) |",
        f"| Agreement with the person's tier | "
        f"{'not computed (no labeled answers)' if agreement is None else f'{agreement:.1%}'} "
        f"({summary['judged']} judged) |",
        f"| Latency p50 / p95 | {summary['latency_ms_p50']} ms / {summary['latency_ms_p95']} ms |",
        f"| Cost per lead | {_usd(summary['cost_per_lead_usd'])} "
        f"(known for {summary['cost_known']} of {summary['items']}) |",
        f"| Total cost | {_usd(summary['cost_total_usd'])} |",
        f"| Sent to review (low confidence) | {summary['needs_review']} |",
        f"| Unusable answers | {summary['rejections'] or 'none'} |",
        f"| Calls that failed | {summary['errors'] or 'none'} |",
        f"| Prompt tokens over the budget check's bound | "
        f"{_over_bound(meta.get('prompt_tokens_over_bound'))} |",
        "",
        "Agreement by kind of inquiry:",
        "",
        "| Kind | Agree | Judged |",
        "|---|---|---|",
        *(
            f"| {kind} | {v['agree']} | {v['judged']} |"
            for kind, v in summary["by_category"].items()
        ),
        "",
        f"Confusion (person -> model): {summary['confusion'] or 'none'}",
        "",
        "| Id | Person | Model | Score | Confidence | ms | Tokens in/out | Cost |",
        "|---|---|---|---|---|---|---|---|",
        *(
            f"| {o.item_id} | {o.expected or '-'} | {o.predicted or o.rejection or o.error} "
            f"| {o.score if o.score is not None else '-'} "
            f"| {o.confidence if o.confidence is not None else '-'} | {o.latency_ms} "
            f"| {o.input_tokens if o.input_tokens is not None else '-'}/"
            f"{o.output_tokens if o.output_tokens is not None else '-'} "
            f"| {_usd(str(o.cost_usd) if o.cost_usd is not None else None)} |"
            for o in outcomes
        ),
        "",
    ]
    return "\n".join(lines)


def as_json(meta: dict[str, Any], summary: dict[str, Any], outcomes: list[Outcome]) -> str:
    rows = [
        {**asdict(o), "cost_usd": str(o.cost_usd) if o.cost_usd is not None else None}
        for o in outcomes
    ]
    return json.dumps({"meta": meta, "summary": summary, "outcomes": rows}, indent=2) + "\n"


def _usd(value: str | None) -> str:
    return "unknown" if value is None else f"${Decimal(value):.6f}"


def _over_bound(item_ids: list[str] | None) -> str:
    if item_ids is None:
        return "not measured"
    return ", ".join(item_ids) or "none"


def changed_inputs(original: Path, labeled: Path) -> list[str]:
    """Rows whose inputs differ between the unlabeled original and the labeled file. Only the
    tier column may be filled in; the person labels, but does not edit, the inquiries."""

    def inputs(path: Path) -> dict[str, tuple[str, ...]]:
        with path.open(newline="", encoding="utf-8") as f:
            return {row["id"]: tuple(row[c] for c in INPUT_COLUMNS) for row in csv.DictReader(f)}

    before, after = inputs(original), inputs(labeled)
    ids = sorted(set(before) | set(after))
    return [i for i in ids if before.get(i) != after.get(i)]


def real_run_refusal(
    frozen_problem: str | None,
    price_in: Decimal | None,
    price_out: Decimal | None,
    worst_case_usd: Decimal | None,
    budget_usd: Decimal,
) -> str | None:
    """Why a run against a real model must not start, or None. Checked before any client
    exists, so a refused run touches no credential and costs nothing."""
    if frozen_problem:
        return frozen_problem
    if price_in is None or price_out is None or worst_case_usd is None:
        return "set LLM_PRICE_INPUT_USD_PER_MTOK and LLM_PRICE_OUTPUT_USD_PER_MTOK"
    if worst_case_usd > budget_usd:
        return f"worst-case cost ${worst_case_usd:.4f} is over EVAL_BUDGET_USD ${budget_usd}"
    return None


def first_committed_version(dataset: Path) -> tuple[str, str] | None:
    """The commit that first added `dataset`, and the file as it was then: the unlabeled
    original. git reads a pathspec relative to its working directory, so git runs in the
    file's own directory with the bare file name (evidence M5/diagnosis-freeze-pathspec.txt)."""

    def git(*args: str) -> str:
        command = ["git", *args]  # a fixed program; the arguments are built here
        run = subprocess.run(  # noqa: S603
            command, cwd=dataset.parent, check=True, capture_output=True, text=True
        )
        return run.stdout

    commits = git("log", "--diff-filter=A", "--format=%H", "--", dataset.name).split()
    if not commits:
        return None
    first = commits[-1]
    return first, git("show", f"{first}:./{dataset.name}")
