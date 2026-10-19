# 万宝工具箱 · 系统设计方案（v0.3 已纳入首批决策）

> 状态：v0.3。已纳入决策：Python 后端；LLM 多供应商+降级路由；附件(前端单文件≤10MB/后端多文件)；档位后台配置；首期纯文本；账户=openid+手机号。
> v0.3 变更：新增 §7.3 模型能力路由（多模态预留）、§16 结构性缺口设计（工具发现/主体升级/AIGC 合规/成本定价）；§5.3/§5.4 补充 `capabilities` 字段；§4 数据库更正为复用已有 MySQL。
> 仍有少量【待确认】项见第 14 节。

---

## 1. 产品定位

**一个入口，N 个 AI 工具。** 用户只描述需求（文本 + 可选附件），Agent 识别意图并调度对应工具，按次扣减次数。核心资产是 **Tool Registry** 与 **Agent 调度**，加功能 = 加工具配置（或加代码扩展点），不改动主链路。

首期工具：纯文本类（文案改写、周报生成、年终述职稿、翻译润色、简历内容优化-纯文本输入……）。后续：轻解析工具（docx/pdf 抽文本后优化）、重活工具（PPT 视觉重排，后期）。

---

## 2. 关键约束（微信个人虚拟支付，已查证 2026-09）

| 项 | 约束 | 对设计的影响 |
|---|---|---|
| 主体 | 个人 + 大陆身份证 + 小程序认证 + 备案 | 一人一号，月限 10 万 |
| 月支付限额 | 全终端 **10 万/月** | 监控告警；放大需升级公司主体 |
| 费率(安卓) | **1%**，T+3 结算 | 定价留空间 |
| 费率(iOS) | 走 **Apple 支付，佣金 12%**，45–60 天结算 | iOS 档位单独上浮对冲 |
| 虚拟商品 | 购买次数/解锁/订阅/付费内容 | 我们的形态 = **购买使用次数** |
| 退款 | 个人支付原路退受限 | 争议补偿走**余额返还** |
| 接入 | 官方提供 Agent 自动接入 SKILL | 支付接入可半自动 |

**计费结论**：余额/次数包——先充值买次数，每次使用扣一次。

---

## 3. 总体架构

```
微信小程序端（独立仓库 miniprogram）: 文本输入 · 附件上传(单文件≤10MB) · 意图识别 · 结果/历史 · 余额/次数 · 支付
        │ HTTPS (登录态: openid + Bearer token)
自建后端 API (FastAPI): 鉴权 · 限流 · 安全审核 · 附件校验 · 日志
        │
   ┌────┴────────┬──────────────┬──────────────┐
   │ Agent 调度  │ 账户计费     │ 工具执行     │
   │ 意图识别    │ 用户/余额    │ Tool Registry│
   │ 路由/澄清   │ 订单/流水    │ + Executor   │
   │ 执行前确认  │ 对账         │ 附件解析队列│
   └────┬────────┴──────┬───────┴──────┬───────┘
        │               │             │
管理后台 Web(static/admin/index.html, 同容器托管): 工具/档位/LLM 供应商·模型·路由 CRUD · 管理员账号密码登录（Bearer JWT）鉴权
        │ HTTPS
基础设施: 复用已有 MySQL · Redis · 对象存储(rustfs/S3) · 消息队列(Redis list)
          LLM 网关(多供应商+降级路由) · 内容安全(msgSecCheck)
```

**部署**：Docker 容器化，单台/多台 ECS，Nginx 前置，HTTPS 接微信。重逻辑全在服务端；解析/执行走异步队列。

---

## 4. 技术栈选型（已定：Python）

| 层 | 选型 | 备注 |
|---|---|---|
| 后端框架 | **Python + FastAPI** | 异步、LLM/解析生态最强；Pydantic 做 schema 校验 |
| 解析库 | `python-docx` / `python-pptx` / `pdfplumber`+`PyPDF` | 文本抽取；OCR 用 `paddleocr`/`easyocr`（扫描件，后期） |
| 数据库 | **MySQL**（复用已有实例） + **Redis** | 计费事务强一致；Redis 做会话/限流/缓存/队列 |
| 对象存储 | 腾讯云 COS / 阿里 OSS / S3 | 附件存储 |
| 消息队列 | Redis Stream / Celery+Redis | 附件解析、异步工具执行、通知 |
| LLM 网关 | 自建统一网关（见第 7 节） | 多供应商、降级路由、按角色选模型 |
| 内容安全 | 微信 msgSecCheck + 附件扫描 | 个人主体审核更严 |
| 管理后台 | **FastAPI 同容器静态托管单页**（`static/admin/index.html`，原生 HTML+JS，零构建链），管理员账号密码登录（Bearer JWT）鉴权，`GET /admin` 访问 | 随 Docker 一起部署，不与小程序耦合 |
| 部署 | Docker + ECS，CI 构建 | 轻量起手 |

---

## 5. 核心数据模型

### 5.1 用户与账户
- **user**
  - `id`, `openid`(uniq), `unionid`(nullable), `phone`(nullable, 绑定后), `phone_verified`(bool), `nickname`, `avatar`, `created_at`, `status`(normal/ban)
- **account**（每用户一行）
  - `user_id`(PK/FK), `balance`(DECIMAL), `quota_total`(INT), `quota_used`(INT), `updated_at`
  - 内部以"次数"为消费单位，余额仅用于展示/折算。

绑定策略：**openid 登录即建户**；**手机号在首次充值前绑定**（微信手机号授权按钮；个人主体若不可用，降级为短信验证码绑定——【待确认】可用性，见 14 节）。

### 5.2 订单与流水
- **order**：`id`(订单号), `user_id`, `package_id`, `amount`, `platform`(android/ios), `channel`(wechat/apple), `status`(pending/paid/closed/refunded), `prepay_params`(JSON), `paid_at`, `created_at`
- **transaction**：`id`, `user_id`, `type`(recharge/consume/refund/adjust), `amount`(+/-), `quota_delta`(INT), `ref_id`, `remark`, `created_at`
  - 强一致：充值 order.paid→transaction(+次数)→账户更新，全部 DB 事务。

### 5.3 工具与调用
- **tool**：`id`(slug), `name`, `category`, `description`, `input_schema`(JSON), `prompt_template`, `output_template`, `executor`(nullable 代码扩展点标识), `price_quota`(INT, 默认1), `model_preference`(nullable, 见 7), `needs_capabilities`(JSON, 默认 `["text"]`, 见 7.3), `enabled`, `version`, `created_at`, `updated_at`
  - `input_schema.attachment`: `{ allowed_formats:[...], max_size_mb, max_count }`（后端按此校验；前端 UI 当前仅渲染单文件，但接口支持多文件）
- **tool_invocation**：`id`, `user_id`, `tool_id`, `tool_version`, `input_text`, `attachment_ids`(JSON), `intent_confidence`(FLOAT), `model_used`(记录实际命中模型), `status`, `quota_charged`, `result_ref`, `err`, `created_at`, `finished_at`
- **attachment**：`id`, `user_id`, `tool_invocation_id`(nullable), `object_key`, `filename`, `mime`, `size`, `status`(uploaded/parsed/failed), `parsed_meta`(JSON), `created_at`

