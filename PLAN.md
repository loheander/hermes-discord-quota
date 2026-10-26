# Implementation Plan: hermes-discord-quota

## 1. Executive Summary

This plugin introduces per-user USD spending controls for Discord interactions with Hermes Agent. It implements two independent stateless time-window buckets:
- **Daily Window**: Resets every calendar day at **12:00 Europe/Stockholm**.
- **Weekly Window**: Resets every Friday at **12:00 Europe/Stockholm**.

Key behaviors:
1. **Stateless Window Accounting (No Carry-Over)**: Balance in each window is strictly `quota - sum(spend_in_current_window)`. Every reset at 12:00 starts fresh with the full configured allowance.
2. **Pre-Turn Admission Gate**: Blocks turns before session acquisition or prompt assembly when remaining budget is $\le \$0.00$, replying with a zero-LLM canned explanation. Benign slash commands (`/quota`, `/help`, `/whoami`, `/stop`) bypass the block.
3. **Mid-Turn Hard Block**: If an active multi-step turn exhausts the user's daily or weekly budget mid-turn, subsequent tool executions are blocked via `pre_tool_call` and the running agent is hard-interrupted so runaway tool loops cannot continue accruing spend.
4. **Deterministic 90% Warning Footer**: Whenever a user's spend in either window reaches $\ge 90\%$ of their limit (or $\ge 100\%$ on the final response of a turn), `transform_llm_output` appends a deterministic, zero-LLM warning footer to the assistant's response.

---

## 2. Architecture & Design Principles

```
Inbound Discord Message
          │
          ▼
┌────────────────────────────────────────────────────────────┐
│  gateway/run_inbound.py                                    │
│    └─> pre_gateway_dispatch hook                           │
└─────────────────────────┬──────────────────────────────────┘
                          │
          ┌───────────────▼────────────────────┐
          │  hermes-discord-quota Plugin       │
          │  1. Platform == DISCORD?           │
          │  2. Bypass if /quota, /help, etc.  │
          │  3. Check Concurrency Lock         │
          │  4. Query Window Spend vs Limits   │
          └───────┬──────────────────┬─────────┘
                  │                  │
           Balance > 0         Balance <= 0 or Blocked (0.0)
                  │                  │
                  │                  ▼
                  │    ┌────────────────────────────────────────────┐
                  │    │ 1. Send canned explanation (thread-aware)  │
                  │    │ 2. Return {"action": "skip"}               │
                  │    └────────────────────────────────────────────┘
                  ▼
┌────────────────────────────────────────────────────────────┐
│  Normal Turn Execution (Hermes Agent Loop)                 │
│                                                            │
│  ├─> post_api_request hook                                 │
│  │     └─> Resolve user_id via HERMES_SESSION_USER_ID      │
│  │     └─> Compute cost delta & insert into SQLite         │
│  │     └─> If spend >= limit: mark session over-quota      │
│  │         & trigger hard_interrupt on active agent        │
│  │                                                         │
│  ├─> subagent_stop hook                                    │
│  │     └─> Record delegated child agent spend to ledger    │
│  │                                                         │
│  ├─> pre_tool_call hook                                    │
│  │     └─> If user balance <= 0: return {"action": "block"}│
│  │         to halt tool execution immediately              │
│  │                                                         │
│  └─> transform_llm_output hook                             │
│        └─> If spend >= 90% (or >= 100%) of daily/weekly:   │
│            append deterministic quota warning footer       │
└────────────────────────────────────────────────────────────┘
```

