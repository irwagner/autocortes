"""Visual C++ x64 do Whisper: a conferência antes de abrir os EXEs, o código de saída de quem o
Windows não abriu virando um erro legível e o filme que espera a instalação sem gastar tentativas,
sem gravar "nenhuma (erro)" e sem entrar num laço.

Nada toca no sistema: o System32 é uma pasta temporária (`ferramentas._pasta_runtime_windows`
trocada), as ferramentas e os modelos ficam numa pasta temporária, as DLLs são cabeçalhos PE
mínimos escritos pelo teste e nenhum programa roda (`analise.executar` trocado). Só a leitura da
tabela de importação abre os binários de verdade de ferramentas\\whisper, quando a pasta existe.
"""

import copy
import os
import re
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from autocortes import analise, db, ferramentas, produtor, selecao, util
from autocortes.analise import Analise
from autocortes.config import PADRAO, RAIZ, Config
from autocortes.midia import ErroMidia, ResultadoProcesso
from autocortes.painel import api
from autocortes.painel.erros import ErroHttp
from autocortes.util import ATIVIDADES, salvar_json

DLL_FALTANDO = 3221225781  # 0xC0000135 como o Python recebe (sem sinal), medido com o carregador
DLL_FALTANDO_COM_SINAL = -1073741515  # o mesmo código, como o "exit /b" do .bat escreve
DLL_32_BITS = 3221225595  # 0xC000007B
DLL_32_BITS_COM_SINAL = -1073741701
DLL_ANTIGA = 3221225785  # 0xC0000139
PASTA_WHISPER = RAIZ / "ferramentas" / "whisper"


def pe_minimo(maquina: int = ferramentas.MAQUINA_X64) -> bytes:
    """Cabeçalho DOS (MZ, e_lfanew = 0x40), assinatura PE e o cabeçalho COFF com a máquina."""
    dos = bytearray(64)
    dos[:2] = b"MZ"
    dos[0x3C:0x40] = (0x40).to_bytes(4, "little")
    return bytes(dos) + b"PE\0\0" + maquina.to_bytes(2, "little") + bytes(18)


def ler_pe(caminho: Path) -> tuple[int, set[str]]:
    """(máquina, DLLs importadas, na tabela normal e na atrasada) de um PE, só com struct."""
    d = caminho.read_bytes()
    (lfanew,) = struct.unpack_from("<I", d, 0x3C)
    maquina, n_secoes, _, _, _, tam_opcional, _ = struct.unpack_from("<HHIIIHH", d, lfanew + 4)
    opcional = lfanew + 24
    (magic,) = struct.unpack_from("<H", d, opcional)
    mais = magic == 0x20B  # PE32+
    (image_base,) = struct.unpack_from("<Q" if mais else "<I", d, opcional + (24 if mais else 28))
    (n_dirs,) = struct.unpack_from("<I", d, opcional + (108 if mais else 92))
    dirs = opcional + (112 if mais else 96)
    secoes = [struct.unpack_from("<8sIIII", d, opcional + tam_opcional + 40 * i)[1:] for i in range(n_secoes)]

    def desloc(rva: int) -> int | None:
        for vsize, va, tam_bruto, ptr in secoes:
            if va <= rva < va + max(vsize, tam_bruto):
                return ptr + rva - va
        return None

    def texto(rva: int) -> str:
        i = desloc(rva)
        return d[i:d.index(b"\0", i)].decode("ascii").lower() if i is not None else ""

    nomes: set[str] = set()
    (rva,) = struct.unpack_from("<I", d, dirs + 8 * 1)
    i = desloc(rva) if rva else None
    while i is not None:
        original, _, _, nome, primeira = struct.unpack_from("<IIIII", d, i)
        if not (original or nome or primeira):
            break
        nomes.add(texto(nome))
        i += 20
    (rva,) = struct.unpack_from("<I", d, dirs + 8 * 13) if n_dirs > 13 else (0,)
    i = desloc(rva) if rva else None
    while i is not None:
        atributos, nome, _, iat, tabela_nomes, *_ = struct.unpack_from("<8I", d, i)
        if not (nome or iat or tabela_nomes):
            break
        nomes.add(texto(nome - (0 if atributos & 1 else image_base)))  # formato antigo guarda VA
        i += 32
    return maquina, nomes


