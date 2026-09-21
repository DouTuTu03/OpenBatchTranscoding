"""编码注册表：BOM 匹配顺序、别名归一、超集关系。"""

from __future__ import annotations

import unittest

from obt.core import BY_NAME, CODECS, UnknownEncoding, match_bom, resolve, resolve_target
from obt.core.heuristics import SIMP_ONLY, TRAD_ONLY


class TestBom(unittest.TestCase):
    def test_utf8_bom(self):
        name, payload = match_bom(b"\xef\xbb\xbfhello")
        self.assertEqual(name, "utf-8")
        self.assertEqual(payload, b"hello")

    def test_utf32le_wins_over_utf16le(self):
        # UTF-32LE 的 BOM 以 UTF-16LE 的 BOM 为前缀，顺序错了就会认错
        name, payload = match_bom(b"\xff\xfe\x00\x00A\x00\x00\x00")
        self.assertEqual(name, "utf-32le")
        self.assertEqual(payload, b"A\x00\x00\x00")

    def test_utf16le(self):
        name, _ = match_bom(b"\xff\xfeA\x00")
        self.assertEqual(name, "utf-16le")

    def test_no_bom(self):
        self.assertIsNone(match_bom(b"plain text"))


class TestResolve(unittest.TestCase):
    def test_aliases(self):
        for alias, expect in [
            ("utf8", "utf-8"), ("UTF-8", "utf-8"), ("cp936", "gbk"),
            ("gb2312", "gbk"), ("ansi", "gbk"), ("sjis", "shift_jis"),
            ("cp932", "shift_jis"), ("latin1", "latin-1"),
            ("iso-8859-1", "latin-1"), ("UHC", "euc-kr"), ("950", "big5"),
        ]:
            with self.subTest(alias=alias):
                self.assertEqual(resolve(alias).name, expect)

    def test_unknown_encoding_gives_suggestions(self):
        with self.assertRaises(UnknownEncoding) as ctx:
            resolve("gb-k-2312-typo")
        self.assertIn("不认识的编码", str(ctx.exception))
        self.assertIn("obt encodings", str(ctx.exception))

    def test_target_bom_intent(self):
        codec, bom = resolve_target("utf-8-bom")
        self.assertEqual(codec.name, "utf-8")
        self.assertTrue(bom)

        codec, bom = resolve_target("utf8sig")
        self.assertEqual(codec.name, "utf-8")
        self.assertTrue(bom)

        # 不带 bom 时按编码默认：UTF-8 默认不写 BOM
        codec, bom = resolve_target("utf-8")
        self.assertFalse(bom)

        # UTF-16 必须写 BOM，否则下次没人认得出来
        codec, bom = resolve_target("utf-16le")
        self.assertTrue(bom)

    def test_bom_suffix_does_not_break_normal_names(self):
        # gb18030 以 "0" 结尾，不能被当成 BOM 后缀误伤
        self.assertEqual(resolve("gb18030").name, "gb18030")
        self.assertEqual(resolve("shift_jis").name, "shift_jis")


class TestRegistry(unittest.TestCase):
    def test_names_unique(self):
        names = [c.name for c in CODECS]
        self.assertEqual(len(names), len(set(names)))

    def test_trad_simp_sets_disjoint_and_useful(self):
        # 两个集合必须互斥，否则繁简判据会自己抵消
        self.assertFalse(SIMP_ONLY & TRAD_ONLY)
        # 而且剩下的判别位要足够多，太少就失去意义
        self.assertGreater(len(SIMP_ONLY), 40)
        self.assertGreater(len(TRAD_ONLY), 40)

    def test_superset_present(self):
        # 判定逻辑里对"同一种解读"做了特判，依赖这两个编码都存在
        self.assertIn("gbk", BY_NAME)
        self.assertIn("gb18030", BY_NAME)

    def test_utf16_excluded_from_pool(self):
        from obt.core.encodings import CODECS_POOL
        self.assertNotIn("utf-16le", [c.name for c in CODECS_POOL])


if __name__ == "__main__":
    unittest.main()
