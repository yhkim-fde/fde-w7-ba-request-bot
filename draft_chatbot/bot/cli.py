"""명령줄 도구 (프로토타입 화면 대신).

마케터: ask "요청" [--user ID] · answer REQ 필드=답 [필드=답 ...] · status REQ
BA    : queue · review REQ · review REQ --draft D --approve --reviewer 이름 [--pii-ack]
        review REQ --draft D --revise --edit-sql 파일 --reviewer 이름 [--tag 실패태그]
        review REQ --draft D --reject "사유" --reviewer 이름 [--tag 실패태그]
운영  : rules (규칙 점검) · log (KPI 로그 요약)

인증은 없다(프로토타입). 실제 도입 시 마케터/BA 권한 분리 필요.
"""
import argparse
import json
import os
import sys
from datetime import datetime

from langgraph.types import Command

from . import config, datastore, graph, store


def _cfg(rid):
    return {"configurable": {"thread_id": rid}}


def _after(app, rid):
    """invoke 뒤 상태를 색인에 반영하고, 끝났으면 KPI 로그 기록."""
    st = app.get_state(_cfg(rid)).values
    pend = graph.pending_interrupt(app, rid)
    kind = pend["kind"] if pend else ("answered" if st.get("answer") else "handoff" if st.get("handoff") else "running")
    store.upsert(rid, status=kind, updated_at=datetime.now().isoformat(timespec="seconds"))
    if not pend:
        store.log_terminal(st)
    return st, pend


def _print_marketer(st, pend):
    if pend and pend["kind"] == "clarify":
        print(f"이해한 내용: {pend['understood']}")
        print("확인이 필요합니다:")
        for q in pend["questions"]:
            print(f"  [{q['field']}] {q['question']}")
        print(f"→ 답하기: python -m bot answer {st['request_id']} 필드=답")
    elif pend and pend["kind"] == "review":
        print(f"이해한 내용: {pend['understood']}")
        print("BA가 확인 중입니다. 승인되면 결과를 받을 수 있습니다.")
    elif st.get("answer"):
        a = st["answer"]
        print(f"[BA 승인 완료 · {a['approved_by']}] {a['summary']}")
        for n in a["scope_notes"]:
            print(f"  · {n}")
        print("  · 데이터 기준: " + ", ".join(f"{k} {v[:16].replace('T', ' ')}" for k, v in a["data_as_of"].items()))
        print(f"  · 결과 파일(사내): {os.path.relpath(a['result_ref'], config.BASE)}")
    elif st.get("handoff"):
        print(f"BA가 직접 처리합니다. 사유: {st['handoff']['message']}")


def cmd_ask(app, a):
    rid = store.new_request_id()
    store.upsert(rid, requester=a.user, request=a.text, created_at=datetime.now().isoformat(timespec="seconds"))
    app.invoke({"request_id": rid, "requester_id": a.user, "raw_request": a.text,
                "created_at": datetime.now().isoformat(timespec="seconds")}, _cfg(rid))
    st, pend = _after(app, rid)
    print(f"요청 번호: {rid}")
    _print_marketer(st, pend)


def cmd_answer(app, a):
    pend = graph.pending_interrupt(app, a.rid)
    if not pend or pend["kind"] != "clarify":
        sys.exit("이 요청은 지금 마케터의 답을 기다리고 있지 않습니다.")
    answers = dict(kv.split("=", 1) for kv in a.pairs)
    app.invoke(Command(resume={"answers": answers}), _cfg(a.rid))
    st, pend = _after(app, a.rid)
    _print_marketer(st, pend)


def cmd_status(app, a):
    st = app.get_state(_cfg(a.rid)).values
    if not st:
        sys.exit("없는 요청 번호입니다.")
    _print_marketer(st, graph.pending_interrupt(app, a.rid))


def cmd_queue(app, a):
    rows = [(rid, r) for rid, r in store.all_requests().items() if r.get("status") == "review"]
    if not rows:
        print("BA 검토 대기 없음")
    for rid, r in rows:
        print(f"{rid}  요청자 {r.get('requester')}  {r.get('request')}")


