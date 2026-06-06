# google-health-mcp — CLAUDE.md

## Project overview

This is a local MCP (Model Context Protocol) server that exposes Google Health API v4 data as tools for Claude Desktop. It allows Claude to read health and fitness data — steps, heart rate, HRV, sleep, VO2 Max, exercises, SpO2, weight — from a Fitbit device connected to a Google account.

The project targets endurance athletes (the codebase explicitly mentions duathlon). All data types and field names are chosen with that use case in mind.

The Google Health API v4 is the successor to Google Fit (deprecated in 2026) and the Fitbit Web API. It accesses Fitbit data through a Google account.

## Stack

- Language: Python 3.10+
- MCP framework: `mcp[cli]` with `FastMCP` (`mcp.server.fastmcp`)
- HTTP client: `httpx` (async)
- Data validation: `pydantic` v2 (`BaseModel`, `ConfigDict`, `Field`)
- Auth: OAuth2 Authorization Code flow with a local redirect server on `localhost:8765`
- No build step, no test suite, no dependency lock file — single-file project

## Dependencies (runtime)

```
mcp[cli]
httpx
pydantic
```

Install with:
```bash
pip install "mcp[cli]" httpx
```

Pydantic is pulled in transitively by `mcp[cli]` but is used explicitly in the source.

## Running the server

The server is started directly by Claude Desktop via its MCP configuration — it is not meant to be run standalone in normal use. If you need to test it manually:

```bash
python server.py
```

Claude Desktop config (Windows path):
```
%APPDATA%\Claude\claude_desktop_config.json
```

Entry in that file:
```json
{
  "mcpServers": {
    "google-health": {
      "command": "python",
      "args": ["C:\\path\\to\\mcp-google-health\\server.py"]
    }
  }
}
```

## File structure

```
mcp-google-health/
└── server.py          # Entire server — all tools, OAuth logic, HTTP client helpers
```

External files the server reads/writes at runtime (outside the repo):
```
~/.config/google-health-mcp/credentials.json   # OAuth client ID/secret (user-provided)
~/.config/google-health-mcp/tokens.json        # Access + refresh tokens (auto-generated)
```

## Architecture of server.py

The file is a single Python module with clear section delimiters (`# ---`). Reading top-to-bottom:

1. **Constants** — API base URLs, OAuth scopes, file paths, `DATA_TYPES` dict mapping friendly names to API slugs.
2. **Token management** — `_load_credentials`, `_load_tokens`, `_save_tokens`, `_refresh_access_token`, `_get_valid_access_token`. Tokens are stored as JSON on disk; expiry is checked with a 60-second buffer.
3. **HTTP client helpers** — `_health_get` (single page GET), `_health_get_all_pages` (paginated aggregation), `_handle_http_error` (friendly error messages), `_parse_period` / `_parse_daily_period` / `_fmt_date` (date/filter builders).
4. **MCP server init** — `FastMCP("google_health_mcp", lifespan=app_lifespan)`. The lifespan only validates that credentials.json exists at startup.
5. **Pydantic input models** — `AuthInput`, `DateRangeInput`, `DataTypeQueryInput`, `SingleDateInput`. All use `extra="forbid"`.
6. **MCP tools** (decorated with `@mcp.tool`) organized in sections: Auth, Activity/Fitness, Health Metrics, Sleep, Data Sources, Profile, Daily Summary.

## MCP tools exposed

| Tool name | Description |
|---|---|
| `health_authenticate` | OAuth2 flow — opens browser, captures callback, saves tokens |
| `health_auth_status` | Shows token validity and expiry |
| `health_get_steps` | Steps count for a date range |
| `health_get_distance` | Distance in meters |
| `health_get_active_energy` | Active calories burned (kcal) |
| `health_get_active_minutes` | Active minutes + Active Zone Minutes (parallel fetch) |
| `health_get_vo2max` | Daily VO2 Max estimate |
| `health_get_exercises` | Exercise sessions (run, bike, etc.) |
| `health_get_heart_rate` | Intraday heart rate samples |
| `health_get_resting_heart_rate` | Daily resting heart rate |
| `health_get_hrv` | Daily HRV (ms) |
| `health_get_heart_rate_zones` | Time in each HR zone per day |
| `health_get_spo2` | Blood oxygen saturation (SpO2) |
| `health_get_weight` | Body weight records |
| `health_get_sleep` | Sleep sessions with stages |
| `health_get_profile` | User profile from Google Health |
| `health_get_nutrition_log` | Food log entries (FatSecret via Google Health) |
| `health_get_calories_consumed` | Nutrition log aliased for calorie lookup (same endpoint as nutrition_log) |
| `health_get_daily_summary` | All metrics for a single date (10 parallel fetches) |
| `health_list_steps_data_sources` | Which devices/apps contributed step data |

