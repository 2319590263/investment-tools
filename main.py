# -*- coding: utf-8 -*-
"""投资工具集 —— 统一入口（核心功能零第三方依赖，同花顺同步可选依赖）。

用法
----
    python main.py                            # 启动 Web 控制台（默认 http://127.0.0.1:8765）
    python main.py webui --port 9000 --no-browser
    python main.py aiplan post --code 002463.SZ --no-fetch   # 等价 python aiplan.py ...
    python main.py pan post
    python main.py stock3d pull 002463.SZ
    python main.py holdings sync               # 从已登录同花顺只读同步持仓
    python main.py holdings sync --preview     # 只抓取校验，不写文件
    python main.py test                        # 跑全部自动化测试（tests/，标准库 unittest）
    python main.py check                       # 静态自检：语法 / 前端引用 / 敏感信息 / 冻结文件

约定
----
- Web 控制台是推荐入口；CLI 子命令只是把余下参数**原样**转给对应模块/脚本，不做二次解析。
- 所有子进程的 cwd 固定为项目根。
- 同花顺同步使用 .venv-holdings；未安装时主程序和其余功能不受影响。
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable
SRC = os.path.join(ROOT, "src")
WEBUI_PKG = "webui"                      # src/webui 是包，用 python -m webui 启动
RUN_AIPLAN = os.path.join(SRC, "webui", "run_aiplan.py")
HOLDINGS_MODULE = "webui.holdings_sync"
HOLDINGS_PYTHON = os.path.join(ROOT, ".venv-holdings", "Scripts", "python.exe")
CHECK_SCRIPT = os.path.join(ROOT, "scripts", "check.py")

USAGE = """投资工具集（webui + aiplan + pan + stock3d + holdings 同步）

  python main.py                            启动 Web 控制台（默认 127.0.0.1:8765）
  python main.py webui [--port N] [--no-browser]
  python main.py aiplan <aiplan 参数>        例：post --code 002463.SZ --no-fetch
  python main.py pan <pan 参数>              例：post / prep
  python main.py stock3d <stock3d 参数>      例：pull 002463.SZ
  python main.py holdings sync [--preview]   从已登录同花顺只读同步持仓
  python main.py test                       跑 tests/ 下全部测试
  python main.py check                      静态自检

核心环境要求：Python 3.9+。同花顺同步另需 .venv-holdings，安装见 README。
更多说明见 README.md；各 CLI 的完整参数见 docs/。
"""


def child_env():
    """子进程统一 UTF-8 + 把 src/ 加进导入路径（webui 包在那儿）。"""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    old = env.get("PYTHONPATH")
    env["PYTHONPATH"] = SRC + (os.pathsep + old if old else "")
    return env


def holdings_python():
    override = os.environ.get("THS_HOLDINGS_PYTHON")
    if override and os.path.isfile(override):
        return override
    return HOLDINGS_PYTHON if os.path.isfile(HOLDINGS_PYTHON) else None


def run(argv):
    try:
        return subprocess.call(argv, cwd=ROOT, env=child_env())
    except KeyboardInterrupt:
        return 130
    except OSError as e:
        sys.stderr.write("[FAIL] 无法启动子进程：%s\n" % e)
        return 127


def main(argv):
    if not argv:                       # 无参数 = 启动 Web 控制台（推荐入口）
        return run([PYTHON, "-X", "utf8", "-m", WEBUI_PKG])
    if argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0
    cmd, rest = argv[0], list(argv[1:])
    if cmd in ("-V", "--version", "version"):
        print("投资工具集：webui + aiplan + pan + stock3d + holdings（核心零第三方依赖）")
        return 0
    if cmd == "webui":
        return run([PYTHON, "-X", "utf8", "-m", WEBUI_PKG] + rest)
    if cmd == "aiplan":
        return run([PYTHON, "-X", "utf8", RUN_AIPLAN] + rest)
    if cmd in ("pan", "stock3d"):
        return run([PYTHON, "-X", "utf8", os.path.join(ROOT, cmd + ".py")] + rest)
    if cmd == "holdings":
        py = holdings_python()
        if not py:
            sys.stderr.write(
                "缺少同花顺自动化环境。请运行：\n"
                "  py -3.11 -m venv .venv-holdings\n"
                "  .\\.venv-holdings\\Scripts\\python.exe -m pip install -r requirements-holdings.txt\n")
            return 8
        return run([py, "-X", "utf8", "-m", HOLDINGS_MODULE] + rest)
    if cmd == "test":
        return run([PYTHON, "-X", "utf8", "-m", "unittest",
                    "discover", "-s", "tests", "-t", "tests", "-p", "test_*.py", "-v"] + rest)
    if cmd == "check":
        return run([PYTHON, "-X", "utf8", CHECK_SCRIPT] + rest)
    sys.stderr.write("[FAIL] 未知子命令：%s\n\n" % cmd)
    sys.stdout.write(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

