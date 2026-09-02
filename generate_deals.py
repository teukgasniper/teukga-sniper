#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
특가스나이퍼 — 항공권 특가 데이터 수집 스크립트 (v2: 전세계 + 왕복/편도)

Travelpayouts Data API에서 인천(ICN) 출발 항공권 최저가를 왕복·편도 각각 수집하고,
노선별 월간 평균가(month-matrix)와 비교해 할인율을 계산한 뒤 deals.json을 생성한다.

- 도시/국가명은 iata_map.json(전세계 공항 매핑)으로 자동 처리 → 모든 나라 커버
- 왕복(round)과 편도(oneway)를 분리 수집

실행: python generate_deals.py
환경변수: TRAVELPAYOUTS_TOKEN, TRAVELPAYOUTS_MARKER, INCLUDE_COMMISSION_LINK
"""

import os
import json
import time
import gzip
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "")
MARKER = os.environ.get("TRAVELPAYOUTS_MARKER", "770703")
ORIGIN = "ICN"
CURRENCY = "krw"
MIN_SAMPLE = 10            # 평균가 표본 최소 개수 (완화)
DISCOUNT_THRESHOLD = -3.0   # 평균보다 3% 이상 싸면 노출 (캐치프로그 방식)
MAX_DESTINATIONS = 200     # 왕복/편도 각각 평균가 조회할 최대 노선 수
INCLUDE_COMMISSION_LINK = os.environ.get("INCLUDE_COMMISSION_LINK", "false").lower() == "true"

DOMESTIC_CODES = {"CJU", "PUS", "TAE", "KWJ", "USN", "RSU", "HIN", "WJU", "KPO", "KUV",
                  "SEL", "GMP", "ICN"}

API_BASE = "https://api.travelpayouts.com"

# ── 전세계 공항→도시/국가 매핑 로드 ──────────────────────────────
IATA_MAP = {}
try:
    with open("iata_map.json", "r", encoding="utf-8") as f:
        IATA_MAP = json.load(f)
except Exception as e:  # noqa: BLE001
    print(f"경고: iata_map.json 로드 실패 ({e}) — 도시명이 코드로 표시될 수 있습니다.")


# 한국인에게 익숙한 이름으로 보정 (공식 도시명 → 통용 명칭)
CITY_ALIAS = {
    "DPS": "발리",       # 덴파사르 → 발리
    "PQC": "푸꾸옥",     # Phuquoc → 푸꾸옥
    "CXR": "나트랑",     # 깜라인 → 나트랑
    "USM": "코사무이",
    "HKT": "푸켓",
    "OKA": "오키나와",
    "SGN": "호치민",
    "TYO": "도쿄", "NRT": "도쿄", "HND": "도쿄",
    "OSA": "오사카", "KIX": "오사카", "ITM": "오사카",
    "MOW": "모스크바", "SVO": "모스크바", "DME": "모스크바", "VKO": "모스크바",
    "BJS": "베이징", "PEK": "베이징", "PKX": "베이징",
    "SHA": "상하이", "PVG": "상하이",
    "NYC": "뉴욕", "JFK": "뉴욕", "EWR": "뉴욕",
}


def city_name(code):
    if code in CITY_ALIAS:
        return CITY_ALIAS[code]
    m = IATA_MAP.get(code)
    name = m["n"] if m else code
    # 어색한 접미사 정리: '광저우 시' → '광저우', '가고시마 현' → '가고시마'
    for suffix in (" 시", "시", " 현", " 州", " 구"):
        if name.endswith(suffix) and len(name) > len(suffix) + 1:
            name = name[: -len(suffix)]
            break
    return name.strip()


def country_code(code):
    m = IATA_MAP.get(code)
    return m["c"] if m else ""


def flag_emoji(cc):
    if not cc or len(cc) != 2:
        return "🏳️"
    try:
        return chr(0x1F1E6 + ord(cc[0].upper()) - 65) + chr(0x1F1E6 + ord(cc[1].upper()) - 65)
    except Exception:  # noqa: BLE001
        return "🏳️"


def api_get(path, params, retries=3):
    params = dict(params)
    params["token"] = TOKEN
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "teukga-sniper/2.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"API 호출 실패: {path} ({last_err})")


def fetch_cheapest_by_route(one_way):
    """prices_for_dates: 인천 출발 최저가를 노선별로 수집 (여러 페이지)."""
    by_route = {}
    for page in range(1, 6):  # 최대 5페이지까지 긁어서 노선 확보
        data = api_get(
            "/aviasales/v3/prices_for_dates",
            {
                "origin": ORIGIN,
                "currency": CURRENCY,
                "limit": 1000,
                "page": page,
                "sorting": "price",
                "one_way": "true" if one_way else "false",
            },
        ).get("data", [])
        if not data:
            break
        for item in data:
            dest = item.get("destination_airport")
            if not dest or dest in DOMESTIC_CODES:
                continue
            if dest not in IATA_MAP:
                continue
            if dest not in by_route or item["price"] < by_route[dest]["price"]:
                by_route[dest] = item
    return by_route


def fetch_month_average(destination, one_way):
    """month-matrix: 월간 평균가와 표본 수. one_way 기준 일치."""
    data = api_get(
        "/v2/prices/month-matrix",
        {
            "origin": ORIGIN,
            "destination": destination,
            "currency": CURRENCY,
            "one_way": "true" if one_way else "false",
            "show_to_affiliates": "true",
        },
    ).get("data", [])
    values = [row["value"] for row in data if row.get("value")]
    if not values:
        return None, 0
    return sum(values) / len(values), len(values)


def build_booking_url(link_field):
    if not link_field:
        return None
    return f"https://www.aviasales.com{link_field}"


def build_commission_url(booking_url):
    if not booking_url:
        return None
    encoded = urllib.parse.quote(booking_url, safe="")
    return f"https://tp.media/r?marker={MARKER}&p=4114&u={encoded}"


def man(n):
    return f"{round(n / 10000)}만 원"


def collect(one_way):
    """왕복 또는 편도 특가 리스트 수집."""
    label = "편도" if one_way else "왕복"
    print(f"  [{label}] 최저가 후보 수집...")
    candidates = fetch_cheapest_by_route(one_way)
    print(f"  [{label}] {len(candidates)}개 노선 후보")

    ordered = sorted(candidates.values(), key=lambda x: x["price"])[:MAX_DESTINATIONS]
    deals = []
    for item in ordered:
        dest = item["destination_airport"]
        avg, sample = fetch_month_average(dest, one_way)
        time.sleep(0.25)
        if avg is None or sample < MIN_SAMPLE:
            continue
        price = item["price"]
        discount = (price - avg) / avg * 100
        if discount > DISCOUNT_THRESHOLD:
            continue

        cc = country_code(dest)
        booking_url = build_booking_url(item.get("link"))
        deal = {
            "destination_code": dest,
            "city": city_name(dest),
            "country_code": cc,
            "flag": flag_emoji(cc),
            "trip_type": "oneway" if one_way else "round",
            "price": price,
            "price_label": man(price),
            "avg_price": round(avg),
            "avg_price_label": man(avg),
            "discount_pct": round(discount, 1),
            "departure_at": item.get("departure_at"),
            "return_at": item.get("return_at") if not one_way else None,
            "sample_size": sample,
            "fact_check_url": booking_url,
        }
        if INCLUDE_COMMISSION_LINK:
            deal["booking_url"] = build_commission_url(booking_url)
        deals.append(deal)

    # 같은 도시명은 가장 싼 것 1개만 남김
    best_by_city = {}
    for d in deals:
        c = d["city"]
        if c not in best_by_city or d["price"] < best_by_city[c]["price"]:
            best_by_city[c] = d
    deals = list(best_by_city.values())

    deals.sort(key=lambda d: d["discount_pct"])
    for i, d in enumerate(deals, 1):
        d["rank"] = i
    print(f"  [{label}] {len(deals)}개 특가 확정 (도시 중복 제거 후)")
    return deals


def main():
    if not TOKEN:
        raise SystemExit("TRAVELPAYOUTS_TOKEN 환경변수가 설정되지 않았습니다.")

    print("왕복/편도 특가 수집 시작 (전세계 노선)...")
    round_deals = collect(one_way=False)
    oneway_deals = collect(one_way=True)

    kst = timezone(timedelta(hours=9))
    output = {
        "generated_at": datetime.now(kst).isoformat(),
        "generated_at_label": datetime.now(kst).strftime("%m월 %d일 %H시 %M분 기준"),
        "origin": ORIGIN,
        "min_sample": MIN_SAMPLE,
        "discount_threshold": DISCOUNT_THRESHOLD,
        "round_count": len(round_deals),
        "oneway_count": len(oneway_deals),
        "round": round_deals,
        "oneway": oneway_deals,
        # 하위호환: 기존 위젯이 deals를 읽던 경우 왕복을 기본 노출
        "deals": round_deals,
        "route_count": len(round_deals),
    }

    with open("deals.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"완료 → deals.json (왕복 {len(round_deals)} / 편도 {len(oneway_deals)})")


if __name__ == "__main__":
    main()
