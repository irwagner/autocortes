"""Textos das postagens escritos por IA (opcional).

Funciona com qualquer API compatível com a da OpenAI: o Ollama local (padrão, nada sai
do PC), LM Studio, OpenRouter, a própria OpenAI etc. Se a IA falhar ou estiver
desligada, os modelos de texto do [textos] continuam valendo.
"""

from __future__ import annotations

import json
import re
import time

import requests

from .config import Config
from .legendas import Frase, palavras_no_intervalo
from .textos import episodio_de, limitar, normalizar
from .util import log

ESQUEMA = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string"},
        "descricao": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["titulo", "descricao", "hashtags"],
    "additionalProperties": False,
}

SISTEMA = (
    "Você escreve textos de postagem para cortes curtos de filmes em redes sociais (YouTube Shorts, "
    "TikTok e Instagram Reels), para o público do Brasil. Escreva em português do Brasil, de forma "
    "natural e sem exageros. Responda somente com um JSON válido no formato pedido."
)

REGRAS = """Regras:
- titulo: até 60 caracteres, desperta curiosidade sobre a cena, sem emojis, sem hashtags, sem aspas e sem o nome do filme.
- descricao: 1 ou 2 frases sobre o que acontece na cena e uma pergunta curta para o público comentar. Até 220 caracteres, sem hashtags.
- hashtags: de 3 a 5, específicas da cena, do tema ou do gênero (ex.: #suspense), sem espaços e sem repetir o nome do filme.
- Fale só do que aparece na fala abaixo. Não invente fatos e não conte o final do filme."""

_PENSAMENTO = re.compile(r"<think>.*?</think>", re.S | re.I)
_BLOCO_JSON = re.compile(r"\{.*\}", re.S)


class ErroIA(Exception):
    def __init__(self, mensagem: str, do_corte: bool = False):
        super().__init__(mensagem)
        # True quando o problema foi a resposta para este corte, e não a conexão ou a configuração
        self.do_corte = do_corte


def disponivel(cfg: Config) -> bool:
    ia = cfg["ia"]
    return bool(ia["ativo"] and str(ia["url"]).strip() and str(ia["modelo"]).strip())


def _endpoint(cfg: Config, caminho: str) -> str:
    return str(cfg["ia"]["url"]).strip().rstrip("/") + caminho


def _cabecalhos(cfg: Config) -> dict:
    cab = {"Content-Type": "application/json", "User-Agent": "autocortes/1.0"}
    chave = str(cfg["ia"]["api_key"]).strip()
    if chave:
        cab["Authorization"] = f"Bearer {chave}"
    return cab


def conversar(cfg: Config, mensagens: list[dict], esquema: dict | None = None) -> str:
    """Uma resposta do modelo (texto puro, sem os blocos <think> de modelos que "pensam")."""
    ia = cfg["ia"]
    corpo = {
        "model": str(ia["modelo"]).strip(),
        "messages": mensagens,
        "temperature": float(ia["temperatura"]),
        "stream": False,
    }
    formatos = [None]
    if esquema:
        formatos = [
            {"type": "json_schema", "json_schema": {"name": "textos", "schema": esquema, "strict": True}},
            {"type": "json_object"},
            None,
        ]
    ultimo = ""
    for formato in formatos:
        dados = dict(corpo)
        if formato:
            dados["response_format"] = formato
        try:
            r = requests.post(_endpoint(cfg, "/chat/completions"), json=dados, headers=_cabecalhos(cfg),
                              timeout=(10, float(ia["tempo_limite_seg"])))
        except requests.ConnectionError as e:
            raise ErroIA(f"não consegui falar com a IA em {ia['url']} (ela está aberta?)") from e
        except requests.Timeout as e:
            raise ErroIA(f"a IA demorou mais de {int(ia['tempo_limite_seg'])} s para responder") from e
        except requests.RequestException as e:
            raise ErroIA(f"falha de rede com a IA: {e.__class__.__name__}") from e
        if r.status_code in (400, 422) and formato is not None:
            ultimo = r.text[:200]
            continue  # provedor sem suporte a esse formato: tenta o próximo
        if r.status_code == 401:
            raise ErroIA("a IA recusou a chave da API")
        if r.status_code == 404:
            raise ErroIA(f"modelo '{ia['modelo']}' não encontrado (no Ollama: ollama pull {ia['modelo']})")
        if r.status_code >= 400:
            raise ErroIA(f"a IA respondeu HTTP {r.status_code}: {r.text[:200]}")
        try:
            texto = r.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ErroIA("resposta da IA em formato inesperado") from e
        return _PENSAMENTO.sub("", texto).strip()
    raise ErroIA(f"a IA recusou o pedido: {ultimo}")


