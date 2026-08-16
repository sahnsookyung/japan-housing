"""Small, dependency-free HTML helpers for untrusted public pages."""

from __future__ import annotations

import hashlib
import html as html_module
import re
from html.parser import HTMLParser
from urllib.parse import urljoin


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
        elif not self.skip_depth and tag in {
            "br",
            "p",
            "li",
            "tr",
            "td",
            "th",
            "div",
            "h1",
            "h2",
            "h3",
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and tag in {"p", "li", "tr", "div"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)


class _LinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        self._href = dict(attrs).get("href")
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            self.links.append(
                (
                    urljoin(self.base_url, html_module.unescape(self._href)),
                    clean_text(" ".join(self._parts)),
                )
            )
        if tag == "a":
            self._href = None
            self._parts = []


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html_module.unescape(value)).strip()


def visible_text(document: str) -> str:
    parser = _TextParser()
    parser.feed(document)
    return "\n".join(
        line for line in (clean_text(part) for part in "".join(parser.parts).splitlines()) if line
    )


def links(document: str, base_url: str) -> tuple[tuple[str, str], ...]:
    parser = _LinkParser(base_url)
    parser.feed(document)
    return tuple(parser.links)


def title(document: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", document, re.I | re.S)
    return clean_text(re.sub(r"<[^>]+>", " ", match.group(1))) if match else ""


def meta(document: str, name: str) -> str:
    escaped = re.escape(name)
    patterns = (
        rf'<meta[^>]+(?:name|property)=["\']{escaped}["\'][^>]+content=["\']([^"\']*)',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:name|property)=["\']{escaped}["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, document, re.I | re.S)
        if match:
            return clean_text(match.group(1))
    return ""


def evidence_hash(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8", "replace")).hexdigest()


def money(value: str) -> int | None:
    match = re.search(r"(?:¥|￥)?\s*([\d,]+)", value)
    if not match:
        return None
    amount = int(match.group(1).replace(",", ""))
    return amount if 0 <= amount <= 100_000_000 else None
