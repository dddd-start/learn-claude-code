#!/usr/bin/env python3
"""
s21_context_management - Agent Context Management

让 agent 能跑一个三天三夜（72 小时）的任务，即使上下文窗口有限。

核心论点：别把 72 小时塞进有限上下文 -- 让它根本不需要塞进去。
三个机制（按重要性排序）：

  1. 委派（Delegation）   - 每个任务项由一次全新的、有界的子上下文处理，
                            只把一行结论带回。主上下文只累积"台账"条目。
  2. 压缩阶梯（Ladder）   - 复用 s08：先便宜（按条数 snip），后贵（LLM 摘要）。
  3. 逐项检查点（Checkpoint）- 每处理一项就原子落盘，崩溃后 resume 不丢进度。

场景：72 个任务项 = 3 天 × 24 小时；每项是"值班分析师"判断一段节点配置是否健康。
管理模式下主上下文始终 ≤ 几 KB；naive 对照模式一二十项内溢出。

Run:
    python s21_context_management/s21_code.py run --reset
    python s21_context_management/s21_code.py run --limit 3            # 冒烟
    python s21_context_management/s21_code.py run --simulate-crash-after 3   # 崩溃演示
    python s21_context_management/s21_code.py run --naive              # 溢出对照
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY / MODEL_ID
"""

# ═══════════════════════════════════════════════════════════════
# Block 1: Imports & Setup
# ═══════════════════════════════════════════════════════════════

import os
import sys
import json
import time
import random
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Callable

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass  # Windows / no readline - fine

import anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = anthropic.Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

STATE_DIR = Path.cwd() / ".state" / "s21"   # runtime artifacts, gitignored


# ═══════════════════════════════════════════════════════════════
# Block 2: Configuration
# ═══════════════════════════════════════════════════════════════

DEFAULT_TOTAL = 72            # 3 天 × 24 小时
DEFAULT_CONTEXT_LIMIT = 1200  # 字符级估算阈值（len(str(messages)) 代理 token）
DEFAULT_MAX_MESSAGES = 24     # snip 按条数阈值
MAX_REACTIVE_RETRIES = 1      # 兜底保留（本架构不触发）
SUB_MAX_TOKENS = 300          # 子上下文单次调用的输出上限
SUB_SYSTEM = (
    "You are an on-call analyst for a simulated fleet shift. "
    "Analyze the given config and return exactly one finding line. "
    "No tools, no preamble."
)


# ═══════════════════════════════════════════════════════════════
# Block 3: Item model & workload (deterministic)
# ═══════════════════════════════════════════════════════════════

def make_item(idx: int, seed: int = 42) -> dict:
    """确定性生成第 idx 个任务项（1 小时 = 1 项）。

    anomalies 是"秘密验收标准"：只用于 work_log 统计，绝不进 prompt。
    """
    rng = random.Random(seed + idx)
    node_num = idx + 1
    region = ["us-east-1", "eu-west-1", "ap-southeast-2"][idx % 3]
    cpu = rng.randint(20, 95)
    mem = rng.randint(30, 98)
    targets = rng.randint(1, 5)
    anomalies = []
    if cpu > 85:
        anomalies.append("cpu")
    if mem > 90:
        anomalies.append("mem")
    config = (f"node-{node_num} ({region}) - load report: cpu {cpu}%, memory {mem}%, "
              f"{targets} upstream targets, autoscale enabled, latency p99 120ms.")
    return {
        "index": idx,
        "day": idx // 24 + 1,
        "hour": idx % 24 + 1,
        "node": f"node-{node_num}",
        "region": region,
        "cpu": cpu,
        "mem": mem,
        "targets": targets,
        "config": config,
        "anomalies": anomalies,
    }


def build_sub_prompt(item: dict) -> str:
    """只包含这一项的内容 -- 子上下文永远只看得到它被派去的那一项。"""
    return (
        f"You are the on-call analyst for a simulated fleet shift.\n"
        f"Analyze the config for {item['node']} (day {item['day']}, hour {item['hour']}):\n\n"
        f"{item['config']}\n\n"
        f"Return EXACTLY ONE line: 'healthy' or 'anomaly: <what> <value>'.\n"
        f"Do not add any other text."
    )


