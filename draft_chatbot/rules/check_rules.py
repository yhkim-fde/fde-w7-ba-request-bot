"""룰 파일 점검 도구 (Claude 없이 BA가 직접 실행).

사용법
  python3 check_rules.py validate              # 룰 파일 4종을 데이터로 점검
  python3 check_rules.py sql 파일.sql [--type report|push_target]   # SQL 한 개를 checks.csv 규칙으로 검사

옵션
  --data 폴더   마스킹 CSV 폴더 (기본: ../../kit_download/masked/A-db/ 아래 첫 폴더)
  --ddl 폴더    DDL 폴더 (기본: ../../kit_download/ddl/A-schema/DDL)

표준 라이브러리만 사용한다 (설치 필요 없음).
"""
import argparse, csv, glob, os, re, sqlite3, sys

HERE = os.path.dirname(os.path.abspath(__file__))
RULES = os.environ.get("BOT_RULES_DIR", HERE)   # 테스트용 사본 폴더 지정 가능
SAMPLE = {"{as_of}": "2026-11-20 18:00:00", "{from}": "20261001", "{to}": "20261031"}


def fill(sql, values=None):
    """용어 SQL의 자리표시({as_of}, {from}, {to})를 채운다. 점검 때는 SAMPLE 값."""
    for k, v in (values or SAMPLE).items():
        sql = sql.replace(k, v)
    return sql


