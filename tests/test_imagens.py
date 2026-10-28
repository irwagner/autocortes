"""Posts de imagem por rede: config, banco, agenda e as consultas de vídeo que não podem contar imagem.

Tudo roda numa pasta temporária com um config.toml mínimo escrito à mão (a pasta_dados fica dentro
dela). Nada toca em dados\\, perfis\\ nem no config.toml da instalação.
"""

import contextlib
import copy
import io
import json
import re
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

from autocortes import (agenda, analise, cli, config, db, edicao, gravador, imagens, legendas, loop, midia, navegador,
                        planejador, produtor, publicador, roteiro, selecao, textos, util)
from autocortes.painel import api
from autocortes.plataformas import base as plat_base
from autocortes.plataformas import navegador as plat_nav
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


_HASHTAGS_SEM_IA = ["#OFilme", "#terrorantigo", "#Classico", "#filmes", "#cinema", "#cenasdefilmes", "#filme"]
_HASHTAGS_COM_IA = ["#OFilme", "#terrorantigo", "#Classico", "#ia1", "#iadois", "#filmes", "#cinema", "#cenasdefilmes"]
_DESCRICAO_SEM_IA = "Uma fala.\n\nO Filme (1999) - Parte 2\n\n" + " ".join(_HASHTAGS_SEM_IA)
_DESCRICAO_COM_IA = "Descrição da IA\n\nO Filme (1999) - Parte 2\n\n" + " ".join(_HASHTAGS_COM_IA)
# o Conteudo que o montar_conteudo do bd7de20 devolveu nestes cenários, gravado antes da refatoração (C10):
# (cenário, usar_ia) -> (titulo, descricao, hashtags)
CONTEUDO_MAIN = {
    ("sem_ia", True): ("O Filme - Parte 2", _DESCRICAO_SEM_IA, _HASHTAGS_SEM_IA),
    ("sem_ia", False): ("O Filme - Parte 2", _DESCRICAO_SEM_IA, _HASHTAGS_SEM_IA),
    ("com_ia", True): ("Título da IA | O Filme - Parte 2", _DESCRICAO_COM_IA, _HASHTAGS_COM_IA),
    ("com_ia", False): ("O Filme - Parte 2", _DESCRICAO_SEM_IA, _HASHTAGS_SEM_IA),
    ("custom", True): ("Meu título O Filme", _DESCRICAO_COM_IA, _HASHTAGS_COM_IA),
    ("custom", False): ("Meu título O Filme", _DESCRICAO_SEM_IA, _HASHTAGS_SEM_IA),
}


class TestTextos(Base):
    IA = '{"titulo": "Título da IA", "descricao": "Descrição da IA", "hashtags": ["#ia1", "ia dois"]}'

    def setUp(self):
        super().setUp()
        arquivo = self.raiz / "filmes" / "O Filme.mkv"
        arquivo.parent.mkdir(parents=True)
        arquivo.write_bytes(b"x")
        (self.raiz / "filmes" / "O Filme.mkv.json").write_text(
            '{"hashtags": ["terror antigo", "#Classico"], "canal": "Canal X", "url": "https://y/1"}', encoding="utf-8")
        self.filme_d = {"caminho": str(arquivo), "titulo": "O Filme", "ano": 1999}
        self.base = {"parte": 2, "frase": " Uma fala. ", "inicio": 10.0, "fim": 52.5, "ia_textos": None,
                     "titulo_custom": None, "descricao_custom": None}
        self.cenarios = {
            "sem_ia": dict(self.base),
            "com_ia": dict(self.base, ia_textos=self.IA),
            "custom": dict(self.base, ia_textos=self.IA, titulo_custom="Meu título {filme}", descricao_custom="  "),
        }

    def imagem(self, tipo="momento", com_chamada=False, corte=None, rede="instagram", frase="", n=0):
        return textos.montar_conteudo_imagem(self.cfg, self.filme_d, corte or self.cenarios["sem_ia"], tipo, n, rede,
                                             frase, com_chamada)

    def test_montar_conteudo_igual_ao_do_main(self):
        for (nome, usar_ia), (titulo, descricao, tags) in CONTEUDO_MAIN.items():
            with self.subTest(nome, usar_ia=usar_ia):
                c = textos.montar_conteudo(self.cfg, self.filme_d, self.cenarios[nome], usar_ia=usar_ia)
                self.assertEqual((c.titulo, c.descricao, c.hashtags, c.duracao, c.filme, c.ano),
                                 (titulo, descricao, tags, 42.5, "O Filme", 1999))

    def test_legenda_sem_ia_comeca_pelo_filme(self):
        c = self.imagem("momento")
        self.assertTrue(c.descricao.startswith("O Filme (1999) - Parte 2"), c.descricao)
        self.assertEqual((c.titulo, c.duracao, c.filme, c.ano), ("O Filme - Parte 2", 0.0, "O Filme", 1999))
        self.assertNotIn("\n\n\n", c.descricao)
        # com a IA, o título dela vem em cima; com usar_ia desligado, não
        com_ia = self.imagem("momento", corte=self.cenarios["com_ia"])
        self.assertTrue(com_ia.descricao.startswith("Título da IA\n\nO Filme (1999)"), com_ia.descricao)
        self.cfg["imagens"]["usar_ia"] = False
        self.assertTrue(self.imagem("momento", corte=self.cenarios["com_ia"]).descricao.startswith("O Filme (1999)"))

    def test_chamada_so_com_com_chamada(self):
        for tipo in config.TIPOS_IMAGEM:
            with self.subTest(tipo):
                sem = self.imagem(tipo, False, frase="Fala desenhada", n=4)
                self.assertNotIn("está no perfil", sem.descricao)
        self.assertIn("\nA Parte 2 completa está no perfil\n", self.imagem("momento", True).descricao)
        self.assertIn("em 4 cenas.\nA Parte 2 completa está no perfil", self.imagem("carrossel", True, n=4).descricao)

    def test_frase_e_o_parametro_e_custom_nao_vale(self):
        corte = self.cenarios["custom"]
        c = self.imagem("frase", corte=corte, frase="A frase desenhada.")
        self.assertTrue(c.descricao.startswith("“A frase desenhada.”\n\nO Filme (1999)"), c.descricao)
        self.assertNotIn("Uma fala", c.descricao)
        self.assertNotIn("Meu título", c.titulo + c.descricao)
        # corte antigo sem parte: a linha da parte sai e o título fica só com o filme
        sem_parte = self.imagem("momento", True, corte=dict(self.base, parte=None))
        self.assertEqual(sem_parte.titulo, "O Filme")
        self.assertNotIn("Parte", sem_parte.descricao)

    def test_aplicar_modelo_sem_vazias(self):
        v = {"filme": "X", "titulo_ia": "", "hashtags": "#a"}
        self.assertEqual(textos.aplicar_modelo_sem_vazias("{titulo_ia}\n\n\n\n{filme}\n{canal}\n\n{hashtags}\n", v),
                         "X\n\n#a")

    def test_instagram_com_no_maximo_5_hashtags(self):
        from autocortes.plataformas import manual
        c = self.imagem("momento", corte=self.cenarios["com_ia"])
        self.assertGreater(c.descricao.count("#"), 5)
        legenda = manual.textos(self.cfg, "instagram", c)["legenda"]
        self.assertEqual(legenda.count("#"), 5)
        self.assertIn("#OFilme", legenda)


TITULO_LONGO = "O Incrível Caso do Homem Que Esqueceu o Próprio Nome na Estação\nParte 12"
FRASE_LONGA = ("Eu te disse mil vezes que não era para abrir aquela porta, e agora olha só o que aconteceu "
               "com todos nós por causa da sua teimosia de sempre, meu amigo, de sempre mesmo")
CHAMADA = "A Parte 12 completa está no perfil"
ASPECTOS = {"2.39:1": 1920 / 804, "4:3": 4 / 3}


def _dentro(teste: unittest.TestCase, g: dict, rotulo: str = "") -> None:
    """CA-16: todo texto dentro da zona do formato, e no 9x16 o que passa de y = 960 fica em x ≤ 780."""
    x0, y0, x1, y1 = g["zona"]
    for b in g["blocos"]:
        cx0, cy0, cx1, cy1 = b["caixa"]
        teste.assertTrue(x0 <= cx0 and cx1 <= x1 and y0 <= cy0 and cy1 <= y1, f"{rotulo} {b} fora de {g['zona']}")
        if g["formato"] == "9x16" and cy1 > imagens.MEIO_9X16:
            teste.assertLessEqual(cx1, imagens.DIREITA_BAIXO_9X16, f"{rotulo} {b}")


