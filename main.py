"""
CAPD 자동 테스트 러너 v3
- 기본: DB의 모든 환자에 대해 오늘 날짜 기록 생성
- 백필: BACKFILL_START / BACKFILL_END 환경변수로 날짜 범위 지정
- 환자별 임상 프로필 적용 (수치 범위 + Gemini 답변 프롬프트)
"""

import json
import logging
import os
import random
import re
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import vertexai
from vertexai.generative_models import GenerativeModel

# ── 환경변수 ────────────────────────────────────────────────
BASE          = os.environ.get("BACKEND_URL",    "https://capd-backend-cdwaxwdxfa-du.a.run.app")
GCP_PROJECT   = os.environ.get("GCP_PROJECT_ID", "skuniv-training-2")
GCP_REGION    = os.environ.get("GCP_REGION",     "asia-northeast3")
GEMINI_MODEL  = os.environ.get("GEMINI_MODEL",   "gemini-2.5-flash")
TEST_PASSWORD = os.environ.get("TEST_PASSWORD",  "TestCapd2025!")
DATABASE_URL  = os.environ.get("DATABASE_URL",   "")
BACKFILL_START = os.environ.get("BACKFILL_START", "").strip()
BACKFILL_END   = os.environ.get("BACKFILL_END",   "").strip()
DATE_OVERRIDE  = os.environ.get("DATE_OVERRIDE",  "").strip()

# ── KST 오늘 날짜 ────────────────────────────────────────────
KST = ZoneInfo("Asia/Seoul")

def _resolve_today() -> str:
    if DATE_OVERRIDE:
        try:
            datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d")
            return DATE_OVERRIDE
        except ValueError:
            pass
    return datetime.now(timezone.utc).astimezone(KST).date().isoformat()

TODAY = _resolve_today()

# ── 로거 ────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)
def log(msg: str) -> None:
    logger.info(msg)


