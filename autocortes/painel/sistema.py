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


# ---------------------------------------------------------------- placa de vídeo

# quanto de VRAM um modelo de linguagem em Q4 pede, por tamanho (GB de VRAM -> sugestão)
SUGESTOES_IA = (
    (14, "14B", "qwen3:14b"),
    (7, "8B", "qwen3:8b"),
    (5, "4B", "qwen3:4b-instruct-2507-q4_K_M"),
    (0, "1,7B", "qwen3:1.7b"),
)


def placas_video() -> list[dict]:
    """Placas de vídeo com a VRAM de verdade, lida do registro.

    O `AdapterRAM` do WMI satura em 4 GB e engana (uma placa de 8 GB aparece como 4).
    """
    if os.name != "nt":
        return []
    import winreg

    chave_classe = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    saida: list[dict] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, chave_classe) as raiz:
            for i in range(64):
                try:
                    nome_sub = winreg.EnumKey(raiz, i)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(raiz, nome_sub) as sub:
                        try:
                            memoria = winreg.QueryValueEx(sub, "HardwareInformation.qwMemorySize")[0]
                        except FileNotFoundError:
                            continue
                        try:
                            nome = str(winreg.QueryValueEx(sub, "DriverDesc")[0])
                        except FileNotFoundError:
                            nome = "placa de vídeo"
                except OSError:
                    continue
                gb = round(int(memoria) / 1024**3, 1)
                if gb >= 0.1:
                    saida.append({"nome": nome, "vram_gb": gb, "integrada": gb < 2})
    except OSError:
        return []
    return sorted(saida, key=lambda p: -p["vram_gb"])


def info_ia() -> dict:
    """Placa dedicada e o tamanho de modelo que cabe nela (para a dica na aba IA)."""
    placas = placas_video()
    dedicada = next((p for p in placas if not p["integrada"]), None)
    if dedicada is None:
        return {"placas": placas, "vram_gb": 0, "cabe": "", "sugestao": ""}
    vram = float(dedicada["vram_gb"])
    tamanho, modelo = next((t, m) for minimo, t, m in SUGESTOES_IA if vram >= minimo)
    return {
        "placas": placas,
        "placa": dedicada["nome"],
        "vram_gb": vram,
        "cabe": tamanho,
        "sugestao": modelo,
    }


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
