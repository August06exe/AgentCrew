#!/usr/bin/env python3
"""理财顾问冒烟测试（范式 §10 自测件，照 03-template/tests/smoke.py 改造）：
init → append(dry-run) → append → query → stats → serve → 六工具逐一 selfcheck → delete → 恢复现场。
与模板的两处差异：
  * fake_value 支持 enum 列（finance 首表 accounts.type 是枚举，照抄模板必挂 schema）；
  * serve 断言追加"财计算段"（data.py serve 的理财扩展载荷）。
测试数据全落 tests/_sandbox（ASSISTANT_DATA_DIR 沙箱），首尾各清一次场，绝不触碰真实存档。
"""
import json
import os
import random
import shutil
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = os.path.join(ROOT, "tools")
SANDBOX = os.path.join(HERE, "_sandbox")
ENV = {**os.environ,
       "ASSISTANT_DATA_DIR": os.path.join(SANDBOX, "data"),
       "ASSISTANT_DASHBOARD_DIR": os.path.join(SANDBOX, "dashboard")}

ALL_TOOLS = ["data.py", "ledger.py", "budget.py", "report.py", "kb.py", "import_bills.py"]


def run(tool, *args, expect_ok=True):
    r = subprocess.run([sys.executable, os.path.join(TOOLS, tool), *args],
                       capture_output=True, text=True, encoding="utf-8", cwd=ROOT, env=ENV)
    out = {}
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        pass
    if expect_ok and (r.returncode != 0 or not out.get("ok")):
        raise SystemExit(f"FAIL: {tool} {args} -> rc={r.returncode} out={r.stdout[:300]} err={r.stderr[:300]}")
    return out


def fake_value(col):
    t = col.get("type", "string")
    if col.get("enum"):
        return random.choice(col["enum"])
    if t == "number":
        return round(random.uniform(1, 99), 1)
    if t == "integer":
        return random.randint(1, 10)
    if t == "boolean":
        return True
    if t == "date":
        return datetime.now().strftime("%Y-%m-%d")
    return f"冒烟{random.randint(100, 999)}"


def fake_row(table):
    row = {}
    for c in table.get("columns") or []:
        if c.get("required") or random.random() < 0.5:
            row[c["name"]] = fake_value(c)
    return row


def main() -> int:
    shutil.rmtree(SANDBOX, ignore_errors=True)  # 每轮清场
    manifest = json.load(open(os.path.join(ROOT, "manifest.json"), encoding="utf-8"))
    tables = manifest.get("tables") or []
    if not tables:
        print("SMOKE SKIP（无表声明）")
        return 0
    tname = tables[0]["name"]  # finance = accounts

    run("data.py", "init")
    row = fake_row(tables[0])
    d = run("data.py", "append", tname, "--json", json.dumps(row, ensure_ascii=False), "--dry-run")
    assert d.get("dry_run"), f"dry-run 不应落盘: {d}"
    r1 = run("data.py", "append", tname, "--json", json.dumps(row, ensure_ascii=False))
    run("data.py", "append", tname, "--json", json.dumps(fake_row(tables[0]), ensure_ascii=False))

    req_cols = [c["name"] for c in (tables[0].get("columns") or []) if c.get("required")]
    if req_cols:
        bad_row = {k: v for k, v in row.items() if k not in req_cols}
        bad = run("data.py", "append", tname, "--json", json.dumps(bad_row, ensure_ascii=False),
                  expect_ok=False)
        assert not bad.get("ok"), "schema 校验应拦下缺必填"

    q = run("data.py", "query", tname)
    assert q["count"] >= 2, "应有至少两行"
    run("data.py", "stats", tname)
    sv = run("data.py", "serve")
    assert os.path.isfile(os.path.join(SANDBOX, "dashboard", "data.js")), "serve 应产出 data.js"
    assert os.path.isfile(os.path.join(SANDBOX, "dashboard", "data.json")), "serve 应产出 data.json"
    assert sv.get("finance"), f"serve 应携带财计算段: {sv}"

    # 六个工具逐一 --selfcheck（收编校验契约）
    for tool in ALL_TOOLS:
        run(tool, "--selfcheck")

    rid = r1.get("appended")
    if rid:
        run("data.py", "delete", tname, "--id", rid, "--yes")
        q2 = run("data.py", "query", tname)
        assert q2["count"] == q["count"] - 1, "delete 后应少一行"

    shutil.rmtree(SANDBOX, ignore_errors=True)  # 恢复现场
    print(f"SMOKE OK（表 {tname}，含必填校验/查询/统计/看板+财计算段/六工具自检/删除/清场）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
