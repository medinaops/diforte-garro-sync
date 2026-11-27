# Pedidos remotos al servidor (garro-sync v1.10.0)

Cómo se opera, y por qué está hecho así. La lista de comandos y sus argumentos están en el README («Pedidos remotos»).

## Piezas

- **Cola** `garro.comandos` en Supabase (migración 693 del repo de la base): RLS activa y sin políticas; solo se toca por funciones `SECURITY DEFINER` con `search_path` vacío. `comandos_pedir` y `comandos_cancelar`: **solo `postgres`** (se pide por psql; no hay rama `is_admin()` a propósito: ninguna persona de la app puede pedirlos; sin permisos para `service_role` ni `n8n_writer`). `comandos_tomar(version, equipo)` y `comandos_resolver`: solo el rol `garro_sync`. Un pedido con `args.equipo` lo toma solo esa PC; la fila guarda en `equipo` cuál lo tomó.
- **`garro_comandos.py`**: valida de nuevo cada comando, arma la invocación como lista de argumentos y la corre con el mismo Python del paquete (si la tarea corre con `pythonw.exe`, usa el `python.exe` de al lado para poder capturar la salida). Sube el resultado ya redactado.
- **`garro_actualizar.py`**: el comando `actualizar`.
- **`garro_recuperar.py`**: solo biblioteca estándar y **nunca se reemplaza a distancia**. Lo primero que corre `garro_sync.py` (tareas y copia diaria): si quedó una actualización a medias, restaura el respaldo y relanza la corrida.
- **`COMANDOS`**: prendido por defecto (`COMANDOS=0` en `.env.garro-sync` lo apaga).
- **`garro_sync.py --si-hay-pedido`** (la tarea de cada 5 minutos): `pedido -> cargas -> relevamiento -> comandos`. Los comandos van al final: una actualización remota reemplaza archivos y esa corrida no debe importar nada después.

## Comando `indices` (v1.10.1)

Pregunta, de solo lectura, si Gestión Integral impide cargar dos veces el mismo comprobante: lista, de cada tabla que toca el escritor (más `pagos` y `asientos`), los índices (`pg_get_indexdef`, con los únicos marcados), las restricciones (`pg_get_constraintdef`: PK, UNIQUE, CHECK, FK), los triggers y las reglas. Es un programa aparte (`garro_indices.py`), no el escritor: sesión de solo lectura, solo catálogos de PostgreSQL 9.3 (nada de filas) y tope de 120 s. La salida empieza con un resumen de lo que impediría duplicados (para que sobreviva al recorte a 20.000 caracteres) y sigue con el detalle por tabla. Requiere la migración 695 (amplía la lista de tipos de `garro.comandos`); `select garro.comandos_pedir('indices', '{}');`.

## Estados

`pendiente -> tomado -> ok | error`; `pendiente -> vencido` (a las 2 h) o `cancelado`; `tomado -> sin_respuesta` (a los 60 min sin resolverse). Lo tomado no se reintenta nunca.

## Por qué no se pisan

- La tarea programada tiene `MultipleInstances IgnoreNew` y un límite de 30 minutos: mientras un comando largo (`copia_crear`) corre, las corridas de los 5 minutos siguientes no arrancan.
- Además, la corrida toma dos candados de archivo del sistema operativo (Windows los suelta solos si el proceso muere): `comandos.lock` y `cargas.lock` (el del escritor). Si alguno está tomado, no se toma ningún comando de la cola (sigue pendiente).
- **28 minutos para todos.** Cada comando se corta antes de los 28 minutos contados desde que arrancó la tarea (lo que queda de ellos acota su tope); al cortarse se mata también lo que lanzó (`taskkill /T`). `copia_crear` (25 minutos) no corre si lo que queda es menos que su tope (resuelve `error`, «pedila de nuevo»). `actualizar` recibe el plazo que queda como límite absoluto: sus pasos (descargas 90 s cada una, autochequeo 140 s, y lo mismo para la restauración) usan el mínimo entre su tope y lo que queda, y no empieza si queda menos que el peor caso (490 s). Si se corta `copia_crear` puede haber quedado una copia a medias: `copia_medir` la muestra y `copia_borrar` la saca; los `copia-*.dump` huérfanos se borran al empezar `copia_medir` y `copia_crear` y tras un corte.
- `actualizar` toma el candado de la copia en Supabase (la misma clave que usa la copia de datos) solo durante el respaldo y el reemplazo: ninguna copia arranca mientras tanto, y si hay una en curso no se actualiza. Se suelta antes del autochequeo.

