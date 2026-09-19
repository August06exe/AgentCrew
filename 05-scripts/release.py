#!/usr/bin/env python3
"""release.py — 放归：把子助理移出收编区（自动回 lite 模式），registry 注销。数据随助理走。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentcrew_lib as B  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="放归子助理（退出联邦，回 lite 模式）")
    ap.add_argument("agent_id", help="助理 id，如 demo.fitness")
    ap.add_argument("--dest", default=None, help="放归目的地（默认：仓库同级 _standalone\\ 目录）")
    ap.add_argument("--purge-data", action="store_true", help="危险：连同数据一起删除（默认保留）")
    ap.add_argument("--yes", action="store_true", help="非交互确认（--purge-data 时必须）")
    args = ap.parse_args()
    B.utf8_console()

    root = B.find_repo_root()
    if not root:
        return B.fail("未找到AgentCrew 仓库根")
    reg = B.load_registry(root)
    entry = B.registry_find(reg, args.agent_id)
    if not entry:
        return B.fail(f"registry 中没有 {args.agent_id}")

    src = B.p(root, entry.get("dir", f"02-agents/{args.agent_id}"))
    if not os.path.isdir(src):
        return B.fail(f"助理目录不存在：{src}")

    # 放归目的地默认为仓库内 _standalone\（判定豁免区：detect_mode 对它视为 lite）
    standalone_root = os.path.abspath(args.dest or B.p(root, "_standalone"))
    dest = B.p(standalone_root, args.agent_id)
    if os.path.exists(dest):
        return B.fail(f"放归目标已存在：{dest}（同 id 两份实例不可同时接客，先处理旧副本）")

    # 存档迁出：实例 _save/agents/<id> → <助理>/_save（数据随顾问走）
    inst_save = B.agent_save_dir(root, args.agent_id)
    if os.path.isdir(inst_save) and any(os.scandir(inst_save)):
        if args.purge_data:
            shutil.rmtree(inst_save, ignore_errors=True)
        else:
            lite_save = B.agent_save_dir_lite(src)
            if os.path.exists(lite_save):
                return B.fail(f"放归目标已自带 _save：{lite_save}（先处理一处再放归）")
            os.makedirs(os.path.dirname(lite_save), exist_ok=True)
            shutil.move(inst_save, lite_save)
    elif args.purge_data and os.path.isdir(B.agent_save_dir_lite(src)):
        shutil.rmtree(B.agent_save_dir_lite(src), ignore_errors=True)

    if args.purge_data:
        if not args.yes:
            return B.fail("--purge-data 必须搭配 --yes（删除主人数据需显式确认）")

    os.makedirs(standalone_root, exist_ok=True)
    shutil.move(src, dest)

    reg["adopted"] = [e for e in reg.get("adopted", []) if e.get("id") != args.agent_id]
    B.save_registry(root, reg)
    B.append_jsonl(B.master_save_files(root)["releases"], {
        "id": args.agent_id, "released_at": B.now_iso(),
        "data": "purged" if args.purge_data else "kept-with-agent",
        "now_at": os.path.relpath(dest, root),
    })

    mode = B.detect_mode(dest)
    return B.ok({
        "released": args.agent_id,
        "now_at": os.path.relpath(dest, root),
        "mode_after_release": mode["mode"],
        "note": "助理已回 lite 模式，可独立使用；数据随它一起搬家",
    })


if __name__ == "__main__":
    sys.exit(main())
