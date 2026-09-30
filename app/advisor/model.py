"""计算二：建议区间、健康度、置信度、预估与解释（PRD v2.3 第 10 章；9.1；R-01 到 R-08）。

纯函数。输入单个 SKU 的基础量（calc.base_quantities 的输出）、生效弹性、竞品价与参数，输出一条建议。
"""
from __future__ import annotations

import math
import statistics
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

NEAR, LOW, HIGH, OK, SHORT = "贴近成本", "偏低", "偏高", "合理", "数据不足"
INFEASIBLE = "保本价也无法在目标天数内清完，建议考虑捆绑或其他渠道"
LOW_CONF = "基于类目经验值，你的记录还不够，先小幅试"


def _q(x: float, mode) -> float:
    return float(Decimal(repr(x)).quantize(Decimal("0.01"), mode))


def mul(a: float, b: float) -> float:
    """两个金额或系数相乘，用十进制算，避免 19.9 × 1.3 = 25.869999… 截成 25.86。"""
    return float(Decimal(repr(a)) * Decimal(repr(b)))


def ceil_cents(x): return _q(x, ROUND_CEILING)
def floor_cents(x): return _q(x, ROUND_FLOOR)
def cents(x): return _q(x, ROUND_HALF_UP)


class Estimator:
    """按价格估日销量、日毛利等。转化路用弹性，曝光路用曝光倍数（默认 1，未计）。"""

    def __init__(self, q: dict, e: float, p: dict):
        self.p0 = q["当前价"]
        self.base_units = q["近30天日均"]
        self.e = e
        self.exposure = p["曝光倍数"]
        self.keep = 1 - p["扣点率"] - p["退货率"] * p["单次退货净损失占比"]
        self.fixed = (q["成本价"] or 0) + p["单件履约费"] + unit_ad_raw(q)
        self.stock = q["库存"]
        v = q.get("近30天访客数")
        self.conv0 = q["近30天成交件数"] / v if v else None

    def factor(self, price: float) -> float:
        return (price / self.p0) ** self.e

    def at(self, price: float) -> dict:
        f = self.factor(price)
        units = self.base_units * f * self.exposure
        margin = price * self.keep - self.fixed
        return {
            "价格": price,
            "日销量": units,
            "单件毛利": margin,
            "日毛利": margin * units,
            "日成交额": price * units,
            "转化率": self.conv0 * f if self.conv0 is not None else None,
            "库存清完需": self.stock / units if units > 0 else None,
        }


def unit_ad_raw(q: dict) -> float:
    return q["近30天广告花费"] / q["近30天广告成交件数"] if q["近30天广告成交件数"] else 0.0


def breakeven_raw(q: dict, p: dict) -> float:
    """未取整的保本价。候选价下限用它向上取到分，保证建议价的单件毛利不为负；页面显示的保本价仍四舍五入到分。"""
    return ((q["成本价"] or 0) + p["单件履约费"] + unit_ad_raw(q)) / (1 - p["扣点率"] - p["退货率"] * p["单次退货净损失占比"])


def resolve_elasticity(category: str, calib: dict, settings_row: dict, p: dict) -> dict:
    """该类目改价事件 ≥ 3 个用本店校准中位数，否则用设置里的基准（默认类目先验）；再叠加商家微调，夹在硬限内。"""
    lo, hi = p["弹性硬限"]
    c = calib.get(category)
    if c and c["有效事件数"] >= 3 and c["事件中位数"] is not None:
        base, src = c["事件中位数"], f"本店改价事件校准（{c['有效事件数']} 个事件）"
        err = c["校准误差"]
    else:
        base = settings_row["基准"]
        src = "类目先验" if settings_row["来源"] == "先验" else "设置里填的基准"
        err = None
    adj = settings_row["微调"]
    eff = min(max(base * (1 + adj), lo), hi)
    return {"基准": base, "微调": adj, "生效": eff, "来源": src, "校准误差": err, "用先验": err is None}


