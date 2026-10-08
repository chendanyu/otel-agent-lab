# -*- coding: utf-8 -*-
"""
方案 B: OpenTelemetry 手动打点智能体 (Manual-instrumentation)
特点:
1. 不使用任何自动化插桩库 (不使用 openinference 或 LangChainInstrumentor)
2. 纯粹使用原生 opentelemetry-api 和 opentelemetry-sdk 进行精细化埋点
3. 显式创建 Root Span (智能体总体运行)、Node Span (思考/决策)、Tool Span (工具执行)
4. 显式从 LLM 响应元数据中提取 Token 消耗，显式记录输入 Prompt 与工具出入参
5. 统一上报至本地 Phoenix (http://localhost:6006/v1/traces)
"""
import sys
import os
import json
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv()

# ==========================================
# 1. 配置原生 OpenTelemetry SDK
# ==========================================
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

PHOENIX_ENDPOINT = os.getenv("PHOENIX_ENDPOINT", "http://localhost:6006/v1/traces")

resource = Resource.create({"service.name": "manual-langgraph-agent", "environment": "lab"})
tracer_provider = TracerProvider(resource=resource)
span_processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=PHOENIX_ENDPOINT))
tracer_provider.add_span_processor(span_processor)
trace.set_tracer_provider(tracer_provider)

# 获取原生 Tracer 对象
tracer = trace.get_tracer("manual-instrumentation-tracer", "1.0.0")
print(f"[Manual-OTel] 原生 Tracer 初始化完成，数据上报端点: {PHOENIX_ENDPOINT}")

# ==========================================
# 2. 定义工具与大模型
# ==========================================
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from typing import Annotated, TypedDict, Sequence
from langchain_core.messages import BaseMessage
import operator

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add]

@tool
def calculate(expression: str) -> str:
    """计算数学表达式的结果。入参为算术表达式字符串，如 '(325 * 48) - 1200'。"""
    try:
        allowed_chars = set("0123456789+-*/() .")
        if not all(c in allowed_chars for c in expression):
            return "错误: 表达式包含不支持的字符"
        res = eval(expression, {"__builtins__": None}, {})
        return str(res)
    except Exception as e:
        return f"计算错误: {e}"

@tool
def search_code_snippets(keyword: str) -> str:
    """在代码库中检索相关代码片段。入参为关键字，如 'async_http_client'。"""
    if "async_http_client" in keyword.lower():
        return (
            "class AsyncHttpClient:\n"
            "    def __init__(self, timeout: float = 30.0):\n"
            "        self.timeout = timeout\n"
            "    async def get(self, url: str) -> dict:\n"
            "        # Exponential backoff retry logic implemented\n"
            "        pass"
        )
    return f"未找到与 '{keyword}' 匹配的代码片段。"

tools = [calculate, search_code_snippets]
tool_map = {t.name: t for t in tools}

model_name = os.getenv("OPENAI_MODEL_NAME", "deepseek-chat")
api_key = os.getenv("OPENAI_API_KEY", "dummy-key")
base_url = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")

llm = ChatOpenAI(
    model=model_name,
    openai_api_key=api_key,
    openai_api_base=base_url,
    temperature=0.2,
).bind_tools(tools)

# ==========================================
# 3. 构建 LangGraph 工作流 (在节点内部纯手动打点，使用 OpenInference 标准语义)
# ==========================================
def agent_node(state: AgentState):
    """大模型思考/推理节点 —— 手动埋点 Span: agent_reasoning (LLM 语义)"""
    messages = state["messages"]
    
    with tracer.start_as_current_span("agent_reasoning") as span:
        # 1. 显式记录标准语义属性
        span.set_attribute("openinference.span.kind", "LLM")
        span.set_attribute("llm.model_name", model_name)
        span.set_attribute("llm.provider", "openai")
        span.set_attribute("input.value", str(messages[-1].content))
        span.set_attribute("input.mime_type", "text/plain")
        
        start_t = time.time()
        try:
            # 2. 调用 LLM
            response = llm.invoke(messages)
            
            # 3. 显式记录输出与工具调用声明
            span.set_attribute("output.mime_type", "text/plain")
            if hasattr(response, "tool_calls") and response.tool_calls:
                tool_names = [tc["name"] for tc in response.tool_calls]
                span.set_attribute("output.value", f"调用工具: {', '.join(tool_names)}")
                # 如果是单个工具调用，按照 OpenInference 的 llm.function_call 规范格式化
                first_tc = response.tool_calls[0]
                function_call_obj = {
                    "name": first_tc["name"],
                    "arguments": first_tc["args"]
                }
                span.set_attribute("llm.function_call", json.dumps(function_call_obj, ensure_ascii=False))
            else:
                span.set_attribute("output.value", str(response.content))
                
            # 4. 显式记录标准 Token 统计属性 (Phoenix 核心识别键)
            if hasattr(response, "response_metadata") and response.response_metadata:
                usage = response.response_metadata.get("token_usage", {})
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
                
                span.set_attribute("llm.token_count.prompt", prompt_tokens)
                span.set_attribute("llm.token_count.completion", completion_tokens)
                span.set_attribute("llm.token_count.total", total_tokens)
            
            span.set_status(Status(StatusCode.OK))
            return {"messages": [response]}
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            raise