def listar_modelos(cfg: Config) -> list[str]:
    try:
        r = requests.get(_endpoint(cfg, "/models"), headers=_cabecalhos(cfg), timeout=(5, 20))
        r.raise_for_status()
        return sorted(str(m.get("id")) for m in (r.json().get("data") or []) if m.get("id"))
    except (requests.RequestException, ValueError, AttributeError) as e:
        raise ErroIA(f"não consegui listar os modelos em {cfg['ia']['url']}: {e.__class__.__name__}") from e


def transcricao_do_corte(frases: list[Frase], inicio: float, fim: float, limite: int = 2200) -> str:
    """Fala do trecho com marcas de tempo a cada ~5 s, para a IA entender o ritmo da cena."""
    palavras = palavras_no_intervalo(frases, inicio, fim)
    linhas, atual, marca = [], [], None
    for p in palavras:
        if marca is None or p.ini - marca >= 5:
            if atual:
                linhas.append(" ".join(atual))
            marca, atual = p.ini, [f"[{int(p.ini) // 60}:{int(p.ini) % 60:02d}]"]
        atual.append(p.txt)
    if atual:
        linhas.append(" ".join(atual))
    return limitar("\n".join(linhas), limite)


def _limpar_hashtags(itens, proibidas: set[str]) -> list[str]:
    saida, vistas = [], set()
    for bruto in itens if isinstance(itens, list) else []:
        tag = re.sub(r"[^\w]", "", str(bruto).replace("#", ""), flags=re.UNICODE)
        chave = normalizar(tag)
        if len(tag) < 2 or chave in vistas or chave in proibidas:
            continue
        vistas.add(chave)
        saida.append("#" + tag)
    return saida[:5]


def _validar(bruto: str, filme: str) -> dict:
    achado = _BLOCO_JSON.search(bruto)
    if not achado:
        raise ErroIA("a IA não devolveu JSON", do_corte=True)
    try:
        dados = json.loads(achado.group(0))
    except json.JSONDecodeError as e:
        raise ErroIA("a IA devolveu um JSON inválido", do_corte=True) from e
    if not isinstance(dados, dict):
        raise ErroIA("a IA devolveu um JSON fora do formato", do_corte=True)
    titulo = re.sub(r"#\w+", "", str(dados.get("titulo") or "")).strip(" \"'“”«»-–—\n\t")
    descricao = re.sub(r"(?<![\w#])#\w+", "", str(dados.get("descricao") or "")).strip()
    descricao = re.sub(r"[ \t]{2,}", " ", descricao)
    if len(titulo) < 3 or len(descricao) < 10:
        raise ErroIA("a IA devolveu título ou descrição vazios", do_corte=True)
    if re.search(r"https?://|www\.", titulo + descricao, re.I):
        raise ErroIA("a IA colocou links no texto", do_corte=True)
    # a hashtag do filme (ou da série, sem o T1:E1) já entra sozinha no post
    nome = episodio_de(filme)[0]
    proibidas = {normalizar(re.sub(r"[^\w]", "", nome)), normalizar(re.sub(r"[^\w]", "", filme)),
                 "filme", "filmes", "cinema", "shorts", "reels", "fyp"}
    return {
        "titulo": limitar(titulo, 70),
        "descricao": limitar(descricao, 400),
        "hashtags": _limpar_hashtags(dados.get("hashtags"), proibidas),
    }


