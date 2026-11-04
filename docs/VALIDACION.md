# Evidencia de validación

Sesión del 3 de noviembre de 2026 (Chile), 4 de noviembre UTC. No se publicaron cambios de los otros repositorios.

## Versiones probadas

| Módulo | Commit publicado |
| --- | --- |
| apolo-knowledge-data | 70142ecb6749cce2cfd250f6673dd486f7bc4954 |
| Korax | f3773579699f86430a303659664fe7dfa032b39c |
| apolo-rag | 63770601e2190482534e229645dd18c6c013e6f2 |
| apolo-skill-standard | cb32ce94a86a2c558ad0ddc72f8e72fb8366df29 |

Se descartó knowledge-data b175a1c para este conjunto: el RAG publicado requiere argumentos de procedencia en `build_catalog` que ese commit no implementa. El commit 70142ec publicado sí los implementa.

## Pruebas realizadas

- Suite del inicializador: 30 pruebas pasadas en Windows y 30 en Linux (deb); incluye regresiones para estado incompleto, referencias no fijadas, paquetes Git, rutas de artefactos enlazadas y aislamiento del entorno uv. Revisión independiente final sin bloqueos adicionales.
- Inicialización coordinator en Zephyrus, instalación completa con Python 3.12 y lock, y segunda ejecución de `--install` correcta. `doctor` verificó entornos, repositorios limpios, commits y catálogo.
- Inicialización rag en deb, instalación con `uv.lock` de RAG, lock de knowledge-data y generación del catálogo desde código fijado. Segunda instalación con la corrección final de aislamiento correcta. `doctor` y HTTP `/ready` correctos.
- Inicialización inference en Hadron con Korax sin dependencias Python externas; segunda inicialización correcta. Instalación adicional desde cero usando `--bundle-dir` en una raíz nueva correcta, conservando el origen Git canónico.
- `check-peers` real desde Zephyrus a deb y hadron: ambos devolvieron el token correcto. `/ready` de deb respondió además a través de un túnel SSH desde Zephyrus.
- Hadron: runtime llama.cpp b10517, Vulkan GTX 1080, GGUF Q4_K_M existente. `/health` correcto; no se descargaron pesos.
- Dos pilotos aislados, cada uno de 25 avisos sintéticos: el segundo usó la instalación nueva de Python 3.12 con dependencias fijadas, sin el entorno de desarrollo vecino. El segundo batch informó `total=25 ok=25 failed=0 skipped=0 elapsed=60.18s`.
- Importación del segundo piloto: 25 extracciones exitosas, 50 menciones, cero fallos de evidencia; igualdad exacta de IDs, no solo conteo de líneas.
- Consumo con backend **real** del RAG de deb: `job_count=25`, `sample_size=25`, Python=25 y SQL=25. Sin llamadas a un proveedor de chat externo.
- Suite local previa del runner pendiente de knowledge-data: seis pruebas pasadas. Esta evidencia no incorpora dicho código sin publicar a la receta.

Los datos del segundo piloto quedan bajo `node_workspace/runs/pilot25-locked` en Zephyrus, `~/apolo-node/runs/pilot25-locked` en los remotos. Datos, salidas, paquetes Git privados y pesos no forman parte del repositorio.

Huella de `requirement_mentions.parquet` del segundo piloto:
`ebc77f2366eaf3c856629d5488952231d5f5bc9c41ee06211e9d190f927611e7`.

## Alcance

La instalación se probó con acceso GitHub en Zephyrus/deb y paquetes Git para Hadron. Se reutilizó el runtime/modelo existente de Hadron. La provisión de hardware, drivers y pesos en un PC completamente vacío queda como requisito explícito, no como prueba ejecutada.

La versión publicada de RAG aún fija el texto de procedencia `hadron1600`; el piloto usa un override aislado y no constituye una publicación de datos reales. Ver [OPERACION.md](OPERACION.md).
