"""Edição: transforma um trecho do filme em vídeo vertical pronto para as redes."""

from __future__ import annotations

import math
import os
import shutil
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

from .config import Config
from .legendas import (
    EstiloLegenda, Frase, blocos_legenda, escala_para_caber, gerar_ass, largura_texto, palavras_estimadas,
    palavras_no_intervalo, piso_escala,
)
from .midia import (
    ErroMidia, Progresso, base_ffmpeg, caminho_filtro, duracao_arquivo, executar, fracao, saida_binaria, sondar,
)
from .util import log


def _par(valor: float) -> int:
    v = int(round(valor))
    return max(2, v - (v % 2))


@dataclass(frozen=True)
class Area:
    """Retângulo em pixels do vídeo final: a janela transparente da moldura."""
    x: int
    y: int
    w: int
    h: int


@dataclass
class Geometria:
    layout: str
    escala_w: int     # tamanho do filme depois do zoom (antes do recorte)
    escala_h: int
    frente_w: int     # área visível do filme
    frente_h: int
    frente_y: int
    topo_y: int       # base do texto do topo
    legenda_y: int    # centro da legenda
    barra_y: int
    frente_x: int = 0
    barra_x: int = 0
    barra_w: int = 0
    # filme mais estreito que a área: a barra corre na base do próprio filme
    barra_no_filme: bool = False
    area: Area | None = None  # janela da moldura (None = tela inteira)


# Faixa que fica livre da interface do TikTok, Reels e Shorts, em proporção da tela.
# Vem dos guias de anúncio das plataformas (não existe guia oficial para posts comuns):
# em 1080x1920, o topo até y=288 e a base a partir de y=1248 ficam cobertos.
SEGURO_TOPO = 0.15
SEGURO_BASE = 0.65
# largura máxima dos textos, centrados: 696 px em 1080 (x de 192 a 888, antes dos botões do Shorts).
# O modelo de anúncios do TikTok reserva ainda mais à direita abaixo do meio da tela (até x=780).
LARGURA_TITULO = 0.644
LARGURA_LEGENDA = 0.644
# texto de exemplo da prévia e do Estúdio
EXEMPLO_LEGENDA = "Você não sabe com quem está falando"
# com moldura, o filme vai estes pixels além da janela, por baixo da borda (cobre o antisserrilhado dela)
SANGRIA_MOLDURA = 4


# ---------------------------------------------------------------- moldura

# a thread de edição e as do painel leem as molduras ao mesmo tempo
_CACHE_MOLDURAS: OrderedDict[tuple, dict] = OrderedDict()
_TRAVA_MOLDURAS = threading.Lock()
_LIMITE_CACHE = 64
_MOLDURAS_AVISADAS: set[str] = set()


def _avisar_uma_vez(chave: str, mensagem: str, *args) -> None:
    if chave not in _MOLDURAS_AVISADAS:
        _MOLDURAS_AVISADAS.add(chave)
        log.warning(mensagem, *args)


def caminho_moldura(cfg: Config) -> Path | None:
    """Arquivo da moldura configurada (None = sem moldura, ou arquivo que não existe mais)."""
    nome = str(cfg["edicao"].get("moldura") or "").strip()
    if not nome:
        return None
    caminho = cfg.caminho_de(nome)
    if not caminho.is_file():
        _avisar_uma_vez(f"falta:{caminho}", "Moldura não encontrada: %s (editando sem ela)", caminho)
        return None
    return caminho


def _filtro_moldura(largura: int, altura: int, png_w: int, png_h: int) -> str:
    """Leva a moldura ao tamanho do vídeo: estica se a proporção for quase a mesma, senão encaixa no meio."""
    if png_w and png_h and abs(png_w / png_h - largura / altura) <= 0.03 * (largura / altura):
        return f"scale={largura}:{altura}:flags=lanczos,format=rgba"
    return (f"scale={largura}:{altura}:force_original_aspect_ratio=decrease:flags=lanczos,format=rgba,"
            f"pad={largura}:{altura}:(ow-iw)/2:(oh-ih)/2:color=black@0")


def _faixa_transparente(seq: bytes, n: int) -> tuple[int, int] | None:
    """(início, fim) da maior sequência transparente de uma linha ou coluna de alfa."""
    melhor, i = None, 0
    while i < n:
        if seq[i] < 128:
            ini = i
            while i < n and seq[i] < 128:
                i += 1
            if melhor is None or i - ini > melhor[1] - melhor[0]:
                melhor = (ini, i)
        else:
            i += 1
    return melhor


