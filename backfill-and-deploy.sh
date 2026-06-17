#!/bin/bash
# CAPD 테스트 러너 v2 — 배포 + 백필 + 스케줄러 업데이트 명령어 모음
# 실행 전: gcloud auth login / gcloud config set project skuniv-training-2

PROJECT=skuniv-training-2
REGION=asia-northeast3
REPO=asia-northeast3-docker.pkg.dev/skuniv-training-2/capd
IMAGE=$REPO/test-runner:latest
JOB=capd-test-runner
SCHEDULER=capd-daily-test
SA=capd-runner@skuniv-training-2.iam.gserviceaccount.com

# ── 1. 이미지 빌드 & 푸시 ─────────────────────────────────
echo "=== 1. Docker 빌드 & 푸시 ==="
docker build -t $IMAGE .
docker push $IMAGE

# ── 2. Cloud Run Job 업데이트 (새 이미지 + 다중환자 환경변수 추가) ──
# DATABASE_URL 은 Secret Manager 에서 주입
echo "=== 2. Cloud Run Job 업데이트 ==="
gcloud run jobs update $JOB \
  --image=$IMAGE \
  --region=$REGION \
  --set-secrets="DATABASE_URL=DATABASE_URL:latest" \
  --set-env-vars="MULTI_PATIENT=true,GCP_PROJECT_ID=$PROJECT,GCP_REGION=$REGION,GEMINI_MODEL=gemini-2.5-flash" \
  --service-account=$SA

# ── 3. 백필 1회 실행 (6월 1일 ~ 오늘) ────────────────────
# Cloud Run Job 실행 시 환경변수 오버라이드로 날짜 범위 지정
echo "=== 3. 백필 실행 (2026-06-01 ~ 2026-06-09) ==="
gcloud run jobs execute $JOB \
  --region=$REGION \
  --update-env-vars="BACKFILL_START=2026-06-01,BACKFILL_END=2026-06-09"

# 실행 로그 보기 (Job 실행 완료 후)
# gcloud run jobs executions list --job=$JOB --region=$REGION
# gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name=$JOB" --limit=200

# ── 4. 일별 Cloud Scheduler 업데이트 ─────────────────────
# 기존 스케줄러가 있으면 update, 없으면 create
echo "=== 4. Cloud Scheduler 업데이트 (매일 자정 KST) ==="
gcloud scheduler jobs update http $SCHEDULER \
  --location=$REGION \
  --schedule="0 0 * * *" \
  --time-zone="Asia/Seoul" \
  --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$JOB:run" \
  --message-body='{}' \
  --oauth-service-account-email=$SA \
  --attempt-deadline=3600s \
  2>/dev/null || \
gcloud scheduler jobs create http $SCHEDULER \
  --location=$REGION \
  --schedule="0 0 * * *" \
  --time-zone="Asia/Seoul" \
  --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$JOB:run" \
  --message-body='{}' \
  --oauth-service-account-email=$SA \
  --attempt-deadline=3600s

echo ""
echo "✅ 완료!"
echo ""
echo "[확인 명령어]"
echo "  백필 진행 확인: gcloud run jobs executions list --job=$JOB --region=$REGION"
echo "  로그 보기:      gcloud logging read \"resource.labels.job_name=$JOB\" --limit=300 --format='value(textPayload)'"
echo "  스케줄러 확인:  gcloud scheduler jobs describe $SCHEDULER --location=$REGION"
