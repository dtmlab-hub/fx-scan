"""クラウド版：朝の計画の見張り（GitHub Actions 用）。判定ロジックは Mac 版の app/plan_watch.run をそのまま使う。

計画は暗号化 Secret（環境変数 FX_PLANS）から読む。通知済みの状態は plan_state.json に残すが、
中身は「計画IDの salt 付きハッシュ → 状態」と価格取得の失敗回数だけ（価格・ペア・方向は残さない）。
送るのは Discord の通知文だけ。注文・証券会社への接続はしない。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import sys
import tempfile
from typing import Callable

from app import cloud_payload, plan_watch, plans

Send = Callable[[str], bool]
REPORT_HOUR = 6  # 毎朝この時刻以降の最初の実行で稼働報告を1通送る（来ない日＝止まっている）


def _load(path: pathlib.Path) -> dict:
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _dump(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True)


def run_plans(now: dt.datetime, payload_text: str, feed, send: Send, state_path: pathlib.Path) -> list[dict]:
    state = _load(state_path)
    before = _dump(state)
    events: list[dict] = []
    active = 0

    data = None
    if payload_text and payload_text.strip():
        try:
            data = cloud_payload.parse_payload(payload_text)
        except ValueError as e:
            if not state.get("bad_payload") and send(f"🔴 クラウドの計画データが読めません（{e}）。"
                                                     "朝の計画の見張りは止まっています。Mac で python3 -m app.cloud_push を実行してください。" + plans.FOOTER):
                state["bad_payload"] = True
        else:
            state.pop("bad_payload", None)

    if data is None:
        state["plans"] = {}
    else:
        salt = data["salt"]
        cfg = data["cfg"]
        saved = state.get("plans", {})
        plist = [plans._from_dict(x) for x in data["plans"]]
        for p in plist:
            if p.state not in plans.TERMINAL:
                p.state = saved.get(cloud_payload.state_key(salt, p.id), p.state)

        def notify(title: str, body: str) -> bool:
            return send(f"☁️ {title}\n{body}")

        # plan_watch のログには計画IDやペア名が入る。公開リポジトリの実行ログに出さないよう、クラウドでは止める
        logging.getLogger("plan_watch").disabled = True
        with tempfile.TemporaryDirectory() as t:
            tmp = pathlib.Path(t)
            by_date: dict[str, list] = {}
            for p in plist:
                by_date.setdefault(p.date, []).append(p)
            for date, ps in by_date.items():
                plans.save_plans(tmp / "plans", dt.date.fromisoformat(date), ps)
            hb = tmp / "hb.json"
            hb.write_text(json.dumps({"feed_fail_runs": int(state.get("feed_fail_runs", 0))}), encoding="utf-8")
            paths = plan_watch.Paths(plans_dir=tmp / "plans", events=tmp / "events.jsonl", heartbeat=hb,
                                     scanner_sent=tmp / "sent.json")
            events = plan_watch.run(now=now, feed=feed, notifiers=[notify], paths=paths,
                                    account=data["account"], cfg=cfg) if plist else []
            if plist:
                state["feed_fail_runs"] = int(json.loads(hb.read_text(encoding="utf-8")).get("feed_fail_runs", 0))
                latest = {}
                for date in by_date:
                    for p in plans.load_plans(tmp / "plans", dt.date.fromisoformat(date)):
                        latest[p.id] = p
                state["plans"] = {cloud_payload.state_key(salt, pid): p.state for pid, p in latest.items()}
                active = sum(1 for p in latest.values() if p.state not in plans.TERMINAL)

    today = now.date().isoformat()
    if now.hour >= REPORT_HOUR and state.get("report_date") != today:
        if send(f"☁️ クラウドの見張り：稼働中。見張り中の計画 {active}件"
                "（この報告が毎朝来ない日は、見張りが止まっている可能性があります）。" + plans.FOOTER):
            state["report_date"] = today

    if _dump(state) != before:  # 変化があるときだけ書く（コミットを増やさない）
        tmp_file = pathlib.Path(state_path).with_suffix(".tmp")
        tmp_file.write_text(_dump(state), encoding="utf-8")
        tmp_file.replace(state_path)
    return events


if __name__ == "__main__":
    print("run_cloud.py から呼ばれます", file=sys.stderr)
