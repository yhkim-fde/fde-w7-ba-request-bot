"""rules/*.csv → RuleSet. BA가 CSV를 고치면 다음 단계부터 반영된다.
버전 고정(검토 H3): 노드마다 current_version() 과 비교해 바뀌었으면 다시 고정·재검사하고 이력에 남긴다.
CSV 형식 오류는 RuleLoadError → 그래프가 rule_invalid 로 BA에게 넘긴다."""
import csv
import hashlib
import importlib.util
import io
import json
import os
from contextlib import redirect_stdout
from datetime import datetime

from schema import BusinessTerm, CheckRule, JoinPath, RuleSet, TableMeta

from . import config

FILES = ["tables.csv", "terms.csv", "joins.csv", "checks.csv", "vocab.csv"]


class RuleLoadError(Exception):
    pass


def _load_check_rules_module():
    spec = importlib.util.spec_from_file_location("check_rules", os.path.join(config.BASE, "rules", "check_rules.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


check_rules = _load_check_rules_module()


def read(name):
    with open(os.path.join(config.RULES_DIR, name), newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def current_version() -> str:
    h = hashlib.sha256()
    for n in FILES:
        with open(os.path.join(config.RULES_DIR, n), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:12]


def _validate(version: str) -> str:
    cache_path = os.path.join(config.VAR_DIR, "validate_cache.json")
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    if version in cache:
        return cache[version]
    rep = check_rules.Report()
    with redirect_stdout(io.StringIO()):
        check_rules.validate(check_rules.load_db(config.require_data_dir(), config.DDL_DIR), rep)
    levels = {l for l, _, _ in rep.rows}
    result = "fail" if "FAIL" in levels else "warn" if "WARN" in levels else "ok"
    cache[version] = result
    json.dump(cache, open(cache_path, "w"))
    return result


def _status(t):
    if t["confirmed_by"]:
        return "confirmed"
    s = t["status"]
    if "재현" in s or "확인됨" in s or "지킴" in s:
        return "reproduced"
    if "미확정" in s:
        return "unresolved"
    return "draft"


_cache: dict[str, RuleSet] = {}


def load() -> RuleSet:
    """현재 파일을 읽어 RuleSet 으로. 형식 오류는 RuleLoadError."""
    version = current_version()
    if version in _cache:
        return _cache[version]
    if os.environ.get("BOT_RULES_DIR"):
        os.environ["BOT_RULES_DIR"] = config.RULES_DIR      # check_rules 도 같은 폴더를 보게
        check_rules.RULES = config.RULES_DIR
    try:
        tables = [TableMeta(
            table=t["table"], domain=t["domain"], allowed=t["allowed"] == "Y",
            id_system=t["id_system"] or "none",
            id_transform="'C' || MBR_NO" if t["id_system"] == "CUST_ID" else None,
            excluded_reason=t["excluded_reason"] or None, is_history=t["is_history"] == "Y",
            refresh=t["refresh"] or None, note=t["note"]) for t in read("tables.csv")]
        terms = [BusinessTerm(
            term_id=t["term_id"], term=t["term"], definition=t["definition"], sql=t["sql"],
            source_tables=t["source_tables"].split(";"), status=_status(t), evidence=t["status"],
            confirmed_by=t["confirmed_by"] or None, version=int(t["version"] or 1)) for t in read("terms.csv")]
        joins = [JoinPath(
            left=j["left"], right=j["right"], keys=[k.split("=") for k in j["keys"].split(";")],
            cardinality=j["cardinality"],
            verified_by="dashboard" if j["verified_by"].startswith("dashboard") else
            j["verified_by"] if j["verified_by"] in ("data", "ba") else "unverified",
            note=j["note"]) for j in read("joins.csv")]
        checks = [CheckRule(
            rule_id=c["rule_id"], enabled=c["enabled"] == "Y", severity=c["severity"],
            description=c["description"], pattern=c["pattern"] or None, applies_to=c["applies_to"])
            for c in read("checks.csv")]
        validate_result = _validate(version)
    except (ValueError, KeyError, OSError) as e:      # pydantic ValidationError 는 ValueError 하위
        raise RuleLoadError(f"규칙 파일을 읽지 못했습니다: {str(e)[:300]}") from e
    rs = RuleSet(version=version, tables=tables, terms=terms, joins=joins, checks=checks,
                 validate_result=validate_result, loaded_at=datetime.now())
    _cache[version] = rs
    return rs


def term(rs: RuleSet, term_id: str) -> BusinessTerm:
    return next(t for t in rs.terms if t.term_id == term_id)


def allowed_tables(rs: RuleSet) -> set:
    return {t.table for t in rs.tables if t.allowed}


def run_checks(rs: RuleSet, sql: str, qtype: str):
    """고정된 RuleSet 의 규칙으로 검사 (check_rules.evaluate_sql 재사용)."""
    tables = {t.table: {"allowed": "Y" if t.allowed else "N"} for t in rs.tables}
    checks = [{"rule_id": c.rule_id, "enabled": "Y" if c.enabled else "N", "severity": c.severity,
               "description": c.description, "pattern": c.pattern or "", "applies_to": c.applies_to}
              for c in rs.checks]
    return check_rules.evaluate_sql(sql, qtype, tables, checks)


def vocab_rows():
    return read("vocab.csv")
