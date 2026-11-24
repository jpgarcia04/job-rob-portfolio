"""
La cáscara de escritorio. Es lo que hay detrás del icono del escritorio.

    .venv\\Scripts\\pythonw.exe herramientas\\abrir.py

QUÉ RESUELVE
------------
Hasta hoy la app se abría con `python -m bandeja`, y eso son tres cosas que
ella no puede ver nunca: una consola negra, un navegador con barra de
direcciones, y una URL con un puerto. Esto las tapa las tres: un icono, una
ventana, y la app dentro.

Por debajo sigue siendo FastAPI y HTML —igual que Electron o Tauri—, y eso no
cambia. Lo que cambia es que **no hay ningún camino por el que ella vea el
andamio.**

POR QUÉ EL NAVEGADOR EN MODO `--app` Y NO `pywebview`
-----------------------------------------------------
Se comparon las dos el 3 septiembre 2026, y la diferencia no es de gusto:

    pywebview  ->  6 paquetes nuevos: pythonnet (un puente a .NET),
                   clr_loader, cffi, pycparser, bottle y proxy_tools.
                   `proxy_tools` viene como sdist, o sea que **pip tiene que
                   compilarlo en la máquina de la clienta**.
    --app      ->  cero dependencias nuevas. Edge está en todo Windows 11.

Una dependencia nueva es algo que `actualizar.ps1` tendrá que instalar solo, a
9.000 km, el día que cambie `requirements.txt`. Un sdist que hay que compilar
es exactamente el fallo que dejaría la app muerta sin nadie que lo vea. Se
eligió lo que no puede fallar.

Probado ejecutando, no leyendo: con un perfil recién creado, Edge abre y **pide
la página** (`/` y `/favicon.ico` llegaron al servidor), sin diálogo de
bienvenida. Perfil: 15,3 MB.

LAS CUATRO DECISIONES QUE LO HACEN SEGURO
-----------------------------------------
1. **Perfil propio** (`data/ventana/`). La ventana no comparte sesión con su
   navegador: ni sus cookies entran aquí, ni cerrar Edge cierra la app, ni
   abrir la app le toca las pestañas que tenía abiertas.

2. **Sin consola, pero con log.** Corre bajo `pythonw.exe`, que no abre
   ventana; a cambio, `print` y las trazas no van a ninguna parte. Así que todo
   lo que el servidor escriba se recoge y se vuelca en `data/logs/`, que es
   justo el archivo que ella va a mandar cuando algo falle. Se gana la ventana
   sin perder el diagnóstico.

3. **Si el servidor no arranca, se dice con una ventana.** Sin consola, un
   fallo de arranque sería la app que «no hace nada al pulsar el icono», que es
   lo único peor que un error. `MessageBoxW` es de Windows, no es una
   dependencia, y dice qué pasó y dónde está el log.

4. **Al cerrar la ventana se mata el ÁRBOL, no el servidor.** El servidor lanza
   corridas como procesos hijos, y esos hijos gastan dinero y escriben en
   `data/uso_llm.json`. Matar solo al padre dejaría una corrida huérfana
   gastando sin nadie mirando, y la siguiente apertura de la app se encontraría
   dos escritores del mismo libro de gastos. `taskkill /T` se los lleva a
   todos.

Y UNA QUE NO ES OBVIA: cerrar la ventana a mitad de una corrida la para, y lo
hecho se queda hecho. Es el mismo final que el botón «Parar» y que quedarse sin
saldo — cada clasificación se guarda en cuanto se tiene y cada gasto se apunta
antes de guardarla. Se apunta en el log para que se pueda leer desde lejos.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from recolector.registro import COPIAS_DE_RESERVA, TAMANO_MAXIMO_BYTES  # noqa: E402
from recolector.version import version_actual  # noqa: E402

HOST, PUERTO = "127.0.0.1", 8765

# Cuánto se espera a que el servidor conteste antes de dar el arranque por
# fallido. Un arranque normal son ~2 s; 40 da margen a un disco lento o a un
# antivirus mirando el ejecutable la primera vez, sin dejarla mirando un icono
# muerto un minuto entero.
ESPERA_MAXIMA_S = 40

# Ventanas de consola: en Windows, un proceso sin consola que lanza un hijo con
# consola le abre una nueva. Sin esto, cada corrida del clasificador haría
# parpadear una ventana negra en su pantalla.
SIN_VENTANA = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

NAVEGADORES = (
    # Edge primero: está en todo Windows 11 y es el que no hay que instalar.
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)


# ---------------------------------------------------------------------------
#  El log del arranque
# ---------------------------------------------------------------------------

def ruta_del_log(datos: Path) -> Path:
    return datos / "logs" / "arranque.log"


def log(datos: Path = RAIZ / "data") -> "logging.Logger":
    """Un archivo PROPIO, y no el de la app. La razón es de Windows.

    `recolector/registro.py` rota `job-rob.log` al llegar a 1 MB, y rotar es
    renombrar. En Windows no se puede renombrar un archivo que otro proceso
    tiene abierto, así que si la cáscara y el servidor escribieran los dos ahí,
    la rotación fallaría —dentro de `logging`, que se traga sus propios
    errores— y el único diagnóstico que existe se perdería en silencio. Justo
    el archivo que no puede romperse.

    Así que la cáscara escribe en `arranque.log`, al lado de `actualizar.log`,
    que es donde ya viven las dos cosas que pasan ANTES de que la app exista.
    El botón «Si algo va mal» de la pantalla abre la carpeta entera, no un
    archivo, para que estén los tres.
    """
    import logging
    import logging.handlers

    registro = logging.getLogger("job-rob-arranque")
    if not registro.handlers:
        destino = ruta_del_log(datos)
        destino.parent.mkdir(parents=True, exist_ok=True)
        manejador = logging.handlers.RotatingFileHandler(
            destino, maxBytes=TAMANO_MAXIMO_BYTES,
            backupCount=COPIAS_DE_RESERVA, encoding="utf-8",
        )
        manejador.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
        ))
        registro.addHandler(manejador)
        registro.setLevel(logging.INFO)
        registro.propagate = False
    return registro


# ---------------------------------------------------------------------------
#  Avisar sin consola
# ---------------------------------------------------------------------------

def decirlo_en_pantalla(titulo: str, mensaje: str) -> None:
    """Un cuadro de diálogo de Windows. Sin dependencias y sin consola.

    Solo para lo que impide que la app se abra. Un aviso que no bloquea el
    arranque va al log y nada más: interrumpirla con una ventana por algo que
    no le impide trabajar es enseñarle nuestro andamio.
    """
    log().error("%s: %s", titulo, mensaje.replace("\n", " "))
    if sys.platform != "win32":
        print(f"{titulo}\n{mensaje}", file=sys.stderr)
        return
    try:
        import ctypes

        # 0x10 = icono de error, 0x40000 = por delante de las demás ventanas.
        ctypes.windll.user32.MessageBoxW(0, mensaje, titulo, 0x10 | 0x40000)
    except Exception:  # pragma: no cover - si ni esto va, no queda nada
        pass


# ---------------------------------------------------------------------------
#  Las piezas
# ---------------------------------------------------------------------------

def actualizar(raiz: Path = RAIZ) -> None:
    """Trae la última versión publicada. NUNCA impide arrancar.

    El script ya está escrito para no fallar nunca (`exit 0` siempre), pero
    aquí se vuelve a proteger: si PowerShell no existiera, o la política lo
    bloqueara, la app se abre igual. Un actualizador que deja la herramienta
    muerta cuando el wifi va mal es peor que no tener ninguno.
    """
    script = raiz / "herramientas" / "actualizar.ps1"
    if not script.exists():
        log().warning("no está %s: se arranca sin actualizar", script.name)
        return
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            cwd=raiz, timeout=120, capture_output=True, creationflags=SIN_VENTANA,
        )
    except Exception as exc:
        log().warning("el actualizador no pudo correr: %s", exc)


def _python_sin_consola(raiz: Path) -> Path:
    """El intérprete del entorno virtual, en su variante sin ventana.

    `pythonw.exe` importa para los NIETOS: el servidor lanza las corridas con
    `sys.executable`, así que si el servidor corriera bajo `python.exe` cada
    corrida heredaría una consola. Con `pythonw` no hay ninguna en toda la
    cadena. `SIN_VENTANA` cubre lo mismo por el otro lado; las dos cosas juntas
    porque esto se prueba en una máquina y se ejecuta en otra.
    """
    for nombre in ("pythonw.exe", "python.exe", "python"):
        candidato = raiz / ".venv" / "Scripts" / nombre
        if candidato.exists():
            return candidato
    return Path(sys.executable)


def arrancar_servidor(raiz: Path = RAIZ) -> subprocess.Popen:
    """Levanta `python -m bandeja` sin navegador y sin ventana.

    Se lanza como PROCESO y no en un hilo a propósito: si el servidor revienta,
    esto lo ve, lo apunta y puede decírselo; con un hilo, un fallo se llevaría
    por delante también a la ventana y no quedaría nadie para contarlo.
    """
    entorno = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return subprocess.Popen(
        [str(_python_sin_consola(raiz)), "-m", "bandeja", "--sin-navegador"],
        cwd=raiz, env=entorno,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=SIN_VENTANA,
    )


def recoger_salida(proceso: subprocess.Popen, memoria: list[str]) -> threading.Thread:
    """Vuelca al log lo que el servidor escriba. En un hilo, porque si nadie
    lee la tubería el servidor se bloquea al llenarla.

    `memoria` guarda las últimas líneas para poder enseñarlas en el cuadro de
    diálogo si el arranque falla: «no arrancó» sin el motivo no sirve de nada.
    """
    def bombear() -> None:
        registro = log()
        for linea in proceso.stdout or ():
            linea = linea.rstrip()
            if not linea:
                continue
            registro.info("%s", linea)
            memoria.append(linea)
            del memoria[:-40]

    hilo = threading.Thread(target=bombear, daemon=True, name="salida-bandeja")
    hilo.start()
    return hilo


def esperar_al_servidor(proceso: subprocess.Popen, segundos: float = ESPERA_MAXIMA_S,
                        host: str = HOST, puerto: int = PUERTO) -> bool:
    """¿Contesta ya? Se pregunta al PUERTO, no al reloj.

    Un `sleep` fijo es la forma de que esto funcione en la máquina del
    desarrollador y falle en la de ella: el arranque tarda lo que tarde el
    disco. Y se vigila también que el proceso siga vivo, para no esperar
    cuarenta segundos a algo que murió en el primer segundo.
    """
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if proceso.poll() is not None:
            return False
        try:
            with socket.create_connection((host, puerto), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def hay_alguien_en_el_puerto(host: str = HOST, puerto: int = PUERTO) -> bool:
    """¿Ya contesta alguien ahí? Se pregunta ANTES de levantar nada.

    POR QUÉ EXISTE (medido el 7 septiembre 2026). `esperar_al_servidor`
    pregunta al puerto, y el puerto no dice de quién es. Con una copia ya
    abierta, la segunda escribía en el log «servidor en pie» un segundo después
    de arrancar —cuando su propio servidor no había podido tomar el puerto, que
    lo tenía la primera— y seguía adelante como si fuera suyo. Aquella vez
    terminó bien de casualidad: mató un proceso que ya estaba muerto. Con el
    orden de carrera al revés, la copia nueva se lleva por delante la que
    funciona, y ella ve morir la ventana que estaba usando.

    Preguntar antes convierte ese caso en lo que de verdad es: la app ya está
    abierta, y lo que hay que hacer es traerla al frente, no levantar otra.
    """
    try:
        with socket.create_connection((host, puerto), timeout=0.4):
            return True
    except OSError:
        return False


def perfil_en_uso(perfil: Path) -> bool:
    """¿Queda alguna ventana de Edge viva sobre este perfil?

    LA PREGUNTA QUE `ventana.wait()` NO CONTESTA, Y ES EL FALLO DEL ARRANQUE.
    Chromium admite UNA sesión por carpeta de perfil: si ya hay una instancia
    viva, el proceso que se lanza le pasa la URL y termina en el acto. Como
    `abrir.py` esperaba a ESE proceso, leía su muerte como «ella ha cerrado la
    ventana» y hacía `taskkill /F /T` sobre el servidor que acababa de
    levantar. Resultado medido: servidor en pie en el segundo 24 y muerto en el
    30, con una ventana en pantalla que no puede conectarse a nada. Y como esa
    ventana muerta sigue abierta, el siguiente doble clic repite el ciclo.

    La señal es el `lockfile` que Chromium deja en la raíz del perfil: existe
    mientras la sesión vive y desaparece al cerrarla (verificado ejecutando el
    7 sept 2026, abriendo y cerrando la app). Se exige ADEMÁS no poder abrirlo
    en exclusiva, porque un `lockfile` que quedó de un cierre a lo bruto sí se
    puede abrir, y creerlo vivo dejaría el servidor colgado para siempre.

    No se enumeran procesos a propósito: por nombre se contaría también su Edge
    personal, y para leer la línea de órdenes hay que llamar a PowerShell en
    bucle desde una cáscara que tiene que ser barata y no abrir ventanas.
    """
    lock = perfil / "lockfile"
    if not lock.exists():
        return False
    try:
        with open(lock, "r+b"):
            return False  # se pudo abrir: nadie lo tiene, es un resto
    except OSError:
        return True


def esperar_a_que_se_cierre_la_ventana(perfil: Path, proceso: Any = None,
                                       gracia_s: float = 5.0,
                                       tope_de_arranque_s: float = 60.0,
                                       intervalo_s: float = 1.0) -> None:
    """Se queda aquí mientras quede una ventana sobre el perfil.

    ES LA ÚNICA SEÑAL, Y ANTES NO LO ERA (medido el 9 septiembre 2026).
    Hasta hoy `main` hacía `ventana.wait()` PRIMERO y solo después miraba el
    perfil. Eso cubre que Chromium delegue y el proceso lanzado muera en el
    acto; no cubre el caso contrario, que es el que pasa cuando ella ya tiene
    su Edge abierto: el proceso que lanzamos **sobrevive al cierre de la
    ventana**, `wait()` no vuelve nunca, y con eso:

        se cerró la ventana ......... 03:14:25
        la corrida siguió leyendo ... hasta 03:15:14  (3 llamadas más, pagadas)
        el servidor seguía en pie ... 03:16:09, escuchando en el 8765

    O sea justo lo contrario de lo que dice AYUDA.md —«cerrar la ventana hace
    lo mismo que Parar»— y de lo que promete la cabecera de este archivo. El
    `lockfile` sí contesta bien a las dos preguntas: medido, aparece al abrir
    y desaparece **a los 2 segundos** de cerrar. Así que se pregunta a él y no
    al proceso.

    `proceso` es solo para el arranque: mientras el que lanzamos siga vivo se
    le renueva la gracia, porque un Edge que tarda en escribir su `lockfile`
    en una máquina lenta no es una ventana cerrada. `tope_de_arranque_s` es el
    freno de ese bucle: si en un minuto no ha aparecido el candado, es que no
    hay ventana ninguna y quedarse esperando sería dejar el servidor vivo para
    siempre — el mismo agujero por el otro lado.

    La segunda espera NO lleva tope: si la ventana lleva tres horas abierta,
    se esperan tres horas. Un tope ahí sería volver a matarle el servidor por
    debajo.
    """
    limite_duro = time.monotonic() + tope_de_arranque_s
    limite = time.monotonic() + gracia_s
    while not perfil_en_uso(perfil):
        ahora = time.monotonic()
        if ahora >= limite_duro:
            return  # ni candado ni paciencia: no llegó a abrirse ninguna ventana
        if proceso is not None and proceso.poll() is None:
            limite = min(ahora + gracia_s, limite_duro)
        elif ahora >= limite:
            return
        time.sleep(0.25)

    while perfil_en_uso(perfil):
        time.sleep(intervalo_s)


def buscar_navegador(candidatos: tuple[Path, ...] = NAVEGADORES) -> Path | None:
    for ruta in candidatos:
        if ruta.exists():
            return ruta
    return None


# LO QUE HACE FALTA PARA QUE LA PRIMERA PANTALLA SEA LA APP Y NO UN AVISO.
#
# EL FALLO (medido el 9 septiembre 2026, con perfil recién creado y repetido
# dos veces para descartar la casualidad). Windows 11 viene con la cuenta de
# Microsoft del equipo iniciada, y Edge la hereda solo: al abrir la app por
# primera vez, encima de la bandeja aparecía un cuadro a media pantalla —
#
#     «Se están sincronizando los datos de exploración en todos tus
#      dispositivos en este momento. <su correo> ha iniciado sesión en este
#      dispositivo, por lo que también hemos iniciado tu sesión en Microsoft
#      Edge. Siempre tendrás acceso a tus favoritos, contraseñas, historial…»
#
# — que tapa la app entera, lleva su dirección de correo dentro, y no se
# quita con la tecla de escape. Es lo primero que ve de una herramienta que
# se le acaba de entregar, y no lo dice ella: lo dice Microsoft.
#
# Y ADEMÁS DESMENTÍA LA DECISIÓN Nº 1 DE ESTE ARCHIVO. `--user-data-dir` le da
# a la app una carpeta de perfil propia, pero no impide que Edge firme ese
# perfil con su cuenta y se ponga a sincronizar. O sea que «ni sus cookies
# entran aquí» era verdad a medias: la identidad sí entraba.
#
# Comprobado ejecutando las dos versiones seguidas, mismo servidor, perfiles
# nuevos: con estas banderas la ventana abre limpia y sin ningún cuadro.
BANDERAS_SIN_MICROSOFT = (
    # No sincronizar nada con la cuenta, ni aunque la sesión se inicie.
    "--disable-sync",
    # Y no iniciarla: `msImplicitSignin` es justo la que hereda la cuenta de
    # Windows. Las otras dos son el mismo camino por sus otros nombres; las
    # que no existan en una versión dada de Edge se ignoran sin fallar.
    "--disable-features=msImplicitSignin,msEdgeIdentitySignIn,EdgeSyncPromo",
)


def abrir_ventana(navegador: Path, url: str, perfil: Path) -> subprocess.Popen:
    """La ventana: sin barra de direcciones, sin pestañas y con su propio icono.

    `--app=` es lo que quita el cromo del navegador. `--user-data-dir` es lo que
    la separa de su sesión: sin él, la app sería una ventana más de su Edge y
    cerrar el navegador cerraría la herramienta. Y `BANDERAS_SIN_MICROSOFT` es
    lo que impide que Edge la firme con su cuenta y le tape la app con un
    aviso de sincronización — ver el comentario de arriba.
    """
    perfil.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [str(navegador), f"--app={url}", f"--user-data-dir={perfil}",
         "--no-first-run", "--no-default-browser-check", "--window-size=1360,900",
         *BANDERAS_SIN_MICROSOFT],
        creationflags=SIN_VENTANA,
    )


def matar_el_arbol(proceso: subprocess.Popen) -> None:
    """Se lleva al servidor Y a las corridas que tuviera lanzadas.

    `kill()` a secas mataría solo al padre y dejaría un `python -m clasificador`
    huérfano gastando dinero contra `data/uso_llm.json`, sin nadie mirándolo y
    sin forma de pararlo desde la app. Ese archivo se escribe entero desde
    memoria, así que un segundo escritor no solo gasta: borra apuntes.
    """
    if proceso.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proceso.pid)],
                       capture_output=True, creationflags=SIN_VENTANA)
    else:  # pragma: no cover - el proyecto corre en Windows
        proceso.kill()
    try:
        proceso.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        log().warning("el servidor no terminó tras el taskkill")


# ---------------------------------------------------------------------------
#  El arranque entero
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    opciones = argparse.ArgumentParser(
        description="Abre la bandeja como una aplicación de escritorio.",
    )
    opciones.add_argument("--sin-actualizar", action="store_true",
                          help="No busca versión nueva antes de abrir.")
    opciones.add_argument("--sin-ventana", action="store_true",
                          help="Levanta el servidor y sale. Para comprobar la instalación.")
    args = opciones.parse_args(argv)

    datos = RAIZ / "data"
    registro = log(datos)
    registro.info("--- abriendo la app — versión %s", version_actual().para_log())

    if not args.sin_actualizar:
        actualizar()


    # SI YA ESTÁ ABIERTA, NO SE LEVANTA OTRA: SE TRAE LA SUYA AL FRENTE.
    #
    # `AYUDA.md` le pide que no vuelva a pulsar el icono mientras espera, y el
    # arranque tarda ~25 segundos: es un gesto que va a hacer. Antes, esa
    # segunda copia arrancaba un servidor que no podía tomar el puerto, daba
    # por bueno el de la primera —ver `hay_alguien_en_el_puerto`— y acababa
    # llamando a `matar_el_arbol`. Ahora se sale por arriba y no se toca nada
    # de la copia que está funcionando. Lanzar Edge sobre el mismo perfil
    # levanta la ventana existente, que es justo lo que ella quería al pulsar.
    if not args.sin_ventana and hay_alguien_en_el_puerto():
        registro.info("la bandeja ya estaba abierta: se trae su ventana al frente")
        navegador = buscar_navegador()
        if navegador is not None:
            abrir_ventana(navegador, f"http://{HOST}:{PUERTO}", datos / "ventana")
        return 0

    servidor = arrancar_servidor()
    ultimas: list[str] = []
    recoger_salida(servidor, ultimas)

    if not esperar_al_servidor(servidor):
        matar_el_arbol(servidor)
        decirlo_en_pantalla(
            "No se pudo abrir la bandeja",
            "La aplicación no llegó a arrancar.\n\n"
            + ("\n".join(ultimas[-6:]) or "El servidor terminó sin decir nada.")
            + f"\n\nMándale a Juan Pablo los archivos de esta carpeta:\n"
            + f"{ruta_del_log(datos).parent}",
        )
        return 1

    url = f"http://{HOST}:{PUERTO}"
    registro.info("servidor en pie: %s", url)

    if args.sin_ventana:
        registro.info("--sin-ventana: se para aquí")
        matar_el_arbol(servidor)
        return 0

    navegador = buscar_navegador()
    if navegador is None:
        # No se muere por esto: se abre en lo que haya, con barra de
        # direcciones y todo. Es feo, y es mejor que no abrirse.
        registro.warning("sin Edge ni Chrome: se abre en el navegador por defecto")
        import webbrowser

        webbrowser.open(url)
        decirlo_en_pantalla(
            "La bandeja se ha abierto en tu navegador",
            "No encontré Microsoft Edge ni Google Chrome, así que la app se ha "
            "abierto como una pestaña normal.\n\nFunciona igual. Para que se "
            "abra en su propia ventana, avísale a Juan Pablo.",
        )
        try:
            servidor.wait()
        except KeyboardInterrupt:  # pragma: no cover
            pass
        matar_el_arbol(servidor)
        return 0

    registro.info("ventana: %s", navegador.name)
    perfil = datos / "ventana"
    ventana = abrir_ventana(navegador, url, perfil)

    try:
        # NO SE ESPERA AL PROCESO: SE ESPERA A LA VENTANA.
        #
        # Esperar al proceso de Edge contestaba mal por los dos lados. Si
        # Chromium delega, el proceso muere en el acto y la espera volvía
        # enseguida: se mataba un servidor recién levantado (7 sept 2026). Y
        # si Chromium NO delega pero el proceso queda de dueño del navegador
        # —lo normal cuando ella ya tenía Edge abierto—, esa espera no vuelve
        # nunca y el servidor sobrevive al cierre, gastando (9 sept 2026).
        #
        # El `lockfile` del perfil contesta bien a las dos. El proceso se le
        # pasa solo para no dar por cerrada una ventana que todavía está
        # abriéndose. Ver `esperar_a_que_se_cierre_la_ventana`.
        esperar_a_que_se_cierre_la_ventana(perfil, ventana)
    except KeyboardInterrupt:  # pragma: no cover - solo al depurar desde consola
        registro.info("interrumpido desde la consola")

    registro.info("ventana cerrada: se para el servidor")
    matar_el_arbol(servidor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
