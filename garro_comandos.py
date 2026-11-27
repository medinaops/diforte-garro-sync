"""Pedidos remotos al servidor (garro.comandos en Supabase): una LISTA CERRADA de comandos.

La tarea de cada 5 minutos toma, al final de su pasada, UN comando pendiente, lo valida de nuevo (el servidor de la
nube ya validó la forma; acá se valida todo otra vez), lo corre con el mismo Python del paquete y sube el resultado.

Reglas que no se negocian:
  - No hay comandos libres: cada tipo tiene su validador (rechaza claves desconocidas) y arma una LISTA de
    argumentos para un script del propio paquete. Nunca se arma un texto para un shell.
  - Nunca se escribe en la base real de Garro. Lo que escribe (`escritor_archivo` con escribir, `deshacer`,
    `copia_crear`, `copia_borrar`) solo acepta el nombre de una copia de prueba, distinta de GARRO_BASE.
  - Antes de subir cualquier salida se reemplaza por *** cada valor secreto de la configuración.
  - Un comando que falla resuelve `error` con su salida; no toca la copia de datos ni las cargas.
  - Lo tomado nunca se vuelve a correr (si el proceso muere, el servidor lo pasa a `sin_respuesta` a los 60 min).

Uso manual (lo usa el autochequeo de la actualización remota):
    python garro_comandos.py --autochequeo   # importa todo el paquete y hace una lectura de Garro y de Supabase
    python garro_comandos.py --version
"""

from __future__ import annotations

import argparse
import importlib
import glob
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from typing import Callable
from urllib.parse import quote, quote_plus, unquote, urlsplit

import garro_actualizar as ga
import garro_copia as gcopia
import garro_escritor as ge
import garro_recuperar as gr
import garro_sync as g

AQUI = os.path.dirname(os.path.abspath(__file__))

MAX_SALIDA = 20000
MAX_PAYLOAD_BYTES = 20 * 1024
MAX_LINEAS_LOG = 400
# La tarea programada se corta a los 30 minutos: un comando nunca se planea más allá de 28 contados desde que arrancó.
PRESUPUESTO_TAREA_S = 28 * 60
PRESUPUESTO_MINIMO_S = 120
SEGUNDOS_ENTRE_AVISOS = 3600

RE_BASE = re.compile(r"gestion_prueba[a-z0-9_]{0,30}")
RE_CARGA_COPIA = re.compile(r"copia-[0-9]{1,12}")
RE_EQUIPO = re.compile(r"[A-Za-z0-9_.-]{1,60}")

TIMEOUTS = {
    "version": 60, "estructura": 120, "indices": 120, "perfil": 180, "copia_medir": 120, "copia_crear": 25 * 60, "copia_borrar": 300,
    "escritor_archivo": 180, "comparar": 120, "deshacer": 180, "logs": 30, "actualizar": ga.PEOR_CASO_S,
}
TIPOS = tuple(TIMEOUTS)


class ErrorComando(Exception):
    """El comando no es válido (o no se puede correr): el mensaje va a la salida del resultado."""


class SinTiempo(ErrorComando):
    """No queda tiempo en esta corrida de la tarea para correr el comando entero: se pide de nuevo."""


class ComandosSinFunciones(Exception):
    """Supabase todavía no tiene la cola de comandos (migración 693)."""


# ---------------------------------------------------------------- validación

def _nombre_corto(k) -> str:
    return str(k)[:40]


def _solo(args, permitidas: tuple, requeridas: tuple = ()) -> dict:
    if not isinstance(args, dict):
        raise ErrorComando("args tiene que ser un objeto")
    raras = sorted(_nombre_corto(k) for k in args if k not in permitidas)
    if raras:
        raise ErrorComando("argumentos desconocidos: " + ", ".join(raras))
    faltan = [k for k in requeridas if k not in args]
    if faltan:
        raise ErrorComando("faltan argumentos: " + ", ".join(faltan))
    return args


def _booleano(args: dict, clave: str, defecto: bool = False) -> bool:
    if clave not in args:
        return defecto
    if args[clave] is not True and args[clave] is not False:
        raise ErrorComando(f"{clave} tiene que ser true o false")
    return args[clave]