# ── 환자별 임상 프로필 ────────────────────────────────────────
# vitals: 수치 생성 범위
# persona: Gemini 프롬프트용 환자 설명
PATIENT_PROFILES = {
    "01000000001": {
        "vitals": {
            "sbp": (133, 158), "dbp": (83, 96),
            "weight": (68.8, 70.2), "glucose": (118, 172),
            "urine": (1, 2), "drainage": (2000, 2130),
            "concentrations": ([1.5, 2.5, 4.25], [3, 5, 2]),
        },
        "persona": (
            "당신은 68세 남성 박영수입니다. 당뇨성 신부전으로 CAPD 2년 6개월째입니다.\n"
            "동반질환: 제2형 당뇨(20년), 고혈압, 만성심부전(EF 45%).\n"
            "UF량이 6개월 전 700~800ml에서 현재 350~500ml로 감소 중 (membrane 기능 저하 의심).\n"
            "체중이 3개월간 68→69.5kg으로 서서히 증가, 발목 부종 간헐적으로 있음.\n"
            "혈압 135~155/85~95로 불안정, 혈당 공복 120~170으로 인슐린 사용 중.\n"
            "오늘 상태: 발목 부종 어제보다 약간 심함, 오후 숨참 경미, 배액이 평소보다 적게 나온 느낌, "
            "아침 인슐린은 맞았으나 저녁 혈압약 깜빡, 피로감으로 낮잠."
        ),
    },
    "01000000002": {
        "vitals": {
            "sbp": (118, 138), "dbp": (72, 88),
            "weight": (54.0, 55.5), "glucose": (95, 135),
            "urine": (2, 4), "drainage": (2050, 2300),
            "concentrations": ([1.5, 2.5], [6, 4]),
        },
        "persona": (
            "당신은 55세 여성 이미경입니다. 사구체신염으로 CAPD 1년 3개월째입니다.\n"
            "복막염 과거력 있음 (8개월 전 1회). 배액이 가끔 약간 혼탁하게 보여 걱정됨.\n"
            "혈압은 비교적 잘 조절되고 있으나 카테터 삽입 부위에 간헐적으로 가려움이 있음.\n"
            "오늘 상태: 배액 색이 평소보다 살짝 뿌연 것 같아 불안, 복부 통증은 없음, "
            "카테터 주변 약간 가려움, 발열 없음, 식사는 잘 함."
        ),
    },
    "01000000003": {
        "vitals": {
            "sbp": (148, 175), "dbp": (90, 105),
            "weight": (74.0, 76.5), "glucose": (100, 130),
            "urine": (1, 2), "drainage": (2000, 2100),
            "concentrations": ([2.5, 4.25], [5, 5]),
        },
        "persona": (
            "당신은 72세 남성 최병국입니다. 고혈압성 신부전으로 CAPD 3년째입니다.\n"
            "체액 과부하 경향 있어 고농도 투석액 자주 사용. 체중이 계속 높은 편.\n"
            "혈압 조절이 어려워 항고혈압제 3종 복용 중이나 수축기 150 이상인 날이 많음.\n"
            "오늘 상태: 아침 혈압이 168/98로 높게 나옴, 두통 경미하게 있음, "
            "다리가 무거운 느낌, 약은 모두 복용함."
        ),
    },
    "01000000004": {
        "vitals": {
            "sbp": (105, 125), "dbp": (65, 80),
            "weight": (46.5, 48.0), "glucose": (82, 115),
            "urine": (3, 5), "drainage": (2100, 2400),
            "concentrations": ([1.5, 2.5], [8, 2]),
        },
        "persona": (
            "당신은 45세 여성 정수연입니다. IgA 신병증으로 CAPD 8개월째입니다.\n"
            "잔여 신기능이 아직 일부 남아있어 소변량이 하루 300~500ml 정도 나옴.\n"
            "최근 3개월간 체중이 49→47kg으로 감소 중, 식욕 부진이 주요 문제.\n"
            "혈압과 혈당은 비교적 양호하게 유지됨.\n"
            "오늘 상태: 아침부터 속이 메슥거리고 밥을 거의 못 먹음, "
            "체중이 또 줄어있어 걱정, 투석은 정상적으로 함, 피로감 있음."
        ),
    },
    "01000000005": {
        "vitals": {
            "sbp": (128, 150), "dbp": (78, 92),
            "weight": (70.0, 72.0), "glucose": (105, 145),
            "urine": (1, 2), "drainage": (2050, 2200),
            "concentrations": ([2.5, 4.25], [6, 4]),
        },
        "persona": (
            "당신은 63세 남성 강동원입니다. 당뇨성 신부전으로 CAPD 2년째입니다.\n"
            "만성심부전(EF 40%) 동반으로 체액 관리가 매우 중요함.\n"
            "UF는 적절하게 나오고 있으나 심부전으로 인한 호흡곤란이 간헐적으로 발생.\n"
            "오늘 상태: 오후에 계단 오를 때 숨이 많이 차서 멈춰야 했음, "
            "누우면 숨쉬기가 편함, 발목 부종 약간, 가슴 답답함 경미하게 있음."
        ),
    },
    "01000000006": {
        "vitals": {
            "sbp": (122, 142), "dbp": (75, 88),
            "weight": (58.0, 59.5), "glucose": (92, 125),
            "urine": (4, 7), "drainage": (2100, 2350),
            "concentrations": ([1.5, 2.5], [7, 3]),
        },
        "persona": (
            "당신은 58세 여성 한지영입니다. 혈액투석에서 복막투석으로 전환한 지 5개월됐습니다.\n"
            "잔여 신기능이 상당히 남아있어 소변이 하루 600~900ml씩 나옴.\n"
            "복막투석에 아직 적응 중이며 교환 과정이 익숙하지 않은 부분이 있음.\n"
            "오늘 상태: 투석 교환 시 배액 연결 과정에서 실수가 있었던 것 같아 불안, "
            "배액은 맑고 양은 정상, 복부 이상 없음, 전반적으로 컨디션 양호."
        ),
    },
    "01000000007": {
        "vitals": {
            "sbp": (140, 168), "dbp": (85, 100),
            "weight": (77.0, 79.5), "glucose": (108, 148),
            "urine": (1, 2), "drainage": (2000, 2120),
            "concentrations": ([1.5, 2.5, 4.25], [2, 5, 3]),
        },
        "persona": (
            "당신은 70세 남성 송기태입니다. 고혈압성 신부전으로 CAPD 4년째입니다.\n"
            "약 복용을 자주 잊어버리는 편이고 식이 제한을 잘 지키지 않음 (짠 음식 선호).\n"
            "혈압이 전반적으로 높게 유지되고 있으며 체중도 목표보다 높음.\n"
            "오늘 상태: 어제 저녁과 오늘 아침 혈압약을 둘 다 깜빡함, "
            "혈압이 아침에 172/102로 매우 높게 나옴, 두통 있음, 어제 저녁 삼겹살 먹음."
        ),
    },
    "01000000008": {
        "vitals": {
            "sbp": (112, 130), "dbp": (68, 82),
            "weight": (51.0, 52.5), "glucose": (85, 108),
            "urine": (3, 5), "drainage": (2150, 2450),
            "concentrations": ([1.5, 2.5], [8, 2]),
        },
        "persona": (
            "당신은 52세 여성 윤서희입니다. 사구체신염으로 CAPD 1년째입니다.\n"
            "혈압, 혈당 모두 양호하게 조절되고 있으며 UF도 충분히 나오는 편.\n"
            "잔여 신기능도 일부 남아있어 전반적으로 상태가 안정적임.\n"
            "오늘 상태: 전반적으로 컨디션 좋음, 투석도 원활하게 완료, "
            "다만 오늘 직장 스트레스로 피로감이 약간 있음, 식사는 잘 함."
        ),
    },
    "01000000009": {
        "vitals": {
            "sbp": (125, 148), "dbp": (78, 92),
            "weight": (65.0, 66.8), "glucose": (110, 155),
            "urine": (2, 3), "drainage": (2030, 2200),
            "concentrations": ([1.5, 2.5], [5, 5]),
        },
        "persona": (
            "당신은 66세 남성 임재호입니다. 당뇨성 신부전으로 CAPD 1년 8개월째입니다.\n"
            "카테터 삽입 부위 주변에 간헐적으로 발적과 분비물이 생기는 경향이 있음.\n"
            "출구 감염 위험이 있어 주기적으로 모니터링 중.\n"
            "오늘 상태: 카테터 삽입 부위가 어제부터 약간 빨개지고 누르면 아픔, "
            "분비물은 없고 발열도 없음, 투석 배액은 맑음, 걱정이 됨."
        ),
    },
    "01000000010": {
        "vitals": {
            "sbp": (118, 138), "dbp": (72, 86),
            "weight": (62.0, 63.5), "glucose": (90, 118),
            "urine": (2, 4), "drainage": (2100, 2400),
            "concentrations": ([1.5, 2.5], [7, 3]),
        },
        "persona": (
            "당신은 60세 여성 오미란입니다. 다낭성 신장으로 CAPD 2년 2개월째입니다.\n"
            "전반적으로 상태가 안정적이며 혈압과 혈당 조절이 잘 되고 있음.\n"
            "체중도 목표 범위에서 유지되고 있고 UF도 충분히 나오는 편.\n"
            "오늘 상태: 전반적으로 컨디션 양호, 투석 순조롭게 완료, "
            "다만 무릎 관절통이 있어 오후에 파스 붙임, 식사 잘 함."
        ),
    },
    "01033334444": {
        "vitals": {
            "sbp": (130, 158), "dbp": (78, 95),
            "weight": (66.0, 68.5), "glucose": (115, 165),
            "urine": (1, 2), "drainage": (2000, 2130),
            "concentrations": ([1.5, 2.5, 4.25], [3, 5, 2]),
        },
        "persona": (
            "당신은 75세 남성 노인환자입니다. 고혈압성 신부전으로 CAPD 3년 6개월째입니다.\n"
            "경도 인지장애가 있어 투석과 복약을 가족(아내)의 도움을 받아 진행함.\n"
            "약을 가끔 더블로 먹거나 빠뜨리는 일이 있음. 식이 제한 인식이 부족함.\n"
            "오늘 상태: 아내가 오늘 외출해서 혼자 투석을 진행했고 잘 됐는지 불안, "
            "약을 아침에 먹었는지 기억이 잘 안 남, 식사는 라면을 먹음, 전반적으로 피곤함."
        ),
    },
    "01044559234": {
        "vitals": {
            "sbp": (128, 152), "dbp": (82, 96),
            "weight": (57.5, 59.0), "glucose": (98, 138),
            "urine": (3, 5), "drainage": (2080, 2300),
            "concentrations": ([1.5, 2.5], [6, 4]),
        },
        "persona": (
            "당신은 48세 여성 직장인입니다. IgA 신병증으로 CAPD 10개월째입니다.\n"
            "낮에는 직장에 다니고 야간에 투석을 주로 진행하는 CAPD 방식 적용 중.\n"
            "직장 스트레스로 혈압이 오르는 경향이 있고 수면이 부족한 편.\n"
            "오늘 상태: 오늘 업무가 많아서 야간 교환이 늦어짐, "
            "피로감이 심하고 두통 약간 있음, 혈압이 평소보다 높게 측정됨, "
            "식사는 점심을 편의점 도시락으로 해결."
        ),
    },
    "01011223344": {
        "vitals": {
            "sbp": (135, 162), "dbp": (82, 98),
            "weight": (71.0, 73.5), "glucose": (145, 220),
            "urine": (1, 2), "drainage": (2000, 2150),
            "concentrations": ([2.5, 4.25], [5, 5]),
        },
        "persona": (
            "당신은 61세 남성입니다. 당뇨성 신부전으로 CAPD 2년 9개월째입니다.\n"
            "당뇨 합병증이 심각해 시력이 저하되어 있고 말초신경병증으로 발 감각이 둔함.\n"
            "혈당 조절이 매우 어렵고 인슐린 용량 조절이 자주 필요함.\n"
            "오늘 상태: 공복 혈당이 198로 높게 나옴, 인슐린을 평소보다 많이 맞았음, "
            "발 저림이 오늘 유독 심함, 눈이 흐릿한 느낌, 식사는 했으나 단 것을 먹은 것 같음."
        ),
    },
    "010-0000-9999": {
        "vitals": {
            "sbp": (133, 158), "dbp": (83, 96),
            "weight": (68.8, 70.2), "glucose": (118, 172),
            "urine": (1, 2), "drainage": (2000, 2130),
            "concentrations": ([1.5, 2.5, 4.25], [3, 5, 2]),
        },
        "persona": (
            "당신은 68세 남성 박영수입니다. 당뇨성 신부전으로 CAPD 2년 6개월째입니다.\n"
            "동반질환: 제2형 당뇨(20년), 고혈압, 만성심부전(EF 45%).\n"
            "UF량이 6개월 전 700~800ml에서 현재 350~500ml로 감소 중 (membrane 기능 저하 의심).\n"
            "체중이 3개월간 68→69.5kg으로 서서히 증가, 발목 부종 간헐적으로 있음.\n"
            "오늘 상태: 발목 부종 어제보다 약간 심함, 배액이 평소보다 적게 나온 느낌, "
            "피로감으로 낮잠, 저녁 혈압약 깜빡."
        ),
    },
}

