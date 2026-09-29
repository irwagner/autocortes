"""Legendas: leitura de SRT/Whisper, ajuste de tempo pela fala e geração de ASS animado."""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from .util import ler_json, log, salvar_json, sem_acentos


@dataclass
class Palavra:
    ini: float
    fim: float
    txt: str


@dataclass
class Frase:
    ini: float
    fim: float
    txt: str
    palavras: list[Palavra]


# ---------------------------------------------------------------- limpeza

_TAG_HTML = re.compile(r"<[^>]+>")
_TAG_ASS = re.compile(r"\{[^}]*\}")
_ANOTACAO = re.compile(r"\[[^\]]*\]|\([^)]*\)|[♪♫♬#*]+")
_SO_ANOTACAO = re.compile(r"^[\[(*♪♫].*[\])*♪♫]$|^[♪♫*#]+$")
_ALUCINACAO = re.compile(
    r"amara\.org|legendas? (por|pela)|legendado por|inscreva-se|obrigad[oa] por assistir|"
    r"subtitles? by|thanks for watching|www\.|https?://",
    re.I,
)
_FIM_FRASE = re.compile(r"[.!?…]['\"”»]?$")


def limpar_texto(texto: str) -> str:
    texto = _TAG_HTML.sub("", texto)
    texto = _TAG_ASS.sub("", texto)
    texto = texto.replace("\\N", " ").replace("\\n", " ")
    linhas = []
    for linha in texto.splitlines():
        linha = _ANOTACAO.sub(" ", linha)
        linha = re.sub(r"^\s*[-–—]+\s*", "", linha)  # travessão de diálogo
        if linha.strip():
            linhas.append(linha.strip())
    return re.sub(r"\s+", " ", " ".join(linhas)).strip()


def _ler_texto(caminho: Path) -> str:
    bruto = caminho.read_bytes()
    try:
        return bruto.decode("utf-8-sig")
    except UnicodeDecodeError:
        return bruto.decode("cp1252", errors="replace")  # legendas brasileiras antigas


def palavras_estimadas(ini: float, fim: float, texto: str) -> list[Palavra]:
    """Distribui o tempo de uma frase entre as palavras, proporcional ao tamanho."""
    tokens = texto.split()
    if not tokens:
        return []
    pesos = [len(t) + 1 for t in tokens]
    total = sum(pesos)
    duracao = max(0.1, fim - ini)
    palavras, t = [], ini
    for tok, peso in zip(tokens, pesos):
        d = duracao * peso / total
        palavras.append(Palavra(round(t, 3), round(t + d, 3), tok))
        t += d
    return palavras


# ---------------------------------------------------------------- SRT

_TEMPO_SRT = re.compile(
    r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
)


