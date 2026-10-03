# Contrato de herramientas del backend

## Alcance e interfaz

`ToolDispatcher` es una abstracción interna y síncrona, independiente de proveedores.
No añade rutas HTTP, modelos externos, registro de usuarios, dispatch arbitrario
ni lógica de entrega a humanos. Reutiliza `Store.session`, `cards`, `card`,
`movements` y `action`. El Store sigue siendo responsable de propiedad,
elegibilidad, transacciones, verificación de cambios e idempotencia.

La aplicación expone `app.state.tools` cuando dispone de Store; en modo sin datos
es `None`. También se puede construir `ToolDispatcher(store)` dentro de código
backend confiable. El Store debe estar inicializado mediante el ciclo de vida
existente. `catalog()` devuelve nueve nombres estables, descripciones,
`mutating` y esquemas JSON de entrada, sin formato de un proveedor particular.
Modificar el catálogo devuelto no cambia el registro de ejecución.

```python
from factored_bck.tools import ExecutionContext

# El transporte backend obtiene el token de la sesión, fuera del contexto del LLM.
context = ExecutionContext(session_token=backend_session_token)
result = app.state.tools.execute(
    "pause_card",
    {"product_id": selected_product_id, "idempotency_key": stable_operation_key},
    context=context,
)
```

En código async, el host debe ejecutar esta interfaz en su threadpool porque
el Store usa psycopg síncrono. No se debe llamar directamente desde el event loop.

## Autenticación y entradas

Cada invocación, incluso un replay, vuelve a resolver la sesión con `Store.session`.
Una sesión expirada/revocada no puede ejecutar herramientas. No se acepta un
diccionario de principal o `customer_id` suministrado por el llamador. El contexto
contiene solo un token opaco de 1–200 caracteres; su representación y serialización
normal ocultan/excluyen la credencial. Nunca se debe enviar el contexto al LLM ni
registrar el token mediante extracción manual de `SecretStr`.

Las entradas son diccionarios con modelos Pydantic estrictos: campos extra
prohibidos, sin conversión de números a strings o de strings/bools a enteros.
Se rechazan `customer_id`, `session_token`, `action`, nombres de métodos, SQL,
cuerpos anidados y otros campos no declarados. El token viaja solo en `context`,
nunca en los argumentos. Nombre de herramienta: máximo 64 caracteres y miembro
exacto del catálogo; no hay `getattr`, `eval`, imports dinámicos ni registro público.

| Herramienta | Argumentos | Método/acción existente |
| --- | --- | --- |
| `get_cards` | `{}` | `Store.cards` |
| `get_card` | `product_id` | `Store.card` |
| `get_movements` | `product_id`, `limit=50`, `before_date=null` | `Store.movements` |
| `block_card` | `product_id`, `idempotency_key` | `block` |
| `pause_card` | `product_id`, `idempotency_key` | `pause` |
| `reactivate_card` | `product_id`, `idempotency_key` | `reactivate` |
| `activate_card` | `product_id`, `idempotency_key` | `activate` |
| `request_replacement` | `product_id`, `idempotency_key` | `replacement` |
| `register_unrecognized_charge` | `product_id`, `idempotency_key`, `transaction_id`, `process_date` | `unrecognized-charge` |

- `product_id`: string de 1–100 caracteres, con al menos uno no blanco.
- `idempotency_key`: 1–100 caracteres de `[A-Za-z0-9_.:-]`, obligatorio en toda acción.
- `limit`: entero entre 1 y 100; no acepta bool.
- Fechas: strings de exactamente 10 caracteres `YYYY-MM-DD`, con fecha de calendario válida.
- `transaction_id`: string de 1–30 caracteres, con al menos uno no blanco.

`process_date` y `transaction_id` son obligatorios solo para cargo desconocido;
las otras acciones no los aceptan. No se añade paginación a `get_cards`; conserva
el contrato existente. `before_date` mantiene el filtro exclusivo por fecha de
proceso del Store; no garantiza paginación sin saltos entre movimientos del mismo día.

## Resultados y evidencia

Lectura: `{"ok": true, "data": <resultado del Store>}`. Conserva release,
semántica histórica, enmascaramiento y alcance del cliente de la sesión.

