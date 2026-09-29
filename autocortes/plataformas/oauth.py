"""Login OAuth pelo navegador com retorno em 127.0.0.1 (usado por YouTube e TikTok)."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .base import ErroPublicacao


class _Http(HTTPServer):
    # no Windows, SO_REUSEADDR deixaria dois programas ouvindo na mesma porta
    allow_reuse_address = os.name != "nt"

_PAGINA_OK = (
    "<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><title>AutoCortes</title></head>"
    "<body style='font-family:system-ui,sans-serif;background:#0f1117;color:#e8eaf0;padding:48px'>"
    "<h1 style='margin:0 0 12px'>Conta conectada</h1>"
    "<p>A autorização chegou ao AutoCortes. Pode fechar esta aba e voltar ao painel.</p></body></html>"
)


def pkce(desafio_hex: bool = False) -> tuple[str, str]:
    """Par (code_verifier, code_challenge). O TikTok exige o SHA-256 em hexadecimal."""
    verificador = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verificador.encode("ascii")).digest()
    if desafio_hex:
        return verificador, digest.hex()
    return verificador, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class ServidorRetorno:
    """Servidor HTTP mínimo, só em 127.0.0.1, que vive apenas durante o login."""

    def __init__(self, porta: int = 0, caminho: str = "/", host: str = "127.0.0.1"):
        self.caminho = caminho
        self.host = host
        self.parametros: dict[str, str] | None = None
        self._recebido = threading.Event()
        self._cancelado = False
        self._rodando = False
        servidor = self

        class Tratador(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (nome exigido pela biblioteca)
                url = urlparse(self.path)
                if url.path.rstrip("/") != servidor.caminho.rstrip("/"):
                    self.send_response(404)
                    self.end_headers()
                    return
                servidor.parametros = {k: v[0] for k, v in parse_qs(url.query).items()}
                corpo = _PAGINA_OK.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)
                servidor._recebido.set()

            def log_message(self, *args):
                pass

        try:
            self._httpd = _Http((host, int(porta)), Tratador)
        except OSError as e:
            raise ErroPublicacao(f"Não consegui abrir a porta {porta} para o login: {e}", "bloqueio") from e
        self.porta = self._httpd.server_address[1]

    @property
    def redirect_uri(self) -> str:
        return f"http://{self.host}:{self.porta}{self.caminho}"

    def iniciar(self) -> None:
        self._rodando = True
        threading.Thread(target=self._httpd.serve_forever, name="login-retorno", daemon=True).start()

    def cancelar(self) -> None:
        self._cancelado = True
        self._recebido.set()

    def _fechar(self) -> None:
        if self._rodando:
            self._httpd.shutdown()
            self._rodando = False
        self._httpd.server_close()

    def esperar(self, estado: str, timeout: float = 300) -> dict[str, str]:
        try:
            recebido = self._recebido.wait(timeout)
        finally:
            self._fechar()
        if self._cancelado:
            raise ErroPublicacao("Login cancelado", "bloqueio")
        if not recebido:
            raise ErroPublicacao("Tempo esgotado esperando a autorização no navegador", "bloqueio")
        p = self.parametros or {}
        if p.get("error"):
            raise ErroPublicacao(f"Autorização recusada: {p.get('error_description') or p['error']}", "bloqueio")
        if not secrets.compare_digest(p.get("state", ""), estado):
            raise ErroPublicacao("Resposta de autorização inválida (state não confere)", "bloqueio")
        if not p.get("code"):
            raise ErroPublicacao("A resposta da autorização veio sem código", "bloqueio")
        return p

    def aguardar(self, url_autorizacao: str, estado: str, timeout: float = 300) -> dict[str, str]:
        """Fluxo do terminal: abre o navegador e espera o retorno."""
        self.iniciar()
        print("\nAbrindo o navegador para você autorizar o acesso.")
        print("Se ele não abrir, copie este endereço no navegador:\n")
        print(url_autorizacao + "\n")
        webbrowser.open(url_autorizacao)
        return self.esperar(estado, timeout)


@dataclass
class LoginOAuth:
    """Login em andamento: o painel abre 'url' e espera o retorno no 'servidor'."""

    url: str
    servidor: ServidorRetorno
    verificador: str
    estado: str
