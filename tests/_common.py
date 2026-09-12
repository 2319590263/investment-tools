# -*- coding: utf-8 -*-
"""测试公用工具：定位项目路径、临时起一个真实服务、发 HTTP 请求。

不是测试用例本身（文件名不以 test_ 开头，unittest 不会收集它）。
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # tests/ 的上一层 = 项目根
SRC = os.path.join(ROOT, "src")
WEBUI = os.path.join(SRC, "webui")
STATIC = os.path.join(WEBUI, "static")
DATA = os.path.join(ROOT, "data")


def child_env():
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    old = env.get("PYTHONPATH")
    env["PYTHONPATH"] = SRC + (os.pathsep + old if old else "")
    return env


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def import_webui():
    """导入 src/webui 包（复用真实代码，但不启动 HTTP 服务）。"""
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    import webui
    return webui


def import_module(name):
    """按名字导入包内模块，例如 import_module("pick") -> webui.pick。"""
    import_webui()
    return importlib.import_module("webui." + name)


class LocalServer:
    """后台起 server.py 真实进程，退出时连带子进程一起收拾干净。"""

    def __init__(self, port=None, timeout=90):
        self.port = port or free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        self.timeout = timeout
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "webui", "--port", str(self.port), "--no-browser"],
            cwd=ROOT, env=child_env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                err = self.proc.stderr.read().decode("utf-8", "replace")
                raise RuntimeError("服务启动失败（退出码 %s）：%s" % (self.proc.returncode, err[:800]))
            try:
                self.get("/api/state", timeout=5)
                return self
            except Exception:
                time.sleep(0.4)
        raise RuntimeError("服务在 %d 秒内没有就绪" % self.timeout)

    def stop(self):
        if not self.proc or self.proc.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def request(self, path, method="GET", body=None, timeout=240, raw=False):
        """返回 (状态码, 解析后的 JSON)；raw=True 时返回 (状态码, 原始文本)。"""
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read().decode("utf-8", "replace")
                if raw:
                    return resp.status, payload
                return resp.status, json.loads(payload)
        except urllib.error.HTTPError as e:
            payload = e.read().decode("utf-8", "replace")
            if raw:
                return e.code, payload
            try:
                return e.code, json.loads(payload)
            except ValueError:
                return e.code, payload

    def get(self, path, **kw):
        return self.request(path, **kw)

    def post(self, path, body, **kw):
        return self.request(path, method="POST", body=body, **kw)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False
