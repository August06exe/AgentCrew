#!/usr/bin/env python3
"""make-instance.py — 从框架仓库 fork 出一个干净的「个人实例」：框架=制度，实例=生活。

排除：.git、.zcode、07-ops（夜航开发记录）、_standalone、各处 data 内容、profile/registry（实例自建）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentcrew_lib as B  # noqa: E402

EXCLUDE_TOP = {".git", ".zcode", "07-ops", "_standalone", "_local", "_save", "__pycache__"}


def registry_ready(instance_root: str) -> bool:
    """收编登记是否就绪（存档口径：registry 在 <实例>/_save/master/registry.json）。"""
    return os.path.isfile(B.registry_path(instance_root))


def ignore_for_instance(directory: str, names: list[str]) -> list[str]:
    """按【相对路径】判定排除：copytree 的 directory 是被列举内容的父目录，
    02-agents/<id>/data 的父目录 basename 是助理 id，不能用 basename 判断。"""
    ignored = []
    rel = os.path.relpath(directory, ROOT_SRC).replace(os.sep, "/")
    for n in names:
        rel_n = (rel + "/" + n) if rel != "." else n
        if n in EXCLUDE_TOP or n == "__pycache__":
            ignored.append(n)
        elif n == "data" and (rel_n.startswith("02-agents/") or rel_n.startswith("03-template")):
            ignored.append(n)  # 数据区由 ensure_agent_skeleton 重建为空
        elif n in ("inbox", "outbox") and rel_n.startswith("02-agents/"):
            ignored.append(n)  # 信箱整体由 ensure_agent_skeleton 重建为空
        elif n in ("data.js", "data.json") and "/dashboard" in f"/{rel_n}":
            ignored.append(n)  # 看板运行时数据 = 全量个人数据副本，绝不分发
        elif rel == "06-tests" or (rel == "03-template" and n == "tests"):
            ignored.append(n)  # 开发痕迹：测试脚本
        elif rel == "00-docs" and n == "RESEARCH.md":
            ignored.append(n)  # 开发痕迹：调研笔记
        elif rel.startswith("02-agents/") and n in ("tests", "docs"):
            ignored.append(n)  # 开发痕迹：助理自测脚本与验尸文档（含个人历史）
        elif rel == "." and n == "README.md":
            ignored.append(n)  # 开源门面不属于运行实例
        elif rel == "04-panel" and n in ("data.html", "install.html", "onboarding.html"):
            ignored.append(n)  # 已被应用壳(index.html)吸收的旧页面残壳
        elif rel == "05-scripts" and n == "make-instance.py":
            ignored.append(n)  # 纯开发工具：实例里不需要再 fork
        elif rel == "01-master" and n in (
                "profile.json", "registry.json", "authorizations.jsonl",
                "releases.jsonl", "inbox-master", "instance.json"):
            ignored.append(n)  # 实例态/个人数据绝不带入新实例
    return ignored


def main() -> int:
    ap = argparse.ArgumentParser(description="fork 个人实例")
    ap.add_argument("--to", required=True, help="实例目标目录")
    ap.add_argument("--name", required=True, help="实例名（如 demo-home）")
    args = ap.parse_args()
    B.utf8_console()

    root = B.find_repo_root()
    if not root:
        return B.fail("未找到AgentCrew 仓库根")
    dest = os.path.abspath(args.to)
    if os.path.exists(dest):
        return B.fail(f"目标已存在：{dest}")
    if os.path.abspath(root) == dest or dest.startswith(os.path.abspath(root) + os.sep):
        return B.fail("实例目录不能在框架仓库内部")

    ROOT_SRC = root
    globals()["ROOT_SRC"] = ROOT_SRC
    shutil.copytree(root, dest, ignore=ignore_for_instance)

    # 重建空骨架：数据区/信箱/实例档案（骨架必须落在新实例存档，不碰框架仓存档）
    for agent in sorted(os.listdir(B.p(dest, "02-agents"))):
        adir = B.p(dest, "02-agents", agent)
        if os.path.isdir(adir) and os.path.isfile(B.p(adir, "manifest.json")):
            B.ensure_agent_skeleton(adir, dest)
    B.atomic_write_json(B.p(dest, "01-master", "instance.json"),
                        {"name": args.name, "created_at": B.now_iso(), "forked_from": root})
    B.atomic_write_text(B.p(dest, "开始这里.md"), f"""# AgentCrew 个人实例（{args.name}）

> 这是你的**家**：把本文件夹指给 OpenClaw / Hermes / zcode 等运行时，
> AI 会话启动时自动读到 AGENTS.md 章程，即以总管身份上岗。

1. **接聊天**：按运行时的通道指引接微信/Telegram（Hermes 用户见 00-docs/DEPLOY-hermes.md）。
2. **初始化**：第一次对话 AI 会引导建档；或在浏览器打开 04-panel/启动管家面板.bat 填表单。
3. **日常**：在 IM 里说话即可。记录、提醒、报表全部本地，不出本机。

