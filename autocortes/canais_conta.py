"""Conta separada (opcional) para baixar dos canais do YouTube.

Fica desligada até você conectar uma, e deve ser uma conta do Google criada só para isso, nunca a
do canal onde você posta. Os cookies saem de um Chrome à parte (`navegador.NavegadorTemporario`),
aberto com um perfil novo e temporário que é apagado no fim, para a sessão nunca mais abrir num
navegador. Ficam cifrados com a proteção de dados do Windows (`segredos`), só os de youtube.com e
google.com.

Na hora de usar (`cookies_temporarios`), eles são decifrados num arquivo temporário em dados/tokens,
só durante a chamada do yt-dlp. O yt-dlp grava ali os cookies renovados: o arquivo é relido, cifrado
de novo e apagado, também em erro e cancelamento. Valores, o texto Netscape e o caminho do arquivo
temporário nunca vão para o registro.
"""

from __future__ import annotations

import secrets
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import segredos
from .config import Config
from .navegador import ErroNavegador, NavegadorTemporario
from .util import log

ARQUIVO = "canais-youtube.json"  # nunca youtube.json: esse é a conta de postar pela API
PASTA_PERFIL = "chrome-conta-canais"
PREFIXO_TEMP = ".canais-cookies-"
URL_LOGIN = "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fwww.youtube.com%2F"
URL_ROBOTS = "https://www.youtube.com/robots.txt"
# com um destes num domínio do YouTube, a conta entrou
COOKIES_DE_SESSAO = ("LOGIN_INFO", "__Secure-3PSID", "__Secure-1PSID")
DOMINIOS = ("youtube.com", "google.com")
PRAZO_LOGIN_SEG = 600
# os testes põem --headless=new e cortam o tráfego de fundo do Chrome
ARGUMENTOS_EXTRAS: tuple[str, ...] = ()
CABECALHO = "# Netscape HTTP Cookie File"
_HTTPONLY = "#HttpOnly_"
NAO_ABRE = "o arquivo da conta não abre neste usuário do Windows; entre de novo"

# a leitura e a gravação da conta (a olhada, o painel e a janela de conectar). A leitura também
# passa por ela: uma leitura no meio de uma gravação guardaria no cache do `segredos` a conta antiga
_TRAVA = threading.RLock()


class ErroConta(Exception):
    """A conta separada não pode ser usada (não existe, caiu ou não deu para guardar)."""


@dataclass
class ArquivoCookies:
    caminho: Path
    id: str


# ---------------------------------------------------------------- caminhos e formato

def arquivo(cfg: Config) -> Path:
    return cfg.pasta_tokens / ARQUIVO


def pasta_perfil(cfg: Config) -> Path:
    return cfg.pasta_dados / PASTA_PERFIL


def dominio_permitido(dominio: str) -> bool:
    """youtube.com, google.com e os subdomínios deles (notyoutube.com não passa)."""
    d = str(dominio or "").strip().lower().lstrip(".")
    return any(d == base or d.endswith("." + base) for base in DOMINIOS)


def _do_youtube(dominio: str) -> bool:
    d = str(dominio or "").strip().lower().lstrip(".")
    return d == "youtube.com" or d.endswith(".youtube.com")


def para_netscape(cookies_cdp) -> tuple[str, int]:
    """Os cookies do CDP (Storage.getCookies) no formato que o yt-dlp lê, só os permitidos.
    Devolve (texto, quantos)."""
    linhas = [CABECALHO]
    for c in cookies_cdp or []:
        if not isinstance(c, dict):
            continue
        dominio = str(c.get("domain") or "").strip()
        nome = str(c.get("name") or "")
        valor = str(c.get("value") or "")
        caminho = str(c.get("path") or "/")
        if not nome or not dominio_permitido(dominio):
            continue
        if any(ch in campo for campo in (dominio, nome, valor, caminho) for ch in "\t\r\n"):
            continue  # quebraria a linha
        try:
            expira = float(c.get("expires") or 0)
        except (TypeError, ValueError):
            expira = 0.0
        expira = 0 if c.get("session") or expira <= 0 else int(expira)  # 0 = cookie de sessão
        linhas.append("\t".join((dominio, "TRUE" if dominio.startswith(".") else "FALSE", caminho,
                                 "TRUE" if c.get("secure") else "FALSE", str(expira), nome, valor)))
    return "\n".join(linhas) + "\n", len(linhas) - 1


