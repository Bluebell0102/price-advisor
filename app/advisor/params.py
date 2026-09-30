"""参数：默认值来自 config/params.default.json（PRD 9.1），店铺设置存数据库，代码里不硬编码数值。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .db import ROOT, get_setting, put_setting

DEFAULT_FILE = ROOT / "config" / "params.default.json"

# 设置页可改的字段：键、显示名、类型（pct = 百分比输入、money、int、choice）
FIELDS = [
    ("平台", "平台（只影响默认扣点率）", "choice"),
    ("扣点率", "扣点率", "pct"),
    ("单件履约费", "单件履约费", "money"),
    ("退货率", "退货率", "pct"),
    ("单次退货净损失占比", "单次退货净损失占售价比例", "pct"),
    ("最低毛利率", "最低毛利率", "pct"),
    ("尾货期最低毛利率", "尾货期最低毛利率", "pct"),
    ("目标清仓天数", "目标清仓天数", "int"),
    ("滞销线", "滞销库存天数线", "int"),
]


def defaults() -> dict:
    d = json.loads(DEFAULT_FILE.read_text(encoding="utf-8"))
    return {k: v for k, v in d.items() if not k.startswith("_")}


def effective(conn) -> dict:
    p = defaults()
    p.update(get_setting(conn, "store", {}))
    return p


def settings_confirmed(conn) -> bool:
    return bool(get_setting(conn, "settings_confirmed", False))


def category_elasticities(conn, categories: list[str]) -> list[dict]:
    """每个类目的基准弹性（先验或门 0 校准值）与商家微调，生效值 = 基准 ×（1 + 微调）。"""
    p = effective(conn)
    rows = {r["category"]: r for r in conn.execute("SELECT * FROM category_elasticity")}
    out = []
    for cat in categories:
        prior = p["类目先验"].get(cat, p["全类目先验"])
        r = rows.get(cat)
        base = r["base"] if r else prior
        adj = r["adjust"] if r else 0.0
        out.append({"类目": cat, "先验": prior, "基准": base, "微调": adj, "生效": base * (1 + adj),
                    "来源": "已改" if r and abs(base - prior) > 1e-9 else "先验"})
    return out


class Invalid(ValueError):
    pass


def _num(raw: str, label: str) -> float:
    try:
        return float(str(raw).strip().replace("%", ""))
    except ValueError:
        raise Invalid(f"{label}：请填数字")


def parse_settings(form: dict, p: dict) -> tuple[dict, list[str]]:
    """把设置页表单转成参数值；百分比字段按 0～100 输入。返回 (新值, 错误列表)。"""
    new, errs = {}, []
    for key, label, kind in FIELDS:
        raw = form.get(key)
        if raw is None or str(raw).strip() == "":
            errs.append(f"{label}：不能为空")
            continue
        try:
            if kind == "choice":
                if raw not in p["平台默认扣点率"]:
                    raise Invalid(f"{label}：只能选 {'、'.join(p['平台默认扣点率'])}")
                new[key] = raw
            elif kind == "pct":
                v = _num(raw, label)
                if v < 0 or v > 100:
                    raise Invalid(f"{label}：合法范围 0% 到 100%，你填的是 {v:g}%")
                new[key] = round(v / 100, 6)
            elif kind == "money":
                v = _num(raw, label)
                if v < 0:
                    raise Invalid(f"{label}：不能为负数")
                new[key] = v
            elif kind == "int":
                v = _num(raw, label)
                if v <= 0 or v != int(v):
                    raise Invalid(f"{label}：请填正整数")
                new[key] = int(v)
        except Invalid as e:
            errs.append(str(e))
    if not errs and new["扣点率"] + new["退货率"] * new["单次退货净损失占比"] >= 1:
        errs.append("扣点率与退货损耗合计不能达到 100%，否则保本价无法计算")
    return new, errs


def parse_elasticity(form: dict, categories: list[str], p: dict) -> tuple[list[tuple], list[str]]:
    lo, hi = p["弹性硬限"]
    cap = p["弹性微调上限"]
    out, errs = [], []
    for i, cat in enumerate(categories):
        b, a = form.get(f"el_base_{i}"), form.get(f"el_adj_{i}")
        try:
            base = _num(b, f"{cat} 基准弹性")
            adj = _num(a if a not in (None, "") else "0", f"{cat} 微调") / 100
        except Invalid as e:
            errs.append(str(e))
            continue
        if not (lo <= base <= hi):
            errs.append(f"{cat} 基准弹性：合法范围 {lo:g} 到 {hi:g}，你填的是 {base:g}")
            continue
        if abs(adj) > cap + 1e-9:
            errs.append(f"{cat} 微调：合法范围 −{cap:.0%} 到 +{cap:.0%}，你填的是 {adj:+.0%}")
            continue
        eff = base * (1 + adj)
        if not (lo <= eff <= hi):
            errs.append(f"{cat} 弹性结果 {eff:.2f} 超出 {lo:g} 到 {hi:g}")
            continue
        out.append((cat, base, adj))
    return out, errs


def save_settings(conn, new: dict, elastic: list[tuple]) -> list[tuple]:
    """写入设置与变更日志，返回改动列表 [(字段, 旧, 新)]。调用方负责事务。"""
    old = effective(conn)
    store = get_setting(conn, "store", {})
    changes = []
    now = datetime.now().isoformat(timespec="seconds")
    for k, v in new.items():
        if old.get(k) != v:
            changes.append((k, old.get(k), v))
        store[k] = v
    put_setting(conn, "store", store)
    cur = {r["category"]: (r["base"], r["adjust"]) for r in conn.execute("SELECT * FROM category_elasticity")}
    prior = old["类目先验"]
    for cat, base, adj in elastic:
        before = cur.get(cat, (prior.get(cat, old["全类目先验"]), 0.0))
        if abs(before[0] - base) > 1e-9 or abs(before[1] - adj) > 1e-9:
            changes.append((f"类目弹性·{cat}", f"{before[0]:g} × (1{before[1]:+.0%})", f"{base:g} × (1{adj:+.0%})"))
            conn.execute("INSERT INTO category_elasticity(category, base, adjust) VALUES(?,?,?) "
                         "ON CONFLICT(category) DO UPDATE SET base = excluded.base, adjust = excluded.adjust", (cat, base, adj))
    for f, o, n in changes:
        conn.execute("INSERT INTO setting_log(at, field, old, new) VALUES(?,?,?,?)",
                     (now, f, json.dumps(o, ensure_ascii=False), json.dumps(n, ensure_ascii=False)))
    return changes
