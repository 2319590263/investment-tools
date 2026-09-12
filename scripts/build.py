# -*- coding: utf-8 -*-
"""打一个可分发的源码包（零第三方依赖，只用标准库 zipfile）。

    python scripts/build.py                 # -> build/投资工具集-YYYYMMDD.zip
    python scripts/build.py --list          # 只列出会打包哪些文件，不生成
    python scripts/build.py --name 自定义名

打包内容：源码（main.py / 三个 CLI / src/webui）+ 文档 + 测试 + 脚本 + 脱敏配置模板。
**绝不打包**：个人密钥与资金数据（模型配置.json / 账户配置.json / 持仓数据.md / 自选股.md）、
运行产物 data/、缓存 __pycache__、.bak 备份、build/ 自身、.git/。
"""

import argparse
import datetime as dt
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "data", "build", "__pycache__", ".pytest_cache", "node_modules", ".venv-holdings",
             ".trash", ".vscode", ".idea", ".mypy_cache", ".ruff_cache", "tmp"}
SKIP_NAMES = {"模型配置.json", "账户配置.json", "持仓数据.md", "自选股.md",
              "token.txt", ".env"}
SKIP_EXT = {".bak", ".pyc", ".pyo", ".log", ".zip", ".tmp"}


def wanted(path):
    name = os.path.basename(path)
    if name in SKIP_NAMES or os.path.splitext(name)[1].lower() in SKIP_EXT:
        return False
    return True


def collect():
    out = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(files):
            path = os.path.join(base, name)
            if wanted(path):
                out.append(path)
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(description="打包源码发布包")
    ap.add_argument("--list", action="store_true", help="只列出文件，不生成压缩包")
    ap.add_argument("--name", default=None, help="压缩包名（不含 .zip）")
    args = ap.parse_args()

    files = collect()
    total = sum(os.path.getsize(p) for p in files)
    print("待打包 %d 个文件，合计 %.1f KB" % (len(files), total / 1024.0))
    if args.list:
        for p in files:
            print("  " + os.path.relpath(p, ROOT).replace("\\", "/"))
        return 0

    name = args.name or ("投资工具集-" + dt.datetime.now().strftime("%Y%m%d"))
    out_dir = os.path.join(ROOT, "build")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, name + ".zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, os.path.join(name, os.path.relpath(path, ROOT)))
    print("已生成：%s（%.1f KB）" % (out, os.path.getsize(out) / 1024.0))
    print("解压后按 README.md「安装 / 启动」两节操作即可；需要单文件 exe 时见 README「打包与部署」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

