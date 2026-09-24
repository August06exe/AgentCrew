#!/usr/bin/env python3
"""kb.py — 理财知识库词条查询（仅标准库，只读）。

词条是**程序区**资产（seeds/kb_seed.jsonl，随版本升级，见设计稿 §4）：
不属于主人数据，谈到才翻，不占日常上下文。本工具绝不写任何文件、绝不联网。

命令：
  list [--category 记账与预算]        词条目录（名称/类别/别名，不带正文，省上下文）
  term --name 复利                    按名称或别名精确查一个词条（全文）
  search --q 回撤                     名称/别名/类别/释义/白话全文里模糊搜
  selfcheck
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REQUIRED_FIELDS = ("term", "aliases", "category", "definition", "plain_explain")
MIN_ENTRIES = 30  # 设计稿 §4：精选约 50 条；跌破此线视为种子库损坏/截断


def jout(obj: dict, code: int = 0) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return code


def jfail(error: str, extra: dict | None = None) -> int:
    return jout({"ok": False, "error": error, **(extra or {})}, code=1)


def agent_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def seed_path() -> str:
    env = os.environ.get("FINANCE_KB_SEED")  # 测试沙箱可指到临时词条文件
    return env or os.path.join(agent_root(), "seeds", "kb_seed.jsonl")


# ---------- 读取与查询（纯函数，selfcheck 直接验这里） ----------

def load_entries(path: str | None = None) -> tuple[list[dict], list[str]]:
    """读词条文件 → (合法词条, 坏行描述列表)。坏行跳过不致命（selfcheck 才算账）。"""
    p = path or seed_path()
    entries: list[dict] = []
    bad: list[str] = []
    if not os.path.isfile(p):
        return entries, [f"词条文件不存在：{p}"]
    with open(p, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                bad.append(f"第 {i} 行不是合法 JSON：{e}")
                continue
            missing = [k for k in REQUIRED_FIELDS if k not in row]
            if missing:
                bad.append(f"第 {i} 行缺字段：{'、'.join(missing)}")
                continue
            if not isinstance(row.get("aliases"), list) \
                    or not all(isinstance(x, str) for x in row["aliases"]):
                bad.append(f"第 {i} 行 aliases 应为字符串列表")
                continue
            entries.append(row)
    return entries, bad


def find_term(entries: list[dict], name: str) -> dict | None:
    key = name.strip().lower()
    for e in entries:
        if str(e.get("term", "")).strip().lower() == key:
            return e
    for e in entries:  # 别名第二优先
        if any(str(a).strip().lower() == key for a in e.get("aliases", [])):
            return e
    return None


def search_entries(entries: list[dict], q: str) -> list[dict]:
    key = q.strip().lower()
    if not key:
        return []
    fields = ("term", "aliases", "category", "definition", "plain_explain")
    hits: list[dict] = []
    for e in entries:
        matched = []
        for fld in fields:
            val = e.get(fld)
            hay = " ".join(val) if isinstance(val, list) else str(val or "")
            if key in hay.lower():
                matched.append(fld)
        if matched:
            hits.append({"term": e["term"], "aliases": e.get("aliases", []),
                         "category": e.get("category"),
                         "definition": e.get("definition"),
                         "plain_explain": e.get("plain_explain"),
                         "matched_in": matched})
    return hits


def _suggestions(entries: list[dict], name: str, limit: int = 5) -> list[str]:
    key = name.strip().lower()
    out = []
    for e in entries:
        hay = str(e.get("term", "")) + " " + " ".join(e.get("aliases", []))
        if key in hay.lower():
            out.append(e["term"])
        if len(out) >= limit:
            break
    return out


# ---------- 命令 ----------

def cmd_list(a) -> int:
    entries, bad = load_entries()
    if a.category:
        entries = [e for e in entries if str(e.get("category", "")) == a.category]
    cats: dict[str, int] = {}
    for e in entries:
        cats[e.get("category", "未分类")] = cats.get(e.get("category", "未分类"), 0) + 1
    return jout({"ok": True, "count": len(entries), "categories": dict(sorted(cats.items())),
                 "terms": [{"term": e["term"], "category": e.get("category"),
                            "aliases": e.get("aliases", [])} for e in entries],
                 **({"skipped_bad_lines": bad} if bad else {})})


def cmd_term(a) -> int:
    name = (a.name or "").strip()
    if not name:
        return jfail("--name 不能为空")
    entries, _ = load_entries()
    hit = find_term(entries, name)
    if not hit:
        return jout({"ok": False, "reason_code": "unknown_term",
                     "error": f"词条库里没有「{name}」",
                     "suggestions": _suggestions(entries, name),
                     "hint": "suggestions 是名称/别名含该字样的词条，可改用其一；"
                             "或如实告诉主人查无此条"})
    return jout({"ok": True, **hit})


def cmd_search(a) -> int:
    q = (a.q or "").strip()
    if not q:
        return jfail("--q 不能为空")
    entries, _ = load_entries()
    hits = search_entries(entries, q)
    return jout({"ok": True, "q": q, "count": len(hits), "hits": hits})


# ---------- selfcheck ----------

def cmd_selfcheck(_a) -> int:
    problems: list[str] = []
    entries, bad = load_entries()
    for b in bad:
        problems.append(b)
    if len(entries) < MIN_ENTRIES:
        problems.append(f"词条仅 {len(entries)} 条（低于 {MIN_ENTRIES} 的健全线，疑似种子库截断）")
    # 唯一性：term 与别名都不许撞（先收齐全量 term 名，再对别名做两向核对）
    term_owner: dict[str, str] = {}
    for e in entries:
        t = str(e.get("term", "")).strip().lower()
        if t in term_owner:
            problems.append(f"词条重名：{e.get('term')}（与 {term_owner[t]} 重复）")
        else:
            term_owner[t] = e.get("term", "")
    alias_owner: dict[str, str] = {}
    for e in entries:
        for alias in e.get("aliases", []):
            k = str(alias).strip().lower()
            if k in alias_owner:
                problems.append(f"别名撞车：{alias} 同时属于 {alias_owner[k]} 与 {e.get('term')}")
            elif k in term_owner:
                problems.append(f"别名与词条名撞车：{alias}（已是词条 {term_owner[k]} 的名字）")
            else:
                alias_owner[k] = e.get("term", "")
    # 功能冒烟：查得到第一条、别名字段生效、搜索命中自身
    if entries:
        first = entries[0]
        if find_term(entries, first["term"]) is None:
            problems.append(f"term 查询失效：查不到 {first['term']}")
        alias_owner = next((e for e in entries if e.get("aliases")), None)
        if alias_owner and find_term(entries, alias_owner["aliases"][0]) is not alias_owner:
            problems.append(f"别名查询失效：{alias_owner['aliases'][0]} 未归到 {alias_owner['term']}")
        if not any(h["term"] == first["term"] for h in search_entries(entries, first["term"])):
            problems.append(f"search 失效：搜 {first['term']} 无命中")
        if find_term(entries, "绝不存在的词条占位符"):
            problems.append("term 查询对不存在词条误命中")
    if problems:
        return jout({"ok": False, "error": "；".join(problems)}, code=1)
    cats = sorted({e.get("category", "未分类") for e in entries})
    return jout({"ok": True, "agent": "agentcrew.finance", "tool": "kb",
                 "entries": len(entries), "categories": cats,
                 "seed": os.path.relpath(seed_path(), agent_root())})


def main() -> int:
    ap = argparse.ArgumentParser(description="理财知识库词条查询（只读）")
    ap.add_argument("--selfcheck", action="store_true", help="自检（收编校验契约，等价于子命令 selfcheck）")
    sub = ap.add_subparsers(dest="cmd", required=False)

    p_l = sub.add_parser("list")
    p_l.add_argument("--category", default=None, help="只看某分类，缺省全部")

    p_t = sub.add_parser("term")
    p_t.add_argument("--name", required=True, help="词条名或别名，如 复利 / 利滚利")

    p_s = sub.add_parser("search")
    p_s.add_argument("--q", required=True, help="关键词（名称/别名/类别/释义/白话全文模糊匹配）")

    sub.add_parser("selfcheck")

    x = ap.parse_args()
    if x.selfcheck:
        return cmd_selfcheck(x)
    if not x.cmd:
        ap.print_help()
        return jout({"ok": False, "error": "缺少子命令（或用 --selfcheck）"}, code=2)
    return {"list": cmd_list, "term": cmd_term, "search": cmd_search,
            "selfcheck": cmd_selfcheck}[x.cmd](x)


if __name__ == "__main__":
    sys.exit(main())
