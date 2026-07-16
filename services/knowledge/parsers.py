from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from services.knowledge.config import KnowledgePolicy
from services.knowledge.contracts import FactKind, SourceKind


PARSER_VERSION = "1.0"


class KnowledgeParseError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"knowledge parser rejected source: {reason_code}")


@dataclass(frozen=True, slots=True)
class ParsedFragment:
    ordinal: int
    locator: str
    start_line: int | None
    end_line: int | None
    title: str | None
    content: str
    extraction_method: str = "deterministic-chunk"


@dataclass(frozen=True, slots=True)
class ExtractedFact:
    kind: FactKind
    key: str
    value: str
    extraction_method: str


_FACT_LINE = re.compile(
    r"(?im)^\s*(?P<kind>fact|decision|факт|решение)\s*:\s*"
    r"(?P<key>[A-Za-zА-Яа-я0-9_.:-]{1,128})\s*=\s*(?P<value>[^\r\n]{1,512})\s*$"
)


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFC", value).replace("\x00", "")
    return "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()


def _chunk_lines(text: str, policy: KnowledgePolicy, *, markdown: bool) -> list[ParsedFragment]:
    normalized_lines = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized_lines.splitlines()
    if not lines:
        return []
    fragments: list[ParsedFragment] = []
    buffer: list[str] = []
    start = 1
    title: str | None = None

    def publish(end_line: int) -> None:
        nonlocal buffer, start, title
        content = _normalize_text("\n".join(buffer))
        if content:
            ordinal = len(fragments)
            fragments.append(
                ParsedFragment(
                    ordinal=ordinal,
                    locator=f"lines:{start}-{end_line}",
                    start_line=start,
                    end_line=end_line,
                    title=title,
                    content=content,
                )
            )
        buffer = []
        title = None

    for line_no, line in enumerate(lines, start=1):
        heading = markdown and bool(re.match(r"^#{1,6}\s+\S", line))
        if len(line) > policy.max_fragment_chars:
            if buffer:
                publish(line_no - 1)
            for part_index, offset in enumerate(
                range(0, len(line), policy.max_fragment_chars), start=1
            ):
                content = _normalize_text(line[offset : offset + policy.max_fragment_chars])
                if content:
                    fragments.append(
                        ParsedFragment(
                            ordinal=len(fragments),
                            locator=f"lines:{line_no}-{line_no}#part:{part_index}",
                            start_line=line_no,
                            end_line=line_no,
                            title=(line.lstrip("#").strip()[:512] if heading and part_index == 1 else None),
                            content=content,
                        )
                    )
                if len(fragments) >= policy.max_fragments_per_source:
                    raise KnowledgeParseError("limit.fragments")
            buffer = []
            title = None
            continue
        projected = len("\n".join((*buffer, line)))
        if buffer and (heading or projected > policy.max_fragment_chars):
            publish(line_no - 1)
            start = line_no
        if not buffer:
            start = line_no
            if heading:
                title = line.lstrip("#").strip()[:512]
        buffer.append(line)
        if len(fragments) >= policy.max_fragments_per_source:
            raise KnowledgeParseError("limit.fragments")
    if buffer:
        publish(len(lines))
    return fragments


def _content_from_message(message: Any) -> str | None:
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    if isinstance(content, list):
        pieces = []
        for item in content:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                pieces.append(item["text"])
        return "\n".join(pieces) if pieces else None
    return None


def _append_message_fragments(
    fragments: list[ParsedFragment],
    *,
    locator: str,
    role: str,
    content: str,
    title: str | None,
    extraction_method: str,
    policy: KnowledgePolicy,
) -> None:
    prefix = f"[{role}] "
    part_size = max(1, policy.max_fragment_chars - len(prefix))
    parts = [content[offset : offset + part_size] for offset in range(0, len(content), part_size)]
    for part_index, part in enumerate(parts, start=1):
        part_locator = locator if len(parts) == 1 else f"{locator}#part:{part_index}"
        fragments.append(
            ParsedFragment(
                ordinal=len(fragments),
                locator=part_locator,
                start_line=None,
                end_line=None,
                title=title,
                content=f"{prefix}{part}",
                extraction_method=extraction_method,
            )
        )
        if len(fragments) > policy.max_fragments_per_source:
            raise KnowledgeParseError("limit.fragments")


