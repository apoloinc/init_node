# Instalación por rol

> Esta guía conserva la primera distribución de prueba. La distribución vigente es Debstat collector, Hadron inference y Zephyrus workstation: ver [iteración actual](ITERACION_20261104.md).

La receta obtiene código publicado: los commits están en `manifest.json`. Los cambios sin commit de otros repositorios de Zephyrus no forman parte de ella.

| Equipo probado | Perfil | Responsabilidad |
| --- | --- | --- |
| Zephyrus, Windows | coordinator | Preparación, importación, transporte SSH |
| deb (Debstat), Linux | rag | Consulta de extracciones con el resolver real |
| Hadron, Linux / GTX 1080 | inference | Korax y modelo de extracción local |

## Receta común

Requisitos: Git y Python >=3.10; para coordinator/rag, uv y Python 3.12 (`uv python install 3.12`). Se requiere acceso Git de lectura a los módulos privados. Los pesos y el runtime de inferencia se aprovisionan aparte; Korax incluye `apolo.py model --help` para gestionarlos. No hay descarga automática de pesos.

```sh
git clone https://github.com/apoloinc/init_node.git
cd init_node
python node.py init --profile coordinator --root node_workspace --install
python node.py doctor --root node_workspace
```

En Linux usar `python3`; cambiar el perfil a `rag` o `inference`. Inferencia no necesita `--install` ni uv. El rol es una propiedad del nodo, no del nombre del PC; para un rol adicional usar otra raíz. Repetir `init` valida los repositorios existentes; rechaza revisiones diferentes o árboles modificados. No actualiza versiones automáticamente. Para probar otro manifiesto, usar una raíz nueva.

## Equipo sin credenciales de GitHub

En un coordinador autorizado, generar un paquete por módulo desde los checkouts fijados:

```sh
git -C node_workspace/repos/korax bundle create /ruta/paquetes/korax.bundle HEAD
```

Crear previamente `/ruta/paquetes`. Transferir el paquete por SSH. En el destino:

```sh
python3 node.py init --profile inference --root ~/apolo-node --bundle-dir /ruta/paquetes
```

Se verifica el commit fijado y se conserva el origen canónico. Si un paquete falta, el módulo se intenta clonar desde GitHub. Los paquetes contienen código privado: transportarlos solo entre equipos autorizados, no publicarlos. Hadron se instaló por este mecanismo.

## Comprobar conexiones

En Zephyrus, con sus alias SSH existentes:

```sh
python node.py check-peers --topology examples/topology.json
```

Se comprueba ida/vuelta de un token aleatorio mediante SSH. Zephyrus coordina los enlaces; los alias `deb` y `hadron` pertenecen a su configuración local. No se necesita conexión directa entre los remotos. Se verificó que Hadron no alcanza la dirección LAN de deb. No se desactiva la verificación de claves de host.

## Piloto repetible de 25 avisos

Usar un nombre de ejecución nuevo cada vez. Estos comandos PowerShell parten de `init_node` en Zephyrus y de nodos remotos en `~/apolo-node`.

```powershell
$py = './node_workspace/repos/apolo-knowledge-data/.venv/Scripts/python.exe'
& $py scripts/pilot.py prepare --run node_workspace/runs/pilot25-nuevo
ssh hadron 'mkdir -p ~/apolo-node/runs/pilot25-nuevo'
scp node_workspace/runs/pilot25-nuevo/exchange/jobs.jsonl hadron:~/apolo-node/runs/pilot25-nuevo/jobs.jsonl
ssh hadron 'python3 ~/apolo-node/repos/korax/apolo.py --base-url http://127.0.0.1:8080/v1 --model apolo-extractor --no-pet batch --input ~/apolo-node/runs/pilot25-nuevo/jobs.jsonl --output ~/apolo-node/runs/pilot25-nuevo/results.jsonl'
scp hadron:~/apolo-node/runs/pilot25-nuevo/results.jsonl node_workspace/runs/pilot25-nuevo/exchange/results.jsonl
& $py scripts/pilot.py import --run node_workspace/runs/pilot25-nuevo
ssh deb 'mkdir -p ~/apolo-node/runs/pilot25-nuevo'
scp -r node_workspace/runs/pilot25-nuevo/enriched deb:~/apolo-node/runs/pilot25-nuevo/
scp scripts/check_rag.py deb:~/init_node/check_rag.py
ssh deb '~/apolo-node/repos/apolo-rag/.venv/bin/python ~/init_node/check_rag.py --root ~/apolo-node --enriched ~/apolo-node/runs/pilot25-nuevo/enriched'
```

