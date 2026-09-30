"""应用层：导入编排、重算、读结果。页面路由只调这里。"""
from __future__ import annotations

import base64
import csv
import io
import json
import secrets
from datetime import date, datetime, timedelta

from . import calc, importer, model, params
from .db import put_setting, tx

EXPORT_COLS = ["商品编号", "商品名称", "类目", "上架天数", "当前价", "成本价", "近30天广告花费", "近30天广告成交件数",
               "单件广告成本", "广告口径", "保本价分母", "保本价", "不含广告保本价", "毛利率", "近30天成交件数",
               "近30天日均", "前30天日均", "库存", "库存天数", "累计成交", "记录天数", "生命周期", "生命周期来源",
               "数据状态", "数据不足原因", "贴近成本线", "贴近成本", "疑似偏高", "疑似偏低", "库存超滞销线",
               "改价事件", "活动日", "有访客数",
               "健康度", "最低建议价", "最优建议价", "最高建议价", "置信度", "采用弹性", "弹性来源", "预计可增毛利",
               "解释", "今天"]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- 读数据 ----------

def load_products(conn) -> list[calc.Product]:
    return [calc.Product(r["sku"], r["name"], r["category"],
                         date.fromisoformat(r["listed_date"]) if r["listed_date"] else None,
                         r["price"], r["cost"], r["stock"], r["manual_stage"])
            for r in conn.execute("SELECT * FROM product ORDER BY sku")]


def load_sales(conn) -> dict[str, dict[date, calc.DayRec]]:
    out: dict[str, dict[date, calc.DayRec]] = {}
    for r in conn.execute("SELECT * FROM sales_day"):
        d = date.fromisoformat(r["day"])
        out.setdefault(r["sku"], {})[d] = calc.DayRec(d, r["price"], r["units"], r["amount"],
                                                       r["visitors"], r["ad_spend"], r["ad_units"])
    return out


def categories(conn) -> list[str]:
    return [r["category"] for r in conn.execute("SELECT DISTINCT category FROM product ORDER BY category")]


def counts(conn) -> dict:
    n = conn.execute("SELECT COUNT(*) FROM product").fetchone()[0]
    days = conn.execute("SELECT COUNT(DISTINCT day) FROM sales_day").fetchone()[0]
    rows = conn.execute("SELECT COUNT(*) FROM sales_day").fetchone()[0]
    return {"商品": n, "天": days, "销售行": rows}


# ---------- 重算 ----------

def load_comps(conn) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for r in conn.execute("SELECT sku, price FROM competitor ORDER BY sku, idx"):
        out.setdefault(r["sku"], []).append(r["price"])
    return out


OVERRIDE_KEYS = ["扣点率", "单件履约费", "退货率", "单次退货净损失占比", "最低毛利率", "尾货期最低毛利率", "目标清仓天数", "弹性微调"]
IGNORE_REASONS = ["价格已定", "不认同", "其他"]


def overrides(conn) -> dict[str, dict]:
    return {r["sku"]: json.loads(r["data"]) for r in conn.execute("SELECT * FROM sku_override")}


