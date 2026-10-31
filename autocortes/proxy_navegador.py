"""SOCKS5 autenticado e métricas do Chrome dedicado às postagens.

O Chromium aceita um proxy SOCKS5 sem autenticação. Esta ponte escuta somente em
127.0.0.1, recebe o Chrome sem credenciais e abre cada túnel no SOCKS5 externo
com usuário e senha. Nenhum outro serviço do AutoCortes passa por ela.
"""

from __future__ import annotations

import atexit
import hashlib
import socket
import sqlite3
import statistics
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from . import db
from .config import Config, separar_proxy_socks5
from .util import log

CHAVE_ESTADO = "proxy:navegador"
PRAZO_CONEXAO = 15.0
INTERVALO_GRAVACAO = 15.0
JANELA_VELOCIDADE = 3.0
TAMANHO_BLOCO = 256 * 1024


class ErroProxy(Exception):
    """O SOCKS5 configurado não conectou, não autenticou ou falou um protocolo inválido."""


class _Metricas:
    def __init__(self, banco: Path):
        self.banco = banco
        self._trava = threading.Lock()
        self._parar = threading.Event()
        self._sujo = False
        self._versao = 0
        self._encerrado = False
        self._eventos: deque[tuple[float, int, int]] = deque(maxlen=5000)
        self._latencias: deque[float] = deque(maxlen=20)
        self._ultimo_log = ("", 0.0)
        self._proxima_conferencia_periodo = 0.0
        self.sessao_enviado = 0
        self.sessao_recebido = 0
        self.total_enviado = 0
        self.total_recebido = 0
        self.dia = ""
        self.dia_enviado = 0
        self.dia_recebido = 0
        self.mes = ""
        self.mes_enviado = 0
        self.mes_recebido = 0
        self.latencia_ms: float | None = None
        self.latencia_em: float | None = None
        self.erro = ""
        self.erro_em: float | None = None
        self.conexoes = 0
        self._carregar()
        with self._trava:
            self._normalizar_periodos()
        self._thread = threading.Thread(target=self._ciclo, name="metricas-proxy", daemon=True)
        self._thread.start()

    @staticmethod
    def _chaves_periodo(instante: float) -> tuple[str, str]:
        agora = datetime.fromtimestamp(instante).astimezone()
        return agora.strftime("%Y-%m-%d"), agora.strftime("%Y-%m")

    def _normalizar_periodos(self) -> None:
        agora_mono = time.monotonic()
        if self.dia and self.mes and agora_mono < self._proxima_conferencia_periodo:
            return
        self._proxima_conferencia_periodo = agora_mono + 30
        instante = time.time()
        dia, mes = self._chaves_periodo(instante)
        mudou = False
        if self.dia != dia:
            self.dia, self.dia_enviado, self.dia_recebido = dia, 0, 0
            mudou = True
        if self.mes != mes:
            self.mes, self.mes_enviado, self.mes_recebido = mes, 0, 0
            mudou = True
        if mudou:
            self._marcar_sujo()

    def _marcar_sujo(self) -> None:
        self._sujo = True
        self._versao += 1

    @staticmethod
    def _numero(dados, chave: str) -> int:
        try:
            return max(0, int(dados.get(chave, 0)))
        except (AttributeError, TypeError, ValueError):
            return 0

    def _carregar(self) -> None:
        try:
            conn = db.conectar(self.banco, preparar=False)
            try:
                dados = db.ler_estado(conn, CHAVE_ESTADO, {}) or {}
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            return
        total = dados.get("total") if isinstance(dados, dict) else {}
        dia = dados.get("dia") if isinstance(dados, dict) else {}
        mes = dados.get("mes") if isinstance(dados, dict) else {}
        self.total_enviado = self._numero(total, "enviado")
        self.total_recebido = self._numero(total, "recebido")
        self.dia = str(dia.get("chave") or "") if isinstance(dia, dict) else ""
        self.dia_enviado = self._numero(dia, "enviado")
        self.dia_recebido = self._numero(dia, "recebido")
        self.mes = str(mes.get("chave") or "") if isinstance(mes, dict) else ""
        self.mes_enviado = self._numero(mes, "enviado")
        self.mes_recebido = self._numero(mes, "recebido")
        try:
            latencia = float(dados.get("latencia_ms"))
            self.latencia_ms = latencia if latencia >= 0 else None
        except (AttributeError, TypeError, ValueError):
            self.latencia_ms = None
        try:
            self.latencia_em = float(dados.get("latencia_em"))
        except (AttributeError, TypeError, ValueError):
            self.latencia_em = None
        self.erro = str(dados.get("erro") or "") if isinstance(dados, dict) else ""
        try:
            self.erro_em = float(dados.get("erro_em"))
        except (AttributeError, TypeError, ValueError):
            self.erro_em = None

    def somar(self, enviado: int = 0, recebido: int = 0) -> None:
        enviado, recebido = max(0, int(enviado)), max(0, int(recebido))
        if not enviado and not recebido:
            return
        with self._trava:
            self._normalizar_periodos()
            self.sessao_enviado += enviado
            self.sessao_recebido += recebido
            self.total_enviado += enviado
            self.total_recebido += recebido
            self.dia_enviado += enviado
            self.dia_recebido += recebido
            self.mes_enviado += enviado
            self.mes_recebido += recebido
            self._eventos.append((time.monotonic(), enviado, recebido))
            self._marcar_sujo()

    def registrar_latencia(self, milissegundos: float) -> None:
        valor = max(0.0, float(milissegundos))
        with self._trava:
            self._latencias.append(valor)
            self.latencia_ms = float(statistics.median(self._latencias))
            self.latencia_em = time.time()
            self.erro = ""
            self.erro_em = None
            self._marcar_sujo()

    def registrar_falha(self, mensagem: str) -> None:
        mensagem = str(mensagem)[:300]
        agora = time.time()
        with self._trava:
            self.erro, self.erro_em = mensagem, agora
            self._marcar_sujo()
            anterior, quando = self._ultimo_log
            avisar = mensagem != anterior or agora - quando >= 60
            if avisar:
                self._ultimo_log = (mensagem, agora)
        if avisar:
            log.warning("Proxy SOCKS5 do navegador: %s", mensagem)

    def conexao_aberta(self) -> None:
        with self._trava:
            self.conexoes += 1

    def conexao_fechada(self) -> None:
        with self._trava:
            self.conexoes = max(0, self.conexoes - 1)

    @staticmethod
    def _bloco(enviado: int, recebido: int) -> dict:
        return {"enviado": enviado, "recebido": recebido, "total": enviado + recebido}

    def resumo(self) -> dict:
        agora_mono = time.monotonic()
        with self._trava:
            self._normalizar_periodos()
            limite = agora_mono - JANELA_VELOCIDADE
            while self._eventos and self._eventos[0][0] < limite:
                self._eventos.popleft()
            vel_enviado = sum(item[1] for item in self._eventos) / JANELA_VELOCIDADE
            vel_recebido = sum(item[2] for item in self._eventos) / JANELA_VELOCIDADE
            return {
                "latencia_ms": round(self.latencia_ms) if self.latencia_ms is not None else None,
                "latencia_em": self.latencia_em,
                "erro": self.erro,
                "erro_em": self.erro_em,
                "conexoes": self.conexoes,
                "velocidade": self._bloco(round(vel_enviado), round(vel_recebido)),
                "sessao": self._bloco(self.sessao_enviado, self.sessao_recebido),
                "hoje": self._bloco(self.dia_enviado, self.dia_recebido),
                "mes": self._bloco(self.mes_enviado, self.mes_recebido),
                "total": self._bloco(self.total_enviado, self.total_recebido),
            }

    def _dados_persistentes(self) -> tuple[int, dict] | None:
        with self._trava:
            if not self._sujo:
                return None
            self._normalizar_periodos()
            return self._versao, {
                "versao": 1,
                "total": self._bloco(self.total_enviado, self.total_recebido),
                "dia": {"chave": self.dia, **self._bloco(self.dia_enviado, self.dia_recebido)},
                "mes": {"chave": self.mes, **self._bloco(self.mes_enviado, self.mes_recebido)},
                "latencia_ms": self.latencia_ms,
                "latencia_em": self.latencia_em,
                "erro": self.erro,
                "erro_em": self.erro_em,
            }

    def gravar(self) -> None:
        item = self._dados_persistentes()
        if item is None:
            return
        versao, dados = item
        try:
            conn = db.conectar(self.banco, preparar=False)
            try:
                db.gravar_estado(conn, CHAVE_ESTADO, dados)
            finally:
                conn.close()
        except (OSError, sqlite3.Error) as e:
            log.debug("Não consegui gravar as métricas do proxy agora: %s", e)
            return
        with self._trava:
            if self._versao == versao:
                self._sujo = False

    def _ciclo(self) -> None:
        while not self._parar.wait(INTERVALO_GRAVACAO):
            self.gravar()

    def encerrar(self) -> None:
        with self._trava:
            if self._encerrado:
                return
            self._encerrado = True
        self._parar.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2)
        self.gravar()


