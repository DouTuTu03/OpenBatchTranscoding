"""行尾（换行符）检测与转换。

单独成模块的理由：换行符和字符集是两件正交的事，但转码时会被一起
写回文件。如果只改编码却把 CRLF 顺手改成 LF，整个仓库的 diff 都会炸——
所以这里显式拆开，默认策略是 ``keep``。
"""

from __future__ import annotations

EOL_LF = "lf"
EOL_CRLF = "crlf"
EOL_CR = "cr"
EOL_MIXED = "mixed"
EOL_NONE = "none"

# CLI 的 --eol 取值
EOL_CHOICES = ("keep", EOL_LF, EOL_CRLF, EOL_CR)

_LABELS = {
    EOL_LF: "LF",
    EOL_CRLF: "CRLF",
    EOL_CR: "CR",
    EOL_MIXED: "混合",
    EOL_NONE: "无换行",
}


def detect_eol(text: str) -> str:
    """判断文本的主要换行风格。"""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    kinds = [k for k, n in ((EOL_CRLF, crlf), (EOL_LF, lf), (EOL_CR, cr)) if n > 0]
    if not kinds:
        return EOL_NONE
    if len(kinds) > 1:
        return EOL_MIXED
    return kinds[0]


def eol_label(style: str) -> str:
    """``lf`` -> ``LF``，用于表格展示。"""
    return _LABELS.get(style, style)


def eol_literal(style: str) -> str:
    """``lf`` -> ``\\n``，用于 --explain 展示真实字节。"""
    return {EOL_LF: "\\n", EOL_CRLF: "\\r\\n", EOL_CR: "\\r"}.get(style, "")


def apply_eol(text: str, style: str) -> str:
    """把文本换行统一到 ``style``；``keep`` 表示原样返回。

    先把所有风格归一成 ``\\n`` 再展开，避免 ``\\r\\n`` 被二次处理成 ``\\r\\r\\n``。
    """
    if style == "keep":
        return text
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if style == EOL_LF:
        return normalized
    if style == EOL_CRLF:
        return normalized.replace("\n", "\r\n")
    if style == EOL_CR:
        return normalized.replace("\n", "\r")
    raise ValueError(f"不支持的换行风格：{style!r}（可选：{'/'.join(EOL_CHOICES)}）")


def count_eol(text: str) -> dict:
    """把三种换行符的个数都数出来，供 --explain 核对"混合行尾"的判断。"""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    return {"crlf": crlf, "lf": lf, "cr": cr}
