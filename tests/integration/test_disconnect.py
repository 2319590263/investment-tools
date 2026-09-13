# -*- coding: utf-8 -*-
"""客户端中途断连（刷新 / 切页 / 取消轮询）不该被当成服务端异常。

用 SO_LINGER=0 让客户端发 RST，逼出 Windows 上的 ConnectionAbortedError(10053)：
修好之前服务端会打一整段 traceback（还会在回 500 时再炸一次），修好之后应当彻底安静、
进程继续正常服务。
"""

import io
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from _common import ROOT, child_env, free_port


def abort_request(port, path="/api/overview?refresh=1"):
    """发一个请求后立刻用 RST 断开（模拟浏览器刷新/切页）。"""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n" % path).encode("ascii"))
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    s.close()


class TestClientAbort(unittest.TestCase):

    def test_abort_is_silent_and_server_survives(self):
        port = free_port()
        fd, logpath = tempfile.mkstemp(suffix=".log")
        log = os.fdopen(fd, "wb")
        proc = subprocess.Popen(
            [sys.executable, "-u", "-X", "utf8", "-m", "webui",
             "--port", str(port), "--no-browser"],
            cwd=ROOT, env=child_env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=log)
        base = "http://127.0.0.1:%d" % port
        try:
            deadline = time.time() + 25
            while time.time() < deadline:
                if proc.poll() is not None:
                    self.fail("服务提前退出")
                try:
                    with urllib.request.urlopen(base + "/api/state", timeout=3) as resp:
                        if resp.status == 200:
                            break
                except Exception:
                    time.sleep(0.3)
            else:
                self.fail("服务在 25 秒内没有就绪")

            for _ in range(3):
                abort_request(port, "/api/overview")
            abort_request(port, "/api/overview?refresh=1")
            time.sleep(1.5)

            with urllib.request.urlopen(base + "/api/state", timeout=10) as resp:
                self.assertEqual(resp.status, 200, "断连之后服务仍应正常响应")
            self.assertIsNone(proc.poll(), "服务不该因客户端断连退出")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            log.close()

        with io.open(logpath, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        os.remove(logpath)
        self.assertNotIn("Traceback", text, "客户端断连不该打 traceback：\n" + text[:600])
        self.assertNotIn("ConnectionAbortedError", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
