# -*- coding: utf-8 -*-
"""荐股的纯逻辑：常量、机械打分、排除规则、参数整理、事实包与 Markdown。"""

import os
import re

from .paths import DATA_DIR, aiplan, pan


PICK_MODULES = (("短线", "1-5 个交易日"), ("波段", "2-6 周"),
                ("中线", "1-3 个月"), ("长线", "6 个月以上"))


PICK_MODULE_CYCLE = dict(PICK_MODULES)


PICK_BOARD_TYPES = ("行业", "概念")


PICK_L1_INDUSTRIES = (
    "农林牧渔", "基础化工", "钢铁", "有色金属", "电子", "汽车", "家用电器", "食品饮料",
    "纺织服饰", "轻工制造", "医药生物", "公用事业", "交通运输", "房地产", "商贸零售",
    "社会服务", "综合", "建筑材料", "建筑装饰", "电力设备", "国防军工", "计算机", "传媒",
    "通信", "银行", "非银金融", "美容护理", "石油石化", "煤炭", "环保", "机械设备",
)


PICK_CONCEPT_THEMES = (
    ("指数与成分", ("上证", "深证", "沪深300", "中证", "创业板", "科创", "MSCI", "富时", "标普",
                    "央视50", "HS300", "深成", "创业成份", "宁组合", "茅指数", "成份", "成分",
                    "股通", "GDR", "转债标的", "做市", "北交所", "证金", "社保", "养老金", "QFII",
                    "基金重仓", "机构重仓", "重仓")),
    ("打板与热度", ("涨停", "跌停", "连板", "首板", "二板", "打板", "炸板", "多板", "触板", "人气",
                    "飙升", "新高", "换手", "振幅", "龙虎榜", "游资", "融资融券", "热股", "活跃",
                    "急涨", "放量", "量能", "庄股", "抢筹", "强势股")),
    ("风格与因子", ("风格", "价值", "成长", "大盘", "中盘", "小盘", "微盘", "红利", "绩优", "超跌",
                    "破发", "破增发", "破净", "市净率", "低价", "高价", "百元", "权重", "蓝筹",
                    "题材股", "趋势股", "龙头", "精选", "周期股", "微利", "调研", "举牌", "股权",
                    "重组", "并购", "增持", "回购", "解禁", "AH", "B股", "分拆", "反内卷", "反转股")),
    ("事件与财务", ("中报", "年报", "一季报", "三季报", "季报", "业绩", "预增", "预减", "预亏",
                    "扭亏", "首亏", "送转", "高送", "摘帽", "ST", "退市", "分红", "金股", "次新",
                    "定增", "资产重组", "减值")),
    ("AI与算力", ("人工智能", "算力", "大模型", "数据中心", "CPO", "AIGC", "智算", "液冷", "数字经济",
                  "数据", "云计算", "边缘计算", "元宇宙", "ChatGPT", "DeepSeek", "Kimi", "鸿蒙",
                  "信创", "网络安全", "区块链", "数字货币", "web3", "Web3", "英伟达", "华为", "腾讯",
                  "阿里", "百度", "字节", "小米", "荣耀", "苹果", "微软", "海思", "软件", "信息化",
                  "数字", "智能体", "ERP", "MLOps", "EDA", "东数西算", "政务", "大数据", "全息",
                  "IDC", "国资云", "VPN", "EDR", "脑机")),
    ("半导体与电子", ("芯片", "半导体", "集成电路", "存储", "光刻", "封测", "封装", "MLCC", "元件",
                      "面板", "消费电子", "PCB", "铜缆", "被动", "电子", "氮化镓", "碳化硅", "传感器",
                      "摄像头", "OLED", "LED", "显示", "虚拟现实", "增强现实", "VR", "MR", "AR", "3D",
                      "屏", "内存", "基板", "碳纤维", "PEEK", "玻璃", "蓝宝石", "纳米银", "石墨烯",
                      "无线耳机", "智能穿戴", "UWB", "智能电视", "超清视频", "空间计算", "混合现实")),
    ("通信与卫星", ("5G", "6G", "通信", "卫星", "北斗", "光纤", "光通信", "光模块", "物联网", "WIFI",
                    "WiFi", "超导", "量子", "星链", "雷达", "空间站", "毫米波", "智慧灯杆", "ETC")),
    ("机器人与智能制造", ("机器人", "减速器", "工业母机", "智能制造", "机床", "3D打印", "工业4.0",
                          "工业互联网", "专精特新", "独角兽", "新型工业化", "自动化", "仪器", "检测",
                          "工程机械", "激光", "机器视觉", "电机", "PLC", "工业气体")),
    ("医药与生物", ("医药", "医疗", "生物", "创新药", "CXO", "疫苗", "中药", "眼科", "牙科", "器械",
                    "基因", "细胞", "血液", "CRO", "减肥", "合成生物", "医美", "诊断", "药", "防治",
                    "健康", "养老", "生殖", "病毒", "流感", "肝素", "维生素", "单抗", "免疫",
                    "阿兹海默", "青蒿素", "抗菌", "幽门", "医废", "SPD")),
    ("军工与安全", ("军工", "军民融合", "大飞机", "航母", "无人机", "航天", "国防", "低空经济",
                    "通用航空", "军贸", "民爆", "安防", "应急", "船舶", "海工", "海洋")),
    ("新能源与电力", ("光伏", "储能", "锂电", "钠电", "氢", "风电", "风能", "核电", "电池", "充电桩",
                      "高压快充", "无线充电", "特高压", "智能电网", "虚拟电厂", "电力", "电网", "绿电",
                      "碳中和", "环保", "核聚变", "水利", "水电", "换电", "超超临界", "抽水蓄能",
                      "地热", "可燃冰", "空气能", "热泵", "碳交易", "超级电容", "磁悬浮", "植物照明",
                      "节能", "净水", "垃圾分类", "土壤修复", "尾气治理", "新能源")),
    ("汽车与出行", ("汽车", "新能源车", "智能驾驶", "车联网", "压铸", "轮胎", "摩托车", "无人驾驶",
                    "物流", "航运", "港口", "铁路", "航空运输", "机场", "高速", "快递", "冷链",
                    "特斯拉", "轮毂", "胎压", "交运", "复合集流体")),
    ("消费与传媒", ("白酒", "食品", "消费", "零售", "免税", "旅游", "影视", "游戏", "传媒", "短剧",
                    "电商", "直播", "网红", "预制菜", "餐饮", "酒店", "家电", "服装", "纺织", "IP",
                    "宠物", "教育", "体育", "彩票", "珠宝", "冰雪", "谷子", "味蕾", "小红书", "抖音",
                    "快手", "微信", "品牌", "啤酒", "饮料", "乳业", "养殖", "农业", "猪", "鸡", "水产",
                    "调味", "酿酒", "化妆品", "婴童", "盲盒", "户外", "露营", "拼多多", "社区团购",
                    "地摊", "共享", "人造肉", "代糖", "退税", "内贸", "供销社", "粮食", "土地流转",
                    "租售", "家居", "首发经济", "C2M", "托育", "工业大麻")),
    ("周期与资源", ("稀土", "有色", "煤炭", "石油", "化工", "钢铁", "水泥", "建材", "磷", "氟", "钛",
                    "小金属", "黄金", "白银", "铜", "铝", "天然气", "页岩", "煤化工", "涤纶", "粘胶",
                    "农药", "化肥", "种业", "林业", "渔业", "矿业", "锂矿", "钠", "镁", "硅", "橡胶",
                    "造纸", "包装", "管道", "上游", "资源", "冶金", "环氧", "草甘膦", "PVDF", "降解",
                    "培育钻石", "油气", "冷能", "水运", "氦气", "碳基材料", "新材料")),
    ("金融与地产", ("券商", "证券", "银行", "保险", "金融", "地产", "房地产", "参股", "期货", "信托",
                    "租房", "物业", "建筑", "装修", "基建", "工程", "PPP", "AMC", "房屋", "REITs",
                    "创投", "蚂蚁", "支付", "跨境", "地下管网", "统一大市场")),
    ("政策与区域", ("国企改革", "央企", "一带一路", "自贸", "长三角", "长江三角", "雄安", "西部",
                    "乡村振兴", "新型城镇化", "市值管理", "混改", "海南", "新疆", "成渝", "粤港澳",
                    "京津冀", "东北", "中特估", "中字头", "职业教育", "财税", "改革", "减税", "就业",
                    "生育", "城市", "区域", "保税", "口岸", "特区", "滨海", "中俄", "贬值", "知识产权")),
)


