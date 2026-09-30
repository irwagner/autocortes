/* AutoCortes - painel: Redes sociais (contas, forma de envio e opções de cada rede) */
"use strict";

const PgRedes = { dados: null };
const Login = { rede: null, ativo: false, timer: null };
const PERMISSOES_IG = "instagram_basic, instagram_content_publish, instagram_manage_insights, pages_show_list, pages_read_engagement, business_management";
// para o Reel sair também na Página do Facebook
const PERMISSAO_PAGINA = "pages_manage_posts";
const permissoesIg = () => (valor("instagram.pagina_facebook") ? `${PERMISSOES_IG}, ${PERMISSAO_PAGINA}` : PERMISSOES_IG);
const NOME_ENVIO = {
  oficial: (info) => `API oficial do ${info.rotulo}`,
  upload_post: () => "Upload-Post (pago, publica em público)",
  navegador: () => "Pelo seu navegador (arriscado)",
  manual: () => "À mão (você posta pelo app ou pelo site)",
};
// redes que só aceitam a postagem à mão
const ENVIO_FIXO = { kwai: "À mão, pelo app do Kwai" };

const PRIVACIDADE_TIKTOK = [
  ["PUBLIC_TO_EVERYONE", "Todos"], ["FOLLOWER_OF_CREATOR", "Seguidores"],
  ["MUTUAL_FOLLOW_FRIENDS", "Amigos (seguidores mútuos)"], ["SELF_ONLY", "Só eu"],
];

function link(url, texto) {
  return h`<a href="${url}" target="_blank" rel="noopener noreferrer">${texto}</a>`;
}

function guiaOficial(rede) {
  if (rede === "youtube") {
    return h`<details class="guia"><summary>${ic("info")}Como conseguir as chaves do YouTube</summary><ol>
      <li>Em ${link("https://console.cloud.google.com/", "console.cloud.google.com")}, crie um projeto e ative a "YouTube Data API v3".</li>
      <li>Na tela de consentimento OAuth, escolha público Externo, adicione seu e-mail como usuário de teste e publique o app. Em modo "Teste", o login expira a cada 7 dias.</li>
      <li>Em Credenciais, crie um "ID do cliente OAuth" do tipo "App para computador".</li>
      <li>Cole o Client ID e o Client secret aqui, salve e clique em Conectar conta.</li>
    </ol></details>`;
  }
  if (rede === "tiktok") {
    const retorno = (App.meta && App.meta.tiktok_redirect) || "http://127.0.0.1:8765/callback/";
    return h`<details class="guia"><summary>${ic("info")}Como conseguir as chaves do TikTok</summary><ol>
      <li>Em ${link("https://developers.tiktok.com/", "developers.tiktok.com")}, abra Manage apps e crie um app.</li>
      <li>Adicione "Login Kit" e "Content Posting API", com "Direct Post" ligado.</li>
      <li>No Login Kit, plataforma Desktop, cadastre este endereço de retorno:<div class="copiavel"><code>${retorno}</code><button class="btn pequeno icone" data-acao="copiar" data-texto="${retorno}" title="Copiar" aria-label="Copiar o endereço">${ic("copiar")}</button></div></li>
      <li>Cole o Client key e o Client secret aqui, salve e clique em Conectar conta.</li>
    </ol></details>`;
  }
  return h`<details class="guia"><summary>${ic("info")}Como preparar o Instagram</summary><ol>
    <li>A conta precisa ser profissional (Criador ou Empresa) e estar ligada a uma Página do Facebook.</li>
    <li>Em ${link("https://developers.facebook.com/apps/", "developers.facebook.com")}, crie um app do tipo Empresa e adicione o produto Instagram (API com login do Facebook).</li>
    <li>Cole o App ID e a chave secreta aqui e salve.</li>
    <li>Clique em Conectar conta e siga os passos para gerar o token.</li>
    <li>Para postar também na Página: adicione ao app o caso de uso de gerenciar a Página, conecte com a permissão ${PERMISSAO_PAGINA} e deixe o app no modo publicado (em desenvolvimento, o post na Página só aparece para quem tem função no app).</li>
  </ol></details>`;
}

