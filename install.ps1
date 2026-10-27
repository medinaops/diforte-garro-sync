# Instalador de garro-sync (Gestion Integral -> Supabase) para una PC Windows 10/11.
#
#   Instalar o actualizar:
#     irm https://raw.githubusercontent.com/medinaops/diforte-garro-sync/main/install.ps1 | iex
#
#   Con opciones (PowerShell 5.1):
#     $i = [scriptblock]::Create((irm https://raw.githubusercontent.com/medinaops/diforte-garro-sync/main/install.ps1))
#     & $i -Reconfigurar      # vuelve a preguntar la configuracion
#     & $i -Hora "02:00"      # cambia la hora de la copia diaria
#     & $i -Desinstalar       # borra tareas y acceso directo
#     & $i -Servidor          # en el servidor de Garro: las tareas corren sin sesion iniciada
#     & $i -Interactivo       # vuelve al modo PC de oficina (tareas con la sesion iniciada)
#
# Modo servidor (-Servidor): las tareas usan S4U ("ejecutar tanto si el usuario inicio sesion como si no",
# sin guardar ninguna contrasena de Windows). Si las tareas ya estaban en S4U, una actualizacion comun
# (irm | iex) conserva el modo. Antes de terminar corre una prueba real como tarea S4U (lee Garro y prueba
# Supabase, sin escribir nada). Requiere el derecho "Iniciar sesion como proceso por lotes"; puede pedir
# abrir PowerShell como administrador.
#
# No pide permisos de administrador. Correrlo de nuevo = actualizar (conserva .env.garro-sync y los logs).
# Todo corre en su propio scope: no deja variables (ni contrasenas) en la sesion de PowerShell.
# Este archivo es solo ASCII a proposito: asi se lee igual con "irm | iex" y desde disco.

param(
    [switch]$Reconfigurar,
    [switch]$Desinstalar,
    [switch]$Servidor,
    [switch]$Interactivo,
    [string]$Hora = "13:00"
)

