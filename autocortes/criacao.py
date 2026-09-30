"""Vídeos criados do zero: da pauta ao arquivo pronto.

A pauta é um arquivo de texto que você escreve (ou que a IA vai escrever, mais adiante), com
um cabeçalho simples e o roteiro da narração:

    titulo: Comece pequeno
    termos: mar ao amanhecer, montanha, cidade de noite
    voz: pt-BR-AntonioNeural
    musica: calma.mp3
    ---
    Ninguém constrói nada grande em um dia.
    Você constrói em mil dias pequenos, quase iguais.
    [pausa: 1s] O segredo é não deixar de aparecer.

Cada linha do roteiro ganha um respiro na narração, e `[pausa: 2s]` cria um silêncio maior no
ponto exato. Os termos são o que buscar como imagem de fundo; sem termos, o título é usado.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import estoque, montagem, voz
from .config import Config
from .midia import ErroMidia
from .util import log, slug

__all__ = ["ErroCriacao", "Pauta", "Resultado", "criar", "ler_pauta", "pasta_pautas", "pautas"]

CAMPOS = ("titulo", "termos", "voz", "musica", "topo")
_CABECALHO = re.compile(r"^\s*([a-zA-ZçãéíóúâêôÇÃÉÍÓÚÂÊÔ_]+)\s*:\s*(.*)$")


class ErroCriacao(Exception):
    def __init__(self, mensagem: str, tipo: str = "conteudo"):
        super().__init__(mensagem)
        self.tipo = tipo


@dataclass
class Pauta:
    titulo: str
    texto: str
    termos: list[str] = field(default_factory=list)
    voz: str = ""
    musica: str = ""
    topo: str = ""          # texto queimado no alto do vídeo ("" = o título)
    arquivo: Path | None = None

    @property
    def busca(self) -> list[str]:
        return self.termos or [self.titulo]

    @property
    def nome_arquivo(self) -> str:
        return slug(self.titulo or "video", 50)


@dataclass
class Resultado:
    video: Path
    narracao: Path
    duracao: float
    clipes: list[estoque.Clipe]
    creditos: str
    palavras: int


def pasta_pautas(cfg: Config) -> Path:
    """Onde ficam as pautas escritas à mão (ao lado da pasta de filmes)."""
    return cfg.raiz / "pautas"


def pautas(cfg: Config) -> list[Path]:
    pasta = pasta_pautas(cfg)
    return sorted(p for p in pasta.glob("*.txt")) if pasta.is_dir() else []


def ler_pauta(caminho: Path) -> Pauta:
    """Lê o arquivo da pauta (cabeçalho + roteiro separados por uma linha com ---)."""
    try:
        bruto = caminho.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ErroCriacao(f"não consegui ler a pauta {caminho.name}: {e}") from e

    cabecalho: dict[str, str] = {}
    corpo = bruto
    if re.search(r"^\s*-{3,}\s*$", bruto, re.M):
        parte_cabecalho, corpo = re.split(r"^\s*-{3,}\s*$", bruto, maxsplit=1, flags=re.M)
        for linha in parte_cabecalho.splitlines():
            if not linha.strip() or linha.lstrip().startswith("#"):
                continue
            achado = _CABECALHO.match(linha)
            if achado and achado.group(1).lower() in CAMPOS:
                cabecalho[achado.group(1).lower()] = achado.group(2).strip()

    texto = corpo.strip()
    if not texto:
        raise ErroCriacao(f"a pauta {caminho.name} não tem roteiro (o texto vem depois da linha ---)")
    titulo = cabecalho.get("titulo") or caminho.stem.replace("-", " ").replace("_", " ").strip().title()
    termos = [t.strip() for t in re.split(r"[,;]", cabecalho.get("termos", "")) if t.strip()]
    return Pauta(
        titulo=titulo,
        texto=texto,
        termos=termos,
        voz=cabecalho.get("voz", ""),
        musica=cabecalho.get("musica", ""),
        topo=cabecalho.get("topo", ""),
        arquivo=caminho,
    )


def _musica(cfg: Config, pauta: Pauta) -> Path | None:
    """A música pedida na pauta, ou uma da pasta de músicas (None = sem música)."""
    pasta_bruta = str(cfg["criacao"].get("pasta_musicas") or "").strip()
    pasta = cfg.caminho_de(pasta_bruta) if pasta_bruta else None
    if pauta.musica:
        alvo = Path(pauta.musica)
        if not alvo.is_absolute():
            for base in ([pasta] if pasta else []) + ([pauta.arquivo.parent] if pauta.arquivo else []):
                if base and (base / pauta.musica).is_file():
                    return base / pauta.musica
            alvo = cfg.caminho_de(pauta.musica)
        if alvo.is_file():
            return alvo
        log.warning("Música '%s' não encontrada: o vídeo sai sem música", pauta.musica)
        return None
    if pasta and pasta.is_dir():
        import random

        achadas = [p for p in pasta.iterdir() if p.suffix.lower() in (".mp3", ".m4a", ".wav", ".ogg", ".opus")]
        if achadas:
            return random.choice(achadas)
    return None


def criar(cfg: Config, pauta: Pauta, destino: Path | None = None,
          parar: threading.Event | None = None) -> Resultado:
    """Narra a pauta, junta o material de fundo e monta o vídeo vertical."""
    pasta_trabalho = cfg.pasta_dados / "criacao" / pauta.nome_arquivo
    pasta_trabalho.mkdir(parents=True, exist_ok=True)
    destino = destino or (pasta_trabalho / f"{pauta.nome_arquivo}.mp4")

    # 1) narração: é ela que define o tempo de tudo
    narracao = pasta_trabalho / "narracao.mp3"
    try:
        fala = voz.falar(cfg, pauta.texto, narracao, voz=pauta.voz or None)
    except voz.ErroVoz as e:
        raise ErroCriacao(f"narração: {e}", e.tipo) from e
    if not fala.frases:
        raise ErroCriacao("a narração saiu sem tempos de palavra, então a legenda não casaria")

    # 2) material de fundo: um clipe por trecho, com folga
    duracao = fala.duracao + float(cfg["criacao"]["cauda_seg"])
    quantos = max(2, min(12, int(duracao // montagem.MIN_CLIPE)))
    try:
        material = estoque.material(cfg, pauta.busca, quantos,
                                    bool(cfg["estoque"]["evitar_repetidos"]))
    except estoque.ErroEstoque as e:
        raise ErroCriacao(f"material de fundo: {e}", e.tipo) from e

    # 3) montagem
    topo = pauta.topo or pauta.titulo
    try:
        video = montagem.montar(cfg, narracao, fala.frases, material.clipes, topo, destino,
                                musica=_musica(cfg, pauta), parar=parar,
                                rotulo=f"Montando '{pauta.titulo}'")
    except ErroMidia as e:
        raise ErroCriacao(f"montagem: {e}") from e

    estoque.marcar_usados(cfg, material.clipes)
    palavras = sum(len(f.palavras) for f in fala.frases)
    log.info("Vídeo criado: '%s' (%.1f s, %d palavras, %d clipes)", pauta.titulo, fala.duracao, palavras,
             len(material.clipes))
    return Resultado(
        video=video,
        narracao=narracao,
        duracao=fala.duracao,
        clipes=material.clipes,
        creditos=estoque.credito_do_video(material.clipes),
        palavras=palavras,
    )
