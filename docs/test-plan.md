# Plano de Testes do Vision Stability Lab

## Testes de Longa Duração e Estabilidade
- **Baseline (30 min)**: Validação inicial do consumo de RSS e estabilidade do pipeline.
  - *Status 2026-08-23:* a primeira execução foi válida apenas para RTSP/decode (sem `nvinfer`, depayloader H.264 incorreto — ver INC-001/INC-002). Reexecução pendente com `deepstream/pipeline_real.py` (inferência + pyds).
- **Operação Contínua (6 h)**: Verificação de vazamento de memória (Memory Growth) e estabilidade de threads e fds. *Não iniciado.*
- **Resiliência RTSP (30 min)**: Teste de desconexão e reconexão automática da fonte de vídeo. *Fallback UDP→TCP observado incidentalmente no teste original; teste dedicado pendente.*
- **Sobrecarga (2 h)**: Validação do comportamento das filas com múltiplos objetos simultâneos. *Não iniciado.*
