"""工具集 — 15 个工具，全部类化"""

import os
import re
import shlex
import socket
import struct
import subprocess
import html as _html_lib
from urllib.parse import urlparse

import requests
from datetime import datetime

from tool_base import BaseTool, ToolResult
from memory import MemoryStore

# 全局记忆实例，由 agent 注入
_memory_store: MemoryStore = None


def set_memory_store(store: MemoryStore):
    global _memory_store
    _memory_store = store


# ===== 安全计算 =====

_MATH_ALLOWED = set("0123456789.+-*/()% e")
_SAFE_FUNCS = {"abs": abs, "round": round, "min": min, "max": max, "pow": pow, "int": int, "float": float}


class CalculateTool(BaseTool):
    name = "calculate"
    description = "安全的数学计算，支持 + - * / ** % 和数学函数"
    parameters = {"expression": {"type": "string", "description": "数学表达式，如 '3.14 * 2 ** 2'"}}

    def run(self, expression=""):
        cleaned = expression.strip()
        if any(c not in _MATH_ALLOWED for c in cleaned):
            return ToolResult(f"表达式包含不允许的字符: {expression}", is_error=True)
        try:
            code = compile(cleaned, "<math>", "eval")
            for name in code.co_names:
                if name not in _SAFE_FUNCS:
                    return ToolResult(f"不允许的函数: {name}", is_error=True)
            result = str(eval(code, {"__builtins__": {}}, _SAFE_FUNCS))
            return ToolResult(result)
        except Exception as e:
            return ToolResult(f"计算失败: {e}", is_error=True)


# ===== 天气 =====

class WeatherTool(BaseTool):
    name = "get_weather"
    description = "查询城市天气"
    parameters = {"city": {"type": "string", "description": "城市名称，如 'Beijing', 'Shanghai'"}}

    def run(self, city=""):
        try:
            url = f"https://wttr.in/{city}?format=%C+%t+%h+%w"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            return ToolResult(f"{city}天气: {resp.text.strip()}")
        except requests.RequestException as e:
            return ToolResult(f"天气查询失败: {e}", is_error=True)


# ===== 时间 =====

class TimeTool(BaseTool):
    name = "get_current_time"
    description = "获取当前日期和时间"

    def run(self):
        return ToolResult(datetime.now().strftime("%Y年%m月%d日 %H:%M:%S"))


# ===== 记忆工具 =====

class RememberTool(BaseTool):
    name = "remember"
    description = "保存信息到长期记忆，如用户偏好、姓名、目标等"
    parameters = {
        "key": {"type": "string", "description": "记忆的标题/键"},
        "value": {"type": "string", "description": "要记住的内容"},
        "tags": {"type": "string", "description": "逗号分隔的标签，如 '用户,偏好'"},
    }

    def run(self, key="", value="", tags=""):
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        return ToolResult(_memory_store.remember(key, value, tag_list))


class RecallTool(BaseTool):
    name = "recall"
    description = "搜索记忆（关键词匹配 + 时间衰减排序）。有相关信息时自动调用"
    parameters = {"query": {"type": "string", "description": "搜索关键词"}}

    def run(self, query=""):
        _memory_store.update_use_count(query)
        return ToolResult(_memory_store.recall(query))


class RecallSemanticTool(BaseTool):
    name = "recall_semantic"
    description = "语义搜索记忆，理解查询含义而非单纯匹配关键词。用法同 recall"
    parameters = {"query": {"type": "string", "description": "搜索内容，自然语言描述"}}

    def run(self, query=""):
        return ToolResult(_memory_store.recall_semantic(query))


class ForgetTool(BaseTool):
    name = "forget"
    description = "删除一条记忆"
    parameters = {"key": {"type": "string", "description": "要删除的记忆 key"}}

    def run(self, key=""):
        return ToolResult(_memory_store.forget(key))


# ===== 命令执行 =====

_SAFE_PREFIXES = [
    "git status", "git diff", "git log", "git add", "git commit",
    "git branch", "git remote", "git stash",
    "ls", "cat ", "head ", "tail ", "wc ", "file ",
    "find ", "grep ", "du ", "df ", "pwd", "echo ", "date",
    "npm ls", "npm run", "npm test", "npm install", "npm ci",
    "pip list", "pip show", "python --version", "python3 --version",
    "node --version", "tree", "which ", "whereis ",
    "cp ", "mv ", "mkdir ", "rm ",
]


