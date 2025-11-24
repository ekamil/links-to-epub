# --- Full updated FastAPI app with docling HTML conversion and RSS excerpt ---
import shutil
from datetime import datetime, UTC
from email.utils import format_datetime
from hashlib import md5
from pathlib import Path
from typing import Literal
from urllib import request

import bs4
import convertext
from docling.document_converter import DocumentConverter
from docling_core.types import DoclingDocument
from docling_core.types.doc import ImageRefMode
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

    data_dir: DirectoryPath
    base_url: str = "http://localhost:8000"
    # Excerpt
    excerpt_limit: int = 200
    allowed_tags: list[str] = ["p", "a", "strong", "em", "ul", "li", "br"]

    @property
    def rss_state_path(self) -> Path:
        return self.data_dir / "state.json"


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
    content: str
    excerpt: str


class RssState(BaseModel):
    updated: datetime = Field(default_factory=lambda: datetime.now(UTC))
    entries: list[RssEntry] = Field(default_factory=list)

    def add_entry(self, entry: RssEntry):
        insert_at = 0
        for idx, e in enumerate(self.entries):
            if e.id == entry.id:
                logger.info(f"Duplicate entry {entry.id}, replacing")
                self.entries.pop(idx)
                insert_at = idx
        self.entries.insert(insert_at, entry)


# endregion


app = FastAPI()


# region conversion: html from docling


def convert_url(url: str) -> DoclingDocument:
    converter = DocumentConverter()
    doc = converter.convert(url).document
    return doc


def get_title(url: str) -> str:
    try:
        html = request.urlopen(url).read().decode("utf8")
        soup = bs4.BeautifulSoup(html, "html.parser")
        title = soup.find("title")
        return title.string
    except Exception as e:
        logger.error(f"Error fetching title: {e}")
        return "Untitled"


# endregion


# region epub conversion


def convert_to_epub(html: Path) -> Path:
    convertext.convert(
        html, "epub", output=str(settings.data_dir), keep_intermediate=False
    )
    return settings.data_dir / f"{html.stem}.epub"


def convert_to_txt(html: Path) -> Path:
    convertext.convert(
        html, "txt", output=str(settings.data_dir), keep_intermediate=False
    )
    return settings.data_dir / f"{html.stem}.txt"


def convert_to_html(html: Path) -> Path:
    convertext.convert(
        html, "html", output=str(settings.data_dir), keep_intermediate=False
    )
    return settings.data_dir / f"{html.stem}.html"


# endregion


# region rss


def update_rss(
    request: SubmitRequest, request_id: str, epub_url: str, excerpt: str
) -> None:
    """Update RSS feed using JSON state file and generate RSS from scratch."""

    # 1. Wczytaj stan z pliku, jeśli istnieje
    if settings.rss_state_path.is_file():
        try:
            with open(settings.rss_state_path, "r") as f:
                state = RssState.model_validate_json(f.read())
        except Exception as e:
            raise RuntimeError(f"RSS state file error: {e}")
    else:
        state = RssState()

    # 2. Dodaj nowe entry
    original_link = request.url
    entry = RssEntry(
        id=request_id,
        title=request.title,
        epub_link=epub_url,
        original_link=original_link,
        # description doesnt work
        content=f"<![CDATA[{excerpt}<br><br>Source: <a href='{original_link}'>{original_link}</a>]]>",
        excerpt=excerpt,
    )
    state.add_entry(entry)

    # 3. Zaktualizuj czas
    state.updated = datetime.now(UTC)

    # 4. Zapisz stan do JSON
    with open(settings.rss_state_path, "w") as f:
        f.write(state.model_dump_json(indent=2))


def _state_to_feed(state: RssState, format: Literal["rss", "atom"]) -> FeedGenerator:
    with open(settings.rss_state_path, "r") as f:
        state = RssState.model_validate_json(f.read())
    fg = FeedGenerator()
    fg.id("EPUB Downloads Feed")
    fg.title(f"EPUB Downloads Feed {format}")
    fg.link(href=f"{settings.base_url}/{format}", rel="self")
    fg.description("Auto-generated feed of processed documents")
    fg.lastBuildDate(state.updated)

    for e in state.entries:
        fe = fg.add_entry()
        fe.id(e.id)
        fe.title(e.title)
        fe.link(href=e.epub_link, rel="enclosure", type="application/epub+zip")
        fe.link(href=e.original_link, rel="alternate")
        fe.content(
            e.content,
        )
        fe.summary(e.excerpt)
    return fg


def rss_from_state(state: RssState) -> str:
    fg = _state_to_feed(state)
    return fg.rss_str(pretty=True)


def atom_from_state(state: RssState) -> str:
    fg = _state_to_feed(state)
    return fg.atom_str(pretty=True)


# endregion


# region endpoints
def md5sum(url: str) -> str:
    return md5(url.encode("utf-8")).hexdigest()


@app.post("/submit")
def submit(req: SubmitRequest):
    request_id = f"req-{md5sum(req.url)}"

    # URL -> HTML via docling
    document: DoclingDocument = convert_url(req.url)

    html: Path = settings.data_dir / f"{request_id}.html"
    document.save_as_html(html, image_mode=ImageRefMode.EMBEDDED)

    if not req.title:
        if document.name not in ("file", "Untitled"):
            req.title = document.name
        else:
            req.title = get_title(req.url)
    if not req.title:
        req.title = "Untitled"

    epub: Path = convert_to_epub(html)
    # txt: Path = convert_to_txt(html)
    shutil.copy(html, html.with_name(f"{request_id}.original.html"))

    # Excerpt
    markdown: Path = settings.data_dir / f"{request_id}.md"
    document.save_as_markdown(markdown)
    excerpt = document.export_to_markdown()[: settings.excerpt_limit]

    # Update RSS state
    epub_url = f"/epub/{epub.name}"
    update_rss(req, request_id, epub_url, excerpt)

    # and respond
    return {"id": request_id, "epub": epub_url, "url": req.url, "title": req.title}


@app.get("/epub/{file}")
def get_epub(file: str):
    path = settings.data_dir / file
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/feed/{fmt}")
def rss(fmt: Literal["rss", "atom"]):
    if not settings.rss_state_path.exists():
        raise HTTPException(404)
    with open(settings.rss_state_path, "r") as f:
        state = RssState.model_validate_json(f.read())
    feed = _state_to_feed(state, fmt)

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
