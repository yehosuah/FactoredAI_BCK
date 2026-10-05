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
| Acción o consulta de tarjeta con identificador en el mensaje | `tool_request` de esa herramienta |
| Acción o consulta sin identificador | `get_cards`, para que el cliente vea sus tarjetas |

Si el último mensaje no basta (por ejemplo, solo `DEMO-CARD-001`), el adaptador vuelve a
clasificar los últimos tres mensajes del cliente juntos, para completar el pedido anterior.

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
  autor. Quedan fuera de git; el notebook los lee desde `INTENT_HELDOUT_PATH`.

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
   y la validación cruzada tienen el mismo autor. La prueba independiente de 72 casos está
   pendiente de ejecutar.
2. **Sin doble etiquetado.** No hay medida de acuerdo entre anotadores.
3. **Confunde la dirección de la acción.** Pausar, reactivar y activar comparten raíces;
   los n-gramas no ven la negación ("desactiva... la vuelvo a activar").
4. **Pedidos poco claros:** recall 0.46. El umbral compensa enviando la mitad de los
   mensajes a aclaración, lo que alarga algunas conversaciones.
5. **Hueco del contrato.** El contexto del adaptador solo trae mensajes de usuario y
   asistente, no resultados de herramientas. El adaptador no puede saber el
   `product_id` de una tarjeta listada por `get_cards`; el identificador tiene que llegar
   en el texto del cliente (por ejemplo, insertado por el frontend al elegir una tarjeta).
   Los identificadores se reconocen con el patrón `DEMO-CARD-001`.
6. **Datos sintéticos.** Ningún número describe clientes reales.

## Reproducir

```bash
uv sync --locked --group ml
uv run --locked --group ml python ml/train_intent.py
cd notebooks && uv run --locked --group ml jupyter nbconvert --to notebook --execute --inplace 01_evaluacion_clasificador_intenciones.ipynb
```

El entrenamiento es determinista (semilla fija). La API no depende de scikit-learn: carga
los pesos exportados en `src/factored_bck/intent/intent_model_v1.json` y una prueba
verifica que reproduce las probabilidades de scikit-learn con diferencia menor a 1e-6.
