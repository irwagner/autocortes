"""Perfis: cada nicho com sua pasta, suas contas, seu tema e sua agenda.

Um perfil é uma pasta com um `config.toml` dentro. Como o `Config` resolve os caminhos
relativos a partir da pasta do próprio config, `pasta_dados = "dados"` e
`pasta_filmes = "filmes"` já deixam cada perfil com banco, contas das redes, perfil do
Chrome, cortes e vídeos separados, sem nada compartilhado por acidente.

    <instalação>/config.toml          o perfil principal (o que sempre existiu)
    <instalação>/perfis/<slug>/config.toml, dados/, filmes/

Cada perfil roda no seu próprio processo, com painel numa porta própria. O que é da
instalação inteira (quantos podem trabalhar ao mesmo tempo) fica no `perfis.toml`.
As ferramentas e os modelos do whisper continuam compartilhados: são da instalação.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg_mod
from .config import PLATAFORMAS, RAIZ, ErroConfig, carregar, criar_config_se_faltar
from .util import Trava, escrever_atomico, log, slug

__all__ = [
    "MAX_PERFIS", "Perfil", "atualizar", "autoiniciar_pendentes", "conflitos", "criar", "encerrar_pedido",
    "encontrar", "excluir", "identidade", "iniciar", "instalacao", "limpar_pedido", "listar", "marcar_porta",
    "parar", "pedido_de_encerrar", "salvar_instalacao", "situacao",
]

PASTA = RAIZ / "perfis"
ARQUIVO_INSTALACAO = RAIZ / "perfis.toml"
CONFIG_PRINCIPAL = RAIZ / "config.toml"
# arquivo que pede a um perfil que se encerre (o painel dele não aceita comando sem o token da janela)
NOME_PEDIDO = "encerrar.pedido"
# a porta em que o painel do perfil está aberto agora: trocar a porta no config de um perfil aberto
# só vale na próxima abertura, e sem isto ele sumia da tela (era procurado na porta nova)
NOME_PORTA = "painel.porta"
# pasta de perfil sendo excluída (renomeada antes de apagar: um arquivo em uso trava a troca de nome,
# e o perfil não fica apagado pela metade)
PREFIXO_EXCLUINDO = "_excluindo-"

PORTA_PAINEL_BASE = 8778
PORTA_NAVEGADOR_BASE = 9223
MAX_PERFIS = 20
INSTALACAO_PADRAO = {"max_simultaneos": 2}
# nomes de pasta que o painel usa para outra coisa
RESERVADOS = {"principal"}
# nome do perfil: sem os caracteres que não valem em nome de pasta no Windows
_NOME_VALIDO = re.compile(r'^[^\\/:*?"<>|\x00-\x1f]{1,60}$')


@dataclass
class Perfil:
    slug: str                    # "" no perfil principal
    nome: str
    config: Path
    principal: bool = False
    porta: int = 0
    porta_navegador: int = 0
    pasta: Path = field(default_factory=Path)        # pasta do perfil (onde fica o config)
    pasta_dados: Path = field(default_factory=Path)
    pasta_filmes: Path = field(default_factory=Path)
    autoiniciar: bool = False
    simulacao: bool = True
    redes: list[str] = field(default_factory=list)   # redes ativas
    modelo: str = ""                                 # modelo visual em uso
    erro: str = ""                                   # config com problema (o painel mostra e não deixa abrir)

    @property
    def id(self) -> str:
        return identidade(self.pasta_dados)

    @property
    def pasta_curta(self) -> str:
        """A pasta como o painel mostra: relativa à instalação, quando estiver dentro dela."""
        try:
            return str(self.pasta.resolve().relative_to(RAIZ.resolve())) or "."
        except (OSError, ValueError):
            return str(self.pasta)

    def para_painel(self) -> dict:
        return {
            "slug": self.slug,
            "nome": self.nome,
            "pasta_curta": self.pasta_curta,
            "principal": self.principal,
            "porta": self.porta,
            "porta_navegador": self.porta_navegador,
            "config": str(self.config),
            "pasta": str(self.pasta),
            "pasta_dados": str(self.pasta_dados),
            "pasta_filmes": str(self.pasta_filmes),
            "autoiniciar": self.autoiniciar,
            "simulacao": self.simulacao,
            "redes": list(self.redes),
            "modelo": self.modelo,
            "erro": self.erro,
            "url": f"http://127.0.0.1:{self.porta}/" if self.porta else "",
        }


def identidade(pasta_dados: Path) -> str:
    """Marca curta da pasta de dados: diz se quem respondeu na porta é este perfil mesmo."""
    try:
        alvo = str(pasta_dados.resolve()).lower()
    except OSError:
        alvo = str(pasta_dados).lower()
    return hashlib.sha256(alvo.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- opções da instalação

def instalacao() -> dict:
    """Opções que valem para a instalação inteira (o `perfis.toml`)."""
    dados = dict(INSTALACAO_PADRAO)
    if ARQUIVO_INSTALACAO.exists():
        try:
            bruto = tomllib.loads(ARQUIVO_INSTALACAO.read_text(encoding="utf-8-sig"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as e:
            log.warning("perfis.toml com problema (usando os padrões): %s", e)
            return dados
        secao = bruto.get("perfis")
        if isinstance(secao, dict):
            valor = secao.get("max_simultaneos")
            if isinstance(valor, int) and not isinstance(valor, bool) and 0 <= valor <= MAX_PERFIS:
                dados["max_simultaneos"] = valor
    return dados


def salvar_instalacao(alteracoes: dict) -> dict:
    """Grava o `perfis.toml` (só a chave conhecida; 0 = sem limite)."""
    import tomlkit

    dados = instalacao()
    if "max_simultaneos" in alteracoes:
        try:
            valor = int(alteracoes["max_simultaneos"])
        except (TypeError, ValueError) as e:
            raise ErroConfig("Quantos perfis ao mesmo tempo: informe um número inteiro") from e
        if not 0 <= valor <= MAX_PERFIS:
            raise ErroConfig(f"Quantos perfis ao mesmo tempo: use de 0 (sem limite) a {MAX_PERFIS}")
        dados["max_simultaneos"] = valor

    from tomlkit.exceptions import ParseError

    texto = ARQUIVO_INSTALACAO.read_text(encoding="utf-8-sig") if ARQUIVO_INSTALACAO.exists() else ""
    try:
        doc = tomlkit.parse(texto)
    except ParseError as e:
        raise ErroConfig(f"O perfis.toml tem um erro de sintaxe (foi editado à mão?): {e}") from e
    if "perfis" not in doc:
        doc.add(tomlkit.comment("AutoCortes - opções da instalação (valem para todos os perfis)"))
        doc.add(tomlkit.nl())
        tabela = tomlkit.table()
        tabela.comment("Quantos perfis podem trabalhar ao mesmo tempo (0 = sem limite)")
        doc["perfis"] = tabela
    doc["perfis"]["max_simultaneos"] = dados["max_simultaneos"]
    escrever_atomico(ARQUIVO_INSTALACAO, tomlkit.dumps(doc))
    return dados


# ---------------------------------------------------------------- leitura

def _nome_padrao(slug_: str, principal_: bool) -> str:
    if principal_:
        return "Principal"
    return slug_.replace("-", " ").strip().title() or "Perfil"


def _ler(caminho: Path, slug_: str, principal_: bool) -> Perfil:
    pasta = caminho.parent
    try:
        cfg = carregar(caminho)
    except ErroConfig as e:
        return Perfil(
            slug=slug_, nome=_nome_padrao(slug_, principal_), config=caminho, principal=principal_,
            pasta=pasta, pasta_dados=pasta / "dados", pasta_filmes=pasta / "filmes",
            erro=str(e).split("\n")[0],
        )
    nome = str(cfg["geral"].get("perfil") or "").strip() or _nome_padrao(slug_, principal_)
    return Perfil(
        slug=slug_,
        nome=nome,
        config=caminho,
        principal=principal_,
        porta=int(cfg["painel"]["porta"]),
        porta_navegador=int(cfg["navegador"]["porta"]),
        pasta=pasta,
        pasta_dados=cfg.pasta_dados,
        pasta_filmes=cfg.pasta_filmes,
        autoiniciar=bool(cfg["geral"].get("autoiniciar")),
        simulacao=cfg.simulacao,
        redes=cfg.plataformas_ativas(),
        modelo=str(cfg["edicao"]["modelo"]),
    )


def listar() -> list[Perfil]:
    """O perfil principal (config.toml da instalação) e cada pasta de `perfis/`."""
    saida: list[Perfil] = []
    if CONFIG_PRINCIPAL.exists():
        saida.append(_ler(CONFIG_PRINCIPAL, "", True))
    if PASTA.is_dir():
        for pasta in sorted(PASTA.iterdir(), key=lambda p: p.name.lower()):
            if pasta.name.startswith(PREFIXO_EXCLUINDO):
                continue  # sobra de uma exclusão que não terminou de apagar
            alvo = pasta / "config.toml"
            if pasta.is_dir() and alvo.is_file():
                saida.append(_ler(alvo, pasta.name, False))
    return saida


def encontrar(slug_: str) -> Perfil:
    alvo = (slug_ or "").strip()
    for p in listar():
        if p.slug == alvo:
            return p
    raise ErroConfig(f"Perfil '{alvo}' não encontrado")


def conflitos(perfis: list[Perfil] | None = None) -> list[str]:
    """Portas repetidas entre perfis: o segundo painel não sobe, e o navegador é pior."""
    perfis = perfis if perfis is not None else listar()
    avisos: list[str] = []
    for rotulo, chave, extra in (
        ("do painel", "porta", "o segundo perfil não abre"),
        ("do navegador", "porta_navegador", "os dois postariam pela mesma janela do Chrome, com as contas trocadas"),
    ):
        vistos: dict[int, str] = {}
        for p in perfis:
            valor = getattr(p, chave)
            if not valor:
                continue
            if valor in vistos:
                avisos.append(
                    f"A porta {rotulo} ({valor}) está repetida em '{vistos[valor]}' e '{p.nome}': {extra}."
                )
            else:
                vistos[valor] = p.nome
    return avisos


# ---------------------------------------------------------------- situação (rodando ou não)

def _escutando(porta: int, espera: float = 0.4) -> bool:
    """Alguém está ouvindo nesta porta? (o urlopen numa porta fechada leva ~2 s no Windows)"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(espera)
        try:
            return s.connect_ex(("127.0.0.1", porta)) == 0
        except OSError:
            return False


