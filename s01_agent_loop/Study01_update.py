#1、加载环境变量
#2、实例化client
#3、定义系统提示词
#4、定义TOOL描述字符串
#5、定义tool方法实现
#6、agent_loop
#7、main方法
import os
import subprocess

import anthropic
from anthropic.types import MessageParam, ToolParam
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv(override = True)
client = anthropic.Anthropic(base_url = os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.getenv("MODEL_ID")
SYSTEM = (f"You are a coding agent on Windows (cmd.exe) at {os.getcwd()}. "
          f"Use Windows commands: dir, cd, type, echo, mkdir, copy, move, del, findstr. "
          f"Act, don't explain.")

TOOLS = [{
    "name":"bash",
    "description":"Run a shell command.",
    "input_schema":{
        "type":"object",
        "properties":{"command":{"type":"string"}},
        "required":["command"],
    }
}]

DANGEROUS = ["rm -rf /"]
def run_bash(command:str) -> str:
    if any( d in command for d in DANGEROUS):
        return "Error: Dangerous command blocked"
    try:
        result = subprocess.run(
            command, shell=True, cwd=os.getcwd(),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=120,
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return output[:5000] if output else "(no output)"
    except Exception as e:
        return f"occured exception {e}"


def agent_loop(messages:list[dict]) -> None:
    while True:
        try:
            with client.messages.stream(
                model = MODEL,
                messages = messages,
                system = SYSTEM,
                max_tokens = 16000,
                tools = TOOLS
            ) as stream:
                response = stream.get_final_message()
        except Exception as e:
            print(e)
            return

        messages.append({"role":"assistant","content":response.content})

        if response.stop_reason != "tool_use":
            return

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            output = run_bash(block.input["command"])
            tool_results.append({"tool_use_id":block.id,"content":output,"type":"tool_result"})

        messages.append({"role":"user","content":tool_results})

if __name__ == '__main__':
    query = input("请输入命令")
    history:list[dict] = [{
        "role": "user",
        "content": query
    }]
    agent_loop(history)
    print()
