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
#
# No pide permisos de administrador. Correrlo de nuevo = actualizar (conserva .env.garro-sync y los logs).
# Todo corre en su propio scope: no deja variables (ni contrasenas) en la sesion de PowerShell.
# Este archivo es solo ASCII a proposito: asi se lee igual con "irm | iex" y desde disco.

param(
    [switch]$Reconfigurar,
    [switch]$Desinstalar,
    [string]$Hora = "13:00"
)

$tlsAnterior = [Net.ServicePointManager]::SecurityProtocol
try {
& {
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ---------------------------------------------------------------- constantes
$Version         = "v1.0.0"
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
    "garro_sync.py" = "6038097ec57160019d896c9e2a458f062b4de2fec029b01fdabe4ac69da58fae"
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
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
            Enable-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue | Out-Null
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
    foreach ($t in @($TareaDiaria, $TareaPedidos)) {
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

Write-Host "garro-sync $Version" -ForegroundColor Cyan

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
    $cfg["GARRO_HOST"]   = Preguntar "GARRO_HOST" $act["GARRO_HOST"] "IP de Radmin del servidor de Garro."
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
# Interactive = corren con la sesion iniciada y NO se guarda ninguna contrasena de Windows.
$principal = New-ScheduledTaskPrincipal -UserId $usuario -LogonType Interactive -RunLevel Limited
$scriptPy = Join-Path $Dir "garro_sync.py"
$ajustes = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew

$accionDiaria = New-ScheduledTaskAction -Execute $Pyw -Argument "`"$scriptPy`"" -WorkingDirectory $Dir
$disparadorDiario = New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.Add([TimeSpan]::Parse($Hora)))
Register-ScheduledTask -TaskName $TareaDiaria -Action $accionDiaria -Trigger $disparadorDiario `
    -Settings $ajustes -Principal $principal -Description "Copia Gestion Integral a Supabase todos los dias." -Force | Out-Null
Ok "$TareaDiaria (todos los dias a las $Hora)"

# Repeticion cada 5 minutos SIN duracion: en Windows 10 (1607+) es indefinida. NO usar
# -RepetitionDuration ([TimeSpan]::MaxValue): en Windows 10/11 Register-ScheduledTask lo rechaza (fuera de rango).
$accionPedidos = New-ScheduledTaskAction -Execute $Pyw -Argument "`"$scriptPy`" --si-hay-pedido" -WorkingDirectory $Dir
$disparadorPedidos = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
try {
    Register-ScheduledTask -TaskName $TareaPedidos -Action $accionPedidos -Trigger $disparadorPedidos `
        -Settings $ajustes -Principal $principal -Description "Cada 5 minutos mira si alguien pidio Actualizar ahora." -Force | Out-Null
} catch {
    Aviso "Windows no acepto la repeticion sin duracion; se reintenta con 10 anios."
    $disparadorPedidos = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    Register-ScheduledTask -TaskName $TareaPedidos -Action $accionPedidos -Trigger $disparadorPedidos `
        -Settings $ajustes -Principal $principal -Description "Cada 5 minutos mira si alguien pidio Actualizar ahora." -Force | Out-Null
}
# Se verifica lo que quedo registrado: el intervalo tiene que ser PT5M (5 minutos).
$rep = (Get-ScheduledTask -TaskName $TareaPedidos).Triggers[0].Repetition
$dur = "$($rep.Duration)"
if ($rep.Interval -ne "PT5M" -or ($dur -ne "" -and $dur -ne "P3650D")) {
    throw "La tarea '$TareaPedidos' quedo con una repeticion inesperada (intervalo '$($rep.Interval)', duracion '$dur'; se esperaba PT5M y duracion vacia o P3650D). Avisa a Alexander."
}
Ok "$TareaPedidos (cada 5 minutos, repeticion: intervalo $($rep.Interval), duracion '$($rep.Duration)' - vacia = indefinida)"

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
Write-Host "Las tareas corren con la sesion de $usuario iniciada: la PC tiene que estar prendida, con Radmin conectado."
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
