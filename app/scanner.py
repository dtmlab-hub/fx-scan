"""波動エントリー候補の判定エンジン（v1：判定のみ。通知は送らない）。

狙い：H4 の第1波 → 第2波の押し目（Fib 38.2〜61.8%）→ M15 の転換 → ネックライン抜け。
判定の数値は scanner.yaml から読み、このファイルには書かない。

売りは価格の符号を反転させて買いと同じロジックで判定する
（高値と安値を入れ替えて -1 倍すれば、下降トレンドは上昇トレンドになる）。
こうすることで買いと売りの判定が必ず左右対称になる。

未来の足は一切見ない：H4・M15 とも、その時点で確定している足と山谷だけを使う。
"""
from __future__ import annotations

import bisect
import pathlib
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "scanner.yaml"

M15 = pd.Timedelta(minutes=15)
H4 = pd.Timedelta(hours=4)


@dataclass
class Signal:
    pair: str
    direction: str  # buy | sell
    time: pd.Timestamp  # シグナル足の確定時刻（JST）
    entry: float
    sl: float
    tp: float  # 本命の利確（第3波目標）。RR と過去検証の勝ち負けはこれで測る
    tp_half: float  # 第1波の高値＝半分利確の目安
    rr: float
    retrace: float  # 第2波の戻し率（0.5 = 50%）
    pattern: str
    neckline: float
    wave1_start: float
    wave1_top: float
    wave2_low: float
    reasons: list[str] = field(default_factory=list)
    # 以下はチャート描画・過去検証用（M15 の行番号）
    bar_index: int = 0
    peak_index: int = 0
    low1_index: int = 0
    low2_index: int = 0

    @property
    def score(self) -> int:
        return len(self.reasons)


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def pip_size(pair: str) -> float:
    return 0.01 if pair.endswith("JPY") else 0.0001


# ---------------------------------------------------------------------
# データ取得（Yahoo Finance。無料・APIキー不要）
# ---------------------------------------------------------------------
def _clean(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].dropna()
    # 最後の1本は形成途中の可能性があるので捨てる
    return df.iloc[:-1]