def _seg(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def ler_srt(caminho: Path) -> list[Frase]:
    texto = _ler_texto(caminho).replace("\r\n", "\n")
    frases: list[Frase] = []
    for bloco in re.split(r"\n\s*\n", texto):
        linhas = bloco.strip().split("\n")
        for i, linha in enumerate(linhas):
            m = _TEMPO_SRT.search(linha)
            if not m:
                continue
            ini, fim = _seg(*m.groups()[:4]), _seg(*m.groups()[4:])
            conteudo = limpar_texto("\n".join(linhas[i + 1 :]))
            if conteudo and fim > ini and not _ALUCINACAO.search(conteudo):
                frases.append(Frase(ini, fim, conteudo, palavras_estimadas(ini, fim, conteudo)))
            break
    frases.sort(key=lambda f: f.ini)
    return frases


# ---------------------------------------------------------------- Whisper

def ler_whisper_json(caminho: Path) -> list[Palavra]:
    dados = json.loads(caminho.read_bytes().decode("utf-8", errors="replace"))
    palavras = []
    for item in dados.get("transcription", []):
        txt = (item.get("text") or "").strip()
        offsets = item.get("offsets") or {}
        ini = float(offsets.get("from", 0)) / 1000
        fim = float(offsets.get("to", 0)) / 1000
        if not txt or _SO_ANOTACAO.match(txt):
            continue
        palavras.append(Palavra(ini, max(fim, ini + 0.05), txt))
    return palavras


def ler_whisper_srt(caminho: Path) -> list[Palavra]:
    palavras = []
    for frase in ler_srt(caminho):
        palavras.extend(palavras_estimadas(frase.ini, frase.fim, frase.txt))
    return palavras


def ajustar_a_fala(palavras: list[Palavra], falas: list[tuple[float, float]], folga: float = 0.15) -> list[Palavra]:
    """Encaixa cada palavra dentro do trecho de fala detectado pelo VAD.

    O whisper.cpp costuma "esticar" a primeira palavra depois de uma pausa até o
    fim da fala anterior (e a última até a próxima). Aqui cada palavra fica só com
    o maior pedaço que cai dentro de um trecho de fala real.
    """
    if not falas:
        return palavras
    inicios = [a for a, _ in falas]
    ajustadas: list[Palavra] = []
    for p in palavras:
        pedacos = []
        i = bisect_right(inicios, p.fim + folga) - 1
        while i >= 0 and falas[i][1] + folga > p.ini:
            a, b = falas[i]
            ini, fim = max(p.ini, a - folga), min(p.fim, b + folga)
            if fim > ini:
                pedacos.append((ini, fim))
            i -= 1
        if not pedacos:
            continue  # palavra fora de qualquer fala: quase sempre alucinação
        ini, fim = max(pedacos, key=lambda x: x[1] - x[0])
        ajustadas.append(Palavra(round(ini, 3), round(max(fim, ini + 0.08), 3), p.txt))
    ajustadas.sort(key=lambda w: w.ini)
    for anterior, atual in zip(ajustadas, ajustadas[1:]):
        if atual.ini < anterior.fim:
            anterior.fim = max(anterior.ini + 0.05, atual.ini)
    return ajustadas


def agrupar_frases(palavras: list[Palavra], pausa: float = 0.7, max_palavras: int = 16) -> list[Frase]:
    frases: list[Frase] = []
    atual: list[Palavra] = []

    def fechar() -> None:
        if atual:
            texto = " ".join(p.txt for p in atual)
            if not _ALUCINACAO.search(texto):
                frases.append(Frase(atual[0].ini, atual[-1].fim, texto, list(atual)))
            atual.clear()

    for p in palavras:
        if atual and (p.ini - atual[-1].fim > pausa or len(atual) >= max_palavras):
            fechar()
        # repetições em loop ("e e e e") são alucinação típica
        if len(atual) >= 3 and all(x.txt.lower() == p.txt.lower() for x in atual[-3:]):
            continue
        atual.append(p)
        if _FIM_FRASE.search(p.txt):
            fechar()
    fechar()
    return frases


# ---------------------------------------------------------------- persistência

def salvar_transcricao(caminho: Path, frases: list[Frase], fonte: str, idioma: str | None) -> None:
    salvar_json(
        caminho,
        {
            "fonte": fonte,
            "idioma": idioma,
            "frases": [
                {"ini": f.ini, "fim": f.fim, "txt": f.txt, "p": [[p.ini, p.fim, p.txt] for p in f.palavras]}
                for f in frases
            ],
        },
    )


def carregar_transcricao(caminho: Path) -> tuple[list[Frase], dict]:
    dados = ler_json(caminho, None)
    if not dados:
        return [], {}
    frases = [
        Frase(f["ini"], f["fim"], f["txt"], [Palavra(a, b, t) for a, b, t in f.get("p", [])])
        for f in dados.get("frases", [])
    ]
    return frases, {"fonte": dados.get("fonte"), "idioma": dados.get("idioma")}


def palavras_no_intervalo(frases: list[Frase], ini: float, fim: float) -> list[Palavra]:
    """Palavras dentro do corte, com o tempo relativo ao início do corte."""
    saida = []
    for frase in frases:
        if frase.fim < ini or frase.ini > fim:
            continue
        for p in frase.palavras:
            centro = (p.ini + p.fim) / 2
            if ini <= centro <= fim:
                saida.append(Palavra(max(0.0, p.ini - ini), min(fim - ini, p.fim - ini), p.txt))
    return saida


# ---------------------------------------------------------------- ASS

def cor_ass(rgb: str, alfa: int = 0) -> str:
    """'FFD400' -> '&H0000D4FF' (ASS usa AABBGGRR)."""
    h = rgb.strip().lstrip("#").upper()
    return f"&H{alfa:02X}{h[4:6]}{h[2:4]}{h[0:2]}"


def _cor_inline(rgb: str) -> str:
    h = rgb.strip().lstrip("#").upper()
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&"


def _tempo_ass(seg: float) -> str:
    cs = int(round(max(0.0, seg) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escapar_ass(texto: str) -> str:
    return texto.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


# Palavras que não devem fechar um bloco da legenda: artigos, preposições, conjunções,
# pronomes átonos, possessivos e o "não" puxam a palavra seguinte (regra do guia de
# legendas pt-BR da Netflix). Inclui as equivalentes em inglês para filmes legendados.
PALAVRAS_LIGACAO = frozenset("""
o a os as um uma uns umas de do da dos das dum duma em no na nos nas num numa por pelo pela
pelos pelas para pra pro pros pras com sem sob sobre ate ao aos e ou mas nem que se porque
quando como onde me te lhe lhes vos nao meu minha meus minhas teu tua teus tuas seu sua seus
suas nosso nossa nossos nossas
the an of to in on at for from with and or but my your his her our their its i
""".split())


def _chave(txt: str) -> str:
    return re.sub(r"[^\w]", "", sem_acentos(txt).lower())


def blocos_legenda(palavras: list[Palavra], max_palavras: int, max_chars: int, pausa: float = 0.6) -> list[list[Palavra]]:
    blocos: list[list[Palavra]] = []
    atual: list[Palavra] = []
    for p in palavras:
        if atual:
            tamanho = sum(len(x.txt) + 1 for x in atual) + len(p.txt)
            natural = p.ini - atual[-1].fim > pausa or re.search(r"[.!?…,;:]['\"”»]?$", atual[-1].txt)
            cheio = len(atual) >= max_palavras or tamanho > max_chars
            if natural:
                blocos.append(atual)
                atual = []
            elif cheio:
                # palavras de ligação no fim descem para o próximo bloco, junto da palavra que introduzem
                cauda: list[Palavra] = []
                while len(atual) > 1 and len(cauda) < max_palavras - 1 and _chave(atual[-1].txt) in PALAVRAS_LIGACAO:
                    cauda.insert(0, atual.pop())
                blocos.append(atual)
                atual = cauda
        atual.append(p)
    if atual:
        blocos.append(atual)
    return blocos


# Largura média dos caracteres da Arial Black (em "em"), um pouco folgada para não estourar.
_FATOR_FONTE = {
    "arial black": 1.0, "impact": 0.66, "segoe ui black": 0.9, "franklin gothic heavy": 0.84,
    "cooper black": 0.95, "berlin sans fb demi": 0.82, "rockwell extra bold": 0.98,
}


def largura_texto(texto: str, tamanho: float, fonte: str = "Arial Black") -> float:
    """Largura aproximada do texto em pixels, sem precisar abrir o arquivo da fonte."""
    total = 0.0
    for c in texto:
        base = sem_acentos(c) or c
        if c == " ":
            w = 0.33
        elif base in ("M", "W"):
            w = 0.97
        elif base in ("m", "w"):
            w = 0.98
        elif base == "I":
            w = 0.42
        elif base in ("i", "j", "l", "f", "t"):
            w = 0.40
        elif base.isupper():
            w = 0.80
        elif base.islower():
            w = 0.67
        elif base.isdigit():
            w = 0.67
        elif c in ".,;:!'\"-":
            w = 0.35
        else:
            w = 0.62
        total += w
    return total * float(tamanho) * _FATOR_FONTE.get(fonte.strip().lower(), 1.0)


def escala_para_caber(texto: str, tamanho: float, fonte: str, contorno: int, maximo: int, minimo: int = 72) -> int:
    """Escala em % (\\fscx/\\fscy) para o texto caber em 'maximo' pixels sem quebrar a linha."""
    largura = largura_texto(texto, tamanho, fonte) + 2 * contorno
    if largura <= maximo:
        return 100
    return max(minimo, int(100 * maximo / largura))


def piso_escala(maximo: int, largura_tela: int) -> int:
    """Menor escala da legenda: 72%, ou menos numa moldura estreita (assim o bloco não quebra em duas linhas)."""
    padrao = int(largura_tela * 0.644)
    if padrao <= 0 or maximo >= padrao:
        return 72
    return max(45, int(72 * maximo / padrao))


@dataclass
class EstiloLegenda:
    largura: int
    altura: int
    fonte: str
    tamanho: int
    cor: str
    destaque: str
    contorno: int
    maiusculas: bool
    tamanho_topo: int
    max_palavras: int
    max_chars: int
    # largura máxima dos textos em pixels (0 = automático, dentro da área segura dos apps)
    largura_max: int = 0
    topo_max: int = 0
    cor_topo: str = "FFFFFF"
    fonte_topo: str = ""  # "" = a mesma da legenda
    centro_x: int = 0     # 0 = meio da tela (com moldura, o meio da área do vídeo)


def gerar_ass(
    estilo: EstiloLegenda,
    palavras: list[Palavra],
    duracao: float,
    legenda_y: int,
    topo: str,
    topo_y: int,
) -> str:
    e = estilo
    cor_topo = cor_ass(e.cor_topo or "FFFFFF")
    fonte_topo = e.fonte_topo or e.fonte
    # textos centrados e mais estreitos que a tela, longe dos botões laterais dos apps;
    # as margens também limitam a quebra de linha quando algo não cabe nem reduzido
    lmax = e.largura_max or int(e.largura * 0.644)
    tmax = e.topo_max or int(e.largura * 0.644)
    margem_legenda = max(0, (e.largura - lmax) // 2)
    margem_topo = max(0, (e.largura - tmax) // 2)
    cabecalho = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {e.largura}",
        f"PlayResY: {e.altura}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Legenda,{e.fonte},{e.tamanho},{cor_ass(e.cor)},{cor_ass(e.cor)},&H00000000,&H64000000,"
        f"0,0,0,0,100,100,0,0,1,{e.contorno},3,5,{margem_legenda},{margem_legenda},0,1",
        f"Style: Topo,{fonte_topo},{e.tamanho_topo},{cor_topo},{cor_topo},&H00000000,&H64000000,"
        f"0,0,0,0,100,100,0,0,1,5,2,2,{margem_topo},{margem_topo},0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    eventos: list[str] = []
    centro_x = e.centro_x or e.largura // 2

    if topo.strip():
        linhas = [item.strip() for item in topo.split("\n") if item.strip()]
        # a mesma escala em todas as linhas (a da mais larga); se nem assim couber, o libass quebra a linha
        esc = min(escala_para_caber(linha, e.tamanho_topo, fonte_topo, 5, tmax) for linha in linhas)
        eventos.append(
            f"Dialogue: 1,{_tempo_ass(0)},{_tempo_ass(duracao)},Topo,,0,0,0,,"
            f"{{\\pos({centro_x},{topo_y})\\fscx{esc}\\fscy{esc}}}" + "\\N".join(_escapar_ass(x) for x in linhas)
        )

    def mostrar(p: Palavra) -> str:
        txt = p.txt.upper() if e.maiusculas else p.txt
        txt = re.sub(r"[.,;:]+$", "", txt) if not txt.endswith("...") else txt.rstrip(".") + "..."
        return _escapar_ass(txt)

    blocos = blocos_legenda(palavras, e.max_palavras, e.max_chars)
    cor_normal = _cor_inline(e.cor)
    cor_destaque = _cor_inline(e.destaque)
    for n, bloco in enumerate(blocos):
        proximo_ini = blocos[n + 1][0].ini if n + 1 < len(blocos) else duracao
        fim_bloco = min(bloco[-1].fim + 0.3, proximo_ini, duracao)
        textos = [mostrar(p) for p in bloco]
        # reduz o bloco inteiro se ele não couber na largura segura (evita quebrar em 2 linhas)
        esc = escala_para_caber(" ".join(textos), e.tamanho, e.fonte, e.contorno, lmax, piso_escala(lmax, e.largura))
        for k, p in enumerate(bloco):
            ini = p.ini
            fim = bloco[k + 1].ini if k + 1 < len(bloco) else fim_bloco
            if fim - ini < 0.04:
                continue
            partes = []
            for j, t in enumerate(textos):
                partes.append(f"{{\\c{cor_destaque}}}{t}{{\\c{cor_normal}}}" if j == k else t)
            if k == 0:  # efeito "pop" quando o bloco aparece
                pop = int(esc * 0.82)
                animacao = f"\\fscx{pop}\\fscy{pop}\\t(0,90,\\fscx{esc}\\fscy{esc})"
            else:
                animacao = f"\\fscx{esc}\\fscy{esc}"
            eventos.append(
                f"Dialogue: 0,{_tempo_ass(ini)},{_tempo_ass(fim)},Legenda,,0,0,0,,"
                f"{{\\pos({centro_x},{legenda_y}){animacao}}}" + " ".join(partes)
            )
    if not palavras:
        log.debug("Corte sem falas: legenda vazia")
    return "\n".join(cabecalho + eventos) + "\n"
