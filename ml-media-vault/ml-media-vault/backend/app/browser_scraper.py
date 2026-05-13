"""
Scraper baseado em navegador headless (Playwright + Chromium).

Estratégia:
1. Abre a página com Chromium headless (User-Agent realista)
2. Intercepta respostas de rede para capturar imagens E vídeos (mp4/m3u8)
3. Dispensa banner de cookies/privacy
4. Scrolla a galeria e clica nas miniaturas para forçar lazy-load
5. Captura imagens via DOM (img.src, data-zoom, srcset)
6. Captura vídeos via:
   a. iframes do YouTube
   b. Elementos <video> no DOM (src + <source>)
   c. URLs de vídeo interceptadas na rede (mlstatic.com mp4/m3u8)
"""
import os
import re
import logging

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

# Extensões/padrões que indicam conteúdo de vídeo
_VIDEO_URL_RE = re.compile(r"\.(mp4|webm|m3u8|mov)(\?|$)", re.IGNORECASE)


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
    return any(token in url for token in ("D_NQ_", "D_W_", "D_Q_"))


def _is_video_url(url: str) -> bool:
    """Retorna True se a URL parecer um arquivo de vídeo."""
    if not isinstance(url, str) or not url.startswith("http"):
        return False
    return bool(_VIDEO_URL_RE.search(url))


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

            # ── Intercepta respostas de rede ──────────────────────────────────
            def _on_response(resp):
                try:
                    ru = resp.url
                    if _is_product_image_url(ru) and ru not in captured_image_urls:
                        captured_image_urls.append(ru)
                    elif _is_video_url(ru) and ru not in captured_video_urls:
                        captured_video_urls.append(ru)
                        logger.info("Vídeo capturado na rede: %s", ru[:120])
                except Exception:
                    pass

            page.on("response", _on_response)

            # ── Navega ────────────────────────────────────────────────────────
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except PWTimeout:
                logger.warning("Timeout no goto, continuando assim mesmo")

            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PWTimeout:
                pass

            # ── Dispensa banners ──────────────────────────────────────────────
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

            # ── Scroll progressivo para lazy-load ─────────────────────────────
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

            # ── Clica nas miniaturas da galeria ───────────────────────────────
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
                    logger.info("Galeria: %d miniaturas via %s", len(thumbs), sel)
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

            # Aguarda um pouco após clicar para requisições de vídeo dispararem
            page.wait_for_timeout(1500)

            # ── Extrai imagens do DOM ─────────────────────────────────────────
            try:
                dom_image_urls: list[str] = page.evaluate("""
                    () => {
                        const out = new Set();
                        const push = (u) => {
                            if (!u || u.startsWith('data:')) return;
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
                logger.error("Falha ao avaliar JS (imagens): %s", e)
                dom_image_urls = []

            # ── Extrai vídeos do DOM (<video> + <source>) ─────────────────────
            try:
                dom_video_urls: list[str] = page.evaluate("""
                    () => {
                        const out = new Set();
                        document.querySelectorAll('video').forEach(v => {
                            if (v.src && v.src.startsWith('http')) out.add(v.src);
                            v.querySelectorAll('source').forEach(s => {
                                if (s.src && s.src.startsWith('http')) out.add(s.src);
                            });
                        });
                        return [...out];
                    }
                """) or []
            except Exception as e:
                logger.error("Falha ao avaliar JS (vídeos DOM): %s", e)
                dom_video_urls = []

            # ── Combina imagens (DOM tem prioridade), dedup ───────────────────
            seen_images: set[str] = set()
            for u in dom_image_urls + captured_image_urls:
                if not _is_product_image_url(u):
                    continue
                up = _upgrade_image_url(u)
                if up in seen_images:
                    continue
                seen_images.add(up)
                result["pictures"].append(up)

            logger.info("Browser scraper: %d imagens", len(result["pictures"]))

            # ── Combina vídeos nativos, dedup ─────────────────────────────────
            seen_videos: set[str] = set()
            for vu in dom_video_urls + captured_video_urls:
                if vu in seen_videos:
                    continue
                seen_videos.add(vu)
                result["videos"].append({"type": "video", "url": vu})
                logger.info("Vídeo nativo encontrado: %s", vu[:120])

            # ── Vídeos YouTube via iframes ────────────────────────────────────
            try:
                for iframe in page.locator('iframe[src*="youtube"]').all():
                    src = iframe.get_attribute("src") or ""
                    m = re.search(r"(?:embed/|v=|youtu\.be/)([A-Za-z0-9_-]{11})", src)
                    if m:
                        yid = m.group(1)
                        entry = {
                            "type": "youtube",
                            "url": f"https://www.youtube.com/watch?v={yid}",
                            "id": yid,
                        }
                        if entry not in result["videos"]:
                            result["videos"].append(entry)
                            logger.info("YouTube encontrado via iframe: %s", yid)
            except Exception:
                pass

            # ── Título ────────────────────────────────────────────────────────
            for sel in ("h1.ui-pdp-title", "h1"):
                try:
                    t = page.locator(sel).first.text_content(timeout=2000)
                    if t and t.strip():
                        result["title"] = t.strip()
                        break
                except Exception:
                    continue

            # ── Preço ─────────────────────────────────────────────────────────
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

            # ── Descrição ─────────────────────────────────────────────────────
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