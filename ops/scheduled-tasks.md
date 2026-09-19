# Claude scheduled tasks (Phase 4)

These run on the Claude Pro subscription inside the Claude desktop app, on this Mac, while the app is open.
If the Mac or the app is off at the scheduled time, the task runs once on the next launch.
Each task only reads data and writes signal files or vault notes. It never places orders: the engine does
that, through the risk gate.

They are created (switched on) only after the Phase 3 check passes. Times are Sydney local time.

| Task id | Schedule (cron, local) | Skill | Why then |
|---|---|---|---|
| `trader-daily-digest` | `35 6 * * *` | daily-digest | After the US close (~6am) and stock decisions (~6:20); replaces the engine's plain 7am summary (the engine's version is the fallback) |
| `trader-analyst-team` | `45 6,10 * * *` | analyst-team | Right after stock decisions (~6:20) and crypto decisions (~10:00); verdicts must arrive within 8 hours |
| `trader-investor-panel` | `0 18 * * 0` | investor-panel | Sunday evening; weights are applied at Monday's US open |
| `trader-weekly-review` | `0 9 * * 6` | weekly-review | Saturday morning, after Friday's US close |

## Prompt used for each task
(`<skill>` replaced by the skill name)

```
You are running the personal trader's "<skill>" job. Work only in /Users/jm/Documents/Projects/personalTrader.
First read .claude/skills/<skill>/SKILL.md and follow it exactly, including its hard rules:
never edit trader/, config/, tests/ or .env, never run `trader run/order/halt/resume/sync/serve`,
and never place, cancel or change orders with any broker tool. Run commands as `uv run trader ...`
from that folder. If something fails, say what failed and stop. Don't improvise around the rules.
Finish by printing the short summary the skill asks for.
```

## First run
Use **Run now** on each task once while watching. Approve only the tools it needs (reading files,
`uv run trader ...` commands, writing to `signals/outbox/` and `vault/`). Say "always allow" for those,
so later runs don't stall. Anything else it asks for should be declined.
