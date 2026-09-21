"""启发式打分：把"这份字节像不像某个编码"变成可解释的数字。

四个分量
--------
``struct``  字节结构契合（0..1）
    字节流是否符合该编码的字节语法，以及双字节序列是否落在该编码
    **最常用**的区间里。以 GBK 为例：GB2312 一级汉字（最常用 3755 字）
    在 0xB0A1–0xD7F9，二级字在 0xD8A1–0xF7FE，而 0x81–0xA0 / 0xF8–0xFE
    开头的是 GBK 扩展（生僻字）。真实中文文本的双字节绝大头落在一级区；
    「UTF-8 字节被当成 GBK 读」这种错判，解出来的一堆生僻字会把这个
    指标拉下来。这就是不需要词频表也能分辨的原因。

    这里还有一条很有效、但需要点明才看得懂的判据：**双字节对里两个字节
    是否都是高位字节**。真正的中文文本，汉字一个接一个，高位字节自然成对
    出现；而"西欧文本被硬当汉字读"时，每一个高位字节都在借用它后面的
    ASCII 字母当尾字节——比例几乎是 100%。见 ``_pair_score`` 的
    ``adjacent`` 系数。

``quality`` 文本质量（0..1）
    解码结果里控制符 / 替换符 U+FFFD / 私用区 / 未分配码位的占比。
    错误的解码常常"能解出来"但解出满屏控制符。

``script``  文字系统一致性（0..1）
    解出来的字符是不是该编码该产出的。假名、谚文是无歧义信号；
    汉字的**繁简倾向**用来收口 GB 族与 Big5 的经典混淆。特征字样本太小时
    按样本量回缩，避免"碰巧两个字都对"就给出满分。

``prior`` / ``decisive``
    ``prior`` 是先验（UTF-8 是现代默认；latin-1 / cp1252 能解码任意字节，
    证据天然最弱）。``decisive`` 是"决定性文字证据"的加分：解出大量谚文或
    假名，这种结构不是猜出来的，值得在总分上体现出来。

所有函数都是纯函数 ``bytes/str -> (分数, 明细)``，明细会进 ``--explain``，
方便对一次判定做审计，而不是让工具黑箱输出一个编码名。
"""

from __future__ import annotations

from .encodings import (
    F_BIG5, F_EUCJP, F_EUCKR, F_GB, F_LATIN, F_SJIS, F_UNICODE,
)

# ------------------------------------------------------------------ 繁简小词表
# 只收「两种字形不同」的字，且集中在开发/试验域，减少无关噪声。
# 两个集合会在下面做差集，因此即使某字被同时误收进两边，
# 也只会被剔除而不会污染判定方向。
_TRAD = frozenset(
    "這為時說國學溫試驗測報單項數據結果設備記錄審批刪除轉換編輯狀態類型歸檔權限員應該"
    "們個來對會過現開關實際產業資訊網路電腦軟體硬體檢測環境標準條件濕度圖檔儲讀設計製造"
    "週期訊號電壓電流頻率儀器樣品發貨庫存採購異常警告執行確認關閉顯示隱藏選擇輸入錯誤"
    "頁面項目組織單位階段順序總計平均標記註解連續斷紀錄"
)
_SIMP = frozenset(
    "这为时说国学温试验测报单项数据结果设备记录审批删除转换编辑状态类型归档权限员应该"
    "们个来对会过现开关实际产业资讯网络电脑软体硬体检测环境标准条件湿度图档储读设计制造"
    "周期讯号电压电流频率仪器样品发货库存采购异常警告执行确认关闭显示隐藏选择输入错误"
    "页面项目组织单位阶段顺序总计平均标记注解连续断记录"
)

# 交集一律剔除：剩下的才是真正的判别位。
_BOTH = _TRAD & _SIMP
TRAD_ONLY = _TRAD - _BOTH
SIMP_ONLY = _SIMP - _BOTH

# 允许出现的控制符（不扣分）
_CTRL_ALLOWED = frozenset("\t\n\r\f\v")

# 繁简特征字少于此数时，比例不可信，向中性分回缩
_FEATURE_FULL_CONFIDENCE = 8.0


