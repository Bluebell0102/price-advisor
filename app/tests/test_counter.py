"""PT-06-1：门 0 反例六类进自动测试，每类断言 PRD 12.4 的正确表现。反例集来自 gate0 脚本，放在 samples/反例集。"""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("accept", Path(__file__).resolve().parent.parent / "scripts" / "accept.py")
accept = importlib.util.module_from_spec(spec)
spec.loader.exec_module(accept)


@pytest.fixture(scope="module")
def checks():
    with accept.fresh_db() as c:
        accept.import_counter(c)
        yield {k: (ok, msg) for k, ok, msg in accept.counter_checks(c)}


@pytest.mark.parametrize("kind", ["弹性低库存高", "竞品低毛利薄", "新品无历史", "无动销", "活动日异常", "成本缺失"])
def test_counter_example(checks, kind):
    ok, msg = checks[kind]
    assert ok, msg
