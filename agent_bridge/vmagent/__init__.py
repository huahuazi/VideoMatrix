"""vmagent — VideoMatrix Agent 接入层。

一套工具定义,三种协议暴露: MCP stdio / HTTP JSON / CLI。
"""
__version__ = "1.0.0"

from .tools import REGISTRY, call_tool, list_tools  # noqa: F401

__all__ = ["REGISTRY", "call_tool", "list_tools", "__version__"]

