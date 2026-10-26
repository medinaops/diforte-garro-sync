# garro-sync: copia de Gestión Integral a Supabase

Copia los comprobantes de venta y compra, las cotizaciones, clientes, proveedores, productos y catálogos de **Gestión Integral** (Garro Sistemas) a una base de Supabase, para el módulo de costos de Diforte. Corre en una PC de oficina de Windows. Este repo es público a propósito: no lleva contraseñas, IPs ni datos internos (esos se piden al instalar y quedan solo en esa PC).

- Lee Gestión Integral con un usuario de **solo lectura** y la sesión abierta en modo solo lectura.
- Escribe en Supabase con un rol que solo puede vaciar e insertar en su schema, por conexión verificada (`sslmode=verify-full` con la CA de Supabase que viaja en este repo, y SCRAM obligatorio).
- Cada copia es completa y en una sola transacción: queda la foto nueva entera o la anterior. Antes de reemplazar nada se valida la foto (ver «Guardia»).
- No abre ningún puerto: la conexión sale de la PC.

## Requisitos

- Windows 10 u 11, con **PowerShell 5.1** (el que trae Windows). No hace falta ser administrador.
- La PC tiene que llegar al servidor de Garro (VPN) y a internet, y estar prendida con la sesión iniciada para que corran las tareas.
- Tener a mano: la IP del servidor de Garro, la contraseña del usuario de solo lectura y la contraseña del rol de Supabase.

## Instalación (un solo comando)

En PowerShell:

```powershell
irm https://raw.githubusercontent.com/medinaops/diforte-garro-sync/main/install.ps1 | iex
```

El instalador, en `%LOCALAPPDATA%\Diforte\garro-sync\` (por usuario, con permisos solo para ese usuario, Administradores y el sistema):

1. Baja el programa de la versión fijada en el script y **verifica la huella SHA-256 de cada archivo** antes de copiarlo.
2. Instala un **Python aislado** (paquete NuGet oficial, un zip: no usa instalador, no toca el registro ni el Python de la PC) con huella verificada, y la dependencia con `pip --require-hashes`.
3. Pregunta la configuración (las contraseñas no se muestran) y la guarda en `.env.garro-sync`.
4. Prueba la lectura de Garro y la conexión con Supabase (ninguna escribe nada). Si una falla, frena con el motivo.
5. Pregunta si hacer la primera copia ahora.
6. Crea dos tareas programadas del usuario actual (sin guardar contraseñas de Windows):
   - **Diforte Garro - copia diaria**: todos los días a las 13:00 (o `-Hora`).
   - **Diforte Garro - pedidos**: cada 5 minutos mira si alguien pidió «Actualizar ahora».
7. Deja en el escritorio el acceso directo **Actualizar Garro ahora**.

### Actualizar el programa

Correr **el mismo comando** otra vez: baja la versión fijada en el script y conserva la configuración y los logs.

### Opciones

```powershell
$i = [scriptblock]::Create((irm https://raw.githubusercontent.com/medinaops/diforte-garro-sync/main/install.ps1))
& $i -Reconfigurar        # vuelve a preguntar la configuración (Enter deja el valor actual)
& $i -Hora "02:00"        # cambia la hora de la copia diaria
& $i -Desinstalar         # borra las tareas y el acceso directo; pregunta si borrar la carpeta
```

## Pedir una actualización

- **En la PC:** doble clic en **Actualizar Garro ahora** (escritorio). Corre la copia, muestra el resultado y espera una tecla.
- **Desde el sistema** de Diforte (Costos > Gastos > «Actualizar ahora», solo administración): la PC lo toma en menos de 5 minutos. Si estaba apagada, el pedido espera hasta 24 horas. Si la copia falla, el pedido se reintenta solo cada 5 minutos, hasta 3 intentos (si lo rechaza la guardia, no se reintenta): después queda como fallido y se puede pedir otro. Una copia diaria o manual también atiende el pedido pendiente.

## Relevamiento de compras (solo lectura)

`relevar_compras.py` registra qué filas escribe Gestión Integral cuando se guarda un comprobante de compra. Usa la misma conexión de solo lectura que la copia. Lo que genera queda en la carpeta `relevamiento\` de la PC y no se sube a ningún lado. Contraseñas, tokens e imágenes nunca salen de la base.

```powershell
cd "$env:LOCALAPPDATA\Diforte\garro-sync"
.\python\python.exe relevar_compras.py catalogo   # una vez: estructura de la base y comprobantes de muestra
.\python\python.exe relevar_compras.py foto       # justo antes de cargar un comprobante
# ...se carga UN comprobante en Gestión Integral, como siempre...
.\python\python.exe relevar_compras.py comparar   # justo después: lista las tablas que cambiaron
```

## Guardia

Antes de vaciar el espejo se valida lo leído. Si algo no cierra, **no se publica nada y sigue la copia anterior**; el motivo queda registrado y se ve en el sistema.

- Las tablas clave (comprobantes, ítems, clientes, proveedores, productos, tipos, monedas) nunca pueden venir vacías.
- Contra la última copia correcta: una tabla no puede pasar de tener filas a cero; comprobantes e ítems no pueden bajar más de 2 %; el resto, más de 20 %. Bajas de hasta 2 filas no alertan.

`--forzar` saltea la guardia. Es solo para uso manual cuando una baja es real y esperada; las tareas programadas nunca la usan.

## Si algo falla

- El resultado y los errores quedan en `%LOCALAPPDATA%\Diforte\garro-sync\garro_sync.log` (rota a `.1` pasados 5 MB). Las tareas corren sin ventana, así que ese archivo es donde mirar.
- Probar a mano, desde esa carpeta:

  ```
  python\python.exe garro_sync.py --prueba           # lee Garro y cuenta filas; no escribe nada
  python\python.exe garro_sync.py --prueba-destino   # prueba la conexión y la contraseña de Supabase; no escribe nada
  python\python.exe garro_sync.py                    # copia completa
  python\python.exe garro_sync.py --si-hay-pedido    # lo que hace la tarea de cada 5 minutos
  python\python.exe garro_sync.py --forzar           # copia saltando la guardia (solo manual)
  ```

- Acentos mal en `--prueba`: agregar `GARRO_ENCODING=WIN1252` (o `UTF8`) en `.env.garro-sync`.
- Si el servidor de Garro rechaza la conexión, el programa muestra el motivo real: la PC no está habilitada en el servidor, usuario o contraseña incorrectos (correr el instalador con `-Reconfigurar`) o base inexistente (revisar `GARRO_BASE`).
- Verificar que la tarea de pedidos quedó con repetición cada 5 minutos:

  ```powershell
  (Get-ScheduledTask -TaskName "Diforte Garro - pedidos").Triggers[0].Repetition
  # Interval = PT5M; Duration vacía = indefinida
  ```

## Mantenimiento (publicar una versión)

1. Dejar los archivos como van a quedar en el tag y correr los tests: `python -m pytest -q`.
2. Regenerar las huellas que `install.ps1` verifica (garro_sync.py, actualizar-ahora.bat, requirements.txt y la CA):

   ```
   python3 actualizar_hashes.py
   ```

   (`python3 actualizar_hashes.py --verificar` solo comprueba; los tests también lo hacen.)
3. Si cambió la versión, actualizar `$Version` en `install.ps1`, hacer commit y crear el tag con ese nombre.
4. Cambiar qué se copia (`TABLAS` en `garro_sync.py`): va una migración nueva en el repo de la base **antes** de publicar la versión nueva; si no, la copia falla entera y queda la anterior. `python garro_sync.py --ddl` imprime el SQL de las tablas.
