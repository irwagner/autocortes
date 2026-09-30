"""Vídeos criados do zero: da pauta ao arquivo pronto.

A pauta é um arquivo de texto que você escreve (ou que a IA vai escrever, mais adiante), com
um cabeçalho simples e o roteiro da narração:

    titulo: Comece pequeno
    termos: mar ao amanhecer, montanha, cidade de noite
    voz: pt-BR-AntonioNeural
    musica: calma.mp3
    ---
    Ninguém constrói nada grande em um dia.
    Você constrói em mil dias pequenos, quase iguais.
    [pausa: 1s] O segredo é não deixar de aparecer.

Cada linha do roteiro ganha um respiro na narração, e `[pausa: 2s]` cria um silêncio maior no
ponto exato. Os termos são o que buscar como imagem de fundo; sem termos, o título é usado.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import json
import sqlite3
import time

from . import estoque, ia, montagem, voz
from .config import Config
from .db import agora, transacao
from .midia import ErroMidia
from .util import log, slug

__all__ = [
    "ARQUIVO_TEMAS", "ErroCriacao", "Pauta", "Resultado", "arquivo_temas", "completar_com_ia", "criar",
    "criar_do_banco", "escrever_pauta", "ler_pauta", "pasta_pautas", "pauta_do_tema", "pautas",
    "proxima_pauta", "registrar", "riscar_tema", "sincronizar", "tema_pendente", "temas",
]

CAMPOS = ("titulo", "termos", "voz", "musica", "topo", "tema", "descricao", "hashtags")
_CABECALHO = re.compile(r"^\s*([a-zA-ZçãéíóúâêôÇÃÉÍÓÚÂÊÔ_]+)\s*:\s*(.*)$")
# fila de temas: uma linha por tema, consumida de cima para baixo pelo motor
ARQUIVO_TEMAS = "temas.txt"


class ErroCriacao(Exception):
    def __init__(self, mensagem: str, tipo: str = "conteudo"):
        super().__init__(mensagem)
        self.tipo = tipo


@dataclass
class Pauta:
    titulo: str
    texto: str
    termos: list[str] = field(default_factory=list)
    voz: str = ""
    musica: str = ""
    topo: str = ""          # texto queimado no alto do vídeo ("" = o título)
    tema: str = ""          # o assunto, quando a IA é que escreve o roteiro
    descricao: str = ""     # descrição do post ("" = as primeiras frases do roteiro)
    hashtags: list[str] = field(default_factory=list)
    arquivo: Path | None = None

    @property
    def busca(self) -> list[str]:
        return self.termos or [self.titulo]

    @property
    def nome_arquivo(self) -> str:
        return slug(self.titulo or "video", 50)

    @property
    def completa(self) -> bool:
        """Tem roteiro para narrar (sem isso, a IA precisa escrever)."""
        return bool(self.texto.strip())


@dataclass
class Resultado:
    video: Path
    narracao: Path
    duracao: float              # da narração
    duracao_video: float        # do arquivo final (narração + cauda)
    clipes: list[estoque.Clipe]
    creditos: str
    palavras: int
    voz: str = ""
    frases: list = field(default_factory=list)


def pasta_pautas(cfg: Config) -> Path:
    """Onde ficam as pautas escritas à mão (ao lado da pasta de filmes)."""
    return cfg.raiz / "pautas"


def pautas(cfg: Config) -> list[Path]:
    pasta = pasta_pautas(cfg)
    return sorted(p for p in pasta.glob("*.txt")) if pasta.is_dir() else []


def ler_pauta(caminho: Path) -> Pauta:
    """Lê o arquivo da pauta (cabeçalho + roteiro separados por uma linha com ---).

    Sem roteiro, a pauta vale como pedido: se houver `tema` (ou só o título), a IA escreve.
    """
    try:
        bruto = caminho.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ErroCriacao(f"não consegui ler a pauta {caminho.name}: {e}") from e

    cabecalho: dict[str, str] = {}
    corpo = bruto
    if re.search(r"^\s*-{3,}\s*$", bruto, re.M):
        parte_cabecalho, corpo = re.split(r"^\s*-{3,}\s*$", bruto, maxsplit=1, flags=re.M)
        for linha in parte_cabecalho.splitlines():
            if not linha.strip() or linha.lstrip().startswith("#"):
                continue
            achado = _CABECALHO.match(linha)
            if achado and achado.group(1).lower() in CAMPOS:
                cabecalho[achado.group(1).lower()] = achado.group(2).strip()
    else:
        # arquivo sem "---": se for uma linha só, é um tema; senão é roteiro solto
        if len([linha for linha in bruto.splitlines() if linha.strip()]) == 1 and len(bruto.strip()) <= 120:
            cabecalho["tema"] = bruto.strip()
            corpo = ""

    texto = corpo.strip()
    tema = cabecalho.get("tema", "")
    titulo = cabecalho.get("titulo") or tema or caminho.stem.replace("-", " ").replace("_", " ").strip().title()
    if not texto and not (tema or cabecalho.get("titulo")):
        raise ErroCriacao(f"a pauta {caminho.name} não tem roteiro nem tema (o texto vem depois da linha ---)")
    partir = lambda valor: [t.strip() for t in re.split(r"[,;]", valor) if t.strip()]  # noqa: E731
    return Pauta(
        titulo=titulo,
        texto=texto,
        termos=partir(cabecalho.get("termos", "")),
        voz=cabecalho.get("voz", ""),
        musica=cabecalho.get("musica", ""),
        topo=cabecalho.get("topo", ""),
        tema=tema,
        descricao=cabecalho.get("descricao", ""),
        hashtags=[h if h.startswith("#") else f"#{h}" for h in partir(cabecalho.get("hashtags", ""))],
        arquivo=caminho,
    )


def escrever_pauta(caminho: Path, pauta: Pauta) -> None:
    """Grava a pauta no mesmo formato que você escreveria à mão (é o que a IA preenche)."""
    linhas = [f"titulo: {pauta.titulo}"]
    if pauta.tema and pauta.tema != pauta.titulo:
        linhas.append(f"tema: {pauta.tema}")
    if pauta.termos:
        linhas.append(f"termos: {', '.join(pauta.termos)}")
    if pauta.topo:
        linhas.append(f"topo: {pauta.topo}")
    if pauta.descricao:
        linhas.append(f"descricao: {pauta.descricao}")
    if pauta.hashtags:
        linhas.append(f"hashtags: {', '.join(h.lstrip('#') for h in pauta.hashtags)}")
    if pauta.voz:
        linhas.append(f"voz: {pauta.voz}")
    if pauta.musica:
        linhas.append(f"musica: {pauta.musica}")
    texto = "\n".join(linhas) + "\n---\n" + pauta.texto.strip() + "\n"
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_name(caminho.name + ".tmp")
    temporario.write_text(texto, encoding="utf-8")
    temporario.replace(caminho)


# ---------------------------------------------------------------- fila de temas

def arquivo_temas(cfg: Config) -> Path:
    return pasta_pautas(cfg) / ARQUIVO_TEMAS


def temas(cfg: Config) -> list[str]:
    """Os temas da fila, de cima para baixo (linha começando com # é comentário)."""
    alvo = arquivo_temas(cfg)
    if not alvo.is_file():
        return []
    try:
        linhas = alvo.read_text(encoding="utf-8-sig").splitlines()
    except OSError as e:
        log.warning("Não consegui ler %s: %s", alvo.name, e)
        return []
    return [linha.strip() for linha in linhas if linha.strip() and not linha.lstrip().startswith("#")]


def tema_pendente(cfg: Config, conn: sqlite3.Connection) -> str | None:
    """Primeiro tema da fila que ainda não tem pauta. Não consome o arquivo."""
    if not temas(cfg):
        return None
    ja = {str(r["titulo"]).strip().lower() for r in conn.execute("SELECT titulo FROM filmes WHERE tipo='pauta'")}
    pasta = pasta_pautas(cfg)
    for tema in temas(cfg):
        if tema.strip().lower() in ja:
            continue
        if (pasta / f"{slug(tema, 50)}.txt").exists():
            continue
        return tema
    return None


def riscar_tema(cfg: Config, tema: str) -> None:
    """Comenta o tema no arquivo, para ele não voltar (e você ver o que já saiu)."""
    alvo = arquivo_temas(cfg)
    if not alvo.is_file():
        return
    try:
        linhas = alvo.read_text(encoding="utf-8-sig").splitlines()
        saida = [f"# feito: {linha}" if linha.strip() == tema.strip() else linha for linha in linhas]
        temporario = alvo.with_name(alvo.name + ".tmp")
        temporario.write_text("\n".join(saida) + "\n", encoding="utf-8")
        temporario.replace(alvo)
    except OSError as e:
        log.warning("Não consegui riscar o tema em %s: %s", alvo.name, e)


# ---------------------------------------------------------------- banco

def sincronizar(cfg: Config, conn: sqlite3.Connection) -> int:
    """Põe cada pauta da pasta no banco (e marca como ausente a que foi apagada)."""
    pasta = pasta_pautas(cfg)
    pasta.mkdir(parents=True, exist_ok=True)
    achadas: dict[str, Path] = {}
    for arquivo in sorted(pasta.glob("*.txt")):
        if arquivo.name == ARQUIVO_TEMAS:
            continue
        achadas[str(arquivo.resolve())] = arquivo

    existentes = {r["caminho"]: r for r in conn.execute(
        "SELECT id, caminho, status, titulo FROM filmes WHERE tipo='pauta'")}
    novas = 0
    t = agora()
    for caminho, arquivo in achadas.items():
        linha = existentes.get(caminho)
        try:
            pauta = ler_pauta(arquivo)
        except ErroCriacao as e:
            if linha is None:
                conn.execute(
                    "INSERT INTO filmes (caminho, titulo, tipo, status, erro, criado_em, atualizado_em) "
                    "VALUES (?, ?, 'pauta', 'erro', ?, ?, ?)",
                    (caminho, arquivo.stem, str(e)[:500], t, t),
                )
            else:
                conn.execute("UPDATE filmes SET status='erro', erro=?, atualizado_em=? WHERE id=?",
                             (str(e)[:500], t, linha["id"]))
            continue
        if linha is None:
            conn.execute(
                "INSERT INTO filmes (caminho, titulo, tipo, status, criado_em, atualizado_em) "
                "VALUES (?, ?, 'pauta', 'novo', ?, ?)",
                (caminho, pauta.titulo, t, t),
            )
            novas += 1
            log.info("Pauta nova: '%s' (%s)", pauta.titulo, arquivo.name)
        elif linha["status"] in ("ausente", "erro"):
            conn.execute("UPDATE filmes SET status='novo', erro=NULL, titulo=?, atualizado_em=? WHERE id=?",
                         (pauta.titulo, t, linha["id"]))

    for caminho, linha in existentes.items():
        if caminho not in achadas and linha["status"] != "ausente" and not Path(caminho).exists():
            conn.execute("UPDATE filmes SET status='ausente', atualizado_em=? WHERE id=?", (t, linha["id"]))
            log.info("Pauta saiu da pasta: %s", Path(caminho).name)
    return novas


def pauta_do_tema(cfg: Config, conn: sqlite3.Connection, tema: str) -> Path:
    """Pede o roteiro à IA, grava a pauta em pautas/ e registra no banco."""
    pauta = completar_com_ia(cfg, Pauta(titulo=tema, texto="", tema=tema))
    destino = pasta_pautas(cfg) / f"{slug(pauta.titulo or tema, 50)}.txt"
    if destino.exists():  # título repetido: não sobrescreve a pauta que já existe
        for n in range(2, 100):
            alternativo = destino.with_name(f"{destino.stem}-{n}.txt")
            if not alternativo.exists():
                destino = alternativo
                break
    pauta.arquivo = destino
    escrever_pauta(destino, pauta)
    riscar_tema(cfg, tema)
    sincronizar(cfg, conn)
    log.info("Tema '%s' virou a pauta %s", tema, destino.name)
    return destino


def proxima_pauta(cfg: Config, conn: sqlite3.Connection):
    """A próxima pauta a virar vídeo (espera crescente depois de falhas)."""
    return conn.execute(
        "SELECT * FROM filmes WHERE tipo='pauta' AND status='novo' "
        "AND atualizado_em + tentativas * 600 <= ? ORDER BY id LIMIT 1",
        (agora(),),
    ).fetchone()


def _textos_do_post(pauta: Pauta, resultado: "Resultado") -> tuple[str, str]:
    """Título e descrição do post. Os modelos de `[textos]` falam de filme, então a pauta traz os seus."""
    titulo = pauta.titulo.strip()
    corpo = pauta.descricao.strip()
    if not corpo:  # sem descrição escrita, as primeiras frases do roteiro servem
        corpo = " ".join(f.txt.strip() for f in resultado.frases[:2]).strip()
    partes = [p for p in (corpo, " ".join(pauta.hashtags).strip()) if p]
    if resultado.creditos:
        partes.append(resultado.creditos)
    return titulo, "\n\n".join(partes)


def registrar(cfg: Config, conn: sqlite3.Connection, pauta_id: int, pauta: Pauta,
              resultado: "Resultado", parte: int) -> int:
    """Grava o vídeo criado como um corte pronto, para entrar na fila e na agenda."""
    from . import modelos_visuais

    titulo, descricao = _textos_do_post(pauta, resultado)
    frase = next((f.txt.strip() for f in resultado.frases), "")
    detalhes = json.dumps({
        "criado": True, "clipes": len(resultado.clipes), "palavras": resultado.palavras,
        "creditos": resultado.creditos, "voz": resultado.voz,
    }, ensure_ascii=False)
    # o texto da IA fica gravado como se fosse dela: o motor não reescreve por cima
    ia_textos = json.dumps({
        "titulo": titulo, "descricao": descricao, "hashtags": list(pauta.hashtags),
        "modelo": "pauta", "gerado_em": time.time(),
    }, ensure_ascii=False)
    status = "revisao" if cfg["geral"]["exigir_aprovacao"] else "pronto"
    t = agora()
    with transacao(conn):
        corte_id = conn.execute(
            "INSERT INTO cortes (filme_id, inicio, fim, pontuacao, detalhes, frase, status, parte, arquivo, "
            "titulo_custom, descricao_custom, ia_textos, modelo, criado_em, renderizado_em, fila_em) "
            "VALUES (?, 0, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pauta_id, round(resultado.duracao_video, 3), detalhes, frase, status, parte,
             str(resultado.video), titulo, descricao, ia_textos, modelos_visuais.nome_ativo(cfg), t, t, t),
        ).lastrowid
        conn.execute(
            "UPDATE filmes SET status='analisado', esgotado=1, duracao=?, erro=NULL, tentativas=0, "
            "atualizado_em=? WHERE id=?",
            (round(resultado.duracao_video, 3), t, pauta_id),
        )
    log.info("Vídeo criado entrou na fila: corte %s ('%s', %.1f s)", corte_id, pauta.titulo,
             resultado.duracao_video)
    return corte_id


