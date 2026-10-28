/* AutoCortes - painel: Cortes (lista por situação e janela de detalhe de cada corte) */
"use strict";

const ABAS_CORTES = [
  ["revisao", "Aguardando aprovação", "Cortes editados esperando você aprovar. Só entram na fila de postagem depois disso."],
  ["fila", "Na fila", "Editados e prontos, na ordem em que vão sair. Cortes com estrela saem antes."],
  ["candidatos", "Trechos encontrados", "Trechos escolhidos nos filmes. O sistema edita os próximos quando a fila precisa, ou você pode editar agora."],
  ["publicados", "Publicados", "Já foram para todas as redes ativas."],
  ["descartados", "Descartados", "Trechos que você descartou ou que falharam na edição. Descartados não voltam a ser escolhidos."],
];
const CLASSE_REDE = {
  publicado: "ok", simulado: "sim", falhou: "erro", pulado: "aviso", interrompido: "aviso", enviando: "enviando",
  aguardando: "mao", conferir: "aviso", ensaio: "sim",
};
const SINAIS = [
  ["volume", "Volume"], ["picos", "Momentos intensos"], ["fala", "Diálogo"], ["cenas", "Ritmo de cenas"],
  ["gancho", "Começo forte"], ["texto", "Falas marcantes"], ["duracao", "Duração ideal"], ["silencio", "Silêncio"],
];

const PgCortes = { aba: null, filme: "", dados: null };
// seleção em lote: os marcados sobrevivem ao redesenho ao vivo e saem ao trocar de aba ou de filtro
const corSel = { ativo: false, ids: new Set(), emCurso: null };

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
  // publicado e sem o arquivo: o vídeo foi apagado para liberar espaço (o corte e o histórico ficam)
  const apagado = c.status === "concluido";
  const capa = c.miniatura
    ? h`<img src="${midia(c.miniatura)}" alt="" loading="lazy">`
    : h`<div class="capa-vazia">${editando ? h`<span class="giro"></span>` : ic(apagado ? "lixo" : "tesoura")}<small>${editando ? "Editando..." : apagado ? "Vídeo apagado" : "Ainda não editado"}</small></div>`;
  const sel = corSel.ativo;
  const acoes = [];
  if (!sel) { // na seleção, o clique no cartão marca: os botões rápidos saem
    if (c.status === "revisao") acoes.push(acaoRapida(c, "aprovar", "check", "Aprovar", "sucesso"));
    if (c.status === "candidato") acoes.push(acaoRapida(c, "editar_agora", "varinha", "Editar agora"));
    if (["candidato", "revisao", "pronto"].includes(c.status)) {
      acoes.push(c.prioridade > 0
        ? acaoRapida(c, "normal", "estrela", "Tirar a prioridade", "ativo")
        : acaoRapida(c, "priorizar", "estrela", "Postar antes dos outros"));
    }
    if (["descartado", "erro"].includes(c.status) && !c.criado) acoes.push(acaoRapida(c, "restaurar", "refazer", "Restaurar"));
  }
  // vídeo criado de uma pauta não tem trecho de origem nem nota de seleção
  const meta = c.criado
    ? ["criado do zero", ...(c.detalhes && c.detalhes.clipes ? [`${c.detalhes.clipes} clipes`] : [])]
    : [`${fmtTempo(c.inicio)}–${fmtTempo(c.fim)}`, `nota ${fmtNum(c.pontuacao, 2)}`];
  if (c.visualizacoes) meta.push(`${fmtNum(c.visualizacoes)} views`);
  if (c.imagens_publicadas) meta.push(plural(c.imagens_publicadas, "imagem publicada", "imagens publicadas"));
  const marcado = sel && corSel.ids.has(c.id);
  // a caixa fica fora de qualquer data-acao (o preventDefault do clique global desfaria a marcação);
  // na seleção, o clique na capa ou no texto marca em vez de abrir o detalhe, e só a caixa recebe o Tab
  const caixa = sel
    ? h`<label class="corte-marca"><input type="checkbox" data-corte="${c.id}" ${marcado ? raw("checked") : ""} aria-label="${corSelRotuloCaixa(c)}"></label>`
    : "";
  const capaTopo = sel
    ? h`<div class="capa" data-acao="cortes-sel-cartao" data-corte="${c.id}">`
    : h`<div class="capa" data-acao="abrir-corte" data-corte="${c.id}" role="button" tabindex="0" aria-label="Abrir ${c.filme}${c.parte ? `, parte ${c.parte}` : ""}">`;
  return h`<article class="corte${sel ? " corte-sel" : ""}${marcado ? " corte-sel-marcado" : ""}" data-corte="${c.id}">
    ${caixa}${capaTopo}
      ${capa}${c.parte ? h`<span class="parte">Parte ${c.parte}</span>` : ""}<span class="tempo">${fmtDuracao(c.duracao)}</span>
      ${c.prioridade > 0 ? h`<span class="prio" title="Com prioridade">${ic("estrela")}</span>` : ""}
      ${c.video ? h`<span class="play">${ic("play")}</span>` : ""}
    </div>
    <div class="info"${sel ? h` data-acao="cortes-sel-cartao" data-corte="${c.id}"` : ""}>
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
  let acoesAba = "";
  // na seleção, as ações ficam na barra do rodapé: o "Aprovar todos" aprova todos do banco, sem olhar
  // a seleção nem o filtro, e se confundiria com o "Aprovar" dos escolhidos
  if (!corSel.ativo && r.aba === "revisao" && r.itens.length) {
    acoesAba = h`<button class="btn sucesso" data-acao="aprovar-todos">${ic("check")}Aprovar todos</button>`;
  } else if (!corSel.ativo && r.aba === "fila" && r.itens.some((c) => !c.criado)) {
    acoesAba = h`<button class="btn" data-acao="refazer-fila" title="Edita de novo os cortes da fila com o modelo visual em uso">${ic("refazer")}Refazer a fila</button>`;
  }
  // só com a API que conhece a seleção (lote: true, ver listar_cortes no api.py)
  const selecionar = r.lote && r.itens.length
    ? h`<button type="button" class="btn${corSel.ativo ? " ativo" : ""}" id="cortes-selecionar" data-acao="cortes-selecionar" aria-pressed="${corSel.ativo ? "true" : "false"}" title="${corSel.ativo ? "Sair da seleção (Esc)" : "Marcar alguns cortes ou todos e aplicar uma ação de uma vez"}">${ic("selecionar")}Selecionar</button>`
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
      <div class="grupo-botoes">${acoesAba}${selecionar}<select class="filtro" id="cortes-filme" aria-label="Filtrar por filme">${opcoesFilme.map(([v, t]) => h`<option value="${v}" ${v === PgCortes.filme ? raw("selected") : ""}>${t}</option>`)}</select></div>
    </div>
    <p class="nota descricao-aba">${aba ? aba[2] : ""}</p>
    ${lista}`;
}

