"""El umbral de primera respuesta es UN minuto, porque el manual de ATC dice un minuto.

Hasta v17 las seis rubricas deterministas usaban `AGIL = timedelta(minutes=2)`. Ese 2 no
salio de una politica: salio de la DISTRIBUCION OBSERVADA, y esta escrito en los propios
docstrings —info "mediana 1,5 min, 62,5% <=2 min", retiro "el 74,1% responde en <=2 min",
deposito "el 78,0% acusa en <=2 min"—. Calibramos contra lo que la gente ya hacia.

EL MANUAL DE ATC FIJA OTRA COSA, y lo dice DOS VECES (cap. 04 para jugadores y cap. 06
para agentes), con su razon tecnica: cuando el mensaje entra a Whaticket el sistema lo
marca como leido con el doble check azul, asi que el cliente YA SABE que lo leyeron.

    "la respuesta del operador debe ser inmediata y no superar un tiempo maximo
     de 1 minuto"

IMPACTO MEDIDO el 2026-08-19 recalculando las SEIS rubricas sobre la copia entera
(52.002 sesiones comparables, piso de ruido del arnes <1% salvo info con 5,3%):
**10.222 notas bajan (19,7%) y NINGUNA sube.** Por rubrica: agilidad 5.035, promo 1.980,
deposito 1.865, soporte_cuenta 654, info 447, retiro 241.

LO QUE ESTE CAMBIO NO HACE, y conviene tenerlo claro: el manual trata el minuto como un
MAXIMO —pasarse es un incumplimiento—, y aca sigue siendo el borde de la banda ALTA. Un
operador que contesta en 3 minutos sigue sacando 4 estrellas en agilidad y 3 en el resto.
Alinear la escala del todo (cumple / no cumple) es una decision del negocio, no un umbral.

NO SE TOCAN los otros relojes, porque el manual no los menciona: `retiro.ENTREGA_AGIL`
(15 min para el comprobante), `retiro.ENTREGA_TOPE` (30), `registro.ENTREGA_AGIL` (5 min
del traspaso de datos a las credenciales) ni `agilidad.GAP_BLOQUE` (15 min, que es
mecanica interna de armado de bloques y no una vara de calidad).
"""
from datetime import datetime, timedelta, timezone

from src.agilidad import calificar_agilidad
from src.deposito import calificar_deposito
from src.info import calificar_info
from src.promo import calificar_promo
from src.retiro import calificar_retiro
from src.soporte import calificar_soporte

import src.agilidad
import src.deposito
import src.info
import src.promo
import src.registro
import src.retiro
import src.soporte

# 15:00 Ecuador (UTC-5). Dentro del horario de operacion (06:00-23:59), para que
# `espera_efectiva` no descuente nada y el reloj del test sea el reloj real.
BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)

UN_MINUTO = timedelta(minutes=1)


def _cli(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR",
            "media_type": media}


# --- la constante, en las seis ----------------------------------------------------

def test_las_seis_rubricas_declaran_un_minuto():
    """Una sola vara para la primera respuesta. Si alguna se desincroniza, dos motivos
    juzgan la misma demora distinto y el ranking entre motivos deja de significar algo."""
    for mod in (src.agilidad, src.deposito, src.info,
                src.promo, src.retiro, src.soporte):
        assert mod.AGIL == UN_MINUTO, f"{mod.__name__}.AGIL deberia ser 1 min"


def test_los_relojes_que_el_manual_NO_menciona_quedan_donde_estaban():
    """El manual fija UN numero: el minuto de la primera respuesta. Todo lo demas se
    calibro con datos y sigue vigente; moverlo seria inventar politica."""
    assert src.retiro.ENTREGA_AGIL == timedelta(minutes=15)
    assert src.retiro.ENTREGA_TOPE == timedelta(minutes=30)
    assert src.agilidad.GAP_BLOQUE == timedelta(minutes=15)
    assert src.registro.ENTREGA_AGIL == timedelta(minutes=5)


# --- el borde, rubrica por rubrica ------------------------------------------------
# En todas: el minuto EXACTO entra en la banda alta (los bordes son inclusivos hacia
# la mejor banda, contrato ya fijado por tests/test_agilidad.py), y 2 minutos —que
# antes era el borde— ahora cae.

def test_agilidad_el_minuto_entra_y_los_dos_minutos_ya_no():
    assert calificar_agilidad([_cli(0, "Me ayuda con una recarga"), _op(1)]).stars == 5
    assert calificar_agilidad([_cli(0, "Me ayuda con una recarga"), _op(2)]).stars == 4


def test_deposito_el_minuto_entra_y_los_dos_minutos_ya_no():
    acuse = "Estamos verificando tu comprobante. Tu recarga se reflejara en breve."
    acredita = "Gracias por tu recarga. Tu saldo ya esta disponible."
    rapido = [_cli(0, media="image"), _op(1, acuse), _op(3, acredita)]
    lento = [_cli(0, media="image"), _op(2, acuse), _op(3, acredita)]
    assert calificar_deposito(rapido).stars == 4
    assert calificar_deposito(lento).stars == 3