def read_csv(name):
    with open(os.path.join(RULES, name), newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_db(data_dir, ddl_dir):
    con = sqlite3.connect(":memory:")
    for f in sorted(glob.glob(os.path.join(data_dir, "*.csv"))):
        t = os.path.basename(f)[:-4]
        ddl = os.path.join(ddl_dir, t + ".sql")
        types = dict(re.findall(r"^\s+(\w+)\s+([A-Z]+)", open(ddl).read(), re.M)) if os.path.exists(ddl) else {}
        with open(f, newline="", encoding="utf-8-sig") as fh:
            r = csv.reader(fh)
            cols = next(r)
            num = [types.get(c) in ("BIGINT", "INTEGER", "NUMERIC") for c in cols]
            con.execute(f'CREATE TABLE "{t}" ({",".join(c + (" INTEGER" if n else " TEXT") for c, n in zip(cols, num))})')

            def cv(x, n):
                if x == "":
                    return None
                if not n:
                    return x
                v = float(x)
                return int(v) if v == int(v) else v

            con.executemany(f'INSERT INTO "{t}" VALUES ({",".join("?" * len(cols))})',
                            ([cv(x, n) for x, n in zip(row, num)] for row in r))
    return con


class Report:
    def __init__(self):
        self.rows = []

    def add(self, level, area, msg):
        self.rows.append((level, area, msg))
        mark = {"OK": "  OK ", "WARN": " WARN", "FAIL": " FAIL"}[level]
        print(f"[{mark}] {area}: {msg}")

    @property
    def failed(self):
        return any(l == "FAIL" for l, _, _ in self.rows)


def validate(con, rep):
    data_tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}

    # 1. tables.csv
    tables = read_csv("tables.csv")
    listed = {t["table"] for t in tables}
    for t in tables:
        if t["allowed"] not in ("Y", "N"):
            rep.add("FAIL", "tables.csv", f"{t['table']}: allowed 값은 Y 또는 N")
        if t["allowed"] == "N" and not t["excluded_reason"]:
            rep.add("WARN", "tables.csv", f"{t['table']}: 사용 금지인데 excluded_reason 비어 있음")
        if t["table"] not in data_tables:
            rep.add("WARN", "tables.csv", f"{t['table']}: 데이터에 없는 테이블")
    for t in sorted(data_tables - listed):
        rep.add("FAIL", "tables.csv", f"{t}: 데이터에는 있는데 목록에 없음 (허용 여부를 정해 주세요)")
    rep.add("OK", "tables.csv", f"{len(tables)}개 테이블 점검")

    # 2. terms.csv — SQL 실행 + 순매출 대시보드 재현
    for t in read_csv("terms.csv"):
        try:
            n = len(con.execute(fill(t["sql"])).fetchall())
            level = "OK" if t["confirmed_by"] else "WARN"
            note = "" if t["confirmed_by"] else " · confirmed_by 비어 있음(BA 확정 필요)"
            rep.add(level, "terms.csv", f"{t['term_id']} {t['term']}: 실행 성공 {n}행{note}")
        except sqlite3.Error as e:
            rep.add("FAIL", "terms.csv", f"{t['term_id']} {t['term']}: SQL 오류 - {e}")
            continue
        if t["term_id"] == "T01":
            q = f"""select count(*), sum(a.NET_AMT = t.NET_AMT) from NX_AGG1_DSL a
                    left join ({fill(t['sql'])}) t on a.DT=t.DT and a.CHNL_CD=t.CHNL_CD and a.CAT_CD=t.CAT_CD"""
            total, same = con.execute(q).fetchone()
            level = "OK" if total == same else "FAIL"
            rep.add(level, "terms.csv", f"T01 순매출 정의로 대시보드 집계 재현: {same}/{total}칸 일치")

    # 3. joins.csv — 데이터로 관계 확인
    for j in read_csv("joins.csv"):
        pairs = [k.split("=") for k in j["keys"].split(";")]
        if any("||" in a or "||" in b for a, b in pairs):
            a, b = pairs[0]
            q = f"select count(*), sum(exists(select 1 from {j['left']} l where {a.replace('MBR_NO','l.MBR_NO')} = r.{b})) from {j['right']} r"
            total, hit = con.execute(q).fetchone()
            rep.add("OK" if total == hit else "WARN", "joins.csv", f"{j['left']} ↔ {j['right']} ({j['keys']}): {hit}/{total} 연결")
            continue
        lcols = ",".join(a for a, _ in pairs)
        rcols = ",".join(b for _, b in pairs)
        lmatch = " and ".join(f"x.{a}=y.{b}" for a, b in pairs)
        rmatch = " and ".join(f"y.{b}=x.{a}" for a, b in pairs)
        try:
            # 같은 키를 가진 행이 오른쪽(왼쪽)에 최대 몇 개인가 — 상대편에 실제로 있는 키만
            max_r = con.execute(f"select max(c) from (select count(*) c from {j['right']} y where exists(select 1 from {j['left']} x where {lmatch}) group by {rcols})").fetchone()[0] or 0
            max_l = con.execute(f"select max(c) from (select count(*) c from {j['left']} x where exists(select 1 from {j['right']} y where {rmatch}) group by {lcols})").fetchone()[0] or 0
        except sqlite3.Error as e:
            rep.add("FAIL", "joins.csv", f"{j['left']} ↔ {j['right']}: {e}")
            continue
        actual = f"{'1' if max_l <= 1 else 'N'}:{'1' if max_r <= 1 else 'N'}"
        expected = j["cardinality"]
        ok = all(e == "N" or a == "1" for e, a in zip(expected.split(":"), actual.split(":")))
        rep.add("OK" if ok else "WARN", "joins.csv", f"{j['left']} ↔ {j['right']}: 적어 둔 관계 {expected}, 데이터 {actual}")

    # 4. 공통코드 중복
    for grp, cd, n in con.execute("select CD_GRP, CD, count(*) from LX_ZCM_CD group by 1,2 having count(*)>1"):
        rep.add("WARN", "LX_ZCM_CD", f"{grp}.{cd} 코드가 {n}개 이름으로 등록됨 (조인 시 중복)")

    # 5. 규칙 파일 형식
    for r in read_csv("checks.csv"):
        if r["enabled"] not in ("Y", "N") or r["severity"] not in ("block", "warn") \
                or r["applies_to"] not in ("all", "report", "push_target", "adhoc"):
            rep.add("FAIL", "checks.csv", f"{r['rule_id']}: enabled(Y/N)·severity(block/warn)·applies_to 값 확인")
        try:
            if r["pattern"]:
                re.compile(r["pattern"], re.I)
        except re.error as e:
            rep.add("FAIL", "checks.csv", f"{r['rule_id']}: pattern 오류 - {e}")
    for j in read_csv("joins.csv"):
        if j["cardinality"] not in ("1:1", "1:N", "N:1", "N:N"):
            rep.add("FAIL", "joins.csv", f"{j['left']} ↔ {j['right']}: cardinality는 1:1·1:N·N:1·N:N 중 하나")
    for t in read_csv("terms.csv"):
        if not (t["version"] or "1").isdigit():
            rep.add("FAIL", "terms.csv", f"{t['term_id']}: version은 숫자")
    rep.add("OK", "checks.csv", "규칙 형식 점검")


