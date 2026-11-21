"""요청 해석 (offline). 판단은 닫힌 규칙, 되묻기 문장은 템플릿.

원칙 (검토 H1·H2): 확실히 풀리지 않는 표현은 추측하지 않는다.
- 부정·제외 표현이 있는데 "기간 + 구매 + 제외" 꼴로 깔끔히 풀리지 않으면 되묻는다.
- 분석형 질문(왜·대비·원인 등)은 BA로 넘긴다.
- 해석에 쓰이지 않은 낱말은 unparsed 로 남겨 마케터 확인·BA 화면에 표시한다.
동의어·등급·분석어 사전은 rules/vocab.csv (BA가 직접 수정).
"""
import calendar
import re
from datetime import date, timedelta

from schema import Condition, SpecExtraction

from . import config, datastore, ruleset

PERIOD_PATTERNS = [
    r"\d{4}[-./]\d{1,2}[-./]\d{1,2}\s*[~\-–]\s*\d{4}[-./]\d{1,2}[-./]\d{1,2}",
    r"\d{1,2}월\s*\d{1,2}일\s*[~\-–]\s*(\d{1,2}월\s*)?\d{1,2}일",
    r"최근\s*(\d+|한|두|세)\s*(일|주|개월|달|년)",
    r"지난\s?주", r"이번\s?주", r"지난\s?달", r"이번\s?달",
    r"(작년|올해|\d{4}년)?\s*\d{1,2}월", r"작년", r"올해",
]


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def parse_period(text: str):
    """기간 표현 → {'from','to','text'} | None. 기준일 config.TODAY 고정. 미래 끝은 TODAY 로 자른다."""
    t = config.TODAY
    m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})\s*[~\-–]\s*(\d{4})[-./](\d{1,2})[-./](\d{1,2})", text)
    if m:
        a, b = date(int(m[1]), int(m[2]), int(m[3])), date(int(m[4]), int(m[5]), int(m[6]))
        return {"from": _ymd(a), "to": _ymd(b), "text": m[0]}
    m = re.search(r"(\d{1,2})월\s*(\d{1,2})일\s*[~\-–]\s*(?:(\d{1,2})월\s*)?(\d{1,2})일", text)
    if m:
        y = t.year if int(m[1]) <= t.month else t.year - 1
        a = date(y, int(m[1]), int(m[2]))
        b = date(y, int(m[3] or m[1]), int(m[4]))
        return {"from": _ymd(a), "to": _ymd(b), "text": m[0]}
    m = re.search(r"최근\s*(\d+|한|두|세)\s*(일|주|개월|달|년)", text)
    if m:
        n = {"한": 1, "두": 2, "세": 3}.get(m[1]) or int(m[1])
        days = {"일": n, "주": 7 * n, "개월": 30 * n, "달": 30 * n, "년": 365 * n}[m[2]]
        return {"from": _ymd(t - timedelta(days=days - 1)), "to": _ymd(t), "text": m[0]}
    if re.search(r"지난\s?주", text):
        mon = t - timedelta(days=t.weekday() + 7)
        return {"from": _ymd(mon), "to": _ymd(mon + timedelta(days=6)), "text": "지난주"}
    if re.search(r"이번\s?주", text):
        return {"from": _ymd(t - timedelta(days=t.weekday())), "to": _ymd(t), "text": "이번 주"}
    if re.search(r"지난\s?달", text):
        y, mo = (t.year, t.month - 1) if t.month > 1 else (t.year - 1, 12)
        return {"from": _ymd(date(y, mo, 1)), "to": _ymd(date(y, mo, calendar.monthrange(y, mo)[1])), "text": "지난달"}
    if re.search(r"이번\s?달", text):
        return {"from": _ymd(date(t.year, t.month, 1)), "to": _ymd(t), "text": "이번 달"}
    m = re.search(r"(작년|올해|(\d{4})년)?\s*(\d{1,2})월", text)
    if m:
        mo = int(m[3])
        if m[2]:
            y = int(m[2])
        elif m[1] == "작년":
            y = t.year - 1
        elif m[1] == "올해":
            y = t.year                                   # '올해 12월'은 올해 (검토 M1)
        else:
            y = t.year if mo <= t.month else t.year - 1
        last = date(y, mo, calendar.monthrange(y, mo)[1])
        return {"from": _ymd(date(y, mo, 1)), "to": _ymd(min(last, t)) if last > t >= date(y, mo, 1) else _ymd(last),
                "text": m[0].strip()}
    if "작년" in text:
        return {"from": f"{t.year - 1}0101", "to": f"{t.year - 1}1231", "text": "작년"}
    if "올해" in text:
        return {"from": f"{t.year}0101", "to": _ymd(t), "text": "올해"}
    return None


