# Instrucciones del backend

## Alcance

Este repositorio contiene el backend (BCK) del proyecto Factored AI.
El ETL se desarrolla en un repositorio separado y entrega datos al backend mediante
contratos acordados. Las ingestas y transformaciones del ETL no se implementan aquí.

Todas las rutas de estas instrucciones son relativas a la raíz de `FactoredAI_BCK`.
Aunque este checkout esté dentro de la carpeta de trabajo del ETL, tiene su propio Git,
configuración, tickets y decisiones. Las convenciones de Python, `uv`, `make check`
y `src/factored_bank/` del repositorio padre pertenecen al ETL; no definen el stack del BCK.
El stack propio del BCK es Python 3.13, FastAPI y `uv`, con código en `src/factored_bck/`.
Su `.venv/`, `pyproject.toml`, `uv.lock`, `Makefile` y Dockerfile son independientes del ETL.
Ejecuta `make check` desde esta raíz para validar el backend y `make dev` para iniciarlo.

## Modo de trabajo

Para trabajo sustancial, usa `achiever-mode` y `toolkit:afinar`. Si falta contexto,
usa `plan-de-sesion`. Aplica proporcionalidad: las tareas triviales siguen siendo triviales.
Determina el resultado buscado, ejecuta, valida y cierra los pasos razonables dentro del alcance.

- Lee `README.md` y `CONTEXT.md` antes de implementar funcionalidades.
- Registra y verifica los contratos de datos que el backend consume del ETL.
- No supongas que un archivo o entorno del repositorio padre estará disponible en otro checkout.
- No publiques credenciales, datos restringidos ni registros individuales.
- Identifica las herramientas simuladas y verifica sus resultados antes de reportar acciones.
- Ejecuta las comprobaciones propias del backend; no uses las del ETL
  como evidencia de funcionamiento del BCK.

## Skills de desarrollo

### Gestor de issues

Issues locales en `.scratch/`. Ver `docs/agents/gestor-de-issues.md`.

### Dominio

Glosario único en `CONTEXT.md` y decisiones en `docs/adr/`.
Ver `docs/agents/dominio.md`.
