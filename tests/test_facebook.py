"""Página do Facebook como rede própria: config, migração, agenda, plataformas, fila e banco.

Nada aqui abre o Facebook de verdade: a API oficial e o Upload-Post são servidores HTTP locais.
"""

import copy
import http.server
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from autocortes import agenda, config
from autocortes.plataformas import facebook as plat_fb
from autocortes.plataformas import manual
from autocortes.plataformas.base import Entrega, ErroPublicacao
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
        cfg = self.carregar('[facebook]\npagina = "https://www.facebook.com/people/Cortes/111/"\npagina_id = "111"\n')
        self.assertEqual(config.id_do_link_facebook(cfg["facebook"]["pagina"]), "111")
        self.carregar('[facebook]\npagina = "Cortes de Filmes"\n')


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


if __name__ == "__main__":
    unittest.main()
