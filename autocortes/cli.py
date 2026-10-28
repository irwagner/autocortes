"""Linha de comando: python -m autocortes <comando>."""

from __future__ import annotations

import argparse
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import __version__

AJUDA = """\
Comandos principais:
  painel                abre o painel no navegador com o loop ligado (padrão)
  init                  cria o config.toml e as pastas
  verificar [--online]  confere FFmpeg, whisper (e o Visual C++ x64 que ele usa), fonte e
                        logins das redes
  auth <rede>           conecta youtube, tiktok ou instagram; numa rede pelo navegador (Bilibili
                        inclusive), abre a janela para entrar na conta. O Kwai é só à mão
  rodar                 loop contínuo sem painel (modo terminal)
  status                resumo de filmes, cortes e próximas postagens
  criar [pauta]         gera um vídeo do zero (narração + imagens) e põe na fila
  criar --tema "..."    a IA escreve a pauta do assunto e gera o vídeo

Comandos manuais:
  atualizar [--aplicar] confere se há versão nova no GitHub; com --aplicar (e o AutoCortes
                        fechado), instala. O painel faz isso sozinho a cada 2 dias
  baixar                baixa whisper.cpp e modelos agora
  analisar [--filme N]  analisa filmes novos (ou o filme N) sem postar
  renderizar [-n N]     edita os próximos N cortes
  postar <rede>         publica o próximo corte pronto agora, fora da agenda (à mão: cria a tarefa)
  cortes [--filme N]    lista os cortes e notas
  reanalisar N          descarta candidatos do filme N e analisa de novo
"""


def _carregar(args):
    from .config import RAIZ, ErroConfig, carregar, criar_config_se_faltar
    from .util import configurar_log, log

    caminho = Path(args.config).resolve() if args.config else RAIZ / "config.toml"
    criado = False
    if not args.config:
        criado = criar_config_se_faltar(caminho)
    try:
        cfg = carregar(caminho)
    except ErroConfig as e:
        print(e, file=sys.stderr)
        raise SystemExit(2) from e
    configurar_log(cfg.pasta_logs, args.detalhado)
    if criado:
        log.info("config.toml criado a partir do exemplo (modo simulação ligado).")
    return cfg


def _conn(cfg):
    from . import db

    return db.conectar(cfg.banco)


def _travar(cfg):
    """Os comandos que editam, criam ou postam não rodam com o AutoCortes aberto na mesma pasta.

    Com o painel (ou o modo terminal) ligado, os dois processos pegavam o mesmo corte: o vídeo
    saía duas vezes na rede, ou um apagava o arquivo que o outro estava usando. A trava é a
    mesma do painel e fica com o comando até ele terminar.
    """
    from .util import Trava

    trava = Trava(cfg.pasta_dados / "autocortes.lock")
    if not trava.adquirir(espera=2):
        print("O AutoCortes está aberto com esta pasta de dados (no painel ou no modo terminal). "
              "Faça isso pelo painel, ou feche-o antes de usar este comando.", file=sys.stderr)
        raise SystemExit(1)
    return trava


# ---------------------------------------------------------------- comandos

def cmd_init(args) -> int:
    cfg = _carregar(args)
    for pasta in (cfg.pasta_filmes, cfg.pasta_dados, cfg.pasta_modelos):
        pasta.mkdir(parents=True, exist_ok=True)
    print(f"Configuração: {cfg.caminho}")
    print(f"Coloque os filmes em: {cfg.pasta_filmes}")
    print("Depois rode: python -m autocortes verificar")
    return 0


