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
  GET  /jump/<id>                          刷新该助理看板数据后跳转
静态：/agent-file/<id>/<path…> 各助理文件（含看板）
"""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PANEL_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PANEL_DIR)
sys.path.insert(0, os.path.join(ROOT, "05-scripts"))
import agentcrew_lib as B  # noqa: E402

PORT = 7530
TABLE_RE = __import__("re").compile(r"^[a-z][a-z0-9_]*$")
INSTALL_PENDING: dict[str, dict] = {}  # token -> {"dir":..., "tmp": bool, "ts": float}


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
    return {"ok": True, "root": ROOT, "profile": profile_state(), "agents": agents,
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

    def _jump(self, agent_id: str) -> None:
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
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return self._json({"ok": False, "error": "bad request body"}, 400)

        if len(parts) == 4 and parts[1] == "agent" and parts[3] == "delete_row":
            if not TABLE_RE.match(str(body.get("table", ""))):
                return self._json({"ok": False, "error": "bad table name"}, 400)
            adir = agent_dir(parts[2])
            if not adir:
                return self._json({"ok": False, "error": "no such agent"}, 404)
            tp = B.p(adir, "data", f"{body.get('table')}.jsonl")
            rows = B.read_jsonl(tp)
            keep = [r for r in rows if r.get("_id") != body.get("row_id")]
            if len(keep) == len(rows):
                return self._json({"ok": False, "error": "row not found"}, 404)
            removed = [r for r in rows if r.get("_id") == body.get("row_id")]
            from datetime import datetime
            trash = B.p(sdir, "data", "trash", f"{body.get('table')}-{datetime.now():%Y%m%d}.jsonl")
            for r in removed:
                r["_deleted_at"] = B.now_iso()
                B.append_jsonl(trash, r)
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
                        dest, ignore=shutil.ignore_patterns("data", "__pycache__", ".zcode", "dashboard/data.js"))
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
        """返回 (manifest目录, 错误, 是否临时目录)。zip=解包；path=原样；git=浅克隆（只读不执行）。
        解包/克隆后若找不到 manifest，就地清理临时目录并返回错误——绝不留泄漏。"""
        if body.get("mode") == "zip":
            tmp = tempfile.mkdtemp(prefix="panel-install-")
            zpath = os.path.join(tmp, body.get("filename") or "agent.zip")
            with open(zpath, "wb") as fh:
                fh.write(base64.b64decode(body.get("b64", "")))
            B.extract_zip(zpath, os.path.join(tmp, "x"))
            found = B.find_manifest_dir(os.path.join(tmp, "x"))
            if not found:
                shutil.rmtree(tmp, ignore_errors=True)
                return None, "包内找不到 manifest.json（支持根目录或下两层）", True
            return found, None, True
        if body.get("mode") == "path":
            p = os.path.abspath(body.get("path", ""))
            if not os.path.isdir(p):
                return None, f"文件夹不存在：{p}", False
            found = B.find_manifest_dir(p)
            if not found:
                return None, "该文件夹内找不到 manifest.json", False
            return found, None, False
        if body.get("mode") == "git":
            tmp = tempfile.mkdtemp(prefix="panel-install-")
            good, msg = B.clone_git(body.get("url", ""), os.path.join(tmp, "x"))
            if not good:
                shutil.rmtree(tmp, ignore_errors=True)
                return None, f"git clone 失败：{msg}", True
            found = B.find_manifest_dir(os.path.join(tmp, "x"))
            if not found:
                shutil.rmtree(tmp, ignore_errors=True)
                return None, "仓库内找不到 manifest.json（支持根目录或下两层）", True
            return found, None, True
        return None, "mode 需为 zip|path|git", False

    def _install_preview(self, body: dict) -> None:
        """第一段：解包/克隆 + 只读校验（不执行包内任何代码），返回预览与一次性 token。"""
        import secrets
        import time
        try:
            src_dir, err, is_tmp = self._resolve_source(body)
        except Exception as e:  # noqa: BLE001  坏 b64 / 坏 zip 等
            return self._json({"ok": False, "error": f"来源解析失败：{e}"}, 400)
        if err:
            if is_tmp and src_dir:
                shutil.rmtree(os.path.dirname(src_dir), ignore_errors=True)
            return self._json({"ok": False, "error": err}, 400)
        if not src_dir:
            return self._json({"ok": False, "error": "找不到 manifest.json（支持根目录或下两层）"}, 400)
        tmp_root = os.path.dirname(src_dir) if is_tmp else None
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
            for k in [k for k, v in INSTALL_PENDING.items() if now - v["ts"] > 3600][:]:
                INSTALL_PENDING.pop(k, None)
            while len(INSTALL_PENDING) >= 50:  # 上限：丢最旧
                oldest = min(INSTALL_PENDING, key=lambda k: INSTALL_PENDING[k]["ts"])
                INSTALL_PENDING.pop(oldest, None)
            INSTALL_PENDING[token] = {"dir": os.path.abspath(src_dir), "tmp": is_tmp, "ts": now}
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
        pending = INSTALL_PENDING.pop(token, None)
        if not pending or not os.path.isdir(pending["dir"]):
            return self._json({"ok": False, "error": "token 无效或已过期，请重新预览"}, 400)
        try:
            r = subprocess.run([sys.executable, B.p(ROOT, "05-scripts", "adopt.py"),
                                "--path", pending["dir"]],
                               capture_output=True, text=True, encoding="utf-8", timeout=300)
            try:
                out = json.loads(r.stdout)
            except json.JSONDecodeError:
                out = {"ok": False, "error": (r.stderr or r.stdout)[:400]}
            return self._json(out, 200 if out.get("ok") else 400)
        finally:
            if pending["tmp"]:
                shutil.rmtree(os.path.dirname(pending["dir"]), ignore_errors=True)


class Server(ThreadingHTTPServer):
    # Windows 下 SO_REUSEADDR 会允许同一端口被重复绑定且不报错——
    # 两个 AgentCrew 面板静默共存 = 主人会连到错误仓库，必须让它崩得响亮
    allow_reuse_address = False


def main() -> int:
    B.utf8_console()
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
