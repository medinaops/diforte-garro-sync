"""Compara, fila por fila, un movimiento de fondos cargado por el bot en una COPIA de la base contra uno cargado por una
persona en el programa. Solo lectura. Sirve para la primera prueba del escritor de movimientos (garro_movimientos.py):
si las filas del bot no son las del programa, la receta está mal.

Uso (en el servidor, carpeta del programa):
    python comparar_movimientos.py --base gestion_prueba --humano 123 --bot 456
        --humano: id del movimiento que cargó una persona (en la base real o, si está en la copia, en la copia)
        --bot:    id del movimiento que cargó el bot en la copia (lo muestra `garro_escritor.py --archivo ... --escribir`)
        --otro-tipo: el movimiento de la persona es de otro tipo que el del bot (esa diferencia es esperada)
    python comparar_movimientos.py --perfil [--base NOMBRE]
        Cuenta, sin mostrar textos, cómo se guardan las columnas que todavía no están confirmadas.

Las columnas que se espera que difieran (claves, usuario, fechas, descripción, importes) se listan aparte. Todo lo
demás tiene que ser idéntico; si no, sale con código 1.
"""

from __future__ import annotations

import argparse
import os
import sys

import garro_escritor as ge
import garro_movimientos as gm
import garro_sync as g

AQUI = os.path.dirname(os.path.abspath(__file__))
TABLA_DE_MEDIO = {"efectivo": gm.TABLAS_EFECTIVO, "transferencia": gm.TABLAS_BANCO}
# Claves foráneas hacia filas que el bot creó con otro número.
CLAVES_ENTRE_FILAS = {"fk_id_movimiento_fondo", "fk_id_efectivo_medio_de_pago", "fk_id_movimiento_cuenta_bancaria"}
ESPERADAS = {
    "fk_id_operario", "fec_movimiento_fondo", "fec_carga_movimiento_fondo", "fecha", "fec_movimiento_cuenta_bancaria",
    "des_movimiento_fondo", "imp_efectivo_medio_de_pago", "imp_movimiento_cuenta_bancaria",
}
TEXTOS_LIBRES = {"nro_movimiento_cuenta_bancaria", "obs_movimiento_cuenta_bancaria", "obs_movimiento_fondo"}


def forma_de_texto(valor) -> str:
    """Un texto libre se muestra solo por su forma (nulo, vacío o su largo), no por su contenido."""
    if valor is None:
        return "NULL"
    return "''" if valor == "" else f"texto({len(str(valor))})"


def mostrar(columna: str, valor) -> str:
    return forma_de_texto(valor) if columna in TEXTOS_LIBRES else repr(valor)


def medio_de(movimiento: dict) -> str | None:
    """efectivo | transferencia según qué tablas tienen filas; None si no hay ninguna o hay de las dos."""
    hay = {m for m, tablas in TABLA_DE_MEDIO.items() if all(movimiento["filas"].get(t) for t in tablas)}
    return hay.pop() if len(hay) == 1 else None


