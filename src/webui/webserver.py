# -*- coding: utf-8 -*-
"""HTTP 层：路由分派、静态文件、本地服务。"""

from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import parse_qs
from urllib.parse import unquote
from urllib.parse import urlparse
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import webbrowser

from .archive import delete_pick, delete_plan_log, delete_report, latest_pick_bundle, latest_report, list_picks, list_reports, pick_bundle, plan_log_entries, prune_reports, report_bundle
from . import alerts as alerts_store
from . import flowapi
from .holdings_sync import CAPTCHA_ROOT, captcha_image, holdings_python, install_hint, submit_captcha_answer
from .jobs import JOBS, build_check_argv, build_run_argv, run_batch
from .market import (build_market, build_state, build_symbols, kline_bundle,
                     latest_market_forecast, run_market_forecast)
from .overview import build_overview
from .flowbook import ledger_view
from .freshness import purge_stale
from .paths import ACCOUNT_PATH, AIPLAN, DATA_DIR, MODELS_PATH, PICK_DIR, POOL_PATH, PYTHON, ROOT, STATIC_DIR, TRACKLIST_PATH, TRASH_DIR, TRASH_TTL_DAYS, WATCHLIST_PATH, aiplan, inside, num, read_text, rel, save_like
from .pick import pick_list_meta, pick_param
from .pick_run import pick_boards_bundle, pick_l1_subs, run_pick
from .plancheck import plancheck_bundle, run_plan_check
from .store import (load_account_bundle, load_holdings_bundle, load_models_bundle,
                    load_tracklist, load_watchlist, tracklist_add, tracklist_import_watchlist,
                    tracklist_remove, save_profile, save_provider, set_default_profile,
                    watchlist_add, watchlist_remove)
from .track import delete_plan as delete_track_plan
from .track import exec_summary as track_exec_summary
from .track import save_exec as save_track_exec
from .track_run import run_track
from .trackview import build_detail as build_track_detail
from .trackview import build_overview as build_track_overview
from .trash import purge_trash_expired, restore_trash, trash_items


MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
}


LOGIN_COOKIE = "aiplan_session"


# 客户端中途断开（刷新 / 切页 / 取消轮询）时 Windows 会给这几个错误码：
# 10053 本机软件中止、10054 对端强制关闭、10058 已无法发送。它们不是服务端故障。
_GONE_WINERRORS = (10053, 10054, 10058)


def is_disconnect(exc):
    """判断异常是不是「客户端已经走了」——这类不该记成服务端异常，也不该回 500。"""
    if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ConnectionError)):
        return True
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in _GONE_WINERRORS


LAN_PASSWORD_ENV = "AIPLAN_WEBUI_PASSWORD"
LAN_PASSWORD_FILE = os.path.join(ROOT, "config", "webui配置.json")


def resolve_lan_password(explicit=None):
    """局域网口令来源：--password > 环境变量 > config/webui配置.json > 随机生成。

    源码里不留默认口令：仓库是公开的，写死的口令等于没有密码。
    """
    if explicit:
        return explicit, "--password"
    from_env = os.environ.get(LAN_PASSWORD_ENV, "").strip()
    if from_env:
        return from_env, "环境变量 %s" % LAN_PASSWORD_ENV
    try:
        with open(LAN_PASSWORD_FILE, "r", encoding="utf-8-sig") as fh:
            saved = json.load(fh)
        value = str(saved.get("密码") or saved.get("password") or "").strip()
        if value:
            return value, rel(LAN_PASSWORD_FILE)
    except (OSError, ValueError, AttributeError):
        pass
    return secrets.token_urlsafe(6), "随机生成（本次运行有效）"


