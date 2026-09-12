# -*- coding: utf-8 -*-
"""HTTP 层：路由分派、静态文件、本地服务。"""

from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs
from urllib.parse import unquote
from urllib.parse import urlparse
import json
import os
import re
import threading
import webbrowser

from .archive import delete_pick, delete_plan_log, delete_report, latest_pick_bundle, latest_report, list_picks, list_reports, pick_bundle, plan_log_entries, report_bundle
from . import alerts as alerts_store
from .holdings_sync import CAPTCHA_ROOT, captcha_image, holdings_python, install_hint, submit_captcha_answer
from .jobs import JOBS, build_check_argv, build_run_argv, run_batch
from .market import (build_market, build_state, build_symbols, latest_market_forecast,
                     load_kline, run_market_forecast)
from .overview import build_overview
from .paths import ACCOUNT_PATH, AIPLAN, DATA_DIR, MODELS_PATH, PICK_DIR, POOL_PATH, PYTHON, ROOT, STATIC_DIR, TRASH_DIR, TRASH_TTL_DAYS, WATCHLIST_PATH, inside, num, read_text, rel, save_like
from .pick import PICK_BOARD_TYPES, PICK_L1_INDUSTRIES, PICK_MAX_CANDIDATES, PICK_MAX_CONCEPTS, PICK_MODULES, PICK_PER_BOARD, pick_param
from .pick_run import pick_boards_bundle, pick_l1_subs, run_pick
from .plancheck import plancheck_bundle, run_plan_check
from .store import load_account_bundle, load_holdings_bundle, load_models_bundle, load_watchlist, watchlist_add, watchlist_remove
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


class Handler(BaseHTTPRequestHandler):
    server_version = "aiplan-webui/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    # ---- 基础输出 ----
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

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
        u = urlparse(self.path)
        path = unquote(u.path)
        q = parse_qs(u.query)
        try:
            if path.startswith("/api/"):
                return self._api_get(path, q)
            return self._static(path)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
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
            return self._json({"items": list_reports()})
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
            data = load_kline(q.get("code", [""])[0], q.get("limit", ["180"])[0])
            if not data:
                return self._err("没有该标的的日K缓存（data/history/）", 404)
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
            return self._json({"items": list_picks(),
                               "目录": rel(PICK_DIR),
                               "模块": [{"值": m, "周期": c} for m, c in PICK_MODULES],
                               "板块类型": list(PICK_BOARD_TYPES),
                               "每板块候选默认": PICK_PER_BOARD,
                               "候选上限默认": PICK_MAX_CANDIDATES,
                               "概念上限": PICK_MAX_CONCEPTS,
                               "一级行业数": len(PICK_L1_INDUSTRIES)})
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
        u = urlparse(self.path)
        path = unquote(u.path)
        try:
            body = self._body()
        except ValueError as e:
            return self._err("请求体不是合法 JSON：%s" % e)
        try:
            return self._api_post(path, body)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
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
                return self._json({"ok": True, "id": job["id"], "命令": job["命令"], "标的": codes})
            elif kind == "market":
                prof = body.get("profile")
                argv = []
                meta = {"profile": prof, "label": "大盘走势预测"}
                job = JOBS.start(kind, argv, meta, label="生成大盘走势预测",
                                 func=lambda log, ctl: run_market_forecast(
                                     log, profile=prof, model_pro=body.get("model_pro"),
                                     api_base=body.get("api_base"), api_key=body.get("api_key")))
                return self._json({"ok": True, "id": job["id"], "命令": job["命令"]})
            elif kind == "pick":
                opts = pick_param(body)
                if not opts["industry"] and not opts["concepts"]:
                    return self._err("请先筛选行业或概念：至少勾选一个行业细分或一个概念板块")
                meta = {"label": "荐股", "模块": opts["modules"],
                        "行业": [x["code"] for x in opts["industry"]],
                        "概念": opts["concepts"], "排除": opts["exclude"]}
                label = ("荐股（行业 %d 个 / 概念 %d 个，每板块 %d 只，候选上限 %d）"
                         % (len(opts["industry"]), len(opts["concepts"]),
                            opts["per_board"], opts["max_candidates"]))
                job = JOBS.start(kind, [], meta, label=label,
                                 func=lambda log, ctl: run_pick(log, ctl, opts))
                return self._json({"ok": True, "id": job["id"], "命令": job["命令"],
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
                return self._json({"ok": True, "id": job["id"], "命令": job["命令"],
                                   "参数": meta})
            elif kind == "holdings_sync":
                py = holdings_python()
                if not py:
                    return self._err(install_hint())
                argv = [py, "-X", "utf8", "-m", "webui.holdings_sync", "sync"]
                meta = {"label": "同步同花顺持仓", "只读": True}
                job = JOBS.start(kind, argv, meta, label="同步同花顺持仓（只读）")
                return self._json({"ok": True, "id": job["id"], "命令": job["命令"]})
            else:
                return self._err("未知任务类型：%s" % kind)
            job = JOBS.start(kind, argv, meta)
            return self._json({"ok": True, "id": job["id"], "命令": job["命令"]})
        m = re.match(r"^/api/jobs/([^/]+)/cancel$", path)
        if m:
            ok = JOBS.cancel(m.group(1))
            return self._json({"ok": ok})
        return self._err("未知接口：%s" % path, 404)

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


def pick_port(host, port):
    for candidate in range(port, port + 20):
        try:
            srv = LocalServer((host, candidate), Handler)
            return candidate, srv
        except OSError:
            continue
    raise SystemExit("端口 %d-%d 都被占用，请用 --port 指定其它端口" % (port, port + 20))


def serve(host="127.0.0.1", port=8765, open_browser=True):
    """启动本地控制台（阻塞直到 Ctrl+C）。参数与 argparse 入口解耦，方便测试。"""
    if not os.path.exists(AIPLAN):
        raise SystemExit("找不到 aiplan.py：%s" % AIPLAN)
    port, httpd = pick_port(host, port)
    url = "http://%s:%d/" % (host, port)
    purge_trash_expired(force=True, log=lambda m: print(m))
    print("[OK] aiplan Web UI  ->  %s" % url)
    print("     root: %s" % ROOT.encode("ascii", "replace").decode("ascii"))
    print("     python: %s" % PYTHON.encode("ascii", "replace").decode("ascii"))
    print("     Ctrl+C to stop")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[..] 正在停止 ...")
    finally:
        httpd.server_close()
    return 0