def cmd_verificar(args) -> int:
    from . import ferramentas
    from .config import PLATAFORMAS
    from .midia import ErroMidia, filtros_disponiveis, versao_ffmpeg
    from .plataformas import ErroPublicacao, criar
    from .produtor import EXTENSOES

    cfg = _carregar(args)
    problemas = 0

    def linha(ok: bool | None, texto: str) -> None:
        nonlocal problemas
        marca = {True: "[ OK ]", False: "[FALHA]", None: "[ -- ]"}[ok]
        if ok is False:
            problemas += 1
        print(f"{marca} {texto}")

    print(f"AutoCortes {__version__}  |  modo: {'SIMULAÇÃO' if cfg.simulacao else 'REAL'}  |  {cfg.caminho}\n")
    try:
        linha(True, versao_ffmpeg(cfg["ferramentas"]["ffmpeg"]))
        filtros = filtros_disponiveis(cfg["ferramentas"]["ffmpeg"])
        faltando = {"ass", "scdet", "cropdetect", "astats", "loudnorm", "boxblur"} - filtros
        linha(not faltando, "Filtros do FFmpeg" + (f" faltando: {', '.join(sorted(faltando))}" if faltando else ""))
    except ErroMidia as e:
        linha(False, f"FFmpeg: {e}")
    try:
        versao_ffmpeg(cfg["ferramentas"]["ffprobe"])
        linha(True, "FFprobe")
    except ErroMidia as e:
        linha(False, f"FFprobe: {e}")

    cli, vad = ferramentas.caminhos_whisper(cfg)
    if cfg["transcricao"]["fonte"] == "nenhuma":
        linha(None, "Transcrição desligada")
    else:
        runtime = ferramentas.runtime_whisper(cfg)
        if runtime["necessario"]:
            # a mensagem da falta já começa com "Falta o Microsoft Visual C++ x64"
            linha(runtime["pronto"], f"Microsoft Visual C++ x64: encontrado ({', '.join(ferramentas.DLLS_VISUAL_CPP)})"
                  if runtime["pronto"] else runtime["mensagem"])
        linha(True if cli.exists() and vad.exists() else None,
              f"whisper.cpp: {cli if cli.exists() else 'será baixado no primeiro uso (ou rode: baixar)'}")
        modelo = ferramentas.caminho_modelo(cfg)
        linha(True if modelo.exists() else None,
              f"Modelo '{cfg['transcricao']['modelo']}': {modelo if modelo.exists() else 'será baixado no primeiro uso'}")

    # caminho relativo vale a partir da pasta do config, como na edição
    nome_fonte = str(cfg["edicao"]["fonte_arquivo"]).strip()
    if nome_fonte:
        fonte = cfg.caminho_de(nome_fonte)
        linha(fonte.is_file(), f"Fonte das legendas: {fonte}")
    else:
        linha(None, "Fonte das legendas: a padrão do sistema")

    filmes = [p for p in cfg.pasta_filmes.rglob("*") if p.suffix.lower() in EXTENSOES] if cfg.pasta_filmes.exists() else []
    linha(True if filmes else None, f"Pasta de filmes {cfg.pasta_filmes}: {len(filmes)} vídeo(s)")

    print()
    for nome in PLATAFORMAS:
        if not cfg[nome]["ativo"]:
            linha(None, f"{nome}: desativado")
            continue
        if cfg[nome]["envio"] == "manual":
            linha(None, f"{nome}: postagem à mão (as tarefas aparecem no Início do painel)")
            continue
        plataforma = criar(nome, cfg)
        if cfg[nome]["envio"] == "navegador" and not args.online:
            pronta, motivo = plataforma.pronta()
            linha(True if pronta else None, f"{nome}: pelo navegador ({'sessão salva' if pronta else motivo})")
            continue
        pronta, motivo = plataforma.pronta()
        if not pronta:
            linha(None if cfg.simulacao else False, f"{nome}: {motivo}")
            continue
        if args.online:
            try:
                linha(True, f"{nome}: {plataforma.verificar()}")
            except ErroPublicacao as e:
                linha(False, f"{nome}: {e}")
        else:
            linha(True, f"{nome}: login salvo (use --online para testar)")
    print()
    if cfg.simulacao:
        print("Modo simulação: os vídeos são gerados, mas nada é enviado. Troque [geral].simulacao para false quando quiser publicar.")
    return 1 if problemas else 0


