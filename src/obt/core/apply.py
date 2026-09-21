"""落地执行：原子写、替换前自校验、可选备份。

三条硬约束
----------
1. **先在临时文件上校验，通过了才替换**。顺序是：写 ``*.obt-tmp`` → fsync →
   读回来验字节、验能不能解回、验字符数 → 通过才 ``os.replace``。
   于是"校验失败"时原文件毫发无损，不需要事后回滚（回滚本身也可能失败）。
   顺带的好处是不必把原文缓存在内存里。
2. **原子替换**：``os.replace`` 是原子的。中途断电或崩溃，要么是原文件，
   要么是完整的新文件，不会出现"写了一半的源码文件"。
3. **备份只在真的要改时产生**，且默认关闭（源码仓库本来就该用版本控制兜底），
   由用户显式 ``--backup`` 打开。

写了但没验，等于没写；验不过还改，等于毁尸灭迹。这两条都不允许。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .decode import strip_bom_for
from .detect import detect
from .encodings import BY_NAME
from .plan import STATUS_CONVERT, Action

TMP_SUFFIX = ".obt-tmp"


@dataclass
class Result:
    """一次落地尝试的结果。"""

    action: Action
    ok: bool
    message: str = ""
    target: Path = None
    backup: Path = None
    verified: bool = False
    wrote_bytes: int = 0
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {
            "path": str(self.action.path),
            "target": str(self.target) if self.target else "",
            "ok": self.ok,
            "verified": self.verified,
            "message": self.message,
            "wrote_bytes": self.wrote_bytes,
            "backup": str(self.backup) if self.backup else "",
        }
        if "detection_after" in self.detail:
            d["detection_after"] = self.detail["detection_after"]
        return d


def target_path(action: Action, *, out_dir: Path = None, root: Path = None) -> Path:
    """计算落地路径。``out_dir`` 为空即原地改；否则镜像 ``root`` 下的相对结构。"""
    if out_dir is None:
        return action.path
    out_dir = Path(out_dir)
    if root is not None:
        try:
            rel = action.path.resolve().relative_to(Path(root).resolve())
            return out_dir / rel
        except ValueError:
            pass
    return out_dir / action.path.name


def apply_actions(actions, *, dry_run: bool = False, backup: bool = False,
                  out_dir: Path = None, root: Path = None,
                  keep_mtime: bool = False, verify: bool = True) -> list:
    """执行计划中 ``status=convert`` 的条目，返回逐条结果。

    ``dry_run=True`` 时只做计算与校验路径推演，不碰磁盘。
    """
    results = []
    for act in actions:
        if act.status != STATUS_CONVERT:
            continue
        dest = target_path(act, out_dir=out_dir, root=root)

        if dry_run:
            results.append(Result(
                action=act, ok=True, message="预演：未写入", target=dest,
                wrote_bytes=len(act.payload),
            ))
            continue

        results.append(_write_one(act, dest, backup=backup, keep_mtime=keep_mtime,
                                  verify=verify, in_place=(out_dir is None)))
    return results


def _write_one(act: Action, dest: Path, *, backup: bool, keep_mtime: bool,
               verify: bool, in_place: bool) -> Result:
    """写一个文件。顺序刻意如此：**先在临时文件上校验，通过了才替换**。

    这样"校验失败"时原文件毫发无损，不需要事后回滚——回滚本身也可能失败，
    不如从一开始就不破坏。同时也省掉了把原文缓存在内存里的开销。
    """
    tmp = dest.with_name(dest.name + TMP_SUFFIX)
    bak = None

    # 时间戳必须在写之前取，写之后就没了
    stat_before = None
    if in_place and keep_mtime and dest.exists():
        try:
            stat_before = dest.stat()
        except OSError:
            stat_before = None

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)

        with open(tmp, "wb") as fh:
            fh.write(act.payload)
            fh.flush()
            os.fsync(fh.fileno())

        if in_place and dest.exists():
            # 保留原文件权限；Windows 上 chmod 能力有限，失败不影响流程
            try:
                shutil.copymode(dest, tmp)
            except OSError:
                pass
    except OSError as e:
        _cleanup(tmp)
        return Result(action=act, ok=False, message=f"写入临时文件失败：{e}", target=dest)

    if verify:
        problem = _verify(act, tmp)
        if problem:
            _cleanup(tmp)
            return Result(action=act, ok=False, target=dest,
                          message=f"写回自校验失败，原文件未改动：{problem}")

    det_after = None
    try:
        det_after = detect(tmp.read_bytes()).encoding
    except OSError:
        pass

    try:
        if backup and in_place and dest.exists():
            bak = dest.with_name(dest.name + ".bak")
            shutil.copy2(dest, bak)
        os.replace(tmp, dest)
    except OSError as e:
        _cleanup(tmp)
        return Result(action=act, ok=False, message=f"替换目标文件失败：{e}",
                      target=dest, backup=bak)

    if stat_before is not None:
        try:
            os.utime(dest, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
        except OSError:
            pass

    return Result(
        action=act, ok=True, message="已写入并校验通过" if verify else "已写入（未校验）",
        target=dest, backup=bak, verified=verify, wrote_bytes=len(act.payload),
        detail={"detection_after": det_after} if det_after else {},
    )


def _verify(act: Action, dest: Path) -> str:
    """写回后自校验：字节一致 + 目标编码能解回来 + 字符数与计划相同。

    解码前要先剥 BOM：否则 U+FEFF 会被当成一个正文字符，让字符数虚增 1。
    """
    try:
        back = dest.read_bytes()
    except OSError as e:
        return f"无法重新读取 {dest}：{e}"
    if back != act.payload:
        return "磁盘字节与计划不一致"
    try:
        codec = BY_NAME[act.dst_encoding]
    except KeyError as e:
        return f"未知目标编码 {act.dst_encoding}：{e}"

    payload, _bom = strip_bom_for(back, codec)
    try:
        text = payload.decode(codec.python_codec)
    except UnicodeDecodeError as e:
        return f"目标编码无法解回：{e}"
    if len(text) != act.chars:
        return f"字符数变化：{act.chars} → {len(text)}"
    return ""


def _cleanup(tmp: Path) -> None:
    try:
        if tmp.exists():
            tmp.unlink()
    except OSError:
        pass