def filtrar_netscape(texto: str) -> tuple[str, int]:
    """O arquivo que o yt-dlp regravou, só com as linhas válidas de youtube.com e google.com."""
    linhas = [CABECALHO]
    for linha in str(texto or "").splitlines():
        sem_prefixo = linha[len(_HTTPONLY):] if linha.startswith(_HTTPONLY) else linha
        if not sem_prefixo.strip() or sem_prefixo.startswith("#"):
            continue  # linha em branco ou comentário (o cabeçalho é sempre o meu)
        campos = sem_prefixo.split("\t")
        if len(campos) == 7 and campos[5] and dominio_permitido(campos[0]):
            linhas.append(linha)
    return "\n".join(linhas) + "\n", len(linhas) - 1


def _tem_sessao(texto: str) -> bool:
    for linha in str(texto or "").splitlines():
        campos = (linha[len(_HTTPONLY):] if linha.startswith(_HTTPONLY) else linha).split("\t")
        if len(campos) == 7 and campos[5] in COOKIES_DE_SESSAO and _do_youtube(campos[0]):
            return True
    return False


# ---------------------------------------------------------------- a conta guardada

def ler(cfg: Config) -> dict:
    """A conta decifrada ({} sem arquivo, ou quando ele não abre neste usuário do Windows)."""
    with _TRAVA:
        return segredos.ler_token(arquivo(cfg))


def _gravar(cfg: Config, dados: dict) -> None:
    if not segredos.DISPONIVEL:  # nunca em texto puro
        raise ErroConta("a conta só pode ser guardada cifrada, e a proteção de dados do Windows não está disponível")
    cfg.pasta_tokens.mkdir(parents=True, exist_ok=True)
    segredos.salvar_token(arquivo(cfg), dados)


def situacao(cfg: Config) -> str:
    """nenhuma, ok ou caiu (o arquivo que não abre aqui também conta como caiu)."""
    if not arquivo(cfg).exists():
        return "nenhuma"
    dados = ler(cfg)
    return "ok" if dados.get("situacao") == "ok" and str(dados.get("cookies") or "").strip() else "caiu"


def utilizavel(cfg: Config) -> bool:
    return situacao(cfg) == "ok"


def publico(cfg: Config) -> dict:
    """O que o painel vê da conta: nunca os cookies, só quantos são."""
    vazio = {"situacao": "nenhuma", "conectada_em": None, "caiu_em": None, "motivo": None, "usada_em": None,
             "renovada_em": None, "cookies": 0}
    if not arquivo(cfg).exists():
        return vazio
    dados = ler(cfg)
    if not dados:
        return {**vazio, "situacao": "caiu", "motivo": NAO_ABRE}
    return {
        "situacao": situacao(cfg),
        **{k: dados.get(k) for k in ("conectada_em", "caiu_em", "motivo", "usada_em", "renovada_em")},
        "cookies": filtrar_netscape(dados.get("cookies") or "")[1],
    }


def salvar_nova(cfg: Config, texto: str) -> dict:
    """Guarda a conta que acabou de entrar (cada conexão ganha um id novo)."""
    dados = {
        "versao": 1, "id": secrets.token_hex(8), "cookies": texto, "conectada_em": time.time(), "situacao": "ok",
        "caiu_em": None, "motivo": None, "usada_em": None, "renovada_em": None,
    }
    with _TRAVA:
        _gravar(cfg, dados)
    return dados


