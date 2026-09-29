/* AutoCortes - painel: Cortes (lista por situação e janela de detalhe de cada corte) */
"use strict";

const ABAS_CORTES = [
  ["revisao", "Aguardando aprovação", "Cortes editados esperando você aprovar. Só entram na fila de postagem depois disso."],
  ["fila", "Na fila", "Editados e prontos, na ordem em que vão sair. Cortes com estrela saem antes."],
  ["candidatos", "Trechos encontrados", "Trechos escolhidos nos filmes. O sistema edita os próximos quando a fila precisa, ou você pode editar agora."],
  ["publicados", "Publicados", "Já foram para todas as redes ativas."],
  ["descartados", "Descartados", "Trechos que você descartou ou que falharam na edição. Descartados não voltam a ser escolhidos."],
];
const CLASSE_REDE = { publicado: "ok", simulado: "sim", falhou: "erro", pulado: "aviso", interrompido: "aviso", enviando: "enviando", aguardando: "mao" };
const SINAIS = [
  ["volume", "Volume"], ["picos", "Momentos intensos"], ["fala", "Diálogo"], ["cenas", "Ritmo de cenas"],
  ["gancho", "Começo forte"], ["texto", "Falas marcantes"], ["duracao", "Duração ideal"], ["silencio", "Silêncio"],
];

const PgCortes = { aba: null, filme: "", dados: null };

/* ------------------------------------------------------------ lista */

function redesStatusHtml(c, ativas, facebook = false) {
  // o Reel da Página do Facebook aparece junto quando a opção está ligada (ou quando já foi postado)
  const redes = facebook || c.postagens.facebook ? [...ativas, "facebook"] : ativas;
  return redes.map((rede) => {
    const p = c.postagens[rede];
    const titulo = `${REDES[rede].rotulo}: ${p ? estadoPost(p.estado)[0] : "ainda não postado"}`;
    return h`<span class="rs ${p ? CLASSE_REDE[p.estado] || "" : ""}" title="${titulo}">${logoRede(rede)}</span>`;
  });
}

function acaoRapida(c, tipo, icone, titulo, classe = "") {
  return h`<button class="btn pequeno icone ${classe}" data-acao="corte-rapido" data-corte="${c.id}" data-tipo="${tipo}" title="${titulo}" aria-label="${titulo}">${ic(icone)}</button>`;
}

function cardCorteHtml(c, ativas, facebook) {
  const editando = c.status === "renderizando";
  const capa = c.miniatura
    ? h`<img src="${midia(c.miniatura)}" alt="" loading="lazy">`
    : h`<div class="capa-vazia">${editando ? h`<span class="giro"></span>` : ic("tesoura")}<small>${editando ? "Editando..." : "Ainda não editado"}</small></div>`;
  const acoes = [];
  if (c.status === "revisao") acoes.push(acaoRapida(c, "aprovar", "check", "Aprovar", "sucesso"));
  if (c.status === "candidato") acoes.push(acaoRapida(c, "editar_agora", "varinha", "Editar agora"));
  if (["candidato", "revisao", "pronto"].includes(c.status)) {
    acoes.push(c.prioridade > 0
      ? acaoRapida(c, "normal", "estrela", "Tirar a prioridade", "ativo")
      : acaoRapida(c, "priorizar", "estrela", "Postar antes dos outros"));
  }
  if (["descartado", "erro"].includes(c.status)) acoes.push(acaoRapida(c, "restaurar", "refazer", "Restaurar"));
  const meta = [`${fmtTempo(c.inicio)}–${fmtTempo(c.fim)}`, `nota ${fmtNum(c.pontuacao, 2)}`];
  if (c.visualizacoes) meta.push(`${fmtNum(c.visualizacoes)} views`);
  return h`<article class="corte">
    <div class="capa" data-acao="abrir-corte" data-corte="${c.id}" role="button" tabindex="0" aria-label="Abrir ${c.filme}${c.parte ? `, parte ${c.parte}` : ""}">
      ${capa}${c.parte ? h`<span class="parte">Parte ${c.parte}</span>` : ""}<span class="tempo">${fmtDuracao(c.duracao)}</span>
      ${c.prioridade > 0 ? h`<span class="prio" title="Com prioridade">${ic("estrela")}</span>` : ""}
      ${c.video ? h`<span class="play">${ic("play")}</span>` : ""}
    </div>
    <div class="info">
      <div class="titulo" title="${c.filme}">${c.filme}</div>
      <div class="meta">${meta.join(" · ")}</div>
      <div class="frase">${c.frase ? `“${c.frase}”` : h`<span class="texto-suave">Sem fala</span>`}</div>
      ${c.status === "erro" && c.erro ? h`<div class="meta erro-texto" title="${c.erro}">${c.erro}</div>` : ""}
      <div class="rodape-corte"><div class="redes-status">${redesStatusHtml(c, ativas, facebook)}</div><div class="acoes">${acoes}</div></div>
    </div>
  </article>`;
}

