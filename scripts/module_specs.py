#!/usr/bin/env python3
"""Module detailed design: the reusable library (references/module-specs/) and its rendering.

Each module spec describes one business module the way a backend design document does:

  features    功能说明      [{name, description}]
  flows       业务流程      [{name, steps: [...]}]
  inputs      页面与输入    [{page, field, label, type, required, rule}]
  apis        接口          [{method, path, caller, purpose, request, response, errors}]
  results     结果与跳转    [{case, page, display}]
  writes      数据写入      [{when, table, content}]
  tables      数据表        [{name, label, purpose, fields: [{name, type, required, default, comment}], indexes: [...]}]
  states      状态流转      [{entity, from, event, to, condition}]
  exceptions  异常与边界    [{case, handling}]

A new plan copies the specs whose `modules` intersect the plan's modules; the agent then adapts them
to the project. The library is canonical; plans own their copies.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
from luz_shell import inline  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "references" / "module-specs"
FRAMEWORK_TABLES = {"outbox_events", "processed_events", "idempotency_records", "audit_logs"}
FIELD_TYPE = re.compile(r"^(BIGINT|INTEGER|SMALLINT|BOOLEAN|DATE|TIMESTAMPTZ|TEXT|JSONB|UUID|VARCHAR\(\d+\)|CHAR\(\d+\)|NUMERIC\(\d+,\d+\))(\[\])?$")
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "MQTT", "TASK", "CALLBACK"}
PARTS = (
    ("features", "功能说明"), ("flows", "业务流程"), ("inputs", "页面与输入"), ("apis", "接口"),
    ("results", "结果与页面跳转"), ("tables", "数据表"), ("states", "状态流转"),
    ("exceptions", "异常与边界"),
)
REQUIRED_PARTS = ("features", "flows", "apis", "tables", "exceptions")
PLACEHOLDER = re.compile(r"待填写|待补充|TODO|TBD|\{\{")


def load_library(root: Path = LIBRARY) -> list[dict[str, Any]]:
    specs = []
    for path in sorted(root.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec.setdefault("id", path.stem)
        specs.append(spec)
    return sorted(specs, key=lambda s: (s.get("order", 999), s["id"]))


def specs_for(modules: Iterable[str], library: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    wanted = set(modules)
    return [copy.deepcopy(s) for s in (library if library is not None else load_library())
            if wanted.intersection(s.get("modules", []))]


def _texts(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _texts(item)


def validate_spec(spec: Mapping[str, Any]) -> list[str]:
    at = f"模块 {spec.get('id', '?')}"
    errors = []
    for field in ("id", "name", "summary", "modules"):
        if not spec.get(field):
            errors.append(f"{at} 缺少 {field}")
    for part in REQUIRED_PARTS:
        if not spec.get(part):
            errors.append(f"{at} 缺少「{dict(PARTS)[part]}」")
    if spec.get("userFacing", True) and not spec.get("results"):
        errors.append(f"{at} 缺少「结果与页面跳转」")
    for flow in spec.get("flows", []):
        if not flow.get("name") or not flow.get("steps"):
            errors.append(f"{at} 的业务流程需要名称与步骤")
    for api in spec.get("apis", []):
        if api.get("method") not in METHODS or not str(api.get("path", "")).strip():
            errors.append(f"{at} 的接口 {api.get('method')} {api.get('path')} 方法或路径无效")
        for field in ("purpose", "request", "response"):
            if not str(api.get(field, "")).strip():
                errors.append(f"{at} 的接口 {api.get('path')} 缺少 {field}")
    for table in spec.get("tables", []):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", str(table.get("name", ""))):
            errors.append(f"{at} 的表名无效：{table.get('name')}")
        if not table.get("fields"):
            errors.append(f"{at} 的表 {table.get('name')} 没有字段")
        names = set()
        for field in table.get("fields", []):
            if not re.fullmatch(r"[a-z][a-z0-9_]*", str(field.get("name", ""))):
                errors.append(f"{at}.{table.get('name')} 字段名无效：{field.get('name')}")
            if field.get("name") in names:
                errors.append(f"{at}.{table.get('name')} 字段重复：{field.get('name')}")
            names.add(field.get("name"))
            if not FIELD_TYPE.match(str(field.get("type", ""))):
                errors.append(f"{at}.{table.get('name')}.{field.get('name')} 类型无效：{field.get('type')}")
            if not str(field.get("comment", "")).strip():
                errors.append(f"{at}.{table.get('name')}.{field.get('name')} 缺少说明")
    for text in _texts(spec):
        if PLACEHOLDER.search(text):
            errors.append(f"{at} 含未完成内容：{text[:30]}")
            break
    return errors


def validate_specs(specs: list[Mapping[str, Any]], with_library: bool = False) -> list[str]:
    """Library check: with_library=False (every written table must be defined). Plan check: tables of
    library modules the plan leaves out are reported separately by unknown_write_tables()."""
    errors = []
    tables: dict[str, str] = {}
    apis: dict[tuple[str, str], str] = {}
    ids = set()
    for spec in specs:
        errors += validate_spec(spec)
        if spec.get("id") in ids:
            errors.append(f"模块 {spec.get('id')} 重复")
        ids.add(spec.get("id"))
        for table in spec.get("tables", []):
            if table.get("name") in tables:
                errors.append(f"表 {table.get('name')} 同时定义在 {tables[table['name']]} 与 {spec['id']}")
            tables[table.get("name")] = spec.get("id")
        for api in spec.get("apis", []):
            key = (api.get("method"), api.get("path"))
            if key in apis and api.get("method") not in {"TASK", "MQTT"}:
                errors.append(f"接口 {key[0]} {key[1]} 同时定义在 {apis[key]} 与 {spec['id']}")
            apis[key] = spec.get("id")
    library_tables = {t["name"] for spec in load_library() for t in spec.get("tables", [])} if with_library else set()
    for spec_id, name in unknown_write_tables(specs):
        if name not in library_tables:
            errors.append(f"模块 {spec_id} 写入未定义的表 {name}")
    return errors


def write_tables(write: Mapping[str, Any]) -> list[str]:
    return [name for name in re.split(r"[,，、\s]+", str(write.get("table", ""))) if name]


def unknown_write_tables(specs: list[Mapping[str, Any]], also_known: Iterable[str] = ()) -> list[tuple[str, str]]:
    """(spec id, table) pairs written by a spec but defined by none of the given specs (nor in also_known)."""
    known = {t.get("name") for spec in specs for t in spec.get("tables", [])} | FRAMEWORK_TABLES | set(also_known)
    return [(spec.get("id"), name) for spec in specs for write in spec.get("writes", [])
            for name in write_tables(write) if name not in known]


# ---------------------------------------------------------------- rendering

def _text(value: Any) -> str:
    return inline(str(value)).strip()


def _table(columns: list[str], rows: list[list[str]], dense: bool = False) -> str:
    head = "".join(f'<th scope="col">{html.escape(c)}</th>' for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    cls = ' class="dense"' if dense else ""
    return f'<div class="table-wrap"><table{cls}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _code(value: Any, nowrap: bool = False) -> str:
    cls = ' class="nw"' if nowrap else ""
    return f"<code{cls}>{html.escape(str(value))}</code>"


def render_part(part: str, spec: Mapping[str, Any]) -> str:
    items = spec.get(part) or []
    if not items:
        return ""
    if part == "features":
        return _table(["功能", "说明"], [[_text(i["name"]), _text(i["description"])] for i in items])
    if part == "flows":
        blocks = []
        for flow in items:
            steps = "".join(f"<li>{_text(step)}</li>" for step in flow["steps"])
            blocks.append(f"<p><strong>{_text(flow['name'])}</strong></p><ol>{steps}</ol>")
        return "".join(blocks)
    if part == "inputs":
        rows = [[_text(i.get("page", "")), f"{_text(i['label'])}<br>{_code(i['field'])}", _text(i.get("type", "")),
                 "是" if i.get("required") else "否", _text(i.get("rule", ""))] for i in items]
        return _table(["页面", "字段", "填写方式", "必填", "校验规则"], rows)
    if part == "apis":
        rows = [[f"{_code(i['method'], True)} {_code(i['path'], True)}<br>{_text(i.get('caller', ''))} · {_text(i['purpose'])}",
                 _text(i["request"]), _text(i["response"]), _text(i.get("errors", "—"))] for i in items]
        return _table(["接口与用途", "请求", "返回", "错误码"], rows, dense=True)
    if part == "results":
        return _table(["场景", "显示页面", "页面内容与下一步"], [[_text(i["case"]), _text(i["page"]), _text(i["display"])] for i in items])
    if part == "writes":
        return _table(["时机", "数据表", "写入内容"], [[_text(i["when"]), _code(i["table"]), _text(i["content"])] for i in items])
    if part == "tables":
        blocks = []
        for table in items:
            rows = [[_code(f["name"], True), _code(f["type"], True), "是" if f.get("required") else "否",
                     _text(f.get("default", "—")) or "—", _text(f["comment"])] for f in table["fields"]]
            header = f"<p><strong>{_code(table['name'])} {_text(table.get('label', ''))}</strong>　{_text(table.get('purpose', ''))}</p>"
            indexes = ""
            if table.get("indexes"):
                indexes = "<ul>" + "".join(f"<li>{_text(i)}</li>" for i in table["indexes"]) + "</ul>"
            blocks.append(header + _table(["字段", "类型", "必填", "默认值", "说明"], rows, dense=True) + indexes)
        return "".join(blocks)
    if part == "states":
        rows = [[_text(i.get("entity", "")), _text(i["from"]), _text(i["event"]), _text(i["to"]), _text(i.get("condition", "—"))] for i in items]
        return _table(["对象", "当前状态", "事件", "下一状态", "条件"], rows)
    if part == "exceptions":
        return _table(["情况", "处理"], [[_text(i["case"]), _text(i["handling"])] for i in items])
    return ""


def render_spec(spec: Mapping[str, Any]) -> tuple[str, str]:
    """Return (title, body); the page renderer numbers h2 and h3 headings.

    The summary is shown once, in the module overview; data writes are already in the flows and tables.
    """
    parts = []
    for part, title in PARTS:
        body = render_part(part, spec)
        if body:
            parts.append(f"<h3>{title}</h3>{body}")
    return str(spec["name"]), "".join(parts)


def render_module_overview(specs: list[Mapping[str, Any]]) -> str:
    rows = [[f'<a href="#module-{html.escape(str(s["id"]), quote=True)}">{_text(s["name"])}</a>', _text(s["summary"])] for s in specs]
    return _table(["模块", "职责"], rows)


def render_data_overview(specs: list[Mapping[str, Any]]) -> str:
    conventions = [
        "数据库为 PostgreSQL；表名与字段名使用小写下划线；每张表都有 `id BIGINT` 主键、`created_at TIMESTAMPTZ`、`updated_at TIMESTAMPTZ`，模块字段表不再列出这三列。",
        "金额一律为整数“分”，字段以 `_cents` 结尾；时间存带时区的时间戳，按门店时区展示。",
        "多门店数据带 `store_id`，查询在数据访问层统一附加门店范围；业务删除使用 `deleted_at` 软删除。",
        "状态字段只通过状态机条件更新修改；资金、次数、库存类变化写只追加的流水表。",
        "手机号、证件号等敏感字段加密存储，另存检索值与脱敏值；日志与导出只出现脱敏值。",
    ]
    return "<h3>统一约定</h3><ul>" + "".join(f"<li>{_text(c)}</li>" for c in conventions) + "</ul>"


def render_api_overview(specs: list[Mapping[str, Any]]) -> str:
    parts = ['<ul>'
             '<li>小程序接口前缀 <code>/api/v1/app</code>，需要会员登录令牌；后台接口前缀 <code>/api/v1/admin</code>，按权限码校验；'
             '设备接口前缀 <code>/api/v1/device</code>，使用设备凭证。</li>'
             '<li>写接口必须携带 <code>Idempotency-Key</code>；返回统一为 <code>{code, message, data, trace_id}</code>，code 为 0 表示成功。</li>'
             '<li>分页使用游标：请求 <code>cursor</code>、<code>limit</code>（不超过 50），返回 <code>next_cursor</code>。</li></ul>']
    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验或列出模块详细设计库")
    parser.add_argument("command", choices=("check", "list"))
    args = parser.parse_args(argv)
    library = load_library()
    if args.command == "list":
        for spec in library:
            print(f"{spec['id']:<16} {spec['name']:<12} 覆盖模块 {','.join(spec['modules'])}  表 {len(spec.get('tables', []))}  接口 {len(spec.get('apis', []))}")
        return 0
    errors = validate_specs(library)
    print(json.dumps({"ok": not errors, "modules": len(library), "errors": errors}, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
