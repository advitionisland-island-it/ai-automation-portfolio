# P01 — テスト結果 / Test results

P01の通常テストは、実PostgreSQLとMailpitを使って入力受付、重複・並行処理、AI出力検証、承認、送信、障害回復を確認します。有料APIのlive testは通常実行から除外しています。

## 検証時点を分ける

| 対象 | 結果 | 解釈 |
|---|---|---|
| 公開済みcheckpoint `2d57491` | 396 passed、live 1件は選択対象外 | 公開用P01での再実行結果。今回のディレクトリ再構成前の結果 |
| 再構成tree・2026-10-07のfresh run | **396 passed、live 1件は選択対象外** | 専用PostgreSQL/Mailpitで実行。最終commitの結果はCI artifactとRelease manifestで照合 |
| GitHub Actionsの対象SHA | SHA別のrunとartifactで確認 | ローカル成功とは別。Releaseのcommit・CI run URLを正本とする |

コードカバレッジは今回未測定です。通常テストにはmock HTTP transportによるAnthropic adapter確認も含まれ、実LLMの応答品質は別の評価です。

## 再現手順

repository rootから:

```bash
cd projects/p01-lead-automation
make check       # lint、format、typecheck、secret pattern scan、通常テスト
make e2e         # Docker stackでフォーム→承認→Mailpit、API停止時の通知
```

`make check`はDockerのPostgreSQLとMailpitを起動します。Python/uv等の前提は[プロジェクトREADME](../../projects/p01-lead-automation/README.ja.md)を参照してください。`make e2e`は別のチェックであり、396件というpytest件数に含めて表現しません。

## レビュアーが見る場所

| 確認したい性質 | テスト |
|---|---|
| idempotency、入力422、同時受付 | [test_leads.py](../../projects/p01-lead-automation/tests/test_leads.py) |
| 不正なAI出力を人へ回す | [test_qualification.py](../../projects/p01-lead-automation/tests/test_qualification.py)、[test_drafting.py](../../projects/p01-lead-automation/tests/test_drafting.py)、[test_worker.py](../../projects/p01-lead-automation/tests/test_worker.py) |
| 承認した本文の一致、送信の競合・既知の重複境界 | [test_review_and_sending.py](../../projects/p01-lead-automation/tests/test_review_and_sending.py) |
| 状態遷移とDB制約 | [test_states.py](../../projects/p01-lead-automation/tests/test_states.py)、[test_migrations.py](../../projects/p01-lead-automation/tests/test_migrations.py) |
| n8nの設定と通知 | [test_n8n_workflows.py](../../projects/p01-lead-automation/tests/test_n8n_workflows.py)、[test_notifications.py](../../projects/p01-lead-automation/tests/test_notifications.py) |

English: historical results and a current-tree rerun are separate claims. Paid live tests are intentionally excluded from the ordinary suite.

## 今回のローカル検証（2026-10-07）

- Python 3.13.15、lockfileで作成した専用環境、PostgreSQL 17、Mailpit v1.31.2。
- `python -m pytest -q`：396 passed / live 1件除外、27.71秒。
- fresh Docker build / healthy start / E2E：PASS。合成フォーム受付、重複抑止、承認依頼、人の承認、承認済み本文の1通配送、API停止時アラート、API復帰を確認。
- lint / format / mypy：PASS。依存監査：既知の脆弱性なし（ローカルのeditable package自体は監査サービス対象外）。secret pattern scan：0 findings。これらはpytest件数に含めない。
- 既存スタックとの競合を避け、専用Compose projectとlocalhostの別ポートを使用。E2Eは既存スクリプトの接続先だけを実行時に差し替え、アプリの業務処理は変更しない。

実行時のMarkdown以外のtracked files（コード・テスト・設定など）を、path順に`path + NUL + SHA256(file bytes)`で連結したSHA-256：

`69af6a89f870e3149f8970da26ac8e6b85b5757a25cf44ca867a0a9299fc2efd`

最終commit自身の396テストはpre-pushとGitHub CIで再確認し、CI artifactは対象の完全SHAを名前と`commit.txt`に記録します。localログとZIPの照合情報はRelease成果物へ添付します。