function cortesHtml(r) {
  const mostrarRevisao = r.contagem.revisao || (App.estado && App.estado.aprovacao) || PgCortes.aba === "revisao";
  const abas = ABAS_CORTES.filter(([id]) => id !== "revisao" || mostrarRevisao);
  const aba = ABAS_CORTES.find(([id]) => id === r.aba);
  const opcoesFilme = [["", "Todos os filmes"], ...r.filmes.map((f) => [String(f.id), f.titulo])];
  const acoesAba = r.aba === "revisao" && r.itens.length
    ? h`<button class="btn sucesso" data-acao="aprovar-todos">${ic("check")}Aprovar todos</button>`
    : "";
  let lista;
  if (r.itens.length) {
    lista = h`<div class="grade-cortes">${r.itens.map((c) => cardCorteHtml(c, r.ativas, r.facebook))}</div>`;
    if (r.total > r.itens.length) lista = h`${lista}<p class="nota centro">Mostrando ${r.itens.length} de ${r.total}.</p>`;
  } else {
    const vazios = {
      revisao: ["check", "Nada esperando aprovação", "Os cortes novos aparecem aqui quando a aprovação está ligada."],
      fila: ["caixa", "A fila está vazia", "O motor edita os próximos trechos quando precisa. Você também pode editar um trecho agora em \"Trechos encontrados\"."],
      candidatos: ["filme", "Nenhum trecho encontrado ainda", "Adicione filmes: cada um é analisado e rende vários trechos."],
      publicados: ["aviao", "Nada publicado ainda", "Os cortes aparecem aqui depois de ir para todas as redes ativas."],
      descartados: ["lixo", "Nada descartado", ""],
    }[r.aba];
    lista = vazioBloco(vazios[0], vazios[1], vazios[2], true);
  }
  return h`<div class="barra-ferramentas">
      <div class="abas" role="tablist">${abas.map(([id, rotulo]) => h`<button type="button" role="tab" class="aba ${r.aba === id ? "ativa" : ""}" aria-selected="${r.aba === id}" data-acao="cortes-aba" data-aba="${id}">${rotulo}<span class="n">${r.contagem[id] || 0}</span></button>`)}</div>
      <div class="grupo-botoes">${acoesAba}<select class="filtro" id="cortes-filme" aria-label="Filtrar por filme">${opcoesFilme.map(([v, t]) => h`<option value="${v}" ${v === PgCortes.filme ? raw("selected") : ""}>${t}</option>`)}</select></div>
    </div>
    <p class="nota descricao-aba">${aba ? aba[2] : ""}</p>
    ${lista}`;
}

