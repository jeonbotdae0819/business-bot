"""유니버스 전체에 scan()을 돌리는 공통 단계 (스크리너·백테스트 공용)."""
import pandas as pd

from .strategy import Params, relative_strength, scan


def run_universe(mkt: pd.DataFrame, prices: dict, p: Params, as_of=None):
    """
    반환: {티커: (signals, snapshot, df)}
    as_of를 주면 그 날짜까지의 데이터만 사용 (과거 시점 스크리닝 재현용).
    """
    if as_of is not None:
        as_of = pd.Timestamp(as_of)
        mkt = mkt.loc[:as_of]
        prices = {t: df.loc[:as_of] for t, df in prices.items()}

    closes = pd.DataFrame({t: df["Close"] for t, df in prices.items()}).reindex(mkt.index)
    rs = relative_strength(closes, p.rs_lookback)
    mkt_close = mkt["Close"]

    results = {}
    for tk, df in prices.items():
        df = df.loc[df.index.isin(mkt.index)]
        if len(df) < p.peak_window + 5:
            continue
        sigs, snap = scan(df, mkt_close.reindex(df.index), rs[tk].reindex(df.index), p)
        results[tk] = (sigs, snap, df)
    return results
