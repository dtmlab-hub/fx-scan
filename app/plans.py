"""朝の計画（TradingView での手動分析の結果）の取り込み・保存・状態判定。

標準ライブラリだけで動く（通信しない・注文しない）。価格の取得と通知は app/plan_watch.py の担当。
相場の判断はしない。ユーザーが書いた価格をそのまま使い、ルールとの照合と計算だけを行う。

使い方:
  python3 -m app.plans parse --stdin [--date YYYY-MM-DD] [--dry-run]   朝の文を読み取って確認表を出す
  python3 -m app.plans save [--date YYYY-MM-DD]                         確認した計画を保存（見張り開始）
  python3 -m app.plans status                                            見張りの心拍と計画の状態
  python3 -m app.plans cancel <計画ID>                                    計画を取り消す
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import pathlib
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import risk_check  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")
PLANS_DIR = ROOT / "data" / "plans"
PENDING = PLANS_DIR / ".pending.json"
HEARTBEAT = ROOT / "data" / "watch_heartbeat.json"
ACCOUNT = ROOT / "data" / "account.json"
CONFIG = ROOT / "plan_watch.json"

FOOTER = "記録とルールからの計算です。売買の判断はご自身で"
TERMINAL = {"triggered", "chased", "invalidated", "expired", "cancelled"}
STATE_JA = {"waiting": "待ち", "zone": "ゾーン到達", "triggered": "条件到達", "chased": "後追いで見送り",
            "invalidated": "無効", "expired": "期限切れ", "cancelled": "取消"}
DIR_JA = {"buy": "買い", "sell": "売り"}

PAIR_ALIASES = [  # 長い名前から先に照合する
    ("ユーロドル", "EURUSD"), ("ポンドドル", "GBPUSD"), ("オージードル", "AUDUSD"), ("豪ドル", "AUDUSD"),
    ("ポンド円", "GBPJPY"), ("ユーロ円", "EURJPY"), ("ドル円", "USDJPY"),
]
DIR_WORDS = [("ロング", "buy"), ("ショート", "sell"), ("買い", "buy"), ("売り", "sell"), ("買", "buy"), ("売", "sell")]
DIR_WORDS_EN = {"buy": "buy", "long": "buy", "sell": "sell", "short": "sell"}
KEYS = [  # (先頭の語, 項目)
    ("ゾーン", "zone"), ("押し目", "zone"), ("戻り目", "zone"), ("zone", "zone"),
    ("トリガー", "trigger"), ("エントリー", "trigger"), ("trigger", "trigger"), ("entry", "trigger"),
    ("損切り", "sl"), ("損切", "sl"), ("ストップ", "sl"), ("stop", "sl"), ("sl", "sl"),
    ("利確", "tp"), ("利食い", "tp"), ("ターゲット", "tp"), ("target", "tp"), ("tp", "tp"),
    ("無効化", "invalid"), ("無効", "invalid"), ("invalid", "invalid"),
    ("期限", "expires"), ("expires", "expires"), ("expire", "expires"),
    ("メモ", "memo"), ("memo", "memo"),
    ("換算", "rate"),
]
NUM = re.compile(r"\d+(?:\.\d+)?")
TIMEFRAME = re.compile(r"(?<![A-Za-z0-9.])[MHDWmhdw]\d{1,3}")


class PlanError(Exception):
    def __init__(self, messages: list[str]):
        self.messages = messages
        super().__init__("\n".join(messages))


@dataclass
class Plan:
    id: str
    date: str
    pair: str
    direction: str
    trigger: float
    sl: float
    tp: float
    invalid: float
    expires: str
    created_at: str
    zone_low: float | None = None
    zone_high: float | None = None
    memo: str = ""
    quote_jpy_rate: float | None = None
    state: str = "waiting"
    updated_at: str = ""
    excluded: list[str] = field(default_factory=list)  # ルール外の理由（保存しない）

    @property
    def rr(self) -> float:
        return abs(self.tp - self.trigger) / abs(self.trigger - self.sl)


@dataclass
class Quote:
    price: float          # 現在値（最新）
    bar_close: float      # 直近で確定した M15 足の終値
    bar_end: dt.datetime  # その足が確定した時刻（JST）


# ---------------------------------------------------------------------
# 設定・口座
def load_config(path: pathlib.Path = CONFIG) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


def load_account(path: pathlib.Path = ACCOUNT) -> dict | None:
    if not path.exists():
        return None
    acc = json.loads(path.read_text(encoding="utf-8"))
    if not acc.get("balance_jpy") or not acc.get("risk_pct"):
        return None
    return acc


def rules_pairs(path: pathlib.Path = ROOT / "rules.yaml") -> list[str]:
    """rules.yaml の pairs: 一覧だけを読む（読み取り専用・PyYAML なしで動かすため簡易に読む）。"""
    pairs, inside = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(r"^pairs:\s*$", line):
            inside = True
            continue
        if inside:
            m = re.match(r"^\s+-\s*([A-Za-z/]+)", line)
            if m:
                pairs.append(m.group(1).replace("/", "").upper())
            elif line.strip() and not line.startswith((" ", "#")):
                break
    return pairs


def decimals(pair: str) -> int:
    return 3 if pair.endswith("JPY") else 5


def fmt(pair: str, v: float) -> str:
    return f"{v:.{decimals(pair)}f}"


# ---------------------------------------------------------------------
# 取り込み
def _key_of(line: str) -> tuple[str | None, str]:
    low = line.lower()
    for word, key in KEYS:
        if low.startswith(word.lower()):
            rest = line[len(word):]
            if key in ("sl", "tp") and word.isascii() and rest[:1].isalpha():
                continue  # "slow" 等の誤検出を避ける
            return key, rest.strip(" :：\t")
    return None, line


def _find_pair(line: str) -> str | None:
    for word, pair in PAIR_ALIASES:
        if word in line:
            return pair
    m = re.search(r"(?<![A-Z])([A-Z]{3})/?([A-Z]{3})(?![A-Z])", line.upper())
    return m.group(1) + m.group(2) if m else None


def _find_direction(line: str) -> str | None:
    for word, d in DIR_WORDS:
        if word in line:
            return d
    for w in re.findall(r"[a-z]+", line.lower()):
        if w in DIR_WORDS_EN:
            return DIR_WORDS_EN[w]
    return None


def _numbers(text: str) -> list[float]:
    return [float(x) for x in NUM.findall(TIMEFRAME.sub(" ", text))]


def _parse_expiry(text: str, date: dt.date) -> dt.datetime | None:
    t = text.strip()
    if not t or t.startswith("今日"):
        d, hm = date, (23, 59)
    elif t.startswith("明日"):
        d, hm = date + dt.timedelta(days=1), (23, 59)
    else:
        m = re.search(r"(?:(\d{4})-(\d{1,2})-(\d{1,2})|(\d{1,2})/(\d{1,2}))?\s*(\d{1,2}):(\d{2})", t)
        if not m:
            return None
        if m.group(1):
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        elif m.group(4):
            d = dt.date(date.year, int(m.group(4)), int(m.group(5)))
        else:
            d = date
        hm = (int(m.group(6)), int(m.group(7)))
    return dt.datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=JST)


def _split_blocks(text: str) -> list[list[str]]:
    blocks, cur, cur_has_pair = [], [], False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if cur:
                blocks.append(cur)
            cur, cur_has_pair = [], False
            continue
        key, _ = _key_of(line)
        if key is None and _find_pair(line):
            if cur_has_pair:
                blocks.append(cur)
                cur = []
            cur_has_pair = True
        cur.append(line)
    if cur:
        blocks.append(cur)
    return blocks


def parse_text(text: str, date: dt.date, pairs: list[str], now: dt.datetime,
               start_seq: int = 1) -> list[Plan]:
    """朝の文 → 計画の一覧。問題があれば PlanError（全ブロック分のメッセージ）。"""
    text = unicodedata.normalize("NFKC", text)
    errors: list[str] = []
    out: list[Plan] = []
    for bi, lines in enumerate(_split_blocks(text), 1):
        vals: dict = {"memo": []}
        pair = direction = None
        for line in lines:
            key, rest = _key_of(line)
            if key is None:
                pair = pair or _find_pair(line)
                direction = direction or _find_direction(line)
                continue
            if key == "memo":
                vals["memo"].append(rest)
            elif key == "expires":
                vals["expires"] = rest
            else:
                nums = _numbers(rest)
                if nums:
                    vals[key] = nums
        tag = f"ブロック{bi}" + (f"（{pair}）" if pair else "")
        errs = []
        if not pair:
            errs.append(f"{tag}: 通貨ペアが見つかりません（例 USDJPY / ドル円）")
        elif pair not in pairs:
            errs.append(f"{tag}: ペア {pair} は rules.yaml の監視ペア（{'・'.join(pairs)}）にありません")
        if not direction:
            errs.append(f"{tag}: 方向（買い/売り）が見つかりません")
        for key, ja in (("trigger", "トリガー"), ("sl", "損切り"), ("tp", "利確")):
            if key not in vals:
                errs.append(f"{tag}: {ja}の価格がありません")
        expires = _parse_expiry(vals.get("expires", ""), date)
        if expires is None:
            errs.append(f"{tag}: 期限「{vals.get('expires')}」を読めません（例 今日 / 明日 / 21:30）")
        elif expires <= now:
            errs.append(f"{tag}: 期限 {expires:%m/%d %H:%M} が既に過ぎています")
        if errs:
            errors += errs
            continue

        trigger, sl, tp = vals["trigger"][0], vals["sl"][0], vals["tp"][0]
        invalid = vals["invalid"][0] if "invalid" in vals else sl
        s = 1 if direction == "buy" else -1
        side_up, side_dn = ("上", "下") if s == 1 else ("下", "上")
        dj = DIR_JA[direction]
        if s * (trigger - sl) <= 0:
            errs.append(f"{tag}: 損切り {sl:g} が{dj}のトリガー {trigger:g} より{side_up}にあります（{side_dn}に置く）")
        if s * (tp - trigger) <= 0:
            errs.append(f"{tag}: 利確 {tp:g} が{dj}のトリガー {trigger:g} より{side_dn}にあります（{side_up}に置く）")
        if s * (trigger - invalid) <= 0:
            errs.append(f"{tag}: 無効化 {invalid:g} が{dj}のトリガー {trigger:g} より{side_up}にあります")
        zone = vals.get("zone")
        if zone is not None and len(zone) < 2:
            errs.append(f"{tag}: ゾーンは「151.20-151.50」のように2つの価格で書いてください")
        if errs:
            errors += errs
            continue
        out.append(Plan(
            id=f"{date:%Y%m%d}-{start_seq + len(out)}", date=date.isoformat(), pair=pair, direction=direction,
            trigger=trigger, sl=sl, tp=tp, invalid=invalid, expires=expires.isoformat(),
            created_at=now.isoformat(), zone_low=min(zone[:2]) if zone else None,
            zone_high=max(zone[:2]) if zone else None, memo=" / ".join(vals["memo"]),
            quote_jpy_rate=vals["rate"][0] if "rate" in vals else None, updated_at=now.isoformat(),
        ))
    if errors:
        raise PlanError(errors)
    if not out:
        raise PlanError(["計画が1つも見つかりません（ペア・方向・トリガー・損切り・利確を書いてください）"])
    return out


# ---------------------------------------------------------------------
# リスク・ルール照合
def quote_currency_jpy_pair(pair: str) -> str:
    """非円ペアの損益を円にするためのレート（例 EURUSD → USDJPY）。"""
    return pair[3:] + "JPY"


def plan_risk(plan: Plan, account: dict | None, quote_jpy_rate: float | None = None,
              entry: float | None = None, cfg: dict | None = None):
    if not account:
        return None
    rate = quote_jpy_rate or plan.quote_jpy_rate
    if not risk_check.is_jpy_quote(plan.pair) and not rate:
        return None
    cfg = cfg or load_config()
    return risk_check.compute(
        balance=float(account["balance_jpy"]), risk_pct=float(account["risk_pct"]),
        entry=plan.trigger if entry is None else entry, sl=plan.sl, tp=plan.tp, pair=plan.pair,
        direction=plan.direction, quote_jpy_rate=rate, lot_size=int(cfg["lot_size"]),
        min_unit=int(cfg["min_unit"]), max_risk_pct=float(cfg["max_risk_pct"]),
    )


def rule_problems(plan: Plan, account: dict | None, cfg: dict) -> list[str]:
    """見張りに回さない理由（空なら OK）。数字は計算から出す。"""
    out = []
    if plan.rr < cfg["min_rr"]:
        out.append(f"RR {plan.rr:.2f} が下限 {cfg['min_rr']:g} 未満")
    if not account:
        out.append("残高・リスク%が未設定（data/account.json）のためロットを計算できません")
        return out
    if float(account["risk_pct"]) > cfg["max_risk_pct"]:
        out.append(f"リスク {float(account['risk_pct']):g}% が上限 {cfg['max_risk_pct']:g}% を超えています")
    r = plan_risk(plan, account, cfg=cfg)
    if r is not None:
        out += [f"入力エラー: {e}" for e in r.errors]
        if not r.errors and r.units == 0:
            out.append("最小単位でも許容損失を超えます（損切り幅が広すぎる）")
    return out


# ---------------------------------------------------------------------
# 状態判定（純粋関数）
def evaluate(plan: Plan, q: Quote, now: dt.datetime, cfg: dict) -> tuple[str, str | None]:
    """(新しい状態, 通知すべき出来事 or None) を返す。状態が変わらなければ出来事は None。"""
    if plan.state in TERMINAL:
        return plan.state, None
    if now >= dt.datetime.fromisoformat(plan.expires):
        return "expired", "expired"
    s = 1 if plan.direction == "buy" else -1
    if s * (q.price - plan.invalid) <= 0:
        return "invalidated", "invalidated"
    bar_is_new = q.bar_end > dt.datetime.fromisoformat(plan.created_at)
    if bar_is_new and s * (q.bar_close - plan.invalid) <= 0:
        return "invalidated", "invalidated"
    if bar_is_new and s * (q.bar_close - plan.trigger) >= 0:
        dist = s * (q.bar_close - plan.trigger)
        limit = cfg["chase_ratio"] * abs(plan.trigger - plan.sl)
        return ("triggered", "triggered") if dist <= limit + 1e-12 else ("chased", "chased")
    if plan.state == "waiting" and plan.zone_low is not None and plan.zone_low <= q.price <= plan.zone_high:
        return "zone", "zone"
    return plan.state, None


# ---------------------------------------------------------------------
# 通知文
def format_event(plan: Plan, event: str, q: Quote, account: dict | None,
                 quote_jpy_rate: float | None = None, cfg: dict | None = None) -> tuple[str, str]:
    p, dj = plan.pair, DIR_JA[plan.direction]
    title = {
        "zone": f"🟡 準備 {p} {dj} ゾーン到達",
        "triggered": f"🟢 条件到達 {p} {dj}",
        "chased": f"⚪ 後追い {p} {dj}（見送り扱い）",
        "invalidated": f"⚫ 無効 {p} {dj}",
        "expired": f"期限切れ {p} {dj}",
    }[event]
    lines = []
    if event == "zone":
        lines.append(f"現在値 {fmt(p, q.price)}（ゾーン {fmt(p, plan.zone_low)}〜{fmt(p, plan.zone_high)}）")
        lines.append(f"トリガー {fmt(p, plan.trigger)} の M15 確定足を待っています")
    elif event in ("triggered", "chased"):
        s = 1 if plan.direction == "buy" else -1
        dist = s * (q.bar_close - plan.trigger)
        lines.append(f"M15 {q.bar_end:%m/%d %H:%M} 確定 終値 {fmt(p, q.bar_close)}"
                     f"（トリガー {fmt(p, plan.trigger)} から {dist:.{decimals(p)}f}）")
        rr = abs(plan.tp - q.bar_close) / abs(q.bar_close - plan.sl)
        lines.append(f"SL {fmt(p, plan.sl)} / TP {fmt(p, plan.tp)}（終値基準の RR {rr:.2f}）")
        r = plan_risk(plan, account, quote_jpy_rate=quote_jpy_rate, entry=q.bar_close, cfg=cfg)
        if r is None:
            lines.append("ロット：未計算（残高未設定、または換算レートなし）")
        elif r.errors:
            lines.append("ロット：未計算（" + "、".join(r.errors) + "）")
        else:
            lines.append(f"ロット {r.lots:g}（{r.units:,}通貨）/ 最大損失 {r.expected_loss:,.0f}円"
                         f"（残高の {r.actual_risk_pct:.2f}%）")
        if event == "triggered":
            lines.append("入る前に：上位足一致・ゾーン内・転換・抜け確定・SL構造・根拠2つ以上・後追いでない・指標の時刻")
            lines.append("記録は http://127.0.0.1:8787 のホームから1クリック")
        else:
            lines.append("後追い上限を超えて抜けたため、ルール上は見送り扱いです")
        lines.append("価格は Yahoo の参考値（証券会社のレートと数pipsずれることがあります）")
    elif event == "invalidated":
        lines.append(f"現在値 {fmt(p, q.price)} が無効化ライン {fmt(p, plan.invalid)} を越えました。この計画は終了")
    if plan.memo:
        lines.append(f"メモ：{plan.memo}")
    lines.append(FOOTER)
    return title, "\n".join(lines)


def tradingview_alerts(plans: list[Plan], limit: int = 3) -> list[str]:
    out = []
    for p in plans:
        if p.state in TERMINAL or p.excluded:
            continue
        side = "上抜け" if p.direction == "buy" else "下抜け"
        out.append(f"{p.pair} {fmt(p.pair, p.trigger)} を{side}（価格アラート「交差」・{DIR_JA[p.direction]}の計画 {p.id}）")
    return out[:limit]


# ---------------------------------------------------------------------
# 保存
def _file(d: pathlib.Path, date: dt.date) -> pathlib.Path:
    return pathlib.Path(d) / f"{date.isoformat()}.json"


def _from_dict(x: dict) -> Plan:
    names = {f.name for f in dataclasses.fields(Plan)}
    return Plan(**{k: v for k, v in x.items() if k in names})


def load_plans(d: pathlib.Path, date: dt.date) -> list[Plan]:
    f = _file(d, date)
    if not f.exists():
        return []
    return [_from_dict(x) for x in json.loads(f.read_text(encoding="utf-8"))["plans"]]


def save_plans(d: pathlib.Path, date: dt.date, new: list[Plan]) -> pathlib.Path:
    d = pathlib.Path(d)
    d.mkdir(parents=True, exist_ok=True)
    cur = {p.id: p for p in load_plans(d, date)}
    for p in new:
        cur[p.id] = p
    f = _file(d, date)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"date": date.isoformat(), "plans": [dataclasses.asdict(p) for p in cur.values()]},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)
    return f


def next_seq(d: pathlib.Path, date: dt.date) -> int:
    seqs = [int(p.id.rsplit("-", 1)[1]) for p in load_plans(d, date)]
    return max(seqs, default=0) + 1


def cancel_plan(d: pathlib.Path, date: dt.date, plan_id: str, now: dt.datetime | None = None) -> bool:
    ps = load_plans(d, date)
    for p in ps:
        if p.id == plan_id and p.state not in TERMINAL:
            p.state = "cancelled"
            p.updated_at = (now or dt.datetime.now(JST)).isoformat()
            save_plans(d, date, [p])
            return True
    return False


def heartbeat_age_minutes(path: pathlib.Path, now: dt.datetime) -> int | None:
    if not pathlib.Path(path).exists():
        return None
    last = dt.datetime.fromisoformat(json.loads(pathlib.Path(path).read_text(encoding="utf-8"))["last_run"])
    return int((now - last).total_seconds() // 60)


# ---------------------------------------------------------------------
# CLI
def _plan_line(p: Plan, account: dict | None, cfg: dict) -> str:
    r = plan_risk(p, account, cfg=cfg)
    if r is None:
        lot = "ロット未計算（残高未設定 / 非円ペアは通知時に換算）"
    elif r.errors:
        lot = "ロット未計算（" + "、".join(r.errors) + "）"
    else:
        lot = f"{r.lots:g}ロット（{r.units:,}通貨）最大損失 {r.expected_loss:,.0f}円"
    zone = f" ゾーン {fmt(p.pair, p.zone_low)}〜{fmt(p.pair, p.zone_high)}" if p.zone_low is not None else ""
    return (f"{p.id} {p.pair} {DIR_JA[p.direction]}{zone} トリガー {fmt(p.pair, p.trigger)} "
            f"SL {fmt(p.pair, p.sl)} TP {fmt(p.pair, p.tp)} 無効 {fmt(p.pair, p.invalid)} "
            f"RR {p.rr:.2f} {lot} 期限 {dt.datetime.fromisoformat(p.expires):%m/%d %H:%M}")


def status_problem(hb_path: pathlib.Path, now: dt.datetime, cfg: dict) -> str | None:
    """見張りの異常を1行で返す（正常なら None）。止まっている／価格が取れていない、の2種類。"""
    age = heartbeat_age_minutes(hb_path, now)
    if age is None:
        return "🔴 見張りはまだ一度も動いていません（launchd 未登録）"
    if age >= cfg["stale_minutes"]:
        return f"🔴 見張りが {age} 分止まっています（Mac のスリープ・電源・launchd を確認）"
    hb = json.loads(pathlib.Path(hb_path).read_text(encoding="utf-8"))
    if hb.get("feed_errors"):
        return (f"🔴 Yahoo から価格が取れていません（{hb.get('feed_fail_runs', 1)}回連続："
                + "、".join(hb["feed_errors"]) + "）。その間は TradingView のアラートで確認")
    return None


def _stale_line(now: dt.datetime, cfg: dict) -> str | None:
    return status_problem(HEARTBEAT, now, cfg)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="朝の計画の取り込み・状態確認（注文はしない）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("parse")
    a.add_argument("--date")
    a.add_argument("--stdin", action="store_true")
    a.add_argument("--file")
    a.add_argument("--dry-run", action="store_true", help="確認表だけ出し、保存待ちファイルを作らない")
    b = sub.add_parser("save")
    b.add_argument("--date")
    sub.add_parser("status")
    c = sub.add_parser("cancel")
    c.add_argument("plan_id")
    args = ap.parse_args(argv)

    now = dt.datetime.now(JST)
    cfg = load_config()
    account = load_account()

    if args.cmd == "parse":
        date = dt.date.fromisoformat(args.date) if args.date else now.date()
        text = sys.stdin.read() if args.stdin or not args.file else pathlib.Path(args.file).read_text(encoding="utf-8")
        try:
            ps = parse_text(text, date, rules_pairs(), now=now, start_seq=next_seq(PLANS_DIR, date))
        except PlanError as e:
            print("読み取りエラー（保存していません）:", file=sys.stderr)
            for m in e.messages:
                print(f"  ✖ {m}", file=sys.stderr)
            return 2
        stale = _stale_line(now, cfg)
        if stale:
            print(stale)
        for p in ps:
            p.excluded = rule_problems(p, account, cfg)
            mark = "✖ 見張らない" if p.excluded else "○ 見張る"
            print(f"{mark} {_plan_line(p, account, cfg)}")
            for x in p.excluded:
                print(f"    理由: {x}")
        if cfg.get("tradingview_alerts"):
            tv = tradingview_alerts(ps)
            if tv:
                print("TradingView 無料版に手で設定する価格アラート（予備・最大3本）:")
                for x in tv:
                    print(f"  - {x}")
        if not args.dry_run:
            PLANS_DIR.mkdir(parents=True, exist_ok=True)
            PENDING.write_text(json.dumps({"date": date.isoformat(), "plans": [dataclasses.asdict(p) for p in ps]},
                                          ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"保存待ち: {PENDING.relative_to(ROOT)}（save で見張り開始）")
        print(f"※ {FOOTER}")
        return 0

    if args.cmd == "save":
        if not PENDING.exists():
            print("保存待ちの計画がありません（先に parse）", file=sys.stderr)
            return 2
        data = json.loads(PENDING.read_text(encoding="utf-8"))
        date = dt.date.fromisoformat(args.date or data["date"])
        ps = [_from_dict(x) for x in data["plans"]]
        ok = [p for p in ps if not p.excluded]
        active = sum(1 for p in load_plans(PLANS_DIR, date) if p.state not in TERMINAL)
        if active + len(ok) > cfg["max_active"]:
            print(f"同時に見張れる計画は {cfg['max_active']} 件までです（現在 {active} 件 + 追加 {len(ok)} 件）",
                  file=sys.stderr)
            return 2
        f = save_plans(PLANS_DIR, date, ok)
        PENDING.unlink()
        print(f"保存: {f.relative_to(ROOT)}（見張る {len(ok)} 件 / 見張らない {len(ps) - len(ok)} 件）")
        for p in ok:
            side = "以上" if p.direction == "buy" else "以下"
            print(f"  {p.id} {p.pair}: M15 確定足の終値が {fmt(p.pair, p.trigger)} {side}で通知")
        return 0

    if args.cmd == "status":
        stale = _stale_line(now, cfg)
        print(stale or f"🟢 見張りは {heartbeat_age_minutes(HEARTBEAT, now)} 分前に動きました")
        rows = []
        for i in range(cfg["lookback_days"]):
            date = now.date() - dt.timedelta(days=i)
            rows += [p for p in load_plans(PLANS_DIR, date)
                     if i == 0 or p.state not in TERMINAL]
        if not rows:
            print("計画はありません（/fx-morning で朝の分析を貼る）")
        for p in rows:
            print(f"[{STATE_JA[p.state]}] {_plan_line(p, account, cfg)}")
        return 0

    if args.cmd == "cancel":
        date = dt.date(int(args.plan_id[:4]), int(args.plan_id[4:6]), int(args.plan_id[6:8]))
        if cancel_plan(PLANS_DIR, date, args.plan_id, now):
            print(f"取消: {args.plan_id}")
            return 0
        print(f"取り消せる計画 {args.plan_id} がありません（既に終了済み・ID違い）", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
