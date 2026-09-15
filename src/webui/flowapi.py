# -*- coding: utf-8 -*-
"""交易流的 HTTP 入口：路由实现集中在这里，webserver 只做分派（它已经贴着 800 行上限）。

handler 只负责 _json / _err，本模块不 import webserver，避免循环依赖。
一条流里可以有多只标的，所以「标的级」的写接口（成交 / 同步 / 结束 / 计划 / 体检）
统一用 `代码` 指定标的；只有一只标的时可以省略。
"""

import re
import time

from . import flow as flow_store
from . import flowview
from . import quotes as quotes_mod
from .flow_run import run_check as run_flow_check
from .flow_run import run_plan as run_flow_plan
from .jobs import JOBS
from .paths import aiplan, num, rel
from .store import load_account_bundle


def pick(body, *keys, **kw):
    """按多个键名取值（页面用中文键、脚本常用英文键，两种都得认）。

    这个函数的由来：`/api/flow/target/add` 曾经只读 `code`，而页面表单发的是 `代码`，
    结果服务端拿到空值、回了一句「请填 6 位证券代码」——六个数字明明填对了。
    """
    default = kw.get("default")
    for key in keys:
        value = (body or {}).get(key)
        if value not in (None, ""):
            return value
    return default


def codes_of(body):
    """请求里的标的代码：支持字符串 / 列表 / 逗号分隔。"""
    raw = pick(body, "代码", "code")
    if raw in (None, ""):
        return []
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[,，\s]+", str(raw))
    out = []
    for item in items:
        c6 = aiplan.code6(item or "")
        if c6 and c6 not in out:
            out.append(c6)
    return out


def pick_targets(doc, codes, active_only=False):
    """要操作的标的：给了代码就用代码；只给流编号时，只有一只标的才允许省略。"""
    nodes = flow_store.targets(doc)
    if codes:
        out = []
        for c6 in codes:
            node, err = flow_store.find_target(doc, c6)
            if err:
                return None, err
            out.append(node)
        return out, None
    pool = [n for n in nodes if n.get("状态") == flow_store.ACTIVE_STATE] if active_only else nodes
    if not pool:
        pool = nodes
    if len(pool) == 1:
        return pool, None
    if not pool:
        return None, "这条流里还没有标的"
    return None, "这条流里有 %d 只标的，请指明「代码」" % len(pool)


def start_job(kind, body):
    """创建交易流任务（kind = flow_plan 重算计划 / flow_check 体检）。"""
    fid = (body.get("流编号") or "").strip()
    if not fid:
        return None, "缺少 流编号"
    codes = codes_of(body)
    if kind == "flow_check" and not codes:
        doc, err = flow_store.load_flow(fid)
        if err:
            return None, err
        nodes, err = pick_targets(doc, [], active_only=True)
        if err:
            return None, err
        codes = [aiplan.code6(n.get("代码") or "") for n in nodes]
    opts = {"流编号": fid, "代码": codes,
            "profile": (body.get("profile") or "").strip() or None,
            "model_pro": (body.get("model_pro") or "").strip() or None,
            "api_base": (body.get("api_base") or "").strip() or None,
            "api_key": (body.get("api_key") or "").strip() or None,
            "review": bool(body.get("review")),
            "max_chars": body.get("max_chars"),
            "补充说明": (body.get("补充说明") or "").strip() or None,
            "先抓": (body.get("先抓") or "").strip() or None}
    meta = {"label": "交易流计划" if kind == "flow_plan" else "交易流体检", "流编号": fid,
            "代码": codes}
    if kind == "flow_plan":
        label = "重算交易计划（%s%s，每只标的 1 次模型调用）" % (fid, "".join(" " + c for c in codes))
        func = lambda log, ctl: run_flow_plan(log, ctl, opts)          # noqa: E731
    else:
        label = "体检：意外排查（%s %s%s）" % (fid, "、".join(codes) or "—",
                                          "，先抓数据" if opts["先抓"] else "")
        func = lambda log, ctl: run_flow_check(log, ctl, opts)         # noqa: E731
    job = JOBS.start(kind, [], meta, label=label, func=func)
    return job, None


def handle_get(handler, path, q):
    """GET /api/flows 与 /api/flow；命中返回 True（已经写过响应）。"""
    if path == "/api/flows":
        refresh = (q.get("refresh", ["0"])[0] or "0") in ("1", "true", "yes")
        codes = [aiplan.code6((n or {}).get("代码")) for d in flow_store.list_flows(active_only=True)
                 for n in flow_store.targets(d)]
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
        code = (q.get("code", [""])[0] or "").strip()
        doc, err = flow_store.load_flow(fid)
        if err:
            handler._err(err, 404)
            return True
        quote_map = {}
        if refresh:
            c6s = [aiplan.code6(n.get("代码")) for n in flow_store.targets(doc)]
            quote_map, _hints = quotes_mod.fetch_quotes(c6s, refresh=True)
        data, err = flowview.detail(fid, quote_map=quote_map, refresh=refresh, code=code)
        if err:
            handler._err(err, 404)
            return True
        handler._json(data)
        return True
    return False


