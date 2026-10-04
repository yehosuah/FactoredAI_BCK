# Evidencia de datos para routing — 2026-10-03

Inspección de solo lectura de datos reales `organizer_synthetic`, no fixtures del
equipo. No se publican registros individuales, nombres, contactos ni IDs de agentes.

## Procedencia y límite de verificación

Repositorio productor inspeccionado: `FactoredAI_base`, hermano de este checkout.
Entrega preparada:
`data/processed/releases/a43445e3755ddbcf914ce430353c16dea922265815230edcdda2b833fa806630/`.
Manifest creado `2026-10-03T03:33:14.636560Z`, contrato `card-support-etl-v1`.
Extract de origen:
`624156a5cce033722fba62bd27fb2441227ca68395022c58aca1ce9bd3bb6be6`.
Se verificaron hashes SHA-256 de los CSV/Parquet y manifests utilizados.

El CSV original contiene 1200 agentes; la entrega tipada acepta **1174** y rechaza
**26** por códigos de empleado duplicados. Hay 811 agentes aceptados con flags de
sucursal no resuelta. Routing no usa sucursal. El snapshot original tiene fecha de
modificación `2026-09-01T02:49:26Z` y fue ingerido `2026-10-02T19:21:38.506398Z`.
No declara fecha de corte de negocio ni partición de proceso.

**No se verificó publicación actual en PostgreSQL**: el backend local no tiene una
conexión de datos configurada. Este análisis de una entrega preparada no sustituye
`bank.current_release`. La implementación debe consultar solo el release aceptado
por PostgreSQL en ejecución, sin leer estos archivos ni depender del checkout ETL.

## Distribuciones observadas

| Experiencia | Total | Active | Vacation | Leave | Inactive |
| --- | ---: | ---: | ---: | ---: | ---: |
| Junior | 18 | 16 | 1 | 1 | 0 |
| Mid-Senior | 134 | 125 | 9 | 0 | 0 |
| Senior | 279 | 246 | 19 | 7 | 7 |
| Specialist | 743 | 678 | 32 | 21 | 12 |
| Total | 1174 | 1065 | 61 | 29 | 19 |

| Especialidad | Total | Active |
| --- | ---: | ---: |
| null | 462 | 428 |
| Fraudes | 103 | 94 |
| Retención | 95 | 84 |
| Cobranza | 94 | 84 |
| Soporte Técnico | 94 | 87 |
| Créditos | 86 | 79 |
| Inversiones | 81 | 72 |
| Ventas | 81 | 73 |
| Quejas y Reclamos | 78 | 64 |

Especialidad nula: **39.35%**. No existe especialidad explícita de soporte general
de tarjetas. Null no demuestra capacidad especializada.

| Idiomas (string real) | Agentes |
| --- | ---: |
| español | 631 |
| español, inglés | 417 |
| español, portugués | 66 |
| español, inglés, portugués | 60 |

Sin idiomas nulos. Tokens separados por coma: 1174 hablan español, 477 inglés y
126 portugués. No se encontraron discrepancias de espacios. Entre los activos,
112 hablan portugués: Junior 2, Mid-Senior 16, Senior 24, Specialist 70. De esos
112, 7 tienen Fraudes (2 Mid-Senior, 2 Senior, 3 Specialist), 7 Quejas y Reclamos
(1 Mid-Senior, 1 Senior, 5 Specialist), y 43 no tienen especialidad.

| Tipo | Total |
| --- | ---: |
| Phone | 576 |
| Digital | 242 |
| In-Person | 228 |
| Hybrid | 128 |

Agentes Active de tipos Phone/Digital/Hybrid combinados: Junior 14, Mid-Senior 101,
Senior 191, Specialist 550. Tipos entre activos que hablan portugués: Phone 54,
Digital 23, Hybrid 11, In-Person 24.

| Variable | No nulos | Nulos | Mín | Q1 | Mediana | Q3 | Máx | Media |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| avg_csat | 1044 | 130 (11.07%) | 3.50 | 3.91 | 4.28 | 4.63 | 5.00 | 4.2753 |
| total_monthly_interactions | 1065 | 109 (9.28%) | 100 | 280 | 455 | 632 | 800 | 453.7934 |

Entre Active: 115 tienen CSAT nulo y 94 interacciones mensuales nulas. Turnos:
Afternoon 411, Morning 388, Rotating 199, Night 176.

**Active no prueba disponibilidad en vivo; work_shift no prueba presencia;
total_monthly_interactions no mide carga de cola actual.** No hay señales para
optimización de fuerza laboral en tiempo real ni pesos ML.

## Segmentos de clientes y contrato de consumo

En el mismo Parquet preparado, 150000 clientes: Basic 89756, Plus 37547,
Premium 15207, Student 7490; sin segmentos nulos. `customers.segment` es
`VARCHAR(50)` requerido. El segmento debe provenir del cliente autenticado en el
release aceptado; ni saldos, profesión, apellido ni texto del modelo lo sustituyen.

`bank.service_agents` es una tabla relacional con clave `(release_id, agent_id)`.
Los campos necesarios existen tipados: `agent_id VARCHAR(20)`, `agent_type
VARCHAR(30)`, `experience_level VARCHAR(20)`, `languages VARCHAR(100)`, `specialty
VARCHAR(100)` nullable, `avg_csat DECIMAL(3,2)` nullable,
`total_monthly_interactions INTEGER` nullable y `agent_status VARCHAR(20)`.

El loader ETL inspeccionado no concede al rol `backend_api` acceso a
`service_agents` ni a `customers.segment`. Esto requiere configuración de permisos
de lectura mínima por el propietario de la base. No se modificó el repositorio
ETL ni se concedieron permisos a una base existente durante este trabajo.

## Reproducción

Se usó DuckDB en memoria con el runtime ya existente del ETL, leyendo solo columnas
agregables de sus Parquet verificados. Consultas base:

```sql
SELECT experience_level, agent_status, count(*) FROM a GROUP BY 1,2;
SELECT specialty, count(*) FROM a GROUP BY 1;
SELECT languages, count(*) FROM a GROUP BY 1;
SELECT agent_type, count(*) FROM a GROUP BY 1;
SELECT count(avg_csat), min(avg_csat), quantile_cont(avg_csat, [0.25,0.5,0.75]),
       max(avg_csat), avg(avg_csat) FROM a;
```

`a` apunta al `service_agents.parquet` de la entrega indicada. Las mismas funciones
se aplicaron a interacciones mensuales; las combinaciones filtraron
`agent_status='Active'` y tokens de idioma. No se imprimieron datos individuales.

## Cobertura de la política Digital/Hybrid

Entre Active, Digital/Hybrid suman 329: Junior 6, Mid-Senior 44, Senior 77,
Specialist 202. Todos incluyen español y 34 portugués. Fraudes tiene 25 candidatos
de estos tipos/estado (2 portugués, ambos Specialist); Quejas 26 (4 portugués:
1 Mid-Senior, 1 Senior, 2 Specialist); Soporte Técnico 36 (5 portugués: 1 Junior,
4 Specialist). Sin especialidad hay 120, de ellos 12 portugués. Estos son límites
del snapshot antes de exigir cuentas de simulador aprovisionadas, no asignaciones
ni disponibilidad en vivo.

El REVOKE del loader también elimina grants directos por columna. Una prueba en
PostgreSQL 17.11 desechable verificó que una membresía heredada de un rol NOLOGIN
con grants mínimos conserva acceso tras ese refresh, mientras email sigue denegado.
El SQL de despliegue del BCK usa ese patrón; no se aplicó a una base existente.
