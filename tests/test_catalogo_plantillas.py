"""Identificar QUE respuesta rapida uso el operador, con el catalogo real del CRM.

POR QUE HACE FALTA UN MODULO NUEVO Y NO ALCANZA `src/plantillas.py`. Ese modulo decide por
CANAL (`sent_from`) y el 2026-08-28 quedo probado que el canal NO ve las respuestas rapidas:
de 14.789 mensajes que el canal llamaba plantilla, **CERO** matchean una plantilla real. Las
respuestas rapidas salen por `WEB`, mezcladas con el texto libre. La unica forma de
reconocerlas es por TEXTO, contra el catalogo que el ETL ahora trae en `fast_responses`.

Y por texto funciona en CUALQUIER CANAL, que es lo que destapa Facebook: ahi `sent_from`
nunca es NULL y el detector viejo es ciego.

EL CRITERIO DE ADMISION SALE DEL DATO. Una plantilla corta matchea prosa cualquiera, asi que
hay que descartar las poco distintivas. Medido sobre 12.000 mensajes del operador de 30 dias
(canales WHATSAPP y FACEBOOK), probando cuatro criterios:

    criterio                plantillas   msgs tocados   pierde
    largo total >= 60           163       2.302 (19,2%)   --
    tramo literal >= 25         177       2.307 (19,2%)   --
    tramo literal >= 35         174       2.307 (19,2%)   --      <- elegido
    tramo literal >= 45         165       2.221 (18,5%)   FIN

Se mide el TRAMO LITERAL (el texto entre placeholders) y no el largo total, porque `/FIN`
son 64 caracteres de los cuales **20 son `{{contactTreatment}}`**: con un umbral sobre el
largo total se descarta justo la plantilla que el manual nombra primero. Y 45 la pierde
igual. 35 conserva las seis plantillas del manual sin aflojar la cobertura.

CUIDADO CON NORMALIZAR DE MAS, que ya me costo un numero falso. La primera version sacaba
`^[A-Za-z ]{1,30}:` para quitar la firma del operador, y con eso se comia el ENCABEZADO de
las plantillas ("Monto a retirar:", "Te llevas:") — las dejaba cortas y hacian match con
prosa: reporte 35,3% de cobertura y el real es 19,2%. Es exactamente la trampa que
`src/operators.py` ya tenia anotada. Por eso la firma se saca con `nombre_de_firma`, que
discrimina un nombre de persona de un encabezado de plantilla, y no con una regex propia.
"""
from datetime import datetime, timezone

from src.catalogo_plantillas import (
    TRAMO_MINIMO,
    build_plantillas_map,
    normalizar,
    plantilla_de,
    plantilla_mas_parecida,
    similitud,
)

T = datetime(2026, 8, 20, 22, 4, 38, tzinfo=timezone.utc)

# Textos REALES de `fast_responses` (cuenta `sistemas`), tal cual los trae el ETL.
CATALOGO = [
    # shortcut, message, account, updated_at
    ("FIN", "{{contactTreatment}} ¿Hay algo más en lo que te pueda ayudar? 🙂🍀",
     "sistemas", T),
    ("R2VERIFICACIONDEBOLETA",
     "Estamos verificando tu comprobante. Tu recarga se reflejará en breve. 🍀",
     "sistemas", T),
    ("R5PLACER",
     "Un placer atenderte 😊. Si tienes alguna otra duda o solicitud, estamos aquí para "
     "ayudarte en todo momento.🍀", "sistemas", T),
    ("FORMULARIODERETIRO",
     "Monto a retirar:\nNombres:\nApellidos: \nCédula:\nBanco:\nTipo de cuenta:\n"
     "Número de cuenta:", "sistemas", T),
    ("1Momento",
     "Permíteme un momento por favor, mientras realizo las verificaciones "
     "correspondientes.", "sistemas", T),
    # CORTA a proposito: 'Gracias 🍀' no puede convertirse en un patron que matchee prosa.
    ("CORTA", "Gracias 🍀", "sistemas", T),
]


class _Cursor:
    """Cursor de mentira con la forma que devuelve `fast_responses`."""

    def __init__(self, filas):
        self._filas = filas
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return list(self._filas)


def _mapa(filas=None):
    return build_plantillas_map(_Cursor(filas if filas is not None else CATALOGO))


def _msg(body, *, from_me=True, is_note=False):
    return {"created_at": T, "from_me": from_me, "is_note": is_note, "body": body,
            "sent_from": "WEB", "media_type": "chat"}


# --- el mapa --------------------------------------------------------------------------

def test_el_mapa_descarta_las_plantillas_poco_distintivas():
    """Una plantilla corta matchearia prosa cualquiera: no entra."""
    mapa = _mapa()
    assert "CORTA" not in mapa, (
        f"'Gracias 🍀' entro al mapa: cualquier mensaje amable va a matchearla "
        f"(el minimo es {TRAMO_MINIMO} caracteres de tramo literal)"
    )
    assert "FIN" in mapa, "se descarto /FIN, que es la plantilla que el manual nombra primero"


def test_el_mapa_conserva_updated_at_porque_el_catalogo_es_el_estado_de_HOY():
    """Los mensajes son HISTORICOS y el catalogo es el de ahora: una plantilla editada ayer
    no describe un mensaje de hace dos meses, y su texto viejo NO se puede reconstruir. El
    llamador necesita la fecha para decidir; el mapa no decide por el."""
    assert _mapa()["FIN"]["updated_at"] == T


