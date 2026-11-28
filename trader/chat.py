"""Ask Claude about this account, from inside the dashboard.

Built on the Claude Agent SDK, which authenticates through your Claude Code login -- so it runs on the
subscription, with no API key, and Anthropic permits exactly this use.

THE POINT OF THIS MODULE IS THE POLICY BELOW, not the plumbing. An agent with a shell, inside a folder
holding broker credentials and a trading engine, driven from a text box, is the most dangerous thing in
this repository. So it is allowed to do three things and nothing else:

  * read and search files -- except anything that could hold a secret
  * run a short allowlist of READ-ONLY `trader` commands
  * talk

It cannot write or edit a file, and it cannot run any command that places an order, moves the kill
switch, or touches credentials. Those are refused by `can_use_tool` before the tool ever executes, not
by asking the model nicely: a system prompt is guidance, and this needs a lock.

The engine remains the only thing that trades, through the risk gate, exactly as before.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

# `trader` subcommands that only read. Anything absent is refused, so a new command is denied by
# default and has to be added here deliberately.
READ_ONLY_COMMANDS = frozenset({
    "status", "check-config", "context", "candidates", "technicals", "excursions",
    "gate1", "backtest", "news", "fundamentals", "signal",
})

# Spelled out so the reason for each refusal is obvious, and so a typo in the allowlist cannot quietly
# permit one of these.
FORBIDDEN_COMMANDS = frozenset({
    "run",            # places orders
    "order",          # places orders
    "halt", "resume",  # moves the kill switch
    "sync",           # mutates the journal
    "serve", "menubar",  # starts long-lived processes
    "telegram-setup", "ctrader-setup", "ctrader-auth",  # touches credentials
    "notify",         # messages you as if it were the engine
    "data",           # rewrites the price cache the backtests depend on
})

# Shell features that would let one allowed command smuggle in another.
SHELL_METACHARACTERS = (";", "&", "|", ">", "<", "`", "$(", "\n", "\r")

# Never read these, whatever asks. Matched against the whole path, case-insensitively.
SECRET_PATTERNS = (".env", "credentials", "secret", "token", "KILL_SWITCH", ".git/config",
                   "id_rsa", ".ssh", "keychain")

WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "mcp__ccd_settings__set_setting"})
READ_TOOLS = frozenset({"Read", "Grep", "Glob"})


class Denied(Exception):
    """A tool call the policy refuses."""


def looks_secret(path: str) -> bool:
    low = str(path).lower()
    return any(p.lower() in low for p in SECRET_PATTERNS)


def check_bash(command: str) -> None:
    """Allow only a single read-only `trader` command. Raises Denied with the reason."""
    text = (command or "").strip()
    if not text:
        raise Denied("empty command")
    for meta in SHELL_METACHARACTERS:
        if meta in text:
            raise Denied(f"shell metacharacter {meta!r} is not allowed; one plain command only")
    try:
        parts = shlex.split(text)
    except ValueError as exc:
        raise Denied(f"could not parse the command: {exc}") from exc
    if not parts:
        raise Denied("empty command")

    # Accept `trader ...`, `uv run trader ...`, `uv run --project X trader ...`
    if "trader" not in parts:
        raise Denied("only `trader` commands are allowed here")
    index = parts.index("trader")
    if parts[0] not in ("trader", "uv"):
        raise Denied(f"commands must start with `trader` or `uv`, not {parts[0]!r}")
    if index + 1 >= len(parts):
        raise Denied("`trader` needs a subcommand")
    subcommand = parts[index + 1]
    if subcommand in FORBIDDEN_COMMANDS:
        raise Denied(f"`trader {subcommand}` can change money, credentials or the kill switch")
    if subcommand not in READ_ONLY_COMMANDS:
        raise Denied(f"`trader {subcommand}` is not on the read-only allowlist")
    for part in parts:
        if looks_secret(part):
            raise Denied(f"{part!r} looks like it could hold a secret")


def check_read(path: str, root: Path) -> None:
    """Allow reads inside the project, except anything that smells of credentials."""
    if looks_secret(path):
        raise Denied(f"{path!r} could hold a secret, so it is not readable from here")
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise Denied(f"unreadable path: {exc}") from exc
    if not resolved.is_relative_to(root.resolve()):
        raise Denied(f"{path!r} is outside the project")


def decide(tool: str, payload: dict, root: Path) -> None:
    """The whole policy in one place. Raises Denied, or returns None to allow."""
    if tool in WRITE_TOOLS:
        raise Denied(f"{tool} is not available: this assistant reads and explains, it does not change files")
    if tool == "Bash":
        check_bash(payload.get("command", ""))
        return
    if tool in READ_TOOLS:
        target = payload.get("file_path") or payload.get("path") or payload.get("pattern") or ""
        if tool == "Read":
            check_read(str(target), root)
        elif looks_secret(str(target)):
            raise Denied(f"{target!r} could hold a secret")
        return
    raise Denied(f"{tool} is not allowed here")


SYSTEM_PROMPT = """You are the assistant inside a personal paper-trading dashboard. The person you are
talking to is not a developer and owns this account.

