# Implementation Plan: hermes-discord-quota

## 1. Executive Summary

This plugin introduces per-user USD spending controls for Discord interactions with Hermes Agent. It implements dual sliding/refilling buckets:
- **Daily Window**: Refills every calendar day at **12:00 Europe/Stockholm**.
- **Weekly Window**: Refills every Friday at **12:00 Europe/Stockholm**.

It enforces limits prior to turn execution, allows admitted turns to run to completion, carries any resulting overspend as a negative starting balance into subsequent periods, and notifies users with an instant canned message when quota is exceeded (costing $0.00 in LLM spend).

---

## 2. Architecture & Design Principles

```
Inbound Discord Message
          │
          ▼
┌────────────────────────────────────────────────────────┐
│  gateway/run_inbound.py                                │
│    └─> pre_gateway_dispatch hook                       │
└─────────────────────────┬──────────────────────────────┘
                          │
          ┌───────────────▼────────────────┐
          │  hermes-discord-quota Plugin   │
          │  1. Check Platform == DISCORD  │
          │  2. Resolve User ID & Config   │
          │  3. Calculate Current Balances │
          └───────┬──────────────┬─────────┘
                  │              │
           Balance > 0     Balance <= 0 or Blocked (0.0)
                  │              │
                  │              ▼
                  │    ┌────────────────────────────────────────┐
                  │    │ 1. Send canned explanation to channel  │
                  │    │ 2. Return {"action": "skip"}           │
                  │    └────────────────────────────────────────┘
                  ▼
┌────────────────────────────────────────────────────────┐
│  Normal Turn Execution (Hermes Agent Loop)             │
│    └─> post_api_request hook                           │
│          └─> Compute cost delta                        │
│          └─> Deduct from User's Daily & Weekly Balance │
│          └─> Append transaction to SQLite Ledger       │
└────────────────────────────────────────────────────────┘
```

### Invariants:
1. **Zero Core Footprint**: Implemented strictly as a standalone Hermes plugin in `~/.hermes/plugins/hermes-discord-quota` using existing hook points (`pre_gateway_dispatch`, `post_api_request`). No modifications to `hermes-agent` core files.
2. **Deterministic Timezones**: All calculations use `zoneinfo.ZoneInfo("Europe/Stockholm")`, accounting natively for Daylight Saving Time (CET/CEST) shifts.
3. **Prompt Cache Protection**: Intercepted messages are dropped before session acquisition or prompt assembly, preventing wasted prompt tokens or cache churn.
4. **Resilient Persistence**: Transactions and period states are maintained in a dedicated SQLite database (`~/.hermes/discord_quota.db`) with WAL mode enabled.

---

## 3. Configuration Specification

Config resides in `~/.hermes/config.yaml` under the `discord_quota` section:

