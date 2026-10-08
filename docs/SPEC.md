# SPEC.md: OpenTelemetry 智能体可观测性对照实验规范

## 1. 项目背景与目标
本项目旨在在 `J:\workroom\otel-agent-lab` 下构建一套完整的 **智能体可观测性对照实验（Observability Lab）**。
通过构建功能完全对等（相同的状态流转、大模型交互和工具调用）的 LangGraph 智能体，以两种截然不同的方式接入 **OpenTelemetry (OTel)** 并上报至 **Arize Phoenix**，以便直观对比两者的：
1. **实现成本与代码侵入度**；
2. **Span 链路层级结构（Trace Tree）**；
3. **元数据丰富度（Prompt、Token 消耗、工具入参及出参、耗时等）**。

---

## 2. 运行环境规范（基于 Conda 的 pycharm 环境）

本项目严格绑定本地已就绪的 Conda `pycharm` 环境进行开发与运行，**禁止使用系统的默认 Python 环境（如 `C:\Python314`）**。

* **Conda 环境名称**：`pycharm`
* **Python 解释器绝对路径**：`D:\win7app\anaconda3\envs\pycharm\python.exe`
* **Python 版本**：Python 3.11.5
* **环境状态**：该环境中已经具备 `langgraph` (1.2.11)、`langchain` (0.2.17) 以及基础的 `opentelemetry` 套件。
* **执行命令规范**：
  * **启动 Phoenix**：
    ```powershell
    & "D:\win7app\anaconda3\envs\pycharm\python.exe" run_phoenix.py
    ```
  * **运行自动插桩智能体**：
    ```powershell
    & "D:\win7app\anaconda3\envs\pycharm\python.exe" agent_auto.py
    ```
  * **运行手动插桩智能体**：
    ```powershell
    & "D:\win7app\anaconda3\envs\pycharm\python.exe" agent_manual.py
    ```
  * **补充安装依赖（仅限该环境）**：
    ```powershell
    & "D:\win7app\anaconda3\envs\pycharm\python.exe" -m pip install -r requirements.txt
    ```

---

## 3. 核心架构设计

接收端统一使用 **Arize Phoenix**（基于标准 OTLP/HTTP 协议，默认监听端口 `6006`，收集端 `http://localhost:6006/v1/traces`）。

```
                               ┌─────────────────────────────────┐
                               │     Arize Phoenix Server        │
                               │  (localhost:6006/v1/traces)     │
                               └────────────────┬────────────────┘
                                                ▲
                                                │ OTLP/HTTP Trace Batches
                    ┌───────────────────────────┴───────────────────────────┐
                    │                                                       │
        ┌───────────┴─────────────┐                           ┌─────────────┴───────────┐
        │ 方案 A: 自动插桩智能体    │                           │ 方案 B: 手动插桩智能体    │
        │ (agent_auto.py)         │                           │ (agent_manual.py)       │
        ├─────────────────────────┤                           ├─────────────────────────┤
        │ • 零侵入代码             │                           │ • 原生 Tracer SDK       │
        │ • LangChainInstrumentor │                           │ • 显式 start_as_current_ │
        │ • 自动捕获 Node/LLM/Tool│                           │   span 管理父子上下文     │
        │ • 自动解析 Token 与状态  │                           │ • 显式 set_attribute 记录 │
        └─────────────────────────┘                           └─────────────────────────┘
```

---

## 4. 业务场景设计（统一测试用例）
为了保证对比的公平性与代表性，两个智能体实现**完全相同的业务逻辑**：
* **业务定位**：代码与数学分析助手（ReAct 架构）。
* **内置工具（Tools）**：
  1. `calculate(expression: str) -> str`：执行基本数学运算。
  2. `search_code_snippets(keyword: str) -> str`：模拟检索本地/知识库代码。
* **典型用户查询**：
  > "请帮我计算 (325 * 48) - 1200 的结果，并检索有关 'async_http_client' 的代码示例，最后给出简短总结。"
* **预期执行路径**：
  `START` ➔ `agent_node (思考)` ➔ `tools_node (执行两个工具)` ➔ `agent_node (综合回答)` ➔ `END`

---

## 5. 方案详细设计与规范

