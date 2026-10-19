"""交互式多轮会话：通用协议 + 配置驱动的默认实现。

工具接入方式（配置驱动，新工具零代码）：
1. `tool.interactive = True`
2. `tool.interaction_config = {"max_rounds":5,"allow_skip":true,"tool_calls":[...],"max_tool_rounds":2}`
3. 可选在 `Tool.analysis_prompt` 覆盖分析提示词

特殊场景可在 `SESSION_REGISTRY` 注册自定义 handler（对齐 `EXECUTOR_REGISTRY` 的扩展方式）。

⚠️ **状态必须是可序列化的纯数据**：图带 checkpointer，state 会落库，
因此禁止把 SQLAlchemy Session / ORM 对象放进 state（节点各自 `SessionLocal()`）。
"""
from __future__ import annotations

import json
from typing import Any, Optional, Protocol

from app.core.config import settings
from app.core.llm.gateway import LLMGateway
from app.core.timeutil import to_local, utcnow

# ---------------- 分析结果结构（前端分析卡 / decide 共用） ----------------
# {"summary": str,
#  "missing": [{"key","label","detail","priority":"high|medium|low"}],
#  "suspicious": [...]}
ANALYSIS_SCHEMA_DOC = (
    '{"summary":"整体评价 2-3 句",'
    '"missing":[{"key":"字段标识","label":"中文名","detail":"为什么需要","priority":"high|medium|low"}],'
    '"suspicious":[{"key":"...","label":"...","detail":"...","priority":"..."}]}'
)

DEFAULT_ANALYSIS_PROMPT = (
    "你是资深文档顾问。请分析用户提供的材料，**只输出 JSON**，不要任何解释或 Markdown 代码块：\n"
    + ANALYSIS_SCHEMA_DOC
    + "\n要求：missing 列出关键信息缺失项；suspicious 列出可疑/矛盾/空泛之处；"
    "两项加起来不超过 8 条，按 priority 从高到低；没有问题时给空数组。"
)

DEFAULT_DECIDE_PROMPT = (
    "你是多轮对话助手。已知材料原文、分析结果、以及用户已补充的信息。\n"
    "判断信息是否已足够生成高质量结果：\n"
    '- 若仍需补充关键信息，输出 {"more":true,"question":{"field":"字段标识","text":"一句话具体问题","why":"为什么问"}}；'
    "一次只问一个问题，且不要重复问已经回答过的内容。\n"
    '- 否则输出 {"more":false}。\n'
    "只输出 JSON，不要解释。"
)

DEFAULT_PRODUCE_PROMPT = "请基于以下材料与用户补充确认的信息，产出最终结果：\n{input}"


def today_note() -> str:
    """给 LLM 的「当前日期」提示。

    模型训练数据有时间截止，若不告知今天几号，会把已过去的日期（如 2026.09）
    误判为「未来时间」。所有会话提示词都统一带上这一行。
    """
    d = to_local(utcnow())
    week = "一二三四五六日"[d.weekday()]
    return (
        f"【当前日期】{d.strftime('%Y-%m-%d')}（星期{week}）。"
        "判断某个时间是否已过去或尚未来临时，一律以此为准，不要依赖你的训练数据。\n"
    )

# 产出后的「优化前/后」对比（可配置关闭；见 session_defaults 的 diff）
DIFF_SCHEMA_DOC = (
    '{"summary":"整体改动概述 1-2 句",'
    '"changes":[{"section":"所在章节/位置","before":"原文片段","after":"优化后片段","reason":"改动原因"}]}'
)

DEFAULT_DIFF_PROMPT = (
    "你是文档审校助手。已知【原始材料】与【优化后的结果】，请对比并列出关键改动。"
    "**只输出 JSON**，不要任何解释或 Markdown 代码块：\n"
    + DIFF_SCHEMA_DOC
    + "\n要求：逐条列出实质性改动（措辞提升 / 补充信息 / 结构调整 / 量化等），"
    "最多 10 条，按重要性从高到低；几乎无改动则 changes 给空数组。"
    "before / after 各控制在 200 字内，before 为原文对应片段。"
)


