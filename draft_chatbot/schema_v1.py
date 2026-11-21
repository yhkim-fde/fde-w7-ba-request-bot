"""BA 요청 처리 draft 챗봇 — 스키마 초안 (구조만, 로직 없음).

범위: 요청 접수·명확화(①~③) → SQL 초안(④) → BA 검토(⑤, HITL)까지.
엑셀 정리·Jira 첨부·푸시 발송(⑥~⑧)은 draft 범위 밖.

보안 확인 전 가정:
- 외부 LLM에는 요청 문장 + 테이블/컬럼 메타데이터(DDL·업무 의미)만 보낸다.
- SQL 실행 결과(실데이터)는 사내 도구 안에만 두고 LLM에는 행 수·컬럼명 같은 요약만 준다.
- Jira 연동은 보안 확인 전이므로 RequestSpec 필드만 Jira 티켓과 맞춰 둔다.
"""

from __future__ import annotations

import operator
from datetime import date, datetime
from typing import Annotated, Literal, Optional, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 1. 공통 열거형
# ---------------------------------------------------------------------------

RequesterDept = Literal["마케팅", "영업기획", "상품기획", "CRM"]

# Jev Choice 로 분류. "out_of_scope"는 '해당 없음' 선택지 (닫힌 질문 원칙).
RequestType = Literal[
    "target_extraction",  # 푸시·캠페인 대상자 추출
    "aggregate_report",   # 매출·주문 집계
    "ad_hoc_lookup",      # 단건 확인성 조회
    "out_of_scope",       # 데이터 추출 요청이 아님 / 분류 불가 → BA 큐로
]

DownstreamUse = Literal["push_send", "report", "planning_reference", "unknown"]

Stage = Literal[
    "intake", "clarifying", "drafting", "validating",
    "awaiting_review", "approved", "rejected", "handed_to_ba",
]

# BA 검토 실패 사유 — Gold Set·평가 지표의 씨앗. 고칠 곳이 드러나게 명명.
FailureTag = Literal[
    "requirement_misread",   # 요구 해석 오류
    "wrong_table",           # 테이블 선택 오류
    "wrong_join",            # 조인 키·카디널리티 오류
    "missing_condition",     # 조건 누락
    "wrong_period",          # 기간·기준일 오류
    "wrong_term_mapping",    # 업무 용어 ↔ 컬럼 매핑 오류
    "pii_exposure",          # 개인정보 컬럼 과다 선택
    "other",
]


# ---------------------------------------------------------------------------
# 2. 요청서 (Jira 티켓에 대응) — '대강 쓴 요청'을 구조화한 결과
# ---------------------------------------------------------------------------

class Condition(BaseModel):
    """대상 조건 1개. term 은 마케터의 말, mapped_* 는 시맨틱 레이어로 확정."""
    term: str = Field(description="마케터가 쓴 표현 (예: '최근 구매 고객')")
    operator: Literal["=", "!=", ">", ">=", "<", "<=", "in", "between", "like", "exists"]
    value: Optional[str] = None
    mapped_term_id: Optional[str] = Field(None, description="BusinessTerm.term_id")
    confirmed: bool = Field(False, description="마케터가 되묻기에 답해 확정했는가")


class DateRange(BaseModel):
    start: Optional[date] = None
    end: Optional[date] = None
    basis: Optional[Literal["주문일", "결제일", "배송완료일", "가입일"]] = None


class OpenQuestion(BaseModel):
    """되묻기 1건. Jira 댓글 왕복을 대체."""
    field: str = Field(description="비어 있거나 모호한 RequestSpec 필드명")
    question: str
    answer: Optional[str] = None
    asked_at: Optional[datetime] = None
    answered_at: Optional[datetime] = None


class RequestSpec(BaseModel):
    """LLM structured output 대상. 완결성 판정은 코드가 한다."""
    request_id: str
    requester_dept: RequesterDept
    purpose: str = Field(description="왜 필요한가 (캠페인명·의사결정)")
    request_type: RequestType
    downstream_use: DownstreamUse = "unknown"
    population: list[Condition] = Field(default_factory=list)
    period: DateRange = Field(default_factory=DateRange)
    metrics: list[str] = Field(default_factory=list, description="집계 지표 (매출, 주문 수 등)")
    output_columns: list[str] = Field(default_factory=list)
    output_format: Literal["count_only", "excel_list", "push_target_list", "unknown"] = "unknown"
    due_date: Optional[date] = None
    open_questions: list[OpenQuestion] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 3. 데이터 카탈로그 / 시맨틱 레이어 — SDS 가 세팅한 낯선 테이블을 설명하는 '조각 지식'