class _Ponte:
    def __init__(self, cfg: Config, porta: int = 0):
        self.cfg = cfg
        self.endereco, self.porta_externa, self.usuario, self.senha = separar_proxy_socks5(
            cfg["navegador"]["proxy_socks5"]
        )
        self.assinatura = assinatura(cfg)
        self.metricas = _metricas(cfg)
        self._parar = threading.Event()
        self._trava_soquetes = threading.Lock()
        self._soquetes: set[socket.socket] = set()
        proibidas = {int(cfg["navegador"]["porta"]), int(cfg["painel"]["porta"])}
        if porta and int(porta) in proibidas:
            raise OSError("a porta local antiga agora está reservada pelo painel ou pelo DevTools")
        tentativas = 1 if porta else 20
        self._ouvinte = None
        ultimo_erro: OSError | None = None
        for _ in range(tentativas):
            ouvinte = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                ouvinte.bind(("127.0.0.1", int(porta)))
                escolhida = int(ouvinte.getsockname()[1])
                if not porta and escolhida in proibidas:
                    ouvinte.close()
                    continue
                ouvinte.listen(128)
                ouvinte.settimeout(0.5)
                self._ouvinte = ouvinte
                break
            except OSError as e:
                ultimo_erro = e
                ouvinte.close()
                if porta:
                    raise
        if self._ouvinte is None:
            if ultimo_erro is not None:
                raise ultimo_erro
            raise OSError("não achei uma porta local livre para a ponte SOCKS5")
        self.porta = int(self._ouvinte.getsockname()[1])
        self._thread = threading.Thread(target=self._aceitar, name="proxy-navegador", daemon=True)

    @property
    def ativa(self) -> bool:
        return not self._parar.is_set() and self._thread.is_alive()

    def iniciar(self) -> None:
        self._thread.start()

    def _guardar(self, soquete: socket.socket) -> None:
        with self._trava_soquetes:
            self._soquetes.add(soquete)

    def _soltar(self, soquete: socket.socket) -> None:
        with self._trava_soquetes:
            self._soquetes.discard(soquete)

    def _aceitar(self) -> None:
        while not self._parar.is_set():
            try:
                cliente, origem = self._ouvinte.accept()
            except socket.timeout:
                continue
            except OSError as e:
                if not self._parar.is_set():
                    self.metricas.registrar_falha(f"a ponte local parou ({e.__class__.__name__}: {e})")
                break
            if origem[0] != "127.0.0.1":
                cliente.close()
                continue
            self._guardar(cliente)
            threading.Thread(target=self._atender, args=(cliente,), name="tunel-proxy", daemon=True).start()

    @staticmethod
    def _receber_exato(soquete: socket.socket, tamanho: int) -> bytes:
        partes = bytearray()
        while len(partes) < tamanho:
            bloco = soquete.recv(tamanho - len(partes))
            if not bloco:
                raise ErroProxy("a conexão foi encerrada durante a negociação SOCKS5")
            partes.extend(bloco)
        return bytes(partes)

    def _receber_proxy(self, soquete: socket.socket, tamanho: int) -> bytes:
        dados = self._receber_exato(soquete, tamanho)
        self.metricas.somar(recebido=len(dados))
        return dados

    def _enviar_proxy(self, soquete: socket.socket, dados: bytes) -> None:
        soquete.sendall(dados)
        self.metricas.somar(enviado=len(dados))

    def _autenticar(self) -> socket.socket:
        inicio = time.perf_counter()
        externo: socket.socket | None = None
        try:
            externo = socket.create_connection((self.endereco, self.porta_externa), timeout=PRAZO_CONEXAO)
            externo.settimeout(PRAZO_CONEXAO)
            self._enviar_proxy(externo, b"\x05\x01\x02")
            resposta = self._receber_proxy(externo, 2)
            if resposta != b"\x05\x02":
                if resposta == b"\x05\xff":
                    raise ErroProxy("o proxy não aceitou autenticação com usuário e senha")
                raise ErroProxy("o proxy devolveu uma resposta inválida ao escolher a autenticação")
            usuario, senha = self.usuario.encode("utf-8"), self.senha.encode("utf-8")
            pedido = b"\x01" + bytes((len(usuario),)) + usuario + bytes((len(senha),)) + senha
            self._enviar_proxy(externo, pedido)
            autenticacao = self._receber_proxy(externo, 2)
            if len(autenticacao) != 2 or autenticacao[0] != 1:
                raise ErroProxy("o proxy devolveu uma resposta de autenticação inválida")
            if autenticacao[1] != 0:
                raise ErroProxy("o proxy recusou o usuário ou a senha")
            self.metricas.registrar_latencia((time.perf_counter() - inicio) * 1000)
            return externo
        except socket.timeout as e:
            mensagem = f"o proxy demorou mais de {int(PRAZO_CONEXAO)} s para responder"
            self.metricas.registrar_falha(mensagem)
            if externo is not None:
                externo.close()
            raise ErroProxy(mensagem) from e
        except OSError as e:
            mensagem = f"não consegui conectar ao proxy configurado ({e.__class__.__name__}: {e})"
            self.metricas.registrar_falha(mensagem)
            if externo is not None:
                externo.close()
            raise ErroProxy(mensagem) from e
        except ErroProxy as e:
            self.metricas.registrar_falha(str(e))
            if externo is not None:
                externo.close()
            raise

    @staticmethod
    def _endereco_bruto(soquete: socket.socket, tipo: int, receber) -> bytes:
        if tipo == 1:
            return receber(soquete, 4)
        if tipo == 4:
            return receber(soquete, 16)
        if tipo == 3:
            tamanho = receber(soquete, 1)
            return tamanho + receber(soquete, tamanho[0])
        raise ErroProxy("o navegador pediu um tipo de endereço SOCKS5 desconhecido")

    @staticmethod
    def _resposta_erro(codigo: int = 1) -> bytes:
        return bytes((5, codigo, 0, 1, 0, 0, 0, 0, 0, 0))

    def _copiar(self, origem: socket.socket, destino: socket.socket, enviando: bool) -> None:
        try:
            while not self._parar.is_set():
                dados = origem.recv(TAMANHO_BLOCO)
                if not dados:
                    break
                destino.sendall(dados)
                if enviando:
                    self.metricas.somar(enviado=len(dados))
                else:
                    self.metricas.somar(recebido=len(dados))
        except OSError:
            pass
        finally:
            try:
                destino.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def _atender(self, cliente: socket.socket) -> None:
        externo: socket.socket | None = None
        ativa = False
        try:
            cliente.settimeout(PRAZO_CONEXAO)
            inicio = self._receber_exato(cliente, 2)
            if len(inicio) != 2 or inicio[0] != 5:
                return
            metodos = self._receber_exato(cliente, inicio[1])
            if 0 not in metodos:
                cliente.sendall(b"\x05\xff")
                return
            cliente.sendall(b"\x05\x00")
            cabecalho = self._receber_exato(cliente, 4)
            if cabecalho[0] != 5 or cabecalho[2] != 0:
                cliente.sendall(self._resposta_erro())
                return
            if cabecalho[1] != 1:
                cliente.sendall(self._resposta_erro(7))
                return
            endereco = self._endereco_bruto(cliente, cabecalho[3], self._receber_exato)
            porta = self._receber_exato(cliente, 2)
            externo = self._autenticar()
            self._guardar(externo)
            self._enviar_proxy(externo, cabecalho + endereco + porta)
            resposta = self._receber_proxy(externo, 4)
            if resposta[0] != 5 or resposta[2] != 0:
                raise ErroProxy("o proxy devolveu uma resposta inválida ao abrir o destino")
            vinculo = self._endereco_bruto(externo, resposta[3], self._receber_proxy)
            vinculo_porta = self._receber_proxy(externo, 2)
            cliente.sendall(resposta + vinculo + vinculo_porta)
            if resposta[1] != 0:
                return
            cliente.settimeout(None)
            externo.settimeout(None)
            ativa = True
            self.metricas.conexao_aberta()
            envio = threading.Thread(target=self._copiar, args=(cliente, externo, True),
                                     name="tunel-proxy-envio", daemon=True)
            envio.start()
            self._copiar(externo, cliente, False)
            try:
                cliente.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                externo.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            envio.join(timeout=1)
        except ErroProxy:
            try:
                cliente.sendall(self._resposta_erro())
            except OSError:
                pass
        except (OSError, ValueError):
            try:
                cliente.sendall(self._resposta_erro())
            except OSError:
                pass
        finally:
            if ativa:
                self.metricas.conexao_fechada()
            for soquete in (cliente, externo):
                if soquete is None:
                    continue
                self._soltar(soquete)
                try:
                    soquete.close()
                except OSError:
                    pass

    def parar(self) -> None:
        if self._parar.is_set():
            return
        self._parar.set()
        try:
            self._ouvinte.close()
        except OSError:
            pass
        with self._trava_soquetes:
            soquetes = list(self._soquetes)
        for soquete in soquetes:
            try:
                soquete.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                soquete.close()
            except OSError:
                pass
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2)
        self.metricas.gravar()


