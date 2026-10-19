# 万宝工具箱

**一个入口，N 个 AI 工具。** 用户用自然语言描述需求（文本 + 可选附件），Agent 识别意图并调度对应工具，按次扣减使用次数。核心资产是 Tool Registry 与 Agent 调度——加功能 = 加工具配置，不改主链路。

## 项目结构

```
backend/       FastAPI 后端（本仓库；即当前目录本身 = 仓库根）
               app/api/       auth · agent · tool · attachment · billing · payment · admin · feedback
               app/services/  agent 调度 · 计费 · 限流 · 内容安全 · 附件解析 · 嵌入
               app/worker/    异步任务队列（工具异步执行）
               static/        管理后台静态页
docs/          设计方案 · 里程碑计划 · 合规清单 · 隐私政策
scripts/       品牌资源生成（icon / 分享图，纯标准库）
```

> **仓库布局**：`wanbao/` 是容器目录（本身不是仓库），下面并列两个独立仓库：
> `backend/`（本仓库：后端 + 文档 + 资源生成脚本）与 `miniprogram/`（微信小程序，独立仓库）。
> 小程序相关改动请到 `miniprogram/` 提交。

## 技术栈

- **后端**：Python 3 / FastAPI / SQLAlchemy 2 / MySQL / Redis / LangGraph（多供应商 LLM + 降级路由）/ S3（附件存储）
- **前端**：微信小程序原生框架（独立仓库 `miniprogram/`）
- **部署**：Docker（复用宿主机已有 MySQL/Redis/S3 中间件）

## 快速开始

### 后端

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # 按需填写配置

uvicorn app.main:app --reload --port 8000
```

启动时自动建库、初始化表并执行 seed。API 文档见 `http://localhost:8000/docs`。

或使用 Docker（复用本机已有中间件）：

```bash
cd backend
docker compose up -d --build   # 服务监听 :28082
```

### 小程序（独立仓库）

用微信开发者工具打开 `../miniprogram` 仓库根目录，填入自己的 `project.config.json` AppID 即可预览。

品牌资源（icon / 分享图）由本仓库脚本生成，默认输出到同级的小程序仓库：

```bash
python3 scripts/gen_icon.py           # -> ../miniprogram/assets/icon.png
python3 scripts/gen_share.py          # -> ../miniprogram/assets/invite-share.png
# 路径不同时用 MP_ASSETS 指定：
MP_ASSETS=/path/to/miniprogram/assets python3 scripts/gen_icon.py
```

## 部署与限流

### 部署流程

```bash
cd backend
docker compose build && docker compose up -d   # 重建镜像 + 启动（代码变更必须 build，restart 不更新代码）
```

- 启动时自动执行幂等迁移与 seed（新工具按 id 补种，已存在的工具行不会被覆盖——改配置需在后台或手动 UPDATE）
- 生产入口：Nginx（gateway-nginx 容器）反代 `wbg.arfeifei.com` → 容器 :28082

### 限流：三层防线

| 层 | 机制 | 阈值 | 调整方式 |
|---|---|---|---|
| Nginx 入口 | limit_req 漏桶（按 IP） | 全站 30 r/s + burst 60；写接口 5 r/s + burst 10，超限 429 | 改 `~/Documents/hyper/devops/gateway/nginx/conf.d/default.conf` → `nginx -t` → `nginx -s reload`（详见 `devops/gateway/README.md`） |
| 应用层 | Redis 固定窗口（按用户/按 IP） | 识别 30/min·IP；建会话 10/10min·用户、30/10min·IP | **后台动态调**：工具管理页"⚙ 限流参数"（存 `app_setting` 表，即时生效，无需重启） |
| 经济闸 | 确认扣费 + 日成本上限 | 每用户每日 LLM 成本 2 元（`SESSION_DAILY_COST_LIMIT`） | `.env` + 重建容器 |

限流实现：`app/services/ratelimit.py`（Redis 固定窗口，Redis 不可用自动放行不阻塞主链路）。

### 运行时开关与本地组件

| 开关/组件 | 说明 | 调整方式 |
|---|---|---|
| 流式输出（`stream_enabled`） | 简单工具产出的增量上屏；关闭 = 轮询链路 | 后台工具管理页"流式输出"勾选框，即时生效 |
| 本地 bge-m3 embedding（:11434） | 意图识别召回 + 工具搜索的语义向量 | 宿主机 launchd 自启（`com.wanbao.embed-server`），详见 `~/Documents/hyper/devops/embed/README.md` |
| 入口 Nginx | 公网反代 + 入口限流 | 详见 `~/Documents/hyper/devops/gateway/README.md` |

`.env` 相关项：`EMBEDDING_PROVIDER/BASE_URL/API_KEY/MODEL`（语义召回，未配置自动回落关键词匹配）、`SESSION_*`（会话护栏）、`RATE_LIMIT_PER_MIN`（动态阈值的默认值）。

## 计费模式

余额/次数包：用户先充值购买次数，每次工具调用扣减一次。个人主体受微信虚拟支付月限额约束（详见 `docs/design.md`）。

## 文档

- 系统设计：`docs/design.md`
- 里程碑计划：`docs/m1-plan.md`
- 合规清单：`docs/compliance-checklist.md`
