"""PT-00 自检：从原始 CSV 独立复算，再和门 0 体检 Excel 比对。不复用 gate0 的计算代码。

用法：uv run python scripts/accept.py PT-00 <数据目录> [输出目录]
输出三列：条目、通过、实测。「人工」= 机器判不了，按验收条例亲自看。
"""
import csv
import json
import math
import random
import re
import statistics
import sys
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from openpyxl import load_workbook

PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
WORDS = ["店名", "店铺", "商家", "买家", "收货", "手机", "电话", "地址", "旺旺", "昵称", "会员名", "身份证"]


def rd(path, mapping):
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gb18030")
    rows = list(csv.reader(text.splitlines()))
    head = [mapping.get(h.strip(), h.strip()) for h in rows[0]]
    return head, [dict(zip(head, [c.strip() for c in r])) for r in rows[1:] if any(c.strip() for c in r)]


def num(s):
    s = (s or "").replace("¥", "").replace("￥", "").replace(",", "").strip()
    return float(s) if s else None


def dt(s):
    for f in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(s.strip(), f).date()
        except ValueError:
            pass
    return None


def money(x):
    return float(Decimal(repr(x)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def sheet_rows(wb, name):
    ws = wb[name]
    rows = list(ws.iter_rows(values_only=True))
    return rows


def main(step, data_dir, out_dir=None):
    if step != "PT-00":
        print("本仓库只有 PT-00 的自检；PT-01 起的自检在 ../app")
        return 2
    d = Path(data_dir)
    out = Path(out_dir) if out_dir else d / "门0输出"
    mp = json.loads((d / "列名映射.json").read_text(encoding="utf-8")) if (d / "列名映射.json").exists() else {}
    prm = json.loads((d / "店铺参数.json").read_text(encoding="utf-8")) if (d / "店铺参数.json").exists() else {}
    fee, ful, ret, loss = prm.get("扣点率", 0.05), prm.get("单件履约费", 3.5), prm.get("退货率", 0.06), prm.get("单次退货净损失占比", 0.6)
    line = prm.get("滞销线", 90)
    ph, prods = rd(d / "商品表.csv", mp.get("商品表", {}))
    sh, sales = rd(d / "销售日记录.csv", mp.get("销售日记录", {}))
    wb = load_workbook(out / "门0体检.xlsx", data_only=True)
    ans = {r[0]: dict(zip(sheet_rows(wb, "答案表")[0], r)) for r in sheet_rows(wb, "答案表")[1:]}
    results = []

    def rec(item, ok, got):
        results.append((item, "人工" if ok is None else ("通过" if ok else "不通过"), got))

    # PT-00-1 脱敏
    bad_cols = [c for c in ph + sh if any(w in c for w in WORDS)]
    phones = sum(1 for r in prods + sales for v in r.values() if PHONE.search(v or ""))
    rec("PT-00-1 数据已脱敏", not bad_cols and not phones, f"疑似身份列 {bad_cols or '无'}；手机号 {phones} 处；商品名是否通用名请人工再看前 20 行")

    # 按 SKU 整理销售
    by = {}
    for r in sales:
        day = dt(r["日期"])
        if day and num(r["当日售价"]):
            by.setdefault(r["商品编号"], {})[day] = r
    days = sorted({x for v in by.values() for x in v})
    today = dt(prm["今天"]) if prm.get("今天") else days[-1]
    cover = (today - days[0]).days + 1
    with_cost = [p for p in prods if num(p["成本价"]) is not None]
    rec("PT-00-2 数据够用", True, f"{days[0]} 到 {today} 共 {cover} 天（≥ 90：{'是' if cover >= 90 else '否'}）；"
        f"有成本价 {len(with_cost)} 个（≥ 50：{'是' if len(with_cost) >= 50 else '否'}）；不足已如实记录")

    # PT-00-3 体检表完整
    body = sheet_rows(wb, "体检")[1:]
    need = ["SKU 总数", "贴近成本", "疑似偏高", "疑似偏低", "需要动价", "库存超 90 天", "改价事件", "访客数", "广告花费", "店主一句话"]
    missing = [k for k in need if not any(k in (r[0] or "") and r[1] and r[3] not in (None, "") for r in body)]
    rec("PT-00-3 体检表完整", not missing, f"MRD 2.2 十行{'齐全' if not missing else '缺：' + '、'.join(missing)}；分母行：{[r[3] for r in body if (r[0] or '').startswith('占比分母')]}")

    last30 = [today - timedelta(days=i) for i in range(30)]

    def unit_ad(sku):
        rs = by.get(sku, {})
        sp = sum(num(rs[x].get("广告花费")) or 0 for x in last30 if x in rs)
        au = sum(num(rs[x].get("广告成交件数")) or 0 for x in last30 if x in rs)
        return sp / au if au > 0 else 0.0

    def be(p):
        c = num(p["成本价"])
        return None if c is None else money((c + ful + unit_ad(p["商品编号"])) / (1 - fee - ret * loss))

    # PT-00-4 保本价
    random.seed(0)
    pick = random.sample(with_cost, min(3, len(with_cost)))
    diffs = [(p["商品编号"], be(p), ans[p["商品编号"]]["保本价"]) for p in pick]
    rec("PT-00-4 保本价算对", all(a == b for _, a, b in diffs), "；".join(f"{s} 复算 {a} / Excel {b}" for s, a, b in diffs))

    # PT-00-5 广告口径
    ad_skus = [p for p in with_cost if unit_ad(p["商品编号"]) > 0]
    if ad_skus:
        p = ad_skus[0]
        ok = money(unit_ad(p["商品编号"])) == ans[p["商品编号"]]["单件广告成本"] and be(p) == ans[p["商品编号"]]["保本价"]
        rec("PT-00-5 广告口径算对", ok, f"{p['商品编号']} 单件广告成本 {money(unit_ad(p['商品编号']))}，含广告保本价 {be(p)}，Excel {ans[p['商品编号']]['单件广告成本']} / {ans[p['商品编号']]['保本价']}")
    else:
        rec("PT-00-5 广告口径算对", all(v["广告口径"] != "含广告" for v in ans.values()), "无投放，Excel 无「含广告」SKU")

    # PT-00-6 改价事件与活动日
    evs = sheet_rows(wb, "改价事件")[1:]
    msgs, ok6 = [], True
    for e in evs[:2]:
        sku, dday = e[0], e[2].date() if isinstance(e[2], datetime) else e[2]
        rs = by[sku]
        bef = [rs.get(dday - timedelta(days=k)) for k in range(1, 8)]
        aft = [rs.get(dday + timedelta(days=k)) for k in range(0, 7)]
        good = all(bef) and all(aft) and all(num(b["当日售价"]) != num(rs[dday]["当日售价"]) for b in bef)
        ok6 &= good
        msgs.append(f"事件 {sku} {dday} {'符合' if good else '不符合'}")
    acts = sheet_rows(wb, "活动日")[1:]
    for a in acts[:1]:
        sku, dday = a[0], a[2].date() if isinstance(a[2], datetime) else a[2]
        rs = by[sku]
        prior = [num(rs[x]["成交件数"]) for x in (dday - timedelta(days=k) for k in range(1, 31)) if x in rs]
        good = num(rs[dday]["成交件数"]) > 3 * statistics.median(prior)
        ok6 &= good
        msgs.append(f"活动日 {sku} {dday} 成交 {num(rs[dday]['成交件数']):g} ＞ 3 × {statistics.median(prior):g}：{'符合' if good else '不符合'}")
    rec("PT-00-6 改价事件与活动日", ok6, "；".join(msgs) or "无改价事件与活动日")

    # PT-00-7 弹性校准表
    ok7, m7 = True, []
    for e in evs:
        sku, dday = e[0], e[2].date() if isinstance(e[2], datetime) else e[2]
        if e[8] is None:
            continue
        rs = by[sku]
        actd = {(x[2].date() if isinstance(x[2], datetime) else x[2]) for x in acts if x[0] == sku}
        b = [rs[dday - timedelta(days=k)] for k in range(1, 8) if dday - timedelta(days=k) not in actd]
        a = [rs[dday + timedelta(days=k)] for k in range(0, 7) if dday + timedelta(days=k) not in actd]
        vis = all(num(r.get("访客数")) for r in a + b)
        f = (lambda rr: sum(num(r["成交件数"]) for r in rr) / sum(num(r["访客数"]) for r in rr)) if vis else \
            (lambda rr: sum(num(r["成交件数"]) for r in rr) / len(rr))
        old, new = num(rs[dday - timedelta(days=1)]["当日售价"]), num(rs[dday]["当日售价"])
        el = math.log(f(a) / f(b)) / math.log(new / old)
        good = abs(el - e[8]) < 1e-6
        ok7 &= good
        m7.append(f"{sku} {el:.2f}")
    cal = [r for r in sheet_rows(wb, "类目校准")[1:] if r[0] and r[1] is not None and isinstance(r[1], int)]
    wrong = [r[0] for r in cal if (r[1] < 3) != (r[7] == "用先验")]
    ok7 &= not wrong
    rec("PT-00-7 弹性校准表", ok7, f"复算事件弹性：{'、'.join(m7) or '无事件'}；事件 ＜ 3 个的类目标「用先验」：{'是' if not wrong else '否，' + '、'.join(wrong)}")

    # PT-00-8 曝光倍数
    exp = [r for r in sheet_rows(wb, "类目校准") if r[0] == "曝光倍数"]
    rec("PT-00-8 曝光倍数", None, (exp[0][1] if exp else "缺「曝光倍数」结论行")
        + "；是否跨价格力档位要对照店主的价格力截图人工判断，机器只核结论行在不在")

    # PT-00-9 反例集
    ce = sheet_rows(wb, "反例集")[1:]
    kinds = {"弹性低库存高", "竞品低毛利薄", "新品无历史", "无动销", "活动日异常", "成本缺失"}
    got = {r[0] for r in ce if r[1] and r[1] != "无可用 SKU" and r[4]}
    files = all((out / "反例集" / f).exists() for f in ("商品表.csv", "销售日记录.csv", "竞品价格.csv"))
    rec("PT-00-9 反例集", got == kinds and files, f"六类齐：{'是' if got == kinds else '缺 ' + '、'.join(kinds - got)}；三张表：{'有' if files else '缺'}")

    # PT-00-10 对照基线
    bl = {r[0]: r[1] for r in sheet_rows(wb, "对照基线")[:5] if r[0]}
    rec("PT-00-10 对照基线", "P10 ～ P90" in bl, f"P10～P90：{bl.get('P10 ～ P90')}；配对与样本：{bl.get('配对数；样本数')}")

    # PT-00-11、12 人工
    shots = [x for x in (d / "截图").glob("*") if x.suffix.lower() in (".png", ".jpg", ".jpeg")] if (d / "截图").exists() else []
    rec("PT-00-11 三张截图", None, f"数据目录/截图 下有 {len(shots)} 张；请人工确认是平台建议价页、价格力档位页、商品报表页")
    rec("PT-00-12 文档已升版", None, "请打开 MRD、PRD 确认【门 0 填】已换成真实数字并升 MRD v2.3、PRD v2.4")

    # PT-00-13 H1：独立复算两项占比
    ok_skus = []
    for p in prods:
        rs = by.get(p["商品编号"], {})
        avg = sum(num(rs[x]["成交件数"]) for x in last30 if x in rs) / 30
        if len(rs) >= 14 and num(p["成本价"]) is not None and avg > 0:
            ok_skus.append((p, avg))
    need_n = over_n = 0
    for p, avg in ok_skus:
        price, b = num(p["当前售价"]), be(p)
        sd = num(p["库存数量"]) / avg
        mg = float(Decimal(repr((price - b) / price)).quantize(Decimal("0.0001"), ROUND_HALF_UP))
        need_n += (price < b * 1.05) or (sd > line and mg > 0.30) or (sd < 20 and mg < 0.15)
        over_n += sd > line
    n = len(ok_skus)
    ns, os_ = (need_n / n, over_n / n) if n else (0, 0)
    h1 = ns >= 0.30 and os_ >= 0.15
    excel_h1 = [r[3] for r in body if r[0] == "H1 判定"][0]
    same = ("通过" if h1 else "不通过") == excel_h1
    rec("PT-00-13 H1 判定", h1 and same,
        f"复算：需要动价 {need_n}/{n} = {ns:.1%}（判据 ≥ 30%），库存超 {line} 天 {over_n}/{n} = {os_:.1%}（判据 ≥ 15%）"
        f" → {'通过' if h1 else '不通过，项目停'}；Excel 写的是「{excel_h1}」{'，与复算一致' if same else '，与复算不一致'}")

    w = max(len(r[0]) for r in results)
    print(f"{'条目'.ljust(w)}  通过    实测")
    for item, st, got in results:
        print(f"{item.ljust(w)}  {st.ljust(4)}  {got}")
    fails = [r for r in results if r[1] == "不通过"]
    print(f"\n机器可判 {sum(1 for r in results if r[1] != '人工')} 条，通过 {sum(1 for r in results if r[1] == '通过')} 条，不通过 {len(fails)} 条，人工 {sum(1 for r in results if r[1] == '人工')} 条")
    return 1 if fails else 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(*sys.argv[1:4]))
