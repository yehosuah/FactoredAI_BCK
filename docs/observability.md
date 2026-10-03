# Observabilidad operativa del backend

## Medición HTTP

`HttpMetrics` es un registro por instancia de aplicación, protegido por un lock.
El middleware existente conserva su responsabilidad de correlación y errores;
la instrumentación usa únicamente método, plantilla de ruta, clase de estado y
tiempo monotónico. No escribe en PostgreSQL por solicitud.

Los contadores acumulan solicitudes observadas desde la creación del registro.
`total_failures` cuenta respuestas 4xx y 5xx; `status_classes` permite distinguir
errores del cliente de errores del servidor. `error_rate` es una fracción entre
0 y 1, y vale 0 sin observaciones. Redirecciones 3xx no son fallos.

La latencia en milisegundos abarca desde la entrada al middleware hasta que
`call_next` produce las cabeceras de respuesta; no incluye la transmisión del
cuerpo, el trabajo posterior ni la red del cliente. Las respuestas 500 generadas
por el manejo de errores existente también se contabilizan. Una desconexión o
cancelación que impida producir una respuesta puede no contabilizarse.

Se guardan como máximo 1024 duraciones recientes para el total y para cada grupo
método/plantilla. p50 y p95 usan rango más cercano: ordenar y elegir el elemento
`ceil(p * n)`, con posiciones desde 1. Sin muestras, los percentiles son `null`.
Son percentiles de la muestra reciente, no de todo el periodo de los contadores.
Las muestras de cada grupo y del total pueden abarcar periodos distintos.

Hay hasta 128 grupos método/plantilla y un grupo fijo `OTHER / <overflow>` para
excesos. Los métodos no reconocidos se normalizan a `OTHER`. La plantilla procede
de la ruta resuelta por FastAPI/Starlette, nunca de la URL ni sus parámetros.
Las solicitudes sin plantilla disponible —incluidos 404 y algunas rutas de
documentación/redirección— se agrupan bajo `<unmatched>`. Plantillas de más de
256 caracteres también se agrupan allí. Los estados usan seis categorías fijas.

Las respuestas de la ruta resuelta `/operations/metrics` se excluyen de todos
los contadores, incluso cuando falla su autenticación. Una URL distinta que
redirige hacia ella sigue las reglas de rutas sin plantilla. Las rutas de salud
sí cuentan: los sondeos pueden influir en los percentiles globales.

## Privacidad y fallos del colector

El registro no recibe cuerpos, tokens, cabeceras, parámetros de consulta, IDs de
clientes/productos, request IDs ni objetos de solicitud. No registra mensajes de
excepciones del colector. Los fallos de reloj o colección se aíslan del resultado
HTTP original; pueden perderse observaciones. Los contadores son de mejor esfuerzo,
no un registro de auditoría ni una promesa de entrega exacta.

No hay exportador externo, persistencia de métricas HTTP ni coordinación entre
workers. Reiniciar/recrear la aplicación reinicia el registro; distintos workers
devuelven registros independientes. Las instantáneas copian el estado bajo lock
y calculan percentiles fuera del lock.

## Endpoint y alcance autorizado

`GET /operations/metrics` usa la misma sesión bearer expirable y revocable que
`/operations/etl`. Según la decisión del usuario, cualquier sesión válida puede
consultar los agregados HTTP y de acciones **de todos los clientes**. No hay un
rol de operador adicional. No se admiten filtros por cliente ni se devuelven IDs.
La ruta solo existe cuando está habilitada la capa de datos/autenticación.

La respuesta lleva `Cache-Control: no-store` y el `X-Request-ID` existente.
Su cuerpo separa tres campos:

| Campo | Contenido |
| --- | --- |
| `http` | Disponibilidad, alcance, ventana, definición de fallo, método de percentiles, rutas excluidas, contadores globales, latencias y grupos método/plantilla |
| `card_actions` | Disponibilidad, alcance global, fuente, ventana, totales confirmados/exitosos y grupos acción/resultado |
| `limitations` | Límites de interpretación, persistencia, muestreo y coherencia entre las dos fuentes |

