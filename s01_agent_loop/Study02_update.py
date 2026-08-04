#1、加载环境变量；实例化client对象；准备tool工具定义说明、系统提示词；
#2、定义工具
#3、agent_loop
#4、main
import os
import subprocess

import anthropic
from dotenv import load_dotenv

load_dotenv()
MODEL_ID = os.getenv("MODEL_ID")
client = anthropic.Anthropic(base_url = os.getenv("ANTHROPIC_BASE_URL"))
SYSTEM = (f"You are a coding agent on Windows (cmd.exe) at {os.getcwd()}. "
          f"Use Windows commands: dir, cd, type, echo, mkdir, copy, move, del, findstr. "
          f"Act, don't explain.")

TOOL = [{
    "name": "bash",
    "description": "",
    "input_schema":{"type":"object", "properties":{"command":{"type":"string"}}, "required":["command"]},
}]

DANGEROUS = ["rm -rf /"]
def run_bash(command:str) -> str:
    if any(p in command for p in DANGEROUS):
        return "dangerous"
    try:
        bash_result = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        result = ((bash_result.stdout or "") + (bash_result.stderr or "")).strip()
        return result[:5000] if result else "(no output)"
    except Exception as e:
        return f"occer error: {e}"


def agent_loop(messages:list[dict]) -> None:
    while True:
        try:
            with client.messages.stream(messages = messages, model = MODEL_ID, system = SYSTEM, tools = TOOL, max_tokens = 16000) as stream:
                response = stream.get_final_message()
        except Exception as e:
            print(f"error: {e}")

        messages.append({"role":"assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        tool_result = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            bash_result = run_bash(block.input["command"])
            tool_result.append({"tool_use_id":block.id, "type":"tool_result", "content":bash_result})

        messages.append({"role":"user","content":tool_result})

if __name__ == '__main__':
    query = input("请输入命令")
    history:list[dict] = [{"role":"user", "content":query}]
    agent_loop(history)
