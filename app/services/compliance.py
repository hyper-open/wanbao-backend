"""备案登记与公示（§16.3-B）。

官方公示口径（网信办 2024 年公告）：已上线的生成式 AI 应用需在显著位置公示
「所用已备案模型名称 + 备案号」。因此备案以**模型维度**为准（llm_model），
供应商维度（llm_provider）作为兜底与「备案主体」信息的补充。
"""
from typing import Optional

from sqlalchemy.orm import Session

from app.core.timeutil import local_iso
from app.models import LLMModel, LLMProvider, LLMRouting


def _level(model: LLMModel, provider: Optional[LLMProvider]) -> str:
    if model.filing_no:
        return "model"
    if provider and provider.filing_no:
        return "provider"
    return "missing"


def model_filings(db: Session, only_ready: bool = False) -> list[dict]:
    """按模型维度汇总备案信息（模型优先，供应商兜底）。

    only_ready=True 时只返回「已配置备案号、模型启用且供应商启用」的条目，用于对外公示。
    供应商停用后其模型实际不再被调用，故不应出现在公示里（公示口径为「所使用」的模型）。
    """
    rows = (
        db.query(LLMModel, LLMProvider)
        .outerjoin(LLMProvider, LLMModel.provider_id == LLMProvider.id)
        .order_by(LLMModel.provider_id, LLMModel.id)
        .all()
    )
    # 每个模型被哪些角色的降级链引用（公示口径是「所用」模型，便于核对）
    chains: dict = {}
    for r in db.query(LLMRouting).all():
        for n in r.chain or []:
            mid = n.get("model_id")
            if mid is not None:
                chains.setdefault(int(mid), []).append(r.role)

    out = []
    for m, p in rows:
        level = _level(m, p)
        provider_enabled = bool(p.enabled) if p else False
        effective = bool(m.enabled and provider_enabled)
        if only_ready and (level == "missing" or not effective):
            continue
        out.append(
            {
                "model_id": m.id,
                # 公示/出示口径是「模型名称 + 备案号」，故model 用真实模型名，不用内部别名
                "model": m.model_actual,
                "model_actual": m.model_actual,
                "alias": m.alias,
                "chains": chains.get(m.id, []),
                "tier": m.tier,
                "enabled": bool(m.enabled),
                "provider_enabled": provider_enabled,
                "effective_enabled": effective,
                "provider_id": m.provider_id,
                "provider_name": (p.name if p else "") or (m.provider_id or ""),
                "filing_name": m.filing_name or ((p.filing_subject if p else "") or ""),
                "filing_no": m.filing_no or ((p.filing_no if p else "") or ""),
                "subject": (p.filing_subject if p else "") or "",
                "source_url": m.filing_source_url or ((p.filing_source_url if p else "") or ""),
                "checked_at": (
                    local_iso(m.filing_checked_at)
                    if m.filing_checked_at
                    else (local_iso(p.filing_checked_at) if (p and p.filing_checked_at) else None)
                ),
                "level": level,
            }
        )
    return out


def public_filings(db: Session) -> dict:
    """对外公示数据（无需登录）：模型名称 + 备案号 + 备案主体。"""
    items = []
    for f in model_filings(db, only_ready=True):
        items.append(
            {
                "model": f["model"],
                "provider": f["provider_name"],
                "filing_name": f["filing_name"],
                "filing_no": f["filing_no"],
                "subject": f["subject"],
                "source_url": f["source_url"],
            }
        )
    return {"items": items, "count": len(items)}
