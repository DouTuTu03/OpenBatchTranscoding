"""混合编码定位：无法解码的字节在哪里、逐行编码分布是什么。"""

from __future__ import annotations

import unittest

from obt.core import BY_NAME, find_undecodable, line_encodings
from obt.core.decode import decode_lenient, decode_strict, strip_bom_for, DecodeError
from tests.fixtures import MIXED, CN


class TestDecodeStrict(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(decode_strict("试验".encode("gbk"), BY_NAME["gbk"]), "试验")

    def test_raises_with_offset(self):
        with self.assertRaises(DecodeError) as ctx:
            decode_strict("试验".encode("gbk"), BY_NAME["utf-8"])
        err = ctx.exception
        self.assertEqual(err.codec.name, "utf-8")
        self.assertEqual(err.total, 4)
        self.assertGreaterEqual(err.offset, 0)
        self.assertTrue(err.raw)
        self.assertIn("严格解码失败", str(err))

    def test_lenient_is_for_display_only(self):
        text = decode_lenient("试验".encode("gbk"), BY_NAME["utf-8"])
        self.assertIn("\ufffd", text)

    def test_strip_bom_for(self):
        raw = b"\xef\xbb\xbf" + "试验".encode("utf-8")
        payload, bom = strip_bom_for(raw, BY_NAME["utf-8"])
        self.assertEqual(bom, b"\xef\xbb\xbf")
        self.assertEqual(payload, "试验".encode("utf-8"))

    def test_strip_bom_skipped_when_codec_disagrees(self):
        # BOM 说 utf-8，但用户指定按 gbk 读：不能悄悄把 BOM 丢掉
        raw = b"\xef\xbb\xbf" + "试验".encode("gbk")
        payload, bom = strip_bom_for(raw, BY_NAME["gbk"])
        self.assertEqual(bom, b"")
        self.assertEqual(payload, raw)


class TestFindUndecodable(unittest.TestCase):
    def test_clean_file_has_no_runs(self):
        self.assertEqual(find_undecodable("试验报告\n".encode("utf-8"),
                                          BY_NAME["utf-8"]), [])

    def test_locates_the_gbk_line(self):
        runs = find_undecodable(MIXED, BY_NAME["utf-8"])
        self.assertEqual(len(runs), 1)
        run = runs[0]
        # 第一行是 UTF-8，长度就是坏字节的起始偏移
        first_line = MIXED.split(b"\n")[0] + b"\n"
        self.assertEqual(run.offset, len(first_line))
        self.assertGreater(run.length, 0)
        self.assertTrue(run.raw.decode("gbk").startswith("第二行"))

    def test_reports_context(self):
        runs = find_undecodable(MIXED, BY_NAME["utf-8"])
        self.assertTrue(runs[0].before)
        self.assertTrue(runs[0].after)

    def test_reports_multiple_runs(self):
        data = (b"ok\n" + "试".encode("gbk") + b"\nok\n" + "验".encode("gbk") + b"\n")
        runs = find_undecodable(data, BY_NAME["utf-8"])
        self.assertEqual(len(runs), 2)
        self.assertLess(runs[0].offset, runs[1].offset)

    def test_respects_limit(self):
        data = b"".join("试验".encode("gbk") + b"\n" for _ in range(10))
        runs = find_undecodable(data, BY_NAME["utf-8"], limit=3)
        self.assertEqual(len(runs), 3)

    def test_offsets_match_decode_error(self):
        # 增量解码器的偏移必须和一次性严格解码报的位置一致
        try:
            decode_strict(MIXED, BY_NAME["utf-8"])
        except DecodeError as e:
            expected = e.offset
        self.assertEqual(find_undecodable(MIXED, BY_NAME["utf-8"])[0].offset, expected)


class TestLineEncodings(unittest.TestCase):
    def test_uniform_file_has_no_anomalies(self):
        report = line_encodings(CN.replace("\r\n", "\n").encode("gbk"),
                                dominant=BY_NAME["gbk"])
        self.assertFalse(report.mixed)
        self.assertEqual(report.anomalies, [])
        self.assertEqual(report.counts.get("gbk"), 4)

    def test_finds_the_odd_line(self):
        report = line_encodings(MIXED, dominant=BY_NAME["utf-8"])
        self.assertTrue(report.mixed)
        self.assertEqual(len(report.anomalies), 1)
        anomaly = report.anomalies[0]
        self.assertEqual(anomaly.line_no, 2)
        self.assertEqual(anomaly.encoding, "gbk")
        self.assertIn("第二行", anomaly.preview)

    def test_counts_ascii_lines_separately(self):
        data = b"code line\n" + "中文行\n".encode("gbk") + b"another ascii\n"
        report = line_encodings(data, dominant=BY_NAME["gbk"])
        self.assertEqual(report.counts.get("ascii"), 2)
        self.assertEqual(report.counts.get("gbk"), 1)

    def test_utf16_is_skipped_with_reason(self):
        report = line_encodings("试验\r\n报告\r\n".encode("utf-16-le"),
                                dominant=BY_NAME["utf-16le"])
        self.assertIn("跳过逐行分析", report.skipped)
        self.assertEqual(report.analyzed, 0)

    def test_line_limit_is_reported(self):
        data = b"line\n" * 50
        report = line_encodings(data, dominant=BY_NAME["utf-8"], limit=10)
        self.assertEqual(report.analyzed, 10)
        self.assertIn("仅分析了前 10 行", report.skipped)

    def test_as_dict_round_trip(self):
        import json
        report = line_encodings(MIXED, dominant=BY_NAME["utf-8"])
        payload = json.loads(json.dumps(report.as_dict(), ensure_ascii=False))
        self.assertTrue(payload["mixed"])
        self.assertEqual(payload["anomalies"][0]["line"], 2)


if __name__ == "__main__":
    unittest.main()
