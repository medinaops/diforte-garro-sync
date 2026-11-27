"""Tests de los pedidos remotos (garro_comandos.py): sin red ni base; los subprocesos son reales."""

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import garro_comandos as gc
import garro_escritor as ge
import garro_recuperar as gr
import garro_sync as g
import socket

AQUI = os.path.dirname(os.path.abspath(__file__))
CFG = {"GARRO_HOST": "garro.local", "GARRO_PUERTO": "5432", "GARRO_BASE": "GestionIntegral", "GARRO_USUARIO": "u",
       "GARRO_PASSWORD": "clave-garro-9", "SUPABASE_HOST": "h", "SUPABASE_PUERTO": "5432", "SUPABASE_USUARIO": "garro_sync.x",
       "SUPABASE_PASSWORD": "p@ss/w:rd#sb", "OTRA_URL": "postgresql://garro_sync:cadenaSecreta7@host:5432/postgres",
       "CARGAS": "1"}
BASE = "gestion_prueba"


def invalido(test, tipo, args, cfg=CFG, texto=None):
    with test.assertRaises(gc.ErrorComando, msg=f"{tipo} {args}") as c:
        gc.validar(tipo, args, cfg)
    if texto:
        test.assertIn(texto, str(c.exception))


class Validacion(unittest.TestCase):
    def test_tipo_desconocido_o_no_texto(self):
        for t in ("rm", "", None, 5, "VERSION", "version ", ["version"], "logs; ls"):
            invalido(self, t, {})

    def test_los_sin_args_rechazan_cualquier_clave(self):
        for tipo in ("version", "estructura", "perfil", "copia_medir"):
            self.assertEqual(gc.validar(tipo, {}, CFG), {})
            invalido(self, tipo, {"base": BASE}, texto="desconocidos")
            invalido(self, tipo, [], texto="objeto")
            invalido(self, tipo, None)
            invalido(self, tipo, "x")

    def test_base_solo_de_copia_y_distinta_de_la_real(self):
        for malo in ("GestionIntegral", "gestion_prueba\n", "Gestion_prueba", "gestion_prueba2", "gestion_prueba; drop", "gestion",
                     "", None, 5, "gestion_prueba ", " gestion_prueba", "gestion_prueba_" + "x" * 40 + "_prueba", "otra_prueba",
                     "gestion_prueba\x00", "gestion_prueba/../x_prueba", "gestion_pruebá"):
            for tipo in ("copia_crear", "copia_borrar"):
                invalido(self, tipo, {"base": malo})
        for bueno in ("gestion_prueba", "gestion_prueba_2_prueba", "gestion_prueba_x_prueba"):
            self.assertEqual(gc.validar("copia_crear", {"base": bueno}, CFG), {"base": bueno})

    def test_la_base_real_se_rechaza_aunque_tenga_forma_de_copia(self):
        for real in ("gestion_prueba", "GESTION_PRUEBA", " Gestion_Prueba "):
            invalido(self, "copia_crear", {"base": "gestion_prueba"}, {**CFG, "GARRO_BASE": real}, "base real")

    def test_copia_exige_base_y_rechaza_extras(self):
        invalido(self, "copia_crear", {}, texto="faltan")
        invalido(self, "copia_borrar", {"base": BASE, "x": 1}, texto="desconocidos")

    def test_escritor_archivo(self):
        r = gc.validar("escritor_archivo", {"payload": {"a": 1}}, CFG)
        self.assertEqual(r, {"payload": {"a": 1}, "escribir": False, "base": None})
        r = gc.validar("escritor_archivo", {"payload": {}, "escribir": True, "base": BASE}, CFG)
        self.assertTrue(r["escribir"])
        self.assertEqual(gc.validar("escritor_archivo", {"payload": {}, "base": BASE}, CFG)["base"], BASE)  # simula contra la copia
        invalido(self, "escritor_archivo", {}, texto="faltan")
        invalido(self, "escritor_archivo", {"payload": []}, texto="objeto")
        invalido(self, "escritor_archivo", {"payload": "x"})
        invalido(self, "escritor_archivo", {"payload": {}, "escribir": "true"}, texto="true o false")
        invalido(self, "escritor_archivo", {"payload": {}, "escribir": 1})
        invalido(self, "escritor_archivo", {"payload": {}, "escribir": True}, texto="nunca se escribe en la base real")
        invalido(self, "escritor_archivo", {"payload": {}, "escribir": True, "base": None})
        invalido(self, "escritor_archivo", {"payload": {}, "escribir": True, "base": "GestionIntegral"})
        invalido(self, "escritor_archivo", {"payload": {}, "escribir": True, "base": "gestion_prueba"}, {**CFG, "GARRO_BASE": "gestion_prueba"})
        invalido(self, "escritor_archivo", {"payload": {}, "otra": 1})

    def test_payload_de_mas_de_20_kb(self):
        invalido(self, "escritor_archivo", {"payload": {"x": "p" * 21000}}, texto="20 KB")
        gc.validar("escritor_archivo", {"payload": {"x": "p" * 19000}}, CFG)
        # los acentos pesan más en bytes que en caracteres
        invalido(self, "escritor_archivo", {"payload": {"x": "á" * 11000}}, texto="20 KB")

    def test_comparar(self):
        ok = gc.validar("comparar", {"base": BASE, "humano": 5, "bot": 6}, CFG)
        self.assertEqual(ok, {"base": BASE, "humano": 5, "bot": 6, "otro_tipo": False})
        self.assertTrue(gc.validar("comparar", {"base": BASE, "humano": 5, "bot": 6, "otro_tipo": True}, CFG)["otro_tipo"])
        for malo in ({"humano": "5"}, {"humano": 5.0}, {"humano": True}, {"humano": 0}, {"humano": -1}, {"humano": 2**31},
                     {"bot": None}, {"otro_tipo": "si"}, {"base": "GestionIntegral"}):
            invalido(self, "comparar", {"base": BASE, "humano": 5, "bot": 6, **malo})
        invalido(self, "comparar", {"base": BASE, "humano": 5}, texto="faltan")

    def test_deshacer_solo_cargas_de_copia(self):
        r = gc.validar("deshacer", {"carga_id": "copia-1700000000", "base": BASE}, CFG)
        self.assertEqual(r, {"carga_id": "copia-1700000000", "base": BASE, "confirmar": False})
        self.assertTrue(gc.validar("deshacer", {"carga_id": "copia-1", "base": BASE, "confirmar": True}, CFG)["confirmar"])
        for malo in ("12", 12, "copia-", "copia-x", "copia-1\n", "copia-1 ; ls", "copia-" + "1" * 13, "COPIA-1", "../copia-1", None):
            invalido(self, "deshacer", {"carga_id": malo, "base": BASE})
        invalido(self, "deshacer", {"carga_id": "copia-1"}, texto="faltan")
        invalido(self, "deshacer", {"carga_id": "copia-1", "base": "GestionIntegral"})
        invalido(self, "deshacer", {"carga_id": "copia-1", "base": BASE, "confirmar": "si"})

    def test_logs(self):
        self.assertEqual(gc.validar("logs", {}, CFG), {"lineas": 100})
        self.assertEqual(gc.validar("logs", {"lineas": 400}, CFG), {"lineas": 400})
        self.assertEqual(gc.validar("logs", {"lineas": 1}, CFG), {"lineas": 1})
        for malo in (0, 401, -1, "50", 5.5, True, None, 10**9):
            invalido(self, "logs", {"lineas": malo})
        invalido(self, "logs", {"archivo": "../x"}, texto="desconocidos")

    def test_actualizar(self):
        r = gc.validar("actualizar", {"version": "v1.10.0", "sha256_install": "AB" * 32}, CFG)
        self.assertEqual(r, {"version": "v1.10.0", "sha256_install": "ab" * 32})
        for v in ("main", "1.10.0", "v1.10", "v1.10.0\n", "v1.10.0-rc", "../v1.0.0", "v1.10.0 ", 5, None):
            invalido(self, "actualizar", {"version": v, "sha256_install": "a" * 64})
        for h in ("a" * 63, "a" * 65, "g" * 64, "", None, 5, "a" * 64 + "\n"):
            invalido(self, "actualizar", {"version": "v1.10.0", "sha256_install": h})
        invalido(self, "actualizar", {"version": "v1.10.0"}, texto="faltan")
        invalido(self, "actualizar", {"version": "v1.10.0", "sha256_install": "a" * 64, "repo": "otro/repo"}, texto="desconocidos")

    def test_los_nombres_de_clave_raros_no_se_repiten_enteros(self):
        with self.assertRaises(gc.ErrorComando) as c:
            gc.validar("version", {"x" * 500: 1}, CFG)
        self.assertLess(len(str(c.exception)), 100)

    def test_la_lista_cerrada_es_la_del_diseno_y_coincide_con_la_migracion(self):
        esperados = {"version", "estructura", "perfil", "copia_medir", "copia_crear", "copia_borrar", "escritor_archivo",
                     "comparar", "deshacer", "logs", "actualizar"}
        self.assertEqual(set(gc.TIPOS), esperados)
        mig = os.path.join(AQUI, "..", "diforte-produccion-control-arca", "supabase", "migrations", "693_garro_comandos.sql")
        if os.path.exists(mig):  # solo en el workspace de Diforte
            texto = open(mig, encoding="utf-8").read()
            for t in esperados:
                self.assertIn(f"'{t}'", texto)