### 5.4 LLM 网关配置（多供应商+降级路由，见第 7 节）
- **llm_provider**：`id`, `name`, `type`(**接口协议**，adapter 选择器；当前仅 `openai-compatible`——网关统一用 OpenAI 兼容客户端调用，接入生图/生视频等非兼容供应商时才按此分 adapter), `base_url`, `api_key`(加密存储), `priority`(INT), `enabled`, `health_status`(healthy/degraded/down), `timeout_ms`
- **llm_model**：`id`, `provider_id`(FK), `model_actual`(厂商模型名), `alias`(如 `strong`/`cheap`), `tier`(cost/quality，**目前仅作标识**、不参与路由决策), `price_per_1k`(DECIMAL), `enabled`。模型**不带 role**——角色归属只存在于 `llm_routing.chain`，同一模型可被多条链引用（配一次即可多角色复用，也避免重复记录导致的配置漂移与公示重复）。`capabilities`(JSON 对象, **已实现**)：能力标签 `{"vision","function_call","audio_in","tts"}`，网关按请求需要过滤（含图→vision、工具调用→function_call）；`supports_vision`(BOOL) 为兼容回落字段。能力子链见 §7.3.4
- **llm_routing**：`id`, `role`(路由槽位, 见 7.3: classifier/executor/embedding/image_gen/video_gen/...), `chain`(JSON 有序数组: `[{provider_id, model_id}, ...]`)，全局降级链；工具 `model_preference` 可覆盖起点。

### 5.5 档位与管理
- **recharge_package**（后台可配置）：`id`, `name`, `quota`(次数), `price`(¥), `price_ios`(¥, 上浮对冲12%), `desc`, `sort`, `enabled`
- **admin_user**：`role`(super/operator), 密码哈希/SSO
- **audit_log**：内容审核命中记录与处理动作
- **config_log**：工具/定价/路由变更历史，便于回滚

---

## 6. Agent 意图识别设计

核心原则：**先识别、再确认、后执行、必可解释**。未确认前绝不扣费。

```
用户输入(文本+附件)
  → 安全审核(msgSecCheck)        失败→拦截
  → 附件校验(格式/大小/数量, 后端按 tool.input_schema)  失败→拦截
  → 候选工具召回(类目/关键词/embedding 粗排)
  → LLM 分类(classifier 角色模型): 候选 schema → {tool_id, confidence, 提取参数}
  → 置信度分支:
       ≥0.85 → 执行前确认
       0.6–0.85 → 追问澄清
       <0.6 → 展示工具菜单
  → 确认卡片: 工具名 + 消耗次数 + 输入摘要 + [确认][换工具]
  → 确认 → 扣次 + 执行(executor 角色模型) → 返回结果
```

- 工具 schema 喂给 LLM 做选择（id/name/when/needs_attachment/attachment_formats/price_quota）。
- 低置信不盲扣；菜单兜底保证找对工具。
- 调用失败且已扣次 → 自动退次 + 流水 `refund`。

### 6.5 Agent 架构模式（明确决策）
- **形态**：单 **Router（调度器）+ 工具 Worker**，**非多 Agent 集群**、非纯 ReAct 自主循环。
- **路由（Router）**：`embedding 召回 top-K 候选` + `classifier 模型 function-calling` 从候选中选 1 并提取参数。工具再多也不撑爆上下文（候选先收缩到 top-K）。
- **执行（Worker）**：**所有工具统一走 LangGraph 执行框架**（自 M1 引入，为后续大量复杂工具预留扩展）：
  - 简单工具 = 单节点图（直接调 LLM + `prompt_template`）。
  - 复杂工具 = 多步状态图（抽取→多步优化→重组，可含分支/重试/人工介入节点）。**「人工介入节点」已实现**，见 §17。
  - 每个工具的图通过代码 `executor` 扩展点注册；纯配置工具由框架自动生成单节点图。
- **为何不用多 Agent / 纯 ReAct**：按次付费要求计费边界清晰、可审计、低成本低延迟；自由多步推理易误扣、难审计。复杂推理被收敛在单个工具图内部，不泄露到调度层。
- **LangGraph 仅用于执行层，不用于路由。**

#### 替代方案评估（已否决）

**A. Plan-and-Execute（外层规划）+ ReAct（内层执行）**
- 适用场景：用户单请求天然跨多工具协作的"复合任务"（如"改周报为述职风并生成 PPT 大纲"）。
- 否决理由：① 计费边界崩塌——动态规划出多步时"扣几次/确认几次"与我们的"按次明确扣费+执行前确认"冲突；② 95% 请求是单工具，每次多跑 planner 纯增延迟/成本；③ 开放 ReAct 步数不固定→单次成本不可预测，对按次付费是硬伤；④ 多步失败归因/对账更难。
- 现状已含"规划-执行"分离：Router 即规划（选 1 工具+抽参），Worker 即执行（LangGraph 工作流），只是规划只到"选 1 工具"层，匹配单工具明码计费。
- 未来若复合任务成主流：建模为**单一复合工具（内部 LangGraph 图串子步骤，确定性工作流）**或**有步数上限的 planner（先出固定计划供确认再执行）**，不采用运行时自由规划。

**B. 低代码平台（Dify 等）替代自研 Agent 编排**
- Dify 能覆盖：多模型供应商+降级、工作流/Agent 编排、意图识别/工具选择（即"LLM 编排"这一层）。
- 但本产品的真正主体 Dify **完全不覆盖、必须自研**：余额/次数包与按次扣费、退次、微信虚拟支付下单/回调/Apple IAP、月限 10 万监控停售、Tool Registry（prompt+附件约束+价格+模型且绑定计费）、PII/内容安全留存。
- 否决理由：① 钱路径（计费/支付/账户/微信/可计价注册表）是产品命脉，Dify 一点都帮不上，全须自研；② 若用 Dify 跑执行图，仍需在其外裹一层我们的计费胶水（Registry+流水账），结果是"我们的栈 + Dify 栈 + 胶水"，比薄层 LangGraph 更重；③ 计费可控性——Dify 把执行变黑盒，成本/扣次对齐进自有流水需 hack，自研 LangGraph 每节点调用天然落账；④ 运维/合规——Dify 自部署是全栈（前端+后端+DB+向量库+worker），比单 FastAPI+LangGraph 容器重；云版则把简历/文档 PII 外送，不利合规。
- 公平补充：若产品仅是"AI 问答机器人带几个工具"，Dify 更快；混合方案（仅用 Dify 跑执行图、其余自研调其 API）可行但引入第二栈与成本对齐摩擦，MVP 不如直接自研薄层。本 Agent 代码量本就很小，大头是钱与用户体系，故自研合理。

---

## 7. LLM 网关与多供应商降级路由（新增）

**目标**：不绑定单一厂商；配置多个供应商/模型；某供应商限流/超时/故障时自动降级到下一节点；按角色（分类 vs 执行）选不同模型档位。

### 7.1 配置结构
- 多个 `llm_provider`（各自 base_url/api_key/优先级/超时/健康状态）。
- 每个 provider 下多个 `llm_model`，标注 `role`（classifier 用便宜快模型，executor 用强模型）与 `tier`。
- `llm_routing` 定义每个 role 的**有序降级链** `chain`。
- **初始接入厂商（均经后台配置，不写死）**：deepseek、qwen(通义)、glm(智谱)、hy(腾讯混元)。每家配 `classifier` 与 `executor` 模型及降级顺序，全部在管理后台维护；新增厂商只需后台加一条 `llm_provider`。

### 7.2 路由逻辑
```
请求(model role, 可选 tool.model_preference):
  candidates = tool.model_preference ? [preference]+global_chain : global_chain[role]
  for (provider, model) in candidates:
      if provider.health != down and model.enabled:
          try: call with timeout
          on success: 记录 model_used; return
          on timeout/限流/5xx/配额错: mark degraded; continue  # 降级
  all failed → 返回友好错误，不扣费
```
- **熔断/健康**：连续失败达阈值 → `health_status=down`，临时跳过；恢复探测后回 healthy。
- **分类与执行分离**：意图识别走 `classifier` 链（快/便宜）；工具执行走 `executor` 链（强）；各自独立降级。
- **成本可见**：每次调用记录 `model_used` + `price_per_1k` 估算成本，进 `tool_invocation` 与对账。
- **可后台热配**：增删供应商、调链顺序、开关模型，均经管理后台，无需发版。

### 7.3 模型能力路由（多模态预留，v0.3 新增）

