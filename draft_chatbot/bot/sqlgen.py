"""SQL 초안 생성 (offline 템플릿). 업무 용어의 SQL은 rules/terms.csv 에서 읽어 CTE로 조립한다
→ BA가 T01·T03·T04 정의를 고치면 챗봇 SQL도 바뀐다 (검토 M2, 고객 요청 C12).
SQLite 방언(Redshift 대역)."""
from . import config
from .ruleset import RuleSet, check_rules, term

EXCLUDED_CAT_L = {"소모품", "홈스타일"}       # 순매출(T01) 범위에서 빠지는 상품군 (데이터로 확인)


def _q(vals):
    return ", ".join("'" + v.replace("'", "''") + "'" for v in vals)


def _ts(ymd: str, end=False) -> str:
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]} {'23:59:59' if end else '00:00:00'}"


def _cust_id_expr(rs: RuleSet, alias: str) -> str:
    """T02: 행동·푸시 계열 CUST_ID = 'C' || MBR_NO (tables.csv id_transform)."""
    tr = next((t.id_transform for t in rs.tables if t.id_system == "CUST_ID" and t.id_transform), "'C' || MBR_NO")
    return tr.replace("MBR_NO", f"{alias}.MBR_NO")


def build(p: dict, rs: RuleSet, cats_l: dict):
    """→ dict(sql, qtype, term_versions, assumptions, scope_notes) 또는 예외(ValueError: 이관 사유 키)."""
    if p["request_type"] == "aggregate_report":
        return _aggregate(p, rs, cats_l)
    return _target(p, rs)


def _aggregate(p, rs, cats_l):
    per = p["period"]
    lo, hi = max(per["from"], config.ORDER_DATA_FROM), per["to"]
    dims = {"day": ("n.DT", ""),
            "channel": ("c.CHNL_NM", "JOIN LX_ZCM_CHNL c ON n.CHNL_CD = c.CHNL_CD"),
            "category": ("k.CAT_M", "JOIN LX_KRP2_C k ON n.CAT_CD = k.CAT_CD")}
    joins, where = [], [f"n.DT BETWEEN '{lo}' AND '{hi}'"]
    if p["categories"]:
        if any(cats_l.get(c) in EXCLUDED_CAT_L for c in p["categories"]):
            raise ValueError("excluded_scope")
        joins.append(dims["category"][1]); where.append(f"k.CAT_M IN ({_q(p['categories'])})")
    if p["channels"]:
        joins.append(dims["channel"][1]); where.append(f"c.CHNL_NM IN ({_q(p['channels'])})")
    sel, grp = "", ""
    if p.get("group_by"):
        col, j = dims[p["group_by"]]
        if j and j not in joins:
            joins.append(j)
        sel, grp = f"{col} AS 구분, ", f"GROUP BY {col} ORDER BY {'1' if p['group_by'] == 'day' else '2 DESC'}"

    if p["metric"] == "net_sales":
        t = term(rs, "T01")
        sql = (f"WITH net AS ({check_rules.fill(t.sql)})\n"
               f"SELECT {sel}SUM(n.NET_AMT) AS 순매출\nFROM net n\n" + "\n".join(joins) +
               f"\nWHERE {' AND '.join(where)}\n{grp}")
        used = {"T01": t.version}
        scope = ["순매출 = 판매금액 − 할인 − 취소·반품(주문일 기준), 소모품·홈스타일 제외 (대시보드와 같은 기준)"]
    else:  # order_count — 주문 상세 기준으로 차원 연결
        t4 = term(rs, "T04")
        dim_join = {"day": ("h.ORD_DT", ""),
                    "channel": ("c.CHNL_NM", "JOIN LX_ZCM_CHNL c ON h.CHNL_CD = c.CHNL_CD"),
                    "category": ("k.CAT_M", "")}
        need_cat = bool(p["categories"]) or p.get("group_by") == "category"
        j2 = ["JOIN valid v ON v.ORD_NO = h.ORD_NO"]
        if need_cat:
            j2 += ["JOIN LX_HJV1_D d ON d.ORD_NO = h.ORD_NO", "JOIN LX_KRP3_SKU s ON s.SKU_CD = d.SKU_CD",
                   "JOIN LX_KRP1_M m ON m.MDL_CD = s.MDL_CD", "JOIN LX_KRP2_C k ON k.CAT_CD = m.CAT_CD"]
        w2 = [f"h.ORD_DT BETWEEN '{lo}' AND '{hi}'"]
        if p["categories"]:
            w2.append(f"k.CAT_M IN ({_q(p['categories'])})")
        if p["channels"] or p.get("group_by") == "channel":
            j2.append(dim_join["channel"][1])
        if p["channels"]:
            w2.append(f"c.CHNL_NM IN ({_q(p['channels'])})")
        sel2 = grp2 = ""
        if p.get("group_by"):
            col = dim_join[p["group_by"]][0]
            sel2, grp2 = f"{col} AS 구분, ", f"GROUP BY {col} ORDER BY {'1' if p['group_by'] == 'day' else '2 DESC'}"
        sql = (f"WITH valid AS ({check_rules.fill(t4.sql)})\n"
               f"SELECT {sel2}COUNT(DISTINCT h.ORD_NO) AS 주문수\nFROM LX_HJV1_H h\n" + "\n".join(j2) +
               f"\nWHERE {' AND '.join(w2)}\n{grp2}")
        used = {"T04": t4.version}
        scope = ["주문 수 = 전체취소(ORD_STAT 90)를 뺀 주문 건수"]
    notes = list(p["notes"])
    if per["from"] < config.ORDER_DATA_FROM:
        notes.append("2026-05-01 이전 기간은 데이터가 없어 제외")
    return {"sql": sql.strip(), "qtype": "adhoc", "term_versions": used, "assumptions": notes, "scope_notes": scope}


