import re

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
# DDD de dois dígitos, sem zero, e número fixo de 8 ou celular de 9 dígitos.
_NACIONAL = re.compile(r"^[1-9][1-9]\d{8,9}$")


def normalizar_telefone(bruto: str | None) -> str | None:
    """Devolve em E.164, ou None se não der para saber o número: nunca adivinha o DDD."""
    if not bruto:
        return None
    # O gateway pode entregar uma URI: sip:011988887777@192.168.0.10;user=phone.
    texto = re.sub(r"^(sips?|tel):", "", bruto.strip(), flags=re.IGNORECASE)
    texto = re.split(r"[@;]", texto, maxsplit=1)[0]
    if re.search(r"[A-Za-z]", texto):
        # "anonymous", "restricted", "unknown": número oculto.
        return None
    internacional = texto.startswith("+")
    digitos = re.sub(r"\D", "", texto)
    if internacional:
        candidato = f"+{digitos}"
        return candidato if _E164.match(candidato) else None
    if digitos.startswith("00"):
        candidato = f"+{digitos[2:]}"
        return candidato if _E164.match(candidato) else None
    if digitos.startswith("0"):
        digitos = digitos[1:]
    if digitos.startswith("55") and _NACIONAL.match(digitos[2:]):
        return f"+{digitos}"
    if _NACIONAL.match(digitos):
        return f"+55{digitos}"
    return None