class Base(unittest.TestCase):
    """Config numa pasta temporária, com as ferramentas, os modelos e o System32 dentro dela."""

    def setUp(self):
        pasta = tempfile.TemporaryDirectory(prefix="autocortes-teste-vc-")
        self.addCleanup(pasta.cleanup)
        self.raiz = Path(pasta.name)
        self.sistema = self.raiz / "System32"
        self.sistema.mkdir()
        self.cfg = Config(copy.deepcopy(PADRAO), self.raiz / "config.toml")
        self.enterContext(patch.object(Config, "pasta_ferramentas", new_callable=PropertyMock,
                                       return_value=self.raiz / "ferramentas"))
        self.enterContext(patch.object(Config, "pasta_modelos", new_callable=PropertyMock,
                                       return_value=self.raiz / "modelos"))
        self.enterContext(patch.object(ferramentas, "_pasta_runtime_windows", return_value=self.sistema))

    @staticmethod
    def colocar(pasta, nomes, maquina=ferramentas.MAQUINA_X64):
        pasta.mkdir(parents=True, exist_ok=True)
        for nome in nomes:
            (pasta / nome).write_bytes(pe_minimo(maquina))

    def instalar(self):
        """O Visual C++ x64 "instalado": as quatro DLLs no System32 falso."""
        self.colocar(self.sistema, ferramentas.DLLS_VISUAL_CPP)

    def desinstalar(self):
        for nome in ferramentas.DLLS_VISUAL_CPP:
            (self.sistema / nome).unlink(missing_ok=True)


class TestRuntimeWhisper(Base):
    """A conferência prévia: onde cada DLL vale, e só a cópia x64 conta."""

    def test_instalacao_limpa_informa_pacote_oficial_e_dlls(self):
        resultado = ferramentas.runtime_whisper(self.cfg)
        self.assertTrue(resultado["necessario"])
        self.assertFalse(resultado["pronto"])
        self.assertEqual(resultado["faltando"], sorted(ferramentas.DLLS_VISUAL_CPP))
        with self.assertRaises(ferramentas.ErroRuntimeWhisper) as erro:
            ferramentas.garantir_whisper(self.cfg)
        self.assertIsInstance(erro.exception, ErroMidia)
        self.assertIsNone(erro.exception.codigo)
        self.assertEqual(erro.exception.faltando, resultado["faltando"])
        for nome in ("msvcp140.dll", "vcomp140.dll", "vcruntime140.dll", "vcruntime140_1.dll",
                     "https://aka.ms/vc14/vc_redist.x64.exe", "Microsoft Visual C++ x64"):
            self.assertIn(nome, str(erro.exception))

    def test_lista_tem_as_quatro_dlls_medidas(self):
        self.assertEqual(set(ferramentas.DLLS_VISUAL_CPP),
                         {"msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll", "vcomp140.dll"})

    def test_exes_ja_baixados_nao_pulam_a_checagem(self):
        cli, vad = ferramentas.caminhos_whisper(self.cfg)
        self.colocar(cli.parent, [cli.name, vad.name])
        with patch.object(ferramentas, "baixar") as baixar:
            with self.assertRaises(ferramentas.ErroRuntimeWhisper):
                ferramentas.garantir_whisper(self.cfg)
            baixar.assert_not_called()
        self.assertTrue(cli.is_file())
        self.assertTrue(vad.is_file())

    def test_sem_runtime_nao_baixa_o_whisper(self):
        with patch.object(ferramentas, "baixar") as baixar:
            with self.assertRaises(ferramentas.ErroRuntimeWhisper):
                ferramentas.garantir_whisper(self.cfg)
            baixar.assert_not_called()

    def test_depois_de_instalar_pode_usar_os_mesmos_exes_sem_baixar(self):
        cli, vad = ferramentas.caminhos_whisper(self.cfg)
        self.colocar(cli.parent, [cli.name, vad.name])
        self.assertFalse(ferramentas.runtime_whisper(self.cfg)["pronto"])
        self.instalar()
        with patch.object(ferramentas, "baixar") as baixar:
            self.assertEqual(ferramentas.garantir_whisper(self.cfg), (cli, vad))
            baixar.assert_not_called()
        resultado = ferramentas.runtime_whisper(self.cfg)
        self.assertTrue(resultado["pronto"])
        self.assertEqual((resultado["faltando"], resultado["de_32_bits"], resultado["mensagem"]), ([], [], ""))

    def test_runtime_local_junto_ao_whisper_tambem_serve(self):
        cli, _ = ferramentas.caminhos_whisper(self.cfg)
        self.colocar(cli.parent, ferramentas.DLLS_VISUAL_CPP)
        self.assertTrue(ferramentas.runtime_whisper(self.cfg)["pronto"])

    def test_runtime_parcial_informa_so_o_que_falta(self):
        self.colocar(self.sistema, ["msvcp140.dll", "vcruntime140.dll"])
        resultado = ferramentas.runtime_whisper(self.cfg)
        self.assertFalse(resultado["pronto"])
        self.assertEqual(resultado["faltando"], ["vcomp140.dll", "vcruntime140_1.dll"])
        self.assertIn("(vcomp140.dll, vcruntime140_1.dll)", resultado["mensagem"])

    def test_dlls_locais_complementam_as_do_sistema(self):
        cli, _ = ferramentas.caminhos_whisper(self.cfg)
        self.colocar(self.sistema, ["msvcp140.dll", "vcruntime140.dll"])
        self.colocar(cli.parent, ["vcruntime140_1.dll", "vcomp140.dll"])
        self.assertTrue(ferramentas.runtime_whisper(self.cfg)["pronto"])

    def test_so_a_versao_de_32_bits_nao_serve(self):
        self.colocar(self.sistema, ["msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"])
        self.colocar(self.sistema, ["vcomp140.dll"], ferramentas.MAQUINA_32_BITS)
        resultado = ferramentas.runtime_whisper(self.cfg)
        self.assertFalse(resultado["pronto"])
        self.assertEqual(resultado["faltando"], ["vcomp140.dll"])
        self.assertEqual(resultado["de_32_bits"], ["vcomp140.dll"])
        self.assertIn("só a versão de 32 bits de vcomp140.dll", resultado["mensagem"])

    def test_32_bits_ao_lado_e_64_no_sistema_serve(self):
        # medido: o carregador pula o msvcp140.dll de 32 bits ao lado do whisper-cli e usa o do System32
        cli, _ = ferramentas.caminhos_whisper(self.cfg)
        self.colocar(cli.parent, ["msvcp140.dll"], ferramentas.MAQUINA_32_BITS)
        self.instalar()
        self.assertTrue(ferramentas.runtime_whisper(self.cfg)["pronto"])

    def test_dll_que_nao_e_pe_nao_conta(self):
        for nome in ferramentas.DLLS_VISUAL_CPP:
            (self.sistema / nome).write_bytes(b"arquivo de teste")
        (self.sistema / "vcomp140.dll").write_bytes(b"MZ" + bytes(62))  # e_lfanew aponta para nada
        resultado = ferramentas.runtime_whisper(self.cfg)
        self.assertFalse(resultado["pronto"])
        self.assertEqual(resultado["faltando"], sorted(ferramentas.DLLS_VISUAL_CPP))
        self.assertEqual(resultado["de_32_bits"], [])

    def test_builds_personalizados_podem_nao_usar_o_runtime(self):
        self.cfg["ferramentas"]["whisper_cli"] = "meu-whisper/cli.exe"
        self.cfg["ferramentas"]["whisper_vad"] = "meu-whisper/vad.exe"
        resultado = ferramentas.runtime_whisper(self.cfg)
        self.assertFalse(resultado["necessario"])
        self.assertTrue(resultado["pronto"])

    def test_um_exe_personalizado_nao_dispensa_o_runtime_do_outro(self):
        self.cfg["ferramentas"]["whisper_cli"] = "meu-whisper/cli.exe"
        self.assertFalse(ferramentas.runtime_whisper(self.cfg)["pronto"])

    def test_fora_do_windows_nao_exige_o_runtime_microsoft(self):
        with patch.object(ferramentas, "_pasta_runtime_windows", return_value=None):
            resultado = ferramentas.runtime_whisper(self.cfg)
        self.assertFalse(resultado["necessario"])
        self.assertTrue(resultado["pronto"])