def mission_ledger_init(total_items: int = DEFAULT_TOTAL) -> list:
    """主上下文的初始"简报" -- 只有一条，是 snip 保留的头部上下文。"""
    return [{
        "role": "user",
        "content": (f"Mission: monitor a simulated fleet for {total_items} hours (3 days). "
                    f"Delegate each hour's config to the analyst; log one-line findings only. "
                    f"Keep this ledger compact - never paste full configs here."),
    }]


def delegation_record(idx: int, item: dict, report: str) -> list:
    """主台账里的一行账目（user 摘要 + assistant 一行结论）。

    完整 config 只在子上下文里，永远不进主上下文 -- 这是委派的核心。
    """
    h, node = item["hour"], item["node"]
    return [
        {"role": "user", "content": f"[h{h} {node} {item['region']}] cpu={item['cpu']} mem={item['mem']} targets={item['targets']} delegated"},
        {"role": "assistant", "content": f"[h{h}] {report}"},
    ]


# ═══════════════════════════════════════════════════════════════
# Block 4: Context budget & compaction ladder (from s08, simplified)
# ═══════════════════════════════════════════════════════════════

def estimate_size(messages: list) -> int:
    """字符级 token 代理：len(str(messages))。与 s08 一致。"""
    return len(str(messages))


def extract_text(content) -> str:
    if not isinstance(content, list):
        return str(content)
    return "\n".join(getattr(b, "text", "")
                     for b in content if getattr(b, "type", None) == "text")


def _message_has_tool_use(msg: dict) -> bool:
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(getattr(b, "type", None) == "tool_use" for b in content)


def _is_tool_result_message(msg: dict) -> bool:
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_result"
               for b in content)


def snip_compact(messages: list, max_messages: int = DEFAULT_MAX_MESSAGES) -> list:
    """L1: 按条数裁中间，保头部（任务简报）和尾部（当前工作）。

    与 s08 原样一致：不拆散 tool_use/tool_result 配对。
    主台账是纯字符串消息，配对保护是 no-op，但保持仓库内实现一致。
    """
    if len(messages) <= max_messages:
        return messages
    keep_head, keep_tail = 3, max_messages - 3
    head_end, tail_start = keep_head, len(messages) - keep_tail
    if head_end > 0 and _message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_result_message(messages[head_end]):
            head_end += 1
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    if head_end >= tail_start:
        return messages
    snipped = tail_start - head_end
    placeholder = {"role": "user", "content": f"[snipped {snipped} messages from ledger middle]"}
    return messages[:head_end] + [placeholder] + messages[tail_start:]


def write_transcript(messages: list, transcripts_dir: Path | None = None) -> Path:
    """压缩前把全量台账快照到磁盘（JSONL），保证压缩不丢原始记录。"""
    transcripts_dir = Path(transcripts_dir) if transcripts_dir else (STATE_DIR / "transcripts")
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    path = transcripts_dir / f"transcript_{int(time.time())}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    return path


def summarize_history(messages: list) -> str:
    """L4: 真实 LLM 摘要。注入缝：测试 monkeypatch 或 --mock-summary 替换。"""
    conversation = json.dumps(messages, default=str)[:80000]
    prompt = ("Summarize this shift-delegation ledger so monitoring can continue.\n"
              "Preserve: 1. mission/goal, 2. anomaly findings so far (node + what), "
              "3. completed hours count, 4. next pending work.\n"
              "Be compact but concrete.\n\n" + conversation)
    response = client.messages.create(
        model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=2000)
    return "\n".join(getattr(b, "text", "") for b in response.content
                     if getattr(b, "type", None) == "text").strip() or "(empty summary)"


