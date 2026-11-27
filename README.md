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

Correr **el mismo comando** otra vez: baja la versión fijada en el script y conserva la configuración y los logs. Desde la v1.10.0, y si no cambia Python, las tareas ni las dependencias, también se puede pedir a distancia (ver «Pedidos remotos»).

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

## Pedidos remotos (desde la v1.10.0)

Después de instalar la v1.10.0 **una vez** en el servidor, lo demás se pide desde la nube: la tarea de cada 5 minutos, al final de su pasada (después del pedido «Actualizar ahora», las cargas y el relevamiento), toma **un** pedido de la cola `garro.comandos` de Supabase (migración 693), lo corre y sube el resultado. Está **prendido por defecto** (es el objetivo: no hace falta tocar nada del servidor); se apaga con `COMANDOS=0` en `.env.garro-sync`. Detalle de diseño y operación en `docs/pedidos-remotos.md`.

**Cómo se pide** (solo como `postgres`, por psql; ninguna persona de la app puede pedirlos) y cómo se lee el resultado:

```sql
select garro.comandos_pedir('estructura', '{}');            -- devuelve el id del pedido
select id, tipo, estado, resultado->>'codigo_salida' as codigo, resultado->>'salida' as salida
  from garro.comandos order by id desc limit 5;            -- estado: pendiente, tomado, ok, error, vencido, sin_respuesta o cancelado
select garro.comandos_cancelar(<id>);                       -- solo un pendiente
```

Lo normal es verlo resuelto en menos de 5 minutos. Un pendiente que no se tomó en 2 horas pasa a `vencido`; uno tomado que no se resolvió en 60 minutos (el servidor se cayó a mitad) pasa a `sin_respuesta`. **Lo tomado nunca se vuelve a correr**: si hace falta, se pide de nuevo.

**La lista cerrada** (no hay otra forma de correr nada; cada tipo rechaza claves desconocidas y arma una lista de argumentos para un programa de este paquete, nunca un texto para un shell):

| Tipo | Qué corre | Args |
|---|---|---|
| `version` | `--version` de `garro_sync.py` y `garro_escritor.py` | ninguno |
| `estructura` | `garro_escritor.py --estructura` (solo lectura) | ninguno |
| `indices` | `garro_indices.py`: índices (con `pg_get_indexdef`, marcando los únicos), restricciones (`pg_get_constraintdef`: PK, UNIQUE, CHECK, FK), triggers y reglas de las tablas que toca el escritor, de la base real, en una sesión de solo lectura (PostgreSQL 9.3: solo catálogos; nada de filas). Primero un resumen de lo que impide duplicados y después el detalle. Hace falta la migración 695 | ninguno |
| `perfil` | `comparar_movimientos.py --perfil` (solo lectura) | ninguno |
| `copia_medir` | `garro_copia.py --medir` (solo lectura) | ninguno |
| `copia_crear` | `garro_copia.py --crear` | `base` |
| `copia_borrar` | `garro_copia.py --borrar` | `base` |
| `escritor_archivo` | `garro_escritor.py --archivo` con el `payload` en un archivo temporal que se borra | `payload` (objeto, ≤ 20 KB), `escribir` (bool, por defecto `false`), `base` |
| `comparar` | `comparar_movimientos.py --base ... --humano ... --bot ...` | `base`, `humano` (int), `bot` (int), `otro_tipo` (bool) |
| `deshacer` | `garro_escritor.py --deshacer ... --base ...` | `carga_id` (solo `copia-...`), `base`, `confirmar` (bool) |
| `logs` | últimas N líneas de `garro_sync.log` (y del `.1`) | `lineas` (1 a 400, por defecto 100) |
| `actualizar` | actualiza el paquete (abajo) | `version` (`vN.N.N`), `sha256_install` (64 hex) |

Cualquier tipo acepta además `equipo` (nombre de la PC): si viene, solo lo toma esa PC; la fila guarda en `equipo` qué PC lo tomó.

**Lo que nunca se puede hacer:**

