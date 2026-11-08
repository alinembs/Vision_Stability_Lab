# Graphify - Context Optimization

## Objetivo
Utilizar o Graphify como primeira camada de investigação
da arquitetura e das dependências do projeto.

## Regras obrigatórias

Antes de investigar uma tarefa:

1. Consulte o grafo antes de realizar buscas extensivas.
2. Utilize graphify query para localizar módulos relevantes.
3. Utilize graphify path para investigar dependências.
4. Utilize graphify explain para compreender componentes.
5. Leia os arquivos originais somente após identificar
   os pontos relevantes no grafo.

## Otimização de tokens

- Não leia GRAPH_REPORT.md integralmente em tarefas pontuais.
- Não faça leitura indiscriminada de diretórios.
- Não carregue arquivos sem relação com a tarefa.
- Evite repetir consultas já respondidas na sessão.
- Recupere somente os trechos necessários.
- Prefira consultas específicas em vez de consultas genéricas.

## Precisão

- O grafo não substitui o código-fonte.
- Confirme assinaturas e implementações nos arquivos reais.
- Verifique testes e contratos antes de modificar código.
- Se o grafo estiver desatualizado, atualize-o.
- Nunca invente dependências com base apenas em inferências.

## Fluxo de execução

1. Entender a tarefa.
2. Consultar o Graphify.
3. Identificar arquivos e dependências.
4. Ler somente os arquivos necessários.
5. Implementar.
6. Executar testes.
7. Atualizar o grafo quando necessário.
