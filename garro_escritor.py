"""Carga de comprobantes de compra y de movimientos de fondos en Gestión Integral: escritor.

Escribe en la base del programa lo que planificaron `garro_cargas.py` (compras) y `garro_movimientos.py` (egresos
de fondos en efectivo o por transferencia), igual que lo hace el programa cuando una persona los carga a mano. Es la única parte de garro-sync que escribe en Garro, así que está encerrada:

  - APAGADA por defecto: hace falta `CARGAS=1` en el .env, y para escribir de verdad también `CARGAS_REAL=1`
    (además del interruptor del lado de Supabase). Sin las dos llaves, todo se procesa en seco.
  - Todo o nada: cada comprobante va en UNA transacción con las tablas bloqueadas; si algo no cierra, ROLLBACK.
  - Antes de escribir se verifica que la estructura de las tablas sea exactamente la de la receta y que no haya
    triggers ni reglas. Lo que no está confirmado, no se escribe.
  - La base real solo se escribe por la cola de Supabase (`--pasada`); a mano solo contra una copia de la base.

Uso:
    python garro_escritor.py --pasada                         # lo que hace la tarea de cada 5 minutos
    python garro_escritor.py --estructura                     # prueba la instalación (solo lectura)
    python garro_escritor.py --archivo carga.json             # planifica una carga (compra o movimiento) en solo lectura
    python garro_escritor.py --archivo carga.json --escribir --base OTRA   # la escribe en una COPIA de la base
    python garro_escritor.py --deshacer N [--confirmar]       # borra lo que escribió la carga N
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime
from decimal import Decimal

import garro_cargas as gc
import garro_movimientos as gm
import garro_sync as g

AQUI = os.path.dirname(os.path.abspath(__file__))
DIARIO = os.path.join(AQUI, "cargas-diario.jsonl")
ARCHIVO_LOCK = os.path.join(AQUI, "cargas.lock")

OPERARIO_POR_DEFECTO = "botcompras"
POR_PASADA_DEFECTO = 1
POR_PASADA_MAXIMO = 5
TOLERANCIA_TOTAL = 0.01
TOLERANCIA_ASIENTO = 0.005
LOCK_TABLAS = ("LOCK TABLE comprobantes_compras, asientos_contables, pagos_en_comprobantes_compras "
               "IN SHARE ROW EXCLUSIVE MODE")
# Un movimiento de fondos solo necesita que nadie dé de alta otro mientras se buscan duplicados. Las tablas de sus
# medios (efectivo y movimientos bancarios) también las escriben los recibos y los pagos del programa: no se bloquean,
# para no frenarlos; la carga las relee y las verifica antes de confirmar.
LOCK_TABLAS_MOVIMIENTO = "LOCK TABLE movimientos_fondos IN SHARE ROW EXCLUSIVE MODE"
CLASE_COMPRA, CLASE_MOVIMIENTO = "compra", gm.CLASE
CLASES_DE_LA_COLA = (CLASE_COMPRA, CLASE_MOVIMIENTO)
COLUMNAS_TODAS = {**gc.COLUMNAS, **gm.COLUMNAS}

# Orden de las tablas hijas en `escrito` (y, al revés, el de borrado).
TABLAS_ESCRITO = (
    "comprobantes_compras", "items_comprobantes_compras", "conceptos_compras",
    "conceptos_x_items_comprobantes_compras", "regimenes_especiales_x_comprobantes_compras",
    "asientos_contables", "asientos_contables_movimientos", "pagos_en_comprobantes_compras",
)


class ErrorEstructura(Exception):
    """La estructura de la base del programa no es la de la receta: no se escribe nada."""


class ErrorPlan(Exception):
    """Una fila del plan no trae exactamente las columnas esperadas."""


class ColaSinFunciones(Exception):
    """Supabase todavía no tiene las funciones de la cola de cargas."""


class ErrorCola(Exception):
    """La cola de Supabase rechazó una transición (la función devolvió false)."""


class ErrorDiario(Exception):
    """No se pudo anotar en el diario local: sin diario no se escribe en Garro."""


class ErrorDeshacer(Exception):
    def __init__(self, codigo: str, mensaje: str):
        super().__init__(mensaje)
        self.codigo = codigo
        self.mensaje = mensaje


def _ahora_txt() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _verdadero(valor) -> bool:
    """Solo un booleano verdadero cuenta: cualquier otra cosa (texto, número, NULL) se trata como falso."""
    return valor is True


def _corto(e: BaseException) -> str:
    primera = (str(e).splitlines() or [""])[0]
    return f"{type(e).__name__}: {primera}"[:240]


# ------------------------------------------------------------------ conexión

# Comentario que garro_copia.py le deja a cada copia de prueba que crea: es lo que la distingue de cualquier otra base.
MARCA_COPIA = "Copia de prueba de garro-sync"


def exigir_copia_marcada(cfg: dict[str, str], base: str) -> None:
    """Toda ESCRITURA con --base exige que esa base sea una copia hecha por garro_copia.py (lleva su marca como
    comentario de la base), no solo que el nombre lo parezca. Se mira desde la base configurada, solo lectura."""
    if str(base).strip().lower() == str(cfg.get("GARRO_BASE", "")).strip().lower():
        raise g.ErrorSync("Esa es la base real: solo se escribe en una copia de prueba.")
    con = conectar_garro(cfg, solo_lectura=True)
    try:
        with con.cursor() as cur:
            cur.execute("SELECT shobj_description(d.oid, 'pg_database') FROM pg_database AS d WHERE d.datname = %s", (base,))
            fila = cur.fetchone()
        con.rollback()
    finally:
        con.close()
    if fila is None or not str(fila[0] or "").startswith(MARCA_COPIA):
        raise g.ErrorSync(f"La base {base} no es una copia de prueba de este programa (no tiene su marca): no se escribe nada. "
                          "Las copias se crean con garro_copia.py --crear.")


def conectar_garro(cfg: dict[str, str], solo_lectura: bool, base: str | None = None):
    """Conexión a la base del programa. Mismas credenciales y codificación que la copia; `base` pisa GARRO_BASE."""
    import psycopg2
    g.validar_origen(cfg)
    if base is not None and not solo_lectura:
        exigir_copia_marcada(cfg, base)
    try:
        con = psycopg2.connect(
            host=cfg["GARRO_HOST"], port=int(cfg["GARRO_PUERTO"]), dbname=base or cfg["GARRO_BASE"],
            user=cfg["GARRO_USUARIO"], password=cfg["GARRO_PASSWORD"],
            connect_timeout=20, application_name="garro_sync (cargas)",
            # Como opciones de la sesión y no con SET: un ROLLBACK deshace un SET, y estos topes tienen que
            # valer en todas las transacciones de la conexión.
            # Los keepalive: si la conexión se corta sin cierre limpio, el servidor suelta la sesión (y sus
            # bloqueos) en segundos en vez de dejarla colgada frenando las grabaciones del programa.
            options="-c lock_timeout=5s -c statement_timeout=60s -c tcp_keepalives_idle=30 -c tcp_keepalives_interval=10",
        )
    except UnicodeDecodeError as e:
        raise g.ErrorSync(g.mensaje_rechazo_origen(g.decodificar_error_conexion(e))) from e
    con.set_session(readonly=solo_lectura, autocommit=False)
    with con.cursor() as cur:
        cur.execute("SHOW server_encoding")
        server_encoding = cur.fetchone()[0]
    con.rollback()
    encoding = cfg.get("GARRO_ENCODING") or ("WIN1252" if server_encoding == "SQL_ASCII" else server_encoding)
    con.set_client_encoding(encoding)
    return con


# ------------------------------------------------------------------ estructura

def verificar_estructura(cur, columnas: dict | None = None, tipos: dict | None = None) -> dict[str, int | None]:
    """Las tablas que se tocan tienen EXACTAMENTE las columnas (y tipos) de la receta, sin triggers ni reglas.
    Cada receta (compras, movimientos de fondos) verifica solo sus tablas: un cambio en una no frena la otra.
    Devuelve los largos máximos de las columnas de texto: {"tabla.columna": largo o None}."""
    columnas = gc.COLUMNAS if columnas is None else columnas
    tipos = gc.TIPOS if tipos is None else tipos
    tablas = list(columnas)
    cur.execute("""SELECT table_name, column_name, character_maximum_length, data_type FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = ANY(%s)""", (tablas,))
    reales: dict[str, set[str]] = {t: set() for t in tablas}
    largos: dict[str, int | None] = {}
    problemas = []
    for tabla, columna, largo, tipo in cur.fetchall():
        reales[tabla].add(columna)
        largos[f"{tabla}.{columna}"] = largo
        esperado = tipos.get(tabla, {}).get(columna)
        if esperado is not None and tipo != esperado:
            problemas.append(f"{tabla}.{columna}: es {tipo} y se esperaba {esperado}")
    for tabla, esperadas in columnas.items():
        if not reales[tabla]:
            problemas.append(f"{tabla}: la tabla no existe")
            continue
        faltan, sobran = sorted(set(esperadas) - reales[tabla]), sorted(reales[tabla] - set(esperadas))
        if faltan:
            problemas.append(f"{tabla}: faltan columnas {', '.join(faltan)}")
        if sobran:
            problemas.append(f"{tabla}: columnas de más {', '.join(sobran)}")
    cur.execute("""SELECT c.relname, t.tgname FROM pg_trigger t
                   JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE NOT t.tgisinternal AND n.nspname = 'public' AND c.relname = ANY(%s)""", (tablas,))
    problemas += [f"{t}: tiene un trigger ({n})" for t, n in cur.fetchall()]
    cur.execute("SELECT tablename, rulename FROM pg_rules WHERE schemaname = 'public' AND tablename = ANY(%s)", (tablas,))
    problemas += [f"{t}: tiene una regla ({n})" for t, n in cur.fetchall()]
    if problemas:
        raise ErrorEstructura("La base del programa no coincide con la receta: " + "; ".join(problemas))
    return largos


# ------------------------------------------------------------------ referencias

_MEDIOS_DE_PAGO = """
    SELECT cp.fk_id_pago AS pago, ch.imp_cheque::numeric AS imp
      FROM cheques_en_pagos AS cp JOIN cheques AS ch ON ch.id_cheque = cp.fk_id_cheque
    UNION ALL
    SELECT ep.fk_id_pago, e.imp_efectivo_medio_de_pago::numeric * coalesce(nullif(e.imp_equivalente_peso, 0), 1)::numeric
      FROM efectivo_medio_de_pago_en_pagos AS ep
      JOIN efectivo_medio_de_pago AS e ON e.id_efectivo_medio_de_pago = ep.fk_id_efectivo_medio_de_pago
    UNION ALL
    SELECT mp.fk_id_pago, mb.imp_movimiento_cuenta_bancaria::numeric
      FROM movimientos_cuenta_bancaria_en_pagos AS mp
      JOIN movimientos_cuentas_bancarias AS mb ON mb.id_movimiento_cuenta_bancaria = mp.fk_id_movimiento_cuenta_bancaria
    UNION ALL
    SELECT op.fk_id_pago, o.imp_otro_medio_de_pago::numeric
      FROM otro_medio_de_pago_en_pagos AS op
      JOIN otro_medio_de_pago AS o ON o.id_otro_medio_de_pago = op.fk_id_otro_medio_de_pago
    UNION ALL
    SELECT cpp.fk_id_pago, chp.imp_cheque_propio::numeric
      FROM cheques_propios_en_pagos AS cpp
      JOIN cheques_propios AS chp ON chp.id_cheque_propio = cpp.fk_id_cheque_propio
