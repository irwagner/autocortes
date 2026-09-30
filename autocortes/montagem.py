"""Monta um vídeo vertical a partir de narração e imagens, sem partir de um filme.

É o render dos vídeos gerados do zero (motivacional, curiosidades, frases). A narração manda
no tempo: cada trecho falado ganha um clipe de fundo, e a legenda palavra por palavra usa os
tempos que a própria voz devolveu, então casa exatamente com o áudio.

O visual é o mesmo dos cortes de filme: a geometria, a moldura, a barra de progresso, o estilo
da legenda e o codificador vêm todos do `edicao.py`. A diferença está só na origem da imagem:
em vez de um trecho de filme, uma sequência de clipes emendados. Imagem parada ganha um
movimento lento de zoom (Ken Burns), senão parece apresentação de slides.
"""

from __future__ import annotations

import math
import random
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from . import edicao
from .config import Config
from .estoque import Clipe
from .legendas import Frase, palavras_no_intervalo
from .midia import ErroMidia, Progresso, base_ffmpeg, caminho_filtro, duracao_arquivo, executar
from .util import log

__all__ = ["Trecho", "montar", "planejar"]

# uma montagem por vez: o FFmpeg com vários clipes já usa a CPU toda
TRAVA = threading.Lock()
# menor e maior tempo que um clipe fica na tela
MIN_CLIPE = 1.8
MAX_CLIPE = 6.0


@dataclass
class Trecho:
    """Um pedaço do vídeo: um clipe de fundo cobrindo um intervalo da narração."""
    clipe: Clipe
    inicio: float          # no vídeo final
    duracao: float
    corte_em: float = 0.0  # de onde começar dentro do clipe


@dataclass
class Plano:
    trechos: list[Trecho] = field(default_factory=list)
    duracao: float = 0.0


def planejar(clipes: list[Clipe], frases: list[Frase], duracao: float, embaralhar: bool = True) -> Plano:
    """Divide a duração entre os clipes, trocando de imagem onde a fala troca de frase.

    Cortar no meio de uma frase corta o raciocínio; por isso o ponto de troca é a fronteira
    das frases, e não um tempo fixo.
    """
    if not clipes:
        raise ErroMidia("não há clipe de fundo para montar o vídeo")
    duracao = max(1.0, float(duracao))

    # pontos onde dá para trocar de imagem: começo de cada frase
    pontos = [0.0]
    for f in frases:
        if f.ini > pontos[-1] + MIN_CLIPE:
            pontos.append(round(f.ini, 3))
    pontos.append(duracao)

    # junta pontos até cada pedaço ficar no tamanho bom, e divide o que ficou longo demais
    limites: list[float] = [0.0]
    for ponto in pontos[1:]:
        espaco = ponto - limites[-1]
        if espaco < MIN_CLIPE:
            continue
        if espaco > MAX_CLIPE:
            # em partes iguais: cortar de MAX_CLIPE em MAX_CLIPE deixaria um resto curtinho no fim
            quantas = math.ceil(espaco / MAX_CLIPE)
            passo = espaco / quantas
            for _ in range(quantas - 1):
                limites.append(round(limites[-1] + passo, 3))
        limites.append(round(ponto, 3))
    if limites[-1] < duracao:
        if duracao - limites[-1] < MIN_CLIPE and len(limites) > 1:
            limites[-1] = duracao
        else:
            limites.append(duracao)

    ordem = list(clipes)
    if embaralhar:
        random.shuffle(ordem)
    trechos: list[Trecho] = []
    for i in range(len(limites) - 1):
        inicio, fim = limites[i], limites[i + 1]
        clipe = ordem[i % len(ordem)]
        precisa = fim - inicio
        sobra = max(0.0, (clipe.duracao or 0.0) - precisa)
        # começa num ponto aleatório do clipe, mas sempre o mesmo para o mesmo clipe e posição
        semente = random.Random(f"{clipe.chave}:{i}")
        corte = round(semente.uniform(0, min(sobra, 5.0)), 2) if sobra > 0.2 else 0.0
        trechos.append(Trecho(clipe=clipe, inicio=round(inicio, 3), duracao=round(precisa, 3), corte_em=corte))
    return Plano(trechos=trechos, duracao=duracao)


