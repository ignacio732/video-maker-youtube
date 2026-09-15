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

def capture(url, out_path, width=1080, height=1350, wait_ms=2500, timeout_ms=20000,
           full_page=False):
    """
    Saca una captura de `url` y la guarda en `out_path` (PNG). Devuelve out_path si
    salió bien, o None si falló (sitio bloquea headless, timeout, etc. — el llamador
    debe seguir funcionando igual sin la captura, nunca cortar el video por esto).
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
                page.screenshot(path=out_path, full_page=full_page)
            finally:
                browser.close()
        return out_path if os.path.exists(out_path) else None
    except Exception:
        return None