"""

_PAGOS_DEL_PROVEEDOR = f"""
    WITH medios AS (
      SELECT m.pago, sum(m.imp) AS imp FROM ({_MEDIOS_DE_PAGO}) AS m GROUP BY 1
    ), imputado AS (
      SELECT x.fk_id_pago AS pago, sum(x.imp_pago_en_comprobante_compra)::numeric AS imp
      FROM pagos_en_comprobantes_compras AS x GROUP BY 1
    )
    SELECT p.id_pago, p.fec_pago, round(coalesce(medios.imp, 0) - coalesce(imputado.imp, 0), 2)
    FROM pagos AS p
    LEFT JOIN medios ON medios.pago = p.id_pago
    LEFT JOIN imputado ON imputado.pago = p.id_pago
    WHERE p.fk_id_proveedor = %s
    ORDER BY p.id_pago
"""


def _fecha_de(propuesta: dict) -> date | None:
    try:
        return date.fromisoformat(str(propuesta.get("fecha")))
    except ValueError:
        return None


def leer_referencias(cur, propuesta, ahora: datetime, cfg: dict[str, str], largos: dict[str, int | None]) -> gc.Referencias:
    """Lo que el planificador necesita saber de la base, leído dentro de la transacción de la carga.
    (`ahora` no se usa hoy: queda en la firma porque las referencias se leen «a esa hora».)"""
    p = propuesta if isinstance(propuesta, dict) else {}

    cur.execute("SELECT id_proveedor, cuit_proveedor, raz_soc_proveedor, nom_fan_proveedor, es_activo FROM proveedores")
    todos = [{"id": r[0], "cuit": r[1], "razon_social": r[2], "fantasia": r[3], "activo": _verdadero(r[4])}
             for r in cur.fetchall()]
    proveedores = gc.proveedores_de(p, todos)

    tipo = None
    tipo_id = gc._entero(p.get("tipo_garro_id"))
    if tipo_id is not None:
        cur.execute("""SELECT id_tipo_comprobante_compra, des_tipo_comprobante_compra, es_ctacte
                       FROM tipos_comprobantes_compras WHERE id_tipo_comprobante_compra = %s""", (tipo_id,))
        fila = cur.fetchone()
        if fila:
            tipo = {"id": fila[0], "descripcion": fila[1], "es_ctacte": _verdadero(fila[2])}

    cur.execute("SELECT id_operario FROM operarios WHERE nombre_usuario = %s AND habilitado",
                (cfg.get("CARGAS_OPERARIO", OPERARIO_POR_DEFECTO),))
    filas = cur.fetchall()
    operario_id = filas[0][0] if len(filas) == 1 else None

    cur.execute("SELECT fk_id_plan_de_cuenta_proveedor, fk_id_plan_de_cuenta_creditofiscal FROM planes_de_cuentas_config")
    filas = cur.fetchall()
    cuenta_proveedores, cuenta_credito = (filas[0][0], filas[0][1]) if len(filas) == 1 else (None, None)

    cur.execute("SELECT id_plan_de_cuenta, es_activo, imputable FROM planes_de_cuentas")
    cuentas = {r[0]: {"activa": _verdadero(r[1]), "imputable": _verdadero(r[2])} for r in cur.fetchall()}

    cur.execute("SELECT id_cuenta_imputacion_item_compra FROM cuentas_imputacion_items_compras")
    categorias = {r[0] for r in cur.fetchall()}

    cur.execute("SELECT id_regimen_especial, fk_id_tipo_regimen_compra, fk_id_plan_de_cuenta FROM regimenes_especiales")
    regimenes = {r[0]: {"tipo": r[1], "cuenta": r[2]} for r in cur.fetchall()}

    duplicados: list[int] = []
    numero = gc._entero(p.get("numero"))
    unico = proveedores[0] if len(proveedores) == 1 else None
    if unico is not None and numero is not None:
        # De cualquier tipo y punto de venta: es a propósito conservador (el equipo a veces carga con otro punto).
        cur.execute("""SELECT id_comprobante_compra FROM comprobantes_compras
                       WHERE estado_comprobante_compra = 'activo' AND fk_id_proveedor = %s
                         AND nro_comprobante_compra = %s ORDER BY 1""", (unico["id"], numero))
        duplicados = [r[0] for r in cur.fetchall()]

    siguiente_grupo = None
    fecha = _fecha_de(p)
    if fecha is not None:
        desde = datetime(fecha.year, fecha.month, 1)
        hasta = datetime(fecha.year + (fecha.month == 12), fecha.month % 12 + 1, 1)
        cur.execute("""SELECT coalesce(max(nro_grupo_asiento_contable), 0) + 1 FROM asientos_contables
                       WHERE fec_asiento_contable >= %s AND fec_asiento_contable < %s""", (desde, hasta))
        siguiente_grupo = cur.fetchone()[0]

    pagos_a_cuenta: list[dict] = []
    if tipo is not None and tipo["es_ctacte"] and unico is not None:
        medios_sin_contemplar = g.contar_sin_copiar(cur)
        if medios_sin_contemplar:
            raise gc.NoCargable(
                "medios_sin_contemplar",
                "En el programa se usan medios de pago que la carga no contempla ("
                + ", ".join(sorted(medios_sin_contemplar)) + "): los restos de los pagos saldrían mal.")
        cur.execute("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname = 'notas_creditos_compras_en_pagos'")
        if cur.fetchone() is None:
            raise ErrorEstructura("La base del programa no coincide con la receta: falta la tabla notas_creditos_compras_en_pagos.")
        cur.execute("""SELECT count(*) FROM notas_creditos_compras_en_pagos AS n
                       JOIN pagos AS p ON p.id_pago = n.fk_id_pago WHERE p.fk_id_proveedor = %s""", (unico["id"],))
        if cur.fetchone()[0]:
            raise gc.NoCargable("medios_sin_contemplar",
                                "El proveedor tiene pagos con notas de crédito aplicadas, que la carga no contempla: "
                                "los restos de los pagos saldrían mal.")
        cur.execute(_PAGOS_DEL_PROVEEDOR, (unico["id"],))
        for pago, fec_pago, resto in cur.fetchall():
            resto = gc.r2(float(resto))
            if resto > 0:
                pagos_a_cuenta.append({"id": pago, "fecha": fec_pago, "resto": resto})

    return gc.Referencias(
        proveedores=proveedores, tipo=tipo, operario_id=operario_id, cuenta_proveedores=cuenta_proveedores,
        cuenta_credito_fiscal=cuenta_credito, cuentas=cuentas, categorias=categorias, regimenes=regimenes,
        duplicados=duplicados, siguiente_grupo=siguiente_grupo, pagos_a_cuenta=pagos_a_cuenta, largos=dict(largos))


# ------------------------------------------------------------------ escritura

def _insertar(cur, tabla: str, fila: dict, **claves) -> int:
    """INSERT con lista explícita de columnas. La fila del plan más las claves foráneas tienen que dar
    EXACTAMENTE las columnas de la receta (menos la clave primaria, que sale de la secuencia)."""
    repetidas = set(fila) & set(claves)
    if repetidas:
        raise ErrorPlan(f"{tabla}: el plan ya trae {', '.join(sorted(repetidas))}")
    completa = {**fila, **claves}
    pk, *columnas = COLUMNAS_TODAS[tabla]
    if set(completa) != set(columnas):
        faltan, sobran = sorted(set(columnas) - set(completa)), sorted(set(completa) - set(columnas))
        raise ErrorPlan(f"{tabla}: el plan no coincide con la receta (faltan {faltan}, de más {sobran})")
    cur.execute(f'INSERT INTO public."{tabla}" ({", ".join(columnas)}) VALUES ({", ".join(["%s"] * len(columnas))}) '
                f"RETURNING {pk}", [completa[c] for c in columnas])
    return cur.fetchone()[0]


def escribir(cur, plan: gc.Plan) -> dict:
    """Escribe el plan completo (sin commit) y devuelve los ids creados por tabla."""
    escrito: dict[str, list[int]] = {t: [] for t in TABLAS_ESCRITO}
    comprobante = _insertar(cur, "comprobantes_compras", plan.comprobante)
    escrito["comprobantes_compras"].append(comprobante)
    for item in plan.items:
        id_item = _insertar(cur, "items_comprobantes_compras", item["fila"], fk_id_comprobante_compra=comprobante)
        id_concepto = _insertar(cur, "conceptos_compras", {"des_concepto_compra": item["concepto"]})
        id_cxi = _insertar(cur, "conceptos_x_items_comprobantes_compras", {},
                           fk_id_item_comprobante_compra=id_item, fk_id_concepto_compra=id_concepto)
        escrito["items_comprobantes_compras"].append(id_item)
        escrito["conceptos_compras"].append(id_concepto)
        escrito["conceptos_x_items_comprobantes_compras"].append(id_cxi)
    for regimen in plan.regimenes:
        escrito["regimenes_especiales_x_comprobantes_compras"].append(
            _insertar(cur, "regimenes_especiales_x_comprobantes_compras", regimen, fk_id_comprobante_compra=comprobante))
    if plan.asiento:
        asiento = _insertar(cur, "asientos_contables", plan.asiento["fila"], fk_id_comprobante_compra=comprobante)
        escrito["asientos_contables"].append(asiento)
        for mov in plan.asiento["movimientos"]:
            escrito["asientos_contables_movimientos"].append(
                _insertar(cur, "asientos_contables_movimientos", mov, fk_id_asiento_contable=asiento))
    for imputacion in plan.imputaciones:
        escrito["pagos_en_comprobantes_compras"].append(
            _insertar(cur, "pagos_en_comprobantes_compras", imputacion, fk_id_comprobante_compra=comprobante))
    return escrito


def _ids_hijos(cur, comprobante_id: int) -> dict[str, list[int]]:
    """Lo que HOY cuelga del comprobante, con las mismas claves que `escrito` (listas ordenadas)."""
    def ids(sql: str, *params) -> list[int]:
        cur.execute(sql, params)
        return sorted(r[0] for r in cur.fetchall())

    items = ids("SELECT id_item_comprobante_compra FROM items_comprobantes_compras WHERE fk_id_comprobante_compra = %s",
                comprobante_id)
    cur.execute("""SELECT id_concepto_x_item_comprobante_compra, fk_id_concepto_compra
                   FROM conceptos_x_items_comprobantes_compras WHERE fk_id_item_comprobante_compra = ANY(%s)""", (items,))
    cxi = cur.fetchall()
    asientos = ids("SELECT id_asiento_contable FROM asientos_contables WHERE fk_id_comprobante_compra = %s", comprobante_id)
    return {
        "comprobantes_compras": ids("SELECT id_comprobante_compra FROM comprobantes_compras "
                                    "WHERE id_comprobante_compra = %s", comprobante_id),
        "items_comprobantes_compras": items,
        "conceptos_compras": sorted(r[1] for r in cxi),
        "conceptos_x_items_comprobantes_compras": sorted(r[0] for r in cxi),
        "regimenes_especiales_x_comprobantes_compras": ids(
            "SELECT id_regimen_especial_x_comprobante_compra FROM regimenes_especiales_x_comprobantes_compras "
            "WHERE fk_id_comprobante_compra = %s", comprobante_id),
        "asientos_contables": asientos,
        "asientos_contables_movimientos": ids(
            "SELECT id_asiento_contable_movimiento FROM asientos_contables_movimientos "
            "WHERE fk_id_asiento_contable = ANY(%s)", asientos),
        "pagos_en_comprobantes_compras": ids(
            "SELECT id_pago_en_comprobante_compra FROM pagos_en_comprobantes_compras WHERE fk_id_comprobante_compra = %s",
            comprobante_id),
    }


def _centavos(x) -> int:
    """Importe en centavos enteros (nunca se compara un float contra 0,01)."""
    return int(round(float(x) * 100))


def comparar_asiento(plan: gc.Plan, leidos: list[tuple]) -> None:
    """`leidos`: (cuenta, es_haber, importe) de los movimientos releídos, en orden por id. Tienen que ser
    EXACTAMENTE los del plan (cuenta, lado e importe en centavos, en el mismo orden) y el asiento tiene que cerrar."""
    esperados = [(m["fk_id_plan_de_cuenta"], bool(m["es_haber"]), _centavos(m["imp_asiento_contable_movimiento"]))
                 for m in plan.asiento["movimientos"]]
    reales = [(cuenta, haber is True, _centavos(importe)) for cuenta, haber, importe in leidos]
    if reales != esperados:
        raise AssertionError("Los movimientos del asiento releídos no coinciden con los del plan.")
    haber = sum(c for _, es_haber, c in reales if es_haber)
    debe = sum(c for _, es_haber, c in reales if not es_haber)
    if debe != haber:
        raise AssertionError(f"El asiento no cierra: debe {debe / 100:.2f}, haber {haber / 100:.2f}.")
    if haber != esperados[0][2]:
        raise AssertionError("El haber del asiento no es el del plan.")


def verificar_escrito(cur, plan: gc.Plan, escrito: dict) -> None:
    """Relee lo escrito (misma transacción, antes del COMMIT) y comprueba que cierra. Si no: excepción → ROLLBACK."""
    cid = escrito["comprobantes_compras"][0]
    hijos = _ids_hijos(cur, cid)
    for tabla in TABLAS_ESCRITO:
        if hijos[tabla] != sorted(escrito[tabla]):
            raise AssertionError(f"Lo escrito en {tabla} no coincide con lo que quedó en la base.")
    if len(hijos["items_comprobantes_compras"]) != len(plan.items):
        raise AssertionError("La cantidad de renglones escritos no coincide con el plan.")

    cur.execute("""SELECT coalesce(sum(i.can_item_comprobante_compra::numeric * i.imp_neto_item_comprobante_compra::numeric
                                * (1 + i.porc_desc_item_comprobante_compra::numeric / 100)
                                * (1 + c.porc_desc_comprobante_compra::numeric / 100)
                                * (1 + i.porc_iva_item_comprobante_compra::numeric / 100)), 0)
                   FROM comprobantes_compras AS c
                   JOIN items_comprobantes_compras AS i ON i.fk_id_comprobante_compra = c.id_comprobante_compra
                   WHERE c.id_comprobante_compra = %s""", (cid,))
    lineas = float(cur.fetchone()[0])
    cur.execute("""SELECT coalesce(sum(imp_regimen_especial_x_comprobante_compra::numeric), 0)
                   FROM regimenes_especiales_x_comprobantes_compras WHERE fk_id_comprobante_compra = %s""", (cid,))
    total = _centavos(lineas + float(cur.fetchone()[0]))
    if abs(total - _centavos(plan.total)) > 1:
        raise AssertionError(f"El total releído ({total / 100:.2f}) no coincide con el plan ({plan.total:.2f}).")

    if plan.asiento:
        cur.execute("""SELECT fk_id_plan_de_cuenta, es_haber, imp_asiento_contable_movimiento
                       FROM asientos_contables_movimientos WHERE fk_id_asiento_contable = ANY(%s)
                       ORDER BY id_asiento_contable_movimiento""", (hijos["asientos_contables"],))
        comparar_asiento(plan, cur.fetchall())
    elif hijos["asientos_contables"] or hijos["asientos_contables_movimientos"]:
        raise AssertionError("Se escribió un asiento que el plan no tenía.")

    cur.execute("""SELECT coalesce(sum(imp_pago_en_comprobante_compra::numeric), 0)
                   FROM pagos_en_comprobantes_compras WHERE fk_id_comprobante_compra = %s""", (cid,))
    if _centavos(cur.fetchone()[0]) > total + 1:
        raise AssertionError("Las imputaciones de pagos superan el total del comprobante.")


def huella(cur, escrito: dict, tablas: tuple[str, ...] = TABLAS_ESCRITO) -> str:
    """sha256 del contenido de TODAS las filas escritas (columnas de la receta, orden estable por id). Si una
    persona toca cualquiera en el programa, la huella cambia."""
    partes = []
    for tabla in tablas:
        pk, *_ = COLUMNAS_TODAS[tabla]
        cur.execute(f'SELECT {", ".join(COLUMNAS_TODAS[tabla])} FROM public."{tabla}" WHERE {pk} = ANY(%s) ORDER BY {pk}',
                    (sorted(escrito[tabla]),))
        partes.append([tabla, [[repr(v) for v in fila] for fila in cur.fetchall()]])
    return hashlib.sha256(json.dumps(partes, ensure_ascii=True).encode("ascii")).hexdigest()


# ------------------------------------------------------------------ movimientos de fondos

# Importe de un movimiento de fondos: la suma de sus tres medios (efectivo por su equivalente en pesos, banco en
# valor absoluto y otro medio), como la migración 666; NULL si no tiene ninguno. `m` es la tabla de movimientos.
_MEDIOS_DEL_MOVIMIENTO = """
      (SELECT count(*) FROM efectivo_medio_de_pago_en_movimientos_fondos v WHERE v.fk_id_movimiento_fondo = m.id_movimiento_fondo)
    + (SELECT count(*) FROM movimientos_cuenta_bancaria_en_movimientos_fondos v WHERE v.fk_id_movimiento_fondo = m.id_movimiento_fondo)
    + (SELECT count(*) FROM otro_medio_de_pago_en_movimientos_fondos v WHERE v.fk_id_movimiento_fondo = m.id_movimiento_fondo)"""
_IMPORTE_DEL_MOVIMIENTO = """
      coalesce((SELECT sum(e.imp_efectivo_medio_de_pago::numeric * coalesce(e.imp_equivalente_peso, 1)::numeric)
                FROM efectivo_medio_de_pago_en_movimientos_fondos AS v
                JOIN efectivo_medio_de_pago AS e ON e.id_efectivo_medio_de_pago = v.fk_id_efectivo_medio_de_pago
                WHERE v.fk_id_movimiento_fondo = m.id_movimiento_fondo), 0)
    + coalesce((SELECT sum(abs(b.imp_movimiento_cuenta_bancaria::numeric))
                FROM movimientos_cuenta_bancaria_en_movimientos_fondos AS v
                JOIN movimientos_cuentas_bancarias AS b ON b.id_movimiento_cuenta_bancaria = v.fk_id_movimiento_cuenta_bancaria
                WHERE v.fk_id_movimiento_fondo = m.id_movimiento_fondo), 0)
    + coalesce((SELECT sum(coalesce(o.imp_otro_medio_de_pago, 0)::numeric)
                FROM otro_medio_de_pago_en_movimientos_fondos AS v
                JOIN otro_medio_de_pago AS o ON o.id_otro_medio_de_pago = v.fk_id_otro_medio_de_pago
                WHERE v.fk_id_movimiento_fondo = m.id_movimiento_fondo), 0)"""
# Los del mismo tipo (todo el historial) y los egresos de cualquier tipo a ±10 días de hoy.
_MOVIMIENTOS_PARECIDOS = f"""
    SELECT m.id_movimiento_fondo, m.fk_id_movimiento_fondo_tipo, m.des_movimiento_fondo, m.fec_movimiento_fondo, m.es_egreso,
           CASE WHEN ({_MEDIOS_DEL_MOVIMIENTO}) = 0 THEN NULL ELSE round(({_IMPORTE_DEL_MOVIMIENTO}), 2) END
    FROM movimientos_fondos AS m
    WHERE m.fec_movimiento_fondo IS NOT NULL
      AND (m.fk_id_movimiento_fondo_tipo = %s OR (m.es_egreso AND abs(m.fec_movimiento_fondo::date - %s::date) <= {gm.DIAS_OTRO_TIPO}))
    ORDER BY m.id_movimiento_fondo
