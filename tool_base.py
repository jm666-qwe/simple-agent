"""工具基类 + 注册表 — 每个工具都是独立的类"""

from abc import ABC, abstractmethod


class ToolResult:
    def __init__(self, content, is_error=False):
        self.content = str(content)
        self.is_error = is_error


class BaseTool(ABC):
    """工具基类，所有工具继承这个"""

    name: str = ""
    description: str = ""
    parameters: dict = {}  # JSON Schema properties

    @abstractmethod
    def run(self, **kwargs) -> ToolResult:
        """执行工具，返回 ToolResult"""
        ...

    def is_dangerous(self) -> bool:
        """是否需要权限确认"""
        return False

    def is_read_only(self) -> bool:
        """是否只读"""
        return not self.is_dangerous()

    def to_openai_schema(self):
        """生成 OpenAI function calling 格式"""
        schema = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
            },
        }
        if self.parameters:
            required = list(self.parameters.keys())
            schema["function"]["parameters"] = {
                "type": "object",
                "properties": self.parameters,
                "required": required,
            }
        else:
            schema["function"]["parameters"] = {"type": "object", "properties": {}}
        return schema


class ToolRegistry:
    """工具注册表：注册、查询、生成 schema 列表"""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool):
        if tool.name in self._tools:
            raise ValueError(f"工具名冲突: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise KeyError(f"未知工具: {name}")
        return self._tools[name]

    def get_all_schemas(self) -> list[dict]:
        return [t.to_openai_schema() for t in self._tools.values()]

    def list_names(self) -> list[str]:
        return sorted(self._tools.keys())