### Invariants:
1. **Zero Core Footprint**: Implemented strictly as a standalone Hermes plugin in `~/.hermes/plugins/hermes-discord-quota` using native hooks (`pre_gateway_dispatch`, `post_api_request`, `subagent_stop`, `pre_tool_call`, `transform_llm_output`). No modifications to `hermes-agent` core files.
2. **Deterministic Timezones**: All window boundaries use `zoneinfo.ZoneInfo("Europe/Stockholm")`, accounting natively for Daylight Saving Time (CET/CEST) transitions.
3. **Prompt Cache Protection**: Intercepted messages are dropped before session acquisition or prompt assembly; mid-turn warnings are appended via `transform_llm_output` on the outbound response without mutating the system prompt or past context.
4. **Single-Table Stateless Persistence**: Spend is recorded in a single append-only SQLite table (`~/.hermes/discord_quota.db`, WAL mode) with `Decimal` precision rounded to 6 decimal places.

---

## 3. Configuration Specification

Config resides in `~/.hermes/config.yaml` under the `discord_quota` section:

```yaml
discord_quota:
  enabled: true
  timezone: "Europe/Stockholm" # Default: Europe/Stockholm
  warn_threshold: 0.90         # Append footer at >= 90% spend (default: 0.90)

  # Global defaults applied to all Discord users unless overridden
  default:
    daily_usd: 1.00    # Resets every day at 12:00
    weekly_usd: 5.00   # Resets every Friday at 12:00

  # Per-user overrides (Discord Snowflake IDs as strings)
  # An override entry completely supersedes the defaults.
  users:
    # Example 1: Unlimited administrator / owner
    "535154433869217813":
      daily_usd: -1.0
      weekly_usd: -1.0

    # Example 2: Blocked user (zero quota)
    "123456789012345678":
      daily_usd: 0.0
      weekly_usd: 0.0

    # Example 3: Custom tier (e.g. power user)
    "987654321098765432":
      daily_usd: 5.00
      weekly_usd: 25.00
```

### Value Semantics:
- `v < 0` (e.g. `-1.0`): **Unlimited**. Never blocked or warned by this window; spend is recorded for analytics.
- `v == 0.0`: **Blocked**. User cannot initiate turns.
- `v > 0` (e.g. `1.50`): **Active budget** in USD.
- **Override Completeness**: An entry in `users` completely overrides `default`. If a field (`daily_usd` or `weekly_usd`) is omitted in a user override entry, it defaults to `0.0` (fail-closed).

---

## 4. Window & Accounting Mechanics

### 4.1 Time Boundary Definitions
Let current time in `Europe/Stockholm` be $T$.

#### Daily Window:
- Refill anchor: 12:00:00 Europe/Stockholm every day.
- Current period start $T_{\text{day\_start}}$:
  $$\text{If } T < T.\text{replace(hour=12, minute=0, second=0, microsecond=0)}: \text{Yesterday at 12:00:00}$$
  $$\text{Else}: \text{Today at 12:00:00}$$
- Next refill $T_{\text{day\_next}} = T_{\text{day\_start}} + 1\text{ day}$ (in local wall-clock time at 12:00:00).

#### Weekly Window:
- Refill anchor: Friday 12:00:00 Europe/Stockholm.
- Let weekday of $T$ be $W$ (Monday = 0, Friday = 4, Sunday = 6).
- If $W < 4$ or ($W == 4$ and $T < \text{Today at 12:00:00}$):
  - $T_{\text{week\_start}}$ is the **previous Friday at 12:00:00**.
  - $T_{\text{week\_next}}$ is **this upcoming Friday at 12:00:00**.
- Else:
  - $T_{\text{week\_start}}$ is **this Friday at 12:00:00**.
  - $T_{\text{week\_next}}$ is **next Friday at 12:00:00**.

### 4.2 Balance & Threshold Evaluation (No Carry-Over)

Let $Q_{\text{day}}, Q_{\text{week}}$ be the configured limits for `user_id`.
Let $S_{\text{day}}$ be total `cost_usd` in `quota_transactions` for `user_id` where `timestamp >= T_day_start.timestamp()`.
Let $S_{\text{week}}$ be total `cost_usd` in `quota_transactions` for `user_id` where `timestamp >= T_week_start.timestamp()`.