async function carregarCortes(token = App.navegacao) {
  const params = new URLSearchParams({ aba: PgCortes.aba, limite: "200" });
  if (PgCortes.filme) params.set("filme", PgCortes.filme);
  const r = await api(`/cortes?${params}`);
  if (!App.ativa(token) || App.nomePagina !== "cortes") return;
  PgCortes.dados = r;
  const el = $("#conteudo");
  const rolagem = window.scrollY;
  montar(el, cortesHtml(r));
  window.scrollTo(0, rolagem);
  const filtro = $("#cortes-filme");
  if (filtro) {
    filtro.addEventListener("change", () => {
      PgCortes.filme = filtro.value;
      history.replaceState(null, "", PgCortes.filme ? `#/cortes?filme=${PgCortes.filme}` : "#/cortes");
      carregarCortes().catch(erroToast);
    });
  }
}

App.acoes["cortes-aba"] = async (el) => {
  PgCortes.aba = el.dataset.aba;
  await carregarCortes();
};

App.acoes["aprovar-todos"] = async (el) => {
  const ok = await confirmar({ titulo: "Aprovar todos?", texto: "Todos os cortes que esperam aprovação entram na fila de postagem.", botao: "Aprovar todos" });
  if (!ok) return;
  await ocupado(el, async () => {
    const r = await post("/cortes/aprovar-todos");
    toast(`${plural(r.aprovados, "corte aprovado", "cortes aprovados")}`);
  });
  await carregarCortes();
  atualizarEstado();
};

const MSG_ACAO = {
  aprovar: "Aprovado: o corte entrou na fila",
  editar_agora: "Editando em segundo plano. Leva alguns segundos.",
  priorizar: "Este corte vai sair antes dos outros",
  normal: "Prioridade removida",
  restaurar: "Restaurado: o trecho voltou para os encontrados",
};

App.acoes["corte-rapido"] = async (el) => {
  const tipo = el.dataset.tipo;
  await ocupado(el, () => post(`/cortes/${el.dataset.corte}/acao`, { acao: tipo }));
  toast(MSG_ACAO[tipo] || "Feito");
  if (App.pagina && App.pagina.recarregar) await App.pagina.recarregar();
  atualizarEstado();
};

App.paginas.cortes = {
  titulo: "Cortes",
  subtitulo: "Os trechos escolhidos, os vídeos editados e o que já foi postado",
  async render(el, token) {
    const q = consultaDaRota();
    PgCortes.filme = q.get("filme") || "";
    if (q.get("aba")) PgCortes.aba = q.get("aba");
    if (!PgCortes.aba) PgCortes.aba = App.estado && App.estado.numeros.revisao ? "revisao" : "fila";
    await carregarCortes(token);
  },
  async recarregar() {
    await carregarCortes();
  },
  aoEstado(e, antes) {
    if (!PgCortes.dados || modal.aberto) return;
    const mudou = !antes
      || e.numeros.na_fila !== antes.numeros.na_fila
      || e.numeros.revisao !== antes.numeros.revisao
      || e.numeros.candidatos !== antes.numeros.candidatos
      || (e.ultimas[0] && e.ultimas[0].id) !== (antes.ultimas[0] && antes.ultimas[0].id);
    if (mudou) carregarCortes().catch(() => {});
  },
};

/* ------------------------------------------------------------ janela de detalhe */

const Detalhe = { id: null, dados: null, timer: null, aguardando: null, desde: 0, videoUrl: null };

function sinaisHtml(det) {
  const barras = SINAIS.filter(([k]) => typeof det[k] === "number").map(([k, rotulo]) => {
    const v = Math.round(Math.max(0, Math.min(1, det[k])) * 100);
    return h`<div class="sinal"><small><span>${rotulo}</span><span>${v}%</span></small><div class="barra"><i style="width:${v}%"></i></div></div>`;
  });
  let abertura = null;
  if (typeof det.abertura === "number") {
    if (det.abertura > 0) abertura = ["Abertura forte: pergunta ou exclamação logo no começo", "ok"];
    else if (det.abertura < 0) abertura = ["Abertura fraca: começa com \"então\", \"mas\" ou no meio de uma frase", "aviso"];
    else abertura = ["Abertura neutra", "neutro"];
  }
  return h`<div class="sinais">${barras}</div>${abertura ? badge(abertura, true) : ""}`;
}

