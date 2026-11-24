"""
Clasificación de ofertas con un LLM — la capa 2.

Regla de la casa, la misma que en `recolector/`: **nada fuera de
`cliente_llm.py` sabe que Anthropic existe.** Cambiar de proveedor debe ser
tocar un archivo, no una refactorización.

El reparto de responsabilidades:

  fichas.py       lee y valida `config/cvs.yaml`
  idioma.py       decide el idioma del anuncio -> qué CV. SIN gastar tokens
  esquema.py      la forma EXACTA de lo que puede devolver el modelo
  prompt.py       arma el mensaje. Testeable con fichas inventadas
  cliente_llm.py  única frontera con el SDK
  agrupador.py    junta ofertas que son la misma vacante
  motor.py        orquesta: pendientes -> caché -> LLM -> persistir
  cli.py          el comando de consola y su resumen de coste
"""

__all__ = ["fichas", "idioma", "esquema", "prompt", "cliente_llm", "agrupador", "motor", "cli"]