class RunCommandTool(BaseTool):
    name = "run_command"
    description = f"执行安全的白名单命令: {', '.join(_SAFE_PREFIXES)}"
    parameters = {"cmd": {"type": "string", "description": "要执行的命令"}}

    def is_dangerous(self):
        return True

    def run(self, cmd=""):
        allowed = any(
            cmd == prefix or cmd.startswith(prefix + " ")
            for prefix in _SAFE_PREFIXES
        )
        if not allowed:
            return ToolResult(f"[拒绝] 命令不在白名单: {cmd[:60]}", is_error=True)

        try:
            # 用 shlex 解析为结构化参数，不经过 shell
            argv = shlex.split(cmd)
            result = subprocess.run(
                argv,
                capture_output=True, text=True, timeout=30,
                cwd=os.path.expanduser("~"),
            )
            out = (result.stdout + result.stderr).strip()
            return ToolResult(out[:2000] if out else "[空输出]")
        except subprocess.TimeoutExpired:
            return ToolResult("[超时] 命令超过 30 秒", is_error=True)
        except Exception as e:
            return ToolResult(f"[错误] {e}", is_error=True)


# ===== 文件操作 =====

def _is_safe_path(path):
    home = os.path.expanduser("~")
    real = os.path.realpath(path)
    return real.startswith(home + os.sep) or real.startswith("/mnt/")


class ReadFileTool(BaseTool):
    name = "read_file"
    description = "读取文件内容，用于查看代码、配置、日志等"
    parameters = {
        "path": {"type": "string", "description": "文件路径"},
        "max_lines": {"type": "integer", "description": "最多读取行数，默认 100"},
    }

    def run(self, path="", max_lines=100):
        if not _is_safe_path(path):
            return ToolResult(f"[拒绝] 只能读取 home 目录下的文件: {path}", is_error=True)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if len(lines) > max_lines:
                return ToolResult(
                    "".join(lines[:max_lines]) + f"\n... (共 {len(lines)} 行，显示前 {max_lines})"
                )
            return ToolResult("".join(lines))
        except FileNotFoundError:
            return ToolResult(f"[错误] 文件不存在: {path}", is_error=True)
        except PermissionError:
            return ToolResult(f"[错误] 无权限: {path}", is_error=True)
        except Exception as e:
            return ToolResult(f"[错误] {e}", is_error=True)


class WriteFileTool(BaseTool):
    name = "write_file"
    description = "写入内容到文件。修改代码或创建文件时使用"
    parameters = {
        "path": {"type": "string", "description": "文件路径"},
        "content": {"type": "string", "description": "要写入的完整内容"},
    }

    def is_dangerous(self):
        return True

    def run(self, path="", content=""):
        try:
            real = os.path.realpath(os.path.abspath(path))
            if not _is_safe_path(real):
                return ToolResult(f"[拒绝] 只能写入 home 目录: {path}", is_error=True)
            os.makedirs(os.path.dirname(real), exist_ok=True)
            with open(real, "w", encoding="utf-8") as f:
                f.write(content)
            return ToolResult(f"已写入: {path} ({len(content)} 字符)")
        except Exception as e:
            return ToolResult(f"[错误] {e}", is_error=True)


# ===== GitHub 工具 =====

class GitHubSearchTool(BaseTool):
    name = "github_search"
    description = "搜索 GitHub 开源项目，按 stars 排序。用于找项目、找工具、找参考代码"
    parameters = {
        "keyword": {"type": "string", "description": "搜索关键词"},
        "max_results": {"type": "integer", "description": "返回数量，默认 5"},
    }

    def run(self, keyword="", max_results=5):
        try:
            url = "https://api.github.com/search/repositories"
            params = {"q": keyword, "sort": "stars", "order": "desc", "per_page": min(max_results, 10)}
            headers = {"Accept": "application/vnd.github+json", "User-Agent": "simple-agent"}
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])
            if not items:
                return ToolResult("没有找到相关项目。")
            lines = []
            for r in items[:max_results]:
                lines.append(f"{r['full_name']} · {r['stargazers_count']} stars · {r['language'] or 'N/A'}")
                lines.append(f"  {r.get('description', '无描述')[:120]}")
                lines.append(f"  https://github.com/{r['full_name']}")
            return ToolResult("\n".join(lines))
        except Exception as e:
            return ToolResult(f"搜索失败: {e}", is_error=True)


class GitHubReadmeTool(BaseTool):
    name = "github_readme"
    description = "获取指定 GitHub 仓库的 README 内容"
    parameters = {
        "owner": {"type": "string", "description": "仓库所有者"},
        "repo": {"type": "string", "description": "仓库名"},
    }

    def run(self, owner="", repo=""):
        try:
            url = f"https://api.github.com/repos/{owner}/{repo}/readme"
            headers = {"Accept": "application/vnd.github.raw+json", "User-Agent": "simple-agent"}
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            text = resp.text[:3000]
            if len(resp.text) > 3000:
                text += "\n... (已截断)"
            return ToolResult(text)
        except Exception as e:
            return ToolResult(f"获取失败: {e}", is_error=True)


# ===== 联网搜索 =====

