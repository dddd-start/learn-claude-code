# s21 学习笔记：Agent 上下文管理 — 让任务跑三天三夜

> 对应代码：`s21_code.py`。配套设计文档：同目录 `README.md`。
> 本文是用真实运行数据写的走读总结，数字都来自 `python s21_code.py run --reset` 的实测输出。

---

## 1. 缘起：为什么"上下文窗口"是长命 agent 必须绕开的瓶颈

一个 agent 跑三天三夜，会经历几千次工具调用、几万条消息。上下文窗口再大（几十万 token）也只是几小时的密集工作量。两个现实问题：

1. **上下文会满** — 满了 API 直接 `prompt_too_long` 拒掉。s08 的解法是"满了再压缩"（snip → micro → budget → LLM 摘要）。
2. **进度会丢** — 跑两天断电，内存里的状态清零，重启从头再来。没有持久化的"我跑到哪了"。

但压缩只是拖延。真正的问题是：**把 72 小时的完整记录塞进一个有限窗口，这个目标本身是错的。**

s21 的核心论点：

> 别把 72 小时塞进有限上下文 —— 让它**根本不需要塞进去**。

---

## 2. 三个关键决策

实现时在几个方案之间犹豫过，最终选了这三个，按重要性排序：

### 决策 1：委派 — 每项任务开一个全新的有界子上下文

不亲自处理每项任务，而是派给一个只含该项内容的子上下文（单次调用、无工具），只把一行结论带回：

```python
response = client.messages.create(model=model, system=SUB_SYSTEM,
    messages=[{"role": "user", "content": build_sub_prompt(item)}],
    max_tokens=300)
```

主上下文只累积"账目"：`[h13 node-14 us-east-1] cpu=55 mem=44 targets=3 delegated` + `[h13] healthy`。

**这是最大的一根杠杆**：主上下文的增长是"每项一行结论"的常数级，与任务本身规模无关。被否决的备选：
- 每项派一个**带工具的多轮子代理**（s06 那种）→ 成本高、每项 3-30 次调用，教不了"上下文管理"这一层；
- 主循环每项都走 **LLM 编排**（s01 那种）→ 成本翻倍且容易抖，不是本 demo 要证明的东西；
- 把**完整记录都放主上下文**（naive 模式）→ 正是我们要证伪的。

### 决策 2：压缩阶梯 — 便宜的先跑贵的后跑

复用 s08 的 `snip_compact`（按条数裁中间）和 `compact_history`（LLM 全文摘要），主上下文是纯字符串台账，L2 micro / L3 budget 用不上（大工具输出被委派挡在子上下文层了）。`--mock-summary` 把摘要换成确定性本地实现，单测里 monkeypatch `summarize_history`，两层注入缝让整个压缩路径无 API 可测。

### 决策 3：逐项检查点 — 状态比上下文活得更久

每处理一项，`checkpoint.json` 原子落盘（`.tmp` + `os.replace`），`next_index` 是唯一真相源。崩溃、断电、杀进程，下次启动从 `next_index` 续，绝不重做。

---

## 3. 代码走读（按 Block）

```
s21_code.py
├── Block 1  imports / client / MODEL          # 照抄 s01/s08/s12 的 env 约定
├── Block 2  DEFAULT_TOTAL=72 等                # 两个阈值是"旋钮"
├── Block 3  make_item / build_sub_prompt      # 确定性任务生成，anomalies 是秘密
├── Block 4  estimate_size / snip_compact /    # 压缩阶梯
│            compact_history / compact_ladder
├── Block 5  Checkpoint（dataclass + 原子写）   # 状态持久化
├── Block 6  call_with_retry / run_sub_agent   # 委派边界
├── Block 7  Runner.run()                      # 确定性编排循环
└── Block 8  CLI + main
```

关键函数关系（一次迭代）：

```
make_item(idx) → run_sub_agent(item)          # 1 次真实 LLM 调用（全新子上下文）
  → delegation_record(idx, item, report)      # 主台账 +2 条短消息
  → compact_ladder(messages, ...)             # 超条数 → snip；超字符 → LLM 摘要
  → work_log_append(...)                      # 磁盘 +1 行（无界）
  → checkpoint.save()                         # next_index = idx + 1（原子）
```

