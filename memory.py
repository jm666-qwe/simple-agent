"""增强记忆系统 — 语义搜索 + 时间衰减 + 三级归档 + 知识蒸馏"""

import json
import os
import re
import time
from enum import Enum

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE = os.path.join(BASE_DIR, "memory.json")
DAILY_FILE = os.path.join(BASE_DIR, "daily_memory.json")
CORE_FILE = os.path.join(BASE_DIR, "core_memory.json")
TIME_DECAY_DAYS = 30.0


class MemoryTier(Enum):
    EPISODIC = "episodic"   # 对话级（memory.json）
    DAILY = "daily"         # 日级摘要（daily_memory.json）
    CORE = "core"           # 核心画像（core_memory.json）


class MemoryStore:
    """增强记忆：
    - 存储格式: {key: {value, tags, importance, created, updated, use_count}}
    - 搜索: 关键词匹配 + 时间衰减 + 重要性加权
    - 语义搜索: 关键词无结果时用 LLM 做语义匹配
    """

    def __init__(self, model_client=None):
        self._model = model_client  # 用于语义搜索

    def set_model(self, model_client):
        self._model = model_client

    def remember(self, key, value, tags=None, importance=1.0):
        mem = self._load()
        now = time.time()
        old = mem.get(key, {})
        mem[key] = {
            "value": value,
            "tags": tags or [],
            "importance": float(importance),
            "created": old.get("created", now),
            "updated": now,
            "use_count": old.get("use_count", 0),
        }
        self._save(mem)
        return f"已记住: {key} = {value}"

    def recall(self, query=""):
        """关键词搜索，按综合评分排序"""
        mem = self._load()
        if not mem:
            return "暂无记忆。"

        now = time.time()
        if not query:
            items = list(mem.items())
        else:
            scored = []
            keywords = _tokenize(query)
            for k, v in mem.items():
                score = self._score(k, v, keywords, now)
                if score > 0:
                    scored.append((k, v, score))
            scored.sort(key=lambda x: x[2], reverse=True)
            items = [(k, v) for k, v, _ in scored]

        if not items:
            return f"没有跟'{query}'相关的记忆。"

        lines = []
        for k, v in items[:10]:
            age = (now - v.get("updated", now)) / 86400
            lines.append(f"- {k}: {v['value']}")
            if v.get("tags"):
                lines[-1] += f"  [{', '.join(v['tags'])}]"
        return "\n".join(lines)

    def recall_semantic(self, query, max_items=5):
        """语义搜索：用 LLM 理解 query 含义，找出最相关的记忆"""
        mem = self._load()
        if not mem:
            return "暂无记忆。"

        if len(mem) <= max_items:
            return self.recall("")

        # 先关键词筛一道
        keywords = _tokenize(query)
        now = time.time()
        scored = [(k, v, self._score(k, v, keywords, now)) for k, v in mem.items()]
        scored.sort(key=lambda x: x[2], reverse=True)

        # 取 top-15 给 LLM 做精排
        candidates = scored[:15]
        if not self._model or not candidates:
            return self.recall(query)

        mem_text = "\n".join(
            f"[{i}] key={k} value={v['value']} tags={v.get('tags', [])}"
            for i, (k, v, _) in enumerate(candidates)
        )

        # 让 LLM 选最相关的
        prompt = (
            f"用户查询: {query}\n\n"
            f"候选记忆:\n{mem_text}\n\n"
            f"选出最相关的 {max_items} 条记忆，只返回编号（如 0,3,7），不要解释。"
        )

        try:
            choice_text = self._model.chat_simple(
                [{"role": "user", "content": prompt}],
                max_tokens=50,
            )
            ids = [int(x) for x in re.findall(r"\d+", choice_text) if int(x) < len(candidates)]
        except Exception:
            ids = list(range(min(max_items, len(candidates))))

        lines = []
        for i in ids[:max_items]:
            k, v = candidates[i][0], candidates[i][1]
            lines.append(f"- {k}: {v['value']}")
        return "\n".join(lines) if lines else self.recall(query)

    def forget(self, key):
        mem = self._load()
        if key in mem:
            del mem[key]
            self._save(mem)
            return f"已删除: {key}"
        return f"没有'{key}'这条记忆。"

    def list_all(self):
        mem = self._load()
        if not mem:
            return "暂无记忆。"
        return "\n".join(f"- {k}: {v['value']}" for k, v in mem.items())

    def update_use_count(self, key):
        mem = self._load()
        if key in mem:
            mem[key]["use_count"] = mem[key].get("use_count", 0) + 1
            self._save(mem)

    def distill_daily(self):
        """用 LLM 总结近期记忆，生成日级摘要"""
        mem = self._load()
        if not mem or not self._model:
            return "无需蒸馏（无记忆或无模型）。"

        now = time.time()
        recent = []
        for k, v in mem.items():
            age_hours = (now - v.get("updated", now)) / 3600
            if age_hours < 48:  # 48小时内的
                recent.append(f"- {k}: {v.get('value', '')}")

        if len(recent) < 3:
            return "记忆太少，跳过蒸馏。"

        prompt = (
            "从以下近期对话记忆中提取重要信息，生成 3-5 条日级摘要。\n"
            "格式: key | value\n"
            "示例:\n"
            "用户位置 | 北京\n"
            "最近在学 | Python Agent 开发\n\n"
            f"近期记忆:\n{chr(10).join(recent)}\n\n"
            "只输出摘要，不要解释。"
        )

        try:
            text = self._model.chat_simple(
                [{"role": "user", "content": prompt}],
                max_tokens=300,
            )
        except Exception as e:
            return f"LLM 蒸馏失败: {e}"

        # 解析结果存入 daily
        daily = self._load_file(DAILY_FILE)
        date_key = time.strftime("%Y-%m-%d")
        daily[date_key] = daily.get(date_key, [])
        for line in text.strip().split("\n"):
            parts = line.strip().split("|", 1)
            if len(parts) == 2:
                daily[date_key].append({"key": parts[0].strip(), "value": parts[1].strip()})
        self._save_file(DAILY_FILE, daily)
        return f"已蒸馏 {len(daily.get(date_key, []))} 条日级记忆 ({date_key})"

    def get_daily(self, date=None):
        """读取日级记忆"""
        daily = self._load_file(DAILY_FILE)
        if date:
            return daily.get(date, [])
        return daily

    def get_core(self):
        """读取核心画像"""
        return self._load_file(CORE_FILE)

    def promote_to_core(self, key):
        """将一条记忆升级为核心"""
        mem = self._load()
        if key not in mem:
            return f"没有'{key}'这条记忆。"
        core = self._load_file(CORE_FILE)
        core[key] = mem[key]
        core[key]["promoted_at"] = time.time()
        self._save_file(CORE_FILE, core)
        return f"已将 '{key}' 升级为核心记忆"

    def export_for_graph(self):
        """导出所有记忆数据供知识图谱使用"""
        return {
            "episodic": self._load(),
            "daily": self._load_file(DAILY_FILE),
            "core": self._load_file(CORE_FILE),
        }

    def _load_file(self, filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_file(self, filepath, data):
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _score(self, key, item, keywords, now):
        if not keywords:
            return 1.0
        text = f"{key} {item['value']} {' '.join(item.get('tags', []))}".lower()
        keyword_hits = sum(1 for kw in keywords if kw in text)
        if keyword_hits == 0:
            return 0
        tag_bonus = sum(2 for tag in item.get("tags", []) if tag in keywords)
        age_days = max(0, (now - item.get("updated", now)) / 86400)
        recency = 1.0 / (1.0 + age_days / TIME_DECAY_DAYS)
        importance = max(0.0, float(item.get("importance", 1)))
        return keyword_hits * 2 + tag_bonus * 3 + recency + importance * 0.5

    def _load(self):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data):
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def _tokenize(text):
    return re.findall(r"[A-Za-z0-9_]+|[一-鿿]+", str(text).lower())


# 兼容旧版 k-v 记忆文件自动迁移
def migrate_old_memory():
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    if not data:
        return
    # 检查是否旧格式: 值是纯字符串
    sample = next(iter(data.values()), None)
    if isinstance(sample, str):
        new_data = {}
        for k, v in data.items():
            new_data[k] = {"value": v, "tags": [], "importance": 1.0, "created": time.time(), "updated": time.time(), "use_count": 0}
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(new_data, f, ensure_ascii=False, indent=2)
