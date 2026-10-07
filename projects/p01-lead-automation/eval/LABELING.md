# Labeling the evaluation set

The evaluation compares the model's tier for each inquiry with the tier **a person** gave it.
The person's labels are the ground truth, so they are written before any model is evaluated
and frozen with a hash. A model never writes them: scoring a model against labels it produced
only measures agreement with itself.

## The file

`eval/dataset.csv` has 30 synthetic inquiries. The companies are invented and the domains are
reserved example domains. Each row shows what the model will see:

| Column | Meaning |
|---|---|
| `id` | E01 to E30 |
| `company` | the company name as the sender typed it (may be empty) |
| `email_domain` | the sender's email domain |
| `company_profile` | synthetic enrichment data for that domain, or empty when unknown |
| `message` | the inquiry |
| `tier` | **empty: this is the column to fill in** |

## The tiers

Judge from the row alone, as if you were the sales lead reading it: how likely is this to
become a paying customer of a lead-automation product?

| Tier | Meaning |
|---|---|
| `hot` | Likely to buy soon. A clear need that fits the product, and signs of buying: budget, a timeline, a decision maker, procurement. |
| `warm` | A real possibility, but something important is missing or far away: no budget yet, an unclear timeline, a doubtful fit, someone who cannot decide, research only. |
| `cold` | Unlikely to buy: spam, job applications, vendors selling to us, students, the wrong product, no usable information, or a request that cannot be served. |

There is no answer to match. Do not try to guess what a model would say; if a row sits between
two tiers, pick the one you would act on.

## How to fill it in

1. Open `eval/dataset.csv` in a text editor or a spreadsheet.
2. Write `hot`, `warm` or `cold` (lowercase) in the `tier` column of every row.
3. Change nothing else. `make eval-freeze` refuses the file if any other cell changed.
4. Save it as CSV in UTF-8, with the same header. If a spreadsheet is used, export as
   "CSV UTF-8".
5. Run `make eval-freeze`, then commit `eval/dataset.csv` and `eval/FREEZE.json`. From then on,
   `make eval` refuses a real model if the file differs from the frozen hash.

`eval/design.csv` records which kind of inquiry each row was written as (CLEAR GOOD,
CLEAR BAD, BORDERLINE, MISSING DATA, CONFLICTING DATA, six of each). Look at it after labeling,
not before, so it does not steer the labels.

---

## 日本語の要約

- `eval/dataset.csv` の `tier` 列に、30 行すべて `hot`・`warm`・`cold`（小文字）を記入してください。ほかのセルは変えないでください。
- 基準: `hot` は近く買いそう（必要性と、予算・時期・決裁者などの買う兆しがある）。`warm` は可能性はあるが大事な点が欠けている・遠い。`cold` は買いそうにない（スパム、求職、売り込み、学生、見当違い、情報が無いなど）。
- 迷ったら、自分なら実際にどう扱うかで選んでください。モデルの答えを予想する必要はありません。
- `eval/design.csv` には各行を作った意図（5 種類）があります。先入観を避けるため、記入の後で見てください。
- 保存は UTF-8 の CSV で。記入が終わったら「続けて」と伝えてください。固定（`make eval-freeze`）とコミットは Claude が行います。
