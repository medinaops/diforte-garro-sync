"""Relevamiento SOLO LECTURA: qué filas escribe Gestión Integral cuando se guarda un comprobante de compra.

Viene con garro-sync y usa su Python y su .env.garro-sync. Desde la carpeta de instalación:

    cd $env:LOCALAPPDATA\\Diforte\\garro-sync
    .\\python\\python.exe relevar_compras.py catalogo
    .\\python\\python.exe relevar_compras.py foto
        (se carga UN comprobante en Gestión Integral, como siempre)
    .\\python\\python.exe relevar_compras.py comparar      (contra la última foto)
    .\\python\\python.exe relevar_compras.py pagos         (estructura y ejemplos de pagos, cheques y cajas)
    .\\python\\python.exe relevar_compras.py auto          (una pasada del relevamiento automático)

La sesión se abre READ ONLY (conectar_origen de garro_sync): este script no puede escribir en la base de Garro.
Los modos manuales dejan todo en la carpeta `relevamiento\\` y no suben nada. El modo `auto` lo dispara solo la
tarea de cada 5 minutos (garro_sync.py --si-hay-pedido): guarda su estado en `relevamiento\\auto\\` y sube a
Supabase (tabla privada `garro.relevamientos`) únicamente las diferencias ya redactadas, con las mismas reglas
que `comparar`. Se apaga con RELEVAMIENTO_AUTO=0 en .env.garro-sync.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import secrets
import socket
import sys
import tempfile
import time
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
# Números de cuenta, tarjeta y CBU: solo los últimos 4 caracteres.
PARCIALES = ("nro_cuenta", "numero_cuenta", "nro_tarjeta", "numero_tarjeta", "cbu", "cvu", "alias")
# CUIT y documentos de proveedores, clientes y firmantes: solo los últimos 4.
PARCIALES_EXACTAS = {"cuit_proveedor", "doc_cliente", "cuit_cheque_de_tercero", "cbu_proveedor", "cbu_doc_proveedor",
                     "cuit_empresa", "cuit_cliente"}
# Tablas de personas: solo salen las columnas que empiezan con estos prefijos (lista permitida, no de exclusión);
# el resto (domicilio, contactos, nombres de particulares, observaciones) queda <omitido>.
PERMITIDAS = {
    "proveedores": ("id_", "fk_id_", "es_", "raz_soc_", "nom_fan_", "porc", "fec_ing_", "genera_", "incluir_"),
    "clientes": ("id_", "fk_id_", "es_", "porc", "fec_ing_", "limite_credito", "bloquear_", "no_inlcuir"),
    "operarios": ("id_", "fk_id_", "permitir_", "limitar_", "mostrar_", "habilitado", "nro_sucursal", "agregar_",
                  "imprimir_", "utilizar_"),
    "cheques_de_terceros": ("id_", "fk_id_"),
}
# Familias de tablas que no hacen al relevamiento de compras y pagos y guardan datos de clientes, destinatarios,
# choferes o configuración: `comparar` solo cuenta sus cambios y las muestras no entran en ellas.
SIN_DETALLE = ("client", "venta", "recibo", "presupuesto", "remitos_transito", "garantia", "reparto", "configuracion",
               "transporte", "viajante", "operario", "usuario", "tickets_de_acceso", "pedido", "proyecto", "carrito",
               "demanda", "ordenes_de_compra")


# Solo se vuelcan filas de las tablas del circuito de compras, pagos, caja y contabilidad (lista permitida):
# una tabla que no coincide con ninguna de estas partes, o que coincide con SIN_DETALLE, solo se cuenta.
CON_DETALLE = ("compra", "pago", "cheque", "caja", "fondo", "asiento", "producto", "stock", "concepto", "regimen",
               "percepcion", "retencion", "logs_sistema", "moneda", "efectivo", "tarjeta", "billetera", "cuenta",
               "banc", "proveedor", "alicuota", "impuesto", "planes_de_cuentas", "deposito", "remito", "iva")


def con_detalle(tabla: str) -> bool:
    return (tabla not in TABLAS_SECRETAS and not any(p in tabla for p in SIN_DETALLE)
            and any(p in tabla for p in CON_DETALLE))


# Tablas auxiliares de personas (clientes_domicilios, clientes_contactos, proveedores_viajantes...): idem.
PERMITIDAS_AUXILIARES = ("id_", "fk_id_", "es_", "fec_", "predeterminado", "porc", "imp_")
# Datos personales de terceros (firmantes de cheques, contactos) y códigos de tarjeta: no salen.
PERSONALES_PREFIJO = ("dir_", "cp_", "loc_", "tel", "equipo_")
# logs_sistema guarda entidades distintas con el mismo fk_id_tipo: solo salen los logs de estos tipos (el resto de
# los cambios en logs, como clientes, ventas o usuarios, se cuenta pero no se vuelca).
LOGS_PERMITIDOS = ("ComprobanteCompra", "Pago", "MovimientoFondo", "AjusteCtaCteProveedor", "Producto")
PERSONALES = ("domicilio", "direccion", "mail", "correo", "smtp", "telefono", "celular", "firmante", "des_cheque_de_tercero",
              "cvv", "cvc", "cod_seguridad", "codigo_seguridad", "venc_tarjeta", "vencimiento_tarjeta")
# En las tablas cuyo nombre contiene estas partes (cuentas, tarjetas, billeteras y todo lo que las vincula),
# los números (nro_*, numero*, cbte_*) quedan con los últimos 4 y las observaciones (obs_*), texto libre donde
# puede haber un alias, un titular o un número, no salen.
FAMILIAS_CON_NUMEROS = ("tarjeta", "billetera", "cuenta_bancaria", "cuentas_bancarias")


def permitidas(tabla: str | None) -> tuple | None:
    """Prefijos de columna permitidos si la tabla es de personas; None si rige la regla general."""
    if tabla is None:
        return None
    if tabla in PERMITIDAS:
        return PERMITIDAS[tabla]
    if tabla.startswith(("clientes_", "proveedores_")):
        return PERMITIDAS_AUXILIARES
    return None


def con_numeros(tabla: str | None) -> bool:
    return tabla is not None and any(p in tabla for p in FAMILIAS_CON_NUMEROS)


# Clave de los hashes de las fotos: aleatoria, generada la primera vez y guardada al lado del script (fuera de
# `relevamiento\`, que es lo que se comparte). No sale de ninguna contraseña: una foto no sirve para probar
# contraseñas ni filas candidatas.
ARCHIVO_CLAVE = os.path.join(AQUI, ".clave-relevamiento")
VERSION_HASH = 2
SAL: str | None = None


def clave_local() -> str:
    if not os.path.exists(ARCHIVO_CLAVE):
        # Se escribe completa en un temporal y se publica con os.link, que falla si ya existe: si dos corridas
        # arrancan juntas, gana una y la otra lee esa misma clave, nunca un archivo a medio escribir.
        fd, temporal = tempfile.mkstemp(dir=AQUI, prefix=".clave-", suffix=".tmp")  # 0600, nombre impredecible
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(secrets.token_hex(32))
        try:
            os.link(temporal, ARCHIVO_CLAVE)
        except FileExistsError:
            pass
        finally:
            os.remove(temporal)
    with open(ARCHIVO_CLAVE, encoding="ascii") as f:
        clave = f.read().strip()
    if len(clave) != 64 or any(c not in "0123456789abcdef" for c in clave):
        raise SystemExit(f"{ARCHIVO_CLAVE} está dañado: borralo y sacá una foto nueva.")
    return clave


def id_clave() -> str:
    """Identifica la clave sin revelarla: dos fotos con distinto id no se pueden comparar."""
    assert SAL
    return hashlib.sha256(("id|" + SAL).encode()).hexdigest()[:12]
PROFUNDIDAD = 3
POR_TIPO_ACTIVOS = 3
POR_TIPO_OTROS = 1


def ahora() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def es_sensible(columna: str) -> bool:
    c = columna.lower()
    return c in SENSIBLES_EXACTAS or any(s in c for s in SENSIBLES) or c.startswith("imagen")


def es_omitido(columna: str, tabla: str | None = None) -> bool:
    c = columna.lower()
    lista = permitidas(tabla)
    if lista is not None:
        return c not in PARCIALES_EXACTAS and not c.startswith(lista)
    return (es_sensible(c) or c.startswith(PERSONALES_PREFIJO) or any(p in c for p in PERSONALES)
            or (con_numeros(tabla) and c.startswith("obs_")))


def es_parcial(columna: str, tabla: str | None = None) -> bool:
    c = columna.lower()
    return (c in PARCIALES_EXACTAS or any(p in c for p in PARCIALES)
            or (con_numeros(tabla) and c.startswith(("nro_", "numero", "cbte_"))))


def es_personal(columna: str, tabla: str | None = None) -> bool:
    return es_omitido(columna, tabla) or es_parcial(columna, tabla)


def limpiar(fila: dict, tabla: str | None = None) -> dict:
    out = {}
    for k, v in fila.items():
        if es_omitido(k, tabla):
            out[k] = "<omitido>"
        elif v is not None and not isinstance(v, bool) and es_parcial(k, tabla):
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


def conectar(cfg: dict[str, str] | None = None):
    global SAL
    if cfg is None:
        cfg = garro_sync.leer_archivo(os.path.join(AQUI, garro_sync.ARCHIVO_CONFIG))
    SAL = clave_local()
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
                out["referencia"][t] = [limpiar(r["r"], t) for r in filas(
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
            if not ids or tabla not in existentes or not con_detalle(tabla):
                continue
            cols = pk.get(tabla)
            if not cols or len(cols) != 1:
                continue
            filas_t = filas(cur, sql.SQL("SELECT row_to_json(t) AS r FROM public.{} t WHERE t.{} = ANY(%s)").format(
                sql.Identifier(tabla), sql.Identifier(cols[0])), (list(ids),))
            destino = juntado.setdefault(tabla, {})
            for f in filas_t:
                destino[str(f["r"][cols[0]])] = limpiar(f["r"], tabla)
            for hija, col in hijos.get(cols[0], []):
                if hija not in existentes or not con_detalle(hija) or not pk.get(hija) or len(pk[hija]) != 1:
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
            juntado.setdefault(padre, {}).update({str(r["r"][cols[0]]): limpiar(r["r"], padre) for r in filas(
                cur, sql.SQL("SELECT row_to_json(t) AS r FROM public.{} t WHERE t.{} = ANY(%s)").format(
                    sql.Identifier(padre), sql.Identifier(cols[0])), (list(ids),))})
    # Historial de costo de esos productos: si el programa lo escribe al guardar la compra, se ve acá.
    ids_prod = [int(k) for k in juntado.get("productos", {})]
    if ids_prod and "productos_costos" in existentes:
        juntado["productos_costos"] = {str(i): limpiar(r["r"]) for i, r in enumerate(filas(
            cur, "SELECT row_to_json(t) AS r FROM productos_costos t WHERE fk_id_producto = ANY(%s)", (ids_prod,)))}
    # Logs: fk_id_tipo no sigue la convención de nombres y es el id de entidades distintas: se filtra por tipo.
    juntado["logs_sistema_por_fk_id_tipo"] = {str(i): limpiar(r["r"], "logs_sistema") for i, r in enumerate(
        logs_de(cur, "ComprobanteCompra", raiz))}
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


def logs_de(cur, tipo_cbte: str, ids: list) -> list[dict]:
    if not ids:
        return []
    return filas(cur, """SELECT row_to_json(t) AS r FROM logs_sistema t
                         WHERE t.tipo_cbte_log_sistema = %s AND t.fk_id_tipo = ANY(%s)""", (tipo_cbte, list(ids)))


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
            if tabla not in existentes or not con_detalle(tabla) or not cols or len(cols) != 1:
                continue
            ya = juntado.setdefault(tabla, {})
            pedir = [i for i in ids if str(i) not in ya]
            if pedir:
                for f in filas(cur, sql.SQL("SELECT row_to_json(t) AS r FROM public.{} t WHERE t.{} = ANY(%s)").format(
                        sql.Identifier(tabla), sql.Identifier(cols[0])), (pedir,)):
                    fila = f["r"]
                    ya[str(fila[cols[0]])] = limpiar(fila, tabla)
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
                if t in TABLAS_SECRETAS or es_personal(nombre, t):
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
                out["referencia"][t] = [limpiar(r["r"], t) for r in filas(
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
        # Logs de las entidades de la muestra, filtrados por tipo (fk_id_tipo se repite entre entidades).
        if "logs_sistema" in existentes:
            ids_por_tabla = {t: [f[pk[t][0]] for f in out["muestras"].get(t, [])] for t in
                             ("pagos", "movimientos_fondos", "ajustes_cuenta_corriente_proveedores") if pk.get(t)}
            out["muestras"]["logs_sistema"] = [limpiar(r["r"], "logs_sistema") for tipo, t in
                                               (("Pago", "pagos"), ("MovimientoFondo", "movimientos_fondos"),
                                                ("AjusteCtaCteProveedor", "ajustes_cuenta_corriente_proveedores"))
                                               for r in logs_de(cur, tipo, ids_por_tabla.get(t, []))]
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


def expr_hash(cols: list[str], tabla: str | None = None) -> tuple[sql.Composable, list[str]]:
    """md5 con clave (SAL) de la fila entera: una foto nunca trae un hash comparable de una fila cruda (contraseñas,
    números de cuenta, datos de terceros). Mismo resultado para la misma fila dentro de una instalación."""
    assert SAL, "SAL sin inicializar: usar conectar()"
    return sql.SQL("md5(%s || t::text)"), [SAL]


def tomar_foto(cur) -> dict:
    """Un hash por fila de cada tabla. Con PK: {pk: md5}. Sin PK: {md5: cuántas filas iguales}."""
    # Con zona horaria: el relevamiento automático lo sube como timestamptz y no puede quedar corrido.
    foto: dict = {"generado": datetime.now().astimezone().isoformat(), "version_hash": VERSION_HASH, "clave": id_clave(), "tablas": {}}
    pk = claves_primarias(cur)
    cols_por_tabla = columnas(cur)
    stats = {r["relname"]: r for r in filas(cur, """SELECT relname, n_tup_ins, n_tup_upd, n_tup_del
                                                     FROM pg_stat_user_tables WHERE schemaname = 'public'""")}
    for t in tablas(cur):
        controlar_plazo()
        cols = pk.get(t)
        h, params = expr_hash(cols_por_tabla.get(t, []), t)
        filas_t: dict = {}
        if cols:
            cur.execute(sql.SQL("SELECT {}, {} FROM public.{} t").format(expr_pk(cols), h, sql.Identifier(t)), params)
            filas_t = {k: v for k, v in cur.fetchall()}
        else:
            cur.execute(sql.SQL("SELECT {}, count(*) FROM public.{} t GROUP BY 1").format(h, sql.Identifier(t)), params)
            filas_t = {k: int(n) for k, n in cur.fetchall()}
        foto["tablas"][t] = {"pk": cols, "columnas": cols_por_tabla.get(t, []), "filas": filas_t, "stats": stats.get(t)}
    controlar_plazo()
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


def calcular_cambios(cur, antes: dict, despues: dict) -> dict:
    """Tablas que cambiaron entre dos fotos, con las filas redactadas de las que tienen detalle.

    Lo usan `comparar` y el modo automático: misma lógica y misma redacción. El cursor tiene que ser el de la
    transacción REPEATABLE READ donde se tomó `despues`, así lo que se cuenta es lo que se vuelca.
    """
    cambios: dict = {}
    for t in sorted(set(antes["tablas"]) | set(despues["tablas"])):
        controlar_plazo()
        a, d = antes["tablas"].get(t), despues["tablas"].get(t)
        item = diferencia_tabla(a, d)
        item["tabla_nueva"], item["tabla_borrada"] = a is None, d is None
        if not (item["altas"] or item["bajas"] or item["modificadas"] or item["tabla_nueva"] or item["tabla_borrada"]):
            continue
        item["stats_antes"] = (a or {}).get("stats")
        item["stats_despues"] = (d or {}).get("stats")
        if d is not None and con_detalle(t):
            if item["pk"] and item["claves_alta_o_modif"]:
                consulta = sql.SQL("SELECT row_to_json(t) AS r FROM public.{} t WHERE {} = ANY(%s)").format(
                    sql.Identifier(t), expr_pk(item["pk"]))
                volcadas = [limpiar(r["r"], t) for r in filas(cur, consulta, (item["claves_alta_o_modif"],))]
                if t == "logs_sistema":
                    ajenas = [f for f in volcadas if f.get("tipo_cbte_log_sistema") not in LOGS_PERMITIDOS]
                    volcadas = [f for f in volcadas if f.get("tipo_cbte_log_sistema") in LOGS_PERMITIDOS]
                    item["logs_de_otros_tipos"] = len(ajenas)
                item["filas_nuevas_o_modificadas"] = volcadas
            elif not item["pk"] and item["hashes_alta"]:
                h, params = expr_hash(d.get("columnas", []), t)
                consulta = sql.SQL("SELECT row_to_json(t) AS r, {} AS h FROM public.{} t WHERE {} = ANY(%s)").format(
                    h, sql.Identifier(t), h)
                vistos: set = set()
                item["filas_nuevas_o_modificadas"] = []
                for r in filas(cur, consulta, params + params + [list(item["hashes_alta"])]):
                    if r["h"] not in vistos:  # una por hash; la cantidad está en hashes_alta
                        vistos.add(r["h"])
                        item["filas_nuevas_o_modificadas"].append({"veces": item["hashes_alta"][r["h"]], **limpiar(r["r"], t)})
        elif t in TABLAS_SECRETAS:
            item["filas_nuevas_o_modificadas"] = "<omitido: tabla con secretos>"
        elif d is not None:
            item["filas_nuevas_o_modificadas"] = "<omitido: clientes, ventas o configuración; solo se cuentan los cambios>"
        cambios[t] = item
    controlar_plazo()
    return cambios


def comparar(con, ruta_antes: str) -> str:
    with open(ruta_antes, encoding="utf-8") as f:
        antes = json.load(f)
    if antes.get("version_hash") != VERSION_HASH or antes.get("clave") != id_clave():
        raise SystemExit(f"La foto {os.path.basename(ruta_antes)} es de otra versión del relevamiento (o de otra clave): "
                         "no se puede comparar. Sacá una foto nueva antes de cargar el comprobante.")
    # La foto nueva y el detalle salen de la MISMA transacción REPEATABLE READ: lo que se cuenta es lo que se vuelca.
    with con.cursor() as cur:
        despues = tomar_foto(cur)
        cambios = calcular_cambios(cur, antes, despues)
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


# ------------------------------------------------------------------ relevamiento automático

# Tablas que Gestión Integral toca al guardar una compra, un pago o un movimiento de fondos: si ninguna cambió,
# no vale la pena ni sacar la foto completa. `productos` no está: las ventas también la actualizan y una compra
# ya cambia items_comprobantes_compras.
DISPARADORES = ["comprobantes_compras", "items_comprobantes_compras", "pagos", "movimientos_fondos",
                "ajustes_cuenta_corriente_proveedores", "planes_de_cuentas", "asientos_contables"]
# Tope de costo de una pasada (el candado no depende de esto: lo suelta el sistema operativo si el proceso muere).
LIMITE_CORRIDA_SEGUNDOS = 25 * 60
_plazo: tuple[float, float] | None = None  # (inicio, vencimiento) en reloj() de la pasada automática en curso


class CorridaDemasiadoLarga(Exception):
    pass


def reloj() -> float:
    return time.monotonic()


def controlar_plazo() -> None:
    """Solo rige en el modo automático (`_plazo` es None en foto/comparar a mano)."""
    if _plazo is not None and reloj() > _plazo[1]:
        raise CorridaDemasiadoLarga(f"{int(reloj() - _plazo[0])}")
MAX_BYTES_SUBIDA = 4 * 1024 * 1024
MAX_DIFERENCIAS_LOCALES = 200


def dir_auto(cfg: dict[str, str]) -> str:
    """Una carpeta por base de origen: si cambia el servidor, la base o el puerto, no se compara contra otra base."""
    origen = f"{cfg.get('GARRO_HOST', '')}|{cfg.get('GARRO_PUERTO', '')}|{cfg.get('GARRO_BASE', '')}"
    return os.path.join(SALIDA, "auto", hashlib.sha256(origen.encode()).hexdigest()[:12])


def escribir_atomico(ruta: str, datos) -> None:
    """Temporal en la misma carpeta + os.replace: nunca queda un JSON a medio escribir."""
    fd, temporal = tempfile.mkstemp(dir=os.path.dirname(ruta), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, default=str)
        os.replace(temporal, ruta)
    except BaseException:
        try:
            os.remove(temporal)
        except OSError:
            pass
        raise


def tomar_lock(cfg: dict[str, str]):
    """Candado del sistema operativo sobre un archivo abierto durante toda la pasada: si el proceso muere, el sistema
    lo suelta solo (sin vencimientos ni carreras). Devuelve el archivo abierto, o None si otra pasada lo tiene."""
    f = open(os.path.join(dir_auto(cfg), "lock"), "a+b")
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
                return None  # otra pasada lo tiene
            raise
    except BaseException:
        f.close()  # cualquier otra falla (escritura, permisos) se propaga para que quede en el log
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


def leer_estado(cfg: dict[str, str]) -> tuple[dict, dict] | None:
    """(estado, foto base) o None si falta o está dañado: en ese caso se arranca de cero."""
    try:
        with open(os.path.join(dir_auto(cfg), "estado.json"), encoding="utf-8") as f:
            estado = json.load(f)
        with open(os.path.join(dir_auto(cfg), estado["foto"]), encoding="utf-8") as f:
            return estado, json.load(f)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def guardar_base(cfg: dict[str, str], nueva: dict, contadores: dict) -> None:
    """La foto nueva pasa a ser la base. El estado se publica al final: si algo falla antes, queda el anterior."""
    controlar_plazo()  # pasado el tope no se publica nada: la próxima pasada reintenta desde la base anterior
    carpeta = dir_auto(cfg)
    nombre = f"base-{ahora()}.json"
    escribir_atomico(os.path.join(carpeta, nombre), nueva)
    escribir_atomico(os.path.join(carpeta, "estado.json"),
                     {"contadores": contadores, "foto": nombre, "generado": datetime.now().astimezone().isoformat()})
    for f in os.listdir(carpeta):  # la base anterior y las huérfanas de una corrida cortada
        if f.startswith("base-") and f != nombre:
            try:
                os.remove(os.path.join(carpeta, f))
            except OSError:
                pass


def leer_contadores(cur) -> dict[str, int]:
    """Altas + modificaciones + bajas acumuladas de las tablas disparadoras (barato: no toca las tablas)."""
    cur.execute("""SELECT relname, COALESCE(n_tup_ins, 0) + COALESCE(n_tup_upd, 0) + COALESCE(n_tup_del, 0)
                   FROM pg_stat_user_tables WHERE schemaname = 'public' AND relname::text = ANY(%s::text[])""", (DISPARADORES,))
    return {t: int(n) for t, n in cur.fetchall()}


CLAVES_DE_FILAS = ("claves_alta_o_modif", "claves_borradas", "hashes_alta", "hashes_baja")


class DiferenciaDemasiadoGrande(Exception):
    """Ni con solo los conteos entra en el límite; `minima` es esa versión para dejarla en disco."""

    def __init__(self, minima: dict):
        super().__init__("la diferencia no entra en el límite de subida")
        self.minima = minima


def para_subir(cambios: dict) -> dict:
    """Lo que sale del servidor: las tablas sin detalle (clientes, ventas, configuración...) y las borradas solo
    informan cantidades, sin las claves ni los hashes de sus filas. `comparar` a mano no pasa por acá."""
    out = copy.deepcopy(cambios)
    for t, c in out.items():
        sin_filas = not con_detalle(t) or c.get("tabla_borrada")
        for k in CLAVES_DE_FILAS:
            # logs_sistema mezcla entidades (clientes, ventas...): sus claves y hashes no salen nunca.
            if k in c and (sin_filas or t == "logs_sistema"):
                del c[k]
    return out


def acotar(carga: dict, maximo: int = MAX_BYTES_SUBIDA) -> dict:
    """Garantiza que el JSON no pase de `maximo`: primero se recortan las filas volcadas de las tablas más pesadas,
    después sus claves y hashes y, como último recurso, cada tabla queda solo con sus cantidades."""
    def tamano() -> int:
        return len(json.dumps(carga, default=str, ensure_ascii=False).encode("utf-8"))
    if tamano() <= maximo:
        return carga
    carga = copy.deepcopy(carga)

    def recortar(campos: tuple[str, ...], texto: str) -> bool:
        pesos = sorted(((len(json.dumps(c[k], default=str)), t, k) for t, c in carga["cambios"].items()
                        for k in campos if isinstance(c.get(k), (list, dict))), reverse=True)
        for _, t, k in pesos:
            item = carga["cambios"][t]
            item[k] = f"<recortado: {len(item[k])} {texto}>"
            if tamano() <= maximo:
                return True
        return False

    if recortar(("filas_nuevas_o_modificadas",), "filas") or recortar(CLAVES_DE_FILAS, "claves"):
        return carga
    for c in carga["cambios"].values():
        for k in ("filas_nuevas_o_modificadas", *CLAVES_DE_FILAS, "stats_antes", "stats_despues"):
            c.pop(k, None)
    if tamano() > maximo:
        raise DiferenciaDemasiadoGrande(carga)
    return carga


def guardar_diferencia(cfg: dict[str, str], carga: dict) -> str:
    carpeta = dir_auto(cfg)
    ruta = os.path.join(carpeta, f"diferencia-{ahora()}.json")
    escribir_atomico(ruta, carga)
    viejas = sorted(f for f in os.listdir(carpeta) if f.startswith("diferencia-") and f.endswith(".json"))
    for f in viejas[:-MAX_DIFERENCIAS_LOCALES]:
        try:
            os.remove(os.path.join(carpeta, f))
        except OSError:
            pass
    return ruta


def subir(cfg: dict[str, str], carga: dict) -> None:
    """Sube la diferencia ya redactada a garro.relevamientos, en una transacción propia y corta."""
    con = garro_sync.conectar_destino(cfg)
    try:
        with con.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '60s'")  # una subida colgada no puede frenar la tarea
            cur.execute("SELECT garro.subir_relevamiento(%s, %s, %s, %s, %s::jsonb)",
                        (socket.gethostname(), carga["antes"], carga["despues"], VERSION_HASH,
                         json.dumps(carga["cambios"], default=str, ensure_ascii=False)))
            fila = cur.fetchone()
            if not fila or fila[0] is None:  # el upsert fue rechazado: no se cuenta como subido
                raise RuntimeError("Supabase rechazó el relevamiento (la función no devolvió id)")
        con.commit()
    finally:
        con.close()


def auto(cfg: dict[str, str]) -> str:
    """Una pasada del relevamiento automático. Devuelve el resultado en una palabra (o 'subido N')."""
    if cfg.get("RELEVAMIENTO_AUTO", "1").strip() == "0":
        return "desactivado"
    os.makedirs(dir_auto(cfg), exist_ok=True)
    lock = tomar_lock(cfg)
    if lock is None:
        return "ocupado"
    global _plazo
    _plazo = (reloj(), reloj() + LIMITE_CORRIDA_SEGUNDOS)
    try:
        return _auto(cfg)
    except CorridaDemasiadoLarga as e:
        garro_sync.decir(f"El relevamiento automático pasó el límite de {LIMITE_CORRIDA_SEGUNDOS // 60} minutos "
                         f"({e} s): la foto completa es demasiado lenta para este servidor. Se puede apagar con "
                         "RELEVAMIENTO_AUTO=0 en .env.garro-sync.")
        return "demasiado_larga"
    finally:
        _plazo = None
        soltar_lock(lock)


def _auto(cfg: dict[str, str]) -> str:
    previo = leer_estado(cfg)
    con = conectar(cfg)
    try:
        with con.cursor() as cur:
            contadores = leer_contadores(cur)
            if previo is None:
                guardar_base(cfg, tomar_foto(cur), contadores)
                return "base_inicial"
            estado, base = previo
            guardados = estado.get("contadores") or {}
            if any(contadores.get(t, 0) < n for t, n in guardados.items()):
                # Estadísticas reiniciadas (el servidor se reinició): los números ya no se pueden comparar.
                guardar_base(cfg, tomar_foto(cur), contadores)
                return "base_reiniciada"
            if contadores == guardados:
                return "sin_cambios"
            despues = tomar_foto(cur)
            if base.get("version_hash") != VERSION_HASH or base.get("clave") != id_clave():
                guardar_base(cfg, despues, contadores)
                return "base_reiniciada"
            cambios = calcular_cambios(cur, base, despues)
    finally:
        con.rollback()
        con.close()

    if not any(con_detalle(t) for t in cambios):
        guardar_base(cfg, despues, contadores)
        return "sin_cambios_compras"
    decir = garro_sync.decir
    carga: dict | None = None
    problema: Exception | None = None
    try:
        carga = acotar({"antes": base["generado"], "despues": despues["generado"], "cambios": para_subir(cambios)})
    except DiferenciaDemasiadoGrande as e:
        carga, problema = e.minima, e
    # Guardar en disco y subir se resuelven por separado: cada uno puede fallar sin arrastrar al otro.
    try:
        guardar_diferencia(cfg, carga)
        guardada = True
    except OSError as e:
        guardada = False
        decir(f"No se pudo guardar la diferencia en disco ({type(e).__name__}).")
    donde = "queda guardada acá" if guardada else "no quedó guardada"
    if problema is not None:
        decir(f"La diferencia no entra en el límite de subida: {donde}, solo con los conteos.")
        subida = False
    else:
        try:
            subir(cfg, carga)
            subida = True
        except Exception as e:
            subida = False
            if getattr(e, "pgcode", None) == "42883":
                decir(f"Falta la migración 373 en Supabase (relevamiento automático): la diferencia {donde}.")
            else:
                decir(f"No se pudo subir el relevamiento ({type(e).__name__}); la diferencia {donde}.")
    if not (guardada or subida):
        # Ni en disco ni en Supabase: la base no avanza y la próxima pasada recalcula la ventana completa.
        return "reintentar"
    # La ventana avanza: la diferencia ya salió por alguno de los dos caminos y no conviene que crezca sin límite.
    try:
        guardar_base(cfg, despues, contadores)
    except OSError:
        garro_sync.decir("No se pudo guardar el estado local; la próxima pasada vuelve a subir la ventana, "
                         "que se reemplaza en Supabase.")
        return "reintentar"
    return f"subido {sum(1 for t in cambios if con_detalle(t))}" if subida else "solo_local"


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("catalogo", "foto", "comparar", "pagos", "auto"):
        print(__doc__)
        return 2
    if argv[0] == "auto":
        cfg = garro_sync.leer_archivo(os.path.join(AQUI, garro_sync.ARCHIVO_CONFIG))
        inicio = time.monotonic()
        resultado = auto(cfg)
        print(f"Relevamiento automático: {resultado} ({int(time.monotonic() - inicio)} s)")
        return 0
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
