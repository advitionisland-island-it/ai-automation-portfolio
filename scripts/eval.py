"""`make eval` (AC-5.3): qualify every inquiry in eval/dataset.csv and compare with the labels.

Provider and model come from LLM_PROVIDER / LLM_MODEL, as for the worker. The default is the
fake provider, which checks the harness and measures no model. A real provider runs only if:

- the labels are frozen (eval/FREEZE.json matches eval/dataset.csv; see `make eval-freeze`),
- prices are set (LLM_PRICE_INPUT_USD_PER_MTOK, LLM_PRICE_OUTPUT_USD_PER_MTOK), and
- the worst-case cost of the whole run fits EVAL_BUDGET_USD (default 0: nothing is sent).

Reports are written to eval/reports/ as Markdown and JSON.
"""

import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sales_ops import evaluation
from sales_ops.providers import build_llm_client
from sales_ops.qualification import PROMPT_VERSION
from sales_ops.worker import WorkerSettings

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval" / "dataset.csv"
DESIGN = ROOT / "eval" / "design.csv"
FREEZE = ROOT / "eval" / "FREEZE.json"
REPORTS = ROOT / "eval" / "reports"


def main() -> int:
    os.environ.setdefault("LLM_PROVIDER", "fake")  # a real model has to be asked for by name
    settings = WorkerSettings()  # type: ignore[call-arg]  # values come from the environment
    items = evaluation.load_dataset(DATASET)
    frozen_problem = evaluation.freeze_problem(DATASET, FREEZE)
    budget = Decimal(os.environ.get("EVAL_BUDGET_USD", "0"))
    price_in, price_out = (
        settings.llm_price_input_usd_per_mtok,
        settings.llm_price_output_usd_per_mtok,
    )
    worst_case = None

    if settings.llm_provider != "fake":
        if price_in is not None and price_out is not None:
            worst_case = evaluation.worst_case_cost_usd(
                items, settings.llm_max_output_tokens, price_in, price_out
            )
        refusal = evaluation.real_run_refusal(
            frozen_problem, price_in, price_out, worst_case, budget
        )
        if refusal:
            print(f"refused: {refusal}")
            return 2

    llm = build_llm_client(settings)
    started = datetime.now(UTC)
    outcomes = evaluation.run(
        items,
        llm,
        max_output_tokens=settings.llm_max_output_tokens,
        low_confidence_threshold=settings.qualify_low_confidence_threshold,
        price_in=price_in,
        price_out=price_out,
    )
    meta = {
        "provider": llm.provider,
        "model": llm.model,
        "prompt_version": PROMPT_VERSION,
        "started_at": started.isoformat(timespec="seconds"),
        "dataset": str(DATASET.relative_to(ROOT)),
        "dataset_sha256": evaluation.file_sha256(DATASET),
        "frozen": frozen_problem is None,
        "budget_usd": str(budget),
        "worst_case_cost_usd": str(worst_case) if worst_case is not None else None,
        "prompt_tokens_over_bound": evaluation.recorded_bound_check(llm.provider, items, outcomes),
    }
    summary = evaluation.summarize(outcomes, evaluation.load_categories(DESIGN))
    REPORTS.mkdir(exist_ok=True)
    stem = f"{started:%Y%m%dT%H%M%SZ}-{llm.provider}-{llm.model}".replace("/", "_")
    markdown = evaluation.report(meta, summary, outcomes)
    (REPORTS / f"{stem}.md").write_text(markdown)
    (REPORTS / f"{stem}.json").write_text(evaluation.as_json(meta, summary, outcomes))
    print("\n".join(markdown.splitlines()[:22]))
    print(f"\nreport: eval/reports/{stem}.md (and .json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
