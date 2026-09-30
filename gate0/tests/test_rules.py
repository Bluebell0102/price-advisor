"""口径边界：舍入、活动日、改价事件、广告口径、读入容错。"""
import csv
import json
from datetime import date, timedelta

from gate0.calc import activity_days, analyze, cents, price_events
from gate0.io import DayRec, load


def recs(prices, units, start=date(2026, 7, 1)):
    return {start + timedelta(days=i): DayRec("X", start + timedelta(days=i), p, u, p * u)
            for i, (p, u) in enumerate(zip(prices, units))}


def test_cents_round_half_up():
    assert cents(0.125) == 0.13
    assert cents(25.715) == 25.72
    assert cents(2.675) == 2.68  # 二进制浮点下 round() 会给 2.67


def test_activity_needs_positive_median_and_7_prior_days():
    r = recs([10.0] * 20, [0] * 19 + [1])
    assert activity_days(r) == set()  # 中位数 0 不判
    r = recs([10.0] * 5, [2, 2, 2, 2, 30])
    assert activity_days(r) == set()  # 此前记录不足 7 天不判
    r = recs([10.0] * 20, [2] * 19 + [7])
    assert activity_days(r) == {date(2026, 7, 20)}


def test_one_price_change_is_one_event():
    r = recs([20.0] * 10 + [18.0] * 10, [5] * 20)
    assert price_events(r, date(2026, 1, 1)) == [date(2026, 7, 11)]


def test_event_needs_7_days_after():
    r = recs([20.0] * 10 + [18.0] * 6, [5] * 16)
    assert price_events(r, date(2026, 1, 1)) == []


def _write(d, products, sales, params=None, mapping=None, enc="utf-8-sig"):
    with open(d / "商品表.csv", "w", newline="", encoding=enc) as f:
        w = csv.writer(f)
        w.writerow(["商品编号", "商品名称", "类目", "上架日期", "当前售价", "成本价", "库存数量", "生命周期标记"])
        w.writerows(products)
    with open(d / "销售日记录.csv", "w", newline="", encoding=enc) as f:
        w = csv.writer(f)
        w.writerow(sales[0])
        w.writerows(sales[1:])
    if params:
        (d / "店铺参数.json").write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
    if mapping:
        (d / "列名映射.json").write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")


def test_spend_without_ad_units_counts_zero(tmp_path):
    day0 = date(2026, 9, 1)
    sales = [["商品编号", "日期", "当日售价", "成交件数", "成交金额", "广告花费", "广告成交件数"]]
    sales += [["A1", (day0 + timedelta(days=i)).isoformat(), "30", "5", "150", "2", "0"] for i in range(30)]
    _write(tmp_path, [["A1", "测试品", "测试", "2026-01-01", "30", "20", "100", ""]], sales)
    s = analyze(load(tmp_path))["skus"][0]
    assert s["单件广告成本"] == 0 and s["广告口径"] == "有花费无广告成交，按 0 计"


def test_gbk_mapping_and_bad_rows(tmp_path):
    day0 = date(2026, 9, 1)
    sales = [["商品编号", "日期", "售价", "销量", "销售额"]]
    sales += [["A1", (day0 + timedelta(days=i)).strftime("%Y/%m/%d"), "¥30", "5", "1,500", ] for i in range(20)]
    sales += [["A1", "2026/13/01", "30", "5", "150"], ["A1", "2026-09-05", "0", "5", "0"]]
    products = [["A1", "测试品", "测试", "2026-01-01", "30", "20", "100", ""], ["A1", "重复", "测试", "2026-01-01", "30", "20", "1", ""]]
    _write(tmp_path, products, sales,
           mapping={"销售日记录": {"售价": "当日售价", "销量": "成交件数", "销售额": "成交金额"}}, enc="gb18030")
    ds = load(tmp_path)
    reasons = [i["原因"] for i in ds.issues]
    assert any("商品编号重复" in r for r in reasons)
    assert any("日期格式错误" in r for r in reasons)
    assert any("当日售价为空或为 0" in r for r in reasons)
    assert len(ds.recs) == 20 and ds.recs[0].amount == 1500


def test_cents_noise_is_not_a_price_event():
    prices = [19.90, 19.87, 19.90, 19.88, 19.90, 19.89, 19.90] * 3
    r = recs(prices, [5] * len(prices))
    assert price_events(r, date(2026, 1, 1)) == []
