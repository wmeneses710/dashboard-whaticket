"""Derivar al agente es el PROCEDIMIENTO CORRECTO, y la rubrica lo castigaba.

Manual de ATC, cap. 06: "Si un jugador pertenece a un agente, el operador NO debe realizar
recargas ni retiros, excepto cuando exista una queja formal sobre el agente". Lo que
corresponde es direccionar al jugador a la plataforma y **proporcionarle el numero de
telefono del agente**.

MEDIDO el 2026-08-19 sobre la copia: 443 sesiones de `jugador` donde el operador deriva al
agente. En `deposito` son 152 con media 3,08 estrellas y **70 en 1 y 2 estrellas (46%)**.
Leidos 3 de esos 70 en orden y sin elegir, los 3 son el procedimiento correcto castigado:

    009312d9  2*  pide usuario, pide banco, deriva con telefono
                  -> "no aclaro el proceso de recarga ni ofrecio guia adicional"
    03566bc9  2*  "comuniquese con su agente 593959803754"
                  -> "no proporciono informacion clara sobre como realizar la recarga"
    09c1b759  2*  "tienes que contactarte con tu agente de confianza +593969563201"
                  -> "nunca le confirmo al cliente que la plata habia entrado"

LA CAUSA: la rubrica le exige al operador el paso que el manual le PROHIBE dar. El daño se
concentra en el camino determinista (56 de 93 sesiones de deposito en 1-2 estrellas, 60%,
contra 24% del camino LLM) porque ahi el comprobante del cliente activa `es_transaccion` y
la nota pasa a depender de una acreditacion que no se podia hacer.

POR QUE EL GATE EXIGE UN NUMERO AJENO, y no alcanza con la frase. La exencion no puede
apoyarse en lo que el operador DICE -- seria auto-otorgada, y "derivalo al agente" seria la
forma de esquivar cualquier deposito. Se apoya en lo que HACE: publicarle al cliente un
numero que no es de ninguna de nuestras lineas. Eso es un acto visible y auditable, y ademas
es LO QUE EL MANUAL PIDE (derivar *y* dar el numero): si el operador solo dijo "hable con su
agente" y no dio nada, tampoco cumplio el procedimiento y no hay nada que eximir.

Se probaron dos corroboraciones mas y las dos se descartaron MIDIENDO:
  - la etiqueta del contacto (`AGENTE` / `JUGADOR AFILIADO`): solo 5 de las 74 sesiones
    castigadas la tienen (7%). No alcanza.
  - `users` no guarda telefono (`metadata` es {isAway, awayReason, awayClearAt}), asi que
    no hay forma de saber desde la BD si un operador es ademas agente.

COBERTURA del gate sobre las 74 castigadas de deposito+retiro: 23 con numero AJENO (31%),
19 con numero NUESTRO (26%, que son redirecciones internas y NO son este caso) y 32 sin
numero (43%, que no cumplieron el procedimiento completo).

FALLA DEL LADO SEGURO: sin el mapa de lineas no se exime nada, igual que en
src/redireccion.py. Una exencion regalada esconderia un deposito sin atender.
"""
from datetime import datetime, timedelta, timezone

from src.signals import operador_derivo_al_agente

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)

# tail de 9 digitos -> status, igual que lo devuelve src.redireccion.build_lineas_map
LINEAS = {"991194168": "CONNECTED", "958949659": "DISCONNECTED"}

AGENTE_CON_NUMERO = ("Mi estimado, para sus cargas y retiros por favor nos ayudaría mucho "
                     "que se comunique con su agente de confianza 0963041121")


def _cli(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR",
            "media_type": media}


# --- el caso que motiva todo ------------------------------------------------------

def test_deriva_al_agente_con_su_numero():
    msgs = [_cli(0, "como hago la recarga"), _op(1, AGENTE_CON_NUMERO)]
    m = operador_derivo_al_agente(msgs, LINEAS)
    assert m is not None
    assert m["created_at"] == BASE + timedelta(minutes=1), "devuelve el mensaje que derivo"


