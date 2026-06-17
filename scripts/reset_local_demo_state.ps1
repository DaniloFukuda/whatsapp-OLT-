$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

Write-Host "Resetando somente dados locais de demonstracao."
Write-Host "Este script nao altera .env, nao imprime tokens e nao deleta a estrutura do banco."

python scripts/reset_local_demo_state.py
if ($LASTEXITCODE -ne 0) {
    throw "Reset local falhou."
}

Write-Host "Reset local concluido."