def comparar(humano: dict, bot: dict, otro_tipo: bool = False) -> dict:
    """Compara dos movimientos leídos con `leer_movimiento`. Devuelve {"lineas": [...], "inesperadas": n, "problemas": [...]}.
    Cada línea: (tabla, columna, valor de la persona, valor del bot, "IGUAL" | "ESPERADA" | "DISTINTA")."""
    problemas = []
    medio_h, medio_b = medio_de(humano), medio_de(bot)
    if medio_h is None or medio_b is None:
        problemas.append("Un movimiento no tiene exactamente un medio (efectivo o transferencia) con su vínculo.")
    elif medio_h != medio_b:
        problemas.append(f"No se pueden comparar: el de la persona es {medio_h} y el del bot es {medio_b}.")
    for nombre, mov in (("persona", humano), ("bot", bot)):
        for tabla in gm.TABLAS_ESCRITO:
            n = len(mov["filas"].get(tabla, []))
            if n > 1:
                problemas.append(f"El movimiento de la {nombre} tiene {n} filas en {tabla}.")
        if not mov["filas"].get("movimientos_fondos"):
            problemas.append(f"No existe el movimiento de la {nombre}.")
        if mov["asientos"]:
            problemas.append(f"El movimiento de la {nombre} tiene {mov['asientos']} asiento(s): se esperaba ninguno.")
        for tabla, n in mov["otras"].items():
            problemas.append(f"Del movimiento de la {nombre} cuelgan {n} fila(s) de {tabla}: otra receta que no está relevada.")
    if problemas:
        return {"lineas": [], "inesperadas": 0, "problemas": problemas}

    lineas, inesperadas = [], 0
    tablas = ("movimientos_fondos",) + TABLA_DE_MEDIO[medio_h]
    for tabla in tablas:
        fh, fb = humano["filas"][tabla][0], bot["filas"][tabla][0]
        for columna in gm.COLUMNAS[tabla]:
            vh, vb = fh.get(columna), fb.get(columna)
            if columna == gm.COLUMNAS[tabla][0] or columna in CLAVES_ENTRE_FILAS:
                continue
            if vh == vb:
                estado = "IGUAL"
            elif columna in ESPERADAS or (otro_tipo and columna == "fk_id_movimiento_fondo_tipo"):
                estado = "ESPERADA"
            else:
                estado = "DISTINTA"
                inesperadas += 1
            lineas.append((tabla, columna, mostrar(columna, vh), mostrar(columna, vb), estado))
    # Reglas de forma que valen para el bot (en la persona son informativas).
    for nombre, mov in (("persona", humano), ("bot", bot)):
        mf = mov["filas"]["movimientos_fondos"][0]
        t_medio = TABLA_DE_MEDIO[medio_h][0]
        fila = mov["filas"][t_medio][0]
        columna = "fecha" if medio_h == "efectivo" else "fec_movimiento_cuenta_bancaria"
        mismo = fila[columna] == mf["fec_movimiento_fondo"]
        lineas.append((t_medio, f"{columna} == movimiento.fec_movimiento_fondo ({nombre})", "", "", "IGUAL" if mismo else
                       ("INFORMATIVA" if nombre == "persona" else "DISTINTA")))
        if not mismo and nombre == "bot":
            inesperadas += 1
    return {"lineas": lineas, "inesperadas": inesperadas, "problemas": []}


def leer_movimiento(cur, movimiento_id: int) -> dict:
    """Las filas del movimiento, sus medios y sus vínculos, como diccionarios por columna; más la cantidad de asientos y
    de filas de otras tablas que cuelgan del movimiento."""
    def filas(tabla: str, donde: str, params) -> list[dict]:
        columnas = gm.COLUMNAS[tabla]
        cur.execute(f"SELECT {', '.join(columnas)} FROM public.{tabla} WHERE {donde}", params)
        return [dict(zip(columnas, r)) for r in cur.fetchall()]

    salida: dict = {"filas": {}, "asientos": 0, "otras": {}}
    salida["filas"]["movimientos_fondos"] = filas("movimientos_fondos", "id_movimiento_fondo = %s", (movimiento_id,))
    for medio, (t_medio, t_vinculo) in TABLA_DE_MEDIO.items():
        vinculos = filas(t_vinculo, "fk_id_movimiento_fondo = %s", (movimiento_id,))
        clave = "fk_id_efectivo_medio_de_pago" if medio == "efectivo" else "fk_id_movimiento_cuenta_bancaria"
        salida["filas"][t_vinculo] = vinculos
        ids = [v[clave] for v in vinculos]
        salida["filas"][t_medio] = filas(t_medio, f"{gm.COLUMNAS[t_medio][0]} = ANY(%s)", (ids,)) if ids else []
    cur.execute("SELECT count(*) FROM asientos_contables WHERE fk_id_movimiento_fondo = %s", (movimiento_id,))
    salida["asientos"] = cur.fetchone()[0]
    cur.execute("""SELECT c.table_name FROM information_schema.columns AS c
                   JOIN information_schema.tables AS t ON t.table_schema = c.table_schema AND t.table_name = c.table_name
                   WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' AND c.column_name = 'fk_id_movimiento_fondo'
                   ORDER BY 1""")
    for (tabla,) in cur.fetchall():
        if tabla in gm.COLUMNAS or tabla == "asientos_contables":
            continue
        cur.execute(f'SELECT count(*) FROM public."{tabla}" WHERE fk_id_movimiento_fondo = %s', (movimiento_id,))
        n = cur.fetchone()[0]
        if n:
            salida["otras"][tabla] = n
    return salida


