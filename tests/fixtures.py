"""测试与演示共用的编码样本语料。

刻意做成"代码生成字节"而不是往仓库里塞二进制文件：
样本长什么样、期望什么，一眼可见，也便于 review 里讨论。

这些样本不是凑数的，每一条都对应一类真实事故：

* ``gbk_legacy``      Windows 中文环境的历史遗留 .cs 文件
* ``big5``/``sjis``/``euc_kr``  同一份"试验报告"分别用繁体/日文/韩文存
* ``mixed``           一个文件里 UTF-8 与 GBK 混杂（真实事故现场）
* ``binary``          非文本文件不该被硬当文本转码
* ``utf16_nobom``     没有 BOM 的 UTF-16，只能靠 NUL 奇偶分布认出来
* ``gb18030_quad``    GB18030 四字节序列，GBK 解不了
"""

from __future__ import annotations

import codecs
import pathlib

CN = (
    "试验报告\r\n"
    "项目：电容器耐久性试验\r\n"
    "条件：额定电压 250V，环境温度 85℃、湿度 85%RH\r\n"
    "结论：外观无异常，容量变化率 -3.2%\r\n"
)
TRAD = (
    "試驗報告\r\n"
    "項目：電容器耐久性試驗\r\n"
    "條件：額定電壓 250V，環境溫度 85℃、濕度 85%RH\r\n"
    "結論：外觀無異常，容量變化率 -3.2%\r\n"
)
JP = (
    "試験報告\r\n"
    "項目：コンデンサ耐久試験\r\n"
    "条件：定格電圧 250V、周囲温度 85℃です。\r\n"
    "結論：外観に異常はありません。\r\n"
)
KR = (
    "시험 보고서\r\n"
    "항목: 커패시터 내구성 시험\r\n"
    "조건: 정격 전압 250V, 주위 온도 85℃\r\n"
    "결론: 외관 이상 없음.\r\n"
)
DE = (
    "Pruefbericht\r\n"
    "Umgebungstemperatur 85 °C, Nennspannung 250 V, Feuchte 85 % r. F.\r\n"
    "Ergebnis: keine Auffaelligkeiten. Grösse Änderung -3,2 %.\r\n"
)

MIXED = (
    "第一行 UTF-8：试验开始\n".encode("utf-8")
    + "第二行 GBK：试验结束\n".encode("gbk")
    + "第三行 UTF-8：报告归档\n".encode("utf-8")
)

BINARY = b"\x7fELF\x02\x01\x01\x00" + bytes(range(1, 256)) * 3


def corpus() -> list:
    """返回 ``[(文件名, 字节, 期望编码, 说明)]``。

    期望编码取 ``"混合"`` 表示"期望判定为无法严格解码"。
    """
    return [
        ("ascii_only.txt", b"ExpVoltage=250\nExpTemp1=85\n", "ascii",
         "纯 ASCII，任何 ASCII 兼容编码等价"),
        ("utf8_lf.txt", CN.replace("\r\n", "\n").encode("utf-8"), "utf-8",
         "UTF-8 无 BOM + LF"),
        ("utf8_bom_crlf.cs", codecs.BOM_UTF8 + CN.encode("utf-8"), "utf-8",
         "UTF-8 带 BOM + CRLF（.NET 仓库常见规范）"),
        ("gbk_legacy.cs", CN.encode("gbk"), "gbk",
         "GBK 简体，Windows 中文环境的历史遗留文件"),
        ("big5_traditional.txt", TRAD.encode("big5"), "big5",
         "Big5 繁体，验证繁简收口"),
        ("sjis_note.txt", JP.encode("shift_jis"), "shift_jis",
         "Shift_JIS 日文，假名是决定性证据"),
        ("euc_kr_note.txt", KR.encode("euc_kr"), "euc-kr",
         "EUC-KR 韩文，谚文是决定性证据"),
        ("cp1252_german.txt", DE.encode("cp1252"), "windows-1252",
         "西欧单字节，证据最弱的一类"),
        ("utf16_nobom.txt", CN.replace("\r\n", "\n").encode("utf-16-le"), "utf-16le",
         "无 BOM 的 UTF-16LE，靠 NUL 奇偶分布推断"),
        ("gb18030_quad.txt", "四字节序列测试：😀 汉字 𠀋\n".encode("gb18030"), "gb18030",
         "GB18030 四字节序列，GBK 解不了"),
        ("mixed_encoding.log", MIXED, "混合",
         "同一文件里 UTF-8 与 GBK 混杂"),
        ("crlf_mixed.txt", b"line1\r\nline2\nline3\r\n", "ascii",
         "换行符混用，与编码无关"),
        ("binary.bin", BINARY, "binary", "非文本文件"),
    ]


def write_all(directory) -> list:
    """把语料落到目录里，返回 ``[(Path, 字节, 期望, 说明)]``。"""
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    out = []
    for name, data, expect, note in corpus():
        path = directory / name
        path.write_bytes(data)
        out.append((path, data, expect, note))
    return out


def matches(det, expect: str) -> bool:
    """判定是否符合期望（"混合"表示期望无法严格解码）。"""
    if expect == "混合":
        return not det.strict_decodable
    return det.encoding == expect
