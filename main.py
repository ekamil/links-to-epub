# --- Full updated FastAPI app with docling HTML conversion and RSS excerpt ---
import uuid
from datetime import datetime, UTC
from email.utils import format_datetime
from pathlib import Path
from typing import Literal

import bleach
import convertext
from docling.document_converter import DocumentConverter
from docling_core.types import DoclingDocument
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from feedgen.feed import FeedGenerator
from loguru import logger
from pydantic import BaseModel, DirectoryPath, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.responses import Response


# region settings


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="LTE_", extra="ignore"
    )

    # Paths
    epub_dir: DirectoryPath
    rss_path: Path
    rss_state_path: Path

    # App/base
    base_url: str = "http://localhost:8000"

    # Excerpt
    excerpt_limit: int = 200
    allowed_tags: list[str] = ["p", "a", "strong", "em", "ul", "li", "br"]

    # External tools
    convertext_bin: str = "convertext"


settings = Settings()


# endregion


# region models
class SubmitRequest(BaseModel):
    url: str
    title: str | None = None


class RssEntry(BaseModel):
    id: str
    title: str
    epub_link: str
    original_link: str
    description: str
    content: str | None = None


class RssState(BaseModel):
    updated: datetime = Field(default_factory=lambda: datetime.now(UTC))
    entries: list[RssEntry] = Field(default_factory=list)

    def add_entry(self, entry: RssEntry):
        self.entries.insert(0, entry)  # newest on top


# endregion


app = FastAPI()


# region conversion: html from docling


def convert_url(url: str) -> DoclingDocument:
    converter = DocumentConverter()
    doc = converter.convert(url).document
    return doc


# endregion


# region excerpt


def html_excerpt(raw_html: str, limit: int | None = None) -> str:
    if limit is None:
        limit = settings.excerpt_limit
    cleaned = bleach.clean(raw_html, tags=set(settings.allowed_tags), strip=True)
    excerpt = cleaned[:limit]
    return excerpt + "..." if len(cleaned) > limit else excerpt


# endregion


# region epub conversion


def convert_to_epub(html: Path, epub: Path):
    success = convertext.convert(
        str(html), "epub", output=str(settings.epub_dir), keep_intermediate=True
    )
    if not success:
        raise RuntimeError("convertext failed")


# endregion


# region rss


def update_rss(title: str, original_url: str, epub_url: str, excerpt: str) -> None:
    """Update RSS feed using JSON state file and generate RSS from scratch."""

    # 1. Wczytaj stan z pliku, jeśli istnieje
    if settings.rss_state_path.is_file():
        try:
            with open(settings.rss_state_path, "r", encoding="utf-8") as f:
                state = RssState.model_validate_json(f.read())
        except Exception as e:
            raise RuntimeError(f"RSS state file error: {e}")
    else:
        state = RssState()

    # 2. Dodaj nowe entry
    entry = RssEntry(
        id=str(uuid.uuid4()),
        title=title,  # error
        epub_link=epub_url,
        original_link=original_url,
        # description doesnt work
        description=f"<![CDATA[{excerpt}<br><br>Source: <a href='{original_url}'>{original_url}</a>]]>",
        content=excerpt,
    )
    state.add_entry(entry)

    # 3. Zaktualizuj czas
    state.updated = datetime.now(UTC)

    # 4. Zapisz stan do JSON
    with open(settings.rss_state_path, "w", encoding="utf-8") as f:
        f.write(state.model_dump_json(indent=2))


def _state_to_feed(state: RssState) -> FeedGenerator:
    with open(settings.rss_state_path, "r", encoding="utf-8") as f:
        state = RssState.model_validate_json(f.read())
    fg = FeedGenerator()
    fg.id("EPUB Downloads Feed")
    fg.title("EPUB Downloads Feed")
    fg.link(href="http://localhost/rss.xml", rel="self")
    fg.description("Auto-generated feed of processed documents")
    fg.lastBuildDate(state.updated)

    for e in state.entries:
        fe = fg.add_entry()
        fe.id(e.id)
        fe.title(e.title)
        fe.link(href=e.epub_link, rel="enclosure", type="application/epub+zip")
        fe.link(href=e.original_link, rel="alternate")
        fe.description(e.description)
        fe.content(e.content)
    return fg


def rss_from_state(state: RssState) -> str:
    fg = _state_to_feed(state)
    return fg.rss_str(pretty=True)


def atom_from_state(state: RssState) -> str:
    fg = _state_to_feed(state)
    return fg.atom_str(pretty=True)


# endregion


# region endpoints
@app.post("/submit")
def submit(req: SubmitRequest):
    request_id = str(uuid.uuid4())

    try:
        # URL -> HTML via docling
        document = convert_url(req.url)
        html_text = document.export_to_html()
    except Exception as e:
        raise HTTPException(500, f"Docling error: {e}")
    if req.title is None:
        req.title = document.name

    excerpt = html_excerpt(html_text)

    html_path: Path = settings.epub_dir / f"{request_id}.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_text)

    safe_name = f"{request_id}.epub"
    epub_path = settings.epub_dir / safe_name

    try:
        convert_to_epub(html_path, epub_path)
    except Exception as e:
        logger.error(f"EPUB conversion failed: {e}")
        raise HTTPException(500, f"EPUB conversion failed: {e}")

    epub_url = f"/epub/{safe_name}"

    update_rss(req.title, req.url, epub_url, excerpt)

    return {"id": request_id, "epub": epub_url}


@app.get("/epub/{file}")
def get_epub(file: str):
    path = settings.epub_dir / file
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/feed/{fmt}")
def rss(fmt: Literal["rss", "atom"]):
    if not settings.rss_state_path.exists():
        raise HTTPException(404)
    with open(settings.rss_state_path, "r", encoding="utf-8") as f:
        state = RssState.model_validate_json(f.read())
        feed = _state_to_feed(state)

    last_modified = state.updated
    etag = str(state.updated)  # lub hash entries

    # Prepare headers
    headers = {
        "Cache-Control": "public, max-age=300",
        "Last-Modified": format_datetime(last_modified),
        "ETag": f'"{etag}"',
        "Content-Disposition": 'inline; filename="feed.xml"',
    }

    # Get content as a string
    match fmt:
        case "rss":
            content = feed.rss_str(pretty=True)
            headers["Content-Type"] = "application/rss+xml; charset=utf-8"
        case "atom":
            content = feed.atom_str(pretty=True)
            headers["Content-Type"] = "application/atom+xml"
        case _:
            raise HTTPException(400, f"Unknown format: {fmt}")
    return Response(
        content,
        headers=headers,
        status_code=200,
    )


# endregion
