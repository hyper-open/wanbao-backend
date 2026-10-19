import json

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    AdminUser,
    LLMModel,
    LLMProvider,
    LLMRouting,
    RechargePackage,
    Tool,
)

# 供应商接口协议：网关目前只实现 OpenAI 兼容客户端（core/llm/gateway.py: _client），
# 所以当前所有供应商都填它；将来接入生图/生视频等非兼容供应商时才需扩展。
PROTOCOL_OPENAI = "openai-compatible"

# 专项免责声明：法律/医疗/心理/运动/税务/保险/学术诚信类工具（C 端详情页横幅展示）。
# 幂等补挂：工具存在且无免责时写入，不覆盖后台人工修改。
DISCLAIMER_MAP = {
    "contract_review": (
        "本工具由 AI 自动分析，仅供个人学习与内部参考，不构成法律意见或律师服务。"
        "签署重要合同前请咨询执业律师；请勿提交涉及国家秘密、商业秘密或他人隐私的内容，敏感信息请先脱敏。"
    ),
    "thesis_polish": (
        "本工具仅用于语言表达润色，不提供内容代写；请遵守所在学校/机构的学术规范，"
        "论文的选题、观点与数据须由您本人完成，并对其真实性负责。"
    ),
    "ortho_teeth": (
        "本工具内容为 AI 生成的健康科普信息，不构成诊疗建议或医疗方案；"
        "牙齿矫正存在个体差异，请以正规医疗机构执业医师的当面诊断为准。"
    ),
    "sleep_tips": (
        "本工具内容为 AI 生成的健康科普信息，不构成诊疗建议；"
        "若长期严重失眠或伴随其他不适，请及时就医。"
    ),
    "stress_relief": (
        "本工具内容为 AI 生成的情绪调节建议，不替代心理咨询或治疗；"
        "若您正处于严重心理困扰或有伤害自己的念头，请立即联系专业机构或心理援助热线。"
    ),
    "posture_advice": (
        "本工具内容为 AI 生成的运动健康建议，不构成诊疗意见；"
        "如有疼痛、伤病或基础疾病，请先咨询医生并在专业指导下锻炼。"
    ),
    "fitness_plan": (
        "本工具生成的训练计划仅供参考；请循序渐进量力而行，"
        "如有心血管疾病、伤病等健康问题，开始训练前请咨询医生。"
    ),
    "personal_tax": (
        "本工具内容基于 AI 生成，税收政策可能变化，具体扣除项目与金额以税务机关官方规定及您的实际情况为准；"
        "本内容不构成税务意见，重要申报请咨询专业人士。"
    ),
    "insurance_basic": (
        "本工具内容为 AI 生成的保险常识科普，不构成投保建议；"
        "具体保障责任与免责条款以保险合同为准，投保前请仔细阅读产品条款。"
    ),
    "budget_plan": (
        "本工具生成的预算方案仅供参考，不构成投资理财建议；"
        "涉及具体金融产品的决策请咨询持牌专业机构。"
    ),
}

PROVIDERS = [
    ("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "YOUR_DEEPSEEK_KEY", 1),
    ("qwen", "Qwen(通义)", "https://dashscope.aliyuncs.com/compatible-mode/v1", "YOUR_QWEN_KEY", 2),
    ("glm", "GLM(智谱)", "https://open.bigmodel.cn/api/paas/v4", "YOUR_GLM_KEY", 3),
    ("hy", "Hunyuan(腾讯)", "https://api.hunyuan.cloud.tencent.com/v1", "YOUR_HY_KEY", 4),
]


def _model_for(pid: str, role: str) -> str:
    """按供应商 id 预设默认模型名（仅首次 seed 使用）。"""
    if pid == "deepseek":
        return "deepseek-chat"
    if pid == "qwen":
        return "qwen-turbo" if role == "classifier" else "qwen-plus"
    if pid == "glm":
        return "glm-4-flash" if role == "classifier" else "glm-4-plus"
    if pid == "hy":
        return "hunyuan-lite" if role == "classifier" else "hunyuan-standard"
    return "default"


def seed(db: Session) -> None:
    if db.query(LLMProvider).count() == 0:
        for pid, name, url, key, pri in PROVIDERS:
            db.add(
                LLMProvider(
                    id=pid, name=name, type=PROTOCOL_OPENAI,
                    base_url=url, api_key=key, priority=pri, enabled=True,
                )
            )
        db.commit()

        classifier_chain, executor_chain = [], []
        for pid, name, url, key, pri in PROVIDERS:
            # 模型不带 role：归属由下面的 chain 决定，同一模型可被多条链引用
            c = LLMModel(
                provider_id=pid, model_actual=_model_for(pid, "classifier"),
                alias="cheap", tier="cost", price_per_1k=0.001,
            )
            e = LLMModel(
                provider_id=pid, model_actual=_model_for(pid, "executor"),
                alias="strong", tier="quality", price_per_1k=0.01,
            )
            db.add(c)
            db.add(e)
            db.commit()
            db.refresh(c)
            db.refresh(e)
            classifier_chain.append({"provider_id": pid, "model_id": c.id})
            executor_chain.append({"provider_id": pid, "model_id": e.id})

        db.add(LLMRouting(role="classifier", chain=classifier_chain))
        db.add(LLMRouting(role="executor", chain=executor_chain))
        db.commit()

    if db.query(RechargePackage).count() == 0:
        pkgs = [
            ("10次包", 10, 9.9, 11.2, "新手尝鲜"),
            ("30次包", 30, 24.9, 28.3, "推荐"),
            ("100次包", 100, 69.0, 78.4, "重度用户"),
        ]
        for i, (name, quota, price, pios, desc) in enumerate(pkgs):
            db.add(
                RechargePackage(
                    name=name, quota=quota, price=price, price_ios=pios,
                    description=desc, sort=i, enabled=True,
                )
            )
        db.commit()

    # 工具：从 seed_tools.json 幂等补种（开源仓库只随附精选示例集；
    # 全量工具库为私有资产，不在开源范围。字段与 Tool 模型列一一对应。）
    if db.query(Tool).count() == 0:
        import os

        tools_file = os.path.join(os.path.dirname(__file__), "seed_tools.json")
        with open(tools_file, encoding="utf-8") as f:
            curated = json.load(f)
        for row in curated:
            db.add(Tool(**{k: v for k, v in row.items() if v is not None}))
        db.commit()
        print(f"[seed] tools added: {len(curated)}")

    # 精选集内带文档导出的工具挂 docx 执行器
    export_ids = [t["id"] for t in curated if t.get("executor") == "docx_export"]
    if export_ids:
        n_docx = (
            db.query(Tool)
            .filter(Tool.id.in_(export_ids), Tool.executor.is_(None))
            .update({"executor": "docx_export"}, synchronize_session=False)
        )
        db.commit()
        print(f"[seed] docx_export executor assigned: {n_docx}")


    # 初始管理员账号（仅在完全没有账号时创建，避免覆盖已改密码）
    if db.query(AdminUser).count() == 0:
        from app.services.admin_auth import hash_password

        db.add(
            AdminUser(
                username=settings.ADMIN_USERNAME,
                password_hash=hash_password(settings.ADMIN_PASSWORD),
                name="超级管理员",
                role="admin",
                status="active",
            )
        )
        db.commit()
        print(f"[seed] 已创建管理员账号 {settings.ADMIN_USERNAME}（初始密码见 ADMIN_PASSWORD 配置，请登录后立即修改）")

    print("[seed] done")
