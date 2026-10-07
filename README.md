# P01 Lead Automation

[![CI](https://github.com/advitionisland-island-it/ai-automation-portfolio/actions/workflows/ci.yml/badge.svg)](https://github.com/advitionisland-island-it/ai-automation-portfolio/actions/workflows/ci.yml)

P01の問い合わせ処理システムのソース、テスト、検証記録を公開しています。公開範囲はP01のみです。親リポジトリの履歴、`docs/ai/`、P02、内部判断記録、その他のプロジェクトは含めません。

2026-10-07のclean exportで **396 passed / 有料APIのlive test 1件除外** を再確認しました。export対象94ファイルのsecret pattern scanは0 findingsです。[対象・環境・確認範囲](evidence/p01/TEST_RESULTS.md)を参照してください。

## P01：問い合わせから、人が承認した返信まで

B2Bの問い合わせを受け付け、AIが見込み度を評価して返信案を作ります。送信する本文は人が確認し、承認したハッシュと一致することをアプリとDBの両方で検証します。n8n / Python・FastAPI / PostgreSQLを組み合わせ、重複受付、同時処理、障害回復を実装しています。

| 第三者が確認できること | 証拠 |
|---|---|
| テストの実行結果と再現方法 | [TEST_RESULTS](evidence/p01/TEST_RESULTS.md) |
| 意図的な不具合を見逃さないか | [MUTATION_RESULTS](evidence/p01/MUTATION_RESULTS.md)：過去checkpointの13/13 |
| どう壊れ、どう直したか | [FAILURE_TO_FIX](evidence/p01/FAILURE_TO_FIX.md)：NUL、制御文字、human-review fallback |
| 実API・使用量・費用・処理時間の確認範囲 | [API_SMOKE](evidence/p01/API_SMOKE.md)：過去の評価段階のみ |
| できないこと・未検証のこと | [LIMITATIONS](evidence/p01/LIMITATIONS.md) |

### 目的別に読む

- **処理の流れと設計判断**：[日本語ケーススタディ](projects/p01-lead-automation/docs/case-study.ja.md)
- **実装・起動方法**：[P01概要・起動方法](projects/p01-lead-automation/README.ja.md) / [English](projects/p01-lead-automation/README.md) / [構成](docs/ARCHITECTURE.md)
- **検証結果**：[証跡](evidence/p01/)・[GitHub Actions](https://github.com/advitionisland-island-it/ai-automation-portfolio/actions)

## 自分で実行する

```bash
git clone https://github.com/advitionisland-island-it/ai-automation-portfolio.git
cd ai-automation-portfolio/projects/p01-lead-automation
uv sync --locked
make up
make check
make e2e
make down
```

Docker Compose v2・make・uvが必要です。開発・E2Eはfake LLMを使い、メールはローカルのMailpitへ送ります。実API呼び出しや課金を既定の検証に含めません。

## 検証を読むときの注意

数値は実行日・対象・SHAを分けて記録します。過去の13/13 mutationや実API smokeを、今回再実行した結果として表示しません。CIバッジはbranchの最新状態であり、特定Releaseの証拠はReleaseに記載するcommitとCI runで確認してください。

認証は未実装、実モデルでの下書きは未検証、企業情報はstubです。SMTP受理直後・DB記録前の停止で二重送信になる既知の条件があります。公開しているのはコードで、一般公開の稼働サービスではありません。

[現在地とRelease条件](docs/PORTFOLIO_STATUS.md) · [Release一覧](https://github.com/advitionisland-island-it/ai-automation-portfolio/releases)

## English overview

This repository publishes P01 source code, tests and technical evidence only. Parent repository history and private material are excluded. P01 implements B2B inquiry intake, qualification, drafting and human approval with n8n, FastAPI and PostgreSQL. Inspect [the project](projects/p01-lead-automation/README.md), [case study](projects/p01-lead-automation/docs/case-study.md) and [evidence](evidence/p01/). Real-model qualification is historical evidence; drafting and E2E use a fake provider. See the limitations before interpreting the results as production readiness.
