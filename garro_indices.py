"""Índices, restricciones, triggers y reglas de las tablas que toca el escritor (comando remoto `indices`).

Sirve para saber con certeza si Gestión Integral impide cargar dos veces el mismo comprobante (índices únicos,
restricciones UNIQUE y CHECK, triggers). Es de SOLO LECTURA: sesión de solo lectura a la base real, solo catálogos de
PostgreSQL 9.3 (pg_class, pg_index, pg_constraint, pg_trigger, pg_rewrite) y nada de filas de datos.

    python garro_indices.py            # imprime un resumen y el detalle por tabla
"""

from __future__ import annotations

import argparse
import os
import sys

import garro_escritor as ge
import garro_movimientos as gm
import garro_sync as g

AQUI = os.path.dirname(os.path.abspath(__file__))
# Las tablas que escribe el escritor (compras y movimientos de fondos) más las que el programa relaciona con ellas.
TABLAS = tuple(dict.fromkeys(ge.TABLAS_ESCRITO + gm.TABLAS_ESCRITO + ("pagos", "asientos")))
TIPOS_RESTRICCION = {"p": "PRIMARY KEY", "u": "UNIQUE", "c": "CHECK", "f": "FOREIGN KEY", "x": "EXCLUDE", "t": "CONSTRAINT TRIGGER"}

SQL_TABLA = ("SELECT c.oid FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace "
             "WHERE n.nspname = 'public' AND c.relname = %s AND c.relkind = 'r'")
SQL_INDICES = ("SELECT ic.relname, ix.indisunique, ix.indisprimary, pg_get_indexdef(ix.indexrelid) "
               "FROM pg_index AS ix JOIN pg_class AS ic ON ic.oid = ix.indexrelid WHERE ix.indrelid = %s ORDER BY ic.relname")
SQL_RESTRICCIONES = ("SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = %s "
                     "ORDER BY contype, conname")
SQL_TRIGGERS = ("SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger WHERE tgrelid = %s AND NOT tgisinternal ORDER BY tgname")
SQL_TRIGGERS_INTERNOS = "SELECT count(*) FROM pg_trigger WHERE tgrelid = %s AND tgisinternal"
SQL_REGLAS = ("SELECT rulename, pg_get_ruledef(oid) FROM pg_rewrite WHERE ev_class = %s AND rulename <> '_RETURN' ORDER BY rulename")


def leer_tabla(cur, nombre: str) -> dict | None:
    """Lo que dice el catálogo de una tabla, o None si no existe. Nada de filas."""
    cur.execute(SQL_TABLA, (nombre,))
    fila = cur.fetchone()
    if fila is None:
        return None
    oid = fila[0]
    cur.execute(SQL_INDICES, (oid,))
    indices = [{"nombre": n, "unico": bool(u), "primaria": bool(p), "def": d} for n, u, p, d in cur.fetchall()]
    cur.execute(SQL_RESTRICCIONES, (oid,))
    restricciones = [{"nombre": n, "tipo": TIPOS_RESTRICCION.get(t, t), "def": d} for n, t, d in cur.fetchall()]
    cur.execute(SQL_TRIGGERS, (oid,))
    triggers = [{"nombre": n, "def": d} for n, d in cur.fetchall()]
    cur.execute(SQL_TRIGGERS_INTERNOS, (oid,))
    internos = int(cur.fetchone()[0])
    cur.execute(SQL_REGLAS, (oid,))
    reglas = [{"nombre": n, "def": d} for n, d in cur.fetchall()]
    return {"indices": indices, "restricciones": restricciones, "triggers": triggers, "triggers_internos": internos, "reglas": reglas}


def formatear(datos: dict[str, dict | None]) -> str:
    """Texto legible: primero un resumen de lo que impediría duplicados, después el detalle por tabla."""
    lineas = ["== Resumen: índices únicos y restricciones UNIQUE (sin la clave primaria), triggers y reglas =="]
    for tabla, t in datos.items():
        if t is None:
            lineas.append(f"{tabla}: NO EXISTE")
            continue
        unicos = [i for i in t["indices"] if i["unico"] and not i["primaria"]]
        uniques = [r for r in t["restricciones"] if r["tipo"] == "UNIQUE"]
        lineas.append(f"{tabla}: índices únicos (sin PK) {len(unicos)} | restricciones UNIQUE {len(uniques)} | "
                      f"triggers {len(t['triggers'])} | reglas {len(t['reglas'])}")
        lineas += [f"    {i['def']}" for i in unicos]
        lineas += [f"    {r['nombre']}: UNIQUE {r['def']}" for r in uniques]
        lineas += [f"    TRIGGER {x['def']}" for x in t["triggers"]]
        lineas += [f"    REGLA {x['def']}" for x in t["reglas"]]
    lineas += ["", "== Detalle por tabla =="]
    for tabla, t in datos.items():
        lineas.append(f"### {tabla}")
        if t is None:
            lineas.append("  (la tabla no existe)")
            continue
        lineas.append("  Índices:" + ("" if t["indices"] else " ninguno"))
        lineas += [f"    {'[ÚNICO]' if i['unico'] else '[no único]'}{' [PK]' if i['primaria'] else ''} {i['def']}" for i in t["indices"]]
        lineas.append("  Restricciones:" + ("" if t["restricciones"] else " ninguna"))
        lineas += [f"    {r['tipo']} {r['nombre']}: {r['def']}" for r in t["restricciones"]]
        lineas.append("  Triggers propios: " + ("ninguno" if not t["triggers"] else ""))
        lineas += [f"    {x['def']}" for x in t["triggers"]]
        lineas.append(f"  Triggers internos (claves foráneas): {t['triggers_internos']}")
        lineas.append("  Reglas: " + ("ninguna" if not t["reglas"] else ""))
        lineas += [f"    {x['def']}" for x in t["reglas"]]
    return "\n".join(lineas)


def indices(cfg: dict[str, str]) -> str:
    con = ge.conectar_garro(cfg, solo_lectura=True)
    try:
        with con.cursor() as cur:
            datos = {t: leer_tabla(cur, t) for t in TABLAS}
        con.rollback()
    finally:
        con.close()
    return formatear(datos)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Índices, restricciones, triggers y reglas de las tablas del escritor (solo lectura)")
    ap.add_argument("--version", action="version", version=f"garro_indices {g.VERSION}")
    ap.add_argument("--config", default=os.path.join(AQUI, g.ARCHIVO_CONFIG))
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass
    try:
        print(indices(g.leer_archivo(args.config)))
        return 0
    except Exception as e:
        g.escribir_traza()
        print(f"ERROR: {g.traducir_error(e, 'origen')}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
