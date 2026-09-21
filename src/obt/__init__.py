"""obt —— OpenBatchTranscoding。

编码嗅探与批量转码的**内核**。CLI 只是它的第一个外壳：

    from obt.core import detect, build_plan, apply_actions

对外稳定入口在 ``obt.core``；``obt.cli`` 只负责参数解析与渲染，
不做任何判定逻辑——这样将来加 GUI / 服务端 / MCP 工具时，
不需要改判定代码。
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