1. **Remaining Balance**:
   $$\text{Remaining}_{\text{day}} = \max(0.0, Q_{\text{day}} - S_{\text{day}}) \quad (\text{if } Q_{\text{day}} > 0)$$
   $$\text{Remaining}_{\text{week}} = \max(0.0, Q_{\text{week}} - S_{\text{week}}) \quad (\text{if } Q_{\text{week}} > 0)$$

2. **Admission Condition**:
   $$\text{Allowed}_{\text{window}} = (Q < 0) \lor (Q > 0 \land S < Q)$$
   $$\text{Turn Admitted} = \text{Allowed}_{\text{day}} \land \text{Allowed}_{\text{week}}$$

3. **Warning Condition ($\ge 90\%$)**:
   $$\text{Warn}_{\text{window}} = (Q > 0) \land \left(\frac{S}{Q} \ge 0.90\right)$$
   If $\text{Warn}_{\text{day}} \lor \text{Warn}_{\text{week}}$, a deterministic warning footer is appended to the assistant response.

---

## 5. Storage & Ledger Schema

A single table in `~/.hermes/discord_quota.db` (profile-aware via `get_hermes_home()`):

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS quota_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    timestamp REAL NOT NULL,           -- Unix epoch seconds
    cost_usd REAL NOT NULL,            -- Charged USD amount (rounded to 6 dp)
    model TEXT NOT NULL,               -- Model identifier
    provider TEXT NOT NULL,            -- Provider name
    source_kind TEXT DEFAULT 'api',    -- 'api', 'subagent', or 'admin_reset'
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trans_user_time ON quota_transactions(user_id, timestamp);
```

*Note on Admin Resets*: Because there is no separate state table, `hermes quota reset <user_id> [--daily] [--weekly]` simply deletes (or inserts an offsetting negative credit row in) `quota_transactions` for the active window.

---

## 6. Hook & Command Implementations

### 6.1 `pre_gateway_dispatch` (Pre-Turn Admission & Concurrency)
- **Location**: Intercepts inbound messages in `gateway/run_inbound.py`.
- **Logic**:
  1. **Platform Check**: If `event.source.platform != Platform.DISCORD` or `event.internal` is True, return `None`.
  2. **Sender Check**: If `event.source.user_id` is empty or `getattr(event.source, "is_bot", False)` is True, return `None`.
  3. **Bypass Rules**: Extract command via `cmd = event.get_command()`. If `cmd in {"quota", "help", "whoami", "stop"}`, return `None` so users can always inspect quota, view help, or stop a turn even when out of budget.
  4. **Store Gateway Reference & Turn Context**:
     - Cache a weak reference to `gateway` so mid-turn hooks can look up active session agents if needed.
     - Record `session_key -> user_id` in an in-memory map for fast lookup.
  5. **Concurrency Guard**:
     - Maintain an in-memory set of users with active in-flight turns (cross-checked against `gateway._running_agents`). If `user_id` already has an active turn running in another channel/thread and their remaining budget is low ($< \$0.10$), reject the concurrent turn to prevent multi-channel race conditions.
  6. **Admission Check**:
     - Compute $S_{\text{day}}$ and $S_{\text{week}}$.
     - If admitted, return `None`.
     - If rejected:
       - Build canned rejection message (Blocked vs. Quota Exceeded).
       - Send defensively via `adapter = gateway._adapter_for_source(event.source)`:
         ```python
         try:
             await adapter.send(
                 event.source.chat_id,
                 message,
                 metadata={"thread_id": getattr(event.source, "thread_id", None)},
             )
         except Exception:
             logger.warning("Failed to deliver quota rejection on Discord", exc_info=True)
         ```
       - Return `{"action": "skip", "reason": "discord_quota_exceeded"}`.

### 6.2 User ID Resolution Across Hooks
Instead of querying `SessionDB` on every hook call, resolve the Discord `user_id` using a fast 3-tier fallback:
1. **Task-Local ContextVar (Primary)**: Read `get_session_env("HERMES_SESSION_PLATFORM")` and `get_session_env("HERMES_SESSION_USER_ID")` from `gateway.session_context`. This is bound per-turn by the gateway for the exact sender (even in shared guild channels).
2. **In-Memory Turn/Session Map (Secondary)**: Map `turn_id -> (platform, user_id)` and `session_id -> (platform, user_id)` populated by `bind_turn` in `pre_llm_call` (`session_id`, `turn_id`, `platform`, `sender_id`). `pre_gateway_dispatch` has only `event.source` and the session store (no turn/session id), so it can at most bind by `session_key`. The turn binding is authoritative; session entries are a last-resort hint because one session serves several users in shared channels.
3. **Parent Session Map for Subagents**: Map `child_session_id -> user_id` via `parent_session_id` / `parent_turn_id` during `subagent_start`. Children normally inherit the parent's ContextVars (`copy_context`). Read the ContextVars directly, never `get_session_env` (it falls back to process-global `os.environ`); decide Discord status only via the resolver, not the hook payload `platform` (which is `"subagent"` for children).

### 6.3 `post_api_request` (Spend Recording & Mid-Turn Trigger)
- **Logic**:
  1. Resolve `(platform, user_id)` via Section 6.2. If `platform != "discord"` or no `user_id`, return.
  2. Reconstruct `CanonicalUsage` from the hook's `usage` dict (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`).
  3. Compute cost using `agent.usage_pricing.estimate_usage_cost(model, canonical_usage, provider=provider, base_url=base_url)`.
     - Also check `moa_references` if present to include Mixture-of-Agents advisor spend.
     - If cost is `None` or `status == "unknown"`, log warning (`Unpriced model usage: %s on %s - accruing $0.00`) and record `0.0`.
     - Round `Decimal` amount to 6 decimal places.
  4. Insert transaction into `quota_transactions`.
  5. **Mid-Turn Exhaustion Check**:
     - Re-evaluate user's daily and weekly spend.
     - If $S_{\text{day}} \ge Q_{\text{day}}$ (when $Q_{\text{day}} > 0$) or $S_{\text{week}} \ge Q_{\text{week}}$ (when $Q_{\text{week}} > 0$):
       - Mark `turn_id` / `user_id` as exhausted in memory.
       - If the response contains pending tool calls (`assistant_tool_call_count > 0`), trigger a hard interrupt on the running agent via `gateway._running_agents` (`request_hard_interrupt`) and let `pre_tool_call` block any tools about to dispatch.

