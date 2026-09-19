#!/usr/bin/env python3
"""report.py — 周报生成（数据汇总 + 文字摘要 + 看板数据刷新）。周=ISO周（周一~周日）。

命令：
  weekly [--date D] [--offset N]    生成某日所在 ISO 周的周报（offset=1 即上周）
  selfcheck
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fitlib as F  # noqa: E402


def iso_week_range(d: date) -> tuple[date, date]:
    monday = d - timedelta(days=d.isoweekday() - 1)
    return monday, monday + timedelta(days=6)


def cmd_weekly(a) -> int:
    d = date.fromisoformat(a.date) if a.date else date.today()
    if a.offset:
        d = d - timedelta(weeks=a.offset)
    mon, sun = iso_week_range(d)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import daily as D  # 同目录兄弟模块，复用 rebuild
    D.rebuild()
    rows = [r for r in F.read_rows("daily_records") if not r.get("_derived")]
    week = [r for r in rows if mon.isoformat() <= str(r.get("recorded_at", ""))[:10] <= sun.isoformat()]

    weights = [(r["recorded_at"], r["weight_kg"]) for r in week if r.get("weight_kg")]
    intakes = [r["total_calories"] for r in week if r.get("total_calories")]
    deficits = [round((r["tdee"] + (r.get("calories_burned") or 0)) - r["total_calories"])
                for r in week if r.get("tdee")]
    proteins = [(r.get("total_protein"), r.get("protein_target")) for r in week if r.get("protein_target")]
    training_days = len({r["recorded_at"] for r in week if (r.get("calories_burned") or 0) > 0})

    goals_rows = [g for g in F.read_rows("goals")
                  if not g.get("_corrupt") and g.get("status", "active") == "active"]
    # 目标进度（P3-7）：体重类目标按最新体重算完成百分比
    goals_progress = []
    _body = F.read_rows("body_stats")
    _weight = None
    for r in _body:
        if r.get("weight_kg") is not None:
            _weight = float(r["weight_kg"])  # 行序即时间序，取最后一条
    for g in goals_rows:
        pct = None
        start, target = g.get("start_value"), g.get("target_value")
        current = _weight if g.get("metric") == "weight_kg" and _weight else g.get("current_value")
        if None not in (start, target, current) and start != target:
            pct = round(max(0.0, min(1.0, (current - start) / (target - start))) * 100)
        goals_progress.append({"name": g.get("name"), "current": current,
                               "target": target, "progress_pct": pct})

    summary = {
        "week": f"{mon.isocalendar().year}-W{mon.isocalendar().week:02d}",
        "range": [mon.isoformat(), sun.isoformat()],
        "days_recorded": len(week),
        "weight_change_kg": round(weights[-1][1] - weights[0][1], 2) if len(weights) >= 2 else None,
        "weight_first": weights[0][1] if weights else None,
        "weight_last": weights[-1][1] if weights else None,
        "avg_intake": round(sum(intakes) / len(intakes)) if intakes else None,
        "avg_deficit": round(sum(deficits) / len(deficits)) if deficits else None,
        "avg_protein": round(sum(p for p, _ in proteins) / len(proteins), 1) if proteins else None,
        "protein_target": proteins[0][1] if proteins else None,
        "training_days": training_days,
        "goals_active": len(goals_rows),
        "goals_progress": goals_progress,
    }
    summary["user_view"] = _line(summary)
    if goals_progress:
        gp = "；".join(f"{g['name']}{g['progress_pct']}%" for g in goals_progress if g["progress_pct"] is not None)
        if gp:
            summary["user_view"] = summary["user_view"] + " 目标进度：" + gp + "。"
    path = os.path.join(F.data_dir(), "reports", f"weekly-{summary['week']}.json")
    F.atomic_json(path, summary)
    return F.jout({"ok": True, **summary, "report_file": os.path.relpath(path, F.root()),
                   "dashboard_hint": "运行 tools/data.py serve 刷新看板数据"})


def _line(s: dict) -> str:
    parts = []
    if s["weight_change_kg"] is not None:
        arrow = "↓" if s["weight_change_kg"] < 0 else "↑"
        parts.append(f"体重{arrow}{abs(s['weight_change_kg'])}kg（{s['weight_first']}→{s['weight_last']}）")
    if s["avg_intake"]:
        parts.append(f"日均摄入{s['avg_intake']}大卡")
    if s["avg_deficit"] is not None:
        parts.append(f"日均缺口{s['avg_deficit']}大卡")
    if s["avg_protein"] is not None:
        parts.append(f"日均蛋白{s['avg_protein']}g")
    parts.append(f"训练{s['training_days']}天")
    return "，".join(parts) + "。"


def cmd_selfcheck(_a) -> int:
    return F.jout({"ok": True, "note": "weekly 可用；不依赖网络"})


def main() -> int:
    ap = argparse.ArgumentParser(description="周报生成")
    ap.add_argument("--selfcheck", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=False)
    p_w = sub.add_parser("weekly")
    p_w.add_argument("--date", default=None)
    p_w.add_argument("--offset", type=int, default=0)
    sub.add_parser("selfcheck")
    x = ap.parse_args()
    if x.selfcheck:
        return cmd_selfcheck(x)
    if not x.cmd:
        ap.print_help()
        return F.jout({"ok": False, "error": "缺少子命令"}, code=2)
    return {"weekly": cmd_weekly, "selfcheck": cmd_selfcheck}[x.cmd](x)


if __name__ == "__main__":
    sys.exit(main())
