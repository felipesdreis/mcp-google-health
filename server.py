"""
google_health_mcp — MCP server para coletar dados do Google Health API (v4).

Usa a nova Google Health API (substituta do Google Fit / Fitbit Web API),
que requer OAuth2 com escopos googlehealth.*.

Credenciais armazenadas em ~/.config/google-health-mcp/tokens.json
"""

import asyncio
import hmac
import io
import json
import logging
import os
import secrets
import stat
import sys
import time
import webbrowser

# Windows cmd/PowerShell pode usar cp1252 por padrão — força UTF-8 para evitar crash com caracteres não-ASCII
if sys.stdout and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Event, Thread
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
HEALTH_API_BASE = "https://health.googleapis.com/v4/users/me/dataTypes"
HEALTH_PROFILE_URL = "https://health.googleapis.com/v4/users/me/profile"

SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.nutrition.readonly",
    "https://www.googleapis.com/auth/googlehealth.profile.readonly",
]

REDIRECT_URI = "http://127.0.0.1:8765/callback"
TOKEN_FILE = Path.home() / ".config" / "google-health-mcp" / "tokens.json"
CREDS_FILE = Path.home() / ".config" / "google-health-mcp" / "credentials.json"

# Tipos de dados relevantes para atleta de endurance
DATA_TYPES = {
    # Atividade e fitness
    "steps": "steps",
    "distance": "distance",
    "active_energy_burned": "active-energy-burned",
    "active_minutes": "active-minutes",
    "active_zone_minutes": "active-zone-minutes",
    "altitude": "altitude",
    "daily_vo2_max": "daily-vo2-max",
    # Métricas de saúde
    "heart_rate": "heart-rate",
    "daily_resting_heart_rate": "daily-resting-heart-rate",
    "daily_heart_rate_variability": "daily-heart-rate-variability",
    "daily_heart_rate_zones": "daily-heart-rate-zones",
    "daily_oxygen_saturation": "daily-oxygen-saturation",
    "daily_respiratory_rate": "daily-respiratory-rate",
    "weight": "weight",
    "body_fat": "body-fat",
    # Removidos: body-mass-index e blood-pressure não existem na API v4
    # Sono
    "sleep": "sleep",
    # Exercício
    "exercise": "exercise",
    # Nutrição (slug oficial v4: nutrition-log)
    "food_log": "nutrition-log",
}

# ---------------------------------------------------------------------------
# Gerenciamento de tokens
# ---------------------------------------------------------------------------


def _load_credentials() -> Dict[str, str]:
    """Carrega client_id e client_secret do arquivo de credenciais."""
    if not CREDS_FILE.exists():
        raise FileNotFoundError(
            f"Arquivo de credenciais não encontrado: {CREDS_FILE}\n"
            "Crie o arquivo com seu client_id e client_secret do Google Cloud Console.\n"
            "Exemplo:\n"
            '  {"client_id": "xxx.apps.googleusercontent.com", "client_secret": "xxx"}'
        )
    with open(CREDS_FILE) as f:
        data = json.load(f)
    if sys.platform != "win32":
        mode = CREDS_FILE.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            import warnings
            warnings.warn(
                f"{CREDS_FILE} é legível por outros usuários — recomenda-se 'chmod 600'",
                stacklevel=2,
            )
    # Suporta tanto o formato direto quanto o formato exportado do Google Console
    if "installed" in data:
        data = data["installed"]
    elif "web" in data:
        data = data["web"]
    return data


def _load_tokens() -> Optional[Dict[str, Any]]:
    """Carrega tokens salvos em disco."""
    if not TOKEN_FILE.exists():
        return None
    with open(TOKEN_FILE) as f:
        data = json.load(f)
    required_typed: Dict[str, Any] = {"access_token": str, "expires_at": (int, float), "token_type": str}
    for field, ftype in required_typed.items():
        if field not in data:
            raise ValueError(f"tokens.json corrompido: campo '{field}' ausente")
        if not isinstance(data[field], ftype):
            raise ValueError(f"tokens.json corrompido: campo '{field}' com tipo inválido")
    if "refresh_token" not in data:
        raise ValueError("tokens.json corrompido: campo 'refresh_token' ausente")
    return data


