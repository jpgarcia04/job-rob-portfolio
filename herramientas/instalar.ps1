<#
    Deja esta carpeta lista para usarse. Cinco minutos, no una tarde.

    powershell -ExecutionPolicy Bypass -File herramientas\instalar.ps1

    PARA QUÉ EXISTE
    ---------------
    La entrega es presencial y son una o dos horas con la clienta delante. Si
    ese rato se va en depurar una instalación, no queda tiempo para lo único
    que la visita puede dar y ninguna otra cosa puede: verla usar la
    herramienta y oír qué echa en falta. Así que instalar tiene que ser copiar
    la carpeta, ejecutar esto, y pegar una clave.

    QUÉ HACE, EN ORDEN
    ------------------
      1. Comprueba que hay Python y git, y dice de dónde se bajan si no.
      2. Crea el entorno virtual e instala las dependencias.
      3. Deja `.env` en su sitio y SE PARA a pedir la clave de Anthropic.
      4. Crea el acceso directo del escritorio, con su icono.
      5. Arranca la app una vez para dejarla caliente, y da el veredicto.

    LO QUE NO HACE, Y ES A PROPÓSITO
    --------------------------------
    · **No toca `data/` si ya está.** Su base de ofertas, su libro de gastos y
      sus CVs viven ahí. Volver a ejecutar esto no puede costarle nada suyo.
    · **No pide las claves de Adzuna.** Son del desarrollador y viajan en el
      `.env` que se copia con la carpeta. La ÚNICA que cambia en la entrega es
      la de Anthropic, porque a partir de ese día el gasto es suyo. Si no
      estuvieran, el paso 5 lo dice.
    · **No instala Python ni git.** Bajar e instalar software es un gesto con
      consentimiento y una barra de progreso; que lo haga un script a espaldas
      de quien está delante no está bien, y además puede pedir permisos de
      administrador que aquí no hay.

    Y SE GUARDA COMO UTF-8 CON BOM. Windows PowerShell 5.1 lee los .ps1 como
    ANSI si no lo llevan y los acentos llegan rotos. Pasó el 3 septiembre 2026.
#>

[CmdletBinding()]
param(
    # Para poder probar esto sin una persona delante. En la instalación real
    # NO se usa: el script se para y la pide.
    [string]$ClaveAnthropic,
    # Deja la clave como esté y no pregunta. Para reparar una instalación.
    [switch]$SinPreguntar,
    # Ni acceso directo ni arranque de prueba. Para las pruebas automáticas.
    [switch]$SoloEntorno
)

$ErrorActionPreference = 'Continue'
$RAIZ = Split-Path -Parent $PSScriptRoot
$fallos = 0

function Paso  { param([string]$T) Write-Host ''; Write-Host "== $T" }
function Bien  { param([string]$T) Write-Host "   [ok]    $T" }
function Aviso { param([string]$T) Write-Host "   [AVISO] $T" }
function Mal   { param([string]$T) Write-Host "   [FALTA] $T"; $script:fallos++ }

Write-Host ''
Write-Host 'Instalación de la Bandeja de ofertas'
Write-Host "  carpeta: $RAIZ"

# ---------------------------------------------------------------------------
Paso '1 de 5 · Lo que tiene que estar ya'

$python = $null
foreach ($nombre in @('py', 'python')) {
    $comando = Get-Command $nombre -ErrorAction SilentlyContinue
    if ($null -ne $comando) { $python = $comando.Source; break }
}
if ($null -eq $python) {
    Mal 'Python no está instalado.'
    Write-Host '            Se baja de https://www.python.org/downloads/windows/'
    Write-Host '            IMPORTANTE: marca «Add python.exe to PATH» al instalarlo.'
} else {
    $version = (& $python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null)
    Bien "Python $version"
}

if ($null -eq (Get-Command git -ErrorAction SilentlyContinue)) {
    # FALTA y no AVISO: sin git no hay actualizador, y sin actualizador un
    # fallo tonto se queda en esa máquina para siempre. Es la mitad del
    # mantenimiento en remoto, no un extra.
    Mal 'Git no está instalado.'
    Write-Host '            Se baja de https://git-scm.com/download/win'
    Write-Host '            Sin git, la app no se puede arreglar desde España.'
} else {
    Bien 'Git'
}

