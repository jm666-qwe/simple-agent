"""权限系统 — 写文件和执行命令前弹确认"""

from enum import Enum


class PermissionMode(Enum):
    DEFAULT = "default"       # 危险操作弹确认
    AUTO = "auto"             # 全部自动允许（--yes）
    PLAN = "plan"             # 只允许只读


DANGEROUS_TOOLS = {"write_file", "run_command"}


class PermissionManager:
    def __init__(self, mode="default"):
        self.mode = PermissionMode(mode)
        self._session_allow = set()  # 本次会话已允许的工具

    def check(self, tool_name: str, args: dict) -> tuple[bool, str]:
        """检查是否允许执行。返回 (允许?, 原因)"""
        # 非危险工具直接放行
        if tool_name not in DANGEROUS_TOOLS:
            return True, "只读工具，自动允许"

        # plan 模式：禁止写
        if self.mode == PermissionMode.PLAN:
            return False, "plan 模式，禁止修改文件或执行命令"

        # auto 模式：全部放行
        if self.mode == PermissionMode.AUTO:
            return True, "auto 模式，自动允许"

        # 会话内已允许的同类工具，不再重复确认
        if tool_name in self._session_allow:
            return True, "本次会话已授权"

        # default 模式：弹确认
        preview = self._format_preview(tool_name, args)
        show = f"\n[权限确认] 是否允许 {tool_name}？\n{preview}\n允许? (y/n/always): "
        try:
            choice = input(show).strip().lower()
        except (EOFError, OSError):
            return False, "非交互环境，自动拒绝"

        if choice == "always":
            self._session_allow.add(tool_name)
            return True, "用户允许（本次会话自动放行）"
        if choice == "y":
            return True, "用户允许"
        return False, "用户拒绝"

    def _format_preview(self, tool_name, args):
        if tool_name == "write_file":
            path = args.get("path", "?")
            content = str(args.get("content", ""))
            return f"  文件: {path}\n  内容预览: {content[:120]}{'...' if len(content) > 120 else ''}"
        if tool_name == "run_command":
            return f"  命令: {args.get('cmd', '?')}"
        return f"  参数: {args}"
