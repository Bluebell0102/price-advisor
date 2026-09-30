"""读三张模板 CSV 与店铺参数。列名按 PRD 11.1；原始列名不同时用 列名映射.json 转换。

读入时不中断：坏行跳过并记进 issues，体检报告里单列「校验问题」。
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

PRODUCT_COLS = ["商品编号", "商品名称", "类目", "上架日期", "当前售价", "成本价", "库存数量", "生命周期标记"]
PRODUCT_REQUIRED = ["商品编号", "商品名称", "类目", "上架日期", "当前售价", "成本价", "库存数量"]
SALES_COLS = ["商品编号", "日期", "当日售价", "成交件数", "成交金额", "退款件数", "访客数", "广告花费", "广告成交件数"]
SALES_REQUIRED = ["商品编号", "日期", "当日售价", "成交件数", "成交金额"]
COMP_COLS = ["商品编号", "竞品序号", "竞品售价", "录入日期"]

FILES = {"商品表": "商品表.csv", "销售日记录": "销售日记录.csv", "竞品价格": "竞品价格.csv"}

# 脱敏检查：表头里出现这些词就算没脱敏（PT-00-1）
SENSITIVE_WORDS = ["店名", "店铺", "商家", "买家", "收货", "手机", "电话", "地址", "旺旺", "昵称", "会员名", "身份证"]

DEFAULT_PARAMS = {
    "平台": "未填",
    "扣点率": 0.05,
    "单件履约费": 3.5,
    "退货率": 0.06,
    "单次退货净损失占比": 0.6,
    "最低毛利率": 0.05,
    "尾货期最低毛利率": 0.0,
    "目标清仓天数": 45,
    "滞销线": 90,
    "贴近成本系数": 1.05,
    "疑似偏高毛利率线": 0.30,
    "疑似偏低库存天数线": 20,
    "疑似偏低毛利率线": 0.15,
    "改价事件最小幅度": 0.01,
    "需要动价占比判据": 0.30,
    "库存超滞销线占比判据": 0.15,
    "有成本价SKU下限": 50,
    "覆盖天数下限": 90,
    "类目先验": {"消费电子": -1.72, "服饰": -0.89},
    "全类目先验": -1.34,
    "今天": None,
    "店主一句话": "",
}


@dataclass
class Product:
    sku: str
    name: str
    category: str
    listed: date | None
    price: float
    cost: float | None
    stock: float
    manual_stage: str | None


@dataclass
class DayRec:
    sku: str
    day: date
    price: float
    units: float
    amount: float
    refunds: float | None = None
    visitors: float | None = None
    ad_spend: float | None = None
    ad_units: float | None = None


@dataclass
class Comp:
    sku: str
    idx: int
    price: float
    day: date | None


@dataclass
class Dataset:
    products: list[Product]
    recs: list[DayRec]
    comps: list[Comp]
    params: dict
    issues: list[dict] = field(default_factory=list)
    headers: dict = field(default_factory=dict)


def parse_date(s: str) -> date:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y.%m.%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"日期格式错误：{s}")


def parse_num(s: str | None) -> float | None:
    if s is None:
        return None
    t = str(s).strip().replace("¥", "").replace("￥", "").replace(",", "").replace("，", "")
    if t in ("", "-", "--", "—"):
        return None
    pct = t.endswith("%")
    v = float(t.rstrip("%"))
    return v / 100 if pct else v


def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"{path.name} 编码无法识别")
    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return [], []
    head = [h.strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue
        out.append({head[i]: (r[i].strip() if i < len(r) else "") for i in range(len(head))})
    return head, out


def _apply_mapping(head: list[str], rows: list[dict], mapping: dict) -> tuple[list[str], list[dict]]:
    if not mapping:
        return head, rows
    head2 = [mapping.get(h, h) for h in head]
    rows2 = [{mapping.get(k, k): v for k, v in r.items()} for r in rows]
    return head2, rows2


def load(data_dir: str | Path) -> Dataset:
    d = Path(data_dir)
    params = dict(DEFAULT_PARAMS)
    pfile = d / "店铺参数.json"
    if pfile.exists():
        user = json.loads(pfile.read_text(encoding="utf-8"))
        params.update({k: v for k, v in user.items() if not k.startswith("_")})
    if isinstance(params.get("今天"), str) and params["今天"]:
        params["今天"] = parse_date(params["今天"])
    mfile = d / "列名映射.json"
    mapping = json.loads(mfile.read_text(encoding="utf-8")) if mfile.exists() else {}

    issues: list[dict] = []
    headers: dict = {}

    def issue(table, line, reason):
        issues.append({"表": table, "行号": line, "原因": reason})

    # 商品表
    head, rows = _read_csv(d / FILES["商品表"])
    head, rows = _apply_mapping(head, rows, mapping.get("商品表", {}))
    headers["商品表"] = head
    missing = [c for c in PRODUCT_REQUIRED if c not in head]
    if missing:
        raise ValueError(f"商品表缺列：{'、'.join(missing)}")
    products: list[Product] = []
    seen: dict[str, int] = {}
    for i, r in enumerate(rows, start=2):
        sku = r["商品编号"]
        if not sku:
            issue("商品表", i, "商品编号为空")
            continue
        if sku in seen:
            issue("商品表", i, f"商品编号重复：{sku}，首次在第 {seen[sku]} 行")
            continue
        try:
            price = parse_num(r["当前售价"])
            if not price or price <= 0:
                issue("商品表", i, f"{sku} 当前售价为空或为 0")
                continue
            products.append(Product(
                sku=sku, name=r["商品名称"], category=r["类目"] or "未分类",
                listed=parse_date(r["上架日期"]) if r["上架日期"] else None,
                price=price, cost=parse_num(r["成本价"]),
                stock=parse_num(r["库存数量"]) or 0.0,
                manual_stage=(r.get("生命周期标记") or None),
            ))
            seen[sku] = i
        except ValueError as e:
            issue("商品表", i, f"{sku} {e}")

    # 销售日记录
    head, rows = _read_csv(d / FILES["销售日记录"])
    head, rows = _apply_mapping(head, rows, mapping.get("销售日记录", {}))
    headers["销售日记录"] = head
    missing = [c for c in SALES_REQUIRED if c not in head]
    if missing:
        raise ValueError(f"销售日记录缺列：{'、'.join(missing)}")
    by_key: dict[tuple, DayRec] = {}
    known = {p.sku for p in products}
    for i, r in enumerate(rows, start=2):
        try:
            sku = r["商品编号"]
            day = parse_date(r["日期"])
            price = parse_num(r["当日售价"])
            if not price or price <= 0:
                issue("销售日记录", i, f"{sku} {day} 当日售价为空或为 0")
                continue
            if sku not in known:
                issue("销售日记录", i, f"{sku} 不在商品表里，已跳过")
                continue
            key = (sku, day)
            if key in by_key:
                # 同一编号同一日期：后一行覆盖前一行，与产品导入口径一致
                issue("销售日记录", i, f"{sku} {day} 重复，按后一行覆盖")
            by_key[key] = DayRec(
                sku=sku, day=day, price=price,
                units=parse_num(r["成交件数"]) or 0.0,
                amount=parse_num(r["成交金额"]) or 0.0,
                refunds=parse_num(r.get("退款件数")),
                visitors=parse_num(r.get("访客数")),
                ad_spend=parse_num(r.get("广告花费")),
                ad_units=parse_num(r.get("广告成交件数")),
            )
        except ValueError as e:
            issue("销售日记录", i, str(e))
    recs = list(by_key.values())

    # 竞品价格（可选）
    comps: list[Comp] = []
    cpath = d / FILES["竞品价格"]
    if cpath.exists():
        head, rows = _read_csv(cpath)
        head, rows = _apply_mapping(head, rows, mapping.get("竞品价格", {}))
        headers["竞品价格"] = head
        for i, r in enumerate(rows, start=2):
            try:
                p = parse_num(r.get("竞品售价"))
                if p and p > 0:
                    comps.append(Comp(sku=r["商品编号"], idx=int(parse_num(r.get("竞品序号")) or 0),
                                      price=p, day=parse_date(r["录入日期"]) if r.get("录入日期") else None))
            except ValueError as e:
                issue("竞品价格", i, str(e))

    return Dataset(products=products, recs=recs, comps=comps, params=params, issues=issues, headers=headers)


def sensitive_columns(headers: dict) -> list[str]:
    hits = []
    for table, cols in headers.items():
        for c in cols:
            if any(w in c for w in SENSITIVE_WORDS):
                hits.append(f"{table}.{c}")
    return hits
