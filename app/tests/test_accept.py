"""把自检命令当回归测试跑：PT-02 到 PT-12 机器可判的条目不许有「不通过」。"""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("accept", Path(__file__).resolve().parent.parent / "scripts" / "accept.py")
accept = importlib.util.module_from_spec(spec)
spec.loader.exec_module(accept)


@pytest.mark.parametrize("step", ["PT-02", "PT-03", "PT-04", "PT-05", "PT-06", "PT-07", "PT-08", "PT-09", "PT-10", "PT-11", "PT-12"])
def test_step(step):
    rep = accept.STEPS[step]()
    fails = [r for r in rep.rows if r[1] == "不通过"]
    assert not fails, fails
