<#
    Trae la última versión PUBLICADA antes de arrancar la app.

    POR QUÉ EXISTE
    --------------
    El desarrollador entrega la app en México y se vuelve a España. A partir de
    ahí no puede tocar esa máquina: sin esto, un fallo tonto —un texto mal, un
    umbral, una excepción— se queda ahí para siempre y el encargo se cae.

    Es la mitad barata del mantenimiento en remoto. La otra mitad es el log
    (`recolector/registro.py`): este manda el arreglo, aquel dice qué arreglar.

    LOS TRES DETALLES QUE LO HACEN SEGURO, y los tres son deliberados:

    1. TIRA DE ETIQUETAS, NO DE `main`. El desarrollador sigue trabajando en
       `main` todos los días; si esto siguiera esa rama, cualquier commit a
       medias llegaría a la máquina de la clienta esa misma tarde. Solo llega
       lo que se etiqueta a propósito.

    2. SI FALLA, ARRANCA IGUAL. Sin red, sin GitHub, con las credenciales de
       git caducadas: la app tiene que abrirse. Un actualizador que deja la
       herramienta muerta cuando el wifi va mal es peor que no tener ninguno.
       Por eso este script NUNCA devuelve un código distinto de 0 por un fallo
       de actualización — se apunta en el log y se sigue.

    3. `pip install` SOLO SI CAMBIÓ `requirements.txt`. Si no, son veinte
       segundos de espera cada vez que ella abre la app, todos los días, para
       nada.

    Y UNA COSA QUE NO HACE: tocar `data/` ni `.env`. Ahí viven su base de
    datos, su libro de gastos y sus API keys. Están en `.gitignore`, así que
    `git checkout` no los mira — pero conviene tenerlo escrito, porque el día
    que alguien añada `data/` al repositorio esto se los llevaría por delante.

    Uso:
        powershell -ExecutionPolicy Bypass -File herramientas\actualizar.ps1
#>

[CmdletBinding()]
param(
    # Para probarlo sin tocar nada: dice qué haría y sale.
    [switch]$SoloMirar
)

$ErrorActionPreference = 'Continue'

$RAIZ = Split-Path -Parent $PSScriptRoot
$LOG  = Join-Path $RAIZ 'data\logs\actualizar.log'

function Apuntar {
    param([string]$Nivel, [string]$Mensaje)
    $linea = '{0}  {1,-7}  {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Nivel, $Mensaje
    Write-Host $linea
    $carpeta = Split-Path -Parent $LOG
    if (-not (Test-Path $carpeta)) { New-Item -ItemType Directory -Force $carpeta | Out-Null }
    Add-Content -Path $LOG -Value $linea -Encoding utf8
}

# OJO CON EL NOMBRE: NO puede llamarse `Git`.
# PowerShell resuelve los nombres de comando buscando primero entre sus
# funciones y solo después entre los ejecutables del PATH, así que una función
# llamada `Git` hace que `& git ...` se llame a sí misma. Recursión infinita, y
# el síntoma es que el script se queda colgado sin escribir una línea — que es
# exactamente el fallo que este archivo existe para no tener. Pasó al
# escribirlo, el 3 septiembre 2026.
function Invoke-Git {
    # Devuelve la salida, o $null si git falla. No lanza: ver el detalle 2.
    #
    # GIT_TERMINAL_PROMPT=0 es OBLIGATORIO y no es una optimización. El
    # repositorio es privado; en una máquina sin credenciales guardadas, git
    # se para a pedir usuario y contraseña por una consola que aquí no existe,
    # y la app no arranca nunca. Con esto falla en seco, se apunta en el log y
    # se sigue. `GCM_INTERACTIVE=never` hace lo mismo con el gestor de
    # credenciales de Windows, que abre una VENTANA en vez de un prompt.
    $env:GIT_TERMINAL_PROMPT = '0'
    $env:GCM_INTERACTIVE = 'never'
    $salida = & git.exe -C $RAIZ @args 2>&1
    if ($LASTEXITCODE -ne 0) {
        # LO QUE DIJO GIT, GUARDADO. Antes se tiraba, y entonces el unico
        # rastro de un fallo era la frase que escribiera quien llamara —
        # normalmente «(sin red?)». El 11 septiembre 2026 el fetch fallo
        # porque una etiqueta habia cambiado de sitio, el log dijo «sin red»,
        # y sin esto la pista habria sido esa. Desde lejos, un diagnostico
        # equivocado cuesta el dia entero.
        $script:UltimoErrorGit = ($salida | Out-String).Trim()
        return $null
    }
    return ($salida | Out-String).Trim()
}

Apuntar 'INFO' '--- comprobando si hay versión nueva ---'

