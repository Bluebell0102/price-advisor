"""门 0 体检计算，口径按 PRD v2.3 第 9.1、12.1、12.2、12.4 节与研发步骤 C-01、C-02。

全部是纯函数：输入 Dataset，输出字典列表，不读写文件。
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .io import Dataset, DayRec, Product

STAGE_NEW, STAGE_MATURE, STAGE_TAIL = "新品期", "成熟期", "尾货期"
R_SHORT, R_NOCOST, R_NOSALE = "记录不足 14 天", "缺成本价", "无动销"


def cents(x: float) -> float:
    """金额四舍五入到分（不是银行家舍入）。"""
    return float(Decimal(repr(x)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def pct2(x: float) -> float:
    """比例保留到百分比两位小数，例如 0.355639 → 0.3556。"""
    return float(Decimal(repr(x)).quantize(Decimal("0.0001"), ROUND_HALF_UP))


def loss_rate(p: dict) -> float:
    return p["退货率"] * p["单次退货净损失占比"]


def breakeven(cost: float, p: dict, unit_ad: float = 0.0) -> float:
    """保本价 =（成本价 + 单件履约费 + 单件广告成本）÷（1 − 扣点率 − 退货率 × 单次退货净损失占比）。"""
    return cents((cost + p["单件履约费"] + unit_ad) / (1 - p["扣点率"] - loss_rate(p)))


def days_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def activity_days(recs: dict[date, DayRec]) -> set[date]:
    """活动日：当日成交件数 ＞ 此前 30 天中位数 × 3。

    口径补充（C-03）：此前 30 天有记录的日子少于 7 天、或中位数为 0 时不判，避免慢动销 SKU 卖 1 件就算活动日。
    """
    out = set()
    for d, r in recs.items():
        prior = [recs[x].units for x in days_between(d - timedelta(days=30), d - timedelta(days=1)) if x in recs]
        if len(prior) < 7:
            continue
        med = statistics.median(prior)
        if med > 0 and r.units > 3 * med:
            out.add(d)
    return out


def price_events(recs: dict[date, DayRec], since: date, min_change: float = 0.01) -> list[date]:
    """改价事件：当日售价与前 7 天每一天的差都 ≥ 最小幅度（默认 1%），且前 7 天、后 7 天（含当日）记录齐全。

    口径补充（C-04）：「与前 7 天售价不同」按「与前 7 天的每一天都不同」判，一次调价只记一个事件；
    差不到 1% 的日常几分钱波动不算改价。
    """
    out = []
    for d in sorted(recs):
        if d < since:
            continue
        before = [d - timedelta(days=k) for k in range(1, 8)]
        after = [d + timedelta(days=k) for k in range(0, 7)]
        if not all(x in recs for x in before) or not all(x in recs for x in after):
            continue
        if all(abs(recs[d].price - recs[x].price) >= min_change * recs[x].price - 1e-9 for x in before):
            out.append(d)
    return out


def event_elasticity(recs: dict[date, DayRec], d: date, acts: set[date]) -> dict:
    """PRD 12.2：弹性 = ln（改价后 7 天转化率 ÷ 改价前 7 天转化率）÷ ln（新价 ÷ 旧价）。

    窗口内活动日不参与。没有访客数时用日均销量代替转化率，得到销量弹性。
    """
    before = [d - timedelta(days=k) for k in range(7, 0, -1)]
    after = [d + timedelta(days=k) for k in range(0, 7)]
    b = [recs[x] for x in before if x not in acts]
    a = [recs[x] for x in after if x not in acts]
    old, new = recs[d - timedelta(days=1)].price, recs[d].price
    has_vis = all(r.visitors for r in b + a)
    res = {"改价日": d, "旧价": old, "新价": new, "剔除活动日": sum(1 for x in before + after if x in acts)}
    if not b or not a:
        return {**res, "口径": "", "改价前": None, "改价后": None, "弹性": None, "访客倍数": None, "备注": "窗口内全是活动日"}
    if has_vis:
        rb = sum(r.units for r in b) / sum(r.visitors for r in b)
        ra = sum(r.units for r in a) / sum(r.visitors for r in a)
        mode = "转化弹性"
        vis_mult = (sum(r.visitors for r in a) / len(a)) / (sum(r.visitors for r in b) / len(b))
    else:
        rb = sum(r.units for r in b) / len(b)
        ra = sum(r.units for r in a) / len(a)
        mode = "销量弹性"
        vis_mult = None
    e = None
    note = ""
    if rb > 0 and ra > 0:
        e = math.log(ra / rb) / math.log(new / old)
        if not (-4 <= e <= -0.3):
            note = "超出硬限 −0.3 到 −4，只记录不采用"
    else:
        note = "窗口内无成交，无法算"
    return {**res, "口径": mode, "改价前": rb, "改价后": ra, "弹性": e, "访客倍数": vis_mult, "备注": note}


def analyze(ds: Dataset) -> dict:
    p = ds.params
    by_sku: dict[str, dict[date, DayRec]] = defaultdict(dict)
    for r in ds.recs:
        by_sku[r.sku][r.day] = r
    all_days = [r.day for r in ds.recs]
    today: date = p.get("今天") or max(all_days)
    first_day = min(all_days)
    last30 = days_between(today - timedelta(days=29), today)
    prev30 = days_between(today - timedelta(days=59), today - timedelta(days=30))
    since90 = today - timedelta(days=89)
    comps = defaultdict(list)
    for c in ds.comps:
        comps[c.sku].append(c.price)

    skus, events, acts_rows = [], [], []
    for prod in ds.products:
        recs = by_sku.get(prod.sku, {})
        u30 = sum(recs[d].units for d in last30 if d in recs)
        u_prev = sum(recs[d].units for d in prev30 if d in recs)
        avg30, avg_prev = u30 / 30, u_prev / 30
        spend30 = sum((recs[d].ad_spend or 0) for d in last30 if d in recs)
        adu30 = sum((recs[d].ad_units or 0) for d in last30 if d in recs)
        if adu30 > 0:
            unit_ad, ad_note = spend30 / adu30, "含广告"
        elif spend30 > 0:
            unit_ad, ad_note = 0.0, "有花费无广告成交，按 0 计"
        else:
            unit_ad, ad_note = 0.0, "无广告数据"
        be = breakeven(prod.cost, p, unit_ad) if prod.cost is not None else None
        be_noad = breakeven(prod.cost, p) if prod.cost is not None else None
        margin = pct2((prod.price - be) / prod.price) if be is not None else None
        stock_days = prod.stock / avg30 if avg30 > 0 else None
        listed_days = (today - prod.listed).days if prod.listed else None
        total_units = sum(r.units for r in recs.values())

        if prod.manual_stage in (STAGE_NEW, STAGE_MATURE, STAGE_TAIL):
            stage, stage_src = prod.manual_stage, "店主标记"
        elif (listed_days is not None and listed_days <= 30) or total_units < 30:
            stage, stage_src = STAGE_NEW, "自动"
        elif (stock_days is None or stock_days > p["滞销线"]) and avg30 < avg_prev * 0.7:
            stage, stage_src = STAGE_TAIL, "自动"
        else:
            stage, stage_src = STAGE_MATURE, "自动"

        reasons = []
        if len(recs) < 14:
            reasons.append(R_SHORT)
        if prod.cost is None:
            reasons.append(R_NOCOST)
        if avg30 == 0:
            reasons.append(R_NOSALE)
        ok = not reasons

        near = high = low = over = False
        if ok:
            near = prod.price < be * p["贴近成本系数"]
            high = stock_days > p["滞销线"] and margin > p["疑似偏高毛利率线"]
            low = stock_days < p["疑似偏低库存天数线"] and margin < p["疑似偏低毛利率线"]
            over = stock_days > p["滞销线"]

        acts = activity_days(recs)
        for d in sorted(acts):
            prior = [recs[x].units for x in days_between(d - timedelta(days=30), d - timedelta(days=1)) if x in recs]
            acts_rows.append({"商品编号": prod.sku, "类目": prod.category, "日期": d, "成交件数": recs[d].units,
                              "此前 30 天中位数": statistics.median(prior)})
        evs = price_events(recs, since90, p["改价事件最小幅度"])
        for d in evs:
            events.append({"商品编号": prod.sku, "类目": prod.category, **event_elasticity(recs, d, acts)})

        cp = sorted(comps.get(prod.sku, []))
        skus.append({
            "商品编号": prod.sku, "商品名称": prod.name, "类目": prod.category,
            "上架天数": listed_days, "当前价": prod.price, "成本价": prod.cost,
            "单件广告成本": cents(unit_ad), "广告口径": ad_note,
            "保本价": be, "不含广告保本价": be_noad, "毛利率": margin,
            "近30天日均": avg30, "前30天日均": avg_prev, "库存": prod.stock,
            "库存天数": stock_days, "生命周期": stage, "生命周期来源": stage_src,
            "数据状态": "正常" if ok else "数据不足", "数据不足原因": "、".join(reasons),
            "贴近成本": near, "疑似偏高": high, "疑似偏低": low, "库存超滞销线": over,
            "记录天数": len(recs), "累计成交": total_units,
            "改价事件数": len(evs), "活动日数": len(acts),
            "有访客数": any(r.visitors for r in recs.values()),
            "近30天有广告花费": spend30 > 0,
            "竞品数": len(cp), "竞品中位价": statistics.median(cp) if cp else None,
        })

    calib = calibrate(events, p)
    for s in skus:
        c = calib["by_cat"].get(s["类目"])
        s["采用弹性"], s["弹性来源"] = (c["采用"], c["来源"]) if c else (prior_for(s["类目"], p), "用先验")
        s["置信度"] = confidence(s)

    summary = summarize(skus, events, ds, p, today, first_day)
    return {"today": today, "first_day": first_day, "skus": skus, "events": events,
            "activity": acts_rows, "calibration": calib, "summary": summary,
            "counter": counter_examples(skus, by_sku, p), "baseline": baseline(skus, by_sku, ds, p)}


def prior_for(cat: str, p: dict) -> float:
    return p["类目先验"].get(cat, p["全类目先验"])


def calibrate(events: list[dict], p: dict) -> dict:
    """按类目取事件弹性中位数；事件少于 3 个的类目用先验。

    校准误差（C-05）= 事件弹性对中位数的中位绝对偏差 ÷ |中位数|。
    """
    by_cat = defaultdict(list)
    for e in events:
        if e["弹性"] is not None and not e["备注"]:
            by_cat[e["类目"]].append(e["弹性"])
    out = {}
    cats = sorted({e["类目"] for e in events})
    for cat in cats:
        vals = by_cat.get(cat, [])
        prior = prior_for(cat, p)
        med = statistics.median(vals) if vals else None
        err = (statistics.median([abs(v - med) for v in vals]) / abs(med)) if vals and med else None
        use_cal = len(vals) >= 3
        out[cat] = {"类目": cat, "有效事件数": len(vals), "事件中位数": med, "先验": prior,
                    "差异": (med - prior) if med is not None else None, "校准误差": err,
                    "采用": med if use_cal else prior, "来源": "门 0 校准" if use_cal else "用先验"}
    return {"by_cat": out}


def confidence(s: dict) -> str:
    """PRD 9.1：高 = ≥ 2 次改价事件且校准误差 ≤ 20%；中 = 记录 ≥ 30 天；低 = 其余。
    门 0 时曝光倍数一律未计，按 10.3 置信度不得为高。"""
    if s["数据状态"] != "正常":
        return "—"
    if s["记录天数"] >= 30:
        return "中"
    return "低"


def share(n: int, d: int) -> float | None:
    return n / d if d else None


def summarize(skus, events, ds: Dataset, p, today, first_day) -> dict:
    ok = [s for s in skus if s["数据状态"] == "正常"]
    den = len(ok)  # C-02：分母 = 非数据不足的 SKU
    near = [s["商品编号"] for s in ok if s["贴近成本"]]
    high = [s["商品编号"] for s in ok if s["疑似偏高"]]
    low = [s["商品编号"] for s in ok if s["疑似偏低"]]
    need = [s["商品编号"] for s in ok if s["贴近成本"] or s["疑似偏高"] or s["疑似偏低"]]
    over = [s["商品编号"] for s in ok if s["库存超滞销线"]]
    ev = [s["商品编号"] for s in ok if s["改价事件数"] > 0]
    vis = [s["商品编号"] for s in skus if s["有访客数"]]
    ad = [s["商品编号"] for s in ok if s["近30天有广告花费"]]
    nosale = [s["商品编号"] for s in skus if R_NOSALE in s["数据不足原因"]]
    insufficient = [s["商品编号"] for s in skus if s["数据状态"] != "正常"]
    with_cost = [s for s in skus if s["成本价"] is not None]
    need_share, over_share = share(len(need), den), share(len(over), den)
    h1 = (need_share is not None and need_share >= p["需要动价占比判据"]
          and over_share is not None and over_share >= p["库存超滞销线占比判据"])
    return {
        "今天": today, "首日": first_day, "覆盖天数": (today - first_day).days + 1,
        "SKU 总数": len(skus), "有成本价 SKU": len(with_cost), "分母": den,
        "数据不足": insufficient, "无动销": nosale,
        "贴近成本": near, "疑似偏高": high, "疑似偏低": low, "需要动价": need,
        "需要动价占比": need_share, "三项相加": len(near) + len(high) + len(low),
        "库存超滞销线": over, "库存超滞销线占比": over_share,
        "有改价事件": ev, "改价事件总数": len(events),
        "有访客数": vis, "近30天有广告花费": ad,
        "H1": h1,
    }


def counter_examples(skus, by_sku, p) -> list[dict]:
    """PRD 12.4 反例六类：从数据里挑候选，没有天然样本的写明怎么改造。"""
    ok = [s for s in skus if s["数据状态"] == "正常"]
    out = []

    def pick(cands, key=None):
        if not cands:
            return None
        return sorted(cands, key=key)[0] if key else cands[0]

    c = pick([s for s in ok if s["库存超滞销线"]], key=lambda s: -(s["库存天数"] or 0))
    if c is None:
        c = pick(ok, key=lambda s: -(s["库存天数"] or 0))
        how = f"把库存改到 {p['滞销线'] * 2:.0f} 天以上的量，并把类目弹性微调 −50%（弹性减半，如 −1.34 → −0.67）"
    else:
        how = "类目弹性微调 −50%（弹性减半，如 −1.34 → −0.67），弹性低"
    out.append(_ce("弹性低库存高", c, how, "不给低于保本价 ×（1 + 最低毛利率）的价；清不完时出「保本价也无法在目标天数内清完」"))

    c = pick([s for s in ok if s["贴近成本"] and s["竞品数"] >= 2 and s["竞品中位价"] < s["当前价"]])
    how = "原样使用"
    if c is None:
        c = pick([s for s in ok if s["贴近成本"]]) or pick(ok, key=lambda s: s["毛利率"])
        how = "补 2 个竞品价，都低于当前价 10% 以上" if c else ""
    out.append(_ce("竞品低毛利薄", c, how, "贴近成本优先于偏高；竞品裁剪不能把区间压到保本价 ×（1 + 最低毛利率）以下"))

    c = pick([s for s in ok if s["生命周期"] == STAGE_NEW], key=lambda s: s["记录天数"])
    how = "原样使用"
    if c is None:
        c = pick(ok, key=lambda s: s["记录天数"])
        how = "上架日期改为 20 天前，只保留最近 20 天记录" if c else ""
    out.append(_ce("新品无历史", c, how, "生命周期为新品期，置信度低"))

    c = pick([s for s in skus if R_NOSALE in s["数据不足原因"]])
    how = "原样使用"
    if c is None:
        c = pick(ok, key=lambda s: s["近30天日均"])
        how = "把近 30 天成交件数全部改为 0" if c else ""
    out.append(_ce("无动销", c, how, "库存天数记「无动销」，数据不足，不出区间"))

    c = pick([s for s in ok if s["活动日数"] > 0])
    how = "原样使用"
    if c is None:
        c = pick([s for s in ok if s["改价事件数"] > 0]) or pick(ok, key=lambda s: -s["近30天日均"])
        how = "在倒数第 15 天插入一天成交 = 近 30 天中位数 × 10" if c else ""
    out.append(_ce("活动日异常", c, how, "活动日被识别出来，不参与弹性校准"))

    c = pick([s for s in skus if R_NOCOST in s["数据不足原因"]])
    how = "原样使用"
    if c is None:
        c = pick(ok)
        how = "把成本价清空" if c else ""
    out.append(_ce("成本缺失", c, how, "不出保本价与区间，操作为「补录成本」"))
    return out


def _ce(kind, s, how, expect):
    return {"类别": kind, "商品编号": s["商品编号"] if s else "无可用 SKU", "类目": s["类目"] if s else "",
            "改造": how, "正确表现": expect}


def window_margin(recs: dict[date, DayRec], days: list[date], cost: float, p: dict, acts: set[date]):
    use = [recs[d] for d in days if d in recs and d not in acts]
    amount = sum(r.amount for r in use)
    if len(use) < 10 or amount <= 0:
        return None
    gm = sum(r.amount * (1 - p["扣点率"] - loss_rate(p)) - r.units * (cost + p["单件履约费"]) - (r.ad_spend or 0)
             for r in use)
    return gm / amount


def baseline(skus, by_sku, ds: Dataset, p) -> dict:
    """PRD 第 3 章「采纳后毛利不掉」的门 0 基线（C-06）。

    每个正常 SKU 配对照：同类目、非本 SKU、库存天数最接近。在历史上每隔 7 天切一刀，
    算「后 14 天毛利率 − 前 14 天毛利率」，SKU 与对照相减得到不采纳时的自然差值；
    全部差值的 P10～P90 就是自然波动区间。窗口剔除各自的活动日，有效天数少于 10 天的窗口不算。
    """
    ok = [s for s in skus if s["数据状态"] == "正常"]
    cost = {s["商品编号"]: s["成本价"] for s in skus}
    pairs = []
    for s in ok:
        cands = [c for c in ok if c["类目"] == s["类目"] and c["商品编号"] != s["商品编号"]]
        if cands:
            c = min(cands, key=lambda c: abs((c["库存天数"] or 0) - (s["库存天数"] or 0)))
            pairs.append((s["商品编号"], c["商品编号"]))
    acts = {k: activity_days(v) for k, v in by_sku.items()}
    all_days = sorted({r.day for r in ds.recs})
    first, last = all_days[0], all_days[-1]
    diffs, rows = [], []
    t = first + timedelta(days=14)
    while t + timedelta(days=13) <= last:
        pre = days_between(t - timedelta(days=14), t - timedelta(days=1))
        post = days_between(t, t + timedelta(days=13))
        for a, b in pairs:
            ra, rb = by_sku.get(a, {}), by_sku.get(b, {})
            ma0 = window_margin(ra, pre, cost[a], p, acts.get(a, set()))
            ma1 = window_margin(ra, post, cost[a], p, acts.get(a, set()))
            mb0 = window_margin(rb, pre, cost[b], p, acts.get(b, set()))
            mb1 = window_margin(rb, post, cost[b], p, acts.get(b, set()))
            if None in (ma0, ma1, mb0, mb1):
                continue
            dlt = (ma1 - ma0) - (mb1 - mb0)
            diffs.append(dlt)
            rows.append({"切点": t, "商品编号": a, "对照": b, "本品变化": ma1 - ma0, "对照变化": mb1 - mb0, "差值": dlt})
        t += timedelta(days=7)
    stat = {"配对数": len(pairs), "样本数": len(diffs)}
    if len(diffs) >= 2:
        q = statistics.quantiles(diffs, n=10, method="inclusive")
        stat.update({"P10": q[0], "中位数": statistics.median(diffs), "P90": q[-1], "最小": min(diffs), "最大": max(diffs)})
    return {"pairs": pairs, "rows": rows, "stat": stat}
