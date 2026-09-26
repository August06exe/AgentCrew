#!/usr/bin/env python3
"""test_savepack.py — 存档单文件导出/导入（v0.3）回归套件。

覆盖：T1 导出→reset→导入 roundtrip（含同秒撞名保护）；T2 四道闸拒绝路径；
T2-6 版本合法性+导出 corrupt_save 闭环；T3 自动备份；T4 旧版本档迁移接续（打桩）；
T5 面板端点冒烟（函数级）；T9 面板 Handler 回归（真起本地服务：删行进 trash/图片根界/
跳转 token/zip 文件名/confirm 过期/临时目录清扫）；T7 首装导入（目标无档）；
T8 压缩比除零守卫；T-dry dry-run 零写入。

红线（T0）：每个用例在任何调用前先把 AGENTCREW_SAVE 指向
06-tests/_sandbox/savepack-<pid>/<case>/save——cmd_import/api_save_* 经 find_repo_root()
解析真实仓库根，漏设 env 会让 save_root 落回真实 _save/（对删除类操作是红线级前提）。
沙箱基路径带进程号：多会话并发跑本套件各用各的沙箱，互不踩踏（共享固定路径曾是
T1/T2/T3/T5/T7 漂移失败根因——并发方 rm_tree 首清场删走对方正持有的包，isfile→open
交错还能让 load_save_manifest 抛 FileNotFoundError）。启动只收 6h 前的崩溃残留，
绝不触碰活动会话的沙箱。临时目录（tempfile.tempdir）一并重定向进沙箱：%TEMP% 全机
共享，并发实例的暂存/解包互不可见。套件首尾清场，全程不触真实 _save——
红线绊线：套件窗口内任何 save_root 解析越出沙箱即当场失败。

用法：python 06-tests/test_savepack.py
"""
from __future__ import annotations

import base64
import contextlib
import glob
import hashlib
import http.client
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from argparse import Namespace
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "05-scripts"))
import agentcrew_lib as B  # noqa: E402

PY = sys.executable
SAVEPY = os.path.join(ROOT, "05-scripts", "save.py")
PANEL = os.path.join(ROOT, "04-panel", "server.py")
# 沙箱基路径进程唯一（<pid> 后缀）：并发会话/多验收员同时跑套件互不踩踏。
BASE = os.path.join(HERE, "_sandbox", f"savepack-{os.getpid()}")
BASE_ABS = os.path.abspath(BASE)  # 红线绊线的判定锚
BASE_REL = os.path.relpath(BASE, ROOT).replace(os.sep, "/")  # 仓内相对路径（HTTP URL 断言用）

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> bool:
    results.append((bool(cond), label))
    print(f" {'✓' if cond else '✗'} {label}")
    return bool(cond)


