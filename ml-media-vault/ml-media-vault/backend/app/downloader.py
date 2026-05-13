"""Baixa imagens e vídeos para a pasta de mídias e devolve o caminho local + metadados."""
import base64
import json
import os
import re
import subprocess
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
    """Decodifica e salva imagens embutidas em data: URLs (base64)."""
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

    if is_placeholder(url):
        raise ValueError(f"URL é um placeholder de lazy-load, ignorado: {url[:80]}")

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


def _probe_video_dimensions(file_path: Path) -> tuple[int | None, int | None]:
    """Usa ffprobe para extrair largura/altura do vídeo."""
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if probe.returncode == 0:
            streams = json.loads(probe.stdout).get("streams", [])
            for s in streams:
                if s.get("codec_type") == "video":
                    return s.get("width"), s.get("height")
    except Exception as e:
        logger.warning("ffprobe falhou em %s: %s", file_path, e)
    return None, None


def download_video(url: str, dest_dir: Path, index: int) -> dict:
    """
    Baixa um vídeo usando yt-dlp.
    Funciona para YouTube, MP4 direto, HLS (.m3u8) e outros formatos.
    Qualidade máxima: 720p para equilibrar tamanho e fidelidade.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Template de saída: ex 007_video_dQw4w9WgXcQ.mp4
    out_template = str(dest_dir / f"{index:03d}_video_%(id)s.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        # Prefere mp4 até 720p; se não houver mp4, aceita qualquer container até 720p
        "-f", (
            "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]"
            "/best[height<=720][ext=mp4]"
            "/best[height<=720]"
            "/best"
        ),
        "--merge-output-format", "mp4",
        # Não mostrar barra de progresso (saída limpa nos logs)
        "--no-progress",
        "--quiet",
        "--print", "after_move:filepath",   # imprime o caminho final do arquivo
        "-o", out_template,
        url,
    ]

    logger.info("yt-dlp iniciando download: %s", url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise RuntimeError("yt-dlp ultrapassou o tempo limite de 5 minutos")

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "erro desconhecido").strip()
        raise RuntimeError(f"yt-dlp falhou (código {result.returncode}): {err[:600]}")

    # Resolve o caminho do arquivo baixado
    printed_path = result.stdout.strip()
    if printed_path and Path(printed_path).exists():
        file_path = Path(printed_path)
    else:
        # Fallback: busca o arquivo mais recente que bate com o padrão
        candidates = sorted(dest_dir.glob(f"{index:03d}_video_*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            raise RuntimeError(
                f"yt-dlp terminou sem erro mas o arquivo não foi encontrado em {dest_dir}. "
                f"Saída: {result.stdout[:200]}"
            )
        file_path = candidates[0]

    file_size = file_path.stat().st_size
    width, height = _probe_video_dimensions(file_path)

    logger.info("Vídeo baixado: %s (%.1f MB)", file_path.name, file_size / 1_048_576)

    return {
        "local_path": str(file_path.relative_to(MEDIA_DIR)),
        "file_size": file_size,
        "width": width,
        "height": height,
    }


def download_youtube_thumbnail(video_id: str, dest_dir: Path, index: int) -> dict | None:
    """
    Fallback: baixa só a thumbnail do YouTube quando o download do vídeo falha.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    thumb_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    try:
        return download_image(thumb_url, dest_dir, index)
    except Exception:
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