def test_el_mapa_acota_por_cuenta_cuando_se_lo_piden():
    cur = _Cursor(CATALOGO)
    build_plantillas_map(cur, account="sistemas")
    assert cur.params == ("sistemas",), "no filtro por cuenta"


# --- identificar la plantilla ---------------------------------------------------------

def test_reconoce_la_plantilla_con_el_placeholder_YA_SUSTITUIDO():
    """La guardada dice `{{contactTreatment}}`; la que sale al aire dice 'Estimado'. Sin
    compilar el placeholder a comodin, el match exacto falla en 45 de 178 plantillas."""
    m = _msg("Estimado ¿Hay algo más en lo que te pueda ayudar? 🙂🍀")
    assert plantilla_de(m, _mapa()) == "FIN"


def test_reconoce_la_plantilla_detras_de_la_firma_del_operador():
    """El CRM prefija `*Nombre:*` en los mensajes firmados y eso no cambia que la plantilla
    es la misma."""
    m = _msg("*Michelle:* Estamos verificando tu comprobante. Tu recarga se reflejará en "
             "breve. 🍀")
    assert plantilla_de(m, _mapa()) == "R2VERIFICACIONDEBOLETA"


def test_el_ENCABEZADO_de_la_plantilla_no_se_confunde_con_una_firma():
    """LA TRAMPA QUE YA ESTABA ANOTADA en src/operators.py: 'Monto a retirar:' cumple las
    tres guardas del patron de firma (<=3 palabras, solo letras, ':' cerrando la linea).
    Sacarlo como si fuera una firma deja la plantilla sin su primera linea."""
    m = _msg("Monto a retirar:\nNombres:\nApellidos: \nCédula:\nBanco:\n"
             "Tipo de cuenta:\nNúmero de cuenta:")
    assert plantilla_de(m, _mapa()) == "FORMULARIODERETIRO"
    assert normalizar(m["body"]).startswith("monto a retirar:"), (
        "la normalizacion se comio el encabezado del formulario tratandolo como firma"
    )


def test_el_texto_libre_no_es_ninguna_plantilla():
    assert plantilla_de(_msg("ya se encuentra activo el saldo"), _mapa()) is None


def test_gana_la_plantilla_MAS_ESPECIFICA():
    """El match no es 1-a-1: en el catalogo real `/michelle verificar comprobante` y
    `R2VERIFICACIONDEBOLETA` matchean LOS MISMOS 986 mensajes. Gana la que aporta mas texto
    literal, que es la que explica mejor el mensaje."""
    corta = "Estamos verificando tu comprobante. Tu recarga se reflejará en breve. 🍀"
    larga = corta + " Te avisamos en cuanto se acredite, no hace falta que escribas de nuevo."
    mapa = _mapa(CATALOGO + [("LARGA", larga, "sistemas", T)])
    assert plantilla_de(_msg(larga), mapa) == "LARGA"


def test_sin_mapa_no_revienta_y_no_afirma_nada():
    """Falla del lado seguro, igual que `lineas` en src/redireccion.py: sin catalogo la
    señal se apaga, no inventa."""
    assert plantilla_de(_msg("Estimado ¿Hay algo más en lo que te pueda ayudar? 🙂🍀"),
                        None) is None


def test_el_mensaje_del_cliente_y_la_nota_no_son_plantillas():
    """La pregunta es sobre el trabajo del OPERADOR."""
    texto = "Estimado ¿Hay algo más en lo que te pueda ayudar? 🙂🍀"
    mapa = _mapa()
    assert plantilla_de(_msg(texto, from_me=False), mapa) is None
    assert plantilla_de(_msg(texto, is_note=True), mapa) is None


# --- E10: la plantilla ALTERADA -------------------------------------------------------

def test_similitud_da_uno_cuando_el_texto_es_la_plantilla():
    m = _msg("Permíteme un momento por favor, mientras realizo las verificaciones "
             "correspondientes.")
    assert similitud(m, "1Momento", _mapa()) == 1.0


def test_E10_necesita_SIMILITUD_y_no_un_booleano():
    """EL PUNTO DEL MODULO PARA E10. Un mensaje ALTERADO, por construccion, NO matchea su
    plantilla: `plantilla_de` devuelve None y eso NO distingue 'la altero' de 'escribio
    libre'. La similitud si: la version alterada queda alta pero por debajo de 1, y el
    texto ajeno queda lejos."""
    alterada = _msg("Permiteme un momentito por favor, mientras hago las verificaciones")
    assert plantilla_de(alterada, _mapa()) is None, (
        "si el match exacto agarrara la version alterada, E10 seria indetectable"
    )
    sc, ratio = plantilla_mas_parecida(alterada, _mapa())
    assert sc == "1Momento", f"no reconocio que estaba usando 1Momento (dijo {sc!r})"
    assert 0.6 < ratio < 1.0, f"ratio fuera de rango para una alteracion: {ratio}"

    ajeno = _msg("ya se encuentra activo el saldo, mucha suerte")
    _, ratio_ajeno = plantilla_mas_parecida(ajeno, _mapa())
    assert ratio_ajeno < ratio, (
        "el texto libre quedo mas parecido que la plantilla alterada: el ratio no separa"
    )


def test_plantilla_mas_parecida_sin_mapa_no_afirma_nada():
    assert plantilla_mas_parecida(_msg("cualquier cosa"), None) == (None, 0.0)