def gerar_textos(cfg: Config, filme: dict, corte: dict, frases: list[Frase]) -> dict:
    """{titulo, descricao, hashtags, modelo, gerado_em} para o corte; lança ErroIA se não der."""
    titulo_filme = str(filme["titulo"])
    fala = transcricao_do_corte(frases, float(corte["inicio"]), float(corte["fim"]))
    ano = filme["ano"] if "ano" in filme.keys() else None
    serie, temporada, episodio = episodio_de(titulo_filme)
    obra = (f"Série: {serie}, temporada {temporada}, episódio {episodio}" if temporada is not None
            else f"Filme: {titulo_filme}")
    pedido = (
        f"{obra}{f' ({ano})' if ano else ''}\n"
        f"Duração do corte: {int(float(corte['fim']) - float(corte['inicio']))} s\n"
        f"Fala do trecho:\n{fala or '(sem fala: cena de ação, música ou silêncio)'}\n\n"
        f"{REGRAS}\n\n"
        'Formato: {"titulo": "...", "descricao": "...", "hashtags": ["#...", "#..."]}'
    )
    inicio = time.monotonic()
    resposta = conversar(cfg, [{"role": "system", "content": SISTEMA}, {"role": "user", "content": pedido}], ESQUEMA)
    textos = _validar(resposta, titulo_filme)
    textos.update(modelo=str(cfg["ia"]["modelo"]), gerado_em=time.time())
    log.info("IA escreveu os textos do corte %s em %.0f s: %s", corte["id"], time.monotonic() - inicio, textos["titulo"])
    return textos


# ---------------------------------------------------------------- pauta de vídeo criado

ESQUEMA_PAUTA = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string"},
        "roteiro": {"type": "string"},
        "termos": {"type": "array", "items": {"type": "string"}},
        "topo": {"type": "string"},
        "descricao": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["titulo", "roteiro", "termos", "topo", "descricao", "hashtags"],
    "additionalProperties": False,
}

SISTEMA_PAUTA = (
    "Você escreve roteiros curtos para vídeos verticais de redes sociais (Shorts, Reels, TikTok), "
    "em português do Brasil, para serem lidos em voz alta. Escreva de forma direta e natural, sem "
    "exageros e sem clichê de coach. Responda somente com um JSON válido no formato pedido."
)
# fim de frase seguido de espaço: usado para quebrar o roteiro por frase quando vem tudo junto
_FIM_DE_FRASE = re.compile(r"([.!?…])\s+")


def _regras_pauta(segundos: int, palavras: int) -> str:
    return f"""Regras:
- roteiro: o texto que a voz vai ler, de {int(palavras * 0.8)} a {int(palavras * 1.2)} palavras
  (perto de {segundos} segundos de fala). Escreva o texto completo, não um resumo.
  Uma frase por linha, na ordem da narração. Sem emojis, sem hashtags, sem títulos, sem marcação.
  A primeira linha tem que prender em até 3 segundos. A última fecha a ideia (sem "se inscreva").
  Para um silêncio de efeito, escreva [pausa: 1s] no meio do texto.
- titulo: até 60 caracteres, sem emojis e sem hashtags.
- topo: até 22 caracteres, em letras maiúsculas, o texto que fica escrito no alto do vídeo.
- termos: de 3 a 6 buscas de imagem de fundo, em INGLÊS, concretas e visuais (ex.: "sunrise over the sea",
  "man running at night"). Nada abstrato, nada de texto na imagem, nada de pessoa famosa.
- descricao: 1 ou 2 frases para a descrição do post, mais uma pergunta curta para o público comentar.
- hashtags: de 3 a 5, específicas do tema, sem espaços."""


