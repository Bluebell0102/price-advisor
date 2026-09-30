"""生成验收样例集（《用户验收条例 v1.0》附录 A）与全部变体文件（附录 A.5）。

用法：uv run python scripts/make_samples.py
输出：samples/样例店/（三张表 + 答案表）、samples/变体/（坏文件、列名不同、无广告列、S09 活动日、规模样例）。
全部虚构，不含任何真实商家名称。
"""
import csv
import random
import shutil
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "samples"
TODAY = date(2026, 9, 29)  # 第 60 天
DAY1 = TODAY - timedelta(days=59)

P_COLS = ["商品编号", "商品名称", "类目", "上架日期", "当前售价", "成本价", "库存数量", "生命周期标记"]
S_COLS = ["商品编号", "日期", "当日售价", "成交件数", "成交金额", "退款件数", "访客数", "广告花费", "广告成交件数"]
C_COLS = ["商品编号", "竞品序号", "竞品售价", "录入日期"]

# 编号, 名称, 类目, 上架天数, 成本价, 当前售价, 库存
PRODUCTS = [
    ("S01", "大号收纳盒", "收纳", 210, 20.0, 39.9, 780),
    ("S02", "厨房沥水篮", "厨房", 300, 20.0, 25.9, 120),
    ("S03", "免打孔挂钩", "五金", 150, 3.0, 9.9, 100),
    ("S04", "加厚垃圾袋", "清洁", 400, 24.0, 36.9, 150),
    ("S05", "硅胶折叠水杯", "水具", 5, 15.0, 29.9, 50),
    ("S06", "防滑浴室垫", "卫浴", 365, 20.0, 45.0, 200),
    ("S07", "收纳袋", "收纳", 180, None, 19.9, 90),
    ("S08", "保鲜盒", "厨房", 100, 10.0, 19.9, 80),
    ("S09", "衣架", "家居", 250, 8.0, 17.9, 60),
    ("S10", "抽纸", "清洁", 200, 20.0, 45.0, 160),
    ("S11", "纸巾盒", "清洁", 200, 20.0, 45.0, 160),
    ("S12", "新款收纳筐", "收纳", 20, 12.0, 25.9, 66),
]
COMPETITORS = [("S10", 1, 37.5), ("S10", 2, 35.9), ("S10", 3, 42.0), ("S11", 1, 30.0)]

# 附录 A.3：编号, 保本价, 不含广告保本价, 单件广告成本, 毛利率, 近 30 天日均, 库存天数, 生命周期, 数据状态, 数据不足原因
ANSWERS = [
    ("S01", "25.71", "25.71", "0.00", "35.56%", "6.00", "130.00", "尾货期", "正常", ""),
    ("S02", "25.71", "25.71", "0.00", "0.73%", "5.00", "24.00", "成熟期", "正常", ""),
    ("S03", "7.11", "7.11", "0.00", "28.18%", "6.00", "16.67", "成熟期", "正常", ""),
    ("S04", "33.37", "30.09", "3.00", "9.57%", "10.00", "15.00", "成熟期", "正常", ""),
    ("S05", "20.24", "20.24", "0.00", "32.31%", "0.17", "300.00", "新品期", "数据不足", "记录不足 14 天"),
    ("S06", "25.71", "25.71", "0.00", "42.87%", "0.00", "无动销", "尾货期", "数据不足", "无动销"),
    ("S07", "", "", "0.00", "", "3.00", "30.00", "成熟期", "数据不足", "缺成本价"),
    ("S08", "14.77", "14.77", "0.00", "25.78%", "5.20", "15.38", "成熟期", "正常", ""),
    ("S09", "12.58", "12.58", "0.00", "29.72%", "5.70", "10.53", "成熟期", "正常", ""),
    ("S10", "25.71", "25.71", "0.00", "42.87%", "5.00", "32.00", "成熟期", "正常", ""),
    ("S11", "25.71", "25.71", "0.00", "42.87%", "5.00", "32.00", "成熟期", "正常", ""),
    ("S12", "16.96", "16.96", "0.00", "34.52%", "2.00", "33.00", "新品期", "正常", ""),
]


def day(n: int) -> date:
    return DAY1 + timedelta(days=n - 1)


def g(x):
    return "" if x is None else f"{x:g}"