# ---------------- 容错解析（mock 模式必须能跑通） ----------------
def _loads_maybe(text: str) -> Optional[dict]:
    """尽力从模型输出里解析 JSON（容忍 ```json 包裹 / 前后废话）。"""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except Exception:  # noqa: BLE001
        i, j = s.find("{"), s.rfind("}")
        if 0 <= i < j:
            try:
                v = json.loads(s[i : j + 1])
                return v if isinstance(v, dict) else None
            except Exception:  # noqa: BLE001
                return None
    return None


def normalize_analysis(raw_text: str, fallback_input: str = "") -> dict:
    """把 LLM 输出规整成分析结构；失败时兜底（否则 mock 模式全链路跑不通）。"""
    d = _loads_maybe(raw_text)
    if not d:
        head = (fallback_input or raw_text or "").strip().replace("\n", " ")[:120]
        return {"summary": head or "已完成材料分析（模型未返回结构化结果）", "missing": [], "suspicious": []}

    def _items(key: str) -> list:
        out = []
        for it in (d.get(key) or [])[:8]:
            if not isinstance(it, dict):
                continue
            out.append(
                {
                    "key": str(it.get("key") or it.get("label") or "item"),
                    "label": str(it.get("label") or it.get("key") or "待确认项"),
                    "detail": str(it.get("detail") or ""),
                    "priority": str(it.get("priority") or "medium"),
                }
            )
        return out

    return {
        "summary": str(d.get("summary") or "").strip(),
        "missing": _items("missing"),
        "suspicious": _items("suspicious"),
    }


def normalize_question(raw_text: str) -> Optional[dict]:
    """把 LLM 输出规整成「下一个问题」；more=false / 解析失败 → None。"""
    d = _loads_maybe(raw_text)
    if not d or not d.get("more"):
        return None
    q = d.get("question") or {}
    if not isinstance(q, dict):
        return None
    text = str(q.get("text") or "").strip()
    if not text:
        return None
    return {
        "field": str(q.get("field") or "general"),
        "text": text,
        "why": str(q.get("why") or ""),
    }


def render_facts(facts: dict) -> str:
    """把已确认信息渲染成给 LLM 读的文本。"""
    rows = [f"- {k}：{v}" for k, v in (facts or {}).items() if v]
    return "\n".join(rows) if rows else "（用户未补充额外信息）"


def normalize_changes(raw_text: str) -> Optional[dict]:
    """把 LLM 输出规整成对比结构；解析失败/无改动 → None（不展示对比，不影响产出）。"""
    d = _loads_maybe(raw_text)
    if not d:
        return None
    changes = []
    for it in (d.get("changes") or [])[:10]:
        if not isinstance(it, dict):
            continue
        before = str(it.get("before") or "").strip()
        after = str(it.get("after") or "").strip()
        if not before and not after:
            continue
        changes.append(
            {
                "section": str(it.get("section") or "改动").strip() or "改动",
                "before": before,
                "after": after,
                "reason": str(it.get("reason") or "").strip(),
            }
        )
    if not changes:
        return None
    return {"summary": str(d.get("summary") or "").strip(), "changes": changes}


