"""
13F 슈퍼인베스터 컨센서스 스크리너

방법
1. SEC EDGAR 원본에서 투자자별 최근 2개 분기 13F-HR(정정본 제외)을 받는다
2. 종목(CUSIP)별로 직전 분기 대비 NEW(신규) / ADD(10% 이상 증가) / REDUCE / SOLD 판정
   - 옵션(Put/Call) 행은 제외, 주식 수(SH) 기준
3. 여러 투자자가 동시에 신규·추가 매수한 종목을 순위화

출력 (output/ 폴더)
- 13f_positions.csv : 투자자별 전체 포지션 변화 (비중 포함)
- 13f_consensus.csv : 종목별 매수 투자자 수, 보유 투자자 수, 평균 비중, 매수자 명단

주의
- 13F는 분기말 기준이고 분기 종료 후 최대 45일 뒤 공개된다 (현재 보유와 다를 수 있음)
- 미국 상장 롱 포지션만 공개되며 공매도·해외주식·채권은 빠진다

설치: pip install pandas requests
사용: SEC_USER_AGENT="이름 이메일" python superinvestor_13f.py [최소매수자수=2]
      (SEC는 연락처가 담긴 User-Agent를 요구한다)
"""
import os
import sys
import time
import xml.etree.ElementTree as ET

import pandas as pd
import requests

# (표시 이름, CIK, 제출자명 확인용 키워드)
SUPERINVESTORS = [
    ("Warren Buffett - Berkshire Hathaway", "0001067983", "BERKSHIRE"),
    ("Bill Ackman - Pershing Square", "0001336528", "PERSHING"),
    ("David Tepper - Appaloosa", "0001656456", "APPALOOSA"),
    ("Seth Klarman - Baupost", "0001061768", "BAUPOST"),
    ("Li Lu - Himalaya Capital", "0001709323", "HIMALAYA"),
    ("Mohnish Pabrai - Dalal Street", "0001549575", "DALAL"),
    ("Brad Gerstner - Altimeter", "0001541617", "ALTIMETER"),
    ("Stanley Druckenmiller - Duquesne", "0001536411", "DUQUESNE"),
    ("Dan Loeb - Third Point", "0001040273", "THIRD POINT"),
    ("David Einhorn - Greenlight", "0001079114", "GREENLIGHT"),
    ("Chuck Akre - Akre Capital", "0001112520", "AKRE"),
    ("Chase Coleman - Tiger Global", "0001167483", "TIGER GLOBAL"),
    ("Philippe Laffont - Coatue", "0001135730", "COATUE"),
    ("Stephen Mandel - Lone Pine", "0001061165", "LONE PINE"),
    ("Andreas Halvorsen - Viking Global", "0001103804", "VIKING"),
    ("Carl Icahn", "0000921669", "ICAHN"),
    ("Bill & Melinda Gates Foundation Trust", "0001166559", "GATES"),
    ("Tom Gayner - Markel", "0001096343", "MARKEL"),
    ("Terry Smith - Fundsmith", "0001569205", "FUNDSMITH"),
    ("Howard Marks - Oaktree", "0000949509", "OAKTREE"),
    ("George Soros - Soros Fund Mgmt", "0001029160", "SOROS"),
]

ADD_THRESHOLD = 0.10
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def session():
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        sys.exit('SEC_USER_AGENT 환경변수를 설정하세요. 예: SEC_USER_AGENT="Hong Gildong hong@example.com"')
    s = requests.Session()
    s.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
    return s


def get(s, url, as_json=False):
    time.sleep(0.15)  # SEC 정책: 초당 10회 이하
    r = s.get(url, timeout=30)
    r.raise_for_status()
    return r.json() if as_json else r.content


def latest_two_filings(s, cik):
    sub = get(s, f"https://data.sec.gov/submissions/CIK{cik}.json", as_json=True)
    rec = sub["filings"]["recent"]
    seen, out = set(), []
    for form, acc, period, filed in zip(rec["form"], rec["accessionNumber"], rec["reportDate"], rec["filingDate"]):
        if form == "13F-HR" and period not in seen:
            seen.add(period)
            out.append({"accession": acc, "period": period, "filed": filed})
        if len(out) == 2:
            break
    return sub.get("name", ""), out


