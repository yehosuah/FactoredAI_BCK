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
