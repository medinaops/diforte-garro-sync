"""Relevamiento SOLO LECTURA: qué filas escribe Gestión Integral cuando se guarda un comprobante de compra.

Viene con garro-sync y usa su Python y su .env.garro-sync. Desde la carpeta de instalación:

    cd $env:LOCALAPPDATA\\Diforte\\garro-sync
    .\\python\\python.exe relevar_compras.py catalogo
    .\\python\\python.exe relevar_compras.py foto
        (se carga UN comprobante en Gestión Integral, como siempre)
    .\\python\\python.exe relevar_compras.py comparar      (contra la última foto)
    .\\python\\python.exe relevar_compras.py pagos         (estructura y ejemplos de pagos, cheques y cajas)

La sesión se abre READ ONLY (conectar_origen de garro_sync): este script no puede escribir en la base.
Lo que produce queda en la carpeta `relevamiento\\` y no se sube a ningún lado.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)

import garro_sync  # noqa: E402  (vive en la misma carpeta)
from psycopg2 import sql  # noqa: E402

SALIDA = os.path.join(AQUI, "relevamiento")
# Columnas que nunca salen del servidor: contraseñas, tokens, firmas y certificados.
SENSIBLES = ("contrasena", "password", "clave", "pass_", "token", "secret", "certificado", "private")
SENSIBLES_EXACTAS = {"sign", "cert", "key"}
# Tablas cuyas filas nunca se vuelcan (el diff solo informa cuántas cambiaron).
TABLAS_SECRETAS = {"tickets_de_acceso_afip"}
# Tablas de referencia de compras que se copian enteras (todas chicas).
REFERENCIA = [
    "tipos_comprobantes_compras", "condiciones_compra", "alicuotas_iva", "cuentas_imputacion_items_compras",
    "tipos_no_gravados_compras", "regimenes_especiales", "impuestos_compras", "tipos_regimenes_compras",
    "planes_de_cuentas", "planes_de_cuentas_config", "tipos_iva", "monedas", "monedas_historicos",
    "productos_depositos", "productos_rubros", "productos_unidades_de_medidas",
]
# Modo `pagos`: tablas cuya estructura se vuelca entera (columnas, tipos, PK, FKs, conteo).
PATRONES_PAGOS = ("pago", "cheque", "caja", "fondo", "banc", "billetera", "tarjeta", "efectivo", "medio_de_pago",
                  "ajustes_cuenta_corriente_proveedores", "regimenes_especiales", "condiciones_compra", "retencion")
# Tablas chicas de referencia de pagos y caja que se copian enteras.
REFERENCIA_PAGOS = ["tipos_pagos", "cajas", "condiciones_compra", "bancos", "cuentas_bancarias",
                    "movimientos_fondos_tipos", "movimientos_fondos_categorias", "billeteras_virtuales",
                    "tarjetas_creditos_propias", "regimenes_especiales", "tipos_regimenes_compras", "impuestos_compras"]
# Al juntar ejemplos de pagos se sube por la FK a estas tablas (el cheque, el medio de pago, la factura)...
SUBIR_PAGOS = {"cheques", "cheques_propios", "efectivo_medio_de_pago", "efectivo_deposito_bancario",
               "movimientos_cuentas_bancarias",
               "movimientos_billeteras_virtuales", "otro_medio_de_pago", "comprobantes_compras",
               "ajustes_cuenta_corriente_proveedores", "pagos", "movimientos_fondos", "movimientos_caja"}
# ...y desde estas, una vez alcanzadas hacia arriba, se vuelve a bajar (cheques_de_terceros cuelga de cheques).
BAJAR_DESDE_ARRIBA = {"pagos", "movimientos_fondos", "movimientos_caja", "cheques", "cheques_propios",
                      "efectivo_medio_de_pago", "efectivo_deposito_bancario", "movimientos_cuentas_bancarias",
                      "movimientos_billeteras_virtuales", "otro_medio_de_pago"}
POR_GRUPO_PAGOS = 3
ULTIMOS_PAGOS = 20
# Números de cuenta, CBU y CUIT de firmantes de cheques: solo los últimos 4 caracteres.
PARCIALES = ("nro_cuenta", "cbu", "alias", "cuit_cheque")
# Datos personales de terceros (firmantes de cheques, contactos): no salen.
PERSONALES = ("dir_", "cp_", "loc_", "tel", "mail", "email", "domicilio", "celular", "des_cheque_de_tercero")
# Clave del hash de las columnas sensibles: sale de la contraseña de la base, que nunca se exporta. Así una foto
# no trae un MD5 de una contraseña o un token que se pueda atacar por diccionario.
SAL: str | None = None
PROFUNDIDAD = 3
POR_TIPO_ACTIVOS = 3
POR_TIPO_OTROS = 1


def ahora() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def es_sensible(columna: str) -> bool:
    c = columna.lower()
    return c in SENSIBLES_EXACTAS or any(s in c for s in SENSIBLES) or c.startswith("imagen")


def es_personal(columna: str) -> bool:
    c = columna.lower()
    return c.startswith(PERSONALES) or any(p in c for p in PARCIALES)


def limpiar(fila: dict) -> dict:
    out = {}
    for k, v in fila.items():
        if es_sensible(k) or k.lower().startswith(PERSONALES):
            out[k] = "<omitido>"
        elif v is not None and any(p in k.lower() for p in PARCIALES):
            out[k] = "…" + str(v)[-4:]
        else:
            out[k] = v
    return out


def guardar(nombre: str, datos) -> str:
    os.makedirs(SALIDA, exist_ok=True)
    ruta = os.path.join(SALIDA, nombre)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=1, default=str)
    return ruta


def conectar():
    global SAL
    cfg = garro_sync.leer_archivo(os.path.join(AQUI, garro_sync.ARCHIVO_CONFIG))
    if not cfg.get("GARRO_PASSWORD"):
        raise SystemExit("Falta GARRO_PASSWORD en .env.garro-sync")
    SAL = hashlib.sha256(("relevamiento-garro|" + cfg["GARRO_PASSWORD"]).encode()).hexdigest()
    con = garro_sync.conectar_origen(cfg)  # ya deja la sesión readonly
    con.rollback()
    # Una sola foto coherente de toda la base, y nada de quedarse esperando locks del programa.
    con.set_session(isolation_level="REPEATABLE READ", readonly=True, autocommit=False)
    with con.cursor() as cur:
        cur.execute("SET statement_timeout = '120s'")
        cur.execute("SET lock_timeout = '5s'")
    con.commit()  # sin esto, el próximo rollback deshace los SET (la transacción no escribió nada)
    return con


def filas(cur, consulta, params=None) -> list[dict]:
    cur.execute(consulta, params)
    nombres = [d[0] for d in cur.description]
    return [dict(zip(nombres, r)) for r in cur.fetchall()]


def tablas(cur) -> list[str]:
    cur.execute("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 1""")
    return [r[0] for r in cur.fetchall()]


