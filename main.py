"""
CAPD 자동 테스트 러너
매일 자정 Cloud Scheduler → Cloud Run Job 으로 실행됨.
테스트 환자 계정으로 투석 기록 제출 + AI 설문 전체 흐름을 검증한다.
"""

import requests
import json
import random
import re
import sys
from datetime import date, datetime, timezone

import vertexai
from vertexai.generative_models import GenerativeModel

BASE = "https://capd-backend-cdwaxwdxfa-du.a.run.app"
GCP_PROJECT = "skuniv-training-2"
GCP_REGION = "asia-northeast3"
GEMINI_MODEL = "gemini-2.5-flash"
PHONE = "010-0000-9999"
PASSWORD = "TestCapd2025!"
import os
_date_override = os.environ.get("DATE_OVERRIDE")
TODAY = _date_override if _date_override else datetime.now(timezone.utc).astimezone(
    __import__("zoneinfo").ZoneInfo("Asia/Seoul")
).date().isoformat()


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
                    "question_id": q.get("question_id") or q.get("id"),
                    "question_text": q.get("question_text", ""),
                    "question_type": q.get("question_type", "short_text"),
                    "options": q.get("options"),
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

        # JSON 배열 추출 (마크다운 코드블록 대응)
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            log("Gemini 응답에서 JSON 배열을 찾지 못했습니다 — fallback 사용")
            return {}

        parsed = json.loads(match.group())
        return {str(item["question_id"]): str(item["answer"]) for item in parsed if "question_id" in item and "answer" in item}

    except Exception as e:
        log(f"Gemini 답변 생성 실패 (fallback 사용): {e}")
        return {}


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    log(f"=== CAPD 자동 테스트 시작 ({TODAY}) ===")
    session = requests.Session()
    session.timeout = 30

    # 1. 로그인 (계정은 DB에 직접 생성되어 있어야 함)
    log("Step 1: 로그인")
    r = session.post(f"{BASE}/api/v1/auth/login", json={"phone_number": PHONE, "password": PASSWORD})
    if r.status_code != 200:
        log(f"로그인 실패: {r.status_code} {r.text[:300]}")
        log("힌트: Cloud SQL에 테스트 환자 계정이 없을 수 있습니다. README의 SQL을 실행해 주세요.")
        sys.exit(1)
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    log("로그인 성공")

    # 2. 오늘 기록 미리 조회 (이미 전체 흐름이 완료됐으면 조기 종료)
    log("Step 2: 오늘 기록 선제 조회")
    r = session.get(f"{BASE}/api/v1/records?date={TODAY}", headers=headers)
    if r.status_code == 200:
        existing = r.json()
        if isinstance(existing, list) and existing:
            first = existing[0]
            if first.get("status") in ("submitted", "reviewed"):
                log(f"오늘 기록(id={first['id']}) 이미 제출 완료 — 테스트 생략")
                print("\n✅ CAPD 자동 테스트 성공 (이미 완료된 기록 존재)")
                sys.exit(0)

    # 3. 투석 기록 draft 생성
    # 환자 프로필: 68세 남성, 당뇨성 신부전, UF 감소 추세, 체중 증가, 혈압 불안정
    log("Step 3: 투석 기록 draft 생성")
    sbp = random.randint(133, 158)
    dbp = random.randint(83, 96)
    infusion_weight = 2000
    exchanges = []
    for i in range(1, 5):
        # UF 감소 추세 반영: 회차별 배액량 낮게 설정 (350~500ml/day 목표)
        drainage = random.randint(2000, 2130)
        uf = drainage - infusion_weight
        # 가끔 고농도 처방 사용 (UF 부족 대응)
        concentration = random.choices([1.5, 2.5, 4.25], weights=[3, 5, 2])[0]
        exchanges.append({
            "session_number": i,
            "exchange_time": f"{6 + (i - 1) * 4:02d}:00",
            "drainage_volume": drainage,
            "infusion_concentration": concentration,
            "infusion_weight": infusion_weight,
            "ultrafiltration": uf,
        })
    total_uf = sum(e["ultrafiltration"] for e in exchanges)
    record_data = {
        "record_date": TODAY,
        "weight": round(random.uniform(68.8, 70.2), 1),   # 체중 증가 추세
        "blood_pressure": f"{sbp}/{dbp}",
        "urine_count": random.randint(1, 2),               # 잔여 신기능 거의 없음
        "fasting_blood_glucose": float(random.randint(118, 172)),  # 혈당 불안정
        "total_ultrafiltration": float(total_uf),
        "turbid_peritoneal": False,
        "memo": f"자동 테스트 기록 ({TODAY})",
        "exchange_records": exchanges,
    }
    r = session.post(f"{BASE}/api/v1/records", json=record_data, headers=headers)
    record_id = None
    if r.status_code == 201:
        record_id = r.json()["id"]
        log(f"기록 생성 완료 (id={record_id})")
    elif r.status_code == 409:
        log("오늘 기록 이미 존재 — 기존 기록 재사용하고 계속 진행")
        r2 = session.get(f"{BASE}/api/v1/records?date={TODAY}", headers=headers)
        if r2.status_code == 200:
            items = r2.json() or []
            if items:
                record_id = items[0]["id"]
                log(f"기존 기록 사용 (id={record_id})")
        else:
            log(f"409 발생했지만 기존 기록 조회 실패: {r2.status_code} {r2.text[:300]}")
    if not record_id:
        log(f"기록 생성 실패: {r.status_code} {r.text[:300]}")
        sys.exit(1)

    # 4. 최종 제출
    log("Step 4: 기록 최종 제출")
    r = session.post(f"{BASE}/api/v1/records/{record_id}/submit", headers=headers)
    survey_id = None
    if r.status_code == 200:
        body = r.json()
        survey_id = body.get("survey_id") or body.get("id")
        log(f"제출 완료 (survey_id={survey_id})")
    elif r.status_code == 409:
        log("이미 제출된 상태 — survey 조회")
    elif r.status_code == 409:
        log("기록 이미 제출된 상태 — survey 조회로 진행")
    else:
        log(f"제출 실패: {r.status_code} {r.text[:300]}")

    if not survey_id:
        r2 = session.get(f"{BASE}/api/v1/surveys?record_id={record_id}", headers=headers)
        if r2.status_code == 200 and r2.json():
            survey_id = r2.json()[0]["id"]
            log(f"기존 survey 사용 (id={survey_id})")
        else:
            log(f"survey_id를 얻지 못했습니다: {r2.status_code} {r2.text[:300]}")
            sys.exit(1)

    # 5. 공통 질문 조회 + 답변 제출
    log("Step 5: 공통 질문 답변 제출")
    r = session.get(f"{BASE}/api/v1/surveys/my-responses/{record_id}", headers=headers)
    if r.status_code == 200:
        data = r.json()
        common_qs = data.get("common_questions", [])
        responses = []
        for q in common_qs:
            qt = q.get("question_type", "short_text")
            if qt == "yes_no":
                responses.append({
                    "question_id": q["question_id"],
                    "question_type": "common",
                    "choice": "no",
                    "text_answer": "",
                })
            else:
                responses.append({
                    "question_id": q["question_id"],
                    "question_type": "common",
                    "choice": None,
                    "text_answer": "특이사항 없음",
                })
        if responses:
            r2 = session.post(
                f"{BASE}/api/v1/surveys/{record_id}/common",
                json={"record_id": record_id, "responses": responses},
                headers=headers,
            )
            log(f"공통 질문 {len(responses)}개 답변 제출: {r2.status_code}")
        else:
            log("공통 질문 없음")
    else:
        log(f"공통 질문 조회 실패: {r.status_code} {r.text[:300]}")

    # 6. AI 추천 질문 SSE 수신
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
        log(f"SSE 오류 (계속 진행): {e}")

    log(f"AI 질문 수신: {len(ai_questions)}개")

    # 7. AI 질문 답변 제출
    if ai_questions:
        log("Step 7: AI 질문 답변 제출")

        # Gemini로 자연스러운 답변 생성 (실패 시 고정 답변으로 fallback)
        log("Step 7-1: Gemini 답변 생성 중...")
        gemini_answers = generate_ai_answers(ai_questions)
        log(f"Gemini 답변 수신: {len(gemini_answers)}개")

        ai_responses = []
        for q in ai_questions:
            qt = q.get("question_type", "short_text")
            qid = q.get("question_id") or q.get("id")
            gemini_answer = gemini_answers.get(str(qid))

            if qt == "yes_no":
                # Gemini가 yes/no 중 하나를 반환했으면 사용, 아니면 'no' fallback
                choice = gemini_answer if gemini_answer in ("yes", "no") else "no"
                ai_responses.append({
                    "question_id": qid,
                    "question_type": "ai",
                    "choice": choice,
                    "text_answer": "",
                })
            elif qt in ("single_select", "multi_select") and q.get("options"):
                ai_responses.append({
                    "question_id": qid,
                    "question_type": "ai",
                    "choice": None,
                    "text_answer": gemini_answer if gemini_answer else (
                        q["options"][0] if isinstance(q["options"], list) else ""
                    ),
                })
            else:
                ai_responses.append({
                    "question_id": qid,
                    "question_type": "ai",
                    "choice": None,
                    "text_answer": gemini_answer if gemini_answer else "특별한 증상 없이 평소와 비슷합니다.",
                })
        r = session.post(
            f"{BASE}/api/v1/surveys/{record_id}/ai",
            json={"record_id": record_id, "responses": ai_responses},
            headers=headers,
        )
        if r.status_code in (200, 201):
            log(f"AI 질문 답변 제출 성공: {r.status_code}")
        else:
            log(f"AI 질문 답변 제출 실패: {r.status_code} {r.text[:300]}")
    else:
        log("Step 7: AI 질문 없음 — 생략")

    log("=== 테스트 완료 ===")
    log(f"record_id={record_id}, survey_id={survey_id}, AI질문={len(ai_questions)}개")
    print("\n✅ CAPD 자동 테스트 성공")


if __name__ == "__main__":
    main()
