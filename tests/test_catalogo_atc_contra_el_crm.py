"""Las respuestas rapidas que NOMBRAMOS tienen que existir en el CRM, con esa grafia.

POR QUE ESTE ARCHIVO EXISTE, Y POR QUE NECESITA BD. El guard que ya habia
(`test_coaching.test_ninguna_respuesta_rapida_del_coaching_esta_inventada`) compara el
coaching contra `catalogo_atc.RESPUESTAS_RAPIDAS`, que es **nuestra propia lista escrita a
mano** desde el manual. Verifica nuestra lista contra nuestra lista: es circular, y por eso
no pudo cazar nada de esto (2026-08-28, todo verificado contra `fast_responses`):

    lo que mostrabamos            lo que el operador tiene en Whaticket
    /R2verificaciondeboleta       R2VERIFICACIONDEBOLETA
    /R3Recarga                    R3RECARGA
    /Bienvenida                   BIENVENIDA
    /contacto no registrado       CONTACTO NO REGISTRADO
    /VerificarCuenta              VERIFICARCUENTA
    /R1solicituddecarga           R1SOLICITUDDECARGA
    /FIN                          FIN
    /R5Placer                     R5PLACER
    /Visto                        VISTO
    /Link afiliado nuevo jugador  NO EXISTE

`store.py` ya habia anotado exactamente que faltaba para cerrar el circulo: *"el manual
nombra las respuestas rapidas pero NO incluye su TEXTO CANONICO"*. Eso llego: el ETL trae
`fast_responses` (180 filas en `sistemas`). El unico chequeo que vale es contra el CRM, y el
CRM esta en la BD — de ahi el skip sin `DATABASE_URL`, mismo patron que los de integracion
del ETL.

LA BARRA NO ES UN ADORNO NUESTRO NI UN PREFIJO UNIFORME. En el catalogo real hay shortcuts
CON barra (`/000`, `/888ALE`, `/agenteverificacion`, `/tuverificacion`) y sin ella
(`FIN`, `BIENVENIDA`, `R3RECARGA`). Agregarla o quitarla "para que se lea mejor" es
inventar un nombre: el operador la busca en su lista y no la encuentra. Se copia verbatim.

POR QUE LA COMPARACION ES SENSIBLE A MAYUSCULAS. Es lo que el operador tiene que TIPEAR.
Y de paso hace seguro nombrar `FIN` en prosa: en minusculas, un `in` contra "fin" matchearia
"finalmente" o "definir".
"""
import os

import pytest

from src.catalogo_atc import RESPUESTAS_RAPIDAS
from src.catalogo_coaching import CONSEJOS

psycopg = pytest.importorskip("psycopg")


@pytest.fixture(scope="module")
def dsn():
    """El DSN, LEIDO AL CORRER Y NO AL IMPORTAR.

    Con `pytestmark = skipif(not os.environ.get(...))` a nivel de modulo esto quedaba
    dependiente del ORDEN DE COLECCION: `src/config.py` llama `load_dotenv()`, asi que
    cualquier test que lo importe antes (por ejemplo `test_app.py`) deja `DATABASE_URL` en el
    ambiente y estos tests corren; solos, se saltean. Los mismos cinco tests dando SKIP o
    PASS segun con quien los corras es la peor forma de tener un guard.
    """
    valor = os.environ.get("DATABASE_URL", "")
    if not valor:
        pytest.skip("DATABASE_URL no configurado")
    return valor


@pytest.fixture(scope="module")
def shortcuts_del_crm(dsn):
    """Los shortcuts REALES, verbatim. Sin acotar por cuenta: las dos comparten operadores
    (siete personas tienen una fila en `users` por cuenta), asi que una plantilla de
    `sistemas` es nombrable igual."""
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT shortcut FROM fast_responses WHERE shortcut IS NOT NULL")
        return {r[0] for r in cur.fetchall()}


def test_el_crm_tiene_catalogo(shortcuts_del_crm):
    """CONTROL DEL CONTROL. Sin esto, un `fast_responses` vacio haria pasar todos los tests
    de abajo por la razon equivocada -- la regla del cero que ya nos costo caro."""
    assert len(shortcuts_del_crm) > 50, (
        f"solo {len(shortcuts_del_crm)} shortcuts en `fast_responses`: el catalogo no se "
        f"sembro y los asserts de abajo no prueban nada"
    )


