"""로컬 테스트 화면 서버 (표준 라이브러리만 사용).

실행: draft_chatbot 폴더에서  .venv/bin/python -m bot.web   →  http://127.0.0.1:8765

고객 요청 반영
- C13 외부 반출 금지: 127.0.0.1 에만 열림. 외부 CDN·스크립트 없음. 결과 파일은 사내(로컬)에서만 내려받음
- C4 BA 승인 필수: 마케터 화면은 승인 전 결과를 받지 못함(서버가 막음). 대상자는 개인정보 확인 필수
- C12 룰 직접 확인: 규칙 파일 점검 결과와 규칙 파일 목록을 화면에서 확인 (수정은 CSV 그대로)
- 인증 없음(프로토타입): 같은 화면에 마케터·BA 영역이 함께 있음. 실제 도입 시 분리
"""
import io
import json
import os
import threading
from contextlib import redirect_stdout
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from langgraph.types import Command

from . import config, datastore, graph, ruleset, store

HOST, PORT = "127.0.0.1", int(os.environ.get("BOT_WEB_PORT", "8765"))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
APP = graph.build()
LOCK = threading.Lock()          # 그래프 실행은 한 번에 하나


def _cfg(rid):
    return {"configurable": {"thread_id": rid}}


def _sync(rid):
    st = APP.get_state(_cfg(rid)).values
    pend = graph.pending_interrupt(APP, rid)
    kind = pend["kind"] if pend else ("answered" if st.get("answer") else "handoff" if st.get("handoff") else "running")
    store.upsert(rid, status=kind, updated_at=datetime.now().isoformat(timespec="seconds"))
    if not pend:
        store.log_terminal(st)
    return st, pend


def marketer_view(rid):
    """마케터에게 보여 줄 것만. 승인 전에는 결과·SQL·행 수를 주지 않는다 (C4)."""
    st = APP.get_state(_cfg(rid)).values
    if not st:
        return {"error": "없는 요청 번호입니다."}
    pend = graph.pending_interrupt(APP, rid)
    out = {"request_id": rid, "request": st.get("raw_request"), "answers": st.get("answers", [])}
    if pend and pend["kind"] == "clarify":
        out.update(state="clarify", understood=pend["understood"], questions=pend["questions"])
    elif pend and pend["kind"] == "review":
        out.update(state="review", understood=pend["understood"])
    elif st.get("answer"):
        a = st["answer"]
        out.update(state="answered", summary=a["summary"], scope_notes=a["scope_notes"],
                   data_as_of=a["data_as_of"], approved_by=a["approved_by"], download=f"/api/result?rid={rid}")
    elif st.get("handoff"):
        out.update(state="handoff", message=st["handoff"]["message"], reason=st["handoff"]["reason"])
    else:
        out.update(state="running")
    return out


def ba_view(rid):
    pend = graph.pending_interrupt(APP, rid)
    if not pend or pend["kind"] != "review":
        return {"error": "이 요청은 지금 BA 검토를 기다리고 있지 않습니다."}
    out = dict(pend)
    out["notice"] = out.pop("error", None)     # 검토 안내(거절 사유 등). 'error'는 '검토 대기 아님'에만 쓴다
    pv = pend.get("preview")
    if pv:
        head, rows = datastore.read_result(pv["result_ref"], 10)
        out["preview_rows"] = {"head": head, "rows": rows}
        out["preview"] = {k: pv[k] for k in ("row_count", "columns", "truncated", "data_as_of")}
    return out


