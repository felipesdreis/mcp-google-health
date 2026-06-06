"""
Testes unitários para server.py — google-health-mcp

Grupos:
  1. _fmt_date                   — helper de formatação de data
  2. _parse_period                — filtros interval (regressão bug data_type)
  3. _parse_daily_period          — filtros daily   (regressão bug data_type)
  4. _handle_http_error           — mensagens de erro por status HTTP
  5. Modelos Pydantic             — validação de entradas
  6. _load_tokens                 — leitura de tokens (mock de disco)
  7. _save_tokens                 — escrita de tokens (tmp_path)
  8. health_get_steps             — ferramenta MCP interval
  9. health_get_resting_heart_rate — ferramenta MCP daily
 10. health_get_daily_summary     — ferramenta MCP com 10 fetches paralelos
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent))

import server  # noqa: E402 — necessário após inserir o path
from server import (
    DATA_TYPES,
    AuthInput,
    DataTypeQueryInput,
    DateRangeInput,
    SingleDateInput,
    _fmt_date,
    _handle_http_error,
    _load_tokens,
    _parse_daily_period,
    _parse_period,
    _save_tokens,
    health_get_daily_summary,
    health_get_resting_heart_rate,
    health_get_steps,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_http_error(status_code: int, body: str = "error") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://health.googleapis.com/v4/test")
    response = httpx.Response(status_code, text=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


INTERVAL_SLUGS = [
    "steps",
    "distance",
    "active-energy-burned",
    "active-minutes",
    "active-zone-minutes",
    "heart-rate",
    "weight",
    "body-fat",
    "sleep",
    "exercise",
    "nutrition-log",
]

DAILY_SLUGS = [
    "daily-vo2-max",
    "daily-resting-heart-rate",
    "daily-heart-rate-variability",
    "daily-heart-rate-zones",
    "daily-oxygen-saturation",
    "daily-respiratory-rate",
]


# ---------------------------------------------------------------------------
# 1. _fmt_date
# ---------------------------------------------------------------------------

class TestFmtDate:
    def test_valid_date_passthrough(self):
        assert _fmt_date("2026-06-01") == "2026-06-01"

    def test_none_returns_today(self):
        from datetime import datetime, timezone
        result = _fmt_date(None)
        assert result == datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def test_empty_string_returns_today(self):
        from datetime import datetime, timezone
        result = _fmt_date("")
        assert result == datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def test_invalid_format_raises_value_error(self):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            _fmt_date("26-06-01")

    def test_truncates_to_date_only(self):
        assert _fmt_date("2026-06-01T12:30:00Z") == "2026-06-01"


# ---------------------------------------------------------------------------
# 2. _parse_period — REGRESSÃO: nunca deve conter "data_type"
# ---------------------------------------------------------------------------

class TestParsePeriod:
    def test_steps_exact_filter(self):
        result = _parse_period("2026-06-01", "2026-06-01", "steps")
        assert result == {
            "filter": (
                'steps.interval.start_time >= "2026-06-01T00:00:00Z" AND '
                'steps.interval.end_time <= "2026-06-01T23:59:59Z"'
            )
        }

    def test_hyphen_converted_to_underscore(self):
        result = _parse_period("2026-06-01", "2026-06-01", "heart-rate")
        assert "heart_rate.interval.start_time" in result["filter"]
        assert "heart-rate" not in result["filter"]

    def test_multi_segment_slug(self):
        result = _parse_period("2026-06-01", "2026-06-01", "active-energy-burned")
        assert "active_energy_burned.interval.start_time" in result["filter"]

    def test_date_range_start_and_end(self):
        result = _parse_period("2026-06-01", "2026-06-07", "steps")
        assert '"2026-06-01T00:00:00Z"' in result["filter"]
        assert '"2026-06-07T23:59:59Z"' in result["filter"]

    @pytest.mark.parametrize("slug", INTERVAL_SLUGS)
    def test_no_data_type_prefix(self, slug):
        result = _parse_period("2026-06-01", "2026-06-01", slug)
        assert "data_type" not in result["filter"], (
            f"REGRESSÃO: filtro para '{slug}' contém prefixo 'data_type' inválido"
        )

    @pytest.mark.parametrize("slug", INTERVAL_SLUGS)
    def test_has_interval_start_and_end(self, slug):
        result = _parse_period("2026-06-01", "2026-06-01", slug)
        assert "interval.start_time" in result["filter"]
        assert "interval.end_time" in result["filter"]


# ---------------------------------------------------------------------------
# 3. _parse_daily_period — REGRESSÃO: nunca deve conter "data_type" ou "interval"
# ---------------------------------------------------------------------------

class TestParseDailyPeriod:
    def test_daily_resting_heart_rate_exact(self):
        result = _parse_daily_period("2026-06-01", "2026-06-07", "daily-resting-heart-rate")
        assert result == {
            "filter": (
                'daily_resting_heart_rate.date >= "2026-06-01" AND '
                'daily_resting_heart_rate.date <= "2026-06-07"'
            )
        }

    def test_daily_vo2_max_slug_conversion(self):
        result = _parse_daily_period("2026-06-01", "2026-06-01", "daily-vo2-max")
        assert "daily_vo2_max.date" in result["filter"]

    @pytest.mark.parametrize("slug", DAILY_SLUGS)
    def test_no_data_type_prefix(self, slug):
        result = _parse_daily_period("2026-06-01", "2026-06-01", slug)
        assert "data_type" not in result["filter"], (
            f"REGRESSÃO: filtro daily para '{slug}' contém prefixo 'data_type' inválido"
        )

    @pytest.mark.parametrize("slug", DAILY_SLUGS)
    def test_no_interval_in_daily_filter(self, slug):
        result = _parse_daily_period("2026-06-01", "2026-06-01", slug)
        assert "interval" not in result["filter"], (
            f"Filtro daily para '{slug}' não deve conter 'interval'"
        )

    @pytest.mark.parametrize("slug", DAILY_SLUGS)
    def test_uses_date_gte_and_lte(self, slug):
        result = _parse_daily_period("2026-06-01", "2026-06-07", slug)
        assert ".date >=" in result["filter"]
        assert ".date <=" in result["filter"]


# ---------------------------------------------------------------------------
# 4. _handle_http_error
# ---------------------------------------------------------------------------

class TestHandleHttpError:
    def test_401_mentions_health_authenticate(self):
        result = _handle_http_error(_make_http_error(401))
        assert "health_authenticate" in result

    def test_403_mentions_scope_or_permission(self):
        result = _handle_http_error(_make_http_error(403))
        assert "escopo" in result.lower() or "permissão" in result.lower()

    def test_404_mentions_not_found(self):
        result = _handle_http_error(_make_http_error(404))
        assert "não encontrado" in result or "not found" in result.lower()

    def test_429_mentions_limit(self):
        result = _handle_http_error(_make_http_error(429))
        assert "limite" in result.lower() or "quota" in result.lower()

    def test_500_includes_status_code(self):
        result = _handle_http_error(_make_http_error(500))
        assert "500" in result

    def test_500_with_json_body_includes_detail(self):
        body = json.dumps({"error": {"message": "internal server error"}})
        result = _handle_http_error(_make_http_error(500, body))
        assert "500" in result
        assert "internal" in result.lower()


# ---------------------------------------------------------------------------
# 5. Modelos Pydantic
# ---------------------------------------------------------------------------

class TestPydanticModels:
    def test_date_range_valid(self):
        m = DateRangeInput(start_date="2026-06-01", end_date="2026-06-05")
        assert m.start_date == "2026-06-01"
        assert m.end_date == "2026-06-05"

    def test_date_range_same_day_valid(self):
        m = DateRangeInput(start_date="2026-06-01", end_date="2026-06-01")
        assert m.start_date == m.end_date

    def test_date_range_start_after_end_fails(self):
        with pytest.raises(ValidationError):
            DateRangeInput(start_date="2026-06-05", end_date="2026-06-01")

    def test_date_range_invalid_format_fails(self):
        with pytest.raises(ValidationError):
            DateRangeInput(start_date="01/06/2026", end_date="2026-06-05")

    def test_data_type_query_default_limit(self):
        m = DataTypeQueryInput(start_date="2026-06-01", end_date="2026-06-05")
        assert m.limit == 100

    def test_data_type_query_custom_limit(self):
        m = DataTypeQueryInput(start_date="2026-06-01", end_date="2026-06-05", limit=50)
        assert m.limit == 50

    def test_data_type_query_limit_zero_fails(self):
        with pytest.raises(ValidationError):
            DataTypeQueryInput(start_date="2026-06-01", end_date="2026-06-05", limit=0)

    def test_data_type_query_limit_over_1000_fails(self):
        with pytest.raises(ValidationError):
            DataTypeQueryInput(start_date="2026-06-01", end_date="2026-06-05", limit=1001)

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            DataTypeQueryInput(start_date="2026-06-01", end_date="2026-06-05", unknown="x")

    def test_single_date_valid(self):
        m = SingleDateInput(date="2026-06-01")
        assert m.date == "2026-06-01"

    def test_auth_input_default_open_browser(self):
        m = AuthInput()
        assert m.open_browser is True

    def test_auth_input_open_browser_false(self):
        m = AuthInput(open_browser=False)
        assert m.open_browser is False


# ---------------------------------------------------------------------------
# 6. _load_tokens (mock de disco)
# ---------------------------------------------------------------------------

class TestLoadTokens:
    def test_returns_none_when_file_missing(self, tmp_path):
        nonexistent = tmp_path / "tokens.json"
        with patch("server.TOKEN_FILE", nonexistent):
            result = _load_tokens()
        assert result is None

    def test_returns_dict_with_valid_tokens(self, tmp_path):
        tokens = {
            "access_token": "ya29.abc",
            "expires_at": time.time() + 3600,
            "token_type": "Bearer",
            "refresh_token": "1//xyz",
        }
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps(tokens))
        with patch("server.TOKEN_FILE", token_file):
            result = _load_tokens()
        assert result is not None
        assert result["access_token"] == "ya29.abc"
        assert result["refresh_token"] == "1//xyz"

    def test_raises_on_missing_required_field(self, tmp_path):
        tokens = {
            "access_token": "ya29.abc",
            # expires_at ausente
            "token_type": "Bearer",
            "refresh_token": "1//xyz",
        }
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps(tokens))
        with patch("server.TOKEN_FILE", token_file):
            with pytest.raises(ValueError, match="corrompido"):
                _load_tokens()

    def test_raises_on_wrong_type_for_expires_at(self, tmp_path):
        tokens = {
            "access_token": "ya29.abc",
            "expires_at": "not-a-number",  # deveria ser int/float
            "token_type": "Bearer",
            "refresh_token": "1//xyz",
        }
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps(tokens))
        with patch("server.TOKEN_FILE", token_file):
            with pytest.raises(ValueError, match="corrompido"):
                _load_tokens()

    def test_raises_when_refresh_token_missing(self, tmp_path):
        tokens = {
            "access_token": "ya29.abc",
            "expires_at": time.time() + 3600,
            "token_type": "Bearer",
            # refresh_token ausente
        }
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps(tokens))
        with patch("server.TOKEN_FILE", token_file):
            with pytest.raises(ValueError, match="corrompido"):
                _load_tokens()


# ---------------------------------------------------------------------------
# 7. _save_tokens (tmp_path)
# ---------------------------------------------------------------------------

class TestSaveTokens:
    def test_creates_file_with_correct_content(self, tmp_path):
        tokens = {
            "access_token": "ya29.save",
            "expires_at": 9999.0,
            "token_type": "Bearer",
            "refresh_token": "1//save",
        }
        fake_file = tmp_path / "tokens.json"
        with patch("server.TOKEN_FILE", fake_file):
            _save_tokens(tokens)
        assert fake_file.exists()
        data = json.loads(fake_file.read_text())
        assert data["access_token"] == "ya29.save"
        assert data["refresh_token"] == "1//save"
        assert data["expires_at"] == 9999.0

    def test_overwrites_existing_file(self, tmp_path):
        fake_file = tmp_path / "tokens.json"
        fake_file.write_text(json.dumps({"access_token": "old"}))
        tokens = {"access_token": "new", "expires_at": 1.0, "token_type": "Bearer", "refresh_token": "x"}
        with patch("server.TOKEN_FILE", fake_file):
            _save_tokens(tokens)
        data = json.loads(fake_file.read_text())
        assert data["access_token"] == "new"

    @pytest.mark.skipif(sys.platform == "win32", reason="chmod 0o600 sem efeito garantido no Windows")
    def test_file_permissions_restricted(self, tmp_path):
        tokens = {"access_token": "x", "expires_at": 1.0, "token_type": "Bearer", "refresh_token": "x"}
        fake_file = tmp_path / "tokens.json"
        with patch("server.TOKEN_FILE", fake_file):
            _save_tokens(tokens)
        mode = fake_file.stat().st_mode & 0o777
        assert mode == 0o600


# ---------------------------------------------------------------------------
# 8. health_get_steps (ferramenta MCP — interval)
# ---------------------------------------------------------------------------

class TestHealthGetSteps:
    async def test_returns_json_on_success(self):
        mock_data = {"dataPoints": [{"value": [{"fpVal": 8000}]}]}
        with patch("server._health_get", new=AsyncMock(return_value=mock_data)):
            result = await health_get_steps(
                DataTypeQueryInput(start_date="2026-06-01", end_date="2026-06-01")
            )
        parsed = json.loads(result)
        assert "dataPoints" in parsed
        assert parsed["dataPoints"][0]["value"][0]["fpVal"] == 8000

    async def test_correct_endpoint_called(self):
        mock_data = {"dataPoints": []}
        with patch("server._health_get", new=AsyncMock(return_value=mock_data)) as mock_get:
            await health_get_steps(
                DataTypeQueryInput(start_date="2026-06-01", end_date="2026-06-01")
            )
        assert mock_get.call_args.args[0] == "steps/dataPoints"

    async def test_filter_uses_slug_prefix_not_data_type(self):
        mock_data = {"dataPoints": []}
        with patch("server._health_get", new=AsyncMock(return_value=mock_data)) as mock_get:
            await health_get_steps(
                DataTypeQueryInput(start_date="2026-06-01", end_date="2026-06-01")
            )
        call_params = mock_get.call_args.kwargs["params"]
        assert "steps.interval.start_time" in call_params["filter"]
        assert "data_type" not in call_params["filter"]

    async def test_runtime_error_returns_string(self):
        with patch("server._health_get", new=AsyncMock(side_effect=RuntimeError("Não autenticado. Use health_authenticate."))):
            result = await health_get_steps(
                DataTypeQueryInput(start_date="2026-06-01", end_date="2026-06-01")
            )
        assert "Não autenticado" in result

    async def test_http_401_returns_human_readable_error(self):
        err = _make_http_error(401)
        with patch("server._health_get", new=AsyncMock(side_effect=err)):
            result = await health_get_steps(
                DataTypeQueryInput(start_date="2026-06-01", end_date="2026-06-01")
            )
        assert "health_authenticate" in result
        assert "401" in result


# ---------------------------------------------------------------------------
# 9. health_get_resting_heart_rate (ferramenta MCP — daily)
# ---------------------------------------------------------------------------

class TestHealthGetRestingHeartRate:
    async def test_correct_endpoint(self):
        mock_data = {"dataPoints": []}
        with patch("server._health_get", new=AsyncMock(return_value=mock_data)) as mock_get:
            await health_get_resting_heart_rate(
                DateRangeInput(start_date="2026-06-01", end_date="2026-06-01")
            )
        assert mock_get.call_args.args[0] == "daily-resting-heart-rate/dataPoints"

    async def test_filter_uses_date_field_not_interval(self):
        mock_data = {"dataPoints": []}
        with patch("server._health_get", new=AsyncMock(return_value=mock_data)) as mock_get:
            await health_get_resting_heart_rate(
                DateRangeInput(start_date="2026-06-01", end_date="2026-06-07")
            )
        call_params = mock_get.call_args.kwargs["params"]
        assert "daily_resting_heart_rate.date >=" in call_params["filter"]
        assert "interval" not in call_params["filter"]
        assert "data_type" not in call_params["filter"]

    async def test_runtime_error_returns_string(self):
        with patch("server._health_get", new=AsyncMock(side_effect=RuntimeError("sem token"))):
            result = await health_get_resting_heart_rate(
                DateRangeInput(start_date="2026-06-01", end_date="2026-06-01")
            )
        assert "sem token" in result


# ---------------------------------------------------------------------------
# 10. health_get_daily_summary (10 fetches paralelos)
# ---------------------------------------------------------------------------

class TestHealthGetDailySummary:
    async def test_returns_all_expected_keys(self):
        mock_data = {"dataPoints": []}
        with patch("server._health_get", new=AsyncMock(return_value=mock_data)):
            result = await health_get_daily_summary(SingleDateInput(date="2026-06-01"))
        parsed = json.loads(result)
        expected = {
            "date", "steps", "distance_m", "active_energy_kcal",
            "resting_heart_rate_bpm", "hrv_ms", "spo2_pct",
            "heart_rate_zones", "sleep", "vo2max", "food_log",
        }
        assert set(parsed.keys()) == expected

    async def test_date_field_matches_input(self):
        mock_data = {"dataPoints": []}
        with patch("server._health_get", new=AsyncMock(return_value=mock_data)):
            result = await health_get_daily_summary(SingleDateInput(date="2026-06-01"))
        assert json.loads(result)["date"] == "2026-06-01"

    async def test_partial_failure_does_not_raise(self):
        call_count = 0

        async def flaky(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 3 == 0:
                raise RuntimeError("fetch parcial falhou")
            return {"dataPoints": []}

        with patch("server._health_get", new=flaky):
            result = await health_get_daily_summary(SingleDateInput(date="2026-06-01"))
        parsed = json.loads(result)
        assert "date" in parsed
        # Fetches com falha retornam {"error": "RuntimeError"} — não levantam exceção
        for key in ("steps", "distance_m", "active_energy_kcal"):
            assert parsed[key] is not None

    async def test_returns_error_dict_for_failed_fetch(self):
        """Safe_get captura exceções e retorna {'error': typename} sem interromper."""
        async def always_fail(*args, **kwargs):
            raise RuntimeError("API indisponível")

        with patch("server._health_get", new=always_fail):
            result = await health_get_daily_summary(SingleDateInput(date="2026-06-01"))
        parsed = json.loads(result)
        # Todos os campos de dados devem ter {"error": "RuntimeError"}
        for key in ("steps", "distance_m", "active_energy_kcal", "resting_heart_rate_bpm"):
            assert parsed[key] == {"error": "RuntimeError"}
