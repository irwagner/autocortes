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
# SHA-256 dos pacotes oficiais já conferidos. Versão fora desta lista não é baixada: seriam
# programas executados sem conferência nenhuma (instale à mão e use [ferramentas].whisper_cli).
SHA256_WHISPER = {
    "b5130": "f9ec6c52a2e949b62ab51fa21d0d497958f9e41c3010c157c4e42932d5316f3c",
}
URL_MODELO = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{nome}.bin"
NOME_VAD = "ggml-silero-v6.2.0.bin"
URL_VAD = f"https://huggingface.co/ggml-org/whisper-vad/resolve/main/{NOME_VAD}"
# (SHA-256, tamanho) dos modelos, tirados da API do Hugging Face em 30/09/2026 (o "lfs.oid" de
# cada arquivo) e conferidos com os modelos já baixados nesta instalação
ARQUIVOS_MODELO = {
    "tiny": ("be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21", 77691713),
    "base": ("60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe", 147951465),
    "small": ("1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b", 487601967),
    "medium": ("6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208", 1533763059),
    "large-v3-turbo-q5_0": ("394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2", 574041195),
    "large-v3-turbo": ("1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69", 1624555275),
}
ARQUIVO_VAD = ("2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987", 885098)
# todo modelo do whisper.cpp (e o do VAD) começa com a marca do formato GGML
MARCA_GGML = b"lmgg"


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
    esperado = SHA256_WHISPER.get(versao)
    if not esperado:
        raise ErroMidia(
            f"Não baixo o whisper.cpp {versao}: só a versão {', '.join(SHA256_WHISPER)} tem o SHA-256 conferido. "
            "Volte [ferramentas].whisper_versao para ela, ou instale o whisper.cpp à mão e informe "
            "[ferramentas].whisper_cli e whisper_vad."
        )
    pasta = cli.parent
    pacote = pasta / "whisper-bin-x64.zip"
    baixar(URL_WHISPER.format(versao=versao), pacote, f"whisper.cpp {versao}")
    if _sha256(pacote) != esperado:
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


def _modelo_inteiro(caminho: Path, tamanho: int | None) -> bool:
    """Conferência rápida (a cada uso): o tamanho certo e a marca do formato no começo."""
    try:
        if tamanho is not None and caminho.stat().st_size != tamanho:
            return False
        with open(caminho, "rb") as f:
            return f.read(4) == MARCA_GGML
    except OSError:
        return False


def _garantir_arquivo_modelo(url: str, destino: Path, rotulo: str, conferencia: tuple[str, int] | None) -> Path:
    """O modelo no disco, conferido. Arquivo pela metade ou corrompido é apagado e baixado de novo.

    Depois do download, o SHA-256 inteiro é comparado com o do Hugging Face; um download que não
    confere é descartado (e conta como falha de download: o filme tenta de novo mais tarde).
    """
    sha, tamanho = conferencia if conferencia else (None, None)
    if destino.exists():
        if _modelo_inteiro(destino, tamanho):
            return destino
        log.warning("O %s em %s está incompleto ou corrompido; baixando de novo", rotulo, destino.name)
        destino.unlink(missing_ok=True)
    baixar(url, destino, rotulo)
    if not _modelo_inteiro(destino, tamanho) or (sha and _sha256(destino) != sha):
        destino.unlink(missing_ok=True)
        raise ErroDownload(f"O {rotulo} baixado não confere com o arquivo oficial; descartei o download")
    return destino


def garantir_modelo(cfg: Config, nome: str | None = None) -> Path:
    nome = nome or str(cfg["transcricao"]["modelo"])
    return _garantir_arquivo_modelo(URL_MODELO.format(nome=nome), caminho_modelo(cfg, nome),
                                    f"modelo Whisper '{nome}'", ARQUIVOS_MODELO.get(nome))


def garantir_vad(cfg: Config) -> Path:
    return _garantir_arquivo_modelo(URL_VAD, cfg.pasta_modelos / NOME_VAD,
                                    "modelo de detecção de voz (Silero VAD)", ARQUIVO_VAD)


def threads(cfg: Config) -> int:
    n = int(cfg["ferramentas"]["threads"] or 0)
    if n > 0:
        return n
    return max(1, min(8, (os.cpu_count() or 4) // 2))