def _entrada_clipe(trecho: Trecho, fps_txt: str, largura: int, altura: int, indice: int,
                   ken_burns: bool) -> tuple[list[str], str]:
    """Argumentos de entrada e o filtro que leva o clipe ao tamanho da tela."""
    clipe = trecho.clipe
    arquivo = clipe.caminho
    if arquivo is None:
        raise ErroMidia(f"clipe sem arquivo no disco: {clipe.chave}")
    dur = f"{trecho.duracao:.3f}"
    if clipe.imagem:
        entrada = ["-loop", "1", "-t", dur, "-i", str(arquivo)]
        if ken_burns:
            # zoom lento de 1,0 a 1,12: imagem parada sem movimento vira slideshow
            filtro = (
                f"[{indice}:v]scale={largura * 2}:{altura * 2}:force_original_aspect_ratio=increase,"
                f"crop={largura * 2}:{altura * 2},"
                f"zoompan=z='min(1+0.12*on/{max(1, int(trecho.duracao * 30))},1.12)':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={largura}x{altura}:fps={fps_txt},"
                f"setsar=1,fps={fps_txt}[v{indice}]"
            )
        else:
            filtro = (f"[{indice}:v]scale={largura}:{altura}:force_original_aspect_ratio=increase,"
                      f"crop={largura}:{altura},setsar=1,fps={fps_txt}[v{indice}]")
    else:
        entrada = []
        if trecho.corte_em > 0.05:
            entrada += ["-ss", f"{trecho.corte_em:.3f}"]
        entrada += ["-t", dur, "-i", str(arquivo)]
        # clipe curto demais: repete até cobrir o trecho
        laco = "loop=loop=-1:size=1:start=0," if (clipe.duracao or 0) < trecho.duracao else ""
        filtro = (f"[{indice}:v]{laco}scale={largura}:{altura}:force_original_aspect_ratio=increase,"
                  f"crop={largura}:{altura},setsar=1,fps={fps_txt},trim=duration={dur},"
                  f"setpts=PTS-STARTPTS[v{indice}]")
    return entrada, filtro


def _filtro_audio(cfg: Config, indice_narracao: int, indice_musica: int | None, dur: float) -> list[str]:
    """Narração, e música de fundo abaixando sob a voz (ducking)."""
    e = cfg["edicao"]
    c = cfg["criacao"]
    narr = f"[{indice_narracao}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    if e["normalizar_audio"]:
        narr += ",loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000"
    if indice_musica is None:
        partes = [narr + "[aout_pre]"]
    else:
        volume = float(c["volume_musica"])
        partes = [
            narr + "[narr]",
            f"[{indice_musica}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={volume:.3f}[mus]",
            "[narr]asplit=2[narr1][narr2]",
            # a voz comprime a música: sem isso a música briga com a narração
            "[mus][narr2]sidechaincompress=threshold=0.03:ratio=12:attack=15:release=350:makeup=1[musduck]",
            "[narr1][musduck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout_pre]",
        ]
    fade = float(c["fade_audio_seg"])
    if fade > 0:
        partes.append(f"[aout_pre]afade=t=in:st=0:d={min(fade, 1.0):.2f},"
                      f"afade=t=out:st={max(0.0, dur - fade):.3f}:d={fade:.2f}[aout]")
    else:
        partes.append("[aout_pre]anull[aout]")
    return partes


