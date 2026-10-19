# 渠道账号配置说明（channel-account）

> 渠道账号 = 一个接入的应用（微信小程序/公众号/支付宝/其他）。登录、支付、内容安全、收款限额**全部按渠道独立配置**，env 中已无任何业务配置键。
> 配置入口：管理后台「渠道账号」页。本文档是字段字典 + 接入指引。

## 1. 字段总表

| 字段 | 说明 | 默认/回落 | 改后生效 |
|---|---|---|---|
| id | 短 id（如 wx_mp_main），数据归属键 | — | — |
| type | 渠道类型：wechat_mp / alipay / other | — | — |
| name | 显示名 | — | — |
| appid / secret | 微信小程序凭据。用于：登录 code2session、jsapi、msgSecCheck v2 | 未填 → 该渠道登录走 mock_openid；**与其他渠道互不影响** | 即时 |
| enabled | 启用（停用后该渠道请求按无渠道处理） | true | 即时 |
| mock | **登录 Mock**：该渠道登录/手机号/内容安全走假数据（测试渠道用） | false | 即时 |
| entity_type | 主体：personal（虚拟支付，月限 10 万）/ company（商户支付）。下单盖戳 + 提审口径 | personal | 即时 |
| payment_provider | 支付通道：mock / wechat_virtual / wechat_merchant（apple_iap 由 iOS 平台决定） | mock | 即时 |
| payment_mock | **支付 Mock**：模拟支付成功 + 回调免验签（联调用）。**生产渠道务必关** | true | 即时 |
| virtual_pay | 虚拟支付配置 JSON（offer_id/appkey/sandbox_appkey/push_token/aes_key/sandbox），仅 wechat_mp | 字段级回落全局「支付配置」 | 即时 |
| month_limit / warn_threshold / stop_threshold | 本月收款上限/预警/停售阈值（¥） | 空回落代码常量 100000/80000/90000 | 即时 |
| remark | 备注 | — | — |

## 2. 两个 Mock 的区别

| | mock（登录 Mock） | payment_mock（支付 Mock） |
|---|---|---|
| 影响范围 | 登录换 openid、手机号获取、内容安全 msgSecCheck | 模拟支付成功端点、支付回调免验签 |
| 典型场景 | 测试小程序没有真实凭据，也能登录进系统调试功能 | 联调充值到账流程，不需要真实付款 |
| 生产要求 | 测试渠道可留开；正式渠道关闭 | 正式渠道**必须关**（否则任何人可 mock-pay 免费充值） |

## 3. 虚拟支付配置的三级回落

```
账户级 virtual_pay JSON（每小程序一套 OfferID/AppKey/推送密钥）
  ↓ 未覆盖的字段
全局「支付配置」（app_setting WECHAT_VIRTUAL_*，后台「支付配置」页）
  ↓ 不再回落 env（已移除）
```

沙箱开关：账户级 `sandbox` > 全局 `WECHAT_VIRTUAL_SANDBOX`；均未配置默认 **sandbox=true**（安全侧）。

## 4. 新渠道接入 checklist

1. 后台「渠道账号 → 新建」：填 id/type/name/appid/secret
2. 勾选主体类型与支付通道（个人主体 → wechat_virtual；企业主体 → wechat_merchant）
3. **确认 payment_mock 关闭**（生产收款渠道）
4. wechat_mp 且走虚拟支付：点「支付配置」填该小程序的 OfferID/AppKey/推送密钥/AESKey
5. 按主体设置收款阈值（个人主体月限 10 万硬限额，建议预警 8 万/停售 9 万）
6. 小程序端请求带 `X-App-Id` 头（或用户登录后自动归属），即完成渠道路由

## 5. 首次部署说明

- 系统首次启动会自动创建一个**无凭据的占位渠道账户**（id=default，承载存量数据归属）
- 部署后请立即在后台填写其 appid/secret，未填前该渠道登录走 mock_openid（有日志可查）

## 6. 相关接口（管理员，Bearer 或 ?token=）

| 接口 | 说明 |
|---|---|
| GET/POST/PUT/DELETE `/api/admin/channel-accounts` | 渠道账号 CRUD |
| GET/PUT `/api/admin/channel-accounts/{id}/virtual-pay` | 账户级虚拟支付配置 |
| GET `/api/admin/settings/virtual-pay` | 全局虚拟支付配置 + 就绪状态 |
| GET/POST `/api/admin/settings/embedding` | 向量模型配置（另见 config-audit §3c） |

C 端：`GET /api/billing/config` 按**请求所属渠道**返回 `payment_mock / payment_provider / entity`，小程序据此决定拉起哪种支付。
