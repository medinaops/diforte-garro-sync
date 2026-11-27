"""Tests del planificador de movimientos de fondos (garro_movimientos.py): unittest de la stdlib, sin base ni red.

Correr desde esta carpeta:  python -m pytest -q   (o:  python -m unittest -v test_garro_movimientos)
"""

import json
import os
import unittest
from datetime import date, datetime

import garro_cargas as gc
import garro_movimientos as gm

AHORA = datetime(2026, 11, 26, 16, 5, 9, 654321)


def ref_base(**cambios) -> gm.Referencias:
    datos = dict(
        tipo={"id": 50, "descripcion": "TIPO DE PRUEBA", "es_ingreso": False, "es_egreso": True, "genera_asiento": False},
        operario_id=77, caja={"id": 1, "habilitada": True},
        cuenta_bancaria={"id": 11, "activa": True, "plan": 109}, existentes=[], largos={})
    datos.update(cambios)
    return gm.Referencias(**datos)


def propuesta(**cambios) -> dict:
    p = {"clase": "movimiento_fondo", "tipo_mov_id": 50, "tipo_mov_nombre": "TIPO DE PRUEBA",
         "descripcion": "COM E IND PER 10/2026", "periodo": "2026-10", "importe": 123456.78, "medio": "efectivo",
         "caja": 1, "cuenta_bancaria_id": None, "fecha": "2026-11-26", "vencimiento": "2026-11-20",
         "organismo": "MUNICIPALIDAD", "requiere_revision": False, "posible_duplicado": False}
    p.update(cambios)
    return p


def transferencia(**cambios) -> dict:
    return propuesta(**{"medio": "transferencia", "cuenta_bancaria_id": 11, **cambios})


def existente(id=1, descripcion="OTRA COSA", fecha=datetime(2026, 11, 3, 9, 0, 0), importe=100.0, tipo=50, egreso=True) -> dict:
    return {"id": id, "tipo": tipo, "descripcion": descripcion, "fecha": fecha, "importe": importe, "egreso": egreso}


def rechaza_con(test, p, codigo, ref):
    with test.assertRaises(gc.NoCargable) as cm:
        gm.planificar(p, ref, AHORA)
    test.assertEqual(cm.exception.codigo, codigo, cm.exception.mensaje)
    return cm.exception


def rechaza(test, p, codigo, **ref):
    with test.assertRaises(gc.NoCargable) as cm:
        gm.planificar(p, ref_base(**ref), AHORA)
    test.assertEqual(cm.exception.codigo, codigo, cm.exception.mensaje)
    return cm.exception


