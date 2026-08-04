#1、加载环境变量
#2、实例化client对象；准备TOOLS、SYSTEM提示词
#3、定义tool方法；定义权限拦截函数
#4、agent_loop
import os
from pathlib import Path

DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda",]

WORK_DIR = Path.getcwd()

def check_deny_list(command:str) -> str|None:
    for p in DENY_LIST:
        if p in command:
            return f"Blocked: '{p}' is on the deny list"
    return None

PERMISSION_RULES = [
    {"tools":["bash"], "check": lambda args : any(p in args.get("command", "") for p in ["rm ", "> /etc/", "chmod 777"]), "message":"Access outside workspace",},
    {"tools":["read_file",  "write_file", "edit_file"], "check": lambda args : not (WORK_DIR / args.get("path", "")).reolve().is_relative_to(WORK_DIR),  "message": "Potentially destructive command",}
]


def ask_user(tool_name:str, args:dict, reason:str) -> str:
    print(f"\n {reason}")
    print(f"  Tool:{tool_name}({args})")
    choice = input(" Allow? [y/N] ").strip().lower()
    if choice in ["y", "yes"]:
        return "allow"
    return "deny"


def check_rules(tool_name:str, args:dict) -> str|None:
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None