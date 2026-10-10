# -*- coding: utf-8 -*-
"""
生产级智能体示例: 支持断点恢复、持久化 Checkpoint 与人工在环 (HITL) 挂起恢复
核心特性:
1. 基于 SqliteSaver 实现状态快照磁盘持久化 (可无缝切换 PostgresSaver)。
2. 进程崩溃/意外中断后，支持基于 thread_id 实现无缝断点续跑 (无需重复调用已执行的工具)。
3. 支持高危操作中断 (Human-in-the-Loop Interrupt) 与人工审批后恢复 (Resume)。
4. 支持历史快照回溯 (Time-Travel) 查看状态演进。
"""
import sys
import os
import json
import sqlite3
from typing import Annotated, Sequence, TypedDict, Literal
import operator

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv()

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage, AIMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt, Command

# ==========================================
# 1. 定义状态 Schema
# ==========================================
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add]
    requires_approval: bool
    audit_notes: Annotated[Sequence[str], operator.add]

# ==========================================
# 2. 定义工具集合 (包含普通工具与高风险工具)
# ==========================================
@tool
def calculate_salary_bonus(base_salary: float, performance_score: float) -> str:
    """计算员工年终奖金。普通只读/计算类工具。"""
    print(f"\n[TOOL RUN] 正在执行计算奖金: base={base_salary}, score={performance_score}...")
    bonus = round(base_salary * (performance_score / 100.0) * 2.5, 2)
    return json.dumps({"base_salary": base_salary, "bonus": bonus, "total": base_salary + bonus}, ensure_ascii=False)

@tool
def execute_bank_transfer(account_no: str, amount: float, reason: str) -> str:
    """向指定银行账户转账打款。高危敏感工具，涉及真实资金流动！"""
    print(f"\n[TOOL RUN] >>> 执行真实银行转账: 账号 {account_no}, 金额: ¥{amount:.2f}, 事由: {reason} <<<")
    return json.dumps({
        "status": "SUCCESS",
        "tx_id": f"TX_{os.urandom(4).hex()}",
        "account_no": account_no,
        "amount": amount,
        "reason": reason
    }, ensure_ascii=False)

tools = [calculate_salary_bonus, execute_bank_transfer]
tool_map = {t.name: t for t in tools}

# 高危工具清单：执行前必须触发人工审批挂起
HIGH_RISK_TOOLS = {"execute_bank_transfer"}

# ==========================================
# 3. 初始化大模型
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

# ==========================================
# 4. 构建工作流节点
# ==========================================
def agent_node(state: AgentState):
    """思考推理节点：根据当前状态与历史消息调用 LLM"""
    messages = state["messages"]
    print(f"\n[NODE] -> agent_node 思考中 (当前历史消息数: {len(messages)})...")
    response = llm.invoke(messages)
    return {"messages": [response]}

def human_approval_guard(state: AgentState):
    """
    高危操作人机协同拦截节点 (HITL Guard)
    如果上一轮大模型声明了高危工具调用，自动中断挂起并保存 Checkpoint，等待外部批准信号
    """
    last_message = state["messages"][-1]
    tool_calls = getattr(last_message, "tool_calls", [])
    
    for tc in tool_calls:
        if tc["name"] in HIGH_RISK_TOOLS:
            print(f"\n⚠️  [HITL GUARD 触发中断] 检测到高危操作: {tc['name']} -> {tc['args']}")
            # interrupt() 会抛出特殊的 GraphInterrupt 异常
            # LangGraph 会将当前状态完整落盘，并挂起进程等待 resume
            approval_decision = interrupt({
                "action": "REQUIRE_HUMAN_APPROVAL",
                "tool_name": tc["name"],
                "arguments": tc["args"],
                "prompt": f"请确认是否批准转账 ¥{tc['args'].get('amount')} 到账户 {tc['args'].get('account_no')}？"
            })
            
            print(f"收到人工审核指令: {approval_decision}")
            if not approval_decision.get("approved", False):
                # 审批被拒绝
                reject_reason = approval_decision.get("reason", "人工审核拒绝执行此高风险操作")
                return {
                    "audit_notes": [f"审核拒绝: {reject_reason}"],
                    "messages": [ToolMessage(
                        content=f"操作被人工拦截并拒绝: {reject_reason}",
                        tool_call_id=tc["id"]
                    )]
                }
            else:
                return {
                    "audit_notes": [f"审核通过，审批人: {approval_decision.get('reviewer', 'Admin')}"]
                }
    return {}