def _saude(porta: int) -> dict | None:
    if not _escutando(porta):
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{porta}/saude", timeout=3) as r:
            dados = json.loads(r.read().decode("utf-8"))
        return dados if isinstance(dados, dict) and dados.get("app") == "autocortes" else None
    except (OSError, ValueError):
        return None


def marcar_porta(pasta_dados: Path, porta: int | None) -> None:
    """O painel anota a porta em que abriu (None apaga a anotação, ao fechar)."""
    alvo = pasta_dados / NOME_PORTA
    try:
        if porta is None:
            alvo.unlink(missing_ok=True)
        else:
            escrever_atomico(alvo, str(int(porta)))
    except OSError as e:
        log.debug("Não consegui anotar a porta do painel: %s", e)


def _porta_anotada(p: Perfil) -> int:
    try:
        return int((p.pasta_dados / NOME_PORTA).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def _travado(pasta_dados: Path) -> bool:
    """Alguém segura a trava desta pasta de dados: o painel, o modo terminal ou um perfil fechando."""
    alvo = pasta_dados / "autocortes.lock"
    if not alvo.is_file():
        return False
    trava = Trava(alvo)
    if trava.adquirir():
        trava.liberar()
        return False
    return True


def _chrome_aberto(pasta_dados: Path) -> bool:
    """A janela do Chrome deste perfil está aberta (ele segura o lockfile da pasta dele)."""
    alvo = pasta_dados / "chrome" / "lockfile"
    if not alvo.is_file():
        return False
    try:
        with open(alvo, "ab"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def situacao(p: Perfil) -> dict:
    """Se o perfil está aberto agora. Compara a marca da pasta de dados, não só a porta.

    Procura na porta do config e na porta anotada pelo painel quando abriu (que é outra se você
    trocou a porta com ele aberto). Aberto sem painel (terminal, ou fechando) vem como "ocupado".
    """
    portas = list(dict.fromkeys(x for x in (p.porta, _porta_anotada(p)) if x))
    estranho = None
    for porta in portas:
        saude = _saude(porta)
        if saude is None:
            continue
        if str(saude.get("id") or "") == p.id:
            return {"rodando": True, "outro": False, "desde": saude.get("desde"), "motor": saude.get("motor"),
                    "porta": porta}
        if porta == p.porta:
            estranho = saude
    if estranho is not None:  # alguém responde na porta do config, mas com outra pasta de dados
        return {"rodando": False, "outro": True, "perfil_na_porta": str(estranho.get("perfil") or "outro perfil"),
                "ocupado": _travado(p.pasta_dados)}
    return {"rodando": False, "outro": False, "ocupado": _travado(p.pasta_dados)}


def situacoes(perfis: list[Perfil]) -> dict[str, dict]:
    """A situação de vários perfis ao mesmo tempo (uma consulta por perfil, em paralelo)."""
    if not perfis:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(perfis))) as executor:
        return dict(zip([p.slug for p in perfis], executor.map(situacao, perfis)))


