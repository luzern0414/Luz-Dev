# Module detailed design library

One JSON file per business module. A new implementation plan copies the specs whose `modules` intersect the plan's modules into `raw/plan.json` → `moduleSpecs`; the agent then adapts the copy to the project (names, fields, rules, errors). The library stays generic; plans own their copies.

Each module renders as its own numbered chapter with these parts (empty parts are skipped):

| Part | Key | Content |
|---|---|---|
| 功能说明 | `features` | `{name, description}` — what the module offers |
| 业务流程 | `flows` | `{name, steps[]}` — numbered steps, naming the API called at each step |
| 页面与输入 | `inputs` | `{page, field, label, type, required, rule}` — what the user fills in and how it is validated |
| 接口 | `apis` | `{method, path, caller, purpose, request, response, errors}`; method also `CALLBACK`, `TASK`, `MQTT` |
| 结果与页面跳转 | `results` | `{case, page, display}` — which page shows after success, failure, pending |
| 数据写入 | `writes` | `{when, table, content}` — tables separated by `、`; every table must be defined in the plan |
| 数据表 | `tables` | `{name, label, purpose, fields[{name, type, required, default, comment}], indexes[]}` |
| 状态流转 | `states` | `{entity, from, event, to, condition}` |
| 异常与边界 | `exceptions` | `{case, handling}` — duplicates, concurrency, timeouts, refunds, abuse |

Conventions every spec follows (also rendered in the plan's 数据库设计 chapter):

- PostgreSQL; `id BIGINT`, `created_at`, `updated_at` exist on every table and are not listed.
- Money is integer cents in `*_cents` fields. Field types: `BIGINT INTEGER SMALLINT BOOLEAN DATE TIMESTAMPTZ TEXT JSONB UUID VARCHAR(n) CHAR(n) NUMERIC(p,s)`, optional `[]`.
- Status fields change only through conditional updates; balances, counts and stock change through append-only ledgers with a unique `(ref_type, ref_id, reason)`.
- API paths: `/api/v1/app` (member token), `/api/v1/admin` (permission code), `/api/v1/device` (device credential). Writes carry `Idempotency-Key`. Errors are stable upper-case codes.
- Text is page content: it must pass Luz Design's `check_document.py` (no 审核人, 发布时间, 校验通过, 以下, 请注意 …).

Commands:

```powershell
python -X utf8 scripts/module_specs.py check                 # validate the library
python -X utf8 scripts/module_specs.py list                  # modules, tables, APIs
python -X utf8 scripts/tech_plan.py spec NN --sync           # copy specs for modules not yet covered
python -X utf8 scripts/tech_plan.py spec NN --file x.json    # replace a spec with an adapted copy
python -X utf8 scripts/tech_plan.py spec NN --remove catalog # drop a spec that does not apply
```

A module a project does not need is removed from the plan, or its responsibilities are folded into another spec by adding the module id to that spec's `modules` (for example a gym sells cards, so `membership` also covers `catalog`).
