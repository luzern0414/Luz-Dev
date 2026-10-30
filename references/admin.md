# Merchant admin implementation

The page patterns, layout, list defaults (20 rows per page, choice of 10/20/50/100), centred floating panels for detail, edit and confirm, inline switches, header filters, version display and copy are designed in Luz System (`references/admin-design.html`, cards `admin-*`) and arrive in the approved session. This page covers what the implementation adds.

## Defaults

Vue 3, Vite, TypeScript, Pinia, Vue Router, Element Plus, and ECharts, unless an existing project has another approved stack. Appearance comes from the design tokens, not from the component library's defaults.

## Routes and permissions

- Routes declare authentication, permission code, title and fallback. Client checks only shape the interface; the server enforces access and its denial is shown when policy changes after page load.
- Use the permission codes of the backend contract (`<module>:<action>`, `backend/22-admin-rbac`); never invent client-only codes.
- Store scope comes from the session (`GET /api/v1/admin/auth/me`); every list and detail request is filtered by the server.

## Lists, filters and export

- Filters, sort and page live in the URL, so a refresh or a shared link restores the view.
- Server-side pagination with `page` / `page_size` (default 20, at most 100) and filters declared through `ListSpec` (`backend/SPEC.md` §3.4); unknown parameters return 4001.
- Selection across pages is distinct from selection on the current page; bulk actions report partial results and offer the failed rows for download.
- Export uses the same filter declaration as the list, runs asynchronously above 5,000 rows, masks sensitive fields and writes an audit record (ADMIN-002).
- Virtualize only when measurement shows a list cannot remain responsive.

## Writes and audit

- Writes carry `Idempotency-Key`; a version conflict (4090) reloads the record and keeps the user's input.
- Dangerous operations state object, count, amount and consequence before confirming (ADMIN-001); sensitive ones require re-authentication (`reauth_required`).
- Every consequential write and sensitive read is audited with actor, store scope, target, change summary, result, trace ID and time (OBS-002).
- The admin shows the front and back versions from the build and the version API and offers a refresh when a new version is deployed (REL-002). Stores have one-click open and close with impact preview and open check (ADMIN-004, module spec `store`).
- Texts and display settings the merchant maintains come from the content module (ADMIN-005); runtime parameters are never an admin page (OPS-005).

## Required tests

- Login, session expiry (idle 15 minutes, absolute 8 hours), disabled account, MFA and store scope
- Route and action permission behaviour, including denial after a permission change
- List, filter, pagination, deep link and bulk partial failure
- Form validation and server conflict mapping
- Dangerous-operation confirmation and audit
- Dashboard metric loading, empty, stale and failure states
