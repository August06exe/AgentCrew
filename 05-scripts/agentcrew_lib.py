#!/usr/bin/env python3
"""agentcrew_lib.py — AgentCrew 总管公共库（仅标准库）。

被 05-scripts 下所有工具与测试复用。约定见 00-docs/PROTOCOL.md。
所有输出 JSON 的工具：成功 {"ok": true, ...}，失败 {"ok": false, "error": ...}。
"""
from __future__ import annotations

import contextlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime

PROTOCOL = "agentcrew.protocol/v1"
SAVE_DIRNAME = "_save"
SAVE_VERSION_CURRENT = 1  # 程序侧存档结构版本；改结构必须升版并附 migrate_save.py 阶梯
MANIFEST_NAME = "export-manifest.json"  # 存档包包根保留名（v0.3 export/import）
SAVE_IMPORT_MAX_ENTRIES = 200_000     # 导入闸②：条目数上限（zip 炸弹防护）
SAVE_IMPORT_MAX_BYTES = 2 * 1024 ** 3  # 导入闸②：解压总量上限（2GiB）
SAVE_IMPORT_MAX_RATIO = 1000          # 导入闸②：单条压缩比上限
PARADIGM = "agentcrew.paradigm/v1"
CAPS = {"record", "query", "report", "remind", "custom"}
COL_TYPES = {"string", "number", "integer", "boolean", "date"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*\.[a-z0-9][a-z0-9_-]*$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{random.randint(0, 0xffff):04x}"


def ok(payload: dict | None = None, code: int = 0) -> int:
    utf8_console()
    print(json.dumps({"ok": True, **(payload or {})}, ensure_ascii=False, indent=2))
    return code


def fail(error: str, payload: dict | None = None, code: int = 1) -> int:
    utf8_console()
    print(json.dumps({"ok": False, "error": error, **(payload or {})}, ensure_ascii=False, indent=2))
    return code


# ---------- 路径与判定 ----------

def find_repo_root(start: str | None = None) -> str | None:
    """向上找AgentCrew 仓库根：AGENTS.md + 01-master/ + 00-docs/PROTOCOL.md 三者齐备。"""
    d = os.path.abspath(start or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    while True:
        if (
            os.path.isfile(os.path.join(d, "AGENTS.md"))
            and os.path.isdir(os.path.join(d, "01-master"))
            and os.path.isfile(os.path.join(d, "00-docs", "PROTOCOL.md"))
        ):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def detect_mode(agent_dir: str) -> dict:
    """PROTOCOL §2 双模式判定：从助理根向上找总管。
    豁免规则：向上途经 `_standalone` 放归区 → 即使上面有总管仓库也视为 lite
    （放归 = 搬出收编区，哪怕物理上仍在仓库文件夹内）。"""
    d = os.path.abspath(agent_dir)
    passed_standalone = False
    while True:
        if os.path.basename(d) == "_standalone":
            passed_standalone = True
        if (
            os.path.isfile(os.path.join(d, "AGENTS.md"))
            and os.path.isdir(os.path.join(d, "01-master"))
            and os.path.isfile(os.path.join(d, "00-docs", "PROTOCOL.md"))
            and os.path.abspath(d) != os.path.abspath(agent_dir)
            and not passed_standalone
        ):
            return {"mode": "managed", "master_root": d}
        parent = os.path.dirname(d)
        if parent == d:
            return {"mode": "lite", "master_root": None}
        d = parent


def save_root(repo_root: str) -> str:
    """存档根：环境变量 AGENTCREW_SAVE 可整体外置（如 ../MySaves/crew1），否则 <仓库>/_save。
    存档 = 一切部署后的增量信息；删除存档 = 项目回到原点。"""
    env = os.environ.get("AGENTCREW_SAVE")
    if env:
        return os.path.abspath(env)
    return p(repo_root, SAVE_DIRNAME)


def save_manifest_path(repo_root: str) -> str:
    return p(save_root(repo_root), "save.json")


def load_save_manifest(repo_root: str) -> dict | None:
    mp = save_manifest_path(repo_root)
    if not os.path.isfile(mp):
        return None
    try:
        return read_json(mp)
    except json.JSONDecodeError:
        return {"save_version": -1, "_corrupt": True}


def ensure_save(repo_root: str, agents: list[str] | None = None) -> str:
    """确保存档存在（新游戏建档）：save.json + master 骨架 + 各顾问切片骨架。幂等。"""
    sr = save_root(repo_root)
    for sub in ("master", "master/inbox-master", "master/trash", "agents"):
        os.makedirs(p(sr, *sub.split("/")), exist_ok=True)
    mp = save_manifest_path(repo_root)
    if not os.path.isfile(mp):
        atomic_write_json(mp, {"save_version": SAVE_VERSION_CURRENT,
                               "created_at": now_iso(), "agents": {}})
    for aid in agents or []:
        os.makedirs(p(sr, "agents", aid, "data"), exist_ok=True)
    return sr


def master_save_files(repo_root: str) -> dict[str, str]:
    """总管侧源数据文件 → 存档区物理路径。"""
    sr = save_root(repo_root)
    return {
        "profile": p(sr, "master", "profile.json"),
        "registry": p(sr, "master", "registry.json"),
        "authorizations": p(sr, "master", "authorizations.jsonl"),
        "releases": p(sr, "master", "releases.jsonl"),
        "inbox_master": p(sr, "master", "inbox-master"),
        "trash": p(sr, "master", "trash"),
    }


def agent_save_dir(repo_root: str, agent_id: str) -> str:
    """托管模式下某顾问的存档切片（data/inbox/outbox/dashboard 都在其中）。"""
    return p(save_root(repo_root), "agents", agent_id)


def agent_save_dir_lite(agent_dir: str) -> str:
    """lite 独立助理自己的存档（自包含公理：档随助理走）。"""
    return p(agent_dir, SAVE_DIRNAME)


def resolve_agent_save(agent_dir: str) -> str:
    """助理工具用：向上找实例根——找到=托管（实例存档切片）；没有=lite（自带 _save）。"""
    d = os.path.abspath(agent_dir)
    passed_standalone = False
    while True:
        if os.path.basename(d) == "_standalone":
            passed_standalone = True
        if (not passed_standalone
                and os.path.isfile(os.path.join(d, "AGENTS.md"))
                and os.path.isdir(os.path.join(d, "01-master"))
                and os.path.isfile(os.path.join(d, "00-docs", "PROTOCOL.md"))
                and os.path.abspath(d) != os.path.abspath(agent_dir)):
            return agent_save_dir(d, os.path.basename(os.path.abspath(agent_dir)))
        parent = os.path.dirname(d)
        if parent == d:
            return agent_save_dir_lite(agent_dir)
        d = parent


def scan_stray_increment(root: str) -> list[str]:
    """确定性监控（不靠 LLM）：扫描程序区里不该存在的增量数据。
    规则=存档架构铁律的物理化：02-agents/*/{data,inbox,outbox} 与 dashboard/data.*、
    01-master 实例态文件——出现即违规。返回违规相对路径列表（空=干净）。"""
    stray: list[str] = []
    agdir = p(root, "02-agents")
    if os.path.isdir(agdir):
        for d in sorted(os.listdir(agdir)):
            adir = p(agdir, d)
            if not os.path.isdir(adir):
                continue
            for sub in ("data", "inbox", "outbox"):
                sdir = p(adir, sub)
                if os.path.isdir(sdir):
                    stray += [f"02-agents/{d}/{sub}/{n}" for n in os.listdir(sdir)
                              if n != ".gitkeep" and not (sub == "inbox" and n == "done")]
            for n in ("data.js", "data.json"):
                if os.path.isfile(p(adir, "dashboard", n)):
                    stray.append(f"02-agents/{d}/dashboard/{n}")
    for banned in ("profile.json", "registry.json", "authorizations.jsonl",
                   "releases.jsonl", "inbox-master", "trash"):
        if os.path.exists(p(root, "01-master", banned)):
            stray.append(f"01-master/{banned}")
    return stray


def p(root: str, *parts: str) -> str:
    return os.path.join(root, *parts)


# ---------- 原子写 ----------

def atomic_write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def atomic_write_json(path: str, obj) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------- JSONL ----------

def read_jsonl(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append({"_corrupt": line})
    return rows


def append_jsonl(path: str, row: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def row_id() -> str:
    return f"r-{datetime.now().strftime('%Y%m%d%H%M%S')}-{random.randint(0, 0xffff):04x}"


# ---------- 信箱 ----------

def ensure_agent_skeleton(agent_dir: str, repo_root: str | None = None) -> None:
    """建顾问的【存档切片】骨架（data/inbox/outbox）。物理位置：
    托管=实例 _save/agents/<id>/；lite=<助理>/_save/。程序区不再放任何增量数据。
    tools 子目录（程序区）仅在缺失时补建。
    root 解析以 agent_dir 为准（向上找它所属的实例根），而非本库所在仓库——
    否则给临时目录建骨架会把临时目录名写成实例存档切片（adopt zip/git 曾踩此坑）。"""
    root = repo_root or find_repo_root(agent_dir)
    if root:
        sd = agent_save_dir(root, os.path.basename(os.path.abspath(agent_dir)))
    else:
        sd = agent_save_dir_lite(agent_dir)
        os.makedirs(p(agent_dir, "tools"), exist_ok=True)
    for sub in ("data", "inbox", "inbox/done", "outbox", "dashboard"):
        os.makedirs(p(sd, *sub.split("/")), exist_ok=True)
    open(p(sd, "data", ".gitkeep"), "a").close()
    open(p(sd, "inbox", "done", ".gitkeep"), "a").close()


def agent_mailbox_dir(agent_dir: str, box: str) -> str:
    """顾问信箱（逻辑概念）的物理路径：托管=存档切片；lite=自带 _save。
    box ∈ inbox|outbox。"""
    root = find_repo_root()
    if root and detect_mode(agent_dir)["mode"] == "managed":
        return p(agent_save_dir(root, os.path.basename(os.path.abspath(agent_dir))), box)
    return p(agent_save_dir_lite(agent_dir), box)


def inbox_tasks(agent_dir: str) -> list[str]:
    d = agent_mailbox_dir(agent_dir, "inbox")
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.endswith(".json") and not f.endswith(".tmp"))


def outbox_receipts(agent_dir: str) -> list[str]:
    d = agent_mailbox_dir(agent_dir, "outbox")
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.endswith(".json"))


# ---------- registry ----------

def registry_path(root: str) -> str:
    return master_save_files(root)["registry"]  # 存档区（增量信息不进程序区）


def load_registry(root: str) -> dict:
    rp = registry_path(root)
    if not os.path.isfile(rp):
        return {"$schema": "agentcrew.registry/v1", "master_instance": "", "adopted": []}
    return read_json(rp)


def save_registry(root: str, reg: dict) -> None:
    atomic_write_json(registry_path(root), reg)


def registry_find(reg: dict, agent_id: str) -> dict | None:
    for e in reg.get("adopted", []):
        if e.get("id") == agent_id:
            return e
    return None


# ---------- manifest 校验（PARADIGM §2） ----------

def load_manifest(agent_dir: str) -> dict:
    return read_json(p(agent_dir, "manifest.json"))


def validate_manifest(m: dict, agent_dir: str | None = None) -> tuple[list[str], list[str]]:
    """返回 (errors, warnings)。errors 阻断收编；warnings 不阻断但 doctor 可见。
    设计取向（见 RESEARCH.md：极简必填+软性质量压）：必填只保留身份与安全相关，
    routing_examples/author/license 等质量字段缺失降为警告。"""
    errs: list[str] = []
    warns: list[str] = []

    def need(key: str):
        if key not in m or m[key] in (None, "", [], {}):
            errs.append(f"缺少必填字段 {key}")

    for k in ("protocol", "paradigm", "id", "name", "version", "description", "domain"):
        need(k)
    if errs:
        return errs, warns

    for key, prefix in (("protocol", "agentcrew.protocol/"), ("paradigm", "agentcrew.paradigm/")):
        v = str(m[key])
        if not v.startswith(prefix):
            errs.append(f"{key} 应以 {prefix} 开头，现在是 {v}")
        elif v[len(prefix):].split(".")[0] != "v1":
            errs.append(f"{key} 主版本不兼容（需 v1）：{v}")

    if not ID_RE.match(str(m["id"])):
        errs.append(f"id 命名不合法（需 <作者>.<名字> 小写）：{m['id']}")

    caps = m.get("capabilities")
    if not caps:
        warns.append("capabilities 未填（默认按 record/query 处理）")
    else:
        bad = [c for c in caps if c not in CAPS]
        if bad:
            errs.append(f"capabilities 含未知项：{bad}")

    rex = m.get("routing_examples") or {}
    pos, neg = rex.get("positive") or [], rex.get("negative") or []
    if len(pos) < 3 or len(neg) < 3:
        warns.append(f"routing_examples 正例 {len(pos)} / 反例 {len(neg)}，建议各≥3（路由质量生命线）")

    if not m.get("author"):
        warns.append("author 未填")
    if not m.get("license"):
        warns.append("license 未填")

    for t in m.get("tables") or []:
        tname = t.get("name", "?")
        if not re.match(r"^[a-z][a-z0-9_]*$", tname):
            errs.append(f"表名不合法：{tname}")
        for c in t.get("columns") or []:
            if c.get("type") not in COL_TYPES:
                errs.append(f"表 {tname} 字段 {c.get('name')} 类型非法：{c.get('type')}")

    for r in m.get("reminders") or []:
        if not r.get("rule"):
            errs.append(f"提醒 {r.get('name')} 缺 rule（人类语言）")
        if r.get("default_time") and not TIME_RE.match(str(r["default_time"])):
            errs.append(f"提醒 {r.get('name')} default_time 需 HH:MM")

    dash = m.get("dashboard") or {}
    if dash.get("main") and agent_dir and not os.path.isfile(p(agent_dir, dash["main"])):
        errs.append(f"dashboard.main 文件不存在：{dash['main']}")

    return errs, warns


# ---------- 工具自检 ----------

def selfcheck_tool(agent_dir: str, tool_rel: str,
                   env_extra: dict[str, str] | None = None) -> tuple[bool, str]:
    agent_dir = os.path.abspath(agent_dir)
    tool = p(agent_dir, tool_rel.replace("/", os.sep))
    if not os.path.isfile(tool):
        return False, f"工具不存在：{tool_rel}"
    try:
        r = subprocess.run(
            [sys.executable, tool, "--selfcheck"],
            capture_output=True, text=True, timeout=60, cwd=agent_dir,
            encoding="utf-8", errors="replace",
            env={**os.environ, **(env_extra or {})},
        )
        if r.returncode != 0:
            return False, f"selfcheck 退出码 {r.returncode}: {(r.stderr or r.stdout).strip()[:300]}"
        try:
            out = json.loads(r.stdout)
            if not out.get("ok"):
                return False, f"selfcheck 报告失败：{out.get('error', '?')}"
        except json.JSONDecodeError:
            return False, f"selfcheck 输出不是 JSON：{r.stdout.strip()[:120]}"
        return True, "ok"
    except subprocess.TimeoutExpired:
        return False, "selfcheck 超时"
    except Exception as e:  # noqa: BLE001
        return False, f"selfcheck 异常：{e}"


# ---------- zip / git 解包（GBK 文件名兜底） ----------

def _fix_zip_name(name: str) -> str:
    if any(0x80 <= ord(ch) <= 0xFF for ch in name):
        try:
            return name.encode("cp437").decode("gbk")
        except Exception:  # noqa: BLE001
            return name
    return name


def extract_zip(zip_path: str, dest: str) -> None:
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = _fix_zip_name(info.filename)
            target = os.path.abspath(os.path.join(dest, name))
            if os.path.commonpath([os.path.abspath(dest), target]) != os.path.abspath(dest):
                continue  # 防 zip 路径穿越（含兄弟目录前缀）
            if info.is_dir() or name.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(z.read(info))


def clone_git(url: str, dest: str) -> tuple[bool, str]:
    if shutil.which("git") is None:
        return False, "本机未安装 git，无法用 git 地址安装"
    r = subprocess.run(["git", "clone", "--depth", "1", url, dest],
                       capture_output=True, text=True, timeout=300)
    return (r.returncode == 0, (r.stderr or r.stdout).strip()[:300])


def find_manifest_dir(src: str) -> str | None:
    """在解包目录的 0-2 层内找含 manifest.json 的目录。"""
    if os.path.isfile(p(src, "manifest.json")):
        return src
    try:
        for name in sorted(os.listdir(src)):
            sub = p(src, name)
            if os.path.isdir(sub) and name not in (".git", "__MACOSX"):
                if os.path.isfile(p(sub, "manifest.json")):
                    return sub
                for name2 in sorted(os.listdir(sub)):
                    sub2 = p(sub, name2)
                    if os.path.isdir(sub2) and os.path.isfile(p(sub2, "manifest.json")):
                        return sub2
    except OSError:
        pass
    return None


# ---------- 存档单文件包（export / import，v0.3） ----------
#
# 设计定稿：07-ops/2026-09-26-存档导出导入设计定稿.md
# 铁律：导入=删除类操作——无备份不替换、无表态不放行（CLI --force / 面板两步确认）。
# 循环导入禁令：本节不得 import migrate_save（migrate_save.py 反向 import 本库，
# 顶层互导成环）——阶梯迁移由 save.py 壳层在持锁下函数内延迟 import 执行。


class SaveGateError(Exception):
    """导出/导入闸拒绝（业务失败）：code 即 reason_code，payload 附带定位信息。"""

    def __init__(self, code: str, message: str, payload: dict | None = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


def _valid_save_version(v) -> bool:
    """save_version 合法形态：int（不含 bool）且 ≥1。
    缺键/None/0/负数/浮点/字符串皆非法——必须在换位前拦截（迁移位于 rmtree 旧档之后）。"""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


@contextlib.contextmanager
def save_lock(repo_root: str):
    """存档导出/导入互斥锁（--dry-run 不取锁）。sibling 锁 <save_root>.lock：
    O_CREAT|O_EXCL 独占创建并写 pid+时间戳；>600s 视陈旧锁可接管（崩溃残留自愈）。
    锁在存档根之外——不被打包、不随换位销毁。"""
    lock_path = save_root(repo_root) + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = None
    for _ in range(2):  # 第二次尝试仅用于陈旧锁接管
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                stale = time.time() - os.path.getmtime(lock_path) > 600
            except OSError:
                stale = False
            if not stale:
                raise SaveGateError("save_busy", f"存档正被其他进程占用（{lock_path}）；确认无并发操作后可手动删锁") 
            try:
                os.remove(lock_path)
            except OSError as e:
                raise SaveGateError("save_busy", f"陈旧锁无法接管（{lock_path}）：{e}") from e
        except OSError as e:
            raise SaveGateError("save_busy", f"锁文件创建失败：{e}") from e
    if fd is None:
        raise SaveGateError("save_busy", f"存档正被其他进程占用（{lock_path}）")
    try:
        os.write(fd, f"{os.getpid()} {now_iso()}\n".encode("utf-8"))
    finally:
        os.close(fd)
    try:
        yield lock_path
    finally:
        try:
            os.remove(lock_path)
        except OSError:
            pass


def _dedup_path(path: str) -> str:
    """目标已存在则追加 -N 序号（crew-save-<ts>-2.asave）——绝不静默覆盖。"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{base}-{n}{ext}"):
        n += 1
    return f"{base}-{n}{ext}"


def _iter_save_files(sr: str):
    """yield (绝对路径, 条目名)：条目名一律相对存档根、'/' 分隔、无盘符无 '..'。"""
    for base, _dirs, files in os.walk(sr):
        for fn in files:
            if fn.endswith(".part.asave"):
                continue  # 兜底：自家打包中间产物绝不入包
            fp = os.path.join(base, fn)
            yield fp, os.path.relpath(fp, sr).replace(os.sep, "/")


def _count_nonempty_lines(path: str) -> int:
    n = 0
    try:
        with open(path, "rb") as f:
            for line in f:
                if line.strip():
                    n += 1
    except OSError:
        pass
    return n


def scan_save_stats(repo_root: str) -> dict:
    """存档统计：文件条目数/字节总数/各助理 data/*.jsonl 非空行合计。
    export manifest、--dry-run 报告与面板预览共用同一口径。"""
    sr = save_root(repo_root)
    entries = 0
    total = 0
    for base, _dirs, files in os.walk(sr):
        for fn in files:
            if fn.endswith(".part.asave"):
                continue  # 与 _iter_save_files 同口径：中间产物不计入统计/manifest
            entries += 1
            try:
                total += os.path.getsize(os.path.join(base, fn))
            except OSError:
                pass
    agents: dict[str, dict] = {}
    rows_total = 0
    adir = p(sr, "agents")
    if os.path.isdir(adir):
        for aid in sorted(os.listdir(adir)):
            aroot = p(adir, aid)
            if not os.path.isdir(aroot):
                continue
            files_n = 0
            tables: dict[str, int] = {}
            for base, _dirs, files in os.walk(aroot):
                for fn in files:
                    files_n += 1
                    fp = os.path.join(base, fn)
                    if os.path.dirname(fp) == p(aroot, "data") and fn.endswith(".jsonl"):
                        rows = _count_nonempty_lines(fp)
                        tables[fn[:-len(".jsonl")]] = rows
                        rows_total += rows
            agents[aid] = {"tables": tables, "files": files_n}
    return {"entries": entries, "bytes": total, "rows_total": rows_total, "agents": agents}


def _check_exportable(repo_root: str) -> dict:
    """导出前置闸：返回 save.json 内容；不满足即 SaveGateError。
    损坏源档产出的包必然炸在导入端——必须在源头拒绝。"""
    sr = save_root(repo_root)
    if not os.path.isdir(sr):
        raise SaveGateError("no_save", f"存档根不存在：{sr}")
    if os.path.isfile(p(sr, MANIFEST_NAME)):
        raise SaveGateError("manifest_name_conflict",
                            f"存档根顶层已存在保留名 {MANIFEST_NAME}，拒绝覆盖用户文件")
    mani = load_save_manifest(repo_root)
    if mani is None:
        raise SaveGateError("corrupt_save", "save.json 缺失——存档不完整，拒绝导出")
    if mani.get("_corrupt"):
        raise SaveGateError("corrupt_save", "save.json 损坏（JSON 解析失败），拒绝导出")
    if not _valid_save_version(mani.get("save_version")):
        raise SaveGateError("corrupt_save",
                            f"save.json 的 save_version 非法：{mani.get('save_version')!r}（需 ≥1 整数）")
    return mani


def _pack_target(repo_root: str, out: str | None, stem: str) -> str:
    """决定包落点：out=已有目录 → 目录内默认名；out=文件路径 → 原样用；缺省 = 存档根上级目录。
    目标已存在 → 追加 -N 序号，绝不静默覆盖。
    落点守卫：--out 指进存档根（含子目录）→ .part.asave 中间产物会被自家 os.walk
    打进包体，产出 ok:true 的废包——业务拒绝（默认落点=存档根上级目录）。"""
    default_name = f"{stem}-{datetime.now():%Y%m%d-%H%M%S}.asave"
    if out:
        outp = os.path.abspath(out)
        sr = os.path.abspath(save_root(repo_root))
        try:
            inside = outp == sr or os.path.commonpath([outp, sr]) == sr
        except ValueError:  # 异盘：必然在存档根之外
            inside = False
        if inside:
            raise SaveGateError("out_inside_save",
                                f"--out 落在存档根内部（{outp}），会被自家打包吞进包体——"
                                "请指向存档根之外（缺省落点=存档根上级目录）",
                                {"out": outp})
        if os.path.isdir(outp):
            outp = os.path.join(outp, default_name)
    else:
        outp = os.path.join(os.path.dirname(save_root(repo_root)), default_name)
    return _dedup_path(outp)


def export_preview(repo_root: str, out: str | None = None) -> dict:
    """export --dry-run：零写入（不取锁、不落盘），只报将打包的内容与大小。"""
    mani = _check_exportable(repo_root)
    stats = scan_save_stats(repo_root)
    return {"would_file": _pack_target(repo_root, out, "crew-save"),
            "entries": stats["entries"], "bytes": stats["bytes"],
            "rows_total": stats["rows_total"], "save_version": mani.get("save_version"),
            "agents": stats["agents"]}


def pack_save(repo_root: str, out: str | None = None, extra_manifest: dict | None = None) -> dict:
    """把整个存档根打包为单文件 .asave（标准 zip + 包根保留名 export-manifest.json）。
    export 与导入前自动备份共用本函数，杜绝两套打包逻辑漂移。
    范围=save_root() 全部内容（master + agents/ 切片）；lite 助理自带 _save 不在包内。
    返回 {file, entries, bytes, rows_total, save_version, agents, zip_bytes}。"""
    mani = _check_exportable(repo_root)
    stats = scan_save_stats(repo_root)
    stem = "crew-save-auto-backup" if (extra_manifest or {}).get("auto_backup") else "crew-save"
    final = _pack_target(repo_root, out, stem)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    manifest = {
        "kind": "agentcrew.save.export",
        "manifest_version": 1,
        "exported_at": now_iso(),
        "tool": "save.py export",
        "save_version": mani.get("save_version"),
        "program_save_version": SAVE_VERSION_CURRENT,
        "origin_external": bool(os.environ.get("AGENTCREW_SAVE")),  # 只记布尔不记路径：包不泄漏本机布局
        "entries": stats["entries"],
        "bytes": stats["bytes"],
        "rows_total": stats["rows_total"],
        "agents": stats["agents"],
    }
    manifest.update(extra_manifest or {})
    part = final + ".part.asave"  # 中间产物含个人数据 → 命名命中 .gitignore 的 *.asave
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
            for absp, rel in _iter_save_files(save_root(repo_root)):
                z.write(absp, rel)  # 非 ASCII 条目名由 zipfile 自动带 UTF-8 旗标
            z.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        os.replace(part, final)
    finally:
        if os.path.isfile(part):
            try:
                os.remove(part)
            except OSError:
                pass
    return {"file": final, "bytes": stats["bytes"], "entries": stats["entries"],
            "rows_total": stats["rows_total"], "save_version": mani.get("save_version"),
            "agents": stats["agents"], "zip_bytes": os.path.getsize(final)}


def _unsafe_entry_name(name: str) -> bool:
    """闸② 条目名消毒：拒绝空名/绝对路径/盘符/反斜杠/「..」段（防 zip-slip）。"""
    if not name or name.startswith("/") or name.startswith("\\"):
        return True
    if "\\" in name:
        return True
    if re.match(r"^[A-Za-z]:", name):
        return True
    return any(seg == ".." for seg in name.split("/"))


def _entry_limit_reason(info) -> str | None:
    """闸② 单条限检（zip 炸弹防护）。compress_size=0 配非零 file_size 视为直接超限——
    绝不裸除（防 ZeroDivisionError）；file_size=0（空文件，zip 常记 compress_size=0）跳过比值检查。"""
    if info.file_size == 0:
        return None
    if info.compress_size == 0:
        return "too_large"
    if info.file_size / info.compress_size > SAVE_IMPORT_MAX_RATIO:
        return "too_large"
    return None


def planned_backup_path(repo_root: str) -> str | None:
    """导入 --dry-run 预演：目标存档非空 → 将生成的自动备份路径；空/不存在 → None。"""
    sr = save_root(repo_root)
    if not (os.path.isdir(sr) and os.listdir(sr)):
        return None
    return _pack_target(repo_root, None, "crew-save-auto-backup")


def inspect_package(pkg_path: str, repo_root: str | None = None, force: bool = False) -> dict:
    """导入闸①②③（纯读包，零写入）：zip 结构与 manifest → 逐条路径消毒与限检 → 版本合法性与比对。
    任何拒绝抛 SaveGateError（code=reason_code）；force=True 仅放行闸③「包比程序新」（降级安装）。
    返回预览信息 dict（entries/bytes/save_version/rows_total/agents/will_migrate/will_backup/...）。"""
    root = repo_root or find_repo_root() or "."
    sr = os.path.abspath(save_root(root))
    # ---- 闸①：zip 结构与 manifest ----
    try:
        z = zipfile.ZipFile(pkg_path)
    except (zipfile.BadZipFile, OSError) as e:
        raise SaveGateError("bad_zip", f"不是有效的 zip 存档包：{e}") from e
    def _read_member(name: str) -> bytes:
        try:
            return z.read(name)
        except (zipfile.BadZipFile, OSError) as e:  # OSError：截断包可致 seek EINVAL
            raise SaveGateError("bad_zip", f"包内 {name} 读取失败（包损坏）：{e}") from e

    with z:
        try:
            crc_bad = z.testzip()  # CRC 全检不过会抛 BadZipFile（不止返回坏名）
        except (zipfile.BadZipFile, OSError) as e:
            raise SaveGateError("bad_zip", f"zip CRC 全检失败：包已损坏（{e}）") from e
        if crc_bad is not None:
            raise SaveGateError("bad_zip", "zip CRC 全检失败：包已损坏")
        infos = z.infolist()
        # 消毒一律看 orig_filename（原始存储名）：读侧 ZipInfo.__init__ 会把 Windows
        # 反斜杠规范成正斜杠，只有 orig_filename 保留原样——gate 才拦得住 foreign 包
        names = {_fix_zip_name(i.orig_filename) for i in infos}
        if MANIFEST_NAME not in names:
            raise SaveGateError("no_manifest", f"包内缺 {MANIFEST_NAME}——不是 AgentCrew 存档包")
        try:
            manifest = json.loads(_read_member(MANIFEST_NAME).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise SaveGateError("bad_manifest", f"{MANIFEST_NAME} 不是合法 JSON：{e}") from e
        if not isinstance(manifest, dict) or manifest.get("kind") != "agentcrew.save.export":
            raise SaveGateError("bad_manifest", "manifest kind 不符——不是 agentcrew.save.export")
        if manifest.get("manifest_version") != 1:
            raise SaveGateError("bad_manifest",
                                f"manifest_version 不支持：{manifest.get('manifest_version')!r}")
        # ---- 闸②：逐条消毒 + 限检（超限即断不续解） ----
        n_files = 0
        total = 0
        for info in infos:
            name = _fix_zip_name(info.orig_filename)
            if _unsafe_entry_name(name):
                raise SaveGateError("unsafe_entry", f"条目路径不安全，已拒绝：{name!r}", {"entry": name})
            target = os.path.abspath(os.path.join(sr, name))
            if os.path.commonpath([sr, target]) != sr:
                raise SaveGateError("unsafe_entry", f"条目逃逸存档根，已拒绝：{name!r}", {"entry": name})
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise SaveGateError("unsafe_entry", f"条目是符号链接，已拒绝：{name!r}", {"entry": name})
            reason = _entry_limit_reason(info)
            if reason:
                raise SaveGateError(reason,
                                    f"条目超限（file_size={info.file_size}, compress_size={info.compress_size}）：{name}",
                                    {"entry": name})
            if name == MANIFEST_NAME:
                continue  # 包根保留名不计入条目数/字节（与 pack_save 的 manifest 口径一致）
            total += info.file_size
            if total > SAVE_IMPORT_MAX_BYTES:
                raise SaveGateError("too_large", f"解压总量超上限（>{SAVE_IMPORT_MAX_BYTES} 字节，zip 炸弹防护）")
            if not info.is_dir() and not name.endswith("/"):
                n_files += 1
        if n_files > SAVE_IMPORT_MAX_ENTRIES:
            raise SaveGateError("too_many_entries", f"条目数 {n_files} 超上限 {SAVE_IMPORT_MAX_ENTRIES}")
        m_entries, m_bytes = manifest.get("entries"), manifest.get("bytes")
        if isinstance(m_entries, int) and m_entries != n_files:
            raise SaveGateError("manifest_mismatch",
                                f"manifest.entries={m_entries} 与包内实际条目 {n_files} 不符（疑被篡改）")
        if isinstance(m_bytes, int) and m_bytes != total:
            raise SaveGateError("manifest_mismatch",
                                f"manifest.bytes={m_bytes} 与包内实际字节 {total} 不符（疑被篡改）")
        # ---- 闸③：版本合法性（换位前完成，任何异常不得带进换位后）与比对 ----
        if "save.json" not in names:
            raise SaveGateError("bad_manifest", "包内缺 save.json——不是完整存档包")
        try:
            sv = json.loads(_read_member("save.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise SaveGateError("bad_manifest", f"包内 save.json 不是合法 JSON：{e}") from e
        v = sv.get("save_version") if isinstance(sv, dict) else None
        if not _valid_save_version(v):
            raise SaveGateError("bad_manifest",
                                f"包内 save.json 的 save_version 非法：{v!r}（缺键/0/负数/非整数一律拒绝）")
        if "save_version" in manifest and manifest.get("save_version") != v:
            raise SaveGateError("manifest_mismatch",
                                f"manifest.save_version={manifest.get('save_version')!r} "
                                f"与包内 save.json={v!r} 不符")
        if not force and v > SAVE_VERSION_CURRENT:
            raise SaveGateError("save_newer_than_program",
                                f"包内存档 v{v} 比程序支持 v{SAVE_VERSION_CURRENT} 新——拒绝降级安装"
                                f"（--force 可强装，风险自担）",
                                {"package_v": v, "program_v": SAVE_VERSION_CURRENT})
    warnings: list[str] = []
    if v < SAVE_VERSION_CURRENT:
        warnings.append(f"包版本 v{v} 旧于程序 v{SAVE_VERSION_CURRENT}：导入后将自动阶梯迁移")
    will_backup = bool(os.path.isdir(sr) and os.listdir(sr))  # 宁多勿漏：刚 reset 的空档含 save.json 也算非空
    return {
        "file": os.path.abspath(pkg_path),
        "entries": n_files,
        "bytes": total,
        "save_version": v,
        "program_save_version": SAVE_VERSION_CURRENT,
        "rows_total": manifest.get("rows_total") if isinstance(manifest.get("rows_total"), int) else 0,
        "agents": manifest.get("agents") if isinstance(manifest.get("agents"), dict) else {},
        "will_migrate": list(range(v + 1, SAVE_VERSION_CURRENT + 1)) if v < SAVE_VERSION_CURRENT else [],
        "will_backup": will_backup,
        "auto_backup_name": planned_backup_path(root) if will_backup else None,
        "warnings": warnings,
        "manifest": manifest,
    }


def _force_banner(package_v: int, program_v: int) -> None:
    utf8_console()
    bar = "⚠" * 24
    sys.stderr.write(f"{bar}\n")
    sys.stderr.write(f"⚠ 强制安装：包内存档 v{package_v} 比程序支持 v{program_v} 更新——"
                     f"旧程序可能无法读取新版结构，数据兼容风险自担\n")
    sys.stderr.write(f"{bar}\n")


def _unpack_verified(z, dest: str, manifest: dict) -> tuple[int, int]:
    """把包内条目解到 dest（条目已过闸②，此处限速读+复算条目数/总字节对 manifest）。"""
    count = 0
    total = 0
    dest_abs = os.path.abspath(dest)
    for info in z.infolist():
        name = _fix_zip_name(info.orig_filename)  # 原始存储名（读侧 filename 已被规范化）
        if name == MANIFEST_NAME:
            continue
        if info.is_dir() or name.endswith("/"):
            os.makedirs(os.path.join(dest, name), exist_ok=True)
            continue
        target = os.path.abspath(os.path.join(dest, name))
        if os.path.commonpath([dest_abs, target]) != dest_abs:
            raise SaveGateError("unsafe_entry", f"条目逃逸存档根，已拒绝：{name!r}", {"entry": name})
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            src_f = z.open(info)
        except (zipfile.BadZipFile, OSError) as e:  # OSError：截断包可致 seek EINVAL
            raise SaveGateError("bad_zip", f"条目读取失败（包损坏）：{name}：{e}") from e
        got = 0
        # 流式直写：单条目不再整体攒进内存（敌意包可撑 ~4GiB 峰值）；压缩比/总量闸照常生效
        with src_f, open(target, "wb") as g:
            while True:
                try:
                    chunk = src_f.read(min(1 << 20, info.file_size + 1 - got))
                except (zipfile.BadZipFile, OSError) as e:  # OSError：截断包可致 seek EINVAL
                    raise SaveGateError("bad_zip", f"条目读取失败（包损坏）：{name}：{e}") from e
                if not chunk:
                    break
                g.write(chunk)
                got += len(chunk)
                if got > info.file_size:
                    raise SaveGateError("too_large",
                                        f"条目实际字节数超过声明（元数据谎报）：{name}",
                                        {"entry": name})
        if got < info.file_size:
            raise SaveGateError("bad_zip", f"条目实际字节数不足声明（包损坏）：{name}", {"entry": name})
        total += got
        if total > SAVE_IMPORT_MAX_BYTES:
            raise SaveGateError("too_large", f"解压总量超上限（>{SAVE_IMPORT_MAX_BYTES} 字节，zip 炸弹防护）")
        count += 1
    m_entries, m_bytes = manifest.get("entries"), manifest.get("bytes")
    if isinstance(m_entries, int) and count != m_entries:
        raise SaveGateError("manifest_mismatch", f"解包条目数 {count} ≠ manifest.entries {m_entries}")
    if isinstance(m_bytes, int) and total != m_bytes:
        raise SaveGateError("manifest_mismatch", f"解包字节总数 {total} ≠ manifest.bytes {m_bytes}")
    return count, total


def import_save(repo_root: str, pkg_path: str, force: bool = False) -> dict:
    """导入存档包：闸①②③④ + 原子换位（无备份不替换）。迁移无关——
    阶梯迁移由 save.py 壳层在持锁下执行（本库禁 import migrate_save，防循环导入）。
    返回 {imported_entries, backup, save_version, package_v, program_v, forced_downgrade, bytes}。"""
    sr = save_root(repo_root)
    prev = inspect_package(pkg_path, repo_root, force=force)  # 闸①②③
    if force and prev["save_version"] > SAVE_VERSION_CURRENT:
        _force_banner(prev["save_version"], SAVE_VERSION_CURRENT)
    # ---- 闸④：目标存在且非空 → 先自动备份（失败即拒绝替换） ----
    backup = None
    if os.path.isdir(sr) and os.listdir(sr):
        try:
            backup = pack_save(repo_root, extra_manifest={"auto_backup": True,
                                                          "reason": "pre-import-backup"})["file"]
        except SaveGateError as e:
            raise SaveGateError("backup_failed",
                                f"导入前自动备份失败，绝不替换（无备份不替换铁律）：{e}",
                                {"reason": e.code}) from e
    # ---- 换位（原子，全程同盘；目标不存在=首装/跨机迁移路径，无备份合法） ----
    parent = os.path.dirname(sr)
    os.makedirs(parent, exist_ok=True)
    tmp_new = tempfile.mkdtemp(prefix="_save.import-", dir=parent)
    staged_old = None
    try:
        try:
            with zipfile.ZipFile(pkg_path) as z:
                _unpack_verified(z, tmp_new, prev["manifest"])
        except zipfile.BadZipFile as e:
            raise SaveGateError("bad_zip", f"解包时发现包损坏：{e}") from e
        if os.path.isdir(sr):
            staged_old = p(parent, f"{os.path.basename(sr)}.old-{datetime.now():%Y%m%d%H%M%S}")
            os.rename(sr, staged_old)
        try:
            os.rename(tmp_new, sr)
        except OSError:
            if staged_old and not os.path.isdir(sr):
                os.rename(staged_old, sr)  # 回滚：旧档归位
                staged_old = None
            raise
    except SaveGateError:
        shutil.rmtree(tmp_new, ignore_errors=True)
        if staged_old and not os.path.isdir(sr):
            os.rename(staged_old, sr)
        raise
    except OSError as e:
        shutil.rmtree(tmp_new, ignore_errors=True)
        if staged_old and not os.path.isdir(sr):
            os.rename(staged_old, sr)
        raise SaveGateError("swap_failed", f"换位失败，已回滚旧档：{e}",
                            {"rolled_back": True}) from e
    # ---- 成功：清旧档暂存 + 骨架自愈（幂等，不碰内容） ----
    shutil.rmtree(tmp_new, ignore_errors=True)
    if staged_old:
        shutil.rmtree(staged_old, ignore_errors=True)
    ensure_save(repo_root)
    agdir = p(sr, "agents")
    if os.path.isdir(agdir):
        for aid in sorted(os.listdir(agdir)):
            ad = p(agdir, aid)
            if os.path.isdir(ad):
                ensure_agent_skeleton(ad, repo_root)
    return {"imported_entries": prev["entries"], "backup": backup,
            "save_version": prev["save_version"],
            "package_v": prev["save_version"], "program_v": prev["program_save_version"],
            "forced_downgrade": bool(force and prev["save_version"] > SAVE_VERSION_CURRENT),
            "bytes": prev["bytes"]}
