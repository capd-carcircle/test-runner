"""
CAPD 자동 테스트 러너 v5
전략: 2단계 처리
  Phase 1 (스캔): 환자별 1회 로그인 → 날짜 범위 전체 기록 조회 → 작업 목록 생성
  Phase 2 (실행): 작업 목록만 처리 — 이미 완료된 건 건드리지 않음
"""

import json
import logging
import os
import random
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types
import requests

# ── 환경변수 ────────────────────────────────────────────────
BASE          = os.environ.get("BACKEND_URL",    "https://capd-backend-675812688902.asia-northeast3.run.app")
GCP_PROJECT   = os.environ.get("GCP_PROJECT_ID", "capd-carcircle-dev")
GCP_REGION    = os.environ.get("GCP_REGION",     "asia-northeast3")
GEMINI_MODEL  = os.environ.get("GEMINI_MODEL",   "gemini-2.5-flash")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TEST_PASSWORD = os.environ.get("TEST_PASSWORD",  "TestCapd2025!")
DATABASE_URL  = os.environ.get("DATABASE_URL",   "")
BACKFILL_START = os.environ.get("BACKFILL_START", "").strip()
BACKFILL_END   = os.environ.get("BACKFILL_END",   "").strip()
DATE_OVERRIDE  = os.environ.get("DATE_OVERRIDE",  "").strip()

GEMINI_MAX_RETRIES = int(os.environ.get("GEMINI_MAX_RETRIES", "4"))

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

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)
def log(msg: str) -> None:
    logger.info(msg)


# ── 환자별 임상 프로필 ────────────────────────────────────────
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
            "UF량이 6개월 전 700~800ml에서 현재 350~500ml로 감소 중.\n"
            "오늘 상태: 발목 부종 약간, 오후 숨참 경미, 배액이 평소보다 적게 나온 느낌, 피로감."
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
            "복막염 과거력 있음. 배액이 가끔 약간 혼탁하게 보여 걱정됨.\n"
            "오늘 상태: 배액 색이 평소보다 살짝 뿌연 것 같아 불안, 카테터 주변 약간 가려움."
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
            "혈압 조절이 어려워 항고혈압제 3종 복용 중.\n"
            "오늘 상태: 아침 혈압 168/98, 두통 경미, 다리가 무거운 느낌."
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
            "최근 체중이 49→47kg으로 감소 중, 식욕 부진.\n"
            "오늘 상태: 아침부터 속이 메슥거리고 밥을 거의 못 먹음, 체중 또 줄어있어 걱정."
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
            "만성심부전(EF 40%) 동반, 체액 관리 매우 중요.\n"
            "오늘 상태: 계단 오를 때 숨이 많이 차서 멈춰야 했음, 발목 부종 약간."
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
            "당신은 58세 여성 한지영입니다. 혈액투석에서 복막투석으로 전환 5개월됐습니다.\n"
            "복막투석에 아직 적응 중.\n"
            "오늘 상태: 교환 과정에서 실수가 있었던 것 같아 불안, 배액은 맑고 양 정상."
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
            "약 복용을 자주 잊어버리고 식이 제한을 잘 지키지 않음.\n"
            "오늘 상태: 어제 저녁과 오늘 아침 혈압약 둘 다 깜빡, 혈압 172/102, 두통."
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
            "혈압, 혈당 모두 양호하게 조절, 전반적으로 상태 안정적.\n"
            "오늘 상태: 전반적으로 컨디션 좋음, 직장 스트레스로 피로감 약간."
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
            "카테터 삽입 부위 주변에 간헐적으로 발적과 분비물 경향.\n"
            "오늘 상태: 카테터 부위가 어제부터 약간 빨개지고 누르면 아픔, 걱정됨."
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
            "전반적으로 상태가 안정적, 혈압과 혈당 조절 잘 됨.\n"
            "오늘 상태: 전반적으로 컨디션 양호, 무릎 관절통 있어 파스 붙임."
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
            "경도 인지장애, 투석과 복약을 가족 도움으로 진행.\n"
            "오늘 상태: 아내가 외출해서 혼자 투석, 약을 먹었는지 기억 안 남, 라면 먹음."
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
            "낮에 직장, 야간에 투석하는 방식. 직장 스트레스로 혈압 오르는 경향.\n"
            "오늘 상태: 업무 많아 야간 교환 늦어짐, 피로 심하고 두통 약간, 점심 편의점 도시락."
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
            "시력 저하, 말초신경병증으로 발 감각 둔함. 혈당 조절 매우 어려움.\n"
            "오늘 상태: 공복 혈당 198, 인슐린 평소보다 많이 맞음, 발 저림 심함."
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
            "오늘 상태: 발목 부종 약간, 배액이 평소보다 적게 나온 느낌, 저녁 혈압약 깜빡."
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
        import psycopg2
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

    client = genai.Client(api_key=GEMINI_API_KEY)

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

    for attempt in range(GEMINI_MAX_RETRIES):
        try:
            response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
            raw = response.text.strip()
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            if not match:
                return {}
            parsed = json.loads(match.group())
            return {str(item["question_id"]): str(item["answer"])
                    for item in parsed if "question_id" in item and "answer" in item}
        except Exception as e:
            err_str = str(e)
            if any(code in err_str for code in ("429", "503", "ResourceExhausted", "RESOURCE_EXHAUSTED")):
                wait = (2 ** attempt) * 10 + random.uniform(0, 3)
                log(f"  Gemini 쿼터 초과 (시도 {attempt+1}/{GEMINI_MAX_RETRIES}) — {wait:.0f}초 대기")
                time.sleep(wait)
            else:
                log(f"  Gemini 실패: {e}")
                return {}
    log("  Gemini 최대 재시도 초과")
    return {}