DEFAULT_PROFILE = {
    "vitals": {
        "sbp": (125, 148), "dbp": (75, 92),
        "weight": (60.0, 65.0), "glucose": (100, 145),
        "urine": (2, 4), "drainage": (2050, 2250),
        "concentrations": ([1.5, 2.5], [6, 4]),
    },
    "persona": (
        "당신은 복막투석(CAPD)을 받고 있는 환자입니다.\n"
        "오늘 상태: 전반적으로 평소와 비슷하며 특이한 증상은 없습니다."
    ),
}


# ── 날짜 범위 ────────────────────────────────────────────────
def get_date_range() -> list[str]:
    if BACKFILL_START and BACKFILL_END:
        try:
            s = date.fromisoformat(BACKFILL_START)
            e = date.fromisoformat(BACKFILL_END)
            dates, d = [], s
            while d <= e:
                dates.append(d.isoformat())
                d += timedelta(days=1)
            return dates
        except ValueError as exc:
            log(f"백필 날짜 형식 오류: {exc} — 오늘만 실행")
    return [TODAY]


# ── DB 환자 목록 조회 ────────────────────────────────────────
def get_all_patient_phones() -> list[str]:
    if not DATABASE_URL:
        log("DATABASE_URL 미설정 — PATIENT_PROFILES 키 목록 사용")
        return list(PATIENT_PROFILES.keys())
    try:
        import psycopg2  # type: ignore
        conn = psycopg2.connect(DATABASE_URL)
        cur  = conn.cursor()
        cur.execute(
            "SELECT phone_number FROM users "
            "WHERE role = 'patient' AND is_active = true ORDER BY id"
        )
        phones = [r[0] for r in cur.fetchall() if r[0]]
        cur.close(); conn.close()
        log(f"DB 조회 완료: 환자 {len(phones)}명")
        return phones or list(PATIENT_PROFILES.keys())
    except Exception as exc:
        log(f"DB 조회 실패 ({exc}) — PATIENT_PROFILES 키 목록 사용")
        return list(PATIENT_PROFILES.keys())


