# FactoredAI_BCK
backend Repo for factored AI

## Alcance

Repositorio independiente del backend (BCK). El ETL se desarrolla en otro repositorio:
extrae, transforma y entrega datos; el backend consume esas entregas mediante contratos acordados.
Cada repositorio mantiene su propio código, entorno, pruebas, tickets y decisiones.

La base ejecutable usa Python 3.13, FastAPI y uv, con entorno propio. Incluye salud,
configuración y manejo de errores; todavía no implementa un flujo bancario, autenticación,
persistencia ni conexiones al ETL. El entorno del ETL no es un requisito de este BCK.

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

Las respuestas de salud incluyen `service` y `version`. No hay dependencias externas:
`ready` no acredita disponibilidad, frescura ni calidad de los datos del ETL.
Al integrar una dependencia requerida se deberá ampliar readiness y devolver 503 si no está disponible.

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
La implementación y la integración con el ETL se acuerdan como siguiente trabajo, mediante
contratos de datos independientes de la ubicación de ambos checkouts.

## Configuración de desarrollo

- [AGENTS.md](AGENTS.md): instrucciones de trabajo y alcance del backend.
- [Gestor de issues](docs/agents/gestor-de-issues.md): tickets locales en `.scratch/`.
- [Dominio](docs/agents/dominio.md): convenciones del glosario y decisiones.
- [CONTEXT.md](CONTEXT.md): glosario del backend.
- [Decisiones de arquitectura](docs/adr/README.md): registro propio en `docs/adr/`.

`escribir-spec` y `partir-en-tickets` leerán esta configuración.
Puedes editar `docs/agents/*.md` directamente. Vuelve a ejecutar `configurar-desarrollo`
solo cuando cambies de gestor de issues.