## Actualización a medias y recuperación

1. Antes del primer reemplazo se hace el respaldo (`copy2`, conserva fechas) y se escribe la marca `actualizacion-en-curso`. Se borra recién cuando el autochequeo salió bien (o tras restaurar todo).
2. Si el proceso muere con la marca puesta, la próxima corrida de `garro_sync.py` (antes de importar nada más) llama a `garro_recuperar.recuperar`: toma `comandos.lock` (si lo tiene otro proceso, está actualizando y no se toca), restaura cada archivo y borra su bytecode, deja `actualizacion-recuperada.json` y una línea en el log, y relanza la corrida. La pasada de comandos siguiente cierra con `error` la fila que quedó `tomado`. Si la restauración no puede (por ejemplo falta el respaldo), la marca queda y se reintenta; si sigue, hay que instalar en el equipo.
3. Con una marca pendiente `actualizar` se niega: nunca pisa el respaldo.
5. Se relanza **una sola vez**: el hijo lleva `GARRO_SYNC_RELANZADO=1` y nunca relanza; si la marca no se puede borrar o vuelve a aparecer, es «incompleta». La recuperación corre antes que `argparse` y que cualquier import del paquete (un test lo comprueba).
6. Antes de restaurar se verifica el sha256 de cada archivo del respaldo contra la marca; si no coincide no se toca nada («incompleta»). El respaldo, los archivos nuevos y la marca se fuerzan a disco (`fsync`) antes de reemplazar.
7. Con una marca sin resolver la corrida solo hace el latido y un log claro (nada de copia, cargas ni relevamiento) y los comandos no toman ninguno nuevo: el que estaba tomado se resuelve con un error que lo explica. Hace falta instalar en el equipo.
8. Si al terminar bien no se pudo borrar la marca, se reescribe con `estado: ok`: la recuperación la borra sin restaurar nada.
4. Un `actualizar` a una versión menor que la instalada se rechaza (bajar de versión solo se hace en el equipo); la misma versión repara si hay diferencias.

## Seguridad

- Todo lo que llega se valida de nuevo en el servidor (`garro_comandos.validar`), aunque la cola ya lo haya validado: una fila forjada directamente en la tabla se rechaza sin correr nada (hay una prueba para eso en `probar_escritor.py`).
- Nunca se escribe en la base real: ver README. Toda escritura o borrado con `--base` exige además que la base tenga la **marca** de `garro_copia.py` (comentario de la base): lo comprueba `garro_escritor.conectar_garro` (vale también a mano) y `garro_comandos` como segunda barrera. No hay comandos libres ni shell.
- Los secretos de `.env.garro-sync` (claves cuyo nombre contiene PASS, PWD, SECRET, TOKEN, KEY, CLAVE, CONN, DSN, URI o URL, y cualquier valor con `://`) se reemplazan por `***` en todo lo que sube, antes de recortar, también en sus formas codificadas (%-encoding en mayúscula y minúscula, `+`, JSON, `repr()`) y la contraseña de una cadena de conexión tal cual y decodificada. **No se redactan valores de menos de 4 caracteres** (una clave de 1 o 2 letras volvería ilegible cualquier salida): las contraseñas tienen que tener al menos 4.
- `actualizar`: quien pide fija la huella del `install.ps1` del tag, calculada **desde el checkout local ya revisado** (`git -C diforte-garro-sync show vX.Y.Z:install.ps1 | shasum -a 256`), nunca bajándola de GitHub; ese archivo fija la de cada archivo del zip. El repo (`REPO` en `garro_actualizar.py`) tiene que coincidir con `$Repo` de `install.ps1` (lo comprueba un test).

## Después de instalar la v1.10.0 (una vez, en el servidor)

1. Aplicar la migración 693 en Supabase. Sin ella, la tarea ignora los pedidos remotos y deja una línea por hora en el log.
2. Correr el instalador (README, «Actualizar el programa»). Deja `tareas-version.txt`, que la actualización remota necesita para comparar.
3. Probar (como `postgres`): `select garro.comandos_pedir('version', '{}');` y mirar `garro.comandos` a los 5 minutos.

## Prueba completa de movimientos en la copia (sin tocar la base real)

Cada paso se pide, se espera el resultado (hasta 5 minutos) y recién ahí se pide el siguiente.