class PlanEfectivo(unittest.TestCase):
    def test_reproduce_las_filas_del_programa(self):
        plan = gm.planificar(propuesta(), ref_base(), AHORA)
        momento = datetime(2026, 11, 26, 16, 5, 9)
        self.assertEqual(plan.movimiento, {
            "fk_id_movimiento_fondo_tipo": 50, "fec_movimiento_fondo": momento, "fk_id_operario": 77,
            "obs_movimiento_fondo": "", "fk_id_caja": 1, "es_ingreso": False, "es_egreso": True,
            "des_movimiento_fondo": "COM E IND PER 10/2026", "es_ctacte": True,
            "fec_carga_movimiento_fondo": momento, "genera_ctacte": False, "centro_costos": False})
        self.assertEqual(plan.medio_fila, {
            "imp_efectivo_medio_de_pago": 123456.78, "imp_equivalente_peso": 1, "fk_id_moneda": 1, "fecha": momento,
            "fk_id_caja": 1, "es_ctacte": True})
        self.assertEqual(plan.vinculo, {"fk_id_caja": 1})
        self.assertEqual(plan.tablas_medio, gm.TABLAS_EFECTIVO)

    def test_las_columnas_son_exactamente_las_de_la_receta_menos_la_clave(self):
        plan = gm.planificar(propuesta(), ref_base(), AHORA)
        self.assertEqual(set(plan.movimiento), set(gm.COLUMNAS["movimientos_fondos"][1:]))
        self.assertEqual(set(plan.medio_fila), set(gm.COLUMNAS["efectivo_medio_de_pago"][1:]))
        self.assertEqual(set(plan.vinculo) | {"fk_id_movimiento_fondo", "fk_id_efectivo_medio_de_pago"},
                         set(gm.COLUMNAS["efectivo_medio_de_pago_en_movimientos_fondos"][1:]))

    def test_la_fecha_de_la_propuesta_con_la_hora_de_la_carga(self):
        plan = gm.planificar(propuesta(fecha="2026-10-30"), ref_base(), AHORA)
        self.assertEqual(plan.movimiento["fec_movimiento_fondo"], datetime(2026, 10, 30, 16, 5, 9))
        self.assertEqual(plan.medio_fila["fecha"], datetime(2026, 10, 30, 16, 5, 9))
        self.assertEqual(plan.movimiento["fec_carga_movimiento_fondo"], datetime(2026, 11, 26, 16, 5, 9))

    def test_resumen_sin_asiento_y_sin_datos_de_mas(self):
        r = gm.planificar(propuesta(), ref_base(), AHORA).resumen()
        self.assertEqual((r["clase"], r["medio"], r["importe"], r["asiento"]), ("movimiento_fondo", "efectivo", 123456.78, False))

    def test_los_cuatro_tipos_habilitados(self):
        for tipo in (50, 17, 58, 13):
            plan = gm.planificar(propuesta(tipo_mov_id=tipo), ref_base(tipo={"id": tipo, "es_ingreso": False, "es_egreso": True,
                                                                               "genera_asiento": False}), AHORA)
            self.assertEqual(plan.movimiento["fk_id_movimiento_fondo_tipo"], tipo)

    def test_la_descripcion_queda_en_mayusculas_y_en_una_linea(self):
        plan = gm.planificar(propuesta(descripcion="  periodo   10/2026\n municipal "), ref_base(), AHORA)
        self.assertEqual(plan.movimiento["des_movimiento_fondo"], "PERIODO 10/2026 MUNICIPAL")


class PlanTransferencia(unittest.TestCase):
    def test_reproduce_el_movimiento_bancario(self):
        plan = gm.planificar(transferencia(importe=500000), ref_base(), AHORA)
        momento = datetime(2026, 11, 26, 16, 5, 9)
        self.assertEqual(plan.medio_fila, {
            "nro_movimiento_cuenta_bancaria": "", "imp_movimiento_cuenta_bancaria": 500000,
            "obs_movimiento_cuenta_bancaria": "", "fec_movimiento_cuenta_bancaria": momento,
            "fk_id_cuenta_bancaria": 11, "fk_id_plan_de_cuenta": 109, "fec_visado_movimiento_cuenta_bancaria": None,
            "tipo_movimiento_cuenta_bancaria": "TRANSFERENCIA BANCARIA"})
        self.assertGreater(plan.medio_fila["imp_movimiento_cuenta_bancaria"], 0)   # el egreso va con signo positivo
        self.assertEqual(plan.vinculo, {"fk_id_caja": 1})
        self.assertEqual(plan.tablas_medio, gm.TABLAS_BANCO)
        self.assertEqual(set(plan.medio_fila), set(gm.COLUMNAS["movimientos_cuentas_bancarias"][1:]))

    def test_el_movimiento_es_el_mismo_que_en_efectivo(self):
        a = gm.planificar(propuesta(), ref_base(), AHORA).movimiento
        b = gm.planificar(transferencia(), ref_base(), AHORA).movimiento
        self.assertEqual(a, b)


