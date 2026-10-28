# WeChat mini-program implementation

Read the approved mini-program design specification and the project's `HANDOFF.json` before changing UI.

## Defaults

- Native WeChat Mini Program.
- TDesign Mini Program or Vant Weapp for behavior and accessibility when needed; override appearance through product tokens.
- No platform design language may replace the project's approved visual system.
- API access through one request layer with authentication refresh, error normalization, retries where safe, and request cancellation.

## App structure

```text
apps/miniprogram/
  app.ts
  app.json
  styles/
    tokens.wxss
  components/
  pages/
  services/
    api/
    auth/
    cache/
  utils/
  tests/
```

Keep package boundaries aligned with the product lifecycle. Use subpackages when initial load, release ownership, or platform limits justify them.

## Screens and navigation

- Preserve the design's screen IDs and route relationships.
- Respect the custom navigation/capsule safe area when the design uses it.
- Handle deep links, resumed sessions, back navigation, date expiry, and account switching.
- Do not place permanent controls under the native capsule or keyboard.
- Confirm destructive or expensive actions before execution and preserve drafts on interruption.

## Data and state

Every screen implements its handoff state matrix. Normalize API errors into stable product states. The request layer owns authentication expiry, timeout, rate limits, and retry policy; page code owns rendering and recovery.

Use local storage only for non-sensitive preferences, drafts, and cache with explicit expiry. Sensitive tokens and payment details stay in platform-supported secure storage and are never logged.

## Performance

- Keep the initial package small; defer noncritical surfaces.
- Resize and compress images to the display target. Reserve layout space to avoid jumps.
- Cache repeatable catalog/content requests with TTL and invalidation.
- Measure cold launch, first meaningful content, interaction latency, and scroll smoothness.
- Avoid work in scroll handlers that can be scheduled or batched.

## Platform integrations

Wrap login, payment, map, subscription messages, camera/scan, location, share, customer service, and file operations behind adapters with test doubles. Verify real-device behavior for each enabled integration.

## Required tests

- Request envelope, token refresh, timeout, offline, rate-limit, and server error
- Screen state matrix for core flows
- Navigation, deep link, and recovery behavior
- Cart/order/payment idempotency and stale-state handling
- Accessibility labels and focus behavior where supported
- Performance budget for the release target
