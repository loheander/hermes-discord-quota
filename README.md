# hermes-discord-quota

Per-user spending quota plugin for Hermes Agent on Discord.

Enforces configurable daily and weekly spending budgets (in USD) per Discord user ID with carry-over deficit accounting, unlimited exemptions, and blocking capabilities.

## Key Features

- **Dual Reset Windows**:
  - Daily budget refilling at 12:00 Europe/Stockholm every day.
  - Weekly budget refilling at 12:00 Europe/Stockholm every Friday.
- **USD Spending Limits**: Configured in `~/.hermes/config.yaml` with global defaults and per-user overrides.
- **Carry-over Deficit Accounting**: Turns run to completion once admitted. Any overshoot carries over as a negative starting balance into the subsequent refill.
- **Special Quota Values**:
  - `negative` (e.g. `-1.0`): Unlimited quota (bypass limits).
  - `0.0`: Blocked immediately.
- **Zero-LLM Canned Explanations**: When a quota is exceeded, the bot responds directly with a formatted rejection stating current spend, limit, and next refill time without invoking model inference.
- **Standalone Hermes Plugin**: Built on top of native Hermes lifecycle hooks (`pre_gateway_dispatch`, `post_api_request`) without patching core code.

## Documentation

See [PLAN.md](PLAN.md) for the detailed architectural specification and implementation roadmap.
