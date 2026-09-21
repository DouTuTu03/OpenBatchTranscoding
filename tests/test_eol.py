"""行尾检测与转换。刻意与"改编码"解耦：默认策略是 keep。"""

from __future__ import annotations

import unittest

from obt.core.eol import (
    EOL_CR, EOL_CRLF, EOL_LF, EOL_MIXED, EOL_NONE, apply_eol, count_eol,
    detect_eol, eol_label, eol_literal,
)


class TestDetect(unittest.TestCase):
    def test_lf(self):
        self.assertEqual(detect_eol("a\nb\nc"), EOL_LF)

    def test_crlf(self):
        self.assertEqual(detect_eol("a\r\nb\r\nc"), EOL_CRLF)

    def test_cr_only(self):
        self.assertEqual(detect_eol("a\rb\rc"), EOL_CR)

    def test_mixed(self):
        self.assertEqual(detect_eol("a\r\nb\nc"), EOL_MIXED)

    def test_none(self):
        self.assertEqual(detect_eol("single line"), EOL_NONE)

    def test_empty(self):
        self.assertEqual(detect_eol(""), EOL_NONE)

    def test_counts(self):
        self.assertEqual(count_eol("a\r\nb\nc\r\n"),
                         {"crlf": 2, "lf": 1, "cr": 0})


class TestApply(unittest.TestCase):
    def test_keep_is_identity(self):
        raw = "a\r\nb\nc\rd"
        self.assertEqual(apply_eol(raw, "keep"), raw)

    def test_to_lf(self):
        self.assertEqual(apply_eol("a\r\nb\rc", EOL_LF), "a\nb\nc")

    def test_to_crlf(self):
        self.assertEqual(apply_eol("a\nb\rc", EOL_CRLF), "a\r\nb\r\nc")

    def test_to_cr(self):
        self.assertEqual(apply_eol("a\r\nb\nc", EOL_CR), "a\rb\rc")

    def test_no_double_cr(self):
        # 必须先把所有风格归一，否则 \r\n 会被二次处理成 \r\r\n
        out = apply_eol("a\r\nb", EOL_CRLF)
        self.assertEqual(out, "a\r\nb")
        self.assertNotIn("\r\r", out)

        out2 = apply_eol("a\r\nb", EOL_LF)
        self.assertNotIn("\r", out2)

    def test_idempotent(self):
        for style in (EOL_LF, EOL_CRLF, EOL_CR):
            with self.subTest(style=style):
                once = apply_eol("a\r\nb\nc\r", style)
                self.assertEqual(apply_eol(once, style), once)

    def test_char_count_effect_is_predictable(self):
        raw = "a\r\nb\r\nc"
        self.assertEqual(len(apply_eol(raw, EOL_LF)), len(raw) - 2)
        self.assertEqual(len(apply_eol(raw, EOL_CRLF)), len(raw))

    def test_bad_style(self):
        with self.assertRaises(ValueError):
            apply_eol("a", "mac-classic")


class TestLabels(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(eol_label(EOL_CRLF), "CRLF")
        self.assertEqual(eol_label(EOL_MIXED), "混合")
        self.assertEqual(eol_literal(EOL_CRLF), "\\r\\n")


if __name__ == "__main__":
    unittest.main()
