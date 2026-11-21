"""BA 요청 처리 draft 챗봇 — 스키마 v2 (구조만, 로직 없음).

v1 → v2 변경 근거: 스키마_재점검.md
- 가치 비중 BA 70 : 마케터 30. 고객은 BA파트, KPI는 BA 적체 해소와 정확도.
- 검토 에이전트 지적(높음 1~5) 반영: 검토 이력 누적, 최종 SQL 재검증, LLM 출력과
  시스템 필드 분리, HITL 페이로드 정의, 개인정보 차단.
- DDL·대시보드에서 발견한 함정을 SqlCheck 규칙과 카탈로그 필드로 반영.
- KPI 두 갈래(① 최종 결과 정확도, ② 초안 무수정 승인률)와 리드타임을 계산할 필드.

보안 확인 전 가정(A1): 실데이터는 LLM에 넣지 않는다. LLM 입력은 요청 문장과 메타데이터만.
"""

from __future__ import annotations

import operator
from datetime import date, datetime
from typing import Annotated, Literal, Optional, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 0. 공통 열거형
# ---------------------------------------------------------------------------

# Jev Choice. 'out_of_scope' = 해당 없음 (닫힌 질문 원칙)
RequestType = Literal["target_extraction", "aggregate_report", "out_of_scope"]

HandoffReason = Literal[
    "out_of_scope", "clarify_limit", "sql_fail_2x", "rejected", "id_mapping_required",
]

FailureTag = Literal[
    # 요청 해석
    "intent_misclassified", "requirement_misread", "missed_clarification",
    # 데이터 선택
    "wrong_table",            # _BAK·TMP_ 등 함정 테이블 포함
    "wrong_term_mapping",     # 업무 용어 ↔ 컬럼 (예: 매출 = NET_AMT?)
    "id_system_mismatch",     # MBR_NO ↔ CUST_ID 직접 조인
    # SQL 구조
    "wrong_period_filter",    # 시작일만 조건, 하드코딩 기간, 날짜 형식 변환 누락
    "fan_out_join",           # 1:N 조인으로 건수·금액 부풀림
    "missing_condition",      # 취소·반품·유효기간 조건 누락
    "pii_exposure",
    "other",
]


# ---------------------------------------------------------------------------
# 1. 요청서 — LLM 출력(SpecExtraction)과 시스템 필드(RequestSpec)를 분리
#    [마케터 가치] 대강 써도 되는 요청, 되묻기 즉시 응답
# ---------------------------------------------------------------------------

class Condition(BaseModel):
    term: str = Field(description="마케터 표현 (예: '최근 구매 고객')")
    raw_expr: str = Field(description="조건 원문 (예: '최근 3개월 2회 이상 구매')")
    term_id: Optional[str] = None          # BusinessTerm 매핑 (시스템이 채움)


class SpecExtraction(BaseModel):
    """LLM structured output 대상. 시스템 필드는 여기 없다 — 재추출해도 이력이 보존된다."""
    purpose: str
    request_type: RequestType
    conditions: list[Condition] = Field(default_factory=list)
    period_expr: Optional[str] = Field(None, description="'10월', '작년 같은 시즌 대비' 등 원문")
    metrics: list[str] = Field(default_factory=list)
    output_format: Literal["count_only", "list", "push_target_list", "unknown"] = "unknown"


class OpenQuestion(BaseModel):
    field: str
    question: str
    answer: Optional[str] = None
    asked_at: datetime
    answered_at: Optional[datetime] = None


class RequestSpec(BaseModel):
    """시스템이 관리. extraction 만 LLM 결과로 교체된다."""
    request_id: str                         # thread_id 와 동일
    requester_id: str
    raw_request: str                        # 원문 보존 (평가 재현)
    created_at: datetime
    due_date: Optional[date] = None
    extraction: SpecExtraction
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    jira_key: Optional[str] = None          # 연동은 보안 확인 후 (A3)


# ---------------------------------------------------------------------------
# 2. 시맨틱 레이어 — [BA 가치의 핵심] 낯선 35개 테이블을 BA가 확정한 지식으로
#    처음엔 스킬(마크다운)로 시작하고, 그래프에는 아래 필드만 필요
# ---------------------------------------------------------------------------

TimeFormat = Literal["yyyymmdd", "yyyymmddhhmiss", "text_timestamp", "timestamp"]
IdSystem = Literal["MBR_NO", "CUST_ID", "none"]


class ColumnMeta(BaseModel):
    name: str
    data_type: str
    business_meaning: str = ""
    time_format: Optional[TimeFormat] = None       # PAY_TS 만 text_timestamp 등
    code_group: Optional[str] = None               # → LX_ZCM_CD.CD_GRP
    is_pii: bool = False


class TableMeta(BaseModel):
    table: str
    domain: str                                    # 회원·주문·상품·혜택·행동·푸시·집계·공통
    id_system: IdSystem = "none"
    allowed: bool = False                          # 화이트리스트
    excluded_reason: Optional[str] = None          # '백업(_BAK)', '개인 임시(TMP_)'
    is_history: bool = False                       # 시점 조인 필요 (DNS2_H, KRP4_PRC)
    refresh: Optional[str] = None                  # 'daily 06:00, D-1' (집계 테이블)
    columns: list[ColumnMeta] = Field(default_factory=list)


class JoinPath(BaseModel):
    left: str
    right: str
    keys: list[list[str]]                          # [[left_col, right_col], ...] — 직렬화 안전
    cardinality: Literal["1:1", "1:N", "N:1", "N:M"]
    verified_by: Optional[str] = None              # 'dashboard_panel_2' 또는 BA 이름


