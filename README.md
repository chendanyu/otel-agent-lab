# OpenTelemetry 智能体可观测性对照实验报告 (otel-agent-lab)

本项目在 `J:\workroom\otel-agent-lab` 下完整实现了基于 **LangGraph** 的双方案 OpenTelemetry 可观测性对照实验，统一上报至本地 **Arize Phoenix[ˈfiːnɪks]** 监控与可视化平台。

---

## 1. 运行环境配置
* **Conda 虚拟环境**：`pycharm`
* **Python 解释器路径**：`D:\win7app\anaconda3\envs\pycharm\python.exe` (Python 3.11.5)
* **可观测性服务端**：Arize Phoenix (端口 `6006`，前端地址 `http://localhost:6006`)
* **遥测协议**：OpenTelemetry OTLP/HTTP 批处理传输 (`http://localhost:6006/v1/traces`)

---

## 2. 核心实验方案与代码对比

| 对比维度 | 方案 A：自动插桩 (Auto-instrumentation) | 方案 B：手动打点 (Manual-instrumentation) |
| :--- | :--- | :--- |
| **入口脚本** | [`agent_auto.py`](file:///J:/workroom/otel-agent-lab/agent_auto.py) | [`agent_manual.py`](file:///J:/workroom/otel-agent-lab/agent_manual.py) |
| **核心机制** | `LangChainInstrumentor().instrument()` 动态劫持 (Monkey Patching) | 原生 `tracer.start_as_current_span(...)` 显式创建 Span |
| **业务代码侵入度** | **零侵入（0%）**：业务代码与常规 LangGraph 毫无区别 | **高侵入（约 40% 样板代码）**：每个 Node、工具与返回需手动维护 Span 上下文 |
| **链路树粒度** | **极度细致**：自动将 `RunnableSequence`、`ChatOpenAI`、`ToolCall` 全层级解构展示 | **高度定制、聚焦核心**：仅展示开发者关心的业务节点与关键工具，层级精简干净 |
| **Token 与元数据** | 自动提取标准规范的 `llm.token_count.*` | 手动显式从 `response.response_metadata` 提取并使用 `span.set_attribute` 写入 |
| **灵活性与扩展** | 依赖第三方 Instrumentor 语义规范，自定义属性较繁琐 | **完全自主**：可随意附加企业私有业务标签（如租户 ID、业务单号、审批链路等） |

---

## 3. 快速复现与运行步骤

### 步骤 1：启动 Phoenix 观测平台
在终端中启动 Phoenix 服务端守护进程：
```powershell
& "D:\win7app\anaconda3\envs\pycharm\python.exe" "J:\workroom\otel-agent-lab\run_phoenix.py"
```
启动后在浏览器打开：👉 **`http://localhost:6006`**

### 步骤 2：运行方案 A（自动插桩）
```powershell
$env:PYTHONUTF8=1; & "D:\win7app\anaconda3\envs\pycharm\python.exe" "J:\workroom\otel-agent-lab\agent_auto.py"
```

### 步骤 3：运行方案 B（手动打点）
```powershell
$env:PYTHONUTF8=1; & "D:\win7app\anaconda3\envs\pycharm\python.exe" "J:\workroom\otel-agent-lab\agent_manual.py"
```

---

## 4. Phoenix 前端可视化效果
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
