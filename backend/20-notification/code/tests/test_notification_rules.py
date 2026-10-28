"""纯规则：关键词类型格式化 / 截断、模板渲染、静默时段、错误码分类、推送事件解析。"""
from datetime import date, datetime, time, timezone

import pytest

from notification import rules
from notification.rules import FormatError, MissingField, format_keyword as fk


# ---------------------------------------------------------------- 关键词类型

@pytest.mark.parametrize("key,kind", [("thing1", "thing"), ("character_string2", "character_string"),
                                      ("number01", "number"), ("phone_number3", "phone_number"),
                                      ("car_number12", "car_number"), ("phrase5", "phrase")])
def test_keyword_type(key, kind):
    assert rules.keyword_type(key) == kind


@pytest.mark.parametrize("key", ["thing", "foo1", "Thing1", "thing123", ""])
def test_keyword_type_unknown(key):
    with pytest.raises(FormatError):
        rules.keyword_type(key)


def test_thing_truncates_to_20_with_ellipsis():
    value = "超长的商品名称超长的商品名称超长的商品名称ABC"
    out = fk("thing1", value)
    assert len(out) == 20 and out.endswith("…") and out.startswith("超长的商品名称")
    assert fk("thing1", "刚好二十个字刚好二十个字刚好二十个字刚好") == "刚好二十个字刚好二十个字刚好二十个字刚好"


def test_thing_strips_control_chars_and_newlines():
    assert fk("thing1", "剁椒\n鱼头​\t大份\x00") == "剁椒 鱼头 大份"


@pytest.mark.parametrize("value", ["", "   ", None, "\n​"])
def test_empty_values_rejected(value):
    with pytest.raises(FormatError):
        fk("thing1", value)


def test_bool_rejected():
    with pytest.raises(FormatError):
        fk("number1", True)


def test_phrase():
    assert fk("phrase1", "配送中") == "配送中"
    assert fk("phrase1", "已经配送完成了") == "已经配送完"
    for bad in ("ok", "配送1", "配送 中"):
        with pytest.raises(FormatError):
            fk("phrase1", bad)


def test_number():
    assert fk("number1", 12) == "12"
    assert fk("number1", "12.50") == "12.50"
    assert fk("number1", 2.5) == "2.5"
    for bad in ("12a", "-3", "1" * 33):
        with pytest.raises(FormatError):
            fk("number1", bad)


def test_character_string_not_truncated():
    assert fk("character_string1", "NO-2026_10#28") == "NO-2026_10#28"
    for bad in ("订单1", "A" * 33, "A B"):
        with pytest.raises(FormatError):
            fk("character_string1", bad)


def test_amount():
    assert fk("amount1", 1234) == "¥12.34"
    assert fk("amount1", 5) == "¥0.05"
    assert fk("amount1", "12.5元") == "¥12.5元"
    assert fk("amount1", "￥100") == "￥100"
    for bad in (-1, 10 ** 11, "12,5", "abc", "¥12345678901"):
        with pytest.raises(FormatError):
            fk("amount1", bad)


def test_time_and_date():
    moment = datetime(2026, 10, 28, 2, 5, tzinfo=timezone.utc)
    assert fk("time1", moment) == "2026年10月28日 10:05"
    assert fk("time1", time(9, 3)) == "09:03"
    assert fk("time1", "15:01") == "15:01"
    assert fk("time1", "2019年10月1日 15:01") == "2019年10月1日 15:01"
    assert fk("time1", "08:00~09:30") == "08:00~09:30"
    assert fk("date1", date(2026, 1, 5)) == "2026年1月5日"
    assert fk("date1", "2018-01-01") == "2018-01-01"
    assert fk("date1", "10月1日") == "10月1日"
    assert fk("date1", moment) == "2026年10月28日 10:05"
    for bad in ("25:00", "tomorrow", "10点"):
        with pytest.raises(FormatError):
            fk("time1", bad)
    with pytest.raises(FormatError):
        fk("date1", "next week")


def test_phone_car_name_symbol_letter_enum():
    assert fk("phone_number1", "+86-0766-66888866") == "+86-0766-66888866"
    with pytest.raises(FormatError):
        fk("phone_number1", "1" * 18)
    assert fk("car_number1", "粤a12345") == "粤A12345"
    for bad in ("粤A1234567", "A粤1234"):
        with pytest.raises(FormatError):
            fk("car_number1", bad)
    assert fk("name1", "张三丰李四王五赵六钱七孙八") == "张三丰李四王五赵六钱"
    assert fk("name1", "John Smith-Brown Junior III") == "John Smith-Brown Jun"
    assert fk("name1", "张John") == "张John"
    with pytest.raises(FormatError):
        fk("name1", "张三1")
    assert fk("symbol1", "#%&") == "#%&"
    with pytest.raises(FormatError):
        fk("symbol1", "a#")
    assert fk("letter1", "ABC") == "ABC"
    with pytest.raises(FormatError):
        fk("letter1", "AB1")
    assert fk("enum1", "已完成", enum_values=["已完成", "已取消"]) == "已完成"
    with pytest.raises(FormatError):
        fk("enum1", "其他", enum_values=["已完成"])


# ---------------------------------------------------------------- 映射与渲染

def test_build_subscribe_data_and_default():
    mapping = [{"key": "thing1", "source": "{goods_name} x{qty}"}, {"key": "amount2", "source": "amount_cents"},
               {"key": "thing3", "source": "{store_name}", "default": "门店"}]
    out = rules.build_subscribe_data(mapping, {"goods_name": "鱼", "qty": 2, "amount_cents": 100}, "Asia/Shanghai")
    assert out == {"thing1": {"value": "鱼 x2"}, "amount2": {"value": "¥1.00"}, "thing3": {"value": "门店"}}
    with pytest.raises(MissingField):
        rules.build_subscribe_data([{"key": "thing1", "source": "goods_name"}], {}, "Asia/Shanghai")


