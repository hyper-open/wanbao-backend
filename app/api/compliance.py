"""对外合规信息（无需登录）：已备案大模型公示（§16.3-B）。

小程序「用户协议与隐私」页与产品详情处展示所用已备案模型名称与备案号。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.services.compliance import public_filings

router = APIRouter()


@router.get("/filings")
def filings(db: Session = Depends(get_db)):
    data = public_filings(db)
    # 主体口径按默认渠道账号配置（提审材料会问）；未配置回落 personal
    from app.models import ChannelAccount

    acc = db.query(ChannelAccount).filter(ChannelAccount.id == "default").first()
    data["entity"] = (acc.entity_type if acc else None) or "personal"
    return data