def _validar_pauta(bruto: str, tema: str) -> dict:
    achado = _BLOCO_JSON.search(bruto)
    if not achado:
        raise ErroIA("a IA não devolveu JSON", do_corte=True)
    try:
        dados = json.loads(achado.group(0))
    except json.JSONDecodeError as e:
        raise ErroIA("a IA devolveu um JSON inválido", do_corte=True) from e
    if not isinstance(dados, dict):
        raise ErroIA("a IA devolveu um JSON fora do formato", do_corte=True)

    roteiro = str(dados.get("roteiro") or "").replace("\r", "")
    # modelo pequeno às vezes escreve a barra e o n como texto: a voz leria "barra n" em voz alta
    roteiro = re.sub(r"\\+[nr]", "\n", roteiro)
    roteiro = re.sub(r"^\s*[-*•]\s*", "", roteiro, flags=re.M)      # marcador de lista
    roteiro = re.sub(r"(?<![\w#])#\w+", "", roteiro)                # hashtag na narração não se fala
    roteiro = re.sub(r"[ \t]{2,}", " ", roteiro)
    roteiro = "\n".join(linha.strip() for linha in roteiro.split("\n") if linha.strip())
    if roteiro.count("\n") == 0 and len(_FIM_DE_FRASE.findall(roteiro)) >= 2:
        # veio tudo numa linha: quebrar por frase dá o respiro entre elas na narração
        roteiro = _FIM_DE_FRASE.sub(lambda m: m.group(1) + "\n", roteiro).strip()
    if len(roteiro.split()) < 15:
        raise ErroIA("a IA devolveu um roteiro curto demais", do_corte=True)
    if re.search(r"https?://|www\.", roteiro, re.I):
        raise ErroIA("a IA colocou links no roteiro", do_corte=True)

    limpar_pontas = " \"'“”«»-–—\n\t{}[]`"
    titulo = re.sub(r"#\w+", "", str(dados.get("titulo") or "")).strip(limpar_pontas) or tema
    topo = re.sub(r"[^\w\s\-!?]", "", str(dados.get("topo") or "")).strip().upper()
    termos = [re.sub(r"\s+", " ", str(t)).strip(limpar_pontas) for t in (dados.get("termos") or []) if str(t).strip()]
    termos = [t for t in termos if t]
    descricao = re.sub(r"(?<![\w#])#\w+", "", str(dados.get("descricao") or "")).strip(limpar_pontas).strip()
    return {
        "titulo": limitar(titulo, 70),
        "roteiro": roteiro,
        "termos": termos[:6],
        "topo": limitar(topo, 22) if topo else "",
        "descricao": limitar(descricao, 400),
        "hashtags": _limpar_hashtags(dados.get("hashtags"), {"shorts", "reels", "fyp", "viral"}),
    }


def gerar_pauta(cfg: Config, tema: str, segundos: float = 40.0) -> dict:
    """Roteiro, termos de busca e textos do post para um tema; lança ErroIA se não der."""
    tema = str(tema or "").strip()
    if not tema:
        raise ErroIA("não há tema para a IA escrever", do_corte=True)
    segundos = int(max(10, min(180, segundos)))
    palavras = int(segundos * 2.4)  # a voz lê perto de 145 palavras por minuto
    pedido = (
        f"Tema do vídeo: {tema}\n\n"
        f"{_regras_pauta(segundos, palavras)}\n\n"
        'Formato: {"titulo": "...", "roteiro": "...", "termos": ["..."], "topo": "...", '
        '"descricao": "...", "hashtags": ["#..."]}'
    )
    inicio = time.monotonic()
    resposta = conversar(
        cfg, [{"role": "system", "content": SISTEMA_PAUTA}, {"role": "user", "content": pedido}], ESQUEMA_PAUTA)
    dados = _validar_pauta(resposta, tema)
    log.info("IA escreveu a pauta de '%s' em %.0f s: %s (%d palavras)", tema, time.monotonic() - inicio,
             dados["titulo"], len(dados["roteiro"].split()))
    return dados


def testar(cfg: Config) -> str:
    inicio = time.monotonic()
    resposta = conversar(cfg, [{"role": "user", "content": "Responda só com a palavra: funcionando"}])
    return f"{cfg['ia']['modelo']} respondeu em {time.monotonic() - inicio:.1f} s: {limitar(resposta, 60)}"
