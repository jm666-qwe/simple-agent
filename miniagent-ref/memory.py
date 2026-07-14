from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field

from miniagent.messages import Message, ToolResultBlock, message_text
from miniagent.utils.ids import new_id


MemoryScope = Literal["user", "project", "session"]

MEMORY_SCOPES: set[str] = {"user", "project", "session"}
DEFAULT_MEMORY_LIMIT = 5
TIME_DECAY_DAYS = 30.0


class MemoryItem(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mem"))
    scope: MemoryScope = "project"
    content: str
    tags: list[str] = Field(default_factory=list)
    importance: float = 1
    source: str = "manual"
    project: str | None = None
    session_id: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    expires_at: float | None = None
    last_used_at: float | None = None
    use_count: int = 0


class MemoryQuery(BaseModel):
    query: str = ""
    tags: list[str] = Field(default_factory=list)
    scope: MemoryScope | None = None
    project: str | None = None
    session_id: str | None = None
    limit: int = Field(default=DEFAULT_MEMORY_LIMIT, ge=1, le=200)
    include_expired: bool = False
    now: float = Field(default_factory=time.time)


class MemoryRecallHit(BaseModel):
    item: MemoryItem
    score: float
    matched_keywords: list[str] = Field(default_factory=list)
    matched_tags: list[str] = Field(default_factory=list)
    keyword_score: float = 0
    tag_score: float = 0
    recency_score: float = 0
    importance_score: float = 0
    scope_score: float = 0
    reason: str = ""


class MemoryCandidate(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mcand"))
    content: str
    scope: MemoryScope = "project"
    tags: list[str] = Field(default_factory=list)
    importance: float = 1
    source: str
    source_message_ids: list[str] = Field(default_factory=list)
    reason: str
    created_at: float = Field(default_factory=time.time)


class MemoryStore:
    def __init__(self, path: str | Path):
        raw_path = Path(path)
        self.path = raw_path if raw_path.suffix.lower() == ".json" else raw_path / "memory.json"

    def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        importance: float = 1,
        *,
        scope: MemoryScope = "project",
        project: str | None = None,
        session_id: str | None = None,
        source: str = "manual",
        expires_at: float | None = None,
    ) -> MemoryItem:
        item = MemoryItem(
            content=content,
            tags=_normalize_tags(tags or []),
            importance=importance,
            scope=scope,
            project=project if scope == "project" else None,
            session_id=session_id if scope == "session" else None,
            source=source,
            expires_at=expires_at,
        )
        items = self._load()
        items.append(item)
        self._save(items)
        return item

    def update(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        tags: list[str] | None = None,
        importance: float | None = None,
        scope: MemoryScope | None = None,
        project: str | None = None,
        session_id: str | None = None,
        source: str | None = None,
        expires_at: float | None = None,
    ) -> MemoryItem | None:
        items = self._load()
        for index, item in enumerate(items):
            if item.id != memory_id:
                continue
            if content is not None:
                item.content = content
            if tags is not None:
                item.tags = _normalize_tags(tags)
            if importance is not None:
                item.importance = importance
            if scope is not None:
                item.scope = scope
            if project is not None or item.scope != "project":
                item.project = project if item.scope == "project" else None
            if session_id is not None or item.scope != "session":
                item.session_id = session_id if item.scope == "session" else None
            if source is not None:
                item.source = source
            if expires_at is not None:
                item.expires_at = expires_at
            item.updated_at = time.time()
            items[index] = item
            self._save(items)
            return item
        return None

    def forget(self, memory_id: str) -> bool:
        items = self._load()
        kept = [item for item in items if item.id != memory_id]
        self._save(kept)
        return len(kept) != len(items)

    def delete(self, memory_id: str) -> bool:
        return self.forget(memory_id)

    def list_memories(
        self,
        *,
        scope: MemoryScope | None = None,
        project: str | None = None,
        session_id: str | None = None,
        include_expired: bool = False,
    ) -> list[MemoryItem]:
        now = time.time()
        items = [
            item
            for item in self._load()
            if self._is_visible(
                item,
                scope=scope,
                project=project,
                session_id=session_id,
                include_expired=include_expired,
                now=now,
            )
        ]
        return sorted(items, key=lambda item: (item.updated_at, item.importance), reverse=True)

    def recall(
        self,
        query: str = "",
        limit: int = DEFAULT_MEMORY_LIMIT,
        *,
        tags: list[str] | None = None,
        scope: MemoryScope | None = None,
        project: str | None = None,
        session_id: str | None = None,
    ) -> list[MemoryItem]:
        return [
            hit.item
            for hit in self.recall_hits(
                query=query,
                limit=limit,
                tags=tags,
                scope=scope,
                project=project,
                session_id=session_id,
            )
        ]

    def recall_hits(
        self,
        query: str = "",
        limit: int = DEFAULT_MEMORY_LIMIT,
        *,
        tags: list[str] | None = None,
        scope: MemoryScope | None = None,
        project: str | None = None,
        session_id: str | None = None,
        include_expired: bool = False,
        now: float | None = None,
    ) -> list[MemoryRecallHit]:
        memory_query = MemoryQuery(
            query=query,
            tags=_normalize_tags(tags or []),
            scope=scope,
            project=project,
            session_id=session_id,
            limit=limit,
            include_expired=include_expired,
            now=now or time.time(),
        )
        keywords = _keywords(memory_query.query)
        hits: list[MemoryRecallHit] = []
        has_filter = bool(keywords or memory_query.tags)
        for item in self._load():
            if not self._is_visible(
                item,
                scope=memory_query.scope,
                project=memory_query.project,
                session_id=memory_query.session_id,
                include_expired=memory_query.include_expired,
                now=memory_query.now,
            ):
                continue
            hit = self._score_item(item, keywords=keywords, query=memory_query)
            if has_filter and not hit.matched_keywords and not hit.matched_tags:
                continue
            hits.append(hit)
        return sorted(
            hits,
            key=lambda hit: (hit.score, hit.item.updated_at),
            reverse=True,
        )[: memory_query.limit]

    def _score_item(
        self,
        item: MemoryItem,
        *,
        keywords: list[str],
        query: MemoryQuery,
    ) -> MemoryRecallHit:
        content = item.content.lower()
        tags = _normalize_tags(item.tags)
        haystack = " ".join([content, *tags])
        matched_keywords = [keyword for keyword in keywords if keyword in haystack]
        matched_tags = [tag for tag in query.tags if tag in tags]
        keyword_score = float(len(matched_keywords) * 2)
        tag_score = float(len(matched_tags) * 3)
        recency_score = _recency_score(item.updated_at, query.now)
        importance_score = max(0.0, float(item.importance)) * 0.35
        scope_score = {"session": 0.6, "project": 0.4, "user": 0.2}.get(item.scope, 0.0)
        score = keyword_score + tag_score + recency_score + importance_score + scope_score
        reason = _explain_recall(
            item=item,
            matched_keywords=matched_keywords,
            matched_tags=matched_tags,
            recency_score=recency_score,
            score=score,
        )
        return MemoryRecallHit(
            item=item,
            score=round(score, 4),
            matched_keywords=matched_keywords,
            matched_tags=matched_tags,
            keyword_score=keyword_score,
            tag_score=tag_score,
            recency_score=round(recency_score, 4),
            importance_score=round(importance_score, 4),
            scope_score=scope_score,
            reason=reason,
        )

    @staticmethod
    def _is_visible(
        item: MemoryItem,
        *,
        scope: MemoryScope | None,
        project: str | None,
        session_id: str | None,
        include_expired: bool,
        now: float,
    ) -> bool:
        if scope is not None and item.scope != scope:
            return False
        if not include_expired and item.expires_at is not None and item.expires_at <= now:
            return False
        if item.scope == "project" and project and item.project not in (None, project):
            return False
        if item.scope == "session" and session_id and item.session_id not in (None, session_id):
            return False
        return True

    def _load(self) -> list[MemoryItem]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [MemoryItem.model_validate(item) for item in raw]

    def _save(self, items: list[MemoryItem]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                [item.model_dump(mode="json") for item in items],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def default_memory_path(*, data_dir: str | Path | None = None, cwd: str | Path | None = None) -> Path:
    if data_dir is not None:
        return Path(data_dir) / "memory.json"
    if cwd is not None:
        return Path(cwd) / ".miniagent" / "memory.json"
    return Path(".miniagent") / "memory.json"


def build_session_memory_candidates(
    messages: list[Message],
    *,
    session_id: str,
    limit: int = 3,
) -> list[MemoryCandidate]:
    candidates: list[MemoryCandidate] = []
    for message in reversed(messages):
        if message.role != "user":
            continue
        if any(isinstance(block, ToolResultBlock) for block in message.content):
            continue
        text = message_text(message).strip()
        if not text or not _looks_memorable(text):
            continue
        candidates.append(
            MemoryCandidate(
                content=_clean_candidate_content(text),
                tags=["candidate"],
                source=f"session:{session_id}",
                source_message_ids=[message.id],
                reason="用户表达了偏好、规则或希望跨会话保留的信息。",
            )
        )
        if len(candidates) >= limit:
            break
    return list(reversed(candidates))


def normalize_scope(scope: str | None) -> MemoryScope | None:
    if scope is None or scope == "" or scope == "all":
        return None
    lowered = scope.lower()
    if lowered not in MEMORY_SCOPES:
        raise ValueError(f"未知记忆层级：{scope}")
    return cast(MemoryScope, lowered)


def _normalize_tags(tags: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        item = str(tag).strip().lower()
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def _keywords(query: str) -> list[str]:
    words = [word.lower() for word in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", query)]
    if words:
        return words
    stripped = query.strip().lower()
    return [stripped] if stripped else []


def _recency_score(updated_at: float, now: float) -> float:
    age_days = max(0.0, (now - updated_at) / 86400.0)
    return 1.0 / (1.0 + age_days / TIME_DECAY_DAYS)


def _explain_recall(
    *,
    item: MemoryItem,
    matched_keywords: list[str],
    matched_tags: list[str],
    recency_score: float,
    score: float,
) -> str:
    reasons = [
        f"scope={item.scope}",
        f"importance={item.importance:g}",
        f"recency={recency_score:.2f}",
    ]
    if matched_keywords:
        reasons.append("keyword=" + ",".join(matched_keywords))
    if matched_tags:
        reasons.append("tag=" + ",".join(matched_tags))
    reasons.append(f"score={score:.2f}")
    return "; ".join(reasons)


def _looks_memorable(text: str) -> bool:
    lowered = text.lower()
    hints = ("记住", "偏好", "规则", "以后", "下次", "remember", "prefer", "always")
    return any(hint in lowered for hint in hints)


def _clean_candidate_content(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[:240]
