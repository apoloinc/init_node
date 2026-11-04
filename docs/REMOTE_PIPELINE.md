# Ejecución de una pasada collector → Hadron → Zephyrus

Este comando toma un JSONL ya escrito por el collector, lo ingesta con el repositorio de datos fijado, manda únicamente el contrato Korax a Hadron, trae resultados y los valida e importa en una carpeta de ejecución nueva. Finalmente consulta el resolver RAG local apuntando a esa carpeta. No activa publicaciones, programa scraping ni modifica snapshots históricos.

Requisitos: `workstation` instalado en Zephyrus con `--install`, acceso SSH desde Zephyrus a `deb` y `hadron`, y llama-server disponible en Hadron en `127.0.0.1:8080`. Los aliases deben estar en `~/.ssh/config` de Zephyrus. Sustituye `/ruta/absoluta/al/feed/jobs.jsonl` por un archivo existente en Debstat. Se necesita un feed JSONL en Debstat con `job_id`, `title` y `description_text`; no se extrae desde una base VectorJobs ni se inicia el scraper aquí. Use `--synthetic` para marcar explícitamente un feed sintético.

En Zephyrus, desde el checkout `init_node`:

```powershell
$py = '.\node_workspace\workstation\repos\apolo-knowledge-data\.venv\Scripts\python.exe'
& $py scripts\remote_pipeline.py --root .\node_workspace\workstation --collector deb --feed /ruta/absoluta/al/feed/jobs.jsonl --inference hadron --inference-root '~/apolo-node' --run-id pilot-20261104-01
```

En Linux, ejecuta el mismo script con `python3`:

```sh
python3 scripts/remote_pipeline.py --root ./node_workspace/workstation --collector deb --feed /ruta/absoluta/al/feed/jobs.jsonl --inference hadron --inference-root '~/apolo-node' --run-id pilot-20261104-01
```

El mismo `run-id` nunca se reutiliza. Con la raíz del ejemplo, los artefactos quedan bajo `node_workspace/workstation/runs/<run-id>/`; `run.json` registra el estado de cada etapa, el hash del feed, conteos, importación y respuesta del resolver. El proceso falla si el feed no ingresa completo, si Korax devuelve IDs faltantes/extra/duplicados o fallidos, o si falla el import/RAG. La importación queda en la carpeta de esa ejecución y el RAG se apunta a ella solo durante la comprobación.

Este cierre prueba procesamiento de un feed hasta la consulta RAG. No prueba generación de feeds por scraping, sync continuo collector→workstation, ni publicación/activación del resultado en el servicio conversacional. La RAG publicada disponible puede conservar procedencia histórica `hadron1600`; esta ejecución aislada no la reescribe.