_TRAVA = threading.RLock()
_PONTES: dict[str, _Ponte] = {}
_METRICAS: dict[str, _Metricas] = {}


def _chave(cfg: Config) -> str:
    try:
        return str(cfg.pasta_dados.resolve())
    except OSError:
        return str(cfg.pasta_dados.absolute())


def _metricas(cfg: Config) -> _Metricas:
    chave = _chave(cfg)
    with _TRAVA:
        atual = _METRICAS.get(chave)
        if atual is None:
            atual = _Metricas(cfg.banco)
            _METRICAS[chave] = atual
        return atual


def assinatura(cfg: Config) -> str:
    """Identificador sem texto puro para saber com qual proxy uma janela foi aberta."""
    endereco, porta, usuario, senha = separar_proxy_socks5(cfg["navegador"]["proxy_socks5"])
    bruto = f"{endereco}\0{porta}\0{usuario}\0{senha}".encode("utf-8")
    return hashlib.sha256(bruto).hexdigest()[:20]


def testar(cfg: Config) -> dict:
    """Confere conexão e autenticação, sem abrir um destino externo."""
    if not str(cfg["navegador"].get("proxy_socks5") or "").strip():
        raise ErroProxy("preencha o proxy no formato servidor:porta:usuario:senha")
    try:
        ponte = _Ponte(cfg)
    except (OSError, ValueError) as e:
        raise ErroProxy(str(e)) from e
    try:
        externo = ponte._autenticar()
        externo.close()
    finally:
        ponte._ouvinte.close()
    return resumo(cfg)


