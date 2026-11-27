"""Copia de prueba de la base de Gestión Integral, en el mismo servidor.

Sirve para probar la carga de compras (garro_escritor.py --archivo ... --escribir --base COPIA) sin tocar la
base real. Se corre a mano, en el servidor donde está la base. Nunca escribe en la base real: de ella solo lee.

    python garro_copia.py --medir             # solo lectura: cuánto ocupa la base, cuánto disco hay, si se puede
    python garro_copia.py --crear NOMBRE      # crea la copia (NOMBRE termina en _prueba, por ejemplo gestion_prueba)
    python garro_copia.py --borrar NOMBRE     # borra una copia creada con este programa

La copia se hace con pg_dump y pg_restore del propio servidor. El programa se puede seguir usando mientras tanto,
pero conviene hacerla en un horario de poca actividad: se lee la base entera una vez y se escribe otra igual, y si
el programa necesita reorganizar una tabla en ese rato, esa operación espera a que termine la lectura.
`--borrar` solo borra bases que este programa creó y marcó. Hace falta que el usuario configurado sea superusuario.

Si la base real tiene un comentario propio, pg_restore lo vuelve a escribir con el mismo texto: es lo único que
toca de ella, y no cambia nada.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

import garro_escritor as ge
import garro_sync as g

AQUI = os.path.dirname(os.path.abspath(__file__))
MARCA = ge.MARCA_COPIA
NOMBRE_VALIDO = re.compile(r"^[a-z][a-z0-9_]{2,40}_prueba$")
TABLAS_DE_CONTROL = ("comprobantes_compras", "items_comprobantes_compras", "asientos_contables", "pagos", "proveedores")
# Si durante la copia el disco baja de esto, se corta: un disco lleno tira abajo el servidor de la base.
DISCO_MINIMO_BYTES = 1024 * 1024 * 1024
PAUSA_CONTROL_S = 5
# Disco libre que se exige antes de copiar, sobre el tamaño de la base: la copia entera más margen.
FACTOR_DISCO_BASE = 2.0
MARGEN_BYTES = 500 * 1024 * 1024
TOPE_SEGUNDOS = 2 * 60 * 60


class ErrorCopia(Exception):
    """La copia no se puede hacer o borrar; el mensaje dice por qué."""


def _lindo(n: int | None) -> str:
    if n is None:
        return "desconocido"
    for unidad in ("B", "KB", "MB", "GB"):
        if n < 1024 or unidad == "GB":
            return f"{n:.0f} {unidad}" if unidad == "B" else f"{n:.1f} {unidad}"
        n /= 1024
    return str(n)


def validar_nombre(nombre: str, cfg: dict[str, str]) -> None:
    if not NOMBRE_VALIDO.match(nombre or ""):
        raise ErrorCopia("El nombre de la copia va en minúsculas, sin espacios, y tiene que terminar en _prueba "
                         "(por ejemplo: gestion_prueba).")
    if nombre.lower() == str(cfg.get("GARRO_BASE", "")).lower():
        raise ErrorCopia("Ese es el nombre de la base real.")


def _conectar(cfg: dict[str, str], solo_lectura: bool):
    con = ge.conectar_garro(cfg, solo_lectura=solo_lectura)
    if not solo_lectura:
        con.set_session(readonly=False, autocommit=True)  # CREATE / DROP DATABASE no van dentro de una transacción
    return con


def _binarios(cfg: dict[str, str], directorio_datos: str | None) -> dict[str, str] | None:
    """pg_dump y pg_restore del servidor: en GARRO_PG_BIN o al lado de la carpeta de datos."""
    exe = ".exe" if os.name == "nt" else ""
    candidatos = [cfg.get("GARRO_PG_BIN", "")]
    if directorio_datos:
        candidatos.append(os.path.join(os.path.dirname(os.path.normpath(directorio_datos)), "bin"))
    for carpeta in candidatos:
        if not carpeta:
            continue
        rutas = {n: os.path.join(carpeta, n + exe) for n in ("pg_dump", "pg_restore")}
        if all(os.path.isfile(r) for r in rutas.values()):
            return rutas
    return None


def _rama(version: str) -> str:
    """«9.3.25» → «9.3»; «16.2» → «16»: lo que tiene que coincidir entre el servidor y sus herramientas."""
    partes = re.findall(r"\d+", version or "")
    if not partes:
        return ""
    return ".".join(partes[:2]) if int(partes[0]) < 10 else partes[0]


def _version_de(binario: str) -> str | None:
    try:
        r = subprocess.run([binario, "--version"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+(?:\.\d+)+)", r.stdout.decode("utf-8", "replace"))
    return m.group(1) if r.returncode == 0 and m else None


def _libre(ruta: str | None) -> int | None:
    try:
        return shutil.disk_usage(ruta).free if ruta and os.path.isdir(ruta) else None
    except OSError:
        return None


def medir(cfg: dict[str, str]) -> dict:
    """Solo lectura: tamaño de la base, disco libre y si están dadas las condiciones para copiarla."""
    con = _conectar(cfg, solo_lectura=True)
    try:
        cur = con.cursor()
        cur.execute("""SELECT d.datname, pg_database_size(d.oid), pg_encoding_to_char(d.encoding), d.datcollate, d.datctype,
                              (SELECT setting FROM pg_settings WHERE name = 'data_directory'),
                              (SELECT count(*) FROM pg_stat_activity AS a WHERE a.datname = d.datname)
                       FROM pg_database AS d WHERE d.datname = current_database()""")
        nombre, tamano, encoding, collate, ctype, datos, conexiones = cur.fetchone()
        cur.execute("""SELECT c.relname, pg_total_relation_size(c.oid)
                       FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
                       WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 2 DESC LIMIT 5""")
        mayores = [(t, int(b)) for t, b in cur.fetchall()]
        cur.execute("""SELECT d.datname, pg_database_size(d.oid) FROM pg_database AS d
                       WHERE shobj_description(d.oid, 'pg_database') LIKE %s ORDER BY 1""", (MARCA + "%",))
        copias = [(n, int(b)) for n, b in cur.fetchall()]
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        fila = cur.fetchone()
        superusuario = bool(fila and fila[0])
        cur.execute("SHOW server_version")
        version_servidor = str(cur.fetchone()[0])
        con.rollback()
    finally:
        con.close()
    tamano = int(tamano)
    libre_datos, libre_aqui = _libre(datos), _libre(AQUI)
    necesario = int(tamano * FACTOR_DISCO_BASE) + MARGEN_BYTES
    motivos = []
    if not superusuario:
        motivos.append("el usuario configurado no es superusuario de la base (hace falta para copiarla)")
    if libre_datos is None:
        motivos.append("no se ve la carpeta de datos del servidor desde esta máquina: hay que correrlo en el servidor")
    elif libre_datos < necesario:
        motivos.append(f"hace falta {_lindo(necesario)} libres en el disco de la base y hay {_lindo(libre_datos)}")
    if libre_aqui is not None and libre_aqui < tamano + MARGEN_BYTES:
        motivos.append(f"hace falta {_lindo(tamano + MARGEN_BYTES)} libres para el archivo temporal y hay {_lindo(libre_aqui)}")
    binarios = _binarios(cfg, datos)
    if binarios is None:
        motivos.append("no se encontraron pg_dump y pg_restore (indicar la carpeta en GARRO_PG_BIN)")
    else:
        version_binarios = _version_de(binarios["pg_restore"])
        if version_binarios is None or _rama(version_binarios) != _rama(version_servidor):
            motivos.append(f"pg_restore es de otra versión ({version_binarios or 'desconocida'}) que el servidor "
                           f"({version_servidor}): indicar en GARRO_PG_BIN la carpeta bin de ESE servidor")
    return {"base": nombre, "tamano_bytes": tamano, "encoding": encoding, "collate": collate, "ctype": ctype,
            "directorio_datos": datos, "conexiones_abiertas": int(conexiones), "tablas_mas_grandes": mayores,
            "copias_existentes": copias, "libre_disco_base_bytes": libre_datos, "libre_disco_programa_bytes": libre_aqui,
            "necesario_bytes": necesario, "superusuario": superusuario, "version_servidor": version_servidor,
            "se_puede": not motivos, "motivos": motivos}


def informe(m: dict) -> str:
    lineas = [
        f"Base: {m['base']} · ocupa {_lindo(m['tamano_bytes'])} · {m['conexiones_abiertas']} conexiones abiertas",
        "Tablas más grandes: " + ", ".join(f"{t} {_lindo(b)}" for t, b in m["tablas_mas_grandes"]),
        f"Disco libre donde está la base: {_lindo(m['libre_disco_base_bytes'])} · "
        f"donde está este programa: {_lindo(m['libre_disco_programa_bytes'])}",
        f"La copia ocuparía unos {_lindo(m['tamano_bytes'])} mientras exista; durante la copia se usa además un "
        f"archivo temporal más chico que se borra al terminar. Se exige tener {_lindo(m['necesario_bytes'])} libres.",
    ]
    if m["copias_existentes"]:
        lineas.append("Copias que ya existen: " + ", ".join(f"{n} ({_lindo(b)})" for n, b in m["copias_existentes"]))
    lineas.append("Se puede copiar." if m["se_puede"] else "NO se puede copiar todavía: " + "; ".join(m["motivos"]) + ".")
    return "\n".join(lineas)


def _correr(comando: list[str], cfg: dict[str, str], vigilar: tuple[str | None, ...] = ()) -> None:
    """Corre pg_dump o pg_restore. La clave va por el entorno, nunca en la línea de comandos. Mientras corre, mira
    el disco de las carpetas de `vigilar`: si baja del mínimo, corta. Ante cualquier corte, mata el proceso."""
    entorno = dict(os.environ, PGPASSWORD=cfg["GARRO_PASSWORD"])
    nombre = os.path.basename(comando[0])
    # Prioridad baja para pg_dump/pg_restore en Windows. El trabajo pesado lo hace igual el servidor de la base.
    banderas = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    with tempfile.TemporaryFile() as errores:
        proceso = subprocess.Popen(comando, env=entorno, stdout=subprocess.DEVNULL, stderr=errores, creationflags=banderas)
        limite = time.monotonic() + TOPE_SEGUNDOS
        try:
            while True:
                try:
                    proceso.wait(timeout=PAUSA_CONTROL_S)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if time.monotonic() > limite:
                    raise ErrorCopia(f"{nombre} tardó más de {TOPE_SEGUNDOS // 3600} horas y se cortó.")
                for ruta in vigilar:
                    libre = _libre(ruta)
                    if libre is not None and libre < DISCO_MINIMO_BYTES:
                        raise ErrorCopia(f"El disco quedó con menos de {_lindo(DISCO_MINIMO_BYTES)} libres y la copia se cortó.")
        finally:
            if proceso.poll() is None:
                proceso.kill()
                proceso.wait()
        if proceso.returncode != 0:
            errores.seek(0)
            detalle = errores.read().decode("utf-8", "replace").strip().splitlines()[-5:]
            g.escribir_log(f"{nombre} terminó con código {proceso.returncode}:\n" + "\n".join(detalle))
            raise ErrorCopia(f"{nombre} falló (código {proceso.returncode}); el detalle está en el log local.")


def _hay_filas(cfg: dict[str, str], base: str | None) -> dict[str, bool]:
    con = ge.conectar_garro(cfg, solo_lectura=True, base=base)
    try:
        cur = con.cursor()
        hay = {}
        for tabla in TABLAS_DE_CONTROL:
            cur.execute(f"SELECT EXISTS (SELECT 1 FROM {tabla})")  # nombres fijos de esta lista, no vienen de afuera
            hay[tabla] = bool(cur.fetchone()[0])
        con.rollback()
        return hay
    finally:
        con.close()


def _soltar_base(cur, nombre: str) -> bool:
    """Borra una base NUESTRA (ya validada): corta las sesiones que le queden y reintenta. True si quedó borrada."""
    for _ in range(3):
        try:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                        (nombre,))
            cur.fetchall()
            cur.execute(f'DROP DATABASE IF EXISTS "{nombre}"')
            return True
        except Exception:
            g.escribir_traza(limitar=True)
            time.sleep(2)
    return False


def crear(cfg: dict[str, str], nombre: str) -> dict:
    validar_nombre(nombre, cfg)
    m = medir(cfg)
    if not m["se_puede"]:
        raise ErrorCopia("No se puede copiar: " + "; ".join(m["motivos"]) + ".")
    binarios = _binarios(cfg, m["directorio_datos"])
    inicio = time.monotonic()
    conexion = ["-h", cfg["GARRO_HOST"], "-p", str(int(cfg["GARRO_PUERTO"])), "-U", cfg["GARRO_USUARIO"]]
    vigilar = (m["directorio_datos"], AQUI)
    temporal = None
    creada = False
    con = _conectar(cfg, solo_lectura=False)
    try:
        cur = con.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (nombre,))
        if cur.fetchone():
            raise ErrorCopia(f"Ya existe una base con el nombre {nombre}. Borrala con --borrar o elegí otro nombre.")
        fd, temporal = tempfile.mkstemp(suffix=".dump", prefix="copia-", dir=AQUI)
        os.close(fd)
        # El nombre ya pasó el patrón (letras minúsculas, números y guion bajo): se puede escribir en la sentencia.
        cur.execute(f'CREATE DATABASE "{nombre}" TEMPLATE template0 ENCODING %s LC_COLLATE %s LC_CTYPE %s',
                    (m["encoding"], m["collate"], m["ctype"]))
        creada = True
        cur.execute(f'COMMENT ON DATABASE "{nombre}" IS %s',
                    (f"{MARCA} · {datetime.now().strftime('%Y-%m-%d %H:%M')} · origen {m['base']}",))
        # --lock-wait-timeout: si una tabla está tomada en exclusiva por el programa, pg_dump no se queda esperando.
        _correr([binarios["pg_dump"], *conexion, "-Fc", "--lock-wait-timeout=5000", "-f", temporal, m["base"]], cfg, vigilar)
        _correr([binarios["pg_restore"], *conexion, "-d", nombre, temporal], cfg, vigilar)
        origen, copia = _hay_filas(cfg, None), _hay_filas(cfg, nombre)
        if any(origen[t] and not copia[t] for t in TABLAS_DE_CONTROL):
            raise ErrorCopia("La copia quedó sin filas en tablas que la base real sí tiene.")
        cur.execute("SELECT pg_database_size(%s)", (nombre,))
        ocupa = int(cur.fetchone()[0])
    except BaseException as e:
        if creada and not _soltar_base(cur, nombre):
            aviso = (f"ATENCIÓN: quedó en el servidor la copia incompleta «{nombre}». "
                     f"Borrala con:  python garro_copia.py --borrar {nombre}")
            print(aviso)
            g.escribir_log(aviso)
            if isinstance(e, ErrorCopia):
                raise ErrorCopia(f"{e} {aviso}") from e
        raise
    finally:
        con.close()
        if temporal and os.path.exists(temporal):
            try:
                os.remove(temporal)
            except OSError:
                print(f"ATENCIÓN: no se pudo borrar el archivo temporal {temporal}. Tiene datos de la base: borralo a mano.")
    return {"copia": nombre, "ocupa_bytes": ocupa, "segundos": int(time.monotonic() - inicio),
            "tablas_controladas": sorted(t for t in TABLAS_DE_CONTROL if copia[t])}


def borrar(cfg: dict[str, str], nombre: str) -> dict:
    validar_nombre(nombre, cfg)
    con = _conectar(cfg, solo_lectura=False)
    try:
        cur = con.cursor()
        cur.execute("SELECT shobj_description(d.oid, 'pg_database') FROM pg_database AS d WHERE d.datname = %s", (nombre,))
        fila = cur.fetchone()
        if fila is None:
            raise ErrorCopia(f"No existe una base con el nombre {nombre}.")
        if not str(fila[0] or "").startswith(MARCA):
            raise ErrorCopia(f"La base {nombre} no la creó este programa (no tiene su marca): no se borra.")
        if not _soltar_base(cur, nombre):
            raise ErrorCopia(f"No se pudo borrar {nombre} (¿alguien la está usando?); el detalle está en el log local.")
    finally:
        con.close()
    return {"borrada": nombre}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Copia de prueba de la base de Gestión Integral (en el mismo servidor)")
    grupo = ap.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--medir", action="store_true", help="solo lectura: tamaño, disco libre y si se puede copiar")
    grupo.add_argument("--crear", metavar="NOMBRE", help="crea la copia (el nombre termina en _prueba)")
    grupo.add_argument("--borrar", metavar="NOMBRE", help="borra una copia creada con este programa")
    ap.add_argument("--config", default=os.path.join(AQUI, g.ARCHIVO_CONFIG))
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass
    try:
        cfg = g.leer_archivo(args.config)
        g.validar_origen(cfg)
        if args.medir:
            print(informe(medir(cfg)))
            return 0
        if args.crear:
            print(f"Copiando la base a {args.crear}. Puede tardar varios minutos. No cierres esta ventana.")
            r = crear(cfg, args.crear)
            print(f"Lista: {r['copia']} ocupa {_lindo(r['ocupa_bytes'])} y tardó {r['segundos']} s.")
            return 0
        r = borrar(cfg, args.borrar)
        print(f"Borrada la copia {r['borrada']}.")
        return 0
    except ErrorCopia as e:
        print(f"No se hizo: {e}")
        return 1
    except KeyboardInterrupt:
        print("Cortado a mano. Si quedó una copia a medio hacer, el aviso de arriba dice cómo borrarla; "
              "con --medir se ven las copias que existen.")
        return 1
    except Exception as e:
        g.escribir_traza()
        print(f"ERROR: {g.traducir_error(e, 'origen')}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
