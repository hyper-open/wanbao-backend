from datetime import datetime
from sqlalchemy import (
    Column, BigInteger, Integer, String, Text, Numeric, Boolean, Float, JSON, DateTime, Date, ForeignKey,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.dialects.mysql import LONGBLOB

from app.core.timeutil import local_iso, utcnow  # noqa: F401  (utcnow 对其它模块保持从 models 导出)

Base = declarative_base()


def _to_dict(self):
    out = {}
    for c in self.__table__.columns:
        v = getattr(self, c.name)
        if isinstance(v, datetime):
            v = local_iso(v)  # naive-UTC -> 本地时间展示（口径见 core/timeutil.py）
        elif hasattr(v, "isoformat"):
            v = v.isoformat()
        out[c.name] = v
    return out


Base.to_dict = _to_dict


class User(Base):
    __tablename__ = "user"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    openid = Column(String(64), unique=True, nullable=True, index=True)  # 微信渠道 legacy 身份，新渠道用 user_identity
    unionid = Column(String(64))
    phone = Column(String(20))
    phone_verified = Column(Boolean, default=False)
    nickname = Column(String(64))
    avatar = Column(String(255))
    invite_code = Column(String(16), unique=True, index=True)  # 邀请码（懒生成）
    status = Column(String(16), default="normal")
    created_at = Column(DateTime, default=utcnow)


class ChannelAccount(Base):
    """渠道账号（应用）：一个微信小程序 / 公众号 / 其他开放平台应用。

    用于多渠道运营：登录按 appid 找到对应账号换取 openid；订单/调用/会话按账号归属统计。
    非微信渠道先登记（type=alipay/other），接入时再补登录与支付实现。
    """
    __tablename__ = "channel_account"
    id = Column(String(32), primary_key=True)          # 短 id，如 wx_mp_main
    type = Column(String(16), default="wechat_mp", index=True)  # wechat_mp / alipay / other
    name = Column(String(64), nullable=False)          # 显示名，如「万宝工具箱小程序」
    appid = Column(String(64), index=True)             # 微信小程序 appid / 其他渠道应用 id
    secret = Column(String(128))                       # 密钥（仅内网后台可见）
    enabled = Column(Boolean, default=True, index=True)
    mock = Column(Boolean, default=False)               # Mock 模式（按渠道：登录/手机号/安全检查走假数据；接入新渠道时同样生效）
    entity_type = Column(String(16), default="personal")  # 主体：personal/company（提审口径与月限规则不同）
    payment_provider = Column(String(24))               # 支付通道：mock/wechat_virtual/wechat_merchant（NULL=mock）
    payment_mock = Column(Boolean, default=True)        # 支付 Mock（模拟支付成功/回调免验签；联调开关，生产渠道务必关）
    remark = Column(String(255))
    # 本月收款阈值（按渠道账号独立配置；NULL = 用系统默认 100000 / 80000 / 90000）
    month_limit = Column(Integer)        # 月收款上限（主体月限）
    # 微信虚拟支付（wechat_mp 账户专用，§16.2-A）：留空字段回落全局配置
    # {offer_id, appkey, sandbox_appkey, push_token, aes_key, sandbox}
    virtual_pay = Column(JSON)
    warn_threshold = Column(Integer)     # 预警阈值（临近月限，建议升级主体）
    stop_threshold = Column(Integer)     # 停售阈值
    created_at = Column(DateTime, default=utcnow)


class UserIdentity(Base):
    """渠道身份：统一账户 user 下可挂多个渠道身份（wechat/alipay/...，§9.5 多渠道）。"""
    __tablename__ = "user_identity"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("user.id"), index=True)
    channel = Column(String(16), nullable=False)   # wechat / alipay / ...
    channel_uid = Column(String(64), nullable=False)
    channel_account_id = Column(String(32), index=True)  # 该身份来自哪个应用（渠道账号）
    created_at = Column(DateTime, default=utcnow)
    last_login_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("channel", "channel_uid", name="uq_channel_uid"),)


