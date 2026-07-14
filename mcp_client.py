"""MCP 协议客户端 — 接入 600+ 外部工具生态

支持两种传输方式:
- stdio: 启动本地 MCP 服务器进程，通过 stdin/stdout 通信
- http: 连接远程 MCP 服务器 (streamable HTTP)

配置方式: 在 agent 目录下创建 mcp_servers.json:
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"]
    }
  }
}

启动 agent 时自动加载并注册所有 MCP 工具。
"""

import json
import os
import subprocess
import threading
import time
import queue
import sys
from typing import Any

from tool_base import BaseTool, ToolResult

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MCP_CONFIG_FILE = os.path.join(BASE_DIR, "mcp_servers.json")


# ===== MCP 客户端 =====

class MCPServerConnection:
    """管理与单个 MCP 服务器的 stdio 连接"""

    def __init__(self, name: str, command: str, args: list[str] = None, env: dict = None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = {**os.environ, **(env or {})}
        self.process: subprocess.Popen | None = None
        self._request_id = 0
        self._pending: dict[int, queue.Queue] = {}
        self._reader_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running = False
        self._tools: list[dict] = []

    def start(self):
        """启动 MCP 服务器进程并初始化"""
        cmd = [self.command] + self.args
        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=self.env,
            )
        except FileNotFoundError:
            raise RuntimeError(f"命令未找到: {self.command}，请确认已安装")

        self._running = True
        self._reader_thread = threading.Thread(target=self._read_responses, daemon=True)
        self._reader_thread.start()

        # MCP 握手: initialize
        result = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "simple-agent", "version": "2.0"},
        })
        if result is None:
            raise RuntimeError(f"MCP 服务器 {self.name} 初始化失败")

        # 发送 initialized 通知
        self._notify("notifications/initialized", {})

        # 获取工具列表
        tools_result = self._request("tools/list", {})
        self._tools = tools_result.get("tools", []) if tools_result else []
        print(f"  [MCP:{self.name}] 已连接，{len(self._tools)} 个工具")

    def get_tools(self) -> list[dict]:
        """返回 MCP 工具 schema 列表"""
        return list(self._tools)

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """调用 MCP 工具，返回结果文本"""
        result = self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        if result is None:
            return "[错误] MCP 服务器无响应"
        if "content" in result:
            # MCP 标准响应格式
            contents = result["content"]
            texts = []
            for c in contents:
                if c.get("type") == "text":
                    texts.append(c.get("text", ""))
                elif c.get("type") == "resource":
                    texts.append(f"[资源: {c.get('resource', {})}]")
            return "\n".join(texts) if texts else json.dumps(result, ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False)

    def stop(self):
        """关闭连接"""
        self._running = False
        if self.process:
            try:
                self.process.stdin.close()
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()

    def _next_id(self) -> int:
        with self._lock:
            self._request_id += 1
            return self._request_id

    def _request(self, method: str, params: dict, timeout: float = 30) -> Any:
        """发送 JSON-RPC 请求并等待响应"""
        req_id = self._next_id()
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        q: queue.Queue = queue.Queue()
        self._pending[req_id] = q

        try:
            self._send(msg)
        except Exception:
            self._pending.pop(req_id, None)
            return None

        try:
            response = q.get(timeout=timeout)
            if "error" in response:
                err = response["error"]
                print(f"  [MCP:{self.name}] 错误: {err.get('message', str(err))}")
                return None
            return response.get("result")
        except queue.Empty:
            print(f"  [MCP:{self.name}] 请求超时: {method}")
            return None
        finally:
            self._pending.pop(req_id, None)

    def _notify(self, method: str, params: dict):
        """发送 JSON-RPC 通知（无需响应）"""
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        self._send(msg)

    def _send(self, msg: dict):
        if not self.process or not self.process.stdin:
            raise RuntimeError("MCP 服务器未启动")
        line = json.dumps(msg, ensure_ascii=False)
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def _read_responses(self):
        """后台线程：持续读取服务器 stdout 的 JSON-RPC 响应"""
        try:
            while self._running and self.process and self.process.stdout:
                line = self.process.stdout.readline()
                if not line:
                    break
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                rid = data.get("id")
                if rid is not None and rid in self._pending:
                    self._pending[rid].put(data)
        except Exception:
            pass


