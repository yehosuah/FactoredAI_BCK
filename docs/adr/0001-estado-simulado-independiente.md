# 0001 - Estado simulado independiente del ETL

**Estado:** aceptada, acordada con el usuario el 2026-10-01.

## Contexto

El ETL publica snapshots históricos. El prototipo debe confirmar herramientas de
soporte de tarjetas y conservar acciones de prueba después de un refresco de datos.
Los IDs del organizador no son credenciales ni estados actuales de un banco.

## Decisión

Consumir `card-support-etl-v1` con un rol de lectura restringido y guardar sesiones,
estado de tarjetas y auditoría en un esquema propio. Inicializar explícitamente desde
Active/Closed/Blocked; Suspended es ineligible. Activación inicial usa fixtures del
equipo. Cada acción verifica cliente, elegibilidad e idempotencia en una transacción.
Reemplazo y cargo desconocido registran solicitudes, sin afirmar ejecución bancaria.

## Alternativas consideradas

Modificar las tablas ETL perdería trazabilidad histórica. Reinicializar estado en cada
release borraría acciones confirmadas. Autenticar por ID de cliente permitiría acceso
sin una identidad verificada.

## Consecuencias

Los refrescos conservan estado y auditoría. Cada lectura identifica su release y
semántica histórica. El aprovisionamiento de usuarios sigue siendo administrativo y
las herramientas permanecen simuladas; no se afirma una integración con un banco real.
