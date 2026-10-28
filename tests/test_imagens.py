"""Posts de imagem por rede: config, banco, agenda e as consultas de vídeo que não podem contar imagem.

Tudo roda numa pasta temporária com um config.toml mínimo escrito à mão (a pasta_dados fica dentro
dela). Nada toca em dados\\, perfis\\ nem no config.toml da instalação.
"""

import copy
import sqlite3
import tempfile
import threading
import time
import tomllib
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from autocortes import agenda, config, db, planejador, publicador
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


if __name__ == "__main__":
    unittest.main()