def criar_do_banco(cfg: Config, conn: sqlite3.Connection, linha, parar: threading.Event | None = None) -> int | None:
    """Cria o vídeo de uma pauta do banco e grava o corte. Devolve o id do corte."""
    arquivo = Path(linha["caminho"])
    if not arquivo.is_file():
        conn.execute("UPDATE filmes SET status='ausente', atualizado_em=? WHERE id=?", (agora(), linha["id"]))
        log.warning("Pauta não encontrada: %s", arquivo)
        return None
    try:
        pauta = ler_pauta(arquivo)
        if not pauta.completa:
            pauta = completar_com_ia(cfg, pauta)
        parte = (conn.execute("SELECT MAX(parte) FROM cortes WHERE filme_id=?",
                              (linha["id"],)).fetchone()[0] or 0) + 1
        resultado = criar(cfg, pauta, parte=parte, parar=parar)
    except ErroCriacao as e:
        tentativas = int(linha["tentativas"] or 0) + 1
        limite = 3
        status = "novo" if tentativas < limite and e.tipo == "temporario" else "erro"
        conn.execute("UPDATE filmes SET status=?, erro=?, tentativas=?, atualizado_em=? WHERE id=?",
                     (status, str(e)[:500], tentativas, agora(), linha["id"]))
        log.warning("Pauta '%s' não virou vídeo: %s%s", linha["titulo"], e,
                    f" (tentativa {tentativas} de {limite})" if status == "novo" else "")
        return None
    return registrar(cfg, conn, linha["id"], pauta, resultado, parte)


