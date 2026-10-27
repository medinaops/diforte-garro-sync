"""Tests mínimos de garro_sync.py (unittest de la stdlib, sin psycopg2 ni red).

Correr desde esta carpeta:  python -m pytest -q   (o:  python -m unittest -v test_garro_sync)

Los tests de paridad con las migraciones 329/334/349 leen el repo diforte-produccion: lo buscan en
la variable DIFORTE_PRODUCCION o en una carpeta hermana; si no lo encuentran, se saltean.
"""

import glob
import io
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

import garro_sync as g

AQUI = os.path.dirname(os.path.abspath(__file__))
SLEEP = mock.MagicMock()


def setUpModule():
    # Ningún test espera de verdad el reintento del candado.
    global _parche_sleep
    _parche_sleep = mock.patch.object(g.time, "sleep", SLEEP)
    _parche_sleep.start()


def tearDownModule():
    _parche_sleep.stop()


def migracion(numero):
    """Ruta de la migración `numero` en diforte-produccion (o salta el test si no está a mano)."""
    candidatos = [os.environ.get("DIFORTE_PRODUCCION", ""),
                  os.path.join(AQUI, "..", "diforte-produccion"),
                  os.path.join(AQUI, "..", "diforte-produccion-garro-sync")]
    for base in candidatos:
        rutas = sorted(glob.glob(os.path.join(base, "supabase", "migrations", f"{numero}_*.sql"))) if base else []
        rutas = [r for r in rutas if not r.endswith(".down.sql")]
        if rutas:
            return rutas[0]
    raise unittest.SkipTest(f"no se encontró la migración {numero} (definir DIFORTE_PRODUCCION)")



class ErrorPg(Exception):
    def __init__(self, msg, pgcode):
        super().__init__(msg)
        self.pgcode = pgcode


class CursorFalso:
    """Imita psycopg2: `rowcount` cambia en CADA execute (SAVEPOINT/RELEASE lo dejan en -1)."""

    def __init__(self, lock=True, resultado="insertada", fallar=None, copy_error=None, previas=None, pedido=None,
                 hay_pedido=True, fallo=None, lock_tras=0):
        self.lock, self.resultado, self.fallar, self.copy_error = lock, resultado, fallar, copy_error
        self.previas, self.pedido, self.hay_pedido, self.fallo = previas, pedido, hay_pedido, fallo
        self.lock_tras, self.intentos_lock = lock_tras, 0
        self.ejecutadas = []
        self.rowcount = -1
        self._ultimo = ""

    def execute(self, sql, params=None):
        self.ejecutadas.append((sql, params))
        if self.fallar:
            self.fallar(sql, params)
        self._ultimo = sql
        self.rowcount = 1 if sql.lstrip().startswith(("INSERT", "SELECT")) else -1

    def fetchone(self):
        if "advisory" in self._ultimo:
            if self.lock is False:
                return (False,)
            self.intentos_lock += 1
            return (self.intentos_lock > self.lock_tras,)
        if "latido_agente" in self._ultimo:
            return (self.hay_pedido,)
        if "fallo_pedido" in self._ultimo:
            return self.fallo
        if "registrar_corrida" in self._ultimo:
            return (self.resultado,)
        if "ultimos_conteos_ok" in self._ultimo:
            return (self.previas,)
        if "tomar_pedido_sync" in self._ultimo:
            return (self.pedido,)
        return None

    def copy_expert(self, sql, buf):
        if self.copy_error:
            raise self.copy_error
        self.rowcount = -1

    def sqls(self):
        return [s for s, _ in self.ejecutadas]

    def llamadas_registro(self):
        return [p for s, p in self.ejecutadas if "registrar_corrida" in s]


def conexion_falsa(cur):
    con = mock.MagicMock()
    con.cursor.return_value.__enter__.return_value = cur
    return con


def datos_falsos():
    return {n: io.StringIO("1,x\n") for n, _, _ in g.TABLAS}


def filas_falsas(n=100):
    return {nombre: n for nombre, _, _ in g.TABLAS}


CFG = {"GARRO_HOST": "h", "GARRO_PUERTO": "5432", "GARRO_BASE": "b", "GARRO_USUARIO": "u",
       "GARRO_PASSWORD": "p", "SUPABASE_HOST": "s", "SUPABASE_PUERTO": "5432",
       "SUPABASE_USUARIO": "r", "SUPABASE_PASSWORD": "q"}


def origen_falso(llamadas=None, latidos=0, filas=None):
    def leer(cfg, corrida, latido=None):
        corrida.filas = dict(filas if filas is not None else filas_falsas())
        if llamadas is not None:
            llamadas.append("leer_origen")
        for _ in range(latidos):
            latido()
        return datos_falsos()
    return leer


def correr_main(cursores, cfg=None, leer=None):
    """Corre main() con una conexión de destino por cada cursor de `cursores` (o excepción)."""
    destinos = [c if isinstance(c, BaseException) else conexion_falsa(c) for c in cursores]
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(g, "LOG_LOCAL", os.path.join(tmp, "log")), \
            mock.patch.object(g, "leer_archivo", return_value=dict(CFG if cfg is None else cfg)), \
            mock.patch.object(g, "leer_origen", side_effect=leer or origen_falso()) as lo, \
            mock.patch.object(g, "conectar_destino", side_effect=destinos):
        codigo = g.main(["--config", "x"])
        with open(os.path.join(tmp, "log"), encoding="utf-8") as f:
            log = f.read()
    return codigo, destinos, lo, log


class ParidadMigracion(unittest.TestCase):
    """TABLAS tiene que coincidir con las migraciones 329 + 350 (tablas, columnas, tipos, PK, GRANT)."""

    @classmethod
    def setUpClass(cls):
        cls.sql = ""
        for numero in (329, 350):
            with open(migracion(numero), encoding="utf-8") as f:
                cls.sql += f.read() + "\n"

    def test_tablas_columnas_tipos_y_pk(self):
        migracion = {}
        for nombre, cuerpo in re.findall(r"CREATE TABLE garro\.(\w+) \((.*?)\n\);", self.sql, re.S):
            if nombre == "sync_corridas":
                continue
            cols, pk = [], None
            for linea in cuerpo.strip().splitlines():
                m = re.match(r"\s*(\w+) ([a-z ]+?)( PRIMARY KEY)?,?$", linea)
                self.assertIsNotNone(m, f"línea no entendida en {nombre}: {linea}")
                cols.append((m.group(1), m.group(2)))
                if m.group(3):
                    pk = m.group(1)
            migracion[nombre] = (pk, cols)
        script = {n: (pk, cols) for n, pk, cols in g.TABLAS}
        self.assertEqual(list(script), list(migracion))
        for nombre in script:
            self.assertEqual(script[nombre], migracion[nombre], nombre)

    def test_grant_cubre_todas_las_tablas(self):
        grants = re.findall(r"GRANT TRUNCATE, INSERT ON (.*?) TO garro_sync;", self.sql, re.S)
        self.assertTrue(grants)
        otorgadas = [t.strip().replace("garro.", "") for m in grants for t in m.split(",")]
        self.assertEqual(sorted(otorgadas), sorted(n for n, _, _ in g.TABLAS))


