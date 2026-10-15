/* AutoCortes - painel: Criação (pautas e temas dos vídeos feitos do zero) */
"use strict";

// temasBase/versao: a fila de temas como veio do servidor. Se você mexeu na caixa, ela não é
// trocada nas atualizações, e salvar com uma versão velha volta 409 com o texto novo.
const PgCriacao = { dados: null, criando: false, temasBase: "", versao: null, pautaTexto: null };

const CRI_ESTADO = {
  novo: ["Esperando para criar", "info"],
  criando: ["Virando vídeo", "acento"],
  analisado: ["Vídeo criado", "ok"],
  erro: ["Com erro", "erro"],
  ausente: ["Arquivo apagado", "neutro"],
  ignorado: ["Ignorada", "neutro"],
};

const CRI_EXEMPLO = `titulo: Comece pequeno
termos: mar ao amanhecer, montanha com neblina, cidade de noite
topo: COMECE HOJE
hashtags: motivacao, disciplina
---
Ninguém constrói nada grande em um dia.
Você constrói em mil dias pequenos, quase iguais, quase chatos.
[pausa: 1s] O segredo é não deixar de aparecer.`;

function criAvisosHtml(d) {
  const avisos = [];
  if (!d.ativo) {
    avisos.push(h`<div class="aviso-inline">${ic("info")}<div>A criação automática está desligada: o motor não vai gerar estes vídeos sozinho. Ligue em <a href="#/config?aba=criacao">Configurações</a>, ou use "Criar agora" em cada pauta.</div></div>`);
  }
  if (!d.material.ok) {
    const onde = d.material.fonte === "pasta"
      ? h`Ponha vídeos ou imagens em <code>${d.material.pasta}</code>`
      : h`Informe a chave da API do ${d.material.fonte} em <a href="#/config?aba=criacao">Configurações</a>`;
    avisos.push(h`<div class="aviso-inline erro">${ic("alerta")}<div>Sem imagens de fundo, nenhum vídeo é montado. ${onde}.</div></div>`);
  }
  if (!d.ia) {
    avisos.push(h`<div class="aviso-inline">${ic("info")}<div>Com a IA desligada, você mesmo escreve o roteiro de cada pauta. Ligando a IA, basta o tema.</div></div>`);
  }
  return avisos;
}

function criCartaoHtml(p) {
  const estado = CRI_ESTADO[p.status] || [p.status, "neutro"];
  const criando = p.status === "criando";
  const capa = p.corte && p.corte.miniatura
    ? h`<img src="${midia(p.corte.miniatura)}" alt="" loading="lazy">`
    : h`<div class="capa-vazia">${criando ? h`<span class="giro"></span>` : ic("varinha")}<small>${criando ? "Virando vídeo..." : p.status === "novo" ? "Ainda não criado" : "Sem vídeo"}</small></div>`;
  return h`<article class="card card-pauta">
    <div class="pauta-capa" data-acao="cri-abrir" data-pauta="${p.id}" role="button" tabindex="0" aria-label="Abrir a pauta ${p.titulo}">
      ${capa}${p.corte ? h`<span class="tempo">${fmtDuracao(p.corte.duracao)}</span>` : ""}
    </div>
    <div class="pauta-info">
      <div class="card-topo"><h3 title="${p.titulo}">${p.titulo}</h3>${badge(estado, true)}</div>
      <ul class="lista-simples">
        <li><span>Arquivo</span><b>${p.arquivo}</b></li>
        <li><span>Vídeos feitos</span><b>${p.videos || 0}</b></li>
      </ul>
      ${p.erro ? h`<p class="erro-texto">${p.erro}</p>` : ""}
      <div class="rodape-card">
        <div class="grupo-botoes">
          <button class="btn pequeno" data-acao="cri-abrir" data-pauta="${p.id}">${ic("lapis")}Ver e editar</button>
          ${p.corte ? h`<button class="btn pequeno fantasma" data-acao="abrir-corte" data-corte="${p.corte.id}">${ic("olho")}Ver o vídeo</button>` : ""}
        </div>
        <div class="grupo-botoes">
          <button class="btn pequeno primario" data-acao="cri-criar" data-pauta="${p.id}" ${p.existe && !criando ? "" : raw("disabled")}>${ic("varinha")}${p.videos ? "Criar outro" : "Criar agora"}</button>
        </div>
      </div>
    </div>
  </article>`;
}

