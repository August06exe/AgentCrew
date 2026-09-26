#!/usr/bin/env python3
"""import_bills.py — 微信/支付宝账单 CSV 导入（设计稿 §6 / §13-A4）。

流水线：解析（表头嗅探+坏行计数）→ 归一化（日期/对方/商品/收支/金额，转账与退款单独标记）
→ 去重（dedup_key 幂等）→ 双花对消（--match-existing）→ 待确认清单（缺省 dry-run）
→ --apply 落库（复用 ledger.record，source=import）→ --assert-balance 期末余额断言。

对消幂等（manifest.cancel_log）：被对消的账单行不落 txns，但 --apply 时逐笔记入
cancel_log 痕迹表——此后无论带不带 --match-existing 重导（换旗），同键账单行一律
按"已在对消痕迹"跳过，同一经济事件绝不记第二遍；dry-run 零写入。

用法：
  # 第一步永远是看清单（缺省 dry-run，一个字节都不落库）
  python tools/import_bills.py 微信账单.csv 支付宝账单.csv --match-existing \
      --map 零钱=微信零钱 --map 余额=支付宝余额 --account 微信零钱 \
      --expense-category 支出·未分类 --income-category 收入·其他
  # 确认清单后落库
  python tools/import_bills.py 微信账单.csv ... --apply
  # 期末余额断言：不给值则从账单"余额行"解析（也可主人报数）
  python tools/import_bills.py 微信账单.csv --apply --assert-account 微信零钱 --assert-balance
  python tools/import_bills.py 微信账单.csv --apply --assert-account 微信零钱 --assert-balance 158.88
  python tools/import_bills.py --selfcheck

选项：
  files...                  账单 CSV，可多文件同跑（跨账单对消就为这个场景：后一账单
                            会与前一账单的待确认行对消）
  --platform auto|wechat|alipay   缺省按表头特征自动嗅探，不写死行号
  --match-existing          双花对消：与本账已有流水（日期±3天 + 同金额 + 同向）对消跳过，
                            对消清单逐笔列出供主人复核
  --map K=V                 支付方式 → 账本账户名（可多次），如 零钱=微信零钱
  --account NAME            未映射支付方式的兜底资金账户（支出作 from、收入作 to）
  --expense-category NAME   支出缺省科目（suggest 无历史时兜底）
  --income-category NAME    收入缺省科目
  --refund-category NAME    退款冲抵科目（缺省回落 expense-category）
  --apply                   真正落库；缺省 dry-run（--apply 与 --dry-run 同给时 dry-run 优先）
  --assert-account NAME     期末余额断言的账户
  --assert-balance [VAL]    期末余额：不带值=从账单余额行解析；带值=直接用
  --assert-date YYYY-MM-DD  断言基准日（缺省账单截止日，再退最晚流水日）
  --selfcheck               自检：临时目录造假 CSV 全流程走一遍，零残留

设计要点（与任务书的偏差均已注明理由）：
  * 去重键 = sha256(平台|完整时间戳|流向|种类|金额|对方|商品)[:20]，前缀平台名。
    任务书给的"日期+金额+对方+商品"补了三味药：完整时间戳（同日两笔同额消费不误伤）、
    流向+种类（当日全额退款与原支出不撞键）、平台前缀。
  * 对消在"±3天+同金额"之上加**同向守卫**（FLOW_COMPAT）：退款绝不与支出对消
    （否则会把真到账的退款吞掉）；账本侧手记退款（支出科目→资金账户）判为 refund
    可与账单退款行对消（真实账户间转账判为 transfer，与退款互不对消）；转账可与
    任意向对消（同一笔还款在两家账单里一边记支出、一边记中性是常态）。
    每条已有流水/待确认行至多被对消一次。
  * 转账/还款行（kind=transfer）永不自动落库——from/to 无从可靠推断，进 manual 清单，
    提示用 ledger.py record 手工记（主人确认方向）或与另一张账单对消。
  * 退款行按复式记为"支出科目 → 账户"（冲减支出），备注加"退款："前缀；
    「原支出行状态含已全额退款」形态照记支出（标记已退款），apply 时自动补一笔
    同额冲减分录（note 标注自动冲减并关联原行；dry-run 以 would_record_refund 预览），
    退款的钱必须回账（D3）。
  * 落库与余额推导全部复用 ledger.py / data.py（单一记账真相），本文件不另设账。

微信账单"格式假设"（测试造假照此造；真实导出不符时以技术错误/坏行计数暴露）：
  * 编码 UTF-8 带 BOM；前若干行为说明区（账单名/起止时间/汇总/分隔线），表头不在首行；
  * 表头：交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注
  * 金额带 ¥ 前缀、千分位逗号；收/支 ∈ {收入, 支出, "/"(中性)}；
  * 退款两种形态：独立行（类型含"退款"、收/支为"/"）或原支出行状态含"已全额退款"；
  * 余额行（个人导出常缺，属可选）形如「账户余额：¥158.88」（全/半角冒号均可）。
支付宝账单"格式假设"：
  * 编码 GBK；文件头若干说明行 + 「--[交易记录明细]--」分隔线，表头不在首行，末尾有结束线；
  * 新版表头：交易时间,业务类型,交易对方,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注
  * 旧版兼容：交易创建时间 / 类型 / 商品名称 / 金额（元）（全角括号自动归一）/ 交易号 / 资金状态；
  * 收/支 ∈ {收入, 支出, 不计收支}；金额为纯数字（个别版本带 ¥，已兼容）；
  * 余额行（可选）形如「期末余额：123.45」。
  嗅探不依赖行号：逐行找"同时具备 时间列+对方列+商品列+收支列+金额列"的首行作表头；
  编码按 utf-8-sig → gbk → utf-16 依次试解；个别字段无值以 "/" 或 "-" 占位。
  转账类关键词：转账/提现/充值/还款/代付/划转（红包有明确收支方向，按普通收/支入账）。
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
import re
import sys
from datetime import date as _date
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data as D  # noqa: E402  （存档解析器与 jout/jfail，范式 §5/§6）
import ledger as L  # noqa: E402  （记账唯一真相：record/derive/EPS）

# ---------- 常量 ----------

TRANSFER_KW = ("转账", "提现", "充值", "还款", "代付", "划转")
REQUIRED_FIELDS = ("date", "counterparty", "product", "flow", "amount")
# 列名别名（匹配时全角括号/空格已归一）；首列命中即用
FIELD_ALIASES = {
    "date": ["交易时间", "交易创建时间"],
    "type": ["交易类型", "业务类型", "类型"],
    "counterparty": ["交易对方"],
    "product": ["商品", "商品说明", "商品名称"],
    "flow": ["收/支"],
    "amount": ["金额(元)", "金额"],
    "pay": ["支付方式", "收/付款方式", "付款方式"],
    "status": ["当前状态", "交易状态"],
    "note": ["备注"],
    "order": ["交易单号", "交易订单号", "交易号"],
}
WECHAT_HINTS = ("交易类型", "当前状态", "支付方式")
# 对消同向守卫：账单行流向 → 允许对消的账本/先前账单行流向
# （refund 含 "in"：跨文件退款行以 make_row 的 flow="in" 进池；含 "refund"：账本手记退款，
#   见 ledger_flow——章程"退款=方向倒过来再记一笔"即 支出科目→资金账户 形态。
#   绝不含 "out"：退款绝不与支出对消，防吞掉真到账的退款；也绝不含 "transfer"：
#   真实账户间转账（还款/互转）与退款是两码事，不得互相吞单。）
FLOW_COMPAT = {
    "out": {"out", "transfer"},
    "in": {"in", "transfer"},
    "transfer": {"out", "in", "transfer"},
    "refund": {"in", "refund"},
}
BAL_RE = re.compile(r"(?:账户余额|期末余额|当前余额|余额)\s*[:：]\s*[¥￥]?\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
MATCH_DAYS = 3  # 双花对消的日期窗口


# ---------- 基础解析 ----------

def _clean(s) -> str:
    if s is None:
        return ""
    return str(s).replace("\ufeff", "").replace("\u200b", "").replace("\u3000", " ").strip()


def _hdrkey(s) -> str:
    return _clean(s).replace("（", "(").replace("）", ")").replace(" ", "")


def decode_file(path: str) -> tuple[str, str]:
    with open(path, "rb") as f:
        raw = f.read()
    tried = []
    for enc in ("utf-8-sig", "gbk", "utf-16"):
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, UnicodeError):
            tried.append(enc)
    raise ValueError(f"{path}: 无法识别编码（依次试过 {'、'.join(tried)}）；微信应为 UTF-8(BOM)、支付宝应为 GBK")


def match_header(cells: list[str]) -> dict | None:
    """表头特征嗅探：必须同时具备 时间/对方/商品/收支/金额 五类列，否则不算表头。"""
    keys = [_hdrkey(c) for c in cells]
    idx: dict[str, int] = {}
    for field, aliases in FIELD_ALIASES.items():
        for al in aliases:
            if al in keys:
                idx[field] = keys.index(al)
                break
    return idx if all(f in idx for f in REQUIRED_FIELDS) else None


def _is_meta(cells: list[str]) -> bool:
    joined = _clean("".join(cells))
    if not joined:
        return True
    return sum(1 for c in cells if _clean(c)) <= 1  # 说明行/分隔线/结束线都是单有效列


def parse_dt(s: str | None):
    if not s:
        return None
    s = _clean(s)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d",
                "%Y年%m月%d日 %H:%M:%S", "%Y年%m月%d日", "%Y%m%d%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_amount(s: str | None):
    if not s:
        return None
    t = (_clean(s).replace("¥", "").replace("￥", "")
         .replace(",", "").replace("，", "").replace(" ", "").rstrip("元"))
    try:
        v = float(t)
    except ValueError:
        return None
    if not v > 0:  # 账单金额恒为正，方向由 收/支 列表达
        return None
    return round(v, 2)


def norm_flow(s: str | None) -> str:
    s = (s or "").strip()
    if s.startswith("支"):
        return "out"
    if s.startswith("收"):
        return "in"
    return "transfer"  # "/"、"不计收支"、"中性交易"、空 → 中性（转账类）


def make_row(platform: str, cells: list[str], idx: dict) -> tuple[dict | None, str | None]:
    """归一化一行 → {日期, 对方, 商品, 流向, 种类, 金额, 支付方式, 备注, 去重键}；失败返回原因。"""
    def cell(field: str) -> str | None:
        i = idx.get(field)
        if i is None or i >= len(cells):
            return None
        v = _clean(cells[i])
        return v if v not in ("", "/", "-") else None

    dt = parse_dt(cell("date"))
    if dt is None:
        return None, "日期无法解析"
    amt = parse_amount(cell("amount"))
    if amt is None:
        return None, "金额无法解析或非正数"

    counter = cell("counterparty") or ""
    product = cell("product") or ""
    flow = norm_flow(cell("flow"))
    typ = cell("type") or ""
    status = cell("status") or ""
    blob = typ + status
    refunded = False
    if "退款" in blob and flow == "out":
        refunded, kind = True, "consume"   # 原支出行被退款：照记支出，标记已退款
    elif "退款" in blob:
        kind, flow = "refund", "in"        # 独立退款行：钱回来，冲减支出科目
    elif any(k in blob for k in TRANSFER_KW):
        kind = "transfer"                  # 转账/提现/充值/还款/代付：单独标记
    else:
        kind = "consume"

    ts = dt.strftime("%Y-%m-%d %H:%M:%S")
    key_src = "|".join([platform, ts, flow, kind, f"{amt:.2f}", counter, product])
    dedup_key = f"{platform}:{hashlib.sha256(key_src.encode('utf-8')).hexdigest()[:20]}"
    note = "｜".join(x for x in (counter, product) if x)
    if refunded:
        note += "｜已退款"
    if kind == "refund":
        note = "退款：" + note
    return {"platform": platform, "date": dt.date().isoformat(), "date_obj": dt.date(),
            "ts": ts, "flow": flow, "kind": kind, "amount": amt,
            "counterparty": counter, "product": product,
            "pay": cell("pay") or "", "status": status, "order": cell("order") or "",
            "refunded": refunded, "note": note,
            "raw": _clean(",".join(cells))[:200], "dedup_key": dedup_key}, None


def _parse_period(meta_lines: list[str]) -> list[str | None]:
    start = end = None

    def dates_in(ln: str) -> list[str]:
        out = []
        for d in re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{8}", ln):
            d = d.replace("/", "-")
            if len(d) == 8:
                d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            out.append(d)
        return out

    for ln in meta_lines:
        if not any(k in ln for k in ("起始", "起止", "开始", "终止", "截止")):
            continue
        ds = dates_in(ln)
        if not ds:
            continue
        has_start = any(k in ln for k in ("起始", "起止", "开始"))
        has_end = any(k in ln for k in ("终止", "截止"))
        if has_start and has_end:
            start, end = min(ds), max(ds)
        elif has_end:
            end = max(ds) if end is None else max(end, max(ds))
        elif has_start:
            start = min(ds) if start is None else min(start, min(ds))
    return [start, end]


def parse_file(path: str, forced_platform: str) -> dict:
    """整文件解析：编码试解 → 逐行找表头 → 数据行归一化 → 坏行/说明行分类计数。"""
    text, enc = decode_file(path)
    bal_line = None
    for n, ln in enumerate(text.splitlines(), 1):
        m = BAL_RE.search(ln)
        if m:
            bal_line = {"file": path, "line": n, "value": round(float(m.group(1).replace(",", "")), 2)}
            break

    reader = csv.reader(io.StringIO(text))
    idx = None
    platform = None
    header_line = None
    meta_lines: list[str] = []
    rows: list[dict] = []
    skipped: list[dict] = []
    for cells in reader:
        ln = reader.line_num
        if header_line is None:
            hit = match_header(cells)
            if hit:
                idx, header_line = hit, ln
                keys = [_hdrkey(c) for c in cells]
                if forced_platform and forced_platform != "auto":
                    platform = forced_platform
                elif any(h in keys for h in WECHAT_HINTS):
                    platform = "wechat"
                else:
                    platform = "alipay"
            else:
                joined = _clean(",".join(cells))
                if joined:
                    meta_lines.append(joined)
            continue
        if _is_meta(cells):
            continue  # 结束分隔线等，不算坏行
        row, err = make_row(platform, cells, idx)
        if err:
            skipped.append({"line": ln, "reason": err, "raw": _clean(",".join(cells))[:120]})
        else:
            row["file"] = path
            row["line"] = ln
            rows.append(row)
    if header_line is None:
        raise ValueError(f"{path}: 未找到表头行（需同时含 交易时间/交易对方/商品/收/支/金额 列）；"
                         f"格式不符或编码不对（已按 {enc} 解出）")
    start, end = _parse_period(meta_lines)
    return {"file": path, "platform": platform,
            "encoding": "utf-8(BOM)" if enc == "utf-8-sig" else enc,
            "header_line": header_line, "meta_line_count": len(meta_lines),
            "parsed": len(rows), "skipped": len(skipped),
            "skipped_samples": skipped[:5],
            "period": [start, end] if (start or end) else None,
            "balance_line": bal_line, "rows": rows}


# ---------- 复用 ledger 的内部调用（重定向 stdout 取回 JSON，不重复记账逻辑） ----------

def call_json(fn, ns) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(ns)
    txt = buf.getvalue()
    try:
        return json.loads(txt)
    except json.JSONDecodeError as e:
        raise ValueError(f"内部调用 {getattr(fn, '__name__', fn)} 输出无法解析（rc={rc}）：{txt[:200]!r}") from e


def suggest_for(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {"ok": False, "reason_code": "no_history"}
    try:
        return call_json(L.cmd_suggest, argparse.Namespace(text=text))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason_code": "suggest_error", "error": str(e)}


def ledger_flow(t: dict, tmap: dict) -> str:
    """账本流水 → 对消池流向（唯一消费点：process() 的对消池构建，不影响记账与报表）。

    转账判型口径（D2）：资金账户 ↔ 资金/负债账户（资产↔资产/负债/权益）= "transfer"；
    支出科目 → 资金账户 = "refund"（手记退款，章程 §3"退款=方向倒过来再记一笔"）。
    原实现把手记退款落入 "transfer"，而 FLOW_COMPAT["refund"] 不含 "transfer"，
    导致手记退款+账单同笔退款落两遍（预算 spent 变负）——对消结构性失明。
    """
    if tmap.get(t.get("to_account")) == "expense":
        return "out"
    if tmap.get(t.get("from_account")) == "expense":
        return "refund"
    if tmap.get(t.get("from_account")) == "income":
        return "in"
    return "transfer"


# ---------- 建议解析与落库链 ----------

def resolve_chain(row: dict, ras: dict, paymap: dict, args, names: set) -> tuple[str | None, str | None, str | None]:
    """返回 (from_acct, to_acct, None) 或 (None, None, 原因码)。

    链条：支付方式映射 → --account 兜底 → ledger.suggest 历史记忆 → 缺省科目。
    suggest 的 record_as 只有方向与账单行一致时才采信（防止历史收入记忆套到支出行上）。
    """
    if row["kind"] == "transfer":
        return None, None, "transfer_manual"
    pay_acct = paymap.get(_clean(row["pay"])) or (args.account or None)
    if row["kind"] == "refund":
        src = ((ras.get("to_account") if ras.get("direction") == "out" else None)
               or args.refund_category or args.expense_category)
        dst = pay_acct or ((ras.get("from_account") if ras.get("direction") == "out" else None))
    elif row["flow"] == "out":
        src = pay_acct or ((ras.get("from_account") if ras.get("direction") == "out" else None))
        dst = ((ras.get("to_account") if ras.get("direction") == "out" else None)
               or args.expense_category)
    else:
        dst = pay_acct or ((ras.get("to_account") if ras.get("direction") == "in" else None))
        src = ((ras.get("from_account") if ras.get("direction") == "in" else None)
               or args.income_category)
    if not src or not dst:
        return None, None, "unresolved"
    missing = sorted({x for x in (src, dst) if x not in names})
    if missing:
        return None, None, "unknown_account:" + "、".join(missing)
    return src, dst, None


def find_cancel(pool: list[dict], row: dict) -> dict | None:
    """±3 天 + 同金额 + 同向守卫；账本流水优先于先前账单的待确认行，再按日期最近。"""
    ckey = "refund" if row["kind"] == "refund" else row["flow"]
    cands = []
    for e in pool:
        if e["used"] or e["flow"] not in FLOW_COMPAT[ckey]:
            continue
        if abs(row["amount"] - e["amount"]) > L.EPS:
            continue
        if abs((row["date_obj"] - e["date"]).days) > MATCH_DAYS:
            continue
        cands.append(e)
    if not cands:
        return None
    cands.sort(key=lambda e: (0 if e["src"] == "ledger" else 1,
                              abs((row["date_obj"] - e["date"]).days)))
    return cands[0]


def view(row: dict) -> dict:
    return {"file": row["file"], "line": row["line"], "date": row["date"],
            "flow": row["flow"], "kind": row["kind"], "amount": row["amount"],
            "counterparty": row["counterparty"], "product": row["product"],
            "pay": row["pay"], "note": row["note"], "raw": row["raw"],
            "dedup_key": row["dedup_key"]}


def _auto_refund_entry(row: dict, orig_id: str | None) -> dict:
    """「原支出行状态含已全额退款」形态的自动冲减分录（D3）：资金账户→支出科目 的反向，
    即 支出科目 → 资金账户、同额，note 标注自动冲减并关联原行（dry-run 阶段原行未落库，
    关联处标注"落库时回填"）。"""
    ref = f"（关联原行 {orig_id}）" if orig_id else "（关联原行：落库时回填）"
    return {"from_account": row["to"], "to_account": row["from"],
            "date": row["date"], "amount": row["amount"],
            "note": f"自动冲减：{row['note']}{ref}", "source": "import",
            "dedup_key": f"{row['dedup_key']}:refund"}


# ---------- 断言 ----------

def run_assertion(args, file_metas: list[dict], max_row_date: str | None) -> dict:
    acct = args.assert_account
    accounts = L.load_accounts()
    names = {x.get("name") for x in accounts}
    if not acct:
        return {"ok": False, "reason_code": "no_assert_account",
                "error": "--assert-balance 需要配合 --assert-account 指定断言账户"}
    if acct not in names:
        return {"ok": False, "reason_code": "unknown_account",
                "error": f"断言账户「{acct}」不存在",
                "known_accounts": sorted(x for x in names if x)}
    as_of = None
    if args.assert_date:
        as_of = D.norm_date(args.assert_date)  # 与 ledger assert 同口径：归一非补零，拒收垃圾（D1②）
        if not as_of:
            raise ValueError(f"--assert-date 应为 YYYY-MM-DD，现在是 {args.assert_date!r}")
    if not as_of:
        ends = [m["period"][1] for m in file_metas if m.get("period") and m["period"][1]]
        as_of = max(ends) if ends else (max_row_date or D.now_iso()[:10])

    reported = None
    if args.assert_balance and args.assert_balance != "FROM_BILL":
        try:
            reported = round(float(str(args.assert_balance).replace(",", "").replace("¥", "")
                                   .replace("￥", "").strip()), 2)
        except ValueError:
            raise ValueError(f"--assert-balance 数值无法解析：{args.assert_balance!r}")
        source = "cli"
    else:
        hits = [m["balance_line"] for m in file_metas if m.get("balance_line")]
        if not hits:
            return {"ok": False, "reason_code": "no_balance_line",
                    "error": "账单里没找到余额行（形如「账户余额：¥xx」/「期末余额：xx」），也未给 --assert-balance 数值",
                    "hint": "微信/支付宝个人导出常常不含余额行；请主人报一个数改用 --assert-balance 数值"}
        reported = hits[0]["value"]
        source = f"{os.path.basename(hits[0]['file'])}:{hits[0]['line']}"
    derived = round(L.derive(accounts, L.load_txns(), as_of=as_of)[0].get(acct, 0.0), 2)
    diff = round(reported - derived, 2)
    out = {"account": acct, "as_of": as_of, "reported": reported, "derived": derived,
           "difference": diff, "matched": abs(diff) <= L.EPS, "balance_source": source}
    if out["matched"]:
        out["user_view"] = f"余额断言对上：截至 {as_of}，{acct} 账本推导 {derived} 元 = 账单 {reported} 元。"
    else:
        out["reason_code"] = "assertion_failed"
        out["hint"] = "差额常来自：转账/还款类待手工、期初未记、或账单外还有收支；先补齐再断言"
        out["user_view"] = (f"余额断言不平：截至 {as_of}，{acct} 账本推导 {derived} 元，"
                            f"账单 {reported} 元，差 {diff} 元。")
    return out


# ---------- 主流程（dry-run 与 apply 共用，selfcheck 也走这里） ----------

def process(files: list[str], args) -> dict:
    apply_mode = bool(args.apply) and not args.dry_run
    paymap: dict[str, str] = {}
    for kv in (args.account_map or []):
        k, _, v = kv.partition("=")
        if k.strip() and v.strip():
            paymap[k.strip()] = v.strip()

    accounts = L.load_accounts()
    names = {x.get("name") for x in accounts}
    tmap = {x.get("name"): x.get("type") for x in accounts}
    txns = L.load_txns()
    existing_keys = {t.get("dedup_key") for t in txns if t.get("dedup_key")}
    # 对消痕迹（manifest.cancel_log）：被双花对消的账单行不落 txns，凭这张表留痕——
    # 之后无论带不带 --match-existing 重导（换旗），同一经济事件都不再记第二遍。
    cancelled_keys = {r.get("dedup_key") for r in D.read_rows(D.table_path("cancel_log"))
                      if r.get("dedup_key")}

    # 对消池：账本流水全量 + 各文件的待确认行（只对后续文件生效）
    pool: list[dict] = []
    if args.match_existing:
        for t in txns:
            amt = t.get("amount")
            if not isinstance(amt, (int, float)) or isinstance(amt, bool):
                continue
            try:
                d = _date.fromisoformat(str(t.get("date", ""))[:10])
            except ValueError:
                continue
            pool.append({"src": "ledger", "flow": ledger_flow(t, tmap), "date": d,
                         "amount": round(float(amt), 2), "_id": t.get("_id"),
                         "note": t.get("note"), "used": False})

    file_metas, duplicates, cancellations, pending = [], [], [], []
    seen_in_run: set = set()
    max_row_date = None
    for path in files:
        meta = parse_file(path, getattr(args, "platform", "auto"))
        file_metas.append({k: meta[k] for k in ("file", "platform", "encoding", "header_line",
                                                "meta_line_count", "parsed", "skipped",
                                                "skipped_samples", "period", "balance_line")})
        file_pending = []
        for row in meta["rows"]:
            if max_row_date is None or row["date"] > max_row_date:
                max_row_date = row["date"]
            if row["dedup_key"] in existing_keys:
                duplicates.append({**view(row), "reason": "dedup_key 已在 txns（此前导入过）"})
                continue
            if row["dedup_key"] in cancelled_keys:
                duplicates.append({**view(row),
                                   "reason": "dedup_key 已在对消痕迹（此前已双花对消，不再入账）"})
                continue
            if row["dedup_key"] in seen_in_run:
                duplicates.append({**view(row), "reason": "本批文件内重复"})
                continue
            seen_in_run.add(row["dedup_key"])
            if args.match_existing:
                hit = find_cancel(pool, row)
                if hit:
                    hit["used"] = True
                    matched = ({"source": "ledger", "_id": hit["_id"],
                                "date": hit["date"].isoformat(), "note": hit.get("note")}
                               if hit["src"] == "ledger" else
                               {"source": "pending", "file": os.path.basename(hit["file"]),
                                "line": hit["line"]})
                    cancellations.append({"file": os.path.basename(path), "line": row["line"],
                                          "date": row["date"], "amount": row["amount"],
                                          "counterparty": row["counterparty"],
                                          "dedup_key": row["dedup_key"], "matched": matched})
                    continue
            file_pending.append(row)
        pending.extend(file_pending)
        for row in file_pending:  # 本文件的行只对"之后处理的文件"可对消，同文件内不对消
            pool.append({"src": "pending", "flow": row["flow"], "date": row["date_obj"],
                         "amount": row["amount"], "file": path, "line": row["line"],
                         "used": False})

    # 建议与落库解析（两种模式都算：dry-run 要亮出 suggest 建议的 from/to）
    for row in pending:
        sugg = suggest_for(f'{row["counterparty"]} {row["product"]}')
        rec = (sugg.get("suggestion") or {}) if sugg.get("ok") else {}
        ras = rec.get("record_as") or {}
        row["suggest"] = {"ok": bool(sugg.get("ok")), "matched": rec.get("matched"),
                          "from": ras.get("from_account"), "to": ras.get("to_account")}
        src, dst, err = resolve_chain(row, ras, paymap, args, names)
        row["from"], row["to"], row["unresolved"] = src, dst, err

    applied: list[dict] = []
    failed: list[dict] = []
    manual: list[dict] = []
    if apply_mode:
        for row in pending:
            if row["unresolved"] == "transfer_manual":
                manual.append({**view(row),
                               "hint": "转账/还款行请用 ledger.py record 手工记账（from/to 需主人确认），"
                                       "或与另一张账单 --match-existing 对消"})
                continue
            if row["unresolved"]:
                failed.append({**view(row), "reason_code": row["unresolved"]})
                continue
            ns = argparse.Namespace(from_account=row["from"], to_account=row["to"],
                                    amount=row["amount"], date=row["date"], note=row["note"],
                                    raw_text=row["raw"], source="import",
                                    dedup_key=row["dedup_key"], json=None, dry_run=False)
            payload = call_json(L.cmd_record, ns)
            if payload.get("ok"):
                applied.append({**view(row), "from_account": row["from"],
                                "to_account": row["to"], "_id": payload.get("recorded"),
                                "duplicate": bool(payload.get("duplicate"))})
                # D3：原支出行状态含「已全额退款」→ 照记支出后自动生成冲减分录，
                # 退款的钱回账（否则退款只打标记、账面永不冲减）
                if row.get("refunded") and not payload.get("duplicate"):
                    rentry = _auto_refund_entry(row, payload.get("recorded"))
                    rpayload = call_json(L.cmd_record, argparse.Namespace(
                        **rentry, raw_text=row["raw"], json=None, dry_run=False))
                    if rpayload.get("ok"):
                        applied.append({**view(row), "from_account": rentry["from_account"],
                                        "to_account": rentry["to_account"],
                                        "_id": rpayload.get("recorded"),
                                        "auto_refund": True,
                                        "refund_of": payload.get("recorded"),
                                        "duplicate": bool(rpayload.get("duplicate"))})
                    else:
                        failed.append({**view(row), "from_account": rentry["from_account"],
                                       "to_account": rentry["to_account"],
                                       "reason_code": rpayload.get("reason_code"),
                                       "error": rpayload.get("error"),
                                       "auto_refund_failed_for": payload.get("recorded")})
            else:
                failed.append({**view(row), "from_account": row["from"],
                               "to_account": row["to"],
                               "reason_code": payload.get("reason_code"),
                               "error": payload.get("error")})
        # 对消留痕（append-only，只在本批真正 apply 时写）：被对消的账单行记入
        # manifest.cancel_log，此后带不带 --match-existing 重导都凭 dedup_key 跳过。
        for c in cancellations:
            m = c.get("matched") or {}
            ref = None
            if m.get("source") == "ledger":
                ref = m.get("_id")
            elif m.get("source") == "pending":
                ref = f'{m.get("file")}:{m.get("line")}'
            D.append_row(D.table_path("cancel_log"), {
                "dedup_key": c["dedup_key"], "source_file": c["file"],
                "bill_date": c["date"], "amount": c["amount"],
                "matched_type": m.get("source"), "matched_ref": ref,
                "counterparty": c.get("counterparty"),
                "_id": D.row_id(), "created_at": D.now_iso()})

    pending_view = []
    for row in pending:
        e = {**view(row), "suggested_from": row["from"], "suggested_to": row["to"]}
        if row["suggest"].get("ok"):
            e["suggest"] = row["suggest"]
        if row["unresolved"]:
            e["unresolved"] = row["unresolved"]
        elif not apply_mode:
            e["would_record"] = {"from_account": row["from"], "to_account": row["to"],
                                 "date": row["date"], "amount": row["amount"],
                                 "note": row["note"], "source": "import",
                                 "dedup_key": row["dedup_key"]}
            if row.get("refunded"):  # D3：已全额退款形态 apply 时会自动生成冲减分录，dry-run 先亮出来
                e["would_record_refund"] = _auto_refund_entry(row, None)
        pending_view.append(e)

    totals = {"parsed": sum(m["parsed"] for m in file_metas),
              "skipped": sum(m["skipped"] for m in file_metas),
              "duplicates": len(duplicates), "cancelled": len(cancellations),
              "pending": len(pending),
              "manual": sum(1 for r in pending if r["unresolved"] == "transfer_manual"),
              "applied": len(applied), "failed": len(failed)}
    report = {"mode": "apply" if apply_mode else "dry_run",
              "files": file_metas, "totals": totals,
              "duplicates": duplicates, "cancellations": cancellations,
              "pending": pending_view, "manual": manual, "failed": failed, "applied": applied}

    if args.assert_account or args.assert_balance:
        assertion = run_assertion(args, file_metas, max_row_date)
        report["assertion"] = assertion

    t = totals
    s = (f"解析 {t['parsed']} 笔"
         + (f"、坏行 {t['skipped']}" if t["skipped"] else "")
         + (f"、已存在 {t['duplicates']} 笔跳过" if t["duplicates"] else "")
         + (f"、双花对消 {t['cancelled']} 笔" if t["cancelled"] else ""))
    if apply_mode:
        s += (f"；落库 {t['applied']} 笔"
              + (f"（转账类 {t['manual']} 笔待手工）" if t["manual"] else "")
              + (f"、失败 {t['failed']} 笔" if t["failed"] else "") + "。")
    else:
        s += (f"；待确认 {t['pending']} 笔（转账类 {t['manual']} 笔届时需手工），"
              "尚未落库，确认后加 --apply。")
    if report.get("assertion"):
        s += report["assertion"].get("user_view", "")
    report["user_view"] = s

    ok, reason = True, None
    assertion = report.get("assertion")
    if assertion:
        if assertion.get("reason_code") in ("no_balance_line", "unknown_account", "no_assert_account"):
            ok, reason = False, assertion["reason_code"]
        elif not assertion.get("matched", False):
            ok, reason = False, "assertion_failed"
    report["ok"] = ok
    if reason:
        report["reason_code"] = reason
    return report


def cmd_import(a) -> int:
    if not a.files:
        return D.jfail("缺少账单文件（或用 --selfcheck）")
    for p in a.files:
        if not os.path.isfile(p):
            return D.jfail(f"文件不存在：{p}")
    try:
        report = process(a.files, a)
    except (IOError, OSError, ValueError) as e:
        return D.jfail(str(e))
    return D.jout(report, code=0)  # 业务性失败（断言不平/无余额行）也是正常对话流，退出码 0


# ---------- selfcheck（临时目录造假 CSV 全流程，零残留） ----------

def cmd_selfcheck(_a) -> int:
    import tempfile
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="import_bills_selfcheck_") as tmp:
        os.environ["ASSISTANT_DATA_DIR"] = tmp  # 整个 data/ledger 栈指到沙箱，真实账本零接触
        try:
            problems = _selfcheck_body(tmp)
        finally:
            os.environ.pop("ASSISTANT_DATA_DIR", None)
    if problems:
        return D.jout({"ok": False, "error": "；".join(problems)}, code=1)
    return D.jout({"ok": True, "tool": "import_bills",
                   "checks": ["编码嗅探(utf-8 BOM/GBK)", "表头特征嗅探(非首行)",
                              "归一化+转账/退款标记", "坏行跳过计数", "suggest 建议",
                              "dry-run 待确认清单+零落库", "--apply 落库", "同文件重导零副作用(幂等)",
                              "跨文件+对账本双花对消", "对消留痕 cancel_log+换旗重导幂等",
                              "已全额退款形态自动冲减", "期末余额断言"],
                   "sandbox": "临时目录已自动清除",
                   "user_view": "自检通过：假账单全流程（解析→去重→对消→落库→断言）在临时目录走通，零残留。"})


def _selfcheck_body(tmp: str) -> list[str]:
    problems: list[str] = []

    def chk(cond, msg):
        if not cond:
            problems.append(msg)

    wechat_csv = (
        "微信支付账单明细,,,,,,,,,\n"
        "微信昵称：[自检用户],,,,,,,,,\n"
        "起始时间：[2026-09-01 00:00:00] 终止时间：[2026-09-30 23:59:59],,,,,,,,,\n"
        "导出时间:[2026-10-01 08:00:00],,,,,,,,,\n"
        "账户余额：¥158.88,,,,,,,,,\n"
        "备注：以下为微信支付账单明细，为保护隐私部分字段已被隐藏。,,,,,,,,,\n"
        "----------------------微信支付账单明细列表--------------------,,,,,,,,,\n"
        "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注\n"
        "2026-09-03 12:00:00,商户消费,瑞幸咖啡,瑞幸咖啡-生椰拿铁,支出,¥12.00,零钱,支付成功,WX001,M001,/\n"
        "2026-09-05 09:00:00,转账,张三,转账给张三,支出,¥200.00,零钱,对方已收钱,WX002,M002,/\n"
        "2026-09-08 10:00:00,退款,瑞幸咖啡,瑞幸咖啡-生椰拿铁退款,/,¥12.00,零钱,已全额退款,WX003,M003,/\n"
        "2026-09-10 18:30:00,微信红包,李四,微信红包,收入,¥8.88,零钱,已存入零钱,WX004,M004,/\n"
        "2026-09-15 10:00:00,信用卡还款,招行信用卡,信用卡还款,支出,¥1000.00,零钱,还款成功,WX005,M005,/\n"
        "2026-09-16 09:00:00,商户消费,测试商户,测试商品,支出,abc,零钱,交易异常,WX006,M006,/\n"
    )
    ali_csv = (
        "支付宝（中国）网络技术有限公司  电商平台明细查询\n"
        "账号:checker@example.com\n"
        "起始日期:20260901000000  截止日期:20260930235959\n"
        "---------------------------------[交易记录明细]-------------------------------------\n"
        "交易时间,业务类型,交易对方,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注\n"
        "2026-09-06 20:11:00,淘系购物,淘宝网,秋季外套,支出,89.00,余额,交易成功,AL001,MB001,/\n"
        "2026-09-15 11:00:00,信用卡还款,招行信用卡,信用卡还款,不计收支,1000.00,余额,还款成功,AL003,MB003,/\n"
        "2026-09-20 12:00:00,转账,王五,转账收款,不计收支,500.00,余额宝,交易成功,AL002,MB002,/\n"
        "---------------------------------[交易记录结束]-------------------------------------\n"
    )
    p_wx = os.path.join(tmp, "wechat.csv")
    p_ali = os.path.join(tmp, "alipay.csv")
    with open(p_wx, "wb") as f:
        f.write(wechat_csv.encode("utf-8-sig"))  # 带 BOM，模拟真实微信导出
    with open(p_ali, "wb") as f:
        f.write(ali_csv.encode("gbk"))

    # 种子沙箱账本：账户 + 期初 + 一笔已在账的 500 转账（供对消命中）
    for acc in ({"name": "期初权益", "type": "equity", "in_net_worth": False},
                {"name": "微信零钱", "type": "asset", "in_net_worth": True},
                {"name": "支付宝余额", "type": "asset", "in_net_worth": True},
                {"name": "信用卡", "type": "liability", "in_net_worth": True},
                {"name": "支出·餐饮", "type": "expense", "in_net_worth": False},
                {"name": "收入·其他", "type": "income", "in_net_worth": False}):
        D.append_row(D.table_path("accounts"), {**acc, "_id": D.row_id(), "created_at": D.now_iso()})

    def seed_txn(d, f, t, amt):
        D.append_row(D.table_path("txns"),
                     {"date": d, "from_account": f, "to_account": t, "amount": amt,
                      "note": "selfcheck种子", "source": "record",
                      "_id": D.row_id(), "created_at": D.now_iso()})

    # 期初 650：落库后微信零钱 = 650 − 500(seed还款) − 12 + 12(退款) + 8.88 = 158.88，与账单余额行持平
    seed_txn("2026-09-01", "期初权益", "微信零钱", 650.0)
    seed_txn("2026-09-01", "期初权益", "支付宝余额", 200.0)
    seed_txn("2026-09-19", "微信零钱", "信用卡", 500.0)

    def mk_args(files, apply=False, match_existing=True, assert_account=None,
                assert_balance=None):
        return argparse.Namespace(files=files, platform="auto", dry_run=not apply,
                                  apply=apply, match_existing=match_existing,
                                  account_map=["零钱=微信零钱", "余额=支付宝余额",
                                               "余额宝=支付宝余额"],
                                  account="微信零钱", expense_category="支出·餐饮",
                                  income_category="收入·其他", refund_category=None,
                                  assert_account=assert_account,
                                  assert_balance=assert_balance, assert_date=None)

    # 1) 解析层：平台/编码/计数/种类/键稳定
    m1, m2 = parse_file(p_wx, "auto"), parse_file(p_wx, "auto")
    chk(m1["platform"] == "wechat" and m1["encoding"] == "utf-8(BOM)", f"微信嗅探错：{m1['platform']}/{m1['encoding']}")
    chk(m1["parsed"] == 5 and m1["skipped"] == 1, f"微信计数错：parsed={m1['parsed']} skipped={m1['skipped']}")
    chk("金额" in m1["skipped_samples"][0]["reason"], "坏行原因未注明金额")
    chk(m1["period"] == ["2026-09-01", "2026-09-30"], f"微信期间解析错：{m1['period']}")
    chk(m1["balance_line"] and m1["balance_line"]["value"] == 158.88, "微信余额行未解析出 158.88")
    chk([r["dedup_key"] for r in m1["rows"]] == [r["dedup_key"] for r in m2["rows"]], "去重键不稳定")
    kinds = {(r["flow"], r["kind"]) for r in m1["rows"]}
    chk(("out", "consume") in kinds and ("in", "consume") in kinds
        and ("in", "refund") in kinds and ("out", "transfer") in kinds, f"归一化种类缺失：{kinds}")
    ma = parse_file(p_ali, "auto")
    chk(ma["platform"] == "alipay" and ma["encoding"] == "gbk", f"支付宝嗅探错：{ma['platform']}/{ma['encoding']}")
    chk(ma["parsed"] == 3 and ma["skipped"] == 0, f"支付宝计数错：{ma['parsed']}/{ma['skipped']}")

    # 2) dry-run 全流程（多文件 + 对消）：不落库
    rep = process([p_wx, p_ali], mk_args([p_wx, p_ali]))
    t = rep["totals"]
    chk((t["parsed"], t["skipped"], t["duplicates"], t["cancelled"], t["pending"], t["manual"])
        == (8, 1, 0, 2, 6, 2), f"dry-run 总数错：{t}")
    chk(rep["mode"] == "dry_run" and rep["ok"] is True, "dry-run 应 ok 且未落库")
    cancels = rep["cancellations"]
    chk(any(c["matched"]["source"] == "ledger" for c in cancels)
        and any(c["matched"]["source"] == "pending" for c in cancels),
        f"对消应各有一笔账本命中与跨文件命中：{cancels}")
    first = next(r for r in rep["pending"] if r["amount"] == 12.0 and r["kind"] == "consume")
    chk(first["suggested_from"] == "微信零钱" and first["suggested_to"] == "支出·餐饮",
        f"建议解析错：{first['suggested_from']}→{first['suggested_to']}")
    chk("would_record" in first, "dry-run 缺 would_record")
    chk(len(D.read_rows(D.table_path("cancel_log"))) == 0, "dry-run 不得写对消痕迹")

    # 3) apply + 余额断言（微信零钱应 = 150 − 12 + 12 + 8.88 = 158.88）
    rep1 = process([p_wx, p_ali], mk_args([p_wx, p_ali], apply=True,
                                          assert_account="微信零钱", assert_balance="FROM_BILL"))
    t1 = rep1["totals"]
    chk((t1["applied"], t1["failed"], t1["manual"], t1["cancelled"], t1["duplicates"])
        == (4, 0, 2, 2, 0), f"apply 总数错：{t1}")
    ass = rep1.get("assertion") or {}
    chk(ass.get("matched") is True and ass.get("derived") == 158.88,
        f"余额断言应平 158.88：{ass}")
    chk(rep1["ok"] is True, f"apply+断言应 ok：{rep1.get('reason_code')}")
    n_after_1 = len(L.load_txns())

    # 4) 幂等：同文件再导一遍 → 全部去重跳过，零副作用
    #    （对消过的 AL002/AL003 现在凭 cancel_log 痕迹跳过，不再重新对消）
    rep2 = process([p_wx, p_ali], mk_args([p_wx, p_ali], apply=True,
                                          assert_account="微信零钱", assert_balance="FROM_BILL"))
    t2 = rep2["totals"]
    chk(t2["applied"] == 0 and t2["duplicates"] == 6 and t2["cancelled"] == 0,
        f"重导应 0 落库 6 去重（4 落库行 + 2 对消痕迹）0 对消：{t2}")
    chk(len(L.load_txns()) == n_after_1, "重导改变了 txns 行数（违反幂等）")
    chk(len(D.read_rows(D.table_path("cancel_log"))) == 2, "重导不得追加对消痕迹行")
    chk(rep2["assertion"]["matched"] is True, "重导后断言应仍平")

    # 5) 换旗幂等（双花对消留痕回归）：带旗对消过的账单行，换成不带 --match-existing
    #    重导也不得再入账——对消不落 txns，但必须落 cancel_log
    seed_txn("2026-09-10", "微信零钱", "支出·餐饮", 45.0)
    n_ledger = len(L.load_txns())
    p_flip = os.path.join(tmp, "wechat_flagflip.csv")
    with open(p_flip, "wb") as f:
        f.write((
            "微信支付账单明细,,,,,,,,,\n"
            "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注\n"
            "2026-09-11 12:00:00,商户消费,某商户,某商品,支出,¥45.00,零钱,支付成功,WX007,M007,/\n"
        ).encode("utf-8"))
    repA = process([p_flip], mk_args([p_flip], apply=True, match_existing=True))
    tA = repA["totals"]
    chk(tA["cancelled"] == 1 and tA["applied"] == 0, f"带旗应与账本支出对消：{tA}")
    chk(len(L.load_txns()) == n_ledger, "对消不得改变 txns 行数")
    repB = process([p_flip], mk_args([p_flip], apply=True, match_existing=False))
    tB = repB["totals"]
    chk(tB["applied"] == 0 and tB["duplicates"] == 1,
        f"换旗（无旗）重导必须凭对消痕迹跳过，不得再入账：{tB}")
    chk(len(L.load_txns()) == n_ledger, "换旗重导把已对消的经济事件又记了一遍")

    # 6) 「原支出行状态含已全额退款」形态（D3）：dry-run 预览冲减分录；apply 落库原单+冲减两笔，
    #    冲减 note 关联原行；重导凭 dedup 跳过，不再生成第二笔冲减
    p_ref = os.path.join(tmp, "wechat_refunded.csv")
    with open(p_ref, "wb") as f:
        f.write((
            "微信支付账单明细,,,,,,,,,\n"
            "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注\n"
            "2026-09-22 09:00:00,商户消费,某店,某商品,支出,¥20.00,零钱,已全额退款,WX008,M008,/\n"
        ).encode("utf-8"))
    n_pre6 = len(L.load_txns())
    rep_d6 = process([p_ref], mk_args([p_ref]))
    pv6 = rep_d6["pending"][0]
    wr6 = pv6.get("would_record_refund") or {}
    chk(wr6.get("from_account") == "支出·餐饮" and wr6.get("to_account") == "微信零钱"
        and wr6.get("amount") == 20.0, f"dry-run 缺自动冲减预览：{pv6}")
    rep6 = process([p_ref], mk_args([p_ref], apply=True))
    t6 = rep6["totals"]
    chk(t6["applied"] == 2 and t6["failed"] == 0, f"已全额退款形态应落库原单+冲减共 2 笔：{t6}")
    tx6 = L.load_txns()
    chk(len(tx6) == n_pre6 + 2, f"冲减应净增 2 行流水：{len(tx6)} vs {n_pre6}")
    orig6 = next((t for t in tx6 if t.get("dedup_key") == pv6["dedup_key"]), None)
    rev6 = next((t for t in tx6 if t.get("dedup_key") == pv6["dedup_key"] + ":refund"), None)
    chk(orig6 is not None and rev6 is not None
        and rev6.get("from_account") == "支出·餐饮" and rev6.get("to_account") == "微信零钱"
        and rev6.get("amount") == 20.0 and str(orig6.get("_id")) in str(rev6.get("note")),
        f"冲减分录缺失或未关联原行：{rev6}")
    rep6b = process([p_ref], mk_args([p_ref], apply=True))
    chk(rep6b["totals"]["applied"] == 0 and rep6b["totals"]["duplicates"] == 1
        and len(L.load_txns()) == n_pre6 + 2, f"重导不得再生成冲减分录：{rep6b['totals']}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(
        description="微信/支付宝账单 CSV 导入（解析→去重→双花对消→确认落库→余额断言）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("用法：")[1].split("选项：")[0] if "用法：" in __doc__ else None)
    ap.add_argument("files", nargs="*", help="账单 CSV（可多个；跨账单对消建议两张一起给）")
    ap.add_argument("--selfcheck", action="store_true", help="自检（临时目录造假账单全流程，零残留）")
    ap.add_argument("--platform", default="auto", choices=["auto", "wechat", "alipay"],
                    help="缺省按表头特征自动嗅探")
    ap.add_argument("--match-existing", action="store_true", help="与已有流水双花对消（±3天+同金额+同向）")
    ap.add_argument("--map", dest="account_map", action="append", default=[], metavar="K=V",
                    help="支付方式→账本账户，如 零钱=微信零钱（可多次）")
    ap.add_argument("--account", default=None, help="未映射支付方式的兜底资金账户")
    ap.add_argument("--expense-category", default=None, help="支出缺省科目")
    ap.add_argument("--income-category", default=None, help="收入缺省科目")
    ap.add_argument("--refund-category", default=None, help="退款冲抵科目（缺省回落支出科目）")
    ap.add_argument("--apply", action="store_true", help="真正落库（缺省 dry-run）")
    ap.add_argument("--dry-run", action="store_true", help="强制 dry-run（与 --apply 同给时优先）")
    ap.add_argument("--assert-account", default=None, help="期末余额断言的账户")
    ap.add_argument("--assert-balance", nargs="?", const="FROM_BILL", default=None,
                    help="期末余额：不带值=从账单余额行解析；带值=直接用")
    ap.add_argument("--assert-date", default=None, help="断言基准日 YYYY-MM-DD（缺省账单截止日）")
    a = ap.parse_args()
    if a.selfcheck:
        return cmd_selfcheck(a)
    return cmd_import(a)


if __name__ == "__main__":
    sys.exit(main())
