"""Tests del planificador de cargas de compras (garro_cargas.py): unittest de la stdlib, sin base ni red.

Correr desde esta carpeta:  python -m pytest -q   (o:  python -m unittest -v test_garro_cargas)
"""

import copy
import json
import unittest
from datetime import date, datetime

import garro_cargas as gc

AHORA = datetime(2026, 11, 25, 10, 30, 15, 123456)

CUENTA_PROVEEDORES, CUENTA_IVA, CUENTA_ITEM, CUENTA_PERCEPCION = 89, 26, 109, 150


def ref_base(**cambios) -> gc.Referencias:
    datos = dict(
        proveedores=[{"id": 5, "razon_social": "TEJIDOS DEL SUR S.A.", "activo": True}],
        tipo={"id": 1, "descripcion": "Factura A", "es_ctacte": True},
        operario_id=31, cuenta_proveedores=CUENTA_PROVEEDORES, cuenta_credito_fiscal=CUENTA_IVA,
        cuentas={CUENTA_ITEM: {"activa": True, "imputable": True}, CUENTA_PERCEPCION: {"activa": True, "imputable": True},
                 7: {"activa": True, "imputable": True}},
        categorias={33, 13}, regimenes={8: {"tipo": 2, "cuenta": CUENTA_PERCEPCION}, 9: {"tipo": 1, "cuenta": CUENTA_PERCEPCION}},
        duplicados=[], siguiente_grupo=12, pagos_a_cuenta=[], largos={})
    datos.update(cambios)
    return gc.Referencias(**datos)


def renglon(**cambios) -> dict:
    r = {"cantidad_garro": 2, "linea_pesos": 1000.0, "alicuota_iva": 21, "garro_cuenta_imputacion_id": 33,
         "garro_plan_de_cuenta_id": CUENTA_ITEM, "descripcion": "Tela lino"}
    r.update(cambios)
    return r


def carga_base(total=1210.0, **cambios) -> dict:
    p = {"tipo_garro_id": 1, "cuit": "30-55512348-1", "punto_venta": 7, "numero": 4521, "fecha": "2026-11-20",
         "descuento_pct": 0, "requiere_revision": False, "items": [renglon()]}
    p.update(cambios)
    return {"propuesta": p, "total_pesos": total}


def remito_x(total=1000.0, **cambios) -> dict:
    c = carga_base(total, tipo_garro_id=5)
    c["propuesta"]["items"] = [renglon(alicuota_iva=0)]
    c["propuesta"].update(cambios)
    return c


REF_X = dict(tipo={"id": 5, "descripcion": "Remito X", "es_ctacte": False})


