---
inclusion: always
---

# AutoCortes: contexto para as próximas sessões

App local para Windows que corta filmes em vídeos verticais 1080x1920 e posta sozinho no YouTube Shorts, TikTok e Instagram Reels (com a Página do Facebook opcional junto), em loop e com agenda. Kwai e Bilibili são postados à mão, com tarefas no painel. Visão geral, situação e roadmap: `README.md`.

## Convenções
- Código, identificadores, textos da interface e logs em português do Brasil. Responder ao usuário em pt-BR.
- Stack: Python 3.14 na `.venv`, só a biblioteca padrão mais `requests` e `tomlkit` com versões fixas em `requirements.txt`. FFmpeg 9 pela linha de comando, whisper.cpp b5130 (SHA-256 fixado em `ferramentas.py`) e SQLite.
- Evitar PyTorch e dependências pesadas. Preferir binários e modelos leves (ONNX, GGUF) que rodem offline.
- Hardware do usuário: Ryzen 5 5600GT e Radeon RX 5500 XT de 4 GB (sem ROCm; usar Vulkan ou AMF). Cabe o whisper `small` e LLM pequeno.
- Opção nova no config: adicionar em `config.PADRAO` (e na validação) e no `config.example.toml`, mantendo os dois iguais. Se for do visual do corte, também em `modelos_visuais.CHAVES`.
- Nunca apagar nem reeditar cortes do usuário (`dados\cortes`) sem ele pedir. Mudança de visual vale para os próximos cortes.
- Rede nova: `PLATAFORMAS`, `ENVIOS` e `ROTULOS` em `config.py` (fonte única dos nomes), seção no `PADRAO` e no exemplo (começando desligada), `agenda.PRESETS`, `plataformas.criar` e, no painel, `REDES`, `ORDEM_REDES` e `LOGOS` do `nucleo.js`.
- Tela nova no painel: `pg_<nome>.js` com `App.paginas.<nome> = {titulo, subtitulo, render, recarregar}`, `<script>` no `index.html`, linha no `MENU` do `app.js` e prefixo próprio nas funções de topo.