class Account(Base):
    __tablename__ = "account"
    user_id = Column(BigInteger, ForeignKey("user.id"), primary_key=True)
    balance = Column(Numeric(10, 2), default=0)
    quota_total = Column(Integer, default=0)
    quota_used = Column(Integer, default=0)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class Order(Base):
    __tablename__ = "order"
    id = Column(String(32), primary_key=True)
    user_id = Column(BigInteger, ForeignKey("user.id"), index=True)
    package_id = Column(Integer)
    amount = Column(Numeric(10, 2))
    platform = Column(String(8))
    channel = Column(String(8))
    client_channel = Column(String(16), default="wechat")  # 来源渠道 wechat/alipay/...（§9.5）
    channel_account_id = Column(String(32), index=True)      # 归属渠道账号（哪个应用）
    payment_provider = Column(String(16), default="mock")  # 支付通道 mock/wechat_virtual/wechat_merchant/apple_iap（§16.2-A）
    entity = Column(String(16), default="personal")        # 收款主体口径 personal/company（§16.2-C 对账拆分）
    status = Column(String(16), default="pending", index=True)
    prepay_params = Column(JSON)
    origin_amount = Column(Numeric(10, 2))      # 折扣前原价（无活动时等于 amount）
    discount = Column(Numeric(3, 2))            # 命中活动的折扣，如 0.50（打折方式）
    bonus_quota = Column(Integer, default=0)    # 命中「送次数」活动时额外赠送的次数（支付到账后发放）
    campaign_name = Column(String(64))          # 命中活动名（便于对账归因）
    paid_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)


class Transaction(Base):
    __tablename__ = "transaction"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("user.id"), index=True)
    type = Column(String(16))  # recharge/consume/refund/adjust
    channel_account_id = Column(String(32), index=True)   # 归属渠道账号
    amount = Column(Numeric(10, 2))
    quota_delta = Column(Integer)
    ref_id = Column(String(32))
    remark = Column(String(255))
    created_at = Column(DateTime, default=utcnow, index=True)


class QuotaGrant(Base):
    """配额台账：按笔记录配额（可带到期时间）。

    `account.quota_total/quota_used` 继续表示**永久额度（充值）**；
    邀请奖励等「会过期」的配额走本表 —— 扣费时优先扣最快到期的一笔。
    `expires_at` 为 NULL 表示永久。
    """
    __tablename__ = "quota_grant"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("user.id"), index=True)
    source = Column(String(16))                     # invite / recharge / adjust
    quota = Column(Integer, default=0)              # 发放数量
    remaining = Column(Integer, default=0)          # 剩余数量
    ref_id = Column(String(64))                     # 关联单号（邀请 id / 订单号）
    expires_at = Column(DateTime, index=True)       # NULL = 永久
    created_at = Column(DateTime, default=utcnow, index=True)


class Invite(Base):
    """邀请关系：一个被邀请人只能被邀请一次。

    两段式奖励（都发给**邀请人**）：
    1. 绑定成功（新用户用邀请码注册）→ 发 x 次（奖励活动配置 reward_quota）；
    2. 被邀请人**首次充值成功** → 按档位次数 y 发 floor(y / recharge_divisor) 次。
    """
    __tablename__ = "invite"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    inviter_id = Column(BigInteger, ForeignKey("user.id"), index=True)
    invitee_id = Column(BigInteger, ForeignKey("user.id"), unique=True, index=True)
    code = Column(String(16))                       # 使用的邀请码
    status = Column(String(16), default="pending", index=True)  # pending/rewarded/invalid/capped
    reward_quota = Column(Integer, default=0)       # 累计实际发放次数（绑定 + 首充）
    bind_quota = Column(Integer, default=0)         # 绑定阶段发放：邀请成功 x 次
    recharge_quota = Column(Integer, default=0)     # 首充阶段发放：floor(档位次数 / 除数)
    order_id = Column(String(32))                   # 触发首充发奖的订单
    created_at = Column(DateTime, default=utcnow, index=True)
    rewarded_at = Column(DateTime)                  # 首充发奖时间
    bind_rewarded_at = Column(DateTime)             # 绑定发奖时间


