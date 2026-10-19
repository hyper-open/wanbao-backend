import json
import time
from typing import Optional

from openai import OpenAI, APIError, APITimeoutError, RateLimitError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import LLMProvider, LLMModel, LLMRouting

# 进程级熔断状态（网关每请求新建实例，健康状态必须跨实例共享）
# pid -> {"fails": int, "down_until": float}
_CB_STATE: dict[str, dict] = {}


def _cb_skip(pid: str) -> bool:
    st = _CB_STATE.get(pid)
    return bool(st and st.get("down_until", 0) > time.time())


def _cb_record(db: Session, provider: LLMProvider, ok: bool) -> None:
    """记录成功/失败，达阈值熔断并落库 health_status（仅状态变化时写库）。"""
    st = _CB_STATE.setdefault(provider.id, {"fails": 0, "down_until": 0})
    prev = provider.health_status
    if ok:
        st["fails"] = 0
        st["down_until"] = 0
        if prev != "healthy":
            provider.health_status = "healthy"
            db.commit()
        return
    st["fails"] += 1
    if st["fails"] >= settings.CB_FAILURE_THRESHOLD:
        st["down_until"] = time.time() + settings.CB_COOLDOWN_SECONDS
        st["fails"] = 0  # 冷却后半开重试，重新计数
        if prev != "down":
            provider.health_status = "down"
            db.commit()
    elif prev not in ("degraded", "down"):
        provider.health_status = "degraded"
        db.commit()


def resolve_chain_node(db: Session, node: dict) -> tuple[Optional[LLMProvider], Optional[LLMModel]]:
    """解析降级链节点 -> (供应商, 模型)。

    供应商以「模型当前所属」为准：链节点里存的 provider_id 可能是该模型
    改绑供应商之前的旧值，用旧值会导致节点被静默跳过。
    """
    mid = node.get("model_id")
    model = db.get(LLMModel, mid) if mid is not None else None
    if not model:
        return None, None  # 模型已删除，悬空节点
    return db.get(LLMProvider, model.provider_id), model



def model_caps(model: "LLMModel", cap: str) -> bool:
    """模型能力判定：capabilities JSON 优先，supports_vision 作为旧数据回落。"""
    caps = getattr(model, "capabilities", None) or {}
    if isinstance(caps, str):
        try:
            caps = json.loads(caps)
        except ValueError:
            caps = {}
    if cap == "vision" and "vision" not in caps:
        return bool(getattr(model, "supports_vision", False))
    return bool(caps.get(cap))


def _has_images(messages: list[dict]) -> bool:
    """消息里是否带多模态图片 part（含图请求只路由到 supports_vision 模型）。"""
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and p.get("type") == "image_url":
                    return True
    return False


