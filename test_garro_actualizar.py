"""Tests de la actualización remota (garro_actualizar.py): descargas falsas, sin red ni base."""

import contextlib
import hashlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

import actualizar_hashes as ah
import garro_actualizar as ga
import garro_comandos as gc
import garro_recuperar as gr
import garro_sync as g

AQUI = os.path.dirname(os.path.abspath(__file__))
V_VIEJA = "v9.0.0"
V_NUEVA = "v9.9.9"
PY_VERSION = "3.12.10"
PY_SHA = "ab" * 32


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def stub_sync(version):
    return ('import os, sys\nif "--version" in sys.argv:\n    print("garro_sync %s")\n'
            'elif os.environ.get("GARRO_SYNC_AUTOCHEQUEO") == "1":\n    print("AUTOCHEQUEO ARGV OK %s")\n' % (version, version)).encode()


def stub_escritor(version):
    return f'import sys\nif "--version" in sys.argv:\n    print("garro_escritor {version}")\n'.encode()


def stub_comandos(version, ok=True):
    cuerpo = f'print("AUTOCHEQUEO OK {version}")' if ok else 'print("AUTOCHEQUEO FALLO: base caida"); sys.exit(1)'
    return f'import sys\nif "--version" in sys.argv:\n    print("garro_comandos {version}")\nelse:\n    {cuerpo}\n'.encode()


def archivos_de(version, comandos_ok=True, extra_py=None):
    a = {"garro_sync.py": stub_sync(version), "garro_escritor.py": stub_escritor(version),
         "garro_comandos.py": stub_comandos(version, comandos_ok), "garro_recuperar.py": b"# recuperar: nunca se reemplaza\n",
         "requirements.txt": b"psycopg2-binary==0\n", "supabase-ca-2021.crt": b"CERT\n",
         "actualizar-ahora.bat": b"@echo off\r\n"}
    a.update(extra_py or {})
    return a


def instalador_de(version, archivos, python_version=PY_VERSION, python_sha=PY_SHA, tareas="1", repo_hashes=None):
    hashes = repo_hashes if repo_hashes is not None else {n: sha(b) for n, b in archivos.items()}
    bloque = "\r\n".join(f'    "{n}" = "{h}"' for n, h in sorted(hashes.items()))
    return (f'$Version         = "{version}"\r\n$Repo            = "x/y"\r\n$VersionTareas  = "{tareas}"\r\n'
            f'$PythonVersion   = "{python_version}"\r\n$PythonSha256    = "{python_sha}"\r\n'
            f"# BEGIN HASHES\r\n$HashesPaquete = @{{\r\n{bloque}\r\n}}\r\n# END HASHES\r\n").encode("ascii")


