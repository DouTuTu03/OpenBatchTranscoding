"""嗅探引擎：把一堆字节判成一个编码，并且**把理由一起交出来**。

一次判定的完整次序
------------------
1. BOM            —— 有 BOM 就是铁证，置信度 0.99
2. 纯 ASCII       —— 所有 ASCII 兼容编码结果一致，不存在歧义
3. NUL 分布       —— 无 BOM 的 UTF-16/32
4. 二进制         —— 不是文本就别硬当文本
5. 候选打分       —— struct / quality / script 三分量 + prior，逐编码算总分
6. 混合编码       —— 谁都解不全时，给出"最接近的编码 + 无法解码的位置"

第 5 步是整个工具的核心，具体算法见 :mod:`obt.core.heuristics`。
这里只负责编排、算置信度、留证据。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .decode import decode_lenient, decode_strict, strip_bom_for
from .encodings import (
    BY_NAME, CODECS, CODECS_POOL, F_BINARY, F_LATIN, F_UNICODE, Codec, match_bom,
)
from .eol import EOL_NONE, detect_eol
from .heuristics import (
    byte_fit, decisive_script_bonus, family_script_fit, script_stats, text_quality,
)

# 三个分量的权重。struct 与 quality 并重，script 用来收口同族歧义。
W_STRUCT, W_QUALITY, W_SCRIPT = 0.35, 0.35, 0.30

# 分类用阈值
_AMBIGUOUS_GAP = 0.08        # 前两名分差小于此值 → 标注"存在结构性歧义"
_UTF16_NUL_SAMPLE = 65536
_MIN_TEXT_QUALITY = 0.40     # 最佳候选解出来还不如这个，就别硬说是文本了
_CONTROL_RATIO_MAX = 0.10    # 控制字节超过这个比例 → 非文本

# 超集关系：GB18030 是 GBK 的超集，GBK 能解的文件 GB18030 必然也能解，
# 且解出**完全相同的文本**。所以这两个不是竞争假设，而是同一种解读；
# 算置信度时不该拿它们互相当"次优候选"，否则一个普通 GBK 文件会被报成"有歧义"。
_SAME_READING = {("gbk", "gb18030"), ("gb18030", "gbk")}


@dataclass
class Candidate:
    """单个候选编码的打分明细。``--explain`` 就是把它整张表打出来。"""

    codec: Codec
    strict: bool
    error: str = ""
    struct: float = 0.0
    quality: float = 0.0
    script: float = 0.0
    prior: float = 0.0
    bonus: float = 0.0
    total: float = 0.0
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "encoding": self.codec.name,
            "strict": self.strict,
            "error": self.error,
            "struct": round(self.struct, 4),
            "quality": round(self.quality, 4),
            "script": round(self.script, 4),
            "prior": round(self.prior, 4),
            "bonus": round(self.bonus, 4),
            "total": round(self.total, 4),
            "detail": self.detail,
        }


@dataclass
class Detection:
    """一次嗅探的结论 + 依据。"""

    encoding: str
    confidence: float
    family: str = F_UNICODE
    bom: bytes = b""
    eol: str = EOL_NONE
    size: int = 0
    evidence: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    text: str = ""
    strict_decodable: bool = True
    replacements: int = 0
    ambiguous: bool = False
    note: str = ""
    forced: bool = False

    @property
    def has_bom(self) -> bool:
        return bool(self.bom)

    @property
    def label(self) -> str:
        """展示用名：``utf-8 (BOM)``。"""
        return f"{self.encoding} (BOM)" if self.has_bom else self.encoding

    @property
    def is_binary(self) -> bool:
        return self.family == F_BINARY

    @property
    def is_text(self) -> bool:
        return not self.is_binary

    def as_dict(self, *, with_text: bool = False, with_candidates: bool = True) -> dict:
        d = {
            "encoding": self.encoding,
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "family": self.family,
            "bom": self.bom.hex(" "),
            "eol": self.eol,
            "size": self.size,
            "strict_decodable": self.strict_decodable,
            "replacements": self.replacements,
            "ambiguous": self.ambiguous,
            "forced": self.forced,
            "note": self.note,
            "evidence": list(self.evidence),
            "alternatives": [
                {"encoding": c.codec.name, "total": round(c.total, 4)}
                for c in self.candidates
                if c.strict and c.codec.name != self.encoding
            ][:3],
        }
        if with_candidates:
            # 打分明细很啰嗦：批量场景（如转码计划）默认不带
            d["candidates"] = [c.as_dict() for c in self.candidates]
        if with_text:
            d["text"] = self.text
        return d


def _has_nonascii(data: bytes) -> bool:
    return any(b >= 0x80 for b in data)


def _control_ratio(data: bytes) -> tuple:
    """统计异常控制字节占比。返回 ``(占比, 明细)``。

    ``0x00`` 单独看：文本文件里不该出现 NUL，出现一次就该怀疑是二进制。
    """
    sample = data[:_UTF16_NUL_SAMPLE]
    n = len(sample) or 1
    nul = sample.count(0)
    weird = sum(1 for b in sample if b < 0x09 or 0x0E <= b <= 0x1F or b == 0x7F)
    return weird / n, {"weird_ratio": round(weird / n, 4), "nul": nul}


def score_candidates(data: bytes, codecs=CODECS_POOL) -> list:
    """对每个候选编码打分，返回 :class:`Candidate` 列表（不排序）。

    即使严格解码失败也照常算 struct / script——因为"最接近的编码，以及
    它为什么不像"正是混合编码排查最需要的信息，``--explain`` 要靠它。
    失败的候选质量分改用**替换符比例**折算，避免把 U+FFFD 重复计入。
    """
    nonascii = _has_nonascii(data)
    out = []
    for codec in codecs:
        cand = Candidate(codec=codec, strict=False)
        try:
            text = data.decode(codec.python_codec)
            cand.strict = True
        except UnicodeDecodeError as e:
            cand.error = f"偏移 {e.start}：{e.reason}"
            # 宽松解码只用于评估"解出来像不像"，绝不用于写文件
            text = data.decode(codec.python_codec, errors="replace")

        s_struct, d_struct = byte_fit(data, codec.family)
        stats = script_stats(text)
        s_script, d_script = family_script_fit(codec.family, stats, nonascii)

        if cand.strict:
            s_qual, d_qual = text_quality(text)
        else:
            reps = text.count("\ufffd")
            ratio = reps / max(1, len(data))
            s_qual = 1.0 / (1.0 + ratio * 20.0)
            d_qual = {"replacements": reps, "ratio": round(ratio, 4)}

        s_bonus, why_bonus = decisive_script_bonus(codec.family, stats)

        cand.struct, cand.quality = s_struct, s_qual
        cand.script, cand.prior = s_script, codec.prior
        cand.bonus = s_bonus
        cand.total = (W_STRUCT * s_struct + W_QUALITY * s_qual
                      + W_SCRIPT * s_script + codec.prior + s_bonus)
        cand.detail = {
            "struct": d_struct, "quality": d_qual, "script": d_script,
            "chars": len(text), "script_stats": stats, "decisive": why_bonus,
        }
        out.append(cand)
    return out


def _forced(data: bytes, codec: Codec) -> Detection:
    """``--from`` 强制指定编码：跳过判定，但仍做严格解码校验。"""
    payload, bom = strip_bom_for(data, codec)
    try:
        text = decode_strict(payload, codec)
    except Exception as e:  # DecodeError
        return Detection(
            encoding=codec.name, confidence=1.0, family=codec.family, bom=bom,
            eol=EOL_NONE, size=len(data), text="", strict_decodable=False,
            forced=True, note=f"--from 指定的编码无法严格解码：{e}",
            evidence=[f"用户指定 --from {codec.name}，但解码失败"],
        )
    return Detection(
        encoding=codec.name, confidence=1.0, family=codec.family, bom=bom,
        eol=detect_eol(text), size=len(data), text=text, forced=True,
        evidence=[f"用户通过 --from 指定 {codec.name}，未启用自动嗅探"],
    )


def detect(data: bytes, *, hint: Codec = None, codecs=CODECS_POOL) -> Detection:
    """嗅探 ``data`` 的编码。``hint`` 非空时等价于用户显式指定。"""
    if hint is not None:
        return _forced(data, hint)

    size = len(data)
    if size == 0:
        return Detection(encoding="empty", confidence=1.0, family=F_UNICODE,
                         size=0, text="", evidence=["文件为空（0 字节）"])

    # 1) BOM
    hit = match_bom(data)
    if hit:
        name, payload = hit
        codec = BY_NAME[name]
        try:
            text = payload.decode(codec.python_codec)
        except UnicodeDecodeError as e:
            # BOM 说一套、内容说另一套：不能信 BOM，降级走完整嗅探。
            det = detect(data[len(codec.bom):], codecs=codecs)
            det.size = size
            det.evidence.insert(
                0,
                f"发现 {name} BOM，但其后内容无法按 {name} 解码（偏移 {e.start}），"
                f"已忽略 BOM 重新嗅探",
            )
            det.confidence = min(det.confidence, 0.6)
            det.bom = b""
            return det
        return Detection(
            encoding=name, confidence=0.99, family=codec.family, bom=codec.bom,
            eol=detect_eol(text), size=size, text=text,
            evidence=[
                f"检测到 {name} BOM（{codec.bom.hex(' ')}）",
                f"BOM 之后 {len(payload)} 字节按 {name} 严格解码成功",
            ],
        )

    # 2) 纯 ASCII：先判掉，省掉后面所有无意义的纠结。
    #    注意必须排除含 NUL 的情况——真正的文本文件里不该有 NUL，
    #    而"全是 ASCII 字节"这个条件会把二进制文件也算进来。
    if not _has_nonascii(data) and b"\x00" not in data[:_UTF16_NUL_SAMPLE]:
        text = data.decode("ascii")
        return Detection(
            encoding="ascii", confidence=1.0, family=F_UNICODE, size=size,
            eol=detect_eol(text), text=text,
            evidence=["全部字节 < 0x80", "任意 ASCII 兼容编码的解码结果完全一致"],
            note="纯 ASCII 无编码歧义，转码只会改变 BOM 与换行",
        )

    # 3) 含 NUL：可能是无 BOM 的 UTF-16
    #    判据是 NUL 必须落在**同一奇偶位**上——ASCII 字符的高字节才是 0x00。
    #    随手拿一个含 NUL 的二进制文件来试 UTF-16 是没意义的，会碰运气解成功。
    sample = data[:_UTF16_NUL_SAMPLE]
    nul_positions = [i for i, b in enumerate(sample) if b == 0]
    if nul_positions:
        odd = sum(1 for i in nul_positions if i % 2)
        even = len(nul_positions) - odd
        strong = len(nul_positions) >= 2
        guesses = []
        if strong and odd >= 0.9 * len(nul_positions):
            guesses.append("utf-16le")
        if strong and even >= 0.9 * len(nul_positions):
            guesses.append("utf-16be")
        for name in guesses:
            codec = BY_NAME[name]
            try:
                text = data.decode(codec.python_codec)
            except UnicodeDecodeError:
                continue
            q, _qd = text_quality(text)
            if q > 0.75:
                return Detection(
                    encoding=name, confidence=0.80, family=F_UNICODE, size=size,
                    eol=detect_eol(text), text=text,
                    evidence=[
                        f"无 BOM，但 {len(nul_positions)} 个 NUL 全部落在"
                        f"{'奇' if name.endswith('le') else '偶'}数位",
                        f"按 {name} 完整解码成功，文本质量 {q:.2f}",
                    ],
                    note="无 BOM 的 UTF-16，建议转码时补上 BOM",
                )

        # 含 NUL 又不构成 UTF-16 → 不是文本。真正的文本文件里不该有 NUL。
        return Detection(
            encoding="binary", confidence=0.9, family=F_BINARY, size=size,
            eol=EOL_NONE, text="", strict_decodable=False,
            evidence=[f"含 {len(nul_positions)} 个 NUL 字节，且不符合任何 UTF-16 端序特征"],
            note="判定为非文本文件，默认跳过",
        )

    # 4) 不含 NUL，但控制字节比例过高 → 也不是文本
    weird_ratio, wd = _control_ratio(data)
    if weird_ratio > _CONTROL_RATIO_MAX:
        return Detection(
            encoding="binary", confidence=0.85, family=F_BINARY, size=size,
            eol=EOL_NONE, text="", strict_decodable=False,
            evidence=[f"异常控制字节占比 {weird_ratio:.1%}（阈值 {_CONTROL_RATIO_MAX:.0%}）"],
            note="判定为非文本文件，默认跳过",
        )

    # 5) 候选打分
    cands = score_candidates(data, codecs)
    usable = [c for c in cands if c.strict]
    order = {c.name: i for i, c in enumerate(codecs)}

    if usable:
        usable.sort(key=lambda c: (-c.total, order.get(c.codec.name, 99)))
        best = usable[0]
        # 次优候选要排除"同一种解读"的编码（如 gb18030 之于 gbk），
        # 否则会把超集关系误报成结构性歧义。
        rivals = [c for c in usable[1:]
                  if (best.codec.name, c.codec.name) not in _SAME_READING]
        second = rivals[0] if rivals else None

        # 5b) 兜底逻辑编码（latin-1 / cp1252）能解码**任意**字节流，所以
        #     "严格解码成功"本身没有说服力。真正的判据是解出来像不像人话：
        #     控制符 / 私用区一多，这就不是文本，别硬贴一个编码名。
        if best.quality < _MIN_TEXT_QUALITY:
            return Detection(
                encoding="binary", confidence=0.75, family=F_BINARY, size=size,
                eol=EOL_NONE, text="", strict_decodable=False, candidates=cands,
                evidence=[
                    f"最佳候选 {best.codec.name} 虽能解码，但文本质量仅 "
                    f"{best.quality:.2f}（阈值 {_MIN_TEXT_QUALITY}）",
                    "控制符 / 私用区占比过高，判定为非文本",
                ],
                note="判定为非文本文件，默认跳过",
            )

        if best.codec.family == F_LATIN and best.script < 0.5:
            # 兜底的西欧编码"赢"了，却解出满屏变音字母（乱码指纹）——
            # 这不是一份西欧文本，而是别的东西被硬读成西欧文本。
            # 不认这个结论，落到第 6 步把"哪里解不了"交出来。
            usable = []
        else:
            text = data.decode(best.codec.python_codec)
            if best.codec.family == F_UNICODE:
                confidence = 0.95
                evidence = ["严格解码成功且无 BOM"]
            else:
                confidence = 0.90
                evidence = ["所有候选编码中总分最高"]
            if second is not None:
                gap = (best.total - second.total) / max(best.total, 1e-6)
                # 置信度由"与次优的相对差距"和"胜出者自身的绝对得分"共同封顶：
                # 差距大但总分低（例如只有几个字节可看）时，不许给满信心。
                by_gap = 0.55 + 0.45 * min(1.0, gap * 3.0)
                by_total = 0.55 + 0.45 * min(1.0, max(0.0, best.total))
                confidence = min(confidence, by_gap, by_total)

            ambiguous = bool(second is not None
                             and (best.total - second.total) < _AMBIGUOUS_GAP)
            evidence.append(
                f"打分：结构 {best.struct:.2f} / 质量 {best.quality:.2f} / "
                f"文字系统 {best.script:.2f} / 先验 {best.prior:+.2f}"
                + (f" / 决胜 {best.bonus:+.2f}" if best.bonus else "")
                + f" = {best.total:.3f}"
            )
            why = best.detail.get("script", {}).get("why")
            if why:
                evidence.append(f"文字系统判据：{why}")
            if best.detail.get("decisive"):
                evidence.append(f"决胜证据：{best.detail['decisive']}")
            if second is not None:
                evidence.append(f"次优候选：{second.codec.name}（{second.total:.3f}）")

            note = ""
            if ambiguous:
                note = (f"与 {second.codec.name} 分差仅 {best.total - second.total:.3f}，"
                        f"存在结构性歧义；建议抽样核对，或用 --from 显式指定")

            return Detection(
                encoding=best.codec.name, confidence=confidence,
                family=best.codec.family, size=size, eol=detect_eol(text),
                text=text, evidence=evidence, candidates=cands,
                ambiguous=ambiguous, note=note,
            )

    # 6) 谁都解不全：给出最接近的编码 + 有多少字节解释不了。
    #    这里**不做任何猜测性修复**——只报告，并让上层拒绝整体转码。
    ranked = sorted(cands, key=lambda c: (-c.total, order.get(c.codec.name, 99)))
    guess = ranked[0]
    reps = guess.detail.get("quality", {}).get("replacements", 0)
    # 行尾和编码是两件正交的事：解不全不代表换行风格看不出来。
    # 宽松解码足以数清 \r\n / \n，这条信息对排查混合编码很有用，不该丢掉。
    eol = detect_eol(decode_lenient(data, guess.codec))
    return Detection(
        encoding=guess.codec.name, confidence=0.30, family=guess.codec.family,
        size=size, eol=eol, text="", strict_decodable=False, replacements=reps,
        candidates=cands,
        evidence=[
            "没有任何候选编码能严格解码整个文件",
            f"最接近的是 {guess.codec.name}：结构契合 {guess.struct:.2f}、"
            f"文字系统 {guess.script:.2f}，但仍有 {reps} 个字节无法解释",
        ],
        note="疑似混合编码或非文本；整体转码会把坏字节永久变成替换符，本工具默认拒绝",
    )