def claves_primarias(cur) -> dict[str, list[str]]:
    # Orden por attnum: alcanza con que sea el mismo en todas las corridas (sin array_position, que no está en todas las versiones).
    cur.execute("""SELECT c.relname, a.attname
                   FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
                   WHERE i.indisprimary AND n.nspname = 'public'
                   ORDER BY c.relname, a.attnum""")
    pk: dict[str, list[str]] = {}
    for t, col in cur.fetchall():
        pk.setdefault(t, []).append(col)
    return pk


def expr_pk(cols: list[str]) -> sql.Composable:
    if len(cols) == 1:
        return sql.SQL("t.{}::text").format(sql.Identifier(cols[0]))
    return sql.SQL("ROW({})::text").format(sql.SQL(", ").join(sql.SQL("t.{}").format(sql.Identifier(c)) for c in cols))


# ------------------------------------------------------------------ catálogo

def catalogo(con) -> str:
    with con.cursor() as cur:
        out: dict = {"generado": datetime.now().isoformat(), "version": con.server_version}
        out["triggers"] = filas(cur, """
            SELECT c.relname AS tabla, t.tgname AS trigger, pg_get_triggerdef(t.oid) AS definicion,
                   p.proname AS funcion
            FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_proc p ON p.oid = t.tgfoid
            WHERE NOT t.tgisinternal AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY 1, 2""")
        out["funciones"] = filas(cur, """
            SELECT n.nspname AS esquema, p.proname AS nombre,
                   pg_get_function_identity_arguments(p.oid) AS argumentos, l.lanname AS lenguaje,
                   p.prosecdef AS security_definer, p.prosrc AS codigo
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND l.lanname NOT IN ('c', 'internal')
            ORDER BY 1, 2""")
        out["defaults"] = filas(cur, """
            SELECT c.relname AS tabla, a.attname AS columna, pg_get_expr(d.adbin, d.adrelid) AS valor
            FROM pg_attrdef d JOIN pg_class c ON c.oid = d.adrelid JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
            WHERE n.nspname = 'public' ORDER BY 1, 2""")
        out["constraints"] = filas(cur, """
            SELECT c.relname AS tabla, k.conname AS nombre, k.contype AS tipo, pg_get_constraintdef(k.oid) AS definicion
            FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' ORDER BY 1, 2""")
        out["reglas"] = filas(cur, "SELECT tablename, rulename, definition FROM pg_rules WHERE schemaname = 'public'")
        out["vistas"] = filas(cur, "SELECT viewname, definition FROM pg_views WHERE schemaname = 'public' ORDER BY 1")
        secuencias = filas(cur, """SELECT c.relname AS secuencia FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                                   WHERE n.nspname = 'public' AND c.relkind = 'S' ORDER BY 1""")
        for s in secuencias:
            cur.execute(sql.SQL("SELECT last_value, is_called FROM public.{}").format(sql.Identifier(s["secuencia"])))
            s["last_value"], s["is_called"] = cur.fetchone()
        out["secuencias"] = secuencias
        # Quién escribe: usuario y programa de las conexiones activas (sin IPs).
        out["conexiones"] = filas(cur, """SELECT usename, application_name, count(*) AS n FROM pg_stat_activity
                                          GROUP BY 1, 2 ORDER BY 3 DESC""")

        existentes = set(tablas(cur))
        out["referencia"] = {}
        for t in REFERENCIA:
            if t in existentes:
                out["referencia"][t] = [limpiar(r["r"]) for r in filas(
                    cur, sql.SQL("SELECT row_to_json(t) AS r FROM public.{} t").format(sql.Identifier(t)))]
        out["logs_tipos"] = filas(cur, """SELECT tipo_log_sistema, tipo_cbte_log_sistema, origen_log_sistema, count(*) AS n
                                          FROM logs_sistema GROUP BY 1, 2, 3 ORDER BY 4 DESC""")
        out["muestras"] = muestras(cur, existentes)
    con.rollback()
    return guardar(f"catalogo-{ahora()}.json", out)


