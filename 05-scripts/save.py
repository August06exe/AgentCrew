#!/usr/bin/env python3
"""save.py — 存档管理（游戏机制的工具化）。

  status   存档概况（版本/顾问切片/体积/最后活动）
  watch    确定性监控：扫描程序区违规增量数据（不靠 LLM 自觉）
           退出码 0=干净，1=有违规（可挂计划任务/CI/面板启动钩子）
  reset    新游戏：二次确认后删除整个存档，项目回到原点
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentcrew_lib as B  # noqa: E402


def human(n: int) -> str:
    return f"{n/1024:.1f}KB" if n < 1024 * 1024 else f"{n/1024/1024:.2f}MB"


def dir_size(d: str) -> int:
    total = 0
    for base, _, files in os.walk(d):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def cmd_status(_a) -> int:
    root = B.find_repo_root()
    if not root:
        return B.fail("未找到 AgentCrew 仓库根")
    sr = B.save_root(root)
    mani = B.load_save_manifest(root)
    if mani is None:
        return B.ok({"exists": False, "note": "无存档（新游戏状态）——首次使用时自动建档"})
    agents = {}
    sdir = B.p(sr, "agents")
    if os.path.isdir(sdir):
        for aid in sorted(os.listdir(sdir)):
            adir = B.p(sdir, aid)
            if os.path.isdir(adir):
                agents[aid] = human(dir_size(adir))
    prof = B.master_save_files(root)["profile"]
    owner = ""
    if os.path.isfile(prof):
        try:
            owner = (B.read_json(prof).get("owner") or {}).get("name", "")
        except Exception:  # noqa: BLE001
            pass
    return B.ok({
        "exists": True,
        "save_root": sr,
        "save_version": mani.get("save_version"),
        "program_supports": B.SAVE_VERSION_CURRENT,
        "owner": owner,
        "created_at": mani.get("created_at"),
        "master_size": human(dir_size(B.p(sr, "master"))),
        "agents": agents,
    })


def cmd_watch(_a) -> int:
    root = B.find_repo_root()
    if not root:
        return B.fail("未找到 AgentCrew 仓库根")
    stray = B.scan_stray_increment(root)
    if stray:
        return B.fail(f"程序区发现 {len(stray)} 处违规增量数据（增量信息必须进 _save）",
                      {"violations": stray[:50],
                       "fix": "python 05-scripts/migrate_save.py 一键归档"},
                      code=1)
    return B.ok({"clean": True, "note": "程序区无增量数据泄漏，存档架构完好"})


def cmd_reset(a) -> int:
    root = B.find_repo_root()
    if not root:
        return B.fail("未找到 AgentCrew 仓库根")
    sr = B.save_root(root)
    if not os.path.isdir(sr):
        return B.ok({"note": "本来就没有存档（已是原点）"})
    if not a.yes:
        # 危险操作：默认要求交互确认；脚本调用须显式 --yes
        try:
            ans = input(f"将删除整个存档 {sr}（主人档案/全部顾问数据，不可恢复）。\n输入 YES 确认：")
        except EOFError:
            ans = ""
        if ans.strip() != "YES":
            return B.fail("未确认，已取消")
    import shutil
    shutil.rmtree(sr, ignore_errors=True)
    B.ensure_save(root)
    return B.ok({"reset": True, "note": "存档已清除并重建空档——项目回到原点（新游戏）"})


def main() -> int:
    ap = argparse.ArgumentParser(description="AgentCrew 存档管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("watch")
    p_r = sub.add_parser("reset")
    p_r.add_argument("--yes", action="store_true", help="跳过交互确认（脚本用）")
    a = ap.parse_args()
    return {"status": cmd_status, "watch": cmd_watch, "reset": cmd_reset}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