# ¿Hay git y esto es un repositorio? Si se entregó como zip en vez de como
# clon, no hay nada que hacer y hay que decirlo: es un fallo de instalación.
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Apuntar 'ERROR' 'no hay git instalado: esta copia no se puede actualizar sola'
    exit 0
}
if ($null -eq (Invoke-Git 'rev-parse' '--is-inside-work-tree')) {
    Apuntar 'ERROR' 'esto no es un clon de git: se entregó como copia suelta y no se puede actualizar'
    exit 0
}

$antes = Invoke-Git 'describe' '--tags' '--always' '--dirty=+'
Apuntar 'INFO' "versión actual: $antes"

# CAMBIOS LOCALES: NO SE PISAN. En la máquina de la clienta esto no debería
# pasar nunca, y si pasa es que alguien tocó archivos a mano. Machacarlos
# borraría lo que hiciera falta para entender el problema.
if ($antes -and $antes.EndsWith('+')) {
    Apuntar 'AVISO' 'hay cambios locales sin guardar: no se actualiza, para no pisarlos'
    exit 0
}

# `--force` NO ES UN ADORNO (medido el 11 septiembre 2026, ejecutandolo).
#
# Sin el, `git fetch --tags` se NIEGA a mover una etiqueta que ya tiene, y
# devuelve error. O sea que el dia que una etiqueta publicada se corrija —se
# reetiqueta un arreglo, que es exactamente lo que uno hace con prisa— todas
# las copias que ya la tuvieran dejarian de actualizarse PARA SIEMPRE, y el
# log les diria «sin red». El repositorio manda: si alli una etiqueta apunta a
# otro sitio, aqui tambien.
if ($null -eq (Invoke-Git 'fetch' '--tags' '--force' '--quiet' 'origin')) {
    Apuntar 'AVISO' "no se pudo traer la lista de versiones. Se arranca con lo que hay. git dijo: $script:UltimoErrorGit"
    exit 0
}

# `-v:refname` es el orden de VERSION de git, y hacen falta las dos mitades:
#
#   alfabetico   -> v1.9.0 por encima de v1.10.0. Instalaria una version
#                   vieja, en silencio y para siempre.
#   creatordate  -> arregla eso, pero empata cuando dos etiquetas se crean
#                   en el mismo segundo, y entonces el desempate vuelve a
#                   ser alfabetico. MEDIDO el 3 septiembre 2026 con tres
#                   etiquetas seguidas: devolvia v1.0.0 como la ultima.
#
# v:refname entiende los numeros y no depende de ningun reloj.
$ultima = Invoke-Git 'for-each-ref' '--sort=-v:refname' '--format=%(refname:short)' '--count=1' 'refs/tags'
if (-not $ultima) {
    Apuntar 'AVISO' 'el repositorio no tiene ninguna etiqueta todavía: no hay nada publicado que traer'
    exit 0
}

$actual = Invoke-Git 'describe' '--tags' '--exact-match' 'HEAD'
if ($actual -eq $ultima) {
    Apuntar 'INFO' "ya está en la última publicada ($ultima)"
    exit 0
}

Apuntar 'INFO' "hay versión nueva: $ultima (ahora: $antes)"
if ($SoloMirar) {
    Apuntar 'INFO' '(--SoloMirar: no se ha tocado nada)'
    exit 0
}

# ¿Cambian las dependencias? Se mira ANTES de movernos, comparando lo que hay
# ahora con lo que trae la etiqueta.
$requisitosCambian = $null -ne (Invoke-Git 'diff' '--name-only' 'HEAD' $ultima '--' 'requirements.txt') -and
                     (Invoke-Git 'diff' '--name-only' 'HEAD' $ultima '--' 'requirements.txt')

if ($null -eq (Invoke-Git 'checkout' '--quiet' $ultima)) {
    Apuntar 'ERROR' "no se pudo cambiar a $ultima. Se arranca con la versión de antes ($antes)"
    exit 0
}

$despues = Invoke-Git 'describe' '--tags' '--always'
Apuntar 'INFO' "actualizada: $antes -> $despues"

if ($requisitosCambian) {
    Apuntar 'INFO' 'requirements.txt cambió: instalando dependencias'
    $python = Join-Path $RAIZ '.venv\Scripts\python.exe'
    if (Test-Path $python) {
        & $python -m pip install --quiet --disable-pip-version-check -r (Join-Path $RAIZ 'requirements.txt')
        if ($LASTEXITCODE -ne 0) {
            # Esto SÍ puede dejar la app sin arrancar, así que se grita en el
            # log. Aun así no se para: quizá lo que falta no hace falta hoy.
            Apuntar 'ERROR' 'falló la instalación de dependencias. La app puede no arrancar'
        } else {
            Apuntar 'INFO' 'dependencias al día'
        }
    } else {
        Apuntar 'ERROR' 'no se encontró el entorno virtual (.venv): hay que reinstalar'
    }
} else {
    Apuntar 'INFO' 'las dependencias no cambian: no se toca pip'
}

exit 0
