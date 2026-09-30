"""冒烟：从空库走到采纳一条（PT-11-10）。uv run python scripts/smoke.py

空库 → 首页空状态 → 上传样例店三张表 → 确认店铺参数 → 体检报告 → 健康度列表 → 打开单品页 → 采纳最优价 → 导出采纳记录。
用临时库，不碰 data/advisor.db；任何一步不对就退出码 1。
"""
import sys
import tempfile
import os
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory() as d:
        os.environ["ADVISOR_DB"] = str(Path(d) / "smoke.db")
        from fastapi.testclient import TestClient
        from advisor.main import app
        c = TestClient(app)
        S = ROOT / "samples" / "样例店"
        steps = []

        def step(name, ok, detail=""):
            steps.append((name, ok, detail))
            print(f"{'通过' if ok else '失败'}  {name}  {detail}")
            if not ok:
                raise SystemExit(1)

        step("空库首页是空状态", "还没有数据" in c.get("/").text)
        files = {k: (f, (S / f).read_bytes(), "text/csv") for k, f in
                 (("products", "商品表.csv"), ("sales", "销售日记录.csv"), ("competitors", "竞品价格.csv"))}
        r = c.post("/import", files=files, follow_redirects=False)
        step("上传样例店三张表", r.status_code == 303 and "first=1" in r.headers["location"], r.headers["location"])
        page = c.get(r.headers["location"]).text
        form = {k: v for k, v in (("平台", "其他"), ("扣点率", "5"), ("单件履约费", "3.5"), ("退货率", "6"), ("单次退货净损失占比", "60"),
                                  ("最低毛利率", "5"), ("尾货期最低毛利率", "0"), ("目标清仓天数", "45"), ("滞销线", "90"),
                                  ("first", "1"), ("log", r.headers["location"].split("log=")[-1]))}
        import re
        for m in re.finditer(r'name="(el_(?:base|adj)_\d+)" value="([^"]*)"', page):
            form[m.group(1)] = m.group(2)
        r = c.post("/settings", data=form, follow_redirects=False)
        step("确认店铺参数后进体检报告", r.headers["location"].startswith("/report"), r.headers["location"])
        rep = c.get(r.headers["location"]).text
        step("体检报告有导入提示与指标", "已导入 12 个商品、60 天记录，其中 3 个数据不足" in rep and "需要动价 SKU 占比" in rep)
        lst = c.get("/").text
        step("健康度列表贴近成本置顶", lst.index("S02") < lst.index("S04"))
        sku = c.get("/sku/S04").text
        step("打开 S04 单品页", "为什么建议这个价" in sku and "采纳" in sku)
        opt = re.search(r'id="adopt-price" type="text" name="price" value="([\d.]+)"', sku).group(1)
        r = c.post("/sku/S04/adopt", data={"price": opt, "back": "/"}, follow_redirects=False)
        done = c.get(r.headers["location"]).text
        step("采纳最优价", "done=adopt" in r.headers["location"] and "工具不会改你的售价，请到平台后台改价" in done, f"¥{opt}")
        csv_text = c.get("/export/adoptions.csv").content.decode("utf-8-sig")
        step("采纳记录可导出", "S04" in csv_text and opt in csv_text)
        print(f"\n冒烟通过：{len(steps)} 步")


if __name__ == "__main__":
    main()
