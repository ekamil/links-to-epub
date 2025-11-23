# --- Full updated FastAPI app with docling HTML conversion and RSS excerpt ---
import json
import subprocess
import uuid
from datetime import datetime, UTC
from pathlib import Path

import bleach
from docling.document_converter import DocumentConverter
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from feedgen.feed import FeedGenerator
from pydantic import BaseModel, DirectoryPath, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Directories
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="LTE_", extra="ignore"
    )

    # Paths
    epub_dir: DirectoryPath
    rss_path: Path
    rss_state_path: Path

    # App/base
    base_url: str = "http://localhost"

    # Excerpt
    excerpt_limit: int = 200
    allowed_tags: list[str] = ["p", "a", "strong", "em", "ul", "li", "br"]

    # External tools
    convertext_bin: str = "convertext"

    @model_validator(mode="after")
    def validate_epub_dir(self) -> "Settings":
        try:
            self.epub_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f"EPUB directory error: {e}")
        return self


settings = Settings()

# Allowed HTML tags for excerpt
ALLOWED_TAGS = settings.allowed_tags

app = FastAPI()


# region conversion: html from docling


def convert_url_to_html(url: str) -> str:
    converter = DocumentConverter()
    doc = converter.convert(url).document
    return doc.export_to_html()


# endregion


# region excerpt


def html_excerpt(raw_html: str, limit: int | None = None) -> str:
    if limit is None:
        limit = settings.excerpt_limit
    cleaned = bleach.clean(raw_html, tags=ALLOWED_TAGS, strip=True)
    excerpt = cleaned[:limit]
    return excerpt + "..." if len(cleaned) > limit else excerpt


# endregion


# region epub conversion


def convert_markdown_to_epub(text: str, output_path: Path):
    cmd = [settings.convertext_bin, "-i", "/dev/stdin", "-o", str(output_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    proc.communicate(text.encode("utf-8"))
    if proc.returncode != 0:
        raise RuntimeError("convertext failed")


# endregion


# region rss


def update_rss(title: str, original_url: str, epub_url: str, excerpt: str):
    """Update RSS feed using JSON state file and generate RSS from scratch."""

    # 1. Wczytaj stan z pliku, jeśli istnieje
    if settings.rss_state_path.exists():
        try:
            with open(settings.rss_state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {"updated": "", "entries": []}
    else:
        state = {"updated": "", "entries": []}

    # 2. Dodaj nowe entry
    entry = {
        "id": str(uuid.uuid4()),
        "title": title,
        "link": epub_url,
        "description": f"<![CDATA[{excerpt}<br><br>Source: <a href='{original_url}'>{original_url}</a>]]>",
        "content": excerpt,
    }
    state["entries"].insert(0, entry)  # najnowsze na górze

    # 3. Zaktualizuj czas
    _now = datetime.now(UTC)
    state["updated"] = _now.isoformat()

    # 4. Zapisz stan do JSON
    with open(settings.rss_state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    # 5. Generuj RSS od zera
    fg = FeedGenerator()
    fg.title("EPUB Downloads Feed")
    fg.link(href="http://localhost/rss.xml", rel="self")
    fg.description("Auto-generated feed of processed documents")
    fg.lastBuildDate(_now)

    for e in state["entries"]:
        fe = fg.add_entry()
        fe.id(e["id"])
        fe.title(e["title"])
        fe.link(href=e["link"])
        fe.description(e["description"])

    # 6. Zapisz feed XML
    fg.rss_file(str(settings.rss_state_path), pretty=True)


# endregion


# region models
class SubmitRequest(BaseModel):
    url: str
    title: str


# endregion


# region endpoints
@app.post("/submit")
def submit(req: SubmitRequest):
    request_id = str(uuid.uuid4())

    try:
        # URL -> HTML via docling
        html_text = convert_url_to_html(req.url)
    except Exception as e:
        raise HTTPException(500, f"Docling error: {e}")

    excerpt = html_excerpt(html_text)

    safe_name = f"{request_id}.epub"
    epub_path = settings.epub_dir / safe_name

    try:
        convert_markdown_to_epub(html_text, epub_path)
    except Exception as e:
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


@app.get("/rss.xml")
def rss():
    if not settings.rss_path.exists():
        raise HTTPException(404)
    return FileResponse(settings.rss_path)


# endregion
