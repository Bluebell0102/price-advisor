"""导入与校验（PRD 5.1、6.3 F-01、11.1 到 11.3）。

流程：读 CSV → 认表头（模板列名 / 已记住的映射 / 同义词猜测）→ 逐行校验 → 有硬错误整批拒收 →
全部通过才在一个事务里写库（商品按编号覆盖，销售按编号 + 日期覆盖，竞品按编号 + 序号覆盖）。
缺成本价、记录不足 14 天不算错误：照常导入，标数据不足。
"""
from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime

TABLES = {
    "商品表": {
        "cols": ["商品编号", "商品名称", "类目", "上架日期", "当前售价", "成本价", "库存数量", "生命周期标记"],
        "required": ["商品编号", "商品名称", "类目", "上架日期", "当前售价", "成本价", "库存数量"],
    },
    "销售日记录": {
        "cols": ["商品编号", "日期", "当日售价", "成交件数", "成交金额", "退款件数", "访客数", "广告花费", "广告成交件数"],
        "required": ["商品编号", "日期", "当日售价", "成交件数", "成交金额"],
    },
    "竞品价格": {
        "cols": ["商品编号", "竞品序号", "竞品售价", "录入日期"],
        "required": ["商品编号", "竞品序号", "竞品售价"],
    },
}
TEMPLATE_FILES = {"商品表": "商品表模板.csv", "销售日记录": "销售日记录模板.csv", "竞品价格": "竞品价格模板.csv"}

# 平台后台导出的常见列名 → 模板列名，只用来预填映射，店主确认后才生效
SYNONYMS = {
    "商品编号": ["商品ID", "商品id", "商品编码", "商家编码", "货号", "SKU", "sku", "宝贝ID", "商品Id"],
    "商品名称": ["商品标题", "宝贝标题", "商品名", "标题", "名称"],
    "类目": ["类目名称", "叶子类目", "分类", "商品分类", "所属类目"],
    "上架日期": ["上架时间", "发布时间", "创建时间"],
    "当前售价": ["售价", "价格", "一口价", "商品价格", "现价"],
    "成本价": ["成本", "进货价", "采购价"],
    "库存数量": ["库存", "可售库存", "总库存"],
    "生命周期标记": ["生命周期"],
    "日期": ["统计日期", "日期时间", "时间"],
    "当日售价": ["售价", "成交均价", "价格"],
    "成交件数": ["销量", "支付件数", "成交数量", "销售件数", "件数"],
    "成交金额": ["支付金额", "销售额", "成交额", "GMV"],
    "退款件数": ["退款数", "退货件数", "成功退款件数"],
    "访客数": ["商品访客数", "访客", "UV"],
    "广告花费": ["花费", "推广花费", "消耗"],
    "广告成交件数": ["广告成交", "推广成交件数", "成交笔数"],
    "竞品序号": ["序号"],
    "竞品售价": ["竞品价格", "竞品价"],
    "录入日期": ["日期"],
}

IGNORE = "（不导入）"


@dataclass
class Issue:
    table: str
    line: int
    reason: str
    hard: bool = True


@dataclass
class Parsed:
    products: list[dict] = field(default_factory=list)
    sales: list[dict] = field(default_factory=list)
    comps: list[dict] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    rows_checked: int = 0

    @property
    def errors(self):
        return [i for i in self.issues if i.hard]

    @property
    def warnings(self):
        return [i for i in self.issues if not i.hard]


class Rejected(Exception):
    """整批拒收：缺列、非模板、编码无法识别。"""


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise Rejected("文件编码无法识别，请另存为 UTF-8 或 GBK 的 CSV")


def read_csv(raw: bytes) -> tuple[list[str], list[list[str]]]:
    rows = list(csv.reader(io.StringIO(decode(raw))))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise Rejected("文件是空的，请下载模板")
    return [h.strip() for h in rows[0]], rows[1:]


def signature(head: list[str]) -> str:
    return json.dumps(sorted(head), ensure_ascii=False)


