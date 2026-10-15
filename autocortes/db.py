"""Estado persistente em SQLite: filmes, cortes, postagens e preferências do painel."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS filmes (
    id            INTEGER PRIMARY KEY,
    caminho       TEXT NOT NULL UNIQUE,
    titulo        TEXT NOT NULL,
    ano           INTEGER,
    tamanho       INTEGER,
    duracao       REAL,
    -- 'filme' (recortado) ou 'pauta' (vídeo criado do zero a partir de um roteiro)
    tipo          TEXT NOT NULL DEFAULT 'filme',
    -- novo | analisando | analisado | erro | ausente | ignorado | criando (pauta virando vídeo)
    status        TEXT NOT NULL DEFAULT 'novo',
    erro          TEXT,
    tentativas    INTEGER NOT NULL DEFAULT 0,
    -- 1 quando não há mais trechos bons para tirar do filme
    esgotado      INTEGER NOT NULL DEFAULT 0,
    criado_em     REAL NOT NULL,
    atualizado_em REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cortes (
    id               INTEGER PRIMARY KEY,
    filme_id         INTEGER NOT NULL REFERENCES filmes(id) ON DELETE CASCADE,
    inicio           REAL NOT NULL,
    fim              REAL NOT NULL,
    pontuacao        REAL NOT NULL,
    detalhes         TEXT,
    frase            TEXT,
    -- candidato | renderizando | revisao | pronto | concluido | erro | descartado
    status           TEXT NOT NULL DEFAULT 'candidato',
    parte            INTEGER,
    arquivo          TEXT,
    erro             TEXT,
    tentativas       INTEGER NOT NULL DEFAULT 0,
    -- maior = sai antes na fila
    prioridade       INTEGER NOT NULL DEFAULT 0,
    titulo_custom    TEXT,
    descricao_custom TEXT,
    criado_em        REAL NOT NULL,
    renderizado_em   REAL,
    -- primeira vez que ficou pronto: a posição na fila (não muda quando o corte é editado de novo)
    fila_em          REAL,
    -- modelo visual usado na última edição
    modelo           TEXT
);

CREATE TABLE IF NOT EXISTS postagens (
    id            INTEGER PRIMARY KEY,
    corte_id      INTEGER NOT NULL REFERENCES cortes(id) ON DELETE CASCADE,
    -- as redes da agenda e 'facebook' (o Reel do Instagram repetido na Página)
    plataforma    TEXT NOT NULL,
    -- enviando | publicado | simulado | falhou | pulado | interrompido | aguardando (tarefa à mão)
    -- | conferir (a rede recebeu o vídeo, falta você confirmar se saiu) | ensaio (preencheu sem publicar)
    status        TEXT NOT NULL,
    id_remoto     TEXT,
    url           TEXT,
    erro          TEXT,
    -- horário da agenda que esta postagem atendeu (NULL = manual)
    horario       REAL,
    manual        INTEGER NOT NULL DEFAULT 0,
    criado_em     REAL NOT NULL,
    atualizado_em REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS estado (
    chave         TEXT PRIMARY KEY,
    valor         TEXT NOT NULL,
    atualizado_em REAL NOT NULL
);

-- canais do YouTube que o AutoCortes olha para baixar os vídeos novos (tela Filmes)
CREATE TABLE IF NOT EXISTS canais (
    id            INTEGER PRIMARY KEY,
    -- endereço do canal, normalizado (https://www.youtube.com/@nome ou .../channel/UC...)
    url           TEXT NOT NULL UNIQUE,
    nome          TEXT,
    ativo         INTEGER NOT NULL DEFAULT 1,
    erro          TEXT,
    verificado_em REAL,
    criado_em     REAL NOT NULL
);

-- cada vídeo de canal que foi baixado ou tentado (é o que impede baixar o mesmo vídeo duas vezes)
CREATE TABLE IF NOT EXISTS downloads (
    id            INTEGER PRIMARY KEY,
    canal_id      INTEGER REFERENCES canais(id) ON DELETE SET NULL,
    video_id      TEXT NOT NULL UNIQUE,
    titulo        TEXT,
    publicado_em  REAL,
    duracao       REAL,
    -- baixando | baixado | erro (tenta de novo, até 3 vezes) | pulado (não serve, ou você pulou)
    status        TEXT NOT NULL,
    erro          TEXT,
    -- caminho do vídeo na pasta de filmes (o mesmo de filmes.caminho)
    arquivo       TEXT,
    tamanho       INTEGER,
    tentativas    INTEGER NOT NULL DEFAULT 0,
    criado_em     REAL NOT NULL,
    atualizado_em REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cortes_filme ON cortes(filme_id, status);
CREATE INDEX IF NOT EXISTS idx_cortes_status ON cortes(status);
CREATE INDEX IF NOT EXISTS idx_postagens_corte ON postagens(corte_id, plataforma);
CREATE INDEX IF NOT EXISTS idx_postagens_plataforma ON postagens(plataforma, criado_em);
-- tarefas à mão, posts a conferir e envios em andamento são procurados pelo status a cada ciclo
CREATE INDEX IF NOT EXISTS idx_postagens_status ON postagens(status, plataforma);
CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status, canal_id);
"""


