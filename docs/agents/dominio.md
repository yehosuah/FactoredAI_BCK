# Dominio: glosario y decisiones

## Glosario

El vocabulario del backend vive en `CONTEXT.md`, en la raíz de este repositorio.
Es un glosario: no es una especificación, un cuaderno de notas ni un registro de implementación.
Cada entrada define un término y, cuando aplique, sus nombres anteriores descartados.
Usa ese vocabulario para nombrar conceptos en código y pruebas; agrega las definiciones que falten.

El BCK y el ETL tienen contextos propios. Los términos de un contrato compartido deben
mantener el mismo significado en ambos repositorios, sin depender de rutas del otro checkout.

## Decisiones de arquitectura

Se registran en `docs/adr/`, numeradas como `0001-titulo-corto.md`, cuando se cumplen
las tres condiciones:

1. Son costosas de revertir.
2. Sorprenderían a quien no conoce el contexto.
3. Hubo alternativas reales.

Si falta alguna condición, documenta la decisión en el cambio correspondiente.

```markdown
# NNNN - <título>

**Estado:** aceptada | reemplazada por NNNN

## Contexto
Hechos que obligaron a decidir.

## Decisión
Qué se decidió.

## Alternativas consideradas
Alternativas y razones para descartarlas.

## Consecuencias
Qué se facilita y qué se dificulta.
```

Cuando cambia una decisión, escribe otra y marca la anterior como reemplazada.
El registro conserva su historia.
