"""朝の計画をクラウドに渡すための「荷物」の作成・読み取り（標準ライブラリのみ・通信しない）。

荷物は GitHub の暗号化 Secret（FX_PLANS）に入る。1行の base64 にして、実行ログで一部だけ
伏せ字が外れる事故を避ける。通知済みの状態は、計画IDを salt 付きハッシュにして公開リポジトリに残す
（価格・ペア・方向・IDは残さない）。注文・証券会社への接続はしない。
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json

MAX_CHARS = 40_000  # GitHub Secret の上限は 48KB。余裕を持たせる
VERSION = 1


def build_payload(plans: list, account: dict | None, cfg: dict, salt: str) -> str:
    raw = json.dumps({"v": VERSION, "salt": salt, "plans": [dataclasses.asdict(p) for p in plans],
                      "account": account, "cfg": cfg}, ensure_ascii=False, separators=(",", ":"))
    text = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    if len(text) > MAX_CHARS:
        raise ValueError(f"荷物が大きすぎます（{len(text)}文字 > {MAX_CHARS}）。メモを短くするか計画を減らしてください")
    return text


def parse_payload(text: str) -> dict:
    try:
        data = json.loads(base64.b64decode(text.strip(), validate=True).decode("utf-8"))
    except Exception as e:  # noqa: BLE001  中身は出さず、種類だけ伝える
        raise ValueError(f"荷物を読めません（{type(e).__name__}）") from None
    if not isinstance(data, dict) or not isinstance(data.get("plans"), list) or not data.get("salt"):
        raise ValueError("荷物の形が正しくありません")
    data.setdefault("account", None)
    data.setdefault("cfg", {})
    return data


def state_key(salt: str, plan_id: str) -> str:
    return hmac.new(salt.encode("utf-8"), plan_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
