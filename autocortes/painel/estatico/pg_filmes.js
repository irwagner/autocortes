/* AutoCortes - painel: Filmes (envio por arrastar e soltar, análise e dados de cada filme) */
"use strict";

// aba: "pasta" (os filmes) ou "canais" (os canais do YouTube que baixam sozinhos)
const PgFilmes = { dados: null, ultimaAssinatura: "", enviando: 0, aba: "pasta", canais: null, assinaturaCanais: "" };

function descricaoLegenda(f) {
  const fonte = String(f.legenda || "");
  if (fonte.startsWith("arquivo")) return "legenda do arquivo .srt";
  if (fonte.startsWith("embutida")) return "legenda embutida no vídeo";
  if (fonte === "whisper") return `transcrição automática${f.idioma ? ` (${f.idioma})` : ""}`;
  if (fonte) return "sem legenda";
  return "";
}

function atividadeDoFilme(f) {
  const atividades = (App.estado && App.estado.atividades) || [];
  return atividades.find((a) => a.texto.includes(`'${f.titulo}'`)) || null;
}

function filmeHtml(f) {
  const estado = ESTADO_FILME[f.status] || [f.status, "neutro"];
  const c = f.cortes;
  const numeros = [];
  if (f.duracao) numeros.push(h`<span>Duração <b>${fmtTempo(f.duracao)}</b></span>`);
  if (f.tamanho) numeros.push(h`<span><b>${fmtBytes(f.tamanho)}</b></span>`);
  if (f.status === "analisado" || c.candidatos || c.prontos || c.publicados) {
    numeros.push(h`<span>Trechos <b>${c.candidatos}</b></span>`);
    if (c.revisao) numeros.push(h`<span>Aguardando aprovação <b>${c.revisao}</b></span>`);
    numeros.push(h`<span>Na fila <b>${c.prontos}</b></span>`);
    numeros.push(h`<span>Publicados <b>${c.publicados}</b></span>`);
  }
  const legenda = descricaoLegenda(f);
  if (legenda) numeros.push(h`<span>${legenda}</span>`);
  const atividade = f.status === "analisando" ? atividadeDoFilme(f) : null;
  let andamento = "";
  if (f.status === "analisando") {
    const pct = atividade && atividade.pct !== null ? atividade.pct : null;
    andamento = h`<div class="filme-progresso"><span class="giro"></span><span>${atividade ? atividade.texto : "Analisando..."}</span>${pct !== null ? h`<div class="barra"><i style="width:${pct}%"></i></div><b>${Math.round(pct)}%</b>` : ""}</div>`;
  } else if (f.status === "novo") {
    andamento = h`<div class="filme-progresso texto-suave">${ic("relogio")}<span>Esperando a análise${App.estado && !App.estado.motor.rodando ? " (o motor está desligado)" : ""}</span></div>`;
  }
  const esgotado = f.esgotado && f.status === "analisado" ? badge(["Todos os trechos aproveitados", "neutro"], true) : "";
  const botoes = [];
  if (c.candidatos || c.prontos || c.publicados || c.revisao) botoes.push(h`<a class="btn pequeno" href="#/cortes?filme=${f.id}">${ic("tesoura")}Ver cortes</a>`);
  botoes.push(h`<button class="btn pequeno" data-acao="filme-dados" data-filme="${f.id}">${ic("lapis")}Dados</button>`);
  if (f.status !== "analisando" && f.status !== "ausente") botoes.push(h`<button class="btn pequeno" data-acao="filme-reanalisar" data-filme="${f.id}">${ic("refazer")}Reanalisar</button>`);
  if (f.status === "ignorado") botoes.push(h`<button class="btn pequeno" data-acao="filme-acao" data-tipo="reativar" data-filme="${f.id}">${ic("play")}Reativar</button>`);
  else if (f.status !== "ausente") botoes.push(h`<button class="btn pequeno fantasma" data-acao="filme-acao" data-tipo="ignorar" data-filme="${f.id}" title="Parar de usar este filme">${ic("pausa")}Ignorar</button>`);
  botoes.push(h`<button class="btn pequeno icone fantasma" data-acao="filme-acao" data-tipo="mostrar" data-filme="${f.id}" title="Mostrar no Explorer" aria-label="Mostrar no Explorer">${ic("pasta")}</button>`);
  return h`<div class="card filme">
    <div class="filme-capa">${f.miniatura ? h`<img src="${midia(f.miniatura)}" alt="" loading="lazy">` : ic("filme")}</div>
    <div class="filme-info">
      <div class="filme-titulo"><b>${f.titulo}</b>${f.ano ? h`<span class="ano">${f.ano}</span>` : ""}${badge(estado, true)}${esgotado}</div>
      <div class="arquivo" title="${f.arquivo}">${f.arquivo}</div>
      ${numeros.length ? h`<div class="filme-numeros">${numeros}</div>` : ""}
      ${andamento}
      ${f.status === "erro" && f.erro ? h`<p class="erro-texto">${f.erro}</p>` : ""}
    </div>
    <div class="filme-acoes">${botoes}</div>
  </div>`;
}

function listaFilmesHtml(r) {
  if (!r.filmes.length) {
    return vazioBloco("filme", "Nenhum filme ainda", "Arraste um filme para a área acima ou coloque os arquivos na pasta de filmes. O AutoCortes procura arquivos novos a cada poucos minutos.", true);
  }
  return h`<div class="lista-filmes">${r.filmes.map(filmeHtml)}</div>`;
}

