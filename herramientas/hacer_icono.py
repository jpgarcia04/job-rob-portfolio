"""
Genera `herramientas/job-rob.ico`, el icono del acceso directo del escritorio.

POR QUÉ ES UN GENERADOR Y NO UN ARCHIVO SUELTO
----------------------------------------------
El `.ico` sí se versiona —hace falta el día de la instalación y no puede
depender de que alguien lo tenga en su carpeta de descargas—, pero un binario
sin fuente es un archivo que nadie puede volver a hacer. Con esto se rehace en
un segundo si hay que cambiarle el color o el tamaño:

    python herramientas\\hacer_icono.py

Sin dependencias: PNG a mano con `zlib`, que es de la biblioteca estándar.
Meter Pillow por un icono sería exactamente la dependencia nueva que la sesión
de la cáscara decidió no arrastrar.

QUÉ DIBUJA
----------
Un cuadrado redondeado del color de la app (`--superficie`, #12161E) con tres
barras de latón (`--acento`, #C9A56B) de largo decreciente: el mismo motivo que
el botón «Ordenar por dónde empezar» de la bandeja. A 16 px una lista se sigue
leyendo como una lista; un logotipo con letras, no.

Se generan cuatro tamaños dentro del mismo `.ico` —16, 32, 48 y 256— y cada uno
se DIBUJA a su tamaño en vez de escalar el grande. Escalar 256 a 16 deja las
barras en gris sucio; dibujarlas nativas las deja de una pieza.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

DESTINO = Path(__file__).parent / "job-rob.ico"

FONDO = (0x12, 0x16, 0x1E)      # --superficie
ACENTO = (0xC9, 0xA5, 0x6B)     # --acento (latón)

TAMANOS = (16, 32, 48, 256)


def _dentro_del_redondeo(x: float, y: float, lado: int, radio: float) -> float:
    """Cuánto de este píxel cae dentro del cuadrado redondeado, de 0 a 1.

    Se muestrea 4×4 dentro del píxel en vez de decidir con el centro: a 16 px
    una esquina decidida por el centro se ve como una escalera de tres
    escalones, y ese es justo el tamaño al que se va a mirar en la barra de
    tareas.
    """
    dentro = 0
    for sy in range(4):
        for sx in range(4):
            px, py = x + (sx + 0.5) / 4, y + (sy + 0.5) / 4
            # Distancia al rectángulo interior; dentro de él siempre cuenta.
            dx = max(radio - px, px - (lado - radio), 0.0)
            dy = max(radio - py, py - (lado - radio), 0.0)
            if dx * dx + dy * dy <= radio * radio:
                dentro += 1
    return dentro / 16


def _mezclar(fondo: tuple[int, int, int], frente: tuple[int, int, int],
             alfa: float) -> tuple[int, int, int]:
    return tuple(round(f + (d - f) * alfa) for f, d in zip(fondo, frente))  # type: ignore[return-value]


def dibujar(lado: int) -> bytes:
    """Devuelve el mapa de píxeles RGBA de un tamaño, fila a fila."""
    radio = lado * 0.22

    # Las tres barras, en fracciones del lado: (y, x_inicio, x_fin, grosor).
    margen = lado * 0.24
    grosor = max(1.0, lado * 0.105)
    hueco = lado * 0.145
    barras = [
        (lado * 0.5 - hueco - grosor, margen, lado - margen),
        (lado * 0.5 - grosor / 2,     margen, lado - margen * 1.55),
        (lado * 0.5 + hueco,          margen, lado - margen * 0.72),
    ]

    filas = []
    for y in range(lado):
        fila = bytearray()
        for x in range(lado):
            alfa_fondo = _dentro_del_redondeo(x, y, lado, radio)
            color = FONDO

            # ¿Cae el píxel en alguna barra? Se mide la cobertura vertical y
            # horizontal por separado y se multiplican: eso da los bordes
            # suavizados sin tener que rasterizar de verdad.
            for barra_y, x0, x1 in barras:
                cobertura_y = max(0.0, min(y + 1.0, barra_y + grosor) - max(float(y), barra_y))
                cobertura_x = max(0.0, min(x + 1.0, x1) - max(float(x), x0))
                cobertura = min(1.0, cobertura_y) * min(1.0, cobertura_x)
                if cobertura > 0:
                    color = _mezclar(color, ACENTO, cobertura)

            fila += bytes(color) + bytes((round(alfa_fondo * 255),))
        filas.append(bytes(fila))
    return b"".join(b"\x00" + f for f in filas)


def _trozo(etiqueta: bytes, datos: bytes) -> bytes:
    return (struct.pack(">I", len(datos)) + etiqueta + datos
            + struct.pack(">I", zlib.crc32(etiqueta + datos) & 0xFFFFFFFF))


def png(lado: int) -> bytes:
    """PNG RGBA de 8 bits. El `.ico` moderno admite PNG dentro, y para 256 px
    es lo único que cabe sin inflar el archivo a un megabyte."""
    cabecera = struct.pack(">IIBBBBB", lado, lado, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _trozo(b"IHDR", cabecera)
            + _trozo(b"IDAT", zlib.compress(dibujar(lado), 9))
            + _trozo(b"IEND", b""))


def ico(tamanos: tuple[int, ...] = TAMANOS) -> bytes:
    imagenes = [(lado, png(lado)) for lado in tamanos]
    desplazamiento = 6 + 16 * len(imagenes)

    cabecera = struct.pack("<HHH", 0, 1, len(imagenes))
    entradas, cuerpos = b"", b""
    for lado, datos in imagenes:
        entradas += struct.pack(
            "<BBBBHHII",
            0 if lado >= 256 else lado,   # 0 significa 256 en el formato ICO
            0 if lado >= 256 else lado,
            0, 0, 1, 32, len(datos), desplazamiento,
        )
        cuerpos += datos
        desplazamiento += len(datos)
    return cabecera + entradas + cuerpos


def main() -> int:
    DESTINO.write_bytes(ico())
    print(f"{DESTINO}  ({DESTINO.stat().st_size:,} bytes, tamaños {TAMANOS})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
