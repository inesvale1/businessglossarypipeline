# businessglossarypipeline

_(anteriormente `semanticcatalog`)_

Fase 2 do diagrama de arquitetura: **Pipeline de Glossário de Negócio**.

Lê, para cada esquema:
- `metadata_context_<esquema>.json` — contexto técnico gerado pela Fase 1 (`../technicalcatalogpipeline`);
- `sources_context_<esquema>.json` — contexto de negócio extraído dos fontes/documentos do sistema que gera o esquema (ver "Gerar sources_context a partir do código-fonte" abaixo);
- `docs/organizational_context_value_chain_sefaz_ce.json` — resumo da cadeia de valor organizacional.

Faz o enriquecimento semântico via LLM (lotes + fusão de decisões, ver `core/pipeline.py`) e grava o Contexto Local Canônico:

```
schema/<esquema>/outputs/local_canonical_context_<esquema>.json
```

Esse artefato é a semente do "Repositório do Glossário" (glossário de termos,
relacionamentos, classificação LGPD) do diagrama de arquitetura.

## Uso

```bash
python scripts/generate_canonical_context.py --config config/catalogo.config.json
```

## Gerar sources_context a partir do código-fonte

`scripts/generate_sources_context.py` lê os arquivos `.java` de uma pasta (um
checkout git local do sistema, ou um pacote/subpacote dele), roda o prompt em
`prompts/prompt0_sources_context.txt` contra o LLM configurado em
`config/catalogo.config.json`, e grava (ou funde em)
`schema/<esquema>/inputs/sources_context_<esquema>.json`.

**O prompt é seu para editar.** `prompts/prompt0_sources_context.txt` traz um
ponto de partida (mesmas regras gerais e mesmo campo `sources_context` já em
uso — `enumeracoes`, `regras_negocio`, `dtos_entrada`, `dtos_saida`,
`validadores`, `tabelas_sql`, documentados em
`prompts/sources_context_output_schema.json`) com uma seção marcada
`ATENÇÃO — instruções adicionais deste esquema` para você colar instruções
específicas de um sistema (convenções de nomenclatura, pacotes a ignorar
etc.) sem precisar reescrever o resto.

Para repositórios grandes, rode uma vez por subpacote (do jeito que já era
feito manualmente) e use `--merge` para acumular no mesmo arquivo — os itens
são deduplicados automaticamente (por `classe`/`constante`/`tabela`, conforme
o campo):

```bash
python scripts/generate_sources_context.py --schema receita2 \
  --system-name receita --package-root br.gov.ce.sefaz.receita \
  --source "D:\caminho\para\receita-core\src\main\java\...\enums"

python scripts/generate_sources_context.py --schema receita2 \
  --system-name receita --package-root br.gov.ce.sefaz.receita \
  --source "D:\caminho\para\receita-core\src\main\java\...\validation" --merge
```

### Clonando direto do git.sefaz.ce.gov.br

Se você não quiser clonar manualmente, configure o repositório uma vez em
`config/catalogo.config.json` e o script faz tudo sozinho:

```json
"checkouts_root": ".checkouts",
"sources_repos": {
  "receita2": {
    "git_url": "https://git.sefaz.ce.gov.br/receita/receita-business-development.git",
    "ref": "main",
    "username": "seu.usuario",
    "token_keyring_service": "businessglossary-git",
    "token_keyring_username": "seu.usuario",
    "subpackages": [],
    "strict_include": false
  }
}
```

Depois grave um **Personal Access Token** (não a sua senha de domínio/LDAP) no
keyring do Windows:

```bash
python scripts/store_keyring_secret.py --service businessglossary-git --username seu.usuario
```

#### Não lê tudo às cegas — lista os nomes primeiro, filtra, só baixa conteúdo do que sobrar

Um repositório como esse costuma ter várias subpastas (submódulos Maven,
testes, build, recursos estáticos) que não servem para gerar
`sources_context`. Rodar sem `--source` faz três passos, nessa ordem:

