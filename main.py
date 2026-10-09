import os
import requests
import time
import schedule
import logging
import random
import re
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
import superinvestor

load_dotenv()  # 같은 폴더의 .env 파일에서 비밀 값을 읽어옴 (.env는 git에 올라가지 않음)

# =============================================
# ✈️ 설정 영역 - 값은 .env 파일에서 관리합니다 (아래 코드는 수정할 필요 없음)
# =============================================

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SERPAPI_KEY      = os.getenv("SERPAPI_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")   # 뉴스 해설 생성용 (https://console.anthropic.com)
ANTHROPIC_MODEL   = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

# 카카오톡 "나에게 보내기" 설정 (일일 뉴스 브리핑 전송용, https://developers.kakao.com)
KAKAO_REST_API_KEY  = os.getenv("KAKAO_REST_API_KEY", "")
KAKAO_REDIRECT_URI  = os.getenv("KAKAO_REDIRECT_URI", "")
KAKAO_REFRESH_TOKEN = os.getenv("KAKAO_REFRESH_TOKEN", "")   # 최초 인가 코드 교환 후 발급받은 값

# 노선 설정 (유럽 직항만)
ROUTES = {
    "유럽": [
        ("ICN", "LHR"),  # 런던 (대한항공 직항)
        ("ICN", "CDG"),  # 파리 (대한항공 직항)
        ("ICN", "FRA"),  # 프랑크푸르트 (대한항공 직항)
        ("ICN", "AMS"),  # 암스테르담 (대한항공 직항)
        ("ICN", "BCN"),  # 바르셀로나 (대한항공 직항)
        ("ICN", "VIE"),  # 빈 (대한항공 직항)
        ("ICN", "ZRH"),  # 취리히 (대한항공 직항)
        ("ICN", "CPH"),  # 코펜하겐 (대한항공 직항)
    ],
}

# 가격 알림 기준
PRICE_LIMIT = {
    "유럽": 1_500_000,   # 150만원 이하
}
DISCOUNT_THRESHOLD = 0.30   # 평균 대비 30% 이상 저렴할 때

# 검색 설정
SEARCH_MONTHS_AHEAD    = 6
CHECK_INTERVAL_MINUTES = 60   # 직항 + 유럽만이라 횟수 줄어서 60분으로 여유있게
DAILY_BRIEFING_TIME    = "08:30"   # 매일 이 시간에 AI/LLM/테크 뉴스 브리핑 전송 (서버 타임존 기준)
SUPERINVESTOR_TIME     = "09:00"   # 13F 공시 마감 직후(2·5·8·11월 16일) 이 시간에 슈퍼인베스터 컨센서스 전송

# 관심 주제 뉴스 브리핑 설정 (카테고리별로 골고루 선별)
NEWS_CATEGORIES = [
    {
        "label": "🔬 연구자 관점 기술 뉴스",
        "query": "AI OR LLM 신규 모델 OR 논문 OR 벤치마크 OR 오픈소스",
        "count": 1,
    },
    {
        "label": "💰 투자·산업 기회",
        "query": "AI OR LLM 투자 OR 펀딩 OR 인수합병 OR 산업 동향",
        "count": 1,
    },
    {
        "label": "🚨 중요도 높은 핵심 이슈",
        "query": "AI OR LLM 발표 OR 정책 OR 규제 OR 주요 이슈",
        "count": 2,
    },
    {
        "label": "🔥 대중 콘텐츠 소재",
        "query": "AI 화제 OR 논란 OR 밈 OR 바이럴 OR 유행",
        "count": 1,
    },
]

# 뉴스 해설을 이 관점/눈높이에 맞춰 생성
USER_PROFILE = (
    "AI, LLM, 테크 분야를 깊게는 모르지만 관심이 많은 초보자. "
    "비즈니스/투자 관점에서 이 소식이 자신에게 어떤 의미가 있는지 알고 싶어함."
)

# =============================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

already_notified = set()
price_history = {}


def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        if r.status_code == 200:
            log.info("텔레그램 전송 성공")
        else:
            log.warning(f"텔레그램 실패: {r.status_code} {r.text}")
    except Exception as e:
        log.error(f"텔레그램 오류: {e}")


