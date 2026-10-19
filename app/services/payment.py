"""微信虚拟支付（MVP 占位 / 真实接入点）。

真实环境需在「微信支付->个人虚拟支付」后台配置，并实现：
  1. /recharge/create 调用下单 API 获取 prepay_id，返回小程序 wx.requestPayment 所需参数；
  2. 微信异步回调验签后调用 billing.notify_paid。
此处以 mock 返回，便于端到端联调；接入时只需替换 create_prepay 实现。
"""
from app.models import Order


def create_prepay(order: Order) -> dict:
    # TODO: 调用微信虚拟支付下单，返回 {appId, timeStamp, nonceStr, package, signType, paySign}
    return order.prepay_params or {"mock": True, "order_id": order.id}