class Rechazos(unittest.TestCase):
    def test_tipo_fuera_de_los_cuatro(self):
        for tipo in (62, 69, 0, -50, 50.5, "50", None, True, [50]):
            rechaza(self, propuesta(tipo_mov_id=tipo), "tipo_no_habilitado")

    def test_tipo_que_no_existe_en_el_programa_o_es_otro(self):
        rechaza(self, propuesta(), "tipo_desconocido", tipo=None)
        rechaza(self, propuesta(), "tipo_desconocido", tipo={"id": 17, "es_ingreso": False, "es_egreso": True, "genera_asiento": False})

    def test_tipo_que_no_es_un_egreso(self):
        for tipo in ({"es_ingreso": True, "es_egreso": False}, {"es_ingreso": False, "es_egreso": False},
                     {"es_ingreso": True, "es_egreso": True}, {"es_ingreso": None, "es_egreso": None}):
            rechaza(self, propuesta(), "tipo_no_egreso", tipo={"id": 50, "genera_asiento": False, **tipo})

    def test_tipo_con_asiento_o_con_el_dato_dudoso(self):
        for genera in (True, None, 0, "f"):
            rechaza(self, propuesta(), "tipo_con_asiento",
                    tipo={"id": 50, "es_ingreso": False, "es_egreso": True, "genera_asiento": genera})

    def test_revision_y_posible_duplicado_de_la_aprobacion(self):
        for valor in (True, None, "no", 0):
            rechaza(self, propuesta(requiere_revision=valor), "requiere_revision")
        p = propuesta()
        del p["requiere_revision"]
        rechaza(self, p, "requiere_revision")
        for valor in (True, 1, "si"):
            rechaza(self, propuesta(posible_duplicado=valor), "posible_duplicado")
        gm.planificar(propuesta(posible_duplicado=None), ref_base(), AHORA)

    def test_sin_el_usuario_del_bot(self):
        rechaza(self, propuesta(), "sin_operario", operario_id=None)

    def test_medio_no_habilitado(self):
        for medio in ("cheque", "tarjeta", "", None, "Efectivo", ["efectivo"]):
            rechaza(self, propuesta(medio=medio), "medio_no_habilitado")

    def test_caja(self):
        for caja in (2, 0, None, "1", 1.5, True):
            rechaza(self, propuesta(caja=caja), "caja_invalida")
        rechaza(self, propuesta(), "caja_invalida", caja=None)
        rechaza(self, propuesta(), "caja_invalida", caja={"id": 1, "habilitada": False})
        rechaza(self, propuesta(), "caja_invalida", caja={"id": 2, "habilitada": True})

    def test_cuenta_bancaria(self):
        for cuenta in (12, None, 0, "11", 11.5, True):
            rechaza(self, transferencia(cuenta_bancaria_id=cuenta), "cuenta_invalida")
        rechaza(self, transferencia(), "cuenta_invalida", cuenta_bancaria=None)
        rechaza(self, transferencia(), "cuenta_invalida", cuenta_bancaria={"id": 11, "activa": False, "plan": 109})
        rechaza(self, transferencia(), "cuenta_invalida", cuenta_bancaria={"id": 11, "activa": None, "plan": 109})
        rechaza(self, transferencia(), "cuenta_invalida", cuenta_bancaria={"id": 11, "activa": True, "plan": 300})
        rechaza(self, transferencia(), "cuenta_invalida", cuenta_bancaria={"id": 12, "activa": True, "plan": 109})

    def test_efectivo_con_cuenta_bancaria_es_incoherente(self):
        rechaza(self, propuesta(cuenta_bancaria_id=11), "cuenta_invalida")

    def test_importe(self):
        for importe in (0, -1, -0.01, 0.001, 0.005, 123456.784, 1e10, 1e11, 10 ** 400, float("nan"), float("inf"), "500", None, True,
                        [500], {"v": 1}, 0.30000000000000004):
            rechaza(self, propuesta(importe=importe), "importe")
        for importe in (0.01, 1, 9999999999.99, 123456.78, 500000):
            gm.planificar(propuesta(importe=importe, descripcion=f"PAGO {importe}"), ref_base(), AHORA)

    def test_fecha(self):
        for fecha in ("2026-11-27", "2027-01-01", "2026-09-26", "2020-01-01", "2026-13-01", "20261126", "26/11/2026",
                      "2026-11-26T10:00", "", None, 20261126, True):
            rechaza(self, propuesta(fecha=fecha), "fecha")
        self.assertEqual(gm.planificar(propuesta(fecha="2026-09-27"), ref_base(), AHORA).fecha, date(2026, 9, 27))  # 60 días
        rechaza(self, propuesta(fecha="2026-09-26"), "fecha")                                                     # 61 días
        gm.planificar(propuesta(fecha="2026-11-26"), ref_base(), AHORA)                                           # hoy

    def test_periodo(self):
        for periodo in (None, "", "2026-13", "2026-00", "2026-1", "10/2026", "2026-10-01", 202610, "AAAA-MM"):
            rechaza(self, propuesta(periodo=periodo), "periodo")

    def test_descripcion(self):
        for descripcion in (None, "", "   ", 5, ["x"], "\n\t"):
            rechaza(self, propuesta(descripcion=descripcion), "descripcion")
        rechaza(self, propuesta(descripcion="A" * 101), "descripcion")
        rechaza(self, propuesta(descripcion="A" * 80), "descripcion", largos={"movimientos_fondos.des_movimiento_fondo": 50})
        self.assertEqual(len(gm.planificar(propuesta(descripcion="A" * 100), ref_base(), AHORA).descripcion), 100)

    def test_sin_propuesta(self):
        for p in (None, [], "x", 5):
            rechaza(self, p, "sin_propuesta")

    def test_clave_mal_formada_no_rompe(self):
        rechaza(self, {}, "tipo_no_habilitado")
        rechaza(self, {"tipo_mov_id": 50}, "requiere_revision")