# 工具分类默认枚举（key -> 展示名）；tool_category 字典表为空时作为回落
CATEGORY_NAMES = {
    "writing": "写作",
    "office": "办公",
    "doc": "文档",
    "marketing": "营销",
    "code": "编程",
    "study": "学习",
    "design": "设计",
    "translate": "翻译",
    "life": "生活",
    "data": "数据",
    "other": "其他",
}


class Tool(Base):
    __tablename__ = "tool"
    id = Column(String(64), primary_key=True)
    name = Column(String(64), nullable=False)
    category = Column(String(32))
    description = Column(Text)
    disclaimer = Column(Text)                      # 专项免责声明（法律/医疗/金融类工具；空=仅通用免责）
    input_schema = Column(JSON, default=dict)
    prompt_template = Column(Text)
    output_template = Column(Text)
    executor = Column(String(64))  # 代码扩展点 key，NULL=纯配置
    price_quota = Column(Integer, default=1)          # 生成/导出消耗次数
    analyze_quota = Column(Integer, default=0)        # 分析消耗次数（交互式工具；0=不扣）
    channel_pricing = Column(JSON)  # 按渠道覆盖次数定价 {"alipay":2}，未配置用 price_quota（§9.5）
    cost_alert_threshold = Column(Numeric(10, 4))  # 单工具月累计 LLM 成本告警阈值（元），NULL=全局默认（§16.4-C）
    model_preference = Column(String(64))
    needs_attachment = Column(Boolean, default=False)  # 是否需要用户上传附件
    async_exec = Column(Boolean, default=False)        # 是否异步执行（重活/解析类）
    # 交互式多轮会话（§6.5「工具图可含人工介入节点」）：图内 interrupt 暂停等用户输入
    interactive = Column(Boolean, default=False)
    analysis_prompt = Column(Text)     # 分析步系统提示词，空=框架默认
    interaction_config = Column(JSON)  # {"max_rounds":5,"allow_skip":true,"tool_calls":[...],"max_tool_rounds":2}
    tags = Column(JSON)            # 搜索标签 ["简历","求职"]（§16.1 工具发现）
    channel_ids = Column(JSON)     # 可见渠道账户 id 列表；空=全渠道（多渠道矩阵：每个小程序可有自己的工具集）
    sort = Column(Integer, default=0)                  # 类目内排序，小在前
    is_hot = Column(Boolean, default=False)            # 运营热门标记
    usage_count = Column(Integer, default=0)           # 累计成功调用数（热度排序）
    enabled = Column(Boolean, default=True)
    version = Column(Integer, default=1)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class ToolCategory(Base):
    """工具分类字典：后台「工具分类」维护名称/排序/启用，C 端类目按 sort 升序展示。"""

    __tablename__ = "tool_category"
    key = Column(String(32), primary_key=True)  # 存入 tool.category 的值
    name = Column(String(32), nullable=False)   # 展示名
    sort = Column(Integer, default=0)           # 排序，小在前
    enabled = Column(Boolean, default=True)


