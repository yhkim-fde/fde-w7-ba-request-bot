"""평가셋 — "97%" 논의용. 결과는 반드시 n/N 으로만 보고한다 (검토 M3).

A. 결과 일치: 챗봇 초안 SQL 결과 vs 정답 SQL 결과 (BA 승인 전 '초안' 정확도)
   - 대시보드 패널 1~3: BA가 쓰던 쿼리가 정답 (같은 기준을 다른 경로로 계산하므로 동어반복 아님:
     챗봇은 원천 테이블로 T01 을 계산, 정답은 집계 테이블)
   - 나머지: FDE가 다른 방식으로 쓴 '임시 정답' (BA 확인 전)
B. 해석: 바꿔 쓴 표현의 처리 결과(초안/되묻기/이관 + 핵심 필드)가 기대와 같은가
C. 무표시 오답: 되묻기·이관 없이 초안을 냈는데 해석이 틀린 건수 (가장 위험한 실패)

실행: .venv/bin/python tests/evalset.py   (네트워크 차단)
"""
import glob
import os
import socket
import sys

socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("네트워크 차단"))
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from bot import config, datastore, nlu, ruleset, sqlgen  # noqa: E402

rs = ruleset.load()
cats, _ = datastore.vocab()
cats_l = {m: l for l, m, _s in cats}
allowed = ruleset.allowed_tables(rs)
dash = os.path.join(config.ROOT, "kit_download/dashboard/A-dashsql")


def dash_sql(prefix):
    return open(glob.glob(os.path.join(dash, prefix + "*.sql"))[0], encoding="utf-8").read()


def rows(sql, tag):
    r = datastore.run(sql, f"eval_{tag}", allowed)
    _, data = datastore.read_result(r["result_ref"], n=10 ** 7)
    return sorted(tuple(str(int(float(x))) if x.replace(".", "", 1).lstrip("-").isdigit() else x for x in row) for row in data)


PUSH_OK = """(SELECT COUNT(*) FROM NX_QPS1_SND q WHERE q.CUST_ID = a.CUST_ID AND q.SND_TS >= '2026-11-13 18:00:00') < 2"""
A_CASES = [
    ("A1", "10월 일별 순매출", dash_sql("패널1"), "대시보드 패널1"),
    ("A2", "10월 채널별 매출", dash_sql("패널2"), "대시보드 패널2"),
    ("A3", "10월 카테고리별 매출", dash_sql("패널3"), "대시보드 패널3"),
    ("A4", "10월 1일~20일 채널별 매출",
     "select c.CHNL_NM, sum(m.NET_AMT) from NX_AGG1_DSL m join LX_ZCM_CHNL c on m.CHNL_CD=c.CHNL_CD "
     "where m.DT between '20261001' and '20261020' group by 1", "FDE 임시 정답"),
    ("A5", "9월 김치냉장고 순매출",
     "select sum(m.NET_AMT) from NX_AGG1_DSL m join LX_KRP2_C k on m.CAT_CD=k.CAT_CD "
     "where k.CAT_M='김치냉장고' and m.DT between '20260901' and '20260930'", "FDE 임시 정답"),
    ("A6", "10월 공기청정기 주문 수",
     "select count(distinct h.ORD_NO) from LX_HJV1_H h where h.ORD_STAT <> '90' and h.ORD_DT between '20261001' and '20261031' "
     "and h.ORD_NO in (select d.ORD_NO from LX_HJV1_D d, LX_KRP3_SKU s, LX_KRP1_M m, LX_KRP2_C k "
     "where s.SKU_CD=d.SKU_CD and m.MDL_CD=s.MDL_CD and k.CAT_CD=m.CAT_CD and k.CAT_M='공기청정기')", "FDE 임시 정답"),
    ("A7", "최근 3개월 공기청정기 구매 고객 푸시 대상자",
     "select a.CUST_ID from NX_DNS5_AGR a where a.PUSH_YN='Y' and " + PUSH_OK + " and a.CUST_ID in ("
     "select 'C'||h.MBR_NO from LX_HJV1_H h, LX_HJV1_D d, LX_KRP3_SKU s, LX_KRP1_M m, LX_KRP2_C k "
     "where d.ORD_NO=h.ORD_NO and s.SKU_CD=d.SKU_CD and m.MDL_CD=s.MDL_CD and k.CAT_CD=m.CAT_CD "
     "and k.CAT_M='공기청정기' and h.ORD_STAT<>'90' and h.ORD_DT between '20260822' and '20261119')", "FDE 임시 정답"),
    ("A8", "최근 3개월 세탁기 조회한 고객 중 최근 1년 구매자 제외하고 푸시 대상자",
     "select a.CUST_ID from NX_DNS5_AGR a where a.PUSH_YN='Y' and " + PUSH_OK + " and a.CUST_ID in ("
     "select e.CUST_ID from NX_EVL2_PV v, NX_EVL1_SSN e, LX_KRP1_M m, LX_KRP2_C k where e.SSN_ID=v.SSN_ID and m.MDL_CD=v.MDL_CD "
     "and k.CAT_CD=m.CAT_CD and k.CAT_M='세탁기' and substr(v.PV_TS,1,10) between '2026-08-22' and '2026-11-19') "
     "and a.CUST_ID not in (select 'C'||h.MBR_NO from LX_HJV1_H h, LX_HJV1_D d, LX_KRP3_SKU s, LX_KRP1_M m, LX_KRP2_C k "
     "where d.ORD_NO=h.ORD_NO and s.SKU_CD=d.SKU_CD and m.MDL_CD=s.MDL_CD and k.CAT_CD=m.CAT_CD and k.CAT_M='세탁기' "
     "and h.ORD_STAT<>'90' and h.ORD_DT between '20251120' and '20261119')", "FDE 임시 정답"),
    ("A9", "8월 앱 채널 장바구니에 냉장고 담은 고객 푸시 대상자", None, "채널 조건은 대상자 템플릿에 없음 → 되묻기 기대"),
]