function zonaEnvioHtml(r) {
  return h`<div class="card zona-envio" id="zona-envio" data-acao="escolher-arquivos" role="button" tabindex="0" aria-label="Enviar filmes ou legendas">
      <div class="zona-conteudo">${ic("enviar", "grande")}<div><b>Arraste filmes ou legendas (.srt) para cá</b><small>ou clique para escolher. Os arquivos são copiados para ${r.pasta}</small></div></div>
    </div>
    <input type="file" id="arquivo-filmes" multiple hidden accept="${[...r.extensoes, ".srt"].join(",")}">
    <div class="envios" id="envios"></div>`;
}

async function carregarFilmes(token = App.navegacao) {
  const r = await api("/filmes");
  if (!App.ativa(token) || App.nomePagina !== "filmes") return;
  PgFilmes.dados = r;
  montarSeMudou($("#lista-filmes"), listaFilmesHtml(r));
}

/* ------------------------------------------------------------ envio de arquivos */

function linhaEnvio(nome) {
  const linha = elementoDe(h`<div class="envio"><span title="${nome}">${nome}</span><div class="barra"><i style="width:0%"></i></div><small>0%</small></div>`);
  $("#envios").appendChild(linha);
  return linha;
}

function marcarEnvio(linha, classe, texto, pct = null) {
  if (classe) linha.classList.add(classe);
  if (pct !== null) linha.querySelector("i").style.width = `${pct}%`;
  linha.querySelector("small").textContent = texto;
}

function enviarArquivo(arquivo, linha) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/filmes/enviar");
    xhr.setRequestHeader("X-AutoCortes", App.token);
    xhr.setRequestHeader("X-Nome-Arquivo", encodeURIComponent(arquivo.name));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) {
        const pct = Math.floor((100 * ev.loaded) / ev.total);
        marcarEnvio(linha, null, `${pct}%`, pct);
      }
    };
    xhr.onload = () => {
      let dados = {};
      try { dados = JSON.parse(xhr.responseText || "{}"); } catch (e) { dados = {}; }
      if (xhr.status === 200) {
        marcarEnvio(linha, "feito", "Enviado", 100);
        resolve(dados);
      } else {
        marcarEnvio(linha, "falhou", dados.erro || `Erro ${xhr.status}`);
        resolve(null);
      }
    };
    xhr.onerror = () => { marcarEnvio(linha, "falhou", "Falha de conexão"); resolve(null); };
    xhr.send(arquivo);
  });
}