def tools_node(state: AgentState):
    """工具执行节点"""
    last_message = state["messages"][-1]
    tool_calls = getattr(last_message, "tool_calls", [])
    results = []
    
    # 获取之前是否已经有审批拒绝记录的 tool_id
    existing_tool_call_ids = {
        m.tool_call_id for m in state["messages"] if isinstance(m, ToolMessage)
    }
    
    for tool_call in tool_calls:
        tool_id = tool_call["id"]
        if tool_id in existing_tool_call_ids:
            continue  # 已经被拦截处理过的无需重复执行
            
        name = tool_call["name"]
        args = tool_call["args"]
        print(f"\n[NODE] -> tools_node 正在调用工具: {name} (ID: {tool_id})")
        
        if name in tool_map:
            try:
                res = tool_map[name].invoke(args)
            except Exception as e:
                res = f"工具异常: {e}"
        else:
            res = f"未找到工具: {name}"
            
        results.append(ToolMessage(content=str(res), tool_call_id=tool_id))
        
    return {"messages": results}

def should_continue(state: AgentState) -> Literal["guard", "__end__"]:
    """条件路由：根据是否有工具调用决定是否进入保护门禁"""
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        # 只要存在工具调用，先过安全门禁
        return "guard"
    return END

def after_guard_condition(state: AgentState) -> Literal["tools", "agent"]:
    """安全门禁后的路由：如果已被拒绝直接回给 Agent，否则执行工具"""
    last_message = state["messages"][-1]
    # 如果门禁已经生成了拒绝的 ToolMessage，直接回到 agent 组织回复
    if isinstance(last_message, ToolMessage):
        return "agent"
    return "tools"

# ==========================================
# 5. 构建与编译图
# ==========================================
workflow = StateGraph(AgentState)
workflow.add_node("agent", agent_node)
workflow.add_node("guard", human_approval_guard)
workflow.add_node("tools", tools_node)

workflow.add_edge(START, "agent")
workflow.add_conditional_edges("agent", should_continue, {"guard": "guard", END: END})
workflow.add_conditional_edges("guard", after_guard_condition, {"tools": "tools", "agent": "agent"})
workflow.add_edge("tools", "agent")

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_checkpoints.db")

_cached_app = None
_cached_conn = None

def get_agent_app(rebuild: bool = False):
    """
    获取绑定持久化 SqliteSaver 的 Agent 实例。
    采用单例缓存机制，避免重复创建 SQLite 连接和反复调用 workflow.compile()。
    """
    global _cached_app, _cached_conn
    if _cached_app is None or rebuild:
        if _cached_conn is not None:
            try:
                _cached_conn.close()
            except Exception:
                pass
        _cached_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        checkpointer = SqliteSaver(_cached_conn)
        checkpointer.setup()
        _cached_app = workflow.compile(checkpointer=checkpointer)
    return _cached_app

def close_agent_app():
    """显式关闭持久化连接并释放单例资源"""
    global _cached_app, _cached_conn
    if _cached_conn is not None:
        try:
            _cached_conn.close()
        except Exception:
            pass
        _cached_conn = None
    _cached_app = None

# ==========================================
# 6. 核心接口：启动任务、恢复执行与状态回溯
# ==========================================
def run_task(thread_id: str, prompt: str, app=None) -> bool:
    """首次启动任务。返回值: True 表示任务已全部完成，False 表示任务被门禁中断挂起等待审批"""
    app = app or get_agent_app()
    config = {"configurable": {"thread_id": thread_id}}
    
    print(f"\n==========================================")
    print(f"🚀 [启动任务] Thread ID: {thread_id}")
    print(f"用户需求: {prompt}")
    print(f"==========================================")
    
    messages = [
        SystemMessage(content=(
            "你是一个自动化财务助手。当用户要求算奖金并转账时：\n"
            "第一步调用 calculate_salary_bonus 计算奖金。\n"
            "第二步必须直接调用 execute_bank_transfer 执行转账（系统底层有安全审批门禁接管，你只需发起调用即可，不用先口头询问确认）。"
        )),
        HumanMessage(content=prompt)
    ]
    
    # 执行图
    for event in app.stream({"messages": messages, "requires_approval": False, "audit_notes": []}, config=config):
        for node_name, values in event.items():
            print(f"  └─ 节点 [{node_name}] 执行完毕")
            
    # 检查图的当前状态（是否中断）
    state = app.get_state(config)
    if state.next:
        print(f"\n⏸️ [任务已中断挂起] 下一步待执行节点: {state.next}")
        if state.tasks and state.tasks[0].interrupts:
            for intr in state.tasks[0].interrupts:
                print(f"   中断原因/审批详情: {intr.value}")
        print(f"👉 任务当前持久化在 SQLite 中，随时可恢复！")
        return False
    else:
        print(f"\n🎉 [任务圆满完成] 最终输出:")
        print(state.values["messages"][-1].content)
        return True