class Invocaciones(unittest.TestCase):
    def armar(self, tipo, args, archivo=None):
        a = gc.validar(tipo, args, CFG)
        return gc.armar(tipo, a, python="/py", aqui="/pk", config="/pk/.env", archivo=archivo)

    def test_listas_exactas(self):
        s = lambda n: os.path.join("/pk", n)  # noqa: E731
        c = ["--config", "/pk/.env"]
        self.assertEqual(self.armar("version", {}), [["/py", s("garro_sync.py"), "--version"], ["/py", s("garro_escritor.py"), "--version"]])
        self.assertEqual(self.armar("estructura", {}), [["/py", s("garro_escritor.py"), "--estructura", *c]])
        self.assertEqual(self.armar("perfil", {}), [["/py", s("comparar_movimientos.py"), "--perfil", *c]])
        self.assertEqual(self.armar("copia_medir", {}), [["/py", s("garro_copia.py"), "--medir", *c]])
        self.assertEqual(self.armar("copia_crear", {"base": BASE}), [["/py", s("garro_copia.py"), "--crear", BASE, *c]])
        self.assertEqual(self.armar("copia_borrar", {"base": BASE}), [["/py", s("garro_copia.py"), "--borrar", BASE, *c]])
        self.assertEqual(self.armar("comparar", {"base": BASE, "humano": 3, "bot": 4, "otro_tipo": True}),
                         [["/py", s("comparar_movimientos.py"), "--base", BASE, "--humano", "3", "--bot", "4", "--otro-tipo", *c]])
        self.assertEqual(self.armar("deshacer", {"base": BASE, "carga_id": "copia-9", "confirmar": True}),
                         [["/py", s("garro_escritor.py"), "--deshacer", "copia-9", "--base", BASE, "--confirmar", *c]])
        self.assertEqual(self.armar("deshacer", {"base": BASE, "carga_id": "copia-9"}),
                         [["/py", s("garro_escritor.py"), "--deshacer", "copia-9", "--base", BASE, *c]])

    def test_escritor_archivo_sin_escribir_es_la_simulacion_de_siempre(self):
        [argv] = self.armar("escritor_archivo", {"payload": {}}, archivo="/pk/c.json")
        self.assertEqual(argv, ["/py", os.path.join("/pk", "garro_escritor.py"), "--archivo", "/pk/c.json", "--config", "/pk/.env"])
        self.assertNotIn("--escribir", argv)
        self.assertNotIn("--base", argv)

    def test_escribir_siempre_lleva_la_base_de_la_copia(self):
        [argv] = self.armar("escritor_archivo", {"payload": {}, "escribir": True, "base": BASE}, archivo="/pk/c.json")
        self.assertIn("--escribir", argv)
        self.assertEqual(argv[argv.index("--base") + 1], BASE)
        self.assertNotEqual(BASE, CFG["GARRO_BASE"])

    def test_todo_es_una_lista_de_textos_y_ningun_valor_se_parte_ni_se_interpreta(self):
        casos = [("copia_crear", {"base": "gestion_prueba_a_prueba"}), ("logs", {"lineas": 5}),
                 ("deshacer", {"base": BASE, "carga_id": "copia-1"}), ("comparar", {"base": BASE, "humano": 1, "bot": 2})]
        for tipo, args in casos:
            for argv in self.armar(tipo, args):
                self.assertIsInstance(argv, list)
                self.assertTrue(all(isinstance(x, str) for x in argv))
                self.assertFalse(any(c in x for x in argv[2:] for c in (";", "|", "&", "`", "$", " ")), argv)

    def test_logs_y_actualizar_no_corren_un_subproceso_por_aqui(self):
        self.assertEqual(self.armar("logs", {}), [])
        self.assertEqual(self.armar("actualizar", {"version": "v1.10.0", "sha256_install": "a" * 64}), [])

    def test_escritor_archivo_sin_archivo_es_un_error(self):
        with self.assertRaises(gc.ErrorComando):
            self.armar("escritor_archivo", {"payload": {}})

    def test_ningun_tipo_arma_una_escritura_a_la_base_real(self):
        # Para toda combinación válida de escritor_archivo / deshacer / copia_*, la base que viaja no es la real.
        for tipo, args in [("escritor_archivo", {"payload": {}, "escribir": True, "base": BASE}),
                           ("deshacer", {"carga_id": "copia-1", "base": BASE, "confirmar": True}),
                           ("copia_crear", {"base": BASE}), ("copia_borrar", {"base": BASE})]:
            [argv] = self.armar(tipo, args, archivo="/pk/c.json")
            self.assertNotIn(CFG["GARRO_BASE"], argv)
            self.assertIn(BASE, argv)


class Secretos(unittest.TestCase):
    def test_los_valores_secretos_de_la_configuracion(self):
        s = gc.secretos_de(CFG)
        for esperado in ("clave-garro-9", "p@ss/w:rd#sb", "cadenaSecreta7", CFG["OTRA_URL"]):
            self.assertIn(esperado, s)
        self.assertIn("p%40ss%2Fw%3Ard%23sb", s)  # variante codificada para URL
        for no_secreto in ("garro.local", "GestionIntegral", "garro_sync.x", "1"):
            self.assertNotIn(no_secreto, s)
        self.assertEqual(s, sorted(s, key=len, reverse=True))

    def test_redactar(self):
        t = gc.redactar("pw=clave-garro-9 y postgresql://u:cadenaSecreta7@h/x y p%40ss%2Fw%3Ard%23sb", gc.secretos_de(CFG))
        for s in ("clave-garro-9", "cadenaSecreta7", "p%40ss"):
            self.assertNotIn(s, t)
        self.assertIn("***", t)

    def test_no_se_redactan_valores_de_menos_de_4_caracteres_las_contrasenas_deben_tener_4_o_mas(self):
        self.assertEqual(gc.MIN_SECRETO, 4)
        for corto in ("a", "pa", "pas"):
            self.assertEqual(gc.secretos_de({"X_PASSWORD": corto}), [])
            self.assertEqual(gc.redactar(f"x {corto} y", gc.secretos_de({"X_PASSWORD": corto})), f"x {corto} y")
        self.assertEqual(gc.redactar("pass", gc.secretos_de({"X_PASSWORD": "pass"})), "***")
        self.assertEqual(gc.secretos_de({"GARRO_PUERTO": "5432", "GARRO_BASE": "g", "CARGAS": "1"}), [])
        sec = gc.secretos_de({"OTRA": "postgresql://u:b@h/x"})
        self.assertIn("postgresql://u:b@h/x", sec)  # la cadena entera sí; la clave de 1 letra no
        self.assertNotIn("b", sec)

    def test_variantes_repr_porcentaje_en_minuscula_y_contrasena_de_url_decodificada_o_no(self):
        sec = gc.secretos_de({"X_PASSWORD": "a'b\"c/d\\e"})
        for aparece in ("a'b\"c/d\\e",                      # tal cual
                        repr("a'b\"c/d\\e")[1:-1],           # como lo escribe repr() (comillas y barra escapadas)
                        "a%27b%22c%2Fd%5Ce", "a%27b%22c%2fd%5ce",   # %-encoding en mayúscula y en minúscula
                        "a\\'b\"c/d\\\\e", "a'b\\\"c/d\\\\e"):
            self.assertEqual(gc.redactar(f"x {aparece} y", sec), "x *** y", aparece)
        dsn = {"CONEXION": "postgresql://u:p%40ss%2fw@h:5432/x"}
        sec = gc.secretos_de(dsn)
        self.assertEqual(gc.redactar("p%40ss%2fw", sec), "***")   # sin decodificar
        self.assertEqual(gc.redactar("p@ss/w", sec), "***")        # decodificada
        self.assertEqual(gc.redactar("p%40ss%2Fw", sec), "***")    # y en mayúscula

    def test_la_salida_con_repr_de_un_dict_tampoco_lleva_el_secreto(self):
        cfg = {"SUPABASE_PASSWORD": "it's \"x\""}
        estado, r = gc.correr({"tipo": "estructura", "args": {}}, cfg, config="x",
                              ejecutar=lambda a, t: (1, repr({"password": "it's \"x\""})))
        self.assertNotIn("it", r["salida"].replace("***", ""))
        self.assertNotIn("x\\\"", r["salida"])

    def lanzar(self, salida=None, excepcion=None, cfg=CFG):
        def ejecutar(argv, t):
            if excepcion:
                raise excepcion
            return 1, salida
        return gc.correr({"id": 1, "tipo": "estructura", "args": {}}, cfg, config="x", ejecutar=ejecutar)

    def test_la_salida_que_sube_nunca_lleva_secretos(self):
        sucia = ("ERROR: password authentication failed 'clave-garro-9'\n"
                 "dsn=postgresql://garro_sync:cadenaSecreta7@host/postgres\nsb=p@ss/w:rd#sb enc=p%40ss%2Fw%3Ard%23sb\n")
        estado, r = self.lanzar(sucia)
        texto = json.dumps(r, ensure_ascii=False)
        for s in ("clave-garro-9", "cadenaSecreta7", "p@ss/w:rd#sb", "p%40ss%2Fw%3Ard%23sb"):
            self.assertNotIn(s, texto)
        self.assertIn("***", r["salida"])
        self.assertEqual(estado, "error")

    def test_tampoco_en_el_mensaje_de_una_excepcion_propia(self):
        estado, r = self.lanzar(excepcion=RuntimeError("no conecta con clave-garro-9"))
        self.assertEqual(estado, "error")
        self.assertNotIn("clave-garro-9", json.dumps(r))
        self.assertIn("RuntimeError", r["salida"])

    def test_se_redacta_antes_de_recortar(self):
        # un secreto que quedaría cortado a la mitad por el recorte no puede asomar
        relleno = "x" * (gc.MAX_SALIDA - 5)
        estado, r = self.lanzar(relleno + "clave-garro-9" + "y" * 30000)
        self.assertNotIn("clave-garro", r["salida"])
        self.assertNotIn("garro-9", r["salida"])

    def test_la_salida_de_logs_tambien_se_redacta(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "garro_sync.log"), "w") as f:
                f.write("linea con clave-garro-9\nsegunda\n")
            estado, r = gc.correr({"id": 1, "tipo": "logs", "args": {"lineas": 5}}, CFG, config="x", aqui=d)
        self.assertEqual(estado, "ok")
        self.assertNotIn("clave-garro-9", r["salida"])
        self.assertIn("segunda", r["salida"])


