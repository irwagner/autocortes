"""Posts de imagem por rede: config, banco, agenda e as consultas de vídeo que não podem contar imagem.

Tudo roda numa pasta temporária com um config.toml mínimo escrito à mão (a pasta_dados fica dentro
dela). Nada toca em dados\\, perfis\\ nem no config.toml da instalação.
"""

import contextlib
import copy
import io
import sqlite3
import tempfile
import threading
import time
import tomllib
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autocortes import (agenda, analise, cli, config, db, gravador, loop, planejador, produtor, publicador, roteiro,
                        selecao, util)
from autocortes.painel import api
from autocortes.painel.erros import ErroHttp
from autocortes.config import PADRAO, RAIZ, Config, ErroConfig

CONFIG_MINIMO = '[geral]\npasta_filmes = "filmes"\npasta_dados = "dados"\n'


class Base(unittest.TestCase):
    """Config numa pasta temporária, carregado do arquivo como o AutoCortes faz."""

    def setUp(self):
        pasta = tempfile.TemporaryDirectory(prefix="autocortes-teste-imagens-")
        self.addCleanup(pasta.cleanup)
        self.raiz = Path(pasta.name)
        self.caminho = self.raiz / "config.toml"
        self.cfg = self.carregar()

    def carregar(self, extra: str = "") -> Config:
        self.caminho.write_text(CONFIG_MINIMO + extra, encoding="utf-8")
        return config.carregar(self.caminho)

    def recusado(self, extra: str) -> list[str]:
        with self.assertRaises(ErroConfig) as erro:
            self.carregar(extra)
        return erro.exception.erros


def _diferencas(a, b, caminho="") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        saida = [f"{caminho}.{k}: só no exemplo" for k in a.keys() - b.keys()]
        saida += [f"{caminho}.{k}: só no PADRAO" for k in b.keys() - a.keys()]
        for k in a.keys() & b.keys():
            saida += _diferencas(a[k], b[k], f"{caminho}.{k}")
        return saida
    return [] if a == b and type(a) is type(b) else [f"{caminho}: {a!r} != {b!r}"]


# o banco como o código do main (bd7de20) deixa depois de migrar: sem a tabela imagens nem as colunas
# midia, imagem_tipo e imagem_id
ESQUEMA_MAIN = """
CREATE TABLE filmes (
    id INTEGER PRIMARY KEY, caminho TEXT NOT NULL UNIQUE, titulo TEXT NOT NULL, ano INTEGER, tamanho INTEGER,
    duracao REAL, tipo TEXT NOT NULL DEFAULT 'filme', status TEXT NOT NULL DEFAULT 'novo', erro TEXT,
    tentativas INTEGER NOT NULL DEFAULT 0, esgotado INTEGER NOT NULL DEFAULT 0, criado_em REAL NOT NULL,
    atualizado_em REAL NOT NULL
);
CREATE TABLE cortes (
    id INTEGER PRIMARY KEY, filme_id INTEGER NOT NULL REFERENCES filmes(id) ON DELETE CASCADE,
    inicio REAL NOT NULL, fim REAL NOT NULL, pontuacao REAL NOT NULL, detalhes TEXT, frase TEXT,
    status TEXT NOT NULL DEFAULT 'candidato', parte INTEGER, arquivo TEXT, erro TEXT,
    tentativas INTEGER NOT NULL DEFAULT 0, prioridade INTEGER NOT NULL DEFAULT 0, titulo_custom TEXT,
    descricao_custom TEXT, criado_em REAL NOT NULL, renderizado_em REAL, fila_em REAL, modelo TEXT, ia_textos TEXT
);
CREATE TABLE postagens (
    id INTEGER PRIMARY KEY, corte_id INTEGER NOT NULL REFERENCES cortes(id) ON DELETE CASCADE,
    plataforma TEXT NOT NULL, status TEXT NOT NULL, id_remoto TEXT, url TEXT, erro TEXT, horario REAL,
    manual INTEGER NOT NULL DEFAULT 0, criado_em REAL NOT NULL, atualizado_em REAL NOT NULL, via TEXT, slot TEXT,
    visualizacoes INTEGER, curtidas INTEGER, comentarios INTEGER, compartilhamentos INTEGER, metricas_extra TEXT,
    metricas_em REAL
);
CREATE TABLE estado (chave TEXT PRIMARY KEY, valor TEXT NOT NULL, atualizado_em REAL NOT NULL);
CREATE INDEX idx_postagens_slot ON postagens(plataforma, slot);
"""
DADOS_MAIN = """
INSERT INTO filmes (id, caminho, titulo, criado_em, atualizado_em) VALUES (1, 'C:/f.mkv', 'Filme', 1, 1);
INSERT INTO cortes (id, filme_id, inicio, fim, pontuacao, status, parte, criado_em)
    VALUES (1, 1, 10, 50, 1, 'pronto', 1, 1);
INSERT INTO postagens (corte_id, plataforma, status, criado_em, atualizado_em, slot)
    VALUES (1, 'youtube', 'publicado', 1, 1, '2026-10-01 12:30'), (1, 'tiktok', 'falhou', 2, 2, NULL);
"""


class TestBanco(Base):
    def test_banco_do_main_migra_com_as_postagens_em_video(self):  # CA-20 (parte do banco)
        caminho = self.raiz / "antigo.db"
        antigo = sqlite3.connect(caminho)
        antigo.executescript(ESQUEMA_MAIN + DADOS_MAIN)
        antigo.close()
        for _ in range(2):  # conectar de novo não falha
            conn = db.conectar(caminho)
            try:
                linhas = conn.execute("SELECT midia, imagem_tipo, imagem_id FROM postagens ORDER BY id").fetchall()
                self.assertEqual([tuple(r) for r in linhas], [("video", None, None), ("video", None, None)])
                tabelas = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
                self.assertIn("imagens", tabelas)
                self.assertIn("idx_postagens_midia", tabelas)
                colunas = {r["name"] for r in conn.execute("PRAGMA table_info(imagens)")}
                self.assertTrue({"corte_id", "tipo", "formato", "arquivos", "quadros", "texto", "com_chamada",
                                 "assinatura", "modelo", "criado_em"} <= colunas)
                # a linha nova sem midia é vídeo
                conn.execute("INSERT INTO postagens (corte_id, plataforma, status, criado_em, atualizado_em) "
                             "VALUES (1, 'instagram', 'simulado', 3, 3)")
                self.assertEqual(conn.execute("SELECT midia FROM postagens ORDER BY id DESC").fetchone()[0], "video")
                conn.execute("DELETE FROM postagens WHERE plataforma='instagram'")
            finally:
                conn.close()

    def test_imagens_somem_com_o_corte(self):
        conn = db.conectar(self.cfg.banco)
        self.addCleanup(conn.close)
        conn.executescript(DADOS_MAIN)
        conn.execute("INSERT INTO imagens (corte_id, tipo, formato, arquivos, quadros, assinatura, criado_em) "
                     "VALUES (1, 'momento', '4x5', '[]', '[]', 'abc', 1)")
        with self.assertRaises(sqlite3.IntegrityError):  # uma por corte, tipo e formato
            conn.execute("INSERT INTO imagens (corte_id, tipo, formato, arquivos, quadros, assinatura, criado_em) "
                         "VALUES (1, 'momento', '4x5', '[]', '[]', 'abc', 1)")
        conn.execute("DELETE FROM cortes WHERE id=1")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM imagens").fetchone()[0], 0)

    def test_conferir_fala_do_post(self):
        conn = db.conectar(self.cfg.banco)
        self.addCleanup(conn.close)
        conn.executescript(DADOS_MAIN)
        conn.execute("UPDATE postagens SET status='enviando', id_remoto='x' WHERE plataforma='tiktok'")
        db.recuperar_estados_pendentes(conn)
        linha = conn.execute("SELECT status, erro FROM postagens WHERE plataforma='tiktok'").fetchone()
        self.assertEqual(linha["status"], "conferir")
        self.assertIn("depois que a rede recebeu o post", linha["erro"])


# resultados da agenda de vídeo gravados com o código de bd7de20 (PADRAO, Instagram só seg/qua/sex),
# para 15/10/2026 (uma quinta): o midia="video" padrão não pode mudar nada
AGENDA_MAIN = {
    'youtube': {"com_chave":[["2026-10-15T12:39:05.109594","2026-10-15 12:30"],["2026-10-15T15:01:17.387318","2026-10-15 15:00"],["2026-10-15T20:05:06.861359","2026-10-15 20:00"]],"entre":[["2026-10-15T12:39:05.109594","2026-10-15 12:30"],["2026-10-15T15:01:17.387318","2026-10-15 15:00"],["2026-10-15T20:05:06.861359","2026-10-15 20:00"],["2026-10-16T12:30:46.138827","2026-10-16 12:30"],["2026-10-16T15:09:12.870702","2026-10-16 15:00"],["2026-10-16T20:06:31.009093","2026-10-16 20:00"],["2026-10-17T12:39:11.679735","2026-10-17 12:30"]],"vigente":["2026-10-15T12:39:05.109594","2026-10-15 12:30"],"proximo":"2026-10-15 15:01:17.387318","semana":21,"permitido":True},  # noqa: E501
    'tiktok': {"com_chave":[["2026-10-15T12:02:13.953248","2026-10-15 12:00"],["2026-10-15T18:36:49.313643","2026-10-15 18:30"],["2026-10-15T21:07:30.745572","2026-10-15 21:00"]],"entre":[["2026-10-15T12:02:13.953248","2026-10-15 12:00"],["2026-10-15T18:36:49.313643","2026-10-15 18:30"],["2026-10-15T21:07:30.745572","2026-10-15 21:00"],["2026-10-16T12:08:50.236695","2026-10-16 12:00"],["2026-10-16T18:39:18.071708","2026-10-16 18:30"],["2026-10-16T21:07:33.094861","2026-10-16 21:00"],["2026-10-17T12:09:40.387079","2026-10-17 12:00"]],"vigente":["2026-10-15T12:02:13.953248","2026-10-15 12:00"],"proximo":"2026-10-15 18:36:49.313643","semana":21,"permitido":True},  # noqa: E501
    'instagram': {"com_chave":[],"entre":[["2026-10-16T12:17:25.073723","2026-10-16 12:15"],["2026-10-16T19:04:59.591498","2026-10-16 19:00"]],"vigente":[],"proximo":"2026-10-16 12:17:25.073723","semana":6,"permitido":False},  # noqa: E501
    'kwai': {"com_chave":[["2026-10-15T12:04:06.834892","2026-10-15 12:00"],["2026-10-15T19:34:24.052295","2026-10-15 19:30"]],"entre":[["2026-10-15T12:04:06.834892","2026-10-15 12:00"],["2026-10-15T19:34:24.052295","2026-10-15 19:30"],["2026-10-16T12:00:17.312591","2026-10-16 12:00"],["2026-10-16T19:39:31.975064","2026-10-16 19:30"],["2026-10-17T12:02:16.589649","2026-10-17 12:00"]],"vigente":["2026-10-15T12:04:06.834892","2026-10-15 12:00"],"proximo":"2026-10-15 19:34:24.052295","semana":14,"permitido":True},  # noqa: E501
    'bilibili': {"com_chave":[["2026-10-15T08:07:21.547995","2026-10-15 08:00"]],"entre":[["2026-10-16T08:00:36.212468","2026-10-16 08:00"],["2026-10-17T08:00:48.239036","2026-10-17 08:00"]],"vigente":[],"proximo":"2026-10-16 08:00:36.212468","semana":7,"permitido":True},  # noqa: E501
    'consumo': 3.0,
}


