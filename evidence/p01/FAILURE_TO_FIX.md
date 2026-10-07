# P01 — 壊れ方から修正まで / Failure to fix

制御文字の問題を、入力検証とAI出力検証の両方で修正しました。以下は2026-09-28のcheckpoint `f4e74a6`に関する履歴要約です。今回の公開treeの実行結果は[TEST_RESULTS](TEST_RESULTS.md)で分けて記録します。

## 1. NULを含む問い合わせが500になる

**Before:** 名前・会社名・本文のNULが入力検証を通過し、PostgreSQLが保存を拒否して500になりました。

**Fix:** APIの文字列を共通ルールで検査し、空白を除く前に拒否します。NUL、BEL、C1等の許可しない制御文字は422となり、問い合わせは保存されません。tab・LF・CRは許可されます。却下理由も同じルールに統一しました。

**Evidence:** [schemas.py](../../projects/p01-lead-automation/src/sales_ops/schemas.py)、[text.py](../../projects/p01-lead-automation/src/sales_ops/text.py)、[test_leads.py](../../projects/p01-lead-automation/tests/test_leads.py)、[test_review_and_sending.py](../../projects/p01-lead-automation/tests/test_review_and_sending.py)。

## 2. 下書きの正規化が不正文字を黙って消す

**Before:** 行末のU+001FやNELが正規化で消え、件名の制御文字が空白に変わり、異常な入力が受理される場合がありました。

**Fix:** qualification・draft・人の編集が共通の制御文字ルールを使用します。domain validationを正規化より先に行い、不正なAI出力は`needs_review`へ回します。NULはDBに保存できないため、調査用のraw出力を保持する際は表示用文字`␀`へ置換します。これは受理する本文のクリーニングとは別です。人の編集に不正文字がある場合は422で、既存の下書きを変えません。

**Evidence:** [drafting.py](../../projects/p01-lead-automation/src/sales_ops/drafting.py)、[qualification.py](../../projects/p01-lead-automation/src/sales_ops/qualification.py)、[worker.py](../../projects/p01-lead-automation/src/sales_ops/worker.py)、[test_drafting.py](../../projects/p01-lead-automation/tests/test_drafting.py)、[test_worker.py](../../projects/p01-lead-automation/tests/test_worker.py)。

## 修正が効くことの確認

当時の通常テストは396 passed。検査を外す・対象を狭める・正規化後へ移す等、13個の選択した変異を13/13検出しました。[変異の範囲と結果](MUTATION_RESULTS.md)。現在のsuiteが存在することと、当時の変異実行を現在SHAで再現したことは区別します。

English: NUL database errors became API validation errors; invalid draft characters are rejected before normalization and routed to human review instead of being silently stripped.