class BusinessTerm(BaseModel):
    term_id: str
    term: str                                      # '순매출', '쿠폰 사용률', '수신동의 고객'
    definition: str
    sql_expression: str
    source_tables: list[str]
    matches_dashboard: Optional[bool] = None       # 대시보드 숫자와 같은 기준인가
    confirmed_by: Optional[str] = None             # None = 미확정 → 초안에 경고 표시
    version: int = 1


class ReferenceQuery(BaseModel):
    """BA가 이미 쓰는 쿼리 (대시보드 패널 등). 초안의 참고 예시이자 평가셋 씨앗."""
    ref_id: str                                    # 'dashboard_panel_4'
    title: str
    sql: str
    known_issues: list[FailureTag] = Field(default_factory=list)
    status: Literal["verified", "has_issues", "unreviewed"] = "unreviewed"


# ---------------------------------------------------------------------------
# 3. SQL 초안·검사·미리보기 — [BA 가치] 검사를 통과한 초안만 BA 앞에 온다
# ---------------------------------------------------------------------------

class QueryDraft(BaseModel):
    draft_id: str
    request_id: str
    attempt: int                                   # 같은 라운드 내 재작성 (최대 2)
    revision_round: int = 0                        # BA 수정 요청 횟수
    sql: str
    tables_used: list[str]
    term_versions: dict[str, int] = Field(default_factory=dict)   # {term_id: version}
    reference_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    prompt_version: str
    author: Literal["llm", "ba"] = "llm"           # BA가 고친 SQL도 같은 검사를 거친다


class SqlCheck(BaseModel):
    """결정적 검사 — 코드로 판정. 발견된 함정 규칙을 그대로 옮김."""
    draft_id: str
    select_only: bool
    tables_whitelisted: bool                       # _BAK·TMP_ 자동 제외
    period_bounded: bool                           # 시작·종료 모두 있음 (패널4 오류)
    period_hardcoded: bool                         # 반복 리포트에서 고정 날짜 (패널1~3)
    time_format_ok: bool                           # 컬럼별 형식에 맞는 비교
    id_system_consistent: bool                     # MBR_NO = CUST_ID 조인 금지
    fan_out_risk: list[str] = Field(default_factory=list)   # 1:N 조인 경로
    pii_columns: list[str] = Field(default_factory=list)
    explain_ok: bool
    passed: bool                                   # property 아닌 저장 필드 (Trace에 남김)
    errors: list[str] = Field(default_factory=list)


class ExecPreview(BaseModel):
    draft_id: str
    row_count: int
    columns: list[str]
    data_as_of: datetime                           # [마케터 가치] 답의 기준 시각
    result_ref: Optional[str] = None               # 사내 저장 위치, BA만 열람 (A1)


# ---------------------------------------------------------------------------
# 4. 사람 개입(HITL) 페이로드 — interrupt() / Command(resume=...)
# ---------------------------------------------------------------------------

class ClarifyInterrupt(BaseModel):
    kind: Literal["clarify"] = "clarify"
    request_id: str
    questions: list[OpenQuestion]


class ClarifyResume(BaseModel):
    answers: dict[str, str]                        # {field: answer}


class ReviewInterrupt(BaseModel):
    kind: Literal["review"] = "review"
    request_id: str
    spec: RequestSpec
    draft: QueryDraft
    check: SqlCheck
    preview: ExecPreview
    unconfirmed_terms: list[str] = Field(default_factory=list)


class BAReview(BaseModel):
    """ReviewInterrupt 에 대한 resume 값. BAReview.model_validate 로 검증."""
    draft_id: str
    reviewer: str
    decision: Literal["approve", "revise", "reject"]
    edited_sql: Optional[str] = None               # 있으면 author='ba' 초안으로 재검사
    failure_tags: list[FailureTag] = Field(default_factory=list)
    comment: str = ""
    pii_ack: bool = False                          # 대상자 목록이면 필수
    reviewed_at: datetime


# ---------------------------------------------------------------------------
# 5. 그래프 상태 — checkpointer 영속 저장 (Postgres 등), thread_id = request_id
#    리스트 필드의 노드는 '새 항목만' 반환한다 (operator.add 중복 방지)
# ---------------------------------------------------------------------------

class ChatState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]   # 마케터 대화. SQL 노드엔 미전달
    request: RequestSpec
    missing_fields: list[str]
    clarify_turns: int                                    # 상한 3
    awaiting: Literal["marketer", "ba", "none"]
    assigned_ba: Optional[str]

    query_drafts: Annotated[list[QueryDraft], operator.add]
    sql_checks: Annotated[list[SqlCheck], operator.add]
    preview: ExecPreview
    reviews: Annotated[list[BAReview], operator.add]      # v1 의 덮어쓰기 버그 수정
    final_sql: Optional[str]

    handoff_reason: Optional[HandoffReason]
    stage_history: Annotated[list[tuple[str, str]], operator.add]   # (stage, iso 시각)
    approved_at: Optional[datetime]


# ---------------------------------------------------------------------------
# 6. KPI·평가 — 종료 시 state 에서 계산 (별도 저장 모델은 두지 않음)
#    ① 최종 결과 정확도 : EvalCase 대비 final_sql 결과 일치
#    ② 초안 무수정 승인률: 첫 review 가 approve 이고 edited_sql 없음
#    ③ 리드타임        : approved_at - request.created_at
# ---------------------------------------------------------------------------

class EvalCase(BaseModel):
    """Gold Set 1건. 대시보드 패널·과거 티켓·BA 수정 이력에서 만든다."""
    case_id: str
    request_text: str
    expected_sql: str
    expected_result_summary: Optional[str] = None   # 예: 합계·건수 (원본 행 아님)
    source: Literal["dashboard", "past_ticket", "ba_revision", "synthetic"]
    tags: list[FailureTag] = Field(default_factory=list)   # 어떤 함정을 시험하는가
    criteria_version: str