def guess(tbl: str, head: list[str]) -> dict[str, str]:
    """给每个文件列猜一个模板列：先精确，再同义词。"""
    cols = TABLES[tbl]["cols"]
    out, used = {}, set()
    for h in head:
        if h in cols and h not in used:
            out[h], used = h, used | {h}
    for h in head:
        if h in out:
            continue
        for target, syns in SYNONYMS.items():
            if target in cols and target not in used and h in syns:
                out[h], used = target, used | {target}
                break
    return out


@dataclass
class HeaderCheck:
    status: str  # ok / mapping / missing / not_template
    mapping: dict[str, str]
    missing: list[str]
    head: list[str]


def check_header(tbl: str, head: list[str], remembered: dict | None) -> HeaderCheck:
    req = TABLES[tbl]["required"]
    if remembered is not None:
        m = {k: v for k, v in remembered.items() if v != IGNORE}
        missing = [c for c in req if c not in m.values()]
        return HeaderCheck("ok" if not missing else "missing", m, missing, head)
    g = guess(tbl, head)
    exact = {h: h for h in head if h in TABLES[tbl]["cols"]}
    recognized = [c for c in req if c in g.values()]
    if len(recognized) < math.ceil(len(req) / 2):
        return HeaderCheck("not_template", g, [c for c in req if c not in recognized], head)
    missing = [c for c in req if c not in g.values()]
    unknown = [h for h in head if h not in g]
    if g == exact and not missing:
        return HeaderCheck("ok", g, [], head)
    if missing and not unknown:
        return HeaderCheck("missing", g, missing, head)
    return HeaderCheck("mapping", g, missing, head)


def parse_date(s: str) -> date:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y.%m.%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError("日期格式错误")


def parse_num(s) -> float | None:
    t = str(s or "").strip().replace("¥", "").replace("￥", "").replace(",", "").replace("，", "")
    if t in ("", "-", "--", "—"):
        return None
    pct = t.endswith("%")
    v = float(t.rstrip("%"))
    return v / 100 if pct else v


def _rows(tbl, head, body, mapping):
    idx = {mapping[h]: i for i, h in enumerate(head) if h in mapping and mapping[h] != IGNORE}
    for n, r in enumerate(body, start=2):
        yield n, {c: (r[i].strip() if i < len(r) else "") for c, i in idx.items()}


