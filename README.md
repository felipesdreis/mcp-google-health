# google-health-mcp

Servidor MCP local para coletar seus dados da **Google Health API** (v4) — a nova API que sucede o Google Fit e o Fitbit Web API.

## Dados disponíveis

| Ferramenta MCP | Dado |
|---|---|
| `health_get_steps` | Contagem de passos |
| `health_get_distance` | Distância percorrida (m) |
| `health_get_active_energy` | Calorias ativas queimadas |
| `health_get_active_minutes` | Minutos ativos + Active Zone Minutes |
| `health_get_vo2max` | VO2 Max estimado diário |
| `health_get_exercises` | Sessões de exercício (corrida, bike, etc) |
| `health_get_heart_rate` | FC intraday (~5s de resolução com Fitbit) |
| `health_get_resting_heart_rate` | FC de repouso diária |
| `health_get_hrv` | HRV diário (ms) |
| `health_get_heart_rate_zones` | Tempo em zonas de FC por dia |
| `health_get_spo2` | Saturação de oxigênio (SpO2) |
| `health_get_weight` | Peso corporal |
| `health_get_sleep` | Sono (estágios, duração, eficiência) |
| `health_get_profile` | Perfil do usuário |
| `health_get_daily_summary` | **Resumo completo do dia** (todas as métricas) |
| `health_list_steps_data_sources` | Fontes dos dados de passos (dispositivo, plataforma) |
| `health_authenticate` | Autenticar via OAuth2 |
| `health_auth_status` | Verificar status da autenticação |

---

## Pré-requisitos

- Python 3.10+
- Conta Google com **Fitbit conectado** (a Google Health API puxa dados do Fitbit)
- Claude Desktop

> **Nota:** A Google Health API é a sucessora do Google Fit (deprecated em 2026). Ela acessa dados do Fitbit vinculado à sua conta Google.

---

## Instalação

### 1. Instalar dependências

```bash
pip install -r requirements.txt
```

Ou manualmente:

```bash
pip install "mcp[cli]" httpx
```

### 2. Criar projeto no Google Cloud Console