def suggest(q: dict, el: dict, comps: list[float], p: dict) -> dict:
    """单个 SKU 的建议。数据不足的 SKU 不出区间，健康度直接为数据不足。"""
    out = {"健康度": SHORT if q["数据状态"] != "正常" else None, "区间": None, "最优价": None,
           "置信度": None, "可增毛利": None, "预估": None, "解释": [], "提示": [], "弹性": el}
    if q["数据状态"] != "正常":
        out["解释"] = [f"数据不足：{q['数据不足原因']}，不出建议区间。"]
        return out

    stage = q["生命周期"]
    m = p["尾货期最低毛利率"] if stage == "尾货期" else p["最低毛利率"]
    be, p0 = q["保本价"], q["当前价"]
    be_raw = breakeven_raw(q, p)
    est = Estimator(q, el["生效"], p)
    lo = ceil_cents(be_raw * (1 + m))
    hi = mul(p0, p["候选价上限倍数"])
    step = p0 * p["候选价步长"]
    notes, reason_bits = [], []

    # R-04 竞品裁剪：只裁范围，不改目标；裁不到保本线以下
    comp_note = None
    if len(comps) >= 2:
        med = statistics.median(comps)
        rule = p["竞品裁剪"]
        diff = p0 / med - 1
        comp_note = f"竞品中位价 ¥{med:.2f}，当前价{'高出' if diff >= 0 else '低于'} {abs(diff):.0%}"
        if p0 > med * (1 + rule["高于中位价"]):
            cap = mul(med, rule["上限倍数"])
            if cap < lo:
                hi = lo
                comp_note += f"，竞品价低于保本线，最高建议价只能压到最低建议价 ¥{lo:.2f}"
            else:
                hi = min(hi, cap)
                comp_note += f"，最高建议价压到 ¥{floor_cents(hi):.2f}"
        elif p0 < med * (1 - rule["低于中位价"]) and stage != "尾货期":
            lo = max(lo, p0)
            comp_note += "，最低建议价抬到当前价"
    elif len(comps) == 1:
        comp_note = "竞品少于 2 个，未对标"

    # 候选价：保本价 ×（1 + 最低毛利率）到当前价 × 1.3，步长当前价的 1%
    cands, k = [], 0
    while cents(lo + k * step) <= hi + 1e-9:
        cands.append(max(cents(lo + k * step), lo))
        k += 1
    top_end = floor_cents(hi)
    if top_end >= lo and (not cands or top_end > cands[-1]):
        cands.append(top_end)  # 上端点也算一个候选价
    cands = sorted(set(cands))
    if not cands:
        out["健康度"] = NEAR if q["贴近成本"] else LOW
        out["解释"] = [f"当前价的 {p['候选价上限倍数']:g} 倍仍低于保本价 ×（1 + 最低毛利率）¥{lo:.2f}，给不出建议区间，请先核对成本价。"]
        return out
    rows = {c: est.at(c) for c in cands}

    # R-02 生命周期定目标；R-03 成熟期库存超滞销线追加约束；R-05 无可行价时的提示
    need = None
    if stage == "新品期":
        pool = [c for c in cands if (c - be_raw) / c >= m - 1e-12] or cands
        key = "日销量"
        reason = f"这款处于新品期，目标是在毛利率不低于 {m:.0%} 的前提下日销最大"
    else:
        pool, key = cands, "日毛利"
        if stage == "尾货期":
            need = q["库存"] / p["目标清仓天数"]
            reason = f"这款处于尾货期，库存够卖 {_days(q['库存天数'])}，目标 {p['目标清仓天数']} 天清完，需要日销 {need:.1f} 件"
        else:
            reason = "这款处于成熟期，目标是日毛利最大"
            if q["库存天数"] is not None and q["库存天数"] > p["滞销线"]:
                need = q["库存"] / p["滞销线"]
                reason += f"；库存够卖 {_days(q['库存天数'])}，超过滞销线 {p['滞销线']} 天，需要日销 {need:.1f} 件"
        if need is not None:
            feas = [c for c in cands if rows[c]["日销量"] >= need - 1e-9]
            if feas:
                pool = feas
            else:
                notes.append(INFEASIBLE)
                key = "日销量"  # 清不完：退而求最快清，仍不低于保本线
    best = max(pool, key=lambda c: (rows[c][key], -c))
    top = rows[best][key]
    keep = [c for c in pool if rows[c][key] >= top * p["区间宽度"] - 1e-12] if top > 0 else [best]
    low_p, high_p = min(keep), max(keep)

    if q["贴近成本"]:
        health = NEAR
    elif p0 < low_p - 1e-9:
        health = LOW
    elif p0 > high_p + 1e-9:
        health = HIGH
    else:
        health = OK

    # 置信度：高 = ≥ 2 次改价事件且校准误差 ≤ 20%；中 = 记录 ≥ 30 天；低 = 其余。未计曝光变化时不得为高
    cf = p["置信度"]
    exposure_counted = p["曝光倍数"] != 1
    if (len(q["改价事件"]) >= cf["高_改价事件数"] and el["校准误差"] is not None
            and el["校准误差"] <= cf["高_校准误差"] and exposure_counted):
        conf = "高"
    elif q["记录天数"] >= cf["中_记录天数"]:
        conf = "中"
    else:
        conf = "低"

    cur, opt = est.at(p0), rows[best]
    gain = (opt["日毛利"] - cur["日毛利"]) * p["预计可增毛利天数"]
    no_vis = est.conv0 is None

    # 解释四句：结论、原因、预估、边界
    if abs(best - p0) < step / 2:
        s1 = f"建议维持 ¥{p0:.2f}（区间 ¥{low_p:.2f} ～ ¥{high_p:.2f}）。"
    else:
        s1 = f"建议从 ¥{p0:.2f} 调到 ¥{best:.2f}（区间 ¥{low_p:.2f} ～ ¥{high_p:.2f}）。"
    if conf == "低":
        s1 = f"{LOW_CONF}。" + s1
    health_bit = {NEAR: f"当前价低于保本价 × {p['贴近成本系数']}，贴近成本", LOW: "当前价低于最低建议价",
                  HIGH: "当前价高于最高建议价", OK: "当前价在建议区间内"}[health]
    s2 = f"{reason}；{health_bit}" + (f"；{comp_note}" if comp_note else "") + "。"
    s3 = (f"预计日销从 {cur['日销量']:.1f} 件到 {opt['日销量']:.1f} 件，日毛利从 ¥{cur['日毛利']:.2f} 到 ¥{opt['日毛利']:.2f}"
          + ("，按销量弹性估" if no_vis else "") + ("" if exposure_counted else "，未计曝光变化") + "。")
    s4 = f"保本价 ¥{be:.2f}"
    if q["广告口径"] == "含广告":
        s4 += f"（含单件广告成本 ¥{q['单件广告成本']:.2f}）"
    s4 += f"，最低建议价已含保本线（最低毛利率 {m:.0%}）"
    if el["用先验"]:
        s4 += f"；弹性 {el['生效']:.2f} 基于类目经验值"
    else:
        s4 += f"；弹性 {el['生效']:.2f} 来自{el['来源']}"
    if notes:
        s4 += "；" + "；".join(notes)
    s4 += "。"

    out.update({
        "健康度": health, "区间": [low_p, best, high_p], "最优价": best, "置信度": conf, "可增毛利": gain,
        "预估": {"当前": cur, "建议": opt, "无访客数": no_vis, "未计曝光": not exposure_counted},
        "解释": [s1, s2, s3, s4], "提示": notes, "候选下限": lo, "候选上限": floor_cents(hi),
        "竞品": {"个数": len(comps), "中位价": statistics.median(comps) if comps else None, "说明": comp_note},
    })
    return out


