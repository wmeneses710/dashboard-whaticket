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
