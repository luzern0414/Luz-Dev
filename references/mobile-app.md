# Mobile app implementation

Read the approved Luz System session and the project's `design-bindings.json` first (`references/design-integration.md`; `HANDOFF.json` only when the session ships one). A cross-platform toolkit does not remove platform-specific navigation, permission, accessibility, or release work.

## Stack selection

| Condition | Default |
|---|---|
| Greenfield, cross-platform product, no existing stack constraint | Flutter |
| Team already maintains a strong React/TypeScript product with shared Web expertise | React Native |
| Deep platform integration, constrained startup/performance, or a platform-specific release strategy dominates | Native Swift/Kotlin |
| Existing app stack | Preserve it unless migration has a measured payoff |

Record the decision and the capabilities that drove it.

## App architecture

Keep routing, feature modules, API clients, models, persistence, and platform adapters separate. Each feature owns its screens, state, API calls, and tests. Shared components implement product tokens and interaction rules; platform adapters expose camera, location, notifications, biometrics, secure storage, and files through testable interfaces.

Use one state pattern consistently. Avoid putting network or persistence logic directly in view widgets/components.

## Navigation and lifecycle

Implement the design's route map, deep links, tab restoration, system back, modal dismissal, and session recovery. Protect unsaved work across backgrounding and process death. Define what happens when an update or remote configuration makes a screen unavailable.

## Offline and synchronization

Only cache what the product can safely use offline. Persist mutations with explicit operation identity and retry semantics. Conflict resolution belongs to a documented strategy: server wins, client review, merge, or rejected operation. Never report a queued mutation as committed without distinguishing it.

## Platform behavior

- Safe areas, keyboards, orientation, text scaling, dark mode, and reduced motion
- Permission explanation, denied state, permanently denied state, and settings recovery
- Notification opt-in, foreground/background handling, and deep-link destination
- Camera, location, files, biometrics, and QR flows with denied/canceled recovery
- Store review, version gating, and upgrade behavior

## Testing

- Unit tests for domain behavior and state reducers
- Contract tests for API clients and generated models
- Integration tests for persistence, backgrounding, and deep links
- End-to-end tests for core journeys under supported platforms
- Accessibility and performance checks on representative devices

Run release builds and verify signing, environment configuration, app identifiers, privacy declarations, and store assets.
