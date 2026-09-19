#!/usr/bin/env python3
"""nutrition.py — 食物营养估算与食物库管理（确定性计算；口语理解归 AI）。

命令：
  estimate --food 鸡蛋 --portion "2个"     查库+折算克重+算宏量；库里没有/折算不了会明说
  add      --food X --cal N --protein N [--carbs N --fat N --category C --portion-gram N]
  search   --kw 鸡                          按关键字列库
  import-seed [--file seeds/food_library_seed.jsonl]   合并种子食物库（跳过重名）
  selfcheck
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fitlib as F  # noqa: E402


def cmd_estimate(a) -> int:
    library = [r for r in F.read_rows("food_library") if not r.get("_corrupt")]
    food = F.lookup_food(library, a.food)
    if not food:
        return F.jout({"ok": False,
                       "reason_code": "unknown_food",
                       "error": f"食物库里没有「{a.food}」",
                       "hint": "问主人营养值（每100g热量/蛋白），确认后用 add 入库"})
    grams, note = F.parse_grams(a.portion, food)
    if a.grams is not None:
        grams, note = float(a.grams), "direct_grams"
    if grams is None:
        return F.jout({"ok": False,
                       "reason_code": "need_portion",
                       "error": f"「{a.food}」的分量折算不了（{note}）",
                       "food": food.get("name"),
                       "common_portion": food.get("common_portion"),
                       "hint": "追问主人大概多少克，或多少个/碗（需库里有 common_portion 参照）"})
    macros = F.estimate_macros(food, grams)
    return F.jout({"ok": True, "food": food.get("name"), "portion": a.portion,
                   "convert_note": note, **macros,
                   "per_100g": {k: food.get(k) for k in
                                ("calories_per_100g", "protein_per_100g", "carbs_per_100g", "fat_per_100g")}})


def cmd_add(a) -> int:
    row = F.append_row("food_library", {
        "name": a.food.strip(),
        "category": a.category or "未分类",
        "calories_per_100g": a.cal,
        "protein_per_100g": a.protein,
        "carbs_per_100g": a.carbs,
        "fat_per_100g": a.fat,
        "common_portion": a.portion_ref,
        "recorded_at": F.today(),
    })
    return F.jout({"ok": True, "added": row["name"], "_id": row["_id"],
                   "note": "已入食物库，下次直接可用"})


def cmd_search(a) -> int:
    library = [r for r in F.read_rows("food_library") if not r.get("_corrupt")]
    kw = (a.kw or "").strip().lower()
    hits = [f for f in library if kw in str(f.get("name", "")).lower()] if kw else library
    return F.jout({"ok": True, "count": len(hits),
                   "items": [{"name": f.get("name"), "category": f.get("category"),
                              "kcal": f.get("calories_per_100g"), "protein": f.get("protein_per_100g"),
                              "common_portion": f.get("common_portion")} for f in hits[:30]]})


def cmd_import_seed(a) -> int:
    seed_file = a.file or os.path.join(F.root(), "seeds", "food_library_seed.jsonl")
    if not os.path.isfile(seed_file):
        return F.jfail(f"种子文件不存在：{seed_file}")
    existing = {str(r.get("name", "")).strip().lower() for r in F.read_rows("food_library")}
    added, skipped = [], 0
    with open(seed_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if str(item.get("name", "")).strip().lower() in existing:
                skipped += 1
                continue
            F.append_row("food_library", {**item, "recorded_at": F.today()})
            added.append(item.get("name"))
    return F.jout({"ok": True, "added": len(added), "skipped_existing": skipped,
                   "names": added[:50]})



def template_save(a) -> int:
    """把某天某餐的已有记录存为模板（数据来自 diet_logs，不重新估算）。"""
    d = (a.date or F.today())[:10]
    rows = [r for r in F.read_rows("diet_logs")
            if str(r.get("recorded_at", ""))[:10] == d and r.get("meal_type") == a.meal
            and not r.get("_corrupt")]
    if not rows:
        return F.jfail(f"{d} 的 {a.meal} 没有饮食记录，无从存模板")
    items = [{k: r.get(k) for k in ("food_name", "portion", "grams_est", "calories", "protein", "carbs", "fat")}
             for r in rows]
    total_calories = round(sum(float(i.get("calories") or 0) for i in items), 1)
    total_protein = round(sum(float(i.get("protein") or 0) for i in items), 1)
    row = F.append_row("meal_templates", {"name": a.name, "items_json": json.dumps(items, ensure_ascii=False),
                                          "total_calories": total_calories, "total_protein": total_protein,
                                          "use_count": 0, "recorded_at": F.today()})
    return F.jout({"ok": True, "template": a.name, "items": len(items),
                   "total_calories": total_calories, "_id": row["_id"],
                   "user_view": f"已存模板「{a.name}」（{total_calories}大卡），以后一句话就能整餐记。"})


def template_use(a) -> int:
    """按模板一键记一餐：展开 items 逐条 append diet_logs，use_count+1。"""
    if a.meal not in ("breakfast", "lunch", "dinner", "snack"):
        return F.jfail(f"meal_type 必须是 breakfast/lunch/dinner/snack，现在是 {a.meal}")
    tpls = [r for r in F.read_rows("meal_templates") if not r.get("_corrupt")]
    tpl = next((t for t in tpls if str(t.get("name", "")) == a.name.strip()), None)
    if not tpl:
        return F.jfail(f"没有叫「{a.name}」的模板", {"known": [t.get("name") for t in tpls]})
    items = json.loads(tpl.get("items_json") or "[]")
    added = []
    for it in items:
        row = {"meal_type": a.meal, **{k: it.get(k) for k in
               ("food_name", "portion", "grams_est", "calories", "protein", "carbs", "fat")},
               "template_id": tpl.get("_id"), "recorded_at": (a.date or F.today())[:10]}
        F.append_row("diet_logs", row)
        added.append(f"{it.get('food_name')}≈{it.get('calories')}kcal")
    new_use = int(tpl.get("use_count") or 0) + 1
    if tpl.get("_id"):
        all_rows = F.read_rows("meal_templates")
        # 重写前：全表旧行快照进 trash（与其他写操作同一纪律）
        from datetime import date
        trash = os.path.join(F.data_dir(), "trash", f"meal_templates-{date.today():%Y%m%d}.jsonl")
        os.makedirs(os.path.dirname(trash), exist_ok=True)
        with open(trash, "a", encoding="utf-8", newline="\n") as fh:
            for r in all_rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        for r in all_rows:
            if r.get("_id") == tpl.get("_id"):
                r["use_count"] = new_use
        F.write_table("meal_templates", all_rows)
    total = round(sum(float(it.get("calories") or 0) for it in items), 1)
    return F.jout({"ok": True, "template": a.name, "logged": len(added), "total_calories": total,
                   "user_view": f"按模板「{a.name}」记好{len(added)}项，合计{total}大卡。"})

def cmd_selfcheck(_a) -> int:
    library = [r for r in F.read_rows("food_library") if not r.get("_corrupt")]
    bad = [r for r in library if not r.get("name") or r.get("calories_per_100g") is None]
    return F.jout({"ok": not bad, "library_size": len(library),
                   "error": f"{len(bad)} 条不完整" if bad else None}, code=1 if bad else 0)


def main() -> int:
    ap = argparse.ArgumentParser(description="营养估算与食物库")
    ap.add_argument("--selfcheck", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=False)

    p_e = sub.add_parser("estimate")
    p_e.add_argument("--food", required=True)
    p_e.add_argument("--portion", default=None)
    p_e.add_argument("--grams", type=float, default=None, help="已知克重时直接给")
    p_a = sub.add_parser("add")
    p_a.add_argument("--food", required=True)
    p_a.add_argument("--cal", type=float, required=True)
    p_a.add_argument("--protein", type=float, required=True)
    p_a.add_argument("--carbs", type=float, default=0)
    p_a.add_argument("--fat", type=float, default=0)
    p_a.add_argument("--category", default=None)
    p_a.add_argument("--portion-ref", default=None, help="如 一碗≈200g")
    p_s = sub.add_parser("search")
    p_s.add_argument("--kw", default="")
    p_i = sub.add_parser("import-seed")
    p_i.add_argument("--file", default=None)
    p_ts = sub.add_parser("template-save")
    p_ts.add_argument("--name", required=True)
    p_ts.add_argument("--meal", default="dinner")
    p_ts.add_argument("--date", default=None)
    p_tu = sub.add_parser("template-use")
    p_tu.add_argument("--name", required=True)
    p_tu.add_argument("--meal", default="lunch")
    p_tu.add_argument("--date", default=None)
    sub.add_parser("selfcheck")

    x = ap.parse_args()
    if x.selfcheck:
        return cmd_selfcheck(x)
    if not x.cmd:
        ap.print_help()
        return F.jout({"ok": False, "error": "缺少子命令"}, code=2)
    return {"estimate": cmd_estimate, "add": cmd_add, "search": cmd_search,
            "import-seed": cmd_import_seed, "template-save": template_save,
            "template-use": template_use, "selfcheck": cmd_selfcheck}[x.cmd](x)


if __name__ == "__main__":
    sys.exit(main())
