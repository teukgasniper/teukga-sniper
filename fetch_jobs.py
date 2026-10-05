"""잡알리오 + 클린아이 + 나라일터 + 공채속보 채용공고 수집 → jobs.json (4시간마다 GitHub Actions에서 실행)
잡알리오: 1순위 공공데이터포털 직접 연결 / 2순위 구글 Apps Script 중계
클린아이: 지방공기업·출자출연기관 (시도별 순회 수집)
나라일터: 인사혁신처 공공취업정보 조회 서비스 (중앙부처·지자체·교육청 포함)
키·주소·암호는 GitHub Secrets에서만 읽습니다. 코드에 적지 마세요.

[2026-09-28 수정] 클린아이 API 통합 — 지방공기업·출자출연기관 채용공고 수집
                  3자 중복 소거 (잡알리오 → 클린아이 → 나라일터)
                  원문 URL 자동 추출 (ACCUSATION·JOB_SEEK_ETC에서 외부 URL 파싱)
                  [저녁] 원문 링크 인식 강화 — https 없는 도메인 인식, 이메일 도메인 제외,
                  홈페이지 접수인데 주소 없으면 클린아이 상세에서 기관 홈페이지 읽기,
                  이메일·우편·방문 접수만 클린아이 상세 유지
[2026-09-24 수정] 알바급 제외 — 정규직·무기계약직·채용형인턴 포함 공고만 수집
                  임원급 제외 — 비상임이사·사장공모 등 일반 취준생 대상 아닌 공고 차단
                  나라일터 API 통합 — 잡알리오에 없는 정부부처·지자체 공고 추가 수집
                  중복 소거 + 특수직·아르바이트급 제외 + 합격자 발표 제외
                  500건씩 + 90초 타임아웃 + 3회 재시도
[2026-10-02 추가] 고용24 공채속보 통합 — 대기업·중견기업 공채 (정규직 포함 공고만, 공공 성격 제외)
                  bizType 필드(대기업/중견기업) → 블로그 '대기업·중견기업' 탭용, logoUrl 추가
                  상세 API로 학력·경력·근무지역·모집분야 보충, 실패 시 직전 데이터 재사용
[2026-10-02 수정] 나라일터 제목 검색어 방식 — 깊은 페이지 타임아웃 해결 (검색어 14개의 최신 페이지만 수집)
[2026-10-02 수정] 공채속보 상세 캐시 — 24시간 안에 받은 상세는 재사용, 새·수정 공고만 조회
[2026-10-02 수정] 나라일터 504 대응 — 100건 페이지 × 100페이지, 연속 5페이지 실패 시 중단,
                  수집 실패 시 직전 jobs.json의 나라일터 공고(마감 전) 재사용
                  클린아이 시도별 3회 재시도 + 실패 시도 있으면 직전 클린아이 공고 재사용
[2026-09-23 수정] 의사직(전문의·전임의·레지던트 등) 공고 수집 제외
"""
import json, os, sys, time, socket, urllib.request, urllib.parse, re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

KEY = os.environ.get("ALIO_API_KEY", "").strip()
RELAY_URL = os.environ.get("RELAY_URL", "").strip()
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "").strip()
GOJOBS_KEY = os.environ.get("GOJOBS_API_KEY", "").strip()
CLEANEYE_KEY = os.environ.get("CLEANEYE_API_KEY", "").strip()
WORK24_KEY = os.environ.get("WORK24_GONGCHAE_KEY", "").strip()

if not KEY and not RELAY_URL:
    sys.exit("ALIO_API_KEY 또는 RELAY_URL 이 필요합니다.")

BASES = ["http://apis.data.go.kr/1051000/recruitment/list",
         "https://apis.data.go.kr/1051000/recruitment/list"]
KEEP = ["recrutPblntSn","instNm","recrutPbancTtl","hireTypeNmLst","workRgnNmLst","recrutSeNm",
        "recrutNope","pbancBgngYmd","pbancEndYmd","srcUrl","acbgCondNmLst","replmprYn",
        "ongoingYn","ncsCdNmLst"]
# 클린아이 전용 추가 필드
KEEP_CLEANEYE = KEEP + ["recruitCnt","yearIncome","judgeMethod","localYn","localName",
                         "address","entGb","entKind","careerType"]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
           "Accept": "application/json"}

# ─────────────────────────────────────────────────────────────
# 의사직 제외 필터 (잡알리오 + 클린아이 + 나라일터 공통)
# ─────────────────────────────────────────────────────────────
DOCTOR_KEYWORDS = [
    "전문의", "전임의", "레지던트", "전공의", "수련의",
    "공중보건의", "공보의", "의사직", "진료의사", "촉탁의",
    "임상강사", "봉직의", "임상교수", "진료과장", "의무직",
    "임상스텝", "임상스태프", "펠로우",
]
SAFE_KEYWORDS = [
    "간호", "임상병리", "방사선", "물리치료", "작업치료",
    "치과위생", "응급구조", "약사", "영양사", "의무기록",
    "보건직", "의사소통",
]

# ─────────────────────────────────────────────────────────────
# 임원급 제외 필터 (잡알리오 + 클린아이 + 나라일터 공통)
# ─────────────────────────────────────────────────────────────
EXECUTIVE_KEYWORDS = [
    "비상임이사", "상임이사", "이사장 공모", "사장 공모", "감사 공모",
    "임원 공모", "임원(", "기관장 공모", "원장 공모", "이사 모집",
    "비상임감사", "상임감사", "사장 모집", "이사 공모",
    "노동이사",  # 클린아이에서 발견
]