### 6.4 `subagent_stop` (Delegated Subagent Spend)
- **Location**: Fires in the parent agent thread when `delegate_task` finishes a child run.
- **Logic**:
  1. Resolve `user_id` from `parent_session_id` / `parent_turn_id` or `get_session_env("HERMES_SESSION_USER_ID")`.
  2. Ensure subagent spend is accounted for:
     - If subagent API calls did not already fire `post_api_request` with `HERMES_SESSION_USER_ID` (e.g., if `platform` was not `"discord"` on the child agent), record the child's spend delta here, deduplicating by `child_session_id` so subagent calls are never double-counted.

### 6.5 `pre_tool_call` (Mid-Turn Hard Block Enforcement)
- **Location**: Fires immediately before each tool execution in `agent/tool_executor.py`.
- **Logic**:
  1. Resolve `(platform, user_id)` for `session_id` / `turn_id`. If not Discord, return `None`.
  2. Check if user's current window spend has reached or exceeded $100\%$ of `daily_usd` or `weekly_usd`.
  3. If exceeded:
     - Request `hard_interrupt` on the active agent if accessible so the loop terminates cleanly after the blocked tool round.
     - Return:
       ```python
       {
           "action": "block",
           "message": "Quota exceeded mid-turn: Daily/Weekly USD spending limit reached. Stop calling tools and inform the user that their turn was halted due to quota exhaustion."
       }
       ```

