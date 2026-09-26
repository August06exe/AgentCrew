#!/usr/bin/env python3
"""健身助理全流程自测（S7 验收）：seed → estimate → 记录四类 → rebuild → today/check → weekly → goals。"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = os.path.join(ROOT, "tools")
sys.path.insert(0, TOOLS)


SANDBOX = os.path.join(HERE, "_sandbox")
ENV = {**os.environ, "ASSISTANT_DATA_DIR": os.path.join(SANDBOX, "data"),
       "ASSISTANT_DASHBOARD_DIR": os.path.join(SANDBOX, "dashboard")}


def run(tool, *args, expect="ok"):
    r = subprocess.run([sys.executable, os.path.join(TOOLS, tool), *args],
                       capture_output=True, text=True, encoding="utf-8", cwd=ROOT, env=ENV)
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"FAIL(non-json): {tool} {args} rc={r.returncode} out={r.stdout[:300]} err={r.stderr[:300]}")
    if expect == "ok" and (r.returncode != 0 or not out.get("ok")):
        raise SystemExit(f"FAIL: {tool} {args} -> {json.dumps(out, ensure_ascii=False)[:300]}")
    if expect == "fail" and out.get("ok"):
        raise SystemExit(f"FAIL(应失败却成功): {tool} {args} -> {json.dumps(out, ensure_ascii=False)[:200]}")
    return out


def main() -> int:
    import shutil
    shutil.rmtree(SANDBOX, ignore_errors=True)  # 每轮全量清场，测试可重复
    run("data.py", "init")
    seed = run("nutrition.py", "import-seed")
    assert seed["added"] + seed.get("skipped_existing", 0) >= 35, \
        f"种子入库数不对（首次 added + 重跑 skipped 都该算）: {seed}"

    # 估算：克重直给
    e1 = run("nutrition.py", "estimate", "--food", "鸡胸肉", "--grams", "150")
    assert e1["calories"] == 200, f"鸡胸肉150g应为200kcal: {e1}"
    # 估算：常见分量参照（一个鸡蛋≈55g）
    e2 = run("nutrition.py", "estimate", "--food", "鸡蛋", "--portion", "2个")
    assert e2["ok"] and abs(e2["grams"] - 110) < 1, f"2个鸡蛋应为110g: {e2}"
    # 估算：库里没有
    e3 = run("nutrition.py", "estimate", "--food", "火星披萨", expect="fail")
    assert e3["reason_code"] == "unknown_food"
    # 估算：斤换算（168斤=84kg → 体直接记，不用营养库）

    # 配置（onboarding 槽位）
    run("daily.py", "set-config", "--key", "height_cm", "--value", "179")
    run("daily.py", "set-config", "--key", "age", "--value", "30")
    run("daily.py", "set-config", "--key", "sex", "--value", "male")
    run("daily.py", "set-config", "--key", "target_weight_kg", "--value", "75")
    run("daily.py", "set-config", "--key", "daily_deficit", "--value", "500")
    run("daily.py", "set-config", "--key", "protein_g_per_kg", "--value", "1.6")

    # 记录四类
    run("data.py", "append", "body_stats", "--json", '{"weight_kg": 84.0}')
    run("data.py", "append", "sleep_records", "--json", '{"hours": 7.5}')
    run("daily.py", "set-activity", "--level", "light")
    run("data.py", "append", "diet_logs", "--json",
        '{"meal_type":"breakfast","food_name":"鸡蛋","portion":"2个","grams_est":110,"calories":158,"protein":14.6,"carbs":3.1,"fat":9.7}')
    run("data.py", "append", "diet_logs", "--json",
        '{"meal_type":"lunch","food_name":"米饭(熟)","portion":"一碗","grams_est":200,"calories":232,"protein":5.2,"carbs":51.8,"fat":0.6}')
    run("data.py", "append", "workouts", "--json",
        '{"exercise":"卧推","weight_kg":80,"sets":5,"reps":5,"muscle_group":"胸"}')  # 无burn→按组估

    # 代谢
    td = run("daily.py", "today")
    assert td["metabolism"]["bmr"], f"BMR 应算出: {td}"
    assert td["intake"]["calories"] == 390, f"摄入应为390: {td}"
    assert td["metabolism"]["workout_burn"] == 30.0, f"5组×6kcal=30: {td}"
    assert td["targets"]["protein_target"] == 134, f"84×1.6=134.4→134: {td}"

    # 完整性检查（全齐→日总结）
    ck = run("daily.py", "check")
    assert ck["most_missing"] is None and "数据齐" in ck["user_view"], ck

    # 周报 + 目标
    wk = run("report.py", "weekly")
    assert wk["days_recorded"] >= 1 and "训练1天" in wk["user_view"], wk
    run("data.py", "append", "goals", "--json",
        '{"name":"减脂到75kg","timeframe":"long","metric":"weight_kg","target_value":75,"start_value":84,"current_value":84,"unit":"kg","end_date":"2026-12-31"}')
    gl = run("daily.py", "goals")
    assert gl["goals"][0]["current"] == 84.0, gl

    # C1 回归：速率型目标（daily/weekly）的 current 不被体重覆写，百分比标「按记录另计」
    run("data.py", "append", "goals", "--json",
        '{"name":"每周减0.57kg","timeframe":"weekly","metric":"weight_kg","start_value":0,"target_value":-0.57,"current_value":0,"unit":"kg","end_date":"2026-12-31"}')
    gl2 = run("daily.py", "goals")
    rate = next(g for g in gl2["goals"] if g["name"] == "每周减0.57kg")
    assert rate["rate"] is True and rate["progress_pct"] is None, f"速率目标不应有挤压百分比: {rate}"
    assert rate["progress_note"] == "按记录另计", f"速率目标应标按记录另计: {rate}"
    assert rate["current"] == 0, f"速率型目标 current 不得被体重覆写: {rate}"
    assert gl2["goals"][0]["current"] == 84.0, f"绝对值目标仍按最新体重推进: {gl2['goals']}"

    # C2 回归：周报对速率型目标同样不算假百分比、显示「按记录另计」
    wk2 = run("report.py", "weekly")
    gp = {g["name"]: g for g in wk2["goals_progress"]}
    assert gp["每周减0.57kg"]["progress_pct"] is None, f"周报速率目标不应有百分比: {gp}"
    assert gp["每周减0.57kg"]["progress_note"] == "按记录另计", gp
    assert gp["减脂到75kg"]["progress_pct"] is not None, f"绝对值目标周报仍应有百分比: {gp}"
    assert "按记录另计" in wk2["user_view"], wk2["user_view"]

    # 餐食模板 A6：存模板 → 用模板一键记一餐
    ts = run("nutrition.py", "template-save", "--name", "晨光套餐", "--meal", "breakfast")
    assert ts["ok"] and ts["items"] == 1, ts
    tu = run("nutrition.py", "template-use", "--name", "晨光套餐", "--meal", "lunch")
    assert tu["ok"] and tu["logged"] == 1 and tu["total_calories"] == 158, tu

    # 目标 update（D4 完成路径）+ 旧行进 trash
    q = run("data.py", "query", "goals")
    goal_id = q["rows"][-1]["_id"]
    up = run("data.py", "update", "goals", "--id", goal_id, "--set", "status=completed", "--yes")
    assert up["ok"], up
    assert os.path.isdir(os.path.join(SANDBOX, "data", "trash")), "update 应产生 trash 快照"

    # 迁移预览：造一个迷你旧库试试水
    import sqlite3
    tmp = os.path.join(tempfile.mkdtemp(), "old_fitness.db")
    conn = sqlite3.connect(tmp)
    conn.execute("CREATE TABLE diet_logs (id INTEGER PRIMARY KEY, raw_text TEXT, meal_type TEXT, food_name TEXT, portion TEXT, calories_est REAL, protein_est REAL, carbs_est REAL, fat_est REAL, recorded_at TEXT)")
    conn.execute("INSERT INTO diet_logs (meal_type, food_name, calories_est, protein_est, recorded_at) VALUES ('lunch','旧库鸡排',500,40,'2026-09-01')")
    conn.commit(); conn.close()
    mg = run("migrate_from_fitmate.py", "--sqlite", tmp)  # 默认 dry-run
    assert mg["mode"] == "DRY-RUN" and mg["tables"]["diet_logs"]["migrated"] == 1, mg

    # C3 回归：--execute 合并语义——迁移前已有行保留、按 _id 去重追加、重跑幂等
    run("data.py", "append", "diet_logs", "--json",
        '{"meal_type":"dinner","food_name":"迁移前手工行","calories":100,"recorded_at":"2026-08-31"}')
    mg1 = run("migrate_from_fitmate.py", "--sqlite", tmp, "--execute")
    t1 = mg1["tables"]["diet_logs"]
    assert t1["migrated"] == 1 and t1["appended"] == 1 and t1["skipped"] == 0, f"首跑应追加1条: {t1}"
    foods = [r.get("food_name") for r in run("data.py", "query", "diet_logs")["rows"]]
    assert foods.count("迁移前手工行") == 1, f"迁移前已有行必须保留: {foods}"
    assert foods.count("旧库鸡排") == 1, f"迁移行应恰好追加一条: {foods}"
    mg2 = run("migrate_from_fitmate.py", "--sqlite", tmp, "--execute")
    t2 = mg2["tables"]["diet_logs"]
    assert t2["migrated"] == 1 and t2["appended"] == 0 and t2["skipped"] == 1, f"重跑应按 _id 全去重: {t2}"
    foods2 = [r.get("food_name") for r in run("data.py", "query", "diet_logs")["rows"]]
    assert foods2.count("旧库鸡排") == 1 and foods2.count("迁移前手工行") == 1, \
        f"重跑不得重复迁入、不得丢既有行: {foods2}"

    run("data.py", "serve")
    assert os.path.isfile(os.path.join(HERE, "_sandbox", "dashboard", "data.js"))
    run("nutrition.py", "selfcheck")
    run("daily.py", "selfcheck")
    print("FITNESS FLOW OK（种子/估算/斤两换算/代谢/目标/周报/速率目标口径/检查/迁移合并幂等/看板 全过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
