"""页面路由。只绑本机 127.0.0.1，运行时不发任何外部请求。"""
from __future__ import annotations

import os
import webbrowser
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import calc, importer, params, service
from .db import connect, get_setting, put_setting, tx

HERE = Path(__file__).resolve().parent
app = FastAPI(title="定价参谋", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


def fmt_money(v, short=False):
    if v is None:
        return "—"
    s = f"¥{abs(v):,.2f}"
    s = s[:-1] if short and s.endswith("0") and "." in s else s
    return ("-" + s) if v < 0 else s


def fmt_pct(v, signed=False, digits=1):
    if v is None:
        return "—"
    return f"{v:+.{digits}%}" if signed else f"{v:.{digits}%}"


def fmt_days(v):
    return "无动销" if v is None else f"{v:.0f} 天" if abs(v - round(v)) < 0.05 else f"{v:.1f} 天"


templates.env.filters.update(money=fmt_money, pct=fmt_pct, days=fmt_days)


def asset_v() -> str:
    """静态文件版本号 = 最后修改时间。页面引用 /static/x?v=…，升级后浏览器不会用旧缓存。"""
    return str(int(max(f.stat().st_mtime for f in (HERE / "static").iterdir())))


templates.env.globals["asset_v"] = asset_v


def conn():
    return connect()


def page(request: Request, name: str, **ctx):
    c = ctx.pop("_conn", None) or conn()
    store = params.effective(c)
    ctx.setdefault("store_name", get_setting(c, "store_name", "我的店铺"))
    ctx.setdefault("has_data", c.execute("SELECT COUNT(*) FROM product").fetchone()[0] > 0)
    ctx["params"] = store
    return templates.TemplateResponse(request, name, ctx)


@app.get("/", response_class=HTMLResponse)
def overview(request: Request, err: str = "", msg: str = "", f: str = "", sort: str = "gain"):
    c = conn()
    if not c.execute("SELECT 1 FROM sku_result LIMIT 1").fetchone():
        return page(request, "empty.html", active="总览", _conn=c)
    v = service.list_view(c, f if f in service.HEALTH_ORDER else "", sort)
    q = request.url.query
    return page(request, "overview.html", active="总览", v=v, rows=v["rows"], stat=service.run_status(c),
                stages=calc.STAGES, sorts=service.SORTS, order=service.HEALTH_ORDER, err=err, msg=msg,
                back=quote("/" + (f"?{q}" if q else ""), safe=""), here="/" + (f"?{q}" if q else ""), _conn=c)


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, log: int | None = None):
    c = conn()
    result = service.import_log(c, log) if log else None
    return page(request, "import.html", active="导入", result=result, _conn=c)


async def _read_uploads(**uploads) -> dict[str, bytes]:
    files = {}
    for tbl, up in uploads.items():
        if up is not None and up.filename:
            data = await up.read()
            if data:
                files[tbl] = data
    return files


def _after_import(request: Request, c, res: dict):
    if res["status"] == "ok":
        if res["first"]:
            return RedirectResponse(f"/settings?first=1&log={res['log_id']}", status_code=303)
        return RedirectResponse(f"/report?log={res['log_id']}", status_code=303)
    if res["status"] == "mapping":
        return page(request, "mapping.html", active="导入", token=res["token"], tables=res["tables"],
                    ignore=importer.IGNORE, _conn=c)
    return RedirectResponse(f"/import?log={res['log_id']}", status_code=303)


@app.post("/import")
async def do_import(request: Request, products: UploadFile | None = None, sales: UploadFile | None = None,
                    competitors: UploadFile | None = None):
    files = await _read_uploads(**{"商品表": products, "销售日记录": sales, "竞品价格": competitors})
    if not files:
        return RedirectResponse("/import", status_code=303)
    c = conn()
    return _after_import(request, c, service.run_import(c, files))


@app.post("/import/mapping")
async def confirm_mapping(request: Request):
    form = await request.form()
    c = conn()
    files = service.unstash(c, form.get("token", ""))
    if files is None:
        return RedirectResponse("/import", status_code=303)
    confirmed = {}
    for tbl in importer.TABLES:
        cols = [k for k in form.keys() if k.startswith(f"map|{tbl}|")]
        if cols:
            confirmed[tbl] = {k.split("|", 2)[2]: form[k] for k in cols}
    for tbl, raw in files.items():
        if tbl not in confirmed:
            head, _ = importer.read_csv(raw)
            m = service.remembered_mapping(c, tbl, head)
            if m:
                confirmed[tbl] = m
    return _after_import(request, c, service.run_import(c, files, confirmed))


@app.get("/import/issues/{log_id}.csv")
def issues_csv(log_id: int):
    c = conn()
    log = service.import_log(c, log_id)
    data = importer.issues_csv(log["issues"] if log else [])
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(f'错误清单-{log_id}.csv')}"})