function criPautasHtml(d) {
  return d.pautas.length
    ? h`<div class="cards-redes">${d.pautas.map(criCartaoHtml)}</div>`
    : vazioBloco("varinha", "Nenhuma pauta ainda", "Escreva um tema aí em cima, ou ponha vários na fila de temas.");
}

const criContaTemas = (d) => (d.temas.length ? plural(d.temas.length, "tema esperando", "temas esperando") : "vazia");

function criPaginaHtml(d) {
  return h`<div class="card descricao-aba">
      <p class="nota">Vídeos montados do zero: a narração é feita por uma voz sintética, as imagens de fundo vêm do seu material (ou de um banco gratuito) e a legenda sai palavra por palavra, no tempo exato da fala. Cada vídeo vira um corte normal, que entra na fila e na agenda como os outros.</p>
    </div>
    <div id="cri-avisos">${criAvisosHtml(d)}</div>
    <div class="grade-2">
      <div class="card">
        <div class="card-topo"><h3>${ic("lista")}Fila de temas</h3><span class="texto-suave" id="cri-temas-conta">${criContaTemas(d)}</span></div>
        <p class="nota">Um tema por linha. O motor pega o primeiro, pede o roteiro à IA, grava a pauta e cria o vídeo. O tema usado vira uma linha <code># feito:</code>, que fica como histórico (linha com <code>#</code> é ignorada).</p>
        <textarea id="cri-temas" rows="7" spellcheck="false" placeholder="o poder do primeiro passo&#10;rotina de quem acorda cedo&#10;por que disciplina vence motivação">${d.temas_texto || ""}</textarea>
        <div class="linha-botoes"><button class="btn pequeno primario" data-acao="cri-salvar-temas">${ic("check")}Salvar a fila</button><span class="nota">Arquivo: ${d.pasta}\\${d.arquivo_temas}</span></div>
      </div>
      <div class="card">
        <div class="card-topo"><h3>${ic("mais")}Nova pauta</h3></div>
        <p class="nota">Escreva o assunto. Com a IA ligada, ela já escreve o roteiro, os termos de busca e os textos do post; sem IA, você recebe uma pauta em branco para preencher.</p>
        <label class="campo"><span>Tema ou título</span><input type="text" id="cri-novo-tema" maxlength="120" placeholder="a diferença entre motivação e disciplina"></label>
        <div class="linha-botoes">
          ${d.ia ? h`<button class="btn pequeno primario" data-acao="cri-nova" data-ia="1">${ic("varinha")}A IA escreve</button>` : ""}
          <button class="btn pequeno" data-acao="cri-nova">${ic("lapis")}Criar em branco</button>
        </div>
        <p class="nota" id="cri-voz">${criVozHtml(d)}</p>
      </div>
    </div>
    <div id="cri-pautas">${criPautasHtml(d)}</div>
    <p class="nota rodape-card">Juntar clipe de banco com voz sintética é o formato que as redes mais filtram: serve para crescer e testar nicho, mas não conte com monetização direta.</p>`;
}

const criVozHtml = (d) => h`A voz atual é <b>${d.voz_atual}</b> e o roteiro que a IA escreve mira <b>${d.duracao_alvo} s</b>. Muda em Configurações.`;

/** Desenha a tela. Depois da primeira vez, troca só o que muda: o que você está digitando fica. */
function criDesenhar(d) {
  PgCriacao.dados = d;
  const el = $("#conteudo");
  if (!$("#cri-pautas")) {
    montar(el, criPaginaHtml(d));
    PgCriacao.temasBase = d.temas_texto || "";
    PgCriacao.versao = d.temas_versao;
    return;
  }
  montarSeMudou($("#cri-avisos"), criAvisosHtml(d));
  montarSeMudou($("#cri-pautas"), criPautasHtml(d));
  montarSeMudou($("#cri-voz"), criVozHtml(d));
  const conta = $("#cri-temas-conta");
  if (conta) conta.textContent = criContaTemas(d);
  const area = $("#cri-temas");
  if (area && area.value === PgCriacao.temasBase) {
    // sem mudança sua na caixa: mostra a fila como está agora (o motor pode ter usado um tema)
    if (area.value !== (d.temas_texto || "")) area.value = d.temas_texto || "";
    PgCriacao.temasBase = d.temas_texto || "";
    PgCriacao.versao = d.temas_versao;
  }
}