function botaoAcao(tipo, rotulo, icone, classe = "") {
  return h`<button class="btn pequeno ${classe}" data-acao="corte-acao" data-tipo="${tipo}">${ic(icone)}${rotulo}</button>`;
}

function acoesDetalheHtml(d) {
  const b = [];
  const s = d.status;
  if (s === "renderizando") return h`<div class="linha-botoes"><button class="btn pequeno" disabled><span class="giro"></span>Editando o vídeo...</button></div>`;
  if (s === "revisao") b.push(botaoAcao("aprovar", "Aprovar", "check", "sucesso"));
  if (s === "candidato") b.push(botaoAcao("editar_agora", "Editar agora", "varinha", "primario"));
  if (["candidato", "revisao", "pronto"].includes(s)) {
    b.push(d.prioridade > 0 ? botaoAcao("normal", "Tirar a prioridade", "estrela") : botaoAcao("priorizar", "Postar antes dos outros", "estrela"));
  }
  if (["revisao", "pronto", "erro", "descartado"].includes(s)) b.push(botaoAcao("reeditar", "Editar de novo", "refazer"));
  if (["descartado", "erro"].includes(s)) b.push(botaoAcao("restaurar", "Restaurar", "refazer"));
  if (["candidato", "revisao", "pronto", "erro"].includes(s)) b.push(botaoAcao("descartar", "Descartar", "lixo", "perigo"));
  return h`<div class="linha-botoes">${b}</div>`;
}

function cabecaDetalheHtml(d) {
  const parte = d.parte || d.textos.parte_prevista;
  const badges = [badge(ESTADO_CORTE[d.status] || [d.status, "neutro"])];
  if (d.prioridade > 0) badges.push(badge(["Com prioridade", "acento"]));
  return h`<div class="detalhe-cabeca"><h3>${d.filme} · Parte ${parte}${d.parte ? "" : " (prevista)"}</h3>${badges}</div>
    <div class="detalhe-meta">Trecho de ${fmtTempo(d.inicio)} a ${fmtTempo(d.fim)} do filme · ${fmtDuracao(d.duracao)} · nota ${fmtNum(d.pontuacao, 2)}${d.video ? ` · visual: ${d.modelo || "anterior aos modelos"}` : ""}</div>
    ${d.erro && ["erro", "candidato"].includes(d.status) ? h`<p class="erro-texto">${d.erro}</p>` : ""}`;
}

function textosDetalheHtml(d) {
  const t = d.textos;
  const fonte = { manual: ["Escrito por você", "acento"], ia: ["Escrito pela IA", "info"], modelo: ["Modelo automático", "neutro"] }[t.fonte];
  const gerandoIa = Detalhe.aguardando === "ia";
  const iaBotoes = [];
  if (t.ia_disponivel) {
    iaBotoes.push(gerandoIa
      ? h`<button class="btn pequeno" disabled><span class="giro"></span>A IA está escrevendo...</button>`
      : h`<button class="btn pequeno" data-acao="corte-acao" data-tipo="gerar_textos">${ic("varinha")}${t.ia ? "Gerar de novo com IA" : "Gerar com IA"}</button>`);
  }
  if (t.ia && !gerandoIa) iaBotoes.push(h`<button class="btn pequeno fantasma" data-acao="corte-acao" data-tipo="limpar_textos_ia">Descartar o texto da IA</button>`);
  const vazioUsa = t.ia ? "o texto da IA" : "o modelo de Configurações > Textos dos posts";
  return h`<div class="linha-botoes">${badge(fonte, true)}${iaBotoes}</div>
    <label class="campo"><span>Título</span><input type="text" id="det-titulo" maxlength="300" value="${t.titulo_custom}" placeholder="${t.titulo_padrao}"></label>
    <label class="campo"><span>Descrição</span><textarea id="det-descricao" rows="5" maxlength="5000" placeholder="${t.descricao_padrao}">${t.descricao_custom}</textarea></label>
    <p class="nota">Em branco, usa ${vazioUsa}. Aceita {filme}, {parte}, {frase} e {hashtags}.</p>
    <div class="linha-botoes"><button class="btn pequeno primario" data-acao="corte-textos">${ic("check")}Salvar os textos</button>${t.titulo_custom || t.descricao_custom ? h`<button class="btn pequeno fantasma" data-acao="corte-textos-limpar">Voltar ao automático</button>` : ""}</div>
    <details class="guia"><summary>${ic("olho")}Como o post vai sair</summary><div class="amostra-post"><b>${t.titulo}</b>${t.descricao}</div></details>`;
}