class TestCodigoDeSaida(Base):
    """O código de quem o Windows não abriu vira um ErroRuntimeWhisper com o que instalar."""

    def setUp(self):
        super().setUp()
        self.instalar()
        self.cli, self.vad = ferramentas.caminhos_whisper(self.cfg)

    def pacote_inteiro(self):
        self.colocar(self.cli.parent, [self.cli.name, self.vad.name, *ferramentas.DLLS_PACOTE_WHISPER])

    def test_dll_faltando_com_e_sem_sinal_vira_erro_legivel(self):
        self.pacote_inteiro()
        sem_sinal = ferramentas.erro_ao_abrir(self.cfg, self.vad, DLL_FALTANDO, "o detector de voz")
        com_sinal = ferramentas.erro_ao_abrir(self.cfg, self.vad, DLL_FALTANDO_COM_SINAL, "o detector de voz")
        for erro in (sem_sinal, com_sinal):
            self.assertIsInstance(erro, ferramentas.ErroRuntimeWhisper)
            self.assertIsInstance(erro, ErroMidia)
            self.assertEqual(erro.codigo, 0xC0000135)
            texto = str(erro)
            for trecho in ("O Windows não abriu o detector de voz (whisper-vad-speech-segments.exe)",
                           "0xC0000135", "falta uma DLL", "Microsoft Visual C++ Redistributable x64",
                           ferramentas.URL_VISUAL_CPP, "sem gastar tentativas"):
                self.assertIn(trecho, texto)
            self.assertNotIn(str(DLL_FALTANDO), texto)
        self.assertEqual(str(sem_sinal), str(com_sinal))

    def test_dll_de_32_bits_e_antiga_viram_erro_legivel(self):
        self.pacote_inteiro()
        for codigo in (DLL_32_BITS, DLL_32_BITS_COM_SINAL):
            erro = ferramentas.erro_ao_abrir(self.cfg, self.cli, codigo, "o Whisper")
            self.assertIsInstance(erro, ferramentas.ErroRuntimeWhisper)
            self.assertEqual(erro.codigo, 0xC000007B)
            for trecho in ("O Windows não abriu o Whisper (whisper-cli.exe)", "0xC000007B", "arquitetura",
                           "x64", ferramentas.URL_VISUAL_CPP):
                self.assertIn(trecho, str(erro))
        erro = ferramentas.erro_ao_abrir(self.cfg, self.cli, DLL_ANTIGA, "o Whisper")
        self.assertEqual(erro.codigo, 0xC0000139)
        self.assertIn("antiga", str(erro))
        self.assertIn(ferramentas.URL_VISUAL_CPP, str(erro))

    def test_outros_codigos_nao_sao_do_visual_cpp(self):
        self.pacote_inteiro()
        for codigo in (0, 1, 2, -1, 3221225477, None, "x"):  # 0xC0000005 é falha do próprio programa
            self.assertIsNone(ferramentas.erro_ao_abrir(self.cfg, self.cli, codigo, "o Whisper"))

    def test_pacote_incompleto_manda_baixar_de_novo(self):
        self.colocar(self.cli.parent, [self.cli.name, self.vad.name, "ggml-base.dll"])
        erro = ferramentas.erro_ao_abrir(self.cfg, self.cli, DLL_FALTANDO, "o Whisper")
        self.assertIn("Faltam arquivos do próprio whisper.cpp (whisper.dll, ggml.dll)", str(erro))
        self.assertIn("apague essa pasta", str(erro))
        self.assertNotIn("Instale ou repare", str(erro))

    def test_build_proprio_nao_manda_instalar_o_pacote_como_causa(self):
        proprio = self.raiz / "meu-whisper" / "whisper-cli.exe"
        self.cfg["ferramentas"]["whisper_cli"] = str(proprio)
        erro = ferramentas.erro_ao_abrir(self.cfg, proprio, DLL_FALTANDO, "o Whisper")
        self.assertEqual(erro.codigo, 0xC0000135)
        self.assertIn("[ferramentas]", str(erro))
        self.assertIn("confira se as DLLs dele estão junto", str(erro))
        self.assertNotIn("Instale ou repare", str(erro))


