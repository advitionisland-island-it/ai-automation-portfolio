# P01 — 変異テスト / Mutation results

**2026-09-28のcheckpoint `f4e74a6`で、制御文字防御を意図的に壊す13種類の変異を13/13検出しました。** 今回の公開treeでの新しい実行結果ではありません。

これは選んだ欠陥に対するテストの検出力を確認する、手動で作成したseeded mutationです。全コードへの自動mutation score、網羅率100%、独立した第三者監査という意味ではありません。公開資料は結果の要約で、当時の診断用実行ログやrunnerは同梱していません。

## 何を壊したか

| ID | 意図的な変更 | 当時の結果 |
|---|---|---|
| T1 | C1範囲 U+0080–U+009Fを検査しない | detected |
| T2 | NULだけを検査する | detected |
| T3 | 許可されるtab・LF・CRまで拒否する | detected |
| T4 | 複数の入力文字列の最初だけを検査する | detected |
| Q1 | qualificationで検査を外す | detected |
| D1 | draftで検査を外す | detected |
| D2 | draftを正規化した後に検査する | detected |
| D3 | draftの件名だけを検査する | detected |
| S1 | 問い合わせ本文を検査しない | detected |
| S2 | 名前を検査しない | detected |
| S3 | 会社名を検査しない | detected |
| S4 | 却下理由を検査しない | detected |
| S5 | API入力の空白除去後に検査する | detected |

変更していないコピーのcontrol runは成功しました。制御文字検査を完全に無効化するcanaryは失敗し、テストが変更したコピーを実際に読み込むことを確認しました。canaryは13件の分母に含めていません。

## 現在の公開コードで確認できるもの

共通ルールは[text.py](../../projects/p01-lead-automation/src/sales_ops/text.py)。[test_text.py](../../projects/p01-lead-automation/tests/test_text.py)は制御文字の規則を、[test_drafting.py](../../projects/p01-lead-automation/tests/test_drafting.py)・[test_leads.py](../../projects/p01-lead-automation/tests/test_leads.py)・[test_review_and_sending.py](../../projects/p01-lead-automation/tests/test_review_and_sending.py)は各入口と正規化順を確認します。

English: 13/13 selected, manually seeded control-character mutants were detected at a historical checkpoint. This is not a repository-wide mutation coverage claim.
