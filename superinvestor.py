"""
13F 기반 슈퍼인베스터 컨센서스 분석

① SEC EDGAR에서 유명 투자자들의 최신 13F(분기말 보유 종목)와 직전 분기 13F를 받아
② 새로 샀거나 비중을 늘린 종목을 찾고
③ 여러 투자자가 동시에 매수한 종목을 순위로 정리

단독 실행:
    python superinvestor.py          # 결과를 화면에 출력
    python superinvestor.py --send   # 카카오톡으로 전송
"""
import os
import sys
import time
import logging
import requests
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# =============================================
# 📈 설정 영역
# =============================================

# SEC는 연락처가 담긴 User-Agent를 요구함 (예: "홍길동 myemail@example.com")
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "")

# 추적할 슈퍼인베스터: (표시 이름, SEC CIK, EDGAR 등록명에 포함돼야 하는 단어)
# 등록명이 맞지 않으면 CIK 오류로 보고 해당 투자자는 건너뜀
SUPERINVESTORS = [
    ("워런 버핏 (버크셔)",        "0001067983", "BERKSHIRE HATHAWAY"),
    ("빌 애크먼 (퍼싱스퀘어)",     "0001336528", "PERSHING SQUARE"),
    ("테리 스미스 (펀드스미스)",    "0001569205", "FUNDSMITH"),
    ("척 애크리 (애크리)",         "0001112520", "AKRE"),
    ("세스 클라만 (바우포스트)",    "0001061768", "BAUPOST"),
    ("데이비드 테퍼 (아팔루사)",    "0001656456", "APPALOOSA"),
    ("리루 (히말라야)",            "0001709323", "HIMALAYA"),
    ("모니시 파브라이",            "0001549575", "DALAL STREET"),
    ("토머스 게이너 (마켈)",       "0001096343", "MARKEL"),
    ("론파인 캐피털",              "0001061165", "LONE PINE"),
    ("바이킹 글로벌",              "0001103804", "VIKING GLOBAL"),
    ("스탠리 드러켄밀러",          "0001536411", "DUQUESNE"),
    ("댄 로브 (서드포인트)",       "0001040273", "THIRD POINT"),
    ("데이비드 아인혼 (그린라이트)", "0001079114", "GREENLIGHT"),
    ("칼 아이칸",                 "0000921669", "ICAHN"),
    ("타이거 글로벌",              "0001167483", "TIGER GLOBAL"),
    ("코투 (필립 라퐁)",           "0001135730", "COATUE"),
    ("마이클 버리 (사이언)",       "0001649339", "SCION"),
    ("폴렌 캐피털",               "0001034524", "POLEN"),
    ("세쿼이아 펀드 (루앤커니프)",  "0001720792", "RUANE"),
    ("토머스 루소 (가드너루소)",    "0000860643", "GARDNER RUSSO"),
    ("밸류액트",                  "0001418814", "VALUEACT"),
    ("넬슨 펠츠 (트라이언)",       "0001345471", "TRIAN"),
    ("메이슨 호킨스 (사우스이스턴)", "0000807985", "SOUTHEASTERN"),
    ("트위디 브라운",              "0000732905", "TWEEDY"),
    ("야크트만",                  "0000905567", "YACKTMAN"),
    ("닷지 앤 콕스",              "0000200217", "DODGE"),
    ("빌 나이그렌 (오크마크)",      "0000813917", "HARRIS ASSOCIATES"),
    ("게이츠 재단",               "0001166559", "GATES FOUNDATION"),
    ("하워드 막스 (오크트리)",      "0000949509", "OAKTREE"),
    ("월리스 와이츠",              "0000883965", "WEITZ"),
    ("크리스 데이비스",            "0001036325", "DAVIS SELECTED"),
    ("퍼스트 이글",               "0001325447", "FIRST EAGLE"),
    ("브루스 버코위츠 (페어홈)",    "0001056831", "FAIRHOLME"),
]

ADD_THRESHOLD = 0.10       # 직전 분기 대비 주식 수가 10% 이상 늘면 '매수'로 집계
MIN_BUYERS    = 2          # 이 인원 이상이 동시에 매수한 종목만 컨센서스로 인정
TOP_BUYS      = 10         # 카톡으로 보낼 매수 컨센서스 종목 수
TOP_SELLS     = 5          # 카톡으로 보낼 매도 컨센서스 종목 수
REPORT_DAYS   = {(2, 16), (5, 16), (8, 16), (11, 16)}   # 13F 공시 마감(분기말+45일) 직후 전송일

# =============================================
log = logging.getLogger(__name__)

_last_request = 0.0


