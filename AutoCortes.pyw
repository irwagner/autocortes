"""Abre o AutoCortes sem janela de console. É o que o "Iniciar com o Windows" usa.

Sem argumentos, liga o painel e o motor em segundo plano, sem abrir o navegador.
Para ver o painel, abra o AutoCortes.bat: ele encontra este processo e só abre o navegador.
"""

from __future__ import annotations

import collections
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent


class _FimDoTexto:
    """Guarda só o fim do que iria para o console (o pythonw não tem console)."""

    def __init__(self) -> None:
        self.partes: collections.deque[str] = collections.deque(maxlen=60)

    def write(self, texto: str) -> int:
        self.partes.append(texto)
        return len(texto)

    def flush(self) -> None:
        pass

    def texto(self) -> str:
        return "".join(self.partes)[-1500:]


def _avisar(mensagem: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, mensagem, "AutoCortes", 0x10)  # caixa com ícone de erro
    except Exception:  # noqa: BLE001 (sem Windows ou sem interface: não há como avisar)
        pass


def main() -> int:
    os.chdir(RAIZ)
    sys.path.insert(0, str(RAIZ))
    saida = _FimDoTexto()
    if sys.stdout is None:
        sys.stdout = saida
    if sys.stderr is None:
        sys.stderr = saida
    argumentos = sys.argv[1:] or ["painel", "--sem-navegador"]
    try:
        from autocortes.cli import main as cli

        codigo = cli(argumentos)
    except SystemExit as e:
        codigo = e.code if isinstance(e.code, int) else 1
    except Exception as e:  # noqa: BLE001 (qualquer falha vira um aviso na tela)
        saida.write(f"{e.__class__.__name__}: {e}")
        codigo = 1
    if codigo:
        detalhe = saida.texto().strip() or r"Veja o registro em dados\logs."
        _avisar(f"O AutoCortes não conseguiu iniciar.\n\n{detalhe}")
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
