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
(a primeira que existir na página vence, e se ela estiver desabilitada eu espero liberar); alvo
entre aspas é procurado pelo texto que aparece na tela, sem aspas é um seletor CSS. Seletor
genérico (button[role="button"], div > div...) só vale quando é o único na tela.
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
MAX_ROLAR = 20000  # pixels

# atributos que qualquer botão ou caixa tem: sozinhos, não apontam um elemento específico
_ATRIBUTOS_GENERICOS = {"role", "type", "contenteditable", "tabindex"}
_ATRIBUTO = re.compile(r"""\[\s*([\w:-]+)\s*(?:[~|^$*]?=\s*(?:"[^"]*"|'[^']*'|[^\]\s]+)\s*(?:[iIsS]\s*)?)?\]""")
_PSEUDO_COM_ARGUMENTO = re.compile(r":[\w-]+\((?:[^()]|\([^()]*\))*\)")
# o que tem cara de ser o clique que publica (texto do botão, rótulo ou pedaço do seletor)
_PALAVRAS_PUBLICAR = re.compile(
    r"\b(publicar|publique|postar|post|publish|compartilhar|share|agendar|programar|schedule)\b")
_PUBLICAR_CJK = ("投稿", "发布")
_TOKENS_PUBLICAR = {"post", "publish", "publicar", "done", "share"}


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


# ---------------------------------------------------------------- força dos alvos

def fraco(seletor: str) -> bool:
    """Seletor genérico demais: casa com qualquer botão ou caixa da página.

    Forte é o que tem #id, .classe ou um atributo que diz qual é o elemento (data-e2e, aria-label,
    name, placeholder...). role, type, contenteditable e tabindex não contam, e um caminho como
    "div > div:nth-of-type(2)" também não. Na hora de postar, o genérico só vale se for o único
    elemento visível que casa com ele.
    """
    s = str(seletor or "")
    if any(nome.lower() not in _ATRIBUTOS_GENERICOS for nome in _ATRIBUTO.findall(s)):
        return False
    resto = _PSEUDO_COM_ARGUMENTO.sub(" ", _ATRIBUTO.sub(" ", s))
    return not re.search(r"[#.][A-Za-z_\\-]", resto)


def texto_de_publicar(texto: str) -> bool:
    """O texto de um botão tem cara de publicar ("Publicar", "Post", "Compartilhar", "投稿"...)."""
    bruto = str(texto or "")
    return bool(_PALAVRAS_PUBLICAR.search(sem_acentos(bruto).lower())) or any(m in bruto for m in _PUBLICAR_CJK)


def parece_publicar(alvos) -> bool:
    """Algum dos alvos tem cara de ser o botão que publica.

    Serve de rede de segurança do ensaio: mesmo que a linha diga só "clicar", um clique com
    cara de publicar não é executado no ensaio.
    """
    for alvo in alvos:
        if isinstance(alvo, Alvo):
            valor, por_texto = alvo.valor, alvo.por_texto
        else:
            valor, por_texto = str(alvo), False
        if por_texto:
            if texto_de_publicar(valor):
                return True
            continue
        if set(re.split(r"[^a-z0-9]+", sem_acentos(valor).lower())) & _TOKENS_PUBLICAR:
            return True
        # o valor de um atributo também conta: button[aria-label="Publicar agora"]
        if any(texto_de_publicar(v) for v in re.findall(r"""=\s*["']([^"']*)["']""", valor)):
            return True
    return False


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
            if opcional:
                erros.append(f"linha {n}: o envio do vídeo não pode ser opcional")
                continue
            alvo = resto
            if alvo.lower().startswith("em "):
                alvo = alvo[3:]
            ins.alvos = _partir_alvos(alvo) if alvo.strip() else [Alvo("input[type=file]")]
            if any(a.por_texto for a in ins.alvos):
                erros.append(f"linha {n}: o vídeo entra num campo de arquivo; use um seletor CSS, sem aspas "
                             "(ex.: video em input[type=file])")
                continue
        elif comando in ("clicar", "publicar"):
            if comando == "publicar" and opcional:
                erros.append(f"linha {n}: o clique que publica não pode ser opcional")
                continue
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
            if any(a.por_texto for a in ins.alvos):
                erros.append(f"linha {n}: o campo onde escrever precisa ser um seletor CSS, sem aspas "
                             "(texto entre aspas só serve para clicar, esperar e conferir)")
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
            if any(a.por_texto for a in ins.alvos):
                erros.append(f"linha {n}: o campo das tags precisa ser um seletor CSS, sem aspas")
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
                if not 1 <= segundos <= 900:
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
            if not resto:
                ins.segundos = 600  # pixels, na verdade
            elif re.fullmatch(r"-?\d+", resto) and 0 < abs(int(resto)) <= MAX_ROLAR:
                ins.segundos = int(resto)
            else:
                erros.append(f"linha {n}: rolar precisa de um número inteiro de pixels, até {MAX_ROLAR} "
                             "(ex.: rolar 800; negativo sobe)")
                continue
        else:
            erros.append(f"linha {n}: não conheço o comando \"{palavras[0]}\"")
            continue
        instrucoes.append(ins)

    if not erros:
        videos = [i.linha for i in instrucoes if i.comando == "video"]
        publicar = [i.linha for i in instrucoes if i.comando == "publicar"]
        if not videos:
            erros.append("falta a linha \"video\", que entrega o corte para a rede")
        if not publicar:
            erros.append("falta a linha \"publicar\", que diz qual clique publica o vídeo")
        if videos and publicar and publicar[0] < videos[0]:
            erros.append(f"linha {publicar[0]}: o clique que publica precisa vir depois da linha \"video\"")
    return instrucoes, erros


