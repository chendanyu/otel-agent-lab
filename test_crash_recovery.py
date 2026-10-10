# -*- coding: utf-8 -*-
"""
场景二实测脚本: 验证非 interrupt、真实进程崩溃 (Crash) 与冷重启断点续跑 (Resume)
设计方案:
1. 任务包含两个按顺序执行的业务工具:
   - Tool 1: step1_query_user_profile (查询用户档案，带全局调用计数器)
   - Tool 2: step2_generate_formal_report (生成正式报告，带全局调用计数器)
2. 阶段 1 (Phase 1):
   - 执行任务，当第一步 (Tool 1) 执行完毕并落盘持久化后，直接模拟严重故障/进程被强制终止。
3. 阶段 2 (Phase 2):
   - 模拟全新的 Python 进程冷启动恢复。
   - 不传入任何新的 Prompt 提问，直接从断点继续执行。
4. 验证指标:
   - Tool 1 调用次数恒等于 1 (绝不重复执行已完成的步骤)。
   - Tool 2 顺利执行完成，业务流程闭环。
"""
import sys
import os
import json
import time
from typing import Annotated, Sequence, TypedDict
import operator

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv()

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

# ==========================================
# 1. 状态与调用审计器
# ==========================================
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add]

CALL_RECORD_FILE = os.path.join(os.path.dirname(__file__), "tool_call_records.json")