function guiaManual(rede) {
  const passos = {
    kwai: h`<li>No horário, a tarefa aparece no Início com o vídeo e a legenda.</li><li>Passe o vídeo para o celular (baixe ou use a pasta sincronizada abaixo).</li><li>No app do Kwai, toque na câmera, depois em Álbum, escolha o vídeo, cole a legenda e publique.</li><li>Volte ao painel e marque "Já postei".</li>`,
    bilibili: h`<li>No horário, a tarefa aparece no Início com o vídeo, o título, a descrição e as tags.</li><li>Abra a ${link("https://member.bilibili.com/platform/upload/video/frame", "página de envio do Bilibili")} e escolha o vídeo.</li><li>Em 类型, marque 转载 (repost) e informe a fonte; em 分区, escolha 影视 > 影视剪辑.</li><li>Cole os textos, clique em 立即投稿 e marque "Já postei" no painel.</li>`,
  }[rede] || h`<li>No horário, a tarefa aparece no Início com o vídeo e os textos no formato da rede.</li><li>Baixe o vídeo (ou pegue na pasta sincronizada), poste pelo app ou pelo site e cole os textos.</li><li>Volte ao painel e marque "Já postei". Se preferir, agende direto na rede.</li>`;
  return h`<details class="guia"><summary>${ic("info")}Como funciona a postagem à mão</summary><ol>${passos}</ol></details>`;
}

function guiaNavegador(rede) {
  const onde = { youtube: "YouTube Studio", tiktok: "TikTok Studio", instagram: "Instagram", bilibili: "Bilibili" }[rede];
  return h`<details class="guia"><summary>${ic("info")}Como funciona o envio pelo navegador</summary><ol>
    <li>Clique em <b>Conectar</b>: abre uma janela do Chrome com um perfil só do AutoCortes (em <code>dados/chrome</code>), separado do seu navegador do dia a dia.</li>
    <li>Entre na conta do ${REDES[rede].rotulo} nessa janela, resolvendo o 2FA se ele pedir. A sessão fica salva ali e sobrevive a reiniciar o PC.</li>
    <li>Clique em <b>Testar</b>: eu abro a página do ${onde} e confirmo que a sessão está de pé.</li>
    <li>No horário, o AutoCortes preenche a página de envio e publica, com pausas entre os passos.</li>
    <li>Antes de valer, ligue o <b>ensaio</b> no cartão "Postagem pelo navegador": ele preenche tudo, não publica e guarda uma imagem da tela.</li>
  </ol></details>`;
}

function avisoRede(rede, envio) {
  const privacidadeTiktok = valor("tiktok.privacidade");
  if (envio === "navegador") {
    return `Isto contraria os termos do ${REDES[rede].rotulo}, que só autorizam a API oficial, e pode custar a conta. Quando a rede muda o layout da página, o envio falha até eu ajustar. Use numa conta que você pode perder.`;
  }
  if (rede === "kwai") {
    return "O Kwai não tem API de postagem para criadores nem envio pelo site: você posta pelo app, com o vídeo e a legenda que o AutoCortes separa.";
  }
  if (rede === "bilibili") {
    return "A API de envio do Bilibili é só para empresas chinesas cadastradas. O corte é de um filme de terceiros: marque 转载 (repost) com a fonte, e esse tipo de vídeo não entra no programa de incentivo do Bilibili.";
  }
  if (envio === "manual") {
    const fb = rede === "instagram" && valor("instagram.pagina_facebook")
      ? " Para sair também na Página, ligue \"Compartilhar no Facebook\" no app ao postar." : "";
    return `Nada é enviado sozinho: no horário, o ${REDES[rede].rotulo} ganha uma tarefa no Início com o vídeo e os textos.${fb}`;
  }
  if (rede === "youtube" && envio === "oficial") {
    return "Projetos sem a auditoria da API do YouTube sobem os vídeos como privados. Para sair público, peça a auditoria (grátis, sem prazo) ou use o Upload-Post.";
  }
  if (rede === "tiktok" && envio === "oficial") {
    return "Sem auditoria, o TikTok só aceita posts visíveis só para você, com a conta privada, e não audita ferramentas de uso próprio. Para postar em público, use o Upload-Post.";
  }
  if (rede === "tiktok" && privacidadeTiktok === "SELF_ONLY") {
    return "A privacidade está em \"Só eu\". Troque para \"Todos\" para publicar em público.";
  }
  if (rede === "instagram" && valor("instagram.pagina_facebook")) {
    return envio === "oficial"
      ? `O Reel vai também para a Página do Facebook. Conecte de novo com a permissão ${PERMISSAO_PAGINA} e deixe o app da Meta no modo publicado. Se a Central de Contas já compartilha seus Reels no Facebook, desligue lá para não sair repetido.`
      : "O Reel vai também para a Página do Facebook conectada no Upload-Post. Se a Central de Contas já compartilha seus Reels no Facebook, desligue lá para não sair repetido.";
  }
  if (rede === "instagram" && envio === "oficial") {
    return "Precisa de conta profissional ligada a uma Página do Facebook. A legenda do post leva no máximo 5 hashtags.";
  }
  return "";
}