class Prueba(unittest.TestCase):
    def test_prueba_no_conecta_al_destino(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, ".env.garro-sync")
            with open(cfg, "w", encoding="utf-8") as f:
                f.write("GARRO_HOST=h\nGARRO_PUERTO=5432\nGARRO_BASE=b\nGARRO_USUARIO=u\nGARRO_PASSWORD=p\n")

            def leer(cfg_, corrida, latido=None):
                corrida.filas = {"tipos_comprobantes_ventas": 1}
                return datos_falsos()

            with mock.patch.object(g, "LOG_LOCAL", os.path.join(tmp, "log")), \
                    mock.patch.object(g, "leer_origen", side_effect=leer), \
                    mock.patch.object(g, "conectar_destino") as destino:
                codigo = g.main(["--prueba", "--config", cfg])
            self.assertEqual(codigo, 0)
            destino.assert_not_called()


CFG = {"GARRO_HOST": "h", "GARRO_PUERTO": "5432", "GARRO_BASE": "b", "GARRO_USUARIO": "u",
       "GARRO_PASSWORD": "p", "SUPABASE_HOST": "s", "SUPABASE_PUERTO": "5432",
       "SUPABASE_USUARIO": "r", "SUPABASE_PASSWORD": "q"}


def origen_falso(llamadas=None, latidos=0, filas=None):
    def leer(cfg, corrida, latido=None):
        corrida.filas = dict(filas if filas is not None else filas_falsas())
        if llamadas is not None:
            llamadas.append("leer_origen")
        for _ in range(latidos):
            latido()
        return datos_falsos()
    return leer


class Corridas(unittest.TestCase):
    def test_error_en_copy_registra_y_no_hace_commit(self):
        cur1 = CursorFalso(copy_error=RuntimeError("se cortó"))
        cur2 = CursorFalso()
        codigo, (con1, con2), _, _ = correr_main([cur1, cur2])
        self.assertEqual(codigo, 1)
        con1.commit.assert_not_called()
        con1.rollback.assert_called()
        p = cur2.llamadas_registro()[0]
        self.assertEqual(p[1], "error")
        self.assertIn("Error inesperado", p[4])
        self.assertIn("[corrida ", p[2])
        con2.commit.assert_called_once()

    def test_si_falla_el_lock_no_se_lee_el_origen(self):
        cur1, cur2 = CursorFalso(lock=False), CursorFalso()
        codigo, (con1, _), lo, _ = correr_main([cur1, cur2])
        self.assertEqual(codigo, 1)
        lo.assert_not_called()
        con1.commit.assert_not_called()
        self.assertIn("otra copia corriendo", cur2.llamadas_registro()[0][4])

    def test_orden_lock_antes_de_leer_origen(self):
        eventos = []
        cur1 = CursorFalso()
        original = cur1.execute
        cur1.execute = lambda sql, params=None: (eventos.append(sql.split("(")[0].strip()[:40]), original(sql, params))[1]
        codigo, (con1,), _, _ = correr_main([cur1], leer=origen_falso(eventos))
        self.assertEqual(codigo, 0)
        self.assertEqual(eventos[0], "SELECT pg_try_advisory_xact_lock")
        self.assertLess(eventos.index("SAVEPOINT tomar_pedido"), eventos.index("leer_origen"))
        con1.commit.assert_called_once()

    def test_latido_corre_entre_tablas(self):
        cur1 = CursorFalso()
        correr_main([cur1], leer=origen_falso(latidos=3))
        self.assertEqual(cur1.sqls().count("SELECT 1"), 3)

    def test_falla_del_latido_aborta_sin_publicar(self):
        def fallar(sql, params):
            if sql == "SELECT 1":
                raise RuntimeError("server closed the connection unexpectedly")
        cur1, cur2 = CursorFalso(fallar=fallar), CursorFalso()
        codigo, (con1, _), _, _ = correr_main([cur1, cur2], leer=origen_falso(latidos=1))
        self.assertEqual(codigo, 1)
        con1.commit.assert_not_called()
        self.assertFalse(any(q.startswith("TRUNCATE") for q in cur1.sqls()))

    def test_commit_indeterminado_registra_indeterminado(self):
        cur1, cur2 = CursorFalso(), CursorFalso()
        con1 = conexion_falsa(cur1)
        con1.commit.side_effect = RuntimeError("server closed the connection unexpectedly")
        con2 = conexion_falsa(cur2)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(g, "LOG_LOCAL", os.path.join(tmp, "log")), \
                mock.patch.object(g, "leer_archivo", return_value=dict(CFG)), \
                mock.patch.object(g, "leer_origen", side_effect=origen_falso()), \
                mock.patch.object(g, "conectar_destino", side_effect=[con1, con2]):
            self.assertEqual(g.main(["--config", "x"]), 1)
        p = cur2.llamadas_registro()[0]
        self.assertEqual(p[1], "error")
        self.assertTrue(p[4].startswith("INDETERMINADO"))
        self.assertIn(p[0], p[4])  # el mismo código de corrida
        self.assertIn(p[0], p[2])
        con1.rollback.assert_not_called()

    def test_commit_indeterminado_con_ok_ya_publicado_sale_bien(self):
        # rowcount cambia con RELEASE SAVEPOINT (-1): igual se reconoce 'ya_existia_ok'
        cur1, cur2 = CursorFalso(), CursorFalso(resultado="ya_existia_ok")
        con1 = conexion_falsa(cur1)
        con1.commit.side_effect = RuntimeError("server closed the connection unexpectedly")
        con2 = conexion_falsa(cur2)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(g, "LOG_LOCAL", os.path.join(tmp, "log")), \
                mock.patch.object(g, "leer_archivo", return_value=dict(CFG)), \
                mock.patch.object(g, "leer_origen", side_effect=origen_falso()), \
                mock.patch.object(g, "conectar_destino", side_effect=[con1, con2]):
            self.assertEqual(g.main(["--config", "x"]), 0)
            with open(os.path.join(tmp, "log"), encoding="utf-8") as f:
                self.assertIn("la copia", f.read())

    def test_configuracion_parcial_registra_en_supabase(self):
        cfg = {k: v for k, v in CFG.items() if k != "GARRO_PASSWORD"}
        cur = CursorFalso()
        codigo, _, lo, _ = correr_main([cur], cfg=cfg)
        self.assertEqual(codigo, 1)
        lo.assert_not_called()
        self.assertIn("GARRO_PASSWORD", cur.llamadas_registro()[0][4])

    def test_si_no_se_puede_registrar_queda_en_el_log(self):
        codigo, _, _, log = correr_main([CursorFalso(copy_error=RuntimeError("x")), OSError("sin red")])
        self.assertEqual(codigo, 1)
        self.assertIn("NO pudo registrarse", log)