class TestAgenda(Base):
    DIA = date(2026, 10, 15)

    def test_video_igual_ao_main(self):
        cfg = Config(copy.deepcopy(PADRAO), self.caminho)
        cfg["instagram"]["dias"] = ["seg", "qua", "sex"]
        cfg["instagram"]["imagens"] = True  # imagens ligadas não mexem nos vídeos
        cfg["instagram"]["envio"] = "navegador"
        for p in config.PLATAFORMAS:
            iso = [[h.isoformat(), c] for h, c in agenda.horarios_com_chave(cfg, p, self.DIA)]
            entre = agenda.horarios_entre_com_chave(cfg, p, datetime(2026, 10, 15, 11), datetime(2026, 10, 17, 13))
            vigente = agenda.horario_vigente_com_chave(cfg, p, datetime(2026, 10, 15, 13))
            obtido = {
                "com_chave": iso,
                "entre": [[h.isoformat(), c] for h, c in entre],
                "vigente": [vigente[0].isoformat(), vigente[1]] if vigente else [],
                "proximo": str(agenda.proximo_horario(cfg, p, datetime(2026, 10, 15, 13))),
                "semana": agenda.posts_por_semana(cfg, p, self.DIA),
                "permitido": agenda.dia_permitido(cfg, p, self.DIA),
            }
            self.assertEqual(obtido, AGENDA_MAIN[p], p)
            self.assertEqual(agenda.horarios_do_dia(cfg, p, self.DIA),
                             [datetime.fromisoformat(h) for h, _ in AGENDA_MAIN[p]["com_chave"]])
        self.assertEqual(agenda.consumo_diario(cfg), AGENDA_MAIN["consumo"])

    def test_chave_e_semente_de_imagem(self):
        cfg = Config(copy.deepcopy(PADRAO), self.caminho)
        cfg["tiktok"]["horarios_imagens"] = ["12:00"]  # o mesmo HH:MM de um vídeo...
        cfg["tiktok"]["videos"] = False  # ...numa rede só de imagens, para não sair do dia
        cfg["tiktok"]["imagens"] = True
        imagem = agenda.horarios_com_chave(cfg, "tiktok", self.DIA, "imagem")
        video = agenda.horarios_com_chave(cfg, "tiktok", self.DIA)
        self.assertEqual([c for _, c in imagem], ["img 2026-10-15 12:00"])
        self.assertEqual(video[0][1], "2026-10-15 12:00")
        self.assertNotEqual(imagem[0][0], video[0][0])  # atraso sorteado com outra semente
        self.assertEqual(agenda.horario_vigente_com_chave(cfg, "tiktok", imagem[0][0], "imagem")[1],
                         "img 2026-10-15 12:00")
        self.assertEqual(agenda.posts_por_semana(cfg, "tiktok", self.DIA, "imagem"), 7)
        self.assertEqual(agenda.proximo_horario(cfg, "instagram", datetime(2026, 10, 15, 11), "imagem").hour, 16)
        # dias_imagens vazio = os dias dos vídeos; com lista, a dela
        cfg["tiktok"]["dias"] = ["sex"]
        self.assertEqual(agenda.horarios_com_chave(cfg, "tiktok", self.DIA, "imagem"), [])
        cfg["tiktok"]["dias_imagens"] = ["qui"]
        self.assertEqual(len(agenda.horarios_com_chave(cfg, "tiktok", self.DIA, "imagem")), 1)

    def test_imagem_perto_do_video_sai_do_dia(self):  # CA-10 (parte da agenda)
        cfg = Config(copy.deepcopy(PADRAO), self.caminho)
        cfg["instagram"].update(horarios=["12:00", "19:00"], horarios_imagens=["11:30", "16:00"])
        self.assertEqual(cfg["agenda"]["variacao_minutos"], 10)
        self.assertEqual(cfg["agenda"]["intervalo_minimo_min"], 60)
        self.assertEqual(agenda.horarios_imagem_descartados(cfg, "instagram", self.DIA),
                         [{"horario": "11:30", "video": "12:00", "minutos": 30, "precisa": 70}])
        self.assertEqual([c for _, c in agenda.horarios_com_chave(cfg, "instagram", self.DIA, "imagem")],
                         ["img 2026-10-15 16:00"])
        # o vídeo nunca sai por causa da imagem
        self.assertEqual(len(agenda.horarios_com_chave(cfg, "instagram", self.DIA)), 2)
        # sem vídeos na rede, a imagem fica
        cfg["instagram"]["videos"] = False
        self.assertEqual(agenda.horarios_imagem_descartados(cfg, "instagram", self.DIA), [])
        # virada da meia-noite: imagem às 23:40 com vídeo às 00:10 do dia seguinte
        cfg["instagram"].update(videos=True, horarios=["00:10"], horarios_imagens=["23:40"])
        self.assertEqual(agenda.horarios_imagem_descartados(cfg, "instagram", self.DIA),
                         [{"horario": "23:40", "video": "00:10", "minutos": 30, "precisa": 70}])

    def test_imagens_perto_entre_si(self):
        cfg = Config(copy.deepcopy(PADRAO), self.caminho)
        cfg["instagram"]["horarios_imagens"] = ["09:00", "09:20"]
        descartados = agenda.horarios_imagem_descartados(cfg, "instagram", self.DIA)
        self.assertEqual(len(descartados), 1)
        self.assertEqual((descartados[0]["horario"], descartados[0]["video"], descartados[0]["imagem"]),
                         ("09:20", None, "09:00"))

    def test_sessenta_dias_sem_imagem_perto_de_video(self):  # CA-10 (com a variação sorteada)
        cfg = Config(copy.deepcopy(PADRAO), self.caminho)
        # 10:50 fica a 70 min do vídeo das 12:00 (o mínimo que a conta pede): mantido
        cfg["instagram"].update(horarios=["12:00", "19:00"], horarios_imagens=["10:50", "17:50", "20:05"])
        minimo = timedelta(minutes=cfg["agenda"]["intervalo_minimo_min"])
        mantidos = 0
        for i in range(60):
            dia = self.DIA + timedelta(days=i)
            videos = [h for d in (-1, 0, 1)
                      for h in agenda.horarios_do_dia(cfg, "instagram", dia + timedelta(days=d))]
            for h in agenda.horarios_do_dia(cfg, "instagram", dia, "imagem"):
                mantidos += 1
                self.assertTrue(all(abs(h - v) >= minimo for v in videos), (dia, h))
        self.assertEqual(mantidos, 120)  # 10:50 e 17:50 ficam; 20:05 sai todo dia

    def test_padrao_e_presets_sem_descartados(self):
        cfg = Config(copy.deepcopy(PADRAO), self.caminho)
        for rede in config.REDES_IMAGEM:
            self.assertEqual(agenda.horarios_imagem_descartados(cfg, rede, self.DIA), [], rede)
        for nome, preset in agenda.PRESETS.items():
            cfg = Config(copy.deepcopy(PADRAO), self.caminho)
            for rede in config.PLATAFORMAS:
                cfg[rede]["horarios"] = list(preset[rede])
            for rede in config.REDES_IMAGEM:
                cfg[rede]["horarios_imagens"] = list(preset["imagens"][rede])
                for i in range(7):
                    dia = self.DIA + timedelta(days=i)
                    self.assertEqual(agenda.horarios_imagem_descartados(cfg, rede, dia), [], (nome, rede))
                    total = (len(agenda.horarios_do_dia(cfg, rede, dia))
                             + len(agenda.horarios_do_dia(cfg, rede, dia, "imagem")))
                    self.assertLessEqual(total, cfg["agenda"]["maximo_por_dia"], (nome, rede))

    def test_consumo_so_com_redes_de_video(self):
        cfg = Config(copy.deepcopy(PADRAO), self.caminho)
        cfg["youtube"]["ativo"] = False
        cfg["tiktok"].update(videos=False, imagens=True, envio="navegador")
        self.assertEqual(agenda.consumo_diario(cfg), 2.0)  # só o Instagram (2 por dia) puxa a fila


