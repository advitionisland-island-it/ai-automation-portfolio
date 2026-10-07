"""`make eval-freeze` (AC-5.3): fix the labels before the first run against a real model.

1. Every row has a tier: hot, warm or cold.
2. Only the tier column changed since the dataset was committed without labels: the person
   labels the inquiries, and does not edit them.
3. eval/FREEZE.json records the labeled file's sha256.

Commit both files. That commit and the hash are the record that the labels existed before any
model was evaluated on them. After that, `make eval` refuses a real model if the file changes.
"""

import json
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from sales_ops import evaluation

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval" / "dataset.csv"
FREEZE = ROOT / "eval" / "FREEZE.json"


def main() -> int:
    if FREEZE.exists():
        problem = evaluation.freeze_problem(DATASET, FREEZE)
        print("already frozen" if problem is None else f"refused: {problem}")
        return 0 if problem is None else 2

    items = evaluation.load_dataset(DATASET)
    unlabeled = [item.id for item in items if item.tier is None]
    if unlabeled:
        print(f"refused: no tier yet for {', '.join(unlabeled)}")
        return 2

    # The first commit that added the dataset holds the inquiries as generated, without labels.
    original = evaluation.first_committed_version(DATASET)
    if original is None:
        print("refused: eval/dataset.csv was never committed; commit it without labels first")
        return 2
    first, content = original
    with tempfile.TemporaryDirectory() as scratch:
        original_file = Path(scratch) / "dataset.csv"
        original_file.write_text(content, encoding="utf-8")
        changed = evaluation.changed_inputs(original_file, DATASET)
    if changed:
        print(f"refused: inputs differ from commit {first[:7]} in {', '.join(changed)}")
        return 2

    record = {
        "dataset": "eval/dataset.csv",
        "sha256": evaluation.file_sha256(DATASET),
        "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rows": len(items),
        "tiers": dict(sorted(Counter(item.tier for item in items if item.tier).items())),
        "unlabeled_source_commit": first,
    }
    FREEZE.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    print("frozen: commit eval/dataset.csv and eval/FREEZE.json before the first real run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
