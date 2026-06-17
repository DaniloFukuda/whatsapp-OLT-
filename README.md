# olt-entulhos

MVP de automacao WhatsApp para gestao de contentores/cacambas de entulho em Portugal.

Esta primeira versao usa FastAPI, SQLite, SQLAlchemy e agentes deterministicos. O nucleo de dominio nao depende do WhatsApp; a integracao WhatsApp fica limitada ao webhook, parser e cliente da Cloud API.

## Instalar

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Configurar `.env`

Crie um arquivo `.env` baseado em `.env.example`:

```env
DATABASE_URL=sqlite:///./olt_entulhos.db
WHATSAPP_VERIFY_TOKEN=troque-este-token
WHATSAPP_OWNER_PHONE=
AUTHORIZED_OPERATOR_PHONE=556198266551
AUTHORIZED_OPERATOR_PHONES=
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_API_VERSION=v25.0
ENV=development
```

Nao use tokens reais em repositorio e nao commite `.env`.

Numeros importantes:

- `WHATSAPP_PHONE_NUMBER_ID` e o ID do numero na Meta, por exemplo `1148807428322172`. Ele nao e o numero que envia comandos.
- O numero do bot/API, exibido como `display_phone_number`, e diferente do numero do operador.
- `AUTHORIZED_OPERATOR_PHONE` deve ser o numero de quem envia comandos para o bot. Exemplo correto: `AUTHORIZED_OPERATOR_PHONE=556198266551`.
- Para varios operadores, use `AUTHORIZED_OPERATOR_PHONES=556198266551,351XXXXXXXXX`.
- `WHATSAPP_OWNER_PHONE` e `OWNER_WHATSAPP` ainda funcionam por compatibilidade.
- Os numeros sao comparados normalizados: `+`, espacos, hifens e parenteses sao ignorados.

## Envio WhatsApp

O envio real usa a WhatsApp Cloud API quando todas as condicoes abaixo forem verdadeiras:

```text
ENV != test
WHATSAPP_ACCESS_TOKEN preenchido
WHATSAPP_PHONE_NUMBER_ID preenchido
```

Endpoint usado:

```text
https://graph.facebook.com/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages
```

Quando `ENV=test`, ou quando token/phone number id estiverem vazios, o envio fica em modo mock e retorna `status: mocked`.

Nunca coloque token real no README, nos testes ou no repositorio. Mantenha apenas no `.env` local/seguro.

## Rodar localmente

```bash
uvicorn app.main:app --reload
```

API local: http://127.0.0.1:8000

## Setup local automatizado

Para ajustar `.env`, validar testes, subir o servidor local, testar o webhook e preparar um commit seguro:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_local_olt_entulhos.ps1
```

Para validar tambem uma URL publica do Ngrok:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_local_olt_entulhos.ps1 -NgrokUrl "https://eraser-badland-roaming.ngrok-free.dev"
```

O script cria backup `.env.backup_YYYYMMDD_HHMMSS`, mas `.env` e backups nunca devem ser commitados.

## Testar webhook GET da Meta

```bash
curl "http://127.0.0.1:8000/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=troque-este-token&hub.challenge=12345"
```

Resposta esperada:

```text
12345
```

## Rodar testes

```bash
pytest
```

## Fluxo principal

1. Lucas envia `novo`.
2. O sistema escolhe automaticamente o primeiro contentor disponivel entre `C01` e `C20`.
3. O bot pede foto do contentor no local.
4. O bot pede localizacao.
5. O bot pede nome do cliente.
6. O bot pede telefone do cliente.
7. O bot pede valor.
8. O bot pergunta se esta pago.
9. O bot pergunta forma de pagamento.
10. O sistema registra o aluguer, cria eventos, marca o contentor como `alugado`, calcula vencimento em 5 dias e deixa a conversa como `confirmado`.

Estados do fluxo:

```text
aguardando_foto_entrega
aguardando_localizacao
aguardando_nome_cliente
aguardando_telefone_cliente
aguardando_valor
aguardando_pago
aguardando_forma_pagamento
confirmado
```

## Testar fluxo local do WhatsApp

Com o servidor rodando, envie payloads simulados para `POST /webhook/whatsapp`. Em modo mock a resposta inclui as mensagens que seriam enviadas; com credenciais preenchidas, ela retorna o status da chamada real.

Iniciar novo aluguer:

```bash
curl -X POST "http://127.0.0.1:8000/webhook/whatsapp" \
  -H "Content-Type: application/json" \
  -d "{\"entry\":[{\"changes\":[{\"value\":{\"messages\":[{\"from\":\"351900000000\",\"id\":\"m1\",\"type\":\"text\",\"text\":{\"body\":\"novo\"}}]}}]}]}"
```

Enviar foto:

```bash
curl -X POST "http://127.0.0.1:8000/webhook/whatsapp" \
  -H "Content-Type: application/json" \
  -d "{\"entry\":[{\"changes\":[{\"value\":{\"messages\":[{\"from\":\"351900000000\",\"id\":\"m2\",\"type\":\"image\",\"image\":{\"id\":\"foto-123\",\"mime_type\":\"image/jpeg\"}}]}}]}]}"
```

Enviar localizacao:

```bash
curl -X POST "http://127.0.0.1:8000/webhook/whatsapp" \
  -H "Content-Type: application/json" \
  -d "{\"entry\":[{\"changes\":[{\"value\":{\"messages\":[{\"from\":\"351900000000\",\"id\":\"m3\",\"type\":\"location\",\"location\":{\"latitude\":38.7223,\"longitude\":-9.1393}}]}}]}]}"
```

Depois envie mensagens de texto na mesma estrutura para:

```text
Cliente Teste
351911111111
150,50
sim
mbway
```

Se `AUTHORIZED_OPERATOR_PHONE`, `AUTHORIZED_OPERATOR_PHONES`, `WHATSAPP_OWNER_PHONE` ou `OWNER_WHATSAPP` estiverem definidos no `.env`, apenas esses telefones podem iniciar o fluxo com `novo`.

## Proximos passos

- Baixar e armazenar midias recebidas do WhatsApp.
- Adicionar autenticacao para rotas de dashboard.
- Criar jobs periodicos para envio automatico de lembretes.
- Expandir comandos de renovacao, recolha e consulta por WhatsApp.