def cmd_baixar(args) -> int:
    from . import ferramentas
    from .midia import ErroMidia

    cfg = _carregar(args)
    try:
        ferramentas.garantir_whisper(cfg)  # sem o Visual C++ x64, recusa antes de baixar
        ferramentas.garantir_vad(cfg)
        ferramentas.garantir_modelo(cfg)
    except ErroMidia as e:
        print(f"Não baixei: {e}", file=sys.stderr)
        return 1
    print("Ferramentas de transcrição prontas.")
    return 0


def cmd_auth(args) -> int:
    from .plataformas import ErroPublicacao, criar

    cfg = _carregar(args)
    try:
        criar(args.rede, cfg).autorizar()
    except ErroPublicacao as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelado.")
        return 1
    return 0


def cmd_rodar(args) -> int:
    from .loop import rodar
    from .modelos_visuais import conciliar_config

    cfg = _carregar(args)
    conciliar_config(cfg)  # nome do modelo visual trocado à mão no config.toml
    return rodar(cfg)


def cmd_atualizar(args) -> int:
    from . import atualizacao

    _carregar(args)  # o registro vai para o log do perfil principal
    if args.aplicar:
        return atualizacao.executar_troca(args.depois_de_fechar)
    s = atualizacao.verificar()
    local, remoto = s.get("local") or {}, s.get("remoto") or {}
    print(f"Instalação ({atualizacao.modo()}): {str(local.get('sha') or '?')[:7]} {local.get('mensagem') or ''}".rstrip())
    if remoto:
        print(f"GitHub ({atualizacao.REPOSITORIO}): {str(remoto.get('sha') or '?')[:7]} {remoto.get('mensagem') or ''}".rstrip())
    if s.get("erro"):
        print(f"Não consegui conferir: {s['erro']}")
        return 1
    if s.get("disponivel"):
        print("Há versão nova. Instale pelo painel (Configurações > Sistema) ou com: python -m autocortes atualizar --aplicar")
    elif s.get("motivo"):
        print(f"Não atualizo sozinho: {s['motivo']}")
    else:
        print("Em dia.")
    return 0


def cmd_painel(args) -> int:
    from .painel.servidor import iniciar_painel

    cfg = _carregar(args)
    abrir = cfg["painel"]["abrir_navegador"] and not getattr(args, "sem_navegador", False)
    return iniciar_painel(
        cfg,
        abrir_navegador=abrir,
        iniciar_motor=not getattr(args, "motor_desligado", False),
        porta=getattr(args, "porta", None),
    )


EXEMPLO_PAUTA = """titulo: Comece pequeno
termos: mar ao amanhecer, montanha com neblina, cidade de noite
topo: COMECE HOJE
voz: pt-BR-AntonioNeural
---
Ninguém constrói nada grande em um dia.
Você constrói em mil dias pequenos, quase iguais, quase chatos.
[pausa: 1s] O segredo é não deixar de aparecer.
"""