def resume_task(thread_id: str, approval_decision: dict, app=None):
    """断点恢复任务 (注入审批决策并继续执行)"""
    app = app or get_agent_app()
    config = {"configurable": {"thread_id": thread_id}}
    
    print(f"\n==========================================")
    print(f"⚡ [断点恢复] Thread ID: {thread_id}")
    print(f"注入审批决策: {approval_decision}")
    print(f"==========================================")
    
    # 通过 Command(resume=...) 唤醒中断节点
    for event in app.stream(Command(resume=approval_decision), config=config):
        for node_name, values in event.items():
            print(f"  └─ 节点 [{node_name}] 执行完毕")
            
    state = app.get_state(config)
    if state.next:
        print(f"\n⏸️ 任务再次中断: {state.next}")
    else:
        print(f"\n🎉 [任务恢复后已圆满完成] 最终输出:")
        print(state.values["messages"][-1].content)

def inspect_task(thread_id: str, app=None):
    """查看任务历史检查点与时间旅行信息"""
    app = app or get_agent_app()
    config = {"configurable": {"thread_id": thread_id}}
    
    print(f"\n==========================================")
    print(f"🔍 [检查点回溯] Thread ID: {thread_id}")
    print(f"==========================================")
    
    history = list(app.get_state_history(config))
    print(f"共记录了 {len(history)} 个历史快照（Checkpoints）：\n")
    for i, state_tuple in enumerate(history):
        ckpt_id = state_tuple.config["configurable"].get("checkpoint_id")
        next_nodes = state_tuple.next
        msg_count = len(state_tuple.values.get("messages", []))
        print(f"[{i+1}] Checkpoint ID: {ckpt_id} | 消息数: {msg_count} | 下一节点: {next_nodes}")

def prompt_for_approval() -> dict:
    """在终端交互式询问审批意见"""
    print("\n" + "=" * 55)
    print("📋 【人工在环 HITL 审批窗口】")
    print("=" * 55)
    print("请选择审批动作:")
    print("  [1] 同意批准 (Approve) - 允许大模型执行银行转账")
    print("  [2] 拒绝驳回 (Reject)  - 拦截高风险操作，通知大模型调整回复")
    
    choice = input("\n请输入选项 [1/2] (默认: 1): ").strip()
    if choice == "2":
        reason = input("请输入拒绝原因 (默认: 财务合规性审核未通过): ").strip() or "财务合规性审核未通过"
        reviewer = input("请输入审批人姓名 (默认: Compliance_Officer): ").strip() or "Compliance_Officer"
        return {
            "approved": False,
            "reviewer": reviewer,
            "reason": reason
        }
    else:
        reviewer = input("请输入审批人姓名 (默认: HR_Director_Alice): ").strip() or "HR_Director_Alice"
        reason = input("请输入通过附言 (默认: 绩效考核与金额核对无误): ").strip() or "绩效考核与金额核对无误"
        return {
            "approved": True,
            "reviewer": reviewer,
            "reason": reason
        }

# ==========================================
# 7. 本地演示入口：一键交互选择模式 (单次运行)
# ==========================================
if __name__ == "__main__":
    demo_thread = "payroll_task_2026_001"
    prompt = "员工张三的基本工资是 20000，绩效得分 95。请先计算他的奖金，然后转账给账户 '6222020203040506'，事由为'年终奖发放'。"
    
    try:
        app = get_agent_app()
        
        # 步骤 1: 首次执行任务（将自动计算奖金，随后在转账前被安全门禁拦截中断挂起）
        is_finished = run_task(demo_thread, prompt, app=app)
        
        # 步骤 2: 若触发审批中断，在控制台交互式让用户选择审批意见并恢复执行
        if not is_finished:
            inspect_task(demo_thread, app=app)
            
            # 终端交互弹出选择：同意批准 / 拒绝驳回
            approval_decision = prompt_for_approval()
            
            # 恢复执行 (Resume)
            resume_task(demo_thread, approval_decision, app=app)
    finally:
        # 演示完毕，释放持久化数据库连接
        close_agent_app()