def get_kakao_access_token() -> str:
    """refresh_token으로 카카오 access_token 발급 (access_token은 6시간마다 만료)"""
    try:
        resp = requests.post(
            "https://kauth.kakao.com/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": KAKAO_REST_API_KEY,
                "refresh_token": KAKAO_REFRESH_TOKEN,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning(f"카카오 토큰 갱신 실패: {resp.status_code} {resp.text}")
            return ""
        return resp.json().get("access_token", "")
    except Exception as e:
        log.error(f"카카오 토큰 갱신 오류: {e}")
        return ""


def send_kakao(text: str, link_url: str = None, max_len: int = 190):
    """카카오톡 '나에게 보내기'로 텍스트 메시지 전송
    (건당 글자수 제한이 있어 내용을 자르지 않고 여러 통으로 나눠 전송)"""
    access_token = get_kakao_access_token()
    if not access_token:
        log.warning("카카오 access_token 발급 실패로 전송 생략")
        return

    plain_text = re.sub(r"<[^>]+>", "", text).strip()
    chunks = [plain_text[i:i + max_len] for i in range(0, len(plain_text), max_len)] or [""]

    for chunk in chunks:
        template = {
            "object_type": "text",
            "text": chunk,
            "link": {
                "web_url": link_url or "https://developers.kakao.com",
                "mobile_web_url": link_url or "https://developers.kakao.com",
            },
        }

        try:
            resp = requests.post(
                "https://kapi.kakao.com/v2/api/talk/memo/default/send",
                headers={"Authorization": f"Bearer {access_token}"},
                data={"template_object": json.dumps(template)},
                timeout=10,
            )
            if resp.status_code == 200:
                log.info("카카오톡 전송 성공")
            else:
                log.warning(f"카카오톡 전송 실패: {resp.status_code} {resp.text}")
        except Exception as e:
            log.error(f"카카오톡 오류: {e}")

        time.sleep(0.5)


def send_startup_message():
    msg = (
        f"✈️ <b>유럽 비즈니스석 직항 알림봇 시작!</b>\n\n"
        f"📍 모니터링 노선: 런던·파리·프랑크푸르트·암스테르담·바르셀로나·빈·취리히·코펜하겐\n"
        f"💺 좌석: 비즈니스석 직항만\n"
        f"💰 알림 조건:\n"
        f"  · 150만원 이하\n"
        f"  · 또는 평균 대비 30% 이상 저렴\n"
        f"⏱ 체크 주기: {CHECK_INTERVAL_MINUTES}분마다\n"
        f"🤖 AI/LLM/테크 뉴스 브리핑: 매일 {DAILY_BRIEFING_TIME} (카카오톡)\n"
        f"🧠 슈퍼인베스터 13F 컨센서스: 2·5·8·11월 16일 {SUPERINVESTOR_TIME} (카카오톡)\n\n"
        f"특가 나오면 바로 알려드릴게요! 🔔"
    )
    send_telegram(msg)


def get_search_dates():
    dates = []
    today = datetime.today()
    for m in range(SEARCH_MONTHS_AHEAD):
        d = today + timedelta(days=30 * m)
        dates.append(d.strftime("%Y-%m-%d"))
    return dates


def fetch_flight_price(origin: str, destination: str, date: str) -> list:
    """SerpAPI Google Flights 직항 비즈니스석 조회"""
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": date,
        "currency": "KRW",
        "hl": "ko",
        "travel_class": "3",    # 3=비즈니스
        "type": "2",            # 2=편도
        "stops": "0",           # 0=직항만
        "adults": "1",
        "api_key": SERPAPI_KEY,
    }

    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=20)
        if resp.status_code != 200:
            log.warning(f"SerpAPI 오류 {resp.status_code}: {origin}→{destination}")
            return []

        data = resp.json()
        results = []

        for section in ["best_flights", "other_flights"]:
            for flight in data.get(section, []):
                # 직항 한번 더 확인 (경유 없음)
                if len(flight.get("layovers", [])) > 0:
                    continue

                price = flight.get("price")
                airline = flight.get("flights", [{}])[0].get("airline", "")
                duration = flight.get("total_duration", 0)

                if price:
                    results.append({
                        "price": price,
                        "airline": airline,
                        "duration": duration,
                    })

        return results

    except Exception as e:
        log.warning(f"가격 조회 오류 {origin}→{destination}: {e}")
        return []


