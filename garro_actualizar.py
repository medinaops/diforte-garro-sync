"""Actualización remota del paquete (comando `actualizar`, ver garro_comandos.py).

Reemplaza SOLO los archivos .py del paquete por los de un tag de este repo y nunca re-registra tareas ni toca Python,
la configuración, el certificado ni las dependencias: si la versión pedida necesita algo de eso, se rechaza con
«requiere instalar en el equipo» y hay que correr el instalador (README). Cadena de confianza:

    quien pide fija el SHA-256 del install.ps1 del tag  ->  install.ps1 fija el SHA-256 de cada archivo del zip.

Orden: se baja y se verifica TODO (install.ps1 y zip) y se arma en una carpeta de staging; recién ahí se toma el
candado de la copia en Supabase, se respaldan los archivos, se escribe la marca `actualizacion-en-curso` y se
reemplazan; se suelta el candado y se corre un autochequeo con el código nuevo en subprocesos. Si algo falla, se
restaura el respaldo y se vuelve a chequear. Una red caída a mitad de camino no deja nada a medio reemplazar: la
red solo se usa antes del primer reemplazo. Si el proceso muere entre la marca y el final, la marca queda y
`garro_recuperar.py` (que nunca se reemplaza) restaura el respaldo al arrancar la próxima corrida.

Plazo: `actualizar` recibe lo que queda de la corrida de la tarea (que Windows corta a los 30 minutos). Cada paso
usa el mínimo entre su tope y lo que queda, y si lo que queda es menor que el peor caso no empieza (sin tocar nada).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import platform
import re
import shutil
import sys
import time
import urllib.request
import zipfile
from typing import Callable

import garro_recuperar as gr
import garro_sync as g

# Debe coincidir con $Repo de install.ps1 (lo comprueba un test): el instalador no se copia a la carpeta del programa.
REPO = "medinaops/diforte-garro-sync"
URL_INSTALADOR = "https://raw.githubusercontent.com/{repo}/{version}/install.ps1"
URL_ZIP = "https://github.com/{repo}/archive/refs/tags/{version}.zip"
MAX_INSTALADOR = 1024 * 1024
MAX_ZIP = 25 * 1024 * 1024
MAX_ARCHIVO = 8 * 1024 * 1024

RE_VERSION = re.compile(r"v[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}")
RE_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
RE_NOMBRE_ARCHIVO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}")

ARCHIVO_VERSION = "version.txt"
ARCHIVO_TAREAS = "tareas-version.txt"
ARCHIVO_PYTHON = "python-verificado.txt"
CARPETA_RESPALDO = "actualizacion-respaldo"
CARPETA_STAGING = "actualizacion-staging"
REQUIERE_INSTALAR = "requiere instalar en el equipo"

# Archivos .py que una actualización remota NUNCA reemplaza (si cambian, hay que instalar en el equipo).
NO_REEMPLAZABLES = ("garro_recuperar.py",)

# Topes (segundos) de cada paso y peor caso de una actualización: nada empieza si lo que queda de la corrida es menos.
TOPE_DESCARGA = 90
TOPE_VERSION = 20
TOPE_AUTOCHEQUEO = 60
# --version de sync y escritor, las dos formas de arrancar de las tareas, y el autochequeo del paquete.
TOPE_CHEQUEO_TOTAL = 4 * TOPE_VERSION + TOPE_AUTOCHEQUEO
MARGEN_S = 30
PEOR_CASO_S = 2 * TOPE_DESCARGA + 2 * TOPE_CHEQUEO_TOTAL + MARGEN_S  # bajar x2, chequear, restaurar y volver a chequear

Descargar = Callable[[str, int], bytes]
CorrerProceso = Callable[..., "tuple[int, str]"]  # (argv, timeout_s, env_extra=None) -> (código, salida)


class ErrorActualizar(Exception):
    """La actualización no se hizo (o se deshizo); el mensaje dice por qué."""


def sha256_de(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def descargar_https(url: str, maximo: int, tope_s: float = TOPE_DESCARGA) -> bytes:
    """GET con tope de tamaño y de tiempo TOTAL. Solo https."""
    if not url.startswith("https://"):
        raise ErrorActualizar("solo se baja por https")
    limite = time.monotonic() + tope_s
    pedido = urllib.request.Request(url, headers={"User-Agent": "garro-sync"})
    partes: list[bytes] = []
    total = 0
    with urllib.request.urlopen(pedido, timeout=30) as r:  # noqa: S310 (https verificado arriba)
        while True:
            trozo = r.read(65536)
            if not trozo:
                break
            partes.append(trozo)
            total += len(trozo)
            if total > maximo:
                raise ErrorActualizar(f"la descarga supera el tope de {maximo} bytes")
            if time.monotonic() > limite:
                raise ErrorActualizar(f"la descarga pasó el tope de {int(tope_s)} s")
    return b"".join(partes)


# ---------------------------------------------------------------- install.ps1 del tag

def _constante(texto: str, nombre: str) -> str:
    m = re.search(r'^[ \t]*\$' + re.escape(nombre) + r'[ \t]*=[ \t]*"([^"\r\n]*)"', texto, re.M)
    if not m:
        raise ErrorActualizar(f"el install.ps1 del tag no define ${nombre}: {REQUIERE_INSTALAR}")
    return m.group(1)


def leer_instalador(texto: str) -> dict:
    """Constantes del install.ps1 de un tag: $Version, $PythonVersion, $PythonSha256, $VersionTareas y $HashesPaquete."""
    bloque = re.search(r"# BEGIN HASHES\r?\n(.*?)# END HASHES", texto, re.S)
    if not bloque:
        raise ErrorActualizar("el install.ps1 del tag no tiene el bloque de huellas")
    hashes: dict[str, str] = {}
    for nombre, huella in re.findall(r'^[ \t]*"([^"\r\n]+)"[ \t]*=[ \t]*"([^"\r\n]+)"[ \t\r]*$', bloque.group(1), re.M):
        if not RE_NOMBRE_ARCHIVO.fullmatch(nombre) or ".." in nombre:
            raise ErrorActualizar(f"nombre de archivo inválido en las huellas: {nombre[:60]}")
        if not re.fullmatch(r"[0-9a-f]{64}", huella):
            raise ErrorActualizar(f"huella inválida para {nombre}")
        hashes[nombre] = huella
    if "garro_sync.py" not in hashes or "garro_comandos.py" not in hashes:
        raise ErrorActualizar("las huellas del tag no incluyen garro_sync.py y garro_comandos.py")
    return {
        "version": _constante(texto, "Version"),
        "python_version": _constante(texto, "PythonVersion"),
        "python_sha256": _constante(texto, "PythonSha256"),
        "version_tareas": _constante(texto, "VersionTareas"),
        "hashes": hashes,
    }


def _leer_marca(ruta: str) -> dict[str, str]:
    datos: dict[str, str] = {}
    try:
        with open(ruta, encoding="utf-8") as f:
            for linea in f:
                if "=" in linea:
                    k, v = linea.split("=", 1)
                    datos[k.strip()] = v.strip()
    except OSError:
        pass
    return datos


def _leer_linea(ruta: str) -> str | None:
    try:
        with open(ruta, encoding="ascii", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def _sha_de_archivo(ruta: str) -> str | None:
    return gr.sha_de(ruta)


def _tupla(version: str) -> tuple:
    return tuple(int(x) for x in version[1:].split("."))


def verificar_compatibilidad(nuevo: dict, aqui: str, version_python: str) -> None:
    """Rechaza (sin tocar nada) lo que una actualización remota no puede hacer: cambiar Python, las tareas, el
    certificado, las dependencias o cualquier archivo que no sea un .py del paquete."""
    r = REQUIERE_INSTALAR
    marca = _leer_marca(os.path.join(aqui, ARCHIVO_PYTHON))
    if not marca.get("version") or not marca.get("sha256"):
        raise ErrorActualizar(f"{r}: no se puede comprobar el Python instalado (falta {ARCHIVO_PYTHON})")
    if nuevo["python_version"] != marca["version"] or nuevo["python_sha256"] != marca["sha256"]:
        raise ErrorActualizar(f"{r}: cambia el Python ({marca['version']} -> {nuevo['python_version']})")
    if version_python != nuevo["python_version"]:
        raise ErrorActualizar(f"{r}: el Python en uso ({version_python}) no es el que pide la versión ({nuevo['python_version']})")

    tareas = _leer_linea(os.path.join(aqui, ARCHIVO_TAREAS))
    if tareas is None:
        raise ErrorActualizar(f"{r}: no se sabe qué versión de tareas hay instalada (falta {ARCHIVO_TAREAS})")
    if tareas != nuevo["version_tareas"]:
        raise ErrorActualizar(f"{r}: cambian las tareas programadas (versión de tareas {tareas} -> {nuevo['version_tareas']})")

    for nombre, huella in sorted(nuevo["hashes"].items()):
        if nombre.endswith(".py") and nombre not in NO_REEMPLAZABLES:
            continue
        if _sha_de_archivo(os.path.join(aqui, nombre)) != huella:
            raise ErrorActualizar(f"{r}: cambia {nombre} (no es un archivo Python que se pueda reemplazar a distancia)")


# ---------------------------------------------------------------- zip del tag -> staging

def preparar_staging(zip_bytes: bytes, hashes: dict[str, str], staging: str) -> None:
    """Escribe en `staging` cada archivo de las huellas, verificado. Nada más del zip se extrae."""
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise ErrorActualizar("el zip del tag está dañado") from e
    with z:
        raices = {n.split("/", 1)[0] for n in z.namelist() if n}
        if len(raices) != 1:
            raise ErrorActualizar("el zip del tag no tiene una única carpeta raíz")
        raiz = next(iter(raices))
        os.makedirs(staging, exist_ok=True)
        for nombre, esperado in sorted(hashes.items()):
            try:
                info = z.getinfo(f"{raiz}/{nombre}")
            except KeyError:
                raise ErrorActualizar(f"falta {nombre} en el zip del tag") from None
            if info.file_size > MAX_ARCHIVO:
                raise ErrorActualizar(f"{nombre} es demasiado grande")
            with z.open(info) as f:
                datos = f.read(MAX_ARCHIVO + 1)
            if len(datos) > MAX_ARCHIVO:
                raise ErrorActualizar(f"{nombre} es demasiado grande")
            if sha256_de(datos) != esperado:
                raise ErrorActualizar(f"{nombre} del zip NO coincide con la huella del install.ps1: no se instala nada")
            with open(os.path.join(staging, nombre), "wb") as out:
                out.write(datos)


# ---------------------------------------------------------------- candado de la copia

@contextlib.contextmanager
def candado_de_la_copia(cfg: dict[str, str]):
    """Candado de sesión sobre la misma clave que usa la copia: mientras dura, ninguna copia arranca, y si hay una en
    curso no se actualiza (el programa en memoria y los archivos nuevos no se mezclan)."""
    con = g.conectar_destino(cfg)
    try:
        con.autocommit = True
        cur = con.cursor()
        cur.execute("SELECT pg_try_advisory_lock(%s)", (g.CLAVE_LOCK,))
        if not cur.fetchone()[0]:
            raise ErrorActualizar("hay una copia en curso: no se actualiza ahora, volver a pedirlo en unos minutos")
        try:
            yield
        finally:
            try:
                cur.execute("SELECT pg_advisory_unlock(%s)", (g.CLAVE_LOCK,))
            except Exception:
                pass
    finally:
        try:
            con.close()
        except Exception:
            pass


# ---------------------------------------------------------------- autochequeo del código nuevo

def autochequeo(aqui: str, version: str, python: str, config: str, correr: CorrerProceso,
                hasta: float | None = None) -> tuple[bool, str]:
    """Con el código que haya en `aqui`, en subprocesos: --version de los dos programas, el argv EXACTO de las dos
    tareas programadas (solo se parsea y se importa todo lo que ese camino usa), y el autochequeo del paquete (import
    de todo y una lectura de Garro y de Supabase que no escribe nada). `hasta` es un plazo absoluto (monotonic)."""
    env = {"GARRO_SYNC_AUTOCHEQUEO": "1"}
    pasos = [
        ("garro_sync --version", [python, os.path.join(aqui, "garro_sync.py"), "--version"], f"garro_sync {version}", None, TOPE_VERSION),
        ("garro_escritor --version", [python, os.path.join(aqui, "garro_escritor.py"), "--version"], f"garro_escritor {version}", None, TOPE_VERSION),
    ]
    for i, args_tarea in enumerate(g.ARGV_TAREAS):
        pasos.append((f"argv de la tarea {i + 1}", [python, os.path.join(aqui, "garro_sync.py"), *args_tarea], f"AUTOCHEQUEO ARGV OK {version}", env, TOPE_VERSION))
    pasos.append(("autochequeo", [python, os.path.join(aqui, "garro_comandos.py"), "--autochequeo", "--config", config],
                  f"AUTOCHEQUEO OK {version}", None, TOPE_AUTOCHEQUEO))
    detalle: list[str] = []
    for nombre, argv, esperado, extra, tope in pasos:
        t = tope if hasta is None else min(tope, int(hasta - time.monotonic()))
        if t < 1:
            detalle.append(f"{nombre}: sin tiempo en esta corrida")
            return False, "\n".join(detalle)
        codigo, salida = correr(argv, t, extra) if extra else correr(argv, t)
        if codigo != 0 or esperado not in salida:
            detalle.append(f"{nombre}: FALLÓ (código {codigo}): {salida.strip()[-600:]}")
            return False, "\n".join(detalle)
        detalle.append(f"{nombre}: ok")
    return True, "\n".join(detalle)


# ---------------------------------------------------------------- reemplazo con respaldo

def respaldar(aqui: str, nombres: list[str], respaldo: str) -> dict[str, bool]:
    """Copia (copy2: conserva la fecha) los archivos actuales al respaldo. Devuelve cuáles existían."""
    if os.path.isdir(respaldo):
        shutil.rmtree(respaldo)
    os.makedirs(respaldo)
    previos: dict[str, bool] = {}
    for nombre in nombres:
        actual = os.path.join(aqui, nombre)
        previos[nombre] = os.path.exists(actual)
        if previos[nombre]:
            shutil.copy2(actual, os.path.join(respaldo, nombre))
            gr.fsync_archivo(os.path.join(respaldo, nombre))
    return previos


def aplicar(aqui: str, staging: str, nombres: list[str]) -> None:
    for nombre in nombres:
        gr.escribir_atomico(os.path.join(staging, nombre), os.path.join(aqui, nombre))
    gr.borrar_pyc(aqui, nombres)


# ---------------------------------------------------------------- el comando

def actualizar(args: dict, cfg: dict[str, str], *, aqui: str, config: str, python: str, correr: CorrerProceso,
               descargar: Descargar = descargar_https, candado=candado_de_la_copia,
               version_actual: str | None = None, version_python: str | None = None,
               plazo_s: float | None = None, comando_id: int | None = None) -> str:
    """Devuelve el texto del resultado si salió bien; levanta ErrorActualizar si no (con el detalle).
    `plazo_s`: segundos que quedan de la corrida de la tarea (por defecto, los 28 minutos completos)."""
    inicio = time.monotonic()
    plazo = 28 * 60 if plazo_s is None else plazo_s
    hasta = inicio + plazo
    version = args["version"]
    sha_pedido = args["sha256_install"].lower()
    actual = version_actual or g.VERSION

    if plazo < PEOR_CASO_S:
        raise ErrorActualizar(f"No hay tiempo en esta corrida para actualizar con seguridad (quedan {int(plazo)} s, hacen falta "
                              f"{PEOR_CASO_S}): no se tocó nada. Volver a pedirla.")
    if gr.marca_sin_resolver(aqui):
        raise ErrorActualizar("Hay una actualización anterior sin cerrar (marca actualizacion-en-curso): no se pisa el respaldo. "
                              "Se restaura sola al arrancar la próxima corrida; después, volver a pedirla.")
    if _tupla(version) < _tupla(actual):
        raise ErrorActualizar(f"{REQUIERE_INSTALAR}: bajar de versión ({actual} -> {version}) solo se hace en el equipo")
    version_python = version_python or platform.python_version()

    def bajar(url: str, maximo: int, que: str) -> bytes:
        if hasta - time.monotonic() < TOPE_DESCARGA:
            raise ErrorActualizar(f"no hay tiempo para bajar {que}: no se tocó nada")
        try:
            return descargar(url, maximo)
        except ErrorActualizar:
            raise
        except Exception as e:
            raise ErrorActualizar(f"no se pudo bajar {que}: {type(e).__name__}") from e

    # 1. install.ps1 del TAG pedido (no de main), verificado contra la huella que fijó quien pidió.
    instalador = bajar(URL_INSTALADOR.format(repo=REPO, version=version), MAX_INSTALADOR, f"el install.ps1 de {version}")
    if sha256_de(instalador) != sha_pedido:
        raise ErrorActualizar("el SHA-256 del install.ps1 bajado NO coincide con el pedido: no se tocó nada")
    try:
        texto = instalador.decode("ascii")
    except UnicodeDecodeError as e:
        raise ErrorActualizar("el install.ps1 no es ASCII: no se tocó nada") from e

    # 2. Qué dice ese install.ps1 y si una actualización remota puede aplicarlo.
    nuevo = leer_instalador(texto)
    if nuevo["version"] != version:
        raise ErrorActualizar(f"el install.ps1 del tag dice {nuevo['version']}, no {version}: no se tocó nada")
    verificar_compatibilidad(nuevo, aqui, version_python)

    py_nuevos = sorted(n for n in nuevo["hashes"] if n.endswith(".py") and n not in NO_REEMPLAZABLES)
    a_reemplazar = [n for n in py_nuevos if _sha_de_archivo(os.path.join(aqui, n)) != nuevo["hashes"][n]]
    if not a_reemplazar and actual == version:
        return f"{version} ya estaba instalada y verificada: no se cambió nada."

    # 3. El zip del tag, verificado archivo por archivo, armado en staging. Toda la red termina acá.
    staging = os.path.join(aqui, CARPETA_STAGING)
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    respaldo = os.path.join(aqui, CARPETA_RESPALDO)
    try:
        zip_bytes = bajar(URL_ZIP.format(repo=REPO, version=version), MAX_ZIP, f"el zip de {version}")
        preparar_staging(zip_bytes, {n: nuevo["hashes"][n] for n in nuevo["hashes"]}, staging)

        # 4. Con la copia frenada: respaldar, marcar y reemplazar. Si algo falla en el reemplazo, restaurar.
        with candado(cfg):
            previos = respaldar(aqui, a_reemplazar, respaldo)
            for nombre in a_reemplazar:  # los nuevos, ya en disco, antes de que exista la marca
                gr.fsync_archivo(os.path.join(staging, nombre))
            huellas = {n: gr.sha_de(os.path.join(respaldo, n)) for n in a_reemplazar if previos[n]}
            gr.escribir_marca(aqui, {"anterior": actual, "nueva": version, "archivos": a_reemplazar, "previos": previos,
                                     "sha256": huellas, "comando_id": comando_id})
            try:
                aplicar(aqui, staging, a_reemplazar)
            except Exception as e:
                problemas = gr.restaurar_archivos(aqui, respaldo, a_reemplazar, previos)
                if problemas:
                    raise ErrorActualizar("falló el reemplazo y la restauración quedó INCOMPLETA (" + "; ".join(problemas)
                                          + "): la marca queda y se reintenta al arrancar la próxima corrida. "
                                          "Si sigue, hace falta instalar en el equipo.") from e
                gr.borrar_marca(aqui)
                raise ErrorActualizar(f"no se pudo reemplazar los archivos ({type(e).__name__}); se restauró la versión anterior") from e

        # 5. Ya sin el candado de la copia: autochequeo con el código nuevo; si falla, restaurar y volver a chequear.
        try:
            bien, detalle = autochequeo(aqui, version, python, config, correr, hasta)
        except Exception as e:
            bien, detalle = False, f"el autochequeo no pudo correr: {type(e).__name__}"
        if not bien:
            problemas = gr.restaurar_archivos(aqui, respaldo, a_reemplazar, previos)
            if problemas:
                raise ErrorActualizar("autochequeo FALLÓ y la restauración quedó INCOMPLETA (" + "; ".join(problemas)
                                      + "): la marca queda y se reintenta al arrancar la próxima corrida. "
                                      "Si sigue, hace falta instalar en el equipo. Detalle: " + detalle)
            gr.borrar_marca(aqui)
            try:
                ok_rest, det_rest = autochequeo(aqui, actual, python, config, correr, max(hasta, time.monotonic() + TOPE_CHEQUEO_TOTAL))
            except Exception as e:
                ok_rest, det_rest = False, f"no pudo correr: {type(e).__name__}"
            estado = "OK" if ok_rest else "FALLÓ"
            raise ErrorActualizar(
                f"El autochequeo de {version} FALLÓ; se restauró {actual}. Chequeo de la restauración: {estado}.\n"
                f"Detalle del autochequeo:\n{detalle}" + ("" if ok_rest else f"\nDetalle de la restauración:\n{det_rest}"))
        gr.borrar_marca(aqui)  # recién ahora: la actualización quedó cerrada
        if gr.hay_marca(aqui):
            # No se pudo borrar: se la deja cerrada (`estado: ok`) para que la recuperación nunca restaure una versión buena.
            try:
                gr.escribir_marca(aqui, {"estado": "ok", "anterior": actual, "nueva": version, "archivos": a_reemplazar,
                                         "comando_id": comando_id})
            except OSError:
                pass
        try:
            with open(os.path.join(aqui, ARCHIVO_VERSION + ".nuevo"), "w", encoding="ascii", newline="") as f:
                f.write(version + "\r\n")
            os.replace(os.path.join(aqui, ARCHIVO_VERSION + ".nuevo"), os.path.join(aqui, ARCHIVO_VERSION))
        except OSError:
            pass  # version.txt es informativo: el código ya está y pasó el autochequeo
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return (f"Actualizado de {actual} a {version}. Versión nueva: {version}. Archivos reemplazados: {', '.join(a_reemplazar)}.\n"
            f"Autochequeo:\n{detalle}\nRespaldo de lo anterior en {CARPETA_RESPALDO}.")


if __name__ == "__main__":  # pragma: no cover
    sys.exit("garro_actualizar.py se usa desde garro_comandos.py")