class PlanFacturaA(unittest.TestCase):
    def test_reproduce_cabecera_items_y_asiento(self):
        plan = gc.planificar(carga_base(), ref_base(), AHORA)
        c = plan.comprobante
        self.assertEqual((c["fk_id_tipo_comprobante_compra"], c["fk_id_proveedor"], c["fk_id_operario"]), (1, 5, 31))
        self.assertEqual((c["nro_suc_comprobante_compra"], c["nro_comprobante_compra"]), (7, 4521))
        self.assertEqual(c["estado_comprobante_compra"], "activo")
        self.assertEqual(c["fec_comprobante_compra"], datetime(2026, 11, 20, 10, 30, 15))
        self.assertEqual(set(c), set(gc.COLUMNAS["comprobantes_compras"][1:]))
        self.assertEqual(len(plan.items), 1)
        self.assertEqual(set(plan.items[0]["fila"]) | {"fk_id_comprobante_compra"}, set(gc.COLUMNAS["items_comprobantes_compras"][1:]))
        self.assertEqual(plan.items[0]["concepto"], "TELA LINO")
        self.assertEqual(plan.items[0]["fila"]["porc_iva_item_comprobante_compra"], 21.0)
        self.assertAlmostEqual(gc.r2(plan.total), 1210.0)

        movs = plan.asiento["movimientos"]
        self.assertTrue(movs[0]["es_haber"] and not movs[0]["es_debe"])          # el haber va primero
        self.assertEqual(movs[0]["fk_id_plan_de_cuenta"], CUENTA_PROVEEDORES)
        self.assertEqual(movs[0]["imp_asiento_contable_movimiento"], 1210.0)
        self.assertEqual(movs[1]["fk_id_plan_de_cuenta"], CUENTA_IVA)             # IVA
        self.assertEqual(movs[1]["imp_asiento_contable_movimiento"], 210.0)
        self.assertEqual(movs[2]["fk_id_plan_de_cuenta"], CUENTA_ITEM)            # un debe por renglón
        self.assertEqual(movs[2]["imp_asiento_contable_movimiento"], 1000.0)
        self.assertEqual(len(movs), 3)
        debe = sum(m["imp_asiento_contable_movimiento"] for m in movs if m["es_debe"])
        self.assertAlmostEqual(debe, movs[0]["imp_asiento_contable_movimiento"])  # cierra
        self.assertEqual(plan.asiento["fila"]["nro_grupo_asiento_contable"], 12)
        self.assertEqual(set(plan.asiento["fila"]) | {"fk_id_comprobante_compra"}, set(gc.COLUMNAS["asientos_contables"][1:]))
        self.assertTrue(plan.asiento["fila"]["des_asiento_contable"].startswith("FACTURA A 0007-00004521 TEJIDOS DEL SUR S.A."))
        self.assertEqual(plan.imputaciones, [])

    def test_unitario_es_linea_sobre_cantidad(self):
        plan = gc.planificar(carga_base(total=1210.0), ref_base(), AHORA)
        fila = plan.items[0]["fila"]
        self.assertEqual(fila["can_item_comprobante_compra"], 2)
        self.assertAlmostEqual(fila["imp_neto_item_comprobante_compra"], 500.0)

    def test_dos_renglones_un_debe_cada_uno(self):
        c = carga_base(total=3630.0)
        c["propuesta"]["items"] = [renglon(), renglon(linea_pesos=2000.0, descripcion="Espuma", garro_plan_de_cuenta_id=7)]
        plan = gc.planificar(c, ref_base(), AHORA)
        movs = plan.asiento["movimientos"]
        self.assertEqual([m["fk_id_plan_de_cuenta"] for m in movs], [CUENTA_PROVEEDORES, CUENTA_IVA, CUENTA_ITEM, 7])
        self.assertEqual(movs[0]["imp_asiento_contable_movimiento"], 3630.0)

    def test_percepcion_suma_al_total_y_va_al_asiento(self):
        c = carga_base(total=1220.0)
        c["propuesta"]["percepciones"] = [{"garro_regimen_id": 8, "importe": 10.0}]
        plan = gc.planificar(c, ref_base(), AHORA)
        self.assertEqual(plan.percepciones, 10.0)
        self.assertAlmostEqual(gc.r2(plan.total), 1220.0)
        self.assertEqual(plan.regimenes, [{"fk_id_regimen_especial": 8, "imp_regimen_especial_x_comprobante_compra": 10.0}])
        movs = plan.asiento["movimientos"]
        self.assertEqual(movs[0]["imp_asiento_contable_movimiento"], 1220.0)
        self.assertIn((CUENTA_PERCEPCION, 10.0), [(m["fk_id_plan_de_cuenta"], m["imp_asiento_contable_movimiento"]) for m in movs])

    def test_descuento_de_cabecera_baja_los_importes_del_asiento(self):
        c = carga_base(total=847.0, descuento_pct=-30)
        plan = gc.planificar(c, ref_base(), AHORA)
        movs = plan.asiento["movimientos"]
        self.assertEqual(movs[0]["imp_asiento_contable_movimiento"], 847.0)
        self.assertEqual(movs[1]["imp_asiento_contable_movimiento"], 147.0)
        self.assertEqual(movs[2]["imp_asiento_contable_movimiento"], 700.0)

    def test_asiento_no_pasa_del_largo_de_la_columna(self):
        plan = gc.planificar(carga_base(), ref_base(largos={"asientos_contables.des_asiento_contable": 20}), AHORA)
        self.assertEqual(len(plan.asiento["fila"]["des_asiento_contable"]), 20)

    def test_resumen_no_trae_ids_ni_textos(self):
        r = gc.planificar(carga_base(), ref_base(), AHORA).resumen()
        self.assertEqual(r["total"], 1210.0)
        self.assertEqual(r["renglones"], 1)
        self.assertTrue(r["asiento"])


class PlanRemitoX(unittest.TestCase):
    def test_sin_asiento_ni_imputaciones_aunque_haya_pagos(self):
        ref = ref_base(pagos_a_cuenta=[{"id": 1, "fecha": "2026-01-01", "resto": 5000.0}], **REF_X)
        plan = gc.planificar(remito_x(), ref, AHORA)
        self.assertIsNone(plan.asiento)
        self.assertEqual(plan.imputaciones, [])
        self.assertEqual(plan.iva, 0)
        self.assertAlmostEqual(gc.r2(plan.total), 1000.0)
        self.assertEqual(plan.items[0]["fila"]["porc_iva_item_comprobante_compra"], 0.0)

    def test_percepcion_en_remito_x_no_se_carga(self):
        c = remito_x(percepciones=[{"garro_regimen_id": 8, "importe": 5.0}])
        with self.assertRaises(gc.NoCargable) as cm:
            gc.planificar(c, ref_base(**REF_X), AHORA)
        self.assertEqual(cm.exception.codigo, "percepcion")