def get_average_price(key: str):
    history = price_history.get(key, [])
    if len(history) < 3:
        return None
    return sum(history) / len(history)


def update_price_history(key: str, price: int):
    if key not in price_history:
        price_history[key] = []
    price_history[key].append(price)
    if len(price_history[key]) > 20:
        price_history[key] = price_history[key][-20:]


def fetch_news_for_query(query: str, count: int) -> list:
    """SerpAPI Google News로 주어진 쿼리에 대한 뉴스 조회"""
    params = {
        "engine": "google_news",
        "q": query,
        "hl": "ko",
        "gl": "kr",
        "api_key": SERPAPI_KEY,
    }

    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=20)
        if resp.status_code != 200:
            log.warning(f"뉴스 조회 오류 {resp.status_code}: {query}")
            return []

        data = resp.json()
        articles = []

        for item in data.get("news_results", []):
            title = item.get("title")
            link = item.get("link")
            if not title or not link:
                continue

            source = item.get("source")
            source_name = source.get("name") if isinstance(source, dict) else source

            articles.append({
                "title": title,
                "link": link,
                "source": source_name or "",
                "snippet": item.get("snippet", ""),
            })

            if len(articles) >= count:
                break

        return articles

    except Exception as e:
        log.warning(f"뉴스 조회 오류: {e}")
        return []


def fetch_tech_news() -> list:
    """카테고리별(연구·투자·핵심이슈·대중소재)로 AI/LLM/테크 뉴스를 골고루 선별"""
    seen_links = set()
    articles = []

    for category in NEWS_CATEGORIES:
        picked = 0
        # 중복 제거를 감안해 필요 개수보다 넉넉히 조회
        for item in fetch_news_for_query(category["query"], category["count"] * 3):
            if item["link"] in seen_links:
                continue
            seen_links.add(item["link"])
            item["category"] = category["label"]
            articles.append(item)
            picked += 1
            if picked >= category["count"]:
                break
        time.sleep(random.uniform(1, 2))

    return articles


