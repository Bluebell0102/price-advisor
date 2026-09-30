"""口径边界：舍入、活动日、改价事件、广告口径、生命周期优先级。"""
from datetime import date, timedelta

from advisor import calc, params

P = params.defaults()


def recs(prices, units, start=date(2026, 7, 1), **kw):
    return {start + timedelta(days=i): calc.DayRec(start + timedelta(days=i), p, u, p * u, **kw)
            for i, (p, u) in enumerate(zip(prices, units))}


def test_cents_round_half_up():
    assert calc.cents(0.125) == 0.13
    assert calc.cents(2.675) == 2.68


def test_breakeven_with_and_without_ad():
    assert calc.breakeven(24.0, P, 3.0) == 33.37
    assert calc.breakeven(24.0, P) == 30.09


def test_activity_rules():
    assert calc.activity_days(recs([10.0] * 20, [0] * 19 + [1]), P) == set()
    assert calc.activity_days(recs([10.0] * 5, [2, 2, 2, 2, 30]), P) == set()
    assert calc.activity_days(recs([10.0] * 20, [2] * 19 + [7]), P) == {date(2026, 7, 20)}


def test_one_change_one_event_and_needs_7_after():
    assert calc.price_events(recs([20.0] * 10 + [18.0] * 10, [5] * 20), P) == [date(2026, 7, 11)]
    assert calc.price_events(recs([20.0] * 10 + [18.0] * 6, [5] * 16), P) == []


def test_ad_spend_without_ad_units_counts_zero():
    r = recs([30.0] * 30, [5] * 30, ad_spend=2.0, ad_units=0)
    unit, spend, units, note = calc.unit_ad_cost(r, list(r))
    assert unit == 0 and spend == 60 and note.startswith("有广告花费但无广告成交")


def test_manual_stage_wins():
    prod = calc.Product("X", "x", "c", date(2025, 1, 1), 30.0, 10.0, 10.0, manual_stage="尾货期")
    r = recs([30.0] * 40, [5] * 40)
    q = calc.base_quantities(prod, r, max(r), P)
    assert (q["生命周期"], q["生命周期来源"]) == ("尾货期", "店主标记")


def test_cents_noise_is_not_a_price_event():
    prices = [19.90, 19.87, 19.90, 19.88, 19.90, 19.89, 19.90] * 3
    assert calc.price_events(recs(prices, [5] * len(prices)), P) == []


def test_candidate_floor_never_loses_money():
    from advisor import model
    q = {"当前价": 39.9, "近30天日均": 6, "成本价": 20.0, "单件广告成本": 0.0, "近30天广告花费": 0, "近30天广告成交件数": 0,
         "库存": 780, "近30天访客数": None, "近30天成交件数": 180, "保本价": 25.71, "数据状态": "正常", "生命周期": "尾货期",
         "库存天数": 130, "贴近成本": False, "改价事件": [], "记录天数": 60, "广告口径": "无广告数据", "数据不足原因": ""}
    el = {"生效": -1.34, "来源": "类目先验", "校准误差": None, "用先验": True, "微调": 0}
    s = model.suggest(q, el, [], P)
    est = model.Estimator(q, -1.34, P)
    assert all(est.at(x)["单件毛利"] >= 0 for x in s["区间"])


def test_cap_is_not_truncated_by_float():
    from advisor import model
    assert model.floor_cents(model.mul(19.9, 1.3)) == 25.87  # 19.9 × 1.3 浮点是 25.869999…
