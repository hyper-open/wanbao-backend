"""复杂工具执行图注册表。

纯配置工具走 simple 图；需代码编排的工具在 EXECUTOR_REGISTRY 注册，
每个值是一个 builder(tool) -> compiled LangGraph。复杂推理收敛在单个工具图内部，
不影响全局调度与计费边界。
"""
from app.services.agent.graphs.docx_resume import build as build_docx_resume
from app.services.agent.graphs.pptx_resume import build as build_pptx_resume
from app.services.agent.graphs.docx_export import build as build_docx_export

EXECUTOR_REGISTRY = {
    "docx_resume": build_docx_resume,
    "pptx_resume": build_pptx_resume,
    "docx_export": build_docx_export,
}

# 执行器中文名（后台工具编辑下拉展示；未注册/为空 = 纯配置工具）
EXECUTOR_LABELS = {
    "docx_resume": "Word 简历导出",
    "pptx_resume": "PPT 简历导出",
    "docx_export": "Word 文档导出（通用）",
}