function statusContaHtml(r, envio) {
  let classe;
  let icone;
  let titulo;
  let sub;
  if (!r.ativo) {
    classe = "pendente"; icone = "pausa"; titulo = "Desativada"; sub = "Ligue a chave acima para postar nesta rede.";
  } else if (r.bloqueada) {
    classe = "erro"; icone = "erro"; titulo = "Pausada por um erro"; sub = r.bloqueada;
  } else if (r.via === "manual") {
    classe = "ok"; icone = "ok"; titulo = "Postagem à mão";
    sub = r.tarefas ? `${plural(r.tarefas, "tarefa esperando", "tarefas esperando")} você no Início` : "Os horários viram tarefas no Início";
  } else if (r.via === "navegador") {
    classe = r.pronta ? "ok" : "pendente";
    icone = r.pronta ? "ok" : "alerta";
    titulo = r.pronta ? r.conta || "Sessão salva no navegador" : "Sem sessão no navegador";
    sub = r.pronta ? "pelo navegador, com a sua sessão" : r.motivo;
  } else if (r.pronta) {
    classe = "ok"; icone = "ok"; titulo = r.conta || "Pronta para postar"; sub = ROTULO_ENVIO[r.via] || r.via;
    if (r.facebook && r.facebook.ativo) sub += ` · também na Página${r.facebook.pagina ? ` ${r.facebook.pagina}` : " do Facebook"}`;
  } else {
    classe = "pendente"; icone = "alerta"; titulo = "Não conectada"; sub = r.motivo;
  }
  const trocando = envio !== r.via ? h`<small class="aviso-inline">${ic("alerta")}Salve para trocar a forma de envio.</small>` : "";
  return h`<div class="status-conta ${classe}">${ic(icone)}<div><b>${titulo}</b><small title="${sub}">${sub}</small>${trocando}</div></div>`;
}

function camposOficiais(rede) {
  if (rede === "youtube") {
    return [
      campo({ chave: "youtube.client_id", tipo: "texto", rotulo: "Client ID", placeholder: "....apps.googleusercontent.com" }),
      campo({ chave: "youtube.client_secret", tipo: "senha", rotulo: "Client secret" }),
    ];
  }
  if (rede === "tiktok") {
    return [
      campo({ chave: "tiktok.client_key", tipo: "texto", rotulo: "Client key" }),
      campo({ chave: "tiktok.client_secret", tipo: "senha", rotulo: "Client secret" }),
    ];
  }
  return [
    campo({ chave: "instagram.app_id", tipo: "texto", rotulo: "App ID" }),
    campo({ chave: "instagram.app_secret", tipo: "senha", rotulo: "Chave secreta do app" }),
  ];
}

function opcoesRede(rede) {
  if (rede === "youtube") {
    return [
      campo({ chave: "youtube.max_segundos", tipo: "numero", min: 15, max: 180, sufixo: "s", rotulo: "Duração máxima", ajuda: "O YouTube só recebe Shorts: vídeo vertical ou quadrado de até 3 min. Cortes mais longos que isto ficam só para as outras redes." }),
      campo({ chave: "youtube.privacidade", tipo: "select", rotulo: "Privacidade", opcoes: [["public", "Público"], ["unlisted", "Não listado"], ["private", "Privado"]] }),
      campo({ chave: "youtube.categoria", tipo: "select", rotulo: "Categoria", opcoes: [["1", "Filmes e animação"], ["24", "Entretenimento"], ["22", "Pessoas e blogs"]] }),
      campo({ chave: "youtube.adicionar_shorts", tipo: "switch", rotulo: "Colocar #shorts no título" }),
      campo({ chave: "youtube.tags", tipo: "chips", rotulo: "Tags do vídeo", larga: true, placeholder: "Digite e tecle Enter" }),
    ];
  }
  if (rede === "bilibili") {
    return [
      campo({ chave: "bilibili.tags", tipo: "chips", rotulo: "Tags fixas", ajuda: "Vão em todo post, junto com as hashtags do corte (sem o #). Em chinês ajudam na busca: 电影 é filme e 影视剪辑 é corte de filme.", larga: true, placeholder: "Digite e tecle Enter" }),
    ];
  }
  if (rede === "kwai") return [];
  if (rede === "tiktok") {
    if (valor("tiktok.envio") === "manual") return []; // você escolhe essas opções no app, ao postar
    return [
      campo({ chave: "tiktok.privacidade", tipo: "select", rotulo: "Quem pode ver", opcoes: PRIVACIDADE_TIKTOK }),
      campo({ chave: "tiktok.modo", tipo: "select", rotulo: "Como postar", ajuda: "Rascunho manda para a caixa de entrada do app para você finalizar no celular.", opcoes: [["direto", "Publicar direto"], ["rascunho", "Mandar como rascunho"]] }),
      campo({ chave: "tiktok.permitir_comentarios", tipo: "switch", rotulo: "Permitir comentários" }),
      campo({ chave: "tiktok.permitir_duet", tipo: "switch", rotulo: "Permitir dueto" }),
      campo({ chave: "tiktok.permitir_stitch", tipo: "switch", rotulo: "Permitir costura (stitch)" }),
    ];
  }
  const opcoes = [
    campo({ chave: "instagram.pagina_facebook", tipo: "switch", rotulo: "Postar também na Página do Facebook", ajuda: "O mesmo Reel vai para a Página ligada à conta e aparece como Facebook no histórico. A Página aceita Reels de até 90 s." }),
  ];
  if (valor("instagram.envio") === "upload_post") {
    opcoes.push(campo({ chave: "instagram.facebook_pagina_id", tipo: "texto", rotulo: "ID da Página no Upload-Post", ajuda: "Só se o perfil tiver mais de uma Página conectada.", placeholder: "só números" }));
  }
  if (valor("instagram.envio") !== "manual") {
    opcoes.push(
      campo({ chave: "instagram.compartilhar_no_feed", tipo: "switch", rotulo: "Mostrar também no feed" }),
      campo({ chave: "instagram.trial_reels", tipo: "switch", rotulo: "Reel de teste", ajuda: "Aparece primeiro só para quem não segue a conta. Pede uma conta pública com seguidores." }),
      campo({ chave: "instagram.trial_graduacao", tipo: "select", rotulo: "Depois do teste", opcoes: [["SS_PERFORMANCE", "Liberar sozinho se for bem"], ["MANUAL", "Eu libero no app"]] }),
    );
  }
  return opcoes;
}