$tlsAnterior = [Net.ServicePointManager]::SecurityProtocol
try {
& {
$ErrorActionPreference = "Stop"
# Los cmdlets de ScheduledTasks (CDXML) NO respetan $ErrorActionPreference del script: sin esto un fallo
# (por ejemplo, Windows no deja crear una tarea S4U) se muestra en rojo y el script sigue como si nada.
# Las llamadas con -ErrorAction explicito (SilentlyContinue) mantienen el suyo. Se copia lo que ya tenga la
# sesion (por ejemplo, un proxy para Invoke-WebRequest) en un diccionario local: la sesion no se modifica.
$defaultsLocales = @{}
foreach ($k in $PSDefaultParameterValues.Keys) { $defaultsLocales[$k] = $PSDefaultParameterValues[$k] }
$defaultsLocales["*-ScheduledTask*:ErrorAction"] = "Stop"
$PSDefaultParameterValues = $defaultsLocales
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ---------------------------------------------------------------- constantes
$Version         = "v1.4.0"
$Repo            = "medinaops/diforte-garro-sync"
# Por usuario: en C:\ cualquier usuario puede crear carpetas y plantar un python.exe; en LOCALAPPDATA no.
$Dir             = Join-Path $env:LOCALAPPDATA "Diforte\garro-sync"
# Python aislado: paquete NuGet oficial (zip). No toca el registro ni pisa un Python del usuario.
# SHA-256 calculado al bajar el .nupkg; el SHA-512 que publica nuget.org (catalogo) coincide:
# u9pNz2iKlCEbYtUJaKkbOPMF0LjR7NkCafdKhvigpPzrt8oWKgdTpHaR6z3wyWQAm9PYGUxv0Zr66NX9AeHMDw==
$PythonVersion   = "3.12.10"
$PythonUrl       = "https://www.nuget.org/api/v2/package/python/$PythonVersion"
$PythonSha256    = "0eb85c2dfccccf1b17352de4c397f69194035b7d37149eacc16f1147d93de3b8"
# SHA-256 de cada archivo del zip del tag. Se regeneran con:  python3 actualizar_hashes.py
# BEGIN HASHES
$HashesPaquete = @{
    "actualizar-ahora.bat" = "82b5fc5df5775d5d6680c085c5b5f417cc5f7658be8ecc8a3b9029218776e177"
    "garro_sync.py" = "bb198a396fb40e74358f02305d2d07847034c8b91190b02f86bca95d971ce2de"
    "relevar_compras.py" = "99ab32c337c8e25ed4c6eb23c85188fb53802ddfffa6d39849c9d0437883a5cf"
    "requirements.txt" = "b3e919e8eac4bfae6f8d92ab116ee3fef65ce487bc818cbcbb38228be74e3703"
    "supabase-ca-2021.crt" = "700723581420dd1ac98fd7e9ac529f0ef210eadcaf87fc868a3ad7d114c2f3b7"
}
# END HASHES
$SupabaseHost    = "aws-1-us-east-2.pooler.supabase.com"
$SupabasePuerto  = "5432"
$SupabaseUsuario = "garro_sync.gvftctuqqskoghlblqem"
$TareaDiaria     = "Diforte Garro - copia diaria"
$TareaPedidos    = "Diforte Garro - pedidos"
$NombreAtajo     = "Actualizar Garro ahora.lnk"
$ArchivoConfig   = ".env.garro-sync"

$Py          = Join-Path $Dir "python\python.exe"
$Pyw         = Join-Path $Dir "python\pythonw.exe"
$Config      = Join-Path $Dir $ArchivoConfig
$MarcaPython = Join-Path $Dir "python-verificado.txt"
$Atajo       = Join-Path ([Environment]::GetFolderPath("Desktop")) $NombreAtajo

function Paso($texto) { Write-Host ""; Write-Host "== $texto" -ForegroundColor Cyan }
function Ok($texto)   { Write-Host "   OK  $texto" -ForegroundColor Green }
function Aviso($texto){ Write-Host "   !   $texto" -ForegroundColor Yellow }

function Confirmar($pregunta) {
    $r = Read-Host "$pregunta (s/N)"
    return ($r.Trim().ToLower() -eq "s")
}

function Leer-Config($ruta) {
    $h = [ordered]@{}
    if (Test-Path $ruta) {
        foreach ($linea in [IO.File]::ReadAllLines($ruta)) {
            $l = $linea.Trim()
            if ($l -eq "" -or $l.StartsWith("#") -or -not $l.Contains("=")) { continue }
            $i = $l.IndexOf("=")
            $h[$l.Substring(0, $i).Trim()] = $l.Substring($i + 1).Trim()
        }
    }
    return $h
}

# Llamadas a Python sin que el stderr de un proceso nativo (PS 5.1 con ErrorAction Stop) corte el script.
function Version-Py {
    $ErrorActionPreference = "Continue"
    $o = & $Py -c "import sys;print(sys.version)" 2>$null
    if ($LASTEXITCODE -ne 0) { return "" }
    return "$o"
}
function Pip-Funciona {
    $ErrorActionPreference = "Continue"
    $null = & $Py -m pip --version 2>&1
    return ($LASTEXITCODE -eq 0)
}

# Al actualizar: se deshabilitan las dos tareas y se espera (hasta 60 s) a que termine una corrida en curso,
# para no pisar python\ ni los archivos con un pythonw andando. Se rehabilitan al final, tambien si algo falla.
$tareasPausadas = New-Object System.Collections.ArrayList
function Hay-Corrida-En-Curso {
    foreach ($t in @($TareaDiaria, $TareaPedidos)) {
        $tarea = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        if ($tarea -and $tarea.State -eq "Running") { return $true }
    }
    $procs = @(Get-Process -Name python, pythonw -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and $_.Path.StartsWith($Dir, [StringComparison]::OrdinalIgnoreCase) } catch { $false }
    })
    return ($procs.Count -gt 0)
}
function Pausar-Tareas {
    foreach ($t in @($TareaDiaria, $TareaPedidos)) {
        $tarea = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        if ($tarea -and $tarea.State -ne "Disabled") {
            [void]$tareasPausadas.Add($t)
            Disable-ScheduledTask -TaskName $t | Out-Null
        }
    }
    $limite = (Get-Date).AddSeconds(60)
    while (Hay-Corrida-En-Curso) {
        if ((Get-Date) -gt $limite) {
            throw "Hay una copia en curso que no termino en 60 segundos. Espera a que termine y volve a correr el comando."
        }
        Start-Sleep -Seconds 2
    }
}
function Reanudar-Tareas {
    foreach ($t in $tareasPausadas) {
        try {
            if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
                Enable-ScheduledTask -TaskName $t -ErrorAction Stop | Out-Null
                if ("$((Get-ScheduledTask -TaskName $t -ErrorAction Stop).State)" -eq "Disabled") { throw "sigue deshabilitada" }
            }
        } catch {
            Write-Host "  ! No se pudo volver a habilitar la tarea '$t' ($($_.Exception.Message)): habilitarla a mano en el Programador de tareas y avisar a Alexander." -ForegroundColor Red
        }
    }
}