# ── 단계 함수 ────────────────────────────────────────────────
def step_login(session, phone: str, password: str) -> str | None:
    r = api_post(session, "/api/v1/auth/login", json={"phone_number": phone, "password": password})
    if r.status_code != 200:
        log(f"  로그인 실패 ({phone}): {r.status_code}")
        return None
    return r.json()["access_token"]


def step_get_existing(session, headers: dict, target_date: str) -> tuple:
    r = api_get(session, f"/api/v1/records?date={target_date}", headers=headers)
    if r.status_code == 200:
        existing = r.json()
        if isinstance(existing, list) and existing:
            first = existing[0]
            return first.get("id"), first.get("status")
    return None, None


def step_get_survey_state(session, headers: dict, record_id: int) -> dict:
    """survey 상태 반환: {common_total, common_answered, ai_total, ai_answered, unanswered_common, unanswered_ai}"""
    r = api_get(session, f"/api/v1/surveys/my-responses/{record_id}", headers=headers)
    if r.status_code != 200:
        return {}
    data = r.json()
    common_qs = data.get("common_questions", [])
    ai_qs     = data.get("ai_questions", [])
    return {
        "common_total":     len(common_qs),
        "common_answered":  sum(1 for q in common_qs if q.get("answered")),
        "ai_total":         len(ai_qs),
        "ai_answered":      sum(1 for q in ai_qs if q.get("answered")),
        "unanswered_common": [q for q in common_qs if not q.get("answered")],
        "unanswered_ai":     [q for q in ai_qs     if not q.get("answered")],
    }


def step_submit_record(session, headers: dict, record_id: int) -> int | None:
    r = api_post(session, f"/api/v1/records/{record_id}/submit", headers=headers)
    if r.status_code == 200:
        body = r.json()
        sid = body.get("survey_id") or body.get("id")
        if sid:
            return sid
    elif r.status_code != 409:
        log(f"  제출 실패: {r.status_code} {r.text[:200]}")
        return None
    # 409 또는 sid 없음 → 조회
    r2 = api_get(session, f"/api/v1/surveys?record_id={record_id}", headers=headers)
    if r2.status_code == 200 and r2.json():
        return r2.json()[0]["id"]
    # survey 조회 실패해도 record_id 자체는 있으므로 -1 반환 (실패 아님)
    return -1



def step_fill_common(session, headers: dict, record_id: int, unanswered: list) -> None:
    if not unanswered:
        return
    YES_KEYWORDS = ["처방약", "약을 복용", "약 복용", "복약"]
    responses = []
    for q in unanswered:
        qt   = q.get("question_type", "short_text")
        text = q.get("question_text", "")
        qid = q.get("question_id") or q.get("id")
        if not qid:
            continue
        if qt == "yes_no":
            choice = "yes" if any(kw in text for kw in YES_KEYWORDS) else "no"
            responses.append({"question_id": qid, "question_type": "common",
                               "choice": choice, "text_answer": ""})
        else:
            responses.append({"question_id": qid, "question_type": "common",
                               "choice": None, "text_answer": "특이사항 없음"})
    r = api_post(session, f"/api/v1/surveys/{record_id}/common",
                 json={"record_id": record_id, "responses": responses}, headers=headers)
    log(f"  공통 질문 {len(responses)}개 제출: {r.status_code}")