class Redondeo(unittest.TestCase):
    def test_a_favor_agrega_renglon_redondeo_con_categoria_del_mayor(self):
        c = carga_base(total=1210.50)
        c["propuesta"]["items"] = [renglon(linea_pesos=100.0, garro_cuenta_imputacion_id=13, garro_plan_de_cuenta_id=7),
                                   renglon(linea_pesos=900.0)]
        plan = gc.planificar(c, ref_base(), AHORA)
        self.assertEqual(len(plan.items), 3)
        ultimo = plan.items[-1]
        self.assertEqual(ultimo["concepto"], "REDONDEO")
        self.assertEqual(ultimo["fila"]["fk_id_cuenta_imputacion_item_compra"], 33)
        self.assertEqual(ultimo["fila"]["fk_id_plan_de_cuenta"], CUENTA_ITEM)
        self.assertEqual(ultimo["fila"]["can_item_comprobante_compra"], 1.0)
        self.assertAlmostEqual(gc.r2(plan.total), 1210.50)
        self.assertEqual(plan.asiento["movimientos"][0]["imp_asiento_contable_movimiento"], 1210.50)
        self.assertEqual(len(plan.avisos), 1)

    def test_en_contra_achica_el_renglon_mayor(self):
        c = carga_base(total=1209.60)
        c["propuesta"]["items"] = [renglon(linea_pesos=100.0), renglon(linea_pesos=900.0)]
        plan = gc.planificar(c, ref_base(), AHORA)
        self.assertEqual(len(plan.items), 2)
        self.assertAlmostEqual(gc.r2(plan.total), 1209.60)
        self.assertLess(plan.items[1]["fila"]["imp_neto_item_comprobante_compra"] * 2, 900.0)
        self.assertEqual(plan.items[0]["fila"]["imp_neto_item_comprobante_compra"], 50.0)  # el chico no se toca

    def test_diferencia_fuera_de_tope_no_se_carga(self):
        with self.assertRaises(gc.NoCargable) as cm:
            gc.planificar(carga_base(total=1300.0), ref_base(), AHORA)
        self.assertEqual(cm.exception.codigo, "total_no_cierra")

    def test_diferencia_negativa_fuera_de_tope_no_se_carga(self):
        with self.assertRaises(gc.NoCargable) as cm:
            gc.planificar(carga_base(total=1100.0), ref_base(), AHORA)
        self.assertEqual(cm.exception.codigo, "total_no_cierra")

    def test_total_que_cierra_no_agrega_nada(self):
        plan = gc.planificar(carga_base(total=1210.0), ref_base(), AHORA)
        self.assertEqual(plan.avisos, [])
        self.assertEqual(len(plan.items), 1)


