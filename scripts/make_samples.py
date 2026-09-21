"""生成"编码试验场"：一批刻意用不同编码保存的文件，用来手工验证与回归。

语料本体在 ``tests/fixtures.py``——测试与演示**共用同一份样本**，
免得出现"演示跑得通、测试对不上"这种两套数据打架的情况。

用法::

    python scripts/make_samples.py            # 生成到 ./samples
    python -m obt sniff samples --explain
    python -m obt scan samples
    python -m obt convert samples --to utf-8-bom --eol lf --dry-run

脚本末尾会把"预期编码 / 实际嗅探结果"做成对照表，
一眼能看出判定引擎有没有跑偏。判定不符时以退出码 1 结束，可直接进 CI。
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))                 # 让 tests 包可被导入

from obt.core import detect                    # noqa: E402
from obt.render import configure_stdio         # noqa: E402
from tests.fixtures import corpus, matches     # noqa: E402

OUT = ROOT / "samples"


def build() -> list:
    """把语料写到 samples/，返回 ``[(Path, 字节, 期望, 说明)]``。"""
    OUT.mkdir(parents=True, exist_ok=True)
    items = []
    for name, data, expect, note in corpus():
        path = OUT / name
        path.write_bytes(data)
        items.append((path, data, expect, note))
    return items


def report(items: list) -> int:
    print(f"{'文件':<24} {'期望':<12} {'实际':<14} {'置信':>5}  {'BOM':<4} "
          f"{'EOL':<5} 判定")
    print("-" * 100)
    bad = 0
    for path, data, expect, note in items:
        det = detect(data)
        ok = matches(det, expect)
        bad += 0 if ok else 1
        verdict = note
        if not ok:
            verdict = f"!! 期望 {expect}；{det.evidence[0]}"
        elif det.note:
            verdict = det.note
        print(f"{'OK ' if ok else '!! '}{path.name:<21} {expect:<12} "
              f"{det.label:<14} {det.confidence:>5.2f}  "
              f"{'有' if det.has_bom else '无':<4} {det.eol:<5} {verdict}")
    print("-" * 100)
    print(f"共 {len(items)} 个样本，判定不符 {bad} 个")
    return 1 if bad else 0


if __name__ == "__main__":
    # 对照表里全是中文。GitHub Actions 的 windows-latest 上 stdout 是 cp1252，
    # 不先调这一步的话 print 直接 UnicodeEncodeError、退出码 1——而"退出码 1"
    # 恰好是脚本用来表示"判定不符"的信号，两者会撞车，非常难查。
    configure_stdio()
    sys.exit(report(build()))