def cmd_criar(args) -> int:
    from . import criacao

    cfg = _carregar(args)
    trava = _travar(cfg)  # noqa: F841 (fica com o comando até o fim)
    conn = _conn(cfg)
    pasta = criacao.pasta_pautas(cfg)
    pasta.mkdir(parents=True, exist_ok=True)
    criacao.sincronizar(cfg, conn)

    alvos = []
    if args.tema:
        try:
            arquivo = criacao.pauta_do_tema(cfg, conn, args.tema)
        except criacao.ErroCriacao as e:
            print(f"Erro: {e}", file=sys.stderr)
            return 1
        print(f"A IA escreveu a pauta: {arquivo}")
        # só a pauta deste tema: as outras pendentes ficam para o motor (ou para um "criar" sem tema)
        linha = conn.execute("SELECT * FROM filmes WHERE tipo='pauta' AND caminho=?",
                             (str(arquivo.resolve()),)).fetchone()
        if linha is None:
            print(f"A pauta {arquivo.name} não entrou no banco (confira o formato)", file=sys.stderr)
            return 1
        alvos = [linha]
    elif args.pauta:
        alvo = Path(args.pauta)
        if not alvo.is_absolute() and not alvo.is_file():
            alvo = pasta / alvo
        if not alvo.is_file() and alvo.suffix != ".txt":
            alvo = alvo.with_suffix(".txt")
        if not alvo.is_file():
            print(f"Não achei a pauta {alvo}", file=sys.stderr)
            return 1
        linha = conn.execute("SELECT * FROM filmes WHERE tipo='pauta' AND caminho=?",
                             (str(alvo.resolve()),)).fetchone()
        if linha is None:
            print(f"A pauta {alvo.name} não entrou no banco (confira o formato)", file=sys.stderr)
            return 1
        if linha["status"] == "criando":
            print(f"A pauta {alvo.name} está virando vídeo agora (no painel ou no motor)", file=sys.stderr)
            return 1
        if linha["status"] != "novo":  # pauta já usada: gera outra parte a pedido
            conn.execute("UPDATE filmes SET status='novo', esgotado=0, erro=NULL, tentativas=0 WHERE id=?",
                         (linha["id"],))
        alvos = [conn.execute("SELECT * FROM filmes WHERE id=?", (linha["id"],)).fetchone()]
    else:
        alvos = conn.execute("SELECT * FROM filmes WHERE tipo='pauta' AND status='novo' ORDER BY id").fetchall()

    if not alvos:
        exemplo = pasta / "exemplo.txt"
        if not criacao.pautas(cfg) and not exemplo.exists():
            exemplo.write_text(EXEMPLO_PAUTA, encoding="utf-8")
            print(f"Criei uma pauta de exemplo em {exemplo}")
            print("Edite e rode de novo, ou use: criar --tema \"seu assunto\"")
            return 1
        print("Nenhuma pauta esperando. Escreva uma em "
              f"{pasta}, ou use: criar --tema \"seu assunto\"")
        return 0

    falhas = 0
    for linha in alvos:
        print(f"'{linha['titulo']}': narrando e montando...")
        corte_id = criacao.criar_do_banco(cfg.copia(), conn, linha)
        if corte_id is None:
            atual = conn.execute("SELECT erro FROM filmes WHERE id=?", (linha["id"],)).fetchone()
            print(f"  não deu: {(atual['erro'] if atual else '') or 'veja o registro'}", file=sys.stderr)
            falhas += 1
            continue
        corte = conn.execute("SELECT * FROM cortes WHERE id=?", (corte_id,)).fetchone()
        print(f"  vídeo: {corte['arquivo']}")
        print(f"  {corte['fim']:.1f} s, na fila como corte {corte_id}"
              + (" (aguardando aprovação)" if corte["status"] == "revisao" else ""))
    return 1 if falhas else 0