def abertos(perfis: list[Perfil] | None = None) -> int:
    lista = perfis if perfis is not None else listar()
    return sum(1 for s in situacoes(lista).values() if s["rodando"])


def porta_ocupada(porta: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        try:
            s.bind(("127.0.0.1", porta))
        except OSError:
            return True
    return False


def _porta_livre(base: int, usadas: set[int]) -> int:
    porta = base
    while porta < 65535:
        if porta not in usadas and not porta_ocupada(porta):
            return porta
        porta += 1
    raise ErroConfig("não encontrei uma porta livre para o perfil novo")


# ---------------------------------------------------------------- criar, alterar e excluir

def _slug_livre(nome: str) -> str:
    base = slug(nome, 40) or "perfil"
    if base in RESERVADOS:  # "principal" é o apelido do config.toml da instalação nas rotas do painel
        base = f"{base}-2"
    usados = {p.slug for p in listar()}
    if base not in usados and not (PASTA / base).exists():
        return base
    for n in range(2, 100):
        tentativa = f"{base}-{n}"
        if tentativa not in usados and not (PASTA / tentativa).exists():
            return tentativa
    raise ErroConfig("não consegui achar um nome de pasta livre para o perfil")


def criar(nome: str) -> Perfil:
    """Cria a pasta do perfil com config próprio, pasta de filmes e as redes desligadas."""
    nome = str(nome or "").strip()
    if not _NOME_VALIDO.match(nome):
        raise ErroConfig('O nome do perfil deve ter de 1 a 60 caracteres, sem \\ / : * ? " < > |')
    existentes = listar()
    if len(existentes) >= MAX_PERFIS:
        raise ErroConfig(f"O limite é de {MAX_PERFIS} perfis")
    if any(p.nome.strip().lower() == nome.lower() for p in existentes):
        raise ErroConfig(f"Já existe um perfil chamado '{nome}'")

    slug_ = _slug_livre(nome)
    pasta = PASTA / slug_
    usadas = {p.porta for p in existentes} | {p.porta_navegador for p in existentes}
    porta = _porta_livre(PORTA_PAINEL_BASE, usadas)
    porta_nav = _porta_livre(PORTA_NAVEGADOR_BASE, usadas | {porta})

    pasta.mkdir(parents=True, exist_ok=True)
    alvo = pasta / "config.toml"
    try:
        if not criar_config_se_faltar(alvo):
            raise ErroConfig(f"Já existe um config.toml em {pasta}")
        cfg = carregar(alvo)
        # perfil novo começa em simulação, sem rede nenhuma ligada: as contas ainda não existem
        alteracoes: dict = {
            "geral": {"perfil": nome, "autoiniciar": False, "simulacao": True,
                      "pasta_dados": "dados", "pasta_filmes": "filmes"},
            "painel": {"porta": porta},
            "navegador": {"porta": porta_nav},
        }
        for rede in PLATAFORMAS:
            alteracoes[rede] = {"ativo": False}
        cfg_mod.salvar(cfg, alteracoes)
        (pasta / "filmes").mkdir(exist_ok=True)
        (pasta / "dados").mkdir(exist_ok=True)
    except Exception:
        # não deixa uma pasta pela metade atrapalhando a próxima tentativa
        if not any(pasta.iterdir()) or not alvo.exists():
            shutil.rmtree(pasta, ignore_errors=True)
        raise
    log.info("Perfil '%s' criado em %s (painel na porta %d)", nome, pasta, porta)
    return _ler(alvo, slug_, False)


def atualizar(p: Perfil, alteracoes: dict) -> Perfil:
    """Muda o nome, o autoiniciar e as portas de um perfil (grava no config dele)."""
    cfg = carregar(p.config)
    limpo: dict = {"geral": {}, "painel": {}, "navegador": {}}
    if "nome" in alteracoes:
        nome = str(alteracoes["nome"] or "").strip()
        if not _NOME_VALIDO.match(nome):
            raise ErroConfig('O nome do perfil deve ter de 1 a 60 caracteres, sem \\ / : * ? " < > |')
        if any(o.nome.strip().lower() == nome.lower() and o.slug != p.slug for o in listar()):
            raise ErroConfig(f"Já existe um perfil chamado '{nome}'")
        limpo["geral"]["perfil"] = nome
    if "autoiniciar" in alteracoes:
        limpo["geral"]["autoiniciar"] = bool(alteracoes["autoiniciar"])
    for chave, secao in (("porta", "painel"), ("porta_navegador", "navegador")):
        if chave not in alteracoes:
            continue
        try:
            valor = int(alteracoes[chave])
        except (TypeError, ValueError) as e:
            raise ErroConfig("A porta deve ser um número") from e
        outros = [o for o in listar() if o.slug != p.slug]
        if any(valor in (o.porta, o.porta_navegador) for o in outros):
            raise ErroConfig(f"A porta {valor} já é usada por outro perfil")
        limpo[secao]["porta"] = valor
    limpo = {k: v for k, v in limpo.items() if v}
    if not limpo:
        return p
    cfg_mod.salvar(cfg, limpo)  # valida (inclusive porta do painel != porta do navegador)
    return _ler(p.config, p.slug, p.principal)


def conteudo(p: Perfil) -> dict:
    """O que existe dentro do perfil, para a confirmação antes de excluir."""
    from .produtor import EXTENSOES

    def contar(pasta: Path, exts: set[str] | None = None) -> int:
        if not pasta.is_dir():
            return 0
        if exts is None:
            return sum(1 for x in pasta.rglob("*") if x.is_file())
        return sum(1 for x in pasta.rglob("*") if x.is_file() and x.suffix.lower() in exts)

    return {
        "filmes": contar(p.pasta_filmes, EXTENSOES),
        "cortes": contar(p.pasta_dados / "cortes", {".mp4"}),
        "tem_contas": (p.pasta_dados / "tokens").is_dir(),
    }


def excluir(slug_: str) -> dict:
    """Apaga a pasta do perfil inteira (o painel confirma antes, pedindo o nome).

    Antes de apagar, confere que nada do perfil está aberto (painel, terminal, perfil fechando,
    janela do Chrome dele) e troca o nome da pasta: no Windows, um arquivo em uso impede a troca,
    e aí nada é apagado. Sem isso, o apagar parava no meio e o perfil ficava pela metade.
    """
    p = encontrar(slug_)
    if p.principal:
        raise ErroConfig("O perfil principal não pode ser excluído")
    estado = situacao(p)
    if estado["rodando"] or estado.get("ocupado"):
        raise ErroConfig(f"Feche o perfil '{p.nome}' antes de excluir (ele está aberto no painel, no terminal "
                         "ou ainda fechando)")
    if _chrome_aberto(p.pasta_dados):
        raise ErroConfig(f"A janela do navegador do perfil '{p.nome}' está aberta: feche-a antes de excluir")
    resumo = conteudo(p)
    lixeira = p.pasta.with_name(f"{PREFIXO_EXCLUINDO}{p.slug}-{int(time.time())}")
    try:
        p.pasta.rename(lixeira)
    except OSError as e:
        raise ErroConfig(f"Não consegui excluir: algum arquivo do perfil está aberto em outro programa ({e}). "
                         f"Feche o que estiver usando {p.pasta} e tente de novo. Nada foi apagado.") from e
    try:
        shutil.rmtree(lixeira)
    except OSError as e:
        log.warning("O perfil '%s' saiu da lista, mas sobrou a pasta %s para apagar à mão (%s)", p.nome, lixeira, e)
    log.warning("Perfil '%s' excluído (%s)", p.nome, p.pasta)
    return resumo


# ---------------------------------------------------------------- abrir e fechar

def _executavel() -> Path:
    """O pythonw da .venv (sem janela preta), ou o Python que está rodando."""
    for candidato in (RAIZ / ".venv" / "Scripts" / "pythonw.exe", Path(sys.executable).with_name("pythonw.exe")):
        if candidato.exists():
            return candidato
    return Path(sys.executable)


def iniciar(p: Perfil) -> dict:
    """Abre o perfil num processo próprio (painel na porta dele, sem abrir o navegador)."""
    if p.erro:
        raise ErroConfig(f"O config do perfil '{p.nome}' tem um problema: {p.erro}")
    estado = situacao(p)
    if estado["rodando"]:
        return {"iniciado": False, "motivo": "já estava aberto",
                "url": f"http://127.0.0.1:{estado.get('porta') or p.porta}/"}
    if estado.get("ocupado"):
        raise ErroConfig(f"O perfil '{p.nome}' já está aberto no terminal (ou ainda fechando). "
                         "Espere fechar e tente de novo.")
    if estado["outro"]:
        raise ErroConfig(
            f"A porta {p.porta} já está ocupada por {estado['perfil_na_porta']}. "
            "Troque a porta deste perfil antes de abrir."
        )
    limite = instalacao()["max_simultaneos"]
    if limite:
        quantos = abertos()
        if quantos >= limite:
            raise ErroConfig(
                f"Já são {quantos} perfis trabalhando ao mesmo tempo, o limite desta instalação. "
                "Feche um perfil ou aumente o limite em Perfis."
            )
    limpar_pedido(p)
    argumentos = [str(_executavel()), "-m", "autocortes", "--config", str(p.config), "painel", "--sem-navegador"]
    try:
        subprocess.Popen(
            argumentos, cwd=str(RAIZ), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except OSError as e:
        raise ErroConfig(f"Não consegui abrir o perfil '{p.nome}': {e}") from e
    log.info("Abrindo o perfil '%s' (porta %d)", p.nome, p.porta)
    limite_espera = time.monotonic() + 40
    while time.monotonic() < limite_espera:
        time.sleep(0.5)
        agora = situacao(p)
        if agora["rodando"]:
            return {"iniciado": True, "url": f"http://127.0.0.1:{agora.get('porta') or p.porta}/"}
    raise ErroConfig(
        f"O perfil '{p.nome}' não respondeu na porta {p.porta}. Veja o registro em {p.pasta_dados / 'logs'}."
    )


def parar(p: Perfil) -> dict:
    """Pede ao perfil que se encerre (ele fecha o painel e o motor do jeito normal)."""
    if not situacao(p)["rodando"]:
        return {"parado": False, "motivo": "não estava aberto"}
    encerrar_pedido(p)
    log.info("Pedi para o perfil '%s' fechar", p.nome)
    limite = time.monotonic() + 40
    while time.monotonic() < limite:
        time.sleep(0.5)
        if not situacao(p)["rodando"]:
            return {"parado": True}
    limpar_pedido(p)
    raise ErroConfig(f"O perfil '{p.nome}' não fechou a tempo. Veja o registro dele.")


def encerrar_pedido(p: Perfil) -> None:
    p.pasta_dados.mkdir(parents=True, exist_ok=True)
    (p.pasta_dados / NOME_PEDIDO).write_text(str(time.time()), encoding="utf-8")


def limpar_pedido(p: Perfil) -> None:
    (p.pasta_dados / NOME_PEDIDO).unlink(missing_ok=True)


def pedido_de_encerrar(pasta_dados: Path) -> bool:
    """Usado pelo próprio painel: alguém pediu para este perfil fechar?"""
    return (pasta_dados / NOME_PEDIDO).exists()


def autoiniciar_pendentes(caminho_config: Path) -> list[str]:
    """Abre os perfis marcados como autoiniciar. Só o principal faz isso (senão viram um laço)."""
    try:
        if caminho_config.resolve() != CONFIG_PRINCIPAL.resolve():
            return []
    except OSError:
        return []
    todos = listar()
    marcados = [p for p in todos if not p.principal and p.autoiniciar and not p.erro]
    if not marcados:
        return []
    limite = instalacao()["max_simultaneos"]
    estados = situacoes(todos)
    quantos = sum(1 for s in estados.values() if s["rodando"])
    iniciados: list[str] = []
    for p in marcados:
        if limite and quantos >= limite:
            log.info("Não abri o perfil '%s': já são %d trabalhando (limite da instalação)", p.nome, quantos)
            continue
        if estados.get(p.slug, {}).get("rodando"):
            continue
        try:
            iniciar(p)
            iniciados.append(p.nome)
            quantos += 1
        except ErroConfig as e:
            log.warning("Não consegui abrir o perfil '%s': %s", p.nome, e)
    return iniciados
