"""把体检结果写成 Excel，另出答案表 CSV 与反例集三张表。表头用原型主色调藏青 #27478C。"""
from __future__ import annotations

import csv
import statistics
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .calc import loss_rate
from .io import Comp, Dataset, sensitive_columns

NAVY, PALE, CREAM, MAUVE = "27478C", "ABC2EC", "EEE5D2", "E4D9E5"
HEAD_FILL = PatternFill("solid", fgColor=NAVY)
HEAD_FONT = Font(bold=True, color="FFFFFF")
PCT, MONEY, NUM = "0.0%", "0.00", "0.00"


def _sheet(wb, title, header, rows, widths=None, fmts=None):
    ws = wb.create_sheet(title)
    ws.append(header)
    for c in ws[1]:
        c.fill, c.font = HEAD_FILL, HEAD_FONT
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for r in rows:
        ws.append([_cell(v) for v in r])
    for i, h in enumerate(header, start=1):
        ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(h, max(10, min(40, len(str(h)) * 2 + 4)))
        if fmts and h in fmts:
            for row in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                row[0].number_format = fmts[h]
    ws.freeze_panes = "A2"
    return ws


def _cell(v):
    if isinstance(v, bool):
        return "是" if v else ""
    return v


def _names(ids):
    return "、".join(ids) if ids else "无"


def _frac(ids, den):
    return f"{len(ids)} ÷ {den}" if den else "分母为 0"