1. **Lista todo o repositório sem baixar nenhum conteúdo** — usa um clone git
   "blobless" (`--filter=blob:none`), que baixa só nomes de arquivo e
   tamanhos, não o conteúdo (`core/git_source.py:sync_blobless_tree` /
   `list_tracked_files_with_size`).
2. **Filtra por nome/caminho** (`core/relevance_filter.py`) — descarta por
   padrão testes (`*/test/*`), build (`*/target/*`, `*/build/*`), recursos
   estáticos, `node_modules`, código gerado, e qualquer extensão fora de
   `.java`/`.sql`. Isso já elimina a maior parte do ruído sem custar nada de
   rede/token. Se quiser um filtro mais agressivo (só arquivos com nomes tipo
   `*enum*`, `*dto*`, `*valid*`, `*repository*` etc.), ligue
   `"strict_include": true` no config — mas isso arrisca descartar regras de
   negócio escondidas em classes com nome não óbvio (ex: um roteador de
   eventos que também é regra de negócio).
3. **Agrupa o que sobrou em lotes por tamanho real** (bytes, vindos do
   passo 1, sem ter baixado conteúdo ainda) e só então busca o conteúdo — um
   arquivo de cada vez — dos arquivos de cada lote, chamando o LLM uma vez por
   lote e fundindo os resultados.

Use `--dry-run` para ver exatamente o que seria incluído/descartado (e por
quê) antes de gastar qualquer chamada de LLM:

```bash
python scripts/generate_sources_context.py --schema receita2 --dry-run
```

E, satisfeito com a lista, rode de verdade:

```bash
python scripts/generate_sources_context.py --schema receita2 --system-name receita --package-root br.gov.ce.sefaz.receita
```

`subpackages` (opcional) restringe a listagem a um prefixo de caminho antes do
filtro — útil para focar em um submódulo Maven específico sem precisar mexer
no filtro de relevância.

**Como a autenticação funciona:** o LDAP do git.sefaz.ce.gov.br é só o backend
de identidade da *interface web/API* do servidor — o cliente `git` nunca fala
LDAP, ele manda HTTPS Basic auth. O token é injetado só na linha de comando
(`git -c http.extraHeader=...`), nunca gravado em `.git/config` nem em log
(`core/git_source.py` redige o token de qualquer mensagem de erro). Testado de
ponta a ponta contra um repositório público (clone real, listagem blobless,
leitura de blob individual, e falha de autenticação limpa sem vazar o token)
— o que não pude testar é a conexão real com `git.sefaz.ce.gov.br` (não
alcançável a partir deste ambiente) nem se o servidor suporta clone parcial
(`--filter=blob:none`, git 2.19+ dos dois lados; GitLab/Gitea/GitHub suportam
há anos). Se o servidor rejeitar `--filter`, ou se a rede tiver proxy com
autenticação NTLM (como já é o caso do `core/llm_client.py` para chamadas ao
Azure OpenAI), avise que adapto `git_source.py` — o modo antigo (`--source`
com clone completo manual) continua funcionando como alternativa.

## Estrutura

- `core/`: batcher, consolidator, inputs_loader, llm_client, prompt_builder, pipeline, sources_context_builder, git_source, relevance_filter.
- `prompts/`: templates dos prompts usados pelo LLM (`prompt1_batch.txt`, `prompt2_merge_decisions.txt`, `prompt0_sources_context.txt`) e schemas de saída.
- `docs/`: contexto organizacional (cadeia de valor).
- `config/catalogo.config.json`: schemas, caminhos, credenciais LLM, repositórios git (reaproveitado por ambos os scripts).
- `scripts/generate_canonical_context.py`: CLI da Fase 2 (metadata_context + sources_context + contexto organizacional → contexto local canônico).
- `scripts/generate_sources_context.py`: CLI para gerar/atualizar `sources_context_<esquema>.json` a partir do código-fonte (pasta local ou listagem+filtro+download automático via `sources_repos`, com `--dry-run`).

## Próximos passos (fora de escopo por enquanto)

- Conexão direta ao Repositório de Metadados/Glossário (PostgreSQL) em vez de arquivos em `schema/`.
