"""
Simple Agent — 单文件 ReAct 实现，从零理解 AI Agent 原理

核心循环只有 4 步:
  用户输入 → LLM 决定调哪个工具 → 执行工具 → 结果塞回去 → 循环

跑起来: python3 agent_simple.py
"""

import json, os, sys, re
from datetime import datetime
from openai import OpenAI

# ===== 1. 加载 API Key =====

def load_api_key():
    """从 .env 文件读 DEEPSEEK_API_KEY"""
    with open(os.path.join(os.path.dirname(__file__), ".env")) as f:
        for line in f:
            m = re.match(r"DEEPSEEK_API_KEY\s*=\s*(.+)", line.strip())
            if m:
                return m.group(1).strip()
    raise RuntimeError("未找到 DEEPSEEK_API_KEY，请先创建 .env 文件")

client = OpenAI(
    api_key=load_api_key(),
    base_url="https://api.deepseek.com",
)
MODEL = "deepseek-chat"

# ===== 2. 工具定义（OpenAI function-calling 格式）=====

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "安全计算数学表达式",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string", "description": "如 '3.14 * 2 ** 2'"}},
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "获取当前日期时间",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

def execute_tool(name, args):
    """执行工具并返回结果字符串"""
    if name == "calculate":
        expr = args.get("expression", "")
        # 安全沙箱：白名单字符 + 禁止内置函数
        allowed = set("0123456789.+-*/()% eabcdefghijklmnopqrstuvwxyz_")
        if any(c not in allowed for c in expr):
            return "[错误] 表达式包含非法字符"
        try:
            code = compile(expr, "<math>", "eval")
            safe = {"abs": abs, "round": round, "min": min, "max": max}
            return str(eval(code, {"__builtins__": {}}, safe))
        except Exception as e:
            return f"[计算错误] {e}"
    if name == "get_time":
        return datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")
    return f"[未知工具] {name}"

# ===== 3. 核心 ReAct 循环 =====

def agent(user_input):
    messages = [
        {"role": "system", "content": "你是智能助手。需要计算时用 calculate，需要时间时用 get_time。"},
        {"role": "user", "content": user_input},
    ]

    print(f"\n[用户] {user_input}")

    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            stream=False,  # 教学版用非流式，逻辑更清晰
        )
        msg = response.choices[0].message

        # 纯文本 → 结束
        if not msg.tool_calls:
            print(f"[助手] {msg.content}")
            return msg.content

        # 工具调用 → 执行并继续
        messages.append(msg.model_dump())
        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            result = execute_tool(name, args)
            print(f"  -> {name}({args})")
            print(f"  <- {result}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })
        # 循环回去，LLM 根据工具结果继续思考

# ===== 4. 入口 =====

if __name__ == "__main__":
    if len(sys.argv) > 1:
        agent(" ".join(sys.argv[1:]))
    else:
        print("用法: python3 agent_simple.py <你的问题>")
        print('举例: python3 agent_simple.py "1+1等于几"')
