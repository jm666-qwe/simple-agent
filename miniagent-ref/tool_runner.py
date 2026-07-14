from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from miniagent.changes import ChangeStore
from miniagent.messages import Message, ToolUseBlock
from miniagent.permissions import PermissionDecision, PermissionManager
from miniagent.security.secrets import redact_secret_text
from miniagent.tool_base import ToolContext, ToolRegistry, ToolResult
from miniagent.utils.text import clip_text
from miniagent.audit import AuditLogger

FILE_MUTATION_TOOLS = {"write_file", "edit_file"}


class ToolCall(BaseModel):
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionResult(BaseModel):
    call: ToolCall
    result: ToolResult
    permission: PermissionDecision | None = None


def parse_tool_calls(message: Message) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for block in message.content:
        if isinstance(block, ToolUseBlock):
            calls.append(ToolCall(id=block.id, name=block.name, input=block.input))
    return calls


def partition_tool_calls(
    calls: list[ToolCall], registry: ToolRegistry
) -> tuple[list[ToolCall], list[ToolCall]]:
    concurrent: list[ToolCall] = []
    serial: list[ToolCall] = []
    for call in calls:
        tool = registry.get(call.name)
        input_data = tool.validate_input(call.input)
        if tool.is_concurrency_safe(input_data):
            concurrent.append(call)
        else:
            serial.append(call)
    return concurrent, serial


class ToolRunner:
    def __init__(
        self,
        registry: ToolRegistry,
        permission_manager: PermissionManager,
        audit_logger: AuditLogger | None = None,
    ):
        self.registry = registry
        self.permission_manager = permission_manager
        self.audit_logger = audit_logger

    async def run_calls(
        self, calls: list[ToolCall], context: ToolContext
    ) -> list[ToolExecutionResult]:
        results: list[ToolExecutionResult] = []
        known_calls: list[ToolCall] = []
        for call in calls:
            if call.name in self.registry:
                known_calls.append(call)
            else:
                results.append(await self.run_call(call, context))
        concurrent, serial = partition_tool_calls(known_calls, self.registry)
        if concurrent:
            results.extend(await asyncio.gather(*(self.run_call(call, context) for call in concurrent)))
        applied_change_ids: list[str] = []
        for call in serial:
            result = await self.run_call(call, context)
            change_id = _result_change_id(result.result)
            if change_id and not result.result.is_error:
                applied_change_ids.append(change_id)
            elif result.result.is_error and call.name in FILE_MUTATION_TOOLS and applied_change_ids:
                rollback_display = self._rollback_changes(applied_change_ids, context)
                if rollback_display:
                    result.result.display = f"{result.result.display}\n\n[rollback]\n{rollback_display}"
                applied_change_ids.clear()
            results.append(result)
        return results

    async def run_call(self, call: ToolCall, context: ToolContext) -> ToolExecutionResult:
        try:
            tool = self.registry.get(call.name)
        except KeyError as exc:
            self._log("tool_error", {"call": call.model_dump(mode="json"), "error": str(exc)})
            return ToolExecutionResult(
                call=call,
                result=ToolResult(display=str(exc), is_error=True),
            )

        try:
            input_data = tool.validate_input(call.input)
        except ValidationError as exc:
            self._log("tool_error", {"call": call.model_dump(mode="json"), "error": str(exc)})
            return ToolExecutionResult(
                call=call,
                result=ToolResult(display=f"工具参数无效：{exc}", is_error=True),
            )

        permission = await self.permission_manager.decide(tool, input_data, context)
        self._log(
            "permission_decision",
            {
                "tool": call.name,
                "call_id": call.id,
                "decision": permission.model_dump(mode="json"),
            },
        )
        if not permission.allowed:
            self._log(
                "tool_result",
                {
                    "tool": call.name,
                    "call_id": call.id,
                    "is_error": True,
                    "reason": permission.reason,
                },
            )
            return ToolExecutionResult(
                call=call,
                permission=permission,
                result=ToolResult(display=permission.reason, is_error=True),
            )

        try:
            self._log("tool_call", {"call": call.model_dump(mode="json")})
            result = await tool.call(input_data, context)
        except Exception as exc:  # 工具边界兜底，避免 agent loop 崩溃。
            result = ToolResult(display=f"工具执行失败：{exc}", is_error=True)
        result.display = clip_text(redact_secret_text(result.display), context.max_result_chars)
        result.structured_content = {
            **(result.structured_content or {}),
            "source": {
                "kind": "tool_result",
                "tool": call.name,
                "trust": "untrusted",
            },
        }
        change_id = _result_change_id(result)
        if change_id and not result.is_error:
            self._log(
                "file_change",
                {
                    "tool": call.name,
                    "call_id": call.id,
                    "change_id": change_id,
                    "diff": result.display,
                },
            )
        self._log(
            "tool_result",
            {
                "tool": call.name,
                "call_id": call.id,
                "is_error": result.is_error,
                "display": result.display,
            },
        )
        return ToolExecutionResult(call=call, permission=permission, result=result)

    def _log(self, event_type: str, data: dict[str, Any]) -> None:
        if self.audit_logger:
            self.audit_logger.log(event_type, data)

    def _rollback_changes(self, change_ids: list[str], context: ToolContext) -> str:
        data_dir = context.data_dir or str(Path(context.cwd) / ".miniagent")
        try:
            results = ChangeStore(data_dir).revert_many(change_ids, cwd=context.cwd)
        except Exception as exc:
            self._log("file_change_rollback_error", {"change_ids": change_ids, "error": str(exc)})
            return f"回滚失败：{exc}"
        payload = [result.model_dump(mode="json") for result in results]
        self._log("file_change_rollback", {"change_ids": change_ids, "results": payload})
        return "\n".join(result.message for result in results)


def _result_change_id(result: ToolResult) -> str | None:
    if not result.structured_content:
        return None
    value = result.structured_content.get("change_id")
    return str(value) if value else None
