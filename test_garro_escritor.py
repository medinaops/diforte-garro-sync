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
from datetime import date
from unittest import mock

import garro_cargas as gc
import garro_escritor as ge
import garro_sync as g
from test_garro_cargas import AHORA, carga_base, ref_base


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
                    mock.patch.dict(sys.modules, {"garro_escritor": fake}):
                self.assertEqual(g.correr_pedido("x"), 0)
        self.assertIn("(cargas) ERROR", open(g.LOG_LOCAL, encoding="utf-8").read())

    def test_corre_despues_del_pedido_y_antes_del_relevamiento(self):
        orden = []
        with mock.patch.object(g, "_atender_pedido", side_effect=lambda c: orden.append("pedido") or 1), \
                mock.patch.object(g, "cargas_auto", side_effect=lambda c: orden.append("cargas")), \
                mock.patch.object(g, "relevamiento_auto", side_effect=lambda c: orden.append("relevamiento")):
            self.assertEqual(g.correr_pedido("x"), 1)
        self.assertEqual(orden, ["pedido", "cargas", "relevamiento"])


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


if __name__ == "__main__":
    unittest.main()