def muestras(cur, existentes: set[str]) -> dict:
    """Por cada tipo de comprobante de compra: los últimos activos y uno no activo, con todo lo que cuelga."""
    pk = claves_primarias(cur)
    cur.execute("""SELECT table_name, column_name FROM information_schema.columns
                   WHERE table_schema = 'public' AND column_name LIKE 'fk\\_id\\_%'""")
    hijos: dict[str, list[tuple[str, str]]] = {}
    for t, col in cur.fetchall():
        hijos.setdefault(col[3:], []).append((t, col))  # 'fk_id_x' -> clave 'id_x'

    cur.execute("""SELECT id_comprobante_compra FROM (
                     SELECT id_comprobante_compra,
                            row_number() OVER (PARTITION BY fk_id_tipo_comprobante_compra,
                                               estado_comprobante_compra = 'activo'
                                               ORDER BY id_comprobante_compra DESC) AS n,
                            estado_comprobante_compra = 'activo' AS activo
                     FROM comprobantes_compras) s
                   WHERE (activo AND n <= %s) OR (NOT activo AND n <= %s)""", (POR_TIPO_ACTIVOS, POR_TIPO_OTROS))
    raiz = [r[0] for r in cur.fetchall()]
    # Comprobantes en otra moneda (dólares): cómo quedan guardados los importes.
    cur.execute("""SELECT id_comprobante_compra FROM comprobantes_compras
                   WHERE fk_id_moneda IS DISTINCT FROM (SELECT id_moneda FROM monedas WHERE es_predeterminada LIMIT 1)
                   ORDER BY id_comprobante_compra DESC LIMIT 10""")
    raiz += [r[0] for r in cur.fetchall() if r[0] not in raiz]

    juntado: dict[str, dict[str, dict]] = {}
    frente = {"comprobantes_compras": set(raiz)}
    for _ in range(PROFUNDIDAD + 1):
        nuevo: dict[str, set] = {}
        for tabla, ids in frente.items():
            if not ids or tabla not in existentes:
                continue
            cols = pk.get(tabla)
            if not cols or len(cols) != 1:
                continue
            filas_t = filas(cur, sql.SQL("SELECT row_to_json(t) AS r FROM public.{} t WHERE t.{} = ANY(%s)").format(
                sql.Identifier(tabla), sql.Identifier(cols[0])), (list(ids),))
            destino = juntado.setdefault(tabla, {})
            for f in filas_t:
                destino[str(f["r"][cols[0]])] = limpiar(f["r"])
            for hija, col in hijos.get(cols[0], []):
                if hija not in existentes or not pk.get(hija) or len(pk[hija]) != 1:
                    continue
                cur.execute(sql.SQL("SELECT t.{} FROM public.{} t WHERE t.{} = ANY(%s)").format(
                    sql.Identifier(pk[hija][0]), sql.Identifier(hija), sql.Identifier(col)), (list(ids),))
                ya = juntado.get(hija, {})
                nuevos = {r[0] for r in cur.fetchall() if str(r[0]) not in ya}
                if nuevos:
                    nuevo.setdefault(hija, set()).update(nuevos)
        frente = nuevo
    # Hacia arriba, un nivel: el texto de los conceptos, los productos y los proveedores de las muestras.
    for padre in ("conceptos_compras", "productos", "proveedores"):
        cols = pk.get(padre)
        if padre not in existentes or not cols or len(cols) != 1:
            continue
        fk = "fk_" + cols[0]
        ids = {f[fk] for t, v in juntado.items() for f in v.values() if f.get(fk) is not None}
        if ids:
            juntado.setdefault(padre, {}).update({str(r["r"][cols[0]]): limpiar(r["r"]) for r in filas(
                cur, sql.SQL("SELECT row_to_json(t) AS r FROM public.{} t WHERE t.{} = ANY(%s)").format(
                    sql.Identifier(padre), sql.Identifier(cols[0])), (list(ids),))})
    # Historial de costo de esos productos: si el programa lo escribe al guardar la compra, se ve acá.
    ids_prod = [int(k) for k in juntado.get("productos", {})]
    if ids_prod and "productos_costos" in existentes:
        juntado["productos_costos"] = {str(i): limpiar(r["r"]) for i, r in enumerate(filas(
            cur, "SELECT row_to_json(t) AS r FROM productos_costos t WHERE fk_id_producto = ANY(%s)", (ids_prod,)))}
    # Logs: fk_id_tipo no sigue la convención de nombres. Las filas traen tipo_cbte/origen para filtrar las ajenas.
    juntado["logs_sistema_por_fk_id_tipo"] = {
        str(i): limpiar(r["r"]) for i, r in enumerate(filas(cur, "SELECT row_to_json(t) AS r FROM logs_sistema t WHERE fk_id_tipo = ANY(%s)", (raiz,)))}
    return {"comprobantes": raiz, "filas": {t: list(v.values()) for t, v in juntado.items()}}