def test_el_numero_puede_venir_en_el_mensaje_siguiente():
    # Es la forma mas comun en la data real: la frase y el numero van separados.
    msgs = [_cli(0, "quiero recargar"),
            _op(1, "Muy bien, comuníquese con su agente por favor"),
            _op(1, "593959803754")]
    m = operador_derivo_al_agente(msgs, LINEAS)
    assert m is not None
    assert m["body"].startswith("Muy bien"), "el reloj se mide desde la FRASE, no desde el numero"


def test_las_tres_formas_reales_de_decirlo():
    for frase in ("se comunique con su agente de confianza 0963041121",
                  "comuníquese con su agente por favor 593959803754",
                  "tienes que contactarte con tu agente de confianza +593969563201"):
        msgs = [_cli(0, "quiero recargar"), _op(1, frase)]
        assert operador_derivo_al_agente(msgs, LINEAS) is not None, frase


# --- los guards -------------------------------------------------------------------

def test_la_frase_SOLA_no_alcanza():
    """Sin numero el operador no completo el procedimiento del manual, que pide derivar Y
    dar el telefono. Y una exencion por la sola palabra seria auto-otorgada."""
    msgs = [_cli(0, "quiero recargar"), _op(1, "comuníquese con su agente por favor")]
    assert operador_derivo_al_agente(msgs, LINEAS) is None


def test_un_numero_NUESTRO_no_es_derivacion_al_agente():
    """Mandar a otra linea propia es `redireccion`, que es otro caso y ya tiene su regla.
    En la data son 19 de las 74 sesiones castigadas: confundirlos regalaria la exencion."""
    msgs = [_cli(0, "quiero recargar"),
            _op(1, "comuníquese con su agente 0991194168")]
    assert operador_derivo_al_agente(msgs, LINEAS) is None


def test_sin_mapa_de_lineas_no_se_exime_nada():
    """Falla del lado seguro, igual que src/redireccion.py: sin poder distinguir un numero
    nuestro de uno ajeno, una exencion regalada esconderia un deposito sin atender."""
    msgs = [_cli(0, "quiero recargar"), _op(1, AGENTE_CON_NUMERO)]
    assert operador_derivo_al_agente(msgs, None) is None
    assert operador_derivo_al_agente(msgs, {}) is None


def test_el_CLIENTE_nombrando_a_su_agente_no_cuenta():
    """La derivacion es un acto del OPERADOR. El cliente diciendo "mi agente no me responde"
    es justo el caso contrario: la queja formal que el manual exceptua."""
    msgs = [_cli(0, "mi agente no me responde, escribile al 0963041121"), _op(1, "Veamos")]
    assert operador_derivo_al_agente(msgs, LINEAS) is None


def test_una_nota_interna_no_cuenta():
    msgs = [_cli(0, "quiero recargar"),
            {"created_at": BASE + timedelta(minutes=1), "from_me": True, "is_note": True,
             "body": AGENTE_CON_NUMERO}]
    assert operador_derivo_al_agente(msgs, LINEAS) is None


def test_una_cifra_corta_no_es_un_telefono():
    """Un monto no puede producir un tail de 9 digitos. Mismo criterio que
    redireccion.tails_del_texto."""
    msgs = [_cli(0, "quiero recargar"),
            _op(1, "hablá con tu agente, el mínimo son 5 dólares")]
    assert operador_derivo_al_agente(msgs, LINEAS) is None


def test_una_linea_nuestra_DESCONECTADA_tampoco_es_derivacion_al_agente():
    """El numero sigue siendo NUESTRO. Que la linea este caida cambia el trato en
    `redireccion` (ahi si lleva nota), pero no lo convierte en el agente del cliente."""
    msgs = [_cli(0, "quiero recargar"), _op(1, "comuníquese con su agente 0958949659")]
    assert operador_derivo_al_agente(msgs, LINEAS) is None


# --- la rama en `deposito` --------------------------------------------------------