def tools_node(state: AgentState):
    """工具执行节点 —— 手动埋点 Span: tools_batch_execution 以及各个 tool_execution"""
    last_message = state["messages"][-1]
    results = []
    
    with tracer.start_as_current_span("tools_batch_execution") as batch_span:
        tool_calls = getattr(last_message, "tool_calls", [])
        batch_span.set_attribute("openinference.span.kind", "CHAIN")
        batch_span.set_attribute("input.value", json.dumps([tc["args"] for tc in tool_calls], ensure_ascii=False))
        batch_span.set_attribute("input.mime_type", "application/json")
        
        for tool_call in tool_calls:
            name = tool_call["name"]
            args = tool_call["args"]
            tool_id = tool_call["id"]
            
            # 为每个独立工具调用启动子 Span (TOOL 语义)
            with tracer.start_as_current_span(f"tool_call:{name}") as tool_span:
                args_json = json.dumps(args, ensure_ascii=False)
                
                tool_span.set_attribute("openinference.span.kind", "TOOL")
                tool_span.set_attribute("tool.name", name)
                tool_span.set_attribute("tool.parameters", args_json)
                tool_span.set_attribute("input.value", args_json)
                tool_span.set_attribute("input.mime_type", "application/json")
                
                if name in tool_map and hasattr(tool_map[name], "description"):
                    tool_span.set_attribute("tool.description", str(tool_map[name].description))
                
                try:
                    if name in tool_map:
                        tool_output = tool_map[name].invoke(args)
                    else:
                        tool_output = f"工具 {name} 不存在"
                    
                    tool_span.set_attribute("output.value", str(tool_output))
                    tool_span.set_attribute("output.mime_type", "text/plain")
                    tool_span.set_status(Status(StatusCode.OK))
                except Exception as te:
                    tool_span.record_exception(te)
                    tool_span.set_status(Status(StatusCode.ERROR, str(te)))
                    tool_output = f"工具执行异常: {te}"
                    
                results.append(ToolMessage(content=str(tool_output), tool_call_id=tool_id))
                
        batch_span.set_attribute("output.value", json.dumps([r.content for r in results], ensure_ascii=False))
        batch_span.set_attribute("output.mime_type", "application/json")
        batch_span.set_status(Status(StatusCode.OK))
    return {"messages": results}

def should_continue(state: AgentState):
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return END

workflow = StateGraph(AgentState)
workflow.add_node("agent", agent_node)
workflow.add_node("tools", tools_node)

workflow.add_edge(START, "agent")
workflow.add_conditional_edges("agent", should_continue, ["tools", END])
workflow.add_edge("tools", "agent")

app = workflow.compile()

# ==========================================
# 4. 执行智能体 (显式创建 Root Span)
# ==========================================
def run():
    print("\n" + "=" * 60)
    print("▶ 运行方案 B: 手动打点智能体 (Manual-instrumentation)")
    print("=" * 60)
    
    test_query = "请帮我计算 (325 * 48) - 1200 的结果，并检索有关 'async_http_client' 的代码示例，最后给出简短总结。"
    print(f"[用户输入]: {test_query}\n")
    
    messages = [
        SystemMessage(content="你是一个全能的代码与数学分析专家，能够熟练调用工具解决问题。"),
        HumanMessage(content=test_query),
    ]
    
    # 手动开启全局根 Span (Root Span)，标记为 AGENT 语义
    with tracer.start_as_current_span("agent_workflow_execution") as root_span:
        root_span.set_attribute("openinference.span.kind", "AGENT")
        root_span.set_attribute("agent.name", "ReAct-Manual-Agent")
        root_span.set_attribute("input.value", test_query)
        root_span.set_attribute("input.mime_type", "text/plain")
        
        final_state = app.invoke({"messages": messages})
        
        final_response_content = final_state["messages"][-1].content
        root_span.set_attribute("output.value", str(final_response_content))
        root_span.set_attribute("output.mime_type", "text/plain")
        root_span.set_status(Status(StatusCode.OK))
    
    print("\n[最终模型回复]:")
    print("-" * 50)
    print(final_state["messages"][-1].content)
    print("-" * 50)
    
    print("\n[Manual-OTel] 正在同步数据至 Phoenix...")
    tracer_provider.shutdown()
    print("[Manual-OTel] 同步完成！请在 http://localhost:6006 刷新查看完整 Trace 链路。\n")

if __name__ == "__main__":
    run()