class BaseBanco(Base):
    """Base com o banco aberto e ajudantes para pôr filmes, cortes e postagens à mão."""

    def setUp(self):
        super().setUp()
        self.cfg["geral"]["simulacao"] = False
        self.conn = db.conectar(self.cfg.banco)
        self.addCleanup(self.conn.close)
        self.filme = self.novo_filme()

    def novo_filme(self, titulo: str = "Filme Teste", tipo: str = "filme", status: str = "analisado") -> int:
        arquivo = self.raiz / "filmes" / f"{titulo}.mkv"
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo.write_bytes(bytes(1024))
        t = time.time()
        return self.conn.execute(
            "INSERT INTO filmes (caminho, titulo, tamanho, tipo, status, criado_em, atualizado_em) "
            "VALUES (?, ?, 1024, ?, ?, ?, ?)", (str(arquivo), titulo, tipo, status, t, t)).lastrowid

    def novo_corte(self, filme_id: int | None = None, status: str = "pronto", parte: int | None = 1,
                   inicio: float = 100.0, fim: float = 140.0, frase: str | None = "Uma fala qualquer.") -> int:
        filme_id = filme_id or self.filme
        t = time.time()
        editado = status != "candidato" or parte is not None
        arquivo = None
        if editado:
            self.cfg.pasta_cortes.mkdir(parents=True, exist_ok=True)
            arquivo = self.cfg.pasta_cortes / f"corte-{filme_id}-{inicio:.0f}.mp4"
            arquivo.write_bytes(bytes(512))
        return self.conn.execute(
            "INSERT INTO cortes (filme_id, inicio, fim, pontuacao, frase, status, parte, arquivo, criado_em, "
            "renderizado_em, fila_em) VALUES (?, ?, ?, 1.0, ?, ?, ?, ?, ?, ?, ?)",
            (filme_id, inicio, fim, frase, status, parte, str(arquivo) if arquivo else None, t,
             t if editado else None, t if editado else None)).lastrowid

    def postagem(self, corte_id: int, rede: str, status: str, midia: str = "video", imagem_tipo: str | None = None,
                 criado_em: float | None = None, slot: str | None = None, erro: str | None = None,
                 horario: float | None = None) -> int:
        t = time.time() if criado_em is None else criado_em
        return self.conn.execute(
            "INSERT INTO postagens (corte_id, plataforma, status, midia, imagem_tipo, slot, erro, horario, via, "
            "criado_em, atualizado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'oficial', ?, ?)",
            (corte_id, rede, status, midia, imagem_tipo, slot, erro, horario, t, t)).lastrowid

    def so_redes(self, *redes: str) -> None:
        for rede in config.PLATAFORMAS:
            self.cfg[rede]["ativo"] = rede in redes

    def so_imagens(self, rede: str) -> None:
        self.cfg[rede].update(videos=False, imagens=True, envio="navegador")

    def corte_row(self, corte_id: int):
        return self.conn.execute("SELECT * FROM cortes WHERE id=?", (corte_id,)).fetchone()


class TestVideosSoComMidiaVideo(BaseBanco):
    def test_imagem_publicada_nao_tira_o_corte_da_fila_nem_conclui(self):  # CA-09
        self.so_redes("instagram")
        corte = self.novo_corte()
        self.postagem(corte, "instagram", "publicado", "imagem", "momento")
        self.postagem(corte, "instagram", "conferir", "imagem", "carrossel")
        self.assertEqual([c["id"] for c in planejador.fila(self.cfg, self.conn, "instagram")], [corte])
        pub = publicador.Publicador(self.cfg, threading.Event())
        pub.atualizar_concluidos(self.conn)
        self.assertEqual(self.corte_row(corte)["status"], "pronto")
        # 4 falhas de imagem não contam como as do vídeo
        for _ in range(planejador.FALHAS_POR_CORTE):
            self.postagem(corte, "instagram", "falhou", "imagem", "frase")
        self.assertEqual([c["id"] for c in planejador.fila(self.cfg, self.conn, "instagram")], [corte])
        # o vídeo publicado conclui, e a imagem a conferir não segura
        self.postagem(corte, "instagram", "publicado")
        self.assertEqual(planejador.fila(self.cfg, self.conn, "instagram"), [])
        pub.atualizar_concluidos(self.conn)
        self.assertEqual(self.corte_row(corte)["status"], "concluido")

    def test_horario_atendido_por_midia(self):
        corte = self.novo_corte()
        sucesso = planejador.status_ocupam_horario(self.cfg)
        self.postagem(corte, "instagram", "publicado", "imagem", "momento", slot="img 2026-10-15 10:00")
        self.assertTrue(planejador.horario_atendido(self.conn, "instagram", 0, sucesso,
                                                    chave="img 2026-10-15 10:00", midia="imagem"))
        self.assertFalse(planejador.horario_atendido(self.conn, "instagram", 0, sucesso,
                                                     chave="img 2026-10-15 10:00"))
        # postagem antiga sem slot nem horário só vale para o vídeo
        self.postagem(corte, "tiktok", "publicado", "imagem", "momento", criado_em=time.time())
        self.assertFalse(planejador.horario_atendido(self.conn, "tiktok", time.time() - 60, sucesso,
                                                     chave="2026-10-15 12:00"))
        self.postagem(corte, "tiktok", "publicado", criado_em=time.time())
        self.assertTrue(planejador.horario_atendido(self.conn, "tiktok", time.time() - 60, sucesso,
                                                    chave="2026-10-15 12:00"))

    def test_seis_posts_misturados_seguram_os_dois(self):  # CA-11
        self.so_redes("instagram")
        self.cfg["instagram"].update(imagens=True, envio="navegador")
        corte = self.novo_corte()
        agora_ = time.time()
        for i in range(6):  # o mais novo há 2 h: fora do intervalo mínimo, dentro das 24 h
            midia, tipo = ("video", None) if i % 2 else ("imagem", "momento")
            criado = agora_ - 7200 - i * 600  # com horário próprio: não atendem o horário de hoje
            self.postagem(corte, "instagram", "publicado", midia, tipo, criado_em=criado, horario=criado)
        self.assertEqual(self.cfg["agenda"]["maximo_por_dia"], 6)
        hoje = date.today()
        video = agenda.horarios_com_chave(self.cfg, "instagram", hoje)[0][0] + timedelta(minutes=1)
        imagem = agenda.horarios_com_chave(self.cfg, "instagram", hoje, "imagem")[0][0] + timedelta(minutes=1)
        pub = publicador.Publicador(self.cfg, threading.Event())
        self.assertIsNone(pub.horario_devido(self.conn, "instagram", video))
        self.assertIsNone(pub.horario_devido(self.conn, "instagram", imagem, midia="imagem"))
        # com 5, os dois saem: era o limite do dia que segurava
        self.conn.execute("DELETE FROM postagens WHERE id = (SELECT MIN(id) FROM postagens)")
        self.assertIsNotNone(pub.horario_devido(self.conn, "instagram", video))
        devido = pub.horario_devido(self.conn, "instagram", imagem, midia="imagem")
        self.assertTrue(devido[1].startswith("img "))

    def test_rede_so_de_imagens_nao_tem_video(self):  # CA-21 (núcleo)
        self.so_redes("tiktok", "instagram")
        self.so_imagens("tiktok")
        corte = self.novo_corte()
        self.assertEqual(self.cfg.redes_de_video(), ["instagram"])
        plano = planejador.montar_plano(self.cfg, self.conn, agora=datetime.combine(date.today(), datetime.min.time()))
        self.assertFalse([i for i in plano["itens"] if i["rede"] == "tiktok" and i.get("midia", "video") == "video"])
        self.assertTrue([i for i in plano["itens"] if i["rede"] == "instagram" and i["midia"] == "video"])
        self.assertNotIn("tiktok", plano["cobertura"]["por_rede"])
        pub = publicador.Publicador(self.cfg, threading.Event())
        self.assertEqual(pub.postar(self.conn, "tiktok", self.corte_row(corte)), "so_imagens")
        self.so_imagens("instagram")
        self.assertEqual(pub.postar(self.conn, "instagram", self.corte_row(corte)), "so_imagens")
        self.assertEqual(pub.postar(self.conn, "facebook", self.corte_row(corte)), "so_imagens")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM postagens").fetchone()[0], 0)

    def test_instagram_so_de_imagens_desliga_a_pagina(self):  # CA-33
        self.so_redes("youtube", "instagram")
        self.cfg["instagram"].update(videos=False, imagens=True, envio="oficial", pagina_facebook=True)
        self.assertFalse(api._facebook_ligado(self.cfg))
        pub = publicador.Publicador(self.cfg, threading.Event())
        self.assertFalse(pub.facebook_ligado())
        corte = self.novo_corte()
        self.postagem(corte, "youtube", "publicado")
        self.postagem(corte, "facebook", "falhou", criado_em=time.time() - 86400)
        with patch.object(pub, "_facebook_pendente", side_effect=AssertionError("não devia olhar a Página")), \
                patch.object(pub, "postar", side_effect=AssertionError("não devia postar na Página")):
            pub.atualizar_concluidos(self.conn)
            pub._repetir_facebook(self.conn)
        self.assertEqual(self.corte_row(corte)["status"], "concluido")
        # com os vídeos ligados, a Página volta
        self.cfg["instagram"]["videos"] = True
        self.assertTrue(pub.facebook_ligado())
        self.assertTrue(api._facebook_ligado(self.cfg))

    def test_falhas_da_gravacao_e_da_geracao_nao_gastam_o_corte(self):
        corte = self.novo_corte()
        pub = publicador.Publicador(self.cfg, threading.Event())
        for erro in ("gravação: não saiu", "geração: o FFmpeg falhou") * 3:
            self.postagem(corte, "instagram", "falhou", "imagem", "momento", erro=erro)
        for _ in range(planejador.FALHAS_POR_CORTE):  # do vídeo e de outro tipo: não somam
            self.postagem(corte, "instagram", "falhou")
            self.postagem(corte, "instagram", "falhou", "imagem", "frase")
        ultima = self.postagem(corte, "instagram", "falhou", "imagem", "momento", erro="seletor sumiu")
        row = self.corte_row(corte)
        self.assertEqual(pub._limitar_falhas(self.conn, "instagram", row, ultima, "seletor sumiu", "imagem",
                                             "momento"), "falhou")
        for _ in range(planejador.FALHAS_POR_CORTE - 1):
            ultima = self.postagem(corte, "instagram", "falhou", "imagem", "momento", erro="seletor sumiu")
        self.assertEqual(pub._limitar_falhas(self.conn, "instagram", row, ultima, "seletor sumiu", "imagem",
                                             "momento"), "pulado")
        linha = self.conn.execute("SELECT status, midia, imagem_tipo FROM postagens WHERE id=?", (ultima,)).fetchone()
        self.assertEqual(tuple(linha), ("pulado", "imagem", "momento"))


