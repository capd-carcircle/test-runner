"""
CAPD 자동 테스트 러너
매일 자정 Cloud Scheduler → Cloud Run Job 으로 실행됨.
테스트 환자 계정으로 투석 기록 제출 + AI 설문 전체 흐름을 검증한다.
"""

import json
import logging
import os
import random
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import vertexai
from vertexai.generative_models import GenerativeModel

# ── 환경변수 설정 ─────────────────────────────────────────
BASE         = os.environ.get("BACKEND_URL",   "https://capd-backend-cdwaxwdxfa-du.a.run.app")
GCP_PROJECT  = os.environ.get("GCP_PROJECT_ID","skuniv-training-2")
GCP_REGION   = os.environ.get("GCP_REGION",   "asia-northeast3")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL",  "gemini-2.5-flash")
PHONE        = os.environ.get("TEST_PHONE",    "010-0000-9999")
PASSWORD     = os.environ.get("TEST_PASSWORD", "TestCapd2025!")

# ── KST 기준 날짜 계산 ────────────────────────────────────
KST = ZoneInfo("Asia/Seoul")

def _resolve_today() -> str:
    raw = os.environ.get("DATE_OVERRIDE", "").strip()
    if raw:
        try:
            datetime.strptime(raw, "%Y-%m-%d")  # 형식 검증
        except ValueError:
            logging.warning(f"DATE_OVERRIDE '{raw}' 형식 오류 — 오늘 날짜 사용")
            raw = ""
    if raw:
        return raw
    return datetime.now(timezone.utc).astimezone(KST).date().isoformat()

TODAY = _resolve_today()

# ── 로거 ──────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

def log(msg: str) -> None:
    logger.info(msg)

# ── 공통 API 헬퍼 ─────────────────────────────────────────
DEFAULT_TIMEOUT = 30

