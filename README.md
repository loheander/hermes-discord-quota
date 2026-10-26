# hermes-discord-quota

Per-user spending quota plugin for Hermes Agent on Discord.

Enforces configurable daily and weekly spending budgets (in USD) per Discord user ID with stateless window resets, 90% warning footers, mid-turn hard blocks, unlimited exemptions, and instant zero-LLM rejections.

## Key Features

- **Dual Stateless Reset Windows**:
  - Daily budget resetting at 12:00 Europe/Stockholm every day.
  - Weekly budget resetting at 12:00 Europe/Stockholm every Friday.
- **USD Spending Limits**: Configured in `~/.hermes/config.yaml` with global defaults and per-user overrides.
- **90% Deterministic Warning Footers**: Automatically appends a zero-LLM usage warning footer to responses when a user exceeds 90% of their daily or weekly limit.
- **Mid-Turn Hard Block**: Halts tool calls via `pre_tool_call` and interrupts the turn if a running turn exhausts the user's quota mid-execution.
- **Special Quota Values**:
  - `negative` (e.g. `-1.0`): Unlimited quota (bypass limits).
  - `0.0`: Blocked immediately.
- **Zero-LLM Canned Explanations**: When a quota is exceeded prior to a turn, the bot responds directly with a formatted rejection stating current spend, limit, and next reset time without invoking model inference, while still allowing `/quota`, `/help`, `/whoami`, and `/stop`.
- **Standalone Hermes Plugin**: Built on native Hermes lifecycle hooks (`pre_gateway_dispatch`, `post_api_request`, `subagent_stop`, `pre_tool_call`, `transform_llm_output`) without patching core code.

## Documentation

See [PLAN.md](PLAN.md) for the detailed architectural specification and implementation roadmap.