class TestSoImagensNasTelas(BaseBanco):
    """CA-21 (o resto): painel, CLI e resumo do motor não postam nem contam vídeo na rede só de imagens."""

    def setUp(self):
        super().setUp()
        self.so_redes("tiktok", "instagram")
        self.so_imagens("tiktok")

    def ctx(self, **campos):
        motor = SimpleNamespace(publicador=None, rodando=False)
        painel = SimpleNamespace(motor=motor, gravacoes={})
        return SimpleNamespace(**{"cfg": self.cfg, "conn": self.conn, "painel": painel, "consulta": {}, "corpo": {},
                                  **campos})

    def test_lista_de_cortes_sem_a_rede_so_de_imagens(self):
        corte = self.novo_corte()
        self.postagem(corte, "instagram", "publicado")
        self.postagem(corte, "tiktok", "publicado", "imagem", "momento")
        resposta = api.listar_cortes(self.ctx(consulta={"aba": "publicados"}))
        self.assertEqual(resposta["ativas"], ["instagram"])
        self.assertEqual([c["id"] for c in resposta["itens"]], [corte])
        self.assertEqual(set(resposta["itens"][0]["postagens"]), {"instagram"})  # a imagem não é o "último vídeo"

    def test_resumo_das_redes(self):
        redes = api._resumo_redes(self.ctx())
        self.assertIsNone(redes["tiktok"]["proximo"])
        self.assertEqual(redes["tiktok"]["por_semana"], 0)
        self.assertIsNotNone(redes["instagram"]["proximo"])
        self.assertEqual(redes["instagram"]["por_semana"], 14)

    def test_postar_agora_recusa(self):
        corte = self.novo_corte()
        for rede in ("tiktok", "facebook"):
            if rede == "facebook":
                self.so_imagens("instagram")
                self.cfg["tiktok"]["videos"] = True
            with self.assertRaises(ErroHttp) as erro:
                api.postar_corte(self.ctx(corpo={"rede": rede}), str(corte))
            self.assertEqual(erro.exception.status, 400)
            self.assertIn("Esta rede está só com imagens: ligue Vídeos em Redes sociais", str(erro.exception))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM postagens").fetchone()[0], 0)

    def test_cli_postar_sai_com_1_sem_chamar_o_publicador(self):
        self.caminho.write_text(CONFIG_MINIMO + '[tiktok]\nvideos = false\nimagens = true\nenvio = "navegador"\n',
                                encoding="utf-8")
        args = SimpleNamespace(config=str(self.caminho), real=False, rede="tiktok", corte=None, detalhado=False)
        saida = io.StringIO()
        with patch.object(util, "configurar_log"), \
                patch.object(publicador, "Publicador", side_effect=AssertionError("não devia chamar o publicador")), \
                patch.object(cli, "_travar", side_effect=AssertionError("não devia travar a pasta")), \
                contextlib.redirect_stderr(saida):
            self.assertEqual(cli.cmd_postar(args), 1)
        self.assertIn(cli.RESULTADOS_POSTAR["so_imagens"], saida.getvalue())

    def test_resumo_do_motor(self):
        texto = loop.resumo(self.cfg, self.conn)
        self.assertIn("tiktok: próxima imagem ", texto)
        self.assertIn("instagram ", texto)


class TestReanalise(BaseBanco):
    """CA-26, CA-32 e DF-8: a reanálise e a mineração poupam o corte já editado que voltou a candidato."""

    def setUp(self):
        super().setUp()
        self.so_redes("instagram")
        self.corte = self.novo_corte(inicio=100, fim=140, parte=1)
        self.postagem(self.corte, "instagram", "publicado")
        self.postagem(self.corte, "instagram", "publicado", "imagem", "momento")
        self.conn.execute(
            "INSERT INTO imagens (corte_id, tipo, formato, arquivos, quadros, assinatura, criado_em) "
            "VALUES (?, 'momento', '4x5', '[]', '[100.5]', 'abc', ?)", (self.corte, time.time()))
        self.sem_parte = self.novo_corte(status="candidato", parte=None, inicio=500, fim=540)
        self.ctx = SimpleNamespace(cfg=self.cfg, conn=self.conn, consulta={}, corpo={},
                                   painel=SimpleNamespace(motor=SimpleNamespace(publicador=None, rodando=False)))

    def editar_de_novo(self) -> None:
        with patch.object(api, "_editar_em_segundo_plano") as editar:
            api._aplicar_acao(self.ctx, api._corte(self.conn, self.corte), "reeditar")
        editar.assert_called_once()
        linha = self.corte_row(self.corte)
        self.assertEqual((linha["status"], linha["parte"]), ("candidato", 1))

    def conferir_historico(self) -> None:
        self.assertIsNotNone(self.corte_row(self.corte))
        n = self.conn.execute("SELECT COUNT(*) FROM postagens WHERE corte_id=?", (self.corte,)).fetchone()[0]
        self.assertEqual(n, 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM imagens WHERE corte_id=?",
                                           (self.corte,)).fetchone()[0], 1)

    def minerar(self) -> dict:
        """Produtor._analisar com a análise e a seleção trocadas: devolve o que a seleção recebeu."""
        recebido: dict = {}

        def gerar(cfg, duracao, cenas, volume, falas, frases, ocupados, limite):
            recebido.update(ocupados=list(ocupados), limite=limite)
            return [SimpleNamespace(inicio=300.0, fim=340.0, pontuacao=2.0, detalhes={}, frase="Outra fala.")]

        a = SimpleNamespace(info={"duracao": 3600.0}, cenas=[], volume=[], falas=[], frases=[])
        filme = self.conn.execute("SELECT * FROM filmes WHERE id=?", (self.filme,)).fetchone()
        with patch.object(analise, "analisar", return_value=a), patch.object(selecao, "gerar_candidatos", gerar):
            self.assertEqual(produtor.Produtor(self.cfg, threading.Event())._analisar(self.conn, filme), 1)
        return recebido

    def candidatos_sem_parte(self) -> list[tuple]:
        return [tuple(r) for r in self.conn.execute(
            "SELECT inicio, fim FROM cortes WHERE status='candidato' AND parte IS NULL ORDER BY inicio")]

    def test_editar_de_novo_e_reanalisar_pelo_painel(self):  # CA-32
        self.editar_de_novo()
        api.acao_filme(SimpleNamespace(**{**vars(self.ctx), "corpo": {"acao": "reanalisar"}}), str(self.filme))
        self.conferir_historico()
        self.assertEqual(self.candidatos_sem_parte(), [])  # o candidato nunca editado saiu, como antes

    def test_editar_de_novo_e_reanalisar_pela_cli(self):  # CA-32
        self.editar_de_novo()
        args = SimpleNamespace(config=str(self.caminho), filme=self.filme, tudo=False, detalhado=False)
        with patch.object(util, "configurar_log"), patch.object(cli, "_travar"), \
                patch.object(cli, "_conn", return_value=self.conn), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_reanalisar(args), 0)
        self.conferir_historico()
        self.assertEqual(self.candidatos_sem_parte(), [])

    def test_editar_de_novo_e_minerar(self):  # CA-32 e DF-8
        self.cfg["cortes"]["max_por_filme"] = 5
        self.novo_corte(inicio=200, fim=240, parte=2)  # outro editado
        self.editar_de_novo()
        recebido = self.minerar()
        self.conferir_historico()
        self.assertIn((100.0, 140.0), recebido["ocupados"])  # o trecho do corte não volta como candidato
        self.assertEqual(recebido["limite"], 3)  # o max_por_filme conta o candidato com parte
        self.assertEqual(self.candidatos_sem_parte(), [(300.0, 340.0)])  # o sem parte foi refeito

    def test_corte_pronto_com_imagem_fica(self):  # CA-26
        self.cfg["cortes"]["max_por_filme"] = 5
        recebido = self.minerar()
        self.conferir_historico()
        self.assertIn((100.0, 140.0), recebido["ocupados"])
        self.assertEqual(recebido["limite"], 4)
        api.acao_filme(SimpleNamespace(**{**vars(self.ctx), "corpo": {"acao": "reanalisar"}}), str(self.filme))
        self.conferir_historico()
        self.assertEqual(self.corte_row(self.corte)["status"], "pronto")