def iniciar(cfg: Config, porta: int = 0) -> dict:
    """Sobe (ou reaproveita) a ponte deste perfil e devolve a porta para o Chrome."""
    chave = _chave(cfg)
    esperada = assinatura(cfg)
    with _TRAVA:
        atual = _PONTES.get(chave)
        proibidas = {int(cfg["navegador"]["porta"]), int(cfg["painel"]["porta"])}
        if atual is not None and atual.ativa and atual.assinatura == esperada and atual.porta not in proibidas and (
                not porta or atual.porta == int(porta)):
            return {"ativo": True, "porta": atual.porta, "assinatura": atual.assinatura}
        if atual is not None:
            _PONTES.pop(chave, None)
            atual.parar()
        try:
            ponte = _Ponte(cfg, porta)
        except OSError as e:
            raise ErroProxy(f"não consegui abrir a porta local da ponte SOCKS5 ({e})") from e
        ponte.iniciar()
        try:
            externo = ponte._autenticar()
            externo.close()
        except ErroProxy:
            ponte.parar()
            raise
        _PONTES[chave] = ponte
        log.info("Proxy SOCKS5 do navegador pronto numa porta local deste perfil")
        return {"ativo": True, "porta": ponte.porta, "assinatura": ponte.assinatura}


def parar(cfg: Config) -> None:
    with _TRAVA:
        ponte = _PONTES.pop(_chave(cfg), None)
    if ponte is not None:
        ponte.parar()


