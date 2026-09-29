"""Escolha dos melhores trechos de um filme.

Cada janela candidata recebe uma nota a partir de sinais baratos de calcular
(volume, picos, fala, ritmo de cortes, gancho inicial, texto) e começa/termina
em pontos naturais: trocas de cena, início/fim de fala ou pausas.
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from .config import Config
from .legendas import Frase
from .textos import normalizar

PASSO = 0.5


@dataclass
class Candidato:
    inicio: float
    fim: float
    pontuacao: float
    detalhes: dict = field(default_factory=dict)
    frase: str = ""


def _percentil(valores: list[float], p: float) -> float:
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    k = (len(ordenados) - 1) * p
    baixo, alto = math.floor(k), math.ceil(k)
    return ordenados[baixo] + (ordenados[alto] - ordenados[baixo]) * (k - baixo)


def _acumulado(valores: list[float]) -> list[float]:
    soma = [0.0]
    for v in valores:
        soma.append(soma[-1] + v)
    return soma


def _mesclar(intervalos: list[tuple[float, float]], folga: float = 0.3) -> list[tuple[float, float]]:
    saida: list[list[float]] = []
    for a, b in sorted(intervalos):
        if saida and a <= saida[-1][1] + folga:
            saida[-1][1] = max(saida[-1][1], b)
        else:
            saida.append([a, b])
    return [(a, b) for a, b in saida]


# Palavras que, logo no começo de um corte, indicam que ele pega a ideia pela metade.
CONECTIVOS_ABERTURA = frozenset(
    "e mas entao ai dai porque pois tambem ne tipo enfim ou nem alias and but so because then or".split()
)
_FIM_DE_FRASE = re.compile(r"[.!?…]['\"”»]?$")


def sinal_abertura(palavras: list, inicios: list[float], s: float) -> float:
    """Qualidade do começo do corte: +0,5 forte, 0 neutro, -1 fraco.

    Fraco: a primeira fala começa com conectivo ("então", "mas", "aí") ou continua uma
    frase que começou antes do corte. Forte: pergunta ou exclamação nos primeiros segundos
    (vale menos, porque o sinal "texto" já premia ! e ?). Corte que abre sem fala
    (ação, música) fica neutro.
    """
    i = bisect_left(inicios, s - 0.05)
    if i >= len(palavras) or palavras[i].ini > s + 3.0:
        return 0.0
    primeira = palavras[i].txt.strip()
    if re.sub(r"[^\w]", "", normalizar(primeira)) in CONECTIVOS_ABERTURA:
        return -1.0
    anterior = palavras[i - 1] if i > 0 else None
    if (anterior is not None and s - anterior.fim < 1.2 and primeira[:1].islower()
            and not _FIM_DE_FRASE.search(anterior.txt.strip())):
        return -1.0
    for p in palavras[i:i + 12]:
        if p.ini > s + 4.0:
            break
        if re.search(r"[!?]['\"”»]?$", p.txt.strip()):
            return 0.5
    return 0.0


def melhor_frase(frases: list[Frase], ini: float, fim: float) -> str:
    dentro = [f for f in frases if f.ini >= ini - 0.2 and f.fim <= fim + 0.2 and len(f.txt) >= 8]
    if not dentro:
        return ""

    def nota(f: Frase) -> float:
        n = 0.0
        if "?" in f.txt or "!" in f.txt:
            n += 2
        tamanho = len(f.txt)
        if 20 <= tamanho <= 90:
            n += 2
        elif tamanho <= 130:
            n += 1
        return n + min(len(f.txt.split()), 12) / 12

    return max(dentro, key=nota).txt


def gerar_candidatos(
    cfg: Config,
    duracao: float,
    cenas: list[float],
    volume: list[float],
    falas: list[list[float]],
    frases: list[Frase],
    ocupados: list[tuple[float, float]] | None = None,
) -> list[Candidato]:
    """Melhores trechos sem sobreposição. 'ocupados' são trechos já usados em cortes anteriores."""
    c = cfg["cortes"]
    pesos = c["pesos"]
    dmin, dmax, alvo = float(c["duracao_min"]), float(c["duracao_max"]), float(c["duracao_alvo"])
    if duracao < dmin + 2:
        return []

    ignorar_ini = min(float(cfg["analise"]["ignorar_inicio_seg"]), duracao * 0.05)
    ignorar_fim = min(float(cfg["analise"]["ignorar_fim_seg"]), duracao * 0.10)
    ini_ok, fim_ok = ignorar_ini, duracao - ignorar_fim
    if fim_ok - ini_ok < dmin:
        ini_ok, fim_ok = 0.0, duracao

    n = int(duracao / PASSO) + 1
    db = (list(volume) + [-90.0] * n)[:n] if volume else [-60.0] * n
    regiao = db[int(ini_ok / PASSO): int(fim_ok / PASSO)] or db
    p10, p80, p90 = _percentil(regiao, 0.10), _percentil(regiao, 0.80), _percentil(regiao, 0.90)
    escala = max(1e-6, p90 - p10)
    tem_volume = bool(volume) and p90 - p10 > 1.0
    norm = [min(1.0, max(0.0, (v - p10) / escala)) for v in db] if tem_volume else [0.0] * n
    alto = [1.0 if v >= p80 else 0.0 for v in db] if tem_volume else [0.0] * n
    limite_silencio = max(p10, -60.0) + 1.5
    silencio = [1.0 if v <= limite_silencio else 0.0 for v in db] if tem_volume else [0.0] * n

    # fala: VAD quando existe, senão as próprias legendas
    base_fala = [(a, b) for a, b in falas] if falas else [(f.ini, f.fim) for f in frases]
    fala_intervalos = _mesclar(base_fala)
    fala = [0.0] * n
    for a, b in fala_intervalos:
        for i in range(max(0, int(a / PASSO)), min(n, int(b / PASSO) + 1)):
            quadro_ini, quadro_fim = i * PASSO, (i + 1) * PASSO
            fala[i] = min(1.0, fala[i] + max(0.0, min(b, quadro_fim) - max(a, quadro_ini)) / PASSO)

    # texto: ! ? e palavras-chave
    chaves = [normalizar(str(p)) for p in c["palavras_chave"] if str(p).strip()]
    texto = [0.0] * n
    for f in frases:
        i = min(n - 1, max(0, int(f.ini / PASSO)))
        pontos = 0.0
        if "!" in f.txt or "?" in f.txt:
            pontos += 1.0
        if chaves:
            alvo_txt = normalizar(f.txt)
            pontos += sum(1.0 for k in chaves if re.search(rf"\b{re.escape(k)}\b", alvo_txt))
        texto[i] += pontos

    s_norm, s_alto, s_sil, s_fala, s_txt = map(_acumulado, (norm, alto, silencio, fala, texto))
    cenas = sorted(cenas)
    cenas_uteis = [t for t in cenas if ini_ok <= t <= fim_ok]
    taxa_media = len(cenas_uteis) / max(1e-6, (fim_ok - ini_ok) / 60)

    # ---- pontos de corte: trocas de cena, respiros entre falas e uma grade de apoio
    inicios_fala = [a for a, _ in fala_intervalos]

    def colide_com_fala(t: float, antes: float, depois: float) -> bool:
        """True se t cai entre (início da fala + antes) e (fim da fala + depois)."""
        i = bisect_right(inicios_fala, t - antes) - 1
        for k in (i, i - 1):
            if 0 <= k < len(fala_intervalos):
                a, b = fala_intervalos[k]
                if a + antes < t < b + depois:
                    return True
        return False

    def cena_entre(a: float, b: float) -> float | None:
        k = bisect_left(cenas, a)
        return cenas[k] if k < len(cenas) and cenas[k] <= b else None

    tipo_ini: dict[float, str] = {}
    tipo_fim: dict[float, str] = {}
    for t in cenas:
        tipo_ini[round(t + 0.04, 2)] = "cena"
        tipo_fim[round(t - 0.08, 2)] = "cena"
    for a, b in fala_intervalos:
        # entra um pouco antes da fala; se houver troca de cena nesse respiro, entra na troca
        cena = cena_entre(a - 0.29, a + 0.01)
        if cena is None:
            tipo_ini.setdefault(round(max(0.0, a - 0.25), 2), "fala")
        cena = cena_entre(b + 0.03, b + 0.43)
        if cena is None:
            tipo_fim.setdefault(round(b + 0.35, 2), "fala")
    t = ini_ok
    while t < fim_ok:
        tipo_ini.setdefault(round(t, 2), "grade")
        tipo_fim.setdefault(round(t, 2), "grade")
        t += 2.5

    def inicio_valido(t: float) -> bool:
        if tipo_ini[t] == "cena":  # na troca de cena a fala pode começar logo em seguida
            return not colide_com_fala(t, 0.05, 0.1)
        return not colide_com_fala(t, -0.2, 0.1)

    def fim_valido(t: float) -> bool:
        if tipo_fim[t] == "cena":
            return not colide_com_fala(t, -0.1, -0.05)
        return not colide_com_fala(t, -0.1, 0.25)

    inicios = sorted(x for x in tipo_ini if ini_ok <= x <= fim_ok - dmin and inicio_valido(x))
    fins = sorted(x for x in tipo_fim if ini_ok + dmin <= x <= fim_ok and fim_valido(x))
    if not inicios or not fins:
        return []

    def media(soma: list[float], a: int, b: int) -> float:
        return (soma[b] - soma[a]) / max(1, b - a)

    todas_palavras = sorted((p for f in frases for p in f.palavras), key=lambda p: p.ini)
    inicios_palavras = [p.ini for p in todas_palavras]

    def pontuar(s: float, e: float, abertura: float = 0.0) -> tuple[float, dict]:
        a, b = int(s / PASSO), max(int(s / PASSO) + 1, int(e / PASSO))
        g = min(b, a + int(3 / PASSO))
        n_cenas = bisect_left(cenas, e) - bisect_left(cenas, s)
        taxa = n_cenas / max(1e-6, (e - s) / 60)
        d = {
            "volume": media(s_norm, a, b),
            "picos": media(s_alto, a, b),
            "fala": media(s_fala, a, b),
            "cenas": min(1.0, taxa / max(1e-6, 2 * taxa_media)) if taxa_media > 0 else 0.0,
            "gancho": max(media(s_norm, a, g), media(s_fala, a, g)),
            "texto": min(1.0, (s_txt[b] - s_txt[a]) / 4),
            "duracao": max(0.0, 1 - abs((e - s) - alvo) / max(1.0, dmax - dmin)),
            "abertura": abertura,
            "silencio": media(s_sil, a, b),
        }
        nota = sum(float(pesos.get(k, 0)) * v for k, v in d.items() if k != "silencio")
        nota -= float(pesos.get("silencio", 0)) * d["silencio"]
        # bônus para começar/terminar em ponto natural (troca de cena vale mais)
        bonus = {"cena": 1.0, "fala": 0.7, "grade": 0.0}
        nota += 0.3 * (bonus[tipo_ini[s]] + bonus[tipo_fim[e]]) / 2
        return nota, d

    candidatos: list[Candidato] = []
    for s in inicios:
        j0, j1 = bisect_left(fins, s + dmin), bisect_right(fins, s + dmax)
        melhor = None
        abertura = sinal_abertura(todas_palavras, inicios_palavras, s)
        for e in fins[j0:j1]:
            nota, det = pontuar(s, e, abertura)
            if melhor is None or nota > melhor[0]:
                melhor = (nota, e, det)
        if melhor:
            nota, e, det = melhor
            candidatos.append(Candidato(round(s, 2), round(e, 2), round(nota, 4), {k: round(v, 3) for k, v in det.items()}))

    # seleção gulosa sem sobreposição
    candidatos.sort(key=lambda x: x.pontuacao, reverse=True)
    espaco = float(c["espaco_minimo_seg"])
    bloqueados = [(float(a), float(b)) for a, b in (ocupados or [])]
    escolhidos: list[Candidato] = []
    for cand in candidatos:
        if len(escolhidos) >= int(c["max_por_filme"]):
            break
        if any(cand.inicio < o.fim + espaco and cand.fim > o.inicio - espaco for o in escolhidos):
            continue
        if any(cand.inicio < b + espaco and cand.fim > a - espaco for a, b in bloqueados):
            continue
        # descarta trechos "mortos": quase sem som e sem fala
        if cand.detalhes["fala"] < 0.1 and cand.detalhes["volume"] < 0.15 and tem_volume:
            continue
        cand.frase = melhor_frase(frases, cand.inicio, cand.fim)
        escolhidos.append(cand)
    return escolhidos