class RegistroDeCorridas(unittest.TestCase):
    def registrar(self, estado="error", **kw):
        cur = CursorFalso(**kw)
        corrida = g.Corrida()
        resultado = g.insertar_corrida(cur, corrida, estado, "boom" if estado == "error" else None)
        return resultado, cur, corrida

    def test_tres_retornos_de_la_funcion(self):
        for r in ("insertada", "ya_existia_ok", "ya_existia_error"):
            resultado, cur, corrida = self.registrar(resultado=r)
            self.assertEqual(resultado, r)
            self.assertEqual(cur.llamadas_registro()[0][0], corrida.id)

    def test_resultado_se_lee_antes_del_release(self):
        resultado, cur, _ = self.registrar(resultado="ya_existia_ok")
        self.assertEqual(resultado, "ya_existia_ok")
        self.assertEqual(cur.rowcount, -1)  # el RELEASE ya pisó rowcount: no se usa

    def test_sin_funcion_cae_al_insert_plano_con_savepoint(self):
        def fallar(sql, params):
            if "registrar_corrida" in sql:
                raise ErrorPg("function garro.registrar_corrida does not exist", "42883")
        resultado, cur, corrida = self.registrar(fallar=fallar)
        sqls = cur.sqls()
        self.assertEqual(resultado, "insertada")
        self.assertIn("ROLLBACK TO SAVEPOINT sync_corrida", sqls)
        insert = [e for e in cur.ejecutadas if e[0].startswith("INSERT")][-1]
        self.assertNotIn("corrida_id", insert[0])
        self.assertNotIn("ON CONFLICT", insert[0])
        self.assertIn(corrida.id, insert[1][1])  # el código sigue en `origen`
        self.assertEqual(sqls[-1], "RELEASE SAVEPOINT sync_corrida")

    def test_choque_de_pk_u_otro_error_se_propaga(self):
        for codigo in ("23505", "42501"):
            def fallar(sql, params, codigo=codigo):
                if "registrar_corrida" in sql:
                    raise ErrorPg("duplicate key value violates unique constraint sync_corridas_pkey", codigo)
            cur = CursorFalso(fallar=fallar)
            with self.assertRaises(ErrorPg):
                g.insertar_corrida(cur, g.Corrida(), "error", "x")
            self.assertFalse(any(s.startswith("INSERT") for s in cur.sqls()))

    def test_choque_de_pk_deja_el_error_en_el_log(self):
        def fallar(sql, params):
            if "registrar_corrida" in sql and params[1] == "error":
                raise ErrorPg("duplicate key value violates unique constraint", "23505")
        codigo, _, _, log = correr_main([CursorFalso(copy_error=RuntimeError("x")), CursorFalso(fallar=fallar)])
        self.assertEqual(codigo, 1)
        self.assertIn("NO pudo registrarse", log)

    def test_ok_que_ya_existia_no_se_acepta(self):
        cur = CursorFalso(resultado="ya_existia_error")
        codigo, _, _, _ = correr_main([cur, CursorFalso()])
        self.assertEqual(codigo, 1)

    def test_funcion_y_columnas_de_la_334(self):
        with open(migracion(334), encoding="utf-8") as f:
            m334 = f.read()
        with open(migracion(329), encoding="utf-8") as f:
            m329 = f.read()
        cuerpo = re.search(r"CREATE TABLE garro\.sync_corridas \((.*?)\n\);", m329, re.S).group(1)
        columnas = {l.split()[0] for l in cuerpo.strip().splitlines()}
        columnas |= set(re.findall(r"ADD COLUMN (\w+)", m334))
        usadas = set()
        for fuente in (m334, open(g.__file__, encoding="utf-8").read()):
            for lista in re.findall(r"INSERT INTO garro\.sync_corridas \(([^)]*)\)", fuente):
                usadas |= {c.strip() for c in lista.split(",")}
        self.assertTrue(usadas)
        self.assertLessEqual(usadas, columnas)
        self.assertIn("SECURITY DEFINER", m334)
        self.assertIn("ON CONFLICT (corrida_id) DO NOTHING", m334)
        self.assertIn("TO garro_sync", m334)
        self.assertIn("garro.registrar_corrida", open(g.__file__, encoding="utf-8").read())


