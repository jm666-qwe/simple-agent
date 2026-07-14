"""
Agent — 工具类化 · 权限确认 · 流式输出 · 模型抽象 · 语义搜索
启动: agent
重置: /reset
退出: quit / Ctrl+C
"""

import json
import os
import re
import sys

import webbrowser

from model import ModelClient, ModelConfig, safe_print
from tool_base import ToolRegistry, ToolResult
from tools import register_all_tools, set_memory_store
from memory import MemoryStore, migrate_old_memory
from permissions import PermissionManager
from mcp_client import MCPManager
from knowledge_graph import KnowledgeGraph
from plugin_loader import register_plugins

# ===== 初始化 =====

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHECKPOINT_FILE = os.path.join(BASE_DIR, "checkpoint.json")

# 加载 .env
def load_dotenv(path=None):
    if path is None:
        path = os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r'^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.+)\s*$', line)
            if m:
                key, val = m.group(1), m.group(2).strip()
                val = val.strip('\'"')
                if key not in os.environ:
                    os.environ[key] = val

load_dotenv()

# 核心组件
config = ModelConfig()
model = ModelClient(config)
registry = ToolRegistry()
migrate_old_memory()
memory = MemoryStore(model)
memory.set_model(model)
set_memory_store(memory)
register_all_tools(registry)

# 插件系统（自动发现 plugins/ 目录）
register_plugins(registry)

# MCP 工具加载
mcp_manager = MCPManager()
mcp_loaded = mcp_manager.load_config()
if mcp_loaded:
    mcp_count = mcp_manager.register_to_registry(registry)
    print(f"  [MCP] 已注册 {mcp_count} 个 MCP 工具")

permissions = PermissionManager()

# 检查 --yes 参数
if "--yes" in sys.argv or "-y" in sys.argv:
    permissions = PermissionManager("auto")
    sys.argv = [a for a in sys.argv if a not in ("--yes", "-y")]


# ===== 断点 =====

def load_checkpoint():
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if data else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_checkpoint(messages):
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)


def clear_checkpoint():
    try:
        os.remove(CHECKPOINT_FILE)
    except FileNotFoundError:
        pass


# ===== System Prompt =====

def build_system_prompt():
    all_mem = memory.recall("")
    prompt = (
        "你是带记忆的智能开发助手。\n"
        "工具: calculate, get_weather, get_current_time, "
        "remember(记住信息), recall(搜索记忆), recall_semantic(语义搜索), forget(删除记忆), "
        "search_web(搜索互联网获取实时信息), read_url(读取网页内容), "
        "github_search(搜索开源项目), github_readme(获取项目README), "
        "run_command(执行安全shell命令), read_file(读取文件), write_file(写入文件)。\n"
        "规则: 1.涉及用户信息必须remember 2.回答前先recall "
        "3.需要实时/最新信息时用search_web 4.找开源项目用github_search 5.修改代码用write_file"
    )
    if all_mem and all_mem != "暂无记忆。":
        prompt += f"\n\n【记忆】\n{all_mem}"
    return prompt


# ===== Trace 模式 =====

_trace_enabled = False

def trace(step, detail):
    """打印 ReAct 循环步骤"""
    if not _trace_enabled:
        return
    labels = {
        1: "[步骤1] LLM 推理 → 决定调用工具",
        2: "[步骤2] 执行工具",
        3: "[步骤3] 工具返回结果",
        4: "[步骤4] LLM 整合 → 生成回答 / 继续调工具",
    }
    label = labels.get(step, f"[步骤{step}]")
    print(f"\n  {label}")
    if detail:
        print(f"    {detail}")

# ===== Streaming Agent Loop =====