class Recorte(unittest.TestCase):
    def test_corto_no_se_toca(self):
        self.assertEqual(gc.recortar("hola"), "hola")
        self.assertEqual(gc.recortar("x" * gc.MAX_SALIDA), "x" * gc.MAX_SALIDA)

    def test_largo_queda_en_el_tope_con_principio_y_final(self):
        t = "A" * 15000 + "B" * 15000
        r = gc.recortar(t)
        self.assertLessEqual(len(r), gc.MAX_SALIDA)
        self.assertTrue(r.startswith("A") and r.endswith("B"))
        self.assertIn("recortado", r)

    def test_la_salida_de_un_resultado_nunca_pasa_de_20000(self):
        _, r = gc.correr({"id": 1, "tipo": "version", "args": {}}, CFG, config="x", ejecutar=lambda a, t: (0, "z" * 500000))
        self.assertLessEqual(len(r["salida"]), 20000)


class Resultado(unittest.TestCase):
    def correr(self, cmd, ejecutar, **kw):
        return gc.correr(cmd, CFG, config="x", ejecutar=ejecutar, **kw)

    def setUp(self):
        p = mock.patch.object(ge, "exigir_copia_marcada")
        self.exigir = p.start()
        self.addCleanup(p.stop)

    def test_forma_del_resultado_y_estado(self):
        estado, r = self.correr({"tipo": "estructura", "args": {}}, lambda a, t: (0, "todo bien"))
        self.assertEqual(estado, "ok")
        self.assertEqual(set(r), {"codigo_salida", "salida", "segundos", "version", "equipo"})
        self.assertEqual(r["equipo"], socket.gethostname())
        self.assertEqual((r["codigo_salida"], r["salida"], r["version"]), (0, "todo bien", g.VERSION))
        self.assertIsInstance(r["segundos"], float)
        estado, r = self.correr({"tipo": "estructura", "args": {}}, lambda a, t: (3, "mal"))
        self.assertEqual((estado, r["codigo_salida"]), ("error", 3))

    def test_comando_invalido_resuelve_error_y_no_corre_nada(self):
        llamadas = []
        for cmd in ({"tipo": "rm", "args": {}}, {"tipo": "copia_crear", "args": {"base": "GestionIntegral"}},
                    {"tipo": "version", "args": {"x": 1}}, "texto", None, {"args": {}}, {"tipo": "logs", "args": {"lineas": 9999}}):
            estado, r = self.correr(cmd, lambda a, t: llamadas.append(a) or (0, ""))
            self.assertEqual(estado, "error", cmd)
            self.assertIn("rechazado", r["salida"])
        self.assertEqual(llamadas, [])

    def test_version_corre_las_dos_y_el_primer_fallo_manda(self):
        vistos = []
        def ejecutar(argv, t):
            vistos.append(os.path.basename(argv[1]))
            return (2, "no anda") if "garro_sync.py" in argv[1] else (0, "garro_escritor v1")
        estado, r = self.correr({"tipo": "version", "args": {}}, ejecutar)
        self.assertEqual(vistos, ["garro_sync.py", "garro_escritor.py"])
        self.assertEqual((estado, r["codigo_salida"]), ("error", 2))
        self.assertIn("no anda", r["salida"])
        self.assertIn("garro_escritor v1", r["salida"])

    def test_el_tiempo_se_corta_al_presupuesto_que_queda(self):
        tiempos = []
        self.correr({"tipo": "copia_crear", "args": {"base": BASE}}, lambda a, t: tiempos.append(t) or (0, ""))
        self.correr({"tipo": "comparar", "args": {"base": BASE, "humano": 1, "bot": 2}}, lambda a, t: tiempos.append(t) or (0, ""), presupuesto_s=60.7)
        self.correr({"tipo": "version", "args": {}}, lambda a, t: tiempos.append(t) or (0, ""), presupuesto_s=5000)
        self.assertEqual(tiempos[0], gc.TIMEOUTS["copia_crear"])
        self.assertEqual(tiempos[1], 60)
        self.assertLess(gc.TIMEOUTS["copia_crear"], 30 * 60)  # la tarea programada se corta a los 30 minutos
        self.assertTrue(all(t <= gc.PRESUPUESTO_TAREA_S for t in gc.TIMEOUTS.values()))

    def test_copia_crear_no_corre_si_no_queda_tiempo_para_hacerla_entera(self):
        llamadas = []
        estado, r = self.correr({"tipo": "copia_crear", "args": {"base": BASE}}, lambda a, t: llamadas.append(a) or (0, ""),
                                presupuesto_s=gc.TIMEOUTS["copia_crear"] - 1)
        self.assertEqual((estado, llamadas), ("error", []))
        self.assertIn("Pedila de nuevo", r["salida"])
        estado, _ = self.correr({"tipo": "copia_crear", "args": {"base": BASE}}, lambda a, t: (0, ""), presupuesto_s=gc.TIMEOUTS["copia_crear"])
        self.assertEqual(estado, "ok")

    def test_version_reparte_un_solo_tope_entre_sus_dos_invocaciones(self):
        tiempos = []
        def lento(argv, t):
            tiempos.append(t)
            time.sleep(1.2)
            return 0, ""
        self.correr({"tipo": "version", "args": {}}, lento)
        self.assertEqual(len(tiempos), 2)
        self.assertLessEqual(tiempos[0], gc.TIMEOUTS["version"])
        self.assertLessEqual(tiempos[1], tiempos[0] - 1)  # lo que ya gastó la primera no se le vuelve a dar a la segunda

    def test_una_copia_que_se_corta_por_tiempo_avisa_que_puede_haber_quedado_a_medias(self):
        estado, r = self.correr({"tipo": "copia_crear", "args": {"base": BASE}}, lambda a, t: (-1, "[Se cortó]"))
        self.assertEqual(estado, "error")
        self.assertIn("copia_borrar", r["salida"])

    def test_una_excepcion_inesperada_es_un_error_del_comando_no_un_crash(self):
        def boom(a, t):
            raise OSError("disco")
        estado, r = self.correr({"tipo": "perfil", "args": {}}, boom)
        self.assertEqual(estado, "error")
        self.assertIn("OSError", r["salida"])

    def test_el_archivo_del_payload_es_utf8_sin_bom_se_usa_y_se_borra(self):
        visto = {}
        with tempfile.TemporaryDirectory() as d:
            def ejecutar(argv, t):
                ruta = argv[argv.index("--archivo") + 1]
                visto["ruta"] = ruta
                visto["bytes"] = open(ruta, "rb").read()
                visto["dentro"] = os.path.dirname(ruta) == d
                return 0, "{}"
            payload = {"propuesta": {"descripcion": "COM E IND PER 09/2026 ñ"}, "total_pesos": 1}
            estado, r = gc.correr({"tipo": "escritor_archivo", "args": {"payload": payload}}, CFG, config="x", aqui=d, ejecutar=ejecutar)
            self.assertEqual(estado, "ok")
            self.assertFalse(visto["bytes"].startswith(b"\xef\xbb\xbf"))
            self.assertEqual(json.loads(visto["bytes"].decode("utf-8")), payload)
            self.assertTrue(visto["dentro"])
            self.assertFalse(os.path.exists(visto["ruta"]))
            self.assertEqual(os.listdir(d), [])

    def test_el_archivo_del_payload_se_borra_aunque_el_comando_reviente_o_se_corte(self):
        for efecto in (OSError("x"), None):
            with tempfile.TemporaryDirectory() as d:
                def ejecutar(argv, t):
                    if efecto:
                        raise efecto
                    return -1, "[Se cortó]"
                estado, _ = gc.correr({"tipo": "escritor_archivo", "args": {"payload": {}}}, CFG, config="x", aqui=d, ejecutar=ejecutar)
                self.assertEqual(estado, "error")
                self.assertEqual(os.listdir(d), [])

    def test_escribir_en_la_base_real_ni_siquiera_arma_el_archivo(self):
        with tempfile.TemporaryDirectory() as d:
            estado, r = gc.correr({"tipo": "escritor_archivo", "args": {"payload": {}, "escribir": True}}, CFG, config="x", aqui=d,
                                  ejecutar=lambda a, t: self.fail("no debía correr"))
            self.assertEqual(estado, "error")
            self.assertEqual(os.listdir(d), [])

    def test_actualizar_pasa_por_garro_actualizar_y_sus_errores_son_errores_del_comando(self):
        cmd = {"tipo": "actualizar", "args": {"version": "v1.10.0", "sha256_install": "a" * 64}}
        import garro_actualizar as ga
        with mock.patch.object(ga, "actualizar", side_effect=ga.ErrorActualizar("requiere instalar en el equipo: x")):
            estado, r = self.correr(cmd, lambda a, t: (0, ""))
        self.assertEqual((estado, r["codigo_salida"]), ("error", 1))
        self.assertIn("requiere instalar", r["salida"])
        with mock.patch.object(ga, "actualizar", return_value="Actualizado") as m:
            estado, r = self.correr({**cmd, "id": 9}, lambda a, t: (0, ""), presupuesto_s=900)
        self.assertEqual((estado, r["salida"]), ("ok", "Actualizado"))
        self.assertEqual(r["version_nueva"], "v1.10.0")
        self.assertEqual(m.call_args.args[0], {"version": "v1.10.0", "sha256_install": "a" * 64})
        self.assertEqual((m.call_args.kwargs["plazo_s"], m.call_args.kwargs["comando_id"]), (900, 9))
        with mock.patch.object(ga, "actualizar", side_effect=ga.ErrorActualizar("x")):
            _, r = self.correr(cmd, lambda a, t: (0, ""))
        self.assertNotIn("version_nueva", r)

    def test_equipo_solo_lo_corre_esa_pc(self):
        llamadas = []
        ejecutar = lambda a, t: llamadas.append(a) or (0, "")  # noqa: E731
        estado, r = self.correr({"tipo": "perfil", "args": {"equipo": "OTRA-PC"}}, ejecutar)
        self.assertEqual((estado, llamadas), ("error", []))
        self.assertIn("OTRA-PC", r["salida"])
        mio = socket.gethostname()
        for nombre in (mio, mio.upper(), mio.lower()):
            estado, _ = self.correr({"tipo": "perfil", "args": {"equipo": nombre}}, ejecutar)
            self.assertEqual(estado, "ok", nombre)
        for malo in ("", "pc con espacio", 5, "a;b", "x" * 61):
            invalido(self, "perfil", {"equipo": malo})
        self.assertEqual(gc.validar("logs", {"equipo": "PC-1.x", "lineas": 3}, CFG), {"lineas": 3, "equipo": "PC-1.x"})

    def test_escribir_o_deshacer_exigen_que_la_base_tenga_la_marca_de_copia(self):
        self.exigir.side_effect = g.ErrorSync("La base gestion_prueba no es una copia de prueba de este programa (no tiene su marca)")
        casos = [("escritor_archivo", {"payload": {}, "escribir": True, "base": BASE}), ("escritor_archivo", {"payload": {}, "base": BASE}),
                 ("deshacer", {"carga_id": "copia-1", "base": BASE, "confirmar": True}),
                 ("comparar", {"base": BASE, "humano": 1, "bot": 2})]
        for tipo, args in casos:
            llamadas = []
            estado, r = self.correr({"tipo": tipo, "args": args}, lambda a, t: llamadas.append(a) or (0, ""))
            self.assertEqual((estado, llamadas), ("error", []), tipo)
            self.assertIn("no tiene su marca", r["salida"])
        self.assertEqual(self.exigir.call_args.args[1], BASE)
        # sin base (simulación contra la real) no hay nada que comprobar; copia_crear y copia_borrar tienen su propio control
        self.exigir.reset_mock()
        self.correr({"tipo": "escritor_archivo", "args": {"payload": {}}}, lambda a, t: (0, ""))
        self.correr({"tipo": "copia_borrar", "args": {"base": BASE}}, lambda a, t: (0, ""))
        self.correr({"tipo": "copia_crear", "args": {"base": BASE}}, lambda a, t: (0, ""))
        self.exigir.assert_not_called()

    def test_copias_cortadas_se_limpian_al_empezar_medir_y_crear_y_tras_un_corte(self):
        with tempfile.TemporaryDirectory() as d:
            def huerfano(n):
                open(os.path.join(d, n), "w").close()
            for tipo, args in (("copia_medir", {}), ("copia_crear", {"base": BASE})):
                huerfano("copia-abc.dump")
                estado, r = gc.correr({"tipo": tipo, "args": args}, CFG, config="x", aqui=d, ejecutar=lambda a, t: (0, "ok"))
                self.assertEqual(os.listdir(d), [], tipo)
                self.assertIn("copia-*.dump", r["salida"])
            huerfano("copia-1.dump")
            estado, r = gc.correr({"tipo": "estructura", "args": {}}, CFG, config="x", aqui=d, ejecutar=lambda a, t: (0, "ok"))
            self.assertEqual(os.listdir(d), ["copia-1.dump"], "otros comandos no tocan los temporales")
            os.remove(os.path.join(d, "copia-1.dump"))

            def corta(argv, t):
                huerfano("copia-2.dump")  # lo que dejó pg_dump antes de que lo matáramos
                return -1, "[Se cortó]"
            estado, r = gc.correr({"tipo": "copia_crear", "args": {"base": BASE}}, CFG, config="x", aqui=d, ejecutar=corta)
            self.assertEqual((estado, os.listdir(d)), ("error", []))
            self.assertIn("Se borraron 1", r["salida"])
            self.assertIn("copia_borrar", r["salida"])


