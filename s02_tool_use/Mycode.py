#1、加载环境变量；实例化client对象；准备tool、system提示词；
#2、定义工具、agent_loop
#3、main
import os
import subprocess
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from typing import Callable

load_dotenv()
client = anthropic.Anthropic(base_url = os.getenv("ANTHROPIC_BASE_URL"))

WORKDIR = Path.cwd()
SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."
MODEL = os.getenv("MODEL_ID")
TOOLS = [
    {"name":"bash", "description":"", "input_schema":{"type":"object", "properties":{"command":{"type":"string"}}, "required":["command"]}},

]

def bash_run(command:str) -> str:
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return output[:5000] if output else "(no output)"
    except Exception as e:
        print(f"occured error {e}")
        return "error"


TOOL_HANDLERS: dict[str, Callable[[any], str]] = {}

def regist_tool(tool: dict[str, Callable[[str], str]]) -> dict[str, Callable[[str], str]]:
    TOOL_HANDLERS.update(tool)
    return TOOL_HANDLERS


def agent_loop(messages:list[dict]) -> None:
    while True:
        try:
            with client.messages.stream(max_tokens = 16000, model = MODEL, system = SYSTEM, messages = messages, tools = TOOLS) as stream:
                response = stream.get_final_message()
        except Exception as e:
            print(f"occired error:{e}")
            return

        messages.append({"role":"assistant", "content":response.content})

        if response.stop_reason != "tool_use":
            return

        tool_result = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = TOOL_HANDLERS[block.name](**block.input)
            tool_result.append({"tool_use_id":block.id, "type": "tool_result", "content":result})

        messages.append({"role":"user", "content":tool_result})

def main():

    regist_tool({"bash":bash_run})

    query = input("请输入命令")
    history = [{"role":"user", "content":query}]
    agent_loop(history)

if __name__ == '__main__':
    main()