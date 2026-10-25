"""Canais do YouTube: ritmo entre as chamadas, pausa que cresce, a linha original do yt-dlp e a
conta separada (opcional), com a exportação dos cookies por um Chrome sem janela.

Nenhum pedido vai ao YouTube nem ao Google: o yt-dlp é um script falso, escrito pelo próprio teste
numa pasta temporária, que responde conforme o `cenario.json` e anota cada chamada no
`chamadas.jsonl`. Na exportação, as páginas de login e o robots.txt são de um servidor local, e os
cookies são plantados pelo DevTools.
"""

import base64
import copy
import ctypes
import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from autocortes import canais, canais_conta, db, ferramentas, perfis, segredos, util
from autocortes.config import Config, PADRAO
from autocortes.midia import Interrompido
from autocortes.navegador import Aba, ErroNavegador, _Websocket, caminho_programa

H = 3600
SEGREDOS = ("segredo-de-teste", "tok-secreto", "psid-secreto")
CONTA_DE_TESTE = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tvalor-de-teste\n"
LINHA_ROBO = ("ERROR: [youtube] n4BNHEqMIbQ: Sign in to confirm you’re not a bot. Use --cookies-from-browser or "
              "--cookies for the authentication. See https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-"
              "cookies-to-yt-dlp for how to manually pass cookies (cookie: SAPISID=segredo-de-teste; "
              "token=tok-secreto; __Secure-3PSID=psid-secreto)")

YTDLP_FALSO = r'''
import hashlib, json, os, sys, time
from pathlib import Path

inicio = time.time()
sys.stdout.reconfigure(encoding="utf-8")  # como o yt-dlp com --encoding utf-8
sys.stderr.reconfigure(encoding="utf-8")
pasta = Path(__file__).resolve().parent
argv = sys.argv[1:]
cenario = json.loads((pasta / "cenario.json").read_text(encoding="utf-8"))
tipo = "listar" if "--flat-playlist" in argv else "baixar"
# --cookies <arquivo>: como o yt-dlp, lê no começo e grava os cookies (renovados) no fim
arq_cookies = Path(argv[argv.index("--cookies") + 1]) if "--cookies" in argv else None
conteudo = ""
if arq_cookies is not None and arq_cookies.is_file():
    conteudo = arq_cookies.read_text(encoding="utf-8")
cookies = conteudo.startswith("# Netscape HTTP Cookie File") and "\tLOGIN_INFO\t" in conteudo
chave = tipo + "_conta" if arq_cookies is not None and (tipo + "_conta") in cenario else tipo
with open(pasta / "pids.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps({"pid": os.getpid(), "chave": chave}) + "\n")
arq_estado = pasta / "estado_falso.json"
estado = json.loads(arq_estado.read_text(encoding="utf-8")) if arq_estado.exists() else {}
n = estado.get(chave, 0)
estado[chave] = n + 1
arq_estado.write_text(json.dumps(estado), encoding="utf-8")
respostas = cenario.get(chave) or ["ok"]
resposta = respostas[min(n, len(respostas) - 1)]
dormir = cenario.get("dormir", 0)
time.sleep(float(dormir.get(chave, 0) if isinstance(dormir, dict) else dormir))
vid = argv[-1].rsplit("=", 1)[-1] if tipo == "baixar" else "n4BNHEqMIbQ"
# o caminho do arquivo de cookies na linha de erro: o AutoCortes não pode deixar ele sair no registro
onde = f" (arquivo de cookies: {arq_cookies})" if arq_cookies is not None else ""
INVALIDOS = ("WARNING: [youtube] The provided YouTube account cookies are no longer valid. They have likely been "
             "rotated in the browser as a security measure.\n")
ROBO = (f"ERROR: [youtube] {vid}: Sign in to confirm you\u2019re not a bot. Use --cookies-from-browser "
        "or --cookies for the authentication. See https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-"
        "pass-cookies-to-yt-dlp for how to manually pass cookies (cookie: SAPISID=segredo-de-teste; "
        f"token=tok-secreto; __Secure-3PSID=psid-secreto){onde}\n")
codigo = 0
if resposta in ("invalidos", "invalidos_ok"):
    sys.stderr.write(INVALIDOS)
if resposta in ("ok", "invalidos_ok") and tipo == "listar":
    agora = time.time()
    entradas = [{"id": f"vid{i:08d}", "title": f"Vídeo de teste {i}", "duration": 600, "timestamp": agora - 3600 - i}
                for i in range(int(cenario.get("entradas", 3)))]
    sys.stdout.write(json.dumps({"channel": "Canal de Teste", "entries": entradas}) + "\n")
elif resposta in ("ok", "invalidos_ok"):
    destino = Path(argv[argv.index("-P") + 1]) / f"{vid}.mp4"
    destino.write_bytes(b"video falso " * 64)
    sys.stdout.write(f"AC_ARQUIVO|{destino}\n")
elif resposta in ("robo", "invalidos"):
    sys.stderr.write(ROBO)
    codigo = 1
elif resposta == "idade":
    sys.stderr.write(f"ERROR: [youtube] {vid}: Sign in to confirm your age. This video may be inappropriate for "
                     f"some users.{onde}\n")
    codigo = 1
elif resposta == "429":
    sys.stderr.write(f"ERROR: [youtube] {vid}: HTTP Error 429: Too Many Requests{onde}\n")
    codigo = 1
elif resposta == "video":
    sys.stderr.write(f"ERROR: [youtube] {vid}: Video unavailable{onde}\n")
    codigo = 1
sys.stdout.flush()
sys.stderr.flush()
if arq_cookies is not None:
    linhas = [x for x in conteudo.splitlines() if x and "\tSIDCC\t" not in x]
    linhas.append(f".youtube.com\tTRUE\t/\tTRUE\t0\tSIDCC\trenovado-{n + 1}")
    linhas.append(".example.com\tTRUE\t/\tFALSE\t0\tintruso\tde-fora")
    arq_cookies.write_text("\n".join(linhas) + "\n", encoding="utf-8")
with open(pasta / "chamadas.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps({"inicio": inicio, "fim": time.time(), "argv": argv, "cookies": cookies, "tipo": tipo,
                        "hash": hashlib.sha256(conteudo.encode()).hexdigest() if conteudo else None}) + "\n")
sys.exit(codigo)
'''


def _erro(causa: str) -> canais.ErroCanal:
    return canais.ErroCanal(canais.MOTIVOS[causa], "bloqueio", causa=causa, detalhe="ERROR: teste")


class Base(unittest.TestCase):
    def setUp(self):
        temporaria = tempfile.TemporaryDirectory(prefix="autocortes-teste-canais-")
        self.addCleanup(temporaria.cleanup)
        self.pasta = Path(temporaria.name)
        self.cfg = Config(copy.deepcopy(PADRAO), self.pasta / "config.toml")
        self.cfg.dados["canais"].update(max_na_fila=50, espaco_minimo_gb=0, minutos_min=0, minutos_max=0,
                                        max_idade_dias=0, max_por_canal=2)
        self.conn = db.conectar(self.cfg.banco)
        self.addCleanup(self.conn.close)