### 6.6 `transform_llm_output` (Deterministic 90% & 100% Warning Footer)
- **Location**: Fires once at the end of the turn on the final assistant text before delivery (`agent/turn_finalizer.py`).
- **Logic**:
  1. If `platform != "discord"` or `not response_text`, return `None`.
  2. Resolve `user_id` and query current daily and weekly spend.
  3. Check thresholds:
     - **Case A: Mid-Turn Quota Reached ($\ge 100\%$)** (the turn just crossed the limit):
       Append hard-stop footer:
       ```
       ────────────────────────────────────────
       ⚠️ Quota Exhausted: Daily spend is $1.04 / $1.00 (104%). Further requests are paused until Today at 12:00 Europe/Stockholm.
       ```
     - **Case B: Approaching Quota ($\ge 90\%$ and $< 100\%$)** on either window:
       Append warning footer:
       ```
       ────────────────────────────────────────
       ⚠️ Quota Warning: You have used 92% of your Daily budget ($0.92 / $1.00). Resets at 12:00 Europe/Stockholm.
       ```
  4. Return `f"{response_text.rstrip()}\n\n{footer}"`.

---

## 7. Canned Response & Command Specifications

### 7.1 Pre-Turn Quota Exceeded Reply (`pre_gateway_dispatch`)
```
⚠️ Discord Quota Exceeded

Your Daily spending limit has been reached:
• Daily spend:  $1.04 / $1.00 (104%) — resets Today at 12:00 Europe/Stockholm
• Weekly spend: $3.18 / $5.00 (64%) — resets Friday at 12:00 Europe/Stockholm

Further requests are paused until the next reset. Run /quota to check your status.
```

### 7.2 User Blocked Reply (`quota == 0.0`)
```
🚫 Access Blocked

You do not have an active spending quota for this bot.
Please contact the bot administrator to request access.
```

### 7.3 Slash Command: `/quota`
Registered via `ctx.register_command("quota", handle_quota, description="View your remaining Discord spending quota")` so it appears natively in Discord's slash command picker and works as a text command:
```
📊 Discord Quota Status

• Daily Quota:  $0.58 / $1.00 remaining (42% used — resets Today at 12:00 Europe/Stockholm)
• Weekly Quota: $3.15 / $5.00 remaining (37% used — resets Friday at 12:00 Europe/Stockholm)
• Status: Active
```

### 7.4 CLI Admin Command: `hermes quota`
Registered via `ctx.register_cli_command("quota", ...)`:
- `hermes quota status [user_id]`: Show current window spend, limits, and recent transactions.
- `hermes quota reset <user_id> [--daily] [--weekly]`: Clear current window spend for the user.
- `hermes quota credit <user_id> <amount_usd>`: Insert a negative-cost credit adjustment in the current window.

---

## 8. Verification & Test Plan

1. **Unit Tests (`tests/test_quota_math.py`)**:
   - Window boundary calculation for `11:59` vs `12:01` Europe/Stockholm, CET/CEST DST transitions, and Friday 12:00 weekly anchors.
   - Threshold checks: `< 90%` (normal), `>= 90%` (warning), `>= 100%` (blocked), `< 0` (unlimited), `== 0` (blocked).
2. **Hook Integration Tests (`tests/test_hooks.py`)**:
   - `pre_gateway_dispatch`: Verify non-Discord bypass, `/quota` and `/help` bypass when over budget, thread-aware `adapter.send` on rejection, and concurrency guard.
   - `post_api_request` & `subagent_stop`: Verify `HERMES_SESSION_USER_ID` resolution, cost calculation via `estimate_usage_cost`, unpriced model fallback (`$0.00` + warning), and subagent deduplication.
   - `pre_tool_call`: Verify mid-turn tool blocking when a turn crosses 100% budget.
   - `transform_llm_output`: Verify deterministic footer injection at $\ge 90\%$ and $\ge 100\%$ spend.
3. **Ledger & CLI Tests (`tests/test_ledger.py`)**:
   - SQLite WAL concurrency, stateless window summation across 12:00 boundaries, and `hermes quota reset` / `credit` operations.