class TraductorDeErrores(unittest.TestCase):
    def test_casos(self):
        casos = [
            (Exception('FATAL: password authentication failed for user "x"'), "origen", "Garro) rechazó"),
            (Exception('FATAL: password authentication failed for user "x"'), "destino", "Supabase rechazó"),
            (Exception("could not connect to server: Connection refused"), "origen", "VPN Radmin"),
            (Exception("could not translate host name"), "destino", "No se puede conectar con Supabase"),
            (UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "x"), "origen", "GARRO_ENCODING=WIN1252"),
            (Exception("canceling statement due to statement timeout"), "copia", "timeout"),
            (Exception("timeout expired"), "origen", "timeout"),
            (Exception("server closed the connection unexpectedly"), "copia", "sin llegar a confirmarla"),
            (g.ErrorSync("Falta el archivo de configuración .env.garro-sync"), "config", "Falta el archivo"),
            (KeyError("algo"), "copia", "garro_sync.log"),
        ]
        for exc, etapa, esperado in casos:
            self.assertIn(esperado, g.traducir_error(exc, etapa), repr(exc))

    def test_rechazos_del_servidor_ya_decodificados(self):
        casos = [
            ('FATAL:  no hay una línea en pg_hba.conf para «203.0.113.7», usuario «u», base de datos «b», sin cifrado',
             "pg_hba.conf"),
            ('FATAL:  la autentificación password falló para el usuario «u»', "-Reconfigurar"),
            ('FATAL:  no existe la base de datos «b»', "GARRO_BASE"),
        ]
        for texto, esperado in casos:
            msg = g.traducir_error(Exception(texto), "origen")
            self.assertIn(esperado, msg, texto)
            self.assertNotIn("GARRO_ENCODING", msg)

    def test_config_faltante(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(g.ErrorSync) as ctx:
                g.leer_archivo(os.path.join(tmp, "no-existe"))
            self.assertIn("Falta el archivo", str(ctx.exception))
            ruta = os.path.join(tmp, "c")
            with open(ruta, "w") as f:
                f.write("GARRO_HOST=h\n")
            with self.assertRaises(g.ErrorSync) as ctx:
                g.validar_origen(g.leer_archivo(ruta))
            self.assertIn("GARRO_PUERTO", str(ctx.exception))


def error_de_conexion(mensaje, codec="latin-1"):
    crudo = mensaje.encode(codec)
    return UnicodeDecodeError("utf-8", crudo, 79, 80, "invalid continuation byte")


class ConexionOrigenRechazada(unittest.TestCase):
    def conectar(self, error):
        falso = mock.MagicMock()
        falso.connect.side_effect = error
        with mock.patch.dict(sys.modules, {"psycopg2": falso}):
            return g.conectar_origen(dict(CFG))

    def test_pg_hba_en_espanol_latin1(self):
        texto = ('FATAL:  no hay una línea en pg_hba.conf para «203.0.113.7», usuario «u», '
                 'base de datos «b», sin cifrado')
        with self.assertRaises(g.ErrorSync) as ctx:
            self.conectar(error_de_conexion(texto))
        msg = str(ctx.exception)
        self.assertIn("pg_hba.conf", msg)
        self.assertIn("administrador del servidor", msg)
        self.assertIn("línea", msg)
        self.assertIn("203.0.113.7", msg)  # el texto del servidor viaja para ver qué pide
        self.assertNotIn("GARRO_ENCODING", msg)

    def test_pg_hba_en_ingles(self):
        texto = 'FATAL:  no pg_hba.conf entry for host "203.0.113.7", user "u", database "b", no encryption'
        with self.assertRaises(g.ErrorSync) as ctx:
            self.conectar(error_de_conexion(texto))
        self.assertIn("administrador del servidor", str(ctx.exception))

    def test_contrasena_rechazada_cp1252(self):
        texto = 'FATAL:  la autentificación password falló para el usuario «u»'
        with self.assertRaises(g.ErrorSync) as ctx:
            self.conectar(error_de_conexion(texto, "cp1252"))
        msg = str(ctx.exception)
        self.assertIn("-Reconfigurar", msg)
        self.assertNotIn("GARRO_ENCODING", msg)

    def test_contrasena_rechazada_en_ingles(self):
        with self.assertRaises(g.ErrorSync) as ctx:
            self.conectar(error_de_conexion('FATAL:  password authentication failed for user "u"'))
        self.assertIn("-Reconfigurar", str(ctx.exception))

    def test_base_inexistente(self):
        for texto in ('FATAL:  no existe la base de datos «b»', 'FATAL:  database "b" does not exist'):
            with self.assertRaises(g.ErrorSync) as ctx:
                self.conectar(error_de_conexion(texto))
            self.assertIn("GARRO_BASE", str(ctx.exception))

    def test_otro_muestra_el_texto_del_servidor(self):
        with self.assertRaises(g.ErrorSync) as ctx:
            self.conectar(error_de_conexion("FATAL:  el sistema está arrancando"))
        self.assertIn("el sistema está arrancando", str(ctx.exception))

    def test_decode_error_de_conexion_sin_traducir_no_pide_encoding(self):
        msg = g.traducir_error(error_de_conexion("FATAL:  algo raro de la conexión"), "origen")
        self.assertNotIn("GARRO_ENCODING", msg)

    def test_decode_error_de_datos_sigue_pidiendo_encoding(self):
        e = UnicodeDecodeError("utf-8", b"Nota D\xe9bito", 6, 7, "invalid continuation byte")
        self.assertIn("GARRO_ENCODING=WIN1252", g.traducir_error(e, "origen"))
        self.assertIn("GARRO_ENCODING=WIN1252", g.traducir_error(Exception("invalid byte sequence for encoding"), "origen"))


class ParidadMigracion349(unittest.TestCase):
    def test_funciones_que_llama_el_script_existen_y_son_de_garro_sync(self):
        with open(migracion(349), encoding="utf-8") as f:
            sql = f.read()
        codigo = open(g.__file__, encoding="utf-8").read()
        for nombre in ("latido_agente", "tomar_pedido_sync", "fallo_pedido", "ultimos_conteos_ok"):
            self.assertIn(f"garro.{nombre}", codigo)
            self.assertRegex(sql, rf"CREATE FUNCTION garro\.{nombre}\(")
            self.assertRegex(sql, rf"GRANT EXECUTE ON FUNCTION garro\.{nombre}\([^)]*\) TO garro_sync;")


class ConexionSegura(unittest.TestCase):
    def conectar(self):
        falso = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"psycopg2": falso}):
            g.conectar_destino(dict(CFG))
        return falso.connect.call_args.kwargs

    def test_verifica_certificado_y_exige_scram(self):
        kw = self.conectar()
        self.assertEqual(kw["sslmode"], "verify-full")
        self.assertEqual(kw["require_auth"], "scram-sha-256")
        self.assertEqual(kw["sslrootcert"], g.CA_SUPABASE)
        self.assertNotIn("channel_binding", kw)

    def test_la_ca_viaja_en_el_repo_al_lado_del_script(self):
        self.assertEqual(os.path.dirname(g.CA_SUPABASE), os.path.dirname(os.path.abspath(g.__file__)))
        self.assertTrue(os.path.exists(g.CA_SUPABASE))
        self.assertIn("BEGIN CERTIFICATE", open(g.CA_SUPABASE, encoding="ascii").read())

    def test_sin_el_archivo_de_la_ca_no_conecta(self):
        with mock.patch.object(g, "CA_SUPABASE", os.path.join(AQUI, "no-existe.crt")), \
                mock.patch.dict(sys.modules, {"psycopg2": mock.MagicMock()}):
            with self.assertRaises(g.ErrorSync):
                g.conectar_destino(dict(CFG))

    def test_error_de_certificado_se_explica_en_castellano(self):
        e = Exception('connection to server at "x" failed: SSL error: certificate verify failed')
        msg = g.traducir_error(e, "destino")
        self.assertIn("certificado", msg)
        self.assertIn("No se envió ninguna contraseña", msg)
        e2 = Exception('connection to server at "x" failed: server certificate for "a" does not match host name "b"')
        self.assertIn("certificado", g.traducir_error(e2, "destino"))


