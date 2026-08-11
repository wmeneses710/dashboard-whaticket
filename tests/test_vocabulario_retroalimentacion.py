"""El texto que LEE el operador tiene que hablar SU idioma.

POR QUE EXISTE. El equipo de atencion al cliente no entendia su propia retroalimentacion:
la etiqueta del tablero decia "mando el flyer o el enlace" y ellos no usan ninguno de los
dos artefactos. Y el repo ya lo tenia documentado en otra punta — `src/registro.py` dice
"NO existe un link de registro", y `src/recommendations.py` cuenta que el 2026-08-06 se
retiro el fragmento del enlace de registro porque el negocio lo marco FALSO.

Y NO LO ESCRIBE EL MODELO. `promo._COACHING[4]` es texto DETERMINISTA: medido el
2026-08-11, **5.595 de 11.714 recomendaciones de `promo` (47,8%) mencionan enlace, y las
MISMAS 5.595 mencionan flyer**. Es la misma forma de bug que el fragmento del afiliado:
"no lo escribia el modelo, lo anteponia este modulo, por eso ningun ajuste de prompt lo
sacaba".

QUE SE PUEDE AFIRMAR Y QUE NO. Del adjunto conocemos la FORMA (`media_type`, el dominio de
la URL), NUNCA el contenido: no hay forma de saber si esa imagen es un flyer de la promo o
una foto cualquiera. Entonces el texto dice lo que sabemos — que le mando algo concreto
ademas de texto — y nada mas. Criterio del usuario: "no quiero inferir en media porque no
tenemos acceso a su contexto".
"""
import pathlib

FALSOS = ("flyer", "enlace de registro", "codigo de afiliado", "código de afiliado")
HTML = pathlib.Path(__file__).resolve().parents[1] / "web" / "index.html"


def test_el_coaching_de_promo_no_nombra_artefactos_que_el_equipo_no_usa():
    from src.promo import _COACHING, _COACHING_1
    for nota, texto in list(_COACHING.items()) + [(1, _COACHING_1)]:
        bajo = texto.lower()
        for falso in FALSOS:
            assert falso not in bajo, f"_COACHING[{nota}] dice {falso!r}: {texto}"


def test_los_rationales_de_promo_no_nombran_el_flyer():
    # Los dos textos que el operador lee en el tablero: el del 5 (mando material) y el del
    # 4 (no mando). Son los que mas se repiten de todo el motivo.
    from datetime import datetime, timedelta, timezone

    from src.promo import calificar_promo
    base = datetime(2026, 3, 10, 20, 0, tzinfo=timezone.utc)

    def _cli(seg):
        return {"created_at": base + timedelta(seconds=seg), "from_me": False,
                "is_note": False, "body": "que promos tienen?", "media_type": "chat"}

    def _op(seg, media="chat"):
        return {"created_at": base + timedelta(seconds=seg), "from_me": True,
                "is_note": False, "body": "tenemos un bono del 100% en tu primera carga",
                "sent_from": "OPERATOR", "media_type": media}

    con_material = calificar_promo([_cli(0), _op(30), _op(31, media="image")])
    sin_material = calificar_promo([_cli(0), _op(30)])
    assert con_material.stars == 5 and sin_material.stars == 4
    for p in (con_material, sin_material):
        bajo = p.rationale.lower()
        for falso in FALSOS:
            assert falso not in bajo, f"{p.stars}★ dice {falso!r}: {p.rationale}"


def test_la_etiqueta_del_tablero_no_nombra_el_flyer():
    html = HTML.read_text(encoding="utf-8").lower()
    assert "mando_material" in html, "cambio el nombre del hecho, revisar este test"
    for falso in FALSOS:
        assert falso not in html, f"web/index.html todavia dice {falso!r}"


def test_los_ejemplos_del_recomendador_no_ensenan_el_enlace():
    # `_RECOM_EXAMPLES` son los few-shot del sub-evaluador de recomendaciones y el propio
    # modulo los llama "el lever real contra la genericidad": si ahi dice "envia el enlace",
    # el modelo lo copia. Para `promo` decia "envia el enlace de REGISTRO", que es doblemente
    # falso porque el registro lo hace el operador.
    from src.subeval import _RECOM_EXAMPLES, _RECOM_SYSTEM
    for motivo, ejemplos in _RECOM_EXAMPLES.items():
        for ej in ejemplos:
            assert "enlace" not in ej.lower(), f"ejemplo de {motivo!r}: {ej}"
    # El prompt SI puede nombrar el enlace — pero para PROHIBIRLO, no como modelo de
    # redaccion. La distincion importa: la palabra suelta no es el bug, usarla de ejemplo si.
    bajo = _RECOM_SYSTEM.lower()
    assert "envia el enlace" not in bajo and "envía el enlace" not in bajo, \
        "el prompt pone 'envia el enlace' como ejemplo de buena redaccion"
    assert "nunca hables de un \"enlace de registro\"" in bajo, \
        "falta la prohibicion explicita: sin ella el modelo lo reinventa"