两个值得注意的注入缝（也是单测能无 API 跑的关键）：
- `Runner.delegate_fn` 默认 `run_sub_agent`，测试换成 `lambda item: "healthy"`；
- 模块级 `summarize_history` 默认真模型，测试 monkeypatch 成确定性返回。

---

## 4. 真实运行记录（默认阈值 max_messages=24 / context_limit=1200）

### 4.1 管理模式下 72 项跑完

```
items completed:  72 / 72
in-context msgs:  5  (441 chars)        ← 主上下文始终有界
work_log lines:   72  (disk, unbounded) ← 磁盘工作量无界
real anomalies:   14
caught (TP):      13    false alarms: 7    missed: 1
snips:            0
LLM compactions:  9
real LLM calls:   ~81 (1 per item + 1 per compaction)
```

**这是本 demo 最重要的两个数字**：`in-context msgs: 5 (441 chars)` 对 `work_log lines: 72`。三天三夜的全部工作（72 小时）在主上下文里只剩 5 条消息 441 字符，完整记录在磁盘上攒了 72 行。**有界 vs 无界，一目了然。**

### 4.2 压缩发生了什么（短运行摘录）

`run --limit 12 --mock-summary`：

```
  [h1 node-1] healthy
  [h2 node-2] healthy
  [h3 node-3] anomaly: memory 96%
  [h4 node-4] healthy
  [h5 node-5] healthy
  [h6 node-6] healthy
[auto compact]
[transcript saved: ...transcripts/transcript_1786627544.jsonl]
  [h7 node-7] anomaly: cpu 90%
  ...
```

第 6 项后台账超 1200 字符 → 触发 L4：先 `write_transcript` 把全量台账落盘（压缩不丢原始记录），再 1 次 LLM 摘要把台账塌缩成单条 `[Compacted]` 消息。72 项一共触发 9 次。

### 4.3 一个诚实的观察：snips = 0

默认阈值下按条数的 snip 一次没触发。原因：委派把每项压到 ~130 字符，台账在按大小超限（1200 字符 ≈ 8-9 项）之前，根本攒不到 24 条。**snip 是"按条数"的便宜手段，条数不够多它就无处可裁**——这本身就是委派的胜利。

想看 snip 干活：`--max-messages 4 --context-limit 3000`，实测 `snips: 4, LLM compactions: 0`。两个阈值就是两级阶梯的旋钮：默认 size 赢，调低条数阈值 count 赢。

### 4.4 模型行为的真实噪声（QA 数字背后）

work_log 里存了"秘密验收标准"，统计出真实模型的几个怪癖：
- **空报告**：72 项里 ~6 次返回 `(no report)`（子上下文单次调用会偶发空返回，需要 `or "(no report)"` 兜底）；
- **过度告警**：7 次误报，集中在 `anomaly: memory 88%` 这类——mem 没到 90 模型也报，对 memory 明显偏激进；
- **截断**：偶尔返回 `anomaly:` 后面没内容；
- **规则外异常**：报过 `anomaly: latency_p99 120`——模型把延迟也当异常，而秘密规则只认 cpu/mem。

这正好说明：**委派的结论质量取决于子上下文单次判断的质量**，光有"上下文管理"不够，下游还需要对结论做校验（这里就是 `caught_anomaly` 的判据）。

### 4.5 成本估算

~81 次 LLM 调用：72 次子上下文（每次输入 ~350 字符 ≈ 100 token）+ 9 次摘要（**输入就是台账本身，≤1300 字符** ≈ 400 token——因为台账被压得很小，连摘要都很便宜）。总输入 token 约 **1.5–2 万**，按 DeepSeek flash 级别定价远低于 $0.01，两三分钟跑完。**"跑三天三夜"的成本被压到了近乎免费**——这得益于委派把每次调用压到极小、摘要只在必要时才做、且摘要的输入本身就是有界的。

---

## 5. 崩溃恢复演示

### 5.1 模拟崩溃

