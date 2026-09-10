"""Tests de la config por entorno (lo relevante: parseo del worker y cuentas)."""
import src.config as config


def test_scoring_accounts_se_parsea_de_csv(monkeypatch):
    monkeypatch.setenv("SCORING_ACCOUNTS", "sistemas, datos ,")
    cfg = config.load_config()
    assert cfg.scoring_accounts == ("sistemas", "datos")


def test_scoring_enabled_es_booleano(monkeypatch):
    monkeypatch.setenv("SCORING_ENABLED", "true")
    assert config.load_config().scoring_enabled is True
    monkeypatch.setenv("SCORING_ENABLED", "0")
    assert config.load_config().scoring_enabled is False


def test_defaults_razonables(monkeypatch):
    for k in ("SCORING_ENABLED", "SCORING_ACCOUNTS", "SCORING_BATCH_SIZE", "SCORING_POLL_SECONDS"):
        monkeypatch.delenv(k, raising=False)
    cfg = config.load_config()
    assert cfg.scoring_enabled is False          # no scorea salvo que se active
    assert cfg.scoring_accounts == ("sistemas", "datos")
    assert cfg.scoring_batch_size > 0
    assert cfg.scoring_poll_seconds > 0


def test_tuning_inferencia_defaults_y_override(monkeypatch):
    for k in ("OLLAMA_NUM_CTX", "OLLAMA_NUM_PREDICT", "LLM_FAST_ATTEMPTS"):
        monkeypatch.delenv(k, raising=False)
    cfg = config.load_config()
    assert cfg.ollama_num_predict == 768   # default bajo para caber bajo el timeout del proxy
    assert cfg.llm_fast_attempts == 2
    assert cfg.ollama_num_ctx == 16384
    monkeypatch.setenv("OLLAMA_NUM_PREDICT", "512")
    monkeypatch.setenv("LLM_FAST_ATTEMPTS", "1")
    monkeypatch.setenv("OLLAMA_NUM_CTX", "8192")
    cfg = config.load_config()
    assert cfg.ollama_num_predict == 512
    assert cfg.llm_fast_attempts == 1
    assert cfg.ollama_num_ctx == 8192


def test_ui_enabled_arranca_prendida_y_se_apaga_explicitamente(monkeypatch):
    """La perilla que deja el tablero apagado con el worker vivo (ver src/app.py).

    DEFAULT PRENDIDO, al revés que `api_docs`/`scoring_enabled`: es una perilla operativa,
    no una credencial, y un despliegue al que se le olvide la variable no puede quedarse
    sin tablero. Una variable VACÍA es lo mismo que ausente: apagar es explícito.
    """
    monkeypatch.delenv("UI_ENABLED", raising=False)
    assert config.load_config().ui_enabled is True
    monkeypatch.setenv("UI_ENABLED", "")
    assert config.load_config().ui_enabled is True
    monkeypatch.setenv("UI_ENABLED", "false")
    assert config.load_config().ui_enabled is False


def test_ui_enabled_con_un_valor_que_no_se_entiende_queda_apagada(monkeypatch):
    """Un valor no reconocido (un typo tipo "ture") APAGA el tablero, no lo deja abierto.

    Es la misma semántica que `_bool` ya le daba a todos los flags, y para este en
    particular es la dirección segura: fallar hacia "no expuesto". Queda fijado acá para
    que sea una decisión y no una casualidad del parseo.
    """
    monkeypatch.setenv("UI_ENABLED", "ture")
    assert config.load_config().ui_enabled is False


def test_el_default_de__bool_no_cambio_para_los_demas_flags(monkeypatch):
    """`_bool` ahora acepta `default`, y el resto de los flags dependen de que siga siendo
    False: un default distinto prendería API_DOCS y el worker sin que nadie lo pida."""
    for k in ("API_DOCS", "SCORING_ENABLED", "SCORING_RECOM_SUBAGENT"):
        monkeypatch.delenv(k, raising=False)
    cfg = config.load_config()
    assert cfg.api_docs is False
    assert cfg.scoring_enabled is False
    assert cfg.recom_subagent_enabled is False
