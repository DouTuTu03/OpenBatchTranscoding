"""CLI 端到端：命令行为、JSON 契约、退出码。

退出码是本工具给 CI 用的接口，必须稳：
  0 一切正常 / 1 有文件不符合预期 / 2 用法错误
"""

from __future__ import annotations

import codecs
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import SRC
from tests.fixtures import CN, MIXED, write_all


def run_obt(*args, cwd=None, expect=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "obt", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        # stdin 接 DEVNULL：非交互环境就该走"必须显式 --yes"的分支，
        # 也让测试结果不依赖运行者的终端
        stdin=subprocess.DEVNULL,
        cwd=str(cwd) if cwd else None, env=env,
    )
    if expect is not None:
        assert proc.returncode == expect, (
            f"期望退出码 {expect}，实际 {proc.returncode}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


class TestBasics(unittest.TestCase):
    def test_version(self):
        proc = run_obt("--version", expect=0)
        self.assertIn("obt", proc.stdout)

    def test_no_command_prints_help(self):
        proc = run_obt(expect=0)
        self.assertIn("编码嗅探与批量转码", proc.stdout)
        self.assertIn("退出码", proc.stdout)

    def test_encodings_listing(self):
        proc = run_obt("encodings", expect=0)
        for name in ("utf-8", "gbk", "gb18030", "big5", "shift_jis", "euc-kr"):
            self.assertIn(name, proc.stdout)
        self.assertIn("别名", proc.stdout)

    def test_unknown_encoding_is_usage_error(self):
        proc = run_obt("convert", ".", "--to", "utf-9", expect=2)
        self.assertIn("不认识的编码", proc.stderr)

    def test_bad_size_is_usage_error(self):
        proc = run_obt("scan", ".", "--max-size", "十个兆", expect=2)
        self.assertIn("无法解析大小", proc.stderr)


class CliCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.samples = self.dir / "samples"
        write_all(self.samples)

    def tearDown(self):
        self._tmp.cleanup()


class TestSniff(CliCase):
    def test_human_output_has_table(self):
        proc = run_obt("sniff", "samples", cwd=self.dir, expect=0)
        self.assertIn("Encoding", proc.stdout)
        self.assertIn("gbk", proc.stdout)
        self.assertIn("混合编码", proc.stdout)

    def test_json_contract(self):
        proc = run_obt("sniff", "samples", "--json", cwd=self.dir, expect=0)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["tool"], "obt")
        self.assertEqual(payload["command"], "sniff")
        self.assertEqual(payload["summary"]["files"], 13)
        names = {Path(i["path"]).name: i for i in payload["items"]}
        self.assertEqual(names["gbk_legacy.cs"]["encoding"], "gbk")
        self.assertFalse(names["mixed_encoding.log"]["strict_decodable"])
        # 每个 item 都要带证据，否则 JSON 消费者看不到判定依据
        self.assertTrue(names["gbk_legacy.cs"]["evidence"])

    def test_explain_shows_candidate_table(self):
        proc = run_obt("sniff", "samples/gbk_legacy.cs", "--explain",
                       cwd=self.dir, expect=0)
        self.assertIn("候选打分", proc.stdout)
        self.assertIn("相邻" if "相邻" in proc.stdout else "结构", proc.stdout)
        self.assertIn("euc-kr", proc.stdout)

    def test_per_line_locates_the_gbk_line(self):
        proc = run_obt("sniff", "samples/mixed_encoding.log", "--per-line",
                       cwd=self.dir, expect=0)
        self.assertIn("逐行编码分布", proc.stdout)
        self.assertIn("第二行", proc.stdout)
        self.assertIn("无法解码的片段", proc.stdout)

    def test_from_override(self):
        proc = run_obt("sniff", "samples/gbk_legacy.cs", "--from", "gbk",
                       cwd=self.dir, expect=0)
        self.assertIn("--from 强制", proc.stdout)

    def test_fail_on_non_utf8_exits_one(self):
        proc = run_obt("sniff", "samples", "--fail-on", "non-utf8",
                       cwd=self.dir, expect=1)
        self.assertIn("gbk", proc.stdout)

    def test_fail_on_specific_encoding(self):
        run_obt("sniff", "samples/gbk_legacy.cs", "--fail-on", "gbk",
                cwd=self.dir, expect=1)
        run_obt("sniff", "samples/gbk_legacy.cs", "--fail-on", "big5",
                cwd=self.dir, expect=0)

    def test_color_never_outputs_no_ansi(self):
        proc = run_obt("sniff", "samples", "--color", "never", cwd=self.dir, expect=0)
        self.assertNotIn("\x1b[", proc.stdout)

    def test_missing_path_is_usage_error(self):
        proc = run_obt("sniff", "nope-nothing-here", cwd=self.dir, expect=2)
        self.assertIn("没有找到", proc.stderr)