### 方案 A：自动插桩（Auto-instrumentation）模式
* **目标文件**：`agent_auto.py`
* **技术选型**：`openinference-instrumentor-langchain` + `opentelemetry-sdk`
* **工作原理**：
  1. 在运行时利用 Python 动态挂载机制（Monkey Patching），为 LangChain / LangGraph 底层核心调用（如 `Runnable.invoke`、`ChatOpenAI.invoke`、`BaseTool.invoke`）自动注入 OpenTelemetry 上下文传播与 Span 生命周期管理。
  2. 自动捕获 `input.value`、`output.value`、`llm.token_count.prompt`、`llm.token_count.completion` 等 OpenInference 标准属性。
* **核心代码规范**：
  ```python
  from openinference.instrumentor.langchain import LangChainInstrumentor
  from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
  from opentelemetry.sdk.trace import TracerProvider
  from opentelemetry.sdk.trace.export import BatchSpanProcessor

  # 1. 初始化标准 TracerProvider 并配置 OTLP HTTP 导出至 Phoenix
  provider = TracerProvider()
  provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:6006/v1/traces")))

  # 2. 启用自动插桩（此后业务代码完全无侵入）
  LangChainInstrumentor().instrument(tracer_provider=provider)
  ```

---

### 方案 B：手动插桩（Manual-instrumentation）模式
* **目标文件**：`agent_manual.py`
* **技术选型**：原生 `opentelemetry-api` + `opentelemetry-sdk`
* **工作原理**：
  1. **不使用**任何自动插桩库（保留最纯粹的 LangGraph + 原生 OTel SDK）。
  2. 在 `Graph.invoke` 外部创建 Root Span（`agent_run`）。
  3. 在各个 Node（`agent_node`、`tools_node`）中通过 `tracer.start_as_current_span(...)` 手动建立子 Span，维护父子上下文层级关系。
  4. 手动从 LLM 的返回结果（`response.response_metadata['token_usage']`）提取 `prompt_tokens`、`completion_tokens`，并用 `span.set_attribute(...)` 注入。
  5. 手动在工具调用前后记录入参、返回值和异常捕获（`span.record_exception(e)`）。
* **核心代码规范**：
  ```python
  from opentelemetry import trace
  from opentelemetry.trace import Status, StatusCode

  tracer = trace.get_tracer("manual-langgraph-agent")

  def agent_node(state):
      with tracer.start_as_current_span("agent_reasoning") as span:
          span.set_attribute("llm.model_name", "gpt-4o-mini")
          span.set_attribute("agent.state.messages_count", len(state["messages"]))
          response = llm.invoke(state["messages"])
          # 手动提取并记录 Token 消耗
          if "token_usage" in response.response_metadata:
              usage = response.response_metadata["token_usage"]
              span.set_attribute("llm.token_count.prompt", usage.get("prompt_tokens", 0))
              span.set_attribute("llm.token_count.completion", usage.get("completion_tokens", 0))
          return {"messages": [response]}
  ```

---

## 6. 项目目录结构

```
J:\workroom\otel-agent-lab/
├── SPEC.md                  # 本设计规范文档（含 Conda 环境说明）
├── requirements.txt         # 额外需要补充的依赖（如 arize-phoenix, openinference-*）
├── run_phoenix.py           # 一键启动 Phoenix 本地服务脚本
├── agent_auto.py            # 方案 A: 自动插桩实现
├── agent_manual.py          # 方案 B: 原生手动插桩实现
└── README.md                # 运行指引与对照结果评测说明
```

---

## 7. 增量依赖列表（requirements.txt）
由于 `pycharm` 环境已安装大部分基础库，仅需补齐以下专门库：
```text
arize-phoenix>=4.0.0
openinference-instrumentor-langchain>=0.1.20
```

---

## 8. 验证与验收标准
1. **环境检查验证**：
   * 确认执行解释器为 `D:\win7app\anaconda3\envs\pycharm\python.exe`（Python 3.11.5）。
2. **服务启动验证**：
   * 运行 `& "D:\win7app\anaconda3\envs\pycharm\python.exe" run_phoenix.py`，浏览器访问 `http://localhost:6006`，Phoenix UI 正常就绪。
3. **方案 A 验证**：
   * 运行 `& "D:\win7app\anaconda3\envs\pycharm\python.exe" agent_auto.py`，程序顺利执行。
   * Phoenix 前端显示完整的 Graph / Node / Tool / LLM 树形 Trace。
4. **方案 B 验证**：
   * 运行 `& "D:\win7app\anaconda3\envs\pycharm\python.exe" agent_manual.py`，程序顺利执行。
   * Phoenix 前端显示手动建立的 Span 树与自定义 Attributes。
