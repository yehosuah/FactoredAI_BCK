# Enrutador de intenciones de tarjetas

Componente aprendido del asistente: un clasificador entrenado que identifica qué quiere el
cliente y una política determinista que convierte esa predicción en una propuesta del
contrato `conversation-adapter-v1`. Se activa con `BCK_CONVERSATION_ADAPTER=classifier`.

Todo resultado de este documento es una **evaluación fuera de línea sobre datos
sintéticos del equipo**. No es una medición en producción.

## Cómo funciona

```text
mensaje del cliente
  -> clasificador (TF-IDF de caracteres + regresión logística, 10 intenciones)
  -> política determinista (umbral, prioridad pérdida/robo, escalamiento)
  -> propuesta no confiable: answer | clarification | tool_request | human_handoff
  -> el backend valida, revisa propiedad y elegibilidad, y pide confirmación
```

| Situación | Propuesta |
| --- | --- |
| Confianza menor a 0.6 | Aclaración con las dos intenciones más probables; pérdida o robo primero si está entre ellas |
| Pedido poco claro o fuera de alcance | Aclaración con lo que el asistente puede hacer |
| Dos aclaraciones seguidas sin resolver | `human_handoff`, motivo `card_support`, severidad baja |
| Cargo no reconocido | `human_handoff`, motivo `fraud`, especialidad `Fraudes`, severidad alta |
| Acción o consulta con tarjeta seleccionada validada o identificador en el mensaje | `tool_request` de esa herramienta |
| Acción o consulta sin identificador | `get_cards`, para que el cliente vea sus tarjetas |

El contexto interno incluye observaciones mínimas de resultados backend persistidos:
herramienta, estado de lectura/preparación/handoff/fallo y hasta 20 identificadores de
las tarjetas listadas. No recibe saldos, contactos, recibos completos, cuentas,
credenciales ni conexiones. Las observaciones son contexto histórico, no autorización.

- `selected_product_id` permite usar la tarjeta elegida en la interfaz sin modificar
  el texto del cliente; el backend valida propiedad y fija el release. Referencias
  contradictorias piden aclaración y no eligen otro objetivo silenciosamente.
- Un identificador real de una tarjeta listada, incluso sin el patrón DEMO-CARD-001,
  completa el pedido que obtuvo ese listado.
- Una respuesta ordinal ("la primera", "2", "a segunda") elige una opción de la
  aclaración o una tarjeta del listado real; las posiciones se conservan aun si
  un identificador no cabe en el contrato de herramientas.
- Los mensajes pendientes se agregan solo después del último resultado backend.
  Un mensaje clasificado con confianza no prueba que la herramienta haya funcionado.
- Un "ok" después de preparar un comando no lo confirma ni lo repite.
- Un fallo no se transforma en éxito supuesto. Reintentos usan las claves persistidas.
- Los identificadores conocidos se quitan del texto antes de clasificar; una selección
  ambigua vuelve al listado en vez de elegir una tarjeta arbitraria.

La política conserva prioridad de pérdida/robo cuando el cliente lo informa explícitamente,
incluso si también menciona un cargo no reconocido. Un pedido de bloqueo sin motivo
pregunta si se trata de pérdida/robo o pausa temporal. Los pedidos de aumentar el
límite van a soporte humano: el simulador solo permite consultar ese valor.
Son guardas de capacidad/riesgo derivadas del contrato, no reentrenamiento con el
corpus borrador. Las reglas lexicales son conservadoras y no cubren toda ambigüedad.

El modelo nunca decide autorización, elegibilidad ni el éxito de una acción: eso lo hace
el backend. Las respuestas son plantillas fijas en español y portugués; no hay un LLM
generando texto.

## Datos

- **Entrenamiento:** `ml/datasets/intent_train_v1.jsonl`, 280 casos sintéticos del equipo
  (10 intenciones x 2 idiomas x 14), taxonomía `card-routing-v1@0.1.0-draft` del ETL.
  El portugués se escribió aparte, no como traducción. Cada caso tiene su familia de fuga.
- **No se usan las transcripciones del organizador:** 171,321 filas con solo 42 textos de
  cliente distintos, según el perfil del ETL. Entrenar ahí mediría plantillas, no clientes.
- **Prueba independiente:** los 72 casos borrador del paquete del ETL, escritos por otro
  autor. Quedan fuera de git; el notebook los lee desde `INTENT_HELDOUT_PATH`. El loader
  admite `candidate_intents` del contrato ETL. Sus anotaciones permanecen borrador.

## Resultados (validación cruzada de 5 particiones, agrupada por familia)

| Sistema | n | Exactitud [IC 95%] | Macro F1 [IC 95%] |
| --- | ---: | --- | --- |
| Línea base de palabras clave | 280 | 0.589 [0.529, 0.643] | 0.612 [0.552, 0.661] |
| Clasificador | 280 | 0.746 [0.693, 0.796] | 0.740 [0.683, 0.786] |

