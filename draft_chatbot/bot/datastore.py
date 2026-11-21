"""Redshift 대역: 마스킹 추출본을 사내(로컬) SQLite로 적재하고 읽기 전용으로 실행한다.
- 결과 행은 var/results/*.csv 에만 저장. 그래프 상태·로그·언어 모델에는 행 수·컬럼명만 (가정 A1)
- 코드 고정 가드(검토 H4): 단일 SELECT 문, 허용 테이블만 읽기(set_authorizer), 실행 시간·행 수 상한
"""
import csv
import os
import re
import sqlite3
import time

from . import config
from .ruleset import check_rules


class GuardError(Exception):
    pass


def ensure_db():
    if os.path.exists(config.DATA_DB):
        return
    src = config.require_data_dir()
    os.makedirs(os.path.dirname(config.DATA_DB), exist_ok=True)   # 새로 받은 저장소에는 var/ 가 없음
    mem = check_rules.load_db(src, config.DDL_DIR)
    mem.commit()                      # load_db 는 트랜잭션을 열어 둔 채 반환한다 (열려 있으면 내보내기가 멈춤)
    tmp = config.DATA_DB + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    mem.execute("VACUUM INTO ?", (tmp,))
    mem.close()
    os.replace(tmp, config.DATA_DB)


def static_guard(sql: str):
    """CSV 규칙과 별개로 항상 적용되는 검사."""
    body = sql.strip().rstrip(";")
    if ";" in body:
        raise GuardError("문장은 하나만 허용합니다")
    if not re.match(r"^\s*(with|select)\b", body, re.I):
        raise GuardError("SELECT 문만 허용합니다")
    if re.search(r"\b(insert|update|delete|drop|alter|create|attach|pragma|vacuum|replace)\b", body, re.I):
        raise GuardError("데이터 변경·설정 명령은 허용하지 않습니다")
    return body


def _authorizer(allowed: set):
    def auth(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_SELECT or action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            # CTE·서브쿼리 별칭은 arg1 이 실제 테이블일 때만 들어온다
            return sqlite3.SQLITE_OK if arg1 in allowed else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY
    return auth


def run(sql: str, draft_id: str, allowed_tables: set):
    body = static_guard(sql)
    ensure_db()
    con = sqlite3.connect(f"file:{config.DATA_DB}?mode=ro", uri=True)
    start = time.monotonic()
    con.set_progress_handler(lambda: 1 if time.monotonic() - start > config.EXEC_TIMEOUT_SEC else 0, 10_000)
    con.set_authorizer(_authorizer(allowed_tables))
    try:
        cur = con.execute(body)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(config.EXEC_MAX_ROWS + 1)
    except sqlite3.DatabaseError as e:
        raise GuardError(f"실행 실패: {e}") from e
    finally:
        con.close()
    truncated = len(rows) > config.EXEC_MAX_ROWS
    rows = rows[: config.EXEC_MAX_ROWS]
    path = os.path.join(config.RESULTS_DIR, f"{draft_id}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    return {"row_count": len(rows), "columns": cols, "result_ref": path, "truncated": truncated}


def read_result(path: str, n: int = 10):
    """BA 검토 화면용 미리보기. 결과 파일에서 직접 읽는다(상태에 행을 넣지 않음)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        r = csv.reader(f)
        head = next(r)
        return head, [row for _, row in zip(range(n), r)]


def vocab():
    """카테고리·채널 마스터. 사내에서만 사용."""
    ensure_db()
    con = sqlite3.connect(f"file:{config.DATA_DB}?mode=ro", uri=True)
    try:
        cats = con.execute("select CAT_L, CAT_M, CAT_S from LX_KRP2_C").fetchall()
        chnl = [r[0] for r in con.execute("select CHNL_NM from LX_ZCM_CHNL")]
    finally:
        con.close()
    return cats, chnl