class ToolStatsDaily(Base):
    """工具运营日统计（worker 在子任务终态投影时累加；运营看板数据源）。

    口径：calls=产出次数（success+failed，取消不计）；amount/charged 仅 success 计入
    （failed 会退次，不计收入）；llm_cost 含免费阶段（成本真实性）。
    """

    __tablename__ = "tool_stats_daily"
    stat_date = Column(Date, primary_key=True)      # 本地自然日
    tool_id = Column(String(64), primary_key=True)
    calls = Column(Integer, default=0)
    success = Column(Integer, default=0)
    failed = Column(Integer, default=0)
    quota_charged = Column(Integer, default=0)
    amount_charged = Column(Numeric(10, 2), default=0)
    llm_cost = Column(Numeric(10, 6), default=0)
    total_seconds = Column(Integer, default=0)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class ToolEmbedding(Base):
    """工具向量缓存：语义召回/搜索共用。

    content 是计算向量时的文本（name+desc+tags）；工具编辑后 content 变化即自动重算。
    向量以 JSON 存储（MySQL 9.0 社区版无 VECTOR_DISTANCE，相似度在应用层计算，
    工具规模量级下全量取出内存余弦即可）。
    """

    __tablename__ = "tool_embedding"
    tool_id = Column(String(64), ForeignKey("tool.id"), primary_key=True)
    model = Column(String(64), primary_key=True)  # embedding 模型名（换模型即全部重算）
    content = Column(Text)
    vector = Column(JSON)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class AppSetting(Base):
    """运行时键值开关（后台可切换，即时生效；worker 每个任务开始时读取）。"""

    __tablename__ = "app_setting"
    key = Column(String(64), primary_key=True)
    value = Column(String(255))
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class ToolInvocation(Base):
    __tablename__ = "tool_invocation"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("user.id"), index=True)
    tool_id = Column(String(64), ForeignKey("tool.id"))
    tool_version = Column(Integer)
    channel_account_id = Column(String(32), index=True)  # 归属渠道账号
    input_text = Column(Text)
    attachment_ids = Column(JSON)
    intent_confidence = Column(Float)
    model_used = Column(String(64))
    cost_amount = Column(Numeric(10, 4), default=0)  # 估算 LLM 成本（元），§16.4 成本闭环
    # 扣费快照（对账用）：扣费时刻的单价与应付金额，避免日后调价/活动影响历史
    unit_price_snapshot = Column(Numeric(10, 4))     # 元/次
    amount_charged = Column(Numeric(10, 2))          # 本次应付金额（元）；退款后置 0
    status = Column(String(16), default="pending", index=True)  # pending/running/success/failed/charged_refunded
    quota_charged = Column(Integer)
    result_ref = Column(String(255))
    result_text = Column(Text)
    err = Column(Text)
    # 归档：用户手动归档，或系统对超过 N 天不活跃的记录自动归档
    archived = Column(Boolean, default=False, index=True)
    archived_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)
    finished_at = Column(DateTime)


class Attachment(Base):
    __tablename__ = "attachment"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("user.id"), index=True)
    tool_invocation_id = Column(BigInteger, ForeignKey("tool_invocation.id"), nullable=True)
    object_key = Column(String(255))
    filename = Column(String(255))
    mime = Column(String(128))
    size = Column(Integer)
    status = Column(String(16), default="uploaded")
    parsed_meta = Column(JSON)
    created_at = Column(DateTime, default=utcnow)


class LLMProvider(Base):
    __tablename__ = "llm_provider"
    id = Column(String(32), primary_key=True)
    name = Column(String(64))
    type = Column(String(32))
    base_url = Column(String(255))
    api_key = Column(String(255))
    priority = Column(Integer)
    enabled = Column(Boolean, default=True)
    health_status = Column(String(16), default="healthy")
    timeout_ms = Column(Integer, default=30000)
    # 合规出示（§16.3-B）：供应商=备案主体层面，作为模型未填时的兜底
    filing_subject = Column(String(128))     # 备案主体企业名称
    filing_no = Column(String(128))          # 大模型/算法备案号（兜底）
    filing_source_url = Column(String(255))  # 核对来源链接
    filing_checked_at = Column(DateTime)     # 最近核对时间


class LLMModel(Base):
    """可用模型目录。

    注意：模型**不带 role** —— 「某个模型服务哪个角色」由 llm_routing.chain 决定，
    因此同一个模型可以被多条降级链引用（配一次即可多角色复用），也不会出现
    同一模型两条记录导致的配置漂移与公示重复。
    """
    __tablename__ = "llm_model"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    provider_id = Column(String(32), ForeignKey("llm_provider.id"))
    model_actual = Column(String(64))
    alias = Column(String(32))
    tier = Column(String(16))
    price_per_1k = Column(Numeric(8, 4))
    enabled = Column(Boolean, default=True)
    # 合规出示（§16.3-B）：备案与公示以「模型名称 + 备案号」为准，故主记录在模型维度
    supports_vision = Column(Boolean, default=False)  # 旧标记：回落用，见 capabilities
    capabilities = Column(JSON)  # 能力标签 {"vision","function_call","audio_in","tts"}；请求时按需过滤
    filing_name = Column(String(128))        # 备案的模型/算法名称（公示用）
    filing_no = Column(String(128))          # 备案号
    filing_source_url = Column(String(255))
    filing_checked_at = Column(DateTime)


