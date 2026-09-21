"""混合编码分析：当一个文件"一半 UTF-8、一半 GBK"时，告诉你具体在哪一行、哪个偏移。

这是 :mod:`obt.core.detect` 的必要补充。detect 回答"整份文件是什么编码"，
本模块回答"整份文件不是单一编码时，坏在哪"。两者的分工对应两种真实场景：

* 单个文件疑似编码错了      → detect + ``--from`` 覆盖
* 仓库里混着历史遗留文件    → mixed 精确定位，改代码只改该改的那几行
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field

from .decode import decode_lenient
from .detect import detect
from .encodings import F_UNICODE, Codec

# 逐行分析的上限。超过就只报"前 N 行"的结论，不假装自己看完了整份文件。
DEFAULT_LINE_LIMIT = 2000


@dataclass
class UndecodableRun:
    """一段无法按目标编码解码的字节。"""

    offset: int
    length: int
    raw: bytes
    before: str = ""
    after: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "offset": self.offset,
            "length": self.length,
            "raw_hex": self.raw.hex(" "),
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
        }


@dataclass
class LineAnomaly:
    """一行与主要编码不一致的情况。"""

    line_no: int
    offset: int
    encoding: str
    confidence: float
    preview: str

    def as_dict(self) -> dict:
        return {
            "line": self.line_no,
            "offset": self.offset,
            "encoding": self.encoding,
            "confidence": round(self.confidence, 4),
            "preview": self.preview,
        }


@dataclass
class LineReport:
    """逐行编码分布。"""

    dominant: str
    total_lines: int
    analyzed: int = 0
    counts: dict = field(default_factory=dict)
    anomalies: list = field(default_factory=list)
    skipped: str = ""

    @property
    def mixed(self) -> bool:
        return bool(self.anomalies)

    @property
    def encodings_seen(self) -> list:
        return sorted(self.counts)

    def as_dict(self) -> dict:
        return {
            "dominant": self.dominant,
            "total_lines": self.total_lines,
            "analyzed": self.analyzed,
            "counts": self.counts,
            "mixed": self.mixed,
            "skipped": self.skipped,
            "anomalies": [a.as_dict() for a in self.anomalies],
        }


def find_undecodable(data: bytes, codec: Codec, *, limit: int = 20) -> list:
    """定位无法按 ``codec`` 严格解码的字节段。

    做法是喂给增量解码器，捕到 ``UnicodeDecodeError`` 就记下位置、
    重置解码器、从出错点之后继续，因此能一次报出**多处**问题。

    一个细节值得说明：增量解码器报出的错误区间常常只有 1 个字节，
    从出错点之后一字节继续，就会在同一段坏数据里连着报很多次（锯齿）。
    所以这里先把字节级结果按**行**合并——同一行内的问题本质上就是同一个
    问题（这一行用了别的编码），合并成一段才对人有意义。

    复杂度是 O(剩余字节 × 出错次数)，因此两道限流：累计出错段数有软上限，
    最终返回条数由 ``limit`` 收口。
    """
    factory = codecs.getincrementaldecoder(codec.python_codec)
    raw_runs = []
    pos, n = 0, len(data)
    step_cap = max(limit * 64, 256)
    steps = 0
    while pos < n and steps < step_cap:
        steps += 1
        dec = factory(errors="strict")
        try:
            dec.decode(data[pos:], final=True)
            break
        except UnicodeDecodeError as e:
            start = pos + e.start
            end = pos + e.end
            if end <= start:
                end = start + 1
            raw_runs.append(UndecodableRun(
                offset=start,
                length=end - start,
                raw=data[start:end],
                before=_context(data, codec, start, -1),
                after=_context(data, codec, end, +1),
                reason=e.reason,
            ))
            pos = end
    return _merge_by_line(data, raw_runs)[:limit]


def _merge_by_line(data: bytes, runs: list) -> list:
    """把同一行内的若干段坏字节合并成一段。"""
    merged = []
    for run in runs:
        if merged:
            prev = merged[-1]
            gap = data[prev.offset + prev.length: run.offset]
            if b"\n" not in gap:
                end = run.offset + run.length
                merged[-1] = UndecodableRun(
                    offset=prev.offset, length=end - prev.offset,
                    raw=data[prev.offset:end], before=prev.before,
                    after=run.after, reason=prev.reason,
                )
                continue
        merged.append(run)
    return merged


def _context(data: bytes, codec: Codec, offset: int, direction: int, width: int = 24) -> str:
    """取出错点前后的上下文，方便肉眼判断"这行本来是中文还是英文"。"""
    if direction < 0:
        chunk = data[max(0, offset - width):offset]
    else:
        chunk = data[offset:offset + width]
    return decode_lenient(chunk, codec).replace("\r", " ").replace("\n", " ")


def line_encodings(data: bytes, *, dominant: Codec,
                   limit: int = DEFAULT_LINE_LIMIT,
                   anomaly_limit: int = 20) -> LineReport:
    """逐行检查编码一致性，返回行级报告。

    按 ``b"\\n"`` 切行对所有目标编码都安全——GBK / Big5 / Shift_JIS / EUC
    的双字节尾部都不包含 0x0A。UTF-16/32 例外，会直接跳过并说明原因。
    """
    if dominant.family == F_UNICODE and dominant.name.startswith(("utf-16", "utf-32")):
        return LineReport(
            dominant=dominant.name, total_lines=0,
            skipped=f"{dominant.name} 按字节切行不安全，已跳过逐行分析",
        )

    lines = data.split(b"\n")
    # 末尾换行会 split 出一个空元素，那不是一行
    if len(lines) > 1 and lines[-1] == b"":
        lines.pop()
    report = LineReport(dominant=dominant.name, total_lines=len(lines))
    offset = 0
    for idx, raw in enumerate(lines, 1):
        if idx > limit:
            report.skipped = f"仅分析了前 {limit} 行（共 {len(lines)} 行）"
            break
        report.analyzed += 1
        # 行尾的 \r 不影响判定，去掉可让 ASCII 行更准地归到 ascii 类
        probe = raw[:-1] if raw.endswith(b"\r") else raw
        try:
            probe.decode(dominant.python_codec)
            key = "ascii" if not any(b >= 0x80 for b in probe) else dominant.name
            report.counts[key] = report.counts.get(key, 0) + 1
        except UnicodeDecodeError:
            if len(report.anomalies) < anomaly_limit:
                det = detect(probe)
                report.anomalies.append(LineAnomaly(
                    line_no=idx, offset=offset, encoding=det.encoding,
                    confidence=det.confidence,
                    preview=decode_lenient(probe, _codec_for(det))[:60],
                ))
            report.counts["<异常>"] = report.counts.get("<异常>", 0) + 1
        offset += len(raw) + 1  # +1 是被 split 掉的 \n
    return report


def _codec_for(det) -> Codec:
    """从一次 Detection 反查 Codec，取不到就用 latin-1 兜底展示。"""
    from .encodings import BY_NAME
    return BY_NAME.get(det.encoding, BY_NAME["latin-1"])