def marcar_caiu(cfg: Config, motivo: str, id_conta: str | None = None) -> bool:
    """A conta precisa entrar de novo: os cookies saem e fica só a situação. Com `id_conta`, só se
    ainda for a mesma conexão (ela pode ter sido trocada ou esquecida no meio de um download)."""
    with _TRAVA:
        dados = ler(cfg)
        if not dados or (id_conta is not None and dados.get("id") != id_conta):
            return False
        dados.update(situacao="caiu", cookies="", caiu_em=time.time(), motivo=motivo)
        _gravar(cfg, dados)
    log.warning("Canais: a conta separada precisa entrar de novo: %s", motivo)
    return True


def _apagar_arquivo(caminho: Path, tentativas: int = 10) -> bool:
    for n in range(tentativas):
        try:
            caminho.unlink(missing_ok=True)
            return True
        except OSError:
            if n < tentativas - 1:
                time.sleep(0.2)
    return False


def _temporarios(cfg: Config) -> list[Path]:
    try:
        return [p for p in cfg.pasta_tokens.glob(f"{PREFIXO_TEMP}*.txt") if p.is_file()]
    except OSError:
        return []


def limpar_sobras(cfg: Config) -> int:
    """Apaga os arquivos temporários de cookies que sobraram (só eles: o perfil pode ser de uma janela
    aberta agora). Devolve quantos apagou."""
    n = sum(1 for p in _temporarios(cfg) if _apagar_arquivo(p, 3))
    if n:
        log.info("Canais: apaguei %d arquivo(s) temporário(s) de cookies que tinham sobrado", n)
    return n


def esquecer(cfg: Config) -> bool:
    """Apaga a conta, os arquivos temporários e o perfil temporário. Devolve se havia uma conta."""
    with _TRAVA:
        existia = arquivo(cfg).exists()
        arquivo(cfg).unlink(missing_ok=True)
        for p in _temporarios(cfg):
            _apagar_arquivo(p, 3)
    _apagar_perfil(cfg)
    return existia


def _apagar_perfil(cfg: Config, tentativas: int = 20) -> bool:
    """Apaga a pasta do Chrome temporário (True = não sobrou nada). Recusa qualquer outra pasta."""
    alvo = pasta_perfil(cfg)
    dados = cfg.pasta_dados.resolve()
    esperado = (cfg.pasta_dados / PASTA_PERFIL).resolve()
    if alvo.resolve() != esperado or esperado.parent != dados or esperado == (dados / "chrome"):
        log.warning("Canais: não apago %s: não é a pasta do perfil temporário da conta", alvo)
        return False
    for n in range(max(1, tentativas)):
        if not alvo.exists():
            return True
        shutil.rmtree(alvo, ignore_errors=True)  # o Chrome pode segurar arquivos por um instante
        if not alvo.exists():
            return True
        if n < tentativas - 1:
            time.sleep(0.5)
    return not alvo.exists()


