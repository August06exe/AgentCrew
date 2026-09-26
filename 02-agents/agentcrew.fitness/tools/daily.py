#!/usr/bin/env python3
"""daily.py — 代谢与目标引擎（原版领域算法的确定性重实现，纯函数可测）。

命令：
  set-config --key height_cm --value 179     改配置（配置=数据，不改代码）
  set-activity --date 2026-09-18 --level light  手工指定某日活动水平（rebuild 会保留）
  rebuild                                    全量重建 daily_records 派生表（幂等）
  today   [--date D]                         当日营养/代谢/目标一句话数据
  check   [--date D]                         数据完整性检查（只报最缺一项）
  goals                                      目标进度（自动用最新体重推进身体类目标）
  selfcheck
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fitlib as F  # noqa: E402

CHECK_PRIORITY = [
    ("weight", "体重", "早上有空就上个体重秤？"),
    ("diet", "饮食", "今天吃了啥？说一句我记一下～"),
    ("sleep", "睡眠", "昨晚睡了几个小时？"),
    ("activity", "活动水平", "今天活动量如何？久坐/轻度/中度/重度？"),
]


def _d(s: str | None) -> date:
    return date.fromisoformat(s) if s else date.today()


def latest_weight_before(rows: list[dict], d: date) -> tuple[float | None, str | None]:
    best, bday = None, None
    for r in rows:
        try:
            rd = date.fromisoformat(str(r.get("recorded_at", ""))[:10])
        except ValueError:
            continue
        if rd <= d and (bday is None or rd > bday) and r.get("weight_kg") is not None:
            best, bday = float(r["weight_kg"]), rd
    return best, bday.isoformat() if bday else None


def _dates_in(*tables: str) -> set[str]:
    out: set[str] = set()
    for t in tables:
        for r in F.read_rows(t):
            if r.get("_corrupt") or r.get("_derived"):
                continue
            ra = str(r.get("recorded_at", ""))[:10]
            if ra:
                out.add(ra)
    return out


def cmd_set_config(a) -> int:
    cfg = F.load_config()
    raw = str(a.value).strip()
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        val = raw
    cfg[a.key] = val
    F.save_config(cfg)
    return F.jout({"ok": True, a.key: val})


def cmd_set_activity(a) -> int:
    if a.level not in F.ACTIVITY_FACTORS:
        return F.jfail(f"level 必须是 {list(F.ACTIVITY_FACTORS)}")
    d = _d(a.date).isoformat()
    recs = F.read_rows("daily_records")
    manual = {}
    for r in recs:
        if r.get("_manual_activity") and not r.get("_derived"):
            manual[str(r.get("recorded_at"))[:10]] = r
    row = manual.get(d) or {"_id": f"r-{datetime.now().strftime('%Y%m%d%H%M%S')}", "created_at": F.now_iso()}
    row.update({"recorded_at": d, "activity_level": a.level, "_manual_activity": True})
    manual[d] = row
    F.write_table("daily_records",
                  [{"_derived": True, "rebuilt_by": "tools/daily.py", "note": "手工活动水平行会由 rebuild 保留"}]
                  + list(manual.values()))
    return F.jout({"ok": True, "date": d, "activity_level": a.level,
                   "note": "已记录，rebuild 时保留；今天代谢按此计算"})


def rebuild() -> dict:
    cfg = F.load_config()
    diet = [r for r in F.read_rows("diet_logs") if not r.get("_corrupt")]
    workouts = [r for r in F.read_rows("workouts") if not r.get("_corrupt")]
    body = [r for r in F.read_rows("body_stats") if not r.get("_corrupt")]
    sleep = [r for r in F.read_rows("sleep_records") if not r.get("_corrupt")]
    prev = F.read_rows("daily_records")
    manual_act = {str(r.get("recorded_at"))[:10]: r.get("activity_level")
                  for r in prev if r.get("_manual_activity") and not r.get("_derived")}

    have_body = all(cfg.get(k) is not None for k in ("height_cm", "age", "sex"))
    rows = []
    for ds in sorted(_dates_in("body_stats", "diet_logs", "workouts", "sleep_records")):
        d = date.fromisoformat(ds)
        weight, wsrc = latest_weight_before(body, d)
        if cfg.get("last_weight_kg") is None and weight:
            cfg["last_weight_kg"] = weight
        day_diet = [r for r in diet if str(r.get("recorded_at", ""))[:10] == ds]
        tot = {k: round(sum(float(r.get(k) or 0) for r in day_diet), 1)
               for k in ("calories", "protein", "carbs", "fat")}
        day_wo = [r for r in workouts if str(r.get("recorded_at", ""))[:10] == ds]
        burn = 0.0
        estimated_sets = 0
        for r in day_wo:
            if r.get("rest_day"):
                continue
            if r.get("calories_burned") is not None:
                burn += float(r["calories_burned"])
            else:
                estimated_sets += int(r.get("sets") or 0)
        burn += estimated_sets * F.KCAL_PER_SET_EST
        burn = round(burn, 1)

        sleep_h = None
        for r in sleep:
            if str(r.get("recorded_at", ""))[:10] == ds and r.get("hours") is not None:
                sleep_h = float(r["hours"])

        level, factor = F.factor_for(d, manual_act.get(ds), cfg)
        bmr = tdee = None
        targets = {"protein_target": None, "calorie_target": None, "deficit_today": None, "fat_floor_g": None}
        if have_body and weight:
            bmr = round(F.bmr_mifflin(weight, float(cfg["height_cm"]), int(cfg["age"]), str(cfg["sex"])))
            tdee = round(bmr * factor)
            targets = F.targets_for(weight, tdee, burn, d.isoweekday(), cfg)
        net = round(tot["calories"] - (tdee + burn)) if tdee is not None else None
        rows.append({
            "_id": f"d-{ds}", "created_at": F.now_iso(), "recorded_at": ds,
            "weight_kg": weight, "sleep_hours": sleep_h,
            "total_calories": tot["calories"], "total_protein": tot["protein"],
            "total_carbs": tot["carbs"], "total_fat": tot["fat"],
            "activity_level": level, "activity_factor": factor,
            "bmr": bmr, "tdee": tdee, "calories_burned": burn,
            "protein_target": targets["protein_target"], "calorie_target": targets["calorie_target"],
            "net_calories": net,
            "_manual_activity": ds in manual_act,
        })
    F.write_table("daily_records",
                  [{"_derived": True, "rebuilt_by": "tools/daily.py",
                    "rebuilt_at": F.now_iso(),
                    "rebuilt_from": ["body_stats", "diet_logs", "workouts", "sleep_records", "config"]}]
                  + rows)
    return {"days": len(rows), "have_body_config": have_body}


def cmd_rebuild(_a) -> int:
    r = rebuild()
    return F.jout({"ok": True, **r, "note": None if r["have_body_config"]
                   else "config 缺 height_cm/age/sex（用 set-config 补齐后 BMR/TDEE 才会计算）"})


def _row_of(rows: list[dict], d: date) -> dict | None:
    ds = d.isoformat()
    for r in rows:
        if not r.get("_derived") and str(r.get("recorded_at", ""))[:10] == ds:
            return r
    return None


def cmd_today(a) -> int:
    d = _d(a.date)
    rebuild()
    row = _row_of(F.read_rows("daily_records"), d)
    if not row:
        return F.jout({"ok": True, "date": d.isoformat(), "empty": True,
                       "user_view": "今天还没有任何记录。"})
    p_t, c_t = row.get("protein_target"), row.get("calorie_target")
    pct_p = round(row["total_protein"] / p_t * 100) if p_t else None
    out = {
        "ok": True, "date": d.isoformat(),
        "weight_kg": row.get("weight_kg"), "sleep_hours": row.get("sleep_hours"),
        "activity": F.ACTIVITY_ZH.get(row.get("activity_level"), row.get("activity_level")),
        "intake": {"calories": row.get("total_calories"), "protein": row.get("total_protein"),
                   "carbs": row.get("total_carbs"), "fat": row.get("total_fat")},
        "macros_ratio": _pcf(row),
        "metabolism": {"bmr": row.get("bmr"), "tdee": row.get("tdee"),
                       "workout_burn": row.get("calories_burned"), "net": row.get("net_calories")},
        "targets": {"protein_target": p_t, "protein_pct": pct_p,
                    "calorie_target": c_t,
                    "calorie_left": (round(c_t - row["total_calories"]) if c_t else None)},
        "status": _status(row),
    }
    out["user_view"] = _today_line(out)
    return F.jout(out)


def _pcf(row: dict) -> str | None:
    p, c, f = row.get("total_protein") or 0, row.get("total_carbs") or 0, row.get("total_fat") or 0
    kc = p * 4 + c * 4 + f * 9
    if kc <= 0:
        return None
    return f"{round(p*4/kc*100)}%:{round(c*4/kc*100)}%:{round(f*9/kc*100)}%"


def _status(row: dict) -> dict:
    st = {}
    p_t = row.get("protein_target")
    if p_t:
        ratio = (row.get("total_protein") or 0) / p_t
        st["protein"] = "good" if ratio >= 0.95 else "warn" if ratio >= 0.8 else "bad"
    c_t = row.get("calorie_target")
    if c_t:
        over = (row.get("total_calories") or 0) - c_t
        st["calorie"] = "good" if over <= 0 else "warn" if over <= 150 else "bad"
    return st


def _today_line(o: dict) -> str:
    i = o["intake"]
    t = o["targets"]
    parts = []
    if i["calories"]:
        parts.append(f"摄入{i['calories']}大卡")
    if t.get("protein_pct") is not None:
        parts.append(f"蛋白{i['protein']}/{t['protein_target']}g（{t['protein_pct']}%）")
    if o["metabolism"].get("net") is not None:
        parts.append(f"净热量{o['metabolism']['net']}")
    return "，".join(parts) + "。" if parts else "今天还没有记录。"


def cmd_check(a) -> int:
    d = _d(a.date)
    rebuild()
    row = _row_of(F.read_rows("daily_records"), d)
    missing = {"weight": not (row and row.get("weight_kg")),
               "diet": not (row and (row.get("total_calories") or 0) > 0),
               "sleep": not (row and row.get("sleep_hours")),
               "activity": not (row and row.get("_manual_activity"))}
    for key, zh, msg in CHECK_PRIORITY:
        if missing[key]:
            return F.jout({"ok": True, "date": d.isoformat(), "missing": missing,
                           "most_missing": key, "zh": zh,
                           "reminder": msg,
                           "note": "只提最缺一项（优先级：体重>饮食>睡眠>活动水平）"})
    today_out = json.loads(_capture_today(a))
    return F.jout({"ok": True, "date": d.isoformat(), "missing": {},
                   "most_missing": None, "summary": today_out.get("user_view"),
                   "user_view": f"今天数据齐啦：{today_out.get('user_view')} 晚安～"})


def _capture_today(a) -> str:
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_today(a)
    return buf.getvalue()


def cmd_goals(_a) -> int:
    all_rows = F.read_rows("goals")
    rows = [r for r in all_rows if not r.get("_corrupt")]
    active = [g for g in rows if g.get("status", "active") == "active"]
    weight, _ = latest_weight_before(F.read_rows("body_stats"), date.today())
    out = []
    changed = False
    for g in active:
        is_rate = g.get("timeframe") in ("daily", "weekly")
        # 速率型目标的 current 不随体重推进：覆写会把"每周减0.57kg"持久化成绝对体重，
        # 语义被破坏（PENDING #14，口径说明见下）
        if g.get("metric") == "weight_kg" and weight and not is_rate:
            if g.get("current_value") != weight:
                g["current_value"] = weight
                changed = True
        start, target, current = (g.get("start_value"), g.get("target_value"), g.get("current_value"))
        pct = None
        if None not in (start, target, current) and start != target:
            pct = round(max(0.0, min(1.0, (current - start) / (target - start))) * 100)
        days_left = None
        if g.get("end_date"):
            try:
                days_left = (date.fromisoformat(str(g["end_date"])) - date.today()).days
            except ValueError:
                pass
        # 达成标记仅限自动追踪的绝对值目标（体重类）；速率型目标（如每周减重 kg/周）
        # 的百分比是"挤压计算"，不构成达成事实——达成要主人亲口说（PENDING #14）
        # 速率型目标（daily/weekly，如"每周减重0.57kg/周"）的 current 不随体重推进，
        # 挤压出的百分比是假的——如实标"按记录另计"，且永不自动判达成（PENDING #14）
        if is_rate:
            pct = None
        achieved = pct is not None and pct >= 100
        out.append({"name": g.get("name"), "metric": g.get("metric"),
                    "current": current, "target": target, "unit": g.get("unit"),
                    "progress_pct": pct, "days_left": days_left,
                    "achieved": achieved, "rate": is_rate,
                    "progress_note": "按记录另计" if is_rate else None,
                    "due_soon": days_left is not None and 0 <= days_left <= 30})
    if changed:
        # 重写前：全表旧行快照进 trash；损坏行原样保留，绝不静默丢弃
        trash = os.path.join(F.data_dir(), "trash", f"goals-{date.today():%Y%m%d}.jsonl")
        os.makedirs(os.path.dirname(trash), exist_ok=True)
        with open(trash, "a", encoding="utf-8", newline="\n") as fh:
            for r in all_rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        by_id = {g.get("_id"): g for g in rows if g.get("_id")}
        merged = [by_id.get(r.get("_id"), r) for r in all_rows]
        F.write_table("goals", merged)
    achieved_names = [g["name"] for g in out if g.get("achieved")]
    base = ("；".join(f"{g['name']}：{g['progress_pct']}%" for g in out if g["progress_pct"] is not None) + "。") if any(g["progress_pct"] is not None for g in out) else ("有目标在跟踪。" if out else "还没有目标，说「设目标」来一个。")
    if achieved_names:
        base = f"🎉 目标达成：{'、'.join(achieved_names)}！恭喜主人，可以说「目标完成了」把它归档，再定一个新的。" + base
    return F.jout({"ok": True, "goals": out,
                   "achieved": achieved_names,
                   "user_view": base})


def cmd_selfcheck(_a) -> int:
    cfg = F.load_config()
    days = rebuild()
    return F.jout({"ok": True, "config_keys": sorted(k for k in cfg if not k.startswith("_")),
                   "days_rebuilt": days["days"]})


def main() -> int:
    ap = argparse.ArgumentParser(description="代谢与目标引擎")
    ap.add_argument("--selfcheck", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=False)
    p_c = sub.add_parser("set-config")
    p_c.add_argument("--key", required=True)
    p_c.add_argument("--value", required=True)
    p_a = sub.add_parser("set-activity")
    p_a.add_argument("--date", default=None)
    p_a.add_argument("--level", required=True)
    sub.add_parser("rebuild")
    p_t = sub.add_parser("today")
    p_t.add_argument("--date", default=None)
    p_k = sub.add_parser("check")
    p_k.add_argument("--date", default=None)
    sub.add_parser("goals")
    sub.add_parser("selfcheck")

    x = ap.parse_args()
    if x.selfcheck:
        return cmd_selfcheck(x)
    if not x.cmd:
        ap.print_help()
        return F.jout({"ok": False, "error": "缺少子命令"}, code=2)
    return {"set-config": cmd_set_config, "set-activity": cmd_set_activity,
            "rebuild": cmd_rebuild, "today": cmd_today, "check": cmd_check,
            "goals": cmd_goals, "selfcheck": cmd_selfcheck}[x.cmd](x)


if __name__ == "__main__":
    sys.exit(main())