# ------------------------------------------------------------------ pagos, cheques y cajas

def claves_foraneas(cur) -> dict[tuple[str, str], str]:
    """(tabla, columna) -> tabla referenciada, solo FKs de una columna."""
    cur.execute("""SELECT c.relname, a.attname, r.relname
                   FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_class r ON r.oid = k.confrelid
                   JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = k.conkey[1]
                   WHERE k.contype = 'f' AND n.nspname = 'public' AND array_length(k.conkey, 1) = 1""")
    return {(t, col): ref for t, col, ref in cur.fetchall()}


def ids_de(cur, consulta: sql.Composable, params=None) -> list:
    cur.execute(consulta, params)
    return [r[0] for r in cur.fetchall()]


def ultimos(cur, tabla: str, pk: str, n: int, por: str | None, cols: list[str]) -> list:
    """Los n últimos de la tabla, más los POR_GRUPO_PAGOS últimos de cada valor de `por` (si la columna existe)."""
    out = ids_de(cur, sql.SQL("SELECT {} FROM public.{} ORDER BY 1 DESC LIMIT %s").format(
        sql.Identifier(pk), sql.Identifier(tabla)), (n,))
    if por and por in cols:
        out += ids_de(cur, sql.SQL("""SELECT id FROM (SELECT {pk} AS id, row_number() OVER (PARTITION BY {por}
                                       ORDER BY {pk} DESC) AS n FROM public.{t}) s WHERE n <= %s""").format(
            pk=sql.Identifier(pk), por=sql.Identifier(por), t=sql.Identifier(tabla)), (POR_GRUPO_PAGOS,))
    return sorted(set(out))


