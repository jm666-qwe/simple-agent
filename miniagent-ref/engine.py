from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from miniagent.audit import AuditLogger
from miniagent.config import AgentConfig, default_config
from miniagent.context import ContextBuilder
from miniagent.events import (
    ASSISTANT_DELTA,
    ASSISTANT_MESSAGE,
    DONE,
    ERROR,
    PERMISSION_DECISION,
    REQUEST_START,
    SESSION_SAVED,
    TOOL_ERROR,
    TOOL_RESULT,
    TOOL_START,
    EngineEvent,
)
from miniagent.memory import build_session_memory_candidates
from miniagent.messages import Message, tool_result_message, user_text
from miniagent.model import ModelClient, create_model_router
from miniagent.permissions import PermissionManager
from miniagent.storage import SessionRecord, SessionStorage
from miniagent.tool_base import ToolContext, ToolRegistry
from miniagent.tool_runner import ToolCall, ToolRunner, parse_tool_calls
from miniagent.tools import builtin_registry
from miniagent.utils.ids import new_id


class QueryEngine:
    def __init__(
        self,
        *,
        model_client: ModelClient | None = None,
        registry: ToolRegistry | None = None,
        config: AgentConfig | None = None,
        storage: SessionStorage | None = None,
        context_builder: ContextBuilder | None = None,
        permission_manager: PermissionManager | None = None,
        audit_logger: AuditLogger | None = None,
        session: SessionRecord | None = None,
    ):
        self.config = config or default_config()
        self.registry = registry or builtin_registry()
        self.model_client = model_client or create_model_router(self.config.model)
        self.storage = storage or SessionStorage(self.config.resolved_data_dir)
        self.context_builder = context_builder or ContextBuilder()
        self.audit_logger = audit_logger or AuditLogger(self.config.audit_path)
        self.permission_manager = permission_manager or PermissionManager(
            non_interactive=self.config.non_interactive
        )
        self.session_id = session.id if session else new_id("sess")
        self.messages: list[Message] = list(session.messages) if session else []
        self.file_reads: dict[str, dict[str, Any]] = dict(session.file_reads) if session else {}
        self.state: dict[str, Any] = dict(session.state) if session else {}
        if session and session.todos:
            self.state["todos"] = session.todos
        self.tool_runner = ToolRunner(self.registry, self.permission_manager, self.audit_logger)
        self.tool_calls: list[dict[str, Any]] = list(session.tool_calls) if session else []
        self.tool_results: list[dict[str, Any]] = list(session.tool_results) if session else []
        self.permission_decisions: list[dict[str, Any]] = (
            list(session.permission_decisions) if session else []
        )

    async def submit(self, prompt: str) -> AsyncIterator[EngineEvent]:
        self.messages.append(user_text(prompt))
        self.audit_logger.log("request_start", {"session_id": self.session_id, "prompt": prompt})
        yield EngineEvent(type=REQUEST_START, data={"session_id": self.session_id})

        for turn in range(self.config.max_turns):
            request = self.context_builder.build(
                messages=self.messages,
                registry=self.registry,
                config=self.config,
                state=self.state,
            )
            self.audit_logger.log(
                "model_request",
                {
                    "session_id": self.session_id,
                    "turn": turn,
                    "message_count": len(request.messages),
                    "tool_count": len(request.tools),
                },
            )
            assistant: Message | None = None
            try:
                async for event in self.model_client.stream(request):
                    if event.type == "text_delta":
                        yield EngineEvent(type=ASSISTANT_DELTA, data=event.data)
                    elif event.type == "assistant_message":
                        assistant = Message.model_validate(event.data["message"])
            except Exception as exc:
                self.audit_logger.log("error", {"where": "model", "message": str(exc)})
                yield EngineEvent(type=ERROR, data={"message": f"模型调用失败：{exc}"})
                path = self._save()
                self.audit_logger.log(
                    "session_saved",
                    {"path": str(path), "session_id": self.session_id, "done": False},
                )
                yield EngineEvent(type=SESSION_SAVED, data={"path": str(path), "session_id": self.session_id})
                return

            if assistant is None:
                self.audit_logger.log("error", {"where": "model", "message": "missing assistant_message"})
                yield EngineEvent(type=ERROR, data={"message": "模型没有返回 assistant_message"})
                path = self._save()
                self.audit_logger.log(
                    "session_saved",
                    {"path": str(path), "session_id": self.session_id, "done": False},
                )
                yield EngineEvent(type=SESSION_SAVED, data={"path": str(path), "session_id": self.session_id})
                return

            self.messages.append(assistant)
            self.audit_logger.log(
                "assistant_message",
                {"message": assistant.model_dump(mode="json"), "turn": turn},
            )
            yield EngineEvent(
                type=ASSISTANT_MESSAGE,
                data={"message": assistant.model_dump(mode="json"), "turn": turn},
            )

            calls = parse_tool_calls(assistant)
            if not calls:
                path = self._save()
                self.audit_logger.log(
                    "session_saved",
                    {"path": str(path), "session_id": self.session_id, "done": True},
                )
                yield EngineEvent(type=SESSION_SAVED, data={"path": str(path), "session_id": self.session_id})
                yield EngineEvent(type=DONE, data={"session_id": self.session_id})
                return

            for call in calls:
                self.tool_calls.append(call.model_dump(mode="json"))
                self.audit_logger.log("tool_start", {"call": call.model_dump(mode="json")})
                yield EngineEvent(type=TOOL_START, data={"call": call.model_dump(mode="json")})

            for result in await self._run_tools(calls):
                tool_output = (
                    f"[source=tool_result trust=untrusted tool={result.call.name}]\n"
                    f"{result.result.display}"
                )
                block_message = tool_result_message(
                    result.call.id,
                    tool_output,
                    is_error=result.result.is_error,
                )
                self.messages.append(block_message)
                event_type = TOOL_ERROR if result.result.is_error else TOOL_RESULT
                yield EngineEvent(
                    type=event_type,
                    data={
                        "call": result.call.model_dump(mode="json"),
                        "result": result.result.model_dump(mode="json"),
                    },
                )
                self.tool_results.append(result.result.model_dump(mode="json"))
                if result.permission:
                    decision = result.permission.model_dump(mode="json")
                    self.permission_decisions.append(decision)
                    yield EngineEvent(type=PERMISSION_DECISION, data=decision)

        path = self._save()
        self.audit_logger.log("session_saved", {"path": str(path), "session_id": self.session_id})
        self.audit_logger.log("error", {"where": "engine", "message": "达到最大轮数，已停止"})
        yield EngineEvent(type=SESSION_SAVED, data={"path": str(path), "session_id": self.session_id})
        yield EngineEvent(type=ERROR, data={"message": "达到最大轮数，已停止"})

    async def _run_tools(self, calls: list[ToolCall]):
        context = ToolContext(
            cwd=self.config.cwd,
            session_id=self.session_id,
            permission_mode=self.config.permission_mode,
            max_result_chars=self.config.max_result_chars,
            data_dir=str(self.config.resolved_data_dir),
            file_reads=self.file_reads,
            state=self.state,
        )
        results = await self.tool_runner.run_calls(calls, context)
        self.file_reads = context.file_reads
        self.state = context.state
        return results

    def _save(self):
        candidates = build_session_memory_candidates(self.messages, session_id=self.session_id)
        if candidates:
            self.state["memory_candidates"] = [
                candidate.model_dump(mode="json") for candidate in candidates
            ]
        record = SessionRecord(
            id=self.session_id,
            cwd=self.config.cwd,
            messages=self.messages,
            tool_calls=self.tool_calls,
            tool_results=self.tool_results,
            permission_decisions=self.permission_decisions,
            todos=self.state.get("todos", []),
            file_reads=self.file_reads,
            state=self.state,
        )
        return self.storage.save(record)