class TestConfig(Base):
    def test_config_sem_as_chaves_novas_carrega_com_o_padrao(self):  # CA-01
        for rede in config.PLATAFORMAS:
            self.assertIs(self.cfg[rede]["videos"], True, rede)
        for rede in config.REDES_IMAGEM:
            self.assertIs(self.cfg[rede]["imagens"], False, rede)
            self.assertEqual(self.cfg[rede]["horarios_imagens"], PADRAO[rede]["horarios_imagens"])
            self.assertEqual(self.cfg[rede]["dias_imagens"], [])
        self.assertEqual(self.cfg["instagram"]["horarios_imagens"], ["10:00", "16:00"])
        self.assertEqual(self.cfg["tiktok"]["horarios_imagens"], ["10:30", "15:30"])
        self.assertEqual(self.cfg["imagens"]["quadros_carrossel"], 4)
        self.assertEqual(self.cfg["imagens"]["tipos"], ["momento", "frase", "carrossel"])

    def test_com_o_padrao_nada_muda(self):
        self.assertEqual(self.cfg.redes_de_video(), self.cfg.plataformas_ativas())
        self.assertEqual(self.cfg.redes_de_imagem(), [])
        cfg = Config(copy.deepcopy(PADRAO), self.caminho)
        self.assertEqual(cfg.redes_de_video(), cfg.plataformas_ativas())
        self.assertEqual(cfg.redes_de_imagem(), [])

    def test_imagens_com_envio_oficial_carrega_salva_e_fica_de_fora(self):  # CA-02 (parte do config)
        cfg = self.carregar('[instagram]\nimagens = true\nenvio = "oficial"\n')
        self.assertTrue(cfg["instagram"]["imagens"])
        self.assertNotIn("instagram", cfg.redes_de_imagem())
        self.assertEqual(config.motivo_sem_imagens(cfg, "instagram"),
                         "as imagens saem só pelo navegador: troque o envio para Navegador")
        salvo = config.salvar(cfg, {"instagram": {"imagens": True, "horarios_imagens": ["9:30", "16:00"]}})
        self.assertNotIn("instagram", salvo.redes_de_imagem())
        relido = config.carregar(self.caminho)
        self.assertTrue(relido["instagram"]["imagens"])
        self.assertEqual(relido["instagram"]["horarios_imagens"], ["09:30", "16:00"])
        # com o navegador, a rede entra
        cfg = self.carregar('[instagram]\nimagens = true\nenvio = "navegador"\n')
        self.assertEqual(cfg.redes_de_imagem(), ["instagram"])
        self.assertIsNone(config.motivo_sem_imagens(cfg, "instagram"))

    def test_motivo_das_redes_sem_imagem(self):
        for rede in ("youtube", "kwai", "bilibili"):
            self.assertEqual(config.motivo_sem_imagens(self.cfg, rede), config.IMAGEM_INDISPONIVEL[rede])
        self.assertIn("Comunidade", config.IMAGEM_INDISPONIVEL["youtube"])

    def test_recusas(self):  # CA-03
        erros = self.recusado('[tiktok]\nimagens = true\nhorarios_imagens = []\n')
        self.assertTrue(any("[tiktok].horarios_imagens" in e for e in erros), erros)
        erros = self.recusado('[imagens]\nquadros_carrossel = 6\n')
        self.assertTrue(any("quadros_carrossel" in e for e in erros), erros)
        erros = self.recusado('[imagens]\ntipos = ["outro"]\n')
        self.assertTrue(any("'outro'" in e for e in erros), erros)
        erros = self.recusado('[imagens]\ntipos = []\n[instagram]\nimagens = true\n')
        self.assertTrue(any("[imagens].tipos" in e for e in erros), erros)
        self.carregar('[imagens]\ntipos = []\n')  # sem imagens ligadas, tipos vazio vale
        erros = self.recusado('[tiktok]\nvideos = false\n')
        self.assertIn("[tiktok] ligue Vídeos ou Imagens, ou desative a rede", erros)
        self.carregar('[tiktok]\nativo = false\nvideos = false\n')  # rede desligada pode
        erros = self.recusado('[youtube]\nimagens = true\n')
        self.assertTrue(any(e.startswith("[youtube].imagens") for e in erros), erros)
        erros = self.recusado('[instagram]\nvideos = "sim"\n')
        self.assertIn("instagram.videos deve ser true ou false", erros)
        erros = self.recusado('[instagram]\nhorarios_imagens = ["25:00"]\n')
        self.assertTrue(any("[instagram].horarios_imagens tem um horário inválido" in e for e in erros), erros)
        erros = self.recusado('[instagram]\ndias_imagens = ["domingo"]\n')
        self.assertTrue(any("[instagram].dias_imagens tem um dia inválido" in e for e in erros), erros)
        self.carregar('[instagram]\nvideos = false\nimagens = true\n')  # só de imagens vale

    def test_horarios_e_dias_por_midia(self):
        cfg = self.carregar('[instagram]\ndias = ["seg", "qua"]\n[youtube]\nhorarios = []\n')
        self.assertEqual(cfg.horarios("instagram", "imagem"), ["10:00", "16:00"])
        self.assertEqual(cfg.horarios("instagram"), ["12:15", "19:00"])
        self.assertEqual(cfg.dias("instagram", "imagem"), ["seg", "qua"])  # vazio = os dias dos vídeos
        self.assertEqual(cfg.horarios("youtube", "imagem"), [])  # sem lista geral para imagens
        self.assertEqual(cfg.horarios("youtube"), cfg["agenda"]["horarios"])
        cfg = self.carregar('[instagram]\ndias_imagens = ["sab"]\n')
        self.assertEqual(cfg.dias("instagram", "imagem"), ["sab"])

    def test_filtrar_normaliza_horarios_de_imagem(self):
        limpo = config._filtrar(PADRAO, {"tiktok": {"horarios_imagens": ["15:30", "9:05", "09:05", " "]}})
        self.assertEqual(limpo["tiktok"]["horarios_imagens"], ["09:05", "15:30"])

    def test_exemplo_igual_ao_padrao(self):
        exemplo = tomllib.loads((RAIZ / "config.example.toml").read_text(encoding="utf-8"))
        self.assertEqual(_diferencas(exemplo, PADRAO), [])


ROTEIRO_IMAGEM = """\
abrir https://www.instagram.com/
clicar "Criar"
imagens em input[type=file]
clicar "Original"
escrever legenda em div[aria-label="Escreva uma legenda..."]
publicar "Compartilhar"
conferir "Sua publicação foi compartilhada"
"""


