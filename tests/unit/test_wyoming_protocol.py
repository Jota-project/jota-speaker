from src.core.config import Settings


def test_wyoming_defaults():
    s = Settings()
    assert s.wyoming_enabled is True
    assert s.wyoming_port == 20424
