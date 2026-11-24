"""
Caché en disco de respuestas crudas.

Existe por una razón concreta: Jooble tiene 500 peticiones DE POR VIDA por
key. Depurar el parseo repitiendo la misma búsqueda veinte veces se come el
4% del presupuesto vital de la clienta cada tarde. Con caché, se pide una vez
y se depura contra el archivo.

Tres modos:

  normal      -> usa la caché si está fresca; si no, pide a la red y guarda
  bypass      -> ignora la caché y siempre pide a la red (pero guarda)
  cache_only  -> NUNCA toca la red; si no está en caché, falla ruidosamente

`cache_only` es el modo de trabajo mientras se desarrolla, y también el que
permite correr los tests sin credenciales.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODE_NORMAL = "normal"
MODE_BYPASS = "bypass"
MODE_CACHE_ONLY = "cache_only"


class CacheMissError(Exception):
    """Se pidió `--cache-only` y la respuesta no estaba en caché."""


@dataclass
class CacheEntry:
    payload: dict[str, Any]
    stored_at: datetime
    age_hours: float
    is_fresh: bool


class DiskCache:
    def __init__(
        self,
        directory: Path,
        ttl_hours: float,
        enabled: bool = True,
        mode: str = MODE_NORMAL,
    ) -> None:
        self.directory = directory
        self.ttl_hours = ttl_hours
        self.enabled = enabled
        self.mode = mode
        if self.enabled:
            # parents=True crea la ruta entera; exist_ok=True evita error si ya está.
            self.directory.mkdir(parents=True, exist_ok=True)

    # -- clave --------------------------------------------------------------

    @staticmethod
    def build_key(source: str, market_key: str, query: str, request_signature: str) -> str:
        """Clave estable para una consulta concreta.

        `request_signature` debe incluir TODO lo que cambia la respuesta
        (parámetros, página, host). Si se olvida algo, la caché devuelve
        respuestas de otra consulta y el error es dificilísimo de ver.
        La API key nunca entra en la firma.
        """
        material = "|".join([source, market_key, query, request_signature])
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        # El nombre lleva fuente y mercado en claro para poder inspeccionar la
        # carpeta a ojo durante el desarrollo.
        safe_market = market_key.replace("/", "_")
        return f"{source}__{safe_market}__{digest}"

    def _path_for(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    # -- lectura / escritura -------------------------------------------------

    def get(self, key: str) -> CacheEntry | None:
        """Devuelve la entrada si existe. `is_fresh` dice si está dentro del TTL.

        Se devuelve también la entrada caducada porque en modo `cache_only`
        preferimos servir algo viejo (y avisar) a no poder trabajar.
        """
        if not self.enabled or self.mode == MODE_BYPASS:
            return None

        path = self._path_for(key)
        if not path.exists():
            return None

        try:
            with open(path, "r", encoding="utf-8") as handle:
                envelope = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            # Caché corrupta: se avisa y se trata como ausencia, no se traga.
            print(f"[cache] AVISO: entrada ilegible {path.name}: {exc}")
            return None

        stored_at = datetime.fromisoformat(envelope["stored_at"])
        age_hours = (datetime.now(timezone.utc) - stored_at).total_seconds() / 3600.0
        return CacheEntry(
            payload=envelope["payload"],
            stored_at=stored_at,
            age_hours=age_hours,
            is_fresh=age_hours <= self.ttl_hours,
        )

    def put(self, key: str, request_meta: dict[str, Any], payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        envelope = {
            "stored_at": datetime.now(timezone.utc).isoformat(),
            "request": request_meta,   # sin credenciales: ver los conectores
            "payload": payload,
        }
        path = self._path_for(key)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(envelope, handle, ensure_ascii=False, indent=2)

    # -- utilidades ----------------------------------------------------------

    @property
    def is_cache_only(self) -> bool:
        return self.mode == MODE_CACHE_ONLY

    def stats(self) -> dict[str, Any]:
        if not self.enabled or not self.directory.exists():
            return {"enabled": self.enabled, "entries": 0}
        entries = list(self.directory.glob("*.json"))
        total_bytes = sum(p.stat().st_size for p in entries)
        return {
            "enabled": True,
            "mode": self.mode,
            "entries": len(entries),
            "size_kb": round(total_bytes / 1024, 1),
            "directory": str(self.directory),
        }