def cmd_status(args) -> int:
    from . import agenda
    from .config import PLATAFORMAS
    from .loop import resumo
    from .plataformas import criar
    from .util import fmt_data, fmt_tempo

    cfg = _carregar(args)
    conn = _conn(cfg)
    print(f"Modo: {'SIMULAÇÃO' if cfg.simulacao else 'REAL'}\n")
    filmes = conn.execute("SELECT * FROM filmes ORDER BY id").fetchall()
    print(f"Filmes ({len(filmes)}):")
    for f in filmes:
        cont = dict(conn.execute("SELECT status, COUNT(*) FROM cortes WHERE filme_id=? GROUP BY status", (f["id"],)).fetchall())
        extra = f" erro: {f['erro']}" if f["status"] == "erro" else ""
        duracao = fmt_tempo(f["duracao"]) if f["duracao"] else "?"
        print(
            f"  #{f['id']:<3} {f['titulo'][:40]:<40} {f['status']:<10} {duracao:>8}  "
            f"candidatos {cont.get('candidato', 0)}, prontos {cont.get('pronto', 0)}, concluídos {cont.get('concluido', 0)}{extra}"
        )
    print("\nRedes:")
    agora = datetime.now()
    for nome in PLATAFORMAS:
        if not cfg[nome]["ativo"]:
            print(f"  {nome:<10} desativado")
            continue
        pronta, motivo = criar(nome, cfg).pronta()
        so_imagens = not cfg[nome].get("videos", True)
        h = None if so_imagens else agenda.proximo_horario(cfg, nome, agora)
        n24 = conn.execute(
            "SELECT COUNT(*) FROM postagens WHERE plataforma=? AND status IN ('publicado','simulado') AND criado_em>=?",
            (nome, agora.timestamp() - 86400),
        ).fetchone()[0]
        if cfg[nome]["envio"] == "manual":
            esperando = conn.execute(
                "SELECT COUNT(*) FROM postagens WHERE plataforma=? AND status='aguardando'", (nome,)
            ).fetchone()[0]
            situacao = f"à mão: {esperando} tarefa(s) esperando"
        elif cfg[nome]["envio"] == "navegador":
            situacao = f"pelo navegador: {'sessão salva' if pronta else motivo}"
        else:
            situacao = "login ok" if pronta else motivo
        proximo = "só imagens" if so_imagens else f"próximo: {h.strftime('%d/%m %H:%M') if h else '-'}"
        print(f"  {nome:<10} {situacao:<45} {proximo}  últimas 24h: {n24}")
    print("\nÚltimas postagens:")
    for p in conn.execute(
        "SELECT p.*, c.parte, f.titulo FROM postagens p JOIN cortes c ON c.id=p.corte_id JOIN filmes f ON f.id=c.filme_id "
        "ORDER BY p.id DESC LIMIT 10"
    ):
        detalhe = p["url"] or p["erro"] or ""
        print(f"  {fmt_data(p['criado_em'])} {p['plataforma']:<10} {p['status']:<12} {p['titulo'][:25]} parte {p['parte']}  {detalhe[:90]}")
    print("\n" + resumo(cfg, conn))
    return 0


def cmd_analisar(args) -> int:
    from .produtor import Produtor, varrer_biblioteca

    cfg = _carregar(args)
    trava = _travar(cfg)  # noqa: F841 (fica com o comando até o fim)
    conn = _conn(cfg)
    varrer_biblioteca(cfg, conn)
    produtor = Produtor(cfg, threading.Event())
    # pauta (vídeo criado do zero) não é filme: o ffprobe num .txt virava "filme com erro"
    if args.filme:
        filmes = conn.execute("SELECT * FROM filmes WHERE id=? AND tipo='filme'", (args.filme,)).fetchall()
        if not filmes:
            pauta = conn.execute("SELECT 1 FROM filmes WHERE id=?", (args.filme,)).fetchone()
            print(f"#{args.filme} é uma pauta de vídeo criado, não um filme (use: criar)." if pauta
                  else f"Filme #{args.filme} não existe (veja os números em: status).", file=sys.stderr)
            return 1
    else:
        filmes = conn.execute("SELECT * FROM filmes WHERE status='novo' AND tipo='filme' ORDER BY id").fetchall()
    if not filmes:
        print("Nenhum filme novo para analisar (coloque vídeos em %s)." % cfg.pasta_filmes)
        return 0
    # sem o Visual C++ do Whisper, nem começa: os filmes ficam na fila sem gastar tentativas
    espera = produtor.espera_runtime()
    if espera:
        print(f"Não analiso agora: {espera}", file=sys.stderr)
        return 1
    for f in filmes:
        n = produtor.analisar(conn, f)
        espera = produtor.espera_runtime()
        if espera:  # o Windows não abriu o Whisper: os próximos dariam no mesmo
            print(f"#{f['id']} {f['titulo']}: não analisado. {espera}", file=sys.stderr)
            return 1
        print(f"#{f['id']} {f['titulo']}: {n} cortes candidatos")
    return 0


