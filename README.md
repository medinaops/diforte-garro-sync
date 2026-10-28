# garro-sync: copia de Gestión Integral a Supabase

Copia los comprobantes de venta y compra, las cotizaciones, los pagos a proveedores con sus medios, los cheques, los movimientos de fondos, clientes, proveedores, productos y catálogos de **Gestión Integral** (Garro Sistemas) a una base de Supabase, para el módulo de costos de Diforte. Corre en el servidor de Garro o en una PC de oficina de Windows. Este repo es público a propósito: no lleva contraseñas, IPs ni datos internos (esos se piden al instalar y quedan solo en esa PC).

- Lee Gestión Integral con un usuario de **solo lectura** y la sesión abierta en modo solo lectura.
- Escribe en Supabase con un rol que solo puede vaciar e insertar en su schema, por conexión verificada (`sslmode=verify-full` con la CA de Supabase que viaja en este repo, y SCRAM obligatorio).
- Cada copia es completa y en una sola transacción: queda la foto nueva entera o la anterior. Antes de reemplazar nada se valida la foto (ver «Guardia»).
- No abre ningún puerto: la conexión sale de la PC.

## Requisitos

- Windows 10/11 o Windows Server 2012 R2 o posterior, con **PowerShell 5.1** (lo trae Windows 10/11 y Server 2016+; en 2012 R2 se instala con Windows Management Framework 5.1). En una PC no hace falta ser administrador; en el servidor puede hacer falta (ver abajo).
- **En el servidor de Garro (recomendado):** salida a internet. Con `-Servidor`, las tareas corren aunque nadie haya iniciado sesión.
- **En una PC de oficina:** tiene que llegar al servidor de Garro (VPN) y a internet, y estar prendida con la sesión iniciada para que corran las tareas.
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
& $i -Servidor            # modo servidor: las tareas corren sin sesión iniciada
& $i -Interactivo         # vuelve al modo PC de oficina (tareas con la sesión iniciada)
```

### Instalar en el servidor de Garro

Así la copia no depende de que una PC de oficina esté prendida.

1. Entrar al servidor por escritorio remoto, con el usuario que va a quedar a cargo de las tareas.
2. Abrir PowerShell y correr:
   ```powershell
   $i = [scriptblock]::Create((irm https://raw.githubusercontent.com/medinaops/diforte-garro-sync/main/install.ps1))
   & $i -Servidor
   ```
3. En `GARRO_HOST` poner `127.0.0.1`: la base está en el mismo servidor y no hace falta VPN. PostgreSQL tiene que aceptar conexiones locales (`pg_hba.conf`); casi siempre viene así.
4. Las tareas quedan en modo S4U («ejecutar tanto si el usuario inició sesión como si no»), sin guardar ninguna contraseña de Windows. Eso usa el derecho **«Iniciar sesión como proceso por lotes»** del usuario (los administradores lo tienen por defecto). Si Windows no deja crearlas así, el instalador lo dice: hay que abrir PowerShell con «Ejecutar como administrador», con el mismo usuario, y repetir.
5. **Antes de crear las tareas**, el instalador corre una **prueba real como tarea sin sesión** (lee Garro y prueba Supabase, sin escribir nada). Si falla, frena con el motivo y las tareas que hubiera de antes quedan como estaban. Si el programa llegó a correr, el detalle queda en `garro_sync.log`; si Windows no dejó ni arrancar la tarea, el motivo es el que muestra el instalador. Si algo falla al crear o verificar las tareas reales, el instalador restaura las que había antes.
6. Las actualizaciones siguientes se hacen con el comando común (`irm … | iex`): si alguna de las tareas ya estaba en modo servidor, lo conservan. Para volver al modo PC de oficina: `& $i -Interactivo`.
7. Cuando el servidor ya copia bien, en la PC de oficina: `& $i -Desinstalar`, para que no queden dos equipos copiando (un candado impide que se pisen, pero es ruido).

El relevamiento (`relevar_compras.py`) se corre igual, desde la carpeta de instalación en el servidor.

## Pedir una actualización

- **En la PC:** doble clic en **Actualizar Garro ahora** (escritorio). Corre la copia, muestra el resultado y espera una tecla.
- **Desde el sistema** de Diforte (Costos > Gastos > «Actualizar ahora», solo administración): la PC lo toma en menos de 5 minutos. Si estaba apagada, el pedido espera hasta 24 horas. Si la copia falla, el pedido se reintenta solo cada 5 minutos, hasta 3 intentos (si lo rechaza la guardia, no se reintenta): después queda como fallido y se puede pedir otro. Una copia diaria o manual también atiende el pedido pendiente.

## Relevamiento de compras

`relevar_compras.py` registra qué filas escribe Gestión Integral cuando se guarda un comprobante de compra. Usa la misma conexión de solo lectura que la copia: en Garro no puede escribir. Los modos manuales (abajo) dejan todo en la carpeta `relevamiento\` de la PC y no suben nada. Contraseñas, tokens e imágenes nunca salen de la base.

```powershell
cd "$env:LOCALAPPDATA\Diforte\garro-sync"
.\python\python.exe relevar_compras.py catalogo   # una vez: estructura de la base y comprobantes de muestra
.\python\python.exe relevar_compras.py foto       # justo antes de cargar un comprobante
# ...se carga UN comprobante en Gestión Integral, como siempre...
.\python\python.exe relevar_compras.py comparar   # justo después: lista las tablas que cambiaron
.\python\python.exe relevar_compras.py pagos      # estructura y ejemplos de pagos, cheques, cajas y fondos
```

El modo `pagos` vuelca las columnas, claves y cantidad de filas de las tablas de pagos, cheques, cajas y movimientos de fondos, las tablas chicas de referencia (tipos de pago, cajas, bancos, condiciones de compra) y los últimos pagos, movimientos y cheques con todo lo que cuelga de ellos. De números de cuenta, tarjeta y CBU y de los CUIT solo quedan los últimos 4 caracteres. De proveedores, clientes, operarios, firmantes de cheques y sus tablas auxiliares (`clientes_*`, `proveedores_*`) solo salen las columnas de una lista permitida (ids, razón social del proveedor, fechas, condiciones): domicilios, contactos y observaciones no salen, ni en `catalogo` ni en `comparar`. En todas las tablas de cuentas, tarjetas y billeteras, y en las que las vinculan con pagos y fondos, los números y comprobantes quedan con los últimos 4 y las observaciones no salen. Tampoco salen los códigos de tarjeta.

`comparar` vuelca las filas nuevas o modificadas solo de las tablas del circuito de compras, pagos, caja, cuentas y productos (lista permitida). De cualquier otra tabla, y siempre de clientes, ventas, recibos, presupuestos, pedidos, remitos en tránsito, repartos, operarios y configuración, informa nada más cuántas filas cambiaron. De `logs_sistema` solo salen los logs de compras, pagos, movimientos de fondos, ajustes de cuenta corriente de proveedores y productos (sin el nombre del equipo); los demás se cuentan. Los hashes de las fotos llevan una clave aleatoria que se genera la primera vez y queda en `.clave-relevamiento`, al lado del script y fuera de la carpeta `relevamiento\` que se comparte. No sale de ninguna contraseña, así que ninguna fila queda con un hash comparable. `comparar` rechaza una foto de otra versión o de otra clave: hay que sacar una foto nueva antes de cargar el comprobante.

### Modo automático (desde la v1.5.0)

Nadie tiene que sacar fotos a mano: la tarea de cada 5 minutos (`--si-hay-pedido`) hace, después de atender el pedido de actualización (haya o no), una pasada de `relevar_compras.py auto`. Primero mira los contadores de cambios de las tablas de compras, pagos, fondos, ajustes, plan de cuentas y asientos (barato: no lee las tablas). Si no cambiaron, no hace nada. Si cambiaron, saca una foto, la compara con la anterior con la misma lógica y la misma redacción que `comparar` y, si cambió alguna tabla del circuito de compras, **sube la diferencia ya redactada** a la tabla privada `garro.relevamientos` de Supabase (de clientes, ventas, operarios y configuración solo sale la cantidad de cambios: ni filas, ni claves, ni hashes). Si la diferencia pasa de 4 MB, las tablas con más filas suben solo la cantidad.

- Cada diferencia es el cambio neto entre dos fotos consecutivas, no la huella de un comprobante: si en la misma ventana se cargan varios, vienen juntos y los estados intermedios se pierden.
- Lo local queda en `relevamiento\auto\<id>\` (una carpeta por base de origen): `estado.json`, la foto base y las últimas 200 diferencias (`diferencia-*.json`).
- La primera pasada (o tras reiniciar el servidor de Garro, o si cambia la clave o la versión del relevamiento) solo toma la foto base y no sube nada.
- Si la subida falla (red caída, falta la migración 373 en Supabase), la diferencia queda en disco, el log lo avisa en una línea y la próxima pasada sigue desde la foto nueva.
- Una pasada que tarda más de 5 minutos no se pisa con la siguiente: usan un candado del sistema operativo sobre `relevamiento\auto\<id>\lock`, que Windows suelta solo si el proceso muere (no hay vencimientos). Cada pasada tiene además un tope de costo de 25 minutos: si la foto completa tarda más, se corta sin tocar el estado, queda una línea en el log y conviene apagar el modo automático (`RELEVAMIENTO_AUTO=0`).
- Cualquier error del relevamiento queda en `garro_sync.log` y nunca cambia el resultado de la tarea ni de la copia.
- Para apagarlo: `RELEVAMIENTO_AUTO=0` en `.env.garro-sync`. Para probarlo a mano: `.\python\python.exe relevar_compras.py auto`.

## Guardia

Antes de vaciar el espejo se valida lo leído. Si algo no cierra, **no se publica nada y sigue la copia anterior**; el motivo queda registrado y se ve en el sistema.

- Las tablas clave (comprobantes, ítems, clientes, proveedores, productos, tipos, monedas) nunca pueden venir vacías.
- Desde la v1.4.0 tampoco pueden venir vacíos pagos, cheques y movimientos de fondos.
- Algunos medios de pago que Gestión Integral tiene, pero que no se usaban cuando se armó la copia, no se copian: tarjetas, billeteras virtuales, depósitos en efectivo, cheques en movimientos de fondos, retenciones de Ganancias, percepciones y saldos sin imputar. Si alguno empieza a usarse, la copia frena con un aviso, para no dejar pagos o gastos sin su importe, hasta que se lo sume a la copia.
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