def wait_gone(path: str, timeout: float = 1.0) -> bool:
    """对「尽力而为」删除的容错断言：文件消失（含本来就不在）即真，1s 内消失即过。
    背景：面板 _remove_best_effort 只短重试 3×50ms 后放弃（server.py 的设计语义），
    杀软/索引器短暂占位可拖过该窗口——立即断言 not exists 不是不变量（B9 复核残余），
    改短轮询吸收环境抖动；超时仍在仍判失败，保住「confirm 会删暂存」的回归力。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not os.path.exists(path):
            return True
        time.sleep(0.02)
    return not os.path.exists(path)


# ---------- T0 沙箱红线设施 ----------

_ORIG_ENV = os.environ.get("AGENTCREW_SAVE")


def sandbox(case: str) -> str:
    """该用例的存档根路径（通常尚不存在；T7 首装场景即依赖「不存在」）。"""
    return os.path.join(BASE, case, "save")


def use(case: str) -> str:
    """in-process 红线动作：设 AGENTCREW_SAVE 指向沙箱，返回存档根。"""
    v = sandbox(case)
    os.environ["AGENTCREW_SAVE"] = v
    return v


def run_save(args: list[str], save_env: str) -> subprocess.CompletedProcess:
    """子进程跑 save.py：env 显式传沙箱，绝不让子进程落回真实 _save。"""
    env = {**os.environ, "AGENTCREW_SAVE": save_env, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([PY, SAVEPY, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120,
                          env=env, cwd=ROOT)


def jout(r: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"非 JSON 输出：{(r.stdout or r.stderr)[:200]}"}


def run_save_import(args: list[str], save_env: str) -> tuple[subprocess.CompletedProcess, dict]:
    """import 类调用（会走到 agentcrew_lib 的原子换位）专用：换位是一次性
    os.rename，本机杀软/索引器偶发短暂占位 → swap_failed[WinError 5]（库侧已
    正确回滚旧档）。回滚语义保证重试安全，故仅对 swap_failed 短重试（至多 2 次，
    共 3 试——三实例并发压测时杀软可连环占位）吸收环境抖动；其余 reason_code
    是确定性拒绝路径（bad_zip/unsafe_entry/…），重试反而会掩盖回归，原样返回。"""
    r = run_save(args, save_env)
    o = jout(r)
    for _ in range(2):
        if not (r.returncode == 0 and o.get("ok") is False
                and o.get("reason_code") == "swap_failed"):
            break
        time.sleep(0.25)
        r = run_save(args, save_env)
        o = jout(r)
    return r, o


def rm_tree(path: str) -> None:
    """清场删除：ignore_errors 会把杀软/索引器占位导致的半途失败静默成残留
    （下轮 seed 在残留上追加 → 行数翻倍、备份断言取中旧包），改显式短重试；
    重试尽仍失败则抛错——带着脏沙箱跑出假红/假绿，不如让套件明确失败。"""
    for i in range(3):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if i == 2:
                raise
            time.sleep(0.3)


# ---------- T0 红线绊线：套件窗口内存档解析必须全程落在沙箱内 ----------
# save_root() 是全库唯一存档根解析点（save_manifest_path/pack_save/scan_save_stats/
# ensure_save 全部经它；save.py 与面板子进程经环境变量走同一点）。套件活动窗口内
# 给它套上绊线：任何解析落到 BASE 之外（= 漏设 env 落回真实 _save，或指去别处），
# 当场抛错阻止后续读写——「全程不触真实 _save」从注释纪律升级为硬断言。
# 覆盖不到的仅剩子进程路径，而 run_save 显式传 env、api_save_export 继承进程 env，
# 两条子进程路都已在各自用例里被断言落沙箱（T1/T5 的落点检查）。

_REAL_SAVE_ROOT = B.save_root
_TRIPWIRE = False
_ORIG_TEMPDIR = tempfile.tempdir  # None=系统默认；套件把它重定向进沙箱（见 main）


def _guarded_save_root(repo_root: str) -> str:
    r = _REAL_SAVE_ROOT(repo_root)
    if _TRIPWIRE:
        ra = os.path.abspath(r)
        if ra != BASE_ABS and not ra.startswith(BASE_ABS + os.sep):
            raise RuntimeError(
                f"T0 红线绊线：save_root 解析越出沙箱 → {ra}（真实 _save 险些被读写）；"
                f"套件窗口内一切存档访问必须落在 {BASE_ABS}")
    return r


def seed_archive() -> str:
    """播种像样的存档：master 档案/名册/授权 + 2 助理切片（中文行/空表/嵌套目录）。"""
    sr = B.save_root(ROOT)
    aids = ("agentcrew.fitness", "agentcrew.demo")
    B.ensure_save(ROOT, agents=list(aids))
    B.atomic_write_json(B.p(sr, "master", "profile.json"),
                        {"owner": {"name": "测试主人"},
                         "notifications": {"allowed": "08:00-22:00"}})
    B.atomic_write_json(B.p(sr, "master", "registry.json"),
                        {"$schema": "agentcrew.registry/v1", "master_instance": "测试实例",
                         "adopted": [{"id": a, "dir": f"02-agents/{a}"} for a in aids]})
    B.append_jsonl(B.p(sr, "master", "authorizations.jsonl"),
                   {"grant": "睡眠数据快照", "from": "健康助理", "to": "职业顾问",
                    "at": B.now_iso()})
    fa, fd_ = aids
    for aid in aids:
        B.ensure_agent_skeleton(B.p(sr, "agents", aid), ROOT)
    for i in range(3):
        B.append_jsonl(B.p(sr, "agents", fa, "data", "weight.jsonl"),
                       {"_id": B.row_id(), "created_at": B.now_iso(),
                        "weight_kg": 70.0 + i, "note": f"第{i}次称重·中文行"})
    B.append_jsonl(B.p(sr, "agents", fd_, "data", "notes.jsonl"),
                   {"_id": B.row_id(), "created_at": B.now_iso(), "title": "备忘一条"})
    open(B.p(sr, "agents", fd_, "data", "empty.jsonl"), "a", encoding="utf-8").close()  # 空表
    os.makedirs(B.p(sr, "agents", fa, "data", "reports", "2026-09"), exist_ok=True)  # 嵌套目录
    B.atomic_write_text(B.p(sr, "agents", fa, "data", "reports", "2026-09", "月报.json"),
                        json.dumps({"month": "2026-09", "memo": "嵌套·中文文件名"},
                                   ensure_ascii=False))
    return sr


def seeded(case: str) -> str:
    use(case)
    return seed_archive()


def snapshot(sr: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for base, _dirs, files in os.walk(sr):
        for fn in files:
            fp = os.path.join(base, fn)
            rel = os.path.relpath(fp, sr).replace(os.sep, "/")
            with open(fp, "rb") as f:
                out[rel] = hashlib.sha256(f.read()).hexdigest()
    return out


def craft_zip(path: str, files: dict[str, bytes], *, save_version: int = 1,
              entries: int | None = None, bytes_: int | None = None) -> str:
    """手工构造存档包（闸测试用）：manifest 缺省按 files 实况复算。"""
    body = {k: v for k, v in files.items() if k != B.MANIFEST_NAME}
    mani = {"kind": "agentcrew.save.export", "manifest_version": 1,
            "exported_at": B.now_iso(), "tool": "test_savepack",
            "save_version": save_version, "program_save_version": B.SAVE_VERSION_CURRENT,
            "origin_external": True,
            "entries": len(body) if entries is None else entries,
            "bytes": sum(len(v) for v in body.values()) if bytes_ is None else bytes_,
            "rows_total": 0, "agents": {}}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
        if B.MANIFEST_NAME not in files:
            z.writestr(B.MANIFEST_NAME, json.dumps(mani, ensure_ascii=False, indent=2) + "\n")
    return path


# ---------- T1 roundtrip ----------

def t1_roundtrip():
    print("\n[T1] 导出→reset→导入 roundtrip（含撞名保护）")
    sv = seeded("t1")
    r = run_save(["export"], sv)
    o = jout(r)
    check(r.returncode == 0 and o.get("ok") and os.path.isfile(o.get("file", "")),
          f"T1 CLI 导出成功：{os.path.basename(o.get('file', '?'))}")
    check(os.path.dirname(o.get("file", "")) == os.path.dirname(sv),
          "T1 缺省落点=存档根上级目录")
    check("个人数据" in (o.get("warning") or ""), "T1 stdout 附「含全部个人数据」提醒")
    pkg = o["file"]
    before = B.scan_save_stats(ROOT)
    before_map = snapshot(sv)
    before_v = B.load_save_manifest(ROOT).get("save_version")
    r = run_save(["reset", "--yes"], sv)
    check(r.returncode == 0 and jout(r).get("ok"), "T1 reset --yes 清档成功")
    r, o = run_save_import(["import", pkg], sv)
    check(r.returncode == 0 and o.get("ok"), f"T1 导入成功（exit={r.returncode}）")
    check(bool(o.get("backup")) and os.path.isfile(o.get("backup", "")),
          f"T1 汇报自动备份：{os.path.basename(o.get('backup') or '?')}")
    after = B.scan_save_stats(ROOT)
    check(before["rows_total"] == after["rows_total"] and before["rows_total"] > 0,
          f"T1 行数守恒：{before['rows_total']} 行")
    check(before["agents"] == after["agents"], "T1 各助理各表行数逐一相等")
    check(snapshot(sv) == before_map, f"T1 全树 sha256 逐字节一致（{len(before_map)} 文件）")
    check(B.load_save_manifest(ROOT).get("save_version") == before_v, "T1 save_version 相等")
    # 同秒撞名保护：--out 指向已存在文件 → 追加 -N 序号，绝不静默覆盖
    dummy = os.path.join(BASE, "t1", "preexisting.asave")
    with open(dummy, "wb") as f:
        f.write(b"DO-NOT-TOUCH")
    r = run_save(["export", "--out", dummy], sv)
    o = jout(r)
    expect = os.path.splitext(dummy)[0] + "-2.asave"
    check(o.get("ok") and o.get("file") == expect and os.path.isfile(expect),
          f"T1 撞名 → 序号保护：{os.path.basename(o.get('file', '?'))}")
    with open(dummy, "rb") as f:
        check(f.read() == b"DO-NOT-TOUCH", "T1 原文件未被覆盖")


# ---------- T2 四道闸拒绝路径 ----------

def t2_gates():
    print("\n[T2] 四道闸拒绝路径")
    # T2-1 坏 zip
    sv = seeded("t2-1")
    good = B.pack_save(ROOT, out=os.path.join(BASE, "t2-1", "good.asave"))["file"]
    with open(good, "rb") as f:
        raw = f.read()
    trunc = os.path.join(BASE, "t2-1", "truncated.asave")
    with open(trunc, "wb") as f:
        f.write(raw[512:])
    before = snapshot(sv)
    r = run_save(["import", trunc], sv)
    o = jout(r)
    check(r.returncode == 0 and o.get("ok") is False and o.get("reason_code") == "bad_zip",
          f"T2-1 截断包 → bad_zip（exit={r.returncode}）")
    check(snapshot(sv) == before, "T2-1 拒绝后沙箱档逐字节未动")

    # T2-2 无 manifest（直接 zipfile 构造，确保包里真没有 export-manifest.json）
    sv = seeded("t2-2")
    with open(B.save_manifest_path(ROOT), "rb") as f:
        sj = f.read()
    pkg = os.path.join(BASE, "t2-2", "only-savejson.asave")
    with zipfile.ZipFile(pkg, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("save.json", sj)
    before = snapshot(sv)
    o = jout(run_save(["import", pkg], sv))
    check(o.get("reason_code") == "no_manifest", "T2-2 缺 manifest → no_manifest")
    check(snapshot(sv) == before, "T2-2 沙箱档未动")

    # T2-3 zip-slip 三形态（反斜杠条目须经 ZipInfo 构造后改 .filename 才能以原样字节入包）
    sv = seeded("t2-3")
    parent = os.path.dirname(sv)
    with open(B.save_manifest_path(ROOT), "rb") as f:
        sj = f.read()
    for i, evil in enumerate(("../evil.txt", "C:/evil.txt", "a\\evil.txt")):
        pth = os.path.join(BASE, "t2-3", f"slip{i}.asave")
        mani = {"kind": "agentcrew.save.export", "manifest_version": 1,
                "exported_at": B.now_iso(), "tool": "test_savepack",
                "save_version": 1, "program_save_version": B.SAVE_VERSION_CURRENT,
                "origin_external": True, "entries": 2,
                "bytes": len(sj) + 5, "rows_total": 0, "agents": {}}
        with zipfile.ZipFile(pth, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("save.json", sj)
            zi = zipfile.ZipInfo("placeholder")
            zi.filename = evil
            zi.file_size = 5
            z.writestr(zi, b"pwned")
            z.writestr(B.MANIFEST_NAME, json.dumps(mani, ensure_ascii=False, indent=2) + "\n")
        before = snapshot(sv)
        o = jout(run_save(["import", pth], sv))
        check(o.get("reason_code") == "unsafe_entry", f"T2-3 zip-slip {evil!r} → unsafe_entry")
        check(snapshot(sv) == before and not os.path.exists(os.path.join(parent, "evil.txt")),
              f"T2-3 {evil!r} 无逃逸落地")

    # T2-4 降级版本闸 + --force 放行
    sv = seeded("t2-4")
    mp = B.save_manifest_path(ROOT)
    B.atomic_write_json(mp, {**B.read_json(mp), "save_version": B.SAVE_VERSION_CURRENT + 1})
    pkg = B.pack_save(ROOT, out=os.path.join(BASE, "t2-4", "vnext.asave"))["file"]
    B.atomic_write_json(mp, {**B.read_json(mp), "save_version": B.SAVE_VERSION_CURRENT})
    before = snapshot(sv)
    r = run_save(["import", pkg], sv)
    o = jout(r)
    check(o.get("reason_code") == "save_newer_than_program"
          and o.get("package_v") == B.SAVE_VERSION_CURRENT + 1
          and o.get("program_v") == B.SAVE_VERSION_CURRENT,
          "T2-4a 新版包无 --force → save_newer_than_program（带 package_v/program_v）")
    check(snapshot(sv) == before, "T2-4a 沙箱档未动")
    r, o = run_save_import(["import", pkg, "--force"], sv)
    why = o.get("reason_code") or o.get("error") or ("ok" if o.get("ok") else "?")
    check(r.returncode == 0 and o.get("ok") and "⚠" in (r.stderr or ""),
          f"T2-4b --force 放行且 stderr 出 ⚠ 大字横幅（exit={r.returncode} reason={why}）")
    check("warning" in o, f"T2-4b stdout JSON 附 warning 字段（reason={why}）")
    cur_v = (B.load_save_manifest(ROOT) or {}).get("save_version")
    check(cur_v == B.SAVE_VERSION_CURRENT + 1,
          f"T2-4b 沙箱档已被替换为新版（实际 save_version={cur_v}）")

    # T2-5 manifest.entries 改错 → 篡改检测
    sv = seeded("t2-5")
    good = B.pack_save(ROOT, out=os.path.join(BASE, "t2-5", "good.asave"))["file"]
    with zipfile.ZipFile(good) as z:
        items = {n: z.read(n) for n in z.namelist()}
    mani = json.loads(items[B.MANIFEST_NAME].decode("utf-8"))
    mani["entries"] = int(mani["entries"]) + 999
    items[B.MANIFEST_NAME] = json.dumps(mani, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    bad = os.path.join(BASE, "t2-5", "tampered.asave")
    with zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as z:
        for n, data in items.items():
            z.writestr(n, data)
    before = snapshot(sv)
    o = jout(run_save(["import", bad], sv))
    check(o.get("reason_code") == "manifest_mismatch", "T2-5 manifest.entries 改错 → manifest_mismatch")
    check(snapshot(sv) == before, "T2-5 沙箱档未动")


def t26_version_legality():
    print("\n[T2-6] 版本合法性（换位前拒绝）+ 导出 corrupt_save 闭环")
    for tag, sj_bytes in [
        ("v0", json.dumps({"save_version": 0}, ensure_ascii=False).encode("utf-8")),
        ("nokey", b'{"created_at": "x"}'),
        ("str", json.dumps({"save_version": "abc"}).encode("utf-8")),
        ("corrupt", b'{"save_version": 1,'),
    ]:
        sv = seeded(f"t2-6-{tag}")
        pkg = craft_zip(os.path.join(BASE, f"t2-6-{tag}", "bad.asave"),
                        {"save.json": sj_bytes})
        before = snapshot(sv)
        o = jout(run_save(["import", pkg], sv))
        check(o.get("reason_code") == "bad_manifest", f"T2-6 save.json {tag} → bad_manifest")
        check(snapshot(sv) == before, f"T2-6 {tag} 换位前拒绝，沙箱档未动")
    # 导出侧闭环
    sv = use("t2-6-export")
    seed_archive()
    sjp = B.save_manifest_path(ROOT)
    os.remove(sjp)
    o = jout(run_save(["export"], sv))
    check(o.get("reason_code") == "corrupt_save", "T2-6 删 save.json 后 export → corrupt_save")
    B.atomic_write_text(sjp, "{bad json")
    o = jout(run_save(["export"], sv))
    check(o.get("reason_code") == "corrupt_save", "T2-6 save.json 损坏 → export corrupt_save")


# ---------- T3 自动备份 ----------

def t3_auto_backup():
    print("\n[T3] 自动备份确实生成且可读通")
    sv = seeded("t3")
    rows0 = B.scan_save_stats(ROOT)["rows_total"]
    pkg = B.pack_save(ROOT, out=os.path.join(BASE, "t3", "incoming.asave"))["file"]
    _, o = run_save_import(["import", pkg], sv)
    check(o.get("ok") and o.get("backup") and os.path.isfile(o["backup"]),
          f"T3 导入后汇报备份：{os.path.basename(o.get('backup') or '?')}")
    # mtime 序取最新：glob 无序，若有残留旧包（清场半失败等环境态）baks[0] 会取中旧包误判
    baks = sorted(glob.glob(os.path.join(os.path.dirname(sv), "crew-save-auto-backup-*.asave")),
                  key=os.path.getmtime)
    check(len(baks) >= 1, f"T3 上级目录出现 crew-save-auto-backup-*.asave ×{len(baks)}")
    info = B.inspect_package(baks[-1], ROOT)
    check(info.get("rows_total") == rows0,
          f"T3 备份包 rows_total={info.get('rows_total')} = 导入前行数 {rows0}")


# ---------- T-dry dry-run 零写入 ----------

def t_dryrun():
    print("\n[T-dry] export/import --dry-run 零写入")
    sv = seeded("dry")
    before = snapshot(sv)
    o = jout(run_save(["export", "--dry-run"], sv))
    check(o.get("ok") and o.get("dry_run") and "would_file" in o
          and o.get("rows_total", 0) > 0 and "agents" in o,
          "T-dry export --dry-run 报告 would_file/entries/bytes/rows_total/agents")
    check(not glob.glob(os.path.join(os.path.dirname(sv), "*.asave")),
          "T-dry export --dry-run 未落盘")
    pkg = B.pack_save(ROOT, out=os.path.join(BASE, "dry", "p.asave"))["file"]
    o = jout(run_save(["import", pkg, "--dry-run"], sv))
    plan = o.get("plan") or {}
    check(o.get("ok") and o.get("dry_run") and plan.get("backup_would_be")
          and plan.get("replace") is True and plan.get("migrate_would") == [],
          "T-dry import --dry-run 预演 plan（备份路径/replace/migrate_would）")
    check(not os.path.exists(sv + ".lock") and snapshot(sv) == before,
          "T-dry 未取锁未写档")
    o = jout(run_save(["import", pkg, "--dry-run"], os.path.join(BASE, "dry", "nowhere", "save")))
    check(o.get("ok") and (o.get("plan") or {}).get("backup_would_be") is None,
          "T-dry 目标无档 → backup_would_be=null")


# ---------- T4 迁移接续（打桩 CURRENT=2） ----------

def t4_migration_chain():
    print("\n[T4] 旧版本档导入后迁移接续（函数级打桩：CURRENT=2 + migrate_v2 注入）")
    sv = use("t4")
    seed_archive()
    pkg = B.pack_save(ROOT, out=os.path.join(BASE, "t4", "v1.asave"))["file"]
    spec = importlib.util.spec_from_file_location("save_under_test", SAVEPY)
    save_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(save_mod)
    import migrate_save
    marker = os.path.join(BASE, "t4", "migrate_v2.marker")

    def fake_v2(root):
        B.atomic_write_text(marker, "v2 applied at " + B.now_iso())

    migrate_save.migrate_v2 = fake_v2
    old_cur = B.SAVE_VERSION_CURRENT
    B.SAVE_VERSION_CURRENT = 2
    try:
        o: dict = {}
        rc = 0
        for _attempt in (1, 2):  # 换位撞占位（swap_failed 已回滚）→ 稍候重试一次；迁移在换位成功后才跑，重跑干净
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = save_mod.cmd_import(Namespace(file=pkg, force=False, dry_run=False))
            o = json.loads(buf.getvalue())
            if o.get("ok") or o.get("reason_code") != "swap_failed":
                break  # 成功或确定性失败都不重试
            time.sleep(0.2)
        check(rc == 0 and o.get("ok"), f"T4 壳层导入+迁移成功（exit={rc}）")
        check(o.get("migrated") == [2], f"T4 汇报 migrated={o.get('migrated')}")
        check(os.path.isfile(marker), "T4 migrate_v2 确实执行（标记文件存在）")
        check(B.load_save_manifest(ROOT).get("save_version") == 2, "T4 沙箱档升到 v2")
        check(not os.path.exists(sv + ".lock"), "T4 迁移在锁内完成后锁已释放")
    finally:
        B.SAVE_VERSION_CURRENT = old_cur
        del migrate_save.migrate_v2


# ---------- T5 面板端点冒烟（函数级） ----------

def t5_panel():
    print("\n[T5] 面板端点冒烟（importlib 独立名加载 04-panel/server.py）")
    spec = importlib.util.spec_from_file_location("crew_panel_under_test", PANEL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    need = ("api_save_export", "api_save_import_preview",
            "api_save_import_confirm", "IMPORT_PENDING")
    if not all(hasattr(mod, f) for f in need):
        print("  （面板端点未落地——本节跳过，面板侧实现后自动启用）")
        return

    def confirm_retry(args: dict, token: str) -> tuple[dict, int]:
        """confirm 撞 swap_failed（in-process 换位一次性 os.rename 撞杀软占位，库侧已回滚）
        的容错：IMPORT_PENDING 一次性销号（无论成败即 pop），token 已死、无法原地重试——
        重新 preview 取新 token 再 confirm 一次；仅对 swap_failed 重试，其余拒绝
        （伪造/过期/篡改）是确定性结果，不重试。"""
        out, c = mod.api_save_import_confirm(token)
        if out.get("ok") is False and out.get("reason_code") == "swap_failed":
            np, _ = mod.api_save_import_preview(args)
            if np.get("ok") and np.get("token"):
                out, c = mod.api_save_import_confirm(np["token"])
        return out, c

    # 导出
    sv = use("t5-export")
    seed_archive()
    ex = mod.api_save_export()
    check(ex.get("ok") and os.path.isfile(ex.get("file", ""))
          and os.path.dirname(ex["file"]) == os.path.dirname(sv),
          f"T5 api_save_export 落沙箱上级：{os.path.basename(ex.get('file', '?'))}")
    # path 预览 → token 入账；伪造/真 token confirm
    sv = seeded("t5-path")
    pkg = B.pack_save(ROOT, out=os.path.join(BASE, "t5-path", "incoming.asave"))["file"]
    payload, code = mod.api_save_import_preview({"path": pkg})
    tok = payload.get("token")
    check(payload.get("ok") and code == 200 and tok and tok in mod.IMPORT_PENDING,
          "T5 path 预览发 token 且入账 IMPORT_PENDING")
    fp, fc = mod.api_save_import_confirm("0" * 32)
    check(fp.get("ok") is False and fc == 400, "T5 伪造 token confirm → ok:false")
    cp, cc = confirm_retry({"path": pkg}, tok)
    check(cp.get("ok") and cc == 200 and tok not in mod.IMPORT_PENDING,
          "T5 真 token confirm 成功且一次性销号")
    check(bool(cp.get("backup")) and os.path.isfile(cp["backup"]),
          f"T5 confirm 自动备份生成：{os.path.basename(cp.get('backup') or '?')}")
    # zip-slip 预览 → 拒且不留 token
    with open(B.save_manifest_path(ROOT), "rb") as f:
        sj = f.read()
    slip = craft_zip(os.path.join(BASE, "t5-path", "slip.asave"),
                     {"save.json": sj, "../evil.txt": b"x"})
    n_tok = len(mod.IMPORT_PENDING)
    sp, _ = mod.api_save_import_preview({"path": slip})
    check(sp.get("ok") is False and sp.get("reason_code") == "unsafe_entry"
          and len(mod.IMPORT_PENDING) == n_tok,
          "T5 zip-slip 预览拒绝且不留 token")
    # b64 生命周期：成功 confirm 后暂存删
    sv = seeded("t5-b64")
    with open(pkg, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    bp, _ = mod.api_save_import_preview({"b64": b64, "filename": "upload.asave"})
    btok = bp.get("token")
    staged = (mod.IMPORT_PENDING.get(btok) or {}).get("staged")
    check(bp.get("ok") and staged and os.path.isfile(staged)
          and "crew-import-staging-" in staged,
          "T5 b64 预览暂存落盘（crew-import-staging-*）")
    bcp, _ = confirm_retry({"b64": b64, "filename": "upload.asave"}, btok)
    check(bcp.get("ok") and staged and wait_gone(staged),
          "T5 b64 confirm 成功后暂存文件已删（尽力而为 1s 容错轮询）")
    # b64 失败路径：预览后篡改暂存 → confirm 拦截且暂存仍删
    bp2, _ = mod.api_save_import_preview({"b64": b64, "filename": "upload2.asave"})
    st2 = mod.IMPORT_PENDING[bp2["token"]]["staged"]
    with open(st2, "ab") as f:
        f.write(b"tampered")
    cp2, _ = mod.api_save_import_confirm(bp2["token"])
    check(cp2.get("ok") is False and wait_gone(st2),
          f"T5 b64 confirm 失败（{cp2.get('reason_code') or str(cp2.get('error'))[:30]}）暂存仍删（1s 容错轮询）")
    # path 模式篡改 → changed_since_preview
    sv = seeded("t5-tamper")
    pkg2 = B.pack_save(ROOT, out=os.path.join(BASE, "t5-tamper", "incoming.asave"))["file"]
    tp, _ = mod.api_save_import_preview({"path": pkg2})
    with open(pkg2, "r+b") as f:
        f.seek(-1, os.SEEK_END)
        f.write(bytes([f.read(1)[0] ^ 0xFF]))
    tp2, _ = mod.api_save_import_confirm(tp["token"])
    check(tp2.get("ok") is False and tp2.get("reason_code") == "changed_since_preview",
          "T5 预览后篡改字节 → changed_since_preview 拦截")
    # token 过期清扫 → 暂存同步删盘（任意一次成功预览触发 _prune_import_tokens）
    pkg3 = B.pack_save(ROOT, out=os.path.join(BASE, "t5-tamper", "sweep.asave"))["file"]
    fd, staged_old = tempfile.mkstemp(prefix="crew-import-staging-", suffix=".asave")
    os.write(fd, b"stale")
    os.close(fd)
    mod.IMPORT_PENDING["expired-tok"] = {"pkg_path": staged_old, "sha256": "0" * 64,
                                         "ts": time.time() - 7200, "staged": staged_old}
    mod.api_save_import_preview({"path": pkg3})
    check("expired-tok" not in mod.IMPORT_PENDING and wait_gone(staged_old),
          "T5 过期 token 清扫时暂存文件同步删盘（1s 容错轮询）")


# ---------- T9 面板 Handler 回归（B2/B3/B4/B5/B6/B7） ----------

def t9_panel_http():
    print("\n[T9] 面板 Handler 回归（真起本地服务；_guard 只认 Host 头，绑随机端口直连）")
    spec = importlib.util.spec_from_file_location("crew_panel_http_under_test", PANEL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sv = seeded("t9")
    pkg = B.pack_save(ROOT, out=os.path.join(BASE, "t9", "incoming.asave"))["file"]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def req(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
        headers = {"Host": "127.0.0.1:7530"}  # 面板 _guard 的白名单 Host
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, headers)
        r = conn.getresponse()
        raw = r.read()
        loc = r.getheader("Location")
        conn.close()
        try:
            return r.status, loc, json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return r.status, loc, raw

    try:
        # --- B3 /docs/images 根界 ---
        st, _, img = req("GET", "/docs/images/logo.png")
        check(st == 200 and isinstance(img, bytes) and img[:4] == b"\x89PNG",
              "T9-B3 docs/images 正常图片可读")
        evil = os.path.join(BASE, "t9", "evil.png")
        with open(evil, "wb") as f:
            f.write(b"pwned-png")
        st, _, _ = req("GET", f"/docs/images/../../{BASE_REL}/t9/evil.png")
        check(st == 404, "T9-B3 /docs/images 穿越读沙箱 png → 404（根界校验）")

        # --- B2 delete_row：读存档切片、trash 落存档（v0.1 起首次真正可用） ---
        st, _, tbl = req("GET", "/api/agent/agentcrew.fitness/table/weight")
        rows = tbl.get("rows") or []
        check(st == 200 and len(rows) == 3, f"T9-B2 预览表可读（{len(rows)} 行）")
        victim = rows[0]["_id"]
        st, _, out = req("POST", "/api/agent/agentcrew.fitness/delete_row",
                         {"table": "weight", "row_id": victim})
        rel = (out.get("trash") or "").replace(os.sep, "/")
        check(st == 200 and out.get("ok") and out.get("deleted") == victim,
              "T9-B2 预览页展示的行删除成功")
        trash_abs = os.path.join(ROOT, rel.replace("/", os.sep))
        # 断言锚定沙箱常量动态换算（B9 复核：硬编码套件内前缀，BASE 迁移即假红）
        trash_rel = os.path.relpath(trash_abs, sandbox("t9")).replace(os.sep, "/")
        check(trash_rel.startswith("agents/agentcrew.fitness/data/trash/")
              and os.path.isfile(trash_abs),
              f"T9-B2 trash 落存档切片：{rel}")
        trows = B.read_jsonl(trash_abs)
        check(len(trows) == 1 and trows[0].get("_id") == victim and trows[0].get("_deleted_at")
              and trows[0].get("note") == rows[0].get("note"),
              "T9-B2 trash 行含 _deleted_at 且内容保真（中文行）")
        st, _, tbl2 = req("GET", "/api/agent/agentcrew.fitness/table/weight")
        rows2 = tbl2.get("rows") or []
        check(st == 200 and len(rows2) == 2 and all(r.get("_id") != victim for r in rows2),
              "T9-B2 表中该行已移除（余 2 行）")
        st, _, err = req("POST", "/api/agent/agentcrew.ghost/delete_row",
                         {"table": "weight", "row_id": "nope"})
        check(st == 404 and err.get("ok") is False, "T9-B2 不存在的助理 → 干净 404（不再 NameError）")
        st, _, err = req("POST", "/api/agent/agentcrew.fitness/delete_row",
                         {"table": "weight", "row_id": "nope"})
        check(st == 404 and err.get("ok") is False, "T9-B2 行不存在 → 404 row not found")

        # --- B4 /jump 一次性 token（/api/state 发放、/jump 校验销号） ---
        st, _, state = req("GET", "/api/state")
        jump = state.get("jump") or {}
        check(st == 200 and state.get("ok") and jump,
              f"T9-B4 /api/state 发放一次性跳转 token（{len(jump)} 枚）")
        target = "agentcrew.fitness" if "agentcrew.fitness" in jump else next(iter(jump))
        tok = jump[target]
        check(tok in mod.JUMP_PENDING, "T9-B4 token 已入账 JUMP_PENDING")
        st, _, err = req("GET", "/jump/" + "0" * 32)
        check(st == 403 and err.get("ok") is False, "T9-B4 伪 token → 403")
        st, _, _ = req("GET", "/jump/agentcrew.fitness")
        check(st == 403, "T9-B4 旧式无 token 直跳（<img> CSRF 面）→ 403")
        st, loc, _ = req("GET", "/jump/" + tok)
        check(st == 302 and loc == f"/agent-file/{target}/dashboard/index.html",
              f"T9-B4 真 token → 302 看板（{target}）")
        check(tok not in mod.JUMP_PENDING, "T9-B4 token 即销号")
        st, _, _ = req("GET", "/jump/" + tok)
        check(st == 403, "T9-B4 重放同一 token → 403")

        # --- B5 install zip 模式 filename 消毒 ---
        tmpdir = tempfile.gettempdir()
        inst_before = {n for n in os.listdir(tmpdir) if n.startswith("panel-install-")}
        with open(os.path.join(ROOT, "03-template", "manifest.json"), "rb") as f:
            mani_bytes = f.read()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("pkg/manifest.json", mani_bytes)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        for bad in ("../evil.zip", "..\\evil.zip", "C:\\evil.zip"):
            st, _, out = req("POST", "/api/install/preview",
                             {"mode": "zip", "b64": b64, "filename": bad})
            check(st == 400 and out.get("ok") is False,
                  f"T9-B5 zip 文件名 {bad!r} → 业务拒绝（400）")
        check(not os.path.exists(os.path.join(tmpdir, "evil.zip"))
              and not os.path.exists("C:\\evil.zip"),
              "T9-B5 恶意文件名未逃逸落盘")
        inst_after = {n for n in os.listdir(tmpdir) if n.startswith("panel-install-")}
        check(inst_after == inst_before,
              f"T9-B5 拒绝路径零 panel-install-* 残留（{len(inst_after)} 个）")
        st, _, out = req("POST", "/api/install/preview",
                         {"mode": "zip", "b64": b64, "filename": "agent.zip"})
        tok5 = out.get("token")
        check(st == 200 and out.get("ok") and tok5 in mod.INSTALL_PENDING
              and mod.INSTALL_PENDING[tok5].get("tmp_root"),
              "T9-B5 合法文件名预览成功并登记临时根")
        t5root = mod.INSTALL_PENDING[tok5]["tmp_root"]
        mod._drop_install_token(tok5)
        check(tok5 not in mod.INSTALL_PENDING and not os.path.exists(t5root),
              "T9-B5 逐出 token 时解包临时目录一并 rmtree")

        # --- B6 confirm 补 token 时间戳校验 ---
        st, _, out = req("POST", "/api/save-import/preview", {"path": pkg})
        tok6 = out.get("token")
        check(st == 200 and out.get("ok") and tok6, "T9-B6 导入预览发 token")
        mod.IMPORT_PENDING[tok6]["ts"] -= mod.IMPORT_TOKEN_TTL + 10
        st, _, out = req("POST", "/api/save-import/confirm", {"token": tok6})
        check(st == 400 and out.get("ok") is False and "无效或已过期" in (out.get("error") or ""),
              "T9-B6 导入 confirm 过期 token → 拒绝（ts 校验）")
        st, _, out = req("POST", "/api/install/preview",
                         {"mode": "path", "path": os.path.join(ROOT, "03-template")})
        tok7 = out.get("token")
        check(st == 200 and out.get("ok") and tok7 in mod.INSTALL_PENDING,
              "T9-B6 安装预览（03-template）发 token")
        mod.INSTALL_PENDING[tok7]["ts"] -= 3700
        st, _, out = req("POST", "/api/install/confirm", {"token": tok7})
        check(st == 400 and "无效或已过期" in (out.get("error") or ""),
              "T9-B6 安装 confirm 过期 token → 拒绝（未触发 adopt）")

        # --- B7 临时目录孤儿清扫（启动兜底 + TTL 逐出连带 rmtree） ---
        stale_d = tempfile.mkdtemp(prefix="panel-install-")
        fd, stale_f = tempfile.mkstemp(prefix="crew-import-staging-", suffix=".asave")
        os.close(fd)
        old = time.time() - mod.IMPORT_TOKEN_TTL - 10
        os.utime(stale_d, (old, old))
        os.utime(stale_f, (old, old))
        fresh_d = tempfile.mkdtemp(prefix="panel-install-")
        fd, fresh_f = tempfile.mkstemp(prefix="crew-import-staging-", suffix=".asave")
        os.close(fd)
        mod._sweep_temp_orphans()
        check(not os.path.exists(stale_d) and not os.path.exists(stale_f),
              "T9-B7 启动清扫：超 TTL 的 panel-install-*/crew-import-staging-* 已删")
        check(os.path.isdir(fresh_d) and os.path.isfile(fresh_f), "T9-B7 未过期的临时文件不动")
        shutil.rmtree(fresh_d, ignore_errors=True)
        try:
            os.remove(fresh_f)
        except OSError:
            pass
        ev = tempfile.mkdtemp(prefix="panel-install-")
        os.makedirs(os.path.join(ev, "x", "pkg"), exist_ok=True)
        shutil.copy(os.path.join(ROOT, "03-template", "manifest.json"),
                    os.path.join(ev, "x", "pkg", "manifest.json"))
        mod.INSTALL_PENDING["t9-expired"] = {"dir": os.path.join(ev, "x", "pkg"),
                                             "tmp_root": ev, "ts": time.time() - 7200}
        req("POST", "/api/install/preview",
            {"mode": "path", "path": os.path.join(ROOT, "03-template")})
        check("t9-expired" not in mod.INSTALL_PENDING and not os.path.exists(ev),
              "T9-B7 预览 TTL 清扫连带 rmtree 解包临时目录")
    finally:
        srv.shutdown()
        srv.server_close()


# ---------- T7 首装导入（目标无档） ----------

def t7_fresh_install():
    print("\n[T7] 首装导入（目标存档不存在——跨机迁移主场景）")
    sv_src = use("t7-src")
    seed_archive()
    pkg = os.path.join(BASE, "t7", "incoming.asave")
    B.pack_save(ROOT, out=pkg)
    sv = os.path.join(BASE, "t7", "fresh", "save")  # 不存在的子路径
    r, o = run_save_import(["import", pkg], sv)
    check(r.returncode == 0 and o.get("ok"), f"T7 无档目标导入成功（exit={r.returncode}）")
    check(o.get("backup") is None, "T7 backup=null（首装合法成功态）")
    os.environ["AGENTCREW_SAVE"] = sv
    stats = B.scan_save_stats(ROOT)
    info = B.inspect_package(pkg, ROOT)
    check(stats["rows_total"] == info["rows_total"] and stats["rows_total"] > 0,
          f"T7 落档行数与包一致（{stats['rows_total']} 行）")
    check(B.load_save_manifest(ROOT) is not None, "T7 save.json 已就位")


# ---------- T8 压缩比除零守卫 ----------

def t8_ratio_guard():
    print("\n[T8] 压缩比除零守卫（函数级，直喂闸②限检函数）")
    zi = zipfile.ZipInfo("bomb.bin")
    zi.file_size, zi.compress_size = 1024, 0
    try:
        check(B._entry_limit_reason(zi) == "too_large",
              "T8 compress_size=0 + file_size>0 → too_large（不抛 ZeroDivisionError）")
    except ZeroDivisionError:
        check(False, "T8 抛了 ZeroDivisionError（守卫失效）")
    zi2 = zipfile.ZipInfo("empty.jsonl")
    zi2.file_size, zi2.compress_size = 0, 0
    check(B._entry_limit_reason(zi2) is None, "T8 空文件（file_size=0）跳过比值检查")
    zi3 = zipfile.ZipInfo("ratio.bin")
    zi3.file_size, zi3.compress_size = 1_000_001, 1_000
    check(B._entry_limit_reason(zi3) == "too_large", "T8 压缩比 >1000 → too_large")
    zi3.compress_size = 2_000
    check(B._entry_limit_reason(zi3) is None, "T8 压缩比 ≤1000 → 放行")


# ---------- 主流程 ----------

def main() -> int:
    B.utf8_console()
    print("# 存档导出/导入套件（v0.3）——全程 AGENTCREW_SAVE 沙箱，首尾清场\n")
    # 陈沙箱清扫（崩溃/强杀残留）：只收 6h 前的 savepack-*/savepack——套件全程仅数分钟，
    # mtime 更新的必是活动会话的沙箱，绝不触碰（并发互踩曾是漂移失败根因）。
    cutoff = time.time() - 6 * 3600
    for pat in ("savepack", "savepack-*"):
        for d in glob.glob(os.path.join(HERE, "_sandbox", pat)):
            try:
                if os.path.getmtime(d) < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
    rm_tree(BASE)  # 套件首清场（路径进程唯一；仅 PID 号被复用撞上时才可能命中）
    os.makedirs(BASE, exist_ok=True)
    check(os.path.basename(BASE) == f"savepack-{os.getpid()}",
          "T0 沙箱基路径进程唯一（多会话并发互不踩踏）")
    # 临时目录同样进程唯一：面板暂存/安装解包/导入 staging 的 tempfile.* 全部收进
    # 本进程沙箱——%TEMP% 是全机共享位置，并发实例互不可见，T9-B5 清点与 B7 清扫
    # 才能密封比较（并发实测曾撞上对方实例的合法暂存 → 清点差集假红）。
    tempfile.tempdir = os.path.join(BASE, "tmp")
    os.makedirs(tempfile.tempdir, exist_ok=True)
    check(tempfile.gettempdir() == tempfile.tempdir,
          "T0 临时目录进程唯一（暂存/解包收进沙箱，不进共享 %TEMP%）")
    global _TRIPWIRE
    B.save_root = _guarded_save_root
    _TRIPWIRE = True
    try:
        t1_roundtrip()
        t2_gates()
        t26_version_legality()
        t3_auto_backup()
        t_dryrun()
        t4_migration_chain()
        t5_panel()
        t9_panel_http()
        t7_fresh_install()
        t8_ratio_guard()
    finally:
        _TRIPWIRE = False
        B.save_root = _REAL_SAVE_ROOT
        tempfile.tempdir = _ORIG_TEMPDIR
        if _ORIG_ENV is None:
            os.environ.pop("AGENTCREW_SAVE", None)
        else:
            os.environ["AGENTCREW_SAVE"] = _ORIG_ENV
        rm_tree(BASE)  # 套件尾清场
        print(f"\n# 沙箱清场：{BASE} 已移除")
    bad = [label for okk, label in results if not okk]
    print(f"# 结果：{len(results) - len(bad)}/{len(results)} 通过")
    for b in bad:
        print(f"  失败：{b}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