def _entero(args: dict, clave: str, minimo: int, maximo: int, defecto: int | None = None) -> int:
    if clave not in args:
        if defecto is None:
            raise ErrorComando(f"falta {clave}")
        return defecto
    v = args[clave]
    if isinstance(v, bool) or not isinstance(v, int) or not minimo <= v <= maximo:
        raise ErrorComando(f"{clave} tiene que ser un entero entre {minimo} y {maximo}")
    return v


def validar_base(valor, cfg: dict[str, str]) -> str:
    """Nombre de una copia de prueba: el patrón de este módulo Y el de garro_copia, y distinto de la base real."""
    if not isinstance(valor, str) or not RE_BASE.fullmatch(valor) or not gcopia.NOMBRE_VALIDO.fullmatch(valor):
        raise ErrorComando("base tiene que ser el nombre de una copia de prueba (gestion_prueba...)")
    if valor.lower() == str(cfg.get("GARRO_BASE", "")).strip().lower():
        raise ErrorComando("base es la base real: los comandos nunca la escriben")
    return valor


def validar(tipo, args, cfg: dict[str, str]) -> dict:
    """Devuelve los args normalizados (con sus valores por defecto) o levanta ErrorComando. Cualquier tipo acepta un
    `equipo` opcional: si viene, solo lo toma esa PC (lo filtra la cola; acá se vuelve a comprobar)."""
    if tipo not in TIPOS:
        raise ErrorComando(f"tipo de comando desconocido: {_nombre_corto(tipo)}")
    equipo = None
    if isinstance(args, dict) and "equipo" in args:
        equipo = args["equipo"]
        if not isinstance(equipo, str) or not RE_EQUIPO.fullmatch(equipo):
            raise ErrorComando("equipo tiene que ser el nombre de una PC (letras, números, punto, guion)")
        args = {k: v for k, v in args.items() if k != "equipo"}
    normalizados = _validar(tipo, args, cfg)
    if equipo is not None:
        normalizados = {**normalizados, "equipo": equipo}
    return normalizados


