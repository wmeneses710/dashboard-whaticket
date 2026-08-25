"""Tests de src/soporte.py: rubrica del motivo `soporte_cuenta`, 100% DETERMINISTA.

EL EJE lo cerro el negocio el 2026-08-05: **velocidad por MEDIANA + el INTENTO**.
Se saca la RESOLUCION a proposito, porque casi siempre ocurre fuera del chat
(desbloqueos, verificaciones, areas tecnicas): calificar el desenlace seria calificar
algo que el operador no controla. Lo que si controla es contestar rapido y hacer algo.

POR QUE LA MEDIANA Y NO EL PEOR TURNO. El peor turno mide CANTIDAD DE TURNOS, no
lentitud: medido el 2026-08-05, retiro con 2,0 turnos daba 63,5% de "peor<=2min" y
soporte con 4,5 turnos daba 36,6%, mientras la mediana se mantenia estable (71-85%)
en los seis motivos. Soporte es justamente el motivo de mas ida y vuelta, asi que el
peor turno lo castigaria por conversar.

ESCALA:
    5  mediana <=2 min + hizo algo concreto + se aseguro de que no faltara nada
    4  mediana <=2 min + hizo algo concreto
    3  mediana <=5 min
    2  mediana >5 min, o no intento nada
    1  no respondio

Umbrales sobre 56 sesiones (1 por persona): la mediana de espera por sesion es 1,1 min
y el 76,8% entra en 2 min. Es el motivo mas rapido de todos.
"""
from datetime import datetime, timedelta, timezone

from src.soporte import calificar_soporte, score_soporte

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)

PROBLEMA = "no puedo entrar a mi cuenta, me dice clave incorrecta"
PASO = "Ingresa a la web y toca 'olvide mi clave' para recuperarla"
ESCALO = "Ya escale tu caso al departamento tecnico, te aviso apenas responda"
ALGO_MAS = "¿Hay algo mas en lo que te pueda ayudar?"


def _cli(minutos, body=PROBLEMA, media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR",
            "media_type": media}


# --- la escala ---------------------------------------------------------------

def test_5_estrellas_rapido_con_accion_y_chequeo_de_cierre():
    msgs = [_cli(0), _op(1, PASO), _cli(3, "listo, ya entre"), _op(4, ALGO_MAS)]
    a = calificar_soporte(msgs)
    assert a.stars == 5 and a.label == "excelente"


def test_4_estrellas_rapido_con_accion_pero_sin_chequear():
    msgs = [_cli(0), _op(1, PASO)]
    a = calificar_soporte(msgs)
    assert a.stars == 4 and a.label == "buena"


def test_escalar_TAMBIEN_es_intentar():
    # La resolucion vive fuera del chat: escalar es lo maximo que puede hacer.
    msgs = [_cli(0), _op(1, ESCALO)]
    assert calificar_soporte(msgs).stars == 4


def test_3_estrellas_si_la_mediana_esta_entre_2_y_5():
    msgs = [_cli(0), _op(4, PASO)]
    a = calificar_soporte(msgs)
    assert a.stars == 3 and a.label == "aceptable"


def test_2_estrellas_si_la_mediana_pasa_de_5():
    msgs = [_cli(0), _op(9, PASO)]
    a = calificar_soporte(msgs)
    assert a.stars == 2 and a.label == "deficiente"


def test_2_estrellas_si_respondio_rapido_pero_NO_intento_nada():
    msgs = [_cli(0), _op(1, "ya lo estamos viendo"), _cli(5, "y?"),
            _op(6, "aguarde")]
    a = calificar_soporte(msgs)
    assert a.stars == 2


def test_1_estrella_si_no_respondio():
    assert calificar_soporte([_cli(0), _cli(4, "hola?")]).stars == 1


# --- la mediana, que es el nucleo del motivo ---------------------------------

def test_UN_turno_lento_no_hunde_una_sesion_por_lo_demas_agil():
    # Cinco turnos: cuatro de 1 min y uno de 20. El peor turno la mandaria a 2; la
    # mediana la deja donde corresponde.
    msgs = [_cli(0), _op(1, PASO),
            _cli(10), _op(11, PASO),
            _cli(20), _op(40, PASO),      # el turno lento
            _cli(50), _op(51, PASO),
            _cli(60), _op(61, PASO)]
    a = calificar_soporte(msgs)
    assert a.stars == 4, f"la mediana deberia mandar, no el peor turno ({a.rationale})"