PICK_THEME_OTHER = "其他"


PICK_PER_BOARD = 5


PICK_MAX_CANDIDATES = 400


PICK_MAX_CONCEPTS = 120


PICK_CACHE_DIR = os.path.join(DATA_DIR, "cache")


PICK_MEM_TTL = 1800


PICK_CACHE_TTL = 6 * 3600


PICK_BOARD_FIELDS = ("f2,f3,f5,f6,f7,f8,f12,f14,f20,f21,f24,f25,f62,f104,f105,"
                     "f109,f110,f128,f136,f140,f160,f184")


PICK_STOCK_FIELDS = ("f2,f3,f5,f6,f7,f8,f9,f10,f12,f14,f20,f21,f23,f24,f25,f62,"
                     "f100,f109,f110,f115,f160,f184")


PICK_MAX_CHARS = 40000


PICK_MODEL_TOP = 12


PICK_PAGE_TOP = 30


PICK_MIN_PRICE = 2.0


PICK_MIN_AMOUNT = 5e7


PICK_CAND_SCAN = 3


PICK_SYSTEM = ("你是 A 股选股助手，只依据给定的事实数据做评估与筛选，不编造数值，"
               "不作出具体买卖指令，结论不构成投资建议。")


PICK_PROMPT = """下面是本地程序采集的中性行情数据（只有数值与口径，没有买卖建议）。
请只输出一个合法 JSON 对象：不要解释文字、不要 markdown 代码围栏、不要注释、不要尾随逗号。

JSON 结构：
{
  "总评": {"一句话": "40-80 字，点明主要机会与风险",
           "多空倾向": "偏多|偏空|中性", "最强模块": "短线|波段|中线|长线",
           "最强板块": "板块名", "操作节奏": "…"},
  "板块评估": [{"类型": "行业|概念", "名称": "…", "评级": "强|偏强|中性|弱",
              "驱动": "…", "代表股": ["代码 名称"], "风险": "…"}],
  "模块": [{"模块": "短线|波段|中线|长线", "评分": 0-100 的整数, "逻辑": "…",
           "介入节奏": "…", "失效条件": "…",
           "首推": [{"代码": "6 位代码", "名称": "…", "所属板块": "…",
                    "评级": "关注|观察|回避", "理由": "…",
                    "关注买点": {"价位": "数字或区间", "依据": "…"},
                    "止损": {"价位": "数字", "依据": "…"},
                    "目标位": [{"价位": "数字", "依据": "…"}],
                    "风险": "…", "失效条件": "…"}]}],
  "降级与不确定性": ["…"],
  "免责声明": "…"
}

要求：
1) 只使用事实包里出现的代码与数值；事实包没有的（财务、分红、ROE 等）一律写进「降级与不确定性」，不要编造；
2) 板块池是用户**筛选出来**的（只有行业细分与概念，没有指数），不是全市场扫描：请对事实包里出现的
   每个板块都给一条「板块评估」，不要引入事实包之外的板块；「模块」覆盖事实包里出现的每个模块；
3) 每个模块「首推」1-3 只，必须来自该模块的候选表；价位依据写清均线／前高前低／整数关口等，不得虚构数值；
4) 每个模块必须给「失效条件」；本结论不构成投资建议。
"""


