#!/usr/bin/env python3
"""adopt.py — 收编子助理：zip / 本地文件夹 / git 地址 三式安装 + 校验 + 登记。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentcrew_lib as B  # noqa: E402


def resolve_source(args) -> tuple[str | None, str | None, bool]:
    """返回 (manifest 所在目录, 错误信息, 是否临时目录)。"""
    tmp = None
    src = None
    if args.zip:
        if not os.path.isfile(args.zip):
            return None, f"zip 不存在：{args.zip}", False
        tmp = tempfile.mkdtemp(prefix="butler-adopt-")
        B.extract_zip(args.zip, tmp)
        src = B.find_manifest_dir(tmp)
    elif args.git:
        tmp = tempfile.mkdtemp(prefix="butler-adopt-")
        good, msg = B.clone_git(args.git, os.path.join(tmp, "repo"))
        if not good:
            return None, f"git clone 失败：{msg}", True
        src = B.find_manifest_dir(os.path.join(tmp, "repo"))
    else:
        src = os.path.abspath(args.path or ".")
        src = B.find_manifest_dir(src)
    if not src:
        return None, "找不到 manifest.json（支持根目录或下两层）", tmp is not None
    return src, None, tmp is not None


def main() -> int:
    ap = argparse.ArgumentParser(description="收编子助理")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--path", help="本地助理文件夹")
    g.add_argument("--zip", help="助理 zip 包")
    g.add_argument("--git", help="git 仓库地址")
    ap.add_argument("--force", action="store_true", help="开发模式：校验警告降级为 unverified 登记")
    ap.add_argument("--paused", action="store_true", help="收编后保持 paused")
    args = ap.parse_args()
    B.utf8_console()

    root = B.find_repo_root()
    if not root:
        return B.fail("未找到AgentCrew 仓库根")
    src, err, is_tmp = resolve_source(args)
    try:
        if err:
            return B.fail(err)

        m = B.load_manifest(src)
        problems, warnings = B.validate_manifest(m, src)
        aid = m.get("id", "")

        # 校验清单（PROTOCOL §5.1 全 8 项）。
        # 注意：此处不建任何存档骨架——zip/git 式 src 是临时目录，先建骨架会把
        # 临时目录名写成实例存档切片（失败后残留 butler-adopt-xxxx 垃圾切片）。
        # 骨架创建统一推迟到全部校验通过、最终 id 确定之后（见下 ensure_agent_skeleton）。
        checklist = {}
        checklist["s1_manifest"] = not problems
        charter_text = ""
        if os.path.isfile(B.p(src, "AGENTS.md")):
            charter_text = open(B.p(src, "AGENTS.md"), encoding="utf-8").read()
        checklist["s2_charter_exists"] = bool(charter_text)
        checklist["s3_charter_dualmode"] = ("向上" in charter_text and "PROTOCOL" in charter_text)
        checklist["s4_persona"] = os.path.isfile(B.p(src, "persona.md"))
        checklist["s5_license"] = os.path.isfile(B.p(src, "LICENSE")) or bool(m.get("license"))
        # s6 表文件：跑 data.py init 补建空表后逐表确认存在
        init_out = None
        data_py = B.p(src, "tools", "data.py")
        if os.path.isfile(data_py):
            r_init = subprocess.run([sys.executable, data_py, "init"], capture_output=True,
                                    text=True, encoding="utf-8", cwd=src, timeout=60)
            try:
                init_out = json.loads(r_init.stdout)
            except json.JSONDecodeError:
                init_out = {"ok": False, "error": (r_init.stderr or r_init.stdout)[:200]}
        checklist["s6_tables_init"] = bool(init_out and init_out.get("ok"))
        declared_tables = [t.get("name") for t in m.get("tables") or []]
        # s6 表文件落点（PARADIGM §5 存档架构）：tables 一律经 tools/data.py 的存档解析器
        # 落在存档切片（托管=实例 _save/agents/<id>/data；lite=<src>/_save/data）；
        # 程序区 data\ 出现表文件反而是违规增量（save.py watch 会拦）。故本检查的落点
        # 与 data.py 的解析规则逐条对齐：沙箱env > 托管 > lite（不认 AGENTCREW_SAVE，
        # 因为助理侧工具不读它，对齐对象是 init 实际写入处）。
        env_data_dir = os.environ.get("ASSISTANT_DATA_DIR")
        if env_data_dir:
            tables_dir = os.path.abspath(env_data_dir)
        else:
            mode = B.detect_mode(src)
            if mode["mode"] == "managed":
                tables_dir = B.p(mode["master_root"], B.SAVE_DIRNAME, "agents",
                                 os.path.basename(os.path.abspath(src)), "data")
            else:
                tables_dir = B.p(B.agent_save_dir_lite(src), "data")
        checklist["s6_tables_exist"] = all(
            os.path.isfile(B.p(tables_dir, f"{t}.jsonl")) for t in declared_tables)
        # first_run：收编时执行一次的初始化命令（如导入种子库；幂等命令为宜）
        first_run_results = {}
        for cmd in m.get("first_run") or []:
            parts = str(cmd).split()
            if not parts or not os.path.isfile(B.p(src, parts[0])):
                first_run_results[cmd] = "工具不存在"
                continue
            r_fr = subprocess.run([sys.executable, B.p(src, *parts[0].split("/")), *parts[1:]],
                                  capture_output=True, text=True, encoding="utf-8",
                                  cwd=src, timeout=120)
            try:
                first_run_results[cmd] = json.loads(r_fr.stdout)
            except json.JSONDecodeError:
                first_run_results[cmd] = {"ok": False, "error": (r_fr.stderr or r_fr.stdout)[:150]}
        checklist["s6b_first_run"] = all(
            isinstance(v, dict) and v.get("ok", True) for v in first_run_results.values())
        tools = m.get("tools") or ["tools/data.py"]
        tool_results = {t: B.selfcheck_tool(src, t) for t in tools}
        checklist["s7_tools"] = all(g for g, _ in tool_results.values())
        dash = m.get("dashboard") or {}
        checklist["s8_dashboard"] = (not dash.get("main")) or os.path.isfile(B.p(src, dash["main"]))
        hard_ok = all(checklist.values()) and not problems

        if not hard_ok and not args.force:
            detail = {"manifest_problems": problems, "manifest_warnings": warnings,
                      "checklist": checklist,
                      "first_run_results": first_run_results,
                      "tools": {t: msg for t, (g, msg) in tool_results.items() if not g}}
            return B.fail("收编校验未通过（--force 可强制登记为 unverified）", detail, code=2)

        dest = B.p(root, "02-agents", aid)
        in_place = os.path.abspath(src) == os.path.abspath(dest)
        if os.path.exists(dest) and not in_place:
            return B.fail(f"id 冲突：02-agents/{aid} 已存在（升级请直接替换程序区，换 id 请改 manifest）")

        # 存档并入：lite 期间攒的 <助理>/_save → 实例 _save/agents/<id>（数据随顾问入职）。
        # 切片名用真实 agent id；从建切片到登记全程可回滚——任何失败路径不留半成品切片。
        lite_save = B.agent_save_dir_lite(src)
        inst_save = B.agent_save_dir(root, aid)
        slice_created = not os.path.isdir(inst_save)
        dest_created = not in_place
        merged_save = False
        try:
            if os.path.isdir(lite_save) and any(os.scandir(lite_save)):
                if os.path.exists(inst_save) and any(os.scandir(inst_save)):
                    return B.fail(f"存档冲突：实例已有 {aid} 的存档切片，且新顾问自带 _save——请先处理一处")
                os.makedirs(os.path.dirname(inst_save), exist_ok=True)
                shutil.move(lite_save, inst_save)
                merged_save = True

            # 拷贝程序区 + 信箱/数据骨架（已在收编区内的就地登记）
            if not in_place:
                shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", ".zcode"))
            B.ensure_agent_skeleton(dest, root)

            # 建档：实例存档登记该顾问切片
            B.ensure_save(root, [aid])

            # 登记（按 id 幂等：重复收编=更新原条目，绝不产生双条目）
            reg = B.load_registry(root)
            entry = {
                "id": aid,
                "dir": f"02-agents/{aid}",
                "version": m.get("version"),
                "status": "paused" if args.paused else "active",
                "adopted_at": B.now_iso(),
                "reminders_enabled": True,
                "unverified": None if hard_ok else True,
            }
            others = [e for e in reg.get("adopted", []) if e.get("id") != aid]
            B.save_registry(root, {**reg, "adopted": others + [entry]})
        except Exception as e:  # noqa: BLE001  落库失败：回滚本次新建的切片，绝不丢随身档
            if slice_created and os.path.isdir(inst_save):
                if merged_save:
                    shutil.move(inst_save, lite_save)  # 随身 _save 原样退回助理
                else:
                    shutil.rmtree(inst_save, ignore_errors=True)  # 仅骨架，直接拆除
            if dest_created and os.path.isdir(dest):
                shutil.rmtree(dest, ignore_errors=True)
            return B.fail(f"收编落库失败（已回滚新建存档切片）：{e}", code=1)

        mode = B.detect_mode(dest)
        standalone_twin = os.path.isdir(B.p(root, "_standalone", aid))
        return B.ok({
            "adopted": aid,
            "save_merged": merged_save,
            "channel_warning": ("_standalone 下存在同 id 副本：同一助理不可两处同时接客，请确认旧副本已停用"
                                if standalone_twin else None),
            "dir": os.path.relpath(dest, root),
            "mode_after_adopt": mode["mode"],
            "unverified": None if hard_ok else True,
            "manifest_problems": problems if problems else "none",
            "next": "总管应向主人介绍新助理；助理级 onboarding 问题见 manifest.onboarding",
        })
    finally:
        if is_tmp and src and os.path.isdir(os.path.dirname(src)):
            shutil.rmtree(os.path.dirname(src), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
