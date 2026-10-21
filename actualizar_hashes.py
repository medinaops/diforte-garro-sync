"""Regenera en install.ps1 las huellas SHA-256 de los archivos que baja del zip del tag.

Uso (al sacar una versión, DESPUÉS de dejar los archivos como van a quedar en el tag):
    python3 actualizar_hashes.py            # reescribe el bloque HASHES de install.ps1
    python3 actualizar_hashes.py --verificar # solo comprueba; sale con 1 si no coinciden

Los hashes se calculan sobre los bytes del repo (.gitattributes: sin conversión de saltos de línea),
que son los mismos que trae el zip de GitHub.
"""

import hashlib
import os
import re
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
ARCHIVOS = ["actualizar-ahora.bat", "garro_sync.py", "requirements.txt", "supabase-ca-2021.crt"]
INSTALADOR = os.path.join(AQUI, "install.ps1")
PATRON = re.compile(r"(# BEGIN HASHES\r?\n).*?(# END HASHES)", re.S)


def sha256(nombre: str) -> str:
    with open(os.path.join(AQUI, nombre), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def bloque() -> str:
    lineas = ["$HashesPaquete = @{"] + [f'    "{n}" = "{sha256(n)}"' for n in ARCHIVOS] + ["}"]
    return "\r\n".join(lineas) + "\r\n"


def main() -> int:
    with open(INSTALADOR, encoding="ascii", newline="") as f:
        texto = f.read()
    nuevo = PATRON.sub(lambda m: m.group(1) + bloque() + m.group(2), texto)
    if "--verificar" in sys.argv:
        if nuevo != texto:
            print("install.ps1 tiene huellas desactualizadas: correr python3 actualizar_hashes.py")
            return 1
        print("Huellas de install.ps1 al día.")
        return 0
    with open(INSTALADOR, "w", encoding="ascii", newline="") as f:
        f.write(nuevo)
    print("Huellas actualizadas en install.ps1:")
    for n in ARCHIVOS:
        print(f"  {n}  {sha256(n)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