**动机**：工具清单会演化到多模态——扫描件简历（vision 输入）、生图配图（image_gen）、视频成片（video_gen/tts/asr）。当前路由只有 classifier/executor 两个"角色"槽位，默认"文本进文本出"，无法表达"这个模型能看图/能生图"。

**设计原则**：Schema 先落地扩展点，等第一个多模态工具进清单时再接真实 API（避免为不存在的工具建 adapter）。

> **实现状态（2026-10-19）**：vision 图片输入与 function_call 过滤**已落地**——`llm_model.capabilities` + 网关按请求需要过滤 + 附件图片 data URL 旁路 + `executor.vision` 能力子链（§7.3.4）。image_gen/video_gen 的 adapter、`tool.needs_capabilities`、audio_in/tts 标签仍为预留。

#### 7.3.1 数据模型变更

1. `llm_model` 增 `capabilities`(JSON, 默认 `["text"]`)：`text / vision / image_gen / video_gen / asr / tts / embedding / function_calling`。`role` 字段保留，语义降级为"适合 classifier 还是 executor 档位"的建议值。
2. `tool` 增 `needs_capabilities`(JSON, 默认 `["text"]`)：工具声明执行所需能力；worker 调网关时携带。
3. `llm_routing.role` 泛化为**路由槽位**（unique key 不变，直接加行）：`classifier / executor / embedding / image_gen / video_gen / ...`，每个槽位一条独立降级链。`embedding` 槽位顺便收编现有 `EMBEDDING_*` 环境变量配置进后台。

#### 7.3.2 路由逻辑（在 §7.2 基础上加一层能力过滤）

```
请求(slot, required_capabilities, 可选 tool.model_preference):
  for (provider, model) in chain[slot]:
      if required_capabilities ⊄ model.capabilities: continue   # 能力不符直接跳过，不消耗降级预算
      adapter = ADAPTER_BY_CAPABILITY(首个所需能力):
          text/vision -> OpenAI 兼容 chat.completions（vision 即消息里带 image_url）
          image_gen   -> images.generate（各家 API 差异大，按 provider.type 分 adapter）
          video_gen   -> 异步任务型接口（提交 + 轮询，挂现有 Redis 队列）
      try: call; on success: 记录 model_used; return
      on 失败: 降级链继续（熔断逻辑同 §7.2）
  all failed → 友好错误，不扣费
```

#### 7.3.3 关键约束与坑

- **生图/生视频供应商 API 不是 OpenAI 兼容**：需要 adapter 层（`provider.type` 决定走哪个 adapter），不是加一条 chain 就完事——这是"等第一个真实工具再实现"的主要原因。
- **video_gen 是分钟级异步长任务**：必须走现有异步队列 + 前端轮询；架构已全量异步（M5），天然兼容，但 worker 需支持"提交后轮询回调"型任务而非单次 LLM 调用。
- **vision 输入改变附件语义**：扫描件 PDF/图片不再是"抽文本"，需在 `attachment` 解析层加旁路——原样（object_key/签名 URL）传给多模态模型；`input_schema.attachment.allowed_formats` 相应放开图片格式。
- **计费不引入新单位**：生成类工具单次成本高且差异大，仍由 `tool.price_quota` 承载（生图类建议 5–10 次/单），成本可见性靠 `model_used` + 各能力模型的价格字段（image/video 模型按"元/张、元/秒"记入 `price_per_1k` 同列即可，备注含义）。
- **内容安全外延**：生图/生视频输出同样要过审（msgSecCheck 仅文本）；图片安全需另接供应商审核或微信 imgSecCheck，纳入 §12。

#### 7.3.4 能力子链（已实现：不同优先级的实现方式）

同一角色下「视觉请求想要与文本请求不同的模型优先级」→ 用**能力子链**表达，而不是拆角色：

- 网关选链顺序：优先读 `{role}.{cap}` 路由行（如 `executor.vision`）；该行不存在或为空 → 回落主链并按能力过滤（= 未配置时的现状行为）
- `llm_routing.role` 扩长到 VARCHAR(32) 以容纳 `executor.audio_in` 等；后台路由页/模型勾选框数据驱动，自动出现子链（显示名「工具执行 · 视觉」）
- 安全网：子链节点仍须真实声明该能力，防止误配
- 一次请求需多种能力时：取第一种能力选链，其余能力继续过滤（当前产品 decide 为纯文本，实际不出现交集）


---

## 8. 工具 Registry 设计规范（配置 + 代码）

纯 LLM 类工具**后台零代码新增**；需解析/生成的工具**注册代码扩展点**。

### 8.1 配置驱动部分
```
tool = {
  id, name, category, description,
  input_schema: {
    text_fields: [{key,label,required,placeholder}],
    attachment: { allowed_formats:[...], max_size_mb, max_count }
  },
  prompt_template: "你是一名资深HR... 需求：{text} 附件摘要：{attachment_summary}",
  output_template: "{result}",
  executor: null,                 # 纯配置为 null
  price_quota: 1,
  model_preference: null          # 可选，指定 preferred 模型/组
  enabled: true
}
```

### 8.2 代码扩展点（executor = LangGraph 图）
每个工具的执行是一个 **LangGraph 状态图**，由代码层注册：
- 简单工具：框架自动生成单节点图（直接跑 `prompt_template`）。
- 复杂工具：注册图构建函数，返回多节点 `StateGraph`（prepare→多步优化→format），可含分支/重试/人工介入。
```python
def build_graph(tool: Tool) -> StateGraph:
    """返回该工具的 LangGraph 状态图，注册到 EXECUTOR_REGISTRY[tool.executor]"""
    ...
```
注册到代码层 `EXECUTOR_REGISTRY`；DB `tool.executor` 指向它。**配置与代码解耦**：换 prompt 不碰代码；加复杂工具只注册新图。

### 8.3 版本与灰度
`tool.version` + `config_log` 可追溯/回滚；新工具可先 `enabled=false` 灰度再放量。

---

## 9. 计费与账户体系（余额/次数包 + 手机号）

### 9.1 账户与绑定
- openid 登录即建 `user`+`account`，**手机号非强制前置**。
- **手机号绑定（MVP 验证项）**：优先用微信手机号授权按钮；MVP 阶段实测个人主体下是否可用。不可用则评估替代方案（短信验证码绑定 / 或不绑定仅以 openid 标识）。目标先跑通闭环，手机号作为可选增强（防刷/找回），非前置门槛。

### 9.2 充值流程（微信虚拟支付）
```
小程序: 选档位 → 后端 /order/create → 调微信下单 → 返回 prepay
小程序: requestPayment
微信: /notify 异步 → 校验签名 → order.paid → 事务: transaction(recharge,+次数) → 账户更新
(iOS 走 Apple IAP 收据校验)
```
- iOS 档位 `price_ios = ceil(price/(1-0.12))` 上浮对冲 12% 佣金。
- **月限 10 万（微信硬限额）**：个人主体虚拟支付全终端 **10 万/月硬上限，超额订单会被微信直接拒绝支付**。采用"临近停售"策略：监控累计已支付金额，达阈值（如 9 万）即**临时停售充值档位**并前端提示"本月额度将满，正在升级主体"，避免用户支付失败；升级公司主体后上限解除。**停售预约与主体升级技术预案见 §16.2。**
- **充值即真实收款，月限是收款流水上限（非余额上限）**：用户充值 ¥X 是一笔真实支付，微信扣 1%(安卓) 后 T+3 结算到你绑定账户；现金流上是真实收入（会计属预收，消费时结转）。月限额限制的是"收款总额"，与用户账号内剩余次数无关——只要充值就吃额度；**退款不释放已占额度**（原交易已计入），故腾额度只能靠停售放量或升级主体。
- **档位后台可配**：`recharge_package` 增删改、价格/次数/iOS 价，均经管理后台。