def _vocab():
    v = {"category": {}, "channel": {}, "grade": {}, "analysis": [], "target_word": [], "ignore": set(), "ignore_suffix": []}
    for r in ruleset.vocab_rows():
        if r["kind"] in ("analysis", "target_word", "ignore_suffix"):
            v[r["kind"]].append(r["word"])
        elif r["kind"] == "ignore":
            v["ignore"].add(r["word"])
        elif r["kind"] in v:
            v[r["kind"]][r["word"]] = r["standard"]
    return v


def _categories(text, cats, syn):
    """→ (표준 중분류 목록, 요청에서 소비한 낱말 목록). 대분류 이름(예: 소모품)은 소속 중분류 전체로."""
    found, words = set(), []
    for w, std in syn.items():
        if w in text:
            found.add(std); words.append(w)
    for cat_l, cat_m, cat_s in cats:
        if cat_s and cat_s in text:
            found.add(cat_m); words.append(cat_s)
        if cat_l and cat_l in text:
            found.add(cat_m); words.append(cat_l)
    for _l, cat_m, _s in cats:
        if cat_m in text:
            # '냉장고'는 '김치냉장고'·'업소용 냉장고' 안에서만 나오면 제외
            if cat_m == "냉장고" and not re.search(r"(?<!김치)(?<!업소용 )냉장고", text):
                continue
            found.add(cat_m); words.append(cat_m)
    return sorted(found), words


NEG = r"제외|빼고|빼서|빼 주|빼줘|빼주|않은|안 산|안산|미구매|없는"
EXCLUDE_RE = re.compile(r"(?P<who>(?:구매|산|샀던|구매한|구매하신)\s*(?:사람|고객|회원|자)?(?:들)?)\s*(?:은|는|을|를)?\s*(?:제외|빼)[가-힣]*")
TARGET_RE = r"대상자|타깃|타겟|푸시 대상|발송 대상|푸시 보낼|푸시"
AGG_RE = r"매출|주문\s*수|주문\s*건|사용률|쿠폰|집계|실적"
STOPWORDS = set("""대상자 타깃 타겟 푸시 발송 대상 명단 리스트 매출 순매출 주문 건수 집계 실적 뽑아 뽑아줘 뽑아주세요 알려 알려줘
주세요 부탁 부탁해요 해주세요 해줘 보내 요청 고객 회원 사람 중에 기준 프로모션 다음 캠페인 위한 위해 싶어요 합니다 드려요 빼고요
하고 정리 데이터 필요 필요해요 있나요 가능 가능한 수신동의 알림 문자 보낼 보내려고 진행 행사 이벤트 전체
안에 동안 관련해서 관련 별로 일별로 채널별로 카테고리별로 같은 정도 혹시 좀 기간 내 사이""".split())
PARTICLES = ("에서", "으로", "에게", "까지", "부터", "하고", "이랑", "랑", "은", "는", "이", "가", "을", "를", "의", "에", "로", "만", "도", "요", "들")


REQUEST_ENDINGS = re.compile(r"(주세요|주실래요|줘요|줘|줄래요|드려요|드립니다|부탁해요|부탁합니다|해요|합니다|궁금해요|궁금합니다|싶어요|보여|알려|뽑아|추출|부탁|궁금)$")


def _is_stop(w: str, v=None) -> bool:
    if w in STOPWORDS or REQUEST_ENDINGS.search(w):
        return True
    if v:
        lw = w.lower()
        if lw in {x.lower() for x in v["target_word"]} or w in v["ignore"]:
            return True
        if any(w.endswith(sfx) for sfx in v["ignore_suffix"]):
            return True
        for p_ in PARTICLES:
            if w.endswith(p_) and w[: -len(p_)] in v["ignore"]:
                return True
    for p_ in PARTICLES:
        if w.endswith(p_) and w[: -len(p_)] in STOPWORDS:
            return True
    return False


