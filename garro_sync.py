"""Copia de Gestión Integral (Garro Sistemas) al schema `garro` de Supabase.

Corre en el servidor de la fábrica (o en una PC de la red Radmin). Lee la base de
Gestión Integral con un usuario de SOLO LECTURA y escribe la copia completa en
Supabase con el rol `garro_sync`, que solo puede vaciar e insertar en `garro.*`.
La conexión sale del servidor hacia Supabase: no abre ningún puerto.

Cada corrida es una copia completa (unas 30.000 filas) dentro de UNA transacción:
o queda la foto nueva entera o queda la anterior. Así entran solas las anulaciones
y correcciones de comprobantes viejos.

Orden de una corrida (evita que una copia lenta pise a una más nueva):
  1. conecta a Supabase y toma un candado (pg_try_advisory_xact_lock);
  2. recién ahí lee Gestión Integral;
  3. vacía, copia y registra la corrida en la misma transacción.

Antes de vaciar el espejo se valida la foto leída (`validar_foto`): una lectura vacía o con una
caída enorme respecto de la última copia correcta no reemplaza a la foto sana.

Toda corrida real deja una fila en `garro.sync_corridas` (ok o error). Si ni siquiera se
puede escribir el error en Supabase, queda en `garro_sync.log`, al lado de este script.

Uso:
    python garro_sync.py               # copia completa
    python garro_sync.py --prueba      # lee el origen y cuenta filas, no escribe nada
    python garro_sync.py --si-hay-pedido  # copia solo si alguien pidió «Actualizar ahora» (tarea de cada 5 min)
    python garro_sync.py --prueba-destino # prueba la conexión segura y la contraseña de Supabase; no escribe nada
    python garro_sync.py --forzar      # copia saltando la guardia de la foto (solo uso manual)
    python garro_sync.py --ddl         # imprime el SQL de las tablas espejo (mig 329)

Configuración: archivo `.env.garro-sync` al lado de este script (ver README.md).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import socket
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone

# Tablas y columnas a copiar: (nombre, columna_pk, [(columna, tipo_destino), ...]).
# Los nombres son los de Gestión Integral. Solo lo que usa el módulo de costos.
# De clientes y proveedores se copian identificación fiscal, razón social, nombre de fantasía,
# condición de IVA y condiciones comerciales (lista de precios, tipo de cliente, condición de
# venta, descuento, estado, cuenta de imputación); NO se copian teléfonos, mails ni domicilios.
# Esta lista es la fuente de verdad de la migración 329 (`--ddl`): si se cambia,
# va una migración nueva con el mismo cambio.
TABLAS: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("tipos_comprobantes_ventas", "id_tipo_comprobante_venta", [
        ("id_tipo_comprobante_venta", "integer"), ("des_tipo_comprobante_venta", "text"),
        ("letra", "text"), ("es_fiscal", "boolean"), ("es_nc", "text"),
        ("es_nota_credito", "boolean"), ("es_nd", "boolean"), ("resta_stock", "text"),
        ("en_cuenta_corriente_suma", "text"), ("se_incluye_en_libro_iva", "boolean"),
        ("es_activo", "boolean"),
    ]),
    ("tipos_comprobantes_compras", "id_tipo_comprobante_compra", [
        ("id_tipo_comprobante_compra", "integer"), ("des_tipo_comprobante_compra", "text"),
        ("letra", "text"), ("es_nc", "text"), ("discrimina_iva", "boolean"),
        ("suma_stock", "text"), ("se_incluye_en_libro_iva", "boolean"),
    ]),
    ("tipos_iva", "id_tipo_iva", [
        ("id_tipo_iva", "integer"), ("des_tipo_iva", "text"), ("discrimina_iva", "boolean"),
        ("letra_comprobante_venta", "text"), ("requiere_cuit", "boolean"), ("activo", "boolean"),
    ]),
    ("listas_precios", "id_lista_precio", [
        ("id_lista_precio", "integer"), ("des_lista_precio", "text"),
        ("porc_lista_precio", "double precision"),
    ]),
    ("monedas", "id_moneda", [
        ("id_moneda", "integer"), ("des_moneda", "text"), ("cod_moneda", "text"),
        ("imp_moneda", "double precision"),
    ]),
    ("cuentas_imputacion_items_compras", "id_cuenta_imputacion_item_compra", [
        ("id_cuenta_imputacion_item_compra", "integer"),
        ("des_cuenta_imputacion_item_compra", "text"),
    ]),
    ("cuentas_imputacion_items_ventas", "id_cuenta_imputacion_item_venta", [
        ("id_cuenta_imputacion_item_venta", "integer"),
        ("des_cuenta_imputacion_item_venta", "text"),
    ]),
    ("clientes", "id_cliente", [
        ("id_cliente", "integer"), ("doc_cliente", "text"), ("raz_soc_cliente", "text"),
        ("nom_fan_cliente", "text"), ("fk_id_tipo_iva", "integer"),
        ("fk_id_tipo_documento", "integer"), ("fk_id_lista_precio", "integer"),
        ("fk_id_cliente_tipo", "integer"), ("fk_id_condicion_venta", "integer"),
        ("porcentaje_descuento", "double precision"), ("es_activo", "boolean"),
    ]),
    ("proveedores", "id_proveedor", [
        ("id_proveedor", "integer"), ("cuit_proveedor", "text"), ("raz_soc_proveedor", "text"),
        ("nom_fan_proveedor", "text"), ("fk_id_tipo_iva", "integer"),
        ("fk_id_cuenta_imputacion_item_compra", "integer"), ("es_activo", "boolean"),
    ]),
    ("comprobantes_ventas", "id_comprobante_venta", [
        ("id_comprobante_venta", "integer"), ("fec_comprobante_venta", "timestamp"),
        ("fec_contab_comprobante_venta", "timestamp"), ("nro_suc_comprobante_venta", "integer"),
        ("nro_comprobante_venta", "integer"), ("estado_comprobante_venta", "text"),
        ("fk_id_tipo_comprobante_venta", "integer"), ("fk_id_cliente", "integer"),
        ("fk_id_condicion_venta", "integer"), ("porc_desc_comprobante_venta", "double precision"),
        ("fk_id_moneda", "integer"), ("imp_moneda_comprobante_venta", "double precision"),
    ]),
    ("items_comprobantes_ventas", "id_item_comprobante_venta", [
        ("id_item_comprobante_venta", "integer"), ("fk_id_comprobante_venta", "integer"),
        ("can_item_comprobante_venta", "double precision"),
        ("imp_neto_item_comprobante_venta", "double precision"),
        ("porc_iva_item_comprobante_venta", "double precision"),
        ("imp_int_item_comprobante_venta", "double precision"),
        ("porc_desc_item_comprobante_venta", "double precision"),
        ("fk_id_cuenta_imputacion_item_venta", "integer"),
        ("costo_item_comprobante_venta", "double precision"),
        ("fk_id_alicuota_iva", "integer"), ("detalle_item_comprobante_venta", "text"),
    ]),
    ("productos_x_items_comprobantes_ventas", "id_producto_x_item_comprobante_venta", [
        ("id_producto_x_item_comprobante_venta", "integer"), ("fk_id_producto", "integer"),
        ("fk_id_item_comprobante_venta", "integer"), ("fk_id_pedido", "integer"),
    ]),
    ("conceptos_ventas", "id_concepto_venta", [
        ("id_concepto_venta", "integer"), ("des_concepto_venta", "text"),
    ]),
    ("conceptos_x_items_comprobantes_ventas", "id_concepto_x_item_comprobante_venta", [
        ("id_concepto_x_item_comprobante_venta", "integer"),
        ("fk_id_item_comprobante_venta", "integer"), ("fk_id_concepto_venta", "integer"),
    ]),
    ("comprobantes_ventas_electronicos", "id_comprobante_venta_electronico", [
        ("id_comprobante_venta_electronico", "integer"), ("fk_id_comprobante_venta", "integer"),
        ("cae", "text"), ("fec_cae", "date"), ("es_comprobante_rechazado", "text"),
    ]),
    ("comprobantes_compras", "id_comprobante_compra", [
        ("id_comprobante_compra", "integer"), ("fec_comprobante_compra", "timestamp"),
        ("fec_contab_comprobante_compra", "timestamp"), ("nro_suc_comprobante_compra", "integer"),
        ("nro_comprobante_compra", "integer"), ("estado_comprobante_compra", "text"),
        ("fk_id_tipo_comprobante_compra", "integer"), ("fk_id_proveedor", "integer"),
        ("porc_desc_comprobante_compra", "double precision"), ("fk_id_moneda", "integer"),
        ("imp_moneda", "double precision"),
    ]),
    ("items_comprobantes_compras", "id_item_comprobante_compra", [
        ("id_item_comprobante_compra", "integer"), ("fk_id_comprobante_compra", "integer"),
        ("can_item_comprobante_compra", "double precision"),
        ("imp_neto_item_comprobante_compra", "double precision"),
        ("porc_iva_item_comprobante_compra", "double precision"),
        ("imp_int_item_comprobante_compra", "double precision"),
        ("porc_desc_item_comprobante_compra", "double precision"),
        ("fk_id_cuenta_imputacion_item_compra", "integer"),
        ("fk_id_tipo_no_gravado_compra", "integer"), ("fk_id_plan_de_cuenta", "integer"),
    ]),
    ("conceptos_compras", "id_concepto_compra", [
        ("id_concepto_compra", "integer"), ("des_concepto_compra", "text"),
    ]),
    ("conceptos_x_items_comprobantes_compras", "id_concepto_x_item_comprobante_compra", [
        ("id_concepto_x_item_comprobante_compra", "integer"),
        ("fk_id_item_comprobante_compra", "integer"), ("fk_id_concepto_compra", "integer"),
    ]),
    ("productos_x_items_comprobantes_compras", "id_producto_x_item_comprobante_compra", [
        ("id_producto_x_item_comprobante_compra", "integer"), ("fk_id_producto", "integer"),
        ("fk_id_item_comprobante_compra", "integer"),
    ]),
    ("productos", "id_producto", [
        ("id_producto", "integer"), ("cod_producto", "text"), ("des_producto", "text"),
        ("costo_producto", "double precision"), ("fk_id_producto_rubro", "integer"),
        ("fk_id_producto_unidad_de_medida", "integer"), ("fk_id_moneda", "integer"),
        ("porc_iva_producto", "double precision"), ("discontinuado", "boolean"),
        ("fec_ult_producto_costo", "timestamp"), ("fk_id_proveedor", "integer"),
    ]),
    ("productos_costos", "id_producto_costo", [
        ("id_producto_costo", "integer"), ("fk_id_producto", "integer"),
        ("fec_producto_costo", "timestamp"), ("imp_producto_costo", "double precision"),
    ]),
    ("productos_rubros", "id_producto_rubro", [
        ("id_producto_rubro", "integer"), ("des_producto_rubro", "text"),
    ]),
    ("productos_unidades_de_medidas", "id_producto_unidad_de_medida", [
        ("id_producto_unidad_de_medida", "integer"), ("cod_producto_unidad_de_medida", "text"),
        ("des_producto_unidad_de_medida", "text"),
    ]),
]

ARCHIVO_CONFIG = ".env.garro-sync"
LOG_LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "garro_sync.log")
# Clave fija del candado: dos corridas de garro-sync nunca la toman a la vez.
CLAVE_LOCK = 329001
# Copia diaria y manual esperan el candado hasta ~15 s (el sondeo de 5 min no debe ganarles); el modo pedido no espera.
ESPERA_CANDADO_S = 15

# CA raíz oficial de Supabase (Supabase Root 2021 CA, vence 2031-04-26). Viaja en el repo, al lado del .py.
# SHA-256: 80:70:25:AD:50:D4:ED:21:9D:2C:9C:7D:29:9C:00:4F:82:4E:B0:0C:F7:F6:5A:FE:F6:07:D0:7B:72:E6:CA:FA
CA_SUPABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supabase-ca-2021.crt")

ORIGEN = "Gestión Integral (el servidor de Garro)"
DESTINO = "Supabase"


class ErrorSync(Exception):
    """Error con mensaje ya redactado para una persona no técnica."""


class ErrorGuardia(ErrorSync):
    """La guardia de la foto rechazó la lectura: reintentar no sirve, el pedido falla en forma definitiva."""


class SinTrabajo(Exception):
    """Modo --si-hay-pedido: no hay pedido, o hay otra copia corriendo. Se sale con 0 sin registrar nada."""


class ErrorIndeterminado(ErrorSync):
    """El COMMIT falló: no se puede saber si la copia nueva quedó publicada o no.

    sync_corridas.estado solo admite 'ok'/'error' (CHECK de la 329), así que se registra como
    'error' con el prefijo INDETERMINADO y el mismo código de corrida que una posible fila 'ok'.
    """


class Corrida:
    """Estado de la corrida en curso: qué etapa va, cuándo empezó y cuántas filas leyó."""

    def __init__(self) -> None:
        self.id = str(uuid.uuid4())
        self.etapa = "config"
        self.inicio = datetime.now(timezone.utc)
        self.filas: dict[str, int] = {}
        self.pedido_tomado = False
        self.pedido_id: int | None = None

    @property
    def origen(self) -> str:
        """Valor de la columna `origen` de sync_corridas: PC + código de corrida (para cruzar filas)."""
        return f"{socket.gethostname()} [corrida {self.id}]"


# ---------------------------------------------------------------- salida y log

MAX_LOG_BYTES = 5 * 1024 * 1024
SEGUNDOS_ENTRE_TRAZAS = 3600


def rotar_log() -> None:
    """Pasado 5 MB, el log pasa a garro_sync.log.1 (se pisa el anterior) y arranca uno nuevo."""
    try:
        if os.path.getsize(LOG_LOCAL) > MAX_LOG_BYTES:
            os.replace(LOG_LOCAL, LOG_LOCAL + ".1")
    except OSError:
        pass


def escribir_traza(limitar: bool = False) -> None:
    """Deja el traceback completo en el log. Con `limitar` (modo de 5 min) a lo sumo una vez por hora."""
    marca = LOG_LOCAL + ".traza"
    if limitar:
        try:
            if time.time() - os.path.getmtime(marca) < SEGUNDOS_ENTRE_TRAZAS:
                return
        except OSError:
            pass
        try:
            with open(marca, "w", encoding="utf-8") as f:
                f.write("")
        except OSError:
            pass
    escribir_log(traceback.format_exc())


def escribir_log(texto: str) -> None:
    rotar_log()
    try:
        with open(LOG_LOCAL, "a", encoding="utf-8") as f:
            f.write(texto if texto.endswith("\n") else texto + "\n")
    except OSError:
        pass


def decir(texto: str = "") -> None:
    """Muestra el texto en pantalla y lo deja también en garro_sync.log."""
    print(texto, flush=True)
    escribir_log(texto)


def ddl() -> str:
    """SQL de las tablas espejo, para la migración 329."""
    partes = []
    for nombre, pk, columnas in TABLAS:
        cols = ",\n  ".join(f"{c} {t}" + (" PRIMARY KEY" if c == pk else "") for c, t in columnas)
        partes.append(f"CREATE TABLE garro.{nombre} (\n  {cols}\n);")
    return "\n\n".join(partes)


# ---------------------------------------------------------------- errores

def traducir_error(exc: BaseException, etapa: str) -> str:
    """Convierte un error técnico en un mensaje en español para quien opera la copia."""
    if isinstance(exc, ErrorSync):
        return str(exc)

    donde = ORIGEN if etapa == "origen" else DESTINO
    quien = "Garro" if etapa == "origen" else "Supabase"
    texto = str(exc)
    bajo = texto.lower()
    pgcode = getattr(exc, "pgcode", None)

    if isinstance(exc, (UnicodeDecodeError, UnicodeEncodeError)) or "invalid byte sequence" in bajo \
            or "client_encoding" in bajo or "unknown encoding" in bajo or "codec can't" in bajo:
        return ("Hay caracteres (acentos, eñes) que no se pueden leer con la codificación actual. "
                "Agregá la línea GARRO_ENCODING=WIN1252 en el archivo .env.garro-sync "
                "(si ya está, probá GARRO_ENCODING=UTF8) y volvé a correr.")
    if etapa != "origen" and ("certificate verify failed" in bajo or "ssl error" in bajo
                              or "server certificate" in bajo or "root certificate" in bajo
                              or "certificate" in bajo and "verif" in bajo):
        return ("No se pudo verificar la identidad de Supabase (certificado SSL). No se envió ninguna contraseña. "
                "Revisá que el archivo supabase-ca-2021.crt esté al lado de garro_sync.py y que la fecha de la PC sea "
                "correcta; si hay un antivirus o proxy que intercepta conexiones, desactivá esa inspección. "
                "Si se repite, avisá a Alexander.")
    if "authentication method requirement" in bajo or "require_auth" in bajo:
        return ("Supabase no ofreció el método de autenticación seguro esperado (scram-sha-256). "
                "No se envió la contraseña. Avisá a Alexander.")
    if "password authentication failed" in bajo or "authentication failed" in bajo \
            or "tenant or user not found" in bajo or "no pg_hba.conf entry" in bajo:
        if etapa == "origen":
            return ("Gestión Integral (Garro) rechazó el usuario o la contraseña. Revisá GARRO_USUARIO y "
                    "GARRO_PASSWORD en .env.garro-sync.")
        return ("Supabase rechazó el usuario o la contraseña. Revisá SUPABASE_USUARIO y "
                "SUPABASE_PASSWORD en .env.garro-sync (la contraseña de garro_sync).")
    if pgcode == "57014" or "statement timeout" in bajo or "timeout expired" in bajo \
            or "timed out" in bajo or "idle-in-transaction" in bajo or "idle_in_transaction" in bajo:
        extra = " Revisá que la VPN Radmin esté conectada." if etapa == "origen" else ""
        return (f"La conexión con {donde} tardó demasiado y se cortó por tiempo (timeout).{extra} "
                "Probá de nuevo en unos minutos; si se repite, avisá.")
    if "could not connect" in bajo or "connection refused" in bajo or "could not translate host" in bajo \
            or "name or service not known" in bajo or "getaddrinfo" in bajo or "network is unreachable" in bajo \
            or "no route to host" in bajo or "is the server running" in bajo:
        if etapa == "origen":
            return ("No se puede conectar con Gestión Integral (Garro). Revisá que la VPN Radmin esté "
                    "conectada, que GARRO_HOST tenga la IP correcta y que el servidor esté encendido.")
        return ("No se puede conectar con Supabase. Revisá la conexión a internet y que SUPABASE_HOST y "
                "SUPABASE_PUERTO estén bien en .env.garro-sync.")
    if "server closed the connection" in bajo or "connection already closed" in bajo \
            or "ssl syscall" in bajo or "connection reset" in bajo or "terminating connection" in bajo \
            or "connection has been closed" in bajo:
        return (f"Se cortó la conexión con {donde} antes de terminar la copia (sin llegar a confirmarla). "
                "Revisá la VPN/internet y volvé a correr.")
    if pgcode == "42501" or "permission denied" in bajo:
        if etapa == "origen":
            return "El usuario de Garro no tiene permiso para leer alguna tabla. Avisá a quien administra el sistema."
        return ("El rol garro_sync no tiene permiso sobre alguna tabla de Supabase (¿falta una migración?). "
                "Avisá a Alexander.")
    if pgcode in ("42P01", "42703") or "does not exist" in bajo:
        if etapa == "origen":
            return ("Gestión Integral no tiene alguna tabla o columna que el programa espera "
                    f"(¿cambió el sistema de Garro?). Detalle: {texto.splitlines()[0] if texto else ''}")
        return ("Supabase no tiene alguna tabla o columna del espejo (¿falta aplicar una migración?). "
                f"Detalle: {texto.splitlines()[0] if texto else ''}")
    primera = texto.splitlines()[0] if texto else ""
    return (f"Error inesperado ({quien}): {type(exc).__name__}: {primera}. "
            "El detalle técnico está en garro_sync.log.")


def insertar_corrida(cur, corrida: Corrida, estado: str, error: str | None = None) -> str:
    """Registra la corrida en sync_corridas y devuelve 'insertada', 'ya_existia_ok' o 'ya_existia_error'.

    Usa la función garro.registrar_corrida (mig 334): corre como dueño, hace
    INSERT ... ON CONFLICT (corrida_id) DO NOTHING y dice si la corrida ya estaba, así que solo el
    choque de corrida_id cuenta como «ya existía»; un choque de la PK u otro error se propaga.
    Si la función no existe todavía (42883: la 334 sin aplicar), cae a un INSERT plano sin corrida_id
    (el código de corrida igual va dentro de `origen`).
    """
    params = (corrida.id, estado, corrida.origen, json.dumps(corrida.filas), error[:2000] if error else None,
              corrida.inicio)
    cur.execute("SAVEPOINT sync_corrida")
    try:
        cur.execute("SELECT garro.registrar_corrida(%s::uuid, %s::text, %s::text, %s::jsonb, %s::text, %s::timestamptz)",
                    params)
        resultado = cur.fetchone()[0]
    except Exception as e:
        if getattr(e, "pgcode", None) != "42883":
            raise
        cur.execute("ROLLBACK TO SAVEPOINT sync_corrida")
        cur.execute(
            "INSERT INTO garro.sync_corridas (iniciado_at, terminado_at, origen, filas, estado, error)"
            " VALUES (%s, now(), %s, %s::jsonb, %s, %s)",
            (corrida.inicio, corrida.origen, params[3], estado, params[4]),
        )
        resultado = "insertada"
    cur.execute("RELEASE SAVEPOINT sync_corrida")
    return resultado


def registrar_error(cfg: dict[str, str], corrida: Corrida, mensaje: str) -> bool:
    """Deja la fila estado='error' en sync_corridas con una conexión NUEVA.

    No reutiliza la conexión de la copia: puede estar rota o con la transacción abortada.
    Si tampoco se puede, avisa y el error queda solo en garro_sync.log.
    Devuelve True si la corrida ya figuraba como 'ok' (el commit sí había llegado).
    """
    try:
        con = conectar_destino(cfg)
        try:
            with con.cursor() as cur:
                resultado = insertar_corrida(cur, corrida, "error", mensaje)
            con.commit()
        finally:
            con.close()
        if resultado == "ya_existia_ok":
            decir("La corrida ya figuraba como 'ok' en Supabase: el commit sí había llegado y la copia "
                  "quedó publicada. No se agregó ninguna fila de error.")
            return True
        if resultado == "ya_existia_error":
            decir("Esta corrida ya tenía un error registrado en Supabase.")
            return False
        decir("El error quedó registrado en Supabase (garro.sync_corridas).")
        return False
    except Exception as e:
        escribir_log(traceback.format_exc())
        decir("ATENCIÓN: este error NO pudo registrarse en Supabase "
              f"({traducir_error(e, 'destino')}). Queda solo en garro_sync.log, en la carpeta del programa.")
        return False


# ---------------------------------------------------------------- conexiones

def leer_archivo(ruta: str) -> dict[str, str]:
    """Lee el .env sin validar nada: una configuración parcial sirve para registrar el error."""
    if not os.path.exists(ruta):
        raise ErrorSync(f"Falta el archivo de configuración {ARCHIVO_CONFIG} (se esperaba en {ruta}). "
                        "Ver README.md, paso «Configuración».")
    cfg: dict[str, str] = {}
    with open(ruta, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            k, v = linea.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def validar_origen(cfg: dict[str, str]) -> None:
    faltan = [k for k in ("GARRO_HOST", "GARRO_PUERTO", "GARRO_BASE", "GARRO_USUARIO",
                          "GARRO_PASSWORD") if not cfg.get(k)]
    if faltan:
        raise ErrorSync(f"Faltan datos de Garro en {ARCHIVO_CONFIG}: {', '.join(faltan)}.")


def conectar_origen(cfg: dict[str, str]):
    import psycopg2
    validar_origen(cfg)
    con = psycopg2.connect(
        host=cfg["GARRO_HOST"], port=int(cfg["GARRO_PUERTO"]), dbname=cfg["GARRO_BASE"],
        user=cfg["GARRO_USUARIO"], password=cfg["GARRO_PASSWORD"],
        connect_timeout=20, application_name="garro_sync (lectura)",
    )
    # Doble seguro: aunque el usuario tuviera permisos de más, esta sesión no puede escribir.
    con.set_session(readonly=True, autocommit=False)
    # Los acentos: una base vieja suele estar en SQL_ASCII con texto de Windows adentro, y
    # leerla como ASCII rompe los nombres. GARRO_ENCODING fuerza uno si hace falta.
    with con.cursor() as cur:
        cur.execute("SHOW server_encoding")
        server_encoding = cur.fetchone()[0]
    con.rollback()
    encoding = cfg.get("GARRO_ENCODING") or ("WIN1252" if server_encoding == "SQL_ASCII" else server_encoding)
    con.set_client_encoding(encoding)
    decir(f"Origen: {con.server_version // 10000}.{con.server_version // 100 % 100} · server_encoding {server_encoding} · leyendo como {encoding}")
    return con


def conectar_destino(cfg: dict[str, str]):
    import psycopg2
    faltan = [k for k in ("SUPABASE_HOST", "SUPABASE_PUERTO", "SUPABASE_USUARIO",
                          "SUPABASE_PASSWORD") if not cfg.get(k)]
    if faltan:
        raise ErrorSync(f"Faltan datos de Supabase en {ARCHIVO_CONFIG}: {', '.join(faltan)}.")
    if not os.path.exists(CA_SUPABASE):
        raise ErrorSync(f"Falta el certificado de Supabase ({os.path.basename(CA_SUPABASE)}) al lado de garro_sync.py. "
                        "Volvé a correr el comando de instalación del README.")
    # verify-full: valida la cadena y el nombre del servidor; require_auth: solo SCRAM (nunca contraseña en claro).
    # Sin channel_binding: el pooler de Supabase lo rechaza.
    return psycopg2.connect(
        host=cfg["SUPABASE_HOST"], port=int(cfg["SUPABASE_PUERTO"]), dbname="postgres",
        user=cfg["SUPABASE_USUARIO"], password=cfg["SUPABASE_PASSWORD"],
        sslmode="verify-full", sslrootcert=CA_SUPABASE, require_auth="scram-sha-256",
        connect_timeout=30, application_name="garro_sync",
    )


def leer_tabla(cur, nombre: str, columnas: list[tuple[str, str]]) -> io.StringIO:
    """Lee la tabla del origen y la devuelve como CSV en memoria (para COPY)."""
    lista = ", ".join(c for c, _ in columnas)
    buf = io.StringIO()
    cur.copy_expert(f"COPY (SELECT {lista} FROM public.{nombre}) TO STDOUT WITH CSV", buf)
    buf.seek(0)
    return buf


def contar_filas(buf: io.StringIO) -> int:
    n = sum(1 for _ in csv.reader(buf))
    buf.seek(0)
    return n


def leer_origen(cfg: dict[str, str], corrida: Corrida, latido=None) -> dict[str, io.StringIO]:
    """Lee todas las tablas del origen (una sola foto) y devuelve el CSV de cada una."""
    corrida.etapa = "origen"
    origen = conectar_origen(cfg)
    datos: dict[str, io.StringIO] = {}
    try:
        with origen.cursor() as cur:
            # Una sola transacción REPEATABLE READ: todas las tablas salen de la misma foto.
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            for nombre, _, columnas in TABLAS:
                datos[nombre] = leer_tabla(cur, nombre, columnas)
                corrida.filas[nombre] = contar_filas(datos[nombre])
                if latido:
                    latido()
        origen.rollback()
    finally:
        origen.close()
    return datos


def mostrar_filas(corrida: Corrida) -> int:
    total = sum(corrida.filas.values())
    decir(f"Origen leído: {total} filas en {len(corrida.filas)} tablas")
    for nombre, n in corrida.filas.items():
        decir(f"  {nombre}: {n}")
    return total


# ---------------------------------------------------------------- guardia de la foto

# Sin datos en estas tablas la foto está rota: la copia nunca se publica vacía.
OBLIGATORIAS = (
    "tipos_comprobantes_ventas", "tipos_comprobantes_compras", "tipos_iva", "monedas",
    "clientes", "proveedores", "comprobantes_ventas", "items_comprobantes_ventas",
    "comprobantes_compras", "items_comprobantes_compras", "productos",
)
# Comprobantes e ítems casi nunca bajan (una anulación no borra filas): tolerancia chica.
ESTRICTAS = ("comprobantes_ventas", "items_comprobantes_ventas",
             "comprobantes_compras", "items_comprobantes_compras")
MAX_CAIDA_ESTRICTA = 0.02
MAX_CAIDA_GENERAL = 0.20
# Una baja de hasta 2 filas nunca alerta por porcentaje (una lista de 4 que pasa a 3 es normal).
TOLERANCIA_FILAS = 2


def validar_foto(nuevas: dict[str, int], previas: dict[str, int] | None, forzar: bool) -> None:
    """Rechaza una foto sospechosa ANTES de vaciar el espejo. `previas` = filas de la última copia ok."""
    if forzar:
        return
    vacias = [t for t in OBLIGATORIAS if int(nuevas.get(t, 0)) <= 0]
    if vacias:
        raise ErrorGuardia(
            "La lectura de Gestión Integral vino vacía en tablas que nunca deberían estarlo "
            f"({', '.join(vacias)}). No se tocó la copia que ya está en Supabase. Revisá que el servidor de "
            "Garro esté bien y que el usuario de lectura vea los datos; si es un cambio real y esperado, "
            "avisá a Alexander.")
    if not previas:
        return
    problemas: list[str] = []
    for tabla, antes in previas.items():
        antes = int(antes or 0)
        ahora = int(nuevas.get(tabla, 0))
        if antes <= 0:
            continue
        if ahora == 0:
            problemas.append(f"{tabla}: pasó de {antes} a 0")
            continue
        tope = MAX_CAIDA_ESTRICTA if tabla in ESTRICTAS else MAX_CAIDA_GENERAL
        caida = (antes - ahora) / antes
        if antes - ahora > TOLERANCIA_FILAS and caida > tope:
            problemas.append(f"{tabla}: pasó de {antes} a {ahora} (-{caida:.0%}, el tope es {tope:.0%})")
    if problemas:
        raise ErrorGuardia(
            "La foto nueva tiene muchas menos filas que la última copia correcta, así que no se publicó "
            "y sigue la anterior. " + "; ".join(problemas) + ". Si la baja es real y esperada, avisá a "
            "Alexander (se puede forzar a mano con --forzar).")


def leer_previas(cur) -> dict[str, int] | None:
    """Filas de la última corrida ok (garro.ultimos_conteos_ok, mig 349). None si no hay o no existe la función."""
    cur.execute("SAVEPOINT conteos_previos")
    try:
        cur.execute("SELECT garro.ultimos_conteos_ok()")
        fila = cur.fetchone()
    except Exception as e:
        if getattr(e, "pgcode", None) != "42883":
            raise
        cur.execute("ROLLBACK TO SAVEPOINT conteos_previos")
        fila = None
    cur.execute("RELEASE SAVEPOINT conteos_previos")
    valor = fila[0] if fila else None
    if isinstance(valor, str):
        valor = json.loads(valor)
    return {k: int(v) for k, v in valor.items()} if isinstance(valor, dict) else None


# ---------------------------------------------------------------- corridas

def correr_prueba(cfg: dict[str, str], corrida: Corrida) -> None:
    datos = leer_origen(cfg, corrida)
    mostrar_filas(corrida)
    # Muestra para revisar acentos a ojo («Nota Débito» tiene que verse bien).
    muestra = datos["tipos_comprobantes_ventas"].getvalue().splitlines()[:8]
    decir("Muestra de tipos de comprobante de venta:")
    for linea in muestra:
        decir(f"  {linea}")
    decir("Modo prueba: no se escribió nada en Supabase.")


def tomar_candado(cur, espera_s: int) -> bool:
    """pg_try_advisory_xact_lock, reintentando cada 1 s hasta `espera_s` segundos."""
    for intento in range(espera_s + 1):
        cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (CLAVE_LOCK,))
        if cur.fetchone()[0]:
            return True
        if intento < espera_s:
            time.sleep(1)
    return False


def tomar_pedido(cur, obligatorio: bool) -> int | None:
    """garro.tomar_pedido_sync(): id del pedido pendiente tomado, o None. Sin la mig 349: error si es obligatorio."""
    cur.execute("SAVEPOINT tomar_pedido")
    try:
        cur.execute("SELECT garro.tomar_pedido_sync()")
        fila = cur.fetchone()
    except Exception as e:
        if getattr(e, "pgcode", None) != "42883":
            raise
        if obligatorio:
            raise ErrorSync("Falta la migración 349 en Supabase (pedidos de actualización). Avisá a Alexander.") from e
        cur.execute("ROLLBACK TO SAVEPOINT tomar_pedido")
        fila = None
    cur.execute("RELEASE SAVEPOINT tomar_pedido")
    return fila[0] if fila else None


def correr_copia(cfg: dict[str, str], corrida: Corrida, forzar: bool = False, solo_si_pedido: bool = False) -> None:
    corrida.etapa = "destino"
    destino = conectar_destino(cfg)
    try:
        with destino.cursor() as cur:
            # Candado en la transacción de destino: si otra copia está en curso, esta no sigue.
            # Se toma ANTES de leer el origen, así la foto que se publica es siempre la última leída.
            if not tomar_candado(cur, 0 if solo_si_pedido else ESPERA_CANDADO_S):
                if solo_si_pedido:
                    raise SinTrabajo()  # otra copia en curso: el pedido sigue pendiente, sin error
                raise ErrorSync("Ya hay otra copia corriendo (en esta u otra PC). Esperá a que termine "
                                "y volvé a correr.")

            # TODA copia (diaria, manual o por pedido) consume el pedido pendiente, en esta misma transacción:
            # si falla, el rollback lo devuelve a «pendiente». Así no queda una segunda copia redundante.
            pedido_id = tomar_pedido(cur, obligatorio=solo_si_pedido)
            if solo_si_pedido:
                if pedido_id is None:
                    raise SinTrabajo()
                corrida.pedido_tomado = True
                corrida.pedido_id = pedido_id
                decir(f"===== {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (pedido de actualización) =====")
            elif pedido_id is not None:
                decir("Esta copia también atiende el pedido de «Actualizar ahora» pendiente.")

            def latido() -> None:
                # El rol de Supabase corta las transacciones inactivas a los 120 s: un SELECT 1
                # entre tabla y tabla evita que la lectura lenta del origen lo dispare.
                corrida.etapa = "destino"
                cur.execute("SELECT 1")
                corrida.etapa = "origen"

            datos = leer_origen(cfg, corrida, latido)
            total = mostrar_filas(corrida)

            # Guardia: se valida la foto contra la última copia ok ANTES de vaciar nada.
            corrida.etapa = "destino"
            validar_foto(corrida.filas, leer_previas(cur), forzar)

            corrida.etapa = "copia"
            cur.execute("SET LOCAL statement_timeout = '300s'")
            nombres = ", ".join(f"garro.{n}" for n, _, _ in TABLAS)
            cur.execute(f"TRUNCATE {nombres}")
            for nombre, _, columnas in TABLAS:
                lista = ", ".join(c for c, _ in columnas)
                cur.copy_expert(f"COPY garro.{nombre} ({lista}) FROM STDIN WITH CSV", datos[nombre])
            if insertar_corrida(cur, corrida, "ok") != "insertada":
                raise ErrorSync("La corrida ya figuraba registrada en Supabase con este mismo código (no debería pasar). "
                                "Avisá a Alexander.")
        try:
            destino.commit()
        except Exception as e:
            raise ErrorIndeterminado(
                "INDETERMINADO: falló la confirmación (commit) y la copia pudo haberse publicado. "
                f"Revisá garro.sync_corridas por la corrida {corrida.id}: si hay una fila 'ok' con ese "
                "código, la copia quedó publicada; si no, sigue la anterior. "
                f"Motivo: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}") from e
        decir(f"Copia completa en Supabase: {total} filas.")
    except ErrorIndeterminado:
        raise
    except Exception:
        # La transacción entera se deshace: en Supabase queda la copia anterior.
        try:
            destino.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            destino.close()
        except Exception:
            pass


def latido_agente(cfg: dict[str, str]) -> bool:
    """Transacción corta: anota que esta PC está viva (garro.latido_agente, mig 349) y cierra la conexión.

    Devuelve True si hay un pedido pendiente. Sin pedido, el sondeo termina acá, sin tomar el candado.
    """
    destino = conectar_destino(cfg)
    try:
        with destino.cursor() as cur:
            try:
                cur.execute("SELECT garro.latido_agente(%s)", (socket.gethostname(),))
            except Exception as e:
                if getattr(e, "pgcode", None) == "42883":
                    raise ErrorSync("Falta la migración 349 en Supabase (pedidos de actualización). "
                                    "Avisá a Alexander.") from e
                raise
            hay_pedido = bool(cur.fetchone()[0])
        destino.commit()
        return hay_pedido
    finally:
        try:
            destino.close()
        except Exception:
            pass


def registrar_fallo_pedido(cfg: dict[str, str], pedido_id: int, definitivo: bool,
                           mensaje: str) -> tuple[int, str] | None:
    """garro.fallo_pedido en su propia transacción corta. Devuelve (intentos, estado) o None si no se pudo."""
    try:
        con = conectar_destino(cfg)
        try:
            with con.cursor() as cur:
                cur.execute("SELECT * FROM garro.fallo_pedido(%s, %s, %s)", (pedido_id, definitivo, mensaje))
                fila = cur.fetchone()
            con.commit()
        finally:
            try:
                con.close()
            except Exception:
                pass
        return (int(fila[0]), str(fila[1])) if fila else None
    except Exception:
        escribir_traza()
        return None


def correr_prueba_destino(cfg: dict[str, str]) -> bool:
    """Conecta a Supabase con la configuración segura y hace una lectura. No escribe nada.

    Devuelve True si la migración 349 está aplicada (si no, solo comprueba la conexión con SELECT 1).
    """
    destino = conectar_destino(cfg)
    try:
        with destino.cursor() as cur:
            cur.execute("SAVEPOINT prueba_destino")
            try:
                cur.execute("SELECT garro.ultimos_conteos_ok()")
                cur.fetchone()
                con_349 = True
            except Exception as e:
                if getattr(e, "pgcode", None) != "42883":
                    raise
                cur.execute("ROLLBACK TO SAVEPOINT prueba_destino")
                cur.execute("SELECT 1")
                con_349 = False
            cur.execute("RELEASE SAVEPOINT prueba_destino")
        destino.rollback()
        return con_349
    finally:
        try:
            destino.close()
        except Exception:
            pass


def correr_pedido(config: str) -> int:
    """Modo de la tarea de cada 5 minutos: latido y, si hay un pedido, copia normal con la guardia."""
    corrida = Corrida()
    cfg: dict[str, str] | None = None
    marca = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        cfg = leer_archivo(config)
        if not latido_agente(cfg):
            return 0  # sin pedido: ni candado ni otra transacción
        validar_origen(cfg)
        correr_copia(cfg, corrida, solo_si_pedido=True)
        return 0
    except SinTrabajo:
        return 0
    except Exception as e:
        mensaje = traducir_error(e, corrida.etapa)
        if not corrida.pedido_tomado or cfg is None or corrida.pedido_id is None:
            # Antes de tomar el pedido (red caída, etc.): una sola línea corta, traceback a lo sumo 1 vez por hora.
            escribir_traza(limitar=True)
            decir(f"{marca} (pedido) ERROR: {mensaje}")
            return 1
        # Falla real de la copia con el pedido tomado: traceback siempre. Ya hubo rollback (el pedido volvió a
        # «pendiente»). Se cuenta el intento en una transacción corta; la guardia es definitiva, lo demás se reintenta.
        escribir_traza()
        decir(f"ERROR: {mensaje}")
        resultado = registrar_fallo_pedido(cfg, corrida.pedido_id, isinstance(e, ErrorGuardia), mensaje)
        if resultado is not None:
            intentos, estado = resultado
            if estado != "fallido" and intentos > 1:
                decir(f"El pedido sigue pendiente (intento {intentos} de 3); se reintenta en 5 minutos.")
                return 1
            if estado == "fallido":
                decir("El pedido de actualización quedó como FALLIDO (se puede pedir otro desde el sistema).")
        # Primer intento fallido, pedido fallido o intento no contabilizable: se registra en sync_corridas.
        return 0 if registrar_error(cfg, corrida, mensaje) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Copia Gestión Integral → Supabase (schema garro)")
    ap.add_argument("--prueba", action="store_true", help="solo lee el origen y cuenta filas")
    ap.add_argument("--si-hay-pedido", action="store_true",
                    help="copia solo si hay un pedido de «Actualizar ahora» (para la tarea de cada 5 minutos)")
    ap.add_argument("--prueba-destino", action="store_true",
                    help="prueba la conexión segura y la contraseña de Supabase (no escribe nada)")
    ap.add_argument("--forzar", action="store_true",
                    help="saltea la guardia de la foto (solo uso manual; nunca en tareas programadas)")
    ap.add_argument("--ddl", action="store_true", help="imprime el SQL de las tablas espejo")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ARCHIVO_CONFIG))
    args = ap.parse_args(argv)
    if args.forzar and (args.si_hay_pedido or args.prueba or args.prueba_destino):
        ap.error("--forzar no se combina con --si-hay-pedido, --prueba ni --prueba-destino")
    if args.prueba_destino and (args.si_hay_pedido or args.prueba):
        ap.error("--prueba-destino va solo")

    if args.ddl:
        print(ddl())
        return 0

    try:
        sys.stdout.reconfigure(errors="replace")  # consolas de Windows sin UTF-8
    except (AttributeError, OSError):
        pass

    if args.si_hay_pedido:
        return correr_pedido(args.config)

    if args.prueba_destino:
        decir(f"===== {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (prueba de destino) =====")
        try:
            con_349 = correr_prueba_destino(leer_archivo(args.config))
        except Exception as e:
            escribir_traza()
            decir(f"ERROR: {traducir_error(e, 'destino')}")
            return 1
        decir("Supabase: conexión segura y contraseña correctas. No se escribió nada.")
        if not con_349:
            decir("AVISO: falta aplicar la migración 349 (pedidos y guardia de la foto). Avisá a Alexander.")
        return 0

    corrida = Corrida()
    cfg: dict[str, str] | None = None
    marca = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    decir(f"===== {marca}{' (prueba)' if args.prueba else ''}{' (forzada)' if args.forzar else ''} =====")
    try:
        cfg = leer_archivo(args.config)
        validar_origen(cfg)
        if args.prueba:
            correr_prueba(cfg, corrida)
        else:
            correr_copia(cfg, corrida, forzar=args.forzar)
        return 0
    except Exception as e:
        escribir_traza()
        mensaje = traducir_error(e, corrida.etapa)
        decir(f"ERROR: {mensaje}")
        if args.prueba:
            decir("(Modo prueba: nada se registra en Supabase.)")
        elif cfg is None:
            decir("Este error no se registró en Supabase porque no hay archivo de configuración. Queda en garro_sync.log.")
        else:
            if registrar_error(cfg, corrida, mensaje):
                return 0
        return 1


if __name__ == "__main__":
    sys.exit(main())