### 9.3 消费流程（防超卖）
```
确认执行:
  BEGIN; SELECT account ... FOR UPDATE;
  IF quota_remain < price_quota → 提示去充值
  UPDATE account SET quota_used += price_quota;
  INSERT transaction(consume, -price_quota, ref=invocation_id);
  COMMIT;
  → 执行工具 → 成功写 result；失败 ROLLBACK 消费 + refund 流水
```

### 9.4 退款/争议
- **两类"退"要区分**：
  1. **余额返还（账户内）**：把次数/余额加回用户账号，不涉及微信。用于执行失败、内容违规等平台主动补偿（`transaction` type=refund, +次数）。是主要补偿手段。
  2. **原路退款（退微信）**：把钱退给用户微信支付。个人虚拟支付下**受限且基本不可行**；即使可行也不释放月限额（原交易已计入）。
- **退款规则**：次数包性质，正常**不开放用户主动退款**；仅执行失败/违规由平台主动补偿（走余额返还）。强烈诉求原路退者，个人主体下难支持，引导升级主体后处理或线下协商。

### 9.5 多渠道身份与定价（v0.3 新增，已实现）

面向后续支付宝小程序等多渠道：

**统一账户 + 渠道身份**：
- `user` 为渠道无关的统一账户（余额/次数/订单/流水/反馈全按 `user_id` 挂，天然跨渠道）；
- `user_identity`：`user_id + channel(wechat/alipay/...) + channel_uid`，unique(channel, channel_uid)，一人可挂多条渠道身份；
- 登录 `POST /api/auth/login {channel, code}` 按渠道适配器换取平台标识（微信 jscode2session，其余渠道预留 OAuth），查/建身份 → 发**渠道无关 JWT**（含 user_id/channel/openid=channel_uid）；
- 兼容：`user.openid` 保留为微信 legacy 身份（改为可空），`user_id_by_openid` 先查 legacy 再查 identity 表，全部既有调用方零改动；存量数据启动时自动回填 identity；
- 跨渠道合并（微信+支付宝同一自然人）：依赖手机号绑定后合并，暂未实现（列 backlog）。

**多渠道 × 多工具定价**：
- 渠道可见性：`recharge_package.channels`(JSON，默认 `["wechat"]`)——档位可限定仅某渠道售卖；
- 档位渠道价：`recharge_package.channel_prices`(JSON 如 `{"alipay":8.8}`)，未配置回落 `price`/`price_ios`；
- 工具渠道价：`tool.channel_pricing`(JSON 如 `{"alipay":2}`)覆盖 `price_quota`；recognize 返回与 confirm 扣次均按 `effective_price_quota(tool, channel)` 生效；
- 溯源：`order.client_channel` 记录来源渠道；API 以 body/查询参数 `channel` 传入，默认 `wechat`；
- 生效函数集中在 `billing.py`（`effective_price_quota` / `effective_package_price` / `package_allows_channel`），管理后台档位/工具表单可直接编辑。
- **定价依据**：price_quota 不是拍脑袋——单工具成本核算与定价闭环（usage 采集/经营视图/防亏熔断/定价建议器）见 §16.4。

---

## 10. 附件处理

- **约束（已定）**：单文件 ≤ **10MB**；**前端 UI 仅渲染单文件**，但**后端接口支持多文件**（`input_schema.attachment.max_count` 由工具配置，后端硬校验）。
- **上传**：小程序直传对象存储（临时凭证）或经后端转发；后端落 `attachment`。
- **双校验**：前端限制（体验）+ 后端硬校验（安全，防绕过），含格式白名单按工具定。
- **解析**：重活（docx/pdf/pptx 抽文本）入**消息队列异步**，`attachment.status` 跟踪；当前 MVP 纯文本工具不涉及，预留。
- **图片输入旁路（已实现，2026-10）**：`image/*` 附件不抽文本——`session_turn.attachment_images` 缓存 object_key，请求时转 base64 data URL（单图 ≤6MB，超限跳过并附提示），与文本组装为 OpenAI vision 多模态 content（`text + image_url`）；一次性路径（runner/executor）与会话路径（session_runner/graph/base）均已接入；decide 追问决策保持纯文本省 token。
- **隐私**：含 PII，存储加密；提供"任务结束后 N 天自动删除"；用户可手动删。

---

## 11. 管理后台（Web）

功能清单：
1. **工具管理**：CRUD、prompt 编辑、附件约束、定价(次数)、`model_preference`、上下架、版本回滚。
2. **LLM 网关管理**：供应商增删、模型启停、`classifier`/`executor` 降级链编排、健康/熔断查看。
3. **档位管理**：`recharge_package` 增删改、安卓/iOS 价格、启用。
4. **调用数据**：各工具调用量/成功率/平均置信度/消耗次数/模型成本。
5. **订单/对账**：充值订单、流水、月限监控、iOS/安卓拆分。
6. **内容审核**：命中队列与处理。
7. **用户管理**：封禁、余额手动调整（补偿）、手机号查看。

> 增量规划交叉引用：工具发现运营（tags/类目/热度）见 §16.1；经营成本视图与防亏告警见 §16.4；反馈/举报处理见 §16.3；模型备案状态展示见 §16.3-B。

### 11.1 管理后台前端（已实现）

- **形态**：单文件原生 HTML+JS（`static/admin/index.html`），由 FastAPI 经 `GET /admin` 同容器托管，无独立构建链，随 `docker compose up -d` 一起部署。
- **鉴权**：管理员账号密码登录（`POST /api/admin/login` 换 Bearer JWT，存 localStorage）；所有 `/api/admin/*` 调用附带 `Authorization: Bearer <token>`。旧共享令牌 `X-Admin-Token` 已移除。
- **功能页签**：工具 / 充值档位 / LLM 供应商 / 模型 / 路由。工具编辑含 `prompt_template`、`price_quota`、`needs_attachment`、`async_exec`、`executor`、`enabled`；路由页可直接编辑有序降级链 JSON（`[{provider_id, model_id}]`）。
- **后端接口**：见 §11 上述清单，对应 `app/api/admin.py`（CRUD + 鉴权）。

### 11.2 微信小程序前端（已实现骨架）

工程目录：**独立仓库 `miniprogram`**（微信开发者工具导入该仓库根目录，`appid` 需替换为你自己的）：

```
miniprogram/
  app.js / app.json / app.wxss       # 入口：globalData.baseUrl、登录态
  project.config.json / sitemap.json
  utils/
    request.js                       # 统一请求封装（自动带 token / 401 重登 / 附件上传）
    auth.js                         # wx.login -> /api/auth/login 换 token
  pages/
    index/      # 输入需求 -> 识别意图 -> 工具卡 -> (附件上传) -> 确认执行
    result/     # 轮询 /api/agent/invocation/{id} 展示结果/失败，复制/打开文档
    account/    # 余额与次数 + 档位列表 + 充值(wx.requestPayment 接入点)
    history/    # /api/agent/history 使用记录
```

- **联调注意**：`app.js` 中 `baseUrl` 本地填后端地址（如 `http://127.0.0.1:28082`）；真机/体验版必须为 https 并在小程序后台配置合法域名，本地调试可勾选「不校验合法域名」。
- **支付**：`account` 页充值走 `/api/billing/recharge/create` 拿 `prepay_params`，真机用 `wx.requestPayment` 调起；当前为 mock 环境，已标注「模拟回调 `/api/payment/notify` 加次数」的占位代码，上线接入真实虚拟支付时替换即可。
- **附件**：简历优化等工具经 `wx.chooseMessageFile` + `wx.uploadFile` 上传到 `/api/attachment/upload`，返回 `attachment_ids` 随确认请求提交。

---

## 12. 内容安全与合规

- **输入审核**：文本 msgSecCheck + 附件内容扫描，命中即拦。
- **输出审核**：高风险工具对输出再审后回显。
- **备案/类目**：小程序类目「工具」，完成认证+备案；个人主体审核更严，预留巡检。
- **隐私**：PII 最小化留存、加密、可删除；隐私政策告知。
- **AIGC 合规细则（v0.3）**：内容显式/隐式标识、生成式 AI 备案路径、投诉举报渠道、合规文书——见 §16.3（P0，提审前必须）。

