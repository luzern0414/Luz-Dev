# 业务闭环结果页实施

- 来源经验：EXP-0003
- 状态：verified
- 适用场景：支付、订单、预约、报名、申请、售后、多步骤表单、异步任务、批量操作，以及任何会形成业务编号或终态的闭环
- 不适用场景：保存标签/开关等低风险即时修改；可继续编辑的字段校验错误；单纯页面加载失败
- 前置条件：技术方案已生成；服务端结果契约与目标平台导航能力已确认；权限、幂等和业务状态机已定义
- 关键步骤：
  1. 在技术方案中按场景选择 inline、form-error、result、processing、partial 或 failure 六种落点，写明路由、状态词和主次动作。
  2. 定义 state、terminal、flow_id、business_ref、retryable、next_action、support_ref、summary；由服务端生成脱敏摘要。
  3. 支付返回后先进入“正在确认”，按 1、2、4、6、8 秒查询服务端；超时仍 unknown 时显示“结果确认中”和“请勿重复支付”。
  4. 表单可修正错误回原页并保留全部输入；创建成功用 replace 到可刷新、可深链的结果页，Back 不返回提交页。
  5. 异步任务显示当前阶段和稍后查看；批次按 total = success + failed + skipped 展示，仅重试失败项。
  6. 失败页说明业务事实、副作用、可重试性和支持编号；会话过期重新认证后安全回跳，网络未知只重试查询。
  7. 结果页进入、终态变化、轮询超时、恢复动作和支持编号写入可观测事件，敏感字段脱敏。
- 失败模式与恢复：只凭 HTTP 200、SDK 回执或跳转显示成功；pending/unknown 被当失败；重新支付造成重复扣款；校验失败清空表单；Back 重复提交；部分成功被显示为全成功；结果页依赖上一步内存。
- 测试与证据：tests.test_function_catalog 8/8、tests.test_validate_handoff 8/8、tests.test_skill_automation 9/9；功能目录 HTML 构建并通过 node --check；validate_skill_package.py PASS。目标项目仍需覆盖沙箱支付回放、前后台切换、迟到回调、越权读取、重复提交和批次部分失败。
- 可复用代码：references/result-patterns.md、references/function-catalog.json、references/function-catalog.html。
- 维护责任与复查条件：支付渠道语义、平台导航/history 能力、结果 schema 或新出现的结果类型变化时更新 EXP-0003 与本 playbook。
