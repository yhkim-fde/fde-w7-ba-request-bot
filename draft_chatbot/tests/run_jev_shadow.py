"""Jev 그림자 비교 (S2). 규칙 해석은 그대로 두고, 같은 문장에 대한 Jev 판단을 옆에 놓고 센다.

사용:
  BOT_JEV_MODE=stub   .venv/bin/python tests/run_jev_shadow.py tests/marketer_cases.json   # 배선 확인(성능 아님)
  BOT_JEV_MODE=record .venv/bin/python tests/run_jev_shadow.py tests/marketer_cases.json   # 키 필요, 녹화
  BOT_JEV_MODE=replay .venv/bin/python tests/run_jev_shadow.py tests/marketer_cases.json   # 녹화 재생(네트워크 없음)
  --save 결과.txt 로 저장

세는 것 (모두 n/N, 비율로 말하지 않음)
- J2 의도: Jev·규칙 일치, 기대 의도(시험 세트 type)와의 일치를 Jev·규칙 각각
- J1 문장 필터: 뺄 후보 구절 수, 규칙이 조건을 찾아 막은 수(override),
  "해석 못 한 말" 때문에 되묻기로 간 요청 중 그 말이 모두 '뺄 후보' 구절 안에 있던 수(= J1이 풀 수 있었던 되묻기)
- J3 부정 표현: Jev·규칙 일치
결정은 바뀌지 않는다. 이 수치로 S3(의도 반영)·S4(문장 필터 반영)를 켤지 정한다.
"""
import argparse
import json
import os
import socket
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
MODE = os.environ.get("BOT_JEV_MODE", "stub")
os.environ["BOT_JEV_MODE"] = MODE
if MODE in ("stub", "replay", "off"):           # 실제 호출이 아닌 모드는 네트워크를 막는다
    socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("네트워크 차단"))

from bot import jev, nlu  # noqa: E402

TYPE = {"aggregate": "aggregate_report", "target": "target_extraction"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("casefile")
    ap.add_argument("--save")
    a = ap.parse_args()
    cases = json.load(open(a.casefile, encoding="utf-8"))
    lines, cnt, dis = [], Counter(), []
    src = Counter()
    for i, c in enumerate(cases, 1):
        spec, p = nlu.extract(c["q"])
        act, info = nlu.decide(p)
        sh = jev.shadow(c["q"], p)
        if sh.get("error"):
            cnt["error"] += 1
            lines.append(f"#{i:02d} 오류 {sh['error']}")
            continue
        src[sh.get("source")] += 1
        d = sh["decisions"]
        it = next(x for x in d if x["point"] == "J2_intent")
        cnt["n"] += 1
        cnt["intent_agree"] += it["agree"]
        exp = TYPE.get(c.get("type"))
        if exp:
            cnt["typed"] += 1
            jev_rule_label = jev.INTENT_TO_RULE.get(it["answer"])
            cnt["jev_vs_exp"] += jev_rule_label == exp
            cnt["rule_vs_exp"] += p["request_type"] == exp
            if jev_rule_label != p["request_type"]:
                dis.append(f"#{i:02d} 기대 {exp} · 규칙 {p['request_type']} · Jev {it['answer']}({it['p']}) | {c['q'][:50]}")
        segs = [x for x in d if x["point"] == "J1_segment"]
        cnt["segs"] += len(segs)
        cnt["drop"] += sum(x["would"] == "drop" for x in segs)
        cnt["override"] += sum(x["override"] for x in segs)
        if act == "clarify" and "unparsed" in (info or []):
            cnt["unparsed_clarify"] += 1
            ss = jev.segments(nlu.jira_focus(c["q"]))
            dropped = " ".join(ss[x["seg"]] for x in segs if x["would"] == "drop")
            if p["unparsed"] and all(w in dropped for w in p["unparsed"]):
                cnt["j1_could_fix"] += 1
        ng = next((x for x in d if x["point"] == "J3_negation"), None)
        if ng:
            cnt["neg"] += 1
            cnt["neg_agree"] += ng["agree"]
        lines.append(f"#{i:02d} 규칙 {act:<7} 의도 규칙 {p['request_type']:<17} Jev {it['answer']:<17} p={it['p']:.2f} "
                     f"구절 {len(segs)} 뺄후보 {sum(x['would'] == 'drop' for x in segs)}")
    head = [f"# Jev 그림자 비교 · {os.path.basename(a.casefile)} · 모드 {MODE} · 출처 {dict(src)}",
            "(stub·replay_miss_stub 출처는 규칙을 흉내 낸 가짜 답이라 성능 수치가 아님)", "",
            f"- 처리 {cnt['n']}/{len(cases)} (오류 {cnt['error']})",
            f"- J2 의도: Jev·규칙 일치 {cnt['intent_agree']}/{cnt['n']} · 기대 의도와 일치 Jev {cnt['jev_vs_exp']}/{cnt['typed']}, "
            f"규칙 {cnt['rule_vs_exp']}/{cnt['typed']}",
            f"- J1 문장 필터: 구절 {cnt['segs']} 중 뺄 후보 {cnt['drop']}, 규칙이 막은 것 {cnt['override']} · "
            f"'해석 못 한 말' 되묻기 {cnt['unparsed_clarify']}건 중 J1이 풀 수 있었던 것 {cnt['j1_could_fix']}",
            f"- J3 부정 표현: 일치 {cnt['neg_agree']}/{cnt['neg']}", "", "## Jev·규칙 의도 불일치 (기대 의도가 있는 문항)"]
    out = "\n".join(head + (dis or ["(없음)"]) + ["", "## 문항별"] + lines)
    print(out)
    if a.save:
        open(a.save, "w", encoding="utf-8").write(out + "\n")


if __name__ == "__main__":
    main()
