# 生产级 Agent 实验室：可观测性与状态工程 (otel-agent-lab)

本项目是一个基于 **LangGraph** 的生产级智能体（Agent）工程实践套件，深入涵盖了两大核心技术板块：

1. **全链路可观测性 (Observability)**：
   - 实现了基于 **OpenTelemetry** 的“自动插桩 (Auto)”与“原生手动打点 (Manual)”双方案深度对照实验，统一上报至本地 **Arize Phoenix** 监控与可视化平台，支持 Trace 瀑布流、Token 消耗统计及调用树解析。
2. **状态持久化、断点恢复与人工在环 (State Engineering & HITL)**：
   - 基于 **Checkpoint 快照持久化**，全面验证真实进程崩溃（Crash Recovery）冷重启零输入断点续跑、工具调用幂等性审计、以及高危操作安全门禁拦截与人工审批恢复（Human-in-the-Loop, HITL）。

---

## 1. 运行环境配置
* **Python 环境**：Python 3.10+ (推荐 3.11)
* **依赖安装**：
  ```bash
  pip install -r requirements.txt
  ```
* **环境配置**：
  复制配置模板并配置相应的 LLM API Key：
  ```bash
  cp .env.example .env
  # Windows PowerShell:
  # Copy-Item .env.example .env
  ```
* **可观测性服务端**：Arize Phoenix (默认端口 `6006`，前端控制台 `http://localhost:6006`)
* **遥测协议**：OpenTelemetry OTLP/HTTP 批处理传输 (`http://localhost:6006/v1/traces`)

---

## 2. 模块一：OpenTelemetry 智能体可观测性实验

### 2.1 核心方案与代码对比

| 对比维度 | 方案 A：自动插桩 (Auto-instrumentation) | 方案 B：手动打点 (Manual-instrumentation) |
| :--- | :--- | :--- |
| **入口脚本** | [`agent_auto.py`](./agent_auto.py) | [`agent_manual.py`](./agent_manual.py) |
| **核心机制** | `LangChainInstrumentor().instrument()` 动态劫持 (Monkey Patching) | 原生 `tracer.start_as_current_span(...)` 显式创建 Span |
| **业务代码侵入度** | **零侵入（0%）**：业务代码与常规 LangGraph 毫无区别 | **高侵入（约 40% 样板代码）**：每个 Node、工具与返回需手动维护 Span 上下文 |
| **链路树粒度** | **极度细致**：自动将 `RunnableSequence`、`ChatOpenAI`、`ToolCall` 全层级解构展示 | **高度定制、聚焦核心**：仅展示开发者关心的业务节点与关键工具，层级精简干净 |
| **Token 与元数据** | 自动提取标准规范的 `llm.token_count.*` | 手动显式从 `response.response_metadata` 提取并使用 `span.set_attribute` 写入 |
| **灵活性与扩展** | 依赖第三方 Instrumentor 语义规范，自定义属性较繁琐 | **完全自主**：可随意附加企业私有业务标签（如租户 ID、业务单号、审批链路等） |

---

### 2.2 快速运行步骤

> **提示 (Windows 用户)**：若终端出现中文输出乱码，可先在终端执行 `$env:PYTHONUTF8=1`。

#### 步骤 1：启动 Phoenix 观测平台
在终端中启动 Phoenix 服务端后台：
```bash
python run_phoenix.py
```
启动后在浏览器打开：👉 **`http://localhost:6006`**

> **数据持久化**：`run_phoenix.py` 通过正式 serve 入口启动 Phoenix，并从 `.env` 读取 `PHOENIX_PORT` / `PHOENIX_WORKING_DIR` / `PHOENIX_SQL_DATABASE_URL`，将 Trace 数据持久化到项目内的 `phoenix_data/core.db`（SQLite）。重启 Phoenix 服务后历史数据不丢失，可直接在控制台回看；该目录已被 `.gitignore` 忽略，不会提交到仓库。

#### 步骤 2：运行方案 A（自动插桩）
```bash
python agent_auto.py
```

#### 步骤 3：运行方案 B（手动打点）
```bash
python agent_manual.py
```

---

### 2.3 Phoenix 前端可视化效果
运行完成后，刷新浏览器 `http://localhost:6006`，在 Phoenix 控制台可以看到：
1. **Traces 列表页**：展示两次不同运行的全局耗时（Latency）、状态（Status: OK）、以及涉及的 Spans 数量。
2. **Trace 瀑布流（Waterfall）与树状图**：
   * 自动插桩展示出从顶级 Graph 到 LangChain 内部执行链的完整内部调用细节；
   * 手动插桩清晰呈现 `agent_workflow_execution` -> `agent_reasoning` -> `tools_batch_execution` -> `tool_call:calculate` / `tool_call:search_code_snippets` 的业务调用树。