function Preguntar($texto, $porDefecto, $ayuda) {
    if ($ayuda) { Write-Host "   $ayuda" -ForegroundColor DarkGray }
    $sufijo = ""
    if ($porDefecto) { $sufijo = " [$porDefecto]" }
    while ($true) {
        $r = (Read-Host "$texto$sufijo").Trim()
        if ($r -eq "" -and $porDefecto) { $r = $porDefecto }
        if ($r -ne "") { return $r }
        Aviso "Este dato es obligatorio."
    }
}

function Preguntar-Secreto($texto, $actual) {
    $nota = ""
    if ($actual) { $nota = " (Enter = dejar la actual)" }
    while ($true) {
        $seguro = Read-Host "$texto$nota" -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($seguro)
        try { $plano = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        if ($plano -eq "" -and $actual) { return $actual }
        if ($plano -ne "") { return $plano }
        Aviso "Este dato es obligatorio."
    }
}

# ---------------------------------------------------------------- desinstalar
if ($Desinstalar) {
    Paso "Desinstalando garro-sync"
    foreach ($t in @($TareaDiaria, $TareaPedidos, "Diforte Garro - prueba de instalacion")) {
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $t -Confirm:$false
            Ok "Tarea borrada: $t"
        }
    }
    if (Test-Path $Atajo) { Remove-Item $Atajo -Force; Ok "Acceso directo borrado" }
    if (Test-Path $Dir) {
        if (Confirmar "Borrar tambien la carpeta $Dir (tiene la configuracion con las contrasenas, Python y los logs)?") {
            Remove-Item $Dir -Recurse -Force
            Ok "Carpeta borrada (incluye Python: no queda nada en Aplicaciones instaladas)"
        } else {
            Aviso "La carpeta queda en $Dir"
        }
    }
    return
}

if ($Hora -notmatch '^([01]?\d|2[0-3]):[0-5]\d$') {
    throw "La hora tiene que ser HH:MM, por ejemplo 13:00 (recibido: $Hora)."
}

# Requisitos: PowerShell 5.1 y Windows 8.1 / Server 2012 R2 o superior (el Python 3.12 no corre en anteriores).
if ($PSVersionTable.PSVersion -lt [Version]"5.1") {
    throw "Hace falta PowerShell 5.1 (hay $($PSVersionTable.PSVersion)). En Windows Server 2012 R2 se instala con Windows Management Framework 5.1. Avisa a Alexander."
}
if ([Environment]::OSVersion.Version -lt [Version]"6.3") {
    throw "Este Windows ($([Environment]::OSVersion.Version)) es anterior a Windows 8.1 / Server 2012 R2: el Python del programa no corre. Avisa a Alexander."
}

# Modo servidor: pedido con -Servidor, o heredado si alguna de las tareas existentes ya esta en S4U.
# -Interactivo vuelve al modo PC de oficina.
if ($Servidor -and $Interactivo) { throw "Elegir -Servidor o -Interactivo, no los dos." }
$modosPrevios = @(foreach ($t in @($TareaDiaria, $TareaPedidos)) {
    $tp = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    if ($tp) { "$($tp.Principal.LogonType)" }
})
$ModoServidor = (-not $Interactivo) -and ($Servidor -or ($modosPrevios -contains "S4U"))
if ($ModoServidor -and -not $Servidor -and ($modosPrevios | Where-Object { $_ -ne "S4U" })) {
    Aviso "Las tareas existentes estaban en modos distintos ($($modosPrevios -join ', ')): quedan las dos en modo servidor."
}

