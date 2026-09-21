"""转码计划：先算出"要对哪些文件做什么"，再谈动手。

把「决定」与「动手」分成两步，是这个小工具最要紧的设计：

* ``build_plan`` 是纯计算，读文件、嗅探、编码，得出每个文件该不该改、改成什么样
* ``apply_actions`` 只负责把已经算好的字节落盘

于是 ``--dry-run`` 和真跑走的是**同一套判定**，不存在
"预演看着没事、真跑翻车"的落差。计划本身也可以直接 ``--json`` 出去，
给 CI、给别的程序、给将来的图形界面消费。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .detect import Detection, detect
from .encodings import Codec, bom_of
from .eol import apply_eol, detect_eol, eol_label

# Action.status 取值
STATUS_CONVERT = "convert"
STATUS_SKIP = "skip"
STATUS_ERROR = "error"


def transcode_text(text: str, dst: Codec, *, write_bom: bool, eol: str) -> bytes:
    """把文本按目标编码 / 换行 / BOM 要求编成字节。可能抛 ``UnicodeEncodeError``。"""
    normalized = apply_eol(text, eol)
    payload = normalized.encode(dst.python_codec)
    prefix = bom_of(dst) if write_bom else b""
    return prefix + payload


def unencodable_chars(text: str, dst: Codec, *, limit: int = 5) -> list:
    """找出目标编码表示不了的字符，用于给出人话报错。

    典型场景：文件里有 emoji 或生僻字，转 GBK 必然失败。
    与其让 Python 抛一串堆栈，不如直接告诉用户是哪个字。
    """
    bad = []
    seen = set()
    for ch in text:
        if ch in seen:
            continue
        try:
            ch.encode(dst.python_codec)
        except UnicodeEncodeError:
            seen.add(ch)
            bad.append(ch)
            if len(bad) >= limit:
                break
    return bad


@dataclass
class Action:
    """单个文件的处理计划。"""

    path: Path
    status: str
    reason: str = ""
    src_encoding: str = ""
    dst_encoding: str = ""
    src_eol: str = ""
    dst_eol: str = ""
    src_bom: bytes = b""
    dst_bom: bytes = b""
    size_before: int = 0
    size_after: int = 0
    chars: int = 0
    payload: bytes = field(default=b"", repr=False)
    detection: Detection = field(default=None, repr=False)

    @property
    def changed(self) -> bool:
        return self.status == STATUS_CONVERT

    @property
    def delta(self) -> int:
        return self.size_after - self.size_before

    def as_dict(self, *, include_payload: bool = False) -> dict:
        d = {
            "path": str(self.path),
            "status": self.status,
            "reason": self.reason,
            "from": self.src_encoding,
            "to": self.dst_encoding,
            "eol": {"from": self.src_eol, "to": self.dst_eol},
            "bom": {"from": self.src_bom.hex(" "), "to": self.dst_bom.hex(" ")},
            "size": {"before": self.size_before, "after": self.size_after},
            "chars": self.chars,
        }
        if self.detection is not None:
            d["detection"] = self.detection.as_dict(with_candidates=False)
        if include_payload:
            d["payload_hex"] = self.payload.hex(" ")
        return d


def build_plan(files, *, to: Codec, write_bom: bool, eol: str = "keep",
               from_codec: Codec = None, only_mismatch: bool = False,
               skip_binary: bool = True, min_confidence: float = 0.6,
               max_size: int = None, refine_confidence: bool = True) -> list:
    """为每个文件算出处理计划。

    拒绝转码的四种情形（都会写成 ``status=error``，绝不静默放过）：

    1. 判定为非文本（二进制）
    2. 无法严格解码（混合编码 / 损坏）——硬转会把坏字节永久变成 U+FFFD
    3. 置信度低于 ``min_confidence``——猜错就是内容损毁，宁可让人来定
    4. 目标编码表示不了其中某些字符（如 emoji 转 GBK）
    """
    plan = []
    for path in files:
        try:
            size = path.stat().st_size
        except OSError as e:
            plan.append(Action(path=path, status=STATUS_ERROR, reason=f"无法读取文件属性：{e}"))
            continue

        if max_size is not None and size > max_size:
            plan.append(Action(
                path=path, status=STATUS_SKIP,
                reason=f"超过 --max-size（{size} > {max_size} 字节）",
                size_before=size, size_after=size,
            ))
            continue

        try:
            data = path.read_bytes()
        except OSError as e:
            plan.append(Action(path=path, status=STATUS_ERROR, reason=f"读取失败：{e}"))
            continue

        det = detect(data, hint=from_codec)
        act = Action(
            path=path, status=STATUS_SKIP, src_encoding=det.encoding,
            dst_encoding=to.name, src_eol=det.eol,
            src_bom=det.bom, size_before=size, size_after=size,
            detection=det,
        )

        if det.is_binary:
            act.status = STATUS_SKIP if skip_binary else STATUS_ERROR
            act.reason = "判定为非文本文件"
            plan.append(act)
            continue

        if not det.strict_decodable:
            act.status = STATUS_ERROR
            act.reason = det.note or "无法严格解码，拒绝转码"
            plan.append(act)
            continue

        effective_conf = det.confidence
        if refine_confidence and det.ambiguous:
            # 存在结构性歧义时，按"次优候选咬得多紧"折算真实可信度，
            # 避免一个 0.93 的假自信把用户带到沟里。
            effective_conf = min(effective_conf, 0.55)
        if from_codec is None and effective_conf < min_confidence:
            act.status = STATUS_ERROR
            act.reason = (f"嗅探置信度 {effective_conf:.2f} 低于阈值 {min_confidence:.2f}"
                          f"（--min-confidence），请用 --from 显式指定源编码")
            plan.append(act)
            continue

        # BOM 一致性检查：源文件带 BOM 但目标不写 BOM（或反之）时也属于"需要改"
        want_bom = bom_of(to) if write_bom else b""
        if only_mismatch and det.encoding == to.name and det.bom == want_bom:
            act.reason = "编码与 BOM 均已符合目标（--only-mismatch）"
            plan.append(act)
            continue

        try:
            payload = transcode_text(det.text, to, write_bom=write_bom, eol=eol)
        except UnicodeEncodeError:
            bad = unencodable_chars(det.text, to)
            act.status = STATUS_ERROR
            act.reason = (f"目标编码 {to.name} 无法表示文件中的字符："
                          + " ".join(f"{c}(U+{ord(c):04X})" for c in bad))
            plan.append(act)
            continue

        if payload == data:
            act.reason = "已是目标编码与换行，字节无变化"
            plan.append(act)
            continue

        act.status = STATUS_CONVERT
        act.payload = payload
        act.dst_eol = detect_eol(payload.decode(to.python_codec))
        act.dst_bom = want_bom
        act.size_after = len(payload)
        # 记录**换行归一之后**的字符数：写回自校验要比的就是它。
        # 用归一前的长度会让每个 CRLF 文件都误报"字符数变化"。
        act.chars = len(apply_eol(det.text, eol))
        act.reason = _describe_change(det, to, want_bom, eol)
        plan.append(act)

    return plan


def _describe_change(det: Detection, to: Codec, want_bom: bool, eol: str) -> str:
    """一句话说清这个文件要改什么，便于人扫描计划。"""
    bits = []
    # 纯 ASCII 是所有 ASCII 兼容编码的共同子集，字节层面并没有"换编码"，
    # 只是按目标编码重新落盘而已，不该说成编码变化。
    src_is_ascii = det.encoding == "ascii" and to.name != "utf-16be" and to.name != "utf-16le"
    if det.encoding != to.name and not src_is_ascii:
        bits.append(f"编码 {det.encoding} → {to.name}")
    elif det.encoding == "ascii":
        bits.append(f"按 {to.name} 重写")
    if det.bom != (bom_of(to) if want_bom else b""):
        bits.append("补写 BOM" if want_bom else "去掉 BOM")
    if eol != "keep" and det.eol != eol:
        bits.append(f"换行 {eol_label(det.eol)} → {eol_label(eol)}")
    return "；".join(bits) or "重写规范化"
