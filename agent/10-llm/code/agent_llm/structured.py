"""结构化输出助手（SPEC §10 末段）：解析失败时把校验错误回传给模型重试，最多 2 次，仍失败抛 StructuredOutputError。"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(Exception):
    def __init__(self, model: type[BaseModel], attempts: int, last_error: str):
        self.model = model
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"{model.__name__}: failed after {attempts} attempts: {last_error}")


def _coerce(raw: Any, model: type[T]) -> T:
    if isinstance(raw, model):
        return raw
    if isinstance(raw, BaseModel):
        raw = raw.model_dump()
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        raw = json.loads(text)
    return model.model_validate(raw)


def structured_output(fn: Callable[[str, type[T]], Any], model: type[T], prompt: str,
                      max_retries: int = 2) -> T:
    """调用 fn(prompt, model) 得到原始输出（dict / JSON 字符串 / 模型实例），校验失败则附上错误重试。"""
    attempts = 0
    current = prompt
    last_error = ""
    while attempts <= max_retries:
        attempts += 1
        raw = fn(current, model)
        try:
            return _coerce(raw, model)
        except (ValidationError, ValueError, TypeError) as exc:
            last_error = str(exc)[:500]
            current = (f"{prompt}\n\n上一次输出未通过校验：{last_error}\n"
                       f"请只输出符合以下 JSON Schema 的对象：{json.dumps(model.model_json_schema(), ensure_ascii=False)}")
    raise StructuredOutputError(model, attempts, last_error)