class Rechazos(unittest.TestCase):
    def rechaza(self, codigo, carga=None, ref=None):
        with self.assertRaises(gc.NoCargable) as cm:
            gc.planificar(carga if carga is not None else carga_base(), ref if ref is not None else ref_base(), AHORA)
        self.assertEqual(cm.exception.codigo, codigo)

    def con(self, **p):
        c = carga_base()
        c["propuesta"].update(p)
        return c

    def test_sin_propuesta(self):
        self.rechaza("sin_propuesta", {"total_pesos": 10})

    def test_tipos(self):
        self.rechaza("tipo_no_habilitado", self.con(tipo_garro_id=3))
        self.rechaza("tipo_no_habilitado", self.con(es_nota_credito=True))
        self.rechaza("tipo_no_habilitado", self.con(tipo_garro_id=None))
        self.rechaza("tipo_desconocido", ref=ref_base(tipo=None))
        self.rechaza("tipo_desconocido", ref=ref_base(tipo={"id": 5, "descripcion": "x", "es_ctacte": False}))

    def test_requiere_revision_true_y_ausente(self):
        self.rechaza("requiere_revision", self.con(requiere_revision=True))
        c = carga_base()
        del c["propuesta"]["requiere_revision"]
        self.rechaza("requiere_revision", c)
        self.rechaza("requiere_revision", self.con(requiere_revision=None))

    def test_sin_operario(self):
        self.rechaza("sin_operario", ref=ref_base(operario_id=None))

    def test_proveedor(self):
        self.rechaza("cuit", self.con(cuit="30-5551234-8"))
        self.rechaza("proveedor", ref=ref_base(proveedores=[]))
        self.rechaza("proveedor", ref=ref_base(proveedores=[{"id": 1, "razon_social": "A", "activo": True},
                                                            {"id": 2, "razon_social": "B", "activo": True}]))
        self.rechaza("cuit", self.con(cuit=None, proveedor="TEJIDOS DEL SUR S.A."))        # Factura A: el CUIT es obligatorio
        self.rechaza("cuit", self.con(cuit="", proveedor="TEJIDOS DEL SUR S.A."))
        self.rechaza("proveedor", remito_x(cuit=None, proveedor=None), ref_base(**REF_X))
        self.rechaza("proveedor_inactivo", ref=ref_base(proveedores=[{"id": 5, "razon_social": "A", "activo": False}]))

    def test_numero_y_duplicado(self):
        self.rechaza("numero", self.con(numero=0))
        self.rechaza("numero", self.con(punto_venta=100000))
        self.rechaza("numero", self.con(numero="4521x"))
        self.rechaza("ya_cargado", ref=ref_base(duplicados=[3300]))

    def test_fechas(self):
        self.rechaza("fecha", self.con(fecha="2026-11-26"))       # futura
        self.rechaza("fecha", self.con(fecha="2025-11-01"))       # de hace más de un año
        self.rechaza("fecha", self.con(fecha="no es fecha"))
        self.rechaza("fecha", self.con(fecha=None))
        gc.planificar(self.con(fecha="2026-11-25"), ref_base(), AHORA)  # hoy sí

    def test_descuento_positivo_o_total(self):
        self.rechaza("descuento", self.con(descuento_pct=10))
        self.rechaza("descuento", self.con(descuento_pct=-100))

    def test_renglones(self):
        self.rechaza("sin_renglones", self.con(items=[]))
        self.rechaza("renglon", self.con(items=["texto"]))
        self.rechaza("renglon_no_imputable", self.con(items=[renglon(no_imputable=True)]))
        for malo in (0, -5, None, "10", True):
            self.rechaza("renglon_importe", self.con(items=[renglon(linea_pesos=malo)]))
        self.rechaza("renglon_importe", self.con(items=[renglon(cantidad_garro=0)]))
        self.rechaza("renglon_texto", self.con(items=[renglon(descripcion="  \n ")]))

    def test_iva_distinto_de_21_en_factura_a(self):
        for iva in (10.5, 0, None, 27):
            self.rechaza("iva", self.con(items=[renglon(alicuota_iva=iva)]))

    def test_categoria_y_cuenta(self):
        self.rechaza("categoria", self.con(items=[renglon(garro_cuenta_imputacion_id=999)]))
        self.rechaza("categoria", self.con(items=[renglon(garro_cuenta_imputacion_id=None)]))
        self.rechaza("cuenta", self.con(items=[renglon(garro_plan_de_cuenta_id=999)]))
        self.rechaza("cuenta", self.con(items=[renglon(garro_plan_de_cuenta_id=None)]))
        cuentas = {CUENTA_ITEM: {"activa": False, "imputable": True}}
        self.rechaza("cuenta", ref=ref_base(cuentas=cuentas))
        cuentas = {CUENTA_ITEM: {"activa": True, "imputable": False}}
        self.rechaza("cuenta", ref=ref_base(cuentas=cuentas))

    def test_percepciones(self):
        self.rechaza("percepcion", self.con(percepciones=[{"garro_regimen_id": 9, "importe": 5}]))     # no es percepción
        self.rechaza("percepcion", self.con(percepciones=[{"garro_regimen_id": 77, "importe": 5}]))    # no existe
        self.rechaza("percepcion", self.con(percepciones=[{"garro_regimen_id": 8, "importe": 0}]))
        self.rechaza("percepcion", self.con(percepciones=[{"garro_regimen_id": 8, "importe": -3}]))
        self.rechaza("percepcion", self.con(percepciones="x"))
        regimenes = {8: {"tipo": 2, "cuenta": 888}}
        self.rechaza("percepcion", self.con(percepciones=[{"garro_regimen_id": 8, "importe": 5}]),
                     ref_base(regimenes=regimenes))                                                    # cuenta que no existe

    def test_sin_total(self):
        for total in (None, 0, -1, "1210"):
            self.rechaza("sin_total", carga_base(total=total))

    def test_sin_configuracion_contable_o_sin_grupo(self):
        self.rechaza("config_contable", ref=ref_base(cuenta_proveedores=None))
        self.rechaza("config_contable", ref=ref_base(cuenta_credito_fiscal=None))
        self.rechaza("sin_grupo", ref=ref_base(siguiente_grupo=None))

    def test_no_modifica_la_propuesta_recibida(self):
        c = carga_base()
        antes = copy.deepcopy(c)
        gc.planificar(c, ref_base(), AHORA)
        self.assertEqual(c, antes)