def juntar_pagos(cur, raices: dict[str, list], existentes: set[str]) -> dict:
    """Desde las raíces: baja por fk_id_X -> id_X (como `muestras`) y sube por FK a SUBIR_PAGOS.

    Lo alcanzado hacia arriba no se vuelve a bajar (salvo BAJAR_DESDE_ARRIBA): una factura traería todo lo suyo.
    Proveedores, operarios y cajas no se suben: el id alcanza (proveedores ya está en la copia; cajas va en referencia).
    """
    pk = claves_primarias(cur)
    fks = claves_foraneas(cur)
    cur.execute("""SELECT table_name, column_name FROM information_schema.columns
                   WHERE table_schema = 'public' AND column_name LIKE 'fk\\_id\\_%'""")
    hijos: dict[str, list[tuple[str, str]]] = {}
    for t, col in cur.fetchall():
        hijos.setdefault(col[3:], []).append((t, col))

    juntado: dict[str, dict[str, dict]] = {}
    # frente: (tabla, puede_bajar) -> ids
    frente: dict[tuple[str, bool], set] = {(t, True): set(ids) for t, ids in raices.items() if ids}
    for _ in range(PROFUNDIDAD + 2):
        nuevo: dict[tuple[str, bool], set] = {}
        for (tabla, puede_bajar), ids in frente.items():
            cols = pk.get(tabla)
            if tabla not in existentes or not cols or len(cols) != 1:
                continue
            ya = juntado.setdefault(tabla, {})
            pedir = [i for i in ids if str(i) not in ya]
            if pedir:
                for f in filas(cur, sql.SQL("SELECT row_to_json(t) AS r FROM public.{} t WHERE t.{} = ANY(%s)").format(
                        sql.Identifier(tabla), sql.Identifier(cols[0])), (pedir,)):
                    fila = f["r"]
                    ya[str(fila[cols[0]])] = limpiar(fila)
                    for col, v in fila.items():  # hacia arriba
                        ref = fks.get((tabla, col))
                        if v is not None and ref in SUBIR_PAGOS and str(v) not in juntado.get(ref, {}):
                            nuevo.setdefault((ref, ref in BAJAR_DESDE_ARRIBA), set()).add(v)
            if not (puede_bajar and ids):
                continue
            for hija, col in hijos.get(cols[0], []):  # hacia abajo
                if hija not in existentes or not pk.get(hija) or len(pk[hija]) != 1:
                    continue
                vistos = juntado.get(hija, {})
                nuevos = {i for i in ids_de(cur, sql.SQL("SELECT t.{} FROM public.{} t WHERE t.{} = ANY(%s)").format(
                    sql.Identifier(pk[hija][0]), sql.Identifier(hija), sql.Identifier(col)), (list(ids),))
                    if str(i) not in vistos}
                if nuevos:
                    nuevo.setdefault((hija, True), set()).update(nuevos)
        frente = nuevo
    return {t: list(v.values()) for t, v in juntado.items() if v}


