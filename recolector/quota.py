"""
Contadores de cuota: Jooble (vitalicia) y Adzuna (por ventana de tiempo).

El plan gratuito de Jooble da 500 peticiones POR VIDA y POR KEY. No se
renuevan. Cuando se acaban, se acabaron: hay que pagar o cambiar de fuente.

Cuidado con la aritmética, que es fácil equivocarse: como Jooble emite una key
por país y cada key solo atiende a su mercado, una corrida completa de 13
combinaciones x 2 mercados gasta 13 peticiones de la key de México y 13 de la
de EE.UU. — no 26 de un presupuesto común. Son ~38 corridas completas, no ~19.

Sigue siendo poco para un producto que se ejecuta solo varias veces al día
(3 corridas diarias = 39 peticiones/día por key = cuota vital agotada en menos
de dos semanas), y es exactamente el tipo de recurso que se agota por
accidente un martes depurando.

Por eso el contador:

  - vive en su propio archivo JSON, no en la base de datos, para sobrevivir a
    un `rm data/ofertas.db` mientras se desarrolla;
  - cuenta por variable de entorno (una key por país = un contador por país);
  - guarda una huella corta de la key, nunca la key, para poder avisar si se
    cambió de key y por tanto el contador ya no corresponde;
  - se consulta ANTES de correr, para poder negarse.

Limitación honesta: este contador solo ve las peticiones que hace este
programa. Si gastas peticiones desde curl o desde Postman, no se entera. Es
una estimación conservadora, no la verdad de Jooble.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class QuotaExhaustedError(Exception):
    """No queda presupuesto suficiente para la corrida solicitada."""


@dataclass
class QuotaStatus:
    env_name: str
    used: int
    budget: int
    planned: int
    fingerprint_matches: bool
    known_fingerprint: str | None

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    @property
    def remaining_after_run(self) -> int:
        return max(0, self.remaining - self.planned)

    @property
    def runs_left_after(self) -> float:
        """Cuántas corridas más del mismo tamaño caben tras esta."""
        if self.planned <= 0:
            return float("inf")
        return self.remaining_after_run / self.planned


class JoobleQuota:
    """Persistencia sencilla del gasto acumulado."""

    def __init__(self, path: Path, budget: int) -> None:
        self.path = path
        self.budget = budget
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"budget": self.budget, "keys": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            # Si el contador está corrupto NO se asume cero: eso invitaría a
            # gastar cuota que quizá ya no existe. Se falla ruidosamente.
            raise RuntimeError(
                f"El contador de cuota de Jooble está ilegible: {self.path}\n"
                f"Error: {exc}\n"
                "Revísalo o bórralo a mano si estás seguro de que el gasto era cero."
            ) from exc

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data["budget"] = self.budget
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, ensure_ascii=False, indent=2)

    # -- consulta ------------------------------------------------------------

    def _entry(self, env_name: str) -> dict[str, Any]:
        # setdefault: devuelve el valor si la clave existe, y si no la crea
        # con el valor por defecto. Equivale a computeIfAbsent en Java.
        return self._data.setdefault("keys", {}).setdefault(
            env_name, {"used": 0, "fingerprint": None, "first_used": None, "last_used": None}
        )

    def used(self, env_name: str) -> int:
        return int(self._entry(env_name).get("used", 0))

    def remaining(self, env_name: str) -> int:
        return max(0, self.budget - self.used(env_name))

    def status(self, env_name: str, fingerprint: str, planned: int) -> QuotaStatus:
        entry = self._entry(env_name)
        known = entry.get("fingerprint")
        return QuotaStatus(
            env_name=env_name,
            used=int(entry.get("used", 0)),
            budget=self.budget,
            planned=planned,
            # Si nunca se usó, no hay huella con la que comparar: se da por buena.
            fingerprint_matches=(known is None or known == fingerprint),
            known_fingerprint=known,
        )

    # -- registro ------------------------------------------------------------

    def record(self, env_name: str, fingerprint: str, count: int = 1) -> None:
        """Anota `count` peticiones consumidas. Se escribe a disco de inmediato.

        Escribir en cada petición (en vez de al final) es a propósito: si el
        proceso muere a mitad de corrida, la cuota gastada ya está anotada.
        """
        entry = self._entry(env_name)
        now = datetime.now(timezone.utc).isoformat()
        entry["used"] = int(entry.get("used", 0)) + count
        entry["fingerprint"] = fingerprint
        if not entry.get("first_used"):
            entry["first_used"] = now
        entry["last_used"] = now
        self._save()

    def check_before_run(
        self,
        env_name: str,
        fingerprint: str,
        planned: int,
        refuse_below: int,
        force: bool = False,
    ) -> tuple[QuotaStatus, list[str]]:
        """Comprueba si se puede correr. Devuelve (estado, avisos).

        Lanza QuotaExhaustedError si no hay presupuesto y no se pasó --force.
        """
        status = self.status(env_name, fingerprint, planned)
        warnings: list[str] = []

        if not status.fingerprint_matches:
            warnings.append(
                f"La key de {env_name} CAMBIÓ desde la última corrida "
                f"(huella anterior {status.known_fingerprint}, ahora {fingerprint}). "
                f"El contador de {status.used} peticiones probablemente ya no corresponde "
                f"a esta key. Si es una key nueva, borra su entrada en {self.path}."
            )

        if status.remaining_after_run < refuse_below:
            message = (
                f"Cuota de Jooble insuficiente para {env_name}.\n"
                f"  Presupuesto de por vida : {status.budget}\n"
                f"  Consumido               : {status.used}\n"
                f"  Disponible              : {status.remaining}\n"
                f"  Necesario esta corrida  : {planned}\n"
                f"  Quedaría                : {status.remaining_after_run} "
                f"(umbral mínimo: {refuse_below})\n"
                f"Recuerda: son peticiones DE POR VIDA, no mensuales.\n"
                f"Opciones: correr solo Adzuna (--sources adzuna), reducir combinaciones "
                f"(--limit-combos N), o forzar con --force si sabes lo que haces."
            )
            if not force:
                raise QuotaExhaustedError(message)
            warnings.append("--force activo: se ignora el umbral de cuota.\n" + message)

        return status, warnings


# ---------------------------------------------------------------------------
#  Consumo de Adzuna
# ---------------------------------------------------------------------------
#  Adzuna es el caso contrario a Jooble: su cuota SÍ se regenera (es por
#  ventana de tiempo, no vitalicia). Pasarse devuelve HTTP 429 y se recupera
#  solo. Es una molestia, no una catástrofe.
#
#  Su límite real no está documentado de forma fiable, así que aquí no se
#  finge saberlo: se MIDE. Se lleva el gasto por día natural, y de ahí salen
#  el total del día, el del mes y el histórico. Después de la primera corrida
#  sabrás cuántas llamadas cuesta una pasada completa y podrás dividir.
#
#  Si algún día conoces el tope, ponlo en el YAML (`daily_call_budget` /
#  `monthly_call_budget`) y esto empezará a negarse igual que Jooble.
# ---------------------------------------------------------------------------


class ApiBudgetExceededError(Exception):
    """Se alcanzó un tope declarado de peticiones por ventana de tiempo."""


class WindowedUsage:
    """Contador de peticiones por día natural, con vistas por mes y total.

    Se guarda un diccionario {fecha: número} en lugar de una lista de sellos
    de tiempo: queda acotado (365 entradas al año), es legible a ojo y basta
    para lo que hace falta.
    """

    def __init__(self, path: Path, label: str) -> None:
        self.path = path
        self.label = label
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"by_day": {}, "total": 0, "first_used": None, "last_used": None}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            # A diferencia de Jooble, aquí un contador corrupto NO es grave:
            # la cuota se regenera. Se avisa y se empieza de cero.
            print(f"[cuota/{self.label}] AVISO: contador ilegible ({exc}). Se reinicia a cero.")
            return {"by_day": {}, "total": 0, "first_used": None, "last_used": None}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, ensure_ascii=False, indent=2)

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def record(self, count: int = 1) -> None:
        today = self._today()
        by_day = self._data.setdefault("by_day", {})
        by_day[today] = int(by_day.get(today, 0)) + count
        self._data["total"] = int(self._data.get("total", 0)) + count
        now = datetime.now(timezone.utc).isoformat()
        if not self._data.get("first_used"):
            self._data["first_used"] = now
        self._data["last_used"] = now
        self._save()

    def used_today(self) -> int:
        return int(self._data.get("by_day", {}).get(self._today(), 0))

    def used_this_week(self) -> int:
        """Los últimos 7 días, contando hoy. VENTANA MÓVIL, no semana natural.

        Adzuna dice «1000 hit / week» y no aclara si su semana empieza el lunes
        o si es móvil. Se elige la lectura móvil porque es la conservadora: una
        ventana móvil nunca cuenta menos que una natural, así que si cabe aquí
        cabe en la suya, sea cual sea. Al revés no se puede prometer — con
        semanas naturales, seis corridas el domingo y seis el lunes son doce en
        24 horas y dos semanas distintas.
        """
        hoy = datetime.now(timezone.utc).date()
        ventana = {
            (hoy - timedelta(days=n)).strftime("%Y-%m-%d") for n in range(7)
        }
        return sum(
            int(value)
            for day, value in self._data.get("by_day", {}).items()
            if day in ventana
        )

    def used_this_month(self) -> int:
        prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        return sum(
            int(value)
            for day, value in self._data.get("by_day", {}).items()
            if day.startswith(prefix)
        )

    def used_total(self) -> int:
        return int(self._data.get("total", 0))

    def check_before_run(
        self,
        planned: int,
        daily_budget: int | None,
        monthly_budget: int | None,
        warn_daily_above: int,
        force: bool = False,
        weekly_budget: int | None = None,
    ) -> list[str]:
        """Avisa (y si hay tope declarado, se niega) antes de gastar.

        `weekly_budget` va al final y con defecto porque llegó después: el plan
        Trial de Adzuna tiene TRES ventanas de volumen (día, semana, mes) y la
        semana no se parece a ninguna de las otras dos. Con una corrida diaria
        la que aprieta primero no es la mensual.
        """
        warnings: list[str] = []
        today = self.used_today()
        week = self.used_this_week()
        month = self.used_this_month()

        if daily_budget is not None and today + planned > daily_budget:
            message = (
                f"[{self.label}] La corrida pasaría el tope DIARIO declarado.\n"
                f"  Consumido hoy : {today}\n"
                f"  Esta corrida  : {planned}\n"
                f"  Tope diario   : {daily_budget}\n"
                "La cuota de Adzuna se regenera: puedes esperar a mañana o subir el tope "
                "en config/search_profile.yaml si sabes que es más alto."
            )
            if not force:
                raise ApiBudgetExceededError(message)
            warnings.append("--force activo: se ignora el tope diario.\n" + message)

        if weekly_budget is not None and week + planned > weekly_budget:
            message = (
                f"[{self.label}] La corrida pasaría el tope SEMANAL declarado.\n"
                f"  Últimos 7 días: {week}\n"
                f"  Esta corrida  : {planned}\n"
                f"  Tope semanal  : {weekly_budget}\n"
                "Se cuenta en ventana móvil de 7 días, que es la lectura "
                "conservadora de «1000 hit / week». Espera un día o corre menos "
                "combinaciones."
            )
            if not force:
                raise ApiBudgetExceededError(message)
            warnings.append("--force activo: se ignora el tope semanal.\n" + message)

        if monthly_budget is not None and month + planned > monthly_budget:
            message = (
                f"[{self.label}] La corrida pasaría el tope MENSUAL declarado "
                f"({month} + {planned} > {monthly_budget})."
            )
            if not force:
                raise ApiBudgetExceededError(message)
            warnings.append("--force activo: se ignora el tope mensual.\n" + message)

        # LA ALARMA DE DESPISTE, QUE NO ES EL TOPE Y SIGUE HACIENDO FALTA.
        #
        # Una corrida son 46 y el tope diario es 250, así que caben cinco: el
        # tope nunca salta en la operación normal. Pero «hoy van 138» significa
        # que algo se corrió tres veces sin querer, y eso hay que decirlo
        # aunque quepa. Antes esta rama solo existía cuando no había tope
        # declarado, o sea que al escribir los números reales se habría apagado
        # justo el aviso que sirve para el día a día.
        if today + planned > warn_daily_above:
            if daily_budget is None:
                warnings.append(
                    f"[{self.label}] Llevas {today} peticiones hoy y esta corrida "
                    f"suma {planned}. No hay tope declarado en la configuración, "
                    f"así que no me puedo negar, pero vigila el HTTP 429."
                )
            else:
                warnings.append(
                    f"[{self.label}] Llevas {today} peticiones hoy y esta corrida "
                    f"suma {planned}, por encima del aviso de {warn_daily_above}. "
                    f"Cabe en el tope diario de {daily_budget}, pero una corrida "
                    "son 46: si esto salta, probablemente algo se corrió de más."
                )

        return warnings

    def summary(self) -> dict[str, Any]:
        return {
            "hoy": self.used_today(),
            "esta_semana": self.used_this_week(),
            "este_mes": self.used_this_month(),
            "historico": self.used_total(),
            "archivo": str(self.path),
        }
