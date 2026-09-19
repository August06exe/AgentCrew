#!/usr/bin/env python3
"""dnd_check.py — 可推送时段核验（勿扰规则的唯一裁判）。

总管在【注册定时任务前】和【任务到点触发前】都必须调用本工具：
  python 05-scripts/dnd_check.py --at 23:30
返回 allowed=true 才可推送；false 时按章程向主人确认，绝不静默改期/跳过。
未建档时按"全天允许"处理（此时引导主人完成初始化优先）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentcrew_lib as B  # noqa: E402

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def to_min(hhmm: str) -> int | None:
    if not TIME_RE.match(str(hhmm or "")):
        return None
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


def parse_window(text: str) -> tuple[int, int] | None:
    m = re.match(r"^((?:[01]\d|2[0-3]):[0-5]\d)\s*[-–~至到]\s*((?:[01]\d|2[0-3]):[0-5]\d)$", str(text or "").strip())
    if not m:
        return None
    a, b = to_min(m.group(1)), to_min(m.group(2))
    if a is None or b is None:
        return None
    return (a, b)


def in_span(t: int, a: int, b: int) -> bool:
    """t 是否落在 [a,b]；支持跨午夜（a>b 时窗口绕零点）。"""
    if a <= b:
        return a <= t <= b
    return t >= a or t <= b


def main() -> int:
    ap = argparse.ArgumentParser(description="可推送时段核验")
    ap.add_argument("--at", required=True, help="待核验时刻 HH:MM（如 cron 触发时刻）")
    ap.add_argument("--profile", default=None, help="profile.json 路径（缺省自动找仓库内 01-master/profile.json）")
    a = ap.parse_args()
    B.utf8_console()

    t = to_min(a.at)
    if t is None:
        return B.fail(f"--at 需要 HH:MM 格式，现在是 {a.at}")

    root = B.find_repo_root()
    pp = a.profile or (B.master_save_files(root)["profile"] if root else None)
    if not pp or not os.path.isfile(pp):
        return B.ok({"allowed": True, "at": a.at, "reason": "未建档——按全天允许处理（应优先引导初始化）"})

    try:
        prof = B.read_json(pp)
    except json.JSONDecodeError as e:
        return B.fail(f"profile.json 损坏：{e}")

    n = prof.get("notifications") or {}
    window_text = str(n.get("window") or "00:00-23:59")

    # 主窗口：允许推送的时段
    if str(n.get("window_preset")) == "all_day" or window_text in ("all_day", "全天"):
        main_ok, main_span = True, None
    else:
        span = parse_window(window_text)
        if not span:
            return B.fail(f"notifications.window 无法解析：{window_text!r}（需 HH:MM-HH:MM / all_day）")
        main_ok, main_span = in_span(t, span[0], span[1]), span

    # 额外勿扰窗口（dnd_extra：绝对勿扰，覆盖主窗口）
    violated_extra = None
    for extra in n.get("dnd_extra") or []:
        es = parse_window(str(extra))
        if es and in_span(t, es[0], es[1]):
            violated_extra = str(extra)
            break

    allowed = main_ok and violated_extra is None
    out = {
        "allowed": allowed,
        "at": a.at,
        "window": window_text,
        "dnd_extra": n.get("dnd_extra") or [],
    }
    if not main_ok:
        out["reason"] = f"{a.at} 不在可推送时段 {window_text} 内"
        out["action"] = "向主人确认：照发还是改期？（不得静默改期/跳过）"
    elif violated_extra:
        out["reason"] = f"{a.at} 落在额外勿扰时段 {violated_extra} 内"
        out["action"] = "向主人确认：照发还是改期？（不得静默改期/跳过）"
    else:
        out["reason"] = "在可推送时段内，照发"
    return B.ok(out)


if __name__ == "__main__":
    sys.exit(main())