def zip_de(version, archivos, raiz=None, extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        r = raiz or f"diforte-garro-sync-{version[1:]}"
        for n, b in archivos.items():
            z.writestr(f"{r}/{n}", b)
        for n, b in (extra or {}).items():
            z.writestr(n, b)
    return buf.getvalue()


class Fabrica:
    """Una carpeta de instalación (versión vieja) y los dos downloads de una versión nueva."""

    def __init__(self, test, *, nuevos=None, **kw_instalador):
        self.dir = tempfile.mkdtemp(prefix="act-")
        test.addCleanup(shutil.rmtree, self.dir, True)
        self.viejos = archivos_de(V_VIEJA)
        for n, b in self.viejos.items():
            self._escribir(n, b)
        self._escribir("python-verificado.txt", f"version={PY_VERSION}\r\nsha256={PY_SHA}\r\n".encode())
        self._escribir("tareas-version.txt", b"1\r\n")
        self.nuevos = nuevos or archivos_de(V_NUEVA)
        self.instalador = instalador_de(V_NUEVA, self.nuevos, **kw_instalador)
        self.zip = zip_de(V_NUEVA, self.nuevos)
        self.pedidos = []

    def _escribir(self, n, b):
        with open(os.path.join(self.dir, n), "wb") as f:
            f.write(b)

    def descargar(self, url, maximo):
        self.pedidos.append(url)
        if url.endswith("install.ps1"):
            return self.instalador
        if url.endswith(".zip"):
            return self.zip
        raise AssertionError(url)

    def args(self):
        return {"version": V_NUEVA, "sha256_install": sha(self.instalador)}

    def estado(self):
        return {n: sha(open(os.path.join(self.dir, n), "rb").read()) for n in sorted(os.listdir(self.dir))
                if os.path.isfile(os.path.join(self.dir, n))}

    def correr(self, *, descargar=None, candado=None, version_python=PY_VERSION, args=None, plazo_s=None, version_actual=V_VIEJA):
        return ga.actualizar(args or self.args(), {}, aqui=self.dir, config=os.path.join(self.dir, ".env"),
                             python=sys.executable, correr=lambda argv, t, env=None: gc.correr_proceso(argv, t, self.dir, env),
                             descargar=descargar or self.descargar, candado=candado or contextlib.nullcontext,
                             version_actual=version_actual, version_python=version_python, plazo_s=plazo_s, comando_id=77)


class Exito(unittest.TestCase):
    def test_reemplaza_los_py_escribe_version_deja_respaldo_y_limpia_staging(self):
        f = Fabrica(self)
        texto = f.correr()
        self.assertIn(f"Actualizado de {V_VIEJA} a {V_NUEVA}", texto)
        for n in ("garro_sync.py", "garro_escritor.py", "garro_comandos.py"):
            self.assertEqual(open(os.path.join(f.dir, n), "rb").read(), f.nuevos[n])
        self.assertEqual(open(os.path.join(f.dir, "version.txt")).read().strip(), V_NUEVA)
        self.assertEqual(open(os.path.join(f.dir, ga.CARPETA_RESPALDO, "garro_sync.py"), "rb").read(), f.viejos["garro_sync.py"])
        self.assertFalse(os.path.exists(os.path.join(f.dir, ga.CARPETA_STAGING)))
        self.assertEqual([n for n in os.listdir(f.dir) if n.endswith(".nuevo")], [])

    def test_baja_el_install_y_el_zip_del_tag_pedido_no_de_main(self):
        f = Fabrica(self)
        f.correr()
        self.assertEqual(len(f.pedidos), 2)
        self.assertEqual(f.pedidos[0], f"https://raw.githubusercontent.com/{ga.REPO}/{V_NUEVA}/install.ps1")
        self.assertEqual(f.pedidos[1], f"https://github.com/{ga.REPO}/archive/refs/tags/{V_NUEVA}.zip")
        self.assertFalse(any("main" in u for u in f.pedidos))

    def test_la_huella_pedida_en_mayusculas_sirve(self):
        f = Fabrica(self)
        self.assertEqual(gc.validar("actualizar", {"version": V_NUEVA, "sha256_install": sha(f.instalador).upper()}, {})["sha256_install"],
                         sha(f.instalador))

    def test_un_py_nuevo_del_paquete_se_agrega_y_si_falla_se_borra(self):
        nuevos = archivos_de(V_NUEVA, extra_py={"garro_otro.py": b"x = 1\n"})
        f = Fabrica(self, nuevos=nuevos)
        f.correr()
        self.assertTrue(os.path.exists(os.path.join(f.dir, "garro_otro.py")))
        # mismo caso pero con autochequeo que falla: el archivo nuevo no queda
        malos = archivos_de(V_NUEVA, comandos_ok=False, extra_py={"garro_otro.py": b"x = 1\n"})
        f2 = Fabrica(self, nuevos=malos)
        antes = f2.estado()
        with self.assertRaises(ga.ErrorActualizar):
            f2.correr()
        self.assertEqual(f2.estado(), antes)
        self.assertFalse(os.path.exists(os.path.join(f2.dir, "garro_otro.py")))

    def test_ya_instalada_y_verificada_no_toca_nada(self):
        f = Fabrica(self)
        f.nuevos = f.viejos
        f.instalador = instalador_de(V_VIEJA, f.viejos)
        antes = f.estado()
        texto = ga.actualizar({"version": V_VIEJA, "sha256_install": sha(f.instalador)}, {}, aqui=f.dir, config="x", python=sys.executable,
                              correr=lambda a, t, env=None: (0, ""), descargar=f.descargar, candado=contextlib.nullcontext,
                              version_actual=V_VIEJA, version_python=PY_VERSION)
        self.assertIn("ya estaba instalada", texto)
        self.assertEqual(f.estado(), antes)
        self.assertEqual(len(f.pedidos), 1)  # ni siquiera bajó el zip


class SinTocarNada(unittest.TestCase):
    def rechaza(self, f, texto, **kw):
        antes = f.estado()
        with self.assertRaises(ga.ErrorActualizar) as c:
            f.correr(**kw)
        self.assertIn(texto, str(c.exception))
        self.assertEqual(f.estado(), antes)
        self.assertFalse(os.path.exists(os.path.join(f.dir, ga.CARPETA_STAGING)))
        self.assertFalse(os.path.exists(os.path.join(f.dir, ga.CARPETA_RESPALDO)))
        return c.exception

    def test_huella_del_install_distinta(self):
        f = Fabrica(self)
        args = {"version": V_NUEVA, "sha256_install": "0" * 64}
        self.rechaza(f, "NO coincide", args=args)
        self.assertEqual(len(f.pedidos), 1, "con la huella mala no se baja el zip")

    def test_el_install_dice_otra_version(self):
        f = Fabrica(self)
        f.instalador = instalador_de("v9.9.8", f.nuevos)
        self.rechaza(f, "dice v9.9.8")

    def test_un_archivo_del_zip_con_huella_mala(self):
        f = Fabrica(self)
        f.zip = zip_de(V_NUEVA, {**f.nuevos, "garro_escritor.py": b"print('cambiado')\n"})
        self.rechaza(f, "garro_escritor.py del zip NO coincide")

    def test_falta_un_archivo_en_el_zip(self):
        f = Fabrica(self)
        sin = dict(f.nuevos)
        del sin["garro_sync.py"]
        f.zip = zip_de(V_NUEVA, sin)
        self.rechaza(f, "falta garro_sync.py")

    def test_zip_sin_raiz_unica_o_dañado(self):
        f = Fabrica(self)
        f.zip = zip_de(V_NUEVA, f.nuevos, extra={"otra-raiz/x.py": b"1"})
        self.rechaza(f, "única carpeta raíz")
        f.zip = b"no es un zip"
        self.rechaza(f, "dañado")

    def test_cambia_el_python(self):
        for kw in ({"python_version": "3.13.0"}, {"python_sha": "cd" * 32}):
            f = Fabrica(self, **kw)
            e = self.rechaza(f, ga.REQUIERE_INSTALAR)
            self.assertIn("cambia el Python", str(e))

    def test_el_python_en_uso_no_es_el_del_tag(self):
        f = Fabrica(self)
        self.rechaza(f, ga.REQUIERE_INSTALAR, version_python="3.11.0")

    def test_sin_marca_de_python_no_se_puede_comprobar(self):
        f = Fabrica(self)
        os.remove(os.path.join(f.dir, "python-verificado.txt"))
        self.rechaza(f, ga.REQUIERE_INSTALAR)

    def test_cambian_las_tareas(self):
        f = Fabrica(self, tareas="2")
        e = self.rechaza(f, ga.REQUIERE_INSTALAR)
        self.assertIn("tareas programadas", str(e))

    def test_sin_version_de_tareas_instalada(self):
        f = Fabrica(self)
        os.remove(os.path.join(f.dir, "tareas-version.txt"))
        self.rechaza(f, ga.REQUIERE_INSTALAR)

    def test_el_install_del_tag_no_define_version_de_tareas(self):
        f = Fabrica(self)
        f.instalador = f.instalador.replace(b'$VersionTareas  = "1"\r\n', b"")
        self.rechaza(f, "VersionTareas")

    def test_cambia_el_certificado_las_dependencias_o_un_archivo_que_no_es_python(self):
        for nombre in ("supabase-ca-2021.crt", "requirements.txt", "actualizar-ahora.bat"):
            nuevos = archivos_de(V_NUEVA)
            nuevos[nombre] = b"otra cosa\n"
            f = Fabrica(self, nuevos=nuevos)
            e = self.rechaza(f, ga.REQUIERE_INSTALAR)
            self.assertIn(nombre, str(e))

    def test_un_archivo_no_python_nuevo_tambien_requiere_instalar(self):
        nuevos = archivos_de(V_NUEVA, extra_py={"datos.json": b"{}"})
        f = Fabrica(self, nuevos=nuevos)
        self.rechaza(f, "datos.json")

    def test_instalador_mal_formado(self):
        f = Fabrica(self)
        for malo in (b"sin nada", "$Version = \"v9.9.9\"\r\n# acentos \xc3\xa1".encode("latin-1", "ignore") + b"\xc3\xa1"):
            f.instalador = malo
            with self.assertRaises(ga.ErrorActualizar):
                f.correr(args={"version": V_NUEVA, "sha256_install": sha(malo)})

    def test_nombres_peligrosos_en_las_huellas(self):
        for nombre in ("../x.py", "a/b.py", "..\\x.py", ".oculto.py", "x.py/.."):
            f = Fabrica(self)
            hashes = {n: sha(b) for n, b in f.nuevos.items()}
            hashes[nombre] = "a" * 64
            f.instalador = instalador_de(V_NUEVA, f.nuevos, repo_hashes=hashes)
            self.rechaza(f, "")

    def test_huellas_sin_garro_comandos(self):
        f = Fabrica(self)
        hashes = {n: sha(b) for n, b in f.nuevos.items() if n != "garro_comandos.py"}
        f.instalador = instalador_de(V_NUEVA, f.nuevos, repo_hashes=hashes)
        self.rechaza(f, "garro_comandos.py")

    def test_red_caida_al_bajar_el_install(self):
        f = Fabrica(self)
        def cae(url, maximo):
            raise OSError("sin red")
        e = self.rechaza(f, "no se pudo bajar el install.ps1", descargar=cae)
        self.assertNotIn("sin red", str(e))

    def test_red_caida_a_mitad_al_bajar_el_zip_no_deja_nada(self):
        f = Fabrica(self)
        def cae_en_el_zip(url, maximo):
            if url.endswith(".zip"):
                raise TimeoutError("se cortó")
            return f.descargar(url, maximo)
        self.rechaza(f, "no se pudo bajar el zip", descargar=cae_en_el_zip)

    def test_hay_una_copia_en_curso(self):
        f = Fabrica(self)
        @contextlib.contextmanager
        def ocupado(cfg):
            raise ga.ErrorActualizar("hay una copia en curso: no se actualiza ahora")
            yield
        self.rechaza(f, "copia en curso", candado=ocupado)


class Restauracion(unittest.TestCase):
    def test_autochequeo_que_falla_restaura_todo_y_lo_dice(self):
        f = Fabrica(self, nuevos=archivos_de(V_NUEVA, comandos_ok=False))
        antes = f.estado()
        with self.assertRaises(ga.ErrorActualizar) as c:
            f.correr()
        msg = str(c.exception)
        self.assertIn("FALLÓ", msg)
        self.assertIn(f"se restauró {V_VIEJA}", msg)
        self.assertIn("base caida", msg)
        self.assertIn("Chequeo de la restauración: OK", msg)
        # los archivos vuelven EXACTOS; solo queda de más la carpeta del respaldo
        despues = f.estado()
        self.assertEqual(despues, antes)
        self.assertFalse(os.path.exists(os.path.join(f.dir, "version.txt")))

    def test_si_la_restauracion_tampoco_pasa_el_chequeo_se_informa(self):
        f = Fabrica(self, nuevos=archivos_de(V_NUEVA, comandos_ok=False))
        viejo = archivos_de(V_VIEJA, comandos_ok=False)  # el viejo también falla el autochequeo
        f.viejos = viejo
        f._escribir("garro_comandos.py", viejo["garro_comandos.py"])
        with self.assertRaises(ga.ErrorActualizar) as c:
            f.correr()
        self.assertIn("Chequeo de la restauración: FALLÓ", str(c.exception))

    def test_falla_el_reemplazo_a_mitad_y_se_restaura(self):
        nuevos = archivos_de(V_NUEVA, extra_py={"garro_otro.py": b"x = 1\n"})
        f = Fabrica(self, nuevos=nuevos)
        antes = f.estado()
        originales = gr.escribir_atomico
        llamadas = []

        def falla_en_la_tercera(origen, destino):
            if destino.endswith(".py") and os.path.dirname(origen).endswith(ga.CARPETA_STAGING):
                llamadas.append(destino)
                if len(llamadas) == 3:
                    raise PermissionError("archivo en uso")
            return originales(origen, destino)

        with mock.patch.object(gr, "escribir_atomico", falla_en_la_tercera):
            with self.assertRaises(ga.ErrorActualizar) as c:
                f.correr()
        self.assertIn("se restauró la versión anterior", str(c.exception))
        self.assertEqual(f.estado(), antes)

    def test_si_ni_restaurar_se_puede_el_mensaje_lo_dice_fuerte(self):
        nuevos = archivos_de(V_NUEVA, comandos_ok=False)
        f = Fabrica(self, nuevos=nuevos)
        with mock.patch.object(gr, "restaurar_archivos", return_value=["garro_sync.py: PermissionError"]):
            with self.assertRaises(ga.ErrorActualizar) as c:
                f.correr()
        self.assertIn("INCOMPLETA", str(c.exception))
        self.assertIn("instalar en el equipo", str(c.exception))

    def test_un_autochequeo_que_revienta_cuenta_como_fallo(self):
        f = Fabrica(self)
        antes = f.estado()
        with mock.patch.object(ga, "autochequeo", side_effect=[RuntimeError("x"), (True, "ok")]):
            with self.assertRaises(ga.ErrorActualizar) as c:
                f.correr()
        self.assertIn("no pudo correr", str(c.exception))
        self.assertEqual(f.estado(), antes)

    def test_el_autochequeo_exige_la_version_pedida(self):
        f = Fabrica(self)
        # los scripts nuevos dicen otra versión que la pedida: el chequeo no la acepta
        otro = archivos_de("v0.0.1")
        otro["garro_comandos.py"] = stub_comandos(V_NUEVA)
        f.nuevos = otro
        f.instalador = instalador_de(V_NUEVA, otro)
        f.zip = zip_de(V_NUEVA, otro)
        antes = f.estado()
        with self.assertRaises(ga.ErrorActualizar):
            f.correr()
        self.assertEqual(f.estado(), antes)


class Corte(BaseException):
    """Simula que el proceso muere (corte de luz, taskkill): no lo atrapa ningún `except Exception`."""


class PlazoYMarca(unittest.TestCase):
    def marca(self, f):
        return os.path.join(f.dir, gr.MARCA_EN_CURSO)

    def test_sin_plazo_suficiente_no_toca_nada_ni_baja_nada(self):
        f = Fabrica(self)
        antes = f.estado()
        with self.assertRaises(ga.ErrorActualizar) as c:
            f.correr(plazo_s=ga.PEOR_CASO_S - 1)
        self.assertIn("No hay tiempo", str(c.exception))
        self.assertEqual(f.pedidos, [])
        self.assertEqual(f.estado(), antes)
        f.correr(plazo_s=ga.PEOR_CASO_S)  # justo alcanza

    def test_el_peor_caso_cabe_en_los_28_minutos_de_la_corrida(self):
        self.assertLess(ga.PEOR_CASO_S, 28 * 60)
        self.assertEqual(gc.TIMEOUTS["actualizar"], ga.PEOR_CASO_S)
        self.assertEqual(ga.PEOR_CASO_S, 2 * ga.TOPE_DESCARGA + 2 * (4 * ga.TOPE_VERSION + ga.TOPE_AUTOCHEQUEO) + ga.MARGEN_S)

    def test_una_descarga_sin_tiempo_no_empieza(self):
        f = Fabrica(self)
        relojes = iter([0.0, 10_000.0])
        with mock.patch.object(ga.time, "monotonic", side_effect=lambda: next(relojes, 10_000.0)):
            with self.assertRaises(ga.ErrorActualizar) as c:
                f.correr(plazo_s=ga.PEOR_CASO_S)
        self.assertIn("no hay tiempo para bajar", str(c.exception))
        self.assertEqual(f.pedidos, [])

    def test_el_autochequeo_respeta_el_plazo_absoluto(self):
        ok, det = ga.autochequeo("/x", "v1", "py", "cfg", lambda *a: self.fail("no debía correr"), hasta=ga.time.monotonic() - 1)
        self.assertFalse(ok)
        self.assertIn("sin tiempo", det)

    def test_el_autochequeo_acota_cada_paso_al_minimo_entre_su_tope_y_lo_que_queda(self):
        tiempos = []
        ga.autochequeo("/x", "v1", "py", "cfg", lambda argv, t, env=None: tiempos.append(t) or (0, "garro_sync v1 garro_escritor v1 AUTOCHEQUEO ARGV OK v1 AUTOCHEQUEO OK v1"),
                       hasta=ga.time.monotonic() + 15)
        self.assertTrue(all(1 <= t <= 15 for t in tiempos), tiempos)
        tiempos.clear()
        ga.autochequeo("/x", "v1", "py", "cfg", lambda argv, t, env=None: tiempos.append(t) or (0, "garro_sync v1 garro_escritor v1 AUTOCHEQUEO ARGV OK v1 AUTOCHEQUEO OK v1"))
        self.assertEqual(tiempos, [ga.TOPE_VERSION] * 4 + [ga.TOPE_AUTOCHEQUEO])

    def test_el_autochequeo_prueba_el_argv_exacto_de_las_dos_tareas_con_la_variable(self):
        vistos = []
        ga.autochequeo("/x", "v1", "py", "cfg", lambda argv, t, env=None: vistos.append((argv[2:], env)) or (0, "garro_sync v1 garro_escritor v1 AUTOCHEQUEO ARGV OK v1 AUTOCHEQUEO OK v1"))
        tareas = [(a, e) for a, e in vistos if e]
        self.assertEqual([a for a, _ in tareas], [list(x) for x in g.ARGV_TAREAS])
        self.assertTrue(all(e == {"GARRO_SYNC_AUTOCHEQUEO": "1"} for _, e in tareas))

    def test_la_marca_existe_durante_el_reemplazo_con_lo_necesario_y_se_borra_al_terminar_bien(self):
        f = Fabrica(self)
        vistas = []
        original = ga.autochequeo
        def espia(*a, **k):
            vistas.append(json_marca(f))
            return original(*a, **k)
        with mock.patch.object(ga, "autochequeo", espia):
            f.correr()
        m = vistas[0]
        self.assertEqual((m["anterior"], m["nueva"], m["comando_id"]), (V_VIEJA, V_NUEVA, 77))
        self.assertEqual(sorted(m["archivos"]), ["garro_comandos.py", "garro_escritor.py", "garro_sync.py"])
        self.assertTrue(all(m["previos"].values()))
        self.assertFalse(os.path.exists(self.marca(f)))

    def test_si_falla_el_autochequeo_y_se_restaura_no_queda_la_marca(self):
        f = Fabrica(self, nuevos=archivos_de(V_NUEVA, comandos_ok=False))
        with self.assertRaises(ga.ErrorActualizar):
            f.correr()
        self.assertFalse(os.path.exists(self.marca(f)))

    def test_corte_entre_reemplazos_la_marca_queda_y_recuperar_restaura_todo(self):
        f = Fabrica(self)
        antes = f.estado()
        mtimes = {n: os.path.getmtime(os.path.join(f.dir, n)) for n in ("garro_sync.py", "garro_escritor.py", "garro_comandos.py")}
        original = gr.escribir_atomico
        cuenta = []
        def corta_en_la_segunda(origen, destino):
            if os.path.dirname(origen).endswith(ga.CARPETA_STAGING):
                cuenta.append(destino)
                if len(cuenta) == 2:
                    raise Corte()
            return original(origen, destino)
        with mock.patch.object(gr, "escribir_atomico", corta_en_la_segunda):
            with self.assertRaises(Corte):
                f.correr()
        self.assertTrue(os.path.exists(self.marca(f)), "el corte deja la marca")
        self.assertNotEqual(f.estado(), antes, "quedó mezclado: un archivo nuevo y el resto viejo")
        self.assertEqual(gr.recuperar(f.dir), "recuperada")
        self.assertEqual(sin_nota(f.estado()), antes)
        self.assertFalse(os.path.exists(self.marca(f)))
        for n, m in mtimes.items():  # copy2: se conserva la fecha de modificación
            self.assertEqual(os.path.getmtime(os.path.join(f.dir, n)), m, n)
        nota = gr.leer_recuperada(f.dir)
        self.assertEqual((nota["comando_id"], nota["anterior"], nota["nueva"]), (77, V_VIEJA, V_NUEVA))
        self.assertIn("se cortó a mitad", nota["mensaje"])

    def test_corte_durante_el_autochequeo_la_marca_queda_y_recuperar_restaura_todo(self):
        f = Fabrica(self)
        antes = f.estado()
        with mock.patch.object(ga, "autochequeo", side_effect=Corte()):
            with self.assertRaises(Corte):
                f.correr()
        self.assertTrue(os.path.exists(self.marca(f)))
        self.assertEqual(open(os.path.join(f.dir, "garro_sync.py"), "rb").read(), f.nuevos["garro_sync.py"])
        self.assertEqual(gr.recuperar(f.dir), "recuperada")
        self.assertEqual(sin_nota(f.estado()), antes)

    def test_un_archivo_nuevo_sin_version_previa_se_borra_al_recuperar(self):
        f = Fabrica(self, nuevos=archivos_de(V_NUEVA, extra_py={"garro_otro.py": b"x = 1\n"}))
        with mock.patch.object(ga, "autochequeo", side_effect=Corte()):
            with self.assertRaises(Corte):
                f.correr()
        self.assertTrue(os.path.exists(os.path.join(f.dir, "garro_otro.py")))
        gr.recuperar(f.dir)
        self.assertFalse(os.path.exists(os.path.join(f.dir, "garro_otro.py")))

    def test_con_la_marca_puesta_un_pedido_nuevo_no_pisa_el_respaldo(self):
        f = Fabrica(self)
        with mock.patch.object(ga, "autochequeo", side_effect=Corte()):
            with self.assertRaises(Corte):
                f.correr()
        respaldo = os.path.join(f.dir, ga.CARPETA_RESPALDO)
        antes = {n: sha(open(os.path.join(respaldo, n), "rb").read()) for n in os.listdir(respaldo)}
        self.assertEqual(antes["garro_sync.py"], sha(f.viejos["garro_sync.py"]))
        f2 = f
        with self.assertRaises(ga.ErrorActualizar) as c:
            f2.correr()  # otro pedido, ahora con los archivos nuevos a medias en disco
        self.assertIn("sin cerrar", str(c.exception))
        despues = {n: sha(open(os.path.join(respaldo, n), "rb").read()) for n in os.listdir(respaldo)}
        self.assertEqual(antes, despues)
        self.assertEqual(f.pedidos.count(f.pedidos[0]), 1, "ni siquiera bajó nada")
        # y la recuperación sigue pudiendo restaurar desde ese respaldo intacto
        self.assertEqual(gr.recuperar(f.dir), "recuperada")
        self.assertEqual(open(os.path.join(f.dir, "garro_sync.py"), "rb").read(), f.viejos["garro_sync.py"])

    def test_recuperar_no_hace_nada_sin_marca_ni_si_otro_proceso_esta_actualizando(self):
        f = Fabrica(self)
        self.assertIsNone(gr.recuperar(f.dir))
        with mock.patch.object(ga, "autochequeo", side_effect=Corte()):
            with self.assertRaises(Corte):
                f.correr()
        lock = gr.tomar_lock(os.path.join(f.dir, gr.LOCK_COMANDOS))  # «otro proceso» actualizando
        try:
            self.assertIsNone(gr.recuperar(f.dir))
            self.assertTrue(os.path.exists(self.marca(f)))
            self.assertEqual(open(os.path.join(f.dir, "garro_sync.py"), "rb").read(), f.nuevos["garro_sync.py"])
        finally:
            gr.soltar_lock(lock)
        self.assertEqual(gr.recuperar(f.dir), "recuperada")

    def test_si_la_restauracion_no_puede_la_marca_queda_para_reintentar(self):
        f = Fabrica(self)
        with mock.patch.object(ga, "autochequeo", side_effect=Corte()):
            with self.assertRaises(Corte):
                f.correr()
        os.remove(os.path.join(f.dir, ga.CARPETA_RESPALDO, "garro_sync.py"))  # el respaldo se perdió
        self.assertEqual(gr.recuperar(f.dir), "incompleta")
        self.assertTrue(os.path.exists(self.marca(f)))
        self.assertEqual(open(os.path.join(f.dir, "garro_sync.py"), "rb").read(), f.nuevos["garro_sync.py"], "no se borra un archivo que existía")

    def test_marca_ilegible_restaura_todo_lo_que_hay_en_el_respaldo(self):
        f = Fabrica(self)
        with mock.patch.object(ga, "autochequeo", side_effect=Corte()):
            with self.assertRaises(Corte):
                f.correr()
        open(self.marca(f), "w").write("{no es json")
        self.assertEqual(gr.recuperar(f.dir), "recuperada")
        self.assertEqual(open(os.path.join(f.dir, "garro_sync.py"), "rb").read(), f.viejos["garro_sync.py"])

    def test_se_borra_el_bytecode_de_lo_tocado_al_actualizar_y_al_restaurar(self):
        f = Fabrica(self)
        cache = os.path.join(f.dir, "__pycache__")
        os.makedirs(cache)
        pyc = [os.path.join(cache, n) for n in ("garro_sync.cpython-312.pyc", "garro_escritor.cpython-312.pyc")]
        otro = os.path.join(cache, "relevar_compras.cpython-312.pyc")
        for r in pyc + [otro]:
            open(r, "w").close()
        f.correr()
        self.assertFalse(any(os.path.exists(r) for r in pyc))
        self.assertTrue(os.path.exists(otro), "solo se borra el de los módulos tocados")
        for r in pyc:
            open(r, "w").close()
        f2 = Fabrica(self, nuevos=archivos_de(V_NUEVA, comandos_ok=False))
        cache2 = os.path.join(f2.dir, "__pycache__")
        os.makedirs(cache2)
        open(os.path.join(cache2, "garro_sync.cpython-312.pyc"), "w").close()
        with self.assertRaises(ga.ErrorActualizar):
            f2.correr()
        self.assertFalse(os.path.exists(os.path.join(cache2, "garro_sync.cpython-312.pyc")))

    def test_garro_recuperar_nunca_se_reemplaza_a_distancia(self):
        nuevos = archivos_de(V_NUEVA)
        nuevos["garro_recuperar.py"] = b"# otra version\n"
        f = Fabrica(self, nuevos=nuevos)
        antes = f.estado()
        with self.assertRaises(ga.ErrorActualizar) as c:
            f.correr()
        self.assertIn(ga.REQUIERE_INSTALAR, str(c.exception))
        self.assertIn("garro_recuperar.py", str(c.exception))
        self.assertEqual(f.estado(), antes)

    def test_no_se_baja_de_version_a_distancia(self):
        f = Fabrica(self)
        antes = f.estado()
        with self.assertRaises(ga.ErrorActualizar) as c:
            f.correr(version_actual="v10.0.0")  # pedida v9.9.9 < instalada v10.0.0
        self.assertIn("bajar de versión", str(c.exception))
        self.assertIn(ga.REQUIERE_INSTALAR, str(c.exception))
        self.assertEqual((f.pedidos, f.estado()), ([], antes))
        f.correr(version_actual="v9.9.8")  # subir una menor sigue andando

    def test_el_candado_de_la_copia_se_suelta_antes_del_autochequeo(self):
        f = Fabrica(self)
        eventos = []
        @contextlib.contextmanager
        def candado(cfg):
            eventos.append("toma")
            yield
            eventos.append("suelta")
        original = ga.autochequeo
        def espia(*a, **k):
            eventos.append("autochequeo")
            return original(*a, **k)
        with mock.patch.object(ga, "autochequeo", espia):
            f.correr(candado=candado)
        self.assertEqual(eventos, ["toma", "suelta", "autochequeo"])

    def test_la_descarga_tiene_un_tope_de_tiempo_total(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.side_effect = [b"x" * 10, b"x" * 10, b""]
        reloj = iter([0.0, 1.0, 1000.0, 1000.0])
        with mock.patch("urllib.request.urlopen", return_value=resp), \
                mock.patch.object(ga.time, "monotonic", side_effect=lambda: next(reloj)):
            with self.assertRaises(ga.ErrorActualizar) as c:
                ga.descargar_https("https://x/y", 1000, 90)
        self.assertIn("tope", str(c.exception))

    def test_n3_si_no_se_puede_borrar_la_marca_al_final_queda_cerrada_y_la_recuperacion_no_restaura(self):
        f = Fabrica(self)
        with mock.patch.object(gr, "borrar_marca"):
            f.correr()
        m = json_marca(f)
        self.assertEqual(m["estado"], "ok")
        self.assertFalse(gr.marca_sin_resolver(f.dir))
        self.assertIsNone(gr.recuperar(f.dir))
        for n in ("garro_sync.py", "garro_escritor.py", "garro_comandos.py"):
            self.assertEqual(open(os.path.join(f.dir, n), "rb").read(), f.nuevos[n], "no se restauró una versión buena")
        self.assertFalse(os.path.exists(self.marca(f)), "la recuperación borra la marca cerrada")
        self.assertIsNone(gr.leer_recuperada(f.dir))

    def test_n3_una_marca_cerrada_que_sigue_ahi_no_frena_un_pedido_nuevo(self):
        f = Fabrica(self)
        with mock.patch.object(gr, "borrar_marca"):
            f.correr()
        f2 = Fabrica(self)
        shutil.copy(self.marca(f), os.path.join(f2.dir, gr.MARCA_EN_CURSO))
        f2.correr()  # no levanta «sin cerrar»

    def test_n1_si_la_marca_no_se_puede_borrar_despues_de_restaurar_es_incompleta(self):
        f = Fabrica(self)
        with mock.patch.object(ga, "autochequeo", side_effect=Corte()):
            with self.assertRaises(Corte):
                f.correr()
        with mock.patch.object(gr, "borrar_marca"):
            self.assertEqual(gr.recuperar(f.dir), "incompleta")
        self.assertTrue(os.path.exists(self.marca(f)))

    def test_n4_la_marca_lleva_la_huella_de_cada_archivo_respaldado(self):
        f = Fabrica(self)
        vistas = []
        original = ga.autochequeo
        def espia(*a, **k):
            vistas.append(json_marca(f))
            return original(*a, **k)
        with mock.patch.object(ga, "autochequeo", espia):
            f.correr()
        self.assertEqual(vistas[0]["sha256"], {n: sha(f.viejos[n]) for n in vistas[0]["archivos"]})

    def test_n4_un_respaldo_que_no_coincide_no_se_restaura_y_no_toca_los_archivos_actuales(self):
        f = Fabrica(self)
        with mock.patch.object(ga, "autochequeo", side_effect=Corte()):
            with self.assertRaises(Corte):
                f.correr()
        actuales = {n: open(os.path.join(f.dir, n), "rb").read() for n in ("garro_sync.py", "garro_escritor.py", "garro_comandos.py")}
        with open(os.path.join(f.dir, ga.CARPETA_RESPALDO, "garro_escritor.py"), "ab") as fh:
            fh.write(b"# corrupto\n")
        self.assertEqual(gr.recuperar(f.dir), "incompleta")
        for n, b in actuales.items():
            self.assertEqual(open(os.path.join(f.dir, n), "rb").read(), b, n)
        self.assertTrue(os.path.exists(self.marca(f)))
        self.assertIsNone(gr.leer_recuperada(f.dir))

    def test_n4_se_fuerza_a_disco_el_respaldo_los_nuevos_antes_de_la_marca_y_la_marca(self):
        f = Fabrica(self)
        eventos = []
        original_fsync, original_marca = gr.fsync_archivo, gr.escribir_marca
        def fsync(ruta):
            eventos.append(("fsync", os.path.relpath(ruta, f.dir)))
            return original_fsync(ruta)
        def marca(aqui, datos):
            eventos.append(("marca", datos.get("estado", "en curso")))
            return original_marca(aqui, datos)
        with mock.patch.object(gr, "fsync_archivo", fsync), mock.patch.object(gr, "escribir_marca", marca):
            f.correr()
        i = eventos.index(("marca", "en curso"))
        antes = [e[1] for e in eventos[:i] if e[0] == "fsync"]
        for n in ("garro_sync.py", "garro_escritor.py", "garro_comandos.py"):
            self.assertIn(os.path.join(ga.CARPETA_RESPALDO, n), antes)   # respaldo
            self.assertIn(os.path.join(ga.CARPETA_STAGING, n), antes)    # nuevos, ya en disco antes de la marca


def sin_nota(estado):
    return {n: h for n, h in estado.items() if n not in (gr.MARCA_RECUPERADA, gr.LOCK_COMANDOS, "garro_sync.log")}


def json_marca(f):
    import json
    with open(os.path.join(f.dir, gr.MARCA_EN_CURSO), encoding="utf-8") as fh:
        return json.load(fh)


def _git(*args):
    import subprocess
    r = subprocess.run(["git", "-C", AQUI, *args], capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
    if r.returncode != 0:
        raise unittest.SkipTest("sin git o sin el tag v1.10.0 en este checkout")
    return r.stdout


class DeLaV1_10_0ALaV1_10_1(unittest.TestCase):
    """La actualización REMOTA de la v1.10.0 (la del tag) a esta versión, con los archivos reales de las dos."""

    VIEJA = "v1.10.0"

    def setUp(self):
        self.instalador_viejo = _git("show", f"{self.VIEJA}:install.ps1").decode("ascii")
        self.viejo = ga.leer_instalador(self.instalador_viejo)
        self.instalador_nuevo = open(os.path.join(AQUI, "install.ps1"), "rb").read()
        self.nuevo = ga.leer_instalador(self.instalador_nuevo.decode("ascii"))
        if self.nuevo["version"] == self.VIEJA:
            self.skipTest("esta versión es la v1.10.0")
        self.dir = tempfile.mkdtemp(prefix="act-real-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.archivos_viejos = {n: _git("show", f"{self.VIEJA}:{n}") for n in self.viejo["hashes"]}
        for n, b in self.archivos_viejos.items():
            self.assertEqual(sha(b), self.viejo["hashes"][n], f"{n} del tag no coincide con sus huellas")
            with open(os.path.join(self.dir, n), "wb") as f:
                f.write(b)
        with open(os.path.join(self.dir, "python-verificado.txt"), "w") as f:
            f.write(f"version={self.viejo['python_version']}\r\nsha256={self.viejo['python_sha256']}\r\n")
        with open(os.path.join(self.dir, "tareas-version.txt"), "w") as f:
            f.write(self.viejo["version_tareas"] + "\r\n")
        self.archivos_nuevos = {n: open(os.path.join(AQUI, n), "rb").read() for n in self.nuevo["hashes"]}

    def test_lo_unico_que_cambia_son_archivos_py_y_no_cambian_python_tareas_ni_garro_recuperar(self):
        v, n = self.viejo, self.nuevo
        self.assertEqual((v["python_version"], v["python_sha256"], v["version_tareas"]), (n["python_version"], n["python_sha256"], n["version_tareas"]))
        for nombre in n["hashes"]:
            if not nombre.endswith(".py"):
                self.assertEqual(n["hashes"][nombre], v["hashes"].get(nombre), f"cambió {nombre}, que no es un .py")
        self.assertEqual(n["hashes"]["garro_recuperar.py"], v["hashes"]["garro_recuperar.py"])
        self.assertTrue(set(v["hashes"]) <= set(n["hashes"]), "no se puede sacar un archivo del paquete")

    def test_el_actualizar_remoto_la_acepta_y_deja_el_paquete_andando(self):
        zip_bytes = zip_de(self.nuevo["version"], self.archivos_nuevos)
        pedidos = []
        def descargar(url, maximo):
            pedidos.append(url)
            return self.instalador_nuevo if url.endswith("install.ps1") else zip_bytes
        antes_recuperar = open(os.path.join(self.dir, "garro_recuperar.py"), "rb").read()
        with mock.patch.object(ga, "autochequeo", return_value=(True, "ok")):
            texto = ga.actualizar({"version": self.nuevo["version"], "sha256_install": sha(self.instalador_nuevo)}, {}, aqui=self.dir,
                                  config=os.path.join(self.dir, ".env"), python=sys.executable, correr=lambda a, t, e=None: (0, ""),
                                  descargar=descargar, candado=contextlib.nullcontext, version_actual=self.VIEJA,
                                  version_python=self.viejo["python_version"], plazo_s=ga.PEOR_CASO_S, comando_id=1)
        self.assertIn(f"Actualizado de {self.VIEJA} a {self.nuevo['version']}", texto)
        cambiados = sorted(n for n in self.nuevo["hashes"] if self.archivos_viejos.get(n) != self.archivos_nuevos[n])
        self.assertTrue(cambiados and all(n.endswith(".py") for n in cambiados), cambiados)
        for n in self.nuevo["hashes"]:
            self.assertEqual(open(os.path.join(self.dir, n), "rb").read(), self.archivos_nuevos[n], n)
        self.assertEqual(open(os.path.join(self.dir, "garro_recuperar.py"), "rb").read(), antes_recuperar)
        self.assertFalse(os.path.exists(os.path.join(self.dir, gr.MARCA_EN_CURSO)))
        self.assertEqual(sha(open(os.path.join(self.dir, "supabase-ca-2021.crt"), "rb").read()), self.nuevo["hashes"]["supabase-ca-2021.crt"])
        # y el código nuevo, ya instalado, arranca de verdad: --version y el argv EXACTO de las dos tareas importando todo
        codigo, salida = gc.correr_proceso([sys.executable, os.path.join(self.dir, "garro_sync.py"), "--version"], 60, self.dir)
        self.assertEqual((codigo, salida.strip()), (0, f"garro_sync {self.nuevo['version']}"))
        for args_tarea in g.ARGV_TAREAS:
            codigo, salida = gc.correr_proceso([sys.executable, os.path.join(self.dir, "garro_sync.py"), *args_tarea], 60, self.dir,
                                                {"GARRO_SYNC_AUTOCHEQUEO": "1"})
            self.assertEqual((codigo, salida.strip()), (0, f"AUTOCHEQUEO ARGV OK {self.nuevo['version']}"), args_tarea)
        self.assertEqual(len(pedidos), 2)


class Candado(unittest.TestCase):
    def conexion(self, toma: bool):
        con = mock.MagicMock()
        cur = con.cursor.return_value
        cur.fetchone.return_value = (toma,)
        return con, cur

    def test_toma_y_suelta_la_clave_de_la_copia(self):
        con, cur = self.conexion(True)
        with mock.patch.object(g, "conectar_destino", return_value=con):
            with ga.candado_de_la_copia({}):
                pass
        sqls = [c.args for c in cur.execute.call_args_list]
        self.assertEqual(sqls[0], ("SELECT pg_try_advisory_lock(%s)", (g.CLAVE_LOCK,)))
        self.assertEqual(sqls[-1], ("SELECT pg_advisory_unlock(%s)", (g.CLAVE_LOCK,)))
        con.close.assert_called_once()

    def test_ocupado_levanta_y_cierra(self):
        con, cur = self.conexion(False)
        with mock.patch.object(g, "conectar_destino", return_value=con):
            with self.assertRaises(ga.ErrorActualizar):
                with ga.candado_de_la_copia({}):
                    self.fail("no debía entrar")
        con.close.assert_called_once()

    def test_suelta_aunque_el_cuerpo_falle(self):
        con, cur = self.conexion(True)
        with mock.patch.object(g, "conectar_destino", return_value=con):
            with self.assertRaises(RuntimeError):
                with ga.candado_de_la_copia({}):
                    raise RuntimeError("x")
        self.assertEqual(cur.execute.call_args_list[-1].args[0], "SELECT pg_advisory_unlock(%s)")


class Descarga(unittest.TestCase):
    def test_solo_https(self):
        for url in ("http://x/y", "file:///etc/passwd", "ftp://x"):
            with self.assertRaises(ga.ErrorActualizar):
                ga.descargar_https(url, 10)

    def test_tope_de_tamano(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.side_effect = [b"x" * 11, b""]
        with mock.patch("urllib.request.urlopen", return_value=resp):
            with self.assertRaises(ga.ErrorActualizar):
                ga.descargar_https("https://x/y", 10)


class ConElRepoReal(unittest.TestCase):
    def texto(self):
        return open(os.path.join(AQUI, "install.ps1"), encoding="ascii", newline="").read()

    def test_el_install_real_se_lee_y_coincide_con_el_paquete(self):
        i = ga.leer_instalador(self.texto())
        self.assertEqual(i["version"], g.VERSION)
        self.assertEqual(sorted(i["hashes"]), sorted(ah.ARCHIVOS))
        for n in ("garro_comandos.py", "garro_actualizar.py"):
            self.assertIn(n, i["hashes"])
        self.assertEqual(i["python_version"], "3.12.10")

    def test_el_repo_de_la_actualizacion_es_el_del_instalador(self):
        self.assertEqual(re.search(r'\$Repo\s*=\s*"([^"]+)"', self.texto()).group(1), ga.REPO)

    def test_toda_version_instalada_pasa_la_validacion_de_los_args(self):
        gc.validar("actualizar", {"version": g.VERSION, "sha256_install": "a" * 64}, {})

    def test_el_install_define_y_guarda_la_version_de_tareas(self):
        t = self.texto()
        self.assertRegex(t, r'\$VersionTareas\s*=\s*"\d+"')
        self.assertIn('Join-Path $Dir "tareas-version.txt") -Value $VersionTareas', t)
        self.assertEqual(ga.ARCHIVO_TAREAS, "tareas-version.txt")

    def test_la_seccion_de_tareas_no_cambia_sin_subir_VersionTareas(self):
        # Si este test falla, editaste la seccion "Tareas programadas" de install.ps1: subi $VersionTareas (para que una
        # actualizacion remota pida instalar en el equipo) y actualiza la huella de abajo.
        t = self.texto().replace("\r\n", "\n")
        seccion = t[t.index('Paso "Tareas programadas"'):t.index('Paso "Acceso directo"')]
        version = re.search(r'\$VersionTareas\s*=\s*"(\d+)"', t).group(1)
        conocidas = {"1": "69d6048f9cf309505cb9d609e329ffd568fbd1ac4ef4b754d1a07ff9b72494d0"}
        self.assertEqual(sha(seccion.encode("ascii")), conocidas[version], f"VersionTareas={version}")


if __name__ == "__main__":
    unittest.main()
