"""Página do Facebook como rede própria: config, migração, agenda, plataformas, fila, banco e a
conferência da Página no navegador.

Nada aqui abre o Facebook de verdade: a API oficial, o Upload-Post e o próprio Facebook (para o
navegador sem janela) são servidores HTTP locais.
"""

import copy
import http.server
import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests

from autocortes import agenda, config, db, gravador, navegador, planejador, publicador
from autocortes.plataformas import facebook as plat_fb
from autocortes.plataformas import navegador as plat_nav
from autocortes.plataformas import manual, pagina_facebook
from autocortes.plataformas.base import Entrega, ErroPublicacao, Resultado
from autocortes.plataformas.facebook import Facebook
from autocortes.plataformas.upload_post import ViaUploadPost
from autocortes.segredos import ler_token, salvar_token
from autocortes.textos import Conteudo
from tests import test_imagens as ti


def _minutos(h: str) -> int:
    hh, mm = h.split(":")
    return int(hh) * 60 + int(mm)


class TestHorariosPadrao(unittest.TestCase):
    """Critério 7a e R6: os horários do Facebook nos presets."""

    VIZINHOS = ("youtube", "tiktok", "instagram", "bilibili")

    def test_longe_das_redes_do_navegador_e_entre_si(self):
        for nome, preset in agenda.PRESETS.items():
            fb = preset["facebook"]
            self.assertTrue(fb, nome)
            outros = [(rede, h) for rede in self.VIZINHOS for h in preset[rede]]
            outros += [(f"{rede} (imagem)", h) for rede, lista in preset.get("imagens", {}).items() for h in lista]
            for h in fb:
                for rede, outro in outros:
                    distancia = abs(_minutos(h) - _minutos(outro))
                    self.assertGreaterEqual(distancia, 45, f"{nome}: Facebook {h} a {distancia} min de {rede} {outro}")
            for i, a in enumerate(fb):
                for b in fb[i + 1:]:
                    self.assertGreaterEqual(abs(_minutos(a) - _minutos(b)), 75, f"{nome}: Facebook {a} e {b}")

    def test_padrao_do_config_e_o_recomendado(self):
        self.assertEqual(config.PADRAO["facebook"]["horarios"], agenda.PRESETS["recomendado"]["facebook"])

    def test_variacao_so_atrasa(self):
        """R6: com 45 min nominais, a variação de 10 min deixa pelo menos 35 min."""
        cfg = config.Config(copy.deepcopy(config.PADRAO), Path("config.toml"))
        cfg["agenda"]["variacao_minutos"] = 10
        from datetime import date, datetime, time as hora

        dia = date(2026, 11, 2)
        for rede in ("facebook", "tiktok", "youtube"):
            for real, chave in agenda.horarios_com_chave(cfg, rede, dia):
                nominal = datetime.combine(dia, hora(*map(int, chave.split(" ")[1].split(":"))))
                self.assertGreaterEqual(real, nominal)
                self.assertLessEqual((real - nominal).total_seconds(), 600)


class TestConfigNovo(ti.Base):
    """Critério 1 e a validação nova do [facebook]."""

    def test_sem_secao_fica_desligado_no_navegador(self):
        self.assertIs(self.cfg["facebook"]["ativo"], False)
        self.assertEqual(self.cfg["facebook"]["envio"], "navegador")
        self.assertIn("facebook", config.PLATAFORMAS)
        self.assertIn(("facebook", "app_secret"), config.SEGREDOS)
        self.assertIn("facebook", config.IMAGEM_INDISPONIVEL)

    def test_exemplo_igual_ao_padrao_na_secao(self):
        exemplo = config.tomllib.loads((config.RAIZ / "config.example.toml").read_text(encoding="utf-8"))
        self.assertEqual(exemplo["facebook"], config.PADRAO["facebook"])

    def test_validacao(self):
        casos = {
            'max_segundos = 120': "max_segundos",
            'versao_api = "26"': "versao_api",
            'pagina = "https://www.instagram.com/x"': "link da Página",
            'pagina_id = "12a"': "pagina_id",
            'app_id = "x"': "app_id",
            'pagina = "https://www.facebook.com/profile.php?id=111"\npagina_id = "222"': "tem o id 111",
            'imagens = true': "imagens não vale",
        }
        for linha, trecho in casos.items():
            with self.subTest(linha=linha):
                erros = self.recusado(f"[facebook]\n{linha}\n")
                self.assertTrue(any(trecho in e for e in erros), erros)
        for link in ("facebook.com/me", "https://www.facebook.com/me/", "https://www.facebook.com/home.php"):
            with self.subTest(link=link):
                erros = self.recusado(f'[facebook]\npagina = "{link}"\n')
                self.assertTrue(any("use o link da Página (facebook.com/SuaPagina ou profile.php?id=...), não "
                                    "facebook.com/" in e for e in erros), erros)
        cfg = self.carregar('[facebook]\npagina = "https://www.facebook.com/people/Cortes/111/"\npagina_id = "111"\n')
        self.assertEqual(config.id_do_link_facebook(cfg["facebook"]["pagina"]), "111")
        self.carregar('[facebook]\npagina = "Cortes de Filmes"\n')


LEGADO = ('[instagram]\nenvio = "{envio}"\npagina_facebook = {ligada}\nfacebook_pagina_id = "{pid}"\n'
          'horarios = ["09:00"]\ndias = ["seg"]\nversao_api = "v25.0"\n')


class TestConfigMigracao(ti.Base):
    """Critérios 1 a 5, 15d, R8 e a coerência link × pagina_id."""

    def escrever(self, texto: str) -> bytes:
        self.caminho.write_text(ti.CONFIG_MINIMO + texto, encoding="utf-8")
        return self.caminho.read_bytes()

    def carregar_registrando(self):
        with self.assertLogs("autocortes", level="INFO") as registro:
            cfg = config.carregar(self.caminho)
        return cfg, "\n".join(registro.output)

    def test_oficial_migra_sem_mexer_no_arquivo(self):
        antes = self.escrever(LEGADO.format(envio="oficial", ligada="true", pid=""))
        cfg, registro = self.carregar_registrando()
        fb = cfg["facebook"]
        self.assertEqual((fb["ativo"], fb["envio"], fb["horarios"], fb["dias"]), (True, "oficial", ["09:00"], ["seg"]))
        self.assertEqual(fb["versao_api"], "v25.0")  # R8
        self.assertTrue(cfg.migrado_facebook)
        self.assertNotIn("Opção desconhecida", registro)
        self.assertIn("agora é a rede Facebook", registro)
        self.assertEqual(self.caminho.read_bytes(), antes)
        self.assertTrue(cfg.copia().migrado_facebook)

    def test_upload_post_leva_o_id(self):
        self.escrever(LEGADO.format(envio="upload_post", ligada="true", pid="123"))
        fb = config.carregar(self.caminho)["facebook"]
        self.assertEqual((fb["envio"], fb["pagina_id"]), ("upload_post", "123"))
        self.assertEqual(fb["versao_api"], config.PADRAO["facebook"]["versao_api"])

    def test_instagram_sem_videos_migra_desligado(self):
        self.escrever(LEGADO.format(envio="oficial", ligada="true", pid="") + "videos = false\nimagens = true\n")
        self.assertIs(config.carregar(self.caminho)["facebook"]["ativo"], False)

    def test_sem_o_que_migrar(self):
        casos = {
            "desligada": LEGADO.format(envio="oficial", ligada="false", pid="9"),
            "ausente": '[instagram]\nenvio = "oficial"\n',
            "navegador": LEGADO.format(envio="navegador", ligada="true", pid=""),
            "manual": LEGADO.format(envio="manual", ligada="true", pid=""),
            "já tem [facebook]": LEGADO.format(envio="oficial", ligada="true", pid="") + '[facebook]\nativo = false\n',
        }
        for nome, texto in casos.items():
            with self.subTest(nome):
                antes = self.escrever(texto)
                with self.assertNoLogs("autocortes", level="WARNING"):
                    cfg = config.carregar(self.caminho)
                self.assertEqual(cfg["facebook"], config.PADRAO["facebook"])
                self.assertFalse(cfg.migrado_facebook)
                self.assertEqual(self.caminho.read_bytes(), antes)

    def test_primeira_gravacao_grava_a_secao_inteira(self):
        for alteracao in ({"agenda": {"maximo_por_dia": 5}}, {"facebook": {"ativo": False}}):
            with self.subTest(alteracao=alteracao):
                self.escrever(LEGADO.format(envio="upload_post", ligada="true", pid="123"))
                cfg = config.carregar(self.caminho)
                novo = config.salvar(cfg, alteracao)
                self.assertFalse(novo.migrado_facebook)
                texto = self.caminho.read_text(encoding="utf-8")
                self.assertNotIn("pagina_facebook", texto)
                self.assertNotIn("facebook_pagina_id", texto)
                gravado = config.tomllib.loads(texto)["facebook"]
                self.assertEqual(set(gravado), set(config.PADRAO["facebook"]))
                with self.assertNoLogs("autocortes", level="WARNING"):
                    de_novo = config.carregar(self.caminho)
                fb = de_novo["facebook"]
                self.assertFalse(de_novo.migrado_facebook)
                self.assertEqual((fb["envio"], fb["horarios"], fb["dias"], fb["pagina_id"]),
                                 ("upload_post", ["09:00"], ["seg"], "123"))
                self.assertIs(fb["ativo"], "facebook" not in alteracao)

    def test_nada_mudou_nao_grava(self):
        antes = self.escrever(LEGADO.format(envio="oficial", ligada="true", pid=""))
        cfg = config.carregar(self.caminho)
        self.assertTrue(config.salvar(cfg, {}).migrado_facebook)
        self.assertEqual(self.caminho.read_bytes(), antes)

    def test_pagina_trocada(self):
        """15d nos quatro envios."""
        for envio in ("oficial", "upload_post"):
            with self.subTest(envio=envio):
                self.escrever(f'[facebook]\nenvio = "{envio}"\npagina = "Cortes"\npagina_id = "111"\n'
                              'pagina_nome = "Cortes"\n')
                cfg = config.carregar(self.caminho)
                with self.assertRaises(config.ErroConfig) as erro:
                    config.salvar(cfg, {"facebook": {"pagina": "Outra"}})
                self.assertIn("escolha a Página na lista", str(erro.exception))
                novo = config.salvar(cfg, {"facebook": {"pagina": "Outra", "pagina_id": "222", "pagina_nome": "Outra"}})
                self.assertEqual(novo["facebook"]["pagina_id"], "222")
                trocado = config.salvar(novo, {"facebook": {"envio": "manual" if envio == "oficial" else "navegador"}})
                self.assertEqual(trocado["facebook"]["pagina_id"], "222")  # trocar só o envio não mexe na Página
        for envio in ("navegador", "manual"):
            with self.subTest(envio=envio):
                self.escrever(f'[facebook]\nenvio = "{envio}"\npagina = "Cortes"\npagina_id = "111"\n'
                              'pagina_nome = "Cortes"\n')
                cfg = config.carregar(self.caminho)
                novo = config.salvar(cfg, {"facebook": {"pagina": "Outra"}})
                self.assertEqual((novo["facebook"]["pagina_id"], novo["facebook"]["pagina_nome"]), ("", ""))
                gravado = config.carregar(self.caminho)["facebook"]
                self.assertEqual((gravado["pagina"], gravado["pagina_id"], gravado["pagina_nome"]), ("Outra", "", ""))
                novo = config.salvar(novo, {"facebook": {"pagina": "https://www.facebook.com/profile.php?id=555"}})
                self.assertEqual(novo["facebook"]["pagina_id"], "555")
                # a mesma Página de novo não apaga o que foi confirmado
                novo = config.salvar(novo, {"facebook": {"pagina_nome": "Cortes 555"}})
                novo = config.salvar(novo, {"facebook": {"pagina": "https://www.facebook.com/profile.php?id=555"}})
                self.assertEqual(novo["facebook"]["pagina_nome"], "Cortes 555")

    def test_link_e_id_que_discordam(self):
        self.escrever('[facebook]\npagina = "https://www.facebook.com/profile.php?id=111"\n')
        cfg = config.carregar(self.caminho)
        with self.assertRaises(config.ErroConfig) as erro:
            config.salvar(cfg, {"facebook": {"pagina_id": "222"}})
        self.assertIn("tem o id 111, mas pagina_id é 222", str(erro.exception))