def montar(cfg: Config, narracao: Path, frases: list[Frase], clipes: list[Clipe], topo: str,
           destino: Path, musica: Path | None = None, parar: threading.Event | None = None,
           rotulo: str | None = None) -> Path:
    """Gera o vídeo vertical. Devolve o caminho do arquivo pronto."""
    e = cfg["edicao"]
    c = cfg["criacao"]
    largura, altura = int(e["largura"]), int(e["altura"])
    fps = Fraction(int(c["fps"]), 1)
    fps_txt = f"{fps.numerator}/{fps.denominator}"

    dur_narracao = duracao_arquivo(cfg["ferramentas"]["ffprobe"], narracao)
    if dur_narracao <= 0:
        raise ErroMidia(f"não consegui ler a duração da narração: {narracao}")
    dur = round(dur_narracao + float(c["cauda_seg"]), 3)

    plano = planejar(clipes, frases, dur, bool(c["embaralhar_clipes"]))
    if not plano.trechos:
        raise ErroMidia("não consegui planejar os trechos do vídeo")

    # a origem já vem no formato da tela, então a geometria do modelo visual dá tela cheia
    info = {"crop": [largura, altura, 0, 0], "video": {"sar": [1, 1], "indice": 0, "fps": fps_txt}}
    g, arquivo_moldura, moldura = edicao.geometria_do_corte(cfg, info, topo)

    # --- legenda e título (ASS), com os tempos que a voz devolveu
    destino.parent.mkdir(parents=True, exist_ok=True)
    ass = destino.with_suffix(".ass")
    palavras = palavras_no_intervalo(frases, 0.0, dur) if e["legendas"] else []
    estilo = edicao.estilo_legenda(cfg, g)
    ass.write_text(edicao.gerar_ass(estilo, palavras, dur, g.legenda_y, topo, g.topo_y), encoding="utf-8")
    pasta_fontes = edicao.preparar_fonte(cfg)

    # --- entradas: narração, clipes, moldura e música
    entradas: list[str] = ["-i", str(narracao)]
    indice_narracao = 0
    filtros: list[str] = []
    rotulos: list[str] = []
    proximo = 1
    for trecho in plano.trechos:
        args, filtro = _entrada_clipe(trecho, fps_txt, largura, altura, proximo, bool(c["ken_burns"]))
        entradas += args
        filtros.append(filtro)
        rotulos.append(f"[v{proximo}]")
        proximo += 1
    if len(rotulos) == 1:
        filtros.append(f"{rotulos[0]}null[vbase]")
    else:
        filtros.append("".join(rotulos) + f"concat=n={len(rotulos)}:v=1:a=0[vbase]")

    indice_moldura = 0
    if moldura is not None and arquivo_moldura is not None:
        entradas += ["-i", str(arquivo_moldura)]
        indice_moldura = proximo
        proximo += 1
    indice_musica = None
    if musica is not None and musica.is_file():
        entradas += ["-stream_loop", "-1", "-t", f"{dur:.3f}", "-i", str(musica)]
        indice_musica = proximo
        proximo += 1

    # --- o resto do visual é o mesmo dos cortes de filme
    partes, atual = edicao._filtro_video(cfg, g, "[vbase]null", f":r={fps_txt}:d={dur:.3f}", dur,
                                         moldura, indice_moldura)
    filtros += partes
    fade = (f",fade=t=in:st=0:d=0.25,fade=t=out:st={max(0.0, dur - 0.45):.3f}:d=0.45" if e["fade"] else "")
    filtros.append(f"[{atual}]{edicao._filtro_ass(ass, pasta_fontes)}{fade},format=yuv420p[vout]")
    filtros += _filtro_audio(cfg, indice_narracao, indice_musica, dur)

    parcial = destino.with_name(destino.stem + ".parcial.mp4")
    rotulo = rotulo or f"Montando {destino.name}"

    def comando(args_video: list[str]) -> list[str]:
        return base_ffmpeg(cfg["ferramentas"]["ffmpeg"]) + [
            "-loglevel", "error", "-progress", "pipe:1", "-nostats",
            *entradas,
            "-filter_complex", ";".join(filtros),
            "-map", "[vout]", "-map", "[aout]",
            *args_video,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-t", f"{dur:.3f}", "-movflags", "+faststart",
            str(parcial),
        ]

    codec = str(e["codec"])
    usar_gpu = codec != "libx264" and codec not in edicao._CODECS_FALHOS
    with TRAVA:
        prog = Progresso(rotulo, dur, intervalo_seg=15)
        r = executar(comando(edicao._args_video(cfg, fps, None if usar_gpu else "libx264")), parar=parar,
                     ao_ler_saida=prog.linha_ffmpeg, baixa_prioridade=cfg["geral"]["prioridade_baixa"])
        if r.codigo != 0 and usar_gpu:
            log.warning("O codificador %s falhou (%s). Tentando com o libx264.", codec, r.resumo_erro(2))
            prog = Progresso(rotulo, dur, intervalo_seg=15)
            r = executar(comando(edicao._args_video(cfg, fps, "libx264")), parar=parar,
                         ao_ler_saida=prog.linha_ffmpeg, baixa_prioridade=cfg["geral"]["prioridade_baixa"])
            if r.codigo == 0:
                edicao._CODECS_FALHOS.add(codec)
    if r.codigo != 0:
        parcial.unlink(missing_ok=True)
        raise ErroMidia(f"falha na montagem: {r.resumo_erro(6)}")

    obtida = duracao_arquivo(cfg["ferramentas"]["ffprobe"], parcial)
    if obtida < dur * 0.9:
        parcial.unlink(missing_ok=True)
        raise ErroMidia(f"vídeo montado ficou curto ({obtida:.1f}s de {dur:.1f}s)")
    parcial.replace(destino)
    edicao.gerar_miniatura(cfg, destino, min(dur / 3, 3.0))
    log.info("Vídeo montado: %s (%.1f s, %d clipes)", destino.name, obtida, len(plano.trechos))
    return destino
