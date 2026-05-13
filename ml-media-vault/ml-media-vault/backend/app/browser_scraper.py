"""
Scraper baseado em navegador headless (Playwright + Chromium).

Usado como fallback quando a API oficial não atende — por exemplo:
- URLs de catálogo universal (MLBU) sem item específico no link
- Itens privados, pausados, ou de regiões fora do escopo da API
- Quando o usuário quer "ver pelos olhos do navegador"

Estratégia:
1. Abre a página com Chromium headless (User-Agent realista)
2. Espera a rede aquietar (todo JS carregado)
3. Dispensa banner de cookies/privacy
4. Scrolla a galeria e clica nas miniaturas para forçar lazy-load
5. Captura TODAS as URLs de imagens via:
   a. Hook nas respostas de rede (mlstatic.com)
   b. Varredura do DOM final (img.src, data-zoom, srcset)
6. Captura iframes do YouTube
7. Captura vídeos nativos do ML (HLS .m3u8) via interceptação de rede

A função expõe `scrape(url)` com o mesmo contrato do scraper principal.
"""
import os
import re
import logging

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

# Padrão de URL de playlist HLS dos vídeos nativos do ML
_ML_VIDEO_HLS_PATTERN = re.compile(
    r"https://video-static(?:-clips)?\.mms\.mlstatic\.com/[^\"'\s]+\.m3u8(?:\?[^\"'\s]*)?"
)