class IntegridadDelPaquete(unittest.TestCase):
    def test_las_huellas_de_install_ps1_coinciden_con_los_archivos(self):
        import actualizar_hashes as h
        texto = open(h.INSTALADOR, encoding="ascii", newline="").read()
        bloque = re.search(r"# BEGIN HASHES\r?\n(.*?)# END HASHES", texto, re.S).group(1)
        for nombre in h.ARCHIVOS:
            self.assertIn(f'"{nombre}" = "{h.sha256(nombre)}"', bloque,
                          f"huella desactualizada de {nombre}: correr python3 actualizar_hashes.py")

    def test_install_ps1_es_solo_ascii_y_sin_rutas_de_la_raiz(self):
        texto = open(os.path.join(AQUI, "install.ps1"), "rb").read()
        texto.decode("ascii")
        self.assertNotIn(b"C:\\diforte", texto)
        activas = [l for l in texto.decode("ascii").splitlines() if not l.lstrip().startswith("#")]
        self.assertFalse(any("TimeSpan]::MaxValue" in l for l in activas))  # rompe Register-ScheduledTask en Win10/11

    def test_install_ps1_sin_variables_seguidas_de_dos_puntos(self):
        # "$usuario:" dentro de comillas es un error de parseo en PowerShell (lo toma como unidad/ambito):
        # todo el script falla antes de correr. Se escribe "${usuario}:".
        texto = open(os.path.join(AQUI, "install.ps1"), encoding="ascii", newline="").read()
        malas = [m.group(0) for m in re.finditer(r"\$([A-Za-z_][A-Za-z0-9_]*):", texto)
                 if m.group(1).lower() not in ("env", "script", "global", "local", "using", "private", "function", "variable")]
        self.assertEqual(malas, [])

    def test_modo_servidor_prueba_s4u_antes_de_tocar_las_tareas_reales(self):
        texto = open(os.path.join(AQUI, "install.ps1"), encoding="ascii", newline="").read()
        activo = "\n".join(l for l in texto.splitlines() if not l.lstrip().startswith("#"))
        prueba = activo.index('Paso "Prueba de las tareas sin sesion iniciada')
        self.assertLess(prueba, activo.index("Register-ScheduledTask -TaskName $TareaDiaria"))
        self.assertLess(prueba, activo.index("Register-ScheduledTask -TaskName $TareaPedidos"))
        self.assertIn('-LogonType $tipoSesion', activo)
        self.assertIn('$PSVersionTable.PSVersion -lt [Version]"5.1"', activo)
        # La prueba corre las dos verificaciones, espera su fin, exige codigo 0 y siempre borra la tarea temporal.
        bloque = activo[prueba:activo.index("Register-ScheduledTask -TaskName $TareaDiaria")]
        for esperado in ('@("--prueba", "--prueba-destino")', "Start-ScheduledTask -TaskName $TareaPrueba",
                         "LastTaskResult -ne 267009", "LastTaskResult -ne 0", "AddMinutes(10)",
                         "Las tareas anteriores no se tocaron"):
            self.assertIn(esperado, bloque)
        self.assertLess(bloque.index("} catch {"), bloque.index("} finally {"))
        fin = bloque[bloque.index("} finally {"):]
        self.assertLess(fin.index("Stop-ScheduledTask -TaskName $TareaPrueba"), fin.index("Unregister-ScheduledTask -TaskName $TareaPrueba"))
        # Una restauracion fallida no se informa como exitosa.
        self.assertIn("restauracion quedo INCOMPLETA", activo)
        # Respaldo antes del primer registro real y restauracion en el catch de toda la seccion.
        respaldo = activo.index("Export-ScheduledTask -TaskName $t")
        self.assertLess(respaldo, activo.index("Register-ScheduledTask -TaskName $TareaDiaria"))
        restaura = activo.index("Register-ScheduledTask -TaskName $t -Xml $respaldoTareas[$t] -Force")
        self.assertLess(activo.index("throw \"La tarea '$t' quedo con sesion"), restaura)
        self.assertLess(activo.index("Ok \"$TareaPedidos (cada 5 minutos"), restaura)
        # -Desinstalar tambien borra una tarea de prueba que haya quedado.
        desinstalar = activo[activo.index("if ($Desinstalar) {"):]
        self.assertIn("Diforte Garro - prueba de instalacion", desinstalar[:desinstalar.index("return")])


class GuardiaDeFoto(unittest.TestCase):
    def test_obligatorias_vacias_se_rechazan(self):
        for tabla in g.OBLIGATORIAS:
            nuevas = filas_falsas()
            nuevas[tabla] = 0
            with self.assertRaises(g.ErrorSync) as ctx:
                g.validar_foto(nuevas, None, False)
            self.assertIn(tabla, str(ctx.exception))

    def test_tabla_obligatoria_ausente_se_rechaza(self):
        nuevas = filas_falsas()
        del nuevas["clientes"]
        with self.assertRaises(g.ErrorSync):
            g.validar_foto(nuevas, None, False)

    def test_sin_previas_alcanza_con_que_no_vengan_vacias(self):
        nuevas = filas_falsas()
        nuevas["listas_precios"] = 0  # no es obligatoria
        g.validar_foto(nuevas, None, False)
        g.validar_foto(nuevas, {}, False)

    def test_tablas_de_cotizaciones_no_son_clave(self):
        nuevas = filas_falsas()
        for t in ("presupuestos", "presupuestos_x_clientes", "items_presupuesto",
                  "productos_x_items_presupuestos", "conceptos_presupuestos", "conceptos_x_items_presupuesto"):
            self.assertIn(t, nuevas)
            self.assertNotIn(t, g.OBLIGATORIAS)
            nuevas[t] = 0
        g.validar_foto(nuevas, None, False)

    def test_tabla_nueva_sin_conteo_previo_no_se_compara(self):
        previas = {n: 100 for n in filas_falsas() if not n.startswith(("presupuestos", "items_presupuesto"))}
        nuevas = filas_falsas()
        nuevas["presupuestos"] = 0
        nuevas["items_presupuesto"] = 50
        g.validar_foto(nuevas, previas, False)
        previas["presupuestos"] = 0
        g.validar_foto(nuevas, previas, False)

    def test_tabla_que_pasa_a_cero_aborta(self):
        nuevas = filas_falsas()
        nuevas["listas_precios"] = 0
        with self.assertRaises(g.ErrorSync) as ctx:
            g.validar_foto(nuevas, {"listas_precios": 5}, False)
        self.assertIn("listas_precios", str(ctx.exception))

    def test_cero_que_ya_era_cero_no_aborta(self):
        nuevas = filas_falsas()
        nuevas["listas_precios"] = 0
        g.validar_foto(nuevas, {"listas_precios": 0}, False)

    def test_comprobantes_e_items_toleran_2_por_ciento(self):
        for tabla in g.ESTRICTAS:
            nuevas = filas_falsas(1000)
            nuevas[tabla] = 980  # justo -2 %: pasa
            g.validar_foto(nuevas, {tabla: 1000}, False)
            nuevas[tabla] = 979  # -2,1 %: aborta
            with self.assertRaises(g.ErrorSync) as ctx:
                g.validar_foto(nuevas, {tabla: 1000}, False)
            self.assertIn(tabla, str(ctx.exception))

    def test_el_resto_tolera_20_por_ciento(self):
        nuevas = filas_falsas(1000)
        nuevas["productos"] = 800
        g.validar_foto(nuevas, {"productos": 1000}, False)
        nuevas["productos"] = 799
        with self.assertRaises(g.ErrorSync):
            g.validar_foto(nuevas, {"productos": 1000}, False)

    def test_comprobantes_ventas_electronicos_usa_el_tope_general(self):
        nuevas = filas_falsas(1000)
        nuevas["comprobantes_ventas_electronicos"] = 900  # -10 %: no es estricta
        g.validar_foto(nuevas, {"comprobantes_ventas_electronicos": 1000}, False)

    def test_tolerancia_absoluta_de_2_filas(self):
        nuevas = filas_falsas()
        nuevas["listas_precios"] = 3
        g.validar_foto(nuevas, {"listas_precios": 4}, False)  # -25 % pero solo 1 fila
        nuevas["listas_precios"] = 2
        g.validar_foto(nuevas, {"listas_precios": 4}, False)  # 2 filas: todavía pasa
        nuevas["listas_precios"] = 1
        with self.assertRaises(g.ErrorSync):
            g.validar_foto(nuevas, {"listas_precios": 4}, False)  # 3 filas y -75 %

    def test_la_tolerancia_absoluta_no_salva_el_paso_a_cero(self):
        nuevas = filas_falsas()
        nuevas["listas_precios"] = 0
        for antes in (1, 2):
            with self.assertRaises(g.ErrorSync):
                g.validar_foto(nuevas, {"listas_precios": antes}, False)

    def test_estrictas_tambien_respetan_la_tolerancia_absoluta(self):
        nuevas = filas_falsas(100)
        nuevas["comprobantes_ventas"] = 48  # -4 % pero solo 2 filas
        g.validar_foto(nuevas, {"comprobantes_ventas": 50}, False)

    def test_crecer_nunca_aborta(self):
        g.validar_foto(filas_falsas(5000), {n: 100 for n, _, _ in g.TABLAS}, False)

    def test_forzar_saltea_todo(self):
        g.validar_foto({n: 0 for n, _, _ in g.TABLAS}, {n: 100 for n, _, _ in g.TABLAS}, True)

    def test_leer_previas(self):
        cur = CursorFalso(previas={"productos": "7"})
        self.assertEqual(g.leer_previas(cur), {"productos": 7})
        self.assertEqual(g.leer_previas(CursorFalso(previas='{"productos": 3}')), {"productos": 3})
        self.assertIsNone(g.leer_previas(CursorFalso()))

    def test_leer_previas_sin_funcion_usa_none_con_savepoint(self):
        def fallar(sql, params):
            if "ultimos_conteos_ok" in sql:
                raise ErrorPg("function garro.ultimos_conteos_ok() does not exist", "42883")
        cur = CursorFalso(fallar=fallar)
        self.assertIsNone(g.leer_previas(cur))
        self.assertIn("ROLLBACK TO SAVEPOINT conteos_previos", cur.sqls())
        self.assertEqual(cur.sqls()[-1], "RELEASE SAVEPOINT conteos_previos")

    def test_otro_error_al_leer_previas_se_propaga(self):
        def fallar(sql, params):
            if "ultimos_conteos_ok" in sql:
                raise ErrorPg("permission denied", "42501")
        with self.assertRaises(ErrorPg):
            g.leer_previas(CursorFalso(fallar=fallar))