Write-Host "garro-sync $Version" -ForegroundColor Cyan
if ($ModoServidor) { Write-Host "Modo servidor: las tareas corren sin sesion iniciada." -ForegroundColor Cyan }

# ---------------------------------------------------------------- carpeta + permisos (ANTES de escribir secretos)
Paso "Carpeta y permisos"
$miSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$SidsConfiables = @($miSid, "S-1-5-32-544", "S-1-5-18")
if (Test-Path $Dir) {
    # Si la carpeta ya existe, tiene que ser de este usuario, de Administradores o del sistema.
    $dueno = (Get-Acl $Dir).GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($SidsConfiables -notcontains $dueno) {
        throw "La carpeta $Dir ya existe y su duenio ($dueno) no es este usuario, Administradores ni el sistema. No se instala. Avisa a Alexander."
    }
} else {
    New-Item -ItemType Directory -Path $Dir -Force | Out-Null
}
& icacls $Dir /setowner "*$miSid" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "No se pudo fijar el duenio de $Dir (icacls fallo con codigo $LASTEXITCODE)." }
# Sin herencia; control total solo para este usuario, Administradores (S-1-5-32-544) y SYSTEM (S-1-5-18).
& icacls $Dir /inheritance:r /grant:r "*${miSid}:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "No se pudieron aplicar los permisos de $Dir (icacls fallo con codigo $LASTEXITCODE)." }
Ok "$Dir (solo este usuario, Administradores y el sistema)"

