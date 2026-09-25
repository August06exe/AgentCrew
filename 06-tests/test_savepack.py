#!/usr/bin/env python3
"""test_savepack.py — 存档单文件导出/导入（v0.3）回归套件。

覆盖：T1 导出→reset→导入 roundtrip（含同秒撞名保护）；T2 四道闸拒绝路径；
T2-6 版本合法性+导出 corrupt_save 闭环；T3 自动备份；T4 旧版本档迁移接续（打桩）；
T5 面板端点冒烟（函数级）；T7 首装导入（目标无档）；T8 压缩比除零守卫；T-dry dry-run 零写入。

红线（T0）：每个用例在任何调用前先把 AGENTCREW_SAVE 指向
06-tests/_sandbox/savepack/<case>/save——cmd_import/api_save_* 经 find_repo_root()
解析真实仓库根，漏设 env 会让 save_root 落回真实 _save/（对删除类操作是红线级前提）。
套件首尾清场，全程不触真实 _save。

用法：python 06-tests/test_savepack.py
"""
from __future__ import annotations

import base64
import contextlib
import glob
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from argparse import Namespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "05-scripts"))
import agentcrew_lib as B  # noqa: E402

PY = sys.executable
SAVEPY = os.path.join(ROOT, "05-scripts", "save.py")
PANEL = os.path.join(ROOT, "04-panel", "server.py")
BASE = os.path.join(HERE, "_sandbox", "savepack")

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> bool:
    results.append((bool(cond), label))
    print(f" {'✓' if cond else '✗'} {label}")
    return bool(cond)


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
    r = run_save(["import", pkg], sv)
    o = jout(r)
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
    r = run_save(["import", pkg, "--force"], sv)
    o = jout(r)
    check(r.returncode == 0 and o.get("ok") and "⚠" in (r.stderr or ""),
          "T2-4b --force 放行且 stderr 出 ⚠ 大字横幅")
    check("warning" in o, "T2-4b stdout JSON 附 warning 字段")
    check(B.load_save_manifest(ROOT).get("save_version") == B.SAVE_VERSION_CURRENT + 1,
          "T2-4b 沙箱档已被替换为新版")

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
    o = jout(run_save(["import", pkg], sv))
    check(o.get("ok") and o.get("backup") and os.path.isfile(o["backup"]),
          f"T3 导入后汇报备份：{os.path.basename(o.get('backup') or '?')}")
    baks = glob.glob(os.path.join(os.path.dirname(sv), "crew-save-auto-backup-*.asave"))
    check(len(baks) >= 1, f"T3 上级目录出现 crew-save-auto-backup-*.asave ×{len(baks)}")
    info = B.inspect_package(baks[0], ROOT)
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
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = save_mod.cmd_import(Namespace(file=pkg, force=False, dry_run=False))
        o = json.loads(buf.getvalue())
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
    cp, cc = mod.api_save_import_confirm(tok)
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
    bcp, _ = mod.api_save_import_confirm(btok)
    check(bcp.get("ok") and staged and not os.path.exists(staged),
          "T5 b64 confirm 成功后暂存文件已删")
    # b64 失败路径：预览后篡改暂存 → confirm 拦截且暂存仍删
    bp2, _ = mod.api_save_import_preview({"b64": b64, "filename": "upload2.asave"})
    st2 = mod.IMPORT_PENDING[bp2["token"]]["staged"]
    with open(st2, "ab") as f:
        f.write(b"tampered")
    cp2, _ = mod.api_save_import_confirm(bp2["token"])
    check(cp2.get("ok") is False and not os.path.exists(st2),
          f"T5 b64 confirm 失败（{cp2.get('reason_code') or str(cp2.get('error'))[:30]}）暂存仍删")
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
    check("expired-tok" not in mod.IMPORT_PENDING and not os.path.exists(staged_old),
          "T5 过期 token 清扫时暂存文件同步删盘")


# ---------- T7 首装导入（目标无档） ----------

def t7_fresh_install():
    print("\n[T7] 首装导入（目标存档不存在——跨机迁移主场景）")
    sv_src = use("t7-src")
    seed_archive()
    pkg = os.path.join(BASE, "t7", "incoming.asave")
    B.pack_save(ROOT, out=pkg)
    sv = os.path.join(BASE, "t7", "fresh", "save")  # 不存在的子路径
    r = run_save(["import", pkg], sv)
    o = jout(r)
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
    shutil.rmtree(BASE, ignore_errors=True)  # 套件首清场
    os.makedirs(BASE, exist_ok=True)
    try:
        t1_roundtrip()
        t2_gates()
        t26_version_legality()
        t3_auto_backup()
        t_dryrun()
        t4_migration_chain()
        t5_panel()
        t7_fresh_install()
        t8_ratio_guard()
    finally:
        if _ORIG_ENV is None:
            os.environ.pop("AGENTCREW_SAVE", None)
        else:
            os.environ["AGENTCREW_SAVE"] = _ORIG_ENV
        shutil.rmtree(BASE, ignore_errors=True)  # 套件尾清场
        print(f"\n# 沙箱清场：{BASE} 已移除")
    bad = [label for okk, label in results if not okk]
    print(f"# 结果：{len(results) - len(bad)}/{len(results)} 通过")
    for b in bad:
        print(f"  失败：{b}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