@unittest.skipUnless(os.name == "nt", "System32 e Sysnative são do Windows")
class TestPastaDoSistema(unittest.TestCase):
    def test_python_32_bits_usa_sysnative(self):
        # num Python de 32 bits, o System32 aponta para o SysWOW64 (DLLs de 32 bits)
        with patch.object(ferramentas.sys, "maxsize", 2**31 - 1), \
                patch.object(ferramentas.platform, "machine", return_value="AMD64"), \
                patch.dict(os.environ, {"PROCESSOR_ARCHITEW6432": "AMD64", "SystemRoot": r"C:\WinTeste"}):
            self.assertEqual(ferramentas._pasta_runtime_windows(), Path(r"C:\WinTeste") / "Sysnative")

    def test_python_64_bits_usa_system32(self):
        with patch.object(ferramentas.sys, "maxsize", 2**63 - 1), \
                patch.object(ferramentas.platform, "machine", return_value="AMD64"), \
                patch.dict(os.environ, {"SystemRoot": r"C:\WinTeste"}):
            self.assertEqual(ferramentas._pasta_runtime_windows(), Path(r"C:\WinTeste") / "System32")


@unittest.skipUnless(PASTA_WHISPER.is_dir(), "o whisper.cpp ainda não foi baixado nesta instalação")
class TestImportacoes(unittest.TestCase):
    def test_lista_cobre_o_que_os_binarios_importam(self):
        binarios = sorted(p for p in PASTA_WHISPER.iterdir() if p.suffix.lower() in (".exe", ".dll"))
        self.assertTrue(binarios)
        importadas: set[str] = set()
        for caminho in binarios:
            maquina, nomes = ler_pe(caminho)
            self.assertEqual(maquina, ferramentas.MAQUINA_X64, f"{caminho.name} não é x64")
            importadas |= {n for n in nomes if re.match(r"(msvcp1|vcruntime1|vcomp1|concrt1)", n)}
        self.assertEqual(importadas, set(ferramentas.DLLS_VISUAL_CPP))