3. **Span 详情抽屉**：
   * 完整的用户 Prompt 输入；
   * 模型思考与 Markdown 回复文本；
   * 工具的 JSON 入参与返回值；
   * 细粒度的 Prompt Tokens / Completion Tokens 与执行耗时分布。

---

## 3. 模块二：状态持久化、断点恢复与人工在环 (HITL) 实验

除了链路可观测性外，本项目还深入实践了生产级智能体的**状态持久化（Checkpointer）**、**故障崩溃冷启动恢复（Crash Recovery）**与**高危操作人机协同拦截（Human-in-the-Loop, HITL）**。

### 3.1 核心定位与技术对比

| 脚本文件 | 核心定位 | 运行与交互模式 | 核心技术点 |
| :--- | :--- | :--- | :--- |
| [`agent_checkpoint.py`](./agent_checkpoint.py) | **人机在环 (HITL) 交互审批** | **一键单次交互**：执行至高危转账时暂停，在控制台交互式选择通过/拒绝后立即接续 | `SqliteSaver`、`interrupt()`、`Command(resume=...)` |
| [`agent_checkpoint_step.py`](./agent_checkpoint_step.py) | **人机在环 (HITL) 分步冷重启** | **分步 CLI 模式**：第 1 次运行触发审批后退出；第 2 次全新进程启动注入审批意见续跑 | `argparse`、跨进程状态反序列化、异步审批模拟 |
| [`test_crash_recovery.py`](./test_crash_recovery.py) | **生产故障崩溃与冷重启断点续跑** | **崩溃对比验证**：Tool 1 执行后模拟断电/崩溃退出，新进程无需 Prompt 自动接续 Tool 2 | 幂等性审计、`app.invoke(None, config=config)` |

---

### 3.2 详细运行指南

#### 方案 1：一键交互审批实验 (`agent_checkpoint.py`)
适合快速演示与调试人工在环（HITL）拦截逻辑：
```bash
python agent_checkpoint.py
```
* **流程说明**：
  1. 智能体自动计算张三的年终奖金（普通只读计算工具）；
  2. 触发高危操作（向指定账户转账），被安全门禁 `human_approval_guard` 自动拦截，状态落盘并触发 `interrupt()`；
  3. 控制台弹出审批交互窗口：
     * 输入 `1`（批准）：智能体接续执行真实转账工具并反馈打款凭证；
     * 输入 `2`（拒绝）：智能体拦截转账，接收驳回理由并向用户做出合理解释。

#### 方案 2：分步跨进程审批实验 (`agent_checkpoint_step.py`)
真实模拟生产业务中“用户发起申请 -> 后台挂起 -> 几天后管理员在独立系统审批并恢复”的异步工作流：

* **第 1 次运行：启动任务并触发中断后退出**
  ```bash
  python agent_checkpoint_step.py start --thread-id payroll_task_001
  ```
  *(任务执行完奖金计算，在转账前安全拦截落盘，并安全退出 Python 进程)*

* **第 2 次运行：独立进程启动，注入审批意见继续推进**
  * **同意批准**：
    ```bash
    python agent_checkpoint_step.py resume --thread-id payroll_task_001 --decision approve --reviewer Alice
    ```
  * **拒绝驳回**：
    ```bash
    python agent_checkpoint_step.py resume --thread-id payroll_task_001 --decision reject --reviewer Bob --reason "张三尚未签署目标确认书"
    ```

* **历史快照回溯（Time-Travel 审计）**：
  ```bash
  python agent_checkpoint_step.py inspect --thread-id payroll_task_001
  ```

#### 方案 3：真实故障崩溃与零输入续跑实验 (`test_crash_recovery.py`)
用于验证非 `interrupt` 的系统级故障（掉电、OOM、Pod 强制漂移）下的冷启动续跑与幂等性保证：

* **阶段 1：运行至 Tool 1 完毕后模拟进程强行崩溃**
  ```bash
  python test_crash_recovery.py phase1
  ```
  *(执行用户档案查询工具 `step1_query_user_profile` 后直接模拟严重故障崩溃退出)*

* **阶段 2：启动全新 Python 进程冷启动续跑**
  ```bash
  python test_crash_recovery.py phase2
  ```
  *(无需输入任何新 Prompt，传入 `None` 即自动从 SQLite 恢复断点。内置审计器会自动校验：Tool 1 调用次数恒为 1，Tool 2 顺利执行，任务完美闭环)*

