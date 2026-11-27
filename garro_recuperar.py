"""Recuperación de una actualización remota cortada a mitad (ver garro_actualizar.py).

Este archivo SOLO usa la biblioteca estándar y NUNCA lo reemplaza una actualización remota (si cambia, hay que
instalar en el equipo): es lo único que corre antes de importar el resto del paquete, así que sirve aunque los
demás archivos hayan quedado mezclados (unos viejos y otros nuevos) por un corte de luz o un proceso matado.

Mientras una actualización reemplaza archivos existe la marca `actualizacion-en-curso` (JSON: versión vieja, nueva,
archivos y cuáles ya existían). `garro_sync.py` la mira al arrancar (tareas y copia diaria): si está y nadie está
actualizando (el candado `comandos.lock` está libre), restaura los archivos desde `actualizacion-respaldo\\`, borra la
marca y deja `actualizacion-recuperada.json`, que la próxima pasada de comandos usa para cerrar la fila que quedó
«tomada» con un resultado que lo explica.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sys
import time

MARCA_EN_CURSO = "actualizacion-en-curso"
MARCA_RECUPERADA = "actualizacion-recuperada.json"
CARPETA_RESPALDO = "actualizacion-respaldo"
LOCK_COMANDOS = "comandos.lock"


# ---------------------------------------------------------------- candado de archivo del sistema operativo

def tomar_lock(ruta: str):
    """Candado sobre un archivo abierto (mismo patrón que el del escritor): el sistema lo suelta solo si el proceso
    muere. Devuelve el archivo o None si está tomado."""
    f = open(ruta, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                f.write(b"0")
                f.flush()
            f.seek(0)
            pedir = lambda: msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)  # noqa: E731
            ocupado = (errno.EACCES, errno.EDEADLK, 36)
        else:
            import fcntl
            pedir = lambda: fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # noqa: E731
            ocupado = (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES)
        try:
            pedir()
        except OSError as e:
            if e.errno in ocupado:
                f.close()
                return None
            raise
    except BaseException:
        f.close()
        raise
    return f


def soltar_lock(f) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        f.close()


# ---------------------------------------------------------------- archivos

def sha_de(ruta: str) -> str | None:
    try:
        with open(ruta, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def fsync_archivo(ruta: str) -> None:
    """Fuerza a disco el contenido de un archivo (y, donde se puede, su carpeta)."""
    with open(ruta, "r+b") as f:
        f.flush()
        os.fsync(f.fileno())
    if sys.platform != "win32":
        try:
            fd = os.open(os.path.dirname(os.path.abspath(ruta)), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass


def escribir_atomico(origen: str, destino: str) -> None:
    """Copia con copy2 (conserva la fecha de modificación) a un nombre temporal, la fuerza a disco y reemplaza de un golpe."""
    tmp = destino + ".nuevo"
    shutil.copy2(origen, tmp)
    fsync_archivo(tmp)
    os.replace(tmp, destino)


def borrar_pyc(aqui: str, nombres: list[str]) -> None:
    """Saca el bytecode compilado de los módulos tocados: que nunca quede uno de la versión que no es."""
    carpeta = os.path.join(aqui, "__pycache__")
    for nombre in nombres:
        if not nombre.endswith(".py"):
            continue
        base = nombre[:-3] + "."
        try:
            for f in os.listdir(carpeta):
                if f.startswith(base) and f.endswith(".pyc"):
                    try:
                        os.remove(os.path.join(carpeta, f))
                    except OSError:
                        pass
        except OSError:
            pass


def restaurar_archivos(aqui: str, respaldo: str, nombres: list[str], previos: dict[str, bool] | None = None) -> list[str]:
    """Devuelve cada archivo a como estaba (o lo borra si no existía antes). Lista de problemas; vacía si salió bien."""
    problemas: list[str] = []
    for nombre in nombres:
        destino = os.path.join(aqui, nombre)
        copia = os.path.join(respaldo, nombre)
        try:
            if os.path.exists(copia):
                escribir_atomico(copia, destino)
                if sha_de(destino) != sha_de(copia):
                    problemas.append(f"{nombre} no quedó igual al respaldo")
            elif previos is not None and previos.get(nombre, False):
                problemas.append(f"{nombre} existía y falta en el respaldo")
            elif os.path.exists(destino):
                os.remove(destino)
        except OSError as e:
            problemas.append(f"{nombre}: {type(e).__name__}")
    borrar_pyc(aqui, nombres)
    return problemas


def _log(aqui: str, texto: str) -> None:
    try:
        with open(os.path.join(aqui, "garro_sync.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} (actualización) {texto}\n")
    except OSError:
        pass


def hay_marca(aqui: str) -> bool:
    return os.path.exists(os.path.join(aqui, MARCA_EN_CURSO))


def leer_marca(aqui: str) -> dict | None:
    try:
        with open(os.path.join(aqui, MARCA_EN_CURSO), encoding="utf-8") as f:
            datos = json.load(f)
        return datos if isinstance(datos, dict) else None
    except (OSError, ValueError):
        return None


def marca_sin_resolver(aqui: str) -> bool:
    """Hay marca y no es una marca cerrada (`estado: ok`, que solo queda si no se pudo borrar)."""
    if not hay_marca(aqui):
        return False
    datos = leer_marca(aqui)
    return datos is None or datos.get("estado") != "ok"


def escribir_marca(aqui: str, datos: dict) -> None:
    ruta = os.path.join(aqui, MARCA_EN_CURSO)
    with open(ruta + ".tmp", "w", encoding="utf-8") as f:
        json.dump(datos, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(ruta + ".tmp", ruta)
    fsync_archivo(ruta)


def borrar_marca(aqui: str) -> None:
    try:
        os.remove(os.path.join(aqui, MARCA_EN_CURSO))
    except OSError:
        pass


def recuperar(aqui: str) -> str | None:
    """Si quedó una actualización a medias, restaura el respaldo. Devuelve:
    None (nada que hacer, o la está haciendo otro proceso), 'recuperada' o 'incompleta' (quedan archivos sin restaurar,
    el respaldo no coincide con lo anotado, o la marca no se pudo borrar; la marca sigue y se reintenta)."""
    ruta_marca = os.path.join(aqui, MARCA_EN_CURSO)
    if not os.path.exists(ruta_marca):
        return None
    lock = tomar_lock(os.path.join(aqui, LOCK_COMANDOS))
    if lock is None:
        return None  # hay una actualización en marcha en otro proceso: no se toca
    try:
        if not os.path.exists(ruta_marca):
            return None
        respaldo = os.path.join(aqui, CARPETA_RESPALDO)
        marca = leer_marca(aqui)
        if marca is not None and marca.get("estado") == "ok":
            borrar_marca(aqui)  # la actualización terminó bien y solo faltó borrar la marca: no se restaura nada
            return None
        try:
            if marca is None:
                raise ValueError("marca ilegible")
            archivos = [n for n in marca["archivos"] if isinstance(n, str) and os.path.basename(n) == n]
            previos = {n: bool(v) for n, v in (marca.get("previos") or {}).items()}
            huellas = marca.get("sha256") if isinstance(marca.get("sha256"), dict) else None
        except (ValueError, KeyError, TypeError):
            marca, previos, huellas = {}, None, None
            archivos = sorted(n for n in os.listdir(respaldo) if n.endswith(".py")) if os.path.isdir(respaldo) else []
        if huellas is not None:  # el respaldo tiene que ser el que se anotó: si no, no se toca nada
            malos = [n for n in archivos if previos.get(n) and sha_de(os.path.join(respaldo, n)) != huellas.get(n)]
            if malos:
                _log(aqui, "ERROR: una actualización quedó a medias y el respaldo NO coincide con lo anotado en la marca ("
                     + ", ".join(malos) + "): no se restaura nada. Hace falta instalar en el equipo.")
                return "incompleta"
        problemas = restaurar_archivos(aqui, respaldo, archivos, previos)
        if problemas:
            _log(aqui, "ERROR: una actualización quedó a medias y la restauración quedó INCOMPLETA ("
                 + "; ".join(problemas) + "). Se reintenta en la próxima corrida; si sigue, instalar en el equipo.")
            return "incompleta"
        aviso = {"comando_id": marca.get("comando_id"), "anterior": marca.get("anterior"), "nueva": marca.get("nueva"),
                 "archivos": archivos,
                 "mensaje": (f"La actualización a {marca.get('nueva')} se cortó a mitad (se reinició el servidor o se mató el proceso): "
                             f"se restauró {marca.get('anterior')} desde el respaldo. Volver a pedirla.")}
        try:
            with open(os.path.join(aqui, MARCA_RECUPERADA), "w", encoding="utf-8") as f:
                json.dump(aviso, f)
        except OSError:
            pass
        borrar_marca(aqui)
        if os.path.exists(ruta_marca):
            _log(aqui, "ERROR: se restauró el respaldo pero no se pudo borrar la marca actualizacion-en-curso: se trata como incompleta.")
            return "incompleta"
        _log(aqui, aviso["mensaje"])
        return "recuperada"
    finally:
        soltar_lock(lock)


def leer_recuperada(aqui: str) -> dict | None:
    try:
        with open(os.path.join(aqui, MARCA_RECUPERADA), encoding="utf-8") as f:
            datos = json.load(f)
        return datos if isinstance(datos, dict) else None
    except (OSError, ValueError):
        return None


def borrar_recuperada(aqui: str) -> None:
    try:
        os.remove(os.path.join(aqui, MARCA_RECUPERADA))
    except OSError:
        pass
