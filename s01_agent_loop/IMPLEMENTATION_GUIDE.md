# s01_agent_loop/s01_code.py 重写指南

## 背景

目标：**从 0 手动重写** `s01_agent_loop/s01_code.py`，在保持手动 agent loop 教育核心的同时，升级到最新的 SDK 用法（streaming、typed exceptions）。使用 DeepSeek Flash 模型。

## 架构

```
REPL 入口(main) → agent_loop(messages) → print_final_response(history)
                       │
                while stop_reason == "tool_use":
                  stream → get_final_message → execute tools → append results
```

## 6 个 Block 实现要点

### Block 1: Imports & Setup（~15行）

```python
#!/usr/bin/env python3
"""docstring..."""
import os, subprocess, sys          # 注意多了 sys
# ... readline try/import 块（不变）
import anthropic                     # 用于异常类: anthropic.NotFoundError 等
from anthropic import Anthropic      # 用于创建 client
from dotenv import load_dotenv
load_dotenv(override=True)
```

**注意**：`import anthropic` 和 `from anthropic import Anthropic` 两者都要。

---

### Block 2: Config（~20行）

不变。保留：

- `ANTHROPIC_BASE_URL` 存在时 pop `ANTHROPIC_AUTH_TOKEN` 的逻辑（DeepSeek 兼容）
- `client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))`
- `MODEL = os.environ["MODEL_ID"]`
- `SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."`
- `TOOLS` — 单一 bash 工具定义

---

### Block 3: `run_bash(command)`（~15行）

逻辑不变，两个健壮性修补：

1. `subprocess.run` 增加 `encoding="utf-8", errors="replace"` — 解决 Windows GBK 崩溃
2. `result.stdout + result.stderr` 改为 `(result.stdout or "") + (result.stderr or "")` — 防止 subprocess 线程崩溃时 stdout 为 None

```python
def run_bash(command: str) -> str:
    DANGEROUS = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    for p in DANGEROUS:
        if p in command:
            return "Error: Dangerous command blocked"
    try:
        result = subprocess.run(
            command, shell=True, cwd=os.getcwd(),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",   # ← 新增
            timeout=120,
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()  # ← 改为 or ""
        return output[:50000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"
```

---

### Block 4: `agent_loop(messages)` — 核心循环（~35行）

这是改动最大的部分，有 4 个变化点。

#### 变化 1：同步 → streaming

```python
# 原版
response = client.messages.create(model=MODEL, ...)

# 新版
with client.messages.stream(model=MODEL, ...) as stream:
    response = stream.get_final_message()
```

`max_tokens` 从 `8000` 升到 `16000`（streaming 不怕超时）。

#### 变化 2：try/except 错误处理

在 stream 调用外层用 try 包裹，按从具体到通用的顺序捕获：

```python
try:
    with client.messages.stream(...) as stream:
        response = stream.get_final_message()
except anthropic.NotFoundError:
    print(f"Error: Model '{MODEL}' not found.", file=sys.stderr)
    return
except anthropic.AuthenticationError:
    print("Error: Invalid API key.", file=sys.stderr)
    return
except anthropic.RateLimitError:
    print("Error: Rate limited. Wait and retry.", file=sys.stderr)
    return
except anthropic.APIStatusError as exc:
    print(f"API error {exc.status_code}: {exc.message}", file=sys.stderr)
    return
except anthropic.APIConnectionError:
    print("Error: Network issue. Check your connection.", file=sys.stderr)
    return
```

每个分支都 `return`，退出循环回到 REPL。

#### 变化 3：`stop_reason == "refusal"` 处理

在判断 `stop_reason != "tool_use"` **之前**加入：

```python
if response.stop_reason == "refusal":
    print("Model refused this request (safety).", file=sys.stderr)
    return
if response.stop_reason != "tool_use":
    return  # end_turn or max_tokens
```

#### 变化 4：打印 request_id

```python
req_id = getattr(response, "_request_id", None)  # 用 getattr 安全获取
if req_id:
    print(f"  [request_id: {req_id}]")
```

#### 不变的部分

```python
# 追加 assistant 消息
messages.append({"role": "assistant", "content": response.content})

# 遍历 tool_use 块，执行，收集结果
tool_results = []
for block in response.content:
    if block.type != "tool_use":
        continue
    print(f"$ {block.input['command']}")
    output = run_bash(block.input["command"])
    print(output[:200])
    tool_results.append({
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": output,
    })

# 回传结果，继续循环
messages.append({"role": "user", "content": tool_results})
```

---

### Block 5: `print_final_response(history)`（~10行）

新增函数，从 history 提取并打印最终文本回复：

```python
def print_final_response(history: list) -> None:
    """Extract and print the model's last text response."""
    last_content = history[-1]["content"]
    if not isinstance(last_content, list):
        return
    for block in last_content:
        if getattr(block, "type", None) == "text":   # 用 getattr 防 dict
            print(block.text)
```

---

### Block 6: `main()` — REPL 入口（~15行）

逻辑和原版一致，只是调用 `print_final_response` 而不是内联提取：

```python
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
        print_final_response(history)   # 改为函数调用
        print()

if __name__ == "__main__":
    main()
```

---

## 刻意跳过的特性（DeepSeek 不兼容）

以下 Claude 专有特性**不要**加入代码：

| 特性 | 原因 |
|------|------|
| `thinking` / `output_config.effort` | DeepSeek 不支持 |
| `task_budget` | DeepSeek 不支持 |
| `client.beta.*` | DeepSeek 无 beta 命名空间 |
| `stop_details` | Claude 专属 |

---

## 验证步骤

```bash
# 1. 确认 .env 已配置 ANTHROPIC_API_KEY, MODEL_ID, ANTHROPIC_BASE_URL

# 2. 运行（用 Module name 模式避免 stdlib code 冲突）
cd G:/learn_claude_code/learn-claude-code
.venv/Scripts/python.exe -m s01_agent_loop.code

# 3. 测试
#    输入: list files in current directory
#    预期: 模型调用 ls/dir → 输出文件列表 → 模型给出文本回复
#    输入: q
#    预期: 退出程序
```