async function enviarArquivos(lista) {
  const r = PgFilmes.dados;
  const aceitas = new Set([...(r ? r.extensoes : []), ".srt"]);
  const arquivos = [...lista];
  if (!arquivos.length) return;
  PgFilmes.enviando++;
  App.enviosAtivos = true;
  let ok = 0;
  try {
    for (const arquivo of arquivos) {
      const ext = (arquivo.name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
      const linha = linhaEnvio(arquivo.name);
      // recusado aqui, antes de subir: o servidor teria de ler o arquivo inteiro só para dizer não
      if (!aceitas.has(ext)) { marcarEnvio(linha, "falhou", "Formato não aceito"); continue; }
      if (!arquivo.size) { marcarEnvio(linha, "falhou", "Arquivo vazio"); continue; }
      if (arquivo.size > 60 * 1024 ** 3) { marcarEnvio(linha, "falhou", "Grande demais (máximo 60 GB)"); continue; }
      if (await enviarArquivo(arquivo, linha)) ok++;
    }
  } finally {
    PgFilmes.enviando--;
    App.enviosAtivos = PgFilmes.enviando > 0;
  }
  if (ok) {
    toast(`${plural(ok, "arquivo enviado", "arquivos enviados")}. A análise começa em instantes.`);
    await carregarFilmes().catch(() => {});
    atualizarEstado();
  }
}

function prepararZonaEnvio() {
  const zona = $("#zona-envio");
  const seletor = $("#arquivo-filmes");
  if (!zona || !seletor) return;
  seletor.addEventListener("change", () => {
    const arquivos = [...seletor.files];
    seletor.value = "";
    enviarArquivos(arquivos);
  });
  let profundidade = 0;
  zona.addEventListener("dragenter", (ev) => { ev.preventDefault(); profundidade++; zona.classList.add("sobre"); });
  zona.addEventListener("dragover", (ev) => { ev.preventDefault(); ev.dataTransfer.dropEffect = "copy"; });
  zona.addEventListener("dragleave", () => { profundidade = Math.max(0, profundidade - 1); if (!profundidade) zona.classList.remove("sobre"); });
  zona.addEventListener("drop", (ev) => {
    ev.preventDefault();
    profundidade = 0;
    zona.classList.remove("sobre");
    enviarArquivos(ev.dataTransfer.files);
  });
}

App.acoes["escolher-arquivos"] = () => {
  const seletor = $("#arquivo-filmes");
  if (seletor) seletor.click();
};

App.acoes["varrer-filmes"] = async (el) => {
  const r = await ocupado(el, () => post("/filmes/varrer"));
  toast(r.novos ? `${plural(r.novos, "filme novo encontrado", "filmes novos encontrados")}` : "Nenhum filme novo na pasta. Arquivos copiados há menos de 1 minuto esperam um pouco.", r.novos ? "ok" : "info", 6000);
  await carregarFilmes();
};

/* ------------------------------------------------------------ ações de cada filme */

function filmePorId(id) {
  return PgFilmes.dados && PgFilmes.dados.filmes.find((f) => String(f.id) === String(id));
}

App.acoes["filme-acao"] = async (el) => {
  const f = filmePorId(el.dataset.filme);
  const tipo = el.dataset.tipo;
  if (tipo === "ignorar") {
    const ok = await confirmar({ titulo: `Ignorar "${f ? f.titulo : "o filme"}"?`, texto: "O filme deixa de ser analisado e os cortes dele não são mais postados. Dá para reativar depois.", botao: "Ignorar" });
    if (!ok) return;
  }
  await ocupado(el, () => post(`/filmes/${el.dataset.filme}/acao`, { acao: tipo }));
  if (tipo !== "mostrar") {
    toast(tipo === "ignorar" ? "Filme ignorado" : "Filme reativado");
    await carregarFilmes();
    atualizarEstado();
  }
};

App.acoes["filme-reanalisar"] = (el) => {
  const f = filmePorId(el.dataset.filme);
  if (!f) return;
  abrirModal({
    titulo: `Reanalisar "${f.titulo}"`,
    largura: 560,
    corpo: h`<p class="texto-modal">Os trechos encontrados que ainda não foram editados são refeitos. Cortes já editados, publicados ou descartados continuam como estão.</p>
      <div class="opcoes-rede">
        <button class="btn" data-acao="filme-reanalisar-ok" data-filme="${f.id}" data-tipo="reanalisar">${ic("texto")}Refazer legendas e trechos<span class="texto-suave">rápido, reaproveita cenas e volume</span></button>
        <button class="btn" data-acao="filme-reanalisar-ok" data-filme="${f.id}" data-tipo="reanalisar_tudo">${ic("refazer")}Analisar tudo do zero<span class="texto-suave">use se o arquivo mudou</span></button>
      </div>
      <div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button></div>`,
  });
};

App.acoes["filme-reanalisar-ok"] = async (el) => {
  await ocupado(el, () => post(`/filmes/${el.dataset.filme}/acao`, { acao: el.dataset.tipo }));
  fecharModal();
  toast("O filme voltou para a fila de análise");
  await carregarFilmes();
  atualizarEstado();
};

App.acoes["filme-dados"] = (el) => {
  const f = filmePorId(el.dataset.filme);
  if (!f) return;
  const meta = f.meta || {};
  const idioma = meta.idioma || "";
  abrirModal({
    titulo: "Dados do filme",
    largura: 600,
    corpo: h`<p class="nota">Arquivo: ${f.arquivo}. Os dados ficam num arquivo .json ao lado do filme.</p>
      <label class="campo"><span>Título</span><input type="text" id="fd-titulo" maxlength="150" value="${meta.titulo || ""}" placeholder="${f.titulo}"></label>
      <label class="campo"><span>Ano</span><input type="number" id="fd-ano" min="1880" max="2100" class="curto" value="${meta.ano || ""}" placeholder="${f.ano || ""}"></label>
      <label class="campo"><span>Hashtags deste filme</span><input type="text" id="fd-hashtags" value="${(meta.hashtags || []).join(" ")}" placeholder="#terror #classico"></label>
      <label class="campo"><span>Idioma falado</span><select id="fd-idioma">${IDIOMAS.map(([v, t]) => h`<option value="${v}" ${v === idioma ? raw("selected") : ""}>${t}</option>`)}</select></label>
      <p class="nota">O idioma ajuda a transcrição automática. Para refazer as legendas com o novo idioma, use Reanalisar.</p>
      <div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button><button class="btn primario" data-acao="filme-dados-salvar" data-filme="${f.id}">${ic("check")}Salvar</button></div>`,
    // o que estava nos campos ao abrir: fechar sem querer com mudança pergunta antes
    protegido: () => filmeDadosAtuais() !== FilmeDados.base,
    aoSalvar: () => salvarDadosFilme(f.id),
  });
  FilmeDados.base = filmeDadosAtuais();
};

const FilmeDados = { base: null };
function filmeDadosAtuais() {
  const campos = ["#fd-titulo", "#fd-ano", "#fd-hashtags", "#fd-idioma"].map((s) => $(s));
  return campos.every(Boolean) ? JSON.stringify(campos.map((c) => c.value)) : FilmeDados.base;
}

async function salvarDadosFilme(id, el = null) {
  const hashtags = $("#fd-hashtags").value.split(/[\s,;]+/).map((t) => t.trim()).filter(Boolean)
    .map((t) => (t.startsWith("#") ? t : `#${t}`));
  await ocupado(el, () => post(`/filmes/${id}/meta`, {
    titulo: $("#fd-titulo").value,
    ano: $("#fd-ano").value,
    hashtags,
    idioma: $("#fd-idioma").value,
  }));
  fecharModal();
  toast("Dados do filme salvos");
  await carregarFilmes();
}

App.acoes["filme-dados-salvar"] = (el) => salvarDadosFilme(el.dataset.filme, el);

/* ------------------------------------------------------------ página */

/* ------------------------------------------------------------ canais do YouTube */

const CAN_ESTADO = {
  baixando: ["Baixando", "acento"], baixado: ["Baixado", "ok"], erro: ["Vai tentar de novo", "aviso"],
  pulado: ["Pulado", "neutro"],
};
const canMinutos = (m) => (m >= 60 && m % 60 === 0 ? `${m / 60} h` : `${m} min`);

function canRegras(c) {
  const g = c.regras;
  const partes = [g.max_idade_dias ? `dos últimos ${plural(g.max_idade_dias, "dia", "dias")}` : "de qualquer data"];
  partes.push(g.minutos_max ? `de ${canMinutos(g.minutos_min)} a ${canMinutos(g.minutos_max)}` : `com ${canMinutos(g.minutos_min)} ou mais`);
  partes.push(`até ${g.resolucao}p`);
  partes.push(`${plural(g.max_por_canal, "vídeo", "vídeos")} de cada canal a cada ${plural(g.verificar_a_cada_horas, "hora", "horas")}`);
  if (g.incluir_ao_vivo) partes.push("com as lives que já terminaram");
  return `Vídeos ${partes.join(", ")}.`;
}

/** O download em andamento (do motor ou do "Olhar agora"), para a barra de progresso. */
function canAtividade() {
  const atividades = (App.estado && App.estado.atividades) || [];
  return atividades.find((a) => (a.chave === "canais" || a.chave === "canais-agora") && !a.ocioso) || null;
}

function canSituacaoHtml(c) {
  const linhas = [];
  const atividade = canAtividade();
  if (c.rodando || atividade) {
    const pct = atividade && atividade.pct !== null ? atividade.pct : null;
    linhas.push(h`<div class="filme-progresso"><span class="giro"></span><span>${atividade ? atividade.texto : "Olhando os canais..."}</span>${pct !== null ? h`<div class="barra"><i style="width:${pct}%"></i></div><b>${Math.round(pct)}%</b>` : ""}</div>`);
  }
  const itens = [];
  if (!c.ativo) itens.push("Desligado: nada é baixado sozinho, mas o Olhar agora funciona");
  else if (!c.motor) itens.push("O motor está desligado: os canais só são olhados sozinhos com ele ligado");
  else if (c.proxima) itens.push(c.proxima * 1000 <= Date.now() ? "Próxima olhada em instantes" : `Próxima olhada ${fmtRelativo(c.proxima)}`);
  if (c.verificado_em) itens.push(`última ${fmtRelativo(c.verificado_em)}`);
  itens.push(`${plural(c.na_fila, "vídeo baixado esperando", "vídeos baixados esperando")} a análise (no máximo ${c.regras.max_na_fila})`);
  linhas.push(h`<p class="nota">${itens.join(" · ")}</p>`);
  if (c.pausa) {
    const p = c.pausa;
    linhas.push(h`<p class="aviso-inline">${ic("alerta")}Pausado até ${fmtQuando(p.ate)}: ${p.motivo}${p.vezes > 1 ? ` (${p.vezes}º aviso seguido)` : ""}</p>`);
    if (p.detalhe) linhas.push(h`<p class="detalhe-ytdlp">Mensagem do yt-dlp: ${p.detalhe}</p>`);
  }
  if (c.so_conta) {
    // o aviso foi sem a conta e a conta passou: até o fim, nada para
    const j = c.so_conta;
    linhas.push(h`<p class="aviso-inline">${ic("info")}O YouTube pediu verificação sem a conta às ${canHorario(j.desde)}. Até ${canHorario(j.ate)} eu baixo direto com a conta separada.</p>`);
    if (j.detalhe) linhas.push(h`<p class="detalhe-ytdlp">Mensagem do yt-dlp: ${j.detalhe}</p>`);
  }
  const f = c.ferramenta || {};
  if (!f.ytdlp) {
    linhas.push(h`<p class="nota">Na primeira olhada eu baixo o yt-dlp (18 MB, conferido pelo SHA-256)${f.js ? "" : " e o Deno (43 MB), que ele usa para falar com o YouTube"}.</p>`);
  }
  return linhas;
}

function canCanalHtml(k) {
  const meta = [plural(k.baixados, "vídeo baixado", "vídeos baixados")];
  if (k.erros) meta.push(plural(k.erros, "com erro", "com erro"));
  meta.push(k.verificado_em ? `olhado ${fmtRelativo(k.verificado_em)}` : "ainda não olhado");
  return h`<div class="canal${k.ativo ? "" : " pausado"}">
    <div class="canal-info">
      <div class="filme-titulo"><b>${k.nome}</b>${k.ativo ? "" : badge(["Pausado", "neutro"], true)}</div>
      <a class="arquivo" href="${k.url}" target="_blank" rel="noopener noreferrer">${k.url}</a>
      <small class="texto-suave">${meta.join(" · ")}</small>
      ${k.erro ? h`<p class="erro-texto">${k.erro}</p>` : ""}
    </div>
    <div class="grupo-botoes">
      <button class="btn pequeno" data-acao="can-verificar" data-canal="${k.id}" ${k.ativo ? "" : raw("disabled")}>${ic("busca")}Olhar agora</button>
      <button class="btn pequeno" data-acao="can-canal" data-tipo="${k.ativo ? "pausar" : "ativar"}" data-canal="${k.id}">${ic(k.ativo ? "pausa" : "play")}${k.ativo ? "Pausar" : "Ativar"}</button>
      <button class="btn pequeno fantasma" data-acao="can-canal" data-tipo="remover" data-canal="${k.id}">${ic("lixo")}Remover</button>
    </div>
  </div>`;
}

function canDownloadHtml(d) {
  // baixado: o que importa daí em diante é a situação do filme (esperando análise, analisado...)
  const estado = d.status === "baixado" && d.filme_status ? ESTADO_FILME[d.filme_status] || CAN_ESTADO.baixado : CAN_ESTADO[d.status] || [d.status, "neutro"];
  const meta = [d.canal, d.duracao ? fmtTempo(d.duracao) : "", d.tamanho ? fmtBytes(d.tamanho) : "", d.quando ? fmtRelativo(d.quando) : ""].filter(Boolean);
  const botoes = [];
  if (d.status === "erro" || d.status === "pulado") botoes.push(h`<button class="btn pequeno" data-acao="can-download" data-tipo="tentar" data-download="${d.id}">${ic("refazer")}Tentar de novo</button>`);
  if (d.filme_id) botoes.push(h`<a class="btn pequeno" href="#/cortes?filme=${d.filme_id}">${ic("tesoura")}Ver cortes</a>`);
  botoes.push(h`<a class="btn pequeno icone fantasma" href="${d.url}" target="_blank" rel="noopener noreferrer" title="Abrir no YouTube" aria-label="Abrir no YouTube">${ic("externo")}</a>`);
  return h`<div class="canal">
    <div class="canal-info">
      <div class="filme-titulo"><b title="${d.titulo || ""}">${d.titulo || d.video_id}</b>${badge(estado, true)}</div>
      <small class="texto-suave">${meta.join(" · ")}</small>
      ${d.erro ? h`<p class="${d.status === "erro" ? "erro-texto" : "nota"}">${d.erro}</p>` : ""}
    </div>
    <div class="grupo-botoes">${botoes}</div>
  </div>`;
}

const canBotaoTodos = (c) => h`<button class="btn" data-acao="can-verificar" ${c.canais.some((k) => k.ativo) && !c.rodando ? "" : raw("disabled")}>${ic("busca")}Olhar todos agora</button>`;
const canListaHtml = (c) => (c.canais.length ? c.canais.map(canCanalHtml)
  : vazioBloco("video", "Nenhum canal ainda", "Cole o endereço de um canal do YouTube acima, como https://www.youtube.com/@canal."));
const canDownloadsHtml = (c) => (c.downloads.length ? c.downloads.map(canDownloadHtml)
  : vazioBloco("baixar", "Nada baixado ainda", "Os vídeos aparecem aqui quando os canais são olhados."));

function canCorpoHtml(c) {
  if (!c) return vazioBloco("baixar", "Carregando os canais...");
  return h`<div class="card">
      <div class="card-topo"><h3>${ic("baixar")}Baixar sozinho os vídeos novos</h3>${CONTROLES.switch({ chave: "canais.ativo", rotulo: "Baixar sozinho os vídeos novos dos canais" })}</div>
      <p class="nota" id="can-regras">${canRegras(c)}</p>
      <div id="can-situacao">${canSituacaoHtml(c)}</div>
      <div class="linha-botoes">
        <span id="can-todos">${canBotaoTodos(c)}</span>
        <a class="btn" href="#/config?aba=canais">${ic("ajustes")}Regras do download</a>
        <button class="btn fantasma" data-acao="abrir-pasta" data-alvo="canais">${ic("pasta")}Abrir a pasta</button>
      </div>
    </div>
    <div class="aviso-rede">${ic("alerta")}<span>Baixe só de canais <b>seus ou que autorizam cortes</b>, e dê o crédito nos posts ({canal} e {link_original} nos textos). Baixar vídeo de terceiros sem permissão viola os termos do YouTube e os direitos de quem fez, e os cortes podem levar reivindicação ou derrubar a conta.</span></div>
    <div class="card">
      <div class="card-topo"><h3>${ic("video")}Canais</h3></div>
      <p class="nota">Cada canal ganha uma pasta, e os vídeos entram na análise como qualquer filme. Ficam em: <code class="caminho">${c.pasta}</code></p>
      <div class="linha-adicionar"><input type="url" id="can-url" placeholder="https://www.youtube.com/@canal" aria-label="Endereço do canal no YouTube" spellcheck="false" autocomplete="off"><button class="btn primario" data-acao="can-adicionar">${ic("mais")}Adicionar</button></div>
      <div class="lista-canais" id="can-lista">${canListaHtml(c)}</div>
    </div>
    <div id="can-conta">${canContaHtml(c)}</div>
    <div class="card">
      <div class="card-topo"><h3>${ic("baixar")}Downloads</h3></div>
      <p class="nota">Os mais recentes primeiro. Um vídeo nunca é baixado duas vezes.</p>
      <div class="lista-canais" id="can-downloads">${canDownloadsHtml(c)}</div>
    </div>`;
}

/** Busca e atualiza a aba por partes: o endereço que você está digitando no campo fica. */
async function canCarregar(desenhar = true) {
  const c = await api("/canais");
  PgFilmes.canais = c;
  if (!desenhar || App.nomePagina !== "filmes" || PgFilmes.aba !== "canais") return c;
  if (!$("#can-situacao")) {
    filmesDesenharAba();
    return c;
  }
  montarSeMudou($("#filmes-abas"), filmesAbasHtml());
  montarSeMudou($("#can-regras"), canRegras(c));
  montarSeMudou($("#can-situacao"), canSituacaoHtml(c));
  montarSeMudou($("#can-todos"), canBotaoTodos(c));
  montarSeMudou($("#can-lista"), canListaHtml(c));
  montarSeMudou($("#can-conta"), canContaHtml(c));
  montarSeMudou($("#can-downloads"), canDownloadsHtml(c));
  return c;
}

function canPrepararCampo() {
  const campo = $("#can-url");
  if (!campo || campo.dataset.pronto) return;
  campo.dataset.pronto = "1";
  campo.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      const botao = $("[data-acao=can-adicionar]");
      if (botao) botao.click();
    }
  });
}