def api(method, path, q, body):
    if path == "/api/examples":
        return EXAMPLES
    if path == "/api/scenarios":
        return SCENARIOS
    if path == "/api/requests":
        ix = store.all_requests()
        return sorted(({"request_id": k, **{f: v.get(f) for f in ("request", "requester", "status", "created_at")}}
                       for k, v in ix.items()), key=lambda r: r["request_id"], reverse=True)
    if path == "/api/ask" and method == "POST":
        text = (body.get("text") or "").strip()
        if not text:
            return {"error": "요청 문장을 입력해 주세요."}
        with LOCK:
            rid = store.new_request_id()
            now = datetime.now().isoformat(timespec="seconds")
            store.upsert(rid, requester=body.get("user") or "marketer01", request=text, created_at=now)
            APP.invoke({"request_id": rid, "requester_id": body.get("user") or "marketer01",
                        "raw_request": text, "created_at": now}, _cfg(rid))
            _sync(rid)
        return marketer_view(rid)
    if path == "/api/answer" and method == "POST":
        rid = body.get("rid")
        with LOCK:
            pend = graph.pending_interrupt(APP, rid)
            if not pend or pend["kind"] != "clarify":
                return {"error": "이 요청은 지금 마케터의 답을 기다리고 있지 않습니다."}
            answers = {k: v for k, v in (body.get("answers") or {}).items() if str(v).strip()}
            APP.invoke(Command(resume={"answers": answers}), _cfg(rid))
            _sync(rid)
        return marketer_view(rid)
    if path == "/api/status":
        return marketer_view(q.get("rid", [""])[0])
    if path == "/api/queue":
        return [{"request_id": k, "request": v.get("request"), "requester": v.get("requester")}
                for k, v in store.all_requests().items() if v.get("status") == "review"]
    if path == "/api/review" and method == "GET":
        return ba_view(q.get("rid", [""])[0])
    if path == "/api/review" and method == "POST":
        rid = body.get("rid")
        with LOCK:
            pend = graph.pending_interrupt(APP, rid)
            if not pend or pend["kind"] != "review":
                return {"error": "이 요청은 지금 BA 검토를 기다리고 있지 않습니다."}
            payload = {"draft_id": body.get("draft_id"), "reviewer": (body.get("reviewer") or "").strip(),
                       "decision": body.get("decision"), "pii_ack": bool(body.get("pii_ack")),
                       "failure_tags": [t for t in body.get("failure_tags") or [] if t],
                       "comment": body.get("comment") or ""}
            if not payload["reviewer"]:
                return {"error": "검토자 이름을 입력해 주세요."}
            if body.get("edited_sql"):
                payload["edited_sql"] = body["edited_sql"]
            APP.invoke(Command(resume=payload), _cfg(rid))
            _sync(rid)
        v = ba_view(rid)
        return v if "error" not in v else {"done": True, "marketer": marketer_view(rid)}
    if path == "/api/rules":
        rep = ruleset.check_rules.Report()
        with redirect_stdout(io.StringIO()):
            ruleset.check_rules.validate(ruleset.check_rules.load_db(config.require_data_dir(), config.DDL_DIR), rep)
        rs = ruleset.load()
        return {"version": rs.version, "rows": [{"level": l, "area": a, "msg": m} for l, a, m in rep.rows],
                "terms": [{"id": t.term_id, "term": t.term, "status": t.status, "confirmed_by": t.confirmed_by,
                           "definition": t.definition} for t in rs.terms],
                "checks": [{"id": c.rule_id, "enabled": c.enabled, "severity": c.severity, "desc": c.description}
                           for c in rs.checks],
                "files": sorted(f for f in os.listdir(config.RULES_DIR) if f.endswith(".csv"))}
    if path == "/api/log":
        if not os.path.exists(config.LOG_PATH):
            return []
        return [json.loads(l) for l in open(config.LOG_PATH, encoding="utf-8")][-50:]
    return {"error": "없는 경로"}


EXAMPLES = [
    {"group": "바로 초안", "items": ["10월 카테고리별 매출 알려 주세요", "지난달 채널별 순매출", "10월 1일~20일 채널별 매출", "최근 2주 일별 주문 수"]},
    {"group": "대상자 초안", "items": ["최근 3개월 공기청정기 구매 고객 푸시 대상자", "최근 30일 김치냉장고 장바구니 담은 고객 대상자",
                                    "최근 3개월 세탁기 조회 고객, 최근 1년 구매자 제외 푸시 대상자"]},
    {"group": "줄임말·영어·Jira", "items": ["김냉 이번 달 어플 순매출", "세탁기 지난달 구매 고객 push target",
                                       "제목: 정수기 일별 매출 / 배경: 주간 보고용 / 요청 사항: 최근 30일 정수기 일별 순매출"]},
    {"group": "되묻기", "items": ["매출 알려줘", "공기청정기 푸시 대상자 뽑아줘", "공기청정기 안 산 고객 푸시 대상자",
                                "서울 거주 고객 중 지난달 냉장고 구매자 푸시 대상"]},
    {"group": "BA 이관", "items": ["10월 매출이 왜 떨어졌나요", "10월 쿠폰 사용률", "VIP 고객 푸시 대상자", "토너 잉크 지난달 순매출"]},
]