def mock_summarize(messages: list) -> str:
    """确定性本地摘要：无 API，用于 --mock-summary 与离线演示。"""
    hours = len([m for m in messages if m.get("role") == "assistant"])
    reports = []
    for m in reversed(messages):
        if m.get("role") == "assistant" and isinstance(m.get("content"), str):
            reports.append(m["content"])
        if len(reports) >= 3:
            break
    return (f"[mock summary] hours logged so far: {hours}. "
            f"last reports: {' | '.join(reversed(reports))}")


def compact_history(messages: list, state_dir: Path | None = None) -> list:
    """L4: 转存 transcript + LLM 摘要，把台账塌缩为单条 [Compacted] 消息。"""
    transcripts_dir = state_dir / "transcripts" if state_dir else None
    path = write_transcript(messages, transcripts_dir)
    print(f"[transcript saved: {path}]")
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


def reactive_compact(messages: list) -> list:
    """应急兜底（s08 原样）：保留尾部 ~5 条，摘要旧历史。

    在本架构（主上下文不发 API）触发不到，保留供编排型 LLM 架构使用。
    """
    write_transcript(messages)
    tail_start = max(0, len(messages) - 5)
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    summary = summarize_history(messages[:tail_start])
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *messages[tail_start:]]


def compact_ladder(messages: list,
                   context_limit: int = DEFAULT_CONTEXT_LIMIT,
                   max_messages: int = DEFAULT_MAX_MESSAGES,
                   state_dir: Path | None = None) -> tuple[int, int]:
    """便宜的先跑贵的后跑：L1 snip（0 API）→ L4 LLM 摘要（1 API）。

    返回 (snip_count, compact_count)。最多 snip 一次 + 摘要一次，无需 while。
    """
    snip_count = compact_count = 0
    if len(messages) > max_messages:
        messages[:] = snip_compact(messages, max_messages)
        snip_count = 1
        print(f"[snip] ledger trimmed to {len(messages)} messages")
    if estimate_size(messages) > context_limit:
        print("[auto compact]")
        messages[:] = compact_history(messages, state_dir=state_dir)
        compact_count = 1
    return snip_count, compact_count


