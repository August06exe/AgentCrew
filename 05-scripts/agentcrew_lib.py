#!/usr/bin/env python3
"""agentcrew_lib.py — AgentCrew 总管公共库（仅标准库）。

被 05-scripts 下所有工具与测试复用。约定见 00-docs/PROTOCOL.md。
所有输出 JSON 的工具：成功 {"ok": true, ...}，失败 {"ok": false, "error": ...}。
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime

PROTOCOL = "agentcrew.protocol/v1"
SAVE_DIRNAME = "_save"
SAVE_VERSION_CURRENT = 1  # 程序侧存档结构版本；改结构必须升版并附 migrate_save.py 阶梯
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
    tools 子目录（程序区）仅在缺失时补建。"""
    root = repo_root or find_repo_root()
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

def selfcheck_tool(agent_dir: str, tool_rel: str) -> tuple[bool, str]:
    agent_dir = os.path.abspath(agent_dir)
    tool = p(agent_dir, tool_rel.replace("/", os.sep))
    if not os.path.isfile(tool):
        return False, f"工具不存在：{tool_rel}"
    try:
        r = subprocess.run(
            [sys.executable, tool, "--selfcheck"],
            capture_output=True, text=True, timeout=60, cwd=agent_dir,
            encoding="utf-8", errors="replace",
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
