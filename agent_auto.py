# -*- coding: utf-8 -*-
"""
方案 A: OpenTelemetry 自动插桩智能体 (Auto-instrumentation)
特点:
1. 业务逻辑完全零侵入 (基于 LangChainInstrumentor)
2. 自动捕获 LangGraph Node 流转、ChatOpenAI 调用、Prompt、Token 消耗及工具出入参
3. 统一上报至本地 Phoenix (http://localhost:6006/v1/traces)
"""
import sys
import os

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv()

# ==========================================
# 1. 配置 OpenTelemetry 及自动插桩插件
# ==========================================
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from openinference.instrumentation.langchain import LangChainInstrumentor

PHOENIX_ENDPOINT = os.getenv("PHOENIX_ENDPOINT", "http://localhost:6006/v1/traces")

tracer_provider = TracerProvider()
span_processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=PHOENIX_ENDPOINT))
tracer_provider.add_span_processor(span_processor)

# 启用自动插桩 (在此之后初始化的 LangChain / LangGraph 调用都会被自动追踪)
LangChainInstrumentor().instrument(tracer_provider=tracer_provider)
print(f"[Auto-OTel] 自动插桩已激活，追踪数据上报端点: {PHOENIX_ENDPOINT}")

# ==========================================
# 2. 定义业务逻辑与工具 (标准业务代码，零侵入)
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
        # 安全计算简单算式
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

# 初始化 LLM (使用 .env 配置的模型)
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
# 3. 构建 LangGraph 工作流
# ==========================================
def agent_node(state: AgentState):
    """大模型思考/推理节点"""
    messages = state["messages"]
    response = llm.invoke(messages)
    return {"messages": [response]}

def tools_node(state: AgentState):
    """工具执行节点"""
    last_message = state["messages"][-1]
    results = []
    if hasattr(last_message, "tool_calls"):
        for tool_call in last_message.tool_calls:
            name = tool_call["name"]
            args = tool_call["args"]
            tool_id = tool_call["id"]
            if name in tool_map:
                tool_output = tool_map[name].invoke(args)
            else:
                tool_output = f"工具 {name} 不存在"
            results.append(ToolMessage(content=str(tool_output), tool_call_id=tool_id))
    return {"messages": results}

def should_continue(state: AgentState):
    """条件边：判断模型是否请求调用工具"""
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
# 4. 执行智能体并上报遥测
# ==========================================
def run():
    print("\n" + "=" * 60)
    print("▶ 运行方案 A: 自动插桩智能体 (Auto-instrumentation)")
    print("=" * 60)
    
    test_query = "请帮我计算 (325 * 48) - 1200 的结果，并检索有关 'async_http_client' 的代码示例，最后给出简短总结。"
    print(f"[用户输入]: {test_query}\n")
    
    messages = [
        SystemMessage(content="你是一个全能的代码与数学分析专家，能够熟练调用工具解决问题。"),
        HumanMessage(content=test_query),
    ]
    
    # 执行图
    final_state = app.invoke({"messages": messages})
    
    print("\n[最终模型回复]:")
    print("-" * 50)
    print(final_state["messages"][-1].content)
    print("-" * 50)
    
    # 强制将内存队列中的 Span 刷写到 Phoenix 服务端
    print("\n[Auto-OTel] 正在同步数据至 Phoenix...")
    tracer_provider.shutdown()
    print("[Auto-OTel] 同步完成！请在 http://localhost:6006 刷新查看完整 Trace 链路。\n")

if __name__ == "__main__":
    run()
