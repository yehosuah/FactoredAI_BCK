# Glosario del backend

| Término | Definición |
| --- | --- |
| BCK / backend | Componente de servicios y lógica de aplicación desarrollado en este repositorio. |
| ETL | Componente de extracción, transformación y carga de datos desarrollado en un repositorio separado. |
| Consumidor | Componente que utiliza una entrega de datos; el backend es consumidor de las entregas del ETL. |
| Contrato de datos | Acuerdo explícito sobre esquema, significado, versión y condiciones de disponibilidad de una entrega. |
| Entrega de datos | Resultado publicado por el ETL para consumo conforme a un contrato. |
| Fixture | Datos controlados de prueba, identificados por separado de los datos del organizador. |
| Entrega aceptada (`accepted release`) | Versión completa e inmutable publicada por el ETL; el puntero PostgreSQL indica la versión disponible para consumidores. |
| Estado del simulador (`simulator state`) | Estado persistente de una tarjeta para herramientas de prueba; separado del estado histórico del organizador y conservado tras refrescos ETL. |
| Sesión de prueba | Identidad autenticada, expirable y revocable asociada a un cliente; conocer su ID no concede acceso. |
| Acción simulada confirmada | Resultado de una herramienta de prueba confirmado y conservado como evidencia; no afirma ejecución bancaria ni resolución del problema del cliente. |
| Fallo HTTP operativo | Respuesta del backend con estado 4xx o 5xx; no equivale a una acción bancaria fallida ni determina su resolución. |
| Herramienta de soporte | Operación explícita de lectura o acción simulada, limitada al cliente autenticado; una intención solicitada no es evidencia de ejecución. |
| Handoff persistente | Caso de soporte con triage, evidencia backend separada y ciclo de vida durable; asignación no afirma aceptación ni resolución. |
| Triage | Contexto no confiable del cliente/modelo que describe qué ayuda se requiere; nunca elige identidad de cliente o agente. |
| Severidad | Riesgo del caso que determina el piso de experiencia, independiente del segmento. |
| Prioridad de servicio | Orden por severidad y preferencia Premium simulada dentro de la misma severidad; no reduce pisos. |
| Agente elegible | Cuenta de simulador aprovisionada cuyo snapshot cumple estado, tipo, idioma, especialidad y experiencia; no prueba disponibilidad en vivo. |
