"""Jev 포트·경계 가드·그림자 운영 점검 (네트워크 차단, 키 불필요). 사용: .venv/bin/python tests/run_jev_checks.py"""
import json
import os
import shutil
import socket
import sys
import tempfile

socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("네트워크 차단"))
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
TMP = tempfile.mkdtemp(prefix="jevchk_")
os.environ["BOT_VAR_DIR"] = os.path.join(TMP, "var")
os.environ["BOT_JEV_CASSETTES"] = os.path.join(TMP, "cass")
os.environ.pop("TYPESAFE_API_KEY", None)

from bot import jev, nlu  # noqa: E402

jev.AUDIT_PATH = os.path.join(TMP, "var", "jev_audit.jsonl")
jev.ENV_FILE = os.path.join(TMP, "no.env.jev")           # 실제 키 파일을 읽지 않는다
jev.CASSETTE_DIR = os.environ["BOT_JEV_CASSETTES"]
ok = fail = 0


def check(name, cond):
    global ok, fail
    ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
    print(("PASS " if cond else "FAIL ") + name)


Q = "10월 공기청정기 구매 고객 푸시 대상자 뽑아 주세요. 최근 1년 구매자 제외, 금요일 발송이라 목요일까지 부탁해요"

# 1. off: 아무것도 부르지 않음
os.environ["BOT_JEV_MODE"] = "off"
_, p = nlu.extract(Q)
check("off 모드는 그림자 기록 없음", jev.shadow(Q, p) == {})

# 2. 가명처리·잔여 차단
s, n = jev.pseudonymize("담당 010-1234-5678, a.b@nuri.com, 회원 12345678 문의")
check("가명처리: 전화·이메일·회원번호 3건 치환", n == 3 and "010" not in s and "@" not in s and "12345678" not in s)
check("가명처리 뒤 잔여 개인정보 없음", jev.residual(s) == [])

# 3. 허용 호스트 밖이면 차단
cfg = {"base_url": "https://evil.example.com", "model": "m"}
try:
    jev.guard("t", "문장", cfg, "live", external=True)
    check("허용 밖 호스트 차단", False)
except jev.EgressBlocked:
    check("허용 밖 호스트 차단", True)

# 4. stub 그림자: 결정 불변, 기록 형식
os.environ["BOT_JEV_MODE"] = "stub"
act_before = nlu.decide(p)
sh = jev.shadow(Q, p)
check("stub 그림자 기록 생성", sh.get("source") == "stub" and sh["decisions"])
check("그림자 운영이 규칙 결정을 바꾸지 않음", nlu.decide(p) == act_before)
pts = {d["point"] for d in sh["decisions"]}
check("J1·J2·J3 판단이 모두 기록됨", {"J1_segment", "J2_intent", "J3_negation"} <= pts)
check("요약에 문장 원문 없음", "공기청정기" not in json.dumps(jev.summary(sh), ensure_ascii=False))

# 5. 감사 기록에 원문·키 없음
os.environ["TYPESAFE_API_KEY"] = "sk-test-SHOULD-NOT-APPEAR"
jev.shadow(Q, p)
audit = open(jev.AUDIT_PATH, encoding="utf-8").read()
check("감사 기록에 요청 원문 없음", "공기청정기" not in audit)
check("감사 기록에 키 없음", "SHOULD-NOT-APPEAR" not in audit)
os.environ.pop("TYPESAFE_API_KEY")

# 6. live 모드 키 없음 → 오류만 기록, 예외는 밖으로 나가지 않음 (fail-open)
os.environ["BOT_JEV_MODE"] = "live"
sh = jev.shadow(Q, p)
check("키 없는 live는 오류 기록 후 계속(fail-open)", "error" in sh and "키" in sh["error"])

