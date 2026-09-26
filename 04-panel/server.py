#!/usr/bin/env python3
"""server.py — 管家面板本地服务（仅标准库，只监听 127.0.0.1）。

页面：/ (全家福) /data.html /install.html /onboarding.html /skin/butler.css
API：
  GET  /api/state                          总览（助理卡片/初始化状态）
  GET  /api/agent/<id>/table/<name>        某助理某表数据
  GET  /api/agent/<id>/export/<name>.csv   导出 CSV（utf-8-sig，Excel 可开）
  POST /api/agent/<id>/delete_row          {table, row_id} → 移入该助理 data/trash
  POST /api/agent/<id>/toggle              active ⇄ paused
  POST /api/install                        {mode: zip|path|git, ...} → 收编
  POST /api/release                        {id} → 放归
  POST /api/save-export                    {} → 存档单文件导出（透传 save.py export）
  POST /api/save-import/preview            {path}|{b64,filename} → 导入预览（闸①②③只读）+ 一次性 token
  POST /api/save-import/confirm            {token} → 确认导入（sha256 复验后透传 save.py import）
  GET  /jump/<token>                       一次性跳转 token（/api/state 发放）→ 刷新该助理看板数据后跳转
静态：/agent-file/<id>/<path…> 各助理文件（含看板）
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PANEL_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PANEL_DIR)
sys.path.insert(0, os.path.join(ROOT, "05-scripts"))
import agentcrew_lib as B  # noqa: E402

PORT = 7530
TABLE_RE = __import__("re").compile(r"^[a-z][a-z0-9_]*$")
INSTALL_PENDING: dict[str, dict] = {}  # token -> {"dir":..., "tmp": bool, "ts": float}
# 存档导入两步确认：token -> {"pkg_path","sha256","ts","staged"}（staged=b64 暂存包路径，用毕即删）
IMPORT_PENDING: dict[str, dict] = {}
IMPORT_TOKEN_TTL = 3600      # 与安装页一致：1 小时过期
IMPORT_TOKEN_MAX = 50        # 与安装页一致：上限 50 丢最旧
# 一次性跳转 token：/jump 会跑子进程写看板（有副作用），凭本页发放的 token 放行，防外站 <img> 直跳
JUMP_PENDING: dict[str, dict] = {}  # token -> {"agent","ts"}


def agent_save(agent_id: str) -> str:
    """该顾问的存档切片（面板读写数据只走这里）。"""
    return B.agent_save_dir(ROOT, agent_id)


def agent_dir(agent_id: str) -> str | None:
    reg = B.load_registry(ROOT)
    e = B.registry_find(reg, agent_id)
    d = B.p(ROOT, e.get("dir", f"02-agents/{agent_id}")) if e else B.p(ROOT, "02-agents", agent_id)
    return d if os.path.isdir(d) else None


def profile_state() -> dict:
    pp = B.master_save_files(ROOT)["profile"]
    if os.path.isfile(pp):
        prof = B.read_json(pp)
        return {"exists": True, "complete": bool(prof.get("onboarding_complete")), "profile": prof}
    return {"exists": False, "complete": False, "profile": None}


def api_state() -> dict:
    reg = B.load_registry(ROOT)
    agents = []
    agdir = B.p(ROOT, "02-agents")
    registered = {e["id"]: e for e in reg.get("adopted", [])}
    if os.path.isdir(agdir):
        for d in sorted(os.listdir(agdir)):
            adir = B.p(agdir, d)
            mp = B.p(adir, "manifest.json")
            if not (os.path.isdir(adir) and os.path.isfile(mp)):
                continue
            try:
                m = B.read_json(mp)
            except json.JSONDecodeError:
                continue
            aid = m.get("id", d)
            e = registered.get(aid, {})
            dash = m.get("dashboard") or {}
            agents.append({
                "id": aid, "name": m.get("name"), "version": m.get("version"),
                "description": m.get("description"),
                "status": e.get("status", "unregistered"),
                "domain": m.get("domain", []),
                "reminders_enabled": e.get("reminders_enabled", True),
                "mode": B.detect_mode(adir)["mode"],
                "dashboard": {"main": dash.get("main"), "up": bool(dash.get("up"))},
                "tables": [t.get("name") for t in m.get("tables") or []],
                "onboarding": m.get("onboarding", []),
            })
    # 一次性跳转 token：随 state 发放、/jump 校验销号（跨站拿不到 token，直跳一律 4xx）
    import secrets
    now = time.time()
    for k in [k for k, v in JUMP_PENDING.items() if now - v["ts"] > 3600]:
        JUMP_PENDING.pop(k, None)
    while len(JUMP_PENDING) >= 50:  # 上限：丢最旧
        JUMP_PENDING.pop(min(JUMP_PENDING, key=lambda k: JUMP_PENDING[k]["ts"]), None)
    jump = {}
    for a in agents:
        t = secrets.token_hex(16)
        JUMP_PENDING[t] = {"agent": a["id"], "ts": now}
        jump[a["id"]] = t
    return {"ok": True, "root": ROOT, "profile": profile_state(), "agents": agents, "jump": jump,
            "save_watch": {"violations": B.scan_stray_increment(ROOT)[:20]}}


def rows_to_csv(table: str, rows: list[dict]) -> str:
    keys: list[str] = []
    for r in rows:
        for k in r.keys():
            if k not in keys:
                keys.append(k)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(keys)
    for r in rows:
        w.writerow(json.dumps(r[k], ensure_ascii=False) if isinstance(r.get(k), (dict, list)) else r.get(k, "") for k in keys)
    return buf.getvalue()


# ---------- 存档备份/恢复（v0.3）----------
# 设计定稿：07-ops/2026-09-26-存档导出导入设计定稿.md。
# 三端点均为模块级函数（可被 06-tests 函数级加载测试），Handler 只委托。
# 双层两步确认：服务端真防线 = preview/confirm 双端点 + 一次性 token + sha256 指纹复验；
# 前端勾选框只是表达层。CLI --force 强装旧/新版本口子只在 save.py，面板不提供。


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _stage_b64(b64: str) -> str:
    """b64 上传的包先落系统临时目录（算 sha256、供 confirm 复用）。
    全量个人数据不滞留 %TEMP%：confirm 无论成败、preview 失败、token 过期/淘汰都删暂存文件。"""
    raw = base64.b64decode(b64)  # binascii.Error → ValueError，由调用方回 400
    fd, tmp = tempfile.mkstemp(prefix="crew-import-staging-", suffix=".asave")
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return tmp


def _remove_best_effort(path: str, tries: int = 3, delay: float = 0.05) -> None:
    """删临时文件，尽力而为：Windows 下杀软/索引器短暂占位会 PermissionError，
    短重试 3×50ms 后放弃（不向上抛，成败皆不阻塞主流程）。"""
    for i in range(tries):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if i < tries - 1:
                time.sleep(delay)


def _drop_import_token(token: str) -> dict | None:
    v = IMPORT_PENDING.pop(token, None)
    if v and v.get("staged"):
        _remove_best_effort(v["staged"])
    return v


def _prune_import_tokens(now: float) -> None:
    for k in [k for k, v in IMPORT_PENDING.items() if now - v["ts"] > IMPORT_TOKEN_TTL]:
        _drop_import_token(k)
    while len(IMPORT_PENDING) >= IMPORT_TOKEN_MAX:  # 上限：丢最旧（暂存文件一并删）
        oldest = min(IMPORT_PENDING, key=lambda k: IMPORT_PENDING[k]["ts"])
        _drop_import_token(oldest)


def _drop_install_token(token: str) -> dict | None:
    """对称导入侧 _drop_import_token：token 逐出（过期/超限/消费）时连带 rmtree 解包临时目录。"""
    v = INSTALL_PENDING.pop(token, None)
    if v and v.get("tmp_root"):
        shutil.rmtree(v["tmp_root"], ignore_errors=True)
    return v


def api_save_export() -> dict:
    """一键导出：透传 save.py export 的 stdout JSON（含绝对路径与个人数据提醒）。"""
    try:
        r = subprocess.run([sys.executable, B.p(ROOT, "05-scripts", "save.py"), "export"],
                           capture_output=True, text=True, encoding="utf-8", timeout=300)
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": (r.stderr or r.stdout or "save.py export 无输出")[:300]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "导出超时（300s）——大档请改用命令行 python 05-scripts/save.py export"}


def api_save_import_preview(body: dict) -> tuple[dict, int]:
    """第一段：闸①②③只读预览（inspect_package，零写入），发一次性 token。
    path=本机路径（主路径，无大小上限、无暂存）；b64=浏览器上传兜底（暂存盘，用毕即删）。"""
    import secrets
    staged = None
    ok_flag = False
    try:
        if body.get("b64"):
            try:
                staged = _stage_b64(body["b64"])
            except Exception:  # noqa: BLE001  坏 b64
                return {"ok": False, "error": "b64 解码失败，请重新选择文件"}, 400
            pkg = staged
        elif body.get("path"):
            pkg = os.path.abspath(str(body["path"]))
        else:
            return {"ok": False, "error": "需提供 path（本机包路径，推荐）或 b64（小文件上传）"}, 400
        if not os.path.isfile(pkg):
            return {"ok": False, "reason_code": "file_not_found", "error": f"文件不存在：{pkg}"}, 400
        digest = _sha256_file(pkg)
        try:
            info = B.inspect_package(pkg)  # 闸①②③：结构/manifest/消毒/版本比对，纯读
        except Exception as e:  # noqa: BLE001  SaveGateError（业务拒绝）与其余异常同路返回
            reason = getattr(e, "code", None)
            payload = dict(getattr(e, "payload", None) or {})
            out = {"ok": False, "error": str(e) or (reason or "预览失败"), **payload}
            if reason:
                out["reason_code"] = reason
            return out, 400
        # 预览字段以 inspect_package 的权威计算为准（will_migrate/will_backup/备份名/警告）
        warnings = [str(w) for w in (info.get("warnings") or [])]
        if not info.get("will_backup"):
            warnings.append("目标存档为空或不存在：首装导入，不产生自动备份")
        now = time.time()
        _prune_import_tokens(now)
        token = secrets.token_hex(16)
        IMPORT_PENDING[token] = {"pkg_path": os.path.abspath(pkg), "sha256": digest,
                                 "ts": now, "staged": staged}
        ok_flag = True
        return {"ok": True, "token": token,
                "preview": {"entries": info.get("entries"), "bytes": info.get("bytes"),
                            "save_version": info.get("save_version"),
                            "program_save_version": info.get("program_save_version", B.SAVE_VERSION_CURRENT),
                            "rows_total": info.get("rows_total", 0),
                            "agents": info.get("agents") or {},
                            "will_migrate": info.get("will_migrate") or [],
                            "will_backup": bool(info.get("will_backup")),
                            "auto_backup_name": info.get("auto_backup_name"),
                            "warnings": warnings},
                "note": "确认导入=整档替换当前存档（目标非空时旧档自动备份）；预览只读未动存档"}, 200
    finally:
        if staged and not ok_flag:
            _remove_best_effort(staged)


def api_save_import_confirm(token: str) -> tuple[dict, int]:
    """第二段：token 一次性 + sha256 指纹复验（变了→changed_since_preview），
    然后透传 save.py import（闸③④+原子换位+迁移由壳层完成）。"""
    pending = IMPORT_PENDING.pop(token, None)  # 一次性：无论成败即销号
    try:
        if not pending or time.time() - pending["ts"] > IMPORT_TOKEN_TTL:
            return {"ok": False, "error": "token 无效或已过期，请重新预览"}, 400
        pkg = pending["pkg_path"]
        if not os.path.isfile(pkg):
            return {"ok": False, "reason_code": "file_not_found", "error": "包文件已不存在，请重新预览"}, 400
        if _sha256_file(pkg) != pending["sha256"]:
            return {"ok": False, "reason_code": "changed_since_preview",
                    "error": "包在预览后已被改动，已拦截，请重新预览"}, 400
        try:
            r = subprocess.run([sys.executable, B.p(ROOT, "05-scripts", "save.py"), "import", pkg],
                               capture_output=True, text=True, encoding="utf-8", timeout=300)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "导入超时（300s）——请改用命令行 python 05-scripts/save.py import <文件>"}, 500
        try:
            out = json.loads(r.stdout)
        except json.JSONDecodeError:
            return {"ok": False, "error": (r.stderr or r.stdout or "save.py import 无输出")[:400]}, 500
        return out, (200 if out.get("ok") else 400)
    finally:
        if pending and pending.get("staged"):
            _remove_best_effort(pending["staged"])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 安静
        pass

    # ---------- CSRF/DNS-rebinding 防护：只认本机 Host；写操作只认本机 Origin ----------
    def _guard(self) -> bool:
        host = self.headers.get("Host", "")
        if host != f"127.0.0.1:{PORT}":
            self._json({"ok": False, "error": "forbidden host"}, 403)
            return False
        if self.command == "POST" and self.path.startswith("/api/"):
            origin = self.headers.get("Origin", "")
            if origin not in ("", f"http://127.0.0.1:{PORT}"):
                self._json({"ok": False, "error": "forbidden origin"}, 403)
                return False
        return True

    # ---------- 基础 ----------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _file(self, path: str, ctype: str) -> None:
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._send(404, "not found".encode(), "text/plain; charset=utf-8")

    MAX_BODY = 64 * 1024 * 1024  # 64MB（zip 安装够用）

    def _body(self):
        """解析 JSON 请求体；任何畸形输入抛 ValueError，由 do_POST 统一回 400。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("bad content-length")
        if n < 0 or n > self.MAX_BODY:
            raise ValueError("body too large")
        if n == 0:
            return {}
        raw = self.rfile.read(n)
        return json.loads(raw.decode("utf-8"))

    # ---------- GET ----------
    def do_GET(self):  # noqa: N802
        if not self._guard():
            return
        u = urllib.parse.urlparse(self.path)
        path = u.path

        if path in ("/", "/index.html"):
            return self._file(B.p(PANEL_DIR, "index.html"), "text/html; charset=utf-8")
        for page in ("data.html", "install.html", "onboarding.html"):
            if path == "/" + page:
                return self._file(B.p(PANEL_DIR, page), "text/html; charset=utf-8")
        if path.startswith("/docs/images/") and path.endswith(".png"):
            # 根界校验（同 /agent-file）：拦 ../ 与盘符等穿越，只认 docs/images 内的 png
            base = os.path.abspath(B.p(ROOT, "docs", "images"))
            target = os.path.abspath(B.p(base, *path[len("/docs/images/"):].split("/")))
            try:
                inside = os.path.commonpath([base, target]) == base
            except ValueError:  # 跨盘等病态输入
                inside = False
            if inside and os.path.isfile(target):
                return self._file(target, "image/png")
            return self._send(404, "not found".encode(), "text/plain; charset=utf-8")
        if path == "/panel-lib.js":
            return self._file(B.p(PANEL_DIR, "panel-lib.js"), "text/javascript; charset=utf-8")
        if path.endswith("butler.css") and "skin" in path:  # 总管皮肤（含助理看板相对路径的兜底）
            return self._file(B.p(PANEL_DIR, "skin", "butler.css"), "text/css; charset=utf-8")

        if path == "/api/state":
            return self._json(api_state())

        parts = [p for p in path.split("/") if p]
        # /api/agent/<id>/table/<name> | export/<name>.csv
        if len(parts) == 5 and parts[1] == "agent" and parts[3] == "table":
            if not TABLE_RE.match(parts[4]):
                return self._json({"ok": False, "error": "bad table name"}, 400)
            sdir = agent_save(parts[2])
            rows = B.read_jsonl(B.p(sdir, "data", f"{parts[4]}.jsonl"))
            return self._json({"ok": True, "table": parts[4], "rows": rows})
        if len(parts) == 5 and parts[1] == "agent" and parts[3] == "export":
            name = parts[4].removesuffix(".csv")
            if not TABLE_RE.match(name):
                return self._json({"ok": False, "error": "bad table name"}, 400)
            sdir = agent_save(parts[2])
            rows = [r for r in B.read_jsonl(B.p(sdir, "data", f"{name}.jsonl")) if not r.get("_derived")]
            csv_text = rows_to_csv(name, rows)
            fname = f"{parts[2]}-{name}.csv"
            return self._send(200, csv_text.encode("utf-8-sig"), "text/csv; charset=utf-8",
                              {"Content-Disposition": f"attachment; filename=\"{fname}\""})
        # /jump/<id>
        if len(parts) == 2 and parts[0] == "jump":
            return self._jump(parts[1])
        # /agent-file/<id>/<path…>
        if len(parts) >= 3 and parts[0] == "agent-file":
            adir = agent_dir(parts[1])
            if not adir:
                return self._send(404, "no such agent".encode(), "text/plain; charset=utf-8")
            rel = "/".join(parts[2:])
            # 看板生成数据物理在存档区：URL 语义不变，来源映射
            if rel in ("dashboard/data.js", "dashboard/data.json"):
                sf = B.p(agent_save(parts[1]), *rel.split("/"))
                if os.path.isfile(sf):
                    return self._file(sf, "text/javascript; charset=utf-8" if rel.endswith(".js")
                                      else "application/json; charset=utf-8")
            target = os.path.abspath(B.p(adir, *rel.split("/")))
            if os.path.commonpath([os.path.abspath(adir), target]) != os.path.abspath(adir) \
                    or not os.path.isfile(target):
                return self._send(404, "not found".encode(), "text/plain; charset=utf-8")
            ctype = ("text/html; charset=utf-8" if target.endswith(".html")
                     else "text/css; charset=utf-8" if target.endswith(".css")
                     else "text/javascript; charset=utf-8" if target.endswith(".js")
                     else "application/json; charset=utf-8" if target.endswith(".json")
                     else "application/octet-stream")
            return self._file(target, ctype)
        return self._send(404, "not found".encode(), "text/plain; charset=utf-8")

    def _jump(self, token: str) -> None:
        v = JUMP_PENDING.pop(token, None)  # 一次性：即销号，防重放
        if not v or time.time() - v["ts"] > 3600:
            return self._json({"ok": False, "error": "jump token 无效或已过期，请回面板重新打开看板"}, 403)
        agent_id = v["agent"]
        adir = agent_dir(agent_id)
        if not adir:
            return self._json({"ok": False, "error": "no such agent"}, 404)
        try:
            subprocess.run([sys.executable, B.p(adir, "tools", "data.py"), "serve"],
                           capture_output=True, timeout=60, cwd=adir)
        except Exception:  # noqa: BLE001
            pass
        self.send_response(302)
        self.send_header("Location", f"/agent-file/{agent_id}/dashboard/index.html")
        self.end_headers()

    # ---------- POST ----------
    def do_POST(self):  # noqa: N802
        if not self._guard():
            return
        parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        try:
            body = self._body()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            if "too large" in str(e) and parts == ["api", "save-import", "preview"]:
                return self._json({"ok": False, "error":
                                   "请求体超过 64MB 上限（约对应 48MB 原始包）——请改用「本机路径」方式导入"}, 400)
            return self._json({"ok": False, "error": "bad request body"}, 400)

        if len(parts) == 4 and parts[1] == "agent" and parts[3] == "delete_row":
            if not TABLE_RE.match(str(body.get("table", ""))):
                return self._json({"ok": False, "error": "bad table name"}, 400)
            # 数据在存档切片（v0.1 误用程序区路径致端点从未可用）；trash 同落存档 data/trash
            sdir = agent_save(parts[2])
            tp = B.p(sdir, "data", f"{body.get('table')}.jsonl")
            rows = B.read_jsonl(tp)
            removed = [r for r in rows if r.get("_id") == body.get("row_id")]
            if not removed:
                return self._json({"ok": False, "error": "row not found"}, 404)
            from datetime import datetime
            trash = B.p(sdir, "data", "trash", f"{body.get('table')}-{datetime.now():%Y%m%d}.jsonl")
            for r in removed:
                r["_deleted_at"] = B.now_iso()
                B.append_jsonl(trash, r)
            keep = [r for r in rows if r.get("_id") != body.get("row_id")]
            B.atomic_write_text(tp, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep))
            return self._json({"ok": True, "deleted": body.get("row_id"), "trash": os.path.relpath(trash, ROOT)})

        if len(parts) == 4 and parts[1] == "agent" and parts[3] == "toggle":
            reg = B.load_registry(ROOT)
            e = B.registry_find(reg, parts[2])
            if not e:
                return self._json({"ok": False, "error": "not registered"}, 404)
            e["status"] = "paused" if e.get("status") == "active" else "active"
            B.save_registry(ROOT, reg)
            return self._json({"ok": True, "id": e["id"], "status": e["status"]})

        if parts == ["api", "install", "preview"]:
            return self._install_preview(body)
        if parts == ["api", "install", "confirm"]:
            return self._install_confirm(body)
        if parts == ["api", "create-from-template"]:
            return self._create_from_template(body)
        if parts == ["api", "release"]:
            r = subprocess.run([sys.executable, B.p(ROOT, "05-scripts", "release.py"), body.get("id", "")],
                               capture_output=True, text=True, encoding="utf-8", timeout=120)
            try:
                return self._json(json.loads(r.stdout))
            except json.JSONDecodeError:
                return self._json({"ok": False, "error": (r.stderr or r.stdout)[:300]}, 500)

        if parts == ["api", "save-migrate"]:
            r = subprocess.run([sys.executable, B.p(ROOT, "05-scripts", "migrate_save.py")],
                               capture_output=True, text=True, encoding="utf-8", timeout=300)
            try:
                return self._json(json.loads(r.stdout))
            except json.JSONDecodeError:
                return self._json({"ok": False, "error": (r.stderr or r.stdout)[:300]}, 500)

        if parts == ["api", "save-export"]:
            return self._json(api_save_export())
        if parts == ["api", "save-import", "preview"]:
            payload, code = api_save_import_preview(body)
            return self._json(payload, code)
        if parts == ["api", "save-import", "confirm"]:
            payload, code = api_save_import_confirm(str(body.get("token", "")))
            return self._json(payload, code)

        if parts == ["api", "profile"]:
            prof = body.get("profile") or {}
            prof["onboarding_complete"] = bool(prof.get("onboarding_complete"))
            B.atomic_write_json(B.master_save_files(ROOT)["profile"], prof)
            return self._json({"ok": True, "saved": "01-master/profile.json",
                               "note": "总管下次上岗即读到新档案"})

        return self._json({"ok": False, "error": "unknown endpoint"}, 404)

    def _create_from_template(self, body: dict) -> None:
        """PENDING #13：面板第四式安装——从 03-template 一键生成新助理并收编。"""
        import re as _re
        aid = str(body.get("id", "")).strip().lower()
        name = str(body.get("name", "")).strip()
        description = str(body.get("description", "")).strip()
        if not _re.match(r"^[a-z0-9][a-z0-9_-]*\.[a-z0-9][a-z0-9_-]*$", aid):
            return self._json({"ok": False, "error": "id 需为 <作者>.<名字> 小写格式，如 demo.reading"}, 400)
        if not name or not description:
            return self._json({"ok": False, "error": "name 与 description 必填"}, 400)
        dest = B.p(ROOT, "02-agents", aid)
        if os.path.exists(dest):
            return self._json({"ok": False, "error": f"02-agents/{aid} 已存在"}, 400)

        shutil.copytree(B.p(ROOT, "03-template"),
                        dest, ignore=shutil.ignore_patterns("data", "__pycache__", ".zcode", "data.js", "data.json"))
        try:
            mp = B.p(dest, "manifest.json")
            m = B.read_json(mp)
            m["id"] = aid
            m["name"] = name
            m["description"] = description
            m["author"] = aid.split(".")[0]
            B.atomic_write_json(mp, m)
            B.ensure_agent_skeleton(dest)
            r = subprocess.run([sys.executable, B.p(ROOT, "05-scripts", "adopt.py"), "--path", dest],
                               capture_output=True, text=True, encoding="utf-8", timeout=120)
            out = json.loads(r.stdout)
            if not out.get("ok"):
                shutil.rmtree(dest, ignore_errors=True)  # 收编失败 → 撤销，不留半成品
                return self._json(out, 400)
            return self._json({"ok": True, "created": aid,
                               "next": f"去 02-agents/{aid} 填章程/人设/数据表，或直接开始对话（lite 能力已可用）"})
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(dest, ignore_errors=True)
            return self._json({"ok": False, "error": f"创建失败已撤销：{e}"}, 500)

    def _resolve_source(self, body: dict):
        """返回 (manifest目录, 错误, 临时根目录|None)。zip=解包；path=原样；git=浅克隆（只读不执行）。
        解包/克隆后若找不到 manifest，就地清理临时目录并返回错误——绝不留泄漏；
        坏 b64/坏 zip 等异常同样先清场再上抛（预览统一回 400）。"""
        if body.get("mode") == "zip":
            tmp = tempfile.mkdtemp(prefix="panel-install-")
            try:
                fname = str(body.get("filename") or "agent.zip")
                if fname in (".", "..") or "/" in fname or "\\" in fname or ":" in fname:
                    shutil.rmtree(tmp, ignore_errors=True)
                    return None, "zip 文件名不合法（含路径分隔符或盘符）", None
                zpath = os.path.join(tmp, os.path.basename(fname))
                with open(zpath, "wb") as fh:
                    fh.write(base64.b64decode(body.get("b64", "")))
                B.extract_zip(zpath, os.path.join(tmp, "x"))
            except Exception:  # noqa: BLE001  坏 b64 / 坏 zip 等
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            found = B.find_manifest_dir(os.path.join(tmp, "x"))
            if not found:
                shutil.rmtree(tmp, ignore_errors=True)
                return None, "包内找不到 manifest.json（支持根目录或下两层）", None
            return found, None, tmp
        if body.get("mode") == "path":
            p = os.path.abspath(body.get("path", ""))
            if not os.path.isdir(p):
                return None, f"文件夹不存在：{p}", None
            found = B.find_manifest_dir(p)
            if not found:
                return None, "该文件夹内找不到 manifest.json", None
            return found, None, None
        if body.get("mode") == "git":
            tmp = tempfile.mkdtemp(prefix="panel-install-")
            try:
                good, msg = B.clone_git(body.get("url", ""), os.path.join(tmp, "x"))
            except Exception:  # noqa: BLE001
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            if not good:
                shutil.rmtree(tmp, ignore_errors=True)
                return None, f"git clone 失败：{msg}", None
            found = B.find_manifest_dir(os.path.join(tmp, "x"))
            if not found:
                shutil.rmtree(tmp, ignore_errors=True)
                return None, "仓库内找不到 manifest.json（支持根目录或下两层）", None
            return found, None, tmp
        return None, "mode 需为 zip|path|git", None

    def _install_preview(self, body: dict) -> None:
        """第一段：解包/克隆 + 只读校验（不执行包内任何代码），返回预览与一次性 token。"""
        import secrets
        import time
        try:
            src_dir, err, tmp_root = self._resolve_source(body)
        except Exception as e:  # noqa: BLE001  坏 b64 / 坏 zip 等（_resolve_source 已先清场）
            return self._json({"ok": False, "error": f"来源解析失败：{e}"}, 400)
        if err:
            return self._json({"ok": False, "error": err}, 400)
        if not src_dir:
            return self._json({"ok": False, "error": "找不到 manifest.json（支持根目录或下两层）"}, 400)
        try:
            mp = B.p(src_dir, "manifest.json")
            if not os.path.isfile(mp):
                if tmp_root:
                    shutil.rmtree(tmp_root, ignore_errors=True)
                return self._json({"ok": False, "error": "manifest.json 不在包内"}, 400)
            m = B.read_json(mp)
            problems, warnings = B.validate_manifest(m, src_dir)
            token = secrets.token_hex(16)
            now = time.time()
            for k in [k for k, v in INSTALL_PENDING.items() if now - v["ts"] > 3600]:
                _drop_install_token(k)  # 过期逐出：解包临时目录一并清
            while len(INSTALL_PENDING) >= 50:  # 上限：丢最旧（临时目录一并清）
                _drop_install_token(min(INSTALL_PENDING, key=lambda k: INSTALL_PENDING[k]["ts"]))
            INSTALL_PENDING[token] = {"dir": os.path.abspath(src_dir), "tmp_root": tmp_root, "ts": now}
            return self._json({"ok": True, "token": token,
                               "preview": {"id": m.get("id"), "name": m.get("name"),
                                           "version": m.get("version"), "author": m.get("author"),
                                           "description": m.get("description"),
                                           "domain": m.get("domain"),
                                           "tables": [t.get("name") for t in m.get("tables") or []],
                                           "reminders": [r.get("rule") for r in m.get("reminders") or []],
                                           "net_permission": bool((m.get("permissions") or {}).get("net"))},
                               "schema_problems": problems, "schema_warnings": warnings,
                               "note": "确认收编后才执行其工具自检（首次运行包内代码）"})
        except Exception:  # noqa: BLE001
            if tmp_root:
                shutil.rmtree(tmp_root, ignore_errors=True)
            return self._json({"ok": False, "error": "预览失败：包内容无法解析"}, 500)

    def _install_confirm(self, body: dict) -> None:
        """第二段：凭 token 真正收编（此时才执行包内工具自检）。"""
        token = body.get("token", "")
        pending = INSTALL_PENDING.pop(token, None)  # 一次性：无论成败即销号
        try:
            if not pending or time.time() - pending["ts"] > 3600 or not os.path.isdir(pending["dir"]):
                return self._json({"ok": False, "error": "token 无效或已过期，请重新预览"}, 400)
            r = subprocess.run([sys.executable, B.p(ROOT, "05-scripts", "adopt.py"),
                                "--path", pending["dir"]],
                               capture_output=True, text=True, encoding="utf-8", timeout=300)
            try:
                out = json.loads(r.stdout)
            except json.JSONDecodeError:
                out = {"ok": False, "error": (r.stderr or r.stdout)[:400]}
            return self._json(out, 200 if out.get("ok") else 400)
        finally:
            if pending and pending.get("tmp_root"):
                shutil.rmtree(pending["tmp_root"], ignore_errors=True)


