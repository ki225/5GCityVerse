#!/usr/bin/env python3
"""Build Obsidian notes from free5GC web sources.

The script crawls official free5GC pages, optionally imports LinkedIn captures
saved by a browser extension, asks an AI model to synthesize the material, and
writes Markdown into an Obsidian vault.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html.parser
import json
import os
import re
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_SOURCES = [
    "https://free5gc.org/",
    "https://free5gc.org/doc/",
]

LINKEDIN_SOURCE = "https://www.linkedin.com/company/free5gc/posts/?feedView=all"


@dataclass
class SourceDocument:
    title: str
    url: str
    text: str


class LinkParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self._title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.links.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data.strip())

    @property
    def title(self) -> str:
        return " ".join(part for part in self._title_parts if part).strip()


class TextExtractor(html.parser.HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer"}
    BLOCK_TAGS = {
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0 and tag in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif self._skip_depth == 0 and tag in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        cleaned = re.sub(r"\s+", " ", data).strip()
        if cleaned:
            self._parts.append(cleaned)

    def text(self) -> str:
        raw = " ".join(self._parts)
        lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
        compact = "\n".join(line for line in lines if line)
        compact = re.sub(r"\n{3,}", "\n\n", compact)
        return compact.strip()


def fetch_url(url: str, timeout: int) -> tuple[str, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 5GCityVerse free5GC note automation "
                "(contact: local-obisidian-script)"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        html = response.read().decode(charset, errors="replace")
    parser = LinkParser()
    parser.feed(html)
    return parser.title or url, html


def normalize_url(base_url: str, href: str) -> str | None:
    href = href.split("#", 1)[0].strip()
    if not href or href.startswith(("mailto:", "tel:", "javascript:")):
        return None
    absolute = urllib.parse.urljoin(base_url, href)
    parsed = urllib.parse.urlparse(absolute)
    if parsed.netloc != "free5gc.org":
        return None
    if re.search(r"\.(png|jpg|jpeg|gif|svg|pdf|zip|tar|gz)$", parsed.path, re.I):
        return None
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def extract_links(base_url: str, html: str) -> list[str]:
    parser = LinkParser()
    parser.feed(html)
    links: list[str] = []
    for href in parser.links:
        url = normalize_url(base_url, href)
        if not url:
            continue
        path = urllib.parse.urlparse(url).path.lower()
        if any(marker in path for marker in ("/doc", "/blog", "/tutorial", "/guide", "/history")):
            links.append(url)
    return sorted(set(links))


def html_to_text(html: str) -> str:
    extractor = TextExtractor()
    extractor.feed(html)
    return extractor.text()


def crawl_free5gc(seed_urls: list[str], limit: int, timeout: int) -> list[SourceDocument]:
    queue = list(seed_urls)
    seen: set[str] = set()
    documents: list[SourceDocument] = []

    while queue and len(seen) < limit:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            title, html = fetch_url(url, timeout)
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"[warn] failed to fetch {url}: {exc}", file=sys.stderr)
            continue
        text = html_to_text(html)
        if len(text) >= 250:
            documents.append(SourceDocument(title=title, url=url, text=text))
        for link in extract_links(url, html):
            if link not in seen and link not in queue and len(queue) + len(seen) < limit:
                queue.append(link)

    return documents


def read_linkedin_exports(path: Path | None) -> list[SourceDocument]:
    if not path:
        return []
    if not path.exists():
        raise FileNotFoundError(f"LinkedIn export path does not exist: {path}")

    documents: list[SourceDocument] = []
    for file_path in sorted(path.rglob("*")):
        if file_path.suffix.lower() not in {".md", ".txt", ".html"}:
            continue
        content = file_path.read_text(encoding="utf-8", errors="replace")
        text = html_to_text(content) if file_path.suffix.lower() == ".html" else content.strip()
        if not text:
            continue
        documents.append(
            SourceDocument(
                title=file_path.stem,
                url=f"{LINKEDIN_SOURCE}#capture={file_path.name}",
                text=text,
            )
        )
    return documents


def trim_source_text(documents: Iterable[SourceDocument], max_chars_per_source: int) -> str:
    blocks: list[str] = []
    for index, doc in enumerate(documents, start=1):
        text = doc.text[:max_chars_per_source]
        blocks.append(
            textwrap.dedent(
                f"""
                [Source {index}]
                Title: {doc.title}
                URL: {doc.url}
                Content:
                {text}
                """
            ).strip()
        )
    return "\n\n---\n\n".join(blocks)


def build_prompt(source_material: str) -> str:
    return textwrap.dedent(
        f"""
        你是 5G Core / free5GC 技術筆記整理助手。請只根據下方來源材料整理，不要杜撰。

        請輸出一份適合 Obsidian 的繁體中文 Markdown 筆記，主題是「free5GC 知識點整理」。
        需要包含：
        1. Executive Summary
        2. free5GC 定位與版本/社群動態
        3. 5GC / SBA / NF 架構知識點
        4. 部署與操作重點，特別標出 Kubernetes、Helm、Docker、Webconsole、UPF/GTP5G
        5. 對 5GCityVerse 專案可用的落地筆記
        6. 待追蹤問題
        7. Sources，列出使用到的 URL

        格式要求：
        - 使用清楚的 Markdown 標題。
        - 用 bullet points，但每點要有實質技術含義。
        - 每個重要結論後面標註來源 URL。
        - 若來源不足，明確寫「來源不足」。

        來源材料：
        {source_material}
        """
    ).strip()


def openai_response(prompt: str, model: str, timeout: int) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set. Use --no-ai to write raw extracted notes.")

    payload = {
        "model": model,
        "input": prompt,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))

    if data.get("output_text"):
        return data["output_text"].strip()

    parts: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                parts.append(content["text"])
    if not parts:
        raise RuntimeError(f"OpenAI response did not contain text: {json.dumps(data)[:500]}")
    return "\n".join(parts).strip()


def slugify(value: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", value, flags=re.UNICODE)
    return re.sub(r"-+", "-", value).strip("-").lower() or "free5gc-notes"


def markdown_frontmatter(title: str, sources: list[SourceDocument]) -> str:
    now = dt.datetime.now(dt.timezone.utc).astimezone()
    urls = "\n".join(f"  - {doc.url}" for doc in sources)
    return (
        "---\n"
        f'title: "{title}"\n'
        f'created: "{now.isoformat(timespec="seconds")}"\n'
        "tags:\n"
        "  - free5gc\n"
        "  - 5gc\n"
        "  - ai-notes\n"
        "sources:\n"
        f"{urls}\n"
        "---"
    )


def build_raw_note(documents: list[SourceDocument], max_chars_per_source: int) -> str:
    sections = ["# free5GC 原始擷取筆記", ""]
    for doc in documents:
        sections.extend(
            [
                f"## {doc.title}",
                "",
                f"Source: {doc.url}",
                "",
                doc.text[:max_chars_per_source],
                "",
            ]
        )
    return "\n".join(sections).strip()


def write_obsidian_note(
    vault_path: Path,
    output_folder: str,
    title: str,
    body: str,
    sources: list[SourceDocument],
) -> Path:
    target_dir = vault_path / output_folder
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    path = target_dir / f"{timestamp}-{slugify(title)}.md"
    content = f"{markdown_frontmatter(title, sources)}\n\n{body.strip()}\n"
    path.write_text(content, encoding="utf-8")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vault-path",
        default=os.environ.get("OBSIDIAN_VAULT_PATH"),
        help="Path to the Obsidian vault. Can also be set with OBSIDIAN_VAULT_PATH.",
    )
    parser.add_argument("--output-folder", default="Inbox/free5GC")
    parser.add_argument("--crawl-limit", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-chars-per-source", type=int, default=12000)
    parser.add_argument(
        "--linkedin-export-dir",
        type=Path,
        help="Folder containing LinkedIn captures exported by Obsidian Web Clipper or another extension.",
    )
    parser.add_argument(
        "--source-url",
        action="append",
        default=[],
        help="Additional free5gc.org source URL. Can be repeated.",
    )
    parser.add_argument(
        "--openai-model",
        default=os.environ.get("OPENAI_MODEL", "gpt-5.5"),
        help="OpenAI model for synthesis. Can also be set with OPENAI_MODEL.",
    )
    parser.add_argument("--no-ai", action="store_true", help="Write raw extracted content without AI synthesis.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.vault_path:
        print("error: --vault-path or OBSIDIAN_VAULT_PATH is required", file=sys.stderr)
        return 2

    vault_path = Path(args.vault_path).expanduser().resolve()
    if not vault_path.exists():
        print(f"error: vault path does not exist: {vault_path}", file=sys.stderr)
        return 2

    seeds = DEFAULT_SOURCES + args.source_url
    documents = crawl_free5gc(seeds, args.crawl_limit, args.timeout)
    documents.extend(read_linkedin_exports(args.linkedin_export_dir))

    if not documents:
        print("error: no source documents were collected", file=sys.stderr)
        return 1

    title = "free5GC 知識點整理"
    if args.no_ai:
        body = build_raw_note(documents, args.max_chars_per_source)
    else:
        source_material = trim_source_text(documents, args.max_chars_per_source)
        body = openai_response(build_prompt(source_material), args.openai_model, args.timeout)

    note_path = write_obsidian_note(vault_path, args.output_folder, title, body, documents)
    print(f"wrote {note_path}")
    print(f"collected {len(documents)} source document(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
