# P01 · AI Sales Operations Engine

[English](README.md) | 日本語 · [ポートフォリオ](../../README.md)

**B2Bの問い合わせを受け付け、AIで評価して返信案を作り、人が承認した本文だけを送る仕組みです。** n8nは受付と通知、Pythonは入力検証・業務判断・承認、PostgreSQLは状態とジョブを担当します。メールはローカルのMailpitへ送ります。

## 30秒で見る検証と限界

| 証明したいこと | 確認する資料 |
|---|---|
| 再構成後のfresh検証で396テストPASS | [テスト結果](../../evidence/p01/TEST_RESULTS.md)。Releaseの対象commitはCIでも照合 |
| 意図的な不具合13件を13件検出 | [過去mutation結果](../../evidence/p01/MUTATION_RESULTS.md)。`f4e74a6`での限定的な検証 |
| 異常なAI出力を人の確認へ回す | [不具合と修正](../../evidence/p01/FAILURE_TO_FIX.md)、コードと回帰テスト |
| 実APIで見込み度評価と使用量を記録 | [過去smoke test](../../evidence/p01/API_SMOKE.md)。実モデルの下書きは未検証 |

## 動かす

repository rootから実行します。

```bash
cd projects/p01-lead-automation
uv sync --locked
make up
make check
make e2e
make down
```

Docker Compose v2・make・uvが必要です。全ポートはlocalhost限定で、認証は未実装です。

## 詳しく確認する

- [構成](../../docs/ARCHITECTURE.md)・[日本語の技術ガイド](docs/guide.ja.md)
- [日本語ケーススタディ](docs/case-study.ja.md)：課題、設計理由、発見した不具合、実測値
- [実装](src/sales_ops/)・[テスト](tests/)・[合成データのAPI例](examples/requests.http)
- [制約](../../evidence/p01/LIMITATIONS.md)：fake-onlyの下書き、企業情報stub、小さな評価、SMTPの二重送信条件

SMTP受理後・DB記録前の停止では、二重送信になります。評価だけは実モデルで確認済みですが、実モデルで下書きから送信まで通した検証はありません。数値の日時と対象範囲は証跡で確認できます。