def _pick_round(v, nd=3):
    return None if v is None else round(v, nd)


def pick_pct(values, v):
    """池内分位（0-1，越大越高）。样本不足或缺失返回 None。"""
    if v is None:
        return None
    xs = [x for x in values if x is not None]
    if len(xs) < 2:
        return None
    below = sum(1 for x in xs if x < v)
    same = sum(1 for x in xs if x == v)
    return (below + 0.5 * same) / float(len(xs))


def pick_wsum(pairs):
    """加权平均；缺项按可用权重归一，全缺返回 None（缺失不当成 0）。"""
    tot, w = 0.0, 0.0
    for v, weight in pairs:
        if v is None:
            continue
        tot += v * weight
        w += weight
    return None if w <= 0 else tot / w


def pick_grade(score):
    if score is None:
        return "缺数据"
    if score >= 75:
        return "强"
    if score >= 60:
        return "偏强"
    if score >= 45:
        return "中性"
    return "弱"


PICK_MODULE_WEIGHTS = {
    "短线": (("动量", 40), ("资金", 35), ("弹性", 15), ("板块", 10)),
    "波段": (("波段动量", 35), ("资金", 20), ("换手适中", 20), ("板块", 25)),
    "中线": (("60日动量", 25), ("资金占比", 15), ("估值", 30), ("市值流动性", 15), ("板块", 15)),
    "长线": (("估值", 40), ("年初至今", 10), ("市值", 20), ("60日趋势", 15), ("板块", 15)),
}