def casos_dorados() -> list[dict]:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "casos_duplicados.json"), encoding="utf-8") as f:
        return json.load(f)["casos"]


class Duplicados(unittest.TestCase):
    """Los mismos casos que se corren contra compras_asistente_movimientos_parecidos (mig 666)."""

    def candidatos(self, caso):
        return [{"id": n + 1, "tipo": c["tipo"], "descripcion": c["descripcion"], "fecha": datetime.fromisoformat(c["fecha"]),
                 "importe": c["importe"], "egreso": c.get("egreso", True)} for n, c in enumerate(caso["candidatos"])]

    def test_casos_dorados_contra_el_buscador(self):
        esperado = {"ya_cargado": (True, False), "posible": (False, True), "nada": (False, False)}
        for caso in casos_dorados():
            p = caso["propuesta"]
            anio, mes = p["periodo"].split("-")
            ya, posible = gm.buscar_duplicados(self.candidatos(caso), tipo=p["tipo"], descripcion=p["descripcion"],
                                               periodo=(int(anio), int(mes)), importe=p["importe"], hoy=date.fromisoformat(caso["hoy"]))
            self.assertEqual((ya is not None, posible is not None), esperado[caso["esperado"]], caso["nombre"])

    def test_misma_descripcion_exacta_es_ya_cargado_desvio_a_proposito_de_la_666(self):
        # Solo en el escritor: la 666 no la tiene. Va del lado seguro (rechaza de más) y por eso no está en los casos dorados.
        cand = [{"id": 1, "tipo": 17, "descripcion": "Cobro  sin período", "fecha": datetime(2026, 11, 20, 10), "importe": 50.0, "egreso": True}]
        ya, posible = gm.buscar_duplicados(cand, tipo=17, descripcion="COBRO SIN PERIODO", periodo=(2026, 10), importe=9000, hoy=date(2026, 11, 26))
        self.assertIsNotNone(ya)
        self.assertIsNone(posible)
        # Fuera de la ventana del mismo tipo (±40 días y fuera del período) no cuenta.
        cand[0]["fecha"] = datetime(2026, 5, 2, 10)
        ya, _ = gm.buscar_duplicados(cand, tipo=17, descripcion="COBRO SIN PERIODO", periodo=(2026, 10), importe=9000, hoy=date(2026, 11, 26))
        self.assertIsNone(ya)

    def test_casos_dorados_a_traves_del_planificador(self):
        codigos = {"ya_cargado": "ya_cargado", "posible": "posible_duplicado"}
        for caso in casos_dorados():
            p = caso["propuesta"]
            hoy = datetime.fromisoformat(caso["hoy"] + "T10:00:00")
            prop = propuesta(tipo_mov_id=p["tipo"], descripcion=p["descripcion"], periodo=p["periodo"], importe=p["importe"], fecha=caso["hoy"])
            ref = ref_base(tipo={"id": p["tipo"], "es_ingreso": False, "es_egreso": True, "genera_asiento": False},
                           existentes=self.candidatos(caso))
            if caso["esperado"] == "nada":
                gm.planificar(prop, ref, hoy)
                continue
            with self.assertRaises(gc.NoCargable, msg=caso["nombre"]) as cm:
                gm.planificar(prop, ref, hoy)
            self.assertEqual(cm.exception.codigo, codigos[caso["esperado"]], caso["nombre"])

    def test_el_mensaje_nombra_el_movimiento(self):
        e = rechaza(self, propuesta(), "ya_cargado", existentes=[existente(100, "periodo 10/2026", datetime(2026, 11, 20))])
        self.assertIn("100", e.mensaje)

    def test_la_sigla(self):
        for texto, esperado in (("FAECYS PER 10/2026", "faecys"), ("faecys 10/2026 transferencia", "faecys"), ("PER 10/2026", None),
                                ("IMPUESTOS AGEC/INACAP PER 10/2026", "impuestos agec inacap"), ("SINDICATO CÓRDOBA PERIODO 10", "sindicato cordoba"),
                                ("GANANCIAS ANT 05/2031", "ganancias ant"), ("", None), ("PAGO 05/10/2026 FAECYS PER 10/2026", "pago faecys")):
            self.assertEqual(gm.sigla(texto), esperado, texto)

    def test_los_dorados_cubren_las_tres_respuestas(self):
        self.assertEqual({c["esperado"] for c in casos_dorados()}, {"ya_cargado", "posible", "nada"})


