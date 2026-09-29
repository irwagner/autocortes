"""Base comum das redes: erros classificados, tokens em disco e HTTP sem vazar segredos."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from ..config import Config
from ..segredos import ler_token, salvar_token
from ..textos import Conteudo
from ..util import ATIVIDADES

_SEGREDOS = re.compile(
    r"((?:access_token|input_token|client_secret|fb_exchange_token|refresh_token|code|upload_token)=)[^&\s'\"]+",
    re.I,
)
_BEARER = re.compile(r"((?:Bearer|OAuth)\s+)[A-Za-z0-9._~+/=-]{16,}")


def ocultar_segredos(texto: str) -> str:
    return _BEARER.sub(r"\1***", _SEGREDOS.sub(r"\1***", texto))


class ErroPublicacao(Exception):
    """Falha ao publicar, com a ação que o loop deve tomar.

    tipo = "temporario": tenta de novo mais tarde (rede, limite diário, servidor fora)
           "corte":      este vídeo não serve para esta rede; pula para o próximo
           "bloqueio":   login/config com problema; para de postar nesta rede
    """

    def __init__(self, mensagem: str, tipo: str = "temporario"):
        super().__init__(ocultar_segredos(mensagem))
        self.tipo = tipo


@dataclass
class Resultado:
    id_remoto: str
    url: str | None = None
    observacao: str | None = None


class Plataforma:
    nome = ""
    rotulo = ""
    # "oficial" = API da própria rede; "upload_post" = serviço intermediário; "manual" = você posta
    via = "oficial"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.conf = cfg[self.nome]
        self.sessao = requests.Session()
        self.sessao.headers["User-Agent"] = "autocortes/1.0"

    # ---- tokens
    @property
    def arquivo_token(self) -> Path:
        return self.cfg.pasta_tokens / f"{self.nome}.json"

    def token(self) -> dict:
        return ler_token(self.arquivo_token)

    def salvar_token(self, dados: dict) -> None:
        salvar_token(self.arquivo_token, dados)  # cifrado para o usuário do Windows (DPAPI)

    def conta(self) -> str | None:
        """Nome da conta conectada, guardado no último login/teste."""
        return self.token().get("conta")

    def lembrar_conta(self, descricao: str) -> None:
        tok = self.token()
        if tok:
            tok["conta"] = descricao
            self.salvar_token(tok)

    def desconectar(self) -> None:
        self.arquivo_token.unlink(missing_ok=True)

    # ---- interface
    def pronta(self) -> tuple[bool, str]:
        raise NotImplementedError

    def autorizar(self) -> None:
        raise NotImplementedError

    def verificar(self) -> str:
        raise NotImplementedError

    def publicar(self, arquivo: Path, conteudo: Conteudo, parar: threading.Event | None = None) -> Resultado:
        raise NotImplementedError

    # ---- HTTP
    def requisicao(self, metodo: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", (20, 120))
        try:
            return self.sessao.request(metodo, url, **kwargs)
        except requests.RequestException as e:
            raise ErroPublicacao(f"{self.rotulo}: falha de rede ({e.__class__.__name__}: {e})", "temporario") from e


class LeitorProgresso:
    """Trecho de arquivo para upload com requests, informando o progresso ao painel.

    Tem __len__ (o requests usa como Content-Length) e read() em blocos.
    """

    def __init__(self, caminho: Path, inicio: int = 0, fim: int | None = None, total: int | None = None):
        self._arquivo = open(caminho, "rb")
        self._arquivo.seek(inicio)
        tamanho = caminho.stat().st_size
        self._restante = (tamanho if fim is None else fim) - inicio
        self._tamanho_trecho = self._restante
        self._enviado = inicio
        self._total = max(1, total or tamanho)
        self._chave = threading.current_thread().name
        self._ultimo = 0.0

    def __len__(self) -> int:
        return self._tamanho_trecho

    def read(self, n: int = -1) -> bytes:
        if self._restante <= 0:
            return b""
        if n is None or n < 0 or n > self._restante:
            n = self._restante
        dados = self._arquivo.read(n)
        self._restante -= len(dados)
        self._enviado += len(dados)
        agora = time.monotonic()
        if agora - self._ultimo > 0.5 or self._restante <= 0:
            self._ultimo = agora
            ATIVIDADES.progresso(100 * self._enviado / self._total, self._chave)
        return dados

    def close(self) -> None:
        self._arquivo.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def json_seguro(resposta: requests.Response) -> dict:
    try:
        dados = resposta.json()
    except ValueError:
        return {}
    return dados if isinstance(dados, dict) else {}


def esperar(parar: threading.Event | None, segundos: float) -> None:
    """Dorme, mas acorda na hora se o loop pedir para parar."""
    if parar is None:
        threading.Event().wait(segundos)
    elif parar.wait(segundos):
        raise ErroPublicacao("interrompido pelo usuário", "temporario")
