# -*- coding: utf-8 -*-
"""``python -m webui`` 入口：解析参数后交给 http.serve 启动。"""

import argparse
import sys

from .webserver import serve


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m webui", description="aiplan Web UI（本地控制台）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认只监听本机）")
    ap.add_argument("--port", type=int, default=8765, help="监听端口（被占用时自动顺延 20 个）")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = ap.parse_args(argv)
    return serve(args.host, args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