@contextmanager
def cookies_temporarios(cfg: Config):
    """O arquivo de cookies para o `--cookies` do yt-dlp, decifrado só durante a chamada.

    No fim (também em erro e cancelamento), os cookies que o yt-dlp renovou no arquivo voltam para
    a conta cifrada, se ela ainda for a mesma conexão, e o arquivo é apagado."""
    with _TRAVA:
        dados = ler(cfg)
        texto = str(dados.get("cookies") or "")
        if dados.get("situacao") != "ok" or not texto.strip():
            raise ErroConta("a conta separada não está conectada")
        id_conta = str(dados.get("id") or "")
        cfg.pasta_tokens.mkdir(parents=True, exist_ok=True)
        caminho = cfg.pasta_tokens / f"{PREFIXO_TEMP}{secrets.token_hex(8)}.txt"
        with open(caminho, "w", encoding="utf-8", newline="\n") as f:
            f.write(texto if texto.endswith("\n") else texto + "\n")
    try:
        yield ArquivoCookies(caminho, id_conta)
    finally:
        try:
            novo, n = filtrar_netscape(caminho.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            novo, n = "", 0
        try:
            with _TRAVA:
                atual = ler(cfg)
                if atual.get("id") == id_conta and atual.get("situacao") == "ok":
                    agora = time.time()
                    # um arquivo pela metade (o yt-dlp morto no meio da gravação) não troca os bons
                    if n and _tem_sessao(novo) and novo != atual.get("cookies"):
                        atual["cookies"] = novo
                        atual["renovada_em"] = agora
                    atual["usada_em"] = agora
                    _gravar(cfg, atual)
        except (OSError, ErroConta) as e:
            log.warning("Canais: não consegui guardar os cookies renovados da conta separada (%s)",
                        e.__class__.__name__)
        if not _apagar_arquivo(caminho):
            log.warning("Canais: não consegui apagar o arquivo temporário dos cookies da conta; "
                        "ele sai na próxima olhada")


# ---------------------------------------------------------------- a janela de conectar

class Conexao:
    """A janela "Entrar numa conta separada": um Chrome à parte, com um perfil novo e temporário,
    até a conta entrar (ou passar o prazo, ou você cancelar). Em qualquer fim, a janela fecha e o
    perfil é apagado; os cookies só são guardados depois disso."""

    ATIVOS = ("abrindo", "aguardando", "salvando")

    def __init__(self, cfg: Config, ao_conectar=None):
        self.cfg = cfg
        self.ao_conectar = ao_conectar
        self.estado = "abrindo"  # abrindo | aguardando | salvando | ok | erro | cancelado | prazo
        self.mensagem = ""
        self.desde = time.time()
        self._parar = threading.Event()
        self._trava = threading.Lock()  # a troca de estado (o cancelar não cruza com o salvar)
        self._navegador: NavegadorTemporario | None = None
        self.thread: threading.Thread | None = None
        self._comecou = False  # o iniciar() foi chamado
        self._iniciado = threading.Event()  # o iniciar() voltou (o cancelar espera a janela que ainda abre)

    @property
    def ativa(self) -> bool:
        return self.estado in self.ATIVOS

    def publico(self) -> dict:
        return {"estado": self.estado, "mensagem": self.mensagem, "desde": self.desde}

    def _mudar(self, estado: str, mensagem: str) -> bool:
        """Troca de estado enquanto a conexão está ativa (False = ela já tinha acabado)."""
        with self._trava:
            if not self.ativa:
                return False
            self.estado, self.mensagem = estado, mensagem
            return True

    def iniciar(self) -> None:
        """Abre a janela no login do Google (síncrono) e começa a esperar a conta entrar."""
        self._comecou = True
        try:
            self._iniciar()
        finally:
            self._iniciado.set()

    def _iniciar(self) -> None:
        if not segredos.DISPONIVEL:
            raise ErroConta("a conta só pode ser guardada cifrada, e a proteção de dados do Windows não está disponível")
        if not _apagar_perfil(self.cfg):
            raise ErroConta("uma janela da conta separada ainda está aberta: feche-a e tente de novo")
        evitar: set[int] = set()
        try:
            from . import perfis  # aqui dentro para não amarrar os módulos na importação

            for p in perfis.listar():
                evitar.update(x for x in (p.porta, p.porta_navegador) if x)
        except Exception:  # noqa: BLE001 (a porta sorteada já fica fora das do config deste perfil)
            log.debug("Canais: não consegui listar as portas dos perfis", exc_info=True)
        self._navegador = NavegadorTemporario(self.cfg, pasta_perfil(self.cfg), self._parar,
                                              extras=ARGUMENTOS_EXTRAS, evitar=evitar)
        try:
            self._navegador.abrir(URL_LOGIN)
        except BaseException as e:
            self._navegador.encerrar()
            _apagar_perfil(self.cfg)
            self._mudar("erro", f"Não consegui guardar a conta: {e}")  # cancelada enquanto abria: fica "cancelado"
            raise
        self._mudar("aguardando", "Entre na conta separada na janela que abriu.")
        self.thread = threading.Thread(target=self._acompanhar, name="conta-canais", daemon=True)
        self.thread.start()
        log.info("Canais: abri a janela para entrar na conta separada (perfil temporário)")

    def _esperar_a_conta(self) -> tuple[str, int] | None:
        """Os cookies em Netscape quando a conta entra (None = cancelado ou prazo)."""
        nav = self._navegador
        limite = time.monotonic() + PRAZO_LOGIN_SEG
        while True:
            if self._parar.wait(2):
                return None
            if time.monotonic() > limite:
                minutos = max(1, round(PRAZO_LOGIN_SEG / 60))
                self._mudar("prazo", f"Passaram {minutos} min sem a conta entrar. Fechei a janela; tente de novo "
                                     "quando quiser.")
                return None
            try:
                cookies = nav.cookies()
            except ErroNavegador:
                if nav.vivo and nav.responde():
                    continue  # um tropeço com a janela aberta: tento de novo daqui a 2 s
                raise
            if any(isinstance(c, dict) and c.get("name") in COOKIES_DE_SESSAO and _do_youtube(c.get("domain"))
                   for c in cookies):
                break
        if not self._mudar("salvando", "A conta entrou. Guardando os cookies e fechando a janela..."):
            return None
        if self._parar.wait(3):  # o YouTube termina de gravar a sessão
            return None
        # como na wiki do yt-dlp: a única aba no robots.txt, e a janela fecha logo depois de exportar
        nav.so_uma_aba(URL_ROBOTS)
        texto, n = para_netscape(nav.cookies())
        if not n or not _tem_sessao(texto):
            raise ErroConta("a conta entrou, mas os cookies do YouTube não vieram; tente de novo")
        return texto, n

    def _acompanhar(self) -> None:
        resultado = None
        try:
            resultado = self._esperar_a_conta()
        except ErroNavegador as e:
            nav = self._navegador
            if e.conexao or nav is None or not nav.vivo:
                self._mudar("erro", "A janela foi fechada antes de a conta entrar.")
            else:
                self._mudar("erro", f"Não consegui guardar a conta: {e}")
        except Exception as e:  # noqa: BLE001 (a janela fecha e o perfil sai de qualquer jeito)
            if not isinstance(e, ErroConta):
                log.exception("Canais: erro na janela da conta separada")
            self._mudar("erro", f"Não consegui guardar a conta: {e}")
        finally:
            if self._navegador is not None:
                self._navegador.encerrar()
            if not _apagar_perfil(self.cfg):
                log.warning("Canais: não consegui apagar o perfil temporário da conta separada; ele sai na "
                            "próxima conexão")
        if resultado is None:
            if self.estado in ("erro", "prazo"):
                log.warning("Canais: a conta separada não foi conectada: %s", self.mensagem)
            return
        texto, n = resultado
        with self._trava:
            if not self.ativa:
                return  # cancelado enquanto a janela fechava
            try:
                salvar_nova(self.cfg, texto)
            except (OSError, ErroConta) as e:
                self.estado, self.mensagem = "erro", f"Não consegui guardar a conta: {e}"
                return
            log.info("Canais: conta separada conectada (%d cookies do YouTube e do Google)", n)
            if self.ao_conectar is not None:
                # antes do "ok": quem vê o "ok" (o painel) já recebe a pausa trocada pela janela com a conta
                try:
                    self.ao_conectar()
                except Exception:  # noqa: BLE001 (a conta já está guardada)
                    log.exception("Canais: erro depois de conectar a conta separada")
            self.estado, self.mensagem = "ok", f"Conta separada conectada ({n} cookies do YouTube e do Google)."

    def cancelar(self) -> None:
        """Fecha a janela e apaga o perfil temporário (nada é guardado)."""
        if not self._mudar("cancelado", "Cancelado. Fechei a janela e apaguei o perfil temporário."):
            return
        self._parar.set()
        # ainda abrindo (o fechamento do AutoCortes no meio do iniciar): espero ele fechar a janela
        if self._comecou:
            self._iniciado.wait(timeout=20)
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=15)
        log.info("Canais: janela da conta separada cancelada")