class PagosACuenta(unittest.TestCase):
    def test_un_pago_cubre(self):
        r = gc.imputar_pagos_a_cuenta(1000.0, [{"id": 1, "fecha": "2026-01-01", "resto": 5000.0}])
        self.assertEqual(r, [(1, 1000.0)])

    def test_varios_pagos_del_mas_viejo_al_mas_nuevo(self):
        pagos = [{"id": 3, "fecha": "2026-03-01", "resto": 400.0}, {"id": 1, "fecha": "2026-01-01", "resto": 300.0},
                 {"id": 2, "fecha": "2026-02-01", "resto": 500.0}]
        self.assertEqual(gc.imputar_pagos_a_cuenta(1000.0, pagos), [(1, 300.0), (2, 500.0), (3, 200.0)])

    def test_parcial_cuando_los_pagos_no_alcanzan(self):
        pagos = [{"id": 1, "fecha": "2026-01-01", "resto": 300.0}]
        self.assertEqual(gc.imputar_pagos_a_cuenta(1000.0, pagos), [(1, 300.0)])

    def test_sin_pagos_no_imputa(self):
        self.assertEqual(gc.imputar_pagos_a_cuenta(1000.0, []), [])

    def test_resto_de_un_centavo_se_ignora(self):
        pagos = [{"id": 1, "fecha": "2026-01-01", "resto": 0.01}, {"id": 2, "fecha": "2026-02-01", "resto": 800.0}]
        self.assertEqual(gc.imputar_pagos_a_cuenta(500.0, pagos), [(2, 500.0)])

    def test_resto_de_cincuenta_centavos_no_se_carga(self):
        with self.assertRaises(gc.NoCargable) as cm:
            gc.imputar_pagos_a_cuenta(500.0, [{"id": 1, "fecha": "2026-01-01", "resto": 0.50}])
        self.assertEqual(cm.exception.codigo, "pago_resto_dudoso")

    def test_orden_por_id_distinto_del_orden_por_fecha_no_se_carga(self):
        pagos = [{"id": 1, "fecha": "2026-03-01", "resto": 300.0}, {"id": 2, "fecha": "2026-01-01", "resto": 300.0}]
        with self.assertRaises(gc.NoCargable) as cm:
            gc.imputar_pagos_a_cuenta(400.0, pagos)
        self.assertEqual(cm.exception.codigo, "pagos_orden_dudoso")

    def test_orden_distinto_pero_el_mismo_resultado_si_el_primero_cubre_todo(self):
        # Si lo que se imputa es el mismo pago en ambos órdenes, no hay ambigüedad.
        pagos = [{"id": 1, "fecha": "2026-03-01", "resto": 9000.0}, {"id": 2, "fecha": "2026-03-01", "resto": 9000.0}]
        self.assertEqual(gc.imputar_pagos_a_cuenta(400.0, pagos), [(1, 400.0)])

    def test_la_factura_las_incluye_en_el_plan(self):
        ref = ref_base(pagos_a_cuenta=[{"id": 4, "fecha": "2026-01-01", "resto": 500.0},
                                       {"id": 9, "fecha": "2026-02-01", "resto": 5000.0}])
        plan = gc.planificar(carga_base(), ref, AHORA)
        self.assertEqual([(i["fk_id_pago"], i["imp_pago_en_comprobante_compra"]) for i in plan.imputaciones],
                         [(4, 500.0), (9, 710.0)])
        for i in plan.imputaciones:
            self.assertEqual(set(i) | {"fk_id_comprobante_compra"}, set(gc.COLUMNAS["pagos_en_comprobantes_compras"][1:]))


class ProveedoresDe(unittest.TestCase):
    TODOS = [
        {"id": 1, "cuit": "30-55512348-1", "razon_social": "TEJIDOS DEL SUR S.A.", "fantasia": "Tejidos Sur", "activo": True},
        {"id": 2, "cuit": "30555123481", "razon_social": "OTRA SA", "fantasia": None, "activo": True},
        {"id": 3, "cuit": None, "razon_social": "Café Ñandú y Hnos.", "fantasia": "El Ñandú", "activo": True},
        {"id": 4, "cuit": "", "razon_social": "SIN CUIT SRL", "fantasia": "", "activo": True},
    ]

    def test_cuit_con_guiones_o_sin(self):
        ids = [p["id"] for p in gc.proveedores_de({"cuit": "30-55512348-1"}, self.TODOS)]
        self.assertEqual(ids, [1, 2])
        ids = [p["id"] for p in gc.proveedores_de({"cuit": "30555123481"}, self.TODOS)]
        self.assertEqual(ids, [1, 2])

    def test_con_cuit_manda_el_cuit_aunque_el_nombre_coincida(self):
        self.assertEqual(gc.proveedores_de({"cuit": "20111111112", "proveedor": "SIN CUIT SRL"}, self.TODOS), [])

    def test_nombre_con_acentos_y_puntos(self):
        self.assertEqual([p["id"] for p in gc.proveedores_de({"proveedor": "cafe nandu y hnos"}, self.TODOS)], [3])
        self.assertEqual([p["id"] for p in gc.proveedores_de({"proveedor": "TEJIDOS DEL SUR S.A."}, self.TODOS)], [1])

    def test_por_fantasia(self):
        self.assertEqual([p["id"] for p in gc.proveedores_de({"proveedor": "el ñandú"}, self.TODOS)], [3])

    def test_nombre_parcial_no_coincide(self):
        self.assertEqual(gc.proveedores_de({"proveedor": "TEJIDOS DEL SUR SA SUCURSAL"}, self.TODOS), [])

    def test_sin_cuit_ni_nombre(self):
        self.assertEqual(gc.proveedores_de({}, self.TODOS), [])
        self.assertEqual(gc.proveedores_de({"proveedor": "  "}, self.TODOS), [])


