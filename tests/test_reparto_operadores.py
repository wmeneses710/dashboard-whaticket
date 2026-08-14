"""Una fila que la trabajaron varios operadores tiene que DECIRLO.

EL CONTRATO DEL TABLERO: una fila de `conversation_scores` = una nota = UN operador. El
tablero existe para validar la interaccion OPERADOR->CLIENTE, y cada interaccion se le
asigna a alguien. Pero una sesion que el CRM reabre puede tener varias interacciones con
operadores DISTINTOS, y la nota se le carga a uno solo (el de mas mensajes).

MEDIDO el 2026-08-14 sobre v15, separando las tres poblaciones que la auditoria mezclaba:

    15.562 sesiones evaluadas
      83,2%  (12.948)  una sola interaccion          -> atribucion honesta
      16,8%  ( 2.614)  multi-interaccion
               2.110   ...pero con UN SOLO operador  -> atribucion honesta igual
                 504   ...con VARIOS operadores      -> 3,2%, aca esta la mentira

Dentro de esas 504 hay **2.734 interacciones, y 1.824 (66,7%) son de un operador que NO
recibio la nota**. Llegan a 10 operadores en una sola fila.

POR QUE NO SE MUEVE LA VENTANA. Cualquier ventana que se elija deja el 66,7% de las
interacciones afuera del que cobra: el problema no es CUAL ventana sino que la fila es una
sola y los operadores son varios. Partir la sesion es la solucion de raiz y el negocio la
rechazo con numeros (18% de fragmentos sin evaluar, 25 casos poniendole 1-2 estrellas a un
operador que SI acredito; ver docs/handoff.md §10).

QUE SE HACE ENTONCES: se MARCA. La fila deja de mentir en silencio y declara su propio
limite, que es el mismo patron que el repo ya usa con `interaccion_juzgada_desde` ("se
guarda porque desde la fila NO se puede deducir"). El promedio no se toca -- sacarlas
moveria como maximo +0,045 estrellas--, pero el supervisor que abre la sesion ve que la
nota es de un tramo y no de las seis visitas.
"""
from datetime import datetime, timedelta, timezone

from src.metrics import reparto_por_interaccion

BASE = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)
OP_A = "11111111-1111-1111-1111-111111111111"
OP_B = "22222222-2222-2222-2222-222222222222"


def _cli(mins, body="me ayudas con una recarga?"):
    return {"created_at": BASE + timedelta(minutes=mins), "from_me": False,
            "is_note": False, "body": body, "media_type": "chat"}


def _op(mins, quien, body="te ayudo"):
    return {"created_at": BASE + timedelta(minutes=mins), "from_me": True,
            "is_note": False, "body": body, "media_type": "chat",
            "sent_from": "OPERATOR", "user_id": quien}


def _cierre(mins):
    return {"created_at": BASE + timedelta(minutes=mins), "from_me": True,
            "is_note": True, "body": "*resuelto* la conversación", "media_type": None}


def test_una_sola_visita_es_una_interaccion_y_un_operador():
    msgs = [_cli(0), _op(2, OP_A), _cierre(3)]
    assert reparto_por_interaccion(msgs) == (1, 1)


def test_dos_visitas_del_MISMO_operador_no_son_un_problema():
    # 2.110 sesiones caen aca: la atribucion es honesta aunque haya varias visitas.
    msgs = [_cli(0), _op(2, OP_A), _cierre(3),
            _cli(7200), _op(7202, OP_A), _cierre(7203)]
    assert reparto_por_interaccion(msgs) == (2, 1)


def test_dos_visitas_de_operadores_DISTINTOS_se_marcan():
    # Las 504: la nota se la lleva uno y el trabajo del otro desaparece.
    msgs = [_cli(0), _op(2, OP_A), _cierre(3),
            _cli(7200), _op(7202, OP_B), _cierre(7203)]
    assert reparto_por_interaccion(msgs) == (2, 2)


def test_cuenta_operadores_DISTINTOS_no_interacciones():
    # A, B, A -> tres visitas pero dos personas.
    msgs = [_cli(0), _op(2, OP_A), _cierre(3),
            _cli(100), _op(102, OP_B), _cierre(103),
            _cli(200), _op(202, OP_A), _cierre(203)]
    assert reparto_por_interaccion(msgs) == (3, 2)


def test_una_interaccion_sin_operador_identificable_no_suma():
    # Dejar 'sin identificar' como si fuera una persona mas seria inventar un operador.
    msgs = [_cli(0), _op(2, OP_A), _cierre(3),
            _cli(100), _cierre(103)]          # visita sin respuesta del operador
    assert reparto_por_interaccion(msgs) == (2, 1)


def test_sin_notas_de_cierre_es_una_sola_interaccion():
    msgs = [_cli(0), _op(2, OP_A), _cli(50), _op(52, OP_B)]
    interacciones, operadores = reparto_por_interaccion(msgs)
    assert interacciones == 1
    # Sin frontera no hay como separar: es UNA interaccion, con su operador dominante.
    assert operadores == 1


def test_una_sesion_vacia_no_rompe():
    assert reparto_por_interaccion([]) == (0, 0)


# --- se persiste en la fila -------------------------------------------------------

def test_el_worker_persiste_el_reparto_en_dimensions(monkeypatch):
    import src.worker as worker
    from tests.test_worker import (_CtxConn, _fake_score, _params_of_upsert,
                                   _session_row)

    msgs = [_cli(0), _op(2, OP_A), _cierre(3),
            _cli(7200), _op(7202, OP_B), _cierre(7203)]
    monkeypatch.setattr(worker, "fetch_session_messages", lambda cur, sid: msgs)
    monkeypatch.setattr(worker, "score_by_motivo", lambda **kw: _fake_score())
    conn = _CtxConn()
    worker.score_session_and_store(conn, _session_row("sess1"), llm=None, op_map={})
    dims = _params_of_upsert(conn)["dimensions"].obj
    assert dims["interacciones_en_la_sesion"] == 2
    assert dims["operadores_en_la_sesion"] == 2


def test_una_sesion_normal_queda_marcada_como_de_un_solo_operador(monkeypatch):
    # El 83,2% del padron: el dato se persiste igual, para que el front no tenga que
    # adivinar la diferencia entre "un operador" y "no se midio".
    import src.worker as worker
    from tests.test_worker import (_CtxConn, _fake_score, _params_of_upsert,
                                   _session_row)

    msgs = [_cli(0), _op(2, OP_A), _cierre(3)]
    monkeypatch.setattr(worker, "fetch_session_messages", lambda cur, sid: msgs)
    monkeypatch.setattr(worker, "score_by_motivo", lambda **kw: _fake_score())
    conn = _CtxConn()
    worker.score_session_and_store(conn, _session_row("sess1"), llm=None, op_map={})
    dims = _params_of_upsert(conn)["dimensions"].obj
    assert dims["interacciones_en_la_sesion"] == 1
    assert dims["operadores_en_la_sesion"] == 1