def _validar(tipo, args, cfg: dict[str, str]) -> dict:
    if tipo in ("version", "estructura", "indices", "perfil", "copia_medir"):
        _solo(args, ())
        return {}
    if tipo in ("copia_crear", "copia_borrar"):
        a = _solo(args, ("base",), ("base",))
        return {"base": validar_base(a["base"], cfg)}
    if tipo == "escritor_archivo":
        a = _solo(args, ("payload", "escribir", "base"), ("payload",))
        if not isinstance(a["payload"], dict):
            raise ErrorComando("payload tiene que ser un objeto")
        if len(json.dumps(a["payload"], ensure_ascii=False).encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ErrorComando("payload supera 20 KB")
        escribir = _booleano(a, "escribir")
        base = validar_base(a["base"], cfg) if "base" in a else None
        if escribir and base is None:
            raise ErrorComando("escribir=true solo con la base de una copia: nunca se escribe en la base real")
        return {"payload": a["payload"], "escribir": escribir, "base": base}
    if tipo == "comparar":
        a = _solo(args, ("base", "humano", "bot", "otro_tipo"), ("base", "humano", "bot"))
        return {"base": validar_base(a["base"], cfg), "humano": _entero(a, "humano", 1, 2**31 - 1),
                "bot": _entero(a, "bot", 1, 2**31 - 1), "otro_tipo": _booleano(a, "otro_tipo")}
    if tipo == "deshacer":
        a = _solo(args, ("carga_id", "base", "confirmar"), ("carga_id", "base"))
        if not isinstance(a["carga_id"], str) or not RE_CARGA_COPIA.fullmatch(a["carga_id"]):
            raise ErrorComando("carga_id: solo cargas de una copia (copia-...)")
        return {"carga_id": a["carga_id"], "base": validar_base(a["base"], cfg), "confirmar": _booleano(a, "confirmar")}
    if tipo == "logs":
        a = _solo(args, ("lineas",))
        return {"lineas": _entero(a, "lineas", 1, MAX_LINEAS_LOG, defecto=100)}
    # actualizar
    a = _solo(args, ("version", "sha256_install"), ("version", "sha256_install"))
    if not isinstance(a["version"], str) or not ga.RE_VERSION.fullmatch(a["version"]):
        raise ErrorComando("version tiene que ser vN.N.N")
    if not isinstance(a["sha256_install"], str) or not ga.RE_SHA256.fullmatch(a["sha256_install"]):
        raise ErrorComando("sha256_install tiene que tener 64 caracteres hexadecimales")
    return {"version": a["version"], "sha256_install": a["sha256_install"].lower()}


# ---------------------------------------------------------------- invocaciones (listas de argumentos)

def armar(tipo: str, a: dict, *, python: str, aqui: str, config: str, archivo: str | None = None) -> list[list[str]]:
    """Las invocaciones (listas de argumentos, nunca un texto de shell) de un comando YA validado.
    `logs` y `actualizar` no corren un subproceso por esta vía: devuelven [] y los atiende `correr`."""
    def s(nombre: str) -> str:
        return os.path.join(aqui, nombre)

    cfg_arg = ["--config", config]
    if tipo == "version":
        return [[python, s("garro_sync.py"), "--version"], [python, s("garro_escritor.py"), "--version"]]
    if tipo == "estructura":
        return [[python, s("garro_escritor.py"), "--estructura", *cfg_arg]]
    if tipo == "indices":
        return [[python, s("garro_indices.py"), *cfg_arg]]
    if tipo == "perfil":
        return [[python, s("comparar_movimientos.py"), "--perfil", *cfg_arg]]
    if tipo == "copia_medir":
        return [[python, s("garro_copia.py"), "--medir", *cfg_arg]]
    if tipo == "copia_crear":
        return [[python, s("garro_copia.py"), "--crear", a["base"], *cfg_arg]]
    if tipo == "copia_borrar":
        return [[python, s("garro_copia.py"), "--borrar", a["base"], *cfg_arg]]
    if tipo == "escritor_archivo":
        if archivo is None:
            raise ErrorComando("falta el archivo temporal del payload")
        argv = [python, s("garro_escritor.py"), "--archivo", archivo]
        if a["escribir"]:
            argv.append("--escribir")
        if a["base"]:
            argv += ["--base", a["base"]]
        return [argv + cfg_arg]
    if tipo == "comparar":
        argv = [python, s("comparar_movimientos.py"), "--base", a["base"], "--humano", str(a["humano"]), "--bot", str(a["bot"])]
        if a["otro_tipo"]:
            argv.append("--otro-tipo")
        return [argv + cfg_arg]
    if tipo == "deshacer":
        argv = [python, s("garro_escritor.py"), "--deshacer", a["carga_id"], "--base", a["base"]]
        if a["confirmar"]:
            argv.append("--confirmar")
        return [argv + cfg_arg]
    return []


# ---------------------------------------------------------------- secretos, recorte

# Los valores de menos de 4 caracteres no se redactan (una clave de 1 o 2 letras volvería ilegible cualquier salida):
# las contraseñas tienen que tener al menos 4 caracteres.
MIN_SECRETO = 4
_CLAVE_SECRETA = re.compile(r"PASS|PWD|SECRET|TOKEN|KEY|CLAVE|CONN|DSN|URI|URL", re.I)


def _variantes(v: str) -> set[str]:
    """El valor tal cual y como puede aparecer en una salida: %-encoding (mayúsculas y minúsculas), con +, escapado de
    JSON y de repr() (comillas y barras escapadas)."""
    q = quote(v, safe="")
    return {v, q, re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), q), quote_plus(v), json.dumps(v)[1:-1],
            repr(v)[1:-1], v.replace("\\", "\\\\").replace("'", "\\'"), v.replace("\\", "\\\\").replace('"', '\\"')}


def secretos_de(cfg: dict[str, str]) -> list[str]:
    """Cada valor de la configuración que es secreto (contraseñas, claves, cadenas de conexión) con sus variantes
    codificadas, de la más larga a la más corta. Los valores de menos de MIN_SECRETO (4) caracteres no se redactan;
    de la contraseña de una cadena de conexión, tal cual está escrita y decodificada."""
    crudos: set[str] = set()
    for k, v in cfg.items():
        if not isinstance(v, str) or not v:
            continue
        if _CLAVE_SECRETA.search(str(k)) or "://" in v:
            crudos.add(v)
            if "://" in v:
                try:
                    clave = urlsplit(v).password
                except ValueError:
                    clave = None
                if clave:
                    crudos.add(clave)
                    crudos.add(unquote(clave))
    salida: set[str] = set()
    for v in crudos:
        if len(v) >= MIN_SECRETO:
            salida |= {x for x in _variantes(v) if x}
    return sorted(salida, key=len, reverse=True)


