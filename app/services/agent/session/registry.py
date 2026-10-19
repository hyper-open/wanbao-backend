"""交互式会话 handler 注册表。

为什么单独一个模块：`graph.py` 需要 `get_handler`，而 `__init__.py` 又要导出 `graph`，
放一起会形成包级循环导入。注册表独立后依赖是单向的（graph → registry）。

新工具接入：
- **配置驱动**（默认 handler）：`tool.interactive=True` + `interaction_config`，零代码；
- **特殊场景**：`register(tool_id, handler)` 注册自定义 handler（对齐 EXECUTOR_REGISTRY 的扩展方式）。
"""
from __future__ import annotations

from app.services.agent.session.base import DEFAULT_HANDLER, SessionHandler

SESSION_REGISTRY: dict[str, SessionHandler] = {}


def register(tool_id: str, handler: SessionHandler) -> None:
    SESSION_REGISTRY[tool_id] = handler


def get_handler(tool) -> SessionHandler:
    if tool is None:
        return DEFAULT_HANDLER
    h = SESSION_REGISTRY.get(tool.id)
    if h:
        return h
    # 通用扩展点：工具配置了 docx_export 执行器 → 产出阶段自动导出 Word
    if getattr(tool, "executor", None) == "docx_export":
        from app.services.agent.session.docx_export import DocxExportHandler

        return DocxExportHandler()
    return DEFAULT_HANDLER