App.acoes["can-adicionar"] = async (el) => {
  const campo = $("#can-url");
  const url = campo ? campo.value.trim() : "";
  if (!url) { toast("Cole o endereço do canal no YouTube", "info"); if (campo) campo.focus(); return; }
  const ok = await confirmar({
    titulo: "Este canal é seu ou autoriza cortes?",
    texto: "O AutoCortes vai baixar os vídeos novos dele e postar cortes nas suas redes. Sem a permissão de quem fez, isso viola os termos do YouTube e os direitos autorais, e a sua conta pode cair.",
    botao: "É meu ou tenho permissão",
  });
  if (!ok) return;
  PgFilmes.canais = await ocupado(el, () => post("/canais/novo", { url }));
  if (campo) campo.value = "";
  await canCarregar();
  toast(valor("canais.ativo") ? "Canal adicionado. Ele é olhado na próxima volta, ou use Olhar agora." : "Canal adicionado. Ligue \"Baixar sozinho\" para ele ser olhado de tempos em tempos.", "ok", 6000);
};

/** A hora, e o dia quando não é hoje ("18:01 de amanhã"). */
function canHorario(ts) {
  const dia = rotuloDia(ts);
  return dia === "hoje" ? fmtHora(ts) : `${fmtHora(ts)} de ${dia}`;
}