## Como verificar
- `.venv\Scripts\python -m compileall -q autocortes` e `node --check` em cada `autocortes\painel\estatico\*.js`.
- Não há framework de testes. Os testes são scripts temporários na raiz (`_teste_*.py`, `_teste_*.js`), cada um com a própria pasta e um `config.toml` que aponta `pasta_dados` para ela. Apagar tudo no fim.
- No PowerShell, mandar a saída para arquivo (`*> arquivo.log`) e ler com `Get-Content -Encoding UTF8`. O terminal corta saídas longas, e o stderr aparece como NativeCommandError. Nada de `python -c` com aspas aninhadas: trava esperando entrada. Comandos que passam de ~2 min: rodar em segundo plano e ler o log depois.
- Sempre conferir visualmente um quadro do vídeo renderizado (`ffmpeg -ss N -frames:v 1`).
- Painel: subir o servidor de teste (`-m autocortes --config <pasta>\config.toml painel --sem-navegador --porta 8790`) e testar pelo Edge sem janela via DevTools (`--headless=new --remote-debugging-port`), juntando os erros do console e olhando cada captura. Captura de página inteira no headless repete o conteúdo: preferir a captura da janela.
- "Fechar o AutoCortes" pelo painel encerra o servidor de verdade: subir de novo antes do próximo teste.
- Teste com os dados reais: copiar o banco e o cache da análise para a pasta de teste e reescrever `cortes.arquivo` para cópias. Nunca apontar o servidor de teste para `dados\`. Subir o servidor só depois de montar a pasta (os dois ao mesmo tempo deixam o SQLite inconsistente) e usar `--motor-desligado`.
- Arrastar no Edge sem janela: eventos sintéticos não servem para `setPointerCapture`; usar `Input.dispatchMouseEvent` do DevTools.
- Script de teste em Node com saída longa: o `*>` do PowerShell pode perder o fim do log se o comando estourar o tempo. Gravar o log pelo próprio script (`fs.appendFileSync`) e rodar com `Start-Process ... -PassThru` + `WaitForExit`.
- APIs das redes nos testes: servidor HTTP simulado local e trocar as URLs da instância (`graph_url`, `rupload_facebook_url`, `api` do Upload-Post); `instagram.esperar` pode ser trocado para não dormir entre as consultas.
- `config.toml` de teste: escrever um mínimo, à mão. Copiar o `config.example.toml` e acrescentar uma chave (`envio`, por exemplo) gera chave duplicada, o tomlkit recusa e o painel morre na abertura.
- Chrome ou Edge de teste segurando a pasta do perfil faz o `Remove-Item` falhar: matar antes pelos processos com `_teste` na linha de comando (`Get-CimInstance Win32_Process`). Cuidado com o filtro: `*_teste_ui*` também mata o servidor de teste que roda com `--config _teste_ui\...`.
- `Invoke-WebRequest` sem `-UseBasicParsing` abre um aviso interativo e trava o comando.
- Testar `perfis.py` sem tocar na instalação real: sobrescrever `perfis.PASTA`, `perfis.CONFIG_PRINCIPAL` e `perfis.ARQUIVO_INSTALACAO` para dentro da pasta de teste (o `navegador.py` consulta o mesmo módulo, então a troca também vale para ele).
- No Edge sem janela, a viewport sai estreita: usar `Emulation.setDeviceMetricsOverride` antes de capturar, senão o layout em grade não aparece na imagem.

## Lições do painel
- Os scripts das telas dividem o mesmo escopo global: nomes de nível superior não podem repetir entre arquivos (prefixar, como `cfg*` no `pg_config.js`).
- A CSP bloqueia scripts inline e `onclick`: tudo por delegação com `data-acao` no `app.js`. Estilo inline (`style="--cor:..."`) é permitido.
- Classes genéricas colidem: `.mini` era miniatura e badge pequeno ao mesmo tempo. A miniatura agora é `.miniatura`.
- Elemento escondido só com `transform` continua no Tab: usar também `visibility: hidden`.
- A ordem da fila usa `cortes.fila_em` (primeira vez que ficou pronto), não `renderizado_em`, que muda ao editar de novo.
- `ia_textos = {"descartado": true}` marca que o usuário descartou o texto da IA: o motor não reescreve sozinho.
- Camada que cobre a tela toda (como a dos textos no Estúdio) precisa de `pointer-events: none`, com `auto` só nos filhos clicáveis; senão engole o clique de quem está embaixo.
- No Estúdio, a posição de cada camada vem do servidor (`/api/estudio/geometria`, a mesma `edicao.calcular_geometria` do render): não refazer a conta em JS. Prefixo das funções: `est*`.
- Rota com corpo bruto (envio de arquivo): ler o corpo inteiro antes de responder com erro, senão o Windows derruba a conexão (WinError 10053) e o navegador não vê a mensagem.
- O clique global do `app.js` faz `preventDefault` em tudo com `data-acao`: link de download (`<a download>`) não pode ter `data-acao`.
- `abrirModal` foca o primeiro campo do corpo: em tela estreita isso rola a janela. Marcar com `autofocus` o que deve receber o foco.

## Perfis de nicho
- Perfil = pasta com `config.toml`. Como `Config.raiz` é a pasta do config e tudo pende de `pasta_dados`, `pasta_dados = "dados"` e `pasta_filmes = "filmes"` relativos já separam banco, tokens, perfil do Chrome, roteiros, modelos visuais, cortes e log. O principal é o `config.toml` da instalação; os outros ficam em `perfis/<slug>/`. **Um processo por perfil**, não multi-tenancy: a alternativa (coluna `perfil_id` em tudo) exigiria trocar todos os locks e caches de módulo (`TRAVA_EDICAO`, `TRAVAS_REDE`, cache de moldura, `navegador.TRAVA`) e um seletor em cada rota.
- `perfis.py` cuida de listar/criar/ajustar/excluir/abrir/fechar. O que é da instalação inteira fica em `perfis.toml` (`[perfis].max_simultaneos`, 0 = sem limite); a lista de perfis é derivada das pastas, nunca duplicada num registro. `ferramentas/` e `modelos/` continuam compartilhados (`RAIZ`), o que é desejado.
- Perfil novo nasce **em simulação e com todas as redes desligadas** (as contas ainda não existem), com portas de painel e navegador livres. `criar` usa `criar_config_se_faltar` + `config.salvar`, então o config sai comentado como o exemplo.
- **Parar outro perfil não pode ser por HTTP**: o token do painel é aleatório por processo. O jeito é o arquivo `encerrar.pedido` na `pasta_dados`, que o laço do `iniciar_painel` confere a cada segundo (encerramento cooperativo normal: fecha servidor, cancela logins/gravações, para o motor, libera a trava). Ele é apagado na subida e no fim.
- `/saude` (sem token) leva o nome do perfil e `perfis.identidade(pasta_dados)` (sha256 curto, nunca o caminho): é assim que se sabe **qual** perfil está numa porta, em vez de só "alguém respondeu".
- `urlopen` numa porta fechada leva ~2 s no Windows: conferir antes com `socket.connect_ex` (`_escutando`) e consultar os perfis em paralelo (`situacoes`, ThreadPoolExecutor). Sem isso a tela levava 6 s com 3 perfis.
- Só o principal chama `autoiniciar_pendentes` (se qualquer perfil chamasse, viraria um laço de processos).
- `perfis/` e `perfis.toml` estão no `.gitignore`: são dados e contas do usuário.

## Envio pelo navegador (DevTools)
- `navegador.py` é a camada base: cliente WebSocket próprio (a biblioteca padrão não tem), CDP, abertura do Chrome com perfil em `dados/chrome` e os ajudantes. `plataformas/navegador.py` tem o `ViaNavegador`, que executa o roteiro da rede. Kwai fora: não existe página de envio.
- O ajudante `__ac` é injetado na página e faz busca que **entra no shadow DOM**: sem isso o YouTube Studio (Polymer) é inalcançável. Ele se perde a cada navegação, então todo ajudante chama `_garantir_ajudante`.
- Arquivo: `DOM.setFileInputFiles` pelo `objectId` (`Runtime.evaluate` sem `returnByValue` + `DOM.requestNode`), que alcança campo escondido e no shadow DOM. **Antes é obrigatório chamar `DOM.getDocument`**, senão o `requestNode` volta vazio. O Chrome aceita caminho inexistente calado: conferir antes.
- Busca por texto (`__ac.porTexto`): junta `innerText` + `aria-label` + `title` **num só texto** e olha **qualquer tag** (`*`), com nota por casamento exato, clicável e ser folha. Restringir a `button, span, a` ou usar `||` entre os atributos não acha o botão de publicar do TikTok, que é uma `div` com "Post" visível e "Publicar agora" no `aria-label`.
- Uma porta de DevTools só pode ser de um perfil. `abrir()` só reaproveita a janela se `nosso()` confirmar, senão para com erro de `bloqueio` (senão o perfil B postaria com a conta do perfil A, calado). A prova é o arquivo `.autocortes-janela.json` que eu gravo no perfil do Chrome com a porta e o **alvo** do browser (`webSocketDebuggerUrl`, único por processo). Medido em set/2026: o Chrome 154 **não grava mais** o `DevToolsActivePort`, e `Browser.getBrowserCommandLine` só responde com `--enable-automation` — que entrega a automação para as redes, então está fora. Sem anotação (instalação antiga), a janela é adotada só se nenhum outro perfil usar aquela porta.
- Botão desabilitado (`disabled`, `aria-disabled`, classe com `disabled`) **não conta como achado**: assim o passo de publicar espera ele liberar (até 900 s) em vez de clicar num botão morto ou desistir em 30 s.
- Na repetição do roteiro, `__ac.travarArquivo()` sobrescreve `HTMLInputElement.prototype.click` (para `type=file`) e `showOpenFilePicker`: sem isso o clique em "Selecionar vídeo" abre a janela do Windows e trava tudo. O vídeo entra sempre pelo CDP.
- Sessão: nada de procurar texto de login. Medido em set/2026, deslogado: o YouTube vai para o `accounts.google.com`, o Instagram mostra `input[type=password]`, e o TikTok e o Bilibili ficam na mesma URL com a página vazia. Por isso `esperar_sessao` espera um **sinal positivo** por rede (`SESSAO`), e o nome da conta do Instagram exige um `img` dentro do link, senão pega "popular" do rodapé.
- Iframes: só reclamar de quadro que pareça uploader; Bilibili e YouTube têm quadros de terceiros inofensivos.
- Ensaio (`[navegador].ensaio`): preenche tudo, não publica e devolve erro do tipo "corte", então a postagem fica como recusada com o motivo. Toda falha guarda imagem em `dados/navegador`.
- Testar: páginas falsas locais que imitam cada rede (com uma em shadow DOM) mais a checagem de sessão contra os sites reais, deslogado. Os seletores reais só dão para validar com conta logada.
- Sem API não há id nem link: `Resultado` leva `navegador-<hex>` e o link só quando a página mostra.

## Gravador e linguagem de roteiro
- O roteiro **não fica no código**: é gravado enquanto o usuário posta à mão (`gravador.py` + `Gravacao` em `plataformas/navegador.py`) e vive em `dados/roteiros/<rede>.txt`, **fonte única**. Nada de guardar JSON ao lado do texto: duas representações divergem.
- `roteiro.py` é a linguagem: um comando por linha, `abrir`, `video`, `clicar`, `publicar`, `escrever <papel|"texto"> em <alvo>`, `tags em`, `esperar <n>` ou `esperar <alvo>`, `tecla`, `rolar`, `conferir`, prefixo `opcional`, `#` comentário. Alternativas com ` ou `; alvo entre aspas é busca por texto na tela, sem aspas é seletor CSS. `AJUDA` alimenta a lista do painel.
- `publicar` **é** o clique que publica (o ensaio para antes dele), não um marcador separado. Comentário só no começo da linha, porque `clicar #post` usa `#` como seletor.
- `validar` recusa salvar roteiro sem `video`, sem `publicar` ou sem os papéis obrigatórios da rede (`PAPEIS_DA_REDE`), e devolve os erros por linha ("linha 3: ..."). `gravador.salvar` devolve `ok: False` sem gravar.
- A gravação **varre os campos de texto a cada 0,9 s** em vez de confiar em eventos: o editor do TikTok não dispara nada escutável. Os listeners continuam, só para saber a ordem.
- Papel do campo: marcas toleranteseliminação — `@@legendas@@` casa com `@@LEGENDA@@`, e quando falta 1 papel e há 1 campo sem dono o papel é deduzido. Por isso TikTok e Instagram não precisam de marca.
- O painel mostra o log de ações ao vivo, com desfazer, recomeçar e diagnóstico da página; avisar o usuário para **esperar o envio do vídeo terminar antes de clicar em publicar**, senão o roteiro sai com o clique cedo demais.
- Extensão do Chrome foi descartada: `input.files` é somente leitura, rodaria no perfil pessoal e seria um segundo código para manter. Navegador dentro do painel é impossível (`X-Frame-Options: DENY` no YouTube Studio e Instagram, `SAMEORIGIN` no TikTok).

