"""경로와 실행 설정. 날짜는 모델이 추론하지 않고 시스템이 고정한다(데이터 기준 시각)."""
import glob
import os
from datetime import datetime

# 외부 전송 차단 (검토 H5): LangSmith 등 추적이 셸 설정으로 켜져도 강제로 끈다.
for _k in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
    os.environ[_k] = "false"

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))      # draft_chatbot/
ROOT = os.path.dirname(BASE)                                             # 7주차 실습/
RULES_DIR = os.environ.get("BOT_RULES_DIR", os.path.join(BASE, "rules"))
VAR_DIR = os.environ.get("BOT_VAR_DIR", os.path.join(BASE, "var"))       # 체크포인트·결과·로그 (사내 보관)
RESULTS_DIR = os.path.join(VAR_DIR, "results")
LOG_PATH = os.path.join(VAR_DIR, "requests.jsonl")
INDEX_PATH = os.path.join(VAR_DIR, "requests.json")
CHECKPOINT_DB = os.path.join(VAR_DIR, "checkpoints.db")
DATA_DB = os.path.join(BASE, "var", "nuri_masked.db")                    # 데이터 DB는 공유

DDL_DIR = os.environ.get("BOT_DDL_DIR", os.path.join(ROOT, "kit_download/ddl/A-schema/DDL"))
_masked = sorted(glob.glob(os.path.join(ROOT, "kit_download/masked/A-db/*/")))
MASKED_DIR = os.environ.get("BOT_DATA_DIR", _masked[0] if _masked else "")

# 데이터 안내문(2026-11-20 18:00 추출) 기준. 챗봇의 '오늘'은 이 시각으로 고정한다.
DATA_AS_OF = {
    "주문·행동": datetime(2026, 11, 19, 23, 59, 59),     # 야간 배치, 11/19분까지
    "동의·발송": datetime(2026, 11, 20, 18, 0, 0),       # 추출 시각까지
}
AS_OF_TS = "2026-11-20 18:00:00"                          # 발송 제한 계산 기준
TODAY = DATA_AS_OF["주문·행동"].date()
ORDER_DATA_FROM = "20260501"                              # 주문 계열은 2026-05-01 이후분만

# 'offline'(기본): 외부 모델 호출 없음 — 고객 제약 "LLM API 계약 없음"
# 'llm'        : 모델 경로·반출 범위 승인 후 구현 (현재 미구현)
PROVIDER = os.environ.get("BOT_MODEL_PROVIDER", "offline")
PROMPT_VERSION = "offline-rules-2"

MAX_CLARIFY = 3          # 되묻기 상한 (마케터 피로 방지)
MAX_REVISIONS = 2        # BA 수정 SQL 반복 상한

# 코드 고정 실행 가드 (검토 H4) — CSV로 끌 수 없다
EXEC_TIMEOUT_SEC = 10
EXEC_MAX_ROWS = 200_000

os.makedirs(RESULTS_DIR, exist_ok=True)


def require_data_dir():
    if not MASKED_DIR or not os.path.isdir(MASKED_DIR):
        raise RuntimeError("마스킹 데이터 폴더를 찾지 못했습니다. BOT_DATA_DIR 로 지정해 주세요.")
    return MASKED_DIR
