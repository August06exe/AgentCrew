#!/usr/bin/env python3
"""test_repairs.py — 框架核心修复回归套件（全仓评审 46 条中的核心行为项）。

覆盖：
  T1 release：--purge-data 不带 --yes → 入口即拒，零删除（守卫先于一切 rmtree）；
  T2 authz：从存档切片导出（lite 源）成功 + 过期快照清扫落存档回收站（含 master 信箱）
            + sweep 后 save.py watch 仍干净；
  T3 adopt：校验失败零切片残留（zip 式不再产生 butler-adopt-* 垃圾切片）；
            就地收编成功恰好一个真实 id 切片；
  T4 pack_save：--out 指进存档根被拒（out_inside_save）+ 陈旧 .part.asave 不入包
            且 manifest 口径自洽（inspect 全闸复检通过）；
  T5 doctor：全检（含工具 selfcheck）前后真实档 daily_records.jsonl 字节不变；
  T6 make-instance：registry 检查走存档口径（函数级，v0 位置诱饵不误报）；
  T7 import：解包流式直写（多块大条目导入成功，行为与旧实现等价）。

红线：每个用例独立沙箱存档（AGENTCREW_SAVE/ASSISTANT_DATA_DIR 显式传入子进程；
in-process 构造沙箱态前先设 env，套件首尾清场并还原），绝不读写真实 _save 的数据行。

用法：python 06-tests/test_repairs.py
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "05-scripts"))
import agentcrew_lib as B  # noqa: E402

PY = sys.executable
RELEASE = os.path.join(ROOT, "05-scripts", "release.py")
AUTHZ = os.path.join(ROOT, "05-scripts", "authz.py")
ADOPT = os.path.join(ROOT, "05-scripts", "adopt.py")
SAVEPY = os.path.join(ROOT, "05-scripts", "save.py")
DOCTOR = os.path.join(ROOT, "05-scripts", "doctor.py")
MAKE_INSTANCE = os.path.join(ROOT, "05-scripts", "make-instance.py")
SAMPLE_DATA_PY = os.path.join(ROOT, "02-agents", "demo.sample", "tools", "data.py")
BASE = os.path.join(HERE, "_sandbox", "repairs")

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> bool:
    results.append((bool(cond), label))
    print(f" {'✓' if cond else '✗'} {label}")
    return bool(cond)


# ---------- T0 沙箱红线设施 ----------

_ORIG_SAVE = os.environ.get("AGENTCREW_SAVE")
_ORIG_DATA = os.environ.get("ASSISTANT_DATA_DIR")


def use_save(path: str) -> None:
    """in-process 红线动作：设 AGENTCREW_SAVE 指向沙箱（构造沙箱态用）。"""
    os.environ["AGENTCREW_SAVE"] = path


def run_py(script: str, args: list[str], save_env: str | None,
           data_env: str | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
    """子进程跑框架脚本：env 显式注入沙箱；save_env=None 时剥掉两个 env（真实体检姿势）。"""
    env = {k: v for k, v in os.environ.items()
           if k not in ("AGENTCREW_SAVE", "ASSISTANT_DATA_DIR")}
    if save_env is not None:
        env["AGENTCREW_SAVE"] = save_env
    if data_env is not None:
        env["ASSISTANT_DATA_DIR"] = data_env
    return subprocess.run([PY, script, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          env=env, cwd=ROOT)


def jout(r: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"非 JSON 输出：{(r.stdout or r.stderr)[:200]}"}


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def slices_of(sr: str) -> list[str]:
    d = os.path.join(sr, "agents")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def zip_dir(src_dir: str, zip_path: str) -> str:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _dirs, files in os.walk(src_dir):
            for fn in files:
                fp = os.path.join(base, fn)
                z.write(fp, os.path.relpath(fp, src_dir).replace(os.sep, "/"))
    return zip_path


def make_agent_package(pkg_dir: str, aid: str, *, broken: bool = False) -> None:
    """构造一个可收编的最小助理包（数据工具直接复用样板助理的自包含 data.py）。"""
    os.makedirs(os.path.join(pkg_dir, "tools"), exist_ok=True)
    shutil.copyfile(SAMPLE_DATA_PY, os.path.join(pkg_dir, "tools", "data.py"))
    manifest = {
        "protocol": "agentcrew.protocol/v1", "paradigm": "agentcrew.paradigm/v1",
        "id": aid, "name": "回归测试助理", "version": "0.1.0",
        "description": "测试包：收编校验回归用", "author": "test", "license": "MIT",
        "domain": ["回归测试专用域"],
        "tables": [{"name": "notes",
                    "columns": [{"name": "title", "type": "string", "required": True}]}],
        "tools": ["tools/data.py"],
    }
    if broken:
        del manifest["domain"]  # 少必填字段 → s1 必败
    B.atomic_write_json(os.path.join(pkg_dir, "manifest.json"), manifest)
    B.atomic_write_text(os.path.join(pkg_dir, "AGENTS.md"),
                        "# 回归测试助理章程\n向上找总管实例根；协议见 00-docs/PROTOCOL.md。\n")
    B.atomic_write_text(os.path.join(pkg_dir, "persona.md"), "人设：测试。\n")
    B.atomic_write_text(os.path.join(pkg_dir, "LICENSE"), "MIT\n")


def _real_program_trash_state() -> dict[str, bool]:
    """真实收编区各助理 data/trash 现状（sweep 前后对比，防回归污染真实程序区）。"""
    out: dict[str, bool] = {}
    agdir = os.path.join(ROOT, "02-agents")
    for d in sorted(os.listdir(agdir)):
        out[os.path.join(agdir, d, "data", "trash")] = os.path.isdir(os.path.join(agdir, d, "data", "trash"))
    return out


# ---------- T1 release：--yes 守卫前置 ----------

def t1_release_guard():
    print("\n[T1] release --purge-data 不带 --yes → 入口即拒零删除")
    case = os.path.join(BASE, "t1")
    sr = os.path.join(case, "save")
    use_save(sr)
    aid = "t.rep"
    # 程序区代理：registry 的 dir 指进沙箱（release 经 registry/存档触达，全程沙箱内）
    agent_dir = os.path.join(case, "mini", "02-agents", aid)
    os.makedirs(os.path.join(agent_dir, "tools"), exist_ok=True)
    B.atomic_write_json(os.path.join(agent_dir, "manifest.json"), {"id": aid})
    B.ensure_save(ROOT)
    B.atomic_write_json(B.registry_path(ROOT), {
        "$schema": "agentcrew.registry/v1", "master_instance": "t1",
        "adopted": [{"id": aid,
                     "dir": os.path.relpath(agent_dir, ROOT).replace(os.sep, "/")}],
    })
    # 删除靶标：存档切片里一行真实数据
    B.ensure_agent_skeleton(os.path.join(sr, "agents", aid), ROOT)
    keep = B.p(sr, "agents", aid, "data", "keep.jsonl")
    B.append_jsonl(keep, {"_id": "k1", "v": 1})
    standalone = os.path.join(case, "standalone")

    r = run_py(RELEASE, [aid, "--purge-data", "--dest", standalone], save_env=sr)
    o = jout(r)
    check(r.returncode != 0 and o.get("ok") is False and "--yes" in o.get("error", ""),
          "T1 不带 --yes 的 --purge-data → 拒绝并说明需 --yes")
    check(os.path.isfile(keep) and os.path.isdir(agent_dir),
          "T1 拒绝先于删除：切片数据行与程序目录原样")
    check(not os.path.isdir(standalone), "T1 未产生放归产物")
    # 正路回归：--yes 合法清除不被守卫误伤
    r = run_py(RELEASE, [aid, "--purge-data", "--yes", "--dest", standalone], save_env=sr)
    o = jout(r)
    check(o.get("ok") and not os.path.exists(keep), "T1 --yes 合法清除：存档切片已删")
    check(os.path.isdir(os.path.join(standalone, aid)), "T1 程序目录已放归沙箱 standalone")


# ---------- T2 authz：存档切片导出 + 清扫落存档 ----------

def t2_authz_archive():
    print("\n[T2] authz 从存档切片导出（lite 源）+ sweep 落存档回收站 + watch 干净")
    case = os.path.join(BASE, "t2")
    sr = os.path.join(case, "save")
    use_save(sr)
    # lite 源助理：registry dir 的路径链含 _standalone 组件 → resolve_agent_save 判 lite
    src_dir = os.path.join(case, "_standalone", "authz.src")
    dst_dir = os.path.join(case, "agents-dir", "authz.dst")
    for d in (src_dir, dst_dir):
        os.makedirs(os.path.join(d, "tools"), exist_ok=True)
    B.ensure_save(ROOT)
    rel = lambda pth: os.path.relpath(pth, ROOT).replace(os.sep, "/")  # noqa: E731
    B.atomic_write_json(B.registry_path(ROOT), {
        "$schema": "agentcrew.registry/v1", "master_instance": "t2",
        "adopted": [{"id": "authz.src", "dir": rel(src_dir)},
                    {"id": "authz.dst", "dir": rel(dst_dir)}],
    })
    # 源表只落在【存档切片】（lite=自带 _save/data）——程序区无表，v0 读取必落空
    table = B.p(B.agent_save_dir_lite(src_dir), "data", "mood.jsonl")
    B.append_jsonl(table, {"_id": "m1", "mood": "好"})
    B.append_jsonl(table, {"_id": "m2", "mood": "还行"})

    r = run_py(AUTHZ, ["--from", "authz.src", "--table", "mood",
                       "--to", "authz.dst", "--purpose", "回归测试", "--granted"], save_env=sr)
    o = jout(r)
    check(o.get("ok") and o.get("rows") == 2, f"T2 从存档切片导出成功 rows={o.get('rows')}")
    inbox_files = os.listdir(B.p(sr, "agents", "authz.dst", "inbox")) if os.path.isdir(
        B.p(sr, "agents", "authz.dst", "inbox")) else []
    check(len(inbox_files) == 1 and inbox_files[0].startswith("snapshot-"),
          f"T2 快照已投放申请方存档信箱：{inbox_files}")

    # ---- sweep：两个过期快照（托管助理切片信箱 + master 存档信箱）----
    trash_before = _real_program_trash_state()
    snap_path = B.p(sr, "agents", "demo.sample", "inbox", "snapshot-expired-a.json")
    B.ensure_agent_skeleton(B.p(sr, "agents", "demo.sample"), ROOT)
    B.atomic_write_json(snap_path, {"snapshot_id": "snapshot-expired-a",
                                    "source_agent": "agentcrew.fitness",
                                    "source_table": "weight", "purpose": "T2",
                                    "granted_by": "owner",
                                    "expires_at": "2000-01-01T00:00:00+00:00",
                                    "read_only": True, "rows": [], "row_count": 0})
    B.atomic_write_json(B.p(sr, "master", "inbox-master", "snapshot-expired-m.json"),
                        {"snapshot_id": "snapshot-expired-m", "source_agent": "x",
                         "source_table": "y", "purpose": "T2", "granted_by": "owner",
                         "expires_at": "2000-01-01T00:00:00+00:00",
                         "read_only": True, "rows": [], "row_count": 0})
    r = run_py(AUTHZ, ["--sweep"], save_env=sr)
    o = jout(r)
    ids = sorted(i.get("snapshot_id") or "" for i in (o.get("items") or []))
    check(o.get("ok") and o.get("swept") == 2
          and "snapshot-expired-a" in ids and "snapshot-expired-m" in ids,
          f"T2 sweep 清掉 2 张过期快照（助理切片+master 存档信箱）：{ids}")
    check(not os.path.exists(snap_path)
          and not os.path.exists(B.p(sr, "master", "inbox-master", "snapshot-expired-m.json")),
          "T2 两处信箱均已清空")
    check(os.path.isfile(B.p(sr, "agents", "demo.sample", "data", "trash",
                             "expired-snapshots.jsonl")),
          "T2 助理侧回收站落存档切片 data/trash（不写程序区）")
    check(os.path.isfile(B.p(sr, "master", "trash", "expired-snapshots.jsonl")),
          "T2 master 回收站落存档 trash")
    # 防污染守卫：若回归复现（写真实程序区），回滚并判败
    new_pollution = [tp for tp, existed in trash_before.items()
                     if not existed and os.path.isdir(tp)]
    for tp in new_pollution:
        shutil.rmtree(tp, ignore_errors=True)
    check(not new_pollution, "T2 sweep 零程序区 trash 污染")
    # watch 必须仍然干净（A3 的验收口径）
    r = run_py(SAVEPY, ["watch"], save_env=sr)
    o = jout(r)
    check(r.returncode == 0 and o.get("ok") and o.get("clean") is True,
          f"T2 sweep 后 save.py watch 干净：{o.get('error', '')[:80]}")


# ---------- T3 adopt：失败零残留 / 成功真实 id ----------

def t3_adopt_slices():
    print("\n[T3] adopt 校验失败零切片残留 + 成功恰好一个真实 id 切片")
    case = os.path.join(BASE, "t3")
    # 失败路径：zip 式收编坏包（缺 domain）——v0 缺陷会先建 butler-adopt-* 垃圾切片
    sr_bad = os.path.join(case, "bad", "save")
    use_save(sr_bad)
    B.ensure_save(ROOT)
    make_agent_package(os.path.join(case, "bad", "pkg"), "t.bad", broken=True)
    bad_zip = zip_dir(os.path.join(case, "bad", "pkg"), os.path.join(case, "bad", "bad.zip"))
    before = slices_of(sr_bad)
    r = run_py(ADOPT, ["--zip", bad_zip], save_env=sr_bad,
               data_env=os.path.join(case, "bad", "data"))
    o = jout(r)
    check(r.returncode != 0 and o.get("ok") is False and "收编校验未通过" in o.get("error", ""),
          "T3 坏包收编被拒（缺 domain）")
    after = slices_of(sr_bad)
    check(after == before and not any(s.startswith("butler-adopt-") for s in after),
          f"T3 失败零切片残留（含 butler-adopt-*）：{after or '无'}")

    # 成功路径：就地收编真实助理（demo.sample），沙箱存档 + 沙箱数据目录
    sr_ok = os.path.join(case, "ok", "save")
    use_save(sr_ok)
    B.ensure_save(ROOT)
    before = slices_of(sr_ok)
    r = run_py(ADOPT, ["--path", os.path.join(ROOT, "02-agents", "demo.sample")],
               save_env=sr_ok, data_env=os.path.join(case, "ok", "data"))
    o = jout(r)
    check(r.returncode == 0 and o.get("ok") and o.get("adopted") == "demo.sample",
          "T3 就地收编 demo.sample 成功")
    after = slices_of(sr_ok)
    check(before == [] and after == ["demo.sample"],
          f"T3 成功恰好一个切片且名为真实 id：{after}")
    reg = B.read_json(B.registry_path(ROOT))
    check(any(e.get("id") == "demo.sample" for e in reg.get("adopted", [])),
          "T3 registry 已登记（存档口径）")
    check(not os.path.isdir(os.path.join(ROOT, "02-agents", "demo.sample", "_save")),
          "T3 程序区未产生随身 _save（真实助理目录零改动）")


# ---------- T4 pack_save：--out 守卫 + .part.asave 兜底 ----------

def t4_pack_guards():
    print("\n[T4] pack_save --out 指进存档根被拒 + 陈旧 .part.asave 不入包")
    case = os.path.join(BASE, "t4")
    sr = os.path.join(case, "save")
    use_save(sr)
    B.ensure_save(ROOT, agents=["t.x"])
    B.append_jsonl(B.p(sr, "agents", "t.x", "data", "weight.jsonl"),
                   {"_id": "w1", "weight_kg": 70})
    for out in (sr, os.path.join(sr, "sub", "dir")):
        r = run_py(SAVEPY, ["export", "--out", out], save_env=sr)
        o = jout(r)
        check(o.get("ok") is False and o.get("reason_code") == "out_inside_save",
              f"T4 --out 落存档根{'顶层' if out == sr else '子目录'} → out_inside_save")
    check(not any(f.endswith(".asave") for f in os.listdir(sr)),
          "T4 被拒落点零落盘")
    # 兜底：塞一个陈旧中间产物再导出——不入包且 manifest 口径自洽
    B.atomic_write_text(B.p(sr, "agents", "t.x", "data", "stale.part.asave"), "junk")
    r = run_py(SAVEPY, ["export"], save_env=sr)
    o = jout(r)
    check(o.get("ok") and os.path.isfile(o.get("file", "")), "T4 默认导出成功")
    with zipfile.ZipFile(o["file"]) as z:
        names = z.namelist()
    check(not any(n.endswith(".part.asave") for n in names),
          "T4 陈旧 .part.asave 未被打进包体")
    info = B.inspect_package(o["file"], ROOT)  # 全闸复检：entries/bytes 对账不过会抛
    check(isinstance(info, dict) and info.get("entries", 0) >= 1 and info.get("rows_total") == 1,
          f"T4 manifest 口径自洽（entries={info.get('entries')} rows={info.get('rows_total')}）")


# ---------- T5 doctor：体检不碰真实档 ----------

def t5_doctor_no_real_touch():
    print("\n[T5] doctor 全检（含 selfcheck）前后真实档 daily_records.jsonl 字节不变")
    real_file = os.path.join(ROOT, "_save", "agents", "agentcrew.fitness",
                             "data", "daily_records.jsonl")
    check(os.path.isfile(real_file), "T5 前置：真实档 daily_records.jsonl 存在")
    before = sha256_file(real_file)
    r = run_py(DOCTOR, [], save_env=None, data_env=None)  # 真实体检姿势：两个沙箱 env 全剥
    check(r.returncode == 0, f"T5 doctor 全检 exit=0（stderr: {(r.stderr or '')[:160]}）")
    check(sha256_file(real_file) == before, "T5 真实档 daily_records.jsonl 字节不变")
    check(not os.path.exists(os.path.join(ROOT, "06-tests", "_sandbox", "doctor-selfcheck")),
          "T5 体检自检沙箱已清场")


# ---------- T6 make-instance：registry 检查走存档口径（函数级） ----------

def t6_makeinstance_registry():
    print("\n[T6] make-instance registry 检查走存档路径（函数级）")
    os.environ.pop("AGENTCREW_SAVE", None)  # registry_path 受 env 影响，先剥掉
    spec = importlib.util.spec_from_file_location("make_instance_under_test", MAKE_INSTANCE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    inst = os.path.join(BASE, "t6", "inst")
    os.makedirs(os.path.join(inst, "01-master"), exist_ok=True)
    check(mod.registry_ready(inst) is False, "T6 无档 → False")
    B.atomic_write_json(os.path.join(inst, "01-master", "registry.json"), {"adopted": []})
    check(mod.registry_ready(inst) is False,
          "T6 v0 位置（01-master/registry.json）有诱饵仍 False——不再读旧路径")
    B.atomic_write_json(B.registry_path(inst), {"adopted": [{"id": "x.y"}]})
    check(mod.registry_ready(inst) is True, "T6 存档 registry（_save/master/）就位 → True")


# ---------- T7 import：流式解包行为等价 ----------

def t7_stream_unpack():
    print("\n[T7] 导入解包流式直写：3MiB 多块条目导入成功且字节守恒")
    case = os.path.join(BASE, "t7")
    os.makedirs(case, exist_ok=True)
    sr = os.path.join(case, "save")  # 不存在 → 首装导入路径
    use_save(sr)
    payload = os.urandom(3 * 1024 * 1024)  # 随机字节：压缩比≈1，不触闸②比值限
    sj = json.dumps({"save_version": 1, "created_at": B.now_iso()},
                    ensure_ascii=False).encode("utf-8")
    mani = {"kind": "agentcrew.save.export", "manifest_version": 1,
            "exported_at": B.now_iso(), "tool": "test_repairs",
            "save_version": 1, "program_save_version": B.SAVE_VERSION_CURRENT,
            "origin_external": True, "entries": 2,
            "bytes": len(sj) + len(payload), "rows_total": 0, "agents": {}}
    pkg = os.path.join(case, "big.asave")
    with zipfile.ZipFile(pkg, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("save.json", sj)
        z.writestr("big.bin", payload)
        z.writestr(B.MANIFEST_NAME, json.dumps(mani, ensure_ascii=False, indent=2) + "\n")
    r = run_py(SAVEPY, ["import", pkg], save_env=sr)
    o = jout(r)
    check(r.returncode == 0 and o.get("ok") and o.get("imported_entries") == 2,
          f"T7 3MiB 条目流式导入成功（exit={r.returncode}）")
    landed = os.path.join(sr, "big.bin")
    check(os.path.isfile(landed) and sha256_file(landed) == hashlib.sha256(payload).hexdigest(),
          "T7 落盘字节与包内一致")


# ---------- 主流程 ----------

def main() -> int:
    B.utf8_console()
    print("# 框架核心修复回归套件——全程沙箱存档，首尾清场\n")
    shutil.rmtree(BASE, ignore_errors=True)  # 套件首清场
    os.makedirs(BASE, exist_ok=True)
    try:
        t1_release_guard()
        t2_authz_archive()
        t3_adopt_slices()
        t4_pack_guards()
        t5_doctor_no_real_touch()
        t6_makeinstance_registry()
        t7_stream_unpack()
    finally:
        if _ORIG_SAVE is None:
            os.environ.pop("AGENTCREW_SAVE", None)
        else:
            os.environ["AGENTCREW_SAVE"] = _ORIG_SAVE
        if _ORIG_DATA is None:
            os.environ.pop("ASSISTANT_DATA_DIR", None)
        else:
            os.environ["ASSISTANT_DATA_DIR"] = _ORIG_DATA
        shutil.rmtree(BASE, ignore_errors=True)  # 套件尾清场
        print(f"\n# 沙箱清场：{BASE} 已移除")
    bad = [label for okk, label in results if not okk]
    print(f"# 结果：{len(results) - len(bad)}/{len(results)} 通过")
    for b in bad:
        print(f"  失败：{b}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
