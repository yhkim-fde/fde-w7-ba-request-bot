"""시험 세트 채점기 (offline 챗봇). 사용: .venv/bin/python tests/run_casefile.py tests/marketer_cases.json [--save 결과.txt]

- 판정: 초안/되묻기/이관이 기대와 같고, 초안이면 핵심 필드까지 같아야 일치
- 무표시 오답: 초안을 냈는데 기대가 초안이 아니거나 필드가 틀린 경우 (가장 위험)
- 첫 실행 결과를 그대로 저장한다. 이 세트를 보고 코드를 고치면 시험 세트로서의 의미가 사라진다.
"""
import argparse
import json
import os
import socket
import sys
from collections import Counter

socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("네트워크 차단"))
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from bot import datastore, jev, nlu, ruleset, sqlgen  # noqa: E402

TYPE = {"aggregate": "aggregate_report", "target": "target_extraction"}
APPLIED = []          # Jev R1·R2가 결정을 바꾼 문항 (BOT_JEV_MODE=replay 등에서만)


def judge(c, rs, cats_l):
    spec, p = nlu.extract(c["q"])
    act, info = nlu.decide(p)
    if jev.enabled():                                # 그래프 extract 노드와 같은 규칙 (첫 턴 기준)
        act2, info2, rule = jev.apply_rules(p, act, info, jev.shadow(c["q"], p))
        if rule:
            APPLIED.append((c["q"][:40], rule, act, act2))
        act, info = act2, info2
    if act == "draft":
        try:
            sqlgen.build(p, rs, cats_l)
        except ValueError:
            act, info = "handoff", "build"
    diffs = []
    if act == "draft" and c["expected"] == "draft":
        exp = {"type": TYPE.get(c.get("type")), "metric": c.get("metric"), "group_by": c.get("group_by"),
               "categories": sorted(c.get("categories") or []), "channels": sorted(c.get("channels") or []),
               "behavior": c.get("behavior"), "from": c.get("period_from"), "to": c.get("period_to"),
               "ex_from": c.get("exclude_from"), "ex_to": c.get("exclude_to")}
        ex = p.get("exclude_purchase") or {}
        got = {"type": p["request_type"], "metric": p.get("metric"), "group_by": p.get("group_by"),
               "categories": p["categories"], "channels": p["channels"], "behavior": p.get("behavior"),
               "from": (p.get("period") or {}).get("from"), "to": (p.get("period") or {}).get("to"),
               "ex_from": ex.get("from"), "ex_to": ex.get("to")}
        diffs = [f"{k}: 기대 {exp[k]} / 결과 {got[k]}" for k in exp if exp[k] != got[k]]
    good = act == c["expected"] and not diffs
    silent = act == "draft" and (c["expected"] != "draft" or bool(diffs))
    return act, info, diffs, good, silent, p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("casefile")
    ap.add_argument("--save")
    a = ap.parse_args()
    rs = ruleset.load()
    cats, _ = datastore.vocab()
    cats_l = {m: l for l, m, _s in cats}
    cases = json.load(open(a.casefile, encoding="utf-8"))
    lines, conf, by_style = [], Counter(), Counter()
    ok = silent_n = 0
    for i, c in enumerate(cases, 1):
        act, info, diffs, good, silent, p = judge(c, rs, cats_l)
        ok += good; silent_n += silent
        conf[(c["expected"], act)] += 1
        by_style[(c.get("style", "-"), good)] += 1
        q1 = c["q"].replace("\n", " / ")
        lines.append(f"[{'O' if good else 'X'}] #{i} ({c.get('style', '-')}) {q1[:110]}")
        if not good:
            why = f" 사유키={info}" if act != "draft" else ""
            lines.append(f"     기대 {c['expected']} / 결과 {act}{why} {'; '.join(diffs)}"
                         f"{'  ⚠ 무표시 오답' if silent else ''}\n     해석 못 한 말: {p.get('unparsed')} · 근거: {c.get('why', '')}")
    lines.append("")
    lines.append(f"일치 {ok}/{len(cases)} · 무표시 오답 {silent_n}건")
    lines.append("기대 → 결과 (행: 기대, 열: 결과)")
    for e in ("draft", "clarify", "handoff"):
        lines.append(f"  {e:8s}  " + "  ".join(f"{r}:{conf[(e, r)]}" for r in ("draft", "clarify", "handoff")))
    for st in sorted({s for s, _ in by_style}):
        lines.append(f"  문체 {st}: {by_style[(st, True)]}/{by_style[(st, True)] + by_style[(st, False)]}")
    if jev.enabled():
        lines.append(f"Jev 모드 {jev.settings()['mode']} · R1·R2가 결정을 바꾼 문항 {len(APPLIED)}건")
        lines += [f"  {r}: {b} → {a2} | {q.replace(chr(10), ' / ')}" for q, r, b, a2 in APPLIED]
    out = "\n".join(lines)
    print(out)
    if a.save:
        open(a.save, "w", encoding="utf-8").write(out + "\n")


if __name__ == "__main__":
    main()
