"""自检命令：uv run python scripts/accept.py PT-01 [PT-02 ...]

每步输出三列：条目、通过、实测。「人工」= 机器判不了，按《用户验收条例》亲自看。
每个场景用一个临时空库跑，不碰 data/advisor.db。
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import warnings
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient  # noqa: E402

from advisor import service  # noqa: E402
from advisor.db import connect  # noqa: E402
from advisor.main import app  # noqa: E402

S = ROOT / "samples" / "样例店"
V = ROOT / "samples" / "变体"


@contextmanager
def fresh_db():
    with tempfile.TemporaryDirectory() as d:
        old = os.environ.get("ADVISOR_DB")
        os.environ["ADVISOR_DB"] = str(Path(d) / "t.db")
        try:
            yield TestClient(app)
        finally:
            if old is None:
                os.environ.pop("ADVISOR_DB", None)
            else:
                os.environ["ADVISOR_DB"] = old


def upload(c, products=None, sales=None, competitors=None, follow=False):
    files = {}
    for k, p in (("products", products), ("sales", sales), ("competitors", competitors)):
        if p:
            files[k] = (Path(p).name, Path(p).read_bytes(), "text/csv")
    return c.post("/import", files=files, follow_redirects=follow)


def import_samples(c, confirm=True):
    r = upload(c, S / "商品表.csv", S / "销售日记录.csv", S / "竞品价格.csv")
    loc = r.headers.get("location", "")
    if confirm and "first=1" in loc:
        c.post("/settings", data=settings_form(c) | {"first": "1"}, follow_redirects=False)
    return r


def settings_form(c, **over):
    conn = connect()
    from advisor import params
    p = params.effective(conn)
    form = {"平台": p["平台"], "扣点率": f"{p['扣点率'] * 100:g}", "单件履约费": f"{p['单件履约费']:g}",
            "退货率": f"{p['退货率'] * 100:g}", "单次退货净损失占比": f"{p['单次退货净损失占比'] * 100:g}",
            "最低毛利率": f"{p['最低毛利率'] * 100:g}", "尾货期最低毛利率": f"{p['尾货期最低毛利率'] * 100:g}",
            "目标清仓天数": str(p["目标清仓天数"]), "滞销线": str(p["滞销线"]), "store_name": "样例店"}
    for i, e in enumerate(params.category_elasticities(conn, service.elastic_categories(conn))):
        form[f"el_base_{i}"] = f"{e['基准']:g}"
        form[f"el_adj_{i}"] = f"{e['微调'] * 100:g}"
    form.update(over)
    return form


def res_by_sku():
    return {r["商品编号"]: r for r in service.results(connect())}


def last_log():
    conn = connect()
    row = conn.execute("SELECT id FROM import_log ORDER BY id DESC LIMIT 1").fetchone()
    return service.import_log(conn, row["id"])


class Report:
    def __init__(self, step):
        self.step, self.rows = step, []

    def add(self, item, ok, got):
        self.rows.append((item, "人工" if ok is None else ("通过" if ok else "不通过"), str(got)))

    def check(self, item, fn):
        try:
            ok, got = fn()
        except Exception as e:  # 自检本身出错也记不通过，不中断其他条目
            ok, got = False, f"自检出错：{type(e).__name__}: {e}"
        self.add(item, ok, got)

    def show(self):
        w = max(len(r[0]) for r in self.rows)
        print(f"\n== {self.step} ==")
        print(f"{'条目'.ljust(w)}  通过    实测")
        for item, st, got in self.rows:
            print(f"{item.ljust(w)}  {st.ljust(4)}  {got}")
        n_fail = sum(1 for r in self.rows if r[1] == "不通过")
        n_man = sum(1 for r in self.rows if r[1] == "人工")
        print(f"机器可判 {len(self.rows) - n_man} 条：通过 {len(self.rows) - n_man - n_fail}，不通过 {n_fail}；人工 {n_man} 条")
        return n_fail


# ---------------- PT-01 ----------------

def pt01():
    R = Report("PT-01 技术方案与项目骨架")
    R.add("PT-01-1 技术方案齐全", None, "打开 docs/技术方案.md，看七部分：架构分层、数据表、参数文件、计算模块、页面路由、测试、启动方式")

    def start():
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        env = dict(os.environ, ADVISOR_PORT=str(port), ADVISOR_NO_BROWSER="1")
        with tempfile.TemporaryDirectory() as d:
            env["ADVISOR_DB"] = str(Path(d) / "t.db")
            t0 = time.time()
            p = subprocess.Popen([shutil.which("uv") or "/opt/homebrew/bin/uv", "run", "advisor"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                while time.time() - t0 < 30:
                    try:
                        if urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1).status == 200:
                            return True, f"uv run advisor 启动 {time.time() - t0:.1f} 秒可打开（默认端口 8321）"
                    except OSError:
                        time.sleep(0.3)
                return False, "30 秒内没起来"
            finally:
                p.terminate(); p.wait(5)
    R.check("PT-01-2 一条命令启动", start)

    def topbar():
        with fresh_db() as c:
            html = c.get("/").text
        need = ["定价参谋", ">总览<", ">导入<", ">体检报告<", ">设置<", 'class="store"', 'class="btn primary" href="/import">导入数据']
        miss = [n for n in need if n not in html]
        return not miss, "产品名、总览、导入、体检报告、设置、店铺名、导入数据主按钮都在" if not miss else f"缺：{miss}"
    R.check("PT-01-3 顶栏", topbar)

    def empty():
        with fresh_db() as c:
            html = c.get("/").text
        ok = "还没有数据" in html and "/templates/商品表.csv" in html and "<table" not in html
        return ok, "空库只显示「导入数据」引导与三个模板下载，没有表格" if ok else "空状态不对"
    R.check("PT-01-4 空状态", empty)

    def param_file():
        p = json.loads((ROOT / "config" / "params.default.json").read_text(encoding="utf-8"))
        need = {"扣点率": 0.05, "单件履约费": 3.5, "退货率": 0.06, "单次退货净损失占比": 0.6, "最低毛利率": 0.05,
                "尾货期最低毛利率": 0.0, "目标清仓天数": 45, "滞销线": 90, "全类目先验": -1.34, "曝光倍数": 1.0,
                "候选价上限倍数": 1.3, "区间宽度": 0.9, "贴近成本系数": 1.05, "弹性微调上限": 0.5}
        bad = {k: p.get(k) for k, v in need.items() if p.get(k) != v}
        extra = ["单件广告成本口径", "类目先验", "弹性硬限", "竞品裁剪", "置信度", "新品期上架天数", "尾货期日均比"]
        miss = [k for k in extra if k not in p]
        ok = not bad and not miss
        return ok, "PRD 9.1 参数都在，默认值一致，含单件广告成本口径" if ok else f"不一致 {bad}；缺 {miss}"
    R.check("PT-01-5 参数文件", param_file)
    R.add("PT-01-6 自检命令", True, "本输出即三列：条目、通过、实测")

    def samples():
        sys.path.insert(0, str(ROOT / "scripts"))
        import make_samples as ms
        rows = list(csv.DictReader(open(S / "商品表.csv", encoding="utf-8-sig")))
        got = [(r["商品编号"], r["类目"], r["成本价"], r["当前售价"], r["库存数量"]) for r in rows]
        want = [(p[0], p[2], "" if p[4] is None else f"{p[4]:g}", f"{p[5]:g}", str(p[6])) for p in ms.PRODUCTS]
        files = all((S / f).exists() for f in ("商品表.csv", "销售日记录.csv", "竞品价格.csv", "答案表.csv"))
        n_ans = sum(1 for _ in csv.DictReader(open(S / "答案表.csv", encoding="utf-8-sig")))
        ok = files and got == want and n_ans == 12
        return ok, f"三张表与答案表{'齐全' if files else '不全'}；S01～S12 参数与附录 A.2 {'一致' if got == want else '不一致'}；答案表 {n_ans} 行"
    R.check("PT-01-7 样例集", samples)

    def variants():
        need = ["缺列_销售日记录.csv", "日期格式错_销售日记录.csv", "编号重复_商品表.csv", "售价为0_销售日记录.csv",
                "非模板.csv", "列名不同_销售日记录.csv", "无广告列_销售日记录.csv", "S09活动日_销售日记录.csv",
                "规模样例/商品表.csv", "规模样例/销售日记录.csv"]
        miss = [n for n in need if not (V / n).exists()]
        ok = not miss
        return ok, f"变体 {len(need) - len(miss)}/{len(need)} 个在"
    R.check("PT-01-8 变体文件", variants)
    return R


# ---------------- PT-02 ----------------

def pt02():
    R = Report("PT-02 导入与校验")

    def tpl():
        with fresh_db() as c:
            heads = {t: next(csv.reader(io.StringIO(c.get(f"/templates/{t}.csv").content.decode("utf-8-sig"))))
                     for t in ("商品表", "销售日记录", "竞品价格")}
        want = {"商品表": ["商品编号", "商品名称", "类目", "上架日期", "当前售价", "成本价", "库存数量", "生命周期标记"],
                "销售日记录": ["商品编号", "日期", "当日售价", "成交件数", "成交金额", "退款件数", "访客数", "广告花费", "广告成交件数"],
                "竞品价格": ["商品编号", "竞品序号", "竞品售价", "录入日期"]}
        return heads == want, "三张模板列与 PRD 11.1 一致，销售日记录含广告花费、广告成交件数" if heads == want else str(heads)
    R.check("PT-02-1 模板下载", tpl)

    with fresh_db() as c:
        def ok_import():
            r = import_samples(c)
            log = last_log()
            b = service.banner(log)
            return b.startswith("已导入 12 个商品、60 天记录，其中 3 个数据不足"), b
        R.check("PT-02-2 正确导入", ok_import)

    def first():
        with fresh_db() as c:
            r = upload(c, S / "商品表.csv", S / "销售日记录.csv", S / "竞品价格.csv")
            loc1 = r.headers.get("location")
            r2 = c.post("/settings", data=settings_form(c) | {"first": "1", "log": loc1.split("log=")[-1]}, follow_redirects=False)
            loc2 = r2.headers.get("location")
            r3 = upload(c, sales=S / "销售日记录.csv")
            loc3 = r3.headers.get("location")
        ok = loc1.startswith("/settings?first=1") and loc2.startswith("/report") and loc3.startswith("/report")
        return ok, f"首次 → {loc1}；确认后 → {loc2}；再次导入 → {loc3}"
    R.check("PT-02-3 首次导入去设置", first)

    def reject(item, path, field, expect):
        def fn():
            with fresh_db() as c:
                import_samples(c)
                r = upload(c, **{field: path})
                log = last_log()
            hit = [i for i in log["issues"] if expect in i["reason"]]
            ok = log["status"] == "拒收" and bool(hit)
            return ok, f"{log['status']}：" + "；".join(f"第 {i['line']} 行 {i['reason']}" for i in (hit or log["issues"])[:2])
        R.check(item, fn)
    reject("PT-02-4 缺列", V / "缺列_销售日记录.csv", "sales", "缺少列：成交件数")
    reject("PT-02-5 日期格式错", V / "日期格式错_销售日记录.csv", "sales", "日期格式错误")
    reject("PT-02-6 编号重复", V / "编号重复_商品表.csv", "products", "商品编号重复：S03")
    reject("PT-02-7 售价为 0", V / "售价为0_销售日记录.csv", "sales", "当日售价为空或为 0")
    reject("PT-02-8 非模板", V / "非模板.csv", "sales", "请先下载模板")

    def dl():
        with fresh_db() as c:
            import_samples(c)
            upload(c, sales=V / "日期格式错_销售日记录.csv")
            log = last_log()
            body = c.get(f"/import/issues/{log['id']}.csv").content.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(body)))[1:]
        same = [(r[0], int(r[1]), r[2]) for r in rows] == [(i["table"], i["line"], i["reason"]) for i in log["issues"]]
        return bool(same and rows), f"CSV {len(rows)} 行，与页面一致" if same else "CSV 与页面不一致"
    R.check("PT-02-9 错误清单可下载", dl)

    def overwrite():
        with fresh_db() as c:
            import_samples(c)
            n0 = connect().execute("SELECT COUNT(*) FROM sales_day").fetchone()[0]
            upload(c, sales=S / "销售日记录.csv")
            n1 = connect().execute("SELECT COUNT(*) FROM sales_day").fetchone()[0]
            b = service.banner(last_log())
        return n0 == n1 and f"覆盖 {n0} 行" in b, f"记录数 {n0} → {n1}；{b}"
    R.check("PT-02-10 重复上传覆盖", overwrite)

    def nocost():
        with fresh_db() as c:
            import_samples(c)
            s07 = res_by_sku()["S07"]
            html = c.get("/").text
        ok = s07["数据状态"] == "数据不足" and "缺成本价" in s07["数据不足原因"] and 'action="/sku/S07/cost"' in html
        return ok, f"S07 {s07['数据状态']}（{s07['数据不足原因']}），总览有「补录成本」"
    R.check("PT-02-11 缺成本不拦", nocost)

    def short():
        with fresh_db() as c:
            import_samples(c)
            s05 = res_by_sku()["S05"]
        return s05["数据不足原因"] == "记录不足 14 天", f"S05 {s05['数据状态']}（{s05['数据不足原因']}，{s05['记录天数']} 天记录），区间在 PT-05 才生成"
    R.check("PT-02-12 记录不足不拦", short)

    def rollback():
        with fresh_db() as c:
            import_samples(c)
            conn = connect()
            before = conn.execute("SELECT COUNT(*), SUM(units) FROM sales_day").fetchone()[:]
            files = {"商品表": (V / "规模样例" / "商品表.csv").read_bytes(), "销售日记录": (V / "规模样例" / "销售日记录.csv").read_bytes()}
            try:
                service.run_import(conn, files, fail_after=5000)
                crashed = False
            except RuntimeError:
                crashed = True
            after = connect().execute("SELECT COUNT(*), SUM(units) FROM sales_day").fetchone()[:]
            n_p = connect().execute("SELECT COUNT(*) FROM product").fetchone()[0]
        ok = crashed and before == after and n_p == 12
        return ok, f"写到第 5000 行时模拟中断：销售记录 {before[0]} → {after[0]} 行，商品 {n_p} 个，没有半份"
    R.check("PT-02-13 中断回滚", rollback)

    def mapping():
        with fresh_db() as c:
            import_samples(c)
            r = upload(c, sales=V / "列名不同_销售日记录.csv")
            asked = "确认列名" in r.text and "销量" in r.text
            token = r.text.split('name="token" value="')[1].split('"')[0]
            form = {"token": token}
            head = next(csv.reader(open(V / "列名不同_销售日记录.csv", encoding="utf-8-sig")))
            guessm = {"统计日期": "日期", "售价": "当日售价", "销量": "成交件数", "销售额": "成交金额"}
            for h in head:
                form[f"map|销售日记录|{h}"] = guessm.get(h, h)
            r2 = c.post("/import/mapping", data=form, follow_redirects=False)
            r3 = upload(c, sales=V / "列名不同_销售日记录.csv")
        ok = asked and r2.status_code == 303 and "/report" in r2.headers["location"] and r3.status_code == 303
        return ok, f"首次出现确认页：{'是' if asked else '否'}；确认后导入：{r2.headers.get('location')}；再传同样列名直接导入：{'是' if r3.status_code == 303 else '否，又问了'}"
    R.check("PT-02-14 列名映射", mapping)

    def noad():
        with fresh_db() as c:
            import_samples(c)
            upload(c, sales=V / "无广告列_销售日记录.csv")
            s04 = res_by_sku()["S04"]
            upload(c, sales=S / "销售日记录.csv")
            back = res_by_sku()["S04"]["保本价"]
        ok = s04["保本价"] == 30.09 and s04["广告口径"] == "无广告数据" and back == 33.37
        return ok, f"无广告列后 S04 保本价 ¥{s04['保本价']}（{s04['广告口径']}）；重新导入完整样例集后 ¥{back}"
    R.check("PT-02-15 广告列可空", noad)

    def speed():
        with fresh_db() as c:
            t0 = time.time()
            r = upload(c, V / "规模样例" / "商品表.csv", V / "规模样例" / "销售日记录.csv")
            dt = time.time() - t0
            n = connect().execute("SELECT COUNT(*) FROM sales_day").fetchone()[0]
        return dt < 3 and n == 13500, f"150 个 SKU × 90 天 = {n} 行，校验加入库加重算 {dt:.2f} 秒"
    R.check("PT-02-16 速度", speed)

    def keep():
        with fresh_db() as c:
            import_samples(c)
            c.post("/sku/S07/cost", data={"cost": "10"})
            c.post("/sku/S03/stage", data={"stage": "尾货期"})
            r = upload(c, products=S / "商品表.csv")
            by = res_by_sku()
            html = c.get(r.headers["location"]).text
        ok = (by["S07"]["成本价"] == 10 and by["S03"]["生命周期"] == "尾货期"
              and "沿用页面补录的 ¥10.00" in html and "沿用手动标记的尾货期" in html)
        return ok, f"重新导入商品表后 S07 成本 ¥{by['S07']['成本价']}、S03 {by['S03']['生命周期']}；体检报告页逐条说明沿用"
    R.check("补录与标记不被导入冲掉", keep)

    fails = sum(1 for r in R.rows if r[1] == "不通过")
    R.add("PT-02-17 自检", fails == 0, "以上全部通过" if not fails else f"{fails} 条不通过")
    return R


# ---------------- PT-03 ----------------

def pt03():
    R = Report("PT-03 店铺设置与参数")
    with fresh_db() as c:
        import_samples(c)

        def fields():
            html = c.get("/settings").text
            need = ["平台", "扣点率", "单件履约费", "退货率", "单次退货净损失占售价比例", "最低毛利率", "尾货期最低毛利率",
                    "目标清仓天数", "滞销库存天数线", "类目弹性"]
            miss = [n for n in need if n not in html]
            return not miss, "十项都在" if not miss else f"缺：{miss}"
        R.check("PT-03-1 字段齐全", fields)

        def defaults():
            f = settings_form(c)
            got = [f[k] for k in ("扣点率", "单件履约费", "退货率", "单次退货净损失占比", "最低毛利率", "尾货期最低毛利率", "目标清仓天数", "滞销线")]
            want = ["5", "3.5", "6", "60", "5", "0", "45", "90"]
            return got == want, "、".join(got)
        R.check("PT-03-2 默认值", defaults)

        def recalc():
            c.post("/settings", data=settings_form(c, 扣点率="6"))
            a = res_by_sku()["S02"]["保本价"]
            c.post("/settings", data=settings_form(c, 扣点率="5"))
            b = res_by_sku()["S02"]["保本价"]
            return a == 26.00 and b == 25.71, f"扣点率 6% 时 S02 保本价 ¥{a:.2f}，改回 5% 后 ¥{b:.2f}"
        R.check("PT-03-3 改参数即重算", recalc)

        def expire():
            conn = connect()
            before = service.run_status(conn)
            c.post("/settings", data=settings_form(c, 目标清仓天数="60"))
            after = service.run_status(connect())
            c.post("/settings", data=settings_form(c, 目标清仓天数="45"))
            old_rows = connect().execute("SELECT status, COUNT(*) FROM sku_result WHERE run_id = ? GROUP BY status", (before["版本"],)).fetchall()
            ok = after["版本"] > before["版本"] and [tuple(r) for r in old_rows] == [("已过期", 12)]
            return ok, f"第 {before['版本']} 版 12 条标「已过期」，生成第 {after['版本']} 版 {after['本版']} 条（建议区间在 PT-05 生成，届时同样过期）"
        R.check("PT-03-4 旧结果过期", expire)

        def illegal():
            n0 = connect().execute("SELECT COUNT(*) FROM setting_log").fetchone()[0]
            msgs = []
            for k, v in (("扣点率", "-1"), ("退货率", "110"), ("el_adj_0", "80")):
                html = c.post("/settings", data=settings_form(c, **{k: v})).text
                msgs.append("没有保存" in html)
            n1 = connect().execute("SELECT COUNT(*) FROM setting_log").fetchone()[0]
            return all(msgs) and n0 == n1, f"扣点率 −1%、退货率 110%、微调 +80% 都拒收并提示范围；日志行数不变（{n0}）"
        R.check("PT-03-5 非法值", illegal)

        def hard():
            a = "没有保存" in c.post("/settings", data=settings_form(c, el_base_0="-0.2")).text
            b = "没有保存" in c.post("/settings", data=settings_form(c, el_base_0="-5")).text
            return a and b, "基准弹性 −0.2、−5 都拒收（硬限 −4 到 −0.3）"
        R.check("PT-03-6 弹性硬限", hard)

        def logs():
            rows = connect().execute("SELECT at, field, old, new FROM setting_log").fetchall()
            ok = any(r["field"] == "扣点率" and r["new"] == "0.06" for r in rows) and all(r["at"] for r in rows)
            html = c.get("/settings/log").text
            pct = "5%" in html and "6%" in html and "0.06" not in html
            return ok and "扣点率" in html and pct, f"日志 {len(rows)} 行，有时间、字段、旧值、新值；页面 /settings/log 百分数显示为 5% → 6%"
        R.check("PT-03-7 变更日志", logs)
    return R


# ---------------- PT-04 ----------------

def pt04():
    R = Report("PT-04 计算一：基础量")
    with fresh_db() as c:
        import_samples(c)
        by = res_by_sku()

        def eq(item, fn):
            R.check(item, fn)

        eq("PT-04-1 保本价，无广告", lambda: (by["S02"]["保本价"] == 25.71, f"S02 ¥{by['S02']['保本价']:.2f}"))
        eq("PT-04-2 保本价，含广告", lambda: (
            (by["S04"]["单件广告成本"], by["S04"]["保本价"], by["S04"]["不含广告保本价"]) == (3.0, 33.37, 30.09),
            f"S04 单件广告成本 ¥{by['S04']['单件广告成本']:.2f}，保本价 ¥{by['S04']['保本价']:.2f}，不含广告 ¥{by['S04']['不含广告保本价']:.2f}"))
        eq("PT-04-3 无广告不受影响", lambda: (by["S03"]["保本价"] == 7.11 and by["S03"]["广告口径"] == "无广告数据",
                                        f"S03 ¥{by['S03']['保本价']:.2f}，{by['S03']['广告口径']}"))
        eq("PT-04-4 毛利率", lambda: (by["S01"]["毛利率"] == 0.3556, f"S01 {by['S01']['毛利率']:.2%}"))
        eq("PT-04-5 日均与库存天数", lambda: (by["S01"]["近30天日均"] == 6 and round(by["S01"]["库存天数"]) == 130,
                                         f"S01 日均 {by['S01']['近30天日均']:g}，库存天数 {by['S01']['库存天数']:g}"))
        eq("PT-04-6 无动销", lambda: (by["S06"]["库存天数"] is None and by["S06"]["数据不足原因"] == "无动销",
                                   f"S06 库存天数「无动销」，{by['S06']['数据状态']}"))
        eq("PT-04-7 尾货期", lambda: (by["S01"]["生命周期"] == "尾货期",
                                   f"S01 {by['S01']['生命周期']}：{by['S01']['库存天数']:g} ＞ 90，且 {by['S01']['近30天日均']:g} ＜ {by['S01']['前30天日均']:g} × 0.7"))
        eq("PT-04-8 新品期", lambda: (by["S05"]["生命周期"] == by["S12"]["生命周期"] == "新品期",
                                   f"S05 {by['S05']['生命周期']}，S12 {by['S12']['生命周期']}"))

        def manual():
            c.post("/sku/S03/stage", data={"stage": "尾货期"})
            a = res_by_sku()["S03"]
            c.post("/sku/S03/stage", data={"stage": ""})
            b = res_by_sku()["S03"]
            return (a["生命周期"], a["生命周期来源"], b["生命周期"]) == ("尾货期", "店主标记", "成熟期"), \
                f"标记后 {a['生命周期']}（{a['生命周期来源']}），取消后 {b['生命周期']}"
        eq("PT-04-9 手动标记优先", manual)
        eq("PT-04-10 贴近成本", lambda: (by["S02"]["贴近成本"] and by["S02"]["贴近成本线"] == 27.00,
                                      f"S02 贴近成本：{by['S02']['当前价']:g} ＜ {by['S02']['贴近成本线']:.2f}"))
        eq("PT-04-11 数据不足三种原因", lambda: (
            [by[s]["数据不足原因"] for s in ("S05", "S06", "S07")] == ["记录不足 14 天", "无动销", "缺成本价"],
            "；".join(f"{s} {by[s]['数据不足原因']}" for s in ("S05", "S06", "S07"))))

        detail = list(csv.DictReader(io.StringIO(c.get("/export/detail.csv").content.decode("utf-8-sig"))))
        dby = {r["商品编号"]: r for r in detail}
        eq("PT-04-12 改价事件", lambda: (dby["S09"]["改价事件"] == "2026-09-09 19.9→17.9",
                                      f"S09 计算明细：{dby['S09']['改价事件']}（第 40 天 = 2026-09-09）"))
        eq("PT-04-13 活动日", lambda: (dby["S08"]["活动日"] == "2026-09-14",
                                    f"S08 计算明细：{dby['S08']['活动日']}（第 45 天 = 2026-09-14）"))

        def answers():
            ans = list(csv.DictReader(open(S / "答案表.csv", encoding="utf-8-sig")))
            bad = []
            for a in ans:
                d = dby[a["商品编号"]]
                for k in ("保本价", "不含广告保本价", "毛利率", "近30天日均", "库存天数", "生命周期", "数据状态", "数据不足原因"):
                    got = d[k]
                    if k in ("保本价", "不含广告保本价") and got:
                        got = f"{float(got):.2f}"
                    if got != a[k]:
                        bad.append(f"{a['商品编号']}.{k} 明细 {got} / 答案 {a[k]}")
                if f"{float(d['单件广告成本']):.2f}" != a["单件广告成本"]:
                    bad.append(f"{a['商品编号']}.单件广告成本")
            return not bad, "12 个 SKU × 9 项全部与答案表一致" if not bad else "；".join(bad[:5])
        eq("PT-04-14 答案表比对", answers)
    R.add("PT-04-15 目标店比对", None, "需要真实店铺数据，样例集覆盖不了")
    return R


# ---------------- PT-05 ----------------

def sug(by, sku):
    return by[sku]["建议"]


def pt05():
    R = Report("PT-05 计算二：建议区间、置信度、解释")
    with fresh_db() as c:
        import_samples(c)
        by = res_by_sku()
        R.check("PT-05-1 保本约束，成熟期", lambda: (sug(by, "S02")["区间"][0] >= 27.00,
                f"S02 最低建议价 ¥{sug(by, 'S02')['区间'][0]:.2f}（保本价 × 1.05 = ¥27.00）"))

        def infeasible():
            s = sug(by, "S01")
            txt = "".join(s["解释"])
            html = c.get("/sku/S01").text
            ok = "保本价也无法在目标天数内清完，建议考虑捆绑或其他渠道" in txt and min(s["区间"]) >= 25.71 and "保本价也无法" in html
            return ok, f"S01 区间 {s['区间']}，最低 ≥ ¥25.71；解释与页面都有「保本价也无法在目标天数内清完」"
        R.check("PT-05-2 保本约束，尾货期", infeasible)
        R.check("PT-05-3 区间三点顺序", lambda: (all(sug(by, k)["区间"][0] <= sug(by, k)["区间"][1] <= sug(by, k)["区间"][2] for k in ("S03", "S04")),
                "；".join(f"{k} {sug(by, k)['区间']}" for k in ("S03", "S04"))))
        R.check("PT-05-4 候选上限", lambda: (sug(by, "S03")["区间"][2] <= 12.87, f"S03 最高建议价 ¥{sug(by, 'S03')['区间'][2]:.2f}（≤ ¥12.87）"))

        def comp():
            s = sug(by, "S10")
            txt = "".join(s["解释"])
            return s["区间"][2] <= 39.38 and "竞品中位价 ¥37.50" in txt, f"S10 最高建议价 ¥{s['区间'][2]:.2f}（≤ ¥39.38）；解释：{s['竞品']['说明']}"
        R.check("PT-05-5 竞品裁剪", comp)

        def one_comp():
            s = sug(by, "S11")
            from advisor import model, params
            p = params.effective(connect())
            alone = model.suggest(by["S11"], s["弹性"], [], p)
            same = alone["区间"] == s["区间"]
            return same and "竞品少于 2 个，未对标" in "".join(s["解释"]), f"S11 有竞品与无竞品区间一致：{s['区间']}；解释含「竞品少于 2 个，未对标」"
        R.check("PT-05-6 竞品不足 2 个", one_comp)
        low = [k for k, v in by.items() if (v["建议"] or {}).get("健康度") == "偏低"]
        high = [k for k, v in by.items() if (v["建议"] or {}).get("健康度") == "偏高"]
        R.add("PT-05-7 偏低与偏高", None, f"要目标店数据，暂不能做。样例店里偏低 {low}、偏高 {high}，"
              f"如 {low[0]} 当前价 ¥{by[low[0]]['当前价']} ＜ 最低 ¥{sug(by, low[0])['区间'][0]:.2f}，{high[0]} 当前价 ¥{by[high[0]]['当前价']} ＞ 最高 ¥{sug(by, high[0])['区间'][2]:.2f}")

        def novis():
            s = sug(by, "S03")
            html = c.get("/sku/S03").text
            return "按销量弹性估" in "".join(s["解释"]) and "无访客数" in html, "S03 解释含「按销量弹性估」，转化率一行显示「无访客数」"
        R.check("PT-05-8 销量弹性退化", novis)

        def exposure():
            s = sug(by, "S04")
            html = c.get("/sku/S04").text
            return "未计曝光变化" in "".join(s["解释"]) and s["置信度"] != "高" and "曝光变化" in html and "未计" in html, \
                f"S04 曝光变化「未计」，解释含「未计曝光变化」，置信度 {s['置信度']}"
        R.check("PT-05-9 未计曝光", exposure)

        def prior():
            s = sug(by, "S09")
            ev = [e for e in service.run_extra(connect())["events"] if e["商品编号"] == "S09"]
            return (round(s["弹性"]["生效"], 2) == -1.34 and s["置信度"] == "中" and ev and round(ev[0]["弹性"], 2) == -1.72), \
                f"S09 采用 {s['弹性']['生效']:.2f}（{s['弹性']['来源']}），单个事件 {ev[0]['弹性']:.2f} 不用；置信度 {s['置信度']}"
        R.check("PT-05-10 事件不足用先验", prior)

        def lowconf():
            s = sug(by, "S12")
            html = c.get("/").text
            return s["置信度"] == "低" and s["解释"][0].startswith("基于类目经验值") and 'class="range weak"' in html, \
                f"S12 置信度 {s['置信度']}；结论句：{s['解释'][0][:28]}…；总览区间弱化为中蓝"
        R.check("PT-05-11 低置信度", lowconf)

        def four():
            s = sug(by, "S04")
            return len(s["解释"]) == 4 and "含单件广告成本 ¥3.00" in "".join(s["解释"]), f"S04 四句齐全；第 4 句：{s['解释'][3][:40]}…"
        R.check("PT-05-12 解释四句", four)

        def six():
            html = c.get("/sku/S04").text
            names = ["日销量", "日毛利", "日成交额", "转化率", "库存清完需", "曝光变化"]
            return all(n in html for n in names) and "当前 ¥36.90" in html and "建议 ¥" in html, "六行齐全，各有当前与建议两列"
        R.check("PT-05-13 预估对比六行", six)

        def gain():
            s = sug(by, "S04")
            pre = s["预估"]
            want = (pre["建议"]["日毛利"] - pre["当前"]["日毛利"]) * 14
            detail = list(csv.DictReader(io.StringIO(c.get("/export/detail.csv").content.decode("utf-8-sig"))))
            d = [r for r in detail if r["商品编号"] == "S04"][0]["预计可增毛利"]
            html = c.get("/").text
            return abs(s["可增毛利"] - want) < 1e-6 and d == f"{want:.2f}" and f"¥{want:,.2f}" in html, \
                f"S04 （¥{pre['建议']['日毛利']:.2f} − ¥{pre['当前']['日毛利']:.2f}）× 14 = ¥{want:.2f}，总览与计算明细一致"
        R.check("PT-05-14 预计可增毛利", gain)

        def slider():
            vals = [c.get("/sku/S04/estimate", params={"price": x}).json()["行"][0]["建议"] for x in (36.0, 40.0, 45.0)]
            units = [float(v.split()[0]) for v in vals]
            return units[0] > units[1] > units[2], f"S04 试算 ¥36 / ¥40 / ¥45 日销量 {vals}，价越低日销量越高"
        R.check("PT-05-15 滑块任意价", slider)
    fails = sum(1 for r in R.rows if r[1] == "不通过")
    R.add("PT-05-16 自检", fails == 0, "以上机器可判条目全部通过" if not fails else f"{fails} 条不通过")
    return R


# ---------------- PT-06 ----------------

CE = ROOT / "samples" / "反例集"


def import_counter(c):
    r = upload(c, CE / "商品表.csv", CE / "销售日记录.csv", CE / "竞品价格.csv")
    form = settings_form(c)
    from advisor import params
    cats = service.elastic_categories(connect())
    form[f"el_adj_{cats.index('收纳')}"] = "-50"  # 反例「弹性低库存高」：收纳类目弹性微调 −50%
    c.post("/settings", data=form | {"first": "1"})
    return r


def counter_checks(c):
    by = res_by_sku()
    out = []
    s = by["CE1_S01"]["建议"]
    out.append(("弹性低库存高", min(s["区间"]) >= by["CE1_S01"]["保本价"] and "保本价也无法" in "".join(s["解释"]) and round(s["弹性"]["生效"], 2) == -0.67,
                f"CE1_S01 弹性 {s['弹性']['生效']:.2f}，区间 {s['区间']} 不低于保本 ¥{by['CE1_S01']['保本价']:.2f}，出清不完提示"))
    s = by["CE2_S02"]["建议"]
    out.append(("竞品低毛利薄", s["健康度"] == "贴近成本" and s["区间"][0] >= 27.00,
                f"CE2_S02 健康度 {s['健康度']}，最低 ¥{s['区间'][0]:.2f} ≥ ¥27.00；{s['竞品']['说明']}"))
    s = by["CE3_S12"]["建议"]
    out.append(("新品无历史", by["CE3_S12"]["生命周期"] == "新品期" and s["置信度"] == "低", f"CE3_S12 {by['CE3_S12']['生命周期']}，置信度 {s['置信度']}"))
    r = by["CE4_S06"]
    out.append(("无动销", r["库存天数"] is None and r["数据状态"] == "数据不足" and not r["建议"]["区间"], "CE4_S06 库存天数「无动销」，数据不足，不出区间"))
    r = by["CE5_S08"]
    out.append(("活动日异常", r["活动日"] == ["2026-09-14"], f"CE5_S08 活动日 {r['活动日']} 已识别，不参与弹性校准"))
    r = by["CE6_S07"]
    out.append(("成本缺失", r["保本价"] is None and not r["建议"]["区间"], "CE6_S07 不出保本价与区间，操作为「补录成本」"))
    return out


def pt06():
    R = Report("PT-06 反例集与弹性校准复现")
    with fresh_db() as c:
        import_counter(c)
        checks = counter_checks(c)
        R.add("PT-06-1 反例自动测试", all(ok for _, ok, _ in checks), f"六类 {sum(ok for _, ok, _ in checks)}/6 通过（uv run pytest tests/test_counter.py）")
        pages = {k: c.get(f"/sku/{k}").text for k in ("CE1_S01", "CE2_S02", "CE3_S12", "CE4_S06", "CE5_S08", "CE6_S07")}
        page_ok = ("保本价也无法" in pages["CE1_S01"] and "贴近成本" in pages["CE2_S02"] and "d-low" in pages["CE3_S12"]
                   and "无动销" in pages["CE4_S06"] and "补录成本" in pages["CE6_S07"])
        R.add("PT-06-2 反例逐类看", page_ok, "；".join(msg for _, _, msg in checks) + "。页面请再亲自看一遍")
    with fresh_db() as c:
        import_samples(c)
        ev = [e for e in service.run_extra(connect())["events"] if e["商品编号"] == "S09"][0]
        import math
        want = math.log(1.2) / math.log(17.9 / 19.9)
        html = c.get("/report").text
        R.add("PT-06-3 样例弹性复现", round(ev["弹性"], 2) == -1.72 and abs(ev["弹性"] - want) < 1e-9 and "-1.72" in html,
              f"S09 {ev['弹性']:.4f} = ln 1.2 ÷ ln（17.9 ÷ 19.9）；体检报告弹性校准表显示 −1.72")
        R.add("PT-06-4 目标店弹性复现", None, "要目标店数据与门 0 校准表，延到门 0 数据到手后做")
        upload(c, sales=V / "S09活动日_销售日记录.csv")
        ev2 = [e for e in service.run_extra(connect())["events"] if e["商品编号"] == "S09"][0]
        acts = res_by_sku()["S09"]["活动日"]
        R.add("PT-06-5 活动日排除", round(ev2["弹性"], 2) == -1.72 and ev2["剔除活动日"] == 1 and "2026-09-11" in acts,
              f"S09 第 42 天（2026-09-11）成交 60 件识别为活动日，窗口剔除 {ev2['剔除活动日']} 天，弹性仍为 {ev2['弹性']:.2f}")
        upload(c, sales=S / "销售日记录.csv")
        for u in ("/", "/report", "/sku/S04"):  # 先各打开一次：打开单品页会把状态记为「已查看」
            c.get(u)
        before = c.get("/").text + c.get("/report").text + c.get("/sku/S04").text
        body = c.get("/export/simulation.csv").content.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(body)))
        head_ok = rows[0][:8] == ["日期", "商品编号", "真实价", "建议价", "真实销量", "模拟销量", "真实毛利", "模拟毛利"]
        mark_ok = all(r[-1] == "模拟" for r in rows[1:])
        R.add("PT-06-6 模拟推演导出", head_ok and mark_ok and len(rows) > 1, f"{len(rows) - 1} 行；列 {rows[0]}；每行标「模拟」")
        after = c.get("/").text + c.get("/report").text + c.get("/sku/S04").text
        R.add("PT-06-7 模拟不进指标", before == after and "模拟销量" not in after and "模拟毛利" not in after,
              "导出前后三页内容一致；页面上没有任何模拟数字")
    fails = sum(1 for r in R.rows if r[1] == "不通过")
    R.add("PT-06-8 自检", fails == 0, "以上机器可判条目全部通过" if not fails else f"{fails} 条不通过")
    return R


# ---------------- PT-07 ----------------

A4 = {"SKU 总数；有成本价的 SKU 数": "12；11", "数据不足（单列，不进分母）": "3", "贴近成本 SKU 占比": "11.1%",
      "疑似偏高 SKU 占比": "11.1%", "疑似偏低 SKU 占比": "11.1%", "需要动价 SKU 占比": "33.3%", "库存超 90 天 SKU 占比": "11.1%",
      "无动销（单列）": "1", "近 90 天有改价事件的 SKU 占比": "11.1%", "是否有按日访客数": "有，1 个 SKU",
      "近 30 天有广告花费的 SKU 占比": "11.1%"}


def pt07():
    R = Report("PT-07 体检报告页")
    with fresh_db() as c:
        r = upload(c, S / "商品表.csv", S / "销售日记录.csv", S / "竞品价格.csv")
        loc = c.post("/settings", data=settings_form(c) | {"first": "1", "log": r.headers["location"].split("log=")[-1]},
                     follow_redirects=False).headers["location"]
        html = c.get(loc).text
        R.add("PT-07-1 顶部提示", "已导入 12 个商品、60 天记录，其中 3 个数据不足" in html, "「已导入 12 个商品、60 天记录，其中 3 个数据不足」")
        m = service.report_metrics(connect())
        names = [x["指标"] for x in m["指标"]]
        need = ["SKU 总数；有成本价的 SKU 数", "贴近成本 SKU 占比", "疑似偏高 SKU 占比", "疑似偏低 SKU 占比", "需要动价 SKU 占比",
                "库存超 90 天 SKU 占比", "近 90 天有改价事件的 SKU 占比", "是否有按日访客数", "近 30 天有广告花费的 SKU 占比"]
        miss = [n for n in need if n not in names or n not in html]
        R.add("PT-07-2 指标齐全", not miss, f"{len(names)} 项，MRD 2.2 的九项加「近 30 天有广告花费的 SKU 占比」都在" if not miss else f"缺 {miss}")
        got = {x["指标"]: x["结果"] for x in m["指标"]}
        bad = {k: (got.get(k), v) for k, v in A4.items() if got.get(k) != v}
        R.add("PT-07-3 样例数值", not bad, "附录 A.4 十一项全部一致" if not bad else f"不一致：{bad}")
        bins = m["库存天数"]
        want = {"30 天以内": 5, "30～60 天": 3, "60～90 天": 0, "90 天以上": 1, "无动销": 1}
        R.add("PT-07-4 库存天数分布", bins == want, "；".join(f"{k} {v} 个" for k, v in bins.items()))
        detail = list(csv.DictReader(io.StringIO(c.get("/export/detail.csv").content.decode("utf-8-sig"))))
        same = all(f"¥{float(d['保本价']):.2f}" in c.get("/").text for d in detail if d["保本价"])
        R.add("PT-07-5 导出明细", len(detail) == 12 and same, f"计算明细 {len(detail)} 行，保本价与总览一致")
        R.add("PT-07-6 目标店一致", None, "要目标店数据与门 0 Excel，延到门 0 数据到手后做")
    fails = sum(1 for r in R.rows if r[1] == "不通过")
    R.add("PT-07-7 自检", fails == 0, "以上机器可判条目全部通过" if not fails else f"{fails} 条不通过")
    return R


# ---------------- PT-08 ----------------

import re as _re


def row_html(html, sku):
    """取列表里某个 SKU 那一行的 HTML。"""
    for tr in _re.findall(r"<tr>(.*?)</tr>", html, flags=_re.S):
        if f'href="/sku/{sku}?' in tr:
            return tr
    return ""


def list_skus(html):
    return _re.findall(r'<a class="sku" href="/sku/([^?"]+)\?', html)


def pt08():
    R = Report("PT-08 健康度列表页")
    with fresh_db() as c:
        import_samples(c)
        html = c.get("/").text
        v = service.list_view(connect())
        need = v["需要动"]
        R.add("PT-08-1 统计卡", "全店 SKU" in html and "其中 3 个数据不足" in html
              and f"贴近成本 {need['贴近成本']} · 偏低 {need['偏低']} · 偏高 {need['偏高']}" in html and "预计可增毛利 / 14 天" in html,
              f"全店 SKU 12，其中 3 个数据不足；需要动的 {sum(need.values())} 个（贴近成本 {need['贴近成本']} · 偏低 {need['偏低']} · 偏高 {need['偏高']}）；预计可增毛利 {v['可增毛利']:.2f}")
        bad = []
        for h in service.HEALTH_ORDER:
            page = c.get("/", params={"f": h}).text
            n = len(list_skus(page))
            pill = _re.search(rf'>{h} (\d+)</a>', page)
            if not pill or int(pill.group(1)) != n:
                bad.append(f"{h} 胶囊 {pill.group(1) if pill else '?'} / 行 {n}")
        R.add("PT-08-2 筛选", not bad, "五档胶囊上的数与筛出的行数都一致：" + "、".join(f"{h} {v['counts'][h]}" for h in service.HEALTH_ORDER) if not bad else "；".join(bad))
        order = list_skus(html)
        by = res_by_sku()
        rest = [k for k in order if (by[k]["建议"] or {}).get("健康度") not in ("贴近成本", "数据不足")]
        gains = [by[k]["建议"]["可增毛利"] for k in rest]
        R.add("PT-08-3 默认排序", order[0] == "S02" and gains == sorted(gains, reverse=True),
              f"顺序 {order}；S02 贴近成本置顶，其余按可增毛利从高到低，数据不足垫底")
        tr = row_html(html, "S04")
        need_f = ["加厚垃圾袋", "S04", "清洁", "aria-label=\"S04 生命周期\"", "¥36.9", "¥33.37", "含广告", "b-low", "15 天", "～", "class=\"dot", "查看建议"]
        miss = [x for x in need_f if x not in tr]
        R.add("PT-08-4 行字段", not miss, "名称编号、类目、生命周期、当前价、保本价带「含广告」、健康度徽标、库存天数、区间三点、置信度圆点、操作都在" if not miss else f"缺 {miss}")
        R.add("PT-08-5 低置信弱化", 'class="range weak"' in row_html(html, "S12"), "S12 区间用 range weak（中蓝 #5E88CC）")
        rows = {k: row_html(html, k) for k in ("S05", "S06", "S07")}
        R.add("PT-08-6 数据不足行", all("不出区间" in t for t in rows.values()) and 'action="/sku/S07/cost"' in rows["S07"],
              "S05、S06、S07 都不出区间；S07 操作为「补录成本」")
        c.post("/sku/S07/cost", data={"cost": "10", "back": "/"})
        s07 = res_by_sku()["S07"]
        R.add("PT-08-7 补录成本", s07["数据状态"] == "正常" and bool(s07["建议"]["区间"]), f"S07 补录 ¥10 后 {s07['数据状态']}，区间 {s07['建议']['区间']}")
        page = c.get("/", params={"f": "偏高", "sort": "stock"}).text
        first = list_skus(page)[0]
        link = _re.search(rf'href="/sku/{first}\?back=([^"]+)"', page).group(1)
        sku_page = c.get(f"/sku/{first}?back={link}").text
        back = _re.search(r'<a href="([^"]+)">← 返回列表</a>', sku_page).group(1)
        from urllib.parse import unquote
        R.add("PT-08-8 进入与返回", unquote(back).replace("&amp;", "&") in ("/?f=偏高&sort=stock", "/?f=%E5%81%8F%E9%AB%98&sort=stock"),
              f"筛「偏高」、按库存天数排后进 {first}，返回链接 {unquote(back).replace('&amp;', '&')}")
    fails = sum(1 for r in R.rows if r[1] == "不通过")
    R.add("PT-08-9 自检", fails == 0, "以上全部通过" if not fails else f"{fails} 条不通过")
    return R


# ---------------- PT-09 ----------------

def inputs_form(c, sku, **over):
    from advisor import service as sv
    iv = sv.input_values(connect(), sku)
    form = {k: iv[k] for k in ("成本价", "扣点率", "单件履约费", "退货率", "单次退货净损失占比", "最低毛利率", "尾货期最低毛利率",
                               "目标清仓天数", "弹性微调", "生命周期")}
    for i, x in enumerate(iv["竞品"], start=1):
        form[f"竞品{i}"] = x
    form["back"] = "/"
    form.update(over)
    return form


def pt09():
    R = Report("PT-09 单品建议页")
    with fresh_db() as c:
        import_samples(c)
        h = c.get("/sku/S01").text
        need = ["大号收纳盒", "S01", "收纳", "上架 210 天", "库存 780 件", "近 30 天日均 6 件", "库存天数 130 天", "尾货期", "b-high", "class=\"dot"]
        miss = [x for x in need if x not in h]
        R.add("PT-09-1 头部", not miss, "名称编号、类目、上架天数、库存、日均 6、库存天数 130；尾货期、健康度、置信度三个徽标" if not miss else f"缺 {miss}")
        h = c.get("/sku/S04").text
        ok = ('<span class="badge b-near">保本</span> <b class="money be-text">¥33.37</b>' in h and "含广告 ¥3.00" in h
              and "最低建议价已含保本线，再低就是亏钱" in h and "最优建议价" in h)
        R.add("PT-09-2 价格卡", ok, "当前价、区间、最优价、藏青加粗保本价 ¥33.37 带藏青底「保本」、「含广告 ¥3.00」、保本线说明")
        h2 = c.get("/sku/S02").text
        mn = float(_re.search(r'id="slider" type="range" min="([\d.]+)"', h2).group(1))
        R.add("PT-09-3 滑块下限", abs(mn - 27.00) < 1e-9, f"S02 滑块最小值 ¥{mn:.2f}")
        tb = _re.search(r'<table class="est" id="try-table">(.*?)</table>', h, flags=_re.S).group(1)
        rows = dict(_re.findall(r"<tr><td>([^<]+)</td>.*?<td class=\"num\">(<span class=\"(?:up|down|unk|muted)\">)", tb, flags=_re.S))
        want = {"日销量": "down", "日毛利": "up", "日成交额": "down", "转化率": "unk", "库存清完需": "down", "曝光变化": "unk"}
        got = {k: _re.search(r'class="(\w+)"', v).group(1) for k, v in rows.items()}
        R.add("PT-09-4 预估颜色", got == want, f"S04：{got}（up = 主蓝变好，down = 藏青底变差，unk = 中蓝虚线）")
        svg = _re.search(r"<svg viewBox.*?</svg>", h, flags=_re.S).group(0)
        s = res_by_sku()["S04"]["建议"]
        ok = ('fill="url(#hatch)"' in svg and 'fill="#ABC2EC"' in svg and 'stroke="#3F6CC4"' in svg and 'stroke-dasharray="4 3"' in svg
              and f"最优 ¥{s['最优价']:.2f}" in svg and "保本 ¥33.37" in svg)
        R.add("PT-09-5 曲线", ok, f"藏青斜线 = 保本以下、淡蓝带 = 区间 {s['区间'][0]:.2f}～{s['区间'][2]:.2f}、主蓝曲线、藏青虚线 = 最优 ¥{s['最优价']:.2f}")
        h12 = c.get("/sku/S12").text
        R.add("PT-09-6 低置信提示", "<li>基于类目经验值，你的记录还不够，先小幅试。" in h12 and h12.count("<li>") >= 4, "S12 四句，结论句前有「基于类目经验值」")
        c.post("/sku/S04/inputs", data=inputs_form(c, "S04", 成本价="25"))
        a = res_by_sku()["S04"]
        c.post("/sku/S04/inputs", data=inputs_form(c, "S04", 成本价="24"))
        b = res_by_sku()["S04"]
        R.add("PT-09-7 输入面板重算", a["保本价"] == 34.46 and b["保本价"] == 33.37 and a["建议"]["区间"] != b["建议"]["区间"],
              f"成本 ¥25 → 保本价 ¥{a['保本价']:.2f}（31.5 ÷ 0.914），区间 {a['建议']['区间']}；改回 ¥24 → ¥{b['保本价']:.2f}")
        c.post("/sku/S03/inputs", data=inputs_form(c, "S03", 竞品1="12.5", 竞品2="11.9", 竞品3="13.0"))
        s3 = res_by_sku()["S03"]["建议"]
        R.add("PT-09-8 竞品录入", "竞品中位价 ¥12.50" in "".join(s3["解释"]), f"S03 录入 3 个竞品价后：{s3['竞品']['说明']}")
        c.post("/sku/S03/inputs", data=inputs_form(c, "S03", 竞品1="", 竞品2="", 竞品3=""))
        before = res_by_sku()["S03"]["建议"]
        c.post("/sku/S03/inputs", data=inputs_form(c, "S03", 生命周期="尾货期"))
        after = res_by_sku()["S03"]
        c.post("/sku/S03/inputs", data=inputs_form(c, "S03", 生命周期=""))
        restored = res_by_sku()["S03"]
        ok = (after["生命周期"] == "尾货期" and after["建议"]["候选下限"] == 7.12 and before["候选下限"] == 7.47
              and restored["生命周期"] == "成熟期" and restored["建议"]["区间"] == before["区间"])
        R.add("PT-09-9 生命周期手改", ok, f"S03 改尾货期：最低毛利率按 0%，候选下限（滑块下限）¥{before['候选下限']:.2f} → ¥{after['建议']['候选下限']:.2f}"
              f"（未取整保本价 7.1116 向上取分）；区间 {before['区间']} → {after['建议']['区间']}；已恢复原样")
        R.add("PT-09-10 曝光说明", "曝光倍数 1.0（默认）。本店没有跨价格力档位的改价记录，预估未计曝光变化" in h, "「1.0（默认）」并写明未计原因")
    fails = sum(1 for r in R.rows if r[1] == "不通过")
    R.add("PT-09-11 自检", fails == 0, "以上全部通过" if not fails else f"{fails} 条不通过")
    return R


# ---------------- PT-10 ----------------

def status_of(sku):
    run = service.current_run(connect())
    return connect().execute("SELECT status FROM sku_result WHERE run_id = ? AND sku = ?", (run["id"], sku)).fetchone()["status"]


def pt10():
    R = Report("PT-10 采纳、忽略与状态")
    with fresh_db() as c:
        import_samples(c)
        by = res_by_sku()
        s4 = by["S04"]["建议"]
        h = c.get("/sku/S04").text
        val = _re.search(r'id="adopt-price" type="text" name="price" value="([\d.]+)"', h).group(1)
        R.add("PT-10-1 采纳默认价", float(val) == s4["最优价"], f"采纳价默认 ¥{val} = 最优价")
        low = s4["区间"][0]
        r = c.post("/sku/S04/adopt", data={"price": f"{low - 0.10:.2f}", "back": "/"}, follow_redirects=False)
        n0 = len(service.adoptions(connect()))
        js = (ROOT / "advisor" / "static" / "sku.js").read_text(encoding="utf-8")
        R.add("PT-10-2 低于最低不可提交", "err=" in r.headers["location"] and n0 == 0 and "btn.disabled = true" in js and f'data-low="{low}"' in h,
              f"¥{low - 0.10:.2f} 被服务端拒收并提示原因；页面脚本在低于 ¥{low:.2f} 时把采纳按钮变禁用态（请亲眼看一次）")
        high = s4["区间"][2]
        r = c.post("/sku/S04/adopt", data={"price": f"{high + 0.10:.2f}", "back": "/"}, follow_redirects=False)
        a = service.adoptions(connect())[-1]
        done = c.get(r.headers["location"]).text
        R.add("PT-10-3 区间外采纳", a["out_of_range"] == 1 and "区间外采纳" in done, f"¥{high + 0.10:.2f} 可以提交，记录标「区间外采纳」")
        R.add("PT-10-4 弹窗文案", "<dialog" in done and "工具不会改你的售价，请到平台后台改价" in done, "提交后弹层：「工具不会改你的售价，请到平台后台改价」")
        body = c.get("/export/adoptions.csv").content.decode("utf-8-sig")
        head = body.splitlines()[0].split(",")
        R.add("PT-10-5 采纳记录", head[:4] == ["商品编号", "采纳价", "采纳时间", "是否区间外"] and "S04" in body, f"导出列：{head}")
        r1 = c.post("/sku/S03/ignore", data={"reason": "", "back": "/"}, follow_redirects=False)
        r2 = c.post("/sku/S03/ignore", data={"reason": "不认同", "back": "/"}, follow_redirects=False)
        h3 = c.get("/sku/S03").text
        R.add("PT-10-6 忽略要选原因", "err=" in r1.headers["location"] and "done=ignore" in r2.headers["location"]
              and h3.count('name="reason"') == 3 and "required" in h3, "不选原因被拒；选「不认同」后记为已忽略；弹层三个原因必选其一")
        upload(c, sales=S / "销售日记录.csv")
        same = status_of("S03")
        sales = (S / "销售日记录.csv").read_text(encoding="utf-8-sig").replace("S03,2026-09-29,9.9,6,59.40", "S03,2026-09-29,9.9,7,69.30")
        tmp = Path(tempfile.mkdtemp()) / "销售日记录.csv"
        tmp.write_text(sales, encoding="utf-8")
        upload(c, sales=tmp)
        changed = status_of("S03")
        s3 = res_by_sku()["S03"]
        R.add("PT-10-7 忽略后 7 天不重复", same == "已忽略" and changed == "已忽略" and "不再提示" in (s3["状态说明"] or ""),
              f"同样数据重导：{same}；S03 数据变了、建议仍是{__import__('advisor.model', fromlist=['direction']).direction(s3['建议'])}：{changed}（{s3['状态说明']}）")
    with fresh_db() as c:
        import_samples(c)
        a0 = status_of("S10")
        c.get("/sku/S10")
        a1 = status_of("S10")
        R.add("PT-10-8 新建变已查看", (a0, a1) == ("新建", "已查看"), f"S10 新导入后「{a0}」，打开单品页后「{a1}」")
        c.post("/sku/S04/adopt", data={"price": f"{res_by_sku()['S04']['建议']['最优价']:.2f}", "back": "/"})
        run_before = service.current_run(connect())["id"]
        c.post("/sku/S04/inputs", data=inputs_form(c, "S04", 成本价="25"), follow_redirects=False)
        con = connect()
        old4 = con.execute("SELECT status FROM sku_result WHERE run_id = ? AND sku = 'S04'", (run_before,)).fetchone()["status"]
        old3 = con.execute("SELECT status FROM sku_result WHERE run_id = ? AND sku = 'S03'", (run_before,)).fetchone()["status"]
        kept = len(service.adoptions(con))
        R.add("PT-10-9 输入变化过期", old4 == "已过期" and kept == 1 and old3 == "已沿用" and status_of("S04") == "新建",
              f"采纳 S04 后改成本：旧建议「{old4}」，新建议「{status_of('S04')}」，采纳记录 {kept} 条仍在；没改的 S03 旧版「{old3}」")
        pages = "".join(c.get(u).text for u in ("/", "/import", "/report", "/settings", "/sku/S04"))
        buttons = " ".join(_re.findall(r"<(?:a|button)[^>]*>([^<]*)</(?:a|button)>", pages))
        bad = [w for w in ("同步到平台", "一键改价", "自动改价", "改价到平台") if w in buttons]
        R.add("PT-10-10 工具不改价", not bad, "所有按钮与链接里没有「同步到平台」「一键改价」之类的入口" if not bad else f"发现 {bad}")
    fails = sum(1 for r in R.rows if r[1] == "不通过")
    R.add("PT-10-11 自检", fails == 0, "以上机器可判条目全部通过" if not fails else f"{fails} 条不通过")
    return R


# ---------------- PT-11 ----------------

PALETTE = {"#27478C", "#3F6CC4", "#5E88CC", "#7DA1DE", "#ABC2EC", "#FFFFFF"}


def pt11():
    R = Report("PT-11 表现层走查与边界")
    files = list((ROOT / "advisor" / "static").glob("*")) + list((ROOT / "advisor" / "templates").glob("*.html"))
    text = "".join(f.read_text(encoding="utf-8") for f in files)
    colors = {x.upper() for x in _re.findall(r"#[0-9A-Fa-f]{6}\b", text)} | {"#" + x.upper() for x in _re.findall(r"%23([0-9A-Fa-f]{6})", text)}
    named = _re.findall(r"(?<![\w-])(red|green|orange|purple|gray|grey|black|yellow)(?![\w-])", _re.sub(r"<[^>]+>", " ", text))
    R.add("PT-11-1 徽标配色", colors <= PALETTE and not named,
          f"样式与模板里的色值：{sorted(colors)}，只有五个蓝加白色；徽标：贴近成本藏青底白字、偏低淡蓝底、偏高主蓝底白字、合理白底淡蓝边、数据不足白底中蓝虚线")
    with fresh_db() as c:
        import_samples(c)
        pages = {u: c.get(u).text for u in ("/", "/report", "/settings", "/import", "/sku/S04", "/sku/S01")}
        amounts = []
        for u, h in pages.items():
            amounts += _re.findall(r"¥[\d,.-]+", _re.sub(r"<script.*?</script>", "", h, flags=_re.S))
        bad = [a for a in amounts if not _re.fullmatch(r"¥[\d,]+\.\d{1,2}", a)]
        R.add("PT-11-2 金额格式", not bad, f"页面金额 {len(amounts)} 处，都是 ¥ 前缀加一到两位小数（表格可省为一位）" if not bad else f"不合格：{bad[:5]}")
        lst = pages["/"]
        margins = _re.findall(r'<td class="num">(\d+\.\d+%)</td>', lst)
        rep = _re.findall(r'<td class="num strong">(\d+\.\d+%)</td>', pages["/report"])
        chg = _re.findall(r'class="(?:up|down)">([+-][\d.]+%)</span>', pages["/sku/S04"])
        ok = all(_re.fullmatch(r"\d+\.\d%", x) for x in margins + rep) and all(_re.fullmatch(r"[+-]\d+\.\d%", x) for x in chg) and chg
        R.add("PT-11-3 百分比格式", bool(ok), f"列表毛利率如 {margins[:2]}、体检结果如 {rep[:2]} 一位小数；变化如 {chg[:3]} 一位小数带正负号")
        counts = {}
        for u, h in pages.items():
            main = h.split("<main", 1)[1]
            counts[u] = len(_re.findall(r'class="btn[^"]*\bprimary\b', main))
        R.add("PT-11-4 主按钮", all(v <= 1 for v in counts.values()), f"每页正文里的蓝色主按钮数（顶栏「导入数据」除外）：{counts}")
        R.add("PT-11-5 层级", "<dialog" in pages["/sku/S04"] and 'href="/sku/' in pages["/"], "总览 → 单品页 → 采纳 / 忽略弹层，两级页面加一个弹层")
    with fresh_db() as c:
        empty = "还没有数据" in c.get("/").text
        import_samples(c)
        busy = 'data-busy="正在校验…"' in c.get("/import").text
        upload(c, sales=V / "日期格式错_销售日记录.csv")
        err_html = c.get(f"/import?log={last_log()['id']}").text
        upload(c, sales=S / "销售日记录.csv")
        ok_html = c.get(f"/report?log={last_log()['id']}").text
        css = (ROOT / "advisor" / "static" / "style.css").read_text(encoding="utf-8")
        ok = (empty and busy and "已校验" in err_html and "日期格式错误" in err_html and "已导入 12 个商品" in ok_html
              and "已校验" in ok_html and ".btn:disabled" in css)
        R.add("PT-11-6 状态反馈", ok, "空状态「还没有数据」→ 上传时按钮变「正在校验…」→ 出错「已校验 N 行」加错误清单 → 成功提示 → 采纳价非法时禁用态（请亲眼各看一次）")
    R.add("PT-11-7 最小宽度", None, "浏览器调到 1280 宽看各页：开发时已在 1280 宽下核过 scrollWidth = clientWidth，没有横向滚动")
    R.add("PT-11-8 线框一致", None, "对照 PRD 图 P3、P4：列表的统计卡、筛选胶囊、表格；单品页三栏加底部采纳栏")
    ext = _re.findall(r"(?:src|href)=\"(https?://[^\"]+)\"", text) + _re.findall(r"url\((https?://[^)]+)\)", text) + _re.findall(r"fetch\(\s*[\"'`](https?://)", text)
    R.add("PT-11-9 断网可用", not ext, "模板、样式、脚本里没有任何外部地址，运行时不发外部请求；关网走一遍请亲自做" if not ext else f"发现外部地址 {ext}")
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "smoke.py")], cwd=ROOT, capture_output=True, text=True)
    R.add("PT-11-10 冒烟脚本", p.returncode == 0, (p.stdout.strip().splitlines() or ["无输出"])[-1])
    words = [w for w in ("跨店", "多店", "切换店铺", "店铺列表") if w in text]
    R.add("PT-11-11 单店隔离", not words, "一个库只放一家店（data/advisor.db）；页面没有跨店汇总或切换店铺的入口")
    fails = sum(1 for r in R.rows if r[1] == "不通过")
    R.add("PT-11-12 自检", fails == 0, "以上机器可判条目全部通过" if not fails else f"{fails} 条不通过")
    return R


# ---------------- PT-12 ----------------

def pt12():
    R = Report("PT-12 目标店试用准备")

    def restart():
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "keep.db")
            s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
            uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
            counts = []
            for i in range(2):
                pr = subprocess.Popen([uv, "run", "advisor", "--db", db, "--port", str(port), "--no-browser"], cwd=ROOT,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    t0 = time.time()
                    while time.time() - t0 < 30:
                        try:
                            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                            break
                        except OSError:
                            time.sleep(0.3)
                    if i == 0:
                        old = os.environ.get("ADVISOR_DB")
                        os.environ["ADVISOR_DB"] = db
                        import_samples(TestClient(app))
                        if old:
                            os.environ["ADVISOR_DB"] = old
                    html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
                    counts.append(len(list_skus(html)))
                finally:
                    pr.terminate(); pr.wait(5)
            return counts[0] == counts[1] == 12, f"导入后停掉服务再启动，列表仍是 {counts[1]} 个商品；数据在 data/advisor.db，双击 scripts/启动定价参谋.command 或 uv run advisor"
    R.check("PT-12-1 重启可用", restart)

    def bak():
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, ADVISOR_DB=str(Path(d) / "advisor.db"))
            old = os.environ.get("ADVISOR_DB")
            os.environ["ADVISOR_DB"] = env["ADVISOR_DB"]
            import_samples(TestClient(app))
            n0 = connect().execute("SELECT COUNT(*) FROM sales_day").fetchone()[0]
            uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
            out = subprocess.run([uv, "run", "advisor-backup"], cwd=ROOT, env=env, capture_output=True, text=True).stdout
            f = Path(out.strip().split("已备份到 ")[-1])
            for suf in ("", "-wal", "-shm"):
                Path(env["ADVISOR_DB"] + suf).unlink(missing_ok=True)
            subprocess.run([uv, "run", "advisor-restore", str(f)], cwd=ROOT, env=env, capture_output=True, text=True)
            n1 = connect().execute("SELECT COUNT(*) FROM sales_day").fetchone()[0]
            if old:
                os.environ["ADVISOR_DB"] = old
            dated = bool(_re.search(r"定价参谋-\d{8}-\d{6}\.db$", f.name))
            return n0 == n1 and dated, f"uv run advisor-backup 得到 {f.name}；删库后 uv run advisor-restore 恢复，销售记录 {n0} → {n1} 行"
    R.check("PT-12-2 备份恢复", bak)
    R.add("PT-12-3 目标店导入", None, "要目标店真实数据，延到门 0 数据到手后做")
    R.add("PT-12-4 试用说明", None, "打开 docs/试用说明.md，看是否一页讲清每周一导入、看列表、采纳、工具不改价")
    with fresh_db() as c:
        import_samples(c)
        c.get("/sku/S04"); c.get("/sku/S08"); c.get("/sku/S03")
        for k in ("S04", "S08"):
            c.post("/sku/" + k + "/adopt", data={"price": f"{res_by_sku()[k]['建议']['最优价']:.2f}", "back": "/"})
        body = c.get("/export/gate1.csv").content.decode("utf-8-sig")
        g = service.gate1(connect())
        R.add("PT-12-5 门 1 评估导出", body.startswith("采纳率,2 ÷ 3") and "对照" in body and "采纳前" in body and "差值" in body,
              f"采纳率 2 ÷ 3；每个采纳 SKU 有对照、前后 14 天毛利率、差值（样例店没有采纳后的记录，采纳后一栏记「记录不足」）")
        by = res_by_sku()
        ok = True
        msg = []
        for r in g["rows"]:
            me = by[r["商品编号"]]
            cands = [x for x in by.values() if x["类目"] == me["类目"] and x["商品编号"] not in ("S04", "S08")
                     and x["数据状态"] == "正常" and x["库存天数"] is not None]
            want = min(cands, key=lambda x: (abs(x["库存天数"] - me["库存天数"]), x["商品编号"]))["商品编号"] if cands else None
            ok &= r["对照"] == want
            msg.append(f"{r['商品编号']}（{me['类目']}，{me['库存天数']:.1f} 天）→ {r['对照']}")
        R.add("PT-12-6 对照挑选", ok, "；".join(msg) + "：同店未采纳、同类目、库存天数最接近")
        pages = "".join(c.get(u).text for u in ("/", "/import", "/report", "/settings", "/sku/S04"))
        entries = " ".join(_re.findall(r"<(?:a|button)[^>]*>([^<]*)</(?:a|button)>", pages))
        bad = [w for w in ("回看", "预警", "投放毛利", "看板", "店员", "价格测试") if w in entries]
        R.add("PT-12-7 不做的都没做", not bad, "按钮与链接里没有回看、预警、投放毛利、看板、店员、价格测试的入口" if not bad else f"发现 {bad}")
    return R


STEPS = {"PT-01": pt01, "PT-02": pt02, "PT-03": pt03, "PT-04": pt04, "PT-05": pt05, "PT-06": pt06, "PT-07": pt07,
         "PT-08": pt08, "PT-09": pt09, "PT-10": pt10, "PT-11": pt11, "PT-12": pt12}

if __name__ == "__main__":
    steps = sys.argv[1:] or list(STEPS)
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        print(f"没有这些步骤的自检：{unknown}；可用 {list(STEPS)}")
        sys.exit(2)
    total = sum(STEPS[s]().show() for s in steps)
    sys.exit(1 if total else 0)
