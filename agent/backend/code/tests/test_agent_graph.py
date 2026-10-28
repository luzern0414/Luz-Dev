"""智能体编排测试：直接回答、只读工具、确认中断（同意/拒绝）、重试、用户注入防护、步数上限。"""
import pytest
from langgraph.types import Command

from agent_graph import (MAX_TOOL_CALLS, ScriptedDecider, ToolError, TransientToolError,
                         build_graph, default_registry)


class FakeClient:
    """替代后端 HTTP 客户端。记录每次调用，便于断言幂等键与 user_id。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.get_error = None
        self.post_failures = 0

    def get(self, path, params=None, user_id=None):
        self.calls.append({"method": "GET", "path": path, "user_id": user_id})
        if self.get_error:
            raise self.get_error
        if path.endswith("/products"):
            return {"items": [{"name": "剁椒鱼头", "price_cents": 8800}]}
        return {"status": "pending_payment", "total_cents": 8800}

    def post(self, path, json=None, user_id=None, idempotency_key=None):
        self.calls.append({"method": "POST", "path": path, "user_id": user_id, "key": idempotency_key})
        if self.post_failures:
            self.post_failures -= 1
            raise TransientToolError("upstream timeout")
        if path.endswith("/cancel"):
            return {"order_id": 5, "status": "cancelled"}
        return {"order_id": 5, "order_no": "A5", "total_cents": 17600}


def run(app, state, thread="t1"):
    return app.invoke(state, config={"configurable": {"thread_id": thread}})


def base_state(**extra):
    state = {"user_id": 1, "idem_base": "conv-1:turn-1",
             "messages": [{"role": "user", "content": "帮我点一份鱼头"}], "tool_calls_made": 0}
    state.update(extra)
    return state


def tool(name, **args):
    return lambda messages: {"type": "tool", "name": name, "args": args}


def answer(text):
    return {"type": "answer", "text": text}


def test_direct_answer_ends_without_tools():
    client = FakeClient()
    app = build_graph(ScriptedDecider([answer("你好，想吃点什么？")]), default_registry(client))
    result = run(app, base_state())
    assert result["final_text"] == "你好，想吃点什么？"
    assert client.calls == []


def test_read_tool_runs_and_result_goes_back_to_model():
    client = FakeClient()
    decider = ScriptedDecider([tool("search_products", keyword="鱼"), answer("有剁椒鱼头，88 元。")])
    app = build_graph(decider, default_registry(client))
    result = run(app, base_state())
    assert result["final_text"] == "有剁椒鱼头，88 元。"
    tool_msgs = [m for m in result["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["ok"] is True and tool_msgs[0]["content"]["items"][0]["name"] == "剁椒鱼头"


def test_high_risk_tool_pauses_for_confirmation_then_runs_with_idempotency_key():
    client = FakeClient()
    decider = ScriptedDecider([tool("create_order", items=[{"sku_id": 1, "qty": 2}]), answer("已下单，请去支付。")])
    app = build_graph(decider, default_registry(client))
    paused = run(app, base_state())
    assert "__interrupt__" in paused
    assert paused["__interrupt__"][0].value["action"] == "create_order"
    assert client.calls == []  # 确认前不得产生任何副作用

    done = app.invoke(Command(resume={"approved": True}), config={"configurable": {"thread_id": "t1"}})
    assert done["final_text"] == "已下单，请去支付。"
    post = [c for c in client.calls if c["method"] == "POST"]
    assert len(post) == 1
    assert post[0]["key"] == "conv-1:turn-1:create_order"


def test_rejected_confirmation_never_calls_backend():
    client = FakeClient()
    decider = ScriptedDecider([tool("cancel_order", order_id=5)])
    app = build_graph(decider, default_registry(client))
    run(app, base_state())
    done = app.invoke(Command(resume={"approved": False}), config={"configurable": {"thread_id": "t1"}})
    assert "已取消" in done["final_text"]
    assert client.calls == []


def test_transient_failure_is_retried_then_succeeds():
    client = FakeClient()
    client.post_failures = 2  # 前两次超时，第三次成功
    decider = ScriptedDecider([tool("create_order", items=[{"sku_id": 1, "qty": 1}]), answer("好了")])
    app = build_graph(decider, default_registry(client), sleep=lambda s: None)
    run(app, base_state())
    app.invoke(Command(resume={"approved": True}), config={"configurable": {"thread_id": "t1"}})
    assert len([c for c in client.calls if c["method"] == "POST"]) == 3
    assert client.calls[-1]["key"] == "conv-1:turn-1:create_order"


def test_permanent_error_is_not_retried():
    client = FakeClient()
    client.get_error = ToolError("order not found")
    decider = ScriptedDecider([tool("get_order_status", order_id=99), answer("没有找到这个订单。")])
    app = build_graph(decider, default_registry(client), sleep=lambda s: None)
    result = run(app, base_state())
    assert len(client.calls) == 1
    tool_msg = [m for m in result["messages"] if m["role"] == "tool"][0]
    assert tool_msg["ok"] is False and "order not found" in tool_msg["content"]


def test_model_cannot_inject_user_id():
    client = FakeClient()
    decider = ScriptedDecider([tool("get_order_status", order_id=5, user_id=999), answer("ok")])
    app = build_graph(decider, default_registry(client))
    run(app, base_state(user_id=1))
    assert client.calls[0]["user_id"] == 1


def test_missing_required_argument_is_reported_to_model():
    client = FakeClient()
    decider = ScriptedDecider([tool("search_products"), answer("请告诉我想搜什么。")])
    app = build_graph(decider, default_registry(client))
    result = run(app, base_state())
    assert client.calls == []
    tool_msg = [m for m in result["messages"] if m["role"] == "tool"][0]
    assert tool_msg["ok"] is False and "keyword" in tool_msg["content"]


def test_unknown_tool_is_refused():
    client = FakeClient()
    decider = ScriptedDecider([tool("delete_all_users"), answer("抱歉，我无法执行该操作。")])
    app = build_graph(decider, default_registry(client))
    result = run(app, base_state())
    assert client.calls == []
    assert any("不存在" in m["content"] for m in result["messages"] if m["role"] == "tool")


def test_step_limit_stops_runaway_loop():
    client = FakeClient()
    decider = ScriptedDecider([tool("search_products", keyword="x")] * (MAX_TOOL_CALLS + 5))
    app = build_graph(decider, default_registry(client))
    result = run(app, base_state())
    assert result["tool_calls_made"] == MAX_TOOL_CALLS
    assert "步骤较多" in result["final_text"]