def _user_agent() -> str:
    return os.getenv(
        "USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    )


def _upgrade_image_url(url: str) -> str:
    """Promove URL de imagem do CDN do ML para 2X de resolução."""
    if "mlstatic.com" not in url:
        return url
    url = re.sub(r"-[A-Z](\.(webp|jpg|jpeg|png))$", r"-F\1", url, flags=re.IGNORECASE)
    if "D_NQ_NP_2X_" not in url and "D_NQ_NP_" in url:
        url = url.replace("D_NQ_NP_", "D_NQ_NP_2X_")
    return url


def _is_product_image_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    if url.startswith("data:"):
        return False
    if "mlstatic.com" not in url:
        return False
    # Aceita só URLs que parecem foto de produto (CDN tem padrões D_NQ, D_W etc.)
    return any(token in url for token in ("D_NQ_", "D_W_", "D_Q_"))


def _is_ml_video_url(url: str) -> bool:
    """Retorna True se a URL for um stream HLS de vídeo nativo do ML."""
    if not isinstance(url, str):
        return False
    return bool(_ML_VIDEO_HLS_PATTERN.match(url))


def _pick_best_stream(urls: list[str]) -> list[str]:
    """
    Entre múltiplas playlists HLS capturadas, prefere a de maior resolução.
    O ML serve streams como stream-1, stream-2, stream-3 (menor → maior qualidade).
    Mantém apenas a de maior índice; se não reconhecer o padrão, retorna todas.
    """
    if len(urls) <= 1:
        return urls

    def _stream_index(u: str) -> int:
        m = re.search(r"/stream-(\d+)/", u)
        return int(m.group(1)) if m else -1

    numbered = [(u, _stream_index(u)) for u in urls if _stream_index(u) >= 0]
    if numbered:
        best = max(numbered, key=lambda x: x[1])[0]
        logger.info(
            "Streams HLS disponíveis: %s — selecionado: %s",
            [u for u, _ in numbered],
            best,
        )
        return [best]

    return urls  # fallback: retorna todos se não conseguir distinguir


def scrape(url: str, timeout_ms: int = 40000) -> dict:
    result: dict = {
        "url": url, "ml_id": None, "title": None, "price": None,
        "description": None, "seller_nickname": None,
        "pictures": [], "videos": [], "attributes": {}, "raw": None,
    }

    captured_image_urls: list[str] = []
    captured_video_urls: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            context = browser.new_context(
                user_agent=_user_agent(),
                viewport={"width": 1600, "height": 1200},
                locale="pt-BR",
            )
            page = context.new_page()

            def _on_response(resp):
                try:
                    u = resp.url
                    # Captura imagens de produto
                    if _is_product_image_url(u) and u not in captured_image_urls:
                        captured_image_urls.append(u)
                    # Captura playlists HLS de vídeo nativo do ML
                    if _is_ml_video_url(u) and u not in captured_video_urls:
                        captured_video_urls.append(u)
                        logger.info("Stream HLS detectado via rede: %s", u)
                except Exception:
                    pass

            page.on("response", _on_response)

            # 1) Navega
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except PWTimeout:
                logger.warning("Timeout no goto, continuando assim mesmo")

            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PWTimeout:
                pass

            # 2) Dispensa banners (cookies, privacidade, login flutuante)
            for selector in (
                'button[data-testid="action:understood-button"]',
                'button:has-text("Entendi")',
                'button:has-text("Aceitar")',
                'button:has-text("Continuar")',
                'button[aria-label="Fechar"]',
                'button[aria-label="close"]',
            ):
                try:
                    page.locator(selector).first.click(timeout=1500)
                    page.wait_for_timeout(300)
                    break
                except Exception:
                    pass

            # 3) Scroll progressivo para disparar lazy-load
            try:
                page.evaluate("""
                    () => new Promise(r => {
                        let y = 0;
                        const step = 400;
                        const t = setInterval(() => {
                            window.scrollBy(0, step);
                            y += step;
                            if (y >= document.body.scrollHeight) {
                                clearInterval(t);
                                window.scrollTo(0, 0);
                                r();
                            }
                        }, 80);
                    })
                """)
                page.wait_for_timeout(800)
            except Exception:
                pass

            # 4) Percorre a galeria — clica em cada miniatura para forçar a foto principal
            #    e também aciona o player de vídeo (dispara requisição HLS)
            thumb_selectors = (
                ".ui-pdp-thumbnail",
                ".ui-pdp-gallery__column figure",
                '[class*="gallery"] [class*="thumbnail"]',
            )
            for sel in thumb_selectors:
                try:
                    thumbs = page.locator(sel).all()
                    if not thumbs:
                        continue
                    logger.info("Galeria: %d miniaturas encontradas via %s", len(thumbs), sel)
                    for thumb in thumbs[:40]:
                        try:
                            thumb.scroll_into_view_if_needed(timeout=800)
                            thumb.click(timeout=1500, force=True)
                            page.wait_for_timeout(180)
                        except Exception:
                            continue
                    break
                except Exception:
                    continue

            # 5) Clica no player de vídeo para forçar carregamento do HLS
            #    (o ML carrega o .m3u8 só quando o usuário interage com o player)
            video_player_selectors = (
                '[class*="video"] button[class*="play"]',
                '[class*="pdp-video"] button',
                'button[data-testid*="play"]',
                '.vjs-big-play-button',
                '[class*="video-player"] [class*="play"]',
            )
            for sel in video_player_selectors:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=2000):
                        btn.scroll_into_view_if_needed(timeout=800)
                        btn.click(timeout=2000, force=True)
                        logger.info("Player de vídeo clicado via: %s", sel)
                        # Aguarda o HLS começar a ser requisitado
                        page.wait_for_timeout(2500)
                        break
                except Exception:
                    continue

            # 6) Extrai URLs do DOM final (imagens)
            try:
                dom_urls: list[str] = page.evaluate("""
                    () => {
                        const out = new Set();
                        const push = (u) => {
                            if (!u) return;
                            if (u.startsWith('data:')) return;
                            if (!u.includes('mlstatic.com')) return;
                            out.add(u);
                        };
                        document.querySelectorAll('img').forEach(img => {
                            push(img.getAttribute('data-zoom'));
                            push(img.getAttribute('data-src'));
                            push(img.src);
                            const ss = img.getAttribute('srcset');
                            if (ss) ss.split(',').forEach(part => push(part.trim().split(' ')[0]));
                        });
                        return [...out];
                    }
                """) or []
            except Exception as e:
                logger.error("Falha ao avaliar JS: %s", e)
                dom_urls = []

            # 7) Tenta extrair URLs HLS diretamente do HTML renderizado (fallback)
            try:
                page_source: str = page.content()
                dom_video_matches = _ML_VIDEO_HLS_PATTERN.findall(page_source)
                for vu in dom_video_matches:
                    if vu not in captured_video_urls:
                        captured_video_urls.append(vu)
                        logger.info("Stream HLS detectado no DOM: %s", vu)
            except Exception as e:
                logger.debug("Falha ao buscar HLS no DOM: %s", e)

            # 8) Combina imagens (DOM tem prioridade), dedup
            seen: set[str] = set()
            for u in dom_urls + captured_image_urls:
                if not _is_product_image_url(u):
                    continue
                up = _upgrade_image_url(u)
                if up in seen:
                    continue
                seen.add(up)
                result["pictures"].append(up)

            logger.info("Browser scraper: %d imagens capturadas", len(result["pictures"]))

            # 9) Vídeos nativos HLS — seleciona melhor stream
            best_streams = _pick_best_stream(list(set(captured_video_urls)))
            for stream_url in best_streams:
                entry = {"type": "video", "url": stream_url, "id": None}
                if entry not in result["videos"]:
                    result["videos"].append(entry)
                    logger.info("Vídeo nativo ML adicionado: %s", stream_url)

            # 10) Vídeos YouTube via iframes
            try:
                for iframe in page.locator('iframe[src*="youtube"]').all():
                    src = iframe.get_attribute("src") or ""
                    m = re.search(r"(?:embed/|v=|youtu\.be/)([A-Za-z0-9_-]{11})", src)
                    if m:
                        yid = m.group(1)
                        entry = {"type": "youtube", "url": f"https://www.youtube.com/watch?v={yid}", "id": yid}
                        if entry not in result["videos"]:
                            result["videos"].append(entry)
            except Exception:
                pass

            # 11) Título
            for sel in ("h1.ui-pdp-title", "h1"):
                try:
                    t = page.locator(sel).first.text_content(timeout=2000)
                    if t and t.strip():
                        result["title"] = t.strip()
                        break
                except Exception:
                    continue

            # 12) Preço
            try:
                price_text = page.locator(".andes-money-amount__fraction").first.text_content(timeout=2000)
                if price_text:
                    cleaned = price_text.replace(".", "").replace(",", ".").strip()
                    try:
                        result["price"] = float(cleaned)
                    except ValueError:
                        pass
            except Exception:
                pass

            # 13) Descrição
            try:
                desc = page.locator(".ui-pdp-description__content").first.text_content(timeout=2000)
                if desc:
                    result["description"] = desc.strip()[:5000]
            except Exception:
                pass

        finally:
            browser.close()

    if not result["pictures"] and not result["videos"]:
        logger.warning("Browser scraper também não achou mídia em %s", url)

    return result