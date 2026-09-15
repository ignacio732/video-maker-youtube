"""
Capturas reales de páginas web (gratis, Playwright + Chromium headless en el propio
runner de GitHub Actions — sin API paga, sin límite de uso).

Objetivo: mezclar UNA imagen real (la noticia/fuente de verdad de la que sale el
video) con el resto del material del video (stock/IA), para dar autenticidad —
"esto lo estoy sacando de una fuente real", no todo genérico.

No reemplaza el pipeline de visuales existente: agrega UN visual más, que
worker.py inserta en el segmento del hook cuando el canal tiene reference_sites
y se pudo identificar la URL real de la noticia.
"""
import os

_AD_HOSTS = (
    "doubleclick.net", "googlesyndication.com", "google-analytics.com",
    "googletagmanager.com", "googletagservices.com", "adservice.google",
    "amazon-adsystem.com", "taboola.com", "outbrain.com", "criteo.com",
    "moatads.com", "adsafeprotected.com", "pubmatic.com", "rubiconproject.com",
    "casalemedia.com", "openx.net", "smartadserver.com", "yieldmo.com",
    "media.net", "adnxs.com", "advertising.com", "scorecardresearch.com",
)

# Selectores comunes de titular/nota en medios ES/LATAM, probados en orden — el
# primero que exista se usa para recortar la captura, evitando la franja de
# publicidad que casi todo sitio de noticias mete arriba de la nota.
_ARTICLE_SELECTORS = [
    "article h1", "h1[itemprop='headline']", "h1.title", "h1.article-title",
    "header h1", "h1", "article",
]

def capture(url, out_path, width=1080, height=1350, wait_ms=2500, timeout_ms=20000,
           full_page=False):
    """
    Saca una captura de `url` y la guarda en `out_path` (PNG). Devuelve out_path si
    salió bien, o None si falló (sitio bloquea headless, timeout, etc. — el llamador
    debe seguir funcionando igual sin la captura, nunca cortar el video por esto).

    Prioriza capturar el TITULAR/nota real (busca un h1/article y recorta ahí) en vez
    del viewport completo, porque casi todo sitio de noticias mete una franja de
    publicidad pesada arriba del contenido — una captura "a ciegas" del viewport
    termina siendo puro banner de anuncios, no la noticia.
    """
    if not url:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None  # playwright no instalado en este entorno -> se sigue sin captura
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
            try:
                page = browser.new_page(
                    viewport={"width": width, "height": height},
                    ignore_https_errors=True,
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/124.0.0.0 Safari/537.36"))
                # Bloquear publicidad/trackers conocidos: sin esto, media página queda
                # tapada de banners y la captura no muestra la noticia real.
                page.route("**/*", lambda route: route.abort()
                          if any(h in route.request.url for h in _AD_HOSTS)
                          else route.continue_())
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                page.wait_for_timeout(wait_ms)  # deja asentar layout/imágenes/cookies-banner
                # Cerrar banners de cookies comunes en medios ES/LATAM, best-effort.
                for sel in ["text=Aceptar", "text=Accept", "text=Aceptar todo",
                           "[aria-label='Aceptar']", "#onetrust-accept-btn-handler"]:
                    try:
                        page.click(sel, timeout=1200)
                        page.wait_for_timeout(400)
                        break
                    except Exception:
                        pass
                # Preferir la zona del titular, pero capturando el VIEWPORT completo
                # ahí (no solo el recuadro del texto) — así entra también la imagen de
                # portada o lo que sigue debajo. Un recorte de puro texto queda
                # borroso y gigante cuando el render le aplica el efecto Ken Burns
                # (pensado para fotos, no para una tira de texto).
                found_headline = False
                for sel in _ARTICLE_SELECTORS:
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0 and el.is_visible(timeout=1000):
                            box = el.bounding_box()
                            if box and box["height"] > 20:
                                # Scrollear un poco POR ARRIBA del titular (no justo
                                # encima) para que el viewport capturado incluya
                                # titular + imagen/contenido de abajo, no quede el
                                # titular pegado al borde superior.
                                page.evaluate(
                                    "(y) => window.scrollTo(0, Math.max(0, y - 40))",
                                    box["y"] + page.evaluate("window.scrollY"))
                                page.wait_for_timeout(300)
                                found_headline = True
                                break
                    except Exception:
                        continue
                page.screenshot(path=out_path, full_page=full_page)
            finally:
                browser.close()
        return out_path if os.path.exists(out_path) else None
    except Exception:
        return None
