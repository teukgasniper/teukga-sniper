#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
특가스나이퍼 — 항공권 특가 데이터 수집 스크립트

Travelpayouts Data API에서 인천(ICN) 출발 왕복 항공권 최저가를 수집하고,
노선별 월간 평균가(month-matrix)와 비교해 할인율을 계산한 뒤
웹사이트가 읽는 deals.json 파일을 생성한다.

이 스크립트는 브라우저가 아니라 서버(GitHub Actions 또는 사용자 PC)에서 실행되어야 한다.
API 토큰이 클라이언트(HTML/JS)에 절대 노출되지 않도록 하기 위함이다.

실행:
    python generate_deals.py

필요 환경변수:
    TRAVELPAYOUTS_TOKEN   Travelpayouts API 토큰
    TRAVELPAYOUTS_MARKER  파트너 마커 (커미션 추적용, 기본값 770703)
"""

import os
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ── 설정 ──────────────────────────────────────────────────────────────
TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "")
MARKER = os.environ.get("TRAVELPAYOUTS_MARKER", "770703")
ORIGIN = "ICN"
CURRENCY = "krw"
MIN_SAMPLE = 20        # 평균가 표본 최소 개수
DISCOUNT_THRESHOLD = -25.0  # 할인율 -25% 이상만 특가로 인정
MAX_DESTINATIONS = 30   # month-matrix 호출 최대 노선 수 (API 호출량 제한)
INCLUDE_COMMISSION_LINK = os.environ.get("INCLUDE_COMMISSION_LINK", "false").lower() == "true"

DOMESTIC_CODES = {"CJU", "PUS", "TAE", "KWJ", "USN", "RSU", "HIN", "WJU", "KPO", "KUV"}

CITY = {
    "KIX": "오사카", "FUK": "후쿠오카", "HIJ": "히로시마",
    "NGO": "나고야", "CTS": "삿포로", "OKA": "오키나와",
    "KOJ": "가고시마", "HND": "도쿄", "NRT": "도쿄",
    "MNL": "마닐라", "CEB": "세부", "TPE": "타이베이",
    "KHH": "가오슝", "HKG": "홍콩", "TAO": "칭다오",
    "HAN": "하노이", "SGN": "호치민", "DAD": "다낭",
    "BKK": "방콕", "SIN": "싱가포르", "KUL": "쿠알라룸푸르",
    "MFM": "마카오", "DPS": "발리", "CNX": "치앙마이",
}

FLAG = {
    "오사카": "🇯🇵", "후쿠오카": "🇯🇵", "히로시마": "🇯🇵",
    "나고야": "🇯🇵", "삿포로": "🇯🇵", "오키나와": "🇯🇵",
    "가고시마": "🇯🇵", "도쿄": "🇯🇵", "마닐라": "🇵🇭",
    "세부": "🇵🇭", "타이베이": "🇹🇼", "가오슝": "🇹🇼",
    "홍콩": "🇭🇰", "칭다오": "🇨🇳", "하노이": "🇻🇳",
    "호치민": "🇻🇳", "다낭": "🇻🇳", "방콕": "🇹🇭",
    "싱가포르": "🇸🇬", "쿠알라룸푸르": "🇲🇾", "마카오": "🇲🇴",
    "발리": "🇮🇩", "치앙마이": "🇹🇭",
}

AIRLINE = {
    "TW": "티웨이", "ZE": "이스타", "7C": "제주항공",
    "LJ": "진에어", "BX": "에어부산", "TR": "스쿠트",
    "OZ": "아시아나", "KE": "대한항공", "RS": "에어서울",
}

API_BASE = "https://api.travelpayouts.com"


def api_get(path, params, retries=3):
    params = dict(params)
    params["token"] = TOKEN
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "teukga-sniper/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"API 호출 실패: {path} ({last_err})")


def fetch_cheapest_by_route():
    """prices_for_dates: 인천 출발 왕복 최저가 후보를 노선별로 수집."""
    data = api_get(
        "/aviasales/v3/prices_for_dates",
        {
            "origin": ORIGIN,
            "currency": CURRENCY,
            "limit": 1000,
            "sorting": "price",
            "one_way": "false",
        },
    ).get("data", [])

    by_route = {}
    for item in data:
        dest = item.get("destination_airport")
        if not dest or dest in DOMESTIC_CODES or dest not in CITY:
            continue
        # 노선별 최저가 1건만 유지
        if dest not in by_route or item["price"] < by_route[dest]["price"]:
            by_route[dest] = item
    return by_route


def fetch_month_average(destination):
    """month-matrix: 왕복 기준 월간 평균가와 표본 수."""
    data = api_get(
        "/v2/prices/month-matrix",
        {
            "origin": ORIGIN,
            "destination": destination,
            "currency": CURRENCY,
            "one_way": "false",
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
    """원 → 'OO만 원' 반올림 표기."""
    return f"{round(n / 10000)}만 원"


def main():
    if not TOKEN:
        raise SystemExit("TRAVELPAYOUTS_TOKEN 환경변수가 설정되지 않았습니다.")

    print("1/3 인천 출발 왕복 최저가 후보 수집 중...")
    candidates = fetch_cheapest_by_route()
    print(f"   → {len(candidates)}개 노선 후보 발견")

    # 가격이 낮은 순으로 먼저 검사 (API 호출량 절약)
    ordered = sorted(candidates.values(), key=lambda x: x["price"])[:MAX_DESTINATIONS]

    print("2/3 노선별 평균가 조회 및 할인율 계산 중...")
    deals = []
    for item in ordered:
        dest = item["destination_airport"]
        avg, sample = fetch_month_average(dest)
        time.sleep(0.3)  # API rate limit 배려
        if avg is None or sample < MIN_SAMPLE:
            continue

        price = item["price"]
        discount = (price - avg) / avg * 100
        if discount > DISCOUNT_THRESHOLD:
            continue

        city = CITY.get(dest, dest)
        booking_url = build_booking_url(item.get("link"))
        deal = {
            "destination_code": dest,
            "city": city,
            "flag": FLAG.get(city, ""),
            "airline": AIRLINE.get(item.get("airline"), item.get("airline")),
            "price": price,
            "price_label": man(price),
            "avg_price": round(avg),
            "avg_price_label": man(avg),
            "discount_pct": round(discount, 1),
            "departure_at": item.get("departure_at"),
            "return_at": item.get("return_at"),
            "sample_size": sample,
            "fact_check_url": booking_url,
        }
        if INCLUDE_COMMISSION_LINK:
            deal["booking_url"] = build_commission_url(booking_url)
        deals.append(deal)

    print(f"3/3 {len(deals)}개 특가 확정 (기준: 평균 대비 -25% 이상, 표본 {MIN_SAMPLE}개 이상)")

    deals.sort(key=lambda d: d["discount_pct"])  # 가장 많이 떨어진 순
    for i, d in enumerate(deals, 1):
        d["rank"] = i

    kst = timezone(timedelta(hours=9))
    output = {
        "generated_at": datetime.now(kst).isoformat(),
        "generated_at_label": datetime.now(kst).strftime("%m월 %d일 %H시 %M분 기준"),
        "origin": ORIGIN,
        "route_count": len(deals),
        "min_sample": MIN_SAMPLE,
        "discount_threshold": DISCOUNT_THRESHOLD,
        "deals": deals,
    }

    with open("deals.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("완료 → deals.json 저장됨")


if __name__ == "__main__":
    main()
