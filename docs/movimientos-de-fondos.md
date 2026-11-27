# Movimientos de fondos: qué escribe el programa y qué escribe el bot

Receta de `garro_movimientos.py` (v1.9.0). Sale del relevamiento automático (`garro.relevamientos`, diferencias redactadas
de cada carga) y de la copia en Supabase, en solo lectura. Acá no hay datos de la empresa: solo la forma de las filas.

Alcance: egresos en pesos pagados en efectivo (caja 1) o por transferencia (cuenta bancaria 11), de cuatro tipos de
movimiento que **no generan asiento**. Todo lo demás (otros tipos, cheques, tarjetas, billeteras, dólares, ingresos) no se carga.

## Filas que escribe el programa al guardar un egreso

Una transacción, sin asiento (los tipos con `genera_asiento` falso no lo escriben; los verdaderos sí, y esa receta no está relevada).
No se tocó ninguna otra tabla en los movimientos relevados con estos medios (ni logs ni saldos): en la diferencia de cada carga
solo aparecen las tablas de abajo.

| Tabla | Filas | Columnas |
|---|---|---|
| `movimientos_fondos` | 1 | tipo; `fec_movimiento_fondo` = el momento en que se abrió la pantalla (en el bot, fecha del movimiento con la hora de la carga); `fk_id_operario` = quien cargó; `obs_movimiento_fondo` `''`; `fk_id_caja` 1; `es_ingreso` falso; `es_egreso` verdadero; `des_movimiento_fondo` texto libre (no va en mayúsculas); `es_ctacte` verdadero; `genera_ctacte` falso; `centro_costos` falso; `fec_carga_movimiento_fondo` = momento del guardado |
| `efectivo_medio_de_pago` (efectivo) | 1 | `imp_efectivo_medio_de_pago` = importe; `imp_equivalente_peso` 1; `fk_id_moneda` 1; `fecha` = `fec_movimiento_fondo` (en todos los relevados); `fk_id_caja` 1; `es_ctacte` verdadero |
| `efectivo_medio_de_pago_en_movimientos_fondos` | 1 | movimiento, efectivo, `fk_id_caja` 1 |
| `movimientos_cuentas_bancarias` (transferencia) | 1 | `imp_movimiento_cuenta_bancaria` = importe **positivo**; `fk_id_cuenta_bancaria` 11; `tipo_movimiento_cuenta_bancaria` `TRANSFERENCIA BANCARIA`; `fk_id_plan_de_cuenta` = el de la cuenta bancaria (siempre el mismo en los relevados); `fec_movimiento_cuenta_bancaria` = `fec_movimiento_fondo` (casi siempre; en el resto la persona cambió la fecha a mano); `nro_movimiento_cuenta_bancaria` `''`; `fec_visado_movimiento_cuenta_bancaria` NULL |
| `movimientos_cuenta_bancaria_en_movimientos_fondos` | 1 | movimiento, movimiento bancario, `fk_id_caja` 1 |

Un movimiento tiene un solo medio en los tipos habilitados. Las claves primarias salen de la secuencia de cada tabla.

## Lo que el bot hace distinto a propósito

- Usuario propio (`CARGAS_OPERARIO`) en `fk_id_operario`.
- La fecha del movimiento es la de la propuesta (hasta 60 días atrás, nunca futura) con la hora de la carga; el efectivo o el movimiento bancario llevan el mismo momento.
- La descripción va en mayúsculas, una sola línea y de hasta 100 caracteres.
- Rechaza si el tipo genera asiento, deja de ser egreso o cambia la cuenta contable de la cuenta bancaria.

## A relevar en el servidor (no confirmado)

- `obs_movimiento_cuenta_bancaria` del movimiento bancario: el relevamiento la redacta. El bot escribe `''`. `python comparar_movimientos.py --perfil` cuenta cómo está en los movimientos existentes sin mostrar texto.
- `nro_movimiento_cuenta_bancaria`: el relevamiento muestra solo los últimos 4 caracteres y salió vacío en todos; confirmarlo con `--perfil`.
- Que las claves primarias de las cinco tablas tengan valor por defecto (secuencia): `--perfil` lo lista.
- Que el programa no escriba nada más (logs, saldos) al guardar un egreso: el relevamiento de una carga aislada lo descarta, pero el candado no lo ve; se confirma comparando una carga del bot en la copia con una de una persona (`comparar_movimientos.py`).
- Un movimiento de fondos aparece una vez con la descripción vacía y se modifica en la ventana siguiente (un caso): si el programa guarda en dos pasos o la persona lo editó, no está claro. El bot hace un solo INSERT.
- Un par de movimientos de otro tipo tienen `genera_ctacte` verdadero; no está claro qué los distingue y el bot siempre escribe falso (es lo de todos los demás).
- El programa de escritorio abre siempre la base real: lo que se escribe en una copia solo se puede mirar con `comparar_movimientos.py`.

## Duplicados

Antes de escribir, dentro de la transacción y con `movimientos_fondos` bloqueada contra altas simultáneas. Es la misma regla que `compras_asistente_movimientos_parecidos` (migración 666), con los mismos casos en `fixtures/casos_duplicados.json` (cada caso trae su «hoy»; el test del repo los corre y la función de Supabase se corre con ese «hoy» fijado).

- **Ya cargado:** mismo tipo con el período escrito en la descripción: «10/2026», «10-26», «10.2026», «2026/10», «OCTUBRE 2026», «OCT/26». «PER 10», «periodo. 10» y el mes en letras sin año solo cuentan si el movimiento está entre el primer día del período y 4 meses después (si no, es de otro año). Una fecha completa («05/10/2026», «2026/10/05») no es un período. En los tipos 58 y 13 hay varios conceptos por período: además tiene que estar la misma sigla (lo que va antes de «PER», o del período numérico) como palabra entera.
- **Posible duplicado:** el mismo período con otra sigla (tipos 58 y 13); o, con un importe de $1000 o más, un importe a menos de $1 de otro movimiento (suma de sus medios: efectivo, banco y otro medio) del mismo tipo a ±40 días de hoy o entre el primer día del período y 4 meses después, o de un egreso de cualquier otro tipo a ±10 días de hoy.

En ambos casos el movimiento queda para cargar a mano. Una retomada solo toma algo por propio si el diario local tiene un `intento` de esa carga en esa base y coinciden usuario del bot, tipo, descripción exacta, fecha e importe en centavos; un `intento` sin resultado en el que no se encuentra nada queda indeterminado y no se vuelve a planificar.