# ── API 헬퍼 ────────────────────────────────────────────────
DEFAULT_TIMEOUT = 30

def api_get(session, path, **kwargs):
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return session.get(f"{BASE}{path}", **kwargs)

def api_post(session, path, **kwargs):
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return session.post(f"{BASE}{path}", **kwargs)


# ── Gemini 답변 생성 ─────────────────────────────────────────
def generate_ai_answers(ai_questions: list, persona: str) -> dict:
    if not ai_questions:
        return {}
    try:
        vertexai.init(project=GCP_PROJECT, location=GCP_REGION)
        model = GenerativeModel(GEMINI_MODEL)

        questions_text = json.dumps(
            [{"question_id": q.get("question_id") or q.get("id"),
              "question_text": q.get("question_text", ""),
              "question_type": q.get("question_type", "short_text"),
              "options": q.get("options")}
             for q in ai_questions],
            ensure_ascii=False,
        )

        prompt = (
            f"{persona}\n\n"
            "아래 질문 목록을 읽고 위 환자 상태에 맞게 사실적으로 답변해주세요.\n"
            "규칙:\n"
            "- question_type이 yes_no이면 환자 상태에 맞게 'yes' 또는 'no'로만 답하세요\n"
            "- short_text이면 1~2문장 한국어 구어체로 이 환자답게 구체적으로 답하세요\n"
            "- 반드시 JSON 배열만 반환하고 다른 텍스트는 절대 쓰지 마세요\n"
            '형식: [{"question_id": 숫자, "answer": "답변"}, ...]\n\n'
            f"질문 목록:\n{questions_text}"
        )

        response = model.generate_content(prompt)
        raw = response.text.strip()
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            return {}
        parsed = json.loads(match.group())
        return {str(item["question_id"]): str(item["answer"])
                for item in parsed if "question_id" in item and "answer" in item}
    except Exception as e:
        log(f"  Gemini 실패 (fallback): {e}")
        return {}


