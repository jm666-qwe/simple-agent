"""示例插件 — 需要新工具时，在 plugins/ 下创建 .py 文件即可自动加载"""

from tool_base import BaseTool, ToolResult


class HelloTool(BaseTool):
    name = "hello"
    description = "打个招呼，返回问候语"
    parameters = {"name": {"type": "string", "description": "对方的名字"}}

    def run(self, name="世界"):
        return ToolResult(f"你好，{name}！这是插件系统自动加载的工具。")