class TestManual(ti.Base):
    """Critério 18: a tarefa à mão do Facebook cita a Página e lembra de conferir o perfil ativo."""

    def conteudo(self) -> Conteudo:
        return Conteudo(titulo="Trecho do filme", descricao="Uma descrição #cinema #filme", hashtags=["#cinema"])

    def test_pacote_cita_a_pagina_e_o_perfil(self):
        self.cfg["facebook"].update(envio="manual", pagina="https://www.facebook.com/CortesDeFilmes",
                                    pagina_nome="Cortes de Filmes")
        dados = manual.pacote(self.cfg, "facebook", self.conteudo())
        passos = " ".join(dados["passos"])
        self.assertIn("Cortes de Filmes", passos)
        self.assertIn("perfil pessoal", passos)
        self.assertIn("Ver todos os perfis", passos)
        self.assertEqual(dados["site"], {"url": "https://www.facebook.com/CortesDeFilmes", "rotulo": "Abrir a Página"})
        rotulos = [c["rotulo"] for c in dados["campos"]]
        self.assertEqual(rotulos, ["Título", "Descrição"])
        self.assertIn("Facebook", manual.texto_do_pacote("facebook", dados))

    def test_site_pelo_id_e_reserva(self):
        self.cfg["facebook"].update(pagina="Cortes", pagina_id="123")
        self.assertEqual(manual.site_da_rede(self.cfg, "facebook")["url"],
                         "https://www.facebook.com/profile.php?id=123")
        self.cfg["facebook"].update(pagina="", pagina_id="")
        self.assertEqual(manual.site_da_rede(self.cfg, "facebook")["url"], "https://www.facebook.com/")
        passos = manual.pacote(self.cfg, "facebook", self.conteudo())["passos"]
        self.assertIn("configurada em Redes sociais > Facebook", passos[0])

    def test_textos_nos_limites(self):
        longo = Conteudo(titulo="T" * 400, descricao="d" * 3000)
        t = manual.textos(self.cfg, "facebook", longo)
        self.assertLessEqual(len(t["titulo"]), 255)
        self.assertLessEqual(len(t["descricao"]), 2200)


# ---------------------------------------------------------------- servidores simulados

class _Servidor:
    """ThreadingHTTPServer em 127.0.0.1 que responde por uma função (metodo, caminho, consulta, corpo)."""

    def __init__(self, responder):
        self.pedidos: list[dict] = []
        dono = self

        class Tratador(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _tratar(self, metodo):
                partes = urlsplit(self.path)
                tamanho = int(self.headers.get("Content-Length") or 0)
                corpo = self.rfile.read(tamanho) if tamanho else b""
                consulta = {k: v[0] for k, v in parse_qs(partes.query).items()}
                if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
                    consulta.update({k: v[0] for k, v in parse_qs(corpo.decode()).items()})
                pedido = {"metodo": metodo, "caminho": partes.path, "consulta": consulta, "corpo": corpo,
                          "cabecalhos": dict(self.headers)}
                dono.pedidos.append(pedido)
                status, dados = responder(pedido)
                saida = json.dumps(dados).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(saida)))
                self.end_headers()
                self.wfile.write(saida)

            def do_GET(self):
                self._tratar("GET")

            def do_POST(self):
                self._tratar("POST")

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Tratador)
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def fechar(self):
        self.http.shutdown()
        self.http.server_close()


PAGINAS_META = [
    {"id": "111", "name": "Cortes", "access_token": "TOKEN-PAGINA-111", "tasks": ["CREATE_CONTENT", "MANAGE"]},
    {"id": "222", "name": "Só leitura", "access_token": "TOKEN-222", "tasks": ["ANALYZE"]},
    {"id": "333", "name": "Outra", "access_token": "TOKEN-PAGINA-333"},
]


def _graph(pedido):
    caminho, q = pedido["caminho"], pedido["consulta"]
    if caminho.startswith("/rupload/"):
        return 200, {"success": True}
    caminho = caminho.split("/", 2)[2] if caminho.count("/") >= 2 else caminho
    if caminho == "oauth/access_token":
        return 200, {"access_token": "TOKEN-LONGO"}
    if caminho == "me/accounts":
        return 200, {"data": PAGINAS_META}
    if caminho == "debug_token":
        return 200, {"data": {"scopes": ["pages_show_list", "pages_manage_posts"]}}
    if caminho.endswith("/video_reels"):
        if q.get("upload_phase") == "start":
            return 200, {"video_id": "999"}
        return 200, {"success": True}
    if caminho == "999":
        if q.get("fields") == "status":
            return 200, {"status": {"video_status": "ready", "publishing_phase": {"status": "complete"}}}
        return 200, {"permalink_url": "/reel/999"}
    if caminho in ("111", "333", "444"):
        return 200, {"name": {"111": "Cortes", "333": "Outra", "444": "Página do Instagram"}[caminho]}
    return 404, {"error": {"message": "não existe", "code": 803}}


class TestApiOficialSimulada(ti.Base):
    """Critério 16 (parte da plataforma) e R2: o login herdado do Instagram não é copiado."""

    def setUp(self):
        super().setUp()
        self.srv = _Servidor(_graph)
        self.addCleanup(self.srv.fechar)
        self.cfg["facebook"].update(ativo=True, envio="oficial", app_id="123", app_secret="segredo")
        self.cfg.pasta_tokens.mkdir(parents=True, exist_ok=True)
        espera = patch.object(plat_fb, "esperar", lambda parar, s: None)
        espera.start()
        self.addCleanup(espera.stop)

    def plataforma(self) -> Facebook:
        fb = Facebook(self.cfg)
        fb.graph_url = self.srv.url
        fb.rupload_facebook_url = self.srv.url + "/rupload"
        return fb

    def arquivo(self) -> Path:
        video = self.raiz / "corte.mp4"
        video.write_bytes(b"\x00" * 2048)
        return video

    def test_conectar_escolher_e_enviar(self):
        fb = self.plataforma()
        self.assertEqual(fb.pronta(), (False, "falta conectar a Página"))
        paginas = fb.trocar_token("TOKEN-CURTO")
        self.assertEqual([p["id"] for p in paginas], ["111", "333"])  # a só de leitura fica de fora
        texto = fb.salvar_pagina(paginas[0])
        self.assertEqual(texto, "Página Cortes")
        guardado = ler_token(self.cfg.pasta_tokens / "facebook.json")
        self.assertEqual(guardado["pagina_id"], "111")
        self.assertEqual(guardado["access_token"], "TOKEN-PAGINA-111")
        self.assertEqual(fb.conta(), "Página Cortes")
        self.assertEqual(fb.pronta(), (True, "ok"))

        entrega = Entrega()
        res = fb.publicar(self.arquivo(), Conteudo(titulo="Título", descricao="Descrição #a"), entrega=entrega)
        self.assertEqual(res.id_remoto, "999")
        self.assertEqual(res.url, "https://www.facebook.com/reel/999")
        self.assertEqual(entrega.id_remoto, "999")
        fases = [(p["caminho"], p["consulta"].get("upload_phase")) for p in self.srv.pedidos
                 if p["metodo"] == "POST"]
        self.assertEqual(fases, [("/v26.0/111/video_reels", "start"), ("/rupload/v26.0/999", None),
                                 ("/v26.0/111/video_reels", "finish")])
        fim = [p for p in self.srv.pedidos if p["consulta"].get("upload_phase") == "finish"][0]["consulta"]
        self.assertEqual(fim["video_state"], "PUBLISHED")
        self.assertEqual(fim["title"], "Título")
        self.assertEqual(fim["access_token"], "TOKEN-PAGINA-111")
        envio = [p for p in self.srv.pedidos if p["caminho"].startswith("/rupload/")][0]
        self.assertEqual(envio["cabecalhos"]["Authorization"], "OAuth TOKEN-PAGINA-111")

    def test_pagina_do_config_diferente_do_login(self):
        fb = self.plataforma()
        fb.salvar_pagina(PAGINAS_META[2])
        self.cfg["facebook"].update(pagina_id="111", pagina_nome="Cortes")
        pronta, motivo = fb.pronta()
        self.assertFalse(pronta)
        self.assertIn("Página Outra", motivo)
        self.assertIn("escolha Cortes", motivo)

    def test_sem_permissao_de_postar_avisa(self):
        def sem_escopo(pedido):
            if pedido["caminho"].endswith("debug_token"):
                return 200, {"data": {"scopes": ["pages_show_list"]}}
            return _graph(pedido)

        self.srv.fechar()
        self.srv = _Servidor(sem_escopo)
        self.addCleanup(self.srv.fechar)
        self.assertIn("falta a permissão pages_manage_posts", self.plataforma().salvar_pagina(PAGINAS_META[0]))

    def test_credenciais_do_instagram_valem(self):
        self.cfg["facebook"].update(app_id="", app_secret="")
        self.cfg["instagram"].update(app_id="456", app_secret="s2")
        self.plataforma().trocar_token("TOKEN-CURTO")
        troca = [p for p in self.srv.pedidos if p["caminho"].endswith("oauth/access_token")][0]
        self.assertEqual(troca["consulta"]["client_id"], "456")
        self.cfg["instagram"].update(app_id="", app_secret="")
        with self.assertRaises(ErroPublicacao) as erro:
            self.plataforma().trocar_token("x")
        self.assertEqual(erro.exception.tipo, "bloqueio")

    def herdar_do_instagram(self):
        salvar_token(self.cfg.pasta_tokens / "instagram.json",
                     {"access_token": "TOKEN-IG", "ig_user_id": "9", "pagina_id": "444",
                      "pagina_nome": "Página do Instagram", "conta": "@conta_ig"})

    def test_login_do_instagram_herdado(self):
        self.herdar_do_instagram()
        fb = self.plataforma()
        self.assertEqual(fb.pronta(), (True, "ok"))
        self.assertEqual(fb.conta(), "Página Página do Instagram (login do Instagram)")
        fb.publicar(self.arquivo(), Conteudo(titulo="T", descricao="D"))
        inicio = [p for p in self.srv.pedidos if p["consulta"].get("upload_phase") == "start"][0]
        self.assertEqual(inicio["caminho"], "/v26.0/444/video_reels")
        self.assertEqual(inicio["consulta"]["access_token"], "TOKEN-IG")

    def test_testar_conexao_herdado_nao_copia_o_login(self):
        """R2: verificar + lembrar_conta (o Testar conexão do painel) não cria tokens/facebook.json."""
        self.herdar_do_instagram()
        fb = self.plataforma()
        texto = fb.verificar()
        self.assertEqual(texto, "Página Página do Instagram (login do Instagram)")
        fb.lembrar_conta(texto)
        self.assertFalse((self.cfg.pasta_tokens / "facebook.json").exists())
        self.assertEqual(fb.conta(), "Página Página do Instagram (login do Instagram)")

    def test_desconectar_herdado_desconecta(self):
        """R2: o Desconectar no caso herdado deixa o pronta() falso; conectar pela lista volta a valer."""
        self.herdar_do_instagram()
        fb = self.plataforma()
        fb.desconectar()
        self.assertEqual(fb.token(), {})
        self.assertEqual(fb.pronta(), (False, "falta conectar a Página"))
        self.assertIsNone(fb.conta())
        self.assertTrue(ler_token(self.cfg.pasta_tokens / "instagram.json").get("access_token"))
        fb.salvar_pagina(PAGINAS_META[0])
        self.assertEqual(fb.token()["pagina_id"], "111")
        self.assertEqual(fb.pronta(), (True, "ok"))

    def test_criar_escolhe_a_api_oficial(self):
        from autocortes.plataformas import criar

        self.assertIsInstance(criar("facebook", self.cfg), Facebook)