async function carregarCortes(token = App.navegacao) {
  const params = new URLSearchParams({ aba: PgCortes.aba, limite: "200" });
  if (PgCortes.filme) params.set("filme", PgCortes.filme);
  const r = await api(`/cortes?${params}`);
  if (!App.ativa(token) || App.nomePagina !== "cortes") return;
  corSelPodar(r);
  PgCortes.dados = r;
  corSelRedesenhar();
}

App.acoes["cortes-aba"] = async (el) => {
  if (el.dataset.aba !== PgCortes.aba) corSelLimpar(); // outra aba, outra lista: a seleção recomeça
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

App.acoes["refazer-fila"] = async (el) => {
  const ok = await confirmar({
    titulo: "Refazer a fila com o visual atual?",
    texto: "Edito de novo, um de cada vez, os cortes da fila e os que esperam aprovação, com o modelo visual em uso. Cada um volta para o mesmo lugar da fila. Onde o corte já saiu, nada muda; as redes que faltam recebem o vídeo novo. Vídeos criados de pauta ficam como estão.",
    botao: "Refazer a fila",
  });
  if (!ok) return;
  const r = await ocupado(el, () => post("/cortes/refazer-fila"));
  toast(`Refazendo ${plural(r.cortes, "corte", "cortes")} em segundo plano. Acompanhe no Início.`, "info", 6000);
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
  sair() {
    // a barra da seleção fica no body: sem isto ela continuaria à vista nas outras telas
    corSel.ativo = false;
    corSel.ids.clear();
    corSel.emCurso = null;
    corSelDesenharBarra();
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

/* ------------------------------------------------------------ seleção em lote */

// cada ação vale só para os cortes em que faz sentido: as mesmas regras dos botões do cartão e do detalhe
const corSelRegras = {
  aprovar: (c) => c.status === "revisao",
  priorizar: (c) => ["revisao", "pronto"].includes(c.status) && !(c.prioridade > 0),
  normal: (c) => ["revisao", "pronto"].includes(c.status) && c.prioridade > 0,
  reeditar: (c) => ["revisao", "pronto"].includes(c.status) && !c.criado,
  restaurar: (c) => ["erro", "descartado"].includes(c.status) && !c.criado,
  apagar_video: (c) => c.status === "concluido" && Boolean(c.video),
  descartar: (c) => ["candidato", "revisao", "pronto", "erro"].includes(c.status),
};
// ordem na barra: [ação, rótulo, ícone, classe, dica]
const corSelBotoes = [
  ["aprovar", "Aprovar", "check", "sucesso", ""],
  ["priorizar", "Priorizar", "estrela", "", "Saem antes dos outros na fila"],
  ["normal", "Tirar a prioridade", "estrela", "", ""],
  ["reeditar", "Editar de novo", "refazer", "", "Refaz o vídeo com o modelo visual em uso"],
  ["restaurar", "Restaurar", "refazer", "", "Voltam para os trechos encontrados"],
  ["apagar_video", "Apagar os vídeos", "lixo", "perigo", "Libera espaço no disco: os posts e o histórico ficam"],
  ["descartar", "Descartar", "lixo", "perigo", ""],
];
const corSelFeito = {
  descartar: (n) => (n === 1 ? "1 corte descartado" : `${fmtNum(n)} cortes descartados`),
  restaurar: (n) => (n === 1 ? "1 corte restaurado: voltou para os trechos encontrados" : `${fmtNum(n)} cortes restaurados: voltaram para os trechos encontrados`),
  aprovar: (n) => (n === 1 ? "1 corte aprovado: entrou na fila" : `${fmtNum(n)} cortes aprovados: entraram na fila`),
  priorizar: (n) => (n === 1 ? "1 corte vai sair antes dos outros" : `${fmtNum(n)} cortes vão sair antes dos outros`),
  normal: (n) => `Prioridade removida de ${plural(n, "corte", "cortes")}`,
  apagar_video: (n) => (n === 1 ? "1 vídeo apagado" : `${fmtNum(n)} vídeos apagados`),
  reeditar: (n) => (n === 1 ? "Editando de novo 1 corte em segundo plano. Acompanhe no Início."
    : `Editando de novo ${fmtNum(n)} cortes em segundo plano, um de cada vez. Acompanhe no Início.`),
};
// recusas que o servidor marca com um motivo: [um corte, vários]
const corSelMotivos = {
  enviando: ["está sendo enviado agora", "estão sendo enviados agora"],
  arquivo_aberto: ["está com o vídeo aberto em outro programa", "estão com o vídeo aberto em outro programa"],
  fora: ["não está mais na fila nem aguardando aprovação", "não estão mais na fila nem aguardando aprovação"],
};

function corSelRotuloCaixa(c) {
  return c.parte ? `Selecionar a Parte ${c.parte} de ${c.filme}` : `Selecionar o trecho de ${fmtTempo(c.inicio)} a ${fmtTempo(c.fim)} de ${c.filme}`;
}

/** Os cortes marcados que estão na lista mostrada, na ordem da tela. */
function corSelItens() {
  const itens = (PgCortes.dados && PgCortes.dados.itens) || [];
  return itens.filter((c) => corSel.ids.has(c.id));
}

/** Lista nova (redesenho ao vivo, recarga): continuam marcados só os cortes que ainda estão nela. */
function corSelPodar(r) {
  if (!corSel.ativo) return;
  if (!r.lote || !r.itens.length) {
    corSel.ativo = false;
    corSel.ids.clear();
    return;
  }
  const presentes = new Set(r.itens.map((c) => c.id));
  for (const id of [...corSel.ids]) if (!presentes.has(id)) corSel.ids.delete(id);
}

/** Desenha a lista carregada (grade e barra). O foco volta para a mesma caixa ou para o Selecionar. */
function corSelRedesenhar() {
  const r = PgCortes.dados;
  const el = $("#conteudo");
  if (r && el) {
    const ativo = document.activeElement;
    let foco = null;
    if (ativo && el.contains(ativo)) {
      if (ativo.id === "cortes-selecionar") foco = "#cortes-selecionar";
      else if (ativo.matches(".corte-marca input")) foco = `.corte-marca input[data-corte="${ativo.dataset.corte}"]`;
    }
    const rolagem = window.scrollY;
    montar(el, cortesHtml(r));
    window.scrollTo(0, rolagem);
    const filtro = $("#cortes-filme");
    if (filtro) {
      filtro.addEventListener("change", () => {
        PgCortes.filme = filtro.value;
        corSelLimpar(); // outro filme, outra lista: a seleção recomeça
        history.replaceState(null, "", PgCortes.filme ? `#/cortes?filme=${PgCortes.filme}` : "#/cortes");
        carregarCortes().catch(erroToast);
      });
    }
    if (foco) ($(foco, el) || $("#cortes-selecionar", el) || el).focus({ preventScroll: true });
  }
  corSelDesenharBarra();
}

/** A barra do rodapé fica no body (como a do Salvar): o redesenho ao vivo troca o #conteudo inteiro. */
function corSelBarra() {
  let barra = $("#corsel-barra");
  if (!barra) {
    barra = elementoDe(h`<div class="barra-selecao" id="corsel-barra" role="region" aria-label="Seleção de cortes"><div class="corsel-lado"><span class="corsel-contagem" id="corsel-contagem" aria-live="polite"></span><span class="corsel-marcar" id="corsel-marcar"></span></div><div class="corsel-acoes" id="corsel-acoes"></div></div>`);
    document.body.appendChild(barra);
    // em tela estreita a barra ganha linhas: os avisos e o fim da página sobem junto
    if (window.ResizeObserver) {
      new ResizeObserver(() => document.body.style.setProperty("--corsel-altura", `${barra.offsetHeight}px`)).observe(barra);
    }
  }
  return barra;
}

const corSelChave = (el) => {
  const b = el.closest("[data-acao]");
  return b ? `${b.dataset.acao}|${b.dataset.lote || ""}` : null;
};

function corSelFocar(barra, chave) {
  const livres = [...barra.querySelectorAll("button")].filter((b) => !b.disabled);
  const alvo = livres.find((b) => corSelChave(b) === chave) || livres[0];
  if (alvo) alvo.focus({ preventScroll: true });
}

/** Quantos estão marcados e as ações que valem para eles, cada uma com a sua contagem. */
function corSelDesenharBarra(focar = null) {
  const barra = corSelBarra();
  const visivel = corSel.ativo && App.nomePagina === "cortes";
  barra.classList.toggle("visivel", visivel);
  if (!visivel) return;
  const ativo = document.activeElement;
  // os botões são trocados: quem estava com o foco recebe de volta (ou o primeiro que der)
  const chave = focar || (ativo && ativo !== barra && barra.contains(ativo) ? corSelChave(ativo) : null);
  const r = PgCortes.dados || { itens: [], total: 0 };
  const marcados = corSelItens();
  const n = marcados.length;
  const todos = r.itens.length;
  const travar = (sim) => (sim || corSel.emCurso ? raw("disabled") : "");
  $("#corsel-contagem").textContent = n === 0 ? "Nenhum selecionado" : n === 1 ? "1 selecionado" : `${fmtNum(n)} selecionados`;
  const dica = r.total > todos ? `Marca os ${fmtNum(todos)} mostrados, de ${fmtNum(r.total)} nesta aba` : `Marca os ${fmtNum(todos)} mostrados`;
  montarSeMudou($("#corsel-marcar"), h`<button type="button" class="btn pequeno" data-acao="cortes-sel-todos" title="${dica}" ${travar(n >= todos)}>Selecionar todos (${fmtNum(todos)})</button><button type="button" class="btn pequeno fantasma" data-acao="cortes-sel-limpar" ${travar(!n)}>Limpar</button>`);
  const botoes = [];
  for (const [acao, rotulo, icone, classe, titulo] of corSelBotoes) {
    const k = marcados.filter(corSelRegras[acao]).length;
    if (!k) continue;
    botoes.push(h`<button type="button" class="btn pequeno ${classe}${corSel.emCurso === acao ? " carregando" : ""}" data-acao="cortes-sel-acao" data-lote="${acao}" ${titulo ? h`title="${titulo}"` : ""} ${travar(false)}>${ic(icone)}${rotulo} (${fmtNum(k)})</button>`);
  }
  let aviso = "";
  if (!n) aviso = h`<span class="nota">Clique nos cartões ou nas caixas para marcar.</span>`;
  else if (!botoes.length) aviso = h`<span class="nota">Nenhuma ação vale para os selecionados.</span>`;
  montarSeMudou($("#corsel-acoes"), h`${aviso}${botoes}<button type="button" class="btn pequeno fantasma" data-acao="cortes-sel-sair" title="Sair da seleção (Esc)" ${travar(false)}>Cancelar</button>`);
  if (chave && !barra.contains(document.activeElement)) corSelFocar(barra, chave);
}

/** Marca e desmarca no lugar, sem redesenhar a grade (o foco e a rolagem ficam). */
function corSelAtualizarCartoes() {
  for (const cartao of document.querySelectorAll("#conteudo article.corte[data-corte]")) {
    const marcado = corSel.ativo && corSel.ids.has(Number(cartao.dataset.corte));
    cartao.classList.toggle("corte-sel-marcado", marcado);
    const caixa = $(".corte-marca input", cartao);
    if (caixa) caixa.checked = marcado;
  }
}

function corSelMarcar(id, marcado) {
  if (marcado) corSel.ids.add(id);
  else corSel.ids.delete(id);
  corSelAtualizarCartoes();
  corSelDesenharBarra();
}

function corSelLimpar() {
  corSel.ids.clear();
  corSelAtualizarCartoes();
  corSelDesenharBarra();
}

/** Sai da seleção (Cancelar, Esc, ou uma ação que deu certo em todos). */
function corSelSair() {
  corSel.ativo = false;
  corSel.ids.clear();
  corSelRedesenhar();
  ($("#cortes-selecionar") || $("#conteudo")).focus({ preventScroll: true });
}

/** A confirmação das ações que apagam ou refazem (null = sem pergunta, como no botão do cartão). */
async function corSelPergunta(acao, alvo) {
  const n = alvo.length;
  const um = n === 1;
  if (acao === "descartar") {
    const comVideo = alvo.some((c) => c.video);
    const postados = alvo.some((c) => Object.keys(c.postagens || {}).length);
    // o descartar pula todas as tarefas à mão do corte, e ele pode esperar numa tarefa por rede
    const tarefas = alvo.reduce((n, c) => n + Object.values(c.postagens || {}).filter((p) => p.estado === "aguardando").length, 0);
    const criados = alvo.filter((c) => c.criado).length;
    const partes = [comVideo
      ? (um ? "O vídeo editado sai do disco e este trecho não volta a ser escolhido." : "Os vídeos editados saem do disco e esses trechos não voltam a ser escolhidos.")
      : (um ? "Este trecho não volta a ser escolhido." : "Esses trechos não voltam a ser escolhidos.")];
    if (postados) partes.push(`O que já foi postado continua nas redes e no histórico, e as redes que faltavam não recebem mais ${um ? "este corte" : "esses cortes"}.`);
    if (tarefas) partes.push(tarefas === 1 ? "A tarefa de postagem à mão que estava esperando é pulada." : `As ${fmtNum(tarefas)} tarefas de postagem à mão que estavam esperando são puladas.`);
    if (!criados) {
      partes.push(um ? "Dá para restaurar depois em Descartados: o trecho volta para os encontrados." : "Dá para restaurar depois em Descartados: os trechos voltam para os encontrados.");
    } else if (criados < n) {
      partes.push("Dá para restaurar depois em Descartados os cortes de filme: os trechos voltam para os encontrados. Vídeo criado de pauta não volta por Restaurar: para gerar outro, use Criar de novo no detalhe dele.");
    } else {
      partes.push(um ? "Este vídeo foi criado de uma pauta e não volta por Restaurar: para gerar outro, use Criar de novo no detalhe dele."
        : "Esses vídeos foram criados de pautas e não voltam por Restaurar: para gerar outros, use Criar de novo no detalhe de cada um.");
    }
    return { titulo: um ? "Descartar 1 corte?" : `Descartar ${fmtNum(n)} cortes?`, texto: partes.join(" "), botao: um ? "Descartar" : `Descartar ${fmtNum(n)}`, perigo: true };
  }
  if (acao === "apagar_video") {
    return {
      titulo: um ? "Apagar o vídeo deste corte?" : `Apagar os vídeos de ${fmtNum(n)} cortes?`,
      texto: "O vídeo, a legenda e a miniatura saem do disco para liberar espaço. Os posts continuam nas redes, e o corte e o histórico continuam aqui em Publicados. Não dá para desfazer.",
      botao: um ? "Apagar o vídeo" : `Apagar ${fmtNum(n)} vídeos`,
      perigo: true,
    };
  }
  if (acao === "reeditar") {
    await garantirConfig();
    const modelo = App.config && App.config.edicao ? App.config.edicao.modelo : "";
    const qual = modelo ? ` (${modelo})` : "";
    const semSalvar = App.pendente.edicao && !vazio(App.pendente.edicao)
      ? " As mudanças do visual que ainda não foram salvas não entram: salve antes no Estúdio." : "";
    return {
      titulo: um ? "Editar de novo este corte?" : `Editar de novo ${fmtNum(n)} cortes?`,
      texto: um
        ? `O vídeo atual é apagado e refeito com o modelo visual em uso${qual}, que você ajusta no Estúdio. Ele volta para o mesmo lugar da fila.${semSalvar}`
        : `O vídeo atual de cada um é apagado e refeito, um de cada vez, com o modelo visual em uso${qual}, que você ajusta no Estúdio. Cada um volta para o mesmo lugar da fila.${semSalvar}`,
      botao: "Editar de novo",
    };
  }
  return null; // aprovar, priorizar, tirar a prioridade e restaurar não perguntam
}

/** O aviso do resultado: quantos mudaram e, agrupados pelo motivo, os que não deram certo. */
function corSelResultado(acao, feitos, recusados) {
  const grupos = new Map();
  for (const f of recusados) {
    const chave = corSelMotivos[f.motivo] ? f.motivo : String(f.erro || "erro sem detalhes").replace(/\.\s*$/, "");
    grupos.set(chave, (grupos.get(chave) || 0) + 1);
  }
  const falhas = [...grupos].map(([chave, k]) => {
    const frase = corSelMotivos[chave] ? corSelMotivos[chave][k === 1 ? 0 : 1] : chave.charAt(0).toLowerCase() + chave.slice(1);
    return `${fmtNum(k)} não: ${frase}`;
  }).join("; ");
  const feito = corSelFeito[acao](feitos);
  if (!recusados.length) toast(feito, acao === "reeditar" ? "info" : "ok", acao === "reeditar" ? 6000 : 4500);
  else if (feitos) toast(`${feito.replace(/\.$/, "")}. ${falhas}.`, "info", 8000);
  else toast(`Nenhum corte mudou. ${falhas}.`, "erro", 8000);
}

function corSelAoMudar(ev) {
  const caixa = ev.target;
  if (!corSel.ativo || App.nomePagina !== "cortes" || !caixa.matches || !caixa.matches(".corte-marca input")) return;
  corSelMarcar(Number(caixa.dataset.corte), caixa.checked);
}

function corSelTecla(ev) {
  if (ev.key !== "Escape" || ev.defaultPrevented || !corSel.ativo || corSel.emCurso || App.nomePagina !== "cortes") return;
  if (modal.aberto || document.querySelector(".modal-fundo.sobreposto")) return; // o Esc fecha a janela, não a seleção
  ev.preventDefault();
  corSelSair();
}

// a caixa não tem data-acao: a marcação chega pelo change, ouvido uma vez para a página inteira
$("#conteudo").addEventListener("change", corSelAoMudar);
document.addEventListener("keydown", corSelTecla);

App.acoes["cortes-selecionar"] = () => {
  if (corSel.emCurso) return;
  if (corSel.ativo) {
    corSelSair();
    return;
  }
  corSel.ativo = true;
  corSel.ids.clear();
  corSelRedesenhar();
};

App.acoes["cortes-sel-cartao"] = (el) => {
  if (!corSel.ativo) return;
  const id = Number(el.dataset.corte);
  corSelMarcar(id, !corSel.ids.has(id));
};

App.acoes["cortes-sel-todos"] = () => {
  if (!corSel.ativo) return;
  for (const c of (PgCortes.dados && PgCortes.dados.itens) || []) corSel.ids.add(c.id);
  corSelAtualizarCartoes();
  corSelDesenharBarra();
};

App.acoes["cortes-sel-limpar"] = () => corSelLimpar();

App.acoes["cortes-sel-sair"] = () => corSelSair();

App.acoes["cortes-sel-acao"] = async (el) => {
  const acao = el.dataset.lote;
  const regra = corSelRegras[acao];
  if (!regra || !corSel.ativo || corSel.emCurso) return;
  const alvo = corSelItens().filter(regra);
  if (!alvo.length) return;
  const token = App.navegacao;
  const pergunta = await corSelPergunta(acao, alvo);
  if (pergunta && !(await confirmar(pergunta))) return;
  if (!App.ativa(token) || !corSel.ativo || corSel.emCurso) return; // saiu da tela ou da seleção enquanto perguntava
  const ids = alvo.map((c) => c.id);
  const chave = `cortes-sel-acao|${acao}`;
  // sem o ocupado(): o redesenho da barra troca o botão, e o emCurso já desenha o carregando
  corSel.emCurso = acao;
  corSelDesenharBarra(chave);
  let r;
  try {
    r = acao === "reeditar" ? await post("/cortes/refazer-fila", { ids }) : await post("/cortes/lote", { acao, ids });
  } catch (e) {
    corSel.emCurso = null;
    if (App.ativa(token)) {
      corSelDesenharBarra(chave);
      carregarCortes().catch(() => {}); // um erro no meio do lote: o que já mudou aparece
    }
    throw e;
  }
  corSel.emCurso = null;
  const aceitos = acao === "reeditar" ? r.ids : r.feitos;
  const recusados = acao === "reeditar" ? r.fora.map((id) => ({ id, motivo: "fora" })) : r.falhas;
  corSelResultado(acao, aceitos.length, recusados);
  try {
    if (!App.ativa(token)) return;
    const todosForam = !recusados.length;
    if (todosForam) {
      corSel.ativo = false; // deu certo em todos: sai da seleção
      corSel.ids.clear();
    } else {
      // os que deram certo saem da seleção; os recusados ficam marcados, para você ver quais foram
      for (const id of aceitos) corSel.ids.delete(id);
    }
    corSelDesenharBarra(todosForam ? null : chave);
    await carregarCortes().catch((e) => {
      corSelRedesenhar(); // sem a lista nova, a grade ao menos acompanha a seleção
      throw e;
    });
    if (todosForam && App.ativa(token)) ($("#cortes-selecionar") || $("#conteudo")).focus({ preventScroll: true });
  } finally {
    atualizarEstado();
  }
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
  if (["revisao", "pronto", "erro", "descartado"].includes(s)) {
    // vídeo criado não é reeditado: quem gera outro é a pauta
    b.push(d.criado ? botaoAcao("recriar", "Criar de novo", "refazer") : botaoAcao("reeditar", "Editar de novo", "refazer"));
  }
  if (["descartado", "erro"].includes(s) && !d.criado) b.push(botaoAcao("restaurar", "Restaurar", "refazer"));
  if (["candidato", "revisao", "pronto", "erro"].includes(s)) b.push(botaoAcao("descartar", "Descartar", "lixo", "perigo"));
  return h`<div class="linha-botoes">${b}</div>`;
}

function cabecaDetalheHtml(d) {
  const parte = d.parte || d.textos.parte_prevista;
  const badges = [badge(ESTADO_CORTE[d.status] || [d.status, "neutro"])];
  if (d.prioridade > 0) badges.push(badge(["Com prioridade", "acento"]));
  if (d.criado) badges.push(badge(["Criado do zero", "info"]));
  const visual = d.video ? ` · visual: ${d.modelo || "anterior aos modelos"}` : "";
  const origem = d.criado
    ? h`Vídeo criado da pauta “${d.filme}” · ${fmtDuracao(d.duracao)}${d.detalhes && d.detalhes.clipes ? ` · ${d.detalhes.clipes} clipes de fundo` : ""}${d.detalhes && d.detalhes.voz ? ` · voz ${d.detalhes.voz}` : ""}${visual}`
    : h`Trecho de ${fmtTempo(d.inicio)} a ${fmtTempo(d.fim)} do filme · ${fmtDuracao(d.duracao)} · nota ${fmtNum(d.pontuacao, 2)}${visual}`;
  return h`<div class="detalhe-cabeca"><h3>${d.criado ? d.filme : h`${d.filme} · Parte ${parte}${d.parte ? "" : " (prevista)"}`}</h3>${badges}</div>
    <div class="detalhe-meta">${origem}</div>
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
    <p class="nota">Em branco, usa ${vazioUsa}. Aceita {filme}, {ano}, {ano_parenteses}, {parte}, {frase}, {hashtags}, {titulo_ia}, {descricao_ia}, {canal} e {link_original}. Ctrl+S salva.</p>
    <div class="linha-botoes"><button class="btn pequeno primario" data-acao="corte-textos">${ic("check")}Salvar os textos</button>${t.titulo_custom || t.descricao_custom ? h`<button class="btn pequeno fantasma" data-acao="corte-textos-limpar">Voltar ao automático</button>` : ""}</div>
    <details class="guia"><summary>${ic("olho")}Como o post vai sair</summary><div class="amostra-post"><b>${t.titulo}</b>${t.descricao}</div></details>`;
}

function botaoRedeHtml(d, rede, p) {
  if (p && p.estado === "aguardando") {
    return h`<button class="btn pequeno" data-acao="abrir-tarefa" data-tarefa="${p.id}">${ic("lista")}Ver a tarefa</button>`;
  }
  if (p && p.estado === "conferir") {
    // sem "Postar agora" aqui: o vídeo pode já ter saído, e postar de novo repetiria o post
    return h`<button class="btn pequeno" data-acao="corte-conferir" data-rede="${rede}">${ic("olho")}Conferir</button>`;
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
  if (!d.ativas.length) {
    // as redes ligadas podem estar só com imagens: elas aparecem em Imagens, logo abaixo
    return Object.keys(d.imagens_por_rede || {}).length
      ? h`<p class="nota">Nenhuma rede recebe o vídeo: as redes ligadas estão só com imagens.</p>`
      : h`<p class="nota">Nenhuma rede ativa. Ative em Redes sociais.</p>`;
  }
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
  return h`<details class="guia"><summary>${ic("lista")}Histórico (${d.historico.length})</summary><div class="historico">${d.historico.map((p) => h`<div>${logoRede(p.rede)}<span>${fmtQuando(p.quando)}</span>${p.midia === "imagem" ? marcaImagem(p) : ""}${badge(estadoPost(p.estado), true)}${p.manual ? h`<span class="texto-suave">fora da agenda</span>` : ""}${p.erro ? h`<span class="texto-suave" title="${p.erro}">${p.erro}</span>` : ""}</div>`)}</div></details>`;
}

function videoDetalheHtml(d) {
  if (d.video) {
    const poster = d.miniatura ? h`poster="${midia(d.miniatura)}"` : "";
    return h`<video src="${midia(d.video)}" ${poster} controls playsinline preload="metadata"></video>`;
  }
  if (d.status === "concluido") {
    // publicado e sem o arquivo: apagado para liberar espaço ("Apagar os vídeos" ou apagar_apos_postar)
    return h`<div class="capa-vazia">${ic("lixo")}<small>O vídeo deste corte foi apagado do disco. Os posts e o histórico continuam.</small></div>`;
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
      <div id="det-imagens">${corImgSecaoHtml(d)}</div>
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

/** O título e a descrição digitados e ainda não salvos (null quando não há nada pendente). */
function rascunhoTextos() {
  if (!textosSujos()) return null;
  return { titulo: $("#det-titulo").value, descricao: $("#det-descricao").value };
}

function devolverRascunho(rascunho) {
  if (!rascunho) return;
  const titulo = $("#det-titulo");
  const descricao = $("#det-descricao");
  if (titulo) titulo.value = rascunho.titulo;
  if (descricao) descricao.value = rascunho.descricao;
}

function desenharDetalhe(d, completo) {
  const corpo = corpoModal();
  if (!corpo || Detalhe.id !== d.id) return;
  const anterior = Detalhe.dados;
  // Aprovar, Postar antes, Editar agora e Gerar com IA redesenham a janela: o que você digitou
  // nos textos e ainda não salvou volta para os campos em vez de sumir
  const rascunho = rascunhoTextos();
  Detalhe.dados = d;
  if (completo || !$("#det-video")) {
    montar(corpo, detalheHtml(d));
    Detalhe.videoUrl = d.video;
    devolverRascunho(rascunho);
    return;
  }
  if (d.video !== Detalhe.videoUrl || !d.video) {
    Detalhe.videoUrl = d.video;
    montar($("#det-video"), videoDetalheHtml(d));
  }
  montarSeMudou($("#det-cabeca"), cabecaDetalheHtml(d));
  montarSeMudou($("#det-acoes"), acoesDetalheHtml(d));
  montarSeMudou($("#det-redes"), redesDetalheHtml(d));
  montarSeMudou($("#det-imagens"), corImgSecaoHtml(d));
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
      // "a conferir" e "ensaio" não são erro: pedem só que você olhe
      const tipoToast = ["conferir", "ensaio"].includes(p.estado) ? "info" : classe === "erro" || classe === "aviso" ? "erro" : "ok";
      toast(`${REDES[rede].rotulo}: ${texto}${p.erro ? ` (${p.erro})` : ""}`, tipoToast, 7000);
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

async function salvarTextosCorte(el = null) {
  const d = Detalhe.dados;
  if (!d || !$("#det-titulo")) return;
  const novo = await ocupado(el, () => post(`/cortes/${d.id}/textos`, { titulo: $("#det-titulo").value, descricao: $("#det-descricao").value }));
  toast("Textos salvos");
  Detalhe.dados = novo; // os campos já mostram o que foi salvo: o redesenho não acha rascunho
  desenharDetalhe(novo, false);
  montar($("#det-textos"), textosDetalheHtml(novo));
}

async function abrirCorte(id) {
  if (!id) return;
  abrirModal({
    titulo: "Detalhes do corte",
    largura: 1080,
    corpo: h`<div class="carregando-bloco"><span class="giro"></span></div>`,
    protegido: () => textosSujos(),
    aoSalvar: () => salvarTextosCorte(),
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
  if (tipo === "recriar") {
    const ok = await confirmar({
      titulo: "Criar este vídeo de novo?",
      texto: "O vídeo atual é apagado e a pauta volta para a fila da Criação: narração, imagens de fundo e montagem são feitas de novo, e o resultado pode sair diferente.",
      botao: "Apagar e criar de novo",
      perigo: true,
    });
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
  if (tipo === "recriar") toast("A pauta voltou para a fila da Criação", "info");
  desenharDetalhe(novo, true);
  clearTimeout(Detalhe.timer);
  Detalhe.timer = setTimeout(() => carregarDetalhe(false), 1500);
  atualizarEstado();
};

App.acoes["corte-textos"] = (el) => salvarTextosCorte(el);

App.acoes["corte-conferir"] = async (el) => {
  const d = Detalhe.dados;
  if (!d) return;
  const rede = el.dataset.rede;
  const p = d.postagens[rede];
  if (!p || p.estado !== "conferir") return;
  if (textosSujos()) {
    const ok = await confirmar({ titulo: "Sair sem salvar os textos?", texto: "O título e a descrição que você mudou ainda não foram salvos.", botao: "Sair sem salvar", perigo: true });
    if (!ok) return;
  }
  const corteId = d.id;
  abrirConferirPost({ id: p.id, rede, filme: d.filme, parte: d.parte, quando: p.quando, url: p.url, erro: p.erro },
    () => abrirCorte(corteId));
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

/* ------------------------------------------------------------ imagens do corte */

// a janela de uma imagem (ou do "Postar imagem agora") troca a do corte, que volta quando ela fecha
const CorImg = { dados: null, img: null, n: 0, vendo: false };

function corImgEstadoHtml(d) {
  const redes = Object.keys(d.imagens_por_rede || {});
  if (!redes.length) return "";
  return h`<div class="redes-corte">${redes.map((rede) => {
    const posts = d.imagens_por_rede[rede] || {};
    const tipos = Object.keys(TIPOS_IMAGEM).filter((t) => posts[t] || (d.pode_imagem.tipos || []).includes(t));
    if (posts.gravacao) tipos.push("gravacao");
    return h`<div class="rede-linha">${logoRede(rede)}<span class="rede-nome"><b>${REDES[rede].rotulo}</b><small>${tipos.length ? "" : "nenhum tipo serve para este trecho"}</small></span><span class="cor-img-tipos">${tipos.map((t) => {
      const p = posts[t];
      const estado = p ? estadoPost(p.estado) : ["Ainda não saiu", "neutro"];
      const url = p && urlSegura(p.url);
      const nome = t === "gravacao" ? "Da gravação" : TIPOS_IMAGEM[t];
      return h`<span class="cor-img-tipo" title="${p && p.erro ? p.erro : ""}">${nome} ${badge(estado, true)}${url ? h`<a class="link" href="${url}" target="_blank" rel="noopener noreferrer" aria-label="Abrir o post de ${nome} no ${REDES[rede].rotulo}">${ic("externo")}</a>` : ""}</span>`;
    })}</span></div>`;
  })}</div>`;
}

function corImgSecaoHtml(d) {
  if (d.criado) return ""; // vídeo criado de uma pauta não tem imagem
  const pode = d.pode_imagem || { redes: [], tipos: [], por_rede: {} };
  const miniaturas = (d.imagens || []).flatMap((g) => g.urls.map((url, n) => h`<button type="button" class="cor-img-mini" data-acao="corte-img-ver" data-img="${g.id}" data-n="${n}" aria-label="Ver a imagem ${TIPOS_IMAGEM[g.tipo] || g.tipo} ${g.formato}, ${n + 1} de ${g.urls.length}"><img src="${midia(url)}" alt="" loading="lazy"><small>${TIPOS_IMAGEM[g.tipo] || g.tipo}${g.urls.length > 1 ? ` ${n + 1}` : ""}</small></button>`));
  const temRedes = Object.keys(d.imagens_por_rede || {}).length > 0;
  if (!miniaturas.length && !temRedes) return "";
  const botao = pode.redes.length
    ? h`<button class="btn pequeno" data-acao="corte-img-postar-abrir">${ic("imagem")}Postar imagem agora</button>`
    : pode.motivo && temRedes ? h`<small class="texto-suave">${pode.motivo}</small>` : "";
  return h`<div class="rotulo-campo">Imagens</div>
    ${miniaturas.length ? h`<div class="cor-img-galeria">${miniaturas}</div>` : h`<p class="nota">Nenhuma imagem gerada deste corte ainda: elas saem na hora do post.</p>`}
    ${corImgEstadoHtml(d)}
    ${botao ? h`<div class="linha-botoes">${botao}</div>` : ""}`;
}

/** Fecha a janela do corte para abrir outra (a da imagem); avisa antes se há texto por salvar. */
async function corImgTrocarJanela() {
  if (textosSujos()) {
    const ok = await confirmar({ titulo: "Sair sem salvar os textos?", texto: "O título e a descrição que você mudou ainda não foram salvos.", botao: "Sair sem salvar", perigo: true });
    if (!ok) return null;
  }
  return Detalhe.dados;
}

function corImgVerHtml() {
  const d = CorImg.dados;
  const g = (d.imagens || []).find((x) => x.id === CorImg.img);
  if (!g || !g.urls.length) return vazioBloco("imagem", "Esta imagem não está mais no disco");
  const n = Math.max(0, Math.min(CorImg.n, g.urls.length - 1));
  const url = g.urls[n];
  const nome = `${TIPOS_IMAGEM[g.tipo] || g.tipo}, ${g.formato.replace("x", ":")}${g.urls.length > 1 ? `, ${n + 1} de ${g.urls.length}` : ""}`;
  const arquivo = `${corImgSlug(d.filme)}-${g.tipo}-${n + 1}.jpg`;
  // o download é um link comum, sem data-acao (o clique global faria preventDefault)
  return h`<div class="cor-img-ver"><img src="${midia(url)}" alt="${nome}">
    <div class="linha-botoes">
      ${g.urls.length > 1 ? h`<button type="button" class="btn pequeno" data-acao="corte-img-ver" data-img="${g.id}" data-n="${(n - 1 + g.urls.length) % g.urls.length}">Anterior</button><button type="button" class="btn pequeno" data-acao="corte-img-ver" data-img="${g.id}" data-n="${(n + 1) % g.urls.length}">Próxima</button>` : ""}
      <a class="btn pequeno" href="${midia(url)}" download="${arquivo}">${ic("baixar")}Baixar</a>
      <button type="button" class="btn pequeno fantasma" data-acao="fechar-modal">Voltar ao corte</button>
    </div>
    <p class="nota">${nome}${g.texto ? ` · “${g.texto}”` : ""}${g.com_chamada ? " · com a chamada para o vídeo" : ""}</p></div>`;
}

const corImgSlug = (texto) => String(texto || "imagem").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
  .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "imagem";

App.acoes["corte-img-ver"] = async (el) => {
  const img = Number(el.dataset.img);
  const n = Number(el.dataset.n) || 0;
  if (CorImg.vendo && modal.aberto) { // anterior/próxima: a mesma janela
    CorImg.img = img;
    CorImg.n = n;
    montar(corpoModal(), corImgVerHtml());
    return;
  }
  const d = await corImgTrocarJanela();
  if (!d) return;
  CorImg.dados = d;
  CorImg.img = img;
  CorImg.n = n;
  CorImg.vendo = true;
  const corteId = d.id;
  abrirModal({
    titulo: `${d.filme}${d.parte ? ` · Parte ${d.parte}` : ""} · imagem`,
    largura: 760,
    corpo: corImgVerHtml(),
    aoFechar: () => { CorImg.vendo = false; CorImg.dados = null; abrirCorte(corteId); },
  });
};

function corImgNotaHtml() {
  const d = CorImg.dados;
  const rede = ($("#modal-raiz input[name=cor-img-rede]:checked") || {}).value;
  const tipo = ($("#modal-raiz input[name=cor-img-tipo]:checked") || {}).value;
  const info = (d && d.pode_imagem.por_rede[rede]) || {};
  const notas = [];
  if (tipo && (info.ja_saiu || []).includes(tipo)) notas.push("Este tipo já saiu nesta rede: vai sair de novo.");
  if (rede && !info.com_chamada) notas.push("O vídeo desta parte ainda não saiu aqui: a imagem vai sem a chamada para o vídeo.");
  return notas.length ? h`<p class="nota">${notas.map((x) => h`${x}<br>`)}</p>` : "";
}

/** Rede ou tipo trocados na janela do "Postar imagem agora": a nota só lê o que veio do servidor. */
function corImgAoMudar(ev) {
  const alvo = ev.target;
  if (!alvo || !["cor-img-rede", "cor-img-tipo"].includes(alvo.name)) return;
  montarSeMudou($("#cor-img-nota"), corImgNotaHtml());
}
$("#modal-raiz").addEventListener("change", corImgAoMudar);

App.acoes["corte-img-postar-abrir"] = async () => {
  const d = await corImgTrocarJanela();
  if (!d) return;
  const pode = d.pode_imagem;
  CorImg.dados = d;
  const corteId = d.id;
  const simulacao = App.estado && App.estado.simulacao;
  // os rádios não têm data-acao: o change delegado no #modal-raiz troca a nota
  abrirModal({
    titulo: "Postar imagem agora",
    largura: 560,
    corpo: h`<p class="texto-modal">${d.filme}${d.parte ? ` · Parte ${d.parte}` : ""}: a imagem é gerada e ${simulacao ? "o post é só registrado (modo simulação)" : "sai agora, fora da agenda, e conta no limite do dia da rede"}.</p>
      <div class="rotulo-campo">Rede</div>
      <div class="contas-ig">${pode.redes.map((rede, i) => h`<label><input type="radio" name="cor-img-rede" value="${rede}" ${i === 0 ? raw("checked") : ""}><span>${logoRede(rede)} ${REDES[rede].rotulo}</span></label>`)}</div>
      <div class="rotulo-campo">Tipo</div>
      <div class="contas-ig">${pode.tipos.map((tipo, i) => h`<label><input type="radio" name="cor-img-tipo" value="${tipo}" ${i === 0 ? raw("checked") : ""}><span>${TIPOS_IMAGEM[tipo]}</span></label>`)}</div>
      <div id="cor-img-nota"></div>
      <p class="nota">O quadro do filme tem os mesmos direitos do vídeo: poste só o que você tem direito de usar.</p>
      <div class="acoes-modal"><button type="button" class="btn fantasma" data-acao="fechar-modal">Cancelar</button><button type="button" class="btn primario" data-acao="corte-img-postar">${ic("aviao")}${simulacao ? "Simular agora" : "Postar agora"}</button></div>`,
    aoFechar: () => { CorImg.dados = null; abrirCorte(corteId); },
  });
  montarSeMudou($("#cor-img-nota"), corImgNotaHtml());
};

App.acoes["corte-img-postar"] = async (el) => {
  const d = CorImg.dados;
  if (!d) return;
  const rede = ($("#modal-raiz input[name=cor-img-rede]:checked") || {}).value;
  const tipo = ($("#modal-raiz input[name=cor-img-tipo]:checked") || {}).value;
  if (!rede || !tipo) { toast("Escolha a rede e o tipo", "erro"); return; }
  const r = await ocupado(el, () => post(`/cortes/${d.id}/postar-imagem`, { rede, tipo }));
  toast(`${r.simulacao ? "Simulando" : "Publicando"} a imagem (${TIPOS_IMAGEM[tipo].toLowerCase()}) no ${REDES[rede].rotulo}...`, "info", 6000);
  fecharModal(); // a janela do corte volta e mostra o andamento
  atualizarEstado();
};