class TestRoteiro(Base):
    def erros(self, texto: str, nome: str) -> list[str]:
        return roteiro.analisar(texto, nome)[1]

    def test_nomes(self):
        self.assertEqual(roteiro.nome_roteiro("instagram", "imagem"), "instagram-imagem")
        self.assertEqual(roteiro.nome_roteiro("instagram"), "instagram")
        self.assertEqual(roteiro.midia_do_roteiro("tiktok-imagem"), "imagem")
        self.assertEqual(roteiro.midia_do_roteiro("tiktok"), "video")
        self.assertEqual(roteiro.midia_do_roteiro(None), "video")
        self.assertEqual(roteiro.rede_do_roteiro("tiktok-imagem"), "tiktok")
        self.assertEqual(roteiro.rede_do_roteiro("tiktok"), "tiktok")

    def test_imagens_so_no_roteiro_de_imagem(self):  # CA-04
        self.assertEqual(self.erros(ROTEIRO_IMAGEM, "instagram-imagem"), [])
        sem = ROTEIRO_IMAGEM.replace("imagens em input[type=file]\n", "")
        self.assertIn("falta a linha \"imagens\", que entrega as imagens para a rede",
                      self.erros(sem, "instagram-imagem"))
        com_video = ROTEIRO_IMAGEM.replace("imagens em", "video em")
        self.assertEqual(self.erros(com_video, "instagram-imagem"),
                         ["linha 3: o roteiro de imagem não tem \"video\"; as imagens entram com \"imagens\""])
        self.assertEqual(self.erros(ROTEIRO_IMAGEM, "instagram"),
                         ["linha 3: \"imagens\" é do roteiro de imagem; aqui o vídeo entra com \"video\""])
        # os apelidos
        for apelido in ("imagem", "fotos", "arquivo"):
            texto = ROTEIRO_IMAGEM.replace("imagens em", f"{apelido} em")
            instrucoes, erros = roteiro.analisar(texto, "instagram-imagem")
            self.assertEqual(erros, [], apelido)
            self.assertEqual([i.comando for i in instrucoes if i.alvos and i.alvos[0].valor == "input[type=file]"],
                             ["imagens"])
        # no roteiro de vídeo, "arquivo" continua sendo o vídeo
        video = "abrir https://x.com/\narquivo em input[type=file]\npublicar #post\n"
        instrucoes, erros = roteiro.analisar(video, "instagram")
        self.assertEqual(erros, [])
        self.assertEqual(instrucoes[1].comando, "video")

    def test_erros_proprios_do_roteiro_de_imagem(self):
        def so(linha_nova: str) -> list[str]:
            return self.erros(ROTEIRO_IMAGEM.replace("imagens em input[type=file]", linha_nova), "instagram-imagem")
        self.assertEqual(so("opcional imagens"), ["linha 3: o envio das imagens não pode ser opcional"])
        self.assertTrue(so('imagens em "Escolher"')[0].startswith("linha 3: as imagens entram num campo de arquivo"))
        self.assertEqual(so("imagens\nimagens em #outro"),
                         ["linha 4: o roteiro de imagem tem uma linha \"imagens\" só "
                          "(ela entrega todas as imagens de uma vez)"])
        sem_publicar = ROTEIRO_IMAGEM.replace('publicar "Compartilhar"\n', "")
        self.assertEqual(self.erros(sem_publicar, "instagram-imagem"),
                         ["falta a linha \"publicar\", que diz qual clique publica o post"])
        cedo = 'abrir https://x.com/\npublicar "Compartilhar"\nimagens\n'
        self.assertEqual(self.erros(cedo, "instagram-imagem"),
                         ["linha 2: o clique que publica precisa vir depois da linha \"imagens\""])
        # o título é opcional no TikTok de fotos, e o Instagram não tem
        tiktok = "abrir https://x.com/\nimagens\nescrever titulo em #t\nescrever legenda em #l\npublicar #post\n"
        self.assertEqual(self.erros(tiktok, "tiktok-imagem"), [])
        self.assertTrue(self.erros(tiktok, "instagram-imagem")[0].startswith("linha 3: esta rede não tem título"))
        sem_titulo = tiktok.replace("escrever titulo em #t\n", "")
        self.assertFalse([a for a in roteiro.resumo(sem_titulo, "tiktok-imagem")["avisos"]
                          if "Sem preenchimento" in a])
        sem_legenda = tiktok.replace("escrever legenda em #l\n", "")
        self.assertTrue([a for a in roteiro.resumo(sem_legenda, "tiktok-imagem")["avisos"]
                         if "Sem preenchimento automático de legenda" in a])

    def test_avisos_e_resumo(self):
        r = roteiro.resumo(ROTEIRO_IMAGEM, "instagram-imagem")
        self.assertEqual((r["midia"], r["tem_imagens"], r["tem_video"]), ("imagem", True, False))
        self.assertFalse([a for a in r["avisos"] if "recorte" in a])
        sem_recorte = ROTEIRO_IMAGEM.replace('clicar "Original"\n', "")
        avisos = roteiro.resumo(sem_recorte, "instagram-imagem")["avisos"]
        self.assertIn("não vi a escolha do recorte (4:5 ou Original): o Instagram corta a imagem em quadrado "
                      "e o texto pode ficar perto da borda", avisos)
        com_45 = ROTEIRO_IMAGEM.replace('clicar "Original"', 'clicar "Recorte 4:5"')
        self.assertFalse([a for a in roteiro.resumo(com_45, "instagram-imagem")["avisos"] if "recorte" in a])
        self.assertFalse([a for a in roteiro.resumo(sem_recorte, "tiktok-imagem")["avisos"] if "recorte" in a])
        sem_conferir = ROTEIRO_IMAGEM.replace('conferir "Sua publicação foi compartilhada"\n', "")
        avisos = roteiro.resumo(sem_conferir, "instagram-imagem")["avisos"]
        self.assertTrue([a for a in avisos if "se a imagem saiu" in a], avisos)
        # um clique com cara de publicar logo depois do arquivo
        cedo = ROTEIRO_IMAGEM.replace('clicar "Original"', 'clicar "Compartilhar agora"')
        self.assertTrue([a for a in roteiro.resumo(cedo, "instagram-imagem")["avisos"] if "cara de publicar" in a])
        video = roteiro.resumo("abrir https://x.com/\nvideo\npublicar #post\n", "instagram")
        self.assertEqual((video["midia"], video["tem_imagens"], video["tem_video"]), ("video", False, True))

    def test_ajuda(self):
        por_comando = {a["comando"]: a for a in roteiro.AJUDA}
        self.assertTrue(por_comando["imagens"]["o_que"].startswith("só no roteiro de imagem"))
        self.assertEqual(por_comando["imagens"]["exemplo"], "imagens em input[type=file]")
        self.assertTrue(por_comando["video"]["o_que"].startswith("só no roteiro de vídeo"))

    def test_gerar_roteiro_de_imagem(self):
        passos = [
            {"tipo": "clicar", "seletores": ["#criar"], "texto": "Criar"},
            {"tipo": "arquivo", "seletores": ["input[type=file]"], "quantos": 3, "multiplo": True},
            {"tipo": "arquivo", "seletores": ["#outro"], "quantos": 1},
            {"tipo": "digitar", "seletores": ["#legenda"], "papel": "legenda"},
            {"tipo": "clicar", "seletores": ["#publicar"], "texto": "Compartilhar"},
        ]
        tela = "Arquivo exemplo enviado com sucesso. Sua publicação foi compartilhada"
        texto = roteiro.gerar("instagram-imagem", passos, "https://www.instagram.com/", publicar_em=4,
                              confirmacao=tela)
        self.assertTrue(texto.startswith("# Roteiro de imagem do instagram, gravado em"))
        linhas = texto.splitlines()
        self.assertIn("imagens em input[type=file]", linhas)
        self.assertEqual(sum(1 for l in linhas if l.startswith("imagens")), 1)
        self.assertIn("# segunda escolha de arquivo ignorada: a linha \"imagens\" entrega todas de uma vez", linhas)
        self.assertNotIn("video", " ".join(l for l in linhas if not l.startswith("#")))
        self.assertIn('conferir "Arquivo exemplo enviado com sucesso"', linhas)
        self.assertEqual(roteiro.analisar(texto, "instagram-imagem")[1], [])
        # o evitar tira do conferir a frase com o nome do arquivo de exemplo
        texto = roteiro.gerar("instagram-imagem", passos, "https://www.instagram.com/", publicar_em=4,
                              confirmacao=tela, evitar=lambda f: "exemplo" in f.lower())
        self.assertIn('conferir "Sua publicação foi compartilhada"', texto)
        self.assertNotIn("Arquivo exemplo enviado", texto)
        # campo de uma imagem por vez
        um = [dict(passos[1], multiplo=False), passos[4]]
        texto = roteiro.gerar("instagram-imagem", um, "https://www.instagram.com/", publicar_em=1)
        self.assertIn("# O campo de arquivo que você usou aceita uma imagem por vez: "
                      "o carrossel não sai por este roteiro.", texto)
        # o vídeo continua igual
        video = roteiro.gerar("instagram", [passos[1], passos[4]], "https://www.instagram.com/", publicar_em=1)
        self.assertTrue(video.startswith("# Roteiro do instagram, gravado em"))
        self.assertIn("video em input[type=file]", video)

    def test_campo_preenchido_pela_pagina(self):
        passos = [
            {"tipo": "arquivo", "seletores": ["input[type=file]"], "quantos": 3},
            {"tipo": "digitar", "seletores": ["#titulo"], "preenchido_pela_pagina": True},
            {"tipo": "digitar", "seletores": ["#legenda"], "papel": "legenda"},
            {"tipo": "clicar", "seletores": ["#post"], "texto": "Publicar"},
        ]
        texto = roteiro.gerar("tiktok-imagem", passos, "https://www.tiktok.com/", publicar_em=3)
        self.assertIn("escrever titulo em #titulo", texto.splitlines())
        self.assertEqual(roteiro.analisar(texto, "tiktok-imagem")[1], [])
        # com o título já escrito por outro campo, o preenchido vira o comentário
        com_titulo = passos + [{"tipo": "digitar", "seletores": ["#t2"], "papel": "titulo"}]
        texto = roteiro.gerar("tiktok-imagem", com_titulo, "https://www.tiktok.com/", publicar_em=3)
        self.assertNotIn("escrever titulo em #titulo", texto)
        # no Instagram não há título: o comentário e o aviso no resumo
        texto = roteiro.gerar("instagram-imagem", passos, "https://www.instagram.com/", publicar_em=3)
        marca = [l for l in texto.splitlines() if l.startswith(roteiro.MARCA_PREENCHIDO)]
        self.assertEqual(marca, [f'{roteiro.MARCA_PREENCHIDO}: troque por escrever "seu texto" em #titulo, '
                                 "ou ele sai com o nome do arquivo"])
        n = texto.splitlines().index(marca[0]) + 1
        avisos = roteiro.resumo(texto, "instagram-imagem")["avisos"]
        self.assertIn(f"linha {n}: um campo ficou com o nome do arquivo, que a página preencheu sozinha; "
                      "troque pelo seu texto", avisos)

    def test_roteiro_de_imagem_sem_padrao(self):
        self.assertEqual(gravador.carregar_com_origem(self.cfg, "instagram"),
                         (gravador.arquivo_padrao("instagram").read_text(encoding="utf-8-sig"), "padrao"))
        self.assertEqual(gravador.carregar_com_origem(self.cfg, "instagram-imagem"), (None, None))
        arquivo = gravador.arquivo_roteiro(self.cfg, "instagram-imagem")
        self.assertEqual(arquivo.name, "instagram-imagem.txt")
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo.write_text(ROTEIRO_IMAGEM, encoding="utf-8")
        self.assertEqual(gravador.carregar_com_origem(self.cfg, "instagram-imagem"), (ROTEIRO_IMAGEM, "perfil"))

    def test_roteiros_padrao_de_video_continuam_valendo(self):
        arquivos = sorted((RAIZ / "roteiros").glob("*.txt"))
        self.assertTrue(arquivos)
        for arquivo in arquivos:
            texto = arquivo.read_text(encoding="utf-8-sig")
            r = roteiro.resumo(texto, arquivo.stem)
            self.assertEqual(r["erros"], [], arquivo.name)
            self.assertEqual((r["midia"], r["tem_video"], r["tem_imagens"]), ("video", True, False))


class BaseImagens(BaseBanco):
    """Instagram com imagens ligadas, o roteiro de imagem gravado e o filme analisado no disco."""

    def setUp(self):
        super().setUp()
        self.so_redes("instagram")
        self.cfg["instagram"].update(imagens=True, envio="navegador")
        self.analisado(self.filme)
        self.roteiro_imagem("instagram")

    def analisado(self, filme_id: int) -> None:
        pasta = analise.pasta_do_filme(self.cfg, filme_id)
        pasta.mkdir(parents=True, exist_ok=True)
        (pasta / "info.json").write_text("{}", encoding="utf-8")

    def roteiro_imagem(self, rede: str) -> None:
        arquivo = gravador.arquivo_roteiro(self.cfg, roteiro.nome_roteiro(rede, "imagem"))
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo.write_text(ROTEIRO_IMAGEM, encoding="utf-8")

    def na_fila(self, corte_id: int, fila_em: float) -> None:
        self.conn.execute("UPDATE cortes SET fila_em=? WHERE id=?", (fila_em, corte_id))

    def fila(self, rede: str = "instagram", quantidade: int = 1, **kw) -> list[tuple[int, str]]:
        return [(x["corte"]["id"], x["tipo"])
                for x in planejador.fila_imagens(self.cfg, self.conn, rede, quantidade, **kw)]

    def ids(self, rede: str = "instagram", **kw) -> list[int]:
        return [c["id"] for c in planejador.cortes_para_imagem(self.cfg, self.conn, rede, **kw)]