# ---------------- handler 协议 ----------------
class SessionCtx:
    """一次节点执行的上下文（节点自行创建 db/gateway，不放进 state）。"""

    def __init__(
        self,
        *,
        db: Any,
        tool: Any,
        gw: LLMGateway,
        input_text: str,
        attachment_text: str,
        attachment_images: Optional[list] = None,
        analysis: dict,
        facts: dict,
        round_no: int,
        max_rounds: int,
        allowed_tools: list[str],
        max_tool_rounds: int,
        invocation_id: Optional[int] = None,
    ) -> None:
        self.db = db
        self.tool = tool
        self.gw = gw
        self.input_text = input_text
        self.attachment_text = attachment_text
        self.attachment_images = attachment_images or []  # 图片附件 object_key 列表
        self.analysis = analysis or {}
        self.facts = facts or {}
        self.round_no = round_no
        self.max_rounds = max_rounds
        self.allowed_tools = allowed_tools or []
        self.max_tool_rounds = max_tool_rounds
        self.invocation_id = invocation_id

    @property
    def material(self) -> str:
        """材料 = 用户需求 + 附件文本（带上限，避免单次请求过大）。"""
        from app.services.attachment import clip_text

        parts = [self.input_text or ""]
        if self.attachment_text:
            parts.append("【附件内容】\n" + self.attachment_text)
        return clip_text("\n\n".join(p for p in parts if p), settings.SESSION_MATERIAL_MAX_CHARS)

    @property
    def material_excerpt(self) -> str:
        """追问决策只看材料节选：多轮里全文每轮重发是最大的 token 浪费。"""
        from app.services.attachment import clip_text

        return clip_text(self.material, settings.SESSION_DECIDE_MATERIAL_CHARS)

    @property
    def max_tokens(self) -> int:
        return 4000

    def user_content(self, text: str) -> "str | list[dict]":
        """多模态 user content：有图片附件时组装 text+image_url parts（纯文本回落）。"""
        if not self.attachment_images:
            return text
        from app.services.attachment import image_parts, with_images

        parts, skipped = image_parts(self.db, self.attachment_images)
        return with_images(text, parts, skipped)


class SessionHandler(Protocol):
    def analyze(self, ctx: SessionCtx) -> tuple[dict, float]: ...

    def decide(self, ctx: SessionCtx) -> tuple[Optional[dict], float]: ...

    def produce(self, ctx: SessionCtx) -> tuple[str, Optional[str], str, float]: ...

    def diff(self, ctx: SessionCtx, result: str) -> tuple[Optional[dict], float]: ...


