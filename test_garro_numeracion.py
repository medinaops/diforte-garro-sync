"""Numeración automática del Remito X (propuesta.numerar = «siguiente»): sin base ni red."""

import unittest
from datetime import datetime

import garro_cargas as gc
import garro_escritor as ge
from test_garro_cargas import AHORA, REF_X, ref_base, remito_x


class CursorComprobantes:
    """comprobantes_compras en memoria: contesta las dos consultas de siguiente_numero_remito (y guarda lo pedido)."""

    def __init__(self, filas):
        self.filas = filas  # {id, prov, tipo, suc, nro, estado}
        self.sentencias, self.salida = [], []

    def execute(self, sql, params=()):
        self.sentencias.append((sql, params))
        activos = [f for f in self.filas if f["estado"] == "activo" and f["prov"] == params[0]]
        if "LIMIT 1" in sql:
            hay = [f for f in activos if f["tipo"] == params[1] and ("nro_suc_comprobante_compra = %s" not in sql or f["suc"] == params[2])]
            ultimo = max(hay, key=lambda f: f["id"]) if hay else None  # el id más alto, NO el número más alto
            self.salida = [(ultimo["suc"], ultimo["nro"])] if ultimo else []
        else:
            self.salida = [(n,) for n in sorted({f["nro"] for f in activos if f["nro"] >= params[1]})]

    def fetchone(self):
        return self.salida[0] if self.salida else None

    def fetchall(self):
        return list(self.salida)


def remitos(prov, *pares, tipo=5, estado="activo", desde=1):
    """pares (suc, nro) en el orden en que se cargaron (id creciente)."""
    return [{"id": desde + i, "prov": prov, "tipo": tipo, "suc": s, "nro": n, "estado": estado} for i, (s, n) in enumerate(pares)]


def siguiente(filas, prov=1, pv=None):
    return ge.siguiente_numero_remito(CursorComprobantes(filas), prov, pv)


class Calculo(unittest.TestCase):
    def test_el_ultimo_por_id_y_no_el_maximo(self):
        # cargados en este orden: 10, 20, 15 -> el último por id es 15 -> 16 (el máximo daría 21)
        self.assertEqual(siguiente(remitos(1, (1, 10), (1, 20), (1, 15)))["numero"], 16)
        # Caja: último 3112 -> 3113
        self.assertEqual(siguiente(remitos(1, (1, 3112)))["numero"], 3113)

    def test_los_ejemplos_del_equipo_cargados_del_mas_nuevo_al_mas_viejo(self):
        # «EPEC, últimos 77, 4, 3»: el más nuevo es 77 -> 78. «Fletes: 5, 7, 1111»: el más nuevo es 5 -> 6.
        self.assertEqual(siguiente(remitos(1, (1, 3), (1, 4), (1, 77)))["numero"], 78)
        self.assertEqual(siguiente(remitos(1, (1, 1111), (1, 7), (1, 5)))["numero"], 6)

    def test_salta_los_usados_por_cualquier_comprobante_activo_del_proveedor(self):
        filas = remitos(1, (1, 77)) + [
            {"id": 50, "prov": 1, "tipo": 1, "suc": 9, "nro": 78, "estado": "activo"},   # una factura con ese número, otro punto de venta
            {"id": 51, "prov": 1, "tipo": 5, "suc": 3, "nro": 79, "estado": "activo"}]   # otro remito con otro punto de venta
        self.assertEqual(siguiente(filas)["numero"], 80)
        # los anulados no cuentan como usados, ni como último, y los de otro proveedor tampoco
        filas = remitos(1, (1, 77)) + remitos(1, (1, 78), estado="anulado", desde=10) + remitos(2, (1, 78), desde=20)
        self.assertEqual(siguiente(filas)["numero"], 78)

    def test_proveedor_sin_remitos_arranca_en_el_punto_de_venta_1_y_el_numero_1(self):
        r = siguiente([])
        self.assertEqual((r["punto_venta"], r["numero"]), (1, 1))
        # con comprobantes pero de otro tipo (una factura): sigue sin tener remitos; salta el 1 si ya lo usa
        filas = [{"id": 1, "prov": 1, "tipo": 1, "suc": 4, "nro": 1, "estado": "activo"}]
        r = siguiente(filas)
        self.assertEqual((r["punto_venta"], r["numero"]), (1, 2))

    def test_el_punto_de_venta_es_el_del_ultimo_remito_o_el_explicito(self):
        filas = remitos(1, (2, 10), (7, 5), (2, 11))  # el último cargado es del punto 2, número 11
        r = siguiente(filas)
        self.assertEqual((r["punto_venta"], r["numero"]), (2, 12))
        # explícito: el último remito de ESE punto de venta, por id (el 7 tiene un solo remito: 5) -> 6
        r = siguiente(filas, pv=7)
        self.assertEqual((r["punto_venta"], r["numero"]), (7, 6))
        # explícito sin remitos en ese punto: 1 (o el primero libre)
        self.assertEqual(siguiente(filas, pv=9)["numero"], 1)
        self.assertEqual(siguiente(filas + remitos(1, (3, 1), desde=90), pv=9)["numero"], 2)

    def test_numero_lindo_y_limite(self):
        self.assertEqual(siguiente(remitos(1, (3, 41)))["numero_lindo"], "0003-00000042")
        with self.assertRaises(gc.NoCargable):
            siguiente(remitos(1, (1, 99999999)))

    def test_solo_activos_y_del_proveedor_en_cada_consulta(self):
        c = CursorComprobantes(remitos(1, (1, 5)))
        ge.siguiente_numero_remito(c, 1, None)
        for sql, _ in c.sentencias:
            self.assertIn("estado_comprobante_compra = 'activo'", sql)
            self.assertIn("fk_id_proveedor = %s", sql)
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"))


