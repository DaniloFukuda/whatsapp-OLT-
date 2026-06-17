param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$From = "556198266551",
    [string]$ProfileName = "Danilo Fukuda",
    [switch]$AllowRealSend
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

$WebhookUrl = ($BaseUrl.Trim().TrimEnd("/")) + "/webhook/whatsapp"
$WabaId = "1502228507690349"
$DisplayPhoneNumber = "556196870361"
$PhoneNumberId = "1148807428322172"

Write-Host "ATENCAO: este script envia payloads fake para o webhook local."
Write-Host "Por padrao, ele envia o header X-OLT-Mock-Whatsapp=true para evitar envio real."
Write-Host "Se usar -AllowRealSend e o client estiver em modo real (ENV=development com token real), o sistema pode responder pelo WhatsApp real."
Write-Host "O script nao le, nao imprime e nao altera WHATSAPP_ACCESS_TOKEN."
Write-Host ""
Write-Host "Webhook: $WebhookUrl"
Write-Host "Operador simulado: $From"
Write-Host "Modo de envio: $(if ($AllowRealSend) { 'real permitido' } else { 'mock forcado' })"

function New-Wamid {
    return "wamid.fake.$([guid]::NewGuid().ToString('N'))"
}

function New-BasePayload {
    param([hashtable]$Message)

    return @{
        object = "whatsapp_business_account"
        entry = @(
            @{
                id = $WabaId
                changes = @(
                    @{
                        field = "messages"
                        value = @{
                            messaging_product = "whatsapp"
                            metadata = @{
                                display_phone_number = $DisplayPhoneNumber
                                phone_number_id = $PhoneNumberId
                            }
                            contacts = @(
                                @{
                                    profile = @{ name = $ProfileName }
                                    wa_id = $From
                                }
                            )
                            messages = @($Message)
                        }
                    }
                )
            }
        )
    }
}

function New-TextMessage {
    param([string]$Body)
    return @{
        from = $From
        id = New-Wamid
        timestamp = [string][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        type = "text"
        text = @{ body = $Body }
    }
}

function New-ImageMessage {
    return @{
        from = $From
        id = New-Wamid
        timestamp = [string][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        type = "image"
        image = @{
            id = "fake_media_contentor_001"
            mime_type = "image/jpeg"
            sha256 = "fake_sha256_contentor_001"
        }
    }
}

function New-LocationMessage {
    return @{
        from = $From
        id = New-Wamid
        timestamp = [string][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        type = "location"
        location = @{
            latitude = 38.7223
            longitude = -9.1393
            name = "Obra teste Lisboa"
            address = "Lisboa, Portugal"
        }
    }
}

function Send-Step {
    param(
        [string]$Name,
        [hashtable]$Message
    )

    Write-Host ""
    Write-Host "==> $Name"
    $payload = New-BasePayload -Message $Message
    $json = $payload | ConvertTo-Json -Depth 20

    try {
        $headers = @{}
        if (-not $AllowRealSend) {
            $headers["X-OLT-Mock-Whatsapp"] = "true"
        }
        $response = Invoke-WebRequest -UseBasicParsing -Method Post -Uri $WebhookUrl -Headers $headers -ContentType "application/json" -Body $json -TimeoutSec 20
        Write-Host "HTTP status: $($response.StatusCode)"
        Write-Host "Resposta JSON:"
        Write-Host $response.Content

        $parsed = $response.Content | ConvertFrom-Json
        if ($parsed.messages -and $parsed.messages.Count -gt 0) {
            Write-Host "Primeira mensagem de resposta: $($parsed.messages[0].body)"
        } else {
            Write-Host "Primeira mensagem de resposta: <nenhuma>"
        }
    } catch {
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            Write-Host "HTTP status: $([int]$_.Exception.Response.StatusCode)"
        }
        throw
    }
}

$steps = @(
    @{ Name = "A) texto novo"; Message = New-TextMessage -Body "novo" },
    @{ Name = "B) imagem fake"; Message = New-ImageMessage },
    @{ Name = "C) localizacao fake"; Message = New-LocationMessage },
    @{ Name = "D) nome cliente"; Message = New-TextMessage -Body "Cliente Teste Lisboa" },
    @{ Name = "E) telefone cliente"; Message = New-TextMessage -Body "+351 912 345 678" },
    @{ Name = "F) valor"; Message = New-TextMessage -Body "70" },
    @{ Name = "G) pago"; Message = New-TextMessage -Body "sim" },
    @{ Name = "H) forma de pagamento"; Message = New-TextMessage -Body "transferencia" }
)

foreach ($step in $steps) {
    Send-Step -Name $step.Name -Message $step.Message
}

Write-Host ""
Write-Host "==> Ultimo aluguer registrado"
python scripts/inspect_latest_aluguer.py
if ($LASTEXITCODE -ne 0) {
    throw "Falha ao consultar ultimo aluguer."
}