class TestUploadPostSimulado(ti.Base):
    """Critério 17 (parte da plataforma): campos do envio, available_pages e o verificar."""

    def setUp(self):
        super().setUp()
        self.paginas = [{"id": "111", "name": "Cortes"}, {"id": "333", "name": "Outra"}]
        self.resposta_envio = (200, {"success": True, "request_id": "r1",
                                     "results": {"facebook": {"success": True, "post_id": "77",
                                                              "url": "https://www.facebook.com/reel/77"}}})
        self.srv = _Servidor(self.responder)
        self.addCleanup(self.srv.fechar)
        self.cfg["upload_post"].update(api_key="CHAVE-UP", perfil="meuperfil")
        self.cfg["facebook"].update(ativo=True, envio="upload_post")
        self.cfg.pasta_tokens.mkdir(parents=True, exist_ok=True)

    def responder(self, pedido):
        caminho = pedido["caminho"]
        if caminho == "/api/uploadposts/me":
            return 200, {"plan": "basic"}
        if caminho == "/api/uploadposts/users/meuperfil":
            return 200, {"profile": {"social_accounts": {"facebook": {"display_name": "Eu"}}}}
        if caminho == "/api/uploadposts/facebook/pages":
            self.assertEqual(pedido["consulta"].get("profile"), "meuperfil")
            return 200, {"success": True, "pages": self.paginas}
        if caminho == "/api/upload":
            return self.resposta_envio
        return 404, {"message": "não existe"}

    def plataforma(self) -> ViaUploadPost:
        up = ViaUploadPost("facebook", self.cfg)
        up.api = self.srv.url
        return up

    def enviar(self):
        video = self.raiz / "corte.mp4"
        video.write_bytes(b"\x00" * 1024)
        return self.plataforma().publicar(video, Conteudo(titulo="Título", descricao="Descrição"))

    def campos_enviados(self) -> str:
        return [p for p in self.srv.pedidos if p["caminho"] == "/api/upload"][-1]["corpo"].decode("utf-8", "replace")

    def test_campos_do_envio(self):
        self.cfg["facebook"]["pagina_id"] = "111"
        res = self.enviar()
        self.assertEqual(res.url, "https://www.facebook.com/reel/77")
        corpo = self.campos_enviados()
        for nome, valor in (("platform[]", "facebook"), ("facebook_media_type", "REELS"),
                            ("facebook_page_id", "111"), ("facebook_title", "Título"),
                            ("facebook_description", "Descrição")):
            self.assertIn(f'name="{nome}"\r\n\r\n{valor}\r\n', corpo)
        self.assertNotIn("instagram_title", corpo)

    def test_sem_pagina_id_nao_manda_o_campo(self):
        self.enviar()
        self.assertNotIn("facebook_page_id", self.campos_enviados())

    def test_available_pages_vira_bloqueio(self):
        self.resposta_envio = (400, {"success": False, "message": "Multiple pages", "available_pages": self.paginas})
        with self.assertRaises(ErroPublicacao) as erro:
            self.enviar()
        self.assertEqual(erro.exception.tipo, "bloqueio")
        self.assertIn("escolha a Página", str(erro.exception))
        self.assertIn("Redes sociais > Facebook", str(erro.exception))

    def test_paginas_facebook(self):
        self.paginas.append({"id": "abc", "name": "Inválida"})
        self.assertEqual(self.plataforma().paginas_facebook(),
                         [{"id": "111", "nome": "Cortes"}, {"id": "333", "nome": "Outra"}])

    def test_verificar(self):
        texto = self.plataforma().verificar()  # várias Páginas, sem pagina_id: passa com o aviso
        self.assertIn("Página fixada no perfil", texto)
        self.assertEqual(self.plataforma().conta(), texto)
        self.cfg["facebook"]["pagina_id"] = "333"
        self.assertIn("Página Outra via Upload-Post", self.plataforma().verificar())
        self.cfg["facebook"]["pagina_id"] = "555"
        with self.assertRaises(ErroPublicacao) as erro:
            self.plataforma().verificar()
        self.assertEqual(erro.exception.tipo, "bloqueio")
        self.assertIn("555", str(erro.exception))
        self.cfg["facebook"]["pagina_id"] = ""
        self.paginas[:] = [{"id": "111", "name": "Cortes"}]
        self.assertIn("Página Cortes", self.plataforma().verificar())


class _RedeFalsa:
    """Plataforma que anota o que recebeu (nada sai do PC)."""

    def __init__(self, nome: str):
        self.nome, self.rotulo, self.via = nome, ROTULOS_FALSOS[nome], "oficial"
        self.recebidos: list[str] = []

    def pronta(self):
        return True, "ok"

    def publicar(self, arquivo, conteudo, parar=None, entrega=None):
        self.recebidos.append(Path(arquivo).name)
        if entrega is not None:
            entrega.aceitou(f"id-{len(self.recebidos)}")
        return Resultado(f"id-{len(self.recebidos)}", f"https://exemplo.invalid/{len(self.recebidos)}")


ROTULOS_FALSOS = {"instagram": "Instagram (falso)", "facebook": "Facebook (falso)", "youtube": "YouTube (falso)"}


class TestBancoMigracao(ti.BaseBanco):
    """Critérios 6 e 7, R1, R9 e R11."""

    def setUp(self):
        super().setUp()
        self.so_redes("instagram", "facebook")
        self.cfg["agenda"]["variacao_minutos"] = 0
        self.cfg["agenda"]["intervalo_minimo_min"] = 0
        agora_ = datetime.now()
        self.cfg["facebook"]["horarios"] = [(agora_ - timedelta(minutes=2)).strftime("%H:%M")]
        self.cfg["instagram"]["horarios"] = [(agora_ + timedelta(hours=6)).strftime("%H:%M")]

    def publicador(self, *redes: str):
        pub = publicador.Publicador(self.cfg, threading.Event())
        falsas = {nome: _RedeFalsa(nome) for nome in redes}
        pub.plataformas = dict(falsas)
        return pub, falsas

    def reabrir(self):
        db.conectar(self.cfg.banco).close()

    def test_migracao_e_ciclo_real(self):
        """Critério 6: A, B e D não vão ao Facebook, B e D ganham 'pulado', C é tentado de novo."""
        dois_dias = time.time() - 2 * 86400
        horario_a = dois_dias + 60
        a = self.novo_corte(inicio=10, fim=40)
        b = self.novo_corte(inicio=50, fim=80)
        c = self.novo_corte(inicio=100, fim=130)
        d = self.novo_corte(inicio=150, fim=180)
        self.postagem(a, "instagram", "publicado", slot="2026-10-28 12:15", horario=horario_a, criado_em=dois_dias)
        self.postagem(a, "facebook", "publicado", horario=horario_a, criado_em=dois_dias)
        for corte in (b, c, d):
            self.postagem(corte, "instagram", "publicado", criado_em=dois_dias)
        self.postagem(c, "facebook", "falhou", horario=horario_a, criado_em=dois_dias)
        self.postagem(d, "facebook", "simulado", criado_em=dois_dias)
        db.apagar_estado(self.conn, "facebook:slots")
        self.reabrir()  # a migração do banco roda ao abrir

        slot_a = self.conn.execute("SELECT slot FROM postagens WHERE corte_id=? AND plataforma='facebook'",
                                   (a,)).fetchone()[0]
        self.assertEqual(slot_a, "2026-10-28 12:15")
        self.assertIsNotNone(db.ler_estado(self.conn, "facebook:slots"))
        # reabrir não refaz: a marca existe
        self.conn.execute("UPDATE postagens SET slot=NULL WHERE corte_id=? AND plataforma='facebook'", (a,))
        self.reabrir()
        self.assertIsNone(self.conn.execute("SELECT slot FROM postagens WHERE corte_id=? AND plataforma='facebook'",
                                            (a,)).fetchone()[0])

        pub, falsas = self.publicador("instagram", "facebook")
        pub.ciclo(self.conn)
        nomes = {corte: Path(self.corte_row(corte)["arquivo"]).name for corte in (a, b, c, d)}
        self.assertEqual(falsas["facebook"].recebidos, [nomes[c]])
        self.assertEqual(falsas["instagram"].recebidos, [])
        for corte in (b, d):
            linha = self.conn.execute("SELECT status, erro, via FROM postagens WHERE corte_id=? AND "
                                      "plataforma='facebook' AND status='pulado'", (corte,)).fetchone()
            self.assertIsNotNone(linha, corte)
            self.assertIn("repasse do Instagram", linha["erro"])
            self.assertEqual(linha["via"], "navegador")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM postagens WHERE corte_id IN (?, ?) AND "
                                            "plataforma='facebook' AND status='pulado'", (a, c)).fetchone())
        self.assertEqual(db.ler_estado(self.conn, "facebook:inicio")["pulados"], 2)
        # o corte que já saiu nas duas redes conclui; o C também, depois de publicado no Facebook
        for corte in (a, b, c, d):
            self.assertEqual(self.corte_row(corte)["status"], "concluido", corte)
        # uma vez por perfil
        e = self.novo_corte(inicio=200, fim=230)
        self.postagem(e, "instagram", "publicado")
        self.assertEqual(planejador.preparar_facebook(self.cfg, self.conn), 0)

    def test_nova_tentativa_antiga_nao_atende_horario(self):
        """R9: a nova tentativa do código antigo (sem horário, manual = 0) vira manual = 1."""
        hoje = datetime.now().replace(hour=13, minute=30, second=0, microsecond=0)
        corte = self.novo_corte()
        self.postagem(corte, "facebook", "publicado", criado_em=hoje.timestamp() + 30 * 60)
        sucesso = planejador.status_ocupam_horario(self.cfg)
        chave = hoje.strftime("%Y-%m-%d %H:%M")
        self.assertTrue(planejador.horario_atendido(self.conn, "facebook", hoje.timestamp(), sucesso, chave=chave))
        db.apagar_estado(self.conn, "facebook:slots")
        self.reabrir()
        self.assertEqual(self.conn.execute("SELECT manual FROM postagens WHERE corte_id=?", (corte,)).fetchone()[0], 1)
        self.assertFalse(planejador.horario_atendido(self.conn, "facebook", hoje.timestamp(), sucesso, chave=chave))

    def test_preparar_com_a_agenda_pausada(self):
        """R11: a marcação roda antes do teste da pausa da agenda."""
        self.cfg["agenda"]["pausado"] = True
        corte = self.novo_corte()
        self.postagem(corte, "instagram", "publicado")
        pub, falsas = self.publicador("instagram", "facebook")
        pub.ciclo(self.conn)
        self.assertEqual(falsas["facebook"].recebidos, [])
        self.assertIsNotNone(db.ler_estado(self.conn, "facebook:inicio"))
        self.assertEqual(self.corte_row(corte)["status"], "concluido")

    def test_preparar_fora_da_simulacao_e_com_o_facebook_ligado(self):
        corte = self.novo_corte()
        self.postagem(corte, "instagram", "publicado")
        self.cfg["geral"]["simulacao"] = True
        pub, _ = self.publicador("instagram", "facebook")
        pub.ciclo(self.conn)
        self.cfg["geral"]["simulacao"] = False
        pub, _ = self.publicador("instagram")
        pub.ciclo(self.conn)
        self.assertIsNone(db.ler_estado(self.conn, "facebook:inicio"))

    def test_corte_acima_do_limite_conclui(self):
        """R1: um corte de 100 s publicado nas outras redes vira concluido com o Facebook ativo."""
        self.so_redes("youtube", "facebook")
        corte = self.novo_corte(inicio=100, fim=200)
        self.postagem(corte, "youtube", "publicado")
        pub, _ = self.publicador("youtube", "facebook")
        pub.atualizar_concluidos(self.conn)
        self.assertEqual(self.corte_row(corte)["status"], "concluido")
        # sem nenhuma rede em que caiba, não conclui (não saiu em lugar nenhum)
        so_fb = self.novo_corte(inicio=300, fim=400)
        pub, _ = self.publicador("facebook")
        pub.atualizar_concluidos(self.conn)
        self.assertEqual(self.corte_row(so_fb)["status"], "pronto")

    def test_fila_e_sem_cortes_possiveis(self):
        """Critério 7."""
        curto = self.novo_corte(inicio=0, fim=90.4)
        self.novo_corte(inicio=100, fim=191)
        self.assertEqual([r["id"] for r in planejador.fila(self.cfg, self.conn, "facebook")], [curto])
        self.assertFalse(planejador.sem_cortes_possiveis(self.cfg, "facebook"))
        self.cfg["facebook"]["max_segundos"] = 20
        self.cfg["cortes"]["duracao_min"] = 30
        self.assertTrue(planejador.sem_cortes_possiveis(self.cfg, "facebook"))
        self.assertEqual(planejador.fila(self.cfg, self.conn, "facebook"), [])

    def test_postar_agora_acima_do_limite_pula(self):
        corte = self.novo_corte(inicio=0, fim=120)
        pub, falsas = self.publicador("facebook")
        self.assertEqual(pub.postar(self.conn, "facebook", self.corte_row(corte), manual=True), "pulado")
        self.assertEqual(falsas["facebook"].recebidos, [])
        erro = self.conn.execute("SELECT erro FROM postagens WHERE corte_id=?", (corte,)).fetchone()[0]
        self.assertEqual(erro, "o Reel da Página aceita até 90 s")