def con_numeracion(carga_propuesta, numeracion=None, **ref):
    return gc.planificar(carga_propuesta, ref_base(**{**REF_X, "numeracion": numeracion, **ref}), AHORA)


def propuesta_numerada(**cambios):
    c = remito_x(punto_venta=None, numero=None, numerar="siguiente")
    c["propuesta"].update(cambios)
    return c


class Planificar(unittest.TestCase):
    def test_usa_el_numero_asignado_y_lo_informa_en_el_plan(self):
        plan = con_numeracion(propuesta_numerada(), {"punto_venta": 3, "numero": 78})
        self.assertEqual((plan.comprobante["nro_suc_comprobante_compra"], plan.comprobante["nro_comprobante_compra"]), (3, 78))
        self.assertEqual(plan.resumen()["numeracion"], {"punto_venta": 3, "numero": 78, "numero_lindo": "0003-00000078"})

    def test_sin_numerar_todo_igual_que_antes(self):
        plan = gc.planificar(remito_x(), ref_base(**REF_X), AHORA)
        self.assertNotIn("numeracion", plan.resumen())
        self.assertEqual((plan.comprobante["nro_suc_comprobante_compra"], plan.comprobante["nro_comprobante_compra"]), (7, 4521))
        for malo in ({"punto_venta": None}, {"numero": None}):
            with self.assertRaises(gc.NoCargable) as c:
                gc.planificar(remito_x(**malo), ref_base(**REF_X), AHORA)
            self.assertEqual(c.exception.codigo, "numero")

    def rechazo(self, codigo, carga, **ref):
        with self.assertRaises(gc.NoCargable) as c:
            gc.planificar(carga, ref_base(**{**REF_X, "numeracion": {"punto_venta": 1, "numero": 1}, **ref}), AHORA)
        self.assertEqual(c.exception.codigo, codigo)

    def test_otro_tipo_con_numerar_se_rechaza(self):
        factura = {"propuesta": {"tipo_garro_id": 1, "cuit": "30-55512348-1", "punto_venta": None, "numero": None, "numerar": "siguiente",
                                 "fecha": "2026-11-20", "descuento_pct": 0, "requiere_revision": False, "items": []}, "total_pesos": 1}
        with self.assertRaises(gc.NoCargable) as c:
            gc.planificar(factura, ref_base(), AHORA)
        self.assertEqual(c.exception.codigo, "numerar_tipo")
        self.rechazo("tipo_no_habilitado", propuesta_numerada(tipo_garro_id=9))

    def test_valores_raros_de_numerar_o_con_numero_se_rechazan(self):
        for valor in ("ultimo", "SIGUIENTE", True, 1, "", ["siguiente"]):
            self.rechazo("numerar", propuesta_numerada(numerar=valor))
        self.rechazo("numerar", propuesta_numerada(numero=12))
        for pv in (0, -1, 100000, "x", 1.5):
            self.rechazo("numerar", propuesta_numerada(punto_venta=pv))

    def test_punto_de_venta_explicito_valido(self):
        plan = con_numeracion(propuesta_numerada(punto_venta=7), {"punto_venta": 7, "numero": 6})
        self.assertEqual(plan.comprobante["nro_suc_comprobante_compra"], 7)

    def test_sin_calculo_no_se_carga(self):
        self.rechazo("numerar", propuesta_numerada(), numeracion=None)

    def test_numerar_null_equivale_a_no_numerar(self):
        plan = gc.planificar(remito_x(numerar=None), ref_base(**REF_X), AHORA)
        self.assertEqual(plan.comprobante["nro_comprobante_compra"], 4521)


