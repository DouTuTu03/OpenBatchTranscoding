"""终端渲染：表格对齐、颜色、进度条。

单独抽出来是因为**中英混排的表格宽度必须按显示宽度算**：
``len("编码")`` 是 2，但屏幕上占 4 列。用 East Asian Width
把宽字符算 2 列，表格才不会歪。

渲染层不含任何判定逻辑，纯字符串进、字符串出。
"""

from __future__ import annotations

import sys
import unicodedata

_CODES = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "grey": "\033[90m",
}

# 状态标记：刻意全用 GBK 也能表示的字符，避免中文 Windows 控制台炸掉
MARK_CONVERT = "[改]"
MARK_SKIP = "[跳]"
MARK_ERROR = "[拒]"
MARK_OK = "[OK]"
MARK_BAD = "[!!]"


class Palette:
    """颜色开关。非 TTY / --color never / --json 时全部退化为空串。"""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        prefix = "".join(_CODES.get(s, "") for s in styles)
        return f"{prefix}{text}{_CODES['reset']}"

    def bold(self, text: str) -> str:
        return self(text, "bold")

    def dim(self, text: str) -> str:
        return self(text, "dim")


def make_palette(mode: str = "auto") -> Palette:
    """``auto`` 只在输出是终端时才上色（管道给别的程序时不污染）。"""
    if mode == "always":
        return Palette(True)
    if mode == "never":
        return Palette(False)
    try:
        return Palette(bool(sys.stdout.isatty()))
    except Exception:
        return Palette(False)


# ------------------------------------------------------------------ 宽度计算
def dwidth(text: str) -> int:
    """字符串在等宽终端里占的列数。"""
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text: str, width: int, align: str = "l") -> str:
    """按显示宽度补齐。``align`` 取 ``l`` / ``r`` / ``c``。"""
    gap = width - dwidth(text)
    if gap <= 0:
        return text
    if align == "r":
        return " " * gap + text
    if align == "c":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def table(headers, rows, *, aligns=None, indent: str = "  ") -> str:
    """渲染一张对齐的表格。``rows`` 里的单元格可以是已上色的字符串。"""
    if not rows:
        return ""
    widths = [dwidth(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], dwidth(str(cell)))
    aligns = aligns or ["l"] * len(headers)

    lines = [indent + "  ".join(pad(str(h), widths[i], aligns[i])
                                for i, h in enumerate(headers))]
    lines.append(indent + "  ".join("-" * w for w in widths))
    for row in rows:
        lines.append(indent + "  ".join(
            pad(str(c), widths[i], aligns[i]) for i, c in enumerate(row)
        ))
    return "\n".join(lines)


def human_size(n: int) -> str:
    """1024 进制的人读大小。"""
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024.0
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} GB"


def bar(count: int, total: int, *, width: int = 28, char: str = "#") -> str:
    """分布条。用 ASCII 字符画出比例，保证任何控制台都能打印。"""
    if total <= 0:
        return ""
    filled = int(round(count / total * width))
    return char * filled + "." * (width - filled)


def percent(part: int, whole: int) -> str:
    if whole <= 0:
        return "-"
    return f"{part / whole * 100:.1f}%"


def heading(text: str, palette: Palette, *, indent: str = "") -> str:
    return indent + palette(text, "bold")


def section(title: str, palette: Palette, *, indent: str = "  ") -> str:
    return f"{indent}{palette(title, 'bold', 'cyan')}"


def wrap_evidence(items, palette: Palette, *, indent: str = "    ",
                  per_line: int = 3) -> str:
    """证据逐条列出，长列表折成多行，便于粘贴到 issue 里讨论。"""
    out = []
    for i, item in enumerate(items, 1):
        out.append(f"{indent}{palette(str(i) + '.', 'dim')} {item}")
        if i % per_line == 0 and i != len(items):
            out.append("")
    return "\n".join(out)
