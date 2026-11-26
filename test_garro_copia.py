"""Tests de garro_copia.py que no necesitan base (unittest de la stdlib, sin psycopg2 ni red).

La copia de verdad (pg_dump + pg_restore) se prueba contra un PostgreSQL local descartable, fuera de este repo.
"""

import os
import tempfile
import unittest
from unittest import mock

import garro_copia as gp

CFG = {"GARRO_HOST": "127.0.0.1", "GARRO_PUERTO": "5432", "GARRO_BASE": "Gestion", "GARRO_USUARIO": "u",
       "GARRO_PASSWORD": "secreta"}


def medicion(**cambios):
    m = {"base": "Gestion", "tamano_bytes": 300 * 1024 * 1024, "encoding": "SQL_ASCII", "collate": "C", "ctype": "C",
         "directorio_datos": "/datos", "conexiones_abiertas": 3, "tablas_mas_grandes": [("x", 10)],
         "copias_existentes": [], "libre_disco_base_bytes": 10 * 1024 ** 3, "libre_disco_programa_bytes": 10 * 1024 ** 3,
         "necesario_bytes": 1100 * 1024 * 1024, "se_puede": True, "motivos": []}
    m.update(cambios)
    return m


class Nombre(unittest.TestCase):
    def test_validos(self):
        for n in ("gestion_prueba", "copia_2_prueba"):
            gp.validar_nombre(n, CFG)

    def test_invalidos(self):
        for n in ("", "Gestion", "gestion", "Gestion_prueba", "gestion prueba_prueba", 'x"; drop_prueba', "_prueba",
                  "1abc_prueba", "a" * 60 + "_prueba", "gestion_prueba; DROP DATABASE x"):
            with self.assertRaises(gp.ErrorCopia, msg=n):
                gp.validar_nombre(n, CFG)

    def test_no_puede_ser_la_base_real(self):
        with self.assertRaises(gp.ErrorCopia):
            gp.validar_nombre("gestion_prueba", {**CFG, "GARRO_BASE": "Gestion_Prueba"})


class Binarios(unittest.TestCase):
    def test_los_busca_en_la_carpeta_indicada_y_al_lado_de_los_datos(self):
        with tempfile.TemporaryDirectory() as raiz:
            datos, binarios = os.path.join(raiz, "data"), os.path.join(raiz, "bin")
            os.makedirs(datos), os.makedirs(binarios)
            self.assertIsNone(gp._binarios(CFG, datos))
            exe = ".exe" if os.name == "nt" else ""
            for n in ("pg_dump", "pg_restore"):
                open(os.path.join(binarios, n + exe), "w").close()
            self.assertEqual(set(gp._binarios(CFG, datos)), {"pg_dump", "pg_restore"})
            self.assertIsNotNone(gp._binarios({**CFG, "GARRO_PG_BIN": binarios}, None))
            self.assertIsNone(gp._binarios(CFG, None))


class Crear(unittest.TestCase):
    def test_no_arranca_si_la_medicion_dice_que_no_se_puede(self):
        with mock.patch.object(gp, "medir", return_value=medicion(se_puede=False, motivos=["falta disco"])), \
                mock.patch.object(gp, "_conectar") as conectar:
            with self.assertRaises(gp.ErrorCopia):
                gp.crear(CFG, "gestion_prueba")
        conectar.assert_not_called()

    def test_nombre_invalido_ni_mide(self):
        with mock.patch.object(gp, "medir") as medir:
            with self.assertRaises(gp.ErrorCopia):
                gp.crear(CFG, "Gestion")
        medir.assert_not_called()

    def _popen(self, codigo, stderr=b""):
        def fabrica(comando, env, stdout, stderr, creationflags):
            stderr.write(self._stderr)
            proceso = mock.Mock(returncode=codigo)
            proceso.wait.return_value = codigo
            proceso.poll.return_value = codigo
            return proceso
        self._stderr = stderr
        return mock.patch.object(gp.subprocess, "Popen", side_effect=fabrica)

    def test_la_clave_va_por_entorno_y_no_por_la_linea_de_comandos(self):
        with self._popen(0) as popen:
            gp._correr(["pg_dump", "-h", "127.0.0.1", "base"], CFG)
        (comando,), opciones = popen.call_args
        self.assertNotIn("secreta", " ".join(comando))
        self.assertEqual(opciones["env"]["PGPASSWORD"], "secreta")

    def test_un_comando_que_falla_no_muestra_su_salida(self):
        with self._popen(1, b"pg_dump: could not connect to 10.1.2.3"), mock.patch.object(gp.g, "escribir_log") as log:
            with self.assertRaises(gp.ErrorCopia) as e:
                gp._correr(["/x/pg_dump", "base"], CFG)
        self.assertNotIn("10.1.2.3", str(e.exception))
        self.assertIn("10.1.2.3", log.call_args[0][0])

    def test_si_el_disco_se_queda_sin_lugar_corta_y_mata_el_proceso(self):
        proceso = mock.Mock(returncode=None)
        proceso.wait.side_effect = [gp.subprocess.TimeoutExpired("x", 1), 0]
        proceso.poll.return_value = None
        with mock.patch.object(gp.subprocess, "Popen", return_value=proceso), \
                mock.patch.object(gp, "_libre", return_value=10 * 1024 * 1024):
            with self.assertRaises(gp.ErrorCopia):
                gp._correr(["pg_restore", "x"], CFG, vigilar=("/datos",))
        proceso.kill.assert_called_once()

    def test_si_se_corta_a_mano_mata_el_proceso(self):
        proceso = mock.Mock(returncode=None)
        proceso.wait.side_effect = [KeyboardInterrupt(), 0]
        proceso.poll.return_value = None
        with mock.patch.object(gp.subprocess, "Popen", return_value=proceso):
            with self.assertRaises(KeyboardInterrupt):
                gp._correr(["pg_restore", "x"], CFG)
        proceso.kill.assert_called_once()


class Versiones(unittest.TestCase):
    def test_rama(self):
        self.assertEqual(gp._rama("9.3.25"), "9.3")
        self.assertEqual(gp._rama("9.3"), "9.3")
        self.assertEqual(gp._rama("16.2"), "16")
        self.assertEqual(gp._rama("16.2 (Homebrew)"), "16")
        self.assertEqual(gp._rama(""), "")
        self.assertNotEqual(gp._rama("9.3.25"), gp._rama("9.6.1"))


class Informe(unittest.TestCase):
    def test_dice_si_se_puede_y_por_que_no(self):
        self.assertIn("Se puede copiar.", gp.informe(medicion()))
        texto = gp.informe(medicion(se_puede=False, motivos=["hace falta 1.1 GB libres"]))
        self.assertIn("NO se puede copiar", texto)
        self.assertIn("hace falta 1.1 GB libres", texto)

    def test_tamanos_legibles(self):
        self.assertEqual(gp._lindo(None), "desconocido")
        self.assertEqual(gp._lindo(512), "512 B")
        self.assertEqual(gp._lindo(300 * 1024 * 1024), "300.0 MB")
        self.assertEqual(gp._lindo(3 * 1024 ** 3), "3.0 GB")


if __name__ == "__main__":
    unittest.main()
