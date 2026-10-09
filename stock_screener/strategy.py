"""
리더주 반전 전략 (미국 주식)

① 상승장에서 시장보다 강했던 '리더주' (고점 당시 6개월 수익률 상위 20%)
② 고점에서 30~60% 조정
③ 종목이 저점을 찍은 뒤에도 시장(SPY 등)은 신저가를 내는데, 종목은 그 저점을 지킴 (상대강도 다이버전스)
④ 저점 이후 형성된 횡보구간 고점(피벗)을 거래량을 동반해 종가로 돌파 → 다음 날 시가 매수

스크리너와 백테스트가 같은 판정 로직(scan)을 공유합니다.
"""
from collections import deque
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Params:
    # ① 리더주
    peak_window: int = 252          # 최근 1년 최고가를 '고점'으로 사용
    rs_lookback: int = 126          # 상대강도 계산 기간 (6개월 수익률)
    rs_min_pct: float = 0.80        # 고점 시점에 유니버스 내 상위 20% 이내
    # ② 조정 폭
    min_drawdown: float = 0.30
    max_drawdown: float = 0.60
    # ③ 다이버전스 / 횡보구간
    require_divergence: bool = True
    min_base_days: int = 10         # 저점 이후 최소 횡보 기간 (거래일)
    max_base_days: int = 120        # 저점이 너무 오래됐으면 제외
    # ④ 돌파
    breakout_vol_mult: float = 1.5  # 돌파일 거래량 >= 50일 평균의 1.5배
    vol_avg_days: int = 50
    market_filter: bool = True      # 돌파일에 지수가 20일선 위일 때만 진입
    market_ma_days: int = 20
    max_attempts: int = 2           # 같은 횡보구간에서 최대 진입 시도 횟수
    # 유동성
    min_price: float = 10.0
    min_dollar_vol: float = 20e6    # 50일 평균 거래대금 2천만 달러 이상
    # 매도 규칙 (백테스트용)
    stop_pct: float = 0.08          # 매수가 대비 -8% 손절
    breakeven_trigger: float = 0.15 # +15% 도달 후 손절선을 매수가로 상향
    exit_ma_days: int = 50          # 50일선 종가 이탈 시 청산 (한 번 50일선 위로 올라선 뒤부터 적용)
    max_hold_days: int = 252
    cost_pct: float = 0.001         # 편도 수수료+슬리피지 0.1%

    def with_(self, **kw):
        return replace(self, **kw)


def rolling_argmax(values: np.ndarray, window: int) -> np.ndarray:
    """각 시점 t에서 [t-window+1, t] 구간 최댓값의 인덱스 (데이터가 window보다 짧으면 -1)."""
    n = len(values)
    out = np.full(n, -1, dtype=np.int64)
    dq = deque()
    for t in range(n):
        while dq and values[dq[-1]] <= values[t]:
            dq.pop()
        dq.append(t)
        if dq[0] <= t - window:
            dq.popleft()
        if t >= window - 1:
            out[t] = dq[0]
    return out


def relative_strength(closes: pd.DataFrame, lookback: int, min_names: int = 30) -> pd.DataFrame:
    """유니버스 내 N일 수익률 백분위 (0~1). 비교 종목 수가 적은 날은 NaN."""
    ret = closes / closes.shift(lookback) - 1
    pct = ret.rank(axis=1, pct=True)
    pct[ret.notna().sum(axis=1) < min_names] = np.nan
    return pct