class TestPausa(Base):
    T = 1_800_000_000.0

    def registrar(self, quando, causa="robo"):
        return canais._registrar_aviso(self.conn, _erro(causa), agora=quando)

    def test_escalada(self):
        p = self.registrar(self.T)
        self.assertEqual((p["vezes"], p["ate"] - p["desde"]), (1, 12 * H))
        p = self.registrar(p["ate"] + 20 * H)  # 20 h depois do fim
        self.assertEqual((p["vezes"], p["ate"] - p["desde"]), (2, 24 * H))
        p = self.registrar(p["ate"] + 1 * H)  # mais um dentro da janela
        self.assertEqual((p["vezes"], p["ate"] - p["desde"]), (3, 48 * H))
        p = self.registrar(p["ate"] + 1 * H)  # o quarto: 48 h é o máximo
        self.assertEqual((p["vezes"], p["ate"] - p["desde"]), (4, 48 * H))

    def test_durante_a_pausa_sobe(self):
        self.registrar(self.T)
        p = self.registrar(self.T + 1 * H)  # "Olhar agora" que levou o aviso no meio da pausa
        self.assertEqual((p["vezes"], p["ate"] - p["desde"]), (2, 24 * H))

    def test_tres_dias_sem_aviso_zeram(self):
        self.registrar(self.T)
        self.registrar(self.T + 13 * H)
        p = self.registrar(self.T + 13 * H + 73 * H)
        self.assertEqual((p["vezes"], p["ate"] - p["desde"]), (1, 12 * H))

    def test_depois_da_janela_repete(self):
        p = self.registrar(self.T)
        p = self.registrar(p["ate"] + 30 * H)  # 42 h desde o último aviso: nem sobe nem zera
        self.assertEqual((p["vezes"], p["ate"] - p["desde"]), (1, 12 * H))
        p = self.registrar(p["ate"] + 1 * H)
        p = self.registrar(p["ate"] + 30 * H)  # repete a de 24 h
        self.assertEqual((p["vezes"], p["ate"] - p["desde"]), (2, 24 * H))

    def test_registro_gravado(self):
        p = self.registrar(self.T, "limite")
        gravado = db.ler_estado(self.conn, canais.CHAVE_PAUSA)
        self.assertEqual(gravado, p)
        self.assertEqual(set(gravado), {"ate", "desde", "ultimo_aviso", "vezes", "tipo", "motivo", "detalhe",
                                        "com_conta", "so_conta"})
        self.assertEqual(gravado["tipo"], "limite")
        self.assertEqual(gravado["motivo"], canais.MOTIVOS["limite"])
        self.assertNotIn("mensagem", gravado)

    def test_registro_antigo(self):
        desde = datetime(2026, 10, 25, 6, 1, 17).timestamp()
        antigo = {"ate": desde + 12 * H, "desde": desde,
                  "motivo": "o YouTube pediu para confirmar que não é um robô. Paro os downloads por 12 h; "
                            "não uso login nem contorno a verificação"}
        self.assertEqual(canais._normalizar_pausa(antigo), {
            "ate": desde + 12 * H, "desde": desde, "ultimo_aviso": desde, "vezes": 1, "tipo": "robo",
            "motivo": canais.MOTIVOS["robo"], "detalhe": None, "com_conta": False, "so_conta": False,
        })
        limite = {"ate": desde + 12 * H, "desde": desde,
                  "motivo": "o YouTube limitou os pedidos (muitos seguidos). Paro os downloads por 12 h"}
        self.assertEqual(canais._normalizar_pausa(limite)["tipo"], "limite")
        # o antigo entra na escalada: um aviso durante ele já é o segundo
        db.gravar_estado(self.conn, canais.CHAVE_PAUSA, antigo)
        p = self.registrar(desde + 2 * H)
        self.assertEqual((p["vezes"], p["ate"] - p["desde"]), (2, 24 * H))
        self.assertIsNone(canais._normalizar_pausa(None))
        self.assertIsNone(canais._normalizar_pausa({"motivo": "sem ate"}))

    def test_pausa_le_com_mensagem_e_some_depois_do_fim(self):
        agora = time.time()
        db.gravar_estado(self.conn, canais.CHAVE_PAUSA, {"ate": agora - 1, "desde": agora - 12 * H, "motivo": "x"})
        self.assertIsNone(canais.pausa(self.conn, self.cfg))
        ate = agora + 2 * H
        db.gravar_estado(self.conn, canais.CHAVE_PAUSA, {"ate": ate, "desde": agora, "motivo": "x"})
        p = canais.pausa(self.conn, self.cfg)
        self.assertEqual(p["mensagem"], f"o YouTube pediu para confirmar que não é um robô. Paro os downloads até "
                                        f"{datetime.fromtimestamp(ate).strftime('%d/%m às %H:%M')}; para não depender "
                                        "disso, conecte uma conta separada em Filmes > Canais do YouTube (opcional)")
        self.assertNotIn("não uso login", p["mensagem"])

    def test_mensagens_com_a_conta(self):
        p = canais._normalizar_pausa({"ate": self.T + 12 * H, "desde": self.T, "tipo": "robo"})
        hora = datetime.fromtimestamp(self.T + 12 * H).strftime("%d/%m às %H:%M")
        robo = f"o YouTube pediu para confirmar que não é um robô. Paro os downloads até {hora}"
        self.assertEqual(canais._mensagem_pausa(p, "nenhuma"), robo + "; para não depender disso, conecte uma conta "
                                                                     "separada em Filmes > Canais do YouTube (opcional)")
        self.assertEqual(canais._mensagem_pausa(p, "caiu"),
                         robo + "; a conta separada precisa entrar de novo em Filmes > Canais do YouTube")
        self.assertEqual(canais._mensagem_pausa({**p, "com_conta": True}, "ok"), robo)
        self.assertEqual(canais._mensagem_pausa({**p, "tipo": "limite"}, "nenhuma"),
                         f"o YouTube limitou os pedidos (muitos seguidos). Paro os downloads até {hora}")
        janela = {**p, "so_conta": True}
        self.assertEqual(canais._mensagem_pausa(janela, "ok"), f"o YouTube pediu para confirmar que não é um robô sem a "
                                                               f"conta; até {hora} os downloads vão direto com a conta "
                                                               "separada")
        self.assertEqual(canais._mensagem_pausa(janela, "nenhuma"), canais._mensagem_pausa(p, "nenhuma"))

    def test_proxima_verificacao(self):
        self.cfg.dados["canais"].update(ativo=True, verificar_a_cada_horas=6)
        agora = time.time()
        db.gravar_estado(self.conn, canais.CHAVE_VERIFICADO, agora)
        db.gravar_estado(self.conn, canais.CHAVE_PAUSA, {"ate": agora + 12 * H, "desde": agora, "motivo": "x"})
        self.assertEqual(canais.proxima_verificacao(self.cfg, self.conn), agora + 12 * H)
        db.gravar_estado(self.conn, canais.CHAVE_PAUSA, {"ate": agora + 1 * H, "desde": agora, "motivo": "x"})
        self.assertEqual(canais.proxima_verificacao(self.cfg, self.conn), agora + 6 * H)