# 고객이 순서대로 따라 해 보는 시험 시나리오 (문구 · 할 일 · 확인할 점 · 관련 고객 요청)
SCENARIOS = [
    {"title": "매출 집계 — 대시보드와 같은 숫자인가", "text": "10월 카테고리별 매출 알려 주세요",
     "steps": "요청 보내기 → BA 검토에서 승인 → 마케터 카드에서 결과 확인",
     "check": "승인 전에는 마케터에게 'BA 확인 중'만 보이는지 · 결과에 순매출 기준(소모품·홈스타일 제외)과 데이터 기준 시각이 붙는지 · 숫자가 대시보드 카테고리별 매출과 같은지",
     "req": "C1 · C4 · C11"},
    {"title": "푸시 대상자 — 되묻기와 개인정보 확인", "text": "다음 주 공기청정기 프로모션 푸시 대상자 뽑아 주세요. 작년에 산 사람 빼고요.",
     "steps": "요청 → '어떤 고객인가요?'에 '최근 3개월 조회한 고객' 답변 → BA 검토에서 개인정보 확인 없이 승인 → 체크 후 다시 승인",
     "check": "'작년에 산 사람 빼고요'를 제외 조건으로 이해하는지 · 작년 구매 데이터가 없다는 경고가 BA에게 보이는지 · 개인정보 확인 없이는 승인이 거절되는지",
     "req": "C4 · C7"},
    {"title": "애매한 요청 — 추측하지 않고 되묻기", "text": "공기청정기 안 산 고객 푸시 대상자",
     "steps": "요청 보내기",
     "check": "'안 산'을 마음대로 해석하지 않고 제외 조건을 다시 묻는지",
     "req": "C4"},
    {"title": "범위 밖 요청 — BA에게 넘기기", "text": "10월 매출이 왜 떨어졌나요",
     "steps": "요청 보내기 (쿠폰 사용률·VIP 고객·소모품 매출도 같은 방식)",
     "check": "분석형 질문은 SQL을 만들지 않고 BA 이관 사유를 보여 주는지",
     "req": "C7"},
    {"title": "BA가 SQL을 고칠 때 — 보안 검사", "text": "10월 채널별 매출",
     "steps": "요청 → BA 검토에서 SQL을 SELECT * FROM TMP_KJH_0915 로 바꾸고 '수정 SQL로 재검사' → 원래 SQL로 되돌려 다시 재검사 → 승인",
     "check": "금지 테이블이 막히는지 · 고친 SQL도 다시 검사·미리 실행한 뒤 재승인해야 나가는지",
     "req": "C2 · C4"},
    {"title": "규칙 직접 고치기 — Claude 없이", "text": "에어프라이어 10월 매출",
     "steps": "요청 → '해석하지 못한 말' 되묻기 확인 → rules/vocab.csv 에 'category,에어프라이어,전자레인지·오븐,' 한 줄 추가·저장 → 규칙 점검 탭에서 점검 → 같은 문구 다시 요청",
     "check": "CSV 한 줄로 다음 요청부터 동작이 바뀌는지 · 점검 결과에 실패가 없는지",
     "req": "C11 · C12"},
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):        # 요청 문장이 콘솔 로그에 남지 않게
        pass

    def _send(self, code, data, ctype="application/json; charset=utf-8", extra=None):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _route(self, method):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, open(os.path.join(STATIC, "index.html"), "rb").read(), "text/html; charset=utf-8")
        if u.path == "/api/result":
            rid = parse_qs(u.query).get("rid", [""])[0]
            st = APP.get_state(_cfg(rid)).values
            if not st or not st.get("answer"):          # 승인 전에는 결과 파일을 주지 않는다 (C4)
                return self._send(403, {"error": "BA 승인 전에는 결과를 받을 수 없습니다."})
            data = open(st["answer"]["result_ref"], "rb").read()
            return self._send(200, data, "text/csv; charset=utf-8", {"Content-Disposition": f'attachment; filename="{rid}.csv"'})
        if not u.path.startswith("/api/"):
            return self._send(404, {"error": "없는 경로"})
        body = {}
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        try:
            self._send(200, api(method, u.path, parse_qs(u.query), body))
        except Exception as e:  # 화면에 원인을 보여 준다
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


def main():
    datastore.ensure_db()
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        if e.errno == 48 or "in use" in str(e):
            raise SystemExit(f"포트 {PORT}을(를) 이미 쓰고 있습니다. 화면이 이미 켜져 있다면 http://{HOST}:{PORT} 를 여세요.\n"
                             f"다른 포트로 켜려면: BOT_WEB_PORT=8766 .venv/bin/python -m bot.web")
        raise
    print(f"BA 요청 챗봇 테스트 화면: http://{HOST}:{PORT}  (이 PC 안에서만 열림, 종료 Ctrl+C)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
