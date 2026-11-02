import importlib
import sys

import pytest

PACOTES = [
    "clinica_core",
    "clinica_api",
    "clinica_painel",
    "clinica_mcp_clinica",
    "clinica_mcp_conhecimento",
    "clinica_agent",
    "clinica_worker",
]


def test_python_312() -> None:
    assert sys.version_info[:2] == (3, 12)


@pytest.mark.parametrize("pacote", PACOTES)
def test_pacote_importa(pacote: str) -> None:
    importlib.import_module(pacote)
