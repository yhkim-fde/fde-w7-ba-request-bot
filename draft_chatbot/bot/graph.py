"""LangGraph 그래프: 접수 → 해석 → (되묻기) → 초안 → 검사 → 미리 실행 → BA 승인 → 답변 / 이관.

- 사람 개입 2곳(interrupt): 마케터 되묻기, BA 승인. 재개 시 노드가 처음부터 다시 실행되므로
  interrupt 앞에는 부작용 없는 코드만 둔다. KPI 로그는 CLI가 그래프 밖에서 기록한다.
- 상태는 JSON(dict)만 (GraphState). Pydantic 검증은 경계(재개 값, 초안, 답변)에서.
- 규칙 버전: 노드마다 현재 CSV 버전과 비교해 바뀌었으면 다시 고정하고 이력에 남긴다(검토 H3).
- BA 승인 없이는 answer 노드에 도달할 수 없다(고객 요청 C4). 승인은 인증 없는 관례 수준(프로토타입).
"""
import operator
import re
import sqlite3
from datetime import datetime
from typing import Annotated, Literal, Optional, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from schema import REQUIRED_TERMS, BAReview, ClarifyResume, MarketerAnswer, QueryDraft

from . import config, datastore, jev, nlu, ruleset, sqlgen


class GraphState(TypedDict, total=False):
    request_id: str
    requester_id: str
    raw_request: str
    created_at: str
    answers: Annotated[list, operator.add]          # [{field, question, answer}]
    clarify_turns: int
    unparsed_ack: bool
    ruleset_version: str
    rule_events: Annotated[list, operator.add]
    spec: dict
    parsed: dict
    pending_questions: list
    drafts: Annotated[list, operator.add]
    draft: dict
    checks: Annotated[list, operator.add]
    check: dict
    preview: Optional[dict]
    review_error: Optional[str]
    reviews: Annotated[list, operator.add]
    revision_round: int
    awaiting: str                                   # marketer | ba | none
    final_sql: Optional[str]
    answer: Optional[dict]
    handoff: Optional[dict]
    approved_at: Optional[str]
    stage_history: Annotated[list, operator.add]
    jev_shadow: Annotated[list, operator.add]       # S2 그림자 판단 기록 (결정에는 쓰지 않음)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _stage(name):
    return [[name, _now()]]


def _pin(state):
    """현재 규칙을 읽고, 고정 버전과 다르면 다시 고정. → (RuleSet, update dict)"""
    rs = ruleset.load()
    upd = {}
    if state.get("ruleset_version") and state["ruleset_version"] != rs.version:
        upd["rule_events"] = [f"{_now()} 규칙 버전 변경 {state['ruleset_version']} → {rs.version}, 다시 고정"]
    upd["ruleset_version"] = rs.version
    return rs, upd


def _handoff(key_or_reason, message, extra=None):
    upd = {"handoff": {"reason": key_or_reason, "message": message}, "awaiting": "ba",
           "stage_history": _stage("handoff")}
    upd.update(extra or {})
    return Command(goto="handoff", update=upd)


# ---------------------------------------------------------------- nodes

def intake(state: GraphState) -> Command[Literal["extract", "handoff"]]:
    try:
        rs = ruleset.load()
    except ruleset.RuleLoadError as e:
        return _handoff("rule_invalid", str(e))
    if rs.validate_result == "fail":
        return _handoff("rule_invalid", "규칙 파일 점검(check_rules.py validate)에 실패한 버전입니다. BA가 규칙을 고친 뒤 다시 요청해 주세요.")
    return Command(goto="extract", update={"ruleset_version": rs.version, "clarify_turns": 0, "revision_round": 0,
                                           "unparsed_ack": False, "awaiting": "none", "stage_history": _stage("intake")})


def extract(state: GraphState) -> Command[Literal["clarify", "draft", "handoff"]]:
    text = state["raw_request"] + " " + " ".join(a["answer"] for a in state.get("answers", []))
    intent_ans = next((a["answer"] for a in reversed(state.get("answers", [])) if a["field"] == "intent_confirm"), None)
    spec, p = nlu.extract(text, force_type=nlu.intent_from_answer(intent_ans) if intent_ans else None)
    act, info = nlu.decide(p, state.get("unparsed_ack", False))
    base = {"spec": spec.model_dump(), "parsed": p, "stage_history": _stage("extract")}
    if jev.enabled():
        sh = jev.shadow(text, p)
        rule_act, applied = act, None
        if intent_ans is None:                       # 마케터가 의도를 답했으면 그 답이 우선 (반복 방지)
            act, info, applied = jev.apply_rules(p, act, info, sh)   # S3: R1·R2 (되묻기 쪽으로만)
        base["jev_shadow"] = [{**sh, "rule_action": rule_act, "applied": applied, "at": _now()}]
    if act == "handoff":
        reason, msg = nlu.HANDOFF[info]
        return _handoff(reason, msg, base)
    if act == "clarify":
        qs = [{"field": f, "question": nlu.QUESTIONS[f].format(words=", ".join(p["unparsed"]), cats=", ".join(p["categories"]))}
              for f in info]
        return Command(goto="clarify", update={**base, "pending_questions": qs, "awaiting": "marketer"})
    return Command(goto="draft", update={**base, "pending_questions": []})


