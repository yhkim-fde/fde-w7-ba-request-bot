"""시나리오 테스트 — 외부 네트워크 차단 상태에서 실행 (검토 H5).

실행: .venv/bin/python tests/run_scenarios.py
- 임시 var·rules 사본을 써서 원본 규칙 파일과 기록을 건드리지 않는다.
"""
import os
import shutil
import socket
import sys
import tempfile

# 1) 네트워크 차단: 어떤 외부 연결 시도도 예외
def _blocked(*a, **k):
    raise RuntimeError("네트워크 호출 시도 감지 (차단됨)")
socket.socket.connect = _blocked
socket.create_connection = _blocked

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="botscn_")
shutil.copytree(os.path.join(BASE, "rules"), os.path.join(TMP, "rules"))
os.environ["BOT_RULES_DIR"] = os.path.join(TMP, "rules")
os.environ["BOT_VAR_DIR"] = os.path.join(TMP, "var")
sys.path.insert(0, BASE)

from langgraph.types import Command  # noqa: E402

from bot import graph, ruleset  # noqa: E402

app = graph.build(os.path.join(TMP, "ckpt.db"))
results = []


def cfg(rid):
    return {"configurable": {"thread_id": rid}}


def start(rid, text):
    app.invoke({"request_id": rid, "requester_id": "mk", "raw_request": text, "created_at": "2026-11-21T09:00:00"}, cfg(rid))
    return graph.pending_interrupt(app, rid), app.get_state(cfg(rid)).values


def resume(rid, value):
    app.invoke(Command(resume=value), cfg(rid))
    return graph.pending_interrupt(app, rid), app.get_state(cfg(rid)).values


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


# S1 집계: 바로 초안 → BA 승인 전에는 답변 없음 → 승인 후 답변
p, st = start("S1", "10월 채널별 매출 알려 주세요")
check("S1 집계 요청은 되묻기 없이 BA 검토로", p and p["kind"] == "review")
check("S1 승인 전 마케터 답변 없음 (C4)", not st.get("answer"))
p, st = resume("S1", {"draft_id": p["draft_id"], "reviewer": "ba01", "decision": "approve"})
check("S1 승인 후 답변·범위·기준 시각", st.get("answer") and st["answer"]["scope_notes"] and st["answer"]["data_as_of"])

# S2 대상자: 제외 조건 + 행동 누락 → 되묻기 → 개인정보 확인 없이 승인 시 거절 → 확인 후 승인
p, st = start("S2", "다음 주 공기청정기 프로모션 푸시 대상자 뽑아 주세요. 작년에 산 사람 빼고요.")
check("S2 행동 누락 되묻기", p and p["kind"] == "clarify" and any(q["field"] == "behavior" for q in p["questions"]))
check("S2 '작년 구매자 제외'를 제외 조건으로 해석 (H1)", st["parsed"].get("exclude_purchase", {}).get("from") == "20250101"
      and st["parsed"].get("behavior") is None)
p, st = resume("S2", {"answers": {"behavior": "최근 3개월 조회한 고객"}})
check("S2 답변 후 BA 검토", p and p["kind"] == "review", p and p.get("understood"))
check("S2 제외 기간이 데이터 이전이라는 경고", any("2026-05-01" in x for x in p["assumptions"]))
did = p["draft_id"]
p, st = resume("S2", {"draft_id": did, "reviewer": "ba01", "decision": "approve"})
check("S2 개인정보 확인 없으면 승인 거절", p and p["kind"] == "review" and "개인정보" in (p.get("error") or ""))
p, st = resume("S2", {"draft_id": did, "reviewer": "ba01", "decision": "approve", "pii_ack": True})
check("S2 확인 후 답변 (대상자 수만)", st.get("answer") and "명" in st["answer"]["summary"])

# S3 부정 표현 불명확 → 되묻기 (추측 금지)
p, st = start("S3", "공기청정기 안 산 고객 푸시 대상자")
check("S3 '안 산'은 되묻기 (H1)", p and p["kind"] == "clarify" and any(q["field"] == "negation" for q in p["questions"]))

# S4 분석형 질문 → 이관
p, st = start("S4", "10월 매출이 왜 떨어졌나요")
check("S4 분석형은 BA 이관", p is None and st.get("handoff", {}).get("reason") == "out_of_scope")