def _show_review(pend):
    print(f"요청 {pend['request_id']} · 초안 {pend['draft_id']} ({'BA 수정본' if pend['author'] == 'ba' else '챗봇 초안'})")
    if pend.get("error"):
        print(f"!! {pend['error']}")
    print(f"이해한 내용: {pend['understood']}")
    if pend["unparsed"]:
        print(f"!! 해석하지 못한 말(마케터가 제외하고 진행 동의): {', '.join(pend['unparsed'])}")
    print("--- SQL ---\n" + pend["sql"] + "\n-----------")
    c = pend["check"]
    print(f"검사: {'통과' if c['passed'] else '실패'} · 규칙 버전 {c['ruleset_version']}")
    for r in c["results"]:
        print(f"  [{r['level']}] {r['rule_id']} {r['message']}")
    if c["missing_required_terms"]:
        print(f"  [fail] 필수 용어 누락: {c['missing_required_terms']}")
    if pend["unconfirmed_terms"]:
        print(f"주의: BA 미확정 용어 사용 {pend['unconfirmed_terms']} (rules/terms.csv confirmed_by)")
    for n in pend["scope_notes"] + pend["assumptions"]:
        print(f"  · {n}")
    pv = pend.get("preview")
    if pv:
        print(f"미리 실행: {pv['row_count']}행{' (상한에서 잘림)' if pv['truncated'] else ''} · 결과 파일 {os.path.relpath(pv['result_ref'], config.BASE)}")
        head, rows = datastore.read_result(pv["result_ref"])
        print("  " + " | ".join(head))
        for r in rows:
            print("  " + " | ".join(r))
    if pend["is_push_target"]:
        print("대상자 목록입니다. 승인 시 --pii-ack 필요.")


def cmd_review(app, a):
    pend = graph.pending_interrupt(app, a.rid)
    if not pend or pend["kind"] != "review":
        sys.exit("이 요청은 지금 BA 검토를 기다리고 있지 않습니다.")
    decision = "approve" if a.approve else "revise" if a.revise else "reject" if a.reject is not None else None
    if not decision:
        _show_review(pend)
        return
    if not a.draft or not a.reviewer:
        sys.exit("결정에는 --draft(초안 번호)와 --reviewer 가 필요합니다.")
    payload = {"draft_id": a.draft, "reviewer": a.reviewer, "decision": decision, "pii_ack": a.pii_ack,
               "failure_tags": a.tag or [], "comment": a.reject or a.comment or ""}
    if a.edit_sql:
        payload["edited_sql"] = open(a.edit_sql, encoding="utf-8").read()
    app.invoke(Command(resume=payload), _cfg(a.rid))
    st, pend2 = _after(app, a.rid)
    if pend2 and pend2["kind"] == "review":
        _show_review(pend2)
    else:
        _print_marketer(st, pend2)


def cmd_rules(app, a):
    os.system(f'"{sys.executable}" "{os.path.join(config.BASE, "rules", "check_rules.py")}" validate')


def cmd_log(app, a):
    if not os.path.exists(config.LOG_PATH):
        sys.exit("기록 없음")
    recs = [json.loads(l) for l in open(config.LOG_PATH, encoding="utf-8")]
    ans = [r for r in recs if r["outcome"] == "answered"]
    print(f"종료된 요청 {len(recs)}건 · 답변 {len(ans)} · 이관 {len(recs) - len(ans)}")
    if ans:
        unedited = sum(r["first_review_approved_unedited"] for r in ans)
        print(f"초안 무수정 승인 {unedited}/{len(ans)} · 평균 되묻기 {sum(r['clarify_turns'] for r in recs) / len(recs):.1f}회")
    print("※ 프로토타입 데모 기록. 실제 리드타임·승인률은 BA가 쓰기 시작한 뒤에만 의미가 있음")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m bot")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ask"); s.add_argument("text"); s.add_argument("--user", default="marketer01")
    s = sub.add_parser("answer"); s.add_argument("rid"); s.add_argument("pairs", nargs="+")
    s = sub.add_parser("status"); s.add_argument("rid")
    sub.add_parser("queue")
    s = sub.add_parser("review"); s.add_argument("rid"); s.add_argument("--draft"); s.add_argument("--reviewer")
    s.add_argument("--approve", action="store_true"); s.add_argument("--revise", action="store_true")
    s.add_argument("--reject"); s.add_argument("--edit-sql"); s.add_argument("--comment")
    s.add_argument("--pii-ack", action="store_true"); s.add_argument("--tag", action="append")
    sub.add_parser("rules"); sub.add_parser("log")
    a = p.parse_args(argv)
    app = graph.build()
    {"ask": cmd_ask, "answer": cmd_answer, "status": cmd_status, "queue": cmd_queue, "review": cmd_review,
     "rules": cmd_rules, "log": cmd_log}[a.cmd](app, a)


if __name__ == "__main__":
    main()