def pagos(con) -> str:
    with con.cursor() as cur:
        out: dict = {"generado": datetime.now().isoformat(), "version": con.server_version}
        existentes = set(tablas(cur))
        pk = claves_primarias(cur)
        fks = claves_foraneas(cur)
        cur.execute("""SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
                       FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
                       JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE n.nspname = 'public' AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
                       ORDER BY c.relname, a.attnum""")
        todas: dict[str, list[dict]] = {}
        for t, col, tipo, notnull in cur.fetchall():
            todas.setdefault(t, []).append({"columna": col, "tipo": tipo, "not_null": notnull,
                                            "fk": fks.get((t, col))})
        elegidas = sorted(t for t in existentes if any(p in t for p in PATRONES_PAGOS))
        estructura: dict = {}
        for t in elegidas:
            cur.execute(sql.SQL("SELECT count(*) FROM public.{}").format(sql.Identifier(t)))
            cols = todas.get(t, [])
            item = {"filas": cur.fetchone()[0], "pk": pk.get(t), "columnas": cols}
            # Cuántas filas por cada estado o tipo: dice qué se usa de verdad.
            grupos = {}
            for c in cols:
                nombre = c["columna"]
                if t in TABLAS_SECRETAS or es_sensible(nombre) or es_personal(nombre):
                    continue
                if (nombre.startswith(("estado", "tipo_", "es_", "fk_id_tipo", "fk_id_caja", "fk_id_moneda"))
                        or nombre in ("fk_id_movimiento_fondo_tipo", "fk_id_condicion_compra", "genera_cta_cte",
                                      "genera_ctacte", "ctacte")):
                    grupos[nombre] = [{"valor": v, "n": n} for v, n in ids_de_pares(cur, sql.SQL(
                        "SELECT {c}::text, count(*) FROM public.{t} GROUP BY 1 ORDER BY 2 DESC LIMIT 30").format(
                        c=sql.Identifier(nombre), t=sql.Identifier(t)))]
            item["por_valor"] = grupos
            estructura[t] = item
        out["estructura"] = estructura
        out["referencia"] = {}
        for t in REFERENCIA_PAGOS:
            if t in existentes and t not in TABLAS_SECRETAS:
                out["referencia"][t] = [limpiar(r["r"]) for r in filas(
                    cur, sql.SQL("SELECT row_to_json(t) AS r FROM public.{} t").format(sql.Identifier(t)))]

        def raiz(tabla: str, por: str | None, n: int = ULTIMOS_PAGOS) -> list:
            cols = [c["columna"] for c in todas.get(tabla, [])]
            if tabla not in existentes or not pk.get(tabla) or len(pk[tabla]) != 1:
                return []
            return ultimos(cur, tabla, pk[tabla][0], n, por, cols)

        raices = {
            "pagos": raiz("pagos", "fk_id_tipo_pago"),
            "movimientos_fondos": raiz("movimientos_fondos", "fk_id_movimiento_fondo_tipo"),
            "movimientos_caja": raiz("movimientos_caja", "fk_id_caja", 10),
            "cheques": raiz("cheques", "estado_cheque", 10),
            "ajustes_cuenta_corriente_proveedores": raiz("ajustes_cuenta_corriente_proveedores", None, 10),
            # Compras por condición (contado / cuenta corriente): cómo queda pagada una compra contado.
            "comprobantes_compras": raiz("comprobantes_compras", "fk_id_condicion_compra", 5),
        }
        out["raices"] = raices
        out["muestras"] = juntar_pagos(cur, raices, existentes)
    con.rollback()
    return guardar(f"pagos-{ahora()}.json", out)


def ids_de_pares(cur, consulta: sql.Composable, params=None) -> list[tuple]:
    cur.execute(consulta, params)
    return cur.fetchall()