/** A pergunta do "Olhar agora" durante a pausa (o 409 de motivo "pausa" do servidor). */
function canTextoPausa(d) {
  if (d.tipo === "limite") return `O YouTube limitou os pedidos às ${canHorario(d.desde)}, e os downloads estão parados até ${canHorario(d.ate)}. Tentar agora costuma estender o bloqueio. Tentar mesmo assim?`;
  return `O YouTube pediu verificação às ${canHorario(d.desde)}, e os downloads estão parados até ${canHorario(d.ate)}. Tentar agora costuma estender o bloqueio. Tentar mesmo assim?`;
}

App.acoes["can-verificar"] = async (el) => {
  const corpo = el.dataset.canal ? { canal: Number(el.dataset.canal) } : {};
  try {
    await ocupado(el, () => post("/canais/verificar", corpo));
  } catch (e) {
    if (!(e instanceof ErroApi) || e.status !== 409 || e.dados.motivo !== "pausa") throw e;
    const ok = await confirmar({ titulo: "Os downloads estão parados", texto: canTextoPausa(e.dados), botao: "Tentar mesmo assim", cancelar: "Esperar", perigo: true });
    if (!ok) return;
    await ocupado(el, () => post("/canais/verificar", { ...corpo, forcar: true }));
  }
  toast("Olhando os canais em segundo plano. O que for baixado aparece aqui e na aba Na pasta.", "info", 6000);
  await canCarregar();
  atualizarEstado();
};