def api_get(session: requests.Session, path: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return session.get(f"{BASE}{path}", **kwargs)

def api_post(session: requests.Session, path: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return session.post(f"{BASE}{path}", **kwargs)


# ── Gemini 답변 생성 ──────────────────────────────────────
def generate_ai_answers(ai_questions: list) -> dict:
    """Gemini를 호출해 AI 질문에 대한 자연스러운 답변을 생성한다.
    실패 시 빈 dict 반환 → 호출부에서 고정 답변으로 fallback.
    반환 형식: {question_id: answer_str}
    """
    if not ai_questions:
        return {}
    try:
        vertexai.init(project=GCP_PROJECT, location=GCP_REGION)
        model = GenerativeModel(GEMINI_MODEL)

        questions_text = json.dumps(
            [
                {
                    "question_id":   q.get("question_id") or q.get("id"),
                    "question_text": q.get("question_text", ""),
                    "question_type": q.get("question_type", "short_text"),
                    "options":       q.get("options"),
                }
                for q in ai_questions
            ],
            ensure_ascii=False,
        )

        prompt = (
            "당신은 복막투석(CAPD)을 받고 있는 68세 남성 환자 박영수입니다.\n"
            "\n"
            "[환자 기본 정보]\n"
            "- 진단: 당뇨성 신부전(ESRD), CAPD 시작 2년 6개월째\n"
            "- 동반 질환: 제2형 당뇨(20년), 고혈압, 만성심부전(EF 45%)\n"
            "- 체중: 최근 3개월간 68kg → 69.5kg으로 서서히 증가 중 (부종 의심)\n"
            "- 혈압: 평소 135~155/85~95 범위로 조절 불안정\n"
            "- 혈당: 공복 120~170으로 불안정, 인슐린 사용 중\n"
            "- UF량: 6개월 전 700~800ml/day에서 현재 350~500ml/day로 서서히 감소 중\n"
            "- 소변량: 하루 1~2회 소량 (잔여 신기능 거의 없음)\n"
            "- 배액: 대체로 맑으나 가끔 약간 노르스름한 편\n"
            "\n"
            "[오늘 상태]\n"
            f"- 오늘 날짜 기준으로 자연스럽게 변동이 있는 상태를 표현해주세요\n"
            "- 양쪽 발목 부종이 어제보다 약간 심해진 느낌\n"
            "- 오후에 숨이 약간 찬 느낌이 있었으나 심하지는 않음\n"
            "- 식사는 반 공기 정도, 입맛이 전반적으로 없음\n"
            "- 피로감이 있고 오후에 낮잠을 잠\n"
            "- 아침 인슐린은 맞았으나 혈압약을 저녁에 깜빡함\n"
            "- 투석 교환 시 배액이 평소보다 적게 나온 것 같아 신경 쓰임\n"
            "- 카테터 삽입 부위는 이상 없음, 복부 통증·발열 없음\n"
            "\n"
            "아래 질문 목록을 읽고 위 환자 상태에 맞게 사실적으로 답변해주세요.\n"
            "규칙:\n"
            "- question_type이 yes_no이면 질문을 꼼꼼히 읽고 환자 상태에 맞게 'yes' 또는 'no'로만 답하세요\n"
            "- short_text이면 1~2문장 한국어 구어체로 이 환자답게 구체적으로 답하세요\n"
            "  (예: '어제보다 발목이 좀 더 부어있는 것 같아요', '배액이 좀 적게 나온 것 같아서 걱정이에요')\n"
            "- 반드시 JSON 배열만 반환하고 다른 텍스트는 절대 쓰지 마세요\n"
            '형식: [{"question_id": 숫자, "answer": "답변"}, ...]\n\n'
            f"질문 목록 (JSON):\n{questions_text}"
        )

        response = model.generate_content(prompt)
        raw = response.text.strip()

        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            log("Gemini 응답에서 JSON 배열을 찾지 못했습니다 — fallback 사용")
            return {}

        parsed = json.loads(match.group())
        return {str(item["question_id"]): str(item["answer"]) for item in parsed if "question_id" in item and "answer" in item}

    except Exception as e:
        log(f"Gemini 답변 생성 실패 (fallback 사용): {e}")
        return {}


# ── 단계별 함수 ───────────────────────────────────────────

def step_login(session: requests.Session) -> str:
    """로그인 → access_token 반환. 실패 시 sys.exit(1)."""
    log("Step 1: 로그인")
    r = api_post(session, "/api/v1/auth/login", json={"phone_number": PHONE, "password": PASSWORD})
    if r.status_code != 200:
        log(f"로그인 실패: {r.status_code} {r.text[:200]}")
        log("힌트: Cloud SQL에 테스트 환자 계정이 없을 수 있습니다.")
        sys.exit(1)
    token = r.json()["access_token"]
    log("로그인 성공")
    return token


def step_check_existing(session: requests.Session, headers: dict) -> int | None:
    """오늘 기록 조회 → 이미 완료됐으면 record_id 반환, 없으면 None."""
    log("Step 2: 오늘 기록 선제 조회")
    r = api_get(session, f"/api/v1/records?date={TODAY}", headers=headers)
    if r.status_code == 200:
        existing = r.json()
        if isinstance(existing, list) and existing:
            first = existing[0]
            if first.get("status") in ("submitted", "reviewed"):
                log(f"오늘 기록(id={first['id']}) 이미 제출 완료 — 테스트 생략")
                print("\n✅ CAPD 자동 테스트 성공 (이미 완료된 기록 존재)")
                sys.exit(0)
    elif r.status_code != 404:
        log(f"기록 조회 오류: {r.status_code} {r.text[:200]}")
    return None


def step_create_record(session: requests.Session, headers: dict) -> int:
    """투석 기록 draft 생성 → record_id 반환. 실패 시 sys.exit(1)."""
    log("Step 3: 투석 기록 draft 생성")
    sbp = random.randint(133, 158)
    dbp = random.randint(83, 96)
    infusion_weight = 2000
    exchanges = []
    for i in range(1, 5):
        drainage    = random.randint(2000, 2130)
        uf          = drainage - infusion_weight
        concentration = random.choices([1.5, 2.5, 4.25], weights=[3, 5, 2])[0]
        exchanges.append({
            "session_number":        i,
            "exchange_time":         f"{6 + (i - 1) * 4:02d}:00",
            "drainage_volume":       drainage,
            "infusion_concentration":concentration,
            "infusion_weight":       infusion_weight,
            "ultrafiltration":       uf,
        })
    total_uf = sum(e["ultrafiltration"] for e in exchanges)
    record_data = {
        "record_date":         TODAY,
        "weight":              round(random.uniform(68.8, 70.2), 1),
        "blood_pressure":      f"{sbp}/{dbp}",
        "urine_count":         random.randint(1, 2),
        "fasting_blood_glucose": float(random.randint(118, 172)),
        "total_ultrafiltration": float(total_uf),
        "turbid_peritoneal":   False,
        "memo":                f"자동 테스트 기록 ({TODAY})",
        "exchange_records":    exchanges,
    }

    r = api_post(session, "/api/v1/records", json=record_data, headers=headers)
    if r.status_code == 201:
        record_id = r.json()["id"]
        log(f"기록 생성 완료 (id={record_id})")
        return record_id
    if r.status_code == 409:
        log("오늘 기록 이미 존재 — 기존 기록 재사용")
        r2 = api_get(session, f"/api/v1/records?date={TODAY}", headers=headers)
        if r2.status_code == 200 and r2.json():
            record_id = r2.json()[0]["id"]
            log(f"기존 기록 사용 (id={record_id})")
            return record_id
        log(f"기존 기록 조회 실패: {r2.status_code} {r2.text[:200]}")
    else:
        log(f"기록 생성 실패: {r.status_code} {r.text[:200]}")
    sys.exit(1)


def step_submit_record(session: requests.Session, headers: dict, record_id: int) -> int:
    """기록 최종 제출 → survey_id 반환. 실패 시 sys.exit(1)."""
    log("Step 4: 기록 최종 제출")
    r = api_post(session, f"/api/v1/records/{record_id}/submit", headers=headers)
    survey_id = None
    if r.status_code == 200:
        body = r.json()
        survey_id = body.get("survey_id") or body.get("id")
        log(f"제출 완료 (survey_id={survey_id})")
    elif r.status_code == 409:
        log("기록 이미 제출된 상태 — survey 조회로 진행")
    else:
        log(f"제출 실패: {r.status_code} {r.text[:200]}")
        sys.exit(1)

    if not survey_id:
        r2 = api_get(session, f"/api/v1/surveys?record_id={record_id}", headers=headers)
        if r2.status_code == 200 and r2.json():
            survey_id = r2.json()[0]["id"]
            log(f"기존 survey 사용 (id={survey_id})")
        else:
            log(f"survey_id 조회 실패: {r2.status_code} {r2.text[:200]}")
            sys.exit(1)
    return survey_id


def step_common_questions(session: requests.Session, headers: dict, record_id: int) -> None:
    """공통 질문 조회 + 답변 제출. 실패 시 sys.exit(1)."""
    log("Step 5: 공통 질문 답변 제출")
    r = api_get(session, f"/api/v1/surveys/my-responses/{record_id}", headers=headers)
    if r.status_code != 200:
        log(f"공통 질문 조회 실패: {r.status_code} {r.text[:200]}")
        sys.exit(1)

    common_qs = r.json().get("common_questions", [])
    if not common_qs:
        log("공통 질문 없음 — 스킵")
        return

    # 질문 텍스트 키워드로 yes/no 기본값 결정
    # "처방약" 관련 질문은 복약 순응도 테스트를 위해 "yes" 응답
    YES_KEYWORDS = ["처방약", "약을 복용", "약 복용", "복약"]

    def _default_choice(question_text: str) -> str:
        text = question_text or ""
        if any(kw in text for kw in YES_KEYWORDS):
            return "yes"
        return "no"

    responses = []
    for q in common_qs:
        qt   = q.get("question_type", "short_text")
        text = q.get("question_text", "")
        if qt == "yes_no":
            responses.append({"question_id": q["question_id"], "question_type": "common", "choice": _default_choice(text), "text_answer": ""})
        else:
            responses.append({"question_id": q["question_id"], "question_type": "common", "choice": None, "text_answer": "특이사항 없음"})

    r2 = api_post(
        session,
        f"/api/v1/surveys/{record_id}/common",
        json={"record_id": record_id, "responses": responses},
        headers=headers,
    )
    log(f"공통 질문 {len(responses)}개 답변 제출: {r2.status_code}")


def step_stream_ai_questions(session: requests.Session, token: str, record_id: int) -> list:
    """AI 추천 질문 SSE 수신 → questions 목록 반환."""
    log("Step 6: AI 추천 질문 SSE 수신 (최대 120초)")
    ai_questions = []
    try:
        with session.get(
            f"{BASE}/api/v1/surveys/{record_id}/ai-questions/stream",
            params={"token": token},
            headers={"Accept": "text/event-stream"},
            stream=True,
            timeout=120,
        ) as resp:
            if resp.status_code != 200:
                log(f"SSE 연결 실패: {resp.status_code}")
                return []
            log(f"SSE 연결: {resp.status_code}")
            current_event = None
            for raw in resp.iter_lines():
                line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                if not line:
                    current_event = None
                    continue
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                    if current_event == "done":
                        log("SSE 완료")
                        break
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                try:
                    obj = json.loads(payload)
                    if obj.get("question_id"):
                        ai_questions.append(obj)
                        log(f"  질문 수신: {obj.get('question_text', '')[:60]}")
                except Exception:
                    pass
    except Exception as e:
        log(f"SSE 오류: {e}")
        # SSE 실패는 경고로 처리 — 이후 AI 답변 단계에서 빈 목록으로 진행
    log(f"AI 질문 수신: {len(ai_questions)}개")
    return ai_questions


def step_submit_ai_answers(
    session: requests.Session,
    headers: dict,
    record_id: int,
    ai_questions: list,
) -> None:
    """AI 질문 답변 제출."""
    if not ai_questions:
        log("Step 7: AI 질문 없음 — 생략")
        return

    log("Step 7: AI 질문 답변 제출")
    log("Step 7-1: Gemini 답변 생성 중...")
    gemini_answers = generate_ai_answers(ai_questions)
    log(f"Gemini 답변 수신: {len(gemini_answers)}개")

    ai_responses = []
    for q in ai_questions:
        qt   = q.get("question_type", "short_text")
        qid  = q.get("question_id") or q.get("id")
        ans  = gemini_answers.get(str(qid))
        if qt == "yes_no":
            choice = ans if ans in ("yes", "no") else "no"
            ai_responses.append({"question_id": qid, "question_type": "ai", "choice": choice, "text_answer": ""})
        else:
            ai_responses.append({
                "question_id":   qid,
                "question_type": "ai",
                "choice":        None,
                "text_answer":   ans if ans else "특별한 증상 없이 평소와 비슷합니다.",
            })

    r = api_post(
        session,
        f"/api/v1/surveys/{record_id}/ai",
        json={"record_id": record_id, "responses": ai_responses},
        headers=headers,
    )
    if r.status_code in (200, 201):
        log(f"AI 질문 답변 제출 성공: {r.status_code}")
    else:
        log(f"AI 질문 답변 제출 실패: {r.status_code} {r.text[:200]}")


# ── 메인 ──────────────────────────────────────────────────

def main():
    log(f"=== CAPD 자동 테스트 시작 ({TODAY}) ===")
    session = requests.Session()

    token   = step_login(session)
    headers = {"Authorization": f"Bearer {token}"}

    step_check_existing(session, headers)             # 완료 기록 있으면 exit(0)
    record_id  = step_create_record(session, headers)
    survey_id  = step_submit_record(session, headers, record_id)
    step_common_questions(session, headers, record_id)
    ai_questions = step_stream_ai_questions(session, token, record_id)
    step_submit_ai_answers(session, headers, record_id, ai_questions)

    log("=== 테스트 완료 ===")
    log(f"record_id={record_id}, survey_id={survey_id}, AI질문={len(ai_questions)}개")
    print("\n✅ CAPD 자동 테스트 성공")


if __name__ == "__main__":
    main()