def test_deposito_derivado_ya_no_es_deficiente():
    """El caso `09c1b759`: el cliente manda el comprobante, el operador deriva al agente y
    la nota decia "nunca le confirmo al cliente que la plata habia entrado" -- plata que
    tenia PROHIBIDO mover."""
    from src.deposito import calificar_deposito
    msgs = [_cli(0, "hice mi recarga"), _cli(0, media="image"),
            _op(1, AGENTE_CON_NUMERO)]
    sin_mapa = calificar_deposito(msgs, None, None)
    con_mapa = calificar_deposito(msgs, None, LINEAS)
    assert (sin_mapa.stars, sin_mapa.label) == (2, "deficiente"), "el castigo viejo"
    assert (con_mapa.stars, con_mapa.label) == (4, "buena")
    assert con_mapa.derivo_al_agente is True
    assert "agente" in con_mapa.rationale


def test_deposito_derivado_TARDE_no_llega_a_4():
    """El tope de esta rama son 5 min y no 1: el manual exige pedir el usuario y verificar la
    agencia ANTES de derivar, y eso es una consulta. Ver la nota en calificar_deposito."""
    from src.deposito import calificar_deposito
    d = calificar_deposito([_cli(0, "hice mi recarga"), _cli(0, media="image"),
                            _op(11, AGENTE_CON_NUMERO)], None, LINEAS)
    assert (d.stars, d.label) == (3, "aceptable")


def test_deposito_derivado_NO_llega_a_5():
    """Techo en 4, igual que la rama del rechazo: el 5 es "el mejor escenario del motivo" y
    una recarga que ATC no podia hacer no lo es, por mas que se haya cerrado bien."""
    from src.deposito import calificar_deposito
    msgs = [_cli(0, "hice mi recarga"), _cli(0, media="image"),
            _op(1, AGENTE_CON_NUMERO),
            _op(2, "¿Hay algo mas en lo que te pueda ayudar?"), _cli(30, "no, gracias")]
    assert calificar_deposito(msgs, None, LINEAS).stars == 4


def test_si_acredito_IGUAL_manda_la_nota_normal():
    """Excepcion del manual: "si el jugador expresa que desea que le ayudemos con la recarga
    podemos proceder". Si el operador acredito, hizo el trabajo y la nota normal ya es justa
    -- la exencion no puede taparle un 5. En la data son 7 de 37 sesiones."""
    from src.deposito import calificar_deposito
    msgs = [_cli(0, "hice mi recarga"), _cli(0, media="image"),
            _op(1, AGENTE_CON_NUMERO),
            _op(1, "Igual te ayudo: tu saldo ya está disponible")]
    d = calificar_deposito(msgs, None, LINEAS)
    assert d.acredito is True
    assert d.derivo_al_agente is False, "la rama no se activa si acredito"
    assert d.stars >= 4


def test_deposito_el_consejo_habla_de_la_derivacion_y_no_del_rechazo():
    """Un consejo que pida "decile que dato corregir" sobre una recarga que no le correspondia
    manda al operador a hacer justo lo prohibido."""
    from src.deposito import score_deposito
    s = score_deposito([_cli(0, "hice mi recarga"), _cli(0, media="image"),
                       _op(11, AGENTE_CON_NUMERO)], None, LINEAS)
    assert "agente" in s.recomendacion
    assert "corregir" not in s.recomendacion


# --- la rama en `retiro` ----------------------------------------------------------

FORMULARIO = ("Monto a retirar: 30 Nombres: Alan Apellidos: Montaño "
              "Cedula: 0951964055 Banco: Guayaquil")


def test_retiro_derivado_ya_no_es_deficiente():
    """El manual prohibe a ATC procesar recargas Y RETIROS de un jugador de agente."""
    from src.retiro import calificar_retiro
    msgs = [_cli(0, FORMULARIO), _op(1, AGENTE_CON_NUMERO)]
    assert calificar_retiro(msgs, None, None).stars == 2, "el castigo viejo"
    r = calificar_retiro(msgs, None, LINEAS)
    assert (r.stars, r.label) == (4, "buena")
    assert r.derivo_al_agente is True


def test_retiro_con_comprobante_entregado_NO_entra_en_la_rama():
    """Si el operador pago igual, hizo el trabajo: la nota normal manda."""
    from src.retiro import calificar_retiro
    msgs = [_cli(0, FORMULARIO), _op(1, AGENTE_CON_NUMERO), _op(5, media="image")]
    r = calificar_retiro(msgs, None, LINEAS)
    assert r.derivo_al_agente is False
    assert r.entrega is not None