def parse_info_table(xml_bytes):
    root = ET.fromstring(xml_bytes)
    rows = []
    for el in root.iter():
        if not el.tag.endswith("infoTable"):
            continue
        f = {c.tag.split("}")[-1]: c for c in el.iter()}
        if f.get("putCall") is not None and (f["putCall"].text or "").strip():
            continue
        if f.get("sshPrnamtType") is not None and f["sshPrnamtType"].text.strip() != "SH":
            continue
        rows.append({
            "cusip": f["cusip"].text.strip().upper(),
            "issuer": f["nameOfIssuer"].text.strip(),
            "class": (f["titleOfClass"].text or "").strip() if "titleOfClass" in f else "",
            "value": float(f["value"].text),
            "shares": float(f["sshPrnamt"].text),
        })
    df = pd.DataFrame(rows, columns=["cusip", "issuer", "class", "value", "shares"])
    return df.groupby("cusip").agg(issuer=("issuer", "first"), cls=("class", "first"),
                                   value=("value", "sum"), shares=("shares", "sum"))


def holdings(s, cik, accession):
    folder = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"
    idx = get(s, folder + "/index.json", as_json=True)
    xmls = [i["name"] for i in idx["directory"]["item"]
            if i["name"].lower().endswith(".xml") and i["name"].lower() != "primary_doc.xml"]
    for name in xmls:
        df = parse_info_table(get(s, f"{folder}/{name}"))
        if len(df):
            return df
    raise RuntimeError(f"정보 테이블 XML 없음: {folder}")


def compare(cur, prev):
    df = cur.join(prev[["shares"]].rename(columns={"shares": "prev_shares"}), how="outer")
    df["issuer"] = df["issuer"].fillna(prev["issuer"])
    df[["shares", "prev_shares", "value"]] = df[["shares", "prev_shares", "value"]].fillna(0)
    total = df["value"].sum()
    df["weight_pct"] = df["value"] / total * 100 if total else 0

    def status(r):
        if r.prev_shares == 0 and r.shares > 0:
            return "NEW"
        if r.shares == 0:
            return "SOLD"
        chg = r.shares / r.prev_shares - 1
        if chg >= ADD_THRESHOLD:
            return "ADD"
        if chg <= -ADD_THRESHOLD:
            return "REDUCE"
        return "HOLD"

    df["status"] = df.apply(status, axis=1)
    df["share_chg_pct"] = (df["shares"] / df["prev_shares"].replace(0, pd.NA) - 1) * 100
    return df


def main(min_buyers=2):
    s = session()
    frames = []
    for label, cik, keyword in SUPERINVESTORS:
        try:
            name, filings = latest_two_filings(s, cik)
            if keyword not in name.upper():
                print(f"[경고] {label}: CIK {cik} 제출자명이 '{name}' → 건너뜀 (CIK 확인 필요)")
                continue
            if len(filings) < 2:
                print(f"[건너뜀] {label}: 13F-HR 2개 분기 미만")
                continue
            cur, prev = filings
            df = compare(holdings(s, cik, cur["accession"]), holdings(s, cik, prev["accession"]))
            df["investor"] = label
            df["period"] = cur["period"]
            df["prev_period"] = prev["period"]
            df["filed"] = cur["filed"]
            df["accession"] = cur["accession"]
            frames.append(df.reset_index())
            print(f"[완료] {label}: {cur['period']} (제출 {cur['filed']}), 종목 {int((df.shares > 0).sum())}개")
        except Exception as e:
            print(f"[실패] {label}: {e}")

    if not frames:
        sys.exit("수집된 데이터가 없습니다.")
    pos = pd.concat(frames, ignore_index=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    pos.to_csv(os.path.join(OUT_DIR, "13f_positions.csv"), index=False, encoding="utf-8-sig")

    held = pos[pos["shares"] > 0]
    buys = pos[pos["status"].isin(["NEW", "ADD"])]
    cons = pd.DataFrame({
        "issuer": held.groupby("cusip")["issuer"].first(),
        "holders": held.groupby("cusip")["investor"].nunique(),
        "avg_weight_pct": held.groupby("cusip")["weight_pct"].mean().round(2),
    }).join(pd.DataFrame({
        "buyers": buys.groupby("cusip")["investor"].nunique(),
        "new_buyers": buys[buys["status"] == "NEW"].groupby("cusip")["investor"].nunique(),
        "buyer_list": buys.assign(tag=[f"{r.investor.split(' - ')[0]}({r.status},{r.weight_pct:.1f}%)"
                                       for r in buys.itertuples()]).groupby("cusip")["tag"].agg("; ".join),
        "sellers": pos[pos["status"].isin(["SOLD", "REDUCE"])].groupby("cusip")["investor"].nunique(),
    }), how="outer").fillna({"buyers": 0, "new_buyers": 0, "sellers": 0, "holders": 0})
    cons = cons[cons["buyers"] >= min_buyers].sort_values(["buyers", "avg_weight_pct"], ascending=False)
    cons.to_csv(os.path.join(OUT_DIR, "13f_consensus.csv"), encoding="utf-8-sig")
    print(f"\n동시 매수(≥{min_buyers}명) 종목 {len(cons)}개 → output/13f_consensus.csv")
    print(cons.head(30).to_string())


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 2)