# ─────────────────────────────────────────────────────────────
# 나라일터 전용 제외 필터
# ─────────────────────────────────────────────────────────────
GOJOBS_INST_EXCLUDE = [
    "국방부", "공군", "육군", "해군", "해병대", "국군", "사령부", "군단",
    "경찰청", "경찰서", "기동단", "경찰학교",
    "소방청", "소방서", "119구조",
    "검찰청", "지방법원", "고등법원", "가정법원", "대법원",
    "교도소", "구치소", "교정청",
    "초등학교", "중학교", "고등학교", "유치원",
    "우체국",
    "소년원", "분류심사원", "보호관찰소",
]

GOJOBS_TITLE_EXCLUDE = [
    "한시인력", "기간제",
    "조리원", "조리사", "급식보조", "청소원", "환경미화", "당직",
    "대체인력", "대체직원", "육아휴직 대체",
    "방과후", "돌봄", "교육실무", "일용직",
    "변호사", "검사", "법무관",
    "비상임이사", "상임이사", "이사장 공모", "사장 공모", "감사 공모",
    "임원 공모", "기관장 공모", "원장 공모", "이사 모집", "비상임감사",
    "상임감사", "사장 모집", "이사 공모",
    "임기제", "전입", "체험형", "시간강사", "도급",
    "합격자", "불합격", "취소", "연기", "정정",
]

# ─────────────────────────────────────────────────────────────
# 클린아이 전용 설정
# ─────────────────────────────────────────────────────────────
CLEANEYE_ENDPOINT = "https://apis.data.go.kr/B551982/openApiEmployInfo/openXmlEmployInfo"

# 시도코드 — 테스트로 007001(서울) 확인됨, 나머지는 순차 테스트 후 보정
SIDO_CODES = [
    "007001", "007002", "007003", "007004", "007005", "007006", "007007", "007008",
    "007009", "007010", "007011", "007012", "007013", "007014", "007015", "007016", "007017",
]

# 클린아이 제목 제외 (나라일터 공유 + 추가)
CLEANEYE_TITLE_EXCLUDE = GOJOBS_TITLE_EXCLUDE + [
    "주차관리", "시설경비", "단기",
]

# 클린아이 고용형태 중 통과 허용 목록
CLEANEYE_QUALITY_TYPES = ["일반정규직", "상용정규직", "정규직", "무기계약직"]
# 인턴은 제목에서 "체험형" 제외 후 통과


def _as_text(value):
    if isinstance(value, list):
        return " ".join(str(v) for v in value if v)
    return str(value or "")


def is_doctor_post(x):
    text = " ".join([
        _as_text(x.get("recrutPbancTtl")),
        _as_text(x.get("ncsCdNmLst")),
    ])
    if any(k in text for k in DOCTOR_KEYWORDS):
        return True
    if "의사" in text and not any(s in text for s in SAFE_KEYWORDS):
        return True
    return False


def is_executive_post(x):
    title = _as_text(x.get("recrutPbancTtl") or x.get("title"))
    return any(k in title for k in EXECUTIVE_KEYWORDS)


def is_gojobs_excluded(x):
    inst = x.get("insttname") or x.get("instNm") or ""
    title = x.get("title") or x.get("recrutPbancTtl") or ""
    if any(k in inst for k in GOJOBS_INST_EXCLUDE):
        return True
    if any(k in title for k in GOJOBS_TITLE_EXCLUDE):
        return True
    return False


def is_quality_post(x):
    if x.get("_source") == "gojobs":
        return True
    if x.get("_source") == "cleaneye":
        return True  # 클린아이는 collect 단계에서 이미 필터됨
    if x.get("_source") == "work24":
        return True  # 공채속보도 collect 단계에서 정규직 포함 공고만 남김
    ht = x.get("hireTypeNmLst") or ""
    types = [h.strip() for h in ht.split(",")]
    return any(t in ("정규직", "무기계약직") or "채용형" in t for t in types)