def validar(texto: str, rede: str | None = None) -> list[Instrucao]:
    instrucoes, erros = analisar(texto, rede)
    if erros:
        raise ErroRoteiro(erros)
    return instrucoes


# ---------------------------------------------------------------- escrita

def _alvos_de(passo: dict) -> str:
    """As alternativas de um passo: seletores fortes, depois o texto do botão.

    Seletor genérico só entra quando não sobrou nada melhor (o painel avisa) ou no campo de
    arquivo, que costuma ser o único da página.
    """
    seletores = [str(s) for s in (passo.get("seletores") or []) if s]
    alvos = [s for s in seletores if not fraco(s)]
    for chave in ("texto", "rotulo", "dica"):
        valor = " ".join(str(passo.get(chave) or "").split()).replace('"', "'")
        if valor and f'"{valor}"' not in alvos:
            alvos.append(f'"{valor}"')
    if passo.get("tipo") == "arquivo" or not alvos:
        alvos += [s for s in seletores if fraco(s)]
    return " ou ".join(alvos[:4])


def gerar(rede: str, passos: list[dict], url: str, confirmacao: str = "", publicar_em: int | None = None,
          publicar_incerto: bool = False, texto_inicial: str = "") -> str:
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
            if i == publicar_em and publicar_incerto:
                linhas.append("# Confira: nenhum clique tinha cara de publicar, então marquei o último. "
                              "Se não for este, troque o \"publicar\" de linha.")
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
    pista = _pista(confirmacao, texto_inicial, [str(p.get("valor") or "") for p in passos])
    if pista:
        linhas.append(f'conferir "{pista}"')
    else:
        linhas += [
            "# Falta conferir: sem uma linha assim, eu não tenho como saber se o vídeo saiu mesmo.",
            "# Troque pelo texto que a rede mostra quando publica e tire o # da frente:",
            "#   conferir \"Seu vídeo foi publicado\"",
        ]
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


def _pista(confirmacao: str, inicial: str = "", digitados=()) -> str:
    """Uma frase que apareceu na tela depois que você publicou.

    Não serve o que já estava na página no começo (menu, cabeçalho: sempre estaria lá), nem o que
    você escreveu (a legenda muda a cada post), nem frase com número (data, contagem).
    """
    antes = " ".join(str(inicial).split()).lower()
    meus = [" ".join(str(d).split()).lower() for d in digitados if str(d).strip()]
    for frase in re.split(r"[.!|·•\n]", " ".join(str(confirmacao).split())):
        f = frase.strip()
        if not 12 <= len(f) <= 60 or '"' in f or "@@" in f or re.search(r"\d", f):
            continue
        baixa = f.lower()
        if antes and baixa in antes:
            continue
        if any(baixa in m or (len(m) >= 6 and m[:20] in baixa) for m in meus):
            continue
        return f
    return ""


