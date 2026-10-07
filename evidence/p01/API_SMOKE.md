# P01 — 実API smoke / Real API smoke

**2026-09-27に、合成問い合わせ1件をworker経由で実Anthropic APIに送り、qualificationを確認しました。** この文書の作成に伴う新たな有料API呼び出しは行っていません。

| 項目 | 当時の実測 |
|---|---|
| Provider / model | Anthropic / `claude-sonnet-5` |
| 呼び出し対象 | qualification 1件、HTTP 200 |
| Input / output tokens | 736 / 462 |
| 推定コスト | $0.006092（当時設定したtoken単価による計算） |
| Latency | 6.8 s |
| 出力 | score 82、tier hot、confidence 0.6 |
| 記録 | provider、model、prompt version、tokens、cost、latency |

## 証明できる範囲

workerから実APIを呼び、応答を検証し、assessmentと呼び出し情報を記録する経路を確認した単発smokeです。実モデルによる下書き、承認、メール送信までのE2E、継続稼働、応答品質の一般化は証明しません。下書きから送信までの経路はfake providerとMailpitで検証しています。

1件のsmokeと30件の評価データセットは別です。モデル評価は[Sonnetの公開レポート](../../projects/p01-lead-automation/eval/reports/20260927T091709Z-anthropic-claude-sonnet-5.md)と[Haikuの公開レポート](../../projects/p01-lead-automation/eval/reports/20260927T091932Z-anthropic-claude-haiku-4-5.md)を参照してください。

## 再実行を選ぶ場合

[実APIテスト](../../projects/p01-lead-automation/tests/test_live_anthropic.py)と[adapter](../../projects/p01-lead-automation/src/sales_ops/anthropic_llm.py)を読んでから、プロジェクト内で`make smoke-live`を実行します。有効なAPI keyと課金が必要で、通常CIには含めません。keyはgit管理外の環境変数で渡します。

SDKのretryを無効にし、workerが試行を管理します。usageを取得できないtimeout等ではtokens/costをNULL（unknown）で記録します。API呼び出しが成功しても、出力はschema/domain validationを通します。

English: historical, one-call qualification smoke only; not a live-model end-to-end product validation.
