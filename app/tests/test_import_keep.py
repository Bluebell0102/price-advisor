"""导入时成本价、生命周期标记那一格是空的，保留库里原来的值，并在导入结果里说明。"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
S = ROOT / "samples" / "样例店"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADVISOR_DB", str(tmp_path / "t.db"))
    from advisor.main import app
    return TestClient(app)


def up(c, **files):
    return c.post("/import", files={k: (Path(v).name, Path(v).read_bytes(), "text/csv") for k, v in files.items()},
                  follow_redirects=False)


def test_page_cost_and_stage_survive_reimport(client):
    from advisor import service
    from advisor.db import connect
    up(client, products=S / "商品表.csv", sales=S / "销售日记录.csv")
    client.post("/sku/S07/cost", data={"cost": "10"})
    client.post("/sku/S03/stage", data={"stage": "尾货期"})
    r = up(client, products=S / "商品表.csv")
    by = {x["商品编号"]: x for x in service.results(connect())}
    assert by["S07"]["成本价"] == 10 and by["S07"]["保本价"] == 14.77 and by["S07"]["数据状态"] == "正常"
    assert by["S03"]["生命周期"] == "尾货期" and by["S03"]["生命周期来源"] == "店主标记"
    log_id = int(r.headers["location"].split("log=")[-1])
    html = client.get(f"/report?log={log_id}").text
    assert "S07 成本价格为空，沿用页面补录的 ¥10.00" in html
    assert "S03 生命周期标记格为空，沿用手动标记的尾货期" in html
    assert "S07 成本价缺失" not in html


def test_import_value_still_wins(client, tmp_path):
    from advisor import service
    from advisor.db import connect
    up(client, products=S / "商品表.csv", sales=S / "销售日记录.csv")
    client.post("/sku/S07/cost", data={"cost": "10"})
    fixed = (S / "商品表.csv").read_text(encoding="utf-8-sig").replace("S07,收纳袋,收纳,2026-04-02,19.9,,90,", "S07,收纳袋,收纳,2026-04-02,19.9,11,90,")
    p = tmp_path / "商品表.csv"
    p.write_text(fixed, encoding="utf-8")
    up(client, products=p)
    by = {x["商品编号"]: x for x in service.results(connect())}
    assert by["S07"]["成本价"] == 11
