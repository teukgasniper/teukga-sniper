#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
특가스나이퍼 — 항공권 데이터 수집 (v7: 멀티 출발지)

5개 출발지(ICN, PUS, CJJ, TAE, CJU) 왕복·편도 항공편을
노선별로 1년치까지 수집해 출발지별 JSON으로 저장.

파일 구조:
  deals.json       ← ICN 데이터 (하위호환)
  deals_ICN.json   ← 인천 전용
  deals_PUS.json   ← 김해/부산 전용
  deals_CJJ.json   ← 청주 전용
  deals_TAE.json   ← 대구 전용
  deals_CJU.json   ← 제주 전용
  deals_index.json ← 출발지 목록 + 요약 (블팟 UI용)
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
CURRENCY = "krw"
MIN_SAMPLE = 8
MONTHS_AHEAD = 12
MAX_PER_ROUTE = 20
INCLUDE_COMMISSION_LINK = os.environ.get("INCLUDE_COMMISSION_LINK", "false").lower() == "true"

# ── 멀티 출발지 설정 ──
ORIGINS = [
    {"code": "ICN", "name": "인천", "max_routes": 300},
    {"code": "PUS", "name": "김해", "max_routes": 100},
    {"code": "CJJ", "name": "청주", "max_routes": 80},
    {"code": "TAE", "name": "대구", "max_routes": 80},
    {"code": "CJU", "name": "제주", "max_routes": 80},
]