class BaseAnalise(Base):
    """Um filme de 2 min com metadados, cenas e volume já no cache: a análise só chega à voz e à
    transcrição. Os EXEs, as DLLs do whisper.cpp e os modelos existem (pequenos) e nada roda."""

    def setUp(self):
        super().setUp()
        self.cfg["transcricao"]["modelo"] = "base"
        self.instalar()
        self.cli, self.vad = ferramentas.caminhos_whisper(self.cfg)
        self.colocar(self.cli.parent, [self.cli.name, self.vad.name, *ferramentas.DLLS_PACOTE_WHISPER])
        modelos = self.raiz / "modelos"
        modelos.mkdir()
        for nome in ("ggml-base.bin", ferramentas.NOME_VAD):
            (modelos / nome).write_bytes(ferramentas.MARCA_GGML + bytes(4))
        # modelos de 8 bytes: o garantir_* de verdade confere tamanho e marca, e não baixa nada
        self.enterContext(patch.dict(ferramentas.ARQUIVOS_MODELO, {"base": ("0" * 64, 8)}))
        self.enterContext(patch.object(ferramentas, "ARQUIVO_VAD", ("0" * 64, 8)))
        self.baixar = self.enterContext(patch.object(ferramentas, "baixar",
                                                     side_effect=AssertionError("não devia baixar nada")))
        self.arquivo = self.raiz / "filmes" / "Filme Teste.mkv"
        self.arquivo.parent.mkdir()
        self.arquivo.write_bytes(bytes(2048))
        self.codigos = {"vad": 0, "whisper": 0}
        self.enterContext(patch.object(analise, "executar", side_effect=self._executar))
        self.addCleanup(ATIVIDADES.limpar)

    def _executar(self, cmd, **_):
        programa = Path(cmd[0])
        if programa == self.vad:
            codigo = self.codigos["vad"]
            return ResultadoProcesso(codigo, "start = 100.00, end = 1100.00" if codigo == 0 else "")
        if programa == self.cli:
            return ResultadoProcesso(self.codigos["whisper"])
        raise AssertionError(f"programa inesperado: {cmd}")

    def pasta(self, filme_id: int = 1) -> Path:
        return analise.pasta_do_filme(self.cfg, filme_id)

    def preparar_cache(self, filme_id: int = 1, info_extra: dict | None = None) -> Path:
        pasta = self.pasta(filme_id)
        pasta.mkdir(parents=True, exist_ok=True)
        info = {
            "tamanho": self.arquivo.stat().st_size, "duracao": 120.0,
            "video": {"indice": 0, "largura": 1280, "altura": 720, "sar": [1, 1], "fps": "24/1"},
            "audio": {"indice": 1, "canais": 2, "idioma": "por", "original": False, "dub": False},
            "audios": [{"indice": 1, "idioma": "por", "original": False, "dub": False, "extra": False}],
            "legendas": [], "crop": [1280, 720, 0, 0], **(info_extra or {}),
        }
        salvar_json(pasta / "info.json", info)
        salvar_json(pasta / "cenas.json", [])
        salvar_json(pasta / "volume.json", [-30.0] * 241)
        (pasta / "audio16k.wav").write_bytes(bytes(4096))  # o "finally" da análise apaga
        return pasta

    def filme_dict(self) -> dict:
        return {"id": 1, "caminho": str(self.arquivo), "titulo": "Filme Teste", "tentativas": 0, "status": "novo"}


class TestAnalise(BaseAnalise):
    def test_vad_sem_runtime_sem_transcricao_sobe(self):
        self.codigos["vad"] = DLL_FALTANDO
        pasta = self.preparar_cache()
        with self.assertRaises(ferramentas.ErroRuntimeWhisper) as erro:
            analise.analisar(self.cfg, self.filme_dict())
        self.assertEqual(erro.exception.codigo, 0xC0000135)
        for nome in ("transcricao.json", "legenda.json", "fala.json"):
            self.assertFalse((pasta / nome).exists(), nome)

    def test_whisper_que_nao_abre_sobe_sem_virar_erro_de_transcricao(self):
        self.codigos["whisper"] = DLL_FALTANDO_COM_SINAL
        pasta = self.preparar_cache()
        filme = {**self.filme_dict(), "tentativas": analise.TENTATIVAS_TRANSCRICAO + 5}
        with self.assertRaises(ferramentas.ErroRuntimeWhisper) as erro:
            analise.analisar(self.cfg, filme)
        self.assertNotIsInstance(erro.exception, analise.ErroTranscricao)
        self.assertEqual(erro.exception.codigo, 0xC0000135)
        self.assertTrue((pasta / "fala.json").exists())  # a voz foi detectada e fica no cache
        self.assertFalse((pasta / "transcricao.json").exists())
        self.assertFalse((pasta / "legenda.json").exists())

    def test_vad_sem_runtime_com_transcricao_gravada_segue_sem_voz(self):
        self.codigos["vad"] = DLL_FALTANDO
        pasta = self.preparar_cache()
        analise.salvar_transcricao(pasta / "transcricao.json", [], "arquivo:Filme Teste.srt", "pt")
        with self.assertLogs(util.log, "WARNING") as registro:
            resultado = analise.analisar(self.cfg, self.filme_dict())
        self.assertEqual(resultado.falas, [])
        self.assertFalse((pasta / "fala.json").exists())  # tenta a voz de novo na próxima vez
        self.assertIn("Sem detecção de voz", "\n".join(registro.output))

    def test_dublado_sem_runtime_nao_usa_a_legenda_do_original(self):
        pasta = self.preparar_cache()
        info = {
            "duracao": 120.0, "audio": {"indice": 1, "idioma": "por", "original": False, "dub": False},
            "audios": [{"indice": 1, "idioma": "por", "extra": False}, {"indice": 2, "idioma": "eng", "extra": False}],
            "legendas": [{"indice": 3, "codec": "subrip", "idioma": "por", "forcada": False, "titulo": ""}],
        }
        falta = ferramentas.ErroRuntimeWhisper("falta o Visual C++", codigo=0xC0000135)
        with patch.object(analise, "transcrever", side_effect=falta), \
                patch.object(analise, "_extrair_embutida") as embutida:
            with self.assertRaises(ferramentas.ErroRuntimeWhisper):
                analise.obter_legendas(self.cfg, self.arquivo, info, pasta, pasta / "audio16k.wav", [], {},
                                       "Filme Teste", None)
        embutida.assert_not_called()


