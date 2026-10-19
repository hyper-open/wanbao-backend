from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
from app.models import Base, CATEGORY_NAMES


def _make_engine():
    return create_engine(
        settings.DATABASE_URL,
        echo=settings.DB_ECHO,
        future=True,
        pool_pre_ping=True,
    )


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def ensure_database() -> None:
    """MySQL 下确保目标库存在（复用已有实例，不重新部署）。"""
    if not settings.DATABASE_URL.startswith("mysql"):
        return
    from sqlalchemy import create_engine as _ce, text

    db_name = settings.DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    # 从 DATABASE_URL 解析业务库用户，授予新库权限
    user = settings.DATABASE_URL.split("//", 1)[1].split(":", 1)[0]
    try:
        root = _ce(settings.MYSQL_ROOT_URL, pool_pre_ping=True)
        with root.connect() as conn:
            conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4"))
            conn.execute(text(f"GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{user}'@'%'"))
            conn.execute(text("FLUSH PRIVILEGES"))
        print(f"[db] ensured database `{db_name}` (granted to {user})")
    except Exception as e:  # noqa: BLE001
        print(f"[db] ensure_database warning: {e}")


def _migrate() -> None:
    """开发期轻量迁移：对已有库补加新增列（create_all 不会改表）。"""
    from sqlalchemy import text

    alters = [
        "ALTER TABLE tool ADD COLUMN needs_attachment TINYINT(1) NOT NULL DEFAULT 0",
        "ALTER TABLE tool ADD COLUMN async_exec TINYINT(1) NOT NULL DEFAULT 0",
        # 工具专项免责声明（法律/医疗/金融类；C 端工具详情页横幅展示）
        "ALTER TABLE tool ADD COLUMN disclaimer TEXT",
        "ALTER TABLE channel_account ADD COLUMN mock TINYINT(1) NOT NULL DEFAULT 0",
        "ALTER TABLE channel_account ADD COLUMN entity_type VARCHAR(16) NULL DEFAULT 'personal'",
        "ALTER TABLE channel_account ADD COLUMN payment_provider VARCHAR(24) NULL",
        "ALTER TABLE channel_account ADD COLUMN payment_mock TINYINT(1) NOT NULL DEFAULT 1",
        # 工具向量缓存（语义召回/搜索；JSON 存储，相似度应用层计算）
        "CREATE TABLE IF NOT EXISTS tool_embedding ("
        "tool_id VARCHAR(64) NOT NULL, model VARCHAR(64) NOT NULL, content TEXT NULL, "
        "vector JSON NULL, updated_at DATETIME NULL, PRIMARY KEY (tool_id, model))",
        # 运行时键值开关（如流式输出开关；后台切换即时生效）
        "CREATE TABLE IF NOT EXISTS app_setting ("
        "key VARCHAR(64) NOT NULL, value VARCHAR(255) NULL, updated_at DATETIME NULL, "
        "PRIMARY KEY (key))",
        # 工具运营日统计（看板趋势/排行数据源）
        "CREATE TABLE IF NOT EXISTS tool_stats_daily ("
        "stat_date DATE NOT NULL, tool_id VARCHAR(64) NOT NULL, calls INT DEFAULT 0, "
        "success INT DEFAULT 0, failed INT DEFAULT 0, quota_charged INT DEFAULT 0, "
        "amount_charged DECIMAL(10,2) DEFAULT 0, llm_cost DECIMAL(10,6) DEFAULT 0, "
        "total_seconds INT DEFAULT 0, updated_at DATETIME NULL, PRIMARY KEY (stat_date, tool_id))",
        "ALTER TABLE tool_invocation ADD COLUMN result_text TEXT",
        "ALTER TABLE attachment MODIFY COLUMN mime VARCHAR(128) NOT NULL",
        "ALTER TABLE llm_provider ADD COLUMN filing_no VARCHAR(128) NULL",
        "ALTER TABLE tool_invocation ADD COLUMN cost_amount DECIMAL(10,4) NOT NULL DEFAULT 0",
        "ALTER TABLE tool ADD COLUMN channel_pricing JSON NULL",
        "ALTER TABLE recharge_package ADD COLUMN channels JSON NULL",
        "ALTER TABLE recharge_package ADD COLUMN channel_prices JSON NULL",
        "ALTER TABLE `order` ADD COLUMN client_channel VARCHAR(16) NULL DEFAULT 'wechat'",
        "ALTER TABLE `user` MODIFY COLUMN openid VARCHAR(64) NULL",  # 多渠道：非微信身份 openid 为空（§9.5）
        # 多模态：模型视觉能力标记 + 会话子任务图片附件缓存（图片输入到模型链路）
        "ALTER TABLE llm_model ADD COLUMN supports_vision TINYINT(1) NOT NULL DEFAULT 0",
        "ALTER TABLE session_turn ADD COLUMN attachment_images JSON NULL",
        "ALTER TABLE llm_model ADD COLUMN capabilities JSON NULL",
        "ALTER TABLE llm_routing MODIFY COLUMN role VARCHAR(32) NOT NULL",  # 能力子链：executor.vision 等
        # 视觉子链：空链=回落主链按能力过滤；配置后即表达与主链不同的优先级
        "INSERT IGNORE INTO llm_routing (role, chain) VALUES ('executor.vision', JSON_ARRAY())",
        "ALTER TABLE tool ADD COLUMN cost_alert_threshold DECIMAL(10,2) NULL",
        "ALTER TABLE tool MODIFY COLUMN cost_alert_threshold DECIMAL(10,4) NULL",  # 0.001 级阈值精度
        "ALTER TABLE tool ADD COLUMN tags JSON NULL",
        "ALTER TABLE tool ADD COLUMN sort INT NOT NULL DEFAULT 0",
        "ALTER TABLE tool ADD COLUMN is_hot TINYINT(1) NOT NULL DEFAULT 0",
        "ALTER TABLE tool ADD COLUMN usage_count INT NOT NULL DEFAULT 0",
        "ALTER TABLE `order` ADD COLUMN payment_provider VARCHAR(16) NULL DEFAULT 'mock'",
        "ALTER TABLE `order` ADD COLUMN entity VARCHAR(16) NULL DEFAULT 'personal'",
        # 渠道活动折扣（营销对账归因）
        "ALTER TABLE `order` ADD COLUMN origin_amount DECIMAL(10,2) NULL",
        "ALTER TABLE `order` ADD COLUMN discount DECIMAL(3,2) NULL",
        "ALTER TABLE `order` ADD COLUMN campaign_name VARCHAR(64) NULL",
        # 管理员账号体系
        "ALTER TABLE admin_user ADD COLUMN name VARCHAR(64) NULL",
        "ALTER TABLE admin_user ADD COLUMN status VARCHAR(16) NULL DEFAULT 'active'",
        "ALTER TABLE admin_user ADD COLUMN last_login_at DATETIME NULL",
        "ALTER TABLE admin_user ADD COLUMN created_at DATETIME NULL",
        # 备案登记（§16.3-B）：模型维度为主，供应商维度作兜底
        "ALTER TABLE llm_model ADD COLUMN filing_name VARCHAR(128) NULL",
        "ALTER TABLE llm_model ADD COLUMN filing_no VARCHAR(128) NULL",
        "ALTER TABLE llm_model ADD COLUMN filing_source_url VARCHAR(255) NULL",
        "ALTER TABLE llm_model ADD COLUMN filing_checked_at DATETIME NULL",
        "ALTER TABLE llm_provider ADD COLUMN filing_subject VARCHAR(128) NULL",
        "ALTER TABLE llm_provider ADD COLUMN filing_source_url VARCHAR(255) NULL",
        "ALTER TABLE llm_provider ADD COLUMN filing_checked_at DATETIME NULL",
        # 调用扣费快照（对账）
        "ALTER TABLE tool_invocation ADD COLUMN unit_price_snapshot DECIMAL(10,4) NULL",
        "ALTER TABLE tool_invocation ADD COLUMN amount_charged DECIMAL(10,2) NULL",
        # 模型目录去冗余：角色归属只保留在 llm_routing.chain（同模型可被多条链引用）
        "ALTER TABLE llm_model DROP COLUMN role",
        # 交互式多轮会话（§6.5）：工具声明可含人工介入节点；新表由 create_all 建
        "ALTER TABLE tool ADD COLUMN interactive TINYINT(1) NOT NULL DEFAULT 0",
        "ALTER TABLE tool ADD COLUMN interaction_config JSON NULL",
        "ALTER TABLE tool ADD COLUMN analysis_prompt TEXT NULL",
        # 邀请有奖：邀请码（唯一，允许多个 NULL）
        "ALTER TABLE `user` ADD COLUMN invite_code VARCHAR(16) NULL",
        "ALTER TABLE `user` ADD UNIQUE INDEX uk_user_invite_code (invite_code)",
        # 渠道活动类型化：discount（充值优惠）/ invite（邀请有奖）/ checkin（签到奖励）
        "ALTER TABLE channel_campaign ADD COLUMN type VARCHAR(16) NOT NULL DEFAULT 'discount'",
        "ALTER TABLE channel_campaign ADD COLUMN config JSON NULL",
        # 反馈：管理员回复（用户端可见）
        "ALTER TABLE feedback ADD COLUMN reply TEXT NULL",
        "ALTER TABLE feedback ADD COLUMN replied_at DATETIME NULL",
        # 充值优惠「送次数」：订单快照额外赠送次数
        "ALTER TABLE `order` ADD COLUMN bonus_quota INT NULL DEFAULT 0",
        "ALTER TABLE channel_campaign MODIFY COLUMN discount DECIMAL(3,2) NULL",
        "ALTER TABLE channel_campaign ADD INDEX ix_campaign_type (type)",
        # 记录归档（用户手动 / 超期自动）
        "ALTER TABLE tool_invocation ADD COLUMN archived TINYINT(1) NOT NULL DEFAULT 0",
        "ALTER TABLE tool_invocation ADD COLUMN archived_at DATETIME NULL",
        "ALTER TABLE tool_invocation ADD INDEX ix_invocation_archived (archived)",
        # 分析 / 导出分开计价：price_quota=生成导出消耗，analyze_quota=分析消耗
        "ALTER TABLE tool ADD COLUMN analyze_quota INT NOT NULL DEFAULT 0",
        # 多渠道：渠道账号归属（channel_account 表由 create_all 建）
        "ALTER TABLE user_identity ADD COLUMN channel_account_id VARCHAR(32) NULL",
        "ALTER TABLE user_identity ADD INDEX ix_identity_account (channel_account_id)",
        "ALTER TABLE `order` ADD COLUMN channel_account_id VARCHAR(32) NULL",
        "ALTER TABLE `order` ADD INDEX ix_order_account (channel_account_id)",
        "ALTER TABLE transaction ADD COLUMN channel_account_id VARCHAR(32) NULL",
        "ALTER TABLE tool_invocation ADD COLUMN channel_account_id VARCHAR(32) NULL",
        "ALTER TABLE tool_invocation ADD INDEX ix_invocation_account (channel_account_id)",
        "ALTER TABLE tool_session ADD COLUMN channel_account_id VARCHAR(32) NULL",
        "ALTER TABLE tool_session ADD INDEX ix_session_account (channel_account_id)",
        # 渠道账号独立收款阈值（NULL = 用系统默认 100000 / 80000 / 90000）
        "ALTER TABLE channel_account ADD COLUMN month_limit INT NULL",
        "ALTER TABLE channel_account ADD COLUMN warn_threshold INT NULL",
        "ALTER TABLE channel_account ADD COLUMN stop_threshold INT NULL",
        # 邀请两段式奖励：绑定发放 / 首充发放
        "ALTER TABLE invite ADD COLUMN bind_quota INT NULL DEFAULT 0",
        "ALTER TABLE invite ADD COLUMN recharge_quota INT NULL DEFAULT 0",
        "ALTER TABLE invite ADD COLUMN bind_rewarded_at DATETIME NULL",
        # 多工具会话：一个会话含多个子任务（session_turn 表由 create_all 建）
        "ALTER TABLE session_message ADD COLUMN turn_seq INT NULL",
        "ALTER TABLE session_message ADD INDEX ix_msg_session_turn (session_id, turn_seq)",
        "ALTER TABLE tool_session ADD COLUMN turn_count INT NOT NULL DEFAULT 0",
        "ALTER TABLE tool_session ADD COLUMN active_turn_id BIGINT NULL",
        # 工具分类字典（后台可维护名称/排序/启用；新表也由 create_all 建）
        "CREATE TABLE IF NOT EXISTS tool_category ("
        "`key` VARCHAR(32) NOT NULL, name VARCHAR(32) NOT NULL, sort INT DEFAULT 0, "
        "enabled TINYINT(1) DEFAULT 1, PRIMARY KEY (`key`))",
        # 渠道账户独立虚拟支付配置（多小程序各一套 OfferID/AppKey/推送密钥）
        "ALTER TABLE channel_account ADD COLUMN virtual_pay JSON NULL",
        # 工具可见渠道：空=全渠道（多渠道矩阵，每个小程序可有自己的工具集）
        "ALTER TABLE tool ADD COLUMN channel_ids JSON NULL",
    ]
    try:
        with engine.connect() as conn:
            for sql in alters:
                try:
                    conn.execute(text(sql))
                    conn.commit()
                except Exception:  # noqa: BLE001  列已存在则忽略
                    conn.rollback()
            # 工具分类字典：空表时注入默认分类（后台「工具分类」可维护）
            cnt = conn.execute(text("SELECT COUNT(*) FROM tool_category")).scalar()
            if cnt == 0:
                for i, (k, name) in enumerate(CATEGORY_NAMES.items()):
                    conn.execute(
                        text("INSERT INTO tool_category (`key`, name, sort, enabled) VALUES (:k, :n, :s, 1)"),
                        {"k": k, "n": name, "s": (i + 1) * 10},
                    )
                conn.commit()
            # 运营日统计：仅当聚合表为空时做一次性历史回填（此后由 worker 实时累加）
            cnt = conn.execute(text("SELECT COUNT(*) FROM tool_stats_daily")).scalar()
            if cnt == 0:
                conn.execute(text(
                    "INSERT INTO tool_stats_daily (stat_date, tool_id, calls, success, failed, "
                    "quota_charged, amount_charged, llm_cost, total_seconds) "
                    "SELECT DATE(CONVERT_TZ(created_at, '+00:00', '+08:00')), tool_id, COUNT(*), "
                    "SUM(status='success'), SUM(status='failed'), SUM(quota_charged), SUM(amount_charged), "
                    "SUM(cost_amount), COALESCE(SUM(IF(status='success' AND finished_at IS NOT NULL, "
                    "TIMESTAMPDIFF(SECOND, created_at, finished_at), 0)), 0) "
                    "FROM tool_invocation GROUP BY DATE(CONVERT_TZ(created_at, '+00:00', '+08:00')), tool_id"
                ))
            # 存量微信身份回填到 user_identity（幂等）
            conn.execute(text(
                "INSERT INTO user_identity (user_id, channel, channel_uid) "
                "SELECT u.id, 'wechat', u.openid FROM `user` u "
                "WHERE u.openid IS NOT NULL AND NOT EXISTS ("
                "SELECT 1 FROM user_identity i WHERE i.channel='wechat' AND i.channel_uid=u.openid)"
            ))
            conn.commit()
            # 存量余额迁移到配额台账（余额真源改为 quota_grant）；幂等
            conn.execute(text(
                "INSERT INTO quota_grant (user_id, source, quota, remaining, ref_id, created_at) "
                "SELECT a.user_id, 'migrate', a.quota_total - a.quota_used, "
                "       a.quota_total - a.quota_used, 'migrate', NOW() "
                "FROM account a "
                "WHERE a.quota_total - a.quota_used > 0 "
                "  AND NOT EXISTS (SELECT 1 FROM quota_grant g WHERE g.user_id = a.user_id)"
            ))
            conn.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[db] migrate warning: {e}")

    # 交互式工具默认开启「优化前后对比」（pptx 视觉重排不适用，显式关闭）；seed 不会覆盖已有行
    from app.models import Tool as _Tool

    db = SessionLocal()
    try:
        for t in db.query(_Tool).filter(_Tool.interactive == True).all():  # noqa: E712
            cfg = dict(t.interaction_config or {})
            if "diff" not in cfg:
                cfg["diff"] = t.id != "pptx_optimize"
                t.interaction_config = cfg
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print(f"[db] migrate diff flag warning: {e}")
    finally:
        db.close()

    # 存量用户补邀请码（幂等）
    from app.core.ids import gen_invite_code
    from app.models import User as _User

    db = SessionLocal()
    try:
        used = {
            c
            for (c,) in db.query(_User.invite_code).filter(_User.invite_code.isnot(None)).all()
        }
        changed = False
        for u in db.query(_User).filter(_User.invite_code.is_(None)).all():
            code = gen_invite_code()
            while code in used:
                code = gen_invite_code()
            used.add(code)
            u.invite_code = code
            changed = True
        if changed:
            db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print(f"[db] migrate invite_code warning: {e}")
    finally:
        db.close()

    # 历史会话补建调用记录（「会话开始时即建记录」这个改动之前的数据）
    from app.models import ToolInvocation as _TI
    from app.models import ToolSession as _TS

    status_map = {
        "success": "success",
        "failed": "failed",
        "cancelled": "cancelled",
        "expired": "expired",
    }
    db = SessionLocal()
    try:
        legacy = db.query(_TS).filter(_TS.invocation_id.is_(None)).all()
        for s in legacy:
            inv = _TI(
                user_id=s.user_id,
                tool_id=s.tool_id,
                input_text=s.input_text,
                attachment_ids=s.attachment_ids,
                intent_confidence=1.0,
                status=status_map.get(s.status, "running"),
                quota_charged=int(s.quota_price or 0) if s.charged else 0,
                amount_charged=s.amount_charged or 0,
                cost_amount=s.llm_cost or 0,
                created_at=s.created_at,
                finished_at=s.updated_at,
                err=s.err,
            )
            db.add(inv)
            db.flush()
            s.invocation_id = inv.id
        if legacy:
            db.commit()
            print(f"[db] backfilled {len(legacy)} invocation(s) for legacy session(s)")
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print(f"[db] migrate session->invocation warning: {e}")
    finally:
        db.close()

    # 多工具会话：把存量会话回填为「seq=1 的子任务」（幂等）。
    # 旧会话的 checkpoint 线程名是 str(sid)，新命名 {sid}:{seq} 不兼容，
    # 故把仍在进行中的残留会话结束掉（正常应无，仅兜底）。
    from sqlalchemy import text as _text_turn

    from app.models import SessionTurn as _ST

    db = SessionLocal()
    try:
        # 只处理「还没有任何子任务」的存量会话：这些是新模型之前的数据。
        # ⚠️ 必须用 `s.id in have` 判断，否则每次启动都会误杀正在等用户回答的新会话。
        have = {sid for (sid,) in db.query(_ST.session_id).distinct().all()}
        created = 0
        closed = 0
        for s in db.query(_TS).all():
            if s.id in have:
                continue
            # 存量会话的 checkpoint 线程名是 str(sid)，与新命名 {sid}:{seq} 不兼容：
            # 仍在进行中的一律结束，避免用错的线程恢复出诡异状态。
            if s.status in ("running", "awaiting_answer", "awaiting_confirm"):
                s.status = "cancelled"
                s.err = s.err or "迁移：多子任务改造，旧会话已结束"
                closed += 1
            db.add(
                _ST(
                    session_id=s.id,
                    seq=1,
                    tool_id=s.tool_id,
                    status=s.status,
                    input_text=s.input_text,
                    attachment_ids=s.attachment_ids,
                    attachment_text=s.attachment_text,
                    analysis=s.analysis,
                    facts=s.facts,
                    current_question=s.current_question,
                    round_count=s.round_count or 0,
                    max_rounds=s.max_rounds or 5,
                    llm_cost=s.llm_cost or 0,
                    quota_price=s.quota_price,
                    unit_price_snapshot=s.unit_price_snapshot,
                    amount_charged=s.amount_charged,
                    charged=bool(s.charged),
                    invocation_id=s.invocation_id,
                    step_started_at=s.step_started_at,
                    expires_at=s.expires_at,
                    finished_at=s.updated_at,
                    created_at=s.created_at,
                )
            )
            created += 1
        if created:
            db.commit()
            print(f"[db] backfilled {created} session_turn(s) (seq=1), closed {closed} legacy in-flight")
        elif closed:
            db.commit()

        # 容器镜像列归位（自愈，幂等）：turn_count / active_turn_id
        db.execute(
            _text_turn(
                "UPDATE tool_session s SET turn_count="
                "(SELECT COUNT(*) FROM session_turn t WHERE t.session_id=s.id)"
            )
        )
        db.execute(
            _text_turn(
                "UPDATE tool_session s SET active_turn_id="
                "(SELECT t.id FROM session_turn t WHERE t.session_id=s.id "
                " AND t.status IN ('running','awaiting_answer','awaiting_confirm') "
                " ORDER BY t.seq DESC LIMIT 1)"
            )
        )
        db.execute(_text_turn("UPDATE session_message SET turn_seq=1 WHERE turn_seq IS NULL"))
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print(f"[db] migrate session_turn warning: {e}")
    finally:
        db.close()

    # 多渠道：建默认渠道账号，并把存量数据归到它名下（幂等）
    from app.models import ChannelAccount as _CA

    db = SessionLocal()
    try:
        acc = db.get(_CA, "default")
        if acc is None:
            acc = _CA(
                id="default",
                type="wechat_mp",
                name="默认渠道（微信小程序）",
                # 凭据（appid/secret）请部署后在后台「渠道账号」填写；未填前该渠道登录走 mock_openid
                enabled=True,
                remark="自动创建：承载存量数据；可改名或另建其它渠道账号",
            )
            db.add(acc)
            db.commit()
        from sqlalchemy import text as _text2

        for sql in (
            "UPDATE user_identity SET channel_account_id=:a WHERE channel_account_id IS NULL AND channel='wechat'",
            "UPDATE `order` SET channel_account_id=:a WHERE channel_account_id IS NULL",
            "UPDATE transaction SET channel_account_id=:a WHERE channel_account_id IS NULL",
            "UPDATE tool_invocation SET channel_account_id=:a WHERE channel_account_id IS NULL",
            "UPDATE tool_session SET channel_account_id=:a WHERE channel_account_id IS NULL",
        ):
            db.execute(_text2(sql), {"a": acc.id})
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print(f"[db] migrate channel_account warning: {e}")
    finally:
        db.close()


