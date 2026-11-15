# 波動スキャナー クラウド版（通知のみ・注文はしない）

GitHub Actions（無料）が15分ごとに為替6ペアを判定し、波動の形が揃った自動候補を Discord に送ります。
個人の検証用です。売買の助言ではありません。結果は保証されません。

- 判定ロジック：`app/scanner.py`、数値：`scanner.yaml`
- 通知先：Discord の Webhook（GitHub の Settings → Secrets → `DISCORD_WEBHOOK_URL` に登録。コードには書かない）
- 送信済みの記録：`sent_signals.json`（ペア・方向・時刻だけ）
- 価格データ：Yahoo Finance（無料の参考値）