class PosibleDuplicado(unittest.TestCase):
    """El número es nuevo por construcción: «ya_cargado» nunca dispara; la guarda mira fecha y total del mismo proveedor."""

    NUM = {"punto_venta": 3, "numero": 78}

    def planificar(self, remitos, total=1000.0, **ref):
        return gc.planificar(propuesta_numerada(), ref_base(**{**REF_X, "numeracion": self.NUM, "remitos_del_dia": remitos, **ref}), AHORA)

    def test_mismo_proveedor_fecha_y_total_es_posible_duplicado_con_id_y_numero_del_existente(self):
        with self.assertRaises(gc.NoCargable) as c:
            self.planificar([{"id": 41, "punto_venta": 2, "numero": 77, "centavos": 100000}])
        self.assertEqual(c.exception.codigo, "posible_duplicado")
        self.assertIn("41", c.exception.mensaje)
        self.assertIn("0002-00000077", c.exception.mensaje)

    def test_tolera_un_centavo_de_diferencia_y_no_mas(self):
        for centavos in (99999, 100001):
            with self.assertRaises(gc.NoCargable):
                self.planificar([{"id": 1, "punto_venta": 1, "numero": 1, "centavos": centavos}])
        self.planificar([{"id": 1, "punto_venta": 1, "numero": 1, "centavos": 100002}])
        self.planificar([{"id": 1, "punto_venta": 1, "numero": 1, "centavos": 99998}])

    def test_otro_total_o_sin_remitos_ese_dia_se_carga(self):
        self.planificar([])
        self.planificar([{"id": 1, "punto_venta": 1, "numero": 1, "centavos": 250000}])

    def test_alcanza_uno_solo_entre_varios(self):
        with self.assertRaises(gc.NoCargable):
            self.planificar([{"id": 1, "punto_venta": 1, "numero": 1, "centavos": 5}, {"id": 2, "punto_venta": 1, "numero": 2, "centavos": 100000}])

    def test_sin_numerar_la_guarda_no_corre(self):
        plan = gc.planificar(remito_x(), ref_base(**{**REF_X, "remitos_del_dia": [{"id": 1, "punto_venta": 1, "numero": 1, "centavos": 100000}]}), AHORA)
        self.assertEqual(plan.comprobante["nro_comprobante_compra"], 4521)

    def test_la_consulta_de_remitos_del_dia_es_de_solo_lectura_y_filtra_lo_justo(self):
        class Cur:
            sql = params = None
            def execute(self, sql, params=()):
                Cur.sql, Cur.params = sql, params
            def fetchall(self):
                return [(41, 2, 77, 100000)]
        r = ge.remitos_del_dia(Cur(), 9, datetime(2026, 11, 20).date())
        self.assertEqual(r, [{"id": 41, "punto_venta": 2, "numero": 77, "centavos": 100000}])
        for t in ("estado_comprobante_compra = 'activo'", "fk_id_proveedor = %s", "fk_id_tipo_comprobante_compra = %s", "::date = %s"):
            self.assertIn(t, Cur.sql)
        self.assertNotIn("fk_id_operario", Cur.sql)
        ge.remitos_del_dia(Cur(), 9, datetime(2026, 11, 20).date(), operario_id=31)
        self.assertIn("c.fk_id_operario = %s", Cur.sql)
        self.assertEqual(Cur.params[-1], 31)


if __name__ == "__main__":
    unittest.main()
