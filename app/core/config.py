from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    JWT_SECRET: str = "change-me-in-prod"
    JWT_ALGO: str = "HS256"

    # 数据库：复用已有 MySQL，自动建库 wanbao（真实连接串走 .env，勿写回此处）
    DATABASE_URL: str = "mysql+pymysql://user:password@127.0.0.1:3306/wanbao"
    MYSQL_ROOT_URL: str = "mysql+pymysql://root:password@127.0.0.1:3306/wanbao"
    DB_ECHO: bool = False

    # 复用已有 Redis
    REDIS_URL: str = "redis://127.0.0.1:26379/0"
    ALERT_WEBHOOK_URL: str = ""   # 兜底默认；后台 ALERT_WEBHOOK_URL 可覆盖              # 限流/成本告警 webhook（钉钉/飞书/企微机器人，空=不告警）

    # 对象存储 S3 兼容（rustfs）
    S3_ENDPOINT: str = "http://127.0.0.1:29000"
    S3_ACCESS_KEY: str = ""   # 走 .env
    S3_SECRET_KEY: str = ""   # 走 .env
    S3_BUCKET: str = "wanbao"
    S3_REGION: str = "us-east-1"

    # 微信（留空走 mock）

    # LLM（1=mock，无需 key 即可跑通）

    # 计费

    # 内容安全：provider = mock(默认) | wechat(微信 msgSecCheck 真实路径)
    CONTENT_SECURITY_PROVIDER: str = "mock"   # 兜底默认；后台「限流参数」弹窗可覆盖

    # 结果文件下载链接有效期（秒）
    PRESIGN_EXPIRES_SECONDS: int = 3600

    # Embedding 召回/语义搜索（§16.1-D3 / §7.3）：provider=local(bigram哈希,默认) | openai(配以下三项)

    # LLM 熔断：连续失败 N 次标 down，冷却 M 秒后半开重试
    CB_FAILURE_THRESHOLD: int = 3
    CB_COOLDOWN_SECONDS: int = 60

    # 异步任务超时：pending 超过 N 分钟由 reaper 标失败并退次
    PENDING_TIMEOUT_MINUTES: int = 10

    # 限流：recognize/confirm 每分钟每 Key（IP/openid）最大次数
    RATE_LIMIT_PER_MIN: int = 30           # 兜底默认；后台 app_setting rl_recognize_per_min 可覆盖

    # 防亏告警（§16.4-C）：单工具本月累计 LLM 成本超阈值（元）触发；tool.cost_alert_threshold 可按工具覆盖
    TOOL_COST_ALERT_THRESHOLD: float = 50.0   # 兜底默认；后台 tool_cost_alert_threshold 可覆盖

    # 支付开关：true=模拟支付（联调用，前端可"一键支付成功"，仅限本人订单）；false=真实支付
    # 真实支付回调验签密钥（PAYMENT_MOCK=false 时必填，回调需带 X-Pay-Sign 头）
    PAYMENT_NOTIFY_SECRET: str = ""
    # 微信小程序虚拟支付（PAYMENT_PROVIDER=wechat_virtual；mp 后台「虚拟支付」模块产出，个人主体 2026-08-31 起开放）
    # 微信虚拟支付配置已全部迁移后台（app_setting + channel_account.virtual_pay），不再走 env

    # 生成文档交付方式：
    #   proxy  = 经后端 /api/agent/invocation/{id}/file 代理下载（推荐，无需公网 S3 域名）
    #   presign= 返回 S3 预签名直链（需 S3_PUBLIC_ENDPOINT 对手机可达）
    FILE_DELIVERY: str = "proxy"
    S3_PUBLIC_ENDPOINT: str = ""  # 预签名直链模式下对外可达的 S3 地址（空=沿用 S3_ENDPOINT）

    # 管理后台：仅账号密码登录（Bearer JWT）；初始账号由 seed 创建，登录后请立即改密
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "change-me-in-prod"   # 仅 seed 建号时使用；上线后请立即改密

    # 附件约束
    ATTACHMENT_MAX_BYTES: int = 10 * 1024 * 1024  # 单文件 <=10MB
    ATTACHMENT_TEXT_MAX_CHARS: int = 12000        # 单附件解析文本上限（超出截断后入 prompt）
    ATTACHMENT_IMAGE_MAX_BYTES: int = 6 * 1024 * 1024  # 单图片 base64 输入上限（超限跳过，避免撑爆请求）
    SESSION_ATTACHMENT_TEXT_MAX_CHARS: int = 20000  # 一个子任务内附件文本合计上限
    ATTACHMENT_ALLOWED_MIME: str = "application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.presentationml.presentation,application/pdf,text/plain,text/markdown,image/jpeg,image/png,image/webp"


    # 交互式多轮会话（§6.5 工具图内人工介入）
    SESSION_TTL_MINUTES: int = 30           # 会话有效期（awaiting_* 超时则 expired）   # 兜底默认；后台 session_ttl_minutes 可覆盖（仅对新会话生效）
    SESSION_MAX_ROUNDS: int = 5             # 最多追问轮次（可由 tool.interaction_config.max_rounds 覆盖）
    SESSION_MAX_ACTIVE: int = 3             # 同一用户未付费活跃会话上限（防白嫖）   # 兜底默认；后台 session_max_active 可覆盖
    SESSION_STEP_TIMEOUT_MINUTES: int = 10  # running 卡死回收阈值
    RECORD_ARCHIVE_DAYS: int = 30           # 记录超过 N 天不活跃自动归档
    SESSION_MAX_TOOL_ROUNDS: int = 2        # 图内受限工具调用最大轮次
    SESSION_MAX_TURNS_PER_SESSION: int = 20  # 一个会话最多可含多少个子任务（防白嫖）
    # 上下文护栏：材料（用户输入 + 附件文本）上限，以及「追问决策」只看材料节选
    SESSION_MATERIAL_MAX_CHARS: int = 20000   # 单次送入模型的材料上限
    SESSION_DECIDE_MATERIAL_CHARS: int = 3000  # 追问决策只发材料前 N 字（避免每轮重发全文）
    # 防白嫖：会话按工具「分析次数」扣费，另设每日 LLM 成本硬上限兜底
    SESSION_DAILY_COST_LIMIT: float = 2.0   # 兜底默认；后台 session_daily_cost_limit 可覆盖   # 每用户每日会话累计 LLM 成本上限（元）
    SESSION_CREATE_PER_10MIN: int = 10      # 每用户 10 分钟内新建会话上限（后台 rl_session_user_10m 可覆盖）
    SESSION_CREATE_IP_PER_10MIN: int = 30   # 每 IP 10 分钟内新建会话上限（后台 rl_session_ip_10m 可覆盖；防批量注册）
    # worker 消费线程数（BLPOP 对多消费者原子安全）。>1 可缓解多轮会话的排队；
    # reaper/waitlist 只由 0 号线程执行，避免并发重复回收与重复退次。
    WORKER_CONCURRENCY: int = 1


settings = Settings()
