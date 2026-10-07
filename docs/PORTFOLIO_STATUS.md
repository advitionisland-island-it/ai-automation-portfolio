# Portfolio status / 現在地

対象はP01のみ。ソース公開は完了済みで、この更新ではレビュー担当が検証できる構造へ整理しています。

| 項目 | 状態・確認先 |
|---|---|
| 公開baseline | `2d57491263c4a0f786ca2e366329ba816327bc37`。P01と日本語資料を公開済み |
| baselineのローカルテスト | 396 PASS / live 1件除外。再構成後の検証とは別 |
| baselineのGitHub CI | FAIL：存在しないsetup-uv action指定。有効な公式actionのcommit固定へ修正 |
| 今回の構造 | root README → projects/P01 → evidence/P01 |
| 今回の検証 | fresh tests 396 PASS。実行結果は[TEST_RESULTS](../evidence/p01/TEST_RESULTS.md)へ記録 |
| 現在SHAのCI | [Actions](https://github.com/advitionisland-island-it/ai-automation-portfolio/actions)で確認。Releaseは対象SHAのsuccess後のみ |
| Release | `v1.0.0`は検証済みSHAのtracked tree、sanitized ZIP、SHA-256を揃えて作成 |
| ライセンス | 未指定。LICENSEを置いた場合はその内容を確認。未設定の場合、利用許諾を推定しない |
| P02・P03 | 今回の公開・検証・Releaseには含めない |

## Releaseを凍結する条件

1. 同じsource treeでfresh tests・Docker E2E・secret scanがPASS。
2. 最終commitのGitHub Actionsがsuccess。
3. ZIPはそのcommitのtracked filesから作り、環境・秘密・親履歴・内部資料を含めない。
4. ZIPを再検査し、SHA-256とcommit、CI run URLをReleaseへ記録する。

commit自身にそのSHAを書くと新しいSHAになるため、最終SHAの確認結果はRelease本文と添付manifestで固定します。repo内の結果は実行時点のtreeと手順を示します。過去mutation・実API呼び出しを今回再実行済みとは扱いません。