def agent(user_input, messages=None):
    if messages is None:
        messages = [{"role": "system", "content": build_system_prompt()}]

    messages.append({"role": "user", "content": user_input})
    print(f"\n[用户] {user_input}")
    save_checkpoint(messages)

    while True:
        # 获取 tool schemas（每次都重新生成，因为 registry 可能变化）
        tool_schemas = registry.get_all_schemas()

        try:
            text, tool_calls = model.chat_stream(messages, tool_schemas)
        except Exception as e:
            safe_print(f"\n[错误] LLM 调用失败: {e}")
            messages.append({"role": "assistant", "content": f"(系统错误: {e})"})
            save_checkpoint(messages)
            return

        # 纯文本回答 → 结束
        if not tool_calls:
            trace(4, f"最终回答: {text[:80]}...")
            messages.append({"role": "assistant", "content": text})
            save_checkpoint(messages)
            return

        # LLM 决定调用工具 → trace step 1
        tool_names = [tc["function"]["name"] for tc in tool_calls]
        trace(1, f"调用工具: {', '.join(tool_names)}")

        # 文本 + 工具调用
        assistant_msg = {"role": "assistant", "content": text or None}
        assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        for tc in tool_calls:
            name = tc["function"]["name"]
            args = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
            tool = registry.get(name)
            call_id = tc["id"]

            arg_str = str(args)[:80]
            print(f"  -> {name}({arg_str})")

            # 权限检查
            allowed, reason = permissions.check(name, args)
            if not allowed:
                print(f"  [拒绝] {reason}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": f"[权限拒绝] {reason}",
                })
                save_checkpoint(messages)
                continue

            # 执行
            try:
                trace(2, f"执行 {name}({arg_str})")
                result = tool.run(**args)
            except Exception as e:
                result = ToolResult(str(e), is_error=True)

            # 显示结果
            display = result.content[:80] + "..." if len(result.content) > 80 else result.content
            tag = "[错误]" if result.is_error else ""
            print(f"  <- {tag}{display}")
            trace(3, f"{name} 返回: {display}")

            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": result.content,
            })
            save_checkpoint(messages)


# ===== CLI =====

if __name__ == "__main__":
    # --trace 模式
    args = sys.argv[1:]
    if "--trace" in args:
        _trace_enabled = True
        args.remove("--trace")
    if "-t" in args:
        _trace_enabled = True
        args.remove("-t")

    # 单次模式
    if args:
        agent(" ".join(args))
        sys.exit(0)

    # 断点恢复
    ckpt = load_checkpoint()
    messages = None
    if ckpt:
        last = ckpt[-1] if ckpt else {}
        preview = str(last.get("content", ""))[:60]
        print(f"\n[断点] 上次: {preview}...")
        choice = input("恢复? (y/n): ").strip().lower()
        if choice == "y":
            messages = ckpt
            print("[已恢复]\n")
        else:
            clear_checkpoint()

    print(f"模型: {config.provider_name}/{config.model}")
    print(f"工具: {len(registry.list_names())} 个")
    print("/tools /graph /distill /mem /reload /reset quit\n")

    kg = KnowledgeGraph()

    while True:
        try:
            user = input("> ").strip()
            if user.lower() == "quit":
                mcp_manager.shutdown()
                print("bye")
                break
            if user.lower() == "/reset":
                clear_checkpoint()
                messages = None
                print("[已重置]\n")
                continue
            if user.lower() == "/tools":
                for name in registry.list_names():
                    t = registry.get(name)
                    danger = " [需确认]" if t.is_dangerous() else ""
                    print(f"  {name}{danger} — {t.description[:60]}")
                continue
            if user.lower() == "/graph":
                print("  生成知识图谱...")
                kg.extract_from_memory(memory)
                kg.extract_with_llm(memory, model)
                html_path = kg.generate_html()
                print(f"  {kg.summary()}")
                webbrowser.open(f"file://{html_path}")
                continue
            if user.lower() == "/distill":
                print("  LLM 蒸馏中...")
                result = memory.distill_daily()
                print(f"  {result}")
                continue
            if user.lower() == "/mem":
                mem = memory._load()
                daily = memory.get_daily()
                core = memory.get_core()
                print(f"  对话记忆: {len(mem)} 条")
                print(f"  日级摘要: {len(daily)} 天")
                print(f"  核心记忆: {len(core)} 条")
                if core:
                    for k, v in core.items():
                        val = v.get("value", str(v)) if isinstance(v, dict) else v
                        print(f"    * {k}: {val[:60]}")
                continue
            if user.lower() == "/reload":
                count = register_plugins(registry)
                print(f"  插件重载完成，新增 {count} 个工具，共 {len(registry.list_names())} 个")
                continue
            if user:
                agent(user, messages)
                messages = load_checkpoint()
        except (EOFError, KeyboardInterrupt):
            print("\n[已保存进度]")
            break
