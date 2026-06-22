# Cadastro unitario de contentores via WhatsApp

## Visao geral

Este documento consolida o comportamento final do modulo de cadastro unitario de contentores pelo WhatsApp. O fluxo permite que o operador registre uma entrega de contentor com identificacao do contentor, foto, localizacao, cliente, pagamento e confirmacao final antes da gravacao.

O cadastro e conduzido por etapas, mantendo o estado da conversa e validando cada resposta antes de avancar. Ao confirmar, o sistema grava o aluguer, vincula o contentor interno disponivel, registra o numero informado pelo operador e retorna o resumo do cadastro salvo.

## Regra principal: 1 cadastro = 1 contentor

Cada execucao do fluxo representa exatamente um cadastro para exatamente um contentor.

- O operador informa um unico `numero_contentor`.
- O sistema associa o cadastro a um contentor interno disponivel.
- O fluxo nao solicita nem aceita quantidade de contentores.
- Para cadastrar mais de um contentor, o operador deve repetir o fluxo uma vez para cada contentor.

## Campo obrigatorio: numero_contentor

O campo `numero_contentor` e obrigatorio e passou a ser a primeira pergunta do fluxo.

Regras:

- Nao pode ser vazio.
- Deve ter entre 1 e 20 caracteres.
- Aceita formatos operacionais como `12`, `C12`, `OLT-12` ou codigos equivalentes usados pela equipa.
- Se invalido, o bot permanece na mesma etapa e responde com orientacao de exemplo.

Mensagem de validacao:

```text
⚠️ Informe o numero do contentor. Exemplo: 12, C12 ou OLT-12.
```

## Remocao de quantidade_contentores

O campo `quantidade_contentores` foi removido do modelo funcional do cadastro.

Impactos:

- O fluxo WhatsApp nao pergunta quantidade.
- O servico de aluguer nao recebe quantidade.
- A migracao de schema remove a coluna antiga `quantidade_contentores` quando ela existir.
- A regra operacional deixa de permitir cadastros agregados no mesmo fluxo.

## Fluxo WhatsApp passo a passo

1. Menu inicial

```text
Ola, sou o Robo de Gestao de Contentores da OLT. O que vamos fazer agora?

1. Cadastrar entrega de contentor
2. Alterar informacoes
3. Excluir pedidos
4. Ver resumo
```

2. Inicio do cadastro

O operador escolhe `1`.

```text
🚛 Qual o numero do contentor?
```

3. Foto do contentor no local

Depois de informar o numero do contentor:

```text
📷 Por favor, envie a foto do contentor no local.
```

4. Localizacao GPS

Depois de enviar a imagem:

```text
📍 Agora, envie a localizacao GPS do local.
```

5. Nome do cliente

```text
👤 Qual o nome do cliente?
```

6. Telefone do cliente

```text
📞 Envie o telefone do cliente.
```

7. E-mail do cliente

```text
✉️ Qual o e-mail do cliente? Voce tambem pode responder Pular.
```

8. Data de entrega

```text
📅 Confirma a entrega para hoje?

1. Sim
2. Outra data
```

Se escolher outra data:

```text
📅 Envie a data de entrega no formato DD/MM ou DD/MM/AAAA.
```

9. Tipo de residuo

```text
🧱 Qual o tipo de residuo?

1. Entulho Limpo
2. Entulho Misto
```

10. Valor do servico

```text
💰 Qual o valor do servico?
```

11. Forma de pagamento

```text
💳 Qual a forma de pagamento?

1. MBWay
2. Transferencia
3. Dinheiro
4. Outro
```

Se escolher `Outro`:

```text
💳 Por favor, digite textualmente a forma de pagamento.
```

12. Status do pagamento

```text
✅ O servico ja esta pago?

1. Pago
2. Pendente
```

13. Confirmacao final

O bot apresenta todos os dados coletados e aguarda a decisao do operador.

## Validacoes por etapa