function botoesRede(rede, r, envio) {
  const b = [];
  if (envio === "manual") {
    if (r.tarefas) b.push(h`<a class="btn" href="#/inicio">${ic("lista")}Ver as tarefas</a>`);
  } else if (envio === "navegador") {
    b.push(h`<button class="btn ${r.pronta ? "" : "primario"}" data-acao="rede-conectar" data-rede="${rede}">${ic("externo")}${r.pronta ? "Abrir o navegador" : "Conectar"}</button>`);
    b.push(h`<button class="btn" data-acao="rede-testar" data-rede="${rede}">${ic("ok")}Testar a sessão</button>`);
    if (r.conta) b.push(h`<button class="btn fantasma" data-acao="rede-desconectar" data-rede="${rede}">Esquecer a conta</button>`);
  } else if (envio === "upload_post") {
    b.push(h`<button class="btn" data-acao="rede-testar" data-rede="${rede}">${ic("ok")}Testar conexão</button>`);
  } else {
    b.push(h`<button class="btn ${r.pronta ? "" : "primario"}" data-acao="rede-conectar" data-rede="${rede}">${ic("link")}${r.pronta ? "Conectar de novo" : "Conectar conta"}</button>`);
    if (r.pronta) b.push(h`<button class="btn" data-acao="rede-testar" data-rede="${rede}">${ic("ok")}Testar</button>`);
    if (r.pronta || r.conta) b.push(h`<button class="btn fantasma" data-acao="rede-desconectar" data-rede="${rede}">Desconectar</button>`);
  }
  return b;
}

function envioHtml(rede, info, envio) {
  const permitidos = (App.meta && App.meta.envios && App.meta.envios[rede]) || ["oficial", "upload_post", "manual"];
  if (permitidos.length === 1) {
    return linhaCampo("Forma de envio", "Esta rede não tem API de postagem aberta.", h`<span class="valor-fixo">${ENVIO_FIXO[rede] || NOME_ENVIO[permitidos[0]](info)}</span>`);
  }
  return campo({ chave: `${rede}.envio`, tipo: "select", rotulo: "Forma de envio", opcoes: permitidos.map((v) => [v, NOME_ENVIO[v](info)]) });
}

function notaEnvio(rede, info, envio) {
  if (envio === "oficial") return guiaOficial(rede);
  if (envio === "manual") return guiaManual(rede);
  if (envio === "navegador") return guiaNavegador(rede);
  return h`<p class="nota">A conta do ${info.rotulo} é conectada no site do Upload-Post, dentro do perfil informado abaixo.</p>`;
}

