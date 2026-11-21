"""BA 요청 처리 draft 챗봇 — 스키마 v3 (구조만, 로직 없음).

버전 이력 (고객이 자료를 줄 때마다 갱신)
- v1 (사전 QA 후)       : 요청서·SQL 초안·BA 검토의 뼈대          → schema_v1.py
- v2 (DDL·대시보드 후)  : 함정 검사, BA 70 : 마케터 30, KPI 필드     → schema_v2.py
- v3 (마스킹 데이터 후) : 아래 변경                                  → 이 파일

v3 변경 근거: 마스킹데이터_검증.md
1. 규칙의 원천을 rules/*.csv 로 옮김. 고객(BA)이 Claude 없이 직접 고친다.
   → 고정 bool 필드였던 SqlCheck 를 CheckRule(편집 가능) + CheckResult(결과 목록)로 교체.
   → RuleSet 버전과 변경 이력(RuleChange)을 둔다.
2. 회원 키 이원화는 '연결 불가'가 아니라 '암묵 변환 규칙'으로 판명 → TableMeta.id_transform.
3. 순매출 정의를 데이터로 재현(7,905/7,905칸) → BusinessTerm.status='reproduced' + evidence.
   순매출 범위(소모품·홈스타일 제외)를 답변에 반드시 표시 → MarketerAnswer.scope_notes.
4. 테이블마다 데이터 기준 시각이 다름(주문 D-1, 동의·발송 실시간) → data_as_of 를 테이블군별로.
5. 요청 유형별 필수 용어: 푸시 대상자 = T02(고객ID 변환) + T03(푸시 가능 고객).

보안 확인 전 가정(A1)은 유지: 실데이터는 LLM에 넣지 않는다. 마스킹 추출본도 외부 반출 금지.
"""

from __future__ import annotations

import operator
from datetime import date, datetime
from typing import Annotated, Literal, Optional, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

RequestType = Literal["target_extraction", "aggregate_report", "out_of_scope"]
HandoffReason = Literal["out_of_scope", "clarify_limit", "sql_fail", "rejected", "rule_invalid",
                        "revision_limit", "exec_error"]
Level = Literal["ok", "warn", "fail"]

FailureTag = Literal[
    "intent_misclassified", "requirement_misread", "missed_clarification",
    "wrong_table", "wrong_term_mapping",
    "id_system_mismatch",     # MBR_NO = CUST_ID 직접 조인 (데이터상 0건으로 조용히 실패)
    "code_join_duplicate",    # LX_ZCM_CD 채널 03 중복 (v3 신규)
    "wrong_period_filter", "fan_out_join", "missing_condition",
    "scope_mismatch",         # 순매출 범위(소모품·홈스타일 제외)를 모른 채 비교 (v3 신규)
    "pii_exposure", "other",
]


# ===========================================================================
# 1. 규칙 — rules/*.csv 를 읽은 모델. 챗봇은 읽기만, 고치는 사람은 BA
# ===========================================================================

class TableMeta(BaseModel):                      # ← tables.csv
    table: str
    domain: str
    allowed: bool
    id_system: Literal["MBR_NO", "CUST_ID", "none"] = "none"
    id_transform: Optional[str] = None           # v3: CUST_ID 계열이면 "'C' || MBR_NO"
    excluded_reason: Optional[str] = None
    is_history: bool = False
    refresh: Optional[str] = None                # '야간 배치(D-1)', '추출 시각까지'
    data_from: Optional[date] = None             # v3: 주문 계열 2026-05-01 이후
    note: str = ""


class BusinessTerm(BaseModel):                   # ← terms.csv
    term_id: str
    term: str
    definition: str
    sql: str
    source_tables: list[str]
    status: Literal["draft", "reproduced", "confirmed", "unresolved"]
    evidence: Optional[str] = None               # v3: 'NX_AGG1_DSL 7,905/7,905칸 일치'
    scope_note: Optional[str] = None             # v3: '소모품·홈스타일 제외, B2B 포함'
    confirmed_by: Optional[str] = None
    version: int = 1


class JoinPath(BaseModel):                       # ← joins.csv
    left: str
    right: str
    keys: list[list[str]]
    cardinality: Literal["1:1", "1:N", "N:1", "N:N"]
    verified_by: Literal["data", "dashboard", "ba", "unverified"] = "unverified"
    note: str = ""


class CheckRule(BaseModel):                      # ← checks.csv (v3: 고정 필드 대신 편집 가능한 규칙)
    rule_id: str
    enabled: bool
    severity: Literal["block", "warn"]
    description: str
    pattern: Optional[str] = None
    applies_to: Literal["all", "report", "push_target", "adhoc"] = "all"


class RuleSet(BaseModel):
    """챗봇이 한 요청을 처리하는 동안 고정해서 쓰는 규칙 묶음. 평가 재현의 기준."""
    version: str                                  # 예: '2026-11-21.3' 또는 파일 해시
    tables: list[TableMeta]
    terms: list[BusinessTerm]
    joins: list[JoinPath]
    checks: list[CheckRule]
    validate_result: Level                        # check_rules.py validate 결과
    loaded_at: datetime


class RuleChange(BaseModel):
    """BA가 CSV를 고칠 때마다 1건. 누가 무엇을 바꿨고 점검을 통과했는가."""
    changed_by: str
    file: Literal["tables.csv", "terms.csv", "joins.csv", "checks.csv"]
    target_id: str                                # table / term_id / rule_id
    summary: str
    before_version: str
    after_version: str
    validate_result: Level
    changed_at: datetime


