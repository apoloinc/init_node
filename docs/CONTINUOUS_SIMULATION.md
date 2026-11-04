# Simulación de adquisición realista y publicación continua

Este documento describe la simulación acotada de ciclo continuo implementada en `scripts/simulate_continuous.py`, su modelo de datos y ciclo de vida, las garantías de seguridad y publicación atómica, y las limitaciones operativas conocidas.

## Propósito

Permitir la ejecución de varios ciclos secuenciales de adquisición simulada desde el collector (`deb`), procesamiento distribuido en Hadron (`hadron`) mediante `scripts/remote_pipeline.py`, publicación inmutable de resultados verificados y avance atómico de un puntero local `current.json` en Zephyrus (`workstation`).

El diseño respeta estrictamente los roles probados:
- **Debstat (`deb`)**: collector / scraping de entradas.
- **Hadron (`hadron`)**: nodo GPU exclusivo (modelo de extracción Korax).
- **Zephyrus (`workstation`)**: orquestación, ingestión, RAG y almacenamiento local.

No requiere servicios nuevos, frameworks externos ni demonios en segundo plano.

---

## Escenario realista acotado (5 ciclos)

Para garantizar una prueba representativa y finita, la simulación ejecuta un escenario determinista acotado a un total de 10 trabajos GPU (máximo permitido <= 12):

1. **Ciclo 1 (`initial_snapshot`):**
   - Snapshot inicial con 3 ofertas laborales sintéticas en español (`sim-job-101`, `sim-job-102-v1`, `sim-job-103`).
   - Cada aviso incluye `job_id`, `title`, `company_name`, `source_url` (dominio reservado `example.invalid`), `published_at` (ISO 8601) y `description_text` con requerimientos explícitos de Python y SQL.
   - Envío a Hadron: 3 trabajos GPU.
   - Creación de manifiesto inmutable `pub-<sim_id>-c1.json` y avance atómico del puntero `current.json`.
   - Consulta de comprobación RAG local verificada.
   - Delta registrado: 3 añadidos, 0 modificados, 0 retirados, 0 sin cambios.

2. **Ciclo 2 (`delta_snapshot`):**
   - Segundo snapshot con cambios de ciclo de vida manteniendo identificadores estables:
     - 1 nuevo: `sim-job-104` (Especialista en Automatización de Datos).
     - 1 modificado: `sim-job-102-v2` (Desarrollador Backend y Datos Senior, texto y título actualizados).
     - 1 retirado: `sim-job-103` (omitido del feed activo).
     - 1 sin cambios: `sim-job-101` (Ingeniero de Datos Python y SQL).
   - Total activos en snapshot: 3 ofertas.
   - Envío a Hadron: 3 trabajos GPU (el pipeline actual procesa el feed completo; no se simula reutilización granular de extracciones intermedias).
   - Avance atómico del puntero a Ciclo 2.
   - Delta registrado: 1 añadido, 1 modificado, 1 retirado, 1 sin cambios.

3. **Ciclo 3 (`exact_repeat`):**
   - Repetición exacta del feed del Ciclo 2 (mismo hash SHA-256).
   - **Omisión de cómputo y publicación:** el orquestador detecta que el feed es idéntico al puntero activo y omite la invocación de inferencia y la generación de un nuevo manifiesto.
   - Trabajos GPU usados: 0.
   - El puntero `current.json` permanece inalterado apuntando al Ciclo 2.

4. **Ciclo 4 (`controlled_failure`):**
   - Inyección controlada de un feed con esquema inválido (registros sin campos requeridos `title` y `description_text`).
   - El procesador falla de manera controlada y previsible (`returncode != 0`).
   - **Invariante verificado:** la falla esperada no corrompe ni modifica el puntero `current.json`, y la publicación del Ciclo 2 continúa intacta y consultable.

5. **Ciclo 5 (`recovery`):**
   - Recuperación inmediata mediante un snapshot válido que restaura el flujo e incorpora una nueva vacante: `sim-job-105` (Ingeniero de Pipelines de Datos).
   - Total activos en snapshot: 4 ofertas (`sim-job-101`, `sim-job-102-v2`, `sim-job-104`, `sim-job-105`).
   - Envío a Hadron: 4 trabajos GPU.
   - Procesamiento e importación exitosos; avance atómico del puntero a Ciclo 5.
   - Comprobación RAG local final validada.

**Consumo total GPU:** 3 + 3 + 0 + 0 + 4 = 10 trabajos GPU (acotado a <= 12).

---

## Ejecución del comando

Desde `init_node` en Zephyrus (Windows PowerShell):

```powershell
$py = '.\node_workspace\workstation\repos\apolo-knowledge-data\.venv\Scripts\python.exe'
& $py scripts\simulate_continuous.py --root .\node_workspace\workstation --collector deb --inference hadron --inference-root '~/apolo-node' --simulation-id sim-pilot-20261104-01 --interval-seconds 1
```

