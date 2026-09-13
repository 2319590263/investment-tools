# -*- coding: utf-8 -*-
"""客户端断连的处理：只吞连接类异常，其它异常照旧抛。"""

import io
import unittest
from contextlib import redirect_stderr

from _common import import_module

ws = import_module("webserver")


class FakeWFile:
    """写就炸：模拟对端已经 RST。"""

    def __init__(self, exc):
        self.exc = exc

    def write(self, data):
        raise self.exc


def fake_handler(exc):
    h = ws.Handler.__new__(ws.Handler)
    h.wfile = FakeWFile(exc)
    h._gone = False
    h.close_connection = False
    h.send_response = lambda code: None
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda: None
    return h


class TestIsDisconnect(unittest.TestCase):

    def test_matches_client_gone(self):
        for exc in (BrokenPipeError("x"), ConnectionResetError("x"), ConnectionAbortedError("x"),
                    ConnectionError("x")):
            self.assertTrue(ws.is_disconnect(exc), exc)
        self.assertTrue(ws.is_disconnect(OSError(10053, "本机软件中止了一个已建立的连接")))
        self.assertTrue(ws.is_disconnect(OSError(0, "x", None, 10054)))

    def test_does_not_match_real_errors(self):
        self.assertFalse(ws.is_disconnect(ValueError("boom")))
        self.assertFalse(ws.is_disconnect(OSError(2, "No such file")))
        self.assertFalse(ws.is_disconnect(KeyError("k")))


class TestSendOnDeadSocket(unittest.TestCase):

    def test_send_swallows_abort_and_marks_gone(self):
        h = fake_handler(ConnectionAbortedError(10053, "本机软件中止了一个已建立的连接"))
        self.assertFalse(ws.Handler._send(h, 200, "{}"))
        self.assertTrue(h._gone)                  # 标记后不再重复写
        self.assertTrue(h.close_connection)
        self.assertFalse(ws.Handler._send(h, 200, "{}"))   # 第二次直接跳过，不抛

    def test_send_reraises_unexpected(self):
        h = fake_handler(ValueError("boom"))
        with self.assertRaises(ValueError):
            ws.Handler._send(h, 200, "{}")
        self.assertFalse(h._gone)


class TestServerHandleError(unittest.TestCase):

    def test_disconnect_is_silent(self):
        srv = ws.LocalServer.__new__(ws.LocalServer)
        err = io.StringIO()
        try:
            raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接")
        except ConnectionResetError:
            with redirect_stderr(err):
                srv.handle_error(None, ("127.0.0.1", 1))    # 不该抛、也不该打 traceback
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