def redactar(texto: str, secretos: list[str]) -> str:
    for s in secretos:
        texto = texto.replace(s, "***")
    return texto


def recortar(texto: str, limite: int = MAX_SALIDA) -> str:
    if len(texto) <= limite:
        return texto
    aviso = f"\n[... recortado: el texto tenía {len(texto)} caracteres ...]\n"
    resto = max(limite - len(aviso), 0)
    cabeza = resto * 2 // 5
    return texto[:cabeza] + aviso + texto[len(texto) - (resto - cabeza):]


# ---------------------------------------------------------------- subprocesos

def python_del_paquete() -> str:
    """El mismo Python del paquete. La tarea corre con pythonw.exe (sin consola): para capturar la salida se usa
    el python.exe de al lado."""
    exe = sys.executable
    if os.path.basename(exe).lower() == "pythonw.exe":
        candidato = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(candidato):
            return candidato
    return exe


def _matar_arbol(proc: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def correr_proceso(argv: list[str], timeout_s: int, cwd: str = AQUI, env_extra: dict | None = None) -> tuple[int, str]:
    """Corre una lista de argumentos (sin shell), con stdin cerrado y tope de tiempo. Devuelve (código, salida).
    Si se pasa de tiempo se mata el proceso y todo lo que lanzó, y el código es -1."""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra or {})
    opciones: dict = {}
    if sys.platform == "win32":
        opciones["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        opciones["start_new_session"] = True
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, shell=False, **opciones)
    try:
        bruto, _ = proc.communicate(timeout=max(int(timeout_s), 1))
        return proc.returncode, bruto.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        _matar_arbol(proc)
        try:
            bruto, _ = proc.communicate(timeout=15)
        except Exception:
            bruto = b""
        texto = (bruto or b"").decode("utf-8", errors="replace")
        return -1, texto + f"\n[Se cortó: pasó el límite de {int(timeout_s)} s]"


# ---------------------------------------------------------------- logs

def leer_logs(aqui: str, lineas: int) -> str:
    """Últimas `lineas` líneas de garro_sync.log (y de .1 si hace falta). Solo archivos dentro de la carpeta del paquete."""
    raiz = os.path.realpath(aqui)
    juntas: list[str] = []
    for nombre in ("garro_sync.log", "garro_sync.log.1"):
        ruta = os.path.realpath(os.path.join(aqui, nombre))
        if os.path.dirname(ruta) != raiz or not os.path.isfile(ruta):
            continue
        with open(ruta, "rb") as f:
            f.seek(0, os.SEEK_END)
            tam = f.tell()
            f.seek(max(0, tam - 2 * 1024 * 1024))
            texto = f.read().decode("utf-8", errors="replace").splitlines()
        juntas = texto + juntas
        if len(juntas) >= lineas:
            break
    if not juntas:
        return "(no hay garro_sync.log)"
    return "\n".join(juntas[-lineas:])


# ---------------------------------------------------------------- correr un comando

def _resultado(codigo: int, salida: str, segundos: float, secretos: list[str]) -> dict:
    return {"codigo_salida": codigo, "salida": recortar(redactar(salida, secretos)),
            "segundos": round(segundos, 1), "version": g.VERSION, "equipo": socket.gethostname()[:100]}


def limpiar_dumps(aqui: str) -> int:
    """Borra los copia-*.dump que quedaron de una copia cortada (el archivo temporal de pg_dump). Windows no deja
    borrar uno que está en uso: ese se saltea."""
    n = 0
    for ruta in glob.glob(os.path.join(aqui, "copia-*.dump")):
        try:
            os.remove(ruta)
            n += 1
        except OSError:
            pass
    return n


def limpiar_archivos_de_payload(aqui: str) -> None:
    for ruta in glob.glob(os.path.join(aqui, "comando-*.json")):
        try:
            os.remove(ruta)
        except OSError:
            pass


