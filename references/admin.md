# Merchant admin implementation

The admin is an operational tool. Density, speed, and permission clarity matter more than matching the customer-facing screen one-for-one.

## Defaults

Vue 3, Vite, TypeScript, Pinia, Vue Router, Element Plus, and ECharts, unless an existing project has another approved stack.

## Shell

Implement the layout from the approved design: navigation, top bar, breadcrumbs where useful, global search or command entry if specified, notifications, account menu, and organization/store scope.

Routes declare authentication, role or permission requirements, title, and fallback. Permission checks in the client improve the experience; the server enforces access.

## Primary page patterns

- **Overview:** a small number of decisions, trends, alerts, and shortcuts. Every metric defines its source, time range, freshness, and failure state.
- **List:** URL-owned filters, cursor or page navigation, sort, selection, bulk actions, loading, empty, error, and export behavior.
- **Detail:** stable identity, status, summary, actions, history, related records, and permission-aware visibility.
- **Create/edit:** keyed form state, validation, unsaved-change protection, server error mapping, and success continuation.
- **Wizard:** short steps, resumable state, clear validation, and a review page.
- **Settings:** grouped changes, explicit save scope, audit of sensitive changes, and confirmation for destructive impact.

## Data tables

- Preserve filters and page state in the URL.
- Show server-side sorting and pagination where the dataset can grow.
- Use stable row identity.
- Distinguish selection across pages from selection on the current page.
- Explain partial bulk results and provide the result download.
- Virtualize only when measurement shows a list cannot remain responsive.

## Permissions and audit

Use the shared permission codes from the backend contract. Do not invent client-only codes. Render the action visibility and disabled explanation exactly as the approved design requires. The server remains authoritative and its denial is shown when policy changes after page load.

Audit sensitive reads and all consequential writes with actor, organization/store scope, target, change summary, result, trace ID, and time. Never render sensitive fields to users outside the approved scope.

## Required tests

- Login, session expiration, disabled account, and organization/store scope
- Route and action permission behavior
- List/filter/pagination/deep link and bulk partial failure
- Form validation and server conflict mapping
- Irreversible action confirmation and audit
- Dashboard metric loading, empty, stale, and failure states
