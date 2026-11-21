"""エントリー前のポジションサイズ・リスク計算（完全ローカル。注文・外部通信は一切しない）。

口座通貨は円前提。決済通貨（ペアの後ろ側）が円でないペアは、
その通貨→円の換算レートを --quote-jpy-rate で受け取る（例: EURUSD なら USDJPY のレート）。

使い方:
  python3 scripts/risk_check.py --balance 1000000 --risk-pct 1 --pair USDJPY \
      --direction buy --entry 150.00 --sl 149.50 --tp 151.00
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field

DEFAULT_LOT_SIZE = 10_000   # 1ロットの通貨数（国内業者に多い1万通貨。業者に合わせて --lot-size で変更）
DEFAULT_MIN_UNIT = 1_000    # 発注できる最小単位（この単位で切り捨てる）
DEFAULT_MAX_RISK_PCT = 2.0  # これを超えるリスク許容率は警告


@dataclass
class RiskResult:
    pair: str
    direction: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    pip: float = 0.0
    sl_pips: float = 0.0
    tp_pips: float = 0.0
    risk_amount: float = 0.0
    loss_per_unit: float = 0.0
    units: int = 0
    lots: float = 0.0
    expected_loss: float = 0.0
    expected_profit: float = 0.0
    actual_risk_pct: float = 0.0
    rr: float = 0.0


def normalize_pair(pair: str) -> str:
    return pair.replace("/", "").replace("_", "").strip().upper()


def is_jpy_quote(pair: str) -> bool:
    return normalize_pair(pair).endswith("JPY")


def pip_size(pair: str) -> float:
    return 0.01 if is_jpy_quote(pair) else 0.0001


def _validate(r: RiskResult, balance, risk_pct, entry, sl, tp, direction,
              quote_jpy_rate, lot_size, min_unit) -> None:
    if balance <= 0:
        r.errors.append(f"口座残高は正の数で入力してください（入力: {balance}）")
    if risk_pct <= 0:
        r.errors.append(f"リスク許容率(%)は正の数で入力してください（入力: {risk_pct}）")
    for name, v in (("エントリー価格", entry), ("損切り価格", sl), ("利確価格", tp)):
        if v <= 0:
            r.errors.append(f"{name}は正の数で入力してください（入力: {v}）")
    if lot_size <= 0 or min_unit <= 0:
        r.errors.append("ロットの通貨数・最小単位は正の数で入力してください")
    if direction not in ("buy", "sell"):
        r.errors.append(f"売買方向は buy / sell のどちらかです（入力: {direction}）")
        return

    if sl == entry:
        r.errors.append("損切り価格がエントリー価格と同じです（損切り幅0では計算できません）")
    elif direction == "buy" and sl > entry:
        r.errors.append(f"損切りが逆側です：買いなのに損切り({sl})がエントリー({entry})より上")
    elif direction == "sell" and sl < entry:
        r.errors.append(f"損切りが逆側です：売りなのに損切り({sl})がエントリー({entry})より下")

    if tp == entry:
        r.errors.append("利確価格がエントリー価格と同じです")
    elif direction == "buy" and tp < entry:
        r.errors.append(f"利確が逆側です：買いなのに利確({tp})がエントリー({entry})より下")
    elif direction == "sell" and tp > entry:
        r.errors.append(f"利確が逆側です：売りなのに利確({tp})がエントリー({entry})より上")

    if is_jpy_quote(r.pair):
        if entry > 0 and entry < 10:
            r.warnings.append(f"価格の桁が通貨ペアと合っていない可能性：{r.pair} で価格 {entry}")
        if quote_jpy_rate is not None:
            r.warnings.append("円が決済通貨のペアなので --quote-jpy-rate は使いません（無視しました）")
    else:
        if quote_jpy_rate is None:
            quote = normalize_pair(r.pair)[3:] or "決済通貨"
            r.errors.append(
                f"{r.pair} は円以外が決済通貨のため、{quote}→円の換算レートが必要です"
                f"（--quote-jpy-rate、例: {quote}JPY の現在値）"
            )
        elif quote_jpy_rate <= 0:
            r.errors.append(f"換算レートは正の数で入力してください（入力: {quote_jpy_rate}）")


def compute(
    balance: float,
    risk_pct: float,
    entry: float,
    sl: float,
    tp: float,
    pair: str,
    direction: str,
    quote_jpy_rate: float | None = None,
    lot_size: int = DEFAULT_LOT_SIZE,
    min_unit: int = DEFAULT_MIN_UNIT,
    max_risk_pct: float = DEFAULT_MAX_RISK_PCT,
) -> RiskResult:
    pair = normalize_pair(pair)
    direction = direction.strip().lower()
    r = RiskResult(pair=pair, direction=direction)
    _validate(r, balance, risk_pct, entry, sl, tp, direction, quote_jpy_rate, lot_size, min_unit)
    if risk_pct > max_risk_pct:
        r.warnings.append(f"リスク許容率 {risk_pct}% が上限 {max_risk_pct}% を超過しています")
    if r.errors:
        return r

    conv = 1.0 if is_jpy_quote(pair) else float(quote_jpy_rate)
    r.pip = pip_size(pair)
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    r.sl_pips = round(sl_dist / r.pip, 1)
    r.tp_pips = round(tp_dist / r.pip, 1)
    r.rr = tp_dist / sl_dist

    r.risk_amount = balance * risk_pct / 100
    r.loss_per_unit = sl_dist * conv
    units_raw = r.risk_amount / r.loss_per_unit
    # 浮動小数の誤差で 20000 が 19999.999… になり1単位落ちるのを防ぐ
    r.units = int(math.floor(units_raw / min_unit + 1e-9)) * min_unit
    r.lots = r.units / lot_size
    r.expected_loss = r.units * r.loss_per_unit
    r.expected_profit = r.units * tp_dist * conv
    r.actual_risk_pct = r.expected_loss / balance * 100

    conv_note = "" if conv == 1.0 else f" × 換算レート {conv:g}"
    r.steps = [
        f"pip単位: {r.pip:g}（{'円が決済通貨' if conv == 1.0 else '円以外が決済通貨'}）",
        f"損切り幅: |{entry:g} - {sl:g}| = {sl_dist:.5g} → {r.sl_pips:g} pips",
        f"利確幅:   |{tp:g} - {entry:g}| = {tp_dist:.5g} → {r.tp_pips:g} pips",
        f"許容損失額: {balance:,.0f}円 × {risk_pct:g}% = {r.risk_amount:,.0f}円",
        f"1通貨あたり損失: {sl_dist:.5g}{conv_note} = {r.loss_per_unit:.5g}円",
        f"理論通貨数: {r.risk_amount:,.0f} ÷ {r.loss_per_unit:.5g} = {units_raw:,.1f}通貨",
        f"最小単位 {min_unit:,}通貨で切り捨て: {r.units:,}通貨 = {r.lots:g}ロット（1ロット={lot_size:,}通貨）",
        f"想定損失額: {r.units:,} × {r.loss_per_unit:.5g} = {r.expected_loss:,.0f}円（残高の {r.actual_risk_pct:.2f}%）",
        f"想定利益額: {r.units:,} × {tp_dist:.5g}{conv_note} = {r.expected_profit:,.0f}円",
        f"RR: {tp_dist:.5g} ÷ {sl_dist:.5g} = {r.rr:.2f}",
    ]

    if r.units == 0:
        min_loss = min_unit * r.loss_per_unit
        r.warnings.append(
            f"最小単位 {min_unit:,}通貨でも想定損失 {min_loss:,.0f}円（{min_loss / balance * 100:.2f}%）となり、"
            f"許容損失 {r.risk_amount:,.0f}円を超えます。この損切り幅では許容率内に収まりません"
        )
    if not is_jpy_quote(pair):
        r.warnings.append("非円ペアの損益は換算レートが変わると増減します（入力時点のレートで概算）")
    return r


def format_result(r: RiskResult) -> str:
    lines = [f"■ {r.pair} {r.direction}"]
    if r.errors:
        lines.append("入力エラー（計算していません）:")
        lines += [f"  ✖ {e}" for e in r.errors]
        if r.warnings:
            lines += [f"  ⚠ {w}" for w in r.warnings]
        return "\n".join(lines)
    lines += [
        f"ポジションサイズ: {r.units:,}通貨（{r.lots:g}ロット）",
        f"想定損失額: {r.expected_loss:,.0f}円（残高の {r.actual_risk_pct:.2f}%）",
        f"想定利益額: {r.expected_profit:,.0f}円",
        f"RR: {r.rr:.2f}（損切り {r.sl_pips:g} pips / 利確 {r.tp_pips:g} pips）",
        "",
        "計算過程:",
    ]
    lines += [f"  {i}. {s}" for i, s in enumerate(r.steps, 1)]
    if r.warnings:
        lines.append("")
        lines.append("警告:")
        lines += [f"  ⚠ {w}" for w in r.warnings]
    lines.append("")
    lines.append("※ 計算結果のみ。注文は行いません。将来の損益を保証するものではありません。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="エントリー前のポジションサイズ・リスク計算（注文はしない）")
    p.add_argument("--balance", type=float, required=True, help="口座残高（円）")
    p.add_argument("--risk-pct", type=float, required=True, help="1トレードのリスク許容率(%%)")
    p.add_argument("--pair", required=True, help="通貨ペア 例: USDJPY / EURUSD")
    p.add_argument("--direction", required=True, choices=["buy", "sell"], help="売買方向")
    p.add_argument("--entry", type=float, required=True, help="エントリー価格")
    p.add_argument("--sl", type=float, required=True, help="損切り価格")
    p.add_argument("--tp", type=float, required=True, help="利確価格")
    p.add_argument("--quote-jpy-rate", type=float, default=None,
                   help="決済通貨→円の換算レート（非円ペアのみ必須。EURUSDならUSDJPYの値）")
    p.add_argument("--lot-size", type=int, default=DEFAULT_LOT_SIZE, help="1ロットの通貨数（既定 10000）")
    p.add_argument("--min-unit", type=int, default=DEFAULT_MIN_UNIT, help="最小取引単位（既定 1000）")
    p.add_argument("--max-risk-pct", type=float, default=DEFAULT_MAX_RISK_PCT,
                   help="これを超えるリスク許容率を警告（既定 2.0）")
    a = p.parse_args(argv)
    r = compute(
        balance=a.balance, risk_pct=a.risk_pct, entry=a.entry, sl=a.sl, tp=a.tp,
        pair=a.pair, direction=a.direction, quote_jpy_rate=a.quote_jpy_rate,
        lot_size=a.lot_size, min_unit=a.min_unit, max_risk_pct=a.max_risk_pct,
    )
    print(format_result(r))
    return 2 if r.errors else 0


if __name__ == "__main__":
    sys.exit(main())
