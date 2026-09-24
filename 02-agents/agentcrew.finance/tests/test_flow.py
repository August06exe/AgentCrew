#!/usr/bin/env python3
"""理财助理全流程自测：开户 → 期初入账 → 收支/转账记账 → 余额与净资产 → 试算平衡 →
余额断言（正/反例，反例断言 reason_code）→ 冲正后余额复原 → suggest 命中历史类别 →
预算 set/check 超支 → 月度简报（含异常检测）→ 微信+支付宝账单 CSV 导入
（dry-run/apply/重复导入幂等/双花对消/换旗重导幂等（对消留痕）/账单余额断言）→ 知识库词条查询。
账目数字用固定历史月 2026-08（与"今天"解耦，任何 ≥2026-08-31 的日期重跑结果一致）。
测试数据全落 tests/_sandbox，跑完清场，绝不触碰真实存档。"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = os.path.join(ROOT, "tools")
SANDBOX = os.path.join(HERE, "_sandbox")
ENV = {**os.environ, "ASSISTANT_DATA_DIR": os.path.join(SANDBOX, "data"),
       "ASSISTANT_DASHBOARD_DIR": os.path.join(SANDBOX, "dashboard")}

MONTH = "2026-08"

# 微信账单（UTF-8 带 BOM，表头不在首行，金额带 ¥，余额行可选）——按 import_bills.py 格式假设造。
# 余额行 ¥1672.88 是刻意设计的：期初 3000 − 8月净流出 327.12（导入 12 支出 + 8.88 红包收入）
# 恰好等于账本推导值，apply --assert-balance（不带值=从账单解析）必须对平。
WECHAT_CSV = (
    "微信支付账单明细,,,,,,,,,\n"
    "微信昵称：[流程测试用户],,,,,,,,,\n"
    "起始时间：[2026-09-01 00:00:00] 终止时间：[2026-09-30 23:59:59],,,,,,,,,\n"
    "账户余额：¥1672.88,,,,,,,,,\n"
    "----------------------微信支付账单明细列表--------------------,,,,,,,,,\n"
    "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注\n"
    "2026-09-03 12:00:00,商户消费,瑞幸咖啡,生椰拿铁,支出,¥12.00,零钱,支付成功,WX9001,M9001,/\n"
    "2026-09-10 18:30:00,微信红包,李四,节日红包,收入,¥8.88,零钱,已存入零钱,WX9002,M9002,/\n"
    "2026-09-15 10:00:00,信用卡还款,招行信用卡,信用卡还款,支出,¥300.00,零钱,还款成功,WX9003,M9003,/\n"
    "2026-09-20 09:00:00,转账,老王,转账给老王,支出,¥150.00,零钱,对方已收钱,WX9004,M9004,/\n"
)
# 支付宝账单（GBK，说明行 + 分隔线 + 结束线，不计收支为中性）。
# 300 元信用卡还款与微信账单同日同额 → 双花对消命中；500 元转账类进手工清单。
ALIPAY_CSV = (
    "支付宝（中国）网络技术有限公司  电商平台明细查询\n"
    "账号:flowtest@example.com\n"
    "起始日期:20260901000000  截止日期:20260930235959\n"
    "---------------------------------[交易记录明细]-------------------------------------\n"
    "交易时间,业务类型,交易对方,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注\n"
    "2026-09-05 20:11:00,淘系购物,淘宝网,秋季外套,支出,89.00,余额,交易成功,AL9001,MB9001,/\n"
    "2026-09-15 11:00:00,信用卡还款,招行信用卡,信用卡还款,不计收支,300.00,余额,还款成功,AL9003,MB9003,/\n"
    "2026-09-22 12:00:00,转账,王五,转账收款,不计收支,500.00,余额宝,交易成功,AL9002,MB9002,/\n"
    "---------------------------------[交易记录结束]-------------------------------------\n"
)
# 换旗场景迷你账单（⑩c）：单笔 45 消费与账本 2026-09-10 的 45 支出（测试自己种的）
# 同额近邻 → 带旗对消；随后无旗重导必须凭 cancel_log 痕迹跳过，不得再入账。
FLIP_CSV = (
    "微信支付账单明细,,,,,,,,,\n"
    "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注\n"
    "2026-09-11 12:00:00,商户消费,某商户,某商品,支出,¥45.00,零钱,支付成功,WX9005,M9005,/\n"
)


def run(tool, *args, expect="ok"):
    r = subprocess.run([sys.executable, os.path.join(TOOLS, tool), *args],
                       capture_output=True, text=True, encoding="utf-8", cwd=ROOT, env=ENV)
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"FAIL(non-json): {tool} {args} rc={r.returncode} "
                         f"out={r.stdout[:300]} err={r.stderr[:300]}")
    if expect == "ok" and (r.returncode != 0 or not out.get("ok")):
        raise SystemExit(f"FAIL: {tool} {args} -> {json.dumps(out, ensure_ascii=False)[:300]}")
    if expect == "fail" and out.get("ok"):
        raise SystemExit(f"FAIL(应失败却成功): {tool} {args} -> {json.dumps(out, ensure_ascii=False)[:200]}")
    return out


def almost(x, y, eps=0.005):
    return abs(float(x) - float(y)) <= eps


def acc(name, typ):
    return run("ledger.py", "accounts", "add", "--name", name, "--type", typ)


def rec(d, f, t, amt, note=None):
    args = ["record", "--from", f, "--to", t, "--amount", str(amt), "--date", d,
            "--source", "record"]
    if note:
        args += ["--note", note]
    return run("ledger.py", *args)


def main() -> int:
    shutil.rmtree(SANDBOX, ignore_errors=True)  # 每轮全量清场，测试可重复
    run("data.py", "init")

    # ① 开户：先 dry-run 验证不落盘，再真开 10 户；重复开户须报 account_exists
    d = run("ledger.py", "accounts", "add", "--name", "微信零钱", "--type", "asset", "--dry-run")
    assert d.get("dry_run"), d
    n0 = run("data.py", "query", "accounts")["count"]
    for name, typ in [("期初权益", "equity"),
                      ("微信零钱", "asset"), ("储蓄卡", "asset"), ("支付宝余额", "asset"),
                      ("信用卡", "liability"), ("花呗", "liability"),
                      ("收入·工资", "income"), ("收入·其他", "income"),
                      ("支出·餐饮", "expense"), ("支出·购物", "expense"), ("支出·交通", "expense")]:
        acc(name, typ)
    n1 = run("data.py", "query", "accounts")["count"]
    assert n1 == n0 + 11, f"开户数不对: {n0} -> {n1}"
    dup = run("ledger.py", "accounts", "add", "--name", "微信零钱", "--type", "asset", expect="fail")
    assert dup["reason_code"] == "account_exists", dup

    # ② 期初入账（期初权益 → 各资产账户）
    rec("2026-06-01", "期初权益", "微信零钱", 3000, "期初")
    rec("2026-06-01", "期初权益", "储蓄卡", 20000, "期初")
    rec("2026-06-01", "期初权益", "支付宝余额", 500, "期初")
    b = run("ledger.py", "balance", "--account", "微信零钱")
    assert almost(b["account"]["balance"], 3000), b
    nw = run("ledger.py", "net-worth")
    assert almost(nw["totals"]["net_worth"], 23500), nw["totals"]

    # ③ 记多笔收支与转账（先探两个负例：未知账户=业务失败、负数金额=技术失败）
    bad1 = run("ledger.py", "record", "--from", "火星钱包", "--to", "支出·餐饮",
               "--amount", "5", "--date", "2026-08-04", expect="fail")
    assert bad1["reason_code"] == "unknown_account", bad1
    bad2 = run("ledger.py", "record", "--from", "微信零钱", "--to", "支出·餐饮",
               "--amount", "-5", "--date", "2026-08-04", expect="fail")
    assert not bad2.get("ok") and "error" in bad2, bad2
    for args in [
        ("2026-06-10", "微信零钱", "支出·购物", 25, "视频会员"),
        ("2026-07-01", "收入·工资", "储蓄卡", 15000, "7月工资"),
        ("2026-07-10", "微信零钱", "支出·购物", 25, "视频会员"),
        ("2026-07-20", "微信零钱", "支出·购物", 200, "七月大采购"),
        ("2026-08-01", "收入·工资", "储蓄卡", 15000, "8月工资"),
        ("2026-08-02", "微信零钱", "支出·餐饮", 32, "瑞幸"),
        ("2026-08-03", "微信零钱", "支出·交通", 8, "地铁"),
        ("2026-08-05", "信用卡", "支出·购物", 2400, "家电"),
        ("2026-08-06", "花呗", "支出·购物", 400, "球鞋"),
        ("2026-08-08", "微信零钱", "支出·餐饮", 45, "瑞幸"),
        ("2026-08-09", "微信零钱", "支出·餐饮", 32, "楼下便利店"),
        ("2026-08-09", "微信零钱", "支出·餐饮", 32, "楼下便利店"),  # 同日同额两笔 → 月报重复扣款
        ("2026-08-10", "微信零钱", "支出·购物", 25, "视频会员"),    # 6/7/8 月同额同对方 → 疑似订阅
        ("2026-08-15", "储蓄卡", "信用卡", 2000, "信用卡还款"),      # 转账（资产 → 负债）
        ("2026-08-20", "微信零钱", "支出·购物", 900, "八月大采购"),  # 购物环比 225→3725 → 飙升
    ]:
        rec(*args)

    # ④ 试算平衡 + 余额与净资产核对（复式守恒：全部账户代数和应为 0）
    tb = run("ledger.py", "trial-balance")
    assert tb["balanced"] is True and almost(tb["uniform_total"], 0), tb["error"] or tb
    bal = run("ledger.py", "balance")
    bmap = {i["name"]: i["balance"] for i in bal["balances"]}
    for name, want in [("微信零钱", 1676), ("储蓄卡", 48000), ("支付宝余额", 500),
                       ("信用卡", 400), ("花呗", 400)]:
        assert almost(bmap[name], want), f"{name} 应为 {want}，算得 {bmap[name]}"
    assert almost(bal["totals"]["asset"], 50176) and almost(bal["totals"]["liability"], 800), bal["totals"]
    nw = run("ledger.py", "net-worth")
    assert almost(nw["totals"]["assets"], 50176) and almost(nw["totals"]["liabilities"], 800), nw["totals"]
    assert almost(nw["totals"]["net_worth"], 49376), nw["totals"]

    # ⑤ 余额断言两例：正例对平；反例必须 ok:false + reason_code=assertion_failed + 差额
    ok_a = run("ledger.py", "assert", "--account", "微信零钱", "--balance", "1676",
               "--date", "2026-08-31")
    assert ok_a["matched"] is True and almost(ok_a["difference"], 0), ok_a
    bad_a = run("ledger.py", "assert", "--account", "储蓄卡", "--balance", "99999",
                "--date", "2026-08-31", expect="fail")
    assert bad_a["reason_code"] == "assertion_failed", bad_a
    assert almost(bad_a["difference"], 51999) and bad_a["suspects"], bad_a

    # ⑥ 冲正：记错一笔 → 余额变动 → 冲正复原 → 不可重复冲正
    rec("2026-08-21", "微信零钱", "支出·餐饮", 77, "记错了")
    b2 = run("ledger.py", "balance", "--account", "微信零钱")
    assert almost(b2["account"]["balance"], 1599), b2
    q = run("data.py", "query", "txns", "--where", "note=记错了")
    assert q["count"] == 1, q
    tid = q["rows"][0]["_id"]
    n_pre_reverse = run("data.py", "query", "txns")["count"]
    rd = run("ledger.py", "reverse", "--id", tid, "--dry-run")
    assert rd.get("dry_run"), rd
    run("ledger.py", "reverse", "--id", tid)
    assert run("data.py", "query", "txns")["count"] == n_pre_reverse + 1, "冲正应恰好新增一行"
    b3 = run("ledger.py", "balance", "--account", "微信零钱")
    assert almost(b3["account"]["balance"], 1676), f"冲正后余额应复原 1676: {b3}"
    tb2 = run("ledger.py", "trial-balance")
    assert tb2["balanced"] is True, tb2
    ok_a2 = run("ledger.py", "assert", "--account", "微信零钱", "--balance", "1676",
                "--date", "2026-08-31")
    assert ok_a2["matched"] is True, ok_a2
    again = run("ledger.py", "reverse", "--id", tid, expect="fail")
    assert again["reason_code"] == "already_reversed", again

    # ⑦ suggest 分类记忆建议：同备注历史 → 支出·餐饮/微信零钱；无历史 → no_history
    sg = run("ledger.py", "suggest", "--text", "瑞幸")
    s = sg["suggestion"]
    assert s["kind"] == "expense" and s["category"] == "支出·餐饮" \
        and s["account"] == "微信零钱" and s["count"] == 2, sg
    assert s["record_as"]["from_account"] == "微信零钱" \
        and s["record_as"]["to_account"] == "支出·餐饮", s
    sg2 = run("ledger.py", "suggest", "--text", "火星外卖", expect="fail")
    assert sg2["reason_code"] == "no_history", sg2

    # ⑧ 预算：set（含 upsert 更新路径）→ check 超支
    b50 = run("budget.py", "set", "--month", MONTH, "--category", "支出·餐饮", "--amount", "50")
    assert b50["action"] == "created", b50
    b100 = run("budget.py", "set", "--month", MONTH, "--category", "支出·餐饮", "--amount", "100")
    assert b100["action"] == "updated" and b100["replaced"] == 1, b100
    run("budget.py", "set", "--month", MONTH, "--category", "支出·交通", "--amount", "50")
    lb = run("budget.py", "list", "--month", MONTH)
    assert lb["count"] == 2, lb
    ck = run("budget.py", "check", "--month", MONTH)
    rows = {r["category"]: r for r in ck["rows"]}
    assert rows["支出·餐饮"]["overspent"] is True and almost(rows["支出·餐饮"]["used_pct"], 141.0), rows
    assert rows["支出·交通"]["overspent"] is False, rows["支出·交通"]
    assert ck["totals"]["overspent_count"] == 1, ck["totals"]
    assert almost(ck["totals"]["unbudgeted_spend"], 3725), ck["totals"]

    # ⑨ 月度简报：收支/净资产变动/Top 开销 + 异常检测三件套 + markdown 落盘
    rp = run("report.py", "monthly", "--month", MONTH, "--json")
    p = rp["report"]
    assert almost(p["income_total"], 15000) and almost(p["expense_total"], 3874) \
        and almost(p["net"], 11126), (p["income_total"], p["expense_total"], p["net"])
    nwp = p["net_worth"]
    assert almost(nwp["start"], 38250) and almost(nwp["end"], 49376) \
        and almost(nwp["change"], 11126), nwp
    an = p["anomalies"]
    dup = an["duplicate_charges"]["items"]
    assert len(dup) == 1 and dup[0]["date"] == "2026-08-09" \
        and almost(dup[0]["amount"], 32) and dup[0]["count"] == 2, dup
    sp = an["category_spikes"]["items"]
    assert len(sp) == 1 and sp[0]["category"] == "支出·购物" and almost(sp[0]["delta"], 3500), sp
    subs = an["suspected_subscriptions"]["items"]
    assert len(subs) == 1 and subs[0]["note"] == "视频会员" \
        and almost(subs[0]["total_in_window"], 75), subs
    assert p["budget_execution"]["totals"]["overspent_count"] == 1, p["budget_execution"]["totals"]
    assert p["top_expenses"] and almost(p["top_expenses"][0]["amount"], 2400), p["top_expenses"][:1]
    assert os.path.isfile(os.path.join(SANDBOX, "data", "reports", f"monthly-{MONTH}.md")), \
        "月报 markdown 应落盘"

    # ⑩ 账单导入：造微信(UTF-8 BOM)+支付宝(GBK) CSV → dry-run → apply → 幂等重导 → 双花对消
    bills = os.path.join(SANDBOX, "bills")
    os.makedirs(bills, exist_ok=True)
    p_wx = os.path.join(bills, "wechat.csv")
    p_ali = os.path.join(bills, "alipay.csv")
    with open(p_wx, "wb") as f:
        f.write(WECHAT_CSV.encode("utf-8-sig"))
    with open(p_ali, "wb") as f:
        f.write(ALIPAY_CSV.encode("gbk"))
    n_pre = run("data.py", "query", "txns")["count"]  # 期初3 + 收支转账15 + 记错1 + 冲正1 = 20

    def imp(apply=False):
        args = [p_wx, p_ali, "--match-existing",
                "--map", "零钱=微信零钱", "--map", "余额=支付宝余额", "--map", "余额宝=支付宝余额",
                "--account", "微信零钱",
                "--expense-category", "支出·餐饮", "--income-category", "收入·其他"]
        if apply:
            args += ["--apply", "--assert-account", "微信零钱", "--assert-balance"]
        return run("import_bills.py", *args)

    dry = imp()
    t = dry["totals"]
    assert dry["mode"] == "dry_run" and dry["ok"] is True, dry.get("reason_code")
    # 对消语义（同 selfcheck）：后处理一侧（支付宝 300）记 cancelled，先处理一侧
    # （微信 300）仍留 pending，因其为转账类进 manual 待手工，绝不会自动落库
    assert (t["parsed"], t["skipped"], t["duplicates"], t["cancelled"],
            t["pending"], t["manual"], t["applied"]) == (7, 0, 0, 1, 6, 3, 0), t
    assert run("data.py", "query", "txns")["count"] == n_pre, "dry-run 不应落库"
    fmeta = dry["files"]
    assert fmeta[0]["platform"] == "wechat" and fmeta[0]["encoding"] == "utf-8(BOM)", fmeta[0]
    assert fmeta[1]["platform"] == "alipay" and fmeta[1]["encoding"] == "gbk", fmeta[1]
    pend = {r["amount"]: r for r in dry["pending"]}
    assert pend[12.0]["suggested_to"] == "支出·餐饮" and pend[12.0]["suggested_from"] == "微信零钱", pend[12.0]
    assert pend[8.88]["suggested_from"] == "收入·其他" and pend[8.88]["suggested_to"] == "微信零钱", pend[8.88]
    assert t["manual"] == 3, t
    assert {r["amount"] for r in dry["pending"] if r.get("unresolved") == "transfer_manual"} \
        == {150.0, 300.0, 500.0}, dry["pending"]
    assert {c["amount"] for c in dry["cancellations"]} == {300.0}, dry["cancellations"]
    assert dry["cancellations"][0]["matched"]["source"] == "pending", dry["cancellations"]

    ap = imp(apply=True)
    t1 = ap["totals"]
    assert (t1["applied"], t1["failed"], t1["manual"], t1["cancelled"],
            t1["duplicates"]) == (3, 0, 3, 1, 0), t1
    ass = ap["assertion"]
    assert ass["matched"] is True and almost(ass["derived"], 1672.88) \
        and almost(ass["reported"], 1672.88), ass
    assert ap["ok"] is True, ap.get("reason_code")
    app = {r["amount"]: r for r in ap["applied"]}
    assert app[12.0]["from_account"] == "微信零钱" and app[12.0]["to_account"] == "支出·餐饮", app[12.0]
    assert app[8.88]["from_account"] == "收入·其他" and app[8.88]["to_account"] == "微信零钱", app[8.88]
    assert app[89.0]["from_account"] == "支付宝余额" and app[89.0]["to_account"] == "支出·餐饮", app[89.0]
    assert run("data.py", "query", "txns")["count"] == n_pre + 3
    bw = run("ledger.py", "balance", "--account", "微信零钱")
    assert almost(bw["account"]["balance"], 1672.88), bw
    bz = run("ledger.py", "balance", "--account", "支付宝余额")
    assert almost(bz["account"]["balance"], 411), bz

    # 幂等：同一批文件重导 → 3 笔落库行 + 1 笔已对消行（凭 cancel_log 痕迹）全部跳过
    ap2 = imp(apply=True)
    t2 = ap2["totals"]
    assert (t2["applied"], t2["duplicates"], t2["cancelled"], t2["manual"]) == (0, 4, 0, 3), t2
    assert run("data.py", "query", "txns")["count"] == n_pre + 3, "重导改变了流水数（违反幂等）"
    assert ap2["assertion"]["matched"] is True, ap2["assertion"]

    # ⑩c 换旗幂等（对消留痕回归）：带旗与账本支出对消过的账单行，换成不带
    # --match-existing 重导也不得再入账——对消不落 txns，但必须落 cancel_log
    rec("2026-09-10", "微信零钱", "支出·餐饮", 45, "换旗对消种子")
    n_flag = run("data.py", "query", "txns")["count"]
    p_flip = os.path.join(bills, "flagflip.csv")
    with open(p_flip, "wb") as f:
        f.write(FLIP_CSV.encode("utf-8"))
    flip_args = ["--map", "零钱=微信零钱", "--account", "微信零钱",
                 "--expense-category", "支出·餐饮"]
    repA = run("import_bills.py", p_flip, "--match-existing", "--apply", *flip_args)
    assert repA["totals"]["cancelled"] == 1 and repA["totals"]["applied"] == 0, repA["totals"]
    assert repA["cancellations"][0]["matched"]["source"] == "ledger", repA["cancellations"]
    assert run("data.py", "query", "txns")["count"] == n_flag, "对消不得改变流水数"
    repB = run("import_bills.py", p_flip, "--apply", *flip_args)
    assert repB["totals"]["applied"] == 0 and repB["totals"]["duplicates"] == 1, repB["totals"]
    assert repB["duplicates"][0]["reason"].startswith("dedup_key 已在对消痕迹"), repB["duplicates"]
    assert run("data.py", "query", "txns")["count"] == n_flag, "换旗重导把已对消的经济事件又记了一遍"

    # ⑪ 知识库：term 名称/别名/查无 + search
    kt = run("kb.py", "term", "--name", "复利")
    assert kt["definition"] and kt["aliases"] == ["利滚利"], kt
    ka = run("kb.py", "term", "--name", "利滚利")
    assert ka["term"] == "复利", ka
    ks = run("kb.py", "search", "--q", "回撤")
    assert ks["count"] >= 1, ks
    kf = run("kb.py", "term", "--name", "不存在的词条XYZ", expect="fail")
    assert kf["reason_code"] == "unknown_term", kf

    shutil.rmtree(SANDBOX, ignore_errors=True)  # 恢复现场
    print("FINANCE FLOW OK（开户/期初/复式记账/净资产/试算/断言正反例/冲正复原/记忆建议/"
          "预算超支/月报异常检测/双账单导入幂等与双花对消/知识库 全过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
