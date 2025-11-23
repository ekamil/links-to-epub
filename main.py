# --- Full updated FastAPI app with docling HTML conversion and RSS excerpt ---
import subprocess
import uuid
from pathlib import Path

import bleach
from docling.document_converter import DocumentConverter
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from feedgen.entry import FeedEntry
from feedgen.feed import FeedGenerator
from pydantic import BaseModel, DirectoryPath
from pydantic_settings import BaseSettings, SettingsConfigDict

# Directories
BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="LTE_", extra="ignore"
    )

    # Paths
    epub_dir: DirectoryPath = BASE_DIR / "epubs"
    rss_path: Path = BASE_DIR / "rss.xml"

    # App/base
    base_url: str = "http://localhost"

    # Excerpt
    excerpt_limit: int = 200
    allowed_tags: list[str] = ["p", "a", "strong", "em", "ul", "li", "br"]

    # External tools
    convertext_bin: str = "convertext"


settings = Settings()
EPUB_DIR = settings.epub_dir
RSS_PATH = settings.rss_path
EPUB_DIR.mkdir(exist_ok=True)

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
    fg = FeedGenerator()
    if RSS_PATH.exists():
        fg.load_extension("podcast")
        fg.parse(str(RSS_PATH))
    else:
        fg.id("urn:epubfeed")
        fg.title("EPUB Downloads Feed")
        fg.link(href=f"{settings.base_url}/rss.xml", rel="self")

    fe: FeedEntry = fg.add_entry()
    fe.id(str(uuid.uuid4()))
    fe.title(title)
    fe.link(href=epub_url)
    fe.description(
        f"<![CDATA[{excerpt}<br><br>"
        f"Source: <a href='{original_url}'>{original_url}</a>]]>"
    )

    fg.rss_str(pretty=True)
    fg.rss_file(str(RSS_PATH))


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
    epub_path = EPUB_DIR / safe_name

    try:
        convert_markdown_to_epub(html_text, epub_path)
    except Exception as e:
        raise HTTPException(500, f"EPUB conversion failed: {e}")

    epub_url = f"/epub/{safe_name}"

    update_rss(req.title, req.url, epub_url, excerpt)

    return {"id": request_id, "epub": epub_url}


@app.get("/epub/{file}")
def get_epub(file: str):
    path = EPUB_DIR / file
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/rss.xml")
def rss():
    if not RSS_PATH.exists():
        raise HTTPException(404)
    return FileResponse(RSS_PATH)


# endregion
