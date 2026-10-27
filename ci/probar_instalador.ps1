# Pruebas del instalador en Windows PowerShell 5.1 (GitHub Actions, windows-latest). No instala nada.
$ErrorActionPreference = "Stop"
$fallas = 0
function Chequear($ok, $texto) {
    if ($ok) { Write-Host "OK    $texto" } else { Write-Host "FALLA $texto"; $script:fallas++ }
}

Chequear ($PSVersionTable.PSVersion -ge [Version]"5.1" -and $PSVersionTable.PSEdition -eq "Desktop") "corre en Windows PowerShell 5.1 ($($PSVersionTable.PSVersion))"

# 1. El instalador parsea (como lo hace [scriptblock]::Create al bajarlo con irm).
$errores = $null
$tokens = $null
[void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot "..\install.ps1"), [ref]$tokens, [ref]$errores)
Chequear ($errores.Count -eq 0) "install.ps1 sin errores de parseo ($(@($errores | ForEach-Object { $_.Message + ' (linea ' + $_.Extent.StartLineNumber + ')' }) -join '; '))"
$texto = [IO.File]::ReadAllText((Join-Path $PSScriptRoot "..\install.ps1"))
$sb = $null
try { $sb = [scriptblock]::Create($texto) } catch { }
Chequear ($null -ne $sb) "[scriptblock]::Create del instalador funciona"

# 2. Un error no terminante de un cmdlet de ScheduledTasks, dentro de una funcion hija, llega al catch
#    con el mismo patron de defaults que usa el instalador (y la sesion conserva sus defaults).
$PSDefaultParameterValues["Invoke-WebRequest:UseBasicParsing"] = $true
$resultado = & {
    $ErrorActionPreference = "Stop"
    $defaultsLocales = @{}
    foreach ($k in $PSDefaultParameterValues.Keys) { $defaultsLocales[$k] = $PSDefaultParameterValues[$k] }
    $defaultsLocales["*-ScheduledTask*:ErrorAction"] = "Stop"
    $PSDefaultParameterValues = $defaultsLocales
    function Consultar { Get-ScheduledTaskInfo -TaskName "diforte-no-existe-$(Get-Random)" | Out-Null; "siguio" }
    $heredado = $PSDefaultParameterValues.ContainsKey("Invoke-WebRequest:UseBasicParsing")
    try { Consultar } catch { "corto" }
    if ($heredado) { "heredado" }
}
Chequear ($resultado -contains "corto" -and $resultado -notcontains "siguio") "un error de Get-ScheduledTaskInfo en una funcion hija corta ($($resultado -join ','))"
Chequear ($resultado -contains "heredado") "los defaults de la sesion se conservan dentro del instalador"
Chequear (-not $PSDefaultParameterValues.ContainsKey("*-ScheduledTask*:ErrorAction")) "la sesion no queda modificada"

# Control: sin ese default, el mismo error NO corta (es el bucle de v1.3.3). Si esto cambia, la prueba de arriba
# dejaria de demostrar algo.
$sinDefault = & {
    $ErrorActionPreference = "Stop"
    function Consultar { Get-ScheduledTaskInfo -TaskName "diforte-no-existe-$(Get-Random)" 2>$null | Out-Null; "siguio" }
    try { Consultar } catch { "corto" }
}
Chequear ($sinDefault -contains "siguio") "control: sin el default, el error de ScheduledTasks no corta ($($sinDefault -join ','))"

if ($fallas -gt 0) { Write-Host "$fallas fallas"; exit 1 }
Write-Host "0 fallas"