def step_trigger_ai_generate(session, headers: dict, record_id: int, timeout: int = 600) -> int:
    """
    비스트리밍 AI 질문 생성 엔드포인트 호출.
    202(BackgroundTasks) 또는 200 모두 처리 — 생성 완료까지 polling.
    생성된 질문 수 반환 (실패 시 0).
    """
    url = f"{BASE}/api/v1/surveys/{record_id}/ai-questions/generate"
    try:
        r = session.post(url, headers=headers, timeout=timeout)
        if r.status_code != 200:
            log(f"  AI 질문 생성 실패: {r.status_code}")
            return 0
        data = r.json()
        total = data.get("total", 0)
        generated = data.get("generated", 0)
        log(f"  AI 질문 생성 완료 — generated={generated}, total={total}")
        return total
    except Exception as e:
        log(f"  AI 질문 생성 예외: {e}")
        return 0


def step_fill_ai(session, headers: dict, record_id: int, unanswered: list, persona: str) -> None:
    if not unanswered:
        return
    gemini_answers = generate_ai_answers(unanswered, persona)
    ai_responses = []
    for q in unanswered:
        qt  = q.get("question_type", "short_text")
        qid = q.get("question_id") or q.get("id")
        ans = gemini_answers.get(str(qid))
        if qt == "yes_no":
            choice = ans if ans in ("yes", "no") else "no"
            ai_responses.append({"question_id": qid, "question_type": "ai",
                                  "choice": choice, "text_answer": ""})
        else:
            ai_responses.append({"question_id": qid, "question_type": "ai",
                                  "choice": None,
                                  "text_answer": ans or "특별한 증상 없이 평소와 비슷합니다."})
    r = api_post(session, f"/api/v1/surveys/{record_id}/ai",
                 json={"record_id": record_id, "responses": ai_responses}, headers=headers)
    log(f"  AI 답변 {len(ai_responses)}개 제출: {r.status_code}")


