"""Tests de scripts/dump_jugadores_vip.py: el puente entre el reporte VIP y el CRM.

POR QUE EXISTE. El negocio quiere alertas cuando un jugador critico escribe, y para eso
hace falta saber A QUIEN vigilar. El reporte identifica al jugador por `username`, que es
un dato del CASINO y no existe en la base del CRM. Este puente decide a quien alerta el
webhook: si se rompe en silencio, la alerta deja de sonar para un jugador que si escribio
--o suena para cualquiera--, y en los dos casos nadie se entera.

MEDIDO sobre el reporte del 2026-08-21 (334 jugadores) contra la copia:
    telefono exacto ............ 108   `alta`
    username CON etiqueta ...... 111   `alta`
    username mencionado, 1 cto .. 32   `media`
    username ambiguo ............ 15   `baja`
    sin ningun rastro ........... 68   `ninguna`
"""
import importlib.util
import pathlib

_RUTA = pathlib.Path(__file__).parents[1] / "scripts" / "dump_jugadores_vip.py"
_spec = importlib.util.spec_from_file_location("dump_jugadores_vip", _RUTA)
vip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vip)


# --- EL TELEFONO: 140 de los 334 usernames SON un numero ---------------------

def test_el_username_numerico_se_traduce_al_formato_de_contacts():
    """`contacts.number` guarda 593 + 9 digitos. El reporte trae el formato local, con el
    cero adelante. VERIFICADO con un control por los ultimos 9 digitos: da el MISMO
    conjunto de 108, asi que la regla del prefijo no inventa ni pierde contactos."""
    assert vip._telefono_de("0981601125") == "593981601125"
    assert vip._telefono_de("0930911680") == "593930911680"


def test_un_username_de_letras_no_es_un_telefono():
    for u in ("quirozsabando", "vinic88", "cr7", "e1717900326", "1105013914ec"):
        assert vip._telefono_de(u) is None, u


# --- LA FICHA QUE CONSUME LA ALERTA ------------------------------------------

def _fila(username="quirozsabando", **kw):
    base = {"agencia": "ModoSorti", "rank": "1", "username": username,
            "player_id": "49846", "kyc": "si", "ggr_casa": "95.08", "turnover": "722.29",
            "dias_apuesta": "8", "apuestas_sport": "1", "dias_registro": "48",
            "motivo": "GGRx5", "depositos": "110.00", "retiros": "40.00"}
    base.update(kw)
    return base


def test_el_jugador_sin_rastro_queda_con_confianza_ninguna():
    """68 de los 334 no dejaron ni un mensaje. No se omiten: se listan en `ninguna`, para
    que se vea que fueron buscados y que la lista esta completa."""
    doc = vip.construir([_fila()], {}, "reporte.csv")
    j = doc["jugadores"][0]
    assert j["vinculo"]["confianza"] == "ninguna"
    assert j["vinculo"]["contactos"] == []
    assert doc["resumen"]["jugadores"] == 1


def test_la_ficha_NO_lleva_el_dato_financiero():
    """EL ARCHIVO SE VERSIONA, asi que lo que no se usa no entra. `ggr_casa`, `turnover`,
    `depositos`, `retiros` y `kyc` venian del reporte y **no los lee nadie** --ni el loader
    ni los dos mensajes--, pero son lo mas sensible que habia ahi: cuanto deposita, cuanto
    retira y cuanto pierde una persona identificable, en un repo, para siempre.

    Lo que no se usa no se guarda. Si algun dia hace falta, esta en el CSV de origen."""
    j = vip.construir([_fila()], {}, "reporte.csv")["jugadores"][0]
    for prohibido in ("ggr_casa", "turnover", "depositos", "retiros", "kyc"):
        assert prohibido not in j, prohibido


def test_la_ficha_lleva_lo_que_las_DOS_alertas_necesitan():
    """La de resumen necesita identificar al jugador y su peso; la de espera solo necesita
    resolver el contacto. Las dos arrancan en `contact_id`."""
    v = {"quirozsabando": {"confianza": "alta", "metodo": "telefono",
                           "contactos": [{"contact_id": "abc", "account": "sistemas",
                                          "numero": "593981601125"}],
                           "mensajes": 12, "ultimo_mensaje": "2026-08-25"}}
    j = vip.construir([_fila()], v, "reporte.csv")["jugadores"][0]
    assert j["username"] == "quirozsabando" and j["player_id"] == "49846"
    assert j["agencia"] == "ModoSorti" and j["rank"] == 1 and j["motivo"] == "GGRx5"
    assert j["vinculo"]["contactos"][0]["contact_id"] == "abc"


def test_un_jugador_puede_tener_UN_contacto_POR_CUENTA():
    """El mismo numero vive en `datos` y en `sistemas` con filas de contacto distintas:
    son 356 numeros. El webhook tiene que vigilar las dos, asi que `contactos` es una
    LISTA y cada entrada dice de que cuenta es."""
    v = {"quirozsabando": {"confianza": "alta", "metodo": "telefono", "contactos": [
        {"contact_id": "a1", "account": "datos", "numero": "593988662475"},
        {"contact_id": "b2", "account": "sistemas", "numero": "593988662475"}],
        "mensajes": 8370, "ultimo_mensaje": "2026-08-25"}}
    j = vip.construir([_fila()], v, "reporte.csv")["jugadores"][0]
    assert {c["account"] for c in j["vinculo"]["contactos"]} == {"datos", "sistemas"}


def test_el_resumen_cuenta_por_confianza():
    """`alta` se alerta sin pensar; `baja` es casi seguro una palabra comun. El resumen
    esta arriba del archivo para que la diferencia no se lea recorriendo 334 fichas."""
    v = {"a": {"confianza": "alta", "metodo": "telefono", "contactos": [], "mensajes": 1,
               "ultimo_mensaje": None},
         "b": {"confianza": "baja", "metodo": "username_ambiguo", "contactos": [],
               "mensajes": 3, "ultimo_mensaje": None}}
    doc = vip.construir([_fila("a"), _fila("b"), _fila("c")], v, "reporte.csv")
    assert doc["resumen"]["por_confianza"] == {"alta": 1, "baja": 1, "ninguna": 1}


def test_un_RANK_ilegible_no_revienta_la_ficha():
    """El reporte viene de una exportacion: una celda vacia o con texto no puede tumbar el
    dump entero. `rank` es el unico numero que sobrevive al recorte, asi que es el unico
    que puede venir sucio."""
    j = vip.construir([_fila(rank="—")], {}, "r.csv")["jugadores"][0]
    assert j["rank"] is None
    j2 = vip.construir([_fila(rank="")], {}, "r.csv")["jugadores"][0]
    assert j2["rank"] is None