def clarify(state: GraphState) -> Command[Literal["extract", "handoff"]]:
    if state.get("clarify_turns", 0) >= config.MAX_CLARIFY:
        return _handoff("clarify_limit", f"되묻기 {config.MAX_CLARIFY}회로도 요청이 확정되지 않아 BA가 직접 확인합니다.")
    resume = interrupt({"kind": "clarify", "request_id": state["request_id"],
                        "questions": state["pending_questions"], "understood": nlu.understood(state["parsed"])})
    try:
        ans = ClarifyResume.model_validate(resume).answers
    except ValidationError:
        ans = {}
    now = _now()
    asked = {q["field"]: q["question"] for q in state["pending_questions"]}
    CONFIRM = ("unparsed", "categories_confirm")          # '진행'으로 확인하는 질문
    new = [{"field": f, "question": asked.get(f, ""), "answer": a, "answered_at": now} for f, a in ans.items() if f not in CONFIRM]
    ack = state.get("unparsed_ack", False)
    for f in CONFIRM:
        if f not in ans:
            continue
        if re.fullmatch(r"\s*(진행|예|네|그대로|빼고 진행|ok|OK)\s*[.!]?\s*", ans[f]):
            ack = True
        else:
            new.append({"field": f, "question": asked.get(f, ""), "answer": ans[f], "answered_at": now})
    return Command(goto="extract", update={"answers": new, "clarify_turns": state.get("clarify_turns", 0) + 1,
                                           "unparsed_ack": ack, "awaiting": "none", "stage_history": _stage("clarify")})


def draft(state: GraphState) -> Command[Literal["check", "handoff"]]:
    rs, upd = _pin(state)
    cats, _ = datastore.vocab()
    try:
        d = sqlgen.build(state["parsed"], rs, {m: l for l, m, _s in cats})
    except ValueError as e:
        reason, msg = nlu.HANDOFF.get(str(e), ("out_of_scope", str(e)))
        return _handoff(reason, msg, upd)
    n = len(state.get("drafts", [])) + 1
    qd = QueryDraft(draft_id=f"{state['request_id']}-d{n}", request_id=state["request_id"], attempt=1,
                    revision_round=state.get("revision_round", 0), sql=d["sql"],
                    tables_used=sorted(set(re.findall(r"\b([A-Z]{2,3}_[A-Z0-9_]+)\b", d["sql"]))),
                    term_versions=d["term_versions"], ruleset_version=rs.version, author="template",
                    assumptions=d["assumptions"], prompt_version=config.PROMPT_VERSION)
    dd = {**qd.model_dump(), "qtype": d["qtype"], "scope_notes": d["scope_notes"]}
    return Command(goto="check", update={**upd, "draft": dd, "drafts": [dd], "stage_history": _stage("draft")})


def check(state: GraphState) -> Command[Literal["preview", "ba_review", "handoff"]]:
    rs, upd = _pin(state)
    d = state["draft"]
    results = [{"rule_id": rid, "level": lvl.lower(), "message": msg} for rid, lvl, msg in ruleset.run_checks(rs, d["sql"], d["qtype"])]
    try:
        datastore.static_guard(d["sql"])
    except datastore.GuardError as e:
        results.append({"rule_id": "GUARD", "level": "fail", "message": str(e)})
    req = REQUIRED_TERMS.get(state["parsed"]["request_type"], [])
    missing = [t for t in req if t not in d["term_versions"]]
    passed = not missing and all(r["level"] != "fail" for r in results)
    chk = {"draft_id": d["draft_id"], "ruleset_version": rs.version, "results": results,
           "missing_required_terms": missing, "passed": passed}
    upd.update({"check": chk, "checks": [chk], "stage_history": _stage("check")})
    if passed:
        return Command(goto="preview", update=upd)
    if d["author"] == "ba":
        return Command(goto="ba_review", update={**upd, "preview": None,
                                                 "review_error": "수정한 SQL이 검사를 통과하지 못했습니다. 다시 고쳐 주세요."})
    return _handoff("sql_fail", "자동 검사를 통과하는 초안을 만들지 못해 BA가 직접 작성합니다.", upd)


def preview(state: GraphState) -> Command[Literal["ba_review", "handoff"]]:
    rs, upd = _pin(state)
    d = state["draft"]
    try:
        res = datastore.run(d["sql"], d["draft_id"], ruleset.allowed_tables(rs))
    except datastore.GuardError as e:
        if d["author"] == "ba":
            return Command(goto="ba_review", update={**upd, "preview": None, "review_error": str(e)})
        return _handoff("exec_error", f"초안 실행에 실패해 BA가 직접 확인합니다: {e}", upd)
    res["data_as_of"] = {k: v.isoformat() for k, v in config.DATA_AS_OF.items()}
    return Command(goto="ba_review", update={**upd, "preview": res, "review_error": None,
                                             "awaiting": "ba", "stage_history": _stage("preview")})