def pick_score_boards(boards):
    """同一类板块池内打分（0-100），就地写 机械分 / 评级 / 维度。"""
    def col(k):
        return [pan.num(b.get(k)) for b in boards]
    c3, c109, c160, c110 = col("f3"), col("f109"), col("f160"), col("f110")
    c184, c62, c8, c6 = col("f184"), col("f62"), col("f8"), col("f6")
    for b in boards:
        mom = pick_wsum([(pick_pct(c3, pan.num(b.get("f3"))), 0.4),
                         (pick_pct(c109, pan.num(b.get("f109"))), 0.3),
                         (pick_pct(c160, pan.num(b.get("f160"))), 0.2),
                         (pick_pct(c110, pan.num(b.get("f110"))), 0.1)])
        fund = pick_wsum([(pick_pct(c184, pan.num(b.get("f184"))), 0.6),
                          (pick_pct(c62, pan.num(b.get("f62"))), 0.4)])
        up, dn = pan.num(b.get("f104")), pan.num(b.get("f105"))
        breadth = None if (up is None or dn is None or (up + dn) <= 0) else up / (up + dn)
        vol = pick_wsum([(pick_pct(c8, pan.num(b.get("f8"))), 0.5),
                         (pick_pct(c6, pan.num(b.get("f6"))), 0.5)])
        score = pick_wsum([(mom, 0.35), (fund, 0.30), (breadth, 0.20), (vol, 0.15)])
        b["维度"] = {"动量": _pick_round(mom), "资金": _pick_round(fund),
                     "广度": _pick_round(breadth), "量能": _pick_round(vol)}
        b["机械分"] = None if score is None else round(100.0 * score, 1)
        b["评级"] = pick_grade(b["机械分"])
    return boards


def pick_board_view(b):
    """板块行 → 存档/页面用的扁平结构。"""
    n = pan.num
    up, dn = n(b.get("f104")), n(b.get("f105"))
    return {
        "代码": str(b.get("f12") or ""), "名称": str(b.get("f14") or ""),
        "类型": b.get("类型"),
        "涨跌幅_pct": n(b.get("f3")), "5日_pct": n(b.get("f109")),
        "10日_pct": n(b.get("f160")), "20日_pct": n(b.get("f110")),
        "60日_pct": n(b.get("f24")), "年初至今_pct": n(b.get("f25")),
        "成交额_亿": None if n(b.get("f6")) is None else round(n(b.get("f6")) / 1e8, 2),
        "换手率_pct": n(b.get("f8")),
        "主力净流入_亿": None if n(b.get("f62")) is None else round(n(b.get("f62")) / 1e8, 3),
        "主力净占比_pct": n(b.get("f184")),
        "上涨家数": None if up is None else int(up),
        "下跌家数": None if dn is None else int(dn),
        "领涨股": b.get("f128"), "领涨股代码": b.get("f140"), "领涨股涨跌幅_pct": n(b.get("f136")),
        "机械分": b.get("机械分"), "评级": b.get("评级"), "维度": b.get("维度"),
        "热度": b.get("热度"),
        "行情口径": b.get("行情口径"), "一级行业": b.get("一级行业"), "细分": b.get("细分"),
        "主题": b.get("主题"), "入池": b.get("入池"),
    }


def pick_stock_view(r, module):
    """候选股行 → 存档/页面用的扁平结构（行情 + 打分）。"""
    n = pan.num
    e8 = lambda v: None if n(v) is None else round(n(v) / 1e8, 4)
    return {
        "代码": r.get("代码"), "名称": r.get("名称"), "来源板块": r.get("来源板块"),
        "板块类型": r.get("板块类型"), "板块分": r.get("板块分"),
        "模块": module,
        "现价": n(r.get("f2")), "涨跌幅_pct": n(r.get("f3")),
        "5日_pct": n(r.get("f109")), "10日_pct": n(r.get("f160")),
        "20日_pct": n(r.get("f110")), "60日_pct": n(r.get("f24")),
        "年初至今_pct": n(r.get("f25")), "换手率_pct": n(r.get("f8")),
        "量比": n(r.get("f10")), "振幅_pct": n(r.get("f7")),
        "成交额_亿": e8(r.get("f6")), "主力净流入_亿": e8(r.get("f62")),
        "主力净占比_pct": n(r.get("f184")),
        "PE": pick_pe(r), "PB": n(r.get("f23")),
        "总市值_亿": e8(r.get("f20")), "流通市值_亿": e8(r.get("f21")),
        "所属行业": r.get("f100"),
        "机械分": r.get("机械分"), "评级": r.get("评级"), "维度": r.get("维度"),
    }


