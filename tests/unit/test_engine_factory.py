import sys
import types

import pytest

from src.core.config import Settings
from src.core.engine_factory import create_engine_registry


class _FakeKokoro:
    def __init__(self, model_path, voices_path):
        self.model_path = model_path
        self.voices_path = voices_path

    def get_voices(self):
        return ["ef_dora", "em_alex"]


@pytest.fixture
def fake_kokoro_module(monkeypatch):
    fake_mod = types.ModuleType("kokoro_onnx")
    fake_mod.Kokoro = _FakeKokoro
    monkeypatch.setitem(sys.modules, "kokoro_onnx", fake_mod)


def test_mock_engine_registry_has_single_default():
    settings = Settings(engine="mock")
    registry = create_engine_registry(settings)
    model_id, engine = registry.resolve(None)
    assert model_id == "mock"
    assert list(registry.engines) == ["mock"]


def test_kokoro_discovers_all_onnx_in_models_dir(tmp_path, fake_kokoro_module):
    (tmp_path / "kokoro-v1.0.int8.onnx").write_bytes(b"")
    (tmp_path / "kokoro-v1.0.fp32.onnx").write_bytes(b"")
    (tmp_path / "voices-v1.0.bin").write_bytes(b"")
    settings = Settings(
        engine="kokoro",
        kokoro_model=str(tmp_path / "kokoro-v1.0.int8.onnx"),
        kokoro_voices=str(tmp_path / "voices-v1.0.bin"),
    )
    registry = create_engine_registry(settings)
    assert set(registry.engines) == {"kokoro-v1.0.int8", "kokoro-v1.0.fp32"}
    assert registry.default_model == "kokoro-v1.0.int8"


def test_kokoro_no_onnx_files_raises(tmp_path, fake_kokoro_module):
    settings = Settings(
        engine="kokoro",
        kokoro_model=str(tmp_path / "missing.onnx"),
        kokoro_voices=str(tmp_path / "voices.bin"),
    )
    with pytest.raises(ValueError, match="No .onnx model files found"):
        create_engine_registry(settings)


def test_kokoro_default_model_not_discovered_raises(tmp_path, fake_kokoro_module):
    (tmp_path / "other.onnx").write_bytes(b"")
    settings = Settings(
        engine="kokoro",
        kokoro_model=str(tmp_path / "missing.onnx"),
        kokoro_voices=str(tmp_path / "voices.bin"),
    )
    with pytest.raises(ValueError, match="not found among discovered models"):
        create_engine_registry(settings)
