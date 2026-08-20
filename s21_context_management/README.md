# s21: Agent Context Management — 跑三天三夜的任务，怎么塞进一个有限的上下文

[中文](README.md) · [English](README.en.md) · [日本語](README.ja.md)

s01 → s02 → ... → s08 → ... → s20 → `s21`
> *"上下文是一本台账，不是一份完整速记。"* — 委派、有界台账、阶梯压缩、逐项检查点，让 agent 在有限上下文里**无界地持续工作**。
>
> **Harness 层**: 上下文管理 — 三天不眠的 agent。

---

## 问题

让它跑三天三夜，它第二天中午就死了。

Agent 有 bash、有 read、有 write，能力是够的。但 72 小时的任务意味着：几千次工具调用、几万条消息、几十个文件的内容。全堆在 `messages` 里，上下文窗口（几十万 token ≈ 几小时的密集工作）早就爆了。

两个层面都扛不住：

- **上下文会满**。s08 解决了这个：满了再腾地方（snip → micro → budget → LLM 摘要）。但压缩是在"已经不该进上下文的东西"上做二次处理——它能拖延，不能根治。
- **进度会丢**。跑两天，一个异常、一次断电、一次 API 报错，所有内存里的状态清零。没有持久化的"我跑到哪了"，重启就得从头再来。

本节的答案不是"把上下文做得更大"，而是三个更根本的机制：

> 别把 72 小时塞进有限上下文——让它**根本不需要塞进去**。

---

## 方案总览

```
                    ┌─────────────────────────────────────────────┐
                    │             主上下文（有界台账）              │
                    │  目标 + 进度 + 一行一行的结论，始终 ≤ 几 KB   │
                    └──────┬───────────────────────────┬──────────┘
                           │ 委派：每项一次全新子上下文   │ 压缩阶梯：快满了就整理
                           ▼                           ▼
              ┌─────────────────────┐        ┌──────────────────────┐
              │   子上下文（一次性）  │        │ L1 snip → L4 LLM 摘要 │
              │  只看到这一项任务     │        │ 便宜的先跑贵的后跑     │
              │  返回一行结论         │        └──────────────────────┘
              └─────────┬───────────┘
                        │ 一行报告回写台账
                        ▼
              ┌─────────────────────┐
              │   逐项检查点（落盘）  │ ← 每处理一项，原子写一次
              │   checkpoint.json   │    磁盘上的工作量无界增长
              └─────────────────────┘
```

三个机制，按重要性排序：

1. **委派（Delegation）** — 每个任务项由一次**全新的、有界的子上下文**处理，只把一行结论带回。主上下文永远只累积"谁、什么时候、什么结果"这类台账条目，不累积任何完整记录。→ 这是"不让上下文长起来"的根本手段。
2. **压缩阶梯（Compaction ladder）** — 复用 s08：先便宜（按条数裁中间），后贵（LLM 全文摘要）。即便台账是短条目，72 小时也会攒到几万条，阶梯负责把历史"折叠"成一段可继续工作的摘要。
3. **逐项检查点（Checkpoint-per-item）** — 每处理完一项，状态原子落盘。崩溃、断电、杀进程，下次启动自动恢复，**已做完的绝不重做**。

一句话：**总工作量在磁盘上无界增长，上下文里的消息数始终有界。**

---

## 工作原理

### 场景设定

为了把"三天三夜"放进一次演示，我们把时间压扁：72 个任务项 = 3 天 × 24 小时，每个循环迭代代表 1 小时。每项是一个"深夜值班分析师"任务——给出一段节点配置（cpu / mem / region / targets），判断该节点是否健康：

```
你是值班分析师。分析 node-14 的配置：
  [配置文本 ~250 字符]
只返回一行：healthy 或 anomaly: <什么> <值>
```

配置里有秘密规则（cpu > 85% 或 mem > 90% 即异常），模型不知道规则，但 demo 用规则校验它的判断——"异常命中率"成了学习笔记里的真实 QA 数字。

### 机制 1：委派 — 全新的有界子上下文

每一项任务，主上下文**不亲自处理**，而是派给一个全新的子上下文：

```python
def run_sub_agent(item, *, client, model):
    prompt = build_sub_prompt(item)              # 只包含这一项
    response = client.messages.create(
        model=model, system=SUB_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300)                          # 一次调用，有界
    return extract_text(response.content).strip()
```

主上下文只收到一行"账目"：