```yaml
discord_quota:
  enabled: true
  timezone: "Europe/Stockholm" # Default: Europe/Stockholm
  
  # Global defaults applied to all Discord users unless overridden
  default:
    daily_usd: 1.00    # Refills every day at 12:00
    weekly_usd: 5.00   # Refills every Friday at 12:00

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
- `v < 0` (e.g. `-1.0`): **Unlimited**. Never blocked by this window; spend is recorded for analytics but not capped.
- `v == 0.0`: **Blocked**. User cannot initiate turns.
- `v > 0` (e.g. `1.50`): **Active budget** in USD.
- **Override Completeness**: An entry in `users` completely overrides the default. If a field is omitted in a user entry, it defaults to `0.0` (fail-closed) to ensure explicit configuration.

---

## 4. Window & Refill Mechanics

### 4.1 Time Boundary Definitions
Let current time in `Europe/Stockholm` be $T$.

#### Daily Window:
- Refill anchor: 12:00:00 Europe/Stockholm every day.
- Current period start $T_{\text{day\_start}}$:
  $$\text{If } T.\text{hour} < 12: \text{Yesterday at 12:00:00}$$
  $$\text{If } T.\text{hour} \ge 12: \text{Today at 12:00:00}$$
- Next refill $T_{\text{day\_next}}$:
  $$\text{If } T.\text{hour} < 12: \text{Today at 12:00:00}$$
  $$\text{If } T.\text{hour} \ge 12: \text{Tomorrow at 12:00:00}$$

#### Weekly Window:
- Refill anchor: Friday 12:00:00 Europe/Stockholm.
- Let weekday of $T$ be $W$ (Monday = 0, Friday = 4, Sunday = 6).
- If $W < 4$ or ($W == 4$ and $T.\text{hour} < 12$):
  - Current period started on the **previous Friday at 12:00:00**.
  - Next refill is **this upcoming Friday at 12:00:00**.
- If $W > 4$ or ($W == 4$ and $T.\text{hour} \ge 12$):
  - Current period started **this Friday at 12:00:00**.
  - Next refill is **next Friday at 12:00:00**.

### 4.2 Balance & Carry-Over Deficit Formula

Let $Q$ be the quota limit ($Q_{\text{day}}$ or $Q_{\text{week}}$).
Let $S$ be total spend during the current period.
Let $D_{\text{prior}}$ be the carried-over deficit from the previous period ($D_{\text{prior}} \le 0$).

1. **Available Balance**:
   $$\text{Balance} = Q + D_{\text{prior}} - S$$

2. **Admission Condition**:
   A turn is admitted if and only if:
   $$\text{Daily Allowed} \land \text{Weekly Allowed}$$
   Where $\text{Allowed} = (Q < 0) \lor (Q > 0 \land \text{Balance} > 0)$.
   (If $Q == 0$, $\text{Allowed} = \text{False}$).

3. **Turn Execution & Overshoot**:
   Once admitted, the turn runs to completion.
   The turn incurs actual cost $\Delta$.
   New spend: $S' = S + \Delta$.
   Updated balance: $\text{Balance}' = \text{Balance} - \Delta$.
   If $\text{Balance}' < 0$, the user has entered a deficit of magnitude $|\text{Balance}'|$.

4. **Refill Transition**:
   When period transitions from period $k$ to $k+1$:
   - Unused positive balance does **not** roll over: $\max(0, \text{Balance}_k)$ is expired.
   - Any negative deficit carries over:
     $$D_{\text{prior}, k+1} = \min(0.0, \text{Balance}_k)$$
   - New period starting balance:
     $$\text{Balance}_{k+1} = Q + D_{\text{prior}, k+1}$$
   - *Example A (Normal under-budget)*: $Q = 1.00$, spent $0.40$, remaining $+0.60$. At 12:00 refill, new balance resets to $\$1.00$.
   - *Example B (Minor deficit)*: $Q = 1.00$, spent $1.25$, remaining $-0.25$. At 12:00 refill, starting balance is $\$1.00 - \$0.25 = \$0.75$.
   - *Example C (Severe deficit)*: $Q = 1.00$, spent $2.50$, remaining $-1.50$. At 12:00 refill, starting balance is $\$1.00 - \$1.50 = -\$0.50$. User remains blocked until the subsequent day's refill (which will bring it to $+\$0.50$), unless an admin manually credits or resets.

---

## 5. Storage & Ledger Schema

A dedicated SQLite database at `~/.hermes/discord_quota.db` (profile-aware via `get_hermes_home()`):

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- Transaction log of all LLM API invocations
CREATE TABLE IF NOT EXISTS quota_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    timestamp REAL NOT NULL,           -- Unix epoch seconds
    cost_usd REAL NOT NULL,            -- Charged USD amount
    model TEXT NOT NULL,               -- Model identifier
    provider TEXT NOT NULL,            -- Provider name
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    turn_id TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trans_user_time ON quota_transactions(user_id, timestamp);

-- Period accounting snapshots (tracking carried deficits and period IDs)
CREATE TABLE IF NOT EXISTS user_period_state (
    user_id TEXT NOT NULL,
    window_type TEXT NOT NULL,          -- 'daily' or 'weekly'
    period_start_epoch REAL NOT NULL,   -- Epoch timestamp of period start
    carried_deficit REAL DEFAULT 0.0,   -- Deficit from previous period (<= 0)
    PRIMARY KEY (user_id, window_type)
);
```

---

## 6. Hook Implementations

### 6.1 `pre_gateway_dispatch`
- **Location**: Intercepts in `gateway/run_inbound.py`.
- **Logic**:
  1. If `event.source.platform != Platform.DISCORD`, return `None` (bypass).
  2. If `event.source.user_id` is empty or bot, return `None`.
  3. Load quota configuration for `user_id`.
  4. Compute current daily and weekly balances:
     - Detect if period rollover occurred since last recorded state.
     - Advance period and carry over negative balance if necessary.
     - Sum transactions in the current period from `quota_transactions`.
  5. Check admission condition:
     - If both daily and weekly are admitted, return `None` (allow message through).
     - If rejected:
       - Construct canned explanation message.
       - Asynchronously send message via `gateway._adapter_for_source(event.source).send(...)`.
       - Return `{"action": "skip", "reason": "discord_quota_exceeded"}`.

