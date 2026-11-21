"""별도 시험 세트 — 코드를 보지 않은 에이전트가 사양만 보고 작성한 25문항 (tests/heldout_cases.json).
이 세트를 보고 코드를 고치면 더 이상 시험 세트가 아니다. 결과는 처음 실행한 그대로 기록한다."""
import json
import os
import socket
import sys

socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("네트워크 차단"))
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from bot import datastore, nlu, ruleset, sqlgen  # noqa: E402

rs = ruleset.load()
cats, _ = datastore.vocab()
cats_l = {m: l for l, m, _s in cats}
TYPE = {"aggregate": "aggregate_report", "target": "target_extraction"}

cases = json.load(open(os.path.join(BASE, "tests", "heldout_cases.json"), encoding="utf-8"))
ok = silent = 0
for c in cases:
    spec, p = nlu.extract(c["q"])
    act, info = nlu.decide(p)
    if act == "draft":
        try:
            sqlgen.build(p, rs, cats_l)
        except ValueError:
            act = "handoff"
    diffs = []
    if act == "draft" and c["expected"] == "draft":
        exp = {"type": TYPE.get(c.get("type")), "metric": c.get("metric"), "group_by": c.get("group_by"),
               "categories": sorted(c.get("categories") or []), "channels": sorted(c.get("channels") or []),
               "behavior": c.get("behavior"), "from": c.get("period_from"), "to": c.get("period_to")}
        got = {"type": p["request_type"], "metric": p.get("metric"), "group_by": p.get("group_by"),
               "categories": p["categories"], "channels": p["channels"], "behavior": p.get("behavior"),
               "from": (p.get("period") or {}).get("from"), "to": (p.get("period") or {}).get("to")}
        diffs = [f"{k}: 기대 {exp[k]} / 결과 {got[k]}" for k in exp if exp[k] != got[k]]
    good = act == c["expected"] and not diffs
    if act == "draft" and (c["expected"] != "draft" or diffs):
        silent += 1
    ok += good
    print(f"[{'O' if good else 'X'}] {c['q']}\n     기대 {c['expected']} / 결과 {act} {'; '.join(diffs)}" + ("" if good else f"  (근거: {c.get('why', '')})"))
print(f"\n시험 세트: {ok}/{len(cases)} 일치 · 무표시 오답(초안을 냈는데 틀림) {silent}건")
