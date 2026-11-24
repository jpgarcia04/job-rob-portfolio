"""
Deja esta carpeta lista para copiarla a la máquina de la clienta.

    python herramientas\\preparar_entrega.py --dry-run
    python herramientas\\preparar_entrega.py

POR QUÉ ES UN SCRIPT Y NO UNA LISTA DE COSAS QUE HACER A MANO
--------------------------------------------------------------
Porque una de ellas es el libro de gastos, y equivocarse ahí no da ningún
síntoma. Si la copia se lleva `data/uso_llm.json` tal cual, su barra del mes
arranca con **$4,05 de gasto que no es suyo**, el techo de $12 cuenta desde
ahí, y el primer aviso del 75% le llega semanas antes de tiempo. No falla nada:
simplemente el número que ella mira está mal desde el primer día.

Y borrar el archivo a mano tampoco vale, por el otro lado: entonces se pierde
de dónde venían esos $4,05, y este proyecto ya decidió cuatro veces que cuando
las dos opciones pierden información, la salida es **guardar la procedencia**
(`clasificaciones.heredada_de`, `triajes.heredada_de`,
`description_full_via = 'nada'`, y el ajuste de los $0,1587).

Así que el libro viejo se ARCHIVA y el nuevo nace a cero **con una línea que
dice de dónde viene**.

QUÉ SE LLEVA Y QUÉ NO, Y POR QUÉ
--------------------------------
    data/ofertas.db ......... SÍ. Es lo que hace que su primera pantalla tenga
                              381 ofertas y un orden hecho en vez de un vacío y
                              dos minutos y medio de espera. La primera
                              impresión es lo único que no se puede repetir.
    data/cvs/ ............... SÍ, y sin esto la app no arranca: `cargar_fichas`
                              se niega si los archivos no están, y `data/` está
                              en .gitignore, así que un clon nuevo los trae
                              vacíos. Salió al probar la instalación en frío.
    data/uso_adzuna.json .... SÍ, y es el contraintuitivo. La cuenta de Adzuna
                              sigue siendo del desarrollador, o sea que las dos
                              máquinas comparten una cuota de 2.500/mes y
                              ninguno de los dos contadores ve al otro (límite
                              conocido nº 6). Llevarlo hace que su máquina crea
                              que se ha gastado MÁS de lo que ella ha gastado,
                              que es el lado conservador.
    data/uso_llm.json ....... NO. Se archiva y se empieza a cero. Es el único
                              contador que mide dinero de OTRA persona, y a
                              partir del día de la entrega la clave es suya.
    data/cache/ ............. NO. Son 29 MB de respuestas HTTP crudas con 12
                              horas de vida. Se regenera solo y no aporta nada.
    data/reportes/ .......... NO. Son las capturas de las corridas del
                              desarrollador. No le sirven de nada y ocupan.

NO COPIA NADA. Este script solo prepara ESTA carpeta y dice qué falta; la copia
la hace una persona, mirando. Automatizar el copiado sería la clase de gesto
que se estropea en silencio el día que una ruta cambia.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from clasificador.presupuesto import LibroDeGastos, ruta_del_libro  # noqa: E402
from recolector.config import load_config  # noqa: E402

# Lo que NO viaja. Se enseña para que quede escrito, no para borrarlo: borrar
# la caché del desarrollador no es cosa de este script.
NO_VIAJAN = ("cache", "reportes", "ventana", "ofertas.db.bak-v8")


def archivar_el_libro(ruta: Path, *, dry_run: bool) -> Path | None:
    """Aparta el libro viejo y deja uno nuevo a cero, con su procedencia.

    Devuelve dónde quedó el viejo, o `None` si no había ninguno.
    """
    if not ruta.exists():
        return None

    viejo = LibroDeGastos(ruta)
    gastado = viejo.gastado_total()
    marca = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    destino = ruta.with_suffix(f".json.desarrollo-{marca}")

    if dry_run:
        return destino

    shutil.move(str(ruta), str(destino))

    # El nuevo nace vacío y con una sola línea de historia. `ajustar(0.0, ...)`
    # deja la nota sin mover ni un céntimo: el importe es cero a propósito,
    # porque ese gasto NO es suyo y no puede contar contra su techo.
    nuevo = LibroDeGastos(ruta)
    nuevo.ajustar(
        0.0,
        f"Libro reiniciado para la entrega. El anterior acumulaba "
        f"${gastado:.4f} de pruebas del desarrollador, con su clave de "
        f"Anthropic, y está archivado en {destino.name}. A partir de aquí la "
        f"clave y el gasto son de la clienta.",
    )
    return destino


def revisar(config, informe: list[tuple[str, str]]) -> None:
    datos = Path(config.database_path).parent

    base = Path(config.database_path)
    informe.append(
        ("ok" if base.exists() else "FALTA",
         f"data/{base.name} — la base que abre su primera pantalla")
    )

    cvs = datos / "cvs"
    archivos = sorted(p.name for p in cvs.glob("*")) if cvs.exists() else []
    informe.append(
        ("ok" if archivos else "FALTA",
         f"data/cvs/ — {', '.join(archivos) if archivos else 'VACÍA: la app no arrancará'}")
    )

    adzuna = datos / "uso_adzuna.json"
    informe.append(
        ("ok" if adzuna.exists() else "AVISO",
         "data/uso_adzuna.json — la cuota compartida con la máquina del desarrollador")
    )

    for nombre in NO_VIAJAN:
        ruta = datos / nombre
        if ruta.exists():
            informe.append(("no viaja", f"data/{nombre}"))


def main(argv: list[str] | None = None) -> int:
    opciones = argparse.ArgumentParser(
        description="Prepara esta carpeta para copiarla a la máquina de la clienta.",
    )
    opciones.add_argument("--dry-run", action="store_true",
                          help="Dice qué haría y no toca nada.")
    args = opciones.parse_args(argv)

    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            try:
                flujo.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    config = load_config()
    print("Preparar la entrega")
    print(f"  carpeta: {RAIZ}")
    if args.dry_run:
        print("  (--dry-run: no se toca nada)")
    print()

    ruta = ruta_del_libro(config)
    antes = LibroDeGastos(ruta).gastado_total() if ruta.exists() else 0.0
    destino = archivar_el_libro(ruta, dry_run=args.dry_run)

    print("== El libro de gastos")
    if destino is None:
        print("   [ok]    ya estaba a cero: no había libro que archivar")
    elif args.dry_run:
        print(f"   [haría] archivar ${antes:.4f} en {destino.name} y empezar a cero")
    else:
        print(f"   [ok]    ${antes:.4f} archivados en {destino.name}")
        print(f"   [ok]    libro nuevo a $0.0000, con la nota de dónde venía")

    print()
    print("== Lo que viaja en data/")
    informe: list[tuple[str, str]] = []
    revisar(config, informe)
    for estado, texto in informe:
        print(f"   [{estado}] {texto}")

    print()
    print("== Lo que NO hace este script, y hay que hacer a mano")
    print("   1. Etiquetar y publicar la versión que se entrega:")
    print("        git tag -a v1.0.0 -m 'entrega' && git push origin v1.0.0")
    print("      Sin una etiqueta, el actualizador no trae NADA, y en silencio.")
    print("   2. Copiar la carpeta a su equipo (sin .venv, sin data/cache).")
    print("   3. Allí: herramientas\\instalar.ps1, y pegar SU clave de Anthropic.")
    print("   4. Comprobar con herramientas\\comprobar.ps1 que sale todo en verde.")
    print()
    print("   El orden importa: si se instala antes de etiquetar, su máquina se")
    print("   queda con una versión que el actualizador no reconoce como publicada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