# 한국 내 모든 공항 → 도착지가 여기면 국내선으로 간주해서 제외
DOMESTIC_CODES = {
    "CJU", "PUS", "TAE", "KWJ", "USN", "RSU", "HIN", "WJU", "KPO", "KUV",
    "SEL", "GMP", "ICN", "CJJ", "MWX", "YNY", "KPO", "GMP",
}

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
            req = urllib.request.Request(url, headers={"User-Agent": "teukga-sniper/7.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.2 * (attempt + 1))
    return {"data": []}


def fetch_route_candidates(origin, one_way):
    best = {}
    for page in range(1, 6):
        data = api_get(
            "/aviasales/v3/prices_for_dates",
            {"origin": origin, "currency": CURRENCY, "limit": 1000, "page": page,
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


def fetch_year_matrix(origin, destination, one_way):
    rows = []
    for ym in month_iter(MONTHS_AHEAD):
        data = api_get(
            "/v2/prices/month-matrix",
            {"origin": origin, "destination": destination, "currency": CURRENCY,
             "one_way": "true" if one_way else "false", "show_to_affiliates": "true",
             "month": ym},
        ).get("data", [])
        rows.extend(data)
        time.sleep(0.08)
    return rows


def build_booking_url(origin, dest, depart, ret=None):
    try:
        d = datetime.fromisoformat(depart)
        seg = f"{d.day:02d}{d.month:02d}"
        if ret:
            r = datetime.fromisoformat(ret)
            return f"https://www.aviasales.com/search/{origin}{seg}{dest}{r.day:02d}{r.month:02d}1"
        return f"https://www.aviasales.com/search/{origin}{seg}{dest}1"
    except Exception:  # noqa: BLE001
        return f"https://www.aviasales.com/search?origin_iata={origin}&destination_iata={dest}"


def build_commission_url(booking_url):
    if not booking_url:
        return None
    encoded = urllib.parse.quote(booking_url, safe="")
    return f"https://tp.media/r?marker={MARKER}&p=4114&u={encoded}"


def man(n):
    return f"{round(n / 10000)}만 원"


def collect(origin, max_routes, one_way):
    label = "편도" if one_way else "왕복"
    print(f"  [{origin}/{label}] 노선 후보 수집...")
    best = fetch_route_candidates(origin, one_way)
    top = sorted(best.keys(), key=lambda d: best[d]["price"])[:max_routes]
    print(f"  [{origin}/{label}] {len(best)}개 노선 → 상위 {len(top)}개 1년치 수집")

    all_flights = []
    for dest in top:
        rows = fetch_year_matrix(origin, dest, one_way)
        vals = [r["value"] for r in rows if r.get("value")]
        if len(vals) < MIN_SAMPLE:
            continue
        avg = sum(vals) / len(vals)
        cc = country_code(dest)
        city = city_name(dest)

        seen = {}
        for r in rows:
            price = r.get("value")
            depart = r.get("depart_date")
            ret = r.get("return_date") or None
            if not price or not depart:
                continue
            key = depart
            if key not in seen or price < seen[key]["value"]:
                seen[key] = r

        route_flights = []
        for r in seen.values():
            price = r["value"]
            depart = r["depart_date"]
            ret = r.get("return_date") or None
            discount = (price - avg) / avg * 100
            booking_url = build_booking_url(origin, dest, depart, ret if not one_way else None)
            f = {
                "origin": origin,
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
                f["booking_url"] = build_commission_url(booking_url)
            route_flights.append(f)

        route_flights.sort(key=lambda x: x["discount_pct"])
        all_flights.extend(route_flights[:MAX_PER_ROUTE])

    all_flights.sort(key=lambda x: x["discount_pct"])
    for i, f in enumerate(all_flights, 1):
        f["rank"] = i

    dates = sorted(set(f["departure_at"][:10] for f in all_flights))
    print(f"  [{origin}/{label}] 총 {len(all_flights)}개 항공편 / {len(dates)}개 출발일")
    return all_flights, dates


def process_origin(origin_cfg):
    code = origin_cfg["code"]
    name = origin_cfg["name"]
    max_routes = origin_cfg["max_routes"]
    print(f"\n{'='*50}")
    print(f"출발지: {name}({code}) — max_routes={max_routes}")
    print(f"{'='*50}")

    round_flights, round_dates = collect(code, max_routes, one_way=False)
    oneway_flights, oneway_dates = collect(code, max_routes, one_way=True)

    kst = timezone(timedelta(hours=9))
    output = {
        "generated_at": datetime.now(kst).isoformat(),
        "generated_at_label": datetime.now(kst).strftime("%m월 %d일 %H시 %M분 기준"),
        "origin": code,
        "origin_name": name,
        "round_count": len(round_flights),
        "oneway_count": len(oneway_flights),
        "round": round_flights,
        "oneway": oneway_flights,
        "round_dates": round_dates,
        "oneway_dates": oneway_dates,
        # 하위호환
        "deals": round_flights,
    }

    # 출발지별 전용 파일
    filename = f"deals_{code}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))
    size_kb = os.path.getsize(filename) // 1024
    print(f"  → {filename} ({size_kb} KB, 왕복 {len(round_flights)} / 편도 {len(oneway_flights)})")

    # ICN은 하위호환용 deals.json도 생성
    if code == "ICN":
        with open("deals.json", "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, separators=(",", ":"))
        print(f"  → deals.json (하위호환 복사)")

    return {
        "code": code,
        "name": name,
        "round_count": len(round_flights),
        "oneway_count": len(oneway_flights),
        "file": filename,
        "size_kb": size_kb,
    }


def main():
    if not TOKEN:
        raise SystemExit("TRAVELPAYOUTS_TOKEN 환경변수가 설정되지 않았습니다.")

    kst = timezone(timedelta(hours=9))
    print("멀티 출발지 수집 시작...")
    print(f"출발지: {', '.join(o['name']+'('+o['code']+')' for o in ORIGINS)}")

    summaries = []
    for origin_cfg in ORIGINS:
        try:
            summary = process_origin(origin_cfg)
            summaries.append(summary)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ {origin_cfg['code']} 수집 실패: {e}")
            summaries.append({
                "code": origin_cfg["code"],
                "name": origin_cfg["name"],
                "round_count": 0,
                "oneway_count": 0,
                "file": f"deals_{origin_cfg['code']}.json",
                "size_kb": 0,
                "error": str(e),
            })

    # 인덱스 파일 생성 (블팟 UI에서 출발지 목록 로드용)
    index = {
        "generated_at": datetime.now(kst).isoformat(),
        "generated_at_label": datetime.now(kst).strftime("%m월 %d일 %H시 %M분 기준"),
        "origins": summaries,
    }
    with open("deals_index.json", "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\n{'='*50}")
    print("전체 완료!")
    for s in summaries:
        status = f"왕복 {s['round_count']} / 편도 {s['oneway_count']} ({s['size_kb']} KB)"
        if s.get("error"):
            status = f"⚠️ 실패: {s['error']}"
        print(f"  {s['name']}({s['code']}): {status}")


if __name__ == "__main__":
    main()