# ------------------------------------------------------------------ 文本质量
def text_quality(text: str) -> tuple:
    """解码结果的可读性。返回 ``(0..1, 明细)``。

    采用 ``1 / (1 + 坏字符占比 * 8)``：坏字符占 12.5% 时降到 0.5，
    平滑且不会被超长文件稀释成"看起来没事"。
    """
    n = len(text)
    if n == 0:
        return 1.0, {"total": 0}

    fffd = ctrl = c1 = pua = unassigned = 0
    for ch in text:
        o = ord(ch)
        if o == 0xFFFD:
            fffd += 1
        elif o < 0x20:
            if ch not in _CTRL_ALLOWED:
                ctrl += 1
        elif o == 0x7F or 0x80 <= o <= 0x9F:
            c1 += 1
        elif 0xE000 <= o <= 0xF8FF:
            pua += 1
        elif 0xD800 <= o <= 0xDFFF or 0xFFF0 <= o <= 0xFFFF:
            unassigned += 1

    bad = fffd * 6 + ctrl * 4 + c1 * 3 + pua * 3 + unassigned * 4
    detail = {
        "total": n, "fffd": fffd, "ctrl": ctrl,
        "c1": c1, "pua": pua, "unassigned": unassigned,
    }
    return 1.0 / (1.0 + bad / n * 8.0), detail


# -------------------------------------------------------------- 文字系统统计
def script_stats(text: str) -> dict:
    """统计各文字系统的字符数（含繁简倾向）。"""
    st = {"ascii": 0, "cjk": 0, "kana": 0, "hangul": 0,
          "latin": 0, "other": 0, "trad": 0, "simp": 0}
    for ch in text:
        o = ord(ch)
        if o < 0x80:
            st["ascii"] += 1
        elif 0x4E00 <= o <= 0x9FFF:
            st["cjk"] += 1
            if ch in TRAD_ONLY:
                st["trad"] += 1
            elif ch in SIMP_ONLY:
                st["simp"] += 1
        elif 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF:
            st["kana"] += 1
        elif 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF:
            st["hangul"] += 1
        elif 0xC0 <= o <= 0x24F:
            st["latin"] += 1
        else:
            st["other"] += 1
    return st


def family_script_fit(family: str, stats: dict, has_nonascii: bool) -> tuple:
    """解码结果的文字系统与该编码族的期望是否吻合。返回 ``(0..1, 明细)``。"""
    if not has_nonascii:
        # 纯 ASCII：所有编码等价，这一项不提供任何信息。
        return 1.0, {"note": "纯 ASCII，文字系统无判别力"}

    total = sum(stats.values()) or 1
    cjk_r = stats["cjk"] / total
    kana_r = stats["kana"] / total
    hangul_r = stats["hangul"] / total

    if family == F_UNICODE:
        # Unicode 族不挑文字系统：UTF-8 严格解码成功本身就是最强证据。
        return 1.0, {"note": "Unicode 族不挑文字系统"}

    if family == F_LATIN:
        exotic = cjk_r + kana_r + hangul_r
        if exotic > 0.02:
            return 0.05, {"why": f"西欧编码不应产出表意字符（占比 {exotic:.1%}）"}
        # 乱码指纹：UTF-8 的中文/日文被当成 cp1252 读时，
        # 每个 3 字节字变成 3 个 À-ÿ 区字母，占比能到三成以上；
        # 而真正的德语/法语文本里变音字母撑死几个百分点。
        accent_r = stats["latin"] / total
        if accent_r > 0.12:
            return 0.15, {
                "why": f"西欧变音字母占比异常高（{accent_r:.1%}），典型"
                       f"「UTF-8 被当成西欧编码读」的乱码特征"
            }
        return 1.0, {"accent_ratio": round(accent_r, 4)}

    if family in (F_GB, F_BIG5):
        if kana_r > 0.02:
            return 0.10, {"why": f"中文编码却产出假名（{kana_r:.1%}）"}
        if hangul_r > 0.02:
            return 0.10, {"why": f"中文编码却产出谚文（{hangul_r:.1%}）"}
        if cjk_r == 0:
            return 0.12, {"why": "声称中文编码，但一个汉字都没有"}
        denom = stats["trad"] + stats["simp"]
        if denom == 0:
            # 一个繁简特征字都没有：可能是冷门话题，也可能是"韩国/日文
            # 字节被当中文读"解出的一堆生僻字。此时退回汉字占比——真正
            # 的中文文本哪怕话题冷门，汉字占比也不会是个位数。
            if cjk_r >= 0.30:
                return 0.70, {"why": f"无繁简特征字，但汉字占比 {cjk_r:.1%}"}
            if cjk_r >= 0.15:
                return 0.55, {"why": f"无繁简特征字，汉字占比 {cjk_r:.1%}，证据一般"}
            return 0.35, {"why": f"无繁简特征字且汉字占比仅 {cjk_r:.1%}，"
                                 f"更像西欧文本被硬当汉字读"}
        base = 0.30 + 0.70 * (stats["simp"] if family == F_GB else stats["trad"]) / denom
        # 特征字太少时比例不可信（两三字全对可能纯属偶然），按样本量回缩
        weight = min(1.0, denom / _FEATURE_FULL_CONFIDENCE)
        score = 0.60 + (base - 0.60) * weight
        return score, {
            "simp": stats["simp"], "trad": stats["trad"], "features": denom,
            "why": f"{'GB 族期望简体' if family == F_GB else 'Big5 期望繁体'}，"
                   f"特征字 {denom} 个，按样本量回缩到 {weight:.0%}",
        }

    if family in (F_SJIS, F_EUCJP):
        if hangul_r > 0.02:
            return 0.15, {"why": f"日文编码却产出谚文（{hangul_r:.1%}）"}
        jp_share = _share(stats["kana"], stats["kana"] + stats["cjk"])
        if stats["kana"] > 0:
            # 真日文里假名通常占表意字符的一半以上；只冒出零星假名说明是误读
            score = 0.30 + 0.70 * min(1.0, jp_share / 0.5)
            return score, {"why": f"假名占表意字符 {jp_share:.0%}"}
        if cjk_r > 0.05:
            return 0.60, {"why": "只有汉字无假名，日文可能性一般"}
        return 0.25, {"why": "既无假名也无汉字"}

    if family == F_EUCKR:
        if kana_r > 0.02:
            return 0.15, {"why": f"韩文编码却产出假名（{kana_r:.1%}）"}
        kr_share = _share(stats["hangul"], stats["hangul"] + stats["cjk"])
        if stats["hangul"] > 0:
            # 这一条是韩文与简体中文的关键判据，值得展开说：
            # KS X 1001 的谚文区（B0A1–C8FE）与 GB2312 一级汉字区（B0A1–D7F9）
            # 在字节空间上高度重叠，所以一份**简体中文 GBK 文件**往往能被
            # 韩文编码"解出来"——但解出来是谚文(B0–C8)与汉字(C9–D7)的混杂，
            # 而真正的韩文文本里谚文占绝对多数、汉字只是点缀。
            score = 0.20 + 0.80 * _clamp01((kr_share - 0.50) / 0.40)
            return score, {"why": f"谚文占表意字符 {kr_share:.0%}"
                                  f"（真韩文通常 >85%）"}
        if cjk_r > 0.05:
            return 0.55, {"why": "只有汉字无谚文，韩文可能性一般"}
        return 0.25, {"why": "既无谚文也无汉字"}

    return 0.5, {"why": f"未知编码族 {family}"}