App.acoes["can-canal"] = async (el) => {
  const tipo = el.dataset.tipo;
  const k = PgFilmes.canais && PgFilmes.canais.canais.find((x) => String(x.id) === el.dataset.canal);
  if (tipo === "remover") {
    const ok = await confirmar({ titulo: `Remover ${k ? k.nome : "o canal"}?`, texto: "Ele deixa de ser olhado. Os vídeos que já foram baixados continuam na pasta de filmes, com os cortes deles.", botao: "Remover", perigo: true });
    if (!ok) return;
  }
  await ocupado(el, () => post(`/canais/${el.dataset.canal}/acao`, { acao: tipo }));
  await canCarregar();
  toast(tipo === "remover" ? "Canal removido" : tipo === "pausar" ? "Canal pausado" : "Canal ativado");
};

App.acoes["can-download"] = async (el) => {
  const d = PgFilmes.canais && PgFilmes.canais.downloads.find((x) => String(x.id) === el.dataset.download);
  await ocupado(el, () => post(`/downloads/${el.dataset.download}/acao`, { acao: el.dataset.tipo }));
  await canCarregar();
  // o pulado volta a passar pelas regras: só é baixado se servir nelas agora (você pode ter mudado a duração)
  toast(d && d.status === "pulado" ? "Ele volta a ser olhado na próxima vez, com as regras de agora" : "Ele volta a ser baixado na próxima olhada do canal", "info", 6000);
};

/* ------------------------------------------------------------ conta separada para baixar (opcional) */

// a janela "Entrar na conta separada" aberta agora. sessao numera cada uma: a janela antiga, ao
// fechar, não cancela a conexão que acabou de começar
const CanConta = { ativo: false, timer: null, sessao: 0 };

/** dd/mm */
function canData(ts) {
  const d = new Date(ts * 1000);
  return `${dois(d.getDate())}/${dois(d.getMonth() + 1)}`;
}

function canContaBadge(conta) {
  const s = conta && conta.situacao;
  return badge(s === "ok" ? ["Conectada", "ok"] : s === "caiu" ? ["Precisa entrar de novo", "erro"] : ["Sem conta", "neutro"]);
}