class TotalYFixtures(unittest.TestCase):
    def test_total_pesos_tiene_que_ser_el_importe(self):
        gm.planificar(propuesta(importe=500), ref_base(), AHORA, 500)
        gm.planificar(propuesta(importe=500), ref_base(), AHORA, None)
        for total in (501, 499.99, "500", True, float("nan")):
            with self.assertRaises(gc.NoCargable) as cm:
                gm.planificar(propuesta(importe=500), ref_base(), AHORA, total)
            self.assertEqual(cm.exception.codigo, "importe")

    def test_los_fixtures_de_la_cola_se_planifican(self):
        import json
        import os
        aqui = os.path.dirname(os.path.abspath(__file__))
        for nombre, medio, caja_banco in (("movimiento_efectivo.json", "efectivo", None), ("movimiento_transferencia.json", "transferencia", 11)):
            with open(os.path.join(aqui, "fixtures", nombre), encoding="utf-8") as f:
                payload = json.load(f)
            p = payload["propuesta"]
            self.assertEqual((p["clase"], p["medio"], p["caja"], p["cuenta_bancaria_id"]), ("movimiento_fondo", medio, 1, caja_banco))
            self.assertEqual(payload["total_pesos"], p["importe"])
            ref = ref_base(tipo={"id": p["tipo_mov_id"], "es_ingreso": False, "es_egreso": True, "genera_asiento": False})
            plan = gm.planificar(p, ref, AHORA, payload["total_pesos"])
            self.assertEqual(plan.medio, medio)
            self.assertEqual(plan.movimiento["fk_id_caja"], 1)      # también en la transferencia
            self.assertEqual(plan.vinculo, {"fk_id_caja": 1})
            self.assertEqual(plan.medio_fila["fk_id_cuenta_bancaria"] if medio == "transferencia" else 11, 11)

    def test_transferencia_con_otra_caja_se_rechaza(self):
        rechaza(self, transferencia(caja=2), "caja_invalida")
        rechaza(self, transferencia(caja=None), "caja_invalida")


class TiposYColumnas(unittest.TestCase):
    def test_cada_tabla_tiene_tipos_para_todas_sus_columnas(self):
        self.assertEqual(set(gm.COLUMNAS), set(gm.TIPOS))
        for tabla, columnas in gm.COLUMNAS.items():
            self.assertEqual(set(columnas), set(gm.TIPOS[tabla]), tabla)
        self.assertEqual(set(gm.TABLAS_ESCRITO), set(gm.COLUMNAS))

    def test_no_pisa_las_tablas_de_compras(self):
        self.assertFalse(set(gm.COLUMNAS) & set(gc.COLUMNAS))

    def test_la_clave_primaria_va_primera(self):
        for tabla, columnas in gm.COLUMNAS.items():
            self.assertTrue(columnas[0].startswith("id_"), tabla)


if __name__ == "__main__":
    unittest.main()