JIRA_REQ = re.compile(r"요청\s*사항\s*[:：]\s*(.+?)(?=(?:희망일|기한|마감|비고|참고|첨부)\s*[:：]|$)", re.S)


def jira_focus(text: str) -> str:
    """Jira 양식이면 '요청 사항' 칸만 해석한다(제목·배경 등 설명 문장 제외). 아니면 그대로 (개선 ①)."""
    m = JIRA_REQ.search(text)
    return m.group(1).strip() if m else text


def classify_intent(text: str, analysis_words, target_words=()) -> str:
    if any(w in text for w in analysis_words):
        return "analysis"
    if re.search(r"문구|카피|써\s*주|작성해", text):
        return "out_of_scope"
    tw = any(w.lower() in text.lower() for w in target_words)
    if re.search(TARGET_RE, text) or tw or (re.search(r"고객|사람|회원", text) and re.search(r"뽑|추출|리스트|명단", text)):
        return "target_extraction"
    if re.search(AGG_RE, text):
        return "aggregate_report"
    return "out_of_scope"


def intent_from_answer(ans: str):
    """의도 확인 질문(intent_confirm)에 대한 마케터 답 → 요청 종류. 알 수 없으면 None."""
    if re.search(r"대상자|명단|리스트|타겟|타깃|푸시|발송|고객", ans):
        return "target_extraction"
    if re.search(r"집계|숫자|매출|주문|건수|실적", ans):
        return "aggregate_report"
    return None


def extract(text: str, force_type: str = None):
    """→ (SpecExtraction, parsed dict). parsed 는 SQL 생성·누락 판정·확인 요약에 쓴다.
    force_type: 마케터가 의도 확인 질문에 답한 요청 종류. 있으면 의도 분류를 건너뛴다."""
    if config.PROVIDER != "offline":
        raise NotImplementedError("외부 모델 모드는 모델 경로·반출 범위 승인 후 구현합니다. 지금은 offline 만 지원합니다.")
    text = jira_focus(text)
    v = _vocab()
    cats, chnls = datastore.vocab()
    rtype = force_type or classify_intent(text, v["analysis"], v["target_word"])
    cat_list, cat_words = _categories(text, cats, v["category"])
    cat_l_of = {m: l for l, m, _s in cats}
    ch_words = [w for w in v["channel"] if w in text] + [c for c in chnls if c in text]
    p = {"request_type": rtype, "categories": cat_list,
         "channels": sorted({v["channel"].get(w, w) for w in ch_words}),
         "grades": sorted({g for w, g in v["grade"].items() if re.search(rf"{w}\s*(등급|회원|고객)", text)}),
         "notes": [], "negation_unclear": False,
         # 소모품·홈스타일은 사내 순매출 기준 밖 → 되묻기보다 먼저 이관
         "excluded_scope": any(cat_l_of.get(c) in ("소모품", "홈스타일") for c in cat_list)}
    consumed = cat_words + ch_words

    # 제외 조건 (H1): "<기간> <상품?> 구매자 제외" 꼴만 받아들인다
    main = text
    if re.search(NEG, text):
        m = EXCLUDE_RE.search(text)
        if m:
            before = text[: m.start()]
            per = None
            for pat in PERIOD_PATTERNS:
                hits = list(re.finditer(pat, before))
                if hits:
                    h = hits[-1]
                    per = parse_period(h.group(0))
                    span_start = h.start()
                    break
            if per:
                p["exclude_purchase"] = per
                main = text[:span_start] + " " + text[m.end():]
                consumed += [per["text"], m.group(0)]
            else:
                p["exclude_purchase"] = {"from": None, "to": None, "text": m.group(0)}
                main = text[: m.start()] + " " + text[m.end():]
                consumed.append(m.group(0))
        else:
            p["negation_unclear"] = True       # '안 산', '미구매' 등 — 추측하지 않고 되묻기

    per = parse_period(main)
    p["period"] = per
    if per:
        consumed.append(per["text"])

    if rtype == "aggregate_report":
        p["metric"] = ("coupon_use_rate" if re.search(r"쿠폰|사용률", text) else
                       "order_count" if re.search(r"주문\s*(수|건)", text) else
                       "net_sales" if "매출" in text else None)
        p["group_by"] = ("day" if re.search(r"일별|일자별|날짜별|매일", text) else
                         "channel" if "채널" in text else
                         "category" if re.search(r"카테고리|품목|상품군", text) else None)
        p["output_format"] = "list" if p["group_by"] else "count_only"
        consumed += ["일별", "일자별", "날짜별", "채널별", "채널", "카테고리별", "카테고리", "품목별", "상품군별", "쿠폰", "사용률"]
    elif rtype == "target_extraction":
        p["behavior"] = ("purchase" if re.search(r"구매|샀|산\s*(사람|고객|회원)|주문한", main) else
                         "cart" if "장바구니" in main else
                         "wish" if re.search(r"찜|위시", main) else
                         "view" if re.search(r"조회|본\s*(사람|고객)|봤|관심", main) else None)
        p["output_format"] = "push_target_list"
        consumed += ["구매한", "구매", "구매자", "장바구니", "담은", "찜한", "찜", "조회한", "조회", "본", "봤던", "관심"]

    # 해석에 안 쓰인 낱말 (H2)
    rest = text
    for c in sorted(set(consumed), key=len, reverse=True):
        rest = rest.replace(c, " ")
    tokens = [w for w in re.findall(r"[가-힣A-Za-z0-9·]+", rest) if len(w) >= 2]
    p["unparsed"] = [w for w in tokens if not _is_stop(w, v)]

    conds = [Condition(term="category", raw_expr=c) for c in p["categories"]]
    conds += [Condition(term="channel", raw_expr=c) for c in p["channels"]]
    conds += [Condition(term="grade", raw_expr=g, term_id="T06") for g in p["grades"]]
    if p.get("behavior"):
        conds.append(Condition(term="behavior", raw_expr=p["behavior"]))
    if p.get("exclude_purchase"):
        conds.append(Condition(term="exclude_purchase", raw_expr=p["exclude_purchase"]["text"]))
    if p.get("group_by"):
        conds.append(Condition(term="group_by", raw_expr=p["group_by"]))
    spec = SpecExtraction(
        purpose=text[:100], request_type=rtype if rtype != "analysis" else "out_of_scope", conditions=conds,
        period_expr=f"{per['from']}~{per['to']} ({per['text']})" if per else None,
        metrics=[p["metric"]] if p.get("metric") else [], output_format=p.get("output_format", "unknown"))
    return spec, p