# lista de comandos mostrada no painel, ao lado do editor
AJUDA = [
    {"comando": "abrir", "exemplo": "abrir https://www.tiktok.com/tiktokstudio/upload",
     "o_que": "vai para o endereço. Costuma ser a primeira linha."},
    {"comando": "video", "exemplo": "video em input[type=file]",
     "o_que": "entrega o corte no campo de arquivo. Sem o \"em\", procura sozinho."},
    {"comando": "clicar", "exemplo": 'clicar #post ou "Publicar agora"',
     "o_que": "clica no primeiro alvo que existir na tela; se ele estiver desabilitado, espera liberar. "
              "Seletor genérico (button[role=\"button\"], div > div) só vale se for o único na tela."},
    {"comando": "publicar", "exemplo": 'publicar #post ou "Publicar agora"',
     "o_que": "o clique que publica. No ensaio eu paro aqui (e em qualquer clique com cara de publicar "
              "depois do vídeo), sem clicar."},
    {"comando": "escrever", "exemplo": "escrever legenda em #legenda",
     "o_que": "escreve o texto do corte. Vale título, descrição, legenda, fonte, "
              "ou um texto fixo entre aspas. O campo é um seletor CSS, sem aspas."},
    {"comando": "tags", "exemplo": "tags em input[placeholder='tag']",
     "o_que": "escreve cada hashtag e tecla Enter, uma por uma."},
    {"comando": "esperar", "exemplo": "esperar 5",
     "o_que": "para de 1 a 900 segundos sem fazer nada."},
    {"comando": "esperar", "exemplo": 'esperar "Enviado"',
     "o_que": "espera o texto aparecer na tela (ou o elemento existir). Melhor que contar tempo."},
    {"comando": "tecla", "exemplo": "tecla Enter",
     "o_que": "tecla Enter, Tab ou Escape no campo em que você está."},
    {"comando": "rolar", "exemplo": "rolar 800",
     "o_que": "rola a página para baixo, em pixels (negativo sobe)."},
    {"comando": "conferir", "exemplo": 'conferir "foi publicado"',
     "o_que": "no fim, espera a confirmação da rede. Se não vier, o post fica \"a conferir\" no painel, "
              "para você dizer se saiu."},
    {"comando": "opcional", "exemplo": 'opcional clicar "Agora não"',
     "o_que": "na frente de qualquer linha (menos video e publicar): se não achar o alvo, segue em frente."},
    {"comando": "#", "exemplo": "# isto é um comentário",
     "o_que": "linha que começa com # não faz nada. Serve para desligar um passo sem apagar."},
]


def avisos(instrucoes: list[Instrucao], rede: str | None = None) -> list[str]:
    """O que funciona, mas é arriscado: seletor genérico, clique de publicar cedo, falta de conferir."""
    saida: list[str] = []
    viu_video = viu_publicar = False
    for ins in instrucoes:
        if ins.comando == "video":
            viu_video = True
            continue
        if ins.comando == "publicar":
            viu_publicar = True
        css = [a for a in ins.alvos if not a.por_texto]
        genericos = [a.valor for a in css if fraco(a.valor)]
        if genericos:
            if len(genericos) == len(ins.alvos):
                saida.append(f"linha {ins.linha}: nenhuma alternativa aponta um elemento específico "
                             f"({genericos[0]}); só uso se for o único na tela. Grave de novo ou troque "
                             "por um seletor com id, classe ou atributo próprio")
            else:
                saida.append(f"linha {ins.linha}: {', '.join(genericos[:2])} é genérico e só vale se for o "
                             "único na tela; dá para apagar essa alternativa")
        if ins.comando == "clicar" and viu_video and not viu_publicar and parece_publicar(ins.alvos):
            saida.append(f"linha {ins.linha}: este clique tem cara de publicar, mas vem antes da linha "
                         "\"publicar\". No ensaio eu paro aqui; se for ele que publica, troque o comando")
        elif ins.comando == "clicar" and viu_publicar and not ins.opcional and parece_publicar(ins.alvos):
            saida.append(f"linha {ins.linha}: confirmação depois de publicar. Se a caixa nem sempre aparece, "
                         "ponha \"opcional\" na frente, senão o envio para aqui")
    if instrucoes and not any(i.comando == "conferir" for i in instrucoes):
        saida.append("falta uma linha \"conferir\": sem ela eu não tenho como saber se o vídeo saiu. "
                     "Ex.: conferir \"Seu vídeo foi publicado\"")
    if rede:
        escritos = {i.papel for i in instrucoes if i.papel}
        faltando = [NOMES.get(p, p) for p in PAPEIS_DA_REDE.get(rede, ()) if p not in escritos]
        if faltando:
            saida.append(f"Sem preenchimento automático de {', '.join(faltando)}. Os textos fixos "
                         "se repetem como foram gravados; você pode ajustar os campos no roteiro.")
    for ins in instrucoes:
        if ins.texto and "@@" in ins.texto:
            saida.append(f"linha {ins.linha}: a marca não foi identificada e ficou como texto fixo; "
                         "ela será repetida como foi digitada.")
    return saida


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
        "avisos": avisos(instrucoes, rede) if not erros else [],
        "papeis": sorted({i.papel for i in instrucoes if i.papel}),
        "tem_video": any(i.comando == "video" for i in instrucoes),
        "tem_publicar": any(i.comando == "publicar" for i in instrucoes),
        "tem_conferir": any(i.comando == "conferir" for i in instrucoes),
    }
