# Nodo por perfil y prueba de procesamiento

## Qué hace la célula base

Se clona `init_node` en cada PC. Se elige un perfil explícito y el inicializador obtiene solo sus módulos, con commits fijados y entornos separados. El hardware no decide automáticamente el rol.

| Equipo | Perfil activo probado | Módulos necesarios |
| --- | --- | --- |
| Debstat, alias SSH `deb` | `collector` | apolo-scraper |
| Hadron | `inference` | Korax; runtime/modelo GPU ya existentes |
| Zephyrus | `workstation` | apolo-rag, apolo-knowledge-data, apolo-skill-standard |

Los perfiles anteriores se conservan por compatibilidad. El RAG de prueba que antes corría en Debstat se detuvo; el backend de esta iteración corre en Zephyrus. No se borraron los espacios de trabajo anteriores.

```sh
# Debstat (Linux)
python3 node.py init --profile collector --root ~/apolo-collector --install
# Hadron (Linux, usar bundles si no dispone de acceso a los repos privados)
python3 node.py init --profile inference --root ~/apolo-node
# Zephyrus (Windows)
python node.py init --profile workstation --root node_workspace/workstation --install
```

Datos/RAG/scraper requieren uv y Python 3.12; el modelo y el binario GPU son requisitos externos explícitos. Las claves de OpenRouter solo se cargan en el proceso backend de Zephyrus.

## Qué se comprobó realmente

1. **Captura real universitaria en Debstat:** página de Ingeniería Civil Industrial de la U. de Chile, con el scraper publicado 73b8e4c y `http_soup`. El manifiesto informó `n_records=1`, `n_documents=1`, `n_chunks=8`, `n_errors=0`, `status=ok`. Generó `graduate_profile/0.0.1-draft` con resumen y evidencia; listas de competencias vacías en este caso.
2. **Procesamiento cerrado de un feed sintético:** un solo comando tomó un JSONL de Debstat, ingirió/exportó, envió el contrato a Hadron, ejecutó GPU y devolvió/importó resultados en Zephyrus. Corrida `auto-loop-20261104-b`: 25 entradas, 25 éxitos, 0 fallos, 50 menciones; RAG real contó Python=25 y SQL=25. Informe local: `node_workspace/workstation/runs/auto-loop-20261104-b/run.json`.
3. **Chat sin frontend:** `apolo-rag chat` comparte las rutas HTTP del frontend. Se probaron turnos, seguimiento, herramientas, estado, acciones, historial/listado, onboarding, entrevista, exploración y borrado. El chat consumió el lote nuevo y devolvió 25 avisos; los datos académicos del chat siguieron siendo fixtures, no la página recién raspada.
4. **Modelo real:** se cargó una clave local existente sin copiarla a los otros equipos. Una llamada API y otra desde la CLI respondieron con `backend=openrouter`, `model=openrouter/free`, y ejecución de `get_skill_alignment`. No se seleccionó un modelo pagado. Las demás conversaciones fueron deterministas con backend fake.
5. **Revisión independiente:** las correcciones de aislamiento de rutas, estado de sesión, codificación UTF-8 y transporte SSH se revisaron. Los logs y sesiones de prueba quedan en `node_workspace/` y no se versionan.

Verificación final ejecutada: 37 pruebas del inicializador/pipeline y 31 de CLI, conversación e interfaz API pasadas; `git diff --check` limpio para los cambios relevantes. La nueva comprobación SSH de raíz remota también pasó en Hadron tras la corrección de comillas.

AGY actuó como probador de entradas y como interlocutor de la API. El informe inicial de chat no encontró clave en `apolo_rag/.env`; la prueba real se realizó después de encontrarla en otro entorno local, sin mostrar su valor.

## Repetir el procesamiento

Ver [REMOTE_PIPELINE.md](REMOTE_PIPELINE.md). Ejemplo PowerShell, desde `init_node`, para un feed ya existente en Debstat:

```powershell
python scripts/remote_pipeline.py --root node_workspace/workstation --collector deb --feed /home/fabian/apolo-collector/feeds/synthetic-loop-20261104/jobs.jsonl --inference hadron --inference-root '~/apolo-node' --run-id otra-corrida --synthetic
```

`--synthetic` marca expresamente datos de prueba. Cada ejecución requiere un identificador nuevo y falla ante IDs faltantes, inesperados, duplicados, inferencias fallidas o reconciliación incorrecta. No activa una publicación global ni programa nuevas capturas.

## Usar la CLI de chat

La implementación está en el checkout local de `apolo_rag` y todavía no forma parte del commit remoto fijado en el manifiesto. Desde ese checkout, con un backend configurado en Zephyrus:

```powershell
# El modelo y la clave pertenecen al backend, no a la CLI.
$env:OPENROUTER_MODEL = 'openrouter/free'
# Archivo local de secretos/configuración, excluido de Git:
uv run --env-file RUTA_ENV_LOCAL uvicorn apolo_rag.api.main:app --host 127.0.0.1 --port 8000
```

En otra terminal del mismo checkout:

```powershell
uv run apolo-rag chat --session .tmp/sesion.json --backend fake --message 'Qué habilidades se piden?'
uv run apolo-rag chat --session .tmp/sesion.json --history
uv run apolo-rag chat --session .tmp/sesion.json --action evento.json
uv run apolo-rag chat --session .tmp/sesion.json --request GET /apolo/interview/questions
```

La CLI devuelve JSON completo (respuesta, bloques, componentes, herramientas y estado), conserva token/estado/conversación y permite el resto de operaciones mediante `--request`. Comparte las capacidades del backend; no intenta reproducir la presentación visual del navegador. Ver `apolo_rag/docs/chat_cli.md` para eventos y operaciones.

## Qué falta y dónde quedó

- [init_node #1: productor laboral real](https://github.com/apoloinc/init_node/issues/1). VectorJobs consume una base histórica; no se encontró el productor web de avisos. El feed procesado fue sintético.
- [apolo-scraper #6: contrato universitario → publicación/RAG](https://github.com/apoloinc/apolo-scraper/issues/6). El JSON draft no debe renombrarse como competencias oficiales sin validar su contenido y procedencia.
- Publicación/activación de snapshots: ya se sigue en [knowledge-data #7](https://github.com/apoloinc/apolo-knowledge-data/issues/7) y [RAG #4](https://github.com/apoloinc/apolo-rag/issues/4); no se duplicaron issues.
- La etiqueta histórica de procedencia en la versión publicada de RAG sigue relacionada con [RAG #12](https://github.com/apoloinc/apolo-rag/issues/12).
- Programación periódica, reanudación incremental y promoción automática deben integrarse con el runner/publicación existente tras versionarlos. Este script prueba una pasada completa de procesamiento, no una captura real continua.
- Los cambios de init_node y de la CLI permanecen locales hasta autorizar commit/push. Los clones remotos usados en la prueba recibieron los archivos de receta por SSH; un nuevo clon de GitHub aún no los obtiene.
