"""Regressões do gravador; os testes de navegador usam um perfil temporário, sem contas."""

import copy
import json
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import quote

import requests

from autocortes import gravador, roteiro
from autocortes.config import Config, PADRAO
from autocortes.navegador import Aba, ErroNavegador, _Websocket, caminho_programa
from autocortes.plataformas.navegador import CRIAR_INSTAGRAM, SESSAO, ViaNavegador, _quadro_de_envio, esperar_sessao


PAGINA = """<!doctype html><html><body>
<input type="file" id="video">
<ytcp-social-suggestions-textbox id="title-textarea"><ytcp-form-input-container id="container">
  <div id="outer"><div id="child-input"><div id="container-content">
    <ytcp-social-suggestion-input id="input"><div id="textbox" contenteditable="true" role="textbox"
      aria-label="Adicione um título que descreva o conteúdo do vídeo (insira uma @ para mencionar um canal)"></div></ytcp-social-suggestion-input>
  </div></div></div>
</ytcp-form-input-container></ytcp-social-suggestions-textbox>
<ytcp-social-suggestions-textbox id="description-textarea"><ytcp-form-input-container id="container">
  <div id="outer"><div id="child-input"><div id="container-content">
    <ytcp-social-suggestion-input id="input"><div id="textbox" contenteditable="true" role="textbox"
      aria-label="Fale sobre seu vídeo para os espectadores (mencione canais usando @)"></div></ytcp-social-suggestion-input>
  </div></div></div>
</ytcp-form-input-container></ytcp-social-suggestions-textbox>
<section id="outro-campo"><input id="text-input" type="text"></section>
<section id="tags-container"><input id="text-input" type="text"></section>
<button id="done-button">Publicar</button>
</body></html>
"""


class TestMarcas(unittest.TestCase):
    def test_marcas_do_youtube(self):
        for papel in ("titulo", "descricao", "tags"):
            with self.subTest(papel=papel):
                self.assertEqual(gravador._papel_do_valor(roteiro.MARCAS[papel]), (papel, []))

    def test_gravacao_sem_video_preserva_o_roteiro_anterior(self):
        with tempfile.TemporaryDirectory() as pasta:
            cfg = Config(copy.deepcopy(PADRAO), Path(pasta) / "config.toml")
            gravador.gravar_roteiro(cfg, "youtube", "# roteiro anterior\n")
            resultado = gravador.salvar(cfg, "youtube", [], "https://studio.youtube.com")
            self.assertFalse(resultado["ok"])
            self.assertEqual(gravador.carregar(cfg, "youtube"), "# roteiro anterior\n")

    def salvar_passos(self, campos):
        passos = [{"tipo": "arquivo", "seletores": ["#video"], "em": 1}]
        passos += [{"tipo": "digitar", "seletores": [seletor], "valor": valor, "em": n}
                   for n, (seletor, valor) in enumerate(campos, 2)]
        passos.append({"tipo": "clicar", "seletores": ["#done-button"], "texto": "Publicar", "em": 10})
        with tempfile.TemporaryDirectory() as pasta:
            cfg = Config(copy.deepcopy(PADRAO), Path(pasta) / "config.toml")
            gravador.gravar_roteiro(cfg, "youtube", "# roteiro anterior\n")
            resultado = gravador.salvar(cfg, "youtube", passos, "https://studio.youtube.com")
            self.assertTrue(resultado["ok"], resultado)
            salvo = gravador.carregar(cfg, "youtube")
            self.assertEqual(salvo, resultado["texto"])
            instrucoes = roteiro.validar(salvo, "youtube")
            return resultado, instrucoes

    def test_salva_sem_nenhuma_marca_e_preserva_textos(self):
        resultado, instrucoes = self.salvar_passos([
            ("#title-textarea #textbox", "Título digitado à mão"),
            ("#description-textarea #textbox", "Descrição digitada à mão"),
        ])
        self.assertEqual(resultado["faltando"], ["titulo", "descricao", "tags"])
        self.assertEqual([i.texto for i in instrucoes if i.comando == "escrever"],
                         ["Título digitado à mão", "Descrição digitada à mão"])
        self.assertTrue(any("título" in a for a in resultado["resumo"]["avisos"]))
        self.assertEqual(resultado["resumo"]["erros"], [])

    def test_salva_quando_faltam_titulo_e_tags(self):
        resultado, instrucoes = self.salvar_passos([("#description-textarea #textbox", "@@DESCRICAO@@")])
        self.assertEqual(resultado["faltando"], ["titulo", "tags"])
        self.assertEqual([i.papel for i in instrucoes if i.papel], ["descricao"])
        self.assertEqual(resultado["resumo"]["erros"], [])

    def test_salva_marca_desconhecida_como_texto_fixo(self):
        resultado, instrucoes = self.salvar_passos([("#title-textarea #textbox", "@@XYZ@@")])
        self.assertEqual(resultado["estranhas"], ["@@XYZ@@"])
        self.assertEqual([i.texto for i in instrucoes if i.comando == "escrever"], ["@@XYZ@@"])
        self.assertTrue(any("marca" in a for a in resultado["resumo"]["avisos"]))

    def test_sem_campos_de_texto_continua_executavel_em_cada_rede(self):
        for rede in roteiro.PAPEIS_DA_REDE:
            with self.subTest(rede=rede):
                # o roteiro de imagem ("<rede>-imagem") entrega o arquivo com "imagens"
                arquivo = "imagens" if roteiro.midia_do_roteiro(rede) == "imagem" else "video"
                texto = f"abrir https://example.com\n{arquivo} em #video\npublicar #done\n"
                roteiro.validar(texto, rede)
                resumo = roteiro.resumo(texto, rede)
                self.assertEqual(resumo["erros"], [])
                self.assertTrue(any("preenchimento automático" in a for a in resumo["avisos"]))