function canContaHtml(c) {
  const conta = (c && c.conta) || { situacao: "nenhuma" };
  let situacao;
  if (conta.situacao === "ok") {
    situacao = h`<p class="nota">Conectada em ${canData(conta.conectada_em)}.${conta.usada_em ? ` Usada pela última vez ${fmtRelativo(conta.usada_em)}.` : " Ainda não precisou ser usada."}</p>`;
  } else if (conta.situacao === "caiu") {
    situacao = h`<p class="aviso-inline erro">${ic("erro")}Precisa entrar de novo: ${conta.motivo || "os cookies dela não servem mais"}.</p>`;
  } else {
    situacao = h`<p class="nota">Nenhuma conta conectada. Sem ela, quando o YouTube pedir verificação, os downloads param por um tempo.</p>`;
  }
  return h`<div class="card">
      <div class="card-topo"><h3>${ic("chave")}Conta para baixar (opcional)</h3>${canContaBadge(conta)}</div>
      <p class="nota">Só quando o YouTube pedir para confirmar que não é um robô, ou quando um vídeo tiver restrição de idade, eu repito o mesmo pedido com esta conta. Cada pedido vai primeiro sem ela.</p>
      ${situacao}
      <div class="aviso-rede">${ic("alerta")}<ul class="lista-avisos">
        <li>Use uma conta do Google só para isso, nunca a do canal onde você posta.</li>
        <li>O Google pode suspender essa conta.</li>
        <li>Baixar continua contra os termos do YouTube.</li>
        <li>Só canais seus ou que autorizam cortes.</li>
      </ul></div>
      <div class="linha-botoes">
        <button class="btn${conta.situacao === "ok" ? "" : " primario"}" data-acao="can-conta-entrar">${ic("chave")}Entrar numa conta separada</button>
        ${conta.situacao !== "nenhuma" ? h`<button class="btn fantasma" data-acao="can-conta-esquecer">${ic("lixo")}Esquecer a conta</button>` : ""}
      </div>
    </div>`;
}

function canPararConexao() {
  CanConta.ativo = false;
  clearTimeout(CanConta.timer);
}

function canConexaoErroHtml(s) {
  return h`<div class="login-espera">${ic("erro", "grande")}<p class="erro-texto">${s.mensagem}</p><div class="acoes-modal"><button class="btn primario" data-acao="fechar-modal">Fechar</button></div></div>`;
}

/** Acompanha a janela de conectar até a conta entrar, passar o prazo, dar erro ou ser cancelada. */
async function canAcompanharConexao(sessao) {
  if (!CanConta.ativo || CanConta.sessao !== sessao) return;
  let r;
  try {
    r = await api("/canais/conta");
  } catch (e) {
    if (CanConta.ativo && CanConta.sessao === sessao) CanConta.timer = setTimeout(() => canAcompanharConexao(sessao), 3000);
    return;
  }
  if (!CanConta.ativo || CanConta.sessao !== sessao) return;
  const s = r.conexao || {};
  if (s.estado === "ok") {
    canPararConexao();
    fecharModal();
    toast("Conta separada conectada. Ela só é usada quando o YouTube pedir.", "ok", 7000);
    await canCarregar();
    atualizarEstado();
  } else if (s.estado === "erro" || s.estado === "prazo") {
    canPararConexao();
    montar(corpoModal(), canConexaoErroHtml(s));
  } else if (["abrindo", "aguardando", "salvando"].includes(s.estado)) {
    const texto = $("#can-conexao-texto");
    if (texto && s.estado === "salvando") texto.textContent = s.mensagem;
    CanConta.timer = setTimeout(() => canAcompanharConexao(sessao), 2000);
  } else {
    canPararConexao(); // cancelado, ou nenhuma janela
  }
}

App.acoes["can-conta-entrar"] = async (el) => {
  const ok = await confirmar({
    titulo: "Entrar numa conta separada?",
    texto: "Vou abrir uma janela do Chrome com um perfil novo e temporário. Use uma conta do Google só para isso, nunca a do canal onde você posta: o Google pode suspender essa conta. Baixar continua contra os termos do YouTube, então use só canais seus ou que autorizam cortes. Quando a conta entrar, a janela fecha sozinha e eu guardo só os cookies do YouTube e do Google, cifrados.",
    botao: "Abrir a janela",
  });
  if (!ok) return;
  await ocupado(el, () => post("/canais/conta/conectar"));
  const sessao = ++CanConta.sessao;
  CanConta.ativo = true;
  abrirModal({
    titulo: "Entrar na conta separada",
    largura: 520,
    corpo: h`<div class="login-espera"><span class="giro"></span><p id="can-conexao-texto">Entre na conta separada na janela que abriu. Esta janela fecha sozinha quando a conta entrar (até 10 min).</p><p class="nota">Use uma conta do Google só para isso, nunca a do canal onde você posta.</p><div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button></div></div>`,
    aoFechar: () => {
      if (CanConta.sessao !== sessao) return; // outra janela já tomou o lugar desta
      if (CanConta.ativo) post("/canais/conta/cancelar").then(() => canCarregar()).catch(() => {});
      canPararConexao();
    },
  });
  CanConta.timer = setTimeout(() => canAcompanharConexao(sessao), 2000);
};