def cmd_reanalisar(args) -> int:
    import shutil

    from .analise import pasta_do_filme

    cfg = _carregar(args)
    trava = _travar(cfg)  # noqa: F841 (fica com o comando até o fim)
    conn = _conn(cfg)
    filme = conn.execute("SELECT * FROM filmes WHERE id=?", (args.filme,)).fetchone()
    if not filme:
        print(f"Filme #{args.filme} não existe.")
        return 1
    if filme["tipo"] != "filme":
        print(f"#{args.filme} é uma pauta de vídeo criado: para refazer, use o \"Recriar\" do painel.",
              file=sys.stderr)
        return 1
    # o candidato com parte já foi editado uma vez: apagá-lo levaria o histórico das postagens junto
    conn.execute("DELETE FROM cortes WHERE filme_id=? AND status='candidato' AND parte IS NULL", (filme["id"],))
    pasta = pasta_do_filme(cfg, filme["id"])
    if args.tudo:
        shutil.rmtree(pasta, ignore_errors=True)
    else:  # mantém cenas/volume/fala, refaz só as legendas (igual ao botão do painel)
        for nome in ("transcricao.json", "legenda.json"):
            (pasta / nome).unlink(missing_ok=True)
    conn.execute("UPDATE filmes SET status='novo', erro=NULL, tentativas=0, esgotado=0 WHERE id=?", (filme["id"],))
    print(f"'{filme['titulo']}' volta para a fila de análise.")
    return 0


def cmd_renderizar(args) -> int:
    from .produtor import Produtor

    cfg = _carregar(args)
    trava = _travar(cfg)  # noqa: F841 (fica com o comando até o fim)
    conn = _conn(cfg)
    produtor = Produtor(cfg, threading.Event())
    feitos = 0
    for _ in range(args.n):
        corte = produtor.proximo_candidato(conn)
        if corte is None:
            print("Não há cortes candidatos (rode: analisar).")
            break
        if produtor.renderizar(conn, corte):
            feitos += 1
    print(f"{feitos} corte(s) editado(s) em {cfg.pasta_cortes}")
    return 0


RESULTADOS_POSTAR = {
    "publicado": "publicado",
    "simulado": "simulado (modo simulação: nada foi enviado)",
    "aguardando": "tarefa de postagem à mão criada (veja o vídeo e os textos no Início do painel)",
    "conferir": "a rede recebeu o post, mas não deu para confirmar que saiu. Confira no app e marque "
                "no Início do painel (Saiu / Não saiu)",
    "so_imagens": "esta rede está só com imagens: ligue Vídeos em Redes sociais",
    "ensaio": "ensaio: tudo foi preenchido e nada foi publicado (o motivo ficou no histórico do corte)",
    "pausado": "a rede ou a internet falharam antes de o vídeo sair: nada foi publicado e a rede espera "
               "um pouco antes de tentar de novo (veja o registro)",
    "bloqueado": "a rede recusou a conta (login vencido, verificação ou configuração): nada foi publicado, e a "
                 "rede fica pausada até você resolver em Redes sociais no painel (veja o registro)",
    "ocupado": "já existe um envio para essa rede em andamento",
    "pulado": "o corte foi pulado nesta rede (o motivo ficou no histórico)",
    "falhou": "falhou (o motivo ficou no histórico e no registro)",
    "erro": "o arquivo do corte sumiu",
    "interrompido": "interrompido",
}


