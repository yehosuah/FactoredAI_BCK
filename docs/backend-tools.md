# Contrato de herramientas del backend

## Interfaz y autenticación

`app.state.tools` contiene un `ToolDispatcher` síncrono e independiente de proveedores
cuando está habilitado el Store; en modo sin datos es `None`. `catalog()` devuelve
once nombres estables, descripciones, `mutating`, `requires_confirmation` y esquemas
JSON estrictos. Modificar el catálogo no altera el registro privado. No hay dispatch
arbitrario, registro público, `eval`, `getattr` ni endpoint `/tools/execute`.

El transporte confiable entrega `ExecutionContext(session_token=...)`, separado de
argumentos del modelo. Cada invocación revalida la sesión. El contexto oculta su
credencial en repr/serialización; nunca enviarlo al LLM ni registrarlo. La identidad
del cliente viene del backend; los argumentos no admiten `customer_id`. Un host
async debe ejecutar estas llamadas síncronas en un threadpool.

| Herramienta | Argumentos | Resultado |
| --- | --- | --- |
| `get_cards` | `{}` | Tarjetas propias históricas |
| `get_card` | `product_id` | Tarjeta propia y estado simulado |
| `get_movements` | `product_id`, `limit=50`, `before_date=null` | Movimientos históricos propios |
| `block_card` | `product_id`, `idempotency_key` | Preparar confirmación de `block` |
| `pause_card` | `product_id`, `idempotency_key` | Preparar confirmación de `pause` |
| `reactivate_card` | `product_id`, `idempotency_key` | Preparar confirmación de `reactivate` |
| `activate_card` | `product_id`, `idempotency_key` | Preparar confirmación de `activate` |
| `request_replacement` | `product_id`, `idempotency_key` | Preparar confirmación de `replacement` |
| `register_unrecognized_charge` | `product_id`, `idempotency_key`, `transaction_id`, `process_date` | Preparar confirmación de revisión de cargo |
| `create_handoff` | `triage`, `idempotency_key` | Persistir/rutear caso, no confirmar acción bancaria |
| `get_handoff` | `handoff_id` | Estado de caso propio |

Los seis tools de tarjeta **ahora preparan**, sin ejecutar Store.action directamente.
Conservan nombres y argumentos, con `requires_confirmation=true` y descripción
explícita. No existe herramienta para confirmar, cancelar ni ejecutar un comando
pendiente. Los tools de lectura y handoff conservan su comportamiento; triage es el
único objeto anidado permitido. Handoff no ejecuta acciones bancarias.

## Esquemas

Pydantic estricto, campos extra prohibidos, sin coerción de bool/string a enteros.
Producto: 1–100 caracteres no blancos. Clave: 1–100 de `[A-Za-z0-9_.:-]`.
Limit: entero 1–100. Transaction ID: 1–30 caracteres no blancos. Fechas: strings
válidos de exactamente 10 caracteres `YYYY-MM-DD`. Solo cargo desconocido admite
transaction ID y process date, ambos obligatorios. Nombre: máximo 64 caracteres y
miembro exacto del catálogo. No se admiten `confirmed`, `user_confirmed`,
`confirmation_id`, `conversation_id`, action arbitraria, SQL ni identidad.

`get_cards` mantiene su contrato sin nueva paginación; before_date conserva el
filtro exclusivo por fecha de proceso, sin promesa de evitar saltos entre movimientos
del mismo día. Ver [handoffs](human-handoffs.md) para el esquema de triage.

## Resultados y confirmación

Formato común: `{"ok":true,"data":...}`. Las lecturas conservan semántica histórica,
release, enmascaramiento y propiedad. La preparación devuelve `status=pending`,
`confirmation_required=true`, `verified=false`, el comando exacto, ID, snapshot,
creación, vencimiento y política. No devuelve evidencia de ejecución.

```python
from factored_bck.tools import ExecutionContext

result = app.state.tools.execute(
    "pause_card",
    {"product_id": owned_card_id, "idempotency_key": stable_preparation_key},
    context=ExecutionContext(session_token=backend_session_token),
)
# Mostrar el comando guardado al cliente; esta llamada NO pausó la tarjeta.
```

El cliente debe confirmar el ID por el transporte autenticado separado. El modelo
no puede hacerlo por tool dispatch. El backend carga el comando guardado, revalida
sesión/propiedad/estado/expiry, ejecuta la misma identidad idempotente y confirma
atómicamente evidencia y lifecycle. Solo entonces devuelve `verified=true` y un
receipt validado mediante `evidence.verified_action_evidence`, extraído del validator
anterior de ToolDispatcher. Flags, prose o una solicitud reemplazada no autorizan.

El replay de preparación conserva el mismo ID/expiry y devuelve su estado actual,
que puede ser executed si el cliente ya confirmó. Esa evidencia es histórica;
no representa una nueva acción ni renueva autoridad. Reutilizar una clave con otro
comando falla. Para reglas de expiry, revisión ABA, concurrencia, HTTP y futura
conversación, ver [action-confirmation.md](action-confirmation.md).

`replacement_request_registered` y `request_registered_for_human_review` confirman
registro simulado, nunca reembolso, emisión, envío ni decisión de fraude.
`create_handoff` devuelve `persisted=true` para registro del caso: queued/assigned
no significa aceptado ni resuelto. Ninguna intención solicitada es evidencia.

## Errores y observabilidad

Error: `{"ok":false,"error":{"code":"...","message":"texto fijo"}}`, sin data,
inputs, evidencia, credenciales, SQL ni excepción interna.

| Código | Significado |
| --- | --- |
| unauthenticated | Contexto o sesión inválida (401) |
| invalid_tool | Nombre fuera del catálogo |
| invalid_arguments | Esquema inválido (422) |
| not_authorized | Rechazo 403 |
| not_found | Recurso inexistente o ajeno (404) |
| conflict | Elegibilidad o idempotencia incompatible (409) |
| rate_limited | Rechazo 429 |
| unavailable | Backend no disponible (503) |
| backend_error | Error inesperado; no afirmar ejecución |

No se registran argumentos ni mensajes de excepción. Invocaciones internas no
incrementan métricas HTTP. Preparaciones no incrementan simulator.actions; solo
acciones confirmadas y comprometidas aparecen en métricas/handoff como evidencia.
Una respuesta perdida no prueba rollback: consultar/reintentar el mismo ID/clave.

El futuro orquestador debe conservar claves e IDs, validar outputs estructurados,
mantener tokens fuera de prompts y separar eventos explícitos del cliente de texto
del modelo. No darle acceso HTTP arbitrario ni registrar confirm como tool. La
conversación/UI y el proveedor no están implementados; no se afirma P05 completo.

P04's conversation host may supply a trusted `conversation_id` and shared database
connection to `execute`; neither is an adapter/tool argument or catalogue field.
This binds prepared confirmations and handoffs to the owned conversation and commits
them with the turn. Standalone callers keep their existing transaction behavior.
No confirm/cancel/recover tool is added. See [conversation contract](conversations.md).
