"""Jev(TypeSafe) 판단 모델 포트와 그림자 운영 (S1·S2).

설계 (Jev_baseagent_적용검토.md, base-agent `app/jev/client.py` 참고)
- 포트-어댑터: 그래프는 `shadow()`만 부른다. 백엔드는 BOT_JEV_MODE로 고른다.
    off(기본)  : Jev를 부르지 않는다. 기존 동작과 완전히 같다
    stub       : 규칙으로 흉내 낸 가짜 답. 배선 확인용이며 성능 수치가 아니다
    replay     : 녹화된 답을 재생(네트워크 없음). 녹화가 없으면 stub으로 넘기고 출처에 표시
    record     : 실제 호출 + 녹화
    live       : 실제 호출
- 그림자 운영(S2): Jev 판단은 기록만 한다. 되묻기·이관·초안 결정(nlu.decide)에는 쓰지 않는다.
  Jev 호출이 실패해도 요청 처리는 그대로 진행한다(fail-open, 오류만 기록).
- 경계 가드(고객 제약, 사용자 승인 2026-11-21): 보내는 것은 마케터 요청 문장과 고정 질문뿐.
  보내기 전에 가명처리하고, 남은 개인정보가 있으면 막는다. 허용 호스트만. 감사 기록에는 원문 대신 지문(sha)만.
- 키는 draft_chatbot/.env.jev 에 사용자가 직접 넣는다. 코드·로그·녹화본에 키를 남기지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime
from urllib.parse import urlparse

from . import config

MODE = os.environ.get("BOT_JEV_MODE", "off")
ENV_FILE = os.path.join(config.BASE, ".env.jev")
CASSETTE_DIR = os.environ.get("BOT_JEV_CASSETTES", os.path.join(config.BASE, "tests", "jev_cassettes"))
AUDIT_PATH = os.path.join(config.VAR_DIR, "jev_audit.jsonl")
ALLOWED_HOSTS = {"api.typesafe.ai", "gateway.smith.langchain.com", "openrouter.ai"} | {
    h.strip() for h in os.environ.get("BOT_JEV_ALLOWED_HOSTS", "").split(",") if h.strip()}

# 그림자 요약에 쓰는 기준 (행동에는 쓰지 않음 — S3에서 시험 세트로 다시 정한다)
TAU_INTENT = 0.7          # 의도: 이 확률 이상이고 규칙과 같으면 "그대로 진행 가능"
TAU_SEG_DROP = 0.3        # 문장 필터: 이 확률 미만이면 "해석에서 뺄 후보"
MAX_SEGMENTS = 24         # 한 요청 질문 수 상한(32) 안에서 의도·부정 질문 자리를 남긴다


class EgressBlocked(RuntimeError):
    """가명처리 뒤에도 개인정보가 남았거나 허용되지 않은 호스트라 외부 호출을 막았다."""


# ------------------------------------------------------------------ 설정·키
def _load_env_file():
    """.env.jev 의 TYPESAFE_API_KEY · TYPESAFE_BASE_URL · BOT_JEV_MODEL 만 읽는다(값은 출력하지 않음)."""
    if not os.path.exists(ENV_FILE):
        return
    for line in open(ENV_FILE, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = (s.strip().strip('"').strip("'") for s in line.split("=", 1))
        if k in ("TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "BOT_JEV_MODEL") and v and not os.environ.get(k):
            os.environ[k] = v


def settings() -> dict:
    _load_env_file()
    return {"mode": os.environ.get("BOT_JEV_MODE", MODE),
            "model": os.environ.get("BOT_JEV_MODEL", "jev-latest"),
            "base_url": os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai"),
            "has_key": bool(os.environ.get("TYPESAFE_API_KEY"))}


def enabled() -> bool:
    return settings()["mode"] != "off"


# ------------------------------------------------------------------ 경계 가드
PII_PATTERNS = [
    ("phone", re.compile(r"01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}")),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("rrn", re.compile(r"\d{6}-?[1-4]\d{6}")),
    ("card", re.compile(r"\d{4}[-\s]\d{4}[-\s]\d{4}[-\s]\d{4}")),
    ("member_id", re.compile(r"\b[A-Z]?\d{7,}\b")),          # 회원 번호·고객 ID 같은 긴 숫자
]


def pseudonymize(text: str) -> tuple[str, int]:
    n = 0
    for name, pat in PII_PATTERNS:
        def _sub(m, name=name):
            nonlocal n
            n += 1
            return f"<{name.upper()}_{n}>"
        text = pat.sub(_sub, text)
    return text, n


def residual(text: str) -> list[str]:
    return [name for name, pat in PII_PATTERNS if pat.search(text)]


def _audit(point, base_url, model, source, payload: str, replaced: int, ok: bool, note: str = ""):
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "point": point,
           "host": urlparse(base_url).hostname, "model": model, "source": source,
           "payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
           "payload_chars": len(payload), "pii_replaced": replaced, "sent": ok, "note": note}
    os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
    with open(AUDIT_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def guard(point: str, state: str, cfg: dict, source: str, external: bool) -> str:
    """보내기 직전 한 곳. 가명처리 → 잔여 검사 → 호스트 확인 → 감사 기록. 가명 처리된 state를 돌려준다."""
    safe, n = pseudonymize(state)
    left = residual(safe)
    host = urlparse(cfg["base_url"]).hostname
    if left:
        _audit(point, cfg["base_url"], cfg["model"], source, safe, n, False, "residual:" + ",".join(left))
        raise EgressBlocked("가명처리 뒤에도 개인정보 형태가 남아 Jev 호출을 막았습니다: " + ",".join(left))
    if external and host not in ALLOWED_HOSTS:
        _audit(point, cfg["base_url"], cfg["model"], source, safe, n, False, "host_not_allowed")
        raise EgressBlocked(f"허용되지 않은 호스트입니다: {host}")
    _audit(point, cfg["base_url"], cfg["model"], source, safe, n, external)
    return safe


# ------------------------------------------------------------------ 질문 (영어 고정 문자열 — 캐시·재현성)
INTENT_CRITERIA = {
    "aggregate_report": "Asks for a count or sum such as sales revenue or number of orders over a period, "
                        "optionally broken down by day, channel or product category.",
    "target_extraction": "Asks for a list of customers to receive a push or promotion message (a target list).",
    "analysis": "Asks why something changed, compares causes, or asks for an interpretation or effect analysis.",
    "copywriting": "Asks to write marketing copy or message text.",
    "none": "None of the above, or not a data request.",
}
INTENT_TO_RULE = {"aggregate_report": "aggregate_report", "target_extraction": "target_extraction",
                  "analysis": "analysis", "copywriting": "out_of_scope", "none": "out_of_scope"}
NEGATION_CRITERIA = {
    "exclude_buyers": "Customers who purchased (in a stated period) should be removed from the target.",
    "only_non_buyers": "Only customers who have NOT purchased should be targeted.",
    "unclear": "The request does not say clearly who is excluded.",
}
SEGMENT_INSTRUCTIONS = ("Does this text segment state a data condition for the request, such as a period, product, "
                        "customer behavior, sales channel, metric, or an exclusion? Answer no for background, "
                        "purpose, schedule of use, or politeness.")


def _questions(segments: list[str], ask_negation: bool) -> dict:
    from langchain_typesafe import Choice, Noul, NoulCriteria
    qs = {"intent": Choice(instructions="Which kind of data request is this message?", criteria=INTENT_CRITERIA)}
    if ask_negation:
        qs["negation"] = Choice(instructions="Regarding past purchases, who should be excluded from the target?",
                                criteria=dict(NEGATION_CRITERIA))
    for i, s in enumerate(segments):
        qs[f"seg_{i}"] = Noul(instructions=SEGMENT_INSTRUCTIONS + f' Segment: "{s}"',
                              criteria=NoulCriteria(true="States a data condition.",
                                                    false="Background, purpose or politeness only."))
    return qs


# ------------------------------------------------------------------ 백엔드
def _norm(answers) -> dict:
    out = {}
    for qid, a in answers.items():
        a = a.model_dump() if hasattr(a, "model_dump") else dict(a)
        if a.get("type") == "noul":
            out[qid] = {"type": "noul", "p": float(a.get("noul", a.get("p", 0.0)))}
        elif a.get("type") == "choice":
            probs = {k: float(v) for k, v in a["probabilities"].items()}
            out[qid] = {"type": "choice", "choice": a["choice"], "p": probs.get(a["choice"], 0.0),
                        "probabilities": probs, "confidence": float(a.get("confidence", 0.0))}
    return out


def _cassette_key(model: str, state: str, questions: dict) -> str:
    blob = json.dumps({"model": model, "state": state,
                       "questions": {k: q.model_dump(mode="json") for k, q in sorted(questions.items())}},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class StubBackend:
    """규칙을 흉내 낸 가짜 답. 배선·기록 형식 확인용. 결과를 성능으로 읽지 않는다."""
    source = "stub"

    def classify(self, state, questions, hints):
        out = {}
        for qid in questions:
            if qid == "intent":
                lab = {v: k for k, v in INTENT_TO_RULE.items() if k != "none"}.get(hints["rule_intent"], "none")
                out[qid] = {"type": "choice", "choice": lab, "p": 0.9, "probabilities": {lab: 0.9}, "confidence": 0.8}
            elif qid == "negation":
                lab = "unclear" if hints["negation_unclear"] else "exclude_buyers"
                out[qid] = {"type": "choice", "choice": lab, "p": 0.8, "probabilities": {lab: 0.8}, "confidence": 0.6}
            else:
                i = int(qid.split("_")[1])
                out[qid] = {"type": "noul", "p": 0.85 if hints["seg_rule"][i] else 0.15}
        return {"answers": out, "model": "stub", "input_tokens": None, "request_id": None}


class LiveBackend:
    source = "live"

    def __init__(self, cfg):
        from langchain_typesafe import TypeSafeClassifier
        if not os.environ.get("TYPESAFE_API_KEY"):
            raise RuntimeError("Jev 키가 없습니다. draft_chatbot/.env.jev 에 TYPESAFE_API_KEY 를 넣어 주세요.")
        self.clf = TypeSafeClassifier(model=cfg["model"], base_url=cfg["base_url"], timeout=10.0)

    def classify(self, state, questions, hints):
        r = self.clf.invoke({"state": state, "questions": questions})
        usage = getattr(r, "usage", None)
        return {"answers": _norm(r.answers), "model": r.model, "request_id": r.request_id,
                "input_tokens": (usage or {}).get("input_tokens") if isinstance(usage, dict)
                else getattr(usage, "input_tokens", None)}


class CassetteStore:
    """녹화본: 답·모델·시각만 저장한다. 요청 원문은 저장하지 않는다(키는 원문 해시)."""

    def __init__(self, root=CASSETTE_DIR):
        self.root = root

    def get(self, key):
        p = os.path.join(self.root, key + ".json")
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None

    def put(self, key, res):
        os.makedirs(self.root, exist_ok=True)
        data = {"model": res["model"], "answers": res["answers"], "input_tokens": res.get("input_tokens"),
                "recorded_at": datetime.now().isoformat(timespec="seconds")}
        json.dump(data, open(os.path.join(self.root, key + ".json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)


def _backend(cfg):
    m = cfg["mode"]
    if m == "stub":
        return StubBackend(), False
    if m in ("live", "record"):
        return LiveBackend(cfg), True
    return None, False          # replay: classify()에서 녹화 조회


def classify(point: str, state: str, questions: dict, hints: dict) -> dict:
    """포트. 경계 가드를 지난 뒤 백엔드를 부르고, 정규화한 결과에 출처·지연을 붙인다."""
    cfg = settings()
    mode = cfg["mode"]
    backend, external = _backend(cfg)
    safe = guard(point, state, cfg, mode, external)
    key = _cassette_key(cfg["model"], safe, questions)
    t0 = time.perf_counter()
    if mode == "replay":
        hit = CassetteStore().get(key)
        if hit:
            res, source = {**hit, "request_id": None}, "replay"
        else:
            res, source = StubBackend().classify(safe, questions, hints), "replay_miss_stub"
    else:
        res, source = backend.classify(safe, questions, hints), backend.source
        if mode == "record":
            CassetteStore().put(key, res)
            source = "record"
    res["source"] = source
    res["latency_ms"] = round((time.perf_counter() - t0) * 1000)
    return res


# ------------------------------------------------------------------ 그림자 운영 (S2)
SEG_SPLIT = re.compile(r"[\n.!?~]+|,\s+|(?<=요)\s+|(?<=다)\s+|(?<=고)\s+(?=[가-힣]{2,}\s)")


def segments(text: str) -> list[str]:
    segs = [s.strip(" \t·-") for s in SEG_SPLIT.split(text)]
    return [s for s in segs if len(s) >= 2][:MAX_SEGMENTS]


def rule_has_condition(seg: str, p: dict) -> bool:
    """규칙이 이 구절에서 조건을 찾았는가. 찾은 구절은 Jev 답과 관계없이 남긴다(재현율 우선)."""
    from . import nlu
    words = list(p.get("categories", [])) + list(p.get("channels", []))
    for k in ("period", "exclude_purchase"):
        if p.get(k) and p[k].get("text"):
            words.append(p[k]["text"])
    if any(w and w in seg for w in words):
        return True
    if nlu.parse_period(seg):
        return True
    return bool(re.search(nlu.NEG + "|" + nlu.AGG_RE + "|" + nlu.TARGET_RE +
                          r"|구매|샀|장바구니|찜|조회|일별|채널|카테고리|등급", seg))


def shadow(text: str, p: dict) -> dict:
    """규칙 해석(p)과 나란히 Jev 판단을 기록한다. 결정에는 쓰지 않는다. 실패해도 예외를 밖으로 내지 않는다."""
    from . import nlu
    cfg = settings()
    if cfg["mode"] == "off":
        return {}
    focus = nlu.jira_focus(text).strip()          # 앞뒤 공백 정리(녹화 키 안정화)
    segs = segments(focus)
    seg_rule = [rule_has_condition(s, p) for s in segs]
    ask_neg = bool(re.search(nlu.NEG, focus))
    out = {"mode": cfg["mode"], "model": cfg["model"], "decisions": []}
    try:
        res = classify("interpret", focus, _questions(segs, ask_neg),
                       {"rule_intent": p["request_type"], "negation_unclear": p.get("negation_unclear", False),
                        "seg_rule": seg_rule})
    except Exception as e:                               # fail-open: 처리 흐름에는 영향 없음
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        return out
    a = res["answers"]
    out.update(source=res["source"], latency_ms=res["latency_ms"], input_tokens=res.get("input_tokens"),
               jev_model=res.get("model"))
    if "intent" in a:
        lab, pr = a["intent"]["choice"], a["intent"]["p"]
        agree = INTENT_TO_RULE.get(lab) == p["request_type"]
        out["decisions"].append({"point": "J2_intent", "answer": lab, "p": round(pr, 3), "rule": p["request_type"],
                                 "agree": agree,
                                 "would": "proceed" if agree and pr >= TAU_INTENT else "clarify"})
    if "negation" in a:
        lab, pr = a["negation"]["choice"], a["negation"]["p"]
        rule = "unclear" if p.get("negation_unclear") else ("exclude_buyers" if p.get("exclude_purchase") else "none")
        out["decisions"].append({"point": "J3_negation", "answer": lab, "p": round(pr, 3), "rule": rule,
                                 "agree": lab == rule})
    for i, s in enumerate(segs):
        q = a.get(f"seg_{i}")
        if not q:
            continue
        drop = q["p"] < TAU_SEG_DROP and not seg_rule[i]
        out["decisions"].append({"point": "J1_segment", "seg": i, "chars": len(s), "p": round(q["p"], 3),
                                 "rule_condition": seg_rule[i],
                                 "would": "drop" if drop else "keep",
                                 "override": q["p"] < TAU_SEG_DROP and seg_rule[i]})
    return out


# ------------------------------------------------------------------ S3: 결정 반영 (R1·R2만, 사용자 승인 2026-11-21)
TAU_APPLY = 0.95
REAL_SOURCES = ("live", "record", "replay")       # stub·replay_miss_stub·오류는 반영하지 않음 (규칙 결과 유지)
DATA_TYPES = ("aggregate_report", "target_extraction")


def apply_rules(p: dict, act: str, info, sh: dict):
    """그림자 판단(sh)으로 규칙 결정을 '되묻기' 쪽으로만 옮긴다. → (act, info, 적용 규칙 또는 None)

    R1: 규칙은 범위 밖(분석어 없음)인데 Jev가 집계·대상자로 p ≥ 0.95 → 이관 대신 의도 확인
    R2: 규칙과 Jev가 집계·대상자로 엇갈리고 Jev p ≥ 0.95 → 초안·다른 되묻기 대신 의도 확인
    분석형 판단(analysis)과 등급·쿠폰 같은 범위 이관은 건드리지 않는다. 초안을 새로 만들지 않는다.
    """
    if os.environ.get("BOT_JEV_APPLY", "1") == "0" or not sh or sh.get("error") \
            or sh.get("source") not in REAL_SOURCES:
        return act, info, None
    it = next((d for d in sh.get("decisions", []) if d["point"] == "J2_intent"), None)
    if not it or it["p"] < TAU_APPLY:
        return act, info, None
    jev_type = INTENT_TO_RULE.get(it["answer"])
    if jev_type not in DATA_TYPES:
        return act, info, None
    rule_type = p["request_type"]
    if rule_type == "out_of_scope" and act == "handoff":
        return "clarify", ["intent_confirm"], "R1"
    if rule_type in DATA_TYPES and jev_type != rule_type and act in ("draft", "clarify"):
        return "clarify", ["intent_confirm"], "R2"
    return act, info, None


def summary(sh: dict) -> dict:
    """처리 기록(requests.jsonl)용 요약. 문장 원문은 넣지 않는다."""
    if not sh:
        return {}
    if sh.get("error"):
        return {"mode": sh.get("mode"), "error": sh["error"]}
    d = sh.get("decisions", [])
    it = next((x for x in d if x["point"] == "J2_intent"), None)
    segs = [x for x in d if x["point"] == "J1_segment"]
    return {"mode": sh["mode"], "source": sh.get("source"), "latency_ms": sh.get("latency_ms"),
            "intent": it and {k: it[k] for k in ("answer", "p", "rule", "agree")},
            "segments": len(segs), "would_drop": sum(x["would"] == "drop" for x in segs),
            "rule_override": sum(x["override"] for x in segs)}
