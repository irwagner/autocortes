"""Logins das redes guardados com a proteção de dados do Windows (DPAPI).

O arquivo em dados/tokens/ fica cifrado para o usuário do Windows que conectou a conta: outro
usuário, outro PC ou uma cópia da pasta não conseguem abrir. Arquivos antigos, em texto puro,
continuam sendo lidos e passam a ser cifrados na próxima gravação.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path

from .util import log, salvar_json

_MARCA = "dpapi"
_DESCRICAO = "AutoCortes"
_SEM_INTERFACE = 0x1  # CRYPTPROTECT_UI_FORBIDDEN: nunca abre janela pedindo nada
_AVISADOS: set[str] = set()


if os.name == "nt":
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob), ctypes.c_void_p,
                                          ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_Blob),
                                            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    def _chamar(funcao, dados: bytes) -> bytes:
        buffer = ctypes.create_string_buffer(dados, len(dados))
        entrada = _Blob(len(dados), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        saida = _Blob()
        extra = _DESCRICAO if funcao is _crypt32.CryptProtectData else None
        if not funcao(ctypes.byref(entrada), extra, None, None, None, _SEM_INTERFACE, ctypes.byref(saida)):
            raise OSError(ctypes.get_last_error(), "a proteção de dados do Windows recusou")
        try:
            return ctypes.string_at(saida.pbData, saida.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(saida.pbData, ctypes.c_void_p))

    def cifrar(dados: bytes) -> bytes:
        return _chamar(_crypt32.CryptProtectData, dados)

    def decifrar(dados: bytes) -> bytes:
        return _chamar(_crypt32.CryptUnprotectData, dados)

    DISPONIVEL = True
else:  # fora do Windows (não é o uso normal): fica em texto puro
    DISPONIVEL = False

    def cifrar(dados: bytes) -> bytes:
        raise OSError("proteção de dados só existe no Windows")

    def decifrar(dados: bytes) -> bytes:
        raise OSError("proteção de dados só existe no Windows")


def ler_token(caminho: Path) -> dict:
    """O login guardado ({} se não houver ou se não der para abrir neste usuário do Windows)."""
    try:
        bruto = json.loads(caminho.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("Login salvo ilegível em %s: %s", caminho.name, e)
        return {}
    if not isinstance(bruto, dict):
        return {}
    if bruto.get("protegido") != _MARCA:
        if bruto and DISPONIVEL:  # arquivo antigo em texto puro: cifra agora
            try:
                salvar_token(caminho, bruto)
            except OSError as e:
                log.warning("Não consegui cifrar o login salvo em %s: %s", caminho.name, e)
        return bruto
    try:
        dados = json.loads(decifrar(base64.b64decode(bruto["dados"])).decode("utf-8"))
        return dados if isinstance(dados, dict) else {}
    except (OSError, ValueError, KeyError, TypeError) as e:
        if caminho.name not in _AVISADOS:
            _AVISADOS.add(caminho.name)
            log.warning("O login salvo em %s foi guardado por outro usuário do Windows ou em outro PC e não abre "
                        "aqui (%s). Conecte a conta de novo em Redes sociais.", caminho.name, e)
        return {}


def salvar_token(caminho: Path, dados: dict) -> None:
    if not DISPONIVEL:
        salvar_json(caminho, dados)
        return
    cifrado = cifrar(json.dumps(dados, ensure_ascii=False).encode("utf-8"))
    salvar_json(caminho, {"protegido": _MARCA, "dados": base64.b64encode(cifrado).decode("ascii")})