class TestQuadrosYoutube(unittest.TestCase):
    def test_cookies_que_citam_o_studio_nao_sao_uploader(self):
        self.assertFalse(_quadro_de_envio(
            "https://accounts.youtube.com/RotateCookiesPage?origin=https://studio.youtube.com&yt_pid=62"))

    def test_quadros_de_envio_continuam_detectados(self):
        for url in ("https://example.com/upload/video", "https://studio.example.com/frame",
                    "https://example.com/creator/upload?modo=video"):
            with self.subTest(url=url):
                self.assertTrue(_quadro_de_envio(url))

    def test_quadros_de_suporte_e_enderecos_invalidos(self):
        for url in ("https://realtimesupport.youtube.com/static/proxy.html",
                    "https://www.youtube.com/persist_identity", "/ytscframe",
                    "javascript:studio", "https://[invalido/upload"):
            with self.subTest(url=url):
                self.assertFalse(_quadro_de_envio(url))


class TestGravadorNoNavegador(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pasta = tempfile.TemporaryDirectory(prefix="autocortes-teste-gravador-")
        cls.addClassCleanup(cls.pasta.cleanup)
        cfg = Config(copy.deepcopy(PADRAO), Path(cls.pasta.name) / "config.toml")
        try:
            programa = caminho_programa(cfg)
        except ErroNavegador as erro:
            raise unittest.SkipTest(str(erro)) from erro
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            porta = sock.getsockname()[1]
        cls.base = f"http://127.0.0.1:{porta}"
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
                paginas = requests.get(f"{cls.base}/json/list", timeout=1).json()
                pagina = next(p for p in paginas if p["type"] == "page")
                cls.aba = Aba(_Websocket(pagina["webSocketDebuggerUrl"], timeout=10), ritmo=(0, 0))
                cls.addClassCleanup(cls.aba._ws.fechar)
                return
            except (requests.RequestException, StopIteration):
                time.sleep(0.1)
        raise RuntimeError("O navegador de teste não abriu")

    @classmethod
    def _encerrar(cls):
        try:
            url = requests.get(f"{cls.base}/json/version", timeout=2).json()["webSocketDebuggerUrl"]
            ws = _Websocket(url, timeout=2)
            try:
                ws.enviar(json.dumps({"id": 1, "method": "Browser.close"}))
            finally:
                ws.fechar()
            cls.processo.wait(timeout=5)
        except (requests.RequestException, ErroNavegador, subprocess.TimeoutExpired):
            cls.processo.terminate()
            cls.processo.wait(timeout=5)

    def setUp(self):
        self.aba.navegar("data:text/html;charset=utf-8," + quote(PAGINA))
        self.aba.avaliar(gravador.SCRIPT)

    def js(self, corpo):
        return self.aba.avaliar("(() => { " + corpo + " })()")

    def preencher(self, seletor, texto, evento=True):
        self.js(f"const el = document.querySelector({json.dumps(seletor)}); "
                f"if (el.isContentEditable) el.textContent = {json.dumps(texto)}; "
                f"else el.value = {json.dumps(texto)}; "
                + ('el.dispatchEvent(new Event("input", {bubbles: true, composed: true}));' if evento else ""))

    def papeis(self, passos=None):
        if passos is None:
            passos = self.aba.avaliar("window.__acGrav.tirar()")
        return {p.get("papel") for p in gravador.limpar_passos("youtube", passos) if p.get("papel")}

    def test_titulo_e_descricao_com_o_mesmo_id(self):
        self.preencher("#title-textarea #textbox", "@@TITULO@@")
        self.preencher("#description-textarea #textbox", "@@DESCRICAO@@")
        passos = self.aba.avaliar("window.__acGrav.tirar()")
        self.assertEqual(self.papeis(passos), {"titulo", "descricao"})
        for passo in passos:
            self.assertNotIn("#textbox", passo["seletores"])
            self.assertNotIn("#input #textbox", passo["seletores"])
            self.assertEqual(len(self.aba.avaliar(
                "[...document.querySelectorAll(" + json.dumps(passo["seletores"][0]) + ")]")), 1)

    def test_marcas_ja_coladas_antes_da_injecao(self):
        pagina = PAGINA.replace('aria-label="Adicione um título que descreva o conteúdo do vídeo (insira uma @ para mencionar um canal)"></div>',
                                'aria-label="Título">@@TITULO@@</div>')
        self.aba.navegar("data:text/html;charset=utf-8," + quote(pagina))
        self.aba.avaliar(gravador.SCRIPT)
        self.assertIn("titulo", self.papeis())

    def test_tags_e_outro_input_com_o_mesmo_id(self):
        self.preencher("#tags-container input", "@@TAGS@@", evento=False)
        self.aba.avaliar("window.__acGrav.varrer()")
        self.preencher("#outro-campo input", "outro valor", evento=False)
        self.aba.avaliar("window.__acGrav.varrer()")
        self.assertIn("tags", self.papeis())

    def test_marca_preservada_apos_texto_real(self):
        self.preencher("#title-textarea #textbox", "@@TITULO@@")
        self.preencher("#title-textarea #textbox", "Título do vídeo de teste")
        self.assertIn("titulo", self.papeis())

    def test_tags_capturadas_antes_do_enter_limpar_o_campo(self):
        self.preencher("#tags-container input", "@@TAGS@@", evento=False)
        self.js('const el = document.querySelector("#tags-container input"); '
                'el.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true, composed: true})); '
                'el.value = "";')
        self.assertIn("tags", self.papeis())

    def test_colagem_preservada_antes_do_editor_transformar_o_texto(self):
        self.js('const el = document.querySelector("#tags-container input"); '
                'const dados = new DataTransfer(); dados.setData("text/plain", "@@TAGS@@"); '
                'el.dispatchEvent(new ClipboardEvent("paste", {clipboardData: dados, bubbles: true, composed: true}));')
        self.assertIn("tags", self.papeis())

    def test_evento_e_varredura_nao_duplicam_nem_reordenam_texto(self):
        self.preencher("#title-textarea #textbox", "@@TITULO@@")
        self.js('document.querySelector("#done-button").click(); window.__acGrav.varrer();')
        passos = self.aba.avaliar("window.__acGrav.tirar()")
        self.assertEqual([p["tipo"] for p in passos], ["digitar", "clicar"])

    def test_camadas_visuais_gravam_o_botao_com_nome_e_ancora(self):
        self.js('document.body.insertAdjacentHTML("beforeend", `'
                '<ytcp-button id="next-button"><ytcp-button-shape><button>Próximo'
                '<yt-touch-feedback-shape><div></div><div></div></yt-touch-feedback-shape>'
                '</button></ytcp-button-shape></ytcp-button>'
                '<ytcp-button id="publish-button"><ytcp-button-shape><button>Publicar'
                '<yt-touch-feedback-shape><div></div><div></div></yt-touch-feedback-shape>'
                '</button></ytcp-button-shape></ytcp-button>`); '
                'for (const id of ["next-button", "publish-button"]) '
                'document.querySelector("#" + id + " yt-touch-feedback-shape div:last-child").click();')
        passos = self.aba.avaliar("window.__acGrav.tirar()")
        self.assertEqual([p["texto"] for p in passos], ["Próximo", "Publicar"])
        self.assertEqual([p["tag"] for p in passos], ["button", "button"])
        self.assertEqual(passos[0]["seletores"][0], "#next-button button")
        self.assertEqual(passos[1]["seletores"][0], "#publish-button button")
        for passo in passos:
            self.assertFalse(any("nth-of-type" in s or "feedback" in s for s in passo["seletores"]))
        self.assertFalse(gravador._parece_publicar(passos[0]))
        self.assertTrue(gravador._parece_publicar(passos[1]))

    def test_tres_cliques_no_mesmo_proximo_sao_tres_passos_no_roteiro(self):
        self.js('document.body.insertAdjacentHTML("beforeend", \'<button id="next-button">Próximo</button>\'); '
                'document.querySelector("#video").dispatchEvent(new Event("change", {bubbles: true})); '
                'for (let n = 0; n < 3; n++) document.querySelector("#next-button").click(); '
                'document.querySelector("#done-button").click();')
        passos = self.aba.avaliar("window.__acGrav.tirar()")
        analise = gravador.analisar("youtube", passos)
        self.assertEqual(len(analise["passos_limpos"]), 5)
        self.assertEqual(analise["publicar_em"], 4)
        self.assertFalse(analise["publicar_incerto"])
        texto = roteiro.gerar("youtube", analise["passos_limpos"], "https://studio.youtube.com",
                              publicar_em=analise["publicar_em"])
        instrucoes = roteiro.validar(texto, "youtube")
        self.assertEqual(sum(i.comando == "clicar" for i in instrucoes), 3)
        self.assertEqual(sum(i.comando == "publicar" for i in instrucoes), 1)

    def test_icone_em_shadow_dom_identifica_botao_no_host(self):
        self.js('const botao = document.createElement("button"); botao.id = "upload-button"; '
                'botao.setAttribute("aria-label", "Enviar vídeos"); document.body.append(botao); '
                'const host = document.createElement("span"); botao.append(host); '
                'const root = host.attachShadow({mode: "open"}); root.innerHTML = "<div>↑</div>"; '
                'root.querySelector("div").click();')
        passos = self.aba.avaliar("window.__acGrav.tirar()")
        self.assertEqual(len(passos), 1)
        self.assertIn("#upload-button", passos[0]["seletores"])
        self.assertEqual(passos[0]["rotulo"], "Enviar vídeos")
        self.assertEqual(passos[0]["tag"], "button")

    def test_clique_em_texto_do_editor_preserva_o_campo(self):
        self.preencher("#title-textarea #textbox", "@@TITULO@@")
        self.js('const campo = document.querySelector("#title-textarea #textbox"); '
                'campo.innerHTML = "<span>@@TITULO@@</span>"; campo.querySelector("span").click();')
        passos = self.aba.avaliar("window.__acGrav.tirar()")
        clique = next(p for p in passos if p["tipo"] == "clicar")
        self.assertEqual(clique["seletores"][0], "#title-textarea #textbox")
        self.assertEqual(clique["texto"], "")

    def test_contenteditable_plaintext_only(self):
        self.js('document.querySelector("#title-textarea #textbox").setAttribute("contenteditable", "plaintext-only");')
        self.preencher("#title-textarea #textbox", "@@TITULO@@", evento=False)
        self.assertIn("titulo", self.papeis())

    def test_campos_em_shadow_dom(self):
        self.js('const host = document.createElement("div"); document.body.append(host); '
                'const root = host.attachShadow({mode: "open"}); '
                'root.innerHTML = \'<section id="campo-sombra"><div id="textbox" contenteditable="true"></div></section>\'; '
                'const el = root.querySelector("#textbox"); el.textContent = "@@TITULO@@"; '
                'el.dispatchEvent(new Event("input", {bubbles: true, composed: true}));')
        self.assertIn("titulo", self.papeis())

    def test_nenhum_texto_em_tela_com_senha(self):
        self.js('const senha = document.createElement("input"); senha.type = "password"; document.body.append(senha);')
        self.preencher("#title-textarea #textbox", "@@TITULO@@")
        self.assertEqual(self.aba.avaliar("window.__acGrav.tirar()"), [])

    def test_sessao_instagram_e_clique_com_menu_compacto(self):
        for nome in ("Novo post", "New Post"):
            with self.subTest(nome=nome):
                pagina = '<a href="#" role="link" onclick="document.querySelector(\'#envio\').hidden=false; return false;">'
                pagina += f'<div><svg aria-label="{nome}" width="24" height="24"><title>{nome}</title></svg></div>'
                pagina += '<span style="display:none">Criar</span></a><section id="envio" hidden>Novo envio</section>'
                self.aba.navegar("data:text/html;charset=utf-8," + quote(pagina))
                self.assertEqual(self.aba.js(SESSAO["instagram"]), 1)
                esperar_sessao(self.aba, "instagram", segundos=1)
                self.assertTrue(self.aba.avaliar('document.querySelector("#envio").hidden'))
                self.aba.clicar_alvos(CRIAR_INSTAGRAM, segundos=1)
                self.assertFalse(self.aba.avaliar('document.querySelector("#envio").hidden'))

    def test_sessao_instagram_com_botao_criar_visivel(self):
        self.aba.navegar("data:text/html;charset=utf-8," + quote('<button>Criar</button>'))
        self.assertEqual(self.aba.js(SESSAO["instagram"]), 1)

    def test_roteiro_instagram_clicar_criar_com_texto_escondido(self):
        pagina = '''<a href="#" role="link" onclick="window.abriu=true; return false;">
          <svg aria-label="Novo post" width="24" height="24"><title>Novo post</title></svg>
          <span style="display:none">Criar</span></a>'''
        self.aba.navegar("data:text/html;charset=utf-8," + quote(pagina))
        cfg = Config(copy.deepcopy(PADRAO), Path(self.pasta.name) / "config.toml")
        ins = roteiro.validar('clicar "Criar"\nvideo\npublicar "Compartilhar"', "instagram")[0]
        ViaNavegador("instagram", cfg)._rodar(self.aba, ins, Path("."), {})
        self.assertTrue(self.aba.avaliar("!!window.abriu"))

    def test_gravacao_instagram_preserva_nome_do_icone_no_seletor(self):
        self.js('document.body.insertAdjacentHTML("beforeend", `'
                '<a href="#" role="link"><svg aria-label="Novo post" width="24" height="24">'
                '<title>Novo post</title></svg><span style="display:none">Criar</span></a>`); '
                'document.querySelector("a svg").dispatchEvent(new MouseEvent("click", {bubbles:true,composed:true}));')
        passo = next(p for p in self.aba.avaliar("window.__acGrav.tirar()") if p["tipo"] == "clicar")
        self.assertIn('a:has(svg[aria-label="Novo\\ post"])', passo["seletores"])
        self.assertFalse(gravador.fragil(passo))
        self.assertIn(":has(svg", roteiro._alvos_de(passo))

    def test_clique_por_nome_de_icone_svg_aciona_o_botao(self):
        pagina = '''<button onclick="window.cortou=true"><svg aria-label="Selecionar corte" width="24" height="24">
          <title>Selecionar corte</title></svg></button>'''
        self.aba.navegar("data:text/html;charset=utf-8," + quote(pagina))
        self.aba.clicar_alvos([{"v": "Selecionar corte", "t": True}], segundos=1)
        self.assertTrue(self.aba.avaliar("!!window.cortou"))

    def test_icone_svg_de_botao_desabilitado_nao_e_clicado(self):
        pagina = '''<button disabled onclick="window.cortou=true"><svg aria-label="Selecionar corte" width="24" height="24">
          <title>Selecionar corte</title></svg></button>'''
        self.aba.navegar("data:text/html;charset=utf-8," + quote(pagina))
        with self.assertRaisesRegex(ErroNavegador, "desabilitado"):
            self.aba.clicar_alvos([{"v": "Selecionar corte", "t": True}], segundos=.2)
        self.assertFalse(self.aba.avaliar("!!window.cortou"))

    def test_instagram_abre_envio_pelo_submenu_postar(self):
        pagina = '''<a href="#" onclick="document.querySelector('#menu').hidden=false; return false;">
          <svg aria-label="Novo post" width="24" height="24"><title>Novo post</title></svg>
          <span style="display:none">Criar</span></a>
        <div id="menu" hidden>
          <a href="#" onclick="document.body.insertAdjacentHTML('beforeend', '<input type=file accept=video/mp4>'); return false;">
            Postar<svg aria-label="Postar" width="24" height="24"><title>Postar</title></svg></a>
          <a href="#" onclick="window.errou=true">Vídeo ao vivo</a>
        </div>'''
        self.aba.navegar("data:text/html;charset=utf-8," + quote(pagina))
        ViaNavegador._abrir_envio_instagram(self.aba, segundos=1)
        self.assertTrue(self.aba.existe("input[type=file][accept='video/mp4']"))
        self.assertFalse(self.aba.avaliar("!!window.errou"))

    def test_instagram_abre_envio_direto_no_layout_anterior(self):
        pagina = '''<button onclick="document.body.insertAdjacentHTML('beforeend', '<input type=file accept=video/mp4>')">Criar</button>'''
        self.aba.navegar("data:text/html;charset=utf-8," + quote(pagina))
        ViaNavegador._abrir_envio_instagram(self.aba, segundos=1)
        self.assertTrue(self.aba.existe("input[type=file][accept='video/mp4']"))

    def test_instagram_nao_reconhece_post_do_feed_ou_icone_escondido(self):
        for pagina in ('<article><span>Novo post</span><span>Criar</span></article>',
                       '<a href="#" hidden><svg aria-label="Novo post" width="24" height="24"></svg></a>'):
            with self.subTest(pagina=pagina):
                self.aba.navegar("data:text/html;charset=utf-8," + quote(pagina))
                self.assertEqual(self.aba.js(SESSAO["instagram"]), 0)

    def test_instagram_recusa_pagina_de_login_mesmo_com_botao_criar(self):
        self.aba.navegar("data:text/html;charset=utf-8," + quote('<input type="password"><button>Criar</button>'))
        with self.assertRaisesRegex(ErroNavegador, "não encontrei a sua sessão do Instagram"):
            esperar_sessao(self.aba, "instagram", segundos=1)

    def test_salvar_e_reexecutar_campos_distintos(self):
        self.js('document.querySelector("#video").dispatchEvent(new Event("change", {bubbles: true, composed: true}));')
        self.preencher("#title-textarea #textbox", "@@TITULO@@")
        self.preencher("#description-textarea #textbox", "@@DESCRICAO@@")
        self.preencher("#tags-container input", "@@TAGS@@")
        self.js('document.querySelector("#done-button").click();')
        with tempfile.TemporaryDirectory() as pasta:
            cfg = Config(copy.deepcopy(PADRAO), Path(pasta) / "config.toml")
            passos = self.aba.avaliar("window.__acGrav.tirar()")
            resultado = gravador.salvar(cfg, "youtube", passos, "https://studio.youtube.com")
            self.assertTrue(resultado["ok"], resultado)
            self.assertEqual(resultado["papeis"], ["descricao", "tags", "titulo"])
            instrucoes = roteiro.validar(gravador.carregar(cfg, "youtube"), "youtube")
            self.aba.avaliar("window.__acGrav.parar()")
            for ins in instrucoes:
                if ins.papel:
                    alvos = [{"v": a.valor, "t": a.por_texto, "fraco": roteiro.fraco(a.valor)} for a in ins.alvos]
                    self.aba.digitar_alvos(alvos, "Texto de " + ins.papel, segundos=2)
            self.assertEqual(self.aba.avaliar('document.querySelector("#title-textarea #textbox").innerText'), "Texto de titulo")
            self.assertEqual(self.aba.avaliar('document.querySelector("#description-textarea #textbox").innerText'), "Texto de descricao")
            self.assertEqual(self.aba.avaliar('document.querySelector("#tags-container input").value'), "Texto de tags")


if __name__ == "__main__":
    unittest.main()