function cardRedeHtml(rede, r) {
  const info = REDES[rede];
  const envio = valor(`${rede}.envio`);
  const aviso = avisoRede(rede, envio);
  const opcoes = opcoesRede(rede);
  const rodape = [];
  if (r.ativo) rodape.push(`${r.postados_24h} nas últimas 24 h`);
  if (r.ativo && r.proximo) rodape.push(`próximo: ${fmtQuando(r.proximo)}`);
  return h`<div class="card card-rede" style="--cor:${info.cor}">
    <div class="card-topo"><div class="rede-titulo">${logoRede(rede)}<div><b>${info.nome}</b><small>${r.por_semana} posts por semana na agenda</small></div></div>${CONTROLES.switch({ chave: `${rede}.ativo`, rotulo: `Postar no ${info.rotulo}` })}</div>
    ${statusContaHtml(r, envio)}
    ${aviso ? h`<div class="aviso-rede">${ic("alerta")}<span>${aviso}</span></div>` : ""}
    ${formulario([envioHtml(rede, info, envio), ...(envio === "oficial" ? camposOficiais(rede) : [])])}
    ${notaEnvio(rede, info, envio)}
    ${opcoes.length ? h`<details class="guia"><summary>${ic("ajustes")}Opções dos posts</summary><div class="form-interno">${formulario(opcoes)}</div></details>` : ""}
    <div class="rodape-card"><div class="grupo-botoes">${botoesRede(rede, r, envio)}</div><small class="texto-suave">${rodape.join(" · ")}</small></div>
  </div>`;
}

function publicacaoHtml(d) {
  if (d.simulacao) {
    return h`<div class="card destaque-card"><div class="estado-postagens">${ic("info", "grande")}<div><b>Modo simulação ligado</b><p class="nota">Os vídeos são editados e os horários seguem a agenda, mas nada é enviado. Quando as contas estiverem conectadas e testadas, ative a publicação real.</p></div></div><div class="linha-botoes"><button class="btn primario" data-acao="modo-real">${ic("aviao")}Ativar a publicação real</button></div></div>`;
  }
  return h`<div class="card destaque-card real"><div class="estado-postagens">${ic("aviao", "grande")}<div><b>Publicando de verdade</b><p class="nota">Nos horários da agenda, cada rede ativa recebe o próximo corte da fila.</p></div></div><div class="linha-botoes"><button class="btn" data-acao="modo-simulacao">${ic("pausa")}Voltar ao modo simulação</button></div></div>`;
}

function uploadPostHtml() {
  const usando = ORDEM_REDES.filter((r) => valor(`${r}.envio`) === "upload_post");
  return h`<div class="card"><div class="card-topo"><h3>${ic("aviao")}Upload-Post (opcional)</h3>${usando.length ? badge([`Usado por ${usando.map((r) => REDES[r].rotulo).join(", ")}`, "info"], true) : badge(["Não usado", "neutro"], true)}</div>
    <p class="nota">Serviço pago que já passou pela auditoria das redes e publica em público, inclusive no TikTok. Seus vídeos e o acesso às contas passam por ele (${link("https://www.upload-post.com/privacy-policy", "política de privacidade")}). O TikTok não está no plano grátis.</p>
    ${formulario([
      campo({ chave: "upload_post.api_key", tipo: "senha", rotulo: "Chave da API" }),
      campo({ chave: "upload_post.perfil", tipo: "texto", rotulo: "Nome do perfil", placeholder: "o perfil criado no Upload-Post" }),
    ])}
    <details class="guia"><summary>${ic("info")}Como configurar</summary><ol>
      <li>Crie a conta em ${link("https://www.upload-post.com/", "upload-post.com")} e gere a chave da API.</li>
      <li>Em ${link("https://app.upload-post.com/manage-users", "Manage users")}, crie um perfil e conecte nele as redes.</li>
      <li>Preencha a chave e o nome do perfil aqui e salve.</li>
      <li>Em cada rede abaixo, escolha "Upload-Post" na forma de envio, salve e clique em Testar conexão.</li>
    </ol></details>
  </div>`;
}

function manualHtml(d) {
  const usando = ORDEM_REDES.filter((r) => valor(`${r}.envio`) === "manual" && valor(`${r}.ativo`));
  const maximo = (App.meta && App.meta.max_tarefas) || 5;
  return h`<div class="card"><div class="card-topo"><h3>${ic("lista")}Postagem à mão</h3>${usando.length ? badge([`Usada por ${usando.map((r) => REDES[r].rotulo).join(", ")}`, "info"], true) : badge(["Não usada", "neutro"], true)}</div>
    <p class="nota">Para redes sem API aberta (Kwai e Bilibili) ou quando você prefere postar pelo app. No horário da agenda, o AutoCortes cria uma tarefa no Início com o vídeo e os textos no formato da rede, e você marca "Já postei" depois. Cada rede junta no máximo ${maximo} tarefas esperando; com mais que isso, os horários seguintes ficam sem tarefa nova.${d.simulacao ? " As tarefas saem também no modo simulação, porque quem posta é você." : ""}</p>
    ${formulario([
      campo({ chave: "manual.pasta", tipo: "texto", rotulo: "Pasta sincronizada (opcional)", ajuda: "Cada tarefa copia o vídeo e um .txt com os textos para esta pasta, numa subpasta por rede. Use uma pasta do OneDrive ou do Google Drive para pegar no celular. As cópias somem quando a tarefa é feita ou pulada.", placeholder: "ex.: C:\\Users\\voce\\OneDrive\\AutoCortes", larga: true }),
    ])}
  </div>`;
}