def _content_text(content) -> str:
    """mock/日志用：多模态 content 取文本部分。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    return str(content or "")


def _llm_mock() -> bool:
    """LLM Mock 模式开关（后台顶栏；app_setting llm_mock，30s 缓存）。"""
    from app.services import appsettings

    return appsettings.get_flag_cached("llm_mock", default=False)



class LLMGateway:
    """多供应商 LLM 网关：按 role 读取有序降级链，失败自动降级 + 熔断（跨请求共享）。

    每实例累加本次调用链的估算成本 total_cost（tokens/1000 × price_per_1k，§16.4）；
    网关实例与一次工具执行一一对应，成本天然归属 invocation。
    """

    def __init__(self, db: Session):
        self.db = db
        self.total_cost = 0.0
        # 本次执行累计 token（观测/上下文护栏）：网关实例与一次工具执行一一对应
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def _load_chain(self, role: str, cap: Optional[str] = None) -> list[dict]:
        """读降级链：优先 `{role}.{cap}` 能力子链（可表达与主链不同的优先级），

        子链未配置（无该行 / 空链）则回落主链——由调用方按能力过滤，
        保证「没配子链 = 现状行为」，零迁移成本。
        """
        if cap:
            sub = self.db.query(LLMRouting).filter(LLMRouting.role == f"{role}.{cap}").first()
            if sub and sub.chain:
                return sub.chain
        r = self.db.query(LLMRouting).filter(LLMRouting.role == role).first()
        return r.chain if r and r.chain else []

    def _client(self, provider: LLMProvider) -> OpenAI:
        return OpenAI(base_url=provider.base_url, api_key=provider.api_key)

    def call(
        self,
        role: str,
        messages: list[dict],
        *,
        json_mode: bool = False,
        temperature: float = 0.3,
        max_tokens: int = 2000,
        tools: Optional[list] = None,
        tool_choice: str = "auto",
    ) -> dict:
        if _llm_mock():
            return self._mock(role, messages, json_mode, tools)

        want_vision = _has_images(messages)
        want_tools = bool(tools)
        # 链选择：所需能力中有专用子链（executor.vision 等）则优先用之，
        # 主链仍按全部所需能力过滤兜底
        need_caps = ["vision" if want_vision else None, "function_call" if want_tools else None]
        need_caps = [c for c in need_caps if c]
        chain = self._load_chain(role, need_caps[0] if need_caps else None)
        last_err = None
        for node in chain:
            prov, model = resolve_chain_node(self.db, node)
            if not prov or not model or not prov.enabled or not model.enabled:
                continue
            if want_vision and not model_caps(model, "vision"):
                continue  # 含图请求只走支持视觉的模型
            if want_tools and not model_caps(model, "function_call"):
                continue  # 工具调用请求只走支持 function calling 的模型
            pid = prov.id
            if _cb_skip(pid):
                continue
            try:
                client = self._client(prov)
                kwargs = dict(
                    model=model.model_actual,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=prov.timeout_ms / 1000,
                )
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = tool_choice
                resp = client.chat.completions.create(**kwargs)
                _cb_record(self.db, prov, ok=True)
                usage = getattr(resp, "usage", None)
                pt = getattr(usage, "prompt_tokens", 0) or 0
                ct = getattr(usage, "completion_tokens", 0) or 0
                cost = (pt + ct) / 1000.0 * float(model.price_per_1k or 0)
                self.total_cost += cost
                self.total_prompt_tokens += pt
                self.total_completion_tokens += ct
                msg = resp.choices[0].message
                return {
                    "content": msg.content or "",
                    # 受限工具调用：把 tool_calls 归一化后透出（现有调用方不读该字段，向后兼容）
                    "tool_calls": [
                        {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                        for tc in (getattr(msg, "tool_calls", None) or [])
                    ],
                    "model_used": f"{prov.id}/{model.model_actual}",
                    "usage": {"prompt_tokens": pt, "completion_tokens": ct},
                    "cost": round(cost, 6),
                }
            except (APITimeoutError, RateLimitError, APIError) as e:
                _cb_record(self.db, prov, ok=False)
                last_err = e
                continue

        if last_err:
            raise RuntimeError(f"All LLM providers failed: {last_err}")
        if want_vision:
            raise RuntimeError("No vision-capable LLM model configured（请在后台为模型勾选「支持视觉」）")
        if want_tools:
            raise RuntimeError("No function-calling LLM model configured（请在后台为模型勾选「支持工具调用」）")
        raise RuntimeError(f"No LLM provider configured for role={role}")

    def call_stream(
        self,
        role: str,
        messages: list[dict],
        *,
        temperature: float = 0.3,
        max_tokens: int = 2000,
        on_delta=None,
    ) -> dict:
        """流式调用：与 call() 同返回结构；每个文本增量回调 on_delta(delta_str)。

        - on_delta 为 None 时等价 call()（仍走流式请求但不上报增量）
        - 某供应商中途失败 → 记账后换下一家重试（已发出的增量由前端在结束时
          用库内最终结果对齐覆盖，不保证跨供应商增量连续）
        """
        if _llm_mock():
            result = self._mock(role, messages, False)
            content = result.get("content") or ""
            if on_delta:
                step = max(1, len(content) // 8)
                for i in range(0, len(content), step):
                    on_delta(content[i: i + step])
            return result

        want_vision = _has_images(messages)
        chain = self._load_chain(role, "vision" if want_vision else None)
        last_err = None
        for node in chain:
            prov, model = resolve_chain_node(self.db, node)
            if not prov or not model or not prov.enabled or not model.enabled:
                continue
            if want_vision and not model_caps(model, "vision"):
                continue  # 子链安全网：子链节点也必须真的支持视觉
            pid = prov.id
            if _cb_skip(pid):
                continue
            try:
                client = self._client(prov)
                stream = client.chat.completions.create(
                    model=model.model_actual,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=prov.timeout_ms / 1000,
                    stream=True,
                    stream_options={"include_usage": True},
                )
                parts: list[str] = []
                pt = ct = 0
                for chunk in stream:
                    if getattr(chunk, "usage", None):
                        pt = chunk.usage.prompt_tokens or 0
                        ct = chunk.usage.completion_tokens or 0
                    if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                        d = chunk.choices[0].delta.content
                        parts.append(d)
                        if on_delta:
                            on_delta(d)
                _cb_record(self.db, prov, ok=True)
                cost = (pt + ct) / 1000.0 * float(model.price_per_1k or 0)
                self.total_cost += cost
                self.total_prompt_tokens += pt
                self.total_completion_tokens += ct
                return {
                    "content": "".join(parts),
                    "tool_calls": [],
                    "model_used": f"{prov.id}/{model.model_actual}",
                    "usage": {"prompt_tokens": pt, "completion_tokens": ct},
                    "cost": round(cost, 6),
                }
            except (APITimeoutError, RateLimitError, APIError) as e:
                _cb_record(self.db, prov, ok=False)
                last_err = e
                continue

        if last_err:
            raise RuntimeError(f"All LLM providers failed: {last_err}")
        if want_vision:
            raise RuntimeError("No vision-capable LLM model configured（请在后台为模型勾选「支持视觉」）")
        raise RuntimeError(f"No LLM provider configured for role={role}")

    def _mock(self, role: str, messages: list, json_mode: bool, tools: Optional[list]) -> dict:
        # mock 也返回模拟 usage，使成本采集管线可离线验证（§16.4）
        price = 0.001 if role == "classifier" else 0.01
        usage = {"prompt_tokens": 100, "completion_tokens": 200}
        cost = round(sum(usage.values()) / 1000.0 * price, 6)
        self.total_cost += cost
        self.total_prompt_tokens += usage["prompt_tokens"]
        self.total_completion_tokens += usage["completion_tokens"]
        if role == "classifier":
            # 仅对用户原始诉求做匹配，避免候选工具 JSON 描述干扰命中
            raw = _content_text(messages[-1]["content"]) if messages else ""
            user_msg = raw.split("候选工具：")[0]
            # 名称 bigram 命中派生置信度：1 命中=confirm，多命中=clarify，0=menu
            from app.services.agent.recall import _bigrams

            hits_b = _bigrams(user_msg)
            matched = []
            for t in tools or []:
                terms = _bigrams(f"{t.get('name', '')}{t.get('category', '')}")
                if terms & hits_b:
                    matched.append(t.get("id"))
            if len(matched) == 1:
                tool_id, conf = matched[0], 0.92
            elif len(matched) >= 2:
                tool_id, conf = matched[0], 0.7
            else:
                tool_id, conf = (tools or [{"id": "copy_rewrite"}])[0].get("id"), 0.45
            return {
                "content": json.dumps(
                    {"tool_id": tool_id, "confidence": conf, "extracted": {}, "needs_menu": conf < 0.6},
                    ensure_ascii=False,
                ),
                "tool_calls": [],
                "model_used": "mock-classifier",
                "usage": usage,
                "cost": cost,
            }
        user_msg = _content_text(messages[-1]["content"]) if messages else ""
        return {
            "content": f"[MOCK LLM 输出]\n{user_msg}",
            "tool_calls": [],
            "model_used": "mock-executor",
            "usage": usage,
            "cost": cost,
        }
