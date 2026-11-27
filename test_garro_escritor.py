"""Tests del escritor de cargas (garro_escritor.py) que no necesitan base: unittest de la stdlib, sin psycopg2 ni red.

Lo que escribe en PostgreSQL se prueba con una base real descartable (ver active/garro-carga-compras/escritor/
probar_escritor.py en el workspace). Acá: las llaves, el candado, la pasada con la cola y el diario.
"""

import contextlib
import io
import json
import os
import random
import sys
import tempfile
import types
import unittest
from datetime import date, datetime
from decimal import Decimal
from unittest import mock

import garro_cargas as gc
import garro_escritor as ge
import garro_movimientos as gm
import garro_sync as g
from test_garro_cargas import AHORA, carga_base, ref_base
from test_garro_movimientos import AHORA as AHORA_MOV, propuesta as propuesta_mov, ref_base as ref_mov, transferencia as transf_mov


class CursorFalso:
    def __init__(self):
        self.sentencias = []

    def execute(self, sql, params=None):
        self.sentencias.append((sql, params))

    def fetchone(self):
        return (1,)


class _Psycopg2Falso(types.ModuleType):
    """psycopg2 de mentira con excepciones de verdad: los tests corren igual con la librería instalada o sin ella."""

    class Error(Exception):
        pass

    class OperationalError(Error):
        pass

    class InterfaceError(Error):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(sys.modules, {"psycopg2": _Psycopg2Falso("psycopg2")})
        p.start()
        self.addCleanup(p.stop)
        for obj, nombre, valor in ((g, "LOG_LOCAL", os.path.join(self.tmp.name, "log")),
                                   (ge, "DIARIO", os.path.join(self.tmp.name, "diario.jsonl")),
                                   (ge, "ARCHIVO_LOCK", os.path.join(self.tmp.name, "lock"))):
            p = mock.patch.object(obj, nombre, valor)
            p.start()
            self.addCleanup(p.stop)
        self.salida = io.StringIO()
        p = contextlib.redirect_stdout(self.salida)
        p.__enter__()
        self.addCleanup(p.__exit__, None, None, None)


