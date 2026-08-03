#!/usr/bin/env python3
"""
s01_agent_loop — The Agent Loop

The entire secret of an AI coding agent in one pattern:

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

This is the core loop: feed tool results back to the model
until the model decides to stop. Production agents layer
policy, hooks, and lifecycle controls on top.

Usage:
    pip install anthropic python-dotenv
    python -m s01_agent_loop.code
"""

# ═══════════════════════════════════════════════════════════════
# Block 1: Imports & Setup
# ═══════════════════════════════════════════════════════════════

import os
import subprocess
import sys

# --- Readline: better line-editing in the REPL ---
try:
    import readline
    # Fix backspace issue with CJK input on macOS libedit
    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
except ImportError:
    pass  # Windows or environments without readline — no problem

import anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

# ═══════════════════════════════════════════════════════════════
# Block 2: Configuration
# ═══════════════════════════════════════════════════════════════

# When a custom base URL is set (e.g. DeepSeek), drop ANTHROPIC_AUTH_TOKEN
# so the SDK sends the API key via x-api-key instead.
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = anthropic.Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = (
    f"You are a coding agent at {os.getcwd()}. "
    "Use bash to solve tasks. Act, don't explain."
)

# --- Tool definition: just bash ---
TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    }
]

# ═══════════════════════════════════════════════════════════════
# Block 3: Tool execution
# ═══════════════════════════════════════════════════════════════

DANGEROUS_PATTERNS = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]

def run_bash(command: str) -> str:
    """Execute a shell command and return stdout+stderr (truncated)."""
    for pattern in DANGEROUS_PATTERNS:
        if pattern in command:
            return "Error: Dangerous command blocked"

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return output[:50000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as exc:
        return f"Error: {exc}"

# ═══════════════════════════════════════════════════════════════
# Block 4: The core agent loop
# ═══════════════════════════════════════════════════════════════

def agent_loop(messages: list) -> None:
    """
    The agent loop: call the model, execute tools, feed results back,
    repeat until the model stops asking for tools.

    Uses streaming to avoid HTTP timeouts on long responses.
    """
    while True:
        # ── Call the model (streaming) ──
        try:
            with client.messages.stream(
                model=MODEL,
                system=SYSTEM,
                messages=messages,
                tools=TOOLS,
                max_tokens=16000,
            ) as stream:
                response = stream.get_final_message()
        except anthropic.NotFoundError:
            print(f"\033[31mError: Model '{MODEL}' not found.\033[0m", file=sys.stderr)
            return
        except anthropic.AuthenticationError:
            print("\033[31mError: Invalid API key.\033[0m", file=sys.stderr)
            return
        except anthropic.RateLimitError:
            print("\033[31mError: Rate limited. Wait and retry.\033[0m", file=sys.stderr)
            return
        except anthropic.APIStatusError as exc:
            print(f"\033[31mAPI error {exc.status_code}: {exc.message}\033[0m", file=sys.stderr)
            return
        except anthropic.APIConnectionError:
            print("\033[31mError: Network issue. Check your connection.\033[0m", file=sys.stderr)
            return

        req_id = getattr(response, "_request_id", None)
        if req_id:
            print(f"  [request_id: {req_id}]")

        # ── Append assistant turn ──
        messages.append({"role": "assistant", "content": response.content})

        # ── Check stop reason ──
        if response.stop_reason == "refusal":
            print("\033[31mModel refused this request (safety).\033[0m", file=sys.stderr)
            return
        if response.stop_reason != "tool_use":
            return  # end_turn or max_tokens — we're done

        # ── Execute each tool call, collect results ──
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[33m$ {block.input['command']}\033[0m")
            output = run_bash(block.input["command"])
            print(output[:200])
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })

        # ── Feed tool results back → loop continues ──
        messages.append({"role": "user", "content": tool_results})

# ═══════════════════════════════════════════════════════════════
# Block 5: Response display
# ═══════════════════════════════════════════════════════════════

def print_final_response(history: list) -> None:
    """Extract and print the model's last text response."""
    last_content = history[-1]["content"]
    if not isinstance(last_content, list):
        return
    for block in last_content:
        if getattr(block, "type", None) == "text":
            print(block.text)

# ═══════════════════════════════════════════════════════════════
# Block 6: REPL entry point
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    print("s01: Agent Loop")
    print("Input your question, press Enter to send. Type q to quit.\n")

    history: list[dict] = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        print_final_response(history)
        print()

if __name__ == "__main__":
    main()
