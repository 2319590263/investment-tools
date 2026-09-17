# -*- coding: utf-8 -*-
"""项目静态自检：不联网、不调模型、零成本。

    python main.py check                  # 或 python scripts/check.py
    python scripts/check.py --update-baseline   # 有意改了 CLI 之后重新登记哈希

检查项：
  1. 结构：入口 / 包 / 静态资源 / 文档 / 测试 / 配置模板齐全，且没有回退成单文件
  2. Python 语法 + 包内约束（单文件 ≤800 行、不许改 sys.path）
  3. 前端：node --check、单模块入口、无内联事件、import 必须命中导出、无循环依赖、id 引用一致
  4. 敏感信息：入库文件里没有明文 Key，个人文件仍在忽略清单里
  5. CLI 哈希基线：aiplan.py / pan.py / stock3d.py 与登记的 sha256 一致
"""

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBUI = os.path.join(ROOT, "src", "webui")
STATIC = os.path.join(WEBUI, "static")
JS_DIR = os.path.join(STATIC, "js")
TEST_DIR = os.path.join(ROOT, "tests")
FROZEN = ("aiplan.py", "pan.py", "stock3d.py")
BASELINE = os.path.join(ROOT, "scripts", "cli_baseline.json")
SPEC_FILES = ("机器打分逻辑.txt", "大盘评分逻辑.txt")     # 两份口径都锁定 sha256
SPEC_BASELINE = os.path.join(ROOT, "scripts", "spec_baseline.json")
SKIP_DIRS = {".git", "data", "build", "tmp", "__pycache__", "node_modules", ".trash",
             ".idea", ".vscode", ".pytest_cache", ".ruff_cache", ".venv", ".venv-holdings"}
TEXT_EXT = {".py", ".js", ".mjs", ".css", ".html", ".json", ".md", ".txt", ".cmd",
            ".bat", ".yml", ".yaml", ".toml", ".ini", ".cfg"}
PY_MAX_LINES = 800
JS_MAX_LINES = 900

PACKAGE_MODULES = ("__init__.py", "__main__.py", "paths.py", "sources.py", "store.py",
                   "archive.py", "market.py", "jobs.py", "pick.py", "pick_run.py",
                   "pickrank.py", "mech.py", "mechdata.py", "mechtech.py",
                   "mechstock.py",
                   "mktdata.py", "mktscore.py",
                   "background.py", "plancheck.py", "planlines.py", "quotes.py",
                   "overview.py",
                   "alerts.py", "trash.py", "track.py", "track_run.py", "trackview.py",
                   "flow.py", "flowbook.py", "flow_run.py", "flowview.py", "flowplan.py",
                   "webserver.py", "run_aiplan.py",
                   "holdings_ths.py", "holdings_trades.py", "holdings_sync.py")
JS_MODULES = ("main.js", "core/util.js", "core/api.js", "core/app.js", "core/poller.js",
              "core/mobile.js",
                  "ui/markdown.js", "ui/jsontree.js", "ui/modal.js", "ui/cards.js", "ui/kline.js",
                  "ui/stockcard.js", "ui/planprices.js", "ui/pickcards.js",
                 "ui/trackcards.js", "ui/flowcards.js", "ui/flowchart.js",
             "views/run.js", "views/report.js", "views/holdings.js",
              "views/watch.js", "views/pick.js", "views/models.js", "views/market.js",
              "views/flow.js")
VIEWS = ("console", "flow", "run", "report", "holdings", "watch", "pick",
         "models", "market")

FAILS = []


def ok(name, detail=""):
    print("  PASS  %s%s" % (name, ("  — " + detail) if detail else ""))


def bad(name, detail=""):
    FAILS.append(name)
    print("  FAIL  %s%s" % (name, ("  — " + detail) if detail else ""))


def read(path):
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def lines_of(path):
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        return sum(1 for _ in fh)


def walk_files(root=ROOT):
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            yield os.path.join(base, name)