Linux coordinador usa `node_workspace/repos/apolo-knowledge-data/.venv/bin/python`; los demás comandos SSH son equivalentes. Ajustar usuario y raíz remota al instalar en otros equipos. Los scripts rechazan reutilizar las salidas del piloto.

Aceptación: 25 IDs de entrada únicos, exactamente los mismos 25 IDs de salida, todos `ok`, cero fallos de evidencia al importar y conteos RAG de Python=25 y SQL=25. No basta contar líneas.

## Servicios probados y arranque manual

En deb, desde el nodo RAG instalado:

```sh
export APOLO_KNOWLEDGE_DATA_ROOT="$HOME/apolo-node/repos/apolo-knowledge-data"
export APOLO_SKILL_STANDARD_DIR="$HOME/apolo-node/artifacts/skill-standard"
export APOLO_HADRON_DIR="$HOME/apolo-node/runs/pilot25-nuevo/enriched"
export APOLO_CONVERSATION_DB="$HOME/apolo-node/conversations.sqlite3"
unset APOLO_SETUP_MODE
~/apolo-node/repos/apolo-rag/.venv/bin/python -m uvicorn apolo_rag.api.main:app --host 127.0.0.1 --port 18000
```

Consultar `/health` y `/ready`. Desde Zephyrus: `ssh -N -L 127.0.0.1:18080:127.0.0.1:18000 deb`, luego `http://127.0.0.1:18080/ready`. `/ready` valida la API/base de conversaciones; `scripts/check_rag.py` valida adicionalmente el consumo real del piloto.

Hadron reutilizó el binario existente `~/worker/runtime/b10517/llama-b10517/llama-server` y el modelo `~/worker/models/apolo-korax-qwen3.5-4b-lora-r08-v0.1.0-Q4_K_M.gguf`. Arranque compatible probado:

```sh
export LD_LIBRARY_PATH="$HOME/worker/runtime/b10517/llama-b10517:$HOME/worker/runtime/libgomp/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
~/worker/runtime/b10517/llama-b10517/llama-server --model ~/worker/models/apolo-korax-qwen3.5-4b-lora-r08-v0.1.0-Q4_K_M.gguf --gpu-layers 99 --ctx-size 4096 --parallel 1 --threads 8 --threads-batch 8 --batch-size 256 --ubatch-size 64 --cache-type-k q8_0 --cache-type-v q8_0 --flash-attn on --cache-ram 0 --host 127.0.0.1 --port 8080 --alias apolo-extractor --reasoning off
```

Este runtime usa Vulkan y reconoce la GTX 1080. En otros equipos se debe elegir/verificar un runtime y modelo compatibles antes de ejecutar Korax. Diagnóstico: `python3 node.py doctor --root ~/apolo-node --url http://127.0.0.1:8080/health`.

Los procesos de esta prueba son manuales, sin arranque al reiniciar. Logs/PID en Hadron: `~/apolo-node-runtime/llama-server.{log,pid}`; deb: `~/apolo-node/rag.{log,pid}`. Para detenerlos, verificar el PID y comando antes de usar `kill`; los repositorios históricos no se administran desde este inicializador.

## Límites comprobados

- El RAG publicado conserva etiquetas de procedencia `hadron1600` al leer el piloto sintético por override. Los conteos se validaron contra el piloto; esas etiquetas no deben presentarse como procedencia real en producción.
- La taxonomía del commit fijado genera `skill-standard-v0.0.5-seed`. No se presupone la versión citada en informes anteriores.
- Los cambios locales de runner incremental/publicación y los 48 commits de RAG de Zephyrus no se publican ni se incorporan aquí. Esta receta instala la base publicada compatible y prueba el flujo de extracción/importación/consumo, no la publicación nueva pendiente.
- No se probaron Silverbox, otros sistemas operativos ni aprovisionamiento desde cero de drivers/binarios/pesos. El bootstrap no sustituye sus requisitos de hardware.
