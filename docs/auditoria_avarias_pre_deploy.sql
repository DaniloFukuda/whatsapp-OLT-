-- Consultas somente leitura. Executar manualmente na base correta antes do deploy.
-- Este arquivo não altera dados e não foi executado durante a implementação.

-- Contagens consolidadas de avarias atuais pendentes e resolvidas.
SELECT status_resolucao_avaria, COUNT(*) AS total
FROM pedido_contentores
WHERE contentor_avariado = 1
GROUP BY status_resolucao_avaria;

-- Contagens consolidadas de avarias legadas pendentes e resolvidas.
SELECT status_resolucao_avaria, COUNT(*) AS total
FROM alugueres_contentor
WHERE contentor_avariado = 1
GROUP BY status_resolucao_avaria;

-- Totais separados para facilitar a decisão de desativação.
SELECT
  SUM(CASE WHEN status_resolucao_avaria = 'PENDENTE' THEN 1 ELSE 0 END) AS atuais_pendentes,
  SUM(CASE WHEN status_resolucao_avaria = 'RESOLVIDO' THEN 1 ELSE 0 END) AS atuais_resolvidas
FROM pedido_contentores
WHERE contentor_avariado = 1;

SELECT
  SUM(CASE WHEN status_resolucao_avaria = 'PENDENTE' THEN 1 ELSE 0 END) AS legadas_pendentes,
  SUM(CASE WHEN status_resolucao_avaria = 'RESOLVIDO' THEN 1 ELSE 0 END) AS legadas_resolvidas
FROM alugueres_contentor
WHERE contentor_avariado = 1;

-- Total de conversas atualmente em estados relacionados a avaria.
SELECT COUNT(*) AS conversas_em_estado_de_avaria
FROM conversas_whatsapp
WHERE estado_atual IN (
  'v24_recolha_avaria', 'v24_recolha_relato',
  'recolha_aguardando_triagem_avaria', 'recolha_aguardando_relato_avaria',
  'resolucao_avaria_revisao'
);

-- Totais de itens atuais e legados que conservam relato de avaria.
SELECT
  (SELECT COUNT(*) FROM pedido_contentores WHERE relato_avaria IS NOT NULL AND TRIM(relato_avaria) <> '')
  +
  (SELECT COUNT(*) FROM alugueres_contentor WHERE relato_avaria IS NOT NULL AND TRIM(relato_avaria) <> '')
  AS itens_com_relato;

-- Total de itens com fotos operacionais relacionadas, sem retornar URL de mídia.
SELECT COUNT(DISTINCT origem || ':' || item_id) AS itens_com_fotos
FROM (
  SELECT 'atual' AS origem, pedido_contentor_id AS item_id
  FROM contentor_fotos
  WHERE pedido_contentor_id IS NOT NULL AND tipo_foto = 'RECOLHA'
  UNION ALL
  SELECT 'legado' AS origem, aluguer_id AS item_id
  FROM contentor_fotos_recolha
);

-- Total de pendências que ainda exigem comando de resolução.
SELECT COUNT(*) AS comandos_resolucao_necessarios
FROM (
  SELECT id FROM pedido_contentores
  WHERE contentor_avariado = 1 AND status_resolucao_avaria = 'PENDENTE'
  UNION ALL
  SELECT id FROM alugueres_contentor
  WHERE contentor_avariado = 1 AND status_resolucao_avaria = 'PENDENTE'
);

-- Avarias pendentes (modelo atual e legado).
SELECT id, pedido_id, numero_adesivo_contentor, relato_avaria, status_resolucao_avaria
FROM pedido_contentores
WHERE contentor_avariado = 1 AND status_resolucao_avaria = 'PENDENTE';

SELECT id, numero_contentor, relato_avaria, status_resolucao_avaria
FROM alugueres_contentor
WHERE contentor_avariado = 1 AND status_resolucao_avaria = 'PENDENTE';

-- Conversas atualmente em estados de avaria ou revisão.
SELECT id, SUBSTR(telefone, 1, 3) || '***' || SUBSTR(telefone, -2) AS telefone_mascarado, estado_atual
FROM conversas_whatsapp
WHERE estado_atual IN (
  'v24_recolha_avaria',
  'v24_recolha_relato',
  'recolha_aguardando_triagem_avaria',
  'recolha_aguardando_relato_avaria',
  'resolucao_avaria_revisao'
);

-- Avarias resolvidas e respectivos campos de auditoria.
SELECT id, pedido_id, numero_adesivo_contentor, avaria_estado_anterior,
       avaria_resolvida_em,
       SUBSTR(avaria_resolvida_por, 1, 3) || '***' || SUBSTR(avaria_resolvida_por, -2)
         AS responsavel_mascarado
FROM pedido_contentores
WHERE status_resolucao_avaria = 'RESOLVIDO';

SELECT a.id, a.numero_contentor, a.status_resolucao_avaria,
       e.criado_em, e.tipo AS tipo_evento
FROM alugueres_contentor AS a
LEFT JOIN eventos_aluguer AS e
  ON e.aluguer_id = a.id AND e.tipo = 'pendencia_avaria_resolvida'
WHERE a.status_resolucao_avaria = 'RESOLVIDO';

-- Itens que conservam relato de avaria ou fotos operacionais de recolha.
SELECT id, pedido_id, numero_adesivo_contentor, relato_avaria
FROM pedido_contentores
WHERE relato_avaria IS NOT NULL AND TRIM(relato_avaria) <> '';

SELECT DISTINCT pc.id, pc.pedido_id, pc.numero_adesivo_contentor, cf.tipo_foto
FROM pedido_contentores AS pc
JOIN contentor_fotos AS cf ON cf.pedido_contentor_id = pc.id
WHERE cf.tipo_foto = 'RECOLHA';

SELECT DISTINCT a.id, a.numero_contentor, a.relato_avaria, fr.criado_em AS foto_criada_em
FROM alugueres_contentor AS a
LEFT JOIN contentor_fotos_recolha AS fr ON fr.aluguer_id = a.id
WHERE (a.relato_avaria IS NOT NULL AND TRIM(a.relato_avaria) <> '')
   OR fr.id IS NOT NULL;

-- Comandos de resolução que ainda seriam necessários antes da desativação.
SELECT 'resolver avaria ' || id AS comando
FROM pedido_contentores
WHERE contentor_avariado = 1 AND status_resolucao_avaria = 'PENDENTE'
UNION ALL
SELECT 'resolver avaria ' || id AS comando
FROM alugueres_contentor
WHERE contentor_avariado = 1 AND status_resolucao_avaria = 'PENDENTE';
