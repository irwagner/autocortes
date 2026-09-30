"""A linguagem dos roteiros de postagem: um comando por linha, em português.

O roteiro é gravado enquanto você posta um vídeo à mão, e sai como texto que você pode ler e
editar no painel. Serve para consertar o que a gravação não pegou bem: dar um tempo antes de um
clique, esperar um texto aparecer, tornar um passo opcional.

    # Roteiro do TikTok
    abrir https://www.tiktok.com/tiktokstudio/upload
    clicar #escolher ou "Selecionar vídeo"
    video
    escrever legenda em #legenda ou div[contenteditable="true"]
    esperar "Enviado"
    publicar #post ou "Publicar agora"
    conferir "foi publicado"

Regras: uma ação por linha; linha começando com # é comentário; alternativas separadas por " ou "
(a primeira que existir na página vence); alvo entre aspas é procurado pelo texto que aparece na
tela, sem aspas é um seletor CSS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .util import sem_acentos

# textos-marca que você cola nos campos durante a gravação, para eu saber o papel de cada um
MARCAS = {
    "titulo": "@@TITULO@@",
    "descricao": "@@DESCRICAO@@",
    "legenda": "@@LEGENDA@@",
    "tags": "@@TAGS@@",
    "fonte": "@@FONTE@@",
}
# o que cada rede precisa que você marque na gravação
PAPEIS_DA_REDE = {
    "youtube": ("titulo", "descricao", "tags"),
    "tiktok": ("legenda",),
    "instagram": ("legenda",),
    "bilibili": ("titulo", "descricao", "tags", "fonte"),
}
NOMES = {"titulo": "título", "descricao": "descrição", "legenda": "legenda", "tags": "tags", "fonte": "fonte"}
TECLAS = ("Enter", "Tab", "Escape")


class ErroRoteiro(Exception):
    """Roteiro com linha que eu não entendi."""

    def __init__(self, erros: list[str]):
        super().__init__("; ".join(erros))
        self.erros = erros


@dataclass
class Alvo:
    """Onde agir: um seletor CSS ou um texto que aparece na tela."""

    valor: str
    por_texto: bool = False

    def __str__(self) -> str:
        return f'"{self.valor}"' if self.por_texto else self.valor


@dataclass
class Instrucao:
    comando: str
    alvos: list[Alvo] = field(default_factory=list)
    papel: str | None = None      # escrever: titulo, legenda...
    texto: str | None = None      # escrever: texto fixo entre aspas
    segundos: float | None = None  # esperar 5
    tecla: str = "Enter"
    url: str = ""
    opcional: bool = False
    linha: int = 0
    origem: str = ""

    @property
    def descricao(self) -> str:
        return self.origem.strip()


# ---------------------------------------------------------------- leitura

def _sem_aspas(texto: str) -> tuple[str, bool]:
    t = texto.strip()
    if len(t) >= 2 and t[0] == t[-1] == '"':
        return t[1:-1], True
    return t, False


def _partir_alvos(texto: str) -> list[Alvo]:
    """Separa as alternativas por " ou ", respeitando o que está entre aspas."""
    partes: list[str] = []
    atual = ""
    dentro = False
    i = 0
    while i < len(texto):
        c = texto[i]
        if c == '"':
            dentro = not dentro
        if not dentro and texto[i:i + 4].lower() == " ou " :
            partes.append(atual)
            atual = ""
            i += 4
            continue
        atual += c
        i += 1
    partes.append(atual)
    alvos = []
    for parte in partes:
        valor, por_texto = _sem_aspas(parte)
        if valor:
            alvos.append(Alvo(valor, por_texto))
    return alvos


def _partir_em(texto: str) -> tuple[str, str]:
    """Separa "<o que> em <onde>" no último " em " que estiver fora de aspas."""
    dentro = False
    corte = -1
    for i in range(len(texto)):
        if texto[i] == '"':
            dentro = not dentro
        elif not dentro and texto[i:i + 4].lower() == " em ":
            corte = i
    if corte < 0:
        return texto, ""
    return texto[:corte], texto[corte + 4:]


def _numero(texto: str) -> float | None:
    try:
        return float(texto.replace(",", ".").rstrip("s").strip())
    except ValueError:
        return None


def analisar(texto: str, rede: str | None = None) -> tuple[list[Instrucao], list[str]]:
    """Lê o roteiro e devolve as instruções e os erros (um por linha problemática)."""
    instrucoes: list[Instrucao] = []
    erros: list[str] = []
    papeis_validos = set(PAPEIS_DA_REDE.get(rede or "", MARCAS.keys()))

    for n, bruta in enumerate(texto.splitlines(), 1):
        linha = bruta.strip()
        if not linha or linha.startswith("#"):
            continue
        opcional = False
        if sem_acentos(linha).lower().startswith("opcional "):
            opcional, linha = True, linha[len("opcional "):].strip()
        palavras = linha.split(None, 1)
        comando = sem_acentos(palavras[0]).lower()
        resto = palavras[1].strip() if len(palavras) > 1 else ""
        ins = Instrucao(comando=comando, opcional=opcional, linha=n, origem=bruta)

        if comando == "abrir":
            if not re.match(r"^https?://\S+$", resto):
                erros.append(f"linha {n}: depois de \"abrir\" precisa vir um endereço http")
                continue
            ins.url = resto
        elif comando in ("video", "arquivo"):
            ins.comando = "video"
            alvo = resto
            if alvo.lower().startswith("em "):
                alvo = alvo[3:]
            ins.alvos = _partir_alvos(alvo) if alvo.strip() else [Alvo("input[type=file]")]
        elif comando in ("clicar", "publicar"):
            ins.alvos = _partir_alvos(resto)
            if not ins.alvos:
                erros.append(f"linha {n}: \"{palavras[0]}\" precisa dizer onde clicar")
                continue
        elif comando == "escrever":
            oque, onde = _partir_em(resto)
            ins.alvos = _partir_alvos(onde)
            valor, entre_aspas = _sem_aspas(oque)
            if not ins.alvos:
                erros.append(f"linha {n}: falta o \" em <onde>\" no fim (ex.: escrever legenda em #campo)")
                continue
            if entre_aspas:
                ins.texto = valor
            else:
                papel = sem_acentos(valor).lower()
                if papel not in MARCAS:
                    erros.append(f"linha {n}: não conheço o texto \"{valor}\". Use {', '.join(MARCAS)} "
                                 "ou um texto fixo entre aspas")
                    continue
                if papel not in papeis_validos:
                    erros.append(f"linha {n}: esta rede não tem {NOMES.get(papel, papel)}; "
                                 f"aqui vale {', '.join(sorted(papeis_validos))}")
                    continue
                ins.papel = papel
        elif comando == "tags":
            onde = resto[3:] if resto.lower().startswith("em ") else resto
            ins.alvos = _partir_alvos(onde)
            ins.papel = "tags"
            if not ins.alvos:
                erros.append(f"linha {n}: \"tags\" precisa do campo (ex.: tags em #tag)")
                continue
            if "tags" not in papeis_validos:
                erros.append(f"linha {n}: esta rede não usa tags")
                continue
        elif comando == "tecla":
            nome = resto.strip().capitalize() or "Enter"
            if nome not in TECLAS:
                erros.append(f"linha {n}: tecla \"{resto}\" não vale; use {', '.join(TECLAS)}")
                continue
            ins.tecla = nome
        elif comando == "esperar":
            segundos = _numero(resto)
            if segundos is not None:
                if not 0 < segundos <= 900:
                    erros.append(f"linha {n}: esperar precisa ser de 1 a 900 segundos")
                    continue
                ins.segundos = segundos
            else:
                ins.alvos = _partir_alvos(resto)
                if not ins.alvos:
                    erros.append(f"linha {n}: esperar precisa de segundos ou do que esperar na tela")
                    continue
        elif comando == "conferir":
            ins.alvos = _partir_alvos(resto)
            if not ins.alvos:
                erros.append(f"linha {n}: conferir precisa do texto ou do elemento que confirma o post")
                continue
        elif comando == "rolar":
            ins.segundos = _numero(resto) or 600  # pixels, na verdade
        else:
            erros.append(f"linha {n}: não conheço o comando \"{palavras[0]}\"")
            continue
        instrucoes.append(ins)

    if not erros:
        if not any(i.comando == "video" for i in instrucoes):
            erros.append("falta a linha \"video\", que entrega o corte para a rede")
        if not any(i.comando == "publicar" for i in instrucoes):
            erros.append("falta a linha \"publicar\", que diz qual clique publica o vídeo")
        if rede:
            escritos = {i.papel for i in instrucoes if i.papel}
            for papel in PAPEIS_DA_REDE.get(rede, ()):
                if papel not in escritos:
                    erros.append(f"nenhuma linha escreve a {NOMES.get(papel, papel)} desta rede")
    return instrucoes, erros


def validar(texto: str, rede: str | None = None) -> list[Instrucao]:
    instrucoes, erros = analisar(texto, rede)
    if erros:
        raise ErroRoteiro(erros)
    return instrucoes


# ---------------------------------------------------------------- escrita

def _alvos_de(passo: dict) -> str:
    alvos = list(passo.get("seletores") or [])
    for chave in ("texto", "rotulo", "dica"):
        valor = str(passo.get(chave) or "").strip()
        if valor and f'"{valor}"' not in alvos:
            alvos.append(f'"{valor}"')
    return " ou ".join(alvos[:4])


def gerar(rede: str, passos: list[dict], url: str, confirmacao: str = "", publicar_em: int | None = None) -> str:
    """Monta o texto do roteiro a partir do que a gravação anotou."""
    import time

    from .util import fmt_data

    linhas = [
        f"# Roteiro do {rede}, gravado em {fmt_data(time.time())}",
        "# Uma ação por linha. Linha começando com # é comentário, e não faz nada.",
        "# Alternativas vão separadas por \" ou \"; entre aspas eu procuro pelo texto na tela.",
        "# Dá para editar: veja a lista de comandos no painel.",
        "",
        f"abrir {url}",
    ]
    for i, passo in enumerate(passos):
        tipo = passo["tipo"]
        alvos = _alvos_de(passo)
        if tipo == "arquivo":
            linhas.append(f"video em {alvos}" if alvos else "video")
        elif tipo == "clicar":
            linhas.append(f"{'publicar' if i == publicar_em else 'clicar'} {alvos}")
        elif tipo == "digitar":
            if passo.get("papel") == "tags":
                linhas.append(f"tags em {alvos}")
            elif passo.get("papel"):
                linhas.append(f"escrever {passo['papel']} em {alvos}")
            else:
                valor = str(passo.get("valor") or "").replace('"', "'")
                linhas.append(f'escrever "{valor}" em {alvos}')
        elif tipo == "tecla":
            linhas.append(f"tecla {passo.get('tecla') or 'Enter'}")
    pista = _pista(confirmacao)
    if pista:
        linhas.append(f'conferir "{pista}"')
    linhas += [
        "",
        "# Precisa de um tempo em algum ponto? Ponha uma linha assim onde quiser:",
        "#   esperar 5",
        "# Ou espere algo aparecer na tela, em vez de contar o tempo:",
        "#   esperar \"Enviado\"",
        "# Passo que às vezes não aparece (uma caixa de aviso, por exemplo):",
        "#   opcional clicar \"Agora não\"",
    ]
    return "\n".join(linhas) + "\n"


def _pista(confirmacao: str) -> str:
    """A frase mais específica do que apareceu na tela quando você terminou de postar."""
    limpo = " ".join(str(confirmacao).split())
    for frase in re.split(r"[.!|·•\n]", limpo):
        f = frase.strip()
        if 12 <= len(f) <= 60 and '"' not in f:
            return f
    return ""


# lista de comandos mostrada no painel, ao lado do editor
AJUDA = [
    {"comando": "abrir", "exemplo": "abrir https://www.tiktok.com/tiktokstudio/upload",
     "o_que": "vai para o endereço. Costuma ser a primeira linha."},
    {"comando": "video", "exemplo": "video em input[type=file]",
     "o_que": "entrega o corte no campo de arquivo. Sem o \"em\", procura sozinho."},
    {"comando": "clicar", "exemplo": 'clicar #post ou "Publicar agora"',
     "o_que": "clica no primeiro alvo que existir na tela e estiver habilitado."},
    {"comando": "publicar", "exemplo": 'publicar #post ou "Publicar agora"',
     "o_que": "o clique que publica. No ensaio eu paro aqui, sem clicar."},
    {"comando": "escrever", "exemplo": "escrever legenda em #legenda",
     "o_que": "escreve o texto do corte. Vale título, descrição, legenda, fonte, "
              "ou um texto fixo entre aspas."},
    {"comando": "tags", "exemplo": "tags em input[placeholder='tag']",
     "o_que": "escreve cada hashtag e tecla Enter, uma por uma."},
    {"comando": "esperar", "exemplo": "esperar 5",
     "o_que": "para de 1 a 900 segundos sem fazer nada."},
    {"comando": "esperar", "exemplo": 'esperar "Enviado"',
     "o_que": "espera o texto aparecer na tela (ou o elemento existir). Melhor que contar tempo."},
    {"comando": "tecla", "exemplo": "tecla Enter",
     "o_que": "tecla Enter, Tab ou Escape no campo em que você está."},
    {"comando": "rolar", "exemplo": "rolar 800",
     "o_que": "rola a página para baixo, em pixels."},
    {"comando": "conferir", "exemplo": 'conferir "foi publicado"',
     "o_que": "no fim, espera a confirmação da rede. Se não vier, eu aviso mas não trato como erro."},
    {"comando": "opcional", "exemplo": 'opcional clicar "Agora não"',
     "o_que": "na frente de qualquer linha: se não achar o alvo, segue em frente."},
    {"comando": "#", "exemplo": "# isto é um comentário",
     "o_que": "linha que começa com # não faz nada. Serve para desligar um passo sem apagar."},
]


def resumo(texto: str, rede: str | None = None) -> dict:
    """Resumo do roteiro para o painel."""
    instrucoes, erros = analisar(texto, rede)
    contagem: dict[str, int] = {}
    for ins in instrucoes:
        contagem[ins.comando] = contagem.get(ins.comando, 0) + 1
    return {
        "linhas": len(instrucoes),
        "contagem": contagem,
        "erros": erros,
        "papeis": sorted({i.papel for i in instrucoes if i.papel}),
        "tem_video": any(i.comando == "video" for i in instrucoes),
        "tem_publicar": any(i.comando == "publicar" for i in instrucoes),
    }