class TestPlanejadorImagens(BaseImagens):
    def test_estado_e_imagens_prontas(self):
        self.assertEqual(planejador.imagens_prontas(self.cfg, self.conn, "instagram"), (True, ""))
        self.assertEqual(planejador.imagens_prontas(self.cfg, self.conn, "tiktok")[0], False)
        planejador.gravar_estado_imagens(self.conn, "instagram", {"sem_carrossel": "uma imagem por vez"})
        self.assertEqual(planejador.ler_estado_imagens(self.conn, "instagram"), {"sem_carrossel": "uma imagem por vez"})
        self.assertEqual(planejador.tipos_efetivos(self.cfg, self.conn, "instagram"), ["momento", "frase"])
        self.assertTrue(planejador.imagens_prontas(self.cfg, self.conn, "instagram")[0])
        planejador.gravar_estado_imagens(self.conn, "instagram", {"parado": "o campo não aceita imagem"})
        self.assertEqual(planejador.imagens_prontas(self.cfg, self.conn, "instagram"),
                         (False, "o campo não aceita imagem"))
        planejador.gravar_estado_imagens(self.conn, "instagram", {"sem_fotos": "sem fotos pelo computador"})
        self.assertEqual(planejador.imagens_prontas(self.cfg, self.conn, "instagram"),
                         (False, "sem fotos pelo computador"))
        planejador.limpar_estado_imagens(self.conn, "instagram")
        self.assertEqual(planejador.ler_estado_imagens(self.conn, "instagram"), {})
        self.assertEqual(planejador.tipos_efetivos(self.cfg, self.conn, "instagram"), list(config.TIPOS_IMAGEM))
        # roteiro com erro conta como faltando
        gravador.arquivo_roteiro(self.cfg, "instagram-imagem").write_text("abrir https://x.com/\n", encoding="utf-8")
        pronta, motivo = planejador.imagens_prontas(self.cfg, self.conn, "instagram")
        self.assertFalse(pronta)
        self.assertTrue(motivo.startswith("o roteiro de imagem tem erro"), motivo)
        gravador.arquivo_roteiro(self.cfg, "instagram-imagem").unlink()
        self.assertEqual(planejador.imagens_prontas(self.cfg, self.conn, "instagram"),
                         (False, "falta gravar o roteiro de imagem desta rede"))

    def test_rodizio_de_tipos(self):
        self.so_imagens("instagram")
        c = self.novo_corte()
        self.assertEqual(self.fila(quantidade=4), [(c, "momento"), (c, "frase"), (c, "carrossel")])
        self.postagem(c, "instagram", "publicado", "imagem", "frase")
        self.assertEqual(planejador.tipos_da_vez(self.cfg, self.conn, "instagram"), ["carrossel", "momento", "frase"])
        self.assertEqual(self.fila(), [(c, "carrossel")])
        # a linha da gravação sem tipo não esgota nada nem mexe no rodízio
        self.postagem(c, "instagram", "conferir", "imagem", None, criado_em=time.time() + 5)
        self.assertEqual(planejador.tipos_da_vez(self.cfg, self.conn, "instagram"), ["carrossel", "momento", "frase"])
        self.assertEqual(self.fila(quantidade=3), [(c, "carrossel"), (c, "momento")])
        # um tipo desligado: o rodízio pula para o seguinte na ordem fixa
        self.cfg["imagens"]["tipos"] = ["momento", "frase"]
        self.assertEqual(planejador.tipos_da_vez(self.cfg, self.conn, "instagram"), ["momento", "frase"])
        self.cfg["imagens"]["tipos"] = ["carrossel"]
        self.assertEqual(self.fila(forcar_tipo="momento"), [(c, "momento")])

    def test_rede_com_videos_precisa_do_video_publicado(self):
        c1, c2 = self.novo_corte(inicio=100), self.novo_corte(inicio=200)
        self.na_fila(c1, 1000)
        self.na_fila(c2, 2000)
        self.assertEqual(self.fila(), [])
        self.assertFalse(planejador.video_publicado(self.conn, c1, "instagram", True))
        self.postagem(c1, "instagram", "simulado")
        self.assertTrue(planejador.video_publicado(self.conn, c1, "instagram", True))
        self.assertFalse(planejador.video_publicado(self.conn, c1, "instagram", False))
        self.assertEqual(self.fila(), [])  # fora da simulação, o simulado não vale
        # a gravação (qualquer_editado) cai no editado mais antigo da fila
        self.assertEqual(self.fila(forcar_tipo="carrossel", qualquer_editado=True), [(c1, "carrossel")])
        agora_ = time.time()
        self.postagem(c1, "instagram", "publicado", criado_em=agora_ - 100)
        self.postagem(c2, "instagram", "publicado", criado_em=agora_)
        self.postagem(c2, "tiktok", "publicado", "imagem", "momento")  # de outra rede: não conta
        self.assertEqual(self.ids(), [c2, c1])  # o vídeo mais recente primeiro
        self.assertEqual(self.fila(), [(c2, "momento")])
        self.assertTrue(planejador.video_publicado(self.conn, c2, "instagram", False))
        self.assertEqual(self.fila(forcar_tipo="carrossel", qualquer_editado=True), [(c2, "carrossel")])

    def test_so_cortes_editados_de_filme(self):  # CA-15
        self.so_imagens("instagram")
        pronto = self.novo_corte(inicio=100)
        concluido = self.novo_corte(status="concluido", inicio=200)
        self.novo_corte(status="candidato", parte=None, inicio=300)
        self.novo_corte(status="candidato", parte=2, inicio=400)  # "Editar de novo"
        self.novo_corte(status="revisao", inicio=500)
        pauta = self.novo_filme("Uma pauta", tipo="pauta")
        self.analisado(pauta)
        self.novo_corte(pauta, inicio=0)
        for status in ("ignorado", "ausente"):
            outro = self.novo_filme(f"Filme {status}", status=status)
            self.analisado(outro)
            self.novo_corte(outro)
        self.assertEqual(self.ids(), [pronto, concluido])
        vistos = {cid for cid, _ in self.fila(quantidade=20)}
        self.assertEqual(vistos, {pronto, concluido})

    def test_corte_sem_frase(self):  # CA-14
        self.so_imagens("instagram")
        mudo = self.novo_corte(frase="   ")
        self.assertEqual(self.fila(quantidade=3), [(mudo, "momento"), (mudo, "carrossel")])
        self.postagem(mudo, "instagram", "publicado", "imagem", "momento")
        self.assertEqual(self.fila(), [(mudo, "carrossel")])  # o tipo seguinte assume a vez
        self.assertEqual(self.ids(tipos=["frase"]), [])
        self.novo_corte(frase=None, inicio=300)
        self.assertEqual(self.ids(tipos=["frase"]), [])

    def test_filme_fora_do_disco(self):  # CA-27 (a lista)
        self.so_imagens("instagram")
        c = self.novo_corte()
        caminho = Path(self.conn.execute("SELECT caminho FROM filmes WHERE id=?", (self.filme,)).fetchone()[0])
        caminho.rename(caminho.with_name("renomeado.mkv"))
        self.assertEqual(self.ids(), [])
        caminho.with_name("renomeado.mkv").rename(caminho)
        self.assertEqual(self.ids(), [c])
        info = analise.pasta_do_filme(self.cfg, self.filme) / "info.json"
        info.unlink()
        self.assertEqual(self.fila(), [])
        self.analisado(self.filme)
        self.assertEqual(self.fila(), [(c, "momento")])

    def test_falhas_da_gravacao_e_da_geracao_nao_contam(self):
        self.so_imagens("instagram")
        c = self.novo_corte()
        for i in range(planejador.FALHAS_POR_CORTE):
            self.postagem(c, "instagram", "falhou", "imagem", "momento", erro="gravação: você conferiu: não saiu")
            self.postagem(c, "instagram", "falhou", "imagem", "momento", erro="geração: o FFmpeg não abriu")
        self.assertEqual(self.fila(), [(c, "momento")])
        for i in range(planejador.FALHAS_POR_CORTE):
            self.postagem(c, "instagram", "falhou", "imagem", "momento", erro="seletor sumiu")
        self.assertEqual(self.fila(), [(c, "frase")])

    def test_muitos_esgotados_nas_duas_listas(self):  # CA-31 (a lista)
        self.so_redes("tiktok", "instagram")
        self.so_imagens("instagram")
        self.cfg["tiktok"].update(imagens=True, envio="navegador")
        self.roteiro_imagem("tiktok")
        base = time.time() - 100000
        with db.transacao(self.conn):
            for i in range(205):
                c = self.novo_corte(inicio=float(i))
                self.na_fila(c, base + i)
                self.postagem(c, "tiktok", "publicado", criado_em=base + 1000 + i)
                for rede in ("instagram", "tiktok"):
                    for tipo in config.TIPOS_IMAGEM:
                        self.postagem(c, rede, "publicado", "imagem", tipo, criado_em=base + 2000 + i)
            novo = self.novo_corte(inicio=9999)
            self.na_fila(novo, base + 500)  # o último da fila
            self.postagem(novo, "tiktok", "publicado", criado_em=base)  # o vídeo mais velho
        self.assertEqual(self.fila("instagram"), [(novo, "momento")])
        self.assertEqual(self.fila("tiktok"), [(novo, "momento")])
        self.assertEqual(planejador.estoque_imagens(self.cfg, self.conn, "instagram"), 1)
        self.assertEqual(planejador.estoque_imagens(self.cfg, self.conn, "tiktok"), 1)

    def test_pagina_inteira_fora_do_disco_e_cache(self):  # CA-31 (OFFSET) e DF-12
        self.so_imagens("instagram")
        longe = self.novo_filme("No HDD desligado")
        self.analisado(longe)
        with db.transacao(self.conn):
            for i in range(200):
                self.na_fila(self.novo_corte(longe, inicio=float(i)), 1000 + i)
            perto = self.novo_corte(inicio=5000)
            self.na_fila(perto, 9000)
        caminho = Path(self.conn.execute("SELECT caminho FROM filmes WHERE id=?", (longe,)).fetchone()[0])
        caminho.unlink()
        original = Path.is_file
        chamadas: list[Path] = []

        def contar(p):
            chamadas.append(p)
            return original(p)

        with patch.object(Path, "is_file", contar):
            self.assertEqual(self.ids(), [perto])
        # um stat por filme (e o do info.json só no filme que está no disco), não um por corte
        self.assertEqual(len(chamadas), 3, chamadas)
        self.assertEqual(len(set(chamadas)), 3)

    def test_rendimento(self):
        self.so_imagens("instagram")
        self.cfg["imagens"]["tipos"] = ["frase"]
        mudos = [self.novo_corte(frase=None, inicio=float(i)) for i in range(2)]
        self.assertFalse(planejador.imagens_sem_rendimento(self.cfg, self.conn, "instagram"))  # poucos ainda
        mudos.append(self.novo_corte(frase="", inicio=3.0))
        self.assertTrue(planejador.imagens_sem_rendimento(self.cfg, self.conn, "instagram"))
        self.assertFalse(planejador.imagens_possiveis(self.cfg, self.conn, "instagram"))
        self.cfg["imagens"]["tipos"] = ["frase", "momento"]
        self.assertFalse(planejador.imagens_sem_rendimento(self.cfg, self.conn, "instagram"))
        self.assertTrue(planejador.imagens_possiveis(self.cfg, self.conn, "instagram"))
        # um corte que já rendeu imagem não conta como sem rendimento
        self.cfg["imagens"]["tipos"] = ["frase"]
        self.postagem(mudos[0], "instagram", "publicado", "imagem", "momento")
        self.assertFalse(planejador.imagens_sem_rendimento(self.cfg, self.conn, "instagram"))
        # sem tipo efetivo, nunca é possível
        self.cfg["imagens"]["tipos"] = ["carrossel"]
        planejador.gravar_estado_imagens(self.conn, "instagram", {"sem_carrossel": "uma por vez"})
        self.assertFalse(planejador.imagens_possiveis(self.cfg, self.conn, "instagram"))


