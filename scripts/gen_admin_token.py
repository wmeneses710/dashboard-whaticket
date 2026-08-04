#!/usr/bin/env python3
"""Genera el token de administracion del dashboard (DASHBOARD_ADMIN_TOKEN).

Ese token habilita los endpoints de ESCRITURA: hoy, prender y apagar operadores. Si la
variable NO esta puesta, la escritura queda DESHABILITADA (falla cerrada) y el modal se
muestra en modo lectura.

    python scripts/gen_admin_token.py            # solo si NO hay token todavia
    python scripts/gen_admin_token.py --rotar    # reemplazar uno existente, a proposito

SE NIEGA A GENERAR SI YA HAY UNO. El script nunca escribio archivos (solo imprime), asi que
no puede sobreescribir nada por si mismo; el peligro es HUMANO: alguien lo corre "para ver",
pega el token nuevo, y el anterior deja de servir al instante para todos los que lo tenian
guardado en el navegador. Con este guard, generar uno nuevo tiene que ser una decision
explicita (--rotar), no un accidente.

Es un SECRETO: no va al repo (.env esta en .gitignore, y .dockerignore lo mantiene fuera de
la imagen). En prod va en las variables de entorno de EasyPanel y NO se regenera en cada
rebuild. Usa un token DISTINTO en prod que en local.
"""
import argparse
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

# 32 bytes -> ~43 caracteres url-safe. Es un secreto compartido, no una password de humano:
# no hace falta que se pueda recordar, hace falta que no se pueda adivinar.
LARGO_BYTES = 32
VAR = "DASHBOARD_ADMIN_TOKEN"


def token_existente() -> str:
    """Devuelve el token ya configurado, o "" si no hay.

    Se lee con la MISMA mecanica que src/config.py (load_dotenv + os.environ) en vez de
    parsear el .env a mano: asi el script ve exactamente lo que vera la app, incluida una
    variable exportada en el shell o inyectada por el panel.
    """
    load_dotenv()  # no pisa variables ya presentes en el entorno
    return os.environ.get(VAR, "").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rotar", action="store_true",
                    help="generar uno nuevo aunque ya exista (invalida el anterior)")
    args = ap.parse_args()

    actual = token_existente()
    if actual and not args.rotar:
        # A proposito NO se imprime ni un fragmento del token existente: un secreto que
        # pasa por una terminal o un log deja de ser secreto.
        print(f"YA HAY UN {VAR} configurado. No se genera nada.\n")
        print("Si lo que necesitas es el valor actual, miralo en tu .env o en el panel de")
        print("EasyPanel — este script no lo puede recuperar, solo crear uno nuevo.\n")
        print("Si de verdad queres REEMPLAZARLO:")
        print("    python scripts/gen_admin_token.py --rotar\n")
        print("OJO: al reemplazarlo, el token viejo deja de funcionar al instante y todos")
        print("los que lo tengan guardado en el navegador van a recibir 401.")
        return 1

    token = secrets.token_urlsafe(LARGO_BYTES)
    if args.rotar and actual:
        print("ROTACION: se reemplaza el token existente. El anterior deja de servir en")
        print("cuanto pegues este en el .env / en el panel.\n")
    print("Token generado (guardalo, no se vuelve a mostrar):\n")
    print(f"{VAR}={token}\n")
    print("Local  -> pegalo en .env")
    print("Prod   -> variables de entorno del servicio en EasyPanel, y redesplega")
    print("\nSin esta variable el dashboard NO permite escribir (modo solo lectura).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
