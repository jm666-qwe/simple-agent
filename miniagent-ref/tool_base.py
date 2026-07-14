from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    display: str
    is_error: bool = False
    structured_content: dict[str, Any] | None = None


class ToolContext(BaseModel):
    cwd: str
    session_id: str
    permission_mode: str
    max_result_chars: int = 6000
    data_dir: str | None = None
    file_reads: dict[str, dict[str, Any]] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)


class EmptyInput(BaseModel):
    pass


class BaseTool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[BaseModel]] = EmptyInput

    def is_read_only(self, input_data: BaseModel) -> bool:
        return False

    def is_concurrency_safe(self, input_data: BaseModel) -> bool:
        return self.is_read_only(input_data)

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }

    def validate_input(self, input_data: dict[str, Any] | BaseModel) -> BaseModel:
        return self.input_model.model_validate(input_data)

    @abstractmethod
    async def call(self, input_data: BaseModel, context: ToolContext) -> ToolResult:
        raise NotImplementedError


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._metadata: dict[str, dict[str, Any]] = {}

    def register(self, tool: BaseTool, metadata: dict[str, Any] | None = None) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具已注册：{tool.name}")
        self._tools[tool.name] = tool
        self._metadata[tool.name] = metadata or {"source": "builtin"}

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._metadata.pop(name, None)

    def get(self, name: str) -> BaseTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"未知工具：{name}") from exc

    def metadata(self, name: str) -> dict[str, Any]:
        return dict(self._metadata.get(name, {}))

    def set_metadata(self, name: str, metadata: dict[str, Any]) -> None:
        if name not in self._tools:
            raise KeyError(f"未知工具：{name}")
        self._metadata[name] = metadata

    def names(self) -> list[str]:
        return sorted(self._tools)

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [self._tools[name].schema() for name in self.names()]

    def __contains__(self, name: str) -> bool:
        return name in self._tools