---

## 13. 关键流程时序（文字版）

**A. 使用工具并扣次**
```
小程序→后端: 提交(文本+附件keys)
后端: 安全审核 → 附件校验 → Agent意图识别(tool+confidence)
小程序: 确认卡片(工具, 消耗次数)
用户: 确认
后端: 事务扣次 → 执行工具(降级路由选模型) → 返回结果
```

**B. 充值**
```
小程序→后端: 选档位 → /order/create → 微信下单 → prepay
小程序: requestPayment
微信→后端: /notify → 校验 → order.paid → 事务加次数
```

**C. 管理员新增纯配置工具**
```
后台: 填表单(名称/类目/prompt/附件约束/次数/model) → 保存(enabled=false)
后台: 灰度 → 设 enabled=true 放量
```

**D. 供应商故障降级**
```
工具执行请求 → 网关按 executor 链: 主供应商超时 → 标记 degraded → 下一节点成功 → 记录 model_used
```

---

## 14. 风险与【待确认】

### 风险
1. **意图误判扣费争议** → 执行前确认 + 低置信兜底 + 失败自动退次。
2. **iOS 12% 佣金** → 单独定价上浮；监控占比。
3. **月限 10 万** → 监控告警，规划公司主体升级。
4. **供应商不稳定** → 多供应商+降级路由+熔断（已设计）。
5. **附件/解析重** → 队列异步 + 超时失败处理；PPT 视觉重排列为后期。
6. **内容安全** → 必做审核，个人主体更严。

### 待确认【已全部明确，余下实现期验证】
- **手机号**：MVP 阶段实测个人主体下 `getPhoneNumber` 可用性；不可用则评估替代方案或仅以 openid 标识（非强制）。
- **LLM 厂商**：deepseek / qwen / glm / hy 全部经后台配置，不预选写死（见 7.1）。
- **首期工具**：文案改写、周报生成、年终述职稿 **全量上线**。
- **月限**：临近（~9 万）临时停售 + 提示升级主体（微信硬限额，超额支付会被拒）。
- **解析节奏**：纯文本 MVP 跑通后**立即做 docx 简历优化**（轻解析）。

### Backlog（技术债与后续评估，v0.3）

**数据库 PostgreSQL 迁移评估**（2026-10 评审记录）：
- 现状：§4 初稿写 PostgreSQL，**落地自 M1 起即复用本机已有 MySQL@23306**（决策依据：不重新部署中间件；InnoDB `FOR UPDATE` 已满足计费强一致），v0.3 已将 §4 更正为 MySQL——文档与实现一致，无需改动。
- 触发条件（满足其一才评估切换）：① 需要 PG 独有能力（如 JSONB 查询/数组类型、更强的并发写、逻辑复制）；② 已有 PG 实例可复用；③ 引入 Alembic 做正规迁移时顺带方言化。
- 工作量：**中等**。连接串一行（`postgresql+psycopg`）；SQLAlchemy 模型全部可移植；**主要痛点是 `core/db.py::_migrate()` 的 MySQL 方言裸 SQL**（`TINYINT(1)`/`MODIFY COLUMN`/反引号 `` `order` ``/`INSERT...SELECT` 回填），需重写为方言版或迁移到 Alembic；docker compose 需新增 PG 实例。
- 结论：**维持 MySQL**，当前切换纯成本无收益；若启动 Alembic 迁移管理，切换门槛自然降低。

**其他已知 backlog**（按优先级排序；"条件触发"类随对应外部事件自然升优先级，**触发信号检查清单见 `docs/backlog-triggers.md`，建议每月 1 号随月限检查过一遍**）：

| 优先级 | 条目 | 排序理由 / 触发条件 |
|---|---|---|
| **高** | 附件"任务后 N 天自动删除"定时清理（§10） | 隐私政策已对外承诺（`docs/privacy-policy.md`），不做=虚假陈述；实现小（worker 定时任务删 S3 对象+attachment 记录），**提审前应完成** |
| **高** | 订单对账 + 支付对账（§11-5） | 真实支付（`PAYMENT_PROVIDER≠mock`）上线后即为运营刚需，钱路径必须可核对 |
| **中** | `config_log` 配置变更历史与回滚（§8.3） | 工具/定价后台化后防误操作；工具过 20 个或多人运维前完成 |
| **中** | 用户余额手动调整（§11-7） | 客诉补偿的人工通道（自动退次已覆盖大部分场景）；客诉出现即升**高** |
| **中** | 声明式 DAG 工作流配置（§6.5 讨论） | 工具 30+ 且非技术人员需要编排时；当前代码注册方式足够 |
| **低** | 停售预约订阅消息真实触达（§16.2-B） | 依赖微信订阅消息模板申请 + 停售真实发生；月度任务已自动置位兜底 |
| **低** | 跨渠道账户合并（§9.5） | 依赖支付宝等第二渠道真实接入 |
| **低** | PG 迁移（评估见上） | 条件触发：需要 PG 独有能力 / 有现成实例 / 引入 Alembic 时顺带；结论为维持 MySQL |

---

## 15. 里程碑

- **M0 设计对齐**：本文档评审通过。
- **M1 最小闭环(MVP)**：Python+FastAPI + **LangGraph 执行框架(自 M1 起)** 起架；纯文本工具全量（文案改写/周报生成/年终述职稿）→ 意图识别(embedding 召回 + classifier 降级) → LangGraph 执行 → 扣次 → 结果；LLM 网关多供应商(deepseek/qwen/glm/hy)+降级跑通；充值 1 档位接微信虚拟支付；账户 openid（手机号 MVP 验证可用性）。
- **M2 工具规模化**：Tool Registry 后台化（已完成）。管理后台 CRUD：工具（含 prompt_template/price_quota/needs_attachment/async_exec/executor/enabled）、充值档位、LLM 供应商/模型/路由；管理员账号登录（Bearer JWT）鉴权。纯配置工具已扩至 9 个（文案改写/周报/述职/邮件/纪要/小红书/翻译/扩写/摘要）+ 1 个解析工具（简历优化）。
- **M3 轻解析工具**（已完成）：S3(rustfs) 附件上传（单文件≤10MB、后端支持多文件、格式受限）；docx/pdf/txt 文本抽取；Redis 异步队列 + 进程内 worker 线程；`docx_resume` 多步 LangGraph（optimize→make_docx）生成新文档并回写 S3；`/api/agent/confirm` 异步入队、`/api/agent/invocation/{id}` 轮询结果。计费：异步工具在执行前原子扣次，失败退次。
- **前端工程**（已完成）：① 管理后台 Web 界面 `static/admin/index.html`（FastAPI 同容器托管 `GET /admin`，原生 HTML+JS，管理员账号登录（Bearer JWT）鉴权，工具/档位/供应商/模型/路由 CRUD）；② 微信小程序骨架（已拆分到独立仓库 `miniprogram`；4 页面：index 意图识别+附件+确认 / result 轮询 / account 余额+充值 / history 记录，含 `utils/request.js`、`utils/auth.js`，`wx.requestPayment` 与微信虚拟支付接入点齐备）。小程序需导入微信开发者工具并替换 appid/baseUrl，真机须 https 合法域名。
- **M4 重活与合规**（已完成）：
  - **PPT 视觉重排**：`pptx_optimize` 工具（3 次/单）+ `pptx_resume` 多步 LangGraph（load 取原稿→optimize LLM 内容优化与版式规划(JSON)→rebuild python-pptx 重排导出并回写 S3）；附件解析新增 pptx 逐页文本抽取；mock 模式下 LLM 非 JSON 输出自动回退「原稿重渲染」，保证链路可跑通。
  - **内容安全全链路**：`content_security.screen()` 可插拔 provider（`mock` 演示拦截词表 / `wechat` 预留 msgSecCheck 真实路径，异常 fail-closed）；覆盖 **输入(recognize/confirm) → 附件文本(worker) → 输出(executor)** 三层；每次筛查写入 `audit_log`（通过/命中均留痕），命中即 400/退次标失败。
  - **月限监控 + 主体升级评估**：`monthly_status()` 实时计算本月收款 vs 月限(10万)/预警(8万)/停售(9万) 阈值；`notify_paid` 到账后自动评估并写入 `alert_log`（同类型当月去重）；管理后台新增「监控」页：额度进度条、停售/预警/建议升级主体状态、告警历史、内容安全命中明细（`GET /api/admin/monitor`）。
  - 验证：`test_m4.py` 端到端（登录→充值→识别→拦截词 400→pptx 上传→异步重排→轮询取回 `results/{id}.pptx`→监控数据），p313 + 复用中间件通过。
