"""按《用户验收条例 v1.0》附录 A 生成样例店：12 个虚构 SKU、60 天记录，第 60 天为「今天」。

用法：uv run python samples/make_samples.py [输出目录]，默认 samples/样例店。
"""
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

TODAY = date(2026, 9, 29)
DAY1 = TODAY - timedelta(days=59)


def day(n: int) -> date:
    return DAY1 + timedelta(days=n - 1)


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


def sales():
    """逐 SKU 逐日：(编号, 第几天, 当日售价, 成交件数, 访客数, 广告花费, 广告成交件数)"""
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
        yield "S09", n, 19.9 if n <= 39 else 17.9, 5 if n <= 39 else 6, 100, None, None
        yield "S10", n, 45.0, 5, None, None, None
        yield "S11", n, 45.0, 5, None, None, None
        if n >= 41:
            yield "S12", n, 25.9, 3, None, None, None


COMPETITORS = [("S10", 1, 37.5), ("S10", 2, 35.9), ("S10", 3, 42.0), ("S11", 1, 30.0)]

PARAMS = {
    "_说明": "样例店参数，见用户验收条例附录 A.1；今天 = 第 60 天",
    "平台": "样例平台",
    "扣点率": 0.05,
    "单件履约费": 3.5,
    "退货率": 0.06,
    "单次退货净损失占比": 0.6,
    "最低毛利率": 0.05,
    "尾货期最低毛利率": 0.0,
    "目标清仓天数": 45,
    "滞销线": 90,
    "今天": TODAY.isoformat(),
    "店主一句话": "（样例店，无）",
}


def fmt(x):
    if x is None:
        return ""
    return f"{x:g}" if isinstance(x, (int, float)) else str(x)


def write(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "商品表.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["商品编号", "商品名称", "类目", "上架日期", "当前售价", "成本价", "库存数量", "生命周期标记"])
        for sku, name, cat, listed, cost, price, stock in PRODUCTS:
            w.writerow([sku, name, cat, (TODAY - timedelta(days=listed)).isoformat(), fmt(price), fmt(cost), stock, ""])
    with open(out / "销售日记录.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["商品编号", "日期", "当日售价", "成交件数", "成交金额", "退款件数", "访客数", "广告花费", "广告成交件数"])
        for sku, n, price, units, vis, spend, adu in sales():
            w.writerow([sku, day(n).isoformat(), fmt(price), units, f"{price * units:.2f}", "", fmt(vis), fmt(spend), fmt(adu)])
    with open(out / "竞品价格.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["商品编号", "竞品序号", "竞品售价", "录入日期"])
        for sku, idx, price in COMPETITORS:
            w.writerow([sku, idx, fmt(price), TODAY.isoformat()])
    (out / "店铺参数.json").write_text(json.dumps(PARAMS, ensure_ascii=False, indent=2), encoding="utf-8")
    print("样例店已生成：", out)


if __name__ == "__main__":
    write(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "样例店")
