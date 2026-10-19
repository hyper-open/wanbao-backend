"""交互式多轮会话框架（§6.5「工具图可含人工介入节点」）。

结构：
- `base.py`      通用协议 + 配置驱动的 DefaultHandler（提示词容错、facts 渲染）
- `registry.py`  handler 注册表（按 tool_id 覆盖默认实现）
- `graph.py`     通用交互图：analyze → decide → ask(interrupt) → merge → … → ask_confirm → produce
- `resume.py`    简历优化 handler（产出阶段导出 docx）

导入顺序：base → graph → registry → resume（graph 依赖 registry，注册表独立以免循环导入）。
"""
from app.services.agent.session.base import (  # noqa: F401
    DEFAULT_HANDLER,
    SessionCtx,
    SessionHandler,
    session_defaults,
)
from app.services.agent.session.graph import SessionState, build_graph  # noqa: F401
from app.services.agent.session.registry import (  # noqa: F401
    SESSION_REGISTRY,
    get_handler,
    register,
)

# —— 注册需要自定义产出逻辑的工具（其余工具走配置驱动的默认 handler）——
from app.services.agent.session.pptx import PptxHandler  # noqa: E402
from app.services.agent.session.resume import ResumeHandler  # noqa: E402

register("resume_optimize", ResumeHandler())  # 产出 docx
register("pptx_optimize", PptxHandler())      # 产出 pptx（基于原稿重排）

__all__ = [
    "build_graph",
    "SessionState",
    "SessionCtx",
    "SessionHandler",
    "SESSION_REGISTRY",
    "get_handler",
    "register",
    "session_defaults",
    "DEFAULT_HANDLER",
]
