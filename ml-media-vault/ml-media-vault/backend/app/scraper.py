"""
Scraper de páginas de anúncio do Mercado Livre.

Estratégia (em camadas, da mais robusta para fallback):
1. Tenta extrair o JSON de estado embutido (__PRELOADED_STATE__ / window.__PROPS__ / scripts inline)
2. Cai para BeautifulSoup procurando elementos da galeria
3. Como último recurso, usa Open Graph / JSON-LD

Retorna sempre um dicionário padronizado:
{
    "ml_id": "MLB1234567890",
    "url": "...",
    "title": "...",
    "price": 199.90,
    "description": "...",
    "seller_nickname": "...",
    "pictures": ["url1", "url2", ...],
    "videos": [{"type": "youtube"|"native", "url": "..."}, ...],
    "attributes": {...},
    "raw": {...}  # JSON bruto encontrado, para auditoria
}
"""
import re
import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}

ML_ID_REGEX = re.compile(r"(ML[ABMU])-?(\d{6,})")
PRELOADED_REGEX = re.compile(
    r"window\.__PRELOADED_STATE__\s*=\s*(\{.+?\});", re.DOTALL,
)
INITIAL_STATE_REGEX = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.+?\});", re.DOTALL,
)
PROPS_REGEX = re.compile(
    r"window\.__PROPS__\s*=\s*JSON\.parse\((['\"])(.+?)\1\)", re.DOTALL,
)

# Prefixos de URLs que são placeholders de lazy-load — nunca devem entrar na lista de mídias
_SKIP_URL_PREFIXES = (
    "data:image/gif;base64,R0lGODlhAQABAI",
    "data:image/gif;base64,R0lGODlhAQABAIA",
    "data:image/svg+xml",
    "data:",  # qualquer outro data: URL não vindo de base64 real
)


def _is_valid_picture_url(url: str) -> bool:
    """Retorna True apenas para URLs HTTP(S) reais de imagens de produto."""
    if not isinstance(url, str):
        return False
    if url.startswith(_SKIP_URL_PREFIXES):
        return False
    if not url.startswith(("http://", "https://")):
        return False
    return True