App.acoes["can-conta-esquecer"] = async (el) => {
  const ok = await confirmar({
    titulo: "Esquecer a conta separada?",
    texto: "Eu apago os cookies guardados e o que tiver sobrado da janela temporária. A conta continua existindo no Google. Sem ela, quando o YouTube pedir verificação, os downloads param por um tempo.",
    botao: "Esquecer a conta",
    perigo: true,
  });
  if (!ok) return;
  PgFilmes.canais = await ocupado(el, () => post("/canais/conta/esquecer"));
  await canCarregar();
  toast("Conta esquecida");
};

/* ------------------------------------------------------------ página */

function filmesAbasHtml() {
  const r = PgFilmes.dados;
  const c = PgFilmes.canais;
  const aba = (id, rotulo, n, icone) => h`<button type="button" role="tab" class="aba ${PgFilmes.aba === id ? "ativa" : ""}" aria-selected="${PgFilmes.aba === id}" data-acao="filmes-aba" data-aba="${id}">${icone ? ic(icone) : ""}${rotulo}${n !== null ? h`<span class="n">${n}</span>` : ""}</button>`;
  return h`<div class="barra-ferramentas"><div class="abas" role="tablist">${aba("pasta", "Na pasta", r ? r.filmes.length : null, "")}${aba("canais", "Canais do YouTube", c ? c.canais.length : null, "baixar")}</div></div>`;
}

function filmesPastaHtml(r) {
  return h`${zonaEnvioHtml(r)}
    <div class="barra-ferramentas">
      <p class="nota">Pasta: ${r.pasta}</p>
      <div class="grupo-botoes"><button class="btn pequeno" data-acao="varrer-filmes">${ic("busca")}Procurar filmes novos</button><button class="btn pequeno" data-acao="abrir-pasta" data-alvo="filmes">${ic("pasta")}Abrir a pasta</button></div>
    </div>
    <div id="lista-filmes"></div>
    <p class="nota rodape-card">Use filmes que você tem direito de publicar: domínio público, Creative Commons, próprios ou licenciados. Cortes de filmes protegidos geram bloqueios e podem derrubar a conta.</p>`;
}

/** Desenha só o conteúdo da aba aberta (as abas ficam). */
function filmesDesenharAba() {
  const alvo = $("#filmes-aba");
  if (!alvo) return;
  const abas = $("#filmes-abas");
  if (abas) montar(abas, filmesAbasHtml());
  if (PgFilmes.aba === "canais") {
    montar(alvo, canCorpoHtml(PgFilmes.canais));
    canPrepararCampo();
  } else {
    montar(alvo, filmesPastaHtml(PgFilmes.dados));
    montarSeMudou($("#lista-filmes"), listaFilmesHtml(PgFilmes.dados));
    prepararZonaEnvio();
  }
}

App.acoes["filmes-aba"] = async (el) => {
  PgFilmes.aba = el.dataset.aba === "canais" ? "canais" : "pasta";
  history.replaceState(null, "", PgFilmes.aba === "canais" ? "#/filmes?aba=canais" : "#/filmes");
  App.hashAtual = location.hash;
  filmesDesenharAba();
  if (PgFilmes.aba === "canais") await canCarregar();
  else await carregarFilmes();
};

App.paginas.filmes = {
  titulo: "Filmes",
  subtitulo: "Os filmes de onde saem os cortes, e os canais do YouTube que mandam vídeos novos",
  async render(el, token) {
    const aba = consultaDaRota().get("aba");
    if (aba === "canais" || aba === "pasta") PgFilmes.aba = aba;
    await garantirConfig(); // a chave "Baixar sozinho" mostra o valor salvo
    const [r, c] = await Promise.all([api("/filmes"), api("/canais")]);
    if (!App.ativa(token)) return;
    PgFilmes.dados = r;
    PgFilmes.canais = c;
    montar(el, h`<div id="filmes-abas">${filmesAbasHtml()}</div><div id="filmes-aba"></div>`);
    filmesDesenharAba();
  },
  aoMudar(caminho) {
    // a chave liga e desliga na hora, sem esperar o Salvar das Configurações
    if (caminho === "canais.ativo") {
      salvarConfig(["canais.ativo"]).then(() => canCarregar()).catch(erroToast);
    }
  },
  aoEstado(e) {
    if (PgFilmes.aba === "canais") {
      if (!PgFilmes.canais || !$("#can-situacao")) return;
      const assinatura = JSON.stringify([e.motor.rodando, e.numeros.filmes, e.numeros.filmes_na_fila,
        e.atividades.filter((a) => a.chave === "canais" || a.chave === "canais-agora").map((a) => [a.texto, a.ocioso, a.pct === null ? null : Math.floor(a.pct / 5)])]);
      if (assinatura !== PgFilmes.assinaturaCanais) {
        PgFilmes.assinaturaCanais = assinatura;
        canCarregar().catch(() => {});
      }
      return;
    }
    if (!PgFilmes.dados || !$("#lista-filmes")) return;
    const assinatura = JSON.stringify([e.numeros.filmes, e.numeros.filmes_na_fila, e.numeros.filmes_erro, e.numeros.candidatos,
      e.atividades.map((a) => [a.texto, a.pct === null ? null : Math.floor(a.pct / 5)])]);
    if (assinatura !== PgFilmes.ultimaAssinatura) {
      PgFilmes.ultimaAssinatura = assinatura;
      carregarFilmes().catch(() => {});
    }
  },
  async recarregar() {
    if (PgFilmes.aba === "canais") await canCarregar();
    else await carregarFilmes();
  },
};