function botaoRedeHtml(d, rede, p) {
  if (p && p.estado === "aguardando") {
    return h`<button class="btn pequeno" data-acao="abrir-tarefa" data-tarefa="${p.id}">${ic("lista")}Ver a tarefa</button>`;
  }
  const podePostar = ["revisao", "pronto", "concluido"].includes(d.status) && Boolean(d.video);
  if (!podePostar || (rede === "facebook" && !d.facebook)) return "";
  if (rede === "youtube" && d.duracao > d.youtube_max + 0.5) {
    return h`<small class="texto-suave" title="O YouTube recebe só Shorts de até ${d.youtube_max} s">Longo para Short</small>`;
  }
  if (Detalhe.aguardando === `postar-${rede}`) return h`<button class="btn pequeno" disabled><span class="giro"></span>Enviando</button>`;
  if (rede !== "facebook" && d.envios[rede] === "manual") {
    return h`<button class="btn pequeno" data-acao="corte-postar" data-rede="${rede}" title="Criar a tarefa agora, fora da agenda">${ic("lista")}Postar à mão</button>`;
  }
  return h`<button class="btn pequeno" data-acao="corte-postar" data-rede="${rede}" title="Postar agora, fora da agenda">${ic("aviao")}Postar agora</button>`;
}

function redesDetalheHtml(d) {
  if (!d.ativas.length) return h`<p class="nota">Nenhuma rede ativa. Ative em Redes sociais.</p>`;
  const redes = [];
  for (const rede of d.ativas) {
    redes.push(rede);
    if (rede === "instagram" && (d.facebook || d.postagens.facebook)) redes.push("facebook");
  }
  if (!redes.includes("facebook") && d.postagens.facebook) redes.push("facebook");
  return h`<div class="redes-corte">${redes.map((rede) => {
    const p = d.postagens[rede];
    const url = p && urlSegura(p.url);
    const estado = p ? estadoPost(p.estado) : ["Ainda não postado", "neutro"];
    const partes = [];
    if (p) partes.push(fmtQuando(p.quando));
    else if (rede === "facebook") partes.push("vai junto com o Instagram");
    else if (d.envios[rede] === "manual") partes.push("à mão: vira tarefa no horário");
    if (p && p.visualizacoes !== null && p.visualizacoes !== undefined) partes.push(`${fmtNum(p.visualizacoes)} views · ${fmtNum(p.curtidas)} curtidas`);
    if (p && p.erro) partes.push(p.erro);
    return h`<div class="rede-linha">${logoRede(rede)}<span class="rede-nome"><b>${REDES[rede].rotulo}</b><small title="${partes.join(" · ")}">${partes.join(" · ") || "—"}</small></span>${badge(estado, true)}${url ? h`<a class="btn pequeno icone fantasma" href="${url}" target="_blank" rel="noopener noreferrer" title="Abrir o post" aria-label="Abrir o post no ${REDES[rede].rotulo}">${ic("externo")}</a>` : ""}${botaoRedeHtml(d, rede, p)}</div>`;
  })}</div>`;
}