# 요청 유형별로 반드시 써야 하는 용어 (데이터로 확인된 BA 관행)
REQUIRED_TERMS: dict[str, list[str]] = {
    "target_extraction": ["T02", "T03"],         # 고객ID 변환 + 푸시 가능 고객(동의·7일 2회)
    "aggregate_report": [],                      # 매출이면 T01 권장
}


# ===========================================================================
# 2. 요청서 — LLM 출력과 시스템 필드 분리 (v2 유지)
# ===========================================================================

class Condition(BaseModel):
    term: str
    raw_expr: str
    term_id: Optional[str] = None


class SpecExtraction(BaseModel):
    purpose: str
    request_type: RequestType
    conditions: list[Condition] = Field(default_factory=list)
    period_expr: Optional[str] = None
    metrics: list[str] = Field(default_factory=list)
    output_format: Literal["count_only", "list", "push_target_list", "unknown"] = "unknown"


class OpenQuestion(BaseModel):
    field: str
    question: str
    answer: Optional[str] = None
    asked_at: datetime
    answered_at: Optional[datetime] = None


class RequestSpec(BaseModel):
    request_id: str
    requester_id: str
    raw_request: str
    created_at: datetime
    due_date: Optional[date] = None
    extraction: SpecExtraction
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    jira_key: Optional[str] = None


# ===========================================================================
# 3. SQL 초안·검사·미리보기
# ===========================================================================

class QueryDraft(BaseModel):
    draft_id: str
    request_id: str
    attempt: int
    revision_round: int = 0
    sql: str
    tables_used: list[str]
    term_versions: dict[str, int] = Field(default_factory=dict)
    ruleset_version: str                          # v3: 어떤 규칙 버전으로 만들었나
    author: Literal["llm", "template", "ba"] = "template"   # offline 은 template
    assumptions: list[str] = Field(default_factory=list)
    prompt_version: str


class CheckResult(BaseModel):
    rule_id: str
    level: Level
    message: str


class SqlCheck(BaseModel):
    """v3: CheckRule 을 돌린 결과 목록. BA가 규칙을 추가하면 코드 수정 없이 반영된다."""
    draft_id: str
    ruleset_version: str
    results: list[CheckResult]
    missing_required_terms: list[str] = Field(default_factory=list)
    passed: bool                                  # fail 이 하나도 없음


class ExecPreview(BaseModel):
    draft_id: str
    row_count: int
    columns: list[str]
    data_as_of: dict[str, datetime]               # v3: {'주문': 11/19 24:00, '동의·발송': 11/20 18:00}
    result_ref: Optional[str] = None


# ===========================================================================
# 4. 사람 개입 (v2 유지) + 마케터에게 가는 답
# ===========================================================================

class ClarifyInterrupt(BaseModel):
    kind: Literal["clarify"] = "clarify"
    request_id: str
    questions: list[OpenQuestion]


class ClarifyResume(BaseModel):
    answers: dict[str, str]


class ReviewInterrupt(BaseModel):
    kind: Literal["review"] = "review"
    request_id: str
    spec: RequestSpec
    draft: QueryDraft
    check: SqlCheck
    preview: ExecPreview
    unconfirmed_terms: list[str] = Field(default_factory=list)


class BAReview(BaseModel):
    draft_id: str
    reviewer: str
    decision: Literal["approve", "revise", "reject"]
    edited_sql: Optional[str] = None
    failure_tags: list[FailureTag] = Field(default_factory=list)
    comment: str = ""
    pii_ack: bool = False
    rule_change_suggested: Optional[str] = None   # v3: "이 실수는 규칙으로 막자" → RuleChange 로 연결
    reviewed_at: datetime


class MarketerAnswer(BaseModel):
    """v3: BA 승인 후 마케터에게 가는 답. [마케터 가치] 숫자의 기준을 같이 보여 준다."""
    request_id: str
    summary: str                                   # '푸시 대상 N명' 같은 한 줄
    scope_notes: list[str] = Field(default_factory=list)   # '소모품·홈스타일 제외(대시보드와 동일)'
    data_as_of: dict[str, datetime]
    matches_dashboard: Optional[bool] = None
    approved_by: str
    delivered_at: datetime


# ===========================================================================
# 5. 그래프 상태
# ===========================================================================

class ChatState(TypedDict, total=False):
    """설계용 상태. 실제 그래프는 체크포인터 직렬화를 위해 dict 만 담는 bot/graph.py 의 GraphState 를 쓴다
    (검토 M4). 필드 의미는 같고, 경계에서 이 파일의 Pydantic 모델로 검증한다."""
    messages: Annotated[list[AnyMessage], add_messages]
    ruleset_version: str                                   # v3: 요청 처리 중 규칙 고정
    request: RequestSpec
    missing_fields: list[str]
    clarify_turns: int
    awaiting: Literal["marketer", "ba", "none"]
    assigned_ba: Optional[str]
    query_drafts: Annotated[list[QueryDraft], operator.add]
    sql_checks: Annotated[list[SqlCheck], operator.add]
    preview: ExecPreview
    reviews: Annotated[list[BAReview], operator.add]
    final_sql: Optional[str]
    answer: MarketerAnswer
    handoff_reason: Optional[HandoffReason]
    stage_history: Annotated[list[tuple[str, str]], operator.add]
    approved_at: Optional[datetime]


# ===========================================================================
# 6. 평가셋 — 고객 자료가 늘 때마다 커진다
# ===========================================================================

class EvalCase(BaseModel):
    case_id: str
    request_text: str
    expected_sql: str
    expected_result_summary: Optional[str] = None
    source: Literal["dashboard", "data_reproduction", "past_ticket", "ba_revision", "synthetic"]
    tags: list[FailureTag] = Field(default_factory=list)
    ruleset_version: str
    criteria_version: str
