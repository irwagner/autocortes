"""Posts de imagem por rede: config, banco, agenda e as consultas de vídeo que não podem contar imagem.

Tudo roda numa pasta temporária com um config.toml mínimo escrito à mão (a pasta_dados fica dentro
dela). Nada toca em dados\\, perfis\\ nem no config.toml da instalação.
"""

import copy
import sqlite3
import tempfile
import tomllib
import unittest
from pathlib import Path

from autocortes import config, db
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
