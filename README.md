# init_node

Una sola receta para preparar equipos APOLO. Elige un perfil y el inicializador descarga los repositorios necesarios, fija sus versiones e instala sus entornos.

| Equipo | Perfil | Trabajo actual |
| --- | --- | --- |
| Debstat (`deb`) | `collector` | Scraping universitario; recepción de feeds laborales de prueba |
| Hadron | `inference` | Extracción con Korax y GPU |
| Zephyrus | `workstation` | Importación, RAG y pruebas de conversaciones |

**Estado:** probado entre los tres equipos con datos sintéticos y una captura universitaria real. La adquisición real de avisos laborales y la operación autónoma de producción siguen pendientes. Ver [qué está listo y qué falta](docs/NODE_READINESS.md).

## 1. Preparar los requisitos

En todos los equipos: Git, Python **3.10 o superior** y acceso de lectura a los repositorios privados de los módulos.

En Debstat y Zephyrus: [instala uv](https://docs.astral.sh/uv/getting-started/installation/) y comprueba que esté en el PATH. Después prepara Python 3.12:

```sh
uv --version
uv python install 3.12
```

Hadron ejecuta Korax sin dependencias Python externas. Su driver, runtime de inferencia y modelo GGUF se aprovisionan aparte; `init_node` no los descarga ni arranca automáticamente.

## 2. Clonar la misma base en cada equipo

Desde la carpeta donde quieras guardar la receta:

```sh
git clone https://github.com/apoloinc/init_node.git
cd init_node
```

Todos los comandos siguientes se ejecutan desde esa carpeta `init_node`.

## 3. Activar el perfil correspondiente

Ejecuta **solo el bloque del equipo que estás preparando**.

### Zephyrus — PowerShell

```powershell
python node.py init --profile workstation --root node_workspace/workstation --install
python node.py doctor --root node_workspace/workstation
```

### Debstat — Linux

```sh
python3 node.py init --profile collector --root ~/apolo-collector --install
python3 node.py doctor --root ~/apolo-collector
```

El módulo publicado del collector captura universidades. El [productor laboral real](https://github.com/apoloinc/init_node/issues/1) todavía no está conectado.

### Hadron — Linux

```sh
python3 node.py init --profile inference --root ~/apolo-node
python3 node.py doctor --root ~/apolo-node
```

Con el modelo y `llama-server` ya iniciados en Hadron, comprueba también el servicio:

```sh
python3 node.py doctor --root ~/apolo-node --url http://127.0.0.1:8080/health
```

La inferencia necesita el endpoint `http://127.0.0.1:8080/v1` y el alias de modelo `apolo-extractor`. Ver [el arranque de Hadron probado](docs/OPERACION.md#servicios-probados-y-arranque-manual).

**Resultado esperado:** `init` y `doctor` terminan con código 0 y muestran `"status": "ok"`. Un `doctor` sin `--url` verifica la instalación, no que los servicios estén arrancados.

¿El equipo no tiene acceso a los repositorios privados? Usa [paquetes Git sin copiar credenciales](docs/OPERACION.md#equipo-sin-credenciales-de-github).

## 4. Comprobar la comunicación desde Zephyrus

Configura los alias `deb` y `hadron` en el SSH de Zephyrus. Estos dos comandos deben funcionar sin pedir contraseña:

```powershell
ssh -o BatchMode=yes deb hostname
ssh -o BatchMode=yes hadron hostname
python node.py check-peers --topology examples/topology.json
```

`check-peers` debe devolver `"status": "ok"` y verificar un token de ida y vuelta para ambos equipos. Los alias pertenecen a Zephyrus; los remotos no necesitan conocerse entre sí.

## 5. Probar adquisición y publicación simuladas

Con los tres perfiles preparados y el servidor de inferencia de Hadron activo, ejecuta desde Zephyrus:

```powershell
python scripts/simulate_continuous.py --root node_workspace/workstation --simulation-id sim-demo-01 --interval-seconds 1
```

Usa un **identificador nuevo** cada vez. Los destinos por defecto son `deb`, `hadron` y `~/apolo-node` en Hadron; puedes cambiarlos con `--collector`, `--inference` y `--inference-root`.

La prueba termina por sí sola. Debe completar cinco ciclos: entrada inicial, cambios, repetición omitida, fallo controlado que conserva la publicación y recuperación. El resultado global esperado es `"status": "success"`, tres publicaciones válidas y diez extracciones GPU.

Para verificar después la integridad de la publicación activa, sin repetir inferencias:

```powershell
python scripts/simulate_continuous.py --root node_workspace/workstation --simulation-id sim-demo-01 --check-only
```

Los informes y datos quedan en `node_workspace/workstation/simulations/sim-demo-01/` y `node_workspace/workstation/runs/`. La prueba marca los datos como sintéticos y mantiene su publicación aislada de la del chat.

La simulación usa el resolver RAG real, pero no necesita frontend ni clave de OpenRouter. El chat completo sigue pendiente de publicación en el módulo `apolo-rag`; esta receta no afirma instalar esa CLI desde el commit fijado. Las claves de chat se configuran únicamente en Zephyrus.

## Si algo falla

| Síntoma | Qué hacer |
| --- | --- |
| `uv` no encontrado | Revisa `uv --version`. En Linux, si está en `~/.local/bin`, añade `export PATH="$HOME/.local/bin:$PATH"` en esa terminal |
| GitHub rechaza el acceso a un módulo | Configura acceso de lectura o usa los paquetes Git del enlace anterior |
| `/health` no responde | Inicia y verifica el runtime/modelo en Hadron; `init` no arranca el servidor |
| SSH falla | Prueba `ssh deb hostname` y `ssh hadron hostname` antes de ejecutar la simulación |
| La simulación ya existe | Elige otro `--simulation-id`; las publicaciones anteriores se conservan |
| Repositorio modificado o versión diferente | Conserva el workspace y usa otra ruta `--root`; el inicializador no sobrescribe cambios |

## Actualizar y profundizar

Actualiza la receta con `git pull --ff-only`. `manifest.json` fija las versiones de los módulos: no sigue sus ramas automáticamente. Si cambia una versión fijada, prepara un workspace nuevo con otra ruta `--root` para conservar el anterior.

Los perfiles `coordinator` y `rag` siguen disponibles por compatibilidad; para la distribución actual usa la tabla inicial.

- [Estado verificado y pendientes de producción](docs/NODE_READINESS.md).
- [Detalle de la simulación continua y sus garantías](docs/CONTINUOUS_SIMULATION.md).
- [Procesar un feed JSONL que ya existe en Debstat](docs/REMOTE_PIPELINE.md).
- [Evidencia de la iteración anterior](docs/ITERACION_20261104.md).

Para ejecutar las pruebas locales del repositorio:

```sh
python -m unittest discover -s tests -q
```

En Linux usa `python3` en ese último comando. Las pruebas unitarias no necesitan SSH, GPU ni claves.