def step_create_record(session, headers: dict, target_date: str, vitals: dict) -> int | None | str:
    sbp = random.randint(*vitals["sbp"])
    dbp = random.randint(*vitals["dbp"])
    conc_list, conc_weights = vitals["concentrations"]
    infusion_weight = 2000
    exchanges = []
    for i in range(1, 5):
        drainage      = random.randint(*vitals["drainage"])
        concentration = random.choices(conc_list, weights=conc_weights)[0]
        exchanges.append({
            "session_number": i,
            "exchange_time": f"{6 + (i-1)*4:02d}:00",
            "drainage_volume": drainage,
            "infusion_concentration": concentration,
            "infusion_weight": infusion_weight,
            "ultrafiltration": drainage - infusion_weight,
        })
    record_data = {
        "record_date": target_date,
        "weight": round(random.uniform(*vitals["weight"]), 1),
        "blood_pressure": f"{sbp}/{dbp}",
        "urine_count": random.randint(*vitals["urine"]),
        "fasting_blood_glucose": float(random.randint(*vitals["glucose"])),
        "total_ultrafiltration": float(sum(e["ultrafiltration"] for e in exchanges)),
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
        log(f"  [{target_date}] 담당 의사 미지정 — 스킵")
        return "skip"
    log(f"  [{target_date}] 기록 생성 실패: {r.status_code} {r.text[:200]}")
    return None



# ── Phase 1: 환자별 스캔 ─────────────────────────────────────
def scan_patient(phone: str, password: str, dates: list[str]) -> list[dict]:
    """
    환자 1명 로그인 → 전체 날짜 스캔 → 작업 필요한 항목만 반환
    반환 형식:
      {"type": "new",  "phone": ..., "date": ...}
      {"type": "fill", "phone": ..., "date": ..., "record_id": ..., "status": ...,
       "missing_common": [...], "missing_ai": [...]}
    """
    profile = PATIENT_PROFILES.get(phone, DEFAULT_PROFILE)
    session = requests.Session()
    token = step_login(session, phone, password)
    if not token:
        return []
    headers = {"Authorization": f"Bearer {token}"}

    work = []
    for target_date in dates:
        record_id, status = step_get_existing(session, headers, target_date)

        if status == "reviewed":
            continue  # 완료 — 건너뜀

        if record_id is None:
            # 기록 자체 없음
            work.append({"type": "new", "phone": phone, "date": target_date})
            continue

        # submitted / draft — survey 상태 확인
        state = step_get_survey_state(session, headers, record_id)
        if not state:
            work.append({"type": "fill", "phone": phone, "date": target_date,
                         "record_id": record_id, "status": status,
                         "missing_common": [], "missing_ai": []})
            continue

        missing_common = state["unanswered_common"]
        missing_ai     = state["unanswered_ai"]
        ai_total       = state["ai_total"]

        # draft이거나, 미답변 있거나, AI 질문이 아예 0개(미생성)이면 처리 필요
        if status == "draft" or missing_common or missing_ai or ai_total == 0:
            work.append({"type": "fill", "phone": phone, "date": target_date,
                         "record_id": record_id, "status": status,
                         "missing_common": missing_common, "missing_ai": missing_ai,
                         "ai_total": ai_total})
        # else: submitted + 전부 answered → 완료, 건너뜀

    log(f"  [{phone}] 스캔 완료: {len(dates)}일 중 작업 필요 {len(work)}건")
    return work


# ── Phase 2: 작업 항목 실행 ──────────────────────────────────
def execute_work_item(item: dict, password: str) -> bool:
    phone       = item["phone"]
    target_date = item["date"]
    profile     = PATIENT_PROFILES.get(phone, DEFAULT_PROFILE)
    persona     = profile["persona"]
    vitals      = profile["vitals"]

    session = requests.Session()
    token = step_login(session, phone, password)
    if not token:
        return False
    headers = {"Authorization": f"Bearer {token}"}

    # 1. 기록 없으면 생성
    record_id, status = step_get_existing(session, headers, target_date)
    if record_id is None:
        record_id = step_create_record(session, headers, target_date, vitals)
        if record_id is None:
            return False
        if record_id == "skip":
            return True
        status = "draft"

    # 2. draft면 제출
    if status == "draft":
        sid = step_submit_record(session, headers, record_id)
        if sid is None:
            return False

    # 3. 공통 질문 — 미답변 답변
    state = step_get_survey_state(session, headers, record_id)
    if state and state["unanswered_common"]:
        step_fill_common(session, headers, record_id, state["unanswered_common"])

    # 4. AI 질문 — 비스트리밍 생성 후 답변
    state = step_get_survey_state(session, headers, record_id)
    if not state:
        log(f"  ✅ [{phone}] {target_date} 완료 (record={record_id})")
        return True

    if state["ai_total"] == 0:
        log(f"  AI 질문 없음 → 생성 요청")
        total = step_trigger_ai_generate(session, headers, record_id)
        if total > 0:
            state = step_get_survey_state(session, headers, record_id)
        else:
            log(f"  AI 질문 미생성 — 건너뜀")

    if state and state["unanswered_ai"]:
        step_fill_ai(session, headers, record_id, state["unanswered_ai"], persona)

    log(f"  ✅ [{phone}] {target_date} 완료 (record={record_id})")
    return True


# ── 사전 로그인 검증 ─────────────────────────────────────────
def preflight_login(phones: list, password: str) -> list:
    log(f"\n=== 로그인 사전 검증 ({len(phones)}명) ===")
    valid = []
    for phone in phones:
        session = requests.Session()
        token = step_login(session, phone, password)
        if token:
            valid.append(phone)
            log(f"  ✅ {phone}")
        else:
            log(f"  ❌ {phone} — 제외")
    log(f"  → 유효 {len(valid)}명 / 제외 {len(phones)-len(valid)}명\n")
    return valid


# ── 메인 ────────────────────────────────────────────────────
def main():
    dates    = get_date_range()
    phones   = get_all_patient_phones()
    password = TEST_PASSWORD

    log(f"=== CAPD 테스트 러너 v5 시작 ===")
    log(f"  전체 환자: {len(phones)}명 | 날짜: {dates[0]} ~ {dates[-1]} ({len(dates)}일)")

    # ── Phase 0: 로그인 사전 검증 ──
    phones = preflight_login(phones, password)
    if not phones:
        log("로그인 가능한 환자 없음 — 종료")
        sys.exit(1)

    # ── Phase 1: 전체 스캔 (직렬) ──
    log(f"\n=== Phase 1: 전체 스캔 ({len(phones)}명 × {len(dates)}일) ===")
    all_work: list[dict] = []
    for phone in phones:
        items = scan_patient(phone, password, dates)
        all_work.extend(items)

    new_count  = sum(1 for w in all_work if w["type"] == "new")
    fill_count = sum(1 for w in all_work if w["type"] == "fill")
    done_count = len(phones) * len(dates) - new_count - fill_count
    log(f"\n  스캔 결과: 신규={new_count}건 | 보충={fill_count}건 | 이미완료={done_count}건")

    if not all_work:
        log("모든 기록 완료 — 작업 없음")
        return

    # ── Phase 2: 실행 (직렬, 항목 간 2초 간격) ──
    log(f"\n=== Phase 2: 실행 ({len(all_work)}건) ===")
    ok = fail = 0
    for i, item in enumerate(all_work, 1):
        log(f"  [{i}/{len(all_work)}] {item['phone']} {item['date']} ({item['type']})")
        try:
            result = execute_work_item(item, password)
            if result:
                ok += 1
            else:
                fail += 1
        except Exception as e:
            log(f"  예외: {e}")
            fail += 1
        time.sleep(2)  # 백엔드/AI 서비스 부하 방지

    log(f"\n=== 완료 === 성공={ok} 실패={fail} 전체={len(all_work)}")
    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