#    (스킬 'Order Base Knowledge' 로 출발, BA 가 확정·버전 관리)
# ---------------------------------------------------------------------------

class ColumnMeta(BaseModel):
    name: str
    data_type: str
    business_meaning: str = ""
    is_pii: bool = False
    code_values: dict[str, str] = Field(default_factory=dict, description="코드 → 의미")


class TableMeta(BaseModel):
    table: str                       # schema.table
    business_description: str = ""
    grain: str = Field("", description="1행의 의미 (예: 주문상품 1건)")
    partition_column: Optional[str] = None
    allowed: bool = Field(False, description="화이트리스트 — 조회 허용 테이블만 True")
    columns: list[ColumnMeta] = Field(default_factory=list)


class JoinPath(BaseModel):
    left: str
    right: str
    keys: list[tuple[str, str]]
    cardinality: Literal["1:1", "1:N", "N:1", "N:M"]


class BusinessTerm(BaseModel):
    """업무 용어 → SQL 표현. 사람이 확정해야 쓰인다."""
    term_id: str
    term: str
    synonyms: list[str] = Field(default_factory=list)
    definition: str
    sql_expression: str
    tables: list[str]
    confirmed_by: Optional[str] = None   # BA 이름. None 이면 '미확정' — 초안에만 쓰고 표시
    version: int = 1
    updated_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# 4. SQL 초안 / 검증 / 미리보기 / BA 검토
# ---------------------------------------------------------------------------

class QueryDraft(BaseModel):
    draft_id: str
    request_id: str
    attempt: int = Field(description="1부터. 최대 2회 재시도 후 BA 수작업으로 넘김")
    sql: str
    tables_used: list[str]
    term_ids_used: list[str]
    assumptions: list[str] = Field(default_factory=list, description="BA 가 확인할 가정")
    model: str


class SqlCheck(BaseModel):
    """결정적 검사 — 코드로 판정 (LLM 아님)."""
    draft_id: str
    select_only: bool
    tables_whitelisted: bool
    pii_columns_selected: list[str] = Field(default_factory=list)
    has_period_filter: bool
    explain_ok: bool
    errors: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (self.select_only and self.tables_whitelisted
                and self.has_period_filter and self.explain_ok and not self.errors)


class ExecPreview(BaseModel):
    """사내 실행 결과 요약. 원본 행은 저장소 경로로만 가리키고 LLM 에 넣지 않는다."""
    draft_id: str
    row_count: int
    columns: list[str]
    null_ratio: dict[str, float] = Field(default_factory=dict)
    result_ref: Optional[str] = Field(None, description="사내 저장 위치 (BA 만 열람)")
    elapsed_ms: int


class BAReview(BaseModel):
    """동료 검토 단계를 BA 승인으로. 승인 없이 마케터에게 결과가 가지 않는다 (사전QA ④)."""
    draft_id: str
    reviewer: str
    decision: Literal["approve", "revise", "reject"]
    comment: str = ""
    edited_sql: Optional[str] = None
    failure_tags: list[FailureTag] = Field(default_factory=list)
    reviewed_at: datetime


# ---------------------------------------------------------------------------
# 5. LangGraph State — 워크플로우 순서는 고정(코드), 단계 안의 판단만 모델
# ---------------------------------------------------------------------------

class ChatState(TypedDict, total=False):
    # 프론트(마케터 대화). SQL 초안 노드에는 넘기지 않는다 — 컨텍스트 제어.
    messages: Annotated[list[AnyMessage], add_messages]

    # 접수·명확화
    request: RequestSpec
    intent_probs: dict[str, float]          # Jev Choice 분포 (임계값 튜닝용)
    missing_fields: list[str]               # 코드가 계산
    clarify_turns: int

    # 초안·검증
    candidate_tables: list[str]
    query_drafts: Annotated[list[QueryDraft], operator.add]
    sql_checks: Annotated[list[SqlCheck], operator.add]
    preview: ExecPreview

    # 검토
    review: BAReview
    stage: Stage


# ---------------------------------------------------------------------------
# 6. Trace 메타데이터 (Langfuse) — 다음 단계 평가용
# ---------------------------------------------------------------------------

class TraceMeta(BaseModel):
    request_id: str
    requester_dept: RequesterDept
    request_type: RequestType
    clarify_turns: int
    sql_attempts: int
    sql_check_passed: bool
    ba_decision: Optional[Literal["approve", "revise", "reject"]] = None
    failure_tags: list[FailureTag] = Field(default_factory=list)
    lead_time_min: Optional[int] = None    # 접수 → BA 승인 (KPI: 일이 밀리지 않는다)
