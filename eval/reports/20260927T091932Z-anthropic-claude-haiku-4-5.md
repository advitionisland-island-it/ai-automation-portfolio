# Qualification evaluation — anthropic / claude-haiku-4-5

- When: 2026-09-27T09:19:32+00:00 (UTC)
- Dataset: `eval/dataset.csv`, sha256 `9ba63296718bd80d309778609d4387b747019be910a1855aceb12bae07a7277f`
- Labels frozen: True
- Prompt version: qualify-v1

| Measure | Value |
|---|---|
| Items | 30 (labeled 30, answered 30) |
| Agreement with the person's tier | 43.3% (30 judged) |
| Latency p50 / p95 | 2371 ms / 4643 ms |
| Cost per lead | $0.001119 (known for 30 of 30) |
| Total cost | $0.033563 |
| Sent to review (low confidence) | 1 |
| Unusable answers | none |
| Calls that failed | none |

Agreement by kind of inquiry:

| Kind | Agree | Judged |
|---|---|---|
| BORDERLINE | 2 | 6 |
| CLEAR BAD | 3 | 6 |
| CLEAR GOOD | 5 | 6 |
| CONFLICTING DATA | 0 | 6 |
| MISSING DATA | 3 | 6 |

Confusion (person -> model): {'cold->cold': 6, 'hot->cold': 7, 'hot->hot': 5, 'hot->warm': 1, 'warm->cold': 7, 'warm->hot': 2, 'warm->warm': 2}

| Id | Person | Model | Score | Confidence | ms | Tokens in/out | Cost |
|---|---|---|---|---|---|---|---|
| E01 | warm | cold | 15 | 0.85 | 4599 | 502/111 | $0.001057 |
| E02 | warm | cold | 15 | 0.65 | 2349 | 503/112 | $0.001063 |
| E03 | hot | cold | 15 | 0.85 | 2843 | 548/122 | $0.001158 |
| E04 | hot | cold | 5 | 0.95 | 2249 | 562/118 | $0.001152 |
| E05 | warm | cold | 2 | 0.95 | 2153 | 529/114 | $0.001099 |
| E06 | hot | hot | 82 | 0.85 | 6860 | 569/120 | $0.001169 |
| E07 | warm | warm | 45 | 0.62 | 2097 | 538/111 | $0.001093 |
| E08 | cold | cold | 35 | 0.4 | 2176 | 513/107 | $0.001048 |
| E09 | cold | cold | 15 | 0.85 | 2217 | 498/108 | $0.001038 |
| E10 | warm | cold | 8 | 0.95 | 2348 | 527/107 | $0.001062 |
| E11 | hot | cold | 25 | 0.7 | 3134 | 553/116 | $0.001133 |
| E12 | warm | warm | 42 | 0.65 | 2684 | 527/122 | $0.001137 |
| E13 | hot | hot | 78 | 0.87 | 3362 | 578/127 | $0.001213 |
| E14 | cold | cold | 15 | 0.75 | 2618 | 508/120 | $0.001108 |
| E15 | hot | hot | 85 | 0.92 | 2379 | 560/114 | $0.001130 |
| E16 | hot | cold | 25 | 0.75 | 2228 | 559/123 | $0.001174 |
| E17 | warm | hot | 78 | 0.92 | 2371 | 553/116 | $0.001133 |
| E18 | warm | cold | 15 | 0.75 | 2482 | 520/117 | $0.001105 |
| E19 | cold | cold | 5 | 0.95 | 1761 | 522/89 | $0.000967 |
| E20 | warm | cold | 15 | 0.85 | 2063 | 557/110 | $0.001107 |
| E21 | cold | cold | 5 | 0.98 | 1918 | 516/94 | $0.000986 |
| E22 | hot | warm | 45 | 0.72 | 2225 | 549/109 | $0.001094 |
| E23 | hot | cold | 15 | 0.92 | 4643 | 542/120 | $0.001142 |
| E24 | hot | cold | 5 | 0.95 | 2441 | 553/135 | $0.001228 |
| E25 | hot | hot | 78 | 0.82 | 2197 | 564/121 | $0.001169 |
| E26 | hot | hot | 78 | 0.85 | 2916 | 598/114 | $0.001168 |
| E27 | warm | cold | 5 | 0.95 | 2772 | 522/125 | $0.001147 |
| E28 | warm | hot | 72 | 0.75 | 2964 | 563/143 | $0.001278 |
| E29 | cold | cold | 5 | 0.92 | 1943 | 525/94 | $0.000995 |
| E30 | hot | cold | 35 | 0.62 | 2882 | 565/129 | $0.001210 |