## Postagem à mão e Página do Facebook
- Tarefa à mão = postagem com status `aguardando` e `via = 'manual'`. Não usar a palavra "manual" em status: `postagens.manual = 1` já quer dizer "Postar agora" (fora da agenda), que o painel mostra como "fora da agenda".
- `aguardando` entra em `planejador.status_concluidos` (o corte sai da fila da rede) e em `status_ocupam_horario` (atende o horário, conta no intervalo mínimo e no limite do dia), mas não nos posts de hoje, na aba Publicados nem em `atualizar_concluidos`: o corte só conclui depois que o usuário marca "Já postei" ou pula. Limite de 5 tarefas esperando por rede (`manual.MAX_TAREFAS_POR_REDE`).
- As tarefas saem também no modo simulação (quem posta é o usuário). A pasta sincronizada (`[manual].pasta`) recebe `<Rede>/<nome>-tarefaN.mp4` e `.txt`; só esses nomes são apagados.
- O Reel da Página é a linha `plataforma = 'facebook'`, que não está em `PLATAFORMAS` (sem agenda nem seção no config): sai logo depois do Instagram (`Publicador._postar_facebook`), com trava própria em `TRAVAS_REDE`. O `video_id` e o link são gravados assim que o Facebook aceita (`ao_avancar`); `recuperar_estados_pendentes` transforma `enviando` com link em `publicado`, para não postar duas vezes. Erro temporário repete até 3 vezes (`_repetir_facebook`); `corte` e `bloqueio` viram `pulado`, sem pausar o Instagram.
- YouTube: `planejador.fila` tira os cortes acima de `max_segundos`, e `Publicador._postar` confere o arquivo com o ffprobe (`youtube.motivo_nao_short`) antes de qualquer envio, inclusive a tarefa à mão. `sem_cortes_possiveis` evita que o produtor edite sem parar quando nada cabe no limite.
- O render limita a taxa de quadros entre 24 e 60 (Reels da Página): o episódio do usuário, em 23,976, sai em 24.