@app.get("/templates/{tbl}.csv")
def template(tbl: str):
    if tbl not in importer.TABLES:
        return Response("没有这个模板", status_code=404)
    return Response(importer.template_csv(tbl), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(importer.TEMPLATE_FILES[tbl])}"})


@app.get("/report", response_class=HTMLResponse)
def report(request: Request, log: int | None = None):
    c = conn()
    result = service.import_log(c, log) if log else None
    rows = service.results(c)
    return page(request, "report.html", active="体检报告", result=result,
                banner=service.banner(result) if result and result["status"] == "成功" else None,
                kept=service.kept_lines(result) if result and result["status"] == "成功" else [],
                hints=[i["reason"] for i in result["issues"] if not i["hard"]] if result and result["status"] == "成功" else [],
                n=len(rows), insufficient=sum(1 for r in rows if r["数据状态"] != "正常"),
                cnt=service.counts(c), stat=service.run_status(c),
                m=service.report_metrics(c) if rows else None, _conn=c)


def _settings_ctx(c, form=None):
    p = params.effective(c)
    cats = service.elastic_categories(c)
    el = params.category_elasticities(c, cats)
    values = {}
    for key, label, kind in params.FIELDS:
        v = p[key]
        values[key] = (f"{v * 100:g}" if kind == "pct" else f"{v:g}" if kind in ("money", "int") else v)
    if form:
        values.update({k: form.get(k, values.get(k)) for k, _, _ in params.FIELDS})
        for i, e in enumerate(el):
            e["基准输入"] = form.get(f"el_base_{i}", f"{e['基准']:g}")
            e["微调输入"] = form.get(f"el_adj_{i}", f"{e['微调'] * 100:g}")
    for e in el:
        e.setdefault("基准输入", f"{e['基准']:g}")
        e.setdefault("微调输入", f"{e['微调'] * 100:g}")
    return {"fields": params.FIELDS, "values": values, "elastic": el, "platform_rates": p["平台默认扣点率"],
            "store_name_value": (form or {}).get("store_name") or get_setting(c, "store_name", "我的店铺")}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, first: int = 0, log: int | None = None, saved: int = 0):
    c = conn()
    return page(request, "settings.html", active="设置", first=first, log=log, saved=saved, errors=[],
                **_settings_ctx(c), _conn=c)


@app.post("/settings")
async def settings_save(request: Request):
    form = dict(await request.form())
    c = conn()
    errs, changes = service.save_settings(c, form)
    name = (form.get("store_name") or "").strip()
    if not errs and name and name != get_setting(c, "store_name", "我的店铺"):
        with tx(c):
            put_setting(c, "store_name", name)
    if errs:
        return page(request, "settings.html", active="设置", first=int(form.get("first") or 0), log=form.get("log"),
                    saved=0, errors=errs, **_settings_ctx(c, form), _conn=c)
    if form.get("first") == "1":
        log = form.get("log")
        return RedirectResponse(f"/report?log={log}" if log else "/report", status_code=303)
    return RedirectResponse(f"/settings?saved={len(changes)}", status_code=303)


@app.get("/settings/log", response_class=HTMLResponse)
def settings_log(request: Request):
    c = conn()
    kinds = {k: (label, kind) for k, label, kind in params.FIELDS}

    def show(key, raw):
        import json as _json
        try:
            v = _json.loads(raw)
        except (TypeError, ValueError):
            return raw
        kind = kinds.get(key, (key, ""))[1]
        if kind == "pct" and isinstance(v, (int, float)):
            return f"{v * 100:g}%"
        if kind == "money" and isinstance(v, (int, float)):
            return f"¥{v:g}"
        if kind == "int" and isinstance(v, (int, float)):
            return f"{v:g} 天"
        return v if v is not None else "—"

    rows = [{"at": r["at"], "field": kinds.get(r["field"], (r["field"], ""))[0],
             "old": show(r["field"], r["old"]), "new": show(r["field"], r["new"])}
            for r in c.execute("SELECT * FROM setting_log ORDER BY id DESC")]
    return page(request, "settings_log.html", active="设置", rows=rows, _conn=c)


@app.get("/export/detail.csv")
def export_detail():
    c = conn()
    return Response(service.export_csv(c), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote('计算明细.csv')}"})


def _back(url: str, key: str, text: str) -> str:
    url = url if url.startswith("/") else "/"
    return url + ("&" if "?" in url else "?") + f"{key}={quote(text)}"


@app.post("/sku/{sku}/cost")
def sku_cost(sku: str, cost: str = Form(""), back: str = Form("/")):
    err = service.set_cost(conn(), sku, cost)
    return RedirectResponse(_back(back, "err", err) if err else _back(back, "msg", f"{sku} 成本价已保存，已重算"), status_code=303)


