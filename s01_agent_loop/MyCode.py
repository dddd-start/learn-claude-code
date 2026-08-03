import os, subprocess, sys
import anthropic
from anthropic import Anthropic
from dotenv import load_dotenv


load_dotenv(override = True)

client = anthropic.Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
SYSTEM = (f"You are a coding agent on Windows (cmd.exe) at {os.getcwd()}. "
          f"Use Windows commands: dir, cd, type, echo, mkdir, copy, move, del, findstr. "
          f"Act, don't explain.")

TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        }
    }
]


DANGEROUS = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]

def run_bash(command: str) -> str:

    if any(p in command for p in DANGEROUS):
        return "Error: Dangerous command blocked"

    try:
        result = subprocess.run(
        command, shell=True, cwd=os.getcwd(),
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=120,
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return output[:50000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

def agent_loop(messages: list) -> None:
    while True:
        try:
            with client.messages.stream(
                model = MODEL,
                system = SYSTEM,
                messages = messages,
                tools = TOOLS,
                max_tokens = 16000,
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

        messages.append({"role": "assistant", "content":response.content})

        if response.stop_reason == "refusal":
            print("\033[31mModel refused this request (safety).\033[0m", file=sys.stderr)
            return

        if response.stop_reason != "tool_use":
            return

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

        messages.append({"role": "user", "content": tool_results})

def print_final_response(history: list) -> None:
    last_content = history[-1]["content"]
    if not isinstance(last_content, list):
        return
    for block in last_content:
        if getattr(block, "type", None) == "Text":
            print(block.text)

def main() -> None:
    print("s01: Agent Loop")
    print("Input your question, press Enter to send. Type q to quit.\n")

    history: list[dict] = []

    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except(EOFError, KeyboardInterrupt):
            print()
            break
        history.append({"role": "user", "content":query})
        agent_loop(history)
        print_final_response(history)
        print()

if __name__ == '__main__':
    main()