class TestClassificar(unittest.TestCase):
    def test_robo(self):
        e = canais._classificar(["WARNING: algo", LINHA_ROBO])
        self.assertEqual((e.tipo, e.causa, str(e)), ("bloqueio", "robo", canais.MOTIVOS["robo"]))
        self.assertTrue(e.detalhe.startswith("ERROR:"))
        self.assertIn("Sign in to confirm you’re not a bot", e.detalhe)
        for s in SEGREDOS:
            self.assertNotIn(s, e.detalhe)
        self.assertNotIn("não uso login", str(e))

    def test_idade_antes_do_robo(self):
        e = canais._classificar(["ERROR: [youtube] abcdefghijk: Sign in to confirm your age. This video may be "
                                 "inappropriate for some users."])
        self.assertEqual((e.tipo, e.causa, str(e)), ("video", "idade", "o vídeo tem restrição de idade"))

    def test_429(self):
        e = canais._classificar(["ERROR: [youtube] abcdefghijk: HTTP Error 429: Too Many Requests"])
        self.assertEqual((e.tipo, e.causa, str(e)), ("bloqueio", "limite", canais.MOTIVOS["limite"]))

    def test_sem_javascript(self):
        e = canais._classificar(["WARNING: [youtube] No supported JavaScript runtime could be found",
                                 "ERROR: [youtube] abcdefghijk: Requested format is not available"])
        self.assertEqual((e.tipo, e.causa), ("ferramenta", "js"))

    def test_desatualizado(self):
        e = canais._classificar(["ERROR: [youtube] abcdefghijk: Requested format is not available. Use --list-formats"])
        self.assertEqual((e.tipo, e.causa), ("desatualizado", "desatualizado"))
        self.assertEqual(str(e), "Requested format is not available. Use --list-formats")

    def test_limpar_detalhe(self):
        caminho = r"C:\Users\fulano\dados\tokens\.canais-cookies-0123456789abcdef.txt"
        t = canais._limpar_detalhe(f"{LINHA_ROBO}   arquivo {caminho}", extras=(caminho,))
        for s in (*SEGREDOS, caminho, "0123456789abcdef"):
            self.assertNotIn(s, t)
        self.assertIn("SAPISID=***", t)
        self.assertIn("token=***", t)
        self.assertIn("__Secure-3PSID=***", t)
        self.assertIn("--cookies for the authentication", t)
        self.assertNotIn("  ", t)
        self.assertLessEqual(len(t), 300)
        self.assertEqual(len(canais._limpar_detalhe("ERROR: " + "x" * 1000)), 300)
        self.assertIn("<cookies da conta>", canais._limpar_detalhe(f"ERROR: {caminho}", extras=(caminho,)))
        # a marca no lugar do caminho não vira *** (ela mesma começa com "cookies")
        self.assertEqual(canais._limpar_detalhe(f"ERROR: x (arquivo de cookies: {caminho})", extras=(caminho,)),
                         "ERROR: x (arquivo de cookies: <cookies da conta>)")
        espaco = r"E:\Projetos KIRO\dados\tokens\.canais-cookies-0123456789abcdef.txt"
        self.assertEqual(canais._limpar_detalhe(f"ERROR: cookies: {espaco}", extras=(espaco,)),
                         "ERROR: cookies: <cookies da conta>")


def trocar(teste: unittest.TestCase, trocas) -> None:
    """Troca atributos de módulo durante o teste (e desfaz no fim)."""
    for modulo, nome, valor in trocas:
        teste.addCleanup(setattr, modulo, nome, getattr(modulo, nome))
        setattr(modulo, nome, valor)


class BaseFalso(Base):
    """Um canal cadastrado e o yt-dlp falso no lugar do de verdade."""

    def setUp(self):
        super().setUp()
        canais.adicionar(self.conn, "https://www.youtube.com/@canalteste")
        self.falso = self.pasta / "ytdlp_falso.py"
        self.falso.write_text(YTDLP_FALSO, encoding="utf-8")
        self.parar = threading.Event()
        trocar(self, [
            (canais, "_base", lambda cfg, ytdlp, js: [sys.executable, str(self.falso)]),
            (ferramentas, "garantir_ytdlp", lambda cfg: Path(sys.executable)),
            (ferramentas, "atualizar_ytdlp", lambda cfg, forcar=False: False),
            (ferramentas, "runtime_js", lambda cfg: []),
            (canais, "RITMO_SEG", (0.2, 0.3)),
        ])

    def cenario(self, **dados):
        (self.pasta / "estado_falso.json").unlink(missing_ok=True)
        (self.pasta / "cenario.json").write_text(json.dumps(dados), encoding="utf-8")

    def chamadas(self) -> list[dict]:
        arq = self.pasta / "chamadas.jsonl"
        if not arq.exists():
            return []
        return [json.loads(x) for x in arq.read_text(encoding="utf-8").splitlines() if x.strip()]

    def verificar(self, forcar=False):
        return canais.verificar(self.cfg, self.conn, self.parar, forcar=forcar)

    def registro(self) -> dict:
        return db.ler_estado(self.conn, canais.CHAVE_PAUSA)


