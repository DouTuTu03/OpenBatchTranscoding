"""给 Vue TUI 使用的本地 JSONL 后端。

这个模块是一个很小的进程边界：终端 UI 独占 stdout，Python 只在其
stdin/stdout 上交换一行一条的 JSON 消息。所有编码判定与写盘仍复用
``obt.core``，而不是调用 CLI 后再解析展示文本。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from .core import (
    CODECS,
    DEFAULT_EXCLUDES,
    STATUS_CONVERT,
    apply_actions,
    build_plan,
    detect,
    iter_files,
    resolve,
    resolve_target,
)


def _paths(value: Any) -> list[Path]:
    """将一个路径或路径列表正规化为 ``Path`` 列表。"""
    if isinstance(value, str):
        return [Path(value)]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [Path(item) for item in value]
    raise ValueError("paths 必须是路径字符串或路径字符串数组")


def _files(params: dict[str, Any]) -> list[Path]:
    paths = _paths(params.get("paths", "."))
    includes = tuple(params.get("include", ()))
    excludes = tuple(DEFAULT_EXCLUDES) + tuple(params.get("exclude", ()))
    return iter_files(
        paths,
        recursive=not bool(params.get("no_recursive", False)),
        includes=includes,
        excludes=excludes,
        max_size=params.get("max_size"),
    )


def _scan(params: dict[str, Any]) -> dict[str, Any]:
    files = _files(params)
    items = []
    groups: Counter[str] = Counter()
    for path in files:
        try:
            det = detect(path.read_bytes())
        except OSError as exc:
            items.append({"path": str(path), "error": str(exc)})
            continue
        item = {"path": str(path), **det.as_dict()}
        items.append(item)
        groups[det.label] += 1
    return {"summary": {"files": len(files), "groups": dict(groups)}, "items": items}


def _sniff(params: dict[str, Any]) -> dict[str, Any]:
    files = _files(params)
    items = []
    for path in files:
        try:
            det = detect(path.read_bytes(), hint=resolve(params["from"]) if params.get("from") else None)
        except OSError as exc:
            items.append({"path": str(path), "error": str(exc)})
            continue
        items.append({"path": str(path), **det.as_dict(with_candidates=bool(params.get("explain")))})
    return {"summary": {"files": len(files)}, "items": items}


def _convert(params: dict[str, Any]) -> dict[str, Any]:
    target, default_bom = resolve_target(str(params.get("to", "utf-8")))
    write_bom = default_bom if params.get("bom") is None else bool(params["bom"])
    files = _files(params)
    plan = build_plan(
        files,
        to=target,
        write_bom=write_bom,
        eol=str(params.get("eol", "keep")),
        from_codec=resolve(params["from"]) if params.get("from") else None,
        only_mismatch=bool(params.get("only_mismatch", False)),
        skip_binary=not bool(params.get("no_skip_binary", False)),
        min_confidence=float(params.get("min_confidence", 0.6)),
    )
    converted = [action for action in plan if action.status == STATUS_CONVERT]
    result: dict[str, Any] = {
        "summary": {
            "files": len(files), "to_convert": len(converted),
            "rejected": sum(1 for action in plan if action.status == "error"),
            "target": target.name, "write_bom": write_bom,
        },
        "plan": [action.as_dict() for action in plan],
    }
    # 前端必须明确发送 confirm=true；仅生成计划绝不会触碰磁盘。
    if not params.get("confirm"):
        result["needs_confirmation"] = bool(converted)
        return result

    roots = _paths(params.get("paths", "."))
    out_dir = Path(params["out_dir"]) if params.get("out_dir") else None
    root = roots[0] if len(roots) == 1 and roots[0].is_dir() else None
    writes = apply_actions(
        plan,
        backup=bool(params.get("backup", False)),
        out_dir=out_dir,
        root=root,
        keep_mtime=bool(params.get("keep_mtime", False)),
        verify=not bool(params.get("no_verify", False)),
    )
    result["needs_confirmation"] = False
    result["results"] = [write.as_dict() for write in writes]
    return result


def _check(params: dict[str, Any]) -> dict[str, Any]:
    expected, expect_bom = resolve_target(str(params.get("expect", "utf-8")))
    want_bom = expect_bom if params.get("bom") is None else bool(params["bom"])
    items = []
    for path in _files(params):
        try:
            det = detect(path.read_bytes())
        except OSError as exc:
            items.append({"path": str(path), "problems": [f"读取失败：{exc}"]})
            continue
        problems = []
        same = det.encoding == expected.name or (
            det.encoding == "ascii" and expected.name not in ("utf-16le", "utf-16be")
        )
        if expected.name == "gb18030" and det.encoding == "gbk":
            same = True
        if not det.strict_decodable:
            problems.append("无法严格解码（疑似混合编码或非文本）")
        if not same:
            problems.append(f"实际编码 {det.label}（要求 {expected.name}）")
        if want_bom is True and not det.has_bom:
            problems.append("缺少 BOM（要求有）")
        elif want_bom is False and det.has_bom:
            problems.append("多余 BOM（要求无）")
        eol = str(params.get("eol", "keep"))
        if eol != "keep" and det.eol not in ("none", eol):
            problems.append(f"实际换行 {det.eol}（要求 {eol}）")
        items.append({"path": str(path), "encoding": det.encoding, "label": det.label,
                      "confidence": det.confidence, "eol": det.eol,
                      "bom": det.bom.hex(" "), "problems": problems})
    bad = sum(1 for item in items if item.get("problems"))
    return {"summary": {"files": len(items), "violations": bad}, "items": items}


def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """处理一条协议请求；此函数也让协议能脱离子进程做单元测试。"""
    action = request.get("action")
    params = request.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("params 必须是对象")
    if action == "encodings":
        return {"items": [{"name": codec.name, "family": codec.family,
                            "aliases": list(codec.aliases), "note": codec.note}
                           for codec in CODECS]}
    if action == "sniff":
        return _sniff(params)
    if action == "scan":
        return _scan(params)
    if action == "convert":
        return _convert(params)
    if action == "check":
        return _check(params)
    raise ValueError(f"未知 action：{action!r}")


def main() -> int:
    """运行 JSON Lines 服务。stdout 是协议专用通道，禁止打印人类日志。"""
    for line in sys.stdin:
        request: dict[str, Any] | None = None
        try:
            # Windows PowerShell 5.1 管道常在第一段 UTF-8 文本前附 U+FEFF；
            # Node 的 child-process 管道没有它，但协议也应方便人工调试。
            request = json.loads(line.lstrip("\ufeff"))
            if not isinstance(request, dict):
                raise ValueError("请求必须是 JSON 对象")
            response = {"id": request.get("id"), "ok": True,
                        "result": handle_request(request)}
        except (ValueError, OSError, UnicodeError) as exc:
            response = {"id": request.get("id") if request else None,
                        "ok": False, "error": str(exc)}
        print(json.dumps(response, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    raise SystemExit(main())