QUESTIONS = {
    "metric": "어떤 숫자가 필요하신가요? (순매출 / 주문 수)",
    "period": "기간을 알려 주세요. (예: 10월, 최근 3개월, 10월 1일~20일)",
    "categories": "어떤 상품군 고객인가요? (예: 공기청정기, 세탁기, 김치냉장고)",
    "behavior": "어떤 고객인가요? (구매한 고객 / 조회한 고객 / 장바구니에 담은 고객 / 찜한 고객)",
    "exclude_period": "제외할 구매 기간을 알려 주세요. 예: '최근 1년 구매자 제외'",
    "negation": "제외 조건을 정확히 알려 주세요. 예: '최근 1년 공기청정기 구매자 제외'",
    "unparsed": "다음 말은 조건에 반영하지 못했습니다: {words}. 이 말 없이 진행할까요? ('진행' 또는 조건을 다시 적어 주세요)",
    "categories_confirm": "상품군을 {cats} 모두 포함할까요? ('진행' 또는 원하는 상품군만 적어 주세요)",
    # Jev R1·R2: 규칙과 판단 모델의 의도 판단이 엇갈릴 때 (초안·이관 대신 먼저 확인)
    "intent_confirm": "어떤 요청인지 먼저 확인할게요. 매출·주문 같은 숫자 집계인가요, 푸시 보낼 고객 명단(대상자)인가요? ('집계' 또는 '대상자'로 답해 주세요)",
}