Acción confirmada:

```json
{
  "ok": true,
  "data": {
    "verified": true,
    "evidence": {
      "action_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "product_id": "TEAM-CARD-ACTIVE",
      "action": "pause",
      "status": "succeeded",
      "outcome": "state_change_verified",
      "simulator_state": "PAUSED",
      "simulated": true,
      "source_kind": "team_synthetic",
      "release_id": "example-release"
    }
  }
}
```

El dispatcher espera que `Store.action` termine, incluida la confirmación de su
transacción, antes de validar y devolver evidencia. No crea IDs, estados ni éxitos
a partir de intención. Comprueba campos requeridos, `status=succeeded`,
`simulated=true`, coincidencia de producto/acción y resultado esperado. Para
reemplazo/cargo desconocido exige también el `request_id` de negocio existente.
Solo devuelve campos de evidencia permitidos. No reconstruye la máquina de estados
ni consulta otra auditoría: confía en la implementación backend del Store.

Los resultados `replacement_request_registered` y
`request_registered_for_human_review` confirman registro, no emisión, envío,
reembolso, resolución segura ni decisión de fraude. La herramienta de cargo
desconocido reutiliza la acción existente; no implementa entrega a humanos.

La misma clave y payload devuelven la misma evidencia persistida; otra petición
con esa clave genera conflicto. Las claves conservan alcance por cliente y se
comparten con las rutas HTTP existentes. No hay nueva tabla de acciones ni otra
copia de payloads, request IDs o evidencia. El orquestador debe conservar la clave
estable por operación lógica, también cuando cambie de transporte o reintente.

## Errores explícitos

Error: `{"ok": false, "error": {"code": "...", "message": "texto fijo"}}`.
No contiene `data`, evidencia, inputs, mensajes de excepciones, tokens ni detalles SQL.

| Código | Significado |
| --- | --- |
| `unauthenticated` | Contexto ausente/inválido o sesión rechazada (401) |
| `invalid_tool` | Nombre fuera del catálogo |
| `invalid_arguments` | Esquema o validación del Store (422) |
| `not_authorized` | Rechazo 403 |
| `not_found` | Rechazo 404; no distingue inexistente de ajeno |
| `conflict` | Estado no elegible o conflicto de idempotencia (409) |
| `rate_limited` | Rechazo 429 |
| `unavailable` | Backend devuelve 503; ejecución no confirmada |
| `backend_error` | Excepción inesperada u otro error; ejecución no confirmada |
| `unverified_result` | Falta evidencia coincidente y válida; no afirmar éxito |

Un error de conexión/confirmación puede dejar el resultado de una acción desconocido.
No asumir rollback ni generar una clave nueva automáticamente: reintentar la misma
operación con la misma clave cuando corresponda. Una sesión rechazada se evalúa
antes de nombre/argumentos. El dispatcher no registra entradas ni excepciones.
El host futuro debe conservar esta política en sus propios logs/trazas.

## Observabilidad, pruebas y futura integración

Las invocaciones internas no son solicitudes HTTP y no incrementan los contadores
HTTP. Las acciones confirmadas sí aparecen en los agregados existentes de
`simulator.actions`, una vez por evidencia. No se añade un segundo colector.

Las pruebas unitarias verifican catálogo, límites, identidad, dispatch cerrado,
errores seguros y evidencia incompleta. Las pruebas con PostgreSQL desechable
verifican propiedad real, lecturas, seis acciones, rollback por rechazo, replays
entre interfaces y expiración/revocación. El clúster y contrato ETL sintético se
comparten como fixtures con las pruebas de observabilidad; no usan datos existentes.

La futura integración debe proporcionar autenticación de usuario y transporte
confiable del contexto, adaptar los esquemas al proveedor, aplicar confirmación de
intención y política de autorización del producto, conservar claves de idempotencia,
limitar llamadas y ejecutar trabajo síncrono fuera del event loop. Debe tratar
argumentos del modelo como no confiables, evitar enviar credenciales al modelo y
limitar los datos bancarios compartidos conforme al permiso del usuario. Solo puede
anunciar una acción realizada cuando recibe `ok=true`, `verified=true` y evidencia
coincidente, conservando siempre el significado **simulado** y las limitaciones.