What the system is: a Python engine runs every 15 minutes, decides using rule-based strategies, and sends
every order through a 17-check risk gate. Sleeves are strategies with their own slice of virtual capital:
B0 is a buy-and-hold benchmark, S1 trend following, S8 diversified trend (shadow), S9 forex (shadow). All
of it is paper money.

How to be useful:
- Answer from the data. `uv run trader status`, `context --json`, `excursions`, `gate1` are available to
  you, and you can read files in the project.
- Compare strategies against B0, not against zero. Beating zero in a rising market means nothing.
- Say when the data cannot answer the question. Two weeks and five closed trades is noise, and you should
  say so rather than reading a story into it.
- Never recommend a trade or tell them what to buy. You are not a licensed adviser and this is explicitly
  out of scope.

You cannot change anything. You have no write access, and you cannot place orders, move the kill switch
or touch credentials -- that is enforced outside this prompt. If asked to, explain that the engine is the
only thing that trades, and point at the command they would run themselves."""


# --- running the agent ------------------------------------------------------------------------------

MAX_TURNS = 30
MAX_BUDGET_USD = 1.50     # a runaway loop costs usage, so cap each question

# The CLI's own name for a result cut short by one of the caps above, as opposed to one that ran out
# of things to say. Anything else -- "success", or an unrelated error subtype -- is not a truncation.
TRUNCATED_SUBTYPES = frozenset({"error_max_turns", "error_max_budget_usd"})


def _options(root: Path, resume: str | None = None):
    """Agent options wired to the policy above. Imported lazily so the SDK is optional.

    The policy runs as a PreToolUse HOOK, deliberately. Listing a tool in `allowed_tools` auto-approves
    it BEFORE can_use_tool is consulted -- the SDK warns about exactly this -- so a callback alone is
    decorative and the first version of this file shipped a lock that was never installed. A hook fires
    on every call regardless.
    """
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    async def gate(payload: dict, _tool_use_id, _context):
        tool = payload.get("tool_name", "")
        try:
            decide(tool, payload.get("tool_input", {}) or {}, root)
        except Denied as exc:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                           "permissionDecision": "deny",
                                           "permissionDecisionReason": str(exc)}}
        return {}

    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        cwd=str(root),
        disallowed_tools=sorted(WRITE_TOOLS),
        hooks={"PreToolUse": [HookMatcher(matcher="*", hooks=[gate])]},
        permission_mode="bypassPermissions",   # the hook above IS the permission system here
        max_turns=MAX_TURNS,
        max_budget_usd=MAX_BUDGET_USD,
        resume=resume,
        setting_sources=None,     # ignore repo CLAUDE.md/settings: the policy here is the whole policy
    )


async def ask_stream(question: str, root: Path, resume: str | None = None):
    """Yield ('tool'|'text'|'done'|'error', payload) as the agent works."""
    from claude_agent_sdk import query

    session: str | None = None
    try:
        async for message in query(prompt=question, options=_options(root, resume)):
            kind = type(message).__name__
            session = getattr(message, "session_id", None) or session
            for block in getattr(message, "content", []) or []:
                name = type(block).__name__
                if name == "TextBlock" and getattr(block, "text", "").strip():
                    yield "text", block.text
                elif name == "ToolUseBlock":
                    yield "tool", _describe(block)
            if kind == "ResultMessage":
                yield "done", {"session_id": session,
                               "cost_usd": getattr(message, "total_cost_usd", None),
                               "turns": getattr(message, "num_turns", None),
                               "truncated": getattr(message, "subtype", None) in TRUNCATED_SUBTYPES}
    except Exception as exc:                 # a broken assistant must say so, not hang the page
        yield "error", f"{type(exc).__name__}: {exc}"


def _describe(block) -> str:
    """One short line per tool call, for the activity list."""
    name = getattr(block, "name", "?")
    args = getattr(block, "input", {}) or {}
    if name == "Bash":
        return f"Running {args.get('command', '')[:90]}"
    if name == "Read":
        return f"Reading {Path(str(args.get('file_path', ''))).name}"
    if name == "Grep":
        return f"Searching for {str(args.get('pattern', ''))[:50]}"
    if name == "Glob":
        return f"Listing {str(args.get('pattern', ''))[:50]}"
    return name
