"""用《用户验收条例 v1.0》附录 A 的样例店核对门 0 体检脚本：A.3 逐 SKU 预期值、A.4 体检汇总。"""
import pytest

from gate0.calc import analyze
from gate0.io import load
from samples.make_samples import write

# 编号: (保本价, 毛利率, 近 30 天日均, 库存天数, 生命周期, 数据不足原因)
A3 = {
    "S01": (25.71, 0.3556, 6, 130, "尾货期", ""),
    "S02": (25.71, 0.0073, 5, 24, "成熟期", ""),
    "S03": (7.11, 0.2818, 6, 16.7, "成熟期", ""),
    "S04": (33.37, 0.0957, 10, 15, "成熟期", ""),
    "S05": (20.24, 0.3231, 0.17, 300, "新品期", "记录不足 14 天"),
    "S06": (25.71, 0.4287, 0, None, "尾货期", "无动销"),
    "S07": (None, None, 3, 30, "成熟期", "缺成本价"),
    "S08": (14.77, 0.2578, 5.2, 15.4, "成熟期", ""),
    "S09": (12.58, 0.2972, 5.7, 10.5, "成熟期", ""),
    "S10": (25.71, 0.4287, 5, 32, "成熟期", ""),
    "S11": (25.71, 0.4287, 5, 32, "成熟期", ""),
    "S12": (16.96, 0.3452, 2, 33, "新品期", ""),
}


@pytest.fixture(scope="module")
def res(tmp_path_factory):
    d = tmp_path_factory.mktemp("样例店")
    write(d)
    return analyze(load(d))


@pytest.fixture(scope="module")
def by(res):
    return {s["商品编号"]: s for s in res["skus"]}


@pytest.mark.parametrize("sku", sorted(A3))
def test_a3_per_sku(by, sku):
    be, margin, avg, days, stage, reason = A3[sku]
    s = by[sku]
    assert s["保本价"] == be
    assert s["毛利率"] == margin
    assert round(s["近30天日均"], 2) == avg
    if days is None:
        assert s["库存天数"] is None
    else:
        assert round(s["库存天数"], 1) == pytest.approx(days, abs=0.05)
    assert s["生命周期"] == stage
    assert s["数据不足原因"] == reason
    assert s["数据状态"] == ("数据不足" if reason else "正常")


def test_s04_ad_cost(by):
    s = by["S04"]
    assert s["单件广告成本"] == 3.00
    assert s["不含广告保本价"] == 30.09
    assert s["广告口径"] == "含广告"
    assert by["S03"]["广告口径"] == "无广告数据"


def test_flags(by):
    assert by["S02"]["贴近成本"] and not by["S04"]["贴近成本"]
    assert by["S01"]["疑似偏高"]
    assert by["S04"]["疑似偏低"]
    assert [k for k, s in by.items() if s["库存超滞销线"]] == ["S01"]


def test_s09_event_and_elasticity(res, by):
    assert len(res["events"]) == 1
    e = res["events"][0]
    assert e["商品编号"] == "S09" and e["口径"] == "转化弹性"
    assert round(e["弹性"], 2) == -1.72
    assert by["S09"]["弹性来源"] == "用先验" and by["S09"]["采用弹性"] == -1.34
    assert by["S09"]["置信度"] == "中"
    assert by["S12"]["置信度"] == "低"


def test_s08_activity_day(res):
    assert [(a["商品编号"], a["成交件数"]) for a in res["activity"]] == [("S08", 40)]


def test_a4_summary(res):
    sm = res["summary"]
    assert (sm["SKU 总数"], sm["有成本价 SKU"]) == (12, 11)
    assert sm["数据不足"] == ["S05", "S06", "S07"]
    assert sm["分母"] == 9
    assert sm["贴近成本"] == ["S02"]
    assert sm["疑似偏高"] == ["S01"]
    assert sm["疑似偏低"] == ["S04"]
    assert round(sm["需要动价占比"], 3) == 0.333
    assert round(sm["库存超滞销线占比"], 3) == 0.111
    assert sm["无动销"] == ["S06"]
    assert sm["有改价事件"] == ["S09"]
    assert sm["有访客数"] == ["S09"]
    assert sm["近30天有广告花费"] == ["S04"]
    assert sm["H1"] is False  # 样例店库存超 90 天只有 11.1%，H1 不过


def test_counter_examples_cover_six_kinds(res):
    kinds = {c["类别"]: c["商品编号"] for c in res["counter"]}
    assert kinds == {"弹性低库存高": "S01", "竞品低毛利薄": "S02", "新品无历史": "S12",
                     "无动销": "S06", "活动日异常": "S08", "成本缺失": "S07"}