function navegadorHtml() {
  const usando = ORDEM_REDES.filter((r) => valor(`${r}.envio`) === "navegador" && valor(`${r}.ativo`));
  const ensaio = valor("navegador.ensaio");
  return h`<div class="card card-navegador"><div class="card-topo"><h3>${ic("externo")}Postagem pelo navegador</h3>${usando.length ? badge([`Usada por ${usando.map((r) => REDES[r].rotulo).join(", ")}`, "aviso"], true) : badge(["Não usada", "neutro"], true)}</div>
    <div class="aviso-rede">${ic("alerta")}<span>Contraria os termos das redes, que só autorizam as APIs oficiais, e pode custar a conta. O AutoCortes não disfarça nada: sem forjar fingerprint, sem resolver captcha e sem proxy. Se a rede pedir verificação, o envio para e avisa você.</span></div>
    <p class="nota">O AutoCortes abre o Chrome com um perfil separado, em <code>dados/chrome</code>, onde você entra na conta uma vez. Depois ele preenche a página de envio da rede como se fosse você. Funciona no YouTube, TikTok, Instagram e Bilibili; o Kwai não tem envio pelo site.</p>
    ${ensaio ? h`<div class="aviso-rede">${ic("info")}<span>Ensaio ligado: os envios preenchem tudo e <b>não publicam</b>, guardando uma imagem da tela em <code>dados/navegador</code>. A postagem fica registrada como recusada, com o motivo.</span></div>` : ""}
    ${formulario([
      campo({ chave: "navegador.ensaio", tipo: "switch", rotulo: "Ensaio (não publica)", ajuda: "Preenche a página e para antes de publicar. Deixe ligado no primeiro teste de cada rede." }),
      campo({ chave: "navegador.visivel", tipo: "switch", rotulo: "Mostrar a janela", ajuda: "Precisa estar ligado para você entrar na conta. Desligado, o navegador roda escondido." }),
      campo({ chave: "navegador.programa", tipo: "texto", rotulo: "Caminho do navegador", ajuda: "Em branco, acha o Chrome e, se não houver, o Edge.", placeholder: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", larga: true }),
      campo({ chave: "navegador.porta", tipo: "numero", min: 1024, max: 65535, rotulo: "Porta do DevTools", ajuda: "Só em 127.0.0.1. Troque se essa porta já estiver em uso." }),
      campo({ chave: "navegador.pausa_min_seg", tipo: "numero", min: 0, max: 30, passo: 0.1, sufixo: "s", rotulo: "Pausa mínima entre passos" }),
      campo({ chave: "navegador.pausa_max_seg", tipo: "numero", min: 0, max: 60, passo: 0.1, sufixo: "s", rotulo: "Pausa máxima entre passos" }),
      campo({ chave: "navegador.tempo_limite_seg", tipo: "numero", min: 30, max: 1800, sufixo: "s", rotulo: "Tempo limite de cada passo" }),
    ])}
  </div>`;
}

async function carregarRedes(token = App.navegacao) {
  const d = await api("/redes");
  if (!App.ativa(token)) return null;
  PgRedes.dados = d;
  App.config = d.config;
  App.meta = d.meta;
  return d;
}

function desenharRedes() {
  const d = PgRedes.dados;
  const el = $("#conteudo");
  if (!d || !el || App.nomePagina !== "redes") return;
  montar(el, h`${publicacaoHtml(d)}
    <div class="cards-redes">${ORDEM_REDES.map((r) => cardRedeHtml(r, d.redes[r]))}</div>
    <div class="grade-2">${uploadPostHtml()}${manualHtml(d)}</div>
    ${navegadorHtml()}
    <p class="nota rodape-card">O caminho seguro é a API oficial de cada rede, ou o Upload-Post. O envio pelo navegador contraria os termos e pode custar a conta: você escolhe rede por rede. Cortes de filmes são conteúdo de terceiros: poste só o que você tem direito de usar, e saiba que as redes tratam trechos com pouca edição como conteúdo não original na monetização e nas recomendações.</p>`);
}

/* ------------------------------------------------------------ login */

function pararLogin() {
  Login.ativo = false;
  clearTimeout(Login.timer);
}