@app.post("/sku/{sku}/stage")
def sku_stage(sku: str, stage: str = Form(""), back: str = Form("/")):
    err = service.set_stage(conn(), sku, stage)
    return RedirectResponse(_back(back, "err", err) if err else _back(back, "msg", f"{sku} 生命周期已更新，已重算"), status_code=303)


@app.get("/sku/{sku}", response_class=HTMLResponse)
def sku_page(request: Request, sku: str, back: str = "/", done: str = "", err: str = "", msg: str = ""):
    c = conn()
    if service.sku_result(c, sku) is None:
        return RedirectResponse("/", status_code=303)
    service.mark_viewed(c, sku)
    r = service.sku_result(c, sku)
    s = r.get("建议") or {}
    rows = []
    from . import model
    if s.get("预估"):
        rows = model.estimate_rows(s["预估"]["当前"], s["预估"]["建议"], s["预估"]["无访客数"], s["预估"]["未计曝光"])
    p = params.effective(c)
    p_sku = {**p, **{k: v for k, v in (r.get("单品覆盖") or {}).items() if k != "弹性微调"}}
    last = c.execute("SELECT * FROM adoption WHERE sku = ? ORDER BY id DESC LIMIT 1", (sku,)).fetchone()
    back = back if back.startswith("/") else "/"
    return page(request, "sku.html", active="总览", r=r, s=s, rows=rows, curve=service.curve(r, p_sku), p_sku=p_sku,
                inputs=service.input_values(c, sku), stages=calc.STAGES, reasons=service.IGNORE_REASONS,
                back=back, back_q=quote(back, safe=""), done=done, err=err, msg=msg,
                last=dict(last) if last else None, _conn=c)


@app.post("/sku/{sku}/inputs")
async def sku_inputs(request: Request, sku: str):
    form = dict(await request.form())
    back = quote(form.get("back") or "/", safe="")
    if form.get("action") == "reset":
        service.reset_inputs(conn(), sku)
        return RedirectResponse(f"/sku/{quote(sku)}?back={back}&msg={quote('已恢复店铺默认，只重算了这个商品')}", status_code=303)
    errs = service.save_inputs(conn(), sku, form)
    if errs:
        return RedirectResponse(f"/sku/{quote(sku)}?back={back}&err={quote('；'.join(errs))}", status_code=303)
    return RedirectResponse(f"/sku/{quote(sku)}?back={back}&msg={quote('已保存，只重算了这个商品')}", status_code=303)


@app.post("/sku/{sku}/adopt")
def sku_adopt(sku: str, price: str = Form(""), back: str = Form("/")):
    err, rec = service.adopt(conn(), sku, price)
    b = quote(back or "/", safe="")
    if err:
        return RedirectResponse(f"/sku/{quote(sku)}?back={b}&err={quote(err)}", status_code=303)
    return RedirectResponse(f"/sku/{quote(sku)}?back={b}&done=adopt", status_code=303)


@app.post("/sku/{sku}/ignore")
def sku_ignore(sku: str, reason: str = Form(""), back: str = Form("/")):
    err = service.ignore(conn(), sku, reason)
    b = quote(back or "/", safe="")
    if err:
        return RedirectResponse(f"/sku/{quote(sku)}?back={b}&err={quote(err)}", status_code=303)
    return RedirectResponse(f"/sku/{quote(sku)}?back={b}&done=ignore", status_code=303)


@app.get("/export/adoptions.csv")
def export_adoptions():
    return Response(service.adoptions_csv(conn()), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote('采纳记录.csv')}"})


@app.get("/export/gate1.csv")
def export_gate1():
    return Response(service.gate1_csv(conn()), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote('门1评估.csv')}"})


@app.get("/sku/{sku}/estimate")
def sku_estimate(sku: str, price: float):
    res = service.estimate(conn(), sku, price)
    if res is None:
        return Response('{"error": "没有建议区间"}', status_code=404, media_type="application/json")
    return res


@app.get("/export/simulation.csv")
def export_simulation():
    return Response(service.simulation_csv(conn()), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote('模拟推演.csv')}"})


@app.get("/export/events.csv")
def export_events():
    return Response(service.events_csv(conn()), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote('改价事件与弹性.csv')}"})


@app.get("/healthz")
def healthz():
    return {"ok": True}


def run():
    """uv run advisor [--port 8321] [--db 路径] [--no-browser]"""
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(prog="advisor")
    ap.add_argument("--port", type=int, default=int(os.environ.get("ADVISOR_PORT", "8321")))
    ap.add_argument("--db", default=None, help="数据库文件，默认 data/advisor.db")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if a.db:
        os.environ["ADVISOR_DB"] = str(Path(a.db).resolve())
    port = a.port
    if not a.no_browser and os.environ.get("ADVISOR_NO_BROWSER") != "1":
        import threading

        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