class TestOlhadaSemConta(BaseFalso):
    def test_ritmo_entre_as_chamadas(self):
        self.cenario(listar=["ok"], baixar=["ok"], entradas=3)
        resumo = self.verificar()
        self.assertEqual(len(resumo["baixados"]), 2, resumo)
        chamadas = self.chamadas()
        self.assertEqual([c["tipo"] for c in chamadas], ["listar", "baixar", "baixar"])
        for antes, depois in zip(chamadas, chamadas[1:]):
            self.assertGreaterEqual(depois["inicio"] - antes["fim"], canais.RITMO_SEG[0])
        self.assertFalse(any(c["cookies"] or "--cookies" in c["argv"] for c in chamadas))
        self.assertTrue(all("--sleep-requests" in c["argv"] for c in chamadas if c["tipo"] == "baixar"))

    def test_parar_no_meio_da_espera(self):
        canais.RITMO_SEG = (30.0, 30.0)
        self.cenario(listar=["ok"], baixar=["ok"], entradas=3)

        def parar_depois_da_primeira():
            limite = time.monotonic() + 10
            while not self.chamadas() and time.monotonic() < limite:
                time.sleep(0.05)
            time.sleep(0.5)
            self.parar.set()

        threading.Thread(target=parar_depois_da_primeira, daemon=True).start()
        inicio = time.monotonic()
        with self.assertRaises(Interrompido):
            self.verificar()
        self.assertLess(time.monotonic() - inicio, 3)
        self.assertEqual(len(self.chamadas()), 1)

    def test_robo_na_listagem(self):
        self.cenario(listar=["robo"])
        with self.assertLogs(util.log, "WARNING") as registro:
            resumo = self.verificar()
        self.assertIn("Paro os downloads até", resumo["parado"])
        self.assertNotIn("não uso login", resumo["parado"])
        p = self.registro()
        self.assertEqual(p["vezes"], 1)
        self.assertEqual(p["ate"] - p["desde"], 12 * H)
        self.assertEqual(p["tipo"], "robo")
        self.assertTrue(p["detalhe"].startswith("ERROR:"))
        self.assertIn("Sign in to confirm you’re not a bot", p["detalhe"])
        self.assertIn("SAPISID=***", p["detalhe"])
        texto = "\n".join(registro.output)
        self.assertIn("o YouTube pediu para confirmar que não é um robô. Paro os downloads até", texto)
        self.assertIn("Sign in to confirm", texto)
        for s in SEGREDOS:
            self.assertNotIn(s, p["detalhe"])
            self.assertNotIn(s, texto)
        canal = self.conn.execute("SELECT erro FROM canais").fetchone()
        self.assertIn("Paro os downloads até", canal["erro"])

    def test_forcar_durante_a_pausa_cresce(self):
        self.cenario(listar=["robo"])
        self.verificar()
        esperado = [(2, 24 * H), (3, 48 * H), (4, 48 * H)]
        for vezes, duracao in esperado:
            resumo = self.verificar(forcar=True)
            p = self.registro()
            self.assertEqual((p["vezes"], p["ate"] - p["desde"]), (vezes, duracao))
            self.assertIn("Paro os downloads até", resumo["parado"])

    def test_sem_forcar_durante_a_pausa_nao_chama(self):
        self.cenario(listar=["robo"])
        self.verificar()
        antes = len(self.chamadas())
        resumo = self.verificar()
        self.assertEqual(len(self.chamadas()), antes)
        self.assertEqual(resumo["parado"], canais.pausa(self.conn, self.cfg)["mensagem"])
        self.assertEqual(self.registro()["vezes"], 1)

    def test_429(self):
        self.cenario(listar=["ok"], baixar=["429"], entradas=2)
        resumo = self.verificar()
        p = self.registro()
        self.assertEqual(p["tipo"], "limite")
        self.assertIn("limitou", resumo["parado"])
        self.assertIn("Paro os downloads até", resumo["parado"])
        linha = self.conn.execute("SELECT status, tentativas FROM downloads").fetchone()
        self.assertEqual((linha["status"], linha["tentativas"]), ("erro", 0))  # o bloqueio não conta contra o vídeo

    def test_idade(self):
        self.cenario(listar=["ok"], baixar=["idade"], entradas=1)
        resumo = self.verificar()
        self.assertIsNone(resumo["parado"])
        linha = self.conn.execute("SELECT status, erro FROM downloads").fetchone()
        self.assertEqual(linha["status"], "pulado")
        self.assertIn("restrição de idade", linha["erro"])
        self.assertIsNone(self.registro())