class GuardiaEnLaCopia(unittest.TestCase):
    def test_foto_vacia_no_trunca_ni_publica_y_registra_el_error(self):
        vacia = filas_falsas()
        vacia["clientes"] = 0
        cur1, cur2 = CursorFalso(), CursorFalso()
        codigo, (con1, con2), _, _ = correr_main([cur1, cur2], leer=origen_falso(filas=vacia))
        self.assertEqual(codigo, 1)
        self.assertFalse(any(q.startswith("TRUNCATE") for q in cur1.sqls()))
        self.assertFalse(any("COPY" in q for q in cur1.sqls()))
        con1.commit.assert_not_called()
        self.assertIn("clientes", cur2.llamadas_registro()[0][4])

    def test_caida_contra_la_ultima_copia_no_trunca(self):
        previas = {"comprobantes_ventas": 1000}
        nuevas = filas_falsas(100)
        nuevas["comprobantes_ventas"] = 900
        cur1, cur2 = CursorFalso(previas=previas), CursorFalso()
        codigo, (con1, _), _, _ = correr_main([cur1, cur2], leer=origen_falso(filas=nuevas))
        self.assertEqual(codigo, 1)
        self.assertFalse(any(q.startswith("TRUNCATE") for q in cur1.sqls()))
        con1.commit.assert_not_called()

    def test_foto_sana_trunca_despues_de_la_guardia(self):
        cur1 = CursorFalso(previas={"productos": 100})
        codigo, (con1,), _, _ = correr_main([cur1])
        self.assertEqual(codigo, 0)
        sqls = cur1.sqls()
        self.assertLess(next(i for i, q in enumerate(sqls) if "ultimos_conteos_ok" in q),
                        next(i for i, q in enumerate(sqls) if q.startswith("TRUNCATE")))
        con1.commit.assert_called_once()

    def test_forzar_publica_una_foto_que_la_guardia_rechazaria(self):
        vacia = filas_falsas()
        vacia["clientes"] = 0
        cur1 = CursorFalso()
        destino = conexion_falsa(cur1)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(g, "LOG_LOCAL", os.path.join(tmp, "log")), \
                mock.patch.object(g, "leer_archivo", return_value=dict(CFG)), \
                mock.patch.object(g, "leer_origen", side_effect=origen_falso(filas=vacia)), \
                mock.patch.object(g, "conectar_destino", side_effect=[destino]):
            self.assertEqual(g.main(["--forzar", "--config", "x"]), 0)
        self.assertTrue(any(q.startswith("TRUNCATE") for q in cur1.sqls()))

    def test_forzar_no_se_combina_con_modo_pedido(self):
        with self.assertRaises(SystemExit):
            g.main(["--forzar", "--si-hay-pedido"])

    def test_sin_la_funcion_de_conteos_la_copia_sigue_con_las_reglas_basicas(self):
        def fallar(sql, params):
            if "ultimos_conteos_ok" in sql:
                raise ErrorPg("function does not exist", "42883")
        cur1 = CursorFalso(fallar=fallar)
        codigo, _, _, _ = correr_main([cur1])
        self.assertEqual(codigo, 0)
        self.assertTrue(any(q.startswith("TRUNCATE") for q in cur1.sqls()))


