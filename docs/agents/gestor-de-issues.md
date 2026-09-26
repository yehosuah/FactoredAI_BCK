# Gestor de issues: archivos locales

Los issues del backend son archivos Markdown bajo `.scratch/` en este repositorio.
El remoto es `https://github.com/yehosuah/FactoredAI_BCK`, pero no tenía issues abiertos
ni cerrados al configurar este flujo. Por eso se usa el gestor local.

## Dónde van

```text
.scratch/<nombre-del-trabajo>/issues/<NN>-<slug>.md
```

`NN` empieza en `01` y numera en orden de dependencia: lo que bloquea va primero.

## Formato de un issue

```markdown
# <NN>: <título>

**Qué se construye:** comportamiento de punta a punta desde la perspectiva de quien lo usa.

**Bloqueado por:** números o títulos previos, o "Nada (se puede empezar ya)".

**Estado:** listo-para-agente

- [ ] Criterio de aceptación verificable
```

## Cómo se trabaja

Un issue está disponible cuando todos sus bloqueos están cerrados.
Usa `en-progreso` o `bloqueado` cuando corresponda y explica el bloqueo.
Para cerrarlo, marca sus criterios y cambia el estado a `cerrado`. No borres los archivos:
son el registro del trabajo.

Los tickets del ETL permanecen en su propio repositorio. Si una tarea de BCK depende de
una entrega del ETL, registra la referencia y el contrato esperado sin duplicar el ticket.

## Limitaciones

No hay asignación automática ni vista de dependencias. Quienes trabajen en paralelo deben
coordinar la toma de tickets. Para cambiar de gestor, vuelve a ejecutar `configurar-desarrollo`.