Equivalente en Linux/POSIX:

```sh
python3 scripts/simulate_continuous.py --root ./node_workspace/workstation --collector deb --inference hadron --inference-root ~/apolo-node --simulation-id sim-pilot-20261104-01 --interval-seconds 1
```

### Comprobación y lectura del puntero activo

Para inspeccionar y validar la publicación activa sin ejecutar ciclos nuevos:

```powershell
& $py scripts\simulate_continuous.py --root .\node_workspace\workstation --simulation-id sim-pilot-20261104-01 --check-only
```

Devuelve el JSON del puntero y del manifiesto activo tras verificar la integridad de los hashes y la existencia de los artefactos.

---

## Estructura de almacenamiento y seguridad

Para garantizar aislamiento y reproducibilidad, los archivos se distribuyen de la siguiente manera:

```text
node_workspace/workstation/
├── runs/
│   ├── sim-pilot-20261104-01-c1/
│   │   ├── feed.jsonl
│   │   ├── run.json
│   │   └── enriched/
│   │       ├── requirement_mentions.parquet
│   │       ├── job_extractions.parquet
│   │       ├── failed_extractions.parquet
│   │       └── manifest.json
│   ├── sim-pilot-20261104-01-c2/
│   └── ...
└── simulations/
    └── sim-pilot-20261104-01/
        ├── current.json                  <- Puntero activo (actualizado con os.replace)
        ├── simulation_report.json        <- Resumen global del escenario
        ├── staged_feeds/
        │   ├── cycle_1.jsonl
        │   └── cycle_2.jsonl
        └── publications/
            ├── pub-sim-pilot-20261104-01-c1.json  <- Manifiesto inmutable
            ├── pub-sim-pilot-20261104-01-c2.json
            └── pub-sim-pilot-20261104-01-c5.json
```

En el collector remoto Debstat:
- Los feeds se ubican en: `~/apolo-collector/simulations/<simulation_id>/cycle_<N>/feed.jsonl`.
- Antes de transferir, el script resuelve `~` por SSH a una ruta absoluta y usa esa ruta en las operaciones remotas y el manifiesto.
- Se escriben mediante un archivo temporal `.tmp` y se promueven con `mv` para garantizar escritura atómica remota.
- No se tocan rutas históricas ni archivos de datos legados.

### Contención y seguridad

1. **Validación de identificadores y alias:** `simulation_id`, alias SSH (`collector`, `inference`) y rutas remotas se validan contra expresiones regulares estrictas; no se permite interpolación de comandos ni metacaracteres.
2. **Contención de symlinks y junctions:** las rutas locales de simulaciones, corridas y artefactos se validan antes de escribir; se rechazan symlinks y junctions NTFS.
3. **Escritura atómica:** `current.json` se escribe primero en un archivo temporal en el mismo directorio y se renombra atómicamente con `os.replace()`.
4. **Inmutabilidad:** los manifiestos de publicación se abren con el modo exclusivo `x` y nunca se sobreescriben.
5. **Sin borrados destructivos:** no se realizan operaciones de borrado recursivo (`rmtree`) sobre el historial de corridas ni publicaciones.
6. **Validación previa a promoción:** la corrida debe reportar todas las importaciones correctas, cero fallos y conteos RAG coincidentes antes de crear el manifiesto y mover `current.json`. Un ID de simulación existente no se reutiliza.
7. **Integridad del historial:** el puntero enlaza publicación, corrida, hash del feed y directorio enriquecido. Se verifican los hashes y tamaños del informe y cada artefacto requerido. Las referencias de módulos provienen de los commits guardados en `state.json`.

---

## Advertencias y límites operativos conocidos

1. **Datos sintéticos:** el scraper real de ofertas laborales no está disponible (`init_node` issue #1). Las ofertas procesadas son explícitamente sintéticas (`synthetic=true`) con dominios `.invalid`.
2. **Contrato de simulación vs publicador de producción:** este script implementa y valida el contrato de publicación de la simulación. No reemplaza el publicador de producción pendiente en `apolo_knowledge_data` (`runner.py` / `publish_rag.py` sin versionar), ni importa fuentes no fijadas.
3. **Reutilización por aviso:** cuando un snapshot cambia, el pipeline procesa el snapshot completo. No se afirma reutilización de extracciones a nivel de aviso individual en esta iteración.
4. **Sin recarga automática en API 18001:** el servicio HTTP local (puerto 18001) no recarga automáticamente el backend ante un cambio de puntero. La comprobación del consumidor RAG consulta la herramienta real directamente (`apolo_rag.tools.labor_market.get_labor_market`) parametrizando `APOLO_HADRON_DIR` al directorio enriquecido resuelto del puntero.
5. **Programación:** el orquestador es una CLI finita y acotada; la ejecución continua en producción requerirá temporizadores o cron cuando los servicios productivos estén formalmente integrados.