# ── 단계별 함수 ──────────────────────────────────────────────
def step_login(session, phone: str, password: str) -> str | None:
    r = api_post(session, "/api/v1/auth/login", json={"phone_number": phone, "password": password})
    if r.status_code != 200:
        log(f"  로그인 실패 ({phone}): {r.status_code} {r.text[:200]}")
        return None
    return r.json()["access_token"]


def step_check_existing(session, headers: dict, target_date: str) -> bool:
    r = api_get(session, f"/api/v1/records?date={target_date}", headers=headers)
    if r.status_code == 200:
        existing = r.json()
        if isinstance(existing, list) and existing:
            first = existing[0]
            if first.get("status") in ("submitted", "reviewed"):
                log(f"  [{target_date}] 이미 완료 (id={first['id']}) — 스킵")
                return True
    return False


def step_create_record(session, headers: dict, target_date: str, vitals: dict) -> int | None:
    sbp = random.randint(*vitals["sbp"])
    dbp = random.randint(*vitals["dbp"])
    conc_list, conc_weights = vitals["concentrations"]
    infusion_weight = 2000
    exchanges = []
    for i in range(1, 5):
        drainage     = random.randint(*vitals["drainage"])
        concentration = random.choices(conc_list, weights=conc_weights)[0]
        exchanges.append({
            "session_number": i,
            "exchange_time": f"{6 + (i-1)*4:02d}:00",
            "drainage_volume": drainage,
            "infusion_concentration": concentration,
            "infusion_weight": infusion_weight,
            "ultrafiltration": drainage - infusion_weight,
        })
    total_uf = sum(e["ultrafiltration"] for e in exchanges)
    record_data = {
        "record_date": target_date,
        "weight": round(random.uniform(*vitals["weight"]), 1),
        "blood_pressure": f"{sbp}/{dbp}",
        "urine_count": random.randint(*vitals["urine"]),
        "fasting_blood_glucose": float(random.randint(*vitals["glucose"])),
        "total_ultrafiltration": float(total_uf),
        "turbid_peritoneal": False,
        "memo": f"자동 테스트 기록 ({target_date})",
        "exchange_records": exchanges,
    }
    r = api_post(session, "/api/v1/records", json=record_data, headers=headers)
    if r.status_code == 201:
        record_id = r.json()["id"]
        log(f"  [{target_date}] 기록 생성 (id={record_id})")
        return record_id
    if r.status_code == 409:
        r2 = api_get(session, f"/api/v1/records?date={target_date}", headers=headers)
        if r2.status_code == 200 and r2.json():
            record_id = r2.json()[0]["id"]
            log(f"  [{target_date}] 기존 기록 재사용 (id={record_id})")
            return record_id
    if r.status_code == 403 and "담당 의사" in r.text:
        log(f"  [{target_date}] 기록 생성 건너뜀 (담당 의사 미지정): {r.text[:100]}")
        return "skip"
    log(f"  [{target_date}] 기록 생성 실패: {r.status_code} {r.text[:200]}")
    return None


