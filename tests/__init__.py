"""测试包。在这里把 ``src`` 塞进 sys.path，让测试无需安装即可跑：

    python -m unittest discover -s tests -t . -v

（``-t .`` 让 ``tests`` 被当成包导入，于是本模块的路径注入先于各测试模块执行。）
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

__all__ = ["ROOT", "SRC"]
