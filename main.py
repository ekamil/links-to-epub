# --- Full updated FastAPI app with docling HTML conversion and RSS excerpt ---
import re
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

    @property
    def feed_md(self) -> Path:
        return self.data_dir / "feed.md"

    @property
    def feed_epub(self) -> Path:
        return self.data_dir / "feed.epub"

    @property
    def feed_atom(self) -> Path:
        return self.data_dir / "feed.atom"

    @property
    def feed_rss(self) -> Path:
        return self.data_dir / "feed.rss"


settings = Settings()


# endregion


# region models
class SubmitRequest(BaseModel):
    url: str
    title: str | None = None


class RssEntry(BaseModel):
    id: str
    title: str
    original_link: str
    content: str

    @property
    def excerpt(self):
        return self.content


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


# region conversion


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


def convert_to_epub(inputfile: Path) -> Path:
    success = convertext.convert(
        inputfile,
        "epub",
        output=str(settings.data_dir),
        keep_intermediate=False,
        overwrite=True,
    )
    logger.info(
        f"Conversion for {inputfile} result: {success}. Target {inputfile.stem}.epub"
    )
    return settings.data_dir / f"{inputfile.stem}.epub"


def md5sum(url: str) -> str:
    return md5(url.encode("utf-8")).hexdigest()


# endregion


def update_rss_state(request: SubmitRequest, request_id: str) -> RssState:
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
    content = (settings.data_dir / f"{request_id}.html").read_text()
    original_link = request.url
    entry = RssEntry(
        id=request_id,
        title=request.title,
        original_link=original_link,
        # description doesnt work
        content=content,
    )
    state.add_entry(entry)

    # 3. Zaktualizuj czas
    state.updated = datetime.now(UTC)

    # 4. Zapisz stan do JSON
    with open(settings.rss_state_path, "w") as f:
        f.write(state.model_dump_json(indent=2))

    return state


def _state_to_feed(state: RssState, fmt: Literal["rss", "atom"]) -> FeedGenerator:
    with open(settings.rss_state_path, "r") as f:
        state = RssState.model_validate_json(f.read())
    fg = FeedGenerator()
    fg.id("EPUB Downloads Feed")
    fg.title(f"EPUB Downloads Feed {fmt}")
    fg.link(href=f"{settings.base_url}/{fmt}", rel="self")
    fg.description("Auto-generated feed of processed documents")
    fg.lastBuildDate(state.updated)

    for e in state.entries:
        fe = fg.add_entry()
        fe.id(e.id)
        fe.title(e.title)
        fe.link(href=e.original_link, rel="alternate")
        fe.content(e.content)
        fe.summary(e.excerpt)
    return fg


def enforce_min_heading_level(md: str, min_level: int = 2) -> str:
    """
    Wymusza minimalny poziom nagłówków Markdown (#..######).
    - Nagłówki o poziomie < min_level zostaną podniesione do min_level.
    - Nagłówki o poziomie >= min_level pozostają bez zmian.
    - Poziom nie przekroczy 6 (H6).

    Parametry:
        md: wejściowy tekst Markdown
        min_level: minimalny dozwolony poziom nagłówka (1..6)

    Zwraca:
        Zmieniony tekst Markdown.
    """
    if not (1 <= min_level <= 6):
        raise ValueError("min_level musi być w zakresie 1..6")

    heading_re = re.compile(r"^(#{1,6})([ \t]+)(.*\S.*)$", flags=re.M)

    def repl(m: re.Match) -> str:
        hashes, space, rest = m.group(1), m.group(2), m.group(3)
        level = len(hashes)
        if level < min_level:
            level = min_level
        level = min(level, 6)
        return "#" * level + space + rest

    return heading_re.sub(repl, md)


def merge_markdowns_into_epub(state: RssState) -> None:
    """Generate a single EPUB file from all the markdown files.
    This is kind of a feed like RSS.
    """
    # Sformatuj markdown
    merged_markdowns = [
        "# EPUB Downloads Feed\n\n",
        f"Last updated: {state.updated.isoformat()}.\n\n",
    ]
    for e in reversed(state.entries):  # oldest first
        merged_markdowns.append(f"# {e.title}\n")
        merged_markdowns.append(f"Published on {e.title}.\n")
        merged_markdowns.append(
            f"Source link: [{e.original_link}]({e.original_link}).\n"
        )
        content = (settings.data_dir / f"{e.id}.md").read_text()
        content = enforce_min_heading_level(content, 2)
        merged_markdowns.append(content)
        merged_markdowns.append("\n---\n\n")
    with open(settings.feed_md, "w") as f:
        f.writelines(merged_markdowns)
    # Zapisz jako epub
    convert_to_epub(settings.feed_md)


def refresh_feeds(state: RssState) -> None:
    # create a single epub
    merge_markdowns_into_epub(state)

    # create feed files
    feed = _state_to_feed(state, "rss")
    feed.rss_file(settings.feed_rss)
    feed = _state_to_feed(state, "atom")
    feed.atom_file(settings.feed_atom)


# region endpoints


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

    # MD
    markdown: Path = settings.data_dir / f"{request_id}.md"
    document.save_as_markdown(markdown)

    # Update RSS state
    state = update_rss_state(req, request_id)
    refresh_feeds(state)

    # and respond
    return {"id": request_id, "url": req.url, "title": req.title}


def read_state_or_404() -> RssState:
    if not settings.rss_state_path.exists():
        raise HTTPException(404)
    with open(settings.rss_state_path, "r") as f:
        state = RssState.model_validate_json(f.read())
    return state


@app.get("/")
def list_entries():
    state = read_state_or_404()
    for entry in state.entries:
        entry.content = "-"
    return state


@app.delete("/")
def clear():
    # TODO
    return Response(status_code=204)


@app.post("/refresh-feeds")
def clear_feeds():
    state = read_state_or_404()
    settings.feed_md.unlink(missing_ok=True)
    settings.feed_rss.unlink(missing_ok=True)
    settings.feed_atom.unlink(missing_ok=True)
    refresh_feeds(state)
    return Response(status_code=204)


@app.get("/feed/{fmt}")
def rss(fmt: Literal["rss", "atom", "epub"]):
    state = read_state_or_404()

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
            feed = _state_to_feed(state, fmt)
            content = feed.rss_str(pretty=True)
            headers["Content-Type"] = "application/rss+xml; charset=utf-8"
        case "atom":
            feed = _state_to_feed(state, fmt)
            content = feed.atom_str(pretty=True)
            headers["Content-Type"] = "application/atom+xml"
        case "epub":
            feed = _state_to_feed(state, fmt)
            content = feed.atom_str(pretty=True)
            headers["Content-Type"] = "application/epub+zip"
            headers["Content-Disposition"] = 'inline; filename="feed.xml"'
        case _:
            raise HTTPException(400, f"Unknown format: {fmt}")
    return Response(
        content,
        headers=headers,
        status_code=200,
    )


# endregion