def _achar_janela(alfa: bytes, largura: int, altura: int) -> Area | None:
    """Área transparente em volta do meio da imagem, onde o vídeo aparece."""
    cx, cy = largura // 2, altura // 2
    coluna = alfa[cx::largura]
    if coluna[cy] >= 128:  # o meio é opaco: procura a maior faixa transparente na coluna do meio, depois na linha
        faixa = _faixa_transparente(coluna, altura)
        if faixa is not None:
            cy = (faixa[0] + faixa[1] - 1) // 2
        else:
            faixa = _faixa_transparente(alfa[cy * largura:(cy + 1) * largura], largura)
            if faixa is None:
                return None
            cx = (faixa[0] + faixa[1] - 1) // 2
            coluna = alfa[cx::largura]
    linha = alfa[cy * largura:(cy + 1) * largura]
    x0 = x1 = cx
    while x0 > 0 and linha[x0 - 1] < 128:
        x0 -= 1
    while x1 < largura - 1 and linha[x1 + 1] < 128:
        x1 += 1
    y0 = y1 = cy
    while y0 > 0 and coluna[y0 - 1] < 128:
        y0 -= 1
    while y1 < altura - 1 and coluna[y1 + 1] < 128:
        y1 += 1
    x0, y0 = x0 - x0 % 2, y0 - y0 % 2
    w, h = _par(x1 + 1 - x0), _par(y1 + 1 - y0)
    if w * h < 0.05 * largura * altura:
        return None  # buraco pequeno demais para ser a área do vídeo
    return Area(x0, y0, w, h)