class ModoPedido(unittest.TestCase):
    def correr(self, destinos, filas=None, argv=None, tmp=None):
        con = [d if isinstance(d, BaseException) else conexion_falsa(d) for d in destinos]
        propio = tmp is None
        carpeta = tempfile.TemporaryDirectory() if propio else None
        ruta_tmp = carpeta.name if propio else tmp
        try:
            with mock.patch.object(g, "LOG_LOCAL", os.path.join(ruta_tmp, "log")), \
                    mock.patch.object(g, "leer_archivo", return_value=dict(CFG)), \
                    mock.patch.object(g, "leer_origen", side_effect=origen_falso(filas=filas)) as lo, \
                    mock.patch.object(g, "conectar_destino", side_effect=con):
                codigo = g.main(argv or ["--si-hay-pedido", "--config", "x"])
            ruta = os.path.join(ruta_tmp, "log")
            log = open(ruta, encoding="utf-8").read() if os.path.exists(ruta) else ""
        finally:
            if carpeta:
                carpeta.cleanup()
        return codigo, con, lo, log

    @staticmethod
    def vacia():
        v = filas_falsas()
        v["clientes"] = 0  # la guardia rechaza la foto
        return v

    def setUp(self):
        SLEEP.reset_mock()

    def test_sin_pedido_el_sondeo_termina_tras_el_latido_sin_candado_ni_otra_transaccion(self):
        cur_latido = CursorFalso(hay_pedido=False)
        codigo, con, lo, log = self.correr([cur_latido])  # una 2.ª conexión fallaría (lista agotada)
        self.assertEqual(codigo, 0)
        self.assertEqual(log, "")
        lo.assert_not_called()
        self.assertEqual(len(con), 1)
        con[0].commit.assert_called_once()
        con[0].close.assert_called()
        self.assertFalse(any("advisory" in q for q in cur_latido.sqls()))

    def test_si_otro_se_llevo_el_pedido_entre_el_latido_y_la_toma_no_copia(self):
        cur_copia = CursorFalso(pedido=None)
        codigo, (_, con_copia), lo, log = self.correr([CursorFalso(), cur_copia])
        self.assertEqual(codigo, 0)
        self.assertEqual(log, "")
        lo.assert_not_called()
        con_copia.commit.assert_not_called()
        con_copia.rollback.assert_called()
        self.assertFalse(any(q.startswith("TRUNCATE") for q in cur_copia.sqls()))

    def test_el_pedido_se_toma_en_la_transaccion_de_la_copia_despues_del_candado(self):
        cur_latido, cur_copia = CursorFalso(), CursorFalso(pedido=7)
        codigo, (_, con_copia), lo, log = self.correr([cur_latido, cur_copia])
        self.assertEqual(codigo, 0)
        lo.assert_called_once()
        self.assertIn("pedido de actualización", log)
        sqls = cur_copia.sqls()
        self.assertLess(next(i for i, q in enumerate(sqls) if "advisory" in q),
                        next(i for i, q in enumerate(sqls) if "tomar_pedido_sync" in q))
        self.assertLess(next(i for i, q in enumerate(sqls) if "tomar_pedido_sync" in q),
                        next(i for i, q in enumerate(sqls) if q.startswith("TRUNCATE")))
        self.assertFalse(any("tomar_pedido_sync" in q for q in cur_latido.sqls()))
        con_copia.commit.assert_called_once()

    def test_candado_ocupado_sale_con_cero_sin_error_sin_esperar_y_sin_tomar_el_pedido(self):
        cur_copia = CursorFalso(lock=False, pedido=1)
        codigo, con, lo, log = self.correr([CursorFalso(), cur_copia])  # una 3.ª conexión (error) fallaría
        self.assertEqual(codigo, 0)
        self.assertEqual(log, "")
        lo.assert_not_called()
        SLEEP.assert_not_called()
        self.assertEqual(sum("advisory" in q for q in cur_copia.sqls()), 1)
        self.assertFalse(any("tomar_pedido_sync" in q for q in cur_copia.sqls()))

    def test_falla_de_la_guardia_es_definitiva_y_se_registra_en_sync_corridas(self):
        cur_copia, cur_fallo, cur_error = CursorFalso(pedido=9), CursorFalso(fallo=(1, "fallido")), CursorFalso()
        codigo, (_, con_copia, con_fallo, con_error), _, log = self.correr(
            [CursorFalso(), cur_copia, cur_fallo, cur_error], filas=self.vacia())
        self.assertEqual(codigo, 1)
        con_copia.commit.assert_not_called()  # el «tomado» nunca se confirmó: el pedido vuelve a pendiente
        con_copia.rollback.assert_called()
        self.assertFalse(any(q.startswith("TRUNCATE") for q in cur_copia.sqls()))
        pid, definitivo, msg = next(p for q, p in cur_fallo.ejecutadas if "fallo_pedido" in q)
        self.assertEqual((pid, definitivo), (9, True))
        self.assertIn("clientes", msg)
        con_fallo.commit.assert_called_once()
        self.assertIn("clientes", cur_error.llamadas_registro()[0][4])
        self.assertIn("FALLIDO", log)
        self.assertIn("Traceback", log)

    def test_falla_comun_primer_intento_es_no_definitiva_y_se_registra(self):
        cur_fallo, cur_error = CursorFalso(fallo=(1, "pendiente")), CursorFalso()
        codigo, _, _, _ = self.correr([CursorFalso(), CursorFalso(pedido=3, copy_error=RuntimeError("se cortó")),
                                       cur_fallo, cur_error])
        self.assertEqual(codigo, 1)
        self.assertFalse(next(p for q, p in cur_fallo.ejecutadas if "fallo_pedido" in q)[1])
        self.assertEqual(len(cur_error.llamadas_registro()), 1)

    def test_falla_comun_intermedia_solo_deja_una_linea_en_el_log(self):
        cur_fallo = CursorFalso(fallo=(2, "pendiente"))
        codigo, con, _, log = self.correr([CursorFalso(), CursorFalso(pedido=3, copy_error=RuntimeError("se cortó")),
                                           cur_fallo])  # una 4.ª conexión (registrar_error) fallaría
        self.assertEqual(codigo, 1)
        self.assertEqual(len(con), 3)
        self.assertIn("intento 2 de 3", log)

    def test_tercer_intento_fallido_se_registra_otra_vez(self):
        cur_error = CursorFalso()
        codigo, _, _, log = self.correr([CursorFalso(), CursorFalso(pedido=3, copy_error=RuntimeError("x")),
                                         CursorFalso(fallo=(3, "fallido")), cur_error])
        self.assertEqual(codigo, 1)
        self.assertEqual(len(cur_error.llamadas_registro()), 1)
        self.assertIn("FALLIDO", log)

    def test_si_no_se_pudo_contar_el_intento_igual_se_registra_el_error(self):
        cur_error = CursorFalso()
        codigo, _, _, _ = self.correr([CursorFalso(), CursorFalso(pedido=3, copy_error=RuntimeError("x")),
                                       OSError("sin red"), cur_error])
        self.assertEqual(codigo, 1)
        self.assertEqual(len(cur_error.llamadas_registro()), 1)

    def test_el_modo_pedido_nunca_saltea_la_guardia(self):
        cur_copia = CursorFalso(pedido=1)
        codigo, _, _, _ = self.correr([CursorFalso(), cur_copia, CursorFalso(fallo=(1, "fallido")), CursorFalso()],
                                      filas=self.vacia())
        self.assertEqual(codigo, 1)
        self.assertFalse(any(q.startswith("TRUNCATE") for q in cur_copia.sqls()))

    def test_sin_la_funcion_dice_que_falta_la_349_y_no_registra_en_sync_corridas(self):
        def fallar(sql, params):
            raise ErrorPg("function garro.latido_agente(text) does not exist", "42883")
        codigo, _, lo, log = self.correr([CursorFalso(fallar=fallar)])  # solo 1 conexión: no hay registrar_error
        self.assertEqual(codigo, 1)
        self.assertIn("migración 349", log)
        lo.assert_not_called()

    def test_con_la_red_caida_deja_una_linea_corta_y_el_traceback_una_vez_por_hora(self):
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(3):
                codigo, _, _, log = self.correr([OSError("could not connect to server")], tmp=tmp)
                self.assertEqual(codigo, 1)
            self.assertEqual(log.count("(pedido) ERROR"), 3)
            self.assertEqual(log.count("Traceback"), 1)
            marca = os.path.join(tmp, "log.traza")
            viejo = os.path.getmtime(marca) - 3601
            os.utime(marca, (viejo, viejo))
            _, _, _, log = self.correr([OSError("could not connect to server")], tmp=tmp)
            self.assertEqual(log.count("Traceback"), 2)

    def test_el_host_va_como_parametro_del_latido(self):
        cur = CursorFalso(hay_pedido=False)
        with mock.patch.object(g.socket, "gethostname", return_value="PC-OFICINA"):
            self.correr([cur])
        self.assertEqual(cur.ejecutadas[0][1], ("PC-OFICINA",))

    def test_cada_camino_cierra_su_conexion_antes_de_abrir_otra(self):
        abiertas, maximo, n = [0], [0], [0]
        cursores = [CursorFalso(), CursorFalso(pedido=1), CursorFalso(fallo=(1, "fallido")), CursorFalso()]

        def abrir(cfg):
            abiertas[0] += 1
            maximo[0] = max(maximo[0], abiertas[0])
            con = conexion_falsa(cursores[n[0]])
            n[0] += 1
            con.close.side_effect = lambda: abiertas.__setitem__(0, abiertas[0] - 1)
            return con
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(g, "LOG_LOCAL", os.path.join(tmp, "log")), \
                mock.patch.object(g, "leer_archivo", return_value=dict(CFG)), \
                mock.patch.object(g, "leer_origen", side_effect=origen_falso(filas=self.vacia())), \
                mock.patch.object(g, "conectar_destino", side_effect=abrir):
            g.main(["--si-hay-pedido", "--config", "x"])
        self.assertEqual(n[0], 4)  # latido, copia, fallo_pedido y registro de error
        self.assertEqual(maximo[0], 1)  # nunca dos a la vez


