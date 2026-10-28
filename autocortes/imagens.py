"""Posts de imagem tirados dos cortes: geometria, escolha dos quadros e render com o FFmpeg.

Três tipos: momento (um quadro com o nome do filme e a parte), frase (a fala real do trecho sobre o
quadro) e carrossel (3 a 5 quadros de cenas diferentes, em ordem). Dois formatos: 4x5 (1080x1350, o
Instagram) e 9x16 (1080x1920, o TikTok). A geometria é calculada aqui e serve à geração, à prévia e
ao teste: o painel nunca refaz a conta.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from . import analise, selecao
from .config import FORMATOS_IMAGEM, Config
from .edicao import (EXEMPLO_LEGENDA, LARGURA_TITULO, SEGURO_BASE, SEGURO_TOPO, estilo_legenda, filtro_ass,
                     fonte_do_topo, info_do_quadro, preparar_fonte, quadro_exemplo)
from .legendas import Frase, gerar_ass_cartaz, largura_texto
from .midia import ErroMidia, base_ffmpeg, caminho_filtro, executar
from .textos import _variaveis_e_tags, aplicar_modelo_sem_vazias, limitar, normalizar
from .util import ler_json, log, salvar_json, trocar_arquivo

# chaves do metadata=mode=print medidas no FFmpeg 9.0.2 (out/2026) com signalstats e blurdetect num
# testsrc2: cada quadro começa com "frame:N pts:N pts_time:T", e as linhas seguintes são "chave=valor"
CHAVES_METADADO = {
    "tempo": "pts_time",
    "brilho": "lavfi.signalstats.YAVG",
    "nitidez": "lavfi.blur",
}

# tamanho de cada formato, pelo nome que vai na tabela imagens
TAMANHOS = {f["nome"]: (int(f["largura"]), int(f["altura"])) for f in FORMATOS_IMAGEM.values()}
# no 9x16, abaixo do meio da tela, a coluna de botões e a legenda do TikTok ficam sobre a foto o tempo
# todo: o texto que passa de y = 960 fica entre x 192 e 780
MEIO_9X16 = 960
DIREITA_BAIXO_9X16 = 780
# espaço entre o topo, o quadro e o texto de baixo
FOLGA = 24
# no 9x16 o quadro pode passar da zona de texto (a interface cobre a foto, não o texto), até 62% da tela
ALTURA_MAX_9X16 = 1190
# altura de uma linha, em vezes o tamanho da fonte. O libass ajusta a fonte para a subida mais a descida
# darem o Fontsize, então a linha mede o próprio tamanho (medido em out/2026: 43 px com 43,2 px de fonte),
# com 5% de folga. Pelo mesmo motivo, o legendas.largura_texto (em = tamanho) sai folgado para qualquer
# fonte com subida + descida maior que 1 em: na Arial Black, o texto desenhado tem ~71% da largura contada
ALTURA_LINHA = 1.05
# frase sobre o quadro só se o quadro tiver a altura do texto mais esta folga
FOLGA_FRASE = 80
# o pé da frase sobre o quadro fica este tanto acima da base dele
PE_FRASE = 32
# emoji não sai no desenho (o libass não desenha emoji colorido)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")


@dataclass(frozen=True)
class Zona:
    """Retângulo em que o texto fica inteiro (x1 e y1 são o limite, fora)."""
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def h(self) -> int:
        return self.y1 - self.y0


def _zona_9x16() -> Zona:
    largura, altura = TAMANHOS["9x16"]
    margem = int(largura * (1 - LARGURA_TITULO) / 2)
    return Zona(margem, int(altura * SEGURO_TOPO), largura - margem, int(altura * SEGURO_BASE))


# 4x5: o quadrado central (y 135..1215) com 30 px de folga, longe dos 34 px que a grade 3:4 corta de cada lado
ZONAS = {"4x5": Zona(90, 165, 990, 1185), "9x16": _zona_9x16()}


class ErroImagem(Exception):
    """Falha ao gerar as imagens de um post.

    do_corte: o problema é do trecho (sem fala, sem quadros bons) e o corte pode ser pulado neste tipo;
    senão é do PC (FFmpeg, prazo, disco) e não gasta o corte. sem_linha: nem a postagem é gravada
    (o arquivo do filme saiu da pasta entre a fila e a geração).
    """

    def __init__(self, mensagem: str, do_corte: bool, sem_linha: bool = False):
        super().__init__(mensagem)
        self.do_corte = do_corte
        self.sem_linha = sem_linha


# uma geração por vez: o motor espera; a gravação e a prévia desistem em 5 s
TRAVA = threading.Lock()


def _sem_emoji(texto: str) -> str:
    """O texto sem emoji, com os espaços que sobraram arrumados."""
    limpo = _EMOJI.sub("", str(texto or ""))
    return "\n".join(re.sub(r"[ \t]{2,}", " ", linha).strip() for linha in limpo.split("\n")).strip()


def _par(valor: float) -> int:
    v = int(round(valor))
    return max(2, v - (v % 2))


def altura_fundo(altura: int) -> int:
    """Altura do fundo reduzido antes do desfoque: um quarto da tela, sempre par (338 no 4x5, 480 no 9x16)."""
    return int(round(altura / 4 / 2)) * 2


# ---------------------------------------------------------------- texto

def _estilos(cfg: Config) -> dict[str, dict]:
    """Fonte, tamanho e contorno de cada bloco, do modelo visual em uso."""
    e = cfg["edicao"]
    fonte_topo = fonte_do_topo(cfg)[1]
    tamanho_topo = int(e["tamanho_topo"])
    return {
        "Topo": {"fonte": fonte_topo, "tamanho": tamanho_topo, "contorno": 5},
        "Chamada": {"fonte": fonte_topo, "tamanho": round(tamanho_topo * 0.75), "contorno": 5},
        "Frase": {"fonte": str(e["fonte_nome"]), "tamanho": int(e["tamanho_legenda"]),
                  "contorno": int(e["contorno_legenda"])},
    }


def _largura(linha: str, s: dict, escala: int) -> float:
    return largura_texto(linha, s["tamanho"] * escala / 100, s["fonte"]) + 2 * s["contorno"]


def _altura(linhas: int, s: dict, escala: int) -> int:
    return int(math.ceil(linhas * s["tamanho"] * escala / 100 * ALTURA_LINHA)) + 2 * s["contorno"]


def _quebrar(texto: str, s: dict, escala: int, maximo: int) -> list[str]:
    """Quebra gulosa por palavra, respeitando as quebras de linha do texto."""
    linhas: list[str] = []
    for paragrafo in texto.split("\n"):
        palavras = paragrafo.split()
        if not palavras:
            continue
        atual = palavras[0]
        for palavra in palavras[1:]:
            tentativa = f"{atual} {palavra}"
            if _largura(tentativa, s, escala) <= maximo:
                atual = tentativa
            else:
                linhas.append(atual)
                atual = palavra
        linhas.append(atual)
    return linhas


def _encaixar(texto: str, s: dict, maximo: int, escalas: list[int], max_linhas: int) -> tuple[list[str], int]:
    """Linhas e escala do bloco: a primeira escala em que o texto cabe em max_linhas (senão, a última)."""
    linhas, escala = [texto], escalas[-1]
    for escala in escalas:
        linhas = _quebrar(texto, s, escala, maximo)
        if len(linhas) <= max_linhas:
            break
    maior = max((_largura(linha, s, escala) for linha in linhas), default=0)
    if maior > maximo:  # uma palavra sozinha mais larga que a faixa: a escala desce até ela caber
        escala = max(1, int(escala * maximo / maior))
    return linhas, escala


def _texto_topo(texto: str, s: dict, maximo: int) -> tuple[list[str], int]:
    """Em escala para caber sem quebrar (até 72%); abaixo disso, a linha quebra."""
    paragrafos = [p.strip() for p in texto.split("\n") if p.strip()]
    escala = 100
    for p in paragrafos:
        largura = largura_texto(p, s["tamanho"], s["fonte"])
        if largura > 0:
            escala = min(escala, int(100 * (maximo - 2 * s["contorno"]) / largura))
    return _encaixar("\n".join(paragrafos), s, maximo, [max(72, min(100, escala))], len(paragrafos))


def _texto_chamada(texto: str, s: dict, maximo: int) -> tuple[list[str], int]:
    return _encaixar(texto, s, maximo, [100, 92, 84, 76, 72], 2)


# a frase começa em 80% do tamanho da legenda e cai de 8% em 8% até 55%, até caber em 4 linhas
_ESCALAS_FRASE = sorted({max(55, round(80 * 0.92 ** k)) for k in range(8)}, reverse=True)


def texto_da_frase(cfg: Config, frase: str) -> str:
    """A frase como é desenhada: entre aspas e em maiúsculas se o modelo usa."""
    texto = _sem_emoji(frase).replace("\n", " ").strip()
    texto = f"“{texto}”"
    return texto.upper() if cfg["edicao"]["maiusculas"] else texto


def _texto_frase(cfg: Config, frase: str, s: dict, maximo: int) -> tuple[list[str], int]:
    linhas, escala = _encaixar(texto_da_frase(cfg, frase), s, maximo, _ESCALAS_FRASE, 4)
    # ainda grande: corta a frase em 160 letras e conta de novo; na faixa estreita do 9x16, em maiúsculas,
    # 160 ainda dá mais de 4 linhas, então o corte desce de 20 em 20
    limite = 160
    while len(linhas) > 4 and limite >= 40:
        linhas, escala = _encaixar(texto_da_frase(cfg, limitar(_sem_emoji(frase), limite)), s, maximo,
                                   _ESCALAS_FRASE, 4)
        limite -= 20
    return linhas, escala


# ---------------------------------------------------------------- geometria

def _faixa(formato: str, zona: Zona, estreita: bool) -> tuple[int, int]:
    """(largura máxima, centro x) do bloco: a zona inteira, ou a faixa de 192 a 780 abaixo do meio no 9x16."""
    if formato == "9x16" and estreita:
        return DIREITA_BAIXO_9X16 - zona.x0, (zona.x0 + DIREITA_BAIXO_9X16) // 2
    return zona.w, (zona.x0 + zona.x1) // 2


def _caixa(bloco: dict) -> list[int]:
    x, y, w, h = bloco["x"], bloco["y"], bloco["w"], bloco["h"]
    meio = int(math.ceil(w / 2))
    return [x - meio, y - h, x + meio, y] if bloco["an"] == 2 else [x - meio, y, x + meio, y + h]


def _montar(cfg: Config, formato: str, aspecto: float, topo: str, chamada: str, frase: str,
            estreitos: set[str]) -> dict:
    largura, altura = TAMANHOS[formato]
    zona = ZONAS[formato]
    estilos = _estilos(cfg)

    def bloco(estilo: str, texto: str) -> dict:
        s = estilos[estilo]
        maximo, centro = _faixa(formato, zona, estilo in estreitos)
        if estilo == "Topo":
            linhas, escala = _texto_topo(texto, s, maximo)
        elif estilo == "Chamada":
            linhas, escala = _texto_chamada(texto, s, maximo)
        else:
            linhas, escala = _texto_frase(cfg, texto, s, maximo)
        w = int(math.ceil(max((_largura(linha, s, escala) for linha in linhas), default=0)))
        return {"estilo": estilo, "linhas": linhas, "escala": escala, "x": centro, "w": w,
                "h": _altura(len(linhas), s, escala)}

    b_topo = bloco("Topo", topo) if topo else None
    b_chamada = bloco("Chamada", chamada) if chamada else None
    b_frase = bloco("Frase", frase) if frase else None

    # quadro: a largura toda, a altura pela proporção (no 4x5, o bloco inteiro cabe na zona)
    w_q, h_q = largura, _par(largura / max(0.1, aspecto))
    if formato == "9x16":
        h_max = ALTURA_MAX_9X16
    else:
        h_max = zona.h - (b_topo["h"] + FOLGA if b_topo else 0) - (FOLGA + b_chamada["h"] if b_chamada else 0)
    frase_sobre = b_frase is not None
    if b_frase is not None and b_chamada is None and min(h_q, h_max) < b_frase["h"] + FOLGA_FRASE:
        frase_sobre = False  # o quadro é baixo para a frase: ela vai logo abaixo dele
        if formato != "9x16":
            h_max -= FOLGA + b_frase["h"]
    h_max = max(h_max, 160)
    if h_q > h_max:  # encolhe e fica no meio, com o fundo desfocado dos lados
        h_q = _par(h_max)
        w_q = min(largura, _par(h_q * aspecto))
    x_q = (largura - w_q) // 2

    baixo = b_chamada or (b_frase if not frase_sobre else None)
    acima = b_topo["h"] + FOLGA if b_topo else 0
    h_bloco = acima + h_q + (FOLGA + baixo["h"] if baixo else 0)
    if h_bloco <= zona.h:  # o bloco (topo, quadro e texto de baixo) centrado na zona
        y_q = zona.y0 + (zona.h - h_bloco) // 2 + acima
    else:  # maior que a zona (só no 9x16): o quadro no meio dela, e os textos dentro da zona
        y_q = (zona.y0 + zona.y1) // 2 - h_q // 2

    blocos = []
    if b_topo:
        b_topo.update(an=2, y=y_q - FOLGA)
        if b_topo["y"] - b_topo["h"] < zona.y0:  # passaria do alto da zona: ancorado por cima, sobre o quadro
            b_topo.update(an=8, y=zona.y0)
        blocos.append(b_topo)
    if baixo:
        baixo.update(an=8, y=y_q + h_q + FOLGA)
        if baixo["y"] + baixo["h"] > zona.y1:  # passaria da base da zona: ancorado por baixo, sobre o quadro
            baixo.update(an=2, y=zona.y1)
        blocos.append(baixo)
    if b_frase is not None and frase_sobre:
        b_frase.update(an=2, y=min(y_q + h_q - PE_FRASE, zona.y1))
        blocos.append(b_frase)
    for b in blocos:
        b["caixa"] = _caixa(b)
    return {
        "formato": formato, "largura": largura, "altura": altura,
        "zona": [zona.x0, zona.y0, zona.x1, zona.y1],
        "x_quadro": x_q, "y_quadro": y_q, "w_quadro": w_q, "h_quadro": h_q,
        "blocos": [{k: b[k] for k in ("estilo", "linhas", "x", "y", "an", "escala", "caixa")} for b in blocos],
    }


def geometria(cfg: Config, formato: str, aspecto: float, topo: str = "", chamada: str = "",
              frase: str = "") -> dict:
    """Onde ficam o quadro e cada texto de uma imagem. Função pura, a mesma da geração e da prévia.

    aspecto: largura / altura do quadro do filme (já com o recorte das tarjas e o SAR). topo e chamada
    são os textos já montados ("" = sem o bloco); frase é a fala crua (as aspas e as maiúsculas são
    postas aqui). Devolve largura, altura, zona, x/y/w/h do quadro e os blocos de texto, cada um com
    linhas, x, y, âncora (an 2 = por baixo, 8 = por cima), escala em % e a caixa [x0, y0, x1, y1].
    """
    if formato not in ZONAS:
        raise ValueError(f"formato desconhecido: {formato}")
    topo, chamada = _sem_emoji(topo), _sem_emoji(chamada)
    frase = _sem_emoji(frase)
    # no 9x16, o texto de baixo passa sempre do meio da tela; o topo só usa a faixa estreita se passar
    estreitos = {"Chamada", "Frase"} if formato == "9x16" else set()
    g = _montar(cfg, formato, aspecto, topo, chamada, frase, estreitos)
    for _ in range(2):
        cruzam = {b["estilo"] for b in g["blocos"] if formato == "9x16" and b["caixa"][3] > MEIO_9X16} - estreitos
        if not cruzam:
            break
        estreitos |= cruzam
        g = _montar(cfg, formato, aspecto, topo, chamada, frase, estreitos)
    return g


# ---------------------------------------------------------------- quadros do trecho

# quadros medidos por segundo do trecho
QUADROS_POR_SEGUNDO = 4
# regras do quadro bom: longe das bordas do trecho e das trocas de cena, nem escuro nem estourado
MARGEM_INICIO = 0.6
MARGEM_FIM = 0.8
LONGE_DA_CENA = 0.35
BRILHO_MIN = 40
BRILHO_MAX = 235
# no carrossel, o quadro que completa as cenas fica pelo menos isto longe dos outros
DISTANCIA_CARROSSEL = 1.5
# nome do filtro de nitidez (trocado no teste para simular um FFmpeg sem ele)
_FILTRO_NITIDEZ = "blurdetect"
_SEM_NITIDEZ_AVISADO = False


@dataclass(frozen=True)
class Quadro:
    t: float        # segundo do filme
    brilho: float   # YAVG do signalstats (0 a 255)
    nota: float


class Relogio:
    """O prazo único de uma geração (medição e renders): o que sobra vai como prazo de cada FFmpeg."""

    def __init__(self, prazo: float):
        self.prazo = float(prazo)
        self.fim = time.monotonic() + self.prazo

    def restante(self) -> float:
        return self.fim - time.monotonic()

    def erro(self) -> "ErroImagem":
        if self.prazo >= 60 and self.prazo % 60 == 0:
            texto = f"{int(self.prazo // 60)} min"
        else:
            texto = f"{int(round(self.prazo))} s"
        return ErroImagem(f"a geração das imagens passou de {texto}", False)

    def conferir(self) -> float:
        """O tempo que sobra; acabou, ErroImagem (do PC, não gasta o corte)."""
        sobra = self.restante()
        if sobra <= 0:
            raise self.erro()
        return sobra


def pasta_imagens(cfg: Config, corte_id: int) -> Path:
    return cfg.pasta_dados / "imagens" / str(corte_id)


def _ler_amostras(texto: str, inicio: float) -> list[tuple[float, float, float | None]]:
    """Amostras (t, brilho, blur) do arquivo do metadata=mode=print, com o tempo somado ao início."""
    amostras: list[list] = []
    chave_tempo = CHAVES_METADADO["tempo"] + ":"
    chave_brilho = CHAVES_METADADO["brilho"] + "="
    chave_nitidez = CHAVES_METADADO["nitidez"] + "="
    for linha in texto.splitlines():
        linha = linha.strip()
        try:
            if linha.startswith("frame:") and chave_tempo in linha:
                amostras.append([inicio + float(linha.split(chave_tempo, 1)[1].split()[0]), None, None])
            elif amostras and linha.startswith(chave_brilho):
                amostras[-1][1] = float(linha.split("=", 1)[1])
            elif amostras and linha.startswith(chave_nitidez):
                amostras[-1][2] = float(linha.split("=", 1)[1])
        except (ValueError, IndexError):
            continue
    # o blurdetect dá NaN num quadro todo preto: sem nitidez nesse quadro
    return [(round(t, 3), y, b if b is not None and math.isfinite(b) else None)
            for t, y, b in amostras if y is not None and math.isfinite(y)]


def medir_quadros(cfg: Config, filme, corte, info: dict, parar: threading.Event | None = None,
                  relogio: Relogio | None = None) -> list[tuple[float, float, float | None]]:
    """Brilho e nitidez do trecho a 4 quadros por segundo, numa passada do FFmpeg: [(t, brilho, blur)].

    Cache em dados/imagens/<corte>/quadros.json, que vale enquanto o início, o fim e o tamanho do
    arquivo do filme forem os mesmos. Sem o blurdetect no FFmpeg, mede só o brilho (blur = None).
    Falha do FFmpeg, prazo e nenhuma amostra lida são do PC: ErroImagem(do_corte=False).
    """
    global _SEM_NITIDEZ_AVISADO
    relogio = relogio or Relogio(300)
    caminho = Path(filme["caminho"])
    inicio, fim = float(corte["inicio"]), float(corte["fim"])
    try:
        tamanho = caminho.stat().st_size
    except OSError as erro:
        raise ErroImagem("o arquivo do filme não está na pasta agora", False, sem_linha=True) from erro
    pasta = pasta_imagens(cfg, corte["id"])
    arquivo_cache = pasta / "quadros.json"
    cache = ler_json(arquivo_cache)
    if (isinstance(cache, dict) and cache.get("inicio") == inicio and cache.get("fim") == fim
            and cache.get("tamanho_filme") == tamanho and cache.get("amostras")):
        return [(float(t), float(y), None if b is None else float(b)) for t, y, b in cache["amostras"]]

    pasta.mkdir(parents=True, exist_ok=True)
    bruto = pasta / "quadros.txt"
    dur = max(0.1, fim - inicio)
    cw, ch, cx, cy = info["crop"]
    com_nitidez = not _SEM_NITIDEZ_AVISADO
    while True:
        filtros = f"fps={QUADROS_POR_SEGUNDO},crop={cw}:{ch}:{cx}:{cy},scale=320:-2,signalstats"
        if com_nitidez:
            filtros += f",{_FILTRO_NITIDEZ}"
        filtros += f",metadata=mode=print:file={caminho_filtro(bruto)}"
        prazo = min(max(120.0, dur * 4), relogio.conferir())
        bruto.unlink(missing_ok=True)
        try:
            r = executar(
                base_ffmpeg(cfg["ferramentas"]["ffmpeg"]) + [
                    "-loglevel", "error", "-ss", f"{inicio:.3f}", "-t", f"{dur:.3f}", "-i", str(caminho),
                    "-map", f"0:{info['video']['indice']}", "-an", "-sn", "-dn", "-vf", filtros, "-f", "null", "-"],
                parar=parar, baixa_prioridade=cfg["geral"]["prioridade_baixa"], timeout=prazo)
        except ErroMidia as erro:
            bruto.unlink(missing_ok=True)
            if relogio.restante() <= 1:
                raise relogio.erro() from erro
            raise ErroImagem(f"a medição dos quadros falhou: {erro}", False) from erro
        except BaseException:
            bruto.unlink(missing_ok=True)
            raise
        if r.codigo != 0 and com_nitidez and _FILTRO_NITIDEZ in r.resumo_erro(8):
            com_nitidez = False  # FFmpeg sem o blurdetect: vale só o brilho
            if not _SEM_NITIDEZ_AVISADO:
                _SEM_NITIDEZ_AVISADO = True
                log.warning("O FFmpeg não tem o filtro %s: as imagens escolhem os quadros só pelo brilho",
                            _FILTRO_NITIDEZ)
            continue
        break
    try:
        texto = bruto.read_text(encoding="utf-8", errors="replace") if bruto.exists() else ""
    finally:
        bruto.unlink(missing_ok=True)
    if r.codigo != 0:
        raise ErroImagem(f"o FFmpeg falhou ao medir os quadros: {r.resumo_erro(2)}", False)
    amostras = _ler_amostras(texto, inicio)
    if not amostras:  # código 0 sem nada lido: é do PC ou do FFmpeg, não do trecho
        raise ErroImagem("o FFmpeg não mediu nenhum quadro do trecho", False)
    salvar_json(arquivo_cache, {"inicio": inicio, "fim": fim, "tamanho_filme": tamanho,
                                "amostras": [list(a) for a in amostras]})
    return amostras


def cenas_do_filme(cfg: Config, filme_id: int) -> list[float]:
    """As trocas de cena gravadas na análise (sem o cache, nenhuma: o trecho vira uma cena só)."""
    cenas = ler_json(analise.pasta_do_filme(cfg, filme_id) / "cenas.json", [])
    return sorted(float(c) for c in cenas if isinstance(c, (int, float))) if isinstance(cenas, list) else []


def quadros_validos(amostras: list[tuple[float, float, float | None]], inicio: float, fim: float,
                    cenas: list[float]) -> list[Quadro]:
    """Os quadros que podem sair, com a nota (0,7 da nitidez e 0,3 da distância da troca de cena)."""
    blurs = [b for _, _, b in amostras if b is not None]
    maior_blur = max(blurs, default=0.0)
    saida = []
    for t, brilho, blur in amostras:
        if t < inicio + MARGEM_INICIO or t > fim - MARGEM_FIM:
            continue
        if brilho < BRILHO_MIN or brilho > BRILHO_MAX:
            continue
        distancia = min((abs(t - c) for c in cenas), default=2.0)
        if distancia < LONGE_DA_CENA:
            continue
        nitidez = 0.5 if blur is None or maior_blur <= 0 else 1 - blur / maior_blur
        saida.append(Quadro(t, brilho, round(0.7 * nitidez + 0.3 * min(distancia, 2.0) / 2, 4)))
    return saida


def _melhor(quadros: list[Quadro]) -> Quadro | None:
    return max(quadros, key=lambda q: (q.nota, -q.t), default=None)


def _frase_pelo_texto(frases: list[Frase], texto: str, inicio: float, fim: float) -> Frase | None:
    """A primeira fala dentro do trecho (com 0,2 s de folga) com o mesmo texto normalizado."""
    alvo = normalizar(str(texto or "")).strip()
    if not alvo:
        return None
    for f in frases:
        if f.ini >= inicio - 0.2 and f.fim <= fim + 0.2 and normalizar(f.txt).strip() == alvo:
            return f
    return None


def _escolher_momento(validos: list[Quadro]) -> Quadro:
    melhor = _melhor(validos)
    if melhor is None:
        raise ErroImagem("o trecho não tem nenhum quadro bom (escuro, estourado ou em troca de cena)", True)
    return melhor


def _escolher_frase(corte, frases: list[Frase], validos: list[Quadro], inicio: float,
                    fim: float) -> tuple[Frase, Quadro]:
    chaves = set(corte.keys())
    frase = _frase_pelo_texto(frases, corte["frase"] if "frase" in chaves else "", inicio, fim)
    if frase is None:  # a transcrição foi refeita: a melhor fala do trecho, como na mineração
        frase = _frase_pelo_texto(frases, selecao.melhor_frase(frases, inicio, fim), inicio, fim)
    if frase is None:
        raise ErroImagem("o trecho não tem fala", True)
    durante = [q for q in validos if frase.ini - 0.3 <= q.t <= frase.fim + 0.3]
    quadro = _melhor(durante)
    if quadro is None:  # nenhum bom durante a fala: o mais perto, a até 1,5 s
        perto = [q for q in validos if frase.ini - 1.5 <= q.t <= frase.fim + 1.5]
        quadro = min(perto, key=lambda q: max(frase.ini - q.t, q.t - frase.fim, 0), default=None)
    if quadro is None:
        quadro = _escolher_momento(validos)
        log.info("Imagem de frase do corte %s: nenhum quadro bom perto da fala, usando o melhor do trecho",
                 corte["id"])
    return frase, quadro


def _escolher_carrossel(validos: list[Quadro], cenas: list[float], inicio: float, fim: float,
                        n: int) -> list[Quadro]:
    """O melhor quadro de cada cena, as n cenas de maior nota, em ordem de tempo."""
    cortes_cena = [inicio] + [c for c in cenas if inicio < c < fim] + [fim + 1]
    por_cena = []
    for a, b in zip(cortes_cena, cortes_cena[1:]):
        melhor = _melhor([q for q in validos if a <= q.t < b])
        if melhor is not None:
            por_cena.append(melhor)
    escolhidos = sorted(por_cena, key=lambda q: (-q.nota, q.t))[:n]
    if len(escolhidos) < n:  # poucas cenas: completa com os melhores restantes, longe dos já escolhidos
        for q in sorted(validos, key=lambda q: (-q.nota, q.t)):
            if len(escolhidos) >= n:
                break
            if all(abs(q.t - e.t) >= DISTANCIA_CARROSSEL for e in escolhidos):
                escolhidos.append(q)
    if len(escolhidos) < 3:
        raise ErroImagem("o trecho não tem 3 quadros bons diferentes", True)
    return sorted(escolhidos, key=lambda q: q.t)


def escolher(tipo: str, corte, amostras: list[tuple[float, float, float | None]], cenas: list[float],
             frases: list[Frase], quadros: int = 4) -> dict:
    """Os segundos do filme usados na imagem e, no tipo frase, a fala desenhada.

    Devolve {"tempos": [...], "texto": str}. Trecho sem quadro bom, sem fala ou sem 3 quadros
    diferentes no carrossel: ErroImagem(do_corte=True), o corte sai deste tipo.
    """
    inicio, fim = float(corte["inicio"]), float(corte["fim"])
    validos = quadros_validos(amostras, inicio, fim, cenas)
    if tipo == "momento":
        return {"tempos": [_escolher_momento(validos).t], "texto": ""}
    if tipo == "frase":
        frase, quadro = _escolher_frase(corte, frases, validos, inicio, fim)
        return {"tempos": [quadro.t], "texto": frase.txt.strip()}
    if tipo == "carrossel":
        return {"tempos": [q.t for q in _escolher_carrossel(validos, cenas, inicio, fim, int(quadros))], "texto": ""}
    raise ValueError(f"tipo de imagem desconhecido: {tipo}")


# ---------------------------------------------------------------- render

# prazo de cada FFmpeg que desenha uma imagem
PRAZO_RENDER = 120
# muda quando o desenho muda: as imagens ainda não postadas são refeitas
VERSAO_DESENHO = 1
# opções do modelo visual que mudam o desenho das imagens (as do vídeo, como zoom e moldura, ficam de fora)
CHAVES_VISUAIS = (
    "fonte_arquivo", "fonte_nome", "fonte_topo_arquivo", "fonte_topo_nome", "tamanho_legenda", "tamanho_topo",
    "cor_legenda", "cor_destaque", "cor_topo", "contorno_legenda", "maiusculas", "desfoque_fundo", "escurecer_fundo",
)


def _aspecto(info: dict) -> float:
    cw, ch = info["crop"][0], info["crop"][1]
    sar = info["video"].get("sar") or [1, 1]
    fator = Fraction(int(sar[0]), int(sar[1])) if int(sar[1] or 0) else Fraction(1)
    return float(cw * fator) / max(1, ch)


def _renderizar_quadro(cfg: Config, entrada: Path, t: float | None, info: dict, g: dict, destino: Path,
                       relogio: Relogio, parar: threading.Event | None = None) -> Path:
    """Uma imagem: fundo desfocado do próprio quadro, o quadro do filme e os textos (ASS) por cima.

    t = None: a entrada já é uma imagem parada (a prévia sem filme). O JPEG sai num nome temporário
    e só troca de lugar inteiro; o .ass temporário sai em qualquer caso.
    """
    e = cfg["edicao"]
    largura, altura = g["largura"], g["altura"]
    h4 = altura_fundo(altura)
    destino.parent.mkdir(parents=True, exist_ok=True)
    marca = f"{os.getpid()}-{threading.get_ident()}"
    parcial = destino.with_name(f"{destino.stem}.{marca}.parcial.jpg")
    ass = destino.with_name(f"{destino.stem}.{marca}.parcial.ass")
    cw, ch, cx, cy = info["crop"]
    indice = info["video"].get("indice", 0) if t is not None else 0
    filtro = ";".join([
        f"[0:{indice}]crop={cw}:{ch}:{cx}:{cy},scale={g['w_quadro']}:{g['h_quadro']}:flags=lanczos,setsar=1,"
        "split=2[f][b]",
        f"[b]scale=270:{h4}:force_original_aspect_ratio=increase,crop=270:{h4},boxblur={int(e['desfoque_fundo'])}:2,"
        f"eq=brightness={-float(e['escurecer_fundo']):.3f}:saturation=1.15,scale={largura}:{altura}:flags=bilinear,"
        "setsar=1[fundo]",
        f"[fundo][f]overlay=x={g['x_quadro']}:y={g['y_quadro']},{filtro_ass(ass, preparar_fonte(cfg))},"
        "format=yuvj420p[saida]",
    ])
    busca = ["-ss", f"{t:.3f}"] if t is not None else []
    try:
        ass.write_text(gerar_ass_cartaz(estilo_legenda(cfg), g["blocos"], largura, altura), encoding="utf-8")
        prazo = min(PRAZO_RENDER, relogio.conferir())
        try:
            r = executar(
                base_ffmpeg(cfg["ferramentas"]["ffmpeg"]) + [
                    "-loglevel", "error", *busca, "-i", str(entrada), "-filter_complex", filtro,
                    "-map", "[saida]", "-frames:v", "1", "-q:v", "2", "-update", "1", str(parcial)],
                parar=parar, baixa_prioridade=cfg["geral"]["prioridade_baixa"], timeout=prazo)
        except ErroMidia as erro:
            if relogio.restante() <= 1:
                raise relogio.erro() from erro
            raise ErroImagem(f"o FFmpeg demorou demais para desenhar a imagem: {erro}", False) from erro
        if r.codigo != 0 or not parcial.is_file() or parcial.stat().st_size == 0:
            raise ErroImagem(f"o FFmpeg falhou ao desenhar a imagem: {r.resumo_erro(3)}", False)
        try:
            trocar_arquivo(parcial, destino)  # a imagem antiga pode estar aberta no painel
        except OSError as erro:
            raise ErroImagem(f"não consegui pôr a imagem pronta no lugar de {destino.name}: {erro}", False) from erro
        return destino
    finally:
        parcial.unlink(missing_ok=True)
        ass.unlink(missing_ok=True)


def _textos_do_desenho(cfg: Config, variaveis: dict, com_chamada: bool) -> tuple[str, str]:
    """(topo, chamada) já montados; a linha com a parte vazia some."""
    im = cfg["imagens"]
    topo = aplicar_modelo_sem_vazias(str(im["topo"]), variaveis)
    chamada = aplicar_modelo_sem_vazias(str(im["chamada"]), variaveis) if com_chamada else ""
    return topo, chamada


def _desenhar(cfg: Config, entrada: Path, info: dict, tempos: list[float | None], tipo: str, formato: str,
              topo: str, chamada: str, frase: str, pasta: Path, prefixo: str, relogio: Relogio,
              parar: threading.Event | None) -> list[Path]:
    """As imagens do post, na ordem: {prefixo}-1.jpg, -2.jpg... As que sobraram de um post maior saem."""
    aspecto = _aspecto(info)
    arquivos = []
    for i, t in enumerate(tempos):
        primeiro, ultimo = i == 0, i == len(tempos) - 1
        if tipo == "carrossel":  # o primeiro leva o topo, o último a chamada, os do meio só o quadro
            textos = dict(topo=topo if primeiro else "", chamada=chamada if ultimo else "")
        elif tipo == "frase":
            textos = dict(topo=topo, frase=frase)
        else:
            textos = dict(topo=topo, chamada=chamada)
        g = geometria(cfg, formato, aspecto, **textos)
        arquivos.append(_renderizar_quadro(cfg, entrada, t, info, g, pasta / f"{prefixo}-{i + 1}.jpg", relogio, parar))
    validos = {a.name for a in arquivos}
    for sobra in pasta.glob(f"{prefixo}-*.jpg"):
        if sobra.name not in validos and re.fullmatch(rf"{re.escape(prefixo)}-\d+\.jpg", sobra.name):
            sobra.unlink(missing_ok=True)
    return arquivos


def assinatura(cfg: Config, tipo: str, formato: str, com_chamada: bool, quadros: int | None = None) -> str:
    """sha256 curto do visual e dos modelos usados (o número de quadros conta só no carrossel)."""
    dados = {
        "visual": {k: cfg["edicao"].get(k) for k in CHAVES_VISUAIS},
        "topo": cfg["imagens"]["topo"], "chamada": cfg["imagens"]["chamada"], "com_chamada": bool(com_chamada),
        "tipo": tipo, "formato": formato, "versao": VERSAO_DESENHO,
    }
    if tipo == "carrossel":
        dados["quadros"] = int(quadros if quadros is not None else cfg["imagens"]["quadros_carrossel"])
    return hashlib.sha256(json.dumps(dados, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def _filme_no_disco(cfg: Config, filme) -> tuple[dict, list[Frase]]:
    """info e falas do filme; o arquivo ou a análise fora da pasta não é do trecho (sem_linha)."""
    sumiu = ErroImagem("o arquivo do filme não está na pasta agora", False, sem_linha=True)
    if not Path(filme["caminho"]).is_file() or not (analise.pasta_do_filme(cfg, filme["id"]) / "info.json").is_file():
        raise sumiu
    try:
        return analise.carregar_para_render(cfg, filme["id"])
    except ErroMidia as erro:
        raise sumiu from erro


def _postada(cfg: Config, conn: sqlite3.Connection, linha) -> bool:
    """A imagem já saiu num post do mesmo tipo (a linha da gravação sem tipo não congela nada: DF-9)."""
    from . import planejador  # o planejador importa o gravador, que usa este módulo

    concl = planejador.status_concluidos(cfg)
    marcas = ",".join("?" * len(concl))
    return conn.execute(
        f"SELECT 1 FROM postagens WHERE imagem_id = ? AND midia = 'imagem' AND imagem_tipo = ? "
        f"AND status IN ({marcas}) LIMIT 1", (linha["id"], linha["tipo"], *concl)).fetchone() is not None


def preparar(cfg: Config, conn: sqlite3.Connection, corte, tipo: str, rede: str,
             parar: threading.Event | None = None, quadros: int | None = None, prazo: float = 300,
             post_simulado: bool | None = None) -> sqlite3.Row:
    """Gera (ou reaproveita) as imagens de um post e devolve a linha da tabela imagens.

    Tudo sai de um retrato do config: o painel pode salvar no meio sem misturar dois visuais.
    quadros vale só no carrossel (sem ele, [imagens].quadros_carrossel). post_simulado diz se o post
    que vai sair é simulado (None = cfg.simulacao; a gravação passa False): é o que decide se o vídeo
    simulado vale para desenhar a chamada. Reaproveita a linha com a mesma assinatura e os arquivos no
    lugar, e nunca redesenha a que já saiu num post do mesmo tipo. Quem chama segura a TRAVA.
    """
    cfg = cfg.copia()
    from . import planejador  # o planejador importa o gravador, que usa este módulo

    relogio = Relogio(prazo)
    filme = conn.execute("SELECT * FROM filmes WHERE id = ?", (corte["filme_id"],)).fetchone()
    if filme is None:
        raise ErroImagem("o filme deste corte não está mais no banco", False, sem_linha=True)
    if (filme["tipo"] or "filme") != "filme":
        raise ErroImagem("este corte é de um vídeo criado, não de um filme", True)
    if tipo not in ("momento", "frase", "carrossel"):
        raise ValueError(f"tipo de imagem desconhecido: {tipo}")
    info, frases = _filme_no_disco(cfg, filme)
    formato = FORMATOS_IMAGEM[rede]["nome"]
    simulado = cfg.simulacao if post_simulado is None else bool(post_simulado)
    com_chamada = bool(cfg[rede].get("videos", True) and corte["parte"]
                       and planejador.video_publicado(conn, corte["id"], rede, aceitar_simulado=simulado))
    n = int(quadros if quadros is not None else cfg["imagens"]["quadros_carrossel"])
    marca = assinatura(cfg, tipo, formato, com_chamada, n)

    linha = conn.execute("SELECT * FROM imagens WHERE corte_id = ? AND tipo = ? AND formato = ?",
                         (corte["id"], tipo, formato)).fetchone()
    if linha is not None:
        arquivos = [Path(a) for a in json.loads(linha["arquivos"] or "[]")]
        if (linha["assinatura"] == marca and arquivos and all(a.is_file() for a in arquivos)) \
                or _postada(cfg, conn, linha):
            return linha

    amostras = medir_quadros(cfg, filme, corte, info, parar, relogio)
    escolha = escolher(tipo, corte, amostras, cenas_do_filme(cfg, filme["id"]), frases, n)
    variaveis = _variaveis_e_tags(cfg, filme, corte, False)[0]
    topo, chamada = _textos_do_desenho(cfg, variaveis, com_chamada)
    arquivos = _desenhar(cfg, Path(filme["caminho"]), info, escolha["tempos"], tipo, formato, topo, chamada,
                         escolha["texto"], pasta_imagens(cfg, corte["id"]), f"{tipo}-{formato}", relogio, parar)
    conn.execute(
        "INSERT INTO imagens (corte_id, tipo, formato, arquivos, quadros, texto, com_chamada, assinatura, modelo, "
        "criado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(corte_id, tipo, formato) DO UPDATE SET arquivos = excluded.arquivos, quadros = excluded.quadros, "
        "texto = excluded.texto, com_chamada = excluded.com_chamada, assinatura = excluded.assinatura, "
        "modelo = excluded.modelo, criado_em = excluded.criado_em",
        (corte["id"], tipo, formato, json.dumps([str(a) for a in arquivos], ensure_ascii=False),
         json.dumps(escolha["tempos"]), escolha["texto"] or None, int(com_chamada), marca,
         str(cfg["edicao"].get("modelo") or ""), time.time()))
    return conn.execute("SELECT * FROM imagens WHERE corte_id = ? AND tipo = ? AND formato = ?",
                        (corte["id"], tipo, formato)).fetchone()


def _corte_de_exemplo(cfg: Config, conn: sqlite3.Connection, tipo: str):
    """O corte editado de filme mais recente com o filme e a análise no disco (no tipo frase, com fala)."""
    frase = "AND TRIM(COALESCE(c.frase, '')) <> ''" if tipo == "frase" else ""
    linhas = conn.execute(
        "SELECT c.* FROM cortes c JOIN filmes f ON f.id = c.filme_id "
        "WHERE c.status IN ('pronto', 'concluido') AND f.tipo = 'filme' AND f.status = 'analisado' "
        f"{frase} ORDER BY COALESCE(c.renderizado_em, c.fila_em, c.criado_em) DESC, c.id DESC LIMIT 20").fetchall()
    for corte in linhas:
        filme = conn.execute("SELECT * FROM filmes WHERE id = ?", (corte["filme_id"],)).fetchone()
        if Path(filme["caminho"]).is_file() and (analise.pasta_do_filme(cfg, filme["id"]) / "info.json").is_file():
            return corte, filme
    return None, None


def previa(cfg: Config, conn: sqlite3.Connection, tipo: str, rede: str, destino_pasta: Path,
           parar: threading.Event | None = None) -> tuple[list[Path], bool]:
    """Imagens de exemplo com o visual em uso: (arquivos, real).

    Com um corte editado de verdade, o mesmo caminho do preparar (sem gravar linha); sem nenhum, ou
    se o trecho dele não serve para o tipo, o quadro de teste com "Nome do Filme" (real = False).
    Grava em <destino_pasta>/imagem-<tipo>-<formato>-<n>.jpg. Quem chama segura a TRAVA.
    """
    cfg = cfg.copia()
    from . import planejador  # o planejador importa o gravador, que usa este módulo

    relogio = Relogio(300)
    formato = FORMATOS_IMAGEM[rede]["nome"]
    prefixo = f"imagem-{tipo}-{formato}"
    n = int(cfg["imagens"]["quadros_carrossel"])
    corte, filme = _corte_de_exemplo(cfg, conn, tipo)
    if corte is not None:
        try:
            info, frases = _filme_no_disco(cfg, filme)
            com_chamada = bool(cfg[rede].get("videos", True) and corte["parte"]
                               and planejador.video_publicado(conn, corte["id"], rede, aceitar_simulado=cfg.simulacao))
            amostras = medir_quadros(cfg, filme, corte, info, parar, relogio)
            escolha = escolher(tipo, corte, amostras, cenas_do_filme(cfg, filme["id"]), frases, n)
            topo, chamada = _textos_do_desenho(cfg, _variaveis_e_tags(cfg, filme, corte, False)[0], com_chamada)
            return _desenhar(cfg, Path(filme["caminho"]), info, escolha["tempos"], tipo, formato, topo, chamada,
                             escolha["texto"], destino_pasta, prefixo, relogio, parar), True
        except ErroImagem as erro:
            if not erro.do_corte and not erro.sem_linha:
                raise
            log.debug("Prévia de imagem sem o corte %s: %s", corte["id"], erro)
    quadro = quadro_exemplo(cfg, destino_pasta / f"{prefixo}.jpg")
    info = info_do_quadro(cfg, quadro)
    info["video"]["indice"] = 0
    variaveis = {"filme": "Nome do Filme", "ano": "", "ano_parenteses": "", "parte": 1}
    topo, chamada = _textos_do_desenho(cfg, variaveis, bool(cfg[rede].get("videos", True)))
    tempos: list[float | None] = [None] * (n if tipo == "carrossel" else 1)
    return _desenhar(cfg, quadro, info, tempos, tipo, formato, topo, chamada, EXEMPLO_LEGENDA, destino_pasta,
                     prefixo, relogio, parar), False
