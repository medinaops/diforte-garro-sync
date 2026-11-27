"""Tests de la comparación de movimientos de fondos (comparar_movimientos.py): sin base ni red."""

import copy
import unittest
from datetime import datetime

import comparar_movimientos as cm
import garro_movimientos as gm
from test_garro_movimientos import AHORA, propuesta, ref_base, transferencia


def leido(plan: gm.Plan, base_id: int, operario: int | None = None, **cambios_mf) -> dict:
    """Lo que `leer_movimiento` devolvería para un movimiento con las filas de ese plan."""
    t_medio, t_vinculo = plan.tablas_medio
    mf = {"id_movimiento_fondo": base_id, **plan.movimiento, **cambios_mf}
    if operario is not None:
        mf["fk_id_operario"] = operario
    medio = {gm.COLUMNAS[t_medio][0]: base_id + 1000, **plan.medio_fila}
    clave = "fk_id_efectivo_medio_de_pago" if plan.medio == "efectivo" else "fk_id_movimiento_cuenta_bancaria"
    vinculo = {gm.COLUMNAS[t_vinculo][0]: base_id + 2000, "fk_id_movimiento_fondo": base_id, clave: base_id + 1000, **plan.vinculo}
    filas = {t: [] for t in gm.TABLAS_ESCRITO}
    filas["movimientos_fondos"], filas[t_medio], filas[t_vinculo] = [mf], [medio], [vinculo]
    return {"filas": filas, "asientos": 0, "otras": {}}


def plan_de(p):
    return gm.planificar(p, ref_base(), AHORA)