def completar_com_ia(cfg: Config, pauta: Pauta) -> Pauta:
    """Pede à IA o roteiro (e os termos de busca) de uma pauta que só tem tema."""
    if not ia.disponivel(cfg):
        raise ErroCriacao(
            f"a pauta '{pauta.titulo}' não tem roteiro, e a IA está desligada (ligue em Configurações > IA "
            "ou escreva o roteiro na pauta)", "config")
    tema = pauta.tema or pauta.titulo
    try:
        dados = ia.gerar_pauta(cfg, tema, float(cfg["criacao"]["duracao_alvo_seg"]))
    except ia.ErroIA as e:
        raise ErroCriacao(f"a IA não escreveu o roteiro de '{tema}': {e}",
                          "conteudo" if e.do_corte else "temporario") from e
    nova = Pauta(
        titulo=pauta.titulo if pauta.titulo != tema else (dados.get("titulo") or tema),
        texto=dados["roteiro"],
        termos=pauta.termos or list(dados.get("termos") or []),
        voz=pauta.voz,
        musica=pauta.musica,
        topo=pauta.topo or str(dados.get("topo") or ""),
        tema=tema,
        descricao=pauta.descricao or str(dados.get("descricao") or ""),
        hashtags=pauta.hashtags or list(dados.get("hashtags") or []),
        arquivo=pauta.arquivo,
    )
    if nova.arquivo is not None:  # grava o roteiro na própria pauta: dá para ler, editar e refazer
        escrever_pauta(nova.arquivo, nova)
    log.info("IA escreveu o roteiro de '%s' (%d palavras, termos: %s)", tema,
             len(nova.texto.split()), ", ".join(nova.termos[:4]))
    return nova