def step_submit_record(session, headers: dict, record_id: int) -> int | None:
    r = api_post(session, f"/api/v1/records/{record_id}/submit", headers=headers)
    survey_id = None
    if r.status_code == 200:
        body = r.json()
        survey_id = body.get("survey_id") or body.get("id")
    elif r.status_code != 409:
        log(f"  제출 실패: {r.status_code} {r.text[:200]}")
        return None
    if not survey_id:
        r2 = api_get(session, f"/api/v1/surveys?record_id={record_id}", headers=headers)
        if r2.status_code == 200 and r2.json():
            survey_id = r2.json()[0]["id"]
        else:
            log(f"  survey_id 조회 실패: {r2.status_code}")
            return None
    return survey_id


def step_common_questions(session, headers: dict, record_id: int) -> None:
    r = api_get(session, f"/api/v1/surveys/my-responses/{record_id}", headers=headers)
    if r.status_code != 200:
        return
    common_qs = r.json().get("common_questions", [])
    if not common_qs:
        return
    YES_KEYWORDS = ["처방약", "약을 복용", "약 복용", "복약"]
    responses = []
    for q in common_qs:
        qt   = q.get("question_type", "short_text")
        text = q.get("question_text", "")
        if qt == "yes_no":
            choice = "yes" if any(kw in text for kw in YES_KEYWORDS) else "no"
            responses.append({"question_id": q["question_id"], "question_type": "common", "choice": choice, "text_answer": ""})
        else:
            responses.append({"question_id": q["question_id"], "question_type": "common", "choice": None, "text_answer": "특이사항 없음"})
    r2 = api_post(session, f"/api/v1/surveys/{record_id}/common",
                  json={"record_id": record_id, "responses": responses}, headers=headers)
    log(f"  공통 질문 {len(responses)}개 제출: {r2.status_code}")


