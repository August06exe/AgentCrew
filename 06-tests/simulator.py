#!/usr/bin/env python3
"""simulator.py — 联邦协议全链路模拟（无 LLM）。

演练：路由 → 写交办单 → 模拟助理执行（真实调各助理工具落库）→ 回执 → 总管确认。
真实场景里"模拟助理执行"这一步由宿主 AI 会话完成；协议与文件完全一致。

用法：python 06-tests/simulator.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "05-scripts"))
import agentcrew_lib as B  # noqa: E402

PASS, FAIL = "✓", "✗"
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> None:
    results.append((bool(cond), label))
    print(f" {PASS if cond else FAIL} {label}")


SANDBOX = os.path.join(HERE, "_sandbox", "simulator")  # 数据落自己的子目录，跑完整体拆除


def run_tool(adir: str, rel: str, *args):
    import subprocess
    env = {**os.environ, "ASSISTANT_DATA_DIR": SANDBOX}
    r = subprocess.run([sys.executable, B.p(adir, *rel.split("/")), *args],
                       capture_output=True, text=True, encoding="utf-8", cwd=adir, timeout=60, env=env)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": (r.stderr or r.stdout)[:300]}


# ---------- 总管侧 ----------

def route(utterance: str, agents: list[dict]) -> dict:
    """字符 bigram 相似度路由（真实总管由 LLM 判断，此处为确定性演示）。"""
    def grams(s: str) -> set:
        return {s[i:i + 2] for i in range(len(s) - 1)}
    u = grams(utterance)
    best, best_score = None, 0.0
    for a in agents:
        m = a["manifest"]
        score = max((len(u & grams(ex)) / max(1, len(u | grams(ex)))
                     for ex in (m.get("routing_examples") or {}).get("positive") or []), default=0)
        if score > best_score:
            best, best_score = a, score
    return {"agent": best, "score": round(best_score, 3)}


CREATED: list[str] = []  # 本次模拟创建的信箱产物，退出时清场


def _cleanup_created(created: list[str]) -> int:
    n = 0
    for p in created:
        try:
            os.remove(p)
            n += 1
        except OSError:
            pass
    return n


def dispatch(agent_id: str, kind: str, utterance: str, slots: dict | None = None) -> str:
    adir = B.p(ROOT, "02-agents", agent_id)
    task = {
        "protocol": B.PROTOCOL,
        "task_id": B.new_id("t"),
        "from": "master", "to": agent_id, "kind": kind,
        "created_at": B.now_iso(), "priority": "normal", "deadline": None,
        "request": {"utterance": utterance, "intent_hint": kind, "slots": slots or {}, "context_refs": []},
        "reply_to": "outbox",
    }
    B.atomic_write_json(B.p(B.agent_mailbox_dir(adir, "inbox"), task["task_id"] + ".json"), task)
    CREATED.append(B.p(B.agent_mailbox_dir(adir, "inbox"), task["task_id"] + ".json"))
    return task["task_id"]


# ---------- 助理侧（模拟 AI 会话执行，真实调工具） ----------

def assistant_execute(agent_id: str, task_id: str) -> dict:
    adir = B.p(ROOT, "02-agents", agent_id)
    tpath = B.p(B.agent_mailbox_dir(adir, "inbox"), task_id + ".json")
    task = B.read_json(tpath)
    req = task["request"]
    if agent_id == "demo.sample":
        result = _sample_do(adir, req)
    else:
        result = _fitness_do(adir, req)
    receipt = {
        "protocol": B.PROTOCOL, "task_id": task_id, "from": agent_id,
        "status": result.pop("status"), "created_at": B.now_iso(), "result": result,
    }
    os.makedirs(B.p(B.agent_mailbox_dir(adir, "inbox"), "done"), exist_ok=True)
    receipt_path = B.p(B.agent_mailbox_dir(adir, "outbox"), task_id + ".receipt.json")
    B.atomic_write_json(receipt_path, receipt)
    CREATED.append(receipt_path)
    done_path = B.p(B.agent_mailbox_dir(adir, "inbox"), "done", task_id + ".json")
    os.replace(tpath, done_path)
    CREATED.append(done_path)
    return receipt


def _sample_do(adir: str, req: dict) -> dict:
    u = req["utterance"]
    if u.startswith("记一下") or u.startswith("帮我记着"):
        title = u.replace("记一下", "").replace("帮我记着", "").replace("：", "").strip()
        r = run_tool(adir, "tools/data.py", "append", "notes",
                     "--json", json.dumps({"title": title}, ensure_ascii=False))
        return {"status": "done", "summary": f"已记录：{title}", "table": "notes", "_id": r.get("appended"),
                "user_view": f"记好了：{title}。"}
    return {"status": "done", "summary": "查询结果见 rows", "rows": run_tool(adir, "tools/data.py", "query", "notes", "--last", "5")["rows"],
            "user_view": f"你记过 {len(run_tool(adir, 'tools/data.py', 'query', 'notes')['rows'])} 条。"}


def _fitness_do(adir: str, req: dict) -> dict:
    slots = req.get("slots") or {}
    if req.get("intent_hint") == "record_diet":
        meal = slots.get("meal_type", "snack")
        items, total = [], {"calories": 0, "protein": 0.0}
        for it in slots.get("items", []):
            est = run_tool(adir, "tools/nutrition.py", "estimate",
                           "--food", it["food"], "--portion", it.get("portion") or "")
            if not est.get("ok"):
                if est.get("reason_code") == "unknown_food":
                    return {"status": "need_input",
                            "questions": [f"「{it['food']}」不在我食物库里，它每100g热量/蛋白大概多少？"],
                            "reason_code": "unknown_food",
                            "user_view": f"「{it['food']}」我不认识，告诉我营养值我就记上～"}
                return {"status": "need_input", "questions": [est.get("error")],
                        "reason_code": "need_portion",
                        "user_view": f"「{it['food']}」大概多少克？"}
            row = {"meal_type": meal, "food_name": est["food"], "portion": it.get("portion"),
                   "grams_est": est["grams"], "calories": est["calories"], "protein": est["protein"],
                   "carbs": est.get("carbs"), "fat": est.get("fat"), "raw_text": req["utterance"]}
            run_tool(adir, "tools/data.py", "append", "diet_logs", "--json", json.dumps(row, ensure_ascii=False))
            items.append(f"{est['food']} {est['grams']}g≈{est['calories']}kcal")
            total["calories"] += est["calories"]
            total["protein"] += est["protein"]
        return {"status": "done", "summary": "；".join(items),
                "total_calories": total["calories"], "total_protein": round(total["protein"], 1),
                "user_view": f"记好了，{'；'.join(items)}，合计{total['calories']}大卡。"}
    if req.get("intent_hint") == "daily_check":
        out = run_tool(adir, "tools/daily.py", "check")
        return {"status": "done", "summary": out.get("user_view") or out.get("reminder"),
                "most_missing": out.get("most_missing"), "user_view": out.get("user_view") or out.get("reminder")}
    return {"status": "failed", "error": "模拟器未覆盖该 intent", "user_view": "这个我还不会。"}


# ---------- 主流程 ----------

def master_collect(agent_id: str, task_id: str) -> dict:
    adir = B.p(ROOT, "02-agents", agent_id)
    rp = B.p(B.agent_mailbox_dir(adir, "outbox"), task_id + ".receipt.json")
    if not os.path.isfile(rp):
        return {"ok": False, "error": "回执未生成"}
    r = B.read_json(rp)
    os.remove(rp)  # 总管收取
    return {"ok": True, **r}


def main() -> int:
    B.utf8_console()
    print("# 联邦协议全链路模拟\n")

    shutil.rmtree(SANDBOX, ignore_errors=True)  # 起跑先拆旧沙箱：数据目录全新
    reg = B.load_registry(ROOT)
    agents = []
    for e in reg.get("adopted", []):
        adir = B.p(ROOT, e.get("dir", ""))
        if os.path.isdir(adir):
            agents.append({"id": e["id"], "dir": e["dir"], "manifest": B.load_manifest(adir)})
    check(len(agents) >= 2, f"收编区有 {len(agents)} 个托管助理")

    # 0) 沙箱播种：健身食物库（沙箱数据目录是全新的）
    fit_dir = B.p(ROOT, "02-agents", "agentcrew.fitness")
    seed = run_tool(fit_dir, "tools/nutrition.py", "import-seed")
    check(seed.get("ok"), f"沙箱食物库播种（新增 {seed.get('added', 0)} 条）")

    # 1) 路由
    r1 = route("记一下明天要还书", agents)
    check(r1["agent"] and r1["agent"]["id"] == "demo.sample", f"路由「记一下…」→ {r1['agent']['id'] if r1['agent'] else '?'} ({r1['score']})")
    r2 = route("早餐吃了3个鸡蛋一杯牛奶", agents)
    check(r2["agent"] and r2["agent"]["id"] == "agentcrew.fitness", f"路由「早餐吃了…」→ {r2['agent']['id'] if r2['agent'] else '?'} ({r2['score']})")

    # 2) 交办→执行→回执→收取：备忘记录
    tid = dispatch("demo.sample", "record", "记一下明天要还书")
    rec = assistant_execute("demo.sample", tid)
    check(rec["status"] == "done" and "还书" in rec["result"]["user_view"], f"样板记录回执：{rec['result']['user_view']}")
    got = master_collect("demo.sample", tid)
    check(got["ok"] is not False and got["status"] == "done", "总管收取回执并转达")
    check(os.path.isfile(B.p(B.agent_mailbox_dir(B.p(ROOT, "02-agents/demo.sample"), "inbox"), "done", tid + ".json")), "交办单已归档 inbox/done")
    check(not os.path.isfile(B.p(B.agent_mailbox_dir(B.p(ROOT, "02-agents/demo.sample"), "outbox"), tid + ".receipt.json")), "outbox 已清（已转达）")

    # 3) 健身：正常记录（slots 模拟 LLM 抽取结果）
    tid2 = dispatch("agentcrew.fitness", "record_diet", "早餐吃了3个鸡蛋一杯牛奶",
                    {"meal_type": "breakfast", "items": [{"food": "鸡蛋", "portion": "3个"},
                                                          {"food": "纯牛奶", "portion": "一杯"}]})
    rec2 = assistant_execute("agentcrew.fitness", tid2)
    check(rec2["status"] == "done" and rec2["result"]["total_calories"] == 400,
          f"健身记录回执：3蛋238+牛奶162={rec2['result'].get('total_calories')}kcal")
    master_collect("agentcrew.fitness", tid2)

    # 4) 健身：未知食物 → need_input（总管会代问主人）
    tid3 = dispatch("agentcrew.fitness", "record_diet", "午饭吃了火星披萨",
                    {"meal_type": "lunch", "items": [{"food": "火星披萨", "portion": "两块"}]})
    rec3 = assistant_execute("agentcrew.fitness", tid3)
    check(rec3["status"] == "need_input" and rec3["result"]["reason_code"] == "unknown_food",
          "未知食物 → need_input 追问（不硬编数据）")
    master_collect("agentcrew.fitness", tid3)

    # 5) 晚间检查（哑钟到点后的固定流程）
    tid4 = dispatch("agentcrew.fitness", "daily_check", "晚间数据完整性检查")
    rec4 = assistant_execute("agentcrew.fitness", tid4)
    check(rec4["status"] == "done", f"晚间检查回执：{rec4['result'].get('user_view', '')[:50]}")
    master_collect("agentcrew.fitness", tid4)

    # 6) 双模式判定抽查
    check(B.detect_mode(B.p(ROOT, "02-agents", "demo.sample"))["mode"] == "managed", "收编区内=托管")
    check(B.detect_mode(ROOT)["mode"] == "lite", "总管仓库根自身=lite（无上级总管）")

    bad = [label for okk, label in results if not okk]
    try:
        pass  # 清场必须在 finally：失败路径也要拆沙箱
    finally:
        # 清场：本次模拟创建的信箱产物全部移除（真实信箱不被测试流量污染）；
        # 沙箱数据落在自己的子目录（06-tests/_sandbox/simulator），跑完整体拆除无残留
        removed = 0
        for p in CREATED:
            try:
                os.remove(p)
                removed += 1
            except OSError:
                pass
        shutil.rmtree(SANDBOX, ignore_errors=True)
        sandbox_clean = not os.path.exists(SANDBOX)
        results.append((sandbox_clean, "沙箱数据清场：跑完 _sandbox/simulator 无残留"))
        print(f"# 信箱清场：移除测试产物 {removed} 项；沙箱数据清场：{'完成' if sandbox_clean else '失败'}")
    bad = [label for okk, label in results if not okk]
    print(f"\n# 结果：{len(results) - len(bad)}/{len(results)} 通过")
    if bad:
        for b in bad:
            print(f"  失败：{b}")
        print("# 存在失败项")
        return 1
    print("# 全链路（路由→交办→执行→回执→收取→归档）全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
