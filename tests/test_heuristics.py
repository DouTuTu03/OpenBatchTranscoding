"""启发式打分：文本质量、文字系统一致性、字节结构契合度。"""

from __future__ import annotations

import unittest

from obt.core import detect
from obt.core.encodings import (
    F_BIG5, F_EUCKR, F_GB, F_LATIN, F_SJIS, F_UNICODE,
)
from obt.core.heuristics import (
    byte_fit, family_script_fit, script_stats, text_quality,
)


class TestTextQuality(unittest.TestCase):
    def test_clean_text_scores_one(self):
        score, _ = text_quality("正常的文本，含中英文 mixed 123。\n")
        self.assertAlmostEqual(score, 1.0, places=3)

    def test_tabs_and_newlines_allowed(self):
        score, _ = text_quality("a\tb\nc\rd\fe\v")
        self.assertAlmostEqual(score, 1.0, places=3)

    def test_control_chars_penalised(self):
        score, detail = text_quality("abc\x01\x02\x03")
        self.assertLess(score, 0.6)
        self.assertEqual(detail["ctrl"], 3)

    def test_c1_controls_penalised(self):
        # 0x80-0x9F 是"UTF-8 被当西欧编码读"的典型产物
        score, detail = text_quality("abc\x81\x8d\x8f\x90\x9d")
        self.assertLess(score, 0.7)
        self.assertGreater(detail["c1"], 0)

    def test_replacement_and_pua_penalised(self):
        s1, d1 = text_quality("abc\ufffd\ufffd")
        self.assertLess(s1, 0.35)
        self.assertEqual(d1["fffd"], 2)
        s2, d2 = text_quality("abc\ue000\ue001")
        self.assertLess(s2, 0.6)
        self.assertEqual(d2["pua"], 2)

    def test_empty(self):
        self.assertEqual(text_quality("")[0], 1.0)


class TestScriptStats(unittest.TestCase):
    def test_counts_by_script(self):
        st = script_stats("中文abc한국어かなカナ")
        self.assertEqual(st["cjk"], 2)
        self.assertEqual(st["ascii"], 3)
        self.assertEqual(st["hangul"], 3)
        self.assertEqual(st["kana"], 4)

    def test_trad_simp_classified(self):
        st = script_stats("温度溫度")
        self.assertEqual(st["simp"], 1)   # 温
        self.assertEqual(st["trad"], 1)   # 溫


class TestFamilyScriptFit(unittest.TestCase):
    def test_latin_rejects_ideographs(self):
        st = script_stats("中文内容")
        score, detail = family_script_fit(F_LATIN, st, True)
        self.assertLess(score, 0.2)
        self.assertIn("why", detail)

    def test_latin_mojibake_signature(self):
        # UTF-8 中文被当 cp1252 读：满屏 À-ÿ 区字母（"试验报告结论"的错读）
        st = script_stats("Ã¦ÂµÂ‹Ã¨Â¯Â•Ã¦ÂŠÂ¥Ã¥Â‘ÂŠÃ§Â»Â“Ã¨Â®Âº")
        score, detail = family_script_fit(F_LATIN, st, True)
        self.assertLess(score, 0.3)
        self.assertIn("乱码特征", detail["why"])

    def test_latin_accepts_normal_german(self):
        st = script_stats("Umgebungstemperatur 85 °C, Grösse Änderung")
        score, _ = family_script_fit(F_LATIN, st, True)
        self.assertGreater(score, 0.9)

    def test_gbk_expects_simplified(self):
        st = script_stats("试验报告温度设备记录" * 2)
        gb_score, _ = family_script_fit(F_GB, st, True)
        big5_score, _ = family_script_fit(F_BIG5, st, True)
        self.assertGreater(gb_score, big5_score)

    def test_big5_expects_traditional(self):
        st = script_stats("試驗報告溫度設備記錄" * 2)
        gb_score, _ = family_script_fit(F_GB, st, True)
        big5_score, _ = family_script_fit(F_BIG5, st, True)
        self.assertGreater(big5_score, gb_score)

    def test_korean_and_japanese_are_decisive(self):
        kr = script_stats("항목 조건 결론 시험 보고서 " * 3)
        s, detail = family_script_fit(F_EUCKR, kr, True)
        self.assertGreater(s, 0.95)
        self.assertIn("谚文", detail["why"])

        # 假名占表意字符的比例要高，才够得上"决定性"
        jp = script_stats("これはテストです。それもテストです。" * 3)
        s, detail = family_script_fit(F_SJIS, jp, True)
        self.assertGreater(s, 0.95)
        self.assertIn("假名", detail["why"])

    def test_japanese_with_mostly_kanji_is_not_decisive(self):
        # 通篇汉字、只有零星假名 → 不该给满分（这可能是中文被误读）
        kanji_heavy = script_stats("試験報告項目条件結論電圧電流温度湿度" * 2 + "です")
        s, _ = family_script_fit(F_SJIS, kanji_heavy, True)
        self.assertLess(s, 0.7)

    def test_tiny_sample_recognised_but_flagged_ambiguous(self):
        """样本太小时判定仍然给出，但必须明说"证据不够"。

        这比"给个假自信"有用得多：4 个字节的输入本来就无法可靠区分
        EUC-KR 与 GBK（两者字节空间重叠），工具该做的是把这件事讲清楚。
        """
        tiny = detect("한국".encode("euc_kr"))
        self.assertEqual(tiny.encoding, "euc-kr")
        self.assertTrue(tiny.ambiguous)
        self.assertLess(tiny.confidence, 0.8)

        big = detect(("한국어로 된 문서입니다. " * 6).encode("euc_kr"))
        self.assertEqual(big.encoding, "euc-kr")
        self.assertFalse(big.ambiguous)
        self.assertGreater(big.confidence, tiny.confidence)

    def test_tiny_gbk_sample_flagged_too(self):
        tiny = detect("试验".encode("gbk"))
        self.assertEqual(tiny.encoding, "gbk")
        self.assertTrue(tiny.ambiguous)
        self.assertLess(tiny.confidence, 0.8)

    def test_korean_codec_penalised_on_mixed_hangul_hanja(self):
        # "简体中文被当韩文读"会得到谚文+汉字混杂，真韩文里汉字只是点缀
        mixed = script_stats("항목" * 5 + "漢字漢字漢字漢字漢字")
        s_mixed, _ = family_script_fit(F_EUCKR, mixed, True)
        pure = script_stats("항목" * 10)
        s_pure, _ = family_script_fit(F_EUCKR, pure, True)
        self.assertLess(s_mixed, s_pure - 0.2)

    def test_ascii_has_no_discriminating_power(self):
        score, detail = family_script_fit(F_GB, script_stats("abc"), False)
        self.assertEqual(score, 1.0)
        self.assertIn("纯 ASCII", detail["note"])


