"""严格解码与解码失败的结构化描述。

一条铁律：**不能严格解码的文件，绝不做整体转码**。
"用 errors='replace' 硬转"会把原本还能救的字节永久替换成 U+FFFD，
这是编码工具最不可原谅的失败方式——它不报错，但结果不对。
所以这里把失败原因做成结构化的 :class:`DecodeError`，交给上层决定：
定位、分段处理，或者干脆拒绝。
"""

from __future__ import annotations

from .encodings import Codec, match_bom


class DecodeError(Exception):
    """严格解码失败。带上偏移、原始字节和中文原因，便于直接展示给用户。"""

    def __init__(self, codec: Codec, offset: int, raw: bytes, reason: str, total: int):
        super().__init__(
            f"{codec.name} 严格解码失败于偏移 {offset}（共 {total} 字节）：{reason}"
        )
        self.codec = codec
        self.offset = offset
        self.raw = raw
        self.reason = reason
        self.total = total


def strip_bom_for(data: bytes, codec: Codec) -> tuple:
    """若开头是该编码的 BOM 则剥掉，返回 ``(载荷, 剥掉的 BOM)``。

    显式指定 ``--from`` 时也要剥 BOM：否则 U+FEFF 会作为内容被写进新文件。
    """
    hit = match_bom(data)
    if hit:
        name, payload = hit
        if name == codec.name:
            return payload, data[: len(data) - len(payload)]
        # BOM 与实际编码不一致：保留原样，让解码去暴露问题，而不是悄悄丢掉 BOM。
    return data, b""


def decode_strict(data: bytes, codec: Codec) -> str:
    """按 ``codec`` 严格解码；失败抛 :class:`DecodeError`。"""
    try:
        return data.decode(codec.python_codec)
    except UnicodeDecodeError as e:
        raw = bytes(e.object[e.start:e.end]) if isinstance(e.object, (bytes, bytearray)) else b""
        raise DecodeError(codec, e.start, raw, _explain(e), len(data)) from None


def decode_lenient(data: bytes, codec: Codec) -> str:
    """宽松解码（替换非法字节）。只用于**展示与定位**，不用于落地写文件。"""
    return data.decode(codec.python_codec, errors="replace")


def _explain(e: UnicodeDecodeError) -> str:
    """把 Python 的英文报错翻译成能直接贴给同事看的中文。"""
    raw = bytes(e.object[e.start:e.end]) if isinstance(e.object, (bytes, bytearray)) else b""
    if not raw:
        return "序列在文件末尾被截断"
    head = raw[0]
    if head >= 0x80 and len(raw) == 1:
        return f"连续字节 0x{head:02X} 不是合法的多字节序列开头（或序列被截断）"
    return f"字节序列 {raw.hex(' ')} 不构成合法字符"