```python
def delegation_record(idx, item, report):
    return [
        {"role": "user",      "content": f"[h{hour} {node} {region}] cpu={cpu} mem={mem} delegated"},
        {"role": "assistant", "content": f"[h{hour}] {report}"},
    ]
```

**对照 naive 模式**（`--naive`）：如果每项都把完整配置 + 完整报告塞进主上下文，72 项会累积几万字符，几十项就爆。demo 里用同一个 `context_limit` 跑 naive，让它在一二十项处当场溢出、打印诊断——这就是"委派"救下来的东西。

> 真实 Claude Code 的 `task` 工具（s06/s12）就是这么做的：子代理只把**最终结论**带回主对话，它的完整工具调用记录留在子上下文里。s08 的 micro_compact / tool_result_budget 之所以在本架构的主上下文里用不上，正是因为委派把"大工具输出"挡在了子上下文层。

### 机制 2：压缩阶梯 — 便宜的先跑贵的后跑

从 s08 原样搬来并简化：主上下文是纯字符串台账，没有 `tool_use`/`tool_result` 大块，所以 L2 micro / L3 budget 用不上，保留 L1 snip + L4 LLM 摘要：

```python
def compact_ladder(messages, context_limit, max_messages):
    snip_count = compact_count = 0
    if len(messages) > max_messages:          # 便宜：按条数裁中间
        messages[:] = snip_compact(messages, max_messages)
        snip_count = 1
    if estimate_size(messages) > context_limit:  # 贵：LLM 全文摘要
        messages[:] = compact_history(messages)  # 转存 transcript + 1 次摘要调用
        compact_count = 1
    return snip_count, compact_count
```

`summarize_history` 是唯一的"贵"操作，也是测试的注入缝：`--mock-summary` 切换成本地确定性实现，单测里 monkeypatch 掉它验证 `compact_history` 的输入输出。

**真实的触发节奏**：默认阈值（`max_messages=24`、`context_limit=1200`）下，72 项跑下来 LLM 摘要触发 ~9 次、snip 触发 0 次——因为委派把每项压到 ~130 字符，台账在按大小超限之前根本攒不到 24 条。这本身是委派的胜利：snip 是"按条数"的便宜手段，台账条数不够多它就无处可裁。想看 snip 干活，把 `--max-messages` 调低（如 `--max-messages 4 --context-limit 3000`），条数先超限，snip 成为主力。两个阈值就是两级阶梯的"旋钮"。

### 机制 3：逐项检查点 — 状态比上下文活得更久

每处理完一项，立刻落盘：

```python
checkpoint.next_index = idx + 1
checkpoint.updated_at = now()
checkpoint.save(state_dir / "checkpoint.json")   # .tmp + os.replace，原子写
```

- `next_index` 是**唯一真相源**：它等于"已完成的项数"，也是下次启动的起点。
- 原子写保证任何时刻磁盘上的 checkpoint 要么是旧的、要么是新的，不会半截。
- 崩溃演示：`--simulate-crash-after 3` 在第 3 项后 `exit(3)`；再次 `run` 自动从第 4 项续，work_log 里索引不重不漏。

### 兜底：reactive_compact

s08 的 `reactive_compact`（API 报 `prompt_too_long` 时应急裁剪）原样保留，但**在本架构中触发不到**——主上下文从不作为完整 prompt 发给 API（只有小的子上下文 prompt 和截断到 80k 字符的摘要输入）。它被保留、被单测、被文档化为"编排型 LLM 架构"（s01 那种主循环每轮都把全量消息发给模型）的兜底。这是诚实的设计：说明一个机制在什么架构下有意义、在什么架构下不必要。

---

## 数据流

单次迭代（managed 模式）：

```
make_item(idx)                      # 确定性生成第 idx 项
   → run_sub_agent(item)            # 1 次真实 LLM 调用（全新子上下文）
   → 台账追加 2 条短消息            # 只有一行结论
   → compact_ladder(...)            # 超 max_messages 条 → snip；仍超 context_limit 字符（默认 1200）→ LLM 摘要
   → work_log 追加 1 行             # 磁盘上无界累积
   → checkpoint 原子落盘            # next_index = idx + 1
   → 可选：simulated crash (exit 3)
```

72 次迭代后：台账 ≤ 几 KB、消息数 ≤ 24（snip 保证）；work_log 72 行；默认阈值下 LLM 压缩事件 ~9 次；真实 LLM 调用 ≈ 72（子上下文）+ 压缩次数（摘要）。

