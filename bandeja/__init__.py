"""
Bandeja de revisión — v1.

Servidor local (FastAPI) + interfaz que lee las ofertas ya recolectadas y
deja marcarlas. Vive separado de `recolector/` a propósito: el recolector no
sabe que existe una interfaz, y la interfaz no sabe de dónde salieron las
ofertas. Lo único que comparten es la base de datos.

Modelo copiloto: aquí NO se postula a nada de forma automática. Se revisa,
se marca, y se abre la oferta en el navegador para que ella decida.
"""