- **Escribir en la base real de Garro.** `base` tiene que ser el nombre de una copia de prueba **y la base tiene que llevar la marca que deja `garro_copia.py --crear`** (su comentario): sin la marca, `garro_escritor.py` se niega a escribir o a borrar con `--base`, también a mano; el nombre solo no alcanza. El nombre es: `gestion_prueba`, o `gestion_prueba…_prueba` (tiene que cumplir además el patrón de `garro_copia.py`: terminar en `_prueba`), y distinto de `GARRO_BASE`. `escritor_archivo` con `escribir` exige `base`; sin `escribir` es la simulación de siempre (solo lee la base real); `deshacer` solo acepta cargas `copia-...`. El servidor valida de nuevo todo lo que llega, aunque venga de la cola ya validado.
- **Correr comandos libres**, ni tocar archivos fuera del paquete (`logs` lee solo `garro_sync.log` de la carpeta del programa).
- **Subir secretos.** Antes de subir cualquier salida se reemplaza por `***` cada valor secreto de `.env.garro-sync` (contraseñas, claves y cadenas de conexión, también codificados para URL, en minúscula o mayúscula, escapados como JSON o `repr()` y la contraseña de una cadena de conexión tal cual y decodificada; **no se redactan valores de menos de 4 caracteres: las contraseñas tienen que tener al menos 4**); la salida se recorta a 20.000 caracteres y el resultado a 64 KB.

El resultado de cada comando es `{codigo_salida, salida, segundos, version}`; `codigo_salida` 0 → estado `ok`, cualquier otro → `error` con la salida adentro (`-1` = no corrió o se cortó por tiempo). Cada tipo tiene su tope de tiempo y ninguno pasa de los 28 minutos contados desde que arrancó la tarea (que Windows corta a los 30): lo que queda de esos 28 minutos acota a cada comando. `copia_crear` (tope 25 minutos) **no corre** si lo que queda es menos que su tope: se resuelve `error` «pedila de nuevo». `actualizar` tiene un peor caso de 490 segundos y tampoco empieza si queda menos. Al empezar `copia_medir` y `copia_crear`, y después de cortar una copia, se borran los `copia-*.dump` huérfanos (el mensaje lo dice). `version` reparte un solo tope entre sus dos invocaciones. Dos corridas no ejecutan comandos a la vez (candado del sistema operativo; la tarea además no se superpone consigo misma). Un comando que falla no toca la copia de datos ni las cargas. Si la base de Supabase todavía no tiene la migración 693, se ignora con una línea en el log por hora.

### Actualizar el programa a distancia (`actualizar`)

Reemplaza **solo los archivos `.py` del paquete**. No re-registra tareas, no toca Python, la configuración, el certificado ni las dependencias: si la versión nueva necesita algo de eso, el comando termina en `error` con «requiere instalar en el equipo» y hay que correr el instalador en el servidor.

1. Publicar el tag de la versión (con `python3 actualizar_hashes.py` hecho antes) y calcular la huella de **su** `install.ps1` **desde el checkout local ya revisado, nunca bajándola de GitHub**:
   ```
   git -C diforte-garro-sync show v1.11.0:install.ps1 | shasum -a 256
   ```
2. `select garro.comandos_pedir('actualizar', '{"version":"v1.11.0","sha256_install":"<la huella>"}');`

