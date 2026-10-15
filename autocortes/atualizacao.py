"""Atualização pelo GitHub: confere a cada N dias e, com tudo parado, atualiza e abre de novo.

A conferência roda no perfil principal (como o "abrir junto" dos perfis), e as opções são da
instalação inteira (`perfis.toml`, [atualizacao]). Há dois jeitos de instalar, e os dois atualizam:

- clone do git (o README manda clonar): `git fetch` e `git merge --ff-only`. Só avança se nenhum
  arquivo do AutoCortes foi mudado nesta pasta e se não há commit daqui que falte no GitHub: nada
  do que você mexeu é sobrescrito, e quem desenvolve na pasta nunca é atualizado por cima.
- pasta baixada em ZIP: baixa o ZIP do commit novo e troca só os arquivos que vieram nele (os seus,
  como config.toml, dados/, perfis/, filmes/ e .venv/, não estão no ZIP e não são tocados).

A troca é com tudo fechado: o painel anota quem estava aberto, pede para todos fecharem e passa
a vez para `python -m autocortes atualizar --aplicar --depois-de-fechar`, que espera as travas
soltarem, troca os arquivos, instala as bibliotecas se o requirements.txt mudou e abre o
principal de novo, que reabre os perfis anotados. Se a versão nova não abrir, a anterior volta.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

from . import perfis
from .config import RAIZ, ErroConfig
from .util import ATIVIDADES, ler_json, log, salvar_json

REPOSITORIO = "irwagner/autocortes"
RAMO = "main"
ARQUIVO_ESTADO = RAIZ / ".atualizacao.json"
# o ZIP baixado e a cópia do que foi trocado (só na instalação por ZIP)
PASTA_TRABALHO = RAIZ / ".atualizacao"
# o que uma atualização por ZIP nunca escreve nem apaga (o que é seu, e o que o git não guarda)
PROTEGIDOS = {
    ".git", ".venv", "config.toml", "dados", "perfis", "perfis.toml", "pautas", "material", "musicas", "modelos",
    "ferramentas", "filmes", ".atualizacao", ".atualizacao.json", ".playwright-cli", "template.png",
}
# a troca espera: ninguém mexeu no painel neste tempo, e nada está editando, analisando, postando
# ou baixando, em nenhum perfil
SEM_USO_SEG = 15 * 60
ESPERA_FECHAR_SEG = 10 * 60
ESPERA_ABRIR_SEG = 120
_CRIAR_SEM_JANELA = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ErroAtualizacao(Exception):
    pass


# ---------------------------------------------------------------- estado

def ler_estado() -> dict:
    dados = ler_json(ARQUIVO_ESTADO, {})
    return dados if isinstance(dados, dict) else {}


def gravar_estado(estado: dict) -> None:
    salvar_json(ARQUIVO_ESTADO, estado)


# no principal, três threads mexem no estado (a que confere, a que reabre os perfis e o painel):
# ler, mudar e gravar sem a trava perdia a mudança da outra (o "reabrir" voltava depois de usado)
_TRAVA_ESTADO = threading.RLock()


@contextmanager
def _mudar_estado():
    """Lê o estado, deixa mudar e grava, com a trava. Não segure durante uma consulta à internet."""
    with _TRAVA_ESTADO:
        estado = ler_estado()
        yield estado
        gravar_estado(estado)


def modo() -> str:
    """git (clone com o programa git), zip (pasta baixada do GitHub) ou sem_git (clone sem o programa)."""
    if (RAIZ / ".git").exists():
        return "git" if shutil.which("git") else "sem_git"
    return "zip"


# ---------------------------------------------------------------- git

def _git(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    # nunca pergunta nada (usuário, senha, chave): numa thread sem janela, a pergunta travaria tudo
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    try:
        return subprocess.run(["git", "-C", str(RAIZ), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env, stdin=subprocess.DEVNULL,
                              creationflags=_CRIAR_SEM_JANELA)
    except (OSError, subprocess.SubprocessError) as e:
        raise ErroAtualizacao(f"não consegui rodar o git: {e}") from e


def _git_ok(*args: str, timeout: float = 120) -> str:
    r = _git(*args, timeout=timeout)
    if r.returncode != 0:
        detalhe = " ".join((r.stderr or r.stdout or "").split())[-300:]
        raise ErroAtualizacao(f"o git {args[0]} falhou: {detalhe or f'código {r.returncode}'}")
    return r.stdout.strip()


def _commit_git(ref: str) -> dict:
    sha, data, mensagem = (_git_ok("log", "-1", "--format=%H%n%cI%n%s", ref).splitlines() + ["", "", ""])[:3]
    return {"sha": sha, "data": data, "mensagem": mensagem[:200]}


def _mudados_git() -> list[str]:
    return [x for x in _git_ok("status", "--porcelain", "--untracked-files=no").splitlines() if x.strip()]


def _verificar_git() -> dict:
    upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").stdout.strip()
    if upstream != f"origin/{RAMO}":
        ramo = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "?"
        return {"disponivel": False, "local": _commit_git("HEAD"), "remoto": None,
                "motivo": f"esta pasta está no ramo '{ramo}', que não acompanha o origin/{RAMO}: atualize à mão"}
    _git_ok("fetch", "--quiet", "origin", RAMO, timeout=120)  # o "Procurar agora" do painel espera por isto
    local, remoto = _commit_git("HEAD"), _commit_git(f"origin/{RAMO}")
    atras = int(_git_ok("rev-list", "--count", f"HEAD..origin/{RAMO}") or 0)
    frente = int(_git_ok("rev-list", "--count", f"origin/{RAMO}..HEAD") or 0)
    base = {"local": local, "remoto": remoto, "atras": atras, "frente": frente, "motivo": None}
    if not atras:
        return {**base, "disponivel": False}
    if frente:
        return {**base, "disponivel": False,
                "motivo": f"esta pasta tem {frente} commit(s) que não estão no GitHub: atualize à mão (git pull)"}
    mudados = _mudados_git()
    if mudados:
        return {**base, "disponivel": False,
                "motivo": f"há {len(mudados)} arquivo(s) do AutoCortes mudados nesta pasta: atualize à mão, "
                          "para não perder o que você mexeu"}
    return {**base, "disponivel": True}


# ---------------------------------------------------------------- ZIP

def _commit_remoto() -> dict:
    import requests

    try:
        r = requests.get(f"https://api.github.com/repos/{REPOSITORIO}/commits/{RAMO}", timeout=30,
                         headers={"Accept": "application/vnd.github+json", "User-Agent": "autocortes"})
    except requests.RequestException as e:
        raise ErroAtualizacao(f"não consegui falar com o GitHub: {e}") from e
    if r.status_code in (403, 429):
        raise ErroAtualizacao("o GitHub limitou as consultas agora; confiro de novo mais tarde")
    if r.status_code != 200:
        raise ErroAtualizacao(f"o GitHub respondeu {r.status_code} ao pedir a versão nova")
    dados = r.json()
    return {"sha": dados["sha"], "data": dados["commit"]["committer"]["date"],
            "mensagem": str(dados["commit"]["message"]).splitlines()[0][:200]}


def _baixar_zip(sha: str) -> Path:
    from .ferramentas import ErroDownload, baixar

    destino = PASTA_TRABALHO / f"{sha}.zip"
    if destino.is_file():
        try:
            with zipfile.ZipFile(destino) as z:
                if z.testzip() is None:
                    return destino
        except zipfile.BadZipFile:
            pass
        destino.unlink(missing_ok=True)
    for antigo in PASTA_TRABALHO.glob("*.zip") if PASTA_TRABALHO.is_dir() else []:
        antigo.unlink(missing_ok=True)  # o de uma versão que já passou
    try:
        # o registro diz "Baixando <rótulo> ..." e "<rótulo> pronto": masculino
        baixar(f"https://codeload.github.com/{REPOSITORIO}/zip/{sha}", destino, "o ZIP da versão nova do AutoCortes")
    except ErroDownload as e:
        raise ErroAtualizacao(str(e)) from e
    finally:
        ATIVIDADES.limpar()  # o "Baixando..." não fica no painel depois
    try:
        with zipfile.ZipFile(destino) as z:
            if z.testzip() is not None:
                raise zipfile.BadZipFile("arquivo com defeito")
    except zipfile.BadZipFile as e:
        destino.unlink(missing_ok=True)
        raise ErroAtualizacao(f"o ZIP da versão nova veio com defeito: {e}") from e
    return destino


def _protegido(rel: str) -> bool:
    partes = rel.split("/")
    if partes[0] in PROTEGIDOS or "__pycache__" in partes:
        return True
    # as imagens das molduras são suas (o repositório guarda só a pasta)
    return partes[0] == "molduras" and rel.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))


def _arquivos_zip(z: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """Os arquivos do ZIP pelo caminho dentro da instalação (sem a pasta de cima, autocortes-<sha>/)."""
    saida: dict[str, zipfile.ZipInfo] = {}
    for info in z.infolist():
        if info.is_dir():
            continue
        partes = info.filename.replace("\\", "/").split("/")[1:]
        if not partes or any(p in ("", ".", "..") or ":" in p for p in partes):
            continue  # nada fora da instalação (nem caminho absoluto, nem ..)
        rel = "/".join(partes)
        if not _protegido(rel):
            saida[rel] = info
    return saida


def _igual(caminho: Path, conteudo: bytes) -> bool:
    try:
        return caminho.is_file() and caminho.stat().st_size == len(conteudo) and caminho.read_bytes() == conteudo
    except OSError:
        return False


def _mudancas_zip(z: zipfile.ZipFile, arquivos: dict[str, zipfile.ZipInfo], estado: dict) -> dict:
    trocar = [rel for rel, info in arquivos.items() if not _igual(RAIZ / rel, z.read(info))]
    # apagar só o que veio de uma atualização anterior e saiu do repositório (nunca um arquivo seu)
    anteriores = {str(x) for x in (estado.get("arquivos") or [])}
    apagar = sorted(rel for rel in anteriores - set(arquivos) if not _protegido(rel) and (RAIZ / rel).is_file())
    return {"trocar": sorted(trocar), "apagar": apagar}


def _verificar_zip(estado: dict) -> dict:
    remoto = _commit_remoto()
    local = estado.get("local") if isinstance(estado.get("local"), dict) else None
    if local and local.get("sha") == remoto["sha"]:
        return {"local": local, "remoto": remoto, "disponivel": False, "motivo": None}
    pacote = _baixar_zip(remoto["sha"])
    with zipfile.ZipFile(pacote) as z:
        arquivos = _arquivos_zip(z)
        mudancas = _mudancas_zip(z, arquivos, estado)
    if not mudancas["trocar"] and not mudancas["apagar"]:
        # a pasta já é igual à do GitHub (a primeira conferência depois de baixar o ZIP)
        return {"local": remoto, "remoto": remoto, "disponivel": False, "motivo": None, "arquivos": sorted(arquivos)}
    return {"local": local, "remoto": remoto, "disponivel": True, "motivo": None,
            "mudam": len(mudancas["trocar"]) + len(mudancas["apagar"])}


# ---------------------------------------------------------------- conferir

def verificar() -> dict:
    """Confere no GitHub se há versão nova e anota o resultado. Não troca nada."""
    m = modo()
    resultado = {"verificado_em": time.time(), "erro": None, "motivo": None, "modo": m}
    try:  # a consulta fica fora da trava: o git fetch pode levar um tempo
        if m == "git":
            resultado.update(_verificar_git())
        elif m == "zip":
            resultado.update(_verificar_zip(ler_estado()))
        else:
            resultado.update({"disponivel": False, "motivo": "esta pasta é um clone do git, mas o programa git não "
                              "está instalado: instale o git (git-scm.com) para eu atualizar sozinho"})
    except (ErroAtualizacao, OSError, ValueError, KeyError) as e:
        resultado.update({"disponivel": False, "erro": str(e)})
        log.warning("Atualização: não consegui conferir (%s)", e)
    with _mudar_estado() as estado:
        estado.update(resultado)
        atual = dict(estado)
    if atual.get("disponivel"):
        remoto = atual.get("remoto") or {}
        log.info("Atualização disponível no GitHub: %s (%s)", str(remoto.get("sha", ""))[:7], remoto.get("mensagem", ""))
    return atual


def situacao(principal: bool) -> dict:
    """O que o painel mostra em Configurações > Sistema."""
    estado = ler_estado()
    opcoes = perfis.instalacao()
    m = modo()
    local = estado.get("local") if isinstance(estado.get("local"), dict) else None
    if m == "git":
        try:
            local = _commit_git("HEAD")  # o de agora (você pode ter atualizado à mão)
        except ErroAtualizacao:
            pass
    verificado = estado.get("verificado_em")
    proxima = (float(verificado) + opcoes["atualizar_a_cada_dias"] * 86400
               if verificado and opcoes["atualizar_sozinho"] else None)
    return {
        "modo": m, "repositorio": REPOSITORIO, "ramo": RAMO, "local": local, "remoto": estado.get("remoto"),
        "disponivel": bool(estado.get("disponivel")) and not (local and (estado.get("remoto") or {}).get("sha") == local.get("sha")),
        "motivo": estado.get("motivo"), "erro": estado.get("erro"), "verificado_em": verificado, "proxima": proxima,
        "aplicado": estado.get("aplicado"), "aplicado_em": estado.get("aplicado_em"),
        # a versão nova já falhou aqui (a automática espera a próxima conferência; o botão tenta)
        "falhou": estado.get("falhou") if tentada_ha_pouco(estado, opcoes["atualizar_a_cada_dias"]) else None,
        "opcoes": {"atualizar_sozinho": opcoes["atualizar_sozinho"], "atualizar_a_cada_dias": opcoes["atualizar_a_cada_dias"]},
        "principal": principal,
    }


# ---------------------------------------------------------------- aplicar (com tudo fechado)

def _aplicar_git() -> dict:
    antes = _commit_git("HEAD")
    _git_ok("fetch", "--quiet", "origin", RAMO, timeout=300)
    if _mudados_git():
        raise ErroAtualizacao("há arquivos do AutoCortes mudados nesta pasta; não atualizo por cima")
    _git_ok("merge", "--ff-only", "--quiet", f"origin/{RAMO}")
    return {"de": antes, "para": _commit_git("HEAD"), "volta": {"modo": "git", "sha": antes["sha"]}}


def _aplicar_zip(estado: dict) -> dict:
    remoto = _commit_remoto()
    pacote = _baixar_zip(remoto["sha"])
    copia = PASTA_TRABALHO / "anterior"
    shutil.rmtree(copia, ignore_errors=True)
    novos: list[str] = []
    with zipfile.ZipFile(pacote) as z:
        arquivos = _arquivos_zip(z)
        mudancas = _mudancas_zip(z, arquivos, estado)
        for rel in mudancas["trocar"] + mudancas["apagar"]:  # a cópia de segurança antes de mexer
            alvo = RAIZ / rel
            if alvo.is_file():
                (copia / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(alvo, copia / rel)
            else:
                novos.append(rel)
        for rel in mudancas["trocar"]:
            alvo = RAIZ / rel
            alvo.parent.mkdir(parents=True, exist_ok=True)
            temporario = alvo.with_name(alvo.name + ".atualizando")
            temporario.write_bytes(z.read(arquivos[rel]))
            os.replace(temporario, alvo)
        for rel in mudancas["apagar"]:
            (RAIZ / rel).unlink(missing_ok=True)
    volta = {"modo": "zip", "copia": str(copia), "novos": novos, "local": estado.get("local"),
             "arquivos": estado.get("arquivos")}
    with _mudar_estado() as atual:
        atual.update({"local": remoto, "arquivos": sorted(arquivos)})
    log.info("Atualização: %d arquivo(s) trocados e %d apagados", len(mudancas["trocar"]), len(mudancas["apagar"]))
    return {"de": volta["local"], "para": remoto, "volta": volta}


def desfazer(volta: dict) -> None:
    """Volta para a versão de antes (a nova não abriu, ou as bibliotecas não instalaram)."""
    if volta.get("modo") == "git":
        _git_ok("reset", "--keep", "--quiet", volta["sha"])
        return
    copia = Path(volta["copia"])
    for rel in volta.get("novos") or []:
        (RAIZ / rel).unlink(missing_ok=True)
    if copia.is_dir():
        for origem in copia.rglob("*"):
            if origem.is_file():
                alvo = RAIZ / origem.relative_to(copia)
                alvo.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origem, alvo)
    with _mudar_estado() as estado:
        estado.update({"local": volta.get("local"), "arquivos": volta.get("arquivos")})


def _requisitos() -> bytes | None:
    try:
        return (RAIZ / "requirements.txt").read_bytes()
    except OSError:
        return None


def _python_da_venv() -> Path:
    candidato = RAIZ / ".venv" / "Scripts" / "python.exe"
    return candidato if candidato.exists() else Path(sys.executable)  # o Python que roda o AutoCortes


def _bibliotecas(antes: bytes | None) -> None:
    """O que o AutoCortes.bat faria: instala as bibliotecas, só quando esta atualização mudou o requirements.txt."""
    agora = _requisitos()
    if agora is None or agora == antes:
        return
    requisitos = RAIZ / "requirements.txt"
    log.info("Atualização: instalando as bibliotecas do requirements.txt novo")
    try:
        r = subprocess.run([str(_python_da_venv()), "-m", "pip", "install", "--disable-pip-version-check", "--quiet",
                            "-r", str(requisitos)], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=900, stdin=subprocess.DEVNULL, creationflags=_CRIAR_SEM_JANELA)
    except (OSError, subprocess.SubprocessError) as e:
        raise ErroAtualizacao(f"não consegui instalar as bibliotecas novas: {e}") from e
    if r.returncode != 0:
        raise ErroAtualizacao("não consegui instalar as bibliotecas novas: "
                              + " ".join((r.stderr or r.stdout).split())[-300:])
    marca = RAIZ / ".venv" / "autocortes-requisitos.txt"  # o .bat compara com esta e não instala de novo
    if marca.parent.is_dir():
        shutil.copyfile(requisitos, marca)


# ---------------------------------------------------------------- troca (fechar, aplicar, abrir)

def _lancar(args: list[str]) -> subprocess.Popen:
    """Um processo nosso solto deste: ele continua depois que este fechar."""
    cmd = [str(perfis._executavel()), *args]
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    for extra in (0x01000000, 0):  # CREATE_BREAKAWAY_FROM_JOB, se o Windows deixar
        try:
            return subprocess.Popen(cmd, cwd=str(RAIZ), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags | extra)
        except OSError:
            if not extra:
                raise
    raise OSError("não consegui abrir o processo")


def _ocupado_neste(painel) -> str | None:
    from .painel import api

    if api.ocupado(painel):
        return "o AutoCortes está editando, analisando, postando ou baixando agora"
    return None


def motivo_para_esperar(painel, contar_uso: bool = True) -> str | None:
    """Por que a versão não pode ser trocada agora (None = pode)."""
    motivo = _ocupado_neste(painel)
    if motivo:
        return motivo
    if contar_uso and time.time() - float(getattr(painel, "ultimo_uso", 0)) < SEM_USO_SEG:
        return "você mexeu no painel há pouco"
    outros = [p for p in perfis.listar() if p.config.resolve() != painel.cfg.caminho.resolve()]
    estados = perfis.situacoes(outros)
    for p in outros:
        if estados.get(p.slug, {}).get("trabalhando"):
            return f"o perfil {p.nome} está trabalhando agora"
    return None


def iniciar_troca(painel) -> None:
    """Anota quem está aberto, lança o atualizador e fecha todos (este por último)."""
    lista = perfis.listar()
    situacoes = perfis.situacoes(lista)
    atual = painel.cfg.caminho.resolve()
    reabrir, motor_principal = [], True
    for p in lista:
        s = situacoes.get(p.slug, {})
        este = p.config.resolve() == atual
        aberto = este or s.get("rodando")
        motor = painel.motor.rodando if este else bool(s.get("motor", True))
        if p.principal:
            motor_principal = motor
        elif aberto:
            reabrir.append({"slug": p.slug, "motor": motor})
    with _mudar_estado() as estado:
        estado.update({"reabrir": reabrir, "principal_motor": motor_principal, "troca_pedida_em": time.time()})
    _lancar(["-m", "autocortes", "atualizar", "--aplicar", "--depois-de-fechar"])
    for p in lista:
        if p.config.resolve() != atual and situacoes.get(p.slug, {}).get("rodando"):
            perfis.encerrar_pedido(p)  # cada um fecha do jeito normal (o atualizador espera as travas)
    painel.atualizando = True
    log.info("Fechando o AutoCortes para atualizar (abro de novo em seguida)")
    threading.Timer(3.0, painel.encerrar.set).start()  # dá tempo de a janela aberta ver o aviso


def _abrir_principal(motor: bool) -> subprocess.Popen:
    return _lancar(["-m", "autocortes", "painel", "--sem-navegador", *([] if motor else ["--motor-desligado"])])


def _esperar_principal(processo: subprocess.Popen, prazo: float = ESPERA_ABRIR_SEG) -> bool:
    principal = next((p for p in perfis.listar() if p.principal), None)
    if principal is None:
        return False
    limite = time.monotonic() + prazo
    while time.monotonic() < limite:
        if perfis.situacao(principal).get("rodando"):
            return True
        if processo.poll() is not None:
            return False  # fechou sozinho: a versão não abre
        time.sleep(2)
    return False


def _matar(processo: subprocess.Popen) -> None:
    if processo.poll() is not None:
        return
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(processo.pid)], capture_output=True, timeout=20,
                       creationflags=_CRIAR_SEM_JANELA)
    except (OSError, subprocess.SubprocessError):
        processo.kill()


def _registrar_falha(texto: str) -> None:
    """Anota o erro e a versão que falhou: ela só é tentada de novo sozinha na próxima conferência.
    Sem isso, uma versão que não instala (ou não abre) faria o AutoCortes fechar e abrir sem parar."""
    with _mudar_estado() as estado:
        estado["erro"] = texto
        estado["falhou"] = {"sha": (estado.get("remoto") or {}).get("sha"), "em": time.time(), "erro": texto}
    log.error("Atualização: %s", texto)


def tentada_ha_pouco(estado: dict, dias: int) -> bool:
    """A versão do GitHub já falhou aqui há menos de N dias (a automática espera; o botão tenta)."""
    falhou = estado.get("falhou") if isinstance(estado.get("falhou"), dict) else {}
    remoto = (estado.get("remoto") or {}).get("sha")
    return bool(remoto) and falhou.get("sha") == remoto and time.time() - float(falhou.get("em") or 0) < dias * 86400


def executar_troca(depois_de_fechar: bool) -> int:
    """O atualizador: espera todos fecharem, troca a versão e abre o principal de novo."""
    lista = perfis.listar()
    abertos = [p for p in lista if perfis._travado(p.pasta_dados)]
    if abertos and not depois_de_fechar:
        print("Feche o AutoCortes antes (ou use Configurações > Sistema > Atualizar agora no painel): "
              + ", ".join(p.nome for p in abertos))
        return 1
    limite = time.monotonic() + ESPERA_FECHAR_SEG
    while abertos and time.monotonic() < limite:
        time.sleep(2)
        abertos = [p for p in lista if perfis._travado(p.pasta_dados)]
    motor = bool(ler_estado().get("principal_motor", True))
    if abertos:
        _registrar_falha(f"não atualizei: {', '.join(p.nome for p in abertos)} não fechou a tempo")
        if depois_de_fechar and not any(p.principal for p in abertos):
            _abrir_principal(motor)
        return 1
    requisitos_antes = _requisitos()
    try:
        m = modo()
        if m == "git":
            resultado = _aplicar_git()
        elif m == "zip":
            resultado = _aplicar_zip(ler_estado())
        else:
            raise ErroAtualizacao("o programa git não está instalado")
    except (ErroAtualizacao, OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
        _registrar_falha(f"não atualizei: {e}")
        if depois_de_fechar:
            _abrir_principal(motor)
        return 1
    try:
        _bibliotecas(requisitos_antes)
    except ErroAtualizacao as e:
        desfazer(resultado["volta"])
        _registrar_falha(f"voltei para a versão anterior: {e}")
        if depois_de_fechar:
            _abrir_principal(motor)
        return 1
    de, para = resultado["de"] or {}, resultado["para"] or {}
    with _mudar_estado() as estado:
        estado.update({"aplicado": {"de": de, "para": para}, "aplicado_em": time.time(), "aplicado_avisado": False,
                       "disponivel": False, "erro": None, "motivo": None, "local": para, "falhou": None})
    log.info("Atualização: %s -> %s (%s)", str(de.get("sha", "?"))[:7], str(para.get("sha", "?"))[:7],
             para.get("mensagem", ""))
    if not depois_de_fechar:
        return 0
    processo = _abrir_principal(motor)
    if _esperar_principal(processo):
        return 0
    log.error("Atualização: a versão nova não abriu; voltando para a anterior")
    _matar(processo)
    desfazer(resultado["volta"])
    with _mudar_estado() as estado:
        # a versão de antes volta a ser a instalada, e a nova continua disponível (para o botão)
        estado.update({"aplicado": None, "aplicado_em": None, "disponivel": True, "local": de or estado.get("local"),
                       "remoto": para or estado.get("remoto")})
    _registrar_falha("a versão nova não abriu, e eu voltei para a anterior. Veja o registro.")
    _abrir_principal(motor)
    return 1


def reabrir_perfis() -> list[str]:
    """No principal, ao abrir: os perfis que estavam abertos antes da atualização abrem de novo."""
    with _mudar_estado() as estado:
        lista = estado.pop("reabrir", None)  # usada uma vez só
    if not lista:
        return []
    abertos = []
    for item in lista:
        try:
            p = perfis.encontrar(str(item.get("slug") or ""))
            if perfis.situacao(p).get("rodando"):
                continue
            perfis.iniciar(p, motor=bool(item.get("motor", True)))
            abertos.append(p.nome)
        except ErroConfig as e:
            log.warning("Depois da atualização, não consegui abrir o perfil '%s': %s", item.get("slug"), e)
    return abertos


def vigiar(painel) -> None:
    """Thread do principal: confere a cada N dias e, com tudo parado, troca a versão sozinho."""
    with _mudar_estado() as estado:
        if estado.get("aplicado") and not estado.get("aplicado_avisado"):
            para = (estado["aplicado"] or {}).get("para") or {}
            log.info("AutoCortes atualizado para %s: %s", str(para.get("sha", ""))[:7], para.get("mensagem", ""))
            estado["aplicado_avisado"] = True
    if painel.encerrar.wait(120):  # deixa o AutoCortes terminar de abrir
        return
    while not painel.encerrar.is_set():
        try:
            opcoes = perfis.instalacao()
            if opcoes["atualizar_sozinho"]:
                dias = opcoes["atualizar_a_cada_dias"]
                estado = ler_estado()
                if time.time() - float(estado.get("verificado_em") or 0) >= dias * 86400:
                    estado = verificar()
                if (estado.get("disponivel") and not tentada_ha_pouco(estado, dias)
                        and motivo_para_esperar(painel) is None):
                    remoto = estado.get("remoto") or {}
                    log.info("Atualizando o AutoCortes para %s (%s)", str(remoto.get("sha", ""))[:7],
                             remoto.get("mensagem", ""))
                    iniciar_troca(painel)
                    return
        except Exception:  # noqa: BLE001 (a atualização nunca pode derrubar o painel)
            log.exception("Erro na atualização automática")
        painel.encerrar.wait(600)
