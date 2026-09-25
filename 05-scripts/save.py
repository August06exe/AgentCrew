#!/usr/bin/env python3
"""save.py — 存档管理（游戏机制的工具化）。

  status   存档概况（版本/顾问切片/体积/最后活动）
  watch    确定性监控：扫描程序区违规增量数据（不靠 LLM 自觉）
           退出码 0=干净，1=有违规（可挂计划任务/CI/面板启动钩子）
  reset    新游戏：二次确认后删除整个存档，项目回到原点
  export   打包整个存档为单文件 crew-save-<时间戳>.asave（标准 zip，含全部个人数据）
  import   从 .asave 单文件包恢复存档——四道闸+自动备份+原子换位（删除类操作，--dry-run 可预演）
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentcrew_lib as B  # noqa: E402


def human(n: int) -> str:
    return f"{n/1024:.1f}KB" if n < 1024 * 1024 else f"{n/1024/1024:.2f}MB"


def dir_size(d: str) -> int:
    total = 0
    for base, _, files in os.walk(d):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def cmd_status(_a) -> int:
    root = B.find_repo_root()
    if not root:
        return B.fail("未找到 AgentCrew 仓库根")
    sr = B.save_root(root)
    mani = B.load_save_manifest(root)
    if mani is None:
        return B.ok({"exists": False, "note": "无存档（新游戏状态）——首次使用时自动建档"})
    agents = {}
    sdir = B.p(sr, "agents")
    if os.path.isdir(sdir):
        for aid in sorted(os.listdir(sdir)):
            adir = B.p(sdir, aid)
            if os.path.isdir(adir):
                agents[aid] = human(dir_size(adir))
    prof = B.master_save_files(root)["profile"]
    owner = ""
    if os.path.isfile(prof):
        try:
            owner = (B.read_json(prof).get("owner") or {}).get("name", "")
        except Exception:  # noqa: BLE001
            pass
    return B.ok({
        "exists": True,
        "save_root": sr,
        "save_version": mani.get("save_version"),
        "program_supports": B.SAVE_VERSION_CURRENT,
        "owner": owner,
        "created_at": mani.get("created_at"),
        "master_size": human(dir_size(B.p(sr, "master"))),
        "agents": agents,
    })


def cmd_watch(_a) -> int:
    root = B.find_repo_root()
    if not root:
        return B.fail("未找到 AgentCrew 仓库根")
    stray = B.scan_stray_increment(root)
    if stray:
        return B.fail(f"程序区发现 {len(stray)} 处违规增量数据（增量信息必须进 _save）",
                      {"violations": stray[:50],
                       "fix": "python 05-scripts/migrate_save.py 一键归档"},
                      code=1)
    return B.ok({"clean": True, "note": "程序区无增量数据泄漏，存档架构完好"})


def cmd_reset(a) -> int:
    root = B.find_repo_root()
    if not root:
        return B.fail("未找到 AgentCrew 仓库根")
    sr = B.save_root(root)
    if not os.path.isdir(sr):
        return B.ok({"note": "本来就没有存档（已是原点）"})
    if not a.yes:
        # 危险操作：默认要求交互确认；脚本调用须显式 --yes
        try:
            ans = input(f"将删除整个存档 {sr}（主人档案/全部顾问数据，不可恢复）。\n输入 YES 确认：")
        except EOFError:
            ans = ""
        if ans.strip() != "YES":
            return B.fail("未确认，已取消")
    import shutil
    shutil.rmtree(sr, ignore_errors=True)
    B.ensure_save(root)
    return B.ok({"reset": True, "note": "存档已清除并重建空档——项目回到原点（新游戏）"})


def cmd_export(a) -> int:
    B.utf8_console()
    root = B.find_repo_root()
    if not root:
        return B.fail("未找到 AgentCrew 仓库根")
    if a.dry_run:  # 零写入：不取锁、不落盘
        try:
            plan = B.export_preview(root, out=a.out)
        except B.SaveGateError as e:
            return B.fail(str(e), {"reason_code": e.code, **e.payload}, code=0)
        return B.ok({"dry_run": True, **plan})
    try:
        with B.save_lock(root):
            r = B.pack_save(root, out=a.out)
    except B.SaveGateError as e:  # 业务拒绝：退出码 0 + reason_code（PARADIGM §6）
        return B.fail(str(e), {"reason_code": e.code, **e.payload}, code=0)
    except OSError as e:  # 技术性失败：非零退出
        return B.fail(f"导出失败（IO/权限）：{e}", code=1)
    return B.ok({"file": r["file"], "bytes": r["bytes"], "entries": r["entries"],
                 "rows_total": r["rows_total"], "save_version": r["save_version"],
                 "agents": r["agents"],
                 "warning": "此文件含全部个人数据，谨防外泄"})


def cmd_import(a) -> int:
    B.utf8_console()
    root = B.find_repo_root()
    if not root:
        return B.fail("未找到 AgentCrew 仓库根")
    pkg = os.path.abspath(a.file)
    if not os.path.isfile(pkg):
        return B.fail("存档包不存在", {"reason_code": "file_not_found", "file": pkg}, code=0)
    if a.dry_run:  # 只跑闸①②③纯读包零写入（不取锁）
        try:
            prev = B.inspect_package(pkg, root, force=a.force)
        except B.SaveGateError as e:
            return B.fail(str(e), {"reason_code": e.code, **e.payload}, code=0)
        return B.ok({"dry_run": True, "file": pkg, "entries": prev["entries"],
                     "save_version": prev["save_version"],
                     "plan": {"backup_would_be": prev["auto_backup_name"],
                              "migrate_would": prev["will_migrate"],
                              "replace": True},
                     "warnings": prev["warnings"]})
    try:
        with B.save_lock(root):
            try:
                r = B.import_save(root, pkg, force=a.force)
            except B.SaveGateError as e:
                return B.fail(str(e), {"reason_code": e.code, **e.payload}, code=0)
            migrated: list[int] = []
            if r["save_version"] < B.SAVE_VERSION_CURRENT:
                # 旧版本档：换位成功后在锁内跑阶梯迁移（平级脚本单向依赖本库，
                # 函数内延迟 import 防循环导入——agentcrew_lib 禁顶层 import migrate_save）
                try:
                    import migrate_save
                    migrated = migrate_save.migrate_chain(root)
                except Exception as e:  # noqa: BLE001  迁移炸=程序 bug；auto-backup 兜底
                    return B.fail(f"存档已导入但阶梯迁移失败（可用自动备份回退）：{e}",
                                  {"reason_code": "migrate_failed", "backup": r["backup"]}, code=1)
    except B.SaveGateError as e:
        return B.fail(str(e), {"reason_code": e.code, **e.payload}, code=0)
    except OSError as e:
        return B.fail(f"导入失败（IO/权限）：{e}", code=1)
    mani = B.load_save_manifest(root)
    final_v = (mani.get("save_version")
               if isinstance(mani, dict) and not mani.get("_corrupt") else r["save_version"])
    payload = {"file": pkg, "imported_entries": r["imported_entries"], "backup": r["backup"],
               "migrated": migrated, "save_version": final_v,
               "note": (f"旧档已自动备份：{r['backup']}" if r["backup"]
                        else "目标为空档（首装/跨机迁移），未产生自动备份")}
    if r.get("forced_downgrade"):
        payload["warning"] = (f"已强制安装较新存档（包 v{r['package_v']} > 程序支持 v{r['program_v']}）："
                              "当前程序可能无法读取新版结构，风险自担")
    return B.ok(payload)


def main() -> int:
    ap = argparse.ArgumentParser(description="AgentCrew 存档管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("watch")
    p_r = sub.add_parser("reset")
    p_r.add_argument("--yes", action="store_true", help="跳过交互确认（脚本用）")
    p_e = sub.add_parser("export", help="打包整个存档为单文件 .asave（含全部个人数据）")
    p_e.add_argument("--out", default=None,
                     help="落点：目录（目录内默认名）或完整文件路径；缺省=存档根上级目录")
    p_e.add_argument("--dry-run", action="store_true", help="只报告将打包的内容与大小，不落盘")
    p_i = sub.add_parser("import", help="从 .asave 单文件包恢复存档（整档替换，目标非空自动先备份）")
    p_i.add_argument("file", help="存档包路径（不校验扩展名，只认包内 manifest）")
    p_i.add_argument("--force", action="store_true",
                     help="允许安装比程序更新的存档（降级风险自担）")
    p_i.add_argument("--dry-run", action="store_true", help="只检查包与预演计划（闸①②③），零写入")
    a = ap.parse_args()
    return {"status": cmd_status, "watch": cmd_watch, "reset": cmd_reset,
            "export": cmd_export, "import": cmd_import}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