class LLMRouting(Base):
    __tablename__ = "llm_routing"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    role = Column(String(16), unique=True, nullable=False)
    chain = Column(JSON)  # 有序降级链 [{provider_id, model_id}, ...]


class RechargePackage(Base):
    __tablename__ = "recharge_package"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(64))
    quota = Column(Integer)
    price = Column(Numeric(10, 2))
    price_ios = Column(Numeric(10, 2))
    description = Column(String(255))
    sort = Column(Integer)
    enabled = Column(Boolean, default=True)
    channels = Column(JSON)          # 可售渠道 ["wechat","alipay"]，NULL=仅 wechat（§9.5）
    channel_prices = Column(JSON)    # 渠道价格覆盖 {"alipay":8.8}，未配置回落 price/price_ios


class AdminUser(Base):
    """后台管理员账号（单角色：admin/operator/viewer 预留）。"""
    __tablename__ = "admin_user"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True)
    password_hash = Column(String(255))
    role = Column(String(16), default="operator")
    name = Column(String(64))                      # 显示名（可空）
    status = Column(String(16), default="active")  # active / disabled
    last_login_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)


class ChannelCampaign(Base):
    """渠道活动（营销）：同一渠道同一类型同一时间仅一条生效（按 start_at 最新者优先）。

    类型 `type`：
    - `discount` 充值优惠：作用于「实付金额」，次数不变（5 折 = 付一半钱拿同样次数），参数在 `discount`
    - `invite`   邀请有奖：①注册即送 ②首充加奖（比例/期限）与各自日/月上限、奖励有效期，参数在 `config`
    - `checkin`  每日签到：签到奖励次数 / 有效期，参数在 `config`

    注：**没有配置对应活动 = 该活动不启用**（不回落任何默认值）。
    """
    __tablename__ = "channel_campaign"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(64), nullable=False)       # 活动名，如「2 月新用户 5 折」
    type = Column(String(16), default="discount", index=True)  # discount / invite / checkin
    channel = Column(String(16), default="wechat", index=True)
    discount = Column(Numeric(3, 2), nullable=True)  # 0.50 = 5 折（仅 discount 类型）
    config = Column(JSON)                            # 与 type 相关的参数
    start_at = Column(DateTime, index=True)
    end_at = Column(DateTime, nullable=True)          # NULL = 长期有效
    enabled = Column(Boolean, default=True, index=True)
    remark = Column(String(255))
    created_at = Column(DateTime, default=utcnow)


class AuditLog(Base):
    """内容安全审计日志：覆盖 输入/输出/附件 三层筛查。"""
    __tablename__ = "audit_log"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("user.id"), nullable=True, index=True)
    stage = Column(String(16))            # input/output/attachment
    passed = Column(Boolean, default=True)
    reason = Column(String(255))
    sample = Column(Text)                 # 命中片段（前 200 字）
    created_at = Column(DateTime, default=utcnow, index=True)


class AlertLog(Base):
    """运营告警：月限逼近/停售、内容命中统计等触发。"""
    __tablename__ = "alert_log"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    kind = Column(String(32))             # month_limit_warn / month_limit_stop
    message = Column(String(255))
    payload = Column(JSON)
    created_at = Column(DateTime, default=utcnow, index=True)


class Feedback(Base):
    """用户反馈/举报（监管要求提供投诉举报渠道，§16.3-C）。"""
    __tablename__ = "feedback"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("user.id"), index=True)
    invocation_id = Column(BigInteger, nullable=True)  # 可选：关联具体生成结果
    type = Column(String(16))             # report(举报) / suggestion(建议)
    content = Column(Text)
    status = Column(String(16), default="open", index=True)  # open/processed/ignored
    reply = Column(Text)                     # 管理员回复（小程序「我的反馈记录」可见）
    replied_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow, index=True)