- **M5 可靠性与体验补课**（已完成，架构评审后实现层加固，模式不变）：
  - **presigned 下载闭环**：`result_ref` 改存 object_key，API 层按需签发临时下载 URL（`PRESIGN_EXPIRES_SECONDS`，默认 1h）；小程序 result 页用签名 URL `wx.downloadFile`+`openDocument` 打开文档（私有 bucket 裸 URL 403 问题解决）。
  - **意图识别三件套**：新增 `recall.py` 候选召回（embedding 可插拔：配 `EMBEDDING_*` 走 OpenAI 兼容 /embeddings 余弦，未配置用本地 bigram 排序，top-K=5 收缩候选）；置信度三分支结构化输出 `status`（confirm ≥0.85 / clarify 0.6–0.85 追问 / menu <0.6 菜单兜底）；mock classifier 按名称 bigram 命中派生置信度（1 命中 0.92 / 多命中 0.7 澄清 / 0 命中 0.45 菜单）；小程序 index 页支持菜单选择与澄清确认。
  - **熔断落库**：网关健康状态进程级共享（跨请求），连续失败 `CB_FAILURE_THRESHOLD`(3) 次标 down 跳过，冷却 `CB_COOLDOWN_SECONDS`(60) 后半开重试；状态变化落库 `provider.health_status`（healthy/degraded/down），后台可查。
  - **pending reaper**：worker 循环每 ~60s 扫描超时 pending（`PENDING_TIMEOUT_MINUTES`=10）→ 标 failed + 退次，兜底"扣费任务丢失"。
  - **全量异步 + 限流**：confirm 所有工具统一异步入队（LLM 调用不再占请求线程）；recognize 按 IP、confirm 按 openid Redis 固定窗口限流（`RATE_LIMIT_PER_MIN`=30，429），Redis 异常时放行不阻塞。
  - 验证：`test_m5.py`（三分支/全量异步/presigned 下载 PK 魔数/限流 429 + allow() 单测），p313 + Docker 双环境通过。
- **M6 结构性缺口补齐**（规划，见 §16）：按 P0 AIGC 合规 → P1 成本闭环 → P2 工具发现 → P3 主体升级预案 推进。

---

## 16. 结构性缺口设计（v0.3 评审结论）

产品定位匹配度评审发现四个"定位已隐含要求、本文档未覆盖"的盲区。**共同取向：全部做成对现有架构的增量**（新表 ≤3 张、新字段 ≤6 个、新接口 ≤6 个、零主链路改动）。

### 16.1 工具发现（工具 >30 个前落地，P2）

定位"N 个工具"只回答了"怎么选"（§6），未回答"怎么逛"。工具上百后 menu 兜底平铺不可用。

**数据模型**：`tool` 补 `tags`(JSON, 搜索)、`sort`(类目内排序)、`is_hot`(BOOL)、`usage_count`(INT 冗余计数)。分类沿用 `tool.category` 字符串约定，不建分类表。热度 = 7 天窗口 invocation 实时聚合（量级小不物化）。

**API（新增 `app/api/discovery.py`）**：
```
GET /api/tools/categories                     -> [{category, name, count}]
GET /api/tools?category=&sort=hot|new&page=   -> 分页列表
GET /api/tools/search?q=                      -> name/description/tags 检索
GET /api/tools/{id}                           -> C 端详情
```
上百工具后 search 切 embedding 语义检索——复用 §7.3 embedding 槽位与 recall.py，零新架构。

**小程序**：index 改版（搜索框 → 类目 tab → 热门卡片 → 底部保留智能识别输入框）；工具详情页"立即使用"带 tool_id 直进确认卡（confirm 本就接受 tool_id，后端零改动）；深链分享 `index?tool_id=xxx` 作增长钩子。

**Router 配合**：menu 兜底改两级（先类目后列表）；类目上下文作为 recall 先验收敛歧义。

**分期**：D1 分类/列表/搜索/详情 4 接口 + index 改版（1-2 天）→ D2 热度统计 + 详情页 + 深链分享（1 天）→ D3 embedding 语义搜索（随 §7.3 落地）。
**D3 已完成（2026-10）**：新增共享向量化服务 `services/embedding.py`（`EMBEDDING_PROVIDER=local` 默认：字符 bigram 哈希 256 维，零依赖离线可用，部分匹配/容错优于 LIKE；`=openai` 时配 `EMBEDDING_*` 走兼容 /embeddings 获真语义；`embed_texts` 恒可用，provider 失败自动回落 local）；搜索改为**语义余弦 + 关键词命中加权**混合排序（精确命中 +0.3 / 子串 +0.1），异常回落 LIKE；recall.py 复用同一服务（provider 模式召回语义排序，local 模式保持 bigram——哈希向量不优于 bigram）。实测："述职用的幻灯演示"（LIKE 必空的部分查询）语义返回相关工具；"简历"精确查询仍首位；recognize 无回归。§7.3 embedding 路由槽位即用此服务。验证：`test_d3.py`，p313 + Docker 通过。
**D1/D2 已完成（2026-10）**：`tool` 补 `tags/sort/is_hot/usage_count`；C 端发现接口 `GET /api/tools/categories`、`GET /api/tools?category=&sort=hot|new&page=`（分页，hot=运营标记+累计使用量）、`GET /api/tools/search?q=`（name/description/tags LIKE，上百工具后按 D3 切 embedding）、`GET /api/tools/{id}` 详情（含 attachment_spec）；worker 成功后 `usage_count` 自增；小程序 index 改版（搜索框+类目横滑 tab+工具列表+保留智能识别入口）、新增工具详情页 `pages/tool`（自助确认执行，带 tool_id 跳过意图识别）、深链分享 `index?tool_id=`（onShareAppMessage）、menu 兜底按类目分组；admin 工具表单可编辑 tags/sort/is_hot；seed 为部分工具预置 tags/热门。验证：`test_discovery.py`（类目/排序/过滤/分页/搜索含 tags/详情/热度自增/menu 分组），p313 + Docker 通过。

### 16.2 主体升级预案（P3，waitlist 先行）

月限 10 万 ≈ 1500–10000 单/月，起量即触顶；§9.2 只有"停售+提示"。

**A. 支付通道抽象**：`PaymentProvider` 接口（create_order/query_order/parse_notify/refund），实现 mock / wechat_virtual(个人) / wechat_merchant(公司) / apple_iap。`order` 补 `payment_provider` + `entity`(personal/company)。现有 mock 流程已按此形状，抽象成本低；通道切换为发版级操作，后台只读展示。

**B. 月限分级响应（补全 §9.2）**：8 万告警（✅ M4）→ 9 万停售（✅）→ 停售期间新增 **`restock_waitlist`(user_id, created_at, notified)** 预约表 + 小程序"开售提醒"入口，次月 1 号订阅消息触达；预约量兼做升级主体的 ROI 需求验证。