Cada grupo HTTP contiene `method`, `route`, `total_requests`, `total_failures`,
`error_rate`, `status_classes` y `latency_ms` (`sample_count`, `p50`, `p95`).
Los mismos contadores y latencias aparecen en el total HTTP. No se devuelven
muestras individuales. `window` explicita inicio/observación, límite de muestras
y capacidad de grupos. No se deben sumar ni promediar percentiles entre workers.

Si falla la instantánea del registro, `http` contiene solo
`{"status":"unavailable","reason":"collection_unavailable"}`. Si falla la consulta
de acciones, `card_actions` contiene solo
`{"status":"unavailable","reason":"aggregation_unavailable"}`. El endpoint conserva
HTTP 200 con las secciones disponibles; una sección ausente no se sustituye por
ceros. Autenticación ocurre antes de estas lecturas y sigue fallando normalmente
si la sesión no es válida o no se puede verificar en PostgreSQL.

## Acciones confirmadas frente a tráfico HTTP

`Store.action_metrics()` hace una consulta agregada a `simulator.actions`, en una
transacción de solo lectura con timeout de sentencia de 2 segundos. La conexión
conserva el timeout existente de 5 segundos. Se consulta al pedir métricas, nunca
al atender cada solicitud bancaria. No se crea otra tabla ni se modifica la
ejecución, evidencia, idempotencia o entrega al humano de acciones.

La consulta agrupa por las seis acciones conocidas y tres resultados conocidos;
valores inesperados se convierten en `other` **en SQL**, antes de devolverlos al
proceso. Solo obtiene etiquetas permitidas y conteos; no trae payloads, resultados
completos, claves de idempotencia, IDs ni mensajes de error.

`total_committed` cuenta todas las filas confirmadas retenidas; `total_succeeded`
cuenta las que declaran `status=succeeded`. Cada grupo presenta `action`, `outcome`,
`committed_count` y `succeeded_count`. Una transacción no confirmada o revertida
no cuenta. Repetir una acción con la misma clave puede incrementar el contador
HTTP, pero no crea evidencia adicional ni incrementa los conteos de acciones.

La implementación actual no persiste los intentos fallidos/rechazados de acciones:
`total_failures` es `null`, no 0. No se puede obtener una tasa de resolución segura
a partir de estas evidencias. Registrar un reemplazo o un cargo desconocido no
afirma emisión, envío, reembolso, adjudicación de fraude ni resolución del problema.

La ventana de acciones es toda la evidencia confirmada todavía retenida, a través
de reinicios y refrescos ETL. No coincide con la ventana HTTP. Ambas instantáneas
son lecturas independientes, no una foto atómica del sistema. La consulta recorre
la evidencia retenida: el timeout limita cada lectura, pero no hay caché ni límite
de frecuencia adicional; este endpoint está pensado para sondeos moderados durante
la evaluación. No es un almacén de observabilidad distribuido.

## Validación y entorno local

Las pruebas HTTP inyectan un reloj determinista y un store controlado. Verifican
conteos, percentiles, privacidad, autenticación, fallos del colector, concurrencia,
capacidad y exclusión del propio endpoint.

`tests/test_action_metrics.py` usa un clúster PostgreSQL desechable en `/tmp`, con
socket Unix privado y TCP deshabilitado. Solo usa datos sintéticos y no se conecta
a bases existentes. Requiere `initdb` y `pg_ctl` en PATH y un usuario no root; de
lo contrario esas pruebas se marcan como omitidas. Verifica SQL real, confirmación,
rollback, replay, revocación/expiración de sesión, lectura sola y timeout. No requiere
Docker Compose. Un sandbox que impida memoria compartida/sockets puede requerir
ejecutar estas pruebas fuera del sandbox.

En el Mac con el archivo editable `.pth` oculto, ejecutar:

```bash
PYTHONPATH="$PWD/src" make check
```

Esto no cambia la arquitectura ni los manifiestos. Una reparación local alternativa
es quitar el flag macOS `hidden` del archivo
`.venv/lib/python3.13/site-packages/_editable_impl_factored_bck.pth` con `chflags nohidden`
y verificar el import sin `PYTHONPATH`; no se aplica como parte de esta función.