def test_una_sesion_lenta_de_verdad_SI_baja():
    msgs = [_cli(0), _op(20, PASO), _cli(30), _op(55, PASO), _cli(60), _op(90, PASO)]
    assert calificar_soporte(msgs).stars == 2


# --- lo que NO puede pasar ---------------------------------------------------

def test_la_cortesia_sola_NO_alcanza_para_el_5():
    msgs = [_cli(0), _op(1, PASO),
            _op(2, "Un placer atenderte 😊✨ Gracias por preferirnos! 🍀💚")]
    assert calificar_soporte(msgs).stars == 4


def test_el_bot_no_cuenta_como_respuesta():
    bot = {"created_at": BASE + timedelta(minutes=1), "from_me": True,
           "is_note": False, "body": PASO, "sent_from": "CHATBOT", "media_type": "chat"}
    assert calificar_soporte([_cli(0), bot]).stars == 1


def test_sin_created_at_cede_el_turno():
    msgs = [{"from_me": False, "is_note": False, "body": PROBLEMA, "media_type": "chat"},
            {"from_me": True, "is_note": False, "body": PASO, "media_type": "chat"}]
    assert calificar_soporte(msgs) is None
    assert score_soporte(msgs) is None


def test_score_soporte_devuelve_un_ScoreResult_usable():
    msgs = [_cli(0), _op(1, PASO), _cli(3, "gracias"), _op(4, ALGO_MAS)]
    r = score_soporte(msgs)
    assert r.motivo == "soporte_cuenta"
    assert r.rating_label == "excelente" and r.stars == 5
    assert r.recomendacion == ""


def test_la_recomendacion_del_4_pide_chequear_el_cierre():
    msgs = [_cli(0), _op(1, PASO)]
    r = score_soporte(msgs)
    assert r.stars == 4 and "algo más" in r.recomendacion.lower()


# --- EL VOCABULARIO REAL DEL OPERADOR (auditoria del 2026-08-11) ------------------
# `_PASO_RE` se armo mirando pocos ejemplos y quedo corto: no tenia verificar, validar,
# confirmar, subir ni cambiar. Medido sobre las 1.234 sesiones de 2 estrellas marcadas
# "sin intento", 627 (50,8%) tienen un mensaje del operador con alguno de esos verbos.
# Caso `88793cdc`: el operador dice "suba la informacion directamente en la plataforma, si
# desea yo le puedo guiar" y el rationale afirma "ni un paso a seguir".

def test_los_verbos_de_instruccion_que_el_regex_no_veia():
    for paso in ("suba la informacion directamente en la plataforma",
                 "verifica tu cuenta con la cedula y te habilito",
                 "valida el correo que te llego",
                 "confirma los datos y lo reviso",
                 "cambia la clave desde el perfil",
                 "comunicate con atencion al cliente al 099"):
        r = calificar_soporte([_cli(0), _op(1, paso)])
        assert r.stars >= 4, f"{paso} -> {r.stars}★ {r.rationale}"


def test_un_verbo_NEGADO_no_es_un_paso():
    # El otro lado del filo: ampliar el vocabulario sin mirar la negacion acreditaria
    # "no puedo verificar tu cuenta" como si fuera una instruccion. La rubrica ya usa
    # este criterio en `operator_acreditacion` (la negacion invalida la frase).
    for no_paso in ("no puedo verificar tu cuenta desde aca",
                    "no se puede cambiar el usuario, lo siento",
                    "todavia no confirmo nada"):
        r = calificar_soporte([_cli(0), _op(1, no_paso)])
        assert r.stars == 2, f"{no_paso} -> {r.stars}★ {r.rationale}"


def test_la_PLANTILLA_de_cierre_no_es_un_paso():
    # FALSO POSITIVO del vocabulario ampliado, hallado el mismo dia. El root `comunic`
    # matchea el boilerplate "Gracias por comunicarte con nosotros", que esta en casi toda
    # sesion y no es una instruccion. Caso `7d562266-d4fd-4b58-9062-216a7c79c67c`: el
    # operador dice "Amigo ya fue atendido por la otra linea" -- no hizo nada -- y cerro con
    # la plantilla; la sesion saco 4 estrellas por "hizo algo concreto".
    # OJO: "Gracias por comunicarte con el DEPARTAMENTO de soporte" NO entra en este test.
    # Esa frase igual da 4 estrellas, pero por otro patron y preexistente: `_ESCALO_RE` tiene
    # el token `departamento`, asi que la plantilla se lee como si el caso se hubiera escalado.
    # Es un falso positivo distinto, no verificado sobre datos, y arreglarlo a ciegas se
    # llevaria escalados legitimos ("pase tu caso al departamento"). Queda anotado, sin tocar.
    for plantilla in ("Amigo ya fue atendido por la otra linea. Gracias por comunicarte con nosotros!",
                      "cuando quieras puedas comunicarte con nosotros"):
        r = calificar_soporte([_cli(0), _op(1, plantilla)])
        assert r.stars == 2, f"{plantilla} -> {r.stars}★ {r.rationale}"