def correr(cmd: dict, cfg: dict[str, str], *, config: str, aqui: str = AQUI, presupuesto_s: float | None = None,
           ejecutar: Callable[..., tuple[int, str]] | None = None,
           descargar: ga.Descargar = ga.descargar_https, candado=ga.candado_de_la_copia,
           exigir_marca: Callable[[dict, str], None] | None = None) -> tuple[str, dict]:
    """Valida y corre un comando. Devuelve (estado, resultado) con estado `ok` o `error`. Nunca levanta."""
    inicio = time.monotonic()
    secretos = secretos_de(cfg)
    extra: dict = {}
    try:
        estado, codigo, salida = _correr(cmd, cfg, config, aqui, presupuesto_s,
                                         ejecutar or (lambda argv, t, env=None: correr_proceso(argv, t, aqui, env)),
                                         descargar, candado, exigir_marca, extra)
    except ErrorComando as e:
        estado, codigo, salida = "error", -1, f"Comando rechazado: {e}"
    except ga.ErrorActualizar as e:
        estado, codigo, salida = "error", 1, str(e)
    except Exception as e:
        estado, codigo, salida = "error", -1, f"Falló el programa al correr el comando: {type(e).__name__}: {e}"
    return estado, {**_resultado(codigo, salida, time.monotonic() - inicio, secretos), **extra}


def _exigir_copia(a: dict, cfg: dict, exigir_marca) -> None:
    """Segunda barrera: la base de un comando tiene que ser una copia hecha por garro_copia (su marca), no solo
    tener el nombre de una."""
    if a.get("base"):
        try:
            exigir_marca(cfg, a["base"])
        except g.ErrorSync as e:
            raise ErrorComando(str(e)) from e


def _correr(cmd, cfg, config, aqui, presupuesto_s, ejecutar, descargar, candado, exigir_marca, extra) -> tuple[str, int, str]:
    if not isinstance(cmd, dict):
        raise ErrorComando("pedido mal formado")
    tipo = cmd.get("tipo")
    a = validar(tipo, cmd.get("args", {}), cfg)
    if a.get("equipo") and a["equipo"].lower() != socket.gethostname().lower():
        raise ErrorComando(f"el comando es para el equipo {a['equipo']}")
    tope = TIMEOUTS[tipo]
    limite = tope if presupuesto_s is None else max(1, min(tope, int(presupuesto_s)))
    if tipo == "copia_crear" and presupuesto_s is not None and presupuesto_s < tope:
        raise SinTiempo(f"No queda tiempo en esta corrida para hacer la copia entera (quedan {int(presupuesto_s)} s, hacen falta "
                        f"{tope}): no se corrió. Pedila de nuevo.")
    python = python_del_paquete()

    if tipo == "logs":
        return "ok", 0, leer_logs(aqui, a["lineas"])
    if tipo == "actualizar":
        texto = ga.actualizar({"version": a["version"], "sha256_install": a["sha256_install"]}, cfg, aqui=aqui, config=config,
                              python=python, correr=lambda argv, t, env=None: ejecutar(argv, t, env), descargar=descargar,
                              candado=candado, plazo_s=presupuesto_s, comando_id=cmd.get("id"))
        extra["version_nueva"] = a["version"]
        return "ok", 0, texto
    # copia_borrar: garro_copia comprueba la marca por su cuenta; copia_crear: la base todavía no existe.
    if tipo not in ("copia_crear", "copia_borrar"):
        _exigir_copia(a, cfg, exigir_marca or ge.exigir_copia_marcada)

    archivo = None
    huerfanos = 0
    try:
        if tipo in ("copia_medir", "copia_crear"):
            huerfanos = limpiar_dumps(aqui)
        if tipo == "escritor_archivo":
            # Archivo propio, UTF-8 sin BOM, dentro de la carpeta del paquete (solo la ve este usuario); se borra siempre.
            fd, archivo = tempfile.mkstemp(prefix="comando-", suffix=".json", dir=aqui)
            with os.fdopen(fd, "wb") as f:
                f.write(json.dumps(a["payload"], ensure_ascii=False).encode("utf-8"))
        invocaciones = armar(tipo, a, python=python, aqui=aqui, config=config, archivo=archivo)
        partes: list[str] = []
        codigo = 0
        fin = time.monotonic() + limite  # un solo tope para todas las invocaciones del comando
        for argv in invocaciones:
            c, salida = ejecutar(argv, max(1, math.ceil(fin - time.monotonic())))
            partes.append(salida.rstrip())
            if c != 0 and codigo == 0:
                codigo = c
        if tipo == "copia_crear" and codigo == -1:
            borrados = limpiar_dumps(aqui)
            partes.append("Puede haber quedado una copia a medio hacer: pedir copia_medir para verla y copia_borrar para sacarla."
                          + (f" Se borraron {borrados} archivo(s) temporal(es) copia-*.dump." if borrados else ""))
        elif huerfanos:
            partes.append(f"(Se borraron {huerfanos} archivo(s) temporal(es) copia-*.dump que habían quedado de una copia cortada.)")
        return ("ok" if codigo == 0 else "error"), codigo, "\n".join(p for p in partes if p)
    finally:
        if archivo is not None:
            try:
                os.remove(archivo)
            except OSError:
                pass


