<#!
Sets pilot-only process environment variables. It does not edit .env, start
APScheduler, connect to PostgreSQL, call Gemini, or run the newsroom pilot.

Run this in the same PowerShell window as pilot_preflight.py and the eventual
operator-authorized pilot command. The password is not written to disk.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("AUTHORIZED_FREE_TIER_PILOT")]
    [string]$PilotOperatorAuthorization,

    [Parameter(Mandatory)]
    [ValidateRange(1, 100)]
    [int]$PilotMaxRequests,

    [Parameter(Mandatory)]
    [string]$PilotAllowedModel,

    [Parameter(Mandatory)]
    [string]$PilotDbPassword,

    [string]$PilotDbHost = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$PilotDbPort = 55434
)

if ([string]::IsNullOrWhiteSpace($PilotDbPassword)) {
    throw "PilotDbPassword must not be empty."
}

$escapedPassword = [uri]::EscapeDataString($PilotDbPassword)
$env:SCHEDULER_ENABLED = "false"
$env:X_PUBLISHING_MODE = "local_draft"
$env:PUBLISHING_ENABLED = "false"
$env:DRY_RUN = "true"
$env:X_API_ENABLED = "false"
$env:X_READS_ENABLED = "false"
$env:X_RESEARCH_ENABLED = "false"
$env:ENABLE_REPLY_SCANNER = "false"
$env:AI_REPLY_PUBLISHING_ENABLED = "false"
$env:PILOT_OPERATOR_AUTHORIZATION = $PilotOperatorAuthorization
$env:PILOT_FREE_TIER_ONLY = "true"
$env:PILOT_MAX_COST_USD = "0.00"
$env:PILOT_MAX_REQUESTS = "$PilotMaxRequests"
$env:PILOT_ALLOWED_MODELS = $PilotAllowedModel
$env:PILOT_DATABASE_URL = "postgresql+asyncpg://x_agent_pilot_app:${escapedPassword}@${PilotDbHost}:${PilotDbPort}/x_agent_pilot"

@{
    scheduler_enabled = $env:SCHEDULER_ENABLED
    x_publishing_mode = $env:X_PUBLISHING_MODE
    publishing_enabled = $env:PUBLISHING_ENABLED
    dry_run = $env:DRY_RUN
    x_api_enabled = $env:X_API_ENABLED
    pilot_database = "x_agent_pilot"
    pilot_host = $PilotDbHost
    pilot_port = $PilotDbPort
    pilot_max_requests = $env:PILOT_MAX_REQUESTS
    pilot_allowed_models = $env:PILOT_ALLOWED_MODELS
} | ConvertTo-Json
