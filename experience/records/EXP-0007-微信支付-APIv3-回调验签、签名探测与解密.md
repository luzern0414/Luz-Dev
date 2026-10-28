---
id: "EXP-0007"
title: "微信支付 APIv3 回调验签、签名探测与解密"
status: candidate
created: "2026-10-26T15:58:56+08:00"
updated: "2026-10-26T15:58:56+08:00"
task_mode: "implementation"
plan: "D:\\Codes\\skill输出\\_归档\\Luz-Dev\\05-Luz-Dev-调研审核门禁与安全规范体系\\05-Luz-Dev-调研审核门禁与安全规范体系.html"
tags: ["callback", "payment", "security", "wechat-pay"]
evidence: ["test: backend/09-wechat-pay/code/tests/test_apiv3.py 17 项通过（backend 共 101 项）", "source: pay.weixin.qq.com/doc/v3/merchant/4012791861、4012071382、4012791911 于 2026-10-26 读取"]
reusable_assets: ["backend/09-wechat-pay/code/wechat_pay/apiv3.py"]
supersedes: ""
superseded_by: ""
---

# 微信支付 APIv3 回调验签、签名探测与解密

## 摘要

回调用原始报文构造验签串，按 Wechatpay-Serial 选择微信支付公钥或平台证书；WECHATPAY/SIGNTEST/ 探测签名必须验签失败；AES-256-GCM 解密后核对 appid、mchid、单号、金额、trade_state，5 秒内应答 204。

## 决策与做法

- 生产优先官方 SDK；apiv3.py 作为纯函数参考实现与 SDK 行为核对。

## 失败尝试与教训

- 原实现只有 HMAC 占位验签，与真实回调格式不一致，测试不能证明安全性。

## 验证证据

<!-- EVIDENCE_START -->
- test: backend/09-wechat-pay/code/tests/test_apiv3.py 17 项通过（backend 共 101 项）
- source: pay.weixin.qq.com/doc/v3/merchant/4012791861、4012071382、4012791911 于 2026-10-26 读取
<!-- EVIDENCE_END -->

## 可复用候选

- 尚无复用候选。

## 废弃与替代

<!-- DEPRECATION_START -->
_未废弃。_
<!-- DEPRECATION_END -->
