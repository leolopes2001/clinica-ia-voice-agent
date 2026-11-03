import pytest

from clinica_core.telefone import normalizar_telefone


@pytest.mark.parametrize(
    ("bruto", "esperado"),
    [
        ("+5511988887777", "+5511988887777"),
        ("+55 (11) 98888-7777", "+5511988887777"),
        ("5511988887777", "+5511988887777"),
        ("11988887777", "+5511988887777"),
        ("011988887777", "+5511988887777"),
        ("1133334444", "+551133334444"),
        ("01133334444", "+551133334444"),
        ("005511988887777", "+5511988887777"),
        ("sip:011988887777@192.168.0.10;user=phone", "+5511988887777"),
        ("tel:+5511988887777", "+5511988887777"),
        ("+14155550100", "+14155550100"),
    ],
)
def test_normaliza(bruto: str, esperado: str) -> None:
    assert normalizar_telefone(bruto) == esperado


@pytest.mark.parametrize(
    "bruto",
    [
        None,
        "",
        "   ",
        "anonymous",
        "Anonymous",
        "restricted",
        "unknown",
        "sip:anonymous@anonymous.invalid",
        "0",
        "988887777",
        "33334444",
        "123",
        "+0123456789",
        "1098887777",
    ],
)
def test_sem_numero_confiavel_e_oculto(bruto: str | None) -> None:
    assert normalizar_telefone(bruto) is None
