@echo off
REM Actualizar Garro ahora: copia Gestion Integral a Supabase con el Python privado.
cd /d "%~dp0"
if not exist "%~dp0python\python.exe" (
  echo No se encontro el Python de garro-sync. Volve a correr el comando de instalacion del README.
  pause
  exit /b 1
)
echo Copiando Gestion Integral a Supabase... puede tardar unos minutos.
echo.
"%~dp0python\python.exe" garro_sync.py
set RESULTADO=%errorlevel%
echo.
if %RESULTADO%==0 (
  echo LISTO: la copia termino bien.
) else (
  echo HUBO UN ERROR: mira el mensaje de arriba. Detalle en garro_sync.log.
)
pause
exit /b %RESULTADO%