class ProveedorFijadoEnLaAprobacion(unittest.TestCase):
    """`proveedor_garro` (lo decidió la aprobación): el escritor usa ESE id y lo relee de Garro."""

    def prov(self, cuit="30555123481", activo=True, id=468):
        return {"id": id, "cuit": cuit, "razon_social": "TAPI HOUSE", "fantasia": None, "activo": activo}

    def ref(self, *proveedores, **cambios):
        return ref_base(proveedores=list(proveedores), **cambios)

    def fijada(self, base=None, pid=468, **p):
        c = base if base is not None else carga_base()
        c["propuesta"]["proveedor_garro"] = {"id": pid, "razon_social": "TAPI HOUSE", "por": "nombre"}
        c["propuesta"].update(p)
        return c

    def rechaza(self, codigo, carga, ref):
        with self.assertRaises(gc.NoCargable) as cm:
            gc.planificar(carga, ref, AHORA)
        self.assertEqual(cm.exception.codigo, codigo)

    def test_proveedores_de_usa_solo_el_id(self):
        todos = [self.prov(), self.prov(id=9, cuit="30555123481")]
        p = {"cuit": "30555123481", "proveedor": "OTRO", "proveedor_garro": {"id": 468}}
        self.assertEqual([x["id"] for x in gc.proveedores_de(p, todos)], [468])
        self.assertEqual(gc.proveedores_de({"proveedor_garro": {"id": 77}}, todos), [])

    def test_proveedores_de_con_campo_invalido_no_cae_en_el_cuit(self):
        todos = [self.prov()]
        for pg in (None, {}, {"id": None}, {"id": "468"}, {"id": 468.5}, {"id": -1}, {"id": 0}, {"id": True}, 468, "468", []):
            self.assertEqual(gc.proveedores_de({"cuit": "30555123481", "proveedor_garro": pg}, todos), [], repr(pg))

    def test_factura_a_con_el_mismo_cuit_carga_al_id_fijado(self):
        plan = gc.planificar(self.fijada(cuit="30-55512348-1"), self.ref(self.prov()), AHORA)
        self.assertEqual(plan.comprobante["fk_id_proveedor"], 468)

    def test_factura_a_con_cuit_distinto_o_vacio_en_garro_no_se_carga(self):
        for cuit_garro in ("30711982538", "", None, "30-5551234"):
            self.rechaza("proveedor_cuit", self.fijada(), self.ref(self.prov(cuit=cuit_garro)))

    def test_factura_a_sin_cuit_valido_en_el_papel_no_se_carga(self):
        for cuit in (None, "", "30-5551234-8"):
            self.rechaza("cuit", self.fijada(cuit=cuit), self.ref(self.prov()))

    def test_remito_x_alcanza_con_el_id_aunque_garro_no_tenga_cuit_o_el_papel_traiga_otro(self):
        for cuit_garro in ("", None, "20999999999"):
            for cuit_papel in (None, "", "20-11111111-2", "123"):
                c = self.fijada(remito_x(cuit=cuit_papel, proveedor="TAPI HOUSE SRL"))
                plan = gc.planificar(c, self.ref(self.prov(cuit=cuit_garro), **REF_X), AHORA)
                self.assertEqual(plan.comprobante["fk_id_proveedor"], 468)

    def test_id_que_no_existe_o_inactivo(self):
        c = self.fijada(remito_x(), pid=999)      # lo que lee el escritor: solo el id fijado, y ese no está
        self.rechaza("proveedor", c, self.ref(*gc.proveedores_de(c["propuesta"], [self.prov()]), **REF_X))
        self.rechaza("proveedor", self.fijada(remito_x()), self.ref(**REF_X))
        self.rechaza("proveedor_inactivo", self.fijada(remito_x()), self.ref(self.prov(activo=False), **REF_X))
        self.rechaza("proveedor_inactivo", self.fijada(cuit="30-55512348-1"), self.ref(self.prov(activo=False)))

    def test_id_no_entero_negativo_o_proveedor_sin_resolver_no_se_carga(self):
        for pg in (None, {}, {"id": None}, {"id": "468"}, {"id": 1.5}, {"id": -468}, {"id": 0}, {"id": False}, "468"):
            c = remito_x()
            c["propuesta"]["proveedor_garro"] = pg
            self.rechaza("proveedor_sin_resolver", c, self.ref(self.prov(), **REF_X))

    def test_sin_el_campo_sigue_como_antes(self):
        plan = gc.planificar(carga_base(), ref_base(), AHORA)
        self.assertEqual(plan.comprobante["fk_id_proveedor"], 5)
        self.rechaza("proveedor", remito_x(cuit=None, proveedor=None), ref_base(**REF_X))

    def test_requiere_revision_sigue_mandando(self):
        self.rechaza("requiere_revision", self.fijada(requiere_revision=True, cuit="30-55512348-1"), self.ref(self.prov()))

    def test_no_modifica_la_propuesta(self):
        c = self.fijada(cuit="30-55512348-1")
        antes = json.dumps(c, sort_keys=True)
        gc.planificar(c, self.ref(self.prov()), AHORA)
        self.assertEqual(json.dumps(c, sort_keys=True), antes)