def sales_rows(s09_spike=False):
    """(编号, 第几天, 当日售价, 成交件数, 访客数, 广告花费, 广告成交件数)"""
    for n in range(1, 61):
        yield "S01", n, 39.9, 9 if n <= 30 else 6, None, None, None
        yield "S02", n, 25.9, 5, None, None, None
        yield "S03", n, 9.9, 6, None, None, None
        yield "S04", n, 36.9, 10, None, (3.0 if n >= 31 else None), (1 if n >= 31 else None)
        if n >= 56:
            yield "S05", n, 29.9, 1, None, None, None
        yield "S06", n, 45.0, 2 if n <= 30 else 0, None, None, None
        yield "S07", n, 19.9, 3, None, None, None
        yield "S08", n, 19.9, 40 if n == 45 else 4, None, None, None
        units09 = 60 if (s09_spike and n == 42) else (5 if n <= 39 else 6)
        yield "S09", n, 19.9 if n <= 39 else 17.9, units09, 100, None, None
        yield "S10", n, 45.0, 5, None, None, None
        yield "S11", n, 45.0, 5, None, None, None
        if n >= 41:
            yield "S12", n, 25.9, 3, None, None, None


def write_csv(path: Path, head, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(head)
        w.writerows(rows)


def product_rows():
    return [[sku, name, cat, (TODAY - timedelta(days=listed)).isoformat(), g(price), g(cost), stock, ""]
            for sku, name, cat, listed, cost, price, stock in PRODUCTS]


def sale_row(sku, n, price, units, vis, spend, adu):
    return [sku, day(n).isoformat(), g(price), units, f"{price * units:.2f}", "", g(vis), g(spend), g(adu)]


def main():
    base, var = OUT / "样例店", OUT / "变体"
    for d in (base, var):  # 只清自己生成的两个目录；samples/反例集 来自门 0 脚本，不动
        if d.exists():
            shutil.rmtree(d)
    sales = [sale_row(*r) for r in sales_rows()]
    write_csv(base / "商品表.csv", P_COLS, product_rows())
    write_csv(base / "销售日记录.csv", S_COLS, sales)
    write_csv(base / "竞品价格.csv", C_COLS, [[s, i, g(p), TODAY.isoformat()] for s, i, p in COMPETITORS])
    write_csv(base / "答案表.csv", ["商品编号", "保本价", "不含广告保本价", "单件广告成本", "毛利率", "近30天日均",
                                  "库存天数", "生命周期", "数据状态", "数据不足原因"], ANSWERS)

    # A.5 变体
    write_csv(var / "缺列_销售日记录.csv", [c for c in S_COLS if c != "成交件数"],
              [[v for c, v in zip(S_COLS, r) if c != "成交件数"] for r in sales[:24]])
    bad = [r[:] for r in sales[:24]]
    bad[5][1] = "2026/13/01"
    write_csv(var / "日期格式错_销售日记录.csv", S_COLS, bad)
    prows = product_rows()
    write_csv(var / "编号重复_商品表.csv", P_COLS, prows + [prows[2][:]])
    zero = [r[:] for r in sales[:24]]
    zero[7][2] = "0"
    write_csv(var / "售价为0_销售日记录.csv", S_COLS, zero)
    write_csv(var / "非模板.csv", ["姓名", "部门", "入职年份", "备注"], [["甲", "市场部", "2021", "无"], ["乙", "财务部", "2019", "无"]])
    renamed = {"成交件数": "销量", "成交金额": "销售额", "日期": "统计日期", "当日售价": "售价"}
    write_csv(var / "列名不同_销售日记录.csv", [renamed.get(c, c) for c in S_COLS], sales)
    write_csv(var / "无广告列_销售日记录.csv", S_COLS[:7], [r[:7] for r in sales])
    write_csv(var / "S09活动日_销售日记录.csv", S_COLS, [sale_row(*r) for r in sales_rows(s09_spike=True)])

    # 规模样例：150 个 SKU × 90 天，固定种子
    rnd = random.Random(20260929)
    cats = ["收纳", "厨房", "清洁", "家居", "五金", "卫浴", "水具"]
    big_p, big_s = [], []
    start = TODAY - timedelta(days=89)
    for i in range(1, 151):
        sku = f"G{i:03d}"
        price = round(rnd.uniform(9.9, 99.9), 1)
        cost = round(price * rnd.uniform(0.35, 0.8), 1)
        base_units = rnd.randint(0, 12)
        big_p.append([sku, f"规模样例商品{i:03d}", cats[i % len(cats)], (TODAY - timedelta(days=rnd.randint(40, 600))).isoformat(),
                      g(price), g(cost), rnd.randint(0, 900), ""])
        for k in range(90):
            u = max(0, base_units + rnd.randint(-2, 2))
            big_s.append([sku, (start + timedelta(days=k)).isoformat(), g(price), u, f"{price * u:.2f}", "",
                          rnd.randint(40, 400), "", ""])
    write_csv(var / "规模样例" / "商品表.csv", P_COLS, big_p)
    write_csv(var / "规模样例" / "销售日记录.csv", S_COLS, big_s)
    print(f"样例店与变体已生成：{OUT}")


if __name__ == "__main__":
    main()