def test_ninguna_respuesta_rapida_QUE_NOMBRAMOS_esta_inventada(shortcuts_del_crm):
    """El chequeo que el guard viejo no podia hacer: contra el CRM, no contra nosotros."""
    faltan = sorted(rr for rr in RESPUESTAS_RAPIDAS if rr not in shortcuts_del_crm)
    assert not faltan, (
        f"nombramos respuestas rapidas que el CRM no tiene: {faltan}. El operador las va a "
        f"buscar en Whaticket y no las va a encontrar — y ademas es el error critico E10 "
        f"del manual ('alterar respuestas rapidas... o informacion oficial')."
    )


def test_la_grafia_es_EXACTA_no_solo_parecida(shortcuts_del_crm):
    """Lo que se rompio: `/R3Recarga` contra `R3RECARGA`. Normalizando barra y mayusculas
    las dos son 'la misma', y justamente por eso el chequeo laxo no servia."""
    def clave(s):
        return s.replace("/", "").replace(" ", "").upper()

    por_clave = {}
    for real in shortcuts_del_crm:
        por_clave.setdefault(clave(real), []).append(real)

    errores = []
    for nuestro in RESPUESTAS_RAPIDAS:
        if nuestro in shortcuts_del_crm:
            continue
        candidatos = por_clave.get(clave(nuestro))
        if candidatos:
            errores.append(f"{nuestro!r} -> deberia decir {sorted(candidatos)!r}")
    assert not errores, "grafia distinta a la del CRM:\n  " + "\n  ".join(errores)


def test_el_coaching_solo_nombra_plantillas_que_existen(shortcuts_del_crm):
    """El texto que el supervisor LEE en el tablero. Es el que le llego al negocio con
    `/R2verificaciondeboleta` y `/R3Recarga`, ninguna de las dos existente asi."""
    fallos = []
    for c in CONSEJOS:
        for nombre in RESPUESTAS_RAPIDAS:
            if nombre not in c.texto:
                continue
            if nombre not in shortcuts_del_crm:
                fallos.append(f"{c.codigo}: nombra {nombre!r}, que no esta en el CRM")
    assert not fallos, "\n  ".join(fallos)


def test_R3RECARGA_es_la_ACREDITACION_y_no_la_carga_en_curso(dsn, shortcuts_del_crm):
    """EL ERROR QUE NO ERA DE PRESENTACION. Nuestro catalogo decia que `R3RECARGA` sirve para
    'confirmar que la recarga esta en curso'. Su texto real es 'Tu saldo ya está disponible':
    es la acreditacion CONSUMADA. Aconsejar mandarla mientras la carga esta en curso le
    miente al cliente y ademas hace que `operator_acreditacion` marque acredito=True sin que
    la plata haya entrado (verificado: la senal la lee como ACREDITO).

    El momento 'en curso' lo cubren `R1SOLICITUDDECARGA` ('esta siendo procesada') y
    `R2VERIFICACIONDEBOLETA` ('se reflejara en breve')."""
    from src.signals import operator_acreditacion

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT shortcut, message FROM fast_responses "
                    "WHERE shortcut IN ('R3RECARGA', 'R2VERIFICACIONDEBOLETA')")
        textos = dict(cur.fetchall())

    def acredita(texto):
        return operator_acreditacion([{"from_me": True, "is_note": False, "body": texto,
                                       "created_at": None}])

    assert acredita(textos["R3RECARGA"]), (
        "R3RECARGA dejo de leerse como acreditacion: revisar si el negocio le cambio el texto"
    )
    assert not acredita(textos["R2VERIFICACIONDEBOLETA"]), (
        "R2VERIFICACIONDEBOLETA se lee como acreditacion y es el ACUSE"
    )
    # Y nuestra descripcion tiene que estar del lado correcto.
    descripcion = RESPUESTAS_RAPIDAS["R3RECARGA"].lower()
    assert "en curso" not in descripcion, (
        f"seguimos describiendo R3RECARGA como 'en curso': {descripcion!r}"
    )