- McNemar exacto p = 0.00002 (el modelo acierta solo en 73 casos, la línea base solo en 29).
- Por idioma, macro F1 del modelo: español 0.752, portugués 0.728 (n = 140 cada uno).
- Calibración: ECE 0.156; el modelo es subconfiado.
- Umbral 0.6: automatiza el 50% de los mensajes con 93.6% de exactitud en ese grupo.
- Latencia de inferencia: 0.06 ms p50 y 0.08 ms p95 por mensaje, en proceso, sin red.

Detalle, gráficos y errores en
[`notebooks/01_evaluacion_clasificador_intenciones.ipynb`](../notebooks/01_evaluacion_clasificador_intenciones.ipynb).

## Limitaciones

1. **Evidencia optimista.** Los datos de entrenamiento, las palabras clave de la línea base
   y la validación cruzada tienen el mismo autor. El diagnóstico de 72 casos se ejecutó, pero sus anotaciones, idioma y rutas
   aún no están revisados; no es un benchmark independiente aceptado.
2. **Sesgo de selección.** `C` y el umbral 0.6 se eligieron sobre las mismas predicciones
   fuera de partición que se reportan, lo que también infla un poco los números.
3. **Agrupación sin efecto.** Cada caso de entrenamiento es su propia familia, así que la
   agrupación por familia no protege contra paráfrasis cercanas; solo el control de texto
   duplicado lo hace.
4. **Sin doble etiquetado.** No hay medida de acuerdo entre anotadores.
5. **Confunde la dirección de la acción.** Pausar, reactivar y activar comparten raíces.
   Se reprodujeron errores de reactivación ante pedidos cortos de pausa en ambos
   idiomas. La política ahora pide aclaración si verbos explícitos contradicen la
   dirección predicha o hay negación ante una intención mutante. Es una guarda
   conservadora, no un parser semántico completo. No modifica pesos ni métricas.
6. **Pedidos poco claros:** recall 0.46. El umbral compensa enviando la mitad de los
   mensajes a aclaración, lo que alarga algunas conversaciones.
7. **Contexto limitado.** Observaciones backend acotadas permiten seleccionar una
   tarjeta realmente listada sin inferir éxito. Solo se incluyen los últimos 20
   mensajes y hasta 20 IDs por listado; no se revela el resultado financiero completo.
   La autorización y vigencia del release se revalidan al invocar la herramienta.
8. **Datos sintéticos.** Ningún número describe clientes reales.

## Diagnóstico del paquete ETL borrador (5 de octubre)

El manifest identifica `draft_review_only`; las 72 anotaciones son `draft`. Se verificó
SHA-256 `4fb6cbcbb9153bafcaf9dd701824c3e2ff1cdc05d920e1790e6f3cd6255f2289` y el conteo.
No hay textos normalizados ni familias declaradas compartidas con entrenamiento.
No se reentrenó ni ajustó el umbral con estos casos. La medida usa la primera intención
candidata (pérdida/robo primero); no certifica rutas de múltiples intenciones.

| Medida borrador | Clasificador | Palabras clave |
| --- | --- | --- |
| Exactitud de intención primaria, 72 casos | 0.847 | 0.639 |
| Macro F1 | 0.847 | 0.636 |

Con el umbral ya fijado en 0.6, 58/72 casos (80.6%) superan el umbral y 53/58 (91.4%)
tienen la intención primaria candidata correcta. Dos predicciones por encima del
umbral son `unsupported_unclear`, que la política no automatiza: 56/72 son elegibles
por clase y umbral antes de las guardas nuevas, y 54/56 coinciden con alguna intención
candidata. Esa compatibilidad tampoco mide seguridad de ejecución. Por idioma: exactitud es 0.833 en
español y 0.861 en portugués (36 casos cada uno). Son diagnósticos sintéticos con
etiquetas no revisadas, no promesas de calidad ni autorización automática.
Los cinco errores por encima del umbral muestran por qué la propuesta y la evidencia
se mantienen separadas y las acciones requieren confirmación explícita.

La carga valida taxonomía, dimensiones, índices y pesos numéricos finitos antes de
servir; un artefacto inválido falla al arrancar, sin fallback silencioso al stub.

## Reproducir

```bash
uv sync --locked --group ml
uv run --locked --group ml python ml/train_intent.py
cd notebooks && uv run --locked --group ml jupyter nbconvert --to notebook --execute --inplace 01_evaluacion_clasificador_intenciones.ipynb
```

El entrenamiento es determinista (semilla fija). La API no depende de scikit-learn: carga
los pesos exportados en `src/factored_bck/intent/intent_model_v1.json` y una prueba
verifica que reproduce las probabilidades de scikit-learn con diferencia menor a 1e-6.