def explain_article(article: dict) -> str:
    """Claude API로 뉴스에 대한 초보자 친화적 심층 해설 + 개인화된 시사점 생성"""
    if not ANTHROPIC_API_KEY:
        return ""

    prompt = (
        f"다음 AI/LLM/테크 뉴스를 독자에게 설명해줘.\n\n"
        f"제목: {article['title']}\n"
        f"요약: {article.get('snippet') or '(제공된 요약 없음, 제목 기반으로 설명)'}\n\n"
        f"독자 특징: {USER_PROFILE}\n\n"
        f"요청사항:\n"
        f"1. 전문 용어를 풀어 쓰면서 이 소식이 기술적으로 무엇을 의미하는지 깊이 있게, "
        f"하지만 초보자도 이해할 수 있게 3~5문장으로 설명해줘.\n"
        f"2. 이 소식이 독자의 관점(비즈니스/투자)에서 어떤 시사점이 있는지 1~2문장으로 짚어줘.\n\n"
        f"아래 형식을 그대로 지켜서 답해줘 (다른 텍스트 추가 금지):\n"
        f"📖 해설: ...\n"
        f"💡 시사점: ..."
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if resp.status_code != 200:
            log.warning(f"해설 생성 오류 {resp.status_code}: {resp.text}")
            return ""

        data = resp.json()
        return data["content"][0]["text"].strip()

    except Exception as e:
        log.warning(f"해설 생성 오류: {e}")
        return ""


def send_daily_briefing():
    """관심 주제(AI, LLM, 테크)에 대한 카테고리별 일일 뉴스 브리핑을 카카오톡으로 전송
    (카카오 텍스트 메시지는 건당 글자수 제한이 있어 기사별로 나눠서 전송)"""
    log.info("===== AI/LLM/테크 일일 브리핑 전송 (카카오톡) =====")

    header = f"🤖 AI·LLM·테크 일일 브리핑\n📅 {datetime.now().strftime('%Y-%m-%d')}"
    send_kakao(header)

    articles = fetch_tech_news()

    if not articles:
        send_kakao("오늘은 가져올 소식이 없어요.")
    else:
        for i, article in enumerate(articles, 1):
            explanation = explain_article(article)

            body = f"{i}. [{article['category']}]\n{article['title']}"
            if explanation:
                body += f"\n{explanation}"

            send_kakao(body, link_url=article["link"])
            time.sleep(random.uniform(1, 2))

    log.info("일일 브리핑 전송 완료")


def send_superinvestor_report():
    """13F 공시 마감 직후에만 슈퍼인베스터 컨센서스 종목을 카카오톡으로 전송"""
    if not superinvestor.is_report_day():
        return

    log.info("===== 슈퍼인베스터 13F 컨센서스 분석 (카카오톡) =====")
    try:
        messages = superinvestor.build_report()
    except Exception as e:
        log.error(f"슈퍼인베스터 분석 오류: {e}")
        return

    for msg in messages:
        send_kakao(msg, link_url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=13F-HR")
        time.sleep(random.uniform(1, 2))

    log.info("슈퍼인베스터 컨센서스 전송 완료")


def check_all_routes():
    log.info("===== 전체 노선 체크 시작 =====")
    dates = get_search_dates()

    for region, routes in ROUTES.items():
        price_limit = PRICE_LIMIT[region]

        for origin, destination in routes:
            for date in dates:
                log.info(f"체크 중: {origin}→{destination} ({date})")

                flights = fetch_flight_price(origin, destination, date)

                if not flights:
                    time.sleep(random.uniform(2, 4))
                    continue

                min_flight = min(flights, key=lambda x: x["price"])
                price = min_flight["price"]
                history_key = f"{origin}-{destination}"

                update_price_history(history_key, price)
                avg_price = get_average_price(history_key)

                condition_1 = price <= price_limit
                condition_2 = avg_price and price <= avg_price * (1 - DISCOUNT_THRESHOLD)

                if condition_1 or condition_2:
                    notify_key = f"{origin}-{destination}-{date}-{price}"
                    if notify_key in already_notified:
                        time.sleep(random.uniform(2, 4))
                        continue

                    already_notified.add(notify_key)

                    date_fmt = datetime.strptime(date, "%Y-%m-%d").strftime("%Y.%m.%d")
                    price_fmt = f"{price:,}원"
                    duration_h = min_flight["duration"] // 60
                    duration_m = min_flight["duration"] % 60

                    reasons = []
                    if condition_1:
                        reasons.append(f"기준가 {price_limit:,}원 이하")
                    if condition_2:
                        discount_pct = int((1 - price / avg_price) * 100)
                        reasons.append(f"평균가 대비 {discount_pct}% 저렴")

                    msg = (
                        f"🚨 <b>유럽 비즈니스 직항 특가!</b>\n\n"
                        f"✈️ 노선: {origin} → {destination} [{region}] 직항\n"
                        f"📅 날짜: {date_fmt}\n"
                        f"💰 가격: <b>{price_fmt}</b>\n"
                        f"🏢 항공사: {min_flight['airline']}\n"
                        f"⏱ 비행시간: {duration_h}시간 {duration_m}분\n"
                        f"📊 알림 이유: {', '.join(reasons)}\n"
                        + (f"📈 평균가: {avg_price:,.0f}원\n" if avg_price else "")
                        + f"\n👉 <a href='https://www.google.com/flights?hl=ko#flt={origin}.{destination}.{date};c:KRW;e:1;sd:1;t:b'>Google Flights에서 예약하기</a>\n\n"
                        f"⏰ 발견: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    send_telegram(msg)
                    log.info(f"알림 전송: {origin}→{destination} {date} {price_fmt}")

                time.sleep(random.uniform(3, 6))

    log.info("===== 체크 완료 =====\n")


if __name__ == "__main__":
    log.info("유럽 비즈니스 직항 알림봇 시작")
    send_startup_message()
    check_all_routes()

    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(check_all_routes)
    schedule.every().day.at(DAILY_BRIEFING_TIME).do(send_daily_briefing)
    schedule.every().day.at(SUPERINVESTOR_TIME).do(send_superinvestor_report)
    while True:
        schedule.run_pending()
        time.sleep(30)
