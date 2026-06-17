param(
    [string]$NgrokUrl = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

$VerifyToken = "olt_entulhos_webhook_2026_danilo"
$RequiredEnv = [ordered]@{
    "AUTHORIZED_OPERATOR_PHONE" = "556198266551"
    "AUTHORIZED_OPERATOR_PHONES" = "556198266551"
    "WHATSAPP_BUSINESS_ACCOUNT_ID" = "1502228507690349"
    "WHATSAPP_PHONE_NUMBER_ID" = "1148807428322172"
    "WHATSAPP_VERIFY_TOKEN" = $VerifyToken
    "WHATSAPP_API_VERSION" = "v25.0"
    "ENV" = "development"
}
$RequiredGitignore = @(
    ".env",
    "*.db",
    "media/",
    ".env.backup_*"
)

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message"
}

function Get-DisplayEnvValue {
    param([string]$Name, [string]$Value)
    if ($Name -eq "WHATSAPP_ACCESS_TOKEN") {
        return "***REDACTED***"
    }
    return $Value
}

function Backup-EnvFile {
    $envPath = Join-Path $ProjectRoot ".env"
    if (-not (Test-Path $envPath)) {
        New-Item -Path $envPath -ItemType File | Out-Null
        Write-Host "Criado .env vazio."
        return
    }

    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $backupPath = Join-Path $ProjectRoot ".env.backup_$timestamp"
    Copy-Item -LiteralPath $envPath -Destination $backupPath
    Write-Host "Backup criado: $(Split-Path $backupPath -Leaf)"
}

function Update-EnvFile {
    $envPath = Join-Path $ProjectRoot ".env"
    $lines = [System.Collections.Generic.List[string]]::new()
    if (Test-Path $envPath) {
        foreach ($line in (Get-Content -LiteralPath $envPath)) {
            $lines.Add($line)
        }
    }

    foreach ($key in $RequiredEnv.Keys) {
        $value = $RequiredEnv[$key]
        $pattern = "^\s*$([regex]::Escape($key))\s*="
        $foundIndex = -1

        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($lines[$i] -match $pattern) {
                if ($foundIndex -eq -1) {
                    $foundIndex = $i
                    $lines[$i] = "$key=$value"
                } else {
                    $lines.RemoveAt($i)
                    $i--
                }
            }
        }

        if ($foundIndex -eq -1) {
            $lines.Add("$key=$value")
        }
    }

    Set-Content -LiteralPath $envPath -Value $lines -Encoding UTF8

    Write-Host "Variaveis locais ajustadas:"
    foreach ($key in $RequiredEnv.Keys) {
        Write-Host "  $key=$(Get-DisplayEnvValue $key $RequiredEnv[$key])"
    }
    Write-Host "  WHATSAPP_ACCESS_TOKEN=***REDACTED***"
}

function Ensure-Gitignore {
    $gitignorePath = Join-Path $ProjectRoot ".gitignore"
    $lines = [System.Collections.Generic.List[string]]::new()
    if (Test-Path $gitignorePath) {
        foreach ($line in (Get-Content -LiteralPath $gitignorePath)) {
            $lines.Add($line)
        }
    }

    foreach ($entry in $RequiredGitignore) {
        if (-not ($lines | Where-Object { $_.Trim() -eq $entry })) {
            $lines.Add($entry)
        }
    }

    Set-Content -LiteralPath $gitignorePath -Value $lines -Encoding UTF8
}

function Invoke-Tests {
    Write-Step "Rodando testes"
    python -m pytest
    if ($LASTEXITCODE -ne 0) {
        throw "pytest falhou."
    }
}

function Get-HealthContent {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/health" -TimeoutSec 5
        return $response.Content.Trim()
    } catch {
        return $null
    }
}

function Start-LocalServerIfNeeded {
    $health = Get-HealthContent
    if ($health -eq '{"status":"ok"}') {
        Write-Host "Servidor local ja responde em /health."
        return
    }

    Write-Host "Subindo servidor local na porta 8000."
    Start-Process -FilePath "python" `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--port", "8000") `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden | Out-Null

    Start-Sleep -Seconds 3
    $health = Get-HealthContent
    if ($health -ne '{"status":"ok"}') {
        throw "Servidor local nao respondeu /health com ok. Resposta: $health"
    }
}

function Restart-LocalServer {
    Write-Host "Reiniciando servidor local na porta 8000 para recarregar .env."
    $connections = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in $connections) {
        Stop-Process -Id $connection.OwningProcess -Force -ErrorAction SilentlyContinue
    }

    Start-Process -FilePath "python" `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--port", "8000") `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden | Out-Null

    Start-Sleep -Seconds 3
}