# ═══════════════════════════════════════════════════════════════
# Block 5: Checkpointer - 状态比上下文活得更久
# ═══════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class Checkpoint:
    mode: str = "managed"                 # managed | naive
    next_index: int = 0                   # 唯一真相源：下一项起点 == 已完成项数
    total_items: int = DEFAULT_TOTAL
    snip_count: int = 0
    compaction_count: int = 0
    reactive_count: int = 0               # 本架构恒 0，保留与 s08 叙事对齐
    overflowed: bool = False              # naive 模式专用
    overflow_index: int | None = None
    started_at: str = ""
    updated_at: str = ""
    limit: int = DEFAULT_TOTAL
    context_limit: int = DEFAULT_CONTEXT_LIMIT
    max_messages: int = DEFAULT_MAX_MESSAGES
    work_log_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict, defaults: "Checkpoint | None" = None) -> "Checkpoint":
        base = asdict(defaults) if defaults else {}
        known = {k: v for k, v in d.items() if k in Checkpoint.__dataclass_fields__}
        base.update(known)  # 缺失/新增键用 defaults 填 → schema 可演进
        return Checkpoint(**base)

    def save(self, path: Path) -> None:
        """原子写：先写 .tmp，再 os.replace 覆盖。任何时刻磁盘上都是完整文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def load_or_init(path: Path, defaults: "Checkpoint | None" = None) -> "Checkpoint":
        if path.exists():
            return Checkpoint.from_dict(json.loads(path.read_text(encoding="utf-8")), defaults)
        return defaults if defaults else Checkpoint()


# ═══════════════════════════════════════════════════════════════
# Block 6: Delegator - 全新有界子上下文，只回一行结论
# ═══════════════════════════════════════════════════════════════

def call_with_retry(fn: Callable, *, attempts: int = 3, base_delay: float = 1.0):
    """RateLimit / 连接错误 / 429 → 指数退避重试；其它 API 错误立即抛。"""
    last_exc = None
    for attempt in range(attempts):
        try:
            return fn()
        except anthropic.RateLimitError as exc:
            last_exc = exc
        except anthropic.APIConnectionError as exc:
            last_exc = exc
        except anthropic.APIStatusError as exc:
            if exc.status_code != 429:
                raise
            last_exc = exc
        if attempt < attempts - 1:
            time.sleep(base_delay * (2 ** attempt))
    raise last_exc


def run_sub_agent(item: dict, *, client=client, model=MODEL) -> str:
    """委派边界：1 次真实 LLM 调用，全新子上下文，无工具，返回一行报告。"""
    prompt = build_sub_prompt(item)
    response = call_with_retry(lambda: client.messages.create(
        model=model, system=SUB_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=SUB_MAX_TOKENS))
    return extract_text(response.content).strip() or "(no report)"


# ═══════════════════════════════════════════════════════════════
# Block 7: Runner - 确定性编排循环
# ═══════════════════════════════════════════════════════════════

def work_log_append(path: Path, idx: int, item: dict, report: str) -> dict:
    """追加一行工作记录到磁盘（无界累积，与有界台账对照）。

    real_anomalies 是"秘密验收标准"，只存本地 QA 参考，绝不进 prompt。
    """
    anomalies = item.get("anomalies", [])
    reported = "anomaly" in report.lower()
    caught = bool(anomalies) and any(a in report.lower() for a in anomalies)
    record = {
        "index": idx, "day": item["day"], "hour": item["hour"], "node": item["node"],
        "status": "completed", "report": report,
        "reported_anomaly": reported, "caught_anomaly": caught,
        "real_anomalies": anomalies,
        "ts": _now_iso(),
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_work_log(path: Path) -> list:
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


@dataclass
class Runner:
    mode: str = "managed"
    state_dir: Path = STATE_DIR
    client: object = client
    model: str = MODEL
    limit: int = DEFAULT_TOTAL
    seed: int = 42
    context_limit: int = DEFAULT_CONTEXT_LIMIT
    max_messages: int = DEFAULT_MAX_MESSAGES
    crash_after: int | None = None          # 第 N 项后模拟崩溃（exit 3）
    delegate_fn: Callable = run_sub_agent    # 测试注入缝
    messages: list = field(default_factory=mission_ledger_init)   # 有界台账
    checkpoint: Checkpoint | None = None

    def run(self) -> None:
        defaults = Checkpoint(
            mode=self.mode, total_items=self.limit, limit=self.limit,
            context_limit=self.context_limit, max_messages=self.max_messages,
            work_log_path=str(self.state_dir / "work_log.jsonl"))
        cp = Checkpoint.load_or_init(self.state_dir / "checkpoint.json", defaults)
        if cp.mode != self.mode:
            print(f"[state mismatch] checkpoint is mode={cp.mode}, running mode={self.mode}. "
                  f"Use --reset to start fresh.")
            return
        if cp.overflowed:
            print(f"[already overflowed] naive run died at item {cp.overflow_index}. "
                  f"Use --reset to re-run.")
            return
        self.checkpoint = cp
        if not cp.started_at:
            cp.started_at = _now_iso()

        work_log = self.state_dir / "work_log.jsonl"
        for idx in range(cp.next_index, cp.limit):   # resume 以 checkpoint 的 limit 为准
            item = make_item(idx, self.seed)
            try:
                report = self.delegate_fn(item)          # 1 次真实 LLM 调用
            except Exception as exc:
                print(f"\n[run aborted at item {idx + 1}] {exc}")
                print("[checkpoint is safe - just run again to resume]")
                sys.exit(1)

            if self.mode == "managed":
                self.messages.extend(delegation_record(idx, item, report))
                snips, comps = compact_ladder(
                    self.messages, self.context_limit, self.max_messages, self.state_dir)
                cp.snip_count += snips
                cp.compaction_count += comps
            else:  # naive：无委派边界、无压缩 → 上下文必然溢出
                self.messages.append({"role": "user", "content": build_sub_prompt(item)})
                self.messages.append({"role": "assistant", "content": f"finding: {report}"})
                if estimate_size(self.messages) > self.context_limit:
                    cp.overflowed = True
                    cp.overflow_index = idx
                    cp.save(self.state_dir / "checkpoint.json")
                    print(f"[naive overflow] context exceeded {self.context_limit} chars at item {idx} "
                          f"(day {item['day']}, hour {item['hour']}). "
                          f"Managed mode handles all {cp.limit}; naive died here.")
                    print_summary(self)
                    return

            work_log_append(work_log, idx, item, report)
            cp.next_index = idx + 1
            cp.updated_at = _now_iso()
            cp.save(self.state_dir / "checkpoint.json")
            print(f"  [h{item['hour']} {item['node']}] {report}")

            if self.crash_after == idx + 1:
                print(f"\n[simulated crash] after item {idx + 1} - checkpoint saved, exit(3).")
                sys.exit(3)

        print_summary(self)


def print_summary(runner: "Runner") -> None:
    cp = runner.checkpoint
    print("\n" + "=" * 64)
    print(f"mode:             {cp.mode}")
    if cp.overflowed:
        print(f"overflowed:       YES at item {cp.overflow_index}")
    print(f"items completed:  {cp.next_index} / {cp.limit}")
    if cp.mode == "managed":
        print(f"in-context msgs:  {len(runner.messages)}  ({estimate_size(runner.messages)} chars)")
        work_log = read_work_log(runner.state_dir / "work_log.jsonl")
        print(f"work_log lines:   {len(work_log)}  (disk, unbounded)")
        real = sum(1 for r in work_log if r.get("real_anomalies"))
        caught = sum(1 for r in work_log if r.get("caught_anomaly"))
        false_alarm = sum(1 for r in work_log if r.get("reported_anomaly") and not r.get("caught_anomaly"))
        print(f"real anomalies:   {real}")
        print(f"caught (TP):      {caught}    false alarms: {false_alarm}    missed: {real - caught}")
        print(f"snips:            {cp.snip_count}")
        print(f"LLM compactions:  {cp.compaction_count}")
        print(f"real LLM calls:   ~{cp.next_index + cp.compaction_count} "
              f"(1 per item + 1 per compaction)")
    print("=" * 64)


# ═══════════════════════════════════════════════════════════════
# Block 8: CLI + main
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="s21: Agent Context Management demo")
    parser.add_argument("command", nargs="?", default="run", choices=["run"])
    parser.add_argument("--reset", action="store_true",
                        help="delete .state/s21/* and start fresh")
    parser.add_argument("--limit", type=int, default=DEFAULT_TOTAL, metavar="N")
    parser.add_argument("--naive", action="store_true",
                        help="no delegation, no compaction -> context overflow")
    parser.add_argument("--simulate-crash-after", type=int, default=None, metavar="N",
                        help="exit(3) right after item N completes")
    parser.add_argument("--context-limit", type=int, default=DEFAULT_CONTEXT_LIMIT, metavar="CHARS")
    parser.add_argument("--max-messages", type=int, default=DEFAULT_MAX_MESSAGES, metavar="N")
    parser.add_argument("--mock-summary", action="store_true",
                        help="use deterministic local summarizer instead of an LLM summary call")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    if args.reset:
        import shutil
        if STATE_DIR.exists():
            shutil.rmtree(STATE_DIR)
            print(f"[reset] removed {STATE_DIR}")
        else:
            print("[reset] nothing to remove")

    if args.mock_summary:
        global summarize_history
        summarize_history = mock_summarize
        print("[mock-summary] using deterministic local summarizer")

    runner = Runner(
        mode="naive" if args.naive else "managed",
        limit=args.limit,
        seed=args.seed,
        context_limit=args.context_limit,
        max_messages=args.max_messages,
        crash_after=args.simulate_crash_after,
    )
    try:
        runner.run()
    except SystemExit:
        raise
    except Exception as exc:
        at = runner.checkpoint.next_index + 1 if runner.checkpoint else 1
        print(f"\n[run aborted at item {at}] {exc}")
        print("[checkpoint is safe - just run again to resume]")
        sys.exit(1)


if __name__ == "__main__":
    main()
