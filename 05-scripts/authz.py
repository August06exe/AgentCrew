#!/usr/bin/env python3
"""authz.py — 担保授权：把源助理某表导出为只读快照，投放给申请方 inbox，并写审计日志。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentcrew_lib as B  # noqa: E402


def agent_dir_of(root: str, reg: dict, agent_id: str) -> str | None:
    e = B.registry_find(reg, agent_id)
    if e:
        d = B.p(root, e.get("dir", f"02-agents/{agent_id}"))
        return d if os.path.isdir(d) else None
    d = B.p(root, "02-agents", agent_id)
    return d if os.path.isdir(d) else None


def cmd_sweep(root: str, _args) -> int:
    """扫描各助理 inbox 中的过期快照：过期即移入该助理 data/trash（PROTOCOL §6.2），并写审计。"""
    from datetime import datetime
    now = datetime.now().astimezone()
    swept = []
    agdir = B.p(root, "02-agents")
    if os.path.isdir(agdir):
        for d in sorted(os.listdir(agdir)):
            inbox = B.agent_mailbox_dir(B.p(agdir, d), "inbox")
            if not os.path.isdir(inbox):
                continue
            for fname in sorted(os.listdir(inbox)):
                if not fname.startswith("snapshot-") or not fname.endswith(".json"):
                    continue
                fp = B.p(inbox, fname)
                try:
                    snap = B.read_json(fp)
                except json.JSONDecodeError:
                    continue
                try:
                    expired = datetime.fromisoformat(snap.get("expires_at")) < now
                except (TypeError, ValueError):
                    expired = False
                if expired:
                    # 回收站落存档切片（写程序区 data\ 会触发 save.py watch 违规）
                    trash = B.p(B.resolve_agent_save(B.p(agdir, d)),
                                "data", "trash", "expired-snapshots.jsonl")
                    rec = dict(snap)
                    rec["_swept_at"] = B.now_iso()
                    B.append_jsonl(trash, rec)
                    os.remove(fp)
                    swept.append({"agent": d, "snapshot_id": snap.get("snapshot_id")})
    # 总管本人的信箱也要扫（快照实际投在存档信箱，程序区 01-master 已无此目录）
    master_inbox = B.master_save_files(root)["inbox_master"]
    if os.path.isdir(master_inbox):
        for fname in sorted(os.listdir(master_inbox)):
            if not fname.startswith("snapshot-") or not fname.endswith(".json"):
                continue
            fp = B.p(master_inbox, fname)
            try:
                snap = B.read_json(fp)
            except json.JSONDecodeError:
                continue
            try:
                expired = datetime.fromisoformat(snap.get("expires_at")) < now
            except (TypeError, ValueError):
                expired = False
            if expired:
                trash = B.p(B.master_save_files(root)["trash"], "expired-snapshots.jsonl")
                rec = dict(snap)
                rec["_swept_at"] = B.now_iso()
                B.append_jsonl(trash, rec)
                os.remove(fp)
                swept.append({"agent": "master", "snapshot_id": snap.get("snapshot_id")})
    B.append_jsonl(B.master_save_files(root)["authorizations"],
                   {"event": "sweep", "swept_at": B.now_iso(), "count": len(swept), "items": swept})
    return B.ok({"swept": len(swept), "items": swept})


def main() -> int:
    ap = argparse.ArgumentParser(description="担保授权：跨助理只读快照")
    ap.add_argument("--sweep", action="store_true", help="清扫各助理 inbox 中的过期快照")
    ap.add_argument("--from", dest="src", default=None, help="数据源助理 id")
    ap.add_argument("--table", default=None, help="表名（data/<table>.jsonl）")
    ap.add_argument("--to", dest="dst", default=None, help="申请方助理 id（或 master）")
    ap.add_argument("--purpose", default=None, help="用途（将展示给主人）")
    ap.add_argument("--hours", type=int, default=24, help="有效期小时数（默认24）")
    ap.add_argument("--granted", action="store_true", help="主人已批准（生产须由总管先问主人）")
    ap.add_argument("--dry-run", action="store_true", help="只显示将导出多少行，不落盘")
    args = ap.parse_args()
    B.utf8_console()

    root = B.find_repo_root()
    if args.sweep:
        return cmd_sweep(root, args)
    if not (args.src and args.table and args.dst and args.purpose):
        return B.fail("--from/--table/--to/--purpose 均必填（或用 --sweep 清扫过期快照）")
    if not root:
        return B.fail("未找到AgentCrew 仓库根")
    reg = B.load_registry(root)

    src_dir = agent_dir_of(root, reg, args.src)
    if not src_dir:
        return B.fail(f"源助理不存在：{args.src}")
    # 存档架构：表在存档切片（托管=实例 _save/agents/<id>/data；lite=助理自带 _save/data），
    # 程序区 data\ 已无数据（v0 位置读取永远落空）
    table_file = B.p(B.resolve_agent_save(src_dir), "data", f"{args.table}.jsonl")
    if not os.path.isfile(table_file):
        return B.fail(f"源助理没有这张表：{args.src}/data/{args.table}.jsonl")

    rows = [r for r in B.read_jsonl(table_file) if not r.get("_corrupt") and not r.get("_derived")]
    if args.dry_run:
        return B.ok({"would_export_rows": len(rows), "from": args.src, "table": args.table, "to": args.dst})

    if args.dst != "master":
        dst_dir = agent_dir_of(root, reg, args.dst)
        if not dst_dir:
            return B.fail(f"申请方助理不存在：{args.dst}")
        dest_path = B.p(B.agent_mailbox_dir(dst_dir, "inbox"), f"{B.new_id('snapshot')}.json")
    else:
        dest_path = B.p(B.master_save_files(root)["inbox_master"], f"{B.new_id('snapshot')}.json")

    granted_at = datetime.now().astimezone()
    snapshot = {
        "protocol": B.PROTOCOL,
        "snapshot_id": os.path.basename(dest_path)[:-5],
        "source_agent": args.src,
        "source_table": args.table,
        "purpose": args.purpose,
        "granted_by": "owner" if args.granted else "PENDING-OWNER-APPROVAL",
        "granted_at": granted_at.isoformat(timespec="seconds"),
        "expires_at": (granted_at + timedelta(hours=args.hours)).isoformat(timespec="seconds"),
        "read_only": True,
        "rows": rows,
        "row_count": len(rows),
    }
    B.atomic_write_json(dest_path, snapshot)
    B.append_jsonl(B.master_save_files(root)["authorizations"], {
        "snapshot_id": snapshot["snapshot_id"],
        "from": args.src, "table": args.table, "to": args.dst,
        "purpose": args.purpose, "granted_by": snapshot["granted_by"],
        "granted_at": snapshot["granted_at"], "expires_at": snapshot["expires_at"],
        "rows": len(rows), "file": os.path.relpath(dest_path, root),
    })
    return B.ok({"snapshot_id": snapshot["snapshot_id"], "rows": len(rows),
                 "delivered_to": os.path.relpath(dest_path, root),
                 "expires_at": snapshot["expires_at"],
                 "warning": None if args.granted else "未带 --granted：快照标记为 PENDING-OWNER-APPROVAL，助理不得使用"})


if __name__ == "__main__":
    sys.exit(main())
