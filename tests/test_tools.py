"""工具层测试 — 覆盖 calculate, memory CRUD, permissions, search 安全校验"""

import os
import sys
import json
import time
import tempfile

# 确保可以导入项目模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tool_base import BaseTool, ToolResult, ToolRegistry
from tools import (
    CalculateTool, TimeTool,
    _is_private_url,
)
from permissions import PermissionManager
from memory import MemoryStore, _tokenize


# ===== Calculate =====

class TestCalculate:
    def test_basic_arithmetic(self):
        tool = CalculateTool()
        r = tool.run("2 + 3 * 4")
        assert not r.is_error
        assert "14" in r.content

    def test_power(self):
        tool = CalculateTool()
        r = tool.run("2 ** 10")
        assert not r.is_error
        assert "1024" in r.content

    def test_safe_functions(self):
        tool = CalculateTool()
        r = tool.run("abs(-5) + round(3.14)")
        assert not r.is_error
        assert "8" in r.content  # 5 + 3

    def test_reject_dangerous_input(self):
        tool = CalculateTool()
        r = tool.run("__import__('os').system('ls')")
        assert r.is_error  # 包含不允许的字符

    def test_reject_hidden_function(self):
        tool = CalculateTool()
        r = tool.run("eval('1+1')")
        assert r.is_error  # eval 不在允许列表


# ===== Time =====

class TestTime:
    def test_returns_current_time(self):
        tool = TimeTool()
        r = tool.run()
        assert not r.is_error
        assert "年" in r.content
        assert "月" in r.content


# ===== Permissions =====

class TestPermissions:
    def test_readonly_tool_auto_allowed(self):
        pm = PermissionManager("default")
        allowed, _ = pm.check("calculate", {})
        assert allowed

    def test_dangerous_tool_default_asks(self):
        pm = PermissionManager("default")
        # 非交互模式下，危险工具应该被拒绝
        allowed, reason = pm.check("write_file", {"path": "/tmp/test"})
        # default 模式 + 无交互终端 → 应该拒绝
        assert not allowed

    def test_auto_mode_allows_all(self):
        pm = PermissionManager("auto")
        allowed, _ = pm.check("write_file", {"path": "/tmp/test"})
        assert allowed

    def test_plan_mode_blocks_writes(self):
        pm = PermissionManager("plan")
        allowed, _ = pm.check("write_file", {"path": "/tmp/test"})
        assert not allowed
        allowed2, _ = pm.check("read_file", {"path": "/tmp/test"})
        assert allowed2


# ===== URL 安全校验 =====

class TestURLSecurity:
    def test_localhost_blocked(self):
        assert _is_private_url("http://127.0.0.1/admin")
        assert _is_private_url("http://localhost/admin")

    def test_private_ip_blocked(self):
        assert _is_private_url("http://192.168.1.1/")
        assert _is_private_url("http://10.0.0.1/")
        assert _is_private_url("http://172.16.0.1/")

    def test_ipv6_loopback_blocked(self):
        assert _is_private_url("http://[::1]/")

    def test_ipv4_mapped_ipv6_blocked(self):
        assert _is_private_url("http://[::ffff:127.0.0.1]/")

    def test_ipv4_compatible_ipv6_blocked(self):
        assert _is_private_url("http://[::127.0.0.1]/")

    def test_public_url_allowed(self):
        assert not _is_private_url("https://github.com")
        assert not _is_private_url("https://www.google.com")


# ===== Memory =====

class TestMemory:
    def setup_method(self):
        # 使用临时文件
        self.tmpfile = tempfile.mktemp(suffix=".json")
        import memory as mem_module
        self._orig = mem_module.MEMORY_FILE
        mem_module.MEMORY_FILE = self.tmpfile
        self.store = MemoryStore()

    def teardown_method(self):
        import memory as mem_module
        mem_module.MEMORY_FILE = self._orig
        try:
            os.remove(self.tmpfile)
        except FileNotFoundError:
            pass

    def test_remember_and_recall(self):
        self.store.remember("test_key", "test_value", ["tag1"])
        result = self.store.recall("test_key")
        assert "test_value" in result

    def test_recall_keyword_match(self):
        self.store.remember("name", "乔唯一", ["用户"])
        self.store.remember("city", "北京", ["位置"])
        result = self.store.recall("乔")
        assert "乔唯一" in result
        assert "北京" not in result  # 关键词不匹配

    def test_forget(self):
        self.store.remember("tmp", "删除我")
        result = self.store.forget("tmp")
        assert "已删除" in result
        assert "暂无记忆" in self.store.recall("tmp")

    def test_list_all(self):
        self.store.remember("a", "1")
        self.store.remember("b", "2")
        result = self.store.recall("")
        assert "a" in result
        assert "b" in result

    def test_tokenize_chinese(self):
        tokens = _tokenize("你好世界 hello")
        assert "你好" in tokens or "世界" in tokens
        assert "hello" in tokens


# ===== Tool Registry =====

class TestToolRegistry:
    def test_register_and_get(self):
        registry = ToolRegistry()
        registry.register(CalculateTool())
        tool = registry.get("calculate")
        assert tool.name == "calculate"

    def test_get_all_schemas(self):
        registry = ToolRegistry()
        registry.register(CalculateTool())
        registry.register(TimeTool())
        schemas = registry.get_all_schemas()
        assert len(schemas) == 2
        names = [s["function"]["name"] for s in schemas]
        assert "calculate" in names
        assert "get_current_time" in names

    def test_duplicate_register_raises(self):
        registry = ToolRegistry()
        registry.register(CalculateTool())
        try:
            registry.register(CalculateTool())
            assert False, "应该抛出异常"
        except ValueError:
            pass

    def test_unknown_tool_raises(self):
        registry = ToolRegistry()
        try:
            registry.get("nonexistent")
            assert False
        except KeyError:
            pass


# ===== 旧记忆迁移 =====

class TestMemoryMigration:
    def setup_method(self):
        self.tmpfile = tempfile.mktemp(suffix=".json")
        import memory as mem_module
        self._orig = mem_module.MEMORY_FILE
        mem_module.MEMORY_FILE = self.tmpfile

    def teardown_method(self):
        import memory as mem_module
        mem_module.MEMORY_FILE = self._orig
        try:
            os.remove(self.tmpfile)
        except FileNotFoundError:
            pass

    def test_old_format_migration(self):
        # 写入旧格式
        old = {"key1": "strvalue", "key2": "strvalue2"}
        with open(self.tmpfile, "w", encoding="utf-8") as f:
            json.dump(old, f)

        from memory import migrate_old_memory
        migrate_old_memory()

        with open(self.tmpfile, "r", encoding="utf-8") as f:
            new = json.load(f)

        assert isinstance(new["key1"], dict)
        assert new["key1"]["value"] == "strvalue"
        assert "tags" in new["key1"]
