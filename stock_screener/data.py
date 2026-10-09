"""
가격 데이터 수집 (yfinance) + 로컬 캐시

유니버스: 현재 S&P 500 + Nasdaq-100 구성 종목 (위키백과)
※ '현재' 구성 종목만 쓰기 때문에 과거 백테스트에는 생존 편향이 있습니다
  (2008년 이후 상장폐지·지수 편출된 종목이 빠져 있어 결과가 실제보다 좋게 나올 수 있음).
"""
import hashlib
import time
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

CACHE_DIR = Path(__file__).resolve().parent / "data_cache"
CACHE_MAX_AGE_HOURS = 12
UA = {"User-Agent": "Mozilla/5.0 (stock-screener; research)"}

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NDX_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"


def _read_tables(url: str):
    resp = requests.get(url, headers=UA, timeout=30)
    resp.raise_for_status()
    return pd.read_html(StringIO(resp.text))


def get_universe() -> list[str]:
    tickers = set()
    for t in _read_tables(SP500_URL):
        if "Symbol" in t.columns:
            tickers.update(t["Symbol"].astype(str))
            break
    for t in _read_tables(NDX_URL):
        col = next((c for c in ("Ticker", "Symbol") if c in t.columns), None)
        if col and len(t) > 90:
            tickers.update(t[col].astype(str))
            break
    # yfinance 표기: BRK.B → BRK-B
    return sorted(s.strip().replace(".", "-") for s in tickers if s.strip())


def load_tickers_file(path: str) -> list[str]:
    text = Path(path).read_text(encoding="utf-8")
    return sorted({s.strip().upper() for s in text.replace(",", "\n").splitlines() if s.strip()})


def _cache_path(tickers, start, end) -> Path:
    key = hashlib.md5(f"{','.join(sorted(tickers))}|{start}|{end}".encode()).hexdigest()[:12]
    return CACHE_DIR / f"prices_{key}.pkl"


def download_prices(tickers, start="2005-01-01", end=None, refresh=False, chunk=100) -> dict[str, pd.DataFrame]:
    """{티커: OHLCV DataFrame} (수정주가). 받은 데이터는 data_cache/에 저장해 재사용."""
    import yfinance as yf

    CACHE_DIR.mkdir(exist_ok=True)
    path = _cache_path(tickers, start, end)
    if not refresh and path.exists() and time.time() - path.stat().st_mtime < CACHE_MAX_AGE_HOURS * 3600:
        return pd.read_pickle(path)

    out = {}
    tickers = list(tickers)
    for i in range(0, len(tickers), chunk):
        batch = tickers[i:i + chunk]
        print(f"  가격 다운로드 {i + 1}-{i + len(batch)} / {len(tickers)}")
        raw = yf.download(batch, start=start, end=end, auto_adjust=True, group_by="ticker",
                          progress=False, threads=True)
        for tk in batch:
            try:
                df = raw[tk] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
            df = df[(df["Close"] > 0) & (df["High"] >= df["Low"])]
            if len(df) > 0:
                df.index = pd.DatetimeIndex(df.index).tz_localize(None)
                out[tk] = df
    pd.to_pickle(out, path)
    return out


def load_market_data(market: str, tickers=None, tickers_file=None, start="2005-01-01", end=None, refresh=False):
    """(시장지수 DataFrame, {티커: DataFrame}) 반환."""
    if tickers is None:
        tickers = load_tickers_file(tickers_file) if tickers_file else get_universe()
    print(f"유니버스 {len(tickers)}개 종목, 시장 지수 {market}")
    prices = download_prices(sorted(set(tickers) | {market}), start=start, end=end, refresh=refresh)
    if market not in prices:
        raise RuntimeError(f"시장 지수 {market} 데이터를 받지 못했습니다.")
    mkt = prices.pop(market)
    return mkt, prices