# ------------------------------------------------------------------ foto y comparación

def columnas(cur) -> dict[str, list[str]]:
    cur.execute("""SELECT c.relname, a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = 'public' AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
                   ORDER BY c.relname, a.attnum""")
    out: dict[str, list[str]] = {}
    for t, col in cur.fetchall():
        out.setdefault(t, []).append(col)
    return out


def expr_hash(cols: list[str]) -> tuple[sql.Composable, list[str]]:
    """md5 de la fila; las columnas sensibles entran solo como hash con clave (SAL), nunca crudas."""
    sensibles = [c for c in cols if es_sensible(c) or es_personal(c)]
    if not sensibles:
        return sql.SQL("md5(t::text)"), []
    assert SAL, "SAL sin inicializar: usar conectar()"
    normales = [c for c in cols if c not in sensibles]
    partes = [sql.SQL("ROW({})::text").format(sql.SQL(", ").join(sql.SQL("t.{}").format(sql.Identifier(c)) for c in normales))
              if normales else sql.SQL("''")]
    partes += [sql.SQL("md5(%s || coalesce(t.{}::text, ''))").format(sql.Identifier(c)) for c in sensibles]
    return sql.SQL("md5({})").format(sql.SQL(" || ").join(partes)), [SAL] * len(sensibles)


def tomar_foto(cur) -> dict:
    """Un hash por fila de cada tabla. Con PK: {pk: md5}. Sin PK: {md5: cuántas filas iguales}."""
    foto: dict = {"generado": datetime.now().isoformat(), "tablas": {}}
    pk = claves_primarias(cur)
    cols_por_tabla = columnas(cur)
    stats = {r["relname"]: r for r in filas(cur, """SELECT relname, n_tup_ins, n_tup_upd, n_tup_del
                                                     FROM pg_stat_user_tables WHERE schemaname = 'public'""")}
    for t in tablas(cur):
        cols = pk.get(t)
        h, params = expr_hash(cols_por_tabla.get(t, []))
        filas_t: dict = {}
        if cols:
            cur.execute(sql.SQL("SELECT {}, {} FROM public.{} t").format(expr_pk(cols), h, sql.Identifier(t)), params)
            filas_t = {k: v for k, v in cur.fetchall()}
        else:
            cur.execute(sql.SQL("SELECT {}, count(*) FROM public.{} t GROUP BY 1").format(h, sql.Identifier(t)), params)
            filas_t = {k: int(n) for k, n in cur.fetchall()}
        foto["tablas"][t] = {"pk": cols, "columnas": cols_por_tabla.get(t, []), "filas": filas_t, "stats": stats.get(t)}
    return foto


def foto(con) -> str:
    with con.cursor() as cur:
        datos = tomar_foto(cur)
    con.rollback()
    return guardar(f"foto-{ahora()}.json", datos)


def diferencia_tabla(a: dict, d: dict) -> dict:
    """Altas, bajas y modificaciones entre dos fotos de una tabla (a o d pueden faltar: tabla nueva o borrada)."""
    pk = (d or a)["pk"]
    fa, fd = (a or {}).get("filas", {}), (d or {}).get("filas", {})
    if pk:
        altas = [k for k in fd if k not in fa]
        bajas = [k for k in fa if k not in fd]
        modif = [k for k in fd if k in fa and fa[k] != fd[k]]
        return {"pk": pk, "altas": len(altas), "bajas": len(bajas), "modificadas": len(modif),
                "claves_alta_o_modif": altas + modif, "claves_borradas": bajas}
    # Sin PK: multiconjunto. Una modificación se ve como una baja + una alta.
    delta = {h: int(fd.get(h, 0)) - int(fa.get(h, 0)) for h in set(fa) | set(fd)}
    return {"pk": None, "altas": sum(v for v in delta.values() if v > 0),
            "bajas": -sum(v for v in delta.values() if v < 0), "modificadas": 0,
            "hashes_alta": {h: v for h, v in delta.items() if v > 0},
            "hashes_baja": {h: -v for h, v in delta.items() if v < 0}}