def fetch(pair: str, cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(M15, H4) を返す。H4 は1時間足から作る。"""
    import yfinance as yf

    ticker = f"{pair}=X"
    m15 = _clean(yf.download(ticker, interval="15m", period=cfg["data"]["m15_period"],
                             progress=False, auto_adjust=False))
    h1 = _clean(yf.download(ticker, interval="1h", period=cfg["data"]["h1_period"],
                            progress=False, auto_adjust=False))
    h4 = h1.resample("4h", label="left", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    ).dropna()
    return m15, h4


# ---------------------------------------------------------------------
# 部品
# ---------------------------------------------------------------------
def pivots(high: np.ndarray, low: np.ndarray, left: int, right: int) -> tuple[list[int], list[int]]:
    """山（pivot high）と谷（pivot low）の行番号。i + right 本目が確定した時点で判明する。"""
    ph, pl = [], []
    for i in range(left, len(high) - right):
        if high[i] >= high[i - left:i].max() and high[i] > high[i + 1:i + right + 1].max():
            ph.append(i)
        if low[i] <= low[i - left:i].min() and low[i] < low[i + 1:i + right + 1].min():
            pl.append(i)
    return ph, pl


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, length: int) -> np.ndarray:
    prev = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low, np.maximum(abs(high - prev), abs(low - prev)))
    return pd.Series(tr).rolling(length, min_periods=1).mean().to_numpy()


def _last_before(idxs: list[int], limit: int) -> int | None:
    """idxs（昇順）のうち limit より小さい最後の値。"""
    k = bisect.bisect_left(idxs, limit)
    return idxs[k - 1] if k > 0 else None


# ---------------------------------------------------------------------
# 判定本体（買い方向のみ。売りは呼び出し側で符号反転して渡す）
# ---------------------------------------------------------------------
def _scan_long(m: dict, h: dict, pair: str, cfg: dict, sign: float) -> list[dict]:
    o_h, o_l, o_c = m["high"], m["low"], m["close"]
    H_h, H_l = h["high"], h["low"]
    w2, rv, en, sc = cfg["wave2"], cfg["reversal"], cfg["entry"], cfg["score"]
    r4, r15 = cfg["htf"]["pivot_right"], cfg["ltf"]["pivot_right"]

    a15 = atr(o_h, o_l, o_c, cfg["ltf"]["atr_len"])
    a4 = atr(H_h, H_l, h["close"], cfg["htf"]["atr_len"])
    ph4, pl4 = pivots(H_h, H_l, cfg["htf"]["pivot_left"], r4)
    _, pl15 = pivots(o_h, o_l, cfg["ltf"]["pivot_left"], r15)
    ph4_conf = [i + r4 for i in ph4]  # 確定する H4 行番号
    pl4_conf = [i + r4 for i in pl4]
    pl15_conf = [i + r15 for i in pl15]

    round_step = sc["round_step_pips"] * pip_size(pair)
    peak_cache: dict[int, int | None] = {}
    done_waves: set[int] = set()
    out = []

    for t in range(1, len(o_c)):
        # この M15 足が確定した時点で確定済みの H4 本数
        k = int(np.searchsorted(h["close_time"], m["close_time"][t], side="right"))
        if k == 0:
            continue
        # 確定済みの H4 山谷
        ph = ph4[:bisect.bisect_right(ph4_conf, k - 1)]
        pl = pl4[:bisect.bisect_right(pl4_conf, k - 1)]
        if not ph:
            continue
        top_i = ph[-1]                      # 第1波の頂点
        if top_i in done_waves:
            continue
        start_i = _last_before(pl, top_i)   # 第1波の起点
        if start_i is None:
            continue
        prev_top = _last_before(ph, start_i)
        if prev_top is None:
            continue
        prev_low = _last_before(pl, prev_top)
        if prev_low is None:
            continue
        # H4 上昇トレンド：高値切上げ＋安値切上げ
        if not (H_h[top_i] > H_h[prev_top] and H_l[start_i] > H_l[prev_low]):
            continue

        top, start = H_h[top_i], H_l[start_i]
        rng = top - start
        if rng <= 0:
            continue

        # 第1波の頂点が M15 のどの足か
        if top_i not in peak_cache:
            in_bar = np.where((m["open_time"] >= h["open_time"][top_i])
                              & (m["open_time"] < h["open_time"][top_i] + np.timedelta64(4, "h")))[0]
            peak_cache[top_i] = int(in_bar[np.argmax(o_h[in_bar])]) if len(in_bar) else None
        p = peak_cache[top_i]
        if p is None or p >= t:
            continue

        # 頂点を超えていたら第2波ではない
        if o_h[p + 1:t + 1].max(initial=-np.inf) > top:
            continue

        # ---- トリガー（安い判定から先に）：M15 ネックライン抜け ----
        lows = [i for i in pl15[:bisect.bisect_right(pl15_conf, t)] if i > p]
        if len(lows) < 2:
            continue
        l1, l2 = lows[-2], lows[-1]
        neck = o_h[l1:l2 + 1].max()
        if not (o_c[t] > neck >= o_c[t - 1]):
            continue

        # ---- 第2波ゾーン ----
        w2_low = o_l[p:t + 1].min()
        retr = (top - w2_low) / rng
        if not (w2["fib_min"] <= retr <= w2["fib_max"]) or w2_low <= start:
            continue

        # ---- 転換パターン ----
        tol = rv["double_tol_atr"] * a15[t]
        diff = o_l[l2] - o_l[l1]
        if diff < -tol:
            continue  # 安値切下げ中
        if o_l[l2 + 1:t + 1].min(initial=np.inf) < o_l[l2] - tol:
            continue  # 2つ目の安値を割ってから戻ってきた形は崩れとみなす
        double = abs(diff) <= tol
        pattern = "ダブルボトム" if double else "安値切上げ"

        # ---- 後追い除外 ----
        if o_c[t] - neck > en["chase_atr"] * a15[t]:
            continue

        # ---- SL / TP / RR ----
        entry = o_c[t]
        if en.get("sl_fib") is not None:
            sl_base = top - rng * en["sl_fib"]
        else:
            sl_base = min(o_l[l1], o_l[l2]) if double else o_l[l2]
        sl = sl_base - en["sl_buffer_atr"] * a15[t]
        risk = entry - sl
        if risk <= 0:
            continue
        tp_half = top
        tp = top if en.get("tp_extension") is None else w2_low + en["tp_extension"] * rng
        rr = (tp - entry) / risk
        if rr < en["min_rr"]:
            continue

        # ---- 根拠の数 ----
        reasons = [f"M15 {pattern}"]
        if w2["golden_min"] <= retr:
            reasons.append("Fib 50〜61.8%（黄金比ゾーン）")
        old_tops = [i for i in ph if i < start_i]
        if any(abs(H_h[i] - w2_low) <= sc["role_reversal_tol_atr"] * a4[k - 1] for i in old_tops):
            reasons.append("H4 旧高値が支持に転換" if sign > 0 else "H4 旧安値が抵抗に転換")
        real_low = abs(w2_low)
        nearest_round = round(real_low / round_step) * round_step
        if abs(real_low - nearest_round) <= sc["round_tol_atr"] * a15[t]:
            reasons.append(f"キリ番 {nearest_round:.{2 if pair.endswith('JPY') else 4}f}")
        if len(reasons) < sc["min_score"]:
            continue

        done_waves.add(top_i)
        out.append(dict(
            t=t, p=p, l1=l1, l2=l2, entry=entry, sl=sl, tp=tp, tp_half=tp_half, rr=rr,
            retrace=retr, pattern=pattern, neckline=neck, wave1_start=start,
            wave1_top=top, wave2_low=w2_low, reasons=reasons,
        ))
    return out


def _arrays(df: pd.DataFrame, span: pd.Timedelta, sign: float) -> dict:
    hi, lo = df["High"].to_numpy(), df["Low"].to_numpy()
    if sign < 0:
        hi, lo = -lo, -hi
    return dict(
        high=hi, low=lo, close=sign * df["Close"].to_numpy(),
        open_time=df.index.to_numpy(), close_time=(df.index + span).to_numpy(),
    )


def scan(pair: str, m15: pd.DataFrame, h4: pd.DataFrame, cfg: dict) -> list[Signal]:
    """全期間を1本ずつ進めて、その時点で出ていたはずのシグナルを全部返す。

    リアルタイム監視（v2）でも同じ関数を使い、最後の足のシグナルだけを拾う。
    """
    def flip(text: str) -> str:
        return text.replace("ダブルボトム", "ダブルトップ").replace("安値切上げ", "高値切下げ")

    signals = []
    for direction, sign in (("buy", 1.0), ("sell", -1.0)):
        raw = _scan_long(_arrays(m15, M15, sign), _arrays(h4, H4, sign), pair, cfg, sign)
        for r in raw:
            px = lambda v: sign * v  # noqa: E731  反転していた価格を元に戻す
            signals.append(Signal(
                pair=pair, direction=direction,
                time=(m15.index[r["t"]] + M15).tz_convert("Asia/Tokyo"),
                entry=px(r["entry"]), sl=px(r["sl"]), tp=px(r["tp"]), tp_half=px(r["tp_half"]),
                rr=r["rr"], retrace=r["retrace"],
                pattern=r["pattern"] if sign > 0 else flip(r["pattern"]),
                neckline=px(r["neckline"]), wave1_start=px(r["wave1_start"]),
                wave1_top=px(r["wave1_top"]), wave2_low=px(r["wave2_low"]),
                reasons=[s if sign > 0 else flip(s) for s in r["reasons"]],
                bar_index=r["t"], peak_index=r["p"], low1_index=r["l1"], low2_index=r["l2"],
            ))
    signals.sort(key=lambda s: s.time)
    return signals


def format_message(s: Signal, prefix: str = "") -> str:
    """通知の本文（app/plan_watch.py が Mac・Discord に送る）。"""
    d = 3 if s.pair.endswith("JPY") else 5
    side = "BUY" if s.direction == "buy" else "SELL"
    trend = "上昇" if s.direction == "buy" else "下降"
    wave = "押し目" if s.direction == "buy" else "戻り目"
    return "\n".join([
        f"{prefix}🟢 {s.pair} {side}候補（M15）{s.time:%m/%d %H:%M}",
        f"H4{trend}・第2波{wave} {s.retrace * 100:.1f}%",
        f"M15 {s.reasons[0].replace('M15 ', '')} → ネックライン {s.neckline:.{d}f} ブレイク",
        f"Entry {s.entry:.{d}f} / SL {s.sl:.{d}f} / TP {s.tp:.{d}f}（RR {s.rr:.1f}）",
        f"半分利確の目安 {s.tp_half:.{d}f}（第1波の{'高値' if s.direction == 'buy' else '安値'}）",
        f"根拠 {s.score}：" + "、".join(s.reasons),
    ])