def record_tool_call(tool_name: str) -> int:
    records = {}
    if os.path.exists(CALL_RECORD_FILE):
        try:
            with open(CALL_RECORD_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)
        except Exception:
            records = {}
    records[tool_name] = records.get(tool_name, 0) + 1
    with open(CALL_RECORD_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    return records[tool_name]

@tool
def step1_query_user_profile(user_id: str) -> str:
    """第 1 步工具: 查询用户的基础档案和资产数据"""
    count = record_tool_call("step1_query_user_profile")
    print(f"\n🔥 [Tool 1 执行中] step1_query_user_profile: user_id={user_id} (这是该工具第 {count} 次被调用)")
    return json.dumps({
        "user_id": user_id,
        "name": "李四",
        "level": "VIP-Gold",
        "assets": 128000
    }, ensure_ascii=False)

@tool
def step2_generate_formal_report(user_id: str, summary_data: str) -> str:
    """第 2 步工具: 生成正式资产体检报告并存档"""
    count = record_tool_call("step2_generate_formal_report")
    print(f"\n🔥 [Tool 2 执行中] step2_generate_formal_report: user_id={user_id} (这是该工具第 {count} 次被调用)")
    return json.dumps({
        "status": "GENERATED",
        "report_id": f"REP_{int(time.time())}",
        "user_id": user_id,
        "detail": f"已成功为李四归档报告: {summary_data}"
    }, ensure_ascii=False)

tools = [step1_query_user_profile, step2_generate_formal_report]
tool_map = {t.name: t for t in tools}

# ==========================================
# 2. 初始化大模型与图 (无任何 interrupt)
# ==========================================
model_name = os.getenv("OPENAI_MODEL_NAME", "deepseek-chat")
api_key = os.getenv("OPENAI_API_KEY", "dummy-key")
base_url = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")

llm = ChatOpenAI(
    model=model_name,
    openai_api_key=api_key,
    openai_api_base=base_url,
    temperature=0.1,
).bind_tools(tools)

def agent_node(state: AgentState):
    """思考推理节点"""
    messages = state["messages"]
    print(f"\n[NODE] -> agent 正在思考决策 (历史消息条数: {len(messages)})...")
    res = llm.invoke(messages)
    return {"messages": [res]}

def tools_node(state: AgentState):
    """工具执行节点"""
    last_message = state["messages"][-1]
    tool_calls = getattr(last_message, "tool_calls", [])
    results = []
    
    for tc in tool_calls:
        name = tc["name"]
        args = tc["args"]
        tool_id = tc["id"]
        print(f"[NODE] -> tools 正在分发工具: {name}...")
        tool_res = tool_map[name].invoke(args)
        results.append(ToolMessage(content=str(tool_res), tool_call_id=tool_id))
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

DB_PATH = os.path.join(os.path.dirname(__file__), "crash_recovery_demo.db")

# ==========================================
# 3. 阶段 1: 运行到 Tool 1 完成后强制中断
# ==========================================
def run_phase_1(thread_id: str):
    """
    阶段 1: 首次发起任务。当第 1 个工具执行完成并落盘持久化后，模拟致命中断退出。
    """
    print("\n" + "=" * 60)
    print("🚀 [阶段 1: 首次启动任务]")
    print(f"Thread ID: {thread_id}")
    print("=" * 60)
    
    prompt = (
        "请为用户 'U88001' 办理资产归档业务。\n"
        "步骤要求:\n"
        "第一步: 调用 step1_query_user_profile 查询档案；\n"
        "第二步: 根据查询结果，调用 step2_generate_formal_report 生成正式报告。"
    )
    
    messages = [
        SystemMessage(content="你是一个严格按顺序执行业务的多步骤业务办理智能体。每次只调用当前步骤所需的工具。"),
        HumanMessage(content=prompt)
    ]
    
    config = {"configurable": {"thread_id": thread_id}}
    
    with SqliteSaver.from_conn_string(DB_PATH) as saver:
        saver.setup()
        app = workflow.compile(checkpointer=saver)
        
        # 使用流式执行，精准捕获每个超步完成点
        for event in app.stream({"messages": messages}, config=config):
            node_name = list(event.keys())[0]
            print(f"  └─ 节点 [{node_name}] 执行完毕并已写入磁盘 Checkpoint！")
            
            # 当第一个工具节点执行完毕并完成落盘时，模拟系统进程崩溃
            if node_name == "tools":
                print("\n💥💥💥 [模拟生产灾难] 第一步工具刚执行完毕并成功落盘！")
                print("💥💥💥 模拟服务器突然掉电 / OOM / Pod 强制重启！进程即将强行退出！")
                print("💥💥💥 (此时 Tool 1 已经执行，但整个业务任务尚未完成)\n")
                sys.exit(0)

# ==========================================
# 4. 阶段 2: 全新进程冷启动断点恢复
# ==========================================
def run_phase_2(thread_id: str):
    """
    阶段 2: 模拟全新 Python 进程启动，从断点直接恢复，无需任何输入 Prompt！
    """
    print("\n" + "=" * 60)
    print("♻️  [阶段 2: 模拟全新进程冷启动 - 从磁盘断点续跑]")
    print(f"Thread ID: {thread_id}")
    print("核心调用: app.invoke(None, config=config)  <- 传入 None 即可自动续跑！")
    print("=" * 60)
    
    config = {"configurable": {"thread_id": thread_id}}
    
    with SqliteSaver.from_conn_string(DB_PATH) as saver:
        saver.setup()
        app = workflow.compile(checkpointer=saver)
        
        # 1. 检查崩溃前留存在数据库中的最新断点状态
        current_state = app.get_state(config)
        print("📦 从 SQLite 数据库读取到的最后有效断点:")
        print(f"   Checkpoint ID: {current_state.config['configurable'].get('checkpoint_id')}")
        print(f"   历史已保存消息条数: {len(current_state.values.get('messages', []))}")
        print(f"   下一步待执行节点: {current_state.next}")
        
        print("\n▶ 开始从断点恢复执行...")
        start_t = time.time()
        
        # 核心: 传入 None，自动从断点继续执行后续节点！
        final_state = app.invoke(None, config=config)
        
        print(f"\n✅ 任务恢复执行完成！耗时: {time.time() - start_t:.2f}s")
        print("-" * 50)
        print("[最终智能体回复]:")
        print(final_state["messages"][-1].content)
        print("-" * 50)
        
        # 2. 统计并审计工具调用次数
        with open(CALL_RECORD_FILE, "r", encoding="utf-8") as f:
            records = json.load(f)
        print("\n📊 [工具调用次数审计]:")
        print(f"  Tool 1 (step1_query_user_profile) 调用次数: {records.get('step1_query_user_profile', 0)}")
        print(f"  Tool 2 (step2_generate_formal_report) 调用次数: {records.get('step2_generate_formal_report', 0)}")
        
        if records.get('step1_query_user_profile') == 1 and records.get('step2_generate_formal_report') == 1:
            print("\n🎉🎉🎉 【场景二断点恢复验证成功！】")
            print("结论: 崩溃重启后，Tool 1 绝没有被重新调用（保证了幂等与成本），智能体无缝接着执行 Tool 2 直至任务完成！\n")
        else:
            print("\n⚠️ 异常: 工具调用次数不符合预期！")

if __name__ == "__main__":
    demo_thread = "crash_recovery_demo_001"
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    
    if mode == "phase1":
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        if os.path.exists(CALL_RECORD_FILE):
            os.remove(CALL_RECORD_FILE)
        run_phase_1(demo_thread)
    elif mode == "phase2":
        run_phase_2(demo_thread)
    else:
        # 一键顺序演练
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        if os.path.exists(CALL_RECORD_FILE):
            os.remove(CALL_RECORD_FILE)
            
        print(">>> 启动阶段 1 (将在 Tool 1 跑完后崩溃退出) <<<")
        run_phase_1(demo_thread)
