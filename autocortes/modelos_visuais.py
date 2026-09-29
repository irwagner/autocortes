"""Modelos visuais: conjuntos com nome das opções de visual do [edicao] (moldura, vídeo, título, legenda...).

O modelo em uso é o próprio [edicao] do config.toml. Os outros ficam guardados em
dados/modelos_visuais.json e, ao trocar de modelo, os valores dele vão para o [edicao].
O arquivo também guarda qual era o modelo em uso: assim, se o nome for trocado à mão no
config.toml, a abertura do painel sabe se foi uma troca de modelo ou só um nome novo.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

from .config import PADRAO, Config, ErroConfig, salvar
from .util import log

# o que um modelo guarda (o resto do [edicao] é qualidade e áudio, igual para todos)
CHAVES = (
    "moldura", "layout", "zoom", "posicao_video", "desfoque_fundo", "escurecer_fundo",
    "legendas", "palavras_por_bloco", "max_caracteres_bloco", "maiusculas", "fonte_arquivo", "fonte_nome",
    "tamanho_legenda", "cor_legenda", "cor_destaque", "contorno_legenda", "legenda_lugar", "posicao_legenda",
    "texto_topo", "tamanho_topo", "cor_topo", "fonte_topo_arquivo", "fonte_topo_nome", "posicao_topo",
    "barra_progresso", "cor_barra", "fade",
)
_NOME = re.compile(r"[^\\/:*?\"<>|\x00-\x1f]{1,60}")
_TRAVA = threading.RLock()


class ErroModelo(Exception):
    def __init__(self, mensagem: str, status: int = 400):
        super().__init__(mensagem)
        self.status = status  # código HTTP da resposta do painel


def arquivo(cfg: Config) -> Path:
    return cfg.pasta_dados / "modelos_visuais.json"


def nome_ativo(cfg: Config) -> str:
    return str(cfg["edicao"]["modelo"]).strip()


def visual_de(cfg: Config) -> dict:
    return {k: copy.deepcopy(cfg["edicao"][k]) for k in CHAVES}


def validar_nome(nome) -> str:
    nome = re.sub(r"\s+", " ", str(nome or "")).strip()
    if not _NOME.fullmatch(nome):
        raise ErroModelo("O nome do modelo precisa ter de 1 a 60 caracteres, sem \\ / : * ? \" < > |")
    return nome


def _valor_ok(valor, padrao):
    """O valor guardado, se tiver o tipo certo; senão, o padrão (arquivo editado à mão)."""
    if isinstance(padrao, bool):
        return valor if isinstance(valor, bool) else padrao
    if isinstance(padrao, (int, float)):
        return valor if isinstance(valor, (int, float)) and not isinstance(valor, bool) else padrao
    if isinstance(padrao, str):
        return valor if isinstance(valor, str) else padrao
    return copy.deepcopy(valor)


def _ler_tudo(cfg: Config) -> tuple[str | None, dict[str, dict]]:
    """(modelo em uso gravado no arquivo, modelos)."""
    caminho = arquivo(cfg)
    try:
        bruto = json.loads(caminho.read_text(encoding="utf-8-sig"))
        if not isinstance(bruto, dict) or not isinstance(bruto.get("modelos"), dict):
            raise ValueError("formato inesperado")
    except FileNotFoundError:
        return None, {}
    except ValueError as e:
        # arquivo estragado (edição à mão, queda de energia): guarda uma cópia em vez de gravar por cima
        copia = caminho.with_name(f"{caminho.stem}.invalido-{time.strftime('%Y%m%d-%H%M%S')}.json")
        try:
            os.replace(caminho, copia)
        except OSError as erro:
            raise ErroModelo(f"{caminho.name} está com problema ({e}) e não consegui guardar uma cópia: {erro}",
                             500) from erro
        log.warning("%s estava ilegível (%s): guardei como %s, e os modelos recomeçam do visual atual",
                    caminho.name, e, copia.name)
        return None, {}
    except OSError as e:  # bloqueado por outro programa: melhor falhar do que perder os modelos
        raise ErroModelo(f"Não consegui ler {caminho.name}: {e}", 500) from e
    padrao = PADRAO["edicao"]
    modelos = {}
    for nome, valores in bruto["modelos"].items():
        if isinstance(valores, dict):
            # opções que um modelo antigo não tinha ficam com o valor padrão
            modelos[str(nome)] = {k: _valor_ok(valores.get(k, padrao[k]), padrao[k]) for k in CHAVES}
    ativo = bruto.get("ativo")
    return (ativo if isinstance(ativo, str) else None), modelos


def _gravar(cfg: Config, modelos: dict[str, dict], ativo: str) -> None:
    caminho = arquivo(cfg)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_suffix(".tmp")
    with open(temporario, "w", encoding="utf-8") as saida:
        json.dump({"versao": 1, "ativo": ativo, "modelos": modelos}, saida, ensure_ascii=False, indent=1)
        saida.flush()
        os.fsync(saida.fileno())  # numa queda de energia fica o arquivo antigo ou o novo, nunca um vazio
    os.replace(temporario, caminho)


def listar(cfg: Config) -> dict[str, dict]:
    """Todos os modelos; o modelo em uso sempre com o visual atual do config."""
    with _TRAVA:
        gravado, modelos = _ler_tudo(cfg)
        ativo, atual = nome_ativo(cfg), visual_de(cfg)
        if gravado and gravado != ativo and gravado in modelos:
            if ativo in modelos and modelos[ativo] != atual:
                # o nome no config.toml mudou para outro modelo, mas o visual ainda é o do anterior:
                # não grava nada para não perder nenhum dos dois (a abertura do painel faz a troca)
                return modelos
            if ativo not in modelos:  # só o nome mudou: é o mesmo modelo com outro nome
                modelos = {(ativo if n == gravado else n): v for n, v in modelos.items()}
        if modelos.get(ativo) != atual or gravado != ativo:
            modelos[ativo] = atual
            _gravar(cfg, modelos, ativo)
        return modelos


def sincronizar(cfg: Config) -> None:
    """Depois de salvar o visual: o modelo em uso passa a guardar os valores atuais."""
    listar(cfg)


def conciliar(cfg: Config, aplicar: Callable[[dict], None]) -> None:
    """Na abertura: se o nome do modelo foi trocado à mão no config.toml, faz a troca de verdade.

    aplicar(valores) grava os valores do modelo escolhido no [edicao]. Um nome que não existe
    vira o novo nome do modelo que estava em uso.
    """
    with _TRAVA:
        gravado, modelos = _ler_tudo(cfg)
        ativo = nome_ativo(cfg)
        if not gravado or gravado == ativo or gravado not in modelos:
            return
        if ativo in modelos:
            modelos[gravado] = visual_de(cfg)  # o visual que está no config ainda é o do modelo anterior
            _gravar(cfg, modelos, gravado)
            aplicar(copy.deepcopy(modelos[ativo]))
            _gravar(cfg, modelos, ativo)
            log.info("Modelo visual trocado no config.toml: %s -> %s", gravado, ativo)
        else:
            modelos = {(ativo if n == gravado else n): v for n, v in modelos.items()}
            _gravar(cfg, modelos, ativo)
            log.info("Modelo visual renomeado no config.toml: %s -> %s", gravado, ativo)


def conciliar_config(cfg: Config) -> None:
    """Chamado na abertura, antes do motor: grava no config.toml a troca feita à mão (e atualiza o cfg)."""
    def aplicar(valores_modelo: dict) -> None:
        cfg.atualizar_de(salvar(cfg, {"edicao": valores_modelo}))

    try:
        conciliar(cfg, aplicar)
    except (ErroConfig, ErroModelo, OSError) as e:
        log.warning("Não consegui conferir o modelo visual em uso: %s", e)


def valores(cfg: Config, nome: str) -> dict:
    modelos = listar(cfg)
    if nome not in modelos:
        raise ErroModelo(f"O modelo '{nome}' não existe", 404)
    return modelos[nome]


def criar(cfg: Config, nome: str, base: str | None = None) -> dict:
    """Modelo novo, copiado de outro (ou do visual atual). Devolve os valores dele."""
    nome = validar_nome(nome)
    with _TRAVA:
        modelos = listar(cfg)
        if any(n.lower() == nome.lower() for n in modelos):
            raise ErroModelo(f"Já existe um modelo chamado '{nome}'", 409)
        if base is not None and base not in modelos:
            raise ErroModelo(f"O modelo '{base}' não existe", 404)
        modelos[nome] = copy.deepcopy(modelos[base] if base else visual_de(cfg))
        _gravar(cfg, modelos, nome_ativo(cfg))
        return modelos[nome]


def renomear(cfg: Config, antigo: str, novo: str) -> str:
    novo = validar_nome(novo)
    with _TRAVA:
        modelos = listar(cfg)
        if antigo not in modelos:
            raise ErroModelo(f"O modelo '{antigo}' não existe", 404)
        if novo != antigo and any(n.lower() == novo.lower() for n in modelos if n != antigo):
            raise ErroModelo(f"Já existe um modelo chamado '{novo}'", 409)
        ativo = nome_ativo(cfg)
        # mantém a ordem dos modelos
        modelos = {(novo if n == antigo else n): v for n, v in modelos.items()}
        _gravar(cfg, modelos, novo if antigo == ativo else ativo)
    return novo


def excluir(cfg: Config, nome: str) -> None:
    with _TRAVA:
        modelos = listar(cfg)
        if nome == nome_ativo(cfg):
            raise ErroModelo("Este modelo está em uso. Troque para outro antes de excluir.", 409)
        if nome not in modelos:
            raise ErroModelo(f"O modelo '{nome}' não existe", 404)
        del modelos[nome]
        _gravar(cfg, modelos, nome_ativo(cfg))


def resumo(valores_modelo: dict) -> str:
    """Descrição curta para a lista de modelos."""
    partes = []
    moldura = str(valores_modelo.get("moldura") or "").strip()
    partes.append(f"moldura {Path(moldura).stem}" if moldura else "sem moldura")
    partes.append({"desfocado": "fundo desfocado", "preto": "fundo preto", "preencher": "tela cheia"}.get(
        str(valores_modelo.get("layout")), str(valores_modelo.get("layout"))))
    try:
        partes.append(f"vídeo {float(valores_modelo.get('zoom') or 1):.2f}x".replace(".", ","))
    except (TypeError, ValueError):
        pass
    return " · ".join(partes)