Lo que hace el servidor: rechaza si el plazo que queda de la corrida es menor que el peor caso, si hay una actualización anterior sin cerrar o si la versión pedida es **menor** que la instalada (bajar de versión solo se hace en el equipo; la misma versión repara si hay diferencias). Baja el `install.ps1` del **tag pedido** (no de `main`) y verifica la huella; lee de él `$Version` (tiene que ser la pedida), `$HashesPaquete`, la versión de Python y `$VersionTareas`; rechaza la actualización si cambia el Python, `$VersionTareas` (la versión de lo que define las tareas, que `install.ps1` deja en `tareas-version.txt`), `garro_recuperar.py` o cualquier archivo que no sea un `.py` (el certificado, `requirements.txt`, el `.bat`); baja el zip del tag, verifica cada archivo contra `$HashesPaquete` y lo arma en una carpeta de staging (si la red falla antes de este punto no se tocó nada). Después, con el candado de la copia en Supabase tomado (si hay una copia en curso no actualiza): respalda los archivos actuales en `actualizacion-respaldo\` (con `copy2`), escribe la marca `actualizacion-en-curso` (versión vieja y nueva, archivos, cuáles ya existían y el id del comando) y los reemplaza (y borra su `__pycache__`). **Suelta el candado de la copia** y corre el autochequeo con el código nuevo en subprocesos, cada uno con su tope (20 s las cuatro comprobaciones de versión y de arranque, 60 s el autochequeo del paquete): `--version` igual a la pedida, el argv EXACTO de las dos tareas programadas con `GARRO_SYNC_AUTOCHEQUEO=1` (solo parsea e importa todos los módulos de esos caminos) y el autochequeo del paquete (import de todo y una lectura de Garro y de Supabase que no escribe nada). Si falla, restaura el respaldo, vuelve a chequear y devuelve `error` con el detalle; si sale bien, borra la marca y devuelve `ok` con la versión vieja y la nueva (`version_nueva` en el resultado).

**Si el proceso muere a mitad** (corte de luz, tarea matada): queda la marca. `garro_sync.py` la mira al arrancar (tareas y copia diaria), antes de importar el resto del paquete, usando `garro_recuperar.py` (solo biblioteca estándar, **nunca se reemplaza a distancia**): si ningún otro proceso está actualizando (candado `comandos.lock` libre), restaura los archivos desde el respaldo, deja `actualizacion-recuperada.json`, escribe una línea en el log y relanza la corrida con los archivos ya consistentes (una sola vez: el hijo lleva `GARRO_SYNC_RELANZADO=1` y nunca vuelve a relanzar; si la marca no se puede borrar o vuelve a aparecer, es «incompleta»). La recuperación corre antes que `argparse` y que cualquier import del paquete. Antes de restaurar verifica el sha256 de cada archivo del respaldo contra la marca: si no coincide, no toca nada. La siguiente pasada de comandos cierra con `error` la fila que había quedado `tomado`, con un resultado que lo explica. Mientras hay una marca sin resolver, la corrida solo hace el latido y deja un log claro (nada de copia, cargas ni relevamiento) y los comandos no toman ninguno nuevo: si había uno tomado se resuelve con un error que lo explica; hace falta instalar en el equipo. Si al terminar bien no se pudo borrar la marca, se la reescribe con `estado: ok` y la recuperación la borra sin restaurar nada. El respaldo, los archivos nuevos y la marca se fuerzan a disco (`fsync`) antes de reemplazar.

Los comandos van al final de la pasada a propósito: después de reemplazar archivos, esa corrida no importa nada más del paquete.

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

## Carga de compras (apagada por defecto)

`garro_escritor.py` puede cargar en Gestión Integral los comprobantes de compra que alguien aprobó en Supabase, escribiendo las mismas filas que escribe el programa al cargarlos a mano. Es la única parte de garro-sync que escribe en Gestión Integral, y **no hace nada** hasta que estén las tres llaves:

1. `CARGAS=1` en `.env.garro-sync` (sin esto la tarea ni mira la cola).
2. `CARGAS_REAL=1` en `.env.garro-sync` (sin esto las cargas «reales» se procesan en seco: se calcula lo que se escribiría y se informa, sin tocar nada).
3. El interruptor del lado de Supabase, que decide si una carga se aprueba como real.

Otras claves: `CARGAS_POR_PASADA` (cuántas cargas procesa cada pasada de 5 minutos; por defecto 1, tope 5) y `CARGAS_OPERARIO` (usuario propio del bot en el programa; por defecto `botcompras`). La conexión usa las mismas credenciales `GARRO_*`: el usuario tiene que poder escribir en las tablas de compras.

El proveedor de cada comprobante puede venir ya decidido en la aprobación (`proveedor_garro`: el id del proveedor en el programa). En ese caso el escritor usa ese proveedor, lo relee del programa y exige que exista y esté activo; en una Factura A además exige que el CUIT del comprobante sea el que ese proveedor tiene en el programa, y en un Remito X alcanza con el id. Sin ese dato (cargas encoladas antes), lo busca como siempre: por CUIT, o por nombre exacto si el comprobante no trae CUIT.

Cada comprobante va en una sola transacción con las tablas de compras, asientos y pagos bloqueadas: si algo no cierra, no queda nada. Antes de escribir se comprueba que las tablas tengan exactamente las columnas y tipos esperados y ningún trigger ni regla; si una actualización del programa los cambia, frena. Después de escribir, y antes de confirmar, se relee todo y se comprueba (en centavos) que el total y el asiento coincidan con lo planificado. Lo que no está confirmado contra cargas reales (otros tipos de comprobante, IVA distinto de 21 %, medios de pago poco usados, etc.) se rechaza y queda para cargar a mano. Si Gestión Integral está ocupada o no responde, la carga vuelve a la cola y se reintenta. Si una carga se cortó sin saberse si llegó a escribirse, primero se busca en el programa lo que el bot ya cargó: si está, se da por cargada; si no se puede comprobar, no se hace nada hasta que lo revise una persona.

Cada escritura deja una línea en `cargas-diario.jsonl` (solo clave, total, base e ids; nunca el contenido del comprobante) y guarda una huella del contenido escrito: `--deshacer` solo borra si todo sigue exactamente como se escribió y no hay nada más colgado del comprobante; si alguien lo tocó en el programa, hay que anularlo desde el programa.

```powershell
python garro_escritor.py --estructura                  # prueba la instalación: estructura y lecturas (solo lectura)
python garro_escritor.py --archivo carga.json          # planifica una carga contra Gestión Integral, sin escribir
python garro_escritor.py --archivo carga.json --escribir --base COPIA   # la escribe en una COPIA de la base (nunca en la real); muestra su id «copia-...»
python garro_escritor.py --deshacer N                  # carga de la cola: muestra qué borraría (solo lectura; la cola tiene que responder y decir «cargada»)
python garro_escritor.py --deshacer N --confirmar      # la borra y la marca «deshecha» en la cola
python garro_escritor.py --deshacer copia-... --base COPIA [--confirmar]   # una carga hecha a mano en una copia (no toca la cola)
python garro_escritor.py --pasada                      # lo que hace la tarea de cada 5 minutos
```

La base real solo se escribe por la cola: `--archivo --escribir` se rechaza si no se indica otra base con `--base`.

### Movimientos de fondos (desde la v1.9.0)

La misma cola puede traer movimientos de fondos de egreso (`clase` = `movimiento_fondo`), pagados en efectivo (caja 1) o por transferencia (cuenta bancaria 11), solo de los tipos de movimiento comercio e industria, cargas sociales, sindicato y pagos a la AFIP. Mismas tres llaves y mismos modos que las compras (`--archivo`, `--escribir --base`, `--deshacer`, `--estructura` y `--pasada` valen para las dos clases). Las filas que escribe están en `docs/movimientos-de-fondos.md`.

- Escribe el movimiento, su efectivo o su movimiento bancario y el vínculo entre los dos. **Nunca escribe asiento contable**: si el tipo pasa a generar asiento en el programa, rechaza.
- Rechaza (queda para cargar a mano) un tipo fuera de los cuatro, un medio que no sea efectivo o transferencia, una caja o una cuenta bancaria que no sean las habilitadas, un importe que no sea mayor que cero y a centavos, una fecha futura o de más de 60 días, un movimiento marcado para revisar o como posible duplicado, y lo que ya está cargado o parece estarlo (ver abajo). `requiere_revision` ausente se trata como «para revisar» (falla cerrado): la propuesta tiene que traerlo en `false`. `posible_duplicado` el escritor lo acepta en `false` o `null`, pero la cola (`compras_movimiento_encolar`) solo encola `null`: hay que mandar `null`.
- **Duplicados**, con la misma regla que `compras_asistente_movimientos_parecidos` (migración 666) y los mismos casos (`fixtures/casos_duplicados.json`): *ya cargado* = mismo tipo con el período escrito en la descripción («10/2026», «10-26», «2026/10», «OCTUBRE 2026», «OCT/26»; «PER 10» o el mes solo, sin año, solo si el movimiento cae entre el primer día del período y 4 meses después; una fecha completa no es un período), y en los tipos 58 y 13 además la misma sigla (lo que va antes de «PER») como palabra. *Posible duplicado* = lo mismo con otra sigla; o, con un importe de $1000 o más, un importe a menos de $1 (suma de efectivo, banco y otro medio) del mismo tipo a ±40 días de hoy o entre el primer día del período y 4 meses después, o de un egreso de cualquier otro tipo a ±10 días de hoy. Además, y solo en el escritor (la 666 no lo tiene, va del lado seguro): la misma descripción exacta de un movimiento del mismo tipo dentro de esa ventana es *ya cargado*.
- La verificación previa de la estructura y el bloqueo son propios de esta clase (solo se bloquea `movimientos_fondos`, unos milisegundos): un cambio en las tablas de compras no frena los movimientos ni al revés. `--estructura` revisa las dos y dice cuál no coincide.
- `--deshacer` borra el movimiento, el efectivo o el movimiento bancario y el vínculo, solo si siguen idénticos a lo escrito y nada más cuelga de ellos (ni un pago, ni un recibo, ni un asiento).

Contrato de la cola para un movimiento: `payload.propuesta` con `clase`, `tipo_mov_id`, `descripcion`, `periodo` (AAAA-MM), `importe`, `medio` (`efectivo` | `transferencia`), `caja` = 1 siempre, `cuenta_bancaria_id` = 11 en transferencia y `null` en efectivo, `fecha`, `requiere_revision` y `posible_duplicado`; y `payload.total_pesos` = `importe`. Los ejemplos están en `fixtures/` y los usa un test (`test_garro_movimientos.py`). Para regenerarlos con el payload real: copiar lo que genera el flujo que encola, y reemplazar todo dato real (descripción, organismo, importe, fecha) por valores de ejemplo antes de commitear; el repo es público.

**Antes de habilitar movimientos reales** (`tipos_mov_reales` en la cola) es obligatorio, en la copia de prueba: `python comparar_movimientos.py --perfil` y `python comparar_movimientos.py --base COPIA --humano <id de una persona> --bot <id del bot>` para un efectivo y para una transferencia. Dos columnas del movimiento bancario (`obs` y `nro`) no están confirmadas: el relevamiento las redacta, y el bot escribe cadena vacía.

**Permisos del usuario de la conexión** (`GARRO_USUARIO`): además de lo de compras, INSERT, UPDATE y DELETE sobre `movimientos_fondos`, `efectivo_medio_de_pago`, `efectivo_medio_de_pago_en_movimientos_fondos`, `movimientos_cuentas_bancarias` y `movimientos_cuenta_bancaria_en_movimientos_fondos`; SELECT sobre `movimientos_fondos_tipos`, `cajas`, `cuentas_bancarias`, `otro_medio_de_pago` y `otro_medio_de_pago_en_movimientos_fondos`; y LOCK TABLE sobre `movimientos_fondos` (en PostgreSQL requiere UPDATE, DELETE o TRUNCATE sobre la tabla). `--deshacer` toma las cinco filas con `SELECT … FOR UPDATE`, que en PostgreSQL requiere UPDATE sobre esas tablas (no las modifica).

### Numeración automática del Remito X (desde la v1.10.1)

Cuando el papel no trae número, la propuesta de un Remito X puede venir con `"numerar": "siguiente"`, `"numero": null` y `"punto_venta"` null o un entero. Sin `numerar` todo sigue igual: número y punto de venta obligatorios, del papel. Con cualquier otro tipo, `numerar` se rechaza (`numerar_tipo`), y también un valor distinto de `siguiente` o un `numero` que no sea null.

- **Punto de venta:** el de la propuesta; si no viene, el del ÚLTIMO Remito X activo del proveedor (el `id_comprobante_compra` más alto); si no tiene ninguno, 1.
- **Número:** el del último Remito X activo del proveedor con ese punto de venta (por id, no el máximo) + 1, o 1 si no hay; si el proveedor ya tiene un comprobante activo con ese número (de cualquier tipo y punto de venta: el mismo criterio de choque que el chequeo de duplicados), sigue sumando hasta uno libre. Los anulados no cuentan.
- Se calcula **dentro de la transacción de escritura, con las tablas ya bloqueadas**: una carga a mano simultánea no puede chocar (si el programa tiene una carga en curso, la del bot vuelve a la cola sin escribir).
- En seco, el plan trae `numeracion` (`punto_venta`, `numero`, `numero_lindo`) con la nota «a confirmar al cargar». Una carga real devuelve `punto_venta` y `numero` asignados (y los guarda en `escrito.numeracion` del diario); la huella de `--deshacer` cubre las filas con el número realmente escrito.
- **Posible duplicado:** el número es nuevo por construcción, así que «ya cargado» no dispara. Con la tabla ya bloqueada se busca un Remito X activo del mismo proveedor, de la misma fecha y del mismo total (en centavos, tolerancia de 1): si existe, `NoCargable("posible_duplicado")` con el id y el número del existente (también en seco).
- **Retomada** de una carga numerada que se cortó sin saber si se escribió: primero el número que el diario local alcanzó a anotar (`cargada` o `intento`: proveedor + número + usuario del bot); si no, proveedor + Remito X + usuario del bot + fecha + total. Un único resultado es «ya cargada» (si el diario guardó la huella, tiene que coincidir; si no, `revisar` / `huella_distinta`); ninguno, se vuelve a cargar como una carga nueva; más de uno, `revisar` / `varios_propios` con el motivo.

Si la cola todavía no tiene `cargas_tomar(text[])`, la pasada vuelve a `cargas_tomar()` y las compras siguen funcionando.

`comparar_movimientos.py` compara fila por fila un movimiento cargado por el bot en una copia contra uno cargado por una persona, y `comparar_movimientos.py --perfil` cuenta (sin mostrar textos) cómo se guardan las columnas todavía sin confirmar.

### Copia de prueba de la base

Para probar la carga sin tocar la base real, `garro_copia.py` hace una copia en el mismo servidor. Se corre a mano, en el servidor donde está la base; de la base real solo lee, y el programa se puede seguir usando mientras tanto.

```powershell
python garro_copia.py --medir                  # solo lectura: cuánto ocupa la base, cuánto disco hay y si se puede copiar
python garro_copia.py --crear gestion_prueba   # crea la copia (el nombre termina en _prueba)
python garro_copia.py --borrar gestion_prueba  # borra una copia creada con este programa
```

- Usa `pg_dump` y `pg_restore` del propio servidor (los busca al lado de la carpeta de datos; si están en otro lado, indicar la carpeta en `GARRO_PG_BIN`). Tienen que ser de la misma versión que el servidor, y el usuario configurado tiene que ser superusuario: `--medir` lo comprueba y avisa.
- Antes de copiar exige el doble del tamaño de la base más 500 MB libres en el disco, y corta si durante la copia el disco baja de 1 GB. La copia ocupa lo mismo que la base hasta que se borra.
- Conviene hacerla en un horario de poca actividad: se lee la base entera una vez y se escribe otra igual. El programa se puede seguir usando, pero si en ese rato necesita reorganizar una tabla, esa operación espera a que termine la lectura.
- Si falla o se corta a mitad, borra lo que alcanzó a crear; si no puede, lo dice en pantalla con el comando para borrarlo.
- `--borrar` solo borra bases que este programa creó (les deja una marca) y nunca la configurada en `GARRO_BASE`. Si la ventana se cerró justo al empezar, la base puede haber quedado sin la marca: en ese caso se borra a mano.

## Guardia

Antes de vaciar el espejo se valida lo leído. Si algo no cierra, **no se publica nada y sigue la copia anterior**; el motivo queda registrado y se ve en el sistema.

- Las tablas clave (comprobantes, ítems, clientes, proveedores, productos, tipos, monedas) nunca pueden venir vacías.
- Desde la v1.4.0 tampoco pueden venir vacíos pagos, cheques y movimientos de fondos.
- Desde la v1.6.0 se copian las percepciones de las compras (`regimenes_especiales` y `regimenes_especiales_x_comprobantes_compras`); pueden venir vacías.
- Algunos medios de pago que Gestión Integral tiene, pero que no se usaban cuando se armó la copia, no se copian: tarjetas, billeteras virtuales, depósitos en efectivo, cheques en movimientos de fondos, retenciones de Ganancias y saldos sin imputar. Si alguno empieza a usarse, la copia frena con un aviso, para no dejar pagos o gastos sin su importe, hasta que se lo sume a la copia.
- Contra la última copia correcta: una tabla no puede pasar de tener filas a cero; comprobantes e ítems no pueden bajar más de 2 %; el resto, más de 20 %. Bajas de hasta 2 filas no alertan.

`--forzar` saltea la guardia. Es solo para uso manual cuando una baja es real y esperada; las tareas programadas nunca la usan.

## Si algo falla

- El resultado y los errores quedan en `%LOCALAPPDATA%\Diforte\garro-sync\garro_sync.log` (rota a `.1` pasados 5 MB). Las tareas corren sin ventana, así que ese archivo es donde mirar.
- Probar a mano, desde esa carpeta:

  ```
  python\python.exe garro_sync.py --prueba           # lee Garro y cuenta filas; no escribe nada
  python\python.exe garro_sync.py --prueba-destino   # prueba la conexión y la contraseña de Supabase; no escribe nada
  python\python.exe garro_sync.py --version          # versión instalada (también de garro_escritor.py y garro_comandos.py)
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
2. Regenerar las huellas que `install.ps1` verifica (los `.py` del paquete, actualizar-ahora.bat, requirements.txt y la CA):

   ```
   python3 actualizar_hashes.py
   ```

   (`python3 actualizar_hashes.py --verificar` solo comprueba; los tests también lo hacen.)
3. Si cambió la versión, actualizar `$Version` en `install.ps1` **y `VERSION` en `garro_sync.py`** (un test las compara), hacer commit y crear el tag con ese nombre. Si tocaste la sección «Tareas programadas» de `install.ps1`, subir también `$VersionTareas` (un test avisa si esa sección cambia): así una actualización remota se rechaza y pide instalar en el equipo.
4. Cambiar qué se copia (`TABLAS` en `garro_sync.py`): va una migración nueva en el repo de la base **antes** de publicar la versión nueva; si no, la copia falla entera y queda la anterior. `python garro_sync.py --ddl` imprime el SQL de las tablas.
