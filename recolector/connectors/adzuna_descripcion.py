"""
La descripción entera del anuncio, de la web de Adzuna.

POR QUÉ EXISTE
--------------
El plan Trial de la API recorta las descripciones a **500 caracteres**: 367 de
las 381 ofertas de la base miden exactamente eso, partidas a media palabra. El
clasificador tiene que decidir si el puesto encaja leyendo media introducción
corporativa, y por eso devuelve tantas dudas: medido el 3 septiembre 2026,
**28 de 45 relevantes (62%) volvieron con «necesita tu criterio»**, con
confianza media 0,594 contra un umbral de 0,70.

El texto completo SÍ está publicado, sin login, en la misma URL que la bandeja
ya abre. Lo que recorta el plan Trial es la API, no el anuncio.

POR QUÉ ESTÁ EN `connectors/` Y NO EN `clasificador/`
-----------------------------------------------------
Regla nº 4 de la casa: **nada fuera de `connectors/` sabe que Adzuna existe.**
Este módulo conoce su HTML, sus dos formas de URL y sus rarezas de servidor,
así que es un conector aunque no hable con la API. No hereda de
`BaseConnector` porque aquel contrato es el de una búsqueda —`fetch(combo,
market)`, `planned_requests(combos, markets)`— y esto es otra forma: una
petición por oferta ya conocida. Comparte lo que importa: la caché en disco y
la regla de fallar ruidosamente.

NO TOCA LA CUOTA DE LA API
--------------------------
Son peticiones a `www.adzuna.com`, no a `api.adzuna.com`. La cuota de
2.500/mes del plan Trial no se entera. Pero sigue siendo *scraping*, con sus
condiciones de uso encima, y por eso va **apagado por defecto** en el YAML y
con una pausa entre peticiones.

LO QUE SE MIDIÓ ANTES DE ESCRIBIRLO
-----------------------------------
Sobre las 28 ofertas reales que habían vuelto con «necesita tu criterio»:

    recuperadas ...... 27 de 28  (96%)
      por JSON-LD .... 20
      por el HTML .... 7
      ninguna ........ 1   (`Gerente corporativo`, de Grupo Lunar)
    caracteres ....... mediana 5.589, media 5.867, máximo 11.001

Contra los 500 de la base, es **11,7x** más texto.

TRES RAREZAS QUE NO SE VEN Y SIN LAS CUALES NO FUNCIONA
-------------------------------------------------------
1. **Los parámetros `utm_medium=api&utm_source=<key>` son obligatorios.** Sin
   ellos Adzuna responde **HTTP 403 con 919 bytes**. Vienen ya puestos en el
   `canonical_url` que guardó el recolector, así que la regla práctica es: no
   los quites al reescribir la URL.
2. **`/land/ad/<id>` hay que reescribirlo a `/details/<id>`.** Son 110 de las
   381 ofertas (29%). Sin reescribir, en México es una página-puente de 10 KB
   que no trae descripción ninguna, solo un salto a `click.appcast.io`.
3. **Adzuna sirve HTTP 404 con la página entera.** El estado NO puede decidir:
   decide si sale texto. Un `raise_for_status()` aquí tiraría el 21% de las
   ofertas de Texas por un código que la propia página desmiente.

Y una cuarta que no es rareza sino geografía: desde fuera de EE.UU. las
páginas de Texas llegan con un cartel de *«Sorry, this job is not available in
your region»* **que sustituye al botón de postular, no a la descripción**. El
texto está entero debajo. Ver el límite conocido nº 7 del README.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests

from ..http_cache import CacheMissError, DiskCache

logger = logging.getLogger(__name__)

FUENTE = "adzuna_desc"

# Un navegador de verdad. Adzuna sirve la página completa a un cliente
# corriente; con el User-Agent del recolector la respuesta cambia.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Cuánto texto hace falta para considerar que se recuperó algo. Por debajo de
# esto es una página-puente o un error maquetado, no un anuncio: la más corta
# de las 27 medidas tenía 1.981 caracteres, y los recortes de la API son 500.
MINIMO_CARACTERES = 600


class DescripcionError(Exception):
    """No se pudo traer la página. Se cuenta y se sigue; nunca se traga."""


@dataclass(frozen=True)
class Descripcion:
    """Lo que se sacó de una página, y POR DÓNDE se sacó.

    `via` no es telemetría: es la regla nº 1 de la casa. «no se pudo» y «salió
    corta» son finales distintos y no pueden leerse igual, y el día que Adzuna
    cambie la página el reparto entre `json-ld` y `html` se moverá antes de que
    nada se rompa del todo.
    """

    texto: str
    via: str          # "json-ld" | "html" | "nada"
    http: int
    url: str
    salario: dict[str, Any] | None = None
    industria: str | None = None

    @property
    def ok(self) -> bool:
        return len(self.texto) >= MINIMO_CARACTERES


# ---------------------------------------------------------------------------
#  Lo puro: se prueba entero sin red
# ---------------------------------------------------------------------------

_ENTIDADES = (
    ("&nbsp;", " "), ("&amp;", "&"), ("&#39;", "'"), ("&apos;", "'"),
    ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">"), ("&rsquo;", "'"),
    ("&lsquo;", "'"), ("&ndash;", "-"), ("&mdash;", "-"), ("&hellip;", "..."),
)


def a_texto(html: str) -> str:
    """HTML -> texto plano legible, conservando la estructura de párrafos.

    No se usa una librería de parseo (BeautifulSoup, lxml) a propósito: sería
    una dependencia nueva en un `requirements.txt` que el README presume corto,
    para un trabajo que aquí son doce sustituciones. Lo que se conserva son los
    saltos de línea de `<br>`, `<p>` y `<li>`, porque un anuncio sin ellos es
    un muro de texto y el modelo lee peor las responsabilidades.
    """
    t = html or ""
    t = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", t, flags=re.I | re.S)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"<li\b[^>]*>", "\n- ", t, flags=re.I)
    t = re.sub(r"</(p|div|h[1-6]|tr|ul|ol|section)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    for entidad, letra in _ENTIDADES:
        t = t.replace(entidad, letra)
    # `&#233;` y compañía, que aparecen en los anuncios en español.
    t = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _json_ld(html: str) -> dict[str, Any] | None:
    """El bloque `schema.org/JobPosting`, si la página lo trae.

    Es la vía buena y hay que intentarla primero: Adzuna lo publica para que
    Google Jobs indexe la oferta, así que es un formato con dueño y con
    incentivo de no romperse — al revés que una clase de CSS, que cambia con
    el rediseño. Trae además `baseSalary` e `industry`, que la API no da.
    """
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.I | re.S,
    ):
        try:
            bloque = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        # Algunas páginas envuelven el JobPosting en una lista.
        candidatos = bloque if isinstance(bloque, list) else [bloque]
        for c in candidatos:
            if isinstance(c, dict) and c.get("@type") == "JobPosting":
                return c
    return None


def _div_descripcion(html: str) -> str:
    """La descripción del HTML visible, para las páginas sin JSON-LD.

    `ui-foreign-click-description` es el contenedor que Adzuna usa cuando la
    visita viene de fuera de la región del anuncio — que es el caso del 68% de
    la base. Es una clase de CSS y por tanto frágil: por eso va de segunda y
    nunca de primera.
    """
    m = re.search(
        r'class="[^"]*ui-foreign-click-description[^"]*"(.*?)</section>',
        html, re.S,
    )
    return a_texto(m.group(1)) if m else ""


def normalizar_url(url: str) -> str:
    """`/land/ad/<id>` -> `/details/<id>`, conservando los `utm_*`.

    Los `utm_*` NO son adorno: sin ellos la respuesta es un 403 de 919 bytes.
    Se conservan solo esos y se tira el resto de la query (`se=`, tokens de
    sesión) porque caducan y ensucian la clave de caché.
    """
    p = urlparse(url)
    utm = "&".join(x for x in p.query.split("&") if x.startswith("utm_"))
    m = re.match(r"^/land/ad/(\d+)", p.path)
    ruta = f"/details/{m.group(1)}" if m else p.path
    return urlunparse((p.scheme, p.netloc, ruta, "", utm, ""))


def extraer(html: str, http: int = 200, url: str = "") -> Descripcion:
    """De una página a una descripción. Puro: ni red, ni disco, ni reloj."""
    bloque = _json_ld(html)
    if bloque and bloque.get("description"):
        texto = a_texto(bloque["description"])
        if len(texto) >= MINIMO_CARACTERES:
            return Descripcion(
                texto=texto, via="json-ld", http=http, url=url,
                salario=bloque.get("baseSalary"),
                industria=bloque.get("industry"),
            )

    texto = _div_descripcion(html)
    if len(texto) >= MINIMO_CARACTERES:
        return Descripcion(texto=texto, via="html", http=http, url=url)

    return Descripcion(texto="", via="nada", http=http, url=url)


# ---------------------------------------------------------------------------
#  Lo que toca la red
# ---------------------------------------------------------------------------

class TraedorDeDescripciones:
    """Una petición por oferta, con caché en disco y pausa entre llamadas.

    La caché es la misma pieza que usa el recolector (`DiskCache`) y por la
    misma razón: depurar el extractor contra veinte páginas reales no puede
    costar veinte peticiones cada vez que se toca una expresión regular. En
    modo `cache_only` no toca la red en absoluto, que es como corren los tests
    y como corre `--cache-only`.
    """

    def __init__(
        self,
        cache: DiskCache,
        timeout_seconds: int = 30,
        sleep_seconds: float = 1.5,
        sesion: Any | None = None,
    ) -> None:
        self.cache = cache
        self.timeout_seconds = timeout_seconds
        self.sleep_seconds = sleep_seconds
        # La sesión entra por parámetro para poder pasar una falsa en los
        # tests, igual que `Motor` y `Triador` reciben el cliente del LLM.
        self.sesion = sesion or requests.Session()
        if hasattr(self.sesion, "headers"):
            self.sesion.headers.update(
                {"User-Agent": USER_AGENT,
                 "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"}
            )
        self.peticiones = 0

    def traer(self, canonical_url: str) -> Descripcion:
        """La descripción de UNA oferta. No lanza por 404: ver el módulo."""
        url = normalizar_url(canonical_url)
        clave = DiskCache.build_key(FUENTE, "web", url, "v1")

        entrada = self.cache.get(clave)
        if entrada is not None and entrada.is_fresh:
            payload = entrada.payload
            return extraer(payload["html"], payload.get("http", 200), url)

        if self.cache.is_cache_only:
            if entrada is not None:
                payload = entrada.payload
                return extraer(payload["html"], payload.get("http", 200), url)
            raise CacheMissError(
                f"[{FUENTE}] --cache-only y no hay copia de {url}"
            )

        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)
        self.peticiones += 1

        try:
            respuesta = self.sesion.get(url, timeout=self.timeout_seconds)
        except requests.Timeout as exc:
            raise DescripcionError(
                f"[{FUENTE}] TIMEOUT tras {self.timeout_seconds}s en {url}"
            ) from exc
        except requests.RequestException as exc:
            raise DescripcionError(f"[{FUENTE}] Fallo de red en {url}: {exc}") from exc

        html = respuesta.text
        codigo = respuesta.status_code

        # 403 SÍ es un error, y uno con nombre: significa que se perdieron los
        # `utm_*` por el camino. Un 404, en cambio, viene con la página entera.
        if codigo == 403:
            raise DescripcionError(
                f"[{FUENTE}] HTTP 403 en {url}\n"
                "  Casi siempre es que la URL perdió los parámetros "
                "`utm_medium=api&utm_source=...`, que Adzuna exige. "
                "Ver `normalizar_url`."
            )

        self.cache.put(
            clave,
            {"url": url, "fuente": FUENTE},
            {"html": html, "http": codigo},
        )
        return extraer(html, codigo, url)