def write_excel(ds: Dataset, res: dict, path: Path):
    p, sm = ds.params, res["summary"]
    wb = Workbook()
    wb.remove(wb.active)
    den = sm["分母"]

    def ratio(ids):
        return len(ids) / den if den else None

    body = [
        ["数据范围", "销售日记录首日到「今天」", f"≥ {p['覆盖天数下限']} 天", f"{sm['首日']} 到 {sm['今天']}，{sm['覆盖天数']} 天",
         "达标" if sm["覆盖天数"] >= p["覆盖天数下限"] else "不足，如实记录"],
        ["SKU 总数；有成本价的 SKU 数", "计数", f"有成本价 ≥ {p['有成本价SKU下限']} 个", f"{sm['SKU 总数']}；{sm['有成本价 SKU']}",
         "达标" if sm["有成本价 SKU"] >= p["有成本价SKU下限"] else "不足，如实记录"],
        ["数据不足（单列，不进分母）", "记录不足 14 天、缺成本价、无动销，任一即算", "参考", len(sm["数据不足"]), _names(sm["数据不足"])],
        ["其中无动销（C-01 单列）", "近 30 天成交 0 件", "参考", len(sm["无动销"]), _names(sm["无动销"])],
        ["占比分母（C-02）", "非数据不足的 SKU 数", "—", den, ""],
        ["贴近成本 SKU 占比", f"当前价 ＜ 保本价 × {p['贴近成本系数']}", "参考", ratio(sm["贴近成本"]), f"{_frac(sm['贴近成本'], den)}：{_names(sm['贴近成本'])}"],
        ["疑似偏高 SKU 占比", f"库存天数 ＞ {p['滞销线']} 且毛利率 ＞ {p['疑似偏高毛利率线']:.0%}", "参考", ratio(sm["疑似偏高"]), f"{_frac(sm['疑似偏高'], den)}：{_names(sm['疑似偏高'])}"],
        ["疑似偏低 SKU 占比", f"库存天数 ＜ {p['疑似偏低库存天数线']} 且毛利率 ＜ {p['疑似偏低毛利率线']:.0%}", "参考", ratio(sm["疑似偏低"]), f"{_frac(sm['疑似偏低'], den)}：{_names(sm['疑似偏低'])}"],
        ["需要动价 SKU 占比", "上三行去重合计", f"≥ {p['需要动价占比判据']:.0%}", sm["需要动价占比"],
         f"{_frac(sm['需要动价'], den)}；三项相加 {sm['三项相加']}" + ("，有重叠已去重" if sm["三项相加"] != len(sm["需要动价"]) else "")],
        ["库存超 90 天 SKU 占比", "当前库存 ÷ 近 30 天日均销量 ＞ 滞销线", f"≥ {p['库存超滞销线占比判据']:.0%}", sm["库存超滞销线占比"], f"{_frac(sm['库存超滞销线'], den)}：{_names(sm['库存超滞销线'])}"],
        ["近 90 天有改价事件的 SKU 占比", "当日售价与前 7 天每天都不同，前后各 7 天记录齐（C-04）", "决定弹性能否校准", ratio(sm["有改价事件"]),
         f"{_frac(sm['有改价事件'], den)}：{_names(sm['有改价事件'])}；事件共 {sm['改价事件总数']} 个"],
        ["是否有按日访客数", "访客数列非空", "决定曝光项能否估", ("有，" + f"{len(sm['有访客数'])} 个 SKU") if sm["有访客数"] else "否", _names(sm["有访客数"])],
        ["近 30 天有广告花费的 SKU 占比", "近 30 天广告花费 ＞ 0", "决定含广告保本价能否启用", ratio(sm["近30天有广告花费"]), f"{_frac(sm['近30天有广告花费'], den)}：{_names(sm['近30天有广告花费'])}"],
        ["店主一句话", "现在怎么定价、多久改一次、改价前看什么", "记录原话", p.get("店主一句话") or "【待填】", ""],
        ["H1 判定", "需要动价占比 与 库存超 90 天占比 两项都达标", "两项都达标才通过", "通过" if sm["H1"] else "不通过",
         "通过：进入 PT-01" if sm["H1"] else "不通过：项目停，不进 PT-01"],
    ]
    ws = _sheet(wb, "体检", ["指标", "算法", "判据", "结果", "明细"], body,
                widths={"指标": 30, "算法": 44, "判据": 22, "结果": 18, "明细": 60})
    for row in ws.iter_rows(min_row=2):
        if isinstance(row[3].value, float):
            row[3].number_format = PCT
        row[1].alignment = row[4].alignment = Alignment(wrap_text=True, vertical="top")

    cols = ["商品编号", "商品名称", "类目", "上架天数", "当前价", "成本价", "单件广告成本", "广告口径", "保本价", "不含广告保本价",
            "毛利率", "近30天日均", "前30天日均", "库存", "库存天数", "生命周期", "生命周期来源", "数据状态", "数据不足原因",
            "贴近成本", "疑似偏高", "疑似偏低", "库存超滞销线", "记录天数", "改价事件数", "活动日数", "有访客数",
            "近30天有广告花费", "竞品数", "竞品中位价", "采用弹性", "弹性来源", "置信度"]
    rows = []
    for s in res["skus"]:
        r = [s[c] for c in cols]
        r[cols.index("库存天数")] = round(s["库存天数"], 2) if s["库存天数"] is not None else "无动销"
        rows.append(r)
    _sheet(wb, "答案表", cols, rows, widths={"商品名称": 16, "数据不足原因": 20, "广告口径": 22},
           fmts={"毛利率": "0.00%", "保本价": MONEY, "不含广告保本价": MONEY, "单件广告成本": MONEY,
                 "近30天日均": NUM, "前30天日均": NUM, "采用弹性": NUM})

    ev_cols = ["商品编号", "类目", "改价日", "旧价", "新价", "口径", "改价前", "改价后", "弹性", "访客倍数", "剔除活动日", "备注"]
    _sheet(wb, "改价事件", ev_cols + ["是否跨价格力档位"],
           [[e[c] for c in ev_cols] + ["待店主截图确认"] for e in res["events"]],
           widths={"备注": 30, "是否跨价格力档位": 18}, fmts={"弹性": NUM, "访客倍数": NUM, "改价前": "0.0000", "改价后": "0.0000"})

    cal_cols = ["类目", "有效事件数", "事件中位数", "先验", "差异", "校准误差", "采用", "来源"]
    cal_rows = [[c[k] for k in cal_cols] for c in res["calibration"]["by_cat"].values()]
    ws = _sheet(wb, "类目校准", cal_cols, cal_rows, fmts={"事件中位数": NUM, "先验": NUM, "差异": NUM, "校准误差": "0.0%", "采用": NUM})
    ws.append([])
    ws.append(["没有事件的类目", "一律用先验：" + _priors(p)])
    vis_ev = [e for e in res["events"] if e["访客倍数"] is not None]
    ws.append(["曝光倍数", "无法估，默认 1" if not vis_ev else
               f"{len(vis_ev)} 个事件有访客倍数，是否跨价格力档位待店主截图确认；确认前默认 1"])

    _sheet(wb, "活动日", ["商品编号", "类目", "日期", "成交件数", "此前 30 天中位数"],
           [[a[k] for k in ["商品编号", "类目", "日期", "成交件数", "此前 30 天中位数"]] for a in res["activity"]])

    _sheet(wb, "反例集", ["类别", "商品编号", "类目", "改造", "正确表现"],
           [[c[k] for k in ["类别", "商品编号", "类目", "改造", "正确表现"]] for c in res["counter"]],
           widths={"改造": 40, "正确表现": 60})

    b = res["baseline"]
    st = b["stat"]
    ws = _sheet(wb, "对照基线", ["切点", "商品编号", "对照", "本品变化", "对照变化", "差值"],
                [[r[k] for k in ["切点", "商品编号", "对照", "本品变化", "对照变化", "差值"]] for r in b["rows"]],
                fmts={"本品变化": "0.00%", "对照变化": "0.00%", "差值": "0.00%"})
    ws.insert_rows(1, 6)
    ws.freeze_panes = "A8"
    ws["A1"] = "自然波动区间（不采纳时 SKU 与对照的 14 天毛利率变化之差）"
    ws["A1"].font = Font(bold=True, color=NAVY)
    ws["A2"], ws["B2"] = "配对数；样本数", f"{st['配对数']}；{st['样本数']}"
    if "P10" in st:
        ws["A3"], ws["B3"] = "P10 ～ P90", f"{st['P10']:+.2%} ～ {st['P90']:+.2%}"
        ws["A4"], ws["B4"] = "中位数；最小；最大", f"{st['中位数']:+.2%}；{st['最小']:+.2%}；{st['最大']:+.2%}"
    else:
        ws["A3"], ws["B3"] = "P10 ～ P90", "样本不足，门 1 只看方向"
    ws["A5"] = "算法：C-06，见「参数与口径」"

    cov = [
        ["销售日记录行数", len(ds.recs)],
        ["商品表 SKU 数", len(ds.products)],
        ["有销售记录的 SKU 数", len({r.sku for r in ds.recs})],
        ["竞品价格行数", len(ds.comps)],
        ["表头疑似含身份信息的列", _names(sensitive_columns(ds.headers))],
    ]
    ws = _sheet(wb, "数据覆盖与校验", ["项", "值"], cov, widths={"项": 28, "值": 60})
    ws.append([])
    ws.append(["校验问题：表", "行号", "原因"])
    for i in ds.issues:
        ws.append([i["表"], i["行号"], i["原因"]])

    prm = [[k, _priors(p) if k == "类目先验" else str(v)] for k, v in p.items() if not k.startswith("_")]
    prm += [
        ["保本价", "（成本价 + 单件履约费 + 单件广告成本）÷（1 − 扣点率 − 退货率 × 单次退货净损失占比），四舍五入到分"],
        ["保本价分母", f"{1 - p['扣点率'] - loss_rate(p):.4f}"],
        ["单件广告成本", "近 30 天广告花费 ÷ 广告成交件数；无数据或广告成交为 0 时取 0"],
        ["毛利率", "（当前价 − 四舍五入后的保本价）÷ 当前价"],
        ["日均与库存天数", "近 30 天成交件数 ÷ 30（含 0 销量日与无记录日）；库存 ÷ 日均"],
        ["生命周期", "店主标记优先；上架 ≤ 30 天或累计成交 ＜ 30 件为新品期；库存天数 ＞ 滞销线且近 30 天日均 ＜ 前 30 天 × 70% 为尾货期；其余成熟期"],
        ["置信度", "门 0 时曝光倍数未计，按 PRD 10.3 不得为高：记录 ≥ 30 天为中，其余为低"],
        ["C-01", "无动销的 SKU 在体检里单列计数，不进占比分母（研发步骤第 3 节）"],
        ["C-02", "体检各项占比的分母 = 非数据不足的 SKU 数（研发步骤第 3 节）"],
        ["C-03", "活动日：此前 30 天有记录的日子少于 7 天或中位数为 0 时不判"],
        ["C-04", "改价事件：当日售价与前 7 天每一天的差都 ≥ 1%（改价事件最小幅度），一次调价只记一个事件；几分钱的日常波动不算"],
        ["C-05", "校准误差 = 类目内事件弹性对中位数的中位绝对偏差 ÷ |中位数|"],
        ["C-06", "对照基线：同类目库存天数最接近的 SKU 作对照，每 7 天切一刀比前后 14 天毛利率变化之差，取 P10～P90；窗口剔除活动日，有效天数 ＜ 10 不算"],
    ]
    _sheet(wb, "参数与口径", ["项", "值或算法"], prm, widths={"项": 22, "值或算法": 100})
    wb.save(path)


