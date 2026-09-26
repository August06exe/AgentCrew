#!/usr/bin/env python3
"""data.py — 子助理通用数据工具（范式 §6 标准实现，仅标准库）。

职责边界：只做确定性校验与落盘，不做任何"理解"。AI 负责决定记什么，本工具保证记得对。

命令：
  init                                  按 manifest.tables 建空表文件与 config.json（已存在则跳过）
  append <table> --json '<json>'        写入一行（schema 校验；--dry-run 只验不写）
  query  <table> [--last N] [--date D] [--where k=v]...
  stats  <table>                        行数/时间跨度/字段填充率
  delete <table> --id <row-id> --yes    移入 data/trash/（可人工恢复）
  update <table> --id <row-id> --set k=v --yes
  serve                                 重建 dashboard/data.json + data.js（看板取数）
  selfcheck                             自检（收编校验用）

理财扩展（agentcrew.finance）：serve 在范式标准载荷之外追加 `finance` 计算段——
账户余额表、净资产、本月收支汇总、近 6 个月现金流、近期流水（看板取数形状）。
推导逻辑唯一真相在 ledger.py（延迟导入，避免两处算账）；ledger 缺席时降级为
`finance._unavailable` 并保留原始表数据，看板仍可渲染。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
# 模板/实例中 tools/ 与仓库 05-scripts 的 agentcrew_lib 二选一：优先用自带轻量实现，保证自包含。
# 这里刻意不 import agentcrew_lib：助理必须能被整包拷走独立运行（范式公理1）。


def jout(obj: dict, code: int = 0) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return code


def jfail(error: str, extra: dict | None = None) -> int:
    return jout({"ok": False, "error": error, **(extra or {})}, code=1)


def agent_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def save_dir() -> str:
    """存档切片根（自包含解析：优先级 沙箱env > 向上找实例根 > lite 自带 _save）。"""
    env = os.environ.get("ASSISTANT_DATA_DIR")
    if env:
        return os.path.abspath(env)
    agent = agent_root()
    d = os.path.abspath(agent)
    passed = False
    while True:
        if os.path.basename(d) == "_standalone":
            passed = True
        if (not passed
                and os.path.isfile(os.path.join(d, "AGENTS.md"))
                and os.path.isdir(os.path.join(d, "01-master"))
                and os.path.isfile(os.path.join(d, "00-docs", "PROTOCOL.md"))
                and os.path.abspath(d) != os.path.abspath(agent)):
            return os.path.join(d, "_save", "agents", os.path.basename(os.path.abspath(agent)))
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.join(agent, "_save")
        d = parent


def data_dir() -> str:
    env = os.environ.get("ASSISTANT_DATA_DIR")  # 沙箱/外置：直接给数据目录本身（历史语义）
    if env:
        return os.path.abspath(env)
    return os.path.join(save_dir(), "data")


def dashboard_out_dir() -> str:
    env = os.environ.get("ASSISTANT_DASHBOARD_DIR")
    if env:
        return os.path.abspath(env)
    return os.path.join(save_dir(), "dashboard")


def manifest() -> dict:
    with open(os.path.join(agent_root(), "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def table_schema(tables: list[dict], name: str) -> dict | None:
    for t in tables:
        if t.get("name") == name:
            return t
    return None


def table_path(name: str) -> str:
    return os.path.join(data_dir(), f"{name}.jsonl")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def row_id() -> str:
    import random
    return f"r-{datetime.now().strftime('%Y%m%d%H%M%S')}-{random.randint(0, 0xffff):04x}"


def norm_date(s) -> str | None:
    """宽容日期归一：'2026-8-5' → '2026-08-05'；解析失败返回 None（调用方决定拒收或进 suspects）。

    写入侧（ledger record/reverse/assert --date）先归一再落库，杜绝非补零日期
    在按月/按日推导（date[:7] 比较、as_of 字符串比较）里静默丢失；
    读取侧（月聚合）用它救回历史非补零坏行。
    """
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        return None


def atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def read_rows(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    out.append({"_corrupt": line})
    return out


def append_row(path: str, row: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def check_value(col: dict, value, where: str) -> str | None:
    t = col.get("type", "string")
    name = col.get("name", "?")
    if value is None:
        return None if not col.get("required") else f"{where}: 必填字段 {name} 缺失"
    if t == "number" and not isinstance(value, (int, float)) or isinstance(value, bool) and t == "number":
        return f"{where}: {name} 应为 number"
    if t == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        return f"{where}: {name} 应为 integer"
    if t == "boolean" and not isinstance(value, bool):
        return f"{where}: {name} 应为 boolean"
    if t == "string" and not isinstance(value, str):
        return f"{where}: {name} 应为 string"
    if t == "date":
        if not isinstance(value, str):
            return f"{where}: {name} 应为 ISO 日期字符串"
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return f"{where}: {name} 不是合法 ISO 日期: {value}"
    if col.get("enum") and value not in col["enum"]:
        return f"{where}: {name} 必须是 {col['enum']} 之一，现在是 {value!r}"
    return None


def validate_row(table: dict, row: dict) -> list[str]:
    errs = []
    cols = table.get("columns") or []
    for col in cols:
        e = check_value(col, row.get(col["name"]), table["name"])
        if e:
            errs.append(e)
    if "recorded_at" in row:
        try:
            datetime.fromisoformat(str(row["recorded_at"]))
        except ValueError:
            errs.append("recorded_at 不是合法 ISO 日期")
    return errs


# ---------- 命令实现 ----------

def _rel_to_root(path: str) -> str:
    try:
        return os.path.relpath(path, agent_root())
    except ValueError:  # 跨盘符（存档外置到别的盘）时给绝对路径
        return path

def cmd_init(_a) -> int:
    m = manifest()
    os.makedirs(data_dir(), exist_ok=True)
    created = []
    for t in m.get("tables") or []:
        tp = table_path(t["name"])
        if not os.path.isfile(tp):
            open(tp, "w", encoding="utf-8").close()
            created.append(t["name"])
    cfg = os.path.join(data_dir(), "config.json")
    if not os.path.isfile(cfg):
        atomic_write(cfg, json.dumps({
            "_comment": "助理配置（改参数=改数据，不改代码）。字段随助理领域自行扩展。",
            "updated_at": now_iso(),
        }, ensure_ascii=False, indent=2) + "\n")
        created.append("config.json")
    return jout({"ok": True, "created": created or "全部已存在"})


def cmd_append(a) -> int:
    m = manifest()
    try:
        row = json.loads(a.json)
    except json.JSONDecodeError as e:
        return jfail(f"--json 解析失败：{e}")
    t = table_schema(m.get("tables") or [], a.table)
    if not t:
        return jfail(f"manifest 未声明表 {a.table}（先改 manifest 再记数据）")
    row.setdefault("_id", row_id())
    row.setdefault("created_at", now_iso())
    row.setdefault("recorded_at", now_iso()[:10])
    errs = validate_row(t, row)
    if errs:
        return jfail("schema 校验失败", {"errors": errs})
    if a.dry_run:
        return jout({"ok": True, "dry_run": True, "would_append": row})
    append_row(table_path(a.table), row)
    return jout({"ok": True, "appended": row["_id"], "table": a.table})


def _match(row: dict, kv: str) -> bool:
    if "=" not in kv:
        return False
    k, v = kv.split("=", 1)
    return str(row.get(k)) == v


def cmd_query(a) -> int:
    rows = read_rows(table_path(a.table))
    if a.date:
        rows = [r for r in rows if str(r.get("recorded_at", ""))[:10] == a.date]
    for kv in a.where or []:
        rows = [r for r in rows if _match(r, kv)]
    if a.last:
        rows = rows[-a.last:]
    return jout({"ok": True, "table": a.table, "count": len(rows), "rows": rows})


def cmd_stats(a) -> int:
    rows = [r for r in read_rows(table_path(a.table)) if not r.get("_corrupt") and not r.get("_derived")]
    if not rows:
        return jout({"ok": True, "table": a.table, "count": 0})
    dates = sorted(str(r.get("recorded_at", ""))[:10] for r in rows if r.get("recorded_at"))
    fields = {c.get("name") for t in (manifest().get("tables") or []) if t["name"] == a.table
              for c in (t.get("columns") or [])}
    fill = {f: round(sum(1 for r in rows if r.get(f) is not None) / len(rows), 3) for f in sorted(fields)}
    return jout({"ok": True, "table": a.table, "count": len(rows),
                 "date_range": [dates[0], dates[-1]] if dates else None, "fill_rate": fill})


def cmd_delete(a) -> int:
    if not a.yes:
        return jfail("删除需 --yes（数据先移 trash，可人工恢复）")
    tp = table_path(a.table)
    rows = read_rows(tp)
    keep = [r for r in rows if r.get("_id") != a.id]
    removed = [r for r in rows if r.get("_id") == a.id]
    if not removed:
        return jfail(f"找不到 _id={a.id}")
    trash = os.path.join(data_dir(), "trash", f"{a.table}-{datetime.now():%Y%m%d}.jsonl")
    for r in removed:
        r["_deleted_at"] = now_iso()
        append_row(trash, r)
    atomic_write(tp, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep))
    return jout({"ok": True, "deleted": a.id, "trash": _rel_to_root(trash)})


def cmd_update(a) -> int:
    if not a.yes:
        return jfail("update 需 --yes（旧行先移 trash，可人工恢复）")
    m = manifest()
    t = table_schema(m.get("tables") or [], a.table)
    if not t:
        return jfail(f"manifest 未声明表 {a.table}")
    tp = table_path(a.table)
    rows = read_rows(tp)
    changes = {}
    for kv in a.set or []:
        k, _, v = kv.partition("=")
        try:
            changes[k.strip()] = json.loads(v)
        except json.JSONDecodeError:
            changes[k.strip()] = v
    hit = None
    for i, r in enumerate(rows):
        if r.get("_id") == a.id:
            hit = i
            break
    if hit is None:
        return jfail(f"找不到 _id={a.id}")
    old = rows[hit]
    new_row = {**old, **changes}
    errs = validate_row(t, new_row)
    if errs:
        return jfail("schema 校验失败", {"errors": errs})
    trash = os.path.join(data_dir(), "trash", f"{a.table}-{datetime.now():%Y%m%d}.jsonl")
    old["_superseded_at"] = now_iso()
    os.makedirs(os.path.dirname(trash), exist_ok=True)
    with open(trash, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(old, ensure_ascii=False) + "\n")
    rows[hit] = new_row
    atomic_write(tp, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return jout({"ok": True, "updated": a.id, "changed": list(changes), "trash": _rel_to_root(trash)})


def _finance_payload() -> dict:
    """理财计算段：余额表/净资产/本月收支/近6月现金流/近期流水。

    推导唯一真相在 ledger.py（它顶层 import data，这里延迟导入避免环）。
    任何失败都降级为 `_unavailable`，原始表数据照常输出，看板不空窗。
    """
    try:
        import ledger  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        return {"_unavailable": f"ledger.py 无法导入，财计算段缺失：{e}"}
    try:
        return ledger.serve_snapshot()
    except Exception as e:  # noqa: BLE001
        return {"_unavailable": f"账目推导失败：{e}"}


def cmd_serve(_a) -> int:
    m = manifest()
    payload = {"agent": {"id": m["id"], "name": m["name"], "version": m["version"]},
               "generated_at": now_iso(), "tables": {}}
    for t in m.get("tables") or []:
        rows = [r for r in read_rows(table_path(t["name"])) if not r.get("_corrupt")]
        payload["tables"][t["name"]] = rows
    cfgp = os.path.join(data_dir(), "config.json")
    payload["config"] = json.load(open(cfgp, encoding="utf-8")) if os.path.isfile(cfgp) else {}
    payload["finance"] = _finance_payload()
    dash = dashboard_out_dir()
    os.makedirs(dash, exist_ok=True)
    atomic_write(os.path.join(dash, "data.json"), json.dumps(payload, ensure_ascii=False, indent=2))
    atomic_write(os.path.join(dash, "data.js"),
                 "window.DASHBOARD_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n")
    fin = payload.get("finance") or {}
    return jout({"ok": True, "written": ["dashboard/data.json", "dashboard/data.js"],
                 "tables": {k: len(v) for k, v in payload["tables"].items()},
                 "finance": {k: (len(v) if isinstance(v, list) else "✓")
                             for k, v in fin.items() if not str(k).startswith("_")}})


def cmd_selfcheck(_a) -> int:
    m = manifest()
    root = agent_root()
    problems = []
    if not os.path.isdir(data_dir()):
        problems.append("data/ 不存在")
    # 写读删一轮（不动真实表：用 _selfcheck 临时表）
    tmp_tbl = table_path("_selfcheck")
    try:
        append_row(tmp_tbl, {"_id": "t-selfcheck", "created_at": now_iso(), "_note": "selfcheck"})
        rows = read_rows(tmp_tbl)
        if not any(r.get("_id") == "t-selfcheck" for r in rows):
            problems.append("写入读回失败")
    finally:
        if os.path.isfile(tmp_tbl):
            os.remove(tmp_tbl)
    for t in m.get("tables") or []:
        for r in read_rows(table_path(t["name"])):
            if r.get("_corrupt"):
                problems.append(f"表 {t['name']} 有损坏行")
                break
    # 理财计算段：serve 依赖 ledger.py（延迟导入），导入失败在此暴露
    try:
        import ledger  # noqa: F401, PLC0415
    except Exception as e:  # noqa: BLE001
        problems.append(f"ledger.py 无法导入（serve 财计算段缺失）：{e}")
    if problems:
        return jout({"ok": False, "error": "；".join(problems)}, code=1)
    return jout({"ok": True, "agent": m["id"], "version": m["version"]})


def main() -> int:
    ap = argparse.ArgumentParser(description="子助理通用数据工具")
    ap.add_argument("--selfcheck", action="store_true", help="自检（收编校验契约，等价于子命令 selfcheck）")
    sub = ap.add_subparsers(dest="cmd", required=False)

    sub.add_parser("init")
    p_app = sub.add_parser("append")
    p_app.add_argument("table")
    p_app.add_argument("--json", required=True)
    p_app.add_argument("--dry-run", action="store_true")
    p_q = sub.add_parser("query")
    p_q.add_argument("table")
    p_q.add_argument("--last", type=int)
    p_q.add_argument("--date")
    p_q.add_argument("--where", nargs="*")
    p_s = sub.add_parser("stats")
    p_s.add_argument("table")
    p_d = sub.add_parser("delete")
    p_d.add_argument("table")
    p_d.add_argument("--id", required=True)
    p_d.add_argument("--yes", action="store_true")
    p_u = sub.add_parser("update")
    p_u.add_argument("table")
    p_u.add_argument("--id", required=True)
    p_u.add_argument("--set", nargs="+", help="k=v，值尽量 JSON")
    p_u.add_argument("--yes", action="store_true")
    sub.add_parser("serve")
    sub.add_parser("selfcheck")

    a = ap.parse_args()
    if a.selfcheck:
        return cmd_selfcheck(a)
    if not a.cmd:
        ap.print_help()
        return jout({"ok": False, "error": "缺少子命令（或用 --selfcheck）"}, code=2)
    return {
        "init": cmd_init, "append": cmd_append, "query": cmd_query,
        "stats": cmd_stats, "delete": cmd_delete, "update": cmd_update, "serve": cmd_serve,
        "selfcheck": cmd_selfcheck,
    }[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
