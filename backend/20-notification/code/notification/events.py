"""25-jobs worker 加载的事件订阅表：{topic: handler(db, payload, event_id)}。

handler 不提交（worker 每事件一个会话，成功提交、异常回滚并退避重试）；幂等靠 notification_requests 去重键唯一。
主题与字段映射见 consumers.EVENT_BINDINGS（改名用 consumers.set_event_binding，本表同步更新）。
"""
from .consumers import SUBSCRIPTIONS

__all__ = ["SUBSCRIPTIONS"]