**C. 升级执行清单**：公司注册 → 小程序主体变更（微信认证 300/年）→ 虚拟支付/商户权限重申 → PaymentProvider 切换演练 → 双通道灰度对账。存量迁移：account 按 user_id 与主体无关、余额天然延续；历史 order 按 `entity` 拆对账。解锁项：原路退款放开（transaction 加 `refund_cash` 类型），月限解除但停售逻辑保留为总开关（防平台风控）。

**分期**：U1（半天）waitlist + order.entity → U2（随真实支付接入）PaymentProvider 抽象 → U3（升级发生时）执行清单。
**U1 已完成（2026-10）**：`restock_waitlist` 表（user_id unique 幂等 / channel / notified）；`POST /api/billing/restock/join`（仅停售可约 `not_sold_out`，限流）+ `GET /restock/status`；admin waitlist 列表 + 人工标记触达；月度触达任务（worker 每小时检查，上月未通知自动置 notified，**小程序订阅消息真实发送为接入点 TODO**）；小程序 account 页停售横幅 + "预约下月开售提醒"按钮（已预约/已提醒三态）；`order` 补 `payment_provider` + `entity`（默认 mock/personal，升级公司主体时切配置 `PAYMENT_PROVIDER`/`ENTITY_TYPE` 并按 entity 拆对账）。验证：`test_u1.py` 三阶段（正常充值 → STOP_SELL_THRESHOLD=1 停售态：充值拦截/停售标记/join 幂等/月度触达置位/entity 落库 → 恢复阈值未停售 join 拒绝），p313 + Docker 通过。

### 16.3 AIGC 合规（P0，提审前必须）

《生成式 AI 服务管理暂行办法》《AI 生成合成内容标识办法》要求的动作，§12 原有内容不覆盖。

**A. 内容标识（显式+隐式）**：结果页固定"内容由 AI 生成，仅供参考"声明；导出文档写元数据标识（docx core_properties / pptx 备注，author="万宝工具箱 AI"，~10 行）；tool_invocation 全量留存已满足可追溯。

**B. 备案路径（待法务确认）**：我方为"接入已备案大模型"（deepseek/qwen/glm/hy 均已备案）的轻量路径；是否触发独立算法备案待确认。**注：网信办公示口径为「模型名称 + 备案号」（2024 年备案公告），故备案登记以「模型维度」为准**（`llm_model.filing_name/filing_no/filing_source_url/filing_checked_at`），供应商维度（`llm_provider.filing_subject/filing_no`…）作为备案主体信息与兜底。系统配合：监控页"大模型备案与公示"卡片（模型优先、供应商兜底、未登记标红）+ 对外公示接口 `GET /api/compliance/filings` + 小程序协议页底部公示位（合规硬要求：显著位置公示）。

**C. 投诉举报渠道（监管硬要求）**：新表 `feedback`(user_id, invocation_id, type[report/suggestion], content, status, created_at)；小程序结果页"反馈/举报"入口；admin 处理列表；audit_log 高频命中用户人工 `user.status=ban`。

**D. 文书**：隐私政策 + 用户协议（AI 生成内容免责、个人信息处理、未成年人条款），随提审提交。

**分期**：C1（提审前）声明+标识+feedback+文书 → C2（随生图）图片标识/水印 → C3 备案路径确认后回写。
**C1 已完成（2026-10）**：结果页 AI 生成声明+协议页入口；docx/pptx 导出写元数据标识（author=万宝工具箱 AI / category=AI生成内容）**+ 文内显式标识**（docx 末尾说明行、pptx 首页备注）；`feedback` 表 + `POST /api/feedback`（登录态+限流+内容安全筛查）+ 小程序结果页"反馈/举报"入口 + admin 反馈处理页；admin 用户列表/封禁（ban 生效于 confirm，403）；`llm_provider.filing_no` + 监控页备案状态卡片；小程序协议页（`pages/agreement`）；文书模板 `docs/user-agreement.md`、`docs/privacy-policy.md`（**发布前须经法务审阅并填占位符**）；提审对照清单 `docs/compliance-checklist.md`（微信隐私指引逐项/类目/标识自验/支付合规/上线自检）。验证：`test_p0.py`，p313 + Docker 通过。

### 16.4 成本-定价闭环（P1，先于规模化）

`price_quota` 目前拍脑袋；多步工具（pptx）单次 token 为单步数倍，可能亏钱。

**A. 成本采集**：网关 `call()` 捕获 `usage`（prompt/completion tokens，现被丢弃）；`tool_invocation` 加 `cost_amount`(DECIMAL)，多步图在 state 累加 `tokens × model.price_per_1k`，run_tool 收口写库；生图/视频按"单价×数量"记同列（§7.3）。

**B. 经营视图（admin 监控页"经营"区块）**：本月毛收入 / 消耗次数 / LLM 成本 / 毛利率；按工具（调量/单均成本/单均毛利，低于阈值标红）、按供应商分布（对账）。

**C. 定价规则**：上架依据 = 预估 tokens × 单价 × 1.3(降级冗余) ÷ 目标毛利率 70% → price_quota；上架 2 周按真实成本校准。**防亏熔断**：单工具月累计成本超阈值 → `alert_log`(kind=tool_cost)。

**分期**：P1 usage 捕获 + cost_amount + 经营区块（1-2 天）→ P2 按工具报表 + 防亏告警 → P3 后台定价建议器。
**P1 已完成（2026-10）**：网关 `call()` 捕获 usage（prompt/completion tokens，mock 亦返回模拟 usage 保证管线可测）按 `price_per_1k` 折算，网关实例级 `total_cost` 累加（实例=一次工具执行，天然归属）；`tool_invocation.cost_amount` 落库（run_tool 收口，worker 写入，invocation 接口返回）；`analytics.biz_metrics()`：本月毛收入/售出次数/单次均价/成功调用/消耗额度/LLM 成本/毛利额与毛利率 + 按工具（估算收入=消耗次数×单次均价，毛利率<30% 标红）+ 按供应商分布；admin 监控页新增"经营"区块。配套完成 §9.5 多渠道身份与定价。验证：`test_p1.py`，p313 + Docker 通过。
**P2 已完成（2026-10）**：单工具防亏告警——`tool.cost_alert_threshold`(DECIMAL(10,4)，空=全局 `TOOL_COST_ALERT_THRESHOLD` 默认 50 元/月)；worker 成功落库成本后按月累计该工具成本，超阈值写 `alert_log(kind=tool_cost)`（`_maybe_alert` 支持按工具 dedup_key，当月每工具至多一条）；admin 监控页告警列表与工具表单直接可视/可配。验证：`test_p2.py`（触发/当月去重/工具间隔离/恢复默认），p313 + Docker 通过。

### 16.5 实施优先级

| 优先级 | 项 | 理由 |
|---|---|---|
| **P0** | AIGC 合规 C1（§16.3） | 提审硬门槛 |
| **P1** | 成本闭环 A/B（§16.4） | 规模化前看清单位经济，改动小 |
| **P2** | 工具发现 D1/D2（§16.1） | 工具 30+ 前落地，兼增长钩子 |
| **P3** | 主体升级 U1（§16.2） | 半天成本，waitlist 兼需求验证 |

---

## 17. 交互式多轮会话工具（已实现）

**动机**：工具的常见形态不应该只是「输入 → 出结果」。以简历优化为例，用户真正需要的是：
上传简历 → 系统**分析**并指出「缺失信息 / 可疑之处」→ **多轮追问**补齐 → 用户**确认** → 产出文档。
这一形态对多数文档类工具通用，故做成**框架 + 配置**，而非每个工具各写一套。

### 17.1 架构：把「人工介入」落在工具图内（形态 A）

按 §6.5 的原话实现——人工介入是工具图里的节点，用 LangGraph `interrupt` 暂停：

```
analyze ─► decide ─(还有问题)─► ask(interrupt) ─► merge ─► decide        ← 追问循环
              └─(够了/跳过)─► prepare_confirm ─► ask_confirm(interrupt) ─► produce ─► END
```