def validate(files: dict[str, tuple[list[str], list[list[str]], dict]], known_skus: set[str]) -> Parsed:
    """files: 表名 → (表头, 数据行, 映射)。逐行校验，硬错误进 errors，缺成本价进 warnings。"""
    out = Parsed()
    batch_skus = set()
    if "商品表" in files:
        head, body, m = files["商品表"]
        first_line: dict[str, int] = {}
        for n, r in _rows("商品表", head, body, m):
            out.rows_checked += 1
            sku = r.get("商品编号", "")
            if not sku:
                out.issues.append(Issue("商品表", n, "商品编号为空"))
                continue
            if sku in first_line:
                out.issues.append(Issue("商品表", n, f"商品编号重复：{sku}，与第 {first_line[sku]} 行重复"))
                continue
            first_line[sku] = n
            try:
                price = parse_num(r.get("当前售价"))
            except ValueError:
                price = None
            if not price or price <= 0:
                out.issues.append(Issue("商品表", n, f"{sku} 当前售价为空或为 0"))
                continue
            try:
                listed = parse_date(r["上架日期"]) if r.get("上架日期") else None
            except ValueError:
                out.issues.append(Issue("商品表", n, f"{sku} 上架日期格式错误：{r['上架日期']}"))
                continue
            try:
                cost = parse_num(r.get("成本价"))
                stock = parse_num(r.get("库存数量"))
            except ValueError:
                out.issues.append(Issue("商品表", n, f"{sku} 成本价或库存数量不是数字"))
                continue
            if stock is None or stock < 0:
                out.issues.append(Issue("商品表", n, f"{sku} 库存数量为空或为负数"))
                continue
            if cost is None:
                out.issues.append(Issue("商品表", n, f"{sku} 成本价缺失，导入后标数据不足，可在总览补录", hard=False))
            elif cost < 0:
                out.issues.append(Issue("商品表", n, f"{sku} 成本价为负数"))
                continue
            stage = r.get("生命周期标记") or None
            if stage and stage not in ("新品期", "成熟期", "尾货期"):
                out.issues.append(Issue("商品表", n, f"{sku} 生命周期标记只能是新品期、成熟期、尾货期或留空"))
                continue
            batch_skus.add(sku)
            out.products.append({"sku": sku, "name": r.get("商品名称") or sku, "category": r.get("类目") or "未分类",
                                 "listed_date": listed.isoformat() if listed else None, "price": price, "cost": cost,
                                 "stock": stock, "manual_stage": stage})
    skus = known_skus | batch_skus
    if "销售日记录" in files:
        head, body, m = files["销售日记录"]
        seen: dict[tuple, int] = {}
        for n, r in _rows("销售日记录", head, body, m):
            out.rows_checked += 1
            sku = r.get("商品编号", "")
            try:
                day = parse_date(r.get("日期", ""))
            except ValueError:
                out.issues.append(Issue("销售日记录", n, f"日期格式错误：{r.get('日期', '')}"))
                continue
            if sku not in skus:
                out.issues.append(Issue("销售日记录", n, f"{sku or '（空）'} 不在商品表里，请先导入商品表"))
                continue
            key = (sku, day)
            if key in seen:
                out.issues.append(Issue("销售日记录", n, f"{sku} {day} 与第 {seen[key]} 行重复，同一商品同一天只能一行"))
                continue
            seen[key] = n
            try:
                price = parse_num(r.get("当日售价"))
                units = parse_num(r.get("成交件数"))
                amount = parse_num(r.get("成交金额"))
                opt = {k: parse_num(r.get(k)) for k in ("退款件数", "访客数", "广告花费", "广告成交件数")}
            except ValueError:
                out.issues.append(Issue("销售日记录", n, f"{sku} {day} 有数值列不是数字"))
                continue
            if not price or price <= 0:
                out.issues.append(Issue("销售日记录", n, f"{sku} {day} 当日售价为空或为 0"))
                continue
            if units is None or units < 0 or amount is None or amount < 0:
                out.issues.append(Issue("销售日记录", n, f"{sku} {day} 成交件数或成交金额为空或为负数"))
                continue
            if any(v is not None and v < 0 for v in opt.values()):
                out.issues.append(Issue("销售日记录", n, f"{sku} {day} 退款、访客或广告列有负数"))
                continue
            out.sales.append({"sku": sku, "day": day.isoformat(), "price": price, "units": units, "amount": amount,
                              "refunds": opt["退款件数"], "visitors": opt["访客数"], "ad_spend": opt["广告花费"],
                              "ad_units": opt["广告成交件数"]})
    if "竞品价格" in files:
        head, body, m = files["竞品价格"]
        for n, r in _rows("竞品价格", head, body, m):
            out.rows_checked += 1
            sku = r.get("商品编号", "")
            if sku not in skus:
                out.issues.append(Issue("竞品价格", n, f"{sku or '（空）'} 不在商品表里"))
                continue
            try:
                idx = parse_num(r.get("竞品序号"))
                price = parse_num(r.get("竞品售价"))
                day = parse_date(r["录入日期"]).isoformat() if r.get("录入日期") else None
            except ValueError:
                out.issues.append(Issue("竞品价格", n, f"{sku} 序号、售价或录入日期格式错误"))
                continue
            if idx is None or idx != int(idx) or not 1 <= idx <= 5:
                out.issues.append(Issue("竞品价格", n, f"{sku} 竞品序号只能是 1 到 5"))
                continue
            if not price or price <= 0:
                out.issues.append(Issue("竞品价格", n, f"{sku} 竞品售价为空或为 0"))
                continue
            out.comps.append({"sku": sku, "idx": int(idx), "price": price, "day": day})
    return out


