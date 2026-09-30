"""Clipes e imagens de fundo para os vídeos gerados do zero.

Três fontes, escolhidas em `[estoque].fonte`:
- **pexels** e **pixabay**: bancos de vídeo gratuitos, com chave de API própria (grátis).
  Várias chaves podem ser configuradas e vão sendo alternadas, porque cada uma tem limite
  de uso por hora.
- **pasta**: seus próprios vídeos e imagens, sem chave e sem internet.

O que já foi baixado fica em `dados/estoque/`, então o mesmo termo não é baixado duas vezes.
E o que já foi usado é anotado por perfil: as redes tratam repetição de material como conteúdo
não original, então um clipe não volta em dois vídeos seguidos.

Licenças: o material do Pexels e do Pixabay é de uso livre, inclusive comercial, mas nenhum
dos dois autoriza revender o clipe cru nem passar o banco por seu. O crédito de cada clipe é
guardado em `creditos.json` junto do vídeo, e `credito_do_video` monta a linha para a descrição.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .config import Config
from .midia import duracao_arquivo, sondar
from .util import ler_json, log, salvar_json, slug

__all__ = ["Clipe", "ErroEstoque", "baixar", "buscar", "creditos", "credito_do_video", "marcar_usados", "material"]

EXTENSOES_VIDEO = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
EXTENSOES_IMAGEM = {".jpg", ".jpeg", ".png", ".webp"}
LIMITE_BYTES = 200 * 1024**2  # clipe maior que isso é descartado (não cabe no ritmo de um Short)
MAX_USADOS = 400              # quantos ids ficam na memória de "já usei"
_TRAVA = threading.Lock()     # duas produções podem pedir material ao mesmo tempo


class ErroEstoque(Exception):
    def __init__(self, mensagem: str, tipo: str = "temporario"):
        super().__init__(mensagem)
        self.tipo = tipo  # temporario | config | conteudo


@dataclass
class Clipe:
    fonte: str          # pexels | pixabay | pasta
    id: str
    url: str = ""
    caminho: Path | None = None   # já no disco (fonte pasta, ou cache)
    largura: int = 0
    altura: int = 0
    duracao: float = 0.0
    termo: str = ""
    autor: str = ""
    pagina: str = ""
    imagem: bool = False

    @property
    def chave(self) -> str:
        return f"{self.fonte}:{self.id}"

    @property
    def vertical(self) -> bool:
        return self.altura >= self.largura

    def para_json(self) -> dict:
        return {"fonte": self.fonte, "id": self.id, "autor": self.autor, "pagina": self.pagina,
                "termo": self.termo, "largura": self.largura, "altura": self.altura}


@dataclass
class Material:
    """O que a montagem recebe: clipes na ordem de uso, e os créditos deles."""
    clipes: list[Clipe] = field(default_factory=list)
    creditos: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------- pastas e memória

def pasta_cache(cfg: Config) -> Path:
    return cfg.pasta_dados / "estoque"


def _arquivo_usados(cfg: Config) -> Path:
    return pasta_cache(cfg) / "usados.json"


def usados(cfg: Config) -> list[str]:
    dados = ler_json(_arquivo_usados(cfg), {}) or {}
    lista = dados.get("clipes")
    return [str(x) for x in lista] if isinstance(lista, list) else []


def marcar_usados(cfg: Config, clipes: list[Clipe]) -> None:
    """Anota o que entrou num vídeo, para não repetir no próximo."""
    if not clipes:
        return
    with _TRAVA:
        atuais = usados(cfg)
        novos = [c.chave for c in clipes if c.chave not in atuais]
        salvar_json(_arquivo_usados(cfg), {"clipes": (atuais + novos)[-MAX_USADOS:]})


def creditos(cfg: Config) -> dict:
    return ler_json(pasta_cache(cfg) / "creditos.json", {}) or {}


def _guardar_credito(cfg: Config, clipe: Clipe) -> None:
    with _TRAVA:
        dados = creditos(cfg)
        dados[clipe.chave] = clipe.para_json()
        salvar_json(pasta_cache(cfg) / "creditos.json", dados)


def credito_do_video(clipes: list[Clipe]) -> str:
    """Linha de crédito para a descrição do post (vazia quando é material próprio)."""
    partes = []
    for fonte, rotulo in (("pexels", "Pexels"), ("pixabay", "Pixabay")):
        autores = sorted({c.autor for c in clipes if c.fonte == fonte and c.autor})
        if autores:
            partes.append(f"Imagens: {', '.join(autores[:4])} ({rotulo})")
        elif any(c.fonte == fonte for c in clipes):
            partes.append(f"Imagens: {rotulo}")
    return " · ".join(partes)


# ---------------------------------------------------------------- chaves

_indice_chave: dict[str, int] = {}


def _chaves(cfg: Config, fonte: str) -> list[str]:
    brutas = cfg["estoque"].get(f"{fonte}_chaves") or []
    return [str(x).strip() for x in brutas if str(x).strip()]


def _proxima_chave(cfg: Config, fonte: str) -> str:
    """Alterna entre as chaves: cada uma tem limite de uso por hora."""
    lista = _chaves(cfg, fonte)
    if not lista:
        raise ErroEstoque(
            f"falta a chave da API do {fonte.capitalize()}. Pegue uma de graça no site e informe em "
            "Configurações > Criação.", "config")
    with _TRAVA:
        n = _indice_chave.get(fonte, 0) % len(lista)
        _indice_chave[fonte] = n + 1
    return lista[n]


# ---------------------------------------------------------------- busca

def _pedir(url: str, cabecalhos: dict, parametros: dict, fonte: str) -> dict:
    try:
        r = requests.get(url, headers=cabecalhos, params=parametros, timeout=(10, 40))
    except requests.RequestException as e:
        raise ErroEstoque(f"não consegui falar com o {fonte} ({e.__class__.__name__})") from e
    if r.status_code in (401, 403):
        raise ErroEstoque(f"o {fonte} recusou a chave da API", "config")
    if r.status_code == 429:
        raise ErroEstoque(f"o {fonte} pediu para esperar (limite de uso da chave)")
    if r.status_code >= 400:
        raise ErroEstoque(f"o {fonte} respondeu HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise ErroEstoque(f"o {fonte} respondeu em formato inesperado") from e


def _buscar_pexels(cfg: Config, termo: str, vertical: bool, por_pagina: int) -> list[Clipe]:
    dados = _pedir(
        "https://api.pexels.com/videos/search",
        {"Authorization": _proxima_chave(cfg, "pexels"), "User-Agent": "autocortes/1.0"},
        {"query": termo, "per_page": por_pagina, "orientation": "portrait" if vertical else "landscape",
         "size": "medium"},
        "Pexels",
    )
    saida: list[Clipe] = []
    for v in dados.get("videos") or []:
        arquivos = [a for a in (v.get("video_files") or []) if str(a.get("file_type")) == "video/mp4" and a.get("link")]
        if not arquivos:
            continue
        # o maior que ainda não passa de 1080 de largura: qualidade suficiente para 1080x1920
        arquivos.sort(key=lambda a: (int(a.get("width") or 0) > 1200, -(int(a.get("width") or 0))))
        melhor = arquivos[0]
        saida.append(Clipe(
            fonte="pexels", id=str(v.get("id")), url=str(melhor.get("link")),
            largura=int(melhor.get("width") or 0), altura=int(melhor.get("height") or 0),
            duracao=float(v.get("duration") or 0), termo=termo,
            autor=str((v.get("user") or {}).get("name") or ""), pagina=str(v.get("url") or ""),
        ))
    return saida


def _buscar_pixabay(cfg: Config, termo: str, vertical: bool, por_pagina: int) -> list[Clipe]:
    dados = _pedir(
        "https://pixabay.com/api/videos/",
        {"User-Agent": "autocortes/1.0"},
        {"key": _proxima_chave(cfg, "pixabay"), "q": termo, "per_page": max(3, por_pagina),
         "video_type": "film", "safesearch": "true"},
        "Pixabay",
    )
    saida: list[Clipe] = []
    for v in dados.get("hits") or []:
        versoes = v.get("videos") or {}
        escolhida = None
        for nome in ("large", "medium", "small", "tiny"):
            atual = versoes.get(nome) or {}
            if atual.get("url"):
                escolhida = atual
                if int(atual.get("width") or 0) <= 1400:
                    break
        if not escolhida:
            continue
        largura, altura = int(escolhida.get("width") or 0), int(escolhida.get("height") or 0)
        if vertical and largura > altura:
            continue  # a API de vídeo não filtra orientação: filtro aqui
        saida.append(Clipe(
            fonte="pixabay", id=str(v.get("id")), url=str(escolhida.get("url")),
            largura=largura, altura=altura, duracao=float(v.get("duration") or 0), termo=termo,
            autor=str(v.get("user") or ""), pagina=str(v.get("pageURL") or ""),
        ))
    return saida


def buscar(cfg: Config, termo: str, vertical: bool = True, por_pagina: int = 12) -> list[Clipe]:
    """Clipes de um termo, na fonte configurada."""
    fonte = str(cfg["estoque"]["fonte"])
    termo = re.sub(r"\s+", " ", str(termo or "")).strip()
    if not termo:
        return []
    if fonte == "pexels":
        achados = _buscar_pexels(cfg, termo, vertical, por_pagina)
    elif fonte == "pixabay":
        achados = _buscar_pixabay(cfg, termo, vertical, por_pagina)
    elif fonte == "pasta":
        achados = _da_pasta(cfg, termo)
    else:
        raise ErroEstoque(f"fonte de material '{fonte}' desconhecida", "config")
    minimo = float(cfg["estoque"]["duracao_min_seg"])
    return [c for c in achados if c.imagem or c.duracao <= 0 or c.duracao >= minimo]


# ---------------------------------------------------------------- pasta do usuário

def pasta_material(cfg: Config) -> Path:
    bruto = str(cfg["estoque"].get("pasta") or "").strip()
    return cfg.caminho_de(bruto) if bruto else (cfg.raiz / "material")


def _da_pasta(cfg: Config, termo: str) -> list[Clipe]:
    """Arquivos seus. O termo casa com o nome do arquivo ou da subpasta; sem casar, vale tudo."""
    pasta = pasta_material(cfg)
    if not pasta.is_dir():
        raise ErroEstoque(
            f"a pasta de material não existe: {pasta}. Crie e ponha seus vídeos e imagens lá.", "config")
    alvo = slug(termo).replace("-", "")
    todos: list[Clipe] = []
    combinando: list[Clipe] = []
    for arquivo in sorted(pasta.rglob("*")):
        sufixo = arquivo.suffix.lower()
        if not arquivo.is_file() or sufixo not in (EXTENSOES_VIDEO | EXTENSOES_IMAGEM):
            continue
        clipe = Clipe(
            fonte="pasta", id=slug(str(arquivo.relative_to(pasta)), 60), caminho=arquivo,
            termo=termo, imagem=sufixo in EXTENSOES_IMAGEM,
        )
        todos.append(clipe)
        texto = slug(str(arquivo.relative_to(pasta))).replace("-", "")
        if alvo and alvo in texto:
            combinando.append(clipe)
    if not todos:
        raise ErroEstoque(f"não achei vídeo nem imagem em {pasta}", "config")
    escolhidos = combinando or todos
    random.shuffle(escolhidos)
    return escolhidos


# ---------------------------------------------------------------- download

def _arquivo_cache(cfg: Config, clipe: Clipe) -> Path:
    return pasta_cache(cfg) / f"{clipe.fonte}-{slug(clipe.id, 40)}.mp4"


def baixar(cfg: Config, clipe: Clipe) -> Path:
    """O arquivo do clipe no disco, baixando se preciso (o que já veio fica em cache)."""
    if clipe.caminho is not None and clipe.caminho.is_file():
        return clipe.caminho
    destino = _arquivo_cache(cfg, clipe)
    if destino.is_file() and destino.stat().st_size > 10_000:
        clipe.caminho = destino
        return destino
    destino.parent.mkdir(parents=True, exist_ok=True)
    parcial = destino.with_name(destino.name + ".parcial")
    try:
        with requests.get(clipe.url, stream=True, timeout=(10, 120),
                          headers={"User-Agent": "autocortes/1.0"}) as r:
            if r.status_code >= 400:
                raise ErroEstoque(f"o {clipe.fonte} devolveu HTTP {r.status_code} no download")
            tamanho = int(r.headers.get("Content-Length") or 0)
            if tamanho > LIMITE_BYTES:
                raise ErroEstoque(f"clipe grande demais ({tamanho // 1024**2} MB)", "conteudo")
            escritos = 0
            with open(parcial, "wb") as saida:
                for bloco in r.iter_content(chunk_size=1024 * 256):
                    if not bloco:
                        continue
                    escritos += len(bloco)
                    if escritos > LIMITE_BYTES:
                        raise ErroEstoque("clipe grande demais", "conteudo")
                    saida.write(bloco)
    except requests.RequestException as e:
        parcial.unlink(missing_ok=True)
        raise ErroEstoque(f"falha ao baixar o clipe ({e.__class__.__name__})") from e
    except ErroEstoque:
        parcial.unlink(missing_ok=True)
        raise
    parcial.replace(destino)
    clipe.caminho = destino
    if clipe.duracao <= 0:
        clipe.duracao = duracao_arquivo(cfg["ferramentas"]["ffprobe"], destino)
    if not clipe.largura or not clipe.altura:
        fluxo = next((s for s in sondar(cfg["ferramentas"]["ffprobe"], destino).get("streams", [])
                      if s.get("codec_type") == "video"), {})
        clipe.largura, clipe.altura = int(fluxo.get("width") or 0), int(fluxo.get("height") or 0)
    _guardar_credito(cfg, clipe)
    log.info("Clipe baixado (%s, %s): %s", clipe.fonte, clipe.termo, destino.name)
    return destino


# ---------------------------------------------------------------- seleção

def material(cfg: Config, termos: list[str], quantos: int, evitar_repetidos: bool = True) -> Material:
    """Escolhe e baixa `quantos` clipes para os termos, evitando o que já foi usado antes."""
    if quantos <= 0:
        return Material()
    termos = [t for t in (str(x).strip() for x in termos) if t]
    if not termos:
        raise ErroEstoque("não há termo de busca para o material", "conteudo")
    ja_usados = set(usados(cfg)) if evitar_repetidos else set()
    escolhidos: list[Clipe] = []
    vistos: set[str] = set()
    sobras: list[Clipe] = []

    for volta in range(2):  # 1ª volta só o que nunca foi usado; 2ª aceita repetir, se faltar
        for termo in termos:
            if len(escolhidos) >= quantos:
                break
            try:
                achados = buscar(cfg, termo)
            except ErroEstoque as e:
                if e.tipo == "config":
                    raise
                log.warning("Busca de material falhou para '%s': %s", termo, e)
                continue
            for clipe in achados:
                if len(escolhidos) >= quantos:
                    break
                if clipe.chave in vistos:
                    continue
                if volta == 0 and clipe.chave in ja_usados:
                    sobras.append(clipe)
                    continue
                vistos.add(clipe.chave)
                try:
                    baixar(cfg, clipe)
                except ErroEstoque as e:
                    log.warning("Clipe descartado: %s", e)
                    continue
                escolhidos.append(clipe)
        if len(escolhidos) >= quantos:
            break
        for clipe in sobras:  # segunda volta: material repetido é melhor que vídeo incompleto
            if len(escolhidos) >= quantos or clipe.chave in vistos:
                continue
            vistos.add(clipe.chave)
            try:
                baixar(cfg, clipe)
            except ErroEstoque:
                continue
            escolhidos.append(clipe)
        break

    if not escolhidos:
        raise ErroEstoque(f"não achei material para {', '.join(termos[:4])}", "conteudo")
    if len(escolhidos) < quantos:
        log.info("Material: achei %d de %d clipes; os que tenho vão se repetir no vídeo",
                 len(escolhidos), quantos)
    return Material(clipes=escolhidos, creditos=[c.para_json() for c in escolhidos])
