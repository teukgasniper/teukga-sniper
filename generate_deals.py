#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
특가스나이퍼 — 항공권 특가 데이터 수집 (v5: 1년치 + 메인 인기 분리)

인천(ICN) 출발 왕복·편도 항공권을 최대 1년치까지 수집.
- 각 노선의 12개월치 특가를 month-matrix로 모아 평균 대비 할인율 계산
- 결과를 deals.json에 담음. 위젯이 메인(top N)과 검색(전체)을 구분해서 노출.
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
MIN_SAMPLE = 10
DISCOUNT_THRESHOLD = -12.0     # 평균보다 12% 이상 싸면 특가
MONTHS_AHEAD = 12              # 앞으로 몇 개월치 수집
MAX_ROUTES = 45               # 1년치 수집할 노선 수 (가격 낮은 순)
MAX_PER_CITY_MONTH = 2        # 도시+월 조합당 최대 카드 (날짜 다양성)
MAX_TOTAL = 250              # 왕복/편도 각각 최대 카드
INCLUDE_COMMISSION_LINK = os.environ.get("INCLUDE_COMMISSION_LINK", "false").lower() == "true"

DOMESTIC_CODES = {"CJU", "PUS", "TAE", "KWJ", "USN", "RSU", "HIN", "WJU", "KPO", "KUV",
                  "SEL", "GMP", "ICN"}
API_BASE = "https://api.travelpayouts.com"

IATA_MAP = {}
try:
    with open("iata_map.json", "r", encoding="utf-8") as f:
        IATA_MAP = json.load(f)
except Exception as e:  # noqa: BLE001
    print(f"경고: iata_map.json 로드 실패 ({e})")

CITY_ALIAS = {
    "DPS": "발리", "PQC": "푸꾸옥", "CXR": "나트랑", "USM": "코사무이", "HKT": "푸켓",
    "OKA": "오키나와", "SGN": "호치민",
    "TYO": "도쿄", "NRT": "도쿄", "HND": "도쿄",
    "OSA": "오사카", "KIX": "오사카", "ITM": "오사카",
    "MOW": "모스크바", "SVO": "모스크바", "DME": "모스크바", "VKO": "모스크바",
    "BJS": "베이징", "PEK": "베이징", "PKX": "베이징",
    "SHA": "상하이", "PVG": "상하이", "NYC": "뉴욕", "JFK": "뉴욕", "EWR": "뉴욕",
}


