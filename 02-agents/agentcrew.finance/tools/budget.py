#!/usr/bin/env python3
"""budget.py — 月度预算：设额度、查执行（仅标准库）。

预算 = 每月每支出科目一行（manifest.tables 的 budgets 表）；类别必须是 accounts 里
type=expense 的支出科目（与设计稿 §4"类别即会计科目"一致）。

执行口径：某类本月花费 = 该支出科目的**净流入**（流入 − 流出，由流水当场推导，
余额永不存死数）。净流入天然消化冲正/退款——原单 32、冲正 32，本月该类花费归 0。

命令：
  set --month YYYY-MM --category 支出·餐饮 --amount 800 [--dry-run]
      （upsert：同月同类已有预算则替换，旧行移 data/trash/ 可人工恢复）
  list [--month YYYY-MM]
  check [--month YYYY-MM]      缺省本月：各类别执行率与超支标记
  selfcheck
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data as D  # noqa: E402  （存档解析器与通用读写，范式 §5/§6）

EPS = 0.005  # 金额比对：分以下视为相等（与 ledger.py 同阈值）


# ---------- 月份小工具（report.py 复用） ----------

def parse_month(s: str) -> str:
    """'2026-9' / '2026-09' → 归一化 'YYYY-MM'；非法抛 ValueError。"""
    dt = datetime.strptime(str(s).strip(), "%Y-%m")
    return f"{dt.year:04d}-{dt.month:02d}"


def prev_month(mkey: str) -> str:
    y, m = int(mkey[:4]), int(mkey[5:7])
    m -= 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y:04d}-{m:02d}"


def month_last_day(mkey: str) -> str:
    import calendar
    y, m = int(mkey[:4]), int(mkey[5:7])
    return f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


# ---------- 读取与推导 ----------

def load_budgets() -> list[dict]:
    return [r for r in D.read_rows(D.table_path("budgets")) if not r.get("_corrupt")]


def month_net_by_account(txns: list[dict], mkey: str) -> dict[str, float]:
    """某月每个账户的净流入（流入 − 流出）；坏行（金额非数值/缺方向/日期不可解析）跳过。

    纯函数：selfcheck 用它验数学。支出科目净流入=净花费，收入科目取负即净收入。
    日期经 D.norm_date 宽容归一：历史非补零行（如 '2026-8-5'）照常计入当月，
    不在聚合中静默消失（与 ledger.month_net_agg 同滤，D1③）。
    """
    nets: dict[str, float] = {}
    for t in txns:
        nd = D.norm_date(t.get("date"))
        if nd is None or nd[:7] != mkey:
            continue
        f, to, amt = t.get("from_account"), t.get("to_account"), t.get("amount")
        if not isinstance(amt, (int, float)) or isinstance(amt, bool) or not f or not to:
            continue
        nets[to] = nets.get(to, 0.0) + float(amt)
        nets[f] = nets.get(f, 0.0) - float(amt)
    return {k: round(v, 2) for k, v in nets.items()}


def compute_execution(accounts: list[dict], txns: list[dict],
                      budget_rows: list[dict], mkey: str) -> dict:
    """预算执行（纯函数）：每个"有预算或有花费"的支出科目一行。

    返回 {month, rows:[{category,budget,spent,remaining,used_pct,overspent,budgeted}],
          totals:{budgeted_categories, budget_total, spent_on_budgeted, overspent_count,
                  unbudgeted_categories, unbudgeted_spend}, note}
    """
    tmap = {a.get("name"): a.get("type") for a in accounts}
    nets = month_net_by_account(txns, mkey)
    bmap: dict[str, float] = {}
    for b in budget_rows:
        if b.get("month") == mkey and isinstance(b.get("amount"), (int, float)) \
                and not isinstance(b.get("amount"), bool) and b.get("category"):
            bmap[b["category"]] = float(b["amount"])  # 同月同类多行时后行覆盖
    expense_cats = {n for n, t in tmap.items() if t == "expense"}
    cats = set(bmap) | {c for c in expense_cats if nets.get(c, 0.0)}
    rows: list[dict] = []
    for cat in sorted(cats):
        spent = round(nets.get(cat, 0.0), 2)
        budget = bmap.get(cat)
        has_budget = budget is not None
        overspent = has_budget and spent > budget + EPS
        used_pct = round(spent / budget * 100, 1) if has_budget and budget > EPS else None
        rows.append({"category": cat, "budget": round(budget, 2) if has_budget else None,
                     "spent": spent,
                     "remaining": round(budget - spent, 2) if has_budget else None,
                     "used_pct": used_pct, "overspent": overspent, "budgeted": has_budget})
    # 有预算的在前（按已花多→少），无预算有花费的殿后
    rows.sort(key=lambda r: (not r["budgeted"], -(r["spent"])))
    budgeted = [r for r in rows if r["budgeted"]]
    unbudgeted = [r for r in rows if not r["budgeted"]]
    note = None
    if not bmap:
        note = f"{mkey} 未设任何预算"
    elif not rows:
        note = f"{mkey} 设了预算但本月尚无支出"
    return {"month": mkey, "rows": rows,
            "totals": {"budgeted_categories": len(budgeted),
                       "budget_total": round(sum(r["budget"] for r in budgeted), 2),
                       "spent_on_budgeted": round(sum(r["spent"] for r in budgeted), 2),
                       "overspent_count": sum(1 for r in budgeted if r["overspent"]),
                       "unbudgeted_categories": len(unbudgeted),
                       "unbudgeted_spend": round(sum(r["spent"] for r in unbudgeted), 2)},
            "note": note}


def _user_view(exec_: dict) -> str:
    t = exec_["totals"]
    if t["budgeted_categories"] == 0:
        return f"{exec_['month']} 未设预算。"
    overs = [r for r in exec_["rows"] if r["budgeted"] and r["overspent"]]
    if overs:
        worst = overs[0]
        return (f"{exec_['month']} 预算 {t['budgeted_categories']} 类，"
                f"{t['overspent_count']} 类超支（最紧的 {worst['category']} 已用 "
                f"{worst['used_pct']}%）。")
    top = max((r for r in exec_["rows"] if r["budgeted"]),
              key=lambda r: (r["used_pct"] if r["used_pct"] is not None else 0), default=None)
    if top and top["used_pct"] is not None:
        return (f"{exec_['month']} 预算 {t['budgeted_categories']} 类，无一超支"
                f"（最紧的 {top['category']} 已用 {top['used_pct']}%）。")
    return f"{exec_['month']} 预算 {t['budgeted_categories']} 类，本月尚无支出。"


# ---------- 命令 ----------

def _expense_categories(accounts: list[dict]) -> list[str]:
    return sorted({a.get("name") for a in accounts
                   if a.get("type") == "expense" and a.get("name")})


def cmd_set(a) -> int:
    try:
        month = parse_month(a.month)
    except ValueError:
        return D.jfail(f"--month 应为 YYYY-MM，现在是 {a.month!r}")
    if not isinstance(a.amount, (int, float)) or isinstance(a.amount, bool):
        return D.jfail(f"--amount 必须为数值，现在是 {a.amount!r}")
    amount = round(float(a.amount), 2)
    if amount <= 0:  # 先 round 再校验：0.001 round 到分为 0 会落成幽灵预算（D4，与 ledger.record 同口径）
        return D.jout({"ok": False, "reason_code": "amount_too_small",
                       "error": f"--amount 必须为正数；{a.amount!r} 四舍五入到分为 {amount}，拒绝落库"},
                      code=1)
    category = (a.category or "").strip()
    if not category:
        return D.jfail("--category 不能为空")
    accounts = D.read_rows(D.table_path("accounts"))
    if category not in _expense_categories(accounts):
        return D.jout({"ok": False, "reason_code": "unknown_category",
                       "error": f"「{category}」不是已存在的支出科目",
                       "known_expense_categories": _expense_categories(accounts),
                       "hint": "预算类别须先经 ledger.py accounts add --type expense 开科目；"
                               "命名习惯如 支出·餐饮"})
    # 未过滤读取参与重写：upsert 重写整表时 _corrupt 坏行原样保留（D7，与 data.py delete/update 口径一致），
    # 判定 upsert 命中仍用过滤后的合法行（坏行无 month/category，本就不参与）
    rows_raw = D.read_rows(D.table_path("budgets"))
    rows = [r for r in rows_raw if not r.get("_corrupt")]
    old = [r for r in rows if r.get("month") == month and r.get("category") == category]
    row = {"month": month, "category": category, "amount": amount,
           "_id": D.row_id(), "created_at": D.now_iso(), "recorded_at": D.now_iso()[:10]}
    action = "updated" if old else "created"
    if a.dry_run:
        return D.jout({"ok": True, "dry_run": True, "action": action,
                       "would_write": row, "replaced": len(old)})
    if old:  # upsert：旧行先移 trash（可人工恢复），再写新行；坏行原样保留（D7）
        trash = os.path.join(D.data_dir(), "trash", f"budgets-{datetime.now():%Y%m%d}.jsonl")
        for r in old:
            r["_superseded_at"] = D.now_iso()
            D.append_row(trash, r)
        keep = [r for r in rows_raw if not (r.get("month") == month and r.get("category") == category)]
        keep.append(row)
        D.atomic_write(D.table_path("budgets"),
                       "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep))
    else:
        D.append_row(D.table_path("budgets"), row)
    verb = "更新" if old else "记好"
    return D.jout({"ok": True, "action": action, "replaced": len(old), "budget": row,
                   "user_view": f"预算{verb}：{month} {category} 每月 {amount} 元。"})


def cmd_list(a) -> int:
    rows = load_budgets()
    if a.month:
        try:
            rows = [r for r in rows if r.get("month") == parse_month(a.month)]
        except ValueError:
            return D.jfail(f"--month 应为 YYYY-MM，现在是 {a.month!r}")
    rows.sort(key=lambda r: (str(r.get("month", "")), str(r.get("category", ""))), reverse=True)
    return D.jout({"ok": True, "count": len(rows),
                   "budgets": [{"month": r.get("month"), "category": r.get("category"),
                                "amount": r.get("amount")} for r in rows]})


def cmd_check(a) -> int:
    try:
        month = parse_month(a.month) if a.month else D.now_iso()[:7]
    except ValueError:
        return D.jfail(f"--month 应为 YYYY-MM，现在是 {a.month!r}")
    accounts = D.read_rows(D.table_path("accounts"))
    exec_ = compute_execution(accounts, D.read_rows(D.table_path("txns")),
                              load_budgets(), month)
    return D.jout({"ok": True, **exec_, "user_view": _user_view(exec_)})


# ---------- selfcheck ----------

def cmd_selfcheck(_a) -> int:
    problems: list[str] = []
    # 1) 表可读（未 init 时按空表处理，不算故障）
    for t in ("budgets", "txns", "accounts"):
        try:
            D.read_rows(D.table_path(t))
        except Exception as e:  # noqa: BLE001
            problems.append(f"表 {t} 读取失败：{e}")
    # 2) 推导数学（纯内存合成账本，不落盘）
    accts = [{"name": n, "type": t} for n, t in
             [("期初权益", "equity"), ("钱包", "asset"), ("支出·餐饮", "expense"),
              ("支出·交通", "expense"), ("收入·工资", "income")]]
    txns = [
        {"_id": "1", "date": "2026-08-01", "from_account": "期初权益", "to_account": "钱包", "amount": 1000},
        {"_id": "2", "date": "2026-08-05", "from_account": "钱包", "to_account": "支出·餐饮", "amount": 500},
        {"_id": "3", "date": "2026-08-05", "from_account": "钱包", "to_account": "支出·交通", "amount": 100},
        {"_id": "4", "date": "2026-08-09", "from_account": "钱包", "to_account": "支出·餐饮", "amount": 32},
        {"_id": "5", "date": "2026-08-10", "from_account": "支出·餐饮", "to_account": "钱包", "amount": 32},  # 冲正
        {"_id": "6", "date": "2026-08-20", "from_account": "钱包", "to_account": "支出·交通", "amount": 300},
    ]
    budgets = [{"month": "2026-08", "category": "支出·餐饮", "amount": 500},
               {"month": "2026-08", "category": "支出·交通", "amount": 300}]
    ex = compute_execution(accts, txns, budgets, "2026-08")
    by_cat = {r["category"]: r for r in ex["rows"]}
    # 餐饮：500+32−32(冲正净额归零…实为 500)=500，预算 500 → 用满 100% 未超
    if abs(by_cat["支出·餐饮"]["spent"] - 500.0) > EPS:
        problems.append(f"餐饮净花费应为 500（冲正须被净额消化），算得 {by_cat['支出·餐饮']['spent']}")
    if by_cat["支出·餐饮"]["overspent"] or by_cat["支出·餐饮"]["used_pct"] != 100.0:
        problems.append(f"餐饮应为 100% 用满未超支，算得 {by_cat['支出·餐饮']}")
    # 交通：400/300 → 超支
    if not by_cat["支出·交通"]["overspent"] or by_cat["支出·交通"]["used_pct"] != 133.3:
        problems.append(f"交通应判超支且 133.3%，算得 {by_cat['支出·交通']}")
    if ex["totals"]["unbudgeted_categories"] != 0 or ex["totals"]["overspent_count"] != 1:
        problems.append(f"汇总计数不对：{ex['totals']}")
    # 无预算月份：有花费的科目也该出现（unbudgeted）
    ex2 = compute_execution(accts, txns, budgets, "2026-09")
    if ex2["rows"] or ex2["totals"]["budgeted_categories"] != 0:
        problems.append(f"9月无预算无支出，应只有 note：{ex2['rows']}")
    txns9 = txns + [{"_id": "7", "date": "2026-09-01", "from_account": "钱包",
                     "to_account": "支出·餐饮", "amount": 66}]
    ex3 = compute_execution(accts, txns9, budgets, "2026-09")
    if [r["category"] for r in ex3["rows"]] != ["支出·餐饮"] or ex3["rows"][0]["budgeted"]:
        problems.append(f"9月无预算但餐饮有花费，应出 unbudgeted 行：{ex3['rows']}")
    # 2b) 日期容错（D1 回归）：非补零日期行归一计入当月，垃圾日期行不静默计数
    nets_d = month_net_by_account(
        txns + [{"_id": "8", "date": "2026-8-11", "from_account": "钱包",
                 "to_account": "支出·餐饮", "amount": 7},
                {"_id": "9", "date": "垃圾", "from_account": "钱包",
                 "to_account": "支出·餐饮", "amount": 9}], "2026-08")
    if abs(nets_d.get("支出·餐饮", 0.0) - 507.0) > EPS or abs(nets_d.get("钱包", 0.0) - 93.0) > EPS:
        problems.append(f"month_net_by_account 未宽容归一非补零日期/未剔除垃圾日期：{nets_d}")

    # 3) 月份工具
    if (prev_month("2026-01"), prev_month("2026-09"), month_last_day("2026-02")) != \
            ("2025-12", "2026-08", "2026-02-28"):
        problems.append("月份工具（prev_month/month_last_day）结果不对")
    if problems:
        return D.jout({"ok": False, "error": "；".join(problems)}, code=1)
    real = load_budgets()
    return D.jout({"ok": True, "agent": D.manifest()["id"], "tool": "budget",
                   "derive_math": "verified",
                   "real_budget_rows": len(real),
                   "real_months": sorted({r.get("month") for r in real})})


def main() -> int:
    ap = argparse.ArgumentParser(description="月度预算（设额度、查执行）")
    ap.add_argument("--selfcheck", action="store_true", help="自检（收编校验契约，等价于子命令 selfcheck）")
    sub = ap.add_subparsers(dest="cmd", required=False)

    p_s = sub.add_parser("set")
    p_s.add_argument("--month", required=True, help="月份 YYYY-MM")
    p_s.add_argument("--category", required=True, help="支出科目名，如 支出·餐饮")
    p_s.add_argument("--amount", type=float, required=True, help="当月额度（元，正数）")
    p_s.add_argument("--dry-run", action="store_true")

    p_l = sub.add_parser("list")
    p_l.add_argument("--month", default=None, help="只看某月 YYYY-MM，缺省全部")

    p_c = sub.add_parser("check")
    p_c.add_argument("--month", default=None, help="缺省本月")

    sub.add_parser("selfcheck")

    x = ap.parse_args()
    if x.selfcheck:
        return cmd_selfcheck(x)
    if not x.cmd:
        ap.print_help()
        return D.jout({"ok": False, "error": "缺少子命令（或用 --selfcheck）"}, code=2)
    return {"set": cmd_set, "list": cmd_list, "check": cmd_check,
            "selfcheck": cmd_selfcheck}[x.cmd](x)


if __name__ == "__main__":
    sys.exit(main())
