#!/usr/bin/env python3
"""通用冒烟测试（范式 §10 自测件）：读 manifest 自动造合法行。
init → append(dry-run) → append → query → stats → serve → selfcheck → delete → 恢复现场。"""
import json
import os
import random
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(ROOT, "tools", "data.py")


SANDBOX = os.path.join(HERE, "_sandbox")
ENV = {**os.environ,
       "ASSISTANT_DATA_DIR": os.path.join(SANDBOX, "data"),
       "ASSISTANT_DASHBOARD_DIR": os.path.join(SANDBOX, "dashboard")}


def run(*args, expect_ok=True):
    r = subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True,
                       encoding="utf-8", cwd=ROOT, env=ENV)
    out = {}
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        pass
    if expect_ok and (r.returncode != 0 or not out.get("ok")):
        raise SystemExit(f"FAIL: {args} -> rc={r.returncode} out={r.stdout[:300]} err={r.stderr[:300]}")
    return out


def fake_value(col):
    t = col.get("type", "string")
    if t == "number":
        return round(random.uniform(1, 99), 1)
    if t == "integer":
        return random.randint(1, 10)
    if t == "boolean":
        return True
    if t == "date":
        return datetime.now().strftime("%Y-%m-%d")
    return f"冒烟{random.randint(100,999)}"


def fake_row(table):
    row = {}
    for c in table.get("columns") or []:
        if c.get("required") or random.random() < 0.5:
            row[c["name"]] = fake_value(c)
    return row


def main() -> int:
    import shutil
    shutil.rmtree(SANDBOX, ignore_errors=True)  # 每轮清场
    manifest = json.load(open(os.path.join(ROOT, "manifest.json"), encoding="utf-8"))
    tables = manifest.get("tables") or []
    if not tables:
        print("SMOKE SKIP（无表声明）")
        return 0
    tname = tables[0]["name"]

    run("init")
    row = fake_row(tables[0])
    run("append", tname, "--json", json.dumps(row, ensure_ascii=False), "--dry-run")
    r1 = run("append", tname, "--json", json.dumps(row, ensure_ascii=False))
    run("append", tname, "--json", json.dumps(fake_row(tables[0]), ensure_ascii=False))

    req_cols = [c["name"] for c in (tables[0].get("columns") or []) if c.get("required")]
    if req_cols:
        bad_row = {k: v for k, v in row.items() if k not in req_cols}
        bad = run("append", tname, "--json", json.dumps(bad_row, ensure_ascii=False), expect_ok=False)
        assert not bad.get("ok"), "schema 校验应拦下缺必填"

    q = run("query", tname)
    assert q["count"] >= 2, "应有至少两行"
    run("stats", tname)
    run("serve")
    assert os.path.isfile(os.path.join(SANDBOX, "dashboard", "data.js")), "serve 应产出 data.js"
    run("selfcheck")
    rid = r1.get("appended")
    if rid:
        run("delete", tname, "--id", rid, "--yes")
    print(f"SMOKE OK（表 {tname}，含必填校验/查询/统计/看板/自检/删除）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
