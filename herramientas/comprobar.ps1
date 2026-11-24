<#
    ¿Está bien esta instalación? Sin gastar un céntimo.

    powershell -ExecutionPolicy Bypass -File herramientas\comprobar.ps1

    ES LO QUE SE EJECUTA POR TELÉFONO. El desarrollador está en España, ella
    dice «no me funciona», y esto convierte esa frase en una lista de líneas
    que puede leer en voz alta o mandar en una foto.

    ESTE ARCHIVO ES CASI TODO CÁSCARA, Y ES DELIBERADO.
    Lo único que hace es encontrar un Python y pasarle el trabajo a
    `herramientas\comprobar.py`. La lógica vive en Python porque ahí se PUEDE
    PROBAR: la sesión del 3 septiembre 2026 escribió un .ps1 que parecía
    correcto y tenía tres fallos, y los tres solo salieron al ejecutarlo. Lo
    que queda aquí es lo que Python no puede comprobar por definición —si hay
    Python, para empezar— y eso son veinte líneas, no doscientas.

    NO GASTA NADA: ni un token, ni una petición de cuota, ni una llamada.

    Y SE GUARDA COMO UTF-8 CON BOM. Windows PowerShell 5.1 lee los .ps1 como
    ANSI si no lo llevan, y los acentos de estos mensajes llegarían rotos
    justo a la persona que los tiene que leer. Pasó el 3 septiembre 2026.
#>

[CmdletBinding()]
param(
    # Para cuando no hay red: se salta la única comprobación que la usa.
    [switch]$SinRed
)

$ErrorActionPreference = 'Continue'
$RAIZ = Split-Path -Parent $PSScriptRoot

# El del entorno virtual primero: es el que tiene las dependencias. El del
# sistema sirve igual para dar el diagnóstico, y decirlo es parte del
# diagnóstico — «tienes Python pero no el entorno» es un fallo distinto de «no
# tienes Python», y el remedio también.
$python = Join-Path $RAIZ '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $delSistema = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $delSistema) {
        Write-Host ''
        Write-Host '  [FALTA] Python no está instalado en este equipo.'
        Write-Host ''
        Write-Host '          Se baja de https://www.python.org/downloads/windows/'
        Write-Host '          Al instalarlo, marca la casilla «Add python.exe to PATH».'
        Write-Host '          Después ejecuta herramientas\instalar.ps1'
        Write-Host ''
        exit 1
    }
    Write-Host ''
    Write-Host '  [AVISO] No hay entorno virtual (.venv): se comprueba con el Python'
    Write-Host '          del sistema. Ejecuta herramientas\instalar.ps1 para crearlo.'
    Write-Host ''
    $python = $delSistema.Source
}

$argumentos = @((Join-Path $RAIZ 'herramientas\comprobar.py'))
if ($SinRed) { $argumentos += '--sin-red' }

& $python @argumentos
exit $LASTEXITCODE