# S5 해석 못 한 말 → 확인 → '진행' 후 초안, 화면에 표시
p, st = start("S5", "부산 지역 최근 3개월 공기청정기 구매 고객 푸시 대상자")
check("S5 해석 못 한 말 되묻기 (H2)", p and p["kind"] == "clarify" and any(q["field"] == "unparsed" for q in p["questions"]))
p, st = resume("S5", {"answers": {"unparsed": "진행"}})
check("S5 진행 동의 후 BA 화면에 해석 못 한 말 표시", p and p["kind"] == "review" and "부산" in p["unparsed"])

# S6 BA 수정 SQL: 금지 테이블 → 검사 실패로 다시 검토 → 정상 SQL → 재검사·미리 실행 → 승인
d = p["draft_id"]
p, st = resume("S5", {"draft_id": d, "reviewer": "ba01", "decision": "revise",
                      "edited_sql": "SELECT 'C'||MBR_NO AS CUST_ID FROM TMP_KJH_0915", "failure_tags": ["wrong_table"]})
check("S6 금지 테이블 수정본은 다시 BA 검토 + 오류", p and p["kind"] == "review" and p["author"] == "ba" and not p["check"]["passed"])
good = st["drafts"][0]["sql"].replace("ORDER BY 1", "ORDER BY 1 LIMIT 100")
p, st = resume("S5", {"draft_id": p["draft_id"], "reviewer": "ba01", "decision": "revise", "edited_sql": good})
check("S6 정상 수정본은 재검사·미리 실행 후 재승인 대기", p and p["kind"] == "review" and p["check"]["passed"] and p["preview"]["row_count"] <= 100)
p, st = resume("S5", {"draft_id": p["draft_id"], "reviewer": "ba01", "decision": "approve", "pii_ack": True})
check("S6 재승인 후 답변", bool(st.get("answer")))

# S7 이전 초안 번호로 승인 시도 → 거절
p, st = start("S7", "10월 일별 순매출")
p2, st = resume("S7", {"draft_id": "S7-d99", "reviewer": "ba01", "decision": "approve"})
check("S7 다른 초안 번호 승인 거절", p2 and p2["kind"] == "review" and "바뀌었습니다" in (p2.get("error") or ""))

# S8 반려 → 이관
p, st = resume("S7", {"draft_id": p["draft_id"], "reviewer": "ba01", "decision": "reject", "comment": "기간 재확인", "failure_tags": ["wrong_period_filter"]})
check("S8 반려는 이관", st.get("handoff", {}).get("reason") == "rejected")

# S9 BA가 CSV를 고치면 동작이 바뀜 (C12): 채널 마스터 사용 금지 → 채널별 매출 초안이 R02로 막힘
tpath = os.path.join(os.environ["BOT_RULES_DIR"], "tables.csv")
orig = open(tpath, encoding="utf-8").read()
open(tpath, "w", encoding="utf-8").write(orig.replace("LX_ZCM_CHNL,공통,Y,", "LX_ZCM_CHNL,공통,N,"))
p, st = start("S9", "10월 채널별 매출")
check("S9 tables.csv 수정이 다음 요청에 반영", st.get("handoff", {}).get("reason") == "sql_fail"
      and any(r["rule_id"] == "R02" for r in st["check"]["results"]))
open(tpath, "w", encoding="utf-8").write(orig)

# S10 규칙 파일 형식 오류 → 처리 중단·이관
cpath = os.path.join(os.environ["BOT_RULES_DIR"], "checks.csv")
corig = open(cpath, encoding="utf-8").read()
open(cpath, "w", encoding="utf-8").write(corig.replace("R01,Y,block", "R01,Y,blok"))
p, st = start("S10", "10월 채널별 매출")
check("S10 규칙 오타는 rule_invalid 이관 (H3)", st.get("handoff", {}).get("reason") == "rule_invalid")
open(cpath, "w", encoding="utf-8").write(corig)

# S11 데이터 범위 밖·미래 기간
p, st = start("S11", "2025년 3월 매출")
check("S11 데이터 이전 기간은 이관", st.get("handoff", {}).get("reason") == "out_of_scope")
p, st = start("S12", "올해 12월 매출")
check("S12 미래 기간은 이관", st.get("handoff", {}).get("reason") == "out_of_scope")

passed = sum(ok for _, ok, _ in results)
print(f"\n시나리오 {passed}/{len(results)} 통과 · 네트워크 차단 상태에서 실행")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if passed == len(results) else 1)