def _target(p, rs):
    per = p["period"]
    cats = _q(p["categories"])
    cust = _cust_id_expr(rs, "h")
    b = p["behavior"]
    if b == "purchase":
        base = (f"SELECT DISTINCT {cust} AS CUST_ID FROM LX_HJV1_H h "
                f"JOIN LX_HJV1_D d ON d.ORD_NO = h.ORD_NO JOIN LX_KRP3_SKU s ON s.SKU_CD = d.SKU_CD "
                f"JOIN LX_KRP1_M m ON m.MDL_CD = s.MDL_CD JOIN LX_KRP2_C k ON k.CAT_CD = m.CAT_CD "
                f"WHERE k.CAT_M IN ({cats}) AND h.ORD_DT BETWEEN '{per['from']}' AND '{per['to']}' AND h.ORD_STAT <> '90'")
    elif b == "view":
        base = (f"SELECT DISTINCT e.CUST_ID FROM NX_EVL2_PV v JOIN NX_EVL1_SSN e ON e.SSN_ID = v.SSN_ID "
                f"JOIN LX_KRP1_M m ON m.MDL_CD = v.MDL_CD JOIN LX_KRP2_C k ON k.CAT_CD = m.CAT_CD "
                f"WHERE k.CAT_M IN ({cats}) AND v.PV_TS BETWEEN '{_ts(per['from'])}' AND '{_ts(per['to'], True)}'")
    elif b == "cart":
        base = (f"SELECT DISTINCT c.CUST_ID FROM NX_EVL3_CRT c JOIN LX_KRP3_SKU s ON s.SKU_CD = c.SKU_CD "
                f"JOIN LX_KRP1_M m ON m.MDL_CD = s.MDL_CD JOIN LX_KRP2_C k ON k.CAT_CD = m.CAT_CD "
                f"WHERE k.CAT_M IN ({cats}) AND c.ADD_TS BETWEEN '{_ts(per['from'])}' AND '{_ts(per['to'], True)}'")
    else:  # wish
        base = (f"SELECT DISTINCT w.CUST_ID FROM NX_EVL4_WSH w JOIN LX_KRP1_M m ON m.MDL_CD = w.MDL_CD "
                f"JOIN LX_KRP2_C k ON k.CAT_CD = m.CAT_CD "
                f"WHERE k.CAT_M IN ({cats}) AND w.ADD_TS BETWEEN '{_ts(per['from'])}' AND '{_ts(per['to'], True)}'")
    t3 = term(rs, "T03")
    eligible = check_rules.fill(t3.sql, {"{as_of}": config.AS_OF_TS})
    parts = [f"WITH base AS ({base})", f"eligible AS ({eligible})"]
    where = ""
    if p.get("exclude_purchase"):
        e = p["exclude_purchase"]
        cust_x = _cust_id_expr(rs, "hx")
        parts.append(f"excluded AS (SELECT DISTINCT {cust_x} AS CUST_ID FROM LX_HJV1_H hx "
                     f"JOIN LX_HJV1_D dx ON dx.ORD_NO = hx.ORD_NO JOIN LX_KRP3_SKU sx ON sx.SKU_CD = dx.SKU_CD "
                     f"JOIN LX_KRP1_M mx ON mx.MDL_CD = sx.MDL_CD JOIN LX_KRP2_C kx ON kx.CAT_CD = mx.CAT_CD "
                     f"WHERE kx.CAT_M IN ({cats}) AND hx.ORD_DT BETWEEN '{e['from']}' AND '{e['to']}' AND hx.ORD_STAT <> '90')")
        where = "\nWHERE b.CUST_ID NOT IN (SELECT CUST_ID FROM excluded)"
    sql = ",\n".join(parts) + "\nSELECT b.CUST_ID\nFROM base b\nJOIN eligible g ON g.CUST_ID = b.CUST_ID" + where + "\nORDER BY 1"
    notes = list(p["notes"])
    if p.get("exclude_purchase") and p["exclude_purchase"]["from"] < config.ORDER_DATA_FROM:
        notes.append("제외 기간 중 2026-05-01 이전 구매는 데이터가 없어 제외되지 않음 — BA 확인 필요")
    if b == "purchase" and per["from"] < config.ORDER_DATA_FROM:
        notes.append("구매 기간 중 2026-05-01 이전은 데이터 없음")
    return {"sql": sql, "qtype": "push_target",
            "term_versions": {"T02": term(rs, "T02").version, "T03": t3.version},
            "assumptions": notes,
            "scope_notes": ["푸시 수신동의 고객 중 최근 7일 발송 제한(T03)에 걸리지 않은 고객만", "고객 ID만 포함, 이름·연락처 없음"]}
