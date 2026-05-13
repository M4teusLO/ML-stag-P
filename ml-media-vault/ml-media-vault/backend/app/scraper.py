"""
Scraper híbrido — duas estratégias em ordem de prioridade:

1) API PÚBLICA OFICIAL do Mercado Livre (https://api.mercadolibre.com/items/{MLB-ID})
   - JSON limpo, sem login, sem renderizar nada. Funciona pra qualquer item público.
   - É a forma "certa" de obter dados — usa o mesmo endpoint que o próprio site/app do ML usa.

2) NAVEGADOR HEADLESS (Playwright) como fallback
   - Para URLs onde só achamos MLBU (catálogo) sem item específico,
     ou se a API negar (item pausado, deletado, etc.)
   - Abre a página de verdade, espera o JS carregar, e extrai dos elementos renderizados.

Sempre retorna o mesmo dict:
{ ml_id, url, title, price, description, seller_nickname, pictures[], videos[], attributes{}, raw }
"""
import os
import re
import logging
from urllib.parse import urlparse, parse_qs

import httpx

logger = logging.getLogger(__name__)

# Captura prefixos do ML: MLB (Brasil item), MLBU (Brasil catálogo), MLA/MLM/MLC etc. de outros países
ML_ID_REGEX = re.compile(r"(MLB[U]?|MLA|MLM|MLC|MLU|MPE|MLV)-?(\d{6,})")


