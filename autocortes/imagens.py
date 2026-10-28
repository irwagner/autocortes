"""Posts de imagem tirados dos cortes: geometria, escolha dos quadros e render com o FFmpeg.

Três tipos: momento (um quadro com o nome do filme e a parte), frase (a fala real do trecho sobre o
quadro) e carrossel (3 a 5 quadros de cenas diferentes, em ordem). Dois formatos: 4x5 (1080x1350, o
Instagram) e 9x16 (1080x1920, o TikTok). A geometria é calculada aqui e serve à geração, à prévia e
ao teste: o painel nunca refaz a conta.
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass

from .config import FORMATOS_IMAGEM, Config
from .edicao import LARGURA_TITULO, SEGURO_BASE, SEGURO_TOPO, fonte_do_topo
from .legendas import largura_texto
from .textos import limitar

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