def get_json(url, timeout):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get_xml(url, timeout):
    req = urllib.request.Request(url, headers={**HEADERS, "Accept": "application/xml"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def direct_open():
    for port in (80, 443):
        try:
            socket.create_connection(("apis.data.go.kr", port), timeout=8).close()
            print(f"[진단] 직접 연결 가능 (포트 {port})")
            return True
        except Exception as e:
            print(f"[진단] 직접 연결 불가 (포트 {port}): {e}")
    return False


USE_DIRECT = bool(KEY) and direct_open()


# ─────────────────────────────────────────────────────────────
# 1. 잡알리오 수집
# ─────────────────────────────────────────────────────────────
def fetch_alio(page):
    if USE_DIRECT:
        q = urllib.parse.urlencode({"serviceKey": KEY, "numOfRows": 1000, "pageNo": page,
                                    "resultType": "json", "ongoingYn": "Y"}, safe="%")
        for base in BASES:
            try:
                data = get_json(f"{base}?{q}", 25)
                print(f"[잡알리오] page {page} 직접 수집 성공")
                return data
            except Exception as e:
                print(f"[잡알리오] page {page} 직접({base.split(':')[0]}) 실패: {e}")
    if RELAY_URL:
        q = urllib.parse.urlencode({"token": RELAY_TOKEN, "page": page})
        for attempt in range(3):
            try:
                data = get_json(f"{RELAY_URL}?{q}", 90)
                if data.get("error"):
                    raise RuntimeError(data["error"])
                print(f"[잡알리오] page {page} 구글 중계 수집 성공")
                return data
            except Exception as e:
                print(f"[잡알리오] page {page} 구글 중계 시도 {attempt+1} 실패: {e}")
                time.sleep(5)
    raise RuntimeError(f"[잡알리오] page {page} 수집 실패 (직접·중계 모두 실패)")


def collect_alio():
    items, page = [], 1
    while True:
        data = fetch_alio(page)
        batch = data.get("result") or []
        items += batch
        total = int(data.get("totalCount") or 0)
        if len(batch) < 1000 or len(items) >= total or page >= 5:
            break
        page += 1
    ongoing = [x for x in items if x.get("ongoingYn") == "Y"]
    doctor_posts = [x for x in ongoing if is_doctor_post(x)]
    clean = [x for x in ongoing if not is_doctor_post(x)]
    if doctor_posts:
        print(f"[잡알리오] 의사직 공고 {len(doctor_posts)}건 제외")
        for x in doctor_posts[:5]:
            print(f"  - {x.get('instNm')} | {x.get('recrutPbancTtl')}")
    exec_posts = [x for x in clean if is_executive_post(x)]
    clean = [x for x in clean if not is_executive_post(x)]
    if exec_posts:
        print(f"[잡알리오] 임원급 공고 {len(exec_posts)}건 제외")
        for x in exec_posts[:5]:
            print(f"  - {x.get('instNm')} | {x.get('recrutPbancTtl')}")
    print(f"[잡알리오] 수집 완료: {len(clean)}건 (의사직 {len(doctor_posts)}건, 임원급 {len(exec_posts)}건 제외)")
    return clean


# ─────────────────────────────────────────────────────────────
# 2. 클린아이 수집 (지방공기업·출자출연기관)
# ─────────────────────────────────────────────────────────────
# 도메인 인식: https 없는 맨 도메인(guc.hubst.co.kr)도 잡고, 이메일 안의 도메인(abc@xx.or.kr)은 무시
_URL_RE = re.compile(
    r'(?<![@\w.\-])'
    r'((?:https?://)?(?:[A-Za-z0-9\-]+\.)+(?:kr|com|net|org|io|me|biz|info)'
    r'(?::\d+)?(?:/[A-Za-z0-9\-._~/?#=&%+:]*)?)'
    r'(?![A-Za-z0-9\-@])'
)
_URL_EXCLUDE = ("cleaneye.go.kr",)
# 접수방법에 이 말이 있으면 '온라인 접수' → 기관 홈페이지로 보낼 가치가 있음
_ONLINE_WORDS = ("홈페이지", "온라인", "인터넷", "채용사이트", "채용 사이트", "채용시스템",
                 "인크루트", "사람인", "잡코리아", "커리어", "전자접수", "웹")
_DETAIL_FETCH_MAX = 40          # 1회 실행당 상세페이지 조회 상한
_detail_fetch_count = 0


def _find_urls(text):
    if not text or text.strip() in ("-", ""):
        return []
    urls = []
    for m in _URL_RE.finditer(text):
        u = m.group(1).rstrip(".,)>]}")
        if any(ex in u for ex in _URL_EXCLUDE):
            continue
        if not u.lower().startswith("http"):
            u = "https://" + u
        urls.append(u)
    return urls


def _fetch_homepage_from_detail(detail_url):
    """클린아이 상세페이지에서 기관 홈페이지(fn_UrlLink) 주소 읽기. 실패하면 ''."""
    global _detail_fetch_count
    if not detail_url or _detail_fetch_count >= _DETAIL_FETCH_MAX:
        return ""
    _detail_fetch_count += 1
    for attempt in range(3):
        try:
            req = urllib.request.Request(detail_url, headers={"User-Agent": HEADERS["User-Agent"]})
            with urllib.request.urlopen(req, timeout=15) as r:
                page = r.read().decode("utf-8", "ignore")
            m = re.search(r"fn_UrlLink\(\s*'([^']+)'", page)
            time.sleep(0.5)
            if not m:
                return ""
            home = m.group(1).strip()
            if not home.lower().startswith("http"):
                home = "https://" + home
            return "" if any(ex in home for ex in _URL_EXCLUDE) else home
        except Exception as e:
            if attempt == 2:
                print(f"[클린아이] 상세페이지 조회 실패 {detail_url}: {e}")
            time.sleep(2)
    return ""


def extract_original_url(item):
    """원문 링크 결정 순서
    1) 접수방법(ACCUSATION) → 기타(JOB_SEEK_ETC) → 제출서류(EXHIBIT) → 전형방법(JUDGE_METHOD)에서 주소 찾기
       (https 없는 맨 도메인 포함, 이메일 도메인 제외)
    2) 접수방법이 '홈페이지·온라인' 접수인데 주소가 없으면 → 클린아이 상세페이지에서 기관 홈페이지 주소 읽기
    3) 그래도 없으면(이메일·우편·방문 접수 등) → 클린아이 상세페이지 (공고문 첨부파일이 여기 있음)
    반환: (url, 종류)  종류 = 원문 / 홈페이지 / 클린아이
    """
    detail = item.get("URL", "")
    for field in ("ACCUSATION", "JOB_SEEK_ETC", "EXHIBIT", "JUDGE_METHOD"):
        urls = _find_urls(item.get(field, ""))
        if urls:
            return urls[0], "원문"

    accusation = item.get("ACCUSATION", "") or ""
    if any(w in accusation for w in _ONLINE_WORDS):
        home = _fetch_homepage_from_detail(detail)
        if home:
            return home, "홈페이지"

    return detail, "클린아이"


def cleaneye_to_alio_format(item):
    """클린아이 XML item → 잡알리오 호환 포맷"""
    employ_num = 0
    try:
        employ_num = int(item.get("EMPLOY_NUM", 0))
    except (ValueError, TypeError):
        pass

    job_type = item.get("JOB_TYPE", "")
    hire_map = {"일반정규직": "정규직", "상용정규직": "정규직", "기간제": "기간제",
                "인턴": "인턴", "전문계약직": "계약직", "일반계약직": "계약직", "비상임": "비상임"}
    hire_type = hire_map.get(job_type, job_type)

    career_map = {"신입": "신입", "경력": "경력", "신입+경력": "신입/경력"}
    career_type = career_map.get(item.get("EMPLOY_GB", ""), item.get("EMPLOY_GB", ""))

    licenses = [item.get(f"ENT_LICENSE{i}", "") for i in range(1, 5)]
    licenses = [l for l in licenses if l and l != "-"]

    src_url, src_kind = extract_original_url(item)

    return {
        "recrutPblntSn": f"CE-{item.get('NO', '')}",
        "instNm": item.get("ENT_NAME", ""),
        "recrutPbancTtl": item.get("ENT_TITLE", ""),
        "hireTypeNmLst": hire_type,
        "workRgnNmLst": item.get("SIDO_CD", ""),
        "recrutSeNm": career_type,
        "recrutNope": employ_num if employ_num > 0 else 0,
        "pbancBgngYmd": (item.get("PUB_DATE", "") or "").replace("-", ""),
        "pbancEndYmd": (item.get("PUB_END_DATE", "") or "").replace("-", ""),
        "srcUrl": src_url,
        "acbgCondNmLst": "",
        "replmprYn": "Y" if item.get("RE_MAN_YN") == "Y" else "N",
        "ongoingYn": "Y",
        "ncsCdNmLst": item.get("ENT_RECRUIT", ""),
        # 클린아이 고유 필드
        "recruitCnt": employ_num if employ_num > 0 else "",
        "yearIncome": item.get("YEARINCOME", ""),
        "judgeMethod": item.get("JUDGE_METHOD", ""),
        "localYn": item.get("LOCAL_YN", "N"),
        "localName": item.get("LOCAL_NAME", ""),
        "address": item.get("ADDRESS", ""),
        "entGb": item.get("ENT_GB", ""),
        "entKind": item.get("ENT_KIND", ""),
        "careerType": career_type,
        "srcKind": src_kind,          # 원문 / 홈페이지 / 클린아이
        "_source": "cleaneye",
    }


CLEANEYE_FAILED = 0   # 이번 실행에서 실패한 시도 수


def collect_cleaneye():
    if not CLEANEYE_KEY:
        print("[클린아이] CLEANEYE_API_KEY 없음 → 건너뜀")
        return []

    global CLEANEYE_FAILED
    all_items = []
    n_excluded = {"status": 0, "title": 0, "doctor": 0, "executive": 0, "quality": 0, "substitute": 0}

    for sido_cd in SIDO_CODES:
        try:
            q = urllib.parse.urlencode({"serviceKey": CLEANEYE_KEY, "sidoCd": sido_cd, "type": "xml"}, safe="%")
            # [2026-10-02 추가] 시도별 3회 재시도
            root = None
            for attempt in range(3):
                try:
                    root = ET.fromstring(get_xml(f"{CLEANEYE_ENDPOINT}?{q}", 30))
                    break
                except Exception as e:
                    print(f"[클린아이] {sido_cd} 시도 {attempt+1}/3 실패: {e}")
                    if attempt < 2:
                        time.sleep(5 * (attempt + 1))
            if root is None:
                CLEANEYE_FAILED += 1
                continue

            result_code = root.findtext(".//resultCode", "")
            if result_code != "0":
                result_msg = root.findtext(".//resultMsg", "")
                print(f"[클린아이] {sido_cd} 오류: {result_code} - {result_msg}")
                if "NOTEXISTDATA" not in result_msg:   # 데이터 없음(정상)은 실패로 안 셈
                    CLEANEYE_FAILED += 1
                continue

            items = root.findall(".//item")
            passed = 0

            for item_el in items:
                item = {child.tag: (child.text or "") for child in item_el}
                title = item.get("ENT_TITLE", "")
                job_type = item.get("JOB_TYPE", "")
                position = item.get("POSITION", "")
                licenses_text = " ".join([item.get(f"ENT_LICENSE{i}", "") for i in range(1, 5)])

                # 필터 1: 모집중만
                if item.get("STATUS") != "모집중":
                    n_excluded["status"] += 1
                    continue
                # 필터 2: 제목 키워드 제외
                if any(kw in title for kw in CLEANEYE_TITLE_EXCLUDE):
                    n_excluded["title"] += 1
                    continue
                # 필터 3: 의사직 제외
                if any(kw in title for kw in DOCTOR_KEYWORDS) or any(kw in licenses_text for kw in DOCTOR_KEYWORDS):
                    if not any(s in title for s in SAFE_KEYWORDS):
                        n_excluded["doctor"] += 1
                        continue
                # 필터 4: 임원급 제외
                if any(kw in title for kw in EXECUTIVE_KEYWORDS) or any(kw in position for kw in EXECUTIVE_KEYWORDS):
                    n_excluded["executive"] += 1
                    continue
                # 필터 5: 고용형태 품질 (정규직·무기계약직 + 인턴)
                type_ok = any(qt in job_type for qt in CLEANEYE_QUALITY_TYPES) or job_type == "인턴"
                if not type_ok:
                    n_excluded["quality"] += 1
                    continue
                # 필터 6: 대체인력 제외
                if item.get("RE_MAN_YN") == "Y":
                    n_excluded["substitute"] += 1
                    continue

                converted = cleaneye_to_alio_format(item)
                all_items.append(converted)
                passed += 1

            print(f"[클린아이] {sido_cd}: {len(items)}건 → {passed}건 통과")
            time.sleep(0.5)

        except Exception as e:
            print(f"[클린아이] {sido_cd} 실패: {e}")
            CLEANEYE_FAILED += 1
            continue

    print(f"[클린아이] 수집 완료: {len(all_items)}건")
    kinds = {}
    for x in all_items:
        kinds[x.get("srcKind", "?")] = kinds.get(x.get("srcKind", "?"), 0) + 1
    print(f"  원문 링크 — 원문 {kinds.get('원문', 0)} / 기관 홈페이지 {kinds.get('홈페이지', 0)} / "
          f"클린아이 상세 {kinds.get('클린아이', 0)} (상세페이지 조회 {_detail_fetch_count}회)")
    print(f"  제외 — 마감: {n_excluded['status']} / 제목: {n_excluded['title']} / "
          f"의사직: {n_excluded['doctor']} / 임원급: {n_excluded['executive']} / "
          f"고용형태: {n_excluded['quality']} / 대체인력: {n_excluded['substitute']}")
    return all_items


# ─────────────────────────────────────────────────────────────
# 3. 나라일터 수집
# ─────────────────────────────────────────────────────────────
GOJOBS_BASE = "https://apis.data.go.kr/1760000/PblJobService/getList"


# [2026-10-02] 나라일터는 공고를 오래된 순으로만 주고, 필터 없이 맨 끝(최신) 페이지를 요청하면
#   서버가 60초 안에 응답을 못 함(SERVICETIMEOUT). 정렬·날짜 조건은 지원하지 않고 '제목 검색(title)'만 작동.
#   → 제목 검색어 여러 개로 건수를 줄여서 각 검색어의 최신 페이지만 받아 합친다.
#   검색어는 2026-10-02 실측으로 고름 (전체 177건 중 이 14개로 거의 전부 커버, 기여 0인 검색어는 뺌)
NARA_KEYWORDS = ["2026", "공무직", "직원", "모집", "재공고", "경력경쟁", "연구원", "신규",
                 "정규직", "청원경찰", "선발", "실무", "운영", "공무원"]
NARA_PAGES_PER_KW = 3          # 검색어당 최신 페이지 최대 3개(300건)
NARA_LOOKBACK_DAYS = 45        # 이보다 오래 전 등록된 공고가 나오면 그 검색어는 더 안 내려감
NARA_FAILED = 0                # 실패한 검색어 수 (있으면 직전 카드 재사용)


def fetch_gojobs_page(page, per_page, title=None):
    params = {"serviceKey": GOJOBS_KEY, "numOfRows": per_page, "pageNo": page}
    if title:
        params["title"] = title
    q = urllib.parse.urlencode(params, safe="%")
    for attempt in range(2):
        try:
            root = ET.fromstring(get_xml(f"{GOJOBS_BASE}?{q}", 58))
            err = root.findtext(".//errMsg")
            if err:
                raise RuntimeError(err)
            total = int(root.findtext(".//totalCount") or 0)
            return total, [{child.tag: child.text for child in item} for item in root.findall(".//item")]
        except Exception as e:
            print(f"[나라일터] '{title}' page {page} 시도 {attempt+1}/2 실패: {e}")
            if attempt < 1:
                time.sleep(5)
    return None, None


def collect_gojobs():
    global NARA_FAILED
    if not GOJOBS_KEY:
        print("[나라일터] GOJOBS_API_KEY 없음 → 건너뜀")
        return []

    now = datetime.now(timezone(timedelta(hours=9)))
    today_str = now.strftime("%Y%m%d")
    oldest = (now - timedelta(days=NARA_LOOKBACK_DAYS)).strftime("%Y%m%d")
    per_page = 100
    by_idx, t0 = {}, time.time()

    for kw in NARA_KEYWORDS:
        total, _ = fetch_gojobs_page(1, 1, kw)
        if total is None:
            NARA_FAILED += 1
            continue
        last_page = (total + per_page - 1) // per_page
        got, t1 = 0, time.time()
        for page in range(last_page, max(0, last_page - NARA_PAGES_PER_KW), -1):
            _, batch = fetch_gojobs_page(page, per_page, kw)
            if batch is None:
                NARA_FAILED += 1
                break
            for x in batch:
                if x.get("idx"):
                    by_idx[x["idx"]] = x
            got += len(batch)
            regs = [x.get("regdate") or "" for x in batch if x.get("regdate")]
            if not regs or min(regs) < oldest:
                break
            time.sleep(0.3)
        print(f"[나라일터] '{kw}': 전체 {total:,}건 중 최신 {got}건 ({int(time.time()-t1)}초)")

    items = list(by_idx.values())
    print(f"[나라일터] 수집 완료: {len(items)}건 (검색어 {len(NARA_KEYWORDS)}개, 실패 {NARA_FAILED}회, {int(time.time()-t0)}초)")

    ongoing = []
    for x in items:
        enddate = x.get("enddate", "")
        if len(enddate) >= 8 and enddate >= today_str:
            ongoing.append(x)

    after_doctor = []
    n_doctor = 0
    for x in ongoing:
        title = x.get("title", "")
        if any(k in title for k in DOCTOR_KEYWORDS):
            n_doctor += 1
            continue
        if "의사" in title and not any(s in title for s in SAFE_KEYWORDS):
            n_doctor += 1
            continue
        after_doctor.append(x)

    clean = []
    n_special = 0
    for x in after_doctor:
        if is_gojobs_excluded(x):
            n_special += 1
            continue
        clean.append(x)

    print(f"[나라일터] 진행 중: {len(ongoing)}건 → 의사직 {n_doctor}건 제외 → 특수직·아르바이트·임원급 {n_special}건 제외 → 최종: {len(clean)}건")
    return clean


def gojobs_to_alio_format(gj):
    enddate = gj.get("enddate", "")
    bgn = gj.get("regdate", "")
    return {
        "recrutPblntSn": f"GJ-{gj.get('idx', '')}",
        "instNm": gj.get("insttname", ""),
        "recrutPbancTtl": gj.get("title", ""),
        "hireTypeNmLst": "",
        "workRgnNmLst": "",
        "recrutSeNm": "",
        "recrutNope": 0,
        "pbancBgngYmd": bgn,
        "pbancEndYmd": enddate,
        "srcUrl": f"https://www.gojobs.go.kr/apmView.do?empmnsn={gj.get('idx', '')}",
        "acbgCondNmLst": "",
        "replmprYn": "N",
        "ongoingYn": "Y",
        "ncsCdNmLst": "",
        "_source": "gojobs",
    }



# ─────────────────────────────────────────────────────────────
# 4. 고용24 공채속보 수집 (대기업·중견기업 공채) [2026-10-02 추가]
# ─────────────────────────────────────────────────────────────
WORK24_LIST = "https://www.work24.go.kr/cm/openApi/call/wk/callOpenApiSvcInfo210L21.do"
WORK24_DETAIL = "https://www.work24.go.kr/cm/openApi/call/wk/callOpenApiSvcInfo210D21.do"
WORK24_DETAIL_MAX = 300      # 1회 실행당 상세 조회 상한
GONGCHAE_FAILED = False      # 목록 수집 실패 여부 (실패 시 직전 데이터 재사용)

# 기업구분이 빈칸일 때 대기업으로 볼 그룹명 (공채속보는 한글 표기: 에스케이, 엘지 등)
BIG_GROUPS = [
    "삼성", "현대", "기아", "에스케이", "SK", "엘지", "LG", "롯데", "한화", "포스코",
    "지에스", "GS", "씨제이", "CJ", "신세계", "이마트", "두산", "효성", "에이치에스효성",
    "엘에스", "LS", "디비", "DB", "에이치디", "HD", "코오롱", "호반", "셀트리온", "카카오",
    "네이버", "쿠팡", "한진", "대한항공", "아시아나", "금호", "케이티", "KT", "부영", "중흥",
    "대우", "아모레", "농심", "오리온", "동원", "한국타이어", "한국앤컴퍼니", "에쓰오일", "S-OIL",
    "하나은행", "하나카드", "하나증권", "신한", "케이비", "KB", "국민은행", "우리은행", "우리카드",
    "엔에이치", "NH", "농협", "미래에셋", "한국투자", "교보", "삼정회계", "부산은행", "경남은행",
    "아워홈", "대한전선", "넥슨", "엔씨소프트", "넷마블", "에스엠엔터테인먼트", "하이브",
]
# 그룹명으로 시작하지만 대기업 계열이 아닌 회사 [2026-10-05 추가]
NOT_BIG = ["엔에이치엔", "엘에스이"]
# 공공 성격 (잡알리오·클린아이·나라일터가 담당) → 공채속보에서는 제외
PUBLIC_WORDS = ["공사", "공단", "재단", "진흥원", "연구원", "관리원", "평가원", "인재원",
                "공제회", "중앙회", "협회", "지원협회", "교육원", "위원회"]
GONGCHAE_TITLE_EXCLUDE = ["체험형", "합격자", "취소", "연기", "정정", "대체인력", "단기", "아르바이트"]


def gongchae_biz_type(x):
    """대기업 / 중견기업 / 공공 판정"""
    cls = (x.get("coClcdNm") or "").strip()
    name = x.get("empBusiNm") or ""
    if cls in ("공공기관", "공기업"):
        return "공공"
    if cls == "대기업":
        return "대기업"
    if any(w in name for w in PUBLIC_WORDS):
        return "공공"
    # 그룹명으로 시작하지만 그룹 계열이 아닌 회사 (NHN ≠ NH농협, 엘에스이 ≠ LS그룹) — 오탐 발견 시 여기에 추가
    if any(name.startswith(n) for n in NOT_BIG):
        return "중견기업"
    if any(name.startswith(g) for g in BIG_GROUPS):   # 앞글자 일치만 (한국엔에스케이 오탐 방지)
        return "대기업"
    return "중견기업"


def fetch_gongchae_page(page):
    q = urllib.parse.urlencode({"authKey": WORK24_KEY, "callTp": "L", "returnType": "XML",
                                "startPage": page, "display": 100})
    for attempt in range(3):
        try:
            root = ET.fromstring(get_xml(f"{WORK24_LIST}?{q}", 40))
            err = root.findtext(".//e") or root.findtext(".//errMsg")
            if err:
                raise RuntimeError(err)
            total = int(root.findtext("total") or 0)
            items = [{c.tag: (c.text or "") for c in it} for it in root.findall("dhsOpenEmpInfo")]
            return total, items
        except Exception as e:
            print(f"[공채속보] page {page} 시도 {attempt+1}/3 실패: {e}")
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
    return None, None


def fetch_gongchae_detail(seqno):
    """상세: 학력·경력·근무지역·모집분야 (실패하면 빈 dict)"""
    q = urllib.parse.urlencode({"authKey": WORK24_KEY, "callTp": "D", "returnType": "XML",
                                "empSeqno": seqno})
    for attempt in range(2):
        try:
            root = ET.fromstring(get_xml(f"{WORK24_DETAIL}?{q}", 20))
            edu, career, region, fields = [], [], [], []
            for r in root.findall(".//empRecrListInfo"):
                for v, bucket in ((r.findtext("empWantedEduNm"), edu),
                                  (r.findtext("empWantedCareerNm"), career),
                                  (r.findtext("workRegionNm"), region),
                                  (r.findtext("empRecrNm"), fields)):
                    for part in (v or "").replace(",", "|").split("|"):
                        part = part.strip()
                        if part and part not in bucket:
                            bucket.append(part)
            # [2026-10-05] 공채속보 학력은 '최소~최대' 범위로 와서 최대값 '박사'가 섞임
            #   (대졸 신입 공채도 '박사', 고졸 생산직도 '고졸,박사') → 박사는 버리고 최소 요건만 남김
            edu = [e for e in edu if e != "박사"]
            return {"edu": edu, "career": career, "region": region, "fields": fields,
                    "homepage": root.findtext("empWantedHomepg") or ""}
        except Exception:
            time.sleep(2)
    return {}


def _career_to_alio(career):
    has_new = any("신입" in c for c in career)
    has_exp = any(c.startswith("경력") and "무관" not in c for c in career)
    if any("무관" in c for c in career) or (has_new and has_exp):
        return "신입+경력"
    if has_new:
        return "신입"
    if has_exp:
        return "경력"
    return ""


def collect_gongchae():
    global GONGCHAE_FAILED
    if not WORK24_KEY:
        print("[공채속보] WORK24_GONGCHAE_KEY 없음 → 건너뜀")
        return []

    today_str = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
    raw, page = [], 1
    while True:
        total, items = fetch_gongchae_page(page)
        if items is None:
            GONGCHAE_FAILED = True
            break
        raw += items
        if len(items) < 100 or len(raw) >= total or page >= 10:
            break
        page += 1
        time.sleep(0.5)
    print(f"[공채속보] 목록 수집: {len(raw)}건")

    n = {"마감": 0, "고용형태": 0, "제목": 0, "공공": 0, "의사·임원": 0}
    kept = []
    for x in raw:
        title = x.get("empWantedTitle", "")
        if (x.get("empWantedEndt") or "") < today_str:
            n["마감"] += 1; continue
        if "정규직" not in (x.get("empWantedTypeNm") or ""):   # 정규직·정규직전환형 포함 공고만
            n["고용형태"] += 1; continue
        if any(k in title for k in GONGCHAE_TITLE_EXCLUDE):
            n["제목"] += 1; continue
        if any(k in title for k in DOCTOR_KEYWORDS) or any(k in title for k in EXECUTIVE_KEYWORDS):
            n["의사·임원"] += 1; continue
        biz = gongchae_biz_type(x)
        if biz == "공공":
            n["공공"] += 1; continue
        x["_biz"] = biz
        kept.append(x)

    # [2026-10-02] 상세 조회 캐시 — 직전 jobs.json에 같은 공고(제목·마감일 동일)가 있고 24시간 안에 받은 상세면 재사용
    #              새 공고·수정된 공고·24시간 지난 공고만 상세 조회 (매 실행 229회 → 새 공고만)
    now_kst = datetime.now(timezone(timedelta(hours=9)))
    prev = {}
    try:
        for p_ in get_json("https://teukgasniper.github.io/job-compass/jobs.json", 30).get("result", []):
            if p_.get("_source") == "work24" and p_.get("_detailAt"):
                prev[p_["recrutPblntSn"]] = p_
    except Exception as e:
        print(f"[공채속보] 직전 데이터 불러오기 실패 → 전부 새로 조회: {e}")

    def cached_detail(x):
        p_ = prev.get(f"WK-{x.get('empSeqno', '')}")
        if not p_ or p_.get("recrutPbancTtl") != x.get("empWantedTitle") or p_.get("pbancEndYmd") != x.get("empWantedEndt"):
            return None                                   # 처음 보는 공고 또는 제목·마감일이 바뀐 공고
        try:
            age_h = (now_kst - datetime.strptime(p_["_detailAt"], "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone(timedelta(hours=9)))).total_seconds() / 3600
        except Exception:
            return None
        if age_h >= 24:
            return None                                   # 하루 지난 상세는 다시 받아 수정사항 반영
        return p_

    out, detail_cnt, reuse_cnt, t0 = [], 0, 0, time.time()
    for x in kept:
        d, p_ = {}, cached_detail(x)
        if p_:
            d = {"region": [r for r in (p_.get("workRgnNmLst") or "").split(",") if r],
                 "career_alio": p_.get("recrutSeNm") or "",
                 "edu": [e for e in (p_.get("acbgCondNmLst") or "").split(",") if e and e != "박사"],
                 "fields": [f for f in (p_.get("ncsCdNmLst") or "").split(",") if f],
                 "homepage": "", "_at": p_["_detailAt"]}
            reuse_cnt += 1
        elif detail_cnt < WORK24_DETAIL_MAX:
            d = fetch_gongchae_detail(x.get("empSeqno", ""))
            if d:
                d["_at"] = now_kst.strftime("%Y-%m-%d %H:%M")
            detail_cnt += 1
            time.sleep(0.3)
        types = [t for t in (x.get("empWantedTypeNm") or "").split("|") if t and t != "기타"]
        src = x.get("empWantedHomepgDetail") or x.get("empWantedMobileUrl") or d.get("homepage") \
              or f"https://www.work24.go.kr/wk/a/b/1500/retriveDtlEmpSrchList.do?empSeqno={x.get('empSeqno','')}"
        out.append({
            "recrutPblntSn": f"WK-{x.get('empSeqno', '')}",
            "instNm": x.get("empBusiNm", ""),
            "recrutPbancTtl": x.get("empWantedTitle", ""),
            "hireTypeNmLst": ",".join(types),
            "workRgnNmLst": ",".join(d.get("region", [])),
            "recrutSeNm": d.get("career_alio") if "career_alio" in d else _career_to_alio(d.get("career", [])),
            "recrutNope": 0,
            "pbancBgngYmd": x.get("empWantedStdt", ""),
            "pbancEndYmd": x.get("empWantedEndt", ""),
            "srcUrl": src,
            "acbgCondNmLst": ",".join(d.get("edu", [])),
            "replmprYn": "N",
            "ongoingYn": "Y",
            "ncsCdNmLst": ",".join(d.get("fields", [])[:5]),
            "bizType": x["_biz"],                       # 대기업 / 중견기업 → '대기업·중견기업' 탭
            "logoUrl": x.get("regLogImgNm", ""),
            "_detailAt": d.get("_at", ""),               # 상세 정보 받은 시각 (캐시 판단용)
            "_source": "work24",
        })

    big = sum(1 for x in out if x["bizType"] == "대기업")
    print(f"[공채속보] 최종 {len(out)}건 (대기업 {big} / 중견기업 {len(out)-big}) · 상세조회 {detail_cnt}회 + 재사용 {reuse_cnt}건 · {int(time.time()-t0)}초")
    print(f"  제외 — " + " / ".join(f"{k}: {v}" for k, v in n.items()))
    return out

# ─────────────────────────────────────────────────────────────
# 5. 중복 소거 (잡알리오 1순위 → 클린아이 2순위 → 나라일터 3순위)
# ─────────────────────────────────────────────────────────────
def normalize_inst(name):
    name = re.sub(r'\(주\)|\(재\)|\(사\)|\(학\)', '', name)
    name = re.sub(r'[㈜㈔\s]', '', name)
    name = re.sub(r'^재단법인', '', name)
    name = re.sub(r'^주식회사', '', name)
    return name.strip()

def normalize_title(title):
    title = re.sub(r'[\s\-·~]', '', title)
    return title[:30]

def dedup_key(inst, title):
    return f"{normalize_inst(inst)}|{normalize_title(title)}"


def merge_and_dedup(alio_items, cleaneye_items, gojobs_items, gongchae_items=()):
    seen = set()
    merged = []

    # 1순위: 잡알리오
    for x in alio_items:
        key = dedup_key(x.get("instNm", ""), x.get("recrutPbancTtl", ""))
        if key not in seen:
            seen.add(key)
            item = {k: x.get(k) for k in KEEP}
            item["_source"] = "alio"
            merged.append(item)

    # 2순위: 클린아이
    ce_added, ce_skipped = 0, 0
    for x in cleaneye_items:
        key = dedup_key(x.get("instNm", ""), x.get("recrutPbancTtl", ""))
        if key not in seen:
            seen.add(key)
            merged.append(x)  # 이미 잡알리오 포맷 + 추가 필드 포함
            ce_added += 1
        else:
            ce_skipped += 1

    # 3순위: 나라일터
    gj_added, gj_skipped = 0, 0
    for gj in gojobs_items:
        converted = gojobs_to_alio_format(gj)
        key = dedup_key(converted["instNm"], converted["recrutPbancTtl"])
        if key not in seen:
            seen.add(key)
            merged.append(converted)
            gj_added += 1
        else:
            gj_skipped += 1

    # 4순위: 공채속보 (대기업·중견기업)
    wk_added, wk_skipped = 0, 0
    for x in gongchae_items:
        key = dedup_key(x.get("instNm", ""), x.get("recrutPbancTtl", ""))
        if key not in seen:
            seen.add(key)
            merged.append(x)
            wk_added += 1
        else:
            wk_skipped += 1

    print(f"[병합] 잡알리오 {len(alio_items)}건 + 클린아이 {ce_added}건(중복 {ce_skipped}) + 나라일터 {gj_added}건(중복 {gj_skipped}) + 공채속보 {wk_added}건(중복 {wk_skipped})")
    print(f"[병합] 최종 합계: {len(merged)}건")
    return merged


# ─────────────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────────────
print("=" * 50)
print("채용공고 수집 시작")
print("=" * 50)

alio_items = collect_alio()
cleaneye_items = collect_cleaneye()
gojobs_items = collect_gojobs()
gongchae_items = collect_gongchae()
merged = merge_and_dedup(alio_items, cleaneye_items, gojobs_items, gongchae_items)

# [2026-10-02 추가] 수집 실패 시 직전 jobs.json 공고 재사용 (마감 전 + 이번 결과와 중복 아닌 것만)
_prev_cache = None


def reuse_previous(source, label, merged):
    global _prev_cache
    try:
        if _prev_cache is None:
            _prev_cache = get_json("https://teukgasniper.github.io/job-compass/jobs.json", 30).get("result", [])
        today_str = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
        have_id = {x.get("recrutPblntSn") for x in merged}
        have_key = {dedup_key(x.get("instNm", ""), x.get("recrutPbancTtl", "")) for x in merged}
        reused = []
        for x in _prev_cache:
            if x.get("_source") != source or (x.get("pbancEndYmd") or "") < today_str:
                continue
            if x.get("recrutPblntSn") in have_id:
                continue
            if dedup_key(x.get("instNm", ""), x.get("recrutPbancTtl", "")) in have_key:
                continue
            reused.append(x)
        merged.extend(reused)
        print(f"[{label}] 수집 실패 있음 → 직전 데이터 {len(reused)}건 재사용")
    except Exception as e:
        print(f"[{label}] 직전 데이터 재사용 실패: {e}")


if CLEANEYE_KEY and CLEANEYE_FAILED > 0:
    reuse_previous("cleaneye", "클린아이", merged)
if GOJOBS_KEY and (not gojobs_items or NARA_FAILED):   # 일부 검색어만 실패해도 빠진 카드는 직전 데이터로
    reuse_previous("gojobs", "나라일터", merged)
if WORK24_KEY and (GONGCHAE_FAILED or not gongchae_items):
    reuse_previous("work24", "공채속보", merged)

# ★ 알바급 제외 — 정규직·무기계약직·채용형인턴 포함 공고만 유지
before = len(merged)
merged = [x for x in merged if is_quality_post(x)]
print(f"[품질필터] {before}건 → {len(merged)}건 (알바급 {before - len(merged)}건 제외)")

if len(merged) < 50:
    sys.exit(f"수집 건수가 너무 적음({len(merged)}건) → 기존 데이터 유지")

kst = datetime.now(timezone(timedelta(hours=9)))
out = {"generated_at": kst.strftime("%Y-%m-%d %H:%M"), "count": len(merged), "result": merged}
with open("jobs.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
print(f"\n완료: {len(merged)}건 저장 ({out['generated_at']} KST)")