class RestockWaitlist(Base):
    """停售预约（§16.2-B）：月限触顶停售期间收集"开售提醒"名单，次月 1 号触达。"""
    __tablename__ = "restock_waitlist"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("user.id"), unique=True, index=True)  # 一人一条，幂等
    channel = Column(String(16), default="wechat")  # 预约来源渠道（触达时区分订阅消息模板）
    notified = Column(Boolean, default=False, index=True)
    created_at = Column(DateTime, default=utcnow, index=True)
    notified_at = Column(DateTime)


# ============ 交互式多轮会话（§6.5 工具图内人工介入） ============
class ToolSession(Base):
    """交互式会话的业务态**投影**。

    图的权威状态在 lg_checkpoint*（LangGraph checkpointer）；
    本表只是「前端视图」，由 worker 在每次 graph.invoke 返回后统一写入（唯一写入点），
    单向投影，因此不会与图状态冲突。
    """
    __tablename__ = "tool_session"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("user.id"), index=True)
    tool_id = Column(String(64), ForeignKey("tool.id"), index=True)
    channel = Column(String(16), default="wechat")
    channel_account_id = Column(String(32), index=True)  # 归属渠道账号（哪个应用）
    # running / awaiting_answer / awaiting_confirm / generating / success / failed / expired / cancelled
    status = Column(String(24), default="running", index=True)
    input_text = Column(Text)                 # 用户初始需求
    attachment_ids = Column(JSON)
    attachment_text = Column(Text)            # 已解析附件文本（缓存，避免每轮重复解析）
    analysis = Column(JSON)                   # 分析结果（summary/missing/suspicious）
    facts = Column(JSON)                      # 已确认信息 {field: value}
    current_question = Column(JSON)           # 当前待回答的问题（投影自 interrupt）
    round_count = Column(Integer, default=0)
    max_rounds = Column(Integer, default=5)
    llm_cost = Column(Numeric(10, 4), default=0)   # 免费阶段累计 LLM 成本（§16.4）
    quota_price = Column(Integer)                  # 生成将消耗次数（展示用）
    unit_price_snapshot = Column(Numeric(10, 4))
    amount_charged = Column(Numeric(10, 2))
    charged = Column(Boolean, default=False)       # 仅 confirm 后 True
    invocation_id = Column(BigInteger)             # 最终产出（复用 ToolInvocation）
    err = Column(Text)
    step_started_at = Column(DateTime)             # 当前步骤开始时间（卡死回收）
    expires_at = Column(DateTime, index=True)
    # 多子任务：一个会话可串行做多个任务（每个任务一个工具）
    # 下列字段是「active turn 的镜像」，让既有读者（get_session / 后台列表）改动最小；
    # 权威数据在 session_turn。
    turn_count = Column(Integer, default=0)        # 已创建的子任务数（seq 上限）
    active_turn_id = Column(BigInteger)            # 当前进行中的子任务（session_turn.id）
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    __table_args__ = (
        Index("ix_session_user_status", "user_id", "status"),
        Index("ix_session_status_expires", "status", "expires_at"),
    )


