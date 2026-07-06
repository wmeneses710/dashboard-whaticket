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
