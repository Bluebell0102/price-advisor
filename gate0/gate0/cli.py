"""uv run gate0 <数据目录> [--out 输出目录]

数据目录里放：商品表.csv、销售日记录.csv、竞品价格.csv（可选）、店铺参数.json、列名映射.json（可选）。
输出：门0体检.xlsx、答案表.csv、反例集/ 三张表。
"""
import argparse
import sys
from pathlib import Path

from .calc import analyze
from .io import load
from .report import write_answers_csv, write_counter_set, write_excel


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gate0", description="门 0 体检")
    ap.add_argument("data_dir")
    ap.add_argument("--out", default=None, help="输出目录，默认 <数据目录>/门0输出")
    a = ap.parse_args(argv)
    data = Path(a.data_dir)
    out = Path(a.out) if a.out else data / "门0输出"
    out.mkdir(parents=True, exist_ok=True)
    try:
        ds = load(data)
    except ValueError as e:
        print(f"读入失败：{e}", file=sys.stderr)
        return 2
    res = analyze(ds)
    write_excel(ds, res, out / "门0体检.xlsx")
    write_answers_csv(res, out / "答案表.csv")
    write_counter_set(ds, res, out / "反例集")
    sm = res["summary"]
    fmt = lambda x: "—" if x is None else f"{x:.1%}"
    print(f"数据 {sm['首日']} 到 {sm['今天']}（{sm['覆盖天数']} 天），SKU {sm['SKU 总数']} 个，有成本价 {sm['有成本价 SKU']} 个，数据不足 {len(sm['数据不足'])} 个")
    print(f"需要动价 {fmt(sm['需要动价占比'])}（判据 ≥ {ds.params['需要动价占比判据']:.0%}），库存超 {ds.params['滞销线']} 天 {fmt(sm['库存超滞销线占比'])}（判据 ≥ {ds.params['库存超滞销线占比判据']:.0%}）")
    print(f"H1：{'通过' if sm['H1'] else '不通过'}")
    if ds.issues:
        print(f"校验问题 {len(ds.issues)} 条，见 Excel「数据覆盖与校验」")
    print(f"输出：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