def imprimir(resultado: dict) -> None:
    if resultado["problemas"]:
        for p in resultado["problemas"]:
            print(f"PROBLEMA: {p}")
        return
    ancho = max(len(f"{t}.{c}") for t, c, *_ in resultado["lineas"])
    for tabla, columna, vh, vb, estado in resultado["lineas"]:
        print(f"{estado:<11} {tabla + '.' + columna:<{ancho}}  persona={vh}  bot={vb}")
    print()
    print("RESULTADO:", "IGUAL EN TODO LO QUE TENÍA QUE SER IGUAL" if not resultado["inesperadas"]
          else f"{resultado['inesperadas']} DIFERENCIA(S) INESPERADA(S)")


def perfil(cur) -> None:
    """Cuenta cómo se guardan las columnas sin confirmar, sin mostrar ningún texto."""
    def contar(titulo: str, sql: str) -> None:
        cur.execute(sql)
        print(f"{titulo}:")
        for fila in cur.fetchall():
            print("   ", " | ".join(str(x) for x in fila))
    contar("Movimiento bancario de un movimiento de fondos: forma de nro y obs (cantidad)",
           """SELECT CASE WHEN b.nro_movimiento_cuenta_bancaria IS NULL THEN 'nro NULL' WHEN b.nro_movimiento_cuenta_bancaria = '' THEN 'nro vacio'
                        ELSE 'nro con texto' END,
                     CASE WHEN b.obs_movimiento_cuenta_bancaria IS NULL THEN 'obs NULL' WHEN b.obs_movimiento_cuenta_bancaria = '' THEN 'obs vacia'
                        ELSE 'obs con texto' END,
                     CASE WHEN b.fec_visado_movimiento_cuenta_bancaria IS NULL THEN 'sin visar' ELSE 'visado' END, count(*)
              FROM movimientos_cuenta_bancaria_en_movimientos_fondos v
              JOIN movimientos_cuentas_bancarias b ON b.id_movimiento_cuenta_bancaria = v.fk_id_movimiento_cuenta_bancaria
              GROUP BY 1, 2, 3 ORDER BY 4 DESC""")
    contar("Movimientos de fondos: obs, centro_costos, es_ctacte, genera_ctacte (cantidad)",
           """SELECT CASE WHEN obs_movimiento_fondo IS NULL THEN 'obs NULL' WHEN obs_movimiento_fondo = '' THEN 'obs vacia' ELSE 'obs con texto' END,
                     centro_costos, es_ctacte, genera_ctacte, count(*) FROM movimientos_fondos GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC""")
    contar("Movimientos de fondos: el momento de la carga contra el del movimiento (cantidad)",
           """SELECT CASE WHEN fec_carga_movimiento_fondo < fec_movimiento_fondo THEN 'carga antes'
                         WHEN fec_carga_movimiento_fondo::date = fec_movimiento_fondo::date THEN 'mismo dia' ELSE 'otro dia' END, count(*)
              FROM movimientos_fondos GROUP BY 1 ORDER BY 2 DESC""")
    contar("Movimientos de fondos: cuántos usuarios distintos los cargaron", "SELECT count(DISTINCT fk_id_operario) FROM movimientos_fondos")
    contar("Los cuatro tipos habilitados: genera_asiento y egreso",
           """SELECT id_movimiento_fondo_tipo, genera_asiento, es_egreso, es_ingreso FROM movimientos_fondos_tipos
              WHERE id_movimiento_fondo_tipo IN (50, 17, 58, 13) ORDER BY 1""")
    contar("Efectivo de movimientos de fondos: moneda, equivalente y caja (cantidad)",
           """SELECT e.fk_id_moneda, e.imp_equivalente_peso, e.fk_id_caja, e.es_ctacte, count(*)
              FROM efectivo_medio_de_pago_en_movimientos_fondos v
              JOIN efectivo_medio_de_pago e ON e.id_efectivo_medio_de_pago = v.fk_id_efectivo_medio_de_pago GROUP BY 1, 2, 3, 4""")
    contar("Movimientos bancarios de movimientos de fondos: cuenta, plan, tipo y signo (cantidad)",
           """SELECT b.fk_id_cuenta_bancaria, b.fk_id_plan_de_cuenta, b.tipo_movimiento_cuenta_bancaria, b.imp_movimiento_cuenta_bancaria > 0, count(*)
              FROM movimientos_cuenta_bancaria_en_movimientos_fondos v
              JOIN movimientos_cuentas_bancarias b ON b.id_movimiento_cuenta_bancaria = v.fk_id_movimiento_cuenta_bancaria GROUP BY 1, 2, 3, 4""")
    contar("Asientos colgados de movimientos de fondos, por tipo de movimiento con genera_asiento (cantidad)",
           """SELECT t.genera_asiento, count(a.id_asiento_contable), count(DISTINCT m.id_movimiento_fondo)
              FROM movimientos_fondos m JOIN movimientos_fondos_tipos t ON t.id_movimiento_fondo_tipo = m.fk_id_movimiento_fondo_tipo
              LEFT JOIN asientos_contables a ON a.fk_id_movimiento_fondo = m.id_movimiento_fondo GROUP BY 1""")
    contar("Claves primarias con valor por defecto (el escritor deja que las ponga la base)",
           """SELECT c.relname, pg_get_expr(d.adbin, d.adrelid) IS NOT NULL AS tiene_default
              FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = 1
              LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = 1
              WHERE c.relname IN ('movimientos_fondos', 'efectivo_medio_de_pago', 'efectivo_medio_de_pago_en_movimientos_fondos',
                                  'movimientos_cuentas_bancarias', 'movimientos_cuenta_bancaria_en_movimientos_fondos') AND c.relkind = 'r' ORDER BY 1""")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compara un movimiento de fondos del bot (en una copia) con uno de una persona")
    ap.add_argument("--base", help="la base (la copia) donde está el movimiento del bot; por defecto GARRO_BASE")
    ap.add_argument("--humano", type=int, help="id del movimiento que cargó una persona")
    ap.add_argument("--bot", type=int, help="id del movimiento que cargó el bot")
    ap.add_argument("--base-humano", help="base del movimiento de la persona, si no es la misma (por ejemplo, la real)")
    ap.add_argument("--otro-tipo", action="store_true", help="los dos movimientos son de tipos distintos")
    ap.add_argument("--perfil", action="store_true", help="cuenta cómo se guardan las columnas sin confirmar (solo lectura)")
    ap.add_argument("--config", default=os.path.join(AQUI, g.ARCHIVO_CONFIG))
    args = ap.parse_args(argv)
    if not args.perfil and (args.humano is None or args.bot is None):
        ap.error("hace falta --humano y --bot (o --perfil)")
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass
    try:
        cfg = g.leer_archivo(args.config)
        if args.perfil:
            con = ge.conectar_garro(cfg, solo_lectura=True, base=args.base)
            try:
                perfil(con.cursor())
                con.rollback()
            finally:
                con.close()
            return 0
        con_bot = ge.conectar_garro(cfg, solo_lectura=True, base=args.base)
        con_humano = con_bot if not args.base_humano else ge.conectar_garro(cfg, solo_lectura=True, base=args.base_humano)
        try:
            humano = leer_movimiento(con_humano.cursor(), args.humano)
            bot = leer_movimiento(con_bot.cursor(), args.bot)
            con_bot.rollback()
            con_humano.rollback()
        finally:
            con_bot.close()
            if con_humano is not con_bot:
                con_humano.close()
        resultado = comparar(humano, bot, args.otro_tipo)
        imprimir(resultado)
        return 1 if resultado["problemas"] or resultado["inesperadas"] else 0
    except Exception as e:
        g.escribir_traza()
        print(f"ERROR: {g.traducir_error(e, 'origen')}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
