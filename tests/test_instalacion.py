"""
La cáscara de escritorio, el instalador y el comprobador.

QUÉ SE PUEDE PROBAR AQUÍ Y QUÉ NO
---------------------------------
Lo que toca el arranque **no se revisa leyendo**: la sesión del 3 septiembre
2026 escribió un `.ps1` que parecía correcto y tenía tres fallos, y los tres
solo salieron al ejecutarlo contra un repositorio de usar y tirar. Así que la
prueba de verdad del instalador es correrlo en una carpeta limpia, y eso se
hizo a mano (queda escrito en el README).

Lo que sí se puede fijar aquí es lo que se rompe en silencio y no se ve al
ejecutarlo una vez:

  · que los `.ps1` sigan llevando BOM. Sin él, Windows PowerShell 5.1 los lee
    como ANSI y los acentos llegan rotos a la única persona que los va a leer.
    No falla nada: solo se vuelve ilegible.
  · que el comprobador distinga una clave PUESTA de una clave que se quedó con
    el hueco de la plantilla, que es el fallo real de una instalación con
    prisa.
  · que el comprobador busque el acceso directo donde de verdad está. Con
    OneDrive el escritorio NO es `~/Desktop`, y mirar ahí decía «no está» sobre
    un acceso directo que sí existía — un diagnóstico que miente es peor que no
    tener ninguno. Salió al ejecutar el instalador en el clon limpio.
  · que el comprobador NO gaste. Es la herramienta que se ejecuta por teléfono
    cuando algo va mal, y una que cobra es una que no se usa dos veces.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from herramientas import abrir as modulo_abrir  # noqa: E402
from herramientas import comprobar as modulo_comprobar  # noqa: E402
from herramientas import hacer_icono  # noqa: E402


# ---------------------------------------------------------------------------
#  Los scripts de PowerShell
# ---------------------------------------------------------------------------

class TestLosScriptsSePuedenLeer:
    """El BOM no es una preferencia: es que se lean los acentos.

    Windows PowerShell 5.1 lee un `.ps1` sin BOM como ANSI. El síntoma no es un
    error, es que «Comprobación» sale como «Comprobaci?n» — en el único texto
    que existe para que ella lo lea en voz alta por teléfono. Ya pasó una vez.
    """

    SCRIPTS = ("actualizar.ps1", "instalar.ps1", "comprobar.ps1")

    @pytest.mark.parametrize("nombre", SCRIPTS)
    def test_lleva_bom_utf8(self, nombre):
        datos = (RAIZ / "herramientas" / nombre).read_bytes()
        assert datos[:3] == b"\xef\xbb\xbf", (
            f"{nombre} se ha guardado sin BOM: sus acentos llegarán rotos."
        )

    @pytest.mark.parametrize("nombre", SCRIPTS)
    def test_y_el_contenido_es_utf8_valido(self, nombre):
        datos = (RAIZ / "herramientas" / nombre).read_bytes()
        datos[3:].decode("utf-8")  # revienta si no lo es

    def test_ninguna_funcion_se_llama_Git(self):
        """El fallo que colgó el actualizador sin escribir una línea.

        PowerShell resuelve los nombres buscando primero entre sus funciones,
        así que una función `Git` hace que `& git ...` se llame a sí misma.
        Recursión infinita, y el síntoma es exactamente el que estos scripts
        existen para no tener.
        """
        for nombre in self.SCRIPTS:
            texto = (RAIZ / "herramientas" / nombre).read_text(encoding="utf-8-sig")
            assert "function Git" not in texto, (
                f"{nombre} define una función llamada Git: recursión infinita."
            )

    def test_el_instalador_no_pisa_las_claves_que_ya_estan(self):
        """Reescribe SOLO la línea de Anthropic.

        Las de Adzuna viajan en el `.env` que se copia con la carpeta, y son las
        únicas credenciales sin las que no se puede buscar una sola oferta.
        Rehacer el archivo desde la plantilla las borraría.
        """
        texto = (RAIZ / "herramientas" / "instalar.ps1").read_text(encoding="utf-8-sig")
        assert "ANTHROPIC_API_KEY=$ClaveAnthropic" in texto

        # SE MIRA LO QUE ESCRIBE, NO LO QUE MENCIONA. La versión anterior
        # exigía que la palabra «ADZUNA» no apareciera en el paso 3, y eso
        # prohibía también EXPLICARLO en un comentario — justo cuando lo que
        # había que explicar era que este paso llegó a romper esa clave (con un
        # BOM, ver TestElEnvNoSeRompeConUnBOM). Una prueba que impide escribir
        # el porqué de un fallo empuja a borrar el porqué.
        paso = texto.split("Paso '3")[1].split("Paso '4")[0]
        for linea in paso.splitlines():
            codigo = linea.split("#")[0]
            assert "ADZUNA" not in codigo, (
                f"el paso de las claves no debe escribir nada de Adzuna: {linea!r}"
            )

    def test_el_acceso_directo_apunta_a_pythonw(self):
        """`python.exe` dejaría una consola negra detrás de la app toda la
        sesión, que es justo lo que la cáscara existe para quitar."""
        texto = (RAIZ / "herramientas" / "instalar.ps1").read_text(encoding="utf-8-sig")
        assert "pythonw.exe" in texto
        assert "GetFolderPath('Desktop')" in texto, (
            "con OneDrive el escritorio no es $HOME\\Desktop"
        )


# ---------------------------------------------------------------------------
#  El comprobador
# ---------------------------------------------------------------------------

class TestElComprobadorDistingueLoQueImporta:
    def test_una_clave_con_el_hueco_de_la_plantilla_cuenta_como_que_falta(self, tmp_path):
        """ES EL FALLO REAL DE UNA INSTALACIÓN CON PRISA.

        `.env.example` trae `ANTHROPIC_API_KEY=sk-ant-tu_key_aqui`. Si esa línea
        se queda, la variable EXISTE y tiene valor, así que cualquier
        comprobación de «¿está puesta?» diría que sí — y la app fallaría en la
        primera llamada, ya en su máquina.
        """
        (tmp_path / ".env").write_text(
            "ANTHROPIC_API_KEY=sk-ant-tu_key_aqui\n"
            "ADZUNA_APP_ID=tu_app_id_aqui\n"
            "ADZUNA_APP_KEY=de_verdad_1234\n",
            encoding="utf-8",
        )
        informe = modulo_comprobar.Informe()
        modulo_comprobar.comprobar_claves(informe, raiz=tmp_path)

        estados = {titulo: estado for estado, titulo, _ in informe.lineas}
        assert estados["ANTHROPIC_API_KEY"] == modulo_comprobar.FALTA
        assert estados["ADZUNA_APP_ID"] == modulo_comprobar.FALTA
        assert estados["ADZUNA_APP_KEY"] == modulo_comprobar.OK

    def test_no_ensena_la_clave_entera(self):
        """Esto se lee en voz alta por teléfono y se pega en correos.

        Los últimos cuatro caracteres bastan para la única pregunta real —«¿es
        la tuya o la mía?»— y no sirven para usar la clave.
        """
        import inspect

        fuente = inspect.getsource(modulo_comprobar.comprobar_claves)
        assert "valor[-4:]" in fuente
        assert "{valor}" not in fuente, "no se imprime la clave entera"

    def test_sin_env_lo_dice_y_no_revienta(self, tmp_path):
        informe = modulo_comprobar.Informe()
        modulo_comprobar.comprobar_claves(informe, raiz=tmp_path)
        estados = [estado for estado, _, _ in informe.lineas]
        assert modulo_comprobar.FALTA in estados

    def test_el_escritorio_incluye_el_de_OneDrive(self):
        """El fallo que salió al probar el instalador en el clon limpio.

        El acceso directo acabó en `C:\\Users\\...\\OneDrive\\Escritorio` y el
        comprobador miraba en `~/Desktop`: decía «no está» sobre algo que sí
        estaba. Reintroducirlo es volver a `Path.home() / "Desktop"`.
        """
        rutas = [str(r).lower() for r in modulo_comprobar.escritorios()]
        assert any("onedrive" in r for r in rutas) or sys.platform != "win32"
        assert len(rutas) == len(set(rutas)), "no debe repetir carpetas"

    def test_faltas_y_avisos_no_son_lo_mismo(self):
        """Sin la clave la app no clasifica; sin etiqueta de versión, sí.

        Meterlos en el mismo saco haría que el veredicto final —«¿está bien
        esta instalación?»— dejara de significar nada.
        """
        informe = modulo_comprobar.Informe()
        informe.anotar(modulo_comprobar.OK, "bien")
        informe.anotar(modulo_comprobar.AVISO, "regular")
        informe.anotar(modulo_comprobar.FALTA, "mal")
        assert informe.faltas == 1
        assert informe.avisos == 1

    def test_no_gasta_nada(self):
        """La única red que toca es `git ls-remote`, y con `--sin-red` ni eso.

        Un diagnóstico que cobra es uno que ella no va a querer usar dos veces,
        y este existe justo para usarse cuando algo ya va mal.
        """
        import inspect

        fuente = inspect.getsource(modulo_comprobar)
        for prohibido in ("anthropic.", "messages.create", "ClienteAnthropic",
                          "requests.get", "urlopen"):
            assert prohibido not in fuente, (
                f"el comprobador no puede llamar a nada: encontrado {prohibido}"
            )


# ---------------------------------------------------------------------------
#  La cáscara
# ---------------------------------------------------------------------------

class TestLaCascara:
    def test_su_log_NO_es_el_de_la_app(self):
        """Y el motivo es de Windows, no de orden.

        `registro.py` rota `job-rob.log` al llegar a 1 MB, y rotar es renombrar.
        En Windows no se puede renombrar un archivo que otro proceso tiene
        abierto: si la cáscara y el servidor escribieran los dos ahí, la
        rotación fallaría dentro de `logging` —que se traga sus errores— y el
        único diagnóstico que existe se perdería en silencio.
        """
        from recolector.registro import ruta_del_log as ruta_de_la_app

        datos = RAIZ / "data"
        assert modulo_abrir.ruta_del_log(datos) != ruta_de_la_app(datos)
        assert modulo_abrir.ruta_del_log(datos).parent == ruta_de_la_app(datos).parent, (
            "los dos van en la misma carpeta: el botón «Si algo va mal» la abre entera"
        )

    def test_sin_navegador_devuelve_None_en_vez_de_reventar(self):
        """Sin Edge ni Chrome la app se abre igual, en lo que haya. Es feo, y
        es mejor que no abrirse."""
        assert modulo_abrir.buscar_navegador(candidatos=()) is None

    def test_esperar_al_servidor_se_rinde_si_el_proceso_ha_muerto(self):
        """Sin esto, un servidor que revienta en el primer segundo dejaría a la
        clienta cuarenta segundos mirando un icono que no hace nada."""
        class Muerto:
            def poll(self):
                return 1

        assert modulo_abrir.esperar_al_servidor(Muerto(), segundos=5) is False

    def test_la_ventana_va_en_modo_app_y_con_perfil_propio(self):
        """Las dos mitades de que sea una aplicación y no una pestaña.

        `--app=` quita la barra de direcciones; `--user-data-dir` la separa de
        su navegador, para que cerrar Edge no le cierre la herramienta.
        """
        import inspect

        fuente = inspect.getsource(modulo_abrir.abrir_ventana)
        assert "--app=" in fuente
        assert "--user-data-dir=" in fuente

    def test_al_cerrar_se_mata_el_ARBOL(self):
        """Matar solo al servidor dejaría una corrida huérfana gastando dinero
        contra `data/uso_llm.json`, sin nadie mirándola y sin forma de pararla.
        Y ese archivo se escribe entero desde memoria: un segundo escritor no
        solo gasta, borra apuntes."""
        import inspect

        fuente = inspect.getsource(modulo_abrir.matar_el_arbol)
        assert "/T" in fuente, "taskkill sin /T deja vivos a los hijos"

    def test_un_lockfile_que_nadie_tiene_no_es_una_ventana_viva(self, tmp_path):
        """El resto de un cierre a lo bruto no puede colgar el servidor.

        Si un `lockfile` huérfano contara como ventana abierta, el servidor se
        quedaría vivo para siempre y `matar_el_arbol` no llegaría nunca: una
        corrida podría seguir gastando sin nadie mirándola. Por eso no basta
        con que el archivo exista, hay que no poder abrirlo.
        """
        perfil = tmp_path / "ventana"
        perfil.mkdir()
        assert modulo_abrir.perfil_en_uso(perfil) is False, "sin lockfile, no hay ventana"

        (perfil / "lockfile").write_bytes(b"")
        assert modulo_abrir.perfil_en_uso(perfil) is False, (
            "un lockfile que se puede abrir es un resto, no una sesión viva"
        )

    def test_no_se_mata_el_servidor_solo_porque_el_lanzador_termine(self):
        """EL FALLO DEL ARRANQUE, FIJADO (medido el 7 septiembre 2026).

        Chromium admite una sesión por perfil: si ya hay instancia viva, el
        proceso lanzado le pasa la URL y termina en el acto. `ventana.wait()`
        volvía enseguida y la cáscara mataba el servidor recién levantado —
        medido: en pie en el segundo 24, muerto en el 30—, dejándola con un
        `ERR_CONNECTION_REFUSED` que se repetía en cada doble clic.

        Se comprueba sobre la fuente porque lo que hay que fijar es el ORDEN:
        que entre esperar al proceso y matar el árbol se consulte el perfil.
        """
        import inspect

        fuente = inspect.getsource(modulo_abrir.main)
        assert "esperar_a_que_se_cierre_la_ventana" in fuente, (
            "sin esto, que Edge delegue la ventana se lee como que ella la cerró"
        )
        despues_de_esperar = fuente.split("esperar_a_que_se_cierre_la_ventana", 1)[1]
        assert "matar_el_arbol" in despues_de_esperar, (
            "el servidor tiene que morir DESPUÉS de que se cierre la ventana"
        )

    def test_una_segunda_copia_no_toca_a_la_primera(self):
        """«Servidor en pie» no puede decirse mirando el servidor de otro.

        Con una copia abierta, la segunda daba por suyo el puerto de la primera
        y seguía hasta `matar_el_arbol`. Aquella vez no hizo daño de milagro.
        """
        import inspect

        fuente = inspect.getsource(modulo_abrir.main)
        antes_de_arrancar = fuente.split("arrancar_servidor()", 1)[0]
        assert "hay_alguien_en_el_puerto" in antes_de_arrancar, (
            "hay que mirar si ya está abierta ANTES de levantar un segundo servidor"
        )
        assert "return 0" in antes_de_arrancar, (
            "y hay que salirse: seguir adelante acaba llamando a matar_el_arbol"
        )

    def test_el_servidor_se_lanza_sin_navegador(self):
        """Si el servidor abriera además el navegador por defecto, ella vería
        la app Y una pestaña normal con la URL y el puerto a la vista."""
        import inspect

        assert "--sin-navegador" in inspect.getsource(modulo_abrir.arrancar_servidor)

    def test_la_bandeja_entiende_esa_bandera(self):
        from bandeja.server import main  # noqa: F401
        import inspect

        from bandeja import server

        assert "--sin-navegador" in inspect.getsource(server.main)


# ---------------------------------------------------------------------------
#  El icono
# ---------------------------------------------------------------------------

class TestElIcono:
    def test_el_archivo_versionado_es_un_ico_valido_de_cuatro_tamanos(self):
        """Se versiona porque hace falta el día de la instalación y no puede
        depender de que alguien lo tenga en sus descargas. El generador se
        versiona con él para que un binario sin fuente no quede huérfano."""
        datos = (RAIZ / "herramientas" / "job-rob.ico").read_bytes()
        reservado, tipo, cuantas = struct.unpack("<HHH", datos[:6])
        assert (reservado, tipo) == (0, 1)
        assert cuantas == len(hacer_icono.TAMANOS)

        for i in range(cuantas):
            ancho, _, _, _, _, _, tamano, desde = struct.unpack(
                "<BBBBHHII", datos[6 + 16 * i: 22 + 16 * i]
            )
            assert datos[desde:desde + 8] == b"\x89PNG\r\n\x1a\n"
            assert tamano > 0

    def test_se_puede_volver_a_generar_igual(self):
        """Si el generador dejara de reproducir el archivo, el `.ico` sería un
        binario que nadie puede rehacer."""
        # Se comparan los PÍXELES, no los bytes: el PNG va comprimido con zlib y
        # versiones distintas de zlib comprimen igual de bien pero no idéntico.
        assert _pixeles(hacer_icono.ico()) == _pixeles(
            (RAIZ / "herramientas" / "job-rob.ico").read_bytes()
        )


def _pixeles(ico: bytes) -> list[bytes]:
    """Los datos de imagen descomprimidos de cada PNG dentro de un `.ico`."""
    import struct
    import zlib

    total = struct.unpack_from("<H", ico, 4)[0]
    imagenes = []
    for i in range(total):
        tam, desde = struct.unpack_from("<II", ico, 6 + 16 * i + 8)
        png, pos, idat = ico[desde:desde + tam], 8, b""
        while pos < len(png):
            largo, tipo = struct.unpack_from(">I4s", png, pos)
            if tipo == b"IDAT":
                idat += png[pos + 8:pos + 8 + largo]
            pos += 12 + largo
        imagenes.append(zlib.decompress(idat))
    return imagenes


# ---------------------------------------------------------------------------
#  Preparar la entrega
# ---------------------------------------------------------------------------

class TestElLibroSeReiniciaSinPerderLaProcedencia:
    """El día de la entrega la clave pasa a ser suya, y el contador también.

    Si la copia se lleva `uso_llm.json` tal cual, su barra del mes arranca con
    $4,05 de gasto que no es suyo y el aviso del 75% le llega semanas antes de
    tiempo. No falla nada: el número que ella mira está mal desde el primer día.

    Y borrarlo tampoco vale: entonces se pierde de dónde venían esos $4,05. Es
    la quinta vez que este proyecto se encuentra con que las dos salidas obvias
    pierden información, y la respuesta es la de siempre — guardar la
    procedencia.
    """

    def test_archiva_el_viejo_y_empieza_a_cero_con_la_nota(self, tmp_path):
        from clasificador.presupuesto import LibroDeGastos
        from herramientas.preparar_entrega import archivar_el_libro

        ruta = tmp_path / "uso_llm.json"
        libro = LibroDeGastos(ruta)
        libro.registrar(4.0570, llamadas=137)
        assert libro.gastado_total() == pytest.approx(4.0570)

        destino = archivar_el_libro(ruta, dry_run=False)

        assert destino is not None and destino.exists(), (
            "el libro viejo se archiva, no se borra"
        )
        assert LibroDeGastos(destino).gastado_total() == pytest.approx(4.0570)

        nuevo = LibroDeGastos(ruta)
        assert nuevo.gastado_total() == pytest.approx(0.0), (
            "su mes tiene que empezar en cero: ese gasto no es suyo"
        )
        ajustes = nuevo.ajustes()
        assert len(ajustes) == 1
        assert ajustes[0]["usd"] == pytest.approx(0.0), (
            "la nota no puede mover dinero: solo deja escrito de dónde venía"
        )
        assert destino.name in ajustes[0]["motivo"]
        assert "4.0570" in ajustes[0]["motivo"]

    def test_dry_run_no_toca_nada(self, tmp_path):
        from clasificador.presupuesto import LibroDeGastos
        from herramientas.preparar_entrega import archivar_el_libro

        ruta = tmp_path / "uso_llm.json"
        LibroDeGastos(ruta).registrar(1.23)
        archivar_el_libro(ruta, dry_run=True)

        assert ruta.exists()
        assert LibroDeGastos(ruta).gastado_total() == pytest.approx(1.23)

    def test_sin_libro_previo_no_revienta(self, tmp_path):
        from herramientas.preparar_entrega import archivar_el_libro

        assert archivar_el_libro(tmp_path / "no-existe.json", dry_run=False) is None


# ---------------------------------------------------------------------------
#  El BOM del .env: el fallo que la app no habría notado hasta días después
# ---------------------------------------------------------------------------

class TestElEnvNoSeRompeConUnBOM:
    """Salió ejecutando el instalador en una carpeta limpia (4 sept 2026).

    `Set-Content -Encoding UTF8` en Windows PowerShell 5.1 escribe UTF-8 **con
    BOM**, y esos tres bytes se pegan al nombre de la primera variable del
    archivo: `python-dotenv` cargaba `﻿ADZUNA_APP_ID` y `ADZUNA_APP_ID` dejaba
    de existir. Efecto en su máquina: la app abre, la bandeja se ve entera con
    la base que viaja, y **«Buscar ofertas nuevas» no puede funcionar nunca**.

    Lo que hace falta fijar son las dos mitades, porque arreglan cosas
    distintas: que el instalador no lo escriba, y que quien lee el `.env`
    aguante uno igualmente — el Bloc de notas también guarda con BOM, y ella va
    a abrir ese archivo el día que cambie una clave.
    """

    def test_el_instalador_escribe_el_env_sin_bom(self):
        texto = (RAIZ / "herramientas" / "instalar.ps1").read_text(encoding="utf-8-sig")
        assert "UTF8Encoding($false)" in texto, (
            "el .env se debe escribir sin BOM: con BOM se pierde la primera clave"
        )
        assert "Set-Content -Path $archivoEnv" not in texto, (
            "Set-Content -Encoding UTF8 pone BOM en PowerShell 5.1"
        )

    def test_la_configuracion_lee_un_env_con_bom(self, tmp_path, monkeypatch):
        """La red de seguridad: aunque alguien lo guarde con BOM, se lee."""
        from recolector import config as modulo_config

        env = tmp_path / ".env"
        env.write_bytes(
            "\ufeffADZUNA_APP_ID=id-de-prueba\nADZUNA_APP_KEY=key-de-prueba\n".encode("utf-8")
        )
        for nombre in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY"):
            monkeypatch.delenv(nombre, raising=False)

        modulo_config.cargar_env(env)

        import os
        assert os.getenv("ADZUNA_APP_ID") == "id-de-prueba"

    def test_el_comprobador_tambien(self, tmp_path):
        """Un diagnóstico que miente es peor que no tener ninguno: sin esto
        decía [FALTA] sobre una credencial que estaba puesta."""
        env = tmp_path / ".env"
        env.write_bytes(
            "\ufeffADZUNA_APP_ID=id-de-prueba\n"
            "ADZUNA_APP_KEY=key-de-prueba\n"
            "ANTHROPIC_API_KEY=sk-ant-de-prueba\n".encode("utf-8")
        )

        informe = modulo_comprobar.Informe()
        modulo_comprobar.comprobar_claves(informe, raiz=tmp_path)

        assert informe.faltas == 0, [linea for linea in informe.lineas]


# ---------------------------------------------------------------------------
#  Cerrar la ventana tiene que parar la app
# ---------------------------------------------------------------------------

class TestCerrarLaVentanaPara:
    """EL FALLO, MEDIDO EJECUTANDO LA APP EL 9 SEPTIEMBRE 2026.

    `main` hacía `ventana.wait()` y solo después miraba el perfil. Eso cubre
    que Chromium delegue y el proceso lanzado muera en el acto (el arreglo del
    7 sept). No cubre el caso contrario, que es el que pasa cuando ella ya
    tiene su Edge abierto: el proceso que lanzamos SOBREVIVE al cierre de la
    ventana y `wait()` no vuelve nunca. Medido, con una corrida en marcha:

        se cerró la ventana ......... 03:14:25
        siguió leyendo y pagando .... hasta 03:15:14  (3 llamadas más)
        el servidor seguía en pie ... 03:16:09, escuchando en el 8765

    Que es lo contrario de lo que AYUDA.md le promete: «cerrar la ventana con
    algo corriendo hace lo mismo que Parar».
    """

    class ProcesoInmortal:
        """El de Edge cuando ya había una instancia suya abierta."""

        def poll(self):
            return None

    class ProcesoQueDelega:
        """El de Edge cuando le pasa la URL a otra instancia y termina."""

        def poll(self):
            return 0

    def test_no_espera_al_proceso_sino_a_la_ventana(self, monkeypatch, tmp_path):
        """Con el proceso vivo para siempre, se vuelve igual al cerrar."""
        respuestas = iter([False, True, True, False])
        monkeypatch.setattr(
            modulo_abrir, "perfil_en_uso", lambda _perfil: next(respuestas, False)
        )

        modulo_abrir.esperar_a_que_se_cierre_la_ventana(
            tmp_path, self.ProcesoInmortal(), gracia_s=1.0, intervalo_s=0.0
        )
        # Si no volviera, la prueba no llegaría aquí: se colgaría, que es
        # exactamente lo que le pasaba al servidor.

    def test_main_ya_no_bloquea_en_el_proceso(self):
        """Se comprueba sobre la fuente, igual que el arreglo del 7 sept.

        Lo que hay que fijar es que la señal sea UNA: el perfil. Volver a
        poner `ventana.wait()` delante reintroduce el fallo sin romper nada
        más, que es lo que lo hizo durar dos días.
        """
        import inspect

        fuente = inspect.getsource(modulo_abrir.main)
        assert "ventana.wait()" not in fuente, (
            "esperar al proceso deja el servidor vivo cuando Edge no muere"
        )
        assert "esperar_a_que_se_cierre_la_ventana(perfil, ventana)" in fuente, (
            "el proceso se pasa como pista de arranque, no como señal de cierre"
        )

    def test_una_ventana_que_no_llega_a_abrirse_no_cuelga_el_servidor(
        self, monkeypatch, tmp_path
    ):
        """El agujero por el otro lado.

        Si el candado no aparece nunca y el proceso no muere nunca, esperar
        sin tope dejaría el servidor vivo para siempre y `matar_el_arbol` no
        llegaría jamás — el mismo final que se está arreglando.
        """
        monkeypatch.setattr(modulo_abrir, "perfil_en_uso", lambda _perfil: False)

        modulo_abrir.esperar_a_que_se_cierre_la_ventana(
            tmp_path,
            self.ProcesoInmortal(),
            gracia_s=0.2,
            tope_de_arranque_s=0.6,
            intervalo_s=0.0,
        )

    def test_si_el_proceso_delega_se_le_da_tiempo_al_candado(
        self, monkeypatch, tmp_path
    ):
        """El caso del 7 septiembre, que no se puede volver a romper.

        El lanzador muere en el acto y la ventana la abre otra instancia: el
        candado tarda un instante en aparecer y NO se puede leer esa demora
        como «ella la cerró».
        """
        respuestas = iter([False, False, True, True, False])
        monkeypatch.setattr(
            modulo_abrir, "perfil_en_uso", lambda _perfil: next(respuestas, False)
        )

        modulo_abrir.esperar_a_que_se_cierre_la_ventana(
            tmp_path, self.ProcesoQueDelega(), gracia_s=2.0, intervalo_s=0.0
        )
        assert next(respuestas, "agotado") == "agotado", (
            "tiene que haber esperado a que el candado apareciera y se soltara"
        )


# ---------------------------------------------------------------------------
#  La primera pantalla es la app, no un aviso de Microsoft
# ---------------------------------------------------------------------------

class TestLaVentanaAbreLimpia:
    """EL FALLO, MEDIDO CON PERFIL NUEVO EL 9 SEPTIEMBRE 2026 (y repetido).

    Windows 11 trae la cuenta de Microsoft del equipo iniciada y Edge la
    hereda solo. Al abrir la app por primera vez, encima de la bandeja salía
    un cuadro a media pantalla —«Se están sincronizando los datos de
    exploración… <su correo> ha iniciado sesión…»— que tapa la app entera,
    lleva su dirección de correo dentro y no se quita con escape.

    No es cosmético por dos motivos: es lo primero que ve de la herramienta
    que le acaban de entregar, y desmiente la decisión nº 1 de `abrir.py`
    —«perfil propio, ni sus cookies entran aquí»—, porque la identidad sí
    entraba.
    """

    def test_la_ventana_no_sincroniza_con_su_cuenta(self, tmp_path, monkeypatch):
        """Las banderas tienen que llegar a la línea de órdenes de verdad.

        Se comprueba sobre el comando y no sobre la constante: una constante
        bien puesta que nadie usa es exactamente el fallo que esto arregla.
        """
        lanzados = {}

        class PopenFalso:
            def __init__(self, argv, **kwargs):
                lanzados["argv"] = argv

        monkeypatch.setattr(modulo_abrir.subprocess, "Popen", PopenFalso)
        modulo_abrir.abrir_ventana(
            Path("msedge.exe"), "http://127.0.0.1:8765", tmp_path / "ventana"
        )

        argv = lanzados["argv"]
        assert "--disable-sync" in argv, (
            "sin esto Edge sincroniza el perfil de la app con su cuenta"
        )
        banderas = next(a for a in argv if a.startswith("--disable-features="))
        assert "msImplicitSignin" in banderas, (
            "es la que hereda la cuenta de Windows, que es la que abre el cuadro"
        )

    def test_sigue_siendo_una_ventana_de_app_y_con_perfil_propio(
        self, tmp_path, monkeypatch
    ):
        """Que arreglar lo de la cuenta no se lleve por delante lo demás."""
        lanzados = {}

        class PopenFalso:
            def __init__(self, argv, **kwargs):
                lanzados["argv"] = argv

        monkeypatch.setattr(modulo_abrir.subprocess, "Popen", PopenFalso)
        perfil = tmp_path / "ventana"
        modulo_abrir.abrir_ventana(Path("msedge.exe"), "http://x", perfil)

        argv = lanzados["argv"]
        assert "--app=http://x" in argv, "sin esto se ve la barra de direcciones"
        assert f"--user-data-dir={perfil}" in argv, "sin esto comparte su sesión"
        assert perfil.exists(), "la carpeta del perfil se crea antes de lanzar"


# ---------------------------------------------------------------------------
#  La ayuda tiene que decir cómo localizarte
# ---------------------------------------------------------------------------

class TestElContactoDeLaAyuda:
    """EL HUECO, ENCONTRADO LEYENDO LA HOJA ENTERA EL 9 SEPTIEMBRE 2026.

    `AYUDA.md` decía en su primera línea «si lo que no abre es la aplicación,
    escríbeme directamente […] al final está cómo localizarme», el paso 3 de
    «Cómo pedirme ayuda» decía «abre un correo para mí», y el botón «Si algo
    va mal» copiaba un texto que decía «mándale esto a Juan Pablo». Los tres
    apuntaban a un sitio que no existía: en toda la hoja no había ni una
    dirección ni un teléfono.

    Solo se nota el día que ella lo necesita, y ese día suele ser justo
    aquel en el que la app no arranca — o sea, cuando tampoco puede abrir
    esta hoja a buscarlo.
    """

    def test_la_hoja_promete_el_contacto_y_lo_tiene(self):
        hoja = (RAIZ / "AYUDA.md").read_text(encoding="utf-8")
        assert "cómo localizarme" in hoja.lower(), (
            "la primera línea de la hoja lo promete"
        )
        assert "## Cómo localizarme" in hoja, (
            "y tiene que existir la sección, no solo la promesa"
        )

    def test_un_hueco_sin_rellenar_se_canta(self, tmp_path):
        """Que no pueda viajar en silencio: es lo único que lo impide."""
        (tmp_path / "AYUDA.md").write_text(
            "# Ayuda\n\n## Cómo localizarme\n\n- Correo: `PENDIENTE_CORREO`\n",
            encoding="utf-8",
        )
        informe = modulo_comprobar.Informe()
        modulo_comprobar.comprobar_ayuda(informe, raiz=tmp_path)

        estado, _, detalle = informe.lineas[0]
        assert estado == modulo_comprobar.AVISO
        assert "PENDIENTE_CORREO" in detalle, "hay que decir CUÁL falta"

    def test_relleno_pasa(self, tmp_path):
        (tmp_path / "AYUDA.md").write_text(
            "# Ayuda\n\n## Cómo localizarme\n\n- Correo: `alguien@ejemplo.com`\n",
            encoding="utf-8",
        )
        informe = modulo_comprobar.Informe()
        modulo_comprobar.comprobar_ayuda(informe, raiz=tmp_path)

        assert informe.lineas[0][0] == modulo_comprobar.OK
        assert informe.avisos == 0

    def test_sin_hoja_es_un_fallo_de_verdad(self, tmp_path):
        """Sin `AYUDA.md` el botón «Dudas y ayuda» no enseña nada."""
        informe = modulo_comprobar.Informe()
        modulo_comprobar.comprobar_ayuda(informe, raiz=tmp_path)

        assert informe.lineas[0][0] == modulo_comprobar.FALTA

    def test_la_revision_completa_lo_mira(self):
        """Una comprobación que nadie llama no comprueba nada."""
        import inspect

        fuente = inspect.getsource(modulo_comprobar.revisar)
        assert "comprobar_ayuda(informe)" in fuente


# ---------------------------------------------------------------------------
#  El actualizador no se puede quedar mudo
# ---------------------------------------------------------------------------

class TestElActualizadorNoSeAtasca:
    """MEDIDO EL 11 SEPTIEMBRE 2026, ejecutándolo contra una etiqueta movida.

    `git fetch --tags` se NIEGA a mover una etiqueta que ya tiene en local, y
    sale con error. El script lee ese error como «no se pudo hablar con el
    repositorio (¿sin red?)» y arranca con lo que hay — o sea que el día que
    una etiqueta publicada se corrija (reetiquetar un arreglo con prisa es
    justo lo que uno hace), **todas las copias que ya la tuvieran dejan de
    actualizarse para siempre**, y el log manda a mirar el wifi.

    Con la clienta a 9.000 km y el actualizador como único camino hasta su
    máquina, eso es perderla entera.
    """

    def _fuente(self) -> str:
        return (RAIZ / "herramientas" / "actualizar.ps1").read_text(encoding="utf-8-sig")

    def test_el_fetch_acepta_que_una_etiqueta_se_mueva(self):
        fuente = self._fuente()
        linea = next(l for l in fuente.splitlines() if "'fetch'" in l)
        assert "'--force'" in linea, (
            "sin --force, una etiqueta corregida deja la copia congelada para siempre"
        )

    def test_el_log_dice_lo_que_dijo_git(self):
        """«¿Sin red?» sobre un fallo que no es de red cuesta el día entero."""
        fuente = self._fuente()
        assert "UltimoErrorGit" in fuente, (
            "hay que guardar lo que git escribió, no solo que falló"
        )
        aviso = next(
            l for l in fuente.splitlines()
            if "Apuntar 'AVISO'" in l and "lista de versiones" in l
        )
        assert "$script:UltimoErrorGit" in aviso, (
            "el motivo real tiene que llegar al archivo que ella manda"
        )

    def test_sigue_sin_poder_pedir_credenciales_por_consola(self):
        """La red de seguridad de siempre: sin esto la app no arranca nunca."""
        fuente = self._fuente()
        assert "GIT_TERMINAL_PROMPT = '0'" in fuente
        assert "GCM_INTERACTIVE = 'never'" in fuente