class SessionTurn(Base):
    """会话里的一个「子任务」（一个工具做一件事），是**可计费单元**。

    多工具会话：一个 ToolSession 含 1..N 个 SessionTurn，串行执行；
    每个 turn 有自己独立的 LangGraph 线程（`{session_id}:{seq}`）与自己的 ToolInvocation。
    与 ToolSession 相同：本表是图的**单向投影**，由 worker 统一写入。
    """
    __tablename__ = "session_turn"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(BigInteger, ForeignKey("tool_session.id"), index=True)
    seq = Column(Integer, nullable=False)          # 会话内序号，1-based
    tool_id = Column(String(64), ForeignKey("tool.id"), index=True)
    # running / awaiting_answer / awaiting_confirm / generating / success / failed / expired / cancelled
    status = Column(String(24), default="running", index=True)
    input_text = Column(Text)                      # 本子任务的用户输入
    attachment_ids = Column(JSON)
    attachment_text = Column(Text)                 # 已解析附件文本（缓存，避免每轮重复解析）
    attachment_images = Column(JSON)               # 图片附件 object_key 列表（每轮请求时转 data URL 给视觉模型）
    analysis = Column(JSON)                        # 分析结果（summary/missing/suspicious）
    facts = Column(JSON)                           # 已确认信息 {field: value}
    current_question = Column(JSON)                # 当前待回答的问题（投影自 interrupt）
    round_count = Column(Integer, default=0)
    max_rounds = Column(Integer, default=5)
    llm_cost = Column(Numeric(10, 4), default=0)   # 本子任务累计 LLM 成本（§16.4）
    prompt_tokens = Column(Integer, default=0)     # 累计输入 token（观测/护栏）
    completion_tokens = Column(Integer, default=0) # 累计输出 token
    quota_price = Column(Integer)                  # 生成将消耗次数（展示用）
    unit_price_snapshot = Column(Numeric(10, 4))
    amount_charged = Column(Numeric(10, 2))
    charged = Column(Boolean, default=False)       # 仅 confirm 后 True
    invocation_id = Column(BigInteger)             # 本子任务产出（复用 ToolInvocation）
    result_text = Column(Text)                     # 产出文本（供「基于上一条结果」显式引用）
    result_ref = Column(String(255))
    err = Column(Text)
    step_started_at = Column(DateTime)             # 当前步骤开始时间（卡死回收）
    expires_at = Column(DateTime, index=True)
    finished_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uk_turn_session_seq"),
        Index("ix_turn_session_seq", "session_id", "seq"),
        Index("ix_turn_status_expires", "status", "expires_at"),
    )


class SessionMessage(Base):
    """会话消息（前端渲染用的扁平列表，由 worker 投影写入）。

    `turn_seq` 指向所属子任务（NULL = 历史数据，前端按 turn 1 处理）；
    前端据此把消息按子任务分组展示。
    """
    __tablename__ = "session_message"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(BigInteger, ForeignKey("tool_session.id"), index=True)
    turn_seq = Column(Integer, index=True)  # 所属子任务序号（session_turn.seq）
    role = Column(String(16))   # user / assistant / system
    type = Column(String(16))   # text / analysis / question / answer / notice
    content = Column(Text)
    payload = Column(JSON)
    created_at = Column(DateTime, default=utcnow, index=True)


# ---- LangGraph checkpointer 存储（自建 MySQL saver，见 core/llm/checkpoint.py）----
class LangCheckpoint(Base):
    __tablename__ = "lg_checkpoint"
    thread_id = Column(String(64), primary_key=True)
    checkpoint_ns = Column(String(255), primary_key=True, default="")
    checkpoint_id = Column(String(64), primary_key=True)
    parent_checkpoint_id = Column(String(64))
    checkpoint_type = Column(String(32))   # serde 的 (type, bytes) 二元组
    checkpoint = Column(LONGBLOB)
    metadata_type = Column(String(32))
    meta_blob = Column("metadata", LONGBLOB)  # 属性名避开声明式保留名 metadata
    created_at = Column(DateTime, default=utcnow)


class LangCheckpointBlob(Base):
    __tablename__ = "lg_checkpoint_blob"
    thread_id = Column(String(64), primary_key=True)
    checkpoint_ns = Column(String(255), primary_key=True, default="")
    channel = Column(String(128), primary_key=True)
    version = Column(String(64), primary_key=True)
    type = Column(String(32))
    value = Column(LONGBLOB)


class LangCheckpointWrite(Base):
    __tablename__ = "lg_checkpoint_write"
    thread_id = Column(String(64), primary_key=True)
    checkpoint_ns = Column(String(255), primary_key=True, default="")
    checkpoint_id = Column(String(64), primary_key=True)
    task_id = Column(String(128), primary_key=True)
    idx = Column(Integer, primary_key=True)
    channel = Column(String(128))
    type = Column(String(32))
    value = Column(LONGBLOB)
    task_path = Column(String(255), default="")
