"""
스크리너: 지금(또는 --as-of 날짜) 기준으로 조건을 만족하는 미국 종목 찾기

실행 예:
  python -m stock_screener.screener
  python -m stock_screener.screener --market QQQ
  python -m stock_screener.screener --as-of 2022-11-30      # 과거 시점 재현
  python -m stock_screener.screener --tickers-file my_list.txt

출력 단계:
  [A] 최근 돌파 신호   : ①~④ 모두 충족 (최근 N거래일 이내 돌파)
  [B] 돌파 대기        : ①~③ 충족, 횡보구간 고점(피벗) 돌파를 기다리는 중
  [C] 관찰             : ①② 충족, 아직 ③ 다이버전스(시장만 신저가) 미확인
"""
import argparse
from pathlib import Path

import pandas as pd

from .data import load_market_data
from .engine import run_universe
from .strategy import Params

OUT_DIR = Path(__file__).resolve().parent / "output"


def build_tables(results, recent_days: int, p: Params):
    recent, ready, watch = [], [], []
    for tk, (sigs, snap, df) in results.items():
        if sigs and sigs[-1]["idx"] >= len(df) - recent_days:
            s = sigs[-1]
            last = df["Close"].iloc[-1]
            recent.append({
                "ticker": tk, "signal_date": s["date"].date(), "pivot": s["pivot"], "close_now": last,
                "since_signal": last / s["close"] - 1, "drawdown": s["drawdown"],
                "base_days": s["base_days"], "rs_at_peak": s["rs_at_peak"], "vol_ratio": s["vol_ratio"],
                "stop_ref": s["close"] * (1 - p.stop_pct),
            })
        if not snap or not (snap["leader"] and snap["in_drawdown_range"] and snap["liquid"]):
            continue
        row = {
            "ticker": tk, "close": snap["close"], "peak": snap["peak"], "peak_date": snap["peak_date"].date(),
            "low_date": snap["low_date"].date(), "drawdown": snap["drawdown"], "base_days": snap["base_days"],
            "pivot": snap["pivot"], "to_pivot": snap["to_pivot"], "rs_at_peak": snap["rs_at_peak"],
        }
        if snap["setup"] and not snap["breakout_today"]:
            if snap["to_pivot"] <= 0:
                row["note"] = "피벗 위, 거래량 부족" if snap["market_ok"] else "피벗 위, 지수 20일선 아래"
            else:
                row["note"] = "" if snap["market_ok"] else "지수 20일선 아래"
            ready.append(row)
        elif not snap["diverged"]:
            watch.append(row)

    def frame(rows, sort, asc=True):
        return pd.DataFrame(rows).sort_values(sort, ascending=asc).reset_index(drop=True) if rows else pd.DataFrame()

    return frame(recent, "signal_date", False), frame(ready, "to_pivot"), frame(watch, "rs_at_peak", False)


def fmt(df: pd.DataFrame) -> str:
    if df.empty:
        return "  (해당 종목 없음)"
    df = df.copy()
    for col in ("drawdown", "to_pivot", "since_signal"):
        if col in df:
            df[col] = df[col].map(lambda x, c=col: "-" if pd.isna(x) else (f"-{x:.1%}" if c == "drawdown" else f"{x:+.1%}"))
    if "rs_at_peak" in df:
        df["rs_at_peak"] = (df["rs_at_peak"] * 100).round().astype("Int64")
    for col in ("close", "peak", "pivot", "close_now", "stop_ref", "vol_ratio"):
        if col in df:
            df[col] = df[col].round(2)
    return df.to_string(index=False)


def main():
    ap = argparse.ArgumentParser(description="리더주 반전 스크리너 (미국 주식)")
    ap.add_argument("--market", default="SPY", help="다이버전스 비교 지수 (SPY 또는 QQQ)")
    ap.add_argument("--as-of", help="이 날짜 기준으로 스크리닝 (YYYY-MM-DD)")
    ap.add_argument("--tickers-file", help="유니버스를 직접 지정 (한 줄에 티커 하나)")
    ap.add_argument("--recent-days", type=int, default=5, help="[A] 돌파 신호를 몇 거래일 전까지 보여줄지")
    ap.add_argument("--start", default="2015-01-01", help="데이터 시작일 (스크리닝엔 2년 이상이면 충분)")
    ap.add_argument("--refresh", action="store_true", help="캐시 무시하고 다시 다운로드")
    args = ap.parse_args()

    p = Params()
    mkt, prices = load_market_data(args.market, tickers_file=args.tickers_file, start=args.start, refresh=args.refresh)
    results = run_universe(mkt, prices, p, as_of=args.as_of)

    m = mkt.loc[:args.as_of] if args.as_of else mkt
    asof = m.index[-1].date()
    mc = m["Close"]
    print(f"\n=== 리더주 반전 스크리너 | 기준일 {asof} | 지수 {args.market} ===")
    print(f"지수: {mc.iloc[-1]:.2f}  (1년 고점 대비 {mc.iloc[-1] / mc.iloc[-252:].max() - 1:+.1%}, "
          f"20일선 {'위' if mc.iloc[-1] > mc.iloc[-20:].mean() else '아래'})")

    recent, ready, watch = build_tables(results, args.recent_days, p)
    print(f"\n[A] 최근 {args.recent_days}거래일 돌파 신호 (①~④ 충족, 다음 날 시가 매수 기준)")
    print(fmt(recent))
    print("\n[B] 돌파 대기 (①~③ 충족, to_pivot = 피벗까지 남은 상승률)")
    print(fmt(ready))
    print("\n[C] 관찰 (①② 충족, 시장 신저가 대비 다이버전스 미확인)")
    print(fmt(watch.head(30)))

    OUT_DIR.mkdir(exist_ok=True)
    for name, df in (("A_signals", recent), ("B_ready", ready), ("C_watch", watch)):
        df.to_csv(OUT_DIR / f"screener_{asof}_{name}.csv", index=False)
    print(f"\nCSV 저장: {OUT_DIR}")


if __name__ == "__main__":
    main()