def handle_post(handler, path, body):
    """交易流的写入口（开流 / 加标的 / 成交 / 同步 / 结束 / 删除 / 设置 / 任务）；命中返回 True。"""
    if path == "/api/flows":
        account = load_account_bundle().get("配置") or {}
        doc, err = flow_store.create_flow(
            body.get("流资金"), body.get("目标收益率_pct"), body.get("最大亏损_pct"),
            note=body.get("备注"), code=pick(body, "code", "代码"),
            name=pick(body, "name", "名称"),
            style=pick(body, "打法", "style"), alloc=pick(body, "分配资金", "alloc"),
            start=pick(body, "起始持仓", "start"),
            account_total=num(account.get("总资金")))
        if err:
            handler._err(err)
            return True
        handler._json({"ok": True, "流编号": doc["流编号"], "流": flowview.flow_card(doc),
                       "列表": flowview.overview(with_closed=False)})
        return True
    if path == "/api/flow/target/add":
        doc, err = flow_store.load_flow(body.get("流编号"))
        if err:
            handler._err(err, 404)
            return True
        node, err = flow_store.add_target(doc, pick(body, "代码", "code"),
                                          pick(body, "名称", "name"),
                                          style=pick(body, "打法", "style"),
                                          alloc=pick(body, "分配资金", "alloc"),
                                          start=pick(body, "起始持仓", "start"))
        if err:
            handler._err(err)
            return True
        handler._json({"ok": True, "标的": flowview.target_card(doc, node),
                       "卡": flowview.flow_card(doc),
                       "列表": flowview.overview(with_closed=False)})
        return True
    if path == "/api/flow/target/remove":
        doc, err = flow_store.load_flow(body.get("流编号"))
        if err:
            handler._err(err, 404)
            return True
        doc, err = flow_store.remove_target(doc, pick(body, "代码", "code"))
        if err:
            handler._err(err)
            return True
        handler._json({"ok": True, "卡": flowview.flow_card(doc),
                       "列表": flowview.overview(with_closed=False)})
        return True
    if path == "/api/flow/target/set":
        doc, err = flow_store.load_flow(body.get("流编号"))
        if err:
            handler._err(err, 404)
            return True
        doc, err = flow_store.set_target(doc, pick(body, "代码", "code"),
                                         style=pick(body, "打法", "style"),
                                         alloc=pick(body, "分配资金", "alloc"))
        if err:
            handler._err(err)
            return True
        handler._json({"ok": True, "卡": flowview.flow_card(doc),
                       "列表": flowview.overview(with_closed=False)})
        return True
    if path == "/api/flow/params":
        doc, err = flow_store.load_flow(body.get("流编号"))
        if err:
            handler._err(err, 404)
            return True
        doc, err = flow_store.set_params(doc, capital=body.get("流资金"),
                                         target_pct=body.get("目标收益率_pct"),
                                         loss_pct=body.get("最大亏损_pct"),
                                         note=body.get("备注"))
        if err:
            handler._err(err)
            return True
        handler._json({"ok": True, "卡": flowview.flow_card(doc),
                       "列表": flowview.overview(with_closed=False)})
        return True
    if path == "/api/flow/fill":
        doc, err = flow_store.load_flow(body.get("流编号"))
        if err:
            handler._err(err, 404)
            return True
        nodes, err = pick_targets(doc, codes_of(body), active_only=True)
        if err:
            handler._err(err)
            return True
        node = nodes[0]
        fill, err = flow_store.add_fill(node, body.get("方向"), body.get("价格"),
                                        body.get("数量"), day=body.get("日期"),
                                        when=body.get("时间"), note=body.get("备注"),
                                        source="手工")
        if err:
            handler._err(err)
            return True
        flow_store.save_flow(doc)
        handler._json({"ok": True, "成交": fill, "卡": flowview.target_card(doc, node),
                       "流卡": flowview.flow_card(doc), "代码": node.get("代码"),
                       "持仓": node.get("持仓"), "盈亏": node.get("盈亏"),
                       "提示": node.get("提示") or []})
        return True
    if path == "/api/flow/fill/delete":
        doc, err = flow_store.load_flow(body.get("流编号"))
        if err:
            handler._err(err, 404)
            return True
        nodes, err = pick_targets(doc, codes_of(body))
        if err:
            handler._err(err)
            return True
        node = nodes[0]
        node, err = flow_store.delete_fill(node, body.get("序号"))
        if err:
            handler._err(err)
            return True
        flow_store.save_flow(doc)
        handler._json({"ok": True, "卡": flowview.target_card(doc, node),
                       "流卡": flowview.flow_card(doc), "代码": node.get("代码"),
                       "持仓": node.get("持仓"), "盈亏": node.get("盈亏")})
        return True
    if path == "/api/flow/sync":
        account = load_account_bundle().get("配置") or {}
        fid = (body.get("流编号") or "").strip()
        codes = codes_of(body)
        pairs = []
        if fid:
            doc, err = flow_store.load_flow(fid)
            if err:
                handler._err(err, 404)
                return True
            if codes:                       # 不传代码 = 同步这条流里所有标的（与页面一致）
                nodes, err = pick_targets(doc, codes)
                if err:
                    handler._err(err)
                    return True
            else:
                nodes = flow_store.targets(doc)
            pairs = [(doc, node) for node in nodes]
        else:
            pairs = [(doc, node) for doc in flow_store.list_flows(active_only=True)
                     for node in flow_store.targets(doc)]
        added = 0
        for doc, node in pairs:
            added += flow_store.sync_ledger(node, account=account)
            flow_store.save_flow(doc)
        handler._json({"ok": True, "新增": added, "标的数": len(pairs),
                       "列表": flowview.overview(with_closed=False)})
        return True
    if path == "/api/flow/close":
        doc, err = flow_store.load_flow(body.get("流编号"))
        if err:
            handler._err(err, 404)
            return True
        codes = codes_of(body)
        code = codes[0] if codes else None
        doc, err = flow_store.close_flow(doc, body.get("原因") or "人工结束",
                                         state=body.get("状态"), code=code)
        if err:
            handler._err(err)
            return True
        handler._json({"ok": True, "卡": flowview.flow_card(doc),
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