def ba_review(state: GraphState) -> Command[Literal["answer", "check", "handoff", "ba_review"]]:
    d, chk, pv = state["draft"], state["check"], state.get("preview")
    rs = ruleset.load()
    unconfirmed = [t for t in d["term_versions"] if ruleset.term(rs, t).confirmed_by is None]
    resume = interrupt({
        "kind": "review", "request_id": state["request_id"], "draft_id": d["draft_id"], "author": d["author"],
        "understood": nlu.understood(state["parsed"]), "unparsed": state["parsed"].get("unparsed", []),
        "sql": d["sql"], "check": chk, "preview": pv, "assumptions": d["assumptions"], "scope_notes": d["scope_notes"],
        "unconfirmed_terms": unconfirmed, "error": state.get("review_error"),
        "is_push_target": d["qtype"] == "push_target"})
    resume = dict(resume or {})
    if resume.get("draft_id") != d["draft_id"]:
        return Command(goto="ba_review", update={"review_error": f"검토 대상이 바뀌었습니다. 현재 초안은 {d['draft_id']} 입니다."})
    try:
        rv = BAReview.model_validate({**resume, "reviewed_at": _now()})
    except ValidationError as e:
        return Command(goto="ba_review", update={"review_error": f"검토 입력 오류: {e.errors()[0]['msg']}"})
    rvd = rv.model_dump(mode="json")
    if rv.decision == "approve":
        if not chk["passed"] or not pv:
            return Command(goto="ba_review", update={"review_error": "검사 통과와 미리 실행이 끝난 초안만 승인할 수 있습니다."})
        if d["qtype"] == "push_target" and not rv.pii_ack:
            return Command(goto="ba_review", update={"review_error": "대상자 목록은 개인정보 확인(화면의 체크 또는 --pii-ack)이 필요합니다."})
        return Command(goto="answer", update={"reviews": [rvd], "approved_at": _now(), "final_sql": d["sql"],
                                              "review_error": None, "stage_history": _stage("approved")})
    if rv.decision == "reject":
        return _handoff("rejected", f"BA가 반려했습니다: {rv.comment or '사유 미기재'}", {"reviews": [rvd]})
    # revise — offline 에서는 수정 SQL 필수 (검토 H6)
    if not rv.edited_sql:
        return Command(goto="ba_review", update={"review_error": "offline 모드의 수정 요청은 고친 SQL이 필요합니다."})
    rnd = state.get("revision_round", 0) + 1
    if rnd > config.MAX_REVISIONS:
        return _handoff("revision_limit", "수정이 반복되어 BA가 직접 마무리합니다.", {"reviews": [rvd]})
    n = len(state.get("drafts", [])) + 1
    nd = {**d, "draft_id": f"{state['request_id']}-d{n}", "sql": rv.edited_sql.strip(), "author": "ba",
          "revision_round": rnd, "tables_used": sorted(set(re.findall(r"\b([A-Z]{2,3}_[A-Z0-9_]+)\b", rv.edited_sql)))}
    return Command(goto="check", update={"reviews": [rvd], "revision_round": rnd, "draft": nd, "drafts": [nd],
                                         "review_error": None, "stage_history": _stage("revise")})


def answer(state: GraphState):
    d, pv, rv = state["draft"], state["preview"], state["reviews"][-1]
    summary = (f"푸시 대상 {pv['row_count']}명" if d["qtype"] == "push_target" else f"결과 {pv['row_count']}행")
    ans = MarketerAnswer(request_id=state["request_id"], summary=summary,
                         scope_notes=d["scope_notes"] + d["assumptions"],
                         data_as_of={k: datetime.fromisoformat(v) for k, v in pv["data_as_of"].items()},
                         matches_dashboard=True if "T01" in d["term_versions"] else None,
                         approved_by=rv["reviewer"], delivered_at=datetime.now())
    return {"answer": {**ans.model_dump(mode="json"), "result_ref": pv["result_ref"]},
            "awaiting": "none", "stage_history": _stage("answer")}


def handoff(state: GraphState):
    return {}


def build(checkpoint_path: str = None):
    g = StateGraph(GraphState)
    for name, fn in [("intake", intake), ("extract", extract), ("clarify", clarify), ("draft", draft),
                     ("check", check), ("preview", preview), ("ba_review", ba_review), ("answer", answer),
                     ("handoff", handoff)]:
        g.add_node(name, fn)
    g.add_edge(START, "intake")
    g.add_edge("answer", END)
    g.add_edge("handoff", END)
    con = sqlite3.connect(checkpoint_path or config.CHECKPOINT_DB, check_same_thread=False)
    return g.compile(checkpointer=SqliteSaver(con))


def pending_interrupt(app, request_id):
    """현재 대기 중인 interrupt 값 (없으면 None)."""
    st = app.get_state({"configurable": {"thread_id": request_id}})
    for t in st.tasks:
        for i in t.interrupts:
            return i.value
    return None