# 이관 사유 (그래프 handoff_reason 과 마케터 안내)
HANDOFF = {
    "analysis": ("out_of_scope", "원인·비교 같은 분석형 질문은 BA가 직접 맡습니다."),
    "out_of_scope": ("out_of_scope", "지금은 '푸시 대상자 추출'과 '매출·주문 집계'만 챗봇이 초안을 만듭니다."),
    "coupon": ("out_of_scope", "쿠폰 사용률은 기간 정의를 BA와 확정한 뒤 지원합니다."),
    "grade": ("out_of_scope", "회원 등급 기준(마스터 vs 이력)이 아직 확정되지 않아 BA가 처리합니다."),
    "before_data": ("out_of_scope", "요청 기간이 데이터 보관 범위(2026-05-01 이후) 밖입니다."),
    "excluded_scope": ("out_of_scope", "소모품·홈스타일은 사내 순매출 기준에서 빠지는 상품군이라 BA가 기준을 정해 처리합니다."),
    "future": ("out_of_scope", "요청 기간이 아직 데이터가 없는 미래입니다(주문 데이터는 2026-11-19까지)."),
}


def decide(p: dict, unparsed_ack: bool = False):
    """→ ('handoff', key) | ('clarify', [fields]) | ('draft', None)
    unparsed_ack: 마케터가 '해석 못 한 말 없이 진행'에 동의했는가"""
    if p["request_type"] in ("analysis", "out_of_scope"):
        return "handoff", p["request_type"]
    if p["grades"]:
        return "handoff", "grade"
    if p.get("metric") == "coupon_use_rate":
        return "handoff", "coupon"
    if p.get("excluded_scope") and p["request_type"] == "aggregate_report":
        return "handoff", "excluded_scope"
    miss = []
    if p["negation_unclear"]:
        miss.append("negation")
    if p["request_type"] == "aggregate_report":
        if not p.get("metric"):
            miss.append("metric")
        if not p.get("period"):
            miss.append("period")
    else:
        if not p["categories"]:
            miss.append("categories")
        if not p.get("behavior"):
            miss.append("behavior")
        if p.get("behavior") and not p.get("period"):
            miss.append("period")
        if p.get("exclude_purchase") and not p["exclude_purchase"]["from"]:
            miss.append("exclude_period")
    if p["unparsed"] and not unparsed_ack:
        miss.append("unparsed")
    # 상품군이 둘 이상이면 확인 (맥락 문장의 상품군이 섞여 들어오는 무표시 오답 방지)
    if len(p["categories"]) > 1 and not unparsed_ack:
        miss.append("categories_confirm")
    if miss:
        return "clarify", miss
    per = p.get("period")
    if per and per["from"] > config.TODAY.strftime("%Y%m%d"):
        return "handoff", "future"
    order_based = p["request_type"] == "aggregate_report" or p.get("behavior") == "purchase"
    if per and order_based and per["to"] < config.ORDER_DATA_FROM:
        return "handoff", "before_data"
    if per and order_based and per["from"] < config.ORDER_DATA_FROM:
        p["notes"].append("요청 기간 중 2026-05-01 이전은 데이터가 없어 제외됩니다.")
    return "draft", None


def understood(p: dict) -> str:
    """마케터에게 보여 줄 '이렇게 이해했습니다' 요약 (H2)."""
    if p["request_type"] not in ("aggregate_report", "target_extraction"):
        return "요청 종류를 확인하는 중입니다"
    parts = []
    if p.get("period"):
        parts.append(f"기간 {p['period']['from']}~{p['period']['to']}")
    if p["categories"]:
        parts.append("상품군 " + ", ".join(p["categories"]))
    if p["channels"]:
        parts.append("채널 " + ", ".join(p["channels"]))
    if p["request_type"] == "aggregate_report":
        parts.append({"net_sales": "순매출", "order_count": "주문 수"}.get(p.get("metric"), "?"))
        if p.get("group_by"):
            parts.append({"day": "일별", "channel": "채널별", "category": "카테고리별"}[p["group_by"]])
    else:
        parts.append({"purchase": "구매", "view": "조회", "cart": "장바구니", "wish": "찜"}.get(p.get("behavior"), "?") + " 고객")
        parts.append("푸시 수신동의·발송 횟수 제한 적용")
        if p.get("exclude_purchase"):
            e = p["exclude_purchase"]
            parts.append(f"{e['from']}~{e['to']} 구매자 제외")
    s = " · ".join(parts)
    if p["unparsed"]:
        s += f"  (해석하지 못한 말: {', '.join(p['unparsed'])})"
    return s
