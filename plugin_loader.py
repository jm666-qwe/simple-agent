"""插件系统 — 热加载 plugins/ 目录下的工具，无需重启"""

import importlib.util
import os
import sys
import time
from pathlib import Path

from tool_base import BaseTool, ToolRegistry

PLUGINS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")


def discover_plugins() -> list[str]:
    """扫描 plugins/ 目录，返回 .py 文件列表"""
    if not os.path.isdir(PLUGINS_DIR):
        return []
    return sorted(
        f for f in os.listdir(PLUGINS_DIR)
        if f.endswith(".py") and not f.startswith("_")
    )


def load_plugin(filepath: str) -> list[BaseTool]:
    """动态加载单个插件文件，返回发现的 BaseTool 子类实例"""
    tools = []
    module_name = Path(filepath).stem

    try:
        spec = importlib.util.spec_from_file_location(
            f"plugin_{module_name}_{int(time.time() * 1000)}", filepath
        )
        if spec is None or spec.loader is None:
            print(f"  [插件] 无法加载: {module_name}")
            return tools

        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        # 找所有 BaseTool 子类
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseTool)
                and attr is not BaseTool
                and attr_name != "MCPToolWrapper"
            ):
                try:
                    instance = attr()
                    tools.append(instance)
                except Exception as e:
                    print(f"  [插件] 实例化失败 {attr_name}: {e}")
    except Exception as e:
        print(f"  [插件] 加载 '{module_name}' 失败: {e}")

    return tools


def register_plugins(registry: ToolRegistry) -> int:
    """扫描并注册所有插件，返回新注册数量。支持热重载"""
    files = discover_plugins()
    count = 0
    for fname in files:
        filepath = os.path.join(PLUGINS_DIR, fname)
        for tool in load_plugin(filepath):
            try:
                registry.register(tool)
                danger = " [需确认]" if tool.is_dangerous() else ""
                print(f"  [插件] 加载 {tool.name}{danger}")
                count += 1
            except ValueError:
                # 工具名已存在，跳过
                pass
    return count
