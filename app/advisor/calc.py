"""计算一：基础量（PRD v2.3 术语、9.1、12.1；R-01、R-06）。

纯函数，不读写数据库。门 0 体检（price-advisor/gate0）用的是同一套口径，产品里可复现门 0 结论。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

STAGE_NEW, STAGE_MATURE, STAGE_TAIL = "新品期", "成熟期", "尾货期"
STAGES = (STAGE_NEW, STAGE_MATURE, STAGE_TAIL)
R_SHORT, R_NOCOST, R_NOSALE = "记录不足 14 天", "缺成本价", "无动销"


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    category: str
    listed: date | None
    price: float
    cost: float | None
    stock: float
    manual_stage: str | None = None


@dataclass(frozen=True)
class DayRec:
    day: date
    price: float
    units: float
    amount: float
    visitors: float | None = None
    ad_spend: float | None = None
    ad_units: float | None = None


def cents(x: float) -> float:
    """金额四舍五入到分（ROUND_HALF_UP，不用 round() 的银行家舍入）。"""
    return float(Decimal(repr(x)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def pct2(x: float) -> float:
    """比例保留到百分比两位小数：0.355639 → 0.3556。"""
    return float(Decimal(repr(x)).quantize(Decimal("0.0001"), ROUND_HALF_UP))


def denominator(p: dict) -> float:
    return 1 - p["扣点率"] - p["退货率"] * p["单次退货净损失占比"]


def unit_ad_cost(recs: dict[date, DayRec], window: list[date]) -> tuple[float, float, float, str]:
    """单件广告成本 = 近 30 天广告花费 ÷ 广告成交件数；无数据或广告成交为 0 时取 0。"""
    spend = sum((recs[d].ad_spend or 0) for d in window if d in recs)
    units = sum((recs[d].ad_units or 0) for d in window if d in recs)
    if units > 0:
        return spend / units, spend, units, "含广告"
    if spend > 0:
        return 0.0, spend, units, "有广告花费但无广告成交，按 0 计"
    return 0.0, spend, units, "无广告数据"


def breakeven(cost: float, p: dict, unit_ad: float = 0.0) -> float:
    """保本价 =（成本价 + 单件履约费 + 单件广告成本）÷（1 − 扣点率 − 退货率 × 单次退货净损失占比），到分。"""
    return cents((cost + p["单件履约费"] + unit_ad) / denominator(p))


def days_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def activity_days(recs: dict[date, DayRec], p: dict) -> set[date]:
    """活动日：当日成交件数 ＞ 此前 30 天中位数 × 3。此前有记录的日子不足 7 天或中位数为 0 时不判（C-03）。"""
    n, k = p["近期天数"], p["活动日倍数"]
    out = set()
    for d, r in recs.items():
        prior = [recs[x].units for x in days_between(d - timedelta(days=n), d - timedelta(days=1)) if x in recs]
        if len(prior) < 7:
            continue
        med = statistics.median(prior)
        if med > 0 and r.units > k * med:
            out.add(d)
    return out


def price_events(recs: dict[date, DayRec], p: dict) -> list[date]:
    """改价事件：当日售价与前 7 天每一天的差都 ≥ 最小幅度（默认 1%），且前 7 天、后 7 天（含当日）记录齐全（C-04）。
    几分钱的日常波动不算改价。"""
    w, k = p["改价事件窗口天数"], p["改价事件最小幅度"]
    out = []
    for d in sorted(recs):
        before = [d - timedelta(days=i) for i in range(1, w + 1)]
        after = [d + timedelta(days=i) for i in range(0, w)]
        if all(x in recs for x in before) and all(x in recs for x in after) \
                and all(abs(recs[d].price - recs[x].price) >= k * recs[x].price - 1e-9 for x in before):
            out.append(d)
    return out


def lifecycle(prod: Product, listed_days, total_units, stock_days, avg30, avg_prev, p) -> tuple[str, str]:
    if prod.manual_stage in STAGES:
        return prod.manual_stage, "店主标记"
    if (listed_days is not None and listed_days <= p["新品期上架天数"]) or total_units < p["新品期累计成交"]:
        return STAGE_NEW, "自动"
    if (stock_days is None or stock_days > p["滞销线"]) and avg30 < avg_prev * p["尾货期日均比"]:
        return STAGE_TAIL, "自动"
    return STAGE_MATURE, "自动"


def base_quantities(prod: Product, recs: dict[date, DayRec], today: date, p: dict) -> dict:
    """单个 SKU 的全部基础量。today = 全店销售记录的最后一天。"""
    n = p["近期天数"]
    last = days_between(today - timedelta(days=n - 1), today)
    prev = days_between(today - timedelta(days=2 * n - 1), today - timedelta(days=n))
    u_last = sum(recs[d].units for d in last if d in recs)
    u_prev = sum(recs[d].units for d in prev if d in recs)
    avg30, avg_prev = u_last / n, u_prev / n
    unit_ad, spend, ad_units, ad_note = unit_ad_cost(recs, last)
    be = breakeven(prod.cost, p, unit_ad) if prod.cost is not None else None
    be_noad = breakeven(prod.cost, p) if prod.cost is not None else None
    margin = pct2((prod.price - be) / prod.price) if be is not None else None
    stock_days = prod.stock / avg30 if avg30 > 0 else None
    listed_days = (today - prod.listed).days if prod.listed else None
    total_units = sum(r.units for r in recs.values())
    stage, stage_src = lifecycle(prod, listed_days, total_units, stock_days, avg30, avg_prev, p)

    reasons = []
    if len(recs) < p["数据不足记录天数"]:
        reasons.append(R_SHORT)
    if prod.cost is None:
        reasons.append(R_NOCOST)
    if avg30 == 0:
        reasons.append(R_NOSALE)

    near_line = cents(be * p["贴近成本系数"]) if be is not None else None
    ok = not reasons
    near = bool(ok and prod.price < be * p["贴近成本系数"])
    high = bool(ok and stock_days > p["滞销线"] and margin > p["疑似偏高毛利率线"])
    low = bool(ok and stock_days < p["疑似偏低库存天数线"] and margin < p["疑似偏低毛利率线"])
    over = bool(ok and stock_days > p["滞销线"])
    visitors30 = sum((recs[d].visitors or 0) for d in last if d in recs)
    has_vis30 = all(recs[d].visitors for d in last if d in recs) and any(d in recs for d in last)
    acts = sorted(activity_days(recs, p))
    evs = price_events(recs, p)
    return {
        "商品编号": prod.sku, "商品名称": prod.name, "类目": prod.category,
        "上架天数": listed_days, "当前价": prod.price, "成本价": prod.cost,
        "近30天广告花费": spend, "近30天广告成交件数": ad_units,
        "单件广告成本": cents(unit_ad), "广告口径": ad_note,
        "保本价分母": round(denominator(p), 6), "保本价": be, "不含广告保本价": be_noad,
        "毛利率": margin,
        "近30天成交件数": u_last, "近30天日均": avg30, "前30天日均": avg_prev,
        "库存": prod.stock, "库存天数": stock_days,
        "累计成交": total_units, "记录天数": len(recs),
        "生命周期": stage, "生命周期来源": stage_src,
        "数据状态": "数据不足" if reasons else "正常", "数据不足原因": "、".join(reasons),
        "贴近成本线": near_line, "贴近成本": near, "疑似偏高": high, "疑似偏低": low, "库存超滞销线": over,
        "近30天访客数": visitors30 if has_vis30 else None,
        "改价事件": [{"日期": d.isoformat(), "旧价": recs[d - timedelta(days=1)].price, "新价": recs[d].price} for d in evs],
        "活动日": [d.isoformat() for d in acts],
        "有访客数": any(r.visitors for r in recs.values()),
        "今天": today.isoformat(),
    }


def compute_all(products: list[Product], sales: dict[str, dict[date, DayRec]], p: dict) -> tuple[date | None, list[dict]]:
    days = [d for recs in sales.values() for d in recs]
    if not days:
        return None, []
    today = max(days)
    return today, [base_quantities(prod, sales.get(prod.sku, {}), today, p) for prod in products]


def event_elasticity(recs: dict[date, DayRec], d: date, acts: set[date], p: dict) -> dict:
    """PRD 12.2：弹性 = ln（改价后 7 天转化率 ÷ 改价前 7 天转化率）÷ ln（新价 ÷ 旧价）。
    窗口内活动日不参与；没有访客数时用日均销量代替转化率，得到销量弹性。"""
    import math
    w = p["改价事件窗口天数"]
    lo, hi = p["弹性硬限"]
    before = [d - timedelta(days=i) for i in range(w, 0, -1)]
    after = [d + timedelta(days=i) for i in range(0, w)]
    b = [recs[x] for x in before if x not in acts]
    a = [recs[x] for x in after if x not in acts]
    old, new = recs[d - timedelta(days=1)].price, recs[d].price
    res = {"改价日": d.isoformat(), "旧价": old, "新价": new, "剔除活动日": sum(1 for x in before + after if x in acts),
           "口径": "", "改价前": None, "改价后": None, "弹性": None, "访客倍数": None, "备注": ""}
    if not b or not a:
        return {**res, "备注": "窗口内全是活动日"}
    if all(r.visitors for r in a + b):
        rb = sum(r.units for r in b) / sum(r.visitors for r in b)
        ra = sum(r.units for r in a) / sum(r.visitors for r in a)
        res["口径"] = "转化弹性"
        res["访客倍数"] = (sum(r.visitors for r in a) / len(a)) / (sum(r.visitors for r in b) / len(b))
    else:
        rb = sum(r.units for r in b) / len(b)
        ra = sum(r.units for r in a) / len(a)
        res["口径"] = "销量弹性"
    res["改价前"], res["改价后"] = rb, ra
    if rb > 0 and ra > 0:
        e = math.log(ra / rb) / math.log(new / old)
        res["弹性"] = e
        if not (lo <= e <= hi):
            res["备注"] = f"超出硬限 {lo:g} 到 {hi:g}，只记录不采用"
    else:
        res["备注"] = "窗口内无成交，无法算"
    return res


def calibrate(events: list[dict]) -> dict[str, dict]:
    """按类目取有效事件弹性的中位数与校准误差（C-05：中位绝对偏差 ÷ |中位数|）。"""
    by: dict[str, list[float]] = {}
    for e in events:
        if e["弹性"] is not None and not e["备注"]:
            by.setdefault(e["类目"], []).append(e["弹性"])
    out = {}
    for cat in sorted({e["类目"] for e in events}):
        vals = by.get(cat, [])
        med = statistics.median(vals) if vals else None
        err = statistics.median([abs(v - med) for v in vals]) / abs(med) if vals and med else None
        out[cat] = {"类目": cat, "有效事件数": len(vals), "事件中位数": med, "校准误差": err}
    return out
