"""요청 색인과 KPI 로그. 그래프 밖(CLI)에서만 쓴다 — interrupt 재실행으로 중복 기록되지 않도록."""
import json
import os
from datetime import datetime

from . import config


def _load():
    return json.load(open(config.INDEX_PATH, encoding="utf-8")) if os.path.exists(config.INDEX_PATH) else {}


def _save(ix):
    tmp = config.INDEX_PATH + ".tmp"
    json.dump(ix, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, config.INDEX_PATH)


def new_request_id():
    ix = _load()
    n = len(ix) + 1
    return f"REQ-{datetime.now():%m%d}-{n:03d}"


def upsert(request_id, **fields):
    ix = _load()
    ix.setdefault(request_id, {}).update(fields)
    _save(ix)


def all_requests():
    return _load()


def log_terminal(state: dict):
    """요청이 끝났을 때(답변 또는 이관) KPI 한 줄. 실데이터 행은 넣지 않는다."""
    ix = _load()
    rid = state["request_id"]
    if ix.get(rid, {}).get("logged"):
        return
    ended = state.get("approved_at") or datetime.now().isoformat(timespec="seconds")
    created = datetime.fromisoformat(state["created_at"])
    reviews = state.get("reviews", [])
    first = reviews[0] if reviews else None
    rec = {
        "request_id": rid, "requester_id": state.get("requester_id"),
        "request_type": (state.get("parsed") or {}).get("request_type"),
        "created_at": state["created_at"], "ended_at": ended,
        "lead_time_min": round((datetime.fromisoformat(ended) - created).total_seconds() / 60, 1),
        "outcome": "answered" if state.get("answer") else "handoff",
        "handoff_reason": (state.get("handoff") or {}).get("reason"),
        "clarify_turns": state.get("clarify_turns", 0),
        "drafts": len(state.get("drafts", [])),
        "revision_round": state.get("revision_round", 0),
        "check_findings": sorted({r["rule_id"] for c in state.get("checks", []) for r in c["results"]}),
        "first_review_approved_unedited": bool(first and first["decision"] == "approve" and not first.get("edited_sql")),
        "failure_tags": sorted({t for r in reviews for t in r.get("failure_tags", [])}),
        "row_count": (state.get("preview") or {}).get("row_count"),
        "ruleset_version": state.get("ruleset_version"),
        "rule_events": state.get("rule_events", []),
    }
    if state.get("jev_shadow"):                      # S2: 판단 요약만(문장 원문 없음)
        from . import jev
        rec["jev_shadow"] = [{**jev.summary(s), "rule_action": s.get("rule_action"), "applied": s.get("applied")}
                             for s in state["jev_shadow"]]
    with open(config.LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    upsert(rid, logged=True)