async function acompanharLogin(rede) {
  if (!Login.ativo || Login.rede !== rede) return;
  let s;
  try {
    s = await api(`/redes/${rede}/login`);
  } catch (e) {
    Login.timer = setTimeout(() => acompanharLogin(rede), 3000);
    return;
  }
  if (!Login.ativo) return;
  if (s.estado === "ok") {
    pararLogin();
    fecharModal();
    toast(`${REDES[rede].rotulo} conectado: ${s.mensagem}`, "ok", 7000);
    await App.paginas.redes.recarregar();
    atualizarEstado();
  } else if (s.estado === "erro") {
    pararLogin();
    montar(corpoModal(), h`<div class="login-espera">${ic("erro", "grande")}<p class="erro-texto">${s.mensagem}</p><div class="acoes-modal"><button class="btn primario" data-acao="fechar-modal">Fechar</button></div></div>`);
  } else if (s.estado === "aguardando") {
    Login.timer = setTimeout(() => acompanharLogin(rede), 2000);
  } else {
    pararLogin();
  }
}

async function conectarOAuth(rede) {
  const s = await post(`/redes/${rede}/login`);
  const url = urlSegura(s.url);
  if (url) window.open(url, "_blank", "noopener");
  Login.rede = rede;
  Login.ativo = true;
  abrirModal({
    titulo: `Conectar o ${REDES[rede].rotulo}`,
    largura: 520,
    corpo: h`<div class="login-espera"><span class="giro"></span><p>Autorize o acesso na aba que abriu. Esta janela fecha sozinha quando terminar.</p>${url ? h`<p class="nota">A aba não abriu? ${link(url, "Abrir a página de autorização")}</p>` : ""}<div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button></div></div>`,
    aoFechar: () => {
      if (Login.ativo && Login.rede === rede) post(`/redes/${rede}/cancelar`).catch(() => {});
      pararLogin();
    },
  });
  Login.timer = setTimeout(() => acompanharLogin(rede), 2000);
}

function conectarInstagram() {
  abrirModal({
    titulo: "Conectar o Instagram",
    largura: 640,
    corpo: h`<ol class="passos">
        <li>Abra o ${link("https://developers.facebook.com/tools/explorer/", "Graph API Explorer")}.</li>
        <li>Em "App da Meta", escolha o seu app (App ID ${valor("instagram.app_id") || "?"}).</li>
        <li>Em "Usuário ou Página", escolha "Obter token de acesso do usuário".</li>
        <li>Adicione as permissões: <code class="permissoes">${permissoesIg()}</code>${valor("instagram.pagina_facebook") ? h`<br><small class="texto-suave">${PERMISSAO_PAGINA} é para postar na Página do Facebook; se ela não aparecer, adicione ao app o caso de uso de gerenciar a Página.</small>` : ""}</li>
        <li>Clique em "Generate Access Token", autorize a Página e a conta do Instagram e copie o token.</li>
      </ol>
      <label class="campo"><span>Token de acesso</span><textarea id="ig-token" rows="3" placeholder="Cole o token aqui" spellcheck="false" autocomplete="off"></textarea></label>
      <p class="nota">O token é trocado por um de longa duração e fica salvo só neste computador.</p>
      <div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button><button class="btn primario" data-acao="ig-enviar-token">${ic("link")}Conectar</button></div>`,
  });
}

App.acoes["ig-enviar-token"] = async (el) => {
  const token = $("#ig-token").value.trim();
  if (!token) { toast("Cole o token gerado no Graph API Explorer", "erro"); return; }
  const s = await ocupado(el, () => post("/redes/instagram/login", { token }));
  if (s.estado === "ok") {
    fecharModal();
    toast(`Instagram conectado: ${s.mensagem}`, "ok", 7000);
    await App.paginas.redes.recarregar();
    atualizarEstado();
    return;
  }
  if (s.estado === "escolher") {
    tituloModal("Qual conta vai receber os Reels?");
    montar(corpoModal(), h`<div class="contas-ig">${s.contas.map((c, i) => h`<label><input type="radio" name="ig-conta" value="${c.indice}" ${i === 0 ? raw("checked") : ""}><span><b>@${c.usuario || "?"}</b> <span class="texto-suave">Página ${c.pagina || "?"}</span></span></label>`)}</div><div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Cancelar</button><button class="btn primario" data-acao="ig-escolher">${ic("check")}Usar esta conta</button></div>`);
  }
};

App.acoes["ig-escolher"] = async (el) => {
  const marcado = document.querySelector("input[name=ig-conta]:checked");
  if (!marcado) return;
  const s = await ocupado(el, () => post("/redes/instagram/conta", { indice: Number(marcado.value) }));
  fecharModal();
  toast(`Instagram conectado: ${s.mensagem}`, "ok", 7000);
  await App.paginas.redes.recarregar();
  atualizarEstado();
};