- Numero do contentor: obrigatorio, 1 a 20 caracteres.
- Foto: exige mensagem do tipo imagem com media valida.
- Localizacao: exige mensagem do tipo localizacao com latitude e longitude.
- Nome do cliente: exige texto entre 3 e 50 caracteres.
- Telefone: normaliza telefone de Portugal e exige numero valido.
- E-mail: aceita e-mail valido ou a resposta `Pular`.
- Data de entrega: aceita hoje ou data manual em `DD/MM` ou `DD/MM/AAAA`.
- Tipo de residuo: aceita apenas `1` para Entulho Limpo ou `2` para Entulho Misto.
- Valor: exige valor numerico valido, com suporte a virgula, ponto e simbolo de euro.
- Forma de pagamento: aceita `1`, `2`, `3`, `4` ou texto livre quando a opcao for Outro.
- Status do pagamento: aceita `1` para Pago ou `2` para Pendente.

## Confirmacao final

Antes de salvar, o bot exibe um resumo com:

- Numero do contentor.
- Cliente.
- Contacto.
- Data de entrega.
- Retirada prevista.
- Tipo de residuo.
- Valor.
- Forma e status do pagamento.

Opcoes da confirmacao:

```text
1. Confirmar e Salvar
2. Corrigir Dados
3. Cancelar Tudo
```

Ao escolher `1`, o cadastro e salvo, a conversa volta para `idle` e o contexto temporario e limpo.

## Correcao de dados

Ao escolher `2. Corrigir Dados`, o bot apresenta o menu de campos corrigiveis:

```text
Qual campo deseja corrigir?

1. Numero do contentor
2. Nome
3. Telefone
4. E-mail
5. Data de entrega
6. Residuo
7. Valor
8. Forma de pagamento
9. Status do pagamento
```

Depois que o operador informa o novo valor, a mesma validacao da etapa original e aplicada. Se o valor for valido, o bot retorna para a confirmacao final com os dados atualizados.

## Cancelamento global

O cancelamento global preserva a capacidade de interromper o fluxo sem salvar dados.

Na confirmacao final, a opcao `3. Cancelar Tudo`:

- limpa o contexto da conversa;
- retorna o estado para `idle`;
- nao grava o aluguer;
- retorna o menu inicial.

Mensagem esperada:

```text
🚫 Cadastro cancelado. Nenhum dado foi salvo.
```

## Timeout

Se o cadastro ficar parado por mais de 30 minutos, o sistema marca a conversa como expirada e pergunta se o operador deseja continuar ou recomecar.

Mensagem:

```text
Vi que voce nao terminou o cadastro do cliente ainda sem nome. Deseja continuar de onde parou?

1. Sim, continuar
2. Nao, recomecar
```

Ao recomecar, o fluxo volta para a primeira pergunta:

```text
🚛 Qual o numero do contentor?
```

## Comandos preservados

Os comandos operacionais existentes foram preservados junto do novo fluxo:

- `resumo`: mostra totais de contentores, alugueres ativos, vencimentos e atrasos.
- `lista`: lista todos os contentores com status.
- `disponiveis`: lista apenas contentores disponiveis.
- `alugados`: lista contentores alugados com cliente, vencimento e status.
- `contentores` ou `status`: lista o status dos contentores.

## Arquivos principais alterados

Principais pontos da implementacao atual do modulo:

- `app/agents/aluguer_agent.py`: fluxo conversacional, estados, validacoes, confirmacao, correcao, cancelamento e timeout.
- `app/agents/whatsapp_router_agent.py`: roteamento do menu, comandos preservados e retomada de conversas.
- `app/models/aluguer.py`: persistencia de `numero_contentor`.
- `app/services/aluguer_service.py`: criacao do aluguer unitario e validacao do numero do contentor.
- `app/core/schema_migrations.py`: inclusao de `numero_contentor` e remocao de `quantidade_contentores` quando presente.
- `tests/test_cadastro_contentor.py`: cobertura do fluxo WhatsApp unitario, validacoes, correcao, cancelamento e timeout.
- `tests/test_services.py`: validacao de obrigatoriedade e limite de `numero_contentor` no servico.
- `tests/test_schema_migrations.py`: cobertura da migracao removendo `quantidade_contentores`.

## Testes finais

Resultado final informado:

```text
101 passed, 1 warning
```

## Commit final

Commit final informado:

```text
6938e36
```
