"""
백테스트: 과거 신호를 규칙대로 매매했을 때의 성과

실행 예:
  python -m stock_screener.backtest                      # 2005년~현재, SPY 기준
  python -m stock_screener.backtest --market QQQ
  python -m stock_screener.backtest --compare            # 조건을 하나씩 뺀 버전과 비교 (③ 다이버전스의 효과 등)
  python -m stock_screener.backtest --max-positions 5

결과:
  1) 트레이드 통계 (승률, 평균 수익/손실, 손익비, 기대값, 청산 사유)
  2) 하락장 구간별 성과 (2008, 2018 말, 2020, 2022, 2025)
  3) 포트폴리오 시뮬레이션 (최대 N종목 균등 비중) vs 지수 단순 보유: CAGR, MDD
  CSV: output/trades.csv, output/equity.csv
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from .data import load_market_data
from .engine import run_universe
from .strategy import Params, simulate_trade

OUT_DIR = Path(__file__).resolve().parent / "output"

EPISODES = [
    ("2008 금융위기", "2008-06-01", "2009-12-31"),
    ("2011 유럽위기", "2011-07-01", "2012-06-30"),
    ("2015-16 조정", "2015-08-01", "2016-08-31"),
    ("2018 말 급락", "2018-10-01", "2019-06-30"),
    ("2020 코로나", "2020-02-15", "2020-12-31"),
    ("2022 약세장", "2022-01-01", "2023-06-30"),
    ("2025 관세 쇼크", "2025-02-15", "2025-12-31"),
]


def collect_trades(results, p: Params) -> pd.DataFrame:
    rows = []
    for tk, (sigs, _snap, df) in results.items():
        busy_until = -1
        for s in sigs:
            if s["idx"] <= busy_until:      # 이미 보유 중이면 추가 신호 무시
                continue
            tr = simulate_trade(df, s["idx"], p)
            if tr is None:
                continue
            busy_until = tr["exit_idx"]
            rows.append({"ticker": tk, "signal_date": s["date"], **tr,
                         "drawdown": s["drawdown"], "base_days": s["base_days"], "rs_at_peak": s["rs_at_peak"]})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).drop(columns=["entry_idx", "exit_idx"]).sort_values("entry_date").reset_index(drop=True)


def trade_stats(tr: pd.DataFrame) -> dict:
    if tr.empty:
        return {"trades": 0}
    r = tr["return"]
    wins, losses = r[r > 0], r[r <= 0]
    avg_w = wins.mean() if len(wins) else 0.0
    avg_l = losses.mean() if len(losses) else 0.0
    return {
        "trades": len(r),
        "win_rate": len(wins) / len(r),
        "avg_win": avg_w,
        "avg_loss": avg_l,
        "payoff": avg_w / -avg_l if avg_l < 0 else np.nan,
        "expectancy": r.mean(),
        "median": r.median(),
        "profit_factor": wins.sum() / -losses.sum() if losses.sum() < 0 else np.nan,
        "best": r.max(),
        "worst": r.min(),
        "avg_hold": tr["hold_days"].mean(),
    }


def simulate_portfolio(trades: pd.DataFrame, prices: dict, mkt: pd.DataFrame, max_positions: int, cost: float):
    """최대 N종목, 매수 시점 자산의 1/N씩 투자. 같은 날 신호가 넘치면 상대강도 높은 순."""
    if trades.empty:
        return pd.Series(dtype=float), 0
    dates = mkt.index[mkt.index >= trades["entry_date"].min()]
    closes = pd.DataFrame({t: prices[t]["Close"] for t in trades["ticker"].unique()}).reindex(dates).ffill()
    by_entry = {d: g.sort_values("rs_at_peak", ascending=False) for d, g in trades.groupby("entry_date")}

    cash, equity_prev = 1.0, 1.0
    held = {}   # ticker -> (shares, exit_date, exit_px)
    curve, invested, taken = [], [], 0
    for d in dates:
        for _, t in by_entry.get(d, pd.DataFrame()).iterrows():
            if len(held) >= max_positions or t["ticker"] in held:
                continue
            alloc = min(equity_prev / max_positions, cash)
            if alloc <= 0:
                break
            cash -= alloc
            held[t["ticker"]] = (alloc * (1 - cost) / t["entry"], t["exit_date"], t["exit"])
            taken += 1
        for tk in [k for k, (_, xd, _) in held.items() if xd == d]:
            sh, _, px = held.pop(tk)
            cash += sh * px * (1 - cost)
        pos_val = sum(sh * closes.at[d, tk] for tk, (sh, _, _) in held.items())
        equity_prev = cash + pos_val
        curve.append(equity_prev)
        invested.append(pos_val / equity_prev if equity_prev > 0 else 0)
    eq = pd.Series(curve, index=dates, name="equity")
    eq.attrs["exposure"] = float(np.mean(invested))
    return eq, taken


def curve_stats(eq: pd.Series) -> dict:
    if eq.empty:
        return {}
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    total = eq.iloc[-1] / eq.iloc[0] - 1
    dd = eq / eq.cummax() - 1
    daily = eq.pct_change().dropna()
    return {
        "total": total,
        "cagr": (1 + total) ** (1 / years) - 1 if years > 0 else np.nan,
        "mdd": dd.min(),
        "vol": daily.std() * np.sqrt(252),
        "sharpe0": daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else np.nan,
    }


def pct(x):
    return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:+.1%}"


def print_trade_stats(title, s):
    print(f"\n[{title}]")
    if s.get("trades", 0) == 0:
        print("  트레이드 없음")
        return
    print(f"  트레이드 {s['trades']}회 | 승률 {s['win_rate']:.1%} | 평균 수익 {pct(s['avg_win'])} | 평균 손실 {pct(s['avg_loss'])}")
    print(f"  손익비 {s['payoff']:.2f} | 1회 기대값 {pct(s['expectancy'])} (중앙값 {pct(s['median'])}) | "
          f"Profit Factor {s['profit_factor']:.2f}")
    print(f"  최고 {pct(s['best'])} | 최악 {pct(s['worst'])} | 평균 보유 {s['avg_hold']:.0f}거래일")


def run_once(mkt, prices, p: Params, max_positions: int, verbose=True):
    results = run_universe(mkt, prices, p)
    trades = collect_trades(results, p)
    s = trade_stats(trades)
    eq, taken = simulate_portfolio(trades, prices, mkt, max_positions, p.cost_pct)
    bench = mkt["Close"].reindex(eq.index) if not eq.empty else pd.Series(dtype=float)
    ps, bs = curve_stats(eq), curve_stats(bench)

    if verbose:
        print_trade_stats("전체 트레이드 통계 (신호마다 1회 매매 가정)", s)
        if not trades.empty:
            print("  청산 사유:", ", ".join(f"{k} {v}" for k, v in trades["reason"].value_counts().items()),
                  "(stop=손절, breakeven=본전손절, ma_exit=50일선 이탈, time=보유기간 만료, open=보유 중)")

            print("\n[하락장 구간별 (매수일 기준)]")
            rows = []
            for name, a, b in EPISODES:
                sub = trades[(trades["entry_date"] >= a) & (trades["entry_date"] <= b)]
                st = trade_stats(sub)
                if st["trades"]:
                    rows.append([name, st["trades"], f"{st['win_rate']:.0%}", pct(st["expectancy"]),
                                 pct(st["avg_win"]), pct(st["avg_loss"]), pct(st["best"])])
            print(pd.DataFrame(rows, columns=["구간", "횟수", "승률", "기대값", "평균수익", "평균손실", "최고"])
                  .to_string(index=False) if rows else "  해당 구간 트레이드 없음")

            print("\n[연도별 (매수일 기준)]")
            yr = trades.groupby(trades["entry_date"].dt.year.rename("연도"))["return"].agg(
                횟수="count", 승률=lambda r: f"{(r > 0).mean():.0%}", 평균=lambda r: pct(r.mean()), 합계=lambda r: pct(r.sum()))
            print(yr.to_string())

        if ps:
            print(f"\n[포트폴리오: 최대 {max_positions}종목 균등, {eq.index[0].date()} ~ {eq.index[-1].date()}]")
            print(f"  전략     총수익 {pct(ps['total'])} | CAGR {pct(ps['cagr'])} | MDD {pct(ps['mdd'])} | "
                  f"샤프(무위험0) {ps['sharpe0']:.2f} | 평균 투자비중 {eq.attrs['exposure']:.0%} | 실제 체결 {taken}회")
            print(f"  지수보유 총수익 {pct(bs['total'])} | CAGR {pct(bs['cagr'])} | MDD {pct(bs['mdd'])} | "
                  f"샤프(무위험0) {bs['sharpe0']:.2f}")
    return trades, eq, s, ps, bs


def main():
    ap = argparse.ArgumentParser(description="리더주 반전 전략 백테스트 (미국 주식)")
    ap.add_argument("--market", default="SPY")
    ap.add_argument("--start", default="2005-01-01")
    ap.add_argument("--end")
    ap.add_argument("--tickers-file")
    ap.add_argument("--max-positions", type=int, default=10)
    ap.add_argument("--compare", action="store_true", help="조건을 하나씩 바꾼 버전들과 성과 비교")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    mkt, prices = load_market_data(args.market, tickers_file=args.tickers_file, start=args.start,
                                   end=args.end, refresh=args.refresh)
    base = Params()
    print("\n※ 유니버스가 '현재' S&P500+나스닥100 구성종목이라 생존 편향이 있습니다 (과거일수록 결과가 좋게 나옴).")
    print("=" * 90)
    print("기본 전략: ①리더주(RS 상위20%) ②고점 -30~60% ③시장만 신저가 ④피벗 거래량 돌파 + 지수 20일선 위")
    trades, eq, base_s, base_ps, base_bs = run_once(mkt, prices, base, args.max_positions)

    OUT_DIR.mkdir(exist_ok=True)
    trades.to_csv(OUT_DIR / "trades.csv", index=False)
    eq.to_csv(OUT_DIR / "equity.csv")
    print(f"\nCSV 저장: {OUT_DIR}/trades.csv, equity.csv")

    if args.compare:
        variants = [
            ("기본 (①~④)", base),
            ("③ 다이버전스 제외", base.with_(require_divergence=False)),
            ("지수 20일선 필터 제외", base.with_(market_filter=False)),
            ("① 리더주 조건 제외", base.with_(rs_min_pct=0.0)),
            ("조정폭 15~30%", base.with_(min_drawdown=0.15, max_drawdown=0.30)),
            ("손절 -5%", base.with_(stop_pct=0.05)),
        ]
        rows = []
        for name, p in variants:
            if p == base:
                s, ps, bs = base_s, base_ps, base_bs
            else:
                print(f"  비교 실행: {name} ...")
                _, _, s, ps, bs = run_once(mkt, prices, p, args.max_positions, verbose=False)
            rows.append([name, s.get("trades", 0),
                         f"{s['win_rate']:.0%}" if s.get("trades") else "-",
                         pct(s.get("expectancy")), f"{s['payoff']:.2f}" if s.get("trades") else "-",
                         pct(ps.get("cagr")), pct(ps.get("mdd")), pct(bs.get("cagr"))])
        print("\n[조건별 비교]")
        print(pd.DataFrame(rows, columns=["버전", "트레이드", "승률", "기대값", "손익비", "전략CAGR", "전략MDD", "지수CAGR"])
              .to_string(index=False))


if __name__ == "__main__":
    main()
