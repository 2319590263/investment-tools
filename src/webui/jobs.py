# -*- coding: utf-8 -*-
"""后台任务（子进程）管理：启动 / 增量日志 / 中断 / 诊断 / 批量编排。"""

import os
import subprocess
import threading
import time

from .archive import ai_files, latest_report
from .paths import JOB_KEEP, PHASES, PYTHON, ROOT, RUNNER, aiplan, now_str, rel
from .sources import newest_file, stock3d_snapshot


class JobManager:
    def __init__(self):
        self._jobs = {}
        self._order = []
        self._lock = threading.Lock()
        self._seq = 0

    def start(self, kind, argv, meta=None, func=None, label=None):
        with self._lock:
            self._seq += 1
            jid = "job%d" % self._seq
            job = {
                "id": jid,
                "kind": kind,
                "argv": argv,
                "func": func,
                "命令": label or " ".join(_display_arg(a) for a in argv),
                "meta": meta or {},
                "status": "running",
                "exit_code": None,
                "lines": [],
                "result": {},
                "started_at": now_str(),
                "started_ts": time.time(),
                "finished_at": None,
                "elapsed": 0.0,
                "proc": None,
            }
            self._jobs[jid] = job
            self._order.append(jid)
            while len(self._order) > JOB_KEEP:
                old = self._order.pop(0)
                self._jobs.pop(old, None)
        t = threading.Thread(target=self._run, args=(jid,), daemon=True)
        t.start()
        return job

    def _append(self, job, text):
        job["lines"].append({
            "n": len(job["lines"]),
            "t": time.strftime("%H:%M:%S"),
            "text": text.rstrip("\r\n"),
        })

    def _run(self, jid):
        job = self._jobs.get(jid)
        if not job:
            return
        if job.get("func"):
            return self._run_callable(job)
        env = child_env()
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            proc = subprocess.Popen(
                job["argv"], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=creationflags,
            )
        except OSError as e:
            self._append(job, "[FAIL] 无法启动进程：%s" % e)
            job["status"] = "failed"
            job["exit_code"] = -1
            job["finished_at"] = now_str()
            return
        job["proc"] = proc
        try:
            for line in proc.stdout:
                self._append(job, line)
        except Exception as e:  # noqa: BLE001
            self._append(job, "[WARN] 读取输出中断：%s" % e)
        code = proc.wait()
        job["exit_code"] = code
        job["elapsed"] = round(time.time() - job["started_ts"], 1)
        job["finished_at"] = now_str()
        if job["status"] == "running":
            job["status"] = "done" if code == 0 else "failed"
        self._collect(job, code)
        tip = diagnose_job(job)
        if tip:
            job["result"]["诊断"] = tip

    def _run_callable(self, job):
        """服务进程内的任务（大盘预测 / 批量研判）：用回调把日志写进 job。"""
        ctl = {"cancel": False, "proc": None}
        job["ctl"] = ctl

        def log(text):
            self._append(job, text)
        try:
            result = job["func"](log, ctl)
            if isinstance(result, dict) and result.get("__canceled__"):
                job["result"].update({k: v for k, v in result.items() if k != "__canceled__"})
                job["status"] = "canceled"
                job["exit_code"] = None
                self._append(job, "[WARN] 任务已取消，未写入产物")
                return
            job["result"].update(result or {})
            job["exit_code"] = 0
            job["status"] = "done"
        except Exception as e:  # noqa: BLE001
            self._append(job, "[FAIL] %s" % e)
            job["exit_code"] = 3
            job["status"] = "failed"
        finally:
            job["elapsed"] = round(time.time() - job["started_ts"], 1)
            job["finished_at"] = now_str()
            tip = diagnose_job(job)
            if tip:
                job["result"]["诊断"] = tip

    def _collect(self, job, code):
        """跑完后定位产物，省得前端再猜文件名。"""
        meta = job["meta"] or {}
        phase = meta.get("phase")
        if job["kind"] != "run" or not phase:
            return
        if meta.get("dry_run"):
            packs = ai_files(r"^\d{6}_%s\.factpack\.md$" % phase)
            pack = newest_file(packs)
            if pack and os.stat(pack).st_mtime + 2 >= job["started_ts"]:
                job["result"]["factpack"] = rel(pack)
                job["result"]["factpack_path"] = pack
            return
        path = latest_report(phase)
        if not path:
            return
        try:
            fresh = os.stat(path).st_mtime + 2 >= job["started_ts"]
        except OSError:
            fresh = False
        # 模型调用失败时 aiplan 仍会落盘报告（退出码 3）—— 只要本次跑出来就回传
        if fresh:
            job["result"]["report"] = rel(path)
            job["result"]["report_path"] = path
            job["result"]["report_ok"] = (code == 0)

    def get(self, jid, start=0):
        job = self._jobs.get(jid)
        if not job:
            return None
        lines = job["lines"]
        return {
            "id": job["id"],
            "kind": job["kind"],
            "命令": job["命令"],
            "meta": job["meta"],
            "status": job["status"],
            "exit_code": job["exit_code"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "elapsed": job["elapsed"] if job["status"] != "running" else round(time.time() - job["started_ts"], 1),
            "result": job["result"],
            "lines": lines[start:],
            "next": len(lines),
            "总数": len(lines),
        }

    def cancel(self, jid):
        job = self._jobs.get(jid)
        if not job or job["status"] != "running":
            return False
        ctl = job.get("ctl")
        if ctl is not None:
            ctl["cancel"] = True
        proc = job.get("proc") or (ctl or {}).get("proc")
        if not proc:
            if ctl is not None:
                self._append(job, "[WARN] 已请求中断，当前步骤结束后停止")
                return True
            return False
        job["status"] = "canceled"
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                proc.terminate()
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self._append(job, "[WARN] 已被用户中断")
        return True


def child_env():
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _display_arg(a):
    a = str(a)
    if len(a) > 60:
        a = a[:30] + "…" + a[-24:]
    return ('"%s"' % a) if (" " in a or "\\" in a) else a


def diagnose_job(job):
    """把 aiplan 的 ASCII 化输出翻成一句能看懂的中文（stdout 里的中文已被替换成 ?）。"""
    rc = job.get("exit_code")
    meta = job.get("meta") or {}
    kind = job.get("kind")
    if kind == "check":
        if rc == 0:
            return "研判档与复核档都连通。"
        return "自检失败：检查该 profile 的 provider、base_url 与 API Key。"
    if kind == "market":
        if rc == 0:
            return None
        return "大盘预测失败：检查模型 Key、余额与网络；日志里 [FAIL] 一行是原始原因。"
    if kind == "pick":
        if rc in (None, 0):
            return None
        return ("荐股失败：常见原因是网络/东财限流或模型 Key、余额；"
                "日志里 [FAIL]/[WARN] 一行是原始原因。")
    if kind == "track":
        if rc in (None, 0):
            return None
        return ("标的跟踪失败：检查模型 Key/余额与网络；若日志里写「未录入上一份计划的执行情况」，"
                "先在跟踪页把上一份计划的执行记录填好再生成。")
    if kind in ("flow_plan", "flow_check"):
        if rc in (None, 0):
            return None
        if kind == "flow_check":
            return ("体检失败：检查模型 Key/余额与网络；若日志里写「抓数超时/失败」，"
                    "先单独跑一次 python stock3d.py news <代码> 看是不是消息面源不可用。")
        return ("重算计划失败：检查模型 Key/余额与网络；流已结束或未开流会被直接拒绝。")
    if rc in (None, 0):
        return None
    code6_ = aiplan.code6(meta.get("code") or "")
    if rc == 4:
        _p, doc, avail = stock3d_snapshot()
        tip = "数据缺失（退出码 4）：选中标的的数据不在本地文件里。"
        if avail:
            tip += "最新 stock3d 快照（%s）只含：%s。" % ((doc or {}).get("target_date") or "—", "、".join(avail))
        if meta.get("no_fetch"):
            tip += ("现在开着「复用已有数据」，不会重新抓数，所以直接失败。"
                    "关掉它再跑一次即可先抓数；或先手工执行 python stock3d.py pull %s。" % (code6_ or "代码"))
        else:
            tip += "抓数没取到该标的，确认代码是否正确（6 位代码或 002463.SZ 形式）。"
        return tip
    if rc == 3:
        return ("模型调用失败（退出码 3）：检查 API Key、账户余额与网络；"
                "也可以先用「只看不花钱」确认事实包能正常生成。报告仍会落盘并标注 [FAIL]。")
    if rc == 2:
        return ("参数或配置错误（退出码 2）：检查账户配置的「总资金」是否填写、"
                "模型配置里的 profile 与 providers 是否完整。")
    return "运行失败（退出码 %s），原因见上方输出。" % rc


JOBS = JobManager()


def build_pan_argv():
    """一键抓大盘快照：跑根目录 pan.py post（落 data/pan/<今天>/，只读、不调模型）。"""
    return [PYTHON, "-X", "utf8", os.path.join(ROOT, "pan.py"), "post"]


def stream_argv(log, ctl, argv):
    """跑一个子进程并把输出逐行写进任务日志（返回退出码）。取消时杀掉它。"""
    proc = subprocess.Popen(argv, cwd=ROOT, env=child_env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)
    ctl["proc"] = proc
    rc = None
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if ctl.get("cancel"):
                proc.terminate()
                break
            if line.strip():
                log("    " + line)
    except Exception:                      # noqa: BLE001  读输出被打断按失败处理
        proc.terminate()
    finally:
        try:
            rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
        ctl["proc"] = None
    return rc


def run_pan_and_pick(log, ctl, pick_opts):
    """批注 5：抓大盘快照（pan post）**顺带**跑一轮荐股，给大盘快照页一份四档推荐。

    两步串行、日志连着看：pan 不能失败（失败直接报错），荐股失败不影响 pan 的产物，
    只在结果里带 error 让页面如实标出来。
    """
    from .pick_run import run_pick
    log("[..] 第一步：抓大盘快照（python main.py pan post，只读、不调模型）")
    rc = stream_argv(log, ctl, build_pan_argv())
    if ctl.get("cancel"):
        return {"__canceled__": True}
    log("[%s] pan 结束（退出码 %s）" % ("OK" if rc == 0 else "WARN", rc))
    if rc not in (0, None):
        return {"error": "大盘快照抓取失败（退出码 %s）：荐股这一步没有跑" % rc}
    log("[..] 第二步：荐股（全大盘扫描 → 机械打分 → 前 %d 只交模型；这一步会花模型钱）"
        % pick_opts.get("model_top", 0))
    try:
        out = run_pick(log, ctl, pick_opts)
    except Exception as exc:               # noqa: BLE001  荐股失败不该让 pan 白跑
        log("[FAIL] 荐股失败：%s" % exc)
        return {"error": "荐股失败：%s" % str(exc)[:200]}
    if isinstance(out, dict) and out.get("__canceled__"):
        return {"__canceled__": True}
    log("[OK] 大盘快照 + 荐股都完成：四档推荐见大盘快照页「打法推荐」卡")
    return {"pan_ok": True, "荐股": out}


def start_pan_job(body):
    """大盘快照任务：默认只跑 pan post；body.pick=true 时同一任务里再跑一轮荐股（批注 5）。"""
    body = body or {}
    if not body.get("pick"):
        return JOBS.start("pan", build_pan_argv(), {"label": "抓取大盘快照"},
                          label="抓取大盘快照（pan post）")
    from .pick import pick_param            # 放函数里：避免 jobs ↔ pick 的导入环
    opts = pick_param({})
    meta = {"label": "抓大盘 + 荐股", "候选池": opts["pool_size"],
            "送模型": opts["model_top"], "排除": opts["exclude"]}
    label = ("抓大盘快照 + 荐股（候选池 %d 只 → 送模型 %d 只 → 1 次模型调用）"
             % (opts["pool_size"], opts["model_top"]))
    return JOBS.start("pan", [], meta, label=label,
                      func=lambda log, ctl: run_pan_and_pick(log, ctl, opts))


def start_market_job(kind, body):
    """批注 1 的大盘评分任务：

    · kind="mkt_score"：机械评分（全量/快速版），不调模型、零成本；
    · kind="mkt_read" ：模型盲评（0-100 分）+ 精简解读，1 次模型调用。
    """
    body = body or {}
    from .market import run_market_read, run_market_score     # 放函数里：避免导入环
    if kind == "mkt_score":
        full = str(body.get("full", "1")).strip() not in ("0", "false", "False")
        refresh = bool(body.get("refresh"))
        meta = {"label": "大盘评分", "全量": full, "强制重取": refresh}
        return JOBS.start(kind, [], meta,
                          label="计算大盘评分（%s%s）"
                                % ("全量" if full else "快速版", "，强制重取" if refresh else ""),
                          func=lambda log, ctl: run_market_score(log, ctl, full, refresh))
    prof = body.get("profile")
    meta = {"label": "大盘评分解读", "profile": prof, "model_pro": body.get("model_pro")}
    return JOBS.start(kind, [], meta, label="生成大盘评分解读（1 次模型调用）",
                      func=lambda log, ctl: run_market_read(
                          log, profile=prof, model_pro=body.get("model_pro"),
                          api_base=body.get("api_base"), api_key=body.get("api_key")))


def build_run_argv(body):
    phase = (body.get("phase") or "post").strip()
    if phase not in PHASES:
        raise ValueError("phase 必须是 prep/live/post/all 之一")
    code = (body.get("code") or "").strip()
    if not code:
        raise ValueError("必须指定标的代码（6 位代码或 thscode）")
    argv = [PYTHON, RUNNER, phase, "--code", code]
    opts = [
        ("name", "--name"), ("date", "--date"), ("pool", "--pool"),
        ("account", "--account"), ("models", "--models"), ("profile", "--profile"),
        ("phase_profile", "--phase-profile"), ("api_key", "--api-key"),
        ("api_base", "--api-base"), ("model_pro", "--model-pro"),
        ("model_review", "--model-review"),
    ]
    for key, flag in opts:
        v = body.get(key)
        if v is not None and str(v).strip() != "":
            argv += [flag, str(v).strip()]
    for key, flag in (("top", "--top"), ("max_input_chars", "--max-input-chars")):
        v = body.get(key)
        if v is not None and str(v).strip() != "":
            try:
                argv += [flag, str(int(v))]
            except (TypeError, ValueError):
                raise ValueError("%s 必须是整数" % key)
    for key, flag in (("no_fetch", "--no-fetch"), ("no_review", "--no-review"),
                      ("dry_run", "--dry-run"), ("debug", "--debug")):
        if body.get(key):
            argv.append(flag)
    return argv


def build_check_argv(body):
    argv = [PYTHON, RUNNER, "check-model"]
    if body.get("profile"):
        argv += ["--profile", str(body["profile"]).strip()]
    if body.get("phase"):
        argv += ["--phase", str(body["phase"]).strip()]
    for key, flag in (("api_key", "--api-key"), ("api_base", "--api-base"),
                      ("model_pro", "--model-pro"), ("model_review", "--model-review")):
        v = body.get(key)
        if v is not None and str(v).strip() != "":
            argv += [flag, str(v).strip()]
    return argv


def run_batch(log, ctl, codes, phase, opts):
    """按顺序对多只标的跑研判。单只失败不中断，跑完给汇总；支持中途取消。"""
    results, total = [], len(codes)
    for i, code in enumerate(codes, 1):
        if ctl.get("cancel"):
            log("[WARN] 已取消，剩余 %d 只未执行" % (total - i + 1))
            break
        argv = [PYTHON, RUNNER, phase, "--code", code] + list(opts)
        log("[..] (%d/%d) %s 开始" % (i, total, code))
        t0 = time.time()
        try:
            proc = subprocess.Popen(argv, cwd=ROOT, env=child_env(),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace", bufsize=1)
        except OSError as e:
            log("[FAIL] %s 无法启动：%s" % (code, e))
            results.append({"代码": code, "退出码": -1, "耗时_秒": 0})
            continue
        ctl["proc"] = proc
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if line.strip():
                log("    " + line)
        rc = proc.wait()
        ctl["proc"] = None
        secs = round(time.time() - t0, 1)
        row = {"代码": code, "退出码": rc, "耗时_秒": secs}
        if rc == 0:
            report = latest_report(phase)
            if report:
                row["报告"] = rel(report)
        results.append(row)
        log("[%s] %s 完成（退出码 %s，%ss）" % ("OK" if rc == 0 else "FAIL", code, rc, secs))
    ok = sum(1 for r in results if r.get("退出码") == 0)
    log("[OK] 批量结束：成功 %d / 共 %d" % (ok, total))
    return {"批量": results, "成功": ok, "总数": total}