def cmd_postar(args) -> int:
    import threading as th
    from datetime import datetime as dt

    from .config import ROTULOS
    from .publicador import Publicador, ler_bloqueios

    cfg = _carregar(args)
    if args.real:
        cfg["geral"]["simulacao"] = False
    rede, rotulo = args.rede, ROTULOS[args.rede]
    if not cfg[rede]["ativo"]:
        print(f"O {rotulo} está desativado ([{rede}].ativo no config ou em Redes sociais no painel).",
              file=sys.stderr)
        return 1
    if not cfg[rede].get("videos", True):
        print(f"{rotulo}: {RESULTADOS_POSTAR['so_imagens']}", file=sys.stderr)
        return 1
    trava = _travar(cfg)  # noqa: F841 (fica com o comando até o fim)
    conn = _conn(cfg)
    a_mao = cfg[rede]["envio"] == "manual"
    if not cfg.simulacao and not a_mao:
        # a pausa do motor vale aqui também: postar à mão não fura um bloqueio da rede
        bloqueio = ler_bloqueios(conn).get(rede)
        if bloqueio and bloqueio.get("tipo") != "rede":
            ate = bloqueio.get("ate")
            quando = ("até você testar a conta em Redes sociais" if ate is None
                      else f"até {dt.fromtimestamp(ate).strftime('%d/%m %H:%M')}")
            print(f"O {rotulo} está pausado {quando}: {bloqueio.get('motivo')}", file=sys.stderr)
            return 1
    publicador = Publicador(cfg, th.Event())
    if args.corte:
        corte = conn.execute("SELECT * FROM cortes WHERE id=?", (args.corte,)).fetchone()
        if corte is None:
            print(f"O corte #{args.corte} não existe (veja os números em: cortes).", file=sys.stderr)
            return 1
        editado = corte["status"] in ("revisao", "pronto", "concluido") and bool(corte["arquivo"]) \
            and Path(corte["arquivo"]).is_file()
        if not editado:
            print(f"O corte #{corte['id']} ainda não foi editado (ou o arquivo sumiu): situação '{corte['status']}'.",
                  file=sys.stderr)
            return 1
        if rede == "youtube":
            limite = int(cfg["youtube"]["max_segundos"])
            if corte["fim"] - corte["inicio"] > limite + 0.5:
                print(f"O corte #{corte['id']} tem {corte['fim'] - corte['inicio']:.0f} s e o YouTube recebe só "
                      f"Shorts de até {limite} s ([youtube].max_segundos).", file=sys.stderr)
                return 1
        if corte["status"] == "revisao":  # postar à mão também aprova, como no painel
            conn.execute("UPDATE cortes SET status='pronto' WHERE id=?", (corte["id"],))
            corte = conn.execute("SELECT * FROM cortes WHERE id=?", (corte["id"],)).fetchone()
    else:
        corte = publicador.proximo_corte(conn, rede)
    if corte is None:
        print("Nenhum corte pronto para essa rede (rode: renderizar).")
        return 1
    status = publicador.postar(conn, rede, corte, manual=True)  # fora da agenda, como o "postar agora"
    publicador.atualizar_concluidos(conn)
    print(f"Resultado: {RESULTADOS_POSTAR.get(status, status)}")
    return 0 if status in ("publicado", "simulado", "aguardando", "ensaio") else 1


def cmd_cortes(args) -> int:
    import json

    from .util import fmt_tempo

    cfg = _carregar(args)
    conn = _conn(cfg)
    sql = "SELECT c.*, f.titulo FROM cortes c JOIN filmes f ON f.id=c.filme_id"
    params: tuple = ()
    if args.filme:
        sql += " WHERE c.filme_id=?"
        params = (args.filme,)
    sql += " ORDER BY c.filme_id, c.pontuacao DESC LIMIT ?"
    for c in conn.execute(sql, params + (args.limite,)):
        det = json.loads(c["detalhes"] or "{}")
        print(
            f"#{c['id']:<5} {c['titulo'][:22]:<22} {fmt_tempo(c['inicio']):>8}-{fmt_tempo(c['fim']):<8} "
            f"nota {c['pontuacao']:.2f} {c['status']:<11} parte {c['parte'] or '-':<3} "
            f"fala {det.get('fala', 0):.2f} picos {det.get('picos', 0):.2f}  {(c['frase'] or '')[:50]}"
        )
    return 0


# ---------------------------------------------------------------- entrada