function avisoNavegadorHtml(rede, s) {
  return h`<div class="login-espera"><p>${ic("externo")}Abri a janela do Chrome do AutoCortes no login do ${REDES[rede].rotulo}.</p>
    <ol class="passos">
      <li>Entre na conta nessa janela (e resolva o 2FA, se pedir).</li>
      <li>A sessão fica salva no perfil do AutoCortes, não no seu Chrome normal.</li>
      <li>Volte aqui e clique em <b>Testar a sessão</b>.</li>
    </ol>
    <p class="nota">A janela não abriu? Confira o caminho do Chrome no cartão "Postagem pelo navegador".${s && s.url ? h` Endereço: <code>${s.url}</code>` : ""}</p>
    <div class="acoes-modal"><button class="btn fantasma" data-acao="fechar-modal">Fechar</button><button class="btn primario" data-acao="rede-testar" data-rede="${rede}">${ic("ok")}Testar a sessão</button></div></div>`;
}

App.acoes["rede-conectar"] = async (el) => {
  const rede = el.dataset.rede;
  if (!vazio(App.pendente)) {
    const salvo = await ocupado(el, salvarConfig);
    if (!salvo) return;
  }
  if (valor(`${rede}.envio`) === "navegador") {
    const s = await ocupado(el, () => post(`/redes/${rede}/login`));
    abrirModal({ titulo: `Entrar no ${REDES[rede].rotulo} pelo navegador`, largura: 560, corpo: avisoNavegadorHtml(rede, s) });
    return;
  }
  if (rede === "instagram") { conectarInstagram(); return; }
  await ocupado(el, () => conectarOAuth(rede));
};

App.acoes["rede-testar"] = async (el) => {
  const rede = el.dataset.rede;
  if (!vazio(App.pendente)) {
    const salvo = await ocupado(el, salvarConfig);
    if (!salvo) return;
  }
  if (valor(`${rede}.envio`) === "navegador") toast("Abrindo a página da rede no navegador...", "info");
  const r = await ocupado(el, () => post(`/redes/${rede}/testar`));
  if (modal.aberto) fecharModal();
  toast(`${REDES[rede].rotulo}: ${r.conta}`, "ok", 7000);
  await App.paginas.redes.recarregar();
};

App.acoes["rede-desconectar"] = async (el) => {
  const rede = el.dataset.rede;
  const ok = await confirmar({ titulo: `Desconectar o ${REDES[rede].rotulo}?`, texto: "O login salvo é apagado deste computador. Para voltar a postar, conecte a conta de novo.", botao: "Desconectar", perigo: true });
  if (!ok) return;
  await ocupado(el, () => post(`/redes/${rede}/desconectar`));
  toast(`${REDES[rede].rotulo} desconectado`);
  await App.paginas.redes.recarregar();
  atualizarEstado();
};

App.acoes["modo-real"] = async () => {
  const d = PgRedes.dados;
  const prontas = d ? ORDEM_REDES.filter((r) => d.redes[r].ativo && d.redes[r].pronta && d.redes[r].via !== "manual") : [];
  const texto = prontas.length
    ? `A partir de agora, os cortes são publicados de verdade nos horários da agenda (${prontas.map((r) => REDES[r].rotulo).join(", ")}).`
    : "Nenhuma rede ativa está conectada ainda. Nada será publicado até você conectar uma conta (as redes à mão continuam só com tarefas).";
  const ok = await confirmar({ titulo: "Ativar a publicação real?", texto, botao: "Ativar" });
  if (!ok) return;
  definirValor("geral.simulacao", false);
  if (await salvarConfig()) toast("Publicação real ativada");
};

App.acoes["modo-simulacao"] = async () => {
  definirValor("geral.simulacao", true);
  if (await salvarConfig()) toast("Modo simulação ligado: nada será publicado", "info");
};

App.acoes.copiar = async (el) => {
  try {
    await navigator.clipboard.writeText(el.dataset.texto);
    toast("Copiado");
  } catch (e) {
    toast("Não consegui copiar. Selecione o texto e copie com Ctrl+C.", "erro");
  }
};

/* ------------------------------------------------------------ página */

App.paginas.redes = {
  titulo: "Redes sociais",
  subtitulo: "Contas conectadas, forma de envio e opções de cada rede",
  async render(el, token) {
    const d = await carregarRedes(token);
    if (!d) return;
    desenharRedes();
  },
  aoMudar(caminho) {
    if (/\.(envio|ativo|privacidade|pagina_facebook)$/.test(caminho) || caminho === "navegador.ensaio") desenharRedes();
  },
  async aoSalvar() {
    await this.recarregar();
  },
  async recarregar() {
    const d = await carregarRedes();
    if (d) desenharRedes();
  },
  sair() {
    pararLogin();
  },
};