# ---------------------------------------------------------------- cola en Supabase

def _destino(cfg: dict[str, str], sql: str, params: tuple = ()):
    con = g.conectar_destino(cfg)
    try:
        with con.cursor() as cur:
            cur.execute(sql, params)
            fila = cur.fetchone()
        con.commit()
        return fila[0] if fila else None
    except Exception as e:
        if getattr(e, "pgcode", None) in ("42883", "3F000"):  # función o schema inexistente
            raise ComandosSinFunciones() from e
        raise
    finally:
        con.close()


def tomar(cfg: dict[str, str]) -> dict | None:
    cmd = _destino(cfg, "SELECT garro.comandos_tomar(%s, %s)", (g.VERSION, socket.gethostname()))
    if isinstance(cmd, str):
        cmd = json.loads(cmd)
    return cmd


def resolver(cfg: dict[str, str], comando_id: int, estado: str, resultado: dict) -> bool:
    return _destino(cfg, "SELECT garro.comandos_resolver(%s, %s, %s::jsonb)",
                    (comando_id, estado, json.dumps(resultado, ensure_ascii=False))) is True


def _ahora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _avisar_sin_funciones(aqui: str) -> None:
    """La línea del log (una por hora como mucho: la tarea corre cada 5 minutos)."""
    marca = os.path.join(aqui, "comandos.aviso")
    try:
        if time.time() - os.path.getmtime(marca) < SEGUNDOS_ENTRE_AVISOS:
            return
    except OSError:
        pass
    try:
        with open(marca, "w", encoding="utf-8") as f:
            f.write("")
    except OSError:
        pass
    g.decir(f"{_ahora()} (comandos) Supabase todavía no tiene la cola de comandos (migración 693); no se hace nada.")


def pasada(cfg: dict[str, str], config: str, inicio: float | None = None, aqui: str = AQUI) -> None:
    """Toma y corre UN comando. Dos candados del sistema operativo (sueltos solos si el proceso muere): uno de comandos
    y el de las cargas, para no pisar al escritor. Si alguno está tomado, no se toma nada (el comando sigue pendiente)."""
    restante = PRESUPUESTO_TAREA_S - (time.monotonic() - inicio) if inicio is not None else float(PRESUPUESTO_TAREA_S)
    if restante < PRESUPUESTO_MINIMO_S:
        return
    if gr.marca_sin_resolver(aqui):  # además de garro_sync: nunca se toma un comando con una actualización a medias
        informar_marca(cfg, aqui)
        return
    lock_cmd = gr.tomar_lock(os.path.join(aqui, gr.LOCK_COMANDOS))
    if lock_cmd is None:
        return
    lock_cargas = None
    try:
        lock_cargas = ge.tomar_lock(os.path.join(aqui, "cargas.lock"))
        if lock_cargas is None:
            return
        limpiar_archivos_de_payload(aqui)  # con el candado tomado nadie los está usando
        _cerrar_actualizacion_cortada(cfg, aqui)
        try:
            cmd = tomar(cfg)
        except ComandosSinFunciones:
            _avisar_sin_funciones(aqui)
            return
        if not cmd:
            return
        estado, resultado = correr(cmd, cfg, config=config, aqui=aqui, presupuesto_s=restante)
        g.decir(f"{_ahora()} (comandos) {cmd.get('id')} {cmd.get('tipo')}: {estado} ({resultado['segundos']} s)")
        for intento in (1, 2):
            try:
                if not resolver(cfg, cmd["id"], estado, resultado):
                    g.decir(f"{_ahora()} (comandos) la cola no aceptó resolver el comando {cmd.get('id')}.")
                return
            except Exception as e:
                if intento == 2:
                    g.escribir_traza(limitar=True)
                    g.decir(f"{_ahora()} (comandos) ERROR al resolver el comando {cmd.get('id')}: {type(e).__name__}")
    finally:
        if lock_cargas is not None:
            ge.soltar_lock(lock_cargas)
        gr.soltar_lock(lock_cmd)