## Lições de mídia
- O filtro `whisper` do FFmpeg foi lento e errou os tempos: usar o `whisper-cli` e encaixar as palavras nos trechos de voz do VAD.
- Caminhos dentro de filtros do FFmpeg têm dois níveis de escape: usar `midia.caminho_filtro`.
- Por padrão, textos na área segura: y de 288 a 1248 (em 1920) e no máximo 696 px de largura centrada (`edicao.SEGURO_*` e `LARGURA_*`). Com `legenda_lugar = "abaixo"` a legenda passa de 1248 a pedido do usuário; o Estúdio mostra as faixas que os apps cobrem. O libass não desenha emoji colorido.
- Codificador de GPU que falha volta sozinho para o `libx264` (`edicao._CODECS_FALHOS`).
- Moldura: PNG 9:16 com a área do vídeo transparente. `edicao.info_moldura` acha a janela pela transparência a partir do centro (cache LRU com trava). Sem janela, o corte sai sem moldura. O filme passa `SANGRIA_MOLDURA` (4 px) por baixo da borda. Ordem das camadas: fundo, filme, barra, moldura e textos (ASS). Com moldura, a barra corre numa faixa recortada da base (`split` + `crop` + `overlay`), para nunca aparecer fora da janela. O `overlay` de imagem única repete o quadro sozinho (`eof_action=repeat`). O `drawbox` não anima com o tempo (o `t` dele é a espessura).
- A edição usa um retrato do config (`cfg.copia()`), e o modelo do corte é anotado no começo: o painel pode salvar no meio sem misturar dois visuais.
- Logins das redes cifrados com DPAPI (`segredos.py`, só ctypes). Arquivo em texto puro antigo é lido e cifrado na hora.
- O `control_pwsh_process stop` às vezes deixa o Python do servidor de teste vivo por um instante: conferir e encerrar pelo processo antes de remontar a pasta.
- Sem moldura e com as opções novas no padrão, o render sai idêntico byte a byte ao editor anterior (é o Model One do usuário). Ao mexer na geometria, comparar com uma cópia do `edicao.py` antigo.
- Modelos visuais: o em uso é o `[edicao]` do `config.toml` (`edicao.modelo`); os outros ficam em `dados/modelos_visuais.json`, que também grava o `"ativo"` (para `conciliar` saber, na abertura, se o nome foi trocado à mão). As rotas dos modelos seguram `painel.trava_config` (RLock) do começo ao fim. O `/config` recusa trocar `edicao.modelo` e responde 409 quando o `modelo_esperado` da janela não é o em uso. Do usuário: Model One (sem moldura, zoom 1,25; partes 1 a 4 de The Great) e Model Two (moldura CineLink, zoom 1,7, legenda abaixo; em uso).
- O `info.json` guarda as faixas simplificadas (`{"idioma": "por"}`), não o `tags.language` do ffprobe. Arquivo "Dual" (ou "Dublado" no nome) com legenda no idioma do áudio é dublagem: transcrever com o Whisper (`analise.audio_dublado`), porque a legenda é a tradução do original. Análise antiga sem a lista `audios` é completada por `analise.completar_info` (só o ffprobe). O WAV só é extraído quando precisa (VAD ou Whisper).
- O `config.salvar` (tomlkit) pode reescrever texto com `\n` como string de várias linhas (`"""`). O valor não muda.

