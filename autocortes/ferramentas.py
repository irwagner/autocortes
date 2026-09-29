"""Download e localização do whisper.cpp e dos modelos de transcrição."""

from __future__ import annotations

import hashlib
import os
import platform
import time
import zipfile
from pathlib import Path

from .config import Config
from .midia import ErroMidia
from .util import ATIVIDADES, log

# modelos oferecidos no painel (tamanho aproximado do download)
MODELOS = {
    "tiny": "75 MB, muito rápido, erra bastante",
    "base": "142 MB, rápido",
    "small": "466 MB, bom equilíbrio para português",
    "medium": "1,5 GB, preciso e lento",
    "large-v3-turbo-q5_0": "547 MB, preciso e rápido",
    "large-v3-turbo": "1,6 GB, o mais preciso",
}

URL_WHISPER = "https://github.com/ggml-org/whisper.cpp/releases/download/{versao}/whisper-bin-x64.zip"
# SHA-256 dos pacotes oficiais já conferidos. Outras versões são baixadas sem essa checagem.
SHA256_WHISPER = {
    "b5130": "f9ec6c52a2e949b62ab51fa21d0d497958f9e41c3010c157c4e42932d5316f3c",
}
URL_MODELO = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{nome}.bin"
NOME_VAD = "ggml-silero-v6.2.0.bin"
URL_VAD = f"https://huggingface.co/ggml-org/whisper-vad/resolve/main/{NOME_VAD}"


class ErroDownload(ErroMidia):
    """Falha de rede ao baixar ferramenta/modelo: vale tentar de novo mais tarde."""


def baixar(url: str, destino: Path, rotulo: str) -> None:
    import requests

    destino.parent.mkdir(parents=True, exist_ok=True)
    parcial = destino.with_name(destino.name + ".part")
    log.info("Baixando %s ...", rotulo)
    ATIVIDADES.definir(f"Baixando {rotulo}", 0)
    for tentativa in range(1, 4):
        try:
            with requests.get(url, stream=True, timeout=(20, 120), headers={"User-Agent": "autocortes"}) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0)
                feito, ultimo, ultimo_painel = 0, time.monotonic(), 0.0
                with open(parcial, "wb") as f:
                    for bloco in r.iter_content(chunk_size=1024 * 1024):
                        f.write(bloco)
                        feito += len(bloco)
                        agora = time.monotonic()
                        if total and agora - ultimo_painel > 0.5:
                            ultimo_painel = agora
                            ATIVIDADES.progresso(100 * feito / total)
                        if agora - ultimo > 10:
                            ultimo = agora
                            if total:
                                log.info("  %s: %d%% de %d MB", rotulo, 100 * feito // total, total // 2**20)
                            else:
                                log.info("  %s: %d MB", rotulo, feito // 2**20)
            if total and feito != total:
                raise OSError(f"download incompleto ({feito} de {total} bytes)")
            os.replace(parcial, destino)
            log.info("%s pronto (%d MB)", rotulo, destino.stat().st_size // 2**20)
            return
        except (OSError, requests.RequestException) as e:
            log.warning("Falha ao baixar %s (tentativa %d/3): %s", rotulo, tentativa, e)
            time.sleep(5 * tentativa)
    parcial.unlink(missing_ok=True)
    raise ErroDownload(f"Não foi possível baixar {rotulo} de {url}")


def _sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


def caminhos_whisper(cfg: Config) -> tuple[Path, Path]:
    f = cfg["ferramentas"]
    pasta = cfg.pasta_ferramentas / "whisper"
    exe = ".exe" if os.name == "nt" else ""
    cli = cfg.caminho_de(f["whisper_cli"]) if f["whisper_cli"] else pasta / f"whisper-cli{exe}"
    vad = cfg.caminho_de(f["whisper_vad"]) if f["whisper_vad"] else pasta / f"whisper-vad-speech-segments{exe}"
    return cli, vad


def garantir_whisper(cfg: Config) -> tuple[Path, Path]:
    """Retorna (whisper-cli, whisper-vad-speech-segments), baixando se necessário."""
    cli, vad = caminhos_whisper(cfg)
    if cli.exists() and vad.exists():
        return cli, vad
    if cfg["ferramentas"]["whisper_cli"] or cfg["ferramentas"]["whisper_vad"]:
        raise ErroMidia(f"whisper.cpp não encontrado em {cli} / {vad} (confira [ferramentas])")
    if os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise ErroMidia(
            "Download automático do whisper.cpp só existe para Windows x64. Compile o whisper.cpp "
            "e configure [ferramentas].whisper_cli e whisper_vad."
        )

    versao = str(cfg["ferramentas"]["whisper_versao"])
    pasta = cli.parent
    pacote = pasta / "whisper-bin-x64.zip"
    baixar(URL_WHISPER.format(versao=versao), pacote, f"whisper.cpp {versao}")
    esperado = SHA256_WHISPER.get(versao)
    if esperado and _sha256(pacote) != esperado:
        pacote.unlink(missing_ok=True)
        raise ErroMidia("O pacote do whisper.cpp baixado não confere com o SHA-256 esperado. Download descartado.")

    with zipfile.ZipFile(pacote) as z:
        for item in z.infolist():
            nome = Path(item.filename).name  # achata as pastas: sem risco de sair do diretório
            if item.is_dir() or not nome:
                continue
            util = nome.lower().endswith(".dll") or nome in (cli.name, vad.name)
            if util:
                (pasta / nome).write_bytes(z.read(item))
    pacote.unlink(missing_ok=True)
    if not (cli.exists() and vad.exists()):
        raise ErroMidia("O pacote do whisper.cpp não contém whisper-cli/whisper-vad-speech-segments")
    return cli, vad


def caminho_modelo(cfg: Config, nome: str | None = None) -> Path:
    nome = nome or str(cfg["transcricao"]["modelo"])
    return cfg.pasta_modelos / f"ggml-{nome}.bin"


def garantir_modelo(cfg: Config, nome: str | None = None) -> Path:
    nome = nome or str(cfg["transcricao"]["modelo"])
    destino = caminho_modelo(cfg, nome)
    if not destino.exists():
        baixar(URL_MODELO.format(nome=nome), destino, f"modelo Whisper '{nome}'")
    return destino


def garantir_vad(cfg: Config) -> Path:
    destino = cfg.pasta_modelos / NOME_VAD
    if not destino.exists():
        baixar(URL_VAD, destino, "modelo de detecção de voz (Silero VAD)")
    return destino


def threads(cfg: Config) -> int:
    n = int(cfg["ferramentas"]["threads"] or 0)
    if n > 0:
        return n
    return max(1, min(8, (os.cpu_count() or 4) // 2))