"""


def leer_referencias_movimiento(cur, propuesta, ahora: datetime, cfg: dict[str, str], largos: dict[str, int | None]) -> gm.Referencias:
    """Lo que el planificador de movimientos necesita saber de la base, leído dentro de la transacción de la carga."""
    p = propuesta if isinstance(propuesta, dict) else {}
    tipo = None
    existentes: list[dict] = []
    tipo_id = gc._entero(p.get("tipo_mov_id"))
    if tipo_id is not None:
        cur.execute("""SELECT id_movimiento_fondo_tipo, des_movimiento_fondo_tipo, es_ingreso, es_egreso, genera_asiento
                       FROM movimientos_fondos_tipos WHERE id_movimiento_fondo_tipo = %s""", (tipo_id,))
        fila = cur.fetchone()
        if fila:
            tipo = {"id": fila[0], "descripcion": fila[1], "es_ingreso": fila[2], "es_egreso": fila[3],
                    "genera_asiento": fila[4]}
        if tipo_id in gm.TIPOS_HABILITADOS:
            cur.execute(_MOVIMIENTOS_PARECIDOS, (tipo_id, ahora.date()))
            existentes = [{"id": r[0], "tipo": r[1], "descripcion": r[2], "fecha": r[3], "egreso": r[4],
                           "importe": None if r[5] is None else float(r[5])} for r in cur.fetchall()]

    cur.execute("SELECT id_operario FROM operarios WHERE nombre_usuario = %s AND habilitado",
                (cfg.get("CARGAS_OPERARIO", OPERARIO_POR_DEFECTO),))
    filas = cur.fetchall()
    operario_id = filas[0][0] if len(filas) == 1 else None

    cur.execute("SELECT id_caja, habilitada FROM cajas WHERE id_caja = %s", (gm.CAJA,))
    fila = cur.fetchone()
    caja = {"id": fila[0], "habilitada": fila[1]} if fila else None

    cuenta = None
    cuenta_id = gc._entero(p.get("cuenta_bancaria_id"))
    if cuenta_id is not None:
        cur.execute("""SELECT id_cuenta_bancaria, es_activa, fk_id_plan_de_cuenta FROM cuentas_bancarias
                       WHERE id_cuenta_bancaria = %s""", (cuenta_id,))
        fila = cur.fetchone()
        if fila:
            cuenta = {"id": fila[0], "activa": fila[1], "plan": fila[2]}
    return gm.Referencias(tipo=tipo, operario_id=operario_id, caja=caja, cuenta_bancaria=cuenta,
                          existentes=existentes, largos=dict(largos))


def escribir_movimiento(cur, plan: gm.Plan) -> dict:
    """Escribe el plan de un movimiento (sin commit) y devuelve los ids creados por tabla."""
    escrito: dict = {t: [] for t in gm.TABLAS_ESCRITO}
    movimiento = _insertar(cur, "movimientos_fondos", plan.movimiento)
    escrito["movimientos_fondos"].append(movimiento)
    tabla_medio, tabla_vinculo = plan.tablas_medio
    medio = _insertar(cur, tabla_medio, plan.medio_fila)
    clave_medio = ("fk_id_efectivo_medio_de_pago" if plan.medio == "efectivo" else "fk_id_movimiento_cuenta_bancaria")
    vinculo = _insertar(cur, tabla_vinculo, plan.vinculo, fk_id_movimiento_fondo=movimiento, **{clave_medio: medio})
    escrito[tabla_medio].append(medio)
    escrito[tabla_vinculo].append(vinculo)
    escrito["clase"] = CLASE_MOVIMIENTO
    return escrito


def _ids_hijos_movimiento(cur, movimiento_id: int) -> dict[str, list[int]]:
    """Lo que HOY cuelga del movimiento, con las mismas claves que `escrito` (listas ordenadas)."""
    cur.execute("SELECT id_movimiento_fondo FROM movimientos_fondos WHERE id_movimiento_fondo = %s", (movimiento_id,))
    propio = sorted(r[0] for r in cur.fetchall())
    cur.execute("""SELECT id_efectivo_medio_de_pago_en_movimiento_fondo, fk_id_efectivo_medio_de_pago
                   FROM efectivo_medio_de_pago_en_movimientos_fondos WHERE fk_id_movimiento_fondo = %s""", (movimiento_id,))
    efectivo = cur.fetchall()
    cur.execute("""SELECT id_movimiento_cuenta_bancaria_en_movimiento_fondo, fk_id_movimiento_cuenta_bancaria
                   FROM movimientos_cuenta_bancaria_en_movimientos_fondos WHERE fk_id_movimiento_fondo = %s""", (movimiento_id,))
    banco = cur.fetchall()
    return {
        "movimientos_fondos": propio,
        "efectivo_medio_de_pago": sorted(r[1] for r in efectivo),
        "efectivo_medio_de_pago_en_movimientos_fondos": sorted(r[0] for r in efectivo),
        "movimientos_cuentas_bancarias": sorted(r[1] for r in banco),
        "movimientos_cuenta_bancaria_en_movimientos_fondos": sorted(r[0] for r in banco),
    }


def _esperadas_movimiento(plan: gm.Plan, escrito: dict) -> dict[str, dict]:
    """Las filas completas que tendrían que haber quedado, por tabla: las del plan más las claves entre filas."""
    t_medio, t_vinculo = plan.tablas_medio
    clave_medio = "fk_id_efectivo_medio_de_pago" if plan.medio == "efectivo" else "fk_id_movimiento_cuenta_bancaria"
    mid, medio_id = escrito["movimientos_fondos"][0], escrito[t_medio][0]
    return {
        "movimientos_fondos": {gm.COLUMNAS["movimientos_fondos"][0]: mid, **plan.movimiento},
        t_medio: {gm.COLUMNAS[t_medio][0]: medio_id, **plan.medio_fila},
        t_vinculo: {gm.COLUMNAS[t_vinculo][0]: escrito[t_vinculo][0], "fk_id_movimiento_fondo": mid, clave_medio: medio_id,
                    **plan.vinculo},
    }


def _igual(esperado, leido) -> bool:
    """Un importe se compara en centavos; el resto, tal cual (None incluido)."""
    if isinstance(esperado, float) or isinstance(leido, (float, Decimal)):
        return esperado is not None and leido is not None and _centavos(esperado) == _centavos(leido)
    return esperado == leido


def verificar_movimiento(cur, plan: gm.Plan, escrito: dict) -> None:
    """Relee lo escrito (misma transacción, antes del COMMIT) y comprueba que cierra: cada fila COMPLETA contra la del
    plan (todas las columnas de la receta, con los importes en centavos), los vínculos y que no haya asiento. Si no:
    excepción → ROLLBACK."""
    mid = escrito["movimientos_fondos"][0]
    hijos = _ids_hijos_movimiento(cur, mid)
    for tabla in gm.TABLAS_ESCRITO:
        if hijos[tabla] != sorted(escrito[tabla]):
            raise AssertionError(f"Lo escrito en {tabla} no coincide con lo que quedó en la base.")
    otras = gm.TABLAS_BANCO if plan.medio == "efectivo" else gm.TABLAS_EFECTIVO
    if any(hijos[t] for t in otras):
        raise AssertionError("Quedó escrito un medio de pago que el plan no tenía.")

    for tabla, esperada in _esperadas_movimiento(plan, escrito).items():
        columnas = gm.COLUMNAS[tabla]
        cur.execute(f'SELECT {", ".join(columnas)} FROM public."{tabla}" WHERE {columnas[0]} = %s', (esperada[columnas[0]],))
        filas = cur.fetchall()
        if len(filas) != 1:
            raise AssertionError(f"La fila releída de {tabla} no es una sola.")
        distintas = [c for c, v in zip(columnas, filas[0]) if not _igual(esperada[c], v)]
        if distintas:
            raise AssertionError(f"La fila releída de {tabla} no coincide con el plan ({', '.join(distintas)}).")

    cur.execute("""SELECT count(*) FROM asientos_contables
                   WHERE fk_id_movimiento_fondo = %s OR fk_id_movimiento_cuenta_bancaria = ANY(%s)""",
                (mid, hijos["movimientos_cuentas_bancarias"] or [0]))
    if cur.fetchone()[0]:
        raise AssertionError("Hay un asiento colgado del movimiento: el plan no tenía ninguno.")


# ------------------------------------------------------------------ diario local

def _base_de(cfg: dict[str, str], base: str | None) -> str:
    return base or cfg.get("GARRO_BASE", "")


def diario_escribir(evento: str, carga: dict, base: str, **datos) -> None:
    """Una línea JSON por evento. Nunca guarda la propuesta: solo clave, total y los ids escritos."""
    linea = {"hora": datetime.now().isoformat(timespec="seconds"), "carga": carga.get("id"),
             "clave": carga.get("clave"), "base": base, "evento": evento, **datos}
    with open(DIARIO, "a", encoding="utf-8") as f:
        f.write(json.dumps(linea, ensure_ascii=False, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


def diario_ultimo(carga_id, eventos: tuple[str, ...], base: str) -> dict | None:
    """Último evento del diario (de los pedidos) de esa carga en ESA base, o None."""
    ultimo = None
    try:
        with open(DIARIO, encoding="utf-8") as f:
            for linea in f:
                try:
                    d = json.loads(linea)
                except ValueError:
                    continue
                if (isinstance(d, dict) and d.get("carga") == carga_id and d.get("base") == base
                        and d.get("evento") in eventos):
                    ultimo = d
    except OSError:
        return None
    return ultimo


# ------------------------------------------------------------------ cargar una

MOTIVO_CONEXION = "No se pudo conectar con Gestión Integral o se cortó la conexión; el detalle está en el log local."
MOTIVO_OCUPADO = "Gestión Integral estaba ocupado (tablas bloqueadas o consulta demasiado lenta); se reintenta."


def _es_de_conexion(e: BaseException) -> bool:
    try:
        import psycopg2
    except ImportError:
        return isinstance(e, g.ErrorSync)
    return isinstance(e, (g.ErrorSync, psycopg2.OperationalError, psycopg2.InterfaceError))


def _resultado_de_excepcion(e: BaseException) -> dict:
    """Una excepción ANTES del COMMIT (ya hecho el ROLLBACK, nada quedó escrito) como resultado para la cola.
    Lo transitorio vuelve a la cola (`pendiente`); lo demás es un error. El texto de un error de conexión
    nunca sale de la PC (puede traer el host): el detalle queda en el log local."""
    pgcode = getattr(e, "pgcode", None)
    if pgcode in ("55P03", "57014"):  # lock_timeout / statement_timeout
        return {"estado": "pendiente", "codigo": "ocupado", "motivo": MOTIVO_OCUPADO}
    if _es_de_conexion(e):
        return {"estado": "pendiente", "codigo": "sin_conexion", "motivo": MOTIVO_CONEXION}
    return {"estado": "error", "codigo": "excepcion", "motivo": _corto(e)}


def _buscar_lo_propio(cfg: dict[str, str], propuesta: dict, base: str | None) -> tuple[int | None, bool]:
    """Para una retomada: ¿el bot ya escribió este comprobante? Consulta mínima, de solo lectura y sin LOCK, que no
    depende de ninguna guarda. Devuelve (id o None, conclusiva): `conclusiva` es False si no se pudo saber."""
    con = conectar_garro(cfg, solo_lectura=True, base=base)
    try:
        cur = con.cursor()
        cur.execute("SELECT id_operario FROM operarios WHERE nombre_usuario = %s AND habilitado",
                    (cfg.get("CARGAS_OPERARIO", OPERARIO_POR_DEFECTO),))
        operarios = cur.fetchall()
        cur.execute("SELECT id_proveedor, cuit_proveedor, raz_soc_proveedor, nom_fan_proveedor, es_activo FROM proveedores")
        todos = [{"id": r[0], "cuit": r[1], "razon_social": r[2], "fantasia": r[3], "activo": r[4]} for r in cur.fetchall()]
        proveedores = gc.proveedores_de(propuesta, todos)
        numero = gc._entero(propuesta.get("numero"))
        if len(operarios) != 1 or len(proveedores) != 1 or numero is None:
            return None, False
        cur.execute("""SELECT id_comprobante_compra FROM comprobantes_compras
                       WHERE estado_comprobante_compra = 'activo' AND fk_id_proveedor = %s
                         AND nro_comprobante_compra = %s AND fk_id_operario = %s ORDER BY 1""",
                    (proveedores[0]["id"], numero, operarios[0][0]))
        filas = cur.fetchall()
        if len(filas) > 1:
            return None, False
        return (filas[0][0] if filas else None), True
    finally:
        con.close()


def _buscar_movimiento_propio(cfg: dict[str, str], propuesta: dict, base: str | None) -> tuple[int | None, bool]:
    """Para una retomada: ¿el bot ya escribió este movimiento? Mismo usuario del bot, mismo tipo, misma descripción
    exacta, misma fecha y el mismo importe en centavos (sumando sus medios). Solo lectura y sin LOCK. Devuelve
    (id o None, conclusiva): `conclusiva` es False si no se pudo saber."""
    tipo_id = gc._entero(propuesta.get("tipo_mov_id"))
    descripcion = gc.limpiar_texto(propuesta.get("descripcion")) if isinstance(propuesta.get("descripcion"), str) else ""
    importe = gm._importe_valido(propuesta.get("importe"))
    fecha = propuesta.get("fecha")
    try:
        fecha = date.fromisoformat(fecha) if isinstance(fecha, str) else None
    except ValueError:
        fecha = None
    if tipo_id is None or not descripcion or importe is None or fecha is None:
        return None, False
    con = conectar_garro(cfg, solo_lectura=True, base=base)
    try:
        cur = con.cursor()
        cur.execute("SELECT id_operario FROM operarios WHERE nombre_usuario = %s AND habilitado",
                    (cfg.get("CARGAS_OPERARIO", OPERARIO_POR_DEFECTO),))
        operarios = cur.fetchall()
        if len(operarios) != 1:
            return None, False
        cur.execute(f"""SELECT m.id_movimiento_fondo FROM movimientos_fondos AS m
                        WHERE m.fk_id_operario = %s AND m.fk_id_movimiento_fondo_tipo = %s AND m.des_movimiento_fondo = %s
                          AND m.fec_movimiento_fondo::date = %s AND round(({_IMPORTE_DEL_MOVIMIENTO}) * 100) = %s
                        ORDER BY 1""", (operarios[0][0], tipo_id, descripcion, fecha, _centavos(importe)))
        filas = cur.fetchall()
        if len(filas) > 1:
            return None, False
        return (filas[0][0] if filas else None), True
    finally:
        con.close()


def _incierta(resultado: dict, retomada: bool) -> dict:
    """En una retomada, lo que no sea «escrita ahora» ni un rechazo posterior a una búsqueda concluyente no se
    resuelve: no se sabe si quedó algo escrito."""
    if retomada and resultado["estado"] in ("error", "pendiente"):
        return {**resultado, "estado": "indeterminado"}
    return resultado


def cargar_una(cfg: dict[str, str], carga: dict, en_seco: bool, ahora: datetime | None = None,
               base: str | None = None) -> dict:
    """Planifica y, si no es en seco, escribe UN comprobante. Devuelve el resultado para la cola:
    simulada / cargada / rechazada / error / pendiente (vuelve a la cola: se sabe que no se escribió nada).
    «indeterminado» significa que no se pudo saber si quedó algo escrito: la carga no se resuelve y la próxima
    pasada la recibe como retomada."""
    ahora = ahora or datetime.now()
    payload = carga.get("payload") if isinstance(carga.get("payload"), dict) else {}
    propuesta = payload.get("propuesta") if isinstance(payload.get("propuesta"), dict) else {}
    nombre_base = _base_de(cfg, base)
    clase_propuesta, clase_cola = propuesta.get("clase"), carga.get("clase")
    clase = clase_propuesta or clase_cola or CLASE_COMPRA
    if clase not in CLASES_DE_LA_COLA or (clase_propuesta and clase_cola and clase_propuesta != clase_cola):
        return {"estado": "rechazada", "codigo": "clase",
                "motivo": "La clase de la carga no es una de las que se cargan, o no coincide entre la cola y la propuesta."}
    movimiento = clase == CLASE_MOVIMIENTO
    # Una retomada de una carga real puede haber escrito antes de cortarse: LO PRIMERO es buscar lo propio,
    # aunque ahora la escritura esté apagada en esta instalación (si no, quedaría como rechazada por «ya cargado»
    # algo que cargó el bot). Una carga en seco nunca escribió nada: no busca y se simula normal.
    retomada_real = bool(carga.get("retomada")) and carga.get("modo") == "real"
    retomada = retomada_real and not en_seco

    # Un movimiento solo se toma por propio si ESTA carga dejó un «intento» en el diario de esta base: sin intento nunca
    # empezó a escribir y se planifica como una carga nueva.
    intento = diario_ultimo(carga.get("id"), ("intento",), nombre_base) if movimiento else True
    if retomada_real and intento:
        try:
            existente, conclusiva = (_buscar_movimiento_propio if movimiento else _buscar_lo_propio)(cfg, propuesta, base)
        except Exception as e:
            g.escribir_traza()
            g.decir(f"{_ahora_txt()} (cargas) carga {carga.get('id')}: no se pudo buscar lo ya escrito: {_corto(e)}")
            return {"estado": "indeterminado", "codigo": "busqueda_fallida",
                    "motivo": "No se pudo comprobar si la carga ya estaba escrita; el detalle está en el log local."}
        if existente is not None:
            previo = diario_ultimo(carga.get("id"), ("cargada",), nombre_base)
            id_propio = {"clase": CLASE_MOVIMIENTO, "movimiento_id": existente} if movimiento else {"comprobante_id": existente}
            return {"estado": "cargada", **id_propio, "recuperada": True,
                    "escrito": previo.get("escrito") if previo else None}
        if not conclusiva:
            return {"estado": "indeterminado", "codigo": "busqueda_inconclusa",
                    "motivo": "No se pudo comprobar si la carga ya estaba escrita (usuario del bot, proveedor o descripción no únicos)."}
        cierre = (diario_ultimo(carga.get("id"), ("cargada", "deshecha"), nombre_base) or {}).get("evento")
        if movimiento and cierre is None:
            # Hubo un intento, no consta que se haya confirmado ni deshecho y ahora no se encuentra nada: no se sabe si
            # el COMMIT se hizo y alguien lo borró, o si no llegó. No se replanifica.
            return {"estado": "indeterminado", "codigo": "intento_sin_resultado",
                    "motivo": "El diario local tiene un intento de esta carga sin resultado y no se encuentra nada en el programa."}
        if cierre == "cargada":
            # El diario dice que esta carga SÍ se escribió y ahora no está: alguien la anuló en el programa.
            # No se vuelve a escribir algo que una persona decidió borrar: queda para revisar.
            return {"estado": "indeterminado", "codigo": "escrita_y_ya_no_esta",
                    "motivo": "La carga figura como escrita en el diario local y ya no está en el programa."}

    con = None
    en_commit = False
    try:
        con = conectar_garro(cfg, solo_lectura=en_seco, base=base)
        cur = con.cursor()
        if movimiento:
            largos = verificar_estructura(cur, gm.COLUMNAS, gm.TIPOS)
            if not en_seco:
                cur.execute(LOCK_TABLAS_MOVIMIENTO)
            ref_mov = leer_referencias_movimiento(cur, propuesta, ahora, cfg, largos)
            plan_mov = gm.planificar(propuesta, ref_mov, ahora, payload.get("total_pesos"))
            resumen, importe = plan_mov.resumen(), plan_mov.importe
        else:
            largos = verificar_estructura(cur)
            if not en_seco:
                # Con las tablas bloqueadas, ni los duplicados, ni los restos de los pagos ni el número de asiento
                # cambian hasta el COMMIT.
                cur.execute(LOCK_TABLAS)
            ref = leer_referencias(cur, propuesta, ahora, cfg, largos)
            plan = gc.planificar(payload, ref, ahora)
            resumen, importe = plan.resumen(), plan.total
        if en_seco:
            con.rollback()
            return {"estado": "simulada", "plan": resumen}

        try:
            diario_escribir("intento", carga, nombre_base, total=gc.r2(importe))
        except OSError as e:
            raise ErrorDiario(f"No se pudo anotar en el diario local: {e}") from e
        if movimiento:
            escrito = escribir_movimiento(cur, plan_mov)
            verificar_movimiento(cur, plan_mov, escrito)
            escrito["huella"] = huella(cur, escrito, gm.TABLAS_ESCRITO)
            identificador = {"clase": CLASE_MOVIMIENTO, "movimiento_id": escrito["movimientos_fondos"][0]}
        else:
            escrito = escribir(cur, plan)
            verificar_escrito(cur, plan, escrito)
            escrito["huella"] = huella(cur, escrito)
            identificador = {"comprobante_id": escrito["comprobantes_compras"][0]}
        en_commit = True
        con.commit()
        en_commit = False
        try:
            diario_escribir("cargada", carga, nombre_base, escrito=escrito)
        except OSError:
            g.escribir_traza()
            g.decir(f"{_ahora_txt()} (cargas) AVISO: no se pudo anotar en el diario local la carga {carga.get('id')}.")
        return {"estado": "cargada", **identificador, "escrito": escrito, "plan": resumen}
    except gc.NoCargable as e:
        _rollback(con)
        return {"estado": "rechazada", "codigo": e.codigo, "motivo": e.mensaje}
    except ErrorEstructura as e:
        _rollback(con)
        return _incierta({"estado": "error", "codigo": "estructura", "motivo": str(e)[:500]}, retomada)
    except Exception as e:
        g.escribir_traza()
        g.decir(f"{_ahora_txt()} (cargas) carga {carga.get('id')}: {_corto(e)}")
        if en_commit:
            return {"estado": "indeterminado", "codigo": "commit_incierto",
                    "motivo": "No se pudo confirmar si el COMMIT se hizo; el detalle está en el log local."}
        _rollback(con)
        return _incierta(_resultado_de_excepcion(e), retomada)
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _rollback(con) -> None:
    if con is not None:
        try:
            con.rollback()
        except Exception:
            pass


# ------------------------------------------------------------------ cola en Supabase

def _destino(cfg: dict[str, str], sql: str, params: tuple = ()):
    """Una llamada corta a Supabase en su propia conexión y transacción."""
    con = g.conectar_destino(cfg)
    try:
        with con.cursor() as cur:
            cur.execute(sql, params)
            fila = cur.fetchone()
        con.commit()
        return fila[0] if fila else None
    except Exception as e:
        if getattr(e, "pgcode", None) in ("42883", "3F000"):  # función o schema inexistente
            raise ColaSinFunciones() from e
        raise
    finally:
        con.close()


def cola_tomar(cfg: dict[str, str]) -> dict | None:
    """Toma la próxima carga de las clases que esta versión sabe escribir (compras y movimientos de fondos)."""
    try:
        return _destino(cfg, "SELECT garro.cargas_tomar(ARRAY['compra','movimiento_fondo'])")
    except ColaSinFunciones:
        # La cola todavía no tiene la versión con clases (migración pendiente): las compras siguen como siempre.
        return _destino(cfg, "SELECT garro.cargas_tomar()")


def cola_resolver(cfg: dict[str, str], carga_id: int, estado: str, resultado: dict) -> None:
    """Resuelve la carga en la cola. La función devuelve true si la transición es válida: cualquier otra cosa
    (false, NULL) es un error y la carga NO queda resuelta."""
    ok = _destino(cfg, "SELECT garro.cargas_resolver(%s, %s, %s::jsonb)",
                  (carga_id, estado, json.dumps(resultado, ensure_ascii=False, default=str)))
    if ok is not True:
        raise ErrorCola(f"La cola no aceptó pasar la carga {carga_id} a «{estado}».")


def cola_leer(cfg: dict[str, str], carga_id: int) -> dict | None:
    return _destino(cfg, "SELECT garro.cargas_leer(%s)", (carga_id,))


# ------------------------------------------------------------------ candado y pasada

def tomar_lock(ruta: str):
    """Candado del sistema operativo sobre un archivo abierto durante toda la pasada (mismo patrón que el
    relevamiento): si el proceso muere, el sistema lo suelta solo. Devuelve el archivo o None si está tomado."""
    f = open(ruta, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                f.write(b"0")
                f.flush()
            f.seek(0)
            pedir = lambda: msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)  # noqa: E731
            ocupado = (errno.EACCES, errno.EDEADLK, 36)
        else:
            import fcntl
            pedir = lambda: fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # noqa: E731
            ocupado = (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES)
        try:
            pedir()
        except OSError as e:
            if e.errno in ocupado:
                f.close()
                return None
            raise
    except BaseException:
        f.close()
        raise
    return f


def soltar_lock(f) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        f.close()


def _cargas_por_pasada(cfg: dict[str, str]) -> int:
    try:
        n = int(cfg.get("CARGAS_POR_PASADA", "").strip() or POR_PASADA_DEFECTO)
    except ValueError:
        n = POR_PASADA_DEFECTO
    return max(1, min(n, POR_PASADA_MAXIMO))


def _linea_de_log(carga_id, resultado: dict) -> str:
    codigo = f" ({resultado['codigo']})" if resultado.get("codigo") else ""
    extra = " [recuperada]" if resultado.get("recuperada") else ""
    return f"{_ahora_txt()} (cargas) carga {carga_id}: {resultado['estado']}{codigo}{extra}"


def pasada(cfg: dict[str, str]) -> None:
    """Procesa hasta CARGAS_POR_PASADA cargas de la cola. Apagada salvo CARGAS=1. Una sola pasada a la vez."""
    if cfg.get("CARGAS", "0").strip() != "1":
        return
    lock = tomar_lock(ARCHIVO_LOCK)
    if lock is None:
        return
    try:
        real_permitido = cfg.get("CARGAS_REAL", "0").strip() == "1"
        for _ in range(_cargas_por_pasada(cfg)):
            try:
                carga = cola_tomar(cfg)
            except ColaSinFunciones:
                g.decir(f"{_ahora_txt()} (cargas) Supabase todavía no tiene la cola de cargas; no se hace nada.")
                return
            if not carga:
                return
            es_real = carga.get("modo") == "real"
            en_seco = not (es_real and real_permitido)
            resultado = cargar_una(cfg, carga, en_seco=en_seco)
            if es_real and en_seco and resultado.get("estado") == "simulada":
                resultado["aviso"] = "la escritura real está apagada en esta instalación"
            g.decir(_linea_de_log(carga.get("id"), resultado))
            if resultado["estado"] == "indeterminado":
                return  # sin resolver: la próxima pasada la recibe como retomada y busca si quedó escrita
            try:
                cola_resolver(cfg, carga["id"], resultado["estado"], resultado)
            except Exception as e:
                # Si ya se escribió en Garro NO se reintenta escribir: la próxima pasada la recupera como retomada.
                g.escribir_traza(limitar=True)
                g.decir(f"{_ahora_txt()} (cargas) ERROR al resolver la carga {carga.get('id')} en Supabase: {_corto(e)}")
                return
            if resultado["estado"] == "pendiente":
                return  # volvió a la cola: no se la vuelve a tomar en esta misma pasada
    finally:
        soltar_lock(lock)


# ------------------------------------------------------------------ deshacer

# Columnas de otras tablas que apuntan a lo que escribimos: qué ids de nuestras tablas apuntan.
COLUMNAS_QUE_APUNTAN = {
    "fk_id_comprobante_compra": "comprobantes_compras",
    "fk_id_item_comprobante_compra": "items_comprobantes_compras",
    "fk_id_asiento_contable": "asientos_contables",
    "fk_id_concepto_compra": "conceptos_compras",
}


def _escrito_de_la_carga(cfg: dict[str, str], carga_id, copia: str | None) -> dict:
    """`escrito` de una carga ya cargada. En la base real manda la cola (tiene que responder y decir «cargada»;
    sin diario de reemplazo). En una copia nunca se toca la cola: se usa el diario, solo las entradas de esa base."""
    if copia is not None:
        ultimo = diario_ultimo(carga_id, ("cargada", "deshecha"), copia)
        if ultimo is None:
            raise ErrorDeshacer("sin_escrito", f"No hay registro de la carga {carga_id} en la base {copia}.")
        if ultimo["evento"] == "deshecha":
            raise ErrorDeshacer("ya_deshecha", f"La carga {carga_id} ya está deshecha.")
        if not isinstance(ultimo.get("escrito"), dict):
            raise ErrorDeshacer("sin_escrito", f"El diario no tiene lo escrito por la carga {carga_id}.")
        return ultimo["escrito"]
    try:
        fila = cola_leer(cfg, carga_id)
    except Exception as e:
        g.escribir_traza()
        g.decir(f"{_ahora_txt()} (cargas) deshacer: la cola no respondió: {_corto(e)}")
        raise ErrorDeshacer("cola_no_responde", "La cola de Supabase no respondió: no se deshace nada "
                                                "sin saber en qué estado está la carga.") from None
    if not isinstance(fila, dict):
        raise ErrorDeshacer("sin_carga", f"No existe la carga {carga_id} en la cola.")
    if fila.get("estado") == "deshecha":
        raise ErrorDeshacer("ya_deshecha", f"La carga {carga_id} ya está deshecha.")
    if fila.get("estado") != "cargada":
        raise ErrorDeshacer("no_cargada", f"La carga {carga_id} no está cargada (estado: {fila.get('estado')}).")
    resultado = fila.get("resultado")
    if isinstance(resultado, dict) and isinstance(resultado.get("escrito"), dict):
        return resultado["escrito"]
    ultimo = diario_ultimo(carga_id, ("cargada", "deshecha"), _base_de(cfg, None))
    if ultimo and ultimo["evento"] == "cargada" and isinstance(ultimo.get("escrito"), dict):
        return ultimo["escrito"]
    raise ErrorDeshacer("sin_escrito", f"No hay registro de qué se escribió para la carga {carga_id}.")


def _normalizar_escrito(escrito: dict) -> dict:
    clase = escrito.get("clase", CLASE_COMPRA)
    if clase not in CLASES_DE_LA_COLA:
        raise ErrorDeshacer("escrito_invalido", "El registro de lo escrito es de una clase desconocida.")
    tablas = gm.TABLAS_ESCRITO if clase == CLASE_MOVIMIENTO else TABLAS_ESCRITO
    salida: dict = {"clase": clase}
    for tabla in tablas:
        lista = escrito.get(tabla, [])
        if not isinstance(lista, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in lista):
            raise ErrorDeshacer("escrito_invalido", f"El registro de lo escrito no es válido ({tabla}).")
        salida[tabla] = sorted(lista)
    if clase == CLASE_MOVIMIENTO:
        n = lambda ts: [len(salida[t]) for t in ts]  # noqa: E731
        efectivo, banco = n(gm.TABLAS_EFECTIVO), n(gm.TABLAS_BANCO)
        if len(salida["movimientos_fondos"]) != 1 or sorted((efectivo, banco)) != [[0, 0], [1, 1]]:
            raise ErrorDeshacer("escrito_invalido", "El registro de lo escrito no tiene un único movimiento con un único medio.")
    elif len(salida["comprobantes_compras"]) != 1:
        raise ErrorDeshacer("escrito_invalido", "El registro de lo escrito no tiene un único comprobante.")
    if not isinstance(escrito.get("huella"), str) or not escrito["huella"]:
        raise ErrorDeshacer("escrito_invalido", "El registro de lo escrito no trae la huella de su contenido.")
    salida["huella"] = escrito["huella"]
    return salida


def _comillas(nombre: str) -> str:
    return '"' + nombre.replace('"', '""') + '"'


def _filas_colgadas(cur, escrito: dict, apuntan: dict[str, str] | None = None) -> list[str]:
    """Tablas del programa (cualquiera que tenga una columna que apunte a lo que escribimos) con filas colgadas
    que NO escribimos nosotros: por ejemplo un producto vinculado al renglón o una nota de crédito aplicada."""
    apuntan = COLUMNAS_QUE_APUNTAN if apuntan is None else apuntan
    cur.execute("""SELECT c.table_name, c.column_name FROM information_schema.columns AS c
                   JOIN information_schema.tables AS t ON t.table_schema = c.table_schema AND t.table_name = c.table_name
                   WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' AND c.column_name = ANY(%s)
                   ORDER BY 1, 2""", (list(apuntan),))
    colgadas = []
    for tabla, columna in cur.fetchall():
        ids = escrito[apuntan[columna]]
        if not ids:
            continue
        sql = f"SELECT count(*) FROM public.{_comillas(tabla)} WHERE {_comillas(columna)} = ANY(%s)"
        params: list = [ids]
        if tabla in COLUMNAS_TODAS:  # las filas propias no cuentan
            pk = COLUMNAS_TODAS[tabla][0]
            sql += f" AND {pk} <> ALL(%s)"
            params.append(escrito.get(tabla) or [0])
        cur.execute(sql, params)
        if cur.fetchone()[0]:
            colgadas.append(f"{tabla}.{columna}")
    return colgadas


# Nombres de columna (por prefijo) que apuntan al movimiento o a sus medios, aunque no tengan una FK declarada.
PREFIJOS_QUE_APUNTAN_MOVIMIENTO = (("fk_id_efectivo_medio_de_pago", "efectivo_medio_de_pago"),
                                   ("fk_id_movimiento_cuenta_bancaria", "movimientos_cuentas_bancarias"))


def _filas_colgadas_movimiento(cur, escrito: dict) -> list[str]:
    """Filas de otras tablas que usan lo que escribió un movimiento (un pago, un recibo, un asiento, una conciliación...).
    Se buscan por las claves foráneas declaradas hacia las cinco tablas (pg_constraint) y por el nombre de la columna
    (`fk_id_movimiento_fondo`, y los que empiezan con `fk_id_efectivo_medio_de_pago` o `fk_id_movimiento_cuenta_bancaria`)."""
    destinos: dict[tuple[str, str], str] = {}
    cur.execute("""SELECT cl.relname, a.attname, rf.relname
                   FROM pg_constraint c
                   JOIN pg_class cl ON cl.oid = c.conrelid JOIN pg_namespace n ON n.oid = cl.relnamespace
                   JOIN pg_class rf ON rf.oid = c.confrelid
                   JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
                   WHERE c.contype = 'f' AND n.nspname = 'public' AND rf.relname = ANY(%s)""", (list(gm.TABLAS_ESCRITO),))
    for tabla, columna, referida in cur.fetchall():
        destinos[(tabla, columna)] = referida
    cur.execute("""SELECT c.table_name, c.column_name FROM information_schema.columns AS c
                   JOIN information_schema.tables AS t ON t.table_schema = c.table_schema AND t.table_name = c.table_name
                   WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'""")
    for tabla, columna in cur.fetchall():
        if columna == "fk_id_movimiento_fondo":
            destinos.setdefault((tabla, columna), "movimientos_fondos")
        for prefijo, referida in PREFIJOS_QUE_APUNTAN_MOVIMIENTO:
            if columna.startswith(prefijo):
                destinos.setdefault((tabla, columna), referida)
    colgadas = []
    for (tabla, columna), referida in sorted(destinos.items()):
        ids = escrito.get(referida)
        if not ids:
            continue
        sql = f"SELECT count(*) FROM public.{_comillas(tabla)} WHERE {_comillas(columna)} = ANY(%s)"
        params: list = [ids]
        if tabla in COLUMNAS_TODAS:  # las filas propias no cuentan
            sql += f" AND {COLUMNAS_TODAS[tabla][0]} <> ALL(%s)"
            params.append(escrito.get(tabla) or [0])
        cur.execute(sql, params)
        if cur.fetchone()[0]:
            colgadas.append(f"{tabla}.{columna}")
    return colgadas