class TestProdutor(BaseAnalise):
    """O filme espera o Visual C++ sem gastar tentativas, e o motor não fica num laço."""

    def setUp(self):
        super().setUp()
        self.conn = db.conectar(self.cfg.banco)
        self.addCleanup(self.conn.close)
        t = time.time()
        self.conn.execute(
            "INSERT INTO filmes (caminho, titulo, tamanho, status, criado_em, atualizado_em) "
            "VALUES (?, 'Filme Teste', ?, 'novo', ?, ?)", (str(self.arquivo.resolve()), 2048, t, t))
        self.enterContext(patch.object(selecao, "gerar_candidatos", return_value=[]))
        self.produtor = produtor.Produtor(self.cfg, threading.Event())
        self.produtor._ultima_varredura = time.time()  # o ciclo não varre a pasta

    def linha(self):
        return self.conn.execute("SELECT * FROM filmes WHERE id=1").fetchone()

    def conferir_esperando(self, pasta: Path, status: str = "novo") -> None:
        filme = self.linha()
        self.assertEqual(filme["status"], status)
        self.assertEqual(filme["tentativas"], 0)
        self.assertIn("0xC0000135", filme["erro"])
        self.assertFalse((pasta / "transcricao.json").exists())
        self.assertFalse((pasta / "legenda.json").exists())
        self.assertIn("Microsoft Visual C++", self.produtor.espera_runtime())

    @staticmethod
    def analise_vazia():
        return Analise(info={"duracao": 120.0}, cenas=[], volume=[], falas=[], frases=[])

    def test_vad_que_nao_abre_nao_gasta_tentativas(self):
        self.codigos["vad"] = DLL_FALTANDO
        with self.assertLogs(util.log, "INFO") as registro:
            for _ in range(analise.TENTATIVAS_TRANSCRICAO + 1):
                pasta = self.preparar_cache()
                self.assertEqual(self.produtor._analisar(self.conn, self.linha()), 0)
                self.conferir_esperando(pasta)
        texto = "\n".join(registro.output)
        self.assertEqual(texto.count("'Filme Teste' espera o Microsoft Visual C++ x64"), 4)
        self.assertNotIn("nenhuma (erro)", texto)
        self.assertNotRegex(texto, r"tentativa \d+ de \d+")  # nem ErroTranscricao, nem tentativa do filme

    def test_whisper_que_nao_abre_nunca_vira_nenhuma_erro(self):
        self.codigos["whisper"] = DLL_FALTANDO
        with self.assertLogs(util.log, "INFO") as registro:
            for _ in range(analise.TENTATIVAS_TRANSCRICAO + 1):
                pasta = self.preparar_cache()
                self.assertEqual(self.produtor._analisar(self.conn, self.linha()), 0)
                self.conferir_esperando(pasta)
        texto = "\n".join(registro.output)
        self.assertNotIn("nenhuma (erro)", texto)
        self.assertNotIn("Sem legendas", texto)
        self.assertNotRegex(texto, r"tentativa \d+ de \d+")
        self.assertTrue((pasta / "fala.json").exists())
        # instalado (ou reparado): a mesma análise termina, com os mesmos EXEs e modelos
        with patch.object(analise, "transcrever", return_value=([], "pt")):
            (pasta / "audio16k.wav").write_bytes(bytes(4096))
            self.produtor._analisar(self.conn, self.linha())
        filme = self.linha()
        self.assertEqual((filme["status"], filme["tentativas"], filme["erro"]), ("analisado", 0, None))
        self.assertEqual(util.ler_json(pasta / "legenda.json")["fonte"], "whisper")
        self.baixar.assert_not_called()

    def test_falha_por_codigo_espera_30_min(self):
        self.codigos["vad"] = DLL_FALTANDO
        self.preparar_cache()
        with self.assertLogs(util.log, "WARNING"):
            self.produtor._analisar(self.conn, self.linha())
        self.assertIsNotNone(self.produtor.espera_runtime())
        self.assertEqual(self.produtor.falha_runtime()["codigo"], 0xC0000135)
        with self.assertLogs(util.log, "INFO") as registro, patch.object(analise, "analisar") as analisar:
            for _ in range(3):
                self.assertFalse(self.produtor.ciclo(self.conn))
            analisar.assert_not_called()
            # 31 min depois (sem mexer no relógio do sistema)
            self.produtor._runtime_falhou["desde"] -= produtor.ESPERA_RUNTIME + 60
            self.assertIsNone(self.produtor.falha_runtime())
            analisar.return_value = self.analise_vazia()
            self.assertTrue(self.produtor.ciclo(self.conn))
            analisar.assert_called_once()
        self.assertEqual(self.produtor._esperando_runtime, set())
        self.assertEqual(self.linha()["status"], "analisado")
        texto = "\n".join(registro.output)
        self.assertNotIn("A análise dos filmes espera:", texto)  # a falha já tinha avisado
        self.assertEqual(texto.count("A análise dos filmes volta a rodar."), 1)

    def test_sem_dlls_o_ciclo_nao_analisa_nem_entra_em_laco(self):
        self.desinstalar()
        with self.assertLogs(util.log, "INFO") as registro, \
                patch.object(analise, "analisar", return_value=self.analise_vazia()) as analisar:
            for _ in range(5):
                self.assertFalse(self.produtor.ciclo(self.conn))
            analisar.assert_not_called()
            filme = self.linha()
            self.assertEqual((filme["status"], filme["tentativas"], filme["erro"]), ("novo", 0, None))
            textos = [a["texto"] for a in ATIVIDADES.listar()]
            self.assertIn("Esperando o Microsoft Visual C++ x64 para analisar 1 filme: veja Configurações > Transcrição",
                          textos)
            self.instalar()  # o usuário instalou o pacote
            self.assertTrue(self.produtor.ciclo(self.conn))
            analisar.assert_called_once()
            self.assertFalse(self.produtor.ciclo(self.conn))
            analisar.assert_called_once()
        self.assertEqual(self.linha()["status"], "analisado")
        texto = "\n".join(registro.output)
        self.assertEqual(texto.count("A análise dos filmes espera: Falta o Microsoft Visual C++ x64"), 1)
        self.assertEqual(texto.count("A análise dos filmes volta a rodar."), 1)
        self.baixar.assert_not_called()

    def test_falha_da_conferencia_nao_segura_depois_de_instalar(self):
        # transcrição desligada, mas este filme pede o Whisper no .json dele
        self.cfg["transcricao"]["fonte"] = "nenhuma"
        self.arquivo.with_suffix(".json").write_text('{"fonte": "whisper"}', encoding="utf-8")
        self.desinstalar()
        pasta = self.preparar_cache()
        with self.assertLogs(util.log, "WARNING"):
            self.assertEqual(self.produtor._analisar(self.conn, self.linha()), 0)
        filme = self.linha()
        self.assertEqual((filme["status"], filme["tentativas"]), ("novo", 0))
        self.assertIn("Falta o Microsoft Visual C++ x64", filme["erro"])
        self.assertIsNone(self.produtor.falha_runtime()["codigo"])
        self.assertIn("Falta o Microsoft Visual C++ x64", self.produtor.espera_runtime())
        with patch.object(analise, "analisar") as analisar:
            self.assertFalse(self.produtor.ciclo(self.conn))
            analisar.assert_not_called()
        textos = [a["texto"] for a in ATIVIDADES.listar()]
        self.assertIn("Esperando o Microsoft Visual C++ x64 para analisar 1 filme: veja o Registro", textos)
        self.instalar()
        self.assertIsNone(self.produtor.espera_runtime())  # sem esperar os 30 min
        self.assertIsNone(self.produtor.falha_runtime())
        self.assertFalse((pasta / "transcricao.json").exists())

    def test_mineracao_pula_o_filme_que_espera(self):
        self.conn.execute("UPDATE filmes SET status='analisado', esgotado=0 WHERE id=1")
        self.codigos["vad"] = DLL_FALTANDO
        pasta = self.preparar_cache()  # sem transcricao.json: a voz decide a legenda
        with self.assertLogs(util.log, "WARNING"):
            self.produtor._analisar(self.conn, self.linha())
        self.conferir_esperando(pasta, status="analisado")
        self.assertEqual(self.produtor._esperando_runtime, {1})
        with patch.object(analise, "analisar") as analisar:
            self.assertIsNone(self.produtor.minerar_mais(self.conn))
            analisar.assert_not_called()

    def test_cli_analisar_para_no_primeiro_que_nao_abre(self):
        from autocortes import cli

        self.codigos["vad"] = DLL_FALTANDO
        self.preparar_cache()
        args = SimpleNamespace(config=str(self.cfg.caminho), detalhado=False, filme=None)
        with patch.object(cli, "_carregar", return_value=self.cfg), patch.object(cli, "_travar"), \
                patch.object(cli, "_conn", return_value=self.conn), \
                patch.object(produtor, "varrer_biblioteca"), \
                patch("sys.stderr") as erro, self.assertLogs(util.log, "WARNING"):
            self.assertEqual(cli.cmd_analisar(args), 1)
        escrito = "".join(c.args[0] for c in erro.write.call_args_list)
        self.assertIn("#1 Filme Teste: não analisado. O Windows não abriu o detector de voz", escrito)
        self.assertEqual(self.linha()["tentativas"], 0)
        self.desinstalar()
        with patch.object(cli, "_carregar", return_value=self.cfg), patch.object(cli, "_travar"), \
                patch.object(cli, "_conn", return_value=self.conn), \
                patch.object(produtor, "varrer_biblioteca"), patch.object(analise, "analisar") as analisar, \
                patch("sys.stderr") as erro:
            self.assertEqual(cli.cmd_analisar(args), 1)
            analisar.assert_not_called()
        escrito = "".join(c.args[0] for c in erro.write.call_args_list)
        self.assertIn("Não analiso agora: Falta o Microsoft Visual C++ x64", escrito)