def pick_pe(r):
    """PE 取 f115（TTM）优先，缺失回退 f9（动态）；负值视为缺失。"""
    for k in ("f115", "f9"):
        v = pan.num(r.get(k))
        if v is not None and v > 0:
            return v
    return None


def pick_is_new(r):
    """次新/上市不足 60 日：f24 缺失，或 5/10/20/60 日与年初至今涨幅完全相等。"""
    if pan.num(r.get("f24")) is None:
        return True
    vals = [pan.num(r.get(k)) for k in ("f24", "f25", "f109", "f110", "f160")]
    if any(v is None for v in vals):
        return False
    return max(vals) == min(vals)


def pick_exclude_reason(r, exclude):
    """返回排除原因（不排除返回 None）。"""
    name = str(r.get("f14") or "")
    price, amount = pan.num(r.get("f2")), pan.num(r.get("f6"))
    if pan.num(r.get("f3")) is None or price is None or price <= 0:
        return "无行情/停牌"
    code6 = aiplan.code6(str(r.get("f12") or ""))
    if exclude.get("st", True) and ("ST" in name.upper()):
        return "ST"
    if exclude.get("low_amount", True) and (amount is None or amount < PICK_MIN_AMOUNT):
        return "成交额过低"
    if exclude.get("low_price", True) and price < PICK_MIN_PRICE:
        return "低价股"
    if exclude.get("new", True) and pick_is_new(r):
        return "次新/上市不足60日"
    if exclude.get("skip_688_bj", False) and code6 and re.match(r"^(688|8|4)", code6):
        return "科创板/北交所"
    return None


def pick_filter(rows, exclude):
    """按排除规则过滤，返回 (保留行, 排除统计)。"""
    kept, stats = [], {}
    for r in rows:
        why = pick_exclude_reason(r, exclude)
        if why:
            stats[why] = stats.get(why, 0) + 1
            continue
        kept.append(r)
    return kept, stats


def pick_score_stocks(rows, module):
    """候选池内按模块权重打分（0-100），就地写 机械分 / 评级 / 维度。"""
    def col(k):
        return [pan.num(r.get(k)) for r in rows]
    c3, c109, c160, c110, c24 = col("f3"), col("f109"), col("f160"), col("f110"), col("f24")
    c25, c184, c62, c8, c10 = col("f25"), col("f184"), col("f62"), col("f8"), col("f10")
    c7, c6, c21, c20 = col("f7"), col("f6"), col("f21"), col("f20")
    pe_col = [pick_pe(r) for r in rows]
    pb_col = [pan.num(r.get("f23")) for r in rows]
    for r in rows:
        g = lambda k: pan.num(r.get(k))
        p = lambda col_, v: pick_pct(col_, v)
        board = None if r.get("板块分") is None else r["板块分"] / 100.0
        mom_short = pick_wsum([(p(c3, g("f3")), 0.35), (p(c109, g("f109")), 0.35),
                               (p(c10, g("f10")), 0.30)])
        fund = pick_wsum([(p(c184, g("f184")), 0.5), (p(c62, g("f62")), 0.5)])
        elastic = pick_wsum([(p(c7, g("f7")), 0.5), (p(c8, g("f8")), 0.5)])
        mom_swing = pick_wsum([(p(c109, g("f109")), 0.4), (p(c160, g("f160")), 0.35),
                               (p(c110, g("f110")), 0.25)])
        p8 = p(c8, g("f8"))
        turn_mid = None if p8 is None else max(0.0, 1.0 - abs(p8 - 0.5) * 2.0)
        pe_v, pb_v = pick_pe(r), g("f23")
        pe_pct, pb_pct = p(pe_col, pe_v), p(pb_col, pb_v)
        val = pick_wsum([(None if pe_pct is None else 1.0 - pe_pct, 0.6),
                         (None if pb_pct is None else 1.0 - pb_pct, 0.4)])
        size_liq = pick_wsum([(p(c6, g("f6")), 0.5), (p(c21, g("f21")), 0.5)])
        dims = {
            "短线": {"动量": mom_short, "资金": fund, "弹性": elastic, "板块": board},
            "波段": {"波段动量": mom_swing, "资金": p(c62, g("f62")),
                     "换手适中": turn_mid, "板块": board},
            "中线": {"60日动量": p(c24, g("f24")), "资金占比": p(c184, g("f184")),
                     "估值": val, "市值流动性": size_liq, "板块": board},
            "长线": {"估值": val, "年初至今": p(c25, g("f25")), "市值": p(c20, g("f20")),
                     # 60 日趋势：跌得少/涨得多的标视为回撤更可控（方向与动量一致）
                     "60日趋势": p(c24, g("f24")), "板块": board},
        }[module]
        weights = PICK_MODULE_WEIGHTS[module]
        score = pick_wsum([(dims.get(k), w / 100.0) for k, w in weights])
        r["维度"] = {k: _pick_round(v) for k, v in dims.items()}
        r["机械分"] = None if score is None else round(100.0 * score, 1)
        r["评级"] = pick_grade(r["机械分"])
    return rows


