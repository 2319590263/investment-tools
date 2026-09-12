# -*- coding: utf-8 -*-
"""局域网模式必须经过密码登录，且登录后能正常访问控制台。"""

import http.cookiejar
import json
import secrets
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

from _common import ROOT, child_env, free_port


# 口令随机生成：真实口令不进仓库，测试也不依赖写死的常量
PASSWORD = "pw" + secrets.token_hex(6)


class TestLanAccess(unittest.TestCase):

    def test_password_login(self):
        port = free_port()
        base = "http://127.0.0.1:%d" % port
        proc = subprocess.Popen(
            [sys.executable, "-u", "-X", "utf8", "-m", "webui",
             "--host", "0.0.0.0", "--port", str(port), "--no-browser",
             "--password", PASSWORD],
            cwd=ROOT, env=child_env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        killer = threading.Timer(30, proc.terminate)
        killer.start()
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                if proc.poll() is not None:
                    self.fail("服务提前退出：%s" % proc.stdout.read())
                try:
                    with urllib.request.urlopen(base + "/", timeout=2) as resp:
                        if resp.status == 200:
                            break
                except urllib.error.HTTPError:
                    pass
                except Exception:
                    pass
                time.sleep(0.2)
            else:
                self.fail("服务在 10 秒内没有就绪")

            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(base + "/api/state", timeout=5)
            self.assertEqual(raised.exception.code, 401)

            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            wrong = urllib.parse.urlencode({"password": "wrong"}).encode("utf-8")
            with self.assertRaises(urllib.error.HTTPError) as raised:
                opener.open(urllib.request.Request(base + "/__login", data=wrong), timeout=5)
            self.assertEqual(raised.exception.code, 401)

            status, login_html = None, ""
            with urllib.request.urlopen(base + "/m", timeout=5) as resp:
                status = resp.status
                login_html = resp.read().decode("utf-8", "replace")
            self.assertEqual(status, 200)
            self.assertIn('name="next" value="/m"', login_html)

            right = urllib.parse.urlencode({"password": PASSWORD, "next": "/m"}).encode("utf-8")
            with opener.open(urllib.request.Request(base + "/__login", data=right), timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertTrue(resp.geturl().endswith("/m"), resp.geturl())
                mobile_html = resp.read().decode("utf-8", "replace")
                self.assertIn('id="mobile-nav"', mobile_html)
            with opener.open(base + "/api/state", timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("根目录", data)
        finally:
            killer.cancel()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if proc.stdout:
                proc.stdout.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
