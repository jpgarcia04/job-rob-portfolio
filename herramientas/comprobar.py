"""
¿Está bien esta instalación? La respuesta, sin gastar un céntimo.

    python herramientas\\comprobar.py

ES LO QUE SE EJECUTA POR TELÉFONO. El desarrollador está en España, ella dice
«no me funciona», y esto es lo que convierte esa frase en una lista de líneas
que ella puede leer en voz alta. Sin esto, la conversación es adivinar.

POR QUÉ LA LÓGICA ESTÁ AQUÍ Y NO EN EL `.ps1`
---------------------------------------------
Porque en Python se puede PROBAR. La sesión anterior escribió un script de
PowerShell que parecía correcto y tenía tres fallos, y los tres solo salieron
al ejecutarlo. Lo que queda en `comprobar.ps1` es lo único que aquí no se puede
comprobar —si existe Python, para empezar—; todo lo demás vive aquí y tiene
pruebas en `tests/test_instalacion.py`.

QUÉ NO HACE
-----------
**No gasta.** Ni un token, ni una petición de cuota, ni una llamada al LLM.
Comprobar que la API key es válida exigiría una llamada de verdad, y una
herramienta de diagnóstico que cobra es una que ella no va a querer usar dos
veces. Lo que sí se comprueba es que la clave ESTÉ y no sea el hueco del
ejemplo, que es el fallo real: la de la plantilla se queda puesta.

La única línea que toca la red es la del acceso al repositorio, y es a
propósito: que su máquina pierda el acceso de lectura es el fallo que deja el
actualizador muerto **sin ningún síntoma visible**. Vale la pena preguntarlo.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

OK, AVISO, FALTA = "ok", "AVISO", "FALTA"

# Lo que hay en `.env.example` como hueco a rellenar. Si sigue puesto, la clave
# no está: es el fallo más probable de una instalación hecha con prisa.
PLANTILLAS = ("tu_app_id_aqui", "tu_app_key_aqui", "sk-ant-tu_key_aqui",
              "tu_key_de_mexico_aqui", "tu_key_de_estados_unidos_aqui")

PUERTO_BANDEJA = 8765

# Los huecos de `AYUDA.md`. Mismo criterio que `PLANTILLAS`: un hueco que
# sobrevive a la entrega no da ningún síntoma hasta el día que ella lo
# necesita. Ver `comprobar_ayuda`.
HUECOS_DE_AYUDA = ("PENDIENTE_CORREO", "PENDIENTE_WHATSAPP")


class Informe:
    """Las líneas del diagnóstico y el veredicto final.

    Se separan `FALTA` de `AVISO` porque no significan lo mismo y confundirlos
    haría inútil el veredicto: sin la clave de Anthropic la app no clasifica
    —eso es un FALTA—, pero que la versión no esté etiquetada solo importa en
    su máquina, no en la del desarrollador.
    """

    def __init__(self) -> None:
        self.lineas: list[tuple[str, str, str]] = []

    def anotar(self, estado: str, titulo: str, detalle: str = "") -> None:
        self.lineas.append((estado, titulo, detalle))

    @property
    def faltas(self) -> int:
        return sum(1 for estado, _, _ in self.lineas if estado == FALTA)

    @property
    def avisos(self) -> int:
        return sum(1 for estado, _, _ in self.lineas if estado == AVISO)

    def imprimir(self) -> None:
        for estado, titulo, detalle in self.lineas:
            marca = {OK: "[ok]   ", AVISO: "[AVISO]", FALTA: "[FALTA]"}[estado]
            print(f"  {marca} {titulo}")
            if detalle:
                for linea in detalle.splitlines():
                    print(f"          {linea}")


# ---------------------------------------------------------------------------
#  Las comprobaciones, una por función para poder probarlas sueltas
# ---------------------------------------------------------------------------

def comprobar_python(informe: Informe) -> None:
    version = ".".join(str(n) for n in sys.version_info[:3])
    if sys.version_info < (3, 10):
        informe.anotar(
            FALTA, f"Python {version}",
            "Hace falta Python 3.10 o superior.\n"
            "Se baja de https://www.python.org/downloads/windows/",
        )
    else:
        informe.anotar(OK, f"Python {version}")


def comprobar_entorno(informe: Informe, raiz: Path = RAIZ) -> None:
    """¿Se está corriendo dentro del entorno virtual del proyecto?

    Importa porque las dependencias se instalan ahí. Correr con el Python del
    sistema funciona hasta que falta `fastapi`, y entonces el síntoma es un
    `ModuleNotFoundError` que no dice nada de instalaciones.
    """
    venv = raiz / ".venv"
    if not venv.exists():
        informe.anotar(
            FALTA, "Entorno virtual (.venv)",
            f"No está en {venv}.\nEjecuta herramientas\\instalar.ps1",
        )
        return
    dentro = Path(sys.prefix).resolve() == venv.resolve()
    informe.anotar(OK if dentro else AVISO, "Entorno virtual (.venv)",
                   "" if dentro else "Existe, pero esto no se está ejecutando dentro de él.")


def comprobar_dependencias(informe: Informe) -> None:
    import importlib.util

    faltan = [
        nombre for nombre, modulo in (
            ("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
            ("anthropic", "anthropic"), ("PyYAML", "yaml"),
            ("python-dotenv", "dotenv"), ("requests", "requests"),
        )
        if importlib.util.find_spec(modulo) is None
    ]
    if faltan:
        informe.anotar(
            FALTA, "Dependencias",
            f"Faltan: {', '.join(faltan)}\n"
            ".venv\\Scripts\\python.exe -m pip install -r requirements.txt",
        )
    else:
        informe.anotar(OK, "Dependencias")


def comprobar_git(informe: Informe, raiz: Path = RAIZ, red: bool = True) -> None:
    """Git, el clon y el acceso de lectura al repositorio privado.

    LAS TRES SON EL ACTUALIZADOR. Si esta máquina no es un clon o no puede leer
    el repositorio, `actualizar.ps1` no trae nada — y lo hace en silencio, que
    es lo peor: la app sigue abriéndose con la versión vieja para siempre.
    """
    entorno = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")

    def git(*args: str, timeout: int = 20):
        try:
            return subprocess.run(
                ["git", "-C", str(raiz), *args], capture_output=True, text=True,
                timeout=timeout, env=entorno,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    prueba = git("rev-parse", "--is-inside-work-tree", timeout=10)
    if prueba is None:
        informe.anotar(
            FALTA, "Git",
            "No está instalado, o no responde.\n"
            "Se baja de https://git-scm.com/download/win\n"
            "Sin git, la app no se puede actualizar sola.",
        )
        return
    if prueba.returncode != 0:
        informe.anotar(
            FALTA, "Git",
            "Esta carpeta no es un clon de git: se copió suelta.\n"
            "El actualizador no puede trabajar sobre ella.",
        )
        return
    informe.anotar(OK, "Git")

    if not red:
        return

    remoto = git("ls-remote", "--tags", "--quiet", "origin", timeout=30)
    if remoto is None or remoto.returncode != 0:
        informe.anotar(
            AVISO, "Acceso al repositorio",
            "No se pudo leer el repositorio (¿sin red, o sin credenciales?).\n"
            "La app funciona igual, pero NO se actualizará sola.",
        )
    else:
        etiquetas = sum(1 for linea in remoto.stdout.splitlines() if "refs/tags/" in linea)
        if etiquetas:
            informe.anotar(OK, "Acceso al repositorio", f"{etiquetas} etiqueta(s) publicadas")
        else:
            # CERO ETIQUETAS DEJA EL ACTUALIZADOR INERTE, y en silencio.
            # `actualizar.ps1` solo trae lo etiquetado a propósito, así que sin
            # una sola etiqueta se limita a apuntar «no hay nada publicado» y
            # salir — para siempre. Es un fallo de ENTREGA, no de código, y por
            # eso no se ve mirando el programa: se ve preguntando al servidor.
            informe.anotar(
                AVISO, "Acceso al repositorio",
                "El repositorio no tiene NINGUNA etiqueta publicada.\n"
                "El actualizador no traerá nada hasta que exista una:\n"
                "  git tag -a v1.0.0 -m 'entrega' && git push origin v1.0.0",
            )


def comprobar_version(informe: Informe) -> None:
    from recolector.version import version_actual

    v = version_actual()
    if v.es_de_fiar:
        informe.anotar(OK, f"Versión {v.para_pantalla()}")
    else:
        informe.anotar(
            AVISO, f"Versión {v.para_pantalla()}",
            "No corresponde a ninguna versión publicada. En la máquina del\n"
            "desarrollador es normal; en la de la clienta significa que el\n"
            "actualizador no gobierna esta copia.",
        )


def comprobar_configuracion(informe: Informe) -> None:
    try:
        from recolector.config import load_config

        config = load_config()
        informe.anotar(OK, "Configuración", f"perfil {config.active_profile_key}")
    except Exception as exc:
        informe.anotar(FALTA, "Configuración", f"{type(exc).__name__}: {exc}")
        return

    try:
        from clasificador.fichas import cargar_fichas

        fichas = cargar_fichas()
        informe.anotar(OK, "Fichas de los CVs", f"{len(fichas.lista)} ficha(s): {', '.join(fichas.ids)}")
    except Exception as exc:
        informe.anotar(FALTA, "Fichas de los CVs", f"{type(exc).__name__}: {exc}")


def comprobar_claves(informe: Informe, raiz: Path = RAIZ) -> None:
    """Que estén, y que no sean el hueco de la plantilla.

    NO se comprueba que funcionen: eso costaría una llamada. La clave dejada a
    medio rellenar es el fallo que de verdad ocurre, y ese sí se ve gratis.
    """
    entorno = raiz / ".env"
    if not entorno.exists():
        informe.anotar(
            FALTA, "Archivo .env",
            f"No está en {entorno}.\nEjecuta herramientas\\instalar.ps1",
        )
        return
    informe.anotar(OK, "Archivo .env")

    valores: dict[str, str] = {}
    # `utf-8-sig` para que un BOM no convierta a `ADZUNA_APP_ID` en una clave
    # con otro nombre. Aquí importa el doble: si el diagnóstico se equivoca,
    # ella pasa la tarde buscando una clave que sí está — o al revés, la da por
    # buena cuando la app no la ve. Ver `recolector.config.cargar_env`.
    for linea in entorno.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            nombre, _, valor = linea.partition("=")
            valores[nombre.strip()] = valor.strip().strip('"').strip("'")

    for nombre, para_que in (
        ("ANTHROPIC_API_KEY", "sin ella la app no clasifica ni ordena"),
        ("ADZUNA_APP_ID", "sin ella no se pueden buscar ofertas nuevas"),
        ("ADZUNA_APP_KEY", "sin ella no se pueden buscar ofertas nuevas"),
    ):
        valor = valores.get(nombre, "")
        if not valor or valor in PLANTILLAS:
            informe.anotar(
                FALTA, nombre,
                f"Vacía o sin rellenar en .env — {para_que}.",
            )
        else:
            # NUNCA la clave entera: esto se lee en voz alta por teléfono y se
            # pega en correos. Los últimos cuatro bastan para distinguir la
            # suya de la del desarrollador, que es la única pregunta real.
            informe.anotar(OK, nombre, f"puesta (termina en …{valor[-4:]})")


def comprobar_base(informe: Informe) -> None:
    from recolector.config import load_config

    try:
        ruta = load_config().database_path
    except Exception:
        return  # ya se anotó en comprobar_configuracion

    if not ruta.exists():
        informe.anotar(
            AVISO, "Base de datos",
            "Todavía no hay. Se crea sola con la primera búsqueda de ofertas.",
        )
        return
    try:
        import sqlite3

        with sqlite3.connect(f"file:{ruta}?mode=ro", uri=True) as conexion:
            ofertas = conexion.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
            juzgadas = conexion.execute("SELECT COUNT(*) FROM clasificaciones").fetchone()[0]
        informe.anotar(OK, "Base de datos",
                       f"{ofertas} ofertas, {juzgadas} clasificadas")
    except Exception as exc:
        informe.anotar(FALTA, "Base de datos", f"No se puede leer: {exc}")


def comprobar_libro(informe: Informe) -> None:
    """El libro de gastos. Si está corrupto, la app se niega a arrancar.

    Y es correcto que se niegue —perder la cuenta cuesta dinero de otra
    persona—, pero desde su lado el síntoma sería «no abre». Aquí se ve.
    """
    try:
        from clasificador.presupuesto import LibroDeGastos, ruta_del_libro
        from recolector.config import load_config

        config = load_config()
        libro = LibroDeGastos(ruta_del_libro(config))
        estado = libro.estado(config.llm, es_manual=True)
        informe.anotar(
            OK, "Libro de gastos",
            f"${estado.gastado_mes:.4f} este mes de ${estado.techo_mensual:.2f} "
            f"· tope por acción ${estado.tope_por_accion:.2f}",
        )
    except Exception as exc:
        informe.anotar(FALTA, "Libro de gastos", f"{type(exc).__name__}: {exc}")


NOMBRE_DEL_ACCESO = "Bandeja de ofertas.lnk"


def escritorios() -> list[Path]:
    """Dónde puede estar de verdad el escritorio de esta máquina.

    `~/Desktop` NO VALE, y se comprobó ejecutando: en un equipo con OneDrive
    activado —y el suyo lo tendrá, igual que el del desarrollador— el
    escritorio real es `~/OneDrive/Escritorio`, con el nombre en el idioma de
    Windows. `instalar.ps1` lo hace bien porque usa `GetFolderPath('Desktop')`,
    que resuelve la redirección; esto tiene que mirar en el mismo sitio o diría
    «no está» sobre un acceso directo que sí existe — un diagnóstico que miente
    es peor que no tenerlo.

    En Python la vía equivalente es el registro. Las carpetas de reserva están
    por si el registro no se pudiera leer, no como método principal.
    """
    candidatos: list[Path] = []
    if sys.platform == "win32":
        try:
            import winreg

            clave = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, clave) as k:
                candidatos.append(Path(winreg.QueryValueEx(k, "Desktop")[0]))
        except OSError:
            pass

    casa = Path(os.path.expanduser("~"))
    candidatos += [
        casa / "Desktop", casa / "Escritorio",
        casa / "OneDrive" / "Desktop", casa / "OneDrive" / "Escritorio",
    ]
    vistos: list[Path] = []
    for ruta in candidatos:
        if ruta not in vistos:
            vistos.append(ruta)
    return vistos


def buscar_acceso_directo(nombre: str = NOMBRE_DEL_ACCESO) -> Path | None:
    for carpeta in escritorios():
        candidato = carpeta / nombre
        if candidato.exists():
            return candidato
    return None


def comprobar_ventana(informe: Informe, raiz: Path = RAIZ) -> None:
    """Lo que hace falta para que se abra como aplicación y no como pestaña."""
    from herramientas.abrir import buscar_navegador  # noqa: PLC0415

    navegador = buscar_navegador()
    if navegador is None:
        informe.anotar(
            AVISO, "Navegador para la ventana",
            "No hay Edge ni Chrome: la app se abrirá como una pestaña normal.",
        )
    else:
        informe.anotar(OK, "Navegador para la ventana", navegador.name)

    icono = raiz / "herramientas" / "job-rob.ico"
    if icono.exists():
        informe.anotar(OK, "Icono")
    else:
        informe.anotar(AVISO, "Icono",
                       "Falta job-rob.ico. python herramientas\\hacer_icono.py")

    acceso = buscar_acceso_directo()
    informe.anotar(
        OK if acceso else AVISO, "Acceso directo del escritorio",
        str(acceso.parent) if acceso else "No está. Lo crea herramientas\\instalar.ps1",
    )


def comprobar_puerto(informe: Informe, puerto: int = PUERTO_BANDEJA) -> None:
    """¿Está el puerto libre, o ya hay una bandeja abierta?

    Las dos son respuestas útiles y ninguna es un fallo. Sin esta línea, «no me
    abre» y «ya la tenía abierta» se ven igual.
    """
    try:
        with socket.create_connection(("127.0.0.1", puerto), timeout=0.5):
            informe.anotar(OK, f"Puerto {puerto}", "hay una bandeja abierta ahora mismo")
    except OSError:
        informe.anotar(OK, f"Puerto {puerto}", "libre")


# ---------------------------------------------------------------------------

def comprobar_ayuda(informe: Informe, raiz: Path = RAIZ) -> None:
    """Que la hoja de ayuda diga de verdad cómo localizar al desarrollador.

    POR QUÉ ESTO ES UNA COMPROBACIÓN Y NO UNA LÍNEA DEL GUION DE ENTREGA.
    `AYUDA.md` dice en su primera línea «si lo que no abre es la aplicación,
    escríbeme directamente […] al final está cómo localizarme», y el botón
    «Si algo va mal» copia un texto que dice «mándale esto a Juan Pablo». Los
    dos apuntaban, hasta el 9 septiembre 2026, a un sitio que no existía: la
    hoja no traía ni correo ni teléfono por ninguna parte.

    Es el peor hueco posible porque solo se nota el día que ella lo necesita,
    y ese día es justo cuando la app no arranca — o sea, cuando ni siquiera
    puede abrir esta hoja para buscar el dato. Un olvido así no da ningún
    síntoma hasta que ya es tarde, así que se le pone uno.

    Es AVISO y no FALTA a propósito: la app funciona perfectamente sin esto.
    Lo que no funciona es ella pidiendo ayuda. El guion de la entrega exige
    pasar este comprobador «sin ámbares», así que un aviso basta para que no
    viaje sin rellenar.
    """
    hoja = raiz / "AYUDA.md"
    if not hoja.exists():
        informe.anotar(FALTA, "Hoja de ayuda", f"no está {hoja}")
        return

    texto = hoja.read_text(encoding="utf-8")
    huecos = [p for p in HUECOS_DE_AYUDA if p in texto]
    if huecos:
        informe.anotar(
            AVISO, "Cómo localizarte en la ayuda",
            "AYUDA.md sigue con el hueco sin rellenar: "
            + ", ".join(huecos) + ".\n"
            "La hoja le dice que te escriba y no dice a dónde. Rellénalo\n"
            "ANTES de entregar, y dale el dato también fuera de la app.",
        )
        return

    informe.anotar(OK, "Cómo localizarte en la ayuda", "la hoja trae el contacto")


def revisar(red: bool = True) -> Informe:
    informe = Informe()
    comprobar_python(informe)
    comprobar_entorno(informe)
    comprobar_dependencias(informe)
    comprobar_git(informe, red=red)
    comprobar_version(informe)
    comprobar_configuracion(informe)
    comprobar_claves(informe)
    comprobar_base(informe)
    comprobar_libro(informe)
    comprobar_ventana(informe)
    comprobar_ayuda(informe)
    comprobar_puerto(informe)
    return informe


def main(argv: list[str] | None = None) -> int:
    import argparse

    opciones = argparse.ArgumentParser(
        description="Dice si esta instalación está bien. No gasta nada.",
    )
    opciones.add_argument("--sin-red", action="store_true",
                          help="No pregunta al repositorio. Todo lo demás es local igual.")
    args = opciones.parse_args(argv)

    # Acentos en la consola de Windows. Sin esto, la línea que ella lee en
    # voz alta por teléfono dice «Comprobaci?n» y «versi?n»: el mismo fallo que
    # la sesión anterior encontró en el .ps1, aquí por el otro lado.
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            try:
                flujo.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    print("Comprobación de la instalación")
    print(f"  carpeta: {RAIZ}\n")
    informe = revisar(red=not args.sin_red)
    informe.imprimir()

    print()
    if informe.faltas:
        print(f"{informe.faltas} cosa(s) sin resolver. La app no va a funcionar bien "
              "hasta arreglarlas.")
        print("Mándale esta pantalla entera a Juan Pablo.")
        return 1
    if informe.avisos:
        print(f"Todo lo esencial está. Hay {informe.avisos} aviso(s) arriba: "
              "la app funciona, pero conviene leerlos.")
        return 0
    print("Todo en orden. La app está lista.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
