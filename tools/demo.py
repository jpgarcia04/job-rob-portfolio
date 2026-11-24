"""
Demo pública de Job Rob: la bandeja real, con ofertas INVENTADAS.

    python tools/demo.py              # abre la bandeja en el navegador
    python tools/demo.py --sin-navegador --puerto 8800

No necesita API keys, no hace ninguna llamada de red y no gasta nada.

LO QUE ES REAL
  - el pipeline: las ofertas entran como respuestas de la API de Adzuna y
    pasan por los mismos filtros, la deduplicación, el filtro geográfico y el
    guardado que en producción
  - la bandeja: es el mismo servidor y la misma interfaz que usa la clienta

LO QUE ESTÁ INVENTADO
  - las ofertas y las empresas: no existen
  - las clasificaciones y el orden de «Por dónde empezar»: aquí los escribe
    este archivo; en producción los escribe el modelo leyendo cada anuncio
  - el perfil buscado: un ejecutivo de operaciones ficticio
    (config/search_profile.yaml y config/cvs.yaml)

Todo se crea en una carpeta temporal. No toca `data/`.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from recolector.storage import Storage  # noqa: E402
from tests import fixtures  # noqa: E402
from tests.offline import run_offline  # noqa: E402

MODELO = "(demo — sin llamadas al modelo)"
HUELLA = "demo"


# ---------------------------------------------------------------------------
#  Las ofertas (ficticias), en el formato en que las devuelve Adzuna
# ---------------------------------------------------------------------------

def _fecha(dias: int) -> str:
    momento = datetime.now(timezone.utc) - timedelta(days=dias)
    return momento.strftime("%Y-%m-%dT%H:%M:%SZ")


_contador = iter(range(7_000_000_001, 7_000_001_000))


def _oferta(titulo, empresa, lugar, area, descripcion, dias, sueldo=None,
            dominio="adzuna.com") -> dict:
    ident = str(next(_contador))
    o = {
        "id": ident,
        "created": _fecha(dias),
        "title": titulo,
        "company": {"display_name": empresa},
        "location": {"display_name": lugar, "area": area},
        "salary_is_predicted": "0",
        "redirect_url": f"https://www.{dominio}/land/ad/{ident}",
        "description": descripcion,
    }
    if sueldo:
        o["salary_min"], o["salary_max"] = sueldo
    return o


TEXAS_LOGISTICS = [
    _oferta("Chief Operating Officer - Logistics", "Gulfway Freight Systems",
            "Houston, Harris County", ["US", "Texas", "Harris County", "Houston"],
            "Lead a 1,200-person freight and warehousing operation across the Gulf "
            "Coast. Reports to the CEO; owns P&L, safety and network expansion.",
            2, (260000.0, 300000.0)),
    _oferta("Director of Operations - Cross-border Logistics", "Rio Grande Crossdock Co.",
            "McAllen, Hidalgo County", ["US", "Texas", "Hidalgo County", "McAllen"],
            "Run cross-dock and customs operations between Texas and Nuevo León. "
            "Bilingual leadership role with a team of 300.",
            4),
    _oferta("Chief Operating Officer", "Harbor Injury Attorneys",
            "Houston, Harris County", ["US", "Texas", "Harris County", "Houston"],
            "Personal injury law firm seeks a COO to oversee intake, case "
            "management and marketing operations.",
            3, (180000.0, 210000.0)),
    _oferta("General Manager, Logistics", "Coastal Staffing Partners",
            "Corpus Christi, Nueces County", ["US", "Texas", "Nueces County", "Corpus Christi"],
            "Our client, a port logistics operator, is hiring a General Manager to "
            "lead terminal and trucking operations.",
            6),
    _oferta("Head of Operations", "Lone Star Distribution",
            "Houston, Harris County", ["US", "Texas", "Harris County", "Houston"],
            "Growing distributor looking for a hands-on operations leader.",
            1),
]

TEXAS_SUPPLY = [
    _oferta("Chief Operating Officer, Supply Chain", "Brazos Cold Chain",
            "Austin, Travis County", ["US", "Texas", "Travis County", "Austin"],
            "Temperature-controlled supply chain company (12 DCs) seeks a COO to "
            "scale operations ahead of a regional expansion.",
            5, (230000.0, 270000.0)),
    _oferta("Plant Director, Supply Chain", "Alamo Packaging Group",
            "San Antonio, Bexar County", ["US", "Texas", "Bexar County", "San Antonio"],
            "Lead a 450-person packaging plant: production, procurement, logistics "
            "and continuous improvement.",
            7),
    _oferta("COO - Logistics Marketplace (SaaS)", "ShipStack",
            "Austin, Travis County", ["US", "Texas", "Travis County", "Austin"],
            "Venture-backed startup. The COO will build our B2B go-to-market "
            "motion and scale customer onboarding.",
            2),
]

MEXICO_LOGISTICS = [
    _oferta("Director de Operaciones - Logística", "Grupo Transportes del Norte",
            "Monterrey, Nuevo León", ["Mexico", "Nuevo León", "Monterrey"],
            "Dirección de una red de 9 centros de distribución y 600 unidades de "
            "transporte. Reporta a Dirección General.",
            3, (2400000.0, 3000000.0), "adzuna.com.mx"),
    _oferta("Gerente General de Logística", "Almacenes de Occidente",
            "Guadalajara, Jalisco", ["Mexico", "Jalisco", "Guadalajara"],
            "Responsable de almacenes, inventarios y última milla para la región "
            "occidente.",
            8, None, "adzuna.com.mx"),
    _oferta("Director de Operaciones de TI", "Banco Regional del Norte",
            "Monterrey, Nuevo León", ["Mexico", "Nuevo León", "Monterrey"],
            "Dirección de operaciones de sistemas, centros de datos y soporte "
            "tecnológico para la red de sucursales.",
            4, None, "adzuna.com.mx"),
]

MEXICO_SUPPLY = [
    _oferta("Director de Planta", "Manufacturas del Bajío",
            "Querétaro, Querétaro", ["Mexico", "Querétaro", "Querétaro"],
            "Planta de autopartes con 800 colaboradores. Dirección de producción, "
            "cadena de suministro y calidad.",
            5, None, "adzuna.com.mx"),
    _oferta("Consultor Senior de Operaciones", "Consultora Delta",
            "Saltillo, Coahuila", ["Mexico", "Coahuila", "Saltillo"],
            "Proyectos de transformación operativa para clientes industriales.",
            9, None, "adzuna.com.mx"),
]

ALEMANIA_LOGISTICS = [
    _oferta("Chief Operating Officer (COO) – Logistics", "Rheinland Logistik AG",
            "Düsseldorf, Nordrhein-Westfalen", ["Germany", "Nordrhein-Westfalen", "Düsseldorf"],
            "International contract logistics provider seeks a COO for its DACH "
            "network (18 sites).",
            3, (190000.0, 230000.0), "adzuna.de"),
    _oferta("Leiter Betrieb – Logistics", "Hanse Port Services GmbH",
            "Hamburg", ["Germany", "Hamburg"],
            "Verantwortung für den Terminalbetrieb und 250 Mitarbeitende im "
            "Hamburger Hafen.",
            6, None, "adzuna.de"),
    _oferta("Chief Operating Officer", "Vertraulich",
            "Berlin", ["Germany", "Berlin"],
            "Bedeutende Unternehmensgruppe sucht COO.",
            10, None, "adzuna.de"),
]

ALEMANIA_SUPPLY = [
    _oferta("COO - Supply Chain Development", "Frankfurter Immobilien Partner",
            "Frankfurt am Main, Hessen", ["Germany", "Hessen", "Frankfurt am Main"],
            "Projektentwicklung im Bereich Wohn- und Gewerbeimmobilien.",
            4, None, "adzuna.de"),
]


def _payload(resultados: list[dict]) -> dict:
    return {"count": len(resultados), "results": resultados}


PAYLOADS_DEMO = {
    ("adzuna", "texas_us", "COO + Logistics"): lambda: _payload(TEXAS_LOGISTICS),
    ("adzuna", "texas_us", "COO + Supply Chain"): lambda: _payload(TEXAS_SUPPLY),
    ("adzuna", "mexico", "COO + Logistics"): lambda: _payload(MEXICO_LOGISTICS),
    ("adzuna", "mexico", "COO + Supply Chain"): lambda: _payload(MEXICO_SUPPLY),
    ("adzuna", "alemania", "COO + Logistics"): lambda: _payload(ALEMANIA_LOGISTICS),
    ("adzuna", "alemania", "COO + Supply Chain"): lambda: _payload(ALEMANIA_SUPPLY),
}


# ---------------------------------------------------------------------------
#  Los veredictos (escritos a mano; en producción los escribe el modelo)
# ---------------------------------------------------------------------------

def _v(relevante, confianza, razon, seniority, descarte=None, criterio=False,
       reclutadora=False, modalidad="presencial") -> dict:
    return {
        "relevante": relevante, "confianza": confianza, "razon": razon,
        "seniority": seniority, "descarte": descarte, "criterio": criterio,
        "reclutadora": reclutadora, "modalidad": modalidad,
    }


VEREDICTOS = {
    "Gulfway Freight Systems": _v(1, 0.94,
        "Dirige la operación completa de transporte y almacenes con P&L propio y "
        "reporta al CEO. Es exactamente el nivel y la materia del perfil.", "c_level"),
    "Rio Grande Crossdock Co.": _v(1, 0.88,
        "Dirección de operaciones transfronterizas Texas–Nuevo León con 300 personas. "
        "El componente bilingüe y aduanero le suma.", "direccion"),
    "Harbor Injury Attorneys": _v(0, 0.91,
        "El título encaja, pero es un despacho de abogados: la operación es de "
        "captación y gestión de casos, no logística.", "c_level",
        descarte="Nivel correcto, sector sin relación con la trayectoria."),
    "Coastal Staffing Partners": _v(1, 0.78,
        "Dirección general de un operador logístico portuario. La publica una agencia, "
        "así que el empleador final no aparece.", "direccion", reclutadora=True),
    "Lone Star Distribution": _v(1, 0.58,
        "El título promete, pero el anuncio no dice tamaño de equipo ni a quién "
        "reporta. Merece leerlo entero antes de decidir.", "direccion", criterio=True),
    "Brazos Cold Chain": _v(1, 0.91,
        "COO de una cadena de frío con 12 centros de distribución en plena expansión: "
        "puesto de crecimiento en su materia.", "c_level", modalidad="hibrido"),
    "Alamo Packaging Group": _v(1, 0.84,
        "Dirección de planta que incluye compras, logística y mejora continua. Algo "
        "más industrial que logístico, pero dentro del perfil.", "direccion"),
    "ShipStack": _v(0, 0.86,
        "Pese a decir «Logistics», la función real es comercial: montar la venta B2B "
        "de una startup de software.", "c_level",
        descarte="La función es comercial y de producto, no operativa."),
    "Grupo Transportes del Norte": _v(1, 0.93,
        "Dirige 9 centros de distribución y una flota propia, reportando a Dirección "
        "General. Encaja de lleno.", "direccion"),
    "Almacenes de Occidente": _v(1, 0.74,
        "Gerencia regional de almacenes y última milla. Encaja en materia, aunque el "
        "nivel queda un escalón por debajo.", "gerencia"),
    "Banco Regional del Norte": _v(0, 0.95,
        "Es dirección de operaciones de sistemas en un banco: tecnología, no "
        "logística ni cadena de suministro.", "direccion",
        descarte="Operaciones de TI; otra función por completo."),
    "Manufacturas del Bajío": _v(1, 0.87,
        "Dirección de una planta de 800 personas con cadena de suministro a su cargo.",
        "direccion"),
    "Consultora Delta": _v(1, 0.55,
        "La consultoría en operaciones encaja en «también aplica», pero el anuncio no "
        "deja claro si es un puesto senior o de ejecución.", "gerencia", criterio=True),
    "Rheinland Logistik AG": _v(1, 0.9,
        "COO de un operador de logística por contrato con 18 centros en DACH. "
        "Anuncio en inglés: va el CV en inglés.", "c_level"),
    "Hanse Port Services GmbH": _v(1, 0.82,
        "Dirección de la operación de una terminal con 250 personas. El anuncio está "
        "en alemán; el CV en inglés es la mejor opción disponible.", "direccion"),
    "Vertraulich": _v(1, 0.6,
        "Empresa confidencial y descripción de una línea: el nivel encaja, pero no hay "
        "forma de saber el sector sin leer el anuncio.", "c_level", criterio=True),
    "Frankfurter Immobilien Partner": _v(0, 0.86,
        "«Supply Chain» aparece en el título, pero la empresa es una promotora "
        "inmobiliaria.", "c_level",
        descarte="Sector inmobiliario; no es una operación logística."),
}

ORDEN = [
    ("Gulfway Freight Systems", "postular_ya",
     "La más completa: P&L, 1.200 personas y reporte directo al CEO."),
    ("Grupo Transportes del Norte", "postular_ya",
     "Misma materia en México, con flota propia; publicada hace tres días."),
    ("Brazos Cold Chain", "postular_ya",
     "Expansión inminente: es cuando más se valora experiencia escalando redes."),
    ("Rheinland Logistik AG", "revisar",
     "Encaja, pero implica mudanza a Alemania: decide tú si es una opción."),
    ("Rio Grande Crossdock Co.", "revisar",
     "Un nivel por debajo de las tres primeras, pero muy alineada en materia."),
    ("Manufacturas del Bajío", "revisar",
     "Más planta que logística; buena si quieres abrir el abanico a manufactura."),
    ("Hanse Port Services GmbH", "revisar", "Anuncio en alemán: léelo antes de decidir."),
    ("Coastal Staffing Partners", "revisar",
     "Vía agencia: vale la pena preguntar quién es el cliente final."),
    ("Alamo Packaging Group", "revisar", "Más industrial que logística."),
    ("Lone Star Distribution", "revisar",
     "El anuncio dice poco: confirma tamaño de equipo y a quién reporta."),
    ("Vertraulich", "revisar", "Confidencial y escueta; solo merece la pena si el sector cuadra."),
    ("Consultora Delta", "revisar", "Consultoría: interesa solo si el proyecto es de dirección."),
    ("Almacenes de Occidente", "descartar",
     "Encaja en materia, pero a nivel de gerencia no compite con el resto."),
]


def sembrar_veredictos(almacen: Storage) -> int:
    filas = almacen.connection.execute(
        "SELECT dedupe_key, title, company, market_key, description FROM offers"
    ).fetchall()
    for fila in filas:
        v = VEREDICTOS.get(fila["company"])
        if v is None:
            continue
        idioma = "es" if fila["market_key"] == "mexico" else "en"
        anuncio = "de" if fila["company"] == "Hanse Port Services GmbH" else idioma
        almacen.guardar_clasificacion({
            "dedupe_key": fila["dedupe_key"],
            "estado": "ok",
            "relevante": v["relevante"],
            "motivo_descarte": v["descarte"],
            "confianza": v["confianza"],
            "razon": v["razon"],
            "cv_id": f"ejecutivo_{idioma}",
            "idioma_anuncio": anuncio,
            "idioma_detectado": anuncio,
            "empresa_normalizada": fila["company"],
            "puesto_canonico": fila["title"],
            "es_reclutadora": int(v["reclutadora"]),
            "seniority_detectado": v["seniority"],
            "modalidad": v["modalidad"],
            "necesita_criterio": int(v["criterio"]),
            "motivo_criterio": (
                f"Confianza {v['confianza']:.2f}, por debajo del umbral 0.70."
                if v["criterio"] else None
            ),
            "error": None,
            "modelo": MODELO,
            "tokens_entrada": 900,
            "tokens_salida": 380,
            "coste_usd": 0.0145,
            "fichas_huella": HUELLA,
            "heredada_de": None,
            "descripcion_via": "recorte",
            "caracteres_descripcion": len(fila["description"] or ""),
        })
    return len(filas)


def sembrar_ranking(almacen: Storage) -> None:
    claves = {
        f["company"]: f["dedupe_key"]
        for f in almacen.connection.execute("SELECT dedupe_key, company FROM offers")
    }
    items = [
        (claves[empresa], pos, por_que, accion)
        for pos, (empresa, accion, por_que) in enumerate(ORDEN, start=1)
        if empresa in claves
    ]
    almacen.guardar_ranking(
        {
            "id": uuid.uuid4().hex[:12],
            "creado_at": datetime.now(timezone.utc).isoformat(),
            "modelo": MODELO,
            "lectura": (
                "Semana buena para logística en Texas y Nuevo León: tres puestos de "
                "dirección general con equipo grande y reporte al CEO. Alemania trae "
                "dos opciones sólidas si la mudanza entra en tus planes. Lo demás "
                "es ruido con título bonito: un despacho de abogados, una startup "
                "de software y una promotora inmobiliaria."
            ),
            "patrones": '["El corredor Texas–Nuevo León concentra lo mejor de la semana.", '
                        '"Tres anuncios usan «COO» o «Supply Chain» para puestos de otra función."]',
            "ofertas_consideradas": len(items),
            "ofertas_disponibles": len(items),
            "tokens_entrada": 4200,
            "tokens_salida": 1100,
            "coste_usd": 0.048,
        },
        items,
    )


def preparar(carpeta: Path) -> Path:
    """Crea la base de la demo en `carpeta` y devuelve su ruta."""
    # run_offline siembra la caché desde `fixtures.PAYLOADS`; se cambia por las
    # ofertas de la demo solo durante la corrida y se deja como estaba.
    originales = dict(fixtures.PAYLOADS)
    fixtures.PAYLOADS.clear()
    fixtures.PAYLOADS.update(PAYLOADS_DEMO)
    try:
        _stats, config, _perfil, almacen = run_offline(
            carpeta,
            sources=("adzuna",),
            markets=("texas_us", "mexico", "alemania"),
        )
    finally:
        fixtures.PAYLOADS.clear()
        fixtures.PAYLOADS.update(originales)
    try:
        sembrar_veredictos(almacen)
        sembrar_ranking(almacen)
    finally:
        almacen.close()
    return config.database_path


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--puerto", type=int, default=8766)
    p.add_argument("--sin-navegador", action="store_true")
    args = p.parse_args(argv)

    carpeta = Path(tempfile.mkdtemp(prefix="jobrob_demo_"))
    base = preparar(carpeta)

    print()
    print("=" * 70)
    print(" JOB ROB — DEMO (ofertas, empresas y veredictos inventados)")
    print("=" * 70)
    print(f"  Base temporal: {base}")

    from bandeja import server

    server._abrir_almacen = lambda: Storage(base)  # type: ignore[assignment]

    import uvicorn

    url = f"http://127.0.0.1:{args.puerto}"
    print(f"  {url}   (Ctrl+C para parar)\n")
    if not args.sin_navegador:
        webbrowser.open(url)
    uvicorn.run(server.app, host="127.0.0.1", port=args.puerto, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
