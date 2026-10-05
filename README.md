# FactoredAI_BCK
backend Repo for factored AI

## Alcance

Repositorio independiente del backend (BCK). El ETL se desarrolla en otro repositorio:
extrae, transforma y entrega datos; el backend consume esas entregas mediante contratos acordados.
Cada repositorio mantiene su propio código, entorno, pruebas, tickets y decisiones.

El backend usa Python 3.13, FastAPI y uv, con entorno propio. Incluye salud,
configuración y manejo de errores, y un prototipo autenticado de soporte de tarjetas
que consume entregas versionadas en PostgreSQL. Las acciones son simuladas y persisten
en un esquema propio; una actualización del ETL no reemplaza su estado. El entorno
del ETL no es un requisito de este BCK.

## Ejecución local

Requisito: [uv](https://docs.astral.sh/uv/getting-started/installation/) en el PATH.
Ejecuta estos comandos desde la raíz de `FactoredAI_BCK`, no desde el repositorio padre:

```bash
uv sync --locked
uv run --locked uvicorn factored_bck.app:create_app --factory --reload --host 127.0.0.1 --port 8000
```

También puedes usar `make setup` y `make dev`. uv crea `.venv/` con Python 3.13.14 y las
dependencias de `uv.lock`; si falta Python, puede descargarlo. La primera instalación necesita red.
No se necesitan credenciales ni datos bancarios para iniciar esta base.

Con el servidor activo:

```bash
curl -f http://127.0.0.1:8000/health/live
curl -f http://127.0.0.1:8000/health/ready
```

La documentación interactiva está en [Swagger UI](http://127.0.0.1:8000/docs).
Detén el servidor local con Ctrl+C.

## Docker

Requisitos: Docker con su motor activo y Docker Compose.

```bash
docker compose up --build -d --wait
curl -f http://127.0.0.1:8000/health/ready
docker compose down
```

Equivalentes: `make docker-up` y `make docker-down`. El contenedor corre como usuario
sin privilegios, instala solo dependencias de ejecución y expone el puerto únicamente
en la interfaz local del host. El contexto de construcción incluye solo manifiestos,
README y código; no incorpora `.env`, `.git`, el ETL ni la carpeta preexistente `Untitled/`.

El puerto 8000 no puede usarse simultáneamente para el servidor local y Compose.
Para usar otro puerto publicado: `BCK_PORT=8001 docker compose up --build -d --wait`.
La aplicación dentro del contenedor sigue escuchando en el puerto 8000.

## Configuración

Los valores por defecto permiten iniciar sin `.env`. Si necesitas modificarlos, copia
`.env.example` a `.env` y edítalo. Las variables del proceso tienen precedencia sobre `.env`.
No versiones `.env` ni secretos.

| Variable | Valor inicial | Uso |
| --- | --- | --- |
| `BCK_APP_NAME` | `Factored AI Backend` | Nombre del servicio, de 1 a 100 caracteres |
| `BCK_ENVIRONMENT` | `development` | `development`, `test` o `production` |
| `BCK_ENABLE_DOCS` | `true` | Activa `/docs` y `/openapi.json` |
| `BCK_PORT` | `8000` | Puerto del host para Compose; localmente usa `--port` en uvicorn |

Una configuración inválida de la aplicación impide el arranque. `BCK_ENVIRONMENT` es una
etiqueta de entorno: elegir `production` no agrega autenticación, TLS ni políticas operativas.
Desactivar documentación requiere `BCK_ENABLE_DOCS=false`.

## Contrato HTTP inicial

| Ruta | Resultado |
| --- | --- |
| `GET /health/live` | HTTP 200: proceso sirviendo, `status: ok` |
| `GET /health/ready` | HTTP 200: base inicial disponible, `status: ready` |
| `GET /openapi.json` | Esquema de la API, cuando la documentación está habilitada |

Las respuestas de salud incluyen `service` y `version`. Con `BCK_DATA_ENABLED=false`
se comprueba únicamente la base HTTP. Con datos habilitados, `ready` comprueba la
entrega PostgreSQL y su versión de contrato; devuelve 503 si falta o es incompatible.
La frescura y el resultado de la ejecución se consultan en `/operations/etl`.

Cada respuesta lleva un `X-Request-ID` generado por el servidor. Los errores HTTP, de validación
y no controlados tienen la forma:

```json
{"error": {"code": "http_404", "message": "Resource not found", "request_id": "..."}}
```

Se conservan el código HTTP y cabeceras necesarias como `Allow`.
Los errores 422 no devuelven el input recibido y los 500 no exponen mensajes internos ni trazas.
El registro de errores de aplicación incluye el ID y tipo de excepción, sin payloads.
El contenedor desactiva access logs; el modo local usa los logs de uvicorn para desarrollo.

## Comprobaciones y estructura

```bash
make check
# Sin make:
uv lock --check
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
uv run --locked pytest
```

Las pruebas verifican salud, configuración y precedencia de variables, rechazo de configuración
inválida, errores 404/405/422/500, correlación por solicitud y control de documentación.
Las rutas que fuerzan errores existen solo en pruebas, no en la aplicación ejecutable.

Las regresiones de rotación y paginación usan PostgreSQL desechable con datos del
equipo, socket Unix privado y TCP deshabilitado. Requieren `initdb` y `pg_ctl` en
PATH; si faltan, pytest las omite. En macOS con PostgreSQL de Homebrew:

```bash
PATH="/opt/homebrew/opt/postgresql@18/bin:$PATH" PYTHONPATH=src make check
```

`PYTHONPATH=src` permite importar el checkout aunque macOS oculte el archivo editable
`.pth` del entorno local. La suite no se conecta a una base existente.

## Integración de soporte de tarjetas

La API de datos se habilita con `BCK_DATA_ENABLED=true`. Configura `BCK_DB_HOST`,
`BCK_DB_PORT`, `BCK_DB_NAME`, `BCK_DB_USER` y `BCK_DB_PASSWORD_FILE` con el rol propio
del backend. La base debe contener el esquema `simulator` propiedad del backend y
una entrega completa aceptada bajo el contrato `card-support-etl-v1` en `bank`.
Readiness devuelve 503 hasta que haya una entrega disponible. La base HTTP sola
conserva sus rutas de salud cuando `BCK_DATA_ENABLED=false`.

Con datos habilitados, readiness también verifica los permisos por columnas del
handoff. Aplica `deploy/handoff-read-grants.sql` como administrador después de
crear las tablas ETL, indicando el `BCK_DB_USER` real mediante la configuración
de sesión `factored_bck.backend_role` ([ejemplo seguro](docs/human-handoffs.md#deployment)).
El script rechaza un lector LOGIN y destinos ausentes o inválidos antes de conceder
permisos. Luego ejecuta `python -m factored_bck.handoff_admin check` bajo
el rol real del backend. No se concede lectura de contactos ni escritura en `bank`.

`BCK_DEMO_PASSWORD_FILE` crea un usuario `demo` y tarjetas ficticias identificadas
como `team_synthetic`; no activa acceso a clientes del organizador. Para crear una
cuenta de prueba asociada a un cliente de la entrega aceptada:

```bash
uv run --locked factored-provision --username <test-user> \
  --customer-id <accepted-customer> --password-file /private/path/test-password
```

El archivo debe contener una contraseña de al menos 12 caracteres. Nunca uses una
contraseña como argumento ni registres tokens. El aprovisionamiento es administrativo
local; no hay registro público basado únicamente en un ID de cliente.

Al reiniciar con una contraseña demo diferente (12–200 caracteres), se actualiza el
hash y se revocan las sesiones demo en una sola transacción. Reiniciar con la misma
contraseña conserva sus sesiones; la rotación no reinicia estados ni auditoría.

Antes de verificar contraseñas, el login admite como máximo una verificación
simultánea por base PostgreSQL mediante un lock transaccional no bloqueante.
Las ventanas persistentes de cinco minutos permiten 30 intentos por peer,
además del límite de 10 fallos por usuario/peer. Cambiar el nombre de usuario no
evita el límite peer; login exitoso no borra ese presupuesto. No hay un contador
temporal compartido que permita a unos peers bloquear otros durante cinco minutos.
El límite global es de concurrencia: scrypt solo ocupa un slot a la vez y el lock
se libera automáticamente al finalizar la transacción; slot ocupado devuelve 429
con `Retry-After: 1`. Rotar peers no permite ejecutar KDF simultáneos.
Saturación devuelve 429 antes de consultar usuarios o ejecutar scrypt, tanto para
cuentas existentes como inexistentes. El peer es la dirección que ve el servidor;
tras un proxy compartido se comparte ese presupuesto. Son límites conservadores
del simulador, no una política de disponibilidad para producción.

| Ruta | Comportamiento |
| --- | --- |
| `POST /auth/login`, `POST /auth/logout` | Sesión opaca, expirable y revocable de prueba |
| `GET /me`, `GET /me/cards`, `GET /me/cards/{id}` | Identidad de sesión y tarjetas del cliente autenticado |
| `GET /me/cards/{id}/movements` | Movimientos históricos propios, con límite y fecha de proceso |
| `POST /me/cards/{id}/actions` | Prepara confirmación de una acción; no ejecuta inmediatamente |
| `GET /me/handoff` | Evidencia de acciones simuladas confirmadas y limitaciones |
| `GET /operations/etl` | Estado agregado de la entrega y ejecución ETL |
| `GET /operations/metrics` | Métricas globales; requiere credencial de operador independiente |

Las acciones requieren preparación con `Idempotency-Key` y una confirmación
explícita separada del cliente. Los seis tools de tarjeta y la ruta `/actions`
solo preparan un comando persistido; no ejecutan la intención del modelo.
`POST /me/action-confirmations/{id}/confirm` carga y ejecuta el comando exacto tras
revalidar sesión, propiedad, vencimiento y estado. Cancelar impide ejecución futura.
Solo el resultado confirmado devuelve evidencia `verified=true`; las herramientas
siguen siendo simuladas. Ver [contrato de confirmación](docs/action-confirmation.md).

No ejecuta pagos, reembolsos, adjudicación de fraude, emisión ni envío. Los valores
del organizador son históricos y los números de producto están enmascarados.
Las conversaciones persistentes y el adaptador inyectable se describen abajo.
El modo stub es explícito; proveedor ML real e interfaz navegador se integran
por sus propietarios. Esta entrega no certifica P05 completo.

Los movimientos devuelven `next_cursor` (null al terminar). Para continuar, envíalo
como `cursor` conservando la misma tarjeta y sesión; el límite puede cambiar.
El cursor recorre `process_date DESC, transaction_date DESC, transaction_id ASC`,
incluyendo movimientos con la misma fecha y hora. `before_date` sigue siendo un
filtro exclusivo opcional en la primera página; el cursor conserva ese filtro.
Un cursor inválido o de otra tarjeta/cliente devuelve 422; cambiar el filtro devuelve
422. Si el ETL cambia la entrega aceptada, continuar devuelve 409: empieza una nueva
lectura para evitar mezclar versiones históricas. El cursor es continuación, no una
credencial; cada consulta verifica la sesión y propiedad y usa parámetros SQL.

Este checkout tiene su propio Dockerfile y Compose para la base HTTP. El Compose
integrado se encuentra en la raíz del workspace ETL; construye este repositorio
como contexto independiente y configura PostgreSQL y secretos en ejecución.

## Observabilidad operativa

`GET /operations/metrics` separa `http`, `card_actions`, `handoffs` y `limitations`. Toda
sesión de cliente queda denegada. Configura `BCK_METRICS_TOKEN_FILE` con un secreto
independiente de operador (32–200 caracteres ASCII alfanuméricos, `_` o `-`;
se recomienda generar 32 bytes aleatorios mediante `secrets.token_urlsafe(32)`).
La consulta usa ese secreto como bearer; sin configuración queda denegada. El
archivo se relee por consulta: rotarlo revoca el secreto anterior inmediatamente;
si falta o es inválido se falla cerrado con 503 sin consultar agregados. Mantén
el archivo privado y entrega el secreto solo a operadores, nunca a clientes/modelos.
HTTP incluye conteos por método/plantilla y clase de estado, fallos 4xx/5xx, tasa
de error y p50/p95 en milisegundos de hasta 1024 muestras recientes por grupo.
Los contadores duran la vida de la instancia de aplicación; no se comparten entre
workers. Las métricas excluyen su propia ruta y no almacenan cuerpos, tokens,
URLs crudas ni identificadores. Un fallo del colector no altera la respuesta bancaria.

Las acciones se agregan desde la evidencia ya confirmada en PostgreSQL, sin duplicarla.
Los replays idempotentes no aumentan su conteo; los intentos fallidos no están
persistidos y su total se informa como `null`. Éxito HTTP o simulado no implica
resolución segura. Consulta [mediciones, ventanas, privacidad y pruebas](docs/observability.md)
para interpretar la respuesta y las limitaciones de esta implementación.

## Herramientas internas para un futuro orquestador

`app.state.tools` ofrece un `ToolDispatcher` interno cuando está habilitado el Store.
Su catálogo fijo contiene `get_cards`, `get_card`, `get_movements`, `block_card`,
`pause_card`, `reactivate_card`, `activate_card`, `request_replacement` y
`register_unrecognized_charge`, además de `create_handoff` y `get_handoff`. Revalida la sesión en cada llamada, rechaza
`customer_id` en argumentos y reutiliza la propiedad, idempotencia y evidencia del
Store. Las acciones preparan confirmaciones; solo una confirmación explícita
del cliente por el transporte separado puede ejecutarlas. No se añade un endpoint HTTP de herramientas ni un proveedor LLM.
Ver [contrato, esquemas, errores y responsabilidades del orquestador](docs/backend-tools.md).

```text
src/factored_bck/  Aplicación y configuración
tests/            Pruebas del BCK
pyproject.toml    Dependencias propias
uv.lock           Versiones resueltas
Dockerfile        Imagen de ejecución
compose.yaml      Arranque local en contenedor
docs/agents/      Configuración de skills
docs/adr/         Decisiones de arquitectura
.scratch/         Tickets locales
```

La carpeta preexistente `Untitled/` no participa en instalación, pruebas o construcción Docker.
La integración consume el contrato de datos versionado, independiente de la ubicación
de ambos checkouts.

## Configuración de desarrollo

- [AGENTS.md](AGENTS.md): instrucciones de trabajo y alcance del backend.
- [Gestor de issues](docs/agents/gestor-de-issues.md): tickets locales en `.scratch/`.
- [Dominio](docs/agents/dominio.md): convenciones del glosario y decisiones.
- [CONTEXT.md](CONTEXT.md): glosario del backend.
- [Decisiones de arquitectura](docs/adr/README.md): registro propio en `docs/adr/`.

`escribir-spec` y `partir-en-tickets` leerán esta configuración.
Puedes editar `docs/agents/*.md` directamente. Vuelve a ejecutar `configurar-desarrollo`
solo cuando cambies de gestor de issues.

## Handoffs humanos persistentes

El backend persiste triage, evidencia propia y asignación determinista a agentes
Digital/Hybrid aprovisionados. Sesiones de cliente y agente son independientes.
`POST/GET /me/handoffs` y las rutas `/agent/handoffs` comparten el módulo con las
herramientas internas. `GET /me/handoff` conserva su contrato de historial.
Asignado no significa disponible, aceptado ni resuelto.

El login de agente comparte los límites peer y de concurrencia de verificación del
cliente. Crear/cancelar/aceptar/resolver revalida la sesión dentro de su transacción.
El administrador local puede recuperar casos assigned/accepted con agente no
elegible mediante `python -m factored_bck.handoff_admin recover --handoff-id <id>`:
reutiliza el router, conserva evidencia y registra la historia previa en una auditoría.
No existe transporte público ni tool de recuperación; casos terminales no se reabren.

Consulta [contrato, política, endpoints y despliegue](docs/human-handoffs.md) y
[datos reales inspeccionados](docs/service-agents-audit.md). El despliegue requiere
permisos de lectura por columnas y cuentas de agentes; el chequeo de desarrollo
no sustituye ese preflight. Senior para critical está deshabilitado por defecto.

`BCK_CONFIRMATION_SECONDS` controla el TTL de confirmación (300 por defecto,
30–900 segundos). El despliegue debe retirar workers antiguos para evitar rutas
de ejecución inmediata. La migración de simulator es aditiva al arrancar.

## Conversaciones persistentes (P04)

`POST /me/conversations`, `GET /me/conversations/{id}` y
`POST /me/conversations/{id}/turns` conservan conversaciones autenticadas ES/PT y
sus eventos ordenados. Creación y turnos requieren `Idempotency-Key`. El adaptador
ML se inyecta en `create_app`; sus propuestas se validan y nunca confirman acciones.
Los tools mutantes preparan confirmaciones vinculadas a la conversación. Reconnect
incorpora estados y evidencia verificada de confirmaciones y handoffs propios.

El adaptador está deshabilitado por defecto. `BCK_CONVERSATION_ADAPTER=stub` habilita
un stub determinista identificado como prueba y `BCK_CONVERSATION_ADAPTER=classifier` el
clasificador de intenciones entrenado ([detalle y resultados](docs/ml-intent-router.md));
`BCK_ADAPTER_TIMEOUT_SECONDS` limita su espera (5 segundos por defecto). No incluye frontend. Consulta
[contrato, comandos de prueba y dependencias P00/P01](docs/conversations.md).

## Procedencia de fixtures publicados

Las tarjetas de `bank.products` derivan su procedencia del manifest del release.
`team_generated_fixture`/`team_synthetic` producen `team_synthetic` en tarjeta,
confirmación y recibo, con importes etiquetados `team_fixture`.
`organizer_synthetic` y releases históricos sin ese campo conservan la semántica
histórica. Un valor explícito null o desconocido falla cerrado con 503 antes de preparar
o ejecutar. No se infiere procedencia desde texto del cliente/adaptador.

El aprovisionamiento de clientes aplica la misma procedencia del manifest y fija
el release aceptado hasta el commit. Las contraseñas de prueba tienen 12–200
caracteres y se leen de un archivo privado; no se imprimen.
