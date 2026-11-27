"""Tests de garro_indices.py (comando remoto `indices`): catálogos de solo lectura, sin base."""

import os
import re
import unittest
from unittest import mock

import garro_comandos as gc
import garro_indices as gi
import garro_sync as g

AQUI = os.path.dirname(os.path.abspath(__file__))


class CursorFalso:
    """Responde a cada consulta del catálogo según la tabla; guarda las sentencias para revisarlas."""

    def __init__(self, tablas):
        self.tablas, self.sentencias, self.actual, self.ultima = tablas, [], None, []

    def execute(self, sql, params=()):
        self.sentencias.append((sql, params))
        if sql == gi.SQL_TABLA:
            self.actual = params[0]
            self.ultima = [(1,)] if self.tablas.get(params[0]) is not None else []
        elif self.actual is not None:
            datos = self.tablas[self.actual]
            self.ultima = {gi.SQL_INDICES: datos["indices"], gi.SQL_RESTRICCIONES: datos["restricciones"], gi.SQL_TRIGGERS: datos["triggers"],
                           gi.SQL_TRIGGERS_INTERNOS: [(datos["internos"],)], gi.SQL_REGLAS: datos["reglas"]}[sql]

    def fetchone(self):
        return self.ultima[0] if self.ultima else None

    def fetchall(self):
        return list(self.ultima)


def tabla(indices=(), restricciones=(), triggers=(), internos=2, reglas=()):
    return {"indices": list(indices), "restricciones": list(restricciones), "triggers": list(triggers), "internos": internos, "reglas": list(reglas)}


UNICO = ("comprobantes_compras_uq", True, False, "CREATE UNIQUE INDEX comprobantes_compras_uq ON comprobantes_compras USING btree (fk_id_proveedor, nro_comprobante_compra)")
PK = ("comprobantes_compras_pkey", True, True, "CREATE UNIQUE INDEX comprobantes_compras_pkey ON comprobantes_compras USING btree (id_comprobante_compra)")
COMUN = ("comprobantes_compras_fec", False, False, "CREATE INDEX comprobantes_compras_fec ON comprobantes_compras USING btree (fec_comprobante_compra)")


class Lectura(unittest.TestCase):
    def test_lee_indices_restricciones_triggers_y_reglas(self):
        cur = CursorFalso({"t": tabla([PK, UNICO, COMUN], [("t_pkey", "p", "PRIMARY KEY (id)"), ("t_uq", "u", "UNIQUE (a, b)"),
                                                           ("t_fk", "f", "FOREIGN KEY (a) REFERENCES x(id)"), ("t_ck", "c", "CHECK (a > 0)")],
                                      [("trg", "CREATE TRIGGER trg BEFORE INSERT ON t ...")], reglas=[("r", "CREATE RULE r AS ...")])})
        d = gi.leer_tabla(cur, "t")
        self.assertEqual([i["unico"] for i in d["indices"]], [True, True, False])
        self.assertEqual([i["primaria"] for i in d["indices"]], [True, False, False])
        self.assertEqual([r["tipo"] for r in d["restricciones"]], ["PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK"])
        self.assertEqual((len(d["triggers"]), d["triggers_internos"], len(d["reglas"])), (1, 2, 1))

    def test_tabla_inexistente(self):
        self.assertIsNone(gi.leer_tabla(CursorFalso({"t": None}), "t"))

    def test_solo_catalogos_nada_de_filas_de_datos_y_cada_consulta_es_un_select(self):
        sqls = [gi.SQL_TABLA, gi.SQL_INDICES, gi.SQL_RESTRICCIONES, gi.SQL_TRIGGERS, gi.SQL_TRIGGERS_INTERNOS, gi.SQL_REGLAS]
        for sql in sqls:
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"), sql)
            tablas = re.findall(r"\b(?:FROM|JOIN)\s+([a-z_\.]+)", sql)
            self.assertTrue(tablas and all(t.startswith("pg_") for t in tablas), (sql, tablas))
            self.assertNotRegex(sql.upper(), r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT)\b")
        for f in ("pg_get_indexdef", "pg_get_constraintdef", "pg_get_triggerdef", "pg_get_ruledef"):
            self.assertIn(f, "".join(sqls))
        self.assertNotIn("to_regclass", "".join(sqls))  # no existe en PostgreSQL 9.3

    def test_las_tablas_son_las_que_toca_el_escritor(self):
        for t in ("comprobantes_compras", "items_comprobantes_compras", "movimientos_fondos", "efectivo_medio_de_pago",
                  "movimientos_cuentas_bancarias", "pagos", "asientos", "asientos_contables", "pagos_en_comprobantes_compras"):
            self.assertIn(t, gi.TABLAS)
        self.assertEqual(len(gi.TABLAS), len(set(gi.TABLAS)))

    def test_solo_lectura_y_cierra_la_conexion_sin_confirmar_nada(self):
        con = mock.MagicMock()
        con.cursor.return_value.__enter__.return_value = CursorFalso({t: None for t in gi.TABLAS})
        with mock.patch.object(gi.ge, "conectar_garro", return_value=con) as c:
            texto = gi.indices({})
        self.assertTrue(c.call_args.kwargs["solo_lectura"])
        self.assertIsNone(c.call_args.kwargs.get("base"))
        con.commit.assert_not_called()
        con.close.assert_called_once()
        self.assertIn("NO EXISTE", texto)

    def test_cierra_la_conexion_aunque_falle(self):
        con = mock.MagicMock()
        con.cursor.side_effect = RuntimeError("boom")
        with mock.patch.object(gi.ge, "conectar_garro", return_value=con):
            with self.assertRaises(RuntimeError):
                gi.indices({})
        con.close.assert_called_once()