# ---------------------------------------------------------------- conferência da Página

C_USER = "987654321012345"  # o perfil pessoal de teste: nunca pode aparecer num texto
PAGINA = "5550001"
OUTRA = "5550002"
LINK_PAGINA = f"https://www.facebook.com/profile.php?id={PAGINA}"


class _AbaDeMentira:
    """Aba sem navegador: cookies, /me e URL prontos (para as partes puras da conferência)."""

    def __init__(self, c_user=C_USER, i_user=None, me_url=None, titulo="", url="https://exemplo.invalid/"):
        self.ck = {"c_user": c_user, "i_user": i_user}
        self.me_url, self.titulo, self.atual = me_url, titulo, url
        self.navegou: list[str] = []

    def comando(self, metodo, **params):
        assert metodo == "Network.getCookies", metodo
        return {"cookies": [{"name": k, "value": v} for k, v in self.ck.items() if v is not None]}

    def navegar(self, url, segundos=45):
        self.navegou.append(url)
        self.atual = self.me_url if url.endswith("/me") and self.me_url else url

    def url(self):
        return self.atual

    def avaliar(self, expressao, *a, **k):
        assert expressao == "document.title", expressao
        return self.titulo


class TestEntradaEDecisao(unittest.TestCase):
    """Item 6: ler_entrada, normalizar, decidir, descrever e as conferências sem navegador (e R5)."""

    def fb(self, pagina=LINK_PAGINA, pagina_id=PAGINA, pagina_nome="Cortes de Filmes") -> dict:
        return {"pagina": pagina, "pagina_id": pagina_id, "pagina_nome": pagina_nome}

    def assertSemCUser(self, *textos):
        for texto in textos:
            self.assertNotIn(C_USER, str(texto))

    def test_r5_importa_sem_o_navegador_das_redes(self):
        codigo = ("import sys, autocortes.plataformas.pagina_facebook; "
                  "print(int('autocortes.plataformas.navegador' in sys.modules))")
        saida = subprocess.run([sys.executable, "-c", codigo], cwd=str(config.RAIZ), capture_output=True,
                               text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(saida.returncode, 0, saida.stderr)
        self.assertEqual(saida.stdout.strip(), "0")

    def test_ler_entrada(self):
        vazio = {"id": None, "caminho": None, "nome": None}
        casos = {
            PAGINA: dict(vazio, id=PAGINA),
            LINK_PAGINA: dict(vazio, id=PAGINA),
            f"https://web.facebook.com/people/Cortes-de-Filmes/{PAGINA}/": dict(vazio, id=PAGINA),
            "https://www.facebook.com/CortesDeFilmes/?ref=bookmarks": dict(vazio, caminho="/cortesdefilmes"),
            "facebook.com/CortesDeFilmes/": dict(vazio, caminho="/cortesdefilmes"),
            "  Página   da  Ação ": dict(vazio, nome="página da ação"),
            "": vazio,
            "https://www.facebook.com/": vazio,
            # o /me e as páginas do site não apontam uma Página (o /me bateria com qualquer uma em uso)
            "facebook.com/me": vazio,
            "https://www.facebook.com/me/": vazio,
            "https://www.facebook.com/home.php": vazio,
            "https://www.facebook.com/profile.php": vazio,
        }
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(pagina_facebook.ler_entrada(texto), esperado)

    def test_normalizar(self):
        self.assertEqual(pagina_facebook.normalizar("(3) Cortes  de\tFilmes | Facebook"), "cortes de filmes")
        self.assertEqual(pagina_facebook.normalizar(unicodedata.normalize("NFD", "Página Ação")),
                         pagina_facebook.normalizar("PÁGINA AÇÃO"))

    def test_decidir_com_pagina_id(self):
        fb = self.fb()
        certo = pagina_facebook.decidir({"c_user": C_USER, "i_user": PAGINA},
                                        {"id": PAGINA, "caminho": "/profile.php",
                                         "titulo": "(2) Cortes de Filmes | Facebook"}, fb)
        self.assertTrue(certo.confere)
        self.assertEqual((certo.agindo_como, certo.id, certo.nome), ("pagina", PAGINA, "Cortes de Filmes"))
        self.assertIsNone(certo.aprendido)
        # só pelo título (o /me foi para o link curto da Página)
        pelo_titulo = pagina_facebook.decidir({"c_user": C_USER, "i_user": PAGINA},
                                              {"id": None, "caminho": "/cortes", "titulo": "Cortes de Filmes"}, fb)
        self.assertTrue(pelo_titulo.confere)
        pessoal = pagina_facebook.decidir({"c_user": C_USER, "i_user": None},
                                          {"id": C_USER, "titulo": "Fulano Pessoal | Facebook"}, fb)
        self.assertFalse(pessoal.confere)
        self.assertEqual((pessoal.agindo_como, pessoal.id, pessoal.nome), ("pessoal", None, None))
        self.assertIn("o seu perfil pessoal", pessoal.motivo)
        self.assertNotIn("Fulano", pessoal.motivo)
        outra = pagina_facebook.decidir({"c_user": C_USER, "i_user": OUTRA}, {"id": OUTRA, "titulo": "Outra"}, fb)
        self.assertEqual((outra.agindo_como, outra.confere, outra.nome), ("outra", False, "Outra"))
        self.assertIn(f"a Página Outra (id {OUTRA})", outra.motivo)
        discordam = pagina_facebook.decidir({"c_user": C_USER, "i_user": PAGINA},
                                            {"id": C_USER, "titulo": "Fulano Pessoal"}, fb)
        self.assertFalse(discordam.confere)
        self.assertTrue(discordam.motivo.startswith("os sinais não concordam"), discordam.motivo)
        self.assertNotIn("Fulano", discordam.motivo)
        self.assertSemCUser(pessoal.motivo, discordam.motivo, pessoal.publico(), discordam.publico())
        # cookie que não é número: como se não existisse
        self.assertEqual(pagina_facebook.decidir({"c_user": "abc", "i_user": PAGINA}, {"id": PAGINA}, fb).agindo_como,
                         "deslogado")
        self.assertEqual(pagina_facebook.decidir({"c_user": C_USER, "i_user": "x1"}, {"id": PAGINA}, fb).agindo_como,
                         "pessoal")
        self.assertEqual(pagina_facebook.decidir({"c_user": C_USER, "i_user": C_USER}, {"id": C_USER}, fb).agindo_como,
                         "pessoal")
        self.assertFalse(pagina_facebook.decidir({}, {}, fb).confere)

    def test_decidir_aprende_pelo_link(self):
        pelo_id = pagina_facebook.decidir({"c_user": C_USER, "i_user": PAGINA}, {"id": PAGINA, "titulo": "Cortes"},
                                          self.fb(pagina_id="", pagina_nome=""))
        self.assertTrue(pelo_id.confere)
        self.assertEqual(pelo_id.aprendido, {"pagina_id": PAGINA, "pagina_nome": "Cortes"})
        self.assertIsNone(pelo_id.sugestao)
        # o link aponta outra Página que a do cookie: não aprende
        errado = pagina_facebook.decidir({"c_user": C_USER, "i_user": OUTRA}, {"id": OUTRA},
                                         self.fb(pagina_id="", pagina_nome=""))
        self.assertFalse(errado.confere)
        self.assertIsNone(errado.aprendido)
        curto = self.fb(pagina="https://www.facebook.com/CortesDeFilmes/", pagina_id="", pagina_nome="")
        pelo_caminho = pagina_facebook.decidir({"c_user": C_USER, "i_user": PAGINA},
                                               {"id": None, "caminho": "/cortesdefilmes", "titulo": "Cortes"}, curto)
        self.assertTrue(pelo_caminho.confere)
        self.assertEqual(pelo_caminho.aprendido, {"pagina_id": PAGINA, "pagina_nome": ""})
        no_pessoal = pagina_facebook.decidir({"c_user": C_USER}, {"id": None, "caminho": "/fulano.pessoal"}, curto)
        self.assertEqual((no_pessoal.confere, no_pessoal.agindo_como), (False, "pessoal"))

    def test_decidir_pelo_nome_so_sugere(self):
        fb = self.fb(pagina="Cortes de Filmes", pagina_id="", pagina_nome="")
        ident = pagina_facebook.decidir({"c_user": C_USER, "i_user": PAGINA},
                                        {"id": PAGINA, "titulo": "Cortes de Filmes | Facebook"}, fb)
        self.assertFalse(ident.confere)
        self.assertIsNone(ident.aprendido)
        self.assertEqual(ident.sugestao, {"pagina_id": PAGINA, "pagina_nome": "Cortes de Filmes"})
        self.assertIn("confirme", ident.motivo)
        outro_nome = pagina_facebook.decidir({"c_user": C_USER, "i_user": OUTRA}, {"id": OUTRA, "titulo": "Outra"}, fb)
        self.assertIsNone(outro_nome.sugestao)
        vazio = pagina_facebook.decidir({"c_user": C_USER, "i_user": PAGINA}, {"id": PAGINA},
                                        self.fb(pagina="", pagina_id="", pagina_nome=""))
        self.assertIn("falta informar a Página", vazio.motivo)

    def test_link_do_me_nao_ensina_a_pagina(self):
        # o /me sem redirecionar: a URL lida continua /me, e o i_user era de outra Página qualquer
        for link in ("facebook.com/me", "https://www.facebook.com/me/", "https://www.facebook.com/home.php"):
            with self.subTest(link=link):
                ident = pagina_facebook.decidir({"c_user": C_USER, "i_user": OUTRA},
                                                {"id": None, "caminho": "/me", "titulo": "Outra"},
                                                self.fb(pagina=link, pagina_id="", pagina_nome=""))
                self.assertFalse(ident.confere)
                self.assertIsNone(ident.aprendido)
                self.assertIsNone(ident.sugestao)
                self.assertIn("não é de uma Página", ident.motivo)
                self.assertIn("use o link da Página (facebook.com/SuaPagina ou profile.php?id=...)", ident.motivo)

    def test_id_do_me_decide_antes_do_titulo(self):
        # o título do /me igual ao pagina_nome não vale contra um id na URL que é outro
        for numero in (OUTRA, C_USER):
            with self.subTest(numero=numero):
                ident = pagina_facebook.decidir({"c_user": C_USER, "i_user": PAGINA},
                                                _me(numero, "Cortes de Filmes"), self.fb())
                self.assertFalse(ident.confere)
                self.assertTrue(ident.motivo.startswith("os sinais não concordam"), ident.motivo)
                self.assertSemCUser(ident.motivo, ident.publico())
        # sem id na URL, o título ainda vale
        sem_id = pagina_facebook.decidir({"c_user": C_USER, "i_user": PAGINA},
                                         {"id": None, "caminho": "/cortes", "titulo": "Cortes de Filmes"}, self.fb())
        self.assertTrue(sem_id.confere)

    def test_sugestao_pelo_nome_com_id_que_contradiz(self):
        fb = self.fb(pagina="Cortes de Filmes", pagina_id="", pagina_nome="")
        for numero in (OUTRA, C_USER):
            with self.subTest(numero=numero):
                ident = pagina_facebook.decidir({"c_user": C_USER, "i_user": PAGINA},
                                                _me(numero, "Cortes de Filmes"), fb)
                self.assertIsNone(ident.sugestao)
                self.assertFalse(ident.confere)
                self.assertSemCUser(ident.motivo, ident.publico())
        # o id da URL é o do cookie: sugere
        certo = pagina_facebook.decidir({"c_user": C_USER, "i_user": PAGINA}, _me(PAGINA, "Cortes de Filmes"), fb)
        self.assertEqual(certo.sugestao, {"pagina_id": PAGINA, "pagina_nome": "Cortes de Filmes"})
        # sem id na URL (o /me foi para o link curto), vale o título
        curto = pagina_facebook.decidir({"c_user": C_USER, "i_user": PAGINA},
                                        {"id": None, "caminho": "/cortes", "titulo": "Cortes de Filmes | Facebook"}, fb)
        self.assertEqual(curto.sugestao, {"pagina_id": PAGINA, "pagina_nome": "Cortes de Filmes"})

    def test_descrever_sem_o_c_user(self):
        url_pessoal = f"https://www.facebook.com/profile.php?id={C_USER}"
        textos = [
            pagina_facebook.descrever({"c_user": C_USER}, {"id": C_USER, "url": url_pessoal, "titulo": "Fulano"}),
            pagina_facebook.descrever({"c_user": C_USER, "i_user": PAGINA},
                                      {"id": C_USER, "url": url_pessoal, "titulo": "Fulano Pessoal"}),
            pagina_facebook.descrever({}, {"id": C_USER, "url": url_pessoal}),
            pagina_facebook.descrever({"c_user": C_USER}),
        ]
        self.assertSemCUser(*textos)
        self.assertEqual(textos[0], "o seu perfil pessoal")
        self.assertIn(f"a Página id {PAGINA}", textos[1])
        self.assertNotIn("Fulano", textos[1])
        self.assertIn("uma página que não reconheço", textos[2])
        # o título só entra com o id do i_user na URL
        self.assertEqual(pagina_facebook.descrever({"c_user": C_USER, "i_user": PAGINA},
                                                   {"id": PAGINA, "titulo": "Cortes | Facebook"}),
                         f"a Página Cortes (id {PAGINA})")

    def cfg(self, **fb) -> dict:
        return {"facebook": self.fb(**fb)}

    def test_exigir_no_perfil_pessoal(self):
        aba = _AbaDeMentira(i_user=None, me_url=f"https://www.facebook.com/profile.php?id={C_USER}",
                            titulo="Fulano Pessoal | Facebook")
        with self.assertRaises(pagina_facebook.ErroPagina) as erro:
            pagina_facebook.exigir(aba, self.cfg(), trocar=False)
        e = erro.exception
        self.assertEqual((e.tipo, e.persistente, e.identidade.agindo_como), ("bloqueio", True, "pessoal"))
        self.assertTrue(str(e).startswith("não consegui confirmar que estou na Página"), str(e))
        self.assertIn("Troque para a Página", str(e))
        self.assertSemCUser(str(e), e.identidade.publico())
        self.assertNotIn("Fulano", str(e))
        self.assertEqual(aba.navegou, [pagina_facebook.BASE + "/me"])  # sem trocar=True, nada de troca

    def test_exigir_na_pagina_certa(self):
        aba = _AbaDeMentira(i_user=PAGINA, me_url=LINK_PAGINA, titulo="Cortes de Filmes | Facebook")
        ident, c_user = pagina_facebook.exigir(aba, self.cfg(), trocar=True)
        self.assertTrue(ident.confere)
        self.assertEqual(c_user, C_USER)

    def test_antes_de_publicar(self):
        cfg = self.cfg()
        pagina_facebook.antes_de_publicar(_AbaDeMentira(i_user=PAGINA, url=f"https://x.invalid/?asset_id={PAGINA}"),
                                          cfg, C_USER)
        casos = {
            "perfil pessoal": _AbaDeMentira(i_user=None),
            "outra Página": _AbaDeMentira(i_user=OUTRA),
            "outra pessoa": _AbaDeMentira(c_user="123456", i_user=PAGINA),
            "asset_id de outra Página": _AbaDeMentira(i_user=PAGINA, url=f"https://x.invalid/?asset_id={OUTRA}"),
        }
        for caso, aba in casos.items():
            with self.subTest(caso=caso), self.assertRaises(pagina_facebook.ErroPagina) as erro:
                pagina_facebook.antes_de_publicar(aba, cfg, C_USER)
            self.assertTrue(str(erro.exception).startswith("a Página mudou no meio do envio"))
            self.assertTrue(erro.exception.persistente)
            self.assertSemCUser(str(erro.exception), erro.exception.identidade.publico())
        with self.assertRaises(pagina_facebook.ErroPagina):  # sem a Página confirmada, nunca passa
            pagina_facebook.antes_de_publicar(_AbaDeMentira(i_user=PAGINA), self.cfg(pagina_id=""), C_USER)


# ---------------------------------------------------------------- Facebook falso no navegador

NOMES_FB = {C_USER: "Fulano Pessoal", PAGINA: "Página Certa", OUTRA: "Outra Página"}

PAGINA_INICIAL = """<!doctype html><html><head><meta charset="utf-8"><title>Facebook</title></head><body>
<div role="button" tabindex="0" aria-label="Seu perfil" id="menu-conta">foto</div>
<div id="menu" role="menu" hidden><div role="menuitem" tabindex="0" id="todos">Ver todos os perfis</div></div>
<div id="perfis" role="dialog" hidden>
  <div class="linha" role="button" tabindex="0" data-id="__C__"><a href="/profile.php?id=__C__">foto</a>
    <span>Fulano Pessoal</span></div>
  <div class="linha" role="button" tabindex="0" data-id="__OUTRA__"><a href="/profile.php?id=__OUTRA__">foto</a>
    <span>Outra Página</span></div>
  <linha-sombra data-id="__PAGINA__" data-nome="Página Certa"></linha-sombra>
</div>
<a href="/reels/create">Criar reel</a>
<script>
function trocar(id) { fetch("/trocar?para=" + id).then(() => { document.getElementById("perfis").hidden = true; }); }
customElements.define("linha-sombra", class extends HTMLElement {
  connectedCallback() {
    if (this.shadowRoot) return;
    const raiz = this.attachShadow({ mode: "open" });
    raiz.innerHTML = '<div role="button" tabindex="0" class="linha"><a href="/profile.php?id=' + this.dataset.id
      + '">foto</a> <span>' + this.dataset.nome + '</span></div>';
    raiz.querySelector(".linha").addEventListener("click", (e) => { e.preventDefault(); trocar(this.dataset.id); });
  }
});
document.getElementById("menu-conta").addEventListener("click", () => { document.getElementById("menu").hidden = false; });
document.getElementById("todos").addEventListener("click", () => { document.getElementById("perfis").hidden = false; });
for (const l of document.querySelectorAll("#perfis > .linha")) {
  l.addEventListener("click", (e) => { e.preventDefault(); trocar(l.dataset.id); });
}
</script></body></html>"""

PAGINA_PERFIL = """<!doctype html><html><head><meta charset="utf-8"><title>__NOME__ | Facebook</title></head><body>
<h1>__NOME__</h1>__BOTAO__
<script>
const b = document.getElementById("mudar");
if (b) b.addEventListener("click", () => fetch("/trocar?para=__ID__"));
</script></body></html>"""

COMPOSITOR = """<!doctype html><html><head><meta charset="utf-8"><title>Criar reel | Facebook</title></head><body>
<input type="file" id="arquivo" accept="video/*">
<div contenteditable="true" aria-label="Descreva seu reel" id="descricao" style="min-height:40px"></div>
<div role="button" tabindex="0" id="publicar" aria-disabled="true">Publicar</div>
<div id="feito"></div>
<script>
const cenario = "__CENARIO__";
const pub = document.getElementById("publicar");
const liberar = () => pub.setAttribute("aria-disabled", "false");
document.getElementById("arquivo").addEventListener("change", () => {
  if (cenario === "muda_antes") {
    const x = new XMLHttpRequest(); x.open("GET", "/trocar?para=__OUTRA__", false); x.send(); liberar();
  } else if (cenario === "muda_durante") {
    setTimeout(() => fetch("/trocar?para=__OUTRA__"), 1000); setTimeout(liberar, 3000);
  } else liberar();
});
pub.addEventListener("click", () => {
  if (pub.getAttribute("aria-disabled") === "true") return;
  fetch("/publicar", { method: "POST", body: document.getElementById("descricao").innerText })
    .then(() => { document.getElementById("feito").textContent = "Seu reel foi publicado"; });
});
</script></body></html>"""

# a lista de perfis já aberta, para o R3: tipo r3 (só "Cortes de Filmes" visível), repetido e ok
LISTAS = {
    "r3": '<div role="button" class="linha" data-id="1" hidden><span>Cortes</span></div>'
          '<div role="button" class="linha" data-id="2"><span>Cortes de Filmes</span></div>',
    "repetido": '<div role="button" class="linha" data-id="1"><span>Cortes</span></div>'
                '<div role="button" class="linha" data-id="2"><span>Cortes</span></div>',
    "ok": '<div role="button" class="linha" data-id="1"><span>Cortes</span></div>'
          '<div role="button" class="linha" data-id="2"><span>Cortes de Filmes</span></div>',
}
PAGINA_LISTA = """<!doctype html><html><head><meta charset="utf-8"><title>Lista</title></head><body>
<p>Cortes</p><div role="dialog">__LINHAS__</div>
<script>
for (const l of document.querySelectorAll(".linha")) l.addEventListener("click", () => fetch("/trocar?para=" + l.dataset.id));
</script></body></html>"""


class _FacebookFalso(http.server.BaseHTTPRequestHandler):
    """Imita o que o AutoCortes olha no Facebook: os cookies, o /me, a troca de perfil e o compositor."""

    cenario = ""  # troca_quebrada, muda_durante ou muda_antes
    trocas: list[str] = []
    publicados: list[dict] = []

    def _cookies(self) -> dict:
        saida = {}
        for parte in (self.headers.get("Cookie") or "").split(";"):
            nome, _, valor = parte.strip().partition("=")
            if nome:
                saida[nome] = valor
        return saida

    def _responder(self, corpo: str = "ok", status: int = 200, cookies: dict | None = None, local: str = ""):
        dados = corpo.encode("utf-8")
        self.send_response(status)
        for nome, valor in (cookies or {}).items():
            # como o servidor de teste é http, sem SameSite=None (o Chrome recusaria sem Secure)
            fim = "; Max-Age=0" if valor is None else ""
            self.send_header("Set-Cookie", f"{nome}={valor or ''}; Path=/; HttpOnly{fim}")
        if local:
            self.send_header("Location", local)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(dados)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(dados)

    def do_POST(self):
        tamanho = int(self.headers.get("Content-Length") or 0)
        texto = self.rfile.read(tamanho).decode("utf-8", "replace")
        if urlsplit(self.path).path == "/publicar":
            _FacebookFalso.publicados.append({"i_user": self._cookies().get("i_user"), "texto": texto})
        self._responder()

    def do_GET(self):
        partes = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(partes.query).items()}
        ck = self._cookies()
        caminho = partes.path
        if caminho == "/entrar":
            como = q.get("como", "pessoal")
            i_user = {"pagina": PAGINA, "outra": OUTRA}.get(como)
            self._responder(cookies={"c_user": None if como == "sair" else C_USER, "i_user": i_user})
        elif caminho == "/trocar":
            _FacebookFalso.trocas.append(q.get("para", ""))
            if _FacebookFalso.cenario == "troca_quebrada":
                self._responder()
            else:
                para = q.get("para")
                self._responder(cookies={"i_user": None if para == C_USER else para})
        elif caminho in ("/login",) or (caminho == "/" and "c_user" not in ck):
            self._responder('<!doctype html><title>Entrar</title><form><input type="password"></form>')
        elif caminho == "/":
            self._responder(PAGINA_INICIAL.replace("__C__", C_USER).replace("__OUTRA__", OUTRA)
                            .replace("__PAGINA__", PAGINA))
        elif caminho == "/me":
            self._responder(status=302, local=f"/profile.php?id={ck.get('i_user') or ck.get('c_user')}")
        elif caminho == "/profile.php":
            numero = q.get("id", "")
            botao = ('<div role="button" tabindex="0" id="mudar">Mudar agora</div>'
                     if numero != C_USER and "i_user" not in ck else "")
            self._responder(PAGINA_PERFIL.replace("__NOME__", NOMES_FB.get(numero, "Alguém"))
                            .replace("__BOTAO__", botao).replace("__ID__", numero))
        elif caminho == "/reels/create":
            self._responder(COMPOSITOR.replace("__CENARIO__", _FacebookFalso.cenario).replace("__OUTRA__", OUTRA))
        elif caminho == "/lista":
            self._responder(PAGINA_LISTA.replace("__LINHAS__", LISTAS[q.get("tipo", "ok")]))
        else:
            self._responder("não existe", 404)

    def log_message(self, *args):
        pass


