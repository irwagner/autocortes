"""Cliente WebSocket mínimo (RFC 6455), porque a biblioteca padrão não tem um.

Usado em dois lugares: o DevTools do navegador (`ws://`, texto) e a voz da Microsoft
(`wss://`, texto e áudio binário). Só o necessário: texto, binário, ping e continuação.
Sem compressão: a extensão permessage-deflate não é anunciada, então o servidor responde
sem compressão.

Todo erro de rede sai como `ErroWebsocket` (nunca um OSError cru). Depois de um erro no meio
de uma leitura, a conexão é fechada: o fluxo de quadros ficaria fora de sincronia.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
from urllib.parse import urlparse

__all__ = ["ErroWebsocket", "Websocket"]

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # constante do protocolo
TEXTO, BINARIO = 0x1, 0x2
MAX_MENSAGEM = 64 * 1024 * 1024  # uma captura de tela do navegador tem poucos MB; mais que isto é defeito
MAX_CABECALHO = 64 * 1024


class ErroWebsocket(Exception):
    """Falha de conexão, de handshake ou de leitura."""

    def __init__(self, mensagem: str, status: int = 0, cabecalhos: dict | None = None):
        super().__init__(mensagem)
        self.status = status               # status HTTP quando o handshake é recusado
        self.cabecalhos = cabecalhos or {}  # cabeçalhos da recusa (o Date serve para acertar o relógio)


class Websocket:
    def __init__(self, url: str, timeout: float = 60, cabecalhos: dict | None = None, rotulo: str = "o servidor"):
        partes = urlparse(url)
        if partes.scheme not in ("ws", "wss"):
            raise ErroWebsocket(f"endereço inesperado: {url}")
        segura = partes.scheme == "wss"
        porta = partes.port or (443 if segura else 80)
        host = partes.hostname or ""
        caminho = (partes.path or "/") + (f"?{partes.query}" if partes.query else "")
        self.rotulo = rotulo
        self._buffer = b""
        self._fechado = False
        self._sock: socket.socket | None = None
        try:
            self._sock = socket.create_connection((host, porta), timeout=20)
            if segura:
                contexto = ssl.create_default_context()
                self._sock = contexto.wrap_socket(self._sock, server_hostname=host)
        except (OSError, ssl.SSLError) as e:
            self._soltar()
            raise ErroWebsocket(f"não consegui falar com {rotulo} ({e})") from e
        try:
            self._sock.settimeout(timeout)
            try:
                self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            self._handshake(host, porta, caminho, cabecalhos or {})
        except BaseException:
            self._soltar()  # handshake recusado ou caído: o socket não fica aberto à toa
            raise

    def _handshake(self, host: str, porta: int, caminho: str, cabecalhos: dict) -> None:
        chave = base64.b64encode(os.urandom(16)).decode()
        linhas = [
            f"GET {caminho} HTTP/1.1",
            f"Host: {host}" if porta in (80, 443) else f"Host: {host}:{porta}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {chave}",
            "Sec-WebSocket-Version: 13",
        ]
        for nome, valor in cabecalhos.items():
            linhas.append(f"{nome}: {valor}")
        try:
            self._sock.sendall(("\r\n".join(linhas) + "\r\n\r\n").encode("latin-1"))
        except OSError as e:
            raise ErroWebsocket(f"não consegui falar com {self.rotulo} ({e})") from e

        bruto = self._ler_ate(b"\r\n\r\n").decode("latin-1")
        primeira, _, resto = bruto.partition("\r\n")
        recebidos = {}
        for linha in resto.split("\r\n"):
            nome, sep, valor = linha.partition(":")
            if sep:
                recebidos[nome.strip().lower()] = valor.strip()
        if "101" not in primeira:
            partes_status = primeira.split(" ")
            status = int(partes_status[1]) if len(partes_status) > 1 and partes_status[1].isdigit() else 0
            raise ErroWebsocket(f"{self.rotulo} recusou a conexão ({primeira})", status, recebidos)
        esperado = base64.b64encode(hashlib.sha1((chave + GUID).encode()).digest()).decode()
        if recebidos.get("sec-websocket-accept", "").strip() != esperado:
            raise ErroWebsocket(f"resposta inválida de {self.rotulo} (Sec-WebSocket-Accept)")

    def _soltar(self) -> None:
        """Fecha o socket sem conversa (depois de erro)."""
        self._fechado = True
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    # ---- leitura de bytes
    def _receber_bloco(self, tamanho: int) -> bytes:
        if self._fechado or self._sock is None:
            raise ErroWebsocket(f"a conexão com {self.rotulo} já foi fechada")
        try:
            bloco = self._sock.recv(tamanho)
        except (TimeoutError, socket.timeout) as e:  # noqa: UP041 (o ssl ainda levanta socket.timeout)
            self._soltar()
            raise ErroWebsocket(f"{self.rotulo} não respondeu no tempo esperado") from e
        except OSError as e:
            self._soltar()
            raise ErroWebsocket(f"a conexão com {self.rotulo} caiu ({e})") from e
        if not bloco:
            self._soltar()
            raise ErroWebsocket(f"{self.rotulo} encerrou a conexão")
        return bloco

    def _ler_ate(self, marca: bytes) -> bytes:
        while marca not in self._buffer:
            if len(self._buffer) > MAX_CABECALHO:
                raise ErroWebsocket(f"resposta inválida de {self.rotulo} (cabeçalho grande demais)")
            self._buffer += self._receber_bloco(65536)
        cabeca, self._buffer = self._buffer.split(marca, 1)
        return cabeca + marca

    def _ler_exato(self, n: int) -> bytes:
        partes = [self._buffer] if self._buffer else []
        juntos = len(self._buffer)
        while juntos < n:
            bloco = self._receber_bloco(max(65536, n - juntos))
            partes.append(bloco)
            juntos += len(bloco)
        tudo = b"".join(partes)
        dados, self._buffer = tudo[:n], tudo[n:]
        return dados

    # ---- quadros
    def _enviar_quadro(self, opcode: int, dados: bytes) -> None:
        if self._fechado or self._sock is None:
            raise ErroWebsocket(f"a conexão com {self.rotulo} já foi fechada")
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
            self._soltar()
            raise ErroWebsocket(f"não consegui enviar para {self.rotulo} ({e})") from e

    def enviar(self, texto: str) -> None:
        self._enviar_quadro(TEXTO, texto.encode("utf-8"))

    def receber_quadro(self) -> tuple[int, bytes]:
        """Uma mensagem completa: (opcode, dados). Junta continuação e responde aos pings."""
        partes: list[bytes] = []
        total = 0
        tipo = TEXTO
        while True:
            b1, b2 = self._ler_exato(2)
            fim, opcode = b1 & 0x80, b1 & 0x0F
            mascarado, tamanho = b2 & 0x80, b2 & 0x7F
            if tamanho == 126:
                tamanho = int.from_bytes(self._ler_exato(2), "big")
            elif tamanho == 127:
                tamanho = int.from_bytes(self._ler_exato(8), "big")
            if tamanho > MAX_MENSAGEM or total + tamanho > MAX_MENSAGEM:
                self._soltar()
                raise ErroWebsocket(f"{self.rotulo} mandou uma mensagem grande demais ({tamanho} bytes)")
            mascara = self._ler_exato(4) if mascarado else b""
            dados = self._ler_exato(tamanho) if tamanho else b""
            if mascarado:
                dados = bytes(b ^ mascara[i % 4] for i, b in enumerate(dados))
            if opcode == 0x9:  # ping
                self._enviar_quadro(0xA, dados)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # fechamento: os 2 primeiros bytes são o código, o resto é a razão
                codigo = int.from_bytes(dados[:2], "big") if len(dados) >= 2 else 0
                razao = dados[2:].decode("utf-8", "replace").strip()
                try:  # o protocolo pede que o fechamento seja respondido com o mesmo código
                    self._enviar_quadro(0x8, dados[:2])
                except ErroWebsocket:
                    pass
                self._soltar()
                detalhe = f" (código {codigo}{': ' + razao if razao else ''})" if codigo else ""
                raise ErroWebsocket(f"{self.rotulo} fechou a conexão{detalhe}", codigo)
            if opcode in (TEXTO, BINARIO):
                tipo = opcode
            partes.append(dados)
            total += len(dados)
            if fim:
                return tipo, b"".join(partes)

    def receber(self) -> str:
        """Uma mensagem de texto (o que o CDP usa)."""
        tipo, dados = self.receber_quadro()
        if tipo != TEXTO:
            raise ErroWebsocket(f"{self.rotulo} mandou dados binários onde eu esperava texto")
        return dados.decode("utf-8", "replace")

    def fechar(self) -> None:
        if not self._fechado:
            try:
                self._enviar_quadro(0x8, b"")
            except ErroWebsocket:
                pass
        self._soltar()

    def __enter__(self) -> "Websocket":
        return self

    def __exit__(self, *_) -> None:
        self.fechar()
