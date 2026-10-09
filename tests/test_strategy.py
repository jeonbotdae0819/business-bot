"""합성 데이터로 전략 판정 로직 검증: python -m pytest tests/"""
import numpy as np
import pandas as pd

from stock_screener.backtest import collect_trades, simulate_portfolio, trade_stats
from stock_screener.engine import run_universe
from stock_screener.strategy import Params, rolling_argmax, scan, simulate_trade

DATES = pd.bdate_range("2020-01-01", periods=520)


def ohlcv(close, vol=None):
    close = np.asarray(close, float)
    vol = np.full(len(close), 2_000_000.0) if vol is None else np.asarray(vol, float)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": vol}, index=DATES[:len(close)])


def path(*legs):
    """(일수, 목표가) 구간을 이어 붙인 직선 경로."""
    out, cur = [], legs[0][1]
    for days, target in legs[1:]:
        out.extend(np.linspace(cur, target, days + 1)[1:])
        cur = target
    return np.array([legs[0][1]] + out)


# 시장: 상승 → 1차 하락(300일) → 반등 → 더 깊은 신저가(340일) → 회복
MKT = path((0, 100), (260, 150), (40, 120), (20, 130), (20, 110), (179, 140))


def leader_prices(new_low_with_market=False):
    # 리더주: 강한 상승 → 고점(260일) → -40%(300일) → 횡보 → 360일 돌파 → 상승
    legs = [(0, 50), (260, 150), (40, 90), (15, 100), (15, 92)]
    legs += [(10, 80)] if new_low_with_market else [(10, 95)]
    legs += [(19, 99), (1, 104), (159, 160)]
    c = path(*legs)
    v = np.full(len(c), 2_000_000.0)
    v[360] = 5_000_000   # 돌파일 거래량 2.5배
    return ohlcv(c, v)


def run_scan(df, p=Params()):
    rs = pd.Series(0.95, index=df.index)
    return scan(df, pd.Series(MKT[:len(df)], index=df.index), rs, p)


def test_rolling_argmax():
    vals = np.array([1, 3, 2, 5, 4, 1, 0])
    assert rolling_argmax(vals, 3).tolist() == [-1, -1, 1, 3, 3, 3, 4]


def test_signal_on_divergence_breakout():
    sigs, _ = run_scan(leader_prices())
    assert len(sigs) >= 1
    s = sigs[0]
    assert s["idx"] == 360
    assert 0.38 < s["drawdown"] < 0.42
    assert s["low_date"] == DATES[300]


def test_no_signal_when_stock_breaks_low_with_market():
    sigs, _ = run_scan(leader_prices(new_low_with_market=True))
    # 시장과 함께 신저가를 냈으므로 저점 이후 시장이 더 낮은 저점을 만들지 않음 → 다이버전스 없음
    assert all(s["idx"] != 360 for s in sigs)


def test_no_signal_without_volume():
    df = leader_prices()
    df.loc[DATES[360], "Volume"] = 2_000_000
    sigs, _ = run_scan(df)
    assert all(s["idx"] != 360 for s in sigs)


def test_no_signal_for_non_leader():
    df = leader_prices()
    sigs, _ = scan(df, pd.Series(MKT, index=df.index), pd.Series(0.5, index=df.index), Params())
    assert sigs == []


def test_divergence_can_be_disabled():
    df = leader_prices(new_low_with_market=True)
    sigs, _ = run_scan(df, Params(require_divergence=False))
    assert len(sigs) >= 1


def test_snapshot_reports_ready_setup_before_breakout():
    df = leader_prices().iloc[:355]
    _, snap = run_scan(df)
    assert snap["setup"] and snap["diverged"] and not snap["breakout_today"]
    assert snap["to_pivot"] > 0


def test_trade_exits():
    df = leader_prices()
    tr = simulate_trade(df, 360, Params())
    assert tr["entry_date"] == DATES[361]
    assert tr["return"] > 0.3          # 이후 160까지 상승 → 큰 수익

    # 서서히 밀리면 -8% 손절가에서 체결
    c = df["Close"].to_numpy().copy()
    c[362:367] = c[361] * np.linspace(0.98, 0.90, 5)
    tr = simulate_trade(ohlcv(c), 360, Params())
    assert tr["reason"] == "stop"
    assert -0.09 < tr["return"] < -0.07

    # 갭 하락이면 손절가가 아니라 시가에 체결 (손실이 -8%보다 커짐)
    c = df["Close"].to_numpy().copy()
    c[362:] = c[361] * 0.85
    tr = simulate_trade(ohlcv(c), 360, Params())
    assert tr["reason"] == "stop"
    assert tr["return"] < -0.14


def test_end_to_end_universe():
    rng = np.random.default_rng(0)
    prices = {f"R{i}": ohlcv(50 * np.exp(np.cumsum(rng.normal(0, 0.01, len(MKT)))))
              for i in range(40)}
    prices["LEAD"] = leader_prices()
    mkt = ohlcv(MKT)
    p = Params()
    results = run_universe(mkt, prices, p)
    trades = collect_trades(results, p)
    assert "LEAD" in set(trades["ticker"])
    assert trade_stats(trades)["trades"] == len(trades)
    eq, taken = simulate_portfolio(trades, prices, mkt, 10, p.cost_pct)
    assert taken >= 1 and eq.iloc[-1] > 0