def deshacer(cfg: dict[str, str], carga_id, confirmar: bool, base: str | None = None) -> dict:
    """Borra lo que escribió una carga, solo si sigue EXACTAMENTE como se escribió (mismos ids y misma huella de
    contenido) y nada más cuelga de ello. Sin `confirmar` solo informa (sesión de solo lectura, sin bloqueos).
    Sin `base` la carga se busca en la cola; con `base` (una copia) nunca se toca la cola: manda el diario."""
    copia = base if base and base != cfg.get("GARRO_BASE") else None
    nombre_base = _base_de(cfg, copia)
    con = None
    try:
        escrito = _normalizar_escrito(_escrito_de_la_carga(cfg, carga_id, copia))
        movimiento = escrito["clase"] == CLASE_MOVIMIENTO
        tablas = gm.TABLAS_ESCRITO if movimiento else TABLAS_ESCRITO
        con = conectar_garro(cfg, solo_lectura=not confirmar, base=copia)
        cur = con.cursor()
        if movimiento:
            verificar_estructura(cur, gm.COLUMNAS, gm.TIPOS)
            if confirmar:
                cur.execute(LOCK_TABLAS_MOVIMIENTO)
                # Las cinco filas quedan tomadas hasta el COMMIT: nadie las edita ni las usa entre la comparación y el borrado.
                for t in gm.TABLAS_ESCRITO:
                    if escrito[t]:
                        cur.execute(f'SELECT 1 FROM public."{t}" WHERE {COLUMNAS_TODAS[t][0]} = ANY(%s) ORDER BY {COLUMNAS_TODAS[t][0]} FOR UPDATE',
                                    (escrito[t],))
            cid = escrito["movimientos_fondos"][0]
            cur.execute("SELECT fk_id_operario FROM movimientos_fondos WHERE id_movimiento_fondo = %s", (cid,))
            fila = cur.fetchone()
            etiqueta, clave_id = f"El movimiento {cid}", "movimiento_id"
        else:
            verificar_estructura(cur)
            if confirmar:
                cur.execute(LOCK_TABLAS)
            cid = escrito["comprobantes_compras"][0]
            cur.execute("SELECT estado_comprobante_compra, fk_id_operario FROM comprobantes_compras "
                        "WHERE id_comprobante_compra = %s", (cid,))
            fila = cur.fetchone()
            etiqueta, clave_id = f"El comprobante {cid}", "comprobante_id"
        if fila is None:
            raise ErrorDeshacer("no_existe", f"{etiqueta} ya no existe en el programa.")
        cur.execute("SELECT id_operario FROM operarios WHERE nombre_usuario = %s AND habilitado",
                    (cfg.get("CARGAS_OPERARIO", OPERARIO_POR_DEFECTO),))
        operarios = cur.fetchall()
        if not movimiento and fila[0] != "activo":
            raise ErrorDeshacer("no_activo", f"{etiqueta} no está activo.")
        if len(operarios) != 1 or fila[-1] != operarios[0][0]:
            raise ErrorDeshacer("no_es_del_bot", f"{etiqueta} no lo cargó el usuario del bot.")
        hijos = _ids_hijos_movimiento(cur, cid) if movimiento else _ids_hijos(cur, cid)
        for tabla in tablas:
            if hijos[tabla] != escrito[tabla]:
                raise ErrorDeshacer("modificado", f"{etiqueta} fue modificado en el programa "
                                                  f"({tabla}): anularlo desde el programa.")
        if huella(cur, escrito, tablas) != escrito["huella"]:
            raise ErrorDeshacer("modificado", f"{etiqueta} fue modificado en el programa "
                                              "(cambió el contenido de alguna fila): anularlo desde el programa.")
        colgadas = _filas_colgadas_movimiento(cur, escrito) if movimiento else _filas_colgadas(cur, escrito)
        if colgadas:
            raise ErrorDeshacer("dependencias", f"Hay filas de otras tablas colgadas {'del movimiento' if movimiento else 'del comprobante'} "
                                                f"{cid} ({', '.join(colgadas)}): anularlo desde el programa.")
        borraria = {t: len(escrito[t]) for t in tablas}
        if not confirmar:
            con.rollback()
            return {"estado": "sin_confirmar", clave_id: cid, "borraria": borraria}

        for tabla in reversed(tablas):
            ids = escrito[tabla]
            if not ids:
                continue
            cur.execute(f'DELETE FROM public."{tabla}" WHERE {COLUMNAS_TODAS[tabla][0]} = ANY(%s)', (ids,))
            if cur.rowcount != len(ids):
                raise AssertionError(f"Se esperaba borrar {len(ids)} filas de {tabla} y se borraron {cur.rowcount}.")
        con.commit()
        resultado = {"estado": "deshecha", clave_id: cid, "borrado": borraria}
        try:
            diario_escribir("deshecha", {"id": carga_id, "clave": None}, nombre_base, escrito=escrito)
        except OSError:
            g.decir(f"{_ahora_txt()} (cargas) AVISO: no se pudo anotar en el diario local la carga {carga_id} deshecha.")
        if copia is None:
            aviso = f"borrado en Garro; la cola NO quedó actualizada: carga {carga_id}"
            try:
                cola_resolver(cfg, carga_id, "deshecha", resultado)
            except Exception as e:
                g.escribir_traza()
                g.decir(f"{_ahora_txt()} (cargas) {aviso} ({_corto(e)})")
                resultado["aviso"] = aviso
                try:
                    diario_escribir("cola_sin_actualizar", {"id": carga_id, "clave": None}, nombre_base)
                except OSError:
                    pass
        return resultado
    except ErrorDeshacer as e:
        _rollback(con)
        return {"estado": "rechazada", "codigo": e.codigo, "motivo": e.mensaje}
    except ErrorEstructura as e:
        _rollback(con)
        return {"estado": "error", "codigo": "estructura", "motivo": str(e)[:500]}
    except Exception as e:
        g.escribir_traza()
        g.decir(f"{_ahora_txt()} (cargas) deshacer {carga_id}: {_corto(e)}")
        _rollback(con)
        if _es_de_conexion(e):
            return {"estado": "error", "codigo": "sin_conexion", "motivo": MOTIVO_CONEXION}
        return {"estado": "error", "codigo": "excepcion", "motivo": _corto(e)}
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