def _parse_conversation_json(text: str, policy: KnowledgePolicy) -> list[ParsedFragment]:
    try:
        payload = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise KnowledgeParseError("format.malformed_json") from exc
    conversations = payload.get("conversations") if isinstance(payload, dict) else payload
    if not isinstance(conversations, list):
        raise KnowledgeParseError("format.unsupported_conversation_json")
    fragments: list[ParsedFragment] = []
    for conversation_index, conversation in enumerate(conversations):
        if not isinstance(conversation, dict) or not isinstance(conversation.get("messages"), list):
            raise KnowledgeParseError("format.unsupported_conversation_json")
        conversation_id = str(conversation.get("id", conversation_index))[:128]
        conversation_title = conversation.get("title")
        title = str(conversation_title)[:512] if conversation_title else None
        for message_index, message in enumerate(conversation["messages"]):
            content = _content_from_message(message)
            if content is None:
                continue
            normalized = _normalize_text(content)
            if not normalized:
                continue
            role = str(message.get("role", "unknown"))[:32] if isinstance(message, dict) else "unknown"
            _append_message_fragments(
                fragments,
                locator=f"conversation:{conversation_id}/message:{message_index}",
                role=role,
                content=normalized,
                title=title,
                extraction_method="conversation-message",
                policy=policy,
            )
    if not fragments:
        raise KnowledgeParseError("format.empty_conversation_export")
    return fragments


class _ConversationHTMLParser(HTMLParser):
    _VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }

    def __init__(self, policy: KnowledgePolicy) -> None:
        super().__init__(convert_charrefs=True)
        self.policy = policy
        self.fragments: list[ParsedFragment] = []
        self._capture: dict[str, str] | None = None
        self._parts: list[str] = []
        self._depth = 0
        self._tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "iframe", "object", "embed", "base"}:
            raise KnowledgeParseError("format.active_html")
        values = {key.casefold(): value or "" for key, value in attrs}
        if self._capture is None and "data-role" in values:
            if tag.casefold() in self._VOID_TAGS:
                raise KnowledgeParseError("format.malformed_html")
            self._capture = values
            self._parts = []
            self._depth = 1
            self._tags = [tag.casefold()]
        elif self._capture is not None and tag.casefold() not in self._VOID_TAGS:
            self._tags.append(tag.casefold())
            self._depth = len(self._tags)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._VOID_TAGS:
            return
        if self._capture is None:
            return
        if not self._tags or self._tags[-1] != tag.casefold():
            raise KnowledgeParseError("format.malformed_html")
        self._tags.pop()
        self._depth = len(self._tags)
        if self._tags:
            return
        content = _normalize_text("".join(self._parts))
        values = self._capture
        self._capture = None
        self._parts = []
        self._tags = []
        if not content:
            return
        conversation = values.get("data-conversation-id", "0")[:128]
        message = values.get("data-message-id", str(len(self.fragments)))[:128]
        role = values.get("data-role", "unknown")[:32]
        _append_message_fragments(
            self.fragments,
            locator=f"conversation:{conversation}/message:{message}",
            role=role,
            content=content,
            title=None,
            extraction_method="conversation-html-message",
            policy=self.policy,
        )

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._parts.append(data)


def _parse_conversation_html(text: str, policy: KnowledgePolicy) -> list[ParsedFragment]:
    parser = _ConversationHTMLParser(policy)
    try:
        parser.feed(text)
        parser.close()
    except KnowledgeParseError:
        raise
    except Exception as exc:
        raise KnowledgeParseError("format.malformed_html") from exc
    if parser._capture is not None or parser._depth != 0:
        raise KnowledgeParseError("format.malformed_html")
    if not parser.fragments:
        raise KnowledgeParseError("format.unsupported_conversation_html")
    return parser.fragments


def parse_source(payload: bytes, source_kind: SourceKind, policy: KnowledgePolicy) -> list[ParsedFragment]:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise KnowledgeParseError("format.invalid_utf8") from exc
    if source_kind is SourceKind.CONVERSATION_JSON:
        return _parse_conversation_json(text, policy)
    if source_kind is SourceKind.CONVERSATION_HTML:
        return _parse_conversation_html(text, policy)
    return _chunk_lines(text, policy, markdown=source_kind is SourceKind.MARKDOWN)


def extract_facts(fragment: ParsedFragment) -> list[ExtractedFact]:
    facts: list[ExtractedFact] = []
    for match in _FACT_LINE.finditer(fragment.content):
        kind = match.group("kind").casefold()
        facts.append(
            ExtractedFact(
                kind=FactKind.DECISION if kind in {"decision", "решение"} else FactKind.FACT,
                key=unicodedata.normalize("NFKC", match.group("key")).casefold(),
                value=_normalize_text(match.group("value"))[:512],
                extraction_method="explicit-key-value-v1",
            )
        )
    return facts
