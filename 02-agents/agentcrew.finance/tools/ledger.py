#!/usr/bin/env python3
"""ledger.py — 复式记账引擎（底层严格守恒，表层"从哪来到哪去"；仅标准库）。

原则：
  余额永不存死数 —— balance / net-worth / trial-balance / serve 全部由 txns 流水当场推导；
  符号约定 —— 资产/支出为借方科目（流入为正），负债/收入/权益为贷方科目（流出为正）：
      displayed = 科目符号 × (流入 − 流出)。于是：资产正=有钱、负债正=欠钱、
      收入正=挣了、支出正=花了、权益正=期初投入；
  守恒恒等式 —— 每笔交易在 from/to 两个账户上各记 −amt/+amt，全部账户的未符号代数和恒为 0。
      trial-balance 输出该代数和作不变量核验；实际揪错靠三样：指向不存在账户的孤儿流水、
      金额/方向缺失的坏行、类型非法的账户——任一出现即报 unbalanced。

期初余额：从权益账户入账（期初权益 → 某账户 金额），没有专门的期初命令。
退款：方向倒过来再记一笔。改错：reverse 冲正（等额反向），永不涂改历史。

命令：
  accounts add --name 微信零钱 --type asset [--in-net-worth true] [--note 备注] [--dry-run]
  accounts list
  record --from 微信零钱 --to 支出·餐饮 --amount 32 [--date YYYY-MM-DD] [--note ...]
         [--raw-text 原话] [--source record|import] [--dedup-key K] [--json '{...}'] [--dry-run]
  balance [--account 微信零钱]
  net-worth
  trial-balance
  assert --account 微信零钱 --balance 1234 [--date YYYY-MM-DD]
  suggest --text 瑞幸咖啡
  reverse --id r-xxxx [--date YYYY-MM-DD] [--reason 原因] [--dry-run]
  selfcheck
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data as D  # noqa: E402  （存档解析器与通用读写，范式 §5/§6）

SIGN = {"asset": 1, "expense": 1, "liability": -1, "income": -1, "equity": -1}
TYPE_ORDER = ["asset", "liability", "income", "expense", "equity"]
EPS = 0.005            # 金额比对：分以下视为相等
UNBALANCED_EPS = 0.01  # 试算平衡阈值（容忍浮点尾差）


def _num(x):
    """宽容取数：valuations 坏行（手改出非数值）返回 None，不让一行坏数据崩掉整张报表。"""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN 也算坏值


# ---------- 读取与推导 ----------

def load_accounts() -> list[dict]:
    return [r for r in D.read_rows(D.table_path("accounts")) if not r.get("_corrupt")]


def load_txns() -> list[dict]:
    return [r for r in D.read_rows(D.table_path("txns")) if not r.get("_corrupt")]


def load_valuations() -> list[dict]:
    return [r for r in D.read_rows(D.table_path("valuations")) if not r.get("_corrupt")]


def read_config() -> dict:
    p = os.path.join(D.data_dir(), "config.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}


def casual_mode() -> bool:
    """随手档判定：record_mode 由初始化引导写入，兼容常见变体（随手/随手档/casual/wallet/simple）。"""
    raw = str(read_config().get("record_mode", "")).strip().lower()
    return raw in ("casual", "wallet", "simple") or "随手" in raw


def derive(accounts: list[dict], txns: list[dict], as_of: str | None = None):
    """由流水推导余额（纯函数，selfcheck 亦用它做数学验证）。

    返回 (displayed, uniform, orphans, malformed)：
      displayed — 按科目符号呈报的余额（见模块头符号约定）；
      uniform   — 未符号代数和（流入−流出），全部账户之和恒应为 0；
      orphans   — 指向不存在账户的流水（试算不平衡的头号嫌疑）；
      malformed — 金额缺失/非数值、from/to 缺失或日期不可解析的坏行。
    as_of：只统计 date ≤ as_of 的流水（期末对账用）。
    日期一律经 D.norm_date 宽容归一：历史非补零行（如 '2026-8-5'）照常计入，
    解析失败的行进 malformed（suspects 机制），不在推导中静默消失。
    """
    names = {a.get("name") for a in accounts}
    sign = {a.get("name"): SIGN.get(a.get("type"), 1) for a in accounts}
    unif = {a.get("name"): 0.0 for a in accounts}
    orphans: list[dict] = []
    malformed: list[dict] = []
    for t in txns:
        f, to, amt = t.get("from_account"), t.get("to_account"), t.get("amount")
        if not isinstance(amt, (int, float)) or isinstance(amt, bool) or not f or not to:
            malformed.append(t)
            continue
        nd = D.norm_date(t.get("date"))
        if nd is None:
            malformed.append(t)
            continue
        if as_of and nd > as_of:
            continue
        if f not in names or to not in names:
            orphans.append(t)
            continue
        unif[to] += float(amt)
        unif[f] -= float(amt)
    displayed = {n: round(v * sign.get(n, 1), 2) for n, v in unif.items()}
    return displayed, unif, orphans, malformed


def latest_valuations(as_of: str | None = None) -> dict[str, dict]:
    """每账户最新一跳估值：按 (date, created_at) 取最大（主人报的数以最新为准）。

    as_of：只采信 date ≤ as_of 的快照（与 report._valuation_upto 同口径）——
    未来日期的估值快照（手滑填错年份）不得被净资产/看板采信。
    """
    best: dict[str, dict] = {}
    for v in load_valuations():
        if as_of and str(v.get("date", "")) > as_of:
            continue
        key = (str(v.get("date", "")), str(v.get("created_at", "")))
        cur = best.get(v.get("account"))
        if cur is None or key > (str(cur.get("date", "")), str(cur.get("created_at", ""))):
            best[v.get("account")] = v
    return best


def find_account(accounts: list[dict], name: str) -> dict | None:
    return next((a for a in accounts if a.get("name") == name), None)


def _known_names(accounts: list[dict]) -> list[str]:
    return sorted({a.get("name") for a in accounts if a.get("name")})


# ---------- accounts ----------

def cmd_accounts_add(a) -> int:
    # 类型枚举以 manifest 为唯一权威
    schema = next((t for t in (D.manifest().get("tables") or []) if t.get("name") == "accounts"), {})
    types = next((c.get("enum") for c in schema.get("columns", []) if c.get("name") == "type"),
                 None) or list(SIGN)
    if a.type not in types:
        return D.jfail(f"--type 必须是 {types} 之一，现在是 {a.type!r}")
    name = a.name.strip()
    if not name:
        return D.jfail("--name 不能为空")
    accounts = load_accounts()
    if any(str(x.get("name", "")).strip().lower() == name.lower() for x in accounts):
        return D.jout({"ok": False, "reason_code": "account_exists",
                       "error": f"账户「{name}」已存在",
                       "known_accounts": _known_names(accounts),
                       "hint": "换名或直接使用现有账户；要改属性可 data.py update accounts"})
    in_net: bool | str = a.in_net_worth
    if in_net is not None:
        v = str(in_net).strip().lower()
        if v in ("true", "1", "yes", "是", "on"):
            in_net = True
        elif v in ("false", "0", "no", "否", "off"):
            in_net = False
        else:
            return D.jfail(f"--in-net-worth 应为 true/false，现在是 {in_net!r}")
    else:
        in_net = a.type in ("asset", "liability")
    if a.type in ("income", "expense", "equity"):
        in_net = False  # manifest：收支科目与权益一律不计入净资产
    # 随手档：全库只允许一个资产账户「钱包」
    if casual_mode() and a.type == "asset" and any(x.get("type") == "asset" for x in accounts):
        holder = next(x.get("name") for x in accounts if x.get("type") == "asset")
        return D.jout({"ok": False, "reason_code": "casual_wallet_exists",
                       "error": f"随手档全库只有一个资产账户（现有的叫「{holder}」），不再新增资产户",
                       "hint": "随手记账直接用现有账户；要分账户就把 config.json 的 record_mode 改为 detailed"})
    row = {"name": name, "type": a.type, "in_net_worth": in_net, "note": a.note,
           "_id": D.row_id(), "created_at": D.now_iso(), "recorded_at": D.now_iso()[:10]}
    if a.dry_run:
        return D.jout({"ok": True, "dry_run": True, "would_append": row})
    D.append_row(D.table_path("accounts"), row)
    return D.jout({"ok": True, "added": name, "type": a.type, "in_net_worth": in_net,
                   "_id": row["_id"], "user_view": f"开户好了：「{name}」（{a.type}）。"})


def cmd_accounts_list(_a) -> int:
    accounts = load_accounts()
    disp, _, orphans, malformed = derive(accounts, load_txns())
    order = {t: i for i, t in enumerate(TYPE_ORDER)}
    rows = sorted(accounts, key=lambda x: (order.get(x.get("type"), 9), str(x.get("name", ""))))
    return D.jout({"ok": True,
                   "record_mode": read_config().get("record_mode", "detailed"),
                   "count": len(rows),
                   "accounts": [{"name": x.get("name"), "type": x.get("type"),
                                 "in_net_worth": bool(x.get("in_net_worth")),
                                 "note": x.get("note"),
                                 "balance": disp.get(x.get("name"), 0.0)} for x in rows],
                   "warnings": {"orphan_txns": len(orphans), "malformed_txns": len(malformed)}})


# ---------- record ----------

def cmd_record(a) -> int:
    base: dict = {}
    if a.json:
        try:
            base = json.loads(a.json)
        except json.JSONDecodeError as e:
            return D.jfail(f"--json 解析失败：{e}")
        if not isinstance(base, dict):
            return D.jfail("--json 应为 JSON 对象")
    # 显式 flag 优先于 --json 字段
    f = a.from_account or base.get("from_account")
    to = a.to_account or base.get("to_account")
    amt = a.amount if a.amount is not None else base.get("amount")
    date = a.date or base.get("date") or D.now_iso()[:10]
    note = a.note if a.note is not None else base.get("note")
    raw = a.raw_text if a.raw_text is not None else base.get("raw_text")
    source = a.source or base.get("source") or "record"
    dedup = a.dedup_key if a.dedup_key is not None else base.get("dedup_key")

    if not isinstance(amt, (int, float)) or isinstance(amt, bool):
        return D.jfail(f"金额必须为数值，现在是 {amt!r}")
    orig_amt, amt = amt, round(float(amt), 2)
    if amt <= 0:  # 先 round 再校验：0.001 这类微额 round 到分为 0，落库即幽灵流水（D4）
        return D.jout({"ok": False, "reason_code": "amount_too_small",
                       "error": f"金额必须为正数（方向只由 from/to 表达）；"
                                f"{orig_amt!r} 四舍五入到分为 {amt}，拒绝落库"}, code=1)
    if not f or not to:
        return D.jfail("from 与 to 都必填")
    if f == to:
        return D.jfail(f"from 与 to 不能相同：{f}")
    ndate = D.norm_date(date)  # 宽容归一：'2026-8-5' 存成 '2026-08-05'，杜绝按月/按日推导静默丢失
    if not ndate:
        return D.jfail(f"date 应为 YYYY-MM-DD，现在是 {date!r}")
    if source not in ("record", "import"):
        return D.jfail(f"source 必须是 record/import，现在是 {source!r}")

    accounts = load_accounts()
    known = {x.get("name") for x in accounts}
    missing = [x for x in (f, to) if x not in known]
    if missing:
        return D.jout({"ok": False, "reason_code": "unknown_account",
                       "error": f"账户不存在：{'、'.join(missing)}",
                       "missing": missing, "known_accounts": _known_names(accounts),
                       "hint": "先 accounts add 开户，或与主人确认正确账户名"})
    if dedup:  # 幂等：同去重键已在账 → 跳过（导入重复场景主用）
        dup = next((t for t in load_txns() if t.get("dedup_key") == dedup), None)
        if dup:
            return D.jout({"ok": True, "duplicate": True, "existing_id": dup.get("_id"),
                           "user_view": "这笔已经记过了（去重键相同），跳过。"})
    row = {"date": ndate, "from_account": f, "to_account": to, "amount": amt,
           "note": note, "raw_text": raw, "source": source, "dedup_key": dedup,
           "_id": D.row_id(), "created_at": D.now_iso(), "recorded_at": D.now_iso()}
    if a.dry_run:
        return D.jout({"ok": True, "dry_run": True, "would_record": row})
    D.append_row(D.table_path("txns"), row)
    disp, _, _, _ = derive(accounts, load_txns())
    return D.jout({"ok": True, "recorded": row["_id"], "txn": row,
                   "balance_after": {f: disp.get(f, 0.0), to: disp.get(to, 0.0)},
                   "user_view": f"记好了：{f} → {to} {amt} 元。"})


# ---------- 余额 / 净资产 / 试算 ----------

def cmd_balance(a) -> int:
    accounts = load_accounts()
    txns = load_txns()
    disp, _, orphans, malformed = derive(accounts, txns)
    if a.account:
        acct = find_account(accounts, a.account)
        if not acct:
            return D.jout({"ok": False, "reason_code": "unknown_account",
                           "error": f"账户「{a.account}」不存在",
                           "known_accounts": _known_names(accounts)})
        b = disp.get(a.account, 0.0)
        out = {"name": a.account, "type": acct.get("type"),
               "in_net_worth": bool(acct.get("in_net_worth")), "balance": b}
        v = latest_valuations().get(a.account)
        if v:
            out["latest_valuation"] = {"date": v.get("date"), "value": v.get("value")}
        return D.jout({"ok": True, "account": out,
                       "user_view": f"{a.account} 现在账面 {b} 元。"})
    order = {t: i for i, t in enumerate(TYPE_ORDER)}
    rows = sorted(accounts, key=lambda x: (order.get(x.get("type"), 9), str(x.get("name", ""))))
    items = [{"name": x.get("name"), "type": x.get("type"),
              "balance": disp.get(x.get("name"), 0.0)} for x in rows]
    totals = {t: round(sum(i["balance"] for i in items if i["type"] == t), 2) for t in TYPE_ORDER}
    return D.jout({"ok": True, "balances": items, "totals": totals,
                   "warnings": {"orphan_txns": len(orphans), "malformed_txns": len(malformed)}})


def cmd_net_worth(_a) -> int:
    today = D.now_iso()[:10]
    accounts = load_accounts()
    disp, _, orphans, malformed = derive(accounts, load_txns())
    vals = latest_valuations(today)  # 只采信 date ≤ 今天的估值快照（D6，与月报口径对齐）
    assets: list[dict] = []
    liabilities: list[dict] = []
    investments: list[dict] = []
    for x in sorted(accounts, key=lambda y: str(y.get("name", ""))):
        n, t = x.get("name"), x.get("type")
        b = disp.get(n, 0.0)
        if t == "asset":
            v = vals.get(n)
            nv = _num(v.get("value")) if v else None
            e = {"name": n, "ledger_balance": b,
                 "value": round(nv, 2) if nv is not None else b,
                 "value_source": "valuation" if nv is not None else "ledger",
                 "valuation_date": v.get("date") if nv is not None else None,
                 "in_net_worth": bool(x.get("in_net_worth"))}
            assets.append(e)
            if nv is not None:
                investments.append(e)
        elif t == "liability":
            liabilities.append({"name": n, "balance": b,
                                "in_net_worth": bool(x.get("in_net_worth"))})
    ta = round(sum(e["value"] for e in assets if e["in_net_worth"]), 2)
    tl = round(sum(e["balance"] for e in liabilities if e["in_net_worth"]), 2)
    net = round(ta - tl, 2)
    today = D.now_iso()[:10]
    return D.jout({"ok": True, "as_of": today,
                   "assets": assets, "liabilities": liabilities, "investments": investments,
                   "totals": {"assets": ta, "liabilities": tl, "net_worth": net},
                   "excluded": {"assets": [e["name"] for e in assets if not e["in_net_worth"]],
                                "liabilities": [e["name"] for e in liabilities if not e["in_net_worth"]]},
                   "warnings": {"orphan_txns": len(orphans), "malformed_txns": len(malformed)},
                   "user_view": f"净资产 {net:,.2f} 元（资产 {ta:,.2f} − 计入负债 {tl:,.2f}），截至 {today}。"})


def cmd_trial_balance(_a) -> int:
    accounts = load_accounts()
    txns = load_txns()
    disp, unif, orphans, malformed = derive(accounts, txns)
    total = round(sum(unif.values()), 2)
    bad_types = [a.get("name") for a in accounts if a.get("type") not in SIGN]
    balanced = abs(total) <= UNBALANCED_EPS and not orphans and not malformed and not bad_types
    order = {t: i for i, t in enumerate(TYPE_ORDER)}
    rows = sorted(accounts, key=lambda x: (order.get(x.get("type"), 9), str(x.get("name", ""))))
    out: dict = {"balanced": balanced, "uniform_total": total, "threshold": UNBALANCED_EPS,
                 "accounts": [{"name": x.get("name"), "type": x.get("type"),
                               "balance": disp.get(x.get("name"), 0.0),
                               "uniform": round(unif.get(x.get("name"), 0.0), 2)} for x in rows],
                 "orphans": [{"_id": t.get("_id"), "date": t.get("date"),
                              "from_account": t.get("from_account"),
                              "to_account": t.get("to_account"), "amount": t.get("amount")}
                             for t in orphans],
                 "malformed": [{"_id": t.get("_id")} for t in malformed],
                 "bad_type_accounts": bad_types}
    if balanced:
        out.update({"ok": True, "user_view": "试算平衡：全部账户代数和为 0，账是自洽的。"})
    else:
        out.update({"ok": False, "reason_code": "unbalanced",
                    "error": ("试算不平衡："
                              + (f"全部账户未符号代数和为 {total}（阈值 {UNBALANCED_EPS}）"
                                 if abs(total) > UNBALANCED_EPS
                                 else "守恒代数和为 0，但存在指向已不存在账户的流水/坏行/类型非法账户")
                              + (f"；{len(orphans)} 笔孤儿流水" if orphans else "")
                              + (f"；{len(malformed)} 笔坏行" if malformed else "")
                              + (f"；类型非法的账户：{'、'.join(map(str, bad_types))}" if bad_types else "")),
                    "hint": "多半有流水指向被删/改名的账户，或流水行被徒手改坏；嫌疑清单见 orphans/malformed/bad_type_accounts"})
    return D.jout(out)


# ---------- assert（余额断言，A1） ----------

def cmd_assert(a) -> int:
    accounts = load_accounts()
    acct = find_account(accounts, a.account)
    if not acct:
        return D.jout({"ok": False, "reason_code": "unknown_account",
                       "error": f"账户「{a.account}」不存在",
                       "known_accounts": _known_names(accounts)})
    as_of = D.now_iso()[:10]
    if a.date:  # 垃圾/非补零日期会静默改变期末语义，必须先归一（D1②，与 record 同校验）
        as_of = D.norm_date(a.date)
        if not as_of:
            return D.jfail(f"--date 应为 YYYY-MM-DD，现在是 {a.date!r}")
    disp, _, _, _ = derive(accounts, load_txns(), as_of=as_of)
    derived = disp.get(a.account, 0.0)
    reported = round(float(a.balance), 2)
    diff = round(reported - derived, 2)
    base = {"account": a.account, "as_of": as_of, "reported": reported, "derived": derived}
    if abs(diff) <= EPS:
        return D.jout({"ok": True, "matched": True, "difference": 0.0, **base,
                       "user_view": f"对上了：截至 {as_of}，{a.account} 账本推导 {derived} 元，与您报的一致。"})
    # 不平 → 附该账户近期流水帮找嫌疑
    recent: list[dict] = []
    for t in sorted(load_txns(),
                    key=lambda x: (str(x.get("date", "")), str(x.get("created_at", ""))), reverse=True):
        if len(recent) >= 8:
            break
        if t.get("from_account") == a.account:
            recent.append({"date": t.get("date"), "direction": "out",
                           "counterparty": t.get("to_account"), "amount": t.get("amount"),
                           "note": t.get("note"), "_id": t.get("_id")})
        elif t.get("to_account") == a.account:
            recent.append({"date": t.get("date"), "direction": "in",
                           "counterparty": t.get("from_account"), "amount": t.get("amount"),
                           "note": t.get("note"), "_id": t.get("_id")})
    return D.jout({"ok": False, "reason_code": "assertion_failed", "matched": False,
                   "difference": diff, "suspects": recent, **base,
                   "hint": ("核对近期流水是否漏记/多记/金额错；确认后用 reverse 冲正重记，"
                            "或补记一笔差额（期初权益 → 账户）"),
                   "user_view": f"对不上：截至 {as_of}，{a.account} 账本推导 {derived} 元，"
                                f"您报 {reported} 元，差 {diff} 元。"})


# ---------- suggest（分类记忆建议，A2） ----------

def cmd_suggest(a) -> int:
    text = (a.text or "").strip()
    if not text:
        return D.jfail("--text 不能为空")
    txns = load_txns()
    tmap = {x.get("name"): x.get("type") for x in load_accounts()}

    def hits_for(kw: str) -> list[dict]:
        kw = kw.lower()
        if not kw:
            return []
        out = []
        for t in txns:
            hay = " ".join(str(t.get(k) or "") for k in ("note", "raw_text")).lower()
            if kw in hay:
                out.append(t)
        return out

    hits = hits_for(text)
    if not hits and " " in text:  # 整句无命中时按词降级匹配
        seen: set = set()
        for tok in [w for w in text.lower().split() if len(w) >= 2]:
            for t in hits_for(tok):
                if t.get("_id") not in seen:
                    seen.add(t.get("_id"))
                    hits.append(t)
    if not hits:
        return D.jout({"ok": False, "reason_code": "no_history",
                       "error": f"历史流水里没有匹配「{text}」的记录",
                       "hint": "照常问主人或用默认类别入账；这类记忆会随记账积累"})

    def classify(t: dict) -> tuple[str, str | None, str | None]:
        ft, tt = tmap.get(t.get("from_account")), tmap.get(t.get("to_account"))
        if tt == "expense":
            return "expense", t.get("to_account"), t.get("from_account")
        if ft == "income":
            return "income", t.get("from_account"), t.get("to_account")
        return "transfer", None, f"{t.get('from_account')} → {t.get('to_account')}"

    agg: dict = {}
    for t in hits:
        kind, cat, acct = classify(t)
        e = agg.setdefault((kind, cat, acct),
                           {"kind": kind, "category": cat, "account": acct,
                            "count": 0, "last_date": "", "last_amount": 0.0})
        e["count"] += 1
        d = str(t.get("date", ""))
        if d >= e["last_date"]:
            e["last_date"] = d
            e["last_amount"] = t.get("amount")
    cands = sorted(agg.values(), key=lambda e: (e["count"], e["last_date"]), reverse=True)[:5]
    top = cands[0]
    if top["kind"] == "expense":
        record_as = {"direction": "out", "from_account": top["account"], "to_account": top["category"]}
        view = f"历史上「{text}」最常记为 {top['category']}、从 {top['account']} 出（命中 {top['count']} 次）。"
    elif top["kind"] == "income":
        record_as = {"direction": "in", "from_account": top["category"], "to_account": top["account"]}
        view = f"历史上「{text}」最常记为 {top['category']}、进 {top['account']}（命中 {top['count']} 次）。"
    else:
        record_as = None
        view = f"历史上「{text}」是转账类记录（命中 {top['count']} 次），无收支类别可建议。"
    return D.jout({"ok": True, "text": text, "matched": len(hits),
                   "suggestion": {**top, "record_as": record_as},
                   "candidates": cands, "user_view": view})


# ---------- reverse（冲正） ----------

def cmd_reverse(a) -> int:
    txns = load_txns()
    orig = next((t for t in txns if t.get("_id") == a.id), None)
    if not orig:
        return D.jout({"ok": False, "reason_code": "txn_not_found",
                       "error": f"找不到 _id={a.id} 的流水",
                       "hint": "data.py query txns --last 20 可查近期流水拿 _id"})
    prior = next((t for t in txns if t.get("reversed_txn") == a.id), None)
    if prior:
        return D.jout({"ok": False, "reason_code": "already_reversed",
                       "error": f"该笔已被冲正（冲正单 {prior.get('_id')}），不再重复冲正"})
    if orig.get("reversed_txn"):  # 目标自身是冲正单：再冲正=等价复活原单，审计链自相矛盾（D5）
        return D.jout({"ok": False, "reason_code": "already_reversed",
                       "error": f"该笔本身是冲正单（冲正的是 {orig.get('reversed_txn')}），不可再冲正",
                       "hint": "如需撤销这笔冲正的效果，请照原单重新记一笔正向流水"})
    rdate = a.date or str(orig.get("date", ""))
    nrdate = D.norm_date(rdate)  # 原单若是历史非补零行，冲正单一并归一（D1①）
    if not nrdate:
        return D.jfail(f"--date 应为 YYYY-MM-DD，现在是 {rdate!r}")
    note = f"冲正 {a.id}" + (f"（{a.reason}）" if a.reason else "")
    row = {"date": nrdate, "from_account": orig.get("to_account"),
           "to_account": orig.get("from_account"), "amount": orig.get("amount"),
           "note": note, "raw_text": orig.get("raw_text"), "source": "record",
           "reversed_txn": a.id,
           "_id": D.row_id(), "created_at": D.now_iso(), "recorded_at": D.now_iso()}
    if a.dry_run:
        return D.jout({"ok": True, "dry_run": True, "would_record": row})
    D.append_row(D.table_path("txns"), row)
    return D.jout({"ok": True, "reversed": a.id, "reversal": row,
                   "user_view": f"已冲正：等额反向一笔（{row['from_account']} → {row['to_account']} "
                                f"{row['amount']} 元），原单原样保留可查。"})


# ---------- 看板取数（data.py serve 延迟调用，推导唯一真相在此） ----------

def month_net_agg(txns: list[dict], tmap: dict, mkey: str) -> dict:
    """某月收支聚合——**净额口径**（纯函数，selfcheck 验数学）。

    与 budget.month_net_by_account / report.expense_by_category 同一真相：
    每笔流水对科目是"流入 +amt / 流出 −amt"，冲正/退款行（如 支出·餐饮 → 微信零钱）
    自然抵减原单，而不是被毛额口径漏掉。坏行（金额非数值/缺方向/日期不可解析）整行跳过，
    与 budget.month_net_by_account 同滤；历史非补零日期行（如 '2026-8-5'）经宽容归一
    照常计入当月，不在聚合中静默消失。净额归零（|v|≤EPS）的类别不留行，
    与 report.expense_by_category 同滤——三处口径逐位可对账。
    """
    inc: dict[str, float] = {}
    exp: dict[str, float] = {}
    for t in txns:
        nd = D.norm_date(t.get("date"))
        if nd is None or nd[:7] != mkey:
            continue
        amt = t.get("amount")
        f, to = t.get("from_account"), t.get("to_account")
        if not isinstance(amt, (int, float)) or isinstance(amt, bool) or not f or not to:
            continue
        amt = float(amt)
        if tmap.get(to) == "expense":
            exp[to] = exp.get(to, 0.0) + amt
        if tmap.get(f) == "expense":
            exp[f] = exp.get(f, 0.0) - amt
        if tmap.get(f) == "income":
            inc[f] = inc.get(f, 0.0) + amt
        if tmap.get(to) == "income":
            inc[to] = inc.get(to, 0.0) - amt
    inc = {k: round(v, 2) for k, v in inc.items() if abs(v) > EPS}
    exp = {k: round(v, 2) for k, v in exp.items() if abs(v) > EPS}
    i_total, e_total = round(sum(inc.values()), 2), round(sum(exp.values()), 2)
    return {"month": mkey, "income": i_total, "expense": e_total,
            "net": round(i_total - e_total, 2),
            "income_by_category": inc, "expense_by_category": exp}


def serve_snapshot() -> dict:
    today = D.now_iso()[:10]
    accounts = load_accounts()
    txns = load_txns()
    disp, _, orphans, malformed = derive(accounts, txns)
    vals = latest_valuations(today)  # 只采信 date ≤ 今天的估值快照（D6，与月报口径对齐）
    tmap = {a.get("name"): a.get("type") for a in accounts}

    balances = [{"name": a.get("name"), "type": a.get("type"),
                 "in_net_worth": bool(a.get("in_net_worth")),
                 "balance": disp.get(a.get("name"), 0.0)} for a in accounts]

    assets: list[dict] = []
    liabilities: list[dict] = []
    net_a = net_l = 0.0
    for e in balances:
        if e["type"] == "asset":
            v = vals.get(e["name"])
            nv = _num(v.get("value")) if v else None
            e2 = {**e, "value": round(nv, 2) if nv is not None else e["balance"],
                  "value_source": "valuation" if nv is not None else "ledger"}
            assets.append(e2)
            if e["in_net_worth"]:
                net_a += e2["value"]
        elif e["type"] == "liability":
            liabilities.append(e)
            if e["in_net_worth"]:
                net_l += e["balance"]

    def month_agg(mkey: str) -> dict:
        return month_net_agg(txns, tmap, mkey)

    def last_months(n: int) -> list[str]:
        y, m = int(today[:4]), int(today[5:7])
        out = []
        for i in range(n - 1, -1, -1):
            mm, yy = m - i, y
            while mm <= 0:
                mm += 12
                yy -= 1
            out.append(f"{yy:04d}-{mm:02d}")
        return out

    recent = sorted(txns,
                    key=lambda t: (str(t.get("date", "")), str(t.get("created_at", ""))),
                    reverse=True)[:20]
    return {"as_of": today,
            "balances": balances,
            "net_worth": {"assets": assets, "liabilities": liabilities,
                          "assets_total": round(net_a, 2),
                          "liabilities_total": round(net_l, 2),
                          "net_worth": round(net_a - net_l, 2)},
            "this_month": month_agg(today[:7]),
            "cashflow_6m": [month_agg(mk) for mk in last_months(6)],
            "recent_txns": [{k: t.get(k) for k in
                             ("date", "from_account", "to_account", "amount", "note", "source", "_id")}
                            for t in recent],
            "warnings": {"orphan_txns": len(orphans), "malformed_txns": len(malformed)}}


# ---------- selfcheck ----------

def cmd_selfcheck(_a) -> int:
    problems: list[str] = []
    # 1) 表可读（未 init 时按空表处理，不视为故障；init 由 data.py 负责）
    for t in ("accounts", "txns", "valuations"):
        try:
            D.read_rows(D.table_path(t))
        except Exception as e:  # noqa: BLE001
            problems.append(f"表 {t} 读取失败：{e}")
    # 2) 推导数学：合成迷你账本验符号约定与守恒恒等式（纯内存，不落盘）
    accts = [{"name": n, "type": t} for n, t in
             [("期初权益", "equity"), ("微信零钱", "asset"), ("支出·餐饮", "expense"),
              ("收入·工资", "income"), ("花呗", "liability")]]
    txns = [
        {"_id": "1", "date": "2026-09-01", "from_account": "期初权益", "to_account": "微信零钱", "amount": 800},
        {"_id": "2", "date": "2026-09-02", "from_account": "微信零钱", "to_account": "支出·餐饮", "amount": 32},
        {"_id": "3", "date": "2026-09-03", "from_account": "收入·工资", "to_account": "微信零钱", "amount": 15000},
        {"_id": "4", "date": "2026-09-04", "from_account": "花呗", "to_account": "支出·餐饮", "amount": 400},
        {"_id": "5", "date": "2026-09-05", "from_account": "微信零钱", "to_account": "花呗", "amount": 2000},
    ]
    disp, unif, _, _ = derive(accts, txns)
    expect = {"微信零钱": 13768.0, "支出·餐饮": 432.0, "收入·工资": 15000.0,
              "花呗": -1600.0, "期初权益": 800.0}
    for k, v in expect.items():
        if abs(disp.get(k, 0.0) - v) > EPS:
            problems.append(f"推导数学错误：{k} 应为 {v}，算得 {disp.get(k)}")
    if abs(sum(unif.values())) > UNBALANCED_EPS:
        problems.append(f"守恒恒等式不成立：合成账本代数和 {round(sum(unif.values()), 2)}")
    d2, u2, orph2, _ = derive(accts, txns + [{"_id": "6", "date": "2026-09-06",
                                              "from_account": "不存在的户头",
                                              "to_account": "微信零钱", "amount": 5}])
    # 孤儿流水按构造不进代数桶（总和仍为 0），试算平衡翻红必须靠孤儿标记本身：
    # 此处复算 cmd_trial_balance 同款判定谓词，验证它对脏数据判为"不平衡"
    if not orph2:
        problems.append("孤儿流水未被识别")
    tb_would_say_balanced = (abs(round(sum(u2.values()), 2)) <= UNBALANCED_EPS and not orph2)
    if tb_would_say_balanced:
        problems.append("试算平衡判定未抓出孤儿流水")
    # 2b) 月度收支聚合：净额口径（看板 serve 与 budget check / monthly 报告同一真相，
    #     冲正/退款必须抵减原单，评审①回归）
    tmap_s = {a["name"]: a["type"] for a in accts}
    agg = month_net_agg(txns, tmap_s, "2026-09")
    if abs(agg["expense"] - 432.0) > EPS or abs(agg["expense_by_category"].get("支出·餐饮", 0) - 432.0) > EPS \
            or abs(agg["income"] - 15000.0) > EPS:
        problems.append(f"月度聚合基数错：{agg}")
    refund_part = {"_id": "7", "date": "2026-09-06", "from_account": "支出·餐饮",
                   "to_account": "微信零钱", "amount": 400}
    agg_p = month_net_agg(txns + [refund_part], tmap_s, "2026-09")
    if abs(agg_p["expense"] - 32.0) > EPS or abs(agg_p["expense_by_category"].get("支出·餐饮", 0) - 32.0) > EPS:
        problems.append(f"部分冲正未按净额抵减支出：{agg_p}")
    refund_all = {"_id": "8", "date": "2026-09-07", "from_account": "支出·餐饮",
                  "to_account": "微信零钱", "amount": 32}
    agg_f = month_net_agg(txns + [refund_part, refund_all], tmap_s, "2026-09")
    if agg_f["expense"] != 0 or "支出·餐饮" in agg_f["expense_by_category"]:
        problems.append(f"全额冲正后支出应归零且不留行：{agg_f}")
    if abs((agg_f["income"] - agg_f["expense"]) - agg_f["net"]) > EPS:
        problems.append(f"月度净额恒等式不成立：{agg_f}")
    # 2c) 日期容错（D1 回归）：非补零日期行经 norm_date 归一照常计入，垃圾日期行进 malformed
    drows = txns + [
        {"_id": "9", "date": "2026-9-6", "from_account": "微信零钱",
         "to_account": "支出·餐饮", "amount": 10},   # 非补零 → 归一为 2026-09-06 计入
        {"_id": "10", "date": "not-a-date", "from_account": "微信零钱",
         "to_account": "支出·餐饮", "amount": 20},   # 垃圾日期 → suspects（malformed），不静默计数
    ]
    disp_d, _, _, mal_d = derive(accts, drows)
    if abs(disp_d.get("支出·餐饮", 0.0) - 442.0) > EPS or abs(disp_d.get("微信零钱", 0.0) - 13758.0) > EPS:
        problems.append(f"非补零日期行未被宽容归一计入推导：{disp_d}")
    if len(mal_d) != 1:
        problems.append(f"垃圾日期行应进 malformed（suspects 机制），得 {len(mal_d)} 行")
    disp_e, _, _, _ = derive(accts, drows, as_of="2026-09-05")
    if abs(disp_e.get("微信零钱", 0.0) - 13768.0) > EPS:
        problems.append(f"as_of 封顶对归一后日期比较失效：{disp_e.get('微信零钱')}")
    agg_d = month_net_agg(drows, tmap_s, "2026-09")
    if abs(agg_d["expense_by_category"].get("支出·餐饮", 0) - 442.0) > EPS:
        problems.append(f"月聚合未宽容归一非补零日期行：{agg_d}")
    # 3) 真实账本的试算状态（数据状况只报告，不算工具故障）
    real_accounts, real_txns = load_accounts(), load_txns()
    _, runif, rorph, rmal = derive(real_accounts, real_txns)
    trial_ok = abs(sum(runif.values())) <= UNBALANCED_EPS and not rorph and not rmal
    if problems:
        return D.jout({"ok": False, "error": "；".join(problems)}, code=1)
    return D.jout({"ok": True, "agent": D.manifest()["id"], "tool": "ledger",
                   "derive_math": "verified", "month_agg_math": "verified",
                   "real_trial_balanced": trial_ok,
                   "real_accounts": len(real_accounts), "real_txns": len(real_txns)})


def main() -> int:
    ap = argparse.ArgumentParser(description="复式记账引擎（从哪来 → 到哪去）")
    ap.add_argument("--selfcheck", action="store_true", help="自检（收编校验契约，等价于子命令 selfcheck）")
    sub = ap.add_subparsers(dest="cmd", required=False)

    p_acc = sub.add_parser("accounts")
    acc_sub = p_acc.add_subparsers(dest="action", required=True)
    p_add = acc_sub.add_parser("add")
    p_add.add_argument("--name", required=True, help="账户或科目名，如 微信零钱 / 支出·餐饮")
    p_add.add_argument("--type", required=True, help="asset/liability/income/expense/equity")
    p_add.add_argument("--in-net-worth", dest="in_net_worth", default=None,
                       help="是否计入净资产，缺省 asset/liability=true、其余强制 false")
    p_add.add_argument("--note", default=None)
    p_add.add_argument("--dry-run", action="store_true")
    acc_sub.add_parser("list")

    p_r = sub.add_parser("record")
    p_r.add_argument("--from", dest="from_account", default=None)
    p_r.add_argument("--to", dest="to_account", default=None)
    p_r.add_argument("--amount", type=float, default=None)
    p_r.add_argument("--date", default=None, help="业务日期 YYYY-MM-DD，缺省今天")
    p_r.add_argument("--note", default=None, help="备注（对方/事项）")
    p_r.add_argument("--raw-text", dest="raw_text", default=None, help="主人原话")
    p_r.add_argument("--source", default=None, choices=["record", "import"])
    p_r.add_argument("--dedup-key", dest="dedup_key", default=None, help="导入去重键，同键跳过")
    p_r.add_argument("--json", default=None, help="整笔交易 JSON（flag 显式给出时优先）")
    p_r.add_argument("--dry-run", action="store_true")

    p_b = sub.add_parser("balance")
    p_b.add_argument("--account", default=None, help="缺省输出全部账户余额表")

    sub.add_parser("net-worth")
    sub.add_parser("trial-balance")

    p_a = sub.add_parser("assert")
    p_a.add_argument("--account", required=True)
    p_a.add_argument("--balance", type=float, required=True, help="主人报的期末余额")
    p_a.add_argument("--date", default=None, help="截至日期 YYYY-MM-DD，缺省今天")

    p_s = sub.add_parser("suggest")
    p_s.add_argument("--text", required=True, help="备注/对方关键词")

    p_v = sub.add_parser("reverse")
    p_v.add_argument("--id", required=True, help="要冲正的流水 _id")
    p_v.add_argument("--date", default=None, help="冲正业务日期，缺省随原单")
    p_v.add_argument("--reason", default=None)
    p_v.add_argument("--dry-run", action="store_true")

    sub.add_parser("selfcheck")

    x = ap.parse_args()
    if x.selfcheck:
        return cmd_selfcheck(x)
    if not x.cmd:
        ap.print_help()
        return D.jout({"ok": False, "error": "缺少子命令（或用 --selfcheck）"}, code=2)
    if x.cmd == "accounts":
        return {"add": cmd_accounts_add, "list": cmd_accounts_list}[x.action](x)
    return {"record": cmd_record, "balance": cmd_balance, "net-worth": cmd_net_worth,
            "trial-balance": cmd_trial_balance, "assert": cmd_assert, "suggest": cmd_suggest,
            "reverse": cmd_reverse, "selfcheck": cmd_selfcheck}[x.cmd](x)


if __name__ == "__main__":
    sys.exit(main())