# ------------------------------------------------------------------ comandos

def comprobar_estructura(cfg: dict[str, str], base: str | None = None) -> dict:
    """Prueba de la instalación, solo lectura: la estructura de las dos recetas (compras y movimientos de fondos) y
    las lecturas de referencia. Si alguna no coincide, el error dice cuál."""
    con = conectar_garro(cfg, solo_lectura=True, base=base)
    try:
        cur = con.cursor()
        problemas, largos, largos_mov = [], {}, {}
        try:
            largos = verificar_estructura(cur)
        except ErrorEstructura as e:
            problemas.append(f"Compras: {e}")
        try:
            largos_mov = verificar_estructura(cur, gm.COLUMNAS, gm.TIPOS)
        except ErrorEstructura as e:
            problemas.append(f"Movimientos de fondos: {e}")
        if problemas:
            raise ErrorEstructura(" | ".join(problemas))
        ref = leer_referencias(cur, {}, datetime.now(), cfg, largos)
        tipos_ok, tipos_con_problema = [], []
        for tipo_id in gm.TIPOS_HABILITADOS:
            r = leer_referencias_movimiento(cur, {"tipo_mov_id": tipo_id, "cuenta_bancaria_id": gm.CUENTA_BANCARIA}, datetime.now(), cfg, largos_mov)
            t = r.tipo
            bien = t is not None and t.get("es_egreso") is True and t.get("es_ingreso") is False and t.get("genera_asiento") is False
            (tipos_ok if bien else tipos_con_problema).append(tipo_id)
        con.rollback()
    finally:
        con.close()
    return {"estructura": "OK", "usuario_del_bot": ref.operario_id is not None,
            "configuracion_contable": ref.cuenta_proveedores is not None and ref.cuenta_credito_fiscal is not None,
            "cuentas": len(ref.cuentas), "categorias": len(ref.categorias), "regimenes": len(ref.regimenes),
            "movimientos_de_fondos": {
                "tipos_ok": tipos_ok, "tipos_con_problema": tipos_con_problema,
                "caja": r.caja is not None and r.caja.get("habilitada") is True,
                "cuenta_bancaria": r.cuenta_bancaria is not None and r.cuenta_bancaria.get("activa") is True
                and r.cuenta_bancaria.get("plan") == gm.PLAN_CUENTA_BANCARIO}}


