#!/usr/bin/env python3
"""fitlib.py — 健身助理内部共享库（仅标准库，不依赖仓库其它文件，保持助理自包含）。"""
from __future__ import annotations

import json
import os
import random
import re
import sys
from datetime import datetime, date

ACTIVITY_FACTORS = {
    "bed": 1.1,          # 卧床
    "sedentary": 1.2,    # 久坐
    "light": 1.375,      # 轻度（默认）
    "moderate": 1.55,    # 中度
    "heavy": 1.725,      # 重度
    "very_heavy": 1.9,   # 极重
}
ACTIVITY_ZH = {
    "bed": "卧床", "sedentary": "久坐", "light": "轻度",
    "moderate": "中度", "heavy": "重度", "very_heavy": "极重",
}
ACTIVITY_ORDER = ["bed", "sedentary", "light", "moderate", "heavy", "very_heavy"]
DEFAULT_FACTOR_SEDENTARY = "sedentary"
KCAL_PER_SET_EST = 6.0  # 无消耗数据时按组数估算

MEAL_ZH = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐", "snack": "加餐"}


def root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def save_dir() -> str:
    """存档切片根（沙箱env > 实例 _save/agents/<id> > lite 自带 _save）。"""
    env = os.environ.get("ASSISTANT_DATA_DIR")
    if env:
        return os.path.abspath(env)
    agent = root()
    d = os.path.abspath(agent)
    passed = False
    while True:
        if os.path.basename(d) == "_standalone":
            passed = True
        if (not passed
                and os.path.isfile(os.path.join(d, "AGENTS.md"))
                and os.path.isdir(os.path.join(d, "01-master"))
                and os.path.isfile(os.path.join(d, "00-docs", "PROTOCOL.md"))
                and os.path.abspath(d) != os.path.abspath(agent)):
            return os.path.join(d, "_save", "agents", os.path.basename(os.path.abspath(agent)))
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.join(agent, "_save")
        d = parent


def data_dir() -> str:
    env = os.environ.get("ASSISTANT_DATA_DIR")  # 沙箱/外置：直接给数据目录本身（历史语义）
    if env:
        return os.path.abspath(env)
    return os.path.join(save_dir(), "data")


def table_path(name: str) -> str:
    return os.path.join(data_dir(), f"{name}.jsonl")


def config_path() -> str:
    return os.path.join(data_dir(), "config.json")


def load_config() -> dict:
    p = config_path()
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    cfg["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    atomic_json(config_path(), cfg)


def atomic_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def atomic_json(path: str, obj) -> None:
    atomic_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def read_rows(name: str) -> list[dict]:
    p = table_path(name)
    if not os.path.isfile(p):
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"_corrupt": line})
    return out