def comparar(con, ruta_antes: str) -> str:
    with open(ruta_antes, encoding="utf-8") as f:
        antes = json.load(f)
    cambios: dict = {}
    # La foto nueva y el detalle salen de la MISMA transacción REPEATABLE READ: lo que se cuenta es lo que se vuelca.
    with con.cursor() as cur:
        despues = tomar_foto(cur)
        for t in sorted(set(antes["tablas"]) | set(despues["tablas"])):
            a, d = antes["tablas"].get(t), despues["tablas"].get(t)
            item = diferencia_tabla(a, d)
            item["tabla_nueva"], item["tabla_borrada"] = a is None, d is None
            if not (item["altas"] or item["bajas"] or item["modificadas"] or item["tabla_nueva"] or item["tabla_borrada"]):
                continue
            item["stats_antes"] = (a or {}).get("stats")
            item["stats_despues"] = (d or {}).get("stats")
            if d is not None and t not in TABLAS_SECRETAS:
                if item["pk"] and item["claves_alta_o_modif"]:
                    consulta = sql.SQL("SELECT row_to_json(t) AS r FROM public.{} t WHERE {} = ANY(%s)").format(
                        sql.Identifier(t), expr_pk(item["pk"]))
                    item["filas_nuevas_o_modificadas"] = [limpiar(r["r"]) for r in filas(cur, consulta, (item["claves_alta_o_modif"],))]
                elif not item["pk"] and item["hashes_alta"]:
                    h, params = expr_hash(d.get("columnas", []))
                    consulta = sql.SQL("SELECT row_to_json(t) AS r, {} AS h FROM public.{} t WHERE {} = ANY(%s)").format(
                        h, sql.Identifier(t), h)
                    vistos: set = set()
                    item["filas_nuevas_o_modificadas"] = []
                    for r in filas(cur, consulta, params + params + [list(item["hashes_alta"])]):
                        if r["h"] not in vistos:  # una por hash; la cantidad está en hashes_alta
                            vistos.add(r["h"])
                            item["filas_nuevas_o_modificadas"].append({"veces": item["hashes_alta"][r["h"]], **limpiar(r["r"])})
            elif t in TABLAS_SECRETAS:
                item["filas_nuevas_o_modificadas"] = "<omitido: tabla con secretos>"
            cambios[t] = item
    con.rollback()
    guardar(f"foto-{ahora()}.json", despues)
    ruta = guardar(f"diferencia-{ahora()}.json", {"antes": ruta_antes, "despues": despues["generado"], "cambios": cambios})
    print("\nTablas que cambiaron:")
    for t, c in cambios.items():
        extra = " (tabla nueva)" if c["tabla_nueva"] else " (tabla borrada)" if c["tabla_borrada"] else ""
        print(f"  {t}: +{c['altas']} altas, {c['modificadas']} modificadas, -{c['bajas']} bajas{extra}")
    if not cambios:
        print("  ninguna (¿se guardó el comprobante?)")
    return ruta


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("catalogo", "foto", "comparar", "pagos"):
        print(__doc__)
        return 2
    antes = argv[1] if len(argv) > 1 else None
    if argv[0] == "comparar" and antes is None:
        # Sin argumento: la última foto (los nombres llevan fecha y hora, así que el orden alfabético es el temporal).
        fotos = sorted(f for f in os.listdir(SALIDA) if f.startswith("foto-")) if os.path.isdir(SALIDA) else []
        if not fotos:
            print("No hay ninguna foto todavía: primero correr  relevar_compras.py foto")
            return 2
        antes = os.path.join(SALIDA, fotos[-1])
        print(f"Comparando contra {fotos[-1]}")
    con = conectar()
    try:
        if argv[0] == "catalogo":
            ruta = catalogo(con)
        elif argv[0] == "pagos":
            ruta = pagos(con)
        elif argv[0] == "foto":
            ruta = foto(con)
        else:
            ruta = comparar(con, antes)
    finally:
        con.close()
    print(f"\nListo: {ruta}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
