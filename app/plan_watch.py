"""価格見張り係：朝の計画（data/plans/*.json）を、5分ごとに1回だけ判定して通知する。

1回実行すると1回判定して終わる。繰り返しは launchd に任せる。
- 価格：Yahoo Finance（yfinance。無料・APIキー不要・読み取りのみ）
- 通知：Mac の通知センター（osascript。外部送信なし）。plan_watch.json で discord: true のときだけ Discord にも送る
- 波動スキャナー（app/scanner.py）の自動候補も、M15 確定直後の回（15分に1回）だけ判定して同じ通知で出す。
  朝の計画とは混ぜない：通知に【自動候補】と付け、出来事ログには event="scanner"・plan_id なしで残す
- 注文・証券会社への接続は一切しない

起動:
  venv/bin/python -m app.plan_watch             判定して、状態が変わった計画だけ通知
  venv/bin/python -m app.plan_watch --dry-run   判定だけして画面に表示（保存・通知しない）
  venv/bin/python -m app.plan_watch --test      Mac にテスト通知を1件出す
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import pathlib
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

from app import plans
from app.plans import JST, Plan, Quote

ROOT = plans.ROOT
LOG_PATH = ROOT / "logs" / "plan_watch.log"
EVENTS = ROOT / "data" / "plan_events.jsonl"
SCANNER_SENT = ROOT / "data" / "sent_signals.json"

log = logging.getLogger("plan_watch")

Feed = Callable[[str, dt.datetime], "Quote | None"]
Notifier = Callable[[str, str], bool]
Scan = Callable[[dt.datetime], list]  # 候補 dict のリスト（scanner_feed の戻り値の形）


@dataclass
class Paths:
    plans_dir: pathlib.Path = plans.PLANS_DIR
    events: pathlib.Path = EVENTS
    heartbeat: pathlib.Path = plans.HEARTBEAT
    scanner_sent: pathlib.Path = SCANNER_SENT


# ---------------------------------------------------------------------
# 価格（本番用。テストでは偽物に差し替える）
def yfinance_feed(pair: str, now: dt.datetime) -> Quote | None:
    import pandas as pd
    import yfinance as yf

    df = yf.download(f"{pair}=X", interval="15m", period="2d", progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Close"]].dropna()
    if df.empty:
        return None
    idx = df.index.tz_convert(JST) if df.index.tz is not None else df.index.tz_localize("UTC").tz_convert(JST)
    ends = [t.to_pydatetime() + dt.timedelta(minutes=15) for t in idx]
    closed = [i for i, e in enumerate(ends) if e <= now]
    if not closed:
        return None
    i = closed[-1]
    return Quote(price=float(df["Close"].iloc[-1]), bar_close=float(df["Close"].iloc[i]), bar_end=ends[i])


def scanner_feed(now: dt.datetime) -> list[dict]:
    """波動スキャナーを全ペア走らせ、出ていた候補を返す（本番用。テストでは偽物に差し替える）。"""
    from app import scanner

    cfg = scanner.load_config()
    prefix = cfg["watch"]["message_prefix"]
    out = []
    for pair in cfg["pairs"]:
        try:
            m15, h4 = scanner.fetch(pair, cfg)
        except Exception as e:  # noqa: BLE001  1ペアの失敗で全体を止めない
            log.error("スキャナー %s データ取得失敗: %s", pair, type(e).__name__)
            continue
        if m15.empty or h4.empty:
            continue
        for s in scanner.scan(pair, m15, h4, cfg):
            title, _, body = scanner.format_message(s, prefix).partition("\n")
            out.append({"key": f"{s.pair}|{s.direction}|{s.time:%Y-%m-%d %H:%M}", "time": s.time.to_pydatetime(),
                        "pair": s.pair, "direction": s.direction, "entry": s.entry, "title": title, "body": body})
    return out


# ---------------------------------------------------------------------
# 通知
def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def mac_notify(title: str, body: str, runner=subprocess.run) -> bool:
    script = f'display notification "{_esc(body)}" with title "fx-lab" subtitle "{_esc(title)}" sound name "Glass"'
    try:
        r = runner(["osascript", "-e", script], capture_output=True, timeout=15)
    except Exception as e:  # noqa: BLE001
        log.error("Mac 通知失敗: %s", type(e).__name__)
        return False
    return r.returncode == 0


def discord_notify(title: str, body: str) -> bool:
    """承認済みの唯一の外部送信先。plan_watch.json の notify.discord が true のときだけ使う。"""
    from dotenv import load_dotenv

    from app import watch

    load_dotenv(ROOT / ".env")
    return watch.send_discord(f"{title}\n{body}")


# ---------------------------------------------------------------------
# 1回分の判定
def active_plans(plans_dir: pathlib.Path, now: dt.datetime, lookback_days: int = 3) -> list[tuple[Plan, dt.date]]:
    out = []
    for i in range(lookback_days):
        date = now.date() - dt.timedelta(days=i)
        out += [(p, date) for p in plans.load_plans(plans_dir, date) if p.state not in plans.TERMINAL]
    return out


def _append_event(path: pathlib.Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def run(now: dt.datetime, feed: Feed, notifiers: list[Notifier], paths: Paths, account: dict | None,
        cfg: dict, dry_run: bool = False) -> list[dict]:
    active = active_plans(paths.plans_dir, now, cfg.get("lookback_days", 3))
    errors: list[str] = []
    feed_errors: list[str] = []
    quotes: dict[str, Quote | None] = {}

    def get(pair: str) -> Quote | None:
        if pair not in quotes:
            try:
                quotes[pair] = feed(pair, now)
            except Exception as e:  # 1ペアの失敗で全体を止めない
                errors.append(f"{pair}: {type(e).__name__}")
                feed_errors.append(f"{pair}: {type(e).__name__}")
                quotes[pair] = None
        return quotes[pair]

    changed: dict[dt.date, list[Plan]] = defaultdict(list)
    events: list[dict] = []
    for plan, date in active:
        q = get(plan.pair)
        if q is None:
            if dt.datetime.fromisoformat(plan.expires) > now:
                continue
            q = Quote(price=float("nan"), bar_close=float("nan"), bar_end=now)
        state, event = plans.evaluate(plan, q, now, cfg)
        if event is None:
            continue
        rec = {"time": now.isoformat(), "plan_id": plan.id, "pair": plan.pair, "direction": plan.direction,
               "event": event, "price": q.price, "bar_close": q.bar_close, "bar_end": q.bar_end.isoformat()}
        if event != "expired":
            rate = None
            if event in ("triggered", "chased") and not plans.risk_check.is_jpy_quote(plan.pair):
                rq = get(plans.quote_currency_jpy_pair(plan.pair))
                rate = rq.price if rq else None
            title, body = plans.format_event(plan, event, q, account, quote_jpy_rate=rate, cfg=cfg)
            if dry_run:
                print(f"{title}\n{body}\n")
                events.append(rec)
                continue
            if not any([n(title, body) for n in notifiers]):
                errors.append(f"{plan.id}: 通知失敗（次回に再判定）")
                continue
        if dry_run:
            continue
        plan.state = state
        plan.updated_at = now.isoformat()
        changed[date].append(plan)
        _append_event(paths.events, rec)
        events.append(rec)

    if not dry_run:
        for date, ps in changed.items():
            plans.save_plans(paths.plans_dir, date, ps)
        prev = {}
        if paths.heartbeat.exists():
            try:
                prev = json.loads(paths.heartbeat.read_text(encoding="utf-8"))
            except ValueError:
                prev = {}
        fail_runs = prev.get("feed_fail_runs", 0) + 1 if feed_errors else 0
        if fail_runs == cfg.get("alert_after_failed_runs", 3):
            title = "🔴 価格が取れていません"
            body = (f"Yahoo Finance から価格を {fail_runs}回連続で取得できませんでした（"
                    + "、".join(feed_errors) + "）。この間は条件到達を通知できません。"
                    "TradingView のアラートで確認してください\n" + plans.FOOTER)
            for n in notifiers:
                n(title, body)
        paths.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        paths.heartbeat.write_text(json.dumps({
            "last_run": now.isoformat(), "active_plans": len(active), "events": len(events), "errors": errors,
            "feed_errors": feed_errors, "feed_fail_runs": fail_runs,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    for e in errors:
        log.error(e)
    return events


def run_scanner(now: dt.datetime, scan: Scan, notifiers: list[Notifier], paths: Paths, cfg: dict,
                dry_run: bool = False) -> list[dict]:
    """自動候補を通知する。M15 確定直後の5分間（:00〜:04, :15〜:19 …）の回だけ動く。

    実行が1回飛んでも拾えるよう、直近 scanner_lookback_minutes 分の候補を見る。
    二重通知は scanner_sent に記録したキーで防ぐ。通知に失敗した候補は記録せず、次の回に再送する。
    """
    if not cfg.get("scanner") or now.minute % 15 >= 5:
        return []
    try:
        found = scan(now)
    except Exception as e:  # noqa: BLE001
        log.error("スキャナー失敗: %s", type(e).__name__)
        return []
    since = now - dt.timedelta(minutes=cfg.get("scanner_lookback_minutes", 45))
    sent = set(json.loads(paths.scanner_sent.read_text(encoding="utf-8"))) if paths.scanner_sent.exists() else set()
    out = []
    for c in found:
        if c["time"] < since or c["key"] in sent:
            continue
        title = f"🤖 自動候補 {c['title']}"
        body = f"{c['body']}\n※ 機械が見つけた候補（朝の計画ではない）。{plans.FOOTER}"
        rec = {"time": now.isoformat(), "event": "scanner", "pair": c["pair"], "direction": c["direction"],
               "price": c["entry"], "signal_time": c["time"].isoformat()}
        if dry_run:
            print(f"{title}\n{body}\n")
            out.append(rec)
            continue
        if not any([n(title, body) for n in notifiers]):
            log.error("%s: 通知失敗（次回に再送）", c["key"])
            continue
        sent.add(c["key"])
        _append_event(paths.events, rec)
        out.append(rec)
    if not dry_run and out:
        paths.scanner_sent.parent.mkdir(parents=True, exist_ok=True)
        paths.scanner_sent.write_text(json.dumps(sorted(sent), ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def write_cloud_heartbeat(path: pathlib.Path, now: dt.datetime) -> None:
    """クラウドに計画の見張りを任せている間、Mac は通知せず「動いている」印だけ残す（二重通知を避ける）。"""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_run": now.isoformat(), "mode": "cloud", "active_plans": 0, "events": 0,
                                "errors": [], "feed_errors": [], "feed_fail_runs": 0},
                               ensure_ascii=False, indent=1), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="朝の計画の価格見張り（注文はしない）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true", help="Mac にテスト通知を1件出す")
    args = ap.parse_args(argv)

    LOG_PATH.parent.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()])
    cfg = plans.load_config()
    if args.test:
        ok = mac_notify("テスト通知", "fx-lab 価格見張り係：これが見えれば通知の設定OK。\n" + plans.FOOTER)
        log.info("テスト通知: %s", "OK" if ok else "失敗")
        return 0 if ok else 1

    notifiers: list[Notifier] = []
    if cfg["notify"].get("mac", True):
        notifiers.append(mac_notify)
    if cfg["notify"].get("discord"):
        notifiers.append(discord_notify)
    now = dt.datetime.now(JST)
    if cfg.get("cloud_plans"):  # 朝の計画の見張りはクラウドが担当（docs/cloud_scanner.md）。Mac は通知しない
        events = []
        if not args.dry_run:
            write_cloud_heartbeat(Paths().heartbeat, now)
    else:
        events = run(now=now, feed=yfinance_feed, notifiers=notifiers, paths=Paths(),
                     account=plans.load_account(), cfg=cfg, dry_run=args.dry_run)
    found = run_scanner(now=now, scan=scanner_feed, notifiers=notifiers, paths=Paths(), cfg=cfg,
                        dry_run=args.dry_run)
    log.info("判定完了：出来事 %d件・自動候補 %d件%s", len(events), len(found),
             "（dry-run・未通知・未保存）" if args.dry_run else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