function historicoHtml(d) {
  if (!d.historico.length) return "";
  return h`<details class="guia"><summary>${ic("lista")}Histórico (${d.historico.length})</summary><div class="historico">${d.historico.map((p) => h`<div>${logoRede(p.rede)}<span>${fmtQuando(p.quando)}</span>${badge(estadoPost(p.estado), true)}${p.manual ? h`<span class="texto-suave">fora da agenda</span>` : ""}${p.erro ? h`<span class="texto-suave" title="${p.erro}">${p.erro}</span>` : ""}</div>`)}</div></details>`;
}

function videoDetalheHtml(d) {
  if (d.video) {
    const poster = d.miniatura ? h`poster="${midia(d.miniatura)}"` : "";
    return h`<video src="${midia(d.video)}" ${poster} controls playsinline preload="metadata"></video>`;
  }
  const editando = d.status === "renderizando" || Detalhe.aguardando === "edicao";
  const atividade = App.estado && App.estado.atividades.find((a) => a.chave === `edicao-${d.id}`);
  return h`<div class="capa-vazia">${editando ? h`<span class="giro"></span>` : ic("tesoura")}<small>${editando ? `Editando o vídeo${atividade && atividade.pct !== null ? ` · ${Math.round(atividade.pct)}%` : "..."}` : "Este trecho ainda não foi editado"}</small></div>`;
}

function detalheHtml(d) {
  return h`<div class="detalhe-corte">
    <div class="detalhe-video" id="det-video">${videoDetalheHtml(d)}</div>
    <div class="detalhe-info">
      <div id="det-cabeca">${cabecaDetalheHtml(d)}</div>
      <div id="det-acoes">${acoesDetalheHtml(d)}</div>
      ${d.frase ? h`<p class="frase-destaque">“${d.frase}”</p>` : ""}
      <div class="rotulo-campo">Por que este trecho</div>
      ${sinaisHtml(d.detalhes || {})}
      <div class="rotulo-campo">Texto das postagens</div>
      <div id="det-textos">${textosDetalheHtml(d)}</div>
      <div class="rotulo-campo">Redes</div>
      <div id="det-redes">${redesDetalheHtml(d)}</div>
      <div id="det-historico">${historicoHtml(d)}</div>
    </div>
  </div>`;
}

function textosSujos() {
  const t = Detalhe.dados && Detalhe.dados.textos;
  const titulo = $("#det-titulo");
  const descricao = $("#det-descricao");
  if (!t || !titulo || !descricao) return false;
  return titulo.value !== t.titulo_custom || descricao.value !== t.descricao_custom;
}

function desenharDetalhe(d, completo) {
  const corpo = corpoModal();
  if (!corpo || Detalhe.id !== d.id) return;
  const anterior = Detalhe.dados;
  Detalhe.dados = d;
  if (completo || !$("#det-video")) {
    montar(corpo, detalheHtml(d));
    Detalhe.videoUrl = d.video;
    return;
  }
  if (d.video !== Detalhe.videoUrl || !d.video) {
    Detalhe.videoUrl = d.video;
    montar($("#det-video"), videoDetalheHtml(d));
  }
  montarSeMudou($("#det-cabeca"), cabecaDetalheHtml(d));
  montarSeMudou($("#det-acoes"), acoesDetalheHtml(d));
  montarSeMudou($("#det-redes"), redesDetalheHtml(d));
  montarSeMudou($("#det-historico"), historicoHtml(d));
  const textosMudaram = !anterior || JSON.stringify(anterior.textos) !== JSON.stringify(d.textos);
  const focoNosTextos = document.activeElement && document.activeElement.closest && document.activeElement.closest("#det-textos");
  if (textosMudaram && !textosSujos() && !focoNosTextos) montar($("#det-textos"), textosDetalheHtml(d));
}

function aguardar(tipo) {
  Detalhe.aguardando = tipo;
  Detalhe.desde = Date.now();
}

