"""Integração com o Windows: abrir pastas, iniciar junto com o sistema e fontes disponíveis."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ..config import RAIZ, Config

CHAVE_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
NOME_RUN = "AutoCortes"

# fontes grossas que costumam vir no Windows/Office (a família já é "pesada",
# então o estilo da legenda não precisa pedir negrito)
FONTES_CONHECIDAS = (
    ("ariblk.ttf", "Arial Black"),
    ("impact.ttf", "Impact"),
    ("seguibl.ttf", "Segoe UI Black"),
    ("FRAHV.TTF", "Franklin Gothic Heavy"),
    ("COOPBL.TTF", "Cooper Black"),
    ("BRLNSDB.TTF", "Berlin Sans FB Demi"),
    ("ROCKEB.TTF", "Rockwell Extra Bold"),
)


def pasta_fontes_sistema() -> Path:
    return Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def fontes_disponiveis(cfg: Config) -> list[dict]:
    pasta = pasta_fontes_sistema()
    saida = []
    for arquivo, nome in FONTES_CONHECIDAS:
        caminho = pasta / arquivo
        if caminho.exists():
            saida.append({"arquivo": caminho.as_posix(), "nome": nome})
    atual = str(cfg["edicao"]["fonte_arquivo"])
    if not any(Path(f["arquivo"]) == Path(atual) for f in saida):
        saida.append({"arquivo": atual, "nome": str(cfg["edicao"]["fonte_nome"]), "personalizada": True})
    return saida


def abrir_pasta(pasta: Path) -> None:
    if os.name != "nt":
        raise OSError("disponível só no Windows")
    pasta.mkdir(parents=True, exist_ok=True)
    os.startfile(str(pasta))  # noqa: S606 (pasta fixa do próprio programa)


def mostrar_arquivo(caminho: Path) -> None:
    """Abre o Explorer com o arquivo selecionado."""
    if os.name != "nt":
        raise OSError("disponível só no Windows")
    if caminho.exists():
        subprocess.Popen(["explorer", "/select,", str(caminho)])
    else:
        abrir_pasta(caminho.parent)


# ---------------------------------------------------------------- iniciar com o Windows

def comando_inicializacao() -> str:
    pythonw = RAIZ / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw}" "{RAIZ / "AutoCortes.pyw"}"'


def inicializacao_ativa() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHAVE_RUN) as chave:
            valor, _ = winreg.QueryValueEx(chave, NOME_RUN)
        return bool(valor)
    except OSError:
        return False


def definir_inicializacao(ativo: bool) -> None:
    """Liga/desliga o AutoCortes na entrada 'Executar' do usuário (não precisa de administrador)."""
    if os.name != "nt":
        raise OSError("disponível só no Windows")
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHAVE_RUN, 0, winreg.KEY_SET_VALUE) as chave:
        if ativo:
            winreg.SetValueEx(chave, NOME_RUN, 0, winreg.REG_SZ, comando_inicializacao())
        else:
            try:
                winreg.DeleteValue(chave, NOME_RUN)
            except FileNotFoundError:
                pass