class LimpiarTexto(unittest.TestCase):
    def test_mayusculas_y_una_sola_linea(self):
        self.assertEqual(gc.limpiar_texto("  tela\n  lino\t45 "), "TELA LINO 45")

    def test_caracteres_fuera_de_cp1252_se_reemplazan(self):
        t = gc.limpiar_texto("caño ✓ 日本")
        self.assertEqual(t[:4], "CAÑO")
        t.encode("cp1252")

    def test_largo_maximo_y_largo_de_columna(self):
        self.assertEqual(len(gc.limpiar_texto("a" * 500)), gc.LARGO_TEXTO)
        self.assertEqual(gc.limpiar_texto("abcdefgh", 5), "ABCDE")
        self.assertEqual(gc.limpiar_texto("abcd efgh", 5), "ABCD")  # sin espacio colgado

    def test_vacio_y_none(self):
        self.assertEqual(gc.limpiar_texto(None), "")
        self.assertEqual(gc.limpiar_texto(" \n "), "")

    def test_sin_caracteres_de_control(self):
        self.assertEqual(gc.limpiar_texto("a\x00b\x07c"), "ABC")


class Utilidades(unittest.TestCase):
    def test_r2_medio_hacia_arriba(self):
        self.assertEqual(gc.r2(0.125), 0.13)
        self.assertEqual(gc.r2(1.005), 1.01)
        self.assertEqual(gc.r2(-0.125), -0.13)

    def test_numero_lindo(self):
        self.assertEqual(gc.numero_lindo(7, 4521), "0007-00004521")

    def test_tope_redondeo(self):
        self.assertEqual(gc.tope_redondeo(100.0, 1), 10.0)
        self.assertEqual(gc.tope_redondeo(10000.0, 1), 10.0)
        self.assertEqual(gc.tope_redondeo(100000.0, 1), 50.0)
        self.assertEqual(gc.tope_redondeo(100.0, 30), 30.0)
        self.assertEqual(gc.tope_redondeo(3_000_000.0, 1), 500.0)   # tope máximo
        self.assertEqual(gc.tope_redondeo(100.0, 900), 500.0)
        # En dólares cada renglón viene redondeado al centavo de dólar: 5 centavos de dólar por renglón.
        self.assertEqual(gc.tope_redondeo(16550.0, 2, 1570.0), 157.0)
        self.assertEqual(gc.tope_redondeo(16550.0, 2, 1.0), 10.0)
        self.assertEqual(gc.tope_redondeo(3_000_000.0, 20, 1570.0), 500.0)

    def test_fecha_del_asiento_es_la_del_comprobante_con_la_hora_de_carga(self):
        plan = gc.planificar(carga_base(), ref_base(), AHORA)
        self.assertEqual(plan.asiento["fila"]["fec_asiento_contable"].date(), date(2026, 11, 20))