class Server(ThreadingHTTPServer):
    # Windows 下 SO_REUSEADDR 会允许同一端口被重复绑定且不报错——
    # 两个 AgentCrew 面板静默共存 = 主人会连到错误仓库，必须让它崩得响亮
    allow_reuse_address = False


def _sweep_temp_orphans() -> None:
    """启动兜底：清 %TEMP% 里超 TTL 的导入暂存文件（crew-import-staging-*）与
    安装解包孤儿（panel-install-*）——关窗即退等场景留下的残留，靠下次启动兜底。"""
    now = time.time()
    tmp = tempfile.gettempdir()
    try:
        names = os.listdir(tmp)
    except OSError:
        return
    for name in names:
        if not (name.startswith("crew-import-staging-") or name.startswith("panel-install-")):
            continue
        fp = os.path.join(tmp, name)
        try:
            if now - os.path.getmtime(fp) <= IMPORT_TOKEN_TTL:
                continue
            if os.path.isdir(fp):
                shutil.rmtree(fp, ignore_errors=True)
            else:
                _remove_best_effort(fp)
        except OSError:
            pass


def main() -> int:
    B.utf8_console()
    _sweep_temp_orphans()
    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"端口 {PORT} 已被占用：很可能另一个 AgentCrew 面板正在运行。")
        print("请先关闭它（或那个黑窗口），再启动本面板。3 秒后退出。")
        time.sleep(3)
        return 1
    print(f"管家面板运行中：http://127.0.0.1:{PORT}/  （关掉本窗口即退出）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