```
$ python s21_code.py run --reset --limit 5 --simulate-crash-after 3
  [h1 node-1] healthy
  [h2 node-2] healthy
  [h3 node-3] anomaly: memory 96%
[simulated crash] after item 3 - checkpoint saved, exit(3).
```

checkpoint：`next_index: 3`。第 3 项完成后立即落盘，然后 `exit(3)`。

### 5.2 恢复

```
$ python s21_code.py run
  [h4 node-4] healthy
  [h5 node-5] healthy
items completed: 5 / 5
```

从第 4 项续跑，不重做 1-3。work_log 索引是 `[0,1,2,3,4]`——**不重不漏**。单测 `test_resume_starts_at_next_index_and_no_double_work` 用 fake delegate 验证了同一件事（不需要真模型）。

### 5.3 为什么能恢复

关键在 checkpoint 的写入时机：**每项成功后、下一项开始前**。所以任何时刻磁盘上的状态要么是"第 N 项已完成"、要么是"第 N 项进行中"，绝没有"第 N+1 项完成但第 N 项没落盘"的中间态。原子写（`.tmp` + `os.replace`）保证文件不会半截。

### 5.4 naive 对照：不管理的下场

```
$ python s21_code.py run --reset --naive
  [h1 node-1] healthy
  [h2 node-2] (no report)
[naive overflow] context exceeded 1200 chars at item 2 (day 1, hour 3).
  Managed mode handles all 72; naive died here.
```

naive 每项把完整配置 + 报告塞进主上下文，第 3 项就溢出。**managed 72 项、naive 3 项——差的不是模型，是架构。**

---

## 6. 局限与改进

| 本 demo 的简化 | 真实 Claude Code 的做法 | 差距意味着什么 |
|---|---|---|
| 子上下文**单次调用**、无工具 | 子代理带工具、多轮（s06/s12） | 真实委派能完成"读文件→改代码→验证"这类真活，本 demo 只做判断 |
| token 用**字符数代理**（`len(str(msgs))`） | 精确 tokenizer | 字符≠token，阈值要按经验调 |
| **模拟时钟**（72 项=72 小时） | 真实时间 + 后台任务（s13） | 没演示"长时间等待"这类真实节奏 |
| 顺序委派，一次一项 | 可并行委派（workflow 多 agent） | 吞吐受限于单次调用延迟 |
| transcript 是**死文件** | 可语义检索的记忆层（s09） | 压缩丢掉的细节无法回查，除非有记忆索引 |
| 无 prompt 缓存 | 真实 CC 有缓存 | 成本还能再降 |
| 摘要只保留"台账摘要" | sessionMemoryCompact 等更细机制 | 摘要质量决定长跑中"还记得什么" |

改进方向（如果要做成真的长跑系统）：
1. **给摘要加记忆索引**：压缩前把 transcript 的关键结论写入可检索的记忆（接 s09），让"三天前 node-7 上发现过什么"可查；
2. **并行委派**：同一小时的多项配置同时派，加一个聚合层；
3. **空返回重试**：对 `(no report)` 做一次针对性重试（当前只对 API 错误重试，没对空内容重试）；
4. **真实时钟 + 后台调度**：接 s13/s14，把"72 小时"从模拟变成真实运行。

---

## 7. 收获：三个心智模型

1. **先持久化，再前进（persist before you progress）**
   每完成一项就落盘，崩溃才有得救。checkpoint 不是可选项，是长命 agent 的地基。

2. **"便宜的先跑贵的后跑"用了两次**
   一次在压缩阶梯内部（snip 0 API → LLM 摘要 1 API）；一次在委派与压缩之间（**委派本身是最便宜的压缩**——让东西根本不进上下文，比进了再压便宜得多）。s08 是"满了腾地方"，s21 是"别让它满"。

3. **用数字作证据，不靠感觉**
   72 项跑完：`in-context 5 msgs/441 chars` vs `work_log 72 行`。naive 第 3 项溢出。9 次压缩、13/14 命中、7 次误报。这些数字让"架构对了"不是口号，是可以贴进 review 里的证据。