/** Busca e desenha, se a tela Criação ainda for a aberta (trocar de tela no meio não leva a Criação junto). */
async function criCarregar(token = App.navegacao) {
  const d = await api("/pautas");
  if (!App.ativa(token) || App.nomePagina !== "criacao") return null;
  criDesenhar(d);
  return d;
}

const criDe = (id) => (PgCriacao.dados || { pautas: [] }).pautas.find((p) => String(p.id) === String(id));
const criAindaAqui = (token) => App.ativa(token) && App.nomePagina === "criacao";

/* ------------------------------------------------------------ ações */

App.acoes["cri-salvar-temas"] = async (el) => {
  const area = $("#cri-temas");
  if (!area) return;
  const texto = area.value;
  try {
    const r = await ocupado(el, () => post("/pautas/temas", { texto, versao: PgCriacao.versao }));
    PgCriacao.temasBase = r.temas_texto;
    PgCriacao.versao = r.temas_versao;
    if (area.isConnected) area.value = r.temas_texto;
    toast("Fila de temas salva");
  } catch (e) {
    if (e.status === 409 && e.dados && typeof e.dados.texto === "string") {
      // o motor usou um tema enquanto você editava: a caixa mostra o arquivo novo
      PgCriacao.temasBase = e.dados.texto;
      PgCriacao.versao = e.dados.versao;
      if (area.isConnected) area.value = e.dados.texto;
      toast(e.message, "erro", 9000);
      return;
    }
    throw e;
  }
  await criCarregar();
};

App.acoes["cri-nova"] = async (el) => {
  const token = App.navegacao;
  const campo = $("#cri-novo-tema");
  const tema = String(campo.value || "").trim();
  if (!tema) {
    toast("Escreva o tema da pauta", "erro");
    campo.focus();
    return;
  }
  const comIa = el.dataset.ia === "1";
  const r = await ocupado(el, () => post("/pautas/nova", { tema, com_ia: comIa }));
  toast(comIa ? `A IA escreveu a pauta '${r.pauta.titulo}'` : `Pauta '${r.pauta.titulo}' criada`, "ok", 6000);
  if (!criAindaAqui(token)) return; // você foi para outra tela enquanto a IA escrevia
  if (campo.isConnected && campo.value.trim() === tema) campo.value = "";
  await criCarregar(token);
  if (criAindaAqui(token)) await criAbrir(r.pauta.id, token);
};

App.acoes["cri-criar"] = async (el) => {
  const p = criDe(el.dataset.pauta);
  await ocupado(el, () => post(`/pautas/${el.dataset.pauta}/acao`, { acao: "criar_agora" }));
  toast(`Criando o vídeo de '${p ? p.titulo : "a pauta"}'. Acompanhe no Início.`, "info", 7000);
  await criCarregar();
};

async function criAbrir(id, token = App.navegacao) {
  const d = await api(`/pautas/${id}`);
  if (!criAindaAqui(token)) return;
  PgCriacao.pautaTexto = d.texto;
  const criando = d.status === "criando";
  abrirModal({
    titulo: d.titulo,
    largura: 760,
    corpo: h`<div class="editor-roteiro">
      ${d.erro ? h`<div class="aviso-inline erro">${ic("alerta")}<div>${d.erro}</div></div>` : ""}
      ${criando ? h`<div class="aviso-inline">${ic("relogio")}<div>Esta pauta está virando vídeo agora. Dá para ler, mas só dá para salvar depois que o vídeo sair.</div></div>` : ""}
      ${(d.avisos || []).length ? h`<div class="aviso-inline">${ic("info")}<div>${d.avisos.map((a) => h`<div>${a}</div>`)}</div></div>` : ""}
      <p class="nota">Cabeçalho (<code>titulo</code>, <code>termos</code>, <code>topo</code>, <code>voz</code>, <code>musica</code>, <code>hashtags</code>), uma linha com <code>---</code> e o roteiro. Cada linha do roteiro dá um respiro na fala; <code>[pausa: 2s]</code> faz silêncio no ponto exato. Ctrl+S salva.</p>
      <textarea id="cri-texto" rows="18" spellcheck="false" placeholder="${CRI_EXEMPLO}">${d.texto}</textarea>
      <p class="nota">Arquivo: ${d.caminho}</p>
      <div class="acoes-modal">
        <button class="btn fantasma" data-acao="cri-excluir" data-pauta="${d.id}" ${criando ? raw("disabled") : ""}>${ic("lixo")}Excluir a pauta</button>
        ${PgCriacao.dados && PgCriacao.dados.ia ? h`<button class="btn" data-acao="cri-ia" data-pauta="${d.id}" ${criando ? raw("disabled") : ""}>${ic("varinha")}A IA reescreve o roteiro</button>` : ""}
        <button class="btn primario" data-acao="cri-salvar" data-pauta="${d.id}" ${criando ? raw("disabled") : ""}>${ic("check")}Salvar</button>
      </div>
    </div>`,
    protegido: () => {
      const area = $("#cri-texto");
      return Boolean(area) && PgCriacao.pautaTexto !== null && area.value !== PgCriacao.pautaTexto;
    },
    aoSalvar: () => (criando ? toast("Espere o vídeo sair para salvar a pauta", "info") : criSalvarPauta(d.id)),
    aoFechar: () => { PgCriacao.pautaTexto = null; },
  });
}

