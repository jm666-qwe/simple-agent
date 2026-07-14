from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from enum import Enum

from pydantic import BaseModel

from miniagent.security.permissions import (
    classify_tool_request,
    hard_deny_reason,
    match_session_rule,
    sensitive_tool_path_reason,
)
from miniagent.tool_base import BaseTool, ToolContext


class PermissionMode(str, Enum):
    default = "default"
    accept_edits = "accept_edits"
    plan = "plan"
    bypass = "bypass"


class PermissionDecision(BaseModel):
    allowed: bool
    action: str
    reason: str
    risk: str | None = None
    source: str | None = None


ConfirmCallback = Callable[[str, str], bool | Awaitable[bool]]


class PermissionManager:
    def __init__(self, confirmer: ConfirmCallback | None = None, non_interactive: bool = True):
        self.confirmer = confirmer
        self.non_interactive = non_interactive

    async def decide(self, tool: BaseTool, input_data: object, context: ToolContext) -> PermissionDecision:
        mode = PermissionMode(context.permission_mode)
        classification = classify_tool_request(tool.name, input_data, context.cwd)
        risk = classification.get("risk")

        if reason := hard_deny_reason(tool.name, input_data):
            return PermissionDecision(
                allowed=False,
                action="deny",
                reason=reason,
                risk=str(risk),
                source="hard_deny",
            )
        if reason := sensitive_tool_path_reason(input_data, context.cwd):
            return PermissionDecision(
                allowed=False,
                action="deny",
                reason=reason,
                risk="sensitive_path",
                source="sensitive_path_guard",
            )

        if mode is PermissionMode.bypass:
            return PermissionDecision(
                allowed=True,
                action="allow",
                reason="bypass 模式允许执行",
                risk=str(risk),
                source="permission_mode",
            )

        read_only = tool.is_read_only(input_data)  # type: ignore[arg-type]
        planning_tool = tool.name in {"plan_update", "todo_read", "todo_write"}
        memory_tool = tool.name in {"remember", "forget_memory", "recall_memory"}

        deny_rule = match_session_rule(
            context.state.get("permission_rules"),
            tool_name=tool.name,
            input_data=input_data,
            action="deny",
        )
        if deny_rule:
            return PermissionDecision(
                allowed=False,
                action="deny",
                reason=deny_rule.reason or "会话级 deny 规则拒绝执行",
                risk=str(risk),
                source="session_rule",
            )

        allow_rule = match_session_rule(
            context.state.get("permission_rules"),
            tool_name=tool.name,
            input_data=input_data,
            action="allow",
        )
        if allow_rule:
            return PermissionDecision(
                allowed=True,
                action="allow",
                reason=allow_rule.reason or "会话级 allow 规则允许执行",
                risk=str(risk),
                source="session_rule",
            )

        override = match_session_rule(
            context.state.get("permission_overrides"),
            tool_name=tool.name,
            input_data=input_data,
        )
        if override:
            return PermissionDecision(
                allowed=override.action == "allow",
                action=override.action,
                reason=override.reason or f"会话 override {override.action}",
                risk=str(risk),
                source="session_override",
            )

        if mode is PermissionMode.plan:
            if read_only or planning_tool or tool.name == "recall_memory":
                return PermissionDecision(
                    allowed=True,
                    action="allow",
                    reason="plan 模式允许只读和计划工具",
                    risk=str(risk),
                    source="permission_mode",
                )
            return PermissionDecision(
                allowed=False,
                action="deny",
                reason="plan 模式禁止修改文件或执行命令",
                risk=str(risk),
                source="permission_mode",
            )

        if read_only or planning_tool or memory_tool:
            return PermissionDecision(
                allowed=True,
                action="allow",
                reason="只读或状态工具允许执行",
                risk=str(risk),
                source="permission_mode",
            )

        if mode is PermissionMode.accept_edits and tool.name in {"write_file", "edit_file"}:
            return PermissionDecision(
                allowed=True,
                action="allow",
                reason="accept_edits 模式允许文件编辑",
                risk=str(risk),
                source="permission_mode",
            )

        return await self._ask_or_deny(tool.name, context, risk=str(risk))

    async def _ask_or_deny(
        self,
        tool_name: str,
        context: ToolContext,
        *,
        risk: str | None = None,
    ) -> PermissionDecision:
        if self.non_interactive or self.confirmer is None:
            return PermissionDecision(
                allowed=False,
                action="deny",
                reason="非交互模式下需要确认的工具被拒绝",
                risk=risk,
                source="interactive_confirmation",
            )
        value = self.confirmer(tool_name, context.cwd)
        if inspect.isawaitable(value):
            value = await value
        if value:
            return PermissionDecision(
                allowed=True,
                action="allow",
                reason="用户确认允许执行",
                risk=risk,
                source="interactive_confirmation",
            )
        return PermissionDecision(
            allowed=False,
            action="deny",
            reason="用户拒绝执行",
            risk=risk,
            source="interactive_confirmation",
        )