if ($fallos -gt 0) {
    Write-Host ''
    Write-Host 'Instala lo de arriba y vuelve a ejecutar esto. No se ha tocado nada.'
    exit 1
}

# ---------------------------------------------------------------------------
Paso '2 de 5 · El entorno y las dependencias'

$venv = Join-Path $RAIZ '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'

if (Test-Path $venvPython) {
    Bien 'El entorno virtual ya estaba'
} else {
    Write-Host '   creando el entorno virtual...'
    & $python -m venv $venv
    if (-not (Test-Path $venvPython)) {
        Mal 'No se pudo crear el entorno virtual (.venv).'
        exit 1
    }
    Bien 'Entorno virtual creado'
}

Write-Host '   instalando dependencias (puede tardar un minuto)...'
& $venvPython -m pip install --quiet --disable-pip-version-check --upgrade pip
& $venvPython -m pip install --quiet --disable-pip-version-check -r (Join-Path $RAIZ 'requirements.txt')
if ($LASTEXITCODE -ne 0) {
    Mal 'Falló la instalación de dependencias. Mira el texto de arriba.'
} else {
    Bien 'Dependencias instaladas'
}

# ---------------------------------------------------------------------------
Paso '3 de 5 · La clave de Anthropic'

$archivoEnv = Join-Path $RAIZ '.env'
$ejemplo = Join-Path $RAIZ '.env.example'

if (-not (Test-Path $archivoEnv)) {
    if (-not (Test-Path $ejemplo)) {
        Mal 'No hay .env ni .env.example. Falta media instalación.'
    } else {
        Copy-Item $ejemplo $archivoEnv
        Aviso '.env creado desde la plantilla: las claves de Adzuna están sin rellenar.'
    }
} else {
    Bien '.env ya estaba (con las claves de Adzuna dentro)'
}

if ((-not $SinPreguntar) -and (Test-Path $archivoEnv)) {
    if (-not $ClaveAnthropic) {
        Write-Host ''
        Write-Host '   Pega aquí la clave de la API de Anthropic (console.anthropic.com).'
        Write-Host '   Empieza por sk-ant- . Si la dejas en blanco, se queda la que hubiera.'
        $ClaveAnthropic = Read-Host '   Clave'
    }
    if ($ClaveAnthropic) {
        if ($ClaveAnthropic -notmatch '^sk-ant-') {
            # Se avisa y se guarda igual: el formato lo decide Anthropic, no
            # nosotros, y negarse por una comprobación nuestra dejaría la app
            # sin clave el día que cambien el prefijo.
            Aviso 'Esa clave no empieza por «sk-ant-». Se guarda igual, pero revísala.'
        }
        # SE REESCRIBE SOLO ESA LÍNEA. El resto del archivo —las claves de
        # Adzuna, los datos de la clienta— no se toca. Rehacerlo desde la
        # plantilla borraría lo que vino en la copia, y eso son las credenciales
        # sin las que no se puede buscar una sola oferta.
        $lineas = @(Get-Content $archivoEnv -Encoding UTF8)
        $puesta = $false
        $nuevas = @()
        foreach ($linea in $lineas) {
            if ($linea -match '^\s*ANTHROPIC_API_KEY\s*=') {
                $nuevas += "ANTHROPIC_API_KEY=$ClaveAnthropic"
                $puesta = $true
            } else {
                $nuevas += $linea
            }
        }
        if (-not $puesta) { $nuevas += "ANTHROPIC_API_KEY=$ClaveAnthropic" }
        # SIN BOM, Y AQUÍ ESTÁ EL FALLO QUE ESTO ARREGLA (4 septiembre 2026).
        #
        # `Set-Content -Encoding UTF8` en Windows PowerShell 5.1 significa
        # «UTF-8 CON BOM». Esos tres bytes se pegan al principio del archivo, o
        # sea al nombre de la PRIMERA variable, que en este `.env` es
        # `ADZUNA_APP_ID`. `python-dotenv` cargaba entonces una variable
        # llamada `﻿ADZUNA_APP_ID` y `ADZUNA_APP_ID` no existía.
        #
        # Comprobado ejecutando el instalador sobre una copia limpia: la app
        # abría, la bandeja se veía entera con la base que viaja, y **«Buscar
        # ofertas nuevas» no podía funcionar nunca**. Es el peor de los fallos
        # posibles aquí: el día de la entrega no se nota, y a los tres días la
        # herramienta simplemente no trae nada.
        #
        # Es el mismo asunto del BOM que la sesión anterior documentó en los
        # `.ps1` —donde hace falta— aplicado al archivo donde ESTORBA. Un .ps1
        # sin BOM se lee como ANSI; un .env con BOM pierde su primera clave.
        #
        # Y no lo cazaba `test_el_instalador_no_pisa_las_claves_que_ya_estan`,
        # que lee este archivo y comprueba que el paso 3 no menciona ADZUNA:
        # era verdad, y aun así las borraba. Regla nº 13 de la casa — lo que
        # toca el arranque se prueba ejecutándolo, no leyéndolo.
        [System.IO.File]::WriteAllLines(
            $archivoEnv, $nuevas, (New-Object System.Text.UTF8Encoding($false))
        )
        Bien 'Clave de Anthropic guardada'
    } else {
        Aviso 'No se cambió la clave: se queda la que hubiera en .env'
    }
}