# ---------------------------------------------------------------- pausar tareas (se rehabilitan en el finally del final)
try {
Paso "Tareas existentes"
Pausar-Tareas
Ok "Sin corridas en curso"

# ---------------------------------------------------------------- codigo + Python
Paso "Descargando el programa $Version"
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("garro-sync-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    $zip = Join-Path $tmp "codigo.zip"
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$Repo/archive/refs/tags/$Version.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath (Join-Path $tmp "codigo") -Force
    $origen = Get-ChildItem (Join-Path $tmp "codigo") -Directory | Select-Object -First 1
    if (-not $origen) { throw "El zip descargado esta vacio." }
    # Primero se verifica TODO; recien despues se copia algo a la carpeta de instalacion.
    foreach ($f in $HashesPaquete.Keys) {
        $desde = Join-Path $origen.FullName $f
        if (-not (Test-Path $desde)) { throw "Falta $f en la version $Version." }
        $h = (Get-FileHash -Path $desde -Algorithm SHA256).Hash.ToLower()
        if ($h -ne $HashesPaquete[$f]) {
            throw "El archivo $f del paquete NO coincide con la huella esperada ($h). No se instala. Avisa a Alexander."
        }
    }
    foreach ($f in $HashesPaquete.Keys) { Copy-Item (Join-Path $origen.FullName $f) (Join-Path $Dir $f) -Force }
    Set-Content -Path (Join-Path $Dir "version.txt") -Value $Version -Encoding Ascii
    Ok "Programa verificado y copiado a $Dir"

    Paso "Python (aislado, no toca el de la PC)"
    # No se confia en un python.exe que "ya estaba": tiene que figurar en la marca que dejo este instalador
    # (version + huella del paquete verificado) y decir esa misma version.
    $confiable = $false
    if ((Test-Path $Py) -and (Test-Path $MarcaPython)) {
        $marca = Leer-Config $MarcaPython
        if ($marca["version"] -eq $PythonVersion -and $marca["sha256"] -eq $PythonSha256) {
            if ((Version-Py).StartsWith("$PythonVersion ")) { $confiable = $true }
        }
    }
    if ($confiable) {
        Ok "Ya instalado y verificado: $(& $Py --version)"
    } else {
        $destPython = Join-Path $Dir "python"
        if (Test-Path $destPython) {
            Aviso "Hay un Python que no corresponde a lo verificado; se reinstala."
            Remove-Item $destPython -Recurse -Force
        }
        Remove-Item $MarcaPython -Force -ErrorAction SilentlyContinue
        $nupkg = Join-Path $tmp "python.nupkg"
        Write-Host "   Descargando Python $PythonVersion (paquete NuGet oficial)..."
        Invoke-WebRequest -UseBasicParsing -Uri $PythonUrl -OutFile $nupkg
        $hash = (Get-FileHash -Path $nupkg -Algorithm SHA256).Hash.ToLower()
        if ($hash -ne $PythonSha256) {
            throw "El paquete de Python descargado NO coincide con la huella esperada ($hash). No se instala. Avisa a Alexander."
        }
        Ok "Huella SHA-256 verificada"
        # Un .nupkg es un zip: se extrae solo tools\ a la carpeta python (sin instalador, sin registro).
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $raiz = [IO.Path]::GetFullPath($destPython) + [IO.Path]::DirectorySeparatorChar
        $arch = [IO.Compression.ZipFile]::OpenRead($nupkg)
        try {
            foreach ($e in $arch.Entries) {
                if (-not $e.FullName.StartsWith("tools/") -or $e.FullName.EndsWith("/")) { continue }
                $rel = $e.FullName.Substring(6).Replace("/", "\")
                $dest = [IO.Path]::GetFullPath((Join-Path $raiz $rel))
                if (-not $dest.StartsWith($raiz, [StringComparison]::OrdinalIgnoreCase)) {
                    throw "El paquete de Python trae una ruta invalida ($($e.FullName))."
                }
                New-Item -ItemType Directory -Path (Split-Path $dest) -Force | Out-Null
                [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $dest, $true)
            }
        } finally { $arch.Dispose() }
        if (-not (Test-Path $Py)) { throw "El paquete de Python no trajo python.exe." }
        $v = Version-Py
        if (-not $v.StartsWith("$PythonVersion ")) { throw "El Python instalado no es la version esperada ($v)." }
        if (-not (Pip-Funciona)) {
            & $Py -m ensurepip --default-pip
            if ($LASTEXITCODE -ne 0) { throw "No se pudo preparar pip en el Python aislado." }
        }
        [IO.File]::WriteAllText($MarcaPython, "version=$PythonVersion`r`nsha256=$PythonSha256`r`n", (New-Object Text.UTF8Encoding($false)))
        Ok "Instalado: $(& $Py --version)"
    }
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------- dependencias
Paso "Dependencias"
& $Py -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: -r (Join-Path $Dir "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "No se pudieron instalar las dependencias (pip fallo con codigo $LASTEXITCODE)." }
Ok "psycopg2 instalado con huellas verificadas"

# ---------------------------------------------------------------- configuracion
Paso "Configuracion"
if ($Reconfigurar -or -not (Test-Path $Config)) {
    $act = Leer-Config $Config
    $cfg = [ordered]@{}
    $sugerido = $act["GARRO_HOST"]
    if (-not $sugerido -and $ModoServidor) { $sugerido = "127.0.0.1" }
    $cfg["GARRO_HOST"]   = Preguntar "GARRO_HOST" $sugerido "En el servidor de Garro: 127.0.0.1. En otra PC: la IP de Radmin del servidor."
    $cfg["GARRO_PUERTO"] = Preguntar "GARRO_PUERTO" $(if ($act["GARRO_PUERTO"]) { $act["GARRO_PUERTO"] } else { "5432" }) $null
    $cfg["GARRO_BASE"]   = Preguntar "GARRO_BASE" $(if ($act["GARRO_BASE"]) { $act["GARRO_BASE"] } else { "GestionIntegral" }) $null
    $cfg["GARRO_USUARIO"] = Preguntar "GARRO_USUARIO" $(if ($act["GARRO_USUARIO"]) { $act["GARRO_USUARIO"] } else { "garro_lectura" }) $null
    $cfg["GARRO_PASSWORD"] = Preguntar-Secreto "Contrasena de GARRO_USUARIO (no se muestra)" $act["GARRO_PASSWORD"]
    $cfg["SUPABASE_HOST"]    = $SupabaseHost
    $cfg["SUPABASE_PUERTO"]  = $SupabasePuerto
    $cfg["SUPABASE_USUARIO"] = $SupabaseUsuario
    $cfg["SUPABASE_PASSWORD"] = Preguntar-Secreto "Contrasena de Supabase para garro_sync (no se muestra)" $act["SUPABASE_PASSWORD"]
    foreach ($k in $act.Keys) { if (-not $cfg.Contains($k)) { $cfg[$k] = $act[$k] } }  # conserva extras, ej. GARRO_ENCODING

    foreach ($k in $cfg.Keys) {
        if ($cfg[$k] -match "[\r\n]") { throw "El valor de $k tiene un salto de linea; no es valido." }
    }
    $lineas = foreach ($k in $cfg.Keys) { "$k=$($cfg[$k])" }
    # UTF-8 SIN BOM: garro_sync.py lee el archivo como utf-8 y un BOM rompe la primera clave.
    [IO.File]::WriteAllText($Config, (($lineas -join "`r`n") + "`r`n"), (New-Object Text.UTF8Encoding($false)))
    Ok "Guardado en $Config"
} else {
    Ok "Se conserva $ArchivoConfig (para cambiarla: -Reconfigurar)"
}

# ---------------------------------------------------------------- pruebas
Paso "Prueba de lectura de Garro (no escribe nada)"
Push-Location $Dir
try {
    & $Py (Join-Path $Dir "garro_sync.py") --prueba
    $codigoPrueba = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($codigoPrueba -ne 0) {
    throw "La prueba de lectura de Garro fallo (mira el mensaje de arriba). No se crearon las tareas. Revisa el dato indicado y volve a correr el comando; para cambiar los datos agrega -Reconfigurar."
}
Ok "Garro se lee bien"

Paso "Prueba de conexion con Supabase (no escribe nada)"
Push-Location $Dir
try {
    & $Py (Join-Path $Dir "garro_sync.py") --prueba-destino
    $codigoDestino = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($codigoDestino -ne 0) {
    throw "La conexion con Supabase fallo (mira el mensaje de arriba; suele ser la contrasena de garro_sync mal escrita). No se crearon las tareas. Corre el comando de nuevo con -Reconfigurar."
}
Ok "Supabase responde"

# ---------------------------------------------------------------- primera copia
Paso "Primera copia"
if (Confirmar "Hacer la primera copia a Supabase ahora?") {
    Push-Location $Dir
    try {
        & $Py (Join-Path $Dir "garro_sync.py")
        $codigoCopia = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($codigoCopia -eq 0) { Ok "Copia hecha" }
    else { Aviso "La copia no se completo (mira el mensaje de arriba). Las tareas se crean igual y reintentan solas." }
} else {
    Aviso "Se saltea. La primera copia sale a las $Hora o con 'Actualizar Garro ahora'."
}

# ---------------------------------------------------------------- tareas programadas
Paso "Tareas programadas"
$usuario = "$env:USERDOMAIN\$env:USERNAME"
# Interactive = corren con la sesion iniciada. S4U (modo servidor) = corren aunque nadie haya iniciado sesion.
# En los dos casos NO se guarda ninguna contrasena de Windows.
$tipoSesion = if ($ModoServidor) { "S4U" } else { "Interactive" }
$principal = New-ScheduledTaskPrincipal -UserId $usuario -LogonType $tipoSesion -RunLevel Limited
$scriptPy = Join-Path $Dir "garro_sync.py"
$ajustes = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew

# Modo servidor: prueba real con el mismo principal S4U ANTES de tocar las tareas reales (que siguen pausadas):
# lee Garro y prueba Supabase sin escribir nada. Si falla (derecho de inicio por lotes, GPO, red de la tarea),
# se corta aca y las tareas anteriores quedan como estaban.
if ($ModoServidor) {
    Paso "Prueba de las tareas sin sesion iniciada (no escribe nada)"
    $TareaPrueba = "Diforte Garro - prueba de instalacion"
    foreach ($argPrueba in @("--prueba", "--prueba-destino")) {
        $accionPrueba = New-ScheduledTaskAction -Execute $Pyw -Argument "`"$scriptPy`" $argPrueba" -WorkingDirectory $Dir
        try {
            Register-ScheduledTask -TaskName $TareaPrueba -Action $accionPrueba -Settings $ajustes -Principal $principal `
                -Description "Prueba temporal del instalador de garro-sync." -Force | Out-Null
            $antes = (Get-ScheduledTaskInfo -TaskName $TareaPrueba).LastRunTime
            Start-ScheduledTask -TaskName $TareaPrueba
            $limite = (Get-Date).AddMinutes(10)
            do {
                Start-Sleep -Seconds 3
                $estado = (Get-ScheduledTask -TaskName $TareaPrueba).State
                $info = Get-ScheduledTaskInfo -TaskName $TareaPrueba
                $termino = ($estado -ne "Running") -and ($info.LastRunTime -ne $antes) -and ($info.LastTaskResult -ne 267009)
            } until ($termino -or (Get-Date) -gt $limite)
            if (-not $termino) {
                throw "no termino en 10 minutos (estado $estado, resultado $($info.LastTaskResult))"
            }
            if ($info.LastTaskResult -ne 0) {
                throw "termino con codigo $($info.LastTaskResult) (1 = fallo del programa: el motivo queda en $Dir\garro_sync.log)"
            }
            Ok "$argPrueba corre bien sin sesion iniciada"
        } catch {
            throw "La prueba $argPrueba como tarea sin sesion iniciada fallo: $($_.Exception.Message). Las tareas anteriores no se tocaron. Si no es un fallo del programa, suele faltar el derecho 'Iniciar sesion como proceso por lotes' para ${usuario}: abri PowerShell con 'Ejecutar como administrador' (con este mismo usuario) y volve a correr el instalador con -Servidor."
        } finally {
            # Borrar la tarea no corta el programa que ya arranco: primero se detiene.
            Stop-ScheduledTask -TaskName $TareaPrueba -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $TareaPrueba -Confirm:$false -ErrorAction SilentlyContinue
        }
    }
}

# Respaldo de las tareas actuales: si algo falla desde el primer registro hasta la verificacion, se restauran
# tal como estaban (o se borran las nuevas, si no habia). Sin contrasenas: S4U e Interactive no las guardan.
$respaldoTareas = @{}
foreach ($t in @($TareaDiaria, $TareaPedidos)) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) { $respaldoTareas[$t] = Export-ScheduledTask -TaskName $t }
}
try {
    $accionDiaria = New-ScheduledTaskAction -Execute $Pyw -Argument "`"$scriptPy`"" -WorkingDirectory $Dir
    $disparadorDiario = New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.Add([TimeSpan]::Parse($Hora)))
    try {
        Register-ScheduledTask -TaskName $TareaDiaria -Action $accionDiaria -Trigger $disparadorDiario `
            -Settings $ajustes -Principal $principal -Description "Copia Gestion Integral a Supabase todos los dias." -Force | Out-Null
    } catch {
        if ($ModoServidor) {
            throw "Windows no dejo crear la tarea para que corra sin sesion iniciada ($($_.Exception.Message)). Abri PowerShell con 'Ejecutar como administrador' (con este mismo usuario) y volve a correr el instalador con -Servidor."
        }
        throw
    }
    Ok "$TareaDiaria (todos los dias a las $Hora)"

    # Repeticion cada 5 minutos SIN duracion: en Windows 10 (1607+) es indefinida. NO usar
    # -RepetitionDuration ([TimeSpan]::MaxValue): en Windows 10/11 Register-ScheduledTask lo rechaza (fuera de rango).
    $accionPedidos = New-ScheduledTaskAction -Execute $Pyw -Argument "`"$scriptPy`" --si-hay-pedido" -WorkingDirectory $Dir
    $disparadorPedidos = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
    try {
        Register-ScheduledTask -TaskName $TareaPedidos -Action $accionPedidos -Trigger $disparadorPedidos `
            -Settings $ajustes -Principal $principal -Description "Cada 5 minutos mira si alguien pidio Actualizar ahora." -Force | Out-Null
    } catch {
        $primerError = $_.Exception.Message
        Aviso "Windows rechazo la tarea de pedidos ($primerError); se reintenta con repeticion de 10 anios."
        $disparadorPedidos = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) `
            -RepetitionDuration (New-TimeSpan -Days 3650)
        try {
            Register-ScheduledTask -TaskName $TareaPedidos -Action $accionPedidos -Trigger $disparadorPedidos `
                -Settings $ajustes -Principal $principal -Description "Cada 5 minutos mira si alguien pidio Actualizar ahora." -Force | Out-Null
        } catch {
            if ($ModoServidor) {
                throw "Windows no dejo crear la tarea de pedidos para que corra sin sesion iniciada ($primerError / $($_.Exception.Message)). Abri PowerShell con 'Ejecutar como administrador' (con este mismo usuario) y volve a correr el instalador con -Servidor."
            }
            throw
        }
    }
    # Se verifica lo que quedo registrado: el tipo de sesion pedido y el intervalo PT5M (5 minutos).
    foreach ($t in @($TareaDiaria, $TareaPedidos)) {
        $registrado = "$((Get-ScheduledTask -TaskName $t).Principal.LogonType)"
        if ($registrado -ne $tipoSesion) {
            throw "La tarea '$t' quedo con sesion '$registrado' y se esperaba '$tipoSesion'. Avisa a Alexander."
        }
    }
    $rep = (Get-ScheduledTask -TaskName $TareaPedidos).Triggers[0].Repetition
    $dur = "$($rep.Duration)"
    if ($rep.Interval -ne "PT5M" -or ($dur -ne "" -and $dur -ne "P3650D")) {
        throw "La tarea '$TareaPedidos' quedo con una repeticion inesperada (intervalo '$($rep.Interval)', duracion '$dur'; se esperaba PT5M y duracion vacia o P3650D). Avisa a Alexander."
    }
    Ok "$TareaPedidos (cada 5 minutos, repeticion: intervalo $($rep.Interval), duracion '$($rep.Duration)' - vacia = indefinida)"
} catch {
    $motivo = $_.Exception.Message
    $sinRestaurar = New-Object System.Collections.ArrayList
    foreach ($t in @($TareaDiaria, $TareaPedidos)) {
        try {
            if ($respaldoTareas.ContainsKey($t)) {
                Register-ScheduledTask -TaskName $t -Xml $respaldoTareas[$t] -Force | Out-Null
                if (-not (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue)) { throw "no quedo registrada" }
            } elseif (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
                Unregister-ScheduledTask -TaskName $t -Confirm:$false
            }
        } catch {
            [void]$sinRestaurar.Add("$t ($($_.Exception.Message))")
        }
    }
    if ($sinRestaurar.Count -gt 0) {
        throw "No se pudieron dejar las tareas programadas ($motivo) y la restauracion quedo INCOMPLETA: $($sinRestaurar -join '; '). Revisar en el Programador de tareas y avisar a Alexander."
    }
    throw "No se pudieron dejar las tareas programadas ($motivo). Se restauraron las definiciones anteriores y al salir se vuelven a habilitar (si aparece un aviso en rojo, esa hay que habilitarla a mano). Avisa a Alexander."
}


