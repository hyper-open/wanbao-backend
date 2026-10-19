"""受限工具调用用的「检测子工具」——纯函数，不调用 LLM。

设计取舍：
- **纯规则/正则实现，不调 LLM** —— 成本可预测（§16.4 成本闭环）、结果可复现、可审计。
- 白名单由 `tool.interaction_config.tool_calls` 声明；图内越权调用直接拒绝。
- 统一返回结构（供 LLM 阅读，也供前端分析卡展示）：
  `{"key","label","found","detail","priority"}`，`found=False` 表示「缺失/可疑」。

这些检测器是 L2 受限工具调用中 LLM 可以主动调用的工具（见 session/graph.py 的 decide 节点）。
"""
from __future__ import annotations

import json
import re

# 形如 2019.03 / 2019-03 / 2019年3月 / 2019/3
_PERIOD = re.compile(r"(?:19|20)\d{2}\s*[.\-/年]\s*(?:0?[1-9]|1[0-2])?")
_QUANT = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|万|亿|千|k|K|倍|人|次|个|单|天|小时|分钟|ms|毫秒|秒|台|条|家)")
_PHONE = re.compile(r"1[3-9]\d{9}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_YEARS_EXP = re.compile(r"(\d+)\s*年(?:以上)?(?:经验|工作|开发|研发)")

_JOB_INTENT_KEYS = ("求职意向", "期望职位", "目标岗位", "应聘职位", "求职岗位", "期望岗位")
_VAGUE_PHRASES = ("负责相关工作", "相关工作", "相关经验", "若干", "一些", "较强", "良好", "一定的", "熟悉各种", "等等")


def _item(key: str, label: str, found: bool, detail: str, priority: str = "medium") -> dict:
    return {"key": key, "label": label, "found": found, "detail": detail, "priority": priority}


def check_contact(text: str) -> dict:
    """联系方式（手机号 / 邮箱）是否具备。"""
    has_phone = bool(_PHONE.search(text))
    has_email = bool(_EMAIL.search(text))
    if has_phone and has_email:
        return _item("contact", "联系方式", True, "已包含手机号与邮箱", "low")
    missing = []
    if not has_phone:
        missing.append("手机号")
    if not has_email:
        missing.append("邮箱")
    return _item("contact", "联系方式", False, f"简历缺少：{'、'.join(missing)}", "high")


def check_quantified(text: str) -> dict:
    """量化成果：是否用数字/比例/规模支撑成绩。"""
    hits = _QUANT.findall(text)
    if len(hits) >= 3:
        return _item("quantified_results", "量化成果", True, f"发现 {len(hits)} 处量化表述", "low")
    if hits:
        return _item("quantified_results", "量化成果", False, f"仅有 {len(hits)} 处量化表述，建议为主业绩补充数字", "medium")
    return _item("quantified_results", "量化成果", False, "未见任何量化成果（数字/比例/规模），建议补充", "high")


def check_timeline(text: str) -> dict:
    """时间线：抽取时间段，检测同一年份重复出现（可能重叠/冲突）。"""
    periods = _PERIOD.findall(text)
    years = [int(p[:4]) for p in periods]
    if not periods:
        return _item("timeline", "时间线", False, "未识别到明确的时间段（如 2021.03—2024.06）", "high")
    dup = sorted({y for y in years if years.count(y) >= 3})
    if dup:
        return _item("timeline", "时间线", False, f"年份 {dup} 出现多次，请确认工作经历是否存在时间重叠", "high")
    span = f"{min(years)}—{max(years)}"
    return _item("timeline", "时间线", True, f"识别到 {len(periods)} 个时间段，跨度 {span}", "low")


def check_job_intent(text: str) -> dict:
    """求职意向 / 目标岗位是否存在。"""
    if any(k in text for k in _JOB_INTENT_KEYS):
        return _item("job_intent", "求职意向", True, "已包含求职意向", "low")
    return _item("job_intent", "求职意向", False, "缺少明确的求职意向/目标岗位，建议补充", "high")


def check_consistency(text: str) -> dict:
    """表述一致性与具体性：空泛措辞、经验年限与实际跨度是否对得上。"""
    vague = [p for p in _VAGUE_PHRASES if p in text]
    parts = []
    ok = True
    if len(vague) >= 2:
        ok = False
        parts.append(f"存在空泛措辞：{'、'.join(vague[:5])}")
    claimed = [int(x) for x in _YEARS_EXP.findall(text)]
    years = sorted({int(p[:4]) for p in _PERIOD.findall(text)})
    if claimed and years:
        actual = max(years) - min(years) + 1
        if claimed[0] > actual + 1:
            ok = False
            parts.append(f"声称 {claimed[0]} 年经验，但简历时间跨度约 {actual} 年，请核对")
    if not parts:
        parts.append("未见明显前后矛盾")
    return _item("consistency", "一致性", ok, "；".join(parts), "medium" if not ok else "low")


DETECTOR_REGISTRY = {
    "check_contact": check_contact,
    "check_quantified": check_quantified,
    "check_timeline": check_timeline,
    "check_job_intent": check_job_intent,
    "check_consistency": check_consistency,
}

# OpenAI 兼容的 function 声明（喂给 LLMGateway.call(tools=...)）
DETECTOR_TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
    for name, desc in (
        ("check_contact", "检查简历是否含手机号与邮箱"),
        ("check_quantified", "检查业绩是否用数字/比例/规模量化"),
        ("check_timeline", "检查时间线是否完整、是否存在重叠"),
        ("check_job_intent", "检查是否写明求职意向/目标岗位"),
        ("check_consistency", "检查是否存在空泛措辞或经验年限与时间跨度矛盾"),
    )
]


def run_detector(name: str, text: str) -> dict:
    """执行一个检测器。未知名称返回错误项（图内还会先做白名单校验）。"""
    fn = DETECTOR_REGISTRY.get(name)
    if fn is None:
        return _item(name, name, False, f"未知检测器：{name}", "low")
    try:
        return fn(text)
    except Exception as e:  # noqa: BLE001  检测器异常不应中断会话
        return _item(name, name, False, f"检测失败：{e}", "low")


def run_detector_json(name: str, text: str) -> str:
    """给 LLM 回灌用的 JSON 字符串。"""
    return json.dumps(run_detector(name, text), ensure_ascii=False)
