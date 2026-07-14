"""知识图谱 — 实体-关系提取 + HTML 可视化

用法:
- /graph                  → 生成 knowledge_graph.html 并用浏览器打开
- /graph extract          → 从记忆中提取实体关系
- /graph add 实体 关系 目标 → 手动添加三元组
"""

import json
import os
import re
import time
import html as _html_escape
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GRAPH_FILE = os.path.join(BASE_DIR, "knowledge_graph.json")
HTML_FILE = os.path.join(BASE_DIR, "knowledge_graph.html")


class KnowledgeGraph:
    def __init__(self):
        self.entities: dict[str, dict] = {}  # name -> {type, properties}
        self.relations: list[tuple] = []     # (from, relation, to)
        self._load()

    def add_entity(self, name: str, etype: str = "concept", **props):
        name = name.strip()
        if name not in self.entities:
            self.entities[name] = {"type": etype, "properties": props, "created": time.time()}
        else:
            self.entities[name]["properties"].update(props)

    def add_relation(self, from_entity: str, relation: str, to_entity: str):
        from_entity = from_entity.strip()
        to_entity = to_entity.strip()
        relation = relation.strip()
        # 确保实体存在
        if from_entity not in self.entities:
            self.add_entity(from_entity)
        if to_entity not in self.entities:
            self.add_entity(to_entity)
        # 去重
        triple = (from_entity, relation, to_entity)
        if triple not in self.relations:
            self.relations.append(triple)
        self._save()

    def extract_from_memory(self, memory):
        """从记忆数据中提取实体和关系"""
        mem = memory._load()
        for key, item in mem.items():
            value = item.get("value", "") if isinstance(item, dict) else str(item)
            tags = item.get("tags", []) if isinstance(item, dict) else []

            # 实体: key 作为实体名
            self.add_entity(key, "memory_item", value=value[:100])

            # 关系: 从 tags 推断
            for tag in tags:
                self.add_entity(tag, "tag")
                self.add_relation(key, "tagged_as", tag)

            # 简单实体提取: 数字、名称等
            numbers = re.findall(r"\d+岁|\d+年|\d+月|\d+个|\d+元|\d+万", value)
            for n in numbers:
                self.add_entity(n, "fact")
                self.add_relation(key, "has_value", n)

    def extract_with_llm(self, memory, model):
        """用 LLM 从记忆提取结构化三元组"""
        mem = memory._load()
        if not mem or not model:
            return

        mem_text = "\n".join(
            f"- {k}: {v.get('value', v) if isinstance(v, dict) else v}"
            for k, v in mem.items()
        )

        prompt = (
            "从以下记忆提取实体-关系三元组，每个一行，格式: 实体1 | 关系 | 实体2\n\n"
            f"记忆:\n{mem_text[:3000]}\n\n"
            "只输出三元组，每行一个，不要解释。例如:\n"
            "乔唯一 | 年龄 | 19岁\n"
            "乔唯一 | 正在学 | Python\n"
            "乔唯一 | 目标 | 大二实习"
        )

        try:
            text = model.chat_simple([{"role": "user", "content": prompt}], max_tokens=500)
            for line in text.strip().split("\n"):
                parts = line.strip().split("|")
                if len(parts) == 3:
                    a, r, b = parts[0].strip(), parts[1].strip(), parts[2].strip()
                    if a and r and b:
                        self.add_relation(a, r, b)
        except Exception as e:
            print(f"  [图谱] LLM 提取失败: {e}")

    def generate_html(self):
        """生成自包含 HTML 可视化（Mermaid 图）"""
        # 构建 Mermaid graph
        lines = ["graph TD"]
        entity_ids = {}
        colors = {
            "person": "#4A90D9", "skill": "#50B86C", "project": "#E8A838",
            "tag": "#9B59B6", "fact": "#E67E22", "concept": "#95A5A6",
            "memory_item": "#3498DB",
        }

        for i, (name, info) in enumerate(self.entities.items()):
            eid = f"E{i}"
            entity_ids[name] = eid
            etype = info.get("type", "concept")
            color = colors.get(etype, "#95A5A6")
            # 截断长名称 + HTML 转义
            label = name[:20] + "..." if len(name) > 20 else name
            safe_label = label.replace('"', '\\"')
            lines.append(f'    {eid}["{safe_label}"]')
            lines.append(f"    style {eid} fill:{color},stroke:#333,color:#fff")

        for from_e, rel, to_e in self.relations:
            fid = entity_ids.get(from_e)
            tid = entity_ids.get(to_e)
            if fid and tid:
                safe_rel = rel[:15].replace('"', '\\"')
                lines.append(f"    {fid} -->|{safe_rel}| {tid}")

        graph_code = "\n".join(lines)

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>知识图谱 — Simple Agent</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
  body {{ font-family: -apple-system, sans-serif; background: #1a1a2e; color: #eee; margin: 20px; }}
  h1 {{ color: #4A90D9; }}
  .graph {{ background: #16213e; border-radius: 12px; padding: 20px; margin: 10px 0; overflow-x: auto; }}
  .entities {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 10px; }}
  .entity {{ background: #16213e; border-radius: 8px; padding: 12px; border-left: 4px solid #4A90D9; }}
  .entity.person {{ border-color: #4A90D9; }}
  .entity.skill {{ border-color: #50B86C; }}
  .entity.project {{ border-color: #E8A838; }}
  .entity.fact {{ border-color: #E67E22; }}
  .entity h3 {{ margin: 0 0 5px; font-size: 14px; }}
  .entity p {{ margin: 0; font-size: 12px; color: #aaa; }}
  .stats {{ display: flex; gap: 20px; margin: 20px 0; }}
  .stat {{ background: #16213e; border-radius: 8px; padding: 15px 25px; text-align: center; }}
  .stat .num {{ font-size: 32px; font-weight: bold; color: #4A90D9; }}
  .stat .label {{ font-size: 12px; color: #888; }}
</style>
</head>
<body>
<h1>Knowledge Graph</h1>

<div class="stats">
  <div class="stat"><div class="num">{len(self.entities)}</div><div class="label">实体</div></div>
  <div class="stat"><div class="num">{len(self.relations)}</div><div class="label">关系</div></div>
</div>

<div class="graph">
  <h2>关系图谱</h2>
  <pre class="mermaid">
{graph_code}
  </pre>
</div>

<div class="graph">
  <h2>实体列表</h2>
  <div class="entities">
"""
        for name, info in self.entities.items():
            etype = info.get("type", "concept")
            props = info.get("properties", {})
            val = _html_escape.escape(props.get("value", "")[:50], quote=False)
            safe_name = _html_escape.escape(name[:30], quote=False)
            safe_etype = _html_escape.escape(etype, quote=False)
            html += f'    <div class="entity {safe_etype}"><h3>{safe_name}</h3><p>{val}</p></div>\n'

        html += """  </div>
</div>

<script>mermaid.initialize({{startOnLoad:true, theme:'dark'}});</script>
</body>
</html>"""

        with open(HTML_FILE, "w", encoding="utf-8") as f:
            f.write(html)
        return HTML_FILE

    def summary(self) -> str:
        return (
            f"实体: {len(self.entities)} | "
            f"关系: {len(self.relations)} | "
            f"HTML: {HTML_FILE}"
        )

    def _load(self):
        try:
            with open(GRAPH_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.entities = data.get("entities", {})
            self.relations = [tuple(r) for r in data.get("relations", [])]
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save(self):
        data = {
            "entities": self.entities,
            "relations": [list(r) for r in self.relations],
        }
        with open(GRAPH_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