### 6.2 `post_api_request`
- **Location**: Intercepts after every model API response in `agent/turn_response_intake.py`.
- **Logic**:
  1. If `platform != "discord"` or `session_id` is not a Discord session, skip.
  2. Resolve Discord `user_id` from session origin in SessionDB (`sessions.user_id`).
  3. Calculate cost:
     - Use `agent.usage_pricing.estimate_usage_cost(model, usage, provider=provider, base_url=base_url)`.
     - If cost result is `None` or `status == "unknown"`, log warning:
       `logger.warning("Unpriced model usage: %s on %s - accruing $0.00", model, provider)`
       and record cost as `0.0`.
     - If cost is available, convert to float USD.
  4. Insert record into `quota_transactions`.
  5. Update active in-memory cache / state.

---

## 7. Canned Response Specification

When quota is exhausted or blocked, the user receives an immediate plain-text reply in Discord:

### Quota Exceeded Example:
```
⚠️ Discord Quota Exceeded

Your Daily budget limit has been reached:
• Current spend: $1.18 / $1.00
• Carried deficit: -$0.18
• Window: Daily (resets at 12:00 CET/CEST)
• Next refill: Today at 12:00 Europe/Stockholm

Your turn was completed, but further requests are paused until the next refill.
```

### User Blocked Example (`quota == 0`):
```
🚫 Access Blocked

You currently do not have an active spending quota for this bot.
Please contact the bot administrator to request access.
```

---

## 8. Management Commands & User Inspection

### User Command: `/quota` (Discord Slash Command & Gateway Command)
Displays the user's current status:
```
📊 Discord Quota Status

• Daily Quota:  $0.42 / $1.00 remaining (Next refill: 12:00 Europe/Stockholm)
• Weekly Quota: $3.15 / $5.00 remaining (Next refill: Friday 12:00 Europe/Stockholm)
• Status: Active
```

### CLI / Admin Command: `hermes quota`
- `hermes quota show <user_id>`: View breakdown, transaction history, and deficit status.
- `hermes quota reset <user_id> [--daily] [--weekly]`: Clear deficits and reset balance.
- `hermes quota credit <user_id> <amount_usd>`: Apply one-off promotional/support credit.

---

## 9. Edge Cases & Concurrency

1. **Concurrent Message Burst**:
   Multiple messages sent simultaneously before the first turn finishes.
   - Mitigation: A short-lived per-user admission lock / reservation. When the first turn is admitted, mark active turn status so a second concurrent message is queued or checked against remaining headroom.
2. **Daylight Saving Time (DST) Transitions**:
   - `zoneinfo.ZoneInfo("Europe/Stockholm")` handles UTC offset transitions (UTC+1 to UTC+2 and vice versa) automatically. Periods anchored at `12:00` wall-clock time accurately trigger at 12:00 local time on both CET and CEST.
3. **Turn Cost Overshoot Exceeding Full Next Allowance**:
   - The formula $\text{Balance}_{k+1} = Q + \min(0.0, \text{Balance}_k)$ ensures that large deficits persist across multiple periods until fully paid off, preventing users from exploiting expensive prompts right before 12:00.
4. **Unpriced Models**:
   - Does not crash or block. Accrues $0.00 and emits warning in logs per user policy.

---

## 10. Verification & Test Plan

1. **Unit Tests (`tests/test_quota_math.py`)**:
   - Time boundary calculation for 11:59 vs 12:01, DST leap days, Friday weekly resets.
   - Balance formula: normal spend, exact-budget, overspend deficit carry-over, double-deficit carry-over.
   - Quota value flags: `< 0` (unlimited), `== 0` (blocked), `> 0` (capped).
2. **Integration Tests (`tests/test_gateway_interception.py`)**:
   - Simulate `pre_gateway_dispatch` with mock `MessageEvent` for Discord.
   - Verify non-Discord platforms are ignored.
   - Verify blocked users receive canned message and return `{"action": "skip"}`.
   - Verify authorized users return `None` and proceed.
3. **Ledger Tests (`tests/test_ledger.py`)**:
   - SQLite WAL writes, concurrency safety, sum aggregations across period boundaries.
4. **End-to-End Simulation**:
   - Run turn sequence: $0.60 spend -> $0.50 spend (overshoot to -$0.10) -> simulate 12:00 refill -> verify new balance is $0.90.