def compute(conn) -> tuple:
    """全店计算：基础量、改价事件与类目校准、每个 SKU 的建议。返回 (今天, 结果行, 事件, 校准, 输入指纹)。

    单品输入面板的改动存在 sku_override，只作用于这一个 SKU。"""
    import hashlib
    p = params.effective(conn)
    prods, sales = load_products(conn), load_sales(conn)
    days = [d for recs in sales.values() for d in recs]
    if not days:
        return None, [], [], {}, {}
    today = max(days)
    ov = overrides(conn)
    events = []
    for prod in prods:
        recs = sales.get(prod.sku, {})
        acts = calc.activity_days(recs, p)
        for d in calc.price_events(recs, p):
            events.append({"商品编号": prod.sku, "类目": prod.category, **calc.event_elasticity(recs, d, acts, p)})
    calib = calc.calibrate(events)
    el_rows = {r["类目"]: r for r in params.category_elasticities(conn, sorted({pr.category for pr in prods}))}
    comps = load_comps(conn)
    rows, fps = [], {}
    for prod in prods:
        o = ov.get(prod.sku, {})
        p_sku = {**p, **{k: v for k, v in o.items() if k != "弹性微调"}}
        recs = sales.get(prod.sku, {})
        q = calc.base_quantities(prod, recs, today, p_sku)
        row = dict(el_rows[prod.category])
        if "弹性微调" in o:
            row["微调"] = o["弹性微调"]
        el = model.resolve_elasticity(prod.category, calib, row, p_sku)
        q["建议"] = model.suggest(q, el, comps.get(prod.sku, []), p_sku)
        q["单品覆盖"] = o
        rows.append(q)
        basis = {"p": p_sku, "el": row, "calib": calib.get(prod.category), "prod": list(prod.__dict__.values()),
                 "sales": [list(r.__dict__.values()) for _, r in sorted(recs.items())], "comps": comps.get(prod.sku, []),
                 "today": today}
        fps[prod.sku] = hashlib.sha1(json.dumps(basis, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    return today, rows, events, calib, fps


def recompute(conn, reason: str) -> int | None:
    """重算一版。输入没变的 SKU 沿用上一版的状态（已查看、已采纳、已忽略）；输入变了的 SKU 上一版标「已过期」、
    本版为「新建」。7 天内忽略过同向建议的 SKU，新建议直接记「已忽略」（R-07、R-09）。调用方负责事务。"""
    today, rows, events, calib, fps = compute(conn)
    if today is None and not rows:
        return None
    prev = current_run(conn)
    old = {}
    if prev:
        old = {r["sku"]: (r["status"], r["fp"], r["note"]) for r in
               conn.execute("SELECT sku, status, fp, note FROM sku_result WHERE run_id = ?", (prev["id"],))}
    cur = conn.execute("INSERT INTO calc_run(at, reason, as_of, extra) VALUES(?,?,?,?)",
                       (now(), reason, today.isoformat() if today else None,
                        json.dumps({"events": events, "calibration": calib}, ensure_ascii=False)))
    run_id = cur.lastrowid
    p = params.effective(conn)
    ins = []
    for r in rows:
        sku, fp = r["商品编号"], fps[r["商品编号"]]
        st, note = "新建", None
        if sku in old and old[sku][1] == fp and old[sku][0] != "已过期":
            st, note = old[sku][0], old[sku][2]
            conn.execute("UPDATE sku_result SET status = '已沿用' WHERE run_id = ? AND sku = ?", (prev["id"], sku))
        else:
            if sku in old:
                conn.execute("UPDATE sku_result SET status = '已过期' WHERE run_id = ? AND sku = ?", (prev["id"], sku))
            d = model.direction(r["建议"])
            last = conn.execute("SELECT * FROM ignore_log WHERE sku = ? ORDER BY id DESC LIMIT 1", (sku,)).fetchone()
            if d and last and last["direction"] == d and \
                    datetime.now() - datetime.fromisoformat(last["at"]) < timedelta(days=p["忽略冷却天数"]):
                st, note = "已忽略", f"{last['at'][:10]} 忽略过同向（{d}）建议，{p['忽略冷却天数']} 天内不再提示"
        ins.append((run_id, sku, st, json.dumps(r, ensure_ascii=False), fp, note))
    conn.executemany("INSERT INTO sku_result(run_id, sku, status, data, fp, note) VALUES(?,?,?,?,?,?)", ins)
    return run_id


def current_run(conn):
    return conn.execute("SELECT * FROM calc_run ORDER BY id DESC LIMIT 1").fetchone()


def results(conn) -> list[dict]:
    run = current_run(conn)
    if not run:
        return []
    out = []
    for r in conn.execute("SELECT data, status, note FROM sku_result WHERE run_id = ? ORDER BY sku", (run["id"],)):
        d = json.loads(r["data"])
        d["状态"], d["状态说明"] = r["status"], r["note"]
        out.append(d)
    return out


def run_status(conn) -> dict:
    run = current_run(conn)
    if not run:
        return {}
    live = conn.execute("SELECT COUNT(*) FROM sku_result WHERE run_id = ?", (run["id"],)).fetchone()[0]
    prev = conn.execute("SELECT id FROM calc_run WHERE id < ? ORDER BY id DESC LIMIT 1", (run["id"],)).fetchone()
    prev_n = conn.execute("SELECT COUNT(*) FROM sku_result WHERE run_id = ? AND status = '已过期'", (prev["id"],)).fetchone()[0] if prev else 0
    return {"版本": run["id"], "时间": run["at"], "原因": run["reason"], "今天": run["as_of"], "本版": live,
            "上一版": prev["id"] if prev else None, "上一版过期": prev_n}


def insufficient_count(conn) -> int:
    return sum(1 for r in results(conn) if r["数据状态"] != "正常")


def export_csv(conn) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_COLS)
    for r in results(conn):
        row = dict(r)
        row["库存天数"] = f"{r['库存天数']:.2f}" if r["库存天数"] is not None else "无动销"
        row["毛利率"] = f"{r['毛利率']:.2%}" if r["毛利率"] is not None else ""
        row["近30天日均"] = f"{r['近30天日均']:.2f}"
        row["前30天日均"] = f"{r['前30天日均']:.2f}"
        row["改价事件"] = "；".join(f"{e['日期']} {e['旧价']:g}→{e['新价']:g}" for e in r["改价事件"])
        row["活动日"] = "；".join(r["活动日"])
        for k in ("贴近成本", "疑似偏高", "疑似偏低", "库存超滞销线", "有访客数"):
            row[k] = "是" if r.get(k) else ""
        s = r.get("建议") or {}
        rng = s.get("区间")
        row["健康度"] = s.get("健康度", "")
        row["最低建议价"], row["最优建议价"], row["最高建议价"] = (f"{v:.2f}" for v in rng) if rng else ("", "", "")
        row["置信度"] = s.get("置信度") or ""
        el = s.get("弹性") or {}
        row["采用弹性"] = f"{el['生效']:.4f}" if el else ""
        row["弹性来源"] = el.get("来源", "")
        row["预计可增毛利"] = f"{s['可增毛利']:.2f}" if s.get("可增毛利") is not None else ""
        row["解释"] = "".join(s.get("解释") or [])
        w.writerow(["" if row[c] is None else row[c] for c in EXPORT_COLS])
    return ("﻿" + buf.getvalue()).encode("utf-8")


# ---------- 导入 ----------

def stash(conn, files: dict[str, bytes]) -> str:
    """列名需要确认时先把文件暂存，确认后再导。"""
    token = secrets.token_urlsafe(12)
    payload = {k: base64.b64encode(v).decode() for k, v in files.items()}
    conn.execute("DELETE FROM pending_upload WHERE created < datetime('now', '-1 day')")
    conn.execute("INSERT INTO pending_upload(token, created, payload) VALUES(?,?,?)", (token, now(), json.dumps(payload)))
    return token


def unstash(conn, token: str) -> dict[str, bytes] | None:
    row = conn.execute("SELECT payload FROM pending_upload WHERE token = ?", (token,)).fetchone()
    if not row:
        return None
    return {k: base64.b64decode(v) for k, v in json.loads(row["payload"]).items()}


def remembered_mapping(conn, tbl, head):
    row = conn.execute("SELECT mapping FROM column_mapping WHERE tbl = ? AND signature = ?",
                       (tbl, importer.signature(head))).fetchone()
    return json.loads(row["mapping"]) if row else None


def remember_mapping(conn, tbl, head, mapping):
    conn.execute("INSERT INTO column_mapping(tbl, signature, mapping) VALUES(?,?,?) "
                 "ON CONFLICT(tbl, signature) DO UPDATE SET mapping = excluded.mapping",
                 (tbl, importer.signature(head), json.dumps(mapping, ensure_ascii=False)))


def run_import(conn, files: dict[str, bytes], confirmed: dict[str, dict] | None = None, fail_after: int | None = None) -> dict:
    """返回 {"status": ok / mapping / rejected / invalid, ...}。

    confirmed：店主在映射页确认过的 {表: {文件列: 模板列}}，确认后记住。
    """
    confirmed = confirmed or {}
    parsed_files, need_map, rejects = {}, {}, []
    for tbl, raw in files.items():
        try:
            head, body = importer.read_csv(raw)
        except importer.Rejected as e:
            rejects.append({"table": tbl, "line": 1, "reason": str(e), "hard": True})
            continue
        if tbl in confirmed:
            chk = importer.check_header(tbl, head, confirmed[tbl])
        else:
            chk = importer.check_header(tbl, head, remembered_mapping(conn, tbl, head))
        if chk.status == "not_template":
            rejects.append({"table": tbl, "line": 1, "reason": f"不是{tbl}模板格式，请先下载模板，按模板列名填写", "hard": True})
        elif chk.status == "missing":
            rejects.append({"table": tbl, "line": 1, "reason": f"缺少列：{'、'.join(chk.missing)}", "hard": True})
        elif chk.status == "mapping":
            need_map[tbl] = chk
        else:
            parsed_files[tbl] = (head, body, chk.mapping)
    if rejects:
        with tx(conn):
            conn.execute("INSERT INTO import_log(at, status, files, detail) VALUES(?,?,?,?)",
                         (now(), "拒收", json.dumps(list(files), ensure_ascii=False),
                          json.dumps({"issues": rejects, "rows": 0}, ensure_ascii=False)))
            log_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return {"status": "rejected", "issues": rejects, "log_id": log_id}
    if need_map:
        return {"status": "mapping", "token": stash(conn, files),
                "tables": {t: {"head": c.head, "guess": c.mapping, "missing": c.missing,
                               "cols": importer.TABLES[t]["cols"]} for t, c in need_map.items()}}
    known = {r["sku"] for r in conn.execute("SELECT sku FROM product")}
    parsed = importer.validate(parsed_files, known)
    issues = [vars(i) for i in parsed.issues]
    if parsed.errors:
        with tx(conn):
            conn.execute("INSERT INTO import_log(at, status, files, detail) VALUES(?,?,?,?)",
                         (now(), "拒收", json.dumps(list(files), ensure_ascii=False),
                          json.dumps({"issues": issues, "rows": parsed.rows_checked}, ensure_ascii=False)))
            log_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return {"status": "invalid", "issues": issues, "rows": parsed.rows_checked, "log_id": log_id}
    first = not params.settings_confirmed(conn)
    with tx(conn):
        over, kept = importer.write(conn, parsed, fail_after=fail_after)
        kept_cost = {k["sku"] for k in kept if k["field"] == "成本价"}
        issues = [i for i in issues if not (not i["hard"] and i["table"] == "商品表"
                                            and i["reason"].split(" ", 1)[0] in kept_cost and "成本价缺失" in i["reason"])]
        for tbl, m in confirmed.items():
            if tbl in parsed_files:
                remember_mapping(conn, tbl, parsed_files[tbl][0], m)
        run_id = recompute(conn, "导入")
        c = counts(conn)
        k = insufficient_count(conn)
        detail = {"商品": c["商品"], "天": c["天"], "数据不足": k, "覆盖": over, "rows": parsed.rows_checked,
                  "新增": {"商品表": len(parsed.products) - over["商品表"], "销售日记录": len(parsed.sales) - over["销售日记录"],
                         "竞品价格": len(parsed.comps) - over["竞品价格"]}, "issues": issues, "run": run_id,
                  "沿用": kept}
        conn.execute("INSERT INTO import_log(at, status, files, detail) VALUES(?,?,?,?)",
                     (now(), "成功", json.dumps(list(files), ensure_ascii=False), json.dumps(detail, ensure_ascii=False)))
        log_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {"status": "ok", "log_id": log_id, "first": first, **detail}


def import_log(conn, log_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM import_log WHERE id = ?", (log_id,)).fetchone()
    if not row:
        return None
    return {"id": row["id"], "at": row["at"], "status": row["status"], **json.loads(row["detail"])}


def banner(detail: dict) -> str:
    s = f"已导入 {detail['商品']} 个商品、{detail['天']} 天记录，其中 {detail['数据不足']} 个数据不足"
    over = sum(detail["覆盖"].values())
    if over:
        parts = [f"{t} {n} 行" for t, n in detail["覆盖"].items() if n]
        s += f"；覆盖 {over} 行（{'、'.join(parts)}）"
    return s


def kept_lines(detail: dict) -> list[str]:
    """导入时空格保留原值的逐条说明，例如「S07 成本价格为空，沿用页面补录的 ¥10.00」。"""
    out = []
    for k in detail.get("沿用", []):
        if k["field"] == "成本价":
            src = "页面补录" if k["source"] == "页面补录" else "上次导入"
            out.append(f"{k['sku']} 成本价格为空，沿用{src}的 ¥{k['value']:.2f}")
        else:
            out.append(f"{k['sku']} 生命周期标记格为空，沿用手动标记的{k['value']}")
    return out


# ---------- 店主在页面上改数据 ----------

def set_cost(conn, sku: str, raw: str) -> str | None:
    try:
        v = importer.parse_num(raw)
    except ValueError:
        return "成本价请填数字"
    if v is None or v < 0:
        return "成本价不能为空或负数"
    with tx(conn):
        conn.execute("UPDATE product SET cost = ?, cost_source = '页面补录' WHERE sku = ?", (v, sku))
        recompute(conn, f"补录成本 {sku}")
    return None


def set_stage(conn, sku: str, stage: str) -> str | None:
    if stage not in ("", *calc.STAGES):
        return "生命周期只能是新品期、成熟期、尾货期或自动"
    with tx(conn):
        conn.execute("UPDATE product SET manual_stage = ? WHERE sku = ?", (stage or None, sku))
        recompute(conn, f"生命周期标记 {sku}")
    return None


def save_settings(conn, form: dict) -> tuple[list[str], list[tuple]]:
    p = params.effective(conn)
    new, errs = params.parse_settings(form, p)
    cats = elastic_categories(conn)
    el, errs2 = params.parse_elasticity(form, cats, p)
    errs += errs2
    if errs:
        return errs, []
    with tx(conn):
        changes = params.save_settings(conn, new, el)
        put_setting(conn, "settings_confirmed", True)
        if changes and conn.execute("SELECT COUNT(*) FROM product").fetchone()[0]:
            recompute(conn, "设置变更")
    return [], changes


def elastic_categories(conn) -> list[str]:
    return categories(conn)


# ---------- PT-05 单品建议与试算 ----------

def run_extra(conn) -> dict:
    run = current_run(conn)
    if not run or not run["extra"]:
        return {"events": [], "calibration": {}}
    return json.loads(run["extra"])


def sku_result(conn, sku: str) -> dict | None:
    for r in results(conn):
        if r["商品编号"] == sku:
            return r
    return None


def estimate(conn, sku: str, price: float) -> dict | None:
    """任意价格的预估（滑块用），与当前价对比六行。低于候选下限时标出来，但照样给数字。"""
    r = sku_result(conn, sku)
    if not r or not (r.get("建议") or {}).get("区间"):
        return None
    s, p = r["建议"], params.effective(conn)
    est = model.Estimator(r, s["弹性"]["生效"], p)
    cur, new = est.at(r["当前价"]), est.at(price)
    return {"价格": price, "低于下限": price < s["候选下限"] - 1e-9, "下限": s["候选下限"], "上限": s["候选上限"],
            "行": model.estimate_rows(cur, new, s["预估"]["无访客数"], s["预估"]["未计曝光"])}


# ---------- PT-06 模拟推演与改价事件 ----------

SIM_COLS = ["日期", "商品编号", "真实价", "建议价", "真实销量", "模拟销量", "真实毛利", "模拟毛利", "活动日", "标记"]


def simulation_csv(conn) -> bytes:
    """PRD 12.3 模拟推演（C-07）：建议价取本版最优价，历史库存不可得，不按日重算建议。
    模拟销量 = 真实销量 ×（建议价 ÷ 真实价）的弹性次方；每行标「模拟」，不进任何页面指标。"""
    p = params.effective(conn)
    keep = 1 - p["扣点率"] - p["退货率"] * p["单次退货净损失占比"]
    sales = load_sales(conn)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(SIM_COLS)
    for r in results(conn):
        s = r.get("建议") or {}
        if not s.get("区间"):
            continue
        e, sug = s["弹性"]["生效"], s["最优价"]
        recs = sales.get(r["商品编号"], {})
        acts = set(r["活动日"])
        fixed = r["成本价"] + p["单件履约费"]
        unit_margin = sug * keep - fixed - model.unit_ad_raw(r)
        for d in sorted(recs):
            x = recs[d]
            sim_units = x.units * (sug / x.price) ** e
            real_gm = x.amount * keep - x.units * fixed - (x.ad_spend or 0)
            w.writerow([d.isoformat(), r["商品编号"], f"{x.price:.2f}", f"{sug:.2f}", f"{x.units:g}", f"{sim_units:.2f}",
                        f"{real_gm:.2f}", f"{unit_margin * sim_units:.2f}", "是" if d.isoformat() in acts else "", "模拟"])
    return ("﻿" + buf.getvalue()).encode("utf-8")


EV_COLS = ["商品编号", "类目", "改价日", "旧价", "新价", "口径", "改价前", "改价后", "弹性", "访客倍数", "剔除活动日", "备注"]


def events_csv(conn) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EV_COLS)
    for e in run_extra(conn)["events"]:
        w.writerow(["" if e.get(c) is None else (f"{e[c]:.4f}" if isinstance(e[c], float) and c not in ("旧价", "新价") else e[c])
                    for c in EV_COLS])
    return ("﻿" + buf.getvalue()).encode("utf-8")


# ---------- PT-07 体检报告 ----------

def report_metrics(conn) -> dict:
    """体检指标（MRD 2.2、PRD 12.1、验收条例附录 A.4），口径同门 0：C-01 无动销单列，C-02 分母 = 非数据不足。"""
    p = params.effective(conn)
    rows = results(conn)
    ok = [r for r in rows if r["数据状态"] == "正常"]
    den = len(ok)

    def ids(pred, pool=ok):
        return [r["商品编号"] for r in pool if pred(r)]

    near, high, low = ids(lambda r: r["贴近成本"]), ids(lambda r: r["疑似偏高"]), ids(lambda r: r["疑似偏低"])
    need = ids(lambda r: r["贴近成本"] or r["疑似偏高"] or r["疑似偏低"])
    over = ids(lambda r: r["库存超滞销线"])
    ev = ids(lambda r: len(r["改价事件"]) > 0)
    vis = ids(lambda r: r["有访客数"], rows)
    ad = ids(lambda r: r["近30天广告花费"] > 0)
    short = ids(lambda r: r["数据状态"] != "正常", rows)
    nosale = ids(lambda r: "无动销" in r["数据不足原因"], rows)
    with_cost = [r for r in rows if r["成本价"] is not None]

    def share(x):
        return len(x) / den if den else None

    L = p["滞销线"]
    metrics = [
        {"指标": "SKU 总数；有成本价的 SKU 数", "算法": "计数", "判据": "有成本价 ≥ 50 个", "结果": f"{len(rows)}；{len(with_cost)}", "明细": ""},
        {"指标": "数据不足（单列，不进分母）", "算法": "记录不足 14 天、缺成本价、无动销", "判据": "参考", "结果": str(len(short)), "明细": "、".join(short)},
        {"指标": "贴近成本 SKU 占比", "算法": f"当前价 ＜ 保本价 × {p['贴近成本系数']}", "判据": "参考", "占比": share(near), "分子": near},
        {"指标": "疑似偏高 SKU 占比", "算法": f"库存天数 ＞ {L} 且毛利率 ＞ {p['疑似偏高毛利率线']:.0%}", "判据": "参考", "占比": share(high), "分子": high},
        {"指标": "疑似偏低 SKU 占比", "算法": f"库存天数 ＜ {p['疑似偏低库存天数线']} 且毛利率 ＜ {p['疑似偏低毛利率线']:.0%}", "判据": "参考", "占比": share(low), "分子": low},
        {"指标": "需要动价 SKU 占比", "算法": "上三项去重合计", "判据": "≥ 30%", "占比": share(need), "分子": need, "达标": share(need) is not None and share(need) >= 0.30},
        {"指标": f"库存超 {L} 天 SKU 占比", "算法": "当前库存 ÷ 近 30 天日均销量", "判据": "≥ 15%", "占比": share(over), "分子": over, "达标": share(over) is not None and share(over) >= 0.15},
        {"指标": "无动销（单列）", "算法": "近 30 天成交 0 件", "判据": "参考", "结果": str(len(nosale)), "明细": "、".join(nosale)},
        {"指标": "近 90 天有改价事件的 SKU 占比", "算法": "当日售价与前 7 天每天的差都 ≥ 1%", "判据": "决定弹性能否校准", "占比": share(ev), "分子": ev},
        {"指标": "是否有按日访客数", "算法": "访客数列非空", "判据": "决定曝光项能否估", "结果": f"有，{len(vis)} 个 SKU" if vis else "否", "明细": "、".join(vis)},
        {"指标": "近 30 天有广告花费的 SKU 占比", "算法": "近 30 天广告花费 ＞ 0", "判据": "决定含广告保本价能否启用", "占比": share(ad), "分子": ad},
    ]
    for m in metrics:
        if "占比" in m:
            m["结果"] = "—" if m["占比"] is None else f"{m['占比']:.1%}"
            m["明细"] = f"{len(m['分子'])} ÷ {den}" + (f"：{'、'.join(m['分子'])}" if m["分子"] else "")

    health = {k: 0 for k in (model.SHORT, model.NEAR, model.LOW, model.HIGH, model.OK)}
    for r in rows:
        h = (r.get("建议") or {}).get("健康度")
        if h in health:
            health[h] += 1
    bins = {"30 天以内": 0, "30～60 天": 0, "60～90 天": 0, "90 天以上": 0, "无动销": len(nosale)}
    for r in ok:
        d = r["库存天数"]
        key = "30 天以内" if d < 30 else "30～60 天" if d < 60 else "60～90 天" if d <= 90 else "90 天以上"
        bins[key] += 1
    extra = run_extra(conn)
    cats = sorted({r["类目"] for r in rows})
    el_rows = {e["类目"]: e for e in params.category_elasticities(conn, cats)}
    cat_el = []
    for c in cats:
        el = model.resolve_elasticity(c, extra["calibration"], el_rows[c], p)
        cal = extra["calibration"].get(c, {})
        cat_el.append({"类目": c, "有效事件数": cal.get("有效事件数", 0), "事件中位数": cal.get("事件中位数"),
                       "校准误差": cal.get("校准误差"), "先验": el_rows[c]["先验"], "生效": el["生效"], "来源": el["来源"]})
    return {"指标": metrics, "分母": den, "健康度": health, "库存天数": bins, "类目弹性": cat_el,
            "改价事件": len(extra["events"]), "有事件的SKU": len(ev), "校准": extra["calibration"],
            "事件": extra["events"]}


# ---------- PT-08 健康度列表 ----------

HEALTH_ORDER = ["贴近成本", "偏低", "偏高", "合理", "数据不足"]
SORTS = {"gain": "贴近成本置顶 · 预计可增毛利", "stock": "库存天数从多到少", "margin": "毛利率从低到高"}


def list_view(conn, f: str = "", sort: str = "gain") -> dict:
    rows = results(conn)
    health = lambda r: (r.get("建议") or {}).get("健康度") or "数据不足"
    counts = {h: sum(1 for r in rows if health(r) == h) for h in HEALTH_ORDER}
    shown = [r for r in rows if not f or health(r) == f]
    if sort == "stock":
        key = lambda r: (r["库存天数"] is None, -(r["库存天数"] or 0), r["商品编号"])
    elif sort == "margin":
        key = lambda r: (r["毛利率"] is None, r["毛利率"] if r["毛利率"] is not None else 0, r["商品编号"])
    else:
        sort = "gain"
        key = lambda r: (0 if health(r) == "贴近成本" else 2 if health(r) == "数据不足" else 1,
                         -((r.get("建议") or {}).get("可增毛利") or 0), r["商品编号"])
    shown.sort(key=key)
    gains = [(r.get("建议") or {}).get("可增毛利") for r in rows]
    gains = [g for g in gains if g is not None]
    return {
        "rows": shown, "counts": counts, "total": len(rows), "f": f, "sort": sort,
        "短缺": counts["数据不足"],
        "需要动": {k: counts[k] for k in ("贴近成本", "偏低", "偏高")},
        "可增毛利": sum(gains), "可增毛利为负": sum(1 for g in gains if g < 0),
        "有改价事件": sum(1 for r in rows if r["改价事件"]),
    }


# ---------- PT-09 单品输入面板与曲线 ----------

def mark_viewed(conn, sku: str):
    run = current_run(conn)
    if not run:
        return
    row = conn.execute("SELECT status FROM sku_result WHERE run_id = ? AND sku = ?", (run["id"], sku)).fetchone()
    with tx(conn):
        conn.execute("INSERT INTO view_log(sku, run_id, at) VALUES(?,?,?)", (sku, run["id"], now()))
        if row and row["status"] == "新建":
            conn.execute("UPDATE sku_result SET status = '已查看' WHERE run_id = ? AND sku = ?", (run["id"], sku))


def input_values(conn, sku: str) -> dict:
    """输入面板的当前值：单品覆盖优先，没有就用店铺默认。百分比按 0～100 显示。"""
    p = params.effective(conn)
    o = overrides(conn).get(sku, {})
    prod = conn.execute("SELECT * FROM product WHERE sku = ?", (sku,)).fetchone()
    cats = {e["类目"]: e for e in params.category_elasticities(conn, [prod["category"]])}
    comps = load_comps(conn).get(sku, [])
    v = {k: o.get(k, p.get(k)) for k in OVERRIDE_KEYS if k != "弹性微调"}
    out = {k: (f"{v[k] * 100:g}" if k in ("扣点率", "退货率", "单次退货净损失占比", "最低毛利率", "尾货期最低毛利率") else f"{v[k]:g}")
           for k in v}
    out["弹性微调"] = f"{o.get('弹性微调', cats[prod['category']]['微调']) * 100:g}"
    out["成本价"] = "" if prod["cost"] is None else f"{prod['cost']:g}"
    out["生命周期"] = prod["manual_stage"] or ""
    out["竞品"] = [f"{x:g}" for x in comps] + [""] * (5 - len(comps))
    out["覆盖"] = sorted(o)
    return out


def save_inputs(conn, sku: str, form: dict) -> list[str]:
    """输入面板保存：与店铺默认相同的项不存覆盖，保证店铺设置改了还能生效。改动只让这个 SKU 的建议过期。"""
    p = params.effective(conn)
    errs, o = [], {}
    pct = ("扣点率", "退货率", "单次退货净损失占比", "最低毛利率", "尾货期最低毛利率")
    for k in OVERRIDE_KEYS:
        raw = (form.get(k) or "").strip()
        if raw == "":
            continue
        try:
            v = float(raw.replace("%", ""))
        except ValueError:
            errs.append(f"{k}：请填数字")
            continue
        if k in pct:
            if not 0 <= v <= 100:
                errs.append(f"{k}：合法范围 0% 到 100%")
                continue
            v = round(v / 100, 6)
        elif k == "单件履约费":
            if v < 0:
                errs.append("单件履约费：不能为负数")
                continue
        elif k == "目标清仓天数":
            if v <= 0 or v != int(v):
                errs.append("目标清仓天数：请填正整数")
                continue
            v = int(v)
        elif k == "弹性微调":
            if abs(v) > p["弹性微调上限"] * 100 + 1e-9:
                errs.append(f"弹性微调：合法范围 ±{p['弹性微调上限']:.0%}")
                continue
            v = round(v / 100, 6)
        o[k] = v
    fee = o.get("扣点率", p["扣点率"])
    loss = o.get("退货率", p["退货率"]) * o.get("单次退货净损失占比", p["单次退货净损失占比"])
    if fee + loss >= 1:
        errs.append("扣点率与退货损耗合计不能达到 100%")
    cost_raw = (form.get("成本价") or "").strip()
    cost = None
    if cost_raw:
        try:
            cost = float(cost_raw.replace("¥", ""))
            if cost < 0:
                raise ValueError
        except ValueError:
            errs.append("成本价：请填不小于 0 的数字")
    stage = (form.get("生命周期") or "").strip()
    if stage not in ("", *calc.STAGES):
        errs.append("生命周期只能是自动、新品期、成熟期、尾货期")
    comps = []
    for i in range(1, 6):
        raw = (form.get(f"竞品{i}") or "").strip()
        if raw:
            try:
                x = float(raw.replace("¥", ""))
                if x <= 0:
                    raise ValueError
                comps.append(x)
            except ValueError:
                errs.append(f"竞品价 {i}：请填大于 0 的数字")
    if errs:
        return errs
    prod = conn.execute("SELECT category FROM product WHERE sku = ?", (sku,)).fetchone()
    cat_adj = {e["类目"]: e for e in params.category_elasticities(conn, [prod["category"]])}[prod["category"]]["微调"]
    defaults = {k: p[k] for k in OVERRIDE_KEYS if k != "弹性微调"} | {"弹性微调": cat_adj}
    o = {k: v for k, v in o.items() if abs(v - defaults[k]) > 1e-9}
    with tx(conn):
        if o:
            conn.execute("INSERT INTO sku_override(sku, data) VALUES(?, ?) ON CONFLICT(sku) DO UPDATE SET data = excluded.data",
                         (sku, json.dumps(o, ensure_ascii=False)))
        else:
            conn.execute("DELETE FROM sku_override WHERE sku = ?", (sku,))
        if cost is not None:
            old = conn.execute("SELECT cost FROM product WHERE sku = ?", (sku,)).fetchone()["cost"]
            if old is None or abs(old - cost) > 1e-9:
                conn.execute("UPDATE product SET cost = ?, cost_source = '页面补录' WHERE sku = ?", (cost, sku))
        if "生命周期" in form:  # 表单没带的字段不当成清空
            conn.execute("UPDATE product SET manual_stage = ? WHERE sku = ?", (stage or None, sku))
        old_comps = load_comps(conn).get(sku, [])
        if any(f"竞品{i}" in form for i in range(1, 6)) and comps != old_comps:
            conn.execute("DELETE FROM competitor WHERE sku = ?", (sku,))
            day = current_run(conn)["as_of"] if current_run(conn) else None
            conn.executemany("INSERT INTO competitor(sku, idx, price, day) VALUES(?,?,?,?)",
                             [(sku, i + 1, x, day) for i, x in enumerate(comps)])
        recompute(conn, f"单品输入 {sku}")
    return []


def reset_inputs(conn, sku: str):
    with tx(conn):
        conn.execute("DELETE FROM sku_override WHERE sku = ?", (sku,))
        recompute(conn, f"单品恢复店铺默认 {sku}")


def curve(r: dict, p: dict, w: int = 420, h: int = 220) -> dict | None:
    """日毛利随价格变化的曲线（单品页示意图）。坐标算好交给模板画 SVG，只用 PRD 8.2 的颜色。"""
    s = r.get("建议") or {}
    if not s.get("区间"):
        return None
    est = model.Estimator(r, s["弹性"]["生效"], p)
    be = model.breakeven_raw(r, p)
    x0, x1 = min(be * 0.85, s["候选下限"]), max(s["候选上限"], r["当前价"])
    n = 80
    xs = [x0 + (x1 - x0) * i / n for i in range(n + 1)]
    ys = [est.at(x)["日毛利"] for x in xs]
    y0, y1 = min(min(ys), 0), max(ys) * 1.1 or 1
    pad_l, pad_b, pad_t = 44, 26, 18
    X = lambda x: pad_l + (x - x0) / (x1 - x0) * (w - pad_l - 10)
    Y = lambda y: pad_t + (y1 - y) / (y1 - y0) * (h - pad_t - pad_b)
    low, opt, high = s["区间"]
    return {
        "w": w, "h": h, "points": " ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in zip(xs, ys)),
        "be_x": X(be), "zero_y": Y(0), "top": pad_t, "bottom": h - pad_b, "left": pad_l, "right": w - 10,
        "band": (X(low), X(high)), "opt_x": X(opt), "cur_x": X(r["当前价"]),
        "labels": {"x0": x0, "x1": x1, "be": be, "opt": opt, "cur": r["当前价"], "ymax": y1},
    }


# ---------- PT-10 采纳、忽略 ----------

def adopt(conn, sku: str, raw: str) -> tuple[str | None, dict | None]:
    r = sku_result(conn, sku)
    s = (r or {}).get("建议") or {}
    if not s.get("区间"):
        return "这个商品没有建议区间，不能采纳", None
    try:
        price = float(str(raw).replace("¥", "").strip())
    except ValueError:
        return "采纳价请填数字", None
    low, opt, high = s["区间"]
    if price < low - 1e-9:
        return f"采纳价 ¥{price:.2f} 低于最低建议价 ¥{low:.2f}，不能采纳（最低建议价已含保本线）", None
    run = current_run(conn)
    rec = {"sku": sku, "run_id": run["id"], "at": now(), "as_of": run["as_of"], "price": price,
           "current_price": r["当前价"], "low": low, "opt": opt, "high": high, "out_of_range": int(price > high + 1e-9)}
    with tx(conn):
        conn.execute("INSERT INTO adoption(sku, run_id, at, as_of, price, current_price, low, opt, high, out_of_range) "
                     "VALUES(:sku, :run_id, :at, :as_of, :price, :current_price, :low, :opt, :high, :out_of_range)", rec)
        conn.execute("UPDATE sku_result SET status = '已采纳', note = ? WHERE run_id = ? AND sku = ?",
                     (f"{rec['at'][:16].replace('T', ' ')} 采纳 ¥{price:.2f}" + ("，区间外采纳" if rec["out_of_range"] else ""),
                      run["id"], sku))
    return None, rec


def ignore(conn, sku: str, reason: str) -> str | None:
    if reason not in IGNORE_REASONS:
        return "请选一个忽略原因：价格已定、不认同、其他"
    r = sku_result(conn, sku)
    s = (r or {}).get("建议") or {}
    d = model.direction(s)
    if not d:
        return "这个商品没有建议，无需忽略"
    run = current_run(conn)
    at = now()
    with tx(conn):
        conn.execute("INSERT INTO ignore_log(sku, run_id, at, reason, direction) VALUES(?,?,?,?,?)", (sku, run["id"], at, reason, d))
        conn.execute("UPDATE sku_result SET status = '已忽略', note = ? WHERE run_id = ? AND sku = ?",
                     (f"{at[:10]} 忽略（{reason}），{params.effective(conn)['忽略冷却天数']} 天内不再提示同向（{d}）建议", run["id"], sku))
    return None


def adoptions(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM adoption ORDER BY id")]


def adoptions_csv(conn) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["商品编号", "采纳价", "采纳时间", "是否区间外", "采纳时当前价", "最低建议价", "最优建议价", "最高建议价", "数据截至", "计算版本"])
    for a in adoptions(conn):
        w.writerow([a["sku"], f"{a['price']:.2f}", a["at"].replace("T", " "), "是" if a["out_of_range"] else "否",
                    f"{a['current_price']:.2f}", f"{a['low']:.2f}", f"{a['opt']:.2f}", f"{a['high']:.2f}", a["as_of"], a["run_id"]])
    return ("﻿" + buf.getvalue()).encode("utf-8")


# ---------- PT-12 门 1 评估导出 ----------

def _window_margin(recs, days, cost, p, acts, need=10):
    use = [recs[d] for d in days if d in recs and d not in acts]
    amount = sum(x.amount for x in use)
    if len(use) < need or amount <= 0:
        return None
    keep = 1 - p["扣点率"] - p["退货率"] * p["单次退货净损失占比"]
    gm = sum(x.amount * keep - x.units * (cost + p["单件履约费"]) - (x.ad_spend or 0) for x in use)
    return gm / amount


def gate1(conn) -> dict:
    """PRD 第 3 章门 1 指标：采纳率 = 采纳的 SKU ÷ 查看过的 SKU；每个采纳 SKU 比采纳前后 14 天毛利率，
    再减对照 SKU 同期变化。对照 = 同店未采纳、同类目、库存天数最接近。窗口剔除活动日，有效天数少于 10 天记「记录不足」。
    锚定日 = 采纳时那一版数据的最后一天（采纳后的记录要等下次导入）。"""
    p = params.effective(conn)
    sales = load_sales(conn)
    res = {r["商品编号"]: r for r in results(conn)}
    viewed = {r["sku"] for r in conn.execute("SELECT DISTINCT sku FROM view_log")}
    latest = {}
    for a in adoptions(conn):
        latest[a["sku"]] = a
    adopted = set(latest)
    rows = []
    for sku, a in sorted(latest.items()):
        r = res.get(sku)
        anchor = date.fromisoformat(a["as_of"])
        pre = [anchor - timedelta(days=i) for i in range(13, -1, -1)]
        post = [anchor + timedelta(days=i) for i in range(1, 15)]
        recs = sales.get(sku, {})
        acts = calc.activity_days(recs, p)
        cost = (r or {}).get("成本价") or 0
        m0, m1 = _window_margin(recs, pre, cost, p, acts), _window_margin(recs, post, cost, p, acts)
        ctrl = None
        if r:
            cands = [x for x in res.values() if x["类目"] == r["类目"] and x["商品编号"] not in adopted
                     and x["数据状态"] == "正常" and x["库存天数"] is not None]
            if cands:
                ctrl = min(cands, key=lambda x: (abs(x["库存天数"] - (r["库存天数"] or 0)), x["商品编号"]))
        c0 = c1 = None
        if ctrl:
            crecs = sales.get(ctrl["商品编号"], {})
            cacts = calc.activity_days(crecs, p)
            c0 = _window_margin(crecs, pre, ctrl["成本价"], p, cacts)
            c1 = _window_margin(crecs, post, ctrl["成本价"], p, cacts)
        d_self = m1 - m0 if None not in (m0, m1) else None
        d_ctrl = c1 - c0 if None not in (c0, c1) else None
        diff = d_self - d_ctrl if None not in (d_self, d_ctrl) else None
        note = []
        if m1 is None:
            note.append("采纳后 14 天记录不足，下次导入后再看")
        if not ctrl:
            note.append("同类目没有未采纳的正常 SKU 可作对照")
        rows.append({"商品编号": sku, "采纳价": a["price"], "采纳时间": a["at"], "锚定日": a["as_of"],
                     "对照": ctrl["商品编号"] if ctrl else None,
                     "对照库存天数": ctrl["库存天数"] if ctrl else None, "本品库存天数": (r or {}).get("库存天数"),
                     "采纳前": m0, "采纳后": m1, "本品变化": d_self, "对照前": c0, "对照后": c1, "对照变化": d_ctrl,
                     "差值": diff, "说明": "；".join(note)})
    return {"查看": len(viewed), "采纳": len(adopted), "采纳率": len(adopted) / len(viewed) if viewed else None, "rows": rows}


def gate1_csv(conn) -> bytes:
    g = gate1(conn)
    buf = io.StringIO()
    w = csv.writer(buf)
    rate = "—" if g["采纳率"] is None else f"{g['采纳率']:.1%}"
    w.writerow(["采纳率", f"{g['采纳']} ÷ {g['查看']} = {rate}", "按 SKU 计：采纳过的 SKU ÷ 打开过单品页的 SKU"])
    w.writerow([])
    cols = ["商品编号", "采纳价", "采纳时间", "锚定日", "对照", "本品库存天数", "对照库存天数", "采纳前", "采纳后", "本品变化",
            "对照前", "对照后", "对照变化", "差值", "说明"]
    w.writerow(cols)
    pct = lambda v: "" if v is None else f"{v:.2%}"
    for r in g["rows"]:
        w.writerow([r["商品编号"], f"{r['采纳价']:.2f}", r["采纳时间"].replace("T", " "), r["锚定日"], r["对照"] or "",
                    "" if r["本品库存天数"] is None else f"{r['本品库存天数']:.1f}",
                    "" if r["对照库存天数"] is None else f"{r['对照库存天数']:.1f}",
                    pct(r["采纳前"]), pct(r["采纳后"]), pct(r["本品变化"]), pct(r["对照前"]), pct(r["对照后"]),
                    pct(r["对照变化"]), pct(r["差值"]), r["说明"]])
    return ("﻿" + buf.getvalue()).encode("utf-8")