App.acoes["cri-abrir"] = (el) => criAbrir(el.dataset.pauta);

async function criSalvarPauta(id, el = null) {
  const area = $("#cri-texto");
  if (!area) return;
  const texto = area.value;
  try {
    await ocupado(el, () => post(`/pautas/${id}`, { texto }));
  } catch (e) {
    toast((e && e.message) || "Não deu para salvar", "erro", 8000);
    return;
  }
  PgCriacao.pautaTexto = texto;
  fecharModal();
  toast("Pauta salva");
  await criCarregar();
}

App.acoes["cri-salvar"] = (el) => criSalvarPauta(el.dataset.pauta, el);

App.acoes["cri-ia"] = async (el) => {
  const token = App.navegacao;
  const certo = await confirmar({
    titulo: "A IA reescreve o roteiro?",
    texto: "O roteiro que está na pauta agora será substituído pelo que a IA escrever, a partir do tema e do título. O que você mudou nesta janela e não salvou também se perde.",
    botao: "Reescrever",
  });
  if (!certo) return;
  await ocupado(el, () => post(`/pautas/${el.dataset.pauta}/acao`, { acao: "escrever_com_ia" }));
  toast("A IA reescreveu o roteiro", "ok");
  if (!criAindaAqui(token)) return;
  PgCriacao.pautaTexto = null; // o texto novo substitui a janela: nada a perguntar ao fechar
  await criCarregar(token);
  if (criAindaAqui(token)) await criAbrir(el.dataset.pauta, token);
};

App.acoes["cri-excluir"] = async (el) => {
  const p = criDe(el.dataset.pauta);
  const certo = await confirmar({
    titulo: `Excluir a pauta '${p ? p.titulo : ""}'?`,
    texto: "O arquivo da pauta é apagado. Os vídeos que ela já criou continuam na fila e no histórico.",
    botao: "Excluir",
    perigo: true,
  });
  if (!certo) return;
  await ocupado(el, () => post(`/pautas/${el.dataset.pauta}/acao`, { acao: "excluir" }));
  PgCriacao.pautaTexto = null;
  fecharModal();
  toast("Pauta excluída", "info");
  await criCarregar();
};

/* ------------------------------------------------------------ página */

App.paginas.criacao = {
  titulo: "Criação",
  subtitulo: "Vídeos montados do zero: pautas, temas e narração",
  async render(el, token) {
    const d = await api("/pautas");
    if (!App.ativa(token)) return;
    montar(el, ""); // a primeira vez monta a tela inteira
    criDesenhar(d);
  },
  aoEstado(e) {
    // a atividade troca de nome no meio do trabalho (criando -> narrando -> montando):
    // por isso a tela só recarrega quando o motor fica livre de novo
    if (!PgCriacao.dados) return;
    const ocupado = ((e && e.atividades) || []).some((a) => /criando o v|narra|montando|espera a edição/i.test(a.texto || ""));
    if (ocupado) {
      if (!PgCriacao.criando) criCarregar().catch(() => {}); // mostra o "Virando vídeo" no cartão
      PgCriacao.criando = true;
      return;
    }
    if (PgCriacao.criando) {
      PgCriacao.criando = false;
      criCarregar().catch(() => {});
    }
  },
  async recarregar() {
    await criCarregar();
  },
};
