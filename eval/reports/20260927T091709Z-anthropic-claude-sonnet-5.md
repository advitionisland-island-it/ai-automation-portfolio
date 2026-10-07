# Qualification evaluation — anthropic / claude-sonnet-5

- When: 2026-09-27T09:17:09+00:00 (UTC)
- Dataset: `eval/dataset.csv`, sha256 `9ba63296718bd80d309778609d4387b747019be910a1855aceb12bae07a7277f`
- Labels frozen: True
- Prompt version: qualify-v1

| Measure | Value |
|---|---|
| Items | 30 (labeled 30, answered 30) |
| Agreement with the person's tier | 40.0% (30 judged) |
| Latency p50 / p95 | 4403 ms / 8283 ms |
| Cost per lead | $0.004455 (known for 30 of 30) |
| Total cost | $0.133650 |
| Sent to review (low confidence) | 4 |
| Unusable answers | none |
| Calls that failed | none |

Agreement by kind of inquiry:

| Kind | Agree | Judged |
|---|---|---|
| BORDERLINE | 1 | 6 |
| CLEAR BAD | 3 | 6 |
| CLEAR GOOD | 5 | 6 |
| CONFLICTING DATA | 0 | 6 |
| MISSING DATA | 3 | 6 |

Confusion (person -> model): {'cold->cold': 6, 'hot->cold': 8, 'hot->hot': 5, 'warm->cold': 9, 'warm->hot': 1, 'warm->warm': 1}

| Id | Person | Model | Score | Confidence | ms | Tokens in/out | Cost |
|---|---|---|---|---|---|---|---|
| E01 | warm | cold | 5 | 0.85 | 5334 | 646/221 | $0.003502 |
| E02 | warm | cold | 15 | 0.3 | 3974 | 648/224 | $0.003536 |
| E03 | hot | cold | 5 | 0.85 | 4462 | 707/307 | $0.004484 |
| E04 | hot | cold | 5 | 0.75 | 4339 | 713/295 | $0.004376 |
| E05 | warm | cold | 2 | 0.95 | 3888 | 686/181 | $0.003182 |
| E06 | hot | hot | 90 | 0.75 | 3714 | 736/224 | $0.003712 |
| E07 | warm | cold | 35 | 0.5 | 4403 | 695/259 | $0.003980 |
| E08 | cold | cold | 15 | 0.6 | 2573 | 656/146 | $0.002772 |
| E09 | cold | cold | 5 | 0.6 | 4020 | 640/218 | $0.003460 |
| E10 | warm | cold | 3 | 0.85 | 3275 | 684/198 | $0.003348 |
| E11 | hot | cold | 15 | 0.7 | 4099 | 714/273 | $0.004158 |
| E12 | warm | cold | 38 | 0.4 | 3697 | 680/209 | $0.003450 |
| E13 | hot | hot | 90 | 0.75 | 4090 | 750/253 | $0.004030 |
| E14 | cold | cold | 8 | 0.6 | 5614 | 650/297 | $0.004270 |
| E15 | hot | hot | 93 | 0.85 | 4919 | 722/319 | $0.004634 |
| E16 | hot | cold | 22 | 0.6 | 4473 | 723/299 | $0.004436 |
| E17 | warm | hot | 88 | 0.6 | 5501 | 725/424 | $0.005690 |
| E18 | warm | cold | 12 | 0.3 | 4994 | 671/335 | $0.004692 |
| E19 | cold | cold | 2 | 0.95 | 2532 | 668/129 | $0.002626 |
| E20 | warm | cold | 15 | 0.4 | 4737 | 714/300 | $0.004428 |
| E21 | cold | cold | 1 | 0.97 | 4073 | 666/256 | $0.003892 |
| E22 | hot | cold | 28 | 0.5 | 6884 | 711/510 | $0.006522 |
| E23 | hot | cold | 5 | 0.75 | 4557 | 698/294 | $0.004336 |
| E24 | hot | cold | 8 | 0.6 | 5731 | 714/441 | $0.005838 |
| E25 | hot | hot | 82 | 0.6 | 8283 | 735/686 | $0.008330 |
| E26 | hot | hot | 90 | 0.75 | 8202 | 749/644 | $0.007938 |
| E27 | warm | cold | 2 | 0.95 | 3308 | 677/193 | $0.003284 |
| E28 | warm | warm | 62 | 0.5 | 4978 | 707/324 | $0.004654 |
| E29 | cold | cold | 5 | 0.85 | 3001 | 680/145 | $0.002810 |
| E30 | hot | cold | 22 | 0.5 | 8358 | 720/584 | $0.007280 |