class CopiaDiariaYManual(unittest.TestCase):
    def setUp(self):
        SLEEP.reset_mock()

    def test_espera_el_candado_hasta_15_segundos_y_despues_copia(self):
        cur = CursorFalso(lock_tras=5)
        codigo, (con,), _, _ = correr_main([cur])
        self.assertEqual(codigo, 0)
        self.assertEqual(SLEEP.call_count, 5)
        self.assertEqual(cur.intentos_lock, 6)
        con.commit.assert_called_once()

    def test_si_el_candado_sigue_ocupado_a_los_15_segundos_da_error_y_lo_registra(self):
        cur1, cur2 = CursorFalso(lock=False), CursorFalso()
        codigo, _, lo, _ = correr_main([cur1, cur2])
        self.assertEqual(codigo, 1)
        self.assertEqual(SLEEP.call_count, g.ESPERA_CANDADO_S)
        self.assertEqual(sum("advisory" in q for q in cur1.sqls()), g.ESPERA_CANDADO_S + 1)
        lo.assert_not_called()
        self.assertIn("otra copia corriendo", cur2.llamadas_registro()[0][4])

    def test_la_copia_diaria_consume_el_pedido_pendiente_en_su_transaccion(self):
        cur = CursorFalso(pedido=4)
        codigo, (con,), _, log = self.correr_con_log(cur)
        self.assertEqual(codigo, 0)
        sqls = cur.sqls()
        self.assertLess(next(i for i, q in enumerate(sqls) if "advisory" in q),
                        next(i for i, q in enumerate(sqls) if "tomar_pedido_sync" in q))
        self.assertIn("atiende el pedido", log)
        con.commit.assert_called_once()

    def test_sin_pedido_pendiente_la_copia_sigue_igual(self):
        cur = CursorFalso(pedido=None)
        codigo, _, _, log = self.correr_con_log(cur)
        self.assertEqual(codigo, 0)
        self.assertNotIn("atiende el pedido", log)
        self.assertTrue(any(q.startswith("TRUNCATE") for q in cur.sqls()))

    def test_sin_la_349_la_copia_manual_sigue_sin_pedidos(self):
        def fallar(sql, params):
            if "tomar_pedido_sync" in sql:
                raise ErrorPg("function does not exist", "42883")
        cur = CursorFalso(fallar=fallar)
        codigo, _, _, _ = self.correr_con_log(cur)
        self.assertEqual(codigo, 0)
        self.assertIn("ROLLBACK TO SAVEPOINT tomar_pedido", cur.sqls())
        self.assertTrue(any(q.startswith("TRUNCATE") for q in cur.sqls()))

    def test_una_copia_fallida_no_toca_los_intentos_del_pedido(self):
        cur1, cur2 = CursorFalso(pedido=4, copy_error=RuntimeError("x")), CursorFalso()
        codigo, _, _, _ = correr_main([cur1, cur2])
        self.assertEqual(codigo, 1)
        self.assertFalse(any("fallo_pedido" in q for q in cur1.sqls() + cur2.sqls()))

    def correr_con_log(self, cur):
        codigo, destinos, lo, log = correr_main([cur])
        return codigo, destinos, lo, log


class PruebaDestino(unittest.TestCase):
    def correr(self, cur):
        con = conexion_falsa(cur)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(g, "LOG_LOCAL", os.path.join(tmp, "log")), \
                mock.patch.object(g, "leer_archivo", return_value=dict(CFG)), \
                mock.patch.object(g, "leer_origen") as lo, \
                mock.patch.object(g, "conectar_destino", side_effect=[con]):
            codigo = g.main(["--prueba-destino", "--config", "x"])
            log = open(os.path.join(tmp, "log"), encoding="utf-8").read()
        lo.assert_not_called()
        return codigo, con, log

    def test_ok_no_escribe_nada(self):
        cur = CursorFalso()
        codigo, con, log = self.correr(cur)
        self.assertEqual(codigo, 0)
        con.commit.assert_not_called()
        self.assertIn("contraseña correctas", log)
        self.assertNotIn("AVISO", log)
        self.assertFalse(any(q.split()[0] in ("INSERT", "UPDATE", "DELETE", "TRUNCATE") for q in cur.sqls()))

    def test_sin_la_349_usa_select_1_y_avisa(self):
        def fallar(sql, params):
            if "ultimos_conteos_ok" in sql:
                raise ErrorPg("function does not exist", "42883")
        cur = CursorFalso(fallar=fallar)
        codigo, _, log = self.correr(cur)
        self.assertEqual(codigo, 0)
        self.assertIn("SELECT 1", cur.sqls())
        self.assertIn("falta aplicar la migración 349", log)

    def test_contrasena_mal_tipeada_falla_con_mensaje_claro(self):
        con = Exception('FATAL: password authentication failed for user "garro_sync"')
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(g, "LOG_LOCAL", os.path.join(tmp, "log")), \
                mock.patch.object(g, "leer_archivo", return_value=dict(CFG)), \
                mock.patch.object(g, "conectar_destino", side_effect=con):
            self.assertEqual(g.main(["--prueba-destino", "--config", "x"]), 1)
            self.assertIn("Supabase rechazó", open(os.path.join(tmp, "log"), encoding="utf-8").read())

    def test_no_se_combina_con_otros_modos(self):
        for extra in ("--forzar", "--si-hay-pedido", "--prueba"):
            with self.assertRaises(SystemExit):
                g.main(["--prueba-destino", extra])


class LogRotativo(unittest.TestCase):
    def test_rota_pasados_5_mb_y_deja_un_punto_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = os.path.join(tmp, "log")
            with open(ruta, "wb") as f:
                f.write(b"x" * (g.MAX_LOG_BYTES + 1))
            with mock.patch.object(g, "LOG_LOCAL", ruta):
                g.escribir_log("nuevo")
            self.assertEqual(open(ruta, encoding="utf-8").read(), "nuevo\n")
            self.assertEqual(os.path.getsize(ruta + ".1"), g.MAX_LOG_BYTES + 1)

    def test_no_rota_debajo_del_tope(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = os.path.join(tmp, "log")
            with mock.patch.object(g, "LOG_LOCAL", ruta):
                g.escribir_log("a")
                g.escribir_log("b")
            self.assertFalse(os.path.exists(ruta + ".1"))


if __name__ == "__main__":
    unittest.main()
