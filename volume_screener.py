"""
거래량 급증 + 주가 미상승 종목 스크리너 (국장 + 미장)

기준
- 최근 구간  : 최근 20거래일 (약 1개월)
- 평소 구간  : 그 직전 60거래일 (약 3개월)
- 1단계      : 최근 20일 평균 거래량 / 평소 60일 평균 거래량 >= 3
- 2단계      : 최근 20일 주가 변화율 (마지막 종가 / 구간 직전 종가 - 1) < +3%
- 잡음 제거  : 최근 20일 평균 거래대금 하한 (국장 10억 원, 미장 500만 달러),
               평소 구간에 거래량 0인 날이 5일 넘으면 제외 (거래정지·신규상장 착시 방지)

데이터 출처
- 국장: 한국거래소 (pykrx -> data.krx.co.kr), 실패 시 Yahoo Finance
- 미장: 종목 목록 nasdaqtrader.com, 시세 Yahoo Finance (yfinance)

설치: pip install pandas pykrx yfinance
사용법: python volume_screener.py [kr|us|all]
결과: output/ 폴더에 CSV 저장
"""
import os
import sys
import datetime as dt

import pandas as pd

RECENT = 20
BASE = 60
RATIO_MIN = 3.0
PRICE_MAX = 0.03
MIN_VALUE = {"KR": 1_000_000_000, "US": 5_000_000}
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def screen(close: pd.DataFrame, volume: pd.DataFrame, market: str) -> pd.DataFrame:
    """close/volume: index=날짜(오름차순), columns=종목코드."""
    close = close.sort_index()
    volume = volume.sort_index()
    if len(close) < RECENT + BASE + 1:
        raise RuntimeError(f"{market}: 데이터 일수 부족 ({len(close)}일)")
    recent_v = volume.iloc[-RECENT:]
    base_v = volume.iloc[-(RECENT + BASE):-RECENT]
    recent_c = close.iloc[-RECENT:]
    prev_close = close.iloc[-RECENT - 1]

    df = pd.DataFrame({
        "recent_avg_vol": recent_v.mean(),
        "base_avg_vol": base_v.mean(),
        "base_zero_days": (base_v.fillna(0) == 0).sum(),
        "prev_close": prev_close,
        "last_close": close.iloc[-1],
        "recent_avg_value": (recent_v * recent_c).mean(),
    })
    df["vol_ratio"] = df["recent_avg_vol"] / df["base_avg_vol"]
    df["price_chg_pct"] = (df["last_close"] / df["prev_close"] - 1) * 100
    df = df.replace([float("inf"), -float("inf")], pd.NA).dropna(subset=["vol_ratio", "price_chg_pct"])
    df = df[(df["base_zero_days"] <= 5) & (df["recent_avg_value"] >= MIN_VALUE[market])]

    step1 = df[df["vol_ratio"] >= RATIO_MIN].copy()
    step1["passes_step2"] = step1["price_chg_pct"] < PRICE_MAX * 100
    step1["market"] = market
    step1["period"] = f"{recent_v.index[0].date()} ~ {recent_v.index[-1].date()}"
    return step1.sort_values("vol_ratio", ascending=False)


# ---------------- 국장 ----------------
def load_kr():
    names = {}
    try:
        from pykrx import stock
        end = dt.date.today()
        start = end - dt.timedelta(days=200)
        days = stock.get_previous_business_days(fromdate=start.strftime("%Y%m%d"), todate=end.strftime("%Y%m%d"))
        days = days[-(RECENT + BASE + 1):]
        closes, vols = {}, {}
        for d in days:
            ds = d.strftime("%Y%m%d")
            frames = [stock.get_market_ohlcv_by_ticker(ds, market=m) for m in ("KOSPI", "KOSDAQ")]
            day = pd.concat(frames)
            closes[d] = day["종가"]
            vols[d] = day["거래량"]
        close = pd.DataFrame(closes).T
        volume = pd.DataFrame(vols).T
        for t in close.columns:
            try:
                names[t] = stock.get_market_ticker_name(t)
            except Exception:
                pass
        return close, volume, names
    except Exception as e:
        print(f"[KR] pykrx 실패 → Yahoo로 대체: {e}")
        from pykrx import stock
        tickers = {}
        for m, sfx in (("KOSPI", ".KS"), ("KOSDAQ", ".KQ")):
            for t in stock.get_market_ticker_list(market=m):
                tickers[t + sfx] = t
        close, volume = load_yahoo(list(tickers))
        close.columns = [tickers[c] for c in close.columns]
        volume.columns = [tickers[c] for c in volume.columns]
        return close, volume, names


# ---------------- 미장 ----------------
def us_tickers():
    base = "https://www.nasdaqtrader.com/dynamic/SymDir/"
    nas = pd.read_csv(base + "nasdaqlisted.txt", sep="|")
    oth = pd.read_csv(base + "otherlisted.txt", sep="|")
    nas = nas[(nas["Test Issue"] == "N") & (nas["ETF"] == "N")]
    oth = oth[(oth["Test Issue"] == "N") & (oth["ETF"] == "N")]
    names = dict(zip(nas["Symbol"], nas["Security Name"]))
    names.update(dict(zip(oth["ACT Symbol"], oth["Security Name"])))
    # 워런트·유닛·우선주 등 특수 증권 제외
    syms = [s for s in names if isinstance(s, str) and s.isalpha()]
    return syms, names


def load_yahoo(tickers):
    import yfinance as yf
    closes, vols = [], []
    for i in range(0, len(tickers), 300):
        chunk = tickers[i:i + 300]
        data = yf.download(chunk, period="6mo", interval="1d", auto_adjust=False,
                           group_by="column", threads=True, progress=False)
        if data.empty:
            continue
        closes.append(data["Close"])
        vols.append(data["Volume"])
        print(f"  {min(i + 300, len(tickers))}/{len(tickers)}")
    close = pd.concat(closes, axis=1)
    volume = pd.concat(vols, axis=1)
    # 당일 장중 미완성 봉 제거 (거래량 결측이 과반인 마지막 행)
    while len(volume) and volume.iloc[-1].isna().mean() > 0.5:
        close, volume = close.iloc[:-1], volume.iloc[:-1]
    return close, volume


def load_us():
    syms, names = us_tickers()
    yahoo = [s.replace(".", "-") for s in syms]
    close, volume = load_yahoo(yahoo)
    return close, volume, names


def run(market):
    print(f"[{market}] 데이터 수집 중...")
    close, volume, names = load_kr() if market == "KR" else load_us()
    res = screen(close, volume, market)
    res.insert(0, "name", [names.get(t, "") for t in res.index])
    os.makedirs(OUT_DIR, exist_ok=True)
    res.to_csv(os.path.join(OUT_DIR, f"{market}_step1_vol3x.csv"), encoding="utf-8-sig")
    res[res["passes_step2"]].to_csv(os.path.join(OUT_DIR, f"{market}_step2_vol3x_price_lt3pct.csv"), encoding="utf-8-sig")
    print(f"[{market}] 기간 {res['period'].iloc[0] if len(res) else '-'} | 1단계 {len(res)}개 | 2단계 {int(res['passes_step2'].sum())}개")
    return res


if __name__ == "__main__":
    arg = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    for m in (["KR", "US"] if arg == "all" else [arg.upper()]):
        run(m)
