# P01 — 制約 / Limitations

P01は、AIを業務へ組み込む際の検証・承認・障害処理を示すローカルのポートフォリオです。顧客への本番導入実績や売上改善を主張するものではありません。

| 制約 | 現在の範囲と意味 |
|---|---|
| 認証・権限管理 | 未実装。reviewer名は呼び出し側の申告で、本人確認には使えません。本文SHA-256の一致は認証を代替しません |
| 外部への公開運用 | 開発用の入口は127.0.0.1。公開Internet向けのTLS、認証、権限、rate limiting等を備えた運用を検証していません |
| 実LLMの適用範囲 | 実APIはqualificationで確認。draftingはfake providerのみ。実モデルで問い合わせ→送信のE2Eは未検証 |
| メール送信 | Mailpitのみ。顧客への配信、到達性、bounce、unsubscribe等の運用は対象外 |
| モデルの判断品質 | 合成30件の人のtierラベルとの一致率はSonnet 40.0%、Haiku 43.3%。実顧客母集団での精度や営業成果は不明 |
| 企業情報補完 | stubによる合成データ。外部CRMや企業データサービスとの実接続ではありません |
| exactly-once delivery | SMTP成功後、DB記録前に停止すると再試行で重複し得ます。同じMessage-IDを使っても受信側の重複排除は保証できません |
| 通知の遅延・重複 | n8nは10秒pollとleaseを使用。at-least-onceであり、送信後の記録前に停止する境界では通知も再配信し得ます |
| QAの結果 | テスト成功は選んだケースの結果。coverageの測定値や独立監査、全欠陥の不存在を主張しません |
| 証拠の時点 | 過去smoke・mutationの結果を現在SHAのfresh resultとは表示しません。Releaseには対象commitと結果を結び付ける必要があります |

SMTPの既知の境界は[test_review_and_sending.py](../../projects/p01-lead-automation/tests/test_review_and_sending.py)の`test_known_limit_a_crash_between_smtp_and_the_record_sends_twice_with_one_message_id`で明示しています。評価の条件は[eval/LABELING.md](../../projects/p01-lead-automation/eval/LABELING.md)と公開レポートを確認してください。

English: local portfolio scope; no authentication, fake-only drafting, synthetic evaluation, Mailpit-only sending, and a documented at-least-once duplicate boundary.