def git(*args):
    try:
        p = subprocess.run(["git"] + list(args), cwd=ROOT, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=60)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def check_layout():
    print("[1] 目录结构")
    must = ("main.py", "README.md", "requirements.txt", "requirements-holdings.txt", ".gitignore", ".editorconfig",
            "启动WebUI.cmd", "docs/README_webui.md", "docs/ARCHITECTURE.md",
            "src/webui/static/mobile.css",
            "config/账户配置.example.json", "config/模型配置.example.json",
            "tests/unit", "tests/integration", "tests/ui/ui_smoke.mjs", "tests/ui/mobile_smoke.mjs")
    for rel in must:
        (ok if os.path.exists(os.path.join(ROOT, *rel.split("/"))) else bad)("存在 " + rel)
    for name in PACKAGE_MODULES:
        (ok if os.path.isfile(os.path.join(WEBUI, name)) else bad)("包内存在 src/webui/" + name)
    for rel in JS_MODULES:
        (ok if os.path.isfile(os.path.join(JS_DIR, *rel.split("/"))) else bad)("前端存在 js/" + rel)
    if os.path.exists(os.path.join(WEBUI, "server.py")):
        bad("src/webui/server.py 已拆分，不该再出现")
    else:
        ok("单文件 server.py 已拆分")
    if os.path.exists(os.path.join(STATIC, "app.js")):
        bad("static/app.js 已拆分，不该再出现")
    else:
        ok("单文件 app.js 已拆分")
    for name in FROZEN:
        (ok if os.path.isfile(os.path.join(ROOT, name)) else bad)("冻结脚本在根目录 " + name)


def check_python():
    print("[2] Python 语法与包内约束")
    n = 0
    for path in walk_files():
        if not path.endswith(".py"):
            continue
        n += 1
        try:
            ast.parse(read(path), filename=path)
        except SyntaxError as e:
            bad("ast.parse %s" % os.path.relpath(path, ROOT), str(e))
    ok("解析 %d 个 .py 文件" % n)
    for name in PACKAGE_MODULES:
        if name == "run_aiplan.py":      # 独立脚本，需要自己加 sys.path 才能 import aiplan
            continue
        path = os.path.join(WEBUI, name)
        text = read(path)
        if "sys.path.insert" in text:
            bad("%s 里不该改 sys.path" % name)
        n_lines = lines_of(path)
        if n_lines > PY_MAX_LINES:
            bad("%s 有 %d 行（上限 %d）" % (name, n_lines, PY_MAX_LINES))
    if not [f for f in FAILS if "行（上限" in f or "sys.path" in f]:
        ok("包内模块都在 %d 行以内且无 sys.path 改写" % PY_MAX_LINES)


def js_imports(text):
    return [(m.group(2), [x.strip() for x in m.group(1).split(",") if x.strip()])
            for m in re.finditer(r'^import\s*\{([^}]*)\}\s*from\s*"([^"]+)";', text, re.M)]