1. Acesse [console.cloud.google.com](https://console.cloud.google.com)
2. Crie um projeto novo (ex: `google-health-mcp`)
3. No menu lateral → **APIs e Serviços** → **Biblioteca**
4. Pesquise **"Google Health API"** e ative-a
5. Vá em **APIs e Serviços** → **Credenciais**
6. Clique em **+ Criar Credenciais** → **ID do cliente OAuth**
7. Tipo de aplicativo: **Aplicativo para computador** (Desktop app)
8. Baixe o JSON de credenciais

### 3. Configurar credenciais

```bash
mkdir -p ~/.config/google-health-mcp
```

Copie o JSON baixado para:
```
~/.config/google-health-mcp/credentials.json
```

O arquivo deve ter este formato (gerado automaticamente pelo Google):
```json
{
  "installed": {
    "client_id": "xxx.apps.googleusercontent.com",
    "client_secret": "GOCSPX-xxx",
    "redirect_uris": ["http://localhost"],
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token"
  }
}
```

> **Segurança:** após a autenticação, `tokens.json` é salvo com permissão `600` (somente leitura/escrita pelo dono). Trate `~/.config/google-health-mcp/` como um diretório sensível.

### 4. Configurar usuário de teste (OAuth em modo de desenvolvimento)

No Google Cloud Console:
1. **APIs e Serviços** → **Tela de permissão OAuth**
2. Em **Usuários de teste**, adicione seu email Google

### 5. Configurar no Claude Desktop

**Encontrando o arquivo de configuração:**

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** abra o Claude Desktop, vá em **Extensões → (Desenvolvedor) Servidores MCP locais** e clique em **Editar Config** — o arquivo `claude_desktop_config.json` abrirá diretamente no editor.

Edite o arquivo:

```json
{
  "mcpServers": {
    "google-health": {
      "command": "python",
      "args": ["/caminho/para/google-health-mcp/server.py"]
    }
  }
}
```

Substitua `/caminho/para/google-health-mcp/` pelo caminho real onde colocou o `server.py`.

### 6. Usar com llama-server / LlamaFile Web UI

O servidor suporta três modos de transporte, selecionáveis via linha de comando:

| Modo | Comando | Para usar com |
|---|---|---|
| `stdio` (padrão) | `python server.py` | Claude Desktop |
| `sse` | `python server.py --transport sse` | llama-server, LlamaFile Web UI |
| `streamable-http` | `python server.py --transport streamable-http` | Clientes MCP modernos (spec 2025-03) |

**Iniciar em modo SSE para llama-server:**

```bash
python server.py --transport sse --port 8000
```

O servidor sobe em `http://127.0.0.1:8000`. O endpoint SSE fica em:

```
http://127.0.0.1:8000/sse
```

Configure esse URL no campo de MCP server do llama-server ou LlamaFile Web UI.

**Opções disponíveis:**

```
python server.py --help

  --transport {stdio,sse,streamable-http}
  --host HOST      Host HTTP (padrão: 127.0.0.1)
  --port PORT      Porta HTTP (padrão: 8000)
```

> stdio e HTTP são mutuamente exclusivos por processo. Para usar Claude Desktop e llama-server ao mesmo tempo, rode duas instâncias — uma sem flags (stdio) e outra com `--transport sse --port 8000`.

---

## Primeiro uso: Autenticação

No Claude Desktop, após reabrir:

```
Use a ferramenta health_authenticate para autenticar com o Google Health
```

O Claude vai abrir o browser automaticamente. Faça login com sua conta Google e autorize os escopos. Os tokens são salvos em `~/.config/google-health-mcp/tokens.json`.

---

## Parâmetros comuns

Todas as ferramentas que buscam dados históricos aceitam:

| Parâmetro | Formato | Exemplo |
|---|---|---|
| `start_date` | `YYYY-MM-DD` | `2025-06-01` |
| `end_date` | `YYYY-MM-DD` | `2025-06-07` |
| `date` | `YYYY-MM-DD` | `2025-06-05` |
| `limit` | inteiro 1–1000 | `100` (padrão) |

`start_date` deve ser anterior ou igual a `end_date`.

---

## Exemplos de uso com Claude

```
Quais foram meus dados de treino nesta semana? (use health_get_daily_summary para cada dia)
```

```
Mostra meu HRV dos últimos 7 dias e analisa minha recuperação
```

```
Qual foi minha FC de repouso esta semana? Compara com semanas anteriores
```

```
Busca meus dados de sono de junho e identifica padrões
```

```
Resume meu dia de ontem com todas as métricas de saúde
```

---

## Estrutura de arquivos

```
google-health-mcp/
├── server.py           # Servidor MCP principal
├── requirements.txt    # Dependências com versões fixadas
├── README.md           # Este arquivo
└── ~/.config/google-health-mcp/   # fora do repositório
    ├── credentials.json            # Suas credenciais OAuth (você cria)
    └── tokens.json                 # Tokens gerados automaticamente (600)
```

---

## Solução de problemas

**"Arquivo de credenciais não encontrado"**
→ Crie o arquivo `~/.config/google-health-mcp/credentials.json` conforme o passo 3.

**"Token inválido ou expirado"**
→ Execute `health_authenticate` novamente.

**"refresh_token ausente"**
→ O token foi salvo sem refresh token (fluxo OAuth incompleto). Execute `health_authenticate` novamente para regenerar os tokens com o escopo completo.

**"Porta 8765 em uso"**
→ Outro processo está ocupando a porta durante a autenticação. Encerre o processo e tente novamente.

**"Erro 403"**
→ Verifique se adicionou seu email como usuário de teste no Google Cloud Console.

**"Erro 404 - sem dados"**
→ Verifique se o Fitbit está conectado à sua conta Google e sincronizado.

**Dados de Garmin**
→ A Google Health API acessa dados do Fitbit/Google Fit. Para dados do Garmin, use a [Garmin Health API](https://developer.garmin.com/gc-developer-program/overview/) ou exporte via GPX/FIT e use o Strava.
