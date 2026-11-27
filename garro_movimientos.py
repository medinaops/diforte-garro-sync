"""Carga de movimientos de fondos (egresos) en Gestión Integral: planificador.

Decide QUÉ filas habría que escribir para un movimiento de fondos aprobado, igual que las escribe el programa
cuando una persona lo carga a mano. Puro: no abre conexiones ni escribe nada (lo usa `garro_escritor.py`).

Alcance: solo egresos en pesos, pagados en efectivo (caja 1) o por transferencia (cuenta bancaria 11), de los
cuatro tipos de movimiento habilitados. Esos tipos no generan asiento contable: si un tipo pasara a generarlo,
se rechaza (la receta del asiento de un movimiento no está relevada). Lo que no está confirmado, no se carga:
se levanta `NoCargable` con un motivo estable y el movimiento queda para cargar a mano.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

from garro_cargas import NoCargable, _entero, _numero, limpiar_texto, nombre_clave, r2

VERSION_RECETA = 1
CLASE = "movimiento_fondo"

TIPOS_HABILITADOS = (50, 17, 58, 13)
CAJA = 1
CUENTA_BANCARIA = 11
CUENTAS_BANCARIAS_HABILITADAS = (CUENTA_BANCARIA,)
MONEDA_PESOS = 1
TIPO_MOVIMIENTO_BANCARIO = "TRANSFERENCIA BANCARIA"
# Cuenta contable del movimiento bancario: la que usa la cuenta bancaria 11 en el programa.
PLAN_CUENTA_BANCARIO = 109
MEDIOS = ("efectivo", "transferencia")

MAX_DIAS_ATRAS = 60
TOPE_IMPORTE = 1e10
LARGO_DESCRIPCION = 100
TOLERANCIA_DUPLICADO = 1.0

COLUMNAS = {
    "movimientos_fondos": (
        "id_movimiento_fondo", "fk_id_movimiento_fondo_tipo", "fec_movimiento_fondo", "fk_id_operario",
        "obs_movimiento_fondo", "fk_id_caja", "es_ingreso", "es_egreso", "des_movimiento_fondo", "es_ctacte",
        "fec_carga_movimiento_fondo", "genera_ctacte", "centro_costos",
    ),
    "efectivo_medio_de_pago": (
        "id_efectivo_medio_de_pago", "imp_efectivo_medio_de_pago", "imp_equivalente_peso", "fk_id_moneda",
        "fecha", "fk_id_caja", "es_ctacte",
    ),
    "efectivo_medio_de_pago_en_movimientos_fondos": (
        "id_efectivo_medio_de_pago_en_movimiento_fondo", "fk_id_movimiento_fondo", "fk_id_efectivo_medio_de_pago",
        "fk_id_caja",
    ),
    "movimientos_cuentas_bancarias": (
        "id_movimiento_cuenta_bancaria", "nro_movimiento_cuenta_bancaria", "imp_movimiento_cuenta_bancaria",
        "obs_movimiento_cuenta_bancaria", "fec_movimiento_cuenta_bancaria", "fk_id_cuenta_bancaria",
        "fk_id_plan_de_cuenta", "fec_visado_movimiento_cuenta_bancaria", "tipo_movimiento_cuenta_bancaria",
    ),
    "movimientos_cuenta_bancaria_en_movimientos_fondos": (
        "id_movimiento_cuenta_bancaria_en_movimiento_fondo", "fk_id_movimiento_fondo",
        "fk_id_movimiento_cuenta_bancaria", "fk_id_caja",
    ),
}

_I, _D, _T, _V, _B, _F = "integer", "double precision", "timestamp without time zone", "character varying", "boolean", "date"
TIPOS = {
    "movimientos_fondos": {
        "id_movimiento_fondo": _I, "fk_id_movimiento_fondo_tipo": _I, "fec_movimiento_fondo": _T,
        "fk_id_operario": _I, "obs_movimiento_fondo": _V, "fk_id_caja": _I, "es_ingreso": _B, "es_egreso": _B,
        "des_movimiento_fondo": _V, "es_ctacte": _B, "fec_carga_movimiento_fondo": _T, "genera_ctacte": _B,
        "centro_costos": _B,
    },
    "efectivo_medio_de_pago": {
        "id_efectivo_medio_de_pago": _I, "imp_efectivo_medio_de_pago": _D, "imp_equivalente_peso": _D,
        "fk_id_moneda": _I, "fecha": _T, "fk_id_caja": _I, "es_ctacte": _B,
    },
    "efectivo_medio_de_pago_en_movimientos_fondos": {
        "id_efectivo_medio_de_pago_en_movimiento_fondo": _I, "fk_id_movimiento_fondo": _I,
        "fk_id_efectivo_medio_de_pago": _I, "fk_id_caja": _I,
    },
    "movimientos_cuentas_bancarias": {
        "id_movimiento_cuenta_bancaria": _I, "nro_movimiento_cuenta_bancaria": _V,
        "imp_movimiento_cuenta_bancaria": _D, "obs_movimiento_cuenta_bancaria": _V,
        "fec_movimiento_cuenta_bancaria": _T, "fk_id_cuenta_bancaria": _I, "fk_id_plan_de_cuenta": _I,
        "fec_visado_movimiento_cuenta_bancaria": _F, "tipo_movimiento_cuenta_bancaria": _V,
    },
    "movimientos_cuenta_bancaria_en_movimientos_fondos": {
        "id_movimiento_cuenta_bancaria_en_movimiento_fondo": _I, "fk_id_movimiento_fondo": _I,
        "fk_id_movimiento_cuenta_bancaria": _I, "fk_id_caja": _I,
    },
}

# Orden de las tablas en `escrito` (y, al revés, el de borrado): el movimiento primero, los vínculos al final.
TABLAS_ESCRITO = (
    "movimientos_fondos", "efectivo_medio_de_pago", "efectivo_medio_de_pago_en_movimientos_fondos",
    "movimientos_cuentas_bancarias", "movimientos_cuenta_bancaria_en_movimientos_fondos",
)
TABLAS_EFECTIVO = ("efectivo_medio_de_pago", "efectivo_medio_de_pago_en_movimientos_fondos")
TABLAS_BANCO = ("movimientos_cuentas_bancarias", "movimientos_cuenta_bancaria_en_movimientos_fondos")


@dataclass
class Referencias:
    """Lo que el escritor lee de Gestión Integral, dentro de su transacción, antes de planificar."""

    tipo: dict | None                 # {id, descripcion, es_ingreso, es_egreso, genera_asiento}
    operario_id: int | None           # el usuario propio del bot en el programa
    caja: dict | None                 # {id, habilitada}
    cuenta_bancaria: dict | None      # {id, activa, plan}
    existentes: list[dict] = field(default_factory=list)  # {id, tipo, descripcion, fecha, importe, egreso}: los del tipo y los egresos de ±10 días
    largos: dict[str, int | None] = field(default_factory=dict)  # «tabla.columna» -> largo máximo


@dataclass
class Plan:
    movimiento: dict
    medio: str                        # efectivo | transferencia
    medio_fila: dict                  # efectivo_medio_de_pago o movimientos_cuentas_bancarias (sin clave primaria)
    vinculo: dict                     # fila del vínculo sin las dos claves foráneas
    tablas_medio: tuple[str, str]     # (tabla del medio, tabla del vínculo)
    importe: float
    tipo_id: int
    descripcion: str
    fecha: date
    avisos: list[str]

    def resumen(self) -> dict:
        return {
            "receta": VERSION_RECETA, "clase": CLASE, "tipo_mov_id": self.tipo_id, "medio": self.medio,
            "importe": r2(self.importe), "fecha": self.fecha.isoformat(), "descripcion": self.descripcion,
            "asiento": False, "avisos": list(self.avisos),
        }


def _periodo_partes(periodo) -> tuple[int, int] | None:
    m = re.fullmatch(r"(\d{4})-(\d{2})", periodo) if isinstance(periodo, str) else None
    if not m or not 1 <= int(m.group(2)) <= 12:
        return None
    return int(m.group(1)), int(m.group(2))


# --- Duplicados: la misma regla que `compras_asistente_movimientos_parecidos` (migración 666). Las expresiones regulares
# son las de esa función, sobre el texto en minúsculas y sin acentos; si una cambia, cambia la otra (y los casos dorados
# de fixtures/casos_duplicados.json las prueban a las dos).
TIPOS_CON_SIGLA = (58, 13)
MIN_IMPORTE_POR_IMPORTE = 1000.0
DIAS_MISMO_TIPO = 40
DIAS_OTRO_TIPO = 10
MESES_VENTANA = 4
NOMBRES_COMPLETOS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre|setiembre",
                     "octubre", "noviembre", "diciembre")
NOMBRES_ABREVIADOS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sept?|set", "oct", "nov", "dic")
_FECHA_1 = re.compile(r"[0-9]{1,2}\s*[/.\-]\s*[0-9]{1,2}\s*[/.\-]\s*(20)?[0-9]{2}")   # 15/01/2026: una fecha, no un período
_FECHA_2 = re.compile(r"20[0-9]{2}\s*[/.\-]\s*[0-9]{1,2}\s*[/.\-]\s*[0-9]{1,2}")        # 2026/01/15


def sin_acentos_en_minusculas(texto) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(texto or "").lower()) if unicodedata.category(c) != "Mn")


def sin_fechas(texto) -> str:
    return _FECHA_1.sub(" ", _FECHA_2.sub(" ", sin_acentos_en_minusculas(texto)))


def sumar_meses(d: date, meses: int) -> date:
    """Primer día de un mes más `meses` (solo se usa con el día 1)."""
    n = d.year * 12 + d.month - 1 + meses
    return date(n // 12, n % 12 + 1, d.day)


@dataclass
class PatronesDePeriodo:
    anio: int
    mes: int

    def __post_init__(self):
        a, m = self.anio, self.mes
        yy = f"{a % 100:02d}"
        self.inicio = date(a, m, 1)
        self.fin_ventana = sumar_meses(self.inicio, MESES_VENTANA)
        completo, abreviado = NOMBRES_COMPLETOS[m - 1], NOMBRES_ABREVIADOS[m - 1]
        self.a1 = re.compile(rf"(^|[^0-9])0?{m}\s*[/.\-]\s*(20)?{yy}([^0-9]|$)")
        self.a2 = re.compile(rf"(^|[^0-9]){a}\s*[/.\-]\s*0?{m}([^0-9]|$)")
        self.m = re.compile(rf"(^|[^a-z])per(iodo)?\s*\.?\s*0?{m}([^0-9/.\-]|$)")
        self.n1 = re.compile(rf"(^|[^a-z])({completo}|{abreviado})\.?\s*[/.\-]?\s*(20)?{yy}([^0-9]|$)")
        self.n2 = re.compile(rf"(^|[^a-z])({completo})(?![a-z0-9])(?!\s*[/.\-]?\s*[0-9])")

    def coincide(self, descripcion, fecha: date) -> bool:
        """El período está escrito en la descripción. «PER MM» y el mes en letras sin año solo valen si el movimiento cae
        entre el primer día del período y cuatro meses después (si no, es el de otro año)."""
        d = sin_fechas(descripcion)
        if self.a1.search(d) or self.a2.search(d) or self.n1.search(d):
            return True
        return (self.m.search(d) is not None or self.n2.search(d) is not None) and self.inicio <= fecha < self.fin_ventana


def sigla(descripcion) -> str | None:
    """Lo que va antes de «PER» (o antes del período numérico): «FAECYS PER 10/2026» → «faecys»."""
    pn = " " + sin_fechas(descripcion)
    m = re.match(r"^(.*?)[^a-z]per(?:iodo)?(?:[^a-z]|$)", pn, re.S) or re.match(r"^(.*?)[^a-z0-9][0-9]{1,2}\s*[/.\-]", pn, re.S)
    texto = re.sub(r"[^a-z0-9]+", " ", m.group(1) if m else "").strip()
    return texto or None


def _fecha_de(valor) -> date | None:
    if isinstance(valor, datetime):
        return valor.date()
    return valor if isinstance(valor, date) else None


def buscar_duplicados(existentes: list[dict], *, tipo: int, descripcion: str, periodo: tuple[int, int], importe: float,
                      hoy: date) -> tuple[dict | None, dict | None]:
    """(ya_cargado, posible_duplicado). `existentes`: {id, tipo, descripcion, fecha, importe, egreso}; importe es None si
    el movimiento no tiene ningún medio.
    - ya_cargado: mismo tipo con el período escrito en la descripción (en los tipos 58 y 13, además con la misma sigla
      como palabra) o con exactamente la misma descripción dentro de la ventana de fechas del mismo tipo.
    - posible_duplicado: lo mismo sin la sigla; o, con un importe de $1000 o más, un importe a menos de $1 del mismo tipo
      (a ±40 días de hoy, o entre el primer día del período y 4 meses después) o de otro tipo (egresos a ±10 días)."""
    patrones = PatronesDePeriodo(*periodo)
    mia = sigla(descripcion)
    clave = " " + re.sub(r"[^a-z0-9]+", " ", sin_acentos_en_minusculas(descripcion)).strip() + " "
    posible_periodo = None
    for e in existentes:
        f = _fecha_de(e.get("fecha"))
        if f is None or e.get("tipo") != tipo:
            continue
        en_ventana = abs((f - hoy).days) <= DIAS_MISMO_TIPO or patrones.inicio <= f <= patrones.fin_ventana
        otra = " " + re.sub(r"[^a-z0-9]+", " ", sin_acentos_en_minusculas(e.get("descripcion"))).strip() + " "
        if en_ventana and otra == clave:
            return e, None
        if patrones.coincide(e.get("descripcion"), f):
            if tipo not in TIPOS_CON_SIGLA:
                return e, None
            otra_sin_fechas = " " + re.sub(r"[^a-z0-9]+", " ", sin_fechas(e.get("descripcion"))).strip() + " "
            if mia is not None and f" {mia} " in otra_sin_fechas:
                return e, None
            posible_periodo = posible_periodo or e
    if posible_periodo is not None:
        return None, posible_periodo
    if importe < MIN_IMPORTE_POR_IMPORTE:
        return None, None
    for e in existentes:
        f, imp = _fecha_de(e.get("fecha")), _numero(e.get("importe"))
        if f is None or imp is None or abs(imp - importe) > TOLERANCIA_DUPLICADO + 1e-9:
            continue
        if e.get("tipo") == tipo:
            if abs((f - hoy).days) <= DIAS_MISMO_TIPO or patrones.inicio <= f <= patrones.fin_ventana:
                return None, e
        elif e.get("egreso") is True and abs((f - hoy).days) <= DIAS_OTRO_TIPO:
            return None, e
    return None, None


def _importe_valido(x) -> float | None:
    n = _numero(x)
    if n is None or not 0 < n < TOPE_IMPORTE or r2(n) != n:
        return None
    return n


def planificar(propuesta: dict, ref: Referencias, ahora: datetime, total_pesos=None) -> Plan:
    """Valida un movimiento aprobado y arma el plan. Levanta NoCargable si no corresponde cargarlo solo."""
    p = propuesta
    if not isinstance(p, dict):
        raise NoCargable("sin_propuesta", "La carga no trae la propuesta.")
    tipo_id = _entero(p.get("tipo_mov_id"))
    if tipo_id not in TIPOS_HABILITADOS:
        raise NoCargable("tipo_no_habilitado", "Por ahora solo se cargan movimientos de fondos de los tipos "
                                               "comercio e industria, cargas sociales, sindicato y pagos a la AFIP.")
    if ref.tipo is None or ref.tipo.get("id") != tipo_id:
        raise NoCargable("tipo_desconocido", "El tipo de movimiento no existe en el programa.")
    if ref.tipo.get("es_egreso") is not True or ref.tipo.get("es_ingreso") is not False:
        raise NoCargable("tipo_no_egreso", "El tipo de movimiento no es un egreso en el programa.")
    if ref.tipo.get("genera_asiento") is not False:
        raise NoCargable("tipo_con_asiento", "El tipo de movimiento genera asiento contable en el programa: "
                                             "esa carga no está relevada, se hace a mano.")
    if p.get("requiere_revision") is not False:
        raise NoCargable("requiere_revision", "La lectura quedó marcada para revisar: se carga a mano.")
    if p.get("posible_duplicado") is not False and p.get("posible_duplicado") is not None:
        raise NoCargable("posible_duplicado", "Quedó marcado como posible duplicado en la aprobación: se carga a mano.")
    if ref.operario_id is None:
        raise NoCargable("sin_operario", "No está el usuario del bot en el programa.")

    medio = p.get("medio")
    if medio not in MEDIOS:
        raise NoCargable("medio_no_habilitado", "Solo se cargan pagos en efectivo o por transferencia.")
    if _entero(p.get("caja")) != CAJA or ref.caja is None or ref.caja.get("id") != CAJA or ref.caja.get("habilitada") is not True:
        raise NoCargable("caja_invalida", f"La caja tiene que ser la {CAJA} y estar habilitada en el programa.")
    cuenta = p.get("cuenta_bancaria_id")
    if medio == "efectivo":
        if cuenta is not None:
            raise NoCargable("cuenta_invalida", "Un pago en efectivo no lleva cuenta bancaria.")
    else:
        cuenta_id = _entero(cuenta)
        c = ref.cuenta_bancaria
        if cuenta_id not in CUENTAS_BANCARIAS_HABILITADAS or c is None or c.get("id") != cuenta_id \
                or c.get("activa") is not True:
            raise NoCargable("cuenta_invalida", "La transferencia tiene que ser de una cuenta bancaria activa y habilitada.")
        if c.get("plan") != PLAN_CUENTA_BANCARIO:
            raise NoCargable("cuenta_invalida", "La cuenta contable de la cuenta bancaria no es la confirmada: se carga a mano.")

    importe = _importe_valido(p.get("importe"))
    if importe is None:
        raise NoCargable("importe", "El importe tiene que ser mayor que cero, a centavos y razonable.")
    if total_pesos is not None and (_numero(total_pesos) is None or r2(_numero(total_pesos)) != importe):
        raise NoCargable("importe", "El total de la carga no coincide con el importe del movimiento.")

    fecha = None
    if isinstance(p.get("fecha"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p["fecha"]):
        try:
            fecha = date.fromisoformat(p["fecha"])
        except ValueError:
            fecha = None
    if fecha is None:
        raise NoCargable("fecha", "El movimiento no trae una fecha válida.")
    if fecha > ahora.date() or (ahora.date() - fecha).days > MAX_DIAS_ATRAS:
        raise NoCargable("fecha", f"La fecha del movimiento ({fecha.isoformat()}) es futura o de hace más de "
                                  f"{MAX_DIAS_ATRAS} días.")

    periodo = _periodo_partes(p.get("periodo"))
    if periodo is None:
        raise NoCargable("periodo", "El período tiene que venir como AAAA-MM.")
    descripcion = limpiar_texto(p.get("descripcion")) if isinstance(p.get("descripcion"), str) else ""
    largo = ref.largos.get("movimientos_fondos.des_movimiento_fondo")
    tope = min(LARGO_DESCRIPCION, largo) if largo else LARGO_DESCRIPCION
    if not descripcion or len(descripcion) > tope:
        raise NoCargable("descripcion", f"La descripción tiene que tener entre 1 y {tope} caracteres.")

    ya, posible = buscar_duplicados(ref.existentes, tipo=tipo_id, descripcion=descripcion, periodo=periodo, importe=importe,
                                    hoy=ahora.date())
    if ya is not None:
        raise NoCargable("ya_cargado", f"Ya está cargado en el programa (movimiento {ya.get('id')}).")
    if posible is not None:
        raise NoCargable("posible_duplicado", f"Hay un movimiento parecido (mismo período u otro concepto, o casi el mismo importe) "
                                              f"en el programa (movimiento {posible.get('id')}): se carga a mano si no es el mismo.")

    momento = datetime.combine(fecha, ahora.time().replace(microsecond=0))
    movimiento = {
        "fk_id_movimiento_fondo_tipo": tipo_id,
        "fec_movimiento_fondo": momento,
        "fk_id_operario": ref.operario_id,
        "obs_movimiento_fondo": "",
        "fk_id_caja": CAJA,
        "es_ingreso": False,
        "es_egreso": True,
        "des_movimiento_fondo": descripcion,
        "es_ctacte": True,
        "fec_carga_movimiento_fondo": ahora.replace(microsecond=0),
        "genera_ctacte": False,
        "centro_costos": False,
    }
    if medio == "efectivo":
        medio_fila = {"imp_efectivo_medio_de_pago": importe, "imp_equivalente_peso": 1, "fk_id_moneda": MONEDA_PESOS,
                      "fecha": momento, "fk_id_caja": CAJA, "es_ctacte": True}
        tablas = TABLAS_EFECTIVO
    else:
        medio_fila = {"nro_movimiento_cuenta_bancaria": "", "imp_movimiento_cuenta_bancaria": importe,
                      "obs_movimiento_cuenta_bancaria": "", "fec_movimiento_cuenta_bancaria": momento,
                      "fk_id_cuenta_bancaria": CUENTA_BANCARIA, "fk_id_plan_de_cuenta": PLAN_CUENTA_BANCARIO,
                      "fec_visado_movimiento_cuenta_bancaria": None,
                      "tipo_movimiento_cuenta_bancaria": TIPO_MOVIMIENTO_BANCARIO}
        tablas = TABLAS_BANCO
    return Plan(movimiento=movimiento, medio=medio, medio_fila=medio_fila, vinculo={"fk_id_caja": CAJA},
                tablas_medio=tablas, importe=importe, tipo_id=tipo_id, descripcion=descripcion, fecha=fecha, avisos=[])