助理的增删：04-panel 安装页（从模板新建 / zip / 文件夹 / git）。
数据备份：整个本文件夹就是全部，拷走即备份。
""")

    # 新游戏：实例空存档（save.json v1 + master/agents 骨架）
    B.ensure_save(dest, agents=sorted(os.listdir(B.p(dest, "02-agents"))))

    # 实例章程覆盖：框架版 AGENTS.md 面向开发工作区，实例要用运营版（角色铁律+初始化话术）
    charter = B.p(root, "01-master", "instance-charter.md")
    if os.path.isfile(charter):
        shutil.copyfile(charter, B.p(dest, "AGENTS.md"))
        src_charter_copy = B.p(dest, "01-master", "instance-charter.md")
        if os.path.isfile(src_charter_copy):
            os.remove(src_charter_copy)  # 已转正为实例 AGENTS.md，源模板不留

    # 拷贝泄漏门（先于一切生成动作）：此刻 data 区必须全空，有内容即框架数据泄漏
    for agent in sorted(os.listdir(B.p(dest, "02-agents"))):
        data_dir = B.p(dest, "02-agents", agent, "data")
        if os.path.isdir(data_dir):
            dirty = [n for n in os.listdir(data_dir)
                     if n != ".gitkeep" and os.path.getsize(os.path.join(data_dir, n)) > 0]
            if dirty:
                shutil.rmtree(dest, ignore_errors=True)
                return B.fail(f"拷贝阶段检出框架数据泄漏（02-agents/{agent}/data: {dirty}），已撤销")

    # 开箱即用：用实例自己的 adopt.py 收编 02-agents 里的现成助理（登记进实例 registry）
    for agent in sorted(os.listdir(B.p(dest, "02-agents"))):
        adir = B.p(dest, "02-agents", agent)
        if os.path.isdir(adir) and os.path.isfile(B.p(adir, "manifest.json")):
            r_adopt = subprocess.run([sys.executable, B.p(dest, "05-scripts", "adopt.py"),
                                      "--path", adir],
                                     capture_output=True, text=True, encoding="utf-8", timeout=120)
            if not registry_ready(dest):
                print("警告：助理自动收编未完成，可手动跑实例内 adopt.py", file=sys.stderr)

    # 泄漏自检 1：新实例 01-master 下只允许模板/清单/personas
    allowed = {"profile.template.json", "registry.template.json", "onboarding-questions.md",
               "personas", "instance.json", ".gitkeep", "registry.json"}
    leaked = [n for n in os.listdir(B.p(dest, "01-master")) if n not in allowed]
    reg_path = B.registry_path(dest)
    if os.path.isfile(reg_path):
        reg = json.load(open(reg_path, encoding="utf-8"))
        agent_ids = {a for a in os.listdir(B.p(dest, "02-agents"))}
        bad_ids = [e.get("id") for e in reg.get("adopted", []) if e.get("id") not in agent_ids]
        if bad_ids:
            leaked.append(f"registry.json 引用了不存在的助理: {bad_ids}")
    # 泄漏自检 2：各助理 data\ 只允许 .gitkeep（主人的数据绝不随程序分发）
    for agent in sorted(os.listdir(B.p(dest, "02-agents"))):
        dash_dir = B.p(dest, "02-agents", agent, "dashboard")
        if os.path.isdir(dash_dir):
            leaked += [f"02-agents/{agent}/dashboard/{n}" for n in os.listdir(dash_dir)
                       if n in ("data.js", "data.json")]  # 看板数据含全量记录
        for sub in ("data", "inbox", "outbox"):
            sdir = B.p(dest, "02-agents", agent, sub)
            if os.path.isdir(sdir):
                leaked += [f"02-agents/{agent}/{sub}/{n}" for n in os.listdir(sdir)
                           if n != ".gitkeep" and not (sub == "inbox" and n == "done")]
        for box in ("inbox", "outbox"):
            box_dir = B.p(dest, "02-agents", agent, box)
            if os.path.isdir(box_dir):
                leaked += [f"02-agents/{agent}/{box}/{n}" for n in os.listdir(box_dir)
                           if n.endswith(".json")]
        dash_dir = B.p(dest, "02-agents", agent, "dashboard")
        if os.path.isdir(dash_dir):
            leaked += [f"02-agents/{agent}/dashboard/{n}" for n in os.listdir(dash_dir)
                       if n in ("data.js", "data.json")]  # 看板数据含全量记录
    if leaked:
        shutil.rmtree(dest, ignore_errors=True)
        return B.fail(f"fork 自检发现疑似个人数据，已撤销：{leaked}")

    git_init = None
    if shutil.which("git"):
        r = subprocess.run(["git", "init", "-b", "main"], cwd=dest, capture_output=True, text=True)
        git_init = "ok" if r.returncode == 0 else r.stderr.strip()[:200]

    return B.ok({
        "instance": dest,
        "name": args.name,
        "git_init": git_init,
        "next_steps": [
            "1. 双击实例目录下 04-panel/启动管家面板.bat（或在 IM 里直接跟总管说话）",
            "2. 完成初始化引导（聊天问答或面板表单，写 01-master/profile.json）",
            "3. 收编名册 registry.json 在实例存档 _save/master/ 下，由 adopt.py 收编时自动创建",
        ],
    })


if __name__ == "__main__":
    sys.exit(main())