def append_row(name: str, row: dict) -> dict:
    row.setdefault("_id", f"r-{datetime.now().strftime('%Y%m%d%H%M%S')}-{random.randint(0, 0xffff):04x}")
    row.setdefault("created_at", now_iso())
    row.setdefault("recorded_at", today())
    os.makedirs(data_dir(), exist_ok=True)
    with open(table_path(name), "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def write_table(name: str, rows: list[dict]) -> None:
    atomic_text(table_path(name), "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()


def jout(obj: dict, code: int = 0) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return code


def jfail(error: str, extra: dict | None = None) -> int:
    return jout({"ok": False, "error": error, **(extra or {})}, code=1)


# ---------- 分量解析（确定性规则，AI 负责理解口语，这里只换算） ----------

NUM_ZH = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}


def _num(s: str) -> float | None:
    s = s.strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    if s in NUM_ZH:
        return NUM_ZH[s]
    m = re.match(r"^([一二两三四五六七八九十半]+)$", s)
    if m:
        # 简单组合：十X / X十 / X十Y
        t = m.group(1)
        if "十" in t:
            a, _, b = t.partition("十")
            ten = NUM_ZH.get(a, 1) if a else 1
            return ten * 10 + (NUM_ZH.get(b, 0) if b else 0)
        return NUM_ZH.get(t)
    return None


UNIT_GRAMS = {"g": 1, "克": 1, "ml": 1, "毫升": 1, "kg": 1000, "公斤": 1000,
              "斤": 500, "两": 50, "升": 1000, "l": 1000}


def parse_grams(portion: str | None, food: dict | None = None) -> tuple[float | None, str | None]:
    """把分量描述确定性换算成克。返回 (grams, note)；换算不了返回 (None, reason)。"""
    if not portion:
        return None, "no_portion"
    s = portion.strip().lower().replace(" ", "")
    # 纯克/毫升
    m = re.match(r"^(\d+(?:\.\d+)?)(g|克|ml|毫升)$", s)
    if m:
        return float(m.group(1)), None
    # 斤/两/公斤
    m = re.match(r"^(\d+(?:\.\d+)?|[一二两三四五六七八九十半]+)(kg|公斤|斤|两)$", s)
    if m:
        n = _num(m.group(1))
        if n is not None:
            return n * UNIT_GRAMS[m.group(2)], f"{m.group(1)}{m.group(2)}"
    # 「N个/碗/杯/片/根/块…」：优先用食物库 common_portion 里的 "一碗≈200g" 参照
    m = re.match(r"^(\d+(?:\.\d+)?|[一二两三四五六七八九十半]+)([个碗杯片根块勺份袋瓶罐条只])$", s)
    if m:
        n = _num(m.group(1))
        unit = m.group(2)
        if n is not None and food:
            ref = food.get("common_portion") or ""
            m2 = re.search(rf"一?{unit}\s*≈?\s*(\d+(?:\.\d+)?)\s*g", ref)
            if m2:
                return n * float(m2.group(1)), f"{m.group(1)}{unit}×{m2.group(1)}g"
            m3 = re.search(r"每(?:个|碗|杯|片|根|块|勺|份|袋|瓶|罐|条|只)\s*(\d+(?:\.\d+)?)\s*g", ref)
            if m3:
                return n * float(m3.group(1)), f"{m.group(1)}{unit}×{m3.group(1)}g"
        return None, f"need_unit_gram:{unit}"
    return None, "unparseable_portion"


def lookup_food(library: list[dict], name: str) -> dict | None:
    name = name.strip().lower()
    for f in library:
        if str(f.get("name", "")).strip().lower() == name:
            return f
    for f in library:  # 包含匹配（鸡蛋 ↔ 土鸡蛋）
        fn = str(f.get("name", "")).strip().lower()
        if fn and (fn in name or name in fn):
            return f
    return None


def estimate_macros(food: dict, grams: float) -> dict:
    per100 = lambda k: float(food.get(k) or 0)  # noqa: E731
    ratio = grams / 100.0
    return {
        "grams": round(grams, 1),
        "calories": round(per100("calories_per_100g") * ratio),
        "protein": round(per100("protein_per_100g") * ratio, 1),
        "carbs": round(per100("carbs_per_100g") * ratio, 1),
        "fat": round(per100("fat_per_100g") * ratio, 1),
    }


# ---------- 代谢计算（确定性，纯函数） ----------

def bmr_mifflin(weight_kg: float, height_cm: float, age: int, sex: str) -> float:
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + (5 if str(sex).lower().startswith("m") else -161)


def factor_for(d: date, activity: str | None, cfg: dict) -> tuple[str, float]:
    """当天活动系数：显式指定 > 活动水平配置 > 休息日降为久坐 > 默认轻度。"""
    rest_days = cfg.get("rest_days", [6, 7])  # ISO 周几（周一=1）
    if activity:
        lvl = activity
    elif d.isoweekday() in rest_days:
        lvl = DEFAULT_FACTOR_SEDENTARY
    else:
        lvl = cfg.get("default_activity", "light")
    return lvl, ACTIVITY_FACTORS.get(lvl, ACTIVITY_FACTORS["light"])


def targets_for(weight_kg: float | None, tdee: float, workout_burn: float, weekday: int, cfg: dict) -> dict:
    protein_coef = float(cfg.get("protein_g_per_kg", 1.6))
    deficit = float(cfg.get("daily_deficit", 500))
    off_days = set(cfg.get("deficit_off_days", [5, 6, 7]))  # 周五六日不设缺口
    w = weight_kg or float(cfg.get("last_weight_kg") or 0)
    protein_target = round(w * protein_coef) if w else None
    deficit_today = 0 if weekday in off_days else deficit
    calorie_target = round(tdee + workout_burn - deficit_today) if tdee else None
    return {
        "protein_target": protein_target,
        "calorie_target": calorie_target,
        "deficit_today": deficit_today,
        "fat_floor_g": round(w * 0.5, 1) if w else None,  # 底线信息，不强制入库
    }