class TestPainel(Base):
    """Alerta do Início e o download pelo painel."""

    def setUp(self):
        super().setUp()
        self.conn = db.conectar(self.cfg.banco)
        self.addCleanup(self.conn.close)

    def alertas(self, falha=None, rodando=True):
        produtor_falso = SimpleNamespace(falha_runtime=Mock(return_value=falha))
        motor = SimpleNamespace(rodando=rodando, produtor=produtor_falso)
        ctx = SimpleNamespace(cfg=self.cfg, conn=self.conn, painel=SimpleNamespace(motor=motor))
        return api._alertas(ctx, filmes={}, cortes={}, cobertura={}, redes={}, pautas={}, n_conferir=0)

    @staticmethod
    def do_runtime(alertas):
        return [a for a in alertas if "Visual C++" in a["texto"] or "não abriu o Whisper" in a["texto"]]

    @staticmethod
    def sera_baixado(alertas):
        return [a for a in alertas if "será baixado" in a["texto"]]

    def test_sem_dlls_alerta_erro_com_destino_da_transcricao(self):
        alertas = self.alertas()
        runtime = self.do_runtime(alertas)
        self.assertEqual(len(runtime), 1)
        self.assertEqual((runtime[0]["nivel"], runtime[0]["destino"]), ("erro", "config?aba=transcricao"))
        self.assertIn("Falta o Microsoft Visual C++ x64 (msvcp140.dll, vcomp140.dll, vcruntime140.dll, "
                      "vcruntime140_1.dll)", runtime[0]["texto"])
        self.assertEqual(self.sera_baixado(alertas), [])  # o download espera o Visual C++

    def test_transcricao_desligada_nao_avisa(self):
        self.cfg["transcricao"]["fonte"] = "nenhuma"
        alertas = self.alertas(falha={"codigo": 0xC0000135, "mensagem": "x", "desde": time.time()})
        self.assertEqual(self.do_runtime(alertas), [])
        self.assertEqual(self.sera_baixado(alertas), [])

    def test_windows_nao_abriu_com_as_dlls_no_lugar(self):
        self.instalar()
        alertas = self.alertas(falha={"codigo": 0xC0000135, "mensagem": "x", "desde": time.time()})
        runtime = self.do_runtime(alertas)
        self.assertEqual(len(runtime), 1)
        self.assertIn("O Windows não abriu o Whisper ou o detector de voz (código 0xC0000135)", runtime[0]["texto"])
        self.assertEqual((runtime[0]["nivel"], runtime[0]["destino"]), ("erro", "config?aba=transcricao"))
        # com o motor desligado, ninguém está esperando
        self.assertEqual(self.do_runtime(self.alertas(falha={"codigo": 0xC0000135}, rodando=False)), [])

    def test_com_dlls_e_sem_falha_nao_avisa(self):
        self.instalar()
        alertas = self.alertas()
        self.assertEqual(self.do_runtime(alertas), [])
        self.assertEqual(len(self.sera_baixado(alertas)), 1)  # o modelo ainda não foi baixado

    def test_baixar_sem_runtime_responde_409(self):
        ctx = SimpleNamespace(cfg=self.cfg, corpo={})
        with patch.object(ferramentas, "baixar") as baixar:
            with self.assertRaises(ErroHttp) as erro:
                api.baixar_ferramentas(ctx)
            baixar.assert_not_called()
        self.assertEqual(erro.exception.status, 409)
        self.assertIn("Falta o Microsoft Visual C++ x64", str(erro.exception))


if __name__ == "__main__":
    unittest.main()