def test_info_el_minuto_entra_y_los_dos_minutos_ya_no():
    consulta = "¿cuales son los horarios de atencion?"
    respuesta = "Atendemos de 6 de la mañana a medianoche, todos los dias."
    assert calificar_info([_cli(0, consulta), _op(1, respuesta)]).stars == 4
    assert calificar_info([_cli(0, consulta), _op(2, respuesta)]).stars == 3


def test_promo_el_minuto_entra_y_los_dos_minutos_ya_no():
    pregunta = "¿Como reclamo mis 10 giros?"
    explica = "Te cuento: con tu primera recarga se activan los 10 giros gratis."
    assert calificar_promo([_cli(0, pregunta), _op(1, explica)]).stars == 4
    assert calificar_promo([_cli(0, pregunta), _op(2, explica)]).stars == 3


def test_soporte_el_minuto_entra_y_los_dos_minutos_ya_no():
    problema = "no puedo entrar a mi cuenta, me dice clave incorrecta"
    paso = "Ingresa a la web y toca 'olvide mi clave' para recuperarla"
    assert calificar_soporte([_cli(0, problema), _op(1, paso)]).stars == 4
    assert calificar_soporte([_cli(0, problema), _op(2, paso)]).stars == 3


def test_retiro_el_minuto_entra_y_los_dos_minutos_ya_no():
    formulario = ("Monto a retirar: 30 Nombres: Alan Apellidos: Montaño "
                  "Cedula: 0951964055 Banco: Guayaquil")
    acuse = "Tu retiro esta en proceso 🔄. En breve te enviaremos el comprobante."
    rapido = [_cli(0, formulario), _op(1, acuse), _op(5, media="image")]
    lento = [_cli(0, formulario), _op(2, acuse), _op(5, media="image")]
    assert calificar_retiro(rapido).stars == 4
    assert calificar_retiro(lento).stars == 3


# --- el texto que lee el operador -------------------------------------------------

def test_ningun_consejo_le_sigue_pidiendo_dos_minutos():
    """El coaching es lo que el operador REALMENTE lee. Si la nota baja por pasarse del
    minuto y el consejo al lado dice "el objetivo son 2 minutos", la fila se desmiente
    sola — y esa contradiccion es exactamente la clase de bug que este repo ya pago caro
    (ver el rationale desmentido de v16).

    Se recorre el TEXTO de los consejos, no los docstrings: los docstrings que citan
    "<=2 min" son MEDICIONES historicas (que porcentaje respondia en 2 minutos) y son
    evidencia, no politica. Reescribirlas seria falsificar el registro.
    """
    # DOS FUENTES MIENTRAS DURE LA MIGRACION. `agilidad` ya vive en
    # src/catalogo_coaching.py (catalogo cerrado con codigo, 2026-08-21); las otras seis
    # todavia tienen su `_COACHING` local. Se recorren las dos para que el invariante no
    # pierda cobertura durante la mudanza, y para que cubra sola a la proxima que se migre.
    from src.catalogo_coaching import CONSEJOS, FRAGMENTOS

    for c in CONSEJOS:
        assert "2 minutos" not in c.texto, \
            f"catalogo_coaching {c.codigo} ({c.rubrica}/{c.situacion}) sigue prometiendo 2 minutos"
    for f in FRAGMENTOS:
        assert "2 minutos" not in f.texto, \
            f"catalogo_coaching {f.codigo} sigue prometiendo 2 minutos"
    for mod in (src.deposito, src.info, src.promo, src.retiro, src.soporte):
        for clave, texto in mod._COACHING.items():
            assert "2 minutos" not in texto, \
                f"{mod.__name__}._COACHING[{clave!r}] sigue prometiendo 2 minutos"


def test_ningun_rationale_le_sigue_pidiendo_dos_minutos():
    """Los consejos viven en `_COACHING`, pero el RATIONALE se arma con f-strings adentro
    de cada `calificar_*`. Es el texto que el supervisor lee primero, asi que se verifica
    sobre la salida real de una sesion que se pasa del minuto — no leyendo el codigo."""
    lentas = {
        "agilidad": calificar_agilidad([_cli(0, "Me ayuda con una recarga"), _op(3)]),
        "info": calificar_info([_cli(0, "¿cuales son los horarios?"), _op(3, "De 6 a 12")]),
        "promo": calificar_promo([_cli(0, "¿Como reclamo mis giros?"),
                                  _op(3, "Con tu primera recarga se activan")]),
        "soporte": calificar_soporte([_cli(0, "no puedo entrar a mi cuenta"),
                                      _op(3, "Toca 'olvide mi clave' para recuperarla")]),
    }
    for nombre, r in lentas.items():
        assert r is not None, nombre
        assert "2 minutos" not in r.rationale, \
            f"el rationale de {nombre} sigue prometiendo 2 minutos: {r.rationale!r}"