---

## 运行方式

```bash
python s21_context_management/s21_code.py [run] [flags]
```

| 命令 | 作用 | 观察点 |
|---|---|---|
| `run --reset` | 清空状态，从头跑 72 项 | 逐小时 `[h13] node-14 ... healthy`、`[snip]`/`[auto compact]` 标记、末尾总结表 |
| `run`（有 checkpoint 时） | 自动恢复未完成进度 | 从 `next_index` 续跑，不重做 |
| `run --limit 3` | 只跑 3 项冒烟 | 3 条真实报告、`next_index==3` |
| `run --limit 5 --simulate-crash-after 3` | 第 3 项后模拟崩溃 | exit 3；再 `run` 从第 4 项续，log 索引唯一 |
| `run --naive --reset` | 对照：无委派/无压缩 | 一二十项内溢出，打印诊断 |
| `run --mock-summary` | 摘要用本地确定性实现 | 免 API 的摘要调用，可离线演示 |
| `run --max-messages 4 --context-limit 3000` | 让按条数的 snip 先超限 | `snips` 变为主力、LLM 压缩 0 —— 看 L1 干活 |
| `run --context-limit 2000 --max-messages 16` | 调低阈值，让压缩更频繁 | 压缩事件密度变化 |

运行时产物在 `.state/s21/`（已 gitignore）：`checkpoint.json`、`work_log.jsonl`、`transcripts/`。

---

## 变更表（相对 s08）

| 组件 | s08 | s21 | 原因 |
|---|---|---|---|
| 压缩触发 | 每轮 LLM 调用前 | 每项处理完后（runner 内） | 主上下文不再直接发 API |
| L1 snip_compact | ✓ | ✓ 原样 | 按条数裁中间，通用 |
| L2 micro_compact | ✓ | ✗ 省略 | 主上下文无 tool_result 大块（委派挡掉了） |
| L3 tool_result_budget | ✓ | ✗ 省略 | 同上 |
| L4 compact_history | ✓ | ✓ 改造 | 摘要 prompt 改为"台账摘要" |
| reactive_compact | ✓ | ✓ 保留作兜底 | 编排型架构才触发 |
| **委派** | 有子代理工具 | **每项必委派** | 核心新增 |
| **逐项检查点** | 无 | **每项原子落盘 + resume** | 核心新增 |

---

## 深入 CC 源码

教学实现对应真实 Claude Code 的哪些机制：

- **子代理只返回结论** — CC 的 `Task`/`Agent` 子代理跑在隔离的 `messages[]` 里，完成后只把最终输出汇回主对话；主上下文的"账本"性质与本节一致。s06/s12 的 README 有对应设计。
- **autoCompact / microCompact 作用于编排上下文** — CC 在 `query.ts` 里按 budget → snip → micro → contextCollapse → autoCompact 的顺序在主对话上下压缩；本节把压缩从"每轮"降到"每项"，是同一个思想放在确定性 runner 里。
- **持久 checkpoint / `/resume`** — CC 会把会话/任务的进度持久化到磁盘（`.tasks/*.json`、projects 目录），支持中断恢复；本节 `checkpoint.json` 是它的最小教学版。
- **hooks** — CC 用 `SessionStart`/`PreCompact` 等 hook 在关键生命周期注入/保存状态；本节的"每项落盘"可以视为内联的 checkpoint hook。

**诚实的简化差异**（在 notes.md 里展开）：
- 子上下文是**单次调用**、无工具，真实 CC 子代理带工具、多轮；
- token 估算用**字符数代理**（`len(str(msgs))`，同 s08），真实 CC 用精确 tokenizer；
- 用**模拟时钟**（72 项 = 72 小时），真实长跑走真实时间 + 后台任务（s13_background_tasks 的主题）；
- 无 prompt 缓存、无并行委派、transcript 不做语义检索（可接 s09 的记忆层）。

---

## 接下来

台账已经持久化到磁盘（work_log.jsonl），但它是"死"的：追加、不可查询、不可回忆。下一步的自然演进是给台账加**记忆层**——让 agent 能语义检索"三天前我在 node-7 上发现过什么"——这正是 s09 memory 的主题：把无界的磁盘状态变成有界的、可检索的长期记忆。你也可以先跑通本节 demo（`--naive` 看它死、managed 看它活、`--simulate-crash-after` 看它复活），再回到 s08 对比"反应式腾地方"与"主动式不让长起来"的差别。