class Llaves(Base):
    def pasada(self, cfg, cargas, resultado=None, resolver=None):
        cola = list(cargas)
        cargar = mock.MagicMock(side_effect=resultado or (lambda cfg_, carga, en_seco: {"estado": "simulada", "plan": {}}))
        res = resolver or mock.MagicMock()
        with mock.patch.object(ge, "cola_tomar", side_effect=lambda cfg_: cola.pop(0) if cola else None) as tomar, \
                mock.patch.object(ge, "cargar_una", cargar), mock.patch.object(ge, "cola_resolver", res):
            ge.pasada(cfg)
        return tomar, cargar, res

    def test_apagada_no_toca_la_cola(self):
        for cfg in ({}, {"CARGAS": "0"}, {"CARGAS": ""}, {"CARGAS": "si"}):
            tomar, cargar, _ = self.pasada(cfg, [{"id": 1, "modo": "real"}])
            tomar.assert_not_called()
            cargar.assert_not_called()

    def test_real_sin_la_segunda_llave_se_procesa_en_seco_y_se_resuelve_simulada_con_aviso(self):
        for cfg in ({"CARGAS": "1"}, {"CARGAS": "1", "CARGAS_REAL": "0"}, {"CARGAS": "1", "CARGAS_REAL": "true"}):
            _, cargar, res = self.pasada(cfg, [{"id": 7, "modo": "real"}])
            self.assertTrue(cargar.call_args.kwargs["en_seco"])
            estado, resultado = res.call_args.args[2], res.call_args.args[3]
            self.assertEqual(estado, "simulada")
            self.assertEqual(resultado["aviso"], "la escritura real está apagada en esta instalación")

    def test_real_con_las_dos_llaves_escribe(self):
        _, cargar, _ = self.pasada({"CARGAS": "1", "CARGAS_REAL": "1"}, [{"id": 7, "modo": "real"}])
        self.assertFalse(cargar.call_args.kwargs["en_seco"])

    def test_en_seco_nunca_escribe_aunque_la_llave_este_prendida(self):
        _, cargar, _ = self.pasada({"CARGAS": "1", "CARGAS_REAL": "1"}, [{"id": 7, "modo": "en_seco"}])
        self.assertTrue(cargar.call_args.kwargs["en_seco"])

    def test_un_modo_desconocido_se_trata_como_en_seco(self):
        _, cargar, _ = self.pasada({"CARGAS": "1", "CARGAS_REAL": "1"}, [{"id": 7, "modo": "REAL "}])
        self.assertTrue(cargar.call_args.kwargs["en_seco"])

    def test_por_pasada_default_tope_y_valores_raros(self):
        for valor, esperado in ((None, 1), ("", 1), ("3", 3), ("99", 5), ("0", 1), ("-2", 1), ("x", 1)):
            cfg = {"CARGAS": "1"} if valor is None else {"CARGAS": "1", "CARGAS_POR_PASADA": valor}
            self.assertEqual(ge._cargas_por_pasada(cfg), esperado, valor)
        cargas = [{"id": n, "modo": "en_seco"} for n in range(1, 10)]
        _, cargar, _ = self.pasada({"CARGAS": "1", "CARGAS_POR_PASADA": "99"}, cargas)
        self.assertEqual(cargar.call_count, 5)

    def test_cola_vacia_corta(self):
        _, cargar, res = self.pasada({"CARGAS": "1", "CARGAS_POR_PASADA": "5"}, [])
        cargar.assert_not_called()
        res.assert_not_called()

    def test_resolver_falla_corta_la_pasada_sin_reintentar_escribir(self):
        res = mock.MagicMock(side_effect=RuntimeError("sin red"))
        cargas = [{"id": 1, "modo": "real"}, {"id": 2, "modo": "real"}]
        _, cargar, _ = self.pasada({"CARGAS": "1", "CARGAS_REAL": "1", "CARGAS_POR_PASADA": "5"}, cargas,
                                   resultado=lambda cfg_, carga, en_seco: {"estado": "cargada", "comprobante_id": 9},
                                   resolver=res)
        self.assertEqual(cargar.call_count, 1)  # la segunda ni se toma
        self.assertEqual(res.call_count, 1)
        self.assertIn("ERROR al resolver la carga 1", open(g.LOG_LOCAL, encoding="utf-8").read())

    def test_commit_incierto_no_se_resuelve(self):
        _, _, res = self.pasada({"CARGAS": "1", "CARGAS_REAL": "1"}, [{"id": 1, "modo": "real"}],
                                resultado=lambda cfg_, carga, en_seco: {"estado": "indeterminado", "codigo": "commit_incierto"})
        res.assert_not_called()

    def test_sin_funciones_en_supabase_avisa_una_linea(self):
        with mock.patch.object(ge, "cola_tomar", side_effect=ge.ColaSinFunciones()):
            ge.pasada({"CARGAS": "1"})
        lineas = self.salida.getvalue().strip().splitlines()
        self.assertEqual(len(lineas), 1)
        self.assertIn("(cargas)", lineas[0])

    def test_una_linea_de_log_por_carga_sin_importes(self):
        self.pasada({"CARGAS": "1"}, [{"id": 4, "modo": "en_seco"}],
                    resultado=lambda cfg_, carga, en_seco: {"estado": "simulada", "plan": {"total": 123456.78}})
        texto = self.salida.getvalue()
        self.assertIn("carga 4: simulada", texto)
        self.assertNotIn("123456", texto)

    def test_el_candado_impide_dos_pasadas(self):
        f = ge.tomar_lock(ge.ARCHIVO_LOCK)
        try:
            self.assertIsNotNone(f)
            tomar, _, _ = self.pasada({"CARGAS": "1"}, [{"id": 1, "modo": "en_seco"}])
            tomar.assert_not_called()
            self.assertIsNone(ge.tomar_lock(ge.ARCHIVO_LOCK))
        finally:
            ge.soltar_lock(f)
        tomar, _, _ = self.pasada({"CARGAS": "1"}, [{"id": 1, "modo": "en_seco"}])
        tomar.assert_called()

    def test_el_candado_se_suelta_aunque_la_pasada_falle(self):
        with mock.patch.object(ge, "cola_tomar", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                ge.pasada({"CARGAS": "1"})
        f = ge.tomar_lock(ge.ARCHIVO_LOCK)
        self.assertIsNotNone(f)
        ge.soltar_lock(f)


class CargasAuto(Base):
    def test_apagada_ni_importa_el_modulo(self):
        fake = types.ModuleType("garro_escritor")
        fake.pasada = mock.MagicMock()
        with mock.patch.object(g, "leer_archivo", return_value={"CARGAS": "0"}), \
                mock.patch.dict(sys.modules, {"garro_escritor": fake}):
            g.cargas_auto("x")
        fake.pasada.assert_not_called()

    def test_prendida_corre_la_pasada(self):
        fake = types.ModuleType("garro_escritor")
        fake.pasada = mock.MagicMock()
        with mock.patch.object(g, "leer_archivo", return_value={"CARGAS": "1"}), \
                mock.patch.dict(sys.modules, {"garro_escritor": fake}):
            g.cargas_auto("x")
        fake.pasada.assert_called_once()

    def test_una_excepcion_no_cambia_el_codigo_del_pedido_y_queda_en_el_log(self):
        for fallo in (RuntimeError("boom"), SystemExit("x")):
            fake = types.ModuleType("garro_escritor")
            fake.pasada = mock.MagicMock(side_effect=fallo)
            with mock.patch.object(g, "leer_archivo", return_value={"CARGAS": "1"}), \
                    mock.patch.object(g, "_atender_pedido", return_value=0), \
                    mock.patch.object(g, "relevamiento_auto"), \
                    mock.patch.object(g, "comandos_auto"), \
                    mock.patch.dict(sys.modules, {"garro_escritor": fake}):
                self.assertEqual(g.correr_pedido("x"), 0)
        self.assertIn("(cargas) ERROR", open(g.LOG_LOCAL, encoding="utf-8").read())

    def test_corre_despues_del_pedido_y_antes_del_relevamiento(self):
        orden = []
        with mock.patch.object(g, "_atender_pedido", side_effect=lambda c: orden.append("pedido") or 1), \
                mock.patch.object(g, "cargas_auto", side_effect=lambda c: orden.append("cargas")), \
                mock.patch.object(g, "relevamiento_auto", side_effect=lambda c: orden.append("relevamiento")), \
                mock.patch.object(g, "comandos_auto", side_effect=lambda c, inicio=None: orden.append("comandos")):
            self.assertEqual(g.correr_pedido("x"), 1)
        self.assertEqual(orden, ["pedido", "cargas", "relevamiento", "comandos"])


class Insertar(Base):
    def test_lista_explicita_de_columnas_sin_la_clave_primaria(self):
        cur = CursorFalso()
        fila = {"des_concepto_compra": "TELA"}
        self.assertEqual(ge._insertar(cur, "conceptos_compras", fila), 1)
        sql, params = cur.sentencias[0]
        self.assertIn("(des_concepto_compra)", sql)
        self.assertIn("RETURNING id_concepto_compra", sql)
        self.assertEqual(params, ["TELA"])

    def test_columna_de_mas_o_de_menos_no_inserta(self):
        cur = CursorFalso()
        with self.assertRaises(ge.ErrorPlan):
            ge._insertar(cur, "conceptos_compras", {"des_concepto_compra": "x", "otra": 1})
        with self.assertRaises(ge.ErrorPlan):
            ge._insertar(cur, "conceptos_compras", {})
        with self.assertRaises(ge.ErrorPlan):  # el plan ya trae una clave que pone el escritor
            ge._insertar(cur, "conceptos_x_items_comprobantes_compras", {"fk_id_item_comprobante_compra": 1},
                         fk_id_item_comprobante_compra=2, fk_id_concepto_compra=3)
        self.assertEqual(cur.sentencias, [])


class Diario(Base):
    def test_una_linea_por_evento_sin_la_propuesta(self):
        ge.diario_escribir("intento", {"id": 3, "clave": "k"}, "base1", total=1210.0)
        ge.diario_escribir("cargada", {"id": 3, "clave": "k"}, "base1", escrito={"comprobantes_compras": [10]})
        lineas = [json.loads(l) for l in open(ge.DIARIO, encoding="utf-8")]
        self.assertEqual([l["evento"] for l in lineas], ["intento", "cargada"])
        self.assertEqual(lineas[1]["escrito"], {"comprobantes_compras": [10]})
        self.assertEqual(set(lineas[0]), {"hora", "carga", "clave", "base", "evento", "total"})

    def test_ultimo_evento_y_lineas_dañadas(self):
        ge.diario_escribir("cargada", {"id": 3, "clave": "k"}, "base1", escrito={"a": [1]})
        with open(ge.DIARIO, "a", encoding="utf-8") as f:
            f.write("{no es json\n")
        ge.diario_escribir("cargada", {"id": 4, "clave": "k"}, "base1", escrito={"a": [2]})
        self.assertEqual(ge.diario_ultimo(3, ("cargada",), "base1")["escrito"], {"a": [1]})
        self.assertIsNone(ge.diario_ultimo(99, ("cargada",), "base1"))

    def test_solo_las_entradas_de_esa_base(self):
        ge.diario_escribir("cargada", {"id": 3, "clave": "k"}, "copia", escrito={"a": [1]})
        ge.diario_escribir("cargada", {"id": 3, "clave": "k"}, "real", escrito={"a": [2]})
        self.assertEqual(ge.diario_ultimo(3, ("cargada",), "copia")["escrito"], {"a": [1]})
        self.assertEqual(ge.diario_ultimo(3, ("cargada",), "real")["escrito"], {"a": [2]})
        self.assertIsNone(ge.diario_ultimo(3, ("cargada",), "otra"))

    def test_sin_diario_no_hay_ultimo(self):
        self.assertIsNone(ge.diario_ultimo(1, ("cargada",), "x"))


class Cli(Base):
    def test_escribir_sin_base_o_con_la_base_real_se_rechaza_sin_conectar(self):
        ruta = os.path.join(self.tmp.name, "cfg")
        open(ruta, "w").write("GARRO_BASE=real\n")
        carga = os.path.join(self.tmp.name, "c.json")
        open(carga, "w").write("{}")
        with mock.patch.object(ge, "cargar_una") as cargar:
            self.assertEqual(ge.main(["--archivo", carga, "--escribir", "--config", ruta]), 2)
            self.assertEqual(ge.main(["--archivo", carga, "--escribir", "--base", "real", "--config", ruta]), 2)
        cargar.assert_not_called()

    def test_escribir_con_copia_pasa_la_base_y_no_es_en_seco(self):
        ruta = os.path.join(self.tmp.name, "cfg")
        open(ruta, "w").write("GARRO_BASE=real\n")
        carga = os.path.join(self.tmp.name, "c.json")
        open(carga, "w").write(json.dumps({"propuesta": {"a": 1}, "total_pesos": 5}))
        with mock.patch.object(ge, "cargar_una", return_value={"estado": "cargada"}) as cargar:
            self.assertEqual(ge.main(["--archivo", carga, "--escribir", "--base", "copia", "--config", ruta]), 0)
        self.assertFalse(cargar.call_args.kwargs["en_seco"])
        self.assertEqual(cargar.call_args.kwargs["base"], "copia")

    def test_sin_escribir_siempre_es_en_seco(self):
        ruta = os.path.join(self.tmp.name, "cfg")
        open(ruta, "w").write("GARRO_BASE=real\n")
        carga = os.path.join(self.tmp.name, "c.json")
        open(carga, "w").write("{}")
        with mock.patch.object(ge, "cargar_una", return_value={"estado": "simulada"}) as cargar:
            self.assertEqual(ge.main(["--archivo", carga, "--config", ruta]), 0)
        self.assertTrue(cargar.call_args.kwargs["en_seco"])

    def test_pasada_no_acepta_base(self):
        with self.assertRaises(SystemExit):
            ge.main(["--pasada", "--base", "otra"])


class Estructura(Base):
    def test_columnas_de_mas_y_de_menos_se_informan_por_tabla(self):
        class Cur(CursorFalso):
            def __init__(self, filas):
                super().__init__()
                self.filas = filas
                self.resultado = []

            def execute(self, sql, params=None):
                if "information_schema" in sql:
                    self.resultado = self.filas
                else:
                    self.resultado = []

            def fetchall(self):
                return self.resultado

        filas = [(t, c, None, gc.TIPOS[t].get(c, "integer")) for t, cols in gc.COLUMNAS.items() for c in cols]
        largos = ge.verificar_estructura(Cur(filas))
        self.assertEqual(len(largos), sum(len(c) for c in gc.COLUMNAS.values()))
        con_de_mas = filas + [("conceptos_compras", "extra", None, "integer")]
        with self.assertRaises(ge.ErrorEstructura) as cm:
            ge.verificar_estructura(Cur(con_de_mas))
        self.assertIn("conceptos_compras: columnas de más extra", str(cm.exception))
        sin_una = [f for f in filas if f[1] != "des_concepto_compra"]
        with self.assertRaises(ge.ErrorEstructura) as cm:
            ge.verificar_estructura(Cur(sin_una))
        self.assertIn("faltan columnas des_concepto_compra", str(cm.exception))
        tipo_cambiado = [(t, c, l, "text" if c == "des_concepto_compra" else d) for t, c, l, d in filas]
        with self.assertRaises(ge.ErrorEstructura) as cm:
            ge.verificar_estructura(Cur(tipo_cambiado))
        self.assertIn("conceptos_compras.des_concepto_compra: es text y se esperaba character varying", str(cm.exception))
        sin_tabla = [f for f in filas if f[0] != "pagos_en_comprobantes_compras"]
        with self.assertRaises(ge.ErrorEstructura) as cm:
            ge.verificar_estructura(Cur(sin_tabla))
        self.assertIn("pagos_en_comprobantes_compras: la tabla no existe", str(cm.exception))


class Resolver(Base):
    def test_solo_true_cuenta_como_resuelta(self):
        for devuelto in (False, None, 0, "t"):
            with mock.patch.object(ge, "_destino", return_value=devuelto):
                with self.assertRaises(ge.ErrorCola):
                    ge.cola_resolver({}, 5, "cargada", {})
        with mock.patch.object(ge, "_destino", return_value=True):
            ge.cola_resolver({}, 5, "cargada", {})

    def test_si_la_cola_dice_false_la_pasada_se_corta_y_no_da_por_resuelta(self):
        cola = [{"id": 1, "modo": "en_seco"}, {"id": 2, "modo": "en_seco"}]
        with mock.patch.object(ge, "cola_tomar", side_effect=lambda c: cola.pop(0) if cola else None), \
                mock.patch.object(ge, "cargar_una", return_value={"estado": "simulada"}) as cargar, \
                mock.patch.object(ge, "_destino", return_value=False):
            ge.pasada({"CARGAS": "1", "CARGAS_POR_PASADA": "5"})
        self.assertEqual(cargar.call_count, 1)
        self.assertIn("ERROR al resolver la carga 1", open(g.LOG_LOCAL, encoding="utf-8").read())

    def test_pendiente_vuelve_a_la_cola_y_corta_la_pasada(self):
        cola = [{"id": 1, "modo": "en_seco"}, {"id": 2, "modo": "en_seco"}]
        res = mock.MagicMock()
        with mock.patch.object(ge, "cola_tomar", side_effect=lambda c: cola.pop(0) if cola else None), \
                mock.patch.object(ge, "cargar_una", return_value={"estado": "pendiente", "codigo": "ocupado"}) as cargar, \
                mock.patch.object(ge, "cola_resolver", res):
            ge.pasada({"CARGAS": "1", "CARGAS_POR_PASADA": "5"})
        self.assertEqual(cargar.call_count, 1)
        self.assertEqual(res.call_args.args[2], "pendiente")


class ErrE(Exception):
    def __init__(self, msg, pgcode=None):
        super().__init__(msg)
        self.pgcode = pgcode


class Excepciones(Base):
    def test_lock_y_timeout_son_pendientes(self):
        for pgcode in ("55P03", "57014"):
            r = ge._resultado_de_excepcion(ErrE("x", pgcode))
            self.assertEqual((r["estado"], r["codigo"]), ("pendiente", "ocupado"))

    def test_error_de_conexion_no_filtra_el_texto(self):
        import psycopg2
        e = psycopg2.OperationalError('connection to server at "10.1.2.3", port 5432 failed: Connection refused')
        r = ge._resultado_de_excepcion(e)
        self.assertEqual((r["estado"], r["codigo"]), ("pendiente", "sin_conexion"))
        self.assertNotIn("10.1.2.3", json.dumps(r))
        r = ge._resultado_de_excepcion(g.ErrorSync("rechazo con el host 10.1.2.3"))
        self.assertNotIn("10.1.2.3", json.dumps(r))

    def test_otra_excepcion_es_error(self):
        r = ge._resultado_de_excepcion(ValueError("boom"))
        self.assertEqual((r["estado"], r["codigo"]), ("error", "excepcion"))

    def test_en_una_retomada_nada_transitorio_ni_error_se_resuelve(self):
        for r in ({"estado": "error", "codigo": "estructura"}, {"estado": "pendiente", "codigo": "ocupado"}):
            self.assertEqual(ge._incierta(r, True)["estado"], "indeterminado")
            self.assertEqual(ge._incierta(r, False)["estado"], r["estado"])
        self.assertEqual(ge._incierta({"estado": "rechazada"}, True)["estado"], "rechazada")


class RetomadaNumerada(Base):
    """Retomada de un Remito X con numerar: la propuesta no trae número, así que se busca por otros datos."""

    def carga(self):
        c = carga_base()
        c["propuesta"].update(tipo_garro_id=5, numero=None, punto_venta=None, numerar="siguiente")
        return {"id": 31, "clave": "k", "modo": "real", "retomada": True, "payload": c}

    def cargar(self, resultado):
        with mock.patch.object(ge, "_buscar_lo_propio_numerado", return_value=resultado) as buscar, \
                mock.patch.object(ge, "_buscar_lo_propio") as comun, mock.patch.object(ge, "conectar_garro", side_effect=RuntimeError("no llega")):
            r = ge.cargar_una({"GARRO_BASE": "b"}, self.carga(), en_seco=False)
        buscar.assert_called_once()
        comun.assert_not_called()  # la búsqueda por número no sirve: el número no está en la propuesta
        return r

    def test_un_unico_resultado_es_ya_cargada_con_su_punto_de_venta_y_numero(self):
        r = self.cargar((77, True, {"punto_venta": 3, "numero": 78}))
        self.assertEqual((r["estado"], r["comprobante_id"], r["recuperada"], r["punto_venta"], r["numero"]), ("cargada", 77, True, 3, 78))

    def test_mas_de_uno_va_a_revisar_con_un_motivo_claro(self):
        r = self.cargar((None, True, {"problema": "varios", "motivo": "hay más de un comprobante del bot (ids 1, 2)"}))
        self.assertEqual((r["estado"], r["codigo"]), ("revisar", "varios_propios"))
        self.assertIn("ids 1, 2", r["motivo"])

    def test_la_huella_que_no_coincide_va_a_revisar(self):
        r = self.cargar((77, True, {"problema": "huella", "motivo": "alguien lo modificó"}))
        self.assertEqual((r["estado"], r["codigo"], r["comprobante_id"]), ("revisar", "huella_distinta", 77))

    def test_ninguno_conclusivo_sigue_como_una_carga_sin_numerar_y_no_concluyente_es_indeterminada(self):
        r = self.cargar((None, True, {}))
        # siguió al planificador (la conexión falsa falla: en una retomada eso es «indeterminado», pero no por la búsqueda)
        self.assertEqual(r["estado"], "indeterminado")
        self.assertNotEqual(r.get("codigo"), "busqueda_inconclusa")
        r = self.cargar((None, False, {}))
        self.assertEqual((r["estado"], r["codigo"]), ("indeterminado", "busqueda_inconclusa"))

    def test_si_el_diario_dice_que_se_escribio_y_ya_no_esta_no_se_reescribe(self):
        ge.diario_escribir("cargada", {"id": 31, "clave": "k"}, "b", escrito={"comprobantes_compras": [77], "numeracion": {"punto_venta": 3, "numero": 78}})
        r = self.cargar((None, True, {}))
        self.assertEqual((r["estado"], r["codigo"]), ("indeterminado", "escrita_y_ya_no_esta"))

    def test_una_busqueda_que_falla_es_indeterminada(self):
        with mock.patch.object(ge, "_buscar_lo_propio_numerado", side_effect=RuntimeError("sin red")):
            r = ge.cargar_una({"GARRO_BASE": "b"}, self.carga(), en_seco=False)
        self.assertEqual((r["estado"], r["codigo"]), ("indeterminado", "busqueda_fallida"))

    def test_sin_numero_ni_fecha_o_total_no_se_puede_buscar(self):
        c = self.carga()
        c["payload"].pop("total_pesos")
        with mock.patch.object(ge, "conectar_garro") as conectar:
            self.assertEqual(ge._buscar_lo_propio_numerado({"GARRO_BASE": "b"}, c, c["payload"]["propuesta"], None, "b"), (None, False, {}))
        conectar.assert_not_called()


class RetomadaSinBase(Base):
    CARGA = {"id": 8, "clave": "k", "modo": "real", "retomada": True, "payload": carga_base()}

    def test_encontrada_no_toca_ninguna_guarda(self):
        with mock.patch.object(ge, "_buscar_lo_propio", return_value=(77, True)), \
                mock.patch.object(ge, "conectar_garro") as conectar:
            r = ge.cargar_una({"GARRO_BASE": "b"}, dict(self.CARGA), en_seco=False)
        self.assertEqual((r["estado"], r["comprobante_id"], r["recuperada"]), ("cargada", 77, True))
        conectar.assert_not_called()

    def test_busqueda_que_falla_o_inconclusa_es_indeterminada_y_no_escribe(self):
        for busqueda in (mock.MagicMock(side_effect=RuntimeError("sin red")), mock.MagicMock(return_value=(None, False))):
            with mock.patch.object(ge, "_buscar_lo_propio", busqueda), mock.patch.object(ge, "conectar_garro") as conectar:
                r = ge.cargar_una({"GARRO_BASE": "b"}, dict(self.CARGA), en_seco=False)
            self.assertEqual(r["estado"], "indeterminado")
            conectar.assert_not_called()

    def test_retomada_real_con_la_llave_local_apagada_igual_busca_lo_propio(self):
        # La carga se tomó como real y pudo haberse escrito antes de que apagaran CARGAS_REAL: no puede terminar
        # como rechazada por «ya cargado».
        with mock.patch.object(ge, "_buscar_lo_propio", return_value=(77, True)) as buscar, \
                mock.patch.object(ge, "conectar_garro", side_effect=RuntimeError("no llega")):
            r = ge.cargar_una({"GARRO_BASE": "b"}, dict(self.CARGA), en_seco=True)
        buscar.assert_called_once()
        self.assertEqual((r["estado"], r["comprobante_id"], r["recuperada"]), ("cargada", 77, True))
        # No estaba escrita: se simula normal (acá la conexión falsa falla, que es un error común y no incierto).
        with mock.patch.object(ge, "_buscar_lo_propio", return_value=(None, True)), \
                mock.patch.object(ge, "conectar_garro", side_effect=RuntimeError("no llega")):
            r = ge.cargar_una({"GARRO_BASE": "b"}, dict(self.CARGA), en_seco=True)
        self.assertEqual(r["estado"], "error")
        # Búsqueda inconclusa: no se resuelve.
        with mock.patch.object(ge, "_buscar_lo_propio", return_value=(None, False)):
            r = ge.cargar_una({"GARRO_BASE": "b"}, dict(self.CARGA), en_seco=True)
        self.assertEqual(r["estado"], "indeterminado")

    def test_retomada_que_el_diario_da_por_escrita_y_ya_no_esta_no_se_reescribe(self):
        ge.diario_escribir("cargada", {"id": 8, "clave": "k"}, "b", escrito={"comprobantes_compras": [77]})
        with mock.patch.object(ge, "_buscar_lo_propio", return_value=(None, True)), \
                mock.patch.object(ge, "conectar_garro") as conectar:
            r = ge.cargar_una({"GARRO_BASE": "b"}, dict(self.CARGA), en_seco=False)
        self.assertEqual((r["estado"], r["codigo"]), ("indeterminado", "escrita_y_ya_no_esta"))
        conectar.assert_not_called()

    def test_retomada_de_una_carga_en_seco_no_busca(self):
        with mock.patch.object(ge, "_buscar_lo_propio") as buscar, \
                mock.patch.object(ge, "conectar_garro", side_effect=RuntimeError("x")):
            ge.cargar_una({"GARRO_BASE": "b"}, {**self.CARGA, "modo": "en_seco"}, en_seco=True)
        buscar.assert_not_called()

    def test_no_retomada_con_conexion_caida_vuelve_a_la_cola_y_retomada_queda_indeterminada(self):
        import psycopg2
        caida = psycopg2.OperationalError("could not connect to server 10.9.9.9")
        with mock.patch.object(ge, "conectar_garro", side_effect=caida):
            r = ge.cargar_una({"GARRO_BASE": "b"}, {**self.CARGA, "retomada": False}, en_seco=False)
            self.assertEqual((r["estado"], r["codigo"]), ("pendiente", "sin_conexion"))
            self.assertNotIn("10.9.9.9", json.dumps(r))
        with mock.patch.object(ge, "_buscar_lo_propio", return_value=(None, True)), \
                mock.patch.object(ge, "conectar_garro", side_effect=caida):
            r = ge.cargar_una({"GARRO_BASE": "b"}, dict(self.CARGA), en_seco=False)
        self.assertEqual(r["estado"], "indeterminado")


class AsientoEnCentavos(unittest.TestCase):
    def leidos(self, plan):
        return [(m["fk_id_plan_de_cuenta"], m["es_haber"], m["imp_asiento_contable_movimiento"]) for m in plan.asiento["movimientos"]]

    def test_coincide_con_el_plan(self):
        plan = gc.planificar(carga_base(), ref_base(), AHORA)
        ge.comparar_asiento(plan, self.leidos(plan))

    def test_un_centavo_de_diferencia_o_una_cuenta_distinta_se_rechaza(self):
        plan = gc.planificar(carga_base(), ref_base(), AHORA)
        for cambio in (lambda l: [(l[0][0], l[0][1], l[0][2] + 0.01)] + l[1:],
                       lambda l: [(999, l[0][1], l[0][2])] + l[1:],
                       lambda l: l[1:] + l[:1],          # otro orden
                       lambda l: l[:-1]):
            with self.assertRaises(AssertionError):
                ge.comparar_asiento(plan, cambio(self.leidos(plan)))

    def test_asiento_con_descuento_y_renglones_con_decimales_nunca_se_rechaza(self):
        azar = random.Random(20261125)
        for _ in range(400):
            n = azar.randint(1, 12)
            desc = azar.choice([0, -5, -10, -12.5, -20, -30])
            renglones = [{"cantidad": round(azar.uniform(0.1, 40), 3), "linea": round(azar.uniform(10, 900000), 2)} for _ in range(n)]
            armados = [{"cantidad": r["cantidad"], "unitario": r["linea"] / r["cantidad"], "iva": 21.0, "categoria": 33,
                        "cuenta": 109, "texto": "X", "linea": r["linea"]} for r in renglones]
            ref = ref_base()
            plan0 = gc.armar(tipo=ref.tipo, proveedor=ref.proveedores[0], punto_venta=7, numero=4521, fecha=date(2026, 11, 20),
                             ahora=AHORA, descuento_pct=desc, renglones=armados, percepciones=[], ref=ref)
            c = carga_base(total=gc.r2(plan0.total) + azar.choice([0, 0.2, -0.2]), descuento_pct=desc)
            c["propuesta"]["items"] = [{"cantidad_garro": r["cantidad"], "linea_pesos": r["linea"], "alicuota_iva": 21,
                                        "garro_cuenta_imputacion_id": 33, "garro_plan_de_cuenta_id": 109,
                                        "descripcion": "x"} for r in renglones]
            plan = gc.planificar(c, ref, AHORA)
            ge.comparar_asiento(plan, self.leidos(plan))
            movs = plan.asiento["movimientos"]
            self.assertEqual(sum(ge._centavos(m["imp_asiento_contable_movimiento"]) for m in movs if m["es_debe"]),
                             ge._centavos(movs[0]["imp_asiento_contable_movimiento"]))


# ------------------------------------------------------------------ movimientos de fondos

class CursorGuion:
    """Cursor que contesta según un guion (texto que aparece en la consulta -> filas). Una consulta que el guion
    no prevé hace fallar el test."""

    def __init__(self, guion):
        self.guion, self.consultas, self.filas = guion, [], []

    def execute(self, sql, params=None):
        self.consultas.append((sql, params))
        for clave, filas in self.guion:
            if clave in sql:
                self.filas = filas
                return
        raise AssertionError(f"consulta no prevista: {sql[:90]}")

    def fetchone(self):
        return self.filas[0] if self.filas else None

    def fetchall(self):
        return list(self.filas)


def filas_del_plan(plan, mid=700, medio=55, vinculo=33):
    """Las filas completas (en el orden de las columnas de la receta) que Garro devolvería si lo escrito es lo del plan."""
    t_medio, t_vinculo = gm.TABLAS_EFECTIVO if plan.medio == "efectivo" else gm.TABLAS_BANCO
    clave = "fk_id_efectivo_medio_de_pago" if plan.medio == "efectivo" else "fk_id_movimiento_cuenta_bancaria"
    dicts = {
        "movimientos_fondos": {"id_movimiento_fondo": mid, **plan.movimiento},
        t_medio: {gm.COLUMNAS[t_medio][0]: medio, **plan.medio_fila},
        t_vinculo: {gm.COLUMNAS[t_vinculo][0]: vinculo, "fk_id_movimiento_fondo": mid, clave: medio, **plan.vinculo},
    }
    return {t: [tuple(d[c] for c in gm.COLUMNAS[t])] for t, d in dicts.items()}


def guion_movimiento(plan, mid=700, medio=55, vinculo=33, filas=None, **cambios):
    """Lo que devolvería Garro si lo escrito coincide con el plan; `filas` pisa las filas de una tabla y `cambios`, una consulta."""
    efectivo = plan.medio == "efectivo"
    f = {**filas_del_plan(plan, mid, medio, vinculo), **(filas or {})}
    guion = {
        "SELECT id_movimiento_fondo FROM movimientos_fondos": [(mid,)],
        "SELECT id_efectivo_medio_de_pago_en_movimiento_fondo, fk_id_efectivo_medio_de_pago": [(vinculo, medio)] if efectivo else [],
        "SELECT id_movimiento_cuenta_bancaria_en_movimiento_fondo, fk_id_movimiento_cuenta_bancaria": [] if efectivo else [(vinculo, medio)],
        "FROM asientos_contables": [(0,)],
    }
    guion.update({f'FROM public."{t}" WHERE': rows for t, rows in f.items()})
    guion.update(cambios)
    return list(guion.items())


def escrito_mov(medio="efectivo", mid=700, medio_id=55, vinculo=33):
    e = {t: [] for t in gm.TABLAS_ESCRITO}
    e["movimientos_fondos"] = [mid]
    t_medio, t_vinculo = gm.TABLAS_EFECTIVO if medio == "efectivo" else gm.TABLAS_BANCO
    e[t_medio], e[t_vinculo] = [medio_id], [vinculo]
    e["clase"] = "movimiento_fondo"
    return e


def distinto(valor):
    if isinstance(valor, bool):
        return not valor
    if isinstance(valor, (int, float)):
        return valor + 0.01 if isinstance(valor, float) else valor + 1
    if isinstance(valor, datetime):
        return valor.replace(year=valor.year + 1)
    if isinstance(valor, date):
        return date(valor.year + 1, 1, 1)
    return "x" if valor is None else str(valor) + "x"


class VerificarMovimiento(unittest.TestCase):
    def plan(self, medio="efectivo"):
        return gm.planificar(propuesta_mov() if medio == "efectivo" else transf_mov(), ref_mov(), AHORA_MOV)

    def falla(self, plan, escrito, **kw):
        with self.assertRaises(AssertionError):
            ge.verificar_movimiento(CursorGuion(guion_movimiento(plan, **kw)), plan, escrito)

    def test_lo_que_coincide_con_el_plan_pasa_en_efectivo_y_en_transferencia(self):
        for medio in ("efectivo", "transferencia"):
            plan = self.plan(medio)
            ge.verificar_movimiento(CursorGuion(guion_movimiento(plan)), plan, escrito_mov(medio))

    def test_cualquier_columna_de_cualquier_fila_distinta_se_detecta(self):
        for medio in ("efectivo", "transferencia"):
            plan = self.plan(medio)
            for tabla, filas in filas_del_plan(plan).items():
                for k, columna in enumerate(gm.COLUMNAS[tabla]):
                    fila = list(filas[0])
                    fila[k] = distinto(fila[k])
                    with self.assertRaises(AssertionError, msg=f"{tabla}.{columna}") as cm:
                        ge.verificar_movimiento(CursorGuion(guion_movimiento(plan, filas={tabla: [tuple(fila)]})), plan, escrito_mov(medio))
                    if k:
                        self.assertIn(columna, str(cm.exception))

    def test_un_centavo_de_diferencia_en_el_importe(self):
        for medio, tabla, columna in (("efectivo", "efectivo_medio_de_pago", "imp_efectivo_medio_de_pago"),
                                      ("transferencia", "movimientos_cuentas_bancarias", "imp_movimiento_cuenta_bancaria")):
            plan = self.plan(medio)
            for delta in (Decimal("0.01"), Decimal("-0.01"), Decimal("1000")):
                fila = list(filas_del_plan(plan)[tabla][0])
                k = gm.COLUMNAS[tabla].index(columna)
                fila[k] = Decimal(str(fila[k])) + delta
                self.falla(plan, escrito_mov(medio), filas={tabla: [tuple(fila)]})

    def test_un_importe_que_difiere_menos_de_un_centavo_no_es_un_error(self):
        plan = self.plan()
        fila = list(filas_del_plan(plan)["efectivo_medio_de_pago"][0])
        k = gm.COLUMNAS["efectivo_medio_de_pago"].index("imp_efectivo_medio_de_pago")
        fila[k] = fila[k] + 0.0000001
        ge.verificar_movimiento(CursorGuion(guion_movimiento(plan, filas={"efectivo_medio_de_pago": [tuple(fila)]})), plan, escrito_mov())

    def test_la_fila_no_esta_o_hay_mas_de_una(self):
        plan = self.plan()
        self.falla(plan, escrito_mov(), filas={"movimientos_fondos": []})
        self.falla(plan, escrito_mov(), filas={"movimientos_fondos": filas_del_plan(plan)["movimientos_fondos"] * 2})

    def test_lo_que_cuelga_no_es_lo_escrito(self):
        plan = self.plan()
        self.falla(plan, escrito_mov(), **{"SELECT id_movimiento_fondo FROM movimientos_fondos": []})
        self.falla(plan, escrito_mov(vinculo=34))
        self.falla(plan, escrito_mov(medio_id=56))
        self.falla(plan, escrito_mov(), **{"SELECT id_efectivo_medio_de_pago_en_movimiento_fondo, fk_id_efectivo_medio_de_pago": [(33, 55), (34, 56)]})
        self.falla(plan, escrito_mov(), **{"SELECT id_movimiento_cuenta_bancaria_en_movimiento_fondo, fk_id_movimiento_cuenta_bancaria": [(40, 900)]})

    def test_si_el_plan_era_transferencia_y_quedo_efectivo(self):
        plan = self.plan("transferencia")
        self.falla(plan, escrito_mov("transferencia"), **{
            "SELECT id_efectivo_medio_de_pago_en_movimiento_fondo, fk_id_efectivo_medio_de_pago": [(33, 55)]})

    def test_si_el_plan_era_efectivo_y_quedo_banco(self):
        self.falla(self.plan(), escrito_mov(), **{
            "SELECT id_movimiento_cuenta_bancaria_en_movimiento_fondo, fk_id_movimiento_cuenta_bancaria": [(33, 55)]})

    def test_un_asiento_colgado_es_un_error(self):
        self.falla(self.plan(), escrito_mov(), **{"FROM asientos_contables": [(1,)]})

    def test_el_asiento_se_busca_por_el_movimiento_y_por_el_movimiento_bancario(self):
        plan = self.plan("transferencia")
        cur = CursorGuion(guion_movimiento(plan))
        ge.verificar_movimiento(cur, plan, escrito_mov("transferencia"))
        sql, params = [c for c in cur.consultas if "asientos_contables" in c[0]][0]
        self.assertIn("fk_id_movimiento_fondo", sql)
        self.assertIn("fk_id_movimiento_cuenta_bancaria", sql)
        self.assertEqual(params, (700, [55]))


class NormalizarEscritoMovimiento(unittest.TestCase):
    def test_efectivo_y_banco_validos(self):
        for medio in ("efectivo", "transferencia"):
            r = ge._normalizar_escrito({**escrito_mov(medio), "huella": "h"})
            self.assertEqual((r["clase"], r["huella"], r["movimientos_fondos"]), ("movimiento_fondo", "h", [700]))

    def test_lo_escrito_sin_clase_es_de_compras(self):
        e = {t: [] for t in ge.TABLAS_ESCRITO}
        e["comprobantes_compras"] = [5]
        e["huella"] = "h"
        self.assertEqual(ge._normalizar_escrito(e)["clase"], "compra")

    def test_registros_invalidos(self):
        casos = []
        base = {**escrito_mov(), "huella": "h"}
        casos.append({**base, "clase": "otra"})
        casos.append({k: v for k, v in base.items() if k != "huella"})
        casos.append({**base, "huella": ""})
        casos.append({**base, "movimientos_fondos": []})
        casos.append({**base, "movimientos_fondos": [700, 701]})
        casos.append({**base, "efectivo_medio_de_pago": []})                                     # medio sin vínculo
        casos.append({**base, "movimientos_cuentas_bancarias": [9], "movimientos_cuenta_bancaria_en_movimientos_fondos": [9]})  # los dos medios
        casos.append({**{k: ([] if isinstance(v, list) else v) for k, v in base.items()}, "movimientos_fondos": [700]})  # sin medio
        casos.append({**base, "movimientos_fondos": [True]})
        casos.append({**base, "efectivo_medio_de_pago": ["55"]})
        casos.append({**base, "efectivo_medio_de_pago": 55})
        for e in casos:
            with self.assertRaises(ge.ErrorDeshacer) as cm:
                ge._normalizar_escrito(e)
            self.assertEqual(cm.exception.codigo, "escrito_invalido")


class FlujoDeMovimiento(Base):
    CFG = {"GARRO_BASE": "real"}

    def carga(self, p=None, **kw):
        return {"id": 21, "clave": "m1", "modo": "real", "retomada": False,
                "payload": {"propuesta": p if p is not None else propuesta_mov()}, **kw}

    def correr(self, en_seco, *, carga=None, ref=None, verificar=None, commit=None, extra=()):
        con = mock.MagicMock()
        if commit is not None:
            con.commit.side_effect = commit
        escrito = {**escrito_mov(), "huella": None}
        parches = [
            mock.patch.object(ge, "conectar_garro", return_value=con),
            mock.patch.object(ge, "verificar_estructura", return_value={}),
            mock.patch.object(ge, "leer_referencias_movimiento", return_value=ref or ref_mov()),
            mock.patch.object(ge, "leer_referencias"),
            mock.patch.object(ge, "escribir_movimiento", return_value=escrito),
            mock.patch.object(ge, "verificar_movimiento", side_effect=verificar),
            mock.patch.object(ge, "huella", return_value="h"),
            mock.patch.object(ge, "escribir"),
            *extra,
        ]
        mocks = [p.start() for p in parches]
        self.addCleanup(lambda: [p.stop() for p in reversed(parches)])
        r = ge.cargar_una(self.CFG, carga or self.carga(), en_seco=en_seco, ahora=AHORA_MOV)
        return r, con, dict(zip(("conectar", "estructura", "refs_mov", "refs_compra", "escribir_mov", "verificar", "huella", "escribir"), mocks))

    def test_en_seco_solo_lee_y_devuelve_el_plan(self):
        r, con, m = self.correr(True)
        self.assertEqual(r["estado"], "simulada")
        self.assertEqual((r["plan"]["clase"], r["plan"]["medio"], r["plan"]["asiento"]), ("movimiento_fondo", "efectivo", False))
        m["escribir_mov"].assert_not_called()
        con.commit.assert_not_called()
        con.rollback.assert_called()
        self.assertEqual(m["conectar"].call_args.kwargs["solo_lectura"], True)
        self.assertNotIn("LOCK", " ".join(c.args[0] for c in con.cursor.return_value.execute.call_args_list))
        self.assertFalse(os.path.exists(ge.DIARIO))

    def test_real_usa_la_receta_y_el_lock_de_movimientos_y_no_toca_la_de_compras(self):
        r, con, m = self.correr(False)
        self.assertEqual((r["estado"], r["clase"], r["movimiento_id"]), ("cargada", "movimiento_fondo", 700))
        self.assertEqual(r["escrito"]["huella"], "h")
        m["estructura"].assert_called_once()
        self.assertEqual(m["estructura"].call_args.args[1:], (gm.COLUMNAS, gm.TIPOS))
        m["refs_compra"].assert_not_called()
        m["escribir"].assert_not_called()
        sentencias = [c.args[0] for c in con.cursor.return_value.execute.call_args_list]
        self.assertEqual(sentencias, [ge.LOCK_TABLAS_MOVIMIENTO])
        self.assertNotIn("comprobantes_compras", ge.LOCK_TABLAS_MOVIMIENTO)
        con.commit.assert_called_once()
        eventos = [json.loads(l)["evento"] for l in open(ge.DIARIO, encoding="utf-8")]
        self.assertEqual(eventos, ["intento", "cargada"])

    def test_un_rechazo_del_planificador_no_escribe_ni_confirma(self):
        for cambio, codigo in (({"importe": 0}, "importe"), ({"tipo_mov_id": 62}, "tipo_no_habilitado"),
                               ({"requiere_revision": True}, "requiere_revision")):
            r, con, m = self.correr(False, carga=self.carga(propuesta_mov(**cambio)))
            self.assertEqual((r["estado"], r["codigo"]), ("rechazada", codigo))
            m["escribir_mov"].assert_not_called()
            con.commit.assert_not_called()
            con.rollback.assert_called()

    def test_un_duplicado_leido_en_la_transaccion_no_escribe(self):
        ref = ref_mov(existentes=[{"id": 8, "tipo": 50, "descripcion": "periodo 10/2026", "fecha": datetime(2026, 11, 26), "importe": 1.0, "egreso": True}])
        r, con, m = self.correr(False, ref=ref)
        self.assertEqual((r["estado"], r["codigo"]), ("rechazada", "ya_cargado"))
        m["escribir_mov"].assert_not_called()
        con.commit.assert_not_called()

    def test_si_la_verificacion_falla_hace_rollback_y_no_confirma(self):
        r, con, m = self.correr(False, verificar=AssertionError("el importe no coincide"))
        self.assertEqual((r["estado"], r["codigo"]), ("error", "excepcion"))
        con.commit.assert_not_called()
        con.rollback.assert_called()

    def test_un_commit_que_falla_es_incierto_y_no_se_resuelve(self):
        r, con, m = self.correr(False, commit=RuntimeError("se cortó"))
        self.assertEqual((r["estado"], r["codigo"]), ("indeterminado", "commit_incierto"))

    def test_garro_ocupado_vuelve_a_la_cola_y_no_escribe(self):
        r, con, m = self.correr(False, extra=[mock.patch.object(ge, "leer_referencias_movimiento", side_effect=ErrE("lock", "55P03"))])
        self.assertEqual((r["estado"], r["codigo"]), ("pendiente", "ocupado"))
        m["escribir_mov"].assert_not_called()

    def test_la_estructura_de_movimientos_rota_rechaza_el_movimiento_pero_no_las_compras(self):
        def estructura(cur, columnas=None, tipos=None):
            if columnas is gm.COLUMNAS:
                raise ge.ErrorEstructura("cambió movimientos_fondos")
            return {}
        r, con, m = self.correr(False, extra=[mock.patch.object(ge, "verificar_estructura", side_effect=estructura)])
        self.assertEqual((r["estado"], r["codigo"]), ("error", "estructura"))
        m["escribir_mov"].assert_not_called()
        # la misma estructura rota no frena una compra
        with mock.patch.object(ge, "conectar_garro", return_value=mock.MagicMock()), \
                mock.patch.object(ge, "verificar_estructura", side_effect=estructura), \
                mock.patch.object(ge, "leer_referencias", return_value=ref_base()):
            r = ge.cargar_una(self.CFG, {"id": 5, "modo": "en_seco", "payload": carga_base()}, en_seco=True, ahora=AHORA)
        self.assertEqual(r["estado"], "simulada")

    def test_la_estructura_de_compras_rota_no_frena_los_movimientos(self):
        def estructura(cur, columnas=None, tipos=None):
            if columnas is None:
                raise ge.ErrorEstructura("cambió comprobantes_compras")
            return {}
        r, con, m = self.correr(True, extra=[mock.patch.object(ge, "verificar_estructura", side_effect=estructura)])
        self.assertEqual(r["estado"], "simulada")

    def test_clase_desconocida_o_que_no_coincide_se_rechaza_sin_conectar(self):
        for p, cola in (({"clase": "pago"}, None), ({"clase": "compra"}, "movimiento_fondo"), (propuesta_mov(), "compra"),
                        ({"clase": ["compra"]}, None), ({"clase": 1}, None), ({}, "pago")):
            with mock.patch.object(ge, "conectar_garro") as conectar:
                carga = {"id": 1, "modo": "real", "payload": {"propuesta": p}}
                if cola:
                    carga["clase"] = cola
                r = ge.cargar_una(self.CFG, carga, en_seco=False)
            self.assertEqual((r["estado"], r.get("codigo")), ("rechazada", "clase"), (p, cola))
            conectar.assert_not_called()

    def test_la_clase_puede_venir_de_la_cola_o_de_la_propuesta(self):
        p = propuesta_mov()
        del p["clase"]
        r, con, m = self.correr(True, carga=self.carga(p, clase="movimiento_fondo"))
        self.assertEqual(r["plan"]["clase"], "movimiento_fondo")
        r, con, m = self.correr(True, carga=self.carga(propuesta_mov(), clase="movimiento_fondo"))
        self.assertEqual(r["estado"], "simulada")

    def test_sin_clase_en_ningun_lado_es_una_compra(self):
        with mock.patch.object(ge, "conectar_garro", return_value=mock.MagicMock()), \
                mock.patch.object(ge, "verificar_estructura", return_value={}), \
                mock.patch.object(ge, "leer_referencias", return_value=ref_base()), \
                mock.patch.object(ge, "leer_referencias_movimiento") as leer_mov:
            r = ge.cargar_una(self.CFG, {"id": 5, "modo": "en_seco", "payload": carga_base()}, en_seco=True, ahora=AHORA)
        self.assertEqual(r["estado"], "simulada")
        leer_mov.assert_not_called()


class RetomadaDeMovimiento(Base):
    CARGA = {"id": 8, "clave": "m", "modo": "real", "retomada": True, "payload": {"propuesta": propuesta_mov()}}
    CFG = {"GARRO_BASE": "b"}

    def con_intento(self, carga_id=8, base="b"):
        ge.diario_escribir("intento", {"id": carga_id, "clave": "m"}, base, total=1.0)

    def retomar(self, busqueda=None, **kw):
        with mock.patch.object(ge, "_buscar_movimiento_propio", busqueda or mock.MagicMock(return_value=(None, True))) as buscar, \
                mock.patch.object(ge, "_buscar_lo_propio") as de_compras, \
                mock.patch.object(ge, "conectar_garro", side_effect=RuntimeError("no llega")) as conectar:
            r = ge.cargar_una(self.CFG, {**self.CARGA, **kw}, en_seco=False)
        return r, buscar, de_compras, conectar

    def test_con_intento_y_encontrada_no_toca_las_guardas_ni_la_receta_de_compras(self):
        self.con_intento()
        r, buscar, de_compras, conectar = self.retomar(mock.MagicMock(return_value=(77, True)))
        buscar.assert_called_once()
        de_compras.assert_not_called()
        self.assertEqual((r["estado"], r["movimiento_id"], r["recuperada"], r["clase"]), ("cargada", 77, True, "movimiento_fondo"))
        self.assertNotIn("comprobante_id", r)
        conectar.assert_not_called()

    def test_sin_intento_de_esta_carga_no_se_toma_nada_por_propio(self):
        # Ni el intento de otra carga, ni el de otra base: esta carga nunca empezó a escribir, se planifica como nueva.
        self.con_intento(carga_id=9)
        self.con_intento(base="otra")
        r, buscar, _, conectar = self.retomar(mock.MagicMock(return_value=(77, True)))
        buscar.assert_not_called()
        conectar.assert_called()
        self.assertEqual(r["estado"], "indeterminado")      # la retomada con un error no resuelve

    def test_inconclusa_o_fallida_es_indeterminada(self):
        self.con_intento()
        for busqueda in (mock.MagicMock(side_effect=RuntimeError("sin red")), mock.MagicMock(return_value=(None, False))):
            r, _, _, conectar = self.retomar(busqueda)
            self.assertEqual(r["estado"], "indeterminado")
            conectar.assert_not_called()

    def test_un_intento_sin_resultado_y_sin_nada_en_el_programa_no_se_replanifica(self):
        self.con_intento()
        r, _, _, conectar = self.retomar()
        self.assertEqual((r["estado"], r["codigo"]), ("indeterminado", "intento_sin_resultado"))
        conectar.assert_not_called()

    def test_escrita_en_el_diario_y_ya_no_esta_no_se_reescribe(self):
        self.con_intento()
        ge.diario_escribir("cargada", {"id": 8, "clave": "m"}, "b", escrito=escrito_mov())
        r, _, _, conectar = self.retomar()
        self.assertEqual((r["estado"], r["codigo"]), ("indeterminado", "escrita_y_ya_no_esta"))
        conectar.assert_not_called()

    def test_deshecha_y_ya_no_esta_se_planifica_de_nuevo(self):
        self.con_intento()
        ge.diario_escribir("cargada", {"id": 8, "clave": "m"}, "b", escrito=escrito_mov())
        ge.diario_escribir("deshecha", {"id": 8, "clave": "m"}, "b", escrito=escrito_mov())
        r, _, _, conectar = self.retomar()
        conectar.assert_called()

    def test_una_retomada_de_una_carga_en_seco_no_busca(self):
        self.con_intento()
        r, buscar, _, _ = self.retomar(modo="en_seco")
        buscar.assert_not_called()

    def test_buscar_lo_propio_por_operario_tipo_descripcion_fecha_e_importe(self):
        def buscar(filas, operarios=((77,),), p=None):
            cur = CursorGuion([("FROM operarios", list(operarios)), ("FROM movimientos_fondos", list(filas))])
            con = mock.MagicMock()
            con.cursor.return_value = cur
            with mock.patch.object(ge, "conectar_garro", return_value=con):
                return ge._buscar_movimiento_propio({}, p or propuesta_mov(descripcion="  com e ind per 10/2026 ", importe=123456.78, fecha="2026-11-24"), None), cur
        (res, cur) = buscar([(700,)])
        self.assertEqual(res, (700, True))
        sql, params = [c for c in cur.consultas if "FROM movimientos_fondos" in c[0]][0]
        self.assertEqual(params, (77, 50, "COM E IND PER 10/2026", date(2026, 11, 24), 12345678))
        self.assertIn("des_movimiento_fondo = %s", sql)
        self.assertIn("fec_movimiento_fondo::date = %s", sql)
        self.assertIn("* 100) = %s", sql)
        self.assertEqual(buscar([])[0], (None, True))
        self.assertEqual(buscar([(700,), (701,)])[0], (None, False))
        self.assertEqual(buscar([(700,)], operarios=())[0], (None, False))
        self.assertEqual(buscar([(700,)], operarios=((1,), (2,)))[0], (None, False))
        for malo in (propuesta_mov(descripcion=""), propuesta_mov(descripcion=None), propuesta_mov(tipo_mov_id="x"),
                     propuesta_mov(importe=0), propuesta_mov(importe="5"), propuesta_mov(fecha="mañana"), propuesta_mov(fecha=None)):
            with mock.patch.object(ge, "conectar_garro") as conectar:
                self.assertEqual(ge._buscar_movimiento_propio({}, malo, None), (None, False))
            conectar.assert_not_called()


class ColaDeMovimientos(Base):
    def test_toma_las_dos_clases(self):
        with mock.patch.object(ge, "_destino", return_value={"id": 1}) as destino:
            self.assertEqual(ge.cola_tomar({}), {"id": 1})
        self.assertEqual(destino.call_args.args[1], "SELECT garro.cargas_tomar(ARRAY['compra','movimiento_fondo'])")

    def test_sin_la_funcion_nueva_las_compras_siguen_con_la_vieja(self):
        llamadas = []

        def destino(cfg, sql, params=()):
            llamadas.append(sql)
            if "ARRAY" in sql:
                raise ge.ColaSinFunciones()
            return {"id": 3}
        with mock.patch.object(ge, "_destino", side_effect=destino):
            self.assertEqual(ge.cola_tomar({}), {"id": 3})
        self.assertEqual(llamadas, ["SELECT garro.cargas_tomar(ARRAY['compra','movimiento_fondo'])", "SELECT garro.cargas_tomar()"])
        with mock.patch.object(ge, "_destino", side_effect=ge.ColaSinFunciones()):
            with self.assertRaises(ge.ColaSinFunciones):
                ge.cola_tomar({})
        with mock.patch.object(ge, "_destino", side_effect=RuntimeError("sin red")):
            with self.assertRaises(RuntimeError):
                ge.cola_tomar({})

    def test_una_clase_que_el_servidor_no_conoce_nunca_llega_a_escribir(self):
        # Una cola vieja o con una clase nueva puede devolver cualquier cosa: se rechaza, no se escribe.
        cola = [{"id": 1, "modo": "real", "clase": "pago", "payload": {"propuesta": {}}}]
        res = mock.MagicMock()
        with mock.patch.object(ge, "cola_tomar", side_effect=lambda c: cola.pop(0) if cola else None), \
                mock.patch.object(ge, "conectar_garro") as conectar, mock.patch.object(ge, "cola_resolver", res):
            ge.pasada({"CARGAS": "1", "CARGAS_REAL": "1"})
        conectar.assert_not_called()
        self.assertEqual(res.call_args.args[2], "rechazada")


class EstructuraPorReceta(Base):
    def filas(self, columnas, tipos):
        return [(t, c, None, tipos[t].get(c, "integer")) for t, cols in columnas.items() for c in cols]

    def cursor(self, filas):
        class Cur(CursorFalso):
            def __init__(self):
                super().__init__()
                self.resultado = []

            def execute(self, sql, params=None):
                self.resultado = filas if "information_schema" in sql else []

            def fetchall(self):
                return self.resultado
        return Cur()

    def test_cada_receta_mira_solo_sus_tablas(self):
        mov = self.filas(gm.COLUMNAS, gm.TIPOS)
        comp = self.filas(gc.COLUMNAS, gc.TIPOS)
        # las tablas de compras no existen: la receta de movimientos no se entera
        largos = ge.verificar_estructura(self.cursor(mov), gm.COLUMNAS, gm.TIPOS)
        self.assertEqual(len(largos), sum(len(c) for c in gm.COLUMNAS.values()))
        # y al revés
        ge.verificar_estructura(self.cursor(comp))

    def test_movimientos_con_columna_de_mas_de_menos_o_de_otro_tipo(self):
        mov = self.filas(gm.COLUMNAS, gm.TIPOS)
        casos = (
            (mov + [("movimientos_fondos", "nueva", None, "integer")], "movimientos_fondos: columnas de más nueva"),
            ([f for f in mov if f[1] != "centro_costos"], "faltan columnas centro_costos"),
            ([(t, c, l, "text" if c == "des_movimiento_fondo" else d) for t, c, l, d in mov],
             "movimientos_fondos.des_movimiento_fondo: es text y se esperaba character varying"),
            ([f for f in mov if f[0] != "efectivo_medio_de_pago"], "efectivo_medio_de_pago: la tabla no existe"),
        )
        for filas, texto in casos:
            with self.assertRaises(ge.ErrorEstructura) as cm:
                ge.verificar_estructura(self.cursor(filas), gm.COLUMNAS, gm.TIPOS)
            self.assertIn(texto, str(cm.exception))

    def test_un_trigger_en_una_tabla_de_movimientos_frena(self):
        mov = self.filas(gm.COLUMNAS, gm.TIPOS)

        class Cur(CursorFalso):
            def __init__(self):
                super().__init__()
                self.resultado = []

            def execute(self, sql, params=None):
                self.resultado = mov if "information_schema" in sql else ([("movimientos_fondos", "trg_x")] if "pg_trigger" in sql else [])

            def fetchall(self):
                return self.resultado
        with self.assertRaises(ge.ErrorEstructura) as cm:
            ge.verificar_estructura(Cur(), gm.COLUMNAS, gm.TIPOS)
        self.assertIn("movimientos_fondos: tiene un trigger (trg_x)", str(cm.exception))


class InsertarMovimientos(Base):
    def test_las_tablas_de_movimientos_se_insertan_con_su_receta(self):
        cur = CursorFalso()
        ge._insertar(cur, "efectivo_medio_de_pago_en_movimientos_fondos", {"fk_id_caja": 1},
                     fk_id_movimiento_fondo=3, fk_id_efectivo_medio_de_pago=4)
        sql, params = cur.sentencias[0]
        self.assertIn("RETURNING id_efectivo_medio_de_pago_en_movimiento_fondo", sql)
        self.assertEqual(params, [3, 4, 1])
        with self.assertRaises(ge.ErrorPlan):
            ge._insertar(cur, "efectivo_medio_de_pago_en_movimientos_fondos", {"fk_id_caja": 1}, fk_id_movimiento_fondo=3)


if __name__ == "__main__":
    unittest.main()
