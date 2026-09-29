"""Servidor HTTP do painel.

Só escuta em 127.0.0.1. Cada vez que o painel abre, um token aleatório vai
embutido na página; toda chamada da API precisa dele no cabeçalho X-AutoCortes
(e a mídia, no parâmetro ?t=). O cabeçalho Host também é conferido, o que
bloqueia sites de fora tentando falar com o painel pelo navegador.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__, db, modelos_visuais
from ..config import Config
from ..loop import Motor
from ..util import Trava, log
from . import api
from .erros import ErroHttp

ESTATICO = Path(__file__).resolve().parent / "estatico"
TIPOS = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
CSP = (
    "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; font-src 'self'; object-src 'none'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'"
)
LIMITE_JSON = 2 * 1024 * 1024


def _mesmo_token(recebido: str, token: str) -> bool:
    """Compara em bytes: com texto, um acento no token recebido faria o compare_digest dar erro 500."""
    return secrets.compare_digest(str(recebido).encode("utf-8", "surrogateescape"), token.encode("utf-8"))


class Painel:
    """Estado compartilhado entre as requisições: config, motor, token e logins em andamento."""

    def __init__(self, cfg: Config, motor: Motor, porta: int):
        self.cfg = cfg
        self.motor = motor
        self.porta = porta
        self.token = secrets.token_urlsafe(32)
        self.encerrar = threading.Event()
        self.logins: dict[str, api.SessaoLogin] = {}
        self.trava_config = threading.RLock()  # as rotas dos modelos seguram a trava e salvam o config dentro
        self.reinicio: dict[str, object] = {}  # "secao.chave" -> valor salvo que vale ao reiniciar
        self.hosts = {f"127.0.0.1:{porta}", f"localhost:{porta}"}
        self.origens = {f"http://{h}" for h in self.hosts}
        self.iniciado_em = time.time()

    def conectar(self):
        return db.conectar(self.cfg.banco, preparar=False)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.porta}/"


class Servidor(ThreadingHTTPServer):
    daemon_threads = True
    # no Windows, SO_REUSEADDR deixaria outro programa ouvir na mesma porta
    allow_reuse_address = os.name != "nt"

    def __init__(self, endereco, painel: Painel):
        self.painel = painel
        super().__init__(endereco, Tratador)


class Tratador(BaseHTTPRequestHandler):
    server_version = "AutoCortes"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 120
    server: Servidor

    def log_message(self, formato, *args):  # o log do painel fica só no arquivo de debug
        pass

    @property
    def painel(self) -> Painel:
        return self.server.painel

    # ------------------------------------------------------------ respostas
    def _cabecalhos_seguranca(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)

    def enviar(self, status: int, corpo: bytes, tipo: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self._cabecalhos_seguranca()
        self.end_headers()
        self._respondido = True
        if self.command != "HEAD":
            self.wfile.write(corpo)

    def enviar_json(self, status: int, dados) -> None:
        corpo = json.dumps(dados, ensure_ascii=False, default=str).encode("utf-8")
        self.enviar(status, corpo, "application/json; charset=utf-8")

    def enviar_arquivo(self, caminho: Path, tipo: str) -> None:
        """Arquivo com suporte a Range (o player de vídeo do navegador pula para qualquer ponto)."""
        tamanho = caminho.stat().st_size
        inicio, fim, status = 0, tamanho - 1, 200
        faixa = self.headers.get("Range")
        if faixa:
            achado = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", faixa)
            if achado and (achado.group(1) or achado.group(2)):
                if achado.group(1):
                    inicio = int(achado.group(1))
                    fim = int(achado.group(2)) if achado.group(2) else tamanho - 1
                else:
                    inicio = max(0, tamanho - int(achado.group(2)))
                fim = min(fim, tamanho - 1)
                if inicio > fim or inicio >= tamanho:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{tamanho}")
                    self.send_header("Content-Length", "0")
                    self._cabecalhos_seguranca()
                    self.end_headers()
                    self._respondido = True
                    return
                status = 206
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(fim - inicio + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {inicio}-{fim}/{tamanho}")
        self.send_header("Cache-Control", "private, max-age=3600")
        self._cabecalhos_seguranca()
        self.end_headers()
        self._respondido = True
        if self.command == "HEAD":
            return
        with open(caminho, "rb") as arquivo:
            arquivo.seek(inicio)
            restante = fim - inicio + 1
            while restante > 0:
                bloco = arquivo.read(min(512 * 1024, restante))
                if not bloco:
                    break
                self.wfile.write(bloco)
                restante -= len(bloco)

    def ler_json(self) -> dict:
        tamanho = int(self.headers.get("Content-Length") or 0)
        if tamanho > LIMITE_JSON:
            raise ErroHttp(413, "Requisição grande demais")
        bruto = self.rfile.read(tamanho) if tamanho else b""
        if not bruto:
            return {}
        try:
            dados = json.loads(bruto.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ErroHttp(400, "JSON inválido") from e
        if not isinstance(dados, dict):
            raise ErroHttp(400, "JSON inválido")
        return dados

    # ------------------------------------------------------------ páginas
    def _index(self) -> None:
        html = (ESTATICO / "index.html").read_text(encoding="utf-8")
        versao = f"{__version__}-{int(self.painel.iniciado_em)}"
        html = html.replace("{{TOKEN}}", self.painel.token).replace("{{VERSAO}}", versao)
        self.enviar(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _estatico(self, nome: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.[a-z0-9]+", nome):
            raise ErroHttp(404, "Arquivo não encontrado")
        caminho = ESTATICO / nome
        if not caminho.is_file():
            raise ErroHttp(404, "Arquivo não encontrado")
        self.enviar(200, caminho.read_bytes(), TIPOS.get(caminho.suffix, "application/octet-stream"))

    # ------------------------------------------------------------ roteamento
    def do_GET(self):  # noqa: N802
        self._tratar()

    def do_HEAD(self):  # noqa: N802
        self._tratar()

    def do_POST(self):  # noqa: N802
        self._tratar()

    def _tratar(self) -> None:
        self._respondido = False
        metodo = "GET" if self.command == "HEAD" else self.command
        try:
            if self.headers.get("Host", "") not in self.painel.hosts:
                raise ErroHttp(421, "Endereço não permitido")
            url = urlparse(self.path)
            caminho = url.path
            consulta = {k: v[-1] for k, v in parse_qs(url.query).items()}

            if metodo == "GET":
                if caminho in ("/", "/index.html"):
                    return self._index()
                if caminho == "/saude":
                    return self.enviar_json(200, {"app": "autocortes", "versao": __version__})
                if caminho.startswith("/static/"):
                    return self._estatico(caminho[len("/static/"):])
                if caminho == "/favicon.ico":
                    return self._estatico("icone.svg")

            if caminho.startswith("/media/"):
                if metodo != "GET":
                    raise ErroHttp(405, "Método não permitido")
                if not _mesmo_token(consulta.get("t", ""), self.painel.token):
                    raise ErroHttp(403, "Sessão inválida")
                return api.midia(self.painel, unquote(caminho[len("/media/"):]), self)

            if caminho.startswith("/api/"):
                if not _mesmo_token(self.headers.get("X-AutoCortes", ""), self.painel.token):
                    raise ErroHttp(403, "Sessão do painel inválida. Recarregue a página.")
                origem = self.headers.get("Origin")
                if origem and origem not in self.painel.origens:
                    raise ErroHttp(403, "Origem não permitida")
                encontrado = api.encontrar(metodo, caminho[len("/api"):])
                if encontrado is None:
                    raise ErroHttp(404, "Rota não encontrada")
                funcao, grupos, bruto = encontrado
                corpo = {} if (bruto or metodo == "GET") else self.ler_json()
                ctx = api.Contexto(self.painel, corpo, consulta, self)
                try:
                    resultado = funcao(ctx, *grupos)
                finally:
                    ctx.fechar()
                if not self._respondido:
                    self.enviar_json(200, {"ok": True} if resultado is None else resultado)
                return
            raise ErroHttp(404, "Página não encontrada")
        except ErroHttp as e:
            self.close_connection = True
            if not self._respondido:
                self.enviar_json(e.status, {"erro": str(e), **e.extra})
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception as e:
            log.exception("Erro no painel (%s %s)", self.command, self.path.split("?")[0])
            self.close_connection = True
            if not self._respondido:
                self.enviar_json(500, {"erro": f"Erro interno: {e}"})


# ---------------------------------------------------------------- inicialização

def painel_responde(porta: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{porta}/saude", timeout=2) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == "autocortes"
    except (OSError, ValueError):
        return False


def iniciar_painel(cfg: Config, abrir_navegador: bool = True, iniciar_motor: bool = True,
                   porta: int | None = None) -> int:
    porta = int(porta or cfg["painel"]["porta"])
    url = f"http://127.0.0.1:{porta}/"
    trava = Trava(cfg.pasta_dados / "autocortes.lock")
    if not trava.adquirir():
        if painel_responde(porta):
            log.info("O AutoCortes já está aberto em %s", url)
            if abrir_navegador:
                webbrowser.open(url)
            return 0
        log.error("Já existe um AutoCortes rodando com esta pasta de dados (talvez no modo terminal). "
                  "Feche-o antes de abrir o painel.")
        return 1

    conn = db.conectar(cfg.banco)  # cria/migra o banco uma vez
    db.recuperar_estados_pendentes(conn)
    conn.close()
    modelos_visuais.conciliar_config(cfg)  # nome do modelo trocado à mão no config.toml
    motor = Motor(cfg)
    painel = Painel(cfg, motor, porta)
    try:
        servidor = Servidor(("127.0.0.1", porta), painel)
    except OSError as e:
        log.error("Não consegui abrir a porta %d (%s). Troque [painel].porta no config.toml.", porta, e)
        trava.liberar()
        return 1

    threading.Thread(target=servidor.serve_forever, kwargs={"poll_interval": 0.5}, name="painel", daemon=True).start()
    log.info("Painel do AutoCortes em %s (feche esta janela para desligar)", url)
    if iniciar_motor:
        motor.iniciar()
    if abrir_navegador:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        while not painel.encerrar.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Encerrando o AutoCortes...")
        servidor.shutdown()
        servidor.server_close()
        for sessao in list(painel.logins.values()):
            sessao.cancelar()
        motor.parar()
        trava.liberar()
    return 0
