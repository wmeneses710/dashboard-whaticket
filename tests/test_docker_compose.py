"""Los puertos del compose no se publican en todas las interfaces.

`ports: "5432:5432"` en Docker significa `0.0.0.0:5432`: el puerto queda escuchando en TODAS
las interfaces de la maquina, no solo en loopback. Para la BD de desarrollo eso expone un
Postgres con usuario y contraseña que estan escritos en este mismo archivo (`whaticket` /
`whaticket`) a cualquiera que alcance la maquina -- otra maquina de la LAN, una VPN, o
internet si el equipo esta en una red publica. Y Docker publica saltando el firewall del
host: las reglas que mete en la cadena DOCKER de iptables no pasan por INPUT, asi que un
`ufw deny 5432` NO lo tapa. Es la sorpresa clasica.

Prefijar `127.0.0.1:` deja el puerto solo para la propia maquina, que es todo lo que hace
falta en desarrollo: el navegador, los scripts del repo y el cliente de psql corren ahi.
Entre contenedores no cambia nada -- `api` habla con `db` por la red interna de compose
(`postgresql://...@db:5432/...`), que no depende de `ports` en absoluto.

OJO CON EL ALCANCE: este archivo es SOLO desarrollo (lo dice su primera linea). En EasyPanel
son App services y esto no corre. La exposicion de produccion es otra y se cierra en el
proxy, no aca.
"""
import pathlib
import re

COMPOSE = pathlib.Path(__file__).resolve().parents[1] / "docker-compose.yml"


def _publicaciones() -> list[str]:
    """Las entradas de `ports:` del compose, tal cual estan escritas."""
    texto = COMPOSE.read_text(encoding="utf-8")
    return re.findall(r'^\s*-\s*"([^"]+)"\s*$', texto, re.M)


def test_el_compose_existe_y_publica_puertos():
    assert COMPOSE.exists()
    assert _publicaciones(), "no se encontro ninguna publicacion de puertos"


def test_ningun_puerto_se_publica_en_todas_las_interfaces():
    sueltos = [p for p in _publicaciones()
               if re.match(r"^\d+:\d+$", p) or p.startswith("0.0.0.0:")]
    assert not sueltos, (
        f"estos puertos quedan en 0.0.0.0 y los alcanza cualquiera que llegue a la maquina "
        f"(Docker publica saltando el firewall del host): {sueltos}. "
        f"Prefijalos con 127.0.0.1:")


def test_la_base_de_datos_es_la_que_mas_importa():
    """La contraseña de la BD esta escrita en el propio compose: publicarla en todas las
    interfaces es entregar la copia entera."""
    publicados = [p for p in _publicaciones() if p.endswith(":5432")]
    assert publicados, "cambio el mapeo de la BD, revisar este test"
    for p in publicados:
        assert p.startswith("127.0.0.1:"), f"el Postgres de desarrollo esta expuesto: {p}"


# --- EL COMPOSE NO LLEVA CREDENCIALES ---------------------------------------------------
# Decision del negocio (2026-08-24): "en EasyPanel pongo yo las env, asi que quita todo lo que
# podemos cambiar en el env para evitar dar credenciales extra".
# El archivo esta VERSIONADO; `.env` no (git ls-files solo trae `.env.example`). Todo valor
# escrito aca queda en el historial de git para siempre, y ademas duplica la configuracion:
# quien la cambia en EasyPanel no cambia esto, y el archivo empieza a mentir sobre lo que
# corre. Los valores entran por interpolacion desde el entorno; los secretos con `:?` para
# que falte ruidosamente en vez de arrancar con un default silencioso -- la misma regla de
# `require_admin` en src/app.py, que falla cerrada.

_SECRETOS = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
             "DATABASE_URL", "OLLAMA_URL", "OLLAMA_MODEL")


def _texto() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def test_ninguna_credencial_esta_escrita_en_el_compose():
    for clave in _SECRETOS:
        m = re.search(rf"^\s*{clave}:\s*(\S.*)$", _texto(), re.M)
        assert m, f"{clave} desaparecio del compose, revisar este test"
        valor = m.group(1).strip()
        assert valor.startswith("${"), (
            f"{clave} tiene un valor literal en un archivo versionado: {valor!r}")


def test_los_secretos_fallan_CERRADO_si_faltan():
    """Un default silencioso arranca el servicio contra la base equivocada sin avisar."""
    for clave in _SECRETOS:
        m = re.search(rf"^\s*{clave}:\s*(\S.*)$", _texto(), re.M)
        assert ":?" in m.group(1), (
            f"{clave} usa un default en vez de exigir la variable: {m.group(1).strip()!r}")


def test_no_queda_ninguna_url_con_usuario_y_contrasena():
    assert not re.search(r"postgres(ql)?://[^\s${]+:[^\s@${]+@", _texto()), (
        "quedo una URL con credenciales embebidas")


def test_el_healthcheck_no_reintroduce_el_usuario_a_mano():
    """`pg_isready -U whaticket` es la misma credencial por la puerta de atras."""
    m = re.search(r"pg_isready[^\"]*", _texto())
    assert m, "cambio el healthcheck, revisar este test"
    assert "POSTGRES_USER" in m.group(0), f"usuario hardcodeado: {m.group(0)!r}"