class Handler(BaseHTTPRequestHandler):
    server_version = "aiplan-webui/1.0"
    protocol_version = "HTTP/1.1"
    _gone = False                      # 客户端已断：后面的写回一律跳过

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    # ---- 局域网密码校验 ----
    def _password(self):
        return getattr(self.server, "password", None)

    def _session_token(self):
        return getattr(self.server, "session_token", None)

    def _authorized(self):
        if not self._password():
            return True
        session_token = self._session_token()
        if not session_token:
            return False
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            supplied = cookie[LOGIN_COOKIE].value
        except (KeyError, TypeError):
            return False
        return hmac.compare_digest(str(supplied), session_token)

    @staticmethod
    def _safe_next_path(path):
        """只允许回到控制台的已知入口，避免开放重定向。"""
        path = unquote(path or "")
        return path if path in ("/", "/m", "/m/") else "/"

    def _login_page(self, error=False, next_path="/"):
        error_html = '<p class="error">密码错误，请重新输入。</p>' if error else ""
        next_path = self._safe_next_path(next_path)
        body = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>登录 aiplan</title>
<style>body{font:16px/1.7 system-ui,sans-serif;background:#f6f7f9;color:#20242a;margin:0}.box{max-width:360px;margin:12vh auto;background:#fff;padding:28px;border:1px solid #dfe3e8;border-radius:12px;box-shadow:0 8px 30px #0000000d}h2{margin:0 0 8px}.tip{color:#68707a;margin:0 0 20px}label{display:block;font-weight:600;margin-bottom:7px}input{box-sizing:border-box;width:100%;font:inherit;padding:11px 12px;border:1px solid #c8ced6;border-radius:8px}button{width:100%;font:inherit;font-weight:600;padding:11px 12px;margin-top:16px;border:0;border-radius:8px;background:#1769aa;color:#fff;cursor:pointer}.error{color:#c62828;margin:0 0 14px}</style>
<div class="box"><h2>登录 aiplan Web</h2><p class="tip">请输入局域网访问密码。</p>__ERROR__
<form method="post" action="/__login"><input type="hidden" name="next" value="__NEXT__"><label for="password">访问密码</label>
<input id="password" name="password" type="password" autocomplete="current-password" autofocus required>
<button type="submit">登录</button></form></div></html>"""
        body = body.replace("__ERROR__", error_html).replace("__NEXT__", next_path)
        self._send(401 if error else 200, body, "text/html; charset=utf-8")

    def _login(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        form = parse_qs(raw.decode("utf-8", "replace"))
        supplied = (form.get("password") or [""])[0]
        next_path = self._safe_next_path((form.get("next") or ["/"])[0])
        expected = self._password() or ""
        if not hmac.compare_digest(supplied, expected):
            return self._login_page(error=True, next_path=next_path)
        self.send_response(303)
        self.send_header("Location", next_path)
        self.send_header("Set-Cookie",
                         "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800" %
                         (LOGIN_COOKIE, self._session_token()))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _deny_access(self):
        if self.path.startswith("/api/"):
            return self._json({"ok": False, "error": "未登录：请先在登录页输入访问密码"}, 401)
        path = unquote(urlparse(self.path).path)
        return self._login_page(next_path=self._safe_next_path(path))

    def _check_access(self):
        """返回 True 表示请求可以继续；否则已经写回登录页或 401。"""
        if not self._password():
            return True
        if self._authorized():
            return True
        self._deny_access()
        return False

    # ---- 基础输出 ----
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        if self._gone:                 # 已经知道对端没了就别再写
            return False
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:         # noqa: BLE001
            if not is_disconnect(e):
                raise
            self._gone = True          # 客户端断开：安静收场，不当作异常
            self.close_connection = True
            return False
        return True

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

    def _err(self, msg, code=400):
        self._json({"ok": False, "error": msg}, code)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return {}
        return json.loads(text)

    # ---- GET ----
    def do_GET(self):
        if unquote(urlparse(self.path).path) == "/__login":
            return self._login_page()
        if not self._check_access():
            return
        u = urlparse(self.path)
        path = unquote(u.path)
        q = parse_qs(u.query)
        try:
            if path.startswith("/api/"):
                return self._api_get(path, q)
            return self._static(path)
        except Exception as e:  # noqa: BLE001
            if self._gone or is_disconnect(e):
                return                 # 对端已经走了：不用再回 500（回也会再炸一次）
            self._err("服务端异常：%r" % (e,), 500)

    def _api_get(self, path, q):
        if path == "/api/state":
            return self._json(build_state())
        if path == "/api/holdings/captcha":
            captcha_id = q.get("id", [""])[0]
            try:
                image = captcha_image(CAPTCHA_ROOT, captcha_id)
            except Exception as exc:  # noqa: BLE001
                return self._err(str(exc), getattr(exc, "code", 500))
            return self._send(200, image, "image/png")
        if path == "/api/holdings":
            return self._json(load_holdings_bundle())
        if path == "/api/account":
            return self._json(load_account_bundle())
        if path == "/api/models":
            return self._json(load_models_bundle())
        if path == "/api/market":
            return self._json(build_market())
        if path == "/api/reports":
            prune = prune_reports()          # 每个标的只留最新一份（惰性、60 秒节流）
            prune.pop("明细", None)
            return self._json({"items": list_reports(), "清理": prune})
        if path == "/api/report":
            target = q.get("path", [None])[0]
            if not target:
                phase = q.get("phase", [None])[0]
                target = latest_report(phase)
            bundle = report_bundle(target)
            if not bundle:
                return self._err("报告不存在或不在 data/ai 下：%s" % target, 404)
            return self._json(bundle)
        if path == "/api/history":
            return self._json({"plan_log": plan_log_entries(), "reports": list_reports()})
        if path == "/api/symbols":
            return self._json({"items": build_symbols()})
        if path == "/api/kline":
            # 本地没日K缓存就自动拉取（批注 3），取数口径见 market.kline_bundle
            data = kline_bundle(q.get("code", [""])[0], q.get("limit", ["180"])[0])
            if not data:
                return self._err("日K 取不到（东财与腾讯都失败，或该代码不在两市）", 404)
            return self._json(data)
        if path == "/api/watchlist":
            return self._json({"路径": rel(WATCHLIST_PATH), "存在": os.path.exists(WATCHLIST_PATH),
                               "条目": load_watchlist(), "原文": read_text(WATCHLIST_PATH)})
        if path == "/api/trash":
            purge = purge_trash_expired()          # 惰性清理：同进程 30 分钟节流
            return self._json({"items": trash_items(), "目录": rel(TRASH_DIR),
                               "过期天数": TRASH_TTL_DAYS, "上次清理": purge})
        if path == "/api/pick":
            target = q.get("path", [None])[0]
            if target:
                bundle = pick_bundle(target)
                if not bundle:
                    return self._err("荐股结果不存在或不在 data/ai/pick 下：%s" % target, 404)
                return self._json(bundle)
            return self._json(latest_pick_bundle())
        if path == "/api/pick/list":
            return self._json(dict({"items": list_picks(), "目录": rel(PICK_DIR)},
                                   **pick_list_meta()))
        if path == "/api/pick/boards":
            refresh = (q.get("refresh", ["0"])[0] or "0") in ("1", "true", "yes")
            return self._json(pick_boards_bundle(refresh))
        if path == "/api/pick/industry":
            code = (q.get("code", [""])[0] or "").strip().upper()
            if not re.match(r"^BK\d{3,5}$", code):
                return self._err("code 需为板块代码（如 BK1201）", 400)
            refresh = (q.get("refresh", ["0"])[0] or "0") in ("1", "true", "yes")
            data, err = pick_l1_subs(code, refresh)
            if err:
                return self._err("行业细分取数失败：%s" % err, 502)
            return self._json(data)
        if path == "/api/market/forecast":
            doc, p = latest_market_forecast()
            return self._json({"forecast": doc, "路径": rel(p) if p else None,
                               "mtime": os.path.getmtime(p) if p else None})
        if path == "/api/plancheck":
            target = (q.get("report", [None])[0] or "").strip()
            if not target:
                target = latest_report(None) or ""
                if not target:
                    return self._err("还没有报告：先在「运行研判」里跑一次", 404)
            refresh = (q.get("refresh", ["0"])[0] or "0") in ("1", "true", "yes")
            data, err = plancheck_bundle(target, refresh=refresh)
            if err:
                return self._err(err, 400)
            return self._json(data)
        if path == "/api/overview":
            refresh = (q.get("refresh", ["0"])[0] or "0") in ("1", "true", "yes")
            return self._json(build_overview(refresh=refresh))
        if path == "/api/alerts":
            limit = q.get("limit", ["50"])[0]
            return self._json({"items": alerts_store.list_alerts(limit),
                               "队列": alerts_store.info()})
        if path == "/api/tracklist":
            return self._json({"路径": rel(TRACKLIST_PATH),
                               "存在": os.path.exists(TRACKLIST_PATH),
                               "条目": load_tracklist(),
                               "原文": read_text(TRACKLIST_PATH)})
        if path == "/api/ledger":
            # 持仓页「交易明细」：交易台账只读视图（批注 4）
            code = (q.get("code", [""])[0] or "").strip()
            limit = (q.get("limit", ["500"])[0] or "500")
            return self._json(ledger_view(code or None, limit))
        if path == "/api/track/all":
            refresh = (q.get("refresh", ["0"])[0] or "0") in ("1", "true", "yes")
            return self._json(build_track_overview(refresh=refresh))
        if path == "/api/track":
            code = (q.get("code", [""])[0] or "").strip()
            if not code:
                return self._err("缺少 code（6 位证券代码）", 400)
            refresh = (q.get("refresh", ["0"])[0] or "0") in ("1", "true", "yes")
            data, err = build_track_detail(code, refresh=refresh)
            if err:
                return self._err(err, 400)
            return self._json(data)
        if flowapi.handle_get(self, path, q):
            return
        if path.startswith("/api/jobs/"):
            rest = path[len("/api/jobs/"):]
            jid = rest.split("/")[0]
            try:
                start = int(q.get("from", ["0"])[0])
            except ValueError:
                start = 0
            job = JOBS.get(jid, start)
            if not job:
                return self._err("任务不存在：%s" % jid, 404)
            return self._json(job)
        if path == "/api/blob":
            return self._blob(q)
        return self._err("未知接口：%s" % path, 404)

    def _blob(self, q):
        """读取 data/ 下的文本文件（Markdown / txt / json），供下载或预览。"""
        target = q.get("path", [None])[0]
        if not target:
            return self._err("缺少 path 参数")
        p = target if os.path.isabs(target) else os.path.join(ROOT, target)
        p = os.path.abspath(p)
        if not inside(p, DATA_DIR) or not os.path.exists(p):
            return self._err("只允许读取 data/ 下的文件", 403)
        text = read_text(p)
        dl = q.get("download", ["0"])[0] == "1"
        if dl:
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % os.path.basename(p))
            body = text.encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return self._json({"路径": rel(p), "文本": text, "大小": len(text)})

    # ---- POST ----
    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        if path == "/__login":
            return self._login()
        if not self._check_access():
            return
        u = urlparse(self.path)
        path = unquote(u.path)
        try:
            body = self._body()
        except ValueError as e:
            return self._err("请求体不是合法 JSON：%s" % e)
        try:
            return self._api_post(path, body)
        except Exception as e:  # noqa: BLE001
            if self._gone or is_disconnect(e):
                return
            self._err("服务端异常：%r" % (e,), 500)

    def _api_post(self, path, body):
        if path == "/api/alerts/clear":
            cleared = alerts_store.clear()
            return self._json({"ok": True, "已清空": cleared, "items": [],
                               "队列": alerts_store.info()})
        if path == "/api/holdings/captcha":
            try:
                result = submit_captcha_answer(CAPTCHA_ROOT, body.get("id"), body.get("code"))
            except Exception as exc:  # noqa: BLE001
                return self._err(str(exc), getattr(exc, "code", 400))
            return self._json(result)
        if path == "/api/holdings":
            text = body.get("text")
            if not isinstance(text, str):
                return self._err("缺少 text")
            written, bkp = save_like(POOL_PATH, text)
            return self._json({"ok": True, "已写入": written,
                               "备份": rel(bkp) if bkp else None,
                               "持仓": load_holdings_bundle()})
        if path == "/api/account":
            return self._save_json_config(body, ACCOUNT_PATH, load_account_bundle)
        if path == "/api/models":
            return self._save_json_config(body, MODELS_PATH, load_models_bundle)
        if path in ("/api/models/profile", "/api/models/provider"):
            # 模型配置页的「添加 / 修改 / 删除」（批注 8、9）：写 config/模型配置.json，写前 .bak
            action = (body.get("动作") or body.get("action") or "新增").strip()
            data = body.get("数据") if isinstance(body.get("数据"), dict) else body
            fn = save_profile if path.endswith("/profile") else save_provider
            bundle, err = fn(action, data)
            if err:
                return self._err(err)
            return self._json({"ok": True, "已写入": bundle.get("已写入"),
                               "备份": bundle.get("备份"),
                               "改动": bundle.get("改动") or [],
                               "模型": bundle})
        if path == "/api/models/default":
            if "默认_profile" not in body and "profiles_by_phase" not in body:
                return self._err("缺少 默认_profile 或 profiles_by_phase")
            bundle, err = set_default_profile(body.get("默认_profile"), body.get("profiles_by_phase"))
            if err:
                return self._err(err)
            return self._json({"ok": True, "已写入": bundle.get("已写入"),
                               "备份": bundle.get("备份"), "改动": bundle.get("改动") or [],
                               "配置": bundle})
        if path == "/api/trash/restore":
            result, err = restore_trash(body.get("path"))
            if err:
                return self._err(err)
            return self._json(dict(result, ok=True, items=trash_items(),
                                   reports=list_reports(), history=plan_log_entries()))
        if path == "/api/trash/purge":
            try:
                days = int(body.get("days") or TRASH_TTL_DAYS)
            except (TypeError, ValueError):
                days = TRASH_TTL_DAYS
            purge = purge_trash_expired(days=max(1, min(3650, days)), force=True)
            return self._json({"ok": True, "清理": purge, "items": trash_items(),
                               "过期天数": TRASH_TTL_DAYS})
        if path == "/api/pick/delete":
            result, err = delete_pick(body.get("path"))
            if err:
                return self._err(err)
            return self._json(dict(result, ok=True, items=list_picks(),
                                   trash=trash_items()))
        if path == "/api/report/delete":
            result, err = delete_report(body.get("path"))
            if err:
                return self._err(err)
            return self._json(dict(result, ok=True,
                                   reports=list_reports(),
                                   history=plan_log_entries()))
        if path == "/api/watchlist/add":
            items, err = watchlist_add(body.get("code"), body.get("name"), body.get("note"))
            if err:
                return self._err(err)
            return self._json({"ok": True, "条目": items, "自选股": load_watchlist(),
                               "代码候选": build_symbols()})
        if path == "/api/watchlist/remove":
            items, err = watchlist_remove(body.get("code"))
            if err:
                return self._err(err)
            return self._json({"ok": True, "自选股": load_watchlist(),
                               "代码候选": build_symbols()})
        if path == "/api/watchlist":
            text = body.get("text")
            if not isinstance(text, str):
                return self._err("缺少 text")
            written, bkp = save_like(WATCHLIST_PATH, text)
            return self._json({"ok": True, "已写入": written,
                               "备份": rel(bkp) if bkp else None,
                               "自选股": load_watchlist(), "代码候选": build_symbols()})
        if path == "/api/tracklist":
            text = body.get("text")
            if not isinstance(text, str):
                return self._err("缺少 text")
            written, bkp = save_like(TRACKLIST_PATH, text)
            return self._json({"ok": True, "已写入": written,
                               "备份": rel(bkp) if bkp else None,
                               "条目": load_tracklist()})
        if path == "/api/tracklist/add":
            items, err = tracklist_add(body.get("code"), body.get("name"), body.get("note"))
            if err:
                return self._err(err)
            return self._json({"ok": True, "条目": items, "跟踪": load_tracklist()})
        if path == "/api/tracklist/remove":
            items, err = tracklist_remove(body.get("code"))
            if err:
                return self._err(err)
            return self._json({"ok": True, "跟踪": load_tracklist()})
        if path == "/api/tracklist/import-watchlist":
            added, _items = tracklist_import_watchlist()
            return self._json({"ok": True, "新增": added, "跟踪": load_tracklist()})
        if path == "/api/track/exec":
            doc, err = save_track_exec(body.get("计划路径"), body.get("条目"), body.get("总体备注"))
            if err:
                return self._err(err)
            return self._json({"ok": True, "执行记录": doc,
                               "执行摘要": track_exec_summary(doc)})
        if path == "/api/track/delete":
            result, err = delete_track_plan(body.get("path"))
            if err:
                return self._err(err)
            return self._json(dict(result, ok=True, trash=trash_items()))
        if flowapi.handle_post(self, path, body):
            return
        if path == "/api/history/delete":
            if "line" not in body:
                return self._err("缺少 line")
            ok, info = delete_plan_log(body["line"])
            if not ok:
                return self._err(info)
            return self._json({"ok": True, "快照": info,
                               "plan_log": plan_log_entries(),
                               "reports": list_reports()})
        if path == "/api/jobs":
            kind = body.get("kind") or "run"
            fresh = {} if kind == "holdings_sync" else purge_stale()   # 事实包不许用过期数据
            if kind == "run":
                try:
                    argv = build_run_argv(body)
                except ValueError as e:
                    return self._err(str(e))
                meta = {"phase": (body.get("phase") or "post").strip(),
                        "code": (body.get("code") or "").strip(),
                        "date": (body.get("date") or "").strip() or None,
                        "dry_run": bool(body.get("dry_run")),
                        "no_fetch": bool(body.get("no_fetch"))}
            elif kind == "check":
                argv = build_check_argv(body)
                meta = {"profile": body.get("profile")}
            elif kind == "batch":
                codes = [str(c).strip() for c in (body.get("codes") or []) if str(c).strip()]
                if not codes:
                    return self._err("没有可批量研判的标的")
                phase = (body.get("phase") or "post").strip()
                try:
                    probe = build_run_argv(dict(body, code=codes[0]))
                except ValueError as e:
                    return self._err(str(e))
                opts = probe[5:]          # 去掉 python / runner / phase / --code / <首只代码>
                meta = {"phase": phase, "codes": codes, "label": "批量研判"}
                job = JOBS.start(kind, [], meta, label="批量研判持仓（%d 只）" % len(codes),
                                 func=lambda log, ctl: run_batch(log, ctl, codes, phase, opts))
                return self._json({"ok": True, "id": job["id"], "命令": job["命令"], "清理": fresh, "标的": codes})
            elif kind == "market":
                prof = body.get("profile")
                argv = []
                meta = {"profile": prof, "label": "大盘走势预测"}
                job = JOBS.start(kind, argv, meta, label="生成大盘走势预测",
                                 func=lambda log, ctl: run_market_forecast(
                                     log, profile=prof, model_pro=body.get("model_pro"),
                                     api_base=body.get("api_base"), api_key=body.get("api_key")))
                return self._json({"ok": True, "id": job["id"], "命令": job["命令"], "清理": fresh})
            elif kind == "pick":
                opts = pick_param(body)
                if opts["model_top"] > opts["pool_size"]:
                    return self._err("送模型的条数不能超过候选池上限"
                                     "（候选池 %d / 送模型 %d）"
                                     % (opts["pool_size"], opts["model_top"]))
                meta = {"label": "荐股（全大盘）", "候选池": opts["pool_size"],
                        "送模型": opts["model_top"], "排除": opts["exclude"]}
                label = "荐股（全大盘：候选池 %d 只 → 送模型 %d 只 → 1 次模型调用）" % (
                    opts["pool_size"], opts["model_top"])
                job = JOBS.start(kind, [], meta, label=label,
                                 func=lambda log, ctl: run_pick(log, ctl, opts))
                return self._json({"ok": True, "id": job["id"], "命令": job["命令"], "清理": fresh,
                                   "参数": meta})
            elif kind == "plancheck":
                report_rel = (body.get("report") or "").strip()
                if not report_rel:
                    return self._err("缺少 report（报告相对路径）")
                opts = {"report": report_rel,
                        "profile": (body.get("profile") or "").strip() or None,
                        "model_pro": (body.get("model_pro") or "").strip() or None,
                        "api_base": (body.get("api_base") or "").strip() or None,
                        "api_key": (body.get("api_key") or "").strip() or None}
                meta = {"label": "实盘复核点评", "报告": report_rel, "profile": opts["profile"]}
                job = JOBS.start(kind, [], meta, label="实盘复核点评（1 次模型调用）",
                                 func=lambda log, ctl: run_plan_check(log, ctl, opts))
                return self._json({"ok": True, "id": job["id"], "命令": job["命令"], "清理": fresh,
                                   "参数": meta})
            elif kind == "holdings_sync":
                py = holdings_python()
                if not py:
                    return self._err(install_hint())
                argv = [py, "-X", "utf8", "-m", "webui.holdings_sync", "sync", "--no-captcha-prompt"]
                meta = {"label": "同步同花顺持仓", "只读": True}
                job = JOBS.start(kind, argv, meta, label="同步同花顺持仓（只读）")
                return self._json({"ok": True, "id": job["id"], "命令": job["命令"], "清理": fresh})
            elif kind == "track":
                codes = [str(c).strip() for c in (body.get("codes") or []) if str(c).strip()]
                if not codes:
                    return self._err("没有可生成的标的：先在跟踪页勾选标的")
                opts = {"codes": codes, "date": (body.get("date") or "").strip() or None,
                        "profile": (body.get("profile") or "").strip() or None,
                        "model_pro": (body.get("model_pro") or "").strip() or None,
                        "api_base": (body.get("api_base") or "").strip() or None,
                        "api_key": (body.get("api_key") or "").strip() or None,
                        "review": bool(body.get("review")),
                        "max_chars": body.get("max_chars")}
                meta = {"label": "标的跟踪", "codes": codes, "profile": opts["profile"],
                        "复核": opts["review"]}
                label = ("生成每日计划（%d 只%s）"
                         % (len(codes), "，含复核档" if opts["review"] else "，1 次模型调用/只"))
                job = JOBS.start(kind, [], meta, label=label,
                                 func=lambda log, ctl: run_track(log, ctl, opts))
                return self._json({"ok": True, "id": job["id"], "命令": job["命令"], "清理": fresh,
                                   "参数": meta})
            elif kind in ("flow_plan", "flow_check"):
                job, err = self._flow_job(kind, body)
                if err:
                    return self._err(err)
                return self._json({"ok": True, "id": job["id"], "命令": job["命令"], "清理": fresh})
            else:
                return self._err("未知任务类型：%s" % kind)
            job = JOBS.start(kind, argv, meta)
            return self._json({"ok": True, "id": job["id"], "命令": job["命令"], "清理": fresh})
        m = re.match(r"^/api/jobs/([^/]+)/cancel$", path)
        if m:
            ok = JOBS.cancel(m.group(1))
            return self._json({"ok": ok})
        return self._err("未知接口：%s" % path, 404)

    def _flow_job(self, kind, body):
        return flowapi.start_job(kind, body)

    def _save_json_config(self, body, path, loader):
        text = body.get("text")
        if not isinstance(text, str):
            return self._err("缺少 text")
        try:
            parsed = json.loads(text)
        except ValueError as e:
            return self._err("不是合法 JSON，未写入：%s" % e)
        if not isinstance(parsed, dict):
            return self._err("顶层必须是 JSON 对象")
        if path == MODELS_PATH:
            if not isinstance(parsed.get("providers"), list) or not parsed.get("providers"):
                return self._err("models.providers 不能为空")
            if not isinstance(parsed.get("profiles"), dict) or not parsed.get("profiles"):
                return self._err("models.profiles 不能为空")
        if path == ACCOUNT_PATH:
            if not num(parsed.get("总资金")):
                return self._err("「总资金」必须填写且大于 0")
        written, bkp = save_like(path, json.dumps(parsed, ensure_ascii=False, indent=1))
        return self._json({"ok": True, "已写入": written,
                           "备份": rel(bkp) if bkp else None, "配置": loader()})

    # ---- 静态文件 ----
    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        elif path in ("/m", "/m/"):
            path = "/index.html"
        rel_path = path.lstrip("/")
        target = os.path.abspath(os.path.join(STATIC_DIR, rel_path))
        if not inside(target, STATIC_DIR) or not os.path.isfile(target):
            return self._send(404, "404 Not Found\n", "text/plain; charset=utf-8")
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as f:
            body = f.read()
        self._send(200, body, MIME.get(ext, "application/octet-stream"))


class LocalServer(ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 允许两个进程绑同一端口（请求会随机落到旧实例，
    # 表现为「改了代码却不生效」），因此本机显式关闭地址复用。
    allow_reuse_address = (os.name != "nt")
    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, password=None):
        self.password = password
        self.session_token = secrets.token_urlsafe(32) if password else None
        super().__init__(server_address, RequestHandlerClass)

    def handle_error(self, request, client_address):
            """客户端断连不算服务端异常：不打 traceback，其余照旧交给父类。"""
            exc = sys.exc_info()[1]
            if exc is None or is_disconnect(exc):
                return
            super().handle_error(request, client_address)


def pick_port(host, port, password=None):
    for candidate in range(port, port + 20):
        try:
            srv = LocalServer((host, candidate), Handler, password=password)
            return candidate, srv
        except OSError:
            continue
    raise SystemExit("端口 %d-%d 都被占用，请用 --port 指定其它端口" % (port, port + 20))


def _is_loopback_host(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _lan_ips():
    """返回可用于局域网访问的本机 IPv4 地址，优先默认路由接口。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if not ip.startswith("127."):
                return [ip]
        finally:
            sock.close()
    except OSError:
        pass
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except OSError:
        pass
    return found


def serve(host="127.0.0.1", port=8765, open_browser=True, password=None):
    """启动本地控制台（阻塞直到 Ctrl+C）。参数与 argparse 入口解耦，方便测试。"""
    if not os.path.exists(AIPLAN):
        raise SystemExit("找不到 aiplan.py：%s" % AIPLAN)
    lan_source = None
    if password is None and not _is_loopback_host(host):
        password, lan_source = resolve_lan_password(password)
    port, httpd = pick_port(host, port, password=password)
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::", "localhost", "") else host
    local_url = "http://%s:%d/" % (display_host, port)
    if host in ("0.0.0.0", "::"):
        lan_urls = ["http://%s:%d/" % (ip, port) for ip in _lan_ips()]
    else:
        lan_urls = []
    purge_trash_expired(force=True, log=lambda m: print(m))
    print("[OK] aiplan Web UI  ->  %s" % local_url)
    print("[MOBILE] 手机专用页面 ->  %sm" % local_url)
    for url in lan_urls:
        print("[LAN] 同局域网设备 ->  %s" % url)
        print("[LAN] 手机专用页面 ->  %sm" % url)
    if password:
        print("[安全] 访问密码：%s（来源：%s）" % (password, lan_source or "--password"))
        print("[安全] 会话最长保持 7 天；可把口令写进 %s 或环境变量 %s"
              % (rel(LAN_PASSWORD_FILE), LAN_PASSWORD_ENV))
    print("     root: %s" % ROOT.encode("ascii", "replace").decode("ascii"))
    print("     python: %s" % PYTHON.encode("ascii", "replace").decode("ascii"))
    print("     Ctrl+C to stop")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(local_url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[..] 正在停止 ...")
    finally:
        httpd.server_close()
    return 0
