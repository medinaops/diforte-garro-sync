"""Carga de comprobantes de compra en Gestión Integral: planificador.

Decide QUÉ filas habría que escribir para un comprobante aprobado, igual que las escribe el
programa cuando una persona lo carga a mano. Este módulo es puro: no abre conexiones ni escribe
nada. Lo usan el escritor (garro_escritor.py) y la comparación en seco contra las cargas reales.

Regla general: lo que no está confirmado contra cargas reales NO se carga. En ese caso se levanta
`NoCargable` con el motivo y el comprobante queda para cargar a mano.

Tipos habilitados: Factura A y Remito X. Los renglones van siempre como concepto (texto), no como
producto.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

VERSION_RECETA = 1

# Columnas que el escritor espera encontrar en cada tabla que toca. Si el programa cambia la
# estructura (una actualización que agrega o saca columnas), el escritor frena hasta revisar la receta.
COLUMNAS = {
    "comprobantes_compras": (
        "id_comprobante_compra", "fec_comprobante_compra", "fec_contab_comprobante_compra",
        "nro_suc_comprobante_compra", "nro_comprobante_compra", "nro_remito_comprobante_compra",
        "obs_comprobante_compra", "estado_comprobante_compra", "fk_id_tipo_comprobante_compra",
        "fk_id_proveedor", "fk_id_condicion_compra", "porc_desc_comprobante_compra", "fk_id_operario",
        "fk_id_producto_deposito", "fec_venc_comprobante_compra", "porcentaje_descuento_pago_termino",
        "stock", "genera_cta_cte", "fk_id_moneda", "imp_moneda", "nro_proyecto",
        "controlado", "fec_control", "fk_id_operario_control",
    ),
    "items_comprobantes_compras": (
        "id_item_comprobante_compra", "fk_id_comprobante_compra", "can_item_comprobante_compra",
        "imp_neto_item_comprobante_compra", "porc_iva_item_comprobante_compra",
        "imp_int_item_comprobante_compra", "porc_desc_item_comprobante_compra",
        "fk_id_cuenta_imputacion_item_compra", "fk_id_tipo_no_gravado_compra", "fk_id_plan_de_cuenta",
    ),
    "conceptos_compras": ("id_concepto_compra", "des_concepto_compra"),
    "conceptos_x_items_comprobantes_compras": (
        "id_concepto_x_item_comprobante_compra", "fk_id_item_comprobante_compra", "fk_id_concepto_compra",
    ),
    "regimenes_especiales_x_comprobantes_compras": (
        "id_regimen_especial_x_comprobante_compra", "fk_id_comprobante_compra", "fk_id_regimen_especial",
        "imp_regimen_especial_x_comprobante_compra",
    ),
    "asientos_contables": (
        "id_asiento_contable", "fec_asiento_contable", "nro_grupo_asiento_contable", "des_asiento_contable",
        "fk_id_operario", "fk_id_cheque_propio", "fk_id_asiento_contable", "fk_id_recibo",
        "fk_id_comprobante_venta", "visado", "manual", "nro_orden_manual_asiento_contable", "fk_id_pago",
        "es_cierre_resultado", "es_cierre_patrimonial", "fk_id_ajuste_cuenta_corriente_cliente",
        "fk_id_comprobante_compra", "fk_id_ajuste_cuenta_corriente_proveedor", "fk_id_movimiento_fondo",
        "fk_id_cheque", "fk_id_movimiento_cuenta_bancaria", "modulo",
        "fk_id_deposito_efectivo_a_cuenta_bancaria", "fk_id_extraccion_efectivo_de_cuenta_bancaria",
        "fk_id_movimiento_caja", "fk_id_transferencia_entre_cuenta_bancaria", "es_ctacte",
        "fk_id_plazo_fijo", "fk_id_fondo_comun_de_inversion",
    ),
    "asientos_contables_movimientos": (
        "id_asiento_contable_movimiento", "imp_asiento_contable_movimiento", "es_debe", "es_haber",
        "fk_id_plan_de_cuenta", "fk_id_asiento_contable", "obs_asiento_contable_movimiento",
    ),
    "pagos_en_comprobantes_compras": (
        "id_pago_en_comprobante_compra", "fk_id_pago", "fk_id_comprobante_compra",
        "imp_pago_en_comprobante_compra", "es_saldo_pago_anterior", "imp_neto_pago_en_comprobante_compra",
    ),
}

# Tipo de dato (information_schema) esperado de cada columna, según el catálogo del programa del 14/10/2026.
# Las tres columnas de control de `comprobantes_compras` no estaban en ese catálogo: quedan sin control de tipo.
_I, _D, _T, _V, _B = "integer", "double precision", "timestamp without time zone", "character varying", "boolean"
TIPOS = {
    "comprobantes_compras": {
        "id_comprobante_compra": _I, "fec_comprobante_compra": _T, "fec_contab_comprobante_compra": _T,
        "nro_suc_comprobante_compra": _I, "nro_comprobante_compra": _I, "nro_remito_comprobante_compra": _V,
        "obs_comprobante_compra": _V, "estado_comprobante_compra": _V, "fk_id_tipo_comprobante_compra": _I,
        "fk_id_proveedor": _I, "fk_id_condicion_compra": _I, "porc_desc_comprobante_compra": _D,
        "fk_id_operario": _I, "fk_id_producto_deposito": _I, "fec_venc_comprobante_compra": _T,
        "porcentaje_descuento_pago_termino": _D, "stock": _B, "genera_cta_cte": _B, "fk_id_moneda": _I,
        "imp_moneda": _D, "nro_proyecto": _I,
    },
    "items_comprobantes_compras": {
        "id_item_comprobante_compra": _I, "fk_id_comprobante_compra": _I, "can_item_comprobante_compra": _D,
        "imp_neto_item_comprobante_compra": _D, "porc_iva_item_comprobante_compra": _D,
        "imp_int_item_comprobante_compra": _D, "porc_desc_item_comprobante_compra": _D,
        "fk_id_cuenta_imputacion_item_compra": _I, "fk_id_tipo_no_gravado_compra": _I, "fk_id_plan_de_cuenta": _I,
    },
    "conceptos_compras": {
        "id_concepto_compra": _I, "des_concepto_compra": _V,
    },
    "conceptos_x_items_comprobantes_compras": {
        "id_concepto_x_item_comprobante_compra": _I, "fk_id_item_comprobante_compra": _I,
        "fk_id_concepto_compra": _I,
    },
    "regimenes_especiales_x_comprobantes_compras": {
        "id_regimen_especial_x_comprobante_compra": _I, "fk_id_comprobante_compra": _I, "fk_id_regimen_especial": _I,
        "imp_regimen_especial_x_comprobante_compra": _D,
    },
    "asientos_contables": {
        "id_asiento_contable": _I, "fec_asiento_contable": _T, "nro_grupo_asiento_contable": _I,
        "des_asiento_contable": _V, "fk_id_operario": _I, "fk_id_cheque_propio": _I, "fk_id_asiento_contable": _I,
        "fk_id_recibo": _I, "fk_id_comprobante_venta": _I, "visado": _B, "manual": _B,
        "nro_orden_manual_asiento_contable": _I, "fk_id_pago": _I, "es_cierre_resultado": _B,
        "es_cierre_patrimonial": _B, "fk_id_ajuste_cuenta_corriente_cliente": _I, "fk_id_comprobante_compra": _I,
        "fk_id_ajuste_cuenta_corriente_proveedor": _I, "fk_id_movimiento_fondo": _I, "fk_id_cheque": _I,
        "fk_id_movimiento_cuenta_bancaria": _I, "modulo": _V, "fk_id_deposito_efectivo_a_cuenta_bancaria": _I,
        "fk_id_extraccion_efectivo_de_cuenta_bancaria": _I, "fk_id_movimiento_caja": _I,
        "fk_id_transferencia_entre_cuenta_bancaria": _I, "es_ctacte": _B, "fk_id_plazo_fijo": _I,
        "fk_id_fondo_comun_de_inversion": _I,
    },
    "asientos_contables_movimientos": {
        "id_asiento_contable_movimiento": _I, "imp_asiento_contable_movimiento": _D, "es_debe": _B, "es_haber": _B,
        "fk_id_plan_de_cuenta": _I, "fk_id_asiento_contable": _I, "obs_asiento_contable_movimiento": _V,
    },
    "pagos_en_comprobantes_compras": {
        "id_pago_en_comprobante_compra": _I, "fk_id_pago": _I, "fk_id_comprobante_compra": _I,
        "imp_pago_en_comprobante_compra": _D, "es_saldo_pago_anterior": _B,
        "imp_neto_pago_en_comprobante_compra": _D,
    },
}

# Lo que cargó el equipo en todas las compras relevadas (143 de 143).
CONDICION_COMPRA = 2
DEPOSITO = 1
MONEDA_PESOS = 1
DIAS_VENCIMIENTO = 30

TIPO_FACTURA_A = 1
TIPO_REMITO_X = 5
TIPOS_HABILITADOS = (TIPO_FACTURA_A, TIPO_REMITO_X)
IVA_POR_TIPO = {TIPO_FACTURA_A: 21.0, TIPO_REMITO_X: 0.0}
TIPO_REGIMEN_PERCEPCION = 2

CONCEPTO_REDONDEO = "REDONDEO"
MAX_DIAS_ATRAS = 366
LARGO_TEXTO = 200
# Un pago a cuenta con un resto de hasta 1 centavo no se imputa (22 casos). Entre 1 centavo y
# 1 peso no hay ningún caso real: no se sabe qué hace el programa, así que no se carga.
RESTO_DESPRECIABLE = 0.011
RESTO_MINIMO_CONFIRMADO = 1.0
TOPE_IMPORTE = 1e12
MAX_RENGLONES = 200
MAX_PERCEPCIONES = 20
TOPE_REDONDEO_MAXIMO = 500.0


class NoCargable(Exception):
    """El comprobante no se puede cargar solo: queda para cargar a mano. `codigo` es estable."""

    def __init__(self, codigo: str, mensaje: str):
        super().__init__(mensaje)
        self.codigo = codigo
        self.mensaje = mensaje


@dataclass
class Referencias:
    """Lo que el escritor lee de Gestión Integral, dentro de su transacción, antes de planificar."""

    proveedores: list[dict]                 # los que tienen ese CUIT: {id, razon_social, activo}
    tipo: dict | None                       # {id, descripcion, es_ctacte}
    operario_id: int | None                 # el usuario propio del bot en el programa
    cuenta_proveedores: int | None          # configuración contable: cuenta de proveedores
    cuenta_credito_fiscal: int | None       # configuración contable: IVA crédito fiscal
    cuentas: dict[int, dict]                # plan de cuenta -> {activa, imputable}
    categorias: set[int]                    # categorías de renglón que existen
    regimenes: dict[int, dict]              # régimen -> {tipo, cuenta}
    duplicados: list[int]                   # comprobantes activos del mismo proveedor, tipo y número
    siguiente_grupo: int | None             # próximo número de asiento del mes de la fecha
    pagos_a_cuenta: list[dict] = field(default_factory=list)   # {id, fecha, resto}, del proveedor
    largos: dict[str, int | None] = field(default_factory=dict)  # «tabla.columna» -> largo máximo


@dataclass
class Plan:
    comprobante: dict
    items: list[dict]            # {fila, concepto}
    regimenes: list[dict]
    asiento: dict | None         # {fila, movimientos}
    imputaciones: list[dict]
    neto: float
    iva: float
    percepciones: float
    total: float
    avisos: list[str]

    def resumen(self) -> dict:
        return {
            "receta": VERSION_RECETA,
            "renglones": len(self.items),
            "neto": r2(self.neto), "iva": r2(self.iva), "percepciones": r2(self.percepciones),
            "total": r2(self.total),
            "asiento": bool(self.asiento),
            "imputaciones": [{"pago": i["fk_id_pago"], "importe": r2(i["imp_pago_en_comprobante_compra"])}
                             for i in self.imputaciones],
            "avisos": list(self.avisos),
        }


def r3(x: float) -> float:
    """Redondeo a milésimos (así guarda el programa lo imputado a un pago)."""
    return float(Decimal(repr(float(x))).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def r2(x: float) -> float:
    """Redondeo a centavos, medio hacia arriba, sobre la representación corta del número."""
    return float(Decimal(repr(float(x))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _numero(x) -> float | None:
    """Un número finito, o None. Un entero gigante (más grande que un float) también es None."""
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, (int, float)):
        try:
            f = float(x)
        except OverflowError:
            return None
        return f if math.isfinite(f) else None
    return None


def _opcional(d: dict, clave: str, defecto: float, codigo: str, mensaje: str) -> float:
    """Clave numérica opcional: ausente o null vale `defecto`; si EXISTE tiene que ser un número finito."""
    if d.get(clave) is None:
        return defecto
    n = _numero(d[clave])
    if n is None:
        raise NoCargable(codigo, mensaje)
    return n


def _entero(x) -> int | None:
    n = _numero(x)
    return int(n) if n is not None and n == int(n) else None


def limpiar_texto(texto, largo: int | None = None) -> str:
    """Texto de un renglón como lo tipea el equipo: mayúsculas, una línea, solo caracteres de Windows-1252."""
    t = re.sub(r"\s+", " ", str(texto or "")).strip().upper()
    t = t.encode("cp1252", errors="replace").decode("cp1252")
    t = "".join(c for c in t if c.isprintable())
    tope = min(LARGO_TEXTO, largo) if largo else LARGO_TEXTO
    return t[:tope].rstrip()


def solo_digitos(texto) -> str:
    return re.sub(r"\D", "", str(texto or ""))


def nombre_clave(texto) -> str:
    """Nombre de un proveedor para compararlo: sin acentos, sin puntuación, en mayúsculas."""
    t = unicodedata.normalize("NFD", str(texto or "")).encode("ascii", "ignore").decode("ascii").upper()
    return re.sub(r"[^A-Z0-9]+", " ", t).strip()


def proveedor_garro_id(propuesta: dict) -> int | None:
    """Id de Garro que la aprobación fijó (`proveedor_garro.id`), o None si falta o no es un entero positivo."""
    pg = propuesta.get("proveedor_garro")
    pid = _entero(pg.get("id")) if isinstance(pg, dict) else None
    return pid if pid is not None and pid > 0 else None


def proveedores_de(propuesta: dict, todos: list[dict]) -> list[dict]:
    """Los proveedores del programa que corresponden al comprobante. `todos`: {id, cuit, razon_social,
    fantasia, activo}. Si la propuesta trae `proveedor_garro` (lo decidió la aprobación), manda ese id; si no,
    con CUIT manda el CUIT y sin CUIT el nombre exacto (razón social o fantasía)."""
    if "proveedor_garro" in propuesta:
        pid = proveedor_garro_id(propuesta)
        return [p for p in todos if p.get("id") == pid] if pid is not None else []
    cuit = solo_digitos(propuesta.get("cuit"))
    if cuit:
        return [p for p in todos if solo_digitos(p.get("cuit")) == cuit]
    nombre = nombre_clave(propuesta.get("proveedor"))
    if not nombre:
        return []
    return [p for p in todos if nombre in (nombre_clave(p.get("razon_social")), nombre_clave(p.get("fantasia")))]


def numero_lindo(punto_venta: int, numero: int) -> str:
    return f"{punto_venta:04d}-{numero:08d}"


def tope_redondeo(total: float, renglones: int, cotizacion: float = 1.0) -> float:
    """Hasta dónde una diferencia contra el total impreso es redondeo: lo mayor entre el redondeo por renglón,
    $10 y el 0,05 % del total, con un tope de $500. El redondeo por renglón es $1 en un comprobante en pesos y
    5 centavos de dólar en uno en dólares (cada renglón impreso está redondeado al centavo de dólar)."""
    por_renglon = max(1.0, 0.05 * cotizacion)
    return min(max(por_renglon * renglones, 10.0, abs(total) * 0.0005), TOPE_REDONDEO_MAXIMO)


def imputar_pagos_a_cuenta(total: float, pagos: list[dict]) -> list[tuple[int, float]]:
    """Reparte el total de una factura entre los pagos a cuenta del proveedor, del más viejo al más nuevo.

    Es lo que hace el programa al guardar una Factura A (37 de 38 cargas reales; la que no coincide
    es la primera del relevamiento, con el estado previo incompleto). En los Remito X no imputa.
    """
    con_resto = [p for p in pagos if p["resto"] > RESTO_DESPRECIABLE]
    dudosos = [p["id"] for p in con_resto if p["resto"] < RESTO_MINIMO_CONFIRMADO]
    if dudosos:
        raise NoCargable("pago_resto_dudoso",
                         f"El proveedor tiene un pago a cuenta con un resto de centavos (pago {dudosos[0]}): "
                         "no está confirmado qué hace el programa en ese caso.")
    por_id = sorted(con_resto, key=lambda p: p["id"])
    por_fecha = sorted(con_resto, key=lambda p: (str(p["fecha"]), p["id"]))

    def repartir(orden):
        pendiente, salida = total, []
        for p in orden:
            if pendiente <= RESTO_DESPRECIABLE:
                break
            importe = min(p["resto"], pendiente)
            salida.append((p["id"], importe))
            pendiente -= importe
        return salida

    a, b = repartir(por_id), repartir(por_fecha)
    if [x[0] for x in a] != [x[0] for x in b]:
        raise NoCargable("pagos_orden_dudoso",
                         "Los pagos a cuenta del proveedor no están en el mismo orden por número y por fecha: "
                         "no está confirmado cuál usa el programa.")
    return a


def armar(*, tipo: dict, proveedor: dict, punto_venta: int, numero: int, fecha: date, ahora: datetime,
          descuento_pct: float, renglones: list[dict], percepciones: list[dict], ref: Referencias,
          fecha_contable: datetime | None = None, operario_id: int | None = None) -> Plan:
    """Arma las filas a partir de datos ya validados. `renglones`: {cantidad, unitario, iva, descuento,
    categoria, cuenta, texto}. No valida el origen: eso lo hace `planificar`."""
    operario = operario_id if operario_id is not None else ref.operario_id
    momento = datetime.combine(fecha, ahora.time().replace(microsecond=0))
    contable = fecha_contable or momento
    factor_cab = 1 + descuento_pct / 100

    comprobante = {
        "fec_comprobante_compra": momento,
        "fec_contab_comprobante_compra": contable,
        "nro_suc_comprobante_compra": punto_venta,
        "nro_comprobante_compra": numero,
        "nro_remito_comprobante_compra": "",
        "obs_comprobante_compra": "",
        "estado_comprobante_compra": "activo",
        "fk_id_tipo_comprobante_compra": tipo["id"],
        "fk_id_proveedor": proveedor["id"],
        "fk_id_condicion_compra": CONDICION_COMPRA,
        "porc_desc_comprobante_compra": descuento_pct,
        "fk_id_operario": operario,
        "fk_id_producto_deposito": DEPOSITO,
        "fec_venc_comprobante_compra": momento + timedelta(days=DIAS_VENCIMIENTO),
        "porcentaje_descuento_pago_termino": 0,
        "stock": True,
        "genera_cta_cte": True,
        "fk_id_moneda": MONEDA_PESOS,
        "imp_moneda": 1,
        "nro_proyecto": None,
        "controlado": False,
        "fec_control": None,
        "fk_id_operario_control": None,
    }

    items, neto, iva = [], 0.0, 0.0
    for r in renglones:
        neto_linea = r["cantidad"] * r["unitario"] * (1 + r.get("descuento", 0) / 100) * factor_cab
        neto += neto_linea
        iva += neto_linea * r["iva"] / 100
        items.append({
            "fila": {
                "can_item_comprobante_compra": r["cantidad"],
                "imp_neto_item_comprobante_compra": r["unitario"],
                "porc_iva_item_comprobante_compra": r["iva"],
                "imp_int_item_comprobante_compra": 0,
                "porc_desc_item_comprobante_compra": r.get("descuento", 0),
                "fk_id_cuenta_imputacion_item_compra": r["categoria"],
                "fk_id_tipo_no_gravado_compra": None,
                "fk_id_plan_de_cuenta": r["cuenta"],
            },
            "concepto": r["texto"],
            "neto": neto_linea,
        })

    regimenes = [{"fk_id_regimen_especial": p["regimen"], "imp_regimen_especial_x_comprobante_compra": p["importe"]}
                 for p in percepciones]
    total_percep = sum(p["importe"] for p in percepciones)
    total = neto + iva + total_percep

    asiento = None
    if tipo.get("es_ctacte"):
        if ref.cuenta_proveedores is None or ref.cuenta_credito_fiscal is None:
            raise NoCargable("config_contable", "Falta la configuración contable (cuentas de proveedores o de IVA).")
        if ref.siguiente_grupo is None:
            raise NoCargable("sin_grupo", "No se pudo calcular el número de asiento del mes.")
        movimientos = []
        debe = 0.0
        if r2(iva) != 0:
            movimientos.append({"es_debe": True, "es_haber": False, "fk_id_plan_de_cuenta": ref.cuenta_credito_fiscal,
                                "imp_asiento_contable_movimiento": r2(iva), "obs_asiento_contable_movimiento": ""})
            debe += r2(iva)
        for it in items:
            movimientos.append({"es_debe": True, "es_haber": False,
                                "fk_id_plan_de_cuenta": it["fila"]["fk_id_plan_de_cuenta"],
                                "imp_asiento_contable_movimiento": r2(it["neto"]),
                                "obs_asiento_contable_movimiento": (it["concepto"] or "").upper()})
            debe += r2(it["neto"])
        for p in percepciones:
            movimientos.append({"es_debe": True, "es_haber": False, "fk_id_plan_de_cuenta": ref.regimenes[p["regimen"]]["cuenta"],
                                "imp_asiento_contable_movimiento": r2(p["importe"]),
                                "obs_asiento_contable_movimiento": ""})
            debe += r2(p["importe"])
        haber = {"es_debe": False, "es_haber": True, "fk_id_plan_de_cuenta": ref.cuenta_proveedores,
                 "imp_asiento_contable_movimiento": r2(debe), "obs_asiento_contable_movimiento": ""}
        descripcion = (f"{str(tipo['descripcion']).upper()} {numero_lindo(punto_venta, numero)} "
                       f"{proveedor['razon_social']}")
        if items and items[0]["concepto"]:
            descripcion += f" | {items[0]['concepto'].upper()}"
        largo = ref.largos.get("asientos_contables.des_asiento_contable")
        asiento = {
            "fila": {
                "fec_asiento_contable": contable,
                "nro_grupo_asiento_contable": ref.siguiente_grupo,
                "des_asiento_contable": descripcion[:largo] if largo else descripcion,
                "fk_id_operario": operario,
                "fk_id_cheque_propio": None, "fk_id_asiento_contable": None, "fk_id_recibo": None,
                "fk_id_comprobante_venta": None, "visado": False, "manual": False,
                "nro_orden_manual_asiento_contable": None, "fk_id_pago": None,
                "es_cierre_resultado": False, "es_cierre_patrimonial": False,
                "fk_id_ajuste_cuenta_corriente_cliente": None, "fk_id_ajuste_cuenta_corriente_proveedor": None,
                "fk_id_movimiento_fondo": None, "fk_id_cheque": None, "fk_id_movimiento_cuenta_bancaria": None,
                "modulo": None, "fk_id_deposito_efectivo_a_cuenta_bancaria": None,
                "fk_id_extraccion_efectivo_de_cuenta_bancaria": None, "fk_id_movimiento_caja": None,
                "fk_id_transferencia_entre_cuenta_bancaria": None, "es_ctacte": True,
                "fk_id_plazo_fijo": None, "fk_id_fondo_comun_de_inversion": None,
            },
            "movimientos": [haber] + movimientos,
        }

    imputaciones = []
    if tipo.get("es_ctacte"):
        for pago, importe in imputar_pagos_a_cuenta(total, ref.pagos_a_cuenta):
            imputaciones.append({"fk_id_pago": pago, "imp_pago_en_comprobante_compra": r3(importe),
                                 "es_saldo_pago_anterior": False, "imp_neto_pago_en_comprobante_compra": 0})

    return Plan(comprobante=comprobante, items=items, regimenes=regimenes, asiento=asiento,
                imputaciones=imputaciones, neto=neto, iva=iva, percepciones=total_percep, total=total, avisos=[])


def planificar(carga: dict, ref: Referencias, ahora: datetime) -> Plan:
    """Valida una propuesta aprobada del asistente y arma el plan. Levanta NoCargable si no corresponde cargarla.

    `carga`: {"propuesta": <la propuesta del asistente>, "total_pesos": <total impreso en pesos>}.
    """
    p = carga.get("propuesta")
    if not isinstance(p, dict):
        raise NoCargable("sin_propuesta", "La carga no trae la propuesta.")
    tipo_id = _entero(p.get("tipo_garro_id"))
    if tipo_id not in TIPOS_HABILITADOS or p.get("es_nota_credito"):
        raise NoCargable("tipo_no_habilitado", "Por ahora solo se cargan Factura A y Remito X.")
    if ref.tipo is None or ref.tipo.get("id") != tipo_id:
        raise NoCargable("tipo_desconocido", "El tipo de comprobante no existe en el programa.")
    if p.get("requiere_revision") is not False:
        raise NoCargable("requiere_revision", "La lectura quedó marcada para revisar: se carga a mano.")
    if ref.operario_id is None:
        raise NoCargable("sin_operario", "No está el usuario del bot en el programa.")

    # Con `proveedor_garro` el proveedor ya lo decidió la aprobación (y lo vio la persona): se usa ese id. Sin el
    # campo (cargas encoladas antes) lo busca el escritor: por CUIT si el comprobante lo trae; si no (los
    # presupuestos no fiscales no lo traen), por razón social o nombre de fantasía exactos. Tiene que haber uno solo.
    cuit = solo_digitos(p.get("cuit"))
    fijado = "proveedor_garro" in p
    if fijado:
        pid = proveedor_garro_id(p)
        if pid is None:
            raise NoCargable("proveedor_sin_resolver",
                             "En la aprobación no se resolvió a qué proveedor de Garro cargarlo: se carga a mano.")
        if tipo_id == TIPO_FACTURA_A and len(cuit) != 11:
            raise NoCargable("cuit", "Una Factura A tiene que traer el CUIT del proveedor (11 dígitos).")
        if len(ref.proveedores) != 1:
            raise NoCargable("proveedor", f"El proveedor de Garro elegido en la aprobación (id {pid}) no existe en el programa.")
    else:
        if not cuit and tipo_id == TIPO_FACTURA_A:
            raise NoCargable("cuit", "Una Factura A tiene que traer el CUIT del proveedor.")
        if cuit and len(cuit) != 11:
            raise NoCargable("cuit", "El CUIT del proveedor no tiene 11 dígitos.")
        if not cuit and not nombre_clave(p.get("proveedor")):
            raise NoCargable("proveedor", "El comprobante no trae ni el CUIT ni el nombre del proveedor.")
        if len(ref.proveedores) != 1:
            raise NoCargable("proveedor", f"No hay un único proveedor con ese {'CUIT' if cuit else 'nombre'} "
                                          f"en el programa (encontrados: {len(ref.proveedores)}).")
    proveedor = ref.proveedores[0]
    if not proveedor.get("activo"):
        raise NoCargable("proveedor_inactivo", "El proveedor está inactivo en el programa.")
    if fijado and tipo_id == TIPO_FACTURA_A and solo_digitos(proveedor.get("cuit")) != cuit:
        raise NoCargable("proveedor_cuit", "El CUIT de la Factura A no es el que tiene ese proveedor en el programa: "
                                           "corregilo en Garro y cargala a mano.")

    punto_venta, numero = _entero(p.get("punto_venta")), _entero(p.get("numero"))
    if punto_venta is None or not 0 < punto_venta <= 99999 or numero is None or not 0 < numero <= 99999999:
        raise NoCargable("numero", "El punto de venta o el número del comprobante no son válidos.")
    if ref.duplicados:
        raise NoCargable("ya_cargado", f"Ya está cargado en el programa (comprobante {ref.duplicados[0]}).")

    try:
        fecha = date.fromisoformat(str(p.get("fecha")))
    except ValueError:
        raise NoCargable("fecha", "El comprobante no trae una fecha válida.") from None
    if fecha > ahora.date() or (ahora.date() - fecha).days > MAX_DIAS_ATRAS:
        raise NoCargable("fecha", f"La fecha del comprobante ({fecha.isoformat()}) es futura o de hace más de un año.")

    descuento = _opcional(p, "descuento_pct", 0.0, "descuento", "El descuento de cabecera no es un número.")
    if descuento > 0 or descuento <= -100:
        raise NoCargable("descuento", "El descuento de cabecera no es válido (tiene que ser cero o negativo).")

    iva_tipo = IVA_POR_TIPO[tipo_id]
    largo_concepto = ref.largos.get("conceptos_compras.des_concepto_compra")
    crudos = p.get("items")
    if not isinstance(crudos, list) or not crudos:
        raise NoCargable("sin_renglones", "El comprobante no trae renglones.")
    if len(crudos) > MAX_RENGLONES:
        raise NoCargable("demasiados_renglones", f"El comprobante trae más de {MAX_RENGLONES} renglones.")
    renglones = []
    for n, it in enumerate(crudos, 1):
        if not isinstance(it, dict):
            raise NoCargable("renglon", f"El renglón {n} vino con una forma inesperada.")
        if it.get("no_imputable"):
            raise NoCargable("renglon_no_imputable", f"El renglón {n} no es una compra (percepción, sueldo, etc.).")
        cantidad, linea = _numero(it.get("cantidad_garro")), _numero(it.get("linea_pesos"))
        if cantidad is None or not 0 < cantidad < TOPE_IMPORTE or linea is None or not 0 < linea < TOPE_IMPORTE \
                or linea / cantidad >= TOPE_IMPORTE:
            raise NoCargable("renglon_importe", f"El renglón {n} no tiene cantidad e importe positivos y razonables.")
        if tipo_id == TIPO_FACTURA_A and _numero(it.get("alicuota_iva")) != iva_tipo:
            raise NoCargable("iva", f"El renglón {n} no tiene IVA del 21 %: solo ese caso está confirmado.")
        categoria, cuenta = _entero(it.get("garro_cuenta_imputacion_id")), _entero(it.get("garro_plan_de_cuenta_id"))
        if categoria is None or categoria not in ref.categorias:
            raise NoCargable("categoria", f"El renglón {n} no tiene una categoría que exista en el programa.")
        estado = ref.cuentas.get(cuenta) if cuenta is not None else None
        if not estado or not estado.get("activa") or not estado.get("imputable"):
            raise NoCargable("cuenta", f"El renglón {n} no tiene una cuenta contable activa e imputable.")
        texto = limpiar_texto(it.get("descripcion"), largo_concepto)
        if not texto:
            raise NoCargable("renglon_texto", f"El renglón {n} no tiene descripción.")
        renglones.append({"cantidad": cantidad, "unitario": linea / cantidad, "iva": iva_tipo,
                          "categoria": categoria, "cuenta": cuenta, "texto": texto, "linea": linea})

    cotizacion = _opcional(p, "tipo_cambio_efectivo", 1.0, "cotizacion", "La cotización no es un número.")
    if cotizacion <= 0 or cotizacion >= TOPE_IMPORTE:
        raise NoCargable("cotizacion", "La cotización tiene que ser mayor que cero.")
    percepciones = []
    crudas = p.get("percepciones") or []
    if not isinstance(crudas, list):
        raise NoCargable("percepcion", "Las percepciones vinieron con una forma inesperada.")
    if len(crudas) > MAX_PERCEPCIONES:
        raise NoCargable("percepcion", f"El comprobante trae más de {MAX_PERCEPCIONES} percepciones.")
    if crudas and tipo_id != TIPO_FACTURA_A:
        raise NoCargable("percepcion", "Un Remito X con percepciones no está confirmado: se carga a mano.")
    for per in crudas:
        regimen = _entero(per.get("garro_regimen_id")) if isinstance(per, dict) else None
        importe = _numero(per.get("importe")) if isinstance(per, dict) else None
        datos = ref.regimenes.get(regimen) if regimen is not None else None
        if not datos or datos.get("tipo") != TIPO_REGIMEN_PERCEPCION:
            raise NoCargable("percepcion", "Una percepción no tiene un régimen de percepción válido en el programa.")
        estado = ref.cuentas.get(datos.get("cuenta"))
        if not estado or not estado.get("activa") or not estado.get("imputable"):
            raise NoCargable("percepcion", "La cuenta del régimen de la percepción no está activa o no es imputable.")
        en_pesos = r2(importe * cotizacion) if importe is not None and 0 < importe < TOPE_IMPORTE else None
        if en_pesos is None or not 0 < en_pesos < TOPE_IMPORTE:
            raise NoCargable("percepcion", "Una percepción no tiene un importe positivo (en pesos) razonable.")
        percepciones.append({"regimen": regimen, "importe": en_pesos})

    total_impreso = _numero(carga.get("total_pesos"))
    if total_impreso is None or not 0 < total_impreso < TOPE_IMPORTE:
        raise NoCargable("sin_total", "La carga no trae el total impreso en pesos.")

    def intentar(lista):
        return armar(tipo=ref.tipo, proveedor=proveedor, punto_venta=punto_venta, numero=numero, fecha=fecha,
                     ahora=ahora, descuento_pct=descuento, renglones=lista, percepciones=percepciones, ref=ref)

    plan = intentar(renglones)
    diferencia = r2(total_impreso - r2(plan.total))
    avisos = []
    if abs(diferencia) > 0.011:
        if abs(diferencia) > tope_redondeo(total_impreso, len(renglones), cotizacion):
            raise NoCargable("total_no_cierra",
                             f"Los renglones suman {r2(plan.total):.2f} y el total impreso es {total_impreso:.2f}: "
                             "la diferencia es demasiado grande para ser redondeo.")
        mayor = max(renglones, key=lambda r: r["linea"])
        factor = (1 + iva_tipo / 100) * (1 + descuento / 100)
        if diferencia > 0:
            # Como carga el equipo: un renglón «REDONDEO» por lo que falta (27 casos, todos a favor).
            renglones = renglones + [{"cantidad": 1.0, "unitario": diferencia / factor, "iva": iva_tipo,
                                      "categoria": mayor["categoria"], "cuenta": mayor["cuenta"],
                                      "texto": CONCEPTO_REDONDEO, "linea": diferencia / factor}]
            avisos.append(f"Se agregó un renglón REDONDEO por {diferencia:.2f} para igualar el total impreso.")
        else:
            # Cuando sobra, el equipo no carga un redondeo negativo: achica un renglón.
            nueva = mayor["linea"] + diferencia / factor
            if nueva <= 0:
                raise NoCargable("total_no_cierra", "La diferencia contra el total impreso no entra en ningún renglón.")
            renglones = [dict(r, linea=nueva, unitario=nueva / r["cantidad"]) if r is mayor else r for r in renglones]
            avisos.append(f"El renglón «{mayor['texto'][:40]}» se achicó {abs(diferencia):.2f} para igualar el total impreso.")
        plan = intentar(renglones)
        if abs(r2(total_impreso - r2(plan.total))) > 0.011:
            raise NoCargable("total_no_cierra", "Ni con el renglón de redondeo se llega al total impreso.")
    plan.avisos = avisos
    return plan