# 7. replay: 녹화 없으면 stub 대체 표시, 녹화 있으면 재생
os.environ["BOT_JEV_MODE"] = "replay"
sh = jev.shadow(Q, p)
check("녹화 없을 때 replay_miss_stub로 표시", sh.get("source") == "replay_miss_stub")
segs = jev.segments(nlu.jira_focus(Q))
qs = jev._questions(segs, True)
safe, _ = jev.pseudonymize(nlu.jira_focus(Q))
key = jev._cassette_key(jev.settings()["model"], safe, qs)
fake = {"model": "jev-test", "input_tokens": 10,
        "answers": {"intent": {"type": "choice", "choice": "target_extraction", "p": 0.95,
                               "probabilities": {"target_extraction": 0.95}, "confidence": 0.9},
                    "negation": {"type": "choice", "choice": "exclude_buyers", "p": 0.9,
                                 "probabilities": {"exclude_buyers": 0.9}, "confidence": 0.8},
                    **{f"seg_{i}": {"type": "noul", "p": 0.05} for i in range(len(segs))}}}
jev.CassetteStore(jev.CASSETTE_DIR).put(key, fake)
sh = jev.shadow(Q, p)
check("녹화 재생", sh.get("source") == "replay")
d = {x["point"]: x for x in sh["decisions"]}
check("재생된 의도 판단이 규칙과 일치로 기록", d["J2_intent"]["agree"] is True)
segd = [x for x in sh["decisions"] if x["point"] == "J1_segment"]
check("조건을 담은 구절은 Jev가 낮게 봐도 남김(override)",
      any(x["override"] for x in segd) and all(x["would"] == "keep" for x in segd if x["rule_condition"]))
check("맥락 구절('금요일 발송이라 목요일까지')은 뺄 후보", any(x["would"] == "drop" for x in segd))
cass = open(os.path.join(jev.CASSETTE_DIR, key + ".json"), encoding="utf-8").read()
check("녹화본에 요청 원문 없음", "공기청정기" not in cass)

# 8. S3 R1·R2 (결정 반영) — 합성 판단으로 규칙만 확인
def _sh(label, pr, source="replay"):
    return {"source": source, "decisions": [{"point": "J2_intent", "answer": label, "p": pr}]}
po = {"request_type": "out_of_scope"}
pt = {"request_type": "target_extraction"}
pa = {"request_type": "analysis"}
check("R1: 범위 밖 이관 + Jev 대상자 0.99 → 의도 확인",
      jev.apply_rules(po, "handoff", "out_of_scope", _sh("target_extraction", 0.99)) == ("clarify", ["intent_confirm"], "R1"))
check("R1 미적용: Jev 확률 0.9 (기준 0.95 미만)",
      jev.apply_rules(po, "handoff", "out_of_scope", _sh("target_extraction", 0.9))[2] is None)
check("R1 미적용: 분석형은 규칙 우선",
      jev.apply_rules(pa, "handoff", "analysis", _sh("aggregate_report", 0.99))[2] is None)
check("R2: 규칙 대상자 초안 + Jev 집계 0.99 → 초안 대신 의도 확인",
      jev.apply_rules(pt, "draft", None, _sh("aggregate_report", 0.99)) == ("clarify", ["intent_confirm"], "R2"))
check("R2 미적용: 등급 같은 범위 이관은 그대로",
      jev.apply_rules(pt, "handoff", "grade", _sh("aggregate_report", 0.99))[2] is None)
check("R1·R2 미적용: stub 출처",
      jev.apply_rules(po, "handoff", "out_of_scope", _sh("target_extraction", 0.99, "stub"))[2] is None)
check("R1·R2 미적용: 오류 기록",
      jev.apply_rules(po, "handoff", "out_of_scope", {"error": "x"})[2] is None)
os.environ["BOT_JEV_APPLY"] = "0"
check("BOT_JEV_APPLY=0 이면 그림자만",
      jev.apply_rules(po, "handoff", "out_of_scope", _sh("target_extraction", 0.99))[2] is None)
os.environ.pop("BOT_JEV_APPLY")
check("의도 확인 답 해석: '대상자'·'집계'", nlu.intent_from_answer("대상자요") == "target_extraction"
      and nlu.intent_from_answer("숫자 집계") == "aggregate_report" and nlu.intent_from_answer("음") is None)
_, pf = nlu.extract("공청기 지난주 일별 주문 수 확인 부탁합니다. 금요일 푸시 전에 반응 보려구요", force_type="aggregate_report")
check("마케터가 답한 의도로 다시 해석(집계·주문 수·일별)",
      pf["request_type"] == "aggregate_report" and pf.get("metric") == "order_count" and pf.get("group_by") == "day")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{ok}/{ok + fail} 통과")
sys.exit(1 if fail else 0)