def _days(v):
    return "无动销" if v is None else f"{v:.0f} 天"


def estimate_rows(cur: dict, new: dict, no_vis: bool, exposure_note: bool = True) -> list[dict]:
    """预估对比六行：变化列按对店主的好坏给方向（销量、毛利、成交额、转化率升为变好；清完天数降为变好）。"""
    def row(name, a, b, fmt, better_up=True):
        if a is None or b is None:
            return {"指标": name, "当前": fmt(a), "建议": fmt(b), "变化": "—", "好坏": None}
        d = b - a
        rel = d / a if a else None
        chg = f"{rel:+.1%}" if rel is not None and name != "库存清完需" else (f"{d:+.0f} 天" if name == "库存清完需" else f"{d:+.2f}")
        good = None if abs(d) < 1e-9 else ((d > 0) == better_up)
        return {"指标": name, "当前": fmt(a), "建议": fmt(b), "变化": chg, "好坏": good}
    rows = [
        row("日销量", cur["日销量"], new["日销量"], lambda v: "—" if v is None else f"{v:.1f} 件"),
        row("日毛利", cur["日毛利"], new["日毛利"], lambda v: "—" if v is None else f"¥{v:.2f}"),
        row("日成交额", cur["日成交额"], new["日成交额"], lambda v: "—" if v is None else f"¥{v:.2f}"),
    ]
    if no_vis:
        rows.append({"指标": "转化率", "当前": "无访客数", "建议": "无访客数", "变化": "无访客数", "好坏": None})
    else:
        r = row("转化率", cur["转化率"], new["转化率"], lambda v: "—" if v is None else f"{v:.1%}")
        if cur["转化率"] and new["转化率"]:
            r["变化"] = f"{(new['转化率'] - cur['转化率']) * 100:+.1f} pt"
        rows.append(r)
    rows.append(row("库存清完需", cur["库存清完需"], new["库存清完需"], lambda v: "—" if v is None else f"{v:.0f} 天", better_up=False))
    rows.append({"指标": "曝光变化", "当前": "未计", "建议": "未计", "变化": "未计", "好坏": None} if exposure_note else
                {"指标": "曝光变化", "当前": "—", "建议": "—", "变化": "—", "好坏": None})
    return rows


def direction(s: dict) -> str | None:
    """建议方向：最优价相对当前价涨、降还是维持（相差不到半个步长算维持）。用于忽略后 7 天不再提示同向建议。"""
    if not s or not s.get("区间"):
        return None
    p0 = s["预估"]["当前"]["价格"]
    step = p0 * 0.005
    if s["最优价"] > p0 + step:
        return "涨价"
    if s["最优价"] < p0 - step:
        return "降价"
    return "维持"
