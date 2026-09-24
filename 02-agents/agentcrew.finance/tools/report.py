#!/usr/bin/env python3
"""report.py — 月度理财简报（收支分类 / 预算执行 / 净资产变动 / Top 开销 / 异常检测）。

只摆事实：简报陈述账本推导出的数字与"疑似"模式，不给任何投资建议、不下结论。

命令：
  monthly [--month YYYY-MM] [--json] [--out PATH] [--dry-run]
      缺省生成**上月**简报；markdown 写入 <data>/reports/monthly-<月>.md（--out 可改）；
      --json 时 stdout 额外携带完整结构化载荷 report（markdown 文件照常生成，--dry-run 除外）。
  selfcheck

异常检测三条规则（A3，阈值可经 data/config.json 覆盖）：
  疑似重复扣款    同日同金额、非转账（收款方为支出科目）、≥2 笔
  环比飙升类别    支出环比涨幅 > anomaly_rise_pct（缺省 50）% 且增加额 ≥
                  anomaly_rise_min_delta（缺省 100）元；上月无基数的不参与
  疑似周期订阅    近 3 个月（本月 + 前两月）每月都有同金额、同备注（对方）的支出

净资产口径：只统计勾了 in_net_worth 的资产与负债（与 ledger net-worth 同口径）；
投资账户取该时点前最新一次市值快照，没报过市值的按账本余额算。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data as D       # noqa: E402  （存档解析器与通用读写）
import ledger          # noqa: E402  （余额推导唯一真相）
import budget as B     # noqa: E402  （月份工具与预算执行，避免两处算账）

EPS = 0.005


# ---------- 纯计算（selfcheck 用合成数据验这里） ----------

def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def expense_by_category(txns: list[dict], accounts: list[dict], mkey: str) -> dict[str, float]:
    """某月各支出科目的净花费（净流入），只留有活动的科目。"""
    tmap = {a.get("name"): a.get("type") for a in accounts}
    nets = B.month_net_by_account(txns, mkey)
    return {n: v for n, v in nets.items()
            if tmap.get(n) == "expense" and abs(v) > EPS}


def _valuation_upto(valuations: list[dict], account: str, as_of: str) -> dict | None:
    best = None
    for v in valuations:
        if v.get("account") != account or not _is_num(v.get("value")):
            continue
        if str(v.get("date", "")) > as_of:
            continue
        key = (str(v.get("date", "")), str(v.get("created_at", "")))
        if best is None or key > (str(best.get("date", "")), str(best.get("created_at", ""))):
            best = v
    return best


def net_worth_at(accounts: list[dict], txns: list[dict], valuations: list[dict],
                 as_of: str) -> dict:
    """某时点净资产（账本推导 + 时点前最新估值），口径与 ledger net-worth 一致。"""
    disp, _, orphans, malformed = ledger.derive(accounts, txns, as_of=as_of)
    assets = liabilities = 0.0
    for a in accounts:
        n, t = a.get("name"), a.get("type")
        if t == "asset" and a.get("in_net_worth"):
            v = _valuation_upto(valuations, n, as_of)
            assets += float(v["value"]) if v else disp.get(n, 0.0)
        elif t == "liability" and a.get("in_net_worth"):
            liabilities += disp.get(n, 0.0)  # 负债 displayed 正=欠钱（ledger 符号约定）
    assets, liabilities = round(assets, 2), round(liabilities, 2)
    return {"as_of": as_of, "assets": assets, "liabilities": liabilities,
            "net_worth": round(assets - liabilities, 2)}


def top_expenses(txns: list[dict], accounts: list[dict], mkey: str, n: int = 5) -> list[dict]:
    tmap = {a.get("name"): a.get("type") for a in accounts}
    cand = [t for t in txns
            if str(t.get("date", ""))[:7] == mkey
            and tmap.get(t.get("to_account")) == "expense" and _is_num(t.get("amount"))]
    cand.sort(key=lambda t: (-float(t["amount"]), str(t.get("date", ""))))
    return [{"date": t.get("date"), "amount": round(float(t["amount"]), 2),
             "category": t.get("to_account"), "account": t.get("from_account"),
             "note": t.get("note"), "_id": t.get("_id")} for t in cand[:n]]


# ---------- 异常检测（只标记模式，不下结论） ----------

def find_duplicate_charges(txns: list[dict], accounts: list[dict], mkey: str) -> list[dict]:
    """同日同金额、收款方为支出科目（非转账）、≥2 笔。"""
    tmap = {a.get("name"): a.get("type") for a in accounts}
    groups: dict[tuple, list[dict]] = {}
    for t in txns:
        if str(t.get("date", ""))[:7] != mkey or tmap.get(t.get("to_account")) != "expense":
            continue
        if not _is_num(t.get("amount")):
            continue
        groups.setdefault((str(t.get("date", "")), round(float(t["amount"]), 2)), []).append(t)
    hits = [{"date": d, "amount": amt, "count": len(items),
             "occurrences": [{"category": t.get("to_account"),
                              "account": t.get("from_account"), "note": t.get("note"),
                              "_id": t.get("_id")} for t in items]}
            for (d, amt), items in sorted(groups.items()) if len(items) >= 2]
    hits.sort(key=lambda h: (h["date"], -h["amount"]))
    return hits


def find_category_spikes(txns: list[dict], accounts: list[dict], mkey: str,
                         rise_pct: float = 50.0, min_delta: float = 100.0) -> list[dict]:
    """支出环比涨幅超阈值且增加额显著的类别；上月无基数（≤0）的不参与。"""
    cur = expense_by_category(txns, accounts, mkey)
    pm = B.prev_month(mkey)
    prev = expense_by_category(txns, accounts, pm)
    hits = []
    for cat, c in sorted(cur.items()):
        p = prev.get(cat, 0.0)
        if p <= EPS:
            continue
        rise = (c / p - 1.0) * 100
        delta = round(c - p, 2)
        if rise > rise_pct and delta >= min_delta - EPS:
            hits.append({"category": cat, "prev_month": pm, "prev": round(p, 2),
                         "current": round(c, 2), "delta": delta, "rise_pct": round(rise, 1)})
    hits.sort(key=lambda h: -h["delta"])
    return hits


def find_subscriptions(txns: list[dict], accounts: list[dict], mkey: str,
                       months: int = 3) -> list[dict]:
    """近 N 个月每月都有同金额、同备注（对方）的支出。备注为空的不参与。"""
    mlist = [mkey]
    for _ in range(months - 1):
        mlist.append(B.prev_month(mlist[-1]))
    window = sorted(mlist)  # 时间正序展示
    tmap = {a.get("name"): a.get("type") for a in accounts}
    groups: dict[tuple, dict[str, int]] = {}
    for t in txns:
        mk = str(t.get("date", ""))[:7]
        if mk not in mlist or tmap.get(t.get("to_account")) != "expense":
            continue
        note = str(t.get("note") or "").strip().lower()
        if not note or not _is_num(t.get("amount")):
            continue
        g = groups.setdefault((note, round(float(t["amount"]), 2)), {})
        g[mk] = g.get(mk, 0) + 1
    hits = [{"note": note, "amount": amt, "months": {m: per.get(m, 0) for m in window},
             "total_in_window": round(amt * sum(per.values()), 2)}
            for (note, amt), per in sorted(groups.items())
            if all(per.get(m, 0) >= 1 for m in mlist)]
    hits.sort(key=lambda h: (-h["total_in_window"], h["note"]))
    return hits


# ---------- 简报组装 ----------

def build_report(accounts: list[dict], txns: list[dict], budget_rows: list[dict],
                 valuations: list[dict], mkey: str, generated_at: str,
                 cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    rise_pct = float(cfg.get("anomaly_rise_pct", 50.0))
    min_delta = float(cfg.get("anomaly_rise_min_delta", 100.0))
    tmap = {a.get("name"): a.get("type") for a in accounts}
    nets = B.month_net_by_account(txns, mkey)

    income_by = {n: round(-v, 2) for n, v in nets.items()
                 if tmap.get(n) == "income" and abs(v) > EPS}
    expense_by = expense_by_category(txns, accounts, mkey)
    income_by = dict(sorted(income_by.items(), key=lambda kv: -kv[1]))
    expense_by = dict(sorted(expense_by.items(), key=lambda kv: -kv[1]))
    income_total = round(sum(income_by.values()), 2)
    expense_total = round(sum(expense_by.values()), 2)

    execution = B.compute_execution(accounts, txns, budget_rows, mkey)

    today = generated_at[:10]
    start_asof = str(date.fromisoformat(f"{mkey}-01") - timedelta(days=1))
    end_asof = min(B.month_last_day(mkey), today)
    nw_start = net_worth_at(accounts, txns, valuations, start_asof)
    nw_end = net_worth_at(accounts, txns, valuations, end_asof)

    dupes = find_duplicate_charges(txns, accounts, mkey)
    spikes = find_category_spikes(txns, accounts, mkey, rise_pct, min_delta)
    subs = find_subscriptions(txns, accounts, mkey)

    change = round(nw_end["net_worth"] - nw_start["net_worth"], 2)
    payload = {
        "month": mkey, "generated_at": generated_at,
        "income_by_category": income_by, "expense_by_category": expense_by,
        "income_total": income_total, "expense_total": expense_total,
        "net": round(income_total - expense_total, 2),
        "budget_execution": execution,
        "net_worth": {"start_as_of": start_asof, "start": nw_start["net_worth"],
                      "end_as_of": end_asof, "end": nw_end["net_worth"], "change": change,
                      "start_assets": nw_start["assets"], "start_liabilities": nw_start["liabilities"],
                      "end_assets": nw_end["assets"], "end_liabilities": nw_end["liabilities"],
                      "caliber": "只统计勾了计入净资产的资产与负债；投资账户取该时点前最新市值快照，"
                                 "没报过市值的按账本余额算"},
        "top_expenses": top_expenses(txns, accounts, mkey, 5),
        "anomalies": {
            "duplicate_charges": {"rule": "同日同金额、非转账、≥2 笔", "items": dupes},
            "category_spikes": {"rule": f"环比涨幅 > {rise_pct:g}% 且增加额 ≥ {min_delta:g} 元",
                                "items": spikes},
            "suspected_subscriptions": {"rule": "近 3 个月每月同额同对方（备注）支出",
                                        "items": subs},
        },
    }
    # 期末时点的孤儿/坏行计数（数据状况报告，不算故障）
    _, _, orphans, malformed = ledger.derive(accounts, txns, as_of=end_asof)
    payload["warnings"] = {"orphan_txns": len(orphans), "malformed_txns": len(malformed)}
    payload["user_view"] = _user_view(payload, rise_pct, min_delta)
    return payload


def _fmt(x: float) -> str:
    return f"{x:,.2f}"


def _user_view(p: dict, rise_pct: float, min_delta: float) -> str:
    s = (f"{p['month']}：收入 {_fmt(p['income_total'])} 元、支出 {_fmt(p['expense_total'])} 元、"
         f"结余 {_fmt(p['net'])} 元；净资产较月初 {'+' if p['net_worth']['change'] >= 0 else ''}"
         f"{_fmt(p['net_worth']['change'])} 元。")
    ex = p["budget_execution"]
    if ex["totals"]["budgeted_categories"]:
        s += (f"预算 {ex['totals']['budgeted_categories']} 类，"
              f"超支 {ex['totals']['overspent_count']} 类。")
    a = p["anomalies"]
    bits = [f"疑似重复扣款 {len(a['duplicate_charges']['items'])} 组",
            f"环比涨超{rise_pct:g}%且增{min_delta:g}元以上的类别 "
            f"{len(a['category_spikes']['items'])} 个",
            f"疑似订阅 {len(a['suspected_subscriptions']['items'])} 项"]
    s += "异常提示（只摆事实，供核对）：" + "、".join(bits) + "。"
    return s


# ---------- markdown 渲染 ----------

def render_markdown(p: dict) -> str:
    L: list[str] = []
    L.append(f"# 月度理财简报 · {p['month']}")
    L.append("")
    L.append(f"> 生成于 {p['generated_at']} ｜ 数据来源：本地账本（agentcrew.finance）")
    L.append("> 本简报只陈述账本事实，不构成任何投资建议。")
    L.append("")
    L.append("## 一、收支概览")
    L.append("")
    L.append("**收入**")
    L.append("")
    if p["income_by_category"]:
        L.append("| 收入科目 | 金额（元） |")
        L.append("|---|---:|")
        for k, v in p["income_by_category"].items():
            L.append(f"| {k} | {_fmt(v)} |")
    else:
        L.append("本月无收入入账。")
    L.append("")
    L.append("**支出**")
    L.append("")
    if p["expense_by_category"]:
        L.append("| 支出科目 | 金额（元） |")
        L.append("|---|---:|")
        for k, v in p["expense_by_category"].items():
            L.append(f"| {k} | {_fmt(v)} |")
    else:
        L.append("本月无支出记录。")
    L.append("")
    L.append(f"**收入合计** {_fmt(p['income_total'])} 元 ｜ **支出合计** {_fmt(p['expense_total'])} 元"
             f" ｜ **当月结余** {_fmt(p['net'])} 元")
    L.append("")
    L.append("## 二、预算执行")
    L.append("")
    rows = p["budget_execution"]["rows"]
    if rows:
        L.append("| 类别 | 预算（元） | 已花（元） | 执行率 | 状态 |")
        L.append("|---|---:|---:|---:|---|")
        for r in rows:
            if not r["budgeted"]:
                L.append(f"| {r['category']} | — | {_fmt(r['spent'])} | 未设预算 | 未设预算 |")
            else:
                status = "**超支**" if r["overspent"] else "正常"
                pct = f"{r['used_pct']}%" if r["used_pct"] is not None else "—"
                L.append(f"| {r['category']} | {_fmt(r['budget'])} | {_fmt(r['spent'])} |"
                         f" {pct} | {status} |")
    else:
        L.append(p["budget_execution"].get("note") or "本月未设预算，也没有支出。")
    t = p["budget_execution"]["totals"]
    if t["budgeted_categories"]:
        L.append("")
        L.append(f"本月设预算 {t['budgeted_categories']} 类共 {_fmt(t['budget_total'])} 元，"
                 f"对应已花 {_fmt(t['spent_on_budgeted'])} 元，超支 {t['overspent_count']} 类；"
                 f"未设预算类别花销 {_fmt(t['unbudgeted_spend'])} 元。")
    L.append("")
    L.append("## 三、净资产变动")
    L.append("")
    nw = p["net_worth"]
    L.append(f"- 月初（截至 {nw['start_as_of']}）：{_fmt(nw['start'])} 元")
    L.append(f"- 月末（截至 {nw['end_as_of']}）：{_fmt(nw['end'])} 元")
    sign = "+" if nw["change"] >= 0 else ""
    L.append(f"- **变动：{sign}{_fmt(nw['change'])} 元**")
    L.append("")
    L.append(f"口径：{nw['caliber']}。")
    L.append("")
    L.append("## 四、Top 开销 5 笔")
    L.append("")
    if p["top_expenses"]:
        L.append("| 日期 | 金额（元） | 类别 | 付款账户 | 备注 |")
        L.append("|---|---:|---|---|---|")
        for e in p["top_expenses"]:
            L.append(f"| {e['date']} | {_fmt(e['amount'])} | {e['category']} |"
                     f" {e['account'] or '—'} | {e['note'] or '—'} |")
    else:
        L.append("本月无支出记录。")
    L.append("")
    L.append("## 五、异常检测（只摆事实，供您核对）")
    L.append("")
    an = p["anomalies"]

    L.append(f"### 1. 疑似重复扣款 —— {an['duplicate_charges']['rule']}")
    L.append("")
    if an["duplicate_charges"]["items"]:
        for h in an["duplicate_charges"]["items"]:
            detail = "；".join(f"{o.get('category')}（付款 {o.get('account') or '—'}，"
                               f"备注 {o.get('note') or '—'}）" for o in h["occurrences"])
            L.append(f"- {h['date']} 金额 {_fmt(h['amount'])} 元共 {h['count']} 笔：{detail}")
    else:
        L.append("未发现。")
    L.append("")
    L.append(f"### 2. 环比飙升类别 —— {an['category_spikes']['rule']}")
    L.append("")
    if an["category_spikes"]["items"]:
        for h in an["category_spikes"]["items"]:
            L.append(f"- {h['category']}：{h['prev_month']} 为 {_fmt(h['prev'])} 元 →"
                     f" {p['month']} 为 {_fmt(h['current'])} 元"
                     f"（+{h['rise_pct']:g}%，增加 {_fmt(h['delta'])} 元）")
    else:
        L.append("未发现。")
    L.append("")
    L.append(f"### 3. 疑似周期订阅 —— {an['suspected_subscriptions']['rule']}")
    L.append("")
    if an["suspected_subscriptions"]["items"]:
        for h in an["suspected_subscriptions"]["items"]:
            ms = "/".join(f"{m}×{c}" for m, c in h["months"].items())
            L.append(f"- 备注「{h['note']}」每月 {h['amount']:g} 元：{ms}，"
                     f"3 个月合计 {_fmt(h['total_in_window'])} 元")
    else:
        L.append("未发现。")
    L.append("")
    if p["warnings"]["orphan_txns"] or p["warnings"]["malformed_txns"]:
        L.append(f"> 数据提示：账本存在 {p['warnings']['orphan_txns']} 笔孤儿流水、"
                 f"{p['warnings']['malformed_txns']} 笔坏行，相关数字可能不准，"
                 f"可跑 ledger.py trial-balance 排查。")
        L.append("")
    L.append("---")
    L.append("")
    L.append("*以上全部数字由本地流水当场推导，未联网；简报只摆事实，不含任何操作建议。*")
    L.append("")
    return "\n".join(L)


# ---------- 命令 ----------

def _rel(path: str) -> str:
    try:
        return os.path.relpath(path, D.agent_root())
    except ValueError:
        return path


def cmd_monthly(a) -> int:
    today = D.now_iso()[:10]
    try:
        month = B.parse_month(a.month) if a.month else B.prev_month(today[:7])
    except ValueError:
        return D.jfail(f"--month 应为 YYYY-MM，现在是 {a.month!r}")
    payload = build_report(
        D.read_rows(D.table_path("accounts")),
        D.read_rows(D.table_path("txns")),
        [r for r in D.read_rows(D.table_path("budgets")) if not r.get("_corrupt")],
        D.read_rows(D.table_path("valuations")),
        month, D.now_iso(), ledger.read_config())
    md = render_markdown(payload)
    out = a.out or os.path.join(D.data_dir(), "reports", f"monthly-{month}.md")
    result: dict = {"ok": True, "month": month, "user_view": payload["user_view"],
                    "report_file": _rel(out)}
    if a.dry_run:
        result.update({"dry_run": True, "markdown": md, "markdown_chars": len(md)})
    else:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        D.atomic_write(out, md)
    if a.json:
        result["report"] = payload
    else:
        ex = payload["budget_execution"]["totals"]
        an = payload["anomalies"]
        result["summary"] = {
            "income_total": payload["income_total"], "expense_total": payload["expense_total"],
            "net": payload["net"], "net_worth_change": payload["net_worth"]["change"],
            "budget": {"budgeted_categories": ex["budgeted_categories"],
                       "overspent_count": ex["overspent_count"]},
            "anomaly_counts": {"duplicate_charge_groups": len(an["duplicate_charges"]["items"]),
                               "category_spikes": len(an["category_spikes"]["items"]),
                               "suspected_subscriptions": len(an["suspected_subscriptions"]["items"])},
        }
    return D.jout(result)


# ---------- selfcheck ----------

def cmd_selfcheck(_a) -> int:
    problems: list[str] = []
    # 1) 真实表可读（未 init 按空表处理，不算故障）
    for t in ("accounts", "txns", "budgets", "valuations"):
        try:
            D.read_rows(D.table_path(t))
        except Exception as e:  # noqa: BLE001
            problems.append(f"表 {t} 读取失败：{e}")
    # 2) 合成三个月账本，验全部推导（固定远期月份，任何日期重跑结果一致）
    accts = [{"name": n, "type": t, "in_net_worth": w} for n, t, w in
             [("期初权益", "equity", False), ("钱包", "asset", True),
              ("花呗", "liability", True), ("收入·工资", "income", False),
              ("支出·餐饮", "expense", False), ("支出·购物", "expense", False),
              ("支出·订阅", "expense", False)]]
    T = [
        {"_id": "01", "date": "2020-05-01", "from_account": "期初权益", "to_account": "钱包", "amount": 1000},
        {"_id": "02", "date": "2020-06-15", "from_account": "钱包", "to_account": "支出·订阅", "amount": 25, "note": "腾讯视频"},
        {"_id": "03", "date": "2020-07-01", "from_account": "收入·工资", "to_account": "钱包", "amount": 5000},
        {"_id": "04", "date": "2020-07-10", "from_account": "钱包", "to_account": "支出·购物", "amount": 200, "note": "上月大采购"},
        {"_id": "05", "date": "2020-07-15", "from_account": "钱包", "to_account": "支出·订阅", "amount": 25, "note": "腾讯视频"},
        {"_id": "06", "date": "2020-07-20", "from_account": "钱包", "to_account": "支出·餐饮", "amount": 100, "note": "餐厅A"},
        {"_id": "07", "date": "2020-08-01", "from_account": "收入·工资", "to_account": "钱包", "amount": 5000},
        {"_id": "08", "date": "2020-08-02", "from_account": "钱包", "to_account": "支出·购物", "amount": 900, "note": "大采购"},
        {"_id": "09", "date": "2020-08-05", "from_account": "钱包", "to_account": "支出·餐饮", "amount": 32, "note": "瑞幸"},
        {"_id": "10", "date": "2020-08-05", "from_account": "钱包", "to_account": "支出·餐饮", "amount": 32, "note": "瑞幸"},
        {"_id": "11", "date": "2020-08-09", "from_account": "钱包", "to_account": "支出·订阅", "amount": 25, "note": "腾讯视频"},
        {"_id": "12", "date": "2020-08-15", "from_account": "花呗", "to_account": "支出·购物", "amount": 200, "note": "花呗买鞋"},
    ]
    budgets = [{"month": "2020-08", "category": "支出·购物", "amount": 500},
               {"month": "2020-08", "category": "支出·餐饮", "amount": 200}]

    def build(vals=None):
        return build_report(accts, T, budgets, vals or [], "2020-08",
                            "2020-09-25T08:30:00+08:00", {})

    p = build()
    if (p["income_total"], p["expense_total"], p["net"]) != (5000.0, 1189.0, 3811.0):
        problems.append(f"收支汇总不对：{p['income_total']}/{p['expense_total']}/{p['net']}"
                        "（应 5000/1189/3811）")
    nw = p["net_worth"]
    if (nw["start"], nw["end"], nw["change"]) != (5650.0, 9461.0, 3811.0):
        problems.append(f"净资产变动不对：start={nw['start']} end={nw['end']} change={nw['change']}"
                        "（应 5650/9461/3811，花呗欠 200 须扣减）")
    if len(p["top_expenses"]) != 5 or p["top_expenses"][0]["amount"] != 900.0 \
            or p["top_expenses"][0]["category"] != "支出·购物":
        problems.append(f"Top 开销不对：{p['top_expenses'][:1]}")
    an = p["anomalies"]
    if len(an["duplicate_charges"]["items"]) != 1:
        problems.append(f"疑似重复扣款应检出 1 组（08-05 两个 32 元），得 "
                        f"{an['duplicate_charges']['items']}")
    else:
        h = an["duplicate_charges"]["items"][0]
        if (h["date"], h["amount"], h["count"]) != ("2020-08-05", 32.0, 2):
            problems.append(f"重复扣款明细不对：{h}")
    if len(an["category_spikes"]["items"]) != 1 or an["category_spikes"]["items"][0]["category"] != "支出·购物":
        problems.append(f"环比飙升应只检出 支出·购物（200→900），得 {an['category_spikes']['items']}")
    if len(an["suspected_subscriptions"]["items"]) != 1:
        problems.append(f"疑似订阅应只检出 腾讯视频，得 {an['suspected_subscriptions']['items']}")
    else:
        s = an["suspected_subscriptions"]["items"][0]
        if s["note"] != "腾讯视频" or s["amount"] != 25.0 or s["total_in_window"] != 75.0:
            problems.append(f"订阅明细不对：{s}")
    ex = p["budget_execution"]
    bmap = {r["category"]: r for r in ex["rows"]}
    if not bmap["支出·购物"]["overspent"] or bmap["支出·购物"]["used_pct"] != 220.0:
        problems.append(f"购物预算 500 花 1100 应超支 220%，得 {bmap['支出·购物']}")
    if bmap["支出·订阅"]["budgeted"] or bmap["支出·订阅"]["spent"] != 25.0:
        problems.append(f"订阅未设预算应出 unbudgeted 行，得 {bmap['支出·订阅']}")
    # 估值分支：报过 2020-08-20 市值 12000 → 月末用它、月初不用
    pv = build([{"account": "钱包", "date": "2020-08-20", "value": 12000}])
    if pv["net_worth"]["end"] != 11800.0 or pv["net_worth"]["start"] != 5650.0:
        problems.append(f"估值快照口径不对：{pv['net_worth']['start']}/{pv['net_worth']['end']}"
                        "（月末应用 12000−200=11800，月初不受未来估值影响）")
    # 空账本不炸、markdown 完整
    empty = build_report([], [], [], [], "2020-08", "2020-09-25T08:30:00+08:00", {})
    if empty["income_total"] != 0 or empty["top_expenses"]:
        problems.append(f"空账本应全零：{empty['income_total']}/{len(empty['top_expenses'])}")
    md = render_markdown(empty)
    for sec in ("收支概览", "预算执行", "净资产变动", "Top 开销", "异常检测", "未发现"):
        if sec not in md:
            problems.append(f"markdown 缺小节：{sec}")
    if "投资建议" not in render_markdown(p):  # 免责口径行必须在
        problems.append("markdown 缺『不构成任何投资建议』口径行")
    if problems:
        return D.jout({"ok": False, "error": "；".join(problems)}, code=1)
    return D.jout({"ok": True, "agent": D.manifest()["id"], "tool": "report",
                   "fixture_math": "verified",
                   "markdown_chars": len(render_markdown(p))})


def main() -> int:
    ap = argparse.ArgumentParser(description="月度理财简报（只摆事实）")
    ap.add_argument("--selfcheck", action="store_true", help="自检（收编校验契约，等价于子命令 selfcheck）")
    sub = ap.add_subparsers(dest="cmd", required=False)

    p_m = sub.add_parser("monthly")
    p_m.add_argument("--month", default=None, help="月份 YYYY-MM，缺省上月")
    p_m.add_argument("--json", action="store_true", help="stdout 额外携带完整结构化载荷 report")
    p_m.add_argument("--out", default=None, help="markdown 输出路径，缺省 <data>/reports/monthly-<月>.md")
    p_m.add_argument("--dry-run", action="store_true", help="只算不写盘（markdown 随 stdout 返回）")

    sub.add_parser("selfcheck")

    x = ap.parse_args()
    if x.selfcheck:
        return cmd_selfcheck(x)
    if not x.cmd:
        ap.print_help()
        return D.jout({"ok": False, "error": "缺少子命令（或用 --selfcheck）"}, code=2)
    return {"monthly": cmd_monthly, "selfcheck": cmd_selfcheck}[x.cmd](x)


if __name__ == "__main__":
    sys.exit(main())