def _clamp01(v: float) -> float:
    return 0.0 if v < 0 else (1.0 if v > 1 else v)


def _share(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def decisive_script_bonus(family: str, stats: dict) -> tuple:
    """决定性文字证据的加分。返回 ``(加分, 理由)``。

    "解出大量谚文"和"解出大量假名"这两种情况不是靠概率碰巧凑出来的：
    如 :func:`family_script_fit` 里所述，韩文/中文的字节空间高度重叠，
    光看字节永远分不开，只有文字系统能一锤定音。所以给一个明确的加分，
    而不是让它去和繁简比例拼小数点第三位。

    阈值刻意定得高（谚文要占到表意字符的 80%、假名占到 35%），
    否则"简体中文被读成韩文"这种混杂结果也会拿到加分，反而帮倒忙。
    """
    total = sum(stats.values()) or 1
    hangul, kana, cjk = stats["hangul"], stats["kana"], stats["cjk"]
    if family == F_EUCKR:
        share = _share(hangul, hangul + cjk)
        if hangul >= 8 and share > 0.80:
            return 0.10, f"谚文占表意字符 {share:.0%} 且共 {hangul} 字，韩文特征明确"
    if family in (F_SJIS, F_EUCJP):
        share = _share(kana, kana + cjk)
        if kana >= 8 and share > 0.35:
            return 0.10, (f"假名占表意字符 {share:.0%} 且共 {kana} 字，"
                          f"日文特征明确")
    return 0.0, ""


# ------------------------------------------------------------ 字节结构契合度
# 各编码族里"双字节对"的权重表：一级/常用字最高，扩展区最低。
_PAIR_WEIGHTS = {
    #      一级/常用  符号   二级/次常用  扩展    四字节
    F_GB: (1.00, 0.70, 0.50, 0.12, 0.50),
    F_BIG5: (1.00, 0.70, 0.50, 0.12, 0.00),
    F_SJIS: (1.00, 0.50, 0.75, 0.10, 0.45),
    F_EUCJP: (1.00, 0.00, 0.00, 0.15, 0.00),
    F_EUCKR: (1.00, 0.00, 0.00, 0.15, 0.00),
}

# 邻接系数下限：双字节对全都在"借 ASCII 字节当尾字节"时，结构分打到这个折扣
_ADJACENT_FLOOR = 0.45


def _pair_score(family: str, scan: dict) -> tuple:
    """把字节扫描结果折算成结构分。返回 ``(0..1, 明细)``。

    两步：

    1. 加权占比——落在该编码常用区间的双字节对越多越好
    2. 邻接系数——双字节对里"两个字节都是高位"的比例越高越好。
       真实中文文本汉字连片，这个比例接近 1；而西欧文本被硬当汉字读时，
       每个高位字节都在借后面的 ASCII 字母当尾字节，这个比例接近 0。
    """
    w_l1, w_sym, w_l2, w_ext, w_quad = _PAIR_WEIGHTS[family]
    total = scan["l1"] + scan["sym"] + scan["l2"] + scan["ext"] + scan["quad"]
    if total == 0:
        # 全是单字节 ASCII：结构维度没有判别力，给中性分
        return 0.5, {"pairs": 0, "note": "无双字节序列，结构维度无判别力"}

    base = (scan["l1"] * w_l1 + scan["sym"] * w_sym + scan["l2"] * w_l2
            + scan["ext"] * w_ext + scan["quad"] * w_quad) / total

    pairs = scan.get("pairs", 0)
    if pairs:
        adjacent = scan.get("adjacent", 0) / pairs
    else:
        adjacent = 1.0
    mult = _ADJACENT_FLOOR + (1.0 - _ADJACENT_FLOOR) * adjacent

    detail = {
        "l1": scan["l1"], "sym": scan["sym"], "l2": scan["l2"],
        "ext": scan["ext"], "quad": scan["quad"], "pairs": pairs,
        "adjacent_ratio": round(adjacent, 4),
        "base": round(base, 4), "adjacent_factor": round(mult, 4),
    }
    return base * mult, detail


def _scan_gb(data: bytes) -> dict:
    """GB2312/GBK/GB18030 的字节语法扫描。

    * 一级汉字 B0A1–D7F9、二级字 D8A1–F7FE、符号区 A1A1–A9FE
    * GBK 扩展（AA–AF 与 0xF8–0xFE 开头，或尾字节落在 0x40–0x7E）算生僻
    * GB18030 的四字节序列单独计数
    """
    i, n = 0, len(data)
    l1 = nsym = l2 = ext = quad = pairs = adjacent = 0
    while i < n:
        b = data[i]
        if b < 0x80:
            i += 1
            continue
        if not 0x81 <= b <= 0xFE or i + 1 >= n:
            ext += 1
            i += 1
            continue
        t = data[i + 1]
        # GB18030 四字节：首字节 + 0x30-0x39 + 0x81-0xFE + 0x30-0x39
        if (0x30 <= t <= 0x39 and i + 3 < n
                and 0x81 <= data[i + 2] <= 0xFE and 0x30 <= data[i + 3] <= 0x39):
            quad += 1
            i += 4
            continue
        if not (0x40 <= t <= 0x7E or 0x80 <= t <= 0xFE):
            ext += 1
            i += 1
            continue
        pairs += 1
        if t >= 0x80:
            adjacent += 1
        if 0xA1 <= b <= 0xA9 and t >= 0xA1:
            nsym += 1
        elif 0xB0 <= b <= 0xD7 and t >= 0xA1:
            l1 += 1
        elif 0xD8 <= b <= 0xF7 and t >= 0xA1:
            l2 += 1
        else:
            ext += 1
        i += 2
    return {"l1": l1, "sym": nsym, "l2": l2, "ext": ext, "quad": quad,
            "pairs": pairs, "adjacent": adjacent}


def _scan_big5(data: bytes) -> dict:
    """Big5：常用字 A440–C67E、次常用字 C940–F9D5、符号区 A1A1–A3FE。"""
    i, n = 0, len(data)
    l1 = nsym = l2 = ext = pairs = adjacent = 0
    while i < n:
        b = data[i]
        if b < 0x80:
            i += 1
            continue
        if not 0xA1 <= b <= 0xF9 or i + 1 >= n:
            ext += 1
            i += 1
            continue
        t = data[i + 1]
        if not (0x40 <= t <= 0x7E or 0xA1 <= t <= 0xFE):
            ext += 1
            i += 1
            continue
        pairs += 1
        if t >= 0x80:
            adjacent += 1
        if b <= 0xA3:
            nsym += 1
        elif b <= 0xC6:
            l1 += 1
        elif 0xC9 <= b <= 0xF9:
            l2 += 1
        else:
            ext += 1
        i += 2
    return {"l1": l1, "sym": nsym, "l2": l2, "ext": ext, "quad": 0,
            "pairs": pairs, "adjacent": adjacent}


def _scan_sjis(data: bytes) -> dict:
    """Shift_JIS / CP932：双字节区 + A1–DF 的半角片假名单字节。"""
    i, n = 0, len(data)
    k1 = k2 = jis = kana = ext = pairs = adjacent = 0
    while i < n:
        b = data[i]
        if b < 0x80:
            i += 1
            continue
        if 0xA1 <= b <= 0xDF:      # 半角片假名：单字节
            kana += 1
            i += 1
            continue
        if not (0x81 <= b <= 0x9F or 0xE0 <= b <= 0xFC) or i + 1 >= n:
            ext += 1
            i += 1
            continue
        t = data[i + 1]
        if not (0x40 <= t <= 0x7E or 0x80 <= t <= 0xFC):
            ext += 1
            i += 1
            continue
        pairs += 1
        if t >= 0x80:
            adjacent += 1
        code = (b << 8) | t
        if 0x889F <= code <= 0x9872:      # JIS X 0208 第 1 水准汉字
            k1 += 1
        elif 0xE000 <= b <= 0xFC:         # CP932 扩展 / 第 2 水准
            k2 += 1
        else:
            jis += 1
        i += 2
    return {"l1": k1, "sym": kana, "l2": jis, "ext": ext, "quad": k2,
            "pairs": pairs, "adjacent": adjacent}


def _scan_euc(data: bytes) -> dict:
    """EUC-JP / EUC-KR：双字节区均为 A1–FE。

    结构上两者完全同形，只能靠解码出来的文字系统（假名 / 谚文）收口。
    由于这个语法本身就要求"两个字节都是高位"，邻接系数恒为 1。
    """
    i, n = 0, len(data)
    pairs = ext = 0
    while i < n:
        b = data[i]
        if b < 0x80:
            i += 1
            continue
        if i + 1 < n and 0xA1 <= b <= 0xFE and 0xA1 <= data[i + 1] <= 0xFE:
            pairs += 1
            i += 2
        else:
            ext += 1
            i += 1
    return {"l1": pairs, "sym": 0, "l2": 0, "ext": ext, "quad": 0,
            "pairs": pairs, "adjacent": pairs}


def _scan_latin(data: bytes) -> dict:
    """西欧单字节编码的"结构"。

    单字节编码没有字节语法可言，但它有**分布**：真正的西欧文本里，
    高位字节基本都是 À–ÿ 区的字母（ö ü é ñ），其次是 °  §  ©  ½  这类
    符号；而 0x80–0x9F 那一段（cp1252 里一半是未定义槽位）在正常西欧
    文本里几乎不出现，却是 UTF-8 多字节序列的常客。
    """
    letters = punct = ctl = 0
    for b in data:
        if b < 0x80:
            continue
        if b >= 0xC0:
            letters += 1
        elif b >= 0xA0:
            punct += 1
        else:
            ctl += 1
    total = letters + punct + ctl
    if total == 0:
        return {"letters": 0, "punct": 0, "ctl": 0, "total": 0}
    score = (letters * 1.0 + punct * 0.70 + ctl * 0.35) / total
    return {"letters": letters, "punct": punct, "ctl": ctl, "total": total,
            "_score": score}


def byte_fit(data: bytes, family: str) -> tuple:
    """字节结构契合度。返回 ``(0..1, 明细)``。"""
    if family == F_UNICODE:
        # 能走到这里说明严格解码已成功，"结构合法"这件事已被证明。
        return 1.0, {"note": "严格解码成功即结构合法"}
    if family == F_LATIN:
        s = _scan_latin(data)
        if not s["total"]:
            return 0.5, {"note": "无双高位字节，结构维度无判别力"}
        return s["_score"], {
            "letters": s["letters"], "punct": s["punct"], "ctl": s["ctl"],
            "note": "À-ÿ 字母权重 1.0、符号 0.7、0x80-0x9F 区 0.35",
        }
    if family == F_GB:
        return _pair_score(F_GB, _scan_gb(data))
    if family == F_BIG5:
        return _pair_score(F_BIG5, _scan_big5(data))
    if family == F_SJIS:
        return _pair_score(F_SJIS, _scan_sjis(data))
    if family in (F_EUCJP, F_EUCKR):
        return _pair_score(family, _scan_euc(data))
    return 0.5, {"note": f"未知编码族 {family}"}