def write(conn, parsed: Parsed, fail_after: int | None = None) -> tuple[dict, list[dict]]:
    """在调用方的事务里写库，返回 (覆盖计数, 沿用列表)。fail_after 只给测试用：写到第 N 行时模拟中断。

    商品表里成本价或生命周期标记那一格是空的，就保留库里原来的值（页面补录的成本、手动标记不会被冲掉），
    并记进沿用列表，导入结果里逐条说明。
    """
    written = 0

    def tick():
        nonlocal written
        written += 1
        if fail_after is not None and written >= fail_after:
            raise RuntimeError("模拟中断")

    over = {"商品表": 0, "销售日记录": 0, "竞品价格": 0}
    kept: list[dict] = []
    for p in parsed.products:
        p = dict(p)
        old = conn.execute("SELECT cost, cost_source, manual_stage FROM product WHERE sku = ?", (p["sku"],)).fetchone()
        p["cost_source"] = "导入" if p["cost"] is not None else None
        if old:
            over["商品表"] += 1
            if p["cost"] is None and old["cost"] is not None:
                p["cost"], p["cost_source"] = old["cost"], old["cost_source"] or "导入"
                kept.append({"sku": p["sku"], "field": "成本价", "value": old["cost"], "source": p["cost_source"]})
            if not p["manual_stage"] and old["manual_stage"]:
                p["manual_stage"] = old["manual_stage"]
                kept.append({"sku": p["sku"], "field": "生命周期标记", "value": old["manual_stage"], "source": "手动标记"})
        conn.execute("INSERT INTO product(sku, name, category, listed_date, price, cost, stock, manual_stage, cost_source) "
                     "VALUES(:sku, :name, :category, :listed_date, :price, :cost, :stock, :manual_stage, :cost_source) "
                     "ON CONFLICT(sku) DO UPDATE SET name = excluded.name, category = excluded.category, "
                     "listed_date = excluded.listed_date, price = excluded.price, cost = excluded.cost, "
                     "stock = excluded.stock, manual_stage = excluded.manual_stage, cost_source = excluded.cost_source", p)
        tick()
    if parsed.sales:
        existing = {(r["sku"], r["day"]) for r in conn.execute("SELECT sku, day FROM sales_day")}
        over["销售日记录"] = sum(1 for s in parsed.sales if (s["sku"], s["day"]) in existing)
        for s in parsed.sales:
            conn.execute("INSERT INTO sales_day(sku, day, price, units, amount, refunds, visitors, ad_spend, ad_units) "
                         "VALUES(:sku, :day, :price, :units, :amount, :refunds, :visitors, :ad_spend, :ad_units) "
                         "ON CONFLICT(sku, day) DO UPDATE SET price = excluded.price, units = excluded.units, "
                         "amount = excluded.amount, refunds = excluded.refunds, visitors = excluded.visitors, "
                         "ad_spend = excluded.ad_spend, ad_units = excluded.ad_units", s)
            tick()
    for c in parsed.comps:
        if conn.execute("SELECT 1 FROM competitor WHERE sku = ? AND idx = ?", (c["sku"], c["idx"])).fetchone():
            over["竞品价格"] += 1
        conn.execute("INSERT INTO competitor(sku, idx, price, day) VALUES(:sku, :idx, :price, :day) "
                     "ON CONFLICT(sku, idx) DO UPDATE SET price = excluded.price, day = excluded.day", c)
        tick()
    return over, kept


def template_csv(tbl: str) -> bytes:
    buf = io.StringIO()
    csv.writer(buf).writerow(TABLES[tbl]["cols"])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def issues_csv(issues: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["表", "行号", "原因", "级别"])
    for i in issues:
        w.writerow([i["table"], i["line"], i["reason"], "错误" if i["hard"] else "提示"])
    return ("﻿" + buf.getvalue()).encode("utf-8")