# ---------------------------------------------------------------------------
Paso '4 de 5 · El acceso directo del escritorio'

if ($SoloEntorno) {
    Aviso 'omitido (-SoloEntorno)'
} else {
    $icono = Join-Path $RAIZ 'herramientas\job-rob.ico'
    if (-not (Test-Path $icono)) {
        & $venvPython (Join-Path $RAIZ 'herramientas\hacer_icono.py') | Out-Null
    }

    # GetFolderPath y no "$HOME\Desktop": con OneDrive activado el escritorio
    # real está DENTRO de OneDrive, y el acceso directo acabaría en una carpeta
    # que ella no ve nunca. Esta llamada devuelve el de verdad, esté donde esté.
    $escritorio = [Environment]::GetFolderPath('Desktop')
    $atajo = Join-Path $escritorio 'Bandeja de ofertas.lnk'

    # pythonw.exe y no python.exe: es la variante SIN consola. Con python.exe
    # quedaría una ventana negra detrás de la app toda la sesión, que es
    # exactamente lo que la cáscara existe para evitar.
    $pythonw = Join-Path $venv 'Scripts\pythonw.exe'
    if (-not (Test-Path $pythonw)) { $pythonw = $venvPython }

    try {
        $shell = New-Object -ComObject WScript.Shell
        $enlace = $shell.CreateShortcut($atajo)
        $enlace.TargetPath = $pythonw
        $enlace.Arguments = '"' + (Join-Path $RAIZ 'herramientas\abrir.py') + '"'
        $enlace.WorkingDirectory = $RAIZ
        $enlace.IconLocation = $icono
        $enlace.Description = 'Bandeja de ofertas'
        $enlace.Save()
        Bien "Acceso directo en $atajo"
    } catch {
        Aviso "No se pudo crear el acceso directo: $_"
    }
}

# ---------------------------------------------------------------------------
Paso '5 de 5 · Comprobación final'

if (-not $SoloEntorno) {
    # ARRANCA UNA VEZ A PROPÓSITO, y no es por comprobar: es para dejar escrito
    # el caché de bytecode. El primer arranque de Python en una carpeta recién
    # copiada compila el proyecto entero, y eso son ~20 segundos mirando un
    # icono que no hace nada. Que los gaste el instalador, no ella.
    Write-Host '   arrancando la app una vez para dejarla caliente...'
    & $venvPython (Join-Path $RAIZ 'herramientas\abrir.py') --sin-actualizar --sin-ventana | Out-Null
}

& (Join-Path $PSScriptRoot 'comprobar.ps1')
$veredicto = $LASTEXITCODE

Write-Host ''
if (($veredicto -eq 0) -and ($fallos -eq 0)) {
    Write-Host 'LISTO. Abre la app con el icono «Bandeja de ofertas» del escritorio.'
} else {
    Write-Host 'Queda algo sin resolver: mira las líneas [FALTA] de arriba.'
}
exit $veredicto
