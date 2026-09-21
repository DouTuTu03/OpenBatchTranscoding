"""让 ``python -m obt`` 等价于 ``obt`` 命令（无需安装即可自测）。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