def sec_get(url: str):
    """SEC 요청 (초당 10회 제한을 넘지 않도록 간격 유지)"""
    global _last_request
    wait = 0.15 - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.time()
    resp = requests.get(url, headers={"User-Agent": SEC_USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp


def get_13f_filings(cik: str, expected_name: str) -> list:
    """투자자의 13F-HR(원본) 목록을 최신순으로 반환. 등록명이 다르면 빈 목록."""
    data = sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
    name = data.get("name", "")
    if expected_name.upper() not in name.upper():
        log.warning(f"CIK {cik} 등록명 불일치: '{name}' (기대: {expected_name}) → 건너뜀")
        return []

    recent = data.get("filings", {}).get("recent", {})
    filings = []
    for form, acc, filed, period in zip(recent.get("form", []), recent.get("accessionNumber", []),
                                        recent.get("filingDate", []), recent.get("reportDate", [])):
        if form == "13F-HR" and period:
            filings.append({"accession": acc, "filed": filed, "period": period})

    # 같은 분기에 원본이 여러 건이면 가장 늦게 낸 것만 사용
    filings.sort(key=lambda f: (f["period"], f["filed"]), reverse=True)
    by_period = {}
    for f in filings:
        by_period.setdefault(f["period"], f)
    return sorted(by_period.values(), key=lambda f: f["period"], reverse=True)


def parse_info_table(xml_text: str) -> dict:
    """13F 정보표 XML → {cusip: {"name", "shares", "value"}} (풋/콜·채권 제외, 같은 종목은 합산)"""
    root = ET.fromstring(xml_text)
    holdings = {}
    for row in root.iter():
        if row.tag.split("}")[-1] != "infoTable":
            continue
        fields = {}
        for el in row.iter():
            tag = el.tag.split("}")[-1]
            if el.text and el.text.strip():
                fields[tag] = el.text.strip()
        if fields.get("putCall") or fields.get("sshPrnamtType", "SH").upper() != "SH":
            continue
        cusip = fields.get("cusip", "").upper()
        if not cusip:
            continue
        h = holdings.setdefault(cusip, {"name": fields.get("nameOfIssuer", cusip), "shares": 0.0, "value": 0.0})
        h["shares"] += float(fields.get("sshPrnamt", 0) or 0)
        h["value"] += float(fields.get("value", 0) or 0)
    return holdings


def get_holdings(cik: str, accession: str) -> dict:
    """공시 폴더에서 정보표 XML을 찾아 보유 종목을 읽어옴"""
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"
    items = sec_get(f"{base}/index.json").json().get("directory", {}).get("item", [])
    for item in items:
        name = item.get("name", "")
        if not name.lower().endswith(".xml") or name.lower() == "primary_doc.xml":
            continue
        text = sec_get(f"{base}/{name}").text
        if "informationtable" in text[:2000].lower():
            return parse_info_table(text)
    log.warning(f"정보표를 찾지 못함: {base}")
    return {}


def classify_changes(current: dict, previous: dict) -> dict:
    """{cusip: "new" | "add" | "reduce" | "sold"} — 주식 분할로 수량이 튄 경우는 보정"""
    changes = {}
    for cusip, cur in current.items():
        prev = previous.get(cusip)
        if not prev or prev["shares"] <= 0:
            changes[cusip] = "new"
            continue
        share_ratio = cur["shares"] / prev["shares"]
        if share_ratio > 0 and cur["value"] > 0 and prev["value"] > 0:
            # 수량이 k배, 주당 평가액이 1/k배로 함께 변했으면 k:1 분할(병합은 그 반대)로 보고 보정
            price_ratio = (cur["value"] / cur["shares"]) / (prev["value"] / prev["shares"])
            for k in (share_ratio, 1 / share_ratio):
                n = round(k)
                if n >= 2 and abs(k / n - 1) < 0.15 and abs(price_ratio * share_ratio - 1) < 0.5:
                    share_ratio = 1.0
                    break
        change = share_ratio - 1
        if change >= ADD_THRESHOLD:
            changes[cusip] = "add"
        elif change <= -ADD_THRESHOLD:
            changes[cusip] = "reduce"
    for cusip in previous:
        if cusip not in current:
            changes[cusip] = "sold"
    return changes


def lookup_tickers(cusips: list) -> dict:
    """OpenFIGI로 CUSIP → 티커 변환 (실패하면 빈 값, 키 없이 요청당 10개)"""
    tickers = {}
    for i in range(0, len(cusips), 10):
        batch = cusips[i:i + 10]
        try:
            resp = requests.post(
                "https://api.openfigi.com/v3/mapping",
                json=[{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in batch],
                timeout=20,
            )
            if resp.status_code != 200:
                log.warning(f"OpenFIGI 오류 {resp.status_code}")
                continue
            for cusip, result in zip(batch, resp.json()):
                data = result.get("data") or []
                if data and data[0].get("ticker"):
                    tickers[cusip] = data[0]["ticker"]
        except Exception as e:
            log.warning(f"OpenFIGI 오류: {e}")
        time.sleep(2.5)   # 키 없이 분당 25회 제한
    return tickers


def analyze() -> dict:
    """모든 투자자의 최신·직전 13F를 비교해 컨센서스 매수/매도 종목을 계산"""
    if not SEC_USER_AGENT:
        raise RuntimeError("SEC_USER_AGENT 환경변수가 필요합니다 (예: '홍길동 myemail@example.com')")

    investors = []
    for label, cik, expected in SUPERINVESTORS:
        try:
            filings = get_13f_filings(cik, expected)
            if filings:
                investors.append({"label": label, "cik": cik, "filings": filings})
        except Exception as e:
            log.warning(f"{label} 공시 목록 조회 실패: {e}")

    # 절반 이상이 공시를 마친 가장 최근 분기를 기준으로 삼음 (일찍 낸 일부만으로 판단하지 않도록)
    period_counts = Counter(f["period"] for inv in investors for f in inv["filings"])
    target = next((p for p in sorted(period_counts, reverse=True) if period_counts[p] * 2 >= len(investors)), None)
    if not target:
        raise RuntimeError("분석할 13F 공시를 찾지 못했습니다")

    stocks = {}
    analyzed = 0
    for inv in investors:
        cur_filing = next((f for f in inv["filings"] if f["period"] == target), None)
        prev_filing = next((f for f in inv["filings"] if f["period"] < target), None)
        if not cur_filing or not prev_filing:
            continue
        try:
            current = get_holdings(inv["cik"], cur_filing["accession"])
            previous = get_holdings(inv["cik"], prev_filing["accession"])
        except Exception as e:
            log.warning(f"{inv['label']} 보유 종목 조회 실패: {e}")
            continue
        if not current:
            continue
        analyzed += 1

        for cusip, change in classify_changes(current, previous).items():
            name = (current.get(cusip) or previous.get(cusip))["name"]
            s = stocks.setdefault(cusip, {"cusip": cusip, "name": name, "buyers": [], "sellers": [], "holders": 0})
            if change in ("new", "add"):
                s["buyers"].append((inv["label"], change == "new"))
            else:
                s["sellers"].append((inv["label"], change == "sold"))
        for cusip, h in current.items():
            stocks.setdefault(cusip, {"cusip": cusip, "name": h["name"], "buyers": [], "sellers": [], "holders": 0})
            stocks[cusip]["holders"] += 1

    def new_count(people):
        return sum(1 for _, flag in people if flag)

    buys = sorted((s for s in stocks.values() if len(s["buyers"]) >= MIN_BUYERS),
                  key=lambda s: (len(s["buyers"]), new_count(s["buyers"]), s["holders"]), reverse=True)[:TOP_BUYS]
    sells = sorted((s for s in stocks.values() if len(s["sellers"]) >= MIN_BUYERS),
                   key=lambda s: (len(s["sellers"]), new_count(s["sellers"])), reverse=True)[:TOP_SELLS]

    tickers = lookup_tickers([s["cusip"] for s in buys + sells])
    for s in buys + sells:
        s["ticker"] = tickers.get(s["cusip"], "")

    return {"period": target, "analyzed": analyzed, "buys": buys, "sells": sells}


def quarter_label(period: str) -> str:
    d = datetime.strptime(period, "%Y-%m-%d")
    return f"{d.year}년 {(d.month - 1) // 3 + 1}분기"


def format_messages(result: dict) -> list:
    """카톡 전송용 메시지 목록 (종목별로 한 통씩)"""
    def title(s):
        return f"{s['name']} ({s['ticker']})" if s["ticker"] else s["name"]

    messages = [
        f"🧠 슈퍼인베스터 컨센서스\n"
        f"📅 {quarter_label(result['period'])} 13F (기준일 {result['period']})\n"
        f"👥 분석 투자자 {result['analyzed']}명\n"
        f"여러 거물이 동시에 산 종목 TOP {len(result['buys'])}"
    ]

    if not result["buys"]:
        messages.append(f"{MIN_BUYERS}명 이상이 동시에 매수한 종목이 없어요.")
    for i, s in enumerate(result["buys"], 1):
        new = sum(1 for _, is_new in s["buyers"] if is_new)
        names = ", ".join(f"{label}{'(신규)' if is_new else ''}" for label, is_new in s["buyers"])
        messages.append(
            f"{i}. {title(s)}\n"
            f"🛒 매수 {len(s['buyers'])}명 (신규 {new}) · 📦 보유 {s['holders']}명\n"
            f"{names}"
        )

    if result["sells"]:
        lines = [f"📉 여러 거물이 동시에 판 종목"]
        for i, s in enumerate(result["sells"], 1):
            sold = sum(1 for _, is_sold in s["sellers"] if is_sold)
            lines.append(f"{i}. {title(s)} — 매도 {len(s['sellers'])}명 (전량 {sold})")
        messages.append("\n".join(lines))

    messages.append("※ 13F는 분기말 보유 내역을 최대 45일 늦게 공개합니다. 그대로 따라 사기보다 종목 발굴용으로 활용하세요.")
    return messages


def build_report() -> list:
    return format_messages(analyze())


def is_report_day(today: datetime = None) -> bool:
    today = today or datetime.now()
    return (today.month, today.day) in REPORT_DAYS


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    messages = build_report()
    if "--send" in sys.argv:
        from main import send_kakao
        for msg in messages:
            send_kakao(msg)
            time.sleep(1)
    else:
        print("\n\n".join(messages))