class TestPlanoImagens(BaseImagens):
    def plano(self) -> dict:
        return planejador.montar_plano(self.cfg, self.conn, agora=datetime.combine(date.today(), datetime.min.time()))

    @staticmethod
    def de_imagem(plano: dict, rede: str = "instagram") -> list[dict]:
        return [i for i in plano["itens"] if i["tipo"] == "horario" and i["rede"] == rede and i["midia"] == "imagem"]

    def test_descartado_perto_do_video(self):  # CA-10 (plano)
        self.cfg["instagram"].update(horarios=["12:00", "19:00"], horarios_imagens=["11:30", "16:00"])
        plano = self.plano()
        horas = {datetime.fromtimestamp(i["quando"]).strftime("%H") for i in self.de_imagem(plano)}
        self.assertEqual(horas, {"16"})
        self.assertEqual(plano["descartados_imagem"],
                         {"instagram": [{"horario": "11:30", "video": "12:00", "minutos": 30, "precisa": 70}]})
        # imagens desligadas: nem horário nem descartado
        self.cfg["instagram"]["imagens"] = False
        plano = self.plano()
        self.assertEqual((self.de_imagem(plano), plano["descartados_imagem"]), ([], {}))

    def test_plano_igual_ao_motor(self):  # DF-4
        self.cfg["geral"]["simulacao"] = True
        self.so_imagens("instagram")
        self.cfg["instagram"]["horarios_imagens"] = ["09:00", "13:00", "17:00"]
        cortes = [self.novo_corte(inicio=100), self.novo_corte(inicio=200, frase=None), self.novo_corte(inicio=300)]
        for i, c in enumerate(cortes):
            self.na_fila(c, 1000 + i)
        previstos = [(i["corte_id"], i["imagem_tipo"]) for i in self.de_imagem(self.plano())[:6]]
        self.assertEqual(len(previstos), 6)
        motor = []
        for n in range(6):
            (corte, tipo), = self.fila()
            motor.append((corte, tipo))
            self.postagem(corte, "instagram", "simulado", "imagem", tipo, criado_em=time.time() + n)
        self.assertEqual(previstos, motor)
        self.assertEqual(len(set(motor)), 6)  # nenhum (corte, tipo) repetido
        self.assertNotIn((cortes[1], "frase"), motor)

    def test_espera_video_e_sem_corte(self):
        corte = self.novo_corte()
        itens = self.de_imagem(self.plano())
        video = [i for i in self.plano()["itens"] if i["tipo"] == "horario" and i["midia"] == "video"]
        self.assertEqual(video[0]["corte_id"], corte)  # o vídeo das 12:15 de hoje leva o corte
        self.assertEqual((itens[0]["motivo"], itens[0]["detalhe"]), ("sem_corte", "nenhum vídeo desta rede saiu ainda"))
        self.assertTrue(itens[0]["sem_imagem"])
        self.assertEqual(itens[1]["motivo"], "espera_video")  # o das 16:00 vem depois do vídeo das 12:15
        # sem corte nenhum para o vídeo, nada a esperar
        self.conn.execute("UPDATE cortes SET status='concluido' WHERE id=?", (corte,))
        self.assertEqual({i["motivo"] for i in self.de_imagem(self.plano())}, {"sem_corte"})
        # com o vídeo publicado, a imagem sai do corte dele
        self.postagem(corte, "instagram", "publicado", criado_em=time.time() - 3600)
        itens = self.de_imagem(self.plano())
        self.assertEqual((itens[0]["corte_id"], itens[0]["imagem_tipo"]), (corte, "momento"))
        self.assertEqual(itens[3]["motivo"], "sem_corte")
        self.assertNotIn("detalhe", itens[3])

    def test_motivos_sem_imagem(self):
        self.so_imagens("instagram")
        self.novo_corte()
        planejador.gravar_estado_imagens(self.conn, "instagram", {"parado": "o campo não aceita imagem"})
        self.assertEqual({i["motivo"] for i in self.de_imagem(self.plano())}, {"parado"})
        planejador.limpar_estado_imagens(self.conn, "instagram")
        self.cfg["imagens"]["tipos"] = ["carrossel"]
        planejador.gravar_estado_imagens(self.conn, "instagram", {"sem_carrossel": "uma por vez"})
        self.assertEqual({i["motivo"] for i in self.de_imagem(self.plano())}, {"sem_tipo"})
        planejador.limpar_estado_imagens(self.conn, "instagram")
        gravador.arquivo_roteiro(self.cfg, "instagram-imagem").unlink()
        self.assertEqual({i["motivo"] for i in self.de_imagem(self.plano())}, {"sem_roteiro"})
        self.roteiro_imagem("instagram")
        self.cfg["agenda"]["pausado"] = True
        self.assertEqual({i["estado"] for i in self.de_imagem(self.plano())}, {"pausado"})

    def test_rede_so_de_imagens_no_plano(self):  # CA-21 (plano)
        self.so_redes("tiktok", "instagram")
        self.so_imagens("tiktok")
        self.cfg["instagram"]["imagens"] = False
        self.roteiro_imagem("tiktok")
        corte = self.novo_corte()
        plano = self.plano()
        self.assertFalse([i for i in plano["itens"] if i["rede"] == "tiktok" and i["midia"] == "video"])
        imagens = self.de_imagem(plano, "tiktok")
        self.assertTrue(imagens)
        self.assertEqual((imagens[0]["corte_id"], imagens[0]["imagem_tipo"]), (corte, "momento"))
        self.assertFalse(self.de_imagem(plano, "instagram"))
        self.assertNotIn("tiktok", plano["cobertura"]["por_rede"])


class TestEstoqueSoDeImagens(BaseImagens):
    def setUp(self):
        super().setUp()
        self.so_imagens("instagram")
        self.prod = produtor.Produtor(self.cfg, threading.Event())

    def prontos(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM cortes WHERE status='pronto'").fetchone()[0]

    def test_estoque_cai_quando_os_tipos_saem(self):
        corte = self.novo_corte()
        self.novo_corte(status="revisao", inicio=300)
        self.assertEqual(self.prod.prontos_pendentes(self.conn), 2)
        for tipo in config.TIPOS_IMAGEM:
            self.postagem(corte, "instagram", "publicado", "imagem", tipo)
        self.assertEqual(self.prod.prontos_pendentes(self.conn), 1)  # só o da revisão
        self.assertEqual(self.prontos(), 1)
        # com uma rede de vídeo, o de hoje: a fila dela
        self.so_redes("tiktok", "instagram")
        self.assertEqual(self.prod.prontos_pendentes(self.conn),
                         len(planejador.fila(self.cfg, self.conn, "tiktok")) + 1)

    def test_sem_tipo_ou_sem_rendimento_para_no_buffer(self):  # CA-31 (produtor)
        buffer = int(self.cfg["geral"]["buffer_cortes"])
        for i in range(buffer):
            self.novo_corte(inicio=float(i * 100), frase=None)
        candidato = self.novo_corte(status="candidato", parte=None, inicio=9000)
        self.cfg["imagens"]["tipos"] = ["frase"]
        with self.assertLogs(util.log, "WARNING") as registro:
            self.assertEqual(self.prod.prontos_pendentes(self.conn), self.prontos())
        self.assertTrue([r for r in registro.output if "nenhum corte editado serve" in r])
        self.cfg["imagens"]["tipos"] = ["carrossel"]
        planejador.gravar_estado_imagens(self.conn, "instagram", {"sem_carrossel": "uma por vez"})
        self.assertEqual(self.prod.prontos_pendentes(self.conn), self.prontos())
        # o produtor não edita além do buffer_cortes, mesmo com candidato na mão
        cand = self.corte_row(candidato)
        with patch.object(produtor, "varrer_biblioteca", lambda cfg, conn: 0), \
                patch.object(self.prod, "espera_runtime", lambda: None), \
                patch.object(self.prod, "_completar_textos_ia", lambda conn: False), \
                patch.object(self.prod, "proximo_candidato", lambda conn: cand), \
                patch.object(self.prod, "minerar_mais", lambda conn: cand), \
                patch.object(self.prod, "renderizar", side_effect=AssertionError("editou além do buffer")):
            self.assertFalse(self.prod.ciclo(self.conn))
        # ligar outro tipo volta a valer na hora, com a linha no registro
        planejador.limpar_estado_imagens(self.conn, "instagram")
        self.cfg["imagens"]["tipos"] = ["frase", "momento"]
        with self.assertLogs(util.log, "INFO") as registro:
            self.assertEqual(self.prod.prontos_pendentes(self.conn), buffer)
        self.assertTrue([r for r in registro.output if "voltaram a render" in r])


if __name__ == "__main__":
    unittest.main()
