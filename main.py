import requests
import time
import schedule
import logging
import random
from datetime import datetime, timedelta

# =============================================
# ✈️ 설정 영역 - 여기만 수정하세요!
# =============================================

TELEGRAM_TOKEN   = "8782698522:AAGv4f0KxG9Yc5YEFlpxsrgCxTHRXO3yFBI"
TELEGRAM_CHAT_ID = "1192284673"
SERPAPI_KEY      = "dc3c9fedb08fffde8af1f00fa2da7217457ef84561cbe103a5d0600b12f855e1"

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


def send_startup_message():
    msg = (
        f"✈️ <b>유럽 비즈니스석 직항 알림봇 시작!</b>\n\n"
        f"📍 모니터링 노선: 런던·파리·프랑크푸르트·암스테르담·바르셀로나·빈·취리히·코펜하겐\n"
        f"💺 좌석: 비즈니스석 직항만\n"
        f"💰 알림 조건:\n"
        f"  · 150만원 이하\n"
        f"  · 또는 평균 대비 30% 이상 저렴\n"
        f"⏱ 체크 주기: {CHECK_INTERVAL_MINUTES}분마다\n\n"
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
    while True:
        schedule.run_pending()
        time.sleep(30)
