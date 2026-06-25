$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
Set-Location $ProjectRoot

Write-Host "== Validacao final dos fluxos OLT =="
Write-Host "Projeto: $ProjectRoot"
Write-Host ""

$criticalFiles = @(
    "app/core/schema_migrations.py",
    "app/agents/localizacao_agent.py",
    "app/agents/whatsapp_router_agent.py",
    "app/agents/entrega_agent.py",
    "app/services/aluguer_service.py",
    "tests/test_schema_migrations.py",
    "tests/test_cadastro_pedido_paulo.py"
)

Write-Host "== Conferindo arquivos criticos =="
foreach ($file in $criticalFiles) {
    if (-not (Test-Path -LiteralPath $file)) {
        throw "Arquivo critico ausente: $file"
    }
    Write-Host "OK $file"
}
Write-Host ""

Write-Host "== Git status =="
git status --short
Write-Host ""

Write-Host "== Git diff --check =="
git diff --check
Write-Host "OK git diff --check"
Write-Host ""

$python = Join-Path $ProjectRoot ".venv/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = "python"
}
Write-Host "== Python selecionado =="
Write-Host $python
Write-Host ""

$tempRoot = Join-Path $ProjectRoot ".pytest_tmp_validation"
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
$env:TEMP = $tempRoot
$env:TMP = $tempRoot
$env:PYTHONUTF8 = "1"

function Assert-FileContains {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$Needles,
        [string]$Label = $Path
    )

    $content = Get-Content -LiteralPath $Path -Raw
    foreach ($needle in $Needles) {
        if (-not $content.Contains($needle)) {
            throw "$Label nao contem item esperado: $needle"
        }
        Write-Host "OK $Label contem $needle"
    }
}

Write-Host "== Conferindo schema_migrations.py =="
Assert-FileContains `
    -Path "app/core/schema_migrations.py" `
    -Label "schema_migrations.py" `
    -Needles @(
        "contentor_fotos",
        "contentor_fotos_recolha",
        "status_entrega",
        "pedido_feito_por",
        "entrega_feita_por",
        "alterado_por_operador",
        "pedido_endereco_tipo",
        "pedido_endereco_texto",
        "pedido_latitude",
        "pedido_longitude",
        "pedido_ponto_referencia",
        "entrega_latitude",
        "entrega_longitude",
        "entrega_ponto_referencia",
        "status_ciclo",
        "recolha_feita_por",
        "recolha_data_hora",
        "carga_errada",
        "relato_carga",
        "status_resolucao_carga",
        "contentor_avariado",
        "relato_avaria",
        "status_resolucao_avaria"
    )
Write-Host ""

Write-Host "== Conferindo localizacao_agent.py =="
Assert-FileContains `
    -Path "app/agents/localizacao_agent.py" `
    -Label "localizacao_agent.py" `
    -Needles @("!3d", "!4d", "parse_qs", '"q"', "COORD_PATTERN")
Write-Host ""

Write-Host "== Conferindo whatsapp_router_agent.py =="
Assert-FileContains `
    -Path "app/agents/whatsapp_router_agent.py" `
    -Label "whatsapp_router_agent.py" `
    -Needles @(
        "FUNCIONARIO",
        "Confirmar entrega de contentor",
        "Confirmar recolha de contentor",
        "entrega_agent.start",
        "recolha_agent.start"
    )
Write-Host ""

Write-Host "== Compilando app e tests =="
& $python -m compileall app tests
Write-Host ""

Write-Host "== Pytest focado =="
& $python -m pytest tests/test_schema_migrations.py tests/test_cadastro_pedido_paulo.py -q
Write-Host ""

Write-Host "== Pytest completo =="
& $python -m pytest -q
Write-Host ""

Write-Host "== Validacao passou =="
Write-Host "Checklist manual ainda recomendado antes de commit/push:"
Write-Host "- Gestor cria pedido pendente."
Write-Host "- Funcionario opcao 1 abre entrega."
Write-Host "- Entrega exige foto, GPS real e ponto de referencia."
Write-Host "- Se estava pendente, pergunta pagamento no ato."
Write-Host "- Funcionario faz recolha."
Write-Host "- Recolha registra carga errada/avaria quando houver."
Write-Host "- Resumo do gestor mostra pendencias."