# (질문, 기대 행동, 기대 필드 검사)
B_CASES = [
    ("10월 카테고리별 매출", "draft", lambda p: p["metric"] == "net_sales" and p["group_by"] == "category"),
    ("지난달 채널별 순매출 알려줘", "draft", lambda p: p["period"]["from"] == "20261001" and p["group_by"] == "channel"),
    ("이번 달 세탁기 매출", "draft", lambda p: p["categories"] == ["세탁기"] and p["period"]["from"] == "20261101"),
    ("최근 2주 일별 주문 수", "draft", lambda p: p["metric"] == "order_count" and p["group_by"] == "day"),
    ("10월 앱 매출", "draft", lambda p: p["channels"] == ["앱"]),
    ("10월 모바일앱 매출", "draft", lambda p: p["channels"] == ["앱"]),
    ("공청기 9월 매출", "draft", lambda p: p["categories"] == ["공기청정기"]),
    ("매출 알려줘", "clarify", lambda p: True),
    ("10월 실적", "clarify", lambda p: True),
    ("10월 쿠폰 사용률", "handoff", lambda p: True),
    ("10월 매출이 왜 떨어졌나요", "handoff", lambda p: True),
    ("작년 대비 10월 매출", "handoff", lambda p: True),
    ("신규 고객 10월 매출", "handoff", lambda p: True),
    ("2025년 3월 매출", "handoff", lambda p: True),
    ("최근 3개월 공기청정기 구매 고객 푸시 대상자", "draft", lambda p: p["behavior"] == "purchase"),
    ("최근 한 달 김치냉장고 장바구니 담은 고객 대상자", "draft",                 # '한 달' 지원 추가 후 기대값 변경
     lambda p: p["behavior"] == "cart" and p["period"]["from"] == "20261021"),
    ("최근 30일 김치냉장고 장바구니 담은 고객 대상자", "draft", lambda p: p["behavior"] == "cart"),
    ("최근 1개월 정수기 찜한 고객 푸시 대상자", "draft", lambda p: p["behavior"] == "wish"),
    ("공기청정기 푸시 대상자 뽑아줘", "clarify", lambda p: True),
    ("공기청정기 안 산 고객 푸시 대상자", "clarify", lambda p: True),
    ("공기청정기 미구매 고객 푸시 대상자 최근 3개월", "clarify", lambda p: True),
    ("최근 3개월 세탁기 조회 고객, 최근 1년 구매자 제외 푸시 대상자", "draft",
     lambda p: p["behavior"] == "view" and p["exclude_purchase"]["from"] == "20251120"),
    ("VIP 고객 대상 푸시 대상자", "handoff", lambda p: True),
    ("부산 지역 최근 3개월 공기청정기 구매 고객 푸시 대상자", "clarify", lambda p: True),
    ("회의실 예약해줘", "handoff", lambda p: True),
    ("10월 소모품 매출", "handoff", lambda p: True),   # 소모품은 되묻기 전에 이관하도록 변경 (결과는 동일하게 이관)
]


def run_a():
    ok = n = 0
    out = []
    for cid, q, exp_sql, src in A_CASES:
        spec, p = nlu.extract(q)
        act, _ = nlu.decide(p)
        if exp_sql is None:
            good = act == "clarify" or bool(p["unparsed"])
            out.append((cid, q, "되묻기/표시" if good else f"무표시 초안({act})", good, src))
            n += 1; ok += good
            continue
        n += 1
        if act != "draft":
            out.append((cid, q, f"초안 못 냄({act})", False, src)); continue
        d = sqlgen.build(p, rs, cats_l)
        got, exp = rows(d["sql"], cid), rows(exp_sql, cid + "x")
        same = got == exp
        out.append((cid, q, f"일치 ({len(got)}행)" if same else f"불일치 (챗봇 {len(got)}행 / 정답 {len(exp)}행)", same, src))
        ok += same
    return ok, n, out


def run_b():
    ok = n = silent = 0
    out = []
    for q, exp_act, fcheck in B_CASES:
        spec, p = nlu.extract(q)
        act, info = nlu.decide(p)
        if exp_act == "draft_then_handoff":
            good = act == "draft"
            if good:
                try:
                    sqlgen.build(p, rs, cats_l); good = False
                except ValueError:
                    good = True
            got = "초안 단계에서 이관" if good else act
        else:
            good = act == exp_act and (act != "draft" or fcheck(p))
            got = act
        if act == "draft" and exp_act not in ("draft", "draft_then_handoff") or (act == "draft" and exp_act == "draft" and not fcheck(p)):
            silent += 1
        n += 1; ok += good
        out.append((q, exp_act, got, good))
    return ok, n, silent, out


if __name__ == "__main__":
    a_ok, a_n, a_out = run_a()
    print("A. 결과 일치 (초안 기준, BA 승인 전)")
    for cid, q, res, good, src in a_out:
        print(f"  [{'O' if good else 'X'}] {cid} {q} → {res} · 정답 출처: {src}")
    b_ok, b_n, silent, b_out = run_b()
    print("\nB. 해석 (바꿔 쓴 표현)")
    for q, exp, got, good in b_out:
        print(f"  [{'O' if good else 'X'}] {q} → 기대 {exp} / 결과 {got}")
    print(f"\n요약: A 결과 일치 {a_ok}/{a_n} · B 해석 {b_ok}/{b_n} · C 무표시 오답 {silent}건")
    print("※ 문항 수가 적어 비율(%)로 말하지 않는다. 'FDE 임시 정답'은 BA 확인 전.")
