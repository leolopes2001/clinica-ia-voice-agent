# Áudio da frase de espera

O agente toca `frase_espera.wav` (mono, 16 bits) e atende sem a frase se o arquivo não existir. Gere com `scripts/gerar_frase_espera.py`. O WAV não fica no repositório, e o agente lê o arquivo a cada ligação.