class TestByteFit(unittest.TestCase):
    def test_unicode_family_is_free(self):
        # 能走到这一步说明严格解码已成功，结构合法性已被证明
        self.assertEqual(byte_fit(b"\x00\x01", F_UNICODE)[0], 1.0)

    def test_gb_common_hanzi_beat_borrowed_ascii_trails(self):
        # 一级汉字区（最常用 3755 字）应该拿高分
        good = byte_fit(("试验报告温度设备" * 4).encode("gbk"), F_GB)[0]
        # 高位字节后面跟 ASCII 字母：合法的 GBK 扩展区，但真实中文文本里不该这样
        borrowed = byte_fit(b"\xb0C\xc4n\xf6s\xb6\xa1", F_GB)[0]
        self.assertGreater(good, 0.85)
        self.assertLess(borrowed, 0.45)

    def test_adjacent_penalty_catches_borrowed_ascii_trails(self):
        # 每个高位字节都在"借"后面的 ASCII 字母当尾字节 —— 硬凑的典型特征
        borrowed = b"\xb0C\xc4n\xf6s"
        score, detail = byte_fit(borrowed, F_BIG5)
        self.assertEqual(detail["adjacent_ratio"], 0.0)
        self.assertLess(score, 0.45)

    def test_real_big5_beats_borrowed_ascii(self):
        # 真 Big5 文本里汉字连片，邻接比例高；"西欧文本被硬当 Big5 读"则是
        # 每个高位字节都在借后面的 ASCII 字母当尾字节
        real = ("試驗報告溫度" * 6).encode("big5")
        real_score, real_detail = byte_fit(real, F_BIG5)
        fake_score, fake_detail = byte_fit(b"\xb0C\xc4n\xf6s", F_BIG5)
        self.assertGreater(real_detail["adjacent_ratio"],
                           fake_detail["adjacent_ratio"] + 0.3)
        self.assertGreater(real_score, fake_score * 1.5)

    def test_latin_prefers_letters_over_c1_range(self):
        letters = byte_fit("Grösse Änderung ü".encode("cp1252"), F_LATIN)[0]
        c1 = byte_fit(bytes([0x81, 0x8D, 0x8F, 0x90, 0x9D]), F_LATIN)[0]
        self.assertGreater(letters, 0.9)
        self.assertLess(c1, 0.5)

    def test_pure_ascii_has_no_structure_evidence(self):
        for family in (F_GB, F_BIG5, F_SJIS, F_EUCKR, F_LATIN):
            with self.subTest(family=family):
                self.assertEqual(byte_fit(b"plain ascii", family)[0], 0.5)


class TestRegressionOnRealBytes(unittest.TestCase):
    """端到端回归：这些判据组合起来必须能分开真实样本。"""

    def test_gbk_file_not_stolen_by_euc_kr(self):
        from tests.fixtures import CN
        det = detect(CN.encode("gbk"))
        self.assertEqual(det.encoding, "gbk")
        # EUC-KR 的谚文区与 GB2312 一级汉字区字节空间重叠，
        # 必须靠文字系统把它压下去，且不允许报"存在歧义"
        self.assertFalse(det.ambiguous)
        self.assertGreaterEqual(det.confidence, 0.6)

    def test_gb18030_is_same_reading_not_a_rival(self):
        from tests.fixtures import CN
        det = detect(CN.encode("gbk"))
        alts = [c.codec.name for c in det.candidates if c.strict]
        self.assertIn("gb18030", alts)          # 它确实也能解
        self.assertNotIn("歧义", det.note)       # 但不该被当成竞争假设


if __name__ == "__main__":
    unittest.main()