def informar_marca(cfg: dict[str, str], aqui: str) -> None:
    """Con una actualización a medias sin resolver: no se toma ningún comando. Si el que la estaba haciendo quedó
    «tomado», se lo resuelve con un error que explica qué pasó (una sola vez: después ya no está tomado)."""
    marca = gr.leer_marca(aqui) or {}
    comando_id = marca.get("comando_id")
    if not isinstance(comando_id, int) or isinstance(comando_id, bool):
        return
    resultado = {"codigo_salida": -1, "segundos": 0, "version": g.VERSION, "equipo": socket.gethostname()[:100],
                 "salida": (f"La actualización a {marca.get('nueva')} quedó a medias y NO se pudo restaurar {marca.get('anterior')} "
                            "(marca actualizacion-en-curso sin resolver). No se toman más comandos: hace falta instalar en el equipo.")}
    try:
        if resolver(cfg, comando_id, "error", resultado):
            g.decir(f"{_ahora()} (comandos) el comando {comando_id} (actualización a medias) se cerró como error.")
    except ComandosSinFunciones:
        pass


def _cerrar_actualizacion_cortada(cfg: dict[str, str], aqui: str) -> None:
    """Si garro_recuperar restauró una actualización que se cortó a mitad, su comando quedó «tomado»: se lo cierra
    como error con un resultado que lo explica (y recién ahí se borra la nota)."""
    nota = gr.leer_recuperada(aqui)
    if nota is None:
        return
    comando_id = nota.get("comando_id")
    if not isinstance(comando_id, int) or isinstance(comando_id, bool):
        gr.borrar_recuperada(aqui)
        return
    resultado = {"codigo_salida": -1, "salida": str(nota.get("mensaje", "La actualización se cortó a mitad y se restauró."))[:2000],
                 "segundos": 0, "version": g.VERSION, "equipo": socket.gethostname()[:100]}
    try:
        resolver(cfg, comando_id, "error", resultado)  # false = ya no estaba tomado (pasó a sin_respuesta): igual se cierra la nota
        gr.borrar_recuperada(aqui)
        g.decir(f"{_ahora()} (comandos) el comando {comando_id} (actualización cortada a mitad) se cerró como error.")
    except ComandosSinFunciones:
        pass
    except Exception as e:
        g.decir(f"{_ahora()} (comandos) no se pudo cerrar el comando {comando_id} de la actualización cortada: {type(e).__name__}")


# ---------------------------------------------------------------- autochequeo

MODULOS = ("garro_recuperar", "garro_indices", "garro_sync", "garro_cargas", "garro_movimientos", "garro_escritor", "garro_copia", "comparar_movimientos",
           "relevar_compras", "garro_actualizar", "garro_comandos")


def autochequeo_local(cfg: dict[str, str]) -> str:
    """Importa todo el paquete y hace una lectura de Garro (sesión de solo lectura) y de Supabase. No escribe nada."""
    for m in MODULOS:
        importlib.import_module(m)
    con = ge.conectar_garro(cfg, solo_lectura=True)
    try:
        with con.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        con.rollback()
    finally:
        con.close()
    destino = g.conectar_destino(cfg)
    try:
        with destino.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        destino.rollback()
    finally:
        destino.close()
    return f"AUTOCHEQUEO OK {g.VERSION}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pedidos remotos de garro-sync (lista cerrada de comandos)")
    ap.add_argument("--autochequeo", action="store_true", help="importa el paquete y lee Garro y Supabase (no escribe nada)")
    ap.add_argument("--version", action="version", version=f"garro_comandos {g.VERSION}")
    ap.add_argument("--config", default=os.path.join(AQUI, g.ARCHIVO_CONFIG))
    args = ap.parse_args(argv)
    if not args.autochequeo:
        ap.error("falta --autochequeo")
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass
    try:
        print(autochequeo_local(g.leer_archivo(args.config)))
        return 0
    except Exception as e:
        print(f"AUTOCHEQUEO FALLÓ: {g.traducir_error(e, 'origen')}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