# ---------------------------------------------------------------- acceso directo
Paso "Acceso directo"
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($Atajo)
$lnk.TargetPath = Join-Path $Dir "actualizar-ahora.bat"
$lnk.WorkingDirectory = $Dir
$lnk.Description = "Copia Gestion Integral a Supabase ahora"
$lnk.Save()
Ok "Escritorio: $NombreAtajo"

# ---------------------------------------------------------------- resumen
Write-Host ""
Write-Host "================ LISTO ================" -ForegroundColor Green
Write-Host "Version:  $Version"
Write-Host "Carpeta:  $Dir"
Write-Host "Tareas:   $TareaDiaria (a las $Hora)"
Write-Host "          $TareaPedidos (cada 5 minutos)"
if ($ModoServidor) {
    Write-Host "Modo servidor: las tareas corren como $usuario aunque nadie haya iniciado sesion."
} else {
    Write-Host "Las tareas corren con la sesion de $usuario iniciada: la PC tiene que estar prendida, con Radmin conectado."
}
Write-Host ""
Write-Host "Para actualizar ahora:"
Write-Host "  - en esta PC: doble clic en '$NombreAtajo' (escritorio)"
Write-Host "  - desde el sistema: Costos > Gastos > 'Actualizar ahora' (se procesa en menos de 5 minutos)"
Write-Host "Logs: $Dir\garro_sync.log"

} finally {
    Reanudar-Tareas
}
}
} finally {
    [Net.ServicePointManager]::SecurityProtocol = $tlsAnterior
}
