"""目录遍历：把"一堆路径"变成"一串待处理文件"。

默认忽略规则刻意做得保守——排除版本控制目录、构建产物、依赖目录，
因为这些地方的编码问题不该由本工具负责，而且量很大。

规则写成**目录/文件名**而不是 ``*/bin/*`` 这种路径通配，是踩过坑的：
当目标目录本身就是根时，相对路径里没有前导路径段，``*/bin/*`` 匹配不上，
于是 ``bin``/``node_modules`` 会被整个扫进来。带 ``/`` 的模式才按路径匹配。
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

# 默认忽略的目录/文件名。
# 名字模式走 fnmatch，所以支持 ``*``。
#
# ``.venv*`` 里的星号不是随手加的：虚拟环境的目录名带后缀是常态
# （``.venv1``、``.venv-old``、``.venv312``），只写 ``.venv`` 的话这些目录
# 会连带 site-packages 被整个扫进来——几千个第三方文件混进结果里，
# 真正的待处理文件反而被淹没。
DEFAULT_EXCLUDES = (
    ".git", ".hg", ".svn",
    "node_modules", "bower_components",
    "bin", "obj", "dist", "build", "target",
    "__pycache__", ".venv*", "venv", ".tox", ".nox", ".eggs", ".direnv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".ipynb_checkpoints",
    ".vs", ".idea", ".vscode", ".next", ".nuxt",
)


def _posix(path: Path, base: Path) -> str:
    """转成相对 base 的 posix 风格路径，用于 glob 匹配。"""
    try:
        rel = path.relative_to(base)
    except ValueError:
        rel = path
    return rel.as_posix()


def _match(rel: str, name: str, patterns) -> bool:
    """按模式匹配。

    * 模式里**不含** ``/``  → 按文件/目录名匹配（``*.cs``、``node_modules``）
    * 模式里**含** ``/``    → 按相对路径匹配（``src/**/*.cs``），
      同时允许省略开头的目录层级（``sub/*.cs`` 也能命中 ``a/sub/x.cs``）
    """
    for pattern in patterns or ():
        pat = str(pattern).replace("\\", "/").strip()
        if not pat:
            continue
        if pat.startswith("./"):
            pat = pat[2:]
        pat = pat.lstrip("/")
        if "/" in pat:
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, "*/" + pat):
                return True
        elif fnmatch.fnmatch(name, pat):
            return True
    return False


def iter_files(paths, *, recursive: bool = True, includes=(), excludes=DEFAULT_EXCLUDES,
               max_size: int = None, follow_symlinks: bool = False) -> list:
    """展开路径列表，返回去重排序后的文件列表。

    * 目录默认递归；``recursive=False`` 时只取该目录的直接子文件
    * ``includes`` 为空表示"全都要"，否则是白名单
    * 文件大小超过 ``max_size`` 的直接丢弃（由 ``--max-size`` 控制）
    * 结果**排序**且去重，保证同一份输入永远产出同一份计划（可复现）

    显式指向的路径总是生效：即使目标是 ``node_modules``，只要它是命令行
    参数本身，就会被处理——忽略规则只作用于"往下走"的过程。
    """
    out = []
    seen = set()
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.exists():
            continue
        if p.is_file():
            _add(out, seen, p, max_size=max_size)
            continue

        base = p
        if recursive:
            walker = os.walk(p, followlinks=follow_symlinks)
        else:
            try:
                entries = [e.name for e in os.scandir(p) if e.is_file()]
            except OSError:
                continue
            walker = [(str(p), [], entries)]

        for root, dirs, files in walker:
            root_path = Path(root)
            if recursive and excludes:
                # 排序保证遍历顺序可复现（os.walk 的顺序依赖文件系统）
                dirs[:] = sorted(d for d in dirs if not _match("", d, excludes))
            for name in sorted(files):
                f = root_path / name
                rel = _posix(f, base)
                if excludes and _match(rel, name, excludes):
                    continue
                if includes and not _match(rel, name, includes):
                    continue
                _add(out, seen, f, max_size=max_size)

    out.sort(key=lambda x: str(x).lower())
    return out


def _add(out, seen, path: Path, *, max_size: int = None) -> None:
    key = str(path.resolve()).lower()
    if key in seen:
        return
    seen.add(key)
    if max_size is not None:
        try:
            if path.stat().st_size > max_size:
                return
        except OSError:
            return
    out.append(path)
