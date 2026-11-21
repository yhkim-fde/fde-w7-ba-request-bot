"""Jev R1·R2 그래프 흐름 시험 (녹화본 재생, 네트워크 차단). 사용: .venv/bin/python tests/run_jev_flow.py

녹화본(tests/jev_cassettes/)에 있는 실제 Jev 판단으로 그래프를 돌린다.
- R1: 규칙은 범위 밖 이관 → 의도 확인 되묻기 → '대상자' 답 → 다시 의도를 묻지 않고 다음 단계
- R2: 규칙은 대상자로 오인 → 의도 확인 → '집계' 답 → 집계로 다시 해석 → BA 검토
- 녹화가 없는 문장: 규칙 결과 그대로
"""
import os
import shutil
import socket
import sys
import tempfile

socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("네트워크 차단"))
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="jevflow_")
shutil.copytree(os.path.join(BASE, "rules"), os.path.join(TMP, "rules"))
os.environ.update({"BOT_RULES_DIR": os.path.join(TMP, "rules"), "BOT_VAR_DIR": os.path.join(TMP, "var"),
                   "BOT_JEV_MODE": "replay"})
sys.path.insert(0, BASE)

from langgraph.types import Command  # noqa: E402

from bot import graph  # noqa: E402

app = graph.build(os.path.join(TMP, "ckpt.db"))
ok = fail = 0


def cfg(rid):
    return {"configurable": {"thread_id": rid}}


def start(rid, text):
    app.invoke({"request_id": rid, "requester_id": "mk", "raw_request": text, "created_at": "2026-11-21T09:00:00"}, cfg(rid))
    return graph.pending_interrupt(app, rid), app.get_state(cfg(rid)).values


def resume(rid, value):
    app.invoke(Command(resume=value), cfg(rid))
    return graph.pending_interrupt(app, rid), app.get_state(cfg(rid)).values


def check(name, cond, detail=""):
    global ok, fail
    ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def fields(p):
    return [q["field"] for q in (p or {}).get("questions", [])]


# R1 — 마케터 세트 #28
p, st = start("F1", "공청기 장바구니만 담고 미구매한 고객 최근 7일 부탁")
check("R1 이관 대신 의도 확인만 묻기", p and p["kind"] == "clarify" and fields(p) == ["intent_confirm"], fields(p))
check("R1 적용과 출처(replay) 기록", st["jev_shadow"][-1].get("applied") == "R1" and st["jev_shadow"][-1].get("source") == "replay")
check("R1 화면 요약은 '확인 중'", "확인" in (p or {}).get("understood", ""))
p, st = resume("F1", {"answers": {"intent_confirm": "대상자"}})
check("R1 답 후 다시 의도를 묻지 않음", p and "intent_confirm" not in fields(p), fields(p))
check("R1 답 후 대상자로 해석", st["parsed"]["request_type"] == "target_extraction")
check("R1 답 후 '미구매' 부정 표현은 규칙대로 되묻기", "negation" in fields(p), fields(p))

# R2 — 마케터 세트 #05
p, st = start("F2", "공청기 지난주 일별 주문 수 확인 부탁합니다. 금요일 푸시 전에 반응 보려구요")
check("R2 의도 확인 먼저", p and fields(p) == ["intent_confirm"], fields(p))
check("R2 적용 기록", st["jev_shadow"][-1].get("applied") == "R2")
p, st = resume("F2", {"answers": {"intent_confirm": "집계"}})
check("R2 답 후 집계·주문 수·일별로 해석", st["parsed"]["request_type"] == "aggregate_report"
      and st["parsed"].get("metric") == "order_count" and st["parsed"].get("group_by") == "day")
if p and p["kind"] == "clarify":                      # 남은 확인(해석 못 한 말 등)은 '진행'
    p, st = resume("F2", {"answers": {f: "진행" for f in fields(p)}})
check("R2 확인 후 BA 검토로", p and p["kind"] == "review", (p or {}).get("kind"))
check("BA 승인 전 마케터 답변 없음 (C4 유지)", not st.get("answer"))

# 녹화 없는 문장 — 규칙 그대로
p, st = start("F3", "10월 채널별 매출 알려 주세요")
check("녹화 없으면 규칙 결과 그대로(바로 BA 검토)", p and p["kind"] == "review")
check("녹화 없을 때 출처 표시·미적용", st["jev_shadow"][-1].get("source") == "replay_miss_stub"
      and st["jev_shadow"][-1].get("applied") is None)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{ok}/{ok + fail} 통과")
sys.exit(1 if fail else 0)
