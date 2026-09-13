# -*- coding: utf-8 -*-
"""交易流的 HTTP 入口：路由实现集中在这里，webserver 只做分派（它已经贴着 800 行上限）。

handler 只负责 _json / _err，本模块不 import webserver，避免循环依赖。
"""

import time

from . import flow as flow_store
from . import flowview
from . import quotes as quotes_mod
from .flow_run import run_check as run_flow_check
from .flow_run import run_plan as run_flow_plan
from .jobs import JOBS
from .paths import aiplan, num, rel
from .store import load_account_bundle


def start_job(kind, body):
    """创建交易流任务（kind = flow_plan 重算计划 / flow_check 体检）。"""
    fid = (body.get("流编号") or "").strip()
    if not fid:
        return None, "缺少 流编号"
    opts = {"流编号": fid,
            "profile": (body.get("profile") or "").strip() or None,
            "model_pro": (body.get("model_pro") or "").strip() or None,
            "api_base": (body.get("api_base") or "").strip() or None,
            "api_key": (body.get("api_key") or "").strip() or None,
            "review": bool(body.get("review")),
            "max_chars": body.get("max_chars"),
            "补充说明": (body.get("补充说明") or "").strip() or None,
            "先抓": (body.get("先抓") or "").strip() or None}
    meta = {"label": "交易流计划" if kind == "flow_plan" else "交易流体检", "流编号": fid}
    if kind == "flow_plan":
        label = "重算交易计划（%s，1 次模型调用）" % fid
        func = lambda log, ctl: run_flow_plan(log, ctl, opts)          # noqa: E731
    else:
        label = "体检：意外排查（%s%s）" % (fid, "，先抓数据" if opts["先抓"] else "")
        func = lambda log, ctl: run_flow_check(log, ctl, opts)         # noqa: E731
    job = JOBS.start(kind, [], meta, label=label, func=func)
    return job, None


def handle_get(handler, path, q):
    """GET /api/flows 与 /api/flow；命中返回 True（已经写过响应）。"""
    if path == "/api/flows":
        refresh = (q.get("refresh", ["0"])[0] or "0") in ("1", "true", "yes")
        codes = [aiplan.code6((d.get("标的") or {}).get("代码"))
                 for d in flow_store.list_flows(active_only=True)]
        quote_map, hints = ({}, [])
        if refresh and codes:
            quote_map, hints = quotes_mod.fetch_quotes(codes, refresh=True)
        data = flowview.overview(quote_map, refresh=refresh)
        data["刷新"] = {"模式": "实时" if refresh else "本地",
                        "时间": time.strftime("%H:%M:%S"),
                        "来源": "东财批量报价" if refresh else "本地缓存",
                        "报价条数": len(quote_map), "提示": hints}
        handler._json(data)
        return True
    if path == "/api/flow":
        fid = (q.get("id", [""])[0] or "").strip()
        if not fid:
            handler._err("缺少 id（交易流编号）", 400)
            return True
        refresh = (q.get("refresh", ["0"])[0] or "0") in ("1", "true", "yes")
        doc, err = flow_store.load_flow(fid)
        if err:
            handler._err(err, 404)
            return True
        quote_map = {}
        if refresh:
            c6 = aiplan.code6((doc.get("标的") or {}).get("代码"))
            quote_map, _hints = quotes_mod.fetch_quotes([c6], refresh=True)
        data, err = flowview.detail(fid, quote_map=quote_map, refresh=refresh)
        if err:
            handler._err(err, 404)
            return True
        handler._json(data)
        return True
    return False


def handle_post(handler, path, body):
    """交易流的写入口（开流 / 成交 / 同步 / 结束 / 删除 / 设置 / 任务）；命中返回 True。"""
    if path == "/api/flows":
        account = load_account_bundle().get("配置") or {}
        doc, err = flow_store.create_flow(
            body.get("code"), body.get("name"), body.get("本流资金"),
            body.get("目标收益率_pct"), body.get("最大亏损_pct"),
            body.get("最大加仓次数"), start=body.get("起始持仓"),
            note=body.get("备注"), account_total=num(account.get("总资金")))
        if err:
            handler._err(err)
            return True
        handler._json({"ok": True, "流编号": doc["流编号"], "流": flowview.card(doc),
                       "列表": flowview.overview(with_closed=False)})
        return True
    if path == "/api/flow/fill":
        doc, err = flow_store.load_flow(body.get("流编号"))
        if err:
            handler._err(err, 404)
            return True
        fill, err = flow_store.add_fill(doc, body.get("方向"), body.get("价格"),
                                        body.get("数量"), day=body.get("日期"),
                                        when=body.get("时间"), note=body.get("备注"),
                                        source="手工")
        if err:
            handler._err(err)
            return True
        flow_store.save_flow(doc)
        handler._json({"ok": True, "成交": fill, "卡": flowview.card(doc),
                       "持仓": doc.get("持仓"), "盈亏": doc.get("盈亏"),
                       "提示": doc.get("提示") or []})
        return True
    if path == "/api/flow/fill/delete":
        doc, err = flow_store.load_flow(body.get("流编号"))
        if err:
            handler._err(err, 404)
            return True
        doc, err = flow_store.delete_fill(doc, body.get("序号"))
        if err:
            handler._err(err)
            return True
        flow_store.save_flow(doc)
        handler._json({"ok": True, "卡": flowview.card(doc),
                       "持仓": doc.get("持仓"), "盈亏": doc.get("盈亏")})
        return True
    if path == "/api/flow/sync":
        fid = (body.get("流编号") or "").strip()
        if fid:
            doc, err = flow_store.load_flow(fid)
            if err:
                handler._err(err, 404)
                return True
            targets = [doc]
        else:
            targets = flow_store.list_flows(active_only=True)
        account = load_account_bundle().get("配置") or {}
        added = 0
        for doc in targets:
            added += flow_store.sync_ledger(doc, account=account)
            flow_store.save_flow(doc)
        handler._json({"ok": True, "新增": added, "流数": len(targets),
                       "列表": flowview.overview(with_closed=False)})
        return True
    if path == "/api/flow/close":
        doc, err = flow_store.load_flow(body.get("流编号"))
        if err:
            handler._err(err, 404)
            return True
        doc, err = flow_store.close_flow(doc, body.get("原因") or "人工结束",
                                         state=body.get("状态"))
        if err:
            handler._err(err)
            return True
        flow_store.save_flow(doc)
        handler._json({"ok": True, "卡": flowview.card(doc),
                       "列表": flowview.overview(with_closed=False)})
        return True
    if path == "/api/flow/settings":
        handler._json({"ok": True, "设置": flow_store.save_settings(body)})
        return True
    if path == "/api/flow/delete":
        result, err = flow_store.delete_flow(body.get("流编号"))
        if err:
            handler._err(err, 404)
            return True
        out = dict(result)
        out.update({"ok": True, "列表": flowview.overview(with_closed=False)})
        handler._json(out)
        return True
    if path in ("/api/flow/plan", "/api/flow/check"):
        kind = "flow_plan" if path.endswith("/plan") else "flow_check"
        job, err = start_job(kind, body)
        if err:
            handler._err(err)
            return True
        handler._json({"ok": True, "id": job["id"], "命令": job["命令"]})
        return True
    return False
