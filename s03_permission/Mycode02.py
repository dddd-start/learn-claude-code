#1、加载环境变量
#2、实例化client；准备tool，SYSTEM，WORK_DIR
#3、实现tool函数、权限拦截函数
#4、agent_loop
#5、main
import os
import subprocess
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from typing import Callable

# 加载环境变量

load_dotenv()
WORK_DIR = Path.cwd()
client = anthropic.Anthropic(base_url = os.getenv("ANTHROPIC_BASE_URL"))
MODEL_ID = os.getenv("MODEL_ID")


SYSTEM = f"You are a coding agent at {WORK_DIR}. All destructive operations require user approval."


DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda", "del"]

# 定义tool方法实现

def run_bash(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=WORK_DIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = (WORK_DIR / path).resolve().read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        file_path = (WORK_DIR / path).resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = (WORK_DIR / path).resolve()
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORK_DIR):
            if (WORK_DIR / match).resolve().is_relative_to(WORK_DIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


TOOLS:list[dict] = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]

TOOL_HANDLERS: dict[str, Callable[[any], str]] = {}

def regist_tool(tool_name:str, tool:Callable[[any], str]) -> None:
    TOOL_HANDLERS.update({tool_name:tool})
    return None


# 定义权限拦截函数

def check_deny(args: dict) -> str|None:
    for p in DENY_LIST:
        if p in args.get("command", ""):
            return "dangerous command"
    return None

PERMISSION_LIST = [
    {"tools": ["read_file", "write_file", "edit_file"],
     "check": lambda args: not (WORK_DIR / args.get("path", "")).resolve().is_relative_to(WORK_DIR),
     "message": "Writing outside workspace"},
    {"tools": ["bash"],
     "check": lambda args: any(kw in args.get("command", "") for kw in ["rm ", "> /etc/", "chmod 777"]),
     "message": "Potentially destructive command"},
]

def check_rules(tool_name:str, args:dict) -> str|None:
    for rule in PERMISSION_LIST:
        if tool_name in rule["tools"]:
            if rule["check"](args):
                return rule["message"]
    return None

def ask_user(tool_name:str, args:dict) -> str|None:
    print(f"工具：{tool_name}({args})将要执行")
    answer = input("Allow [y/n]?").strip().lower()
    if answer in ["y", "yes"]:
        return "allow"
    return "deny"

def agent_loop(messages:list[dict]) -> None:
    while True:
        try:
            with client.messages.stream(max_tokens = 16000, messages = messages, model = MODEL_ID, system = SYSTEM, tools = TOOLS) as stream:
                response = stream.get_final_message()
        except Exception as e:
            print(f"occured error: {e}")
            return None

        messages.append({"role":"assistant", "content":response.content})
        if response.stop_reason != "tool_use":
            print(f"========{messages}========")
            return None

        tool_result = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool = TOOL_HANDLERS.get(block.name)
            if tool:
                if check_deny(block.input):
                    print("危险命令拒绝执行")
                    return None
                if check_rules(block.name, block.input):
                    user_permission = ask_user(block.name, block.input)
                    if "deny" == user_permission:
                        return None
                result = tool(**block.input)
                tool_result.append({"tool_use_id":block.id, "type":"tool_result", "content":result})

        messages.append({"role":"user", "content":tool_result})
    return None

def main() -> None:
    regist_tool("bash", run_bash)
    regist_tool("read_file", run_read)
    regist_tool("write_file",run_write)
    regist_tool("edit_file", run_edit)
    regist_tool("glob", run_glob)

    query = input("请输入命令")
    if query.strip().lower() in ["q", "exit"]:
        return None
    history = [{"role":"user", "content":query}]
    agent_loop(history)


if __name__ == '__main__':
    main()