class NumerosEstrictos(unittest.TestCase):
    def rechaza(self, codigo, carga, ref=None):
        with self.assertRaises(gc.NoCargable) as cm:
            gc.planificar(carga, ref or ref_base(), AHORA)
        self.assertEqual(cm.exception.codigo, codigo)

    def test_descuento_que_no_es_numero(self):
        for malo in ("-10", "x", True, float("nan"), float("inf"), [], {}):
            self.rechaza("descuento", carga_base(descuento_pct=malo))
        self.rechaza("descuento", carga_base(descuento_pct=10 ** 400))
        for bien in (None, 0, -0.0):
            gc.planificar(carga_base(descuento_pct=bien), ref_base(), AHORA)

    def test_cotizacion_que_no_es_numero_o_no_es_positiva(self):
        for malo in ("1500", "x", True, float("nan"), float("inf"), 0, -1500, 1e13):
            self.rechaza("cotizacion", carga_base(tipo_cambio_efectivo=malo, percepciones=[{"garro_regimen_id": 8, "importe": 5}]))
        # ausente o null vale 1
        c = carga_base(total=1220.0, percepciones=[{"garro_regimen_id": 8, "importe": 10}], tipo_cambio_efectivo=None)
        self.assertEqual(gc.planificar(c, ref_base(), AHORA).percepciones, 10.0)

    def test_percepcion_que_no_queda_positiva_en_pesos(self):
        self.rechaza("percepcion", carga_base(percepciones=[{"garro_regimen_id": 8, "importe": 0.0001}], tipo_cambio_efectivo=1))
        self.rechaza("percepcion", carga_base(percepciones=[{"garro_regimen_id": 8, "importe": "10"}]))
        self.rechaza("percepcion", carga_base(percepciones=[{"garro_regimen_id": 8, "importe": 10 ** 400}]))
        self.rechaza("percepcion", carga_base(percepciones=[{"garro_regimen_id": 8, "importe": 5e12}]))

    def test_percepcion_convertida_a_pesos(self):
        c = carga_base(total=1210.0 + 1500.0, percepciones=[{"garro_regimen_id": 8, "importe": 1.5}], tipo_cambio_efectivo=1000)
        self.assertEqual(gc.planificar(c, ref_base(), AHORA).percepciones, 1500.0)

    def test_enteros_gigantes_y_valores_fuera_de_tope(self):
        for malo in (10 ** 400, 1e12, 1e300, float("nan")):
            self.rechaza("renglon_importe", carga_base(items=[renglon(linea_pesos=malo)]))
            self.rechaza("renglon_importe", carga_base(items=[renglon(cantidad_garro=malo)]))
        self.rechaza("renglon_importe", carga_base(items=[renglon(cantidad_garro=1e-300)]))
        for malo in (10 ** 400, 1e12, float("inf")):
            self.rechaza("sin_total", carga_base(total=malo))

    def test_tope_de_renglones_y_de_percepciones(self):
        self.rechaza("demasiados_renglones", carga_base(items=[renglon(linea_pesos=1.0)] * 201))
        gc.planificar(carga_base(total=200 * 1.21, items=[renglon(cantidad_garro=1, linea_pesos=1.0)] * 200), ref_base(), AHORA)
        self.rechaza("percepcion", carga_base(percepciones=[{"garro_regimen_id": 8, "importe": 1}] * 21))

    def test_tope_de_redondeo_nuevo_en_un_comprobante_grande(self):
        c = carga_base(total=3_000_000.0 + 14_000.0)
        c["propuesta"]["items"] = [renglon(cantidad_garro=1, linea_pesos=2_479_338.84)]   # + IVA = 3.000.000
        self.rechaza("total_no_cierra", c)

    def test_imputacion_con_tres_decimales(self):
        ref = ref_base(pagos_a_cuenta=[{"id": 1, "fecha": "2026-01-01", "resto": 5000.0}])
        plan = gc.planificar(carga_base(total=1210.0), ref, AHORA)
        self.assertEqual(plan.imputaciones[0]["imp_pago_en_comprobante_compra"], 1210.0)
        self.assertEqual(gc.r3(1126871.5049999), 1126871.505)
        self.assertEqual(gc.r3(2230086.1444), 2230086.144)
        self.assertEqual(gc.imputar_pagos_a_cuenta(100.0, [{"id": 1, "fecha": "x", "resto": 1000.0}]), [(1, 100.0)])


class TiposDeColumnas(unittest.TestCase):
    def test_hay_tipo_para_todas_las_columnas_menos_las_tres_de_control(self):
        sin = {(t, c) for t, cols in gc.COLUMNAS.items() for c in cols if c not in gc.TIPOS[t]}
        self.assertEqual(sin, {("comprobantes_compras", "controlado"), ("comprobantes_compras", "fec_control"),
                               ("comprobantes_compras", "fk_id_operario_control")})
        self.assertEqual(gc.TIPOS["comprobantes_compras"]["stock"], "boolean")
        self.assertEqual(gc.TIPOS["items_comprobantes_compras"]["imp_neto_item_comprobante_compra"], "double precision")


if __name__ == "__main__":
    unittest.main()
