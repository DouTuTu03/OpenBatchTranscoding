"""Vue TUI JSONL 后端：协议层只编排 core，不复制编码判定。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from obt.tui_backend import handle_request


class TestTuiBackend(unittest.TestCase):
    def test_scan_returns_detection_items(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.txt").write_bytes("中文\n".encode("utf-8"))
            result = handle_request({"action": "scan", "params": {"paths": str(root)}})
        self.assertEqual(result["summary"]["files"], 1)
        self.assertEqual(result["items"][0]["encoding"], "utf-8")

    def test_convert_requires_confirmation_before_write(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.txt"
            original = "中文\n".encode("gbk")
            path.write_bytes(original)
            request = {"action": "convert", "params": {
                "paths": str(path), "to": "utf-8", "from": "gbk",
            }}
            planned = handle_request(request)
            self.assertTrue(planned["needs_confirmation"])
            self.assertEqual(path.read_bytes(), original)

            request["params"]["confirm"] = True
            applied = handle_request(request)
            self.assertFalse(applied["needs_confirmation"])
            self.assertEqual(path.read_text(encoding="utf-8"), "中文\n")