class TestGeometria(Base):
    def combinacoes(self):
        """Os blocos de cada tipo: momento, frase e o primeiro, o do meio e o último do carrossel."""
        return {"momento": dict(topo=TITULO_LONGO, chamada=CHAMADA), "frase": dict(topo=TITULO_LONGO, frase=FRASE_LONGA),
                "carrossel-1": dict(topo=TITULO_LONGO), "carrossel-meio": {}, "carrossel-fim": dict(chamada=CHAMADA)}

    def test_textos_dentro_da_zona(self):  # CA-16 (geometria)
        for formato in imagens.ZONAS:
            for nome_aspecto, aspecto in ASPECTOS.items():
                for tipo, textos_ in self.combinacoes().items():
                    with self.subTest(formato=formato, aspecto=nome_aspecto, tipo=tipo):
                        g = imagens.geometria(self.cfg, formato, aspecto, **textos_)
                        self.assertEqual((g["largura"], g["altura"]), imagens.TAMANHOS[formato])
                        _dentro(self, g, f"{formato} {nome_aspecto} {tipo}")
                        estilos = sorted(b["estilo"] for b in g["blocos"])
                        esperados = sorted({"topo": "Topo", "chamada": "Chamada", "frase": "Frase"}[k] for k in textos_)
                        self.assertEqual(estilos, esperados)
                        if formato == "4x5":  # no 4x5 o quadro também fica inteiro na zona
                            self.assertGreaterEqual(g["y_quadro"], g["zona"][1])
                            self.assertLessEqual(g["y_quadro"] + g["h_quadro"], g["zona"][3])

    def test_titulo_longo_quebra_e_frase_cabe_em_4_linhas(self):
        g = imagens.geometria(self.cfg, "9x16", 4 / 3, topo=TITULO_LONGO, frase=FRASE_LONGA)
        topo = next(b for b in g["blocos"] if b["estilo"] == "Topo")
        frase = next(b for b in g["blocos"] if b["estilo"] == "Frase")
        self.assertGreater(len(topo["linhas"]), 2)
        self.assertEqual(topo["escala"], 72)
        self.assertLessEqual(len(frase["linhas"]), 4)
        self.assertTrue(frase["linhas"][0].startswith("“") and frase["linhas"][-1].endswith("”"))
        self.assertEqual(frase["linhas"][0], frase["linhas"][0].upper())  # o modelo padrão usa maiúsculas

    def test_bloco_centrado_e_maior_que_a_zona(self):  # CA-28
        for formato in imagens.ZONAS:
            with self.subTest(formato):
                g = imagens.geometria(self.cfg, formato, 1920 / 804, topo="Filme\nParte 1", chamada=CHAMADA)
                x0, y0, x1, y1 = g["zona"]
                topo, chamada = g["blocos"]
                self.assertEqual(topo["caixa"][3], g["y_quadro"] - imagens.FOLGA)
                self.assertEqual(chamada["caixa"][1], g["y_quadro"] + g["h_quadro"] + imagens.FOLGA)
                self.assertLessEqual(abs((topo["caixa"][1] - y0) - (y1 - chamada["caixa"][3])), 1)
        # 9x16 com um 4:3 e título de 2 linhas: o bloco passa da zona e o quadro fica no meio dela
        g = imagens.geometria(self.cfg, "9x16", 4 / 3, topo="Um Filme\nParte 2", chamada=CHAMADA)
        self.assertGreater(g["h_quadro"] + 2 * imagens.FOLGA + sum(b["caixa"][3] - b["caixa"][1] for b in g["blocos"]),
                           960)
        self.assertLessEqual(abs(g["y_quadro"] + g["h_quadro"] / 2 - 768), 1)
        _dentro(self, g)
        self.assertEqual(len(g["blocos"]), 2)
        self.assertEqual(len(g["blocos"][0]["linhas"]), 2)

    def test_frase_sobre_quadro_alto_no_9x16(self):  # CA-34
        g = imagens.geometria(self.cfg, "9x16", 1080 / 1300, topo="Filme\nParte 1", frase=FRASE_LONGA)
        self.assertEqual(g["h_quadro"], imagens.ALTURA_MAX_9X16)
        frase = next(b for b in g["blocos"] if b["estilo"] == "Frase")
        self.assertEqual(frase["an"], 2)
        self.assertLessEqual(frase["y"], 1248)
        self.assertGreaterEqual(frase["caixa"][0], 192)
        self.assertLessEqual(frase["caixa"][2], 780)
        self.assertEqual(frase["x"], (192 + 780) // 2)
        _dentro(self, g)

    def test_sem_chamada_o_quadro_cresce_no_4x5(self):
        com = imagens.geometria(self.cfg, "4x5", 4 / 3, topo="Filme\nParte 1", chamada=CHAMADA)
        sem = imagens.geometria(self.cfg, "4x5", 4 / 3, topo="Filme\nParte 1")
        self.assertGreater(sem["h_quadro"], com["h_quadro"])
        self.assertEqual(com["x_quadro"], (1080 - com["w_quadro"]) // 2)

    def test_altura_do_fundo_par(self):
        self.assertEqual(imagens.altura_fundo(1350), 338)
        self.assertEqual(imagens.altura_fundo(1920), 480)

    def test_sem_emoji(self):
        self.assertEqual(imagens._sem_emoji("Olá 😀 mundo ❤️\nParte 1 🎬"), "Olá mundo\nParte 1")
        g = imagens.geometria(self.cfg, "4x5", 2.0, topo="Filme 🍿")
        self.assertEqual(g["blocos"][0]["linhas"], ["Filme"])

    def test_ass_do_cartaz(self):
        g = imagens.geometria(self.cfg, "9x16", 2.0, topo="Filme\nParte 1", chamada=CHAMADA)
        estilo = edicao.estilo_legenda(self.cfg)
        ass = legendas.gerar_ass_cartaz(estilo, g["blocos"], g["largura"], g["altura"])
        self.assertIn("WrapStyle: 2", ass)
        self.assertIn("PlayResY: 1920", ass)
        dialogos = [linha for linha in ass.splitlines() if linha.startswith("Dialogue:")]
        self.assertEqual(len(dialogos), 2)
        topo = g["blocos"][0]
        self.assertIn(f"\\an2\\pos({topo['x']},{topo['y']})\\fscx{topo['escala']}", dialogos[0])
        self.assertTrue(dialogos[0].endswith("Filme\\NParte 1"))
        self.assertIn(",Chamada,", dialogos[1])

    def test_chaves_do_metadado_no_ffmpeg(self):  # DF-5: as chaves medidas existem no FFmpeg instalado
        arquivo = self.raiz / "meta.txt"
        r = midia.executar(midia.base_ffmpeg(self.cfg["ferramentas"]["ffmpeg"]) + [
            "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=4", "-t", "1",
            "-vf", f"signalstats,blurdetect,metadata=mode=print:file={midia.caminho_filtro(arquivo)}",
            "-f", "null", "-"], baixa_prioridade=False, timeout=60)
        self.assertEqual(r.codigo, 0, r.resumo_erro())
        texto = arquivo.read_text(encoding="utf-8")
        contagens = {chave: len(re.findall(rf"{re.escape(chave)}[=:]", texto))
                     for chave in imagens.CHAVES_METADADO.values()}
        self.assertEqual(len(set(contagens.values())), 1, contagens)  # uma de cada por quadro
        self.assertGreaterEqual(min(contagens.values()), 4, contagens)


# filmes sintéticos: quatro cenas de 4 s com 2 s de preto no meio (8 a 10 s), 18 s no total
FILMES_SINTETICOS = {"largo": (1920, 804), "quatro_tercos": (1440, 1080)}
FALA = (5.0, 6.5, "Uma fala qualquer.")
PRETO = (8.0, 10.0)


class BaseGeracao(BaseBanco):
    """Filmes de verdade (gerados por lavfi uma vez por classe), analisados e com uma fala."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        pasta = tempfile.TemporaryDirectory(prefix="autocortes-teste-imagens-filmes-")
        cls.addClassCleanup(pasta.cleanup)
        cls.pasta_filmes = Path(pasta.name)
        (cls.pasta_filmes / "config.toml").write_text(CONFIG_MINIMO, encoding="utf-8")
        cfg = config.carregar(cls.pasta_filmes / "config.toml")
        cls.analises = {}
        for nome, (w, h) in FILMES_SINTETICOS.items():
            arquivo = cls.pasta_filmes / f"{nome}.mkv"
            s = f"s={w}x{h}:r=24"
            fontes = [f"testsrc2={s}:d=4", f"smptebars={s}:d=4", f"color=c=black:{s}:d=2",
                      f"gradients={s}:d=4:speed=0.02", f"testsrc={s}:d=4"]
            entradas = [x for f in fontes for x in ("-f", "lavfi", "-i", f)]
            r = midia.executar(midia.base_ffmpeg(cfg["ferramentas"]["ffmpeg"]) + [
                "-loglevel", "error", *entradas, "-filter_complex",
                "".join(f"[{i}:v]" for i in range(len(fontes))) + f"concat=n={len(fontes)}:v=1:a=0,format=yuv420p[v]",
                "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", str(arquivo)],
                baixa_prioridade=False, timeout=300)
            if r.codigo != 0:
                raise RuntimeError(r.resumo_erro())
            info = analise.resumo_midia(cfg, arquivo)
            info["tamanho"] = arquivo.stat().st_size
            info["crop"] = [w, h, 0, 0]
            pasta_analise = cls.pasta_filmes / f"analise-{nome}"
            pasta_analise.mkdir()
            cenas = analise.detectar_cenas(cfg, arquivo, info, pasta_analise, nome, None)
            cls.analises[nome] = (arquivo, info, cenas)

    def setUp(self):
        super().setUp()
        self.ids_filme = {nome: self.filme_real(nome) for nome in FILMES_SINTETICOS}

    def filme_real(self, nome: str, falas: tuple = (FALA,)) -> int:
        arquivo, info, cenas = self.analises[nome]
        t = time.time()
        filme_id = self.conn.execute(
            "INSERT INTO filmes (caminho, titulo, ano, tamanho, tipo, status, criado_em, atualizado_em) "
            "VALUES (?, ?, 2001, ?, 'filme', 'analisado', ?, ?)",
            (str(arquivo), f"Filme {nome}", arquivo.stat().st_size, t, t)).lastrowid
        pasta = analise.pasta_do_filme(self.cfg, filme_id)
        pasta.mkdir(parents=True, exist_ok=True)
        util.salvar_json(pasta / "info.json", info)
        util.salvar_json(pasta / "cenas.json", cenas)
        frases = [legendas.Frase(a, b, txt, legendas.palavras_estimadas(a, b, txt)) for a, b, txt in falas]
        legendas.salvar_transcricao(pasta / "transcricao.json", frases, "teste", "pt")
        return filme_id

    def filme_row(self, filme_id: int):
        return self.conn.execute("SELECT * FROM filmes WHERE id=?", (filme_id,)).fetchone()

    def corte_real(self, nome: str = "largo", inicio: float = 0.0, fim: float = 18.0, **kw) -> int:
        return self.novo_corte(self.ids_filme[nome], inicio=inicio, fim=fim, **kw)

    def medir(self, corte_id: int, **kw):
        corte = self.corte_row(corte_id)
        filme = self.filme_row(corte["filme_id"])
        info, _ = analise.carregar_para_render(self.cfg, filme["id"])
        return imagens.medir_quadros(self.cfg, filme, corte, info, **kw)

    def escolher(self, corte_id: int, tipo: str, quadros: int = 4) -> dict:
        corte = self.corte_row(corte_id)
        _, frases = analise.carregar_para_render(self.cfg, corte["filme_id"])
        return imagens.escolher(tipo, corte, self.medir(corte_id), imagens.cenas_do_filme(self.cfg, corte["filme_id"]),
                                frases, quadros)


class TestGeracao(BaseGeracao):
    def test_cenas_do_filme_sintetico(self):
        cenas = self.analises["largo"][2]
        for troca in (4.0, 8.0, 10.0, 14.0):
            self.assertTrue(any(abs(c - troca) < 0.1 for c in cenas), (troca, cenas))

    def test_nenhum_quadro_escuro_nem_perto_de_troca(self):  # CA-17
        for nome in FILMES_SINTETICOS:
            corte = self.corte_real(nome)
            amostras = {round(t, 3): y for t, y, _ in self.medir(corte)}
            self.assertTrue(any(y < imagens.BRILHO_MIN for y in amostras.values()))  # o preto foi medido
            cenas = imagens.cenas_do_filme(self.cfg, self.ids_filme[nome])
            for tipo in config.TIPOS_IMAGEM:
                with self.subTest(nome=nome, tipo=tipo):
                    for t in self.escolher(corte, tipo)["tempos"]:
                        self.assertGreaterEqual(amostras[round(t, 3)], imagens.BRILHO_MIN, t)
                        self.assertFalse(PRETO[0] <= t <= PRETO[1], t)
                        self.assertGreaterEqual(min(abs(t - c) for c in cenas), imagens.LONGE_DA_CENA, t)
                        self.assertTrue(0.6 <= t <= 17.2, t)

    def test_carrossel_com_cenas_diferentes_em_ordem(self):
        corte = self.corte_real()
        cenas = imagens.cenas_do_filme(self.cfg, self.ids_filme["largo"])
        tempos = self.escolher(corte, "carrossel", 4)["tempos"]
        self.assertEqual(tempos, sorted(tempos))
        self.assertEqual(len(tempos), 4)
        segmentos = {sum(1 for c in cenas if c <= t) for t in tempos}
        self.assertEqual(len(segmentos), 4)
        self.assertEqual(len(self.escolher(corte, "carrossel", 3)["tempos"]), 3)
        # 5 pedidos com 4 cenas boas: completa com outro quadro, longe dos escolhidos
        cinco = self.escolher(corte, "carrossel", 5)["tempos"]
        self.assertEqual(len(cinco), 5)
        self.assertTrue(all(b - a >= imagens.DISTANCIA_CARROSSEL for a, b in zip(cinco, cinco[1:])), cinco)

    def test_carrossel_sem_3_quadros_e_do_corte(self):
        corte = self.corte_real(inicio=7.0, fim=10.5)  # quase tudo preto
        with self.assertRaises(imagens.ErroImagem) as erro:
            self.escolher(corte, "carrossel")
        self.assertTrue(erro.exception.do_corte)

    def test_frase_no_tempo_da_fala(self):
        corte = self.corte_real()
        escolha = self.escolher(corte, "frase")
        self.assertEqual(escolha["texto"], FALA[2])
        self.assertTrue(FALA[0] - 0.3 <= escolha["tempos"][0] <= FALA[1] + 0.3, escolha)

    def test_plano_b_da_frase(self):
        # a frase do corte não está mais na transcrição (refeita): vale a melhor fala do trecho
        corte = self.corte_real(frase="Uma frase que a transcrição nova não tem.")
        escolha = self.escolher(corte, "frase")
        self.assertEqual(escolha["texto"], FALA[2])
        self.assertTrue(FALA[0] - 0.3 <= escolha["tempos"][0] <= FALA[1] + 0.3)
        # sem nenhuma fala no trecho: é do corte
        sem_fala = self.corte_real(inicio=10.0, fim=18.0)
        with self.assertRaises(imagens.ErroImagem) as erro:
            self.escolher(sem_fala, "frase")
        self.assertTrue(erro.exception.do_corte)
        self.assertEqual(str(erro.exception), "o trecho não tem fala")

    def test_cache_reaproveitado(self):
        corte = self.corte_real()
        primeira = self.medir(corte)
        self.assertTrue((imagens.pasta_imagens(self.cfg, corte) / "quadros.json").is_file())
        self.assertFalse((imagens.pasta_imagens(self.cfg, corte) / "quadros.txt").exists())
        with patch.object(imagens, "executar", side_effect=AssertionError("mediu de novo")):
            self.assertEqual(self.medir(corte), primeira)
        # trecho diferente: mede de novo
        self.conn.execute("UPDATE cortes SET fim=17 WHERE id=?", (corte,))
        self.assertLess(max(t for t, _, _ in self.medir(corte)), 17.0)

    def test_sem_blurdetect_mede_so_o_brilho(self):
        corte = self.corte_real()
        with patch.object(imagens, "_FILTRO_NITIDEZ", "blurdetectquenaoexiste"), \
                patch.object(imagens, "_SEM_NITIDEZ_AVISADO", False), \
                self.assertLogs(util.log, "WARNING") as registro:
            amostras = self.medir(corte)
        self.assertTrue(amostras)
        self.assertTrue(all(b is None for _, _, b in amostras))
        self.assertTrue([r for r in registro.output if "só pelo brilho" in r])
        validos = imagens.quadros_validos(amostras, 0, 18, [])
        self.assertTrue(validos)
        self.assertTrue(all(q.nota == 0.65 for q in validos))  # 0,7 x 0,5 + 0,3 sem troca de cena

    def test_zero_amostras_e_falha_do_ffmpeg_sao_do_pc(self):  # DF-5
        depois_do_fim = self.corte_real(inicio=100.0, fim=110.0)
        with self.assertRaises(imagens.ErroImagem) as erro:
            self.medir(depois_do_fim)
        self.assertFalse(erro.exception.do_corte)
        self.assertEqual(str(erro.exception), "o FFmpeg não mediu nenhum quadro do trecho")  # saiu com 0
        self.assertFalse((imagens.pasta_imagens(self.cfg, depois_do_fim) / "quadros.json").exists())
        # arquivo que não é vídeo: o FFmpeg sai com erro
        estragado = self.raiz / "filmes" / "estragado.mkv"
        estragado.write_bytes(b"isto nao e um video" * 100)
        self.conn.execute("UPDATE filmes SET caminho=? WHERE id=?", (str(estragado), self.ids_filme["largo"]))
        with self.assertRaises(imagens.ErroImagem) as erro:
            self.medir(self.corte_real())
        self.assertFalse(erro.exception.do_corte)
        # prazo já gasto
        self.conn.execute("UPDATE filmes SET caminho=? WHERE id=?", (str(self.analises["largo"][0]),
                                                                     self.ids_filme["largo"]))
        with self.assertRaises(imagens.ErroImagem) as erro:
            self.medir(self.corte_real(inicio=1.0), relogio=imagens.Relogio(0))
        self.assertFalse(erro.exception.do_corte)
        self.assertIn("passou de 0 s", str(erro.exception))

    def test_amostras_lidas_todas_ruins_sao_do_corte(self):  # DF-5
        corte = self.corte_real(inicio=7.5, fim=10.5)
        with self.assertRaises(imagens.ErroImagem) as erro:
            self.escolher(corte, "momento")
        self.assertTrue(erro.exception.do_corte)


class TestPreparar(BaseGeracao):
    def setUp(self):
        super().setUp()
        self.so_redes("instagram", "tiktok")
        self.geometrias: list[dict] = []
        original = imagens.geometria

        def capturar(*args, **kw):
            g = original(*args, **kw)
            self.geometrias.append(g)
            return g

        espiao = patch.object(imagens, "geometria", side_effect=capturar)
        espiao.start()
        self.addCleanup(espiao.stop)

    def preparar(self, corte_id: int, tipo: str = "momento", rede: str = "instagram", **kw):
        return imagens.preparar(self.cfg, self.conn, self.corte_row(corte_id), tipo, rede, None, **kw)

    def arquivos(self, linha) -> list[Path]:
        return [Path(a) for a in json.loads(linha["arquivos"])]

    def estilos(self) -> list[set]:
        return [{b["estilo"] for b in g["blocos"]} for g in self.geometrias]

    def tamanho(self, arquivo: Path) -> tuple[int, int]:
        video = midia.sondar(self.cfg["ferramentas"]["ffprobe"], arquivo)["streams"][0]
        return video["width"], video["height"]

    def nao_renderiza(self):
        return patch.object(imagens, "_renderizar_quadro", side_effect=AssertionError("desenhou de novo"))

    def test_tres_tipos_dois_formatos(self):  # CA-16 (JPEG) e tamanhos
        for nome in FILMES_SINTETICOS:
            corte = self.corte_real(nome)
            for rede in ("instagram", "tiktok"):
                formato = config.FORMATOS_IMAGEM[rede]
                for tipo in config.TIPOS_IMAGEM:
                    with self.subTest(nome=nome, rede=rede, tipo=tipo):
                        self.geometrias.clear()
                        linha = self.preparar(corte, tipo, rede)
                        arquivos = self.arquivos(linha)
                        self.assertEqual(len(arquivos), 4 if tipo == "carrossel" else 1)
                        self.assertEqual(len(json.loads(linha["quadros"])), len(arquivos))
                        self.assertEqual((linha["formato"], linha["tipo"]), (formato["nome"], tipo))
                        for i, arquivo in enumerate(arquivos, 1):
                            self.assertEqual(arquivo.name, f"{tipo}-{formato['nome']}-{i}.jpg")
                            self.assertEqual(self.tamanho(arquivo), (formato["largura"], formato["altura"]))
                        self.assertEqual(len(self.geometrias), len(arquivos))
                        for g in self.geometrias:
                            _dentro(self, g, f"{nome} {rede} {tipo}")
                        self.assertEqual(linha["texto"], FALA[2] if tipo == "frase" else None)
            pasta = imagens.pasta_imagens(self.cfg, corte)
            self.assertEqual([p.name for p in pasta.iterdir() if ".parcial." in p.name], [])

    def test_reaproveita_e_refaz_com_outro_visual(self):
        corte = self.corte_real()
        primeira = self.preparar(corte)
        with self.nao_renderiza():
            self.assertEqual(tuple(self.preparar(corte)), tuple(primeira))
        self.cfg["edicao"]["cor_topo"] = "FF0000"
        nova = self.preparar(corte)
        self.assertEqual(nova["id"], primeira["id"])  # o upsert troca a mesma linha
        self.assertNotEqual(nova["assinatura"], primeira["assinatura"])
        self.assertEqual(nova["assinatura"], imagens.assinatura(self.cfg, "momento", "4x5", False))
        # arquivo apagado: refaz mesmo com a assinatura igual
        self.arquivos(nova)[0].unlink()
        self.assertTrue(self.arquivos(self.preparar(corte))[0].is_file())

    def test_postada_nao_e_redesenhada_e_a_da_gravacao_nao_congela(self):  # DF-9
        corte = self.corte_real()
        linha = self.preparar(corte)
        gravacao = self.postagem(corte, "instagram", "publicado", "imagem", None)
        self.conn.execute("UPDATE postagens SET imagem_id=? WHERE id=?", (linha["id"], gravacao))
        self.cfg["edicao"]["cor_topo"] = "00FF00"
        refeita = self.preparar(corte)
        self.assertNotEqual(refeita["assinatura"], linha["assinatura"])
        post = self.postagem(corte, "instagram", "publicado", "imagem", "momento")
        self.conn.execute("UPDATE postagens SET imagem_id=? WHERE id=?", (linha["id"], post))
        self.cfg["edicao"]["cor_topo"] = "0000FF"
        with self.nao_renderiza():
            self.assertEqual(self.preparar(corte)["assinatura"], refeita["assinatura"])
        # postagem de outro tipo com o mesmo imagem_id também não congela
        self.conn.execute("UPDATE postagens SET imagem_tipo='frase' WHERE id=?", (post,))
        self.assertEqual(self.preparar(corte)["assinatura"], imagens.assinatura(self.cfg, "momento", "4x5", False))

    def test_pauta_nunca_vira_imagem(self):  # CA-15
        pauta = self.novo_filme("Pauta", tipo="pauta")
        corte = self.novo_corte(pauta)
        with self.assertRaises(imagens.ErroImagem) as erro:
            self.preparar(corte)
        self.assertTrue(erro.exception.do_corte)

    def test_filme_fora_da_pasta_nao_grava_linha(self):  # CA-27 (geração)
        corte = self.corte_real()
        self.conn.execute("UPDATE filmes SET caminho=? WHERE id=?", (str(self.raiz / "sumiu.mkv"),
                                                                     self.ids_filme["largo"]))
        with self.assertRaises(imagens.ErroImagem) as erro:
            self.preparar(corte)
        self.assertTrue(erro.exception.sem_linha)
        self.assertFalse(erro.exception.do_corte)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM imagens").fetchone()[0], 0)

    def chamada(self, linha) -> tuple[bool, bool]:
        """(a chamada foi desenhada, a legenda tem a chamada) da última geração."""
        corte = self.corte_row(linha["corte_id"])
        filme = self.filme_row(corte["filme_id"])
        c = textos.montar_conteudo_imagem(self.cfg, filme, corte, linha["tipo"], len(self.arquivos(linha)),
                                          "instagram", linha["texto"] or "", bool(linha["com_chamada"]))
        desenhada = any("Chamada" in e for e in self.estilos())
        return desenhada, "está no perfil" in c.descricao

    def test_chamada_so_com_o_video_publicado(self):  # CA-24
        corte = self.corte_real()
        # sem vídeo publicado (o "Postar imagem agora" de um corte cujo vídeo não saiu nesta rede)
        linha = self.preparar(corte)
        self.assertEqual(linha["com_chamada"], 0)
        self.assertEqual(self.chamada(linha), (False, False))
        # na gravação, o vídeo só simulado não vale
        self.postagem(corte, "instagram", "simulado")
        self.geometrias.clear()
        linha = self.preparar(corte, "carrossel", post_simulado=False)
        self.assertEqual((linha["com_chamada"], self.chamada(linha)), (0, (False, False)))
        # no motor em simulação, o vídeo simulado vale para o post simulado
        self.cfg["geral"]["simulacao"] = True
        self.geometrias.clear()
        linha = self.preparar(corte, "carrossel")
        self.assertEqual((linha["com_chamada"], self.chamada(linha)), (1, (True, True)))
        self.assertEqual(self.estilos()[-1], {"Chamada"})  # o último quadro do carrossel
        self.assertEqual(self.estilos()[0], {"Topo"})
        self.cfg["geral"]["simulacao"] = False
        # com o vídeo publicado, a chamada aparece (e a imagem é refeita com ela)
        self.postagem(corte, "instagram", "publicado")
        self.geometrias.clear()
        linha = self.preparar(corte)
        self.assertEqual((linha["com_chamada"], self.chamada(linha)), (1, (True, True)))
        # rede só de imagens: nunca
        self.so_imagens("instagram")
        self.geometrias.clear()
        linha = self.preparar(corte)
        self.assertEqual((linha["com_chamada"], self.chamada(linha)), (0, (False, False)))

    def test_previa_sem_filme(self):  # CA-25
        destino = self.cfg.pasta_previa
        for rede in ("instagram", "tiktok"):
            formato = config.FORMATOS_IMAGEM[rede]
            for tipo in config.TIPOS_IMAGEM:
                with self.subTest(rede=rede, tipo=tipo):
                    self.geometrias.clear()
                    arquivos, real = imagens.previa(self.cfg, self.conn, tipo, rede, destino)
                    self.assertFalse(real)
                    self.assertEqual(len(arquivos), 4 if tipo == "carrossel" else 1)
                    for i, arquivo in enumerate(arquivos, 1):
                        self.assertEqual(arquivo, destino / f"imagem-{tipo}-{formato['nome']}-{i}.jpg")
                        self.assertEqual(self.tamanho(arquivo), (formato["largura"], formato["altura"]))
                    for g in self.geometrias:
                        _dentro(self, g)
                    # sem corte, a chamada aparece onde ficaria (a rede tem vídeos)
                    if tipo != "frase":
                        self.assertIn("Chamada", self.estilos()[-1])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM imagens").fetchone()[0], 0)

    def test_previa_com_corte_real(self):
        corte = self.corte_real()
        arquivos, real = imagens.previa(self.cfg, self.conn, "carrossel", "tiktok", self.cfg.pasta_previa)
        self.assertTrue(real)
        self.assertEqual(len(arquivos), 4)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM imagens").fetchone()[0], 0)
        # trecho que não serve para o tipo (sem fala): cai no exemplo
        self.conn.execute("UPDATE cortes SET inicio=10, fim=18 WHERE id=?", (corte,))
        self.assertFalse(imagens.previa(self.cfg, self.conn, "frase", "tiktok", self.cfg.pasta_previa)[1])

    def test_retrato_do_config(self):  # CA-29
        corte = self.corte_real()
        antes = self.cfg.copia()
        entrou, liberar = threading.Event(), threading.Event()
        cores = []

        def render_falso(cfg, entrada, t, info, g, destino, relogio, parar=None):
            cores.append(cfg["edicao"]["cor_topo"])
            entrou.set()
            liberar.wait(10)
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(b"jpg")
            return destino

        resultado = {}
        with patch.object(imagens, "_renderizar_quadro", side_effect=render_falso):
            fio = threading.Thread(target=lambda: resultado.update(linha=self.preparar(corte, "carrossel")))
            fio.start()
            self.assertTrue(entrou.wait(30))
            self.cfg["edicao"]["cor_topo"] = "00FF00"
            liberar.set()
            fio.join(30)
        self.assertEqual(set(cores), {"FFFFFF"})
        self.assertEqual(resultado["linha"]["assinatura"], imagens.assinatura(antes, "carrossel", "4x5", False))

    def test_carrossel_de_3_e_sobras(self):
        corte = self.corte_real()
        quatro = self.preparar(corte, "carrossel")
        tres = self.preparar(corte, "carrossel", quadros=3)
        self.assertNotEqual(tres["assinatura"], quatro["assinatura"])
        self.assertEqual(len(self.arquivos(tres)), 3)
        self.assertFalse((imagens.pasta_imagens(self.cfg, corte) / "carrossel-4x5-4.jpg").exists())
        # de volta ao número do config: refaz com 4
        self.assertEqual(len(self.arquivos(self.preparar(corte, "carrossel"))), 4)

    def ffmpeg_que_dorme(self) -> None:
        bat = self.raiz / "ffmpeg_dorme.bat"
        bat.write_bytes(b"@echo off\r\nping -n 120 127.0.0.1 >nul\r\n")
        self.cfg["ferramentas"]["ffmpeg"] = str(bat)

    def test_prazo(self):
        # a medição de um trecho de 3 min com prazo de 5 s
        longo = self.corte_real(inicio=0.0, fim=180.0)
        self.ffmpeg_que_dorme()
        comeco = time.monotonic()
        with self.assertRaises(imagens.ErroImagem) as erro:
            self.preparar(longo, prazo=5)
        self.assertLess(time.monotonic() - comeco, 12)
        self.assertFalse(erro.exception.do_corte)
        self.assertEqual(str(erro.exception), "a geração das imagens passou de 5 s")
        # o render que passa do prazo (a medição já está no cache): nada de .parcial.jpg
        self.cfg["ferramentas"]["ffmpeg"] = "ffmpeg"
        corte = self.corte_real()
        self.medir(corte)
        self.ffmpeg_que_dorme()
        with self.assertRaises(imagens.ErroImagem) as erro:
            self.preparar(corte, prazo=3)
        self.assertFalse(erro.exception.do_corte)
        pasta = imagens.pasta_imagens(self.cfg, corte)
        self.assertEqual([p.name for p in pasta.iterdir() if ".parcial." in p.name], [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM imagens").fetchone()[0], 0)


class _WsFalso:
    """Websocket de mentira para a Aba: anota cada comando do CDP e responde como a página falsa manda.

    campos: os <input type=file> da página, na ordem do DOM ({"seletores", "accept", "multiple"}).
    """

    def __init__(self, campos: list[dict]):
        self.campos = campos
        self.chamadas: list[tuple[str, dict]] = []
        self._fila: list[str] = []
        self.objetos: dict[str, object] = {}

    def enviar(self, texto: str) -> None:
        msg = json.loads(texto)
        metodo, params = msg["method"], msg.get("params") or {}
        self.chamadas.append((metodo, params))
        self._fila.append(json.dumps({"id": msg["id"], "result": self.responder(metodo, params)}))

    def receber(self) -> str:
        return self._fila.pop(0)

    def metodos(self) -> list[str]:
        return [m for m, _ in self.chamadas]

    def _objeto(self, valor) -> dict:
        objeto_id = f"obj-{len(self.objetos) + 1}"
        self.objetos[objeto_id] = valor
        return {"type": "object", "objectId": objeto_id}

    def _casam(self, lista: list[str]) -> list[int]:
        vistos: list[int] = []
        for s in lista:
            vistos += [i for i, c in enumerate(self.campos) if s in c["seletores"] and i not in vistos]
        return vistos

    def responder(self, metodo: str, params: dict) -> dict:
        if metodo == "Runtime.evaluate":
            expr = params["expression"]
            if not params.get("returnByValue"):
                lista = re.search(r"of (\[.*?\])\)", expr)
                if expr.startswith("__ac.achar("):
                    alvo = json.loads(expr[len("__ac.achar("):-1])
                    casam = self._casam([alvo])
                    return {"result": self._objeto(casam[0] if casam else None)}
                casam = self._casam(json.loads(lista.group(1)))
                return {"result": self._objeto(("lista", casam))}
            if expr.startswith("typeof window.__ac"):
                return {"result": {"value": 1}}
            if expr.startswith("(() => { for (const s of "):
                lista = json.loads(re.search(r"of (\[.*?\])\)", expr).group(1))
                return {"result": {"value": next((s for s in lista if self._casam([s])), 0)}}
            return {"result": {"value": None}}
        if metodo == "Runtime.callFunctionOn":
            casam = self._valor(params["objectId"])[1]
            if params.get("returnByValue"):
                valor = [{"multiplo": bool(self.campos[i].get("multiple")),
                          "aceita": _aceita_imagem(self.campos[i].get("accept", ""))} for i in casam]
                return {"result": {"value": valor}}
            indice = params["arguments"][0]["value"]
            return {"result": self._objeto(casam[indice])}
        if metodo == "DOM.requestNode":
            return {"nodeId": 100 + self._valor(params["objectId"])}
        return {}

    def _valor(self, objeto_id: str):
        return self.objetos[objeto_id]


def _aceita_imagem(accept: str) -> bool:
    """A mesma regra do JS do gravador e do enviar_arquivos (o falso responde com ela)."""
    return not accept or bool(re.search(r"(^|,)\s*(image/\*|image/jpeg|\.jpe?g)\s*(,|$)", accept, re.I))


# o envio de um vídeo pela Aba no código do bd7de20 (CA-35): nenhum comando do CDP a mais
METODOS_VIDEO_MAIN = ["Runtime.evaluate", "Runtime.evaluate", "Runtime.evaluate", "Runtime.evaluate",
                      "DOM.getDocument", "DOM.requestNode", "DOM.setFileInputFiles", "Runtime.releaseObject"]


class TestEnvio(Base):
    """Aba.enviar_arquivos com uma página falsa que anota os comandos do CDP (CA-35, DF-11)."""

    def aba(self, campos: list[dict]):
        ws = _WsFalso(campos)
        aba = navegador.Aba(ws, ritmo=(0, 0))
        ws.chamadas.clear()
        return aba, ws

    def arquivos(self, n: int, extensao: str = "jpg") -> list[Path]:
        saida = []
        for i in range(1, n + 1):
            arquivo = self.raiz / f"arquivo-{i}.{extensao}"
            arquivo.write_bytes(b"x")
            saida.append(arquivo)
        return saida

    def entregues(self, ws: _WsFalso) -> tuple[int, list[str]]:
        params = next(p for m, p in ws.chamadas if m == "DOM.setFileInputFiles")
        return params["nodeId"] - 100, params["files"]

    def test_video_faz_as_mesmas_chamadas_de_hoje(self):  # CA-35
        campos = [{"seletores": ["input[type=file]"], "accept": "video/*"}]
        aba, ws = self.aba(campos)
        video = self.arquivos(1, "mp4")[0]
        aba.enviar_arquivo("input[type=file]", video)
        self.assertEqual(ws.metodos(), METODOS_VIDEO_MAIN)
        self.assertEqual(self.entregues(ws), (0, [str(video.resolve())]))
        aba, ws = self.aba(campos)
        aba.enviar_arquivos(["input[type=file]"], [video])
        self.assertEqual(ws.metodos(), METODOS_VIDEO_MAIN)
        self.assertNotIn("Runtime.callFunctionOn", ws.metodos())

    def test_imagem_vai_para_o_campo_que_aceita_imagem(self):  # DF-11
        campos = [{"seletores": ["input[type=file]"], "accept": "video/mp4,video/*"},
                  {"seletores": ["input[type=file]"], "accept": "image/png, image/jpeg", "multiple": True}]
        aba, ws = self.aba(campos)
        fotos = self.arquivos(3)
        aba.enviar_arquivos(["input[type=file]"], fotos, midia="imagem")
        self.assertEqual(self.entregues(ws), (1, [str(f.resolve()) for f in fotos]))  # na ordem
        # os objetos que pedi à página são soltos
        soltos = [p["objectId"] for m, p in ws.chamadas if m == "Runtime.releaseObject"]
        self.assertEqual(len(soltos), 2)

    def test_nenhum_campo_aceita_imagem(self):  # DF-11
        aba, ws = self.aba([{"seletores": ["input[type=file]"], "accept": "video/*"},
                            {"seletores": ["input[type=file]"], "accept": ".mp4,.mov"}])
        with self.assertRaises(navegador.ErroNavegador) as erro:
            aba.enviar_arquivos("input[type=file]", self.arquivos(1), midia="imagem")
        self.assertEqual((erro.exception.tipo, erro.exception.parado), ("formato", True))
        self.assertNotIn("video", str(erro.exception))  # o accept não vai para a mensagem
        self.assertNotIn("DOM.setFileInputFiles", ws.metodos())

    def test_sem_multiple_com_tres_imagens(self):
        aba, ws = self.aba([{"seletores": ["input[type=file]"], "accept": "image/*"}])
        with self.assertRaises(navegador.ErroNavegador) as erro:
            aba.enviar_arquivos("input[type=file]", self.arquivos(3), midia="imagem")
        self.assertEqual((erro.exception.tipo, erro.exception.parado), ("formato", False))
        self.assertNotIn("DOM.setFileInputFiles", ws.metodos())
        # uma imagem só no mesmo campo passa
        aba, ws = self.aba([{"seletores": ["input[type=file]"], "accept": ""}])
        aba.enviar_arquivos("input[type=file]", self.arquivos(1), midia="imagem")
        self.assertEqual(self.entregues(ws)[0], 0)

    def test_arquivo_sumido(self):
        aba, ws = self.aba([{"seletores": ["input[type=file]"], "accept": "image/*", "multiple": True}])
        fotos = self.arquivos(2)
        fotos[1].unlink()
        with self.assertRaises(navegador.ErroNavegador) as erro:
            aba.enviar_arquivos("input[type=file]", fotos, midia="imagem")
        self.assertEqual(erro.exception.tipo, "corte")
        self.assertIn("a imagem não está mais em", str(erro.exception))
        self.assertEqual(ws.metodos(), [])

    def test_erros_com_parado_e_formato(self):
        self.assertFalse(navegador.ErroNavegador("x").parado)
        e = plat_base.ErroPublicacao("x", "formato", parado=True)
        self.assertEqual((e.tipo, e.parado, e.persistente), ("formato", True, False))
        self.assertFalse(plat_base.ErroPublicacao("x").parado)
        p = plat_base.Plataforma.__new__(plat_base.Plataforma)
        with self.assertRaises(plat_base.ErroPublicacao) as erro:
            p.publicar_imagens([], None)
        self.assertEqual(erro.exception.tipo, "formato")

    def test_link_de_post_de_fotos_do_tiktok(self):  # DF-12
        self.assertRegex("https://www.tiktok.com/@x/photo/123", plat_nav._LINK_POST)
        self.assertEqual(plat_nav._chave_do_link("https://www.tiktok.com/@x/photo/123"), "123")


class _AbaRoteiro:
    """Aba de mentira para o ViaNavegador: tudo dá certo na página, e ela anota o que o roteiro fez."""

    def __init__(self, erro_arquivo: Exception | None = None):
        self.parar = None
        self.feito: list[tuple] = []
        self.erro_arquivo = erro_arquivo
        self.links: list[str] = []

    def js(self, corpo: str, promessa: bool = False):
        if "a[href]" in corpo:
            self.feito.append(("links",))
            return list(self.links)
        return 1

    def esperar(self, expressao: str, segundos: float = 30, o_que: str = ""):
        self.feito.append(("esperar", o_que))
        return 1

    def navegar(self, url: str, segundos: float = 45) -> None:
        self.feito.append(("abrir", url))

    def iframes(self) -> list[str]:
        return []

    def existe(self, seletor: str) -> bool:
        return True

    def texto_dialogos(self) -> str:
        return ""

    def captura(self, destino: Path):
        return None

    def enviar_arquivo(self, seletor, arquivo: Path, segundos: float = 30) -> None:
        self.feito.append(("video", [arquivo]))

    def enviar_arquivos(self, seletor, arquivos, segundos: float = 30, midia: str = "video") -> None:
        if self.erro_arquivo is not None:
            raise self.erro_arquivo
        self.feito.append(("imagens", list(arquivos), midia))

    def clicar_alvos(self, alvos, segundos: float = 30, antes=None, o_que: str = "") -> int:
        if antes is not None:
            antes()
        self.feito.append(("clicar", o_que))
        return 0

    def digitar_alvos(self, alvos, texto: str, segundos: float = 30, o_que: str = "") -> None:
        self.feito.append(("escrever", texto))

    def pausa_fixa(self, segundos: float) -> None:
        pass

    def pausa(self, fator: float = 1.0) -> None:
        pass

    def tecla(self, nome: str = "Enter") -> None:
        pass


class _NavegadorFalso:
    aba: _AbaRoteiro | None = None

    def __init__(self, cfg, parar=None):
        pass

    def nova_aba(self, url: str):
        return _NavegadorFalso.aba

    def fechar_aba(self, aba) -> None:
        pass


class TestViaNavegadorImagem(Base):
    """O roteiro de imagem na repetição (C15), com o navegador e a aba de mentira."""

    def setUp(self):
        super().setUp()
        self.aba = _AbaRoteiro()
        _NavegadorFalso.aba = self.aba
        for alvo, novo in ((plat_nav, "Navegador"), (plat_nav, "esperar_sessao")):
            troca = patch.object(alvo, novo, _NavegadorFalso if novo == "Navegador" else (lambda *a, **k: None))
            troca.start()
            self.addCleanup(troca.stop)
        self.fotos = []
        for i in range(1, 4):
            foto = self.raiz / f"filme-{i}.jpg"
            foto.write_bytes(b"jpg")
            self.fotos.append(foto)
        self.conteudo = textos.Conteudo("O Filme - Parte 2", "A legenda do post #filme", ["#filme"])

    def gravar(self, nome: str, texto: str) -> None:
        arquivo = gravador.arquivo_roteiro(self.cfg, nome)
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo.write_text(texto, encoding="utf-8")

    def via(self, rede: str = "instagram") -> "plat_nav.ViaNavegador":
        return plat_nav.ViaNavegador(rede, self.cfg)

    def publicar(self, rede: str = "instagram") -> tuple[plat_base.Resultado, plat_base.Entrega]:
        entrega = plat_base.Entrega()
        return self.via(rede).publicar_imagens(self.fotos, self.conteudo, None, entrega), entrega

    def erro(self, rede: str = "instagram") -> plat_base.ErroPublicacao:
        with self.assertRaises(plat_base.ErroPublicacao) as erro:
            self.publicar(rede)
        return erro.exception

    def test_publica_as_imagens_na_ordem(self):
        self.gravar("instagram-imagem", ROTEIRO_IMAGEM)
        resultado, entrega = self.publicar()
        self.assertTrue(entrega.aceita)
        self.assertTrue(resultado.confirmado)
        self.assertIn(("imagens", self.fotos, "imagem"), self.aba.feito)
        # os links da página são lidos antes das imagens (senão um /p/ antigo viraria o link deste post)
        indice = self.aba.feito.index(("imagens", self.fotos, "imagem"))
        self.assertEqual(self.aba.feito[indice - 1], ("links",))
        self.assertIn(("escrever", "A legenda do post #filme"), self.aba.feito)

    def test_ensaio_para_antes_de_publicar(self):  # CA-07 (navegador)
        self.gravar("instagram-imagem", ROTEIRO_IMAGEM)
        self.cfg["navegador"]["ensaio"] = True
        entrega = plat_base.Entrega()
        with self.assertRaises(plat_base.ErroPublicacao) as erro:
            self.via().publicar_imagens(self.fotos, self.conteudo, None, entrega)
        self.assertEqual(erro.exception.tipo, "ensaio")
        self.assertFalse(entrega.aceita)
        cliques = [f[1] for f in self.aba.feito if f[0] == "clicar"]
        self.assertEqual(len(cliques), 2)  # "Criar" e "Original", e nunca o "Compartilhar"
        self.assertIn(("escrever", "A legenda do post #filme"), self.aba.feito)

    def test_roteiro_de_imagem_invalido_para_so_as_imagens(self):
        self.gravar("instagram-imagem", "abrir https://www.instagram.com/\nvideo\npublicar \"Compartilhar\"\n")
        e = self.erro()
        self.assertEqual((e.tipo, e.parado, e.persistente), ("formato", True, False))
        self.assertNotIn(("clicar", "Compartilhar"), self.aba.feito)

    def test_roteiro_de_imagem_sumido(self):
        e = self.erro()
        self.assertEqual((e.tipo, e.parado), ("formato", True))
        self.assertIn("o roteiro de imagem sumiu", str(e))
        self.assertEqual(self.aba.feito, [])  # nem abriu a página

    def test_roteiro_de_video_invalido_continua_bloqueio(self):
        self.gravar("instagram", "abrir https://www.instagram.com/\nimagens\npublicar \"Compartilhar\"\n")
        with self.assertRaises(plat_base.ErroPublicacao) as erro:
            self.via().publicar(self.fotos[0], self.conteudo, None, plat_base.Entrega())
        self.assertEqual((erro.exception.tipo, erro.exception.parado), ("bloqueio", False))

    def test_parado_do_campo_chega_ao_publicador(self):
        self.gravar("instagram-imagem", ROTEIRO_IMAGEM)
        self.aba.erro_arquivo = navegador.ErroNavegador("o campo de arquivo desta página não aceita imagem",
                                                        "formato", parado=True)
        e = self.erro()
        self.assertEqual((e.tipo, e.parado), ("formato", True))
        self.assertIn("a linha 3 do roteiro de imagem não funcionou", str(e))
        # sem o parado (campo sem multiple), ele também não aparece do outro lado
        self.aba.erro_arquivo = navegador.ErroNavegador("o campo aceita uma imagem por vez", "formato")
        e = self.erro()
        self.assertEqual((e.tipo, e.parado), ("formato", False))

    def test_campo_que_nao_aceita_imagem_para_as_imagens_da_rede(self):  # CA-30 (unidade)
        self.gravar("instagram-imagem", ROTEIRO_IMAGEM)
        self.aba.erro_arquivo = navegador.ErroNavegador("o campo de arquivo desta página não aceita imagem",
                                                        "formato", parado=True)
        cfg = self.cfg
        cfg["instagram"].update(ativo=True, imagens=True, envio="navegador")
        cfg["geral"]["simulacao"] = False
        conn = db.conectar(cfg.banco)
        self.addCleanup(conn.close)
        pub = publicador.Publicador(cfg, threading.Event())
        pub.plataformas["instagram"] = self.via()
        filme = conn.execute("INSERT INTO filmes (caminho, titulo, tipo, status, criado_em, atualizado_em) "
                             "VALUES ('x.mkv', 'O Filme', 'filme', 'analisado', 1, 1)").lastrowid
        corte = conn.execute("INSERT INTO cortes (filme_id, inicio, fim, pontuacao, status, parte, criado_em) "
                             "VALUES (?, 0, 10, 1, 'pronto', 1, 1)", (filme,)).lastrowid
        row = conn.execute("SELECT * FROM cortes WHERE id=?", (corte,)).fetchone()
        with patch.object(imagens, "preparar", side_effect=lambda *a, **k: _linha_de_imagem(conn, a[2], a[3],
                                                                                              self.fotos)):
            self.assertEqual(pub.postar_imagem(conn, "instagram", row, "momento"), "formato")
        self.assertIn("parado", planejador.ler_estado_imagens(conn, "instagram"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM postagens").fetchone()[0], 0)
        self.assertEqual(publicador.ler_bloqueios(conn), {})

    def test_titulo_no_roteiro_de_imagem_do_tiktok(self):
        self.gravar("tiktok-imagem", "abrir https://www.tiktok.com/tiktokstudio/upload\nimagens\n"
                                     "escrever titulo em input.titulo\nescrever legenda em div.legenda\n"
                                     "publicar \"Publicar\"\n")
        self.publicar("tiktok")
        self.assertIn(("escrever", "O Filme - Parte 2"), self.aba.feito)
        self.assertIn(("escrever", "A legenda do post #filme"), self.aba.feito)


def _passos_da_gravacao(rede: str) -> list[dict]:
    """O que o JS do gravador anota numa página que mostra o nome dos arquivos de exemplo (CA-05).

    Os quatro caminhos: o nome num span clicável, a miniatura com aria-label e title, o campo que a
    página preenche com o nome (aqui o título) e a frase da tela depois de publicar (no `confirmacao`).
    """
    titulo = "input[name=\"titulo\"]" if rede == "tiktok" else "input[aria-label=\"Texto alternativo\"]"
    return [
        {"tipo": "clicar", "seletores": ["button[aria-label=\"Criar\"]"], "texto": "Criar", "em": 1},
        {"tipo": "arquivo", "seletores": ["input[type=\"file\"]", "input[name=\"exemplo-1.jpg\"]"], "quantos": 3,
         "multiplo": True, "aceita_imagem": True, "em": 2},
        {"tipo": "clicar", "seletores": ["span.nome"], "texto": "exemplo-1.jpg", "em": 3},
        {"tipo": "clicar", "seletores": ["img[title=\"exemplo-2.jpg\"]", "div.miniatura > img"], "texto": "",
         "rotulo": "exemplo-2.jpg", "dica": "exemplo-2.jpg", "em": 4},
        {"tipo": "digitar", "seletores": [titulo], "valor": "exemplo-1", "chave": "1", "em": 5},
        {"tipo": "digitar", "seletores": ["div[contenteditable=\"true\"]"], "valor": "exemplo-1 @@LEGENDA@@",
         "chave": "2", "em": 6},
        {"tipo": "clicar", "seletores": ["div[role=\"button\"]"], "texto": "Compartilhar", "em": 7},
    ]


CONFIRMACAO_COM_EXEMPLO = "Início Sua publicação exemplo-1.jpg foi compartilhada Ver publicação"


class TestGravador(Base):
    """O gravador com o roteiro de imagem (C17): os nomes dos exemplos fora, sem_fotos e arrastou."""

    def salvar(self, nome: str, passos: list[dict], confirmacao: str = "", exemplos: bool = True) -> dict:
        return gravador.salvar(self.cfg, nome, passos, "https://www.instagram.com/", confirmacao, "", exemplos)

    def test_nome_dos_exemplos_fora_do_roteiro_e_do_log(self):  # CA-05 (unidade)
        for rede in ("instagram", "tiktok"):
            with self.subTest(rede=rede):
                nome = roteiro.nome_roteiro(rede, "imagem")
                passos = _passos_da_gravacao(rede)
                lista = gravador.lista_passos(nome, passos, exemplos=True)
                self.assertNotIn("exemplo", json.dumps(lista, ensure_ascii=False).lower())
                self.assertIn("escolheu 3 imagens", [p["texto"] for p in lista])
                r = self.salvar(nome, passos, CONFIRMACAO_COM_EXEMPLO)
                self.assertTrue(r["ok"], r)
                texto = gravador.carregar(self.cfg, nome)
                self.assertNotIn("exemplo", texto.lower())
                self.assertEqual(roteiro.analisar(texto, nome)[1], [])
                # a frase antes do nome do arquivo, nunca o pedaço depois do ponto do ".jpg"
                self.assertIn('conferir "Início Sua publicação"', texto)
                self.assertIn('imagens em input[type="file"]', texto)
                self.assertIn('escrever legenda em div[contenteditable="true"]', texto)  # "exemplo-1 @@LEGENDA@@"
                if rede == "tiktok":
                    self.assertIn('escrever titulo em input[name="titulo"]', texto)
                else:
                    self.assertIn(roteiro.MARCA_PREENCHIDO, texto)
                    self.assertTrue(any("nome do arquivo" in a for a in r["resumo"]["avisos"]), r["resumo"])
                self.assertEqual((r["quantos"], r["publicou"], r["publicar_incerto"], r["so_uma"]),
                                 (3, True, False, False))
                self.assertEqual(Path(r["arquivo"]).name, f"{nome}.txt")
        # o roteiro de vídeo não filtra nada
        limpos = gravador.limpar_passos("instagram", _passos_da_gravacao("instagram"))
        self.assertTrue(any(p.get("texto") == "exemplo-1.jpg" for p in limpos))

    def test_marca_trocada_pela_legenda_de_verdade(self):  # DF-2
        passos = _passos_da_gravacao("instagram")
        passos.append({"tipo": "digitar", "seletores": ["div[contenteditable=\"true\"]"],
                       "valor": "Trecho de O Filme - Parte 2 #filme", "chave": "3", "em": 6.5})
        limpos = gravador.limpar_passos("instagram-imagem", passos, exemplos=True)
        legenda = [p for p in limpos if p.get("seletores") == ["div[contenteditable=\"true\"]"]]
        self.assertEqual(len(legenda), 1)
        self.assertEqual(legenda[0]["papel"], "legenda")
        self.assertIn('escrever legenda em div[contenteditable="true"]', self.salvar("instagram-imagem", passos)["texto"])

    def test_valor_proprio_ganha_do_que_a_pagina_preencheu(self):
        campo = ["input[name=\"titulo\"]"]
        passos = [{"tipo": "digitar", "seletores": campo, "valor": "exemplo-1", "em": 1},
                  {"tipo": "digitar", "seletores": campo, "valor": "Meu título", "em": 2},
                  {"tipo": "digitar", "seletores": campo, "valor": "exemplo-1", "em": 3}]
        limpos = gravador.limpar_passos("tiktok-imagem", passos, exemplos=True)
        self.assertEqual(limpos, [{"tipo": "digitar", "seletores": campo, "valor": "Meu título"}])
        so_pagina = gravador.limpar_passos("tiktok-imagem", passos[:1], exemplos=True)
        self.assertEqual(so_pagina, [{"tipo": "digitar", "seletores": campo, "preenchido_pela_pagina": True}])
        self.assertEqual(gravador.descrever(so_pagina[0]),
                         'a página preencheu um campo com o nome do arquivo (input[name="titulo"])')

    def test_campo_da_pagina_nao_rouba_a_legenda(self):
        # sem marca nenhuma: a legenda é deduzida pelo campo que você escreveu, nunca pelo título da página
        passos = [{"tipo": "arquivo", "seletores": ["input[type=\"file\"]"], "quantos": 1, "em": 1},
                  {"tipo": "digitar", "seletores": ["input[name=\"titulo\"]"], "valor": "exemplo-1", "em": 2},
                  {"tipo": "digitar", "seletores": ["div.legenda"], "valor": "minha legenda", "em": 3}]
        a = gravador.analisar("tiktok-imagem", passos, exemplos=True)
        self.assertEqual(a["inferido"], "legenda")
        self.assertEqual([p.get("papel") for p in a["passos_limpos"] if p["tipo"] == "digitar"], [None, "legenda"])

    def test_mascarar_exemplos(self):
        diag = {"campos": 2, "com_texto": [{"seletor": "input[name=\"exemplo-1.jpg\"]", "tamanho": 9}],
                "url": "https://x/", "quadros": ["Exemplo 2", "ok"]}
        self.assertEqual(gravador.mascarar_exemplos(diag),
                         {"campos": 2, "com_texto": [{"seletor": "(nome do arquivo)", "tamanho": 9}],
                          "url": "https://x/", "quadros": ["(nome do arquivo)", "ok"]})
        self.assertEqual(gravador.NOMES_EXEMPLO[0], "exemplo-1.jpg")
        self.assertTrue(all(gravador._tem_nome_de_exemplo(n) for n in gravador.NOMES_EXEMPLO))
        self.assertFalse(gravador._tem_nome_de_exemplo("Por exemplo, o filme"))

    def test_desfazer_na_gravacao_de_imagem(self):
        g = plat_nav.Gravacao(self.cfg, "instagram", "imagem", exemplos=list(gravador.NOMES_EXEMPLO[:3]),
                              exemplo={"corte_id": 1, "imagem_id": 1, "quadros": 3}, sessao=77)
        self.assertEqual((g.nome, g.sessao, g.midia), ("instagram-imagem", 77, "imagem"))
        g.passos = _passos_da_gravacao("instagram")[:4]  # o último é o clique na miniatura (escondido)
        antes = g.situacao()
        self.assertEqual(antes["passos"], 2)  # Criar e a escolha das imagens
        self.assertEqual(antes["midia"], "imagem")
        depois = g.desfazer()
        self.assertEqual(depois["passos"], 1)  # saiu a escolha das imagens, o passo visível
        self.assertEqual([p["texto"] for p in depois["lista"]], ["clicou em \u201cCriar\u201d"])
        self.assertEqual(plat_nav.Gravacao(self.cfg, "instagram").midia, "video")
        self.assertLess(plat_nav.proxima_sessao(), plat_nav.proxima_sessao())  # cada uma com o seu número

    def test_campo_que_nao_aceita_foto(self):  # CA-22 (unidade)
        passos = _passos_da_gravacao("instagram")
        passos[1] = dict(passos[1], aceita_imagem=False)
        r = self.salvar("instagram-imagem", passos)
        self.assertEqual((r["ok"], r["motivo"]), (False, "sem_fotos"))
        self.assertIn("não aceita imagem", r["mensagem"])
        self.assertFalse(gravador.arquivo_roteiro(self.cfg, "instagram-imagem").exists())
        # sem a chave (JS antigo), não dá sem_fotos
        passos[1].pop("aceita_imagem")
        self.assertTrue(self.salvar("instagram-imagem", passos)["ok"])

    def test_arrastou_e_chaves_estranhas(self):  # DF-3
        passos = [p for p in _passos_da_gravacao("instagram") if p["tipo"] != "arquivo"]
        passos.insert(1, {"tipo": "soltou", "quantos": 3, "em": 2})
        r = self.salvar("instagram-imagem", passos)
        self.assertEqual((r["ok"], r["motivo"], r["quantos"]), (False, "arrastou", 0))
        self.assertIn("arrastou", r["mensagem"])
        self.assertIn("arrastou o arquivo", " ".join(p["texto"] for p in
                                                    gravador.lista_passos("instagram-imagem", passos, True)))
        # quantos estranho perde só a chave; o passo do arquivo fica
        passos = _passos_da_gravacao("instagram")
        passos[1] = dict(passos[1], quantos="três", multiplo="sim")
        limpos = gravador.limpar_passos("instagram-imagem", passos, exemplos=True)
        arquivo = next(p for p in limpos if p["tipo"] == "arquivo")
        self.assertNotIn("quantos", arquivo)
        self.assertNotIn("multiplo", arquivo)
        self.assertTrue(arquivo["aceita_imagem"])
        r = self.salvar("instagram-imagem", passos)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["quantos"], 0)

    def test_uma_imagem_por_vez(self):
        passos = _passos_da_gravacao("instagram")
        passos[1] = dict(passos[1], multiplo=False, quantos=1)
        r = self.salvar("instagram-imagem", passos)
        self.assertTrue(r["ok"])
        self.assertTrue(r["so_uma"])
        self.assertIn("aceita uma imagem por vez", r["texto"])
        self.assertIn("escolheu 1 imagem", [p["texto"] for p in gravador.lista_passos("instagram-imagem", passos,
                                                                                       True)])

    def test_papeis_com_os_opcionais(self):
        self.assertEqual(set(gravador.papeis("tiktok-imagem")), {"legenda", "titulo"})
        self.assertEqual(set(gravador.papeis("instagram-imagem")), {"legenda"})
        self.assertEqual(gravador.papeis("tiktok"), {p: roteiro.MARCAS[p] for p in roteiro.PAPEIS_DA_REDE["tiktok"]})

    def test_roteiro_de_video_sai_igual(self):
        # os passos de vídeo com as chaves novas do JS geram o mesmo texto de sem elas
        passos = [{"tipo": "arquivo", "seletores": ["input[type=\"file\"]"], "em": 1},
                  {"tipo": "digitar", "seletores": ["div.legenda"], "valor": "@@LEGENDA@@", "em": 2},
                  {"tipo": "clicar", "seletores": ["button.post"], "texto": "Publicar", "em": 3}]
        com_chaves = [dict(passos[0], quantos=1, multiplo=False, aceita_imagem=False)] + passos[1:]
        a, b = gravador.analisar("tiktok", passos), gravador.analisar("tiktok", com_chaves)
        gerar = lambda x: re.sub(r"gravado em .*", "", roteiro.gerar("tiktok", x["passos_limpos"], "u", "",  # noqa: E731
                                                                     x["publicar_em"], x["publicar_incerto"]))
        self.assertEqual(gerar(a), gerar(b))
        self.assertFalse(b["so_uma"])
        self.assertEqual(gravador.descrever(b["passos_limpos"][0]), "escolheu o vídeo")


def _linha_de_imagem(conn, corte, tipo: str, arquivos: list[Path]):
    """A linha de imagens que o preparar devolveria, apontando para os arquivos dados."""
    conn.execute(
        "INSERT INTO imagens (corte_id, tipo, formato, arquivos, quadros, texto, com_chamada, assinatura, modelo, "
        "criado_em) VALUES (?, ?, '4x5', ?, '[]', ?, 0, 'x', '', 1) ON CONFLICT(corte_id, tipo, formato) DO UPDATE "
        "SET arquivos = excluded.arquivos",
        (corte["id"], tipo, json.dumps([str(a) for a in arquivos]),
         corte["frase"] if tipo == "frase" else None))
    return conn.execute("SELECT * FROM imagens WHERE corte_id=? AND tipo=? AND formato='4x5'",
                        (corte["id"], tipo)).fetchone()


class _PlataformaFalsa:
    """Plataforma de mentira: anota o que recebeu (nomes e se os arquivos existiam) e faz o que `acao` mandar."""

    via = "navegador"

    def __init__(self, rotulo: str = "Instagram (navegador)"):
        self.rotulo = rotulo
        self.envios: list[dict] = []
        self.videos: list[str] = []
        self.acao = None  # acao(arquivos, entrega) -> Resultado, ou levanta

    def pronta(self) -> tuple[bool, str]:
        return True, "ok"

    def publicar_imagens(self, arquivos, conteudo, parar=None, entrega=None):
        self.envios.append({"nomes": [a.name for a in arquivos], "pasta": arquivos[0].parent,
                            "existem": all(a.is_file() for a in arquivos), "legenda": conteudo.descricao})
        if self.acao is not None:
            return self.acao(arquivos, entrega)
        return plat_base.Resultado("navegador-abc", "https://www.instagram.com/p/abc/")

    def publicar(self, arquivo, conteudo, parar=None, entrega=None):
        self.videos.append(Path(arquivo).name)
        return plat_base.Resultado("navegador-video")


class _Agora(datetime):
    """datetime com o now() preso no momento do teste (o ciclo do publicador lê datetime.now())."""

    momento: datetime | None = None

    @classmethod
    def now(cls, tz=None):
        return cls.momento


class TestPublicador(BaseImagens):
    """postar_imagem e o bloco de imagem do ciclo (C16), com o preparar e a plataforma de mentira."""

    def setUp(self):
        super().setUp()
        self.pub = publicador.Publicador(self.cfg, threading.Event())
        self.ig = _PlataformaFalsa()
        self.pub.plataformas["instagram"] = self.ig
        self.erros_preparar: list[Exception | None] = []
        self.preparos: list[tuple] = []
        troca = patch.object(imagens, "preparar", side_effect=self.preparar_falso)
        troca.start()
        self.addCleanup(troca.stop)
        self.corte = self.novo_corte()
        # o vídeo do corte saiu há 3 h (fora do intervalo mínimo e sem atender horário de hoje)
        self.tres_horas = time.time() - 3 * 3600
        self.postagem(self.corte, "instagram", "publicado", criado_em=self.tres_horas, horario=self.tres_horas)

    def preparar_falso(self, cfg, conn, corte, tipo, rede, parar=None, quadros=None, prazo=300, post_simulado=None):
        self.preparos.append((corte["id"], tipo, rede))
        if self.erros_preparar:
            erro = self.erros_preparar.pop(0)
            if erro is not None:
                raise erro
        pasta = imagens.pasta_imagens(cfg, corte["id"])
        pasta.mkdir(parents=True, exist_ok=True)
        fontes = []
        for i in range(1, (3 if tipo == "carrossel" else 1) + 1):
            arquivo = pasta / f"{tipo}-4x5-{i}.jpg"
            arquivo.write_bytes(b"jpg")
            fontes.append(arquivo)
        return _linha_de_imagem(conn, corte, tipo, fontes)

    def postar(self, tipo: str = "momento", rede: str = "instagram", **kw) -> str:
        return self.pub.postar_imagem(self.conn, rede, self.corte_row(self.corte), tipo, **kw)

    def linhas(self, rede: str = "instagram") -> list[tuple]:
        return [tuple(r) for r in self.conn.execute(
            "SELECT status, midia, imagem_tipo, erro FROM postagens WHERE plataforma=? AND midia='imagem' "
            "ORDER BY id", (rede,))]

    def envio(self, rede: str = "instagram") -> Path:
        return imagens.pasta_imagens(self.cfg, self.corte) / "envio" / rede

    def falhar(self, erro: Exception, aceitar: bool = False):
        def acao(arquivos, entrega):
            if aceitar:
                entrega.aceitou("navegador-abc")
            raise erro
        self.ig.acao = acao

    def momento_de_imagem(self) -> tuple[datetime, str]:
        horario, chave = agenda.horarios_com_chave(self.cfg, "instagram", date.today(), "imagem")[0]
        return horario + timedelta(minutes=1), chave

    def test_simulado(self):
        self.cfg["geral"]["simulacao"] = True
        self.assertEqual(self.postar("carrossel"), "simulado")
        self.assertEqual(self.linhas(), [("simulado", "imagem", "carrossel", None)])
        self.assertEqual(self.ig.envios, [])
        self.assertFalse(self.envio().exists())
        imagem_id = self.conn.execute("SELECT imagem_id FROM postagens WHERE midia='imagem'").fetchone()[0]
        self.assertIsNotNone(imagem_id)

    def test_publicado_com_copias_de_nome_neutro(self):  # CA-06 (sucesso) e CA-09
        self.assertEqual(self.postar("carrossel"), "publicado")
        envio = self.ig.envios[0]
        self.assertEqual(envio["nomes"], ["filme-teste-1.jpg", "filme-teste-2.jpg", "filme-teste-3.jpg"])
        self.assertEqual(envio["pasta"], self.envio())
        self.assertTrue(envio["existem"])
        self.assertFalse(self.envio().exists())
        linha = self.conn.execute("SELECT status, url, imagem_id FROM postagens WHERE midia='imagem'").fetchone()
        self.assertEqual((linha["status"], linha["url"]), ("publicado", "https://www.instagram.com/p/abc/"))
        self.assertIsNotNone(linha["imagem_id"])
        # um corte com o vídeo ainda por sair continua na fila da rede e não conclui (CA-09)
        outro = self.novo_corte(inicio=300.0, fim=340.0)
        self.assertEqual(self.pub.postar_imagem(self.conn, "instagram", self.corte_row(outro), "momento"),
                         "publicado")
        self.assertEqual([c["id"] for c in planejador.fila(self.cfg, self.conn, "instagram")], [outro])
        self.pub.atualizar_concluidos(self.conn)
        self.assertEqual(self.corte_row(outro)["status"], "pronto")

    def test_erro_tambem_apaga_as_copias(self):  # CA-06 (erro)
        self.falhar(plat_base.ErroPublicacao("o seletor sumiu"))
        self.assertEqual(self.postar("carrossel"), "falhou")
        self.assertTrue(self.ig.envios[0]["existem"])
        self.assertFalse(self.envio().exists())
        self.assertEqual(self.linhas(), [("falhou", "imagem", "carrossel", "o seletor sumiu")])

    def test_titulo_com_cara_de_exemplo_vira_imagem(self):
        self.conn.execute("UPDATE filmes SET titulo='Exemplo 1' WHERE id=?", (self.filme,))
        self.postar()
        self.assertEqual(self.ig.envios[0]["nomes"], ["imagem-1.jpg"])

    def test_aceita_e_erro_vira_conferir(self):
        self.falhar(plat_base.ErroPublicacao("a página travou"), aceitar=True)
        self.assertEqual(self.postar(), "conferir")
        status, _, _, erro = self.linhas()[0]
        self.assertEqual(status, "conferir")
        self.assertTrue(erro.startswith("A rede recebeu a imagem"))
        self.assertFalse(self.envio().exists())

    def test_ensaio_ocupa_o_horario_e_o_corte_continua(self):  # CA-07
        momento, chave = self.momento_de_imagem()
        tipo = self.fila()[0][1]
        self.falhar(plat_base.ErroPublicacao("ensaio: preenchi tudo e NÃO publiquei", "ensaio"))
        self.assertEqual(self.pub.postar_imagem(self.conn, "instagram", self.corte_row(self.corte), tipo,
                                                horario=momento, chave=chave), "ensaio")
        self.assertEqual(self.linhas()[0][:3], ("ensaio", "imagem", tipo))
        self.assertIsNone(self.pub.horario_devido(self.conn, "instagram", momento, midia="imagem"))
        # o rodízio passa a vez ao tipo seguinte, mas o mesmo corte e tipo continuam possíveis
        self.assertEqual(self.fila(forcar_tipo=tipo)[0], (self.corte, tipo))

    def test_horario_de_imagem_posta_o_corte_do_video(self):  # CA-08
        outro = self.novo_corte(inicio=300.0, fim=340.0)  # sem vídeo publicado: não é a vez dele
        esperado = self.fila()[0]
        self.assertEqual(esperado[0], self.corte)
        momento, chave = self.momento_de_imagem()
        self.pub._ciclo_imagem(self.conn, "instagram", self.ig, momento)
        linha = self.conn.execute("SELECT corte_id, imagem_tipo, slot FROM postagens WHERE midia='imagem'").fetchone()
        self.assertEqual((linha["corte_id"], linha["imagem_tipo"], linha["slot"]), (self.corte, esperado[1], chave))
        self.assertEqual(len(self.ig.envios), 1)
        self.assertEqual(len(self.ig.envios[0]["nomes"]), 3 if esperado[1] == "carrossel" else 1)
        self.assertNotIn(outro, [p[0] for p in self.preparos])

    def test_bloqueio_pausa_a_rede_e_os_videos(self):  # CA-12
        self.falhar(plat_base.ErroPublicacao("a rede pediu verificação", "bloqueio", persistente=True))
        self.assertEqual(self.postar(), "bloqueado")
        self.assertEqual(self.linhas(), [])
        self.assertIn("instagram", publicador.ler_bloqueios(self.conn))
        # o ciclo no horário de vídeo não posta o vídeo da rede pausada
        self.novo_corte(inicio=300.0, fim=340.0)
        horario = agenda.horarios_com_chave(self.cfg, "instagram", date.today())[0][0] + timedelta(minutes=1)
        _Agora.momento = horario
        with patch.object(publicador, "datetime", _Agora):
            self.pub.ciclo(self.conn)
            self.assertEqual(self.ig.videos, [])
            publicador.desbloquear(self.conn, "instagram", persistente=True)  # sem a pausa, ele sairia
            self.pub.ciclo(self.conn)
        self.assertEqual(len(self.ig.videos), 1)

    def test_carrossel_sem_multiple_passa_a_vez(self):  # CA-13
        # a última imagem foi uma frase: o carrossel é a vez
        self.postagem(self.corte, "instagram", "publicado", "imagem", "frase", criado_em=self.tres_horas - 60,
                      horario=self.tres_horas - 60)
        self.assertEqual(self.fila()[0][1], "carrossel")

        def acao(arquivos, entrega):
            if len(arquivos) > 1:
                raise plat_base.ErroPublicacao("o campo de arquivo aceita uma imagem por vez", "formato")
            return plat_base.Resultado("navegador-abc")
        self.ig.acao = acao
        momento, _ = self.momento_de_imagem()
        self.pub._ciclo_imagem(self.conn, "instagram", self.ig, momento)
        self.assertEqual([len(e["nomes"]) for e in self.ig.envios], [3, 1])  # o mesmo horário tenta o seguinte
        novas = [r for r in self.linhas() if r[1] == "imagem" and r[2] != "frase"]
        self.assertEqual([r[:3] for r in novas], [("publicado", "imagem", "momento")])
        estado = planejador.ler_estado_imagens(self.conn, "instagram")
        self.assertEqual(list(estado), ["sem_carrossel"])
        self.assertEqual(publicador.ler_bloqueios(self.conn), {})
        self.assertNotIn("carrossel", [t for _, t in self.fila(quantidade=3)])

    def test_formato_num_momento_para_as_imagens(self):  # CA-13 (o resto)
        self.falhar(plat_base.ErroPublicacao("o campo de arquivo desta página não aceita imagem", "formato"))
        self.assertEqual(self.postar("momento"), "formato")
        self.assertIn("parado", planejador.ler_estado_imagens(self.conn, "instagram"))
        self.assertEqual(self.linhas(), [])
        self.assertEqual(publicador.ler_bloqueios(self.conn), {})
        # o ciclo seguinte nem chama o envio das imagens desta rede
        momento, _ = self.momento_de_imagem()
        self.pub._ciclo_imagem(self.conn, "instagram", self.ig, momento)
        self.assertEqual(len(self.ig.envios), 1)
        # o carrossel com parado também para (nunca vira só "sem_carrossel")
        planejador.limpar_estado_imagens(self.conn, "instagram")
        self.falhar(plat_base.ErroPublicacao("o roteiro de imagem sumiu", "formato", parado=True))
        self.assertEqual(self.postar("carrossel"), "formato")
        self.assertEqual(list(planejador.ler_estado_imagens(self.conn, "instagram")), ["parado"])

    def test_roteiro_apagado_antes_do_envio(self):
        via = plat_nav.ViaNavegador("instagram", self.cfg)
        self.pub.plataformas["instagram"] = via
        roteiro_imagem = gravador.arquivo_roteiro(self.cfg, "instagram-imagem")

        def apaga_e_prepara(*args, **kw):
            roteiro_imagem.unlink()  # depois do imagens_prontas, antes do envio
            return self.preparar_falso(*args, **kw)

        with patch.object(imagens, "preparar", side_effect=apaga_e_prepara), \
                patch.object(plat_nav, "Navegador", side_effect=AssertionError("não devia abrir o navegador")):
            self.assertEqual(self.postar(), "formato")
        self.assertEqual(list(planejador.ler_estado_imagens(self.conn, "instagram")), ["parado"])
        self.assertEqual(publicador.ler_bloqueios(self.conn), {})
        # o vídeo seguinte sai
        self.pub.plataformas["instagram"] = self.ig
        self.assertEqual(self.pub.postar(self.conn, "instagram", self.corte_row(self.corte)), "publicado")

    def test_quatro_falhas_viram_pulado(self):
        self.falhar(plat_base.ErroPublicacao("o seletor sumiu"))
        resultados = [self.postar() for _ in range(planejador.FALHAS_POR_CORTE)]
        self.assertEqual(resultados, ["falhou"] * (planejador.FALHAS_POR_CORTE - 1) + ["pulado"])
        self.assertEqual(self.linhas()[-1][:3], ("pulado", "imagem", "momento"))

    def test_falha_do_pc_na_geracao_nao_gasta_o_corte(self):  # DF-5
        self.erros_preparar = [imagens.ErroImagem("o FFmpeg falhou", False)] * 5
        resultados = [self.postar() for _ in range(5)]
        self.assertEqual(resultados, ["falhou"] * 5)
        self.assertTrue(all(r[0] == "falhou" and r[3].startswith("geração:") for r in self.linhas()))
        self.assertIn("instagram-geracao", self.pub._avisos)
        self.assertEqual(self.ig.envios, [])
        self.assertEqual(self.fila()[0], (self.corte, "momento"))
        # o FFmpeg volta: o mesmo corte sai
        self.assertEqual(self.postar(), "publicado")

    def test_trecho_que_nao_serve_vira_pulado(self):
        self.erros_preparar = [imagens.ErroImagem("o trecho não tem fala", True)]
        self.assertEqual(self.postar("frase"), "pulado")
        self.assertEqual(self.linhas(), [("pulado", "imagem", "frase", "o trecho não tem fala")])
        self.assertEqual(self.pub._recusas, {})

    def test_filme_fora_da_pasta_nao_grava_linha(self):  # CA-27
        self.erros_preparar = [imagens.ErroImagem("o arquivo do filme não está na pasta agora", False,
                                                  sem_linha=True)]
        self.assertEqual(self.postar(), "sem_imagem")
        self.assertEqual(self.linhas(), [])
        self.assertIn("instagram-filme-ausente", self.pub._avisos)
        self.assertEqual(self.postar(), "publicado")  # o arquivo voltou
        self.assertEqual(self.preparos, [(self.corte, "momento", "instagram")] * 2)

    def test_redes_diferentes_nao_apagam_as_copias_uma_da_outra(self):  # DF-6
        self.so_redes("instagram", "tiktok")
        self.cfg["tiktok"].update(imagens=True, envio="navegador")
        self.roteiro_imagem("tiktok")
        tk = _PlataformaFalsa("TikTok (navegador)")
        self.pub.plataformas["tiktok"] = tk
        vistos = {}

        def acao(arquivos, entrega):
            self.assertEqual(self.pub.postar_imagem(self.conn, "tiktok", self.corte_row(self.corte), "momento"),
                             "publicado")
            vistos["depois"] = all(a.is_file() for a in arquivos)
            return plat_base.Resultado("navegador-abc")
        self.ig.acao = acao
        self.assertEqual(self.postar("carrossel"), "publicado")
        self.assertTrue(vistos["depois"])
        self.assertEqual(tk.envios[0]["pasta"], self.envio("tiktok"))
        self.assertFalse(self.envio("tiktok").exists())
        self.assertFalse(self.envio("instagram").exists())

    def test_a_linha_nasce_antes_de_soltar_a_trava(self):  # DF-7
        vistos = []
        conn = self.conn
        envio = self.envio()

        class Espia:
            def __init__(self):
                self.trava = threading.Lock()

            def acquire(self, *a, **k):
                return self.trava.acquire(*a, **k)

            def release(self):
                vistos.append(([r[0] for r in conn.execute("SELECT status FROM postagens WHERE midia='imagem'")],
                               sorted(p.name for p in envio.iterdir()) if envio.exists() else []))
                self.trava.release()

            def locked(self):
                return self.trava.locked()

        with patch.object(imagens, "TRAVA", Espia()):
            self.assertEqual(self.postar("carrossel"), "publicado")
        self.assertEqual(vistos, [(["enviando"], ["filme-teste-1.jpg", "filme-teste-2.jpg", "filme-teste-3.jpg"])])

    def test_ocupado_e_bloqueado_nao_gravam_nada(self):
        with publicador.TRAVAS_REDE["instagram"]:
            self.assertEqual(self.postar(), "ocupado")
        gravador.arquivo_roteiro(self.cfg, "instagram-imagem").unlink()
        self.assertEqual(self.postar(), "bloqueado")
        self.assertEqual(self.linhas(), [])
        self.assertEqual(self.preparos, [])

    def test_com_as_imagens_desligadas_nada_muda(self):  # RNF-01
        _Agora.momento = self.momento_de_imagem()[0]  # o horário de imagem que sairia com elas ligadas
        self.cfg["instagram"]["imagens"] = False
        with patch.object(publicador, "datetime", _Agora), \
                patch.object(planejador, "fila_imagens", side_effect=AssertionError("não devia olhar as imagens")):
            self.pub.ciclo(self.conn)
        self.assertEqual(self.preparos, [])
        self.assertEqual(self.ig.envios, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM postagens WHERE midia='imagem'").fetchone()[0], 0)


class _Tratador:
    """O lado do servidor que a rota de mídia usa: anota o arquivo que seria enviado."""

    def __init__(self):
        self.enviados: list[tuple[Path, str]] = []

    def enviar_arquivo(self, caminho, tipo):
        self.enviados.append((Path(caminho), tipo))


class _PublicadorFalso:
    """Publicador de mentira para as rotas do painel: anota o postar_imagem."""

    chamadas: list[tuple] = []

    def __init__(self, cfg, parar):
        pass

    def postar_imagem(self, conn, nome, corte, tipo, horario=None, manual=False, chave=None):
        _PublicadorFalso.chamadas.append((nome, corte["id"], tipo, manual))
        return "publicado"


class TestPainel(BaseImagens):
    """As rotas do painel para as imagens (C18), com o painel montado à mão."""

    def setUp(self):
        super().setUp()
        self.cfg["transcricao"]["fonte"] = "nenhuma"
        motor = SimpleNamespace(publicador=None, rodando=False, parando=False, iniciado_em=None)
        self.painel = SimpleNamespace(cfg=self.cfg, motor=motor, gravacoes={}, logins={}, reinicio={},
                                      encerrar=threading.Event(), conta_canais=None,
                                      conectar=lambda: db.conectar(self.cfg.banco))
        self.corte = self.novo_corte()
        _PublicadorFalso.chamadas = []

    def ctx(self, corpo=None, consulta=None):
        return SimpleNamespace(cfg=self.cfg, conn=self.conn, painel=self.painel, corpo=corpo or {},
                               consulta=consulta or {}, tratador=None)

    def erro(self, funcao, *args, corpo=None) -> ErroHttp:
        with self.assertRaises(ErroHttp) as erro:
            funcao(self.ctx(corpo), *args)
        return erro.exception

    def postar(self, tipo: str = "momento", rede: str = "instagram", corte: int | None = None):
        return api.postar_imagem_corte(self.ctx({"rede": rede, "tipo": tipo}), str(corte or self.corte))

    def alertas(self) -> list[dict]:
        redes = api._resumo_redes(self.ctx())
        return api._alertas(self.ctx(), {"analisado": 1}, {}, {"dias": None}, redes, {}, 0)

    def sincrono(self):
        """O trabalho em segundo plano roda na hora, com o publicador de mentira."""
        pilha = contextlib.ExitStack()
        pilha.enter_context(patch.object(api, "_em_segundo_plano", side_effect=lambda nome, trabalho: trabalho()))
        pilha.enter_context(patch.object(api, "Publicador", _PublicadorFalso))
        return pilha

    def test_envio_errado_no_estado_e_alerta(self):  # CA-02
        self.cfg["instagram"]["envio"] = "oficial"
        redes = api._resumo_redes(self.ctx())
        ig = redes["instagram"]
        self.assertEqual(ig["imagens_motivo"], config.SO_PELO_NAVEGADOR)
        self.assertFalse(ig["imagens_disponivel"])
        self.assertTrue(ig["imagens"])
        self.assertIsNone(ig["proximo_imagem"])
        self.assertEqual(redes["youtube"]["imagens_motivo"], config.IMAGEM_INDISPONIVEL["youtube"])
        self.assertNotIn("roteiro_imagem", redes["youtube"])
        alerta = [a for a in self.alertas() if config.SO_PELO_NAVEGADOR in a["texto"]]
        self.assertEqual([a["destino"] for a in alerta], ["redes"])

    def test_resumo_com_as_imagens_prontas(self):
        ig = api._resumo_redes(self.ctx())["instagram"]
        self.assertTrue(ig["roteiro_imagem"]["tem"])
        self.assertEqual(ig["roteiro_imagem"]["midia"], "imagem")
        self.assertEqual((ig["imagens_prontas"], ig["imagens_prontas_motivo"]), (True, ""))
        self.assertIsNotNone(ig["proximo_imagem"])
        self.assertEqual(ig["imagens_por_semana"], agenda.posts_por_semana(self.cfg, "instagram", midia="imagem"))
        self.assertGreater(ig["imagens_por_semana"], 0)
        self.assertEqual(ig["estado_imagens"], {})
        self.assertFalse([a for a in self.alertas() if "imagens" in a["texto"].lower()])
        # sem o roteiro de imagem: o alerta leva para Redes sociais
        gravador.arquivo_roteiro(self.cfg, "instagram-imagem").unlink()
        alerta = [a for a in self.alertas() if "as imagens não saem" in a["texto"]]
        self.assertEqual([a["destino"] for a in alerta], ["redes"])
        self.assertIn("falta gravar o roteiro de imagem", alerta[0]["texto"])

    def test_sem_tipo_que_renda_leva_as_configuracoes(self):  # DF-12
        self.cfg["imagens"]["tipos"] = ["carrossel"]
        planejador.gravar_estado_imagens(self.conn, "instagram", {"sem_carrossel": "uma imagem por vez"})
        alerta = [a for a in self.alertas() if "Nenhum corte editado serve" in a["texto"]]
        self.assertEqual([a["destino"] for a in alerta], ["config?aba=imagens"])

    def test_meta_config(self):
        with patch.object(api.sistema, "fontes_disponiveis", return_value=[]), \
                patch.object(api.sistema, "inicializacao_ativa", return_value=False), \
                patch.object(api.sistema, "inicializacao_de_outra_pasta", return_value=False), \
                patch.object(api.sistema, "info_ia", return_value={}):
            meta = api._meta_config(self.ctx())
        self.assertEqual(meta["tipos_imagem"], ["momento", "frase", "carrossel"])
        self.assertEqual(meta["redes_imagem"], ["tiktok", "instagram"])
        self.assertEqual(meta["formatos_imagem"]["instagram"]["nome"], "4x5")
        self.assertIn("kwai", meta["imagem_indisponivel"])

    def test_postar_imagem_recusa(self):  # CA-18 e CA-26
        casos = [
            ({"rede": "instagram", "tipo": "outro"}, 400, "Tipo de imagem inválido"),
            ({"rede": "youtube", "tipo": "momento"}, 400, "Rede sem imagens"),
        ]
        for corpo, status, texto in casos:
            e = self.erro(api.postar_imagem_corte, str(self.corte), corpo=corpo)
            self.assertEqual((e.status, texto in str(e)), (status, True), corpo)
        candidato = self.novo_corte(status="candidato", parte=None, inicio=300.0, fim=340.0)
        e = self.erro(api.postar_imagem_corte, str(candidato), corpo={"rede": "instagram", "tipo": "momento"})
        self.assertEqual((e.status, str(e)), (400, "Este corte ainda não foi editado"))
        sem_fala = self.novo_corte(inicio=400.0, fim=440.0, frase=None)
        e = self.erro(api.postar_imagem_corte, str(sem_fala), corpo={"rede": "instagram", "tipo": "frase"})
        self.assertEqual((e.status, str(e)), (400, "Este trecho não tem fala"))
        pauta = self.novo_corte(self.novo_filme("Pauta", tipo="pauta"), inicio=0.0, fim=30.0)
        e = self.erro(api.postar_imagem_corte, str(pauta), corpo={"rede": "instagram", "tipo": "momento"})
        self.assertIn("pauta", str(e))
        # a rede pausada: 409, como o "Postar agora" do vídeo
        publicador.bloquear(self.conn, "instagram", "a rede pediu verificação", "bloqueio", None)
        with patch.object(api, "criar", return_value=SimpleNamespace(pronta=lambda: (True, "ok"))):
            e = self.erro(api.postar_imagem_corte, str(self.corte), corpo={"rede": "instagram", "tipo": "momento"})
        self.assertEqual(e.status, 409)
        publicador.desbloquear(self.conn, "instagram", persistente=True)
        with patch.object(api, "criar", return_value=SimpleNamespace(pronta=lambda: (True, "ok"))), \
                publicador.TRAVAS_REDE["instagram"]:
            e = self.erro(api.postar_imagem_corte, str(self.corte), corpo={"rede": "instagram", "tipo": "momento"})
        self.assertEqual(e.status, 409)
        # sem o roteiro de imagem: 400 com o motivo
        gravador.arquivo_roteiro(self.cfg, "instagram-imagem").unlink()
        e = self.erro(api.postar_imagem_corte, str(self.corte), corpo={"rede": "instagram", "tipo": "momento"})
        self.assertEqual(e.status, 400)
        self.assertIn("falta gravar o roteiro de imagem", str(e))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM postagens").fetchone()[0], 0)

    def test_postar_imagem_sem_video_e_tipo_que_ja_saiu(self):  # CA-24
        self.cfg["geral"]["simulacao"] = True
        pode = api.detalhe_corte(self.ctx(), str(self.corte))["pode_imagem"]
        self.assertEqual(pode["redes"], ["instagram"])
        self.assertIsNone(pode["motivo"])
        self.assertEqual(pode["por_rede"]["instagram"], {"ja_saiu": [], "com_chamada": False})
        with self.sincrono():
            self.assertEqual(self.postar("momento"), {"ok": True, "simulacao": True})
        self.assertEqual(_PublicadorFalso.chamadas, [("instagram", self.corte, "momento", True)])
        # com o vídeo e a imagem já na rede: a chamada vai junto, e o mesmo tipo é aceito (só a janela avisa)
        self.postagem(self.corte, "instagram", "publicado")
        self.postagem(self.corte, "instagram", "publicado", "imagem", "momento")
        pode = api.detalhe_corte(self.ctx(), str(self.corte))["pode_imagem"]
        self.assertEqual(pode["por_rede"]["instagram"], {"ja_saiu": ["momento"], "com_chamada": True})
        with self.sincrono():
            self.postar("momento")
        self.assertEqual(len(_PublicadorFalso.chamadas), 2)
        # o candidato não oferece rede nenhuma
        candidato = self.novo_corte(status="candidato", parte=None, inicio=300.0, fim=340.0)
        pode = api.detalhe_corte(self.ctx(), str(candidato))["pode_imagem"]
        self.assertEqual((pode["redes"], pode["motivo"]), ([], "Este corte ainda não foi editado"))

    def test_detalhe_lista_e_inicio(self):
        pasta = imagens.pasta_imagens(self.cfg, self.corte)
        pasta.mkdir(parents=True)
        arquivos = [pasta / f"carrossel-4x5-{n}.jpg" for n in (1, 2, 3)]
        for a in arquivos:
            a.write_bytes(b"jpg")
        linha = _linha_de_imagem(self.conn, self.corte_row(self.corte), "carrossel", arquivos)
        p = self.postagem(self.corte, "instagram", "conferir", "imagem", "carrossel")
        self.conn.execute("UPDATE postagens SET imagem_id=? WHERE id=?", (linha["id"], p))
        self.postagem(self.corte, "instagram", "publicado", "imagem", "momento")
        d = api.detalhe_corte(self.ctx(), str(self.corte))
        self.assertEqual([(g["tipo"], len(g["urls"])) for g in d["imagens"]], [("carrossel", 3)])
        self.assertTrue(d["imagens"][0]["urls"][1].startswith(f"/media/imagem/{linha['id']}/2.jpg"))
        self.assertEqual(d["imagens_por_rede"]["instagram"]["carrossel"]["estado"], "conferir")
        self.assertEqual({h["midia"] for h in d["historico"]}, {"imagem"})
        lista = api.listar_cortes(self.ctx(consulta={"aba": "fila"}))
        self.assertEqual(lista["itens"][0]["imagens_publicadas"], 1)
        conferir = api._a_conferir(self.conn)
        self.assertEqual((conferir[0]["midia"], conferir[0]["imagem_tipo"]), ("imagem", "carrossel"))
        self.assertTrue(conferir[0]["miniatura"].startswith(f"/media/imagem/{linha['id']}/1.jpg"))
        ultimas = api._ultimas_postagens(self.conn, 8)
        self.assertEqual([u["midia"] for u in ultimas], ["imagem", "imagem"])

    def test_midia_da_imagem_so_de_dentro_da_pasta(self):
        pasta = imagens.pasta_imagens(self.cfg, self.corte)
        pasta.mkdir(parents=True)
        dentro = pasta / "momento-4x5-1.jpg"
        dentro.write_bytes(b"jpg")
        fora = self.raiz / "fora.jpg"
        fora.write_bytes(b"jpg")
        linha = _linha_de_imagem(self.conn, self.corte_row(self.corte), "momento", [dentro, fora])
        tratador = _Tratador()
        api.midia(self.painel, f"imagem/{linha['id']}/1.jpg", tratador)
        self.assertEqual(tratador.enviados, [(dentro, "image/jpeg")])
        for caminho in (f"imagem/{linha['id']}/2.jpg", f"imagem/{linha['id']}/3.jpg", f"imagem/{linha['id']}/0.jpg",
                        "imagem/999/1.jpg", "previa-imagem/momento-4x5-1.jpg"):
            with self.assertRaises(ErroHttp) as erro:
                api.midia(self.painel, caminho, tratador)
            self.assertEqual(erro.exception.status, 404, caminho)
        previa = self.cfg.pasta_previa / "imagem-momento-4x5-1.jpg"
        previa.parent.mkdir(parents=True, exist_ok=True)
        previa.write_bytes(b"jpg")
        api.midia(self.painel, "previa-imagem/momento-4x5-1.jpg", tratador)
        self.assertEqual(tratador.enviados[-1], (previa, "image/jpeg"))

    def test_previa(self):
        e = self.erro(api.previa_imagem, corpo={"tipo": "outro", "rede": "instagram"})
        self.assertEqual(e.status, 400)
        e = self.erro(api.previa_imagem, corpo={"tipo": "momento", "rede": "youtube"})
        self.assertEqual(e.status, 400)
        presa = SimpleNamespace(acquire=lambda timeout=None: False, release=lambda: None, locked=lambda: True)
        with patch.object(imagens, "TRAVA", presa):
            e = self.erro(api.previa_imagem, corpo={"tipo": "momento", "rede": "instagram"})
        self.assertEqual((e.status, str(e)), (409, "o AutoCortes está gerando imagens agora"))
        vistos = []

        def previa_falsa(cfg, conn, tipo, rede, destino, parar=None):
            vistos.append((cfg["imagens"]["topo"], tipo, rede, imagens.TRAVA.locked()))
            return [destino / f"imagem-{tipo}-9x16-{n}.jpg" for n in (1, 2, 3, 4)], False

        with patch.object(imagens, "previa", side_effect=previa_falsa):
            r = api.previa_imagem(self.ctx({"tipo": "carrossel", "rede": "tiktok",
                                            "alteracoes": {"imagens": {"topo": "Outro topo"}}}))
        self.assertFalse(r["real"])
        self.assertEqual([u.split("?")[0] for u in r["urls"]],
                         [f"/media/previa-imagem/carrossel-9x16-{n}.jpg" for n in (1, 2, 3, 4)])
        self.assertEqual(vistos, [("Outro topo", "carrossel", "tiktok", True)])
        self.assertFalse(imagens.TRAVA.locked())
        self.assertNotEqual(self.cfg["imagens"]["topo"], "Outro topo")

    def test_ocupado_com_a_geracao(self):
        painel = SimpleNamespace(gravacoes={}, logins={}, conta_canais=None)
        self.assertFalse(api.ocupado(painel))
        with imagens.TRAVA:
            self.assertTrue(api.ocupado(painel))


if __name__ == "__main__":
    unittest.main()