class TestScan(CliCase):
    def test_distribution_and_flags(self):
        proc = run_obt("scan", "samples", cwd=self.dir, expect=0)
        self.assertIn("编码分布", proc.stdout)
        self.assertIn("需要关注", proc.stdout)

    def test_json_summary(self):
        payload = json.loads(run_obt("scan", "samples", "--json",
                                     cwd=self.dir, expect=0).stdout)
        self.assertEqual(payload["summary"]["files"], 13)
        self.assertEqual(payload["summary"]["groups"]["gbk"], 1)

    def test_fail_on_gbk_for_ci(self):
        run_obt("scan", "samples", "--fail-on", "gbk", cwd=self.dir, expect=1)

    def test_clean_tree_exits_zero(self):
        clean = self.dir / "clean"
        clean.mkdir()
        (clean / "a.cs").write_bytes(codecs.BOM_UTF8 + "试验\n".encode("utf-8"))
        run_obt("scan", "clean", "--fail-on", "non-utf8", cwd=self.dir, expect=0)

    def test_include_filter(self):
        payload = json.loads(run_obt("scan", "samples", "--include", "*.cs",
                                     "--json", cwd=self.dir, expect=0).stdout)
        self.assertEqual(payload["summary"]["files"], 2)   # gbk_legacy.cs, utf8_bom_crlf.cs


class TestConvert(CliCase):
    def test_dry_run_writes_nothing(self):
        before = sorted(p.name for p in self.samples.iterdir())
        proc = run_obt("convert", "samples", "--to", "utf-8-bom", "--eol", "lf",
                       "--dry-run", cwd=self.dir, expect=1)
        self.assertIn("未写入任何文件", proc.stdout)
        self.assertIn("拒绝 1 个", proc.stdout)
        self.assertEqual(before, sorted(p.name for p in self.samples.iterdir()))

    def test_requires_explicit_yes(self):
        """写盘必须显式 --yes。

        刻意不做交互式 y/N 确认：Windows 上连 DEVNULL 的 isatty() 都可能
        返回 True，"靠终端状态猜是否交互"猜错的代价是直接写盘。
        """
        proc = run_obt("convert", "samples", "--to", "utf-8-bom",
                       cwd=self.dir, expect=2)
        self.assertIn("--yes", proc.stderr)
        self.assertIn("--dry-run", proc.stderr)
        # 确认没写任何东西
        self.assertEqual((self.samples / "gbk_legacy.cs").read_bytes(),
                         CN.encode("gbk"))

    def test_full_run_and_check_passes(self):
        out = self.dir / "out"
        proc = run_obt("convert", "samples", "--to", "utf-8-bom", "--eol", "lf",
                       "--out-dir", str(out), "--yes", cwd=self.dir, expect=1)
        self.assertIn("成功 11 个，失败 0 个", proc.stdout)
        self.assertFalse(list(out.rglob("*.obt-tmp")))
        # 转出来的目录必须通过门禁
        run_obt("check", str(out), "--expect", "utf-8-bom", "--eol", "lf",
                cwd=self.dir, expect=0)

    def test_clean_batch_exits_zero(self):
        src = self.dir / "only-gbk"
        src.mkdir()
        (src / "a.cs").write_bytes(CN.encode("gbk"))
        proc = run_obt("convert", str(src), "--to", "utf-8-bom", "--yes",
                       cwd=self.dir, expect=0)
        self.assertIn("成功 1 个，失败 0 个", proc.stdout)
        self.assertEqual((src / "a.cs").read_bytes()[:3], b"\xef\xbb\xbf")

    def test_json_plan_shape(self):
        payload = json.loads(run_obt(
            "convert", "samples", "--to", "utf-8-bom", "--dry-run", "--json",
            cwd=self.dir, expect=1).stdout)
        self.assertEqual(payload["command"], "convert")
        self.assertTrue(payload["dry_run"])
        statuses = {Path(a["path"]).name: a["status"] for a in payload["plan"]}
        self.assertEqual(statuses["gbk_legacy.cs"], "convert")
        self.assertEqual(statuses["mixed_encoding.log"], "error")
        self.assertEqual(statuses["binary.bin"], "skip")

    def test_in_place_with_backup(self):
        src = self.dir / "only-gbk"
        src.mkdir()
        target = src / "a.cs"
        target.write_bytes(CN.encode("gbk"))
        run_obt("convert", str(src), "--to", "utf-8-bom", "--backup", "--yes",
                cwd=self.dir, expect=0)
        self.assertTrue(Path(str(target) + ".bak").exists())
        self.assertEqual(Path(str(target) + ".bak").read_bytes(), CN.encode("gbk"))