## API details

- Base URL: `https://health.googleapis.com/v4/users/me/dataTypes`
- Profile URL: `https://health.googleapis.com/v4/users/me/profile`
- Filter format for interval-based types: `data_type.interval.start_time >= "YYYY-MM-DDT00:00:00Z" AND data_type.interval.end_time <= "YYYY-MM-DDT23:59:59Z"`
- Filter format for daily types (prefix `daily-`): `data_type.date >= "YYYY-MM-DD" AND data_type.date <= "YYYY-MM-DD"`
- **Rule**: data type slugs that start with `daily-` are date-based — always use `_parse_daily_period`. Other slugs use `_parse_period`.
- Pagination: `pageSize` param + `nextPageToken` in response; `_health_get_all_pages` handles up to 5 pages (uses `pageToken`)

## OAuth scopes required

```
googlehealth.activity_and_fitness.readonly
googlehealth.health_metrics_and_measurements.readonly
googlehealth.sleep.readonly
googlehealth.nutrition.readonly
googlehealth.profile.readonly
```

OAuth redirect runs a temporary `HTTPServer` on `localhost:8765` in a daemon thread with a 120-second timeout.

## Coding conventions

- All tools return `str` (JSON-serialized with `json.dumps(..., ensure_ascii=False, indent=2)` or a plain error string).
- Error handling: `httpx.HTTPStatusError` is caught and converted to a user-friendly string via `_handle_http_error`. `RuntimeError` (unauthenticated) is caught and its message returned as-is.
- Concurrent fetches use `asyncio.gather` — see `health_get_active_minutes` and `health_get_daily_summary`.
- All input models use `ConfigDict(extra="forbid")` to reject unexpected fields.
- Comments and docstrings are in Portuguese (Brazilian).
- Tool annotations use the MCP hint fields: `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`.

## Adding a new data type

1. Add an entry to `DATA_TYPES` dict with the official API slug (kebab-case, e.g. `daily-vo2-max`).
2. Determine if the type is **date-based** (slug starts with `daily-`) or **interval-based** (everything else).
3. Create a new `@mcp.tool` function: use `DateRangeInput` + `_parse_daily_period` for daily types; use `DataTypeQueryInput` + `_parse_period` for interval types.
4. Pass pagination as `"pageSize": limit` (camelCase).
5. Return `json.dumps(data, ensure_ascii=False, indent=2)`.
6. Wrap with the standard `except httpx.HTTPStatusError` / `except RuntimeError` blocks.

## Known limitations

- Garmin data is not accessible through this API. Google Health v4 surfaces Fitbit/Google Fit data only.
- The `_health_get_all_pages` helper is defined but no tool currently calls it (all tools use `_health_get` directly with `pageSize`).
- No test suite exists.
- Token file is plain JSON with no encryption — treat `~/.config/google-health-mcp/` as sensitive.
- The OAuth local server uses a single-threaded `HTTPServer.handle_request()` — it handles exactly one callback then stops.
- Nutrition data types (`nutrition-log`) may not return real data currently — the Google Health API v4 does not yet document any nutrition data type ID as fully supported. The scope `googlehealth.nutrition.readonly` is correct but data availability is uncertain.
- The filter prefix `data_type` (e.g. `data_type.interval.start_time`) may be a valid API placeholder or may need to be replaced with the actual type name in snake_case (e.g. `steps.interval.start_time`). The current format is what the original developer documented and tested.