def _user_agent() -> str:
    return os.getenv(
        "USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    )


def _empty_result(url: str) -> dict:
    return {
        "url": url, "ml_id": None, "title": None, "price": None,
        "description": None, "seller_nickname": None,
        "pictures": [], "videos": [], "attributes": {}, "raw": None,
    }


def extract_ml_ids_from_url(url: str) -> dict[str, str]:
    """Coleta todos os IDs ML* encontrados na URL (path + query)."""
    ids: dict[str, str] = {}
    parsed = urlparse(url)

    def _scan(text: str) -> None:
        for m in ML_ID_REGEX.finditer(text):
            prefix, num = m.group(1), m.group(2)
            ids.setdefault(prefix, f"{prefix}{num}")

    _scan(parsed.path)
    for vals in parse_qs(parsed.query).values():
        for v in vals:
            _scan(v)
    # Alguns URLs do ML colocam dados no fragmento (#) também
    if parsed.fragment:
        _scan(parsed.fragment)

    return ids


def _pick_item_id(ids: dict[str, str]) -> str | None:
    """Prefere item específico (MLB) sobre catálogo (MLBU)."""
    for key in ("MLB", "MLA", "MLM", "MLC", "MLU", "MPE", "MLV"):
        if key in ids:
            return ids[key]
    return ids.get("MLBU") or next(iter(ids.values()), None)


# ============================================================
# Estratégia 1 — API oficial do ML
# ============================================================

def scrape_via_api(item_id: str) -> dict | None:
    """Busca dados do item via API pública do ML."""
    if not item_id or item_id.startswith("MLBU"):
        # MLBU é catálogo (universal product). Não existe em /items.
        # Mesmo assim tentamos o endpoint /products para extrair fotos do catálogo.
        return _scrape_catalog_via_api(item_id)

    api_url = f"https://api.mercadolibre.com/items/{item_id}"
    headers = {"User-Agent": _user_agent(), "Accept": "application/json"}

    try:
        with httpx.Client(headers=headers, timeout=20.0) as client:
            r = client.get(api_url)
            if r.status_code == 404:
                logger.info("API ML: item %s não encontrado (404)", item_id)
                return None
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.warning("API ML falhou para %s: %s", item_id, e)
        return None

    pictures: list[str] = []
    for pic in data.get("pictures") or []:
        u = pic.get("secure_url") or pic.get("url")
        if u and u not in pictures:
            pictures.append(u)

    videos: list[dict] = []
    vid = data.get("video_id")
    if isinstance(vid, str) and len(vid) == 11:
        videos.append({
            "type": "youtube",
            "url": f"https://www.youtube.com/watch?v={vid}",
            "id": vid,
        })

    # Nickname do vendedor (chamada extra)
    seller_nickname = None
    seller_id = data.get("seller_id")
    if seller_id:
        try:
            with httpx.Client(headers=headers, timeout=10.0) as client:
                ur = client.get(f"https://api.mercadolibre.com/users/{seller_id}")
                if ur.status_code == 200:
                    seller_nickname = ur.json().get("nickname")
        except Exception:
            pass

    # Descrição (chamada extra)
    description = None
    try:
        with httpx.Client(headers=headers, timeout=10.0) as client:
            dr = client.get(f"https://api.mercadolibre.com/items/{item_id}/description")
            if dr.status_code == 200:
                description = (dr.json() or {}).get("plain_text")
    except Exception:
        pass

    # Atributos vão como {id: value} pra facilitar consulta depois
    attrs = {}
    for a in data.get("attributes") or []:
        if a.get("id") and a.get("value_name"):
            attrs[a["id"]] = a["value_name"]

    logger.info("API ML: %s — %d fotos, %d vídeos", item_id, len(pictures), len(videos))

    return {
        "ml_id": data.get("id"),
        "title": data.get("title"),
        "price": data.get("price"),
        "description": description,
        "seller_nickname": seller_nickname,
        "pictures": pictures,
        "videos": videos,
        "attributes": attrs,
        "raw": data,
    }


def _scrape_catalog_via_api(catalog_id: str) -> dict | None:
    """Tenta o endpoint /products/{MLBU} para catálogo universal."""
    api_url = f"https://api.mercadolibre.com/products/{catalog_id}"
    headers = {"User-Agent": _user_agent(), "Accept": "application/json"}
    try:
        with httpx.Client(headers=headers, timeout=20.0) as client:
            r = client.get(api_url)
            if r.status_code != 200:
                return None
            data = r.json()
    except Exception as e:
        logger.warning("API ML /products falhou para %s: %s", catalog_id, e)
        return None

    pictures: list[str] = []
    for pic in data.get("pictures") or []:
        u = pic.get("url")
        if u and u not in pictures:
            pictures.append(u)

    attrs = {}
    for a in data.get("attributes") or []:
        if a.get("id") and a.get("value_name"):
            attrs[a["id"]] = a["value_name"]

    return {
        "ml_id": data.get("id") or catalog_id,
        "title": data.get("name"),
        "price": None,
        "description": data.get("short_description", {}).get("content") if isinstance(data.get("short_description"), dict) else None,
        "seller_nickname": None,
        "pictures": pictures,
        "videos": [],
        "attributes": attrs,
        "raw": data,
    }


# ============================================================
# Estratégia 2 — Navegador headless (fallback)
# ============================================================

def scrape_via_browser(url: str) -> dict:
    """Importa lazy para não pagar startup do Playwright quando não for usado."""
    from . import browser_scraper
    return browser_scraper.scrape(url)


# ============================================================
# Função principal — orquestra as duas estratégias
# ============================================================

def scrape(url: str) -> dict:
    """Dado um link de anúncio, retorna metadados + URLs de mídia."""
    parsed = urlparse(url)
    if "mercadoliv" not in parsed.netloc and "mercadolib" not in parsed.netloc:
        raise ValueError(f"URL não parece ser do Mercado Livre: {url}")

    ids = extract_ml_ids_from_url(url)
    primary = _pick_item_id(ids)
    logger.info("IDs extraídos de %s -> %s (primary=%s)", url, ids, primary)

    result: dict = _empty_result(url)

    # 1) API pública
    if primary:
        api_data = scrape_via_api(primary)
        if api_data and api_data.get("pictures"):
            api_data["url"] = url
            return api_data
        else:
            logger.info("API não trouxe fotos. Caindo para navegador headless…")

    # 2) Navegador headless
    try:
        browser_data = scrape_via_browser(url)
        browser_data["url"] = url
        if not browser_data.get("ml_id") and primary:
            browser_data["ml_id"] = primary
        return browser_data
    except Exception as e:
        logger.exception("Browser scraper falhou: %s", e)

    # Tudo falhou — devolve resultado vazio
    result["ml_id"] = primary
    return result