## Limites das redes (setembro de 2026)
- YouTube: projeto sem auditoria sobe os vídeos privados. `videos.insert` tem cota própria de 100 envios por dia. `publishAt` só funciona em vídeo privado. Playlists exigem escopo `youtube` ou `youtube.force-ssl`. Short = vertical ou quadrado de até 3 min. Desde 24/09/2026, Short de 1 a 3 min com reivindicação do Content ID não é mais bloqueado na hora. Em 01/02/2027 o Programa de Parcerias muda para canais novos: 1.000 inscritos e 8.000 h em 365 dias ou 20 mi de views de Shorts em 90 dias, e a receita de Shorts pede 10 mi de views em 90 dias todo mês.
- TikTok: sem auditoria, só SELF_ONLY com a conta privada, e a auditoria de ferramenta de uso próprio é vetada. O Direct Post não tem agendamento. Postagem pública só por um serviço já auditado: o Upload-Post está integrado como opção por rede (`envio = "upload_post"`), desligado por padrão, e o TikTok não está no plano grátis dele. O Zernio (antigo Late) cobra por conta e sai mais barato com poucas contas; não está integrado e não foi confirmado se posta público no TikTok.
- Instagram: publica público pela API, com no máximo 5 hashtags. A API não agenda (o contêiner expira em 24 h). Reel de teste via `trial_params`.
- Página do Facebook: `POST /{page-id}/video_reels` (`upload_phase=start`), envio em `rupload.facebook.com/video-upload/{versao}/{video_id}` com `Authorization: OAuth`, `offset` e `file_size`, e `finish` com `video_state=PUBLISHED`. Status em `fields=status`; `permalink_url` vem sem o domínio. Reels de 3 a 90 s, 24 a 60 fps, 30 por Página em 24 h. Precisa de `pages_manage_posts`; app em desenvolvimento publica só para quem tem função nele. No Upload-Post: `platform[]=facebook`, `facebook_media_type=REELS`, `facebook_page_id` (com mais de uma Página, a resposta traz `available_pages`).
- Kwai: sem API de postagem para criadores, sem envio pelo site e sem serviço terceiro. Bilibili: a Open Platform de envio é só para empresas chinesas; ferramentas com cookies (biliup) violam a cláusula 4.3.15 dos termos. Os dois são postados à mão. Limites do Bilibili usados (título 80, descrição 250, 10 tags de 20) não têm fonte oficial.
- Originalidade: desde 13/03/2026 a Meta trata como não original juntar clipes e mudanças de baixo valor (bordas, legendas inseridas, velocidade), o que inclui a moldura e as legendas do app. TikTok só paga vídeo original com 1 min ou mais; Kwai e Bilibili só obra original (转载 fica fora).
- Métricas: YouTube pelo `videos.list` (escopo `youtube.readonly`) e Instagram pelos insights (`instagram_manage_insights`) já estão no código; contas conectadas antes precisam reconectar. TikTok (`video.list`) e o formato das métricas do Upload-Post ainda não.
- IA: qualquer API compatível com a da OpenAI; o padrão é o Ollama local. Com serviço online, a fala do trecho sai do PC: avisar.
- TMDB: os termos proíbem uso com IA/ML e exigem contrato para uso comercial. Preferir o Wikidata.

## Limites de conteúdo
- Sempre avisar sobre direitos autorais e originalidade.
- Não implementar técnicas para burlar Content ID nem detecção de conteúdo repetido.
- Postagem por automação do navegador: **liberada pelo dono em 29/09/2026**, ciente de que viola os termos das redes e pode custar as contas. Só nas contas dele, com o navegador dele. Regras que ficam: perfil do Chrome dedicado em `dados/chrome/`, DevTools só em 127.0.0.1, ritmo humano com pausa aleatória, limite diário, parada automática em página inesperada ou restrição, e nada de forjar fingerprint, resolver captcha, usar API privada ou proxy. Se a rede bloquear, para e avisa; não insiste.
