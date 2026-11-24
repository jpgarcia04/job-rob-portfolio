"""
La lista blanca de títulos — el filtro que decide qué llega a la clienta.

Se prueba contra el YAML REAL que se envía, no contra listas inventadas, por
la misma razón que las pruebas de la lista negra en test_clasificador.py: el
riesgo no está en el código, está en el afinado. El código lleva semanas sin
cambiar; las listas se tocan cada vez que aparece un anuncio raro.

LAS DOS LISTAS FALLAN EN DIRECCIONES OPUESTAS, Y SOLO UNA DUELE:

  - lista negra demasiado ancha  -> le esconde una oferta buena
  - lista blanca demasiado corta -> le esconde una oferta buena
  - lista blanca demasiado ancha -> le llega ruido y paga una llamada de más

Las dos primeras son la misma catástrofe: ella nunca se entera de que esa
vacante existió. La tercera le cuesta treinta segundos y $0,016. Por eso casi
todo lo que hay aquí abajo comprueba que NO se pierde nada, y solo una prueba
vigila el ruido.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from recolector.config import load_config
from recolector.filters import DROP_TITLE, DROP_TITLE_OFF_TARGET, apply_filters
from recolector.models import JobOffer, KeywordCombo
from recolector.textnorm import contains_any_term

RAIZ = Path(__file__).resolve().parent.parent


def _perfil_del_yaml() -> dict:
    datos = yaml.safe_load((RAIZ / "config" / "search_profile.yaml").read_text(encoding="utf-8"))
    return datos["profiles"][datos["active_profile"]]


def _lista_blanca() -> list[str]:
    return _perfil_del_yaml()["hard_filters"]["include_title_terms"]


def _lista_negra() -> list[str]:
    return _perfil_del_yaml()["hard_filters"]["exclude_title_terms"]


def _puestos_objetivo_de_cvs() -> list[str]:
    """Los `puestos_objetivo` de las fichas, que son la otra mitad del criterio.

    Vive en cvs.yaml, que edita la clienta. La lista blanca vive en
    search_profile.yaml, que edita el desarrollador. Que dos archivos
    distintos tengan que estar de acuerdo es exactamente el tipo de cosa que
    se desincroniza en silencio: ver la prueba de sincronía más abajo.
    """
    fichas = yaml.safe_load((RAIZ / "config" / "cvs.yaml").read_text(encoding="utf-8"))
    return sorted({p for cv in fichas["cvs"] for p in cv["puestos_objetivo"]})


# ---------------------------------------------------------------------------
#  1. No perder nada de lo que ya funcionaba
# ---------------------------------------------------------------------------

# Títulos REALES de la corrida del 27 agosto 2026 que a ella le servían.
# Son los mismos nueve que vigila la lista negra en test_clasificador.py, y
# aquí se vuelven a comprobar contra la lista blanca: entre las dos listas no
# pueden dejar caer ni uno.
#
# Los dos últimos son los que importan de verdad. «Dirección de Manufactura» no
# dice «Director» y «Gerente corporativo» no está en ninguno de sus
# `puestos_objetivo`: son la razón por la que esta lista es de CABEZAS DE ROL
# y no de puestos literales. Si alguien "optimiza" la lista sustituyéndola por
# los puestos objetivo de cvs.yaml, estos dos se ponen rojos.
BUENAS_REALES = [
    "COO — National Logistics Growth Leader (Austin)",
    "COO, Asset Maintenance - Strategic Growth Leader (Dallas)",
    "Chief Operating Officer",
    "Chief Operating Officer COO",
    "Director General",
    "Director de logística",
    "Scale-Driven COO for Marketplace Platform (Austin)",
    "Dirección de Manufactura",
    "Gerente corporativo",
]


@pytest.mark.parametrize("titulo", BUENAS_REALES)
def test_la_lista_blanca_deja_pasar_todas_las_buenas_reales(titulo: str):
    assert contains_any_term(titulo, _lista_blanca()) is not None, (
        f"«{titulo}» le servía en la corrida real y la lista blanca lo deja fuera. "
        "Una oferta que no pasa este filtro no llega al clasificador, no llega a la "
        "bandeja y ella nunca se entera de que existió."
    )


@pytest.mark.parametrize("titulo", BUENAS_REALES)
def test_la_lista_negra_ampliada_sigue_sin_matar_las_buenas(titulo: str):
    """La ampliación de la negra del 31/08 corría el riesgo de daño colateral.

    Se le añadieron 40 términos de golpe (marketing, finanzas, ventas, tienda...)
    para poder abrir la blanca sin ahogarse en ruido. Términos como «comercial»
    o «fiscal» son anchos, y esta prueba es la que impide que uno de ellos se
    lleve por delante una oferta buena.
    """
    culpable = contains_any_term(titulo, _lista_negra())
    assert culpable is None, (
        f"«{titulo}» es una oferta BUENA y la lista negra la mata por «{culpable}». "
        "Ese término es demasiado ancho: recórtalo o quítalo."
    )


@pytest.mark.parametrize("puesto", _puestos_objetivo_de_cvs())
def test_todo_puesto_objetivo_de_la_clienta_pasa_la_lista_blanca(puesto: str):
    """La prueba de sincronía entre los dos archivos de configuración.

    `cvs.yaml` lo edita ELLA; `search_profile.yaml` lo editas TÚ. El día que
    añada «Director de Sostenibilidad» a sus puestos objetivo y nadie toque la
    lista blanca, el recolector dejará de traerle esas ofertas y no habrá
    ningún síntoma: el reporte dirá «no hay mercado», que es la conclusión
    falsa más cara del proyecto.

    Esta prueba convierte ese silencio en un fallo rojo.
    """
    assert contains_any_term(puesto, _lista_blanca()) is not None, (
        f"«{puesto}» está en los puestos objetivo de config/cvs.yaml pero ningún "
        "término de include_title_terms lo reconoce. Añade su cabeza de rol a la "
        "lista blanca o el recolector nunca traerá esas vacantes."
    )


# ---------------------------------------------------------------------------
#  2. Sí dejar fuera lo que no es un puesto de dirección
# ---------------------------------------------------------------------------

# Títulos REALES de la misma corrida que eran ruido. La lista negra ya mataba
# a la mayoría; lo que se comprueba aquí es que la blanca los mataría IGUAL
# aunque un día alguien afloje la negra.
NO_SON_DIRECCION = [
    "Senior Software Developer",
    "Investment Management Analyst",
    "Senior Credit Analyst Monterrey",
    "Sr Business Strategy Associate",
    "Ejecutivo de Cuenta",
    "Recepcionista bilingüe",
    "Operador de montacargas",
    "Technical Program Owner",
]


@pytest.mark.parametrize("titulo", NO_SON_DIRECCION)
def test_la_lista_blanca_deja_fuera_lo_que_no_dirige(titulo: str):
    culpable = contains_any_term(titulo, _lista_blanca())
    assert culpable is None, (
        f"«{titulo}» no es un puesto de dirección y la lista blanca lo deja pasar "
        f"por «{culpable}». Ese término es demasiado ancho: cada oferta que entra de "
        "más es una llamada al LLM que se paga."
    )


def test_la_lista_blanca_compara_por_palabra_completa():
    """El riesgo simétrico al de la lista negra, pero al revés.

    Si «vp» o «ceo» coincidieran por substring, entrarían títulos que no tienen
    nada que ver. Y «director» no puede colarse dentro de «directorio».
    """
    blanca = _lista_blanca()
    assert contains_any_term("Analista de Directorio Telefónico", blanca) is None
    assert contains_any_term("Vpn Network Technician", blanca) is None
    assert contains_any_term("Chief Operating Officer", blanca) is not None


# ---------------------------------------------------------------------------
#  3. La precedencia entre filtros, sobre el pipeline de verdad
# ---------------------------------------------------------------------------
#  Las de arriba prueban las listas. Estas prueban `apply_filters`, que es
#  quien decide en qué orden opinan.

COMBO = KeywordCombo(terms=("COO", "Logistics"))


def _oferta(titulo: str, descripcion: str = "Puesto ejecutivo.") -> JobOffer:
    """Oferta mínima en Texas, reciente y sin nada que la descarte salvo el título."""
    return JobOffer(
        source="adzuna",
        market_key="texas_us",
        query_label=COMBO.label,
        external_id="1",
        title=titulo,
        company="Acme",
        location_raw="Houston, TX",
        location_city="Houston",
        location_region="Texas",
        location_country="US",
        url="https://example.test/1",
        description=descripcion,
        posted_at=datetime.now(timezone.utc) - timedelta(days=2),
    )


def _filtrar(titulo: str, descripcion: str = "Puesto ejecutivo."):
    config = load_config()
    return apply_filters(
        _oferta(titulo, descripcion),
        profile=config.profile(),
        market=config.market("texas_us"),
        combo=COMBO,
    )


def test_la_lista_negra_gana_a_la_lista_blanca():
    """«Junior Operations Director» tiene que caer, y caer por junior.

    Las dos listas opinan sobre ese título y tienen que ganar la que descarta:
    un puesto de entrada no deja de serlo porque lleve «Director» en el nombre.
    Se comprueba también el MOTIVO, no solo que caiga: si un día cayera por
    «fuera de los puestos objetivo», el reporte le estaría contando a la
    clienta una historia falsa sobre su mercado.
    """
    resultado = _filtrar("Junior Operations Director")
    assert resultado.dropped
    assert resultado.drop_reason == DROP_TITLE


def test_un_titulo_fuera_de_objetivo_cae_con_su_propio_motivo():
    resultado = _filtrar("Senior Software Developer")
    assert resultado.dropped
    # Cae por la negra («developer»), que va antes. Lo que se fija aquí es que
    # NO sobrevive, venga por donde venga.
    assert resultado.drop_reason in (DROP_TITLE, DROP_TITLE_OFF_TARGET)

    # Este no está en ninguna lista negra: solo lo puede parar la blanca.
    resultado = _filtrar("Operador de montacargas")
    assert resultado.dropped
    assert resultado.drop_reason == DROP_TITLE_OFF_TARGET


def test_la_lista_blanca_sustituye_a_require_all_terms():
    """El cambio de producto del 31/08, fijado como prueba.

    Un COO de una potabilizadora no tiene ninguna obligación de escribir
    «Logistics» en su anuncio. Con el filtro viejo esta oferta caía; con
    la lista blanca pasa. Si alguien reactiva `require_all_terms_in_text` sin
    vaciar la lista blanca, esta prueba avisa.
    """
    resultado = _filtrar(
        "Chief Operating Officer",
        descripcion="Dirigirá la operación de una planta potabilizadora municipal.",
    )
    assert resultado.kept, (
        f"descartada por «{resultado.drop_reason}»: {resultado.drop_detail}. "
        "El anuncio no dice «Logistics» y no tiene por qué decirlo."
    )


def test_vaciar_la_lista_blanca_devuelve_el_mando_al_filtro_viejo():
    """La lista blanca se puede desactivar, y desactivarla no rompe nada.

    Es la salida de emergencia documentada en el YAML: si un día la lista da
    problemas, se vacía y el proyecto vuelve al comportamiento anterior sin
    tocar código.
    """
    import dataclasses

    config = load_config()
    perfil = config.profile()
    sin_blanca = dataclasses.replace(
        perfil,
        hard_filters=dataclasses.replace(
            perfil.hard_filters,
            include_title_terms=[],
            require_all_terms_in_text=True,
        ),
    )
    assert not sin_blanca.hard_filters.uses_title_whitelist

    resultado = apply_filters(
        _oferta("Chief Operating Officer", "Dirigirá una planta potabilizadora."),
        profile=sin_blanca,
        market=config.market("texas_us"),
        combo=COMBO,
    )
    assert resultado.dropped
    assert resultado.drop_reason == "términos ausentes en el texto"


# ---------------------------------------------------------------------------
#  Un mercado sin divisa evalúa mal el salario, y no avisa
# ---------------------------------------------------------------------------

class TestCadaMercadoTieneDivisa:
    """Todo mercado del YAML tiene que estar en `CURRENCY_BY_COUNTRY`.

    ESTA PRUEBA EXISTE PORQUE EL FALLO YA PASÓ. Alemania entró en el perfil el
    4 septiembre 2026 y nadie añadió `de` al diccionario de divisas del conector
    de Adzuna: las 36 ofertas alemanas de la base se guardaron con
    `salary_currency` a NULL, y `recolector/salary.py` no puede comparar una
    cifra sin divisa. Resultado: el umbral de 170.000 USD de la clienta era
    ciego sobre medio inventario, en silencio, y ni una prueba se cayó.

    No se cayó porque el salario se evalúa en modo `report` —no descarta— y
    porque Adzuna Alemania casi nunca declara sueldo: 1 de 36. Los dos motivos
    son suerte, no diseño.

    Es la regla nº 9 de la casa —un techo se comprueba antes de llamar— aplicada
    a la configuración: añadir un mercado no puede dejar una pieza a medias sin
    que nadie se entere.
    """

    def test_todos_los_mercados_del_perfil_tienen_divisa(self):
        from recolector.connectors.adzuna import CURRENCY_BY_COUNTRY

        config = load_config()
        sin_divisa = [
            f"{clave} (adzuna.country={config.market(clave).adzuna.country!r})"
            for clave in config.profile().market_keys
            if config.market(clave).adzuna.country not in CURRENCY_BY_COUNTRY
        ]
        assert not sin_divisa, (
            "estos mercados guardarían el salario sin divisa y caerían siempre "
            f"en el cubo «desconocido»: {', '.join(sin_divisa)}. "
            "Añade su código de país a CURRENCY_BY_COUNTRY en "
            "recolector/connectors/adzuna.py."
        )

    def test_alemania_da_euros(self):
        """El caso concreto que faltaba, fijado."""
        from recolector.connectors.adzuna import CURRENCY_BY_COUNTRY

        assert CURRENCY_BY_COUNTRY.get("de") == "EUR"


# ---------------------------------------------------------------------------
#  La lista negra en el idioma del inventario (8 septiembre 2026)
# ---------------------------------------------------------------------------

# Títulos REALES de la base del 8 septiembre 2026 que el triaje o el
# clasificador ya habían rechazado, y que la lista negra no paraba porque
# estaba escrita en español para un inventario mexicano que ya no existe.
# Cada uno costó una llamada al triaje, y seis de ellos una lectura entera.
RUIDO_REAL_EN_INGLES = [
    "Crane & Rigging Superintendent",
    "RN / Registered Nurse -Regional Director, Nursing",
    "Director, Tax Compliance",
    "Assistant Director of Housekeeping",
    "Director, Head of FP&A",
    "Chief of Staff",
    "Senior Director, Information and Cybersecurity",
    "Vice President Information Technology",
    "Senior Software Developer",
    "Technical Director, Global Cloud Operations",
]


@pytest.mark.parametrize("titulo", RUIDO_REAL_EN_INGLES)
def test_la_lista_negra_para_el_ruido_en_ingles(titulo: str):
    """Lo que entraba solo porque la lista estaba en el idioma equivocado.

    Medido sobre los 344 anuncios guardados: estos términos habrían dejado
    fuera 38, el 11% del inventario, sin perder ni uno de los 58 relevantes.
    """
    assert contains_any_term(titulo, _lista_negra()) is not None, (
        f"«{titulo}» es ruido medido —lo rechazaron el triaje o el "
        "clasificador— y la lista negra lo deja entrar. Cada uno de estos "
        "cuesta al menos una llamada al triaje."
    )


@pytest.mark.parametrize("titulo", RUIDO_REAL_EN_INGLES)
def test_y_al_ampliarla_no_se_llevo_por_delante_ninguna_buena(titulo: str):
    """La otra mitad: que ninguno de los términos nuevos sea demasiado ancho.

    Se comprueba sobre las buenas de siempre, que es donde dolería.
    """
    for buena in BUENAS_REALES:
        culpable = contains_any_term(buena, _lista_negra())
        assert culpable is None, (
            f"«{buena}» es una oferta BUENA y la lista negra la mata por "
            f"«{culpable}»: ese término es demasiado ancho."
        )


def test_dirigir_un_centro_de_datos_sigue_pasando():
    """El término que se midió y se dejó FUERA de la lista, a propósito.

    «data center» habría parado cuatro anuncios, pero uno de ellos —este— el
    clasificador lo había marcado RELEVANTE: dirigir un centro de datos es
    dirigir logística, que es exactamente su perfil. Un término que
    ahorra tres anuncios y se lleva uno bueno no compensa, porque lo que cae
    aquí no llega ni a la bandeja.
    """
    titulo = "VP, Data Center Delivery (Dallas)"
    culpable = contains_any_term(titulo, _lista_negra())

    assert culpable is None, (
        f"«{titulo}» estaba marcada como RELEVANTE en la base real y la lista "
        f"negra la mata por «{culpable}»"
    )
    assert contains_any_term(titulo, _lista_blanca()) is not None
