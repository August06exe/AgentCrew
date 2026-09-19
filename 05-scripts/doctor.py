#!/usr/bin/env python3
"""doctor.py — AgentCrew 体检：仓库结构 + registry + 各助理完整性 + 工具自检。"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentcrew_lib as B  # noqa: E402

WARN = "WARN"
ERROR = "ERROR"
INFO = "INFO"


def main() -> int:
    ap = argparse.ArgumentParser(description="AgentCrew 体检")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--no-selfcheck", action="store_true", help="跳过工具自检（快）")
    args = ap.parse_args()
    B.utf8_console()

    findings: list[dict] = []

    def add(level: str, where: str, msg: str) -> None:
        findings.append({"level": level, "where": where, "msg": msg})

    root = B.find_repo_root()
    if not root:
        print(json.dumps({"ok": False, "error": "未找到AgentCrew 仓库根（AGENTS.md+01-master+PROTOCOL.md）"},
                         ensure_ascii=False))
        return 1

    # 1. 仓库结构
    for need in ("AGENTS.md", "00-docs/PROTOCOL.md", "00-docs/PARADIGM.md", "01-master",
                 "02-agents", "05-scripts", "03-template"):
        if not os.path.exists(B.p(root, *need.split("/"))):
            add(ERROR, "repo", f"缺少 {need}")
    if not os.path.isdir(B.p(root, "04-panel")):
        add(WARN, "repo", "04-panel 不存在（管家面板未安装）")
    profile = B.master_save_files(root)["profile"]
    if not os.path.exists(profile):
        add(INFO, "master", "主人档案不存在 → 初始化模式（总管只做引导）")

    # 2. registry
    reg = B.load_registry(root)
    registered: dict[str, dict] = {}
    for e in reg.get("adopted", []):
        aid = e.get("id", "?")
        registered[aid] = e
        adir = B.p(root, e.get("dir", f"02-agents/{aid}"))
        if not os.path.isdir(adir):
            add(ERROR, aid, f"registry 指向的目录不存在：{e.get('dir')}")
            continue
        if e.get("status") not in ("active", "paused"):
            add(WARN, aid, f"status 异常：{e.get('status')}")

    # 3. 收编区扫描
    agdir = B.p(root, "02-agents")
    on_disk = []
    if os.path.isdir(agdir):
        on_disk = [d for d in sorted(os.listdir(agdir))
                   if os.path.isdir(B.p(agdir, d)) and not d.startswith(("_", "."))]
    for d in on_disk:
        adir = B.p(agdir, d)
        try:
            m = B.load_manifest(adir)
        except Exception:  # noqa: BLE001
            add(WARN, d, "无 manifest.json（不是合格助理？）")
            continue
        aid = m.get("id", d)
        if aid not in registered:
            add(WARN, aid, "住在收编区但未登记 registry（跑 adopt.py 收编）")
        check_agent(adir, m, args, add)

    # ---- 存档架构审计 ----
    # 1) 程序区散落增量数据（确定性扫描，与 save.py watch 同源）
    stray = B.scan_stray_increment(root)
    for s in stray[:20]:
        add(ERROR, "save", f"增量数据散落程序区（必须迁入 _save）：{s}")
    if stray:
        add(WARN, "save", "跑 python 05-scripts/migrate_save.py 一键归档")
    # 2) 存档版本兼容
    mani = B.load_save_manifest(root)
    if mani is None:
        add(INFO, "save", "无存档（新游戏状态）")
    elif mani.get("_corrupt"):
        add(ERROR, "save", "save.json 损坏")
    else:
        v = mani.get("save_version")
        if v > B.SAVE_VERSION_CURRENT:
            add(ERROR, "save", f"存档版本 {v} 高于程序支持的 {B.SAVE_VERSION_CURRENT}（请升级程序）")
        elif v < B.SAVE_VERSION_CURRENT:
            add(WARN, "save", f"存档版本 {v} 旧于程序 {B.SAVE_VERSION_CURRENT}（跑 migrate_save.py 升档）")
        # 3) 孤儿档切片
        sr_agents = p(B.save_root(root), "agents") if False else B.p(B.save_root(root), "agents")
        if os.path.isdir(sr_agents):
            for aid in os.listdir(sr_agents):
                if os.path.isdir(B.p(sr_agents, aid)) and not os.path.isdir(B.p(root, "02-agents", aid)):
                    add(WARN, "save", f"孤儿档切片：{aid}（程序区已无此顾问）")

    # 防腐检查：模板为源的共享副本必须一致（data.py ×3 / butler.css ×4）
    import hashlib

    def _md5(path):
        try:
            return hashlib.md5(open(path, "rb").read()).hexdigest()
        except OSError:
            return None

    tpl_data = _md5(B.p(root, "03-template", "tools", "data.py"))
    tpl_css = _md5(B.p(root, "03-template", "dashboard", "butler.css"))
    agdir_all = B.p(root, "02-agents")
    if os.path.isdir(agdir_all):
        for d in sorted(os.listdir(agdir_all)):
            h_data = _md5(B.p(agdir_all, d, "tools", "data.py"))
            if h_data and tpl_data and h_data != tpl_data:
                add(WARN, "canary", f"02-agents/{d}/tools/data.py 与模板不一致（共享工具分叉）")
            h_css = _md5(B.p(agdir_all, d, "dashboard", "butler.css"))
            if h_css and tpl_css and h_css != tpl_css:
                add(WARN, "canary", f"02-agents/{d}/dashboard/butler.css 与模板统一皮肤不一致")
    h_skin = _md5(B.p(root, "04-panel", "skin", "butler.css"))
    if h_skin and tpl_css and h_skin != tpl_css:
        add(WARN, "canary", "04-panel/skin/butler.css 与模板统一皮肤不一致")

    problems = [f for f in findings if f["level"] in (ERROR, WARN)]
    if args.json:
        print(json.dumps({"ok": not any(f["level"] == ERROR for f in findings),
                          "root": root, "findings": findings}, ensure_ascii=False, indent=2))
    else:
        print(f"# AgentCrew 体检 · {root}")
        if not findings:
            print("一切正常，无发现。")
        for f in findings:
            mark = {"ERROR": "✗", "WARN": "⚠", "INFO": "ℹ"}[f["level"]]
            print(f" [{mark}] {f['level']:<5} {f['where']}: {f['msg']}")
        n_err = sum(1 for f in findings if f["level"] == ERROR)
        n_warn = sum(1 for f in findings if f["level"] == WARN)
        print(f"# 结果：{n_err} 错误 / {n_warn} 警告")
    return 1 if any(f["level"] == ERROR for f in findings) else 0


def check_agent(adir: str, m: dict, args, add, root: str = '.') -> None:
    aid = m.get("id", "?")
    errs, warns = B.validate_manifest(m, adir)
    for e in errs:
        add(ERROR, aid, f"manifest: {e}")
    for w in warns:
        add(WARN, aid, f"manifest: {w}")
    if not os.path.isfile(B.p(adir, "AGENTS.md")):
        add(ERROR, aid, "缺章程 AGENTS.md")
    else:
        text = open(B.p(adir, "AGENTS.md"), encoding="utf-8").read()
        if "向上" not in text and "PROTOCOL" not in text:
            add(WARN, aid, "章程疑似缺双模式自查条款")
    # 数据区在存档切片（增量信息不进程序区）
    sdir = B.agent_save_dir(B.find_repo_root() or root, aid)
    if not os.path.isdir(B.p(sdir, "data")):
        add(ERROR, aid, "存档切片缺 data/（跑 adopt.py 或 migrate_save.py）")
    pend = B.inbox_tasks(adir)
    if pend:
        add(INFO, aid, f"inbox 有 {len(pend)} 张待办交办单")
    receipts = B.outbox_receipts(adir)
    if receipts:
        add(INFO, aid, f"outbox 有 {len(receipts)} 封未收取回执")
    if m.get("dashboard", {}).get("up") and not os.path.isfile(B.p(adir, m["dashboard"]["main"])):
        add(ERROR, aid, "声明 dashboard.up 但看板文件缺失")
    if not args.no_selfcheck:
        for tool in m.get("tools") or ["tools/data.py"]:
            good, msg = B.selfcheck_tool(adir, tool)
            if not good:
                add(ERROR, aid, f"工具自检失败 {tool}: {msg}")


if __name__ == "__main__":
    sys.exit(main())