def test_ofrecer_CREAR_una_cuenta_si_es_un_paso():
    # FALSO NEGATIVO: el operador diagnostico y ofrecio una salida concreta, y ningun regex
    # la reconocia. Caso `a35b8f53-781c-45b8-b1fc-28b7e77539ab`: "atencion al cliente me dice
    # que tu cuenta es esta luisbrito... pero es de otra agente, si deseas te hago una cuenta
    # con mi agencia" -> hubo_intento=False, 2 estrellas, y el coaching decia "El cliente no
    # se llevo ningun paso a seguir", que contradice el transcript.
    for oferta in ("si deseas te hago una cuenta con mi agencia",
                   "te creo una cuenta nueva y la usas",
                   "podemos crear una cuenta para que la pruebes"):
        r = calificar_soporte([_cli(0), _op(1, oferta)])
        assert r.stars >= 4, f"{oferta} -> {r.stars}★ {r.rationale}"


# --- EL TEXTO NO PUEDE AFIRMAR UNA REPETICION QUE NO HUBO --------------------
#
# `med = median(esperas)` con UN solo turno es esa muestra, no una tendencia. Aun asi el
# texto decia "en cada ida y vuelta" y "habitualmente en X", que afirman una repeticion
# inexistente. MEDIDO en la copia (2026-08-25): de 64 filas con "cada ida y vuelta", **18
# tienen UN solo mensaje del cliente**, y de 9 con "habitualmente", 4. Son 22 filas
# acusando de un patron que no ocurrio.
#
# El caso que lo trajo, `7704ebba` (Salome Ramirez): el cliente pregunto UNA vez, ella
# contesto completo a los 8,8 min, volvio 2 h despues a ofrecerse y cerro. La fila decia
# "el cliente espero 8,8 minutos en CADA ida y vuelta" y le ponia 2 estrellas.
#
# NO SE MUEVE NINGUNA NOTA: la mediana, la estrella y el umbral quedan igual. Cambia solo
# lo que la fila AFIRMA, que es lo que lee el supervisor.

def test_con_UN_turno_el_texto_va_en_singular():
    # Un pedido, una respuesta a los 8 min -> rama del 2 (mediana > 5 min).
    msgs = [_cli(0), _op(8, PASO)]
    a = calificar_soporte(msgs)
    assert a.stars == 2                       # la nota NO cambia
    assert "cada ida y vuelta" not in a.rationale
    assert "habitualmente" not in a.rationale
    assert "8 minutos" in a.rationale         # el dato sigue estando


def test_con_UN_turno_la_rama_del_3_tambien_va_en_singular():
    msgs = [_cli(0), _op(3, PASO)]            # mediana 3 min -> rama del 3
    a = calificar_soporte(msgs)
    assert a.stars == 3
    assert "cada ida y vuelta" not in a.rationale
    assert "3 minutos" in a.rationale


def test_con_UN_turno_la_rama_de_SIN_INTENTO_no_dice_habitualmente():
    # Contesta rapido pero sin ningun paso concreto -> rama `not intento`.
    msgs = [_cli(0), _op(1, "buenas tardes amigo")]
    a = calificar_soporte(msgs)
    assert a.stars == 2 and "no se llevó nada concreto" in a.rationale
    assert "habitualmente" not in a.rationale


def test_con_VARIOS_turnos_la_frase_plural_SE_CONSERVA():
    """El control: con varios turnos la mediana SI describe una tendencia, y la frase
    plural es la correcta. Sin este test, "arreglar el singular" podria borrarla siempre."""
    msgs = [_cli(0), _op(8, PASO), _cli(20, "sigo sin poder"), _op(28, PASO),
            _cli(40, "tampoco"), _op(48, PASO)]
    a = calificar_soporte(msgs)
    assert a.stars == 2
    assert "cada ida y vuelta" in a.rationale