def check_frontend():
    print("[3] 前端模块")
    files = {}
    for base, dirs, names in os.walk(JS_DIR):
        for name in names:
            if name.endswith(".js"):
                p = os.path.join(base, name)
                files[os.path.relpath(p, JS_DIR).replace("\\", "/")] = read(p)

    node = shutil.which("node")
    if not node:
        print("  SKIP  node 未安装，跳过语法与模块图检查")
    else:
        bad_syntax = []
        for rel, text in sorted(files.items()):
            # --check 只接受文件或 stdin，不能和 -e 同时用（node 会直接报错）
            p = subprocess.run([node, "--check", "--input-type=module"],
                               input=text.encode("utf-8"),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if p.returncode != 0:
                bad_syntax.append("%s: %s" % (rel, p.stderr.decode("utf-8", "replace").strip()[:120]))
        (ok if not bad_syntax else bad)("node --check 通过（%d 个模块）" % len(files),
                                        "; ".join(bad_syntax[:3]))

    export_re = re.compile(r'^export\s+(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z_$][\w$]*)', re.M)
    missing, graph = [], {}
    for rel, text in files.items():
        graph[rel] = []
        for target_raw, names in js_imports(text):
            target = os.path.normpath(os.path.join(os.path.dirname(rel), target_raw)).replace("\\", "/")
            if target not in files:
                missing.append("%s -> %s（文件不存在）" % (rel, target))
                continue
            graph[rel].append(target)
            exported = set(export_re.findall(files[target]))
            for name in names:
                if name not in exported:
                    missing.append("%s 导入 %s，但 %s 没有导出" % (rel, name, target))
    (ok if not missing else bad)("import 全部命中导出", "; ".join(missing[:4]))

    color, cycles = {}, []

    def dfs(u, stack):
        color[u] = 1
        for v in graph.get(u, ()):
            if color.get(v) == 1:
                cycles.append(stack[stack.index(v):] + [v])
            elif color.get(v, 0) == 0:
                dfs(v, stack + [v])
        color[u] = 2

    for u in sorted(graph):
        if color.get(u, 0) == 0:
            dfs(u, [u])
    (ok if not cycles else bad)("ES 模块无循环依赖", str(cycles[:2]))

    for rel, text in sorted(files.items()):
        if rel.startswith("views/") and 'registerView("' not in text:
            bad("%s 没有注册到 core/app.js（registerView）" % rel)
        n_lines = text.count("\n") + 1
        if n_lines > JS_MAX_LINES:
            bad("%s 有 %d 行（上限 %d）" % (rel, n_lines, JS_MAX_LINES))

    html = read(os.path.join(STATIC, "index.html"))
    scripts = re.findall(r'<script[^>]*src="([^"]+)"', html)
    inline = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>', html)
    (ok if scripts == ["/js/main.js"] else bad)("index.html 只加载一个模块入口", str(scripts))
    (ok if not inline else bad)("index.html 没有内联脚本", str(inline))
    for view in VIEWS:
        if 'id="view-%s"' % view not in html or 'data-view="%s"' % view not in html:
            bad("缺少视图或导航项 view-%s" % view)
    if not [f for f in FAILS if "缺少视图" in f]:
        ok("%d 个视图与导航项齐全" % len(VIEWS))

    inline_handlers = []
    for rel, text in list(files.items()) + [("index.html", html)]:
        if re.search(r'\bon(click|change|input|keydown)\s*=', text):
            inline_handlers.append(rel)
    (ok if not inline_handlers else bad)("没有内联事件处理器", str(inline_handlers))

    html_ids = set(re.findall(r'id="([^"]+)"', html))
    js_ids = set()
    refs = set()
    for text in files.values():
        js_ids |= set(re.findall(r'id="([^"]+)"', text))
        refs |= set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', text))
    missing_ids = sorted(r for r in refs if r not in html_ids and r not in js_ids)
    (ok if not missing_ids else bad)("js 里 $('#id') 引用的元素都存在",
                                     "缺：%s" % missing_ids if missing_ids else "%d 个引用" % len(refs))
    dups = sorted({i for i in re.findall(r'id="([^"]+)"', html) if html.count('id="%s"' % i) > 1})
    (ok if not dups else bad)("index.html 无重复 id", str(dups))
    for rel in re.findall(r'(?:src|href)="([^"]+)"', html):
        if "://" in rel or rel.startswith("#"):
            continue
        if rel in ("/m", "/m/"):
            ok("静态资源存在 " + rel + "（由 webserver 映射到 index.html）")
            continue
        target = os.path.join(STATIC, rel.lstrip("/").replace("/", os.sep))
        (ok if os.path.exists(target) else bad)("静态资源存在 " + rel)


def check_secrets():
    print("[4] 敏感信息")
    code, out = git("ls-files")
    files = [os.path.join(ROOT, line.strip()) for line in out.splitlines() if line.strip()] if code == 0 else []
    if not files:
        print("  SKIP  不是 git 仓库或无跟踪文件，跳过入库密钥扫描")
    patterns = [(r"sk-[A-Za-z0-9._-]{16,}", "疑似明文 API Key"),
                (r"(?i)bearer\s+[A-Za-z0-9._-]{16,}", "疑似 Bearer Token"),
                (r'"api_key"\s*:\s*"[^"\s]{8,}"', "非空 api_key 字段"),
                (r'(?i)\b(?:default_)?(?:lan_)?password\s*=\s*"[^"]{4,}"', "硬编码口令赋值")]
    hits = []
    for path in files:
        if os.path.splitext(path)[1].lower() not in TEXT_EXT or not os.path.isfile(path):
            continue
        text = read(path)
        for pat, label in patterns:
            for m in re.finditer(pat, text):
                hits.append("%s：%s（%s）" % (os.path.relpath(path, ROOT), m.group(0)[:24], label))
    (ok if not hits else bad)("入库文件无明文密钥", "; ".join(hits[:5]))
    for rel in ("config/模型配置.json", "config/账户配置.json", "data/user/持仓数据.md",
                "data/user/自选股.md"):
        code2, _ = git("check-ignore", "-q", rel)
        (ok if code2 == 0 else bad)("已在 .gitignore 忽略 " + rel)
    code3, _ = git("check-ignore", "-q", "config/webui配置.json")
    (ok if code3 == 0 else bad)("已在 .gitignore 忽略 config/webui配置.json")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def check_frozen(update=False):
    print("[5] CLI 哈希基线")
    current = {name: sha256(os.path.join(ROOT, name)) for name in FROZEN}
    if update:
        with io.open(BASELINE, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"说明": "三个 CLI 的 sha256 基线；只有走查过才用 --update-baseline 重登记。",
                       "文件": current}, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
        spec = {name: sha256(os.path.join(ROOT, name)) for name in SPEC_FILES
                if os.path.isfile(os.path.join(ROOT, name))}
        if spec:
            with io.open(SPEC_BASELINE, "w", encoding="utf-8", newline="\n") as fh:
                json.dump({"说明": "打分口径文件的 sha256（机械打分 + 大盘评分）；改口径必须显式重登记。",
                           "文件": spec}, fh, ensure_ascii=False, indent=1, sort_keys=True)
                fh.write("\n")
        ok("已重新登记基线 scripts/cli_baseline.json")
        return
    if not os.path.isfile(BASELINE):
        bad("缺少 scripts/cli_baseline.json（用 python scripts/check.py --update-baseline 生成）")
        return
    base = (json.loads(read(BASELINE)) or {}).get("文件") or {}
    for name in FROZEN:
        if name not in base:
            bad("基线里没有 " + name)
        elif base[name] != current[name]:
            bad("%s 与基线不一致（改过就用 --update-baseline 重新登记）" % name)
        else:
            ok("%s 与基线一致" % name)


def check_spec():
    """打分口径文件：必须存在，并登记 sha256（口径改动要显式重登记）。"""
    print("[6] 打分口径文件")
    for name in SPEC_FILES:
        path = os.path.join(ROOT, name)
        if not os.path.isfile(path):
            bad("缺少口径文件 %s（打分严格照它实现）" % name)
            return
        ok("口径文件在根目录 %s（%d 字节）" % (name, os.path.getsize(path)))
    if not os.path.isfile(SPEC_BASELINE):
        bad("缺少 %s（用 python scripts/check.py --update-baseline 生成）"
            % os.path.relpath(SPEC_BASELINE, ROOT))
        return
    base = (json.loads(read(SPEC_BASELINE)) or {}).get("文件") or {}
    for name in SPEC_FILES:
        current = sha256(os.path.join(ROOT, name))
        if base.get(name) != current:
            bad("%s 与基线不一致（改口径就用 --update-baseline 重新登记）" % name)
        else:
            ok("%s 与基线一致" % name)


def main():
    ap = argparse.ArgumentParser(description="项目静态自检")
    ap.add_argument("--update-baseline", action="store_true",
                    help="把三个 CLI 的当前 sha256 记为基线（有意修改后才用）")
    args = ap.parse_args()
    print("静态自检（根目录：%s）" % ROOT)
    check_layout()
    check_python()
    check_frontend()
    check_secrets()
    check_frozen(update=args.update_baseline)
    check_spec()
    if FAILS:
        print("\n结果：FAIL（%d 项）" % len(FAILS))
        return 1
    print("\n结果：PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