class Subprocesos(unittest.TestCase):
    def test_sin_shell_los_argumentos_llegan_literales(self):
        raro = "a; echo hacked $(whoami) | `id` && x"
        codigo, salida = gc.correr_proceso([sys.executable, "-c", "import sys; print(sys.argv[1])", raro], 20)
        self.assertEqual(codigo, 0)
        self.assertEqual(salida.strip(), raro)

    def test_stdin_cerrado(self):
        codigo, salida = gc.correr_proceso([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], 20)
        self.assertEqual((codigo, salida.strip()), (0, "''"))

    def test_junta_stdout_y_stderr_y_devuelve_el_codigo(self):
        codigo, salida = gc.correr_proceso([sys.executable, "-c", "import sys; print('uno'); print('dos', file=sys.stderr); sys.exit(4)"], 20)
        self.assertEqual(codigo, 4)
        self.assertIn("uno", salida)
        self.assertIn("dos", salida)

    def test_utf8_en_la_salida(self):
        codigo, salida = gc.correr_proceso([sys.executable, "-c", "print('añ€')"], 20)
        self.assertEqual(salida.strip(), "añ€")

    @unittest.skipIf(sys.platform == "win32", "el árbol de procesos se mata con taskkill en Windows")
    def test_el_tiempo_se_corta_y_mata_tambien_a_lo_que_lanzo(self):
        with tempfile.TemporaryDirectory() as d:
            pidfile = os.path.join(d, "nieto.pid")
            codigo_hijo = ("import subprocess, sys, time\n"
                           f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                           f"open({pidfile!r}, 'w').write(str(p.pid))\n"
                           "time.sleep(60)\n")
            t0 = time.monotonic()
            codigo, salida = gc.correr_proceso([sys.executable, "-c", codigo_hijo], 2)
            self.assertLess(time.monotonic() - t0, 20)
            self.assertEqual(codigo, -1)
            self.assertIn("Se cortó", salida)
            pid = int(open(pidfile).read())
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.1)
            else:
                self.fail("el proceso nieto sigue vivo")

    def test_pythonw_se_cambia_por_python_para_capturar_la_salida(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("pythonw.exe", "python.exe"):
                open(os.path.join(d, n), "w").close()
            with mock.patch.object(sys, "executable", os.path.join(d, "pythonw.exe")):
                self.assertEqual(gc.python_del_paquete(), os.path.join(d, "python.exe"))
            os.remove(os.path.join(d, "python.exe"))
            with mock.patch.object(sys, "executable", os.path.join(d, "pythonw.exe")):
                self.assertEqual(gc.python_del_paquete(), os.path.join(d, "pythonw.exe"))

    def test_version_de_verdad_con_los_scripts_del_paquete(self):
        estado, r = gc.correr({"id": 1, "tipo": "version", "args": {}}, CFG, config="x", aqui=AQUI)
        self.assertEqual(estado, "ok", r)
        self.assertIn(f"garro_sync {g.VERSION}", r["salida"])
        self.assertIn(f"garro_escritor {g.VERSION}", r["salida"])
        self.assertEqual(r["codigo_salida"], 0)

    def test_los_tres_programas_dicen_la_version(self):
        for script, esperado in (("garro_sync.py", "garro_sync"), ("garro_escritor.py", "garro_escritor"),
                                 ("garro_comandos.py", "garro_comandos")):
            codigo, salida = gc.correr_proceso([sys.executable, os.path.join(AQUI, script), "--version"], 60)
            self.assertEqual((codigo, salida.strip()), (0, f"{esperado} {g.VERSION}"))

    def test_la_version_coincide_con_el_instalador(self):
        t = open(os.path.join(AQUI, "install.ps1"), encoding="ascii").read()
        import re
        self.assertEqual(re.search(r'\$Version\s*=\s*"([^"]+)"', t).group(1), g.VERSION)


class Logs(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def escribir(self, nombre, lineas):
        with open(os.path.join(self.d, nombre), "w", encoding="utf-8") as f:
            f.write("\n".join(lineas) + "\n")

    def test_ultimas_n_lineas(self):
        self.escribir("garro_sync.log", [f"l{i}" for i in range(1, 11)])
        self.assertEqual(gc.leer_logs(self.d, 3), "l8\nl9\nl10")

    def test_completa_con_el_log_rotado(self):
        self.escribir("garro_sync.log", ["n1", "n2"])
        self.escribir("garro_sync.log.1", ["v1", "v2", "v3"])
        self.assertEqual(gc.leer_logs(self.d, 4), "v2\nv3\nn1\nn2")

    def test_sin_log(self):
        self.assertIn("no hay", gc.leer_logs(self.d, 5))

    @unittest.skipIf(sys.platform == "win32", "symlinks")
    def test_un_enlace_que_sale_de_la_carpeta_no_se_lee(self):
        with tempfile.TemporaryDirectory() as fuera:
            secreto = os.path.join(fuera, "secreto.txt")
            open(secreto, "w").write("contenido ajeno")
            os.symlink(secreto, os.path.join(self.d, "garro_sync.log"))
            self.assertNotIn("ajeno", gc.leer_logs(self.d, 5))

    def test_acentos_y_bytes_raros(self):
        with open(os.path.join(self.d, "garro_sync.log"), "wb") as f:
            f.write("ñandú\n".encode("utf-8") + b"\xff\xfe roto\n")
        out = gc.leer_logs(self.d, 5)
        self.assertIn("ñandú", out)
        self.assertIn("roto", out)

    def test_archivo_enorme_solo_lee_el_final(self):
        with open(os.path.join(self.d, "garro_sync.log"), "w") as f:
            f.write("viejo\n" * 1_500_000)
            f.write("ultima\n")
        self.assertEqual(gc.leer_logs(self.d, 1), "ultima")


def fake_locks(d):
    """Toma el candado de un archivo de la carpeta, como lo haría otra corrida."""
    return ge.tomar_lock(os.path.join(d, "comandos.lock")), ge.tomar_lock(os.path.join(d, "cargas.lock"))


class Pasada(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.log = []
        for p in (mock.patch.object(g, "decir", side_effect=self.log.append),
                  mock.patch.object(g, "escribir_traza"), mock.patch.object(ge, "exigir_copia_marcada")):
            p.start()
            self.addCleanup(p.stop)

    def pasada(self, **kw):
        return gc.pasada(dict(CFG), "x", aqui=self.d, **kw)

    def test_toma_un_comando_lo_corre_y_lo_resuelve(self):
        cmd = {"id": 7, "tipo": "estructura", "args": {}}
        with mock.patch.object(gc, "tomar", return_value=cmd) as t, \
                mock.patch.object(gc, "correr_proceso", return_value=(0, "OK")), \
                mock.patch.object(gc, "resolver", return_value=True) as r:
            self.pasada()
        t.assert_called_once()
        args = r.call_args.args
        self.assertEqual((args[1], args[2]), (7, "ok"))
        self.assertEqual(args[3]["salida"], "OK")
        self.assertTrue(any("(comandos) 7 estructura: ok" in l for l in self.log))

    def test_sin_comandos_no_hace_nada(self):
        with mock.patch.object(gc, "tomar", return_value=None), mock.patch.object(gc, "resolver") as r:
            self.pasada()
        r.assert_not_called()
        self.assertEqual(self.log, [])

    def test_un_comando_que_falla_resuelve_error_con_la_salida(self):
        cmd = {"id": 8, "tipo": "estructura", "args": {}}
        with mock.patch.object(gc, "tomar", return_value=cmd), \
                mock.patch.object(gc, "correr_proceso", return_value=(1, "ERROR: la base no responde")), \
                mock.patch.object(gc, "resolver", return_value=True) as r:
            self.pasada()
        self.assertEqual(r.call_args.args[2], "error")
        self.assertIn("la base no responde", r.call_args.args[3]["salida"])

    def test_un_comando_invalido_que_llego_a_la_cola_resuelve_error_sin_correr_nada(self):
        cmd = {"id": 9, "tipo": "copia_borrar", "args": {"base": "GestionIntegral"}}
        with mock.patch.object(gc, "tomar", return_value=cmd), \
                mock.patch.object(gc, "correr_proceso", side_effect=AssertionError("no debía correr")), \
                mock.patch.object(gc, "resolver", return_value=True) as r:
            self.pasada()
        self.assertEqual(r.call_args.args[2], "error")
        self.assertIn("rechazado", r.call_args.args[3]["salida"])

    def test_sin_las_funciones_de_la_cola_se_ignora_con_un_aviso_por_hora(self):
        with mock.patch.object(gc, "tomar", side_effect=gc.ComandosSinFunciones()):
            self.pasada()
            self.pasada()
            self.pasada()
        avisos = [l for l in self.log if "migración 693" in l]
        self.assertEqual(len(avisos), 1)
        # pasada la hora, avisa de nuevo
        marca = os.path.join(self.d, "comandos.aviso")
        viejo = time.time() - 2 * 3600
        os.utime(marca, (viejo, viejo))
        with mock.patch.object(gc, "tomar", side_effect=gc.ComandosSinFunciones()):
            self.pasada()
        self.assertEqual(len([l for l in self.log if "migración 693" in l]), 2)

    def test_tomar_con_la_funcion_inexistente_en_la_base_levanta_sin_funciones(self):
        for pgcode in ("42883", "3F000"):
            e = Exception("no existe")
            e.pgcode = pgcode
            con = mock.MagicMock()
            con.cursor.return_value.__enter__.return_value.execute.side_effect = e
            with mock.patch.object(g, "conectar_destino", return_value=con):
                with self.assertRaises(gc.ComandosSinFunciones):
                    gc.tomar(dict(CFG))
            con.close.assert_called_once()

    def test_otro_error_de_la_base_no_se_confunde_con_falta_de_funciones(self):
        e = Exception("permiso")
        e.pgcode = "42501"
        con = mock.MagicMock()
        con.cursor.return_value.__enter__.return_value.execute.side_effect = e
        with mock.patch.object(g, "conectar_destino", return_value=con):
            with self.assertRaises(Exception) as c:
                gc.tomar(dict(CFG))
        self.assertNotIsInstance(c.exception, gc.ComandosSinFunciones)

    def test_tomar_y_resolver_hablan_con_las_funciones_de_la_migracion(self):
        con = mock.MagicMock()
        cur = con.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ({"id": 3, "tipo": "version", "args": {}},)
        with mock.patch.object(g, "conectar_destino", return_value=con):
            self.assertEqual(gc.tomar(dict(CFG))["id"], 3)
            sql, params = cur.execute.call_args.args
            self.assertEqual((sql, params), ("SELECT garro.comandos_tomar(%s, %s)", (g.VERSION, socket.gethostname())))
            cur.fetchone.return_value = (True,)
            self.assertTrue(gc.resolver(dict(CFG), 3, "ok", {"salida": "ñ"}))
            sql, params = cur.execute.call_args.args
            self.assertIn("garro.comandos_resolver", sql)
            self.assertEqual(params[:2], (3, "ok"))
            self.assertEqual(json.loads(params[2]), {"salida": "ñ"})
            cur.fetchone.return_value = (False,)
            self.assertFalse(gc.resolver(dict(CFG), 3, "ok", {}))

    def test_candado_de_comandos_tomado_no_toma_nada(self):
        otro = ge.tomar_lock(os.path.join(self.d, "comandos.lock"))
        try:
            with mock.patch.object(gc, "tomar") as t:
                self.pasada()
            t.assert_not_called()
        finally:
            ge.soltar_lock(otro)

    def test_candado_de_las_cargas_tomado_no_toma_nada_y_libera_el_de_comandos(self):
        otro = ge.tomar_lock(os.path.join(self.d, "cargas.lock"))
        try:
            with mock.patch.object(gc, "tomar") as t:
                self.pasada()
            t.assert_not_called()
        finally:
            ge.soltar_lock(otro)
        libre = ge.tomar_lock(os.path.join(self.d, "comandos.lock"))
        self.assertIsNotNone(libre)
        ge.soltar_lock(libre)

    def test_los_candados_se_sueltan_siempre_aunque_falle(self):
        with mock.patch.object(gc, "tomar", side_effect=RuntimeError("red")):
            with self.assertRaises(RuntimeError):
                self.pasada()
        for n in ("comandos.lock", "cargas.lock"):
            f = ge.tomar_lock(os.path.join(self.d, n))
            self.assertIsNotNone(f, n)
            ge.soltar_lock(f)

    def test_dos_corridas_a_la_vez_no_corren_dos_comandos(self):
        # mientras la primera corre su comando (candado tomado), la segunda no toma nada
        cmd = {"id": 1, "tipo": "copia_medir", "args": {}}
        segunda = []
        def ejecutar_largo(argv, t, cwd=None, env=None):
            with mock.patch.object(gc, "tomar", side_effect=lambda cfg: segunda.append(1)) :
                self.pasada()
            return 0, "midió"
        with mock.patch.object(gc, "tomar", return_value=cmd), \
                mock.patch.object(gc, "correr_proceso", side_effect=ejecutar_largo), \
                mock.patch.object(gc, "resolver", return_value=True) as r:
            self.pasada()
        self.assertEqual(segunda, [])
        self.assertEqual(r.call_args.args[2], "ok")

    def test_sin_presupuesto_de_la_tarea_no_toma_nada(self):
        with mock.patch.object(gc, "tomar") as t:
            self.pasada(inicio=time.monotonic() - gc.PRESUPUESTO_TAREA_S + 10)
        t.assert_not_called()
        with mock.patch.object(gc, "tomar", return_value=None) as t:
            self.pasada(inicio=time.monotonic() - 60)
        t.assert_called_once()

    def test_el_presupuesto_que_queda_acota_el_tiempo_del_comando(self):
        cmd = {"id": 1, "tipo": "perfil", "args": {}}
        tiempos = []
        with mock.patch.object(gc, "tomar", return_value=cmd), \
                mock.patch.object(gc, "correr_proceso", side_effect=lambda argv, t, cwd=None, env=None: tiempos.append(t) or (0, "")), \
                mock.patch.object(gc, "resolver", return_value=True):
            self.pasada(inicio=time.monotonic() - (gc.PRESUPUESTO_TAREA_S - 150))
        self.assertLessEqual(tiempos[0], 150)
        self.assertGreater(tiempos[0], 140)

    def test_una_copia_que_no_entra_en_lo_que_queda_se_resuelve_error_sin_correr(self):
        cmd = {"id": 1, "tipo": "copia_crear", "args": {"base": BASE}}
        with mock.patch.object(gc, "tomar", return_value=cmd), \
                mock.patch.object(gc, "correr_proceso", side_effect=AssertionError("no debía correr")), \
                mock.patch.object(gc, "resolver", return_value=True) as r:
            self.pasada(inicio=time.monotonic() - 20 * 60)
        self.assertEqual(r.call_args.args[2], "error")
        self.assertIn("Pedila de nuevo", r.call_args.args[3]["salida"])

    def test_borra_los_archivos_de_payload_huerfanos_pero_solo_con_el_candado_tomado(self):
        huerfano = os.path.join(self.d, "comando-xyz.json")
        open(huerfano, "w").close()
        otro = ge.tomar_lock(os.path.join(self.d, "comandos.lock"))
        try:
            with mock.patch.object(gc, "tomar", return_value=None):
                self.pasada()
            self.assertTrue(os.path.exists(huerfano))
        finally:
            ge.soltar_lock(otro)
        with mock.patch.object(gc, "tomar", return_value=None):
            self.pasada()
        self.assertFalse(os.path.exists(huerfano))

    def nota(self, **kw):
        datos = {"comando_id": 31, "anterior": "v1", "nueva": "v2", "mensaje": "La actualización a v2 se cortó a mitad: se restauró v1."}
        datos.update(kw)
        with open(os.path.join(self.d, gr.MARCA_RECUPERADA), "w") as f:
            json.dump(datos, f)

    def test_una_actualizacion_cortada_cierra_su_comando_tomado_con_un_error_que_lo_explica(self):
        self.nota()
        with mock.patch.object(gc, "tomar", return_value=None), mock.patch.object(gc, "resolver", return_value=True) as r:
            self.pasada()
        self.assertEqual(r.call_args.args[1:3], (31, "error"))
        self.assertIn("se cortó a mitad", r.call_args.args[3]["salida"])
        self.assertIsNone(gr.leer_recuperada(self.d))

    def test_si_no_se_pudo_cerrar_la_nota_se_conserva_para_reintentar(self):
        self.nota()
        with mock.patch.object(gc, "tomar", return_value=None), mock.patch.object(gc, "resolver", side_effect=OSError("red")):
            self.pasada()
        self.assertIsNotNone(gr.leer_recuperada(self.d))
        self.nota(comando_id=None)
        with mock.patch.object(gc, "tomar", return_value=None), mock.patch.object(gc, "resolver") as r:
            self.pasada()
        r.assert_not_called()
        self.assertIsNone(gr.leer_recuperada(self.d))

    def test_si_no_se_puede_resolver_se_reintenta_una_vez_y_queda_en_el_log(self):
        cmd = {"id": 5, "tipo": "estructura", "args": {}}
        with mock.patch.object(gc, "tomar", return_value=cmd), \
                mock.patch.object(gc, "correr_proceso", return_value=(0, "")), \
                mock.patch.object(gc, "resolver", side_effect=OSError("red")) as r:
            self.pasada()
        self.assertEqual(r.call_count, 2)
        self.assertTrue(any("ERROR al resolver el comando 5" in l for l in self.log))

    def test_la_cola_que_no_acepta_resolver_queda_en_el_log(self):
        cmd = {"id": 5, "tipo": "estructura", "args": {}}
        with mock.patch.object(gc, "tomar", return_value=cmd), \
                mock.patch.object(gc, "correr_proceso", return_value=(0, "")), \
                mock.patch.object(gc, "resolver", return_value=False) as r:
            self.pasada()
        self.assertEqual(r.call_count, 1)
        self.assertTrue(any("no aceptó" in l for l in self.log))


class IntegracionConLaTarea(unittest.TestCase):
    def setUp(self):
        self.msgs = []
        for p in (mock.patch.object(g, "decir", side_effect=self.msgs.append), mock.patch.object(g, "escribir_traza")):
            p.start()
            self.addCleanup(p.stop)

    def test_va_al_final_despues_de_pedido_cargas_y_relevamiento(self):
        orden = []
        with mock.patch.object(g, "_atender_pedido", side_effect=lambda c: orden.append("pedido") or 1), \
                mock.patch.object(g, "cargas_auto", side_effect=lambda c: orden.append("cargas")), \
                mock.patch.object(g, "relevamiento_auto", side_effect=lambda c: orden.append("relevamiento")), \
                mock.patch.object(g, "comandos_auto", side_effect=lambda c, inicio=None: orden.append("comandos")):
            self.assertEqual(g.correr_pedido("x"), 1)
        self.assertEqual(orden, ["pedido", "cargas", "relevamiento", "comandos"])

    def test_el_codigo_de_la_tarea_no_cambia_por_un_fallo_de_comandos(self):
        for fallo in (RuntimeError("boom"), SystemExit("x")):
            with mock.patch.object(g, "_atender_pedido", return_value=0), mock.patch.object(g, "cargas_auto"), \
                    mock.patch.object(g, "relevamiento_auto"), mock.patch.object(g, "leer_archivo", return_value=dict(CFG)), \
                    mock.patch.object(gc, "pasada", side_effect=fallo):
                self.assertEqual(g.correr_pedido("x"), 0)
        self.assertTrue(any("(comandos) ERROR" in m for m in self.msgs))

    def test_apagado_con_COMANDOS_0_ni_importa_el_modulo(self):
        with mock.patch.object(g, "leer_archivo", return_value={"COMANDOS": "0"}), \
                mock.patch.object(gc, "pasada") as p:
            g.comandos_auto("x")
        p.assert_not_called()

    def test_prendido_por_defecto_y_pasa_el_inicio(self):
        with mock.patch.object(g, "leer_archivo", return_value=dict(CFG)), mock.patch.object(gc, "pasada") as p:
            g.comandos_auto("x", 123.0)
        p.assert_called_once()
        self.assertEqual(p.call_args.kwargs["inicio"], 123.0)

    def test_un_pedido_sin_config_no_rompe(self):
        with mock.patch.object(g, "leer_archivo", side_effect=g.ErrorSync("falta")):
            g.comandos_auto("x")
        self.assertTrue(any("(comandos) ERROR" in m for m in self.msgs))


class MarcaDeCopia(unittest.TestCase):
    """H2: la copia se reconoce por su marca (comentario de la base), no por el nombre."""

    def conexion(self, comentario):
        con = mock.MagicMock()
        con.cursor.return_value.__enter__.return_value.fetchone.return_value = None if comentario is False else (comentario,)
        return con

    def test_con_la_marca_pasa_sin_ella_o_sin_la_base_se_rechaza(self):
        cfg = {**CFG}
        for comentario, bien in ((ge.MARCA_COPIA, True), (ge.MARCA_COPIA + " (creada el ...)", True), (None, False), ("otra cosa", False),
                                 ("", False), (False, False)):
            con = self.conexion(comentario)
            with mock.patch.object(ge, "conectar_garro", return_value=con):
                if bien:
                    ge.exigir_copia_marcada(cfg, BASE)
                else:
                    with self.assertRaises(g.ErrorSync) as c:
                        ge.exigir_copia_marcada(cfg, BASE)
                    self.assertIn("no es una copia de prueba", str(c.exception))
            con.close.assert_called_once()

    def test_la_base_real_se_rechaza_sin_ni_mirar(self):
        with mock.patch.object(ge, "conectar_garro") as c:
            with self.assertRaises(g.ErrorSync):
                ge.exigir_copia_marcada(CFG, "gestionintegral")
        c.assert_not_called()

    def test_la_marca_es_la_que_deja_garro_copia(self):
        import garro_copia
        self.assertEqual(garro_copia.MARCA, ge.MARCA_COPIA)

    def test_conectar_para_escribir_con_base_exige_la_marca_y_leer_o_la_real_no(self):
        import psycopg2
        con = mock.MagicMock()
        con.cursor.return_value.__enter__.return_value.fetchone.return_value = ("SQL_ASCII",)
        with mock.patch.object(ge, "exigir_copia_marcada") as exigir, mock.patch.object(psycopg2, "connect", return_value=con):
            ge.conectar_garro(CFG, solo_lectura=True, base=BASE)
            ge.conectar_garro(CFG, solo_lectura=False)
            exigir.assert_not_called()
            ge.conectar_garro(CFG, solo_lectura=False, base=BASE)
            exigir.assert_called_once_with(CFG, BASE)
        # sin marca: no se conecta para escribir
        with mock.patch.object(ge, "exigir_copia_marcada", side_effect=g.ErrorSync("sin marca")), \
                mock.patch.object(psycopg2, "connect") as conectar:
            with self.assertRaises(g.ErrorSync):
                ge.conectar_garro(CFG, solo_lectura=False, base=BASE)
        conectar.assert_not_called()

    def test_escribir_y_deshacer_confirmado_de_verdad_pasan_por_la_marca(self):
        for fuente in ("cargar_una", "deshacer"):
            texto = open(os.path.join(AQUI, "garro_escritor.py"), encoding="utf-8").read()
            self.assertIn(fuente, texto)
        texto = open(os.path.join(AQUI, "garro_escritor.py"), encoding="utf-8").read()
        self.assertIn("conectar_garro(cfg, solo_lectura=en_seco, base=base)", texto)
        self.assertIn("conectar_garro(cfg, solo_lectura=not confirmar, base=copia)", texto)


class ArranqueDeLaTarea(unittest.TestCase):
    def main(self, argv, **env):
        with mock.patch.dict(os.environ, env), mock.patch("garro_recuperar.recuperar", return_value=None) as rec, \
                mock.patch.object(g, "correr_pedido", return_value=0), mock.patch.object(g, "correr_prueba_destino", return_value=True), \
                mock.patch.object(g, "leer_archivo", side_effect=g.ErrorSync("sin config")), mock.patch.object(g, "decir"), \
                mock.patch.object(g, "escribir_traza"):
            rc = g.main(argv)
        return rc, rec

    def test_las_tareas_y_la_copia_recuperan_antes_que_nada_los_modos_de_prueba_no(self):
        for argv in (["--si-hay-pedido"], [], ["--forzar"]):
            _, rec = self.main(argv)
            rec.assert_called_once()
            self.assertEqual(rec.call_args.args[0], g.DIR_PAQUETE)
        for argv in (["--prueba"], ["--prueba-destino"], ["--ddl"]):
            _, rec = self.main(argv)
            rec.assert_not_called()

    def test_recuperada_relanza_la_corrida_una_vez_con_la_variable_de_relanzamiento(self):
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch("garro_recuperar.recuperar", return_value="recuperada"), \
                mock.patch("subprocess.call", return_value=0) as llamar, mock.patch.object(g, "correr_pedido") as correr:
            os.environ.pop(g.VARIABLE_RELANZADO, None)
            with self.assertRaises(SystemExit) as c:
                g.main(["--si-hay-pedido"])
        self.assertEqual(c.exception.code, 0)
        llamar.assert_called_once()
        self.assertEqual(llamar.call_args.args[0][0], sys.executable)
        self.assertEqual(llamar.call_args.kwargs["env"][g.VARIABLE_RELANZADO], "1")
        correr.assert_not_called()

    def test_con_la_marca_que_no_se_puede_borrar_no_hay_relanzamiento_en_cadena(self):
        with tempfile.TemporaryDirectory() as d:
            # una recuperación de verdad: la marca no se puede borrar -> "incompleta", sin relanzar
            os.makedirs(os.path.join(d, gr.CARPETA_RESPALDO))
            open(os.path.join(d, "garro_sync.py"), "w").write("nuevo")
            open(os.path.join(d, gr.CARPETA_RESPALDO, "garro_sync.py"), "w").write("viejo")
            gr.escribir_marca(d, {"anterior": "v1", "nueva": "v2", "archivos": ["garro_sync.py"], "previos": {"garro_sync.py": True},
                                  "comando_id": 4})
            with mock.patch.object(gr, "borrar_marca"):
                self.assertEqual(gr.recuperar(d), "incompleta")
            self.assertTrue(os.path.exists(os.path.join(d, gr.MARCA_EN_CURSO)))
            # el programa: con la variable de relanzamiento, "recuperada" con la marca otra vez presente nunca relanza
            with mock.patch.object(g, "DIR_PAQUETE", d), mock.patch.object(g, "decir"), mock.patch.object(g, "leer_archivo", side_effect=g.ErrorSync("x")), \
                    mock.patch("garro_recuperar.recuperar", return_value="recuperada"), mock.patch("subprocess.call") as llamar, \
                    mock.patch.dict(os.environ, {g.VARIABLE_RELANZADO: "1"}):
                self.assertEqual(g.main(["--si-hay-pedido"]), 1)
            llamar.assert_not_called()
        # y sin la variable pero con la marca todavía ahí tampoco se relanza
        with tempfile.TemporaryDirectory() as d:
            gr.escribir_marca(d, {"anterior": "v1", "nueva": "v2", "archivos": [], "previos": {}})
            with mock.patch.object(g, "DIR_PAQUETE", d), mock.patch.object(g, "decir"), mock.patch.object(g, "leer_archivo", side_effect=g.ErrorSync("x")), \
                    mock.patch("garro_recuperar.recuperar", return_value="recuperada"), mock.patch("subprocess.call") as llamar:
                os.environ.pop(g.VARIABLE_RELANZADO, None)
                self.assertEqual(g.main(["--si-hay-pedido"]), 1)
            llamar.assert_not_called()

    def test_con_la_marca_sin_resolver_la_corrida_solo_hace_el_latido_y_avisa(self):
        for resultado in ("incompleta", None):
            with tempfile.TemporaryDirectory() as d:
                gr.escribir_marca(d, {"anterior": "v1", "nueva": "v2", "archivos": [], "previos": {}, "comando_id": 12})
                mensajes = []
                with mock.patch.object(g, "DIR_PAQUETE", d), mock.patch.object(g, "decir", side_effect=mensajes.append), \
                        mock.patch.object(g, "leer_archivo", return_value=dict(CFG)), mock.patch.object(g, "latido_agente", return_value=True) as latido, \
                        mock.patch("garro_recuperar.recuperar", return_value=resultado), \
                        mock.patch.object(g, "correr_pedido") as pedido, mock.patch.object(g, "correr_copia") as copia, \
                        mock.patch.object(g, "cargas_auto") as cargas, mock.patch.object(g, "relevamiento_auto") as rele, \
                        mock.patch.object(g, "_atender_pedido") as atender, mock.patch.object(gc, "resolver", return_value=True) as resolver, \
                        mock.patch.object(gc, "tomar") as tomar:
                    self.assertEqual(g.main(["--si-hay-pedido", "--config", "x"]), 1)
                    latido.assert_called_once()
                    for nada in (pedido, copia, cargas, rele, atender, tomar):
                        nada.assert_not_called()
                    self.assertEqual(resolver.call_args.args[1:3], (12, "error"))
                    self.assertIn("NO se pudo restaurar", resolver.call_args.args[3]["salida"])
                    self.assertTrue(any("hay una actualización a medias" in m and "instalador" in m for m in mensajes))
                    # la copia diaria tampoco copia
                    self.assertEqual(g.main(["--config", "x"]), 1)
                    copia.assert_not_called()

    def test_la_pasada_de_comandos_con_marca_sin_resolver_no_toma_ninguno(self):
        with tempfile.TemporaryDirectory() as d:
            gr.escribir_marca(d, {"anterior": "v1", "nueva": "v2", "archivos": [], "previos": {}, "comando_id": 5})
            with mock.patch.object(gc, "tomar") as tomar, mock.patch.object(gc, "resolver", return_value=True) as resolver, mock.patch.object(g, "decir"):
                gc.pasada(dict(CFG), "x", aqui=d)
            tomar.assert_not_called()
            self.assertEqual(resolver.call_args.args[1:3], (5, "error"))
            # una marca cerrada (estado ok) no frena nada
            gr.escribir_marca(d, {"estado": "ok", "nueva": "v2", "archivos": []})
            with mock.patch.object(gc, "tomar", return_value=None) as tomar:
                gc.pasada(dict(CFG), "x", aqui=d)
            tomar.assert_called_once()

    def test_informar_marca_sin_comando_no_hace_nada(self):
        with tempfile.TemporaryDirectory() as d:
            gr.escribir_marca(d, {"archivos": [], "previos": {}, "comando_id": None})
            with mock.patch.object(gc, "resolver") as resolver:
                gc.informar_marca(dict(CFG), d)
            resolver.assert_not_called()

    def test_la_recuperacion_corre_antes_que_argparse_y_que_cualquier_import_del_paquete(self):
        import ast
        arbol = ast.parse(open(os.path.join(AQUI, "garro_sync.py"), encoding="utf-8").read())
        paquete = {"garro_cargas", "garro_escritor", "garro_movimientos", "garro_comandos", "garro_actualizar", "garro_copia",
                   "comparar_movimientos", "relevar_compras", "garro_recuperar", "garro_sync"}
        for nodo in arbol.body:  # nada del paquete se importa al cargar garro_sync
            if isinstance(nodo, (ast.Import, ast.ImportFrom)):
                nombres = {a.name.split(".")[0] for a in nodo.names} | ({nodo.module.split(".")[0]} if isinstance(nodo, ast.ImportFrom) and nodo.module else set())
                self.assertFalse(nombres & paquete, nombres)
        funciones = {n.name: n for n in arbol.body if isinstance(n, ast.FunctionDef)}
        main = funciones["main"]
        def linea_de(f, texto):
            return min(n.lineno for n in ast.walk(f) if getattr(n, "id", getattr(n, "attr", None)) == texto)
        self.assertLess(linea_de(main, "_recuperar_al_arrancar"), linea_de(main, "ArgumentParser"))
        self.assertLess(linea_de(main, "_recuperar_al_arrancar"), linea_de(main, "parse_args"))
        # dentro de la recuperación, lo primero del paquete que se importa es garro_recuperar
        recuperar = funciones["_recuperar_al_arrancar"]
        imports = sorted((n.lineno, n.names[0].name) for n in ast.walk(recuperar) if isinstance(n, ast.Import))
        self.assertEqual(imports[0][1], "garro_recuperar")
        # y antes de ella en main no hay ninguna llamada a otra función del paquete
        antes = [n for n in ast.walk(main) if isinstance(n, ast.Call) and n.lineno < linea_de(main, "_recuperar_al_arrancar")]
        nombres_llamados = {getattr(n.func, "id", getattr(n.func, "attr", "")) for n in antes}
        self.assertTrue(nombres_llamados <= {"list", "any", "get"}, nombres_llamados)

    def test_un_error_al_leer_los_argumentos_no_saltea_la_recuperacion(self):
        for argv in (["--no-existe"], ["--si-hay-pedido", "--forzar"], ["--config"]):
            with mock.patch("garro_recuperar.recuperar", return_value=None) as rec, mock.patch.object(g, "decir"):
                with self.assertRaises(SystemExit) as c:
                    with contextlib.redirect_stderr(__import__("io").StringIO()):
                        g.main(argv)
            self.assertEqual(c.exception.code, 2, argv)
            rec.assert_called_once()

    def test_sin_nada_que_recuperar_o_con_un_error_sin_marca_sigue_con_lo_que_hay(self):
        with mock.patch("garro_recuperar.recuperar", return_value=None), mock.patch("subprocess.call") as llamar, \
                mock.patch.object(g, "correr_pedido", return_value=0) as correr:
            g.main(["--si-hay-pedido"])
        llamar.assert_not_called()
        correr.assert_called_once()
        with mock.patch("garro_recuperar.recuperar", side_effect=OSError("disco")), mock.patch.object(g, "escribir_traza"), \
                mock.patch.object(g, "correr_pedido", return_value=0) as correr:
            g.main(["--si-hay-pedido"])
        correr.assert_called_once()

    def test_incompleta_frena_la_corrida(self):
        with mock.patch("garro_recuperar.recuperar", return_value="incompleta"), mock.patch.object(g, "decir"), \
                mock.patch.object(g, "leer_archivo", side_effect=g.ErrorSync("x")), mock.patch.object(g, "escribir_traza"), \
                mock.patch.object(g, "correr_pedido") as correr:
            self.assertEqual(g.main(["--si-hay-pedido"]), 1)
        correr.assert_not_called()

    def test_el_modo_autochequeo_solo_importa_y_sale_con_0_sin_recuperar_ni_leer_config(self):
        for argv in g.ARGV_TAREAS:
            rc, rec = self.main(list(argv), GARRO_SYNC_AUTOCHEQUEO="1")
            self.assertEqual(rc, 0)
            rec.assert_not_called()

    def test_argv_de_las_tareas_es_el_de_install_ps1(self):
        t = open(os.path.join(AQUI, "install.ps1"), encoding="ascii", newline="").read()
        diaria = re.search(r'\$accionDiaria = New-ScheduledTaskAction -Execute \$Pyw -Argument "([^\n]*)" -WorkingDirectory', t).group(1)
        pedidos = re.search(r'\$accionPedidos = New-ScheduledTaskAction -Execute \$Pyw -Argument "([^\n]*)" -WorkingDirectory', t).group(1)
        self.assertEqual(diaria, '`"$scriptPy`"')
        self.assertEqual(pedidos, '`"$scriptPy`" --si-hay-pedido')
        self.assertEqual(g.ARGV_TAREAS, ([], ["--si-hay-pedido"]))

    def test_de_verdad_cada_argv_de_las_tareas_arranca_e_importa_todo_sin_tocar_nada(self):
        for argv in g.ARGV_TAREAS:
            codigo, salida = gc.correr_proceso([sys.executable, os.path.join(AQUI, "garro_sync.py"), *argv], 60,
                                                env_extra={"GARRO_SYNC_AUTOCHEQUEO": "1"})
            self.assertEqual((codigo, salida.strip()), (0, f"AUTOCHEQUEO ARGV OK {g.VERSION}"), argv)

    def test_los_modulos_de_las_tareas_estan_todos_en_las_huellas(self):
        import actualizar_hashes as ah
        for m in g.MODULOS_DE_LAS_TAREAS:
            self.assertIn(f"{m}.py", ah.ARCHIVOS)
        for m in ("garro_cargas", "garro_escritor", "garro_movimientos", "garro_comandos", "garro_actualizar", "garro_copia", "comparar_movimientos"):
            self.assertIn(m, g.MODULOS_DE_LAS_TAREAS)


class Autochequeo(unittest.TestCase):
    def test_importa_el_paquete_y_lee_las_dos_bases_sin_escribir(self):
        garro, destino = mock.MagicMock(), mock.MagicMock()
        with mock.patch.object(ge, "conectar_garro", return_value=garro) as cg, \
                mock.patch.object(g, "conectar_destino", return_value=destino):
            self.assertEqual(gc.autochequeo_local(dict(CFG)), f"AUTOCHEQUEO OK {g.VERSION}")
        self.assertTrue(cg.call_args.kwargs["solo_lectura"])
        for con in (garro, destino):
            con.rollback.assert_called_once()
            con.close.assert_called_once()
            con.commit.assert_not_called()
            sqls = [c.args[0] for c in con.cursor.return_value.__enter__.return_value.execute.call_args_list]
            self.assertEqual(sqls, ["SELECT 1"])

    def test_todos_los_modulos_de_la_lista_existen_y_estan_en_las_huellas(self):
        import actualizar_hashes as ah
        for m in gc.MODULOS:
            self.assertIn(f"{m}.py", ah.ARCHIVOS)

    def test_falla_con_codigo_1_y_dice_por_que(self):
        with mock.patch.object(g, "leer_archivo", return_value=dict(CFG)), \
                mock.patch.object(gc, "autochequeo_local", side_effect=RuntimeError("sin base")), \
                mock.patch("sys.stdout") as out:
            self.assertEqual(gc.main(["--autochequeo", "--config", "x"]), 1)
        self.assertIn("AUTOCHEQUEO FALLÓ", "".join(c.args[0] for c in out.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