def city_name(code):
    if code in CITY_ALIAS:
        return CITY_ALIAS[code]
    m = IATA_MAP.get(code)
    name = m["n"] if m else code
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
            req = urllib.request.Request(url, headers={"User-Agent": "teukga-sniper/5.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.2 * (attempt + 1))
    return {"data": []}  # 실패 시 빈 데이터 (전체 중단 방지)


def fetch_route_candidates(one_way):
    """가격 낮은 순 노선 후보 확보."""
    best = {}
    for page in range(1, 6):
        data = api_get(
            "/aviasales/v3/prices_for_dates",
            {"origin": ORIGIN, "currency": CURRENCY, "limit": 1000, "page": page,
             "sorting": "price", "one_way": "true" if one_way else "false"},
        ).get("data", [])
        if not data:
            break
        for item in data:
            dest = item.get("destination_airport")
            if not dest or dest in DOMESTIC_CODES or dest not in IATA_MAP:
                continue
            if dest not in best or item["price"] < best[dest]["price"]:
                best[dest] = item
    return best


def month_iter(n):
    now = datetime.now(timezone(timedelta(hours=9)))
    y, m = now.year, now.month
    for _ in range(n):
        yield f"{y}-{m:02d}-01"
        m += 1
        if m > 12:
            m = 1
            y += 1


def fetch_year_matrix(destination, one_way):
    """노선의 12개월치 month-matrix 병합."""
    rows = []
    for ym in month_iter(MONTHS_AHEAD):
        data = api_get(
            "/v2/prices/month-matrix",
            {"origin": ORIGIN, "destination": destination, "currency": CURRENCY,
             "one_way": "true" if one_way else "false", "show_to_affiliates": "true",
             "month": ym},
        ).get("data", [])
        rows.extend(data)
        time.sleep(0.12)
    return rows


def build_booking_url(dest, depart, ret=None):
    """month-matrix는 link가 없으므로 aviasales 검색 URL을 직접 구성."""
    # 형식: /search/ICN{DDMM}{DEST}{DDMM}1  (편도는 뒤 날짜 생략)
    try:
        d = datetime.fromisoformat(depart)
        seg = f"{d.day:02d}{d.month:02d}"
        if ret:
            r = datetime.fromisoformat(ret)
            return f"https://www.aviasales.com/search/{ORIGIN}{seg}{dest}{r.day:02d}{r.month:02d}1"
        return f"https://www.aviasales.com/search/{ORIGIN}{seg}{dest}1"
    except Exception:  # noqa: BLE001
        return f"https://www.aviasales.com/search?origin_iata={ORIGIN}&destination_iata={dest}"


def build_commission_url(booking_url):
    if not booking_url:
        return None
    encoded = urllib.parse.quote(booking_url, safe="")
    return f"https://tp.media/r?marker={MARKER}&p=4114&u={encoded}"


def man(n):
    return f"{round(n / 10000)}만 원"


def collect(one_way):
    label = "편도" if one_way else "왕복"
    print(f"  [{label}] 노선 후보 수집...")
    best = fetch_route_candidates(one_way)
    top = sorted(best.keys(), key=lambda d: best[d]["price"])[:MAX_ROUTES]
    print(f"  [{label}] {len(best)}개 노선 → 상위 {len(top)}개 1년치 수집")

    cards = []
    for idx, dest in enumerate(top, 1):
        rows = fetch_year_matrix(dest, one_way)
        vals = [r["value"] for r in rows if r.get("value")]
        if len(vals) < MIN_SAMPLE:
            continue
        avg = sum(vals) / len(vals)
        cc = country_code(dest)
        city = city_name(dest)
        for r in rows:
            price = r.get("value")
            depart = r.get("depart_date")
            ret = r.get("return_date") or None
            if not price or not depart:
                continue
            discount = (price - avg) / avg * 100
            if discount > DISCOUNT_THRESHOLD:
                continue
            booking_url = build_booking_url(dest, depart, ret if not one_way else None)
            card = {
                "destination_code": dest, "city": city, "country_code": cc,
                "flag": flag_emoji(cc), "trip_type": "oneway" if one_way else "round",
                "price": price, "price_label": man(price),
                "avg_price": round(avg), "avg_price_label": man(avg),
                "discount_pct": round(discount, 1),
                "departure_at": depart + "T00:00:00+09:00",
                "return_at": (ret + "T00:00:00+09:00") if (ret and not one_way) else None,
                "sample_size": len(vals),
                "fact_check_url": booking_url,
            }
            if INCLUDE_COMMISSION_LINK:
                card["booking_url"] = build_commission_url(booking_url)
            cards.append(card)

    # (도시, 출발일) 중복 제거 - 최저가
    seen = {}
    for c in cards:
        key = (c["city"], c["departure_at"][:10])
        if key not in seen or c["price"] < seen[key]["price"]:
            seen[key] = c
    cards = list(seen.values())

    # 도시+월 조합당 최대 MAX_PER_CITY_MONTH개
    cards.sort(key=lambda c: c["discount_pct"])
    per_cm, final = {}, []
    for c in cards:
        key = (c["city"], c["departure_at"][:7])
        cnt = per_cm.get(key, 0)
        if cnt >= MAX_PER_CITY_MONTH:
            continue
        per_cm[key] = cnt + 1
        final.append(c)
        if len(final) >= MAX_TOTAL:
            break

    final.sort(key=lambda c: c["discount_pct"])
    for i, c in enumerate(final, 1):
        c["rank"] = i
    print(f"  [{label}] 최종 카드 {len(final)}개")
    return final


def main():
    if not TOKEN:
        raise SystemExit("TRAVELPAYOUTS_TOKEN 환경변수가 설정되지 않았습니다.")
    print("특가 수집 시작 (1년치, 전세계)...")
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
        "deals": round_deals,
        "route_count": len(round_deals),
    }
    with open("deals.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"완료 → deals.json (왕복 {len(round_deals)} / 편도 {len(oneway_deals)})")


if __name__ == "__main__":
    main()
