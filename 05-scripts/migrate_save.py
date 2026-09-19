#!/usr/bin/env python3
"""migrate_save.py — 存档阶梯迁移工具。

两个职责：
1. v0→v1 落位迁移：把旧架构散落在程序区的增量数据（01-master 实例态、
   02-agents/*/{data,inbox,outbox}、dashboard/data.*）一键归入 _save/。幂等。
2. 阶梯升档：save.json 的 save_version 旧于程序时，逐版本执行迁移函数
   （v1→v2→…，每级一个 migrate_vN 函数；本版只有 v1）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentcrew_lib as B  # noqa: E402


def colocate_v0_to_v1(root: str) -> dict:
    """v0（散落）→ v1（_save 收敛）。幂等：只搬还散落着的。"""
    moved: list[str] = []
    skipped: list[str] = []
    sr = B.ensure_save(root, agents=sorted(
        d for d in os.listdir(B.p(root, "02-agents"))
        if os.path.isdir(B.p(root, "02-agents", d))))

    # 1) 总管侧实例态文件
    mf = B.master_save_files(root)
    legacy_master = {
        B.p(root, "01-master", "profile.json"): mf["profile"],
        B.p(root, "01-master", "registry.json"): mf["registry"],
        B.p(root, "01-master", "authorizations.jsonl"): mf["authorizations"],
        B.p(root, "01-master", "releases.jsonl"): mf["releases"],
        B.p(root, "01-master", "inbox-master"): mf["inbox_master"],
        B.p(root, "01-master", "trash"): mf["trash"],
    }
    for old, new in legacy_master.items():
        if not os.path.exists(old):
            continue
        if os.path.isdir(old) and not any(os.scandir(old)):
            shutil.rmtree(old, ignore_errors=True)  # 空壳目录直接清
            continue
        if os.path.exists(new):
            # 目标已存在（多为 ensure_save 先建了骨架）→ 合并内容后清源
            if os.path.isdir(old) and os.path.isdir(new):
                import glob as _g
                for item in os.listdir(old):
                    src_f, dst_f = B.p(old, item), B.p(new, item)
                    if os.path.exists(dst_f):
                        skipped.append(os.path.relpath(src_f, root))
                        continue
                    shutil.move(src_f, dst_f)
                    moved.append(os.path.relpath(src_f, root))
                shutil.rmtree(old, ignore_errors=True)
            else:
                skipped.append(os.path.relpath(old, root))
            continue
        os.makedirs(os.path.dirname(new), exist_ok=True)
        shutil.move(old, new)
        moved.append(os.path.relpath(old, root))

    # 2) 顾问切片：data / inbox / outbox / dashboard 生成数据
    agdir = B.p(root, "02-agents")
    for aid in sorted(os.listdir(agdir)):
        adir = B.p(agdir, aid)
        if not os.path.isdir(adir):
            continue
        sd = B.agent_save_dir(root, aid)
        for sub in ("data", "inbox", "outbox"):
            old_d = B.p(adir, sub)
            if not os.path.isdir(old_d):
                continue
            new_d = B.p(sd, sub)
            os.makedirs(new_d, exist_ok=True)
            for n in os.listdir(old_d):
                if n == ".gitkeep" or (sub == "inbox" and n == "done"):
                    continue
                src_f, dst_f = B.p(old_d, n), B.p(new_d, n)
                rel = f"02-agents/{aid}/{sub}/{n}"
                if os.path.exists(dst_f):
                    skipped.append(rel)
                    continue
                shutil.move(src_f, dst_f)
                moved.append(rel)
            # 清掉搬空的老目录（保留 .gitkeep/done 由 watch 忽略也行，直接删净）
            shutil.rmtree(old_d, ignore_errors=True)
        for n in ("data.js", "data.json"):
            old_f = B.p(adir, "dashboard", n)
            if os.path.isfile(old_f):
                os.makedirs(B.p(sd, "dashboard"), exist_ok=True)
                dst_f = B.p(sd, "dashboard", n)
                if os.path.exists(dst_f):
                    skipped.append(f"02-agents/{aid}/dashboard/{n}")
                else:
                    shutil.move(old_f, dst_f)
                    moved.append(f"02-agents/{aid}/dashboard/{n}")

    # 3) 登记档内顾问
    mani = B.load_save_manifest(root) or {}
    mani.setdefault("agents", {})
    for aid in sorted(os.listdir(B.p(sr, "agents"))):
        mani["agents"].setdefault(aid, {"migrated_at": B.now_iso()})
    mani["save_version"] = 1
    mani["colocated_at"] = B.now_iso()
    B.atomic_write_json(B.save_manifest_path(root), mani)
    return {"moved": moved, "skipped_existing": skipped,
            "moved_count": len(moved), "skipped_count": len(skipped)}


# ---- 阶梯升档注册表：新版本在此追加 migrate_v2, v3… ----
def migrate_chain(root: str) -> list[int]:
    mani = B.load_save_manifest(root)
    if mani is None:
        return []
    v = int(mani.get("save_version", 1))
    applied = []
    while v < B.SAVE_VERSION_CURRENT:
        step = globals().get(f"migrate_v{v + 1}")
        if not step:
            raise RuntimeError(f"缺 v{v}→v{v+1} 的迁移函数（程序 bug，勿动存档）")
        step(root)
        v += 1
        mani = B.load_save_manifest(root)
        mani["save_version"] = v
        B.atomic_write_json(B.save_manifest_path(root), mani)
        applied.append(v)
    return applied


def main() -> int:
    ap = argparse.ArgumentParser(description="存档迁移（v0散落→v1收敛 + 阶梯升档）")
    ap.add_argument("--root", default=None, help="目标仓库（缺省自动探测）")
    a = ap.parse_args()
    B.utf8_console()
    root = a.root or B.find_repo_root()
    if not root:
        return B.fail("未找到 AgentCrew 仓库根")
    report = colocate_v0_to_v1(root)
    applied = migrate_chain(root)
    stray_after = B.scan_stray_increment(root)
    return B.ok({
        "root": root,
        **report,
        "version_upgrades": applied or "已最新",
        "stray_after": stray_after[:10] or "程序区干净 ✓",
        "watch": "python 05-scripts/save.py watch 复核",
    })


if __name__ == "__main__":
    sys.exit(main())