def scan(df: pd.DataFrame, mkt_close: pd.Series, rs_pct: pd.Series, p: Params):
    """
    한 종목의 일봉을 처음부터 끝까지 훑으며 매수 신호와 마지막 날 상태를 계산.

    df: Open/High/Low/Close/Volume (결측 없는 일봉)
    mkt_close: 시장 지수 종가 (df.index 기준으로 정렬돼 있어야 함)
    rs_pct: 이 종목의 상대강도 백분위 (df.index 기준)
    반환: (signals: list[dict], snapshot: dict | None)
    """
    o = df["Open"].to_numpy(float)
    h = df["High"].to_numpy(float)
    lo = df["Low"].to_numpy(float)
    c = df["Close"].to_numpy(float)
    v = df["Volume"].to_numpy(float)
    m = mkt_close.to_numpy(float)
    rs = rs_pct.to_numpy(float)
    idx = df.index
    n = len(df)

    peak_idx = rolling_argmax(h, p.peak_window)
    avg_vol = pd.Series(v).rolling(p.vol_avg_days).mean().shift(1).to_numpy()
    dollar_vol = pd.Series(c * v).rolling(p.vol_avg_days).mean().to_numpy()
    mkt_ma = pd.Series(m).rolling(p.market_ma_days).mean().to_numpy()

    signals = []
    snapshot = None

    cur_peak = -1
    low_val = low_idx = pivot = mkt_before = mkt_after = attempts = None

    def reset(pk):
        nonlocal low_val, low_idx, pivot, mkt_before, mkt_after, attempts
        low_val, low_idx = np.inf, -1
        pivot, mkt_before, mkt_after = -np.inf, np.inf, np.inf
        attempts = 0

    def update(pk, s):
        """s일의 가격으로 '고점 이후' 상태 갱신 (고점 당일은 제외)."""
        nonlocal low_val, low_idx, pivot, mkt_before, mkt_after, attempts
        if s <= pk:
            return
        if lo[s] < low_val:
            # 종목 신저가 → 횡보구간/다이버전스 판정을 새로 시작
            low_val, low_idx = lo[s], s
            pivot = -np.inf
            mkt_before = np.nanmin(m[pk:s + 1])
            mkt_after = np.inf
            attempts = 0
        else:
            pivot = max(pivot, h[s])
            mkt_after = min(mkt_after, m[s])

    for t in range(n):
        pk = peak_idx[t]
        if pk < 0:
            continue
        if pk != cur_peak:
            cur_peak = pk
            reset(pk)
            for s in range(pk + 1, t):   # 고점이 바뀌면 고점 이후 구간을 다시 계산
                update(pk, s)

        # --- t일 판정 (t-1일까지의 상태 사용) ---
        if low_idx >= 0:
            peak_val = h[pk]
            dd = 1 - low_val / peak_val
            base_days = t - low_idx
            leader = bool(rs[pk] >= p.rs_min_pct) if not np.isnan(rs[pk]) else False
            diverged = mkt_after < mkt_before
            liquid = c[t] >= p.min_price and dollar_vol[t] >= p.min_dollar_vol
            in_dd = p.min_drawdown <= dd <= p.max_drawdown
            base_ok = p.min_base_days <= base_days <= p.max_base_days
            setup = leader and in_dd and base_ok and liquid and (diverged or not p.require_divergence)
            vol_ok = not np.isnan(avg_vol[t]) and v[t] >= p.breakout_vol_mult * avg_vol[t]
            breakout = np.isfinite(pivot) and c[t] > pivot and vol_ok
            mkt_ok = (not p.market_filter) or (not np.isnan(mkt_ma[t]) and m[t] > mkt_ma[t])
            fired = setup and breakout and mkt_ok and attempts < p.max_attempts

            if fired:
                attempts += 1
                signals.append({
                    "idx": t, "date": idx[t], "close": c[t], "pivot": pivot,
                    "peak": peak_val, "peak_date": idx[pk], "low": low_val, "low_date": idx[low_idx],
                    "drawdown": dd, "base_days": base_days, "rs_at_peak": rs[pk],
                    "vol_ratio": v[t] / avg_vol[t],
                })

            if t == n - 1:
                snapshot = {
                    "date": idx[t], "close": c[t], "peak": peak_val, "peak_date": idx[pk],
                    "low": low_val, "low_date": idx[low_idx], "drawdown": dd, "base_days": base_days,
                    "pivot": pivot if np.isfinite(pivot) else np.nan,
                    "to_pivot": (pivot / c[t] - 1) if np.isfinite(pivot) else np.nan,
                    "rs_at_peak": rs[pk], "leader": leader, "in_drawdown_range": in_dd,
                    "diverged": diverged, "base_ok": base_ok, "liquid": liquid, "setup": setup,
                    "breakout_today": bool(fired), "market_ok": mkt_ok,
                    "vol_ratio": v[t] / avg_vol[t] if avg_vol[t] else np.nan,
                }

        update(pk, t)

    return signals, snapshot


def simulate_trade(df: pd.DataFrame, sig_idx: int, p: Params):
    """신호 다음 날 시가 매수 → 손절 / 본전 손절 / 50일선 이탈 / 보유기간 만료 중 먼저 오는 조건으로 청산."""
    o = df["Open"].to_numpy(float)
    lo = df["Low"].to_numpy(float)
    c = df["Close"].to_numpy(float)
    sma = df["Close"].rolling(p.exit_ma_days).mean().to_numpy()
    n = len(df)
    e = sig_idx + 1
    if e >= n:
        return None
    entry = o[e]
    stop = entry * (1 - p.stop_pct)
    ma_armed = False
    exit_i, exit_px, reason = None, None, None
    last = min(n - 1, e + p.max_hold_days)

    for t in range(e, last + 1):
        if lo[t] <= stop:
            exit_px = stop if t == e else min(o[t], stop)
            exit_i = t
            reason = "breakeven" if stop >= entry else "stop"
            break
        if c[t] >= entry * (1 + p.breakeven_trigger):
            stop = max(stop, entry)
        if not np.isnan(sma[t]):
            if c[t] > sma[t]:
                ma_armed = True
            elif ma_armed and c[t] < sma[t]:
                exit_i, exit_px, reason = t, c[t], "ma_exit"
                break
    if exit_i is None:
        exit_i, exit_px = last, c[last]
        reason = "time" if last == e + p.max_hold_days else "open"

    ret = (exit_px / entry) * (1 - p.cost_pct) ** 2 - 1
    return {
        "entry_idx": e, "entry_date": df.index[e], "entry": entry,
        "exit_idx": exit_i, "exit_date": df.index[exit_i], "exit": exit_px,
        "return": ret, "hold_days": exit_i - e, "reason": reason,
    }