def pick_concept_theme(name):
    """概念名 → 主题（有序关键词表，命中即返回；未命中落「其他」）。"""
    nm = str(name or "")
    for theme, kws in PICK_CONCEPT_THEMES:
        for kw in kws:
            if kw == "AI":
                if "AI" in nm.upper():
                    return theme
                continue
            if kw in nm:
                return theme
    return PICK_THEME_OTHER


def pick_param(body):
    """整理荐股筛选参数（带默认与上限保护）。"""
    body = body or {}
    # 模块支持多选：module / modules 合并后按固定顺序去重（同时传多个也只留一份）
    asked = [body.get("module")] + list(body.get("modules") or [])
    wanted = {str(m).strip() for m in asked if str(m or "").strip() in PICK_MODULE_CYCLE}
    modules = [name for name, _ in PICK_MODULES if name in wanted]
    if not modules:
        modules = [PICK_MODULES[0][0]]
    industry, seen_i = [], set()
    for raw in (body.get("industry") or []):
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip().upper()
        if not re.match(r"^BK\d{3,5}$", code) or code in seen_i:
            continue
        seen_i.add(code)
        subs = [str(x).strip() for x in (raw.get("subs") or []) if str(x).strip()]
        industry.append({"code": code,
                         "name": str(raw.get("name") or "").strip(),
                         "subs": subs[:40]})
    concepts, seen_c = [], set()
    for raw in (body.get("concepts") or []):
        code = str(raw or "").strip().upper()
        if re.match(r"^BK\d{3,5}$", code) and code not in seen_c:
            seen_c.add(code)
            concepts.append(code)
    concepts = concepts[:PICK_MAX_CONCEPTS]

    def clamp(v, lo, hi, dflt):
        try:
            v = int(v)
        except (TypeError, ValueError):
            return dflt
        return max(lo, min(hi, v))

    ex = body.get("exclude") or {}
    exclude = {
        "st": bool(ex.get("st", True)),
        "new": bool(ex.get("new", True)),
        "low_price": bool(ex.get("low_price", True)),
        "low_amount": bool(ex.get("low_amount", True)),
        "skip_688_bj": bool(ex.get("skip_688_bj", False)),
    }
    return {
        "modules": modules, "industry": industry, "concepts": concepts,
        "per_board": clamp(body.get("per_board"), 1, 20, PICK_PER_BOARD),
        "max_candidates": clamp(body.get("max_candidates"), 20, 2000, PICK_MAX_CANDIDATES),
        "max_chars": clamp(body.get("max_chars"), 4000, 200000, PICK_MAX_CHARS),
        "refresh": bool(body.get("refresh")),
        "profile": (body.get("profile") or "").strip() or None,
        "model_pro": (body.get("model_pro") or "").strip() or None,
        "api_base": (body.get("api_base") or "").strip() or None,
        "api_key": (body.get("api_key") or "").strip() or None,
        "exclude": exclude,
    }


