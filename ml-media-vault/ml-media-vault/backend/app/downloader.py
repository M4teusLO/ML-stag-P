"""Baixa imagens e vídeos para a pasta de mídias e devolve o caminho local + metadados."""
import base64
import os
import re
import logging
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

import httpx
from PIL import Image
from slugify import slugify

logger = logging.getLogger(__name__)

MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "/data/media"))

# GIF 1×1 transparente e outros placeholders comuns do ML (lazy-load)
_PLACEHOLDER_PREFIXES = (
    "data:image/gif;base64,R0lGODlhAQABAI",  # GIF 1×1 transparente
    "data:image/gif;base64,R0lGODlhAQABAIA",
    "data:image/svg+xml",                      # SVG placeholder genérico
)


def is_placeholder(url: str) -> bool:
    """Retorna True se a URL for um placeholder de lazy-load sem valor real."""
    return url.startswith(_PLACEHOLDER_PREFIXES)


def _safe_filename(url: str, fallback_idx: int) -> str:
    """Gera um nome de arquivo seguro derivado da URL."""
    parsed = urlparse(url)
    base = os.path.basename(parsed.path) or f"file_{fallback_idx}"
    base = slugify(base, lowercase=True, separator="_", regex_pattern=r"[^a-z0-9_.\-]+")
    if not base or len(base) > 120:
        base = f"file_{fallback_idx}"
    return base


def _ensure_extension(filename: str, content_type: str | None) -> str:
    """Garante que o arquivo tem extensão, deduzindo do Content-Type quando falta."""
    if "." in filename:
        return filename
    if not content_type:
        return filename + ".bin"
    ct = content_type.split(";")[0].strip().lower()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
    }
    return filename + mapping.get(ct, ".bin")


def _handle_data_url(url: str, dest_dir: Path, index: int) -> dict:
    """
    Decodifica e salva imagens embutidas em data: URLs (base64).
    Formato esperado: data:[<mime>][;base64],<dados>
    """
    match = re.match(r"data:(?P<mime>[^;,]+)(?:;base64)?,(?P<data>.+)", url, re.DOTALL)
    if not match:
        raise ValueError(f"data: URL inválida ou não suportada: {url[:80]}")

    mime = match.group("mime").strip().lower()
    raw = base64.b64decode(match.group("data"))

    ext_map = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }
    ext = ext_map.get(mime, ".bin")
    dest_dir.mkdir(parents=True, exist_ok=True)
    full_path = dest_dir / f"{index:03d}_inline{ext}"
    full_path.write_bytes(raw)

    width = height = None
    try:
        with Image.open(full_path) as img:
            width, height = img.size
    except Exception as e:
        logger.warning("Pillow não conseguiu abrir %s: %s", full_path, e)

    return {
        "local_path": str(full_path.relative_to(MEDIA_DIR)),
        "file_size": len(raw),
        "width": width,
        "height": height,
    }


def download_image(url: str, dest_dir: Path, index: int) -> dict:
    """Baixa uma imagem e retorna metadados (path, size, dimensões)."""

    # Rejeita placeholders de lazy-load antes de qualquer I/O
    if is_placeholder(url):
        raise ValueError(f"URL é um placeholder de lazy-load, ignorado: {url[:80]}")

    # Trata imagens base64 embutidas (data: URLs reais)
    if url.startswith("data:"):
        logger.info("data: URL detectada no índice %d — decodificando localmente.", index)
        return _handle_data_url(url, dest_dir, index)

    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": os.getenv(
            "USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        ),
        "Referer": "https://www.mercadolivre.com.br/",
    }

    with httpx.Client(headers=headers, follow_redirects=True, timeout=60.0) as client:
        r = client.get(url)
        r.raise_for_status()
        content = r.content
        content_type = r.headers.get("content-type")

    filename = _safe_filename(url, index)
    filename = _ensure_extension(filename, content_type)
    full_path = dest_dir / f"{index:03d}_{filename}"
    full_path.write_bytes(content)

    width = height = None
    try:
        with Image.open(full_path) as img:
            width, height = img.size
    except Exception as e:
        logger.warning("Pillow não conseguiu abrir %s: %s", full_path, e)

    return {
        "local_path": str(full_path.relative_to(MEDIA_DIR)),
        "file_size": len(content),
        "width": width,
        "height": height,
    }


def download_youtube_thumbnail(video_id: str, dest_dir: Path, index: int) -> dict | None:
    """Para vídeo do YouTube, baixa só a thumbnail (vídeo em si fica linkado)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    thumb_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    try:
        return download_image(thumb_url, dest_dir, index)
    except Exception:
        # Fallback pra hqdefault que sempre existe
        try:
            return download_image(
                f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                dest_dir,
                index,
            )
        except Exception as e:
            logger.error("Não foi possível baixar thumb do YouTube %s: %s", video_id, e)
            return None


def listing_dir(listing_id: int, ml_id: str | None) -> Path:
    """Pasta destino para mídias de um anúncio - ex: /data/media/MLB1234567890/"""
    folder_name = ml_id if ml_id else f"listing_{listing_id}"
    return MEDIA_DIR / folder_name