class Formato(unittest.TestCase):
    def test_resumen_primero_con_los_unicos_y_luego_el_detalle(self):
        datos = {"comprobantes_compras": gi.leer_tabla(CursorFalso({"comprobantes_compras": tabla(
                    [PK, UNICO, COMUN], [("c_uq", "u", "UNIQUE (a, b)")], [("trg", "CREATE TRIGGER trg ...")])}), "comprobantes_compras"),
                 "pagos": None}
        t = gi.formatear(datos)
        self.assertLess(t.index("== Resumen"), t.index("== Detalle por tabla"))
        resumen = t[:t.index("== Detalle")]
        self.assertIn("comprobantes_compras: índices únicos (sin PK) 1 | restricciones UNIQUE 1 | triggers 1 | reglas 0", resumen)
        self.assertIn(UNICO[3], resumen)
        self.assertNotIn(PK[3], resumen)  # la PK no es lo que impide duplicados de negocio: va en el detalle
        self.assertNotIn(COMUN[3], resumen)
        self.assertIn("pagos: NO EXISTE", resumen)
        detalle = t[t.index("== Detalle"):]
        self.assertIn("[ÚNICO] [PK]", detalle)
        self.assertIn("[no único]", detalle)
        self.assertIn("UNIQUE c_uq: UNIQUE (a, b)", detalle)
        self.assertIn("Triggers internos (claves foráneas): 2", detalle)

    def test_el_resumen_entra_en_lo_que_sobrevive_al_recorte_de_la_salida(self):
        datos = {f"t{i}": gi.leer_tabla(CursorFalso({f"t{i}": tabla([PK, UNICO, COMUN] * 4, [("x", "f", "FOREIGN KEY (a) REFERENCES y(id)")] * 12)}), f"t{i}")
                 for i in range(15)}
        t = gi.formatear(datos)
        self.assertGreater(len(t), gc.MAX_SALIDA)
        cortado = gc.recortar(t)
        self.assertIn("t0: índices únicos", cortado)
        self.assertIn("t14: índices únicos", t[:t.index("== Detalle")])
        self.assertIn(t[:200], cortado)


class Comando(unittest.TestCase):
    def test_es_un_tipo_de_la_lista_cerrada_con_su_timeout_y_sin_argumentos(self):
        self.assertIn("indices", gc.TIPOS)
        self.assertEqual(gc.TIMEOUTS["indices"], 120)
        with self.assertRaises(gc.ErrorComando):
            gc.validar("indices", {"tabla": "pagos"}, {})
        with self.assertRaises(gc.ErrorComando):
            gc.validar("indices", {"base": "gestion_prueba"}, {})

    def test_no_es_el_escritor_modificado(self):
        [argv] = gc.armar("indices", {}, python="/py", aqui="/pk", config="/pk/.env")
        self.assertEqual(os.path.basename(argv[1]), "garro_indices.py")
        self.assertNotIn("garro_escritor.py", " ".join(argv))
        self.assertNotIn("--escribir", argv)

    def test_corre_y_resuelve_con_la_salida_redactada(self):
        cfg = {"GARRO_PASSWORD": "clave-secreta-1"}
        estado, r = gc.correr({"tipo": "indices", "args": {}}, cfg, config="x", ejecutar=lambda a, t: (0, "ok clave-secreta-1"))
        self.assertEqual((estado, r["salida"]), ("ok", "ok ***"))

    def test_de_verdad_arranca_con_version_y_esta_en_las_huellas(self):
        import actualizar_hashes as ah
        self.assertIn("garro_indices.py", ah.ARCHIVOS)
        codigo, salida = gc.correr_proceso([__import__("sys").executable, os.path.join(AQUI, "garro_indices.py"), "--version"], 60)
        self.assertEqual((codigo, salida.strip()), (0, f"garro_indices {g.VERSION}"))
        self.assertIn("garro_indices", gc.MODULOS)


if __name__ == "__main__":
    unittest.main()