def _user_agent() -> str:
    import os
    return os.getenv(
        "USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    )


def fetch_html(url: str, timeout: float = 20.0) -> str:
    headers = {**DEFAULT_HEADERS, "User-Agent": _user_agent()}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


def extract_ml_id(url: str, html: str | None = None) -> str | None:
    """Extrai o MLB-XXXX da URL ou do HTML."""
    m = ML_ID_REGEX.search(url)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    if html:
        m = ML_ID_REGEX.search(html)
        if m:
            return f"{m.group(1)}{m.group(2)}"
    return None


def _upgrade_image_url(url: str) -> str:
    """
    Converte URL de imagem do ML pra maior resolução possível.
    Padrão: .../D_NQ_NP_2X_<code>-<suffix>.webp  ou .../D_NQ_NP_<code>-<suffix>.webp
    Vamos forçar o sufixo '-F' (full) e o prefixo '2X_'.
    """
    if not _is_valid_picture_url(url):
        return url
    if "mlstatic.com" not in url:
        return url

    # Substitui o sufixo de tamanho (-I, -O, -V, -B, -W etc.) por -F antes da extensão
    url = re.sub(r"-[A-Z](\.(webp|jpg|jpeg|png))$", r"-F\1", url, flags=re.IGNORECASE)

    # Garante o prefixo 2X_ no token D_NQ_NP_
    if "D_NQ_NP_2X_" not in url:
        url = url.replace("D_NQ_NP_", "D_NQ_NP_2X_")

    return url


def _walk_for_pictures(obj: Any, found_urls: list[str]) -> None:
    """Caminha recursivamente em estruturas JSON do ML procurando URLs de imagens de produto."""
    if isinstance(obj, dict):
        # Estruturas comuns do ML: 'pictures': [{'url': '...', 'secure_url': '...', 'id': '...'}, ...]
        if "pictures" in obj and isinstance(obj["pictures"], list):
            for pic in obj["pictures"]:
                if isinstance(pic, dict):
                    u = pic.get("secure_url") or pic.get("url") or pic.get("src")
                    # Filtra placeholders e URLs não-HTTP antes de adicionar
                    if u and _is_valid_picture_url(u) and "mlstatic.com" in u and u not in found_urls:
                        found_urls.append(u)
        for v in obj.values():
            _walk_for_pictures(v, found_urls)
    elif isinstance(obj, list):
        for v in obj:
            _walk_for_pictures(v, found_urls)


def _walk_for_videos(obj: Any, found: list[dict]) -> None:
    """Procura referências a vídeo no JSON."""
    if isinstance(obj, dict):
        # YouTube
        if "youtube_id" in obj and obj["youtube_id"]:
            yid = obj["youtube_id"]
            entry = {"type": "youtube", "url": f"https://www.youtube.com/watch?v={yid}", "id": yid}
            if entry not in found:
                found.append(entry)
        if "video_id" in obj and obj["video_id"]:
            vid = obj["video_id"]
            # Heurística: IDs do YouTube têm 11 chars
            if isinstance(vid, str) and len(vid) == 11:
                entry = {"type": "youtube", "url": f"https://www.youtube.com/watch?v={vid}", "id": vid}
                if entry not in found:
                    found.append(entry)
        # Vídeos nativos do ML costumam ter URLs em campos como 'video_url', 'source'
        for k in ("video_url", "source", "src"):
            v = obj.get(k)
            if isinstance(v, str) and (".mp4" in v or "mlstatic" in v and "video" in v.lower()):
                entry = {"type": "native", "url": v}
                if entry not in found:
                    found.append(entry)
        for v in obj.values():
            _walk_for_videos(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _walk_for_videos(v, found)


def _try_parse_state(html: str) -> dict | None:
    """Tenta achar o JSON de estado embutido pelo ML."""
    for rx in (PRELOADED_REGEX, INITIAL_STATE_REGEX):
        m = rx.search(html)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue

    m = PROPS_REGEX.search(html)
    if m:
        raw = m.group(2)
        # JSON.parse escapa aspas; reverte
        try:
            unescaped = raw.encode().decode("unicode_escape")
            return json.loads(unescaped)
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

    return None


def _parse_jsonld(soup: BeautifulSoup) -> dict | None:
    """JSON-LD costuma trazer name, image, offers.price."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "{}")
        except json.JSONDecodeError:
            continue
        # Pode ser lista
        candidates = data if isinstance(data, list) else [data]
        for c in candidates:
            if isinstance(c, dict) and c.get("@type") in ("Product", "Offer"):
                return c
    return None


def _parse_html_fallback(soup: BeautifulSoup) -> dict:
    """Extrai dados via HTML quando o JSON falha."""
    title = None
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)

    # Imagens da galeria — filtra placeholders e data: URLs
    pictures: list[str] = []
    for img in soup.select("figure.ui-pdp-gallery__figure img, img.ui-pdp-image"):
        src = img.get("data-zoom") or img.get("data-src") or img.get("src")
        if src and _is_valid_picture_url(src) and "mlstatic.com" in src and src not in pictures:
            pictures.append(src)

    # Open Graph como último recurso
    if not pictures:
        for meta in soup.select('meta[property="og:image"]'):
            content = meta.get("content")
            if content and _is_valid_picture_url(content) and content not in pictures:
                pictures.append(content)

    # Preço
    price = None
    price_meta = soup.find("meta", attrs={"itemprop": "price"})
    if price_meta and price_meta.get("content"):
        try:
            price = float(price_meta["content"])
        except ValueError:
            pass

    if not price:
        price_el = soup.select_one(".andes-money-amount__fraction")
        if price_el:
            try:
                price = float(price_el.get_text(strip=True).replace(".", "").replace(",", "."))
            except ValueError:
                pass

    return {"title": title, "pictures": pictures, "price": price}


def scrape(url: str) -> dict:
    """Função principal - dada uma URL de anúncio, devolve metadados + mídias."""
    parsed = urlparse(url)
    if "mercadoliv" not in parsed.netloc and "mercadolib" not in parsed.netloc:
        raise ValueError(f"URL não parece ser do Mercado Livre: {url}")

    html = fetch_html(url)
    soup = BeautifulSoup(html, "lxml")

    result: dict = {
        "url": url,
        "ml_id": extract_ml_id(url, html),
        "title": None,
        "price": None,
        "description": None,
        "seller_nickname": None,
        "pictures": [],
        "videos": [],
        "attributes": {},
        "raw": None,
    }

    # 1) JSON embutido
    state = _try_parse_state(html)
    if state:
        result["raw"] = state
        pics: list[str] = []
        _walk_for_pictures(state, pics)
        result["pictures"] = [_upgrade_image_url(u) for u in pics if _is_valid_picture_url(u)]

        vids: list[dict] = []
        _walk_for_videos(state, vids)
        result["videos"] = vids

        # tenta achar título/preço no state genericamente
        def _find_first(obj: Any, keys: tuple[str, ...]) -> Any:
            if isinstance(obj, dict):
                for k in keys:
                    if k in obj and obj[k] not in (None, ""):
                        return obj[k]
                for v in obj.values():
                    r = _find_first(v, keys)
                    if r is not None:
                        return r
            elif isinstance(obj, list):
                for v in obj:
                    r = _find_first(v, keys)
                    if r is not None:
                        return r
            return None

        result["title"] = _find_first(state, ("title", "name"))
        price_val = _find_first(state, ("price", "amount"))
        if isinstance(price_val, (int, float)):
            result["price"] = float(price_val)
        elif isinstance(price_val, dict):
            v = price_val.get("amount") or price_val.get("value")
            if isinstance(v, (int, float)):
                result["price"] = float(v)
        result["seller_nickname"] = _find_first(state, ("nickname", "seller_name"))

    # 2) JSON-LD
    if not result["title"] or not result["pictures"]:
        ld = _parse_jsonld(soup)
        if ld:
            result["title"] = result["title"] or ld.get("name")
            if not result["pictures"]:
                imgs = ld.get("image")
                if isinstance(imgs, str) and _is_valid_picture_url(imgs):
                    result["pictures"] = [imgs]
                elif isinstance(imgs, list):
                    result["pictures"] = [u for u in imgs if _is_valid_picture_url(u)]
            if not result["price"]:
                offers = ld.get("offers")
                if isinstance(offers, dict):
                    p = offers.get("price")
                    if p is not None:
                        try:
                            result["price"] = float(p)
                        except (TypeError, ValueError):
                            pass

    # 3) HTML fallback
    if not result["title"] or not result["pictures"]:
        fb = _parse_html_fallback(soup)
        result["title"] = result["title"] or fb["title"]
        if not result["pictures"]:
            result["pictures"] = [_upgrade_image_url(u) for u in fb["pictures"] if _is_valid_picture_url(u)]
        if not result["price"]:
            result["price"] = fb["price"]

    # Vídeos via HTML (iframes do YouTube)
    for iframe in soup.select('iframe[src*="youtube.com"], iframe[src*="youtu.be"]'):
        src = iframe.get("src", "")
        m = re.search(r"(?:embed/|v=|youtu\.be/)([A-Za-z0-9_-]{11})", src)
        if m:
            yid = m.group(1)
            entry = {"type": "youtube", "url": f"https://www.youtube.com/watch?v={yid}", "id": yid}
            if entry not in result["videos"]:
                result["videos"].append(entry)

    # Descrição
    desc_el = soup.select_one(".ui-pdp-description__content")
    if desc_el:
        result["description"] = desc_el.get_text("\n", strip=True)

    # Dedup imagens — garantia final de que nenhum data: URL escapou
    seen: set[str] = set()
    deduped: list[str] = []
    for u in result["pictures"]:
        if _is_valid_picture_url(u) and u not in seen:
            seen.add(u)
            deduped.append(u)
    result["pictures"] = deduped

    if not result["pictures"] and not result["videos"]:
        logger.warning("Nenhuma mídia encontrada para %s", url)

    return result