class _NavegadorDoTeste:
    """O Navegador do AutoCortes trocado pela aba do navegador sem janela do teste."""

    aba = None

    def __init__(self, cfg, parar=None):
        self.conf = {}

    def abrir(self):
        pass

    def nova_aba(self, url: str = "about:blank"):
        return _NavegadorDoTeste.aba

    def fechar_aba(self, aba) -> None:
        pass


class _Registro(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.linhas: list[str] = []

    def emit(self, record):
        self.linhas.append(record.getMessage())


class TestNoNavegador(ti.BaseBanco):
    """Item 7: o envio e a gravação do Facebook num navegador sem janela, contra um Facebook falso local.

    Critérios 8 a 13 e 15a (registro e navegador.json) e o R3. Nada abre o Facebook de verdade.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pasta = tempfile.TemporaryDirectory(prefix="autocortes-teste-facebook-nav-")
        cls.addClassCleanup(cls.pasta.cleanup)
        try:
            programa = navegador.caminho_programa(
                config.Config(copy.deepcopy(config.PADRAO), Path(cls.pasta.name) / "config.toml"))
        except navegador.ErroNavegador as erro:
            raise unittest.SkipTest(str(erro)) from erro
        cls.servidor = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FacebookFalso)
        threading.Thread(target=cls.servidor.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.servidor.server_close)
        cls.addClassCleanup(cls.servidor.shutdown)
        cls.site = f"http://127.0.0.1:{cls.servidor.server_address[1]}"
        porta = navegador.porta_livre()
        cls.devtools = f"http://127.0.0.1:{porta}"
        cls.processo = subprocess.Popen(
            [str(programa), "--headless=new", f"--remote-debugging-port={porta}",
             "--remote-debugging-address=127.0.0.1", f"--user-data-dir={cls.pasta.name}/chrome",
             "--no-first-run", "--no-default-browser-check", "about:blank"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        cls.addClassCleanup(cls._encerrar)
        limite = time.monotonic() + 20
        while time.monotonic() < limite:
            try:
                paginas = requests.get(f"{cls.devtools}/json/list", timeout=1).json()
                pagina = next(p for p in paginas if p["type"] == "page")
                cls.aba = navegador.Aba(navegador._Websocket(pagina["webSocketDebuggerUrl"], timeout=10), ritmo=(0, 0))
                cls.addClassCleanup(cls.aba._ws.fechar)
                return
            except (requests.RequestException, StopIteration):
                time.sleep(0.1)
        raise RuntimeError("O navegador de teste não abriu")

    _encerrar = classmethod(ti.TestPaginasFalsas._encerrar.__func__)

    def setUp(self):
        super().setUp()
        _NavegadorDoTeste.aba = self.aba
        _FacebookFalso.cenario = ""
        _FacebookFalso.trocas = []
        _FacebookFalso.publicados = []
        self.so_redes("facebook")
        self.cfg["facebook"].update(envio="navegador", pagina=LINK_PAGINA, pagina_id=PAGINA, pagina_nome="Página Certa")
        self.cfg["navegador"]["ensaio"] = False
        gravador.gravar_roteiro(self.cfg, "facebook", (
            f"abrir {self.site}/reels/create\n"
            "video em input[type=file]\n"
            'escrever descricao em [aria-label="Descreva seu reel"]\n'
            'publicar "Publicar"\n'
            'conferir "Seu reel foi publicado"\n'))
        for troca in (patch.object(plat_nav, "Navegador", _NavegadorDoTeste),
                      patch.dict(plat_nav.PAGINAS, {"facebook": (self.site + "/login", self.site + "/")}),
                      patch.object(pagina_facebook, "BASE", self.site),
                      patch.object(plat_nav, "ESPERA_PUBLICAR", 20),
                      patch.object(pagina_facebook, "ESPERA_TROCA", 3),
                      patch.object(pagina_facebook, "ESPERA_BOTAO", 1.5),
                      patch.object(pagina_facebook, "ESPERA_LISTA", 3)):
            troca.start()
            self.addCleanup(troca.stop)
        self.registro = _Registro()
        logger = logging.getLogger("autocortes")
        nivel = logger.level
        logger.addHandler(self.registro)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, nivel)
        self.addCleanup(logger.removeHandler, self.registro)

    # ---- ajudantes
    def como(self, quem: str) -> None:
        """Põe a sessão no perfil pedido (pessoal, pagina, outra ou sair)."""
        self.aba.navegar(f"{self.site}/entrar?como={quem}")

    def i_user(self):
        return pagina_facebook.cookies(self.aba)["i_user"]

    def postar(self) -> tuple[str, int]:
        corte = self.novo_corte()
        pub = publicador.Publicador(self.cfg, threading.Event())
        pub.plataformas["facebook"] = plat_nav.ViaNavegador("facebook", self.cfg)
        return pub.postar(self.conn, "facebook", self.corte_row(corte), manual=True), corte

    def bloqueio(self) -> dict | None:
        return publicador.ler_bloqueios(self.conn).get("facebook")

    def conferencia(self) -> dict:
        return (plat_nav.ViaNavegador("facebook", self.cfg).token().get("paginas") or {}).get("facebook") or {}

    def assertSemCUser(self, *extras):
        guardado = json.dumps(plat_nav.ViaNavegador("facebook", self.cfg).token(), ensure_ascii=False)
        for texto in [guardado, *self.registro.linhas, *(str(x) for x in extras)]:
            self.assertNotIn(C_USER, texto)

    def assertPublicou(self, status: str, corte: int) -> None:
        self.assertEqual(status, "publicado")
        self.assertEqual([p["i_user"] for p in _FacebookFalso.publicados], [PAGINA])
        linha = self.conn.execute("SELECT status FROM postagens WHERE corte_id=? AND plataforma='facebook'",
                                  (corte,)).fetchone()
        self.assertEqual(linha["status"], "publicado")
        self.assertTrue(self.conferencia().get("confere"))

    def assertBloqueou(self, status: str, corte: int, comeco: str) -> None:
        self.assertEqual(status, "bloqueado")
        self.assertEqual(_FacebookFalso.publicados, [])
        self.assertIsNone(self.conn.execute("SELECT id FROM postagens WHERE corte_id=?", (corte,)).fetchone())
        pausa = self.bloqueio()
        self.assertIsNotNone(pausa)
        self.assertIsNone(pausa["ate"])
        self.assertTrue(pausa["motivo"].startswith(comeco), pausa["motivo"])
        self.assertSemCUser(pausa)
        self.assertTrue(list((self.cfg.pasta_dados / "navegador").glob("facebook-pagina-*.png")))

    # ---- critérios 8 a 13
    def test_a_pagina_certa_publica(self):
        self.como("pagina")
        status, corte = self.postar()
        self.assertPublicou(status, corte)
        self.assertEqual(_FacebookFalso.trocas, [])
        self.assertSemCUser()

    def test_b_perfil_pessoal_troca_pelo_botao_da_pagina(self):
        self.como("pessoal")
        status, corte = self.postar()
        self.assertPublicou(status, corte)
        self.assertEqual(_FacebookFalso.trocas, [PAGINA])
        self.assertSemCUser()  # 15a: o cenário do perfil pessoal, no registro e no navegador.json

    def test_c_outra_pagina_troca_pela_lista_de_perfis(self):
        self.como("outra")
        status, corte = self.postar()
        self.assertPublicou(status, corte)
        self.assertEqual(_FacebookFalso.trocas, [PAGINA])  # a linha da Página, dentro do shadow DOM
        self.assertSemCUser()

    def test_d_troca_que_nao_muda_o_cookie_nao_posta(self):
        self.como("pessoal")
        _FacebookFalso.cenario = "troca_quebrada"
        status, corte = self.postar()
        self.assertBloqueou(status, corte, "não consegui confirmar que estou na Página")
        self.assertEqual(self.conferencia().get("agindo_como"), "pessoal")
        self.assertFalse(self.conferencia().get("confere"))
        self.assertSemCUser()

    def test_e_pagina_muda_enquanto_o_botao_espera(self):
        self.como("pagina")
        _FacebookFalso.cenario = "muda_durante"
        chamadas = []
        original = pagina_facebook.antes_de_publicar

        def contar(*a, **k):
            chamadas.append(time.monotonic())
            return original(*a, **k)

        with patch.object(pagina_facebook, "antes_de_publicar", contar):
            status, corte = self.postar()
        self.assertBloqueou(status, corte, "a Página mudou no meio do envio")
        self.assertEqual(len(chamadas), 2)  # a de antes do passo passou; a de dentro do antes() parou
        self.assertEqual(self.i_user(), OUTRA)

    def test_e_pagina_muda_antes_do_passo(self):
        self.como("pagina")
        _FacebookFalso.cenario = "muda_antes"
        status, corte = self.postar()
        self.assertBloqueou(status, corte, "a Página mudou no meio do envio")

    def test_e_entrega_nao_aceita(self):
        """Critério 12 pela via direta: o ErroPagina sobe e a Entrega não é aceita."""
        self.como("pagina")
        _FacebookFalso.cenario = "muda_durante"
        entrega = Entrega()
        arquivo = self.raiz / "video.mp4"
        arquivo.write_bytes(bytes(512))
        with self.assertRaises(ErroPublicacao) as erro:
            plat_nav.ViaNavegador("facebook", self.cfg).publicar(arquivo, Conteudo("T", "Descrição", []), None, entrega)
        self.assertEqual((erro.exception.tipo, erro.exception.persistente), ("bloqueio", True))
        self.assertFalse(entrega.aceita)

    def test_ensaio(self):
        """Critério 13: na Página certa, ensaio; com o perfil trocado antes do publicar, bloqueado."""
        self.cfg["navegador"]["ensaio"] = True
        self.como("pagina")
        status, corte = self.postar()
        self.assertEqual(status, "ensaio")
        self.assertEqual(_FacebookFalso.publicados, [])
        self.assertIsNone(self.bloqueio())
        self.como("pagina")
        _FacebookFalso.cenario = "muda_antes"
        status, corte = self.postar()
        self.assertBloqueou(status, corte, "a Página mudou no meio do envio")

    def test_sem_roteiro_pede_para_gravar(self):
        gravador.arquivo_roteiro(self.cfg, "facebook").unlink()
        (self.cfg.pasta_dados / "chrome").mkdir(parents=True, exist_ok=True)
        plat_nav.ViaNavegador("facebook", self.cfg).lembrar_conta("Página Página Certa (id 5550001)")
        self.assertEqual(plat_nav.ViaNavegador("facebook", self.cfg).pronta(),
                         (False, "falta gravar o roteiro do Facebook (Redes sociais > Aprender a postar)"))
        with self.assertRaises(ErroPublicacao) as erro:
            plat_nav.ViaNavegador("facebook", self.cfg).publicar(self.raiz / "x.mp4", Conteudo("T", "D", []))
        self.assertEqual(erro.exception.tipo, "bloqueio")
        self.assertIn("grave o roteiro", str(erro.exception))

    def test_verificar_e_desconectar(self):
        (self.cfg.pasta_dados / "chrome").mkdir(parents=True, exist_ok=True)
        self.como("pessoal")
        via = plat_nav.ViaNavegador("facebook", self.cfg)
        self.assertEqual(via.verificar(), "Página Página Certa (id 5550001) pelo navegador")
        self.assertTrue(via.identidade.confere)
        self.assertEqual(via.pronta(), (True, "ok"))
        self.cfg["facebook"]["pagina_id"] = ""
        self.assertEqual(via.pronta()[1], "falta confirmar a Página: informe a Página e use Testar sessão")
        self.cfg["facebook"]["pagina_id"] = PAGINA
        _FacebookFalso.cenario = "troca_quebrada"
        self.como("pessoal")
        with self.assertRaises(ErroPublicacao) as erro:
            via.verificar()
        self.assertTrue(erro.exception.persistente)
        self.assertIn("não consegui confirmar que estou na Página", str(erro.exception))
        self.assertEqual(self.conferencia().get("agindo_como"), "pessoal")
        self.assertSemCUser(erro.exception)
        via.desconectar()
        self.assertEqual(self.conferencia(), {})
        self.assertIsNone(via.conta())

    def test_checkpoint_e_deslogado(self):
        self.como("sair")
        aba = self.aba
        aba.navegar(self.site + "/")
        with self.assertRaises(navegador.ErroNavegador) as erro:
            plat_nav.esperar_sessao(aba, "facebook", 3)
        self.assertEqual(erro.exception.tipo, "bloqueio")
        self.como("pagina")
        aba.navegar(self.site + "/checkpoint/123")
        with self.assertRaises(navegador.ErroNavegador) as erro:
            plat_nav.esperar_sessao(aba, "facebook", 3)
        self.assertTrue(erro.exception.persistente)

    # ---- (f) gravação
    def test_f_gravacao(self):
        self.como("pessoal")
        g = plat_nav.Gravacao(self.cfg, "facebook")
        with self.assertRaises(pagina_facebook.ErroPagina) as erro:
            g.iniciar()
        self.assertEqual(erro.exception.identidade.agindo_como, "pessoal")
        self.assertEqual(_FacebookFalso.trocas, [])  # na gravação, quem troca é você
        self.assertEqual(plat_nav.gravando(), [])
        self.como("pagina")
        g = plat_nav.Gravacao(self.cfg, "facebook")
        g.iniciar()
        try:
            self.assertEqual(plat_nav.gravando(), ["facebook"])
            self.assertEqual(self.aba.url(), self.site + "/")
            g.passos.append({"tipo": "clique"})
            self.como("outra")  # o post saiu agindo como outra Página
            with patch.object(plat_nav.gravador, "salvar", lambda *a, **k: {"ok": True}):
                resultado = g.concluir()
        finally:
            g.cancelar()
        self.assertIn(f"a Página id {OUTRA}", resultado["aviso"])
        self.assertSemCUser(resultado)

    # ---- R3: a escolha na lista de perfis
    def test_r3_lista_de_perfis(self):
        self.aba.navegar(f"{self.site}/lista?tipo=r3")
        self.assertEqual(pagina_facebook.escolher_perfil(self.aba, "Cortes", "")["estado"], "nenhum")
        self.aba.navegar(f"{self.site}/lista?tipo=repetido")
        self.assertEqual(pagina_facebook.escolher_perfil(self.aba, "Cortes", ""), {"estado": "repetido", "n": 2})
        time.sleep(0.3)
        self.assertEqual(_FacebookFalso.trocas, [])
        self.aba.navegar(f"{self.site}/lista?tipo=ok")
        self.assertEqual(pagina_facebook.escolher_perfil(self.aba, "  cortes ", "")["estado"], "clicado")
        limite = time.monotonic() + 3
        while not _FacebookFalso.trocas and time.monotonic() < limite:
            time.sleep(0.1)
        self.assertEqual(_FacebookFalso.trocas, ["1"])  # o parágrafo "Cortes" fora da lista não conta


# ---------------------------------------------------------------- item 8: rotas do painel

def _me(numero: str, titulo: str) -> dict:
    url = f"{pagina_facebook.BASE}/profile.php?id={numero}"
    return {"url": url, "id": pagina_facebook._id_da_url(url), "caminho": pagina_facebook._caminho(url),
            "titulo": f"{titulo} | Facebook"}


# os dois sinais (cookies e destino do /me) de cada perfil, como a janela os leria
SINAIS = {
    "pessoal": ({"c_user": C_USER, "i_user": None}, _me(C_USER, "Fulano Pessoal")),
    "pagina": ({"c_user": C_USER, "i_user": PAGINA}, _me(PAGINA, "Página Certa")),
    "outra": ({"c_user": C_USER, "i_user": OUTRA}, _me(OUTRA, "Outra Página")),
}


class _FacebookDaCli:
    """O ViaNavegador do Facebook no verificar: a conferência vem dos SINAIS, como o real a guarda."""

    def __init__(self, cfg, sinais):
        self.cfg, self.sinais = cfg, sinais
        self.identidade = None

    def pronta(self):
        return True, ""

    def verificar(self):
        ck, me = self.sinais
        self.identidade = pagina_facebook.decidir(ck, me, self.cfg["facebook"])
        if not self.identidade.confere:
            raise ErroPublicacao(f"Facebook: {pagina_facebook.mensagem(self.cfg['facebook'], self.identidade)}",
                                 "bloqueio", persistente=True)
        return f"Página id {self.identidade.id} pelo navegador"


class TestCliVerificar(ti.Base):
    """Item 11 do desenho: a CLI não grava o config, então o verificar --online escreve o pagina_id no registro."""

    def rodar(self, pagina: str, sinais) -> list[str]:
        import contextlib
        import io
        from types import SimpleNamespace

        from autocortes import cli, midia
        for rede in config.PLATAFORMAS:
            self.cfg[rede]["ativo"] = rede == "facebook"
        self.cfg["facebook"].update(envio="navegador", pagina=pagina, pagina_id="", pagina_nome="")
        self.cfg["transcricao"]["fonte"] = "nenhuma"
        registro = _Registro()
        logger = logging.getLogger("autocortes")
        nivel = logger.level
        logger.addHandler(registro)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, nivel)
        self.addCleanup(logger.removeHandler, registro)
        with patch.object(cli, "_carregar", lambda args: self.cfg), \
                patch.object(midia, "versao_ffmpeg", lambda caminho: "ffmpeg de teste"), \
                patch.object(midia, "filtros_disponiveis",
                             lambda caminho: {"ass", "scdet", "cropdetect", "astats", "loudnorm", "boxblur"}), \
                patch("autocortes.plataformas.criar", lambda nome, cfg: _FacebookDaCli(cfg, sinais)), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_verificar(SimpleNamespace(online=True, config=None, detalhado=False))
        return registro.linhas

    def test_aprendido_pelo_link(self):
        linhas = self.rodar(LINK_PAGINA, SINAIS["pagina"])
        self.assertTrue(any(f'informe pagina_id = "{PAGINA}" em [facebook]' in l for l in linhas), linhas)
        self.assertNotIn(C_USER, "\n".join(linhas))

    def test_sugestao_pelo_nome(self):
        linhas = self.rodar("Página Certa", SINAIS["pagina"])
        self.assertTrue(any(f'Página Certa; se for esta, informe pagina_id = "{PAGINA}" em [facebook]' in l
                            for l in linhas), linhas)
        self.assertNotIn(C_USER, "\n".join(linhas))

    def test_perfil_pessoal_nao_escreve(self):
        linhas = self.rodar(LINK_PAGINA, SINAIS["pessoal"])
        self.assertFalse(any("informe pagina_id" in l for l in linhas), linhas)


class _AbaDoPainel:
    """Aba que aceita qualquer comando e não faz nada: a conferência é trocada nos testes."""

    def url(self):
        return "https://exemplo.invalid/"

    def __getattr__(self, nome):
        return lambda *a, **k: None


class _NavegadorDoPainel:
    falha: str | None = None

    def __init__(self, cfg, parar=None):
        self.conf = {}

    def abrir(self):
        if _NavegadorDoPainel.falha:
            raise navegador.ErroNavegador(_NavegadorDoPainel.falha)

    def nova_aba(self, url: str = "about:blank"):
        self.abrir()
        return _AbaDoPainel()

    def fechar_aba(self, aba) -> None:
        pass


class _MotorDoPainel:
    def __init__(self, cfg):
        self.cfg = cfg
        self.publicador = None
        self.rodando = self.parando = False
        self.iniciado_em = None
        self.atualizadas: list[tuple[str, bool]] = []

    def aplicar_config(self, novo) -> None:
        self.cfg.atualizar_de(novo)

    def rede_atualizada(self, rede: str, persistente: bool = False) -> None:
        self.atualizadas.append((rede, persistente))
        conn = db.conectar(self.cfg.banco, preparar=False)
        try:
            publicador.desbloquear(conn, rede, persistente)
        finally:
            conn.close()


class TestPainelFacebook(ti.BaseBanco):
    """Item 8: as rotas do Facebook no painel (critérios 14 a 17 e o R7), com a conferência trocada."""

    def setUp(self):
        super().setUp()
        from types import SimpleNamespace

        from autocortes.painel import api
        self.api = api
        self.so_redes("facebook")
        self.cfg["facebook"].update(envio="navegador", pagina=LINK_PAGINA, pagina_id=PAGINA, pagina_nome="Página Certa")
        (self.cfg.pasta_dados / "chrome").mkdir(parents=True, exist_ok=True)
        self.motor = _MotorDoPainel(self.cfg)
        self.painel = SimpleNamespace(cfg=self.cfg, motor=self.motor, gravacoes={}, logins={}, reinicio={},
                                      encerrar=threading.Event(), conta_canais=None, trava_config=threading.RLock(),
                                      conectar=lambda: db.conectar(self.cfg.banco))
        self.sinais = SINAIS["pessoal"]
        self.troca_funciona = True
        self.trocas = 0
        _NavegadorDoPainel.falha = None

        def ler(aba, cfg):
            ck, me = self.sinais
            return pagina_facebook.decidir(ck, me, cfg["facebook"]), ck

        def trocar(aba, cfg, conferir_bloqueio=None):
            self.trocas += 1
            if self.troca_funciona:
                self.sinais = SINAIS["pagina"]

        for troca in (patch.object(plat_nav, "Navegador", _NavegadorDoPainel),
                      patch.object(plat_nav, "esperar_sessao", lambda aba, rede, segundos=30: None),
                      patch.object(pagina_facebook, "_ler", ler),
                      patch.object(pagina_facebook, "trocar", trocar)):
            troca.start()
            self.addCleanup(troca.stop)
        self.registro = _Registro()
        logger = logging.getLogger("autocortes")
        nivel = logger.level
        logger.addHandler(self.registro)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, nivel)
        self.addCleanup(logger.removeHandler, self.registro)
        self.addCleanup(self._fechar_gravacoes)

    def _fechar_gravacoes(self):
        for sessao in self.painel.gravacoes.values():
            sessao.cancelar()

    # ---- ajudantes
    def ctx(self, corpo=None):
        from types import SimpleNamespace
        return SimpleNamespace(cfg=self.cfg, conn=self.conn, painel=self.painel, corpo=corpo or {}, consulta={},
                               tratador=None)

    def erro(self, funcao, *args, corpo=None):
        with self.assertRaises(self.api.ErroHttp) as erro:
            funcao(self.ctx(corpo), *args)
        return erro.exception

    def perfil(self, **corpo) -> dict:
        return self.api.perfil_facebook(self.ctx(corpo))

    def conferencia(self) -> dict:
        return (plat_nav.ViaNavegador("facebook", self.cfg).token().get("paginas") or {}).get("facebook") or {}

    def no_arquivo(self) -> dict:
        return config.carregar(self.caminho)["facebook"]

    def assertSemCUser(self, *extras):
        textos = [json.dumps(plat_nav.ViaNavegador("facebook", self.cfg).token(), ensure_ascii=False),
                  *self.registro.linhas, *(json.dumps(x, ensure_ascii=False, default=str) for x in extras)]
        textos += [str(v) for (v,) in self.conn.execute("SELECT valor FROM estado").fetchall()]
        for texto in textos:
            self.assertNotIn(C_USER, texto)

    # ---- rotas registradas
    def test_rotas(self):
        api = self.api
        self.assertIs(api.encontrar("POST", "/redes/facebook/perfil")[0], api.perfil_facebook)
        self.assertIs(api.encontrar("POST", "/redes/facebook/pagina")[0], api.pagina_upload_post)
        self.assertEqual(api.encontrar("POST", "/redes/facebook/conta")[:2], (api.escolher_conta, ("facebook",)))
        self.assertEqual(api.encontrar("POST", "/redes/instagram/conta")[:2], (api.escolher_conta, ("instagram",)))
        self.assertIsNone(api.encontrar("POST", "/redes/youtube/conta"))
        texto = Path(api.__file__).read_text(encoding="utf-8")
        self.assertEqual(texto.count("@rota("), 88)

    # ---- critério 14: gravação
    def test_14_gravacao(self):
        e = self.erro(self.api.iniciar_gravacao, "facebook")
        self.assertEqual(e.status, 409)
        self.assertIn("Troque para a Página", str(e))
        self.assertEqual(e.extra["agindo_como"], "pessoal")
        self.assertNotIn("facebook", self.painel.gravacoes)
        self.assertEqual(plat_nav.gravando(), [])
        self.assertSemCUser(str(e), e.extra)
        _NavegadorDoPainel.falha = "a janela do navegador não abriu"
        e = self.erro(self.api.iniciar_gravacao, "facebook")
        self.assertEqual(e.status, 400)
        self.assertIn("não abriu", str(e))
        _NavegadorDoPainel.falha = None
        self.sinais = SINAIS["pagina"]
        resposta = self.api.iniciar_gravacao(self.ctx(), "facebook")
        self.assertTrue(resposta["ok"])
        self.assertIn("facebook", self.painel.gravacoes)
        self.assertEqual(self.trocas, 0)  # na gravação quem troca é você

    # ---- critério 15 e 15a: Ver quem está ativo e Trocar para a Página
    def test_15_perfil_mostra_quem_esta_ativo_e_troca(self):
        for quem, numero in (("pessoal", None), ("outra", OUTRA), ("pagina", PAGINA)):
            self.sinais = SINAIS[quem]
            r = self.perfil(trocar=False)
            self.assertEqual((r["agindo_como"], r["id"], r["confere"]), (quem, numero, quem == "pagina"))
            self.assertEqual(self.conferencia()["agindo_como"], quem)
        self.assertEqual(r["nome"], "Página Certa")
        self.assertEqual(self.trocas, 0)
        self.sinais = SINAIS["pessoal"]
        r = self.perfil(trocar=True)
        self.assertEqual(self.trocas, 1)
        self.assertTrue(r["confere"])
        self.assertEqual(r["agindo_como"], "pagina")
        self.assertTrue(r["pagina"]["confirmada"])
        ultima = self.api._resumo_redes(self.ctx())["facebook"]["pagina"]["ultima"]
        self.assertEqual((ultima["agindo_como"], ultima["id"], ultima["confere"]), ("pagina", PAGINA, True))
        self.assertSemCUser(r)

    def test_15a_privacidade_no_perfil_pessoal(self):
        self.troca_funciona = False
        r = self.perfil(trocar=True)
        self.assertFalse(r["confere"])
        self.assertEqual(r["agindo_como"], "pessoal")
        self.assertIsNone(r["id"])
        e = self.erro(self.api.testar_rede, "facebook")
        self.assertEqual(e.status, 400)
        self.assertIn("o seu perfil pessoal", str(e))
        redes = self.api._resumo_redes(self.ctx())
        self.assertSemCUser(r, str(e), e.extra, redes)

    def test_perfil_recusa(self):
        self.assertEqual(self.erro(self.api.perfil_facebook, corpo={"usar": "abc"}).status, 400)
        self.assertEqual(self.erro(self.api.perfil_facebook, corpo={"trocar": "sim"}).status, 400)
        self.painel.gravacoes["tiktok"] = SimpleGravacao()
        self.assertEqual(self.erro(self.api.perfil_facebook).status, 409)
        self.painel.gravacoes.clear()
        with patch.object(plat_nav, "esperar_sessao", side_effect=navegador.ErroNavegador("sem sessão")):
            e = self.erro(self.api.perfil_facebook)
        self.assertEqual((e.status, str(e)), (400, "sem sessão"))
        presa = threading.Lock()
        presa.acquire()
        with patch.object(self.api, "TRAVA_NAVEGADOR", presa):
            self.assertEqual(self.erro(self.api.perfil_facebook).status, 409)
        self.cfg["facebook"]["envio"] = "upload_post"
        self.assertEqual(self.erro(self.api.perfil_facebook).status, 400)

    # ---- critério 15b: entrada só com o nome e entrada com link
    def test_15b_nome_pede_confirmacao(self):
        self.cfg["facebook"].update(pagina="Página Certa", pagina_id="", pagina_nome="")
        self.sinais = SINAIS["pagina"]
        e = self.erro(self.api.testar_rede, "facebook")
        self.assertEqual(e.status, 400)
        self.assertEqual(e.extra["sugestao"], {"pagina_id": PAGINA, "pagina_nome": "Página Certa"})
        self.assertEqual(self.cfg["facebook"]["pagina_id"], "")
        r = self.perfil()
        self.assertEqual(r["sugestao"]["pagina_id"], PAGINA)
        self.assertFalse(r["confere"])
        self.assertEqual(self.cfg["facebook"]["pagina_id"], "")
        self.sinais = SINAIS["outra"]  # a Página ativa mudou entre a pergunta e o clique
        self.assertEqual(self.erro(self.api.perfil_facebook, corpo={"usar": PAGINA}).status, 409)
        self.assertEqual(self.cfg["facebook"]["pagina_id"], "")
        self.sinais = SINAIS["pagina"]
        r = self.perfil(usar=PAGINA)
        self.assertTrue(r["confere"])
        self.assertIsNone(r["sugestao"])
        self.assertEqual((self.cfg["facebook"]["pagina_id"], self.cfg["facebook"]["pagina_nome"]),
                         (PAGINA, "Página Certa"))
        self.assertEqual(self.no_arquivo()["pagina_id"], PAGINA)
        self.assertEqual(self.cfg["facebook"]["pagina"], "Página Certa")  # o nome que você digitou fica
        self.assertTrue(self.conferencia()["confere"])
        self.assertIn(("facebook", True), self.motor.atualizadas)

    def test_15b_link_aprende_o_id(self):
        self.cfg["facebook"].update(pagina=LINK_PAGINA, pagina_id="", pagina_nome="")
        self.sinais = SINAIS["pagina"]
        conta = self.api.testar_rede(self.ctx(), "facebook")["conta"]
        self.assertIn(PAGINA, conta)
        self.assertEqual(self.cfg["facebook"]["pagina_id"], PAGINA)
        guardado = self.no_arquivo()
        self.assertEqual((guardado["pagina_id"], guardado["pagina_nome"]), (PAGINA, "Página Certa"))
        self.assertEqual(self.cfg["facebook"]["pagina"], LINK_PAGINA)  # o link que você informou fica

    # ---- critério 15c e R7: a pausa persistente só sai com a Página confirmada
    def test_15c_pausa_so_sai_com_a_pagina(self):
        publicador.bloquear(self.conn, "facebook", "não consegui confirmar que estou na Página", "bloqueio", None)
        self.troca_funciona = False
        e = self.erro(self.api.testar_rede, "facebook")
        self.assertEqual(e.status, 400)
        self.assertIn("facebook", publicador.ler_bloqueios(self.conn))
        self.assertNotIn(("facebook", True), self.motor.atualizadas)
        r = self.perfil(trocar=False)
        self.assertFalse(r["confere"])
        self.assertIn("facebook", publicador.ler_bloqueios(self.conn))
        self.troca_funciona = True
        r = self.perfil(trocar=True)
        self.assertTrue(r["confere"])
        self.assertIn(("facebook", True), self.motor.atualizadas)
        self.assertNotIn("facebook", publicador.ler_bloqueios(self.conn))

    # ---- critério 16: API oficial pelas rotas de conectar
    def test_16_conectar_pela_api_oficial(self):
        srv = _Servidor(_graph)
        self.addCleanup(srv.fechar)
        self.cfg["facebook"].update(envio="oficial", app_id="123", app_secret="segredo", pagina="", pagina_id="",
                                    pagina_nome="")
        self.cfg.pasta_tokens.mkdir(parents=True, exist_ok=True)
        with patch.object(Facebook, "graph_url", srv.url):
            r = self.api.iniciar_login(self.ctx({"token": "TOKEN-CURTO"}), "facebook")
            self.assertEqual(r["estado"], "escolher")
            self.assertEqual(r["contas"], [{"indice": 0, "pagina": "Cortes"}, {"indice": 1, "pagina": "Outra"}])
            r2 = self.api.escolher_conta(self.ctx({"indice": 1}), "facebook")
            redes = self.api._resumo_redes(self.ctx())
        self.assertEqual(r2["estado"], "ok")
        for texto in (json.dumps(r), json.dumps(r2), json.dumps(redes, default=str)):
            self.assertNotIn("TOKEN", texto)
        self.assertEqual(ler_token(self.cfg.pasta_tokens / "facebook.json")["pagina_id"], "333")
        guardado = self.no_arquivo()
        self.assertEqual((guardado["pagina_id"], guardado["pagina_nome"], guardado["pagina"]), ("333", "Outra", "Outra"))
        pagina = redes["facebook"]["pagina"]
        self.assertEqual((pagina["id"], pagina["confirmada"], pagina["so_leitura"]), ("333", True, True))
        self.assertTrue(redes["facebook"]["pronta"])
        self.assertIn(("facebook", True), self.motor.atualizadas)

    def test_16_login_herdado_do_instagram(self):
        self.cfg["facebook"].update(envio="oficial", pagina="", pagina_id="", pagina_nome="")
        self.cfg.pasta_tokens.mkdir(parents=True, exist_ok=True)
        salvar_token(self.cfg.pasta_tokens / "instagram.json",
                     {"access_token": "TOKEN-IG", "pagina_id": "444", "pagina_nome": "Página do Instagram"})
        pagina = self.api._resumo_redes(self.ctx())["facebook"]["pagina"]
        self.assertEqual((pagina["id"], pagina["nome"], pagina["origem"]), ("444", "Página do Instagram",
                                                                             "login do Instagram"))
        self.assertNotIn("TOKEN", json.dumps(pagina))

    # ---- critério 17: Upload-Post pela rota da Página
    def test_17_escolher_a_pagina_do_upload_post(self):
        def responder(pedido):
            if pedido["caminho"] == "/api/uploadposts/facebook/pages":
                return 200, {"success": True, "pages": [{"id": "111", "name": "Cortes"}, {"id": "333", "name": "Outra"}]}
            return 404, {"message": "não existe"}

        srv = _Servidor(responder)
        self.addCleanup(srv.fechar)
        self.cfg["upload_post"].update(api_key="CHAVE-UP", perfil="meuperfil")
        self.cfg["facebook"].update(envio="upload_post", pagina="", pagina_id="", pagina_nome="")
        from autocortes.plataformas import upload_post
        with patch.object(upload_post, "API", srv.url):
            r = self.api.pagina_upload_post(self.ctx())
            self.assertEqual(r["paginas"], [{"id": "111", "nome": "Cortes"}, {"id": "333", "nome": "Outra"}])
            self.assertEqual(self.erro(self.api.pagina_upload_post, corpo={"id": "999"}).status, 400)
            self.assertEqual(self.erro(self.api.pagina_upload_post, corpo={"id": "x1"}).status, 400)
            r = self.api.pagina_upload_post(self.ctx({"id": "111"}))
        self.assertEqual(r["atual"], "111")
        guardado = self.no_arquivo()
        self.assertEqual((guardado["pagina_id"], guardado["pagina_nome"], guardado["pagina"]), ("111", "Cortes", "Cortes"))
        self.assertIn(("facebook", True), self.motor.atualizadas)
        self.cfg["facebook"]["envio"] = "navegador"
        self.assertEqual(self.erro(self.api.pagina_upload_post).status, 400)


class SimpleGravacao:
    """Uma gravação de outra rede aberta no painel."""

    def cancelar(self):
        pass


if __name__ == "__main__":
    unittest.main()
