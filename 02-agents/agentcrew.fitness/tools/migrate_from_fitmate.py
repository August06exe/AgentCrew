#!/usr/bin/env python3
"""migrate_from_fitmate.py — 从旧 FitMate（SQLite）一次性迁入数据。

只读旧库、只写本助理 data\\；默认 --dry-run。原数据永远不被改动。
--execute 为合并语义（非整表替换）：目标表既有行全部保留，迁移行按稳定 _id
（源自旧库主键）去重后追加——迁移前已有行不丢，重跑幂等不重复迁入。
报告 migrated（旧库读出）/appended（本次追加；DRY-RUN 下为预演值）/skipped（_id 已存在跳过）。
用法：
  python tools/migrate_from_fitmate.py --sqlite "D:/path/Fitness/data/fitness.db"            # 预览
  python tools/migrate_from_fitmate.py --sqlite "...db" --execute                            # 实际迁入
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fitlib as F  # noqa: E402

# 旧表 → 新表 与字段改名映射（新旧数据模型差异见 docs/2026-09-18-FitMate功能还原分析.md §三）
MAPS = {
    "diet_logs": ("diet_logs", {"calories_est": "calories", "protein_est": "protein",
                                "carbs_est": "carbs", "fat_est": "fat"}),
    "workouts": ("workouts", {}),
    "body_stats": ("body_stats", {}),
    "sleep_records": ("sleep_records", {"hours": "hours"}),
    "food_library": ("food_library", {}),
    "meal_templates": ("meal_templates", {"foods_json": "items_json"}),
    "goals": ("goals", {}),
}


def migrate(db_path: str, execute: bool) -> dict:
    if not os.path.isfile(db_path):
        return {"ok": False, "error": f"旧库不存在：{db_path}"}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    report = {}
    for old_table, (new_table, renames) in MAPS.items():
        try:
            cur = conn.execute(f"SELECT * FROM {old_table}")  # noqa: S608 表名来自内置映射
        except sqlite3.OperationalError as e:
            report[old_table] = {"skipped": f"旧库无此表：{e}"}
            continue
        rows = []
        for r in cur:
            row = {renames.get(k, k): r[k] for k in r.keys() if k != "id"}
            # 稳定 _id：无时间戳成分，重跑按 _id 去重才能幂等；旧表无主键时退化为行序
            row["_id"] = f"m-{old_table}-{r['id'] if 'id' in r.keys() else len(rows)}"
            rows.append(row)
        existing = F.read_rows(new_table)
        existing_ids = {r.get("_id") for r in existing if not r.get("_corrupt")}
        fresh = [r for r in rows if r["_id"] not in existing_ids]
        if execute and fresh:
            # 合并写入：既有行（含损坏行）原样保留，只追加去重后的迁移行——绝不整表替换
            F.write_table(new_table, existing + fresh)
        report[old_table] = {"migrated": len(rows), "appended": len(fresh),
                             "skipped": len(rows) - len(fresh), "target": new_table}
    # 旧 app_config → 新 config.json（键名直迁，主人可再改）
    try:
        kv = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_config")}
        if execute and kv:
            cfg = F.load_config()
            cfg.update(kv)
            F.save_config(cfg)
        report["app_config"] = {"keys": sorted(kv), "target": "config.json"}
    except sqlite3.OperationalError:
        report["app_config"] = {"skipped": "旧库无 app_config"}
    conn.close()
    return {"ok": True, "mode": "EXECUTED" if execute else "DRY-RUN", "tables": report}


def main() -> int:
    ap = argparse.ArgumentParser(description="旧 FitMate 数据迁移（只读旧库）")
    ap.add_argument("--sqlite", default=None, help="旧库路径；--selfcheck 时可省")
    ap.add_argument("--execute", action="store_true", help="缺省=只预览不写入")
    ap.add_argument("--selfcheck", action="store_true", help="收编校验：验证 sqlite3 能力与写入权限")
    a = ap.parse_args()
    if a.selfcheck:
        import sqlite3  # noqa: F401
        probe = os.path.join(F.data_dir(), "_migrate_probe.tmp")
        F.atomic_text(probe, "ok\n")
        os.remove(probe)
        return F.jout({"ok": True, "note": "sqlite3 可用；data\\ 可写；不迁移任何数据"})
    if not a.sqlite:
        return F.jfail("缺少 --sqlite 路径")
    return F.jout(migrate(a.sqlite, a.execute))


if __name__ == "__main__":
    sys.exit(main())