def _migrate_points() -> None:
    """次数→积分（1 次 = 10 积分）一次性迁移；幂等（app_setting 标记）。

    范围：工具定价（price_quota/analyze_quota）、存量额度（quota_grant）、
    充值套餐（quota ×10 + 名称改「N积分包」）、活动奖励（签到/邀请的配额）。
    不动：账户余额（元）、订单历史、邀请人数上限（cap 是人数不是配额）。
    """
    from sqlalchemy import text

    with engine.connect() as c:
        marked = c.execute(
            text("SELECT value FROM app_setting WHERE `key`='migration_points_v1'")
        ).scalar()
        if marked and str(marked) not in ("0", ""):
            return
        c.execute(text("UPDATE tool SET price_quota=price_quota*10, analyze_quota=analyze_quota*10"))
        c.execute(text("UPDATE quota_grant SET quota=quota*10, remaining=remaining*10"))
        c.execute(text("UPDATE recharge_package SET quota=quota*10"))
        c.execute(text("UPDATE recharge_package SET name=CONCAT(quota,'积分包') WHERE name REGEXP '^[0-9]+次包$'"))
        # 活动奖励配置：签到 quota、邀请 reward_quota（人数上限/cap 与百分比不动）
        c.execute(text(
            "UPDATE channel_campaign SET config=JSON_SET(config,'$.quota',"
            "JSON_EXTRACT(config,'$.quota')*10) WHERE type='checkin' AND JSON_EXTRACT(config,'$.quota') IS NOT NULL"
        ))
        c.execute(text(
            "UPDATE channel_campaign SET config=JSON_SET(config,'$.reward_quota',"
            "JSON_EXTRACT(config,'$.reward_quota')*10) WHERE type='invite' AND JSON_EXTRACT(config,'$.reward_quota') IS NOT NULL"
        ))
        c.execute(text(
            "INSERT INTO app_setting(`key`,value,updated_at) VALUES('migration_points_v1','1',NOW()) "
            "ON DUPLICATE KEY UPDATE value='1'"
        ))
        c.commit()
        print("[db] points migration done: 1 次 = 10 积分（quota_grant/tool/recharge_package/campaign ×10）")


def _migrate_analyze_fee() -> None:
    """工具分析费统一定价 1 积分（一次性迁移；幂等，app_setting 标记）。

    此前 analyze_quota 全量默认 0（分析不扣费）；按运营要求统一定为 1。
    只跑一次：之后后台按工具单独调价不会被本迁移覆盖。"""
    from sqlalchemy import text

    with engine.connect() as c:
        marked = c.execute(
            text("SELECT value FROM app_setting WHERE `key`='migration_analyze_fee_v1'")
        ).scalar()
        if marked and str(marked) not in ("0", ""):
            return
        c.execute(text("UPDATE tool SET analyze_quota=1"))
        c.execute(text(
            "INSERT INTO app_setting(`key`,value,updated_at) VALUES('migration_analyze_fee_v1','1',NOW()) "
            "ON DUPLICATE KEY UPDATE value='1'"
        ))
        c.commit()
        print("[db] analyze fee migration done: all tools analyze_quota=1")


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate()
    _migrate_points()
    _migrate_analyze_fee()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
