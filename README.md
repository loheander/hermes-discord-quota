# hermes-discord-quota

Per-user spending quota plugin for Hermes Agent on Discord.

Enforces configurable daily and weekly spending budgets (in USD) per Discord user ID with stateless window resets, 90% warning footers, mid-turn hard blocks, unlimited exemptions, and instant zero-LLM rejections.

## Key Features

- **Dual Stateless Reset Windows**:
  - Daily budget resetting at 12:00 Europe/Stockholm every day.
  - Weekly budget resetting at 12:00 Europe/Stockholm every Friday.
- **USD Spending Limits**: Configured in `~/.hermes/config.yaml` with global defaults and per-user overrides.
- **90% Deterministic Warning Footers**: Automatically appends a zero-LLM usage warning footer to responses when a user exceeds 90% of their daily or weekly limit.
- **Media Charging**: `image_generate` and `video_generate` spend (provider-reported cost from the tool result, via `post_tool_call`) is charged to the Discord user's quota as `media` rows; free providers record $0 and results without a cost record $0 with a warning.
- **Mid-Turn Hard Block**: Halts tool calls via `pre_tool_call` and interrupts the turn if a running turn exhausts the user's quota mid-execution.
- **Special Quota Values**:
  - `negative` (e.g. `-1.0`): Unlimited quota (bypass limits).
  - `0.0`: Blocked immediately.
- **Zero-LLM Canned Explanations**: When a quota is exceeded prior to a turn, the bot responds directly with a formatted rejection stating current spend, limit, and next reset time without invoking model inference, while still allowing `/quota`, `/help`, `/whoami`, and `/stop`.
- **Standalone Hermes Plugin**: Built on native Hermes lifecycle hooks (`pre_gateway_dispatch`, `post_api_request`, `subagent_stop`, `pre_tool_call`, `transform_llm_output`) without patching core code.

## Configuration

Set limits under `discord_quota` in `~/.hermes/config.yaml` (global defaults, per-user overrides, timezone, warn threshold; see PLAN.md). Set `free_providers: [vertex]` to count calls answered by those providers (e.g. credit-covered) as $0; a call 429'd on vertex and answered by a fallback provider is still charged. Spend is stored in `~/.hermes/discord_quota.db`.

## Admin CLI

```
hermes quota status [user_id]
hermes quota reset <user_id> [--daily] [--weekly]
hermes quota credit <user_id> <amount_usd>
```

- `status` shows each window's spend, limit, state and reset time plus the latest 10 transactions; without a user id it prints one summary line per user.
- `reset` inserts an offsetting `admin_reset` credit so the window reads exactly 0 (the ledger is never edited or deleted). With no flag both windows are reset. A daily reset also lowers the weekly total (the spend is removed). A weekly reset leaves the daily window alone, except from Friday 12:00 until the next daily reset, when both windows start together and both are zeroed (the command says so). Windows already at or below 0 get no row.
- `credit` inserts a negative `admin_credit` row at the current time; the amount must be a positive number (at least 0.000001). It lowers both the daily and weekly totals and can take them below zero.

Changes apply immediately, also to a user who was blocked for exceeding the quota.

## Documentation

See [PLAN.md](PLAN.md) for the detailed architectural specification and implementation roadmap.