@pytest.mark.parametrize("tpl", ["{a.b}", "{a[0]}", "{a!r}", "{a:>10}", "{0}", "{"])
def test_render_rejects_unsafe_placeholders(tpl):
    with pytest.raises(ValueError):
        rules.placeholders(tpl)


def test_render_missing_field():
    assert rules.render_text("订单 {no}", {"no": 1}) == "订单 1"
    with pytest.raises(MissingField):
        rules.render_text("订单 {no}", {})


def test_validate_mapping():
    ok = rules.validate_subscribe_mapping([{"key": "thing1", "source": "x"}, {"key": "enum2", "source": "s", "enum": ["a"]}])
    assert ok[1]["enum"] == ["a"]
    for bad in ([], [{"key": "foo1", "source": "x"}], [{"key": "thing1", "source": "x"}, {"key": "thing1", "source": "y"}],
                [{"key": "enum1", "source": "x"}], [{"key": "thing1", "source": "a b"}], [{"key": "thing1"}]):
        with pytest.raises(ValueError):
            rules.validate_subscribe_mapping(bad)
    with pytest.raises(ValueError):
        rules.validate_sms_params([{"name": "1x", "source": "a"}])


# ---------------------------------------------------------------- 静默时段

def test_quiet_hours_cross_midnight():
    start, end = time(22), time(8)
    assert rules.in_quiet_hours(datetime(2026, 1, 1, 23, 0), start, end)
    assert rules.in_quiet_hours(datetime(2026, 1, 1, 7, 59), start, end)
    assert not rules.in_quiet_hours(datetime(2026, 1, 1, 8, 0), start, end)
    assert not rules.in_quiet_hours(datetime(2026, 1, 1, 12, 0), time(12), time(12))
    assert rules.in_quiet_hours(datetime(2026, 1, 1, 13, 0), time(12), time(14))


def test_quiet_release_in_store_timezone():
    # 23:30 北京 = 15:30 UTC → 次日 08:00 北京 = 00:00 UTC
    release = rules.quiet_release(datetime(2026, 10, 28, 15, 30, tzinfo=timezone.utc), "Asia/Shanghai", time(22), time(8))
    assert release == datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)
    # 06:00 北京 → 当天 08:00
    release = rules.quiet_release(datetime(2026, 10, 27, 22, 0, tzinfo=timezone.utc), "Asia/Shanghai", time(22), time(8))
    assert release == datetime(2026, 10, 28, 0, 0, tzinfo=timezone.utc)
    assert rules.quiet_release(datetime(2026, 10, 28, 2, 0, tzinfo=timezone.utc), "Asia/Shanghai", time(22), time(8)) is None


# ---------------------------------------------------------------- 错误码

@pytest.mark.parametrize("code,outcome", [
    (0, "sent"), (-1, "retry"), (43108, "retry"), (40001, "retry"), (45009, "defer_day"), (43101, "no_quota"),
    (47003, "permanent"), (40037, "permanent"), (43107, "permanent"), (45168, "permanent"), (40003, "permanent"),
    (99999, "permanent"),
])
def test_classify_wechat(code, outcome):
    assert rules.classify_wechat(code) == outcome


def test_classify_sms_and_backoff():
    assert rules.classify_sms("") == "sent"
    assert rules.classify_sms("LimitExceeded.PhoneNumberDailyLimit") == "permanent"
    assert rules.classify_sms("isv.BUSINESS_LIMIT_CONTROL") == "permanent"
    assert rules.classify_sms("InternalError.Timeout") == "retry"
    assert rules.classify_sms("isv.MOBILE_NUMBER_ILLEGAL") == "permanent"
    assert rules.classify_sms("whatever", retryable=True) == "retry"
    schedule = (60, 300, 1800)
    assert [rules.backoff_seconds(n, schedule) for n in (1, 2, 3, 9)] == [60, 300, 1800, 1800]


# ---------------------------------------------------------------- 推送事件

def test_parse_popup_single_object_and_array():
    single = {"FromUserName": "o-1", "CreateTime": 100, "Event": "subscribe_msg_popup_event",
              "List": {"TemplateId": "T1", "SubscribeStatusString": "accept", "PopupScene": "0"}}
    items = rules.parse_subscribe_event(single)
    assert len(items) == 1 and items[0].template_id == "T1" and items[0].status == "accept" and items[0].event == "popup"
    array = {**single, "List": [{"TemplateId": "T1", "SubscribeStatusString": "accept"},
                                {"TemplateId": "T2", "SubscribeStatusString": "reject"}]}
    assert [(i.template_id, i.status) for i in rules.parse_subscribe_event(array)] == [("T1", "accept"), ("T2", "reject")]


def test_parse_xml_style_wrapper_and_sent_event():
    xml_like = {"FromUserName": "o-1", "CreateTime": "200", "Event": "subscribe_msg_sent_event",
                "SubscribeMsgSentEvent": {"List": {"TemplateId": "T1", "MsgID": "123", "ErrorCode": "10005",
                                                   "ErrorStatus": "fail"}}}
    items = rules.parse_subscribe_event(xml_like)
    assert items[0].event == "sent" and items[0].msg_id == "123" and items[0].error_code == 10005
    assert items[0].create_time == 200


def test_parse_ignores_other_events_and_bad_items():
    assert rules.parse_subscribe_event({"Event": "user_enter_tempsession"}) == []
    assert rules.parse_subscribe_event({"Event": "subscribe_msg_change_event", "List": [{"x": 1}, "junk"]}) == []