# ---------------- 默认实现（配置驱动） ----------------
class DefaultHandler:
    """按 Tool 上声明的提示词跑「分析 → 追问决策 → 产出」。

    产出只返回文本（result_ref=None）；需要导出文档的工具请注册自定义 handler
    （如 resume_interactive 会额外导出 docx）。
    """

    def analyze(self, ctx: SessionCtx) -> tuple[dict, float]:
        system = today_note() + (ctx.tool.analysis_prompt or DEFAULT_ANALYSIS_PROMPT)
        r = ctx.gw.call(
            "executor",
            [
                {"role": "system", "content": system},
                {"role": "user", "content": ctx.user_content(ctx.material)},
            ],
            json_mode=True,
            max_tokens=3000,
        )
        return normalize_analysis(r["content"], ctx.input_text), float(r["cost"])

    def decide(self, ctx: SessionCtx) -> tuple[Optional[dict], float]:
        cost = 0.0
        system = today_note() + DEFAULT_DECIDE_PROMPT
        user = (
            f"【材料】（节选，供判断信息是否足够）\n{ctx.material_excerpt}\n\n"
            f"【分析结果】\n{json.dumps(ctx.analysis, ensure_ascii=False)}\n\n"
            f"【用户已补充】\n{render_facts(ctx.facts)}\n\n"
            f"【已追问轮次】{ctx.round_no}/{ctx.max_rounds}"
        )
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        # 受限工具调用循环：LLM 可主动调用白名单内的检测器，步数上限兜底
        tools = None
        if ctx.allowed_tools:
            from app.services.agent.detectors import DETECTOR_TOOL_SPECS

            tools = [t for t in DETECTOR_TOOL_SPECS if t["function"]["name"] in ctx.allowed_tools]

        raw = ""
        for _ in range(max(1, ctx.max_tool_rounds)):
            r = ctx.gw.call("executor", messages, max_tokens=2000, tools=tools)
            cost += float(r["cost"])
            calls = r.get("tool_calls") or []
            if not calls:
                raw = r["content"]
                break
            # 回灌：assistant 的 tool_calls + 各 tool 结果（白名单校验）
            from app.services.agent.detectors import run_detector_json

            messages.append(
                {
                    "role": "assistant",
                    "content": r["content"] or None,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": c["arguments"] or "{}"},
                        }
                        for c in calls
                    ],
                }
            )
            for c in calls:
                if c["name"] not in ctx.allowed_tools:
                    payload = json.dumps({"error": "denied"}, ensure_ascii=False)
                else:
                    payload = run_detector_json(c["name"], ctx.material)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": payload})
        else:
            # 达到工具调用上限仍未收敛 → 用最后一次内容兜底
            r = ctx.gw.call("executor", messages, max_tokens=2000)
            cost += float(r["cost"])
            raw = r["content"]

        return normalize_question(raw), cost

    def produce(self, ctx: SessionCtx) -> tuple[str, Optional[str], str, float]:
        template = ctx.tool.prompt_template or DEFAULT_PRODUCE_PROMPT
        facts_block = ""
        if ctx.facts:
            facts_block = "\n\n【用户补充确认的信息】\n" + render_facts(ctx.facts)
        prompt = template.replace("{input}", ctx.material + facts_block)
        messages = [
            {"role": "system", "content": today_note() + prompt},
            {"role": "user", "content": ctx.user_content("请开始。")},
        ]
        # 流式：worker 注册了增量回调（开关开启）→ 逐块上报给前端
        if ctx.invocation_id:
            from app.worker.stream_registry import get as get_stream_cb

            cb = get_stream_cb(ctx.invocation_id)
            if cb:
                r = ctx.gw.call_stream(
                    "executor", messages, max_tokens=ctx.max_tokens,
                    on_delta=lambda d: cb({"type": "delta", "text": d}),
                )
                return r["content"], None, r["model_used"], float(r["cost"])
        r = ctx.gw.call("executor", messages, max_tokens=ctx.max_tokens)
        return r["content"], None, r["model_used"], float(r["cost"])

    def diff(self, ctx: SessionCtx, result: str) -> tuple[Optional[dict], float]:
        """产出后生成「优化前/后」结构化对比；失败/无改动返回 (None, cost)。"""
        if not result or not ctx.material:
            return None, 0.0
        user = (
            f"【原始材料】\n{ctx.material[:6000]}\n\n"
            f"【优化后的结果】\n{result[:6000]}"
        )
        r = ctx.gw.call(
            "executor",
            [
                {"role": "system", "content": today_note() + DEFAULT_DIFF_PROMPT},
                {"role": "user", "content": user},
            ],
            json_mode=True,
            max_tokens=3000,
        )
        return normalize_changes(r["content"]), float(r["cost"])


DEFAULT_HANDLER = DefaultHandler()


def session_defaults(tool) -> dict:
    """从 tool.interaction_config 取配置，缺省回落全局设置。

    `analyze`（是否走「分析 + 追问」）默认跟随 `tool.interactive`：
    - 多轮工具（interactive=True，如简历/周报）→ 先分析、必要时追问；
    - 简单工具（interactive=False，如翻译/摘要）→ **跳过分析**，一次确认后直接产出。
    简单工具仍会生成会话与调用记录，因此同样可以在同一会话里继续做别的任务。
    """
    cfg = tool.interaction_config or {}
    analyze = bool(cfg.get("analyze", bool(getattr(tool, "interactive", False))))
    max_rounds = int(cfg.get("max_rounds") or settings.SESSION_MAX_ROUNDS)
    return {
        "analyze": analyze,
        "max_rounds": max_rounds if analyze else 0,
        "allow_skip": bool(cfg.get("allow_skip", True)),
        "tool_calls": list(cfg.get("tool_calls") or []) if analyze else [],
        "max_tool_rounds": int(cfg.get("max_tool_rounds") or settings.SESSION_MAX_TOOL_ROUNDS),
        "diff": bool(cfg.get("diff", analyze)),  # 简单工具默认不做「优化前后对比」
    }