**两条硬约束**（LangGraph 语义决定，实现时最容易踩）：
1. `ask` / `ask_confirm` **只能调用 `interrupt()`，不得有任何副作用** ——
   被中断的节点在恢复时会**从头重跑**，有副作用就会重复调 LLM / 重复写库。
   所有耗时与写操作放在 `interrupt` 之前（`decide`）或之后（`produce`）的独立节点。
2. **state 必须是可序列化纯数据**（图带 checkpointer，state 要落库）——
   禁止放入 SQLAlchemy Session / ORM 对象；每个节点各自 `SessionLocal()` 并关闭。

### 17.2 持久化：自建 MySQL checkpointer

`core/llm/checkpoint.py` 实现 `BaseCheckpointSaver`（`get_tuple/put/put_writes/list/delete_thread`），
语义对齐官方 `InMemorySaver`，存储落在 `lg_checkpoint` / `lg_checkpoint_blob` / `lg_checkpoint_write`。

- **不自建依赖**：官方 sqlite/postgres saver 均需额外安装包；状态与业务同库也便于审计与按会话清理
- 序列化复用 `JsonPlusSerializer`（`dumps_typed` 返回 `(type, bytes)`，两者都要存）
- **坑（已踩）**：值为 `None` 的 channel（挂起的 `branch:to:*` 标记）序列化后是 `("null", b"")`，
  若按「空值」跳过，LangGraph 就无法判断图暂停在哪个节点 → 只能跳过显式 `"empty"`
- 会话结束/过期时 `delete_thread` 清理，避免 checkpoint 表膨胀
- `thread_id` = `str(tool_session.id)`

### 17.3 数据模型（两层状态，单向投影）

| 存储 | 内容 | 写入方 |
|---|---|---|
| `lg_checkpoint*` | 图的**权威状态** | LangGraph 自动 |
| `tool_session` | 业务态**投影**：status / analysis / facts / 报价 / charged / invocation_id / expires_at | **worker 每次 `graph.invoke` 返回后统一投影（唯一写入点）** |
| `session_message` | 前端渲染用的扁平消息列表 | 同上 |

投影是单向的（图状态 → 业务表），所以不会出现两处状态冲突；
`tool_session` 只作前端视图与运维查询。最终产出仍复用 `tool_invocation`，
因此 `result` 轮询、文件下载、`/history` 全部零改动。

`tool` 新增：`interactive`（是否交互式）、`analysis_prompt`（分析步提示词，空=框架默认）、
`interaction_config`（`{"max_rounds","allow_skip","tool_calls","max_tool_rounds"}`）。

### 17.4 会话状态与前端驱动

| status | 含义 | 前端 |
|---|---|---|
| `running` | worker 正在跑一步 | 加载气泡 + 轮询 |
| `awaiting_answer` | 有追问待回答 | 分析卡 + 问题 + 输入栏 + 「跳过追问直接生成」 |
| `awaiting_confirm` | 等计费确认 | 「确认生成（N 次）」 |
| `success` | 产出完成 | 跳结果页（复用轮询/下载） |
| `failed` / `expired` / `cancelled` | 异常 / 超时 / 取消 | 提示 + 重新开始 |

`GET /api/agent/session/{id}` 返回 `interrupt`（`{type:"question"|"confirm_charge",...}`）与 `analysis`，
前端完全由 `interrupt.type` 驱动，不需要理解图结构。

### 17.5 接口

| 端点 | 说明 |
|---|---|
| `POST /api/agent/session` | 建会话（入队跑图到第一个 interrupt）；校验：已是交互式工具、附件必填、输入内容安全、未付费活跃会话上限 |
| `GET /api/agent/session/{id}` | 轮询：状态 + interrupt + 分析 + 消息 + 报价 + 余额 |
| `POST .../answer` | 回答当前追问（状态须为 `awaiting_answer`） |
| `POST .../skip` | 跳过剩余追问，直接进入确认 |
| `POST .../confirm` | **扣次 + 建 `ToolInvocation` + 恢复图**（行锁 + `charged` 守卫，幂等） |
| `POST .../cancel` | 取消（未扣次；已扣次拒绝） |

### 17.6 计费与退款边界

| 场景 | 处理 |
|---|---|
| 分析 / 追问 / 回答 / 检测子工具 | **免费**，只累加 `session.llm_cost` |
| `/confirm` | `consume` 扣一次，写 Transaction；免费阶段成本并入 `invocation.cost_amount`（喂 §16.4 成本告警） |
| 产出阶段失败 / 超时 | `invocation.failed` + `amount_charged=0` + `refund` |
| 分析阶段失败 / cancel / expired | 未扣次，无退款 |
| 重复 `/confirm` | 行锁 + `charged` 守卫 → 返回既有 `invocation_id`，不重复扣次 |

防白嫖：分析免费故需约束 —— 同一用户未付费活跃会话 ≤ `SESSION_MAX_ACTIVE`(3)，
追问 ≤ `SESSION_MAX_ROUNDS`(5) 轮，会话 `SESSION_TTL_MINUTES`(30) 分钟有效。

### 17.7 逐轮安全与受限工具调用

- 内容安全**每轮**执行：用户输入 `check_input`、分析与追问文案 `check_output`、附件 `check_attachment`；
  fail-closed 命中 → 会话 `failed` 且未扣次不退款
- **L2 受限工具调用**：`decide` 节点内让 LLM 主动调用白名单内的**检测子工具**
  （`check_contact` / `check_quantified` / `check_timeline` / `check_job_intent` / `check_consistency`），
  由 `services/agent/detectors.py` 以**纯规则实现、不调 LLM**（成本可预测、结果可复现），
  并有**步数上限** `max_tool_rounds`(2) 与白名单校验（越权直接拒绝）。
  为此 `LLMGateway.call` 新增返回 `tool_calls` 与入参 `tool_choice`（向后兼容）。

### 17.8 已知约束

- **worker 并发**：已支持多消费线程（`WORKER_CONCURRENCY`，默认 2）。`BLPOP` 对多消费者原子安全；
  **reaper / waitlist 只由 0 号线程执行**（reaper 内部是「查-改」而非原子 CAS，多线程并发会导致重复退次）。
  再往上扩（多容器）需给 reaper 加 Redis 抢锁。
- **可序列化 state** 与 **interrupt 节点无副作用** 是新增工具图时必须遵守的两条纪律。
- **成本**：免费阶段的 `llm_cost` 会并入最终 `invocation.cost_amount`；但用户中途放弃、永不 confirm 的会话
  其成本不会进入单工具成本告警（视为获客成本）。

### 17.9 新工具接入方式

**配置驱动（零代码）**：`interactive=True` + `interaction_config` + 可选 `analysis_prompt`。
需要自定义产出（如导出 docx/pptx）时，在 `services/agent/session/__init__.py` 注册 handler
（参考 `session/resume.py` 产出 docx、`session/pptx.py` 基于原稿重排产出 pptx）。

**当前已接入**：
- 文档类（需附件，产出文件）：`resume_optimize`（docx）、`pptx_optimize`（pptx，原稿重排）
- 文本创作类（追问 ≤3 轮，无检测子工具）：`weekly_report`、`annual_review`、`email_write`、`meeting_minutes`、`xhs_copy`
- 纯文本变换类（`copy_rewrite` / `expand` / `summarize` / `translate`）**保持单轮** —— 追问对它们只是噪音

**后台**：「工具」页可勾选/取消「交互式」；「会话」页可查看会话列表（状态/轮次/分析条数/扣费/产出/
过期时间）。出于隐私考虑，列表**不回传附件正文与用户补充内容**，`input_text` 截断展示。

---

*下一步：按 §16.5 优先级推进 M6（P0 起步）；§14 余下【待确认】项随对应里程碑验证。*