class Comparar(unittest.TestCase):
    def test_lo_que_difiere_por_naturaleza_no_es_una_diferencia(self):
        humano = leido(plan_de(propuesta(importe=123456.78, descripcion="periodo 10/2026")), 110, operario=7,
                       fec_movimiento_fondo=datetime(2026, 11, 20, 10, 0, 0), fec_carga_movimiento_fondo=datetime(2026, 11, 20, 10, 1, 5))
        humano["filas"]["efectivo_medio_de_pago"][0]["fecha"] = datetime(2026, 11, 20, 10, 0, 0)
        bot = leido(plan_de(propuesta(importe=1000.5, descripcion="OTRA COSA")), 600, operario=9)
        r = cm.comparar(humano, bot)
        self.assertEqual((r["problemas"], r["inesperadas"]), ([], 0), r["lineas"])
        estados = {(t, c): e for t, c, _, _, e in r["lineas"]}
        self.assertEqual(estados[("movimientos_fondos", "fk_id_operario")], "ESPERADA")
        self.assertEqual(estados[("efectivo_medio_de_pago", "imp_efectivo_medio_de_pago")], "ESPERADA")
        self.assertEqual(estados[("movimientos_fondos", "genera_ctacte")], "IGUAL")
        self.assertEqual(estados[("movimientos_fondos", "centro_costos")], "IGUAL")

    def test_cualquier_otra_columna_distinta_se_marca(self):
        humano = leido(plan_de(propuesta()), 110, operario=7)
        for tabla, columna, valor in (("movimientos_fondos", "genera_ctacte", True), ("movimientos_fondos", "es_ctacte", False),
                                      ("movimientos_fondos", "obs_movimiento_fondo", "x"), ("movimientos_fondos", "fk_id_caja", 2),
                                      ("movimientos_fondos", "es_egreso", False), ("movimientos_fondos", "centro_costos", True),
                                      ("efectivo_medio_de_pago", "imp_equivalente_peso", 2), ("efectivo_medio_de_pago", "fk_id_moneda", 2),
                                      ("efectivo_medio_de_pago", "es_ctacte", False), ("efectivo_medio_de_pago", "fk_id_caja", 2),
                                      ("efectivo_medio_de_pago_en_movimientos_fondos", "fk_id_caja", 2),
                                      ("movimientos_fondos", "fk_id_movimiento_fondo_tipo", 17)):
            bot = leido(plan_de(propuesta(descripcion="OTRA")), 600, operario=9)
            bot["filas"][tabla][0][columna] = valor
            r = cm.comparar(humano, bot)
            self.assertEqual(r["inesperadas"], 1, (tabla, columna))
            self.assertEqual([c for t, c, _, _, e in r["lineas"] if e == "DISTINTA"][:1], [columna])

    def test_otro_tipo_solo_con_la_bandera(self):
        humano = leido(plan_de(propuesta()), 110, operario=7)
        bot = leido(plan_de(propuesta(descripcion="OTRA")), 600, operario=9, fk_id_movimiento_fondo_tipo=17)
        self.assertEqual(cm.comparar(humano, bot)["inesperadas"], 1)
        self.assertEqual(cm.comparar(humano, bot, otro_tipo=True)["inesperadas"], 0)

    def test_transferencia_compara_el_movimiento_bancario(self):
        humano = leido(plan_de(transferencia(importe=54321.09)), 150, operario=8)
        bot = leido(plan_de(transferencia(importe=250000, descripcion="OTRA")), 700, operario=9)
        self.assertEqual(cm.comparar(humano, bot)["inesperadas"], 0)
        for columna, valor in (("fk_id_plan_de_cuenta", 300), ("tipo_movimiento_cuenta_bancaria", "DEPOSITO BANCARIO"),
                               ("fk_id_cuenta_bancaria", 12), ("nro_movimiento_cuenta_bancaria", "123"),
                               ("obs_movimiento_cuenta_bancaria", "algo"), ("fec_visado_movimiento_cuenta_bancaria", datetime(2026, 1, 1))):
            malo = copy.deepcopy(bot)
            malo["filas"]["movimientos_cuentas_bancarias"][0][columna] = valor
            self.assertEqual(cm.comparar(humano, malo)["inesperadas"], 1, columna)

    def test_efectivo_contra_transferencia_no_se_compara(self):
        r = cm.comparar(leido(plan_de(propuesta()), 100), leido(plan_de(transferencia()), 600))
        self.assertTrue(r["problemas"] and not r["lineas"])
        self.assertIn("efectivo", r["problemas"][0])

    def test_asiento_filas_ajenas_o_movimiento_inexistente_son_problemas(self):
        bueno = leido(plan_de(propuesta()), 100)
        con_asiento = copy.deepcopy(bueno)
        con_asiento["asientos"] = 1
        self.assertTrue(cm.comparar(bueno, con_asiento)["problemas"])
        con_otras = copy.deepcopy(bueno)
        con_otras["otras"] = {"cheques_en_movimientos_fondos": 1}
        self.assertTrue(cm.comparar(con_otras, bueno)["problemas"])
        vacio = copy.deepcopy(bueno)
        vacio["filas"] = {t: [] for t in gm.TABLAS_ESCRITO}
        self.assertTrue(cm.comparar(bueno, vacio)["problemas"])
        doble = copy.deepcopy(bueno)
        doble["filas"]["efectivo_medio_de_pago"].append(dict(doble["filas"]["efectivo_medio_de_pago"][0]))
        self.assertTrue(cm.comparar(doble, bueno)["problemas"])

    def test_el_bot_tiene_que_guardar_el_medio_con_el_mismo_momento_que_el_movimiento(self):
        humano = leido(plan_de(propuesta()), 100)
        humano["filas"]["efectivo_medio_de_pago"][0]["fecha"] = datetime(2026, 1, 1)      # en la persona solo informa
        bot = leido(plan_de(propuesta(descripcion="OTRA")), 600)
        r = cm.comparar(humano, bot)
        self.assertEqual(r["inesperadas"], 0)
        self.assertIn("INFORMATIVA", [e for *_, e in r["lineas"]])
        bot["filas"]["efectivo_medio_de_pago"][0]["fecha"] = datetime(2026, 1, 1)
        r = cm.comparar(leido(plan_de(propuesta()), 100), bot)
        self.assertEqual(r["inesperadas"], 1)
        self.assertEqual([c for _, c, _, _, e in r["lineas"] if e == "DISTINTA" and "(bot)" in c], ["fecha == movimiento.fec_movimiento_fondo (bot)"])

    def test_los_textos_libres_se_muestran_solo_por_su_forma(self):
        self.assertEqual(cm.forma_de_texto(None), "NULL")
        self.assertEqual(cm.forma_de_texto(""), "''")
        self.assertEqual(cm.forma_de_texto("abc123"), "texto(6)")
        self.assertNotIn("abc123", cm.mostrar("obs_movimiento_cuenta_bancaria", "abc123"))
        self.assertEqual(cm.mostrar("fk_id_caja", 1), "1")


if __name__ == "__main__":
    unittest.main()