```sql
select garro.comandos_pedir('estructura', '{}');
select garro.comandos_pedir('perfil', '{}');
select garro.comandos_pedir('copia_medir', '{}');
select garro.comandos_pedir('copia_crear', '{"base":"gestion_prueba"}');
-- simulación contra la base real (solo lee), con la fecha de hoy:
select garro.comandos_pedir('escritor_archivo', jsonb_build_object('payload',
  jsonb_set('{"propuesta":{"clase":"movimiento_fondo","tipo_mov_id":50,"tipo_mov_nombre":"COMERCIO INDUSTRIA Y SERVICIO MUNICIPAL","descripcion":"COM E IND PER 09/2026","periodo":"2026-09","importe":123456.78,"medio":"efectivo","caja":1,"cuenta_bancaria_id":null,"fecha":"2026-11-24","vencimiento":"2026-10-20","organismo":"ORGANISMO DE EJEMPLO","requiere_revision":false,"posible_duplicado":null},"total_pesos":123456.78}'::jsonb, '{propuesta,fecha}', to_jsonb(to_char(current_date, 'YYYY-MM-DD')))));
-- escritura en la COPIA (la salida trae "carga_id": "copia-..." y "movimiento_id"):
select garro.comandos_pedir('escritor_archivo', jsonb_build_object('escribir', true, 'base', 'gestion_prueba', 'payload',
  jsonb_set('{"propuesta":{"clase":"movimiento_fondo","tipo_mov_id":50,"tipo_mov_nombre":"COMERCIO INDUSTRIA Y SERVICIO MUNICIPAL","descripcion":"COM E IND PER 09/2026","periodo":"2026-09","importe":123456.78,"medio":"efectivo","caja":1,"cuenta_bancaria_id":null,"fecha":"2026-11-24","vencimiento":"2026-10-20","organismo":"ORGANISMO DE EJEMPLO","requiere_revision":false,"posible_duplicado":null},"total_pesos":123456.78}'::jsonb, '{propuesta,fecha}', to_jsonb(to_char(current_date, 'YYYY-MM-DD')))));
select garro.comandos_pedir('escritor_archivo', jsonb_build_object('escribir', true, 'base', 'gestion_prueba', 'payload',
  jsonb_set('{"propuesta":{"clase":"movimiento_fondo","tipo_mov_id":58,"tipo_mov_nombre":"SINDICATO","descripcion":"FAECYS PER 09/2026","periodo":"2026-09","importe":45678.9,"medio":"transferencia","caja":1,"cuenta_bancaria_id":11,"fecha":"2026-11-24","vencimiento":"2026-10-20","organismo":"ORGANISMO DE EJEMPLO","requiere_revision":false,"posible_duplicado":null},"total_pesos":45678.9}'::jsonb, '{propuesta,fecha}', to_jsonb(to_char(current_date, 'YYYY-MM-DD')))));
-- comparar contra uno cargado por una persona (ids dentro de la copia):
select garro.comandos_pedir('comparar', '{"base":"gestion_prueba","humano":<id>,"bot":<id>}');
-- deshacer (primero sin confirmar, que solo muestra qué borraría) y borrar la copia:
select garro.comandos_pedir('deshacer', '{"base":"gestion_prueba","carga_id":"copia-<n>"}');
select garro.comandos_pedir('deshacer', '{"base":"gestion_prueba","carga_id":"copia-<n>","confirmar":true}');
select garro.comandos_pedir('copia_borrar', '{"base":"gestion_prueba"}');
```

Los dos payloads son los de `fixtures/` (datos de ejemplo). Para comparar con un movimiento real hay que usar uno que ya esté en la copia (la copia es una foto de la base real en el momento de crearla).

## Riesgos conocidos

- Una PC distinta a la esperada: si `args.equipo` no coincide con el nombre de la PC, nadie lo toma y vence a las 2 h (la fila no cambia).
- Un comando largo retiene la tarea de cada 5 minutos: durante `copia_crear` no se procesan pedidos de actualización ni la cola de cargas.
- Si la tarea se corta por el límite de 30 minutos en medio de un comando, queda `tomado` y a los 60 minutos `sin_respuesta`.
- Un corte justo entre escribir la marca y el primer reemplazo, o durante la restauración misma, se resuelve igual en la próxima corrida (restaurar es idempotente).
- `actualizar` no puede cambiar tareas, Python ni dependencias; esos cambios requieren el instalador en el equipo.