def step_stream_ai_questions(session, token: str, record_id: int) -> list:
    ai_questions = []
    try:
        with session.get(
            f"{BASE}/api/v1/surveys/{record_id}/ai-questions/stream",
            params={"token": token},
            headers={"Accept": "text/event-stream"},
            stream=True, timeout=120,
        ) as resp:
            if resp.status_code != 200:
                log(f"  SSE 연결 실패: {resp.status_code}")
                return []
            for raw in resp.iter_lines():
                line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                if not line:
                    continue
                if line.startswith("event:") and line[6:].strip() == "done":
                    break
                if not line.startswith("data:"):
                    continue
                try:
                    obj = json.loads(line[5:].strip())
                    if obj.get("question_id"):
                        ai_questions.append(obj)
                        log(f"  질문: {obj.get('question_text','')[:55]}")
                except Exception:
                    pass
    except Exception as e:
        log(f"  SSE 오류: {e}")
    log(f"  AI 질문 {len(ai_questions)}개 수신")
    return ai_questions


def step_submit_ai_answers(session, headers: dict, record_id: int, ai_questions: list, persona: str) -> None:
    if not ai_questions:
        log("  AI 질문 없음 — 생략")
        return
    gemini_answers = generate_ai_answers(ai_questions, persona)
    log(f"  Gemini 답변: {len(gemini_answers)}개")
    ai_responses = []
    for q in ai_questions:
        qt  = q.get("question_type", "short_text")
        qid = q.get("question_id") or q.get("id")
        ans = gemini_answers.get(str(qid))
        if qt == "yes_no":
            choice = ans if ans in ("yes", "no") else "no"
            ai_responses.append({"question_id": qid, "question_type": "ai", "choice": choice, "text_answer": ""})
        else:
            ai_responses.append({"question_id": qid, "question_type": "ai", "choice": None,
                                  "text_answer": ans if ans else "특별한 증상 없이 평소와 비슷합니다."})
    r = api_post(session, f"/api/v1/surveys/{record_id}/ai",
                 json={"record_id": record_id, "responses": ai_responses}, headers=headers)
    log(f"  AI 답변 제출: {r.status_code}")


# ── 환자 1명 × 날짜 1일 ──────────────────────────────────────
def run_one(phone: str, password: str, target_date: str) -> bool:
    profile = PATIENT_PROFILES.get(phone, DEFAULT_PROFILE)
    vitals  = profile["vitals"]
    persona = profile["persona"]

    session = requests.Session()
    token = step_login(session, phone, password)
    if not token:
        return False
    headers = {"Authorization": f"Bearer {token}"}

    if step_check_existing(session, headers, target_date):
        return True

    record_id = step_create_record(session, headers, target_date, vitals)
    if record_id is None:
        return False
    if record_id == "skip":
        return "skip"

    survey_id = step_submit_record(session, headers, record_id)
    if survey_id is None:
        return False

    step_common_questions(session, headers, record_id)
    ai_questions = step_stream_ai_questions(session, token, record_id)
    step_submit_ai_answers(session, headers, record_id, ai_questions, persona)

    log(f"  ✅ 완료 — record={record_id} survey={survey_id} AI질문={len(ai_questions)}개")
    return True


# ── 메인 ────────────────────────────────────────────────────
def main():
    dates    = get_date_range()
    phones   = get_all_patient_phones()
    password = TEST_PASSWORD

    log(f"=== CAPD 테스트 러너 v3 시작 ===")
    log(f"  환자: {len(phones)}명, 날짜: {dates[0]} ~ {dates[-1]} ({len(dates)}일)")

    total = ok = fail = skip = 0

    for target_date in dates:
        log(f"\n── {target_date} ──")
        for phone in phones:
            total += 1
            log(f"  [{phone}] 시작")
            try:
                result = run_one(phone, password, target_date)
                if result == "skip":
                    skip += 1
                elif result:
                    ok += 1
                else:
                    fail += 1
            except Exception as exc:
                log(f"  [{phone}] 예외: {exc}")
                fail += 1

    log(f"\n=== 완료 === 성공={ok} 건너뜀={skip} 실패={fail} 전체={total}")
    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