def _cargar_archivo(ruta: str) -> dict:
    with open(ruta, encoding="utf-8") as f:
        datos = json.load(f)
    if not isinstance(datos, dict):
        raise ValueError("El archivo tiene que ser un objeto JSON.")
    payload = datos["payload"] if isinstance(datos.get("payload"), dict) else datos
    # Id propio en el diario (para poder deshacerla): las cargas a mano en una copia no tienen número de cola.
    return {"id": f"copia-{int(time.time())}", "clave": str(datos.get("clave") or "archivo"), "modo": "en_seco",
            "retomada": False, "payload": payload}


def _id_de_carga(texto: str):
    return int(texto) if texto.isdigit() else texto


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Carga de compras y movimientos de fondos en Gestión Integral (apagada por defecto)")
    grupo = ap.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--pasada", action="store_true", help="procesa la cola de cargas (lo que hace la tarea)")
    grupo.add_argument("--archivo", metavar="CARGA.JSON", help="planifica esa carga contra Garro en solo lectura")
    grupo.add_argument("--deshacer", metavar="N", help="borra lo que escribió la carga N (en una copia: el id que mostró al escribir)")
    grupo.add_argument("--estructura", action="store_true", help="prueba la instalación (solo lectura)")
    ap.add_argument("--escribir", action="store_true", help="con --archivo: la escribe, solo en una copia (--base)")
    ap.add_argument("--confirmar", action="store_true", help="con --deshacer: borra de verdad")
    ap.add_argument("--base", help="otra base (una copia) en lugar de GARRO_BASE")
    ap.add_argument("--config", default=os.path.join(AQUI, g.ARCHIVO_CONFIG))
    ap.add_argument("--version", action="version", version=f"garro_escritor {g.VERSION}")
    args = ap.parse_args(argv)
    if args.escribir and not args.archivo:
        ap.error("--escribir va con --archivo")
    if args.confirmar and args.deshacer is None:
        ap.error("--confirmar va con --deshacer")
    if args.pasada and args.base:
        ap.error("--pasada no usa --base: la base real solo se escribe por la cola")
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass

    try:
        cfg = g.leer_archivo(args.config)
        if args.pasada:
            pasada(cfg)
            return 0
        if args.estructura:
            print(json.dumps(comprobar_estructura(cfg, args.base), ensure_ascii=False, indent=2))
            return 0
        if args.deshacer is not None:
            resultado = deshacer(cfg, _id_de_carga(args.deshacer), args.confirmar, args.base)
            print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
            return 0 if resultado["estado"] in ("sin_confirmar", "deshecha") else 1
        # --archivo
        if args.escribir and (not args.base or args.base == cfg.get("GARRO_BASE")):
            print("Rechazado: --escribir solo se permite con --base de una COPIA distinta de la base real "
                  "(la base real se escribe únicamente por la cola).")
            return 2
        carga = _cargar_archivo(args.archivo)
        resultado = cargar_una(cfg, carga, en_seco=not args.escribir, base=args.base)
        if args.escribir:
            resultado["carga_id"] = carga["id"]  # para deshacerla: --deshacer ID --base COPIA
        print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
        return 0 if resultado["estado"] in ("simulada", "cargada") else 1
    except Exception as e:
        g.escribir_traza()
        print(f"ERROR: {g.traducir_error(e, 'origen')}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
