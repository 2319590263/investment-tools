# -*- coding: utf-8 -*-
"""aiplan Web 控制台（本地运行，零第三方依赖）。

模块地图：
    paths      路径真源与文件读写工具（含按文件加载根目录的 aiplan.py / pan.py）
    sources    data/ 下原始快照的枚举与读取
    store      账户 / 模型 / 持仓 / 自选的读取与写回
    archive    报告、plan_log、荐股产物的读取与删除入口
    market     页面数据组装（快照 / 代码候选 / K 线）与大盘走势预测
    jobs       后台任务编排（子进程、增量日志、中断、诊断、批量）
    pick       荐股纯逻辑（常量 / 机械打分 / 排除规则 / 参数 / 事实包 / Markdown）
    pick_run   荐股取数与编排（东财抓取、缓存、模型点评）
    trash      回收站（列表 / 7 天过期真删 / 恢复）
    webserver  HTTP 路由与本地服务（不叫 http.py：避免遮蔽标准库 http）

启动：``python main.py webui``（等价于 ``cd src && python -m webui``）。
"""

__version__ = "2.0"