def conectar(caminho: Path, preparar: bool = True) -> sqlite3.Connection:
    """Abre o banco. preparar=False pula a criação/migração (o painel abre uma conexão por requisição)."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: cada comando é confirmado na hora; blocos maiores usam transacao()
    # check_same_thread=False: cada conexão é usada por uma thread só, mas pode ser criada em outra
    conn = sqlite3.connect(caminho, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    if preparar:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(ESQUEMA)
        _migrar(conn)
    return conn


# colunas adicionadas depois da primeira versão do esquema
_COLUNAS_NOVAS = {
    "filmes": {
        "tentativas": "INTEGER NOT NULL DEFAULT 0",
        "esgotado": "INTEGER NOT NULL DEFAULT 0",
        # 'filme' ou 'pauta': separa o que é recortado do que é criado do zero
        "tipo": "TEXT NOT NULL DEFAULT 'filme'",
    },
    "cortes": {
        "tentativas": "INTEGER NOT NULL DEFAULT 0",
        "prioridade": "INTEGER NOT NULL DEFAULT 0",
        "titulo_custom": "TEXT",
        "descricao_custom": "TEXT",
        # JSON {titulo, descricao, hashtags, modelo, gerado_em} escrito pela IA;
        # {"descartado": true} quando você descartou o texto da IA (o motor não escreve de novo sozinho)
        "ia_textos": "TEXT",
        "fila_em": "REAL",
        "modelo": "TEXT",
    },
    "postagens": {
        "horario": "REAL",
        "manual": "INTEGER NOT NULL DEFAULT 0",
        "via": "TEXT",
        # chave do horário da agenda ("AAAA-MM-DD HH:MM", antes da variação) que a postagem atendeu
        "slot": "TEXT",
        # métricas lidas das redes depois da publicação
        "visualizacoes": "INTEGER",
        "curtidas": "INTEGER",
        "comentarios": "INTEGER",
        "compartilhamentos": "INTEGER",
        "metricas_extra": "TEXT",
        "metricas_em": "REAL",
    },
}


def _migrar(conn: sqlite3.Connection) -> None:
    for tabela, colunas in _COLUNAS_NOVAS.items():
        existentes = {r["name"] for r in conn.execute(f"PRAGMA table_info({tabela})")}
        for nome, tipo in colunas.items():
            if nome not in existentes:
                conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}")
                if (tabela, nome) == ("cortes", "fila_em"):
                    # os cortes que já estavam editados mantêm o lugar que tinham na fila
                    conn.execute("UPDATE cortes SET fila_em = renderizado_em WHERE renderizado_em IS NOT NULL")
    # índices sobre colunas que vieram depois: só dá para criar com as colunas já no lugar
    conn.execute("CREATE INDEX IF NOT EXISTS idx_filmes_tipo ON filmes(tipo, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_postagens_slot ON postagens(plataforma, slot)")


@contextmanager
def transacao(conn: sqlite3.Connection):
    """Bloco tudo-ou-nada. Dentro de outra transação, só participa dela (não abre uma nova)."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        # o SQLite desfaz sozinho em alguns erros; um ROLLBACK a mais esconderia o erro de verdade
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    try:
        conn.execute("COMMIT")
    except sqlite3.Error:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def agora() -> float:
    return time.time()