class SearchWebTool(BaseTool):
    name = "search_web"
    description = "搜索互联网获取实时信息。需要最新数据、新闻、事实查询时使用。零成本，无需 API Key"
    parameters = {
        "query": {"type": "string", "description": "搜索关键词"},
        "max_results": {"type": "integer", "description": "返回数量，默认 5"},
    }

    def run(self, query="", max_results=5):
        try:
            # DuckDuckGo Instant Answer API — 免费，无需 Key
            url = "https://api.duckduckgo.com/"
            params = {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            lines = []

            # 摘要/直接答案
            abstract = data.get("AbstractText", "").strip()
            if abstract:
                source = data.get("AbstractURL", "")
                lines.append(f"[答案] {abstract[:300]}")
                if source:
                    lines.append(f"  来源: {source}")

            # 相关话题作为搜索结果
            topics = data.get("RelatedTopics", [])
            count = 0
            for t in topics:
                if count >= max_results:
                    break
                text = t.get("Text", "") if isinstance(t, dict) else str(t)
                url_in = t.get("FirstURL", "") if isinstance(t, dict) else ""
                # 提取纯文本
                text = re.sub(r"<[^>]+>", "", text).strip()
                if text:
                    count += 1
                    lines.append(f"\n{count}. {text[:200]}")
                    if url_in:
                        lines.append(f"   {url_in}")

            if not lines:
                return ToolResult(f"没有找到'{query}'的相关结果。")
            return ToolResult("\n".join(lines))
        except Exception as e:
            return ToolResult(f"搜索失败: {e}", is_error=True)


_BLOCKED_NETS = [
    (0x7F000000, 8), (0x0A000000, 8),       # 127.0.0.0/8, 10.0.0.0/8
    (0xAC100000, 12), (0xC0A80000, 16),      # 172.16.0.0/12, 192.168.0.0/16
    (0xA9FE0000, 16),                         # 169.254.0.0/16
]


def _is_private_url(url_str):
    """检查 URL 是否指向内网地址"""
    try:
        host = urlparse(url_str).hostname
        if not host:
            return False
        ip = socket.gethostbyname(host)
        packed = struct.unpack("!I", socket.inet_aton(ip))[0]
        for net, bits in _BLOCKED_NETS:
            mask = 0xFFFFFFFF << (32 - bits)
            if packed & mask == net:
                return True
    except Exception:
        return False
    return False


class ReadUrlTool(BaseTool):
    name = "read_url"
    description = "读取网页内容，提取纯文本。用于查看搜索结果中的具体页面"
    parameters = {"url": {"type": "string", "description": "网页 URL"}}

    def run(self, url=""):
        if _is_private_url(url):
            return ToolResult(f"[拒绝] 禁止访问内网地址: {url}", is_error=True)
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; SimpleAgent/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            }
            resp = requests.get(url, headers=headers, timeout=15, allow_redirects=False)
            resp.raise_for_status()

            # 检查是否是 HTML
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                return ToolResult(f"[跳过] 非文本内容 ({content_type[:50]}), 共 {len(resp.content)} 字节")

            # 简单 HTML → 文本提取
            html_text = resp.text
            # 去掉 script/style
            html_text = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", html_text, flags=re.I)
            html_text = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", html_text, flags=re.I)
            # 去掉标签
            html_text = re.sub(r"<[^>]+>", " ", html_text)
            # 解码 HTML 实体
            text = _html_lib.unescape(html_text)
            # 压缩空白
            text = re.sub(r"\s+", " ", text).strip()
            # 截断
            if len(text) > 4000:
                text = text[:4000] + "... (已截断)"
            return ToolResult(text) if text.strip() else ToolResult("[空页面]")
        except requests.RequestException as e:
            return ToolResult(f"获取失败: {e}", is_error=True)
        except Exception as e:
            return ToolResult(f"[错误] {e}", is_error=True)


# ===== 代码沙箱 =====

class RunPythonTool(BaseTool):
    name = "run_python"
    description = "在隔离子进程中执行 Python 代码。用于计算、测试、数据处理"
    parameters = {"code": {"type": "string", "description": "要执行的 Python 代码"}}

    def is_dangerous(self):
        return True

    def run(self, code=""):
        try:
            result = subprocess.run(
                ["python3", "-c", code],
                capture_output=True, text=True, timeout=30,
                cwd=os.path.expanduser("~"),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            out = (result.stdout + result.stderr).strip()
            if not out:
                out = "[无输出]"
            return ToolResult(out[:3000])
        except subprocess.TimeoutExpired:
            return ToolResult("[超时] 代码执行超过 30 秒", is_error=True)
        except FileNotFoundError:
            return ToolResult("[错误] 未找到 python3", is_error=True)
        except Exception as e:
            return ToolResult(f"[错误] {e}", is_error=True)


# ===== 注册全部工具 =====

def register_all_tools(registry):
    registry.register(CalculateTool())
    registry.register(WeatherTool())
    registry.register(TimeTool())
    registry.register(RememberTool())
    registry.register(RecallTool())
    registry.register(RecallSemanticTool())
    registry.register(ForgetTool())
    registry.register(RunCommandTool())
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(GitHubSearchTool())
    registry.register(GitHubReadmeTool())
    registry.register(SearchWebTool())
    registry.register(ReadUrlTool())
    registry.register(RunPythonTool())