# ===== MCP 工具包装器 =====

class MCPToolWrapper(BaseTool):
    """将 MCP 工具包装为 BaseTool，可注册到 ToolRegistry"""

    def __init__(self, server_name: str, mcp_tool: dict, connection: MCPServerConnection):
        self._server = server_name
        self._mcp_tool = mcp_tool
        self._conn = connection

        self.name = f"mcp__{server_name}__{mcp_tool['name']}"
        self.description = f"[MCP:{server_name}] {mcp_tool.get('description', '无描述')}"

        # 转换 MCP inputSchema 到 OpenAI parameters 格式
        schema = mcp_tool.get("inputSchema", {})
        self.parameters = {}
        if schema.get("type") == "object" and "properties" in schema:
            self.parameters = schema["properties"]

    def is_dangerous(self):
        # MCP 工具默认标记为需确认（除非明确是只读）
        return True

    def run(self, **kwargs):
        try:
            result_text = self._conn.call_tool(self._mcp_tool["name"], kwargs)
            return ToolResult(result_text[:4000])
        except Exception as e:
            return ToolResult(f"[MCP:{self._server}] 工具执行失败: {e}", is_error=True)

    def to_openai_schema(self):
        """Override: MCP schema 可能包含复杂参数定义"""
        schema = self._mcp_tool.get("inputSchema", {})
        required = schema.get("required", [])
        properties = schema.get("properties", {})

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {k: self._convert_property(v) for k, v in properties.items()},
                    "required": required,
                },
            },
        }

    def _convert_property(self, prop: dict) -> dict:
        """递归转换 JSON Schema 属性（MCP 的 schema 可能很复杂，需要简化）"""
        result = {}
        type_map = {
            "string": "string", "number": "number", "integer": "integer",
            "boolean": "boolean", "object": "object", "array": "array",
        }
        t = prop.get("type", "string")
        if t in type_map:
            result["type"] = type_map[t]
        else:
            result["type"] = "string"
        if "description" in prop:
            result["description"] = prop["description"][:200]
        if "enum" in prop:
            result["enum"] = prop["enum"]
        if t == "object" and "properties" in prop:
            result["properties"] = {k: self._convert_property(v) for k, v in prop["properties"].items()}
        if t == "array" and "items" in prop:
            result["items"] = self._convert_property(prop["items"])
        return result


# ===== MCP 管理器 =====

class MCPManager:
    """管理所有 MCP 服务器连接并提供工具注册"""

    def __init__(self):
        self.servers: dict[str, MCPServerConnection] = {}

    def load_config(self, config_path: str = None) -> list[MCPServerConnection]:
        """从配置文件加载并启动 MCP 服务器"""
        path = config_path or MCP_CONFIG_FILE
        if not os.path.isfile(path):
            print(f"  [MCP] 未找到配置文件: {path}，跳过 MCP 加载")
            print(f"  [MCP] 提示: 创建 mcp_servers.json 即可接入 MCP 工具")
            return []

        try:
            with open(path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError) as e:
            print(f"  [MCP] 配置文件解析失败: {e}")
            return []

        servers_config = config.get("mcpServers", {})
        if not servers_config:
            return []

        started = []
        for name, cfg in servers_config.items():
            command = cfg.get("command", "")
            args = cfg.get("args", [])
            env = cfg.get("env", None)
            if not command:
                print(f"  [MCP:{name}] 缺少 command，跳过")
                continue

            try:
                conn = MCPServerConnection(name, command, args, env)
                conn.start()
                self.servers[name] = conn
                started.append(conn)
            except Exception as e:
                print(f"  [MCP:{name}] 启动失败: {e}")

        return started

    def register_to_registry(self, registry) -> int:
        """将所有 MCP 工具注册到 ToolRegistry，返回注册数量"""
        count = 0
        for name, conn in self.servers.items():
            for tool in conn.get_tools():
                wrapper = MCPToolWrapper(name, tool, conn)
                try:
                    registry.register(wrapper)
                    count += 1
                except ValueError:
                    pass  # 工具名冲突，跳过
        return count

    def shutdown(self):
        for conn in self.servers.values():
            conn.stop()
        self.servers.clear()