def evaluate_sql(sql, qtype, tables=None, checks=None):
    """checks.csv 규칙으로 SQL 한 개 검사 → [(rule_id, 'FAIL'|'WARN', 메시지)].
    R01·R02·R04·R09는 전용 로직, 나머지는 pattern이 나오면 표시. 챗봇도 이 함수를 쓴다."""
    tables = tables or {t["table"]: t for t in read_csv("tables.csv")}
    checks = checks or read_csv("checks.csv")
    used = set(re.findall(r"\b([A-Z]{2,3}_[A-Z0-9_]+)\b", sql.upper())) & set(tables)
    flat = re.sub(r"\s+", " ", sql)
    out = []
    for r in checks:
        if r["enabled"] != "Y" or r["applies_to"] not in ("all", qtype):
            continue
        rid, pat = r["rule_id"], r["pattern"]
        level = "FAIL" if r["severity"] == "block" else "WARN"
        msg = r["description"]
        hit = False
        if rid == "R01":
            hit = not re.search(pat, flat, re.I)
        elif rid == "R02":
            bad = sorted(t for t in used if tables[t]["allowed"] != "Y")
            hit = bool(bad)
            msg = f"{msg}: {', '.join(bad)}" if bad else msg
        elif rid == "R04":
            hit = bool(re.search(r"\bFR_DT\b", flat, re.I)) and not re.search(r"\bTO_DT\b", flat, re.I) \
                or bool(re.search(r"\bFR_TS\b", flat, re.I)) and not re.search(r"\bTO_TS\b", flat, re.I)
        elif rid == "R09":
            hit = not ("NX_DNS5_AGR" in used and ("NX_QPS1_SND" in used or "NX_QPS9_LMT" in used))
        elif pat:
            hit = bool(re.search(pat, flat, re.I))
        if hit:
            out.append((rid, level, msg))
    return out


def check_sql(sql, qtype, rep):
    for rid, level, msg in evaluate_sql(sql, qtype):
        rep.add(level, rid, msg)
    if not rep.rows:
        rep.add("OK", "SQL", "적용된 규칙 위반 없음")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["validate", "sql"])
    p.add_argument("file", nargs="?")
    p.add_argument("--type", default="adhoc", choices=["adhoc", "report", "push_target"])
    p.add_argument("--data")
    p.add_argument("--ddl", default=os.path.join(HERE, "../../kit_download/ddl/A-schema/DDL"))
    a = p.parse_args()
    rep = Report()
    if a.cmd == "validate":
        data = a.data or sorted(glob.glob(os.path.join(HERE, "../../kit_download/masked/A-db/*/")))[0]
        validate(load_db(data, a.ddl), rep)
    else:
        check_sql(open(a.file, encoding="utf-8").read(), a.type, rep)
    fails = sum(l == "FAIL" for l, _, _ in rep.rows)
    warns = sum(l == "WARN" for l, _, _ in rep.rows)
    print(f"\n결과: 실패 {fails} · 주의 {warns}")
    sys.exit(1 if rep.failed else 0)


if __name__ == "__main__":
    main()