function conferirEspera(d) {
  const tipo = Detalhe.aguardando;
  if (!tipo) return;
  const passou = Date.now() - Detalhe.desde;
  const atividades = (App.estado && App.estado.atividades) || [];
  if (tipo === "edicao") {
    if (["revisao", "pronto", "erro"].includes(d.status)) {
      Detalhe.aguardando = null;
      toast(d.status === "erro" ? "A edição falhou. Veja o motivo no corte." : "Vídeo editado", d.status === "erro" ? "erro" : "ok");
    } else if (passou > 15 * 60000) Detalhe.aguardando = null;
  } else if (tipo === "ia") {
    const antes = Detalhe.iaAntes;
    const agora = d.textos.ia ? d.textos.ia.gerado_em : null;
    if (agora && agora !== antes) {
      Detalhe.aguardando = null;
      toast("A IA escreveu os textos");
    } else if (passou > 8000 && !atividades.some((a) => a.chave === `ia-${d.id}`)) {
      Detalhe.aguardando = null;
      toast("A IA não conseguiu escrever os textos. Veja o motivo no Registro.", "erro", 7000);
    }
  } else if (tipo.startsWith("postar-")) {
    const rede = tipo.slice(7);
    const p = d.postagens[rede];
    const terminou = p && p.estado !== "enviando" && p.quando * 1000 >= Detalhe.desde - 5000;
    if (terminou) {
      Detalhe.aguardando = null;
      const [texto, classe] = estadoPost(p.estado);
      toast(`${REDES[rede].rotulo}: ${texto}${p.erro ? ` (${p.erro})` : ""}`, classe === "erro" || classe === "aviso" ? "erro" : "ok", 7000);
    } else if (passou > 30 * 60000) Detalhe.aguardando = null;
  }
}

async function carregarDetalhe(completo = false) {
  clearTimeout(Detalhe.timer);
  const id = Detalhe.id;
  if (!id) return;
  let d;
  try {
    d = await api(`/cortes/${id}`);
  } catch (e) {
    if (Detalhe.id !== id) return;
    if (completo) { montar(corpoModal(), vazioBloco("erro", "Não foi possível abrir o corte", e.message)); return; }
    d = null;
  }
  if (Detalhe.id !== id) return;
  if (d) {
    conferirEspera(d);
    desenharDetalhe(d, completo);
  }
  const ativo = Detalhe.aguardando || (d && d.status === "renderizando");
  Detalhe.timer = setTimeout(() => carregarDetalhe(false), ativo ? 2500 : 10000);
}

async function abrirCorte(id) {
  if (!id) return;
  abrirModal({
    titulo: "Detalhes do corte",
    largura: 1080,
    corpo: h`<div class="carregando-bloco"><span class="giro"></span></div>`,
    aoFechar: () => {
      clearTimeout(Detalhe.timer);
      Detalhe.id = null;
      Detalhe.dados = null;
      Detalhe.aguardando = null;
      if (App.pagina && App.pagina.recarregar) App.pagina.recarregar();
    },
  });
  Detalhe.id = id;
  Detalhe.aguardando = null;
  await carregarDetalhe(true);
}

App.acoes["abrir-corte"] = (el) => abrirCorte(Number(el.dataset.corte));