def pick_table(rows, cols, headers):
    if not rows:
        return "（无数据）"
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c)
            if isinstance(v, float):
                v = ("%.4f" % v).rstrip("0").rstrip(".")
            cells.append("—" if v is None or v == "" else str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


PICK_BOARD_COLS = ["类型", "名称", "一级行业", "主题", "涨跌幅_pct", "5日_pct", "10日_pct",
                   "20日_pct", "60日_pct", "主力净流入_亿", "主力净占比_pct", "上涨家数",
                   "下跌家数", "成交额_亿", "机械分", "评级", "行情口径", "领涨股"]


PICK_BOARD_HEADS = ["类型", "名称", "一级行业", "主题", "涨跌%", "5日%", "10日%", "20日%",
                    "60日%", "主力净流入(亿)", "主力净占比%", "上涨", "下跌", "成交额(亿)",
                    "机械分", "评级", "行情口径", "领涨股"]


PICK_STOCK_COLS = ["代码", "名称", "来源板块", "板块分", "现价", "涨跌幅_pct", "5日_pct",
                   "10日_pct", "20日_pct", "60日_pct", "换手率_pct", "量比", "振幅_pct",
                   "成交额_亿", "主力净流入_亿", "主力净占比_pct", "PE", "PB",
                   "总市值_亿", "机械分", "评级"]


PICK_STOCK_HEADS = ["代码", "名称", "来源板块", "板块分", "现价", "涨跌%", "5日%", "10日%",
                    "20日%", "60日%", "换手%", "量比", "振幅%", "成交额(亿)",
                    "主力净流入(亿)", "主力净占比%", "PE", "PB", "总市值(亿)", "机械分", "评级"]


def _pick_factpack(payload, keep):
    """按 keep（每模块候选数）渲染事实包文本。"""
    p = payload.get("参数") or {}
    L = ["# 荐股事实包（东财行情 + pan 快照，中立数值，不含买卖建议）",
         "- 生成时间：%s ｜ 交易日参考：%s" % (payload.get("生成时间"), payload.get("交易日") or "—"),
         "- 模块：%s ｜ 只覆盖行业细分与概念两类板块（没有指数）"
         % "/".join(p.get("模块") or []),
         "- 候选池 %d 只 ｜ 每板块候选 %d 只 ｜ 候选上限 %s ｜ 分位均在本次池内计算"
         % (payload.get("候选池数量") or 0, p.get("每板块候选") or 0, p.get("候选上限") or "—")]
    sel = p.get("筛选") or {}
    ind = sel.get("行业") or []
    con = sel.get("概念") or []
    if ind:
        L.append("- 用户筛选的行业：%s"
                 % "；".join("%s（细分：%s）" % (x.get("名称"), x.get("细分"))
                             for x in ind))
    if con:
        L.append("- 用户筛选的概念（%d 个）：%s"
                 % (len(con), "、".join(str(x.get("名称")) for x in con[:40])))
    L.append("- 板块池是用户筛选出来的，不是全市场扫描；行情口径为「东财板块」或「成分股聚合」。")
    if payload.get("排除统计"):
        L.append("- 已排除：" + "，".join("%s %d 只" % (k, v)
                                        for k, v in payload["排除统计"].items()))
    if payload.get("降级"):
        L.append("- 降级项：" + "；".join(payload["降级"]))
    L.append("- 说明：PE 为负或缺失记 —；次新股已在排除规则中处理；无财务/分红/ROE 数据。")
    L.append("\n## 1 板块评估（机械打分 0-100）")
    for label, rows in (payload.get("板块") or {}).items():
        top = sorted(rows or [], key=lambda x: (x.get("机械分")
                                                if x.get("机械分") is not None else -1),
                     reverse=True)[:20]
        L.append("\n### %s板块（共 %d 个，下列为机械分前 %d 个）"
                 % (label, len(rows or []), len(top)))
        L.append(pick_table(top, PICK_BOARD_COLS, PICK_BOARD_HEADS))
    L.append("\n## 2 模块候选（按该模块机械分降序，每模块最多 %d 只）" % keep)
    for m, rows in (payload.get("候选") or {}).items():
        L.append("\n### 模块：%s（持有周期 %s）" % (m, PICK_MODULE_CYCLE.get(m, "")))
        L.append(pick_table((rows or [])[:keep], PICK_STOCK_COLS, PICK_STOCK_HEADS))
    return "\n".join(L)


def pick_factpack(payload, cap):
    """事实包；超出 cap 时逐步减少每模块候选数。"""
    keep = PICK_MODEL_TOP
    txt = _pick_factpack(payload, keep)
    while len(txt) > cap and keep > 2:
        keep -= 1
        txt = _pick_factpack(payload, keep)
    return txt


def pick_markdown(p):
    """人读版 Markdown（存档用）。"""
    par = p.get("参数") or {}
    sel = par.get("筛选") or {}
    ind, con = sel.get("行业") or [], sel.get("概念") or []
    L = ["# 荐股结果 · aiplan Web UI", "",
         "- 生成时间：%s ｜ 模块：%s ｜ 板块类型：%s"
         % (p.get("生成时间"), "/".join(par.get("模块") or []),
            "/".join([t for t, v in (p.get("板块") or {}).items() if v]) or "—"),
         "- 筛选：行业 %d 个（细分 %s）｜ 概念 %d 个"
         % (len(ind), "、".join("（".join([x.get("名称") or "", str(x.get("细分"))]) + "）"
                                for x in ind) or "—", len(con)),
         "- 每板块候选 %s 只 ｜ 候选上限 %s" % (par.get("每板块候选"), par.get("候选上限")),
         "- 候选池 %d 只 ｜ 交易日参考 %s ｜ 模型 %s ｜ 费用 %s"
         % (p.get("候选池数量") or 0, p.get("交易日") or "—",
            (p.get("配置") or {}).get("model") or "未点评",
            (p.get("成本") or {}).get("人民币_估算")
            if (p.get("成本") or {}).get("人民币_估算") is not None else "未配置单价"),
         "- 事实包 %d 字符 ｜ 用时见任务日志" % (p.get("factpack_chars") or 0), ""]
    grade = p.get("总评") or {}
    if grade:
        L += ["## 总评", "",
              "- 一句话：%s" % (grade.get("一句话") or "—"),
              "- 多空倾向：%s ｜ 最强模块：%s ｜ 最强板块：%s"
              % (grade.get("多空倾向") or "—", grade.get("最强模块") or "—",
                 grade.get("最强板块") or "—"),
              "- 操作节奏：%s" % (grade.get("操作节奏") or "—"), ""]
    if (p.get("模型层") or {}).get("error"):
        L += ["> **[WARN] 本次没有模型点评**：%s（下列为本地机械打分榜）"
              % (p.get("模型层") or {}).get("error"), ""]

    mods = p.get("模块") or []
    if mods:
        L += ["## 模块评估", ""]
        for m in mods:
            L += ["### %s（评分 %s）" % (m.get("模块") or "—", m.get("评分") if m.get("评分") is not None else "—"),
                  "", "- 逻辑：%s" % (m.get("逻辑") or "—"),
                  "- 介入节奏：%s" % (m.get("介入节奏") or "—"),
                  "- 失效条件：%s" % (m.get("失效条件") or "—"), ""]
            for c in (m.get("首推") or []):
                buy = c.get("关注买点") or {}
                stop = c.get("止损") or {}
                L.append("**%s %s**（%s，%s）" % (c.get("代码") or "—", c.get("名称") or "",
                                              c.get("评级") or "—", c.get("所属板块") or "—"))
                L.append("")
                L.append("- 理由：%s" % (c.get("理由") or "—"))
                L.append("- 关注买点：%s（%s）" % (buy.get("价位") or "—", buy.get("依据") or "—"))
                L.append("- 止损：%s（%s）" % (stop.get("价位") or "—", stop.get("依据") or "—"))
                for t in (c.get("目标位") or []):
                    L.append("- 目标位：%s（%s）" % (t.get("价位") or "—", t.get("依据") or "—"))
                L.append("- 风险：%s" % (c.get("风险") or "—"))
                L.append("- 失效条件：%s" % (c.get("失效条件") or "—"))
                L.append("")
    evals = p.get("板块评估") or []
    if evals:
        L += ["## 模型板块评估", "",
              pick_table([{"类型": e.get("类型"), "名称": e.get("名称"), "评级": e.get("评级"),
                           "驱动": e.get("驱动"), "代表股": "、".join(e.get("代表股") or []),
                           "风险": e.get("风险")} for e in evals],
                         ["类型", "名称", "评级", "驱动", "代表股", "风险"],
                         ["类型", "名称", "评级", "驱动", "代表股", "风险"]), ""]
    L += ["## 机械层：板块打分", ""]
    for label, rows in (p.get("板块") or {}).items():
        top = sorted(rows or [], key=lambda x: (x.get("机械分")
                                               if x.get("机械分") is not None else -1),
                     reverse=True)
        L += ["### %s板块（%d 个）" % (label, len(top)), "",
              pick_table(top, PICK_BOARD_COLS, PICK_BOARD_HEADS), ""]
    L += ["## 机械层：模块候选", ""]
    for m, rows in (p.get("候选") or {}).items():
        L += ["### %s" % m, "", pick_table(rows or [], PICK_STOCK_COLS, PICK_STOCK_HEADS), ""]
    if p.get("降级"):
        L += ["## 数据依赖与降级", ""] + ["- %s" % x for x in p["降级"]] + [""]
    if p.get("降级与不确定性"):
        L += ["## 模型提示的不确定性", ""] + ["- %s" % x for x in p["降级与不确定性"]] + [""]
    L += ["## 说明", "",
          "- 机械分为本地打分（模块权重不同，分位在本次候选池内计算），不是收益预测。",
          "- %s" % (p.get("免责声明") or "本结论由机械打分与模型点评生成，不构成投资建议。"), ""]
    return "\n".join(L)
