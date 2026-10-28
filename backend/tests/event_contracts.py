"""事件契约提取：从生产者源码（AST）得到“每个主题实际发出的字段”，供 test_event_contracts.py 驱动订阅者。

只认 `enqueue_event(db, <topic>, <payload>, ...)` 这一种发布方式（SPEC：发件箱同事务发布）：
- topic：字符串常量，或模块级常量（`REFUND_TOPIC = "refund.requested"`），或辅助函数的参数（见下）；
- payload：字典字面量；同一函数里 `payload = {...}` 的变量（含后续 `payload["k"] = ...` / `payload.update(k=...)`）；
  或返回字典字面量的同模块函数（如 lottery 的 `prize_won_payload(...)`）。
- 辅助函数：`def _event(db, topic, obj, **extra): payload = {...}; payload.update(extra); enqueue_event(db, topic, payload)`，
  调用点 `_event(db, "after_sale.completed", case, lines=...)` 的常量主题 + 关键字参数并入字段。
字典里的 `**spread` 无法静态展开时记为 open=True（字段集合可能更多）。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


@dataclass
class Produced:
    topic: str
    keys: set[str] = field(default_factory=set)
    open: bool = False
    sources: set[str] = field(default_factory=set)


def _pkg_files() -> list[tuple[str, Path]]:
    out = []
    for code_dir in sorted(BACKEND.glob("*/code")):
        for pkg in sorted(code_dir.iterdir()):
            if not pkg.is_dir() or not (pkg / "__init__.py").exists() or pkg.name in {"tests", "luzback"}:
                continue
            for path in sorted(pkg.rglob("*.py")):
                if "tests" not in path.parts:
                    out.append((pkg.name, path))
    return out


def _dict_keys(node: ast.AST) -> tuple[set[str], bool]:
    keys: set[str] = set()
    open_ = False
    if isinstance(node, ast.Dict):
        for k in node.keys:
            if k is None:
                open_ = True
            elif isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
            else:
                open_ = True
    elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "dict":
        keys |= {kw.arg for kw in node.keywords if kw.arg}
        open_ = any(kw.arg is None for kw in node.keywords) or bool(node.args)
    else:
        open_ = True
    return keys, open_


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


class _Module:
    def __init__(self, pkg: str, path: Path):
        self.pkg = pkg
        self.path = path
        self.tree = ast.parse(path.read_text(encoding="utf-8"))
        self.consts: dict[str, str] = {}
        self.dict_funcs: dict[str, tuple[set[str], bool]] = {}
        for node in self.tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        self.consts[t.id] = node.value.value
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                self.consts[node.target.id] = node.value.value
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Return) and sub.value is not None and isinstance(sub.value, ast.Dict):
                        self.dict_funcs[node.name] = _dict_keys(sub.value)
                        break


def _var_payload(func: ast.FunctionDef, name: str) -> tuple[set[str], bool, bool]:
    """函数内变量 name 的字典字段；第三项：是否 .update(**extra) 合并了 **kwargs 参数。"""
    keys: set[str] = set()
    open_ = False
    found = False
    merges_kwargs = False
    kwarg = func.args.kwarg.arg if func.args.kwarg else None
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            value = node.value
            if isinstance(value, ast.Dict) and kwarg:
                spread_kw = [i for i, (k_, v) in enumerate(zip(value.keys, value.values))
                             if k_ is None and isinstance(v, ast.Name) and v.id == kwarg]
                if spread_kw:  # {..., **extra}：extra 由调用点的关键字参数给出
                    merges_kwargs = True
                    value = ast.Dict(keys=[k_ for i, k_ in enumerate(value.keys) if i not in spread_kw],
                                     values=[v for i, v in enumerate(value.values) if i not in spread_kw])
            k, o = _dict_keys(value)
            keys |= k
            open_ |= o
            found = True
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Subscript):
            t = node.targets[0]
            if isinstance(t.value, ast.Name) and t.value.id == name and isinstance(t.slice, ast.Constant):
                keys.add(t.slice.value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("update", "setdefault") \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == name:
            if node.func.attr == "setdefault" and node.args and isinstance(node.args[0], ast.Constant):
                keys.add(node.args[0].value)
                continue
            keys |= {kw.arg for kw in node.keywords if kw.arg}
            for arg in node.args:
                if isinstance(arg, ast.Name) and arg.id == kwarg:
                    merges_kwargs = True
                elif isinstance(arg, ast.Dict):
                    keys |= _dict_keys(arg)[0]
                else:
                    open_ = True
            if any(kw.arg is None and isinstance(kw.value, ast.Name) and kw.value.id == kwarg for kw in node.keywords):
                merges_kwargs = True
    return keys, (open_ or not found), merges_kwargs


def collect() -> dict[str, Produced]:
    produced: dict[str, Produced] = {}
    helpers: dict[tuple[str, str], tuple[int, set[str], bool, bool]] = {}  # (pkg, func) -> (topic 参数位置, keys, open, merges)
    modules = [_Module(pkg, path) for pkg, path in _pkg_files()]

    def add(topic: str, keys: set[str], open_: bool, src: str) -> None:
        item = produced.setdefault(topic, Produced(topic))
        item.keys |= keys
        item.open |= open_
        item.sources.add(src)

    for mod in modules:
        for func in [n for n in ast.walk(mod.tree) if isinstance(n, ast.FunctionDef)]:
            params = [a.arg for a in func.args.args]
            for call in [n for n in ast.walk(func) if isinstance(n, ast.Call) and _call_name(n) == "enqueue_event"]:
                args = list(call.args)
                kw = {k.arg: k.value for k in call.keywords if k.arg}
                topic_node = args[1] if len(args) > 1 else kw.get("topic")
                payload_node = args[2] if len(args) > 2 else kw.get("payload")
                if topic_node is None or payload_node is None:
                    continue
                merges = False
                if isinstance(payload_node, ast.Name):
                    keys, open_, merges = _var_payload(func, payload_node.id)
                elif isinstance(payload_node, ast.Call) and _call_name(payload_node) in mod.dict_funcs:
                    keys, open_ = mod.dict_funcs[_call_name(payload_node)]
                else:
                    keys, open_ = _dict_keys(payload_node)
                src = f"{mod.pkg}:{mod.path.name}:{func.name}"
                if isinstance(topic_node, ast.Constant):
                    add(topic_node.value, keys, open_, src)
                elif isinstance(topic_node, ast.Name) and topic_node.id in mod.consts:
                    add(mod.consts[topic_node.id], keys, open_, src)
                elif isinstance(topic_node, ast.Name) and topic_node.id in params:
                    helpers[(mod.pkg, func.name)] = (params.index(topic_node.id), keys, open_, merges)
    for mod in modules:
        for call in [n for n in ast.walk(mod.tree) if isinstance(n, ast.Call)]:
            name = _call_name(call)
            spec = helpers.get((mod.pkg, name or ""))
            if spec is None:
                continue
            pos, keys, open_, merges = spec
            topic_node = call.args[pos] if len(call.args) > pos else None
            if topic_node is None:
                continue
            if isinstance(topic_node, ast.Constant) and isinstance(topic_node.value, str):
                topic = topic_node.value
            elif isinstance(topic_node, ast.Name) and topic_node.id in mod.consts:
                topic = mod.consts[topic_node.id]
            else:
                continue  # 再次转发的主题变量（嵌套辅助函数）在外层调用点处理
            extra = {k.arg for k in call.keywords if k.arg} if merges else set()
            add(topic, keys | extra, open_ or any(k.arg is None for k in call.keywords), f"{mod.pkg}:{name}")
    return produced


if __name__ == "__main__":  # pragma: no cover - 人工查看
    for t, p in sorted(collect().items()):
        print(f"{t:40s} {'(open) ' if p.open else ''}{sorted(p.keys)}  <- {sorted(p.sources)}")