def _vivo(pid: int) -> bool:
    """O processo ainda roda? (no Windows, os.kill(pid, 0) mataria o processo)"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    alca = k32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not alca:
        return False
    try:
        codigo = ctypes.c_uint32()
        k32.GetExitCodeProcess(alca, ctypes.byref(codigo))
        return codigo.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(alca)


class TestFormatoNetscape(Base):
    def test_cookies_do_cdp(self):
        texto, n = canais_conta.para_netscape([
            {"name": "LOGIN_INFO", "value": "a", "domain": ".youtube.com", "path": "/", "secure": True,
             "expires": 1824454077.6, "session": False},
            {"name": "SESSAO", "value": "b", "domain": "www.youtube.com", "path": "/x", "secure": False,
             "expires": -1, "session": True},
            {"name": "SID", "value": "c", "domain": ".google.com", "path": "/", "secure": True, "expires": 1824454077},
            {"name": "fora", "value": "d", "domain": ".example.com", "path": "/", "expires": 1824454077},
            {"name": "falso", "value": "e", "domain": "notyoutube.com", "path": "/", "expires": 1824454077},
            {"name": "quebra", "value": "f\tg", "domain": ".youtube.com", "path": "/", "expires": 1824454077},
        ])
        self.assertEqual(n, 3)
        self.assertEqual(texto.splitlines(), [
            "# Netscape HTTP Cookie File",
            ".youtube.com\tTRUE\t/\tTRUE\t1824454077\tLOGIN_INFO\ta",
            "www.youtube.com\tFALSE\t/x\tFALSE\t0\tSESSAO\tb",
            ".google.com\tTRUE\t/\tTRUE\t1824454077\tSID\tc",
        ])

    def test_filtro_do_arquivo_regravado(self):
        regravado = ("# Netscape HTTP Cookie File\r\n# This file is generated by yt-dlp.  Do not edit.\r\n\r\n"
                     ".youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\ta\r\n"
                     "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t\tSIDCC\tb\r\n"
                     "accounts.google.com\tFALSE\t/\tTRUE\t1824454077\tLSID\tc\r\n"
                     ".example.com\tTRUE\t/\tFALSE\t0\tintruso\td\r\n"
                     "#HttpOnly_.notyoutube.com\tTRUE\t/\tTRUE\t0\tfalso\te\r\n"
                     ".youtube.com\tTRUE\t/\tTRUE\t0\tcurto\r\n")
        texto, n = canais_conta.filtrar_netscape(regravado)
        self.assertEqual(n, 3)
        self.assertEqual(texto.splitlines(), [
            "# Netscape HTTP Cookie File",
            ".youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\ta",
            "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t\tSIDCC\tb",
            "accounts.google.com\tFALSE\t/\tTRUE\t1824454077\tLSID\tc",
        ])
        for dominio, ok in (("youtube.com", True), (".m.youtube.com", True), ("google.com", True),
                            ("notyoutube.com", False), ("youtube.com.br", False), ("google.com.evil", False)):
            self.assertEqual(canais_conta.dominio_permitido(dominio), ok, dominio)

    def test_situacao_e_publico(self):
        self.assertEqual(canais_conta.publico(self.cfg)["situacao"], "nenhuma")
        canais_conta.salvar_nova(self.cfg, CONTA_DE_TESTE)
        publico = canais_conta.publico(self.cfg)
        self.assertEqual((publico["situacao"], publico["cookies"], publico["usada_em"]), ("ok", 1, None))
        self.assertNotIn("valor-de-teste", json.dumps(publico))
        self.assertTrue(canais_conta.utilizavel(self.cfg))
        self.assertFalse(canais_conta.marcar_caiu(self.cfg, "teste", id_conta="outra-conexao"))
        self.assertTrue(canais_conta.marcar_caiu(self.cfg, "o motivo de teste"))
        publico = canais_conta.publico(self.cfg)
        self.assertEqual((publico["situacao"], publico["motivo"], publico["cookies"]), ("caiu", "o motivo de teste", 0))
        self.assertFalse(canais_conta.utilizavel(self.cfg))
        with self.assertRaises(canais_conta.ErroConta):
            with canais_conta.cookies_temporarios(self.cfg):
                pass
        self.assertTrue(canais_conta.esquecer(self.cfg))
        self.assertEqual(canais_conta.situacao(self.cfg), "nenhuma")

    def test_arquivo_que_nao_abre_aqui(self):
        alvo = canais_conta.arquivo(self.cfg)
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_text(json.dumps({"protegido": "dpapi", "dados": "AAAA"}), encoding="utf-8")
        segredos._AVISADOS.discard(alvo.name)  # o aviso sai uma vez por arquivo em cada processo
        with self.assertLogs(util.log, "WARNING"):
            publico = canais_conta.publico(self.cfg)
        self.assertEqual((publico["situacao"], publico["motivo"]), ("caiu", canais_conta.NAO_ABRE))
        self.assertEqual(canais_conta.situacao(self.cfg), "caiu")

    def test_perfil_fora_do_lugar_nao_e_apagado(self):
        perfil = self.cfg.pasta_dados / "chrome"
        perfil.mkdir(parents=True)
        trocar(self, [(canais_conta, "PASTA_PERFIL", "chrome")])
        with self.assertLogs(util.log, "WARNING"):
            self.assertFalse(canais_conta._apagar_perfil(self.cfg))
        self.assertTrue(perfil.is_dir())


class TestOlhadaComConta(BaseFalso):
    def conectar(self):
        return canais_conta.salvar_nova(self.cfg, CONTA_DE_TESTE)

    def temporarios(self) -> list[Path]:
        return list(self.cfg.pasta_tokens.glob(f"{canais_conta.PREFIXO_TEMP}*"))

    def pids(self) -> list[dict]:
        arq = self.pasta / "pids.jsonl"
        saida = []
        for x in (arq.read_text(encoding="utf-8").splitlines() if arq.exists() else []):
            try:
                saida.append(json.loads(x))
            except ValueError:
                pass  # o falso está escrevendo a linha agora
        return saida

    def esperar_chamada(self, chave: str, segundos: float = 30) -> None:
        limite = time.monotonic() + segundos
        while not any(x["chave"] == chave for x in self.pids()) and time.monotonic() < limite:
            time.sleep(0.05)

    def tipos(self, chamadas=None) -> list[tuple[str, bool]]:
        return [(x["tipo"], "--cookies" in x["argv"]) for x in (self.chamadas() if chamadas is None else chamadas)]

    def test_sem_conta_nunca_ha_cookies(self):
        self.cenario(listar=["ok"], baixar=["idade"], entradas=1)
        self.verificar()
        linha = self.conn.execute("SELECT status, erro FROM downloads").fetchone()
        self.assertEqual(linha["status"], "pulado")
        self.assertEqual(linha["erro"], "o vídeo tem restrição de idade: só dá para baixar com a conta separada "
                                        "(Filmes > Canais do YouTube)")
        self.cenario(listar=["robo"])
        resumo = self.verificar()
        self.assertIn("conecte uma conta separada em Filmes > Canais do YouTube (opcional)", resumo["parado"])
        self.assertEqual(self.tipos(), [("listar", False), ("baixar", False), ("listar", False)])
        self.assertFalse(any(x["cookies"] for x in self.chamadas()))
        self.assertFalse(resumo["conta"])

    def test_robo_repete_o_mesmo_pedido_com_a_conta(self):
        self.conectar()
        self.cenario(listar=["robo"], listar_conta=["ok"], baixar_conta=["ok"], entradas=2)
        resumo = self.verificar()
        self.assertIsNone(resumo["parado"])
        self.assertEqual(len(resumo["baixados"]), 2)
        self.assertTrue(resumo["conta"])
        c = self.chamadas()
        # a primeira sem a conta; a repetição e o resto da olhada, com ela (sem tentar antes sem)
        self.assertEqual(self.tipos(c), [("listar", False), ("listar", True), ("baixar", True), ("baixar", True)])
        self.assertTrue(all(x["cookies"] for x in c[1:]))  # o arquivo tinha o cabeçalho e o LOGIN_INFO
        self.assertEqual(c[1]["argv"][2:], c[0]["argv"])  # o mesmo pedido, só com o --cookies na frente
        self.assertNotEqual(c[2]["hash"], c[1]["hash"])  # a chamada seguinte já leva os cookies renovados
        for antes, depois in zip(c, c[1:]):
            self.assertGreaterEqual(depois["inicio"] - antes["fim"], canais.RITMO_SEG[0])
        p = self.registro()
        self.assertEqual((p["so_conta"], p["com_conta"], p["vezes"], p["tipo"]), (True, False, 1, "robo"))
        self.assertIsNone(canais.pausa(self.conn, self.cfg))
        janela = canais.janela_so_conta(self.conn, self.cfg)
        self.assertIn("sem a conta; até", janela["mensagem"])
        self.assertIn("os downloads vão direto com a conta separada", janela["mensagem"])
        self.assertEqual(self.temporarios(), [])
        # a olhada seguinte já começa com a conta
        antes = len(c)
        self.verificar()
        self.assertEqual(self.tipos()[antes], ("listar", True))
        # depois do fim da janela, volta a começar sem ela
        db.gravar_estado(self.conn, canais.CHAVE_PAUSA, {**self.registro(), "ate": time.time() - 1})
        antes = len(self.chamadas())
        self.verificar()
        self.assertEqual(self.tipos()[antes], ("listar", False))
        self.assertEqual(self.temporarios(), [])

    def test_cookies_renovados_ficam_cifrados(self):
        self.conectar()
        self.cenario(listar=["robo"], listar_conta=["ok"], baixar_conta=["ok"], entradas=1)
        self.verificar()
        conta = canais_conta.ler(self.cfg)
        self.assertIn("\tSIDCC\trenovado-", conta["cookies"])
        self.assertIn("\tLOGIN_INFO\tvalor-de-teste", conta["cookies"])
        self.assertNotIn("example.com", conta["cookies"])
        self.assertIsNotNone(conta["renovada_em"])
        self.assertIsNotNone(conta["usada_em"])
        bruto = canais_conta.arquivo(self.cfg).read_text(encoding="utf-8")
        self.assertEqual(json.loads(bruto)["protegido"], "dpapi")
        for s in ("renovado-", "valor-de-teste", "LOGIN_INFO"):
            self.assertNotIn(s, bruto)
        self.assertEqual(canais_conta.publico(self.cfg)["cookies"], 2)
        self.assertEqual(self.temporarios(), [])

    def test_idade_repete_com_a_conta(self):
        self.conectar()
        self.cenario(listar=["ok"], baixar=["idade"], baixar_conta=["ok"], entradas=2)
        resumo = self.verificar()
        self.assertEqual(len(resumo["baixados"]), 2)
        c = self.chamadas()
        self.assertEqual(self.tipos(c), [("listar", False), ("baixar", False), ("baixar", True), ("baixar", True)])
        self.assertEqual(c[2]["argv"][2:], c[1]["argv"])
        self.assertIsNone(self.registro())  # a idade não pausa nada

    def test_idade_que_a_conta_tambem_nao_ve(self):
        self.conectar()
        self.cenario(listar=["ok"], baixar=["idade"], baixar_conta=["idade"], entradas=1)
        self.verificar()
        linha = self.conn.execute("SELECT status, erro FROM downloads").fetchone()
        self.assertEqual(linha["status"], "pulado")
        self.assertEqual(linha["erro"], "o vídeo tem restrição de idade, e a conta separada também não pôde ver "
                                        "(confirme a idade dela no YouTube)")
        self.assertEqual(canais_conta.situacao(self.cfg), "ok")
        self.assertEqual(self.temporarios(), [])

    def test_conta_passou_do_robo_mas_o_video_falhou(self):
        # a repetição com a conta passa do robô, mas o vídeo dá outro erro: o resto da olhada vai com ela
        self.conectar()
        self.cenario(listar=["ok"], baixar=["robo", "ok"], baixar_conta=["video", "ok"], entradas=2)
        resumo = self.verificar()
        self.assertIsNone(resumo["parado"])
        self.assertEqual(len(resumo["baixados"]), 1)
        c = self.chamadas()
        self.assertEqual(self.tipos(c), [("listar", False), ("baixar", False), ("baixar", True), ("baixar", True)])
        self.assertEqual(c[2]["argv"][2:], c[1]["argv"])
        self.assertTrue(all(x["cookies"] for x in c[2:]))
        p = self.registro()
        self.assertEqual((p["so_conta"], p["com_conta"], p["vezes"], p["tipo"]), (True, False, 1, "robo"))
        self.assertIsNone(canais.pausa(self.conn, self.cfg))
        self.assertEqual(canais_conta.situacao(self.cfg), "ok")
        self.assertEqual(self.temporarios(), [])

    def test_429_com_a_conta(self):
        self.conectar()
        self.cenario(listar=["robo"], listar_conta=["429"])
        resumo = self.verificar()
        self.assertIn("limitou", resumo["parado"])
        p = self.registro()
        self.assertEqual((p["tipo"], p["com_conta"], p["so_conta"], p["vezes"]), ("limite", True, False, 1))
        self.assertIsNotNone(canais.pausa(self.conn, self.cfg))
        self.assertEqual(canais_conta.situacao(self.cfg), "ok")  # 429 é limite do IP: a conta continua
        self.assertIn("<cookies da conta>", p["detalhe"])
        self.assertNotIn(canais_conta.PREFIXO_TEMP, p["detalhe"])
        self.assertEqual(self.temporarios(), [])  # apagado também no erro

    def test_conta_que_leva_o_robo_cai(self):
        self.conectar()
        self.cenario(listar=["robo"], listar_conta=["robo"])
        resumo = self.verificar()
        self.assertIn("Paro os downloads até", resumo["parado"])
        self.assertIn("a conta separada precisa entrar de novo em Filmes > Canais do YouTube", resumo["parado"])
        conta = canais_conta.ler(self.cfg)
        self.assertEqual((conta["situacao"], conta["cookies"]), ("caiu", ""))
        self.assertEqual(conta["motivo"], "o YouTube pediu verificação mesmo com a conta")
        p = self.registro()
        self.assertEqual((p["tipo"], p["com_conta"], p["so_conta"], p["vezes"]), ("robo", True, False, 1))
        self.assertIsNotNone(canais.pausa(self.conn, self.cfg))
        self.assertEqual(self.temporarios(), [])
        # a pausa de verdade cresce como sem conta
        canais_conta.salvar_nova(self.cfg, CONTA_DE_TESTE)
        self.verificar(forcar=True)
        self.assertEqual(self.registro()["vezes"], 2)

    def test_cookies_recusados_e_o_pedido_passou(self):
        self.conectar()
        self.cenario(listar=["robo"], listar_conta=["invalidos_ok"], baixar=["ok"], entradas=1)
        resumo = self.verificar()
        conta = canais_conta.ler(self.cfg)
        self.assertEqual((conta["situacao"], conta["cookies"]), ("caiu", ""))
        self.assertEqual(conta["motivo"], "o YouTube não aceita mais os cookies da conta (ela saiu ou foi trocada)")
        # o aviso sem a conta vale como pausa normal, e nada mais sai sem a conta depois dele
        self.assertIn("Paro os downloads até", resumo["parado"])
        self.assertIn("a conta separada precisa entrar de novo", resumo["parado"])
        p = self.registro()
        self.assertEqual((p["tipo"], p["vezes"], p["com_conta"], p["so_conta"], p["ate"] - p["desde"]),
                         ("robo", 1, False, False, 12 * H))
        self.assertIsNotNone(canais.pausa(self.conn, self.cfg))
        self.assertEqual(self.tipos(), [("listar", False), ("listar", True)])
        self.assertEqual(resumo["baixados"], [])
        self.assertEqual(self.temporarios(), [])

    def test_janela_so_conta_e_a_conta_cai_na_primeira(self):
        self.conectar()
        canais._registrar_aviso(self.conn, _erro("robo"), so_conta=True)
        antes = self.registro()
        self.cenario(listar_conta=["invalidos_ok"], baixar=["ok"], entradas=1)
        resumo = self.verificar()
        self.assertEqual(canais_conta.situacao(self.cfg), "caiu")
        # sem a conta, a janela vira pausa: para ali, sem gravar de novo nem pedir sem a conta
        self.assertEqual(self.tipos(), [("listar", True)])
        self.assertIn("Paro os downloads até", resumo["parado"])
        self.assertEqual(self.registro(), antes)
        self.assertIsNotNone(canais.pausa(self.conn, self.cfg))
        self.assertEqual(self.temporarios(), [])

    def test_cookies_recusados_e_o_robo(self):
        self.conectar()
        self.cenario(listar=["robo"], listar_conta=["invalidos"])
        resumo = self.verificar()
        conta = canais_conta.ler(self.cfg)
        self.assertEqual(conta["situacao"], "caiu")
        self.assertEqual(conta["motivo"], "o YouTube não aceita mais os cookies da conta (ela saiu ou foi trocada)")
        p = self.registro()
        self.assertEqual((p["tipo"], p["com_conta"]), ("robo", True))
        self.assertIn("a conta separada precisa entrar de novo", resumo["parado"])

    def test_cancelar_no_meio_apaga_o_arquivo(self):
        self.conectar()
        self.cenario(listar=["robo"], listar_conta=["ok"], baixar_conta=["ok"], dormir={"baixar_conta": 30},
                     entradas=1)

        def parar_no_download():
            self.esperar_chamada("baixar_conta")
            time.sleep(1)
            self.parar.set()

        threading.Thread(target=parar_no_download, daemon=True).start()
        inicio = time.monotonic()
        with self.assertRaises(Interrompido):
            self.verificar()
        self.assertLess(time.monotonic() - inicio, 20)
        pid = next(x["pid"] for x in self.pids() if x["chave"] == "baixar_conta")
        limite = time.monotonic() + 10
        while _vivo(pid) and time.monotonic() < limite:
            time.sleep(0.1)
        self.assertFalse(_vivo(pid))
        self.assertEqual(self.temporarios(), [])
        self.assertEqual(canais_conta.situacao(self.cfg), "ok")

    def test_esquecer_no_meio_nao_recria_a_conta(self):
        self.conectar()
        self.cenario(listar=["robo"], listar_conta=["ok"], baixar=["ok"], dormir={"listar_conta": 2}, entradas=1)
        esquecida = []

        def esquecer_no_meio():
            self.esperar_chamada("listar_conta")
            time.sleep(0.5)
            esquecida.append(canais_conta.esquecer(self.cfg))

        t = threading.Thread(target=esquecer_no_meio, daemon=True)
        t.start()
        resumo = self.verificar()
        t.join(10)
        self.assertEqual(esquecida, [True])
        self.assertFalse(canais_conta.arquivo(self.cfg).exists())
        self.assertEqual(canais_conta.situacao(self.cfg), "nenhuma")
        self.assertEqual(self.temporarios(), [])
        # a conta sumiu depois do aviso de robô: a janela "só com a conta" virou pausa, e nada saiu sem ela
        self.assertEqual(self.tipos(), [("listar", False), ("listar", True)])
        self.assertEqual(resumo["baixados"], [])
        self.assertIn("Paro os downloads até", resumo["parado"])
        p = self.registro()
        self.assertEqual((p["vezes"], p["so_conta"]), (1, True))
        linha = self.conn.execute("SELECT status, tentativas FROM downloads").fetchone()
        self.assertEqual((linha["status"], linha["tentativas"]), ("erro", 0))

    def test_esquecer_sem_aviso_segue_sem_a_conta(self):
        self.conectar()
        self.cenario(listar=["ok"], baixar=["idade", "ok"], baixar_conta=["ok"], dormir={"baixar_conta": 2},
                     entradas=2)
        esquecida = []

        def esquecer_no_meio():
            self.esperar_chamada("baixar_conta")
            time.sleep(0.5)
            esquecida.append(canais_conta.esquecer(self.cfg))

        t = threading.Thread(target=esquecer_no_meio, daemon=True)
        t.start()
        resumo = self.verificar()
        t.join(10)
        self.assertEqual(esquecida, [True])
        # sem aviso de robô nem pausa, perder a conta só faz o resto da olhada seguir sem ela
        self.assertEqual(self.tipos(), [("listar", False), ("baixar", False), ("baixar", True), ("baixar", False)])
        self.assertEqual(len(resumo["baixados"]), 2)
        self.assertIsNone(resumo["parado"])
        self.assertIsNone(self.registro())

    def test_conta_reconectada(self):
        canais._registrar_aviso(self.conn, _erro("robo"))
        antes = self.registro()
        self.assertIsNotNone(canais.pausa(self.conn, self.cfg))
        self.assertTrue(canais.conta_reconectada(self.conn))
        depois = self.registro()
        self.assertEqual((depois["so_conta"], depois["com_conta"], depois["ate"], depois["vezes"]),
                         (True, False, antes["ate"], antes["vezes"]))
        self.assertFalse(canais.conta_reconectada(self.conn))  # já é a janela
        self.assertIsNotNone(canais.pausa(self.conn, self.cfg))  # sem conta, a janela vale como pausa
        self.assertIsNone(canais.janela_so_conta(self.conn, self.cfg))
        self.conectar()
        self.assertIsNone(canais.pausa(self.conn, self.cfg))
        self.assertIsNotNone(canais.janela_so_conta(self.conn, self.cfg))
        canais._registrar_aviso(self.conn, _erro("limite"))
        antes = self.registro()
        self.assertFalse(canais.conta_reconectada(self.conn))  # o 429 é do IP: continua
        self.assertEqual(self.registro(), antes)
        # o robô que a própria conta levou é a pausa de verdade: entrar de novo não a desfaz
        com_conta = _erro("robo")
        com_conta.com_conta = True
        canais._registrar_aviso(self.conn, com_conta)
        antes = self.registro()
        self.assertFalse(canais.conta_reconectada(self.conn))
        self.assertEqual(self.registro(), antes)
        self.assertIsNotNone(canais.pausa(self.conn, self.cfg))

    def test_registro_sem_o_caminho_nem_os_valores(self):
        self.conectar()
        self.cenario(listar=["robo"], listar_conta=["ok"], baixar_conta=["robo"], entradas=1)
        with self.assertLogs(util.log, logging.DEBUG) as registro:
            resumo = self.verificar()
        texto = "\n".join(registro.output)
        caminhos = {x["argv"][x["argv"].index("--cookies") + 1] for x in self.chamadas() if "--cookies" in x["argv"]}
        self.assertEqual(len(caminhos), 2)  # um arquivo por chamada
        self.assertIn("Executando:", texto)
        self.assertIn("--cookies \"<cookies da conta>\"", texto)
        self.assertIn("a conta separada precisa entrar de novo", texto)
        for s in (*caminhos, canais_conta.PREFIXO_TEMP, "valor-de-teste", "renovado-", *SEGREDOS):
            self.assertNotIn(s, texto)
            self.assertNotIn(s, json.dumps(self.registro(), ensure_ascii=False))
            self.assertNotIn(s, json.dumps(resumo, ensure_ascii=False))
        self.assertEqual(self.temporarios(), [])


class _PaginasFalsas(BaseHTTPRequestHandler):
    """O login e o robots.txt de mentira: nada vai ao Google nem ao YouTube."""

    def do_GET(self):
        self.server.pedidos.append(self.path)
        if self.path.startswith("/robots.txt"):
            corpo, tipo = b"User-agent: *\nDisallow:\n", "text/plain; charset=utf-8"
        else:
            corpo, tipo = "<!doctype html><title>Entrar</title><p>Login de teste</p>".encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, *args):
        pass


class TestContaNoNavegador(Base):
    """A janela de conectar com o Chrome de verdade, sem janela, num perfil temporário."""

    def setUp(self):
        super().setUp()
        try:
            caminho_programa(self.cfg)
        except ErroNavegador as erro:
            self.skipTest(str(erro))
        servidor = ThreadingHTTPServer(("127.0.0.1", 0), _PaginasFalsas)
        servidor.pedidos = []
        threading.Thread(target=servidor.serve_forever, daemon=True).start()
        self.addCleanup(servidor.server_close)
        self.addCleanup(servidor.shutdown)
        self.servidor = servidor
        base = f"http://127.0.0.1:{servidor.server_address[1]}"
        trocar(self, [
            (canais_conta, "URL_LOGIN", f"{base}/login"),
            (canais_conta, "URL_ROBOTS", f"{base}/robots.txt"),
            (canais_conta, "ARGUMENTOS_EXTRAS", ("--headless=new", "--disable-background-networking",
                                                 "--disable-component-update", "--no-pings")),
            (perfis, "listar", lambda: []),  # não lê os perfis da instalação
        ])
        self.conexoes: list[canais_conta.Conexao] = []
        self.addCleanup(self._fechar)

    def _fechar(self):
        for c in self.conexoes:  # nada de Chrome sobrando, mesmo com o teste falhando
            c.cancelar()
            if c._navegador is not None:
                c._navegador.encerrar()
            if c.thread is not None:
                c.thread.join(20)
            canais_conta._apagar_perfil(self.cfg)

    def abrir(self) -> canais_conta.Conexao:
        c = canais_conta.Conexao(self.cfg)
        self.conexoes.append(c)
        c.iniciar()
        self.assertEqual(c.estado, "aguardando")
        self.assertEqual(c.mensagem, "Entre na conta separada na janela que abriu.")
        self.assertTrue(canais_conta.pasta_perfil(self.cfg).is_dir())
        self.assertTrue(c._navegador.vivo)
        self.assertNotIn(c._navegador.porta, (int(self.cfg["navegador"]["porta"]), int(self.cfg["painel"]["porta"])))
        return c

    def esperar(self, c, estados, segundos=40):
        limite = time.monotonic() + segundos
        while c.estado not in estados and time.monotonic() < limite:
            time.sleep(0.2)
        self.assertIn(c.estado, estados, c.mensagem)
        c.thread.join(30)
        self.assertFalse(c.thread.is_alive())

    def plantar(self, porta: int, cookies: list[dict]) -> None:
        paginas = requests.get(f"http://127.0.0.1:{porta}/json/list", timeout=5).json()
        pagina = next(p for p in paginas if p.get("type") == "page")
        aba = Aba(_Websocket(pagina["webSocketDebuggerUrl"], timeout=10), ritmo=(0, 0))
        try:
            aba.comando("Network.enable")
            for cookie in cookies:
                self.assertTrue(aba.comando("Network.setCookie", **cookie).get("success", True), cookie["name"])
        finally:
            aba.fechar()

    def test_conecta_e_guarda_so_youtube_e_google(self):
        c = self.abrir()
        expira = time.time() + 30 * 86400
        comum = {"path": "/", "expires": expira}
        # os de sessão por último: são eles que dizem que a conta entrou
        self.plantar(c._navegador.porta, [
            {"name": "SID", "value": "sid-plantado", "url": "https://www.google.com/", "domain": ".google.com",
             "secure": False, **comum},
            {"name": "SO_HOST", "value": "host-plantado", "url": "https://accounts.google.com/", "secure": True,
             **comum},
            {"name": "outro", "value": "de-fora", "url": "https://www.example.com/", "domain": ".example.com", **comum},
            {"name": "falso", "value": "nao-pode", "url": "https://notyoutube.com/", "secure": True, **comum},
            {"name": "__Secure-3PSID", "value": "psid-plantado", "url": "https://www.youtube.com/",
             "domain": ".youtube.com", "secure": True, "httpOnly": True, **comum},
            {"name": "LOGIN_INFO", "value": "login-plantado", "url": "https://www.youtube.com/", "domain": ".youtube.com",
             "secure": True, "httpOnly": True, **comum},
        ])
        self.esperar(c, ("ok",))
        self.assertIn("/login", self.servidor.pedidos)
        self.assertIn("/robots.txt", self.servidor.pedidos)
        conta = canais_conta.ler(self.cfg)
        self.assertTrue(conta["cookies"].startswith("# Netscape HTTP Cookie File\n"))
        campos = [x.split("\t") for x in conta["cookies"].splitlines() if x and not x.startswith("#")]
        self.assertTrue(campos)
        self.assertTrue(all(len(x) == 7 for x in campos))
        self.assertTrue(all(canais_conta.dominio_permitido(x[0]) for x in campos))
        por_nome = {x[5]: x for x in campos}
        self.assertEqual(por_nome["LOGIN_INFO"][:4], [".youtube.com", "TRUE", "/", "TRUE"])
        self.assertLessEqual(abs(int(por_nome["LOGIN_INFO"][4]) - int(expira)), 1)
        self.assertEqual(por_nome["LOGIN_INFO"][6], "login-plantado")
        self.assertEqual(por_nome["__Secure-3PSID"][6], "psid-plantado")
        self.assertEqual(por_nome["SID"][:2], [".google.com", "TRUE"])
        self.assertEqual(por_nome["SO_HOST"][:2], ["accounts.google.com", "FALSE"])
        self.assertNotIn("outro", por_nome)
        self.assertNotIn("falso", por_nome)
        self.assertFalse(any("example.com" in x[0] or "notyoutube" in x[0] for x in campos))
        self.assertEqual(c.mensagem, f"Conta separada conectada ({len(campos)} cookies do YouTube e do Google).")
        # no disco, só o cifrado; a ida e volta pela DPAPI devolve os mesmos cookies
        bruto = json.loads(canais_conta.arquivo(self.cfg).read_text(encoding="utf-8"))
        self.assertEqual(bruto["protegido"], "dpapi")
        for s in ("login-plantado", "psid-plantado", "sid-plantado", "LOGIN_INFO"):
            self.assertNotIn(s, json.dumps(bruto))
        decifrado = json.loads(segredos.decifrar(base64.b64decode(bruto["dados"])).decode("utf-8"))
        self.assertEqual(decifrado["cookies"], conta["cookies"])
        self.assertEqual(decifrado["situacao"], "ok")
        self.assertFalse(canais_conta.pasta_perfil(self.cfg).exists())
        self.assertFalse(c._navegador.vivo)
        self.assertEqual(canais_conta.situacao(self.cfg), "ok")

    def test_cancelar_fecha_e_apaga(self):
        c = self.abrir()
        c.cancelar()
        self.assertEqual((c.estado, c.mensagem), ("cancelado", "Cancelado. Fechei a janela e apaguei o perfil temporário."))
        self.assertFalse(c.thread.is_alive())
        self.assertFalse(c._navegador.vivo)
        self.assertFalse(canais_conta.pasta_perfil(self.cfg).exists())
        self.assertFalse(canais_conta.arquivo(self.cfg).exists())

    def test_prazo(self):
        trocar(self, [(canais_conta, "PRAZO_LOGIN_SEG", 3)])
        c = self.abrir()
        self.esperar(c, ("prazo",))
        self.assertIn("sem a conta entrar. Fechei a janela; tente de novo quando quiser.", c.mensagem)
        self.assertFalse(c._navegador.vivo)
        self.assertFalse(canais_conta.pasta_perfil(self.cfg).exists())
        self.assertFalse(canais_conta.arquivo(self.cfg).exists())

    def test_janela_fechada(self):
        c = self.abrir()
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(c._navegador.processo.pid)], capture_output=True,
                       timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.esperar(c, ("erro",))
        self.assertEqual(c.mensagem, "A janela foi fechada antes de a conta entrar.")
        self.assertFalse(canais_conta.pasta_perfil(self.cfg).exists())
        self.assertFalse(canais_conta.arquivo(self.cfg).exists())


if __name__ == "__main__":
    unittest.main()
