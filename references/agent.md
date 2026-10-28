# Intelligent agent implementation

Start with `../agent/README.md`, then read the service specification and event protocol. The agent interface reference is in the paired Design skill at `$LUZ_DESIGN_ROOT/references/system/agents.md`.

## Boundary

The agent may interpret intent, retrieve approved knowledge, choose registered tools, present confirmation, and guide the user back to the UI. It may not bypass authentication, permissions, confirmation, budgets, idempotency, or transaction rules.

## Tool registration

A tool is executable only when:

- its backing API exists and is marked safe for agent use;
- its input and output schemas are explicit;
- its risk class is recorded;
- its timeout, retry, circuit breaker, and channel restrictions are configured;
- it filters data by the authenticated tenant, store scope, and user;
- its result is sanitized before entering the model context.

High-impact operations require a server-generated preview, explicit confirmation, expiry, and idempotency. The confirmation must render server facts rather than model-created numbers.

## Graph and limits

Use explicit graph nodes for input guarding, classification, planning, tool execution, confirmation, verification, and response. Reset turn-scoped state. Bound tool calls, repeated arguments, tokens, time, and cost. Distinguish recoverable dependency failures from invalid user input.

## Retrieval

Keep live prices, inventory, order status, and other volatile facts out of static knowledge sources. Retrieve policy and explanatory content with tenant and validity filters. Cite product-approved sources, and degrade when retrieval is unavailable rather than inventing an answer.

## Streaming and recovery

Use the existing event protocol and monotonically increasing sequence. Persist enough event state for reconnection. Prevent concurrent turns in one conversation. A retry or replay must not repeat a confirmed write.

## Evaluation

Test intent and slot extraction, tool choice and arguments, authorization, confirmation, duplicate delivery, injection attempts, unavailable dependencies, step limits, numeric grounding, citations, and page-entry presence. Track failure rates and cost per completed task.