def ler_estado(conn: sqlite3.Connection, chave: str, padrao=None):
    linha = conn.execute("SELECT valor FROM estado WHERE chave=?", (chave,)).fetchone()
    if linha is None:
        return padrao
    try:
        return json.loads(linha["valor"])
    except ValueError:
        return padrao


def gravar_estado(conn: sqlite3.Connection, chave: str, valor) -> None:
    conn.execute(
        "INSERT INTO estado (chave, valor, atualizado_em) VALUES (?, ?, ?) "
        "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor, atualizado_em=excluded.atualizado_em",
        (chave, json.dumps(valor, ensure_ascii=False), agora()),
    )


def apagar_estado(conn: sqlite3.Connection, chave: str) -> None:
    conn.execute("DELETE FROM estado WHERE chave=?", (chave,))


def recuperar_estados_pendentes(conn: sqlite3.Connection, redes_ocupadas=(), editando: bool = False,
                                baixando: bool = False) -> None:
    """Depois de um travamento/desligamento, devolve itens 'em andamento' para a fila.

    redes_ocupadas: redes com um envio de verdade acontecendo agora (o "Postar agora" do painel
    segue rodando quando o motor é desligado); as postagens delas ficam como estão.
    editando: uma edição está rodando agora, então os cortes em 'renderizando' ficam.
    baixando: um download dos canais está rodando agora ("Verificar agora" do painel).
    """
    t = agora()
    if not baixando:
        # a tentativa interrompida não conta: o vídeo volta a ser baixado na próxima verificação
        conn.execute("UPDATE downloads SET status='erro', erro='o AutoCortes fechou no meio do download', "
                     "atualizado_em=? WHERE status='baixando'", (t,))
    if not editando:
        # 'criando' é a pauta reservada para virar vídeo (criacao.criar_do_banco)
        conn.execute("UPDATE filmes SET status='novo', atualizado_em=? WHERE status IN ('analisando', 'criando')", (t,))
        conn.execute("UPDATE cortes SET status='candidato' WHERE status='renderizando'")
    ocupadas = [str(r) for r in redes_ocupadas]
    fora = f" AND plataforma NOT IN ({','.join('?' * len(ocupadas))})" if ocupadas else ""
    # Reel que o Facebook já tinha aceitado (o link é gravado nessa hora): não é enviado de novo
    conn.execute(
        "UPDATE postagens SET status='publicado', erro='o AutoCortes fechou enquanto o Facebook processava o Reel', "
        "atualizado_em=? WHERE status='enviando' AND plataforma='facebook' AND url IS NOT NULL" + fora,
        (t, *ocupadas),
    )
    # a rede já tinha recebido o vídeo (o id é gravado nessa hora): repetir postaria duas vezes
    conn.execute(
        "UPDATE postagens SET status='conferir', erro='o AutoCortes fechou depois que a rede recebeu o vídeo; "
        "confira se ele saiu', atualizado_em=? WHERE status='enviando' AND id_remoto IS NOT NULL" + fora,
        (t, *ocupadas),
    )
    conn.execute(
        "UPDATE postagens SET status='interrompido', erro='processo encerrado durante o envio', "
        "atualizado_em=? WHERE status='enviando'" + fora,
        (t, *ocupadas),
    )
