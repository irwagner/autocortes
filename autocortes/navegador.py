"""Controle do Chrome pelo protocolo do DevTools (CDP), para postar pelo navegador.

O AutoCortes abre o Chrome com um perfil separado, guardado em dados/chrome. Você loga
uma vez em cada rede nessa janela e a sessão fica ali entre reinícios. Depois, o envio é
feito clicando na própria página de postagem da rede, como se fosse você.

Só escuta em 127.0.0.1. Nada de forjar fingerprint, resolver captcha, API privada ou proxy:
se a rede bloquear, o envio para e avisa.

O CDP fala WebSocket, que a biblioteca padrão do Python não tem, então o cliente mínimo
(RFC 6455) está aqui mesmo, para não precisar de dependência nova.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from .config import Config
from .util import log

# uma aba por vez: as redes dividem a mesma janela do navegador
TRAVA = threading.Lock()
GUID_WS = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # constante do protocolo WebSocket
PROGRAMAS = (
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
)


class ErroNavegador(Exception):
    """Falha ao controlar o navegador (com o tipo que o publicador entende)."""

    def __init__(self, mensagem: str, tipo: str = "temporario"):
        super().__init__(mensagem)
        self.tipo = tipo


# ---------------------------------------------------------------- WebSocket

class _Websocket:
    """Cliente WebSocket mínimo: só o que o CDP usa (texto, ping e continuação)."""

    def __init__(self, url: str, timeout: float = 60):
        partes = urlparse(url)
        if partes.scheme != "ws":
            raise ErroNavegador(f"endereço do DevTools inesperado: {url}")
        porta = partes.port or 80
        caminho = partes.path + (f"?{partes.query}" if partes.query else "")
        try:
            self._sock = socket.create_connection((partes.hostname, porta), timeout=15)
        except OSError as e:
            raise ErroNavegador(f"não consegui falar com o navegador ({e})") from e
        self._sock.settimeout(timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._buffer = b""
        chave = base64.b64encode(os.urandom(16)).decode()
        pedido = (
            f"GET {caminho} HTTP/1.1\r\n"
            f"Host: {partes.hostname}:{porta}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {chave}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(pedido.encode("ascii"))
        cabecalhos = self._ler_ate(b"\r\n\r\n").decode("latin-1")
        primeira = cabecalhos.split("\r\n", 1)[0]
        if "101" not in primeira:
            raise ErroNavegador(f"o navegador recusou a conexão do DevTools ({primeira})")
        esperado = base64.b64encode(hashlib.sha1((chave + GUID_WS).encode()).digest()).decode()
        if esperado.lower() not in cabecalhos.lower():
            raise ErroNavegador("resposta inválida do DevTools (Sec-WebSocket-Accept)")

    # ---- leitura de bytes
    def _ler_ate(self, marca: bytes) -> bytes:
        while marca not in self._buffer:
            bloco = self._sock.recv(65536)
            if not bloco:
                raise ErroNavegador("o navegador encerrou a conexão")
            self._buffer += bloco
        cabeca, self._buffer = self._buffer.split(marca, 1)
        return cabeca + marca

    def _ler_exato(self, n: int) -> bytes:
        while len(self._buffer) < n:
            bloco = self._sock.recv(max(65536, n - len(self._buffer)))
            if not bloco:
                raise ErroNavegador("o navegador encerrou a conexão")
            self._buffer += bloco
        dados, self._buffer = self._buffer[:n], self._buffer[n:]
        return dados

    # ---- quadros
    def _enviar_quadro(self, opcode: int, dados: bytes) -> None:
        cabeca = bytes([0x80 | opcode])
        n = len(dados)
        if n < 126:
            cabeca += bytes([0x80 | n])
        elif n < 65536:
            cabeca += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            cabeca += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mascara = os.urandom(4)  # o cliente é obrigado a mascarar
        corpo = bytes(b ^ mascara[i % 4] for i, b in enumerate(dados))
        try:
            self._sock.sendall(cabeca + mascara + corpo)
        except OSError as e:
            raise ErroNavegador(f"não consegui enviar ao navegador ({e})") from e

    def enviar(self, texto: str) -> None:
        self._enviar_quadro(0x1, texto.encode("utf-8"))

    def receber(self) -> str:
        """Uma mensagem completa (junta os quadros de continuação e responde aos pings)."""
        partes: list[bytes] = []
        while True:
            try:
                b1, b2 = self._ler_exato(2)
            except socket.timeout as e:
                raise ErroNavegador("o navegador não respondeu no tempo esperado") from e
            fim, opcode = b1 & 0x80, b1 & 0x0F
            mascarado, tamanho = b2 & 0x80, b2 & 0x7F
            if tamanho == 126:
                tamanho = int.from_bytes(self._ler_exato(2), "big")
            elif tamanho == 127:
                tamanho = int.from_bytes(self._ler_exato(8), "big")
            mascara = self._ler_exato(4) if mascarado else b""
            dados = self._ler_exato(tamanho) if tamanho else b""
            if mascarado:
                dados = bytes(b ^ mascara[i % 4] for i, b in enumerate(dados))
            if opcode == 0x9:  # ping do navegador
                self._enviar_quadro(0xA, dados)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:
                raise ErroNavegador("o navegador fechou a conexão do DevTools")
            partes.append(dados)
            if fim:
                return b"".join(partes).decode("utf-8", "replace")

    def fechar(self) -> None:
        try:
            self._enviar_quadro(0x8, b"")
        except ErroNavegador:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- aba

# Ajudante injetado na página. Faz busca profunda, entrando no shadow DOM: o YouTube Studio
# monta quase tudo assim (Polymer), e um querySelector comum não acha nada lá dentro.
AJUDANTE = r"""
window.__ac = (() => {
  const visivel = (el) => {
    if (!el) return false;
    if (el.tagName === "INPUT" && el.type === "file") return true;  // campo de arquivo costuma ficar escondido
    const e = getComputedStyle(el);
    if (e.visibility === "hidden" || e.display === "none" || Number(e.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width + r.height > 0;
  };
  const raizes = () => {
    const achadas = [document];
    const fila = [document];
    while (fila.length) {
      const raiz = fila.shift();
      for (const el of raiz.querySelectorAll("*")) {
        if (el.shadowRoot) { achadas.push(el.shadowRoot); fila.push(el.shadowRoot); }
      }
    }
    return achadas;
  };
  const todos = (sel) => {
    const saida = [];
    for (const raiz of raizes()) {
      try { for (const el of raiz.querySelectorAll(sel)) saida.push(el); } catch (e) { /* seletor inválido */ }
    }
    return saida;
  };
  const achar = (sel) => todos(sel).find(visivel) || todos(sel)[0] || null;
  const limpar = (t) => String(t || "").replace(/\s+/g, " ").trim().toLowerCase();
  const porTexto = (sel, textos) => {
    const lista = (Array.isArray(textos) ? textos : [textos]).map(limpar);
    return todos(sel).filter(visivel).find((el) => {
      const t = limpar(el.innerText || el.textContent || el.getAttribute("aria-label"));
      return lista.some((x) => x && t.includes(x));
    }) || null;
  };
  const clicar = (el) => {
    if (!el) return false;
    el.scrollIntoView({ block: "center" });
    el.click();
    return true;
  };
  return {
    achar, todos, porTexto, visivel,
    existe: (sel) => !!achar(sel),
    clicarSel: (sel) => clicar(achar(sel)),
    clicarTexto: (sel, textos) => clicar(porTexto(sel, textos)),
    // quadros de outro site (iframe) não entram na busca: os roteiros avisam quando aparecem
    iframes: () => [...document.querySelectorAll("iframe")].map((f) => f.getAttribute("src") || "(sem src)"),
    // junta o texto do shadow DOM: o innerText nem sempre alcança o que está dentro dele
    texto: () => {
      let t = document.body ? document.body.innerText : "";
      for (const raiz of raizes()) { if (raiz !== document) t += " " + (raiz.textContent || ""); }
      return limpar(t);
    },
    valor: (sel) => { const el = achar(sel); return el ? (el.isContentEditable ? el.innerText : el.value) : null; },
  };
})();
1
"""


class Aba:
    """Uma aba do navegador: comandos do CDP e os passos que os roteiros usam."""

    def __init__(self, ws: _Websocket, parar: threading.Event | None = None, ritmo: tuple[float, float] = (0.4, 1.2)):
        self._ws = ws
        self._id = 0
        self._eventos: list[dict] = []
        self.parar = parar
        self.ritmo = ritmo
        self.comando("Page.enable")
        self.comando("Runtime.enable")
        self.comando("DOM.enable")

    # ---- CDP
    def comando(self, metodo: str, **params) -> dict:
        self._id += 1
        self._ws.enviar(json.dumps({"id": self._id, "method": metodo, "params": params}))
        while True:
            bruto = self._ws.receber()
            try:
                msg = json.loads(bruto)
            except ValueError:
                continue
            if msg.get("id") != self._id:
                if msg.get("method"):
                    self._eventos.append(msg)
                    del self._eventos[:-200]
                continue
            if "error" in msg:
                erro = msg["error"] or {}
                raise ErroNavegador(f"{metodo}: {erro.get('message') or erro}")
            return msg.get("result") or {}

    def avaliar(self, expressao: str, promessa: bool = False, objeto: bool = False):
        r = self.comando("Runtime.evaluate", expression=expressao, returnByValue=not objeto,
                         awaitPromise=promessa, userGesture=True)
        if r.get("exceptionDetails"):
            detalhe = r["exceptionDetails"]
            excecao = detalhe.get("exception") or {}
            raise ErroNavegador(f"erro na página: {excecao.get('description') or detalhe.get('text')}")
        resultado = r.get("result") or {}
        return resultado if objeto else resultado.get("value")

    def _garantir_ajudante(self) -> None:
        """Injeta o ajudante de busca profunda (perdido a cada navegação)."""
        if not self.avaliar("typeof window.__ac !== 'undefined' ? 1 : 0"):
            self.avaliar(AJUDANTE)

    def js(self, corpo: str, promessa: bool = False):
        """Roda JS com o ajudante disponível (use __ac.achar, __ac.porTexto...)."""
        self._garantir_ajudante()
        return self.avaliar(corpo, promessa)

    # ---- ritmo e espera
    def pausa(self, fator: float = 1.0) -> None:
        """Espera um tempo aleatório entre os passos (ritmo humano, não é disfarce)."""
        import random

        segundos = random.uniform(*self.ritmo) * fator
        if self.parar is not None and self.parar.wait(segundos):
            raise ErroNavegador("interrompido pelo usuário")
        elif self.parar is None:
            time.sleep(segundos)

    def esperar(self, expressao: str, segundos: float = 30, o_que: str = ""):
        """Espera a expressão JS virar verdadeira e devolve o valor dela."""
        limite = time.monotonic() + segundos
        ultimo = None
        while time.monotonic() < limite:
            if self.parar is not None and self.parar.is_set():
                raise ErroNavegador("interrompido pelo usuário")
            try:
                ultimo = self.js(expressao)
            except ErroNavegador:
                ultimo = None  # a página pode estar trocando
            if ultimo:
                return ultimo
            time.sleep(0.4)
        raise ErroNavegador(f"a página não chegou ao esperado{f': {o_que}' if o_que else ''}")

    # ---- ações
    def navegar(self, url: str, segundos: float = 45) -> None:
        self.comando("Page.navigate", url=url)
        self.esperar("document.readyState === 'complete' || document.readyState === 'interactive'",
                     segundos, "a página não abriu")
        self._garantir_ajudante()
        self.pausa()

    def url(self) -> str:
        return str(self.avaliar("location.href") or "")

    def texto(self) -> str:
        return str(self.js("__ac.texto()") or "")

    def iframes(self) -> list[str]:
        """Quadros de outro endereço na página: a busca não entra neles, então os roteiros avisam."""
        return list(self.js("__ac.iframes()") or [])

    def existe(self, seletor: str) -> bool:
        return bool(self.js(f"__ac.existe({json.dumps(seletor)})"))

    def primeiro(self, seletores, segundos: float = 30, o_que: str = "") -> str:
        """O primeiro seletor da lista que aparecer na página (os sites mudam de layout)."""
        lista = [seletores] if isinstance(seletores, str) else list(seletores)
        achado = self.esperar(
            "(() => { for (const s of " + json.dumps(lista) + ") { if (__ac.existe(s)) return s; } return 0; })()",
            segundos, o_que or " ou ".join(lista),
        )
        return str(achado)

    def esperar_seletor(self, seletor, segundos: float = 30, o_que: str = "") -> str:
        return self.primeiro(seletor, segundos, o_que)

    def clicar(self, seletor, segundos: float = 30) -> None:
        alvo = self.primeiro(seletor, segundos)
        if not self.js(f"__ac.clicarSel({json.dumps(alvo)})"):
            raise ErroNavegador(f"não consegui clicar em {alvo}")
        self.pausa()

    def clicar_texto(self, seletor: str, textos, segundos: float = 30) -> None:
        """Clica pelo texto do botão (mais estável que a classe, que muda a cada versão do site)."""
        lista = [textos] if isinstance(textos, str) else list(textos)
        self.esperar(f"__ac.porTexto({json.dumps(seletor)}, {json.dumps(lista)}) ? 1 : 0", segundos,
                     f"botão {' ou '.join(lista)}")
        if not self.js(f"__ac.clicarTexto({json.dumps(seletor)}, {json.dumps(lista)})"):
            raise ErroNavegador(f"não consegui clicar no botão {' ou '.join(lista)}")
        self.pausa()

    def valor(self, seletor: str):
        return self.js(f"__ac.valor({json.dumps(seletor)})")

    def tecla(self, nome: str = "Enter") -> None:
        """Tecla de verdade no campo com foco (campos de tag confirmam com Enter)."""
        teclas = {"Enter": (13, "Enter", "\r"), "Tab": (9, "Tab", None), "Escape": (27, "Escape", None)}
        codigo, chave, texto = teclas.get(nome, (13, "Enter", "\r"))
        comum = {"key": chave, "code": chave, "windowsVirtualKeyCode": codigo, "nativeVirtualKeyCode": codigo}
        self.comando("Input.dispatchKeyEvent", type="rawKeyDown", **comum)
        if texto:
            self.comando("Input.dispatchKeyEvent", type="char", text=texto, **comum)
        self.comando("Input.dispatchKeyEvent", type="keyUp", **comum)
        self.pausa(0.5)

    def digitar(self, seletor, texto: str, segundos: float = 30) -> None:
        """Escreve num campo ou num div editável, disparando os eventos que o site espera."""
        alvo = self.primeiro(seletor, segundos)
        self.js(
            f"(() => {{ const el = __ac.achar({json.dumps(alvo)}); if (!el) return 0;"
            " el.scrollIntoView({block: 'center'});"
            " if (el.isContentEditable) { el.focus(); document.execCommand('selectAll', false, null);"
            "   document.execCommand('delete', false, null); }"
            " else { el.focus(); el.select && el.select(); }"
            " return 1; })()"
        )
        if texto:
            self.comando("Input.insertText", text=texto)
        self.js(
            f"(() => {{ const el = __ac.achar({json.dumps(alvo)}); if (!el) return 0;"
            " el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('change', {bubbles: true})); return 1; })()"
        )
        self.pausa()

    def enviar_arquivo(self, seletor, arquivo: Path, segundos: float = 30) -> None:
        """Entrega o arquivo ao <input type=file> da página (mesmo escondido ou no shadow DOM)."""
        if not Path(arquivo).is_file():
            # o Chrome aceita caminho inexistente sem reclamar, e o envio falharia lá na frente
            raise ErroNavegador(f"o vídeo não está mais em {arquivo}", "corte")
        alvo = self.primeiro(seletor, segundos, "campo de arquivo")
        # pelo objeto, e não por DOM.querySelector: este acha também o que está dentro do shadow DOM
        self._garantir_ajudante()
        objeto = self.avaliar(f"__ac.achar({json.dumps(alvo)})", objeto=True)
        id_objeto = objeto.get("objectId") if isinstance(objeto, dict) else None
        if not id_objeto:
            raise ErroNavegador(f"o campo de arquivo não virou objeto ({alvo}): {objeto}")
        try:
            self.comando("DOM.getDocument", depth=0)  # sem isto o DOM não sabe mapear o objeto
            no = self.comando("DOM.requestNode", objectId=id_objeto).get("nodeId")
            if not no:
                raise ErroNavegador(f"não consegui apontar o campo de arquivo ({alvo})")
            self.comando("DOM.setFileInputFiles", nodeId=no, files=[str(arquivo.resolve())])
        finally:
            try:
                self.comando("Runtime.releaseObject", objectId=id_objeto)
            except ErroNavegador:
                pass
        self.pausa(2.0)

    def captura(self, destino: Path) -> Path | None:
        """Imagem da aba, para você ver onde o envio parou."""
        try:
            dados = self.comando("Page.captureScreenshot", format="png")["data"]
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(base64.b64decode(dados))
            return destino
        except (ErroNavegador, ValueError, OSError):
            return None

    def fechar(self) -> None:
        self._ws.fechar()


# ---------------------------------------------------------------- navegador

def caminho_programa(cfg: Config) -> Path:
    escolhido = str(cfg["navegador"]["programa"]).strip()
    if escolhido:
        caminho = Path(escolhido)
        if not caminho.exists():
            raise ErroNavegador(f"não encontrei o navegador em {caminho}", "bloqueio")
        return caminho
    for caminho in PROGRAMAS:
        if str(caminho) and caminho.exists():
            return caminho
    achado = shutil.which("chrome") or shutil.which("msedge")
    if achado:
        return Path(achado)
    raise ErroNavegador(
        "não encontrei o Chrome nem o Edge. Informe o caminho em Redes sociais > Postagem pelo navegador",
        "bloqueio",
    )


class Navegador:
    """Abre (ou reaproveita) a janela do Chrome com o perfil do AutoCortes."""

    def __init__(self, cfg: Config, parar: threading.Event | None = None):
        self.cfg = cfg
        self.parar = parar
        self.conf = cfg["navegador"]
        self.porta = int(self.conf["porta"])
        self.perfil = cfg.pasta_dados / "chrome"
        self.base = f"http://127.0.0.1:{self.porta}"

    # ---- processo
    def responde(self) -> bool:
        try:
            r = requests.get(f"{self.base}/json/version", timeout=3)
            return r.status_code == 200 and "Browser" in r.json()
        except (requests.RequestException, ValueError):
            return False

    def abrir(self, esperar_seg: float = 40) -> None:
        """Sobe a janela se ela não estiver aberta (a sessão fica no perfil, em dados/chrome)."""
        if self.responde():
            return
        programa = caminho_programa(self.cfg)
        self.perfil.mkdir(parents=True, exist_ok=True)
        argumentos = [
            str(programa),
            f"--remote-debugging-port={self.porta}",
            "--remote-debugging-address=127.0.0.1",  # nunca 0.0.0.0
            f"--user-data-dir={self.perfil}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--restore-last-session=false",
            "about:blank",
        ]
        if not self.conf["visivel"]:
            argumentos.insert(1, "--headless=new")
        log.info("Abrindo o navegador do AutoCortes (%s) com o perfil %s", programa.name, self.perfil)
        try:
            subprocess.Popen(
                argumentos, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except OSError as e:
            raise ErroNavegador(f"não consegui abrir o navegador ({e})", "bloqueio") from e
        limite = time.monotonic() + esperar_seg
        while time.monotonic() < limite:
            if self.parar is not None and self.parar.is_set():
                raise ErroNavegador("interrompido pelo usuário")
            if self.responde():
                return
            time.sleep(0.5)
        raise ErroNavegador(
            f"o navegador não respondeu na porta {self.porta}. Se você já usa essa porta, troque em "
            "Redes sociais > Postagem pelo navegador", "bloqueio",
        )

    def _ws_navegador(self) -> str:
        try:
            return str(requests.get(f"{self.base}/json/version", timeout=5).json()["webSocketDebuggerUrl"])
        except (requests.RequestException, ValueError, KeyError) as e:
            raise ErroNavegador(f"não consegui falar com o navegador ({e})") from e

    def nova_aba(self, url: str = "about:blank") -> Aba:
        """Abre uma aba e devolve o controle dela."""
        self.abrir()
        principal = _Websocket(self._ws_navegador(), timeout=30)
        try:
            principal.enviar(json.dumps({"id": 1, "method": "Target.createTarget", "params": {"url": url}}))
            alvo = None
            for _ in range(20):
                msg = json.loads(principal.receber())
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise ErroNavegador(f"não consegui abrir a aba ({msg['error'].get('message')})")
                    alvo = (msg.get("result") or {}).get("targetId")
                    break
            if not alvo:
                raise ErroNavegador("o navegador não devolveu a aba")
        finally:
            principal.fechar()
        ritmo = (float(self.conf["pausa_min_seg"]), max(float(self.conf["pausa_min_seg"]),
                                                        float(self.conf["pausa_max_seg"])))
        aba = Aba(_Websocket(f"ws://127.0.0.1:{self.porta}/devtools/page/{alvo}",
                             timeout=float(self.conf["tempo_limite_seg"])), self.parar, ritmo)
        aba._alvo = alvo
        return aba

    def fechar_aba(self, aba: Aba) -> None:
        alvo = getattr(aba, "_alvo", None)
        aba.fechar()
        if alvo:
            try:
                requests.get(f"{self.base}/json/close/{alvo}", timeout=5)
            except requests.RequestException:
                pass

    def encerrar(self) -> None:
        """Fecha a janela do navegador (a sessão continua salva no perfil)."""
        try:
            ws = _Websocket(self._ws_navegador(), timeout=5)
        except ErroNavegador:
            return
        try:
            ws.enviar(json.dumps({"id": 1, "method": "Browser.close", "params": {}}))
            ws.receber()
        except ErroNavegador:
            pass
        finally:
            ws.fechar()
