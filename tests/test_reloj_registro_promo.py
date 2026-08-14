"""El reloj de `registro` y `promo` tampoco cobra la cola.

v15 hizo que el reloj arrancara cuando el operador PUEDE responder -no cuando entra el
mensaje- y lo aplico a `deposito`, `retiro` e `info`. `soporte` quedo afuera A PROPOSITO y
su changelog lo documenta ("la cola solo afecta al primer turno"). `registro` y `promo`
quedaron afuera SIN que ninguna decision lo registrara: es un olvido, no un criterio.

MEDIDO el 2026-08-14 sobre la copia con v15 corriendo, ejecutando `asignacion_at` sobre los
mensajes reales de las filas deterministas:

    promo     2.746 filas · 276 con cola sin descontar (>30 s) · 185 con cola >5 min
    registro  1.551 filas · 88 con cola sin descontar (>30 s) · 57 con cola >5 min

Proporcionalmente `promo` sufre MAS que el motivo que v15 vino a arreglar: 2,3% de filas
que cambian de banda contra el 1,1% medido en `deposito`.

Caso real `2603e73c` (promo, una SOLA interaccion -- no es el pendiente de la §10): 2
estrellas por "respondio recien 26,3 minutos despues"; descontada la cola son 11,7 y cruza
a 3. Caso `a8b99bbe` (registro): 3 estrellas por "tardo 19,7 minutos" con 161,7 minutos de
cola efectiva.

SIN NOTA DE ENTREGA NO SE DESCUENTA NADA. No se inventa una cola que no se puede probar:
es la misma regla que ya sostienen deposito/retiro/info.
"""
from datetime import datetime, timedelta, timezone

from src.promo import calificar_promo
from src.registro import calificar_registro

BASE = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)   # dentro del horario de atencion


def _cli(mins, body):
    return {"created_at": BASE + timedelta(minutes=mins), "from_me": False,
            "is_note": False, "body": body, "media_type": "chat"}


def _op(mins, body):
    return {"created_at": BASE + timedelta(minutes=mins), "from_me": True,
            "is_note": False, "body": body, "media_type": "chat", "sent_from": "OPERATOR"}


def _asignacion(mins):
    return {"created_at": BASE + timedelta(minutes=mins), "from_me": True,
            "is_note": True, "body": "*Asignado automáticamente* a Anya Alexandra",
            "media_type": None}


# --- REGISTRO: del traspaso de datos a las credenciales --------------------------

def _registro(con_asignacion_en=None):
    """Alta con 12 minutos de reloj crudo entre los datos y las credenciales."""
    msgs = [
        _cli(0, "hola quiero registrarme"),
        _op(1, "Ayudame con los datos: Nombre de usuario, correo electronico y numero de celular"),
        _cli(2, "Juan Perez, juan@mail.com, 0999367608"),
        _op(14, "Listo, tu usuario es juanp y tu clave es 1234"),
    ]
    if con_asignacion_en is not None:
        msgs.append(_asignacion(con_asignacion_en))
    return sorted(msgs, key=lambda m: m["created_at"])


def test_registro_sin_nota_de_entrega_cobra_la_espera_entera():
    r = calificar_registro(_registro())
    assert r is not None
    assert "12" in r.rationale, f"rationale={r.rationale}"


def test_registro_con_cola_probada_descuenta_la_espera():
    # El CRM entrega la conversacion 10 minutos despues del traspaso de datos: de los 12
    # minutos de reloj, 10 son cola y 2 son la reaccion real del operador.
    r = calificar_registro(_registro(con_asignacion_en=12))
    assert r is not None
    assert "12" not in r.rationale, f"la cola se sigue cobrando: {r.rationale}"
    assert "2" in r.rationale, f"rationale={r.rationale}"


def test_registro_cruza_de_banda_al_descontar_la_cola():
    # ENTREGA_AGIL son 5 minutos: 12 crudos quedan afuera, 2 netos entran.
    lento = calificar_registro(_registro())
    rapido = calificar_registro(_registro(con_asignacion_en=12))
    assert rapido.stars > lento.stars, f"{lento.stars} -> {rapido.stars}"


def test_registro_con_entrega_ANTERIOR_al_pedido_no_descuenta_nada():
    # El operador ya tenia la conversacion: la demora es entera suya.
    r = calificar_registro(_registro(con_asignacion_en=1))
    assert "12" in r.rationale, f"rationale={r.rationale}"


def test_registro_con_entrega_POSTERIOR_a_las_credenciales_no_descuenta_nada():
    """Una entrega que llega DESPUES de la entrega no pudo haber causado ninguna cola.

    MEDIDO el 2026-08-14: 6 de las 8 filas de `registro` que mejoraban con el descuento
    caian aca, y el texto salia "Creó la cuenta 0 segundos después de recibir los datos".
    El operador ya estaba trabajando la conversacion antes de que el CRM la formalizara:
    no hay cola que probar, y el principio de `inicio_del_reloj` es no inventarla.
    """
    r = calificar_registro(_registro(con_asignacion_en=20))   # credenciales en el minuto 14
    assert r.espera is not None
    assert r.espera.total_seconds() > 0, "no se puede reportar una espera de 0 segundos"
    assert "12" in r.rationale, f"rationale={r.rationale}"


# --- PROMO: del planteo del cliente a la primera respuesta -----------------------

def _promo(con_asignacion_en=None):
    """Consulta de promo con 12 minutos de reloj crudo hasta la primera respuesta."""
    msgs = [
        _cli(0, "hola queria saber del bono de bienvenida"),
        _op(12, "Te cuento: con tu primera recarga te damos un bono. Mira la promo acá"),
    ]
    if con_asignacion_en is not None:
        msgs.append(_asignacion(con_asignacion_en))
    return sorted(msgs, key=lambda m: m["created_at"])


def test_promo_sin_nota_de_entrega_cobra_la_espera_entera():
    r = calificar_promo(_promo())
    assert r is not None
    assert "12" in r.rationale, f"rationale={r.rationale}"


def test_promo_con_cola_probada_descuenta_la_espera():
    r = calificar_promo(_promo(con_asignacion_en=10))
    assert r is not None
    assert "12" not in r.rationale, f"la cola se sigue cobrando: {r.rationale}"


def test_promo_cruza_de_banda_al_descontar_la_cola():
    # RAZONABLE son 5 minutos: 12 crudos quedan afuera, 2 netos entran.
    lento = calificar_promo(_promo())
    rapido = calificar_promo(_promo(con_asignacion_en=10))
    assert rapido.stars > lento.stars, f"{lento.stars} -> {rapido.stars}"


def test_promo_con_entrega_ANTERIOR_al_planteo_no_descuenta_nada():
    r = calificar_promo(_promo(con_asignacion_en=0))
    assert "12" in r.rationale, f"rationale={r.rationale}"