def resumo(cfg: Config) -> dict:
    metricas = _metricas(cfg)
    with _TRAVA:
        ponte = _PONTES.get(_chave(cfg))
        ponte_ativa = bool(ponte is not None and ponte.ativa)
    dados = metricas.resumo()
    ativo = bool(cfg["navegador"].get("proxy_ativo"))
    salvo = cfg.segredo_definido("navegador", "proxy_socks5")
    erro_atual = bool(dados["erro"] and (not dados["latencia_em"] or
                                         (dados["erro_em"] or 0) > dados["latencia_em"]))
    if not ativo:
        estado = "desligado"
    elif erro_atual:
        estado = "erro"
    elif ponte_ativa:
        estado = "ativo"
    else:
        estado = "pronto"
    return {**dados, "ativo": ativo, "configurado": salvo, "ponte_ativa": ponte_ativa, "estado": estado}


def falha_recente(cfg: Config, segundos: float = 120) -> bool:
    """A autenticação/conexão com o SOCKS5 falhou há pouco, depois do último sucesso?"""
    if not cfg["navegador"].get("proxy_ativo"):
        return False
    dados = _metricas(cfg).resumo()
    erro_em = dados.get("erro_em")
    if not dados.get("erro") or not erro_em or time.time() - float(erro_em) > segundos:
        return False
    return not dados.get("latencia_em") or float(erro_em) > float(dados["latencia_em"])


def encerrar_todos() -> None:
    with _TRAVA:
        pontes = list(_PONTES.values())
        metricas = list(_METRICAS.values())
        _PONTES.clear()
        _METRICAS.clear()
    for ponte in pontes:
        ponte.parar()
    for item in metricas:
        item.encerrar()


atexit.register(encerrar_todos)
