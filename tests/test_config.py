from pathlib import Path

import pytest

from dataactivator.core.config import (
    AppConfig,
    ConfigError,
    ProviderConfig,
    StorageConfig,
    YamlConfigBackend,
)

EXAMPLE_YAML = """
storage:
  type: file
  folder: data

providers:
  - name: vw
    type: volkswagen-data-act-portal
    email: user@example.com
    password: s3cr3t!
"""


def test_load_example_schema(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(EXAMPLE_YAML)
    cfg = YamlConfigBackend(path).load()

    assert cfg.storage.type == "file"
    assert cfg.storage.folder == Path("data")

    vw = cfg.provider("vw")
    assert vw.type == "volkswagen-data-act-portal"
    assert vw.settings == {"email": "user@example.com", "password": "s3cr3t!"}


def test_repo_example_file_is_valid() -> None:
    example = Path(__file__).parent.parent / "config.example.yaml"
    cfg = YamlConfigBackend(example).load()
    assert cfg.providers, "example must define at least one provider"


def test_roundtrip(tmp_path: Path) -> None:
    backend = YamlConfigBackend(tmp_path / "config.yaml")
    original = AppConfig(
        storage=StorageConfig(type="file", folder=Path("data")),
        providers=[
            ProviderConfig(
                name="vw",
                type="volkswagen-data-act-portal",
                email="a@b.c",
                password="secret",
            )
        ],
    )
    backend.save(original)
    assert backend.load() == original


def test_save_sets_restrictive_permissions(tmp_path: Path) -> None:
    backend = YamlConfigBackend(tmp_path / "config.yaml")
    backend.save(AppConfig())
    assert backend.path.stat().st_mode & 0o077 == 0


def test_missing_file_raises(tmp_path: Path) -> None:
    backend = YamlConfigBackend(tmp_path / "nope.yaml")
    with pytest.raises(ConfigError, match="not found"):
        backend.load()


def test_unknown_toplevel_key_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("providers: []\ntypo_key: 1\n")
    with pytest.raises(ConfigError, match="typo_key"):
        YamlConfigBackend(path).load()


def test_provider_lookup_missing() -> None:
    with pytest.raises(ConfigError, match="no provider"):
        AppConfig().provider("missing")