function Test-Health {
    Write-Step "Validando /health local"
    $health = Get-HealthContent
    Write-Host "Resposta /health: $health"
    if ($health -ne '{"status":"ok"}') {
        throw "Healthcheck inesperado."
    }
}

function Get-WebhookVerifyUri {
    param([string]$BaseUrl)
    $base = $BaseUrl.TrimEnd("/")
    return "$base/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=$VerifyToken&hub.challenge=12345"
}

function Test-WebhookUrl {
    param(
        [string]$BaseUrl,
        [bool]$AllowRestart = $false,
        [bool]$AllowUnavailable = $false
    )

    $uri = Get-WebhookVerifyUri $BaseUrl
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $uri -TimeoutSec 10
        $content = $response.Content.Trim()
    } catch {
        $errorText = "$($_.Exception.Message) $($_.ErrorDetails.Message)"
        if ($AllowUnavailable -and ($errorText -match "ERR_NGROK_3200|offline|NameResolutionFailure|timed out")) {
            Write-Warning "Ngrok indisponivel; validacao publica ignorada. Detalhe: $($_.Exception.Message)"
            return
        }
        if ($AllowRestart) {
            Restart-LocalServer
            $response = Invoke-WebRequest -UseBasicParsing -Uri $uri -TimeoutSec 10
            $content = $response.Content.Trim()
        } else {
            throw
        }
    }

    if ($content -ne "12345" -and $AllowRestart) {
        Restart-LocalServer
        $response = Invoke-WebRequest -UseBasicParsing -Uri $uri -TimeoutSec 10
        $content = $response.Content.Trim()
    }

    Write-Host "Resposta webhook ($BaseUrl): $content"
    if ($content -ne "12345") {
        throw "Webhook verify inesperado para $BaseUrl."
    }
}

function Normalize-NgrokUrl {
    param([string]$RawUrl)
    if ([string]::IsNullOrWhiteSpace($RawUrl)) {
        return ""
    }

    $match = [regex]::Match($RawUrl, "https?://[^\]\)\s]+")
    if ($match.Success) {
        return $match.Value.TrimEnd("/")
    }
    return $RawUrl.Trim().TrimEnd("/")
}

function Ensure-GitRepository {
    Write-Step "Preparando Git"
    if (-not (Test-Path (Join-Path $ProjectRoot ".git"))) {
        git init
        if ($LASTEXITCODE -ne 0) {
            throw "git init falhou."
        }
    }

    git branch -M main
    if ($LASTEXITCODE -ne 0) {
        throw "git branch -M main falhou."
    }
}

function Assert-StagedFilesAreSafe {
    $staged = @(git diff --cached --name-only)
    $unsafe = @()

    foreach ($file in $staged) {
        $normalized = $file -replace "\\", "/"
        if (
            $normalized -eq ".env" -or
            $normalized -like ".env.backup_*" -or
            $normalized -like "*.db" -or
            $normalized -like "*.sqlite" -or
            $normalized -like "*.sqlite3" -or
            $normalized -eq "media" -or
            $normalized.StartsWith("media/")
        ) {
            $unsafe += $file
        }
    }

    if ($unsafe.Count -gt 0) {
        git restore --staged -- $unsafe 2>$null
        throw "Commit abortado: arquivos sensiveis estavam staged: $($unsafe -join ', ')"
    }
}

function Commit-SafeFiles {
    Write-Step "Status Git antes do commit"
    git status --short

    git add -A
    if ($LASTEXITCODE -ne 0) {
        throw "git add falhou."
    }

    Assert-StagedFilesAreSafe

    $staged = @(git diff --cached --name-only)
    if ($staged.Count -eq 0) {
        Write-Host "Nenhuma alteracao segura para commitar."
    } else {
        git commit -m "Ajusta configuração local e autorização do WhatsApp"
        if ($LASTEXITCODE -ne 0) {
            throw "git commit falhou."
        }
    }

    Write-Step "Status Git final"
    git status --short
}

Write-Step "Backup e atualizacao do .env"
Backup-EnvFile
Update-EnvFile

Write-Step "Garantindo .gitignore"
Ensure-Gitignore

Invoke-Tests

Write-Step "Servidor local"
Start-LocalServerIfNeeded
Test-Health

Write-Step "Validando webhook local"
Test-WebhookUrl -BaseUrl "http://127.0.0.1:8000" -AllowRestart $true

$cleanNgrokUrl = Normalize-NgrokUrl $NgrokUrl
if ($cleanNgrokUrl) {
    Write-Step "Validando webhook via Ngrok"
    Test-WebhookUrl -BaseUrl $cleanNgrokUrl -AllowRestart $false -AllowUnavailable $true
}

Ensure-GitRepository
Commit-SafeFiles

Write-Step "Setup local concluido"
