"""嗅探引擎：语料全量对照 + 各条特殊路径。"""

from __future__ import annotations

import codecs
import unittest

from obt.core import BY_NAME, detect, resolve
from tests import fixtures


class TestCorpus(unittest.TestCase):
    """13 份真实字节样本逐一对照。这一组挂了就是引擎跑偏了。"""

    def test_every_sample_matches_expectation(self):
        for name, data, expect, note in fixtures.corpus():
            with self.subTest(sample=name, note=note):
                det = detect(data)
                self.assertTrue(
                    fixtures.matches(det, expect),
                    f"{name}：期望 {expect}，实际 {det.encoding}"
                    f"（strict={det.strict_decodable}, conf={det.confidence:.2f}）",
                )

    def test_clear_cases_are_confident(self):
        # 判定正确还不够，还得有底气。低于 0.6 的结论会让人不敢下手。
        for name, data, expect, _note in fixtures.corpus():
            if expect in ("混合", "binary"):
                continue
            with self.subTest(sample=name):
                det = detect(data)
                self.assertGreaterEqual(det.confidence, 0.6)


class TestBomPaths(unittest.TestCase):
    def test_utf8_bom(self):
        det = detect(codecs.BOM_UTF8 + "试验\r\n".encode("utf-8"))
        self.assertEqual(det.encoding, "utf-8")
        self.assertTrue(det.has_bom)
        self.assertEqual(det.bom, b"\xef\xbb\xbf")
        self.assertEqual(det.label, "utf-8 (BOM)")
        self.assertGreaterEqual(det.confidence, 0.98)

    def test_utf16le_bom(self):
        det = detect("试验\r\n".encode("utf-16"))
        self.assertEqual(det.encoding, "utf-16le")
        self.assertEqual(det.eol, "crlf")

    def test_bom_lying_about_content_is_ignored(self):
        # BOM 说是 UTF-8，后面却是 GBK 内容：不能信 BOM
        data = codecs.BOM_UTF8 + "试验报告".encode("gbk")
        det = detect(data)
        self.assertFalse(det.has_bom)
        self.assertIn("已忽略 BOM 重新嗅探", " ".join(det.evidence))
        self.assertLessEqual(det.confidence, 0.6)


class TestSpecialPaths(unittest.TestCase):
    def test_empty_file(self):
        det = detect(b"")
        self.assertEqual(det.encoding, "empty")
        self.assertEqual(det.confidence, 1.0)

    def test_pure_ascii(self):
        det = detect(b"ExpVoltage=250\n")
        self.assertEqual(det.encoding, "ascii")
        self.assertEqual(det.confidence, 1.0)
        self.assertIn("无编码歧义", det.note)

    def test_single_nul_means_not_text(self):
        # 真正的文本文件里不该出现 NUL
        det = detect(b"hello\x00world, this is not text")
        self.assertTrue(det.is_binary)

    def test_utf16_without_bom(self):
        det = detect("试验报告 250V\r\n".encode("utf-16-le"))
        self.assertEqual(det.encoding, "utf-16le")
        self.assertFalse(det.has_bom)
        self.assertIn("NUL", " ".join(det.evidence))

    def test_utf16_parity_must_be_consistent(self):
        # NUL 落在两个奇偶位上 → 不是 UTF-16，而是含 NUL 的二进制
        data = b"A\x00B\x00" + b"\x00C\x00D"
        det = detect(data)
        self.assertTrue(det.is_binary)

    def test_mixed_encoding_reported_as_unstrict(self):
        det = detect(fixtures.MIXED)
        self.assertFalse(det.strict_decodable)
        self.assertGreater(det.replacements, 0)
        self.assertIn("混合编码", det.note)
        self.assertLess(det.confidence, 0.5)

    def test_mixed_evidence_names_the_closest_encoding(self):
        det = detect(fixtures.MIXED)
        self.assertTrue(any("最接近的是" in e for e in det.evidence))

    def test_mixed_still_reports_eol(self):
        # 行尾与编码正交：解不全不代表换行风格看不出来
        det = detect(fixtures.MIXED)
        self.assertEqual(det.eol, "lf")

        crlf_mixed = fixtures.MIXED.replace(b"\n", b"\r\n")
        self.assertEqual(detect(crlf_mixed).eol, "crlf")

    def test_gb18030_four_byte_sequence(self):
        det = detect("😀 四字节\n".encode("gb18030"))
        self.assertEqual(det.encoding, "gb18030")

    def test_hint_skips_detection(self):
        det = detect("试验".encode("gbk"), hint=resolve("gbk"))
        self.assertTrue(det.forced)
        self.assertEqual(det.encoding, "gbk")
        self.assertIn("--from", " ".join(det.evidence))

    def test_hint_that_cannot_decode_is_flagged(self):
        det = detect("试验".encode("gbk"), hint=resolve("utf-8"))
        self.assertFalse(det.strict_decodable)
        self.assertTrue(det.forced)
        self.assertIn("解码失败", " ".join(det.evidence))

    def test_hint_strips_bom_before_decoding(self):
        det = detect(codecs.BOM_UTF8 + "试验".encode("utf-8"), hint=resolve("utf-8"))
        self.assertEqual(det.text, "试验")
        self.assertEqual(det.bom, b"\xef\xbb\xbf")


class TestEvidenceQuality(unittest.TestCase):
    """每次判定都要留得下证据，否则出问题没法复盘。"""

    def test_evidence_mentions_scores(self):
        det = detect("试验报告温度".encode("gbk") * 3)
        self.assertGreaterEqual(len(det.evidence), 3)
        self.assertTrue(any("打分：" in e for e in det.evidence))

    def test_alternatives_listed(self):
        det = detect("試験報告です。これはテストです。".encode("shift_jis") * 2)
        self.assertTrue(any("次优候选" in e for e in det.evidence))

    def test_candidates_carry_detail_for_explain(self):
        det = detect("试验报告温度".encode("gbk"))
        gbk = next(c for c in det.candidates if c.codec.name == "gbk")
        self.assertTrue(gbk.strict)
        self.assertIn("struct", gbk.detail)
        self.assertIn("adjacent_ratio", gbk.detail["struct"])

    def test_as_dict_is_json_friendly(self):
        import json
        det = detect("试验报告".encode("gbk") * 2)
        payload = json.dumps(det.as_dict(), ensure_ascii=False)
        self.assertIn("gbk", payload)
        # 批量场景（转码计划）默认不带逐候选明细，避免 JSON 爆炸
        self.assertNotIn("candidates", det.as_dict(with_candidates=False))


if __name__ == "__main__":
    unittest.main()