def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        print("O AutoCortes precisa do Python 3.11 ou mais novo.", file=sys.stderr)
        return 2
    from .config import PLATAFORMAS
    from .util import preparar_console

    preparar_console()
    parser = argparse.ArgumentParser(
        prog="python -m autocortes",
        description="Corta filmes, edita em formato vertical e posta nas redes em loop.",
        epilog=AJUDA,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="caminho de outro config.toml")
    parser.add_argument("--detalhado", action="store_true", help="log com detalhes (comandos do FFmpeg etc.)")
    parser.add_argument("--version", action="version", version=f"AutoCortes {__version__}")
    sub = parser.add_subparsers(dest="comando", metavar="comando")

    p = sub.add_parser("painel", help="abre o painel no navegador (padrão)")
    p.add_argument("--sem-navegador", action="store_true", help="não abre o navegador")
    p.add_argument("--motor-desligado", action="store_true", help="abre o painel com o loop desligado")
    p.add_argument("--porta", type=int, help="porta do painel (padrão: [painel].porta)")
    p.set_defaults(func=cmd_painel)
    sub.add_parser("init", help="cria config.toml e pastas").set_defaults(func=cmd_init)
    p = sub.add_parser("verificar", help="confere a instalação e os logins")
    p.add_argument("--online", action="store_true", help="testa os logins nas APIs")
    p.set_defaults(func=cmd_verificar)
    sub.add_parser("baixar", help="baixa whisper.cpp e modelos").set_defaults(func=cmd_baixar)
    p = sub.add_parser("auth", help="conecta uma rede social")
    p.add_argument("rede", choices=list(PLATAFORMAS))
    p.set_defaults(func=cmd_auth)
    sub.add_parser("rodar", help="loop contínuo").set_defaults(func=cmd_rodar)
    sub.add_parser("status", help="resumo geral").set_defaults(func=cmd_status)
    p = sub.add_parser("criar", help="gera um vídeo do zero a partir de uma pauta")
    p.add_argument("pauta", nargs="?", help="arquivo da pauta (padrão: todas as que estão esperando)")
    p.add_argument("--tema", help="a IA escreve a pauta deste assunto antes de criar")
    p.set_defaults(func=cmd_criar)
    p = sub.add_parser("analisar", help="analisa filmes novos agora")
    p.add_argument("--filme", type=int, help="id do filme (veja em status)")
    p.set_defaults(func=cmd_analisar)
    p = sub.add_parser("reanalisar", help="refaz a análise de um filme")
    p.add_argument("filme", type=int)
    p.add_argument("--tudo", action="store_true", help="apaga também cenas/volume/fala em cache")
    p.set_defaults(func=cmd_reanalisar)
    p = sub.add_parser("renderizar", help="edita os próximos cortes agora")
    p.add_argument("-n", type=int, default=1)
    p.set_defaults(func=cmd_renderizar)
    p = sub.add_parser("postar", help="publica o próximo corte agora (redes à mão: cria a tarefa)")
    p.add_argument("rede", choices=list(PLATAFORMAS))
    p.add_argument("--corte", type=int, help="id de um corte específico")
    p.add_argument("--real", action="store_true", help="publica de verdade mesmo com simulacao = true")
    p.set_defaults(func=cmd_postar)
    p = sub.add_parser("cortes", help="lista cortes")
    p.add_argument("--filme", type=int)
    p.add_argument("--limite", type=int, default=50)
    p.set_defaults(func=cmd_cortes)
    p = sub.add_parser("atualizar", help="confere se há versão nova no GitHub (--aplicar instala)")
    p.add_argument("--aplicar", action="store_true", help="instala a versão nova (com o AutoCortes fechado)")
    # usado pelo painel: ele fecha os perfis e este espera as travas soltarem, depois abre de novo
    p.add_argument("--depois-de-fechar", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_atualizar)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func = cmd_painel  # sem comando: abre o painel
    elif args.func is cmd_postar and args.real:
        print("Atenção: --real publica de verdade mesmo com o modo simulação ligado.")
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\nInterrompido.")
        return 130
