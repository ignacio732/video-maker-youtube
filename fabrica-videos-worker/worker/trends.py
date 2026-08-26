"""
Detección de tendencias / temas virales — 100% gratis, sin API keys.
Fuentes: Wikipedia Pageviews (lo más visto), GDELT (noticias globales),
Google News RSS (por keyword del nicho). Todo con stdlib + requests.
Cada fuente es tolerante a fallos (si una cae, sigue con las demás).
"""
import os, requests, datetime, xml.etree.ElementTree as ET, re, html

UA = {"User-Agent": "FabricaVideosYouTube/1.0 (contenido educativo)"}

_STOP_WIKI = ("Wikipedia:", "Especial:", "Portada", "Wikcionario", "Anexo:",
              "Ayuda:", "Categoría:", "Plantilla:", "(desambiguación)")

def _clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()

def wikipedia_hot(lang="es", limit=30):
    """Artículos más vistos de ayer (novedad/atención)."""
    try:
        d = datetime.date.today() - datetime.timedelta(days=1)
        url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/"
               f"{lang}.wikipedia/all-access/{d.year}/{d.month:02d}/{d.day:02d}")
        arts = requests.get(url, headers=UA, timeout=20).json()["items"][0]["articles"]
        out = []
        for a in arts:
            t = a["article"].replace("_", " ")
            if any(s in t for s in _STOP_WIKI):
                continue
            out.append({"topic": t, "source": "wikipedia", "metric": a["views"]})
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []

def gdelt_news(query, timespan="2d", maxrecords=10):
    """Noticias recientes por tema (volumen = señal de interés)."""
    try:
        url = ("https://api.gdeltproject.org/api/v2/doc/doc?"
               f"query={requests.utils.quote(query)}&mode=ArtList&maxrecords={maxrecords}"
               f"&timespan={timespan}&format=json&sort=hybridrel")
        arts = requests.get(url, headers=UA, timeout=20).json().get("articles", [])
        return [{"topic": _clean(a.get("title")), "source": "gdelt", "metric": 1}
                for a in arts if a.get("title")]
    except Exception:
        return []

def google_news(query, hl="es-419", gl="AR", maxn=10):
    """Titulares de Google News por keyword (stdlib, sin feedparser)."""
    try:
        url = (f"https://news.google.com/rss/search?q={requests.utils.quote(query)}"
               f"&hl={hl}&gl={gl}&ceid={gl}:{hl.split('-')[0]}")
        xml = requests.get(url, headers=UA, timeout=20).text
        root = ET.fromstring(xml)
        items = root.findall(".//item")
        out = []
        for it in items[:maxn]:
            title = _clean(it.findtext("title"))
            title = re.sub(r"\s+-\s+[^-]+$", "", title)
            if title:
                out.append({"topic": title, "source": "google_news", "metric": 1})
        return out
    except Exception:
        return []

def _dedupe(items):
    seen, out = set(), []
    for it in items:
        k = re.sub(r"[^a-z0-9]", "", it["topic"].lower())[:40]
        if k and k not in seen:
            seen.add(k)
            out.append(it)
    return out

def for_channel(channel, max_topics=8):
    """
    Devuelve una lista de temas candidatos para un canal, combinando fuentes
    según su nicho/keywords. Ordenados por relevancia simple.
    """
    kws = channel.get("keywords") or []
    niche = channel.get("niche", "")
    query = " OR ".join(kws[:4]) if kws else niche
    lang = channel.get("language", "es")
    items = []
    # Noticias del nicho (temas del momento)
    items += gdelt_news(query)[:6]
    items += google_news(query)[:6]
    # Novedad general (lo que la gente está mirando)
    items += [w for w in wikipedia_hot(lang)[:15]]
    items = _dedupe(items)
    kws_l = [k.lower() for k in kws]
    def relevant(it):
        t = it["topic"].lower()
        return any(k in t for k in kws_l) or any(k in t for k in niche.lower().split())
    order = {"gdelt": 0, "google_news": 1, "wikipedia": 2}
    items.sort(key=lambda x: (0 if relevant(x) else 1,
                              order.get(x["source"], 3), -x.get("metric", 0)))
    return items[:max_topics]
