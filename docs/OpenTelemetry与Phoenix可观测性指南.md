# OpenTelemetry 与 Arize Phoenix 可观测性实战指南

---

## 1. 项目简介与开源地址

### 1.1 OpenTelemetry (OTel)
* **定位**：云原生计算基金会（CNCF）旗下的顶级开源项目，是当今云原生和分布式系统**可观测性（Observability）事实上的工业级标准**。
* **职责**：专注于**数据的生成（埋点）、处理和传输协议（OTLP）**。它不负责数据的最终持久化和页面展示，扮演“通用的标准化管道”。
* **官方网站**：[https://opentelemetry.io](https://opentelemetry.io)
* **GitHub 仓库**：
  * 规范标准：[https://github.com/open-telemetry/opentelemetry-specification](https://github.com/open-telemetry/opentelemetry-specification)
  * Python SDK：[https://github.com/open-telemetry/opentelemetry-python](https://github.com/open-telemetry/opentelemetry-python)

### 1.2 Arize Phoenix
* **定位**：专为 **AI 智能体（Agents）、大语言模型（LLM）与 RAG 系统** 定制打造的开源可视化评估与可观测性平台。
* **职责**：充当数据**接收端与可视化看板**。原生支持 OpenTelemetry OTLP 协议，开箱即用，自带 Web 前端页面，能自动还原智能体的思考链路瀑布流、Prompt/Token 消耗以及工具调用入参出参。
* **官方网站**：[https://phoenix.arize.com](https://phoenix.arize.com)
* **GitHub 仓库**：[https://github.com/Arize-ai/phoenix](https://github.com/Arize-ai/phoenix)

---

## 2. 必须掌握的核心概念

在微服务与大模型可观测性中，数据通常被组织为树状层级：

```
Trace (一次用户交互全生命周期)
  │
  ├── Span 1: Agent Root (智能体顶层执行)
  │     ├── Span 2: LLM Reasoning (大模型推理，记录 Prompt 与 Token)
  │     └── Span 3: Tool Execution (执行计算/检索工具)
  │           └── Event / Log (工具执行中的单点事件)
```

### 2.1 Trace（链路 / 追踪）
* **定义**：代表一个请求（Request）或一次任务从进入系统到彻底结束的**完整端到端生命周期**。
* **标识**：通过唯一的 `Trace ID` 贯穿整个调用链。
* **示例**：用户发送了一条消息：“帮我查询天气并计算出行花费”，从接收、模型思考、调工具，到最后生成回复的**全流程**，合起来就是一个 Trace。

### 2.2 Span（跨度）—— 最小执行单元
* **定义**：链路中的**单个具体操作步骤**，是一段有确定起止时间的工作单元。
* **核心组成**：
  * **Span Name**：操作名称（如 `llm_reasoning`、`tool_call:calculate`）。
  * **Span ID & Parent Span ID**：自身的 ID 和父节点的 ID（用来在前端拼装出树状调用关系）。
  * **Timestamps**：精确的起始时间和结束时间（相减即为耗时 Latency）。
  * **Status**：执行状态（`OK` 成功 或 `ERROR` 异常）。
  * **Attributes（属性）**：挂载在 Span 上的结构化键值对（见下文）。

#### 💡 进阶：一个 Span 什么时候算“打点完了”？什么时候“提交发送”？
这是很多开发者容易混淆的生命周期细节：

1. **什么时候算“打点完了”？（结束生命周期）**
   * **时机**：**当调用了 `span.end()` 的瞬间**。
   * **推荐写法**：在 Python 中通常使用 `with tracer.start_as_current_span(...) as span:`。**就在代码走出 `with` 缩进块的瞬间**，Python 会自动触发 `span.end()`。
   * **状态变化**：打卡记录 `end_time` 并计算耗时；Span 瞬间进入**只读封口状态**（不可再修改属性）。

2. **什么时候真正“提交 / 发送出去”？（网络传输）**
   * **并不是一调用 `span.end()` 就立刻发网络请求**（否则会严重拖慢程序运行）。
   * 封口后的 Span 会立刻进入 `BatchSpanProcessor` 维护的**内存缓冲队列（In-Memory Queue）**。
   * **触发最终提交到 Phoenix 的条件（满足其一即可）**：
     * **数量达到阈值**：缓冲区攒满一定数量（默认 512 个）；
     * **时间达到阈值**：后台守护线程定时发送（默认每隔 5 秒定时清空队列发送）；
     * **显式强制提交**：在脚本退出前调用 **`tracer_provider.shutdown()`** 或 **`tracer_provider.force_flush()`**（强行不等 5 秒，把当前队列里所有残留数据立即全部发出并关闭连接）。

#### 🌲 核心机制：Span 之间是如何建立“父子关系”的？
底层核心是：子 Span 内部有一个属性叫 **`parent_span_id`**，只要它的值等于父 Span 的 `span_id`，Phoenix/Jaeger 就能将其拼成树形瀑布图。具体实现有两种方式：

##### 方式 1：隐式自动继承（推荐，基于上下文变量 ContextVars）
在同步单线程/协程中，使用 `with tracer.start_as_current_span(...)` 语法进行**代码块嵌套**。内层 Span 会自动读取当前上下文的父 Span 并认它为父亲，无需传任何参数：
```python
# 👨 父 Span (Root)
with tracer.start_as_current_span("parent_agent"):
    # 👶 子 Span 自动挂在 parent_agent 下面
    with tracer.start_as_current_span("child_llm_call"):
        pass
```

##### 方式 2：显式手动传递（用于跨线程、异步任务、线程池或非嵌套解耦场景）
在多线程（如 `ThreadPoolExecutor`）、异步后台任务或无法使用 `with` 嵌套的场景下，Python 的隐式上下文无法自动穿透。此时需要**显式提取父上下文并注入给子 Span**：
```python
from opentelemetry import trace
from opentelemetry.trace import set_span_in_context

# 1. 创建父 Span (不作为全局 current，或者拿到显式对象)
parent_span = tracer.start_span("manual_parent")

# 2. 将 parent_span 包装成一个标准的 Context 对象
parent_context = set_span_in_context(parent_span)

# 3. 创建子 Span 时，显式传入 context=parent_context
#    子 Span 内部将自动填充 parent_span_id = parent_span.get_span_context().span_id
child_span = tracer.start_span("manual_child", context=parent_context)

# 4. 执行业务逻辑...

# 5. 分别手动结束生命周期 (注意：遵循先关子、后关父的原则)
child_span.end()
parent_span.end()
```
*跨网络分布式场景*：如果是跨进程/跨微服务调用，则通过 HTTP 请求头传递标准 W3C `traceparent`（包含 TraceID 与 SpanID），服务端接收后从中还原 `context` 启动子 Span。

### 2.3 Attributes（属性 / 标签）
* **定义**：描述当前 Span 执行细节的元数据（键值对）。
* **在 AI 智能体中的标准语义（OpenInference 规范）**：
  * `input.value`：输入文本（用户提问、上文消息）。
  * `output.value`：输出文本（模型回复内容、工具执行结果）。
  * `llm.model_name`：使用的模型名（如 `gpt-4o`、`deepseek-chat`）。
  * `llm.token_count.prompt`：输入消耗的 Token 数。
  * `llm.token_count.completion`：输出生成的 Token 数。
  * `llm.token_count.total`：总消耗 Token 数。
  * `tool.name` / `tool.parameters`：调用的工具名称及 JSON 入参。

### 2.4 OTLP（OpenTelemetry Protocol）
* **定义**：OpenTelemetry 的统一数据传输协议，基于 HTTP/Protobuf 或 gRPC。
* **默认端点**：
  * HTTP 收集端：`http://localhost:6006/v1/traces`
  * gRPC 收集端：`http://localhost:4317`

---

## 3. 架构协同关系

```
┌───────────────────────────────────────────────┐
│              Python 应用程序 / 智能体          │
│                                               │
│  [业务逻辑]                                   │
│      │                                        │
│  [OpenTelemetry SDK] (内存打点与打包)         │
└───────────────────────┬───────────────────────┘
                        │ OTLP/HTTP 批处理传输
                        ▼ (POST /v1/traces)
┌───────────────────────────────────────────────┐
│                 Arize Phoenix                 │
│  1. 接收器 (Receiver): 解析 OTLP 数据         │
│  2. 存储引擎: 存入 SQLite / DuckDB            │
│  3. Web UI: 浏览器访问 http://localhost:6006  │
└───────────────────────────────────────────────┘
```

---

## 4. 快速上手指南

### 4.1 安装依赖
```bash
pip install arize-phoenix opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
```
*如果你使用 LangChain / LangGraph，再加一个自动插桩库*：
```bash
pip install openinference-instrumentation-langchain
```

---

### 4.2 第一步：启动 Phoenix 可观测性服务端
新建脚本 `run_phoenix.py`：

```python
import time
import phoenix as px

# 启动 Phoenix 服务端，监听 6006 端口
session = px.launch_app(port=6006)

print(f"✅ Phoenix 前端页面已启动: {session.url}")
print("📡 OTLP 收集端点: http://localhost:6006/v1/traces")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("服务已停止")
```
运行该脚本后，在浏览器访问 **`http://localhost:6006`** 即可打开可视化仪表盘。

---

### 4.3 第二步（方式一）：零侵入自动插桩（适用于 LangChain/LangGraph）
**特点**：业务代码一行不用动，生态插件自动通过动态拦截（Monkey Patching）捕获状态流、Prompt、Token 和工具。

```python
import os
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from openinference.instrumentation.langchain import LangChainInstrumentor

# 1. 配置 OTel 导出器指向本地 Phoenix
tracer_provider = TracerProvider()
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:6006/v1/traces"))
)

# 2. 开启自动插桩
LangChainInstrumentor().instrument(tracer_provider=tracer_provider)

# 3. 正常写你的 LangGraph / LangChain 逻辑
from langchain_openai import ChatOpenAI
llm = ChatOpenAI(model="gpt-4o-mini")
response = llm.invoke("用一句话介绍 OpenTelemetry。")
print(response.content)

# 4. 脚本结束前刷写数据
tracer_provider.shutdown()
```

---

### 4.4 第二步（方式二）：原生手动精细打点（通用任意 Python 程序）
**特点**：不依赖特定框架，完全使用 OpenTelemetry 原生 API 手动创建父子 Span 和打标。

```python
import time
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

# 1. 初始化 TracerProvider
tracer_provider = TracerProvider()
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:6006/v1/traces"))
)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("my-agent-tracer")

# 2. 手动创建根 Span (代表智能体整轮运行)
with tracer.start_as_current_span("agent_workflow") as root_span:
    root_span.set_attribute("openinference.span.kind", "AGENT")
    root_span.set_attribute("input.value", "计算 (325 * 48) - 1200")

    # 3. 子步骤：大模型推理 Span
    with tracer.start_as_current_span("llm_reasoning") as llm_span:
        llm_span.set_attribute("openinference.span.kind", "LLM")
        llm_span.set_attribute("llm.model_name", "deepseek-chat")
        
        # 模拟模型输出与 Token 统计
        llm_span.set_attribute("output.value", "调用计算工具")
        llm_span.set_attribute("llm.token_count.prompt", 45)
        llm_span.set_attribute("llm.token_count.completion", 15)
        llm_span.set_attribute("llm.token_count.total", 60)
        llm_span.set_status(Status(StatusCode.OK))

    # 4. 子步骤：工具执行 Span
    with tracer.start_as_current_span("tool_call:calculate") as tool_span:
        tool_span.set_attribute("openinference.span.kind", "TOOL")
        tool_span.set_attribute("tool.name", "calculate")
        tool_span.set_attribute("input.value", "(325 * 48) - 1200")
        
        # 实际执行计算
        res = str((325 * 48) - 1200)
        
        tool_span.set_attribute("output.value", res)
        tool_span.set_status(Status(StatusCode.OK))

    root_span.set_attribute("output.value", f"最终计算结果为: {res}")
    root_span.set_status(Status(StatusCode.OK))

# 5. 确保队列中残留的数据发送完毕
tracer_provider.shutdown()
```

---

## 5. 自动插桩 vs 手动打点 对比总结

| 对比维度 | 自动插桩 (Auto-Instrumentation) | 手动打点 (Manual-Instrumentation) |
| :--- | :--- | :--- |
| **开发成本** | 极低（仅需 2~3 行初始化配置） | 较高（需手写 `with tracer.start_as_current_span`） |
| **代码侵入性** | **零侵入**，不污染原有业务逻辑 | **高侵入**，业务代码混合大量埋点样板代码 |
| **展示细节** | 极细（自动包含框架内部所有组件调用） | 仅包含开发者明确编写的节点 |
| **灵活度** | 受限于第三方插件规范，添加私有属性较繁琐 | **完全自主**，可自由注入私有业务 ID、权限上下文等 |
| **适用场景** | 快速验证、标准 LangChain/LlamaIndex 智能体 | 复杂非标准架构、私有 Agent 引擎、对链路层级有严苛定制需求的生产系统 |