def _save_tokens(tokens: Dict[str, Any]) -> None:
    """Salva tokens em disco com escrita atômica e permissão restrita ao dono."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens, indent=2))
    tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    tmp.replace(TOKEN_FILE)


async def _refresh_access_token(tokens: Dict[str, Any]) -> Dict[str, Any]:
    """Usa o refresh_token para obter novo access_token."""
    if not tokens.get("refresh_token"):
        raise RuntimeError(
            "refresh_token ausente. Execute health_authenticate para re-autenticar."
        )
    creds = _load_credentials()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
            },
        )
        resp.raise_for_status()
        new_tokens = resp.json()
        tokens["access_token"] = new_tokens["access_token"]
        tokens["expires_at"] = time.time() + new_tokens.get("expires_in", 3600)
        _save_tokens(tokens)
        return tokens


async def _get_valid_access_token() -> str:
    """Retorna um access_token válido, renovando se necessário."""
    try:
        tokens = _load_tokens()
    except ValueError as e:
        raise RuntimeError(f"tokens.json inválido ({e}). Delete o arquivo e use 'health_authenticate'.") from e
    if not tokens:
        raise RuntimeError(
            "Nenhuma autenticação encontrada. Use a ferramenta 'health_authenticate' primeiro."
        )
    if time.time() >= tokens.get("expires_at", 0) - 60:
        try:
            tokens = await _refresh_access_token(tokens)
        except httpx.HTTPStatusError as e:
            body: Dict[str, Any] = {}
            try:
                body = e.response.json()
            except Exception:
                pass
            if body.get("error") == "invalid_grant":
                TOKEN_FILE.unlink(missing_ok=True)
                raise RuntimeError(
                    "Refresh token inválido ou revogado — tokens.json removido. "
                    "Use 'health_authenticate' para autenticar novamente. "
                    "Se continuar recebendo erro 403 do Google, verifique se o e-mail está "
                    "na lista de usuários de teste no Google Cloud Console (OAuth consent screen)."
                ) from e
            raise RuntimeError(
                f"Falha ao renovar token (HTTP {e.response.status_code}): "
                f"{json.dumps(body, ensure_ascii=False) if body else e.response.text}"
            ) from e
    return tokens["access_token"]


# ---------------------------------------------------------------------------
# Cliente HTTP para a API
# ---------------------------------------------------------------------------


def _handle_http_error(e: httpx.HTTPStatusError) -> str:
    """Formata erros HTTP de forma acionável."""
    code = e.response.status_code
    if code == 401:
        return "Erro 401: Token inválido ou expirado. Use 'health_authenticate' para reautenticar."
    if code == 403:
        return "Erro 403: Sem permissão. Verifique se o escopo necessário foi autorizado."
    if code == 404:
        return "Erro 404: Tipo de dado não encontrado ou sem dados para o período."
    if code == 429:
        return "Erro 429: Limite de requisições atingido. Aguarde antes de tentar novamente."
    try:
        detail = e.response.json()
        return f"Erro {code}: {json.dumps(detail, ensure_ascii=False, indent=2)}"
    except Exception:
        return f"Erro {code}: {e.response.text}"


async def _health_get(endpoint: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    """Executa GET autenticado na Google Health API v4.
    
    endpoint: ex: "steps/dataPoints" ou "heart-rate/dataPoints"
    params: query parameters (filter, pageSize, pageToken, etc)
    """
    token = await _get_valid_access_token()
    url = f"{HEALTH_API_BASE}/{endpoint}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            params=params or {},
        )
        resp.raise_for_status()
        return resp.json()


async def _health_get_all_pages(
    endpoint: str, base_params: Dict[str, Any], max_pages: int = 5
) -> Dict[str, Any]:
    """Busca múltiplas páginas e agrega os resultados em uma lista única."""
    all_points = []
    params = {**base_params}
    for _ in range(max_pages):
        data = await _health_get(endpoint, params)
        # A API retorna o array com o nome do dataType (ex: "dataPoints", "steps", etc)
        # Pegamos a primeira lista encontrada
        for key, val in data.items():
            if isinstance(val, list):
                all_points.extend(val)
                break
        next_token = data.get("nextPageToken")
        if not next_token:
            break
        params = {**base_params, "pageToken": next_token}
    return {"dataPoints": all_points, "total": len(all_points), "truncated": next_token is not None}


def _fmt_date(dt: Optional[str]) -> str:
    """Garante que a data está no formato YYYY-MM-DD."""
    if not dt:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        datetime.strptime(dt[:10], "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Formato de data inválido: '{dt}'. Use YYYY-MM-DD.")
    return dt[:10]


def _parse_period(start_date: str, end_date: str, slug: str) -> Dict[str, str]:
    """Constrói parâmetros de filtro de período para a API v4.

    O prefixo deve ser o nome do data type em snake_case (ex: 'heart_rate').
    Formato: {slug}.interval.start_time >= "YYYY-MM-DDT00:00:00Z"
    """
    s = _fmt_date(start_date)
    e = _fmt_date(end_date)
    t = slug.replace("-", "_")
    return {
        "filter": (
            f'{t}.interval.start_time >= "{s}T00:00:00Z" AND '
            f'{t}.interval.end_time <= "{e}T23:59:59Z"'
        )
    }


def _parse_daily_period(start_date: str, end_date: str, slug: str) -> Dict[str, str]:
    """Constrói filtros para tipos de dado Daily (sem interval — usa date).

    Formato: {slug}.date >= "YYYY-MM-DD"
    """
    s = _fmt_date(start_date)
    e = _fmt_date(end_date)
    t = slug.replace("-", "_")
    return {
        "filter": f'{t}.date >= "{s}" AND {t}.date <= "{e}"'
    }


# ---------------------------------------------------------------------------
# Lifespan e inicialização do servidor MCP
# ---------------------------------------------------------------------------


@asynccontextmanager
async def app_lifespan(app=None):
    """Verifica configuração ao inicializar."""
    try:
        _load_credentials()
    except FileNotFoundError as e:
        logging.warning("Aviso: %s", e)
    yield {}


mcp = FastMCP("google_health_mcp", lifespan=app_lifespan)

# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------


class AuthInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    open_browser: bool = Field(
        default=True,
        description="Se True, abre o browser automaticamente para o fluxo OAuth",
    )


class DateRangeInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    start_date: str = Field(
        ...,
        description="Data início no formato YYYY-MM-DD (ex: '2025-06-01')",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    end_date: str = Field(
        ...,
        description="Data fim no formato YYYY-MM-DD (ex: '2025-06-05')",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )

    @model_validator(mode="after")
    def validate_date_order(self) -> "DateRangeInput":
        if self.start_date > self.end_date:
            raise ValueError("start_date deve ser anterior ou igual a end_date")
        return self


class DataTypeQueryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    start_date: str = Field(
        ..., description="Data início YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    end_date: str = Field(
        ..., description="Data fim YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    limit: Optional[int] = Field(
        default=100,
        description="Número máximo de pontos retornados (1-1000)",
        ge=1,
        le=1000,
    )

    @model_validator(mode="after")
    def validate_date_order(self) -> "DataTypeQueryInput":
        if self.start_date > self.end_date:
            raise ValueError("start_date deve ser anterior ou igual a end_date")
        return self


class SingleDateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    date: str = Field(
        ...,
        description="Data no formato YYYY-MM-DD (ex: '2025-06-05')",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )


# ---------------------------------------------------------------------------
# Ferramentas: Autenticação
# ---------------------------------------------------------------------------


@mcp.tool(
    name="health_authenticate",
    annotations={
        "title": "Autenticar com Google Health API",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def health_authenticate(params: AuthInput) -> str:
    """
    Inicia o fluxo OAuth2 para autenticar com a Google Health API.

    Abre o browser para que o usuário conceda permissão de acesso aos dados
    de saúde (atividade, sono, métricas). Os tokens são salvos localmente em
    ~/.config/google-health-mcp/tokens.json para uso futuro.

    Pré-requisito: arquivo ~/.config/google-health-mcp/credentials.json com
    client_id e client_secret do Google Cloud Console.

    Returns:
        str: Mensagem de sucesso ou erro com instruções.
    """
    try:
        creds = _load_credentials()
    except FileNotFoundError as e:
        return str(e)

    # Captura o código OAuth via servidor local temporário
    auth_code_holder: Dict[str, Optional[str]] = {"code": None, "error": None}
    state = secrets.token_urlsafe(32)

    class OAuthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = parse_qs(parsed.query)
            received_state = qs.get("state", [None])[0]
            if not hmac.compare_digest(received_state or "", state):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"<html><body><h2>Estado invalido - possivel ataque CSRF</h2></body></html>")
                return
            if "code" in qs:
                auth_code_holder["code"] = qs["code"][0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>Autenticado com sucesso!</h2>"
                    b"<p>Pode fechar esta aba.</p></body></html>"
                )
            elif "error" in qs:
                auth_code_holder["error"] = qs["error"][0]
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"<html><body><h2>Erro na autenticacao</h2></body></html>")

        def log_message(self, format, *args):
            logging.debug("OAuthHandler: " + format % args)

    try:
        server = HTTPServer(("127.0.0.1", 8765), OAuthHandler)
    except OSError as e:
        return f"Porta 8765 em uso ({e}). Encerre o processo ocupando a porta e tente novamente."
    server.timeout = 120  # 2 minutos para o usuário autorizar

    # Monta URL de autorização
    auth_params = {
        "client_id": creds["client_id"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(auth_params)}"

    if params.open_browser:
        webbrowser.open(auth_url)
        print(f"\nBrowser aberto para autorização. Se não abrir, acesse:\n{auth_url}\n")
    else:
        return f"Acesse este URL para autorizar:\n\n{auth_url}"

    # Aguarda callback em thread separada
    auth_done = Event()

    def serve():
        try:
            server.handle_request()
        finally:
            server.server_close()
            auth_done.set()

    t = Thread(target=serve, daemon=True)
    t.start()
    auth_done.wait(timeout=120)

    if auth_code_holder["error"]:
        return f"Erro na autorização: {auth_code_holder['error']}"
    if not auth_code_holder["code"]:
        return "Timeout: nenhuma autorização recebida em 2 minutos. Tente novamente."

    # Troca código por tokens
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": auth_code_holder["code"],
                "redirect_uri": REDIRECT_URI,
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
            },
        )
        resp.raise_for_status()
        token_data = resp.json()

    tokens = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token"),
        "token_type": token_data.get("token_type", "Bearer"),
        "expires_at": time.time() + token_data.get("expires_in", 3600),
        "scope": token_data.get("scope", ""),
    }
    _save_tokens(tokens)

    scopes_granted = tokens["scope"].replace(" ", "\n  - ")
    return (
        f"Autenticado com sucesso!\n\n"
        f"Tokens salvos em: {TOKEN_FILE}\n\n"
        f"Escopos concedidos:\n  - {scopes_granted}"
    )


@mcp.tool(
    name="health_auth_status",
    annotations={
        "title": "Verificar status da autenticação",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_auth_status() -> str:
    """
    Verifica se há autenticação válida salva e mostra informações dos tokens.

    Returns:
        str: Status da autenticação e data de expiração do token.
    """
    tokens = _load_tokens()
    if not tokens:
        return "Nenhuma autenticacao encontrada. Use 'health_authenticate' primeiro."

    expires_at = tokens.get("expires_at", 0)
    expires_dt = datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    is_expired = time.time() >= expires_at - 60
    has_refresh = bool(tokens.get("refresh_token"))

    status = "Expirado" if is_expired else "Valido"
    refresh_status = "Disponivel" if has_refresh else "Ausente"

    return (
        f"Status do token: {status}\n"
        f"Expira em: {expires_dt}\n"
        f"Refresh token: {refresh_status}\n\n"
        f"{'O token será renovado automaticamente via refresh_token.' if has_refresh and is_expired else ''}"
    )


# ---------------------------------------------------------------------------
# Ferramentas: Atividade e Fitness
# ---------------------------------------------------------------------------


@mcp.tool(
    name="health_get_steps",
    annotations={
        "title": "Buscar contagem de passos",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_get_steps(params: DataTypeQueryInput) -> str:
    """
    Retorna a contagem de passos no período especificado.

    Args:
        params: start_date, end_date (YYYY-MM-DD), limit (max pontos)

    Returns:
        str: JSON com lista de pontos de passos com timestamps.
    """
    try:
        data = await _health_get(
            f"{DATA_TYPES['steps']}/dataPoints",
            params={**_parse_period(params.start_date, params.end_date, DATA_TYPES['steps']), "pageSize": params.limit},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return _handle_http_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool(
    name="health_get_distance",
    annotations={
        "title": "Buscar distância percorrida",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_get_distance(params: DataTypeQueryInput) -> str:
    """
    Retorna distância percorrida (em metros) no período.

    Args:
        params: start_date, end_date (YYYY-MM-DD), limit

    Returns:
        str: JSON com pontos de distância.
    """
    try:
        data = await _health_get(
            f"{DATA_TYPES['distance']}/dataPoints",
            params={**_parse_period(params.start_date, params.end_date, DATA_TYPES['distance']), "pageSize": params.limit},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return _handle_http_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool(
    name="health_get_active_energy",
    annotations={
        "title": "Buscar calorias ativas queimadas",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_get_active_energy(params: DataTypeQueryInput) -> str:
    """
    Retorna calorias ativas queimadas no período.

    Args:
        params: start_date, end_date (YYYY-MM-DD), limit

    Returns:
        str: JSON com pontos de energia ativa (kcal).
    """
    try:
        data = await _health_get(
            f"{DATA_TYPES['active_energy_burned']}/dataPoints",
            params={**_parse_period(params.start_date, params.end_date, DATA_TYPES['active_energy_burned']), "pageSize": params.limit},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return _handle_http_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool(
    name="health_get_active_minutes",
    annotations={
        "title": "Buscar minutos ativos",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_get_active_minutes(params: DataTypeQueryInput) -> str:
    """
    Retorna minutos ativos e Active Zone Minutes no período.
    Active Zone Minutes contam quando a FC está em zona de queima de gordura ou acima.

    Args:
        params: start_date, end_date (YYYY-MM-DD), limit

    Returns:
        str: JSON com minutos ativos e active zone minutes.
    """
    try:
        active, azm = await asyncio.gather(
            _health_get(f"{DATA_TYPES['active_minutes']}/dataPoints", params={**_parse_period(params.start_date, params.end_date, DATA_TYPES['active_minutes']), "pageSize": params.limit}),
            _health_get(f"{DATA_TYPES['active_zone_minutes']}/dataPoints", params={**_parse_period(params.start_date, params.end_date, DATA_TYPES['active_zone_minutes']), "pageSize": params.limit}),
        )
        return json.dumps({"active_minutes": active, "active_zone_minutes": azm}, ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return _handle_http_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool(
    name="health_get_vo2max",
    annotations={
        "title": "Buscar VO2 Max",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_get_vo2max(params: DateRangeInput) -> str:
    """
    Retorna estimativa diária de VO2 Max (capacidade aeróbica máxima).
    Métrica fundamental para atletas de endurance como duatletetas.

    Args:
        params: start_date, end_date (YYYY-MM-DD)

    Returns:
        str: JSON com valores diários de VO2 Max (ml/kg/min).
    """
    try:
        data = await _health_get(
            f"{DATA_TYPES['daily_vo2_max']}/dataPoints",
            params=_parse_daily_period(params.start_date, params.end_date, DATA_TYPES['daily_vo2_max']),
        )
        return json.dumps(data, ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return _handle_http_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool(
    name="health_get_exercises",
    annotations={
        "title": "Buscar sessões de exercício",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_get_exercises(params: DataTypeQueryInput) -> str:
    """
    Retorna sessões de exercício registradas no período (corridas, ciclismo, etc).

    Args:
        params: start_date, end_date (YYYY-MM-DD), limit

    Returns:
        str: JSON com lista de sessões de exercício com tipo, duração e métricas.
    """
    try:
        data = await _health_get(
            f"{DATA_TYPES['exercise']}/dataPoints",
            params={**_parse_period(params.start_date, params.end_date, DATA_TYPES['exercise']), "pageSize": params.limit},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return _handle_http_error(e)
    except RuntimeError as e:
        return str(e)


# ---------------------------------------------------------------------------
# Ferramentas: Métricas de Saúde
# ---------------------------------------------------------------------------


@mcp.tool(
    name="health_get_heart_rate",
    annotations={
        "title": "Buscar dados de frequência cardíaca",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_get_heart_rate(params: DataTypeQueryInput) -> str:
    """
    Retorna amostras de frequência cardíaca no período.
    A API retorna dados intraday por padrão (resolução de ~5 segundos com Fitbit).

    Args:
        params: start_date, end_date (YYYY-MM-DD), limit (max 1000 pontos)

    Returns:
        str: JSON com amostras de FC (bpm) com timestamps.
    """
    try:
        data = await _health_get(
            f"{DATA_TYPES['heart_rate']}/dataPoints",
            params={**_parse_period(params.start_date, params.end_date, DATA_TYPES['heart_rate']), "pageSize": params.limit},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return _handle_http_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool(
    name="health_get_resting_heart_rate",
    annotations={
        "title": "Buscar frequência cardíaca de repouso",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_get_resting_heart_rate(params: DateRangeInput) -> str:
    """
    Retorna frequência cardíaca de repouso diária — indicador chave de recuperação.
    FC de repouso elevada pode indicar overtraining ou doença.

    Args:
        params: start_date, end_date (YYYY-MM-DD)

    Returns:
        str: JSON com FC de repouso por dia (bpm).
    """
    try:
        data = await _health_get(
            f"{DATA_TYPES['daily_resting_heart_rate']}/dataPoints",
            params=_parse_daily_period(params.start_date, params.end_date, DATA_TYPES['daily_resting_heart_rate']),
        )
        return json.dumps(data, ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return _handle_http_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool(
    name="health_get_hrv",
    annotations={
        "title": "Buscar variabilidade da frequência cardíaca (HRV)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_get_hrv(params: DateRangeInput) -> str:
    """
    Retorna variabilidade da frequência cardíaca (HRV) diária.
    HRV é um dos melhores indicadores de prontidão para treino e recuperação.
    HRV alto = bem recuperado; HRV baixo = fadiga/estresse.

    Args:
        params: start_date, end_date (YYYY-MM-DD)

    Returns:
        str: JSON com HRV diário (ms).
    """
    try:
        data = await _health_get(
            f"{DATA_TYPES['daily_heart_rate_variability']}/dataPoints",
            params=_parse_daily_period(params.start_date, params.end_date, DATA_TYPES['daily_heart_rate_variability']),
        )
        return json.dumps(data, ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return _handle_http_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool(
    name="health_get_heart_rate_zones",
    annotations={
        "title": "Buscar tempo em zonas de frequência cardíaca",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_get_heart_rate_zones(params: DateRangeInput) -> str:
    """
    Retorna tempo diário gasto em cada zona de FC (repouso, queima de gordura,
    cardio, pico). Essencial para análise de carga de treino em duatlo.

    Args:
        params: start_date, end_date (YYYY-MM-DD)

    Returns:
        str: JSON com minutos por zona de FC por dia.
    """
    try:
        data = await _health_get(
            f"{DATA_TYPES['daily_heart_rate_zones']}/dataPoints",
            params=_parse_daily_period(params.start_date, params.end_date, DATA_TYPES['daily_heart_rate_zones']),
        )
        return json.dumps(data, ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return _handle_http_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool(
    name="health_get_spo2",
    annotations={
        "title": "Buscar saturação de oxigênio (SpO2)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_get_spo2(params: DateRangeInput) -> str:
    """
    Retorna saturação de oxigênio no sangue (SpO2) — medida durante o sono.
    Valores <95% podem indicar apneia ou baixa recuperação.

    Args:
        params: start_date, end_date (YYYY-MM-DD)

    Returns:
        str: JSON com SpO2 diário (%).
    """
    try:
        data = await _health_get(
            f"{DATA_TYPES['daily_oxygen_saturation']}/dataPoints",
            params=_parse_daily_period(params.start_date, params.end_date, DATA_TYPES['daily_oxygen_saturation']),
        )
        return json.dumps(data, ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return _handle_http_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool(
    name="health_get_weight",
    annotations={
        "title": "Buscar peso corporal",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_get_weight(params: DataTypeQueryInput) -> str:
    """
    Retorna registros de peso corporal no período.

    Args:
        params: start_date, end_date (YYYY-MM-DD), limit

    Returns:
        str: JSON com registros de peso (kg).
    """
    try:
        data = await _health_get(
            f"{DATA_TYPES['weight']}/dataPoints",
            params={**_parse_period(params.start_date, params.end_date, DATA_TYPES['weight']), "pageSize": params.limit},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return _handle_http_error(e)
    except RuntimeError as e:
        return str(e)


# ---------------------------------------------------------------------------
# Ferramentas: Sono
# ---------------------------------------------------------------------------


@mcp.tool(
    name="health_get_sleep",
    annotations={
        "title": "Buscar dados de sono",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_get_sleep(params: DataTypeQueryInput) -> str:
    """
    Retorna dados de sono no período — estágios (leve, profundo, REM, acordado),
    duração total e eficiência. Sono de qualidade é crítico para recuperação.

    Args:
        params: start_date, end_date (YYYY-MM-DD), limit

    Returns:
        str: JSON com sessões de sono, estágios e duração.
    """
    try:
        data = await _health_get(
            f"{DATA_TYPES['sleep']}/dataPoints",
            params={**_parse_period(params.start_date, params.end_date, DATA_TYPES['sleep']), "pageSize": params.limit},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return _handle_http_error(e)
    except RuntimeError as e:
        return str(e)



# ---------------------------------------------------------------------------
# Ferramentas: Nutrição
# ---------------------------------------------------------------------------


@mcp.tool(
    name="health_get_nutrition_log",
    annotations={
        "title": "Buscar registro alimentar (FatSecret)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_get_nutrition_log(params: DateRangeInput) -> str:
    """
    Retorna registros alimentares sincronizados do FatSecret via Google Health.
    Inclui refeições, alimentos e macronutrientes registrados no período.

    Args:
        params: start_date, end_date (YYYY-MM-DD)

    Returns:
        str: JSON com entradas do diário alimentar.
    """
    try:
        data = await _health_get(
            f"{DATA_TYPES['food_log']}/dataPoints",
            params={**_parse_daily_period(params.start_date, params.end_date, DATA_TYPES['food_log']), "pageSize": 100},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return _handle_http_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool(
    name="health_get_calories_consumed",
    annotations={
        "title": "Buscar calorias consumidas",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_get_calories_consumed(params: DateRangeInput) -> str:
    """
    Retorna log de nutrição do período — refeições e macros registrados.
    As calorias consumidas estão no campo "energy" de cada data point.
    (O data type "calories-consumed" não existe na API v4; usa nutrition-log.)

    Args:
        params: start_date, end_date (YYYY-MM-DD)

    Returns:
        str: JSON com log de nutrição (campo energy contém as kcal).
    """
    try:
        data = await _health_get(
            "nutrition-log/dataPoints",
            params={**_parse_daily_period(params.start_date, params.end_date, "nutrition-log"), "pageSize": 100},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return _handle_http_error(e)
    except RuntimeError as e:
        return str(e)


# ---------------------------------------------------------------------------
# Ferramentas: Fontes de dados
# ---------------------------------------------------------------------------


@mcp.tool(
    name="health_list_steps_data_sources",
    annotations={
        "title": "Listar fontes de dados de passos",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_list_steps_data_sources(params: DataTypeQueryInput) -> str:
    """
    Mostra de quais fontes (Fitbit, etc) vieram os dados de passos no período.
    Cada data point traz proveniência completa: plataforma, fabricante do dispositivo
    e método de gravação.

    Args:
        params: start_date, end_date (YYYY-MM-DD), limit

    Returns:
        str: JSON com resumo das fontes de passos encontradas e contagem por fonte.
    """
    try:
        # Busca de um tipo simples (steps) para mapear as fontes disponíveis
        data = await _health_get(
            f"{DATA_TYPES['steps']}/dataPoints",
            params={
                **_parse_period(params.start_date, params.end_date, DATA_TYPES['steps']),
                "pageSize": params.limit,
            },
        )
        points = data.get("dataPoints", [])
        
        # Agrega fontes únicas
        sources: Dict[str, Any] = {}
        for pt in points:
            src = pt.get("dataSource", {})
            platform = src.get("platform", "UNKNOWN")
            device = src.get("device", {})
            display_name = device.get("displayName", "")
            manufacturer = device.get("manufacturer", "")
            recording_method = src.get("recordingMethod", "")
            key = f"{platform}|{display_name}|{manufacturer}"
            if key not in sources:
                sources[key] = {
                    "platform": platform,
                    "device_name": display_name,
                    "manufacturer": manufacturer,
                    "recording_method": recording_method,
                    "count": 0,
                }
            sources[key]["count"] += 1

        summary = {
            "period": f"{params.start_date} → {params.end_date}",
            "total_data_points": len(points),
            "sources_found": len(sources),
            "sources": list(sources.values()),
        }
        return json.dumps(summary, ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return _handle_http_error(e)
    except RuntimeError as e:
        return str(e)


# ---------------------------------------------------------------------------
# Ferramentas: Perfil
# ---------------------------------------------------------------------------


@mcp.tool(
    name="health_get_profile",
    annotations={
        "title": "Buscar perfil do usuário",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_get_profile() -> str:
    """
    Retorna informações do perfil do usuário (nome, idade, altura, peso padrão etc).

    Returns:
        str: JSON com dados do perfil.
    """
    try:
        token = await _get_valid_access_token()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                HEALTH_PROFILE_URL,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return _handle_http_error(e)
    except RuntimeError as e:
        return str(e)


# ---------------------------------------------------------------------------
# Ferramenta: Resumo do dia (aggregação)
# ---------------------------------------------------------------------------


@mcp.tool(
    name="health_get_daily_summary",
    annotations={
        "title": "Buscar resumo diário completo",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def health_get_daily_summary(params: SingleDateInput) -> str:
    """
    Retorna um resumo consolidado de todas as métricas para uma data específica:
    passos, distância, calorias, FC de repouso, HRV, SpO2, sono, zonas de FC e nutrição.

    Ideal para análise diária de carga de treino, recuperação e balanço calórico para duatlo.

    Args:
        params: date (YYYY-MM-DD) — data desejada

    Returns:
        str: JSON com todas as métricas do dia agregadas.
    """
    date = params.date

    async def safe_get(slug, extra_params=None, use_daily_period=False):
        try:
            base = _parse_daily_period(date, date, slug) if use_daily_period else _parse_period(date, date, slug)
            return await _health_get(f"{slug}/dataPoints", params={**base, **(extra_params or {})})
        except Exception as ex:
            return {"error": type(ex).__name__}

    results = await asyncio.wait_for(
        asyncio.gather(
            safe_get(DATA_TYPES['steps'], {"pageSize": 50}),
            safe_get(DATA_TYPES['distance'], {"pageSize": 50}),
            safe_get(DATA_TYPES['active_energy_burned'], {"pageSize": 50}),
            safe_get(DATA_TYPES['daily_resting_heart_rate'], use_daily_period=True),
            safe_get(DATA_TYPES['daily_heart_rate_variability'], use_daily_period=True),
            safe_get(DATA_TYPES['daily_oxygen_saturation'], use_daily_period=True),
            safe_get(DATA_TYPES['daily_heart_rate_zones'], use_daily_period=True),
            safe_get(DATA_TYPES['sleep'], {"pageSize": 10}),
            safe_get(DATA_TYPES['daily_vo2_max'], use_daily_period=True),
            safe_get(DATA_TYPES['food_log'], {"pageSize": 50}, use_daily_period=True),
        ),
        timeout=60.0,
    )

    summary = {
        "date": date,
        "steps": results[0],
        "distance_m": results[1],
        "active_energy_kcal": results[2],
        "resting_heart_rate_bpm": results[3],
        "hrv_ms": results[4],
        "spo2_pct": results[5],
        "heart_rate_zones": results[6],
        "sleep": results[7],
        "vo2max": results[8],
        "food_log": results[9],
    }
    return json.dumps(summary, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Google Health MCP Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemplos:\n"
            "  python server.py                          # Claude Desktop (stdio)\n"
            "  python server.py --transport sse          # llama-server Web UI\n"
            "  python server.py --transport sse --port 9000\n"
        ),
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Transporte MCP: stdio (Claude Desktop) | sse (llama-server) | streamable-http (padrão: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host para modo HTTP (padrão: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Porta para modo HTTP (padrão: 8000)")
    args = parser.parse_args()

    if args.transport in ("sse", "streamable-http"):
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware

        mcp.settings = mcp.settings.model_copy(update={"host": args.host, "port": args.port})
        app = mcp.sse_app() if args.transport == "sse" else mcp.streamable_http_app()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["Mcp-Session-Id"],
        )
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        mcp.run(transport=args.transport)