def _musica(cfg: Config, pauta: Pauta) -> Path | None:
    """A música pedida na pauta, ou uma da pasta de músicas (None = sem música)."""
    pasta_bruta = str(cfg["criacao"].get("pasta_musicas") or "").strip()
    pasta = cfg.caminho_de(pasta_bruta) if pasta_bruta else None
    if pauta.musica:
        alvo = Path(pauta.musica)
        if not alvo.is_absolute():
            for base in ([pasta] if pasta else []) + ([pauta.arquivo.parent] if pauta.arquivo else []):
                if base and (base / pauta.musica).is_file():
                    return base / pauta.musica
            alvo = cfg.caminho_de(pauta.musica)
        if alvo.is_file():
            return alvo
        log.warning("Música '%s' não encontrada: o vídeo sai sem música", pauta.musica)
        return None
    if pasta and pasta.is_dir():
        import random

        achadas = [p for p in pasta.iterdir() if p.suffix.lower() in (".mp3", ".m4a", ".wav", ".ogg", ".opus")]
        if achadas:
            return random.choice(achadas)
    return None


def criar(cfg: Config, pauta: Pauta, destino: Path | None = None, parte: int = 1,
          parar: threading.Event | None = None) -> Resultado:
    """Narra a pauta, junta o material de fundo e monta o vídeo vertical."""
    pasta_trabalho = cfg.pasta_dados / "criacao" / pauta.nome_arquivo
    pasta_trabalho.mkdir(parents=True, exist_ok=True)
    # a parte entra no nome: rodar a mesma pauta de novo não sobrescreve o vídeo que já está na fila
    destino = destino or (pasta_trabalho / f"{pauta.nome_arquivo}-parte{max(1, parte):02d}.mp4")

    # 1) narração: é ela que define o tempo de tudo
    narracao = pasta_trabalho / "narracao.mp3"
    try:
        fala = voz.falar(cfg, pauta.texto, narracao, voz=pauta.voz or None)
    except voz.ErroVoz as e:
        raise ErroCriacao(f"narração: {e}", e.tipo) from e
    if not fala.frases:
        raise ErroCriacao("a narração saiu sem tempos de palavra, então a legenda não casaria")

    # 2) material de fundo: um clipe por trecho, com folga
    duracao = fala.duracao + float(cfg["criacao"]["cauda_seg"])
    quantos = max(2, min(12, int(duracao // montagem.MIN_CLIPE)))
    try:
        material = estoque.material(cfg, pauta.busca, quantos,
                                    bool(cfg["estoque"]["evitar_repetidos"]))
    except estoque.ErroEstoque as e:
        raise ErroCriacao(f"material de fundo: {e}", e.tipo) from e

    # 3) montagem
    topo = pauta.topo or pauta.titulo
    try:
        video = montagem.montar(cfg, narracao, fala.frases, material.clipes, topo, destino,
                                musica=_musica(cfg, pauta), parar=parar,
                                rotulo=f"Montando '{pauta.titulo}'")
    except ErroMidia as e:
        raise ErroCriacao(f"montagem: {e}") from e

    estoque.marcar_usados(cfg, material.clipes)
    palavras = sum(len(f.palavras) for f in fala.frases)
    from .midia import duracao_arquivo

    duracao_video = duracao_arquivo(cfg["ferramentas"]["ffprobe"], video) or duracao
    log.info("Vídeo criado: '%s' (%.1f s, %d palavras, %d clipes)", pauta.titulo, duracao_video, palavras,
             len(material.clipes))
    return Resultado(
        video=video,
        narracao=narracao,
        duracao=fala.duracao,
        duracao_video=round(duracao_video, 3),
        clipes=material.clipes,
        creditos=estoque.credito_do_video(material.clipes),
        palavras=palavras,
        voz=fala.voz,
        frases=list(fala.frases),
    )