class TestCheck(CliCase):
    def test_violations_exit_one(self):
        proc = run_obt("check", "samples", "--expect", "utf-8-bom",
                       cwd=self.dir, expect=1)
        self.assertIn("不合规", proc.stdout)
        self.assertIn("gbk_legacy.cs", proc.stdout)

    def test_expected_encoding_is_reported_as_problem(self):
        proc = run_obt("check", "samples/gbk_legacy.cs", "--expect", "utf-8-bom",
                       cwd=self.dir, expect=1)
        self.assertIn("gbk", proc.stdout)
        self.assertIn("缺少 BOM", proc.stdout)

    def test_ascii_is_not_a_violation_for_utf8(self):
        # ASCII 是 UTF-8 的子集，字节完全一致，不该报违规
        run_obt("check", "samples/ascii_only.txt", "--expect", "utf-8",
                cwd=self.dir, expect=0)

    def test_mixed_file_is_a_violation_even_if_closest_matches(self):
        # mixed_encoding.log 的"最接近编码"是 utf-8，但它根本解不全
        proc = run_obt("check", "samples/mixed_encoding.log", "--expect", "utf-8",
                       cwd=self.dir, expect=1)
        self.assertIn("无法严格解码", proc.stdout)

    def test_eol_check(self):
        src = self.dir / "eol"
        src.mkdir()
        (src / "crlf.txt").write_bytes(b"a\r\nb\r\n")
        run_obt("check", str(src), "--expect", "ascii", "--eol", "lf",
                cwd=self.dir, expect=1)
        run_obt("check", str(src), "--expect", "ascii", "--eol", "keep",
                cwd=self.dir, expect=0)

    def test_json(self):
        payload = json.loads(run_obt("check", "samples", "--expect", "utf-8-bom",
                                     "--json", cwd=self.dir, expect=1).stdout)
        self.assertEqual(payload["summary"]["expect"], "utf-8")
        self.assertGreater(payload["summary"]["violations"], 0)


class TestInvariants(unittest.TestCase):
    """跨命令的一致性约束。"""

    def test_json_and_human_report_same_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "s"
            write_all(root)
            human = run_obt("sniff", "s", cwd=tmp, expect=0).stdout
            payload = json.loads(run_obt("sniff", "s", "--json", cwd=tmp,
                                         expect=0).stdout)
            for item in payload["items"]:
                name = Path(item["path"]).name
                self.assertIn(name, human)
                self.assertIn(item["encoding"], human)


if __name__ == "__main__":
    unittest.main()
