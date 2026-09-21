"""obt 内核的稳定 API。

**将来的 GUI / 服务端 / MCP 工具只应该 import 这里**，
不要直接摸 ``obt.cli``。判定与执行的全部能力都在这一层：

    from obt.core import detect, build_plan, apply_actions

    det = detect(open("a.cs", "rb").read())
    plan = build_plan([Path("a.cs")], to=resolve("utf-8"), write_bom=True)
"""

from .apply import Result, apply_actions, target_path
from .decode import DecodeError, decode_lenient, decode_strict, strip_bom_for
from .detect import Candidate, Detection, detect, score_candidates
from .encodings import (
    BY_NAME, CODECS, Codec, UnknownEncoding, bom_of, match_bom, resolve,
    resolve_target,
)
from .eol import (
    EOL_CHOICES, EOL_CR, EOL_CRLF, EOL_LF, EOL_MIXED, EOL_NONE, apply_eol,
    count_eol, detect_eol, eol_label, eol_literal,
)
from .mixed import (
    LineAnomaly, LineReport, UndecodableRun, find_undecodable, line_encodings,
)
from .plan import (
    STATUS_CONVERT, STATUS_ERROR, STATUS_SKIP, Action, build_plan,
    transcode_text, unencodable_chars,
)
from .scan import DEFAULT_EXCLUDES, iter_files

__all__ = [
    # 嗅探
    "detect", "Detection", "Candidate", "score_candidates",
    # 编码
    "Codec", "CODECS", "BY_NAME", "resolve", "resolve_target", "match_bom",
    "bom_of", "UnknownEncoding",
    # 解码
    "decode_strict", "decode_lenient", "strip_bom_for", "DecodeError",
    # 混合编码
    "find_undecodable", "line_encodings", "UndecodableRun", "LineReport",
    "LineAnomaly",
    # 行尾
    "detect_eol", "apply_eol", "count_eol", "eol_label", "eol_literal",
    "EOL_CHOICES", "EOL_LF", "EOL_CRLF", "EOL_CR", "EOL_MIXED", "EOL_NONE",
    # 扫描与计划
    "iter_files", "DEFAULT_EXCLUDES", "build_plan", "Action",
    "transcode_text", "unencodable_chars",
    "STATUS_CONVERT", "STATUS_SKIP", "STATUS_ERROR",
    # 执行
    "apply_actions", "target_path", "Result",
]
