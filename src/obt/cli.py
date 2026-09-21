"""obt 命令行入口。

这一层只做三件事：解析参数、调用内核、渲染结果。
**所有判定逻辑都在 ``obt.core``**——想加图形界面时，直接 import 内核即可，
不必把这里的代码搬来搬去。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .core import (
    BY_NAME, CODECS, EOL_CHOICES, STATUS_CONVERT, STATUS_ERROR,
    UnknownEncoding, apply_actions, build_plan, detect, detect_eol,
    eol_label, find_undecodable, iter_files, line_encodings, resolve,
    resolve_target, DEFAULT_EXCLUDES,
)
from .render import (
    MARK_BAD, MARK_CONVERT, MARK_ERROR, MARK_OK, MARK_SKIP, bar, configure_stdio,
    heading, human_size, make_palette, percent, section, table,
)

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_USAGE = 2

# ---------------------------------------------------------------- 小工具


def _err(msg: str) -> int:
    print(f"obt: {msg}", file=sys.stderr)
    return EXIT_USAGE


def _split_list(value) -> tuple:
    if not value:
        return ()
    out = []
    for chunk in value:
        out.extend(x.strip() for x in chunk.split(",") if x.strip())
    return tuple(out)


def parse_size(text) -> int:
    """``10MB`` / ``512k`` / ``1048576`` -> 字节数。"""
    if text is None:
        return None
    s = str(text).strip().lower().replace("ib", "").replace("b", "")
    mult = 1
    if s.endswith("k"):
        mult, s = 1024, s[:-1]
    elif s.endswith("m"):
        mult, s = 1024 ** 2, s[:-1]
    elif s.endswith("g"):
        mult, s = 1024 ** 3, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        raise ValueError(f"无法解析大小：{text!r}（示例：512k / 10MB / 1048576）") from None


def _read_stdin_paths() -> list:
    """从标准输入读路径清单，每行一个。

    典型用法：``git ls-files | obt check --stdin --expect utf-8 --eol lf``——
    门禁的输入直接来自版本控制，不必手工维护路径列表（加文件忘了同步这种事就不会发生）。
    空行与以 ``#`` 开头的行会被忽略，方便在脚本里夹注释。

    读之前 :func:`render.configure_stdio` 已经把管道 stdin 调成 UTF-8 了，
    所以带中文的路径不会在 Windows 上被按代码页读坏。
    """
    if sys.stdin is None:
        return []
    paths = []
    for line in sys.stdin:
        line = line.strip()
        if line and not line.startswith("#"):
            paths.append(line)
    return paths


def _collect(args):
    """把命令行给的路径（外加 ``--stdin`` 读到的那批）展开成文件列表。"""
    paths = list(args.paths)
    if getattr(args, "stdin", False):
        paths.extend(_read_stdin_paths())
    if not paths:
        return []
    excludes = tuple(DEFAULT_EXCLUDES) + tuple(getattr(args, "exclude", None) or ())
    return iter_files(
        paths,
        recursive=not getattr(args, "no_recursive", False),
        includes=tuple(getattr(args, "include", None) or ()),
        excludes=excludes,
        max_size=parse_size(getattr(args, "max_size", None)),
    )


def _fail_match(det, targets) -> bool:
    """``--fail-on`` 判定。除编码名外支持三个伪值：non-utf8 / mixed / binary。"""
    for t in targets:
        key = t.strip().lower()
        if key == "non-utf8" and det.encoding not in ("utf-8", "ascii"):
            return True
        if key == "mixed" and not det.strict_decodable and not det.is_binary:
            return True
        if key == "binary" and det.is_binary:
            return True
        if key in (det.encoding.lower(), det.label.lower()):
            return True
        if det.has_bom and key == f"{det.encoding}-bom".lower():
            return True
    return False


def _satisfies(det, expected, want_bom) -> list:
    """``check`` 的合规判定。返回问题列表，空列表表示通过。"""
    problems = []
    if not det.strict_decodable:
        # 混合编码 / 非文本：哪怕嗅探出的"最接近编码"恰好等于期望值，
        # 也不能算合规——它根本没法整体解码。
        problems.append("无法严格解码（疑似混合编码或非文本）")
    same = det.encoding == expected.name
    if det.encoding == "ascii" and expected.name not in ("utf-16le", "utf-16be"):
        # 纯 ASCII 是所有 ASCII 兼容编码的共同子集，字节完全一致，不算违规
        same = True
    if expected.name == "gb18030" and det.encoding == "gbk":
        same = True
    if not same:
        problems.append("判定为非文本文件" if det.is_binary
                        else f"实际编码 {det.label}（要求 {expected.name}）")
    if want_bom is True and not det.has_bom:
        problems.append("缺少 BOM（要求有）")
    elif want_bom is False and det.has_bom:
        problems.append("多余 BOM（要求无）")
    return problems


def _emit(args, payload: dict, text: str) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(text)


# ---------------------------------------------------------------- sniff


def cmd_sniff(args) -> int:
    from_codec = resolve(args.from_enc) if args.from_enc else None
    files = _collect(args)
    if not files:
        return _err("没有找到要嗅探的文件")

    fail_on = _split_list(args.fail_on)
    palette = make_palette("never" if args.json else args.color)
    items = []
    rows = []
    details = []
    violations = 0

    for path in files:
        try:
            data = path.read_bytes()
        except OSError as e:
            items.append({"path": str(path), "error": str(e)})
            violations += 1
            continue

        det = detect(data, hint=from_codec)
        item = {"path": str(path), **det.as_dict()}
        runs = []
        if not det.strict_decodable and not det.is_binary:
            runs = find_undecodable(data, BY_NAME[det.encoding],
                                    limit=args.mixed_limit)
        item["undecodable"] = [r.as_dict() for r in runs]
        if args.per_line:
            lr = line_encodings(data, dominant=BY_NAME.get(det.encoding, BY_NAME["utf-8"]))
            item["per_line"] = lr.as_dict()

        hit = _fail_match(det, fail_on)
        if hit:
            violations += 1
        items.append(item)

        conf = f"{det.confidence:.2f}"
        if det.confidence >= 0.85:
            conf = palette(conf, "green")
        elif det.confidence < 0.6:
            conf = palette(conf, "red")

        note = ""
        if det.is_binary:
            note = palette("非文本文件", "yellow")
        elif not det.strict_decodable:
            note = palette(f"疑似混合编码：{det.replacements} 字节无法解释", "red")
        elif det.ambiguous:
            note = palette("存在结构性歧义", "yellow")
        elif hit:
            note = palette("命中 --fail-on", "red")
        if det.forced:
            note = (note + " " if note else "") + palette("(--from 指定)", "dim")

        rows.append([
            str(path), det.label, conf,
            "有" if det.has_bom else "无", eol_label(det.eol),
            human_size(det.size), note,
        ])

        if args.explain or runs or args.per_line or det.note:
            details.append(_sniff_detail(path, det, runs, item, palette, args))

    head = f"嗅探 {len(files)} 个文件"
    if from_codec:
        head += f"（--from 强制为 {from_codec.name}）"
    text = [heading(head, palette), ""]
    text.append(table(["File", "Encoding", "Conf", "BOM", "EOL", "Size", "Note"],
                      rows, aligns=["l", "l", "r", "c", "c", "r", "l"]))
    text.append("")
    text.extend(d for d in details if d)

    payload = {
        "tool": "obt", "version": __version__, "command": "sniff",
        "summary": {
            "files": len(files),
            "violations": violations,
            "fail_on": list(fail_on),
            "forced_from": from_codec.name if from_codec else "",
        },
        "items": items,
    }
    _emit(args, payload, "\n".join(text))
    return EXIT_VIOLATION if violations else EXIT_OK


def _sniff_detail(path, det, runs, item, palette, args) -> str:
    """单个文件的详细依据：判定打分、无法解码的片段、逐行分布。"""
    out = [""]
    if not args.quiet:
        out.append(section(f"依据：{path}", palette))
        for i, ev in enumerate(det.evidence, 1):
            out.append(f"    {palette(str(i) + '.', 'dim')} {ev}")
        if det.note:
            out.append(f"    {palette('注意：', 'yellow')}{det.note}")

    if args.explain:
        rows = []
        for c in det.candidates:
            total = f"{c.total:.3f}"
            rows.append([
                c.codec.name,
                "OK" if c.strict else palette("失败", "dim"),
                f"{c.struct:.2f}", f"{c.quality:.2f}", f"{c.script:.2f}",
                f"{c.prior:+.2f}", f"{c.bonus:+.2f}" if c.bonus else "-",
                palette(total, "bold") if c.strict else palette(total, "dim"),
                (c.error or "")[:26],
            ])
        out.append("")
        out.append(section(
            "候选打分（总分 = 0.35×结构 + 0.35×质量 + 0.30×文字系统 + 先验 + 决胜）",
            palette))
        out.append(table(
            ["编码", "严格解码", "结构", "质量", "文字", "先验", "决胜", "总分", "解码失败原因"],
            rows, aligns=["l", "c", "r", "r", "r", "r", "r", "r", "l"]))

    if runs:
        out.append("")
        out.append(section(f"无法解码的片段（前 {len(runs)} 处）", palette))
        rows = []
        for r in runs:
            ctx = f"…{r.before}【?】{r.after}…"
            rows.append([
                f"0x{r.offset:X}", str(r.length), r.raw.hex(" "),
                ctx[:48], r.reason,
            ])
        out.append(table(["偏移", "字节数", "原始字节", "上下文", "原因"], rows,
                         aligns=["r", "r", "l", "l", "l"]))

    lr = item.get("per_line")
    if lr:
        out.append("")
        if lr.get("skipped"):
            out.append(f"    逐行分析：{palette(lr['skipped'], 'dim')}")
        out.append(section(
            f"逐行编码分布（分析 {lr['analyzed']} / 共 {lr['total_lines']} 行）", palette))
        counts = lr["counts"] or {}
        total = sum(counts.values()) or 1
        rows = [[k, str(v), percent(v, total), bar(v, total)]
                for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
        out.append(table(["行编码", "行数", "占比", "分布"], rows,
                         aligns=["l", "r", "r", "l"]))
        if lr["anomalies"]:
            out.append("")
            out.append(section("异常行（编码与主体不一致）", palette))
            rows = [[str(a["line"]), f"0x{a['offset']:X}", a["encoding"],
                     f"{a['confidence']:.2f}", a["preview"]]
                    for a in lr["anomalies"]]
            out.append(table(["行号", "偏移", "嗅探结果", "置信", "内容预览"], rows,
                             aligns=["r", "r", "l", "r", "l"]))
    return "\n".join(out)


# ---------------------------------------------------------------- scan


def cmd_scan(args) -> int:
    files = _collect(args)
    if not files:
        return _err("没有找到要扫描的文件")
    fail_on = _split_list(args.fail_on)
    palette = make_palette("never" if args.json else args.color)

    items = []
    groups = {}
    flagged = []
    violations = 0
    info = []

    for path in files:
        try:
            data = path.read_bytes()
        except OSError as e:
            items.append({"path": str(path), "error": str(e)})
            continue
        det = detect(data)
        groups.setdefault(det.label, []).append(path)
        entry = {"path": str(path), **det.as_dict()}
        hit = _fail_match(det, fail_on)
        low_conf = det.confidence < args.min_confidence
        if hit or low_conf or not det.strict_decodable:
            entry["flagged"] = {"fail_on": hit, "low_confidence": low_conf,
                                "undecodable": not det.strict_decodable}
            flagged.append(entry)
            if hit:
                violations += 1
        items.append(entry)
        info.append((path, det))

    text = [heading(f"扫描 {len(files)} 个文件", palette), ""]
    text.append(section("编码分布", palette))
    rows = [[name, str(len(ps)), percent(len(ps), len(files)), bar(len(ps), len(files))]
            for name, ps in sorted(groups.items(), key=lambda kv: -len(kv[1]))]
    text.append(table(["编码", "文件数", "占比", "分布"], rows,
                      aligns=["l", "r", "r", "l"]))

    if flagged:
        text.append("")
        text.append(section(f"需要关注（{len(flagged)} 个）", palette))
        rows = []
        for e in flagged:
            why = []
            if e["flagged"]["fail_on"]:
                why.append("命中 --fail-on")
            if e["flagged"]["low_confidence"]:
                why.append("置信度低")
            if e["flagged"]["undecodable"]:
                why.append("无法严格解码")
            rows.append([e["path"], e["label"], f"{e['confidence']:.2f}", "；".join(why)])
        text.append(table(["File", "Encoding", "Conf", "原因"], rows,
                          aligns=["l", "l", "r", "l"]))
    else:
        text.append("")
        text.append("  " + palette("全部文件都在预期内。", "green"))

    payload = {
        "tool": "obt", "version": __version__, "command": "scan",
        "summary": {
            "files": len(files),
            "groups": {k: len(v) for k, v in groups.items()},
            "flagged": len(flagged),
            "violations": violations,
            "fail_on": list(fail_on),
        },
        "items": items,
    }
    _emit(args, payload, "\n".join(text))
    return EXIT_VIOLATION if violations else EXIT_OK


# ---------------------------------------------------------------- convert


def cmd_convert(args) -> int:
    try:
        to_codec, bom_default = resolve_target(args.to)
    except UnknownEncoding as e:
        return _err(str(e))
    write_bom = bom_default if args.bom is None else args.bom
    from_codec = resolve(args.from_enc) if args.from_enc else None

    try:
        min_size = parse_size(args.max_size)
    except ValueError as e:
        return _err(str(e))

    files = _collect(args)
    if not files:
        return _err("没有找到要转码的文件")

    plan = build_plan(
        files, to=to_codec, write_bom=write_bom, eol=args.eol,
        from_codec=from_codec, only_mismatch=args.only_mismatch,
        skip_binary=not args.no_skip_binary, min_confidence=args.min_confidence,
        max_size=min_size,
    )

    palette = make_palette("never" if args.json else args.color)
    to_convert = [a for a in plan if a.status == STATUS_CONVERT]
    errors = [a for a in plan if a.status == STATUS_ERROR]

    out_dir = Path(args.out_dir).expanduser() if args.out_dir else None
    root = Path(args.paths[0]).expanduser() if len(args.paths) == 1 else None
    if root is not None and root.is_file():
        root = None

    mode = "预演" if args.dry_run else "执行"
    head = (f"转码{mode}：{len(files)} 个文件 → {to_codec.name}"
            f"{' + BOM' if write_bom else ''}")
    if args.eol != "keep":
        head += f"，换行统一为 {eol_label(args.eol)}"
    text = [heading(head, palette), ""]

    rows = []
    for a in plan:
        mark = {STATUS_CONVERT: palette(MARK_CONVERT, "green"),
                "skip": palette(MARK_SKIP, "dim"),
                STATUS_ERROR: palette(MARK_ERROR, "red")}[a.status]
        size_txt = (f"{human_size(a.size_before)} → {human_size(a.size_after)}"
                    if a.status == STATUS_CONVERT else human_size(a.size_before))
        eol_txt = ""
        if a.status == STATUS_CONVERT and a.src_eol != a.dst_eol:
            eol_txt = f"{eol_label(a.src_eol)}→{eol_label(a.dst_eol)}"
        rows.append([
            mark, str(a.path), a.src_encoding or "-",
            a.dst_encoding if a.status == STATUS_CONVERT else "-",
            eol_txt or "-", size_txt, a.reason,
        ])
    text.append(table(["", "File", "From", "To", "EOL", "Size", "说明"], rows,
                      aligns=["l", "l", "l", "l", "l", "r", "l"]))
    text.append("")
    text.append(f"  将转码 {len(to_convert)} 个，无需改动 "
                f"{sum(1 for a in plan if a.status == 'skip')} 个，"
                f"拒绝 {len(errors)} 个")

    results = []
    if args.dry_run:
        text.append("  " + palette("未写入任何文件（--dry-run）。去掉 --dry-run 并加 --yes 后执行。",
                                   "dim"))
        exit_code = EXIT_VIOLATION if errors else EXIT_OK
    else:
        if not to_convert:
            text.append("  " + palette("没有文件需要转换。", "dim"))
            exit_code = EXIT_VIOLATION if errors else EXIT_OK
        else:
            if not args.yes:
                # 刻意不做交互式 y/N 确认：Windows 上连 DEVNULL 的 isatty()
                # 都可能返回 True，靠终端状态猜"是否交互"是不可靠的，
                # 猜错的代价是"以为会提示、结果直接写盘"。
                # 所以写盘一律要求显式 --yes，行为在任何环境都一致。
                return _err(
                    f"将修改 {len(to_convert)} 个文件，需要显式确认：先加 --dry-run "
                    f"看一遍计划，确认无误后再加 --yes 执行"
                )
            results = apply_actions(
                plan, dry_run=False, backup=args.backup, out_dir=out_dir, root=root,
                keep_mtime=args.keep_mtime, verify=not args.no_verify,
            )
            ok = sum(1 for r in results if r.ok)
            bad = [r for r in results if not r.ok]
            text.append("")
            text.append(section("结果", palette))
            for r in results:
                mark = palette(MARK_OK, "green") if r.ok else palette(MARK_BAD, "red")
                extra = f"（备份 {r.backup.name}）" if r.backup else ""
                text.append(f"    {mark} {r.target} {r.message}{extra}")
            text.append(f"  成功 {ok} 个，失败 {len(bad)} 个")
            exit_code = EXIT_VIOLATION if (bad or errors) else EXIT_OK

    payload = {
        "tool": "obt", "version": __version__, "command": "convert",
        "dry_run": bool(args.dry_run),
        "summary": {
            "files": len(files), "to_convert": len(to_convert),
            "rejected": len(errors), "target": to_codec.name,
            "write_bom": bool(write_bom), "eol": args.eol,
            "applied": len(results),
            "failed": sum(1 for r in results if not r.ok),
        },
        "plan": [a.as_dict() for a in plan],
        "results": [r.as_dict() for r in results],
    }
    _emit(args, payload, "\n".join(text))
    return exit_code


def _prompt_yes(question: str) -> bool:  # pragma: no cover - 保留给将来的交互模式
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


# ---------------------------------------------------------------- check


def cmd_check(args) -> int:
    try:
        expected, bom_intent = resolve_target(args.expect)
    except UnknownEncoding as e:
        return _err(str(e))
    want_bom = bom_intent if args.bom is None else args.bom
    if not bom_intent and args.bom is None:
        want_bom = None  # 未显式要求时，不把 BOM 差异当违规

    files = _collect(args)
    if not files:
        return _err("没有找到要检查的文件")
    palette = make_palette("never" if args.json else args.color)

    items = []
    bad = []
    for path in files:
        try:
            data = path.read_bytes()
        except OSError as e:
            bad.append({"path": str(path), "problems": [f"读取失败：{e}"]})
            continue
        det = detect(data)
        problems = _satisfies(det, expected, want_bom)
        if args.eol != "keep" and det.eol not in ("none", args.eol):
            problems.append(f"换行 {eol_label(det.eol)}（要求 {eol_label(args.eol)}）")
        entry = {"path": str(path), "encoding": det.encoding, "label": det.label,
                 "confidence": round(det.confidence, 4), "eol": det.eol,
                 "bom": det.bom.hex(" "), "problems": problems}
        items.append(entry)
        if problems:
            bad.append(entry)

    want = expected.name + (" + BOM" if want_bom else (" + 无 BOM" if want_bom is False else ""))
    text = [heading(f"编码门禁：{len(files)} 个文件，要求 {want}", palette), ""]
    if bad:
        text.append(section(f"不合规 {len(bad)} 个", palette))
        rows = [[e["path"], e.get("label", "-"), "；".join(e["problems"])] for e in bad]
        text.append(table(["File", "实际", "问题"], rows, aligns=["l", "l", "l"]))
    else:
        text.append("  " + palette(f"全部 {len(files)} 个文件都符合要求。", "green"))

    payload = {
        "tool": "obt", "version": __version__, "command": "check",
        "summary": {"files": len(files), "violations": len(bad),
                    "expect": expected.name, "bom": want_bom, "eol": args.eol},
        "items": items,
    }
    _emit(args, payload, "\n".join(text))
    return EXIT_VIOLATION if bad else EXIT_OK


# ---------------------------------------------------------------- encodings


def cmd_encodings(args) -> int:
    palette = make_palette("never" if args.json else args.color)
    rows = []
    for c in CODECS:
        rows.append([
            c.name, c.python_codec, c.family,
            "有" if c.write_bom else "-", f"{c.prior:+.2f}", c.note,
        ])
    text = [heading(f"支持 {len(CODECS)} 种编码（全部来自 Python 标准库）", palette), ""]
    text.append(table(["名称", "Python codec", "编码族", "默认BOM", "先验", "说明"],
                      rows, aligns=["l", "l", "l", "c", "r", "l"]))
    text.append("")
    text.append("  别名：gb2312/cp936/ansi → gbk，sjis/cp932 → shift_jis，"
                "latin1/iso-8859-1 → latin-1，utf8sig/utf-8-bom → utf-8 + BOM")
    payload = {"tool": "obt", "version": __version__, "command": "encodings",
               "items": [{"name": c.name, "python_codec": c.python_codec,
                          "family": c.family, "script": c.script,
                          "bom": c.bom.hex(" "), "write_bom": c.write_bom.hex(" "),
                          "prior": c.prior, "aliases": list(c.aliases),
                          "note": c.note} for c in CODECS]}
    _emit(args, payload, "\n".join(text))
    return EXIT_OK


# ---------------------------------------------------------------- Vue TUI


def cmd_tui(args) -> int:
    """启动 Vue TUI；Node 前端会自行拉起 JSONL Python 后端。"""
    if not (getattr(sys.stdin, "isatty", lambda: False)()
            and getattr(sys.stdout, "isatty", lambda: False)()):
        return _err("Vue TUI 必须在交互式终端中运行，不能通过管道或重定向启动")
    node = shutil.which("node")
    if not node:
        return _err("Vue TUI 需要 Node.js 22+；请安装 Node 后重试")
    root = Path(__file__).resolve().parents[2]
    entry = root / "ui" / "dist" / "main.mjs"
    if not entry.is_file():
        return _err("Vue TUI 尚未构建；请先执行：cd ui && npm install && npm run build")
    env = dict(os.environ)
    env["OBT_PYTHON"] = sys.executable
    # 源码运行时保证后端能 import obt；已安装的 wheel 不受此变量影响。
    env["PYTHONPATH"] = str(root / "src")
    try:
        return subprocess.call([node, str(entry)], cwd=str(root / "ui"), env=env)
    except OSError as exc:
        return _err(f"无法启动 Vue TUI：{exc}")


# ---------------------------------------------------------------- 参数解析


def _add_common(p, *, suppress: bool) -> None:
    default = argparse.SUPPRESS if suppress else False
    p.add_argument("--json", action="store_true", default=default,
                   help="以 JSON 输出（内核的可编排契约，图形界面也走这个格式）")
    p.add_argument("--color", choices=("auto", "always", "never"),
                   default=argparse.SUPPRESS if suppress else "auto",
                   help="颜色策略，默认 auto（仅终端上色）")
    p.add_argument("-q", "--quiet", action="store_true",
                   default=argparse.SUPPRESS if suppress else False,
                   help="少说废话：只输出结论表")


def _add_scan_opts(p) -> None:
    p.add_argument("--stdin", action="store_true",
                   help="额外从标准输入读路径清单（每行一个）："
                        "git ls-files | obt check --stdin --expect utf-8")
    p.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    p.add_argument("--include", action="append", metavar="GLOB",
                   help="只处理匹配的文件（可多次，如 --include '*.cs'）")
    p.add_argument("--exclude", action="append", metavar="GLOB",
                   help="额外排除（在默认忽略规则之上叠加）")
    p.add_argument("--max-size", metavar="SIZE",
                   help="跳过大文件，如 10MB / 512k")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common, suppress=True)

    parser = argparse.ArgumentParser(
        prog="obt",
        description="编码嗅探与批量转码。判定可解释，转码有校验，绝不静默损坏文件。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "退出码\n"
            "  0  一切正常\n"
            "  1  有文件不符合预期（命中 --fail-on / 不合规 / 有文件被拒绝转码）\n"
            "  2  用法错误或路径无效\n"
            "\n"
            "例子\n"
            "  obt sniff src/ --explain\n"
            "  obt sniff src/legacy.cs --per-line\n"
            "  obt scan . --include '*.cs' --fail-on non-utf8\n"
            "  obt convert . --to utf-8-bom --eol lf --dry-run\n"
            "  obt convert . --to utf-8-bom --eol lf --yes --backup\n"
            "  obt check . --expect utf-8-bom --eol lf\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"obt {__version__}")
    _add_common(parser, suppress=False)

    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    p = sub.add_parser("sniff", parents=[common], help="嗅探文件/目录的编码并给出依据")
    p.add_argument("paths", nargs="*", metavar="PATH",
                   help="文件或目录；也可以只给 --stdin 从管道读")
    p.add_argument("--from", dest="from_enc", metavar="ENC",
                   help="跳过自动嗅探，强制按该编码解读")
    p.add_argument("--explain", action="store_true", help="打印候选编码的打分明细")
    p.add_argument("--per-line", action="store_true", help="逐行分析编码分布（定位混合编码）")
    p.add_argument("--mixed-limit", type=int, default=20, metavar="N",
                   help="最多列出多少处无法解码的片段（默认 20）")
    p.add_argument("--fail-on", action="append", metavar="ENC",
                   help="命中即退出码 1；支持编码名与 non-utf8 / mixed / binary")
    _add_scan_opts(p)
    p.set_defaults(func=cmd_sniff)

    p = sub.add_parser("scan", parents=[common], help="批量统计编码分布，用于摸底与 CI")
    p.add_argument("paths", nargs="*", metavar="PATH")
    p.add_argument("--fail-on", action="append", metavar="ENC")
    p.add_argument("--min-confidence", type=float, default=0.0, metavar="F",
                   help="低于该置信度的文件列入「需要关注」")
    _add_scan_opts(p)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("convert", parents=[common], help="批量转码（先出计划，再落地）")
    p.add_argument("paths", nargs="*", metavar="PATH")
    p.add_argument("-t", "--to", required=True, metavar="ENC",
                   help="目标编码，如 utf-8 / utf-8-bom / gbk / big5")
    p.add_argument("--from", dest="from_enc", metavar="ENC",
                   help="强制源编码（不做嗅探；混合编码定位后逐段处理时用）")
    p.add_argument("--eol", choices=EOL_CHOICES, default="keep",
                   help="换行处理，默认 keep：只改编码，不动换行")
    p.add_argument("--bom", action="store_true", default=None, help="强制写入 BOM")
    p.add_argument("--no-bom", dest="bom", action="store_false", help="强制不写 BOM")
    p.add_argument("--only-mismatch", action="store_true",
                   help="跳过编码已符合目标的文件")
    p.add_argument("--out-dir", metavar="DIR",
                   help="输出到新目录（镜像目录结构），不原地修改")
    p.add_argument("--backup", action="store_true", help="原地修改前生成 .bak 备份")
    p.add_argument("--keep-mtime", action="store_true", help="保留原修改时间")
    p.add_argument("--no-verify", action="store_true",
                   help="跳过写回自校验（不建议）")
    p.add_argument("--no-skip-binary", action="store_true", help="二进制文件视为错误而不是跳过")
    p.add_argument("--min-confidence", type=float, default=0.6, metavar="F",
                   help="嗅探置信度低于该值时拒绝转码（默认 0.6）")
    p.add_argument("--dry-run", action="store_true", help="只出计划，不写盘")
    p.add_argument("--yes", action="store_true",
                   help="确认写盘。本工具不做交互式确认，写盘必须显式给出此参数")
    _add_scan_opts(p)
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("check", parents=[common], help="CI 门禁：要求全部文件符合指定编码")
    p.add_argument("paths", nargs="*", metavar="PATH")
    p.add_argument("--expect", required=True, metavar="ENC",
                   help="期望编码；写成 utf-8-bom 会连带要求 BOM")
    p.add_argument("--eol", choices=EOL_CHOICES, default="keep",
                   help="期望换行；keep 表示不检查")
    p.add_argument("--bom", action="store_true", default=None)
    p.add_argument("--no-bom", dest="bom", action="store_false")
    _add_scan_opts(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("encodings", parents=[common], help="列出支持的编码与别名")
    p.set_defaults(func=cmd_encodings)

    p = sub.add_parser("tui", help="启动 Vue 终端界面（需先在 ui/ 构建）")
    p.set_defaults(func=cmd_tui)

    return parser


def main(argv=None) -> int:
    # 输出编码策略见 render.configure_stdio：管道给真 UTF-8（JSON 契约不能被
    # 替换成 ?），交互式控制台保留原编码但不再抛 UnicodeEncodeError。
    configure_stdio()

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK
    try:
        return args.func(args)
    except UnknownEncoding as e:
        return _err(str(e))
    except ValueError as e:
        return _err(str(e))
    except KeyboardInterrupt:
        print("obt: 已中断", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return EXIT_OK
