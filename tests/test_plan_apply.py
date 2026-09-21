"""转码计划与落地：拒绝规则、原子写、替换前校验、备份。"""

from __future__ import annotations

import codecs
import os
import tempfile
import unittest
from pathlib import Path

from obt.core import (
    STATUS_CONVERT, STATUS_ERROR, apply_actions, build_plan, iter_files, resolve,
)
from tests.fixtures import BINARY, CN, MIXED

UTF8BOM = resolve("utf-8-bom")
UTF8 = resolve("utf-8")
GBK = resolve("gbk")


class PlanCase(unittest.TestCase):
    """公共夹具：临时目录 + 几个典型文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.gbk = self.dir / "legacy.cs"
        self.gbk.write_bytes(CN.encode("gbk"))
        self.utf8 = self.dir / "modern.txt"
        # 保留 CRLF：这样才能测出"--only-mismatch 只看编码、不看换行"的语义
        self.utf8.write_bytes(codecs.BOM_UTF8 + CN.encode("utf-8"))
        self.mixed = self.dir / "mixed.log"
        self.mixed.write_bytes(MIXED)
        self.binary = self.dir / "app.bin"
        self.binary.write_bytes(BINARY)

    def tearDown(self):
        self._tmp.cleanup()

    def plan(self, paths=None, **kw):
        kw.setdefault("to", UTF8BOM)
        kw.setdefault("write_bom", True)
        paths = paths if paths is not None else [self.gbk, self.utf8, self.mixed, self.binary]
        return build_plan(paths, **kw)

    def by_name(self, plan):
        return {a.path.name: a for a in plan}


class TestPlanDecisions(PlanCase):
    def test_gbk_needs_conversion(self):
        act = self.by_name(self.plan())["legacy.cs"]
        self.assertEqual(act.status, STATUS_CONVERT)
        self.assertEqual(act.src_encoding, "gbk")
        self.assertEqual(act.dst_encoding, "utf-8")
        self.assertIn("gbk → utf-8", act.reason)
        self.assertIn("补写 BOM", act.reason)

    def test_already_target_is_skipped(self):
        act = self.by_name(self.plan())["modern.txt"]
        self.assertEqual(act.status, "skip")
        self.assertIn("无变化", act.reason)

    def test_mixed_is_refused(self):
        act = self.by_name(self.plan())["mixed.log"]
        self.assertEqual(act.status, STATUS_ERROR)
        self.assertIn("拒绝", act.reason)
        # 拒绝的文件绝不能带出可写内容
        self.assertEqual(act.payload, b"")

    def test_binary_is_skipped_by_default(self):
        act = self.by_name(self.plan())["app.bin"]
        self.assertEqual(act.status, "skip")
        self.assertIn("非文本", act.reason)

    def test_binary_can_be_made_an_error(self):
        act = self.by_name(self.plan(skip_binary=False))["app.bin"]
        self.assertEqual(act.status, STATUS_ERROR)

    def test_low_confidence_threshold_refuses(self):
        act = self.by_name(self.plan(min_confidence=1.01))["legacy.cs"]
        self.assertEqual(act.status, STATUS_ERROR)
        self.assertIn("置信度", act.reason)
        self.assertIn("--from", act.reason)

    def test_from_override_bypasses_confidence(self):
        act = self.by_name(self.plan(from_codec=GBK, min_confidence=1.01))["legacy.cs"]
        self.assertEqual(act.status, STATUS_CONVERT)

    def test_only_mismatch_skips_correct_encoding(self):
        # 编码已是 utf-8 + BOM，但要求换行归一 → 默认会改
        self.assertEqual(self.by_name(self.plan(eol="lf"))["modern.txt"].status,
                         STATUS_CONVERT)
        # --only-mismatch 只关心编码：编码已对就跳过，换行不再管
        act = self.by_name(self.plan(eol="lf", only_mismatch=True))["modern.txt"]
        self.assertEqual(act.status, "skip")
        self.assertIn("--only-mismatch", act.reason)

    def test_unencodable_target_is_refused_with_reason(self):
        emoji = self.dir / "emoji.txt"
        emoji.write_bytes("试验😀完成\n".encode("utf-8"))
        plan = build_plan([emoji], to=GBK, write_bom=False)
        act = plan[0]
        self.assertEqual(act.status, STATUS_ERROR)
        self.assertIn("无法表示", act.reason)
        self.assertIn("U+1F600", act.reason)

    def test_emoji_is_fine_in_gb18030(self):
        emoji = self.dir / "emoji.txt"
        emoji.write_bytes("试验😀完成\n".encode("utf-8"))
        act = build_plan([emoji], to=resolve("gb18030"), write_bom=False)[0]
        self.assertEqual(act.status, STATUS_CONVERT)

    def test_max_size_skips_big_files(self):
        act = self.by_name(self.plan(max_size=10))["legacy.cs"]
        self.assertEqual(act.status, "skip")
        self.assertIn("max-size", act.reason)

    def test_eol_change_is_described(self):
        act = self.by_name(self.plan(eol="lf"))["legacy.cs"]
        self.assertIn("CRLF → LF", act.reason)

    def test_chars_count_is_post_normalisation(self):
        # 自校验要比的是换行归一之后的字符数，否则每个 CRLF 文件都会误报
        act = self.by_name(self.plan(eol="lf"))["legacy.cs"]
        expected = len(CN.replace("\r\n", "\n"))
        self.assertEqual(act.chars, expected)

    def test_payload_round_trips_losslessly(self):
        act = self.by_name(self.plan(eol="lf"))["legacy.cs"]
        payload, bom = self._split(act.payload)
        self.assertEqual(bom, b"\xef\xbb\xbf")
        self.assertEqual(payload.decode("utf-8"), CN.replace("\r\n", "\n"))

    @staticmethod
    def _split(payload):
        return payload[3:], payload[:3]

    def test_missing_file_is_error_not_crash(self):
        act = build_plan([self.dir / "nope.txt"], to=UTF8BOM, write_bom=True)[0]
        self.assertEqual(act.status, STATUS_ERROR)


class TestApply(PlanCase):
    def test_out_dir_mirrors_structure(self):
        nested = self.dir / "src" / "sub"
        nested.mkdir(parents=True)
        target = nested / "a.cs"
        target.write_bytes(CN.encode("gbk"))
        out = self.dir / "out"
        plan = build_plan([target], to=UTF8BOM, write_bom=True)
        results = apply_actions(plan, out_dir=out, root=nested.parent.parent)
        self.assertTrue(results[0].ok)
        written = out / "src" / "sub" / "a.cs"
        self.assertTrue(written.exists())
        self.assertEqual(written.read_bytes()[:3], b"\xef\xbb\xbf")

    def test_in_place_conversion(self):
        plan = self.plan([self.gbk], eol="lf")
        results = apply_actions(plan)
        self.assertTrue(results[0].ok)
        self.assertTrue(results[0].verified)
        raw = self.gbk.read_bytes()
        self.assertEqual(raw[:3], b"\xef\xbb\xbf")
        self.assertEqual(raw[3:].decode("utf-8"), CN.replace("\r\n", "\n"))
        self.assertEqual(results[0].detail["detection_after"], "utf-8")
        self.assertNotIn(b"\r\n", raw)

    def test_in_place_keeps_eol_by_default(self):
        apply_actions(self.plan([self.gbk]))
        raw = self.gbk.read_bytes()
        self.assertEqual(raw[3:].decode("utf-8"), CN)

    def test_no_temp_files_left_behind(self):
        apply_actions(self.plan())
        leftovers = [p.name for p in self.dir.iterdir() if p.name.endswith(".obt-tmp")]
        self.assertEqual(leftovers, [])

    def test_dry_run_touches_nothing(self):
        before = {p.name: p.read_bytes() for p in self.dir.iterdir() if p.is_file()}
        plan = self.plan()
        results = apply_actions(plan, dry_run=True)
        self.assertTrue(all(r.ok for r in results))
        self.assertTrue(all(not r.verified for r in results))
        after = {p.name: p.read_bytes() for p in self.dir.iterdir() if p.is_file()}
        self.assertEqual(before, after)

    def test_backup_created_only_when_writing(self):
        plan = self.plan([self.gbk])
        results = apply_actions(plan, backup=True)
        bak = Path(str(self.gbk) + ".bak")
        self.assertTrue(bak.exists())
        self.assertEqual(bak.read_bytes(), CN.encode("gbk"))
        self.assertEqual(results[0].backup, bak)

    def test_skipped_actions_produce_no_results(self):
        plan = self.plan()  # 含 skip 与 error
        results = apply_actions(plan)
        self.assertEqual(len(results), 1)          # 只有 legacy.cs 需要写
        self.assertEqual(results[0].action.path, self.gbk)

    def test_keep_mtime(self):
        before = self.gbk.stat().st_mtime_ns
        os.utime(self.gbk, ns=(before - 10 ** 9, before - 10 ** 9))
        stamped = self.gbk.stat().st_mtime_ns
        apply_actions(self.plan([self.gbk]), keep_mtime=True)
        self.assertEqual(self.gbk.stat().st_mtime_ns, stamped)

    def test_verification_failure_leaves_original_intact(self):
        """校验不过时必须原文件毫发无损——这是"先在临时文件上验"的意义。"""
        plan = self.plan([self.gbk])
        original = self.gbk.read_bytes()
        act = plan[0]
        act.chars = 9999            # 人为制造"字符数不符"
        results = apply_actions(plan)
        self.assertFalse(results[0].ok)
        self.assertIn("原文件未改动", results[0].message)
        self.assertEqual(self.gbk.read_bytes(), original)
        leftovers = [p.name for p in self.dir.iterdir() if p.name.endswith(".obt-tmp")]
        self.assertEqual(leftovers, [])

    def test_result_as_dict(self):
        import json
        plan = self.plan([self.gbk])
        results = apply_actions(plan)
        payload = json.loads(json.dumps(results[0].as_dict(), ensure_ascii=False, default=str))
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["verified"])


class TestScan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "keep.cs").write_bytes(CN.encode("gbk"))
        (self.dir / ".git").mkdir()
        (self.dir / ".git" / "config").write_bytes(b"[core]\n")
        (self.dir / "node_modules").mkdir()
        (self.dir / "node_modules" / "x.js").write_bytes(b"var a=1;\n")
        (self.dir / "bin").mkdir()
        (self.dir / "bin" / "out.dll").write_bytes(BINARY)
        (self.dir / "sub").mkdir()
        (self.dir / "sub" / "deep.cs").write_bytes(CN.encode("gbk"))
        # 带后缀的虚拟环境目录：只写 ".venv" 精确名时会被整个扫进来
        (self.dir / ".venv1").mkdir()
        (self.dir / ".venv1" / "frozen.py").write_bytes(b"x = 1\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_recursive_and_default_excludes(self):
        files = [p.name for p in iter_files([self.dir])]
        self.assertIn("keep.cs", files)
        self.assertIn("deep.cs", files)
        self.assertNotIn("x.js", files)      # node_modules
        self.assertNotIn("out.dll", files)   # bin
        self.assertNotIn("config", files)    # .git

    def test_suffixed_venv_is_excluded(self):
        """``.venv1`` / ``.venv-old`` 这类带后缀的环境目录同样要排除。

        曾经只排除精确名 ``.venv``，结果 ``.venv1`` 里几千个 site-packages
        文件全被扫进来，逼得用户在命令行里手写一长串 --exclude。
        """
        files = [p.name for p in iter_files([self.dir])]
        self.assertNotIn("frozen.py", files)

    def test_explicit_path_overrides_excludes(self):
        """忽略规则只管「往下走」，用户显式点名的路径永远生效。"""
        target = self.dir / ".venv1" / "frozen.py"
        files = iter_files([target])
        self.assertEqual(files, [target])

    def test_non_recursive(self):
        files = [p.name for p in iter_files([self.dir], recursive=False)]
        self.assertIn("keep.cs", files)
        self.assertNotIn("deep.cs", files)

    def test_include_filter(self):
        files = [p.name for p in iter_files([self.dir], includes=("*.cs",))]
        self.assertEqual(sorted(files), ["deep.cs", "keep.cs"])

    def test_extra_exclude(self):
        files = [p.name for p in iter_files([self.dir], includes=("*.cs",),
                                           excludes=("*deep.cs",))]
        self.assertEqual(files, ["keep.cs"])

    def test_results_are_sorted_and_deduplicated(self):
        files = iter_files([self.dir, self.dir / "keep.cs"])
        self.assertEqual(len(files), len({str(p) for p in files}))
        self.assertEqual(files, sorted(files, key=lambda x: str(x).lower()))

    def test_missing_path_is_ignored(self):
        self.assertEqual(iter_files([self.dir / "nope"]), [])


if __name__ == "__main__":
    unittest.main()