App.acoes["corte-acao"] = async (el) => {
  const d = Detalhe.dados;
  if (!d) return;
  const tipo = el.dataset.tipo;
  if (tipo === "descartar") {
    const ok = await confirmar({ titulo: "Descartar este corte?", texto: "O vídeo editado é apagado e este trecho não volta a ser escolhido. Dá para restaurar depois em Descartados.", botao: "Descartar", perigo: true });
    if (!ok) return;
  }
  if (tipo === "reeditar") {
    await garantirConfig();
    const modeloAtual = App.config && App.config.edicao ? App.config.edicao.modelo : "";
    const semSalvar = App.pendente.edicao && !vazio(App.pendente.edicao)
      ? " As mudanças do visual que ainda não foram salvas não entram: salve antes no Estúdio." : "";
    const ok = await confirmar({ titulo: "Editar de novo?", texto: `O vídeo atual é apagado e refeito com o modelo visual em uso${modeloAtual ? ` (${modeloAtual})` : ""}, que você ajusta no Estúdio.${semSalvar}`, botao: "Editar de novo" });
    if (!ok) return;
  }
  if (tipo === "gerar_textos") Detalhe.iaAntes = d.textos.ia ? d.textos.ia.gerado_em : null;
  const novo = await ocupado(el, () => post(`/cortes/${d.id}/acao`, { acao: tipo }));
  if (tipo === "editar_agora" || tipo === "reeditar") aguardar("edicao");
  if (tipo === "gerar_textos") aguardar("ia");
  if (MSG_ACAO[tipo]) toast(MSG_ACAO[tipo]);
  if (tipo === "descartar") toast("Corte descartado");
  desenharDetalhe(novo, true);
  clearTimeout(Detalhe.timer);
  Detalhe.timer = setTimeout(() => carregarDetalhe(false), 1500);
  atualizarEstado();
};

App.acoes["corte-textos"] = async (el) => {
  const d = Detalhe.dados;
  if (!d) return;
  const novo = await ocupado(el, () => post(`/cortes/${d.id}/textos`, { titulo: $("#det-titulo").value, descricao: $("#det-descricao").value }));
  toast("Textos salvos");
  desenharDetalhe(novo, false);
  montar($("#det-textos"), textosDetalheHtml(novo));
};

App.acoes["corte-textos-limpar"] = async (el) => {
  const d = Detalhe.dados;
  if (!d) return;
  const novo = await ocupado(el, () => post(`/cortes/${d.id}/textos`, { titulo: "", descricao: "" }));
  toast("De volta aos textos automáticos");
  Detalhe.dados = novo;
  montar($("#det-textos"), textosDetalheHtml(novo));
};

App.acoes["corte-postar"] = async (el) => {
  const d = Detalhe.dados;
  if (!d) return;
  const rede = el.dataset.rede;
  const nome = rede === "facebook" ? "na Página do Facebook" : `no ${REDES[rede].rotulo}`;
  const simulacao = App.estado && App.estado.simulacao;
  const aMao = rede !== "facebook" && d.envios[rede] === "manual";
  let pergunta;
  if (aMao) {
    pergunta = { titulo: `Postar à mão ${nome}?`, texto: "O AutoCortes cria a tarefa agora, fora da agenda, com o vídeo e os textos no formato da rede. Nada é enviado: você posta pelo app ou pelo site.", botao: "Criar a tarefa" };
  } else if (simulacao) {
    pergunta = { titulo: `Simular ${nome}?`, texto: "O modo simulação está ligado: a postagem é só registrada, nada é enviado.", botao: "Simular agora" };
  } else {
    pergunta = { titulo: `Publicar agora ${nome}?`, texto: "O post sai agora, fora da agenda, e conta no limite do dia da rede.", botao: "Publicar agora" };
  }
  if (!(await confirmar(pergunta))) return;
  if (textosSujos()) {
    await post(`/cortes/${d.id}/textos`, { titulo: $("#det-titulo").value, descricao: $("#det-descricao").value });
  }
  if (aMao) {
    const r = await ocupado(el, () => post(`/cortes/${d.id}/postar`, { rede }));
    atualizarEstado();
    abrirTarefa(r.tarefa);
    return;
  }
  await ocupado(el, () => post(`/cortes/${d.id}/postar`, { rede }));
  aguardar(`postar-${rede}`);
  toast(simulacao ? `Simulando ${nome}...` : `Publicando ${nome}...`, "info");
  montarSeMudou($("#det-redes"), redesDetalheHtml(d));
  clearTimeout(Detalhe.timer);
  Detalhe.timer = setTimeout(() => carregarDetalhe(false), 1500);
};