def info_moldura(cfg: Config, caminho: Path) -> dict:
    """Tamanho original, filtro de encaixe e janela transparente (em pixels do vídeo final)."""
    e = cfg["edicao"]
    largura, altura = int(e["largura"]), int(e["altura"])
    st = caminho.stat()
    chave = (str(caminho.resolve()), st.st_mtime_ns, st.st_size, largura, altura)
    with _TRAVA_MOLDURAS:
        dados = _CACHE_MOLDURAS.get(chave)
        if dados is not None:
            _CACHE_MOLDURAS.move_to_end(chave)
            return dados
    sonda = sondar(cfg["ferramentas"]["ffprobe"], caminho)
    v = next((s for s in sonda.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise ErroMidia("a moldura não é uma imagem válida")
    png_w, png_h = int(v.get("width") or 0), int(v.get("height") or 0)
    filtro = _filtro_moldura(largura, altura, png_w, png_h)
    bruto = saida_binaria(
        base_ffmpeg(cfg["ferramentas"]["ffmpeg"])
        + ["-loglevel", "error", "-i", str(caminho), "-frames:v", "1", "-vf", filtro,
           "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
        timeout=60,
    )
    if len(bruto) != largura * altura * 4:
        raise ErroMidia("não foi possível ler os pixels da moldura")
    janela = _achar_janela(bruto[3::4], largura, altura)
    dados = {"largura": png_w, "altura": png_h, "filtro": filtro, "janela": janela}
    with _TRAVA_MOLDURAS:
        for antiga in [k for k in _CACHE_MOLDURAS if k[0] == chave[0] and k != chave]:
            del _CACHE_MOLDURAS[antiga]  # a mesma imagem antes de ser trocada
        _CACHE_MOLDURAS[chave] = dados
        while len(_CACHE_MOLDURAS) > _LIMITE_CACHE:
            _CACHE_MOLDURAS.popitem(last=False)
    return dados


def moldura_atual(cfg: Config) -> tuple[Path | None, dict | None]:
    """(arquivo, informações) da moldura configurada; (None, None) se não houver ou não der para usar."""
    caminho = caminho_moldura(cfg)
    if caminho is None:
        return None, None
    try:
        dados = info_moldura(cfg, caminho)
    except (ErroMidia, OSError) as e:
        _avisar_uma_vez(f"erro:{caminho}", "Moldura %s não pôde ser lida (%s): editando sem ela", caminho.name, e)
        return None, None
    if dados["janela"] is None:
        # sem área transparente ela cobriria o vídeo inteiro: melhor editar sem ela
        _avisar_uma_vez(f"opaca:{caminho}", "A moldura %s não tem área transparente para o vídeo: editando sem ela",
                        caminho.name)
        return None, None
    return caminho, dados


def problema_moldura(cfg: Config) -> str | None:
    """Por que a moldura configurada não está sendo usada (None = está certa, ou não há moldura)."""
    nome = str(cfg["edicao"].get("moldura") or "").strip()
    if not nome:
        return None
    caminho = cfg.caminho_de(nome)
    if not caminho.is_file():
        return f"A moldura {Path(nome).name} não foi encontrada"
    try:
        dados = info_moldura(cfg, caminho)
    except (ErroMidia, OSError):
        return f"A moldura {caminho.name} não pôde ser lida"
    if dados["janela"] is None:
        return f"A moldura {caminho.name} não tem área transparente para o vídeo"
    return None


# ---------------------------------------------------------------- geometria

def fonte_do_topo(cfg: Config) -> tuple[str, str]:
    """(arquivo, nome) da fonte do título: a própria, ou a mesma da legenda."""
    e = cfg["edicao"]
    arquivo = str(e.get("fonte_topo_arquivo") or "").strip()
    if arquivo:
        return arquivo, str(e.get("fonte_topo_nome") or "").strip() or Path(arquivo).stem
    return str(e["fonte_arquivo"]), str(e["fonte_nome"])


def larguras_texto(cfg: Config, area: Area | None) -> tuple[int, int]:
    """Largura máxima (legenda, título) em pixels: a faixa segura dos apps, e dentro da moldura."""
    largura = int(cfg["edicao"]["largura"])
    legenda, titulo = int(largura * LARGURA_LEGENDA), int(largura * LARGURA_TITULO)
    if area is not None:
        legenda, titulo = min(legenda, area.w - 60), min(titulo, area.w - 60)
        centro = area.x + area.w // 2
        if abs(centro - largura // 2) > 8:
            # janela fora do meio: o texto continua longe dos botões dos apps (x de 192 a 888 em 1080)
            margem = int(largura * (1 - LARGURA_LEGENDA) / 2)
            livre = 2 * max(0, min(centro - margem, largura - margem - centro))
            legenda, titulo = min(legenda, livre), min(titulo, livre)
    return max(120, legenda), max(120, titulo)


def linhas_do_topo(texto: str, cfg: Config | None = None, maximo: int | None = None) -> float:
    """Altura do título em "linhas cheias", contando a redução e as quebras de linha do libass."""
    linhas = [linha.strip() for linha in str(texto).split("\n") if linha.strip()]
    if not linhas or cfg is None:
        return float(len(linhas))
    e = cfg["edicao"]
    tamanho, fonte = int(e["tamanho_topo"]), fonte_do_topo(cfg)[1]
    maximo = maximo or int(int(e["largura"]) * LARGURA_TITULO)
    esc = min(escala_para_caber(linha, tamanho, fonte, 5, maximo) for linha in linhas) / 100
    quebradas = sum(max(1, math.ceil((largura_texto(linha, tamanho * esc, fonte) + 10) / maximo)) for linha in linhas)
    return quebradas * esc


def calcular_geometria(cfg: Config, info: dict, linhas_topo: float | None = None,
                       janela: Area | None = None) -> Geometria:
    e = cfg["edicao"]
    largura, altura = int(e["largura"]), int(e["altura"])
    area = janela or Area(0, 0, largura, altura)
    if janela is not None:  # o filme passa um pouco por baixo da borda da moldura
        x0, y0 = max(0, area.x - SANGRIA_MOLDURA), max(0, area.y - SANGRIA_MOLDURA)
        area_filme = Area(x0, y0, min(largura, area.x + area.w + SANGRIA_MOLDURA) - x0,
                          min(altura, area.y + area.h + SANGRIA_MOLDURA) - y0)
    else:
        area_filme = area
    cw, ch, _, _ = info["crop"]
    sar = Fraction(*info["video"]["sar"]) if info["video"]["sar"][1] else Fraction(1)
    aspecto = float(cw * sar) / max(1, ch)
    layout = e["layout"]
    zoom = float(e["zoom"])
    tamanho_legenda = int(e["tamanho_legenda"])

    if linhas_topo is None:
        linhas_topo = linhas_do_topo(e["texto_topo"])
    seguro_topo, seguro_base = int(altura * SEGURO_TOPO), int(altura * SEGURO_BASE)
    if janela is not None:  # os textos também ficam dentro da moldura
        seguro_topo = max(seguro_topo, area.y + 20)
        seguro_base = min(seguro_base, area.y + area.h - 20)
    base_titulo = seguro_topo + int(linhas_topo * int(e["tamanho_topo"]) * 1.4)  # o título é alinhado por baixo
    meia_legenda = int(tamanho_legenda * 0.72) + int(e["contorno_legenda"])      # meia altura de 1 linha
    legenda_limite = seguro_base - meia_legenda - 8                              # centro mais baixo permitido

    if layout in ("desfocado", "preto"):
        escala_w = _par(area_filme.w * zoom)
        escala_h = _par(escala_w / aspecto)
        if escala_h >= area.h * 0.9:
            if zoom >= 1:  # vídeo já vertical/quase quadrado alto: ocupa a área
                layout = "preencher"
            elif escala_h > area_filme.h:  # vídeo menor pedido de propósito: cabe na altura
                escala_h = _par(area_filme.h)
                escala_w = _par(escala_h * aspecto)

    barra_no_filme = False
    if layout == "preencher":
        fator = max(area_filme.w / (cw * float(sar)), area_filme.h / ch)
        escala_w, escala_h = _par(cw * float(sar) * fator), _par(ch * fator)
        frente_w, frente_h, frente_x, frente_y = area_filme.w, area_filme.h, area_filme.x, area_filme.y
        topo_y = base_titulo + int(altura * 0.01)
        legenda_y = legenda_limite - int(altura * 0.012)
        barra_y = min(legenda_y + meia_legenda + 14, seguro_base - 10)
        barra_x, barra_w = area.x, area.w
    else:
        frente_w, frente_h = min(escala_w, area_filme.w), min(escala_h, area_filme.h)
        frente_x = area_filme.x + (area_filme.w - frente_w) // 2
        # filme um pouco acima do centro: sobra espaço para a legenda antes da descrição do app
        frente_y = max(area_filme.y, int(area.y + area.h * 0.42 - frente_h / 2))
        abaixo_do_titulo = base_titulo + 14
        if linhas_topo and frente_y < abaixo_do_titulo and abaixo_do_titulo + frente_h <= seguro_base:
            frente_y = abaixo_do_titulo  # desce o filme para o título não ficar em cima dele, se ainda couber
        if int(e["posicao_video"]) > 0:  # posição escolhida no Estúdio (centro do filme)
            frente_y = int(e["posicao_video"]) - frente_h // 2
            frente_y = max(area_filme.y, min(area_filme.y + area_filme.h - frente_h, frente_y))
        topo_y = max(base_titulo, frente_y - 30)
        barra_no_filme = frente_w < area_filme.w
        if barra_no_filme:
            barra_y = frente_y + frente_h - 10
        else:
            barra_y = min(frente_y + frente_h, area.y + area.h - 10)
        barra_x, barra_w = frente_x, frente_w
        if janela is not None and not barra_no_filme:  # a barra fica na janela, não por baixo da borda
            barra_x = max(frente_x, area.x)
            barra_w = max(2, min(frente_x + frente_w, area.x + area.w) - barra_x)
        abaixo_do_filme = frente_y + frente_h + 26 + meia_legenda
        lugar = str(e.get("legenda_lugar") or "automatico")
        if lugar == "abaixo":  # fora do filme, mesmo passando da área segura (mas dentro da tela/moldura)
            legenda_y = min(abaixo_do_filme, area.y + area.h - meia_legenda - 12)
        elif lugar == "sobre":
            legenda_y = min(legenda_limite, barra_y - 16 - meia_legenda)
        elif abaixo_do_filme <= legenda_limite:
            legenda_y = abaixo_do_filme
        elif legenda_limite - meia_legenda >= barra_y + 10:
            legenda_y = legenda_limite  # cabe entre a barra e o fim da área segura
        else:
            # não cabe embaixo do filme: fica sobre a parte de baixo dele, sem encostar na barra
            legenda_y = min(legenda_limite, barra_y - 16 - meia_legenda)

    # posições escolhidas no Estúdio: sempre com o texto inteiro dentro da tela (ou da moldura)
    if int(e["posicao_topo"]) > 0:
        altura_titulo = int(linhas_topo * int(e["tamanho_topo"]) * 1.4)
        topo_y = max(area.y + altura_titulo, min(area.y + area.h, int(e["posicao_topo"])))
    if int(e["posicao_legenda"]) > 0:
        legenda_y = max(area.y + meia_legenda, min(area.y + area.h - meia_legenda, int(e["posicao_legenda"])))
        if layout == "preencher":  # a barra fica logo abaixo da legenda
            barra_y = min(legenda_y + meia_legenda + 14, area.y + area.h - 10)
    return Geometria(layout, escala_w, escala_h, frente_w, frente_h, frente_y, topo_y, legenda_y, barra_y,
                     frente_x, barra_x, barra_w, barra_no_filme, janela)


def geometria_do_corte(cfg: Config, info: dict, topo: str) -> tuple[Geometria, Path | None, dict | None]:
    """Geometria do vídeo com a moldura configurada (se houver)."""
    caminho, moldura = moldura_atual(cfg)
    janela = moldura["janela"] if moldura else None
    _, titulo_max = larguras_texto(cfg, janela)
    g = calcular_geometria(cfg, info, linhas_do_topo(topo, cfg, titulo_max), janela)
    return g, caminho, moldura


def estilo_legenda(cfg: Config, g: Geometria | None = None) -> EstiloLegenda:
    e = cfg["edicao"]
    largura = int(e["largura"])
    area = g.area if g is not None else None
    legenda_max, titulo_max = larguras_texto(cfg, area)
    fonte_titulo = fonte_do_topo(cfg)[1]
    return EstiloLegenda(
        largura=largura, altura=int(e["altura"]), fonte=str(e["fonte_nome"]), tamanho=int(e["tamanho_legenda"]),
        cor=str(e["cor_legenda"]), destaque=str(e["cor_destaque"]), contorno=int(e["contorno_legenda"]),
        maiusculas=bool(e["maiusculas"]), tamanho_topo=int(e["tamanho_topo"]),
        max_palavras=int(e["palavras_por_bloco"]), max_chars=int(e["max_caracteres_bloco"]),
        largura_max=legenda_max, topo_max=titulo_max,
        cor_topo=str(e["cor_topo"]), fonte_topo="" if fonte_titulo == str(e["fonte_nome"]) else fonte_titulo,
        centro_x=(area.x + area.w // 2) if area is not None else 0,
    )


def amostra_textos(cfg: Config, g: Geometria, topo: str) -> dict:
    """Título e legenda de exemplo com a escala que o libass vai usar (para desenhar no Estúdio)."""
    e = cfg["edicao"]
    estilo = estilo_legenda(cfg, g)
    linhas = [linha.strip() for linha in str(topo).split("\n") if linha.strip()]
    esc_topo = min((escala_para_caber(linha, estilo.tamanho_topo, estilo.fonte_topo or estilo.fonte, 5,
                                      estilo.topo_max) for linha in linhas), default=100)
    exemplo = EXEMPLO_LEGENDA.upper() if e["maiusculas"] else EXEMPLO_LEGENDA
    blocos = blocos_legenda(palavras_estimadas(0.0, 3.0, exemplo), estilo.max_palavras, estilo.max_chars)
    palavras = [p.txt.upper() if estilo.maiusculas else p.txt for p in (blocos[0] if blocos else [])]
    esc_legenda = escala_para_caber(" ".join(palavras), estilo.tamanho, estilo.fonte, estilo.contorno,
                                    estilo.largura_max, piso_escala(estilo.largura_max, estilo.largura)) if palavras else 100
    return {
        "topo": {"linhas": linhas, "escala": esc_topo, "fonte": estilo.fonte_topo or estilo.fonte,
                 "tamanho": estilo.tamanho_topo, "cor": estilo.cor_topo, "largura_max": estilo.topo_max},
        "legenda": {"palavras": palavras, "destaque": min(1, len(palavras) - 1) if palavras else 0,
                    "escala": esc_legenda, "fonte": estilo.fonte, "tamanho": estilo.tamanho, "cor": estilo.cor,
                    "cor_destaque": estilo.destaque, "contorno": estilo.contorno, "largura_max": estilo.largura_max},
        "centro_x": estilo.centro_x or estilo.largura // 2,
    }


def geometria_json(g: Geometria) -> dict:
    dados = asdict(g)
    dados["area"] = asdict(g.area) if g.area is not None else None
    return dados


# ---------------------------------------------------------------- fontes

_TRAVA_FONTES = threading.Lock()


def preparar_fonte(cfg: Config) -> Path | None:
    """Copia as fontes para dados/fontes (o libass varre só essa pasta, é rápido)."""
    e = cfg["edicao"]
    pasta = cfg.pasta_fontes
    copiou = False
    with _TRAVA_FONTES:  # a edição e a prévia do painel podem copiar ao mesmo tempo
        for arquivo in dict.fromkeys([str(e["fonte_arquivo"]), str(e.get("fonte_topo_arquivo") or "")]):
            if not arquivo.strip():
                continue
            origem = Path(arquivo)
            if not origem.exists():
                log.warning("Fonte %s não encontrada; usando a fonte padrão do sistema", origem)
                continue
            destino = pasta / origem.name
            try:
                o = origem.stat()
                d = destino.stat() if destino.exists() else None
                if d is None or d.st_size != o.st_size or int(d.st_mtime) != int(o.st_mtime):
                    pasta.mkdir(parents=True, exist_ok=True)
                    temporario = destino.with_name(destino.name + ".tmp")
                    shutil.copy2(origem, temporario)
                    os.replace(temporario, destino)  # o libass nunca vê a cópia pela metade
            except OSError as erro:
                log.warning("Não consegui copiar a fonte %s: %s", origem.name, erro)
                if not destino.exists():
                    continue
            copiou = True
    return pasta if copiou else None


# ---------------------------------------------------------------- filtros

# codificadores de GPU que já falharam nesta execução (o resto das edições vai direto para o libx264)
_CODECS_FALHOS: set[str] = set()


def _args_video(cfg: Config, fps: Fraction, codec: str | None = None) -> list[str]:
    e = cfg["edicao"]
    codec = codec or str(e["codec"])
    gop = str(max(12, int(round(float(fps) * 2))))
    if codec == "libx264":
        return ["-c:v", "libx264", "-preset", str(e["preset"]), "-crf", str(e["crf"]), "-profile:v", "high",
                "-pix_fmt", "yuv420p", "-maxrate", "12M", "-bufsize", "24M", "-g", gop]
    # encoders de GPU (h264_amf, h264_nvenc, h264_qsv): taxa de bits fixa segura para as redes
    return ["-c:v", codec, "-b:v", "8M", "-maxrate", "12M", "-bufsize", "24M", "-pix_fmt", "yuv420p", "-g", gop]


def _filtro_video(cfg: Config, g: Geometria, origem: str, extra_cor: str, dur: float | None,
                  moldura: dict | None, entrada_moldura: int, barra_fixa: float | None = None) -> tuple[list[str], str]:
    """Filme, fundo, barra de progresso e moldura. Devolve as partes do filtro e o nome da última saída.

    extra_cor: taxa e duração das fontes de cor (no vídeo) ou "" (na prévia, que é uma imagem só).
    barra_fixa: na prévia, fração da barra já preenchida; None = barra que avança com o vídeo.
    """
    e = cfg["edicao"]
    largura, altura = int(e["largura"]), int(e["altura"])
    area = g.area or Area(0, 0, largura, altura)
    partes: list[str] = []
    barra = bool(e["barra_progresso"])
    cor_barra = str(e["cor_barra"]).lstrip("#")

    def fonte_barra(w: int) -> str:
        largura_barra = max(2, int(w * barra_fixa)) if barra_fixa is not None else w
        return f"color=c=0x{cor_barra}:s={largura_barra}x10{extra_cor}"

    def andar(x0: int) -> str:  # a barra entra pela esquerda e cresce até o fim do vídeo
        return f"{x0}" if barra_fixa is not None else f"{x0}-w+w*t/{dur:.3f}"

    fim_barra = "" if barra_fixa is not None else ":eof_action=pass"
    if g.layout in ("desfocado", "preto"):
        if g.layout == "desfocado":
            bw, bh = _par(largura / 4), _par(altura / 4)
            escurecer = float(e["escurecer_fundo"])
            partes += [
                f"{origem},split=2[frente_src][fundo_src]",
                f"[fundo_src]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
                f"boxblur={int(e['desfoque_fundo'])}:2,eq=brightness={-escurecer:.3f}:saturation=1.15,"
                f"scale={largura}:{altura}:flags=bilinear,setsar=1[fundo]",
            ]
        else:
            partes += [f"{origem}[frente_src]", f"color=c=black:s={largura}x{altura}{extra_cor},setsar=1[fundo]"]
        if barra and g.barra_no_filme:
            partes += [
                f"[frente_src]crop={g.frente_w}:{g.frente_h}[frente_base]",
                f"{fonte_barra(g.frente_w)}[barra]",
                f"[frente_base][barra]overlay=x={andar(0)}:y={g.frente_h - 10}{fim_barra}[frente]",
            ]
        else:
            partes.append(f"[frente_src]crop={g.frente_w}:{g.frente_h}[frente]")
        partes.append(f"[fundo][frente]overlay=x={g.frente_x}:y={g.frente_y}[base]")
    elif g.area is None:
        partes.append(f"{origem},crop={largura}:{altura}[base]")
    else:
        partes += [
            f"{origem},crop={g.frente_w}:{g.frente_h}[frente]",
            f"color=c=black:s={largura}x{altura}{extra_cor},setsar=1[fundo]",
            f"[fundo][frente]overlay=x={g.frente_x}:y={g.frente_y}[base]",
        ]
    atual = "base"

    if barra and not g.barra_no_filme:
        if g.area is None:  # igual ao editor anterior: a barra entra pela esquerda da tela
            partes += [
                f"{fonte_barra(g.barra_w)}[barra]",
                f"[{atual}][barra]overlay=x={andar(g.barra_x)}:y={g.barra_y}{fim_barra}[combinado]",
            ]
        else:  # com moldura, a barra corre numa faixa do tamanho dela e nunca aparece fora da janela
            partes += [
                f"[{atual}]split=2[base_a][base_b]",
                f"[base_b]crop={g.barra_w}:10:{g.barra_x}:{g.barra_y}[trilho]",
                f"{fonte_barra(g.barra_w)}[barra]",
                f"[trilho][barra]overlay=x={andar(0)}:y=0{fim_barra}[trilho_barra]",
                f"[base_a][trilho_barra]overlay=x={g.barra_x}:y={g.barra_y}[combinado]",
            ]
        atual = "combinado"

    if moldura is not None:
        partes += [
            f"[{entrada_moldura}:v]{moldura['filtro']}[moldura]",
            # yuv444 no encontro evita borrar a cor nas bordas finas da moldura
            f"[{atual}][moldura]overlay=x=0:y=0:format=yuv444:eof_action=repeat[com_moldura]",
        ]
        atual = "com_moldura"
    return partes, atual


def _filtro_ass(ass: Path, pasta_fontes: Path | None) -> str:
    filtro = f"ass=filename={caminho_filtro(ass)}"
    if pasta_fontes:
        filtro += f":fontsdir={caminho_filtro(pasta_fontes)}"
    return filtro


# ---------------------------------------------------------------- render

def renderizar(
    cfg: Config,
    caminho_filme: Path,
    info: dict,
    frases: list[Frase],
    inicio: float,
    fim: float,
    topo: str,
    destino: Path,
    parar: threading.Event | None = None,
    rotulo: str | None = None,
) -> Path:
    e = cfg["edicao"]
    dur = round(fim - inicio, 3)
    fps = fracao(info["video"]["fps"], Fraction(30)) or Fraction(30)
    # os Reels da Página do Facebook pedem de 24 a 60 quadros por segundo: 23,976 vira 24
    fps = min(max(fps, Fraction(24)), Fraction(60))
    fps_txt = f"{fps.numerator}/{fps.denominator}"
    g, arquivo_moldura, moldura = geometria_do_corte(cfg, info, topo)
    cw, ch, cx, cy = info["crop"]
    destino.parent.mkdir(parents=True, exist_ok=True)

    # --- legendas e texto do topo (ASS)
    ass = destino.with_suffix(".ass")
    palavras = palavras_no_intervalo(frases, inicio, fim) if e["legendas"] else []
    estilo = estilo_legenda(cfg, g)
    ass.write_text(gerar_ass(estilo, palavras, dur, g.legenda_y, topo, g.topo_y), encoding="utf-8")
    pasta_fontes = preparar_fonte(cfg)

    # --- vídeo
    origem = (
        f"[0:{info['video']['indice']}]crop={cw}:{ch}:{cx}:{cy},"
        f"scale={g.escala_w}:{g.escala_h}:flags=bicubic,setsar=1,fps={fps_txt}"
    )
    partes, atual = _filtro_video(cfg, g, origem, f":r={fps_txt}:d={dur:.3f}", dur, moldura, 1)
    fade = f",fade=t=in:st=0:d=0.25,fade=t=out:st={max(0.0, dur - 0.45):.3f}:d=0.45" if e["fade"] else ""
    partes.append(f"[{atual}]{_filtro_ass(ass, pasta_fontes)}{fade},format=yuv420p[vout]")

    # --- áudio
    if info.get("audio"):
        a = f"[0:{info['audio']['indice']}]aresample=48000,aformat=channel_layouts=stereo"
        if e["normalizar_audio"]:
            a += ",loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000"
        if e["fade"]:
            a += f",afade=t=in:st=0:d=0.2,afade=t=out:st={max(0.0, dur - 0.45):.3f}:d=0.45"
    else:
        a = f"anullsrc=r=48000:cl=stereo,atrim=duration={dur:.3f}"
    partes.append(a + "[aout]")

    parcial = destino.with_name(destino.stem + ".parcial.mp4")
    entrada_moldura = ["-i", str(arquivo_moldura)] if moldura is not None else []

    def comando(args_video: list[str]) -> list[str]:
        return base_ffmpeg(cfg["ferramentas"]["ffmpeg"]) + [
            "-loglevel", "error", "-progress", "pipe:1", "-nostats",
            "-ss", f"{inicio:.3f}", "-t", f"{dur:.3f}", "-i", str(caminho_filme),
            *entrada_moldura,
            "-filter_complex", ";".join(partes),
            "-map", "[vout]", "-map", "[aout]",
            *args_video,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-t", f"{dur:.3f}", "-movflags", "+faststart",
            str(parcial),
        ]

    codec = str(e["codec"])
    usar_gpu = codec != "libx264" and codec not in _CODECS_FALHOS
    rotulo = rotulo or f"Renderizando {destino.name}"
    prog = Progresso(rotulo, dur, intervalo_seg=20)
    r = executar(comando(_args_video(cfg, fps, None if usar_gpu else "libx264")), parar=parar,
                 ao_ler_saida=prog.linha_ffmpeg, baixa_prioridade=cfg["geral"]["prioridade_baixa"])
    if r.codigo != 0 and usar_gpu:
        # placa/driver sem suporte ao codificador escolhido: tenta o libx264 (CPU)
        log.warning("O codificador %s falhou (%s). Tentando com o libx264.", codec, r.resumo_erro(2))
        prog = Progresso(rotulo, dur, intervalo_seg=20)
        r = executar(comando(_args_video(cfg, fps, "libx264")), parar=parar,
                     ao_ler_saida=prog.linha_ffmpeg, baixa_prioridade=cfg["geral"]["prioridade_baixa"])
        if r.codigo == 0:  # o problema era mesmo o codificador da placa: as próximas edições vão direto na CPU
            _CODECS_FALHOS.add(codec)
            log.warning("Usando o libx264 daqui em diante (o %s não funcionou nesta placa ou driver).", codec)
    if r.codigo != 0:
        parcial.unlink(missing_ok=True)
        raise ErroMidia(f"falha na renderização: {r.resumo_erro(6)}")

    obtida = duracao_arquivo(cfg["ferramentas"]["ffprobe"], parcial)
    if obtida < dur * 0.9:
        parcial.unlink(missing_ok=True)
        raise ErroMidia(f"vídeo renderizado ficou curto ({obtida:.1f}s de {dur:.1f}s)")
    parcial.replace(destino)
    gerar_miniatura(cfg, destino, min(dur / 3, 3.0))
    return destino


def gerar_miniatura(cfg: Config, video: Path, segundo: float = 2.0) -> Path | None:
    """JPEG pequeno ao lado do vídeo, usado nos cards do painel."""
    destino = video.with_suffix(".jpg")
    r = executar(
        base_ffmpeg(cfg["ferramentas"]["ffmpeg"])
        + ["-loglevel", "error", "-ss", f"{max(0.0, segundo):.2f}", "-i", str(video), "-frames:v", "1",
           "-vf", "scale=360:-2", "-q:v", "4", "-update", "1", str(destino)],
        baixa_prioridade=cfg["geral"]["prioridade_baixa"],
        timeout=120,
    )
    if r.codigo != 0 or not destino.exists():
        log.debug("Miniatura não gerada para %s: %s", video.name, r.resumo_erro(2))
        return None
    return destino


# ---------------------------------------------------------------- prévia

# quadros de exemplo (a prévia e o Estúdio pedem ao mesmo tempo): gerados um por vez, num arquivo temporário
TRAVA_QUADROS = threading.Lock()
_CACHE_QUADROS: dict[tuple, dict] = {}


def quadro_exemplo(cfg: Config, destino: Path) -> Path:
    """Imagem de teste para a prévia quando ainda não há filme analisado."""
    quadro = destino.with_name("quadro_exemplo.png")
    with TRAVA_QUADROS:
        if not quadro.exists():
            quadro.parent.mkdir(parents=True, exist_ok=True)
            temporario = quadro.with_name("quadro_exemplo.parcial.png")
            r = executar(
                base_ffmpeg(cfg["ferramentas"]["ffmpeg"]) + ["-loglevel", "error", "-f", "lavfi", "-i",
                                                              "testsrc2=s=1920x800:d=1", "-frames:v", "1", "-update",
                                                              "1", str(temporario)],
                baixa_prioridade=False, timeout=60,
            )
            if r.codigo != 0 or not temporario.exists():
                temporario.unlink(missing_ok=True)
                raise ErroMidia(f"não foi possível gerar o quadro de exemplo: {r.resumo_erro(3)}")
            os.replace(temporario, quadro)
    return quadro


def info_do_quadro(cfg: Config, quadro: Path) -> dict:
    """Info mínima (recorte e proporção) de uma imagem usada na prévia."""
    st = quadro.stat()
    chave = (str(quadro), st.st_mtime_ns, st.st_size)
    dados = _CACHE_QUADROS.get(chave)
    if dados is None:
        sonda = sondar(cfg["ferramentas"]["ffprobe"], quadro)
        video = next((s for s in sonda.get("streams", []) if s.get("codec_type") == "video"), {})
        qw, qh = int(video.get("width") or 1920), int(video.get("height") or 800)
        dados = {"crop": [qw - qw % 2, qh - qh % 2, 0, 0], "video": {"sar": [1, 1]}}
        if len(_CACHE_QUADROS) > 32:
            _CACHE_QUADROS.clear()
        _CACHE_QUADROS[chave] = dados
    return {"crop": list(dados["crop"]), "video": {"sar": list(dados["video"]["sar"])}}


def topo_de_exemplo(cfg: Config) -> str:
    return str(cfg["edicao"]["texto_topo"]).replace("{filme}", "Nome do Filme").replace("{parte}", "1").replace(
        "{ano}", "2024")


def previa_estilo(cfg: Config, destino: Path, quadro: Path | None = None, topo: str | None = None) -> Path:
    """Imagem de exemplo do layout, moldura, legenda e barra com as configurações atuais (para o painel)."""
    e = cfg["edicao"]
    destino.parent.mkdir(parents=True, exist_ok=True)
    if quadro is None or not quadro.exists():
        quadro = quadro_exemplo(cfg, destino)
    info = info_do_quadro(cfg, quadro)
    topo = topo_de_exemplo(cfg) if topo is None else topo
    g, arquivo_moldura, moldura = geometria_do_corte(cfg, info, topo)

    exemplo = EXEMPLO_LEGENDA.upper() if e["maiusculas"] else EXEMPLO_LEGENDA
    palavras = palavras_estimadas(0.0, 3.0, exemplo)[:5]
    estilo = estilo_legenda(cfg, g)
    ass = destino.with_suffix(".ass")
    ass.write_text(gerar_ass(estilo, palavras if e["legendas"] else [], 3.0, g.legenda_y, topo, g.topo_y),
                   encoding="utf-8")
    # o quadro sai com a mesma palavra destacada que o Estúdio mostra (a 2ª do primeiro bloco)
    primeiro = (blocos_legenda(palavras, estilo.max_palavras, estilo.max_chars) or [[]])[0]
    if len(primeiro) >= 2:
        momento = (primeiro[1].ini + (primeiro[2].ini if len(primeiro) > 2 else primeiro[1].fim)) / 2
    elif primeiro:
        momento = max(0.15, (primeiro[0].ini + primeiro[0].fim) / 2)
    else:
        momento = 0.9

    cw, ch = info["crop"][0], info["crop"][1]
    origem = f"[0:v]crop={cw}:{ch}:0:0,scale={g.escala_w}:{g.escala_h},setsar=1"
    partes, atual = _filtro_video(cfg, g, origem, "", None, moldura, 1, barra_fixa=0.35)
    partes.append(f"[{atual}]{_filtro_ass(ass, preparar_fonte(cfg))},scale=720:-2[saida]")
    entrada_moldura = ["-i", str(arquivo_moldura)] if moldura is not None else []
    r = executar(
        base_ffmpeg(cfg["ferramentas"]["ffmpeg"])
        + ["-loglevel", "error", "-loop", "1", "-i", str(quadro), *entrada_moldura,
           "-filter_complex", ";".join(partes), "-map", "[saida]", "-frames:v", "1", "-ss", f"{momento:.2f}",
           "-q:v", "3", "-update", "1", str(destino)],
        baixa_prioridade=False, timeout=120,
    )
    ass.unlink(missing_ok=True)
    if r.codigo != 0:
        raise ErroMidia(f"falha ao gerar a prévia: {r.resumo_erro(4)}")
    return destino
