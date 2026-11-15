"""クラウド版：波動スキャナーの自動候補を判定し、新しいものだけ Discord に送る（GitHub Actions 用）。

1回実行すると1回判定して終わる。15分ごとの実行は .github/workflows/scan.yml が担当する。
判定ロジックは Mac 版と同じ app/scanner.py（数値は scanner.yaml）。注文・証券会社への接続はしない。
送るのは通知文だけ。Webhook URL は環境変数 DISCORD_WEBHOOK_URL（GitHub の Secrets）から読む。

起動:
  python run_cloud.py            判定して、新しい候補があれば Discord に送る
  python run_cloud.py --dry-run  判定だけして画面に表示する（送信・保存しない）
  python run_cloud.py --test     Discord にテスト通知を1通送る
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import time
from typing import Callable

import requests

HERE = pathlib.Path(__file__).resolve().parent
SENT_PATH = HERE / "sent_signals.json"
HEALTH_PATH = HERE / "health.json"
JST = dt.timezone(dt.timedelta(hours=9))
FOOTER = "記録とルールからの計算です。売買の判断はご自身で"
KEEP_DAYS = 14
ALERT_AFTER_FAILS = 3  # この回数続けて失敗したら1回だけ知らせる（15分×3 = 約45分）
KEEPALIVE_DAYS = 7  # 動きがない公開リポジトリは60日で自動実行が止まるため、週1回だけ記録を更新する

Scan = Callable[[dt.datetime], list]
Send = Callable[[str], bool]


def with_retry(fn, tries: int = 3, wait: float = 3.0):
    """Yahoo が一時的に断ったときのために、数回やり直す。"""
    for i in range(tries):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(wait * (i + 1))


def update_health(path: pathlib.Path, ok: bool, now: dt.datetime, send: Send, error: str = "") -> dict:
    """失敗が続いたら1回だけ・復旧したら1回だけ Discord に知らせる。週1回 last_ok を更新する。"""
    try:
        h = json.loads(path.read_text(encoding="utf-8"))
        h = h if isinstance(h, dict) else {}
    except (OSError, ValueError):
        h = {}
    fails = int(h.get("fail_runs", 0))
    if ok:
        if fails >= ALERT_AFTER_FAILS:
            send("✅ クラウド版が復旧しました（自動候補の判定が再開）")
        h["fail_runs"] = 0
        try:
            stale = now - dt.datetime.fromisoformat(h["last_ok"]) >= dt.timedelta(days=KEEPALIVE_DAYS)
        except (KeyError, ValueError):
            stale = True
        if stale:
            h["last_ok"] = now.isoformat()
    else:
        h["fail_runs"] = fails + 1
        if h["fail_runs"] == ALERT_AFTER_FAILS:
            send(f"🔴 クラウド版が{ALERT_AFTER_FAILS}回連続で失敗しています（{error}）。"
                 "この間は自動候補が出ません。Yahoo に断られた可能性があります")
    path.write_text(json.dumps(h, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    return h


def load_sent(path: pathlib.Path) -> dict[str, str]:
    """送信済みの {キー: 候補の時刻}。壊れていたら空から始める（二重通知より取りこぼしを避ける）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def prune(sent: dict[str, str], now: dt.datetime) -> dict[str, str]:
    cutoff = now - dt.timedelta(days=KEEP_DAYS)
    out = {}
    for k, v in sent.items():
        try:
            if dt.datetime.fromisoformat(v) >= cutoff:
                out[k] = v
        except ValueError:
            continue
    return out


def run(now: dt.datetime, scan: Scan, send: Send, sent_path: pathlib.Path = SENT_PATH,
        lookback_minutes: int = 60, dry_run: bool = False) -> list[dict]:
    """新しい候補だけ送る。送信に失敗した候補は記録せず、次の回に再送する。"""
    found = scan(now)  # 全ペア取得失敗などは例外のまま上に伝え、Actions を失敗にして気づけるようにする
    since = now - dt.timedelta(minutes=lookback_minutes)
    sent = prune(load_sent(sent_path), now)
    out = []
    for c in found:
        if c["time"] < since or c["key"] in sent:
            continue
        text = f"🤖 自動候補（クラウド） {c['title']}\n{c['body']}\n※ 機械が見つけた候補。{FOOTER}"
        if dry_run:
            print(text, "\n")
            out.append(c)
            continue
        if not send(text):
            print(f"送信失敗（次回に再送）: {c['key']}", file=sys.stderr)
            continue
        sent[c["key"]] = c["time"].isoformat()
        out.append(c)
    if not dry_run:
        sent_path.write_text(json.dumps(sent, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    return out


def scan_all(now: dt.datetime) -> list[dict]:
    """scanner.yaml の全ペアを判定して候補を返す。全ペア取得に失敗したときは例外にする。"""
    from app import scanner

    cfg = scanner.load_config()
    prefix = cfg["watch"]["message_prefix"]
    out, failed = [], 0
    for pair in cfg["pairs"]:
        try:
            m15, h4 = with_retry(lambda: scanner.fetch(pair, cfg))
        except Exception as e:  # noqa: BLE001  1ペアの失敗で全体を止めない
            print(f"{pair} データ取得失敗: {type(e).__name__}", file=sys.stderr)
            failed += 1
            continue
        if m15.empty or h4.empty:
            print(f"{pair} データなし（市場休み？）")
            continue
        for s in scanner.scan(pair, m15, h4, cfg):
            title, _, body = scanner.format_message(s, prefix).partition("\n")
            out.append({"key": f"{s.pair}|{s.direction}|{s.time:%Y-%m-%d %H:%M}", "time": s.time.to_pydatetime(),
                        "pair": s.pair, "title": title, "body": body})
    if failed == len(cfg["pairs"]):
        raise RuntimeError("全ペアのデータ取得に失敗（Yahoo に断られた可能性）")
    return out


def send_discord(text: str) -> bool:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        print("DISCORD_WEBHOOK_URL が設定されていません", file=sys.stderr)
        return False
    try:
        r = requests.post(url, json={"content": text}, timeout=15)
    except requests.RequestException as e:
        print(f"Discord 送信失敗: {type(e).__name__}", file=sys.stderr)
        return False
    if r.status_code >= 300:
        print(f"Discord 送信失敗: HTTP {r.status_code}", file=sys.stderr)
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="波動スキャナー クラウド版（通知のみ・注文はしない）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    if args.test:
        ok = send_discord("✅ fx-lab クラウド版：テスト通知です。これが届けば設定OK。")
        print("テスト通知:", "送信OK" if ok else "失敗")
        return 0 if ok else 1
    now = dt.datetime.now(JST)
    try:
        out = run(now=now, scan=scan_all, send=send_discord, dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001
        print(f"失敗: {e}", file=sys.stderr)
        if not args.dry_run:
            update_health(HEALTH_PATH, False, now, send_discord, error=str(e))
        return 1  # Actions を失敗にして GitHub のメールでも気づけるようにする
    if not args.dry_run:
        update_health(HEALTH_PATH, True, now, send_discord)
    print(f"判定完了：新しい候補 {len(out)}件{'（dry-run・未送信）' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
