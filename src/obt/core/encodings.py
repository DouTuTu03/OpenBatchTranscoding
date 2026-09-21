"""编码注册表：BOM 表、编码族/文字系统定义、别名归一。

纯数据层，不做任何 I/O——这样才能被 CLI、GUI、测试平等复用。

设计要点
--------
``Codec.bom``
    该编码**被识别**时可能的 BOM。utf-8 有 BOM 也能识别，但我们不会
    默认写出去，所以"识别"与"写出"分开：见 ``Codec.write_bom``。
``Codec.family``
    同一族的编码共享字节语法（GBK 与 GB18030 是超集关系，Big5 与 GBK
    的字节区间有重叠），光看字节分不出来时，只能靠"解出来的文字
    是简体、繁体还是日文"来收口。
``Codec.prior``
    先验分。UTF-8 是现代默认，给正分；latin-1 / cp1252 属于"任意字节
    都能解码"的兜底编码，能解码不代表性证据强，给负分。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

# --------------------------------------------------------------------- 编码族
F_UNICODE = "unicode"
F_GB = "gb"          # GB2312 / GBK / GB18030
F_BIG5 = "big5"
F_SJIS = "sjis"
F_EUCJP = "eucjp"
F_EUCKR = "euckr"
F_LATIN = "latin"    # 西欧单字节
F_BINARY = "binary"  # 不是文本

# --------------------------------------------------------------- 期望文字系统
S_ANY = "any"
S_CJK = "cjk"
S_JP = "japanese"
S_KR = "korean"
S_LATIN = "latin"


class UnknownEncoding(ValueError):
    """``--to`` / ``--from`` 给了个不认识的编码名。带上相近候选，别让人猜。"""

    def __init__(self, name: str, suggestions: tuple = ()):
        hint = f"；是不是想写：{' / '.join(suggestions)}" if suggestions else ""
        super().__init__(f"不认识的编码：{name!r}{hint}（可用 `obt encodings` 查看支持列表）")
        self.name = name
        self.suggestions = suggestions


@dataclass(frozen=True)
class Codec:
    """一个可嗅探 / 可转码的编码。"""

    name: str                     # 规范名，输出给用户看的
    python_codec: str             # 真正交给 bytes.decode / str.encode 的名字
    family: str                   # 编码族，见 F_*
    script: str = S_ANY           # 该编码期望产出的文字系统
    bom: bytes = b""              # 识别用 BOM（有则说明该编码可能带 BOM）
    write_bom: bytes = b""        # 转码到该编码时的默认 BOM（空 = 默认不写）
    prior: float = 0.0            # 先验分，见模块 docstring
    aliases: tuple = ()           # 用户可能输入的名字
    note: str = ""                # 一句话说明，给 `obt encodings` 用
    bom_only: bool = False        # 只能靠 BOM / NUL 分布识别，不参与通用打分

    @property
    def is_family_unicode(self) -> bool:
        return self.family == F_UNICODE


# 顺序即平局时的优先级（靠前者优先）。
CODECS: tuple = (
    Codec(
        name="utf-8", python_codec="utf-8", family=F_UNICODE, script=S_ANY,
        bom=b"\xef\xbb\xbf", write_bom=b"", prior=+0.15,
        aliases=("utf8", "u8"),
        note="现代默认。严格解码一旦失败，即可断定不是 UTF-8",
    ),
    Codec(
        name="utf-16le", python_codec="utf-16-le", family=F_UNICODE, script=S_ANY,
        bom=b"\xff\xfe", write_bom=b"\xff\xfe", prior=+0.05, bom_only=True,
        aliases=("utf16le", "utf-16", "utf16", "ucs2", "ucs-2"),
        note="无 BOM 时靠 NUL 的奇偶分布推断；不参与通用打分",
    ),
    Codec(
        name="utf-16be", python_codec="utf-16-be", family=F_UNICODE, script=S_ANY,
        bom=b"\xfe\xff", write_bom=b"\xfe\xff", prior=+0.05, bom_only=True,
        aliases=("utf16be",),
        note="大端 UTF-16，基本只在 BOM 明确时使用",
    ),
    Codec(
        name="utf-32le", python_codec="utf-32-le", family=F_UNICODE, script=S_ANY,
        bom=b"\xff\xfe\x00\x00", write_bom=b"\xff\xfe\x00\x00", prior=-0.05, bom_only=True,
        aliases=("utf32le", "utf-32", "utf32"),
        note="罕见；BOM 与 UTF-16LE 前缀重叠，必须先匹配长的",
    ),
    Codec(
        name="utf-32be", python_codec="utf-32-be", family=F_UNICODE, script=S_ANY,
        bom=b"\x00\x00\xfe\xff", write_bom=b"\x00\x00\xfe\xff", prior=-0.05, bom_only=True,
        aliases=("utf32be",),
        note="罕见",
    ),
    Codec(
        name="gbk", python_codec="gbk", family=F_GB, script=S_CJK, prior=0.0,
        aliases=("gb2312", "gb-2312", "cp936", "ms936", "936", "ansi"),
        note="简体中文 GB2312/GBK（CP936 超集）。Windows 中文环境下 .cs/.txt 的常见编码",
    ),
    Codec(
        name="gb18030", python_codec="gb18030", family=F_GB, script=S_CJK, prior=-0.05,
        aliases=("gb-18030", "cp54936"),
        note="GBK 的超集，含 4 字节序列；仅当 GBK 严格解码失败才会胜出",
    ),
    Codec(
        name="big5", python_codec="big5", family=F_BIG5, script=S_CJK, prior=-0.02,
        aliases=("big-5", "cp950", "950"),
        note="繁体中文",
    ),
    Codec(
        name="shift_jis", python_codec="shift_jis", family=F_SJIS, script=S_JP, prior=-0.02,
        aliases=("sjis", "shift-jis", "cp932", "ms932", "932", "s-jis"),
        note="日文；出现平/片假名是决定性证据",
    ),
    Codec(
        name="euc-jp", python_codec="euc_jp", family=F_EUCJP, script=S_JP, prior=-0.05,
        aliases=("eucjp", "ujis"),
        note="日文（Unix 传统）",
    ),
    Codec(
        name="euc-kr", python_codec="euc_kr", family=F_EUCKR, script=S_KR, prior=-0.02,
        aliases=("euckr", "cp949", "949", "uhc", "ks_c_5601-1987"),
        note="韩文；出现谚文是决定性证据",
    ),
    Codec(
        name="windows-1252", python_codec="cp1252", family=F_LATIN, script=S_LATIN, prior=-0.20,
        aliases=("cp1252", "1252", "windows1252", "ansi-1252"),
        note="西欧单字节；能解码 0x80-0x9F 的排版字符，证据强度中等",
    ),
    Codec(
        name="latin-1", python_codec="latin-1", family=F_LATIN, script=S_LATIN, prior=-0.35,
        aliases=("latin1", "iso-8859-1", "iso88591", "8859-1"),
        note="万能兜底：任意字节序列都能解码，因此证据最弱",
    ),
    Codec(
        # "ascii" 不是真的能拿来转码的目标（写非 ASCII 会失败），但它是
        # 嗅探结果里一个正式取值，也是 check --expect ascii 的依据。
        # 放进注册表才能被 resolve，而不是靠字符串特判。
        name="ascii", python_codec="ascii", family=F_UNICODE, script=S_ANY, prior=-0.10,
        bom_only=True,
        aliases=("us-ascii", "usascii", "646", "7bit"),
        note="7 位 ASCII；只在文件全是 ASCII 字节时为真（含 NUL 的除外）",
    ),
)

BY_NAME = {c.name: c for c in CODECS}

# 嗅探打分池：排除 UTF-16/32 这类"必须靠 BOM 或 NUL 异常分布才能识别"的编码。
# 原因很实际：任意偶数长度字节流几乎都能被 UTF-16 严格解码成功，
# 让它进池子会凭"解码成功"白拿高分，把 GBK / Shift_JIS 全抢走。
CODECS_POOL: tuple = tuple(c for c in CODECS if not c.bom_only)

# BOM 匹配顺序：UTF-32LE 的 BOM 以 UTF-16LE 的 BOM 为前缀，必须先匹配长的。
_BOM_ORDER: tuple = (
    ("utf-32le", b"\xff\xfe\x00\x00"),
    ("utf-32be", b"\x00\x00\xfe\xff"),
    ("utf-8", b"\xef\xbb\xbf"),
    ("utf-16le", b"\xff\xfe"),
    ("utf-16be", b"\xfe\xff"),
)


def _norm(name: str) -> str:
    """把用户输入拍平：大小写、连字符、下划线、空格都不参与匹配。"""
    return name.strip().lower().replace("-", "").replace("_", "").replace(" ", "")


# 别名索引（拍平后）
_ALIAS: dict = {}
for _c in CODECS:
    _ALIAS[_norm(_c.name)] = _c.name
    for _a in _c.aliases:
        _ALIAS.setdefault(_norm(_a), _c.name)


def _split_bom_intent(name: str) -> tuple:
    """``utf-8-bom`` / ``utf8sig`` -> ``("utf8", True)``。

    只认「以 bom/sig 结尾且去掉后缀后是个合法编码」的形式，
    避免把 ``gb18030`` 这类名字误伤。
    """
    key = _norm(name)
    if key in _ALIAS:
        return key, False
    for suffix in ("bom", "sig"):
        if key.endswith(suffix):
            head = key[: -len(suffix)]
            if head in _ALIAS:
                return head, True
    return key, False


def resolve(name: str) -> Codec:
    """按名字或别名解析编码；失败时抛 :class:`UnknownEncoding`。"""
    key, _ = _split_bom_intent(name)
    if key in _ALIAS:
        return BY_NAME[_ALIAS[key]]
    raise UnknownEncoding(name, tuple(difflib.get_close_matches(key, list(_ALIAS), n=3)))


def resolve_target(name: str) -> tuple:
    """解析 ``--to``，返回 ``(Codec, 是否写 BOM)``。

    BOM 策略：名字里显式带 bom/sig 就写；否则按编码默认
    （UTF-16/32 必须写，否则下次没人认得出来；UTF-8 默认不写，
    保持与 Git/编辑器一致）。
    """
    key, bom_intent = _split_bom_intent(name)
    if key not in _ALIAS:
        raise UnknownEncoding(name, tuple(difflib.get_close_matches(key, list(_ALIAS), n=3)))
    codec = BY_NAME[_ALIAS[key]]
    if bom_intent:
        return codec, True
    return codec, bool(codec.write_bom)


def match_bom(data: bytes) -> tuple:
    """返回 ``(编码名, 去掉 BOM 后的载荷)``；无 BOM 返回 ``None``。"""
    for name, bom in _BOM_ORDER:
        if data.startswith(bom):
            return name, data[len(bom):]
    return None


def bom_of(codec: Codec) -> bytes:
    """转码目标要写出去的 BOM 字节。"""
    if codec.write_bom:
        return codec.write_bom
    return b"\xef\xbb\xbf" if codec.name == "utf-8" else b""