def _priors(p):
    return "、".join(f"{k} {v}" for k, v in p["类目先验"].items()) + f"，其余 {p['全类目先验']}"


def write_answers_csv(res: dict, path: Path):
    cols = ["商品编号", "保本价", "不含广告保本价", "单件广告成本", "毛利率", "近30天日均", "库存天数", "生命周期", "数据状态", "数据不足原因"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for s in res["skus"]:
            r = dict(s)
            r["库存天数"] = f"{s['库存天数']:.2f}" if s["库存天数"] is not None else "无动销"
            r["毛利率"] = f"{s['毛利率']:.2%}" if s["毛利率"] is not None else ""
            r["近30天日均"] = f"{s['近30天日均']:.2f}"
            w.writerow([r[c] if r[c] is not None else "" for c in cols])


def write_counter_set(ds: Dataset, res: dict, out: Path):
    """反例集三张表：每类复制一个 SKU，按「改造」列改数据，新编号 CE<序号>_<原编号>。"""
    out.mkdir(parents=True, exist_ok=True)
    today = res["today"]
    prods = {p.sku: p for p in ds.products}
    recs = {}
    for r in ds.recs:
        recs.setdefault(r.sku, []).append(r)
    skus = {s["商品编号"]: s for s in res["skus"]}
    P, R, C = [], [], []
    for i, ce in enumerate(res["counter"], start=1):
        src = ce["商品编号"]
        if src not in prods:
            continue
        new = f"CE{i}_{src}"
        prod = replace(prods[src], sku=new)
        rows = [replace(r, sku=new) for r in sorted(recs.get(src, []), key=lambda r: r.day)]
        comps = [replace(c, sku=new) for c in ds.comps if c.sku == src]
        how, s = ce["改造"], skus[src]
        if how.startswith("把库存改到"):
            prod = replace(prod, stock=round(max(s["近30天日均"], 1) * ds.params["滞销线"] * 2))
        elif how.startswith("补 2 个竞品价"):
            comps = [Comp(new, 1, round(prod.price * 0.88, 2), today), Comp(new, 2, round(prod.price * 0.85, 2), today)]
        elif how.startswith("上架日期改为"):
            prod = replace(prod, listed=today - timedelta(days=20))
            rows = [r for r in rows if r.day > today - timedelta(days=20)]
        elif how.startswith("把近 30 天成交件数全部改为 0"):
            rows = [replace(r, units=0, amount=0) if r.day > today - timedelta(days=30) else r for r in rows]
        elif how.startswith("在倒数第 15 天插入"):
            med = statistics.median([r.units for r in rows[-30:]]) or 1
            target = rows[-15].day
            rows = [replace(r, units=med * 10, amount=r.price * med * 10) if r.day == target else r for r in rows]
        elif how.startswith("把成本价清空"):
            prod = replace(prod, cost=None)
        P.append(prod)
        R.extend(rows)
        C.extend(comps)
    with open(out / "商品表.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["商品编号", "商品名称", "类目", "上架日期", "当前售价", "成本价", "库存数量", "生命周期标记"])
        for p in P:
            w.writerow([p.sku, p.name, p.category, p.listed or "", p.price, "" if p.cost is None else p.cost, p.stock, p.manual_stage or ""])
    with open(out / "销售日记录.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["商品编号", "日期", "当日售价", "成交件数", "成交金额", "退款件数", "访客数", "广告花费", "广告成交件数"])
        for r in R:
            w.writerow([r.sku, r.day, r.price, r.units, f"{r.amount:.2f}"] + ["" if v is None else v for v in (r.refunds, r.visitors, r.ad_spend, r.ad_units)])
    with open(out / "竞品价格.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["商品编号", "竞品序号", "竞品售价", "录入日期"])
        for c in C:
            w.writerow([c.sku, c.idx, c.price, c.day or ""])
    with open(out / "反例说明.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["新编号", "类别", "来源 SKU", "改造", "正确表现"])
        for i, ce in enumerate(res["counter"], start=1):
            w.writerow([f"CE{i}_{ce['商品编号']}", ce["类别"], ce["商品编号"], ce["改造"], ce["正确表现"]])
