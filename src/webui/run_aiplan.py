# -*- coding: utf-8 -*-
"""以 UTF-8 原样输出运行 aiplan.py（网页控制台专用包装）。

aiplan.py 的既有约束是「stdout 只出 ASCII，中文一律进文件」，所以直接看
命令行输出会满屏 ?。网页端需要可读的中文，但**不改动 aiplan.py**：
这里只在运行时把它的 _ascii() 换成恒等函数、并把标准输出切到 UTF-8。

用法与 aiplan.py 完全一致：
    python src/webui/run_aiplan.py post --code 002463.SZ --no-fetch
"""
import os
import sys

# 目录约定：<ROOT>/src/webui/run_aiplan.py —— 往上一层是 src/，再往上一层才是项目根。
HERE = os.path.dirname(os.path.abspath(__file__))       # <ROOT>/src/webui
ROOT = os.path.dirname(os.path.dirname(HERE))           # <ROOT>
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import aiplan  # noqa: E402


def _raw(text):
    return str(text)


def _say(msg):
    sys.stdout.write(_raw(msg) + "\n")
    sys.stdout.flush()


aiplan._ascii = _raw      # 不再把中文替换成 ?
aiplan.say = _say         # stage/done/warn/fail 都走这里


if __name__ == "__main__":
    try:
        sys.exit(aiplan.main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.stdout.write("[FAIL] interrupted\n")
        sys.exit(